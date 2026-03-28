"""
agents/orchestrator.py — Agente 1: Orquestrador Master

Implementa o grafo LangGraph que governa o pipeline completo de setup
de um novo cliente. É o único agente com autoridade para setar
status: approved ou deploy_ready.

Fluxo do grafo:
  START
    → initialize_pipeline       valida onboarding, registra client_id
    → dispatch_phase1           paralelo: ingestion + dev_persona + memory + context
    → [route_phase1]
        se blocked_agents  → handle_escalation → END
        senão              → build_consultant
    → build_consultant          dev_flow: grafo de conversação do consultor
    → audit_decisions           auditor: CoT adversarial sobre todas as entregas
    → [route_audit]
        se vetoed          → handle_escalation → END
        senão              → run_qa
    → run_qa                    paralelo: qa_journeys + qa_integration
    → final_gate                gate exclusivo: deploy_ready ou retrabalho
    → [route_gate]
        se deploy_ready    → activate_monitor → END
        senão              → handle_escalation → END

Regras de mock (Fase 1):
  Cada nó de agente executa a função registrada em `mock_agents`.
  O contrato de uma mock_fn é:
      async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]
          retorna (status: "done" | "blocked", payload: dict)

  Em produção, os nós dispararão tasks no Redis e aguardarão via pub/sub.
  Os mocks isolam o grafo para validação de fluxo antes de conectar os agentes reais.
"""

from __future__ import annotations

import asyncio
import logging
import operator
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import TypedDict

from state.board import HumanEscalationError, StateBoard
from state.pubsub import AgentPubSub
from state.schema import MAX_ITERATIONS, TaskStatus, make_task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

# Contrato de uma função mock: recebe client_id e onboarding, retorna (status, payload)
MockAgentFn = Callable[[str, dict], Awaitable[tuple[str, dict]]]


def _merge_dicts(a: dict, b: dict) -> dict:
    """Reducer para campos dict no estado — faz merge, não replace."""
    return {**(a or {}), **(b or {})}


# ---------------------------------------------------------------------------
# Estado do orquestrador
# ---------------------------------------------------------------------------


class OrchestratorState(TypedDict):
    """
    Estado compartilhado que trafega por todos os nós do grafo.

    Campos com Annotated + reducer são acumulados entre nós.
    Campos simples são substituídos pelo valor mais recente.
    """

    # Identificação
    client_id: str
    onboarding: dict[str, Any]

    # Rastreamento de tasks (merge entre fases)
    task_map: Annotated[dict[str, str], _merge_dicts]       # agent_name → task_id
    agent_results: Annotated[dict[str, Any], _merge_dicts]  # agent_name → payload

    # Erros e bloqueios (acumulam entre fases)
    blocked_agents: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]

    # Resultados de QA
    qa_journeys_score: float        # 0.0 a 1.0 (limiar: 0.85)
    qa_integration_passed: bool

    # Resultado de auditoria
    audit_status: str               # "pending" | "approved" | "approved_with_note" | "vetoed"

    # Controle de pipeline
    phase: str
    deploy_status: str              # "pending" | "deploy_ready" | "failed" | "human_review"
    rework_count: int


# Limiar mínimo de aprovação no QA de jornadas (CLAUDE.md)
QA_JOURNEYS_THRESHOLD = 0.85

# Agentes que rodam em paralelo na Fase 1
PHASE1_AGENTS = ["ingestion", "dev_persona", "memory", "context"]

# Agentes que rodam em paralelo no QA
QA_AGENTS = ["qa_journeys", "qa_integration"]


# ---------------------------------------------------------------------------
# Mocks padrão (retornam sucesso simulado)
# ---------------------------------------------------------------------------


def _make_success_mock(payload: dict | None = None) -> MockAgentFn:
    """Factory de mock que sempre retorna done com payload fixo."""
    _payload = payload or {"mock": True, "status": "success"}

    async def _mock(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return "done", {**_payload, "client_id": client_id}

    return _mock


def _default_mock_agents() -> dict[str, MockAgentFn]:
    """
    Mocks padrão para todos os agentes — usados quando não há override.
    Todos retornam sucesso com dados mínimos para o pipeline avançar.
    """

    async def mock_auditor(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return "done", {
            "audit_status": "approved",
            "justification": "[MOCK] Arquitetura aprovada sem ressalvas.",
            "proposed_alternative": None,
        }

    async def mock_qa_journeys(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return "done", {
            "score": 0.92,
            "approved": 18,
            "total": 20,
            "failures": [],
        }

    async def mock_qa_integration(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return "done", {
            "passed": True,
            "latency_ok": True,
            "checks": {
                "whatsapp": "ok",
                "elevenlabs": "ok",
                "google_places": "ok",
                "supabase_pgvector": "ok",
                "crm_webhook": "ok",
            },
        }

    return {
        "ingestion": _make_success_mock({"imoveis_indexados": 42}),
        "dev_persona": _make_success_mock({"persona_yaml": "mock_persona.yaml"}),
        "memory": _make_success_mock({"schema": "lead_v1", "crm_webhook": "configured"}),
        "context": _make_success_mock({"google_places": "validated", "distance_matrix": "validated"}),
        "dev_flow": _make_success_mock({"grafo": "consultor_v1", "tools": 7}),
        "auditor": mock_auditor,
        "qa_journeys": mock_qa_journeys,
        "qa_integration": mock_qa_integration,
        "monitor": _make_success_mock({"monitoring": "active"}),
    }


# ---------------------------------------------------------------------------
# OrchestratorAgent
# ---------------------------------------------------------------------------


class OrchestratorAgent:
    """
    Agente 1 — Orquestrador Master.

    Não executa tarefas de domínio — planeja, delega, consolida e decide deploy.
    É o único agente com autoridade para setar status: approved ou deploy_ready.

    Args:
        board: StateBoard conectado ao Redis.
        pubsub: AgentPubSub do orchestrator (para publicar tasks).
        mock_agents: Dict agent_name → MockAgentFn. Se None, usa mocks padrão.
                     Útil em testes para injetar comportamentos específicos.
    """

    def __init__(
        self,
        board: StateBoard,
        pubsub: AgentPubSub,
        mock_agents: dict[str, MockAgentFn] | None = None,
    ) -> None:
        self.board = board
        self.pubsub = pubsub
        self._mock_agents: dict[str, MockAgentFn] = {
            **_default_mock_agents(),
            **(mock_agents or {}),
        }
        # Contadores de iteração por agente/cliente: f"{agent_name}:{client_id}" → int
        #
        # Isolamento: a chave inclui client_id — clientes distintos nunca compartilham
        # contadores, independentemente de quantos run()s aconteçam na mesma instância.
        #
        # Persistência intencional: o contador sobrevive entre chamadas de run() para o
        # MESMO cliente. Se ingestion bloqueia no run #1 (counter=1) e run #2 é tentado,
        # ele começa com counter=1, não 0. Isso é o comportamento correto — o CLAUDE.md
        # exige que iteration > MAX_ITERATIONS escale para revisão humana precisamente
        # para detectar falhas repetidas entre tentativas de setup do mesmo cliente.
        #
        # Para resetar o estado de um cliente específico após intervenção humana:
        #   orchestrator.reset_client(client_id)
        self._iteration_counts: dict[str, int] = {}
        self._counts_lock = asyncio.Lock()  # protege leitura+escrita concorrente por corrotina

        self.graph: CompiledStateGraph = self._build_graph()

    # ------------------------------------------------------------------
    # Gerenciamento de estado de iteração
    # ------------------------------------------------------------------

    def reset_client(self, client_id: str) -> None:
        """
        Zera os contadores de iteração de todos os agentes para um cliente.

        Deve ser chamado pelo operador humano após intervenção e correção
        do problema que causou os bloqueios repetidos — antes de tentar
        um novo run() para esse cliente.
        """
        keys_to_delete = [k for k in self._iteration_counts if k.endswith(f":{client_id}")]
        for k in keys_to_delete:
            del self._iteration_counts[k]
        logger.info(
            "[Orchestrator] Contadores de iteração resetados para cliente '%s' (%d entradas removidas).",
            client_id,
            len(keys_to_delete),
        )

    def get_iteration(self, agent_name: str, client_id: str) -> int:
        """Retorna o contador atual de iterações de um agente para um cliente."""
        return self._iteration_counts.get(f"{agent_name}:{client_id}", 0)

    # ------------------------------------------------------------------
    # Core: execução de agente individual
    # ------------------------------------------------------------------

    async def _run_agent_task(
        self,
        agent_name: str,
        client_id: str,
        onboarding: dict,
    ) -> tuple[str, str, dict]:
        """
        Executa um mock agent e persiste o ciclo de vida da task no board.

        Returns:
            (task_id, status, payload) onde status é "done" ou "blocked".

        Raises:
            HumanEscalationError: Se iteration > MAX_ITERATIONS para esse agente/cliente.
                                  Deve propagar sem ser capturado — sinaliza loop infinito.
        """
        iteration_key = f"{agent_name}:{client_id}"

        # Lock garante que leitura + escrita do contador são atômicas,
        # mesmo se duas corrotinas rodarem o mesmo agente para o mesmo cliente
        # em paralelo (ex: retry manual enquanto QA ainda está rodando).
        async with self._counts_lock:
            iteration = self._iteration_counts.get(iteration_key, 0)

        # Cria task com o contador de iterações atual
        task = make_task(
            client_id=client_id,
            agent_from=agent_name,
            agent_to="orchestrator",
        )
        task.iteration = iteration

        # Gate de escalação ANTES de qualquer escrita no board
        if task.requires_human_escalation:
            raise HumanEscalationError(task.task_id, client_id, task.iteration)

        # Registra task como em progresso
        task.status = TaskStatus.IN_PROGRESS
        await self.board.write(task, writer_agent=agent_name)

        logger.debug(
            "[Orchestrator] Agente '%s' iniciou para cliente '%s' (iter=%d).",
            agent_name,
            client_id,
            iteration,
        )

        # Executa mock
        mock_fn = self._mock_agents[agent_name]
        mock_status, mock_payload = await mock_fn(client_id, onboarding)

        # Atualiza task com resultado
        if mock_status == "blocked":
            error_msg = mock_payload.get("error", "Agente retornou blocked sem descrição.")
            task.error = error_msg          # setar error ANTES de status (validação Pydantic)
            task.status = TaskStatus.BLOCKED
            async with self._counts_lock:
                self._iteration_counts[iteration_key] = iteration + 1
            logger.warning(
                "[Orchestrator] Agente '%s' bloqueado (iter=%d): %s",
                agent_name,
                iteration,
                error_msg,
            )
        else:
            task.status = TaskStatus.DONE
            task.payload = mock_payload

        await self.board.write(task, writer_agent=agent_name)

        return task.task_id, mock_status, mock_payload

    # ------------------------------------------------------------------
    # Nós do grafo
    # ------------------------------------------------------------------

    async def _node_initialize_pipeline(self, state: OrchestratorState) -> dict:
        """
        Nó 1 — Valida o onboarding e inicializa o estado do pipeline.
        Único ponto de entrada do grafo.
        """
        client_id = state["client_id"]
        onboarding = state["onboarding"]

        required_fields = ["client_id", "nome_imobiliaria"]
        missing = [f for f in required_fields if f not in onboarding]

        if missing:
            return {
                "phase": "init_failed",
                "deploy_status": "failed",
                "errors": [f"Onboarding incompleto. Campos faltantes: {missing}"],
            }

        logger.info(
            "[Orchestrator] Pipeline iniciado para cliente '%s' (%s).",
            client_id,
            onboarding.get("nome_imobiliaria", "?"),
        )

        return {
            "phase": "initialized",
            "deploy_status": "pending",
        }

    async def _node_dispatch_phase1(self, state: OrchestratorState) -> dict:
        """
        Nó 2 — Despacha os 4 agentes da Fase 1 em paralelo.

        Agentes: ingestion, dev_persona, memory, context.

        Raises:
            HumanEscalationError: Se qualquer agente ultrapassou MAX_ITERATIONS.
                                  Propagada diretamente — não capturada aqui.
        """
        client_id = state["client_id"]
        onboarding = state["onboarding"]

        # asyncio.gather: HumanEscalationError de qualquer agente cancela os demais
        results = await asyncio.gather(*[
            self._run_agent_task(agent, client_id, onboarding)
            for agent in PHASE1_AGENTS
        ])

        task_map: dict[str, str] = {}
        agent_results: dict[str, Any] = {}
        blocked_agents: list[str] = []
        errors: list[str] = []

        for agent, (task_id, status, payload) in zip(PHASE1_AGENTS, results):
            task_map[agent] = task_id
            if status == "blocked":
                blocked_agents.append(agent)
                errors.append(
                    f"[Phase1] {agent} retornou blocked: "
                    f"{payload.get('error', 'sem descrição')}"
                )
            else:
                agent_results[agent] = payload

        logger.info(
            "[Orchestrator] Phase1 concluída. Bloqueados: %s",
            blocked_agents or "nenhum",
        )

        return {
            "task_map": task_map,
            "agent_results": agent_results,
            "blocked_agents": blocked_agents,
            "errors": errors,
            "phase": "phase1_complete",
        }

    async def _node_build_consultant(self, state: OrchestratorState) -> dict:
        """
        Nó 3 — Aciona o dev_flow para construir o grafo do consultor.
        Depende dos resultados da Phase 1.
        """
        client_id  = state["client_id"]
        onboarding = state["onboarding"]

        # Injeta resultados da Fase 1 no onboarding para que o dev_flow possa
        # verificar dependências (ingestion + context) via _agent_results.
        # O dev_flow lê onboarding["_agent_results"][dep]["status"] — sem essa
        # injeção ele encontra {} e trata como "pending", bloqueando.
        onboarding_com_resultados = {
            **onboarding,
            "_agent_results": {
                agent: {"status": "done", "payload": p}
                for agent, p in (state.get("agent_results") or {}).items()
            },
        }

        task_id, status, payload = await self._run_agent_task(
            "dev_flow", client_id, onboarding_com_resultados
        )

        blocked = [agent for agent in ["dev_flow"] if status == "blocked"]
        errors = (
            [f"[BuildConsultant] dev_flow bloqueado: {payload.get('error', '')}"]
            if status == "blocked"
            else []
        )

        return {
            "task_map": {"dev_flow": task_id},
            "agent_results": {"dev_flow": payload} if status != "blocked" else {},
            "blocked_agents": blocked,
            "errors": errors,
            "phase": "consultant_built" if status == "done" else "consultant_blocked",
        }

    async def _node_audit_decisions(self, state: OrchestratorState) -> dict:
        """
        Nó 4 — Aciona o arquiteto auditor para revisar todas as decisões.
        O auditor tem direito a veto. Veto → escalação.
        """
        client_id = state["client_id"]
        onboarding = state["onboarding"]

        task_id, status, payload = await self._run_agent_task(
            "auditor", client_id, onboarding
        )

        audit_status = payload.get("audit_status", "pending") if status == "done" else "pending"

        blocked = []
        errors = []
        if status == "blocked":
            blocked = ["auditor"]
            errors = [f"[Audit] auditor bloqueado: {payload.get('error', '')}"]
        elif audit_status == "vetoed":
            errors = [f"[Audit] Veto do auditor: {payload.get('justification', '')}"]

        logger.info(
            "[Orchestrator] Auditoria concluída: status=%s", audit_status
        )

        return {
            "task_map": {"auditor": task_id},
            "agent_results": {"auditor": payload} if status == "done" else {},
            "blocked_agents": blocked,
            "errors": errors,
            "audit_status": audit_status,
            "phase": "audited",
        }

    async def _node_run_qa(self, state: OrchestratorState) -> dict:
        """
        Nó 5 — Executa QA de jornadas e integração em paralelo.
        Ambos precisam passar para liberar o gate de deploy.
        """
        client_id = state["client_id"]
        onboarding = state["onboarding"]

        results = await asyncio.gather(*[
            self._run_agent_task(agent, client_id, onboarding)
            for agent in QA_AGENTS
        ])

        task_map: dict[str, str] = {}
        agent_results: dict[str, Any] = {}
        blocked_agents: list[str] = []
        errors: list[str] = []

        qa_journeys_score = 0.0
        qa_integration_passed = False

        for agent, (task_id, status, payload) in zip(QA_AGENTS, results):
            task_map[agent] = task_id
            if status == "blocked":
                blocked_agents.append(agent)
                errors.append(f"[QA] {agent} bloqueado: {payload.get('error', '')}")
            else:
                agent_results[agent] = payload
                if agent == "qa_journeys":
                    # qa_journeys entrega "score_percentual" em escala 0–100.
                    # O gate usa escala 0.0–1.0 (QA_JOURNEYS_THRESHOLD = 0.85).
                    score_pct = payload.get("score_percentual", payload.get("score", 0.0))
                    qa_journeys_score = score_pct / 100.0 if score_pct > 1.0 else score_pct
                elif agent == "qa_integration":
                    qa_integration_passed = payload.get("passed", False)

        logger.info(
            "[Orchestrator] QA concluído. journeys_score=%.2f integration_passed=%s",
            qa_journeys_score,
            qa_integration_passed,
        )

        return {
            "task_map": task_map,
            "agent_results": agent_results,
            "blocked_agents": blocked_agents,
            "errors": errors,
            "qa_journeys_score": qa_journeys_score,
            "qa_integration_passed": qa_integration_passed,
            "phase": "qa_complete",
        }

    async def _node_final_gate(self, state: OrchestratorState) -> dict:
        """
        Nó 6 — Gate de deploy exclusivo do orquestrador.

        Único nó que pode setar deploy_status = "deploy_ready".
        Avalia todas as condições antes de liberar.
        """
        client_id = state["client_id"]
        errors: list[str] = []

        # Condições de aprovação
        qa_score_ok = state["qa_journeys_score"] >= QA_JOURNEYS_THRESHOLD
        integration_ok = state["qa_integration_passed"]
        audit_ok = state["audit_status"] in ("approved", "approved_with_note")
        no_blocked = len(state["blocked_agents"]) == 0

        if not qa_score_ok:
            errors.append(
                f"[Gate] QA de jornadas abaixo do limiar: "
                f"{state['qa_journeys_score']:.0%} < {QA_JOURNEYS_THRESHOLD:.0%}"
            )
        if not integration_ok:
            errors.append("[Gate] QA de integração falhou.")
        if not audit_ok:
            errors.append(f"[Gate] Auditoria não aprovada: {state['audit_status']}")
        if not no_blocked:
            errors.append(f"[Gate] Agentes bloqueados: {state['blocked_agents']}")

        if qa_score_ok and integration_ok and audit_ok and no_blocked:
            logger.info(
                "[Orchestrator] ✓ Gate de deploy aprovado para cliente '%s'.", client_id
            )

            # Cria task de deploy aprovado (único momento em que orchestrator seta approved)
            deploy_task = make_task(
                client_id=client_id,
                agent_from="orchestrator",
                agent_to="monitor",
                payload={"deploy": True, "qa_score": state["qa_journeys_score"]},
            )
            deploy_task.status = TaskStatus.APPROVED
            await self.board.write(deploy_task, writer_agent="orchestrator")

            return {
                "task_map": {"deploy": deploy_task.task_id},
                "deploy_status": "deploy_ready",
                "phase": "gate_approved",
                "errors": [],
            }
        else:
            logger.warning(
                "[Orchestrator] ✗ Gate de deploy reprovado para cliente '%s'. Erros: %s",
                client_id,
                errors,
            )
            return {
                "deploy_status": "failed",
                "phase": "gate_rejected",
                "errors": errors,
            }

    async def _node_activate_monitor(self, state: OrchestratorState) -> dict:
        """
        Nó 7 — Ativa o Agente 10 (monitor) após deploy aprovado.
        Último nó do pipeline de setup.
        """
        client_id = state["client_id"]

        task_id, status, payload = await self._run_agent_task(
            "monitor", client_id, state["onboarding"]
        )

        # Cria task final com deploy_ready (único status que o orchestrator pode setar)
        final_task = make_task(
            client_id=client_id,
            agent_from="orchestrator",
            agent_to="monitor",
            payload={"monitoring_active": True},
        )
        final_task.status = TaskStatus.DEPLOY_READY
        await self.board.write(final_task, writer_agent="orchestrator")

        logger.info(
            "[Orchestrator] 🚀 Deploy completo para cliente '%s'. Monitor ativo.", client_id
        )

        return {
            "task_map": {"monitor": task_id, "final": final_task.task_id},
            "agent_results": {"monitor": payload},
            "phase": "deployed",
            "deploy_status": "deploy_ready",
        }

    async def _node_handle_escalation(self, state: OrchestratorState) -> dict:
        """
        Nó terminal de escalação — acionado quando blocked_agents ou audit vetoed.
        Sinaliza revisão humana sem tentar resolver automaticamente.
        """
        client_id = state["client_id"]
        logger.error(
            "[Orchestrator] Escalação para revisão humana. Cliente '%s'. "
            "Bloqueados: %s. Erros: %s",
            client_id,
            state.get("blocked_agents", []),
            state.get("errors", []),
        )

        return {
            "deploy_status": "human_review",
            "phase": "escalated",
        }

    # ------------------------------------------------------------------
    # Roteamento condicional
    # ------------------------------------------------------------------

    @staticmethod
    def _route_after_phase1(state: OrchestratorState) -> str:
        """Depois da Phase 1: se algum agente bloqueou → escalação."""
        if state.get("blocked_agents"):
            return "handle_escalation"
        return "build_consultant"

    @staticmethod
    def _route_after_build(state: OrchestratorState) -> str:
        """Depois do dev_flow: se bloqueou → escalação."""
        if state.get("blocked_agents"):
            return "handle_escalation"
        return "audit_decisions"

    @staticmethod
    def _route_after_audit(state: OrchestratorState) -> str:
        """Depois da auditoria: veto ou bloqueio → escalação."""
        if state.get("blocked_agents") or state.get("audit_status") == "vetoed":
            return "handle_escalation"
        return "run_qa"

    @staticmethod
    def _route_after_qa(state: OrchestratorState) -> str:
        """Depois do QA: se algum agente bloqueou → escalação."""
        if state.get("blocked_agents"):
            return "handle_escalation"
        return "final_gate"

    @staticmethod
    def _route_after_gate(state: OrchestratorState) -> str:
        """Depois do gate: deploy_ready → ativa monitor, senão → escalação."""
        if state.get("deploy_status") == "deploy_ready":
            return "activate_monitor"
        return "handle_escalation"

    # ------------------------------------------------------------------
    # Construção do grafo
    # ------------------------------------------------------------------

    def _build_graph(self) -> CompiledStateGraph:
        """
        Constrói e compila o grafo LangGraph do pipeline de setup.

        Topologia:
          START → initialize → dispatch_phase1
            → [blocked?] handle_escalation → END
            → build_consultant → audit_decisions
              → [vetoed?] handle_escalation → END
              → run_qa → final_gate
                → [approved?] activate_monitor → END
                → handle_escalation → END
        """
        builder = StateGraph(OrchestratorState)

        # Nós
        builder.add_node("initialize_pipeline",  self._node_initialize_pipeline)
        builder.add_node("dispatch_phase1",       self._node_dispatch_phase1)
        builder.add_node("build_consultant",      self._node_build_consultant)
        builder.add_node("audit_decisions",       self._node_audit_decisions)
        builder.add_node("run_qa",                self._node_run_qa)
        builder.add_node("final_gate",            self._node_final_gate)
        builder.add_node("activate_monitor",      self._node_activate_monitor)
        builder.add_node("handle_escalation",     self._node_handle_escalation)

        # Arestas fixas
        builder.add_edge(START,                  "initialize_pipeline")
        builder.add_edge("initialize_pipeline",  "dispatch_phase1")
        builder.add_edge("activate_monitor",     END)
        builder.add_edge("handle_escalation",    END)

        # Arestas condicionais
        builder.add_conditional_edges(
            "dispatch_phase1",
            self._route_after_phase1,
            {"build_consultant": "build_consultant", "handle_escalation": "handle_escalation"},
        )
        builder.add_conditional_edges(
            "build_consultant",
            self._route_after_build,
            {"audit_decisions": "audit_decisions", "handle_escalation": "handle_escalation"},
        )
        builder.add_conditional_edges(
            "audit_decisions",
            self._route_after_audit,
            {"run_qa": "run_qa", "handle_escalation": "handle_escalation"},
        )
        builder.add_conditional_edges(
            "run_qa",
            self._route_after_qa,
            {"final_gate": "final_gate", "handle_escalation": "handle_escalation"},
        )
        builder.add_conditional_edges(
            "final_gate",
            self._route_after_gate,
            {"activate_monitor": "activate_monitor", "handle_escalation": "handle_escalation"},
        )

        return builder.compile()

    # ------------------------------------------------------------------
    # Ponto de entrada público
    # ------------------------------------------------------------------

    async def run(self, onboarding: dict[str, Any]) -> OrchestratorState:
        """
        Executa o pipeline completo de setup para um novo cliente.

        Args:
            onboarding: Dict com dados do formulário de onboarding.
                        Obrigatório: 'client_id', 'nome_imobiliaria'.

        Returns:
            Estado final do grafo com deploy_status e todos os resultados.

        Raises:
            ValueError: Se onboarding não contém 'client_id'.
            HumanEscalationError: Se algum agente ultrapassou MAX_ITERATIONS.
                                  O chamador deve interromper automação e notificar operador.
        """
        if "client_id" not in onboarding:
            raise ValueError("onboarding deve conter o campo 'client_id'.")

        client_id = onboarding["client_id"]

        initial_state: OrchestratorState = {
            "client_id": client_id,
            "onboarding": onboarding,
            "task_map": {},
            "agent_results": {},
            "blocked_agents": [],
            "errors": [],
            "qa_journeys_score": 0.0,
            "qa_integration_passed": False,
            "audit_status": "pending",
            "phase": "start",
            "deploy_status": "pending",
            "rework_count": 0,
        }

        logger.info("[Orchestrator] run() iniciado para cliente '%s'.", client_id)
        result: OrchestratorState = await self.graph.ainvoke(initial_state)
        logger.info(
            "[Orchestrator] run() concluído para cliente '%s'. deploy_status=%s",
            client_id,
            result.get("deploy_status"),
        )
        return result
