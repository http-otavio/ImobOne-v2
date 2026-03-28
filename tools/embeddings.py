"""
tools/embeddings.py — Cliente de embeddings via OpenAI text-embedding-3-small.

Dimensão: 1536 (compatível com pgvector VECTOR(1536) no Supabase).
Usado pelo agente de ingestão para indexar o portfólio de imóveis.

Uso:
    client = EmbeddingsClient(api_key=os.environ["OPENAI_API_KEY"])
    vec = await client.generate("Cobertura 4 quartos Itaim Bibi R$4.5M")
    vecs = await client.generate_batch(["texto 1", "texto 2"])
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import openai

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536  # dimensão fixa do text-embedding-3-small


# ---------------------------------------------------------------------------
# Protocol — para injeção em testes sem dependência concreta
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingsClientProtocol(Protocol):
    async def generate(self, text: str) -> list[float]: ...
    async def generate_batch(self, texts: list[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# Implementação real
# ---------------------------------------------------------------------------


class EmbeddingsClient:
    """
    Cliente assíncrono de embeddings usando OpenAI text-embedding-3-small.

    Args:
        api_key: Chave da OpenAI API (OPENAI_API_KEY).
        model: Modelo de embedding. Default: text-embedding-3-small.
    """

    def __init__(
        self,
        api_key: str,
        model: str = EMBEDDING_MODEL,
    ) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self._model = model

    async def generate(self, text: str) -> list[float]:
        """
        Gera embedding para um único texto.

        Args:
            text: Texto a vetorizar (máx. ~8000 tokens para text-embedding-3-small).

        Returns:
            Lista de 1536 floats representando o embedding.
        """
        text = text.replace("\n", " ").strip()
        if not text:
            raise ValueError("Texto vazio não pode ser vetorizado.")

        response = await self._client.embeddings.create(
            input=text,
            model=self._model,
        )
        return response.data[0].embedding

    async def generate_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Gera embeddings em batch — mais eficiente que chamadas individuais.

        Args:
            texts: Lista de textos (OpenAI processa em batch internamente).

        Returns:
            Lista de embeddings na mesma ordem dos textos de entrada.
        """
        if not texts:
            return []

        cleaned = [t.replace("\n", " ").strip() for t in texts]

        response = await self._client.embeddings.create(
            input=cleaned,
            model=self._model,
        )

        # Garante ordem correta (OpenAI pode retornar fora de ordem por index)
        ordered = sorted(response.data, key=lambda d: d.index)
        return [d.embedding for d in ordered]


def build_imovel_text(imovel: dict) -> str:
    """
    Cria a representação textual de um imóvel para geração de embedding.

    Ordem deliberada: campos mais relevantes para busca semântica primeiro.
    """
    parts = []

    tipo_negocio = imovel.get("tipo_negocio", "")
    tipo_imovel = imovel.get("tipo_imovel", "")
    if tipo_negocio or tipo_imovel:
        parts.append(f"Tipo: {tipo_imovel} | Negócio: {tipo_negocio}")

    if imovel.get("titulo"):
        parts.append(f"Título: {imovel['titulo']}")

    endereco_parts = filter(None, [
        imovel.get("endereco"),
        imovel.get("bairro"),
        imovel.get("cidade"),
        imovel.get("estado"),
    ])
    endereco = ", ".join(endereco_parts)
    if endereco:
        parts.append(f"Endereço: {endereco}")

    metricas = []
    if imovel.get("area_m2"):
        metricas.append(f"{imovel['area_m2']}m²")
    if imovel.get("quartos"):
        metricas.append(f"{imovel['quartos']} quartos")
    if imovel.get("banheiros"):
        metricas.append(f"{imovel['banheiros']} banheiros")
    if imovel.get("vagas"):
        metricas.append(f"{imovel['vagas']} vagas")
    if metricas:
        parts.append(" | ".join(metricas))

    if imovel.get("valor"):
        parts.append(f"Valor: R$ {imovel['valor']:,.0f}".replace(",", "."))

    if imovel.get("descricao"):
        parts.append(f"Descrição: {imovel['descricao'][:500]}")

    caracteristicas = imovel.get("caracteristicas", [])
    if caracteristicas:
        parts.append(f"Características: {', '.join(caracteristicas)}")

    return "\n".join(parts)
