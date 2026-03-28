"""
tests/test_board.py — Testes unitários de state/board.py.

Mínimo exigido: 3 testes antes de qualquer integração (CLAUDE.md).
CLAUDE.md exige adicionalmente testes de concorrência (dois agentes escrevendo
simultaneamente). Todos os testes usam fakeredis para rodar sem Redis real.

Cobertura:
  1. Write + Read round-trip: task escrita deve ser recuperada corretamente
  2. Escalação humana: write deve lançar HumanEscalationError acima do limite
  3. Concorrência: dois agentes escrevendo tasks distintas simultaneamente (sem corrupção)
  (Bônus) 4. UnauthorizedWriteError: agent errado não pode escrever task de outro
  (Bônus) 5. update_audit_result: somente auditor atualiza o campo correto
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from state.board import (
    HumanEscalationError,
    StateBoard,
    TaskNotFoundError,
    UnauthorizedWriteError,
)
from state.schema import MAX_ITERATIONS, AuditStatus, TaskMessage, TaskStatus, make_task


# ---------------------------------------------------------------------------
# Fixture: StateBoard com fakeredis
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def board():
    """
    StateBoard usando fakeredis.aioredis para isolar os testes do Redis real.
    fakeredis simula o Redis em memória com API idêntica ao redis-py assíncrono.
    """
    import fakeredis.aioredis as fakeredis

    fake_redis_instance = fakeredis.FakeRedis(decode_responses=True)

    b = StateBoard()
    b._client = fake_redis_instance  # injeta o fake diretamente
    yield b
    await b.close()


# ---------------------------------------------------------------------------
# Teste 1 — Write + Read round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_and_read_round_trip(board: StateBoard):
    """
    Uma task escrita por seu agente dono deve ser recuperada com todos
    os campos intactos — incluindo payload arbitrário e metadados.
    """
    task = make_task(
        client_id="cliente_001",
        agent_from="ingestion",
        agent_to="orchestrator",
        payload={"imoveis": 37, "namespace": "cliente_001"},
    )

    await board.write(task, writer_agent="ingestion")

    recovered = await board.read(task.task_id, task.client_id)

    assert recovered.task_id == task.task_id
    assert recovered.client_id == "cliente_001"
    assert recovered.agent_from == "ingestion"
    assert recovered.agent_to == "orchestrator"
    assert recovered.payload["imoveis"] == 37
    assert recovered.status == TaskStatus.PENDING
    assert recovered.iteration == 0
    assert recovered.audit_result is None


# ---------------------------------------------------------------------------
# Teste 2 — HumanEscalationError acima do limite de iterações
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_escalation_error_on_write(board: StateBoard):
    """
    Quando uma task ultrapassa MAX_ITERATIONS, write() deve lançar
    HumanEscalationError em vez de persistir — o orchestrator precisa
    parar de tentar resolver automaticamente.
    """
    task = make_task(
        client_id="cliente_002",
        agent_from="dev_flow",
        agent_to="orchestrator",
        payload={"tentativa": "reenviando"},
    )
    # Força iteration acima do limite
    task.iteration = MAX_ITERATIONS + 1

    with pytest.raises(HumanEscalationError) as exc_info:
        await board.write(task, writer_agent="dev_flow")

    assert exc_info.value.task_id == task.task_id
    assert exc_info.value.client_id == "cliente_002"
    assert exc_info.value.iteration == MAX_ITERATIONS + 1

    # Task não deve ter sido salva no Redis
    with pytest.raises(TaskNotFoundError):
        await board.read(task.task_id, task.client_id)


# ---------------------------------------------------------------------------
# Teste 3 — Concorrência: dois agentes escrevendo tasks distintas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_writes_distinct_tasks(board: StateBoard):
    """
    Dois agentes gravando tasks diferentes simultaneamente não devem
    corromper nenhum registro — cada task deve ser recuperada com
    o payload correto do seu respectivo agente.

    Este é o teste de concorrência exigido pelo CLAUDE.md.
    """
    task_a = make_task(
        client_id="cliente_003",
        agent_from="ingestion",
        agent_to="orchestrator",
        payload={"fonte": "agente_ingestion", "valor": 100},
    )
    task_b = make_task(
        client_id="cliente_003",
        agent_from="context",
        agent_to="orchestrator",
        payload={"fonte": "agente_context", "valor": 200},
    )

    # Dispara as duas escritas simultaneamente
    await asyncio.gather(
        board.write(task_a, writer_agent="ingestion"),
        board.write(task_b, writer_agent="context"),
    )

    recovered_a = await board.read(task_a.task_id, "cliente_003")
    recovered_b = await board.read(task_b.task_id, "cliente_003")

    assert recovered_a.payload["fonte"] == "agente_ingestion"
    assert recovered_a.payload["valor"] == 100
    assert recovered_b.payload["fonte"] == "agente_context"
    assert recovered_b.payload["valor"] == 200

    # Garantir que os IDs não foram trocados
    assert recovered_a.task_id != recovered_b.task_id
    assert recovered_a.agent_from == "ingestion"
    assert recovered_b.agent_from == "context"


# ---------------------------------------------------------------------------
# Teste 4 (bônus) — UnauthorizedWriteError: agente errado não pode escrever
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthorized_write_raises_error(board: StateBoard):
    """
    Se writer_agent != task.agent_from, write() deve lançar
    UnauthorizedWriteError. Nenhum agente escreve pela identidade de outro.
    """
    task = make_task(
        client_id="cliente_004",
        agent_from="memory",       # dono legítimo da task
        agent_to="orchestrator",
        payload={"schema": "lead_v1"},
    )

    with pytest.raises(UnauthorizedWriteError):
        # qa_journeys tenta escrever uma task que pertence a memory
        await board.write(task, writer_agent="qa_journeys")


# ---------------------------------------------------------------------------
# Teste 5 (bônus) — update_audit_result persiste apenas o campo correto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_audit_result_persists_correctly(board: StateBoard):
    """
    update_audit_result() deve atualizar somente o campo audit_result
    sem corromper nenhum outro campo da task já existente.
    """
    task = make_task(
        client_id="cliente_005",
        agent_from="dev_flow",
        agent_to="orchestrator",
        payload={"grafo": "consultor_v1"},
        requires_review=True,
    )
    await board.write(task, writer_agent="dev_flow")

    audit_data = {
        "status": AuditStatus.APPROVED_WITH_NOTE,
        "justification": "Estrutura aprovada com ressalva sobre timeout do tool.",
        "proposed_alternative": "Adicionar retry com backoff exponencial.",
    }

    updated = await board.update_audit_result(
        task_id=task.task_id,
        client_id="cliente_005",
        audit_result=audit_data,
    )

    assert updated.audit_result is not None
    assert updated.audit_result.status == AuditStatus.APPROVED_WITH_NOTE
    assert "timeout" in updated.audit_result.justification
    assert updated.audit_result.proposed_alternative is not None

    # Campos originais não devem ter mudado
    assert updated.payload["grafo"] == "consultor_v1"
    assert updated.requires_review is True
    assert updated.agent_from == "dev_flow"
