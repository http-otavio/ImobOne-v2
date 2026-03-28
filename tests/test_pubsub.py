"""
tests/test_pubsub.py — Testes unitários de state/pubsub.py.

Mínimo exigido: 3 testes antes de qualquer integração (CLAUDE.md).
Todos os testes usam fakeredis para rodar sem Redis real.

Cobertura:
  1. Publish + Subscribe: mensagem publicada deve ser recebida pelo handler
  2. channel_for: canal gerado deve seguir o padrão "agent:{nome}"
  3. Agente errado não recebe mensagem destinada a outro canal
  (Bônus) 4. broadcast: mensagem chega em múltiplos canais
  (Bônus) 5. Desserialização inválida não derruba o loop — handler não é chamado
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from state.pubsub import AgentPubSub, broadcast, channel_for
from state.schema import TaskMessage, TaskStatus, make_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drain(pubsub: AgentPubSub, timeout: float = 0.5) -> None:
    """Aguarda o loop de escuta processar mensagens pendentes."""
    await asyncio.sleep(timeout)
    await pubsub.close()


# ---------------------------------------------------------------------------
# Fixture: par de AgentPubSub com fakeredis
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis_server():
    """Servidor fakeredis compartilhado entre publisher e subscriber."""
    import fakeredis.aioredis as fakeredis

    server = fakeredis.FakeServer()
    yield server


@pytest_asyncio.fixture
async def publisher(fake_redis_server):
    """AgentPubSub configurado como orchestrator (publicador)."""
    import fakeredis.aioredis as fakeredis

    pub = AgentPubSub("orchestrator")
    pub._publisher = fakeredis.FakeRedis(server=fake_redis_server, decode_responses=True)
    yield pub
    if pub._publisher:
        await pub._publisher.aclose()


@pytest_asyncio.fixture
async def subscriber(fake_redis_server):
    """AgentPubSub configurado como ingestion (receptor)."""
    import fakeredis.aioredis as fakeredis

    sub = AgentPubSub("ingestion")
    sub._subscriber = fakeredis.FakeRedis(server=fake_redis_server, decode_responses=True)
    sub._publisher = fakeredis.FakeRedis(server=fake_redis_server, decode_responses=True)
    yield sub
    await sub.close()


# ---------------------------------------------------------------------------
# Teste 1 — Publish + Subscribe end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_and_subscribe_round_trip(publisher, subscriber):
    """
    Uma TaskMessage publicada pelo orchestrator deve ser recebida
    e desserializada corretamente pelo handler do agente destinatário.
    """
    received: list[TaskMessage] = []

    async def handler(task: TaskMessage) -> None:
        received.append(task)

    # Subscriber escuta no seu próprio canal: "agent:ingestion"
    await subscriber.subscribe(handler, channel=channel_for("ingestion"))

    task = make_task(
        client_id="cliente_001",
        agent_from="orchestrator",
        agent_to="ingestion",
        payload={"acao": "processar_portfolio", "arquivo": "imoveis.csv"},
    )

    # Publica no canal "agent:ingestion"
    await publisher.publish(task)

    # Aguarda processamento
    await asyncio.sleep(0.3)
    await subscriber.close()

    assert len(received) == 1, f"Esperava 1 mensagem, recebeu {len(received)}"
    msg = received[0]
    assert msg.task_id == task.task_id
    assert msg.agent_from == "orchestrator"
    assert msg.agent_to == "ingestion"
    assert msg.payload["acao"] == "processar_portfolio"


# ---------------------------------------------------------------------------
# Teste 2 — channel_for retorna o padrão correto
# ---------------------------------------------------------------------------


def test_channel_for_returns_correct_pattern():
    """
    channel_for deve seguir estritamente o padrão "agent:{nome}".
    Todos os agentes devem poder derivar seu canal deterministicamente.
    """
    assert channel_for("orchestrator") == "agent:orchestrator"
    assert channel_for("ingestion") == "agent:ingestion"
    assert channel_for("auditor") == "agent:auditor"
    assert channel_for("qa_journeys") == "agent:qa_journeys"
    assert channel_for("monitor") == "agent:monitor"

    # Canal deve ser idempotente
    assert channel_for("dev_flow") == channel_for("dev_flow")

    # Canal não deve conter espaços ou caracteres especiais problemáticos
    for agent in ["orchestrator", "dev_flow", "qa_integration"]:
        ch = channel_for(agent)
        assert " " not in ch
        assert ch.startswith("agent:")


# ---------------------------------------------------------------------------
# Teste 3 — Subscriber não recebe mensagem de canal diferente
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscriber_ignores_different_channel(publisher, subscriber, fake_redis_server):
    """
    Um agente subscrito em "agent:ingestion" NÃO deve receber
    mensagens publicadas em "agent:context" — isolamento de canal é crítico
    para evitar que agentes processem tasks que não são suas.
    """
    import fakeredis.aioredis as fakeredis

    received: list[TaskMessage] = []

    async def handler(task: TaskMessage) -> None:
        received.append(task)

    # ingestion escuta SEU canal
    await subscriber.subscribe(handler, channel=channel_for("ingestion"))

    # Publica no canal de OUTRO agente (context)
    task_for_context = make_task(
        client_id="cliente_002",
        agent_from="orchestrator",
        agent_to="context",      # destinado ao context, não ao ingestion
        payload={"acao": "validar_maps"},
    )

    # Usa publisher direto para publicar no canal errado
    context_publisher = fakeredis.FakeRedis(server=fake_redis_server, decode_responses=True)
    await context_publisher.publish(channel_for("context"), task_for_context.to_redis())
    await context_publisher.aclose()

    await asyncio.sleep(0.3)
    await subscriber.close()

    assert len(received) == 0, (
        f"ingestion não deveria ter recebido mensagem do canal 'context', "
        f"mas recebeu {len(received)}"
    )


# ---------------------------------------------------------------------------
# Teste 4 (bônus) — broadcast entrega para múltiplos canais
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_delivers_to_multiple_channels(publisher, fake_redis_server):
    """
    broadcast() deve publicar a task no canal do destinatário
    E nos canais extras informados — útil para o orchestrator
    notificar múltiplos agentes com um único comando.

    Estratégia: usa handlers assíncronos via subscribe() em vez de
    get_message() diretamente, para evitar race conditions de timing.
    """
    import fakeredis.aioredis as fakeredis

    received_ingestion: list[TaskMessage] = []
    received_context: list[TaskMessage] = []

    async def handler_ingestion(task: TaskMessage) -> None:
        received_ingestion.append(task)

    async def handler_context(task: TaskMessage) -> None:
        received_context.append(task)

    # Cria dois subscribers reais usando AgentPubSub
    sub_ing = AgentPubSub("ingestion")
    sub_ing._subscriber = fakeredis.FakeRedis(server=fake_redis_server, decode_responses=True)
    sub_ing._publisher = fakeredis.FakeRedis(server=fake_redis_server, decode_responses=True)

    sub_ctx = AgentPubSub("context")
    sub_ctx._subscriber = fakeredis.FakeRedis(server=fake_redis_server, decode_responses=True)
    sub_ctx._publisher = fakeredis.FakeRedis(server=fake_redis_server, decode_responses=True)

    await sub_ing.subscribe(handler_ingestion, channel=channel_for("ingestion"))
    await sub_ctx.subscribe(handler_context, channel=channel_for("context"))

    task = make_task(
        client_id="cliente_003",
        agent_from="orchestrator",
        agent_to="ingestion",
        payload={"fase": "inicio_setup"},
    )

    # Publica no destinatário principal + canal extra
    results = await broadcast(
        publisher,
        task,
        extra_channels=[channel_for("context")],
    )

    # Aguarda processamento pelos loops de escuta
    await asyncio.sleep(0.4)

    await sub_ing.close()
    await sub_ctx.close()

    assert len(received_ingestion) == 1, (
        f"ingestion deveria ter recebido 1 mensagem, recebeu {len(received_ingestion)}"
    )
    assert len(received_context) == 1, (
        f"context deveria ter recebido 1 mensagem via broadcast, recebeu {len(received_context)}"
    )
    assert received_ingestion[0].task_id == task.task_id
    assert received_context[0].task_id == task.task_id
    assert channel_for("ingestion") in results
    assert channel_for("context") in results


# ---------------------------------------------------------------------------
# Teste 5 (bônus) — Desserialização inválida não derruba o loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_message_does_not_crash_loop(publisher, subscriber):
    """
    Se uma mensagem corrompida ou inválida chegar no canal,
    o loop de escuta deve logar o erro e continuar — sem derrubar o agente.
    A mensagem válida enviada logo depois deve ser processada normalmente.
    """
    received: list[TaskMessage] = []

    async def handler(task: TaskMessage) -> None:
        received.append(task)

    await subscriber.subscribe(handler, channel=channel_for("ingestion"))

    # Publica mensagem corrompida (JSON inválido)
    await publisher.publish_raw(channel_for("ingestion"), "ISSO_NAO_E_JSON_VALIDO!!!")

    # Publica mensagem válida logo depois
    valid_task = make_task(
        client_id="cliente_005",
        agent_from="orchestrator",
        agent_to="ingestion",
        payload={"status": "ok"},
    )
    await publisher.publish(valid_task)

    await asyncio.sleep(0.4)
    await subscriber.close()

    # Loop sobreviveu — mensagem válida foi processada
    assert len(received) == 1, (
        f"Esperava 1 mensagem válida após mensagem corrompida, recebeu {len(received)}"
    )
    assert received[0].task_id == valid_task.task_id
