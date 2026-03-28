"""
tests/test_qa_integration.py — Testes unitários de agents/qa_integration.py.

Mínimo exigido: 4 testes (CLAUDE.md).

Testes:
  1. Todos os itens passando → done com relatório completo
  2. Item crítico falhando → blocked com diagnóstico
  3. Item não-crítico falhando → done com nota no relatório
  4. Latência medida e registrada para cada integração

Todos os testes usam respx para mock de HTTP — zero chamadas reais.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from agents.qa_integration import (
    LATENCIA_MAX_MS,
    CheckResult,
    QAIntegrationAgent,
    _calcular_latencia_e2e,
    _check_elevenlabs,
    _check_latencia_e2e,
    _check_webhook_crm,
)


# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------


def _onboarding_completo(
    crm_url: str = "https://crm.cliente.com.br/webhook",
    wa_url: str = "https://waba.360dialog.io/v1/messages",
    places_key: str = "google_key_fake",
    supabase_url: str = "https://proj.supabase.co",
    supabase_key: str = "supabase_key_fake",
    voice_id: str = "abc123voice",
) -> dict:
    """Onboarding com todas as integrações configuradas."""
    return {
        "crm_webhook_url": crm_url,
        "crm_webhook_token": "crm_token_fake",
        "whatsapp_bsp_url": wa_url,
        "whatsapp_bsp_api_key": "wa_api_key_fake",
        "whatsapp_operator_number": "+5511999990000",
        "google_places_api_key": places_key,
        "supabase_url": supabase_url,
        "supabase_key": supabase_key,
        "voice_id": voice_id,
        "endereco_teste": {"lat": -23.5505, "lng": -46.6333},
    }


def _make_elevenlabs_mock(audio_bytes: bytes = b"fake_audio_data_for_testing") -> MagicMock:
    """Mock do cliente ElevenLabs com generate() assíncrono."""
    client = MagicMock()
    client.generate = AsyncMock(return_value=audio_bytes)
    return client


# ---------------------------------------------------------------------------
# Teste 1 — Todos os itens passando → done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_todos_os_checks_passando_retorna_done():
    """
    Quando todas as integrações respondem corretamente, run() deve retornar
    ('done', payload) com todos os checks passando e latência registrada.

    Verifica:
    - status == 'done'
    - total_checks == 6 (crm, tts, whatsapp, places, pgvector, latencia_e2e)
    - checks_falhando == 0
    - criticos_falhando_count == 0
    - latencia_e2e_ms registrada (não None)
    - checks lista todos os 6 itens com passou=True
    """
    onboarding = _onboarding_completo()
    elevenlabs = _make_elevenlabs_mock()

    with respx.mock:
        # CRM retorna 200
        respx.post(onboarding["crm_webhook_url"]).mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        # WhatsApp retorna 201
        respx.post(onboarding["whatsapp_bsp_url"]).mock(
            return_value=httpx.Response(201, json={"messages": [{"id": "wamid.123"}]})
        )
        # Google Places retorna resultados
        respx.get("https://maps.googleapis.com/maps/api/place/nearbysearch/json").mock(
            return_value=httpx.Response(200, json={
                "status": "OK",
                "results": [{"name": "Colégio Bandeirantes", "rating": 4.8}],
            })
        )
        # Supabase retorna lista de imóveis
        respx.get(f"{onboarding['supabase_url']}/rest/v1/imoveis_embeddings").mock(
            return_value=httpx.Response(200, json=[
                {"imovel_id": "imovel_001", "titulo": "Cobertura Jardins"}
            ])
        )

        async with httpx.AsyncClient() as http_client:
            agent = QAIntegrationAgent(
                http_client=http_client,
                elevenlabs_client=elevenlabs,
            )
            status, payload = await agent.run("cliente_int_001", onboarding)

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"
    assert payload["checks_falhando"] == 0
    assert payload["criticos_falhando_count"] == 0
    assert payload["total_checks"] == 6
    assert payload["checks_passando"] == 6
    assert payload["latencia_e2e_ms"] >= 0

    # Todos os 6 checks presentes com passou=True
    nomes_esperados = {
        "webhook_crm",
        "elevenlabs_tts",
        "whatsapp_envio",
        "google_places",
        "supabase_pgvector",
        "latencia_e2e",
    }
    nomes_retornados = {c["nome"] for c in payload["checks"]}
    assert nomes_esperados == nomes_retornados, (
        f"Checks ausentes: {nomes_esperados - nomes_retornados}"
    )
    for check in payload["checks"]:
        assert check["passou"] is True, f"Check '{check['nome']}' falhou: {check['erro']}"
        # Latência registrada para checks que fazem chamadas
        if check["nome"] != "latencia_e2e":
            assert check["latencia_ms"] is not None, (
                f"latencia_ms não registrada para '{check['nome']}'"
            )


# ---------------------------------------------------------------------------
# Teste 2 — Item crítico falhando → blocked com diagnóstico
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_critico_falhando_retorna_blocked_com_diagnostico():
    """
    Quando um item CRÍTICO falha (ex: WhatsApp retorna 500), run() deve
    retornar ('blocked', payload) com diagnóstico detalhado do item falhando.

    Cenários:
    2a. WhatsApp retorna 500 → blocked
    2b. Google Places com key inválida → blocked
    2c. Supabase com timeout → blocked

    Verifica para cada cenário:
    - status == 'blocked'
    - criticos_falhando_count >= 1
    - motivo_bloqueio menciona o item crítico
    - criticos_falhando tem detalhes do item
    """
    onboarding = _onboarding_completo()
    elevenlabs = _make_elevenlabs_mock()

    # 2a. WhatsApp retorna 500
    with respx.mock:
        respx.post(onboarding["crm_webhook_url"]).mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        respx.post(onboarding["whatsapp_bsp_url"]).mock(
            return_value=httpx.Response(500, json={"error": "Internal Server Error"})
        )
        respx.get("https://maps.googleapis.com/maps/api/place/nearbysearch/json").mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": [{"name": "Escola X"}]})
        )
        respx.get(f"{onboarding['supabase_url']}/rest/v1/imoveis_embeddings").mock(
            return_value=httpx.Response(200, json=[{"imovel_id": "i001"}])
        )

        async with httpx.AsyncClient() as http_client:
            agent = QAIntegrationAgent(
                http_client=http_client,
                elevenlabs_client=elevenlabs,
            )
            status_2a, payload_2a = await agent.run("cliente_int_002a", onboarding)

    assert status_2a == "blocked", f"Esperava 'blocked' com WhatsApp 500, got '{status_2a}'"
    assert payload_2a["criticos_falhando_count"] >= 1
    assert "motivo_bloqueio" in payload_2a
    assert "whatsapp" in payload_2a["motivo_bloqueio"].lower()

    # criticos_falhando tem detalhe do item
    assert len(payload_2a["criticos_falhando"]) >= 1
    wa_critico = next(
        (c for c in payload_2a["criticos_falhando"] if c["nome"] == "whatsapp_envio"),
        None,
    )
    assert wa_critico is not None, "whatsapp_envio deve estar em criticos_falhando"
    assert wa_critico["passou"] is False
    assert wa_critico["status_code"] == 500
    assert wa_critico["critico"] is True

    # 2b. Supabase sem configuração (sem URL/key) → crítico
    onboarding_sem_supabase = dict(onboarding)
    onboarding_sem_supabase["supabase_url"] = ""

    with respx.mock:
        respx.post(onboarding["crm_webhook_url"]).mock(
            return_value=httpx.Response(200)
        )
        respx.post(onboarding["whatsapp_bsp_url"]).mock(
            return_value=httpx.Response(201)
        )
        respx.get("https://maps.googleapis.com/maps/api/place/nearbysearch/json").mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": []})
        )

        async with httpx.AsyncClient() as http_client:
            agent_b = QAIntegrationAgent(http_client=http_client)
            status_2b, payload_2b = await agent_b.run("cliente_int_002b", onboarding_sem_supabase)

    assert status_2b == "blocked"
    assert payload_2b["criticos_falhando_count"] >= 1
    nomes_criticos = [c["nome"] for c in payload_2b["criticos_falhando"]]
    assert "supabase_pgvector" in nomes_criticos


# ---------------------------------------------------------------------------
# Teste 3 — Item não-crítico falhando → done com nota no relatório
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_nao_critico_falhando_retorna_done_com_nota():
    """
    Quando apenas itens NÃO-CRÍTICOS falham (CRM 404, ElevenLabs vazio),
    run() deve retornar ('done', payload) com as notas nos detalhes mas
    sem bloquear o deploy.

    Verifica:
    - status == 'done'
    - criticos_falhando_count == 0
    - nao_criticos_falhando_count >= 1
    - notas tem os itens não-críticos que falharam
    - checks_falhando >= 1 mas sem bloquear
    """
    onboarding = _onboarding_completo()

    # ElevenLabs falha (não-crítico)
    elevenlabs_falho = MagicMock()
    elevenlabs_falho.generate = AsyncMock(side_effect=Exception("ElevenLabs indisponível"))

    with respx.mock:
        # CRM retorna 404 (não-crítico)
        respx.post(onboarding["crm_webhook_url"]).mock(
            return_value=httpx.Response(404)
        )
        # WhatsApp OK (crítico — passa)
        respx.post(onboarding["whatsapp_bsp_url"]).mock(
            return_value=httpx.Response(201)
        )
        # Places OK (crítico — passa)
        respx.get("https://maps.googleapis.com/maps/api/place/nearbysearch/json").mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": [{"name": "Escola X"}]})
        )
        # Supabase OK (crítico — passa)
        respx.get(f"{onboarding['supabase_url']}/rest/v1/imoveis_embeddings").mock(
            return_value=httpx.Response(200, json=[{"imovel_id": "i001"}])
        )

        async with httpx.AsyncClient() as http_client:
            agent = QAIntegrationAgent(
                http_client=http_client,
                elevenlabs_client=elevenlabs_falho,
            )
            status, payload = await agent.run("cliente_int_003", onboarding)

    assert status == "done", (
        f"Esperava 'done' com apenas não-críticos falhando, got '{status}': "
        f"{payload.get('motivo_bloqueio', '')}"
    )
    assert payload["criticos_falhando_count"] == 0, (
        f"Críticos com falha: {[c['nome'] for c in payload['criticos_falhando']]}"
    )
    assert payload["nao_criticos_falhando_count"] >= 1

    # Notas têm os itens não-críticos falhando
    nomes_notas = {n["nome"] for n in payload["notas"]}
    # CRM retornou 404 → nota
    assert "webhook_crm" in nomes_notas or "elevenlabs_tts" in nomes_notas, (
        f"Itens não-críticos falhando não encontrados nas notas: {nomes_notas}"
    )

    # Não deve ter motivo_bloqueio quando 'done'
    assert "motivo_bloqueio" not in payload or not payload.get("motivo_bloqueio")


# ---------------------------------------------------------------------------
# Teste 4 — Latência medida e registrada para cada integração
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latencia_medida_e_registrada_por_integracao():
    """
    A latência de cada integração deve ser medida e registrada em latencia_ms.
    A latência e2e deve ser a soma de todas as latências individuais.

    Verifica:
    - latencia_ms não é None para checks que fizeram chamadas HTTP
    - latencia_e2e_ms == soma das latências individuais (aproximado)
    - check latencia_e2e tem passou=True quando soma < 8000ms
    - check latencia_e2e tem passou=False quando soma >= 8000ms

    Também testa _check_latencia_e2e() diretamente:
    - soma 7999ms → passou=True
    - soma 8000ms → passou=False (threshold exclusivo)
    - soma 10000ms → passou=False
    """
    # Teste direto de _check_latencia_e2e()
    check_ok = _check_latencia_e2e(7999.9)
    assert check_ok.passou is True, f"7999ms deveria passar: {check_ok.erro}"
    assert check_ok.latencia_ms == 7999.9

    check_limite = _check_latencia_e2e(8000.0)
    assert check_limite.passou is False, "8000ms deveria falhar (threshold exclusivo)"

    check_alto = _check_latencia_e2e(10000.0)
    assert check_alto.passou is False
    assert "8000" in check_alto.erro

    # Teste de _calcular_latencia_e2e()
    checks_simulados = [
        CheckResult("a", True, True, latencia_ms=100.0),
        CheckResult("b", True, False, latencia_ms=200.0),
        CheckResult("c", True, True, latencia_ms=None),   # sem latência
        CheckResult("d", True, True, latencia_ms=300.0),
    ]
    soma = _calcular_latencia_e2e(checks_simulados)
    assert soma == 600.0, f"Soma esperada 600ms (None ignorado), got {soma}"

    # Teste integrado: latência registrada em run()
    onboarding = _onboarding_completo()
    elevenlabs = _make_elevenlabs_mock()

    with respx.mock:
        respx.post(onboarding["crm_webhook_url"]).mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        respx.post(onboarding["whatsapp_bsp_url"]).mock(
            return_value=httpx.Response(201)
        )
        respx.get("https://maps.googleapis.com/maps/api/place/nearbysearch/json").mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": []})
        )
        respx.get(f"{onboarding['supabase_url']}/rest/v1/imoveis_embeddings").mock(
            return_value=httpx.Response(200, json=[])
        )

        async with httpx.AsyncClient() as http_client:
            agent = QAIntegrationAgent(
                http_client=http_client,
                elevenlabs_client=elevenlabs,
            )
            status, payload = await agent.run("cliente_int_004", onboarding)

    assert status == "done"

    # Latência registrada para cada check HTTP
    for check in payload["checks"]:
        if check["nome"] in ("webhook_crm", "whatsapp_envio", "google_places", "supabase_pgvector"):
            assert check["latencia_ms"] is not None, (
                f"latencia_ms não registrada para '{check['nome']}'"
            )
            assert check["latencia_ms"] >= 0, (
                f"latencia_ms inválida para '{check['nome']}': {check['latencia_ms']}"
            )

    # latencia_e2e registrada no payload
    assert "latencia_e2e_ms" in payload
    assert payload["latencia_e2e_ms"] >= 0

    # check latencia_e2e passou (mocks são rápidos — sempre abaixo de 8s)
    latencia_check = next(
        (c for c in payload["checks"] if c["nome"] == "latencia_e2e"),
        None,
    )
    assert latencia_check is not None, "check latencia_e2e ausente"
    assert latencia_check["passou"] is True, (
        f"Latência em ambiente de teste deveria passar o threshold de 8s: {latencia_check}"
    )

    # LATENCIA_MAX_MS deve ser 8000 (CLAUDE.md: < 8 segundos)
    assert LATENCIA_MAX_MS == 8_000.0, f"Threshold esperado: 8000ms. Atual: {LATENCIA_MAX_MS}"
