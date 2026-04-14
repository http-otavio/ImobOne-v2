"""
admin_api.py — Painel Administrativo ImobOne
Porta 8004 | FastAPI + Supabase Auth JWT pass-through

Arquitetura de segurança:
  - Toda autenticação via Supabase Auth (JWT)
  - FastAPI nunca duplica lógica de RLS — apenas passa JWT para o Supabase
  - RLS é a única fonte de verdade para o que cada perfil pode ver/fazer
  - Todas as queries de dados passam pelo client criado com o JWT do usuário
  - Operações de auditoria (audit_reads) e gestão (session revocation) usam service_role
  - Middleware registra audit_reads após cada resposta bem-sucedida
  - Background task polling a cada 30s detecta anomalias não resolvidas e:
      1. Revoga sessão do usuário via Supabase Admin API
      2. Envia alerta WhatsApp ao dono

Endpoints:
  POST /admin/auth/session        → valida JWT e retorna perfil
  GET  /admin/leads               → lista leads (paginado, max 20, filtros)
  GET  /admin/leads/{phone}       → detalhes de um lead
  GET  /admin/leads/{phone}/conversation → histórico de conversa
  POST /admin/leads/{phone}/takeover    → corretor assume conversa
  POST /admin/leads/{phone}/takeover/return → devolve para Sofia
  POST /admin/leads/{phone}/messages    → corretor envia mensagem pelo painel
  GET  /admin/alerts              → alertas de anomalia pendentes (dono only)
  PATCH /admin/alerts/{id}/resolve → resolve alerta (dono only)
  GET  /admin/reports/weekly      → relatório semanal (dono only)
  GET  /admin/profiles            → lista perfis (dono only)
  POST /admin/profiles            → cria perfil (dono only, via service_role)
  GET  /health                    → liveness check
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Literal

import httpx
import redis as _redis_sync
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from supabase import create_client, Client as SupabaseClient

log = logging.getLogger("admin_api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ─── Configuração ─────────────────────────────────────────────────────────────

SUPABASE_URL          = os.environ["SUPABASE_URL"]
# SUPABASE_SERVICE_KEY: chave service_role (bypassa RLS — usada apenas internamente)
# Obter em: Supabase Dashboard → Settings → API → service_role secret
# NUNCA usar a chave anon aqui — sem service_role, audit e admin ops não funcionam
SUPABASE_SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")
if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY não configurada. Configure em /opt/webhook.env antes de iniciar o admin API.")
# Valida que não é a chave anon por acidente
import base64 as _b64
try:
    _payload = _b64.b64decode(SUPABASE_SERVICE_KEY.split(".")[1] + "==")
    if b'"anon"' in _payload or b'"role":"anon"' in _payload:
        raise RuntimeError(
            "SUPABASE_SERVICE_KEY contém a chave anon. Use a chave service_role. "
            "Obtenha em: Supabase Dashboard → Settings → API → service_role secret"
        )
except (IndexError, Exception) as _e:
    if "anon" in str(_e).lower():
        raise
EVOLUTION_API_URL     = os.getenv("EVOLUTION_API_URL", "https://api.otaviolabs.com")
EVOLUTION_API_KEY     = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE    = os.getenv("EVOLUTION_INSTANCE", "devlabz")
OPERATOR_NUMBER       = os.getenv("OPERATOR_NUMBER", "")
ADMIN_PORT            = int(os.getenv("ADMIN_PORT", "8004"))
REDIS_URL             = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")

# CORS: apenas o domínio fixo do painel — sem wildcards em produção
ALLOWED_ORIGINS = [
    "https://app.imobone.com.br",
    "http://localhost:3000",  # dev local Next.js
]

# ─── Supabase service_role client (auditoria + admin ops) ─────────────────────

_sb_service: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _sb_user(access_token: str, refresh_token: str = "") -> SupabaseClient:
    """
    Cria client Supabase com o JWT do usuário — todas as queries passam por RLS.
    Nunca use _sb_service para queries de dados de negócio.
    """
    client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    client.auth.set_session(access_token, refresh_token or access_token)
    return client


# ─── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app_: FastAPI):
    """Startup + shutdown via lifespan (substitui on_event deprecated)."""
    global _bg_task, _redis_client
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(REDIS_URL, decode_responses=False)
        await _redis_client.ping()
        log.info("Redis conectado em %s", REDIS_URL)
    except Exception as e:
        log.warning("Redis indisponível — cache de takeover desabilitado: %s", e)
        _redis_client = None
    _bg_task = asyncio.create_task(_poll_anomalies())
    log.info("Admin API iniciada. Anomaly polling ativo (30s interval).")

    yield  # ── aplicação rodando ──

    if _bg_task:
        _bg_task.cancel()
    if _redis_client:
        await _redis_client.aclose()
    log.info("Admin API encerrada.")


app = FastAPI(
    title="ImobOne Admin API",
    version="1.0.0",
    docs_url=None,   # desabilitado em produção — segurança
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Refresh-Token"],
    expose_headers=["X-Request-Id"],
)


# ─── Modelos Pydantic ─────────────────────────────────────────────────────────

class TakeoverStartRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class TakeoverReturnRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class MessageSendRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)

    @field_validator("text")
    @classmethod
    def sanitize_text(cls, v: str) -> str:
        # Remove caracteres de controle exceto newline/tab
        cleaned = "".join(c for c in v if c >= " " or c in "\n\t")
        return cleaned.strip()


class AlertResolveRequest(BaseModel):
    note: str = Field(default="", max_length=1000)


class ProfileCreateRequest(BaseModel):
    email: str = Field(..., max_length=254)
    role: Literal["dono", "corretor"]
    client_id: str = Field(..., min_length=1, max_length=100)
    corretor_phone: str = Field(default="", max_length=20)
    password: str = Field(..., min_length=12, max_length=128)

    @field_validator("corretor_phone")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        return v.lstrip("+").strip()


# ─── Auth dependency ──────────────────────────────────────────────────────────

class AuthContext:
    """Contexto de autenticação resolvido a partir do JWT."""
    def __init__(
        self,
        user_id: str,
        role: str,
        client_id: str,
        access_token: str,
        refresh_token: str,
        corretor_phone: str,
        ip: str,
        user_agent: str,
    ):
        self.user_id       = user_id
        self.role          = role
        self.client_id     = client_id
        self.access_token  = access_token
        self.refresh_token = refresh_token
        self.corretor_phone = corretor_phone
        self.ip            = ip
        self.user_agent    = user_agent

    @property
    def sb(self) -> SupabaseClient:
        """Client Supabase com JWT do usuário — queries passam por RLS."""
        return _sb_user(self.access_token, self.refresh_token)


async def _get_auth(
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
    x_refresh_token: str = Header(default="", alias="X-Refresh-Token"),
) -> AuthContext:
    """
    Valida o JWT via Supabase Auth e busca o perfil do usuário.
    Rejeita se: token inválido, perfil não encontrado, conta inativa, MFA não completado.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header inválido.")

    access_token = authorization.removeprefix("Bearer ").strip()
    if not access_token:
        raise HTTPException(status_code=401, detail="Token ausente.")

    # Valida JWT com Supabase — usa service_role para verificar sem criar sessão
    try:
        user_response = _sb_service.auth.get_user(access_token)
        user = user_response.user
        if not user:
            raise HTTPException(status_code=401, detail="Token inválido ou expirado.")
    except Exception as e:
        log.warning("Falha na validação de JWT: %s", e)
        raise HTTPException(status_code=401, detail="Token inválido ou expirado.") from e

    # Busca perfil no Supabase (service_role — profiles.SELECT para service_role é permitido)
    try:
        profile_res = _sb_service.table("profiles") \
            .select("id, role, client_id, corretor_phone, mfa_enrolled, is_active") \
            .eq("id", user.id) \
            .single() \
            .execute()
        profile = profile_res.data
    except Exception as e:
        log.warning("Perfil não encontrado para user_id=%s: %s", user.id, e)
        raise HTTPException(status_code=403, detail="Perfil não encontrado.") from e

    if not profile:
        raise HTTPException(status_code=403, detail="Perfil não encontrado.")
    if not profile.get("is_active"):
        raise HTTPException(status_code=403, detail="Conta inativa.")
    if not profile.get("mfa_enrolled"):
        raise HTTPException(status_code=403, detail="MFA obrigatório. Complete o enrollment antes de acessar o painel.")

    ip = request.headers.get("CF-Connecting-IP") or \
         request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or \
         (request.client.host if request.client else "unknown")

    return AuthContext(
        user_id        = str(user.id),
        role           = profile["role"],
        client_id      = profile["client_id"],
        access_token   = access_token,
        refresh_token  = x_refresh_token,
        corretor_phone = profile.get("corretor_phone", ""),
        ip             = ip,
        user_agent     = request.headers.get("User-Agent", ""),
    )


def _require_dono(auth: AuthContext = Depends(_get_auth)) -> AuthContext:
    if auth.role != "dono":
        raise HTTPException(status_code=403, detail="Acesso exclusivo para donos.")
    return auth


# ─── Audit middleware ─────────────────────────────────────────────────────────

_AUDITABLE_ACTIONS: dict[tuple[str, str], str] = {
    # (method, path_prefix): action
    ("GET",   "/admin/leads"):               "read_lead",
    ("GET",   "/admin/leads/*/conversation"): "read_conversation",
    ("GET",   "/admin/reports"):             "export_leads",
    ("POST",  "/admin/leads/*/takeover"):    "takeover_start",
    ("POST",  "/admin/leads/*/messages"):    "message_sent",
}


def _resolve_audit_action(method: str, path: str) -> str | None:
    """Resolve a ação de auditoria a partir do método e path da request."""
    path_parts = path.split("/")
    if method == "GET" and len(path_parts) >= 4 and path_parts[3] == "leads":
        if len(path_parts) >= 6 and path_parts[5] == "conversation":
            return "read_conversation"
        if len(path_parts) == 5:
            return "read_lead"
        if len(path_parts) == 4:
            return "read_lead"
    if method == "GET" and "/admin/reports" in path:
        return "export_leads"
    if method == "POST" and "/takeover" in path and "/return" not in path:
        return "takeover_start"
    if method == "POST" and path.endswith("/messages"):
        return "message_sent"
    return None


async def _write_audit(
    user_id: str,
    client_id: str,
    action: str,
    lead_phone: str,
    ip: str,
    user_agent: str,
) -> None:
    """Grava audit_reads via service_role (não passa por RLS de insert)."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: (
            _sb_service.table("audit_reads")
              .insert({
                  "user_id":    user_id,
                  "client_id":  client_id,
                  "action":     action,
                  "lead_phone": lead_phone,
                  "ip_address": ip,
                  "user_agent": user_agent,
              })
              .execute()
        ))
    except Exception as e:
        log.warning("Falha ao gravar audit_reads: %s", e)


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """
    Middleware de auditoria: após cada resposta 2xx bem-sucedida,
    grava em audit_reads via service_role.
    Extrai lead_phone do path quando disponível.
    """
    response = await call_next(request)

    # Só audita respostas bem-sucedidas (2xx)
    if response.status_code < 200 or response.status_code >= 300:
        return response

    action = _resolve_audit_action(request.method, request.url.path)
    if not action:
        return response

    # Extrai lead_phone do path: /admin/leads/{phone}/...
    path_parts = request.url.path.split("/")
    lead_phone = ""
    if len(path_parts) >= 5 and path_parts[3] == "leads":
        lead_phone = path_parts[4]

    # Extrai contexto do request.state (setado pelo auth dependency)
    user_id   = getattr(request.state, "audit_user_id", "")
    client_id = getattr(request.state, "audit_client_id", "")
    ip        = getattr(request.state, "audit_ip", "")
    ua        = request.headers.get("User-Agent", "")

    if user_id:
        asyncio.create_task(_write_audit(user_id, client_id, action, lead_phone, ip, ua))

    return response


# Helper para setar estado de auditoria no request
def _set_audit_state(request: Request, auth: AuthContext) -> None:
    request.state.audit_user_id  = auth.user_id
    request.state.audit_client_id = auth.client_id
    request.state.audit_ip       = auth.ip


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/admin/auth/session")
async def get_session(auth: AuthContext = Depends(_get_auth)):
    """Valida JWT e retorna perfil do usuário. Usado pelo frontend na inicialização."""
    return {
        "user_id":        auth.user_id,
        "role":           auth.role,
        "client_id":      auth.client_id,
        "corretor_phone": auth.corretor_phone,
    }


# ─── Leads ────────────────────────────────────────────────────────────────────

@app.get("/admin/leads")
async def list_leads(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    status: str = "",
    search: str = "",
    auth: AuthContext = Depends(_get_auth),
):
    """
    Lista leads paginados (max 20 por página).
    Dono vê todos do client_id. Corretor vê apenas seus leads atribuídos.
    RLS garante isso — o client Supabase usa o JWT do usuário.
    """
    _set_audit_state(request, auth)

    # Força max 20 — defesa contra bulk read via parâmetro manipulado
    per_page = min(per_page, 20)
    offset   = (max(page, 1) - 1) * per_page

    sb = auth.sb

    try:
        query = sb.table("leads").select(
            "lead_phone, lead_name, status, intention_score, "
            "visita_agendada, human_takeover, assigned_corretor_id, "
            "ultima_interacao, created_at, origem, canal_entrada, "
            "pipeline_value_brl, descartado, objections_detected"
        ).order("ultima_interacao", desc=True).range(offset, offset + per_page - 1)

        if status:
            query = query.eq("status", status)

        if search:
            # Busca por telefone ou nome (ilike)
            query = query.or_(f"lead_phone.ilike.%{search}%,lead_name.ilike.%{search}%")

        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: query.execute()
        )
        return {"leads": result.data, "page": page, "per_page": per_page}

    except Exception as e:
        log.error("Erro ao buscar leads: %s", e)
        raise HTTPException(status_code=500, detail="Erro ao buscar leads.") from e


@app.get("/admin/leads/{lead_phone}")
async def get_lead(
    request: Request,
    lead_phone: str,
    auth: AuthContext = Depends(_get_auth),
):
    """Detalhes completos de um lead. RLS garante acesso apenas a leads permitidos."""
    _set_audit_state(request, auth)

    # Sanitiza: telefone só tem dígitos e '+'
    if not all(c.isdigit() or c == "+" for c in lead_phone):
        raise HTTPException(status_code=400, detail="Formato de telefone inválido.")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: (
                auth.sb.table("leads")
                       .select("*")
                       .eq("lead_phone", lead_phone)
                       .single()
                       .execute()
            )
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Lead não encontrado.")
        return result.data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erro ao buscar lead.") from e


@app.get("/admin/leads/{lead_phone}/conversation")
async def get_conversation(
    request: Request,
    lead_phone: str,
    limit: int = 50,
    auth: AuthContext = Depends(_get_auth),
):
    """
    Histórico de conversa de um lead. Paginado.
    RLS em conversas garante que corretor vê apenas seus leads atribuídos.
    """
    _set_audit_state(request, auth)

    if not all(c.isdigit() or c == "+" for c in lead_phone):
        raise HTTPException(status_code=400, detail="Formato de telefone inválido.")

    # Força limite máximo — defesa contra dump de histórico completo
    limit = min(limit, 100)

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: (
                auth.sb.table("conversas")
                       .select("role, content, media_type, created_at")
                       .eq("lead_phone", lead_phone)
                       .order("created_at", desc=False)
                       .limit(limit)
                       .execute()
            )
        )
        return {"conversation": result.data, "lead_phone": lead_phone}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erro ao buscar conversa.") from e


# ─── Takeover ─────────────────────────────────────────────────────────────────

@app.post("/admin/leads/{lead_phone}/takeover")
async def start_takeover(
    request: Request,
    lead_phone: str,
    body: TakeoverStartRequest,
    auth: AuthContext = Depends(_get_auth),
):
    """
    Corretor assume a conversa com o lead pelo painel.
    Seta human_takeover=true + takeover_by + takeover_at no lead.
    Sofia para de responder automaticamente para este lead.
    Registra em takeover_audit via service_role.
    """
    if auth.role not in ("corretor", "dono"):
        raise HTTPException(status_code=403, detail="Apenas corretores e donos podem assumir conversas.")

    if not all(c.isdigit() or c == "+" for c in lead_phone):
        raise HTTPException(status_code=400, detail="Formato de telefone inválido.")

    now_iso = datetime.now(timezone.utc).isoformat()
    loop    = asyncio.get_event_loop()

    try:
        # Atualiza lead — passa por RLS (corretor só pode atualizar leads atribuídos)
        await loop.run_in_executor(None, lambda: (
            auth.sb.table("leads")
                   .update({
                       "human_takeover": True,
                       "takeover_by":    auth.user_id,
                       "takeover_at":    now_iso,
                   })
                   .eq("lead_phone", lead_phone)
                   .execute()
        ))

        # Audit: service_role (não passa por RLS de insert)
        await loop.run_in_executor(None, lambda: (
            _sb_service.table("takeover_audit")
                       .insert({
                           "lead_phone":  lead_phone,
                           "actor_id":    auth.user_id,
                           "client_id":   auth.client_id,
                           "action":      "takeover_start",
                           "message_text": body.reason or None,
                           "ip_address":  auth.ip,
                           "user_agent":  auth.user_agent,
                       })
                       .execute()
        ))

        # Invalida cache Redis para que o webhook reconheça imediatamente
        await _invalidate_takeover_cache(lead_phone, active=True)

        log.info("Takeover iniciado: lead=%s, actor=%s", lead_phone, auth.user_id)
        return {"success": True, "lead_phone": lead_phone, "takeover_at": now_iso}

    except Exception as e:
        log.error("Erro ao iniciar takeover: %s", e)
        raise HTTPException(status_code=500, detail="Erro ao iniciar takeover.") from e


@app.post("/admin/leads/{lead_phone}/takeover/return")
async def return_takeover(
    request: Request,
    lead_phone: str,
    body: TakeoverReturnRequest,
    auth: AuthContext = Depends(_get_auth),
):
    """
    Devolve a conversa para Sofia. Seta human_takeover=false + takeover_returned_at.
    Sofia retoma automaticamente ao receber próxima mensagem do lead.
    """
    if not all(c.isdigit() or c == "+" for c in lead_phone):
        raise HTTPException(status_code=400, detail="Formato de telefone inválido.")

    now_iso = datetime.now(timezone.utc).isoformat()
    loop    = asyncio.get_event_loop()

    try:
        await loop.run_in_executor(None, lambda: (
            auth.sb.table("leads")
                   .update({
                       "human_takeover":       False,
                       "takeover_returned_at": now_iso,
                   })
                   .eq("lead_phone", lead_phone)
                   .execute()
        ))

        await loop.run_in_executor(None, lambda: (
            _sb_service.table("takeover_audit")
                       .insert({
                           "lead_phone":  lead_phone,
                           "actor_id":    auth.user_id,
                           "client_id":   auth.client_id,
                           "action":      "takeover_return",
                           "message_text": body.note or None,
                           "ip_address":  auth.ip,
                           "user_agent":  auth.user_agent,
                       })
                       .execute()
        ))

        # Invalida cache Redis para que o webhook retome automaticamente
        await _invalidate_takeover_cache(lead_phone, active=False)

        log.info("Takeover retornado: lead=%s, actor=%s", lead_phone, auth.user_id)
        return {"success": True, "lead_phone": lead_phone, "returned_at": now_iso}

    except Exception as e:
        raise HTTPException(status_code=500, detail="Erro ao devolver conversa.") from e


@app.post("/admin/leads/{lead_phone}/messages")
async def send_message(
    request: Request,
    lead_phone: str,
    body: MessageSendRequest,
    auth: AuthContext = Depends(_get_auth),
):
    """
    Corretor envia mensagem ao lead pelo painel (modo takeover).
    Verifica que human_takeover=true e assigned_corretor_id == auth.user_id.
    Encaminha para Evolution API e persiste em conversas.
    Registra em takeover_audit.
    """
    if not all(c.isdigit() or c == "+" for c in lead_phone):
        raise HTTPException(status_code=400, detail="Formato de telefone inválido.")

    loop = asyncio.get_event_loop()

    # Verifica estado de takeover ativo (usando client com JWT — RLS filtra)
    try:
        lead_res = await loop.run_in_executor(None, lambda: (
            auth.sb.table("leads")
                   .select("human_takeover, assigned_corretor_id")
                   .eq("lead_phone", lead_phone)
                   .single()
                   .execute()
        ))
        lead = lead_res.data
    except Exception:
        raise HTTPException(status_code=404, detail="Lead não encontrado.")

    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado.")
    if not lead.get("human_takeover"):
        raise HTTPException(status_code=400, detail="Takeover não está ativo para este lead.")
    if lead.get("assigned_corretor_id") != auth.user_id and auth.role != "dono":
        raise HTTPException(status_code=403, detail="Este lead não está atribuído a você.")

    # Envia mensagem via Evolution API
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            resp = await client.post(
                f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"number": lead_phone, "text": body.text},
            )
        if resp.status_code not in (200, 201):
            log.error("Evolution API rejeitou mensagem: %s %s", resp.status_code, resp.text[:200])
            raise HTTPException(status_code=502, detail="Falha ao enviar mensagem pelo WhatsApp.")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Erro de conexão com Evolution API: {e}") from e

    now_iso = datetime.now(timezone.utc).isoformat()

    # Persiste em conversas via service_role (webhook também usa service_role para conversas)
    await loop.run_in_executor(None, lambda: (
        _sb_service.table("conversas")
                   .insert({
                       "client_id":  auth.client_id,
                       "lead_phone": lead_phone,
                       "role":       "assistant",
                       "content":    f"[CORRETOR] {body.text}",
                       "media_type": "text",
                       "created_at": now_iso,
                   })
                   .execute()
    ))

    # Audit
    await loop.run_in_executor(None, lambda: (
        _sb_service.table("takeover_audit")
                   .insert({
                       "lead_phone":   lead_phone,
                       "actor_id":     auth.user_id,
                       "client_id":    auth.client_id,
                       "action":       "message_sent",
                       "message_text": body.text[:500],
                       "ip_address":   auth.ip,
                       "user_agent":   auth.user_agent,
                   })
                   .execute()
    ))

    return {"success": True, "sent_at": now_iso}


# ─── Alertas de anomalia (dono only) ─────────────────────────────────────────

@app.get("/admin/alerts")
async def list_alerts(auth: AuthContext = Depends(_require_dono)):
    """Lista alertas de anomalia não resolvidos para o client_id do dono."""
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: (
                auth.sb.table("anomaly_alerts")
                       .select("*")
                       .is_("resolved_at", None)
                       .order("created_at", desc=True)
                       .limit(50)
                       .execute()
            )
        )
        return {"alerts": result.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erro ao buscar alertas.") from e


@app.patch("/admin/alerts/{alert_id}/resolve")
async def resolve_alert(
    alert_id: str,
    body: AlertResolveRequest,
    auth: AuthContext = Depends(_require_dono),
):
    """
    Dono resolve um alerta de anomalia.
    Se session_revoked=false, revoga a sessão do usuário suspeito via Supabase Admin API.
    """
    if not alert_id.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="ID de alerta inválido.")

    loop = asyncio.get_event_loop()

    # Busca o alerta (via JWT do dono — RLS filtra para o client_id correto)
    try:
        alert_res = await loop.run_in_executor(None, lambda: (
            auth.sb.table("anomaly_alerts")
                   .select("*")
                   .eq("id", alert_id)
                   .single()
                   .execute()
        ))
        alert = alert_res.data
    except Exception:
        raise HTTPException(status_code=404, detail="Alerta não encontrado.")

    if not alert:
        raise HTTPException(status_code=404, detail="Alerta não encontrado.")

    now_iso = datetime.now(timezone.utc).isoformat()

    # Revoga sessão se ainda não foi feito
    if not alert.get("session_revoked"):
        try:
            # Supabase Admin API — revoga todas as sessões do usuário suspeito
            await loop.run_in_executor(None, lambda: (
                _sb_service.auth.admin.delete_user_sessions(alert["user_id"])
            ))
            log.info("Sessões revogadas para user_id=%s (alert=%s)", alert["user_id"], alert_id)
        except Exception as e:
            log.error("Falha ao revogar sessão para user=%s: %s", alert["user_id"], e)

    # Marca como resolvido via service_role
    await loop.run_in_executor(None, lambda: (
        _sb_service.table("anomaly_alerts")
                   .update({
                       "resolved_at":     now_iso,
                       "resolved_by":     auth.user_id,
                       "resolution_note": body.note or None,
                       "session_revoked": True,
                   })
                   .eq("id", alert_id)
                   .execute()
    ))

    return {"success": True, "resolved_at": now_iso}


# ─── Perfis (dono only) ───────────────────────────────────────────────────────

@app.get("/admin/profiles")
async def list_profiles(auth: AuthContext = Depends(_require_dono)):
    """Lista perfis do client_id do dono."""
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: (
                auth.sb.table("profiles")
                       .select("id, role, corretor_phone, mfa_enrolled, is_active, created_at")
                       .execute()
            )
        )
        return {"profiles": result.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erro ao buscar perfis.") from e


@app.post("/admin/profiles", status_code=201)
async def create_profile(
    body: ProfileCreateRequest,
    auth: AuthContext = Depends(_require_dono),
):
    """
    Cria usuário no Supabase Auth + perfil em profiles.
    Usa service_role — única operação do painel que usa service_role diretamente.
    Dono só pode criar perfis para o seu próprio client_id.
    """
    loop = asyncio.get_event_loop()

    # Força client_id = client_id do dono — impede escalonamento horizontal
    if body.client_id != auth.client_id:
        raise HTTPException(status_code=403, detail="Não é possível criar perfis em outro client_id.")

    try:
        # Cria usuário no Supabase Auth
        user_res = await loop.run_in_executor(None, lambda: (
            _sb_service.auth.admin.create_user({
                "email":    body.email,
                "password": body.password,
                "email_confirm": True,
            })
        ))
        new_user = user_res.user
    except Exception as e:
        log.error("Falha ao criar usuário Auth: %s", e)
        raise HTTPException(status_code=400, detail=f"Erro ao criar usuário: {e}") from e

    try:
        # Cria perfil (service_role bypassa a policy de INSERT bloqueada para JWT)
        await loop.run_in_executor(None, lambda: (
            _sb_service.table("profiles")
                       .insert({
                           "id":             str(new_user.id),
                           "role":           body.role,
                           "client_id":      body.client_id,
                           "corretor_phone": body.corretor_phone,
                           "mfa_enrolled":   False,
                           "is_active":      True,
                       })
                       .execute()
        ))
    except Exception as e:
        # Rollback: deleta usuário Auth criado
        try:
            await loop.run_in_executor(None, lambda: (
                _sb_service.auth.admin.delete_user(str(new_user.id))
            ))
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Erro ao criar perfil: {e}") from e

    log.info(
        "Perfil criado: user_id=%s role=%s client_id=%s by=%s",
        new_user.id, body.role, body.client_id, auth.user_id
    )
    return {
        "user_id":   str(new_user.id),
        "email":     body.email,
        "role":      body.role,
        "client_id": body.client_id,
    }


# ─── Relatórios (dono only) ───────────────────────────────────────────────────

@app.get("/admin/reports/weekly")
async def weekly_report(
    request: Request,
    auth: AuthContext = Depends(_require_dono),
):
    """Delega para o report_engine — retorna métricas semanais."""
    _set_audit_state(request, auth)
    try:
        from report_engine import compute_weekly_metrics
        metrics = await asyncio.get_event_loop().run_in_executor(
            None, lambda: compute_weekly_metrics(auth.client_id)
        )
        return metrics
    except ImportError:
        raise HTTPException(status_code=501, detail="report_engine não disponível.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar relatório: {e}") from e


# ─── Background task: polling de anomalias + resposta ativa ──────────────────

_anomaly_poll_running = False


async def _poll_anomalies() -> None:
    """
    Polling a cada 30s: busca alertas não resolvidos via v_pending_anomaly_alerts.
    Para cada alerta com session_revoked=false:
      1. Revoga sessão via Supabase Admin API
      2. Envia WhatsApp ao operador
      3. Marca session_revoked=true (não resolve — dono resolve manualmente)
    """
    global _anomaly_poll_running
    if _anomaly_poll_running:
        return
    _anomaly_poll_running = True

    while True:
        try:
            await asyncio.sleep(30)
            await _process_unrevoked_alerts()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Erro no polling de anomalias: %s", e)

    _anomaly_poll_running = False


async def _process_unrevoked_alerts() -> None:
    """Processa alertas com session_revoked=false — revoca + notifica."""
    try:
        loop = asyncio.get_event_loop()

        result = await loop.run_in_executor(None, lambda: (
            _sb_service.from_("v_pending_anomaly_alerts")
                       .select("*")
                       .eq("session_revoked", False)
                       .limit(10)
                       .execute()
        ))
        alerts = result.data or []
    except Exception as e:
        log.warning("Falha ao buscar alertas pendentes: %s", e)
        return

    for alert in alerts:
        alert_id = alert.get("id")
        user_id  = alert.get("user_id")
        atype    = alert.get("alert_type", "unknown")

        # Revoga sessão
        try:
            await asyncio.get_event_loop().run_in_executor(None, lambda: (
                _sb_service.auth.admin.delete_user_sessions(user_id)
            ))
            log.warning(
                "ANOMALIA DETECTADA — sessão revogada: user=%s type=%s alert=%s",
                user_id, atype, alert_id
            )
        except Exception as e:
            log.error("Falha ao revogar sessão %s: %s", user_id, e)

        # Marca session_revoked=true
        try:
            await asyncio.get_event_loop().run_in_executor(None, lambda: (
                _sb_service.table("anomaly_alerts")
                           .update({"session_revoked": True})
                           .eq("id", alert_id)
                           .execute()
            ))
        except Exception as e:
            log.error("Falha ao marcar session_revoked: %s", e)

        # Notifica operador via WhatsApp
        if OPERATOR_NUMBER and EVOLUTION_API_KEY:
            phone    = alert.get("user_phone", "desconhecido")
            details  = json.dumps(alert.get("details", {}), ensure_ascii=False)[:300]
            msg = (
                f"🚨 *ALERTA DE SEGURANÇA — ImobOne Admin*\n\n"
                f"⚠️ Tipo: {atype}\n"
                f"👤 Usuário: {phone}\n"
                f"🔐 Sessão: *REVOGADA AUTOMATICAMENTE*\n"
                f"📋 Detalhes: {details}\n\n"
                f"Acesse o painel para resolver o alerta."
            )
            try:
                async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                    await client.post(
                        f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}",
                        headers={"apikey": EVOLUTION_API_KEY},
                        json={"number": OPERATOR_NUMBER, "text": msg},
                    )
            except Exception as e:
                log.error("Falha ao enviar alerta WhatsApp: %s", e)


# ─── Wiring: webhook verifica human_takeover antes de responder ───────────────

# Nota: whatsapp_webhook.py deve verificar human_takeover antes de chamar o LLM.
# A verificação é feita consultando Redis key "human_takeover:{sender}" (TTL 24h)
# setada por este admin_api ao iniciar o takeover.
# Alternativa se Redis não estiver disponível: consultar Supabase leads diretamente.
# Esta integração é responsabilidade do whatsapp_webhook.py (próximo passo).


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

_bg_task:      asyncio.Task | None = None
_redis_client: object | None      = None   # redis.asyncio.Redis


async def _invalidate_takeover_cache(lead_phone: str, active: bool) -> None:
    """Seta Redis key human_takeover:{phone} para sincronizar com o webhook."""
    global _redis_client
    if _redis_client is None:
        return
    try:
        await _redis_client.set(
            f"human_takeover:{lead_phone}",
            "1" if active else "0",
            ex=86400
        )
    except Exception as e:
        log.warning("Falha ao invalidar cache takeover no Redis: %s", e)




# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "admin_api:app",
        host="0.0.0.0",
        port=ADMIN_PORT,
        reload=False,
        log_level="info",
    )
