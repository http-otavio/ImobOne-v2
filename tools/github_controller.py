"""
tools/github_controller.py — GitHub Controller Tool

Permite que os agentes do nightly squad interajam com o repositório GitHub:
- Ler contexto do repo (commits, issues, estrutura de arquivos)
- Criar branches de feature
- Commitar arquivos modificados
- Abrir Pull Requests com descrição estruturada
- Listar PRs abertos (para o Briefing Agent)

Variáveis de ambiente necessárias:
    GITHUB_TOKEN  — Personal Access Token com permissões: repo, pull_requests
    GITHUB_REPO   — formato "owner/repo" (ex: "http-otavio/ImobOne-v2")
    GITHUB_BRANCH — branch base para PRs (padrão: "main")
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("github_controller")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO   = os.getenv("GITHUB_REPO",  "http-otavio/ImobOne-v2")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_API    = "https://api.github.com"

_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PRInfo:
    number: int
    title: str
    url: str
    branch: str
    created_at: str
    body: str


@dataclass
class RepoContext:
    recent_commits: list[dict]   # últimos 10 commits
    open_issues: list[dict]      # issues abertas
    open_prs: list[PRInfo]       # PRs abertos
    file_tree: list[str]         # lista de caminhos relevantes


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict | list:
    url = f"{GITHUB_API}{path}"
    with httpx.Client(timeout=30) as client:
        r = client.get(url, headers=_HEADERS, params=params or {})
        r.raise_for_status()
        return r.json()


def _post(path: str, body: dict) -> dict:
    url = f"{GITHUB_API}{path}"
    with httpx.Client(timeout=30) as client:
        r = client.post(url, headers=_HEADERS, json=body)
        r.raise_for_status()
        return r.json()


def _patch(path: str, body: dict) -> dict:
    url = f"{GITHUB_API}{path}"
    with httpx.Client(timeout=30) as client:
        r = client.patch(url, headers=_HEADERS, json=body)
        r.raise_for_status()
        return r.json()


def _put(path: str, body: dict) -> dict:
    url = f"{GITHUB_API}{path}"
    with httpx.Client(timeout=30) as client:
        r = client.put(url, headers=_HEADERS, json=body)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def get_repo_context(max_commits: int = 10) -> RepoContext:
    """
    Retorna o contexto atual do repositório para o PO Agent:
    commits recentes, issues abertas, PRs abertos e árvore de arquivos.
    """
    # Commits recentes
    commits_raw = _get(f"/repos/{GITHUB_REPO}/commits", {"per_page": max_commits})
    recent_commits = [
        {
            "sha": c["sha"][:7],
            "message": c["commit"]["message"].splitlines()[0],
            "author": c["commit"]["author"]["name"],
            "date": c["commit"]["author"]["date"],
        }
        for c in commits_raw
    ]

    # Issues abertas (excluindo PRs)
    issues_raw = _get(f"/repos/{GITHUB_REPO}/issues", {"state": "open", "per_page": 20})
    open_issues = [
        {
            "number": i["number"],
            "title": i["title"],
            "labels": [l["name"] for l in i.get("labels", [])],
            "body": (i.get("body") or "")[:300],
        }
        for i in issues_raw
        if "pull_request" not in i
    ]

    # PRs abertos
    open_prs = get_open_prs()

    # Árvore de arquivos Python + JSON relevantes (excluindo node_modules, venv etc.)
    tree_raw = _get(f"/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}", {"recursive": "1"})
    file_tree = [
        item["path"] for item in tree_raw.get("tree", [])
        if item["type"] == "blob"
        and any(item["path"].endswith(ext) for ext in (".py", ".json", ".md", ".yaml", ".yml"))
        and not any(skip in item["path"] for skip in ("node_modules", ".venv", "venv", "__pycache__", ".git"))
    ]

    return RepoContext(
        recent_commits=recent_commits,
        open_issues=open_issues,
        open_prs=open_prs,
        file_tree=file_tree,
    )


def get_file_content(path: str, branch: str = GITHUB_BRANCH) -> str:
    """Retorna o conteúdo de um arquivo do repo como string."""
    data = _get(f"/repos/{GITHUB_REPO}/contents/{path}", {"ref": branch})
    if isinstance(data, dict) and data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8")
    raise ValueError(f"Não foi possível ler '{path}': formato inesperado.")


def get_file_sha(path: str, branch: str = GITHUB_BRANCH) -> str | None:
    """Retorna o SHA do arquivo (necessário para update). None se não existe."""
    try:
        data = _get(f"/repos/{GITHUB_REPO}/contents/{path}", {"ref": branch})
        return data.get("sha")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


def create_branch(branch_name: str, from_branch: str = GITHUB_BRANCH) -> str:
    """
    Cria uma branch nova a partir de `from_branch`.
    Retorna o SHA do commit de origem.
    Se a branch já existir, retorna o SHA atual dela.
    """
    # SHA do último commit da branch base
    ref_data = _get(f"/repos/{GITHUB_REPO}/git/ref/heads/{from_branch}")
    sha = ref_data["object"]["sha"]

    try:
        _post(f"/repos/{GITHUB_REPO}/git/refs", {
            "ref": f"refs/heads/{branch_name}",
            "sha": sha,
        })
        log.info("Branch '%s' criada a partir de '%s' (%s)", branch_name, from_branch, sha[:7])
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:
            log.info("Branch '%s' já existe — reutilizando.", branch_name)
        else:
            raise

    return sha


def commit_file(
    branch: str,
    path: str,
    content: str,
    message: str,
    author_name: str = "ImobOne Nightly Squad",
    author_email: str = "nightly@imoboneai.com",
) -> str:
    """
    Cria ou atualiza um arquivo no repo via API.
    Retorna o SHA do novo commit.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    existing_sha = get_file_sha(path, branch)

    body: dict[str, Any] = {
        "message": message,
        "content": encoded,
        "branch": branch,
        "committer": {"name": author_name, "email": author_email},
    }
    if existing_sha:
        body["sha"] = existing_sha

    result = _put(f"/repos/{GITHUB_REPO}/contents/{path}", body)
    commit_sha = result["commit"]["sha"][:7]
    log.info("Commit '%s' — %s (%s)", path, message[:60], commit_sha)
    return commit_sha


def commit_files_batch(
    branch: str,
    files: list[dict],  # [{"path": "...", "content": "..."}]
    commit_message: str,
) -> list[str]:
    """
    Commita múltiplos arquivos na mesma branch.
    Retorna lista de SHAs dos commits.
    """
    shas = []
    for i, f in enumerate(files):
        msg = commit_message if i == 0 else f"[nightly] update {f['path']}"
        sha = commit_file(branch, f["path"], f["content"], msg)
        shas.append(sha)
    return shas


def open_pr(
    branch: str,
    title: str,
    body: str,
    base: str = GITHUB_BRANCH,
    labels: list[str] | None = None,
) -> PRInfo:
    """
    Abre um Pull Request. NUNCA faz merge automático.
    Retorna PRInfo com URL para revisão manual.
    """
    pr_data = _post(f"/repos/{GITHUB_REPO}/pulls", {
        "title": title,
        "body": body,
        "head": branch,
        "base": base,
        "draft": False,
    })

    pr = PRInfo(
        number=pr_data["number"],
        title=pr_data["title"],
        url=pr_data["html_url"],
        branch=branch,
        created_at=pr_data["created_at"],
        body=body[:200],
    )

    # Adiciona labels se fornecidas
    if labels:
        try:
            _post(f"/repos/{GITHUB_REPO}/issues/{pr.number}/labels", {"labels": labels})
        except Exception as e:
            log.warning("Não foi possível adicionar labels ao PR #%d: %s", pr.number, e)

    log.info("PR #%d aberto: %s — %s", pr.number, title, pr.url)
    return pr


def get_open_prs() -> list[PRInfo]:
    """Lista todos os PRs abertos no repositório."""
    prs_raw = _get(f"/repos/{GITHUB_REPO}/pulls", {"state": "open", "per_page": 20})
    return [
        PRInfo(
            number=p["number"],
            title=p["title"],
            url=p["html_url"],
            branch=p["head"]["ref"],
            created_at=p["created_at"],
            body=(p.get("body") or "")[:200],
        )
        for p in prs_raw
    ]


def build_pr_body(task: dict, tech_spec: dict, test_results: dict, audit_result: dict) -> str:
    """Monta o corpo do PR com informações estruturadas para revisão humana."""
    passed = test_results.get("passed", 0)
    failed = test_results.get("failed", 0)
    status_emoji = "✅" if failed == 0 else "⚠️"

    return f"""## {task.get('title', 'Nightly Squad Task')}

**Origem:** Nightly Squad autônomo — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
**Task ID:** `{task.get('id', 'unknown')}`

---

### O que foi feito
{task.get('description', '')}

### Abordagem técnica
{tech_spec.get('approach', 'Ver commits para detalhes.')}

### Arquivos modificados
{chr(10).join(f"- `{f}`" for f in tech_spec.get('files_modified', []))}

### Resultado dos testes
{status_emoji} **{passed} passou / {failed} falhou**

```
{test_results.get('summary', 'Sem sumário de testes.')}
```

### Auditoria
**Status:** {audit_result.get('status', 'pending')}
**Justificativa:** {audit_result.get('justification', 'N/A')}

---

> ⚠️ **Este PR foi gerado automaticamente pelo Nightly Squad.**
> Revise cuidadosamente antes de fazer merge.
> Para rejeitar: feche o PR com comentário de motivo.
> Para aprovar: faça Squash & Merge na branch `main`.
"""
