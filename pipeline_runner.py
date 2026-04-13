"""
pipeline_runner.py — Runner autônomo do pipeline de setup

Roda como serviço systemd na porta 8002. Aceita requisições para disparar o
pipeline de onboarding de novos clientes e notifica o operador via WhatsApp
quando termina — sem precisar ficar olhando o terminal.

Endpoints:
    POST /pipeline/start
        Body: { "client_id": "alfa_imoveis", "skip_agents": [] }
        → Dispara o pipeline em background. Retorna task_id imediatamente.

    GET  /pipeline/status/{client_id}
        → Retorna status atual (queued | running | done | failed | human_review)

    POST /pipeline/start-json
        Body: { "client_id": "...", "onboarding": { ...json completo... } }
        → Igual ao /start mas aceita o onboarding inline (sem arquivo no disco)

    GET  /health
        → Liveness check

Notificações WhatsApp (número configurado em OPERATOR_NUMBER):
    ✅ deploy_ready  — "Pipeline de {client_id} concluído em X min. Deploy aprovado."
    ⚠️ human_review  — "Pipeline de {client_id} precisa de revisão humana. [detalhes]"
    ❌ failed        — "Pipeline de {client_id} falhou. [erro]"
    🚀 started       — "Pipeline de {client_id} iniciado. Você será notificado ao terminar."

Uso no VPS:
    # Iniciar manualmente (dev)
    cd /opt/ImobOne-v2
    /opt/webhook-venv/bin/python3 pipeline_runner.py

    # Via systemd (produção)
    systemctl start imob-runner

    # Disparar pipeline de um cliente
    curl -X POST http://localhost:8002/pipeline/start \\
         -H "Content-Type: application/json" \\
         -d '{"client_id": "alfa_imoveis"}'
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pipeline_runner")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR       = Path(__file__).parent
CLIENTS_DIR    = BASE_DIR / "clients"

# WhatsApp (Evolution API) — mesmas vars do webhook
EVOLUTION_URL      = os.getenv("EVOLUTION_URL",      "https://api.otaviolabs.com")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY",  "79ffc1f3960f03a27a67e2b1e678d98b")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "devlabz")

# Número do operador que recebe as notificações (sem +)
OPERATOR_NUMBER = os.getenv("OPERATOR_NUMBER", "5511973722075")

# Segredo simples para proteger o endpoint (opcional — vazio = sem auth)
RUNNER_SECRET = os.getenv("RUNNER_SECRET", "")

# ---------------------------------------------------------------------------
# Estado em memória dos jobs (complementado por Redis se disponível)
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}


def _set_job(client_id: str, **fields):
    job = _jobs.setdefault(client_id, {"client_id": client_id})
    job.update(fields)
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    _persist_job(client_id, job)


def _persist_job(client_id: str, job: dict):
    """Salva estado do job em disco para sobreviver a restart."""
    try:
        path = CLIENTS_DIR / client_id / "runner_job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning("Não foi possível persistir job %s: %s", client_id, e)


def _load_job(client_id: str) -> dict | None:
    if client_id in _jobs:
        return _jobs[client_id]
    path = CLIENTS_DIR / client_id / "runner_job.json"
    if path.exists():
        try:
            job = json.loads(path.read_text())
            _jobs[client_id] = job
            return job
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# WhatsApp notifications
# ---------------------------------------------------------------------------

def _notify(message: str):
    """Envia mensagem ao operador via Evolution API (fire-and-forget síncrono)."""
    if not OPERATOR_NUMBER:
        log.warning("OPERATOR_NUMBER não configurado — notificação não enviada.")
        return
    payload = json.dumps({"number": OPERATOR_NUMBER, "text": message}).encode()
    req = urllib.request.Request(
        f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}",
        data=payload,
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            log.info("Notificação enviada ao operador | HTTP %s", r.status)
    except Exception as e:
        log.error("Falha ao enviar notificação WhatsApp: %s", e)


# ---------------------------------------------------------------------------
# Pipeline runner (background task)
# ---------------------------------------------------------------------------

async def _run_pipeline_job(client_id: str, onboarding: dict, skip_agents: list[str]):
    """
    Executa o pipeline completo de forma assíncrona.
    Atualiza _jobs e notifica o operador ao terminar.
    """
    start = time.monotonic()
    _set_job(client_id, status="running", started_at=datetime.now(timezone.utc).isoformat())

    # Importa a função run_pipeline do setup_pipeline.py
    # Feito aqui dentro para capturar erros de import sem quebrar o servidor
    try:
        # Garante que o diretório do projeto está no path
        if str(BASE_DIR) not in sys.path:
            sys.path.insert(0, str(BASE_DIR))
        from setup_pipeline import run_pipeline
    except ImportError as exc:
        msg = f"❌ Erro de import no pipeline de *{client_id}*: {exc}"
        log.error(msg)
        _set_job(client_id, status="failed", error=str(exc))
        _notify(msg)
        return

    try:
        exit_code = await run_pipeline(
            client_id=client_id,
            onboarding=onboarding,
            skip_agents=skip_agents or [],
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        msg = (
            f"❌ Pipeline de *{client_id}* travou com exceção após "
            f"{elapsed/60:.1f} min.\n\n"
            f"Erro: {exc}\n\n"
            f"Acesse o VPS para ver o log completo."
        )
        log.exception("Exceção durante pipeline de %s", client_id)
        _set_job(client_id, status="failed", error=str(exc), elapsed_seconds=round(elapsed, 1))
        _notify(msg)
        return

    elapsed = time.monotonic() - start
    mins = elapsed / 60

    if exit_code == 0:
        status = "done"
        msg = (
            f"✅ Pipeline de *{client_id}* concluído com sucesso em {mins:.1f} min.\n\n"
            f"Status: deploy_ready 🚀\n"
            f"O consultor digital está ativo para este cliente.\n\n"
            f"Relatório salvo em clients/{client_id}/pipeline_report.json"
        )
    elif exit_code == 1:
        # Pode ser human_review ou falha real — lê o relatório para distinguir
        report = _read_report(client_id)
        deploy_status = report.get("deploy_status", "unknown") if report else "unknown"
        blocked = report.get("blocked_agents", []) if report else []

        if deploy_status == "human_review":
            status = "human_review"
            blocked_str = ", ".join(blocked) if blocked else "ver relatório"
            msg = (
                f"⚠️ Pipeline de *{client_id}* precisa de revisão humana ({mins:.1f} min).\n\n"
                f"Causa: iteração máxima atingida.\n"
                f"Agentes bloqueados: {blocked_str}\n\n"
                f"Para corrigir e re-executar:\n"
                f"  python setup_pipeline.py --client-id {client_id} --reset\n\n"
                f"Ou dispare novamente via:\n"
                f"  POST http://localhost:8002/pipeline/start"
            )
        else:
            status = "failed"
            errors = report.get("errors", []) if report else []
            errors_str = "\n• ".join(errors[:3]) if errors else "ver relatório"
            msg = (
                f"❌ Pipeline de *{client_id}* falhou após {mins:.1f} min.\n\n"
                f"Status: {deploy_status}\n"
                f"Erros:\n• {errors_str}\n\n"
                f"Relatório: clients/{client_id}/pipeline_report.json"
            )
    else:
        status = "failed"
        msg = (
            f"❌ Pipeline de *{client_id}* encerrou com código {exit_code} "
            f"após {mins:.1f} min.\n"
            f"Verifique o onboarding.json e tente novamente."
        )

    _set_job(client_id, status=status, exit_code=exit_code, elapsed_seconds=round(elapsed, 1))
    _notify(msg)
    log.info("Job %s finalizado — status=%s exit=%d", client_id, status, exit_code)


def _read_report(client_id: str) -> dict | None:
    path = CLIENTS_DIR / client_id / "pipeline_report.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None


def _load_onboarding(client_id: str) -> dict:
    """Carrega onboarding.json do cliente a partir do disco."""
    path = CLIENTS_DIR / client_id / "onboarding.json"
    if not path.exists():
        raise FileNotFoundError(f"onboarding.json não encontrado para '{client_id}' em {path}")
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ImobOne Pipeline Runner",
    description="Runner autônomo do pipeline de setup de clientes",
    version="1.0.0",
)


class StartRequest(BaseModel):
    client_id: str
    skip_agents: list[str] = []
    secret: str = ""


class StartJsonRequest(BaseModel):
    client_id: str
    onboarding: dict[str, Any]
    skip_agents: list[str] = []
    secret: str = ""


def _check_secret(provided: str):
    if RUNNER_SECRET and provided != RUNNER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health():
    return {"status": "ok", "service": "imob-pipeline-runner", "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/pipeline/start")
async def start_pipeline(req: StartRequest, background_tasks: BackgroundTasks):
    """
    Dispara o pipeline para um cliente cujo onboarding.json já está em disco.
    Retorna imediatamente com o status do job.
    """
    _check_secret(req.secret)

    client_id = req.client_id.strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id não pode ser vazio")

    # Impede execução dupla
    job = _load_job(client_id)
    if job and job.get("status") == "running":
        return JSONResponse(
            status_code=409,
            content={"error": f"Pipeline de '{client_id}' já está em execução.", "job": job},
        )

    # Carrega onboarding do disco
    try:
        onboarding = _load_onboarding(client_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao ler onboarding.json: {e}")

    _set_job(client_id, status="queued", skip_agents=req.skip_agents)

    # Notifica início
    _notify(
        f"🚀 Pipeline de *{client_id}* iniciado.\n"
        f"Você será notificado quando terminar.\n"
        f"Agentes pulados: {req.skip_agents or 'nenhum'}"
    )

    background_tasks.add_task(_run_pipeline_job, client_id, onboarding, req.skip_agents)

    log.info("Pipeline de '%s' enfileirado (skip=%s)", client_id, req.skip_agents)
    return {
        "status": "queued",
        "client_id": client_id,
        "message": f"Pipeline iniciado. Você receberá notificação WhatsApp no número {OPERATOR_NUMBER}.",
        "status_url": f"/pipeline/status/{client_id}",
    }


@app.post("/pipeline/start-json")
async def start_pipeline_json(req: StartJsonRequest, background_tasks: BackgroundTasks):
    """
    Dispara o pipeline com o onboarding inline no body (útil para integração com formulários).
    """
    _check_secret(req.secret)

    client_id = req.client_id.strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id não pode ser vazio")

    job = _load_job(client_id)
    if job and job.get("status") == "running":
        return JSONResponse(
            status_code=409,
            content={"error": f"Pipeline de '{client_id}' já está em execução.", "job": job},
        )

    # Salva o onboarding no disco para o pipeline encontrar
    onboarding_path = CLIENTS_DIR / client_id / "onboarding.json"
    onboarding_path.parent.mkdir(parents=True, exist_ok=True)
    onboarding_path.write_text(json.dumps(req.onboarding, ensure_ascii=False, indent=2))
    log.info("onboarding.json salvo em %s", onboarding_path)

    _set_job(client_id, status="queued", skip_agents=req.skip_agents)
    _notify(
        f"🚀 Pipeline de *{client_id}* iniciado via API.\n"
        f"Você será notificado quando terminar."
    )

    background_tasks.add_task(_run_pipeline_job, client_id, req.onboarding, req.skip_agents)

    return {
        "status": "queued",
        "client_id": client_id,
        "message": f"Pipeline iniciado. Notificação será enviada para {OPERATOR_NUMBER}.",
        "status_url": f"/pipeline/status/{client_id}",
    }


@app.get("/pipeline/status/{client_id}")
def get_status(client_id: str):
    """Retorna o status atual do pipeline para o client_id."""
    job = _load_job(client_id)
    if not job:
        # Tenta ler o relatório mais recente se existir
        report = _read_report(client_id)
        if report:
            return {"client_id": client_id, "status": report.get("deploy_status", "unknown"), "report": report}
        raise HTTPException(status_code=404, detail=f"Nenhum job encontrado para '{client_id}'")
    return job


@app.get("/pipeline/jobs")
def list_jobs():
    """Lista todos os jobs conhecidos (em memória + disco)."""
    jobs = []
    for client_dir in CLIENTS_DIR.iterdir():
        if client_dir.is_dir():
            job = _load_job(client_dir.name)
            if job:
                jobs.append(job)
    return {"jobs": sorted(jobs, key=lambda j: j.get("updated_at", ""), reverse=True)}


# ---------------------------------------------------------------------------
# Relatórios executivos — ICP: Dono da imobiliária / construtora
# ---------------------------------------------------------------------------

def _get_report_engine():
    """Importa report_engine lazily para não quebrar se não estiver instalado."""
    try:
        if str(BASE_DIR) not in sys.path:
    