"""
tools/crm_webhook.py — Bridge entre o sistema de agentes e as integrações CRM.

Responsabilidade:
  Ponto único de entrada para qualquer operação de CRM a partir do webhook
  WhatsApp, do follow-up engine e dos agentes.

Estratégia de compatibilidade (duas gerações de configuração):
  ┌─ onboarding.json com seção "crm" (provider + api_token) ──────────────┐
  │  → Usa CRMRouter (novo) com adapter tipado por provider               │
  │  → Suporta: C2S, CV CRM, Pipedrive, RD Station, Jetimob, Kenlo        │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ onboarding.json com crm_webhook_url (legado) ────────────────────────┐
  │  → Faz POST genérico para a URL configurada                            │
  │  → Mantém compatibilidade com clientes configurados antes dos adapters │
  └───────────────────────────────────────────────────────────────────────┘

Uso no whatsapp_webhook.py:
    from tools.crm_webhook import push_lead_to_crm, update_lead_in_crm, add_note_to_crm

    result = await push_lead_to_crm(onboarding, lead_data, client_id="demo_01")
    if result["success"]:
        crm_id = result["external_id"]

Uso no followup_engine.py:
    from tools.crm_webhook import add_note_to_crm

    await add_note_to_crm(onboarding, crm_external_id, note_text, client_id)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .crm.base import LeadPayload, LeadProfile, LeadSource, LeadStatus
from .crm.router import CRMRouter

log = logging.getLogger(__name__)

TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Helpers de conversão
# ---------------------------------------------------------------------------

def _build_lead_payload_from_dict(data: dict) -> LeadPayload:
    """
    Converte dict de lead do Supabase/webhook para LeadPayload canônico.

    Aceita tanto campos de `leads` do Supabase quanto o formato interno dos agentes.
    """
    # Source
    source_raw = data.get("source", data.get("origem", "WhatsApp"))
    try:
        source = LeadSource(source_raw)
    except ValueError:
        source = LeadSource.WHATSAPP

    # Status
    status_raw = data.get("status", "novo")
    try:
        status = LeadStatus(status_raw)
    except ValueError:
        status = LeadStatus.NOVO

    # Profile
    profile_raw = data.get("profile", data.get("perfil", "indefinido"))
    try:
        profile = LeadProfile(profile_raw)
    except ValueError:
        profile = LeadProfile.INDEFINIDO

    return LeadPayload(
        phone=str(data.get("phone", data.get("telefone", ""))),
        name=data.get("name") or data.get("nome"),
        email=data.get("email"),
        source=source,
        status=status,
        profile=profile,
        intention_score=int(data.get("intention_score", data.get("score", 0))),
        budget=data.get("budget") or data.get("orcamento"),
        bedrooms=data.get("bedrooms") or data.get("quartos"),
        neighborhood=data.get("neighborhood") or data.get("bairro"),
        notes=data.get("notes") or data.get("briefing"),
        history_summary=data.get("history_summary") or data.get("resumo_historico"),
        external_id=data.get("crm_external_id") or data.get("external_id"),
    )


def _is_new_crm_config(onboarding: dict) -> bool:
    """Retorna True se o onboarding tem a seção 'crm' com 'provider' definido."""
    crm = onboarding.get("crm", {})
    return bool(crm.get("provider"))


async def _generic_webhook_post(
    url: str,
    token: str,
    payload: dict,
    client_id: str = "",
) -> dict:
    """
    Faz POST genérico para webhook CRM legado (crm_webhook_url).
    Mantém compatibilidade com configurações anteriores aos adapters.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code in (200, 201, 202):
            log.info("[crm_webhook] Generic POST OK: client=%s status=%d", client_id, resp.status_code)
            return {"success": True, "status_code": resp.status_code}
        log.warning(
            "[crm_webhook] Generic POST falhou: client=%s status=%d",
            client_id, resp.status_code,
        )
        return {"success": False, "error": f"HTTP {resp.status_code}", "status_code": resp.status_code}
    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout no webhook CRM genérico"}
    except Exception as exc:
        log.error("[crm_webhook] Generic POST exception: client=%s %s", client_id, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

async def push_lead_to_crm(
    onboarding: dict,
    lead_data: dict | LeadPayload,
    client_id: str = "",
) -> dict[str, Any]:
    """
    Cria lead no CRM do cliente.

    Args:
        onboarding: Dict do onboarding.json do cliente
        lead_data: Dict de dados do lead ou LeadPayload já construído
        client_id: ID do cliente para logging

    Returns:
        {"success": bool, "external_id": str | None, "provider": str, "error": str | None}
    """
    lead = lead_data if isinstance(lead_data, LeadPayload) else _build_lead_payload_from_dict(lead_data)

    # Nova integração via CRMRouter (provider tipado)
    if _is_new_crm_config(onboarding):
        try:
            router = CRMRouter.from_onboarding(onboarding, client_id=client_id)
            result = await router.create_lead(lead)
            return {
                "success": result.success,
                "external_id": result.external_id,
                "provider": router.provider_name,
                "error": result.error,
            }
        except ValueError as exc:
            log.error("[crm_webhook] Erro ao criar router: %s", exc)
            return {"success": False, "error": str(exc), "provider": "unknown"}

    # Fallback: webhook genérico legado
    crm_url = onboarding.get("crm_webhook_url", "")
    if not crm_url:
        log.debug("[crm_webhook] client=%s sem CRM configurado — skip", client_id)
        return {"success": True, "external_id": None, "provider": "none", "error": None}

    crm_token = onboarding.get("crm_webhook_token", "")
    payload = lead.to_dict() if isinstance(lead, LeadPayload) else lead_data
    result = await _generic_webhook_post(crm_url, crm_token, payload, client_id)
    return {**result, "provider": "generic_webhook", "external_id": None}


async def update_lead_in_crm(
    onboarding: dict,
    external_id: str,
    updates: dict,
    client_id: str = "",
) -> dict[str, Any]:
    """
    Atualiza campos do lead existente no CRM.

    Args:
        external_id: ID do lead no CRM (salvo após create_lead)
        updates: Dict com campos a atualizar (subconjunto de LeadPayload)
    """
    if not external_id:
        return {"success": False, "error": "external_id não fornecido"}

    if _is_new_crm_config(onboarding):
        try:
            router = CRMRouter.from_onboarding(onboarding, client_id=client_id)
            result = await router.update_lead(external_id, updates)
            return {
                "success": result.success,
                "external_id": result.external_id,
                "provider": router.provider_name,
                "error": result.error,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc), "provider": "unknown"}

    # Legado: POST para crm_webhook_url com updates
    crm_url = onboarding.get("crm_webhook_url", "")
    if not crm_url:
        return {"success": True, "external_id": external_id, "provider": "none"}

    crm_token = onboarding.get("crm_webhook_token", "")
    payload = {"id": external_id, "action": "update", **updates}
    result = await _generic_webhook_post(crm_url, crm_token, payload, client_id)
    return {**result, "provider": "generic_webhook", "external_id": external_id}


async def update_status_in_crm(
    onboarding: dict,
    external_id: str,
    status: str | LeadStatus,
    client_id: str = "",
) -> dict[str, Any]:
    """
    Atualiza status/estágio do lead no CRM.

    Args:
        status: LeadStatus enum ou string (ex: "visita_agendada")
    """
    if not external_id:
        return {"success": False, "error": "external_id não fornecido"}

    if isinstance(status, str):
        try:
            status = LeadStatus(status)
        except ValueError:
            status = LeadStatus.QUALIFICANDO

    if _is_new_crm_config(onboarding):
        try:
            router = CRMRouter.from_onboarding(onboarding, client_id=client_id)
            result = await router.update_status(external_id, status)
            return {
                "success": result.success,
                "external_id": result.external_id,
                "provider": router.provider_name,
                "error": result.error,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc), "provider": "unknown"}

    crm_url = onboarding.get("crm_webhook_url", "")
    if not crm_url:
        return {"success": True, "external_id": external_id, "provider": "none"}

    crm_token = onboarding.get("crm_webhook_token", "")
    payload = {"id": external_id, "action": "update_status", "status": status.value}
    result = await _generic_webhook_post(crm_url, crm_token, payload, client_id)
    return {**result, "provider": "generic_webhook", "external_id": external_id}


async def add_note_to_crm(
    onboarding: dict,
    external_id: str,
    note: str,
    client_id: str = "",
) -> dict[str, Any]:
    """
    Adiciona nota/briefing ao lead no CRM.

    Usado principalmente para enviar o briefing estratégico da Sofia
    quando o score do lead atinge o threshold de qualificação.
    """
    if not external_id:
        log.debug("[crm_webhook] add_note ignorado: external_id vazio (cliente=%s)", client_id)
        return {"success": True, "external_id": None, "provider": "none"}

    if _is_new_crm_config(onboarding):
        try:
            router = CRMRouter.from_onboarding(onboarding, client_id=client_id)
            result = await router.add_note(external_id, note)
            return {
                "success": result.success,
                "external_id": result.external_id,
                "provider": router.provider_name,
                "error": result.error,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc), "provider": "unknown"}

    crm_url = onboarding.get("crm_webhook_url", "")
    if not crm_url:
        return {"success": True, "external_id": external_id, "provider": "none"}

    crm_token = onboarding.get("crm_webhook_token", "")
    payload = {"id": external_id, "action": "add_note", "note": note}
    result = await _generic_webhook_post(crm_url, crm_token, payload, client_id)
    return {**result, "provider": "generic_webhook", "external_id": external_id}


async def assign_seller_in_crm(
    onboarding: dict,
    external_id: str,
    seller_phone: str,
    client_id: str = "",
) -> dict[str, Any]:
    """
    Atribui lead a um corretor via seller_mapping do onboarding.

    Args:
        seller_phone: Telefone do corretor (chave no seller_mapping do CRM config)
    """
    if not external_id:
        return {"success": False, "error": "external_id não fornecido"}

    if _is_new_crm_config(onboarding):
        try:
            router = CRMRouter.from_onboarding(onboarding, client_id=client_id)
            result = await router.assign_seller(external_id, seller_phone)
            return {
                "success": result.success,
                "external_id": result.external_id,
                "provider": router.provider_name,
                "error": result.error,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc), "provider": "unknown"}

    # Legado: sem suporte a assignment via generic webhook
    log.debug("[crm_webhook] assign_seller não suportado em modo generic_webhook")
    return {"success": True, "external_id": external_id, "provider": "generic_webhook"}


async def health_check_crm(onboarding: dict, client_id: str = "") -> dict[str, Any]:
    """
    Verifica conectividade e credenciais do CRM.

    Retorna:
        {"ok": bool, "provider": str, "error": str | None}
    """
    if _is_new_crm_config(onboarding):
        try:
            router = CRMRouter.from_onboarding(onboarding, client_id=client_id)
            ok = await router.health_check()
            return {"ok": ok, "provider": router.provider_name, "error": None if ok else "health_check falhou"}
        except ValueError as exc:
            return {"ok": False, "provider": "unknown", "error": str(exc)}

    crm_url = onboarding.get("crm_webhook_url", "")
    if not crm_url:
        return {"ok": True, "provider": "none", "error": None}  # sem CRM = OK para setup

    # Para webhook genérico, testa com GET se disponível ou considera OK
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                crm_url,
                headers={"Authorization": f"Bearer {onboarding.get('crm_webhook_token', '')}"},
            )
        ok = resp.status_code < 500
        return {"ok": ok, "provider": "generic_webhook", "error": None if ok else f"HTTP {resp.status_code}"}
    except Exception as exc:
        return {"ok": False, "provider": "generic_webhook", "error": str(exc)}
