"""
tools/crm/kenlo.py — Adapter para Kenlo CRM (ex-InGaia Imob).

API Reference: https://developers.kenlo.com/ (docs internos via suporte)
Base URL: https://api.kenlo.com.br/v1
Auth: Bearer token no header Authorization

Endpoints usados:
  POST   /leads                      → criar lead
  PATCH  /leads/:id                  → atualizar lead
  POST   /leads/:id/interactions     → adicionar interação/nota
  POST   /leads/:id/labels           → adicionar label/tag
  PATCH  /leads/:id/status           → atualizar status
  PATCH  /leads/:id/assign           → atribuir corretor
  GET    /team/agents                → listar agentes

Notas:
  - Kenlo é rebranding do InGaia Imob — referência em gestão de portfólio
  - Foco em imobiliárias que gerenciam locação e venda simultaneamente
  - "Interactions" são o equivalente a notas/histórico
  - Labels (tags) criadas automaticamente se não existirem
  - Status mapeado como string direta
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import CRMAdapter, CRMResult, LeadPayload, LeadSource, LeadStatus

log = logging.getLogger(__name__)

KENLO_BASE_URL = "https://api.kenlo.com.br/v1"
TIMEOUT = 10.0


class KenloAdapter(CRMAdapter):
    """Adapter para Kenlo (ex-InGaia Imob) — forte em locação e venda simultânea."""

    @property
    def provider_name(self) -> str:
        return "Kenlo"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_lead_payload(self, lead: LeadPayload) -> dict:
        """
        Payload para POST /leads.

        Campos Kenlo:
          name          → nome do lead
          phone         → telefone (com ou sem DDD)
          email         → e-mail (opcional)
          source        → canal de origem
          profile       → perfil (buyer, tenant, investor)
          agent_id      → corretor responsável
          team_id       → equipe/fila
          description   → descrição/interesse
          budget        → orçamento (float)
          bedrooms      → número de quartos desejados
          neighborhoods → lista de bairros de interesse
        """
        payload: dict[str, Any] = {
            "phone": lead.phone,
            "source": self._map_source(lead.source),
        }

        if lead.name:
            payload["name"] = lead.name
        if lead.email:
            payload["email"] = lead.email

        # Perfil mapeado para enum Kenlo
        profile_map = {
            "comprador": "buyer",
            "locatario": "tenant",
            "investidor": "investor",
            "indefinido": "buyer",  # default
        }
        if lead.profile:
            payload["profile"] = profile_map.get(lead.profile.value, "buyer")

        # Agente/corretor default
        default_agent = self.config.get("default_agent_id")
        if default_agent:
            payload["agent_id"] = default_agent

        # Equipe/fila
        if self.queue_id:
            payload["team_id"] = self.queue_id

        # Orçamento
        if lead.budget:
            payload["budget"] = lead.budget

        # Quartos
        if lead.bedrooms:
            payload["bedrooms"] = lead.bedrooms

        # Bairro de interesse como lista
        if lead.neighborhood:
            payload["neighborhoods"] = [lead.neighborhood]

        # Descrição/interesse
        desc_parts = []
        if lead.notes:
            desc_parts.append(lead.notes)
        if lead.intention_score:
            desc_parts.append(f"Score de intenção Sofia: {lead.intention_score}")
        if desc_parts:
            payload["description"] = "\n".join(desc_parts)

        return payload

    async def create_lead(self, lead: LeadPayload) -> CRMResult:
        payload = self._build_lead_payload(lead)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{KENLO_BASE_URL}/leads",
                    headers=self._headers(),
                    json=payload,
                )
            if resp.status_code in (200, 201):
                data = resp.json()
                # Kenlo pode retornar { id } ou { data: { id } } ou { lead: { id } }
                external_id = str(
                    data.get("id")
                    or data.get("data", {}).get("id", "")
                    or data.get("lead", {}).get("id", "")
                )
                log.info("[Kenlo] Lead criado: id=%s phone=%s", external_id, lead.phone)

                # Adicionar labels após criação
                if external_id:
                    await self._add_labels(external_id, lead)

                return CRMResult.ok(external_id=external_id, response=data)

            log.warning("[Kenlo] create_lead falhou: status=%d body=%s", resp.status_code, resp.text[:200])
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except httpx.TimeoutException:
            return CRMResult.fail(error="Timeout ao criar lead no Kenlo")
        except Exception as exc:
            log.error("[Kenlo] create_lead exception: %s", exc)
            return CRMResult.fail(error=str(exc))

    async def _add_labels(self, external_id: str, lead: LeadPayload) -> None:
        """Adiciona labels ao lead após criação."""
        labels = ["Sofia-IA"]
        if lead.intention_score >= 8:
            labels.append("lead-quente")
        elif lead.intention_score >= 4:
            labels.append("lead-morno")
        else:
            labels.append("lead-frio")

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                await client.post(
                    f"{KENLO_BASE_URL}/leads/{external_id}/labels",
                    headers=self._headers(),
                    json={"labels": labels},
                )
        except Exception as exc:
            log.warning("[Kenlo] _add_labels falhou: %s", exc)

    async def update_lead(self, external_id: str, updates: dict) -> CRMResult:
        """PATCH /leads/:id"""
        payload: dict[str, Any] = {}

        if "name" in updates:
            payload["name"] = updates["name"]
        if "email" in updates:
            payload["email"] = updates["email"]
        if "budget" in updates and updates["budget"]:
            payload["budget"] = updates["budget"]
        if "bedrooms" in updates and updates["bedrooms"]:
            payload["bedrooms"] = updates["bedrooms"]
        if "neighborhood" in updates and updates["neighborhood"]:
            payload["neighborhoods"] = [updates["neighborhood"]]

        desc_parts = []
        if updates.get("notes"):
            desc_parts.append(updates["notes"])
        if updates.get("history_summary"):
            desc_parts.append(f"Resumo: {updates['history_summary']}")
        if desc_parts:
            payload["description"] = "\n".join(desc_parts)

        if not payload:
            return CRMResult.ok(external_id=external_id)

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.patch(
                    f"{KENLO_BASE_URL}/leads/{external_id}",
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
        """PATCH /leads/:id/status"""
        crm_status = self._map_status(status)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.patch(
                    f"{KENLO_BASE_URL}/leads/{external_id}/status",
                    headers=self._headers(),
                    json={"status": crm_status},
                )
            if resp.status_code in (200, 201):
                log.info("[Kenlo] Status atualizado: id=%s status=%s", external_id, crm_status)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def add_note(self, external_id: str, note: str) -> CRMResult:
        """POST /leads/:id/interactions — adiciona interação/nota."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{KENLO_BASE_URL}/leads/{external_id}/interactions",
                    headers=self._headers(),
                    json={
                        "type": "note",
                        "content": note,
                        "source": "Sofia IA",
                    },
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
        """PATCH /leads/:id/assign → atribui agente."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.patch(
                    f"{KENLO_BASE_URL}/leads/{external_id}/assign",
                    headers=self._headers(),
                    json={"agent_id": seller_id},
                )
            if resp.status_code in (200, 201):
                log.info("[Kenlo] Lead %s atribuído para agent %s", external_id, seller_id)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def get_sellers(self) -> list[dict]:
        """GET /team/agents — lista agentes para seller_mapping."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{KENLO_BASE_URL}/team/agents",
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, list) else data.get("agents", data.get("data", []))
        except Exception:
            pass
        return []

    async def _do_health_check(self) -> bool:
        """GET /team/agents — valida token."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{KENLO_BASE_URL}/team/agents",
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                log.info("[Kenlo] health_check OK")
                return True
            log.warning("[Kenlo] health_check falhou: status=%d", resp.status_code)
            return False
        except Exception as exc:
            log.error("[Kenlo] health_check exception: %s", exc)
            return False
