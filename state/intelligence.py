"""
state/intelligence.py — State Board Intelligence

Evolução do Redis para que o PO Agent tome decisões baseadas em histórico real:
- Quais tasks foram tentadas e falharam (e quantas vezes)
- Quais tasks foram entregues com sucesso
- Qual foi a última execução do nightly squad
- Score de prioridade dinâmico por task

Complementa board.py — não substitui. Usa prefixo "nightly:" no Redis.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("state.intelligence")

BASE_DIR      = Path(__file__).parent.parent
BACKLOG_FILE  = BASE_DIR / "backlog" / "tasks.json"

# Prefixos Redis
KEY_TASK_HISTORY   = "nightly:task:{task_id}:history"   # lista de execuções
KEY_LAST_RUN       = "nightly:last_run"                  # metadata da última execução
KEY_COMPLETED      = "nightly:completed"                 # set de task_ids concluídas
KEY_FAILED_COUNT   = "nightly:failed:{task_id}:count"   # contador de falhas
KEY_IN_PROGRESS    = "nightly:in_progress"              # task_id atual (se rodando)

TTL_HISTORY   = 60 * 60 * 24 * 30   # 30 dias
TTL_COMPLETED = 60 * 60 * 24 * 90   # 90 dias


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TaskAttempt:
    task_id:    str
    timestamp:  str
    status:     str          # success | failed | vetoed | timeout
    duration_s: float
    pr_url:     str = ""
    error:      str = ""
    agent:      str = "nightly_squad"


@dataclass
class ScoredTask:
    task: dict
    score: float
    reason: str


# ---------------------------------------------------------------------------
# Intelligence class
# ---------------------------------------------------------------------------

class BoardIntelligence:
    """
    Interface de inteligência sobre o histórico do nightly squad.
    Conecta ao Redis quando disponível, cai para modo file-only se Redis estiver offline.
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._local_cache: dict = {}   # fallback em memória

    # ── Conexão Redis opcional ─────────────────────────────────────────────

    @classmethod
    async def create(cls) -> "BoardIntelligence":
        """Factory async que tenta conectar ao Redis."""
        try:
            import redis.asyncio as aioredis
            redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
            client = await aioredis.from_url(redis_url, decode_responses=True)
            await client.ping()
            log.info("BoardIntelligence conectado ao Redis.")
            return cls(redis_client=client)
        except Exception as e:
            log.warning("Redis indisponível (%s) — modo file-only.", e)
            return cls(redis_client=None)

    async def close(self):
        if self._redis:
            await self._redis.aclose()

    # ── Leitura do backlog ─────────────────────────────────────────────────

    def load_backlog(self) -> list[dict]:
        """
        Carrega tasks do backlog/tasks.json.

        REGRA INEGOCIAVEL: erro de leitura ou parse JAMAIS e tratado como
        backlog vazio. Um arquivo corrompido ou ausente e uma falha operacional
        que deve interromper o ciclo e alertar o operador.
        Retornar [] silenciosamente foi o bug que gerou falso alerta de
        "backlog vazio" em 14/04/2026 quando o arquivo estava truncado.
        """
        if not BACKLOG_FILE.exists():
            raise FileNotFoundError(
                "backlog/tasks.json nao encontrado em " + str(BACKLOG_FILE) +
                ". Verifique se o arquivo existe no repositorio."
            )
        try:
            raw = BACKLOG_FILE.read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError("Falha ao ler backlog/tasks.json: " + str(e)) from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "backlog/tasks.json corrompido — JSONDecodeError na posicao " +
                str(e.pos) + ". Restaure o arquivo antes de rodar o Nightly Squad."
            ) from e

        tasks = data.get("tasks", data) if isinstance(data, dict) else data
        if not isinstance(tasks, list):
            raise RuntimeError(
                "backlog/tasks.json formato inesperado: esperado list, encontrado " +
                type(tasks).__name__
            )
        pending = [t for t in tasks if isinstance(t, dict) and t.get("status") not in ("done", "completed", "skip")]
        log.info("Backlog carregado: %d tasks (%d pendentes).", len(tasks), len(pending))
        return tasks

    # ── Histórico de execuções ─────────────────────────────────────────────

    async def get_task_history(self, task_id: str) -> list[TaskAttempt]:
        """Retorna histórico de tentativas de uma task (mais recente primeiro)."""
        key = KEY_TASK_HISTORY.format(task_id=task_id)
        raw = await self._redis_lrange(key, 0, 19) if self._redis else []
        attempts = []
        for item in raw:
            try:
                d = json.loads(item)
                attempts.append(TaskAttempt(**d))
            except Exception:
                pass
        return attempts

    async def get_failed_count(self, task_id: str) -> int:
        """Quantas vezes a task falhou."""
        key = KEY_FAILED_COUNT.format(task_id=task_id)
        if self._redis:
            val = await self._redis.get(key)
            return int(val) if val else 0
        return self._local_cache.get(key, 0)

    async def is_completed(self, task_id: str) -> bool:
        """Verifica se a task já foi entregue com sucesso (PR mergeado)."""
        if self._redis:
            return bool(await self._redis.sismember(KEY_COMPLETED, task_id))
        return task_id in self._local_cache.get("completed", set())

    async def get_last_run(self) -> dict | None:
        """Metadata da última execução completa do nightly squad."""
        if self._redis:
            raw = await self._redis.get(KEY_LAST_RUN)
            if raw:
                return json.loads(raw)
        return self._local_cache.get("last_run")

    # ── Registro de execuções ──────────────────────────────────────────────

    async def record_attempt(self, attempt: TaskAttempt):
        """Registra uma tentativa de execução de task."""
        key = KEY_TASK_HISTORY.format(task_id=attempt.task_id)
        payload = json.dumps({
            "task_id":    attempt.task_id,
            "timestamp":  attempt.timestamp,
            "status":     attempt.status,
            "duration_s": attempt.duration_s,
            "pr_url":     attempt.pr_url,
            "error":      attempt.error,
            "agent":      attempt.agent,
        })
        if self._redis:
            await self._redis.lpush(key, payload)
            await self._redis.ltrim(key, 0, 49)        # mantém últimas 50
            await self._redis.expire(key, TTL_HISTORY)
            if attempt.status == "failed":
                fail_key = KEY_FAILED_COUNT.format(task_id=attempt.task_id)
                await self._redis.incr(fail_key)
                await self._redis.expire(fail_key, TTL_HISTORY)
            elif attempt.status == "success":
                await self._redis.sadd(KEY_COMPLETED, attempt.task_id)
                await self._redis.expire(KEY_COMPLETED, TTL_COMPLETED)
        else:
            hist = self._local_cache.setdefault(key, [])
            hist.insert(0, json.loads(payload))

        log.info("Tentativa registrada: task=%s status=%s", attempt.task_id, attempt.status)

    async def save_run_metadata(self, metadata: dict):
        """Salva metadata da execução completa (usado pelo Briefing Agent)."""
        payload = json.dumps({**metadata, "saved_at": datetime.now(timezone.utc).isoformat()})
        if self._redis:
            await self._redis.set(KEY_LAST_RUN, payload, ex=TTL_HISTORY)
        self._local_cache["last_run"] = json.loads(payload)

    async def set_in_progress(self, task_id: str | None):
        """Marca qual task está em execução agora (para detectar crashes)."""
        if self._redis:
            if task_id:
                await self._redis.set(KEY_IN_PROGRESS, task_id, ex=3600)
            else:
                await self._redis.delete(KEY_IN_PROGRESS)

    async def get_in_progress(self) -> str | None:
        if self._redis:
            return await self._redis.get(KEY_IN_PROGRESS)
        return None

    # ── Priorização inteligente de tasks ──────────────────────────────────

    async def prioritize_tasks(self, max_tasks: int = 3) -> list[ScoredTask]:
        """
        Aplica score a cada task do backlog e retorna as top-N priorizadas.

        Critérios de score (maior = mais prioritário):
            + base_priority da task (definido no tasks.json)
            + 5 pts se nunca foi tentada
            - 2 pts por falha anterior (até -10)
            - 100 pts se já completada (efetivamente remove da lista)
            + 3 pts se tem dependências satisfeitas
            + 2 pts se marcada como "blocker" de outra task
        """
        tasks = self.load_backlog()
        scored: list[ScoredTask] = []

        for task in tasks:
            tid = task.get("id", "")
            if not tid:
                continue

            # Já completada → skip
            if await self.is_completed(tid):
                continue

            # Skip tasks com status "skip" ou "blocked"
            if task.get("status") in ("skip", "blocked", "done"):
                continue

            raw_priority = task.get("priority", 5)
            score = 10.0 if raw_priority == "critical" else float(raw_priority)
            reasons = []

            # Nunca tentada → bônus
            history = await self.get_task_history(tid)
            if not history:
                score += 5
                reasons.append("+5 nunca tentada")
            else:
                # Penalidade por falhas anteriores
                fail_count = await self.get_failed_count(tid)
                penalty = min(fail_count * 2, 10)
                score -= penalty
                if penalty:
                    reasons.append(f"-{penalty} ({fail_count} falha(s))")

            # Dependências satisfeitas
            deps = task.get("depends_on", [])
            if deps:
                completed_deps = [await self.is_completed(dep) for dep in deps]
                deps_ok = all(completed_deps)
            else:
                deps_ok = True

            if deps and not deps_ok:
                score -= 50    # dependência não satisfeita → vai para o final
                reasons.append("-50 deps pendentes")
            elif deps:
                score += 3
                reasons.append("+3 deps ok")

            # Marcada como blocker
            if task.get("blocks"):
                score += 2
                reasons.append("+2 blocker")

            reason_str = ", ".join(reasons) if reasons else "score base"
            scored.append(ScoredTask(task=task, score=score, reason=f"{score:.0f}pts ({reason_str})"))

        # Ordena por score desc
        scored.sort(key=lambda s: s.score, reverse=True)

        selected = scored[:max_tasks]
        log.info("Tasks priorizadas: %s",
                 [(s.task["id"], s.score) for s in selected])
        return selected

    # ── Redis helpers ──────────────────────────────────────────────────────

    async def _redis_lrange(self, key: str, start: int, end: int) -> list[str]:
        if not self._redis:
            return []
        try:
            return await self._redis.lrange(key, start, end)
        except Exception as e:
            log.warning("Redis lrange falhou: %s", e)
            return []
