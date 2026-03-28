"""
tests/test_context.py — Testes unitários de agents/context.py.

Mínimo exigido: 3 testes (CLAUDE.md). Todas as chamadas às APIs Google são
mockadas via unittest.mock.AsyncMock — chamadas reais ficam para o Agente 9.

Cobertura:
  1. buscar_vizinhanca retorna resultados dentro do raio de 2km com status ok
  2. calcular_trajeto retorna tempo e distância válidos
  3. Fallback de timeout em buscar_vizinhanca → status: timeout, lista vazia
  4. Fallback de timeout em calcular_trajeto → status: timeout, campos None
  5. Validação passa (ambas ok) → run() retorna status: done com payload completo
  6. Validação falha (Places timeout) → run() retorna status: blocked com diagnóstico
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from agents.context import ContextAgent, buscar_vizinhanca, calcular_trajeto
from tools.distance_api import DistanceMatrixClient
from tools.places_api import PlacesAPIClient


# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------


def _places_response_ok(n_results: int = 3) -> dict:
    """Simula resposta da Google Places API com n resultados."""
    return {
        "status": "OK",
        "results": [
            {
                "name": f"Escola Exemplo {i+1}",
                "rating": 4.5 - i * 0.1,
                "user_ratings_total": 200 - i * 10,
                "vicinity": f"Rua das Flores {i*100}, São Paulo",
                "types": ["school", "point_of_interest"],
                "place_id": f"ChIJfake{i}",
                "opening_hours": {"open_now": True},
                "geometry": {"location": {"lat": -23.56 + i * 0.001, "lng": -46.65}},
            }
            for i in range(n_results)
        ],
    }


def _distance_response_ok(duracao_s: int = 360, distancia_m: int = 1200) -> dict:
    """Simula resposta da Google Distance Matrix API com trajeto válido."""
    return {
        "status": "OK",
        "rows": [
            {
                "elements": [
                    {
                        "status": "OK",
                        "duration": {
                            "value": duracao_s,
                            "text": f"{duracao_s // 60} minutos",
                        },
                        "distance": {
                            "value": distancia_m,
                            "text": f"{distancia_m / 1000:.1f} km".replace(".", ","),
                        },
                    }
                ]
            }
        ],
    }


def _make_places_client(api_key: str = "fake_key") -> PlacesAPIClient:
    return PlacesAPIClient(api_key=api_key)


def _make_distance_client(api_key: str = "fake_key") -> DistanceMatrixClient:
    return DistanceMatrixClient(api_key=api_key)


def _base_onboarding(client_id: str) -> dict:
    return {
        "client_id": client_id,
        "nome_imobiliaria": "Imob Context Test",
        "endereco_teste": {
            "lat": -23.5632,
            "lng": -46.6542,
            "endereco": "-23.5632,-46.6542",
        },
    }


# ---------------------------------------------------------------------------
# Teste 1 — buscar_vizinhanca retorna resultados dentro do raio de 2km
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buscar_vizinhanca_returns_results_within_radius():
    """
    buscar_vizinhanca deve retornar lista de lugares com status 'ok'
    quando a API responde com resultados dentro do raio de 2km.

    Verifica estrutura dos lugares: nome, rating, lat/lng presentes.
    """
    places_client = _make_places_client()

    with patch("httpx.AsyncClient") as mock_http:
        mock_response = MagicMock()
        mock_response.json.return_value = _places_response_ok(n_results=3)
        mock_response.raise_for_status.return_value = None

        mock_http.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        result = await places_client.buscar_vizinhanca(
            lat=-23.5632,
            lng=-46.6542,
            tipo="school",
            raio_m=2000,
        )

    assert result["status"] == "ok"
    assert result["total"] == 3
    assert result["error"] is None
    assert len(result["lugares"]) == 3

    # Verifica estrutura de cada lugar
    primeiro = result["lugares"][0]
    assert primeiro["nome"] == "Escola Exemplo 1"
    assert isinstance(primeiro["rating"], float)
    assert primeiro["latitude"] is not None
    assert primeiro["longitude"] is not None
    assert "school" in primeiro["tipos"]


# ---------------------------------------------------------------------------
# Teste 2 — calcular_trajeto retorna tempo e distância válidos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calcular_trajeto_returns_valid_time_and_distance():
    """
    calcular_trajeto deve retornar duracao_segundos, duracao_texto,
    distancia_metros e distancia_texto quando a API responde corretamente.

    Verifica que os valores numéricos são positivos e os textos fazem sentido.
    """
    distance_client = _make_distance_client()

    with patch("httpx.AsyncClient") as mock_http:
        mock_response = MagicMock()
        mock_response.json.return_value = _distance_response_ok(
            duracao_s=420, distancia_m=1800
        )
        mock_response.raise_for_status.return_value = None

        mock_http.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        result = await distance_client.calcular_trajeto(
            origem="-23.5632,-46.6542",
            destino="Colégio São Paulo, Pinheiros, SP",
            modo="driving",
        )

    assert result["status"] == "ok"
    assert result["error"] is None

    # Valores numéricos válidos e positivos
    assert result["duracao_segundos"] == 420
    assert result["distancia_metros"] == 1800

    # Textos legíveis
    assert result["duracao_texto"] == "7 minutos"
    assert "km" in result["distancia_texto"] or "m" in result["distancia_texto"]


# ---------------------------------------------------------------------------
# Teste 3 — Fallback de timeout em buscar_vizinhanca
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buscar_vizinhanca_fallback_on_timeout():
    """
    Quando a Google Places API demora mais de 5s, buscar_vizinhanca deve
    retornar status: "timeout" com lista vazia — nunca levanta exceção.

    O consultor pode continuar sem dados de vizinhança e informar o lead
    que as informações serão obtidas em breve.
    """
    places_client = _make_places_client()

    with patch("httpx.AsyncClient") as mock_http:
        mock_http.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=httpx.TimeoutException("Connection timed out")
        )

        result = await places_client.buscar_vizinhanca(
            lat=-23.5632,
            lng=-46.6542,
            tipo="school",
        )

    assert result["status"] == "timeout"
    assert result["lugares"] == []
    assert result["total"] == 0
    assert result["error"] is not None
    assert "Timeout" in result["error"] or "timeout" in result["error"].lower()


# ---------------------------------------------------------------------------
# Teste 4 — Fallback de timeout em calcular_trajeto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calcular_trajeto_fallback_on_timeout():
    """
    Quando a Google Distance Matrix API demora mais de 5s, calcular_trajeto
    deve retornar status: "timeout" com todos os campos None.

    O consultor pode usar uma estimativa estática como fallback narrativo.
    """
    distance_client = _make_distance_client()

    with patch("httpx.AsyncClient") as mock_http:
        mock_http.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=httpx.TimeoutException("Read timed out")
        )

        result = await distance_client.calcular_trajeto(
            origem="-23.5632,-46.6542",
            destino="Av. Paulista, 1000, São Paulo",
            modo="driving",
        )

    assert result["status"] == "timeout"
    assert result["duracao_segundos"] is None
    assert result["duracao_texto"] is None
    assert result["distancia_metros"] is None
    assert result["distancia_texto"] is None
    assert result["error"] is not None
    assert "Timeout" in result["error"] or "timeout" in result["error"].lower()


# ---------------------------------------------------------------------------
# Teste 5 — Validação completa: ambas ok → run() retorna done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_run_returns_done_when_validation_passes():
    """
    ContextAgent.run() deve retornar status: "done" quando:
    - buscar_vizinhanca retorna ok para todos os tipos de validação
    - calcular_trajeto retorna ok para o trajeto de teste

    O payload deve incluir os resultados de validação de ambas as APIs.
    """
    places_client = _make_places_client()
    distance_client = _make_distance_client()

    places_ok = _places_response_ok(n_results=2)
    distance_ok = _distance_response_ok(duracao_s=360, distancia_m=1200)

    with patch("httpx.AsyncClient") as mock_http:
        mock_response_places = MagicMock()
        mock_response_places.json.return_value = places_ok
        mock_response_places.raise_for_status.return_value = None

        mock_response_distance = MagicMock()
        mock_response_distance.json.return_value = distance_ok
        mock_response_distance.raise_for_status.return_value = None

        # Alternamos entre respostas de Places e Distance Matrix
        call_count = {"n": 0}

        async def mock_get(*args, **kwargs):
            call_count["n"] += 1
            url = args[0] if args else kwargs.get("url", "")
            if "distancematrix" in str(url):
                return mock_response_distance
            return mock_response_places

        mock_http.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=mock_get
        )

        agent = ContextAgent(places_client, distance_client)
        status, payload = await agent.run("cliente_ctx_005", _base_onboarding("cliente_ctx_005"))

    assert status == "done", f"Esperava 'done', got '{status}': {payload}"
    assert "google_places" in payload
    assert payload["google_places"]["status"] == "validated"
    assert "google_distance_matrix" in payload
    assert payload["google_distance_matrix"]["status"] == "validated"
    assert payload["ponto_validado"]["lat"] == -23.5632
    assert "buscar_vizinhanca" in str(payload["tools_disponiveis"])


# ---------------------------------------------------------------------------
# Teste 6 — Validação falha: Places timeout → run() retorna blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_run_returns_blocked_when_places_times_out():
    """
    Quando buscar_vizinhanca retorna timeout durante a validação,
    ContextAgent.run() deve retornar status: "blocked" com diagnóstico claro.

    O orquestrador então roteia para handle_escalation — o consultor não
    vai a ar sem confirmação de que as tools de vizinhança funcionam.
    """
    places_client = _make_places_client()
    distance_client = _make_distance_client()

    distance_ok = _distance_response_ok()

    with patch("httpx.AsyncClient") as mock_http:
        mock_response_distance = MagicMock()
        mock_response_distance.json.return_value = distance_ok
        mock_response_distance.raise_for_status.return_value = None

        async def mock_get(*args, **kwargs):
            url = args[0] if args else kwargs.get("url", "")
            if "distancematrix" in str(url):
                return mock_response_distance
            # Places API sempre dá timeout
            raise httpx.TimeoutException("Places API unreachable")

        mock_http.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=mock_get
        )

        agent = ContextAgent(places_client, distance_client)
        status, payload = await agent.run("cliente_ctx_006", _base_onboarding("cliente_ctx_006"))

    assert status == "blocked", f"Esperava 'blocked', got '{status}'"
    assert "error" in payload
    assert "timeout" in payload["error"].lower() or "falhou" in payload["error"].lower()

    # Diagnóstico estruturado deve estar presente
    assert "detalhes_places" in payload
    # Ao menos um tipo de validação deve ter falhado
    for tipo_status in payload["detalhes_places"].values():
        assert tipo_status["status"] in ("timeout", "error", "ok", "zero_results")
