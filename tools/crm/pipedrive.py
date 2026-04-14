"""
tools/crm/pipedrive.py — Adapter para Pipedrive CRM.

API Reference: https://developers.pipedrive.com/docs/api/v1
Base URL: https://api.pipedrive.com/v1
Auth: api_token como query param (?api_token=...) em todas as requisições

Endpoints usados:
  POST   /persons                   → criar pessoa (contact)
  POST   /deals                     → criar deal (oportunidade)
  PUT    /deals/:id                 → atualizar deal
  POST   /deals/:id/timeline        → adicionar nota ao deal (via activities)
  POST   /notes                     → adicionar nota
  POST   /activities                → criar atividade (visita)
  GET    /stages                    → listar estágios do pipeline
  GET    /users                     → listar usuários/vendedores
  PUT    /deals/:id + owner_id      → atribuir deal para vendedor

Modelo Pipedrive:
  - Person  → o lead/contato
  - Deal    → a oportunidade de negócio (referencia a Person)
  - Note    → nota livre em texto
  - Activity → atividade agendada (ligação, visita, tarefa)

Strategy:
  - create_lead  → cria Person + Deal em paralelo
  - external_id  → deal_id (principal entidade no CRM)
  - person_id    → salvo em metadata para vincular notes
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import CRMAdapter, CRMResult, LeadPayload, LeadSource, LeadStatus

log = logging.getLogger(__name__)

PIPEDRIVE_BASE_URL = "https://api.pipedrive.com/v1"
TIMEOUT = 10.0


class PipedriveAdapter(CRMAdapter):
    """Adapter para Pipedrive — popular entre imobiliárias menores e médias."""

    @property
    def provider_name(self) -> str:
        return "Pipedrive"

    def _params(self) -> dict:
        """Query params com api_token — obrigatório em toda requisição."""
        return {"api_token": self.api_token}

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _pipeline_id(self) -> str | None:
        return self.config.get("pipeline_id")

    def _stage_id(self) -> str | None:
        return self.config.get("stage_id")

    def _build_person_payload(self, lead: LeadPayload) -> dict:
        """Cria payload para POST /persons."""
        payload: dict[str, Any] = {
            "phone": [{"value": lead.phone, "primary": True, "label": "mobile"}],
        }
        if lead.name:
            payload["name"] = lead.name
        else:
            payload["name"] = lead.phone  # Pipedrive exige name

        if lead.email:
            payload["email"] = [{"value": lead.email, "primary": True}]

        return payload

    def _build_deal_payload(
        self, lead: LeadPayload, person_id: str
    ) -> dict:
        """Cria payload para POST /deals."""
        title = f"{lead.name or lead.phone} — {self._map_source(lead.source)}"
        payload: dict[str, Any] = {
            "title": title,
            "person_id": int(person_id),
            "status": "open",
        }

        if self._pipeline_id():
            payload["pipeline_id"] = int(self._pipeline_id())  # type: ignore[arg-type]

        if self._stage_id():
            payload["stage_id"] = int(self._stage_id())  # type: ignore[arg-type]

        # Campos customizados opcionais (configurados por cliente no onboarding.json)
        custom_fields = self.config.get("custom_fields", {})
        if lead.budget and custom_fields.get("budget_field"):
            payload[custom_fields["budget_field"]] = lead.budget
        if lead.neighborhood and custom_fields.get("neighborhood_field"):
            payload[custom_fields["neighborhood_field"]] = lead.neighborhood

        return payload

    def _build_note_payload(self, deal_id: str, note: str) -> dict:
        return {
            "content": note,
            "deal_id": int(deal_id),
            "pinned_to_deal_flag": "1",
        }

    async def _create_person(
        self, client: httpx.AsyncClient, lead: LeadPayload
    ) -> str | None:
        """Cria Person no Pipedrive e retorna person_id."""
        try:
            resp = await client.post(
                f"{PIPEDRIVE_BASE_URL}/persons",
                params=self._params(),
                headers=self._headers(),
                json=self._build_person_payload(lead),
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                if data.get("success"):
                    return str(data["data"]["id"])
            log.warning("[Pipedrive] create_person falhou: %s", resp.text[:200])
        except Exception as exc:
            log.error("[Pipedrive] create_person exception: %s", exc)
        return None

    async def create_lead(self, lead: LeadPayload) -> CRMResult:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                # 1. Criar Person
                person_id = await self._create_person(client, lead)
                if not person_id:
                    return CRMResult.fail(error="Falha ao criar Person no Pipedrive")

                # 2. Criar Deal vinculado à Person
                deal_payload = self._build_deal_payload(lead, person_id)
                resp = await client.post(
                    f"{PIPEDRIVE_BASE_URL}/deals",
                    params=self._params(),
                    headers=self._headers(),
                    json=deal_payload,
                )

                if resp.status_code not in (200, 201) or not resp.json().get("success"):
                    return CRMResult.fail(
                        error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                        status_code=resp.status_code,
                    )

                data = resp.json()["data"]
                deal_id = str(data["id"])
                log.info(
                    "[Pipedrive] Lead criado: deal_id=%s person_id=%s phone=%s",
                    deal_id, person_id, lead.phone,
                )

                # 3. Adicionar nota consolidada se houver contexto
                note_parts = []
                if lead.notes:
                    note_parts.append(lead.notes)
                if lead.budget:
                    note_parts.append(f"Budget: R$ {lead.budget:,.0f}")
                if lead.bedrooms:
                    note_parts.append(f"Quartos: {lead.bedrooms}")
                if lead.neighborhood:
                    note_parts.append(f"Região: {lead.neighborhood}")
                if lead.intention_score:
                    note_parts.append(f"Score Sofia: {lead.intention_score}")

                if note_parts:
                    await client.post(
                        f"{PIPEDRIVE_BASE_URL}/notes",
                        params=self._params(),
                        headers=self._headers(),
                        json=self._build_note_payload(deal_id, "\n".join(note_parts)),
                    )

                return CRMResult.ok(
                    external_id=deal_id,
                    response={**data, "person_id": person_id},
                )

        except httpx.TimeoutException:
            return CRMResult.fail(error="Timeout ao criar lead no Pipedrive")
        except Exception as exc:
            log.error("[Pipedrive] create_lead exception: %s", exc)
            return CRMResult.fail(error=str(exc))

    async def update_lead(self, external_id: str, updates: dict) -> CRMResult:
        """PUT /deals/:id — atualiza campos do deal."""
        payload: dict[str, Any] = {}

        if "name" in updates:
            payload["title"] = updates["name"]
        if "status" in updates:
            crm_status = self._map_status(
                updates["status"] if isinstance(updates["status"], LeadStatus)
                else LeadStatus(updates["status"])
            )
            # Pipedrive deal status: open | won | lost | deleted
            if crm_status in ("fechado", "won"):
                payload["status"] = "won"
            elif crm_status in ("descartado", "lost"):
                payload["status"] = "lost"

        note_parts = []
        if updates.get("notes"):
            note_parts.append(updates["notes"])
        if updates.get("history_summary"):
            note_parts.append(f"Resumo: {updates['history_summary']}")
        if updates.get("budget"):
            note_parts.append(f"Budget atualizado: R$ {updates['budget']:,.0f}")

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                if payload:
                    resp = await client.put(
                        f"{PIPEDRIVE_BASE_URL}/deals/{external_id}",
                        params=self._params(),
                        headers=self._headers(),
                        json=payload,
                    )
                    if resp.status_code not in (200, 201):
                        return CRMResult.fail(
                            error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                            status_code=resp.status_code,
                        )

                if note_parts:
                    await client.post(
                        f"{PIPEDRIVE_BASE_URL}/notes",
                        params=self._params(),
                        headers=self._headers(),
                        json=self._build_note_payload(external_id, "\n".join(note_parts)),
                    )

            return CRMResult.ok(external_id=external_id)

        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def update_status(self, external_id: str, status: LeadStatus) -> CRMResult:
        """Atualiza status do deal — mapeado para open/won/lost."""
        crm_status = self._map_status(status)

        # Default mapping se não configurado no onboarding
        pipedrive_status = "open"
        if crm_status in ("fechado", "won"):
            pipedrive_status = "won"
        elif crm_status in ("descartado", "inativo", "lost"):
            pipedrive_status = "lost"

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.put(
                    f"{PIPEDRIVE_BASE_URL}/deals/{external_id}",
                    params=self._params(),
                    headers=self._headers(),
                    json={"status": pipedrive_status},
                )
            if resp.status_code in (200, 201) and resp.json().get("success"):
                log.info("[Pipedrive] Status atualizado: id=%s status=%s", external_id, pipedrive_status)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def add_note(self, external_id: str, note: str) -> CRMResult:
        """POST /notes — adiciona nota ao deal."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{PIPEDRIVE_BASE_URL}/notes",
                    params=self._params(),
                    headers=self._headers(),
                    json=self._build_note_payload(external_id, note),
                )
            if resp.status_code in (200, 201) and resp.json().get("success"):
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def _do_assign_seller(self, external_id: str, seller_id: str) -> CRMResult:
        """PUT /deals/:id com owner_id → atribui deal para usuário."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.put(
                    f"{PIPEDRIVE_BASE_URL}/deals/{external_id}",
                    params=self._params(),
                    headers=self._headers(),
                    json={"owner_id": int(seller_id)},
                )
            if resp.status_code in (200, 201) and resp.json().get("success"):
                log.info("[Pipedrive] Deal %s atribuído para user %s", external_id, seller_id)
                return CRMResult.ok(external_id=external_id)
            return CRMResult.fail(
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        except Exception as exc:
            return CRMResult.fail(error=str(exc))

    async def get_sellers(self) -> list[dict]:
        """GET /users — lista usuários para popular seller_mapping."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{PIPEDRIVE_BASE_URL}/users",
                    params=self._params(),
                )
            if resp.status_code == 200 and resp.json().get("success"):
                return resp.json().get("data", [])
        except Exception:
            pass
        return []

    async def _do_health_check(self) -> bool:
        """GET /users/me — valida token."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{PIPEDRIVE_BASE_URL}/users/me",
                    params=self._params(),
                )
            if resp.status_code == 200 and resp.json().get("success"):
                data = resp.json().get("data", {})
                log.info("[Pipedrive] health_check OK: user=%s", data.get("email", "?"))
                return True
            log.warning("[Pipedrive] health_check falhou: status=%d", resp.status_code)
            return False
        except Exception as exc:
            log.error("[Pipedrive] health_check exception: %s", exc)
            return False
