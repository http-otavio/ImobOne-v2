"""
agents/auditor.py — Agente 2: Arquiteto Auditor

Questiona o raciocínio por trás de cada entrega — não apenas valida resultados.
Tem direito de veto com justificativa obrigatória (nunca veta sem alternativa).
Opera em paralelo ao orquestrador, mas não executa tarefas.

Restrições arquiteturais (CLAUDE.md):
  - NUNCA escreve via board.write() — apenas board.update_audit_result()
  - Toda escrita no board passa pelo AuditorBoard (wrapper restrito)
  - Nenhum veredito é emitido sem todos os campos CoT preenchidos
  - Veto sem proposed_alternative é rejeitado pelo schema antes de ser escrito

Pipeline CoT adversarial (completo e obrigatório):
  argument_for → argument_against → simpler_alternative
    → reversibility → verdict → justification

O prompt vive em prompts/base/auditor.md — não hardcoded aqui.
Isso permite calibrar o comportamento do auditor sem tocar em código.

Uso standalone:
    auditor = AuditorAgent(anthropic_client, board)
    status, payload = await auditor.run("cliente_001", onboarding)

Uso com orquestrador (substitui mock):
    orchestrator = OrchestratorAgent(board, pubsub, mock_agents={
        "auditor": auditor.run,
    })
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from state.schema import AuditResult, AuditStatus

logger = logging.getLogger(__name__)

# Caminho canônico do prompt.
# O filesystem montado (FUSE) retorna exists()=True mesmo para arquivos que
# não podem ser lidos. Usamos leitura real como teste de disponibilidade.
_PROJECT_PROMPT = Path(__file__).parent.parent / "prompts" / "base" / "auditor.md"
_SESSION_PROMPT = Path("/sessions/magical-trusting-wright/prompts/base/auditor.md")
try:
    _PROJECT_PROMPT.read_text(encoding="utf-8")
    PROMPT_PATH = _PROJECT_PROMPT
except (FileNotFoundError, OSError):
    PROMPT_PATH = _SESSION_PROMPT

# Modelo LLM usado pelo auditor (raciocínio profundo — CLAUDE.md)
AUDITOR_MODEL = "claude-sonnet-4-6"
AUDITOR_MAX_TOKENS = 1024

# Campos CoT que o prompt exige — usados para validação em AuditResultFull
COT_REQUIRED_FIELDS = (
    "argument_for",
    "argument_against",
    "simpler_alternative",
    "reversibility",
)

# Escopo de auditoria obrigatória (CLAUDE.md)
AUDIT_SCOPE = (
    "escolha de ferramenta/API",
    "estrutura de memória do lead",
    "tom e persona",
    "dependências de terceiros",
    "resultado final pré-deploy",
)

# Fora do escopo (CLAUDE.md) — auditoria opcional nesses casos
OUT_OF_SCOPE = (
    "ajustes de prompt dentro de módulo aprovado",
    "correções de bug sem impacto arquitetural",
    "formatação de resposta",
)


# ---------------------------------------------------------------------------
# Exceções
# ---------------------------------------------------------------------------


class AuditorWriteViolation(Exception):
    """
    Lançada quando o auditor tenta chamar board.write() diretamente.
    O auditor só pode escrever via update_audit_result() — regra do CLAUDE.md.
    """


class AuditResponseParseError(Exception):
    """Lançada quando a resposta do LLM não é JSON válido ou está malformada."""


# ---------------------------------------------------------------------------
# AuditResultFull — schema estrito com CoT obrigatório
# ---------------------------------------------------------------------------


class AuditResultFull(BaseModel):
    """
    Schema de validação estrita usado pelo AuditorAgent antes de escrever no board.

    Diferença do AuditResult do schema.py:
      - Campos CoT (argument_for, argument_against, simpler_alternative, reversibility)
        são OBRIGATÓRIOS aqui — o prompt do auditor exige todos preenchidos.
      - AuditResult no board mantém esses campos como opcionais para compatibilidade
        com outros fluxos que não passam pelo auditor completo.

    Se qualquer campo CoT estiver ausente ou vazio, Pydantic rejeita antes de
    qualquer escrita no board — "rejeitado pelo schema antes de ser escrito".
    """

    # Pipeline CoT — todos obrigatórios
    argument_for: str = Field(
        ...,
        min_length=10,
        description="Argumento a favor da entrega auditada.",
    )
    argument_against: str = Field(
        ...,
        min_length=10,
        description="Argumento contra — riscos reais identificados.",
    )
    simpler_alternative: str = Field(
        ...,
        min_length=10,
        description="Alternativa mais simples ou justificativa de que não existe.",
    )
    reversibility: str = Field(
        ...,
        min_length=10,
        description="Classificação e custo real de reversão.",
    )

    # Veredito
    status: AuditStatus
    justification: str = Field(
        ...,
        min_length=10,
        description="Uma frase precisa e acionável sobre o veredito.",
    )
    proposed_alternative: str | None = Field(
        default=None,
        description="Obrigatório quando status == vetoed.",
    )

    @model_validator(mode="after")
    def veto_requires_alternative(self) -> AuditResultFull:
        """Veto sem proposed_alternative é rejeitado — regra inegociável do auditor."""
        if self.status == AuditStatus.VETOED and not self.proposed_alternative:
            raise ValueError(
                "Veto requer proposed_alternative preenchido. "
                "O auditor não pode vetar sem oferecer um caminho alternativo concreto."
            )
        return self

    def to_board_dict(self) -> dict[str, Any]:
        """
        Converte para o dict aceito por board.update_audit_result().
        Mapeia 'status' para o campo 'status' do AuditResult do board.
        """
        return {
            "status": self.status,
            "justification": self.justification,
            "proposed_alternative": self.proposed_alternative,
            "argument_for": self.argument_for,
            "argument_against": self.argument_against,
            "simpler_alternative": self.simpler_alternative,
            "reversibility": self.reversibility,
        }


# ---------------------------------------------------------------------------
# AuditorBoard — wrapper restrito ao auditor
# ---------------------------------------------------------------------------


class AuditorBoard:
    """
    Wrapper do StateBoard que expõe apenas as operações permitidas ao auditor.

    O auditor pode:
      - Ler tasks (read, list_tasks)
      - Escrever resultado de auditoria (update_audit_result)

    O auditor NÃO pode:
      - Escrever tasks diretamente (write) → AuditorWriteViolation
      - Atualizar status (update_status) → AuditorWriteViolation
      - Deletar tasks (delete) → AuditorWriteViolation
      - Incrementar iterações (increment_iteration) → AuditorWriteViolation
    """

    def __init__(self, board: Any) -> None:
        self._board = board

    # Operações permitidas
    async def read(self, task_id: str, client_id: str):
        return await self._board.read(task_id, client_id)

    async def list_tasks(self, client_id: str, status=None):
        return await self._board.list_tasks(client_id, status)

    async def update_audit_result(
        self,
        task_id: str,
        client_id: str,
        audit_result: dict,
    ):
        return await self._board.update_audit_result(task_id, client_id, audit_result)

    # Operações proibidas — lançam exceção descritiva
    async def write(self, *args, **kwargs):
        raise AuditorWriteViolation(
            "O auditor não pode chamar board.write() diretamente. "
            "Use board.update_audit_result() para registrar o resultado de auditoria. "
            "Regra CLAUDE.md: o auditor escreve apenas no campo audit_result."
        )

    async def update_status(self, *args, **kwargs):
        raise AuditorWriteViolation(
            "O auditor não pode atualizar status de tasks diretamente. "
            "Apenas o orquestrador tem essa autoridade."
        )

    async def delete(self, *args, **kwargs):
        raise AuditorWriteViolation(
            "O auditor não pode deletar tasks do board."
        )

    async def increment_iteration(self, *args, **kwargs):
        raise AuditorWriteViolation(
            "O auditor não pode incrementar contadores de iteração."
        )


# ---------------------------------------------------------------------------
# AuditorAgent
# ---------------------------------------------------------------------------


class AuditorAgent:
    """
    Agente 2 — Arquiteto Auditor.

    Questiona o raciocínio por trás de cada entrega usando o pipeline
    CoT adversarial. Emite vereditos com justificativa obrigatória.

    Args:
        anthropic_client: anthropic.AsyncAnthropic — Claude Sonnet para auditoria.
        board: StateBoard ou qualquer board compatível.
                       Internamente convertido para AuditorBoard (restrito).
        prompt_path: Caminho para o arquivo de prompt. Default: prompts/base/auditor.md.
    """

    def __init__(
        self,
        anthropic_client: Any,
        board: Any,
        prompt_path: Path | str | None = None,
    ) -> None:
        self.anthropic = anthropic_client
        # Converte para board restrito — garante que write() nunca é chamável
        self.board = AuditorBoard(board) if not isinstance(board, AuditorBoard) else board
        self._prompt_template = self._load_prompt(prompt_path or PROMPT_PATH)

    # ------------------------------------------------------------------
    # Interface pública (compatível com MockAgentFn do orquestrador)
    # ------------------------------------------------------------------

    async def run(
        self,
        client_id: str,
        onboarding: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """
        Executa auditoria sobre os entregáveis do pipeline acumulados até este ponto.

        Lê os resultados de agentes anteriores de:
          1. onboarding["_agent_results"] — injetado pelo orquestrador quando disponível
          2. onboarding["_audit_target"] — descrição textual direta do que auditar
          3. Fallback: audita a própria estrutura do onboarding

        Returns:
            ("done", payload) com AuditResultFull serializado, ou
            ("blocked", {"error": str}) se o LLM falhar ou CoT estiver incompleto.
        """
        agent_results = onboarding.get("_agent_results", {})
        audit_target = onboarding.get("_audit_target")

        deliverable_text = self._build_deliverable_description(
            client_id=client_id,
            onboarding=onboarding,
            agent_results=agent_results,
            audit_target=audit_target,
        )

        try:
            full_result = await self._audit(deliverable_text)
        except ValidationError as exc:
            # CoT incompleto ou veto sem alternativa — rejeitado pelo schema
            logger.error(
                "[Auditor] Schema rejeitou resultado de auditoria para cliente '%s': %s",
                client_id,
                exc,
            )
            return "blocked", {
                "error": f"Resultado de auditoria inválido — schema rejeitou: {exc}",
                "audit_status": "schema_violation",
            }
        except AuditResponseParseError as exc:
            logger.error(
                "[Auditor] Falha ao parsear resposta do LLM para cliente '%s': %s",
                client_id,
                exc,
            )
            return "blocked", {"error": f"Falha ao parsear resposta do auditor: {exc}"}

        # Tenta atualizar o board se houver task_id disponível
        task_id = onboarding.get("_task_id")
        if task_id:
            try:
                await self.board.update_audit_result(
                    task_id=task_id,
                    client_id=client_id,
                    audit_result=full_result.to_board_dict(),
                )
                logger.info(
                    "[Auditor] audit_result escrito no board para task '%s' (status=%s).",
                    task_id,
                    full_result.status,
                )
            except Exception as exc:
                logger.warning(
                    "[Auditor] Não foi possível atualizar board para task '%s': %s",
                    task_id,
                    exc,
                )

        payload = {
            **full_result.to_board_dict(),
            "audit_status": full_result.status,
            "client_id": client_id,
            "scope_audited": list(AUDIT_SCOPE),
        }

        logger.info(
            "[Auditor] Auditoria concluída para cliente '%s': %s — %s",
            client_id,
            full_result.status,
            full_result.justification,
        )
        return "done", payload

    # ------------------------------------------------------------------
    # Core: chamada ao LLM e validação do resultado
    # ------------------------------------------------------------------

    async def _audit(self, deliverable_text: str) -> AuditResultFull:
        """
        Executa o pipeline CoT adversarial via Claude Sonnet.

        Args:
            deliverable_text: Descrição formatada do que deve ser auditado.

        Returns:
            AuditResultFull validado (todos os campos CoT obrigatórios presentes).

        Raises:
            AuditResponseParseError: Se a resposta do LLM não for JSON válido.
            ValidationError: Se o JSON não satisfaz AuditResultFull (CoT incompleto
                             ou veto sem proposed_alternative).
        """
        prompt = self._prompt_template.replace("{deliverable}", deliverable_text)

        response = await self.anthropic.messages.create(
            model=AUDITOR_MODEL,
            max_tokens=AUDITOR_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text.strip()
        raw_data = self._parse_json_response(raw_text)

        # Mapeia "verdict" para "status" (o prompt usa "verdict" por clareza)
        if "verdict" in raw_data and "status" not in raw_data:
            raw_data["status"] = raw_data.pop("verdict")

        # AuditResultFull valida CoT obrigatório + regra veto→proposed_alternative
        # ValidationError propaga para o caller se algo faltar
        return AuditResultFull(**raw_data)

    # ------------------------------------------------------------------
    # Construção do contexto de auditoria
    # ------------------------------------------------------------------

    @staticmethod
    def _build_deliverable_description(
        client_id: str,
        onboarding: dict[str, Any],
        agent_results: dict[str, Any],
        audit_target: str | None,
    ) -> str:
        """
        Formata o contexto de auditoria para o prompt.

        Prioridade:
          1. audit_target (descrição direta)
          2. agent_results (resultados agregados de agentes)
          3. onboarding (informações básicas do cliente)
        """
        if audit_target:
            return audit_target

        sections: list[str] = [f"## Cliente: {client_id}"]

        if agent_results:
            sections.append("\n## Resultados dos Agentes Auditados\n")
            for agent_name, result in agent_results.items():
                sections.append(f"### {agent_name}")
                if isinstance(result, dict):
                    for k, v in result.items():
                        if not k.startswith("_"):  # filtra chaves internas
                            sections.append(f"- **{k}**: {v}")
                else:
                    sections.append(str(result))
                sections.append("")

        if onboarding:
            sections.append("\n## Onboarding do Cliente\n")
            for k, v in onboarding.items():
                if not k.startswith("_"):  # filtra chaves internas do sistema
                    sections.append(f"- **{k}**: {v}")

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Parsing e carregamento
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_response(raw_text: str) -> dict[str, Any]:
        """
        Extrai JSON da resposta do LLM — tolera markdown (```json...```).

        Raises:
            AuditResponseParseError: Se não for possível extrair JSON válido.
        """
        text = raw_text.strip()

        # Remove bloco markdown se presente
        if text.startswith("```"):
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if match:
                text = match.group(1).strip()

        # Tenta parsear diretamente
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Tenta encontrar o primeiro objeto JSON no texto
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        raise AuditResponseParseError(
            f"Não foi possível extrair JSON válido da resposta do auditor. "
            f"Resposta (primeiros 300 chars): {raw_text[:300]}"
        )

    @staticmethod
    def _load_prompt(path: Path | str) -> str:
        """
        Carrega o template de prompt do arquivo.

        Raises:
            FileNotFoundError: Se o arquivo de prompt não existir.
        """
        prompt_path = Path(path)
        if not prompt_path.exists():
            raise FileNotFoundError(
                f"Prompt do auditor não encontrado: {prompt_path}. "
                "Certifique-se de que prompts/base/auditor.md existe."
            )
        content = prompt_path.read_text(encoding="utf-8")
        logger.debug("[Auditor] Prompt carregado de '%s' (%d chars).", prompt_path, len(content))
        return content
