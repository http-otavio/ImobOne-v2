"""
tools/crm/base.py — Interface base para todos os adapters de CRM.

Cada CRM implementa essa interface. O CRMRouter (router.py) instancia
o adapter correto com base na configuração do cliente (onboarding.json).

Fluxo de dados:
  Sofia qualifica lead
    → _supabase_upsert_lead salva no Supabase
    → CRMRouter.create_lead() → adapter específico → API do CRM
    → CRMRouter.update_lead() a cada mudança relevante
    → CRMRouter.add_note() com briefing estratégico quando score ≥ threshold

Modelo de dados normalizado (LeadPayload):
  Todos os adapters recebem e convertem esse dict canônico — nunca
  recebem payloads proprietários diretamente.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums de domínio
# ---------------------------------------------------------------------------

class LeadStatus(str, Enum):
    NOVO            = "novo"
    QUALIFICANDO    = "qualificando"
    VISITA_AGENDADA = "visita_agendada"
    NEGOCIANDO      = "negociando"
    FECHADO         = "fechado"
    DESCARTADO      = "descartado"
    INATIVO         = "inativo"


class LeadProfile(str, Enum):
    COMPRADOR  = "comprador"
    LOCATARIO  = "locatario"
    INVESTIDOR = "investidor"
    INDEFINIDO = "indefinido"


class LeadSource(str, Enum):
    WHATSAPP  = "WhatsApp"
    ZAP       = "ZAP Imóveis"
    VIVAREAL  = "VivaReal"
    OLX       = "OLX"
    SITE      = "Site próprio"
    INSTAGRAM = "Instagram"
    INDICACAO = "Indicação"
    OUTROS    = "Outros"


# ---------------------------------------------------------------------------
# Payload canônico de lead
# ---------------------------------------------------------------------------

@dataclass
class LeadPayload:
    """
    Representação normalizada de um lead — agnóstica de CRM.
    Todos os adapters recebem e convertem esse objeto.
    """
    phone: str                              # Ex: "5511999990001" — sem + ou espaços
    name: str | None = None
    email: str | None = None
    source: LeadSource = LeadSource.WHATSAPP
    status: LeadStatus = LeadStatus.NOVO
    profile: LeadProfile = LeadProfile.INDEFINIDO
    intention_score: int = 0
    budget: float | None = None             # Valor em R$
    bedrooms: int | None = None
    neighborhood: str | None = None
    notes: str | None = None               # Briefing estratégico da Sofia
    history_summary: str | None = None     # Resumo da conversa
    external_id: str | None = None         # ID no CRM (preenchido após criação)
    metadata: dict = field(default_factory=dict)  # Dados extras por CRM

    def to_dict(self) -> dict:
        return {
            "phone": self.phone,
            "name": self.name,
            "email": self.email,
            "source": self.source.value if isinstance(self.source, LeadSource) else self.source,
            "status": self.status.value if isinstance(self.status, LeadStatus) else self.status,
            "profile": self.profile.value if isinstance(self.profile, LeadProfile) else self.profile,
            "intention_score": self.intention_score,
            "budget": self.budget,
            "bedrooms": self.bedrooms,
            "neighborhood": self.neighborhood,
            "notes": self.notes,
            "history_summary": self.history_summary,
            "external_id": self.external_id,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Resultado de operação CRM
# ---------------------------------------------------------------------------

@dataclass
class CRMResult:
    """Resultado de uma operação no CRM."""
    success: bool
    external_id: str | None = None       # ID gerado/atualizado no CRM
    response: dict | None = None         # Payload de resposta bruto
    error: str | None = None
    status_code: int | None = None

    @classmethod
    def ok(cls, external_id: str | None = None, response: dict | None = None) -> "CRMResult":
        return cls(success=True, external_id=external_id, response=response)

    @classmethod
    def fail(cls, error: str, status_code: int | None = None) -> "CRMResult":
        return cls(success=False, error=error, status_code=status_code)


# ---------------------------------------------------------------------------
# Interface base — todos os adapters implementam isso
# ---------------------------------------------------------------------------

class CRMAdapter(ABC):
    """
    Interface abstrata para integração com CRM.

    Cada método recebe e retorna tipos canônicos — nunca payloads proprietários.
    O adapter é responsável por traduzir entre o modelo canônico e a API do CRM.
    """

    def __init__(self, config: dict):
        """
        Args:
            config: Dicionário de configuração do CRM extraído do onboarding.json.
                    Contém: api_token, base_url (opcional), seller_mapping,
                    queue_id, status_mapping, e campos específicos por provider.
        """
        self.config = config
        self.api_token = config.get("api_token", "")
        self.seller_mapping: dict[str, str] = config.get("seller_mapping", {})
        self.status_mapping: dict[str, str] = config.get("status_mapping", {})
        self.queue_id: str | None = config.get("queue_id")

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Nome legível do provider (ex: 'C2S', 'CV CRM', 'Pipedrive')."""

    @abstractmethod
    async def create_lead(self, lead: LeadPayload) -> CRMResult:
        """
        Cria o lead no CRM.
        Returns: CRMResult com external_id preenchido em caso de sucesso.
        """

    @abstractmethod
    async def update_lead(self, external_id: str, updates: dict) -> CRMResult:
        """
        Atualiza campos do lead existente.
        Args:
            external_id: ID do lead no CRM.
            updates: Dict canônico com os campos a atualizar (subconjunto de LeadPayload.to_dict()).
        """

    @abstractmethod
    async def update_status(self, external_id: str, status: LeadStatus) -> CRMResult:
        """Atualiza o status do lead, usando status_mapping para traduzir."""

    @abstractmethod
    async def add_note(self, external_id: str, note: str) -> CRMResult:
        """Adiciona nota/comentário ao lead (ex: briefing estratégico da Sofia)."""

    async def assign_seller(self, external_id: str, seller_phone: str) -> CRMResult:
        """
        Atribui o lead a um vendedor/corretor.
        seller_phone é o telefone do corretor — mapeado para ID do CRM via seller_mapping.
        Implementação padrão retorna sucesso sem fazer nada (para CRMs sem assignment).
        """
        seller_id = self.seller_mapping.get(seller_phone)
        if not seller_id:
            log.warning(
                "[%s] seller_phone '%s' não encontrado no seller_mapping — assignment ignorado",
                self.provider_name, seller_phone
            )
            return CRMResult.ok()
        return await self._do_assign_seller(external_id, seller_id)

    async def _do_assign_seller(self, external_id: str, seller_id: str) -> CRMResult:
        """Override nos adapters que suportam assignment direto."""
        return CRMResult.ok()

    async def subscribe_webhook(self, callback_url: str, events: list[str]) -> CRMResult:
        """
        Registra webhook no CRM para receber eventos (ex: novo lead criado externamente).
        Implementação padrão retorna sucesso sem fazer nada (nem todos os CRMs suportam).
        """
        return CRMResult.ok()

    async def health_check(self) -> bool:
        """Valida se as credenciais estão corretas e a API está acessível."""
        try:
            result = await self._do_health_check()
            return result
        except Exception as exc:
            log.error("[%s] health_check falhou: %s", self.provider_name, exc)
            return False

    @abstractmethod
    async def _do_health_check(self) -> bool:
        """Implementação específica de health check por adapter."""

    def _map_status(self, status: LeadStatus) -> str:
        """Traduz LeadStatus canônico para o status específico do CRM."""
        return self.status_mapping.get(status.value, status.value)

    def _map_source(self, source: LeadSource | str) -> str:
        """Traduz LeadSource canônico para o valor aceito pelo CRM."""
        source_map = self.config.get("source_mapping", {})
        source_val = source.value if isinstance(source, LeadSource) else source
        return source_map.get(source_val, source_val)
