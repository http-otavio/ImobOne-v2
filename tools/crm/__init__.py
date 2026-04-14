"""
tools/crm/__init__.py — Pacote de integrações CRM.

Exporta:
  - CRMAdapter (base)
  - CRMRouter (factory / dispatcher)
  - LeadPayload, CRMResult, LeadStatus, LeadProfile, LeadSource
  - Adapters concretos: C2SAdapter, CVCRMAdapter, PipedriveAdapter,
    RDStationAdapter, JetimobAdapter, KenloAdapter
"""

from .base import (
    CRMAdapter,
    CRMResult,
    LeadPayload,
    LeadProfile,
    LeadSource,
    LeadStatus,
)
from .c2s import C2SAdapter
from .cvcrm import CVCRMAdapter
from .jetimob import JetimobAdapter
from .kenlo import KenloAdapter
from .pipedrive import PipedriveAdapter
from .rdstation import RDStationAdapter
from .router import CRMRouter

__all__ = [
    # Base
    "CRMAdapter",
    "CRMResult",
    "LeadPayload",
    "LeadProfile",
    "LeadSource",
    "LeadStatus",
    # Router
    "CRMRouter",
    # Adapters
    "C2SAdapter",
    "CVCRMAdapter",
    "PipedriveAdapter",
    "RDStationAdapter",
    "JetimobAdapter",
    "KenloAdapter",
]
