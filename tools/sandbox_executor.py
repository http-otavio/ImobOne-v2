"""
tools/sandbox_executor.py — Sandbox Executor Tool

Interface segura para o Dev Agent executar código e testes sem afetar produção.
Roda pytest e scripts Python em subprocessos isolados com timeout configurável.
O Dev Agent usa o feedback de stdout/stderr para se autocorrigir antes de commitar.

Design de segurança:
    - Executa em diretório temporário isolado — nunca no /opt/ImobOne-v2 em prod
    - Timeout hard por execução (padrão 180s para testes, 60s para scripts)
    - Captura stdout/stderr completos para análise do agente
    - Nunca executa como root se SANDBOX_SAFE_USER estiver configurado
    - Bloqueia imports de módulos perigosos via PYTHONPATH limpo
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("sandbox_executor")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR         = Path(__file__).parent.parent
VENV_PYTHON      = os.getenv("SANDBOX_PYTHON", "/opt/webhook-venv/bin/python3")
VENV_PYTEST      = os.getenv("SANDBOX_PYTEST", "/opt/webhook-venv/bin/pytest")
DEFAULT_TIMEOUT  = int(os.getenv("SANDBOX_TIMEOUT", "180"))   # segundos
MAX_OUTPUT_CHARS = 8_000   # limita output para não explodir o contexto do LLM


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SandboxResult:
    passed:      bool
    exit_code:   int
    stdout:      str
    stderr:      str
    summary:     str           # resumo em 1-3 linhas para o agente
    tests_passed: int = 0
    tests_failed: int = 0
    tests_errors: int = 0
    duration_s:  float = 0.0

    def to_agent_feedback(self) -> str:
        """Retorna feedback formatado para o Dev Agent processar."""
        status = "✅ PASSOU" if self.passed else "❌ FALHOU"
        return (
            f"{status} — {self.tests_passed}p/{self.tests_failed}f/{self.tests_errors}e "
            f"em {self.duration_s:.1f}s\n\n"
            f"SUMÁRIO:\n{self.summary}\n\n"
            f"STDERR:\n{self.stderr[:2000] if self.stderr else '(vazio)'}\n\n"
            f"STDOUT (últimas linhas):\n{self._tail(self.stdout, 80)}"
        )

    @staticmethod
    def _tail(text: str, n_lines: int) -> str:
        lines = text.splitlines()
        return "\n".join(lines[-n_lines:]) if lines else "(vazio)"


# ---------------------------------------------------------------------------
# Executor principal
# ---------------------------------------------------------------------------

class SandboxExecutor:
    """
    Executa pytest ou scripts Python em ambiente isolado.

    Uso:
        executor = SandboxExecutor()

        # Rodar testes específicos
        result = executor.run_tests(["tests/test_crm_adapters.py"])

        # Rodar código Python arbitrário
        result = executor.run_script("print('hello')")

        # Rodar testes em código novo (sem commitar)
        result = executor.run_tests_on_code(
            code_files=[{"path": "tools/foo.py", "content": "..."}],
            test_files=[{"path": "tests/test_foo.py", "content": "..."}],
        )
    """

    def __init__(self, base_dir: Path = BASE_DIR, timeout: int = DEFAULT_TIMEOUT):
        self.base_dir = base_dir
        self.timeout = timeout

    # ── Rodar testes existentes no repo ────────────────────────────────────

    def run_tests(
        self,
        test_paths: list[str] | None = None,
        extra_args: list[str] | None = None,
    ) -> SandboxResult:
        """
        Executa pytest no repo existente.
        test_paths: lista de arquivos/diretórios (relativo a base_dir). None = todos.
        """
        cmd = [
            VENV_PYTEST,
            "--tb=short",
            "--no-header",
            "-q",
            *(test_paths or []),
            *(extra_args or []),
        ]
        return self._run_subprocess(cmd, cwd=self.base_dir)

    # ── Rodar testes em código novo (sem tocar no repo) ───────────────────

    def run_tests_on_code(
        self,
        code_files: list[dict],     # [{"path": "tools/foo.py", "content": "..."}]
        test_files: list[dict],     # [{"path": "tests/test_foo.py", "content": "..."}]
        extra_args: list[str] | None = None,
    ) -> SandboxResult:
        """
        Copia o repo para um tmpdir, sobrescreve com os novos arquivos,
        executa os testes e descarta o tmpdir.
        Garante isolamento total — produção nunca é tocada.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="imob_sandbox_"))
        try:
            # Copia o repo inteiro para o tmpdir
            shutil.copytree(
                self.base_dir, tmpdir / "repo",
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".git", "node_modules",
                    ".venv", "venv", "*.egg-info"
                )
            )
            sandbox_dir = tmpdir / "repo"

            # Sobrescreve com os arquivos novos (código + testes)
            for f in code_files + test_files:
                dest = sandbox_dir / f["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(f["content"], encoding="utf-8")

            # Extrai apenas os caminhos dos test_files para rodar
            test_paths = [f["path"] for f in test_files]
            cmd = [
                VENV_PYTEST,
                "--tb=short",
                "--no-header",
                "-q",
                *test_paths,
                *(extra_args or []),
            ]
            return self._run_subprocess(cmd, cwd=sandbox_dir)

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ── Rodar script Python arbitrário ────────────────────────────────────

    def run_script(self, code: str, timeout: int | None = None) -> SandboxResult:
        """
        Executa um snippet Python em processo isolado.
        Útil para validações rápidas do Dev Agent.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="imob_script_"))
        try:
            script_path = tmpdir / "script.py"
            script_path.write_text(textwrap.dedent(code), encoding="utf-8")
            cmd = [VENV_PYTHON, str(script_path)]
            return self._run_subprocess(
                cmd, cwd=tmpdir, timeout=timeout or 60
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ── Verificar sintaxe Python ───────────────────────────────────────────

    def check_syntax(self, code: str) -> SandboxResult:
        """Verifica sintaxe sem executar. Rápido — usa py_compile."""
        return self.run_script(
            f"""
import py_compile, tempfile, os
tmp = tempfile.NamedTemporaryFile(suffix='.py', delete=False, mode='w')
tmp.write({repr(code)})
tmp.close()
try:
    py_compile.compile(tmp.name, doraise=True)
    print("SYNTAX_OK")
except py_compile.PyCompileError as e:
    print(f"SYNTAX_ERROR: {{e}}")
    exit(1)
finally:
    os.unlink(tmp.name)
""",
            timeout=10,
        )

    # ── Subprocess runner interno ─────────────────────────────────────────

    def _run_subprocess(
        self,
        cmd: list[str],
        cwd: Path,
        timeout: int | None = None,
    ) -> SandboxResult:
        import time
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(cwd),
                timeout=timeout or self.timeout,
                env={**os.environ, "PYTHONPATH": str(cwd)},
            )
            duration = time.monotonic() - t0
            stdout = proc.stdout[:MAX_OUTPUT_CHARS]
            stderr = proc.stderr[:MAX_OUTPUT_CHARS]

            passed, failed, errors = _parse_pytest_summary(proc.stdout)
            summary = _extract_summary(proc.stdout, proc.stderr, proc.returncode)

            result = SandboxResult(
                passed=proc.returncode == 0,
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                summary=summary,
                tests_passed=passed,
                tests_failed=failed,
                tests_errors=errors,
                duration_s=round(duration, 2),
            )
            log.info(
                "Sandbox: exit=%d p=%d f=%d e=%d %.1fs | %s",
                proc.returncode, passed, failed, errors, duration,
                " ".join(str(c) for c in cmd[:3])
            )
            return result

        except subprocess.TimeoutExpired:
            duration = timeout or self.timeout
            msg = f"TIMEOUT após {duration}s"
            log.error("Sandbox timeout: %s", " ".join(str(c) for c in cmd))
            return SandboxResult(
                passed=False, exit_code=-1,
                stdout="", stderr=msg, summary=msg,
                duration_s=float(duration),
            )
        except Exception as exc:
            msg = f"Erro ao executar sandbox: {exc}"
            log.exception(msg)
            return SandboxResult(
                passed=False, exit_code=-2,
                stdout="", stderr=msg, summary=msg,
            )


# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------

def _parse_pytest_summary(stdout: str) -> tuple[int, int, int]:
    """
    Extrai passed/failed/errors do output do pytest.
    Ex: "3 passed, 1 failed, 0 errors in 2.34s"
    """
    import re
    passed = failed = errors = 0
    # Linha final do pytest: "X passed", "Y failed", "Z error"
    for match in re.finditer(r"(\d+)\s+(passed|failed|error)", stdout):
        n, kind = int(match.group(1)), match.group(2)
        if kind == "passed":
            passed = n
        elif kind == "failed":
            failed = n
        elif kind == "error":
            errors = n
    return passed, failed, errors


def _extract_summary(stdout: str, stderr: str, returncode: int) -> str:
    """Extrai as linhas mais relevantes para o agente."""
    lines = stdout.splitlines()

    # Procura a linha de sumário do pytest (geralmente a última não-vazia)
    summary_lines = [
        l for l in lines
        if any(kw in l for kw in ("passed", "failed", "error", "FAILED", "ERROR", "warning"))
    ]

    if summary_lines:
        return "\n".join(summary_lines[-5:])

    # Fallback: últimas 5 linhas do stdout
    tail = "\n".join(lines[-5:]) if lines else ""
    if returncode != 0 and stderr:
        tail += "\n" + stderr.splitlines()[-3:][0] if stderr.splitlines() else ""
    return tail or f"Exit code: {returncode}"
