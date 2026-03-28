"""
dashboard/backend/main.py — FastAPI do dashboard do gestor

Endpoints implementados:
  GET  /health          → healthcheck do container (usado pelo Docker Swarm)
  GET  /api/clients     → lista clientes configurados
  GET  /api/clients/{id}/status  → status de pipeline + métricas do monitor
  GET  /api/clients/{id}/report  → relatório do último pipeline_report.json
  WS   /ws/alerts       → stream de alertas do monitor em tempo real (WebSocket)

Autenticação: Bearer token via DASHBOARD_SECRET_KEY no .env.
Em desenvolvimento (SECRET_KEY ausente), autenticação desabilitada com aviso.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ImobOne Dashboard",
    description="Dashboard do gestor — monitoramento de agentes e clientes",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # restringir em produção para o domínio do frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Paths base
# ---------------------------------------------------------------------------

BASE_DIR  = Path(__file__).parent.parent.parent   # raiz do projeto /app
CLIENTS_DIR = BASE_DIR / "clients"

# ---------------------------------------------------------------------------
# Auth simples via Bearer token
# ---------------------------------------------------------------------------

_SECRET = os.getenv("DASHBOARD_SECRET_KEY", "")
_bearer = HTTPBearer(auto_error=False)


def _require_auth(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    """
    Valida o Bearer token contra DASHBOARD_SECRET_KEY.
    Se a chave não estiver configurada no ambiente, autenticação é DESABILITADA
    com log de warning — nunca silenciosamente permissivo em produção.
    """
    if not _SECRET:
        logger.warning(
            "DASHBOARD_SECRET_KEY não configurada — autenticação desabilitada. "
            "Configure a variável antes de expor o dashboard publicamente."
        )
        return  # dev mode: sem auth

    if credentials is None or credentials.credentials != _SECRET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido ou ausente.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Healthcheck — obrigatório para o Docker Swarm healthcheck
# ---------------------------------------------------------------------------

_START_TIME = time.time()


@app.get("/health", tags=["infra"], include_in_schema=False)
async def health():
    """
    Healthcheck do container. Retorna 200 quando o serviço está pronto.
    Verificado pelo Docker Swarm a cada 30s (definido no docker-compose.yml).
    Também verifica conectividade com o Redis.
    """
    redis_ok = False
    redis_error = None
    try:
        redis_url = os.getenv("REDIS_URL", "redis://n8n_n8n_redis:6379")
        import redis as _redis
        r = _redis.Redis.from_url(redis_url, socket_connect_timeout=3)
        r.ping()
        redis_ok = True
    except Exception as exc:
        redis_error = str(exc)

    uptime_s = int(time.time() - _START_TIME)
    payload   = {
        "status":    "ok" if redis_ok else "degraded",
        "uptime_s":  uptime_s,
        "redis":     "ok" if redis_ok else f"unreachable ({redis_error})",
        "version":   app.version,
    }
    # Retorna 200 mesmo se Redis degradado — o container está UP.
    # O Swarm não mata o container por Redis instável; o monitor já alerta isso.
    return JSONResponse(content=payload, status_code=200)


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------


@app.get("/api/clients", tags=["clients"])
async def list_clients(_=Depends(_require_auth)):
    """Lista todos os clientes com pasta em /clients."""
    if not CLIENTS_DIR.exists():
        return {"clients": []}

    clients = []
    for path in sorted(CLIENTS_DIR.iterdir()):
        if not path.is_dir():
            continue
        onboarding_path = path / "onboarding.json"
        report_path     = path / "pipeline_report.json"

        entry = {"client_id": path.name, "has_report": report_path.exists()}

        if onboarding_path.exists():
            try:
                with open(onboarding_path, encoding="utf-8") as f:
                    ob = json.load(f)
                entry["nome_imobiliaria"] = ob.get("nome_imobiliaria", "")
                entry["cidade"]           = ob.get("cidade_atuacao", "")
            except (json.JSONDecodeError, OSError):
                pass

        if report_path.exists():
            try:
                with open(report_path, encoding="utf-8") as f:
                    rpt = json.load(f)
                entry["deploy_status"] = rpt.get("deploy_status")
                entry["timestamp"]     = rpt.get("timestamp")
            except (json.JSONDecodeError, OSError):
                pass

        clients.append(entry)

    return {"clients": clients}


@app.get("/api/clients/{client_id}/status", tags=["clients"])
async def client_status(client_id: str, _=Depends(_require_auth)):
    """Status atual do pipeline e métricas do monitor para um cliente."""
    report_path = CLIENTS_DIR / client_id / "pipeline_report.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail=f"Cliente '{client_id}' sem relatório de pipeline.")
    try:
        with open(report_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao ler relatório: {exc}") from exc


@app.get("/api/clients/{client_id}/report", tags=["clients"])
async def client_report(client_id: str, _=Depends(_require_auth)):
    """Relatório completo do último pipeline_report.json."""
    return await client_status(client_id, _)   # mesmo handler por ora


# ---------------------------------------------------------------------------
# WebSocket — stream de alertas do monitor
# ---------------------------------------------------------------------------

_alert_connections: list[WebSocket] = []


@app.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket):
    """
    Stream de alertas em tempo real.
    O monitor publica via Redis pub/sub; este endpoint faz bridge para o browser.
    Por ora: envia ping a cada 15s para manter conexão viva.
    TODO: conectar ao Redis pub/sub e retransmitir alertas reais.
    """
    await websocket.accept()
    _alert_connections.append(websocket)
    logger.info("WebSocket conectado — total: %d", len(_alert_connections))
    try:
        while True:
            import asyncio
            await asyncio.sleep(15)
            await websocket.send_json({"type": "ping", "timestamp": time.time()})
    except WebSocketDisconnect:
        _alert_connections.remove(websocket)
        logger.info("WebSocket desconectado — total: %d", len(_alert_connections))


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup():
    logger.info("Dashboard FastAPI iniciado. Redis: %s", os.getenv("REDIS_URL", "não configurado"))


@app.on_event("shutdown")
async def on_shutdown():
    for ws in list(_alert_connections):
        await ws.close()
    logger.info("Dashboard encerrado.")
