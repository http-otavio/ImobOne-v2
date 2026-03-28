"""
tests/test_monitor.py — Testes unitários do Agente 10: Monitor de Produção

Cobertura obrigatória:
  1. Métricas saudáveis → status "ok", sem anomalias
  2. Latência acima do threshold → anomalia WARNING, tipo LATENCIA_ALTA
  3. Taxa de erro > 2% → anomalia CRITICO, tipo TAXA_ERRO_ALTA
  4. 3+ falhas consecutivas → anomalia EMERGENCIAL, tipo FALHAS_CONSECUTIVAS
  5. Drift comportamental > 0.40 → anomalia CRITICO, tipo DRIFT_COMPORTAMENTO
  6. Múltiplas anomalias simultâneas → todas detectadas
  7. Alerta enviado via Slack → status "alerta_enviado", canal registrado
  8. Nenhum canal configurado → status "alerta_sem_canal"
  9. Canal Slack falha (HTTP 500) → status "alerta_sem_canal"
 10. WhatsApp como canal secundário → alerta_enviado
"""

import pytest
import respx
import httpx

from agents.monitor import (
    MonitorAgent,
    NivelAlerta,
    TipoAnomalia,
    avaliar_metricas,
    LATENCIA_WARNING_MS,
    TAXA_ERRO_CRITICO_PERCENT,
    FALHAS_CONSECUTIVAS_EMERGENCIAL,
    DRIFT_SCORE_CRITICO,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SLACK_URL = "https://hooks.slack.com/services/TEST/WEBHOOK"
WA_BSP_URL = "https://waba.360dialog.io/v1"
WA_NUMERO = "+5511999990000"
WA_KEY = "fake-bsp-key"


def _monitor_sem_canal() -> MonitorAgent:
    """Monitor sem nenhum canal de alerta configurado."""
    return MonitorAgent(
        slack_webhook_url="",
        whatsapp_operator_number="",
        whatsapp_bsp_url="",
        whatsapp_bsp_api_key="",
    )


def _monitor_com_slack(http_client: httpx.AsyncClient) -> MonitorAgent:
    return MonitorAgent(
        slack_webhook_url=SLACK_URL,
        whatsapp_operator_number="",
        whatsapp_bsp_url="",
        whatsapp_bsp_api_key="",
        http_client=http_client,
    )


def _monitor_com_whatsapp(http_client: httpx.AsyncClient) -> MonitorAgent:
    return MonitorAgent(
        slack_webhook_url="",
        whatsapp_operator_number=WA_NUMERO,
        whatsapp_bsp_url=WA_BSP_URL,
        whatsapp_bsp_api_key=WA_KEY,
        http_client=http_client,
    )


def _metricas_saudaveis() -> dict:
    return {
        "latencia_media_ms": 1_200.0,
        "taxa_erro_percent": 0.5,
        "falhas_consecutivas": 0,
        "drift_score": 0.10,
    }


# ---------------------------------------------------------------------------
# Teste 1: métricas saudáveis → ok
# ---------------------------------------------------------------------------


async def test_metricas_saudaveis_retorna_ok():
    monitor = _monitor_sem_canal()
    status, payload = await monitor.run("cli_001", _metricas_saudaveis())

    assert status == "ok", f"Esperado 'ok', obtido '{status}'"
    assert payload["anomalias"] == []
    assert payload["client_id"] == "cli_001"


# ---------------------------------------------------------------------------
# Teste 2: latência alta → WARNING
# ---------------------------------------------------------------------------


async def test_latencia_alta_gera_anomalia_warning():
    metricas = _metricas_saudaveis()
    metricas["latencia_media_ms"] = LATENCIA_WARNING_MS + 1.0  # 8 001 ms

    anomalias = avaliar_metricas(metricas)

    latencia_anomalias = [a for a in anomalias if a.tipo == TipoAnomalia.LATENCIA_ALTA]
    assert len(latencia_anomalias) == 1
    a = latencia_anomalias[0]
    assert a.nivel == NivelAlerta.WARNING
    assert a.valor_medido == pytest.approx(LATENCIA_WARNING_MS + 1.0)
    assert a.threshold == pytest.approx(LATENCIA_WARNING_MS)


# ---------------------------------------------------------------------------
# Teste 3: taxa de erro > 2% → CRÍTICO
# ---------------------------------------------------------------------------


async def test_taxa_erro_alta_gera_anomalia_critica():
    metricas = _metricas_saudaveis()
    metricas["taxa_erro_percent"] = TAXA_ERRO_CRITICO_PERCENT + 0.5  # 2.5%

    anomalias = avaliar_metricas(metricas)

    erro_anomalias = [a for a in anomalias if a.tipo == TipoAnomalia.TAXA_ERRO_ALTA]
    assert len(erro_anomalias) == 1
    a = erro_anomalias[0]
    assert a.nivel == NivelAlerta.CRITICO
    assert a.valor_medido == pytest.approx(TAXA_ERRO_CRITICO_PERCENT + 0.5)


# ---------------------------------------------------------------------------
# Teste 4: 3 falhas consecutivas → EMERGENCIAL
# ---------------------------------------------------------------------------


async def test_falhas_consecutivas_geram_anomalia_emergencial():
    metricas = _metricas_saudaveis()
    metricas["falhas_consecutivas"] = FALHAS_CONSECUTIVAS_EMERGENCIAL  # exato = 3

    anomalias = avaliar_metricas(metricas)

    emergencial = [a for a in anomalias if a.tipo == TipoAnomalia.FALHAS_CONSECUTIVAS]
    assert len(emergencial) == 1
    a = emergencial[0]
    assert a.nivel == NivelAlerta.EMERGENCIAL
    assert int(a.valor_medido) == FALHAS_CONSECUTIVAS_EMERGENCIAL


# ---------------------------------------------------------------------------
# Teste 5: drift comportamental > 0.40 → CRÍTICO
# ---------------------------------------------------------------------------


async def test_drift_comportamental_gera_anomalia_critica():
    metricas = _metricas_saudaveis()
    metricas["drift_score"] = DRIFT_SCORE_CRITICO + 0.05  # 0.45

    anomalias = avaliar_metricas(metricas)

    drift_anomalias = [a for a in anomalias if a.tipo == TipoAnomalia.DRIFT_COMPORTAMENTO]
    assert len(drift_anomalias) == 1
    a = drift_anomalias[0]
    assert a.nivel == NivelAlerta.CRITICO


# ---------------------------------------------------------------------------
# Teste 6: múltiplas anomalias simultâneas
# ---------------------------------------------------------------------------


async def test_multiplas_anomalias_detectadas_simultaneamente():
    metricas = {
        "latencia_media_ms": 9_500.0,          # WARNING
        "taxa_erro_percent": 5.0,              # CRITICO
        "falhas_consecutivas": 4,              # EMERGENCIAL
        "drift_score": 0.55,                   # CRITICO
    }

    anomalias = avaliar_metricas(metricas)

    tipos = {a.tipo for a in anomalias}
    assert TipoAnomalia.LATENCIA_ALTA in tipos
    assert TipoAnomalia.TAXA_ERRO_ALTA in tipos
    assert TipoAnomalia.FALHAS_CONSECUTIVAS in tipos
    assert TipoAnomalia.DRIFT_COMPORTAMENTO in tipos
    assert len(anomalias) == 4


# ---------------------------------------------------------------------------
# Teste 7: alerta enviado via Slack com sucesso → "alerta_enviado"
# ---------------------------------------------------------------------------


async def test_alerta_enviado_via_slack_retorna_alerta_enviado():
    metricas = _metricas_saudaveis()
    metricas["taxa_erro_percent"] = 5.0  # força anomalia

    with respx.mock:
        respx.post(SLACK_URL).mock(return_value=httpx.Response(200, text="ok"))

        async with httpx.AsyncClient() as http:
            monitor = _monitor_com_slack(http)
            status, payload = await monitor.run("cli_002", metricas)

    assert status == "alerta_enviado", f"Esperado 'alerta_enviado', obtido '{status}'"
    assert "slack" in payload["alertas_enviados"]
    assert len(payload["anomalias"]) > 0


# ---------------------------------------------------------------------------
# Teste 8: sem canal configurado → "alerta_sem_canal"
# ---------------------------------------------------------------------------


async def test_sem_canal_configurado_retorna_alerta_sem_canal():
    metricas = _metricas_saudaveis()
    metricas["falhas_consecutivas"] = 3  # força anomalia

    monitor = _monitor_sem_canal()
    status, payload = await monitor.run("cli_003", metricas)

    assert status == "alerta_sem_canal"
    assert len(payload["anomalias"]) > 0
    assert payload["alertas_enviados"] == []


# ---------------------------------------------------------------------------
# Teste 9: canal Slack retorna 500 → "alerta_sem_canal" (todos os canais falharam)
# ---------------------------------------------------------------------------


async def test_slack_com_erro_http_retorna_alerta_sem_canal():
    metricas = _metricas_saudaveis()
    metricas["taxa_erro_percent"] = 10.0

    with respx.mock:
        respx.post(SLACK_URL).mock(return_value=httpx.Response(500, text="error"))

        async with httpx.AsyncClient() as http:
            monitor = _monitor_com_slack(http)
            status, payload = await monitor.run("cli_004", metricas)

    assert status == "alerta_sem_canal"
    assert payload["alertas_enviados"] == []
    # erros devem registrar a falha do Slack
    assert any("slack" in e.lower() for e in payload["erros"])


# ---------------------------------------------------------------------------
# Teste 10: WhatsApp como canal → alerta_enviado
# ---------------------------------------------------------------------------


async def test_alerta_enviado_via_whatsapp():
    metricas = _metricas_saudaveis()
    metricas["falhas_consecutivas"] = 5  # EMERGENCIAL

    with respx.mock:
        respx.post(f"{WA_BSP_URL}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "abc123"}]})
        )

        async with httpx.AsyncClient() as http:
            monitor = _monitor_com_whatsapp(http)
            status, payload = await monitor.run("cli_005", metricas)

    assert status == "alerta_enviado"
    assert "whatsapp" in payload["alertas_enviados"]


# ---------------------------------------------------------------------------
# Teste extra: threshold exato não dispara (apenas > threshold)
# ---------------------------------------------------------------------------


async def test_threshold_exato_nao_dispara_latencia():
    """Latência exatamente igual ao threshold NÃO deve gerar anomalia."""
    metricas = _metricas_saudaveis()
    metricas["latencia_media_ms"] = LATENCIA_WARNING_MS  # exato = 8 000

    anomalias = avaliar_metricas(metricas)
    latencia_anomalias = [a for a in anomalias if a.tipo == TipoAnomalia.LATENCIA_ALTA]
    assert latencia_anomalias == [], (
        "Threshold exato (8 000 ms) não deve gerar anomalia — apenas valores estritamente maiores."
    )


async def test_threshold_exato_nao_dispara_taxa_erro():
    """Taxa de erro exatamente igual ao threshold NÃO deve gerar anomalia."""
    metricas = _metricas_saudaveis()
    metricas["taxa_erro_percent"] = TAXA_ERRO_CRITICO_PERCENT  # exato = 2.0

    anomalias = avaliar_metricas(metricas)
    erro_anomalias = [a for a in anomalias if a.tipo == TipoAnomalia.TAXA_ERRO_ALTA]
    assert erro_anomalias == []
