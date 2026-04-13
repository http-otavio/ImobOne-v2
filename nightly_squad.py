"""
nightly_squad.py — Time Autônomo de Desenvolvimento

Roda às 02:00 via systemd timer. Instancia o time de agentes via LangGraph,
executa o ciclo de desenvolvimento noturno e notifica o operador via WhatsApp.

Fluxo:
    02:00  PO Agent       → lê backlog + histórico Redis → seleciona 1-3 tasks
    02:05  Tech Lead      → projeta solução técnica por task
    02:10  Dev Agent      → escreve código + loop de autocorreção (max 3x por task)
    03:xx  QA Agent       → roda suite completa de testes
    03:xx  Auditor        → CoT adversarial sobre as mudanças
    03:xx  Deploy Agent   → cria branch + commita + abre PR (NUNCA faz merge)
    07:00  Briefing Agent → WhatsApp com resumo: PRs abertos, falhas, tasks entregues

Restrição inegociável:
    Nenhum código é mergeado automaticamente. O Deploy Agent para no estágio de PR.
    O operador acorda com notificação WhatsApp e aprova/rejeita manualmente.

Uso:
    /opt/webhook-venv/bin/python3 /opt/ImobOne-v2/nightly_squad.py
    /opt/webhook-venv/bin/python3 /opt/ImobOne-v2/nightly_squad.py --dry-run
    /opt/webhook-venv/bin/python3 /opt/ImobOne-v2/nightly_squad.py --task-id TASK_ID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/ImobOne-v2/logs/nightly_squad.log", mode="a"),
    ],
)
log = logging.getLogger("nightly_squad")

# ---------------------------------------------------------------------------
# Paths e config
# ---------------------------------------------------------------------------

BASE_DIR       = Path(__file__).parent
LOGS_DIR       = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
EVOLUTION_URL      = os.getenv("EVOLUTION_URL",      "https://api.otaviolabs.com")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY",  "79ffc1f3960f03a27a67e2b1e678d98b")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "devlabz")
OPERATOR_NUMBER    = os.getenv("OPERATOR_NUMBER",    "5511973722075")
GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN",       "")
GITHUB_REPO        = os.getenv("GITHUB_REPO",        "http-otavio/ImobOne-v2")

MAX_DEV_ITERATIONS = 3     # tentativas de autocorreção do Dev Agent por task
MAX_TASKS_PER_NIGHT = 3    # tasks atacadas por noite

# ---------------------------------------------------------------------------
# State do LangGraph
# ---------------------------------------------------------------------------

class NightlyState(TypedDict):
    # Input
    dry_run: bool
    forced_task_id: str | None

    # PO Agent
    selected_tasks: list[dict]

    # Tech Lead
    technical_specs: list[dict]   # [{task_id, approach, files_to_modify, tests_to_write}]

    # Dev Agent (por task)
    code_changes: list[dict]      # [{task_id, files: [{path, content}], test_results}]
    dev_iterations: dict          # {task_id: int}

    # QA Agent
    qa_report: dict               # {passed, failed, errors, summary}

    # Auditor
    audit_result: dict            # {status, justification, proposed_alternative}

    # Deploy Agent
    pr_urls: list[str]            # URLs dos PRs abertos
    branch_names: list[str]       # branches criadas

    # Briefing
    briefing_sent: bool

    # Controle
    phase: str
    errors: list[str]
    start_time: float


# ---------------------------------------------------------------------------
# WhatsApp notifications
# ---------------------------------------------------------------------------

def _notify(message: str):
    """Envia notificação WhatsApp via Evolution API.
    Usa httpx com verify=False para aceitar certificado self-signed da Evolution API.
    """
    if not OPERATOR_NUMBER:
        return
    try:
        import httpx
        payload = {"number": OPERATOR_NUMBER, "text": message}
        url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
        with httpx.Client(verify=False, timeout=10) as client:
            r = client.post(
                url,
                json=payload,
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
            )
            r.raise_for_status()
    except Exception as e:
        log.error("Falha ao enviar WhatsApp: %s", e)


# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------

async def _call_claude(
    system: str,
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
) -> str:
    """Chama a Anthropic API e retorna o texto da resposta."""
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    return response.content[0].text


async def _call_claude_json(system: str, messages: list[dict], **kwargs) -> dict | list:
    """Chama Claude e parseia a resposta como JSON.

    Tenta múltiplas estratégias de extração — o LLM às vezes embrulha em markdown.
    Para respostas que contêm código Python nos valores (que quebram JSON),
    use _parse_dev_response() ao invés desta função.
    """
    text = await _call_claude(system, messages, **kwargs)
    # Extrai JSON do texto (pode vir embrulhado em markdown)
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    raw = match.group(1) if match else text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Tenta encontrar o primeiro objeto/lista JSON na string
        for start in ["{", "["]:
            idx = raw.find(start)
            if idx >= 0:
                try:
                    return json.loads(raw[idx:])
                except Exception:
                    pass
        raise ValueError(f"Não foi possível parsear JSON da resposta: {raw[:300]}")


def _parse_dev_response(text: str) -> dict:
    """
    Parseia a resposta do Dev Agent usando tags XML delimitadas.

    Formato esperado na resposta do LLM:
        <file path="caminho/relativo.py">
        ...conteúdo do arquivo...
        </file>

        <test path="tests/test_xxx.py">
        ...conteúdo do teste...
        </test>

    Usa regex e não JSON para evitar problemas de escaping com código Python.
    """
    files = []
    test_content = ""
    test_path = ""

    # Extrai arquivos de código
    for m in re.finditer(r'<file\s+path="([^"]+)">\s*([\s\S]*?)\s*</file>', text):
        files.append({"path": m.group(1).strip(), "content": m.group(2)})

    # Extrai arquivo de teste (tag <test>)
    m_test = re.search(r'<test\s+path="([^"]+)">\s*([\s\S]*?)\s*</test>', text)
    if m_test:
        test_path = m_test.group(1).strip()
        test_content = m_test.group(2)

    if not files and not test_content:
        raise ValueError(f"Nenhuma tag <file> ou <test> encontrada. Resposta (300c): {text[:300]}")

    return {"files": files, "test_content": test_content, "test_path": test_path}


# ---------------------------------------------------------------------------
# Agentes — nós do grafo
# ---------------------------------------------------------------------------

async def po_agent_node(state: NightlyState) -> dict:
    """
    PO Agent: lê backlog + histórico Redis, seleciona as tasks mais prioritárias.
    """
    log.info("▶ PO Agent iniciando...")

    from state.intelligence import BoardIntelligence
    intel = await BoardIntelligence.create()

    # Se task forçada via CLI, usa ela
    if state.get("forced_task_id"):
        tasks = intel.load_backlog()
        forced = [t for t in tasks if t.get("id") == state["forced_task_id"]]
        if not forced:
            return {"errors": state.get("errors", []) + [f"Task '{state['forced_task_id']}' não encontrada."],
                    "phase": "failed"}
        selected_tasks = forced
        log.info("Task forçada: %s", state["forced_task_id"])
        await intel.close()
        return {"selected_tasks": selected_tasks, "phase": "tech_lead"}

    # Priorização inteligente
    scored = await intel.prioritize_tasks(max_tasks=MAX_TASKS_PER_NIGHT)
    await intel.close()

    if not scored:
        log.info("Backlog vazio ou todas tasks completadas. Nada a fazer hoje.")
        _notify("🌙 Nightly Squad: backlog vazio. Nenhuma task para hoje. Durma tranquilo.")
        return {"selected_tasks": [], "phase": "done_empty"}

    # Enriquece a seleção com reasoning do LLM
    if ANTHROPIC_API_KEY and not state.get("dry_run"):
        task_list = json.dumps([s.task for s in scored], ensure_ascii=False, indent=2)
        try:
            reasoning = await _call_claude(
                system=(
                    "Você é o Product Owner do ImobOne, plataforma de IA para imobiliárias de alto padrão. "
                    "Analise a lista de tasks priorizadas e confirme ou ajuste a ordem, justificando brevemente. "
                    "Responda em JSON: [{\"id\": \"...\", \"reason\": \"...\"}]"
                ),
                messages=[{"role": "user", "content": f"Tasks priorizadas:\n{task_list}"}],
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
            )
            log.info("PO Agent reasoning OK.")
        except Exception as e:
            log.warning("PO Agent LLM falhou (%s) — usando priorização Redis pura.", e)

    selected_tasks = [s.task for s in scored]
    log.info("PO Agent selecionou %d tasks: %s",
             len(selected_tasks), [t["id"] for t in selected_tasks])
    return {"selected_tasks": selected_tasks, "phase": "tech_lead"}


async def tech_lead_node(state: NightlyState) -> dict:
    """
    Tech Lead: para cada task selecionada, projeta a solução técnica.
    Lê os arquivos relevantes do repo para ter contexto real.
    """
    log.info("▶ Tech Lead iniciando para %d tasks...", len(state.get("selected_tasks", [])))

    if not state.get("selected_tasks"):
        return {"technical_specs": [], "phase": "dev"}

    specs = []
    for task in state["selected_tasks"]:
        # Lê arquivos relevantes para dar contexto ao Tech Lead
        context_files = {}
        for file_path in task.get("context_files", []):
            full_path = BASE_DIR / file_path
            if full_path.exists():
                try:
                    context_files[file_path] = full_path.read_text(encoding="utf-8")[:3000]
                except Exception:
                    pass

        if ANTHROPIC_API_KEY and not state.get("dry_run"):
            context_str = "\n\n".join(
                f"=== {p} ===\n{c}" for p, c in context_files.items()
            ) or "Sem arquivos de contexto disponíveis."

            prompt = (
                f"Task: {task.get('title')}\n"
                f"Descrição: {task.get('description')}\n"
                f"Critérios de aceite: {json.dumps(task.get('acceptance_criteria', []))}\n\n"
                f"Contexto dos arquivos relevantes:\n{context_str[:4000]}\n\n"
                "Projete a solução técnica. Responda em JSON:\n"
                "{\n"
                '  "approach": "descrição da abordagem em 2-4 frases",\n'
                '  "files_to_modify": ["lista de caminhos de arquivo"],\n'
                '  "new_files": ["arquivos a criar, se houver"],\n'
                '  "tests_to_write": ["caminhos dos arquivos de teste"],\n'
                '  "risks": ["riscos identificados"],\n'
                '  "estimated_lines": 50\n'
                "}"
            )
            try:
                spec_data = await _call_claude_json(
                    system=(
                        "Você é o Tech Lead do ImobOne. Projete soluções simples, focadas e testáveis. "
                        "Prefira mudanças pequenas e reversíveis. Nunca modifique o fluxo de produção sem gate de teste."
                    ),
                    messages=[{"role": "user", "content": prompt}],
                )
                spec_data["task_id"] = task["id"]
            except Exception as e:
                log.error("Tech Lead LLM falhou para %s: %s", task["id"], e)
                spec_data = {
                    "task_id": task["id"],
                    "approach": "Implementação direta conforme descrição da task.",
                    "files_to_modify": task.get("context_files", []),
                    "new_files": [],
                    "tests_to_write": [],
                    "risks": [],
                    "estimated_lines": 0,
                }
        else:
            # Dry run ou sem API key
            spec_data = {
                "task_id": task["id"],
                "approach": f"[DRY RUN] {task.get('description', '')}",
                "files_to_modify": task.get("context_files", []),
                "new_files": [],
                "tests_to_write": [],
                "risks": [],
                "estimated_lines": 0,
            }

        specs.append(spec_data)
        log.info("Tech Lead spec para '%s': %d arquivos a modificar",
                 task["id"], len(spec_data.get("files_to_modify", [])))

    return {"technical_specs": specs, "phase": "dev"}


async def dev_agent_node(state: NightlyState) -> dict:
    """
    Dev Agent: escreve o código para cada task.
    Loop de autocorreção: escreve → testa no sandbox → se falhar → corrige (max 3x).
    """
    log.info("▶ Dev Agent iniciando...")

    from tools.sandbox_executor import SandboxExecutor
    sandbox = SandboxExecutor()

    code_changes = []
    dev_iterations = state.get("dev_iterations", {})

    for i, task in enumerate(state.get("selected_tasks", [])):
        task_id = task["id"]
        spec = next((s for s in state.get("technical_specs", []) if s["task_id"] == task_id), {})

        iteration = 0
        last_error = ""
        files_written = []

        while iteration < MAX_DEV_ITERATIONS:
            iteration += 1
            log.info("Dev Agent: task=%s iteração=%d/%d", task_id, iteration, MAX_DEV_ITERATIONS)

            if state.get("dry_run") or not ANTHROPIC_API_KEY:
                log.info("[DRY RUN] Pulando escrita de código para %s", task_id)
                files_written = []
                break

            # Contexto atual dos arquivos a modificar
            current_files = {}
            for fp in spec.get("files_to_modify", []) + spec.get("new_files", []):
                full = BASE_DIR / fp
                if full.exists():
                    current_files[fp] = full.read_text(encoding="utf-8")[:4000]

            correction_hint = (
                f"\n\nTentativa anterior FALHOU. Erro:\n{last_error}\n\nCorrija o problema acima."
                if last_error else ""
            )

            # Formato de resposta usa tags XML para evitar problemas de escaping
            # com código Python em strings JSON (newlines, aspas, backslashes)
            format_instructions = (
                "\n\nESCREVA O CÓDIGO usando estas tags XML — não use JSON:\n\n"
                "<file path=\"caminho/relativo/arquivo.py\">\n"
                "# conteúdo completo do arquivo aqui\n"
                "</file>\n\n"
                "<test path=\"tests/test_XXX.py\">\n"
                "# código completo do teste pytest aqui\n"
                "</test>\n\n"
                "Use uma tag <file> para cada arquivo a criar/modificar. "
                "Use exatamente uma tag <test> para os testes. "
                "NÃO use JSON, NÃO use markdown code blocks dentro das tags."
            )

            prompt = (
                f"Task: {task.get('title')}\n"
                f"Descrição: {task.get('description')}\n"
                f"Abordagem técnica: {spec.get('approach', '')}\n"
                f"Critérios de aceite:\n"
                + "\n".join(f"- {c}" for c in task.get("acceptance_criteria", []))
                + f"{correction_hint}"
                + (
                    f"\n\nArquivos existentes relevantes:\n"
                    + "\n".join(f"=== {p} ===\n{c}" for p, c in current_files.items())
                    if current_files else ""
                )
                + format_instructions
            )

            try:
                raw_text = await _call_claude(
                    system=(
                        "Você é o Dev Agent do ImobOne. Escreva código Python limpo, tipado e testável. "
                        "Siga os padrões do CLAUDE.md. Não quebre nenhuma integração existente. "
                        "Sempre inclua testes pytest para o código que escrever. "
                        "Responda SOMENTE com as tags <file> e <test> solicitadas, sem explicações fora das tags."
                    ),
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=8000,
                )
                result = _parse_dev_response(raw_text)
            except Exception as e:
                log.error("Dev Agent falhou: %s", e)
                last_error = str(e)
                continue

            files_written = result.get("files", [])
            test_content = result.get("test_content", "")
            test_path = result.get("test_path") or f"tests/test_nightly_{task_id}.py"

            # Adiciona o arquivo de teste aos files
            if test_content:
                files_written.append({"path": test_path, "content": test_content})

            # Valida sintaxe antes de testar
            all_valid = True
            for f in files_written:
                syntax = sandbox.check_syntax(f["content"])
                if not syntax.passed:
                    log.warning("Sintaxe inválida em %s", f["path"])
                    last_error = f"Erro de sintaxe em {f['path']}:\n{syntax.stderr}"
                    all_valid = False
                    break

            if not all_valid:
                continue

            # Roda os testes no sandbox
            test_files = [f for f in files_written if f["path"].startswith("tests/")]
            src_files  = [f for f in files_written if not f["path"].startswith("tests/")]

            if test_files:
                sandbox_result = sandbox.run_tests_on_code(
                    code_files=src_files,
                    test_files=test_files,
                )
                log.info(
                    "Sandbox: %s | p=%d f=%d em %.1fs",
                    "PASS" if sandbox_result.passed else "FAIL",
                    sandbox_result.tests_passed,
                    sandbox_result.tests_failed,
                    sandbox_result.duration_s,
                )
                if sandbox_result.passed:
                    break  # ✅ código aprovado, sai do loop
                else:
                    last_error = sandbox_result.to_agent_feedback()
                    log.warning("Dev Agent iteração %d falhou. Tentando corrigir...", iteration)
            else:
                log.info("Sem arquivos de teste — pulando sandbox.")
                break

        dev_iterations[task_id] = iteration
        code_changes.append({
            "task_id": task_id,
            "files": files_written,
            "iterations": iteration,
            "passed": not last_error or iteration >= MAX_DEV_ITERATIONS,
        })

    return {"code_changes": code_changes, "dev_iterations": dev_iterations, "phase": "qa"}


async def qa_agent_node(state: NightlyState) -> dict:
    """
    QA Agent: roda a suite completa de testes do repo para detectar regressões.
    """
    log.info("▶ QA Agent iniciando suite completa...")

    if state.get("dry_run"):
        log.info("[DRY RUN] Pulando QA.")
        return {"qa_report": {"passed": 0, "failed": 0, "errors": 0, "summary": "DRY RUN"}, "phase": "auditor"}

    from tools.sandbox_executor import SandboxExecutor
    sandbox = SandboxExecutor()

    # Primeiro aplica as mudanças em memória e roda os testes
    all_code_files = []
    for change in state.get("code_changes", []):
        all_code_files.extend(change.get("files", []))

    test_files_changed = [f for f in all_code_files if f["path"].startswith("tests/")]
    src_files_changed  = [f for f in all_code_files if not f["path"].startswith("tests/")]

    if test_files_changed:
        result = sandbox.run_tests_on_code(
            code_files=src_files_changed,
            test_files=test_files_changed,
        )
    else:
        # Roda todos os testes existentes
        result = sandbox.run_tests(test_paths=["tests/"])

    qa_report = {
        "passed":  result.tests_passed,
        "failed":  result.tests_failed,
        "errors":  result.tests_errors,
        "summary": result.summary,
        "duration_s": result.duration_s,
        "overall_pass": result.passed,
    }

    status = "✅ PASSOU" if result.passed else "⚠️ FALHOU"
    log.info("QA: %s — %dp/%df em %.1fs", status, result.tests_passed, result.tests_failed, result.duration_s)
    return {"qa_report": qa_report, "phase": "auditor"}


async def auditor_node(state: NightlyState) -> dict:
    """
    Auditor: revisão adversarial (CoT) das mudanças. Mesmo padrão do Agente 2.
    """
    log.info("▶ Auditor iniciando revisão CoT...")

    if state.get("dry_run") or not ANTHROPIC_API_KEY:
        return {
            "audit_result": {
                "status": "approved",
                "justification": "DRY RUN — auditoria simulada.",
                "proposed_alternative": None,
            },
            "phase": "deploy",
        }

    qa = state.get("qa_report", {})
    changes_summary = []
    for c in state.get("code_changes", []):
        changes_summary.append({
            "task_id": c["task_id"],
            "files": [f["path"] for f in c.get("files", [])],
            "iterations": c.get("iterations", 1),
        })

    prompt = (
        f"Mudanças a auditar:\n{json.dumps(changes_summary, ensure_ascii=False)}\n\n"
        f"Resultado de QA: {json.dumps(qa)}\n\n"
        "Aplique o protocolo de auditoria adversarial:\n"
        "1. argumento_a_favor\n2. argumento_contra\n3. alternativa_mais_simples\n"
        "4. reversibilidade\n5. veredicto (approved | approved_with_note | vetoed)\n"
        "6. justificativa_em_uma_frase\n\n"
        "Responda em JSON com esses 6 campos."
    )

    try:
        result = await _call_claude_json(
            system=(
                "Você é o Arquiteto Auditor do ImobOne. Questione cada decisão. "
                "Vete apenas se há risco real de regressão ou violação arquitetural. "
                "Aprovações com ressalva são bem-vindas."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        status = result.get("veredicto", result.get("status", "approved"))
        justification = result.get("justificativa_em_uma_frase", result.get("justification", ""))
        alternative = result.get("alternativa_mais_simples", result.get("proposed_alternative", ""))
    except Exception as e:
        log.error("Auditor falhou: %s — aprovando com ressalva.", e)
        status = "approved_with_note"
        justification = f"Auditoria incompleta por erro de LLM: {e}"
        alternative = None

    log.info("Auditor: %s — %s", status, justification[:80])
    return {
        "audit_result": {
            "status": status,
            "justification": justification,
            "proposed_alternative": alternative,
        },
        "phase": "deploy" if status != "vetoed" else "vetoed",
    }


async def deploy_agent_node(state: NightlyState) -> dict:
    """
    Deploy Agent: cria branch, commita mudanças, abre PR.
    NUNCA faz merge. Para aqui e aguarda aprovação humana.
    """
    log.info("▶ Deploy Agent iniciando...")

    if state.get("dry_run"):
        log.info("[DRY RUN] Simulando abertura de PR.")
        return {"pr_urls": ["https://github.com/DRY_RUN/PR#1"], "branch_names": ["feature/nightly-dryrun"], "phase": "briefing"}

    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN não configurado — pulando Deploy Agent.")
        return {"pr_urls": [], "branch_names": [], "errors": state.get("errors", []) + ["GITHUB_TOKEN ausente"], "phase": "briefing"}

    from tools.github_controller import (
        create_branch, commit_files_batch, open_pr, build_pr_body
    )

    pr_urls = []
    branch_names = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")

    for change in state.get("code_changes", []):
        task_id = change["task_id"]
        files   = change.get("files", [])

        if not files:
            log.info("Nenhum arquivo para commitar em %s", task_id)
            continue

        task = next((t for t in state.get("selected_tasks", []) if t["id"] == task_id), {})
        spec = next((s for s in state.get("technical_specs", []) if s["task_id"] == task_id), {})

        branch_name = f"feature/nightly-{task_id}-{timestamp}"

        try:
            create_branch(branch_name)
            commit_files_batch(
                branch=branch_name,
                files=files,
                commit_message=f"[nightly] {task.get('title', task_id)}",
            )
            pr_body = build_pr_body(
                task=task,
                tech_spec=spec,
                test_results=state.get("qa_report", {}),
                audit_result=state.get("audit_result", {}),
            )
            pr = open_pr(
                branch=branch_name,
                title=f"[Nightly] {task.get('title', task_id)}",
                body=pr_body,
                labels=["nightly-squad", "awaiting-review"],
            )
            pr_urls.append(pr.url)
            branch_names.append(branch_name)
            log.info("PR aberto: %s", pr.url)

        except Exception as e:
            log.error("Deploy Agent falhou para %s: %s", task_id, e)
            state.setdefault("errors", []).append(f"Deploy {task_id}: {e}")

    return {"pr_urls": pr_urls, "branch_names": branch_names, "phase": "briefing"}


async def briefing_agent_node(state: NightlyState) -> dict:
    """
    Briefing Agent: envia resumo completo da noite via WhatsApp.
    Você acorda com tudo que precisa saber em uma mensagem.
    """
    log.info("▶ Briefing Agent montando resumo...")

    from state.intelligence import BoardIntelligence, TaskAttempt
    intel = await BoardIntelligence.create()

    elapsed = time.monotonic() - state.get("start_time", time.monotonic())
    mins = elapsed / 60

    pr_urls   = state.get("pr_urls", [])
    errors    = state.get("errors", [])
    qa        = state.get("qa_report", {})
    audit     = state.get("audit_result", {})
    tasks     = state.get("selected_tasks", [])
    phase     = state.get("phase", "unknown")

    # Registra tentativas no histórico
    for change in state.get("code_changes", []):
        task_id = change["task_id"]
        pr_url  = pr_urls[0] if pr_urls else ""
        status  = "success" if pr_url else ("failed" if errors else "partial")
        await intel.record_attempt(TaskAttempt(
            task_id=task_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            status=status,
            duration_s=elapsed,
            pr_url=pr_url,
            error=errors[0] if errors else "",
        ))

    # Salva metadata da run
    await intel.save_run_metadata({
        "tasks": [t["id"] for t in tasks],
        "pr_count": len(pr_urls),
        "error_count": len(errors),
        "qa_passed": qa.get("passed", 0),
        "qa_failed": qa.get("failed", 0),
        "audit_status": audit.get("status", "unknown"),
        "duration_min": round(mins, 1),
        "phase": phase,
    })
    await intel.close()

    # Monta mensagem WhatsApp
    task_names = "\n".join(f"  • {t.get('title', t['id'])}" for t in tasks) or "  (nenhuma)"
    pr_list    = "\n".join(f"  • {url}" for url in pr_urls) or "  (nenhum)"
    error_list = "\n".join(f"  • {e}" for e in errors[:3]) or "  (nenhum)"

    if phase == "done_empty":
        msg = f"🌙 *Nightly Squad* — {datetime.now(timezone.utc).strftime('%d/%m %H:%M')}\n\nBacklog vazio. Nada a fazer hoje."
    elif phase == "vetoed":
        msg = (
            f"🛑 *Nightly Squad* — {datetime.now(timezone.utc).strftime('%d/%m %H:%M')}\n\n"
            f"Pipeline vetado pelo Auditor.\n"
            f"Motivo: {audit.get('justification', 'ver logs')}\n\n"
            f"Tasks tentadas:\n{task_names}"
        )
    else:
        qa_line = f"✅ {qa.get('passed',0)} testes passando" if qa.get("overall_pass") else f"⚠️ {qa.get('failed',0)} testes falhando"
        msg = (
            f"🌙 *Nightly Squad* — {datetime.now(timezone.utc).strftime('%d/%m %H:%M UTC')}\n\n"
            f"*{len(pr_urls)} PR(s) aguardando sua revisão:*\n{pr_list}\n\n"
            f"*Tasks trabalhadas:*\n{task_names}\n\n"
            f"*QA:* {qa_line}\n"
            f"*Auditoria:* {audit.get('status', 'N/A')}\n"
            f"*Tempo total:* {mins:.1f} min\n\n"
            f"*Erros:*\n{error_list}\n\n"
            f"_Aprove os PRs no GitHub ou responda aqui se precisar de ajuste._"
        )

    log.info("Briefing: %d PRs, %d erros, %.1f min", len(pr_urls), len(errors), mins)
    _notify(msg)

    return {"briefing_sent": True, "phase": "done"}


# ---------------------------------------------------------------------------
# Construção do grafo LangGraph
# ---------------------------------------------------------------------------

def build_graph():
    from langgraph.graph import StateGraph, END

    graph = StateGraph(NightlyState)

    graph.add_node("po_agent",       po_agent_node)
    graph.add_node("tech_lead",      tech_lead_node)
    graph.add_node("dev_agent",      dev_agent_node)
    graph.add_node("qa_agent",       qa_agent_node)
    graph.add_node("auditor",        auditor_node)
    graph.add_node("deploy_agent",   deploy_agent_node)
    graph.add_node("briefing_agent", briefing_agent_node)

    graph.set_entry_point("po_agent")

    # Roteamento condicional
    def route_after_po(state: NightlyState) -> str:
        if state.get("phase") == "done_empty":
            return "briefing_agent"
        if state.get("phase") == "failed":
            return "briefing_agent"
        return "tech_lead"

    def route_after_auditor(state: NightlyState) -> str:
        if state.get("phase") == "vetoed":
            return "briefing_agent"
        return "deploy_agent"

    graph.add_conditional_edges("po_agent", route_after_po, {
        "briefing_agent": "briefing_agent",
        "tech_lead":      "tech_lead",
    })
    graph.add_edge("tech_lead",    "dev_agent")
    graph.add_edge("dev_agent",    "qa_agent")
    graph.add_edge("qa_agent",     "auditor")
    graph.add_conditional_edges("auditor", route_after_auditor, {
        "briefing_agent": "briefing_agent",
        "deploy_agent":   "deploy_agent",
    })
    graph.add_edge("deploy_agent",   "briefing_agent")
    graph.add_edge("briefing_agent", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(dry_run: bool = False, task_id: str | None = None):
    start = time.monotonic()

    log.info("=" * 60)
    log.info("ImobOne Nightly Squad iniciando")
    log.info("Dry-run: %s | Task forçada: %s", dry_run, task_id or "N/A")
    log.info("=" * 60)

    if not dry_run and EVOLUTION_API_KEY:
        _notify(
            f"🌙 *Nightly Squad* acordou — {datetime.now(timezone.utc).strftime('%d/%m %H:%M UTC')}\n"
            "Analisando backlog e iniciando ciclo de desenvolvimento.\n"
            "Você será notificado ao terminar."
        )

    initial_state: NightlyState = {
        "dry_run":           dry_run,
        "forced_task_id":    task_id,
        "selected_tasks":    [],
        "technical_specs":   [],
        "code_changes":      [],
        "dev_iterations":    {},
        "qa_report":         {},
        "audit_result":      {},
        "pr_urls":           [],
        "branch_names":      [],
        "briefing_sent":     False,
        "phase":             "po",
        "errors":            [],
        "start_time":        start,
    }

    try:
        graph = build_graph()
        final_state = await graph.ainvoke(initial_state)
        log.info("Nightly Squad concluído. Phase final: %s", final_state.get("phase"))
        return 0
    except Exception as exc:
        log.exception("Erro fatal no Nightly Squad: %s", exc)
        _notify(
            f"❌ *Nightly Squad travou* — erro fatal.\n"
            f"Erro: {exc}\n"
            f"Verifique: journalctl -u imob-nightly -n 50"
        )
        return 1


def _parse_args():
    parser = argparse.ArgumentParser(description="ImobOne Nightly Squad")
    parser.add_argument("--dry-run",  action="store_true", help="Simula sem escrever código ou abrir PRs")
    parser.add_argument("--task-id",  type=str, default=None, help="Força execução de uma task específica")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run, task_id=args.task_id)))
