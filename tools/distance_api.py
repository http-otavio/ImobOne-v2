"""
tools/distance_api.py — Wrapper da Google Distance Matrix API.

Usado pelo consultor de luxo para calcular tempos e distâncias reais
entre o imóvel e estabelecimentos de interesse (escola, supermercado, etc.).

Timeout: 5s (hard). Fallback explícito — o consultor nunca trava esperando.

Modos de transporte suportados pela API:
  "driving" | "walking" | "bicycling" | "transit"

Uso:
    client = DistanceMatrixClient(api_key=os.environ["GOOGLE_DISTANCE_MATRIX_API_KEY"])
    result = await client.calcular_trajeto(
        origem="-23.5632,-46.6542",
        destino="Colégio São Luís, São Paulo",
        modo="driving",
    )
    if result["status"] == "ok":
        print(result["duracao_texto"])   # "6 minutos"
        print(result["distancia_texto"]) # "1,2 km"
"""

from __future__ import annotations

import logging
from typing import Literal, TypedDict

import httpx

logger = logging.getLogger(__name__)

DISTANCE_API_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
API_TIMEOUT_SECONDS = 5.0

ModoTransporte = Literal["driving", "walking", "bicycling", "transit"]


# ---------------------------------------------------------------------------
# Tipos de retorno
# ---------------------------------------------------------------------------


class TrajetoResult(TypedDict):
    duracao_segundos: int | None
    duracao_texto: str | None       # ex: "6 minutos"
    distancia_metros: int | None
    distancia_texto: str | None     # ex: "1,2 km"
    status: str                     # "ok" | "not_found" | "timeout" | "error"
    error: str | None


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


class DistanceMatrixClient:
    """
    Cliente assíncrono para Google Distance Matrix API.

    Args:
        api_key: Google Distance Matrix API key.
        timeout: Timeout em segundos (default: 5s).
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = API_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def calcular_trajeto(
        self,
        origem: str,
        destino: str,
        modo: ModoTransporte = "driving",
    ) -> TrajetoResult:
        """
        Calcula tempo e distância entre origem e destino.

        Args:
            origem: Endereço ou coordenada de origem (ex: "-23.5632,-46.6542").
            destino: Endereço ou coordenada de destino (ex: "Av. Paulista, São Paulo").
            modo: Modo de transporte.

        Returns:
            TrajetoResult com duracao, distancia e status.
            Em timeout/erro: status != "ok", campos de duração/distância são None.
        """
        params = {
            "origins": origem,
            "destinations": destino,
            "mode": modo,
            "language": "pt-BR",
            "key": self._api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(DISTANCE_API_URL, params=params)
                response.raise_for_status()
                data = response.json()

            if data.get("status") != "OK":
                return self._fallback(
                    f"Distance Matrix API status: {data.get('status', 'UNKNOWN')}"
                )

            rows = data.get("rows", [])
            if not rows:
                return self._fallback("Nenhuma rota retornada pela Distance Matrix API.")

            element = rows[0].get("elements", [{}])[0]
            elem_status = element.get("status", "UNKNOWN")

            if elem_status == "NOT_FOUND":
                return {
                    "duracao_segundos": None,
                    "duracao_texto": None,
                    "distancia_metros": None,
                    "distancia_texto": None,
                    "status": "not_found",
                    "error": f"Endereço não encontrado: origem='{origem}' destino='{destino}'",
                }

            if elem_status != "OK":
                return self._fallback(f"Elemento com status inesperado: {elem_status}")

            duracao = element.get("duration", {})
            distancia = element.get("distance", {})

            result: TrajetoResult = {
                "duracao_segundos": duracao.get("value"),   # em segundos
                "duracao_texto": duracao.get("text"),       # ex: "6 minutos"
                "distancia_metros": distancia.get("value"), # em metros
                "distancia_texto": distancia.get("text"),   # ex: "1,2 km"
                "status": "ok",
                "error": None,
            }

            logger.debug(
                "Distance Matrix: %s → %s [%s] = %s (%s)",
                origem,
                destino,
                modo,
                result["duracao_texto"],
                result["distancia_texto"],
            )
            return result

        except httpx.TimeoutException:
            logger.warning(
                "Timeout (%.1fs) em Distance Matrix API: origem='%s' destino='%s'",
                self._timeout,
                origem,
                destino,
            )
            return {
                "duracao_segundos": None,
                "duracao_texto": None,
                "distancia_metros": None,
                "distancia_texto": None,
                "status": "timeout",
                "error": f"Timeout após {self._timeout}s na Google Distance Matrix API",
            }

        except httpx.HTTPStatusError as exc:
            return self._fallback(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")

        except httpx.HTTPError as exc:
            return self._fallback(f"Erro de conexão com Distance Matrix API: {exc}")

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback(reason: str) -> TrajetoResult:
        return {
            "duracao_segundos": None,
            "duracao_texto": None,
            "distancia_metros": None,
            "distancia_texto": None,
            "status": "error",
            "error": reason,
        }
