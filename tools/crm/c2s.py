"""
tools/crm/c2s.py — Adapter para C2S (Contact2Sale).

API Reference: https://docs-api-leads.c2sapp.com/
Base URL: https://api.contact2sale.com/integration
Auth: Bearer token no header Authorization

Endpoints usados:
  POST   /leads                          → criar lead
  PUT    /leads/:id                      → atualizar lead
  POST   /leads/:id/update_status        → atualizar status
  POST   /leads/:id/create_message       → adicionar nota/mensagem
  POST   /leads/:id/create_tag           → adicionar tag
  PATCH  /leads/:id/forward              → encaminhar para vendedor
  GET    /sellers                        → listar vendedores
  POST   /api/subscribe                  → registrar webhook
  POST   /api/unsubscribe                → remover webhook
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import CRMAdapter, CRMResult, LeadPayload, LeadSource, LeadStatus

log = logging.getLogger(__name__)

C2S_BASE_URL = "https://api.contact2sale.com/integration"
TIMEOUT = 10.0  # segundos


class C2SAdapter(CRMAdapter):
    """Adapter para C2S (Contact2Sale) — CRM dominante no mercado imobiliário SP."""

    @property
    def provider_name(self) -> str:
        return "C2S"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_lead_payload(self, lead: LeadPayload) -> dict:
        """
        Converte LeadPayload canônico para o payload esperado pelo POST /leads da C2S.

        Campos da API C2S:
          name         string  — nome do lead
          phone        string  — telefone (aceita com ou sem código do país)
          email        string  — e-mail (opcional)
          source       string  — origem do lead (ex: "WhatsApp", "ZAP Imóveis")
          note         string  — observação inicial
          queue_id     string  — fila de distribuição (opcional)
          seller_id    string  — vendedor específico (opcional, use queue_id OU seller_id)
          tags         array   — lista de tags string
        """
        payload: dict[str, Any] = {
            "phone": lead.phone,
        }

        if lead.name:
            payload["name"] = lead.name

        if lead.email:
            payload["email"] = lead.email

        payload["source"] = self._map_source(lead.source)

        # Montar nota consolidada com contexto do lead
        note_parts = []
        if lead.notes:
            note_parts.append(lead.notes)
        if lead.budget:
            note_parts.append(f"Budget: R$ {lead.budget:,.0f}")
        if lead.bedrooms:
            note_parts.append(f"Quartos: {lead.bedrooms}")
        if lead.neighborhood:
            note_parts.append(f"Região de interesse: {lead.neighborhood}")
        if lead.intention_score:
            note_parts.append(f"Score de intenção Sofia: {lead.intention_score}")
        if note_parts:
            payload["note"] = "\n".join(note_parts)

        # Fila de distribuição ou vendedor específico
        if self.queue_id:
            payload["queue_id"] = self.queue_id

        # Tags por perfil
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
                    f"{C2S_BASE_URL}/leads",
                    headers=self._headers(),
                    json=payload,
                )
            if resp.status_code in (200, 201):
                data = resp.json()
                external_id = str(data.get("id") or data.get("lead", {}).get("id", ""))
                log.info("[C2S] Lead criado: external_id=%s phone=%s", external_id, lead.phone)
                return CRMResult.ok(external_id=external_id, response=data)
            else:
                log.warning(
                    "[C2S] create_lead falhou: status=%d body=%s",
                    resp.status_code, resp.text[:200]
                )
                return CRMResult.fail(
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )
        except httpx.TimeoutException:
            return CRMResult.fail(error="Timeout ao criar lead no C2S")
        except Exception as exc:
            log.error("[C2S] create_lead exception: %s", exc)
            return CRMResult.fail(error=str(exc))

    async def update_lead(self, external_id: str, updates: dict) -> CRMResult:
        """
        PUT /leads/:id
        Campos aceitos: name, email, phone, note, source, tags
        """
        payload: dict[str, Any] = {}

        if "name" in updates:
            payload["name"] = updates["name"]
        if "email" in updates:
            payload["email"] = updates["email"]
        if "notes" in updates or "history_summary" in updates:
            note_parts = []
            if updates.get("notes"):
                note_parts.append(updates["notes"])
            if updates.get("history_summary"):
                note_parts.append(f"Resumo: {updates['history_summary']}")
            if note_parts:
                payload["note"] = "\n".join(note_parts)
        if "budget" in updates and updates["budget"]:
            payload.setdefault("note", "")
            payload["note"] += f"\nBudget atualizado: R$ {updates['budget']:,.0f}"

        if not payload:
            return CRMResult.ok(external_id=external_id)

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.put(
                    f"{C2S_BASE_URL}/leads/{external_id}",
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
        """POST /leads/:id/update_status"""
        crm_status = self._map_status(status)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{C2S_BASE_URL}/leads/{external_id}/update_status",
                    headers=self._headers(),
                    json={"status": crm_status},
                )
            if resp.status_code in (200, 201):
                log.info("[C2S] Status atualizado: id=%s status=%s", external_id, crm_status)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def add_note(self, external_id: str, note: str) -> CRMResult:
        """POST /leads/:id/create_message — adiciona nota/mensagem ao lead."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{C2S_BASE_URL}/leads/{external_id}/create_message",
                    headers=self._headers(),
                    json={"message": note},
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
        """PATCH /leads/:id/forward — encaminha lead para vendedor específico."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.patch(
                    f"{C2S_BASE_URL}/leads/{external_id}/forward",
                    headers=self._headers(),
                    json={"seller_id": seller_id},
                )
            if resp.status_code in (200, 201):
                log.info("[C2S] Lead %s encaminhado para seller %s", external_id, seller_id)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def subscribe_webhook(self, callback_url: str, events: list[str]) -> CRMResult:
        """
        POST /api/subscribe — registra webhook para receber eventos do C2S.
        Útil para receber novos leads criados diretamente no C2S (ex: portal → C2S → Sofia).
        """
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{C2S_BASE_URL}/api/subscribe",
                    headers=self._headers(),
                    json={"url": callback_url, "events": events},
                )
            if resp.status_code in (200, 201):
                log.info("[C2S] Webhook registrado: url=%s events=%s", callback_url, events)
                return CRMResult.ok(response=resp.json())
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def get_sellers(self) -> list[dict]:
        """GET /sellers — lista vendedores para popular seller_mapping."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{C2S_BASE_URL}/sellers",
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception:
            return []

    async def _do_health_check(self) -> bool:
        """GET /me — valida token e conectividade."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{C2S_BASE_URL}/me",
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                data = resp.json()
                log.info("[C2S] health_check OK: empresa=%s", data.get("name", "?"))
                return True
            log.warning("[C2S] health_check falhou: status=%d", resp.status_code)
            return False
        except Exception as exc:
            log.error("[C2S] health_check exception: %s", exc)
            return False
