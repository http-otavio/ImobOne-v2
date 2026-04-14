"""
tools/crm/rdstation.py — Adapter para RD Station CRM.

API Reference: https://developers.rdstation.com/reference
Base URL: https://crm.rdstation.com/api/v1
Auth: token como query param (?token=...) em todas as requisições

Endpoints usados:
  POST   /deals                        → criar deal (oportunidade)
  PUT    /deals/:id                    → atualizar deal
  POST   /deals/:id/deal_stages        → mover entre estágios
  POST   /activities                   → adicionar atividade/nota
  GET    /users                        → listar usuários (vendedores)
  GET    /deal_stages?pipeline_id=...  → listar estágios do funil
  PUT    /deals/:id + user_id          → atribuir deal para vendedor

Notas de implementação:
  - RD Station CRM (diferente de RD Marketing) tem API própria
  - O deal é a entidade central — não há "Contact" separado como no Pipedrive
  - Contatos são criados implicitamente via campos do deal
  - Atividades do tipo "note" são usadas para briefings
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import CRMAdapter, CRMResult, LeadPayload, LeadSource, LeadStatus

log = logging.getLogger(__name__)

RDSTATION_BASE_URL = "https://crm.rdstation.com/api/v1"
TIMEOUT = 10.0


class RDStationAdapter(CRMAdapter):
    """Adapter para RD Station CRM — amplamente usado por imobiliárias com marketing digital ativo."""

    @property
    def provider_name(self) -> str:
        return "RDStation"

    def _params(self) -> dict:
        return {"token": self.api_token}

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_deal_payload(self, lead: LeadPayload) -> dict:
        """
        Cria payload para POST /deals.

        Campos RD Station CRM:
          name             → título do deal
          amount_montly    → valor mensal (opcional)
          amount_unique    → valor único (venda)
          user_id          → responsável
          deal_stage_id    → estágio do funil
          campaign_id      → campanha de origem
        """
        name = f"{lead.name or lead.phone} — {self._map_source(lead.source)}"
        payload: dict[str, Any] = {
            "deal": {
                "name": name,
                "contacts_attributes": [
                    {
                        "phones": [{"phone": lead.phone}],
                    }
                ],
            }
        }

        if lead.name:
            payload["deal"]["contacts_attributes"][0]["name"] = lead.name
        if lead.email:
            payload["deal"]["contacts_attributes"][0]["emails"] = [{"email": lead.email}]

        # Estágio inicial do funil (configurável por cliente)
        stage_id = self.config.get("initial_stage_id")
        if stage_id:
            payload["deal"]["deal_stage_id"] = stage_id

        # Responsável inicial
        if self.config.get("default_user_id"):
            payload["deal"]["user_id"] = self.config["default_user_id"]

        # Valor estimado
        if lead.budget:
            payload["deal"]["amount_unique"] = lead.budget

        # Nota inicial com contexto
        note_parts = []
        if lead.notes:
            note_parts.append(lead.notes)
        if lead.bedrooms:
            note_parts.append(f"Quartos: {lead.bedrooms}")
        if lead.neighborhood:
            note_parts.append(f"Região: {lead.neighborhood}")
        if lead.intention_score:
            note_parts.append(f"Score Sofia: {lead.intention_score}")
        if note_parts:
            payload["deal"]["annotations"] = "\n".join(note_parts)

        # Tags / labels via campaign se configurado
        campaign_id = self.config.get("campaign_id")
        if campaign_id:
            payload["deal"]["campaign_id"] = campaign_id

        return payload

    async def create_lead(self, lead: LeadPayload) -> CRMResult:
        payload = self._build_deal_payload(lead)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{RDSTATION_BASE_URL}/deals",
                    params=self._params(),
                    headers=self._headers(),
                    json=payload,
                )
            if resp.status_code in (200, 201):
                data = resp.json()
                deal_id = str(data.get("_id") or data.get("id", ""))
                log.info("[RDStation] Deal criado: id=%s phone=%s", deal_id, lead.phone)
                return CRMResult.ok(external_id=deal_id, response=data)
            log.warning("[RDStation] create_lead falhou: status=%d body=%s", resp.status_code, resp.text[:200])
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except httpx.TimeoutException:
            return CRMResult.fail(error="Timeout ao criar lead no RD Station CRM")
        except Exception as exc:
            log.error("[RDStation] create_lead exception: %s", exc)
            return CRMResult.fail(error=str(exc))

    async def update_lead(self, external_id: str, updates: dict) -> CRMResult:
        """PUT /deals/:id"""
        payload: dict[str, Any] = {"deal": {}}

        if "name" in updates:
            payload["deal"]["name"] = updates["name"]
        if "budget" in updates and updates["budget"]:
            payload["deal"]["amount_unique"] = updates["budget"]

        note_parts = []
        if updates.get("notes"):
            note_parts.append(updates["notes"])
        if updates.get("history_summary"):
            note_parts.append(f"Resumo: {updates['history_summary']}")
        if note_parts:
            payload["deal"]["annotations"] = "\n".join(note_parts)

        if not payload["deal"]:
            return CRMResult.ok(external_id=external_id)

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.put(
                    f"{RDSTATION_BASE_URL}/deals/{external_id}",
                    params=self._params(),
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
        """
        RD Station CRM não tem status direto — estágio move o deal no funil.
        Casos especiais: fechado → won, descartado → lost (via deals/:id/lost ou won).
        """
        crm_status = self._map_status(status)

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                if status == LeadStatus.FECHADO:
                    resp = await client.put(
                        f"{RDSTATION_BASE_URL}/deals/{external_id}",
                        params=self._params(),
                        headers=self._headers(),
                        json={"deal": {"deal_stage_id": self.config.get("won_stage_id", "")}},
                    )
                elif status in (LeadStatus.DESCARTADO, LeadStatus.INATIVO):
                    resp = await client.put(
                        f"{RDSTATION_BASE_URL}/deals/{external_id}",
                        params=self._params(),
                        headers=self._headers(),
                        json={"deal": {"deal_stage_id": self.config.get("lost_stage_id", "")}},
                    )
                else:
                    # Tenta mover para estágio configurado no status_mapping
                    stage_id = self.status_mapping.get(status.value)
                    if not stage_id:
                        return CRMResult.ok(external_id=external_id)
                    resp = await client.put(
                        f"{RDSTATION_BASE_URL}/deals/{external_id}",
                        params=self._params(),
                        headers=self._headers(),
                        json={"deal": {"deal_stage_id": stage_id}},
                    )

            if resp.status_code in (200, 201):
                log.info("[RDStation] Status atualizado: id=%s status=%s", external_id, crm_status)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def add_note(self, external_id: str, note: str) -> CRMResult:
        """POST /activities com tipo 'note' vinculado ao deal."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{RDSTATION_BASE_URL}/activities",
                    params=self._params(),
                    headers=self._headers(),
                    json={
                        "activity": {
                            "deal_id": external_id,
                            "type": "rdstation.crm.activities.type.task",
                            "subject": "Briefing Sofia IA",
                            "body": note,
                        }
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
        """PUT /deals/:id com user_id → atribui responsável."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.put(
                    f"{RDSTATION_BASE_URL}/deals/{external_id}",
                    params=self._params(),
                    headers=self._headers(),
                    json={"deal": {"user_id": seller_id}},
                )
            if resp.status_code in (200, 201):
                log.info("[RDStation] Deal %s atribuído para user %s", external_id, seller_id)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def get_sellers(self) -> list[dict]:
        """GET /users — lista usuários."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{RDSTATION_BASE_URL}/users",
                    params=self._params(),
                )
            if resp.status_code == 200:
                return resp.json().get("users", resp.json() if isinstance(resp.json(), list) else [])
        except Exception:
            pass
        return []

    async def _do_health_check(self) -> bool:
        """GET /users — valida token e conectividade."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{RDSTATION_BASE_URL}/users",
                    params=self._params(),
                )
            if resp.status_code == 200:
                log.info("[RDStation] health_check OK")
                return True
            log.warning("[RDStation] health_check falhou: status=%d", resp.status_code)
            return False
        except Exception as exc:
            log.error("[RDStation] health_check exception: %s", exc)
            return False
