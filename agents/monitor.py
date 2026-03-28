"""
agents/monitor.py — Agente 10: Monitor de Produção

Responsabilidade:
  Único agente que continua rodando APÓS o deploy. Detecta anomalias e emite
  alertas antes que o cliente perceba degradação no serviço.

Métricas monitoradas:
  - taxa_erro_percent   → % de chamadas de API que retornaram erro
  - latencia_media_ms   → média de latência das respostas ao lead
  - falhas_consecutivas → contador de falhas ininterruptas (sem sucesso entre elas)
  - drift_score         → 0.0–1.0: desvio comportamental detectado vs padrão treinado

Thresholds (CLAUDE.md):
  ┌──────────────────────────────────────┬─────────────┬─────────────────────┐
  │ Condição                             │ Nível        │ Ação                │
  ├──────────────────────────────────────┼─────────────┼─────────────────────┤
  │ latência média > 8 000 ms            │ WARNING      │ alerta operador     │
  │ taxa de erro > 2 %                   │ CRÍTICO      │ alerta operador     │
  │ 3+ falhas consecutivas               │ EMERGENCIAL  │ alerta operador     │
  │ drift_score > 0.40                   │ CRÍTICO      │ alerta operador     │
  └──────────────────────────────────────┴─────────────┴─────────────────────┘

Canais de alerta:
  - Slack webhook (ALERT_SLACK_WEBHOOK no .env)
  - WhatsApp do operador (WHATSAPP_OPERATOR_NUMBER + BSP API)
  Pelo menos um dos dois deve estar configurado. Se nenhum estiver, monitor
  registra localmente e retorna status "alerta_sem_canal".

Integração com o orquestrador:
  run(client_id, metricas) → (status, payload) onde:
    status ∈ {"ok", "alerta_enviado", "alerta_sem_canal", "blocked"}
    payload: {anomalias, alertas_enviados, timestamp, metricas_avaliadas}

Uso standalone:
    monitor = MonitorAgent()
    status, payload = await monitor.run("cliente_xyz", metricas_dict)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

LATENCIA_WARNING_MS: float = 8_000.0
TAXA_ERRO_CRITICO_PERCENT: float = 2.0
FALHAS_CONSECUTIVAS_EMERGENCIAL: int = 3
DRIFT_SCORE_CRITICO: float = 0.40


# ---------------------------------------------------------------------------
# Enums e dataclasses
# ---------------------------------------------------------------------------


class NivelAlerta(str, Enum):
    WARNING = "warning"
    CRITICO = "critico"
    EMERGENCIAL = "emergencial"


class TipoAnomalia(str, Enum):
    LATENCIA_ALTA = "latencia_alta"
    TAXA_ERRO_ALTA = "taxa_erro_alta"
    FALHAS_CONSECUTIVAS = "falhas_consecutivas"
    DRIFT_COMPORTAMENTO = "drift_comportamento"


@dataclass
class Anomalia:
    tipo: TipoAnomalia
    nivel: NivelAlerta
    detalhe: str
    valor_medido: float | None = None
    threshold: float | None = None

    def to_dict(self) -> dict:
        return {
            "tipo": self.tipo.value,
            "nivel": self.nivel.value,
            "detalhe": self.detalhe,
            "valor_medido": self.valor_medido,
            "threshold": self.threshold,
        }


@dataclass
class ResultadoAlerta:
    canal: str
    sucesso: bool
    erro: str | None = None


@dataclass
class ResultadoMonitor:
    client_id: str
    status: str  # "ok" | "alerta_enviado" | "alerta_sem_canal" | "blocked"
    anomalias: list[Anomalia] = field(default_factory=list)
    alertas_enviados: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metricas_avaliadas: dict = field(default_factory=dict)
    erros: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "status": self.status,
            "anomalias": [a.to_dict() for a in self.anomalias],
            "alertas_enviados": self.alertas_enviados,
            "timestamp": self.timestamp,
            "metricas_avaliadas": self.metricas_avaliadas,
            "erros": self.erros,
        }


# ---------------------------------------------------------------------------
# Lógica central de avaliação (pura, sem I/O)
# ---------------------------------------------------------------------------


def avaliar_metricas(metricas: dict[str, Any]) -> list[Anomalia]:
    """
    Avalia um snapshot de métricas e retorna a lista de anomalias detectadas.

    Parâmetros esperados em `metricas`:
      - latencia_media_ms:   float (opcional, default 0)
      - taxa_erro_percent:   float (opcional, default 0)
      - falhas_consecutivas: int   (opcional, default 0)
      - drift_score:         float (opcional, default 0.0)

    Retorna lista de Anomalia (pode ser vazia se tudo OK).
    """
    anomalias: list[Anomalia] = []

    latencia = float(metricas.get("latencia_media_ms", 0.0))
    taxa_erro = float(metricas.get("taxa_erro_percent", 0.0))
    falhas_consec = int(metricas.get("falhas_consecutivas", 0))
    drift = float(metricas.get("drift_score", 0.0))

    # Latência > 8 000 ms → WARNING
    if latencia > LATENCIA_WARNING_MS:
        anomalias.append(
            Anomalia(
                tipo=TipoAnomalia.LATENCIA_ALTA,
                nivel=NivelAlerta.WARNING,
                detalhe=(
                    f"Latência média de {latencia:.0f} ms excede o threshold de "
                    f"{LATENCIA_WARNING_MS:.0f} ms."
                ),
                valor_medido=latencia,
                threshold=LATENCIA_WARNING_MS,
            )
        )

    # Taxa de erro > 2% → CRÍTICO
    if taxa_erro > TAXA_ERRO_CRITICO_PERCENT:
        anomalias.append(
            Anomalia(
                tipo=TipoAnomalia.TAXA_ERRO_ALTA,
                nivel=NivelAlerta.CRITICO,
                detalhe=(
                    f"Taxa de erro de {taxa_erro:.2f}% excede o threshold crítico de "
                    f"{TAXA_ERRO_CRITICO_PERCENT:.1f}%."
                ),
                valor_medido=taxa_erro,
                threshold=TAXA_ERRO_CRITICO_PERCENT,
            )
        )

    # 3+ falhas consecutivas → EMERGENCIAL
    if falhas_consec >= FALHAS_CONSECUTIVAS_EMERGENCIAL:
        anomalias.append(
            Anomalia(
                tipo=TipoAnomalia.FALHAS_CONSECUTIVAS,
                nivel=NivelAlerta.EMERGENCIAL,
                detalhe=(
                    f"{falhas_consec} falhas consecutivas detectadas "
                    f"(threshold: {FALHAS_CONSECUTIVAS_EMERGENCIAL})."
                ),
                valor_medido=float(falhas_consec),
                threshold=float(FALHAS_CONSECUTIVAS_EMERGENCIAL),
            )
        )

    # Drift comportamental > 0.40 → CRÍTICO
    if drift > DRIFT_SCORE_CRITICO:
        anomalias.append(
            Anomalia(
                tipo=TipoAnomalia.DRIFT_COMPORTAMENTO,
                nivel=NivelAlerta.CRITICO,
                detalhe=(
                    f"Drift comportamental de {drift:.2f} excede o threshold de "
                    f"{DRIFT_SCORE_CRITICO:.2f}. "
                    "O consultor pode estar desviando do padrão de qualidade treinado."
                ),
                valor_medido=drift,
                threshold=DRIFT_SCORE_CRITICO,
            )
        )

    return anomalias


# ---------------------------------------------------------------------------
# Formatação da mensagem de alerta
# ---------------------------------------------------------------------------


def _formatar_mensagem_alerta(client_id: str, anomalias: list[Anomalia]) -> str:
    """Formata uma mensagem de alerta legível para operador humano."""
    niveis = {a.nivel for a in anomalias}
    nivel_maximo = (
        NivelAlerta.EMERGENCIAL
        if NivelAlerta.EMERGENCIAL in niveis
        else NivelAlerta.CRITICO
        if NivelAlerta.CRITICO in niveis
        else NivelAlerta.WARNING
    )

    emoji = {
        NivelAlerta.WARNING: "⚠️",
        NivelAlerta.CRITICO: "🚨",
        NivelAlerta.EMERGENCIAL: "🆘",
    }[nivel_maximo]

    linhas = [
        f"{emoji} *ImobOne Monitor — {nivel_maximo.value.upper()}*",
        f"Cliente: `{client_id}`",
        f"Anomalias detectadas: {len(anomalias)}",
        "",
    ]
    for i, a in enumerate(anomalias, 1):
        linhas.append(f"{i}. [{a.nivel.value.upper()}] {a.detalhe}")

    linhas += [
        "",
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}",
    ]
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Envio de alertas
# ---------------------------------------------------------------------------


async def _enviar_slack(
    mensagem: str,
    webhook_url: str,
    http_client: httpx.AsyncClient,
) -> ResultadoAlerta:
    """Envia alerta via Slack incoming webhook."""
    try:
        resp = await http_client.post(
            webhook_url,
            json={"text": mensagem},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return ResultadoAlerta(canal="slack", sucesso=True)
        return ResultadoAlerta(
            canal="slack",
            sucesso=False,
            erro=f"Slack retornou HTTP {resp.status_code}",
        )
    except Exception as exc:
        return ResultadoAlerta(canal="slack", sucesso=False, erro=str(exc))


async def _enviar_whatsapp(
    mensagem: str,
    numero_operador: str,
    bsp_url: str,
    bsp_api_key: str,
    http_client: httpx.AsyncClient,
) -> ResultadoAlerta:
    """Envia alerta via WhatsApp Business API (BSP) para o número do operador."""
    payload = {
        "to": numero_operador,
        "type": "text",
        "text": {"body": mensagem},
    }
    headers = {
        "Authorization": f"Bearer {bsp_api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = await http_client.post(
            f"{bsp_url.rstrip('/')}/messages",
            json=payload,
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code in (200, 201):
            return ResultadoAlerta(canal="whatsapp", sucesso=True)
        return ResultadoAlerta(
            canal="whatsapp",
            sucesso=False,
            erro=f"WhatsApp BSP retornou HTTP {resp.status_code}",
        )
    except Exception as exc:
        return ResultadoAlerta(canal="whatsapp", sucesso=False, erro=str(exc))


# ---------------------------------------------------------------------------
# Agente principal
# ---------------------------------------------------------------------------


class MonitorAgent:
    """
    Monitor de produção. Avalia métricas de um cliente e emite alertas
    via Slack e/ou WhatsApp quando anomalias são detectadas.

    Todos os parâmetros de configuração são injetáveis para facilitar testes:
      - slack_webhook_url
      - whatsapp_operator_number
      - whatsapp_bsp_url
      - whatsapp_bsp_api_key
      - http_client

    Se não fornecidos, lidos do ambiente (.env).
    """

    def __init__(
        self,
        *,
        slack_webhook_url: str | None = None,
        whatsapp_operator_number: str | None = None,
        whatsapp_bsp_url: str | None = None,
        whatsapp_bsp_api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._slack_url = slack_webhook_url or os.getenv("ALERT_SLACK_WEBHOOK", "")
        self._wa_numero = whatsapp_operator_number or os.getenv(
            "WHATSAPP_OPERATOR_NUMBER", ""
        )
        self._wa_bsp_url = whatsapp_bsp_url or os.getenv("WHATSAPP_BSP_URL", "")
        self._wa_bsp_key = whatsapp_bsp_api_key or os.getenv("WHATSAPP_BSP_API_KEY", "")
        self._http_client = http_client  # None = cria internamente por sessão

    @property
    def _tem_canal_slack(self) -> bool:
        return bool(self._slack_url)

    @property
    def _tem_canal_whatsapp(self) -> bool:
        return bool(self._wa_numero and self._wa_bsp_url and self._wa_bsp_key)

    @property
    def tem_algum_canal(self) -> bool:
        return self._tem_canal_slack or self._tem_canal_whatsapp

    async def _disparar_alertas(
        self,
        client_id: str,
        anomalias: list[Anomalia],
        http: httpx.AsyncClient,
    ) -> list[ResultadoAlerta]:
        """Dispara alertas em todos os canais configurados simultaneamente."""
        import asyncio

        mensagem = _formatar_mensagem_alerta(client_id, anomalias)
        tarefas = []

        if self._tem_canal_slack:
            tarefas.append(_enviar_slack(mensagem, self._slack_url, http))

        if self._tem_canal_whatsapp:
            tarefas.append(
                _enviar_whatsapp(
                    mensagem,
                    self._wa_numero,
                    self._wa_bsp_url,
                    self._wa_bsp_key,
                    http,
                )
            )

        if not tarefas:
            return []

        return await asyncio.gather(*tarefas)

    async def run(
        self, client_id: str, metricas: dict[str, Any]
    ) -> tuple[str, dict]:
        """
        Avalia métricas de um cliente e emite alertas se necessário.

        Returns:
            (status, payload) onde:
            - "ok"               → sem anomalias
            - "alerta_enviado"   → anomalias detectadas, pelo menos um canal confirmou
            - "alerta_sem_canal" → anomalias detectadas, nenhum canal configurado
            - "blocked"          → erro interno impediu a avaliação
        """
        resultado = ResultadoMonitor(
            client_id=client_id,
            status="ok",
            metricas_avaliadas=metricas,
        )

        try:
            anomalias = avaliar_metricas(metricas)
            resultado.anomalias = anomalias

            if not anomalias:
                resultado.status = "ok"
                return "ok", resultado.to_dict()

            # Há anomalias — precisamos alertar
            if not self.tem_algum_canal:
                logger.warning(
                    "Monitor detectou anomalias para '%s' mas nenhum canal de alerta "
                    "está configurado. Configure ALERT_SLACK_WEBHOOK ou "
                    "WHATSAPP_OPERATOR_NUMBER + WHATSAPP_BSP_URL + WHATSAPP_BSP_API_KEY.",
                    client_id,
                )
                resultado.status = "alerta_sem_canal"
                return "alerta_sem_canal", resultado.to_dict()

            # Envia alertas
            if self._http_client is not None:
                resultados = await self._disparar_alertas(client_id, anomalias, self._http_client)
            else:
                async with httpx.AsyncClient() as http:
                    resultados = await self._disparar_alertas(client_id, anomalias, http)

            canais_ok = [r.canal for r in resultados if r.sucesso]
            canais_err = [r for r in resultados if not r.sucesso]

            resultado.alertas_enviados = canais_ok
            resultado.erros = [f"{r.canal}: {r.erro}" for r in canais_err]

            if canais_ok:
                resultado.status = "alerta_enviado"
            else:
                # Tentou enviar mas todos os canais falharam
                resultado.status = "alerta_sem_canal"
                logger.error(
                    "Todos os canais de alerta falharam para '%s': %s",
                    client_id,
                    resultado.erros,
                )

        except Exception as exc:
            logger.exception("Erro interno no MonitorAgent para '%s'", client_id)
            resultado.status = "blocked"
            resultado.erros = [str(exc)]

        return resultado.status, resultado.to_dict()
