"""
tools/places_api.py — Wrapper da Google Places API (Nearby Search).

Usado pelo agente de contexto para buscar estabelecimentos próximos
a um imóvel: escolas, supermercados, hospitais, etc.

Timeout: 5s (hard). Fallback explícito em caso de timeout ou erro HTTP.
O consulttor nunca bloqueia esperando uma API externa — retorna o fallback
e sinaliza ao caller que os dados são estimados.

Tipos de place aceitos pela API (exemplos):
  school, supermarket, hospital, pharmacy, bank, gym, restaurant,
  shopping_mall, park, subway_station, bus_station

Uso:
    client = PlacesAPIClient(api_key=os.environ["GOOGLE_PLACES_API_KEY"])
    result = await client.buscar_vizinhanca(-23.5632, -46.6542, "school")
    if result["status"] == "ok":
        for lugar in result["lugares"]:
            print(lugar["nome"], lugar["rating"])
"""

from __future__ import annotations

import logging
from typing import TypedDict

import httpx

logger = logging.getLogger(__name__)

PLACES_API_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
API_TIMEOUT_SECONDS = 5.0
DEFAULT_RADIUS_M = 2000  # 2km — raio padrão para buscas de vizinhança


# ---------------------------------------------------------------------------
# Tipos de retorno
# ---------------------------------------------------------------------------


class Lugar(TypedDict):
    nome: str | None
    rating: float | None
    total_ratings: int | None
    endereco: str | None
    tipos: list[str]
    lugar_id: str | None
    aberto_agora: bool | None
    latitude: float | None
    longitude: float | None


class BuscarVizinhancaResult(TypedDict):
    lugares: list[Lugar]
    status: str          # "ok" | "zero_results" | "timeout" | "error"
    total: int
    error: str | None    # None quando status == "ok"


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


class PlacesAPIClient:
    """
    Cliente assíncrono para Google Places Nearby Search API.

    Args:
        api_key: Google Places API key.
        timeout: Timeout em segundos para cada request (default: 5s).
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = API_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def buscar_vizinhanca(
        self,
        lat: float,
        lng: float,
        tipo: str,
        raio_m: int = DEFAULT_RADIUS_M,
        max_results: int = 10,
    ) -> BuscarVizinhancaResult:
        """
        Busca estabelecimentos de um tipo específico próximos a uma coordenada.

        Args:
            lat: Latitude do imóvel (ex: -23.5632).
            lng: Longitude do imóvel (ex: -46.6542).
            tipo: Tipo de estabelecimento (ex: "school", "supermarket").
            raio_m: Raio de busca em metros (default: 2000m = 2km).
            max_results: Máximo de resultados a retornar.

        Returns:
            BuscarVizinhancaResult com status e lista de lugares.
            Em caso de timeout ou erro, retorna status != "ok" e lista vazia.
        """
        params = {
            "location": f"{lat},{lng}",
            "radius": raio_m,
            "type": tipo,
            "key": self._api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(PLACES_API_URL, params=params)
                response.raise_for_status()
                data = response.json()

            api_status = data.get("status", "UNKNOWN")

            if api_status == "ZERO_RESULTS":
                return {
                    "lugares": [],
                    "status": "zero_results",
                    "total": 0,
                    "error": None,
                }

            if api_status != "OK":
                logger.warning(
                    "Places API retornou status inesperado '%s' para tipo='%s' lat=%s lng=%s",
                    api_status,
                    tipo,
                    lat,
                    lng,
                )
                return self._fallback(f"Places API status: {api_status}")

            lugares = [
                self._normalizar_lugar(p)
                for p in data.get("results", [])[:max_results]
            ]
            logger.debug(
                "Places API: %d resultados para tipo='%s' em (%.4f, %.4f) r=%dm",
                len(lugares),
                tipo,
                lat,
                lng,
                raio_m,
            )
            return {
                "lugares": lugares,
                "status": "ok",
                "total": len(lugares),
                "error": None,
            }

        except httpx.TimeoutException:
            logger.warning(
                "Timeout (%.1fs) em Places API para tipo='%s' lat=%s lng=%s",
                self._timeout,
                tipo,
                lat,
                lng,
            )
            return self._fallback(f"Timeout após {self._timeout}s na Google Places API")

        except httpx.HTTPStatusError as exc:
            return self._fallback(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")

        except httpx.HTTPError as exc:
            return self._fallback(f"Erro de conexão com Places API: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalizar_lugar(place: dict) -> Lugar:
        geometry = place.get("geometry", {}).get("location", {})
        return {
            "nome": place.get("name"),
            "rating": place.get("rating"),
            "total_ratings": place.get("user_ratings_total"),
            "endereco": place.get("vicinity"),
            "tipos": place.get("types", []),
            "lugar_id": place.get("place_id"),
            "aberto_agora": place.get("opening_hours", {}).get("open_now"),
            "latitude": geometry.get("lat"),
            "longitude": geometry.get("lng"),
        }

    @staticmethod
    def _fallback(reason: str) -> BuscarVizinhancaResult:
        return {
            "lugares": [],
            "status": "error" if "Timeout" not in reason else "timeout",
            "total": 0,
            "error": reason,
        }
