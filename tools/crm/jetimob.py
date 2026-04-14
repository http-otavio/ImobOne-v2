"""
tools/crm/jetimob.py — Adapter para Jetimob CRM.

API Reference: https://developers.jetimob.com/
Base URL: https://api.jetimob.com/v2
Auth: Bearer token no header Authorization

Endpoints usados:
  POST   /leads                     → criar lead
  PUT    /leads/:id                 → atualizar lead
  POST   /leads/:id/notes           → adicionar nota
  PUT    /leads/:id/status          → atualizar status
  POST   /leads/:id/assign          → atribuir corretor
  GET    /users                     → listar usuários
  GET    /funnels                   → listar funis configurados

Notas:
  - Jetimob é plataforma vertical imobiliária (site + CRM integrados)
  - Forte uso em imobiliárias com site próprio Jetimob
  - Status do lead é string direta (não estágio de funil)
  - Suporte a múltiplos funis por cliente
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import CRMAdapter, CRMResult, LeadPayload, LeadSource, LeadStatus

log = logging.getLogger(__name__)

JETIMOB_BASE_URL = "https://api.jetimob.com/v2"
TIMEOUT = 10.0


class JetimobAdapter(CRMAdapter):
    """Adapter para Jetimob — plataforma vertical imobiliária com CRM integrado."""

    @property
    def provider_name(self) -> str:
        return "Jetimob"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_lead_payload(self, lead: LeadPayload) -> dict:
        """
        Payload para POST /leads.

        Campos Jetimob:
          name          → nome do lead
          phone         → telefone
          email         → e-mail (opcional)
          origin        → origem (WhatsApp, Portal, Site, etc.)
          funnel_id     → funil de vendas (configurável)
          broker_id     → corretor responsável (opcional)
          message       → mensagem/interesse inicial
          budget        → orçamento
          tags          → array de strings
        """
        payload: dict[str, Any] = {
            "phone": lead.phone,
            "origin": self._map_source(lead.source),
        }

        if lead.name:
            payload["name"] = lead.name
        if lead.email:
            payload["email"] = lead.email

        # Funil configurado no onboarding.json
        funnel_id = self.config.get("funnel_id")
        if funnel_id:
            payload["funnel_id"] = funnel_id

        # Corretor default
        default_broker = self.config.get("default_broker_id")
        if default_broker:
            payload["broker_id"] = default_broker

        # Mensagem de interesse consolidada
        parts = []
        if lead.notes:
            parts.append(lead.notes)
        if lead.budget:
            parts.append(f"Budget: R$ {lead.budget:,.0f}")
        if lead.bedrooms:
            parts.append(f"Quartos desejados: {lead.bedrooms}")
        if lead.neighborhood:
            parts.append(f"Região: {lead.neighborhood}")
        if lead.intention_score:
            parts.append(f"Score Sofia: {lead.intention_score}")
        if parts:
            payload["message"] = "\n".join(parts)

        if lead.budget:
            payload["budget"] = lead.budget

        # Tags
        tags = ["Sofia-IA"]
        if lead.profile and lead.profile.value != "indefinido":
            tags.append(f"perfil-{lead.profile.value}")
        if lead.intention_score >= 8:
            tags.append("lead-quente")
        elif lead.intention_score >= 4:
            tags.append("lead-morno")
        else:
            tags.append("lead-frio")
        payload["tags"] = tags

        return payload

    async def create_lead(self, lead: LeadPayload) -> CRMResult:
        payload = self._build_lead_payload(lead)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{JETIMOB_BASE_URL}/leads",
                    headers=self._headers(),
                    json=payload,
                )
            if resp.status_code in (200, 201):
                data = resp.json()
                external_id = str(
                    data.get("id")
                    or data.get("lead", {}).get("id", "")
                    or data.get("data", {}).get("id", "")
                )
                log.info("[Jetimob] Lead criado: id=%s phone=%s", external_id, lead.phone)
                return CRMResult.ok(external_id=external_id, response=data)
            log.warning("[Jetimob] create_lead falhou: status=%d body=%s", resp.status_code, resp.text[:200])
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except httpx.TimeoutException:
            return CRMResult.fail(error="Timeout ao criar lead no Jetimob")
        except Exception as exc:
            log.error("[Jetimob] create_lead exception: %s", exc)
            return CRMResult.fail(error=str(exc))

    async def update_lead(self, external_id: str, updates: dict) -> CRMResult:
        """PUT /leads/:id"""
        payload: dict[str, Any] = {}

        if "name" in updates:
            payload["name"] = updates["name"]
        if "email" in updates:
            payload["email"] = updates["email"]
        if "budget" in updates and updates["budget"]:
            payload["budget"] = updates["budget"]

        parts = []
        if updates.get("notes"):
            parts.append(updates["notes"])
        if updates.get("history_summary"):
            parts.append(f"Resumo: {updates['history_summary']}")
        if parts:
            payload["message"] = "\n".join(parts)

        if not payload:
            return CRMResult.ok(external_id=external_id)

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.put(
                    f"{JETIMOB_BASE_URL}/leads/{external_id}",
                    headers=self._headers(),
                    json=payload,
                )
            if resp.status_code in (200, 201):
                return CRMResult.ok(external_id=external_id, response=resp.json())
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def update_status(self, external_id: str, status: LeadStatus) -> CRMResult:
        """PUT /leads/:id/status"""
        crm_status = self._map_status(status)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.put(
                    f"{JETIMOB_BASE_URL}/leads/{external_id}/status",
                    headers=self._headers(),
                    json={"status": crm_status},
                )
            if resp.status_code in (200, 201):
                log.info("[Jetimob] Status atualizado: id=%s status=%s", external_id, crm_status)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def add_note(self, external_id: str, note: str) -> CRMResult:
        """POST /leads/:id/notes"""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{JETIMOB_BASE_URL}/leads/{external_id}/notes",
                    headers=self._headers(),
                    json={"content": note},
                )
            if resp.status_code in (200, 201):
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def _do_assign_seller(self, external_id: str, seller_id: str) -> CRMResult:
        """POST /leads/:id/assign → atribui corretor."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{JETIMOB_BASE_URL}/leads/{external_id}/assign",
                    headers=self._headers(),
                    json={"broker_id": seller_id},
                )
            if resp.status_code in (200, 201):
                log.info("[Jetimob] Lead %s atribuído para broker %s", external_id, seller_id)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def get_sellers(self) -> list[dict]:
        """GET /users — lista usuários/corretores."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{JETIMOB_BASE_URL}/users",
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, list) else data.get("data", [])
        except Exception:
            pass
        return []

    async def _do_health_check(self) -> bool:
        """GET /users — valida token."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{JETIMOB_BASE_URL}/users",
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                log.info("[Jetimob] health_check OK")
                return True
            log.warning("[Jetimob] health_check falhou: status=%d", resp.status_code)
            return False
        except Exception as exc:
            log.error("[Jetimob] health_check exception: %s", exc)
            return False
