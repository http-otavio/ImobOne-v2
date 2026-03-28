"""
tests/test_qa_journeys.py — Testes unitários de agents/qa_journeys.py.

Mínimo exigido: 4 testes (CLAUDE.md).

Testes:
  1. 10/10 jornadas aprovadas → done, score 100%
  2. 8/10 jornadas aprovadas → blocked (80% < 85% threshold)
  3. Jornada com critério crítico reprovado → blocked independente do score
  4. Relatório de falha contém jornada, critério, severidade e sugestão

IMPORTANTE: Nenhum teste chama o LLM real.
  consultant_fn e evaluator_fn são callables mockados com resultados predeterminados.
  Zero custo, zero não-determinismo, cobertura completa da lógica de QA.
"""

from __future__ import annotations

import pytest

from agents.qa_journeys import (
    JORNADAS_BASE,
    SEVERIDADE_CRITICO,
    SEVERIDADE_IMPORTANTE,
    SEVERIDADE_INFORMATIVO,
    THRESHOLD_APROVACAO,
    Criterio,
    Jornada,
    QAJourneysAgent,
    ResultadoJornada,
    _calcular_metricas,
    _executar_jornada,
)


# ---------------------------------------------------------------------------
# Factories de mocks injetáveis
# ---------------------------------------------------------------------------


def _make_consultant_fn(resposta_fixa: str = "Resposta padrão do consultor."):
    """
    Retorna um consultant_fn que sempre retorna a mesma string.
    Simula o consultor sem chamar o LLM.
    """
    async def consultant(mensagens: list[dict]) -> str:
        return resposta_fixa
    return consultant


def _make_evaluator_all_pass():
    """
    Retorna um evaluator_fn que aprova todos os critérios.
    Simula avaliação perfeita sem chamar o LLM.
    """
    async def evaluator(criterio: Criterio, resposta: str) -> tuple[bool, str]:
        return True, ""
    return evaluator


def _make_evaluator_fail_for(ids_jornada_a_reprovar: set[str], severidade_falha: str = SEVERIDADE_IMPORTANTE):
    """
    Retorna um evaluator_fn que reprova o PRIMEIRO critério da severidade
    especificada nas jornadas cujo ID está em ids_jornada_a_reprovar.

    Simula respostas do consultor que falham em jornadas específicas.
    """
    reprovacoes: dict[str, bool] = {jid: False for jid in ids_jornada_a_reprovar}

    async def evaluator(criterio: Criterio, resposta: str) -> tuple[bool, str]:
        # A jornada ID não está disponível aqui diretamente — usamos estado mutable
        # O avaliador reprova o critério de severidade_falha até marcar como reprovado
        for jid in list(reprovacoes.keys()):
            if not reprovacoes[jid] and criterio.severidade == severidade_falha:
                reprovacoes[jid] = True  # marca como reprovada (só uma vez por jornada)
                return False, f"Sugestão de correção automática para critério '{criterio.descricao}'."
        return True, ""
    return evaluator


def _make_evaluator_controlled(
    resultados: dict[str, dict[str, tuple[bool, str]]],
):
    """
    Retorna um evaluator_fn com resultados completamente controlados.

    Args:
        resultados: {jornada_id: {criterio_descricao: (passou, sugestao)}}
    """
    # Rastreamos qual jornada está sendo avaliada via contador
    contexto = {"jornada_idx": 0, "criterio_idx": 0, "jornada_atual": None}

    async def evaluator(criterio: Criterio, resposta: str) -> tuple[bool, str]:
        # Busca em todos os dicts de jornada pelo critério
        for jid, crits in resultados.items():
            if criterio.descricao in crits:
                return crits[criterio.descricao]
        # Default: aprovado se não especificado
        return True, ""
    return evaluator


def _make_jornada_simples(
    id: str,
    nome: str,
    criterios: list[Criterio] | None = None,
) -> Jornada:
    """Fábrica de jornadas simples para testes."""
    return Jornada(
        id=id,
        nome=nome,
        mensagens_simuladas=[{"role": "user", "content": "Mensagem de teste."}],
        criterios=criterios or [
            Criterio(
                descricao="deve responder de forma adequada",
                severidade=SEVERIDADE_IMPORTANTE,
                sugestao_correcao="Revisar prompt base.",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Teste 1 — 10/10 jornadas aprovadas → done, score 100%
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dez_de_dez_aprovadas_retorna_done_score_100():
    """
    Quando todas as 10 jornadas base passam em todos os critérios,
    run() deve retornar ('done', payload) com score_percentual = 100.

    Verifica:
    - status == 'done'
    - score_percentual == 100.0
    - jornadas_aprovadas == 10
    - jornadas_reprovadas == 0
    - tem_critico_reprovado == False
    - payload tem a lista completa de jornadas
    - nenhuma jornada reprovada nos detalhes
    """
    agent = QAJourneysAgent(
        consultant_fn=_make_consultant_fn("Boa tarde! Vou verificar a escola mais próxima."),
        evaluator_fn=_make_evaluator_all_pass(),
    )

    status, payload = await agent.run("cliente_qa_001", {})

    assert status == "done", f"Esperava 'done' com 10/10, got '{status}': {payload.get('motivo_bloqueio', '')}"
    assert payload["score_percentual"] == 100.0, f"Score esperado 100%, got {payload['score_percentual']}%"
    assert payload["jornadas_aprovadas"] == 10
    assert payload["jornadas_reprovadas"] == 0
    assert payload["tem_critico_reprovado"] is False
    assert len(payload["jornadas"]) == 10
    assert len(payload["jornadas_reprovadas_detalhes"]) == 0

    # Threshold correto registrado no payload
    assert payload["threshold_percentual"] == THRESHOLD_APROVACAO
    assert payload["aprovado_por_score"] is True

    # As 10 jornadas base estão todas presentes
    ids_retornados = {j["jornada_id"] for j in payload["jornadas"]}
    ids_esperados = {j.id for j in JORNADAS_BASE}
    assert ids_esperados == ids_retornados, (
        f"IDs de jornadas divergem. Ausentes: {ids_esperados - ids_retornados}"
    )


# ---------------------------------------------------------------------------
# Teste 2 — 8/10 jornadas aprovadas → blocked (score 80% < 85%)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oito_de_dez_aprovadas_retorna_blocked():
    """
    Com 8/10 jornadas aprovadas (score 80%), run() deve retornar 'blocked'
    porque 80% está abaixo do threshold de 85%.

    Verifica:
    - status == 'blocked'
    - score_percentual == 80.0
    - aprovado_por_score == False
    - motivo_bloqueio presente e menciona o score
    - 2 jornadas nos detalhes de reprovadas
    """
    # 10 jornadas, 2 reprovadas via evaluator controlado
    # Usamos as duas primeiras jornadas base como as que falham
    j0_id = JORNADAS_BASE[0].id  # j01_familia_escola
    j1_id = JORNADAS_BASE[1].id  # j02_investidor_rentabilidade

    # Pré-computar quais critérios reprovar: primeiro critério de cada jornada
    primeiro_criterio_j0 = JORNADAS_BASE[0].criterios[0].descricao
    primeiro_criterio_j1 = JORNADAS_BASE[1].criterios[0].descricao

    resultados_controlados = {
        j0_id: {
            primeiro_criterio_j0: (False, "Sugestão específica para j01."),
        },
        j1_id: {
            primeiro_criterio_j1: (False, "Sugestão específica para j02."),
        },
    }

    agent = QAJourneysAgent(
        consultant_fn=_make_consultant_fn("Resposta genérica que falha nos critérios."),
        evaluator_fn=_make_evaluator_controlled(resultados_controlados),
    )

    status, payload = await agent.run("cliente_qa_002", {})

    assert status == "blocked", f"Esperava 'blocked' com 80%, got '{status}'"
    assert payload["score_percentual"] == 80.0, (
        f"Score esperado 80%, got {payload['score_percentual']}%"
    )
    assert payload["aprovado_por_score"] is False
    assert payload["jornadas_aprovadas"] == 8
    assert payload["jornadas_reprovadas"] == 2
    assert "motivo_bloqueio" in payload
    assert "80" in payload["motivo_bloqueio"] or "threshold" in payload["motivo_bloqueio"].lower()

    # 2 jornadas reprovadas no detalhe
    assert len(payload["jornadas_reprovadas_detalhes"]) == 2
    ids_reprovados = {j["jornada_id"] for j in payload["jornadas_reprovadas_detalhes"]}
    assert j0_id in ids_reprovados
    assert j1_id in ids_reprovados

    # THRESHOLD_APROVACAO deve ser 85%
    assert THRESHOLD_APROVACAO == 85.0, f"Threshold esperado: 85.0%. Atual: {THRESHOLD_APROVACAO}"


# ---------------------------------------------------------------------------
# Teste 3 — Critério crítico reprovado → blocked independente do score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterio_critico_reprovado_bloqueia_independente_do_score():
    """
    Mesmo com score >= 85%, um critério CRÍTICO reprovado em qualquer jornada
    deve bloquear o deploy.

    Cenários:
    3a. 9/10 jornadas aprovadas (score 90%) MAS critério crítico reprovado → blocked
    3b. 10/10 jornadas aprovadas com critério informativo reprovado → done
        (critérios informativos não bloqueiam)

    Verifica para 3a:
    - status == 'blocked'
    - score >= 85% (aprovado_por_score == True)
    - tem_critico_reprovado == True
    - motivo_bloqueio menciona o critério crítico
    """
    # 3a. Cenário: 9/10 aprovadas + critério crítico reprovado na 10ª
    # A j10 (desconto abusivo) tem um critério crítico
    j10 = JORNADAS_BASE[-1]  # j10_desconto_abusivo
    assert any(c.severidade == SEVERIDADE_CRITICO for c in j10.criterios), (
        "j10 deve ter critério crítico para este teste"
    )
    primeiro_critico_j10 = next(
        c for c in j10.criterios if c.severidade == SEVERIDADE_CRITICO
    )

    resultados_controlados_3a = {
        j10.id: {
            primeiro_critico_j10.descricao: (False, "Sugestão de correção para critério crítico."),
        }
    }

    agent_3a = QAJourneysAgent(
        consultant_fn=_make_consultant_fn("Claro, posso oferecer 30% de desconto!"),  # resposta errada
        evaluator_fn=_make_evaluator_controlled(resultados_controlados_3a),
    )

    status_3a, payload_3a = await agent_3a.run("cliente_qa_003a", {})

    assert status_3a == "blocked", (
        f"Esperava 'blocked' com critério crítico reprovado, got '{status_3a}'"
    )
    assert payload_3a["tem_critico_reprovado"] is True
    assert payload_3a["score_percentual"] >= THRESHOLD_APROVACAO, (
        "Score deve ser >= threshold — o bloqueio é pelo critério crítico, não pelo score"
    )
    assert payload_3a["aprovado_por_score"] is True, (
        "aprovado_por_score deve ser True — o bloqueio é exclusivamente pelo critério crítico"
    )
    assert j10.id in payload_3a["jornadas_criticas_reprovadas"]
    assert "motivo_bloqueio" in payload_3a
    assert j10.id in payload_3a["motivo_bloqueio"]

    # 3b. Critério apenas informativo reprovado — não bloqueia
    # Criamos jornadas específicas para este cenário
    jornada_com_informativo = _make_jornada_simples(
        id="j_test_informativo",
        nome="Jornada com critério informativo",
        criterios=[
            Criterio(
                descricao="deve mencionar horário de funcionamento",
                severidade=SEVERIDADE_INFORMATIVO,
                sugestao_correcao="Adicionar horário ao onboarding.",
            )
        ],
    )

    resultados_3b = {
        "j_test_informativo": {
            "deve mencionar horário de funcionamento": (False, "Sugestão informativa."),
        }
    }

    agent_3b = QAJourneysAgent(
        consultant_fn=_make_consultant_fn("Resposta sem horário de funcionamento."),
        evaluator_fn=_make_evaluator_controlled(resultados_3b),
        jornadas_extras=[jornada_com_informativo],
    )

    # Injeta apenas a jornada com informativo — as 10 base passam tudo
    # Para isolar: usamos apenas a jornada extra via agent direto
    # Precisamos garantir que as 10 base passem — evaluator aprova todo o resto
    status_3b, payload_3b = await agent_3b.run("cliente_qa_003b", {})

    # Com 10 base aprovadas + 1 extra com só informativo reprovado → done
    # (informativo não conta no score de aprovação)
    assert status_3b == "done", (
        f"Critério informativo reprovado não deve bloquear. got '{status_3b}': "
        f"{payload_3b.get('motivo_bloqueio', '')}"
    )
    assert payload_3b["tem_critico_reprovado"] is False


# ---------------------------------------------------------------------------
# Teste 4 — Relatório contém jornada, critério, severidade e sugestão
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relatorio_de_falha_contem_campos_completos():
    """
    O payload de 'blocked' deve conter, para cada jornada reprovada:
    - jornada_id e jornada_nome
    - criterios com: descricao, severidade, passou=False, sugestao

    O relatório deve ser acionável: quem lê sabe exatamente o que corrigir
    antes de resubmeter o pipeline.

    Verifica:
    - jornadas_reprovadas_detalhes tem todos os campos
    - criterios_reprovados tem sugestao preenchida
    - severidade preservada (critico/importante/informativo)
    - Relatório geral tem: score, threshold, totais
    """
    # Jornada com critério crítico reprovado e sugestão específica
    jornada_teste = Jornada(
        id="j_relatorio_teste",
        nome="Jornada para validação de relatório",
        mensagens_simuladas=[{"role": "user", "content": "Preciso de um apartamento pet-friendly."}],
        criterios=[
            Criterio(
                descricao="deve verificar se o empreendimento aceita animais",
                severidade=SEVERIDADE_CRITICO,
                sugestao_correcao="Adicionar campo 'aceita_pets' ao schema de imóvel no ingestion.",
            ),
            Criterio(
                descricao="deve oferecer alternativas pet-friendly se não houver no portfólio",
                severidade=SEVERIDADE_IMPORTANTE,
                sugestao_correcao="Adicionar fallback de 'sem opções disponíveis' ao nó RECOMENDAÇÃO.",
            ),
        ],
    )

    # O evaluator reprova ambos os critérios desta jornada
    async def evaluator_reprova_tudo(criterio: Criterio, resposta: str) -> tuple[bool, str]:
        # Só reprova para a jornada de teste — deixa as 10 base passarem
        if criterio.descricao in (
            "deve verificar se o empreendimento aceita animais",
            "deve oferecer alternativas pet-friendly se não houver no portfólio",
        ):
            return False, criterio.sugestao_correcao
        return True, ""

    agent = QAJourneysAgent(
        consultant_fn=_make_consultant_fn("Temos ótimas opções para você!"),
        evaluator_fn=evaluator_reprova_tudo,
        jornadas_extras=[jornada_teste],
    )

    status, payload = await agent.run("cliente_qa_004", {})

    # Com a jornada extra falhando em critério crítico → blocked
    assert status == "blocked", f"Esperava 'blocked', got '{status}'"

    # Relatório deve ter campos de visão geral
    assert "score_percentual" in payload
    assert "threshold_percentual" in payload
    assert "total_jornadas" in payload
    assert "jornadas_aprovadas" in payload
    assert "jornadas_reprovadas" in payload

    # A jornada reprovada deve estar nos detalhes
    assert len(payload["jornadas_reprovadas_detalhes"]) >= 1
    jornada_reprovada = next(
        (j for j in payload["jornadas_reprovadas_detalhes"]
         if j["jornada_id"] == "j_relatorio_teste"),
        None,
    )
    assert jornada_reprovada is not None, "Jornada de teste não encontrada no relatório"

    # Campos obrigatórios da jornada reprovada
    assert jornada_reprovada["jornada_id"] == "j_relatorio_teste"
    assert jornada_reprovada["jornada_nome"] == "Jornada para validação de relatório"
    assert jornada_reprovada["aprovada"] is False
    assert jornada_reprovada["tem_criterio_critico_reprovado"] is True

    # Critérios reprovados com campos completos
    criterios = jornada_reprovada["criterios"]
    assert len(criterios) == 2

    criterio_critico = next(
        (c for c in criterios if c["severidade"] == SEVERIDADE_CRITICO),
        None,
    )
    assert criterio_critico is not None, "Critério crítico não encontrado no relatório"
    assert criterio_critico["passou"] is False
    assert criterio_critico["sugestao"], "Sugestão deve estar preenchida"
    assert "ingestion" in criterio_critico["sugestao"].lower() or "pets" in criterio_critico["sugestao"].lower()
    assert criterio_critico["severidade"] == SEVERIDADE_CRITICO

    criterio_importante = next(
        (c for c in criterios if c["severidade"] == SEVERIDADE_IMPORTANTE),
        None,
    )
    assert criterio_importante is not None, "Critério importante não encontrado"
    assert criterio_importante["passou"] is False
    assert criterio_importante["sugestao"], "Sugestão do critério importante deve estar preenchida"

    # motivo_bloqueio deve mencionar a jornada crítica
    assert "j_relatorio_teste" in payload["motivo_bloqueio"]
