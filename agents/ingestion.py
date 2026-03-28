"""
agents/ingestion.py — Agente 5: Ingestão de Portfólio

Responsabilidades:
  1. Carregar o portfólio de imóveis do cliente (PDF, JSON, Excel, CSV)
  2. Extrair e normalizar cada imóvel para o schema canônico
  3. Gerar embeddings via text-embedding-3-small
  4. Salvar no Supabase pgvector com namespace isolado por client_id
  5. Retornar relatório de cobertura com contagens e campos faltantes

Schema canônico de imóvel (campos obrigatórios / opcionais):
  Obrigatórios: titulo, tipo_imovel, tipo_negocio, endereco, valor
  Opcionais: bairro, cidade, estado, cep, area_m2, quartos, banheiros,
             vagas, descricao, latitude, longitude, caracteristicas

Integração com o orquestrador:
  O método run(client_id, onboarding) é compatível com o contrato
  de MockAgentFn — retorna (status: str, payload: dict).

Uso standalone:
    agent = IngestionAgent(embeddings_client, repository)
    status, payload = await agent.run("cliente_001", onboarding)
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema canônico de imóvel
# ---------------------------------------------------------------------------

CAMPOS_OBRIGATORIOS: list[str] = [
    "titulo",
    "tipo_imovel",
    "tipo_negocio",
    "endereco",
    "valor",
]

CAMPOS_OPCIONAIS: list[str] = [
    "bairro",
    "cidade",
    "estado",
    "cep",
    "area_m2",
    "quartos",
    "banheiros",
    "vagas",
    "descricao",
    "latitude",
    "longitude",
    "caracteristicas",
]

TODOS_CAMPOS = CAMPOS_OBRIGATORIOS + CAMPOS_OPCIONAIS

# Mapa de aliases comuns em planilhas e JSONs dos clientes
ALIAS_MAP: dict[str, str] = {
    "nome": "titulo",
    "name": "titulo",
    "title": "titulo",
    "tipo": "tipo_imovel",
    "type": "tipo_imovel",
    "negocio": "tipo_negocio",
    "negócio": "tipo_negocio",
    "business_type": "tipo_negocio",
    "address": "endereco",
    "endereço": "endereco",
    "preco": "valor",
    "preço": "valor",
    "price": "valor",
    "area": "area_m2",
    "área": "area_m2",
    "rooms": "quartos",
    "bedrooms": "quartos",
    "bathrooms": "banheiros",
    "parking": "vagas",
    "garage": "vagas",
    "description": "descricao",
    "descrição": "descricao",
    "lat": "latitude",
    "lon": "longitude",
    "lng": "longitude",
    "features": "caracteristicas",
    "amenities": "caracteristicas",
}


# ---------------------------------------------------------------------------
# Protocol — Repositório Supabase (injetável em testes)
# ---------------------------------------------------------------------------


@runtime_checkable
class ImovelRepositoryProtocol(Protocol):
    """
    Abstração do repositório de imóveis no Supabase pgvector.
    Implementar esta interface permite substituir o Supabase por mocks em testes.
    """

    async def upsert_batch(
        self,
        client_id: str,
        records: list[dict[str, Any]],
    ) -> int:
        """Insere/atualiza imóveis. Retorna número de registros processados."""
        ...

    async def count(self, client_id: str) -> int:
        """Conta imóveis indexados para um cliente."""
        ...

    async def delete_namespace(self, client_id: str) -> int:
        """Remove todos os imóveis de um cliente (para re-ingestão)."""
        ...


# ---------------------------------------------------------------------------
# Implementação do repositório Supabase (real)
# ---------------------------------------------------------------------------


class SupabaseImovelRepository:
    """
    Repositório de imóveis usando Supabase + pgvector.

    Tabela esperada:
        CREATE TABLE imoveis_embeddings (
            id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id   TEXT        NOT NULL,
            imovel_id   TEXT        NOT NULL,
            conteudo    TEXT        NOT NULL,
            embedding   VECTOR(1536),
            metadata    JSONB,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(client_id, imovel_id)
        );
        CREATE INDEX ON imoveis_embeddings (client_id);
    """

    TABLE_NAME = "imoveis_embeddings"

    def __init__(self, supabase_client: Any) -> None:
        self._client = supabase_client

    async def upsert_batch(
        self,
        client_id: str,
        records: list[dict[str, Any]],
    ) -> int:
        if not records:
            return 0

        # Garante isolamento: todos os registros devem ter o client_id correto
        for rec in records:
            rec["client_id"] = client_id

        result = (
            self._client.table(self.TABLE_NAME)
            .upsert(records, on_conflict="client_id,imovel_id")
            .execute()
        )
        return len(result.data or [])

    async def count(self, client_id: str) -> int:
        result = (
            self._client.table(self.TABLE_NAME)
            .select("id", count="exact")
            .eq("client_id", client_id)
            .execute()
        )
        return result.count or 0

    async def delete_namespace(self, client_id: str) -> int:
        result = (
            self._client.table(self.TABLE_NAME)
            .delete()
            .eq("client_id", client_id)
            .execute()
        )
        return len(result.data or [])


# ---------------------------------------------------------------------------
# Parsers por formato
# ---------------------------------------------------------------------------


def _normalizar_chave(chave: str) -> str:
    """Normaliza chave: lowercase, sem espaços, com substituição de aliases."""
    chave_norm = chave.strip().lower().replace(" ", "_").replace("-", "_")
    return ALIAS_MAP.get(chave_norm, chave_norm)


def _normalizar_imovel(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normaliza um dict bruto para o schema canônico.
    Converte aliases, tipos e retorna apenas campos conhecidos.
    """
    imovel: dict[str, Any] = {}

    for k, v in raw.items():
        campo = _normalizar_chave(k)
        if campo in TODOS_CAMPOS:
            # Conversões de tipo
            if campo in ("area_m2", "valor"):
                try:
                    imovel[campo] = float(str(v).replace(".", "").replace(",", ".").strip())
                except (ValueError, AttributeError):
                    imovel[campo] = None
            elif campo in ("quartos", "banheiros", "vagas"):
                try:
                    imovel[campo] = int(v)
                except (ValueError, TypeError):
                    imovel[campo] = None
            elif campo in ("latitude", "longitude"):
                try:
                    imovel[campo] = float(v)
                except (ValueError, TypeError):
                    imovel[campo] = None
            elif campo == "caracteristicas":
                if isinstance(v, list):
                    imovel[campo] = v
                elif isinstance(v, str):
                    imovel[campo] = [c.strip() for c in v.split(",") if c.strip()]
                else:
                    imovel[campo] = []
            else:
                imovel[campo] = str(v).strip() if v is not None else None

    return imovel


def _campos_faltantes(imovel: dict[str, Any]) -> list[str]:
    """Retorna campos obrigatórios ausentes ou vazios."""
    return [
        campo for campo in CAMPOS_OBRIGATORIOS
        if not imovel.get(campo)
    ]


def parse_json(content: str | bytes) -> list[dict[str, Any]]:
    """Parseia portfólio em formato JSON (lista ou objeto com lista)."""
    data = json.loads(content)
    if isinstance(data, list):
        return [_normalizar_imovel(item) for item in data]
    if isinstance(data, dict):
        # Aceita {"imoveis": [...]} ou {"data": [...]} ou {"portfolio": [...]}
        for key in ("imoveis", "imóveis", "data", "portfolio", "properties"):
            if key in data and isinstance(data[key], list):
                return [_normalizar_imovel(item) for item in data[key]]
        # Objeto único → lista com 1 item
        return [_normalizar_imovel(data)]
    raise ValueError(f"Formato JSON não reconhecido: esperava lista ou objeto, got {type(data)}")


def parse_csv(content: str | bytes) -> list[dict[str, Any]]:
    """Parseia portfólio em formato CSV."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")  # remove BOM se houver

    reader = csv.DictReader(io.StringIO(content))
    return [_normalizar_imovel(dict(row)) for row in reader]


def parse_excel(content: bytes) -> list[dict[str, Any]]:
    """Parseia portfólio em formato Excel (.xlsx)."""
    try:
        import openpyxl  # importado aqui para não falhar no import do módulo
    except ImportError as exc:
        raise ImportError("openpyxl é necessário para processar arquivos Excel.") from exc

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
    imoveis = []

    for row in rows[1:]:
        if all(v is None for v in row):
            continue  # pula linhas vazias
        raw = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
        imoveis.append(_normalizar_imovel(raw))

    return imoveis


async def parse_pdf(
    content: bytes,
    anthropic_client: Any,
    client_id: str,
) -> list[dict[str, Any]]:
    """
    Parseia portfólio em formato PDF usando Claude Haiku para extração estruturada.

    Fluxo:
      1. pdfplumber extrai texto raw do PDF
      2. Claude Haiku converte para JSON estruturado de imóveis

    Args:
        content: Bytes do arquivo PDF.
        anthropic_client: anthropic.AsyncAnthropic — Claude Haiku para extração.
        client_id: Usado para logging contextualizado.
    """
    try:
        import pdfplumber  # importado aqui para não falhar no import do módulo
    except ImportError as exc:
        raise ImportError("pdfplumber é necessário para processar arquivos PDF.") from exc

    # Extrai texto de todas as páginas
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        texto_pages = [page.extract_text() or "" for page in pdf.pages]

    texto_completo = "\n\n---\n\n".join(texto_pages).strip()

    if not texto_completo:
        raise ValueError("PDF sem texto extraível — possivelmente escaneado sem OCR.")

    # Claude Haiku extrai estrutura
    prompt = f"""Você receberá o texto de um portfólio de imóveis de uma imobiliária.
Extraia todos os imóveis e retorne um JSON com a seguinte estrutura:

[
  {{
    "titulo": "nome/título do imóvel",
    "tipo_imovel": "apartamento|casa|cobertura|terreno|comercial|studio|etc",
    "tipo_negocio": "venda|aluguel|lançamento",
    "endereco": "endereço completo",
    "bairro": "bairro",
    "cidade": "cidade",
    "estado": "sigla do estado",
    "cep": "CEP se disponível",
    "valor": número_em_reais_sem_formatação,
    "area_m2": número,
    "quartos": número,
    "banheiros": número,
    "vagas": número,
    "descricao": "descrição do imóvel",
    "latitude": número_ou_null,
    "longitude": número_ou_null,
    "caracteristicas": ["lista", "de", "características"]
  }}
]

Retorne APENAS o JSON, sem texto adicional.

TEXTO DO PORTFÓLIO:
{texto_completo[:8000]}"""

    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    resposta_texto = response.content[0].text.strip()

    # Remove markdown se Claude retornou com ```json ... ```
    if resposta_texto.startswith("```"):
        linhas = resposta_texto.split("\n")
        resposta_texto = "\n".join(linhas[1:-1])

    try:
        dados = json.loads(resposta_texto)
        return [_normalizar_imovel(item) for item in dados]
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Claude Haiku retornou JSON inválido para PDF do cliente '{client_id}': {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# IngestionAgent
# ---------------------------------------------------------------------------


class IngestionAgent:
    """
    Agente 5 — Ingestão de Portfólio.

    Args:
        embeddings_client: Instância compatível com EmbeddingsClientProtocol.
        repository: Instância compatível com ImovelRepositoryProtocol.
        anthropic_client: anthropic.AsyncAnthropic para extração de PDFs.
                          Opcional — obrigatório apenas se o portfólio for PDF.
    """

    def __init__(
        self,
        embeddings_client: Any,
        repository: Any,
        anthropic_client: Any | None = None,
    ) -> None:
        self.embeddings = embeddings_client
        self.repository = repository
        self.anthropic = anthropic_client

    # ------------------------------------------------------------------
    # Interface pública (compatível com MockAgentFn do orquestrador)
    # ------------------------------------------------------------------

    async def run(
        self,
        client_id: str,
        onboarding: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """
        Executa a ingestão completa do portfólio de um cliente.

        Lê onboarding["portfolio_data"] (bytes/str) e onboarding["portfolio_format"]
        (json|csv|excel|pdf). Se portfolio_data não existir, usa dados de exemplo mínimos
        para não bloquear o pipeline em desenvolvimento.

        Returns:
            ("done", payload) com relatório de cobertura, ou
            ("blocked", {"error": str}) em caso de falha irrecuperável.
        """
        portfolio_data = onboarding.get("portfolio_data")
        portfolio_format = onboarding.get("portfolio_format", "json").lower()

        # Dados de exemplo para desenvolvimento sem portfólio real
        if portfolio_data is None:
            logger.warning(
                "[Ingestion] portfolio_data ausente para cliente '%s'. "
                "Usando portfólio de exemplo mínimo.",
                client_id,
            )
            portfolio_data = json.dumps([{
                "titulo": "Imóvel de Exemplo",
                "tipo_imovel": "apartamento",
                "tipo_negocio": "venda",
                "endereco": "Rua Exemplo, 1, São Paulo",
                "valor": 1000000,
            }])
            portfolio_format = "json"

        try:
            imoveis = await self._carregar_portfolio(
                portfolio_data, portfolio_format, client_id
            )
        except Exception as exc:
            logger.error("[Ingestion] Falha ao carregar portfólio do cliente '%s': %s", client_id, exc)
            return "blocked", {"error": f"Falha ao carregar portfólio: {exc}"}

        if not imoveis:
            return "blocked", {"error": "Portfólio vazio — nenhum imóvel extraído."}

        # Analisa cobertura de campos
        relatorio_por_imovel = self._analisar_cobertura(imoveis)

        # Gera embeddings em batch
        try:
            from tools.embeddings import build_imovel_text

            textos = [build_imovel_text(im) for im in imoveis]
            embeddings = await self.embeddings.generate_batch(textos)
        except Exception as exc:
            logger.error("[Ingestion] Falha ao gerar embeddings para cliente '%s': %s", client_id, exc)
            return "blocked", {"error": f"Falha ao gerar embeddings: {exc}"}

        # Prepara registros para o Supabase
        records = []
        for i, (imovel, embedding, texto) in enumerate(zip(imoveis, embeddings, textos)):
            records.append({
                "imovel_id": imovel.get("titulo", f"imovel_{i}").lower().replace(" ", "_")[:80],
                "conteudo": texto,
                "embedding": embedding,
                "metadata": imovel,
            })

        # Persiste no Supabase (namespace isolado por client_id)
        try:
            indexados = await self.repository.upsert_batch(client_id, records)
        except Exception as exc:
            logger.error("[Ingestion] Falha ao salvar no Supabase para cliente '%s': %s", client_id, exc)
            return "blocked", {"error": f"Falha ao salvar embeddings: {exc}"}

        payload = self._build_payload(client_id, imoveis, relatorio_por_imovel, indexados)
        logger.info(
            "[Ingestion] Cliente '%s': %d imóveis indexados de %d extraídos.",
            client_id,
            indexados,
            len(imoveis),
        )
        return "done", payload

    # ------------------------------------------------------------------
    # Carregamento por formato
    # ------------------------------------------------------------------

    async def _carregar_portfolio(
        self,
        data: str | bytes,
        fmt: str,
        client_id: str,
    ) -> list[dict[str, Any]]:
        if fmt == "json":
            return parse_json(data)
        if fmt == "csv":
            return parse_csv(data)
        if fmt in ("excel", "xlsx", "xls"):
            if isinstance(data, str):
                data = data.encode("latin-1")
            return parse_excel(data)
        if fmt == "pdf":
            if isinstance(data, str):
                data = data.encode("latin-1")
            if self.anthropic is None:
                raise ValueError("anthropic_client é obrigatório para processar PDFs.")
            return await parse_pdf(data, self.anthropic, client_id)

        raise ValueError(f"Formato de portfólio não suportado: '{fmt}'. Use json|csv|excel|pdf.")

    # ------------------------------------------------------------------
    # Análise de cobertura
    # ------------------------------------------------------------------

    @staticmethod
    def _analisar_cobertura(imoveis: list[dict]) -> list[dict[str, Any]]:
        """Identifica campos faltantes por imóvel."""
        relatorio = []
        for i, imovel in enumerate(imoveis):
            faltantes = _campos_faltantes(imovel)
            relatorio.append({
                "titulo": imovel.get("titulo", f"imóvel_{i}"),
                "campos_faltantes": faltantes,
                "completo": len(faltantes) == 0,
            })
        return relatorio

    @staticmethod
    def _build_payload(
        client_id: str,
        imoveis: list[dict],
        relatorio: list[dict],
        indexados: int,
    ) -> dict[str, Any]:
        total = len(imoveis)
        completos = sum(1 for r in relatorio if r["completo"])
        com_faltantes = [r for r in relatorio if not r["completo"]]

        return {
            "client_id": client_id,
            "imoveis_extraidos": total,
            "imoveis_indexados": indexados,
            "imoveis_completos": completos,
            "imoveis_com_campos_faltantes": len(com_faltantes),
            "detalhes_faltantes": com_faltantes,
            "cobertura_percentual": round(completos / total * 100, 1) if total else 0.0,
        }
