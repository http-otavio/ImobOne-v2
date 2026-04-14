"""
tools/crm/cvcrm.py — Adapter para CV CRM (Construtor de Vendas).

API Reference: https://cv-crm.readme.io/reference
Base URL: https://app.cvcrm.com.br/api/integrations/v1
Auth: email + token nos headers (cv-email + cv-token)

Endpoints usados:
  POST   /leads                         → criar lead
  PUT    /leads/:id                     → atualizar lead
  POST   /leads/:id/history             → adicionar histórico/nota
  PATCH  /leads/:id/change_stage        → mover estágio do funil
  POST   /leads/:id/tags                → adicionar tag
  GET    /lead_origin                   → listar origens configuradas
  GET    /attendants                    → listar atendentes/corretores
  GET    /stages                        → listar estágios do funil

Notas:
  - CV CRM é dominante no mercado imobiliário brasileiro (incorporadoras e lançamentos)
  - Autenticação dual: cv-email + cv-token no header (não Bearer)
  - Lead usa "attendant_id" para atribuição ao corretor
  - Estágios do funil são configuráveis e mapeados no onboarding.json
  - Tags suportadas nativamente
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import CRMAdapter, CRMResult, LeadPayload, LeadSource, LeadStatus

log = logging.getLogger(__name__)

CVCRM_BASE_URL = "https://app.cvcrm.com.br/api/integrations/v1"
TIMEOUT = 10.0


class CVCRMAdapter(CRMAdapter):
    """Adapter para CV CRM — CRM dominante no mercado imobiliário brasileiro."""

    @property
    def provider_name(self) -> str:
        return "CVCRM"

    def _headers(self) -> dict:
        """CV CRM usa cv-email + cv-token no header (não Bearer)."""
        return {
            "cv-email": self.config.get("email", ""),
            "cv-token": self.api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_lead_payload(self, lead: LeadPayload) -> dict:
        """
        Payload para POST /leads.

        Campos CV CRM:
          name              → nome do lead
          phone             → telefone
          email             → e-mail (opcional)
          origin_id         → ID da origem (mapeado via source_mapping)
          channel_id        → canal de comunicação
          attendant_id      → corretor responsável
          product_id        → empreendimento de interesse
          notes             → observação inicial
          custom_fields     → campos customizados pelo cliente
        """
        payload: dict[str, Any] = {
            "phone": lead.phone,
        }

        if lead.name:
            payload["name"] = lead.name
        if lead.email:
            payload["email"] = lead.email

        # Origem mapeada via source_mapping no onboarding.json
        origin_id = self._map_source(lead.source)
        if origin_id:
            payload["origin_id"] = origin_id

        # Canal (ex: WhatsApp = canal configurado no CV)
        channel_id = self.config.get("channel_id")
        if channel_id:
            payload["channel_id"] = channel_id

        # Atendente padrão (corretor) se configurado
        default_attendant = self.config.get("default_attendant_id")
        if default_attendant:
            payload["attendant_id"] = default_attendant

        # Produto/empreendimento default (pode ser sobrescrito por lead)
        default_product = self.config.get("default_product_id")
        if default_product:
            payload["product_id"] = default_product

        # Nota consolidada
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
            payload["notes"] = "\n".join(note_parts)

        # Fila de distribuição
        if self.queue_id:
            payload["queue_id"] = self.queue_id

        return payload

    async def create_lead(self, lead: LeadPayload) -> CRMResult:
        payload = self._build_lead_payload(lead)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{CVCRM_BASE_URL}/leads",
                    headers=self._headers(),
                    json=payload,
                )
            if resp.status_code in (200, 201):
                data = resp.json()
                # CV CRM retorna { "data": { "id": ... } } ou { "id": ... }
                lead_data = data.get("data", data)
                external_id = str(lead_data.get("id", ""))
                log.info("[CVCRM] Lead criado: id=%s phone=%s", external_id, lead.phone)

                # Adicionar tags após criação
                if external_id:
                    await self._add_tags(external_id, lead)

                return CRMResult.ok(external_id=external_id, response=data)

            log.warning("[CVCRM] create_lead falhou: status=%d body=%s", resp.status_code, resp.text[:200])
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except httpx.TimeoutException:
            return CRMResult.fail(error="Timeout ao criar lead no CV CRM")
        except Exception as exc:
            log.error("[CVCRM] create_lead exception: %s", exc)
            return CRMResult.fail(error=str(exc))

    async def _add_tags(self, external_id: str, lead: LeadPayload) -> None:
        """Adiciona tags ao lead após criação."""
        tags = ["Sofia-IA"]
        if lead.profile and lead.profile.value != "indefinido":
            tags.append(f"perfil-{lead.profile.value}")
        if lead.intention_score >= 8:
            tags.append("lead-quente")
        elif lead.intention_score >= 4:
            tags.append("lead-morno")
        else:
            tags.append("lead-frio")

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                for tag in tags:
                    await client.post(
                        f"{CVCRM_BASE_URL}/leads/{external_id}/tags",
                        headers=self._headers(),
                        json={"tag": tag},
                    )
        except Exception as exc:
            log.warning("[CVCRM] _add_tags falhou: %s", exc)

    async def update_lead(self, external_id: str, updates: dict) -> CRMResult:
        """PUT /leads/:id"""
        payload: dict[str, Any] = {}

        if "name" in updates:
            payload["name"] = updates["name"]
        if "email" in updates:
            payload["email"] = updates["email"]

        note_parts = []
        if updates.get("notes"):
            note_parts.append(updates["notes"])
        if updates.get("history_summary"):
            note_parts.append(f"Resumo: {updates['history_summary']}")
        if updates.get("budget"):
            note_parts.append(f"Budget atualizado: R$ {updates['budget']:,.0f}")
        if note_parts:
            payload["notes"] = "\n".join(note_parts)

        if not payload:
            return CRMResult.ok(external_id=external_id)

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.put(
                    f"{CVCRM_BASE_URL}/leads/{external_id}",
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
        """PATCH /leads/:id/change_stage — move lead no funil."""
        stage_id = self._map_status(status)
        if not stage_id:
            log.warning("[CVCRM] status '%s' não mapeado para stage_id", status.value)
            return CRMResult.ok(external_id=external_id)

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.patch(
                    f"{CVCRM_BASE_URL}/leads/{external_id}/change_stage",
                    headers=self._headers(),
                    json={"stage_id": stage_id},
                )
            if resp.status_code in (200, 201):
                log.info("[CVCRM] Estágio atualizado: id=%s stage=%s", external_id, stage_id)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def add_note(self, external_id: str, note: str) -> CRMResult:
        """POST /leads/:id/history — adiciona histórico/nota."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{CVCRM_BASE_URL}/leads/{external_id}/history",
                    headers=self._headers(),
                    json={"description": note, "type": "note"},
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
        """PUT /leads/:id com attendant_id → atribui corretor."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.put(
                    f"{CVCRM_BASE_URL}/leads/{external_id}",
                    headers=self._headers(),
                    json={"attendant_id": seller_id},
                )
            if resp.status_code in (200, 201):
                log.info("[CVCRM] Lead %s atribuído para attendant %s", external_id, seller_id)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def get_sellers(self) -> list[dict]:
        """GET /attendants — lista corretores para seller_mapping."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{CVCRM_BASE_URL}/attendants",
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", data) if isinstance(data, dict) else data
        except Exception:
            pass
        return []

    async def _do_health_check(self) -> bool:
        """GET /attendants — valida credenciais cv-email + cv-token."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{CVCRM_BASE_URL}/attendants",
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                log.info("[CVCRM] health_check OK")
                return True
            log.warning("[CVCRM] health_check falhou: status=%d", resp.status_code)
            return False
        except Exception as exc:
            log.error("[CVCRM] health_check exception: %s", exc)
            return False
