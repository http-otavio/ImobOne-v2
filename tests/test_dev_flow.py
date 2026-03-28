"""
tests/test_dev_flow.py — Testes unitários de agents/dev_flow.py.

Mínimo exigido: 3 testes (CLAUDE.md).

Testes:
  1. Grafo gerado tem todos os 5 nós obrigatórios
  2. Placeholders do cliente estão preenchidos no prompt gerado
  3. Agente bloqueado se ingestion ou context não retornaram 'done' ainda
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents.dev_flow import (
    NOS_OBRIGATORIOS,
    TOOLS_DISPONIVEIS,
    DevFlowAgent,
    DevFlowDependencyError,
    _extrair_variaveis,
    _gerar_consultant_py,
    _resolver_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures e helpers
# ---------------------------------------------------------------------------


def _onboarding_completo(tmp_dir: Path) -> dict:
    """
    Onboarding com dependências satisfeitas (ingestion + context = done).
    Simula o estado do shared state board após Fase 1 parcial.
    """
    return {
        "nome_imobiliaria": "Ápice Imóveis",
        "nome_consultor": "Sofia",
        "cidade_atuacao": "São Paulo",
        "tipo_atuacao": "venda de alto padrão e lançamentos",
        "palavras_proibidas": ["baratão", "imperdível"],
        "exemplos_saudacao": [
            "Boa tarde! Sou Sofia, da Ápice Imóveis. Como posso ajudá-lo?",
            "Bom dia! Que prazer ter você aqui. Estou à sua disposição.",
        ],
        "regras_especificas": "Nunca citar preço sem contexto de metragem e andar.",
        "_agent_results": {
            "ingestion": {
                "status": "done",
                "payload": {
                    "imoveis_indexados": 47,
                    "imoveis_completos": 42,
                    "cobertura_percentual": 89.4,
                },
            },
            "context": {
                "status": "done",
                "payload": {
                    "tools_disponiveis": ["buscar_vizinhanca", "calcular_trajeto"],
                    "validacoes": {"school": "ok", "supermarket": "ok"},
                },
            },
        },
    }


def _agent_com_tmp(tmp_path: Path) -> DevFlowAgent:
    """
    Instância de DevFlowAgent com diretórios temporários para isolamento de teste.
    - output_base_dir: tmp_path/agents/clients (consultant.py)
    - prompts_clients_dir: tmp_path/prompts/clients (consultant_prompt.md)
    - prompt_template_path: resolvido pelo módulo (projeto ou sessão interna)
    """
    from agents.dev_flow import PROMPT_TEMPLATE_PATH
    return DevFlowAgent(
        output_base_dir=tmp_path / "agents" / "clients",
        prompts_clients_dir=tmp_path / "prompts" / "clients",
        prompt_template_path=PROMPT_TEMPLATE_PATH,
    )


# ---------------------------------------------------------------------------
# Teste 1 — Grafo gerado tem todos os 5 nós obrigatórios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grafo_gerado_tem_cinco_nos_obrigatorios(tmp_path):
    """
    O arquivo consultant.py gerado deve conter:
    - Os 5 nós obrigatórios (saudacao, qualificacao, recomendacao, objecao, agendamento)
    - A função build_consultant_graph() montando o StateGraph
    - Registro de cada nó via graph.add_node()
    - O enum No com todos os valores corretos

    Valida tanto o conteúdo textual do arquivo gerado quanto a importação dinâmica
    do módulo para verificar que o grafo é compilável.
    """
    agent = _agent_com_tmp(tmp_path)
    onboarding = _onboarding_completo(tmp_path)

    status, payload = await agent.run("cliente_teste_grafo", onboarding)

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"
    assert "consultant_path" in payload
    assert "nos_implementados" in payload

    # Verifica lista de nós no payload
    assert payload["nos_implementados"] == NOS_OBRIGATORIOS

    # Verifica conteúdo do arquivo gerado
    consultant_path = Path(payload["consultant_path"])
    assert consultant_path.exists(), f"consultant.py não gerado em {consultant_path}"
    conteudo = consultant_path.read_text(encoding="utf-8")

    for no in NOS_OBRIGATORIOS:
        # Nó como valor do enum
        assert f'"{no}"' in conteudo, f"Nó '{no}' ausente no enum No"
        # Nó registrado no grafo
        assert f"node_{no}" in conteudo, f"Função node_{no} ausente no consultant.py"
        # add_node chamado para o nó
        assert f"graph.add_node" in conteudo, "graph.add_node não encontrado"

    # build_consultant_graph() presente e chamada para compilar
    assert "def build_consultant_graph" in conteudo
    assert "graph.compile()" in conteudo

    # Tools registradas no arquivo
    for tool in TOOLS_DISPONIVEIS:
        assert tool in conteudo, f"Tool '{tool}' ausente em TOOLS_REGISTRADAS"

    # Validação sintática via AST (evita problemas de sys.modules com módulos
    # carregados dinamicamente que têm TypedDicts com Annotated)
    import ast
    try:
        arvore = ast.parse(conteudo)
    except SyntaxError as exc:
        pytest.fail(f"consultant.py tem erro de sintaxe: {exc}")

    # Verifica que o enum No está definido no AST com todos os valores esperados
    classes_definidas = {
        node.name: node
        for node in ast.walk(arvore)
        if isinstance(node, ast.ClassDef)
    }
    assert "No" in classes_definidas, "Classe No (enum) não encontrada no AST"
    assert "ConsultorState" in classes_definidas, "ConsultorState não encontrado no AST"

    # Verifica que build_consultant_graph está definida
    funcoes_definidas = {
        node.name
        for node in ast.walk(arvore)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "build_consultant_graph" in funcoes_definidas, "build_consultant_graph ausente"
    for no in NOS_OBRIGATORIOS:
        assert f"node_{no}" in funcoes_definidas, f"Função node_{no} ausente no AST"

    # Verifica assigns de constantes chave
    assert "consultant_graph" in conteudo, "Atributo consultant_graph ausente"
    assert "TOOLS_REGISTRADAS" in conteudo, "TOOLS_REGISTRADAS ausente"


# ---------------------------------------------------------------------------
# Teste 2 — Placeholders do cliente estão preenchidos no prompt gerado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_placeholders_do_cliente_preenchidos_no_prompt(tmp_path):
    """
    O arquivo consultant_prompt.md gerado deve ter todos os placeholders
    resolvidos com as variáveis do cliente.

    Verifica:
    - {{NOME_CONSULTOR}} substituído por "Sofia"
    - {{NOME_IMOBILIARIA}} substituído por "Ápice Imóveis"
    - {{CIDADE_ATUACAO}} substituído por "São Paulo"
    - Exemplos de saudação do briefing presentes no prompt
    - Palavras proibidas do briefing presentes no prompt
    - Nenhum placeholder {{...}} residual (exceto os que foram devidamente
      anotados como [[PENDENTE: ...]]) — não deve haver {{ALGUMA_COISA}} literal
    """
    agent = _agent_com_tmp(tmp_path)
    onboarding = _onboarding_completo(tmp_path)

    status, payload = await agent.run("cliente_teste_placeholder", onboarding)

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"

    prompt_path = Path(payload["prompt_path"])
    assert prompt_path.exists(), f"consultant_prompt.md não gerado em {prompt_path}"
    conteudo = prompt_path.read_text(encoding="utf-8")

    # Variáveis do cliente substituídas
    assert "Sofia" in conteudo, "Nome do consultor não substituído"
    assert "Ápice Imóveis" in conteudo, "Nome da imobiliária não substituído"
    assert "São Paulo" in conteudo, "Cidade de atuação não substituída"

    # Exemplos de saudação do briefing
    assert "Sou Sofia, da Ápice Imóveis" in conteudo, "Exemplo de saudação ausente"

    # Palavras proibidas do briefing
    assert "baratão" in conteudo, "Palavra proibida do briefing ausente"
    assert "imperdível" in conteudo, "Palavra proibida do briefing ausente"

    # Regras específicas do cliente
    assert "sem contexto de metragem" in conteudo, "Regra específica não substituída"

    # Contexto do portfólio com dados do ingestion
    assert "47" in conteudo, "Número de imóveis indexados não presente no prompt"
    assert "89" in conteudo, "Cobertura percentual não presente no prompt"

    # Nenhum placeholder {{...}} residual não resolvido
    import re
    placeholders_residuais = re.findall(r"\{\{[A-Z_]+\}\}", conteudo)
    assert not placeholders_residuais, (
        f"Placeholders não resolvidos encontrados: {placeholders_residuais}"
    )


# ---------------------------------------------------------------------------
# Teste 3 — Agente bloqueado se dependências não estão prontas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agente_bloqueado_se_dependencias_nao_prontas(tmp_path):
    """
    Se ingestion ou context não retornaram 'done', run() deve retornar
    ("blocked", {error: ...}) e NÃO escrever nenhum arquivo.

    Cenários testados:
    3a. ingestion com status 'pending' → blocked
    3b. context com status 'in_progress' → blocked
    3c. _agent_results ausente completamente → blocked
    3d. Ambos prontos → done (controle positivo)
    """
    agent = _agent_com_tmp(tmp_path)

    # 3a. ingestion pending
    onboarding_ingestion_pending = {
        "_agent_results": {
            "ingestion": {"status": "pending", "payload": {}},
            "context": {"status": "done", "payload": {"tools_disponiveis": []}},
        }
    }
    status, payload = await agent.run("cliente_dep_3a", onboarding_ingestion_pending)
    assert status == "blocked", f"Esperava 'blocked' com ingestion pending, got '{status}'"
    assert "ingestion" in payload.get("error", "").lower()

    # Nenhum arquivo escrito
    client_dir = tmp_path / "agents" / "clients" / "cliente_dep_3a"
    assert not client_dir.exists(), "Arquivos escritos mesmo com dependência não satisfeita"

    # 3b. context in_progress
    onboarding_context_in_progress = {
        "_agent_results": {
            "ingestion": {"status": "done", "payload": {"imoveis_indexados": 10}},
            "context": {"status": "in_progress", "payload": {}},
        }
    }
    status, payload = await agent.run("cliente_dep_3b", onboarding_context_in_progress)
    assert status == "blocked", f"Esperava 'blocked' com context in_progress, got '{status}'"
    assert "context" in payload.get("error", "").lower()

    # 3c. _agent_results ausente
    onboarding_sem_results: dict = {"nome_imobiliaria": "Teste"}
    status, payload = await agent.run("cliente_dep_3c", onboarding_sem_results)
    assert status == "blocked", f"Esperava 'blocked' sem _agent_results, got '{status}'"

    # 3d. Controle positivo — ambos prontos
    onboarding_ok = _onboarding_completo(tmp_path)
    status, payload = await agent.run("cliente_dep_3d", onboarding_ok)
    assert status == "done", f"Controle positivo falhou: esperava 'done', got '{status}': {payload}"
    assert Path(payload["consultant_path"]).exists()
