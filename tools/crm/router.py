"""
tools/crm/router.py — CRMRouter: factory e dispatcher para integrações de CRM.

Responsabilidades:
  1. Instanciar o adapter correto com base em crm.provider no onboarding.json
  2. Expor interface unificada: create_lead, update_lead, update_status, add_note, assign_seller
  3. Encaminhar chamadas ao adapter sem que o caller precise conhecer o provider
  4. Logging centralizado de todas as operações com client_id
  5. Retry simples em falhas transitórias (status 5xx / timeout)

Uso básico:
    from tools.crm.router import CRMRouter

    router = CRMRouter.from_onboarding(onboarding_json, client_id="demo_01")
    result = await router.create_lead(lead_payload)
    if result.success:
        # salvar result.external_id no Supabase
        ...

Configuração esperada em onboarding.json:
    {
        "crm": {
            "provider": "c2s",       // c2s | cvcrm | pipedrive | rdstation | jetimob | kenlo
            "api_token": "...",
            "email": "...",          // apenas CV CRM
            "pipeline_id": "...",    // Pipedrive (opcional)
            "stage_id": "...",       // Pipedrive (opcional)
            "funnel_id": "...",      // Jetimob (opcional)
            "initial_stage_id": "...", // RD Station (opcional)
            "won_stage_id": "...",   // RD Station (opcional)
            "lost_stage_id": "...",  // RD Station (opcional)
            "queue_id": "...",       // fila de distribuição (C2S, Kenlo)
            "default_agent_id": "...", // corretor default (Kenlo)
            "default_attendant_id": "...", // corretor default (CVCRM)
            "default_broker_id": "...",    // corretor default (Jetimob)
            "default_user_id": "...",      // corretor default (RD Station)
            "channel_id": "...",     // canal (CVCRM)
            "campaign_id": "...",    // campanha (RD Station)
            "seller_mapping": {      // phone → seller_id por provider
                "5511999990001": "seller_id_no_crm"
            },
            "status_mapping": {      // LeadStatus.value → status/stage no CRM
                "visita_agendada": "stage_id_visita",
                "negociando": "stage_id_negociacao"
            },
            "source_mapping": {      // LeadSource.value → origem no CRM
                "WhatsApp": "whatsapp",
                "ZAP Imóveis": "zap"
            },
            "custom_fields": {}      // campos customizados por provider
        }
    }
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import CRMAdapter, CRMResult, LeadPayload, LeadStatus
from .c2s import C2SAdapter
from .cvcrm import CVCRMAdapter
from .jetimob import JetimobAdapter
from .kenlo import KenloAdapter
from .pipedrive import PipedriveAdapter
from .rdstation import RDStationAdapter

log = logging.getLogger(__name__)

# Registry de providers disponíveis
PROVIDER_REGISTRY: dict[str, type[CRMAdapter]] = {
    "c2s": C2SAdapter,
    "contact2sale": C2SAdapter,
    "cvcrm": CVCRMAdapter,
    "cv_crm": CVCRMAdapter,
    "cv-crm": CVCRMAdapter,
    "construtor_de_vendas": CVCRMAdapter,
    "pipedrive": PipedriveAdapter,
    "rdstation": RDStationAdapter,
    "rd_station": RDStationAdapter,
    "rd-station": RDStationAdapter,
    "jetimob": JetimobAdapter,
    "kenlo": KenloAdapter,
    "ingaia": KenloAdapter,  # alias legado
}

# Tentativas de retry em falhas transitórias
MAX_RETRIES = 2
RETRY_DELAY = 1.0  # segundos


class CRMRouter:
    """
    Factory + dispatcher para CRM adapters.

    Instancia o adapter correto e expõe interface unificada para todas
    as operações de CRM. O caller nunca lida com detalhes do provider.
    """

    def __init__(self, adapter: CRMAdapter, client_id: str = ""):
        self._adapter = adapter
        self._client_id = client_id

    @classmethod
    def from_onboarding(cls, onboarding: dict, client_id: str = "") -> "CRMRouter":
        """
        Cria CRMRouter a partir do onboarding.json do cliente.

        Args:
            onboarding: Dict do onboarding.json completo (ou apenas a seção "crm")
            client_id: ID do cliente para logging
        """
        # Aceita tanto o onboarding completo quanto já a seção crm
        crm_config = onboarding.get("crm", onboarding)

        provider = crm_config.get("provider", "").lower().strip()
        if not provider:
            raise ValueError(
                f"[CRMRouter] client_id={client_id}: 'crm.provider' não definido no onboarding.json. "
                f"Providers disponíveis: {list(PROVIDER_REGISTRY.keys())}"
            )

        adapter_class = PROVIDER_REGISTRY.get(provider)
        if not adapter_class:
            raise ValueError(
                f"[CRMRouter] Provider '{provider}' não suportado. "
                f"Disponíveis: {sorted(set(PROVIDER_REGISTRY.keys()))}"
            )

        adapter = adapter_class(crm_config)
        log.info(
            "[CRMRouter] client_id=%s provider=%s adapter=%s",
            client_id, provider, adapter_class.__name__,
        )
        return cls(adapter=adapter, client_id=client_id)

    @classmethod
    def from_config(cls, provider: str, config: dict, client_id: str = "") -> "CRMRouter":
        """
        Cria CRMRouter passando provider e config diretamente.

        Útil para testes e instâncias programáticas.
        """
        return cls.from_onboarding({"crm": {"provider": provider, **config}}, client_id)

    # ------------------------------------------------------------------
    # Interface pública — mesma para todos os providers
    # ------------------------------------------------------------------

    async def create_lead(self, lead: LeadPayload) -> CRMResult:
        """Cria lead no CRM. Retorna CRMResult com external_id em caso de sucesso."""
        return await self._with_retry("create_lead", lead)

    async def update_lead(self, external_id: str, updates: dict) -> CRMResult:
        """Atualiza campos do lead existente."""
        return await self._with_retry("update_lead", external_id, updates)

    async def update_status(self, external_id: str, status: LeadStatus) -> CRMResult:
        """Atualiza status/estágio do lead."""
        return await self._with_retry("update_status", external_id, status)

    async def add_note(self, external_id: str, note: str) -> CRMResult:
        """Adiciona nota/briefing ao lead."""
        return await self._with_retry("add_note", external_id, note)

    async def assign_seller(self, external_id: str, seller_phone: str) -> CRMResult:
        """Atribui lead a um corretor via seller_mapping."""
        return await self._with_retry("assign_seller", external_id, seller_phone)

    async def health_check(self) -> bool:
        """Valida conectividade e credenciais do CRM."""
        return await self._adapter.health_check()

    async def subscribe_webhook(self, callback_url: str, events: list[str]) -> CRMResult:
        """Registra webhook no CRM (providers que suportam)."""
        return await self._adapter.subscribe_webhook(callback_url, events)

    async def get_sellers(self) -> list[dict]:
        """Lista vendedores/corretores para popular seller_mapping."""
        if hasattr(self._adapter, "get_sellers"):
            return await self._adapter.get_sellers()  # type: ignore[attr-defined]
        return []

    @property
    def provider_name(self) -> str:
        return self._adapter.provider_name

    # ------------------------------------------------------------------
    # Retry com backoff simples
    # ------------------------------------------------------------------

    async def _with_retry(self, method: str, *args: Any) -> CRMResult:
        """
        Executa método do adapter com retry em falhas transitórias (5xx / timeout).

        Não faz retry em erros 4xx (cliente) — são falhas de dados, não de infra.
        """
        last_result: CRMResult | None = None
        fn = getattr(self._adapter, method)

        for attempt in range(MAX_RETRIES + 1):
            try:
                result: CRMResult = await fn(*args)

                if result.success:
                    log.debug(
                        "[CRMRouter] %s.%s OK: client=%s external_id=%s",
                        self.provider_name, method, self._client_id, result.external_id,
                    )
                    return result

                # Só retenta em erros 5xx (servidor) — não em 4xx (dados inválidos)
                is_server_error = result.status_code is not None and result.status_code >= 500
                is_timeout = result.error and "timeout" in result.error.lower()

                if not (is_server_error or is_timeout) or attempt >= MAX_RETRIES:
                    log.warning(
                        "[CRMRouter] %s.%s falhou (sem retry): client=%s error=%s",
                        self.provider_name, method, self._client_id, result.error,
                    )
                    return result

                last_result = result
                log.warning(
                    "[CRMRouter] %s.%s tentativa %d/%d falhou — retentando em %.1fs: %s",
                    self.provider_name, method, attempt + 1, MAX_RETRIES,
                    RETRY_DELAY, result.error,
                )
                await asyncio.sleep(RETRY_DELAY)

            except Exception as exc:
                log.error(
                    "[CRMRouter] %s.%s exception na tentativa %d: %s",
                    self.provider_name, method, attempt + 1, exc,
                )
                last_result = CRMResult(success=False, error=str(exc))
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

        return last_result or CRMResult(success=False, error="Max retries atingido")


# ------------------------------------------------------------------
# Helper para uso no webhook — cria router a partir de client_id
# ------------------------------------------------------------------

def get_router_for_client(client_id: str, clients_dir: str = "/app/clients") -> CRMRouter | None:
    """
    Utilitário para o webhook — carrega onboarding.json do cliente e cria router.

    Retorna None se o cliente não tiver CRM configurado (campo 'crm' ausente).
    """
    import json
    import os

    onboarding_path = os.path.join(clients_dir, client_id, "onboarding.json")
    if not os.path.exists(onboarding_path):
        log.warning("[CRMRouter] onboarding.json não encontrado: %s", onboarding_path)
        return None

    with open(onboarding_path, "r", encoding="utf-8") as f:
        onboarding = json.load(f)

    if "crm" not in onboarding:
        log.debug("[CRMRouter] client_id=%s sem configuração de CRM — skip", client_id)
        return None

    try:
        return CRMRouter.from_onboarding(onboarding, client_id=client_id)
    except ValueError as exc:
        log.error("[CRMRouter] Erro ao criar router para %s: %s", client_id, exc)
        return None
