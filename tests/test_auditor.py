"""
tests/test_auditor.py — Testes unitários de agents/auditor.py.

Mínimo exigido: 5 testes (CLAUDE.md). Todas as chamadas ao LLM são
mockadas via unittest.mock.AsyncMock — chamadas reais ficam para o QA de integração.

Cobertura:
  1. Entrega aprovada passa sem modificação — status: approved, campos CoT intactos
  2. Decisão irreversível questionável → approved_with_note com nota registrada
  3. Entrega claramente problemática → vetoed com proposed_alternative obrigatório
  4. Tentativa de chamar board.write() diretamente levanta AuditorWriteViolation
  5. Veredito sem todos os campos CoT é rejeitado pelo schema antes de ser escrito
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from agents.auditor import (
    AuditResponseParseError,
    AuditResultFull,
    AuditorAgent,
    AuditorBoard,
    AuditorWriteViolation,
)
from state.schema import AuditStatus


# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------


def _make_llm_response(payload: dict) -> MagicMock:
    """
    Simula anthropic.messages.Message com .content[0].text contendo JSON.
    Não usa markdown — resposta limpa, como o prompt exige.
    """
    content_block = MagicMock()
    content_block.text = json.dumps(payload)

    message = MagicMock()
    message.content = [content_block]
    return message


def _approved_cot_payload() -> dict:
    """Payload CoT completo com verdict: approved."""
    return {
        "argument_for": (
            "A escolha da WhatsApp Business API oficial via 360dialog elimina "
            "o risco de ban e é compatível com o posicionamento premium do produto."
        ),
        "argument_against": (
            "A dependência de um BSP (Business Solution Provider) adiciona uma camada "
            "de latência e um custo fixo mensal que precisa ser absorvido pelo cliente."
        ),
        "simpler_alternative": (
            "Não há alternativa mais simples com as mesmas garantias: a API oficial "
            "é o único canal sem risco de ban para produto pago de alto padrão."
        ),
        "reversibility": (
            "Moderada — migrar de BSP exige reconfiguração de webhook e número "
            "de telefone, mas sem perda de dados ou impacto em leads ativos."
        ),
        "verdict": "approved",
        "justification": (
            "Integração via 360dialog está conforme as decisões fixadas e sem riscos residuais."
        ),
        "proposed_alternative": None,
    }


def _approved_with_note_cot_payload() -> dict:
    """Payload CoT com verdict: approved_with_note — decisão irreversível sem documentação."""
    return {
        "argument_for": (
            "Supabase pgvector com namespace por client_id garante isolamento "
            "total de dados entre clientes e cumpre os requisitos LGPD."
        ),
        "argument_against": (
            "A migração de pgvector para outro banco vetorial seria custosa e exigiria "
            "reindexação completa de todos os portfólios — decisão com reversibilidade baixa."
        ),
        "simpler_alternative": (
            "Pinecone seria mais simples de operar inicialmente, mas foi descartado "
            "pelo risco de vendor lock-in — a decisão de usar pgvector já está fixada."
        ),
        "reversibility": (
            "Irreversível — custo alto: migração exige reescrita do repositório, "
            "reindexação de embeddings e reconfiguração de todas as instâncias ativas."
        ),
        "verdict": "approved_with_note",
        "justification": (
            "Decisão irreversível aprovada, mas a ausência de documentação explícita "
            "do custo de migração deve ser corrigida antes do próximo ciclo de auditoria."
        ),
        "proposed_alternative": None,
    }


def _vetoed_cot_payload() -> dict:
    """Payload CoT com verdict: vetoed — uso de Evolution API (proibida pelo CLAUDE.md)."""
    return {
        "argument_for": (
            "A Evolution API oferece custo zero de licença e setup mais rápido "
            "sem dependência de BSP oficial."
        ),
        "argument_against": (
            "A Evolution API usa reverse-engineering da API do WhatsApp e viola "
            "os termos de serviço da Meta — risco de ban permanente do número, "
            "inaceitável para produto pago de alto padrão."
        ),
        "simpler_alternative": (
            "Substituir por 360dialog ou Gupshup (BSPs oficiais): custo adicional "
            "de ~USD 50/mês, mas elimina completamente o risco de ban e é a decisão "
            "já fixada no CLAUDE.md como inegociável."
        ),
        "reversibility": (
            "Irreversível enquanto em produção — um ban do número afeta todos os "
            "leads ativos e exige troca de número, perda de histórico e notificação "
            "de todos os clientes afetados."
        ),
        "verdict": "vetoed",
        "justification": (
            "Evolution API é explicitamente proibida pelo CLAUDE.md — substituir "
            "por 360dialog ou Gupshup antes de qualquer deploy."
        ),
        "proposed_alternative": (
            "Usar 360dialog como BSP oficial: integrar via API REST com webhook "
            "configurado no endpoint /whatsapp/receive — impacto estimado de 1 dia "
            "de trabalho para migração do wrapper existente."
        ),
    }


def _make_mock_board() -> MagicMock:
    """Board mock com update_audit_result como AsyncMock."""
    board = MagicMock()
    board.update_audit_result = AsyncMock(return_value=None)
    board.read = AsyncMock(return_value=None)
    board.list_tasks = AsyncMock(return_value=[])
    board.write = AsyncMock(return_value=None)
    return board


def _make_anthropic_client(response_payload: dict) -> MagicMock:
    """Cliente Anthropic mockado com messages.create retornando payload controlado."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_make_llm_response(response_payload))
    return client


def _base_onboarding(audit_target: str = "Integração WhatsApp via 360dialog") -> dict:
    return {
        "_audit_target": audit_target,
        "_task_id": "task-auditor-001",
    }


# ---------------------------------------------------------------------------
# Teste 1 — Entrega aprovada passa sem modificação
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_delivery_passes_unchanged():
    """
    Quando o LLM retorna um veredito 'approved' com todos os campos CoT,
    run() deve retornar ('done', payload) sem alterar nenhum campo.

    Verifica:
    - status retornado é 'done'
    - audit_status no payload == AuditStatus.APPROVED
    - todos os campos CoT estão presentes no payload
    - board.update_audit_result() foi chamado uma vez com os dados corretos
    """
    payload = _approved_cot_payload()
    anthropic_client = _make_anthropic_client(payload)
    mock_board = _make_mock_board()

    agent = AuditorAgent(anthropic_client, mock_board)
    status, result = await agent.run("cliente_001", _base_onboarding())

    assert status == "done", f"Esperava 'done', got '{status}': {result}"
    assert result["audit_status"] == AuditStatus.APPROVED

    # Todos os campos CoT presentes
    for field in ("argument_for", "argument_against", "simpler_alternative", "reversibility"):
        assert field in result, f"Campo CoT ausente no payload: {field}"
        assert result[field], f"Campo CoT vazio: {field}"

    assert result["justification"]
    assert result["proposed_alternative"] is None

    # Board atualizado corretamente
    # AuditorBoard delega com args posicionais: (task_id, client_id, audit_result)
    mock_board.update_audit_result.assert_called_once()
    call_args = mock_board.update_audit_result.call_args
    # Suporta chamada posicional ou keyword
    args, kwargs = call_args
    task_id_called = args[0] if args else kwargs.get("task_id")
    client_id_called = args[1] if len(args) > 1 else kwargs.get("client_id")
    audit_result_called = args[2] if len(args) > 2 else kwargs.get("audit_result")
    assert task_id_called == "task-auditor-001"
    assert client_id_called == "cliente_001"
    assert audit_result_called["status"] == AuditStatus.APPROVED


# ---------------------------------------------------------------------------
# Teste 2 — Decisão irreversível questionável → approved_with_note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_irreversible_questionable_decision_returns_approved_with_note():
    """
    Quando o LLM identifica uma decisão irreversível sem documentação adequada,
    run() deve retornar ('done', payload) com audit_status == APPROVED_WITH_NOTE.

    Verifica:
    - status retornado é 'done' (não 'blocked')
    - audit_status == AuditStatus.APPROVED_WITH_NOTE
    - justification documenta o ponto de atenção
    - proposed_alternative é None (não é veto — apenas nota)
    """
    payload = _approved_with_note_cot_payload()
    anthropic_client = _make_anthropic_client(payload)
    mock_board = _make_mock_board()

    onboarding = _base_onboarding("Configuração pgvector Supabase sem documentação de migração")
    agent = AuditorAgent(anthropic_client, mock_board)
    status, result = await agent.run("cliente_002", onboarding)

    assert status == "done", f"Esperava 'done', got '{status}': {result}"
    assert result["audit_status"] == AuditStatus.APPROVED_WITH_NOTE

    # A nota deve mencionar o problema identificado
    assert "irreversível" in result["justification"].lower() or "documentação" in result["justification"].lower()

    # Approved_with_note não exige proposed_alternative
    assert result["proposed_alternative"] is None

    # Reversibilidade deve mencionar custo alto
    assert "irreversível" in result["reversibility"].lower() or "alto" in result["reversibility"].lower()


# ---------------------------------------------------------------------------
# Teste 3 — Entrega problemática → vetoed com proposed_alternative
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clearly_problematic_delivery_returns_vetoed_with_alternative():
    """
    Quando o LLM veta uma entrega (Evolution API — explicitamente proibida),
    run() deve retornar ('done', payload) com audit_status == VETOED e
    proposed_alternative preenchido obrigatoriamente.

    Verifica:
    - status retornado é 'done' (o veto é um resultado válido, não um erro)
    - audit_status == AuditStatus.VETOED
    - proposed_alternative não é None e não é vazio
    - argument_against menciona o risco real
    """
    payload = _vetoed_cot_payload()
    anthropic_client = _make_anthropic_client(payload)
    mock_board = _make_mock_board()

    onboarding = _base_onboarding("Integração WhatsApp via Evolution API")
    agent = AuditorAgent(anthropic_client, mock_board)
    status, result = await agent.run("cliente_003", onboarding)

    assert status == "done", f"Esperava 'done', got '{status}': {result}"
    assert result["audit_status"] == AuditStatus.VETOED

    # proposed_alternative é obrigatório no veto
    assert result["proposed_alternative"] is not None
    assert len(result["proposed_alternative"]) >= 10, "proposed_alternative muito curto"

    # Deve mencionar a alternativa concreta (360dialog)
    assert "360dialog" in result["proposed_alternative"] or "Gupshup" in result["proposed_alternative"]

    # argument_against deve identificar o risco real
    assert "ban" in result["argument_against"].lower() or "meta" in result["argument_against"].lower()

    # Board atualizado com status vetoed
    # AuditorBoard delega com args posicionais: (task_id, client_id, audit_result)
    mock_board.update_audit_result.assert_called_once()
    call_args = mock_board.update_audit_result.call_args
    args, kwargs = call_args
    audit_written = args[2] if len(args) > 2 else kwargs.get("audit_result")
    assert audit_written["status"] == AuditStatus.VETOED
    assert audit_written["proposed_alternative"] is not None


# ---------------------------------------------------------------------------
# Teste 4 — board.write() direto levanta AuditorWriteViolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_board_write_raises_auditor_write_violation():
    """
    O AuditorBoard deve bloquear board.write() com AuditorWriteViolation.

    Regra do CLAUDE.md: o auditor NUNCA escreve via board.write() —
    apenas via board.update_audit_result().

    Verifica:
    - AuditorBoard.write() levanta AuditorWriteViolation
    - AuditorBoard.update_status() levanta AuditorWriteViolation
    - AuditorBoard.delete() levanta AuditorWriteViolation
    - AuditorBoard.increment_iteration() levanta AuditorWriteViolation
    - AuditorBoard.update_audit_result() NÃO levanta (operação permitida)
    """
    mock_board = _make_mock_board()
    auditor_board = AuditorBoard(mock_board)

    # write() bloqueado
    with pytest.raises(AuditorWriteViolation) as exc_info:
        await auditor_board.write("task-x", "cliente_x", {})
    assert "board.write()" in str(exc_info.value)
    assert "update_audit_result" in str(exc_info.value)

    # update_status() bloqueado
    with pytest.raises(AuditorWriteViolation):
        await auditor_board.update_status("task-x", "cliente_x", "done")

    # delete() bloqueado
    with pytest.raises(AuditorWriteViolation):
        await auditor_board.delete("task-x", "cliente_x")

    # increment_iteration() bloqueado
    with pytest.raises(AuditorWriteViolation):
        await auditor_board.increment_iteration("task-x", "cliente_x")

    # update_audit_result() permitido — não deve levantar
    await auditor_board.update_audit_result(
        task_id="task-x",
        client_id="cliente_x",
        audit_result={"status": "approved"},
    )
    mock_board.update_audit_result.assert_called_once()


# ---------------------------------------------------------------------------
# Teste 5 — CoT incompleto rejeitado pelo schema antes de escrever no board
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incomplete_cot_rejected_by_schema_before_board_write():
    """
    Se o LLM retorna um JSON sem todos os campos CoT obrigatórios,
    AuditResultFull deve rejeitar com ValidationError — e o board
    NÃO deve ser atualizado.

    Cenários testados:
    5a. Campo argument_for ausente → ValidationError
    5b. Campo argument_against vazio (string curta) → ValidationError
    5c. Veto sem proposed_alternative → ValidationError (regra inegociável)
    5d. run() retorna ('blocked', {error}) quando CoT está incompleto
    """
    mock_board = _make_mock_board()

    # 5a. Campo obrigatório ausente
    with pytest.raises(ValidationError) as exc_info:
        AuditResultFull(
            # argument_for ausente
            argument_against="Risco real identificado com detalhe suficiente.",
            simpler_alternative="Não há alternativa mais simples neste contexto.",
            reversibility="Reversível — custo baixo, sem dependências externas.",
            status=AuditStatus.APPROVED,
            justification="Entrega aprovada sem ressalvas neste momento.",
        )
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("argument_for",) for e in errors)

    # 5b. Campo com conteúdo insuficiente (min_length=10)
    with pytest.raises(ValidationError) as exc_info:
        AuditResultFull(
            argument_for="Ok",  # muito curto — min_length=10
            argument_against="Risco real identificado com detalhe suficiente.",
            simpler_alternative="Não há alternativa mais simples neste contexto.",
            reversibility="Reversível — custo baixo, sem dependências externas.",
            status=AuditStatus.APPROVED,
            justification="Entrega aprovada sem ressalvas neste momento.",
        )
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("argument_for",) for e in errors)

    # 5c. Veto sem proposed_alternative — regra inegociável
    with pytest.raises(ValidationError) as exc_info:
        AuditResultFull(
            argument_for="A entrega tem mérito em termos de velocidade de integração.",
            argument_against="Risco de ban permanente — inaceitável para produto pago.",
            simpler_alternative="BSP oficial elimina o risco com custo incremental baixo.",
            reversibility="Irreversível — ban afeta todos os leads ativos imediatamente.",
            status=AuditStatus.VETOED,
            justification="Evolution API viola termos da Meta e deve ser substituída.",
            proposed_alternative=None,  # PROIBIDO quando vetoed
        )
    errors = exc_info.value.errors()
    assert any("proposed_alternative" in str(e) or "veto" in str(e).lower() for e in errors)

    # 5d. run() retorna blocked quando o LLM retorna CoT incompleto
    incomplete_payload = {
        # argument_for ausente intencionalmente
        "argument_against": "Risco real identificado.",
        "simpler_alternative": "Sem alternativa mais simples aqui.",
        "reversibility": "Reversível com baixo custo de reversão.",
        "verdict": "approved",
        "justification": "Entrega aprovada conforme análise.",
        "proposed_alternative": None,
    }
    anthropic_client = _make_anthropic_client(incomplete_payload)
    agent = AuditorAgent(anthropic_client, mock_board)

    status, result = await agent.run("cliente_005", _base_onboarding("Entrega com CoT incompleto"))

    assert status == "blocked", f"Esperava 'blocked', got '{status}'"
    assert "error" in result
    assert "schema" in result["error"].lower() or "inválido" in result["error"].lower()
    assert result.get("audit_status") == "schema_violation"

    # Board NÃO deve ter sido atualizado — CoT rejeitado antes da escrita
    mock_board.update_audit_result.assert_not_called()
