"""
tests/test_schema.py — Testes unitários de state/schema.py.

Mínimo exigido: 3 testes antes de qualquer integração ao grafo (CLAUDE.md).
Cobertura:
  1. Criação e serialização/desserialização round-trip de TaskMessage válida
  2. Validação de autoridade: não-orchestrator não pode setar status aprovado
  3. Auditoria: veto sem proposed_alternative deve ser rejeitado
  (Bônus) 4. Escalação humana ativada quando iteration > MAX_ITERATIONS
  (Bônus) 5. Status blocked sem campo error deve ser rejeitado
"""

import json

import pytest
from pydantic import ValidationError

from state.schema import (
    MAX_ITERATIONS,
    AuditResult,
    AuditStatus,
    TaskMessage,
    TaskStatus,
    make_task,
)


# ---------------------------------------------------------------------------
# Teste 1 — Round-trip de serialização via Redis
# ---------------------------------------------------------------------------


def test_round_trip_serialization():
    """
    TaskMessage deve sobreviver ao ciclo serialize → deserialize
    sem perda de dados, incluindo campos opcionais None.
    """
    task = make_task(
        client_id="cliente_001",
        agent_from="ingestion",
        agent_to="orchestrator",
        payload={"imoveis_indexados": 42, "campos_faltantes": []},
        requires_review=True,
    )

    raw = task.to_redis()
    assert isinstance(raw, str)

    recovered = TaskMessage.from_redis(raw)

    assert recovered.task_id == task.task_id
    assert recovered.client_id == "cliente_001"
    assert recovered.agent_from == "ingestion"
    assert recovered.agent_to == "orchestrator"
    assert recovered.payload["imoveis_indexados"] == 42
    assert recovered.requires_review is True
    assert recovered.audit_result is None
    assert recovered.error is None
    assert recovered.iteration == 0
    assert recovered.status == TaskStatus.PENDING

    # redis_key e channel devem ser idempotentes após round-trip
    assert recovered.redis_key == f"task:cliente_001:{task.task_id}"
    assert recovered.channel == "agent:orchestrator"


# ---------------------------------------------------------------------------
# Teste 2 — Autoridade exclusiva do orchestrator para status aprovado
# ---------------------------------------------------------------------------


def test_non_orchestrator_cannot_set_approved_status():
    """
    Agentes de execução não podem setar status 'approved' ou 'deploy_ready'.
    Apenas o orchestrator tem essa autoridade (regra central do CLAUDE.md).
    """
    with pytest.raises(ValidationError) as exc_info:
        TaskMessage(
            client_id="cliente_001",
            agent_from="qa_journeys",   # agente de execução — não autorizado
            agent_to="orchestrator",
            status=TaskStatus.APPROVED,
        )

    errors = exc_info.value.errors()
    messages = [e["msg"] for e in errors]
    assert any("orchestrator" in msg for msg in messages), (
        f"Esperava menção a 'orchestrator' nos erros de validação, mas got: {messages}"
    )

    # Orchestrator pode — não deve lançar exceção
    task = TaskMessage(
        client_id="cliente_001",
        agent_from="orchestrator",
        agent_to="dev_flow",
        status=TaskStatus.APPROVED,
    )
    assert task.status == TaskStatus.APPROVED


# ---------------------------------------------------------------------------
# Teste 3 — Veto do auditor exige proposed_alternative
# ---------------------------------------------------------------------------


def test_audit_veto_requires_proposed_alternative():
    """
    AuditResult com status 'vetoed' sem proposed_alternative viola a regra
    do arquiteto auditor: um veto sem alternativa não agrega valor ao pipeline.
    """
    with pytest.raises(ValidationError) as exc_info:
        AuditResult(
            status=AuditStatus.VETOED,
            justification="A abordagem escolhida gera acoplamento desnecessário.",
            proposed_alternative=None,  # ← deve falhar
        )

    errors = exc_info.value.errors()
    assert any("proposed_alternative" in str(e) or "alternativ" in e["msg"].lower() for e in errors), (
        f"Esperava erro relacionado a proposed_alternative, mas got: {errors}"
    )

    # Com alternativa — deve passar
    result = AuditResult(
        status=AuditStatus.VETOED,
        justification="A abordagem escolhida gera acoplamento desnecessário.",
        proposed_alternative="Usar Redis pub/sub ao invés de polling direto.",
    )
    assert result.status == AuditStatus.VETOED
    assert result.proposed_alternative is not None


# ---------------------------------------------------------------------------
# Teste 4 (bônus) — Escalação humana ativada após MAX_ITERATIONS
# ---------------------------------------------------------------------------


def test_human_escalation_flag_above_max_iterations():
    """
    Propriedade requires_human_escalation deve ser True quando
    iteration > MAX_ITERATIONS, sinalizando ao orchestrator
    para parar de tentar resolver automaticamente.
    """
    task_normal = make_task("cliente_001", "dev_flow", "orchestrator")
    task_normal.iteration = MAX_ITERATIONS
    assert task_normal.requires_human_escalation is False

    task_escalada = make_task("cliente_001", "dev_flow", "orchestrator")
    task_escalada.iteration = MAX_ITERATIONS + 1
    assert task_escalada.requires_human_escalation is True


# ---------------------------------------------------------------------------
# Teste 5 (bônus) — Status blocked exige campo error
# ---------------------------------------------------------------------------


def test_blocked_status_requires_error_field():
    """
    Uma task com status 'blocked' sem descrição de erro é dado perdido —
    o orchestrator não tem como diagnosticar sem saber o motivo.
    """
    with pytest.raises(ValidationError) as exc_info:
        TaskMessage(
            client_id="cliente_001",
            agent_from="context",
            agent_to="orchestrator",
            status=TaskStatus.BLOCKED,
            error=None,  # ← deve falhar
        )

    errors = exc_info.value.errors()
    assert any("error" in e["msg"].lower() or "blocked" in e["msg"].lower() for e in errors)

    # Com error preenchido — deve passar
    task = TaskMessage(
        client_id="cliente_001",
        agent_from="context",
        agent_to="orchestrator",
        status=TaskStatus.BLOCKED,
        error="Google Places API retornou 403 — cota esgotada.",
    )
    assert task.status == TaskStatus.BLOCKED
    assert task.error is not None
