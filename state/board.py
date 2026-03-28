"""
state/board.py — Leitura e escrita no Redis shared state board.

Regras arquiteturais (CLAUDE.md):
  - Nenhum agente lê/escreve no Redis diretamente — tudo passa por este módulo
  - Lock por task_id previne race condition entre agentes concorrentes
  - TTL de 24h por padrão: tasks antigas não ficam no Redis indefinidamente
  - iteration > MAX_ITERATIONS → write é bloqueado e HumanEscalationError é lançado

Uso:
    board = StateBoard(redis_url="redis://localhost:6379")
    await board.connect()

    task = make_task("cliente_001", "ingestion", "orchestrator", payload={...})
    await board.write(task, writer_agent="ingestion")

    retrieved = await board.read(task.task_id, task.client_id)
    await board.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import LockError

from state.schema import MAX_ITERATIONS, TaskMessage, TaskStatus, make_task

logger = logging.getLogger(__name__)

# TTL padrão das tasks no Redis (segundos)
TASK_TTL_SECONDS = 86_400  # 24 horas

# Timeout do lock distribuído (segundos)
LOCK_TIMEOUT_SECONDS = 10

# Prefixo do lock no Redis
LOCK_PREFIX = "lock:task"


# ---------------------------------------------------------------------------
# Exceções de domínio
# ---------------------------------------------------------------------------


class HumanEscalationError(Exception):
    """
    Lançada quando iteration > MAX_ITERATIONS na mesma task.
    O orchestrator deve interromper tentativas automáticas e
    notificar o operador humano.
    """

    def __init__(self, task_id: str, client_id: str, iteration: int):
        self.task_id = task_id
        self.client_id = client_id
        self.iteration = iteration
        super().__init__(
            f"Task '{task_id}' (cliente '{client_id}') atingiu {iteration} iterações "
            f"(máximo {MAX_ITERATIONS}). Escalando para revisão humana."
        )


class UnauthorizedWriteError(Exception):
    """
    Lançada quando um agente tenta escrever um campo que não é seu.
    Camada extra de enforcement além da validação do schema.
    """


class TaskNotFoundError(Exception):
    """Lançada quando a task_id + client_id não existe no Redis."""


# ---------------------------------------------------------------------------
# StateBoard
# ---------------------------------------------------------------------------


class StateBoard:
    """
    Interface centralizada para o shared state board no Redis.

    Todos os agentes usam esta classe — nunca o cliente Redis diretamente.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        task_ttl: int = TASK_TTL_SECONDS,
        lock_timeout: int = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        # REDIS_URL do ambiente tem prioridade; fallback usa 127.0.0.1 (não
        # localhost) para evitar resolução IPv6 em containers Docker.
        self._redis_url = redis_url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
        self._task_ttl = task_ttl
        self._lock_timeout = lock_timeout
        self._client: Redis | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Abre conexão assíncrona com o Redis."""
        self._client = await aioredis.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        logger.info("StateBoard conectado ao Redis: %s", self._redis_url)

    async def close(self) -> None:
        """Fecha conexão com o Redis."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("StateBoard desconectado do Redis.")

    @asynccontextmanager
    async def _lock(self, task_id: str) -> AsyncIterator[None]:
        """
        Lock distribuído por task_id usando SET NX PX (sem Lua scripts).

        Usa SET key token NX PX timeout — compatível com Redis e fakeredis.
        Previne race condition quando dois agentes escrevem na mesma task.
        """
        if not self._client:
            raise RuntimeError("StateBoard não conectado. Chame await board.connect() primeiro.")

        import uuid as _uuid

        lock_key = f"{LOCK_PREFIX}:{task_id}"
        token = str(_uuid.uuid4())
        timeout_ms = int(self._lock_timeout * 1000)
        deadline = asyncio.get_event_loop().time() + self._lock_timeout

        # Tenta adquirir o lock com polling simples
        while True:
            acquired = await self._client.set(
                lock_key, token, nx=True, px=timeout_ms
            )
            if acquired:
                break
            if asyncio.get_event_loop().time() >= deadline:
                raise LockError(f"Não foi possível adquirir lock para task '{task_id}' em {self._lock_timeout}s.")
            await asyncio.sleep(0.05)

        try:
            yield
        finally:
            # Libera somente se o token ainda é o nosso (evita liberar lock alheio)
            current = await self._client.get(lock_key)
            if current == token:
                await self._client.delete(lock_key)
            else:
                logger.warning(
                    "Lock para task '%s' expirou ou foi adquirido por outro processo antes da liberação.",
                    task_id,
                )

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    async def write(
        self,
        task: TaskMessage,
        writer_agent: str,
    ) -> None:
        """
        Persiste uma TaskMessage no Redis com lock por task_id.

        Args:
            task: A mensagem a ser salva.
            writer_agent: Nome do agente que está escrevendo.
                          Deve coincidir com task.agent_from.

        Raises:
            HumanEscalationError: Se iteration > MAX_ITERATIONS.
            UnauthorizedWriteError: Se writer_agent != task.agent_from.
            RuntimeError: Se o board não estiver conectado.
        """
        if not self._client:
            raise RuntimeError("StateBoard não conectado.")

        if writer_agent != task.agent_from:
            raise UnauthorizedWriteError(
                f"Agente '{writer_agent}' tentou escrever uma task "
                f"declarada como de '{task.agent_from}'. "
                "agent_from deve coincidir com o agente que está escrevendo."
            )

        if task.requires_human_escalation:
            raise HumanEscalationError(task.task_id, task.client_id, task.iteration)

        async with self._lock(task.task_id):
            await self._client.setex(
                task.redis_key,
                self._task_ttl,
                task.to_redis(),
            )
            logger.debug(
                "Task '%s' escrita por '%s' (status=%s, iteration=%d).",
                task.task_id,
                writer_agent,
                task.status,
                task.iteration,
            )

    async def update_audit_result(
        self,
        task_id: str,
        client_id: str,
        audit_result: dict,
    ) -> TaskMessage:
        """
        Atualiza exclusivamente o campo audit_result de uma task existente.
        Operação reservada ao agente Auditor.

        Args:
            task_id: ID da task a ser auditada.
            client_id: ID do cliente (namespace).
            audit_result: Dict compatível com AuditResult.

        Returns:
            TaskMessage atualizada.
        """
        from state.schema import AuditResult  # import local para evitar ciclo

        async with self._lock(task_id):
            task = await self._read_raw(task_id, client_id)
            task.audit_result = AuditResult(**audit_result)
            await self._client.setex(task.redis_key, self._task_ttl, task.to_redis())
            logger.info(
                "audit_result atualizado para task '%s' (status_auditoria=%s).",
                task_id,
                task.audit_result.status,
            )
            return task

    async def update_status(
        self,
        task_id: str,
        client_id: str,
        new_status: TaskStatus,
        writer_agent: str,
        error: str | None = None,
    ) -> TaskMessage:
        """
        Atualiza apenas o status de uma task existente, com validação de autoridade.

        Args:
            task_id: ID da task.
            client_id: ID do cliente.
            new_status: Novo status desejado.
            writer_agent: Agente que está fazendo a atualização.
            error: Obrigatório se new_status == BLOCKED.

        Returns:
            TaskMessage atualizada.
        """
        async with self._lock(task_id):
            task = await self._read_raw(task_id, client_id)
            task.agent_from = writer_agent
            task.status = new_status
            if error:
                task.error = error
            await self._client.setex(task.redis_key, self._task_ttl, task.to_redis())
            return task

    async def increment_iteration(
        self,
        task_id: str,
        client_id: str,
    ) -> TaskMessage:
        """
        Incrementa o contador de iterações de uma task.
        Se ultrapassar MAX_ITERATIONS, lança HumanEscalationError.
        """
        async with self._lock(task_id):
            task = await self._read_raw(task_id, client_id)
            task.iteration += 1
            if task.requires_human_escalation:
                raise HumanEscalationError(task_id, client_id, task.iteration)
            await self._client.setex(task.redis_key, self._task_ttl, task.to_redis())
            logger.warning(
                "Task '%s' chegou à iteração %d (máximo %d).",
                task_id,
                task.iteration,
                MAX_ITERATIONS,
            )
            return task

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    async def read(self, task_id: str, client_id: str) -> TaskMessage:
        """
        Lê uma TaskMessage do Redis.

        Raises:
            TaskNotFoundError: Se a task não existir.
        """
        return await self._read_raw(task_id, client_id)

    async def _read_raw(self, task_id: str, client_id: str) -> TaskMessage:
        """Leitura interna sem lock — use dentro de blocos _lock()."""
        if not self._client:
            raise RuntimeError("StateBoard não conectado.")

        key = f"task:{client_id}:{task_id}"
        raw = await self._client.get(key)

        if raw is None:
            raise TaskNotFoundError(
                f"Task '{task_id}' para cliente '{client_id}' não encontrada no Redis."
            )

        return TaskMessage.from_redis(raw)

    async def list_tasks(
        self,
        client_id: str,
        status: TaskStatus | None = None,
    ) -> list[TaskMessage]:
        """
        Lista todas as tasks de um cliente, com filtro opcional por status.

        Usa SCAN para não bloquear o Redis em produção.
        """
        if not self._client:
            raise RuntimeError("StateBoard não conectado.")

        pattern = f"task:{client_id}:*"
        tasks: list[TaskMessage] = []

        async for key in self._client.scan_iter(pattern):
            raw = await self._client.get(key)
            if raw:
                try:
                    task = TaskMessage.from_redis(raw)
                    if status is None or task.status == status:
                        tasks.append(task)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Erro ao desserializar task '%s': %s", key, exc)

        return tasks

    async def delete(self, task_id: str, client_id: str) -> bool:
        """
        Remove uma task do Redis.
        Retorna True se a task existia, False caso contrário.
        """
        if not self._client:
            raise RuntimeError("StateBoard não conectado.")

        key = f"task:{client_id}:{task_id}"
        result = await self._client.delete(key)
        return result > 0

    # ------------------------------------------------------------------
    # Healthcheck
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """Verifica se o Redis está respondendo."""
        if not self._client:
            return False
        try:
            return await self._client.ping()
        except Exception:  # noqa: BLE001
            return False
