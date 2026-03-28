"""
tests/test_ingestion.py — Testes unitários de agents/ingestion.py.

Mínimo exigido: 3 testes (CLAUDE.md). Todas as chamadas externas são mockadas
via unittest.mock — Google APIs e Supabase ficam para o Agente 9 (QA integração).

Cobertura:
  1. Formato JSON  → parse correto, campos obrigatórios extraídos
  2. Formato CSV   → parse com campos faltantes, relatório de cobertura correto
  3. Formato Excel → parse via openpyxl mockado, campos normalizados
  4. Formato PDF   → extração via Claude Haiku mockado
  5. Isolamento de namespace → dois clientes não compartilham dados no repositório
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import pytest_asyncio

from agents.ingestion import (
    IngestionAgent,
    _campos_faltantes,
    _normalizar_imovel,
    parse_csv,
    parse_json,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_embeddings_mock(n: int = 1) -> AsyncMock:
    """Mock de EmbeddingsClient que retorna vetores fixos de dimensão 1536."""
    mock = AsyncMock()
    mock.generate_batch.return_value = [[0.1] * 1536 for _ in range(n)]
    return mock


def _make_repository_mock(indexados: int = 0) -> AsyncMock:
    """Mock de ImovelRepositoryProtocol."""
    mock = AsyncMock()
    mock.upsert_batch.return_value = indexados
    mock.count.return_value = indexados
    mock.delete_namespace.return_value = indexados
    return mock


def _onboarding(client_id: str, data=None, fmt: str = "json") -> dict:
    return {
        "client_id": client_id,
        "nome_imobiliaria": "Imob Teste",
        "portfolio_data": data,
        "portfolio_format": fmt,
    }


# JSON de portfólio com 3 imóveis — dois completos, um com campos faltantes
PORTFOLIO_JSON_3_IMOVEIS = json.dumps([
    {
        "titulo": "Cobertura Duplex Itaim Bibi",
        "tipo_imovel": "cobertura",
        "tipo_negocio": "venda",
        "endereco": "Rua Joaquim Floriano, 960, Itaim Bibi, São Paulo",
        "bairro": "Itaim Bibi",
        "cidade": "São Paulo",
        "estado": "SP",
        "area_m2": "380",
        "quartos": "4",
        "banheiros": "5",
        "vagas": "3",
        "valor": "4500000",
        "descricao": "Cobertura com terraço e piscina privativa.",
        "caracteristicas": "terraço,piscina,vista panorâmica",
    },
    {
        "titulo": "Apartamento Alto dos Pinheiros",
        "tipo_imovel": "apartamento",
        "tipo_negocio": "venda",
        "endereco": "Av. Pedroso de Morais, 500, Pinheiros, São Paulo",
        "bairro": "Pinheiros",
        "cidade": "São Paulo",
        "estado": "SP",
        "area_m2": "180",
        "quartos": "3",
        "valor": "2800000",
    },
    {
        # Imóvel incompleto — sem titulo e sem valor
        "tipo_imovel": "casa",
        "tipo_negocio": "aluguel",
        "endereco": "Rua sem título, Morumbi",
    },
])

# CSV simples com 2 imóveis
PORTFOLIO_CSV_2_IMOVEIS = """\
titulo,tipo_imovel,tipo_negocio,endereco,valor,area_m2,quartos
Studio Faria Lima,studio,aluguel,Av. Brigadeiro Faria Lima 2000 SP,12000,45,1
Casa Jardim Europa,casa,venda,Rua da Europa 50 SP,8500000,600,5
"""


# ---------------------------------------------------------------------------
# Teste 1 — Formato JSON: parse e normalização corretos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_json_format_parse_and_normalization():
    """
    parse_json deve extrair 3 imóveis, normalizar tipos (str→float para valor,
    str→int para quartos) e o agente deve reportar cobertura corretamente.
    O repositório deve ser chamado com o client_id correto.
    """
    embeddings_mock = _make_embeddings_mock(3)
    repo_mock = _make_repository_mock(3)

    agent = IngestionAgent(
        embeddings_client=embeddings_mock,
        repository=repo_mock,
    )

    status, payload = await agent.run(
        "cliente_json_001",
        _onboarding("cliente_json_001", PORTFOLIO_JSON_3_IMOVEIS, "json"),
    )

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"
    assert payload["imoveis_extraidos"] == 3
    assert payload["imoveis_indexados"] == 3

    # Relatório de cobertura: 2 completos, 1 com faltantes
    assert payload["imoveis_completos"] == 2
    assert payload["imoveis_com_campos_faltantes"] == 1

    # O campo faltante no imóvel 3 deve ser "titulo" e "valor"
    detalhes = payload["detalhes_faltantes"]
    assert len(detalhes) == 1
    faltantes_imovel3 = detalhes[0]["campos_faltantes"]
    assert "titulo" in faltantes_imovel3
    assert "valor" in faltantes_imovel3

    # Repositório chamado exatamente uma vez com o client_id correto
    repo_mock.upsert_batch.assert_called_once()
    call_args = repo_mock.upsert_batch.call_args
    assert call_args[0][0] == "cliente_json_001", (
        f"upsert_batch chamado com client_id errado: {call_args[0][0]}"
    )

    # Embeddings gerados em batch para os 3 imóveis
    embeddings_mock.generate_batch.assert_called_once()
    textos = embeddings_mock.generate_batch.call_args[0][0]
    assert len(textos) == 3


# ---------------------------------------------------------------------------
# Teste 2 — Formato CSV: campos faltantes por imóvel no relatório
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_csv_format_missing_fields_report():
    """
    parse_csv deve extrair imóveis e o relatório deve identificar
    corretamente campos faltantes em imóveis incompletos.

    O CSV de teste tem 2 imóveis completos nos campos presentes.
    Como banheiros e vagas não estão no CSV, devem aparecer como presentes
    (None) mas não como "faltantes" (são opcionais).
    Os obrigatórios (titulo, tipo_imovel, tipo_negocio, endereco, valor)
    estão todos presentes → ambos completos.
    """
    imoveis = parse_csv(PORTFOLIO_CSV_2_IMOVEIS)

    assert len(imoveis) == 2

    # Verificar normalização de tipos
    studio = next(im for im in imoveis if "Studio" in (im.get("titulo") or ""))
    assert studio["tipo_negocio"] == "aluguel"
    assert studio["area_m2"] == 45.0      # str→float
    assert studio["quartos"] == 1         # str→int
    assert studio["valor"] == 12000.0     # str→float

    # Campos faltantes: banheiros e vagas são opcionais — não devem aparecer
    faltantes_studio = _campos_faltantes(studio)
    assert faltantes_studio == [], (
        f"Studio não deveria ter campos obrigatórios faltantes: {faltantes_studio}"
    )

    # Cobertura de banheiros (opcional): deve ser None pois não estava no CSV
    assert studio.get("banheiros") is None

    # Agora testa o agente completo com CSV
    embeddings_mock = _make_embeddings_mock(2)
    repo_mock = _make_repository_mock(2)
    agent = IngestionAgent(embeddings_mock, repo_mock)

    status, payload = await agent.run(
        "cliente_csv_002",
        _onboarding("cliente_csv_002", PORTFOLIO_CSV_2_IMOVEIS, "csv"),
    )

    assert status == "done"
    assert payload["imoveis_extraidos"] == 2
    assert payload["imoveis_completos"] == 2
    assert payload["cobertura_percentual"] == 100.0


# ---------------------------------------------------------------------------
# Teste 3 — Formato Excel: parse via openpyxl mockado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_excel_format_with_mocked_openpyxl():
    """
    O parser Excel usa openpyxl. Mockamos a abertura do workbook para
    simular 2 imóveis sem precisar de um arquivo .xlsx real.
    Verifica que aliases de coluna (price, area, rooms) são normalizados.
    """
    # Simula planilha com cabeçalhos em inglês (aliases)
    mock_rows = [
        # Cabeçalhos
        ("name", "type", "negocio", "address", "price", "area", "rooms", "bathrooms"),
        # Imóvel 1
        ("Penthouse Faria Lima", "cobertura", "venda",
         "Av. Faria Lima 3500, Itaim, SP", 6000000, 450, 4, 6),
        # Imóvel 2 — com valor None (campo faltante)
        ("Flat Jardins", "studio", "aluguel",
         "Rua Oscar Freire 900, Jardins, SP", 15000, 65, 1, 1),
    ]

    mock_ws = MagicMock()
    mock_ws.iter_rows.return_value = iter(mock_rows)

    mock_wb = MagicMock()
    mock_wb.active = mock_ws
    mock_wb.__enter__ = MagicMock(return_value=mock_wb)
    mock_wb.__exit__ = MagicMock(return_value=False)

    # Conteúdo fake de bytes (parse_excel usa io.BytesIO internamente)
    fake_excel_bytes = b"PK\x03\x04fake_xlsx_content"

    embeddings_mock = _make_embeddings_mock(2)
    repo_mock = _make_repository_mock(2)
    agent = IngestionAgent(embeddings_mock, repo_mock)

    with patch("openpyxl.load_workbook", return_value=mock_wb):
        status, payload = await agent.run(
            "cliente_excel_003",
            _onboarding("cliente_excel_003", fake_excel_bytes, "excel"),
        )

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"
    assert payload["imoveis_extraidos"] == 2

    # Verifica que os aliases foram normalizados
    # (name→titulo, price→valor, area→area_m2, rooms→quartos, address→endereco)
    repo_mock.upsert_batch.assert_called_once()
    records = repo_mock.upsert_batch.call_args[0][1]
    assert len(records) == 2

    # Metadados do primeiro imóvel devem ter titulo e valor normalizados
    penthouse_meta = records[0]["metadata"]
    assert penthouse_meta.get("titulo") == "Penthouse Faria Lima"
    assert penthouse_meta.get("valor") == 6000000.0
    assert penthouse_meta.get("area_m2") == 450.0
    assert penthouse_meta.get("quartos") == 4


# ---------------------------------------------------------------------------
# Teste 4 — Formato PDF: extração via Claude Haiku mockado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_pdf_format_with_mocked_haiku():
    """
    O parser PDF usa pdfplumber para extrair texto e Claude Haiku para
    estruturar os dados. Ambos são mockados — nenhuma chamada real é feita.
    Verifica que o JSON retornado pelo mock do Haiku é corretamente processado.
    """
    # JSON que o Haiku "retornaria"
    haiku_response_json = json.dumps([
        {
            "titulo": "Mansão Morumbi",
            "tipo_imovel": "casa",
            "tipo_negocio": "venda",
            "endereco": "Rua das Flores, 100, Morumbi, São Paulo, SP",
            "bairro": "Morumbi",
            "cidade": "São Paulo",
            "estado": "SP",
            "valor": 12000000,
            "area_m2": 1200,
            "quartos": 6,
            "banheiros": 8,
            "vagas": 6,
            "descricao": "Mansão com piscina olímpica e quadra de tênis.",
            "caracteristicas": ["piscina olímpica", "quadra de tênis", "heliponto"],
        }
    ])

    # Mock do anthropic client
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=haiku_response_json)]

    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create.return_value = mock_message

    # Mock do pdfplumber (página com texto)
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Mansão Morumbi - Portfólio Premium..."

    mock_pdf = MagicMock()
    mock_pdf.pages = [mock_page]
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)

    embeddings_mock = _make_embeddings_mock(1)
    repo_mock = _make_repository_mock(1)

    agent = IngestionAgent(
        embeddings_client=embeddings_mock,
        repository=repo_mock,
        anthropic_client=mock_anthropic,
    )

    fake_pdf_bytes = b"%PDF-1.4 fake content"

    with patch("pdfplumber.open", return_value=mock_pdf):
        status, payload = await agent.run(
            "cliente_pdf_004",
            _onboarding("cliente_pdf_004", fake_pdf_bytes, "pdf"),
        )

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"
    assert payload["imoveis_extraidos"] == 1
    assert payload["imoveis_completos"] == 1
    assert payload["cobertura_percentual"] == 100.0

    # Verifica que Claude Haiku foi chamado com o texto do PDF
    mock_anthropic.messages.create.assert_called_once()
    call_kwargs = mock_anthropic.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
    prompt_content = call_kwargs["messages"][0]["content"]
    assert "Mansão Morumbi" in prompt_content


# ---------------------------------------------------------------------------
# Teste 5 — Isolamento de namespace entre clientes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namespace_isolation_between_clients():
    """
    Dois clientes executando ingestão na mesma instância do agente NÃO devem
    interferir um no namespace do outro.

    Verifica que:
    - upsert_batch é chamado com client_id correto para cada cliente
    - os registros de cliente A não aparecem na chamada do cliente B
    """
    # Portfólio simples para cada cliente
    portfolio_a = json.dumps([{
        "titulo": "Imóvel Cliente A",
        "tipo_imovel": "apartamento",
        "tipo_negocio": "venda",
        "endereco": "Rua A, 1",
        "valor": 1000000,
    }])
    portfolio_b = json.dumps([{
        "titulo": "Imóvel Cliente B",
        "tipo_imovel": "casa",
        "tipo_negocio": "aluguel",
        "endereco": "Rua B, 2",
        "valor": 8000,
    }])

    embeddings_mock = AsyncMock()
    embeddings_mock.generate_batch.return_value = [[0.5] * 1536]

    repo_mock = AsyncMock()
    repo_mock.upsert_batch.side_effect = lambda cid, recs: len(recs)

    agent = IngestionAgent(embeddings_mock, repo_mock)

    # Executa ingestão para dois clientes
    status_a, payload_a = await agent.run(
        "cliente_isolamento_A",
        _onboarding("cliente_isolamento_A", portfolio_a, "json"),
    )
    status_b, payload_b = await agent.run(
        "cliente_isolamento_B",
        _onboarding("cliente_isolamento_B", portfolio_b, "json"),
    )

    assert status_a == "done"
    assert status_b == "done"

    # upsert_batch deve ter sido chamado 2 vezes — uma para cada cliente
    assert repo_mock.upsert_batch.call_count == 2

    calls = repo_mock.upsert_batch.call_args_list

    # Cada chamada usa o client_id do seu cliente — sem contaminação cruzada
    client_ids_usados = [c[0][0] for c in calls]
    assert "cliente_isolamento_A" in client_ids_usados
    assert "cliente_isolamento_B" in client_ids_usados

    # Registros de A não aparecem na chamada de B e vice-versa
    records_a = next(c[0][1] for c in calls if c[0][0] == "cliente_isolamento_A")
    records_b = next(c[0][1] for c in calls if c[0][0] == "cliente_isolamento_B")

    titulos_a = [r["metadata"].get("titulo") for r in records_a]
    titulos_b = [r["metadata"].get("titulo") for r in records_b]

    assert "Imóvel Cliente A" in titulos_a
    assert "Imóvel Cliente B" not in titulos_a
    assert "Imóvel Cliente B" in titulos_b
    assert "Imóvel Cliente A" not in titulos_b
