"""
tests/test_orchestrator.py — Testes unitários de agents/orchestrator.py.

Mínimo exigido: 3 testes antes de integração ao setup_pipeline.py (CLAUDE.md).
Todos os testes usam fakeredis — sem Redis real, sem I/O externo.

Cobertura obrigatória (CLAUDE.md):
  1. Pipeline completo com todos os mocks retornando sucesso → deploy_ready
  2. Um agente retornando blocked → deploy_status == "human_review"
  3. iteration > MAX_ITERATIONS → HumanEscalationError propagado antes do gate

Cobertura adicional:
  4. QA de jornadas abaixo do limiar (< 85%) → gate reprova → human_review
  5. Auditor veta → escalação sem atingir o gate de deploy
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agents.orchestrator import OrchestratorAgent, QA_JOURNEYS_THRESHOLD
from state.board import HumanEscalationError, StateBoard
from state.pubsub import AgentPubSub
from state.schema import MAX_ITERATIONS


# ---------------------------------------------------------------------------
# Fixtures base com fakeredis
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    """Servidor fakeredis compartilhado entre board e pubsub."""
    import fakeredis.aioredis as fakeredis
    server = fakeredis.FakeServer()
    yield server


@pytest_asyncio.fixture
async def board(fake_redis):
    """StateBoard injetado com fakeredis."""
    import fakeredis.aioredis as fakeredis

    b = StateBoard()
    b._client = fakeredis.FakeRedis(server=fake_redis, decode_responses=True)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def pubsub(fake_redis):
    """AgentPubSub do orchestrator com fakeredis."""
    import fakeredis.aioredis as fakeredis

    ps = AgentPubSub("orchestrator")
    ps._publisher = fakeredis.FakeRedis(server=fake_redis, decode_responses=True)
    ps._subscriber = fakeredis.FakeRedis(server=fake_redis, decode_responses=True)
    yield ps
    await ps.close()


def _base_onboarding(client_id: str = "test_cliente_001") -> dict:
    """Onboarding mínimo válido para os testes."""
    return {
        "client_id": client_id,
        "nome_imobiliaria": "Imob Luxo Premium",
        "segmento": "alto_padrao",
        "cidade": "São Paulo",
    }


# ---------------------------------------------------------------------------
# Teste 1 — Pipeline completo com sucesso → deploy_ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_all_mocks_success(board, pubsub):
    """
    Quando todos os agentes retornam sucesso e QA passa o limiar de 85%,
    o orquestrador deve setar deploy_status = 'deploy_ready'.

    Valida:
    - Todos os agentes passaram (nenhum blocked_agent)
    - audit_status == "approved"
    - qa_journeys_score >= QA_JOURNEYS_THRESHOLD
    - qa_integration_passed == True
    - deploy_status == "deploy_ready"
    - task_map contém entries de todos os agentes executados
    """
    orchestrator = OrchestratorAgent(board, pubsub)  # usa mocks padrão (todos success)

    result = await orchestrator.run(_base_onboarding("happy_path_001"))

    assert result["deploy_status"] == "deploy_ready", (
        f"Esperava deploy_ready mas got: {result['deploy_status']}. "
        f"Erros: {result.get('errors')}"
    )
    assert result["blocked_agents"] == [], (
        f"Não deveria ter agentes bloqueados, mas got: {result['blocked_agents']}"
    )
    assert result["audit_status"] in ("approved", "approved_with_note"), (
        f"Auditoria deveria ter aprovado, mas got: {result['audit_status']}"
    )
    assert result["qa_journeys_score"] >= QA_JOURNEYS_THRESHOLD, (
        f"QA score abaixo do limiar: {result['qa_journeys_score']}"
    )
    assert result["qa_integration_passed"] is True

    # Todos os agentes de Fase 1 devem ter task_id registrado
    for agent in ["ingestion", "dev_persona", "memory", "context", "dev_flow"]:
        assert agent in result["task_map"], (
            f"Agente '{agent}' não aparece no task_map: {result['task_map'].keys()}"
        )

    # Monitor ativado no final
    assert "monitor" in result["task_map"]


# ---------------------------------------------------------------------------
# Teste 2 — Agente blocked → human_review (escalação graciosa)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_agent_escalates_to_human_review(board, pubsub):
    """
    Quando um agente da Fase 1 retorna 'blocked', o orquestrador deve:
    - Detectar o blocked_agents
    - Rotear para handle_escalation (sem levantar exceção)
    - Finalizar com deploy_status == "human_review"

    O pipeline NÃO deve chegar ao gate de deploy.
    """

    async def mock_ingestion_blocked(client_id: str, onboarding: dict) -> tuple[str, dict]:
        """Simula ingestão falhando — portfólio corrompido ou API indisponível."""
        return "blocked", {
            "error": "Arquivo de portfólio corrompido — não foi possível processar imoveis.csv.",
            "agent": "ingestion",
        }

    orchestrator = OrchestratorAgent(
        board,
        pubsub,
        mock_agents={"ingestion": mock_ingestion_blocked},
    )

    result = await orchestrator.run(_base_onboarding("blocked_test_002"))

    # Escalação graciosa — sem exceção, com deploy_status claro
    assert result["deploy_status"] == "human_review", (
        f"Esperava 'human_review' mas got: {result['deploy_status']}"
    )
    assert "ingestion" in result["blocked_agents"], (
        f"'ingestion' deveria estar em blocked_agents: {result['blocked_agents']}"
    )
    # Pipeline não chegou ao gate — QA não executou
    assert result["qa_journeys_score"] == 0.0, (
        "QA não deveria ter executado — pipeline parou em Phase 1"
    )
    # Deve haver ao menos um erro registrado
    assert any("ingestion" in e for e in result["errors"]), (
        f"Erros não mencionam 'ingestion': {result['errors']}"
    )


# ---------------------------------------------------------------------------
# Teste 3 — iteration > MAX_ITERATIONS → HumanEscalationError propagado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_iterations_raises_human_escalation_error(board, pubsub):
    """
    Quando o contador de iterações de um agente ultrapassa MAX_ITERATIONS,
    _run_agent_task deve lançar HumanEscalationError ANTES de qualquer
    escrita no board e ANTES de chegar no gate de deploy.

    O erro deve propagar a partir de graph.ainvoke() — não ser silenciado.

    Isso simula um cenário de re-tentativa após falhas repetidas:
    o operador já tentou re-executar o pipeline X vezes e o agente
    continua falhando — é necessária intervenção humana.
    """
    orchestrator = OrchestratorAgent(board, pubsub)

    # Pré-carrega o contador de iterações além do limite para o agente 'context'
    # Simula que 'context' já foi tentado MAX_ITERATIONS + 1 vezes em tentativas anteriores
    iteration_key = f"context:max_iter_003"
    orchestrator._iteration_counts[iteration_key] = MAX_ITERATIONS + 1

    with pytest.raises(HumanEscalationError) as exc_info:
        await orchestrator.run(_base_onboarding("max_iter_003"))

    error = exc_info.value
    assert error.client_id == "max_iter_003", (
        f"Esperava client_id 'max_iter_003' no erro, got: {error.client_id}"
    )
    assert error.iteration > MAX_ITERATIONS, (
        f"iteration no erro deveria ser > {MAX_ITERATIONS}, got: {error.iteration}"
    )
    assert "max_iter_003" in str(error), (
        f"Mensagem do erro deveria mencionar o client_id: {error}"
    )


# ---------------------------------------------------------------------------
# Teste 4 (bônus) — QA score abaixo do limiar → gate reprova → human_review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_qa_score_fails_gate(board, pubsub):
    """
    Quando qa_journeys_score < QA_JOURNEYS_THRESHOLD (85%),
    o gate de deploy deve reprovar e setar deploy_status = "human_review".

    O limiar de 85% é um requisito explícito do CLAUDE.md —
    garantir que o gate o aplica corretamente é crítico.
    """

    async def mock_qa_journeys_low(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return "done", {
            "score": 0.70,  # abaixo de 0.85
            "approved": 14,
            "total": 20,
            "failures": [
                "Jornada 3: resposta sobre escola incorreta",
                "Jornada 7: lead agressivo não tratado corretamente",
                "Jornada 12: solicitação de desconto mal gerenciada",
                "Jornada 15: follow-up fora do prazo",
                "Jornada 18: áudio não gerado para resposta de vizinhança",
                "Jornada 20: pergunta fora do escopo não redirecionada",
            ],
        }

    orchestrator = OrchestratorAgent(
        board,
        pubsub,
        mock_agents={"qa_journeys": mock_qa_journeys_low},
    )

    result = await orchestrator.run(_base_onboarding("low_qa_004"))

    assert result["deploy_status"] == "human_review", (
        f"Gate deveria ter reprovado com QA score baixo, mas got: {result['deploy_status']}"
    )
    assert result["qa_journeys_score"] == 0.70
    assert any("limiar" in e or "QA" in e for e in result["errors"]), (
        f"Erros deveria mencionar falha de QA: {result['errors']}"
    )
    # Pipeline chegou até o gate (QA executou normalmente)
    assert "qa_journeys" in result["task_map"]


# ---------------------------------------------------------------------------
# Teste 5 (bônus) — Auditor veta → escalação antes do QA
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auditor_veto_escalates_before_qa(board, pubsub):
    """
    Quando o auditor retorna audit_status = 'vetoed', o pipeline deve
    rotear para handle_escalation — QA não deve ser executado.

    Garante que o arquiteto auditor tem poder real de veto no pipeline.
    """

    async def mock_auditor_veto(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return "done", {
            "audit_status": "vetoed",
            "justification": "Consultor construído com acoplamento excessivo ao portfólio.",
            "proposed_alternative": "Separar camada de recomendação da camada de dados.",
        }

    orchestrator = OrchestratorAgent(
        board,
        pubsub,
        mock_agents={"auditor": mock_auditor_veto},
    )

    result = await orchestrator.run(_base_onboarding("veto_test_005"))

    assert result["deploy_status"] == "human_review", (
        f"Veto do auditor deveria resultar em human_review, got: {result['deploy_status']}"
    )
    assert result["audit_status"] == "vetoed"
    # QA não deve ter executado (audit bloqueou o caminho)
    assert result["qa_journeys_score"] == 0.0, (
        "QA de jornadas não deveria ter executado após veto do auditor"
    )
    assert "qa_journeys" not in result["task_map"], (
        "task_map não deveria ter 'qa_journeys' — pipeline foi vetado antes"
    )
