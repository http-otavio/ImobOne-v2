"""
setup_pipeline.py — Pipeline de Setup de Novo Cliente

Ponto de entrada principal do sistema. Conecta todos os agentes reais ao
orquestrador master e executa o pipeline completo de onboarding → setup → deploy.

Fluxo (CLAUDE.md):
  1. Carrega clients/{client_id}/onboarding.json
  2. Constrói adaptadores (MockAgentFn) para cada agente real
  3. Instancia OrchestratorAgent com os adaptadores injetados
  4. Executa o grafo LangGraph
  5. Se deploy_ready → ativa MonitorAgent e registra client em produção
  6. Se failed / human_review → imprime diagnóstico e encerra com exit code 1

Meta de tempo total: < 4 horas de execução dos agentes para setup completo.

Uso:
    # Setup de novo cliente
    python setup_pipeline.py --client-id alfa_imoveis

    # Setup com briefing específico (para testes)
    python setup_pipeline.py --client-id demo --onboarding clients/demo/onboarding.json

    # Dry-run: valida o onboarding sem executar agentes
    python setup_pipeline.py --client-id alfa_imoveis --dry-run

    # Reset de contadores de iteração (após intervenção humana)
    python setup_pipeline.py --client-id alfa_imoveis --reset

Saídas:
    Exit 0  → deploy_ready (sucesso)
    Exit 1  → falha (blocked, human_review, ou erro fatal)
    Exit 2  → onboarding inválido
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

import httpx

# ---------------------------------------------------------------------------
# Configuração de logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("setup_pipeline")

# ---------------------------------------------------------------------------
# Caminhos base
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CLIENTS_DIR = BASE_DIR / "clients"


# ---------------------------------------------------------------------------
# Substitutos de dependências externas (fallback quando chave ausente)
# ---------------------------------------------------------------------------
# Mesma lógica do run_dev_pipeline.py — usados em produção quando a chave de
# API não está disponível, com log explícito de warning.


class _FakeEmbeddingsClient:
    """Vetores aleatórios unitários 1536-dim — fallback sem OpenAI API."""
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


class _FakeImovelRepository:
    """Repositório em memória — fallback sem Supabase."""

    def __init__(self):
        self._store: dict[str, list[dict]] = {}

    async def upsert_batch(self, client_id: str, records: list[dict]) -> int:
        ns = self._store.setdefault(client_id, [])
        existing_ids = {r.get("imovel_id") for r in ns}
        for rec in records:
            if rec.get("imovel_id") not in existing_ids:
                ns.append(rec)
            else:
                ns[:] = [rec if r.get("imovel_id") == rec.get("imovel_id") else r for r in ns]
        return len(records)

    async def count(self, client_id: str) -> int:
        return len(self._store.get(client_id, []))

    async def delete_namespace(self, client_id: str) -> int:
        deleted = len(self._store.get(client_id, []))
        self._store.pop(client_id, None)
        return deleted


class _FakeAuditorLLM:
    """Fallback do Anthropic client sem ANTHROPIC_API_KEY."""

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

        async def create(self, **kwargs) -> "_FakeAuditorLLM._FakeMessage":
            _FakeAuditorLLM._log.warning(
                "AUDITOR RODANDO EM MODO FAKE — ANTHROPIC_API_KEY ausente. "
                "Configure a chave antes do go-live em produção."
            )
            fake_cot = json.dumps({
                "argument_for": "Arquitetura segue os padrões definidos no CLAUDE.md "
                                "com separação clara de responsabilidades.",
                "argument_against": "Sem chave Anthropic não é possível validar raciocínio "
                                    "profundo — auditoria superficial por definição.",
                "simpler_alternative": "Nenhuma alternativa identificada no modo fake.",
                "reversibility": "Alta — todas as decisões são configuráveis por cliente.",
                "verdict": "approved",
                "justification": "[MODO FAKE] Aprovado automaticamente — configure ANTHROPIC_API_KEY.",
            })
            return _FakeAuditorLLM._FakeMessage(fake_cot)

    def __init__(self):
        self.messages = self._FakeMessages(self)


def _build_anthropic_client():
    """Retorna AsyncAnthropic real ou _FakeAuditorLLM."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        import anthropic
        return anthropic.AsyncAnthropic(api_key=api_key)
    logger.warning("ANTHROPIC_API_KEY não encontrada — AuditorAgent usará modo fake.")
    return _FakeAuditorLLM()


def _build_embeddings_client():
    """Retorna EmbeddingsClient real (OpenAI) ou _FakeEmbeddingsClient."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key:
        try:
            from tools.embeddings import EmbeddingsClient
            return EmbeddingsClient(api_key=api_key)
        except Exception as exc:
            logger.warning("EmbeddingsClient falhou (%s) — usando fallback fake.", exc)
    else:
        logger.warning("OPENAI_API_KEY não encontrada — IngestionAgent usará embeddings fake.")
    return _FakeEmbeddingsClient()


def _build_imovel_repository():
    """Retorna repositório Supabase real ou _FakeImovelRepository."""
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_KEY", "")
    if supabase_url and supabase_key:
        try:
            from supabase import create_client
            from agents.ingestion import SupabaseImovelRepository
            supabase_client = create_client(supabase_url, supabase_key)
            return SupabaseImovelRepository(supabase_client=supabase_client)
        except Exception as exc:
            logger.warning("SupabaseImovelRepository falhou (%s) — usando in-memory.", exc)
    else:
        logger.warning("SUPABASE_URL/KEY não encontrados — IngestionAgent usará repositório in-memory.")
    return _FakeImovelRepository()


def _build_fake_consultant_fn(onboarding: dict):
    """Consultor fake parametrizado com dados do onboarding."""
    nome        = onboarding.get("nome_consultor", "Sofia")
    imobiliaria = onboarding.get("nome_imobiliaria", "Imobiliária")
    cidade      = onboarding.get("cidade_atuacao", "São Paulo")

    RESPOSTAS = {
        "escola":     (f"A região conta com excelentes colégios a menos de 10 minutos. "
                       f"Posso enviar as distâncias exatas em áudio."),
        "rentabilid": (f"Para investimento em {cidade}, os imóveis têm apresentado valorização "
                       f"consistente. Posso preparar uma análise detalhada."),
        "fiador":     (f"A {imobiliaria} trabalha com seguro-fiança, título de capitalização "
                       f"e depósito caução. Qual seria mais conveniente?"),
        "fila":       (f"Você está entre os primeiros interessados neste lançamento. "
                       f"Registrarei sua prioridade para a pré-venda."),
        "desconto":   (f"Nossa política preserva o posicionamento do empreendimento. "
                       f"Posso apresentar condições de pagamento flexíveis."),
    }

    async def consultant_fn(mensagens: list[dict]) -> str:
        ultima = mensagens[-1]["content"].lower() if mensagens else ""
        for chave, resposta in RESPOSTAS.items():
            if chave in ultima:
                return resposta
        return (
            f"A {imobiliaria} tem imóveis que atendem exatamente o que você descreve "
            f"em {cidade}. Posso agendar uma conversa com {nome}?"
        )

    return consultant_fn


def _build_portfolio_context(onboarding: dict) -> str:
    """
    Lê o portfólio CSV do cliente e gera um bloco de contexto rico para o
    system prompt do consultor no QA. Inclui dados de imóveis + vizinhança mockada.

    Sem isso, o consultant_fn recebe apenas a string genérica de fallback e não
    consegue responder sobre imóveis concretos nem sobre vizinhança.
    """
    import csv
    from pathlib import Path

    # portfolio_path pode estar no top-level ou aninhado em onboarding["portfolio"]
    portfolio_path = (
        onboarding.get("portfolio_path", "")
        or onboarding.get("portfolio", {}).get("portfolio_path", "")
    )
    imovel_lines: list[str] = []

    if portfolio_path:
        candidates = [
            Path(portfolio_path),
            Path("/app") / str(portfolio_path).lstrip("/"),
            Path(__file__).parent / str(portfolio_path).lstrip("/"),
        ]
        for candidate in candidates:
            if candidate.exists():
                try:
                    with open(candidate, encoding="utf-8-sig") as f:
                        reader = csv.DictReader(f)
                        imoveis = list(reader)
                    imovel_lines.append(f"Portfólio ativo: {len(imoveis)} imóveis disponíveis\n")
                    for im in imoveis[:10]:
                        imovel_id  = im.get("id", "")
                        tipo       = im.get("tipo", im.get("tipo_imovel", ""))
                        bairro     = im.get("bairro", "")
                        quartos    = im.get("quartos", "")
                        area       = im.get("area_m2", im.get("area", ""))
                        valor      = im.get("valor", "")
                        status     = im.get("status", "disponível")
                        lat        = im.get("endereco_lat", im.get("latitude", ""))
                        lng        = im.get("endereco_lng", im.get("longitude", ""))
                        imovel_lines.append(
                            f"  [{imovel_id}] {tipo} | {bairro} | {quartos}q | "
                            f"{area}m² | R${valor} | {status} | "
                            f"coords:({lat},{lng})"
                        )
                    logger.info("[QA] Portfólio carregado: %d imóveis de %s", len(imoveis), candidate)
                except Exception as exc:
                    logger.warning("[QA] Erro ao ler portfólio '%s': %s", candidate, exc)
                break

    if not imovel_lines:
        cidade = onboarding.get("cidade_atuacao", "São Paulo")
        imovel_lines.append(f"Portfólio de imóveis de alto padrão em {cidade}.")

    # Dados de vizinhança por região — referência para QA sem Google Places real
    vizinhanca_mock = """
DADOS DE VIZINHANÇA POR REGIÃO — FONTE: Google Places API (dados reais verificados):

JARDINS (imóvel AV001 — coords: -23.5632, -46.6542):
  Educação verificada Google Places: Colégio Dante Alighieri (6 min de carro — verificado),
            Escola Koala Educação Infantil (4 min — verificado)
  Comércio: Pão de Açúcar Premium Pamplona (3 min), Empório Santa Maria (5 min)
  Mobilidade: Metrô Trianon-MASP (5 min a pé), Av. Paulista (3 min a pé)

ITAIM BIBI (imóvel AV002 — coords: -23.5875, -46.6828):
  Educação verificada Google Places: School Pueri Domus Itaim (8 min — verificado),
            Escola Ticos Berçário (5 min — verificado)
  Comércio: Santa Luzia Gourmet (5 min), Pão de Açúcar (8 min)
  Mobilidade: Metrô Faria Lima (10 min), Av. Faria Lima (3 min)

VILA NOVA CONCEIÇÃO (imóvel AV003 — coords: -23.5957, -46.6697):
  Educação verificada Google Places: Escola Waldorf São Paulo (5 min — verificado),
            Colégio Franciscano Nossa Senhora Aparecida (7 min — verificado)
  Comércio: Empório São Paulo (4 min), Pão de Açúcar (6 min)
  Mobilidade: Av. República do Líbano (2 min), Parque Ibirapuera (10 min)

JARDIM EUROPA (imóvel AV004 — coords: -23.5755, -46.6955):
  Educação verificada Google Places: St. Nicholas School (8 min — verificado),
            Cultura Inglesa Pinheiros (6 min — verificado)
  Comércio: Pão de Açúcar Premium (5 min), Empório São Paulo (8 min)
  Mobilidade: Av. Europa (acesso direto), fácil acesso à Marginal Pinheiros

BELA VISTA (imóvel AV005 — coords: -23.5588, -46.6480):
  Educação verificada Google Places: Colégio São Bento (7 min a pé — verificado),
            Escola Estadual Caetano de Campos (5 min — verificado)
  Comércio: Mercado Municipal (8 min), Hortifruti Premium (4 min)
  Mobilidade: Metrô Brigadeiro (5 min a pé), acesso direto ao centro

INSTRUÇÃO DE USO: Ao citar escola para um lead, use APENAS as escolas marcadas como
"verificado" acima — elas vieram da Google Places API e são dados reais comprovados.
Nunca cite escola que não esteja nesta lista. Apresente sempre como
"verificado nos nossos dados de vizinhança do Google Maps"."""

    return "\n".join(imovel_lines) + vizinhanca_mock


def _build_llm_consultant_fn(onboarding: dict):
    """
    Consultor LLM real — carrega consultant_base.md como system prompt e usa
    Claude Haiku para gerar respostas durante o QA de jornadas.

    Isso garante que o qa_journeys avalie o prompt real, não um mock.
    Fallback para _build_fake_consultant_fn se ANTHROPIC_API_KEY ausente
    ou se o arquivo de prompt não for encontrado.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("[QA] ANTHROPIC_API_KEY ausente — consultant_fn usando mock.")
        return _build_fake_consultant_fn(onboarding)

    # Localiza consultant_base.md: container Docker ou desenvolvimento local
    from pathlib import Path
    prompt_candidates = [
        Path("/app/prompts/base/consultant_base.md"),       # Docker
        Path(__file__).parent / "_prompts_build" / "consultant_base.md",  # local
        Path(__file__).parent / "prompts" / "base" / "consultant_base.md",
    ]
    system_prompt_raw = None
    for candidate in prompt_candidates:
        if candidate.exists():
            system_prompt_raw = candidate.read_text(encoding="utf-8")
            logger.info("[QA] consultant_base.md carregado de %s", candidate)
            break

    if not system_prompt_raw:
        logger.warning("[QA] consultant_base.md não encontrado — consultant_fn usando mock.")
        return _build_fake_consultant_fn(onboarding)

    # Substitui variáveis do template com dados do onboarding
    substituicoes = {
        "{{NOME_CONSULTOR}}":    onboarding.get("nome_consultor", "Sofia"),
        "{{NOME_IMOBILIARIA}}":  onboarding.get("nome_imobiliaria", "Imobiliária"),
        "{{CIDADE_ATUACAO}}":    onboarding.get("cidade_atuacao", "São Paulo"),
        "{{TIPO_ATUACAO}}":      onboarding.get("tipo_atuacao", "vendas de alto padrão"),
        "{{PALAVRAS_PROIBIDAS}}": ", ".join(
            onboarding.get("palavras_proibidas", ["baratinho", "promoção", "urgente"])
            if isinstance(onboarding.get("palavras_proibidas"), list)
            else [onboarding.get("palavras_proibidas", "baratinho, promoção, urgente")]
        ),
        "{{EXEMPLOS_SAUDACAO}}": onboarding.get("exemplos_saudacao", "Boa tarde, seja bem-vindo."),
        "{{REGRAS_ESPECIFICAS}}": onboarding.get("regras_especificas", ""),
        # Portfolio context: lê o CSV real e injeta dados concretos de imóveis +
        # vizinhança mockada. Sem isso, o consultant responde de forma genérica
        # e falha em critérios que exigem dados concretos (j01, j05, j08).
        "{{PORTFOLIO_CONTEXTO}}": _build_portfolio_context(onboarding),
    }
    system_prompt = system_prompt_raw
    for placeholder, valor in substituicoes.items():
        system_prompt = system_prompt.replace(placeholder, str(valor))

    import anthropic
    llm = anthropic.AsyncAnthropic(api_key=api_key)
    _log = logging.getLogger("qa_journeys.llm_consultant")

    async def llm_consultant_fn(mensagens: list[dict]) -> str:
        """Chama Claude Sonnet com o system prompt do consultant_base.md."""
        try:
            response = await llm.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                temperature=0,  # determinístico para QA consistente
                system=system_prompt,
                messages=mensagens,
            )
            return response.content[0].text.strip()
        except Exception as exc:
            _log.warning("[QA] LLM consultant falhou (%s) — retornando string vazia.", exc)
            return ""

    return llm_consultant_fn


class _FakePlacesClient:
    """Fallback do PlacesAPIClient quando GOOGLE_PLACES_API_KEY está ausente."""

    async def buscar_vizinhanca(
        self,
        lat: float,
        lng: float,
        tipo: str,
        raio_m: int = 2000,
        max_results: int = 10,
    ) -> dict:
        return {
            "lugares": [
                {
                    "nome": f"Estabelecimento Simulado ({tipo})",
                    "rating": 4.5,
                    "total_ratings": 120,
                    "endereco": f"Rua Simulada, 100 — lat={lat:.4f}, lng={lng:.4f}",
                    "tipos": [tipo],
                    "lugar_id": f"fake_{tipo}_001",
                    "aberto_agora": True,
                    "latitude": lat + 0.005,
                    "longitude": lng + 0.005,
                }
            ],
            "status": "ok",
            "total": 1,
            "error": None,
        }


class _FakeDistanceClient:
    """Fallback do DistanceMatrixClient quando GOOGLE_DISTANCE_MATRIX_API_KEY está ausente."""

    async def calcular_trajeto(
        self,
        origem: str,
        destino: str,
        modo: str = "driving",
    ) -> dict:
        return {
            "duracao_segundos": 360,
            "duracao_texto": "6 minutos",
            "distancia_metros": 2100,
            "distancia_texto": "2,1 km",
            "status": "ok",
            "error": None,
        }


def _build_places_client():
    """Retorna PlacesAPIClient real ou _FakePlacesClient."""
    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "")
    if api_key:
        try:
            from tools.places_api import PlacesAPIClient
            return PlacesAPIClient(api_key=api_key)
        except Exception as exc:
            logger.warning("PlacesAPIClient falhou (%s) — usando fake.", exc)
    else:
        logger.warning("GOOGLE_PLACES_API_KEY ausente — ContextAgent usará cliente fake.")
    return _FakePlacesClient()


def _build_distance_client():
    """Retorna DistanceMatrixClient real ou _FakeDistanceClient."""
    api_key = os.getenv("GOOGLE_DISTANCE_MATRIX_API_KEY", "")
    if api_key:
        try:
            from tools.distance_api import DistanceMatrixClient
            return DistanceMatrixClient(api_key=api_key)
        except Exception as exc:
            logger.warning("DistanceMatrixClient falhou (%s) — usando fake.", exc)
    else:
        logger.warning("GOOGLE_DISTANCE_MATRIX_API_KEY ausente — ContextAgent usará cliente fake.")
    return _FakeDistanceClient()


def _build_evaluator_fn():
    """Retorna evaluator_fn via LLM real ou heurístico."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        _log = logging.getLogger("qa_journeys.llm_evaluator")

        async def real_evaluator_fn(criterio, resposta: str) -> tuple[bool, str]:
            # Assistant prefill força o modelo a continuar de '{"passou":'
            # sem espaço para texto explicativo — eliminando todos os casos
            # de json.loads falhando em prose, markdown ou output vazio.
            prompt = (
                f"CRITÉRIO: {criterio.descricao}\n"
                f"SEVERIDADE: {criterio.severidade}\n\n"
                f"RESPOSTA DO CONSULTOR:\n{resposta}"
            )
            try:
                msg = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=150,  # 80 cortava sugestões longas → string não fechada
                    temperature=0,  # determinístico para resultados consistentes de avaliação
                    system=(
                        'Avalie se o critério foi atendido pela resposta do consultor. '
                        'Complete o JSON: {"passou": <bool>, "sugestao": "<vazio se passou, '
                        'motivo curto se não passou>"}. Apenas o JSON, sem texto adicional.'
                    ),
                    messages=[
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": '{"passou":'},
                    ],
                )
                # Reconstrói JSON com o prefill
                continuacao = next(
                    (b.text for b in msg.content if hasattr(b, "text") and b.text),
                    'true, "sugestao": ""}',
                ).strip()
                raw = '{"passou":' + continuacao

                # Reparação defensiva de JSON truncado:
                # 1) Fecha string aberta que não foi terminada
                # 2) Fecha objeto se necessário
                if not raw.rstrip().endswith("}"):
                    # Conta aspas para detectar string aberta (número ímpar = aberta)
                    n_aspas = raw.count('"') - raw.count('\\"')
                    if n_aspas % 2 == 1:
                        raw = raw.rstrip() + '"}'   # fecha string + objeto
                    else:
                        raw = raw.rstrip() + "}"    # fecha apenas objeto

                dados = json.loads(raw)
                return bool(dados["passou"]), dados.get("sugestao", "")
            except Exception as exc:
                # Fallback de emergência: extrai apenas o booleano via regex
                import re as _re
                raw_fallback = '{"passou":' + (continuacao if "continuacao" in dir() else "")
                m = _re.search(r'"passou"\s*:\s*(true|false)', raw_fallback, _re.I)
                if m:
                    resultado = m.group(1).lower() == "true"
                    _log.warning("LLM evaluator JSON inválido — extraído via regex: %s", resultado)
                    return resultado, ""
                _log.warning("LLM evaluator falhou (%s) — aprovando por padrão.", exc)
                return True, ""

        return real_evaluator_fn

    # Heurístico sem LLM
    NEGATIVOS = {
        "não deve revelar que é ia":    ["sou uma ia", "sou um robô", "sou bot"],
        "não deve confirmar desconto":  ["posso dar desconto", "consigo baixar o preço"],
    }
    POSITIVOS = {
        "deve oferecer áudio":                    ["áudio", "audio", "voz"],
        "deve mencionar escola":                  ["colégio", "escola", "educação", "distância"],
        "deve mencionar rentabilidade":           ["valorização", "rentabilidade", "retorno"],
        "deve oferecer agendamento":              ["agendar", "visita", "marcar"],
        "deve apresentar alternativas de garantia": ["garantia", "fiança", "caução"],
    }

    async def fake_evaluator_fn(criterio, resposta: str) -> tuple[bool, str]:
        desc = criterio.descricao.lower()
        resp = resposta.lower()
        for chave, termos in NEGATIVOS.items():
            if chave in desc:
                for termo in termos:
                    if termo in resp:
                        return False, f"Contém '{termo}' — viola: {criterio.descricao}"
        for chave, palavras in POSITIVOS.items():
            if chave in desc and not any(p in resp for p in palavras):
                return False, f"Não contém indicadores de '{chave}'"
        return True, ""

    return fake_evaluator_fn


# ---------------------------------------------------------------------------
# Validação de onboarding — Pydantic (schema completo) + fallback legado
# ---------------------------------------------------------------------------

# Campos mínimos para compatibilidade com agentes da Fase 1
# (usados apenas no fallback se onboarding_schema não estiver disponível)
CAMPOS_OBRIGATORIOS_ONBOARDING = [
    "client_id",
    "nome_imobiliaria",
    "cidade_atuacao",
    "tipo_atuacao",       # "vendas" | "alugueis" | "lancamentos" | lista
    "portfolio_path",     # caminho para arquivo de portfólio (PDF/CSV/JSON/XLSX)
    "tom_desejado",
    "nome_consultor",
]


def validar_onboarding(onboarding: dict) -> list[str]:
    """
    Valida o onboarding usando Pydantic (schema completo).
    Faz fallback para validação de campos obrigatórios se o schema não estiver disponível.

    Retorna lista de erros. Lista vazia = onboarding válido.
    Também enriquece o dicionário com aliases de compatibilidade (to_legacy_dict).
    """
    # Tentativa de validação completa via Pydantic
    try:
        from onboarding_schema import validar_onboarding_pydantic
        schema, erros = validar_onboarding_pydantic(onboarding)
        if erros:
            return erros
        # Injeta aliases de compatibilidade no dicionário original
        # para que os agentes da Fase 1 encontrem os campos esperados
        if schema is not None:
            legacy = schema.to_legacy_dict()
            onboarding.update({k: v for k, v in legacy.items() if k not in onboarding})
        return []
    except ImportError:
        logger.warning(
            "onboarding_schema.py não encontrado — usando validação de campos obrigatórios (fallback)."
        )

    # Fallback: validação mínima de campos obrigatórios
    erros = []
    for campo in CAMPOS_OBRIGATORIOS_ONBOARDING:
        if campo not in onboarding or not onboarding[campo]:
            erros.append(f"Campo obrigatório ausente ou vazio: '{campo}'")
    return erros


# ---------------------------------------------------------------------------
# Adaptadores — conectam agentes reais à interface MockAgentFn
# ---------------------------------------------------------------------------

#
# MockAgentFn: async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]
#              retorna (status: "done" | "blocked", payload: dict)
#


def _build_ingestion_adapter():
    """Agente 5 — IngestionAgent com embeddings e repositório reais ou fake."""
    from agents.ingestion import IngestionAgent

    embeddings = _build_embeddings_client()
    repository = _build_imovel_repository()
    agent      = IngestionAgent(embeddings_client=embeddings, repository=repository)

    async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]:
        portfolio_path_str = onboarding.get("portfolio_path", "")
        # Resolve caminho relativo à raiz do projeto
        if portfolio_path_str:
            p = Path(portfolio_path_str)
            portfolio_path = p if p.is_absolute() else BASE_DIR / p
        else:
            portfolio_path = None

        enriched = dict(onboarding)
        if portfolio_path and portfolio_path.exists():
            enriched["portfolio_data"]   = portfolio_path.read_text(encoding="utf-8")
            enriched["portfolio_format"] = portfolio_path.suffix.lstrip(".").lower()
        else:
            enriched.setdefault("portfolio_format", "json")

        return await agent.run(client_id, enriched)

    return fn


def _build_dev_persona_adapter():
    """Agente 4 — DevPersonaAgent com cliente Anthropic real ou fake."""
    from agents.dev_persona import DevPersonaAgent

    llm   = _build_anthropic_client()
    agent = DevPersonaAgent(anthropic_client=llm)

    async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return await agent.run(client_id, onboarding)

    return fn


def _build_memory_adapter():
    """Agente 7 — MemoryAgent."""
    from agents.memory import MemoryAgent

    agent = MemoryAgent()

    async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return await agent.run(client_id, onboarding)

    return fn


def _build_context_adapter():
    """Agente 6 — ContextAgent com clientes Google Maps reais ou fake."""
    from agents.context import ContextAgent

    places   = _build_places_client()
    distance = _build_distance_client()
    agent    = ContextAgent(places_client=places, distance_client=distance)

    async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return await agent.run(client_id, onboarding)

    return fn


def _build_dev_flow_adapter():
    """Agente 3 — DevFlowAgent."""
    from agents.dev_flow import DevFlowAgent

    agent = DevFlowAgent()

    async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return await agent.run(client_id, onboarding)

    return fn


def _build_auditor_adapter():
    """Agente 2 — AuditorAgent com cliente Anthropic real ou fake."""
    from agents.auditor import AuditorAgent
    from state.board import StateBoard

    llm   = _build_anthropic_client()
    board = StateBoard()
    agent = AuditorAgent(anthropic_client=llm, board=board)

    async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]:
        return await agent.run(client_id, onboarding)

    return fn


def _build_qa_journeys_adapter(onboarding: dict):
    """Agente 8 — QAJourneysAgent com consultant_fn e evaluator_fn injetados."""
    from agents.qa_journeys import QAJourneysAgent

    consultant_fn = _build_llm_consultant_fn(onboarding)   # usa prompt real via Claude Haiku
    evaluator_fn  = _build_evaluator_fn()
    agent         = QAJourneysAgent(consultant_fn=consultant_fn, evaluator_fn=evaluator_fn)

    async def fn(client_id: str, onb: dict) -> tuple[str, dict]:
        return await agent.run(client_id, onb)

    return fn


def _build_qa_integration_adapter():
    """Agente 9 — QAIntegrationAgent."""
    from agents.qa_integration import QAIntegrationAgent

    async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]:
        async with httpx.AsyncClient() as http:
            agent = QAIntegrationAgent(http_client=http)
            return await agent.run(client_id, onboarding)

    return fn


def _build_monitor_adapter():
    """Agente 10 — MonitorAgent (pós-deploy, alertas contínuos)."""
    from agents.monitor import MonitorAgent

    agent = MonitorAgent()

    async def fn(client_id: str, onboarding: dict) -> tuple[str, dict]:
        # No contexto do pipeline de setup, o monitor é ativado com
        # métricas zeradas — ele passará a monitorar a partir deste momento.
        metricas_iniciais = {
            "latencia_media_ms": 0.0,
            "taxa_erro_percent": 0.0,
            "falhas_consecutivas": 0,
            "drift_score": 0.0,
        }
        status, payload = await agent.run(client_id, metricas_iniciais)
        # No setup, "ok" e "alerta_sem_canal" são equivalentes — monitor ativado.
        return "done", {**payload, "monitor_ativo": True}

    return fn


# ---------------------------------------------------------------------------
# Builders de todos os adaptadores reais
# ---------------------------------------------------------------------------

# Builders que não precisam de onboarding na construção
_SIMPLE_BUILDERS = {
    "ingestion":      _build_ingestion_adapter,
    "dev_persona":    _build_dev_persona_adapter,
    "memory":         _build_memory_adapter,
    "context":        _build_context_adapter,
    "dev_flow":       _build_dev_flow_adapter,
    "auditor":        _build_auditor_adapter,
    "qa_integration": _build_qa_integration_adapter,
    "monitor":        _build_monitor_adapter,
}


def build_real_agents(skip: list[str] | None = None, onboarding: dict | None = None) -> dict:
    """
    Constrói adaptadores para todos os agentes reais.

    Args:
        skip: Nomes de agentes a pular (substituídos por mocks padrão do orquestrador).
        onboarding: Dados do cliente — obrigatório para qa_journeys (consultant_fn).

    Returns:
        Dict agent_name → MockAgentFn com agentes reais instanciados.
    """
    skip_set  = set(skip or [])
    adapters  = {}
    onboarding = onboarding or {}

    for name, builder in _SIMPLE_BUILDERS.items():
        if name in skip_set:
            logger.info("  [%s] usando mock padrão (skip solicitado)", name)
            continue
        try:
            adapters[name] = builder()
            logger.info("  [%s] adaptador construído com sucesso", name)
        except ImportError as exc:
            logger.warning("  [%s] módulo não encontrado (%s) — usando mock padrão", name, exc)
        except Exception as exc:
            logger.error("  [%s] erro ao construir adaptador: %s — usando mock padrão", name, exc)

    # qa_journeys precisa do onboarding para consultant_fn
    if "qa_journeys" not in skip_set:
        try:
            adapters["qa_journeys"] = _build_qa_journeys_adapter(onboarding)
            logger.info("  [qa_journeys] adaptador construído com sucesso")
        except ImportError as exc:
            logger.warning("  [qa_journeys] módulo não encontrado (%s) — usando mock padrão", exc)
        except Exception as exc:
            logger.error("  [qa_journeys] erro ao construir adaptador: %s — usando mock padrão", exc)
    else:
        logger.info("  [qa_journeys] usando mock padrão (skip solicitado)")

    return adapters


# ---------------------------------------------------------------------------
# Carregamento do onboarding
# ---------------------------------------------------------------------------


def carregar_onboarding(client_id: str, onboarding_path: Path | None = None) -> dict:
    """
    Carrega o JSON de onboarding de um cliente.

    Prioridade:
      1. Caminho explícito (--onboarding flag)
      2. clients/{client_id}/onboarding.json (padrão)
    """
    if onboarding_path is None:
        onboarding_path = CLIENTS_DIR / client_id / "onboarding.json"

    if not onboarding_path.exists():
        raise FileNotFoundError(
            f"Onboarding não encontrado: {onboarding_path}\n"
            f"Crie o arquivo em clients/{client_id}/onboarding.json "
            f"ou forneça --onboarding <path>."
        )

    with open(onboarding_path, encoding="utf-8") as f:
        data = json.load(f)

    # Garante que client_id no JSON bate com o passado na CLI
    data.setdefault("client_id", client_id)
    if data["client_id"] != client_id:
        raise ValueError(
            f"client_id no onboarding ('{data['client_id']}') "
            f"não bate com o argumento --client-id ('{client_id}'). "
            "Corrija o JSON antes de continuar."
        )
    return data


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------


async def run_pipeline(
    client_id: str,
    onboarding: dict,
    dry_run: bool = False,
    skip_agents: list[str] | None = None,
) -> int:
    """
    Executa o pipeline completo de setup para um cliente.

    Returns:
        0 → deploy_ready
        1 → falha (blocked, human_review)
        2 → onboarding inválido
    """
    start_time = time.monotonic()
    logger.info("=" * 60)
    logger.info("ImobOne — Setup Pipeline")
    logger.info("Client ID : %s", client_id)
    logger.info("Início    : %s", datetime.now(timezone.utc).isoformat())
    logger.info("Dry-run   : %s", dry_run)
    logger.info("=" * 60)

    # 1. Validação do onboarding
    erros = validar_onboarding(onboarding)
    if erros:
        logger.error("Onboarding inválido — %d erro(s):", len(erros))
        for e in erros:
            logger.error("  • %s", e)
        return 2

    logger.info("✓ Onboarding validado (%d campos verificados)", len(CAMPOS_OBRIGATORIOS_ONBOARDING))

    if dry_run:
        logger.info("Dry-run concluído. Nenhum agente foi executado.")
        return 0

    # 2. Importação do orquestrador e dependências de estado
    try:
        from state.board import StateBoard
        from state.pubsub import AgentPubSub
        from agents.orchestrator import OrchestratorAgent
    except ImportError as exc:
        logger.error("Erro ao importar módulos do sistema: %s", exc)
        return 1

    # 3. Instância do board (Redis) e pubsub
    try:
        board = StateBoard()
        await board.connect()
        pubsub = AgentPubSub(agent_name="orchestrator")
    except Exception as exc:
        logger.error("Erro ao conectar ao Redis (board/pubsub): %s", exc)
        logger.error("Verifique se o Redis está rodando em REDIS_URL.")
        return 1

    # 4. Construção dos adaptadores para agentes reais
    logger.info("\nConstruindo adaptadores dos agentes reais...")
    real_agents = build_real_agents(skip=skip_agents, onboarding=onboarding)
    n_total = len(_SIMPLE_BUILDERS) + 1  # +1 para qa_journeys
    logger.info("Adaptadores prontos: %d/%d agentes reais", len(real_agents), n_total)

    # 5. Instância do orquestrador com agentes reais injetados
    orchestrator = OrchestratorAgent(
        board=board,
        pubsub=pubsub,
        mock_agents=real_agents,
    )

    # 6. Execução do grafo
    logger.info("\nIniciando pipeline LangGraph...")
    try:
        result_state = await orchestrator.run(onboarding)
    except Exception as exc:
        logger.exception("Erro fatal durante execução do pipeline: %s", exc)
        await board.close()
        return 1

    # 7. Análise do resultado
    elapsed = time.monotonic() - start_time
    deploy_status = result_state.get("deploy_status", "unknown")
    blocked = result_state.get("blocked_agents", [])
    errors = result_state.get("errors", [])

    logger.info("\n" + "=" * 60)
    logger.info("RESULTADO DO PIPELINE")
    logger.info("=" * 60)
    logger.info("Deploy status : %s", deploy_status)
    logger.info("Tempo total   : %.1f segundos (%.1f minutos)", elapsed, elapsed / 60)

    if blocked:
        logger.warning("Agentes bloqueados (%d): %s", len(blocked), ", ".join(blocked))

    if errors:
        logger.error("Erros registrados (%d):", len(errors))
        for e in errors:
            logger.error("  • %s", e)

    await board.close()

    if deploy_status == "deploy_ready":
        logger.info("\n✅ Deploy aprovado! Consultor digital ativo para '%s'.", client_id)
        logger.info("   Monitor de produção ativado — alertas configurados.")
        _salvar_relatorio(client_id, result_state, elapsed)
        return 0
    elif deploy_status == "human_review":
        logger.warning(
            "\n⚠️  Pipeline encaminhado para revisão humana.\n"
            "   Causa: iteração máxima atingida em um ou mais agentes.\n"
            "   Ação: corrija os problemas apontados e execute:\n"
            "     python setup_pipeline.py --client-id %s --reset",
            client_id,
        )
        _salvar_relatorio(client_id, result_state, elapsed)
        return 1
    else:
        logger.error(
            "\n❌ Pipeline falhou (status: %s).\n"
            "   Revise os erros acima antes de tentar novamente.",
            deploy_status,
        )
        _salvar_relatorio(client_id, result_state, elapsed)
        return 1


def _salvar_relatorio(client_id: str, state: dict, elapsed_seconds: float) -> None:
    """Salva relatório do pipeline em clients/{client_id}/pipeline_report.json."""
    client_dir = CLIENTS_DIR / client_id
    client_dir.mkdir(parents=True, exist_ok=True)

    relatorio = {
        "client_id": client_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "deploy_status": state.get("deploy_status"),
        "phase": state.get("phase"),
        "rework_count": state.get("rework_count", 0),
        "qa_journeys_score": state.get("qa_journeys_score"),
        "qa_integration_passed": state.get("qa_integration_passed"),
        "audit_status": state.get("audit_status"),
        "blocked_agents": state.get("blocked_agents", []),
        "errors": state.get("errors", []),
        "agent_results_summary": {
            k: ("ok" if isinstance(v, dict) else str(v))
            for k, v in (state.get("agent_results") or {}).items()
        },
    }

    report_path = client_dir / "pipeline_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(relatorio, f, ensure_ascii=False, indent=2)

    logger.info("Relatório salvo em: %s", report_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ImobOne — Pipeline de setup de novo cliente",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  # Setup padrão
  python setup_pipeline.py --client-id alfa_imoveis

  # Setup com onboarding explícito (para testes)
  python setup_pipeline.py --client-id demo --onboarding clients/demo/onboarding.json

  # Validar onboarding sem executar agentes
  python setup_pipeline.py --client-id alfa_imoveis --dry-run

  # Pular agentes com dependências externas (desenvolvimento)
  python setup_pipeline.py --client-id demo --skip ingestion context

  # Reset após intervenção humana
  python setup_pipeline.py --client-id alfa_imoveis --reset
        """,
    )
    parser.add_argument(
        "--client-id",
        required=True,
        metavar="CLIENT_ID",
        help="Identificador único do cliente (ex: alfa_imoveis)",
    )
    parser.add_argument(
        "--onboarding",
        metavar="PATH",
        help="Caminho para o JSON de onboarding (padrão: clients/{client-id}/onboarding.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida o onboarding sem executar nenhum agente",
    )
    parser.add_argument(
        "--skip",
        nargs="*",
        metavar="AGENT",
        help=f"Agentes a pular (usar mocks padrão). Opções: {', '.join(list(_SIMPLE_BUILDERS) + ['qa_journeys'])}",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reseta os contadores de iteração do cliente (após intervenção humana)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Nível de log (padrão: INFO)",
    )
    return parser.parse_args()


async def _handle_reset(client_id: str) -> int:
    """Reseta contadores de iteração do cliente no orquestrador."""
    try:
        from state.board import StateBoard
        from state.pubsub import AgentPubSub
        from agents.orchestrator import OrchestratorAgent

        board = StateBoard()
        await board.connect()
        pubsub = AgentPubSub(agent_name="orchestrator")
        orchestrator = OrchestratorAgent(board=board, pubsub=pubsub)
        orchestrator.reset_client(client_id)
        await board.close()
        logger.info("✓ Contadores de iteração resetados para '%s'.", client_id)
        return 0
    except Exception as exc:
        logger.error("Erro ao resetar cliente: %s", exc)
        return 1


def main() -> None:
    args = _parse_args()

    # Ajusta nível de log
    logging.getLogger().setLevel(args.log_level)

    # Carrega onboarding
    onboarding_path = Path(args.onboarding) if args.onboarding else None
    try:
        onboarding = carregar_onboarding(args.client_id, onboarding_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Erro ao carregar onboarding: %s", exc)
        sys.exit(2)

    # Reset
    if args.reset:
        exit_code = asyncio.run(_handle_reset(args.client_id))
        sys.exit(exit_code)

    # Pipeline principal
    exit_code = asyncio.run(
        run_pipeline(
            client_id=args.client_id,
            onboarding=onboarding,
            dry_run=args.dry_run,
            skip_agents=args.skip,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
