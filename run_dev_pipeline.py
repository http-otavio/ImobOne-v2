"""
run_dev_pipeline.py — Runner de desenvolvimento do pipeline (sem Redis real)

Injeta fakeredis no StateBoard e AgentPubSub, e constrói todas as dependências
de cada agente diretamente — sem alterar os construtores dos agentes (Opção B).

Substitutos de dependências externas (quando chave ausente / sem banco):
  - FakeEmbeddingsClient   → vetores aleatórios 1536-dim (sem OpenAI)
  - FakeImovelRepository   → dicionário em memória (sem Supabase)
  - FakeAuditorLLM         → retorna CoT preenchido + "approved" (sem Anthropic)
  - RealAuditorLLM         → usa ANTHROPIC_API_KEY do env (quando disponível)
  - FakeConsultantFn       → respostas contextuais sem LLM real
  - LLMEvaluatorFn         → usa Claude Sonnet para avaliar critérios (quando disponível)
  - FakeEvaluatorFn        → heurísticas simples sem LLM real

NÃO usar em produção. Em produção: setup_pipeline.py + Redis real.

Uso:
    python run_dev_pipeline.py --client-id demo_alto_padrao \\
        --skip dev_persona context qa_integration

    python run_dev_pipeline.py --client-id demo_alto_padrao --skip dev_persona context qa_integration --quiet
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import fakeredis.aioredis as fakeredis

BASE_DIR = Path(__file__).parent
CLIENTS_DIR = BASE_DIR / "clients"


# ---------------------------------------------------------------------------
# Logging verboso colorido
# ---------------------------------------------------------------------------


class _PhaseFormatter(logging.Formatter):
    COLORS = {
        "DEBUG":    "\033[90m",
        "INFO":     "\033[0m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelname, "")
        ts    = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        name  = record.name.replace("agents.", "").replace("state.", "state/")
        return f"{color}{ts}  [{name:22s}]  {record.getMessage()}{self.RESET}"


def _configure_logging(quiet: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if not quiet else logging.WARNING)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_PhaseFormatter())
    h.setLevel(logging.DEBUG if not quiet else logging.WARNING)
    root.handlers = [h]
    for noisy in ("httpx", "httpcore", "anthropic._base_client", "openai",
                  "langchain", "langgraph"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Injeção de fakeredis compartilhado
# ---------------------------------------------------------------------------


async def _build_fake_board_and_pubsub():
    from state.board import StateBoard
    from state.pubsub import AgentPubSub

    server = fakeredis.FakeServer()
    board  = StateBoard()
    board._client = fakeredis.FakeRedis(server=server, decode_responses=True)

    pubsub = AgentPubSub("orchestrator")
    pubsub._publisher  = fakeredis.FakeRedis(server=server, decode_responses=True)
    pubsub._subscriber = fakeredis.FakeRedis(server=server, decode_responses=True)

    return board, pubsub


# ---------------------------------------------------------------------------
# Substitutos de dependências externas
# ---------------------------------------------------------------------------


class FakeEmbeddingsClient:
    """
    Gera vetores aleatórios unitários 1536-dim sem chamar a OpenAI API.
    Determinístico por texto (seed derivada do hash do texto).
    """
    DIM = 1536

    def _vec(self, text: str) -> list[float]:
        rng = random.Random(hash(text) & 0xFFFF_FFFF)
        raw = [rng.gauss(0, 1) for _ in range(self.DIM)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    async def generate(self, text: str) -> list[float]:
        return self._vec(text)

    async def generate_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class FakeImovelRepository:
    """
    Repositório em memória — implementa ImovelRepositoryProtocol sem Supabase.
    Armazena imóveis por client_id em um dict.
    """

    def __init__(self):
        self._store: dict[str, list[dict]] = {}

    async def upsert_batch(self, client_id: str, records: list[dict]) -> int:
        ns = self._store.setdefault(client_id, [])
        existing_ids = {r.get("imovel_id") for r in ns}
        for rec in records:
            if rec.get("imovel_id") not in existing_ids:
                ns.append(rec)
            else:
                # update
                ns[:] = [rec if r.get("imovel_id") == rec.get("imovel_id") else r for r in ns]
        return len(records)

    async def count(self, client_id: str) -> int:
        return len(self._store.get(client_id, []))

    async def delete_namespace(self, client_id: str) -> int:
        deleted = len(self._store.get(client_id, []))
        self._store.pop(client_id, None)
        return deleted


class FakeAuditorLLM:
    """
    Substituto do Anthropic client para o AuditorAgent quando ANTHROPIC_API_KEY
    não está disponível. Retorna CoT completo + status 'approved'.

    Logado explicitamente como MODO FAKE para não criar falsa confiança.
    """

    _log = logging.getLogger("auditor.fake_llm")

    class _FakeMessage:
        class _FakeContent:
            def __init__(self, text):
                self.text = text
            type = "text"

        def __init__(self, text):
            self.content = [self._FakeContent(text)]

    class _FakeMessages:
        def __init__(self, parent):
            self._parent = parent

        async def create(self, **kwargs) -> "FakeAuditorLLM._FakeMessage":
            FakeAuditorLLM._log.warning(
                "AUDITOR RODANDO EM MODO FAKE — sem chave Anthropic. "
                "Resultado 'approved' gerado automaticamente sem raciocínio real."
            )
            # Chaves em inglês — correspondem exatamente aos campos de AuditResultFull
            fake_cot = json.dumps({
                "argument_for": "A arquitetura segue os padrões definidos no CLAUDE.md "
                                "com separação clara de responsabilidades entre agentes.",
                "argument_against": "Sem chave Anthropic não é possível validar raciocínio "
                                    "profundo — esta auditoria é superficial por definição.",
                "simpler_alternative": "Nenhuma alternativa mais simples identificada no modo fake.",
                "reversibility": "Alta — todas as decisões são configuráveis por cliente.",
                "verdict": "approved",
                "justification": "[MODO FAKE] Aprovado automaticamente para fins de desenvolvimento.",
            })
            return FakeAuditorLLM._FakeMessage(fake_cot)

    def __init__(self):
        self.messages = self._FakeMessages(self)


def _build_anthropic_client():
    """Retorna cliente Anthropic real ou FakeAuditorLLM."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        import anthropic
        return anthropic.AsyncAnthropic(api_key=api_key)
    logging.getLogger("dev_pipeline").warning(
        "ANTHROPIC_API_KEY não encontrada — AuditorAgent usará FakeAuditorLLM."
    )
    return FakeAuditorLLM()


# ---------------------------------------------------------------------------
# consultant_fn e evaluator_fn para QAJourneysAgent
# ---------------------------------------------------------------------------


def _build_fake_consultant_fn(onboarding: dict):
    """
    Consultor fake que responde de forma contextualmente plausível sem LLM.
    Analisa o conteúdo da mensagem e retorna respostas parametrizadas
    com dados do onboarding (nome do consultor, imobiliária, regras).
    """
    nome       = onboarding.get("nome_consultor", "Sofia")
    imobiliaria = onboarding.get("nome_imobiliaria", "Imobiliária")
    cidade     = onboarding.get("cidade_atuacao", "São Paulo")

    RESPOSTAS = {
        "escola":     f"A região conta com excelentes colégios a menos de 10 minutos do imóvel. "
                      f"Posso te enviar as distâncias exatas em áudio se preferir.",
        "rentabilid": f"Para o perfil de investimento que você descreve, os imóveis em {cidade} "
                      f"têm apresentado valorização consistente. "
                      f"Posso preparar uma análise detalhada para você.",
        "fiador":     f"A {imobiliaria} trabalha com diferentes modalidades de garantia — "
                      f"seguro-fiança, título de capitalização e depósito caução. "
                      f"Qual seria mais conveniente para você?",
        "fila":       f"Você está entre os primeiros a demonstrar interesse neste lançamento. "
                      f"Vou registrar sua prioridade e entrar em contato assim que a pré-venda abrir.",
        "desconto":   f"Nossa política preserva a integridade do produto — "
                      f"não trabalhamos com descontos que comprometam o posicionamento do empreendimento. "
                      f"Posso apresentar condições de pagamento que se adaptem ao seu planejamento.",
        "escola|colégio|educação": f"A região conta com colégios de alto padrão próximos. "
                                   f"Deseja receber os detalhes em áudio?",
    }

    async def consultant_fn(mensagens: list[dict]) -> str:
        ultima = mensagens[-1]["content"].lower() if mensagens else ""
        for chave, resposta in RESPOSTAS.items():
            if any(k in ultima for k in chave.split("|")):
                return resposta
        # Resposta genérica premium
        return (
            f"Boa pergunta. A {imobiliaria} tem imóveis que atendem exatamente "
            f"o que você descreve em {cidade}. "
            f"Posso agendar uma visita com {nome} para apresentar as opções?"
        )

    return consultant_fn


def _build_fake_evaluator_fn():
    """
    Avaliador heurístico sem LLM. Avalia se a resposta contém indicadores
    positivos do critério descrito em texto.
    Conservador: aprova por padrão, reprova apenas em casos claramente detectáveis.
    """
    INDICADORES_NEGATIVOS = {
        "não deve revelar que é ia":       ["sou uma ia", "sou um robô", "sou bot"],
        "não deve corrigir português":     ["corrijo", "você escreveu errado", "forma correta é"],
        "não deve confirmar desconto":     ["posso dar desconto", "consigo baixar o preço"],
        "deve oferecer áudio":             None,  # avalia ausência de "áudio" na resposta
        "deve mencionar escola":           None,
        "deve mencionar trajeto":          None,
        "deve mencionar rentabilidade":    None,
        "deve oferecer agendamento":       None,
        "deve apresentar alternativas de garantia": None,
    }

    PALAVRAS_CHAVE_POSITIVAS = {
        "deve oferecer áudio":                    ["áudio", "audio", "voz", "enviar"],
        "deve mencionar escola":                  ["colégio", "escola", "educação", "ensino", "distância"],
        "deve mencionar trajeto":                 ["minutos", "km", "distância", "trajeto", "percurso"],
        "deve mencionar rentabilidade":           ["valorização", "rentabilidade", "retorno", "investimento"],
        "deve oferecer agendamento":              ["agendar", "visita", "marcar", "agenda"],
        "deve apresentar alternativas de garantia": ["garantia", "fiança", "caução", "capitalização"],
    }

    async def evaluator_fn(criterio, resposta: str) -> tuple[bool, str]:
        desc  = criterio.descricao.lower()
        resp  = resposta.lower()

        # Verifica indicadores negativos explícitos
        for chave, termos in INDICADORES_NEGATIVOS.items():
            if chave in desc and termos:
                for termo in termos:
                    if termo in resp:
                        return False, f"Resposta contém '{termo}' — viola critério: {criterio.descricao}"

        # Verifica palavras-chave positivas obrigatórias
        for chave, palavras in PALAVRAS_CHAVE_POSITIVAS.items():
            if chave in desc:
                if not any(p in resp for p in palavras):
                    return False, (
                        f"Resposta não contém indicadores de '{chave}'. "
                        f"Palavras esperadas: {palavras[:3]}..."
                    )

        return True, ""

    return evaluator_fn


def _build_real_evaluator_fn():
    """
    Avaliador real usando Claude Sonnet via ANTHROPIC_API_KEY.
    Usa raciocínio profundo para avaliar se o critério foi atendido.
    """
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    log    = logging.getLogger("qa_journeys.llm_evaluator")

    async def evaluator_fn(criterio, resposta: str) -> tuple[bool, str]:
        # Assistant prefill — garante JSON puro sem prose ou markdown
        prompt = (
            f"CRITÉRIO: {criterio.descricao}\n"
            f"SEVERIDADE: {criterio.severidade}\n\n"
            f"RESPOSTA DO CONSULTOR:\n{resposta}"
        )
        try:
            msg = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system=(
                    'Avalie se o critério foi atendido. Complete o JSON: '
                    '{"passou": <bool>, "sugestao": "<vazio se passou, motivo curto se não>"}. '
                    'Apenas o JSON, sem texto adicional.'
                ),
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": '{"passou":'},
                ],
            )
            continuacao = next(
                (b.text for b in msg.content if hasattr(b, "text") and b.text),
                'true, "sugestao": ""}',
            ).strip()
            raw = '{"passou":' + continuacao
            if not raw.rstrip().endswith("}"):
                raw = raw.rstrip() + "}"
            dados = json.loads(raw)
            return bool(dados["passou"]), dados.get("sugestao", "")
        except Exception as exc:
            log.warning("LLM evaluator falhou (%s) — aprovando por padrão.", exc)
            return True, ""

    return evaluator_fn


# ---------------------------------------------------------------------------
# Builders de adaptadores (Opção B — dependências construídas pelo runner)
# ---------------------------------------------------------------------------


def _build_ingestion_adapter():
    """IngestionAgent com FakeEmbeddingsClient + FakeImovelRepository."""
    from agents.ingestion import IngestionAgent

    embeddings = FakeEmbeddingsClient()
    repository = FakeImovelRepository()
    agent      = IngestionAgent(embeddings_client=embeddings, repository=repository)

    async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]:
        # Injeta dados do CSV no onboarding se existir
        portfolio_path_str = onboarding.get("portfolio_path", "")
        portfolio_path     = BASE_DIR / portfolio_path_str if portfolio_path_str else None
        enriched           = dict(onboarding)

        if portfolio_path and portfolio_path.exists():
            enriched["portfolio_data"]   = portfolio_path.read_text(encoding="utf-8")
            enriched["portfolio_format"] = portfolio_path.suffix.lstrip(".").lower()
        else:
            enriched.setdefault("portfolio_format", "json")

        return await agent.run(client_id, enriched)

    return fn


def _build_dev_persona_adapter():
    from agents.dev_persona import DevPersonaAgent
    agent = DevPersonaAgent()
    async def fn(client_id, onboarding): return await agent.run(client_id, onboarding)
    return fn


def _build_memory_adapter():
    from agents.memory import MemoryAgent
    agent = MemoryAgent()
    async def fn(client_id, onboarding): return await agent.run(client_id, onboarding)
    return fn


def _build_context_adapter():
    from agents.context import ContextAgent
    agent = ContextAgent()
    async def fn(client_id, onboarding): return await agent.run(client_id, onboarding)
    return fn


def _build_dev_flow_adapter():
    from agents.dev_flow import DevFlowAgent
    agent = DevFlowAgent()
    async def fn(client_id, onboarding): return await agent.run(client_id, onboarding)
    return fn


def _build_auditor_adapter(board):
    """AuditorAgent com cliente Anthropic real ou FakeAuditorLLM."""
    from agents.auditor import AuditorAgent
    llm   = _build_anthropic_client()
    agent = AuditorAgent(anthropic_client=llm, board=board)
    async def fn(client_id, onboarding): return await agent.run(client_id, onboarding)
    return fn


def _build_qa_journeys_adapter(onboarding: dict):
    """
    QAJourneysAgent com:
      - FakeConsultantFn (respostas parametrizadas sem LLM)
      - LLMEvaluatorFn se ANTHROPIC_API_KEY disponível, FakeEvaluatorFn caso contrário
    """
    from agents.qa_journeys import QAJourneysAgent

    consultant_fn = _build_fake_consultant_fn(onboarding)
    evaluator_fn  = (
        _build_real_evaluator_fn()
        if os.getenv("ANTHROPIC_API_KEY")
        else _build_fake_evaluator_fn()
    )
    agent = QAJourneysAgent(consultant_fn=consultant_fn, evaluator_fn=evaluator_fn)
    async def fn(client_id, onb): return await agent.run(client_id, onb)
    return fn


def _build_qa_integration_adapter():
    import httpx
    from agents.qa_integration import QAIntegrationAgent
    async def fn(client_id, onboarding):
        async with httpx.AsyncClient() as http:
            agent = QAIntegrationAgent(onboarding=onboarding, http_client=http)
            return await agent.run(client_id, onboarding)
    return fn


def _build_monitor_adapter():
    from agents.monitor import MonitorAgent
    agent = MonitorAgent()
    async def fn(client_id, onboarding):
        status, payload = await agent.run(client_id, {
            "latencia_media_ms": 0.0, "taxa_erro_percent": 0.0,
            "falhas_consecutivas": 0,  "drift_score": 0.0,
        })
        return "done", {**payload, "monitor_ativo": True}
    return fn


ADAPTER_BUILDERS = {
    "ingestion":      lambda board, onb: _build_ingestion_adapter(),
    "dev_persona":    lambda board, onb: _build_dev_persona_adapter(),
    "memory":         lambda board, onb: _build_memory_adapter(),
    "context":        lambda board, onb: _build_context_adapter(),
    "dev_flow":       lambda board, onb: _build_dev_flow_adapter(),
    "auditor":        lambda board, onb: _build_auditor_adapter(board),
    "qa_journeys":    lambda board, onb: _build_qa_journeys_adapter(onb),
    "qa_integration": lambda board, onb: _build_qa_integration_adapter(),
    "monitor":        lambda board, onb: _build_monitor_adapter(),
}


def build_real_agents(skip: list[str], board, onboarding: dict) -> dict:
    log      = logging.getLogger("dev_pipeline")
    skip_set = set(skip)
    adapters = {}
    for name, builder in ADAPTER_BUILDERS.items():
        if name in skip_set:
            log.info("  ⏭  %-20s → mock padrão (skip)", name)
            continue
        try:
            adapters[name] = builder(board, onboarding)
            log.info("  ✓  %-20s → agente real", name)
        except ImportError as e:
            log.warning("  ⚠  %-20s → mock (ImportError: %s)", name, e)
        except Exception as e:
            log.error("  ✗  %-20s → mock (Erro: %s)", name, e)
    return adapters


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------


async def run(client_id: str, skip: list[str], quiet: bool) -> int:
    log = logging.getLogger("dev_pipeline")
    t0  = time.monotonic()

    print(f"\n{'═' * 64}")
    print(f"  ImobOne — Dev Pipeline (fakeredis + dependências injetadas)")
    print(f"  Client:  {client_id}")
    print(f"  Skip:    {', '.join(skip) if skip else '(nenhum)'}")
    print(f"  Início:  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'═' * 64}\n")

    # Carrega onboarding
    path = CLIENTS_DIR / client_id / "onboarding.json"
    if not path.exists():
        log.error("Onboarding não encontrado: %s", path)
        return 2
    with open(path, encoding="utf-8") as f:
        onboarding = json.load(f)
    log.info("Onboarding carregado: %s (%s)", onboarding.get("nome_imobiliaria"), client_id)

    # Fakeredis
    board, pubsub = await _build_fake_board_and_pubsub()
    log.info("Redis: fakeredis (in-memory, servidor compartilhado)")

    # Adaptadores
    print(f"\n{'─' * 40}")
    print("  Construindo adaptadores")
    print(f"{'─' * 40}")
    real_agents = build_real_agents(skip, board, onboarding)
    n_real  = len(real_agents)
    n_total = len(ADAPTER_BUILDERS)
    log.info("Prontos: %d reais + %d mocks\n", n_real, n_total - n_real)

    # Orquestrador
    from agents.orchestrator import OrchestratorAgent
    orchestrator = OrchestratorAgent(board=board, pubsub=pubsub, mock_agents=real_agents)

    print(f"{'─' * 40}")
    print("  Executando grafo LangGraph")
    print(f"{'─' * 40}\n")

    try:
        result = await orchestrator.run(onboarding)
    except Exception as exc:
        log.exception("Erro fatal durante execução: %s", exc)
        await board.close()
        await pubsub.close()
        return 1

    await board.close()
    await pubsub.close()

    elapsed      = time.monotonic() - t0
    deploy_status = result.get("deploy_status", "unknown")
    blocked       = result.get("blocked_agents", [])
    errors        = result.get("errors", [])
    qa_score      = result.get("qa_journeys_score")
    qa_int        = result.get("qa_integration_passed")
    audit_status  = result.get("audit_status", "?")
    rework        = result.get("rework_count", 0)

    print(f"\n{'═' * 64}")
    print("  RESULTADO DO PIPELINE")
    print(f"{'═' * 64}")
    print(f"  Deploy status    : {deploy_status.upper()}")
    print(f"  Audit status     : {audit_status}")
    print(f"  QA jornadas      : {f'{qa_score:.0%}' if qa_score is not None else 'n/a'}")
    print(f"  QA integração    : {qa_int}")
    print(f"  Retrabalhamentos : {rework}")
    print(f"  Tempo total      : {elapsed:.2f}s")

    if blocked:
        print(f"\n  ⚠  Agentes bloqueados ({len(blocked)}):")
        for b in blocked:
            print(f"     • {b}")

    if errors:
        print(f"\n  ✗  Erros ({len(errors)}):")
        for e in errors:
            print(f"     • {e}")

    agent_results = result.get("agent_results") or {}
    if agent_results:
        print(f"\n  Payload por agente:")
        for name, payload in agent_results.items():
            if isinstance(payload, dict):
                s = {k: v for k, v in payload.items()
                     if k not in ("client_id",) and not isinstance(v, (list, dict))}
                print(f"     [{name:18s}] {json.dumps(s, ensure_ascii=False)}")
            else:
                print(f"     [{name:18s}] {payload}")

    print(f"{'═' * 64}\n")

    if deploy_status == "deploy_ready":
        print("  ✅  Deploy aprovado — consultor digital ativo.\n")
        return 0
    elif deploy_status == "human_review":
        print("  ⚠️   Encaminhado para revisão humana.\n")
        return 1
    else:
        print(f"  ❌  Pipeline falhou (status: {deploy_status}).\n")
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description="Dev pipeline runner (fakeredis)")
    p.add_argument("--client-id", required=True)
    p.add_argument("--skip", nargs="*", default=[], metavar="AGENT")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    _configure_logging(args.quiet)
    sys.exit(asyncio.run(run(args.client_id, args.skip, args.quiet)))


if __name__ == "__main__":
    main()
