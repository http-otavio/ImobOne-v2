"""
tests/test_dev_persona.py — Testes unitários de agents/dev_persona.py.

Mínimo exigido: 3 testes (CLAUDE.md).

Testes:
  1. YAML gerado tem todos os campos obrigatórios
  2. Palavras proibidas do briefing aparecem na lista
  3. Cliente sem exemplos de saudação recebe fallback genérico premium, não erro
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from agents.dev_persona import (
    CAMPOS_OBRIGATORIOS,
    SAUDACOES_PREMIUM_FALLBACK,
    VOICE_ID_FALLBACK,
    DevPersonaAgent,
)


# ---------------------------------------------------------------------------
# Helpers e fixtures
# ---------------------------------------------------------------------------


def _make_haiku_response(persona_dict: dict) -> MagicMock:
    """
    Simula anthropic.messages.Message com .content[0].text contendo YAML.
    """
    content_block = MagicMock()
    content_block.text = yaml.dump(
        persona_dict,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        indent=2,
    )
    message = MagicMock()
    message.content = [content_block]
    return message


def _make_anthropic_client(persona_dict: dict) -> MagicMock:
    """Cliente Anthropic mockado retornando persona_dict como YAML."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_make_haiku_response(persona_dict))
    return client


def _persona_completa() -> dict:
    """Persona válida com todos os campos obrigatórios."""
    return {
        "nome_consultor": "Julia",
        "voice_id": "abc123ElevenLabsVoiceId",
        "tom_descritivo": (
            "Tom sofisticado e preciso, voltado ao cliente de alto padrão. "
            "Nunca apressado, nunca genérico. Discreção acima de tudo."
        ),
        "palavras_proibidas": [
            "barato",
            "oportunidade imperdível",
            "não perca",
            "aproveite",
            "correndo",
            "promoção",
        ],
        "frases_proibidas": [
            "Essa é a melhor oferta do mercado!",
            "Corre que vai acabar!",
            "Isso vai acabar rápido!",
        ],
        "exemplos_saudacao": [
            "Boa tarde! Sou Julia, da Porto Alto. Como posso ajudá-lo?",
            "Bom dia! Que prazer ter você aqui. Estou à sua disposição.",
            "Boa noite! Vi que chegou pelo nosso portfólio de lançamentos.",
        ],
        "regras_especificas": (
            "Nunca citar preço por metro quadrado sem informar condomínio e IPTU. "
            "Sempre perguntar sobre andar preferencial antes de recomendar."
        ),
    }


def _onboarding_com_palavras_proibidas() -> dict:
    """Onboarding com palavras proibidas específicas do cliente."""
    return {
        "nome_imobiliaria": "Porto Alto",
        "nome_consultor": "Julia",
        "cidade_atuacao": "Rio de Janeiro",
        "tipo_atuacao": "lançamentos de alto padrão",
        "briefing_tom": "Exclusividade, sofisticação, atenção ao detalhe.",
        "palavras_proibidas": [
            "saldão",
            "liquidação",
            "tá barato",
            "tiro de preço",
        ],
        "frases_proibidas": [
            "Oferta por tempo limitado!",
        ],
        "exemplos_saudacao": [
            "Boa tarde! Bem-vindo à Porto Alto. Como posso apresentar nosso portfólio?",
        ],
    }


# ---------------------------------------------------------------------------
# Teste 1 — YAML gerado tem todos os campos obrigatórios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_gerado_tem_todos_os_campos_obrigatorios(tmp_path):
    """
    Quando o Haiku retorna uma persona com todos os campos obrigatórios,
    run() deve retornar ("done", payload) e o arquivo persona.yaml deve
    conter todos os campos de CAMPOS_OBRIGATORIOS.

    Verifica:
    - status retornado é "done"
    - arquivo persona.yaml criado no caminho correto
    - todos os 7 campos obrigatórios presentes e não-vazios
    - nome_consultor e voice_id refletidos no payload de retorno
    """
    persona_dict = _persona_completa()
    anthropic_client = _make_anthropic_client(persona_dict)

    agent = DevPersonaAgent(
        anthropic_client=anthropic_client,
        output_base_dir=tmp_path / "prompts" / "clients",
    )

    onboarding = _onboarding_com_palavras_proibidas()
    status, payload = await agent.run("cliente_yaml_001", onboarding)

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"
    assert "persona_path" in payload

    # Arquivo criado
    yaml_path = Path(payload["persona_path"])
    assert yaml_path.exists(), f"persona.yaml não criado em {yaml_path}"

    # Parse do YAML gerado (ignorando linhas de comentário do cabeçalho)
    conteudo = yaml_path.read_text(encoding="utf-8")
    linhas_sem_comentario = "\n".join(
        l for l in conteudo.splitlines() if not l.strip().startswith("#")
    )
    persona_lida: dict = yaml.safe_load(linhas_sem_comentario)

    assert isinstance(persona_lida, dict), "persona.yaml não é um dicionário válido"

    # Todos os campos obrigatórios presentes e não-vazios
    for campo in CAMPOS_OBRIGATORIOS:
        valor = persona_lida.get(campo)
        assert valor is not None, f"Campo obrigatório ausente: {campo}"
        assert valor != "" and valor != [] and valor != {}, f"Campo obrigatório vazio: {campo}"

    # Payload reflete os campos principais
    assert payload["nome_consultor"] == persona_dict["nome_consultor"]
    assert payload["voice_id"] == persona_dict["voice_id"]
    assert payload["palavras_proibidas_count"] > 0
    assert payload["exemplos_saudacao_count"] > 0


# ---------------------------------------------------------------------------
# Teste 2 — Palavras proibidas do briefing aparecem na lista
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_palavras_proibidas_do_briefing_aparecem_na_lista(tmp_path):
    """
    Palavras proibidas específicas do briefing do cliente devem ser incluídas
    no lista de palavras_proibidas do persona.yaml, junto com as palavras-base
    obrigatórias (barato, oportunidade imperdível, etc.).

    Verifica:
    - Palavras do briefing ("saldão", "liquidação", etc.) presentes no yaml
    - Palavras-base obrigatórias ("barato", "não perca", etc.) também presentes
    - Lista de palavras_proibidas não vazia e com mínimo 5 itens
    """
    # Haiku retorna persona com palavras-base mas SEM as palavras do briefing
    # Os fallbacks de _aplicar_fallbacks() devem incluir as do onboarding
    persona_base = _persona_completa()
    persona_base["palavras_proibidas"] = [
        "barato",
        "oportunidade imperdível",
        "não perca",
        "aproveite",
        "correndo",
    ]
    anthropic_client = _make_anthropic_client(persona_base)

    agent = DevPersonaAgent(
        anthropic_client=anthropic_client,
        output_base_dir=tmp_path / "prompts" / "clients",
    )

    onboarding = _onboarding_com_palavras_proibidas()
    status, payload = await agent.run("cliente_proibidas_002", onboarding)

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"

    yaml_path = Path(payload["persona_path"])
    conteudo = yaml_path.read_text(encoding="utf-8")
    linhas = "\n".join(l for l in conteudo.splitlines() if not l.strip().startswith("#"))
    persona_lida: dict = yaml.safe_load(linhas)

    palavras: list = persona_lida.get("palavras_proibidas", [])

    # Palavras do briefing do cliente devem estar presentes
    palavras_esperadas_do_briefing = ["saldão", "liquidação", "tá barato", "tiro de preço"]
    for palavra in palavras_esperadas_do_briefing:
        assert palavra in palavras, (
            f"Palavra proibida do briefing '{palavra}' ausente na lista. "
            f"Lista atual: {palavras}"
        )

    # Palavras-base obrigatórias também presentes
    palavras_base = ["barato", "não perca"]
    for palavra in palavras_base:
        assert palavra in palavras, (
            f"Palavra proibida base '{palavra}' ausente na lista. "
            f"Lista atual: {palavras}"
        )

    # Mínimo de 5 palavras proibidas
    assert len(palavras) >= 5, f"Lista de palavras proibidas muito curta: {palavras}"

    # Payload reflete a contagem correta
    assert payload["palavras_proibidas_count"] == len(palavras)


# ---------------------------------------------------------------------------
# Teste 3 — Sem exemplos de saudação → fallback premium, não erro
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sem_exemplos_saudacao_usa_fallback_premium_nao_erro(tmp_path):
    """
    Quando o cliente não fornece exemplos de saudação no briefing E o Haiku
    retorna persona sem exemplos_saudacao, o agente deve:
    - Usar SAUDACOES_PREMIUM_FALLBACK (nunca retornar "blocked")
    - Retornar status "done" com a persona completa
    - As saudações do fallback são sofisticadas (nunca "Olá" isolado)

    Este teste valida a robustez do agente para onboardings incompletos —
    clientes que pulam campos de saudação no formulário não devem bloquear o setup.
    """
    # Haiku retorna persona SEM exemplos_saudacao
    persona_sem_saudacao = _persona_completa()
    persona_sem_saudacao["exemplos_saudacao"] = []  # vazio

    anthropic_client = _make_anthropic_client(persona_sem_saudacao)

    agent = DevPersonaAgent(
        anthropic_client=anthropic_client,
        output_base_dir=tmp_path / "prompts" / "clients",
    )

    # Onboarding também sem exemplos_saudacao
    onboarding_sem_saudacao: dict = {
        "nome_imobiliaria": "Ápice Imóveis",
        "nome_consultor": "Marco",
        "cidade_atuacao": "Curitiba",
        "tipo_atuacao": "venda de alto padrão",
        "briefing_tom": "Profissional, objetivo e sofisticado.",
        # exemplos_saudacao propositalmente ausente
    }

    status, payload = await agent.run("cliente_sem_saudacao_003", onboarding_sem_saudacao)

    assert status == "done", f"Esperava 'done' com fallback, got '{status}': {payload}"
    assert payload["exemplos_saudacao_count"] > 0, "Nenhum exemplo de saudação no payload"

    # Verifica o yaml gerado
    yaml_path = Path(payload["persona_path"])
    conteudo = yaml_path.read_text(encoding="utf-8")
    linhas = "\n".join(l for l in conteudo.splitlines() if not l.strip().startswith("#"))
    persona_lida: dict = yaml.safe_load(linhas)

    saudacoes: list = persona_lida.get("exemplos_saudacao", [])
    assert len(saudacoes) > 0, "exemplos_saudacao vazio após fallback"

    # Fallback nunca usa "Olá" isolado — verificação de qualidade mínima
    for saudacao in saudacoes:
        saudacao_lower = saudacao.lower().strip()
        assert not (saudacao_lower == "olá" or saudacao_lower == "ola"), (
            f"Saudação de baixa qualidade no fallback: '{saudacao}'"
        )
        # Deve ser uma saudação temporal ou conter contexto
        tem_saudacao_temporal = any(
            p in saudacao_lower
            for p in ["bom dia", "boa tarde", "boa noite", "bem-vindo", "bem vindo"]
        )
        assert tem_saudacao_temporal, (
            f"Saudação sem horário ou contexto: '{saudacao}'. "
            f"Fallback premium deve usar 'Bom dia', 'Boa tarde' ou 'Boa noite'."
        )

    # As saudações do fallback devem ser as de SAUDACOES_PREMIUM_FALLBACK
    # (já que o onboarding também não tem exemplos)
    for saudacao_fallback in SAUDACOES_PREMIUM_FALLBACK:
        assert saudacao_fallback in saudacoes, (
            f"Saudação premium '{saudacao_fallback}' ausente no fallback. "
            f"Saudações geradas: {saudacoes}"
        )
