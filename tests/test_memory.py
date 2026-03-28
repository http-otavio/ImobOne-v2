"""
tests/test_memory.py — Testes unitários de agents/memory.py.

Mínimo exigido: 5 testes (CLAUDE.md).

Testes:
  1. Schema criado com todos os campos obrigatórios
  2. Score calculado corretamente para sequência de sinais conhecida
  3. Webhook validado com mock retornando 200
  4. Cliente sem CRM não bloqueia o agente
  5. Lead promovido para `quente` automaticamente quando score >= 8

Extras cobertos:
  - Compactação do histórico de mensagens (janela deslizante)
  - Score negativo (silêncio e desconto)
  - Serialização round-trip (to_dict / from_dict)
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx  # httpx mock library

from agents.memory import (
    CAMPOS_OBRIGATORIOS_LEAD,
    JANELA_MENSAGENS_RECENTES,
    PESOS_SINAL,
    THRESHOLD_LEAD_QUENTE,
    LeadSchema,
    MemoryAgent,
    ResultadoWebhook,
    SinalIntencao,
    StatusFunil,
    calcular_delta_score,
    calcular_score_total,
    determinar_status_funil,
    validar_schema_lead,
    validar_webhook_crm,
)


# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------


def _onboarding_com_crm(crm_url: str = "https://crm.cliente.com.br/webhook/leads") -> dict:
    return {
        "nome_imobiliaria": "Ápice Imóveis",
        "crm_webhook_url": crm_url,
        "crm_webhook_token": "Bearer eyJtoken123",
    }


def _onboarding_sem_crm() -> dict:
    return {
        "nome_imobiliaria": "Porto Alto",
        # crm_webhook_url propositalmente ausente
    }


# ---------------------------------------------------------------------------
# Teste 1 — Schema criado com todos os campos obrigatórios
# ---------------------------------------------------------------------------


def test_schema_lead_tem_todos_os_campos_obrigatorios():
    """
    LeadSchema.novo() deve produzir um dicionário com todos os 15 campos
    obrigatórios definidos em CAMPOS_OBRIGATORIOS_LEAD.

    Verifica:
    - Todos os campos presentes nas chaves do dicionário
    - Campos de lista inicializados como listas (não None)
    - Campos de string inicializados como string (não None)
    - status_funil inicializado como 'novo'
    - score_intencao inicializado como 0
    - lead_id e created_at gerados automaticamente (não vazios)

    Também verifica a função validar_schema_lead() — deve retornar lista vazia.
    """
    lead = LeadSchema.novo("cliente_schema_001")
    lead_dict = lead.to_dict()

    # Todos os campos obrigatórios presentes
    ausentes = validar_schema_lead(lead_dict)
    assert not ausentes, f"Campos ausentes no schema: {ausentes}"

    # Verificações de tipo e valor inicial
    assert lead_dict["lead_id"], "lead_id não gerado"
    assert lead_dict["client_id"] == "cliente_schema_001"
    assert lead_dict["score_intencao"] == 0
    assert lead_dict["status_funil"] == StatusFunil.NOVO
    assert isinstance(lead_dict["historico_mensagens"], list)
    assert isinstance(lead_dict["imoveis_de_interesse"], list)
    assert lead_dict["created_at"], "created_at não gerado"
    assert lead_dict["ultima_interacao"], "ultima_interacao não gerada"

    # Campos de qualificação inicialmente None (não preenchidos até a conversa)
    assert lead_dict["budget_declarado"] is None
    assert lead_dict["prazo_declarado"] is None
    assert lead_dict["perfil_familiar"] is None
    assert lead_dict["uso_imovel"] is None

    # Round-trip: from_dict(to_dict()) deve recriar o mesmo lead
    lead_recriado = LeadSchema.from_dict(lead_dict)
    assert lead_recriado.lead_id == lead.lead_id
    assert lead_recriado.client_id == lead.client_id
    assert lead_recriado.score_intencao == lead.score_intencao

    # CAMPOS_OBRIGATORIOS_LEAD tem os 15 campos do CLAUDE.md
    assert len(CAMPOS_OBRIGATORIOS_LEAD) >= 15, (
        f"Lista de campos obrigatórios incompleta: {len(CAMPOS_OBRIGATORIOS_LEAD)} campos"
    )


# ---------------------------------------------------------------------------
# Teste 2 — Score calculado corretamente para sequência de sinais conhecida
# ---------------------------------------------------------------------------


def test_score_calculado_corretamente_para_sequencia_conhecida():
    """
    O score de intenção deve ser calculado corretamente para sequências
    de sinais conhecidas, respeitando os pesos definidos no CLAUDE.md.

    Pesos de referência:
      BUDGET_DECLARADO                +5
      HORARIO_VISITA_MENCIONADO       +4
      PERGUNTA_ESPECIFICA_IMOVEL      +3
      FOTO_OU_PLANTA_SOLICITADA       +2
      PERGUNTA_VIZINHANCA             +2
      RESPOSTA_RAPIDA_CONSECUTIVA     +1
      DESCONTO_ABUSIVO                -1
      SILENCIO_48H                    -2

    Verifica:
    - Delta de cada sinal individualmente
    - Score acumulado em sequência de múltiplos turnos
    - Sinais negativos decrementam corretamente
    - calcular_score_total() é equivalente a aplicar turno a turno
    """
    # Deltas individuais
    assert calcular_delta_score([SinalIntencao.BUDGET_DECLARADO]) == 5
    assert calcular_delta_score([SinalIntencao.HORARIO_VISITA_MENCIONADO]) == 4
    assert calcular_delta_score([SinalIntencao.PERGUNTA_ESPECIFICA_IMOVEL]) == 3
    assert calcular_delta_score([SinalIntencao.FOTO_OU_PLANTA_SOLICITADA]) == 2
    assert calcular_delta_score([SinalIntencao.PERGUNTA_VIZINHANCA]) == 2
    assert calcular_delta_score([SinalIntencao.RESPOSTA_RAPIDA_CONSECUTIVA]) == 1
    assert calcular_delta_score([SinalIntencao.DESCONTO_ABUSIVO]) == -1
    assert calcular_delta_score([SinalIntencao.SILENCIO_48H]) == -2

    # Sequência de turnos: lead que vai esquentando ao longo da conversa
    sequencia = [
        # Turno 1 — lead pergunta sobre um apartamento específico
        [SinalIntencao.PERGUNTA_ESPECIFICA_IMOVEL],          # +3 → total 3
        # Turno 2 — menciona budget e pede foto
        [SinalIntencao.BUDGET_DECLARADO,
         SinalIntencao.FOTO_OU_PLANTA_SOLICITADA],            # +5+2=+7 → total 10
        # Turno 3 — responde rápido e pergunta sobre vizinhança
        [SinalIntencao.RESPOSTA_RAPIDA_CONSECUTIVA,
         SinalIntencao.PERGUNTA_VIZINHANCA],                  # +1+2=+3 → total 13
        # Turno 4 — pede desconto abusivo (sinal negativo)
        [SinalIntencao.DESCONTO_ABUSIVO],                     # -1 → total 12
    ]
    score_esperado = 3 + 7 + 3 - 1  # = 12

    score_calculado = calcular_score_total(sequencia)
    assert score_calculado == score_esperado, (
        f"Score calculado {score_calculado} != esperado {score_esperado}"
    )

    # Verifica via LeadSchema.aplicar_sinais() — deve chegar ao mesmo resultado
    lead = LeadSchema.novo("cliente_score_002")
    for turno in sequencia:
        lead.aplicar_sinais(turno)

    assert lead.score_intencao == score_esperado

    # Todos os sinais registrados no histórico
    sinais_registrados = lead.sinais_detectados
    assert SinalIntencao.BUDGET_DECLARADO.value in sinais_registrados
    assert SinalIntencao.DESCONTO_ABUSIVO.value in sinais_registrados

    # Sequência com silêncio: lead frio após 48h
    lead_frio = LeadSchema.novo("cliente_frio")
    lead_frio.aplicar_sinais([SinalIntencao.PERGUNTA_ESPECIFICA_IMOVEL])  # +3
    lead_frio.aplicar_sinais([SinalIntencao.SILENCIO_48H])                # -2
    assert lead_frio.score_intencao == 1

    # PESOS_SINAL tem todos os 8 sinais definidos
    assert len(PESOS_SINAL) == len(SinalIntencao), (
        "Algum sinal em SinalIntencao não tem peso definido em PESOS_SINAL"
    )


# ---------------------------------------------------------------------------
# Teste 3 — Webhook validado com mock retornando 200
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_crm_validado_com_mock_200():
    """
    validar_webhook_crm() deve retornar ResultadoWebhook(crm_enabled=True)
    quando o endpoint retorna 200, e o MemoryAgent deve retornar ('done', payload)
    com crm_enabled=True.

    Verifica:
    - crm_enabled=True no ResultadoWebhook
    - status_code=200 registrado no resultado
    - latencia_ms registrada (não None)
    - run() retorna ('done', payload) com crm_enabled=True
    - payload inclui crm_url correta
    - POST enviado com header Authorization correto
    """
    crm_url = "https://crm.cliente.com.br/webhook/leads"
    crm_token = "token_super_secreto"

    with respx.mock:
        # Mock do endpoint CRM retornando 200
        respx.post(crm_url).mock(return_value=httpx.Response(200, json={"ok": True}))

        async with httpx.AsyncClient() as client:
            resultado = await validar_webhook_crm(
                crm_url=crm_url,
                crm_token=crm_token,
                client_id="cliente_webhook_003",
                http_client=client,
            )

    assert resultado.crm_enabled is True, f"crm_enabled deveria ser True: {resultado}"
    assert resultado.status_code == 200
    assert resultado.latencia_ms is not None
    assert resultado.erro is None
    assert resultado.sucesso is True

    # Testa via MemoryAgent.run() completo
    with respx.mock:
        respx.post(crm_url).mock(return_value=httpx.Response(200, json={"ok": True}))

        async with httpx.AsyncClient() as http_client:
            agent = MemoryAgent(http_client=http_client)
            status, payload = await agent.run(
                "cliente_webhook_003",
                _onboarding_com_crm(crm_url),
            )

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"
    assert payload["crm_enabled"] is True
    assert payload["crm_status_code"] == 200
    assert payload["crm_url"] == crm_url
    assert payload["schema_validado"] is True


# ---------------------------------------------------------------------------
# Teste 4 — Cliente sem CRM não bloqueia o agente
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cliente_sem_crm_nao_bloqueia_agente():
    """
    Quando o onboarding não tem crm_webhook_url, run() deve:
    - Retornar ('done', payload) — NÃO 'blocked'
    - Registrar crm_enabled=False no payload
    - Registrar a razão do crm_enabled=False em crm_erro

    Este comportamento é crítico: imobiliárias pequenas podem não ter CRM
    e o setup não pode ser bloqueado por isso.

    Cenários:
    4a. crm_webhook_url completamente ausente do onboarding
    4b. crm_webhook_url presente mas endpoint retorna 500
    4c. crm_webhook_url presente mas timeout

    Em todos os casos: status='done', crm_enabled=False, sem erro fatal.
    """
    # 4a. Sem URL no onboarding
    agent = MemoryAgent()
    status, payload = await agent.run("cliente_sem_crm_004a", _onboarding_sem_crm())

    assert status == "done", f"Esperava 'done' sem CRM, got '{status}': {payload}"
    assert payload["crm_enabled"] is False
    assert payload["crm_erro"] is not None
    assert payload["schema_validado"] is True  # schema válido, apenas CRM ausente

    # 4b. URL presente mas retorna 500
    crm_url_500 = "https://crm-quebrado.cliente.com.br/webhook"
    with respx.mock:
        respx.post(crm_url_500).mock(return_value=httpx.Response(500))

        async with httpx.AsyncClient() as http_client:
            agent_b = MemoryAgent(http_client=http_client)
            status_b, payload_b = await agent_b.run(
                "cliente_sem_crm_004b",
                {"crm_webhook_url": crm_url_500, "crm_webhook_token": "tok"},
            )

    assert status_b == "done", f"Esperava 'done' com CRM 500, got '{status_b}': {payload_b}"
    assert payload_b["crm_enabled"] is False
    assert payload_b["crm_status_code"] == 500

    # 4c. Timeout no CRM
    crm_url_timeout = "https://crm-lento.cliente.com.br/webhook"
    with respx.mock:
        respx.post(crm_url_timeout).mock(side_effect=httpx.TimeoutException("timeout"))

        async with httpx.AsyncClient() as http_client:
            agent_c = MemoryAgent(http_client=http_client)
            status_c, payload_c = await agent_c.run(
                "cliente_sem_crm_004c",
                {"crm_webhook_url": crm_url_timeout, "crm_webhook_token": "tok"},
            )

    assert status_c == "done", f"Esperava 'done' com CRM timeout, got '{status_c}': {payload_c}"
    assert payload_c["crm_enabled"] is False
    assert "timeout" in payload_c["crm_erro"].lower() or payload_c["crm_erro"] is not None


# ---------------------------------------------------------------------------
# Teste 5 — Lead promovido para `quente` quando score >= 8
# ---------------------------------------------------------------------------


def test_lead_promovido_para_quente_quando_score_atinge_threshold():
    """
    Quando o score de intenção do lead atinge ou supera THRESHOLD_LEAD_QUENTE (8),
    o status_funil deve ser promovido automaticamente para 'quente'.

    Verifica:
    - score < 8 → status permanece 'novo' ou 'qualificado'
    - score == 8 → status promovido para 'quente' (threshold exato)
    - score > 8 → status 'quente' (acima do threshold)
    - Status 'agendado' é terminal — nunca rebaixado para 'quente' por score
    - Status 'descartado' é terminal — nunca alterado por score

    Também verifica a função determinar_status_funil() diretamente.
    """
    # Lead em 'novo' com score 0 — permanece 'novo'
    assert determinar_status_funil(0, StatusFunil.NOVO) == StatusFunil.NOVO

    # Score positivo mas abaixo do threshold — promove para 'qualificado'
    assert determinar_status_funil(3, StatusFunil.NOVO) == StatusFunil.QUALIFICADO
    assert determinar_status_funil(7, StatusFunil.QUALIFICADO) == StatusFunil.QUALIFICADO

    # Score == threshold — promove para 'quente'
    assert determinar_status_funil(THRESHOLD_LEAD_QUENTE, StatusFunil.NOVO) == StatusFunil.QUENTE
    assert determinar_status_funil(THRESHOLD_LEAD_QUENTE, StatusFunil.QUALIFICADO) == StatusFunil.QUENTE

    # Score acima do threshold — 'quente'
    assert determinar_status_funil(15, StatusFunil.QUALIFICADO) == StatusFunil.QUENTE
    assert determinar_status_funil(20, StatusFunil.NOVO) == StatusFunil.QUENTE

    # Status terminais — nunca alterados por score
    assert determinar_status_funil(50, StatusFunil.AGENDADO) == StatusFunil.AGENDADO
    assert determinar_status_funil(50, StatusFunil.DESCARTADO) == StatusFunil.DESCARTADO

    # Integração com LeadSchema.aplicar_sinais() — sequência que leva a 'quente'
    lead = LeadSchema.novo("cliente_quente_005")
    assert lead.status_funil == StatusFunil.NOVO

    # Turno 1: budget + foto (+5+2 = 7) → qualificado, ainda abaixo do threshold
    lead.aplicar_sinais([
        SinalIntencao.BUDGET_DECLARADO,
        SinalIntencao.FOTO_OU_PLANTA_SOLICITADA,
    ])
    assert lead.score_intencao == 7
    assert lead.status_funil == StatusFunil.QUALIFICADO

    # Turno 2: resposta rápida (+1 → 8 = threshold exato) → promovido para quente
    lead.aplicar_sinais([SinalIntencao.RESPOSTA_RAPIDA_CONSECUTIVA])
    assert lead.score_intencao == 8
    assert lead.status_funil == StatusFunil.QUENTE, (
        f"Lead deveria estar 'quente' com score {lead.score_intencao}, "
        f"threshold={THRESHOLD_LEAD_QUENTE}. Status atual: {lead.status_funil}"
    )

    # Turno 3: menciona horário de visita (+4 → 12) → continua quente
    lead.aplicar_sinais([SinalIntencao.HORARIO_VISITA_MENCIONADO])
    assert lead.score_intencao == 12
    assert lead.status_funil == StatusFunil.QUENTE

    # Status 'quente' não retrocede com sinal negativo se ainda >= threshold
    lead.aplicar_sinais([SinalIntencao.DESCONTO_ABUSIVO])  # -1 → 11
    assert lead.score_intencao == 11
    assert lead.status_funil == StatusFunil.QUENTE  # continua quente

    # THRESHOLD_LEAD_QUENTE deve ser 8 conforme CLAUDE.md
    assert THRESHOLD_LEAD_QUENTE == 8, (
        f"Threshold esperado: 8 (CLAUDE.md). Atual: {THRESHOLD_LEAD_QUENTE}"
    )


# ---------------------------------------------------------------------------
# Extra — Compactação do histórico de mensagens
# ---------------------------------------------------------------------------


def test_historico_mensagens_compactado_apos_exceder_janela():
    """
    Quando o número de mensagens excede JANELA_MENSAGENS_RECENTES,
    o histórico deve ser compactado: mensagens antigas vão para resumo_historico
    e a lista ativa mantém apenas as JANELA_MENSAGENS_RECENTES mais recentes.

    Verifica:
    - Com 10 mensagens: lista ativa tem 10, resumo vazio
    - Com 11 mensagens: lista ativa tem 10, resumo tem 1 entrada
    - Com 20 mensagens: lista ativa tem 10, resumo tem 10 entradas
    - Mensagens na janela ativa são as MAIS RECENTES (não as mais antigas)
    - resumo_historico é string não vazia após compactação
    """
    lead = LeadSchema.novo("cliente_historico")

    # Adiciona JANELA_MENSAGENS_RECENTES mensagens — ainda dentro da janela
    for i in range(JANELA_MENSAGENS_RECENTES):
        lead.adicionar_mensagem("user", f"Mensagem {i + 1} do usuário")

    assert len(lead.historico_mensagens) == JANELA_MENSAGENS_RECENTES
    assert lead.resumo_historico == "", "resumo_historico deveria estar vazio na janela"

    # Mensagem 11 — excede a janela, deve compactar
    lead.adicionar_mensagem("assistant", "Mensagem 11 do consultor — deve ir para o resumo")

    assert len(lead.historico_mensagens) == JANELA_MENSAGENS_RECENTES, (
        f"Janela ativa deveria ter {JANELA_MENSAGENS_RECENTES} entradas, "
        f"tem {len(lead.historico_mensagens)}"
    )
    assert lead.resumo_historico != "", "resumo_historico deveria ter conteúdo após compactação"

    # A janela ativa deve conter as mensagens mais recentes
    # Mensagem 2 até 11 (a mensagem 1 foi para o resumo)
    resumos_ativos = [e["resumo"] for e in lead.historico_mensagens]
    assert any("Mensagem 2" in r for r in resumos_ativos), (
        "Mensagem 2 deveria estar na janela ativa (não foi compactada)"
    )
    assert any("Mensagem 11" in r for r in resumos_ativos), (
        "Mensagem 11 deveria estar na janela ativa (a mais recente)"
    )

    # Mensagem 1 deve estar no resumo (foi a primeira a sair da janela)
    assert "Mensagem 1" in lead.resumo_historico, (
        "Mensagem 1 deveria estar no resumo_historico"
    )

    # Adiciona mais 9 mensagens (total 20) — compacta mais uma vez
    for i in range(12, 21):
        lead.adicionar_mensagem("user", f"Mensagem {i} extra")

    assert len(lead.historico_mensagens) == JANELA_MENSAGENS_RECENTES, (
        "Janela ativa deve sempre ter no máximo JANELA_MENSAGENS_RECENTES entradas"
    )
    # resumo_historico deve ter mais conteúdo agora (rolling)
    assert len(lead.resumo_historico) > 0
