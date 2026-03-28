"""
state/schema.py — Definição e validação do shared state board.

Cada mensagem que trafega entre agentes via Redis obedece ao schema TaskMessage.
Regras de escrita (enforcement em runtime):
  - Apenas o Orchestrator escreve status 'approved' ou 'deploy_ready'
  - Apenas o Auditor escreve no campo audit_result
  - Agentes de execução escrevem apenas no seu próprio payload
  - iteration > 3 na mesma task → escalação para revisão humana
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerações
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    VETOED = "vetoed"
    APPROVED = "approved"
    DEPLOY_READY = "deploy_ready"


class AuditStatus(str, Enum):
    APPROVED = "approved"
    APPROVED_WITH_NOTE = "approved_with_note"
    VETOED = "vetoed"


# Agentes reconhecidos pelo sistema
KNOWN_AGENTS = frozenset(
    {
        "orchestrator",
        "auditor",
        "dev_flow",
        "dev_persona",
        "ingestion",
        "context",
        "memory",
        "qa_journeys",
        "qa_integration",
        "monitor",
        "human",          # escalação para revisão humana
    }
)

# Apenas o orquestrador pode transicionar para esses status
ORCHESTRATOR_ONLY_STATUSES = frozenset({TaskStatus.APPROVED, TaskStatus.DEPLOY_READY})

# Limite de iterações antes de escalar para revisão humana
MAX_ITERATIONS = 3


# ---------------------------------------------------------------------------
# Sub-modelos
# ---------------------------------------------------------------------------


class AuditResult(BaseModel):
    """
    Resultado gerado pelo Agente 2 (arquiteto auditor).
    Segue o pipeline CoT adversarial definido no CLAUDE.md:
    argumento_a_favor → argumento_contra → alternativa_mais_simples
    → reversibilidade → veredicto → justificativa_em_uma_frase
    """

    status: AuditStatus
    justification: str = Field(
        ...,
        min_length=10,
        description="Justificativa do veredicto em uma frase clara.",
    )
    proposed_alternative: str | None = Field(
        default=None,
        description="Alternativa proposta quando status == vetoed ou approved_with_note.",
    )

    # CoT adversarial (campos opcionais para rastreabilidade)
    argument_for: str | None = None
    argument_against: str | None = None
    simpler_alternative: str | None = None
    reversibility: str | None = None

    @model_validator(mode="after")
    def validate_veto_has_alternative(self) -> AuditResult:
        if self.status == AuditStatus.VETOED and not self.proposed_alternative:
            raise ValueError(
                "Um veto exige proposed_alternative preenchido — "
                "o auditor deve propor um caminho alternativo."
            )
        return self


# ---------------------------------------------------------------------------
# Modelo principal
# ---------------------------------------------------------------------------


class TaskMessage(BaseModel):
    """
    Unidade atômica de comunicação entre agentes via Redis.
    Toda leitura e escrita no shared state board usa este schema.
    """

    # Identificadores
    task_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="UUID único por tarefa — chave primária no Redis.",
    )
    client_id: str = Field(
        ...,
        min_length=1,
        description="ID do cliente sendo configurado. Usado para isolamento de namespace.",
    )

    # Roteamento
    agent_from: str = Field(
        ..., description="Nome do agente que publicou a mensagem."
    )
    agent_to: str = Field(
        ..., description="Nome do agente destinatário ou 'orchestrator'."
    )

    # Estado
    status: TaskStatus = Field(default=TaskStatus.PENDING)

    # Conteúdo
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Entregável ou dados da tarefa. Escrito apenas pelo agente dono.",
    )
    audit_result: AuditResult | None = Field(
        default=None,
        description="Preenchido exclusivamente pelo Auditor após review.",
    )

    # Controle
    requires_review: bool = Field(
        default=False,
        description="Se True, o auditor é acionado antes de avançar no pipeline.",
    )
    error: str | None = Field(
        default=None,
        description="Descrição do erro quando status == blocked.",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC — momento da publicação.",
    )
    iteration: int = Field(
        default=0,
        ge=0,
        description="Número de iterações na mesma task. > 3 → escala para revisão humana.",
    )

    # ------------------------------------------------------------------
    # Validadores
    # ------------------------------------------------------------------

    @field_validator("agent_from", "agent_to")
    @classmethod
    def validate_agent_name(cls, v: str) -> str:
        if v not in KNOWN_AGENTS:
            raise ValueError(
                f"Agente '{v}' não reconhecido. "
                f"Agentes válidos: {sorted(KNOWN_AGENTS)}"
            )
        return v

    @model_validator(mode="after")
    def validate_orchestrator_only_statuses(self) -> TaskMessage:
        """Apenas o orchestrator pode setar status approved ou deploy_ready."""
        if (
            self.status in ORCHESTRATOR_ONLY_STATUSES
            and self.agent_from != "orchestrator"
        ):
            raise ValueError(
                f"Status '{self.status}' só pode ser definido pelo orchestrator. "
                f"Agente '{self.agent_from}' não tem essa autoridade."
            )
        return self

    @model_validator(mode="after")
    def validate_blocked_has_error(self) -> TaskMessage:
        """Task bloqueada sem descrição de erro é dado perdido."""
        if self.status == TaskStatus.BLOCKED and not self.error:
            raise ValueError(
                "Status 'blocked' exige campo 'error' preenchido."
            )
        return self

    @model_validator(mode="after")
    def validate_iteration_limit(self) -> TaskMessage:
        """Emite aviso estruturado quando iteration ultrapassa o limite."""
        if self.iteration > MAX_ITERATIONS:
            # Não bloqueia — o board.py decide a ação. Apenas anota.
            object.__setattr__(
                self,
                "_requires_human_escalation",
                True,
            )
        return self

    # ------------------------------------------------------------------
    # Propriedades derivadas
    # ------------------------------------------------------------------

    @property
    def requires_human_escalation(self) -> bool:
        """True quando iteration > MAX_ITERATIONS."""
        return self.iteration > MAX_ITERATIONS

    @property
    def redis_key(self) -> str:
        """Chave canônica no Redis para esta task."""
        return f"task:{self.client_id}:{self.task_id}"

    @property
    def channel(self) -> str:
        """Canal pub/sub do agente destinatário."""
        return f"agent:{self.agent_to}"

    # ------------------------------------------------------------------
    # Serialização
    # ------------------------------------------------------------------

    def to_redis(self) -> str:
        """Serializa para JSON compacto adequado ao Redis."""
        return self.model_dump_json()

    @classmethod
    def from_redis(cls, raw: str | bytes) -> TaskMessage:
        """Desserializa a partir do valor retornado pelo Redis."""
        return cls.model_validate_json(raw)

    model_config = {"frozen": False, "validate_assignment": True}


# ---------------------------------------------------------------------------
# Utilitários de fábrica
# ---------------------------------------------------------------------------


def make_task(
    client_id: str,
    agent_from: str,
    agent_to: str,
    payload: dict[str, Any] | None = None,
    requires_review: bool = False,
) -> TaskMessage:
    """Atalho para criar uma TaskMessage com defaults sensatos."""
    return TaskMessage(
        client_id=client_id,
        agent_from=agent_from,
        agent_to=agent_to,
        payload=payload or {},
        requires_review=requires_review,
    )
