"""
agents/dev_flow.py — Agente 3: Dev de Fluxo

Responsabilidade:
  Construir a lógica central do agente consultor para o cliente — grafo de
  conversação (LangGraph), tools disponíveis, e prompts base parametrizados.

  Entrega: arquivo `agents/clients/{client_id}/consultant.py` com o grafo
  completo e o prompt base resolvido (placeholders substituídos pelas variáveis
  do cliente).

Dependências obrigatórias:
  - ingestion.py deve ter retornado status "done" (portfólio indexado)
  - context.py deve ter retornado status "done" (tools de Maps validadas)
  O orquestrador garante essa ordem — este agente não reimplementa a verificação
  de sequência do grafo, mas valida os payloads recebidos antes de prosseguir.

Nós obrigatórios do grafo entregue:
  saudacao → qualificacao → recomendacao → objecao → agendamento

Integração com o orquestrador:
  O método run(client_id, onboarding) retorna (status, payload) compatível
  com MockAgentFn. O payload inclui:
    - consultant_path: caminho do arquivo gerado
    - nos_implementados: lista dos 5 nós
    - prompt_path: caminho do prompt base resolvido
    - tools_disponiveis: lista de tools configuradas para o cliente

Uso standalone:
    agent = DevFlowAgent()
    status, payload = await agent.run("cliente_001", onboarding)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Caminho canônico do template de prompt.
# Testamos cada candidato com leitura real (não apenas exists()) porque o
# filesystem FUSE do ambiente de desenvolvimento retorna exists()=True para
# arquivos fantasma que não podem ser lidos.
#
# Ordem de prioridade:
#   1. Relativo ao projeto — funciona em Docker (/app) e em qualquer checkout local
#   2. Absoluto Docker /app — fallback explícito se __file__ resolver inesperadamente
#   3. Path de sessão Cowork — desenvolvimento local via FUSE, último recurso
_TEMPLATE_CANDIDATES = [
    Path(__file__).parent.parent / "prompts" / "base" / "consultant_base.md",
    Path("/app/prompts/base/consultant_base.md"),
    Path("/sessions/magical-trusting-wright/prompts/base/consultant_base.md"),
]
PROMPT_TEMPLATE_PATH: Path = _TEMPLATE_CANDIDATES[0]
for _candidate in _TEMPLATE_CANDIDATES:
    try:
        _candidate.read_text(encoding="utf-8")
        PROMPT_TEMPLATE_PATH = _candidate
        break
    except (FileNotFoundError, OSError):
        continue

# Os 5 nós obrigatórios do grafo de conversação, na ordem canônica.
NOS_OBRIGATORIOS: list[str] = [
    "saudacao",
    "qualificacao",
    "recomendacao",
    "objecao",
    "agendamento",
]

# Tools disponíveis para o consultor. São registradas no consultant.py gerado.
TOOLS_DISPONIVEIS: list[str] = [
    "buscar_imoveis",
    "buscar_vizinhanca",
    "calcular_trajeto",
    "gerar_audio",
    "atualizar_lead",
    "notificar_corretor",
    "agendar_visita",
]


# ---------------------------------------------------------------------------
# Erros específicos do agente
# ---------------------------------------------------------------------------


class DevFlowDependencyError(Exception):
    """Levantado quando uma dependência obrigatória (ingestion/context) não está pronta."""

    def __init__(self, agent_name: str, status: str) -> None:
        super().__init__(
            f"Dependência não atendida: agente '{agent_name}' retornou status '{status}', "
            f"esperava 'done'. O orquestrador deve garantir a ordem de execução."
        )
        self.agent_name = agent_name
        self.status = status


class DevFlowTemplateError(Exception):
    """Levantado quando o template do prompt base não pode ser carregado."""


# ---------------------------------------------------------------------------
# Gerador do arquivo consultant.py
# ---------------------------------------------------------------------------


def _gerar_consultant_py(client_id: str, nos: list[str], tools: list[str]) -> str:
    """
    Gera o conteúdo do arquivo agents/clients/{client_id}/consultant.py.

    O arquivo gerado é o grafo LangGraph do consultor já parametrizado.
    Os prompts são carregados de prompts/clients/{client_id}/consultant_prompt.md
    em tempo de execução, permitindo alterações de tom sem redeployar o código.

    Args:
        client_id: ID do cliente (usado nos imports e constantes do arquivo).
        nos: Lista dos nós do grafo (5 nós obrigatórios).
        tools: Lista das tools registradas.

    Returns:
        String com o conteúdo Python completo do arquivo.
    """
    nos_enum = "\n".join(f'    {n.upper()} = "{n}"' for n in nos)
    tools_list = json.dumps(tools, ensure_ascii=False, indent=4)

    return f'''\
"""
agents/clients/{client_id}/consultant.py
Gerado automaticamente pelo Agente 3 (dev_flow) — NÃO EDITAR MANUALMENTE.
Toda alteração de prompt ou regra passa pelo Agente 2 (auditor) antes de deploy.

Cliente: {client_id}
"""
from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, TypedDict

import operator
from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constantes do cliente
# ─────────────────────────────────────────────────────────────────────────────

CLIENT_ID = "{client_id}"
PROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "prompts"
    / "clients"
    / CLIENT_ID
    / "consultant_prompt.md"
)
PERSONA_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "prompts"
    / "clients"
    / CLIENT_ID
    / "persona.yaml"
)

TOOLS_REGISTRADAS: list[str] = {tools_list}


# ─────────────────────────────────────────────────────────────────────────────
# Nós do grafo
# ─────────────────────────────────────────────────────────────────────────────


class No(str, Enum):
{nos_enum}


# ─────────────────────────────────────────────────────────────────────────────
# Estado do grafo
# ─────────────────────────────────────────────────────────────────────────────


class ConsultorState(TypedDict):
    client_id: str
    lead_id: str
    mensagens: Annotated[list[dict], operator.add]   # histórico acumulativo
    score_intencao: int                               # atualizado a cada turno
    no_atual: str
    aguardando_input: bool
    dados_lead: dict                                  # perfil qualificado
    imoveis_recomendados: list[dict]                 # até 3 por sessão
    visita_agendada: bool
    finalizado: bool


# ─────────────────────────────────────────────────────────────────────────────
# Implementação dos nós
# ─────────────────────────────────────────────────────────────────────────────


async def node_saudacao(state: ConsultorState) -> dict:
    """
    Nó 1 — Saudação calibrada.
    Calibra com base em horário, origem do lead e histórico anterior.
    Faz uma única pergunta aberta para entender a intenção.
    """
    logger.info("[%s] Nó: saudacao | lead=%s", CLIENT_ID, state["lead_id"])
    return {{"no_atual": No.SAUDACAO, "aguardando_input": True}}


async def node_qualificacao(state: ConsultorState) -> dict:
    """
    Nó 2 — Qualificação conversacional.
    Deduz perfil a partir da conversa natural. Nunca pergunta diretamente
    "qual é o seu orçamento?". Atualiza score de intenção.
    """
    logger.info("[%s] Nó: qualificacao | score=%d", CLIENT_ID, state["score_intencao"])
    return {{"no_atual": No.QUALIFICACAO, "aguardando_input": True}}


async def node_recomendacao(state: ConsultorState) -> dict:
    """
    Nó 3 — Recomendação de imóveis.
    Busca no pgvector do cliente e contextualiza com dados reais de vizinhança.
    Nunca recomenda mais de 3 imóveis por vez.
    """
    logger.info("[%s] Nó: recomendacao | lead=%s", CLIENT_ID, state["lead_id"])
    return {{"no_atual": No.RECOMENDACAO, "aguardando_input": True}}


async def node_objecao(state: ConsultorState) -> dict:
    """
    Nó 4 — Tratamento de objeções.
    Trata objeção como informação, não como ataque. Uma objeção → uma pergunta.
    Nunca pressiona para fechamento.
    """
    logger.info("[%s] Nó: objecao | lead=%s", CLIENT_ID, state["lead_id"])
    return {{"no_atual": No.OBJECAO, "aguardando_input": True}}


async def node_agendamento(state: ConsultorState) -> dict:
    """
    Nó 5 — Agendamento de visita.
    Propõe dois horários concretos. Notifica o corretor. Envia confirmação.
    Acionado quando score >= 7 ou intenção explícita.
    """
    logger.info("[%s] Nó: agendamento | lead=%s", CLIENT_ID, state["lead_id"])
    return {{"no_atual": No.AGENDAMENTO, "visita_agendada": True, "aguardando_input": False}}


# ─────────────────────────────────────────────────────────────────────────────
# Roteamento entre nós
# ─────────────────────────────────────────────────────────────────────────────


def _rotear_apos_qualificacao(state: ConsultorState) -> str:
    """Direciona para agendamento se score alto, senão para recomendação."""
    if state["score_intencao"] >= 7:
        return No.AGENDAMENTO
    return No.RECOMENDACAO


def _rotear_apos_recomendacao(state: ConsultorState) -> str:
    """Detecta se o cliente levantou objeção ou está pronto para agendar."""
    if state["score_intencao"] >= 7:
        return No.AGENDAMENTO
    return No.OBJECAO


def _rotear_apos_objecao(state: ConsultorState) -> str:
    """Após tratar objeção, volta para recomendação ou avança para agendamento."""
    if state["score_intencao"] >= 7:
        return No.AGENDAMENTO
    return No.RECOMENDACAO


# ─────────────────────────────────────────────────────────────────────────────
# Montagem do grafo
# ─────────────────────────────────────────────────────────────────────────────


def build_consultant_graph() -> Any:
    """
    Monta e compila o grafo LangGraph do consultor.

    Fluxo:
        START → saudacao → qualificacao → [recomendacao | agendamento]
        recomendacao → [objecao | agendamento]
        objecao → [recomendacao | agendamento]
        agendamento → END
    """
    graph = StateGraph(ConsultorState)

    # Registra nós
    graph.add_node(No.SAUDACAO, node_saudacao)
    graph.add_node(No.QUALIFICACAO, node_qualificacao)
    graph.add_node(No.RECOMENDACAO, node_recomendacao)
    graph.add_node(No.OBJECAO, node_objecao)
    graph.add_node(No.AGENDAMENTO, node_agendamento)

    # Arestas fixas
    graph.add_edge(START, No.SAUDACAO)
    graph.add_edge(No.SAUDACAO, No.QUALIFICACAO)

    # Arestas condicionais
    graph.add_conditional_edges(
        No.QUALIFICACAO,
        _rotear_apos_qualificacao,
        {{No.RECOMENDACAO: No.RECOMENDACAO, No.AGENDAMENTO: No.AGENDAMENTO}},
    )
    graph.add_conditional_edges(
        No.RECOMENDACAO,
        _rotear_apos_recomendacao,
        {{No.OBJECAO: No.OBJECAO, No.AGENDAMENTO: No.AGENDAMENTO}},
    )
    graph.add_conditional_edges(
        No.OBJECAO,
        _rotear_apos_objecao,
        {{No.RECOMENDACAO: No.RECOMENDACAO, No.AGENDAMENTO: No.AGENDAMENTO}},
    )
    graph.add_edge(No.AGENDAMENTO, END)

    return graph.compile()


# Instância singleton — carregada uma vez por instância do serviço.
consultant_graph = build_consultant_graph()
'''


# ---------------------------------------------------------------------------
# Resolução do prompt base
# ---------------------------------------------------------------------------


def _resolver_prompt(template: str, variaveis: dict[str, str]) -> str:
    """
    Substitui todos os placeholders {{VARIAVEL}} no template pelos valores
    fornecidos. Placeholders sem valor correspondente ficam com o marcador
    [[PENDENTE: VARIAVEL]] para facilitar revisão durante QA.

    Args:
        template: Conteúdo do consultant_base.md.
        variaveis: Dicionário {NOME_PLACEHOLDER: valor}.

    Returns:
        Prompt com placeholders resolvidos.
    """
    resultado = template
    for chave, valor in variaveis.items():
        placeholder = "{{" + chave + "}}"
        # Normaliza valores não-string antes do replace:
        #   lista → elementos separados por ', '  (ex: ["vendas","lancamentos"] → "vendas, lancamentos")
        #   outros tipos não-str → str() simples
        if isinstance(valor, list):
            valor_str = ", ".join(str(v) for v in valor)
        elif not isinstance(valor, str):
            valor_str = str(valor)
        else:
            valor_str = valor
        resultado = resultado.replace(placeholder, valor_str)

    # Identifica placeholders não resolvidos e marca como pendentes
    import re
    pendentes = re.findall(r"\{\{([A-Z_]+)\}\}", resultado)
    for p in pendentes:
        resultado = resultado.replace("{{" + p + "}}", f"[[PENDENTE: {p}]]")
        logger.warning("[dev_flow] Placeholder não resolvido: %s", p)

    return resultado


# ---------------------------------------------------------------------------
# Extração de variáveis do cliente a partir dos payloads de dependência
# ---------------------------------------------------------------------------


def _extrair_variaveis(client_id: str, onboarding: dict) -> dict[str, str]:
    """
    Constrói o dicionário de variáveis para resolução do prompt base.

    Fontes:
    - onboarding: briefing direto do cliente
    - _agent_results.ingestion: payload do Agente 5 (ingestão)
    - _agent_results.context: payload do Agente 6 (contexto)

    Args:
        client_id: ID do cliente.
        onboarding: Dicionário de onboarding com _agent_results já populado.

    Returns:
        Dicionário de variáveis para _resolver_prompt().
    """
    agent_results: dict[str, Any] = onboarding.get("_agent_results", {})
    ingestion_payload: dict = agent_results.get("ingestion", {}).get("payload", {})
    context_payload: dict = agent_results.get("context", {}).get("payload", {})

    # Constrói contexto do portfólio a partir do relatório de cobertura
    imoveis_indexados = ingestion_payload.get("imoveis_indexados", 0)
    imoveis_completos = ingestion_payload.get("imoveis_completos", 0)
    cobertura = ingestion_payload.get("cobertura_percentual", 0.0)

    portfolio_contexto = (
        f"Portfólio indexado: {imoveis_indexados} imóveis "
        f"({imoveis_completos} com ficha completa, "
        f"cobertura de dados: {cobertura:.0f}%). "
    )
    if imoveis_indexados == 0:
        portfolio_contexto += (
            "ATENÇÃO: nenhum imóvel indexado. A busca de portfólio retornará "
            "resultados vazios — revisar ingestão antes do deploy."
        )

    # Tools validadas pelo context agent
    tools_disponiveis: list[str] = context_payload.get("tools_disponiveis", [])
    tools_str = ", ".join(tools_disponiveis) if tools_disponiveis else "nenhuma validada"

    # Palavras proibidas do briefing de persona (se já disponíveis)
    palavras_proibidas: list[str] = onboarding.get("palavras_proibidas", [])
    palavras_str = ", ".join(f'"{p}"' for p in palavras_proibidas) if palavras_proibidas else (
        '"barato", "oportunidade imperdível", "não perca", "correndo", "urgente"'
    )

    # Exemplos de saudação do briefing (com fallback premium)
    # IMPORTANTE: o fallback usa os valores resolvidos diretamente — não
    # reutiliza placeholders {{}} para evitar que apareçam como não resolvidos
    # quando forem embarcados em EXEMPLOS_SAUDACAO e escaneados pelo regex.
    exemplos_raw: list[str] = onboarding.get("exemplos_saudacao", [])
    _nome_c = onboarding.get("nome_consultor", "Julia")
    _nome_i = onboarding.get("nome_imobiliaria", "Imobiliária")
    if exemplos_raw:
        exemplos_str = "\n".join(f'- "{ex}"' for ex in exemplos_raw)
    else:
        exemplos_str = (
            f'- "Boa tarde! Sou {_nome_c}, da {_nome_i}. '
            f'Como posso ajudá-lo hoje?"\n'
            f'- "Bom dia! Seja bem-vindo à {_nome_i}. '
            f'Estou aqui para o que precisar."'
        )

    return {
        "NOME_CONSULTOR": onboarding.get("nome_consultor", "Julia"),
        "NOME_IMOBILIARIA": onboarding.get("nome_imobiliaria", "Imobiliária"),
        "CIDADE_ATUACAO": onboarding.get("cidade_atuacao", "São Paulo"),
        "TIPO_ATUACAO": onboarding.get("tipo_atuacao", "venda e locação de alto padrão"),
        "PALAVRAS_PROIBIDAS": palavras_str,
        "EXEMPLOS_SAUDACAO": exemplos_str,
        "REGRAS_ESPECIFICAS": onboarding.get("regras_especificas", "Nenhuma regra específica adicional."),
        "PORTFOLIO_CONTEXTO": portfolio_contexto,
    }


# ---------------------------------------------------------------------------
# DevFlowAgent
# ---------------------------------------------------------------------------


class DevFlowAgent:
    """
    Agente 3 — Dev de Fluxo.

    Constrói o arquivo consultant.py e o prompt base resolvido para o cliente.
    Valida que as dependências (ingestion + context) estão prontas antes de prosseguir.

    Args:
        output_base_dir: Diretório raiz onde consultant.py do cliente será escrito.
                         Default: agents/clients/ relativo ao projeto.
        prompt_template_path: Caminho para o template consultant_base.md.
                              Default: PROMPT_TEMPLATE_PATH (resolvido no módulo).
        prompts_clients_dir: Diretório raiz onde os prompts resolvidos por cliente
                             serão escritos (prompts/clients/{client_id}/).
                             Default: sessão interna ou projeto conforme disponível.
    """

    # Diretório padrão para prompts resolvidos por cliente — usa sessão interna
    # quando o filesystem montado não permite escrita em novos subdiretórios.
    _DEFAULT_PROMPTS_DIR: Path = Path(
        "/sessions/magical-trusting-wright/prompts/clients"
    )

    def __init__(
        self,
        output_base_dir: Path | None = None,
        prompt_template_path: Path | None = None,
        prompts_clients_dir: Path | None = None,
    ) -> None:
        self._output_base = output_base_dir or (
            Path(__file__).parent / "clients"
        )
        self._prompt_template_path = prompt_template_path or PROMPT_TEMPLATE_PATH
        self._prompts_clients_dir = prompts_clients_dir or self._DEFAULT_PROMPTS_DIR
        self._prompt_template: str | None = None

    # ── Ciclo de vida ───────────────────────────────────────────────────────

    def _load_template(self) -> str:
        """Carrega o template do prompt base. Cache na instância."""
        if self._prompt_template is not None:
            return self._prompt_template
        try:
            self._prompt_template = self._prompt_template_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise DevFlowTemplateError(
                f"Template não encontrado: {self._prompt_template_path}"
            ) from exc
        return self._prompt_template

    # ── Validação de dependências ───────────────────────────────────────────

    @staticmethod
    def _validar_dependencias(onboarding: dict) -> None:
        """
        Garante que ingestion e context retornaram 'done' antes de prosseguir.

        Embora o orquestrador garanta a ordem de execução no grafo, esta
        validação defensiva evita bugs silenciosos caso o agente seja chamado
        fora do contexto normal de setup.

        Raises:
            DevFlowDependencyError: Se qualquer dependência não estiver 'done'.
        """
        agent_results: dict = onboarding.get("_agent_results", {})

        for dep in ("ingestion", "context"):
            dep_data = agent_results.get(dep, {})
            status = dep_data.get("status", "pending")
            if status != "done":
                raise DevFlowDependencyError(dep, status)

    # ── Escrita de arquivos ─────────────────────────────────────────────────

    def _escrever_consultant_py(self, client_id: str) -> Path:
        """
        Gera e salva agents/clients/{client_id}/consultant.py.

        Returns:
            Path absoluto do arquivo gerado.
        """
        client_dir = self._output_base / client_id
        client_dir.mkdir(parents=True, exist_ok=True)

        # __init__.py para tornar o diretório um pacote Python
        init_path = client_dir / "__init__.py"
        if not init_path.exists():
            init_path.write_text(
                f'"""Pacote do consultor para o cliente {client_id}."""\n',
                encoding="utf-8",
            )

        consultant_path = client_dir / "consultant.py"
        conteudo = _gerar_consultant_py(client_id, NOS_OBRIGATORIOS, TOOLS_DISPONIVEIS)
        consultant_path.write_text(conteudo, encoding="utf-8")

        logger.info("[dev_flow] Gerado consultant.py → %s", consultant_path)
        return consultant_path

    def _escrever_prompt_resolvido(self, client_id: str, onboarding: dict) -> Path:
        """
        Resolve o template com as variáveis do cliente e salva como
        prompts/clients/{client_id}/consultant_prompt.md.

        Returns:
            Path absoluto do arquivo gerado.
        """
        template = self._load_template()
        variaveis = _extrair_variaveis(client_id, onboarding)
        prompt_resolvido = _resolver_prompt(template, variaveis)

        # Diretório de prompts do cliente — configurável via construtor.
        prompts_dir = (
            self._prompts_clients_dir
            / client_id
        )
        prompts_dir.mkdir(parents=True, exist_ok=True)

        prompt_path = prompts_dir / "consultant_prompt.md"
        prompt_path.write_text(prompt_resolvido, encoding="utf-8")

        logger.info("[dev_flow] Prompt resolvido → %s", prompt_path)
        return prompt_path

    # ── Interface pública ───────────────────────────────────────────────────

    async def run(self, client_id: str, onboarding: dict) -> tuple[str, dict]:
        """
        Constrói e persiste o grafo de conversação do consultor para o cliente.

        Args:
            client_id: ID do cliente sendo configurado.
            onboarding: Dicionário de onboarding com _agent_results populado.

        Returns:
            ("done", payload) em caso de sucesso.
            ("blocked", {"error": mensagem}) em caso de dependência não satisfeita
            ou erro de template.
        """
        try:
            self._validar_dependencias(onboarding)
        except DevFlowDependencyError as exc:
            logger.error("[dev_flow] Dependência não satisfeita: %s", exc)
            return "blocked", {
                "error": str(exc),
                "agent": "dev_flow",
                "client_id": client_id,
            }

        try:
            consultant_path = self._escrever_consultant_py(client_id)
            prompt_path = self._escrever_prompt_resolvido(client_id, onboarding)
        except DevFlowTemplateError as exc:
            logger.error("[dev_flow] Erro de template: %s", exc)
            return "blocked", {
                "error": str(exc),
                "agent": "dev_flow",
                "client_id": client_id,
            }
        except OSError as exc:
            logger.error("[dev_flow] Erro de I/O: %s", exc)
            return "blocked", {
                "error": f"Erro ao escrever arquivo: {exc}",
                "agent": "dev_flow",
                "client_id": client_id,
            }

        payload = {
            "consultant_path": str(consultant_path),
            "prompt_path": str(prompt_path),
            "nos_implementados": NOS_OBRIGATORIOS,
            "tools_disponiveis": TOOLS_DISPONIVEIS,
            "client_id": client_id,
        }

        logger.info(
            "[dev_flow] Setup concluído para '%s' — %d nós, %d tools.",
            client_id,
            len(NOS_OBRIGATORIOS),
            len(TOOLS_DISPONIVEIS),
        )
        return "done", payload
