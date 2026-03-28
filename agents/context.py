"""
agents/context.py — Agente 6: Contexto de Vizinhança

Responsabilidades:
  1. Configurar as tools de dados externos para a região do cliente
  2. Validar `buscar_vizinhanca` e `calcular_trajeto` contra um endereço de teste
  3. Retornar status: done apenas se AMBAS as validações passarem
  4. Retornar status: blocked com diagnóstico claro se alguma falhar

As tools implementadas aqui são as que o consultor de luxo usa em produção
para responder perguntas sobre vizinhança em tempo real.

Fallback explícito: timeout de 5s em cada chamada — nunca bloqueia o pipeline.
Validação obrigatória: sem validação bem-sucedida, o consultor não vai a ar.

Uso standalone:
    agent = ContextAgent(places_client, distance_client)
    status, payload = await agent.run("cliente_001", onboarding)

Uso com orquestrador:
    orchestrator = OrchestratorAgent(board, pubsub, mock_agents={
        "context": context_agent.run,
    })
"""

from __future__ import annotations

import logging
from typing import Any

from tools.distance_api import DistanceMatrixClient, TrajetoResult
from tools.places_api import BuscarVizinhancaResult, PlacesAPIClient

logger = logging.getLogger(__name__)

# Tipos de vizinhança validados obrigatoriamente na inicialização
TIPOS_VALIDACAO: list[str] = ["school", "supermarket"]

# Modo de transporte padrão para validação
MODO_VALIDACAO_DEFAULT = "driving"

# Destino fixo de teste para Distance Matrix (Av. Paulista — referência em SP)
DESTINO_TESTE_DEFAULT = "Avenida Paulista, 1000, São Paulo, SP"


# ---------------------------------------------------------------------------
# Funções de tool (chamadas pelo consultor em produção)
# ---------------------------------------------------------------------------


async def buscar_vizinhanca(
    lat: float,
    lng: float,
    tipo: str,
    places_client: PlacesAPIClient,
    raio_m: int = 2000,
) -> BuscarVizinhancaResult:
    """
    Busca estabelecimentos próximos a um imóvel.

    Interface pública usada pelo consultor de luxo durante conversas.

    Args:
        lat, lng: Coordenadas do imóvel.
        tipo: Tipo de estabelecimento (school, supermarket, hospital, etc.)
        places_client: Cliente configurado com a API key do cliente.
        raio_m: Raio de busca (default: 2km).

    Returns:
        BuscarVizinhancaResult — nunca levanta exceção (fallback interno).
    """
    return await places_client.buscar_vizinhanca(lat, lng, tipo, raio_m)


async def calcular_trajeto(
    origem: str,
    destino: str,
    modo: str,
    distance_client: DistanceMatrixClient,
) -> TrajetoResult:
    """
    Calcula tempo e distância entre dois pontos.

    Interface pública usada pelo consultor de luxo durante conversas.

    Args:
        origem: Endereço ou coordenada "lat,lng" de origem.
        destino: Endereço ou coordenada de destino.
        modo: "driving" | "walking" | "bicycling" | "transit"
        distance_client: Cliente configurado com a API key do cliente.

    Returns:
        TrajetoResult — nunca levanta exceção (fallback interno).
    """
    return await distance_client.calcular_trajeto(origem, destino, modo)


# ---------------------------------------------------------------------------
# ContextAgent
# ---------------------------------------------------------------------------


class ContextAgent:
    """
    Agente 6 — Configuração e validação de contexto de vizinhança.

    Valida as tools de Google Maps para a localização do cliente antes
    de permitir que o consultor entre em produção.

    Args:
        places_client: PlacesAPIClient configurado para o cliente.
        distance_client: DistanceMatrixClient configurado para o cliente.
    """

    def __init__(
        self,
        places_client: PlacesAPIClient,
        distance_client: DistanceMatrixClient,
    ) -> None:
        self.places = places_client
        self.distance = distance_client

    # ------------------------------------------------------------------
    # Interface pública (compatível com MockAgentFn do orquestrador)
    # ------------------------------------------------------------------

    async def run(
        self,
        client_id: str,
        onboarding: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """
        Valida as tools de vizinhança para a localização do portfólio do cliente.

        Lê onboarding["endereco_teste"] para o ponto de validação.
        Se não houver, usa coordenadas padrão de São Paulo.

        Returns:
            ("done", payload) se ambas as validações passarem, ou
            ("blocked", {"error": str}) se alguma falhar.
        """
        endereco_teste = onboarding.get("endereco_teste", {})
        lat = float(endereco_teste.get("lat", -23.5632))
        lng = float(endereco_teste.get("lng", -46.6542))
        endereco_str = endereco_teste.get("endereco", f"{lat},{lng}")

        logger.info(
            "[Context] Iniciando validação de tools para cliente '%s' — ponto: (%.4f, %.4f)",
            client_id,
            lat,
            lng,
        )

        resultados_places: dict[str, dict] = {}
        resultados_distance: dict[str, dict] = {}
        falhas: list[str] = []

        # 1. Valida buscar_vizinhanca para cada tipo obrigatório
        for tipo in TIPOS_VALIDACAO:
            resultado = await buscar_vizinhanca(lat, lng, tipo, self.places)
            resultados_places[tipo] = {
                "status": resultado["status"],
                "total": resultado["total"],
                "error": resultado.get("error"),
            }

            if resultado["status"] not in ("ok", "zero_results"):
                falha_msg = (
                    f"buscar_vizinhanca(tipo='{tipo}') falhou: "
                    f"{resultado.get('error', resultado['status'])}"
                )
                falhas.append(falha_msg)
                logger.warning("[Context] %s para cliente '%s'", falha_msg, client_id)

        # 2. Valida calcular_trajeto (origem: endereço do portfólio → destino de teste)
        destino_teste = onboarding.get("destino_validacao", DESTINO_TESTE_DEFAULT)
        trajeto = await calcular_trajeto(
            endereco_str, destino_teste, MODO_VALIDACAO_DEFAULT, self.distance
        )
        resultados_distance["validacao"] = {
            "status": trajeto["status"],
            "duracao_texto": trajeto.get("duracao_texto"),
            "distancia_texto": trajeto.get("distancia_texto"),
            "error": trajeto.get("error"),
        }

        if trajeto["status"] not in ("ok", "not_found"):
            falha_msg = (
                f"calcular_trajeto falhou: "
                f"{trajeto.get('error', trajeto['status'])}"
            )
            falhas.append(falha_msg)
            logger.warning("[Context] %s para cliente '%s'", falha_msg, client_id)

        # 3. Decide status final
        if falhas:
            logger.error(
                "[Context] Validação falhou para cliente '%s'. Falhas: %s",
                client_id,
                falhas,
            )
            return "blocked", {
                "error": f"Validação de tools falhou: {'; '.join(falhas)}",
                "detalhes_places": resultados_places,
                "detalhes_distance": resultados_distance,
            }

        payload = {
            "client_id": client_id,
            "ponto_validado": {"lat": lat, "lng": lng, "endereco": endereco_str},
            "google_places": {
                "status": "validated",
                "tipos_validados": TIPOS_VALIDACAO,
                "resultados": resultados_places,
            },
            "google_distance_matrix": {
                "status": "validated",
                "resultado": resultados_distance["validacao"],
            },
            "tools_disponiveis": [
                "buscar_vizinhanca(lat, lng, tipo)",
                "calcular_trajeto(origem, destino, modo)",
            ],
        }

        logger.info(
            "[Context] ✓ Validação concluída para cliente '%s'. "
            "Places: %s tipos OK. Distance Matrix: %s.",
            client_id,
            len(TIPOS_VALIDACAO),
            trajeto.get("duracao_texto", "ok"),
        )
        return "done", payload

    # ------------------------------------------------------------------
    # Acesso às tools para uso em produção (pelo consultor)
    # ------------------------------------------------------------------

    async def vizinhanca(
        self,
        lat: float,
        lng: float,
        tipo: str,
        raio_m: int = 2000,
    ) -> BuscarVizinhancaResult:
        """Proxy para buscar_vizinhanca — usado pelo consultor em produção."""
        return await buscar_vizinhanca(lat, lng, tipo, self.places, raio_m)

    async def trajeto(
        self,
        origem: str,
        destino: str,
        modo: str = "driving",
    ) -> TrajetoResult:
        """Proxy para calcular_trajeto — usado pelo consultor em produção."""
        return await calcular_trajeto(origem, destino, modo, self.distance)
