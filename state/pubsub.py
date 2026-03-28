"""
state/pubsub.py — Pub/sub entre agentes via Redis.

Cada agente tem um canal próprio: "agent:{nome}".
O orquestrador pode publicar em qualquer canal.
Agentes de execução publicam de volta para "agent:orchestrator".

Protocolo:
  - Publicar: AgentPubSub.publish(task) → serializa e publica no canal do destinatário
  - Subscrever: AgentPubSub.subscribe(agent_name, handler) → loop assíncrono de escuta
  - Parar: AgentPubSub.stop() → cancela o loop de escuta com graceful shutdown

Design:
  - Um AgentPubSub por agente (ou um central compartilhado pelo orchestrator)
  - Mensagens são TaskMessage JSON — o mesmo schema do board.py
  - Reconnect automático em caso de queda do Redis
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from state.schema import TaskMessage

logger = logging.getLogger(__name__)

# Tipo do handler de mensagens recebidas
MessageHandler = Callable[[TaskMessage], Awaitable[None]]

# Prefixo de canal por agente
AGENT_CHANNEL_PREFIX = "agent"

# Intervalo de reconexão em segundos
RECONNECT_INTERVAL_SECONDS = 3.0

# Timeout de leitura do pubsub (evita busy-wait)
PUBSUB_READ_TIMEOUT_SECONDS = 1.0


def channel_for(agent_name: str) -> str:
    """Retorna o nome canônico do canal pub/sub de um agente."""
    return f"{AGENT_CHANNEL_PREFIX}:{agent_name}"


# ---------------------------------------------------------------------------
# AgentPubSub
# ---------------------------------------------------------------------------


class AgentPubSub:
    """
    Interface de pub/sub para um agente específico.

    Cada instância representa a conexão pub/sub de um único agente.
    Para publicar em múltiplos canais, use a mesma instância — o
    cliente Redis de publicação é compartilhado.
    """

    def __init__(
        self,
        agent_name: str,
        redis_url: str | None = None,
        reconnect_interval: float = RECONNECT_INTERVAL_SECONDS,
    ) -> None:
        self.agent_name = agent_name
        self._redis_url = redis_url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
        self._reconnect_interval = reconnect_interval

        self._publisher: Redis | None = None
        self._subscriber: Redis | None = None
        self._pubsub: PubSub | None = None
        self._listen_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Abre conexões dedicadas para publicação e subscrição."""
        # Redis recomenda conexões separadas para pub/sub
        self._publisher = await aioredis.from_url(
            self._redis_url, encoding="utf-8", decode_responses=True
        )
        self._subscriber = await aioredis.from_url(
            self._redis_url, encoding="utf-8", decode_responses=True
        )
        logger.info(
            "AgentPubSub '%s' conectado ao Redis: %s",
            self.agent_name,
            self._redis_url,
        )

    async def close(self) -> None:
        """Para o loop de escuta e fecha as conexões."""
        self._running = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.aclose()

        if self._publisher:
            await self._publisher.aclose()
        if self._subscriber:
            await self._subscriber.aclose()

        logger.info("AgentPubSub '%s' desconectado.", self.agent_name)

    # ------------------------------------------------------------------
    # Publicação
    # ------------------------------------------------------------------

    async def publish(self, task: TaskMessage) -> int:
        """
        Publica uma TaskMessage no canal do agente destinatário.

        Args:
            task: Mensagem a publicar. task.channel define o destino.

        Returns:
            Número de subscribers que receberam a mensagem.

        Raises:
            RuntimeError: Se não estiver conectado.
        """
        if not self._publisher:
            raise RuntimeError(
                f"AgentPubSub '{self.agent_name}' não conectado. "
                "Chame await pubsub.connect() primeiro."
            )

        channel = task.channel
        message = task.to_redis()
        receivers = await self._publisher.publish(channel, message)

        logger.debug(
            "Publicado em '%s' por '%s' (task_id=%s, receivers=%d).",
            channel,
            self.agent_name,
            task.task_id,
            receivers,
        )
        return receivers

    async def publish_raw(self, channel: str, message: str) -> int:
        """
        Publica uma string arbitrária em um canal.
        Útil para mensagens de controle (ping, shutdown).
        """
        if not self._publisher:
            raise RuntimeError(f"AgentPubSub '{self.agent_name}' não conectado.")
        return await self._publisher.publish(channel, message)

    # ------------------------------------------------------------------
    # Subscrição
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        handler: MessageHandler,
        channel: str | None = None,
    ) -> None:
        """
        Inicia o loop de escuta no canal do agente (ou canal customizado).

        Args:
            handler: Coroutine chamada para cada TaskMessage recebida.
            channel: Canal a escutar. Default: canal próprio do agente.
        """
        if not self._subscriber:
            raise RuntimeError(f"AgentPubSub '{self.agent_name}' não conectado.")

        target_channel = channel or channel_for(self.agent_name)
        self._pubsub = self._subscriber.pubsub()
        await self._pubsub.subscribe(target_channel)
        self._running = True

        logger.info(
            "AgentPubSub '%s' escutando canal '%s'.",
            self.agent_name,
            target_channel,
        )

        self._listen_task = asyncio.create_task(
            self._listen_loop(handler, target_channel),
            name=f"pubsub-{self.agent_name}",
        )

    async def _listen_loop(
        self,
        handler: MessageHandler,
        channel: str,
    ) -> None:
        """
        Loop interno de escuta com reconexão automática.
        Roda até self._running == False.
        """
        while self._running:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=PUBSUB_READ_TIMEOUT_SECONDS,
                )

                if message and message.get("type") == "message":
                    raw = message.get("data", "")
                    await self._dispatch(raw, handler, channel)

            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Erro no loop de escuta de '%s' no canal '%s': %s. "
                    "Reconectando em %.1fs...",
                    self.agent_name,
                    channel,
                    exc,
                    self._reconnect_interval,
                )
                await asyncio.sleep(self._reconnect_interval)

    async def _dispatch(
        self,
        raw: str,
        handler: MessageHandler,
        channel: str,
    ) -> None:
        """Desserializa a mensagem e chama o handler."""
        try:
            task = TaskMessage.from_redis(raw)
            await handler(task)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Erro ao processar mensagem no canal '%s': %s. Raw: %.200s",
                channel,
                exc,
                raw,
            )

    # ------------------------------------------------------------------
    # Utilitários
    # ------------------------------------------------------------------

    async def wait_until_done(self) -> None:
        """Aguarda o término do loop de escuta (útil em testes)."""
        if self._listen_task:
            await self._listen_task

    @property
    def is_listening(self) -> bool:
        """True se o loop de escuta está ativo."""
        return (
            self._running
            and self._listen_task is not None
            and not self._listen_task.done()
        )


# ---------------------------------------------------------------------------
# Broadcast — publicação para múltiplos canais
# ---------------------------------------------------------------------------


async def broadcast(
    publisher: AgentPubSub,
    task: TaskMessage,
    extra_channels: list[str] | None = None,
) -> dict[str, int]:
    """
    Publica uma task no canal do destinatário e opcionalmente em canais extras.
    Útil para o orchestrator notificar múltiplos agentes.

    Returns:
        Dict de canal → número de subscribers que receberam.
    """
    results: dict[str, int] = {}
    results[task.channel] = await publisher.publish(task)

    for ch in extra_channels or []:
        results[ch] = await publisher.publish_raw(ch, task.to_redis())

    return results
