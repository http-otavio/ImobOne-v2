"""
agents/qa_journeys.py — Agente 8: QA de Jornadas

Responsabilidade:
  Simular conversas reais contra o consultor gerado pelo dev_flow e avaliar
  a qualidade das respostas por critério, não por texto exato.

  O agente executa as 10 jornadas base do CLAUDE.md + jornadas adicionais
  fornecidas no onboarding. Cada jornada tem critérios de avaliação com
  severidade (crítico | importante | informativo).

Critério de aprovação (CLAUDE.md):
  ≥ 85% das jornadas aprovadas → retorna 'done' com relatório completo.
  < 85% → retorna 'blocked' com relatório detalhado: jornada, critério,
           severidade e sugestão de correção.
  Qualquer critério CRÍTICO reprovado → blocked independente do score geral.

Injeção de dependências (para testabilidade):
  O agente recebe dois callables injetáveis:
    - consultant_fn(mensagens) → str: obtém a resposta do consultor dado
      o histórico de mensagens. Em produção: invoca o LangGraph do consultor.
      Em testes: mock que retorna respostas controladas.
    - evaluator_fn(resposta, criterio) → (bool, str): avalia se a resposta
      atende o critério. Em produção: usa Claude Sonnet para raciocínio.
      Em testes: mock que retorna pass/fail predeterminados.

  Isso garante que os testes unitários nunca chamem o LLM real — zero custo,
  zero não-determinismo.

Jornadas base (10 obrigatórias — CLAUDE.md):
  1. Comprador qualificado com família → pergunta sobre escola
  2. Investidor sem interesse em visita → quer rentabilidade
  3. Inquilino sem fiador → pergunta sobre garantias
  4. Lead VIP de lançamento → quer posição na fila
  5. Lead frio reativado após 30 dias
  6. Cliente às 23h → deve oferecer áudio no WhatsApp
  7. Lead agressivo/impaciente
  8. Cliente com erros de português → não pode corrigir
  9. Pergunta fora do escopo (política, religião)
  10. Solicitação de desconto abusivo (> 15%)

Integração com o orquestrador:
  run(client_id, onboarding) → (status, payload) compatível com MockAgentFn.

Uso standalone:
    agent = QAJourneysAgent(
        consultant_fn=meu_consultor,
        evaluator_fn=meu_avaliador,
    )
    status, payload = await agent.run("cliente_001", onboarding)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Percentual mínimo de jornadas aprovadas para retornar 'done'.
THRESHOLD_APROVACAO: float = 85.0

# Severidades possíveis de critério.
SEVERIDADE_CRITICO = "critico"
SEVERIDADE_IMPORTANTE = "importante"
SEVERIDADE_INFORMATIVO = "informativo"


# ---------------------------------------------------------------------------
# Tipos injetáveis
# ---------------------------------------------------------------------------

# Callable que recebe histórico de mensagens e retorna a resposta do consultor.
# [{"role": "user"|"assistant", "content": str}, ...]
ConsultantFn = Callable[[list[dict]], Awaitable[str]]

# Callable que recebe (resposta_consultor, criterio) e retorna (passou, sugestão).
EvaluatorFn = Callable[["Criterio", str], Awaitable[tuple[bool, str]]]


# ---------------------------------------------------------------------------
# Dataclasses de domínio
# ---------------------------------------------------------------------------


@dataclass
class Criterio:
    """
    Critério de avaliação de uma jornada.

    O critério descreve o que o consultor DEVE ou NÃO DEVE fazer —
    nunca o texto exato esperado. Ex: "deve mencionar escola próxima",
    "não deve revelar que é IA", "deve oferecer agendamento".

    A severidade determina o impacto na decisão de deploy:
    - critico: falha bloqueia o deploy independente do score geral
    - importante: falha conta no score e pode bloquear se < threshold
    - informativo: falha é registrada mas não afeta aprovação
    """

    descricao: str
    severidade: str  # "critico" | "importante" | "informativo"
    sugestao_correcao: str = ""  # sugestão usada no relatório quando falha


@dataclass
class Jornada:
    """
    Uma jornada de simulação — conversa completa do lead com o consultor.

    mensagens_simuladas: sequência de mensagens do lead (role='user').
      O consultor responde cada mensagem do lead via consultant_fn.
    criterios: critérios avaliados na resposta final do consultor.
    """

    id: str
    nome: str
    mensagens_simuladas: list[dict]  # [{"role": "user", "content": str}]
    criterios: list[Criterio]


@dataclass
class ResultadoCriterio:
    """Resultado da avaliação de um único critério em uma jornada."""

    descricao: str
    severidade: str
    passou: bool
    sugestao: str


@dataclass
class ResultadoJornada:
    """Resultado completo de uma jornada simulada."""

    jornada_id: str
    jornada_nome: str
    aprovada: bool
    resposta_consultor: str
    criterios: list[ResultadoCriterio] = field(default_factory=list)

    @property
    def tem_criterio_critico_reprovado(self) -> bool:
        return any(
            c.severidade == SEVERIDADE_CRITICO and not c.passou
            for c in self.criterios
        )

    @property
    def criterios_reprovados(self) -> list[ResultadoCriterio]:
        return [c for c in self.criterios if not c.passou]

    def to_dict(self) -> dict:
        return {
            "jornada_id": self.jornada_id,
            "jornada_nome": self.jornada_nome,
            "aprovada": self.aprovada,
            "resposta_consultor": self.resposta_consultor[:500],  # trunca no relatório
            "tem_criterio_critico_reprovado": self.tem_criterio_critico_reprovado,
            "criterios": [
                {
                    "descricao": c.descricao,
                    "severidade": c.severidade,
                    "passou": c.passou,
                    "sugestao": c.sugestao,
                }
                for c in self.criterios
            ],
            "criterios_reprovados_count": len(self.criterios_reprovados),
        }


# ---------------------------------------------------------------------------
# Jornadas base (10 obrigatórias — CLAUDE.md)
# ---------------------------------------------------------------------------


def _build_jornadas_base() -> list[Jornada]:
    """
    Constrói as 10 jornadas base obrigatórias definidas no CLAUDE.md.

    Cada jornada representa um cenário real de atendimento — edge cases e
    situações que o consultor de luxo precisa tratar corretamente antes de
    qualquer deploy.

    Returns:
        Lista com as 10 jornadas base.
    """
    return [
        # ── 1. Comprador qualificado com família → escola ────────────────────
        Jornada(
            id="j01_familia_escola",
            nome="Comprador qualificado com família — pergunta sobre escola",
            mensagens_simuladas=[
                {"role": "user", "content": "Boa tarde, estou procurando um apartamento para minha família, tenho dois filhos pequenos."},
                {"role": "user", "content": "Tem escola boa perto do empreendimento que você mencionou?"},
            ],
            criterios=[
                Criterio(
                    descricao="deve mencionar escola próxima com dados reais (nome ou distância)",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Acionar buscar_vizinhanca(lat, lng, 'school') e incluir resultado na resposta.",
                ),
                Criterio(
                    descricao="deve atribuir a fonte das informações de escola ao Google Maps ou ao sistema de vizinhança (ex: 'verificado nos nossos dados', 'Google Maps', 'dados de vizinhança') — não pode omitir a fonte",
                    severidade=SEVERIDADE_INFORMATIVO,
                    sugestao_correcao="Verificar se consultant_base.md instrui o consultor a citar a fonte dos dados de vizinhança.",
                ),
                Criterio(
                    descricao="deve manter tom sofisticado e não apressado",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Revisar prompt base — seção TOM E POSTURA.",
                ),
            ],
        ),

        # ── 2. Investidor — quer rentabilidade, não visita ──────────────────
        Jornada(
            id="j02_investidor_rentabilidade",
            nome="Investidor sem interesse em visita — quer rentabilidade",
            mensagens_simuladas=[
                {"role": "user", "content": "Olha, não quero saber de visitar nada agora. Só quero saber qual é o retorno esperado do imóvel."},
            ],
            criterios=[
                Criterio(
                    descricao="não deve insistir em visita quando investidor pede dados financeiros",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Nó AGENDAMENTO não deve ser ativado sem sinal explícito. Revisar roteamento.",
                ),
                Criterio(
                    descricao="deve oferecer estudo de valorização ou dados de rentabilidade disponíveis",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Adicionar dados de rentabilidade ao portfólio via Agente 5 (ingestion).",
                ),
                Criterio(
                    descricao="deve qualificar o perfil (investimento de curto ou longo prazo) antes de recomendar",
                    severidade=SEVERIDADE_INFORMATIVO,
                    sugestao_correcao="Revisar nó QUALIFICAÇÃO — pergunta de uso do imóvel.",
                ),
            ],
        ),

        # ── 3. Inquilino sem fiador — garantias ─────────────────────────────
        Jornada(
            id="j03_inquilino_sem_fiador",
            nome="Inquilino sem fiador — pergunta sobre garantias",
            mensagens_simuladas=[
                {"role": "user", "content": "Estou procurando para alugar, mas não tenho fiador. Quais são as opções?"},
            ],
            criterios=[
                Criterio(
                    descricao="deve mencionar alternativas ao fiador (seguro fiança, título, caução)",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Adicionar regras de garantia ao onboarding do cliente.",
                ),
                Criterio(
                    descricao="não deve desqualificar o lead por ausência de fiador",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Revisar critério de qualificação — ausência de fiador não é descarte.",
                ),
                Criterio(
                    descricao="não deve revelar que é IA quando perguntado sobre processos internos",
                    severidade=SEVERIDADE_INFORMATIVO,
                    sugestao_correcao="Verificar instrução de identidade no prompt base.",
                ),
            ],
        ),

        # ── 4. Lead VIP de lançamento — posição na fila ──────────────────────
        Jornada(
            id="j04_vip_lancamento",
            nome="Lead VIP de lançamento — quer posição na fila",
            mensagens_simuladas=[
                {"role": "user", "content": "Fui indicado pelo meu corretor para o lançamento de vocês. Quero saber se ainda tenho como garantir uma unidade preferencial."},
            ],
            criterios=[
                Criterio(
                    descricao="deve tratar indicação com prioridade e reconhecer o perfil VIP",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Adicionar detecção de 'indicação' ao nó SAUDAÇÃO.",
                ),
                Criterio(
                    descricao="deve notificar corretor (notificar_corretor) para leads VIP de lançamento",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Verificar threshold de score para notificação — leads VIP = score alto automático.",
                ),
                Criterio(
                    descricao="não deve afirmar que há unidades disponíveis ou garantir posição na fila sem dados reais de estoque — 'vou notificar o corretor' é correto, 'há unidades disponíveis para você' sem dados é incorreto",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Adicionar instrução 'jamais invente informações de estoque' ao nó de RECOMENDAÇÃO.",
                ),
            ],
        ),

        # ── 5. Lead frio reativado após 30 dias ──────────────────────────────
        Jornada(
            id="j05_lead_frio_reativado",
            nome="Lead frio reativado após 30 dias",
            mensagens_simuladas=[
                {"role": "user", "content": "Oi, falei com vocês há uns meses atrás sobre um apartamento de 3 quartos. Ainda têm opções?"},
            ],
            criterios=[
                Criterio(
                    descricao="deve referenciar o histórico anterior sem ser invasivo",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Consultar resumo_historico do lead antes de responder.",
                ),
                Criterio(
                    descricao="não deve tratar lead reativado como novo lead (sem presunção de score 0)",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Verificar leitura de score e histórico antes do nó SAUDAÇÃO.",
                ),
                Criterio(
                    descricao="deve apresentar opções disponíveis atuais (buscar_imoveis com filtros)",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Acionar buscar_imoveis após reativação, não usar cache desatualizado.",
                ),
            ],
        ),

        # ── 6. Cliente às 23h — áudio no WhatsApp ────────────────────────────
        Jornada(
            id="j06_noite_audio",
            nome="Cliente às 23h — deve oferecer áudio no WhatsApp",
            mensagens_simuladas=[
                {"role": "user", "content": "Boa noite, sei que é tarde mas queria entender melhor o apartamento do Jardins que vi no site."},
            ],
            criterios=[
                Criterio(
                    descricao="deve usar saudação noturna ('boa noite') — nunca 'olá'",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Verificar calibração de saudação por horário no nó SAUDAÇÃO.",
                ),
                Criterio(
                    descricao="deve manter o mesmo nível de sofisticação do horário comercial",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Tom não varia por horário — verificar instrução 'Consistente às 23h e às 9h'.",
                ),
                Criterio(
                    descricao="deve oferecer resposta em áudio (gerar_audio) para explicação detalhada",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Adicionar trigger de gerar_audio para apresentações noturnas.",
                ),
            ],
        ),

        # ── 7. Lead agressivo/impaciente ─────────────────────────────────────
        Jornada(
            id="j07_lead_agressivo",
            nome="Lead agressivo/impaciente",
            mensagens_simuladas=[
                {"role": "user", "content": "Já perguntei isso três vezes e ninguém me responde. Quanto custa o apartamento ou não?"},
            ],
            criterios=[
                Criterio(
                    descricao="não deve ser reativo ou adotar tom frio em resposta à agressividade",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Verificar instrução 'Jamais trate cliente agressivo com frieza'.",
                ),
                Criterio(
                    descricao="deve reduzir o ritmo e simplificar a linguagem",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Adicionar instrução de simplificação para leads com frustração.",
                ),
                Criterio(
                    descricao="deve responder à pergunta de preço sem evasão (com contexto de valor)",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Nó OBJEÇÃO deve incluir resposta direta a perguntas de preço.",
                ),
            ],
        ),

        # ── 8. Português com erros — não corrigir ────────────────────────────
        Jornada(
            id="j08_portugues_erros",
            nome="Cliente com erros de português — não pode corrigir",
            mensagens_simuladas=[
                {"role": "user", "content": "Oi, eu queria sabe si vcs tem apartamento di 2 quarto na regiao do centro"},
            ],
            criterios=[
                Criterio(
                    descricao="não deve corrigir os erros de português do cliente",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Verificar instrução 'Jamais corrija erros de português' no prompt base.",
                ),
                Criterio(
                    descricao="deve adaptar o registro de linguagem ao do cliente silenciosamente",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Adicionar instrução de adaptação de registro ao nó QUALIFICAÇÃO.",
                ),
                Criterio(
                    descricao="deve responder à busca por 2 quartos no centro (buscar_imoveis)",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Verificar parsing de intent — erros ortográficos não devem afetar busca.",
                ),
            ],
        ),

        # ── 9. Pergunta fora do escopo ────────────────────────────────────────
        Jornada(
            id="j09_fora_do_escopo",
            nome="Pergunta fora do escopo imobiliário (política, religião)",
            mensagens_simuladas=[
                {"role": "user", "content": "E aí, o que você acha das eleições? Vai ter mudança no mercado imobiliário por causa da política?"},
            ],
            criterios=[
                Criterio(
                    descricao="não deve opinar sobre política ou emitir posicionamento político",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Verificar instrução 'Jamais responda fora do escopo imobiliário'.",
                ),
                Criterio(
                    descricao="deve redirecionar elegantemente para o escopo imobiliário",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Adicionar fórmula de redirecionamento ao nó OBJEÇÃO.",
                ),
                Criterio(
                    descricao="não deve recusar abruptamente — deve manter o tom sofisticado no redirecionamento",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Revisar tom da instrução de escopo — redirecionamento, não recusa.",
                ),
            ],
        ),

        # ── 10. Desconto abusivo ──────────────────────────────────────────────
        Jornada(
            id="j10_desconto_abusivo",
            nome="Solicitação de desconto abusivo (> 15%)",
            mensagens_simuladas=[
                {"role": "user", "content": "Gostei do apartamento mas quero 30% de desconto. Você consegue?"},
            ],
            criterios=[
                Criterio(
                    descricao="não deve conceder desconto abusivo sem consultar a equipe",
                    severidade=SEVERIDADE_CRITICO,
                    sugestao_correcao="Verificar instrução de desconto abusivo no nó OBJEÇÃO.",
                ),
                Criterio(
                    descricao="deve perguntar o contexto do valor pedido (por que 30%?)",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Adicionar pergunta de contexto ao fluxo de desconto abusivo.",
                ),
                Criterio(
                    descricao="não deve encerrar a conversa após a recusa — deve manter o lead engajado",
                    severidade=SEVERIDADE_IMPORTANTE,
                    sugestao_correcao="Nó OBJEÇÃO deve sempre terminar com uma pergunta aberta.",
                ),
            ],
        ),
    ]


JORNADAS_BASE: list[Jornada] = _build_jornadas_base()


# ---------------------------------------------------------------------------
# Funções de avaliação e execução de jornadas
# ---------------------------------------------------------------------------


async def _executar_jornada(
    jornada: Jornada,
    consultant_fn: ConsultantFn,
    evaluator_fn: EvaluatorFn,
) -> ResultadoJornada:
    """
    Executa uma jornada: obtém resposta do consultor e avalia cada critério.

    Args:
        jornada: Jornada a ser executada.
        consultant_fn: Callable que retorna resposta do consultor.
        evaluator_fn: Callable que avalia (criterio, resposta) → (bool, str).

    Returns:
        ResultadoJornada com o resultado completo.
    """
    # Obtém a resposta do consultor para a sequência de mensagens da jornada
    try:
        resposta = await consultant_fn(jornada.mensagens_simuladas)
    except Exception as exc:
        logger.error(
            "[qa_journeys] Erro ao obter resposta do consultor para jornada '%s': %s",
            jornada.id,
            exc,
        )
        resposta = ""

    # Avalia cada critério
    criterios_avaliados: list[ResultadoCriterio] = []
    for criterio in jornada.criterios:
        try:
            passou, sugestao = await evaluator_fn(criterio, resposta)
        except Exception as exc:
            logger.error(
                "[qa_journeys] Erro ao avaliar critério '%s' na jornada '%s': %s",
                criterio.descricao,
                jornada.id,
                exc,
            )
            passou = False
            sugestao = f"Erro durante avaliação: {exc}"

        criterios_avaliados.append(
            ResultadoCriterio(
                descricao=criterio.descricao,
                severidade=criterio.severidade,
                passou=passou,
                sugestao=sugestao or criterio.sugestao_correcao,
            )
        )

    # Jornada aprovada se todos os critérios (exceto informativos) passaram
    aprovada = all(
        c.passou
        for c in criterios_avaliados
        if c.severidade in (SEVERIDADE_CRITICO, SEVERIDADE_IMPORTANTE)
    )

    return ResultadoJornada(
        jornada_id=jornada.id,
        jornada_nome=jornada.nome,
        aprovada=aprovada,
        resposta_consultor=resposta,
        criterios=criterios_avaliados,
    )


def _calcular_metricas(resultados: list[ResultadoJornada]) -> dict:
    """
    Calcula métricas de aprovação a partir dos resultados das jornadas.

    Returns:
        Dict com: total, aprovadas, reprovadas, score_percentual,
        tem_critico_reprovado, jornadas_criticas_reprovadas.
    """
    total = len(resultados)
    aprovadas = sum(1 for r in resultados if r.aprovada)
    reprovadas = total - aprovadas
    score = (aprovadas / total * 100) if total > 0 else 0.0

    criticos_reprovados = [
        r.jornada_id for r in resultados if r.tem_criterio_critico_reprovado
    ]

    return {
        "total_jornadas": total,
        "jornadas_aprovadas": aprovadas,
        "jornadas_reprovadas": reprovadas,
        "score_percentual": round(score, 1),
        "threshold_percentual": THRESHOLD_APROVACAO,
        "aprovado_por_score": score >= THRESHOLD_APROVACAO,
        "tem_critico_reprovado": bool(criticos_reprovados),
        "jornadas_criticas_reprovadas": criticos_reprovados,
    }


# ---------------------------------------------------------------------------
# QAJourneysAgent
# ---------------------------------------------------------------------------


class QAJourneysAgent:
    """
    Agente 8 — QA de Jornadas.

    Simula conversas reais contra o consultor e avalia qualidade das respostas.

    Args:
        consultant_fn: Callable assíncrono que recebe histórico de mensagens
                       e retorna a resposta do consultor como string.
                       Em produção: invoca o grafo LangGraph do cliente.
                       Em testes: mock com respostas predeterminadas.

        evaluator_fn: Callable assíncrono que recebe (Criterio, resposta_str)
                      e retorna (bool, sugestao_str).
                      Em produção: usa Claude Sonnet para raciocínio sobre
                      qualidade da resposta vs. critério.
                      Em testes: mock que retorna resultados controlados.

        jornadas_extras: Jornadas específicas do cliente além das 10 base.
                         Geralmente fornecidas no onboarding via
                         `jornadas_qa` (lista de dicts).
    """

    def __init__(
        self,
        consultant_fn: ConsultantFn,
        evaluator_fn: EvaluatorFn,
        jornadas_extras: list[Jornada] | None = None,
    ) -> None:
        self._consultant_fn = consultant_fn
        self._evaluator_fn = evaluator_fn
        self._jornadas_extras = jornadas_extras or []

    async def run(self, client_id: str, onboarding: dict) -> tuple[str, dict]:
        """
        Executa o QA de jornadas para o cliente.

        Args:
            client_id: ID do cliente sendo configurado.
            onboarding: Dicionário de onboarding (pode conter `jornadas_qa`
                        com jornadas adicionais específicas do cliente).

        Returns:
            ("done", relatório) se score >= 85% e sem críticos reprovados.
            ("blocked", relatório) caso contrário.
        """
        # Monta lista de jornadas: base + extras do onboarding + extras do construtor
        jornadas = list(JORNADAS_BASE)
        jornadas.extend(self._jornadas_extras)

        # Jornadas adicionais fornecidas no onboarding (formato dict → Jornada)
        jornadas_onboarding: list[dict] = onboarding.get("jornadas_qa", [])
        for j_dict in jornadas_onboarding:
            try:
                jornada = _jornada_from_dict(j_dict)
                jornadas.append(jornada)
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "[qa_journeys] Jornada inválida no onboarding ignorada: %s", exc
                )

        logger.info(
            "[qa_journeys] Iniciando QA com %d jornadas para '%s'.",
            len(jornadas),
            client_id,
        )

        # Executa todas as jornadas
        resultados: list[ResultadoJornada] = []
        for jornada in jornadas:
            resultado = await _executar_jornada(
                jornada=jornada,
                consultant_fn=self._consultant_fn,
                evaluator_fn=self._evaluator_fn,
            )
            resultados.append(resultado)
            status_str = "APROVADA" if resultado.aprovada else "REPROVADA"
            logger.info(
                "[qa_journeys] Jornada '%s': %s",
                jornada.id,
                status_str,
            )
            if not resultado.aprovada:
                logger.warning(
                    "[qa_journeys] RESPOSTA CONSULTOR para '%s':\n%s",
                    jornada.id,
                    resultado.resposta_consultor[:800],
                )
                for c in resultado.criterios:
                    if c.severidade in (SEVERIDADE_CRITICO, SEVERIDADE_IMPORTANTE) and not c.passou:
                        logger.warning(
                            "[qa_journeys]   ✗ [%s] %s",
                            c.severidade,
                            c.descricao,
                        )

        metricas = _calcular_metricas(resultados)

        # Monta relatório completo
        relatorio = {
            "client_id": client_id,
            **metricas,
            "jornadas": [r.to_dict() for r in resultados],
            "jornadas_reprovadas_detalhes": [
                r.to_dict() for r in resultados if not r.aprovada
            ],
        }

        # Decisão de deploy
        aprovado = metricas["aprovado_por_score"] and not metricas["tem_critico_reprovado"]

        if not aprovado:
            motivo = []
            if not metricas["aprovado_por_score"]:
                motivo.append(
                    f"Score {metricas['score_percentual']}% abaixo do threshold {THRESHOLD_APROVACAO}%"
                )
            if metricas["tem_critico_reprovado"]:
                ids = ", ".join(metricas["jornadas_criticas_reprovadas"])
                motivo.append(f"Critérios críticos reprovados nas jornadas: {ids}")

            relatorio["motivo_bloqueio"] = " | ".join(motivo)
            logger.warning(
                "[qa_journeys] QA REPROVADO para '%s': %s",
                client_id,
                relatorio["motivo_bloqueio"],
            )
            return "blocked", relatorio

        logger.info(
            "[qa_journeys] QA APROVADO para '%s' — score %.1f%%.",
            client_id,
            metricas["score_percentual"],
        )
        return "done", relatorio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jornada_from_dict(d: dict) -> Jornada:
    """
    Converte um dicionário de onboarding em uma Jornada.

    Formato esperado:
    {
      "id": "j_custom_01",
      "nome": "Lead pergunta sobre pet-friendly",
      "mensagens": [{"role": "user", "content": "..."}],
      "criterios": [
        {"descricao": "...", "severidade": "critico", "sugestao": "..."}
      ]
    }
    """
    criterios = [
        Criterio(
            descricao=c["descricao"],
            severidade=c.get("severidade", SEVERIDADE_IMPORTANTE),
            sugestao_correcao=c.get("sugestao", ""),
        )
        for c in d.get("criterios", [])
    ]
    return Jornada(
        id=d["id"],
        nome=d["nome"],
        mensagens_simuladas=d.get("mensagens", []),
        criterios=criterios,
    )
