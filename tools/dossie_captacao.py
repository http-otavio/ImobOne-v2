"""
tools/dossie_captacao.py — Dossiê de Captação e Posicionamento de Mercado.

Gera um dossiê completo em Markdown/PDF para proprietários, cruzando:
- POIs de luxo da vizinhança (Places API)
- Comparativos de mercado via pgvector (tipologia + bairro)
- Estratégia de precificação baseada em dados reais

Uso:
    resultado = await gerar_dossie_captacao(
        lat=-23.5632,
        lng=-46.6542,
        tipologia="apartamento",
        bairro="Jardins",
        area_m2=120.0,
        client_id="cli_001",
        corretor_phone="5511999999999",
    )
"""

from __future__ import annotations

import logging
import os
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tipos e contratos
# ---------------------------------------------------------------------------

TIPOS_LUXO = [
    "restaurant",
    "shopping_mall",
    "gym",
    "spa",
    "park",
    "school",
    "hospital",
    "bank",
]


@dataclass
class POI:
    nome: str
    tipo: str
    rating: Optional[float]
    endereco: Optional[str]
    distancia_m: Optional[float] = None


@dataclass
class ImovelComparavel:
    id: str
    tipologia: str
    bairro: str
    area_m2: float
    preco_total: float
    preco_m2: float
    similaridade: float


@dataclass
class DadosDossie:
    lat: float
    lng: float
    tipologia: str
    bairro: str
    area_m2: float
    client_id: str
    corretor_phone: str
    pois: list[POI] = field(default_factory=list)
    comparaveis: list[ImovelComparavel] = field(default_factory=list)
    preco_m2_medio: float = 0.0
    preco_m2_min: float = 0.0
    preco_m2_max: float = 0.0
    markdown: str = ""
    pdf_path: Optional[Path] = None
    asset_url: Optional[str] = None


class PlacesClientProtocol(Protocol):
    async def buscar_vizinhanca(
        self,
        lat: float,
        lng: float,
        tipo: str,
        raio_m: int,
        max_results: int,
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Funções injetáveis (isoladas para facilitar mock)
# ---------------------------------------------------------------------------


async def buscar_pois_luxo(
    lat: float,
    lng: float,
    places_client: Optional[PlacesClientProtocol],
    raio_m: int = 1000,
    max_por_tipo: int = 3,
) -> list[POI]:
    """
    Busca POIs de luxo na vizinhança usando Places API.

    Fallback gracioso: se api_key ausente ou cliente None,
    retorna lista vazia sem lançar exceção.

    Args:
        lat: Latitude do imóvel.
        lng: Longitude do imóvel.
        places_client: Instância de PlacesAPIClient ou None para fallback.
        raio_m: Raio de busca em metros (default: 1000m).
        max_por_tipo: Máximo de resultados por tipo.

    Returns:
        Lista de POIs encontrados (pode ser vazia em caso de fallback).
    """
    if places_client is None:
        logger.warning(
            "Places API client não disponível — POIs não serão buscados (fallback gracioso)"
        )
        return []

    pois: list[POI] = []

    for tipo in TIPOS_LUXO:
        try:
            result = await places_client.buscar_vizinhanca(
                lat=lat,
                lng=lng,
                tipo=tipo,
                raio_m=raio_m,
                max_results=max_por_tipo,
            )

            if result.get("status") != "ok":
                logger.debug(
                    "Places API retornou status '%s' para tipo='%s'",
                    result.get("status"),
                    tipo,
                )
                continue

            for lugar in result.get("lugares", [])[:max_por_tipo]:
                pois.append(
                    POI(
                        nome=lugar.get("nome") or "Sem nome",
                        tipo=tipo,
                        rating=lugar.get("rating"),
                        endereco=lugar.get("endereco"),
                        distancia_m=None,
                    )
                )

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Erro ao buscar tipo='%s' na Places API: %s", tipo, exc
            )
            continue

    logger.info("POIs encontrados: %d", len(pois))
    return pois


async def buscar_comparaveis_pgvector(
    tipologia: str,
    bairro: str,
    area_m2: float,
    db_query_fn: Callable[..., Any],
) -> list[ImovelComparavel]:
    """
    Consulta pgvector para encontrar imóveis comparáveis por tipologia e bairro.

    Args:
        tipologia: Tipo do imóvel (ex: "apartamento").
        bairro: Bairro do imóvel.
        area_m2: Área em m² para filtro de similaridade.
        db_query_fn: Função injetável que executa a query no banco.

    Returns:
        Lista de imóveis comparáveis. Loga warning se base vazia.
    """
    try:
        rows = await db_query_fn(
            tipologia=tipologia,
            bairro=bairro,
            area_m2=area_m2,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Erro ao consultar pgvector: %s", exc)
        return []

    if not rows:
        logger.warning(
            "pgvector: nenhum imóvel comparável encontrado para tipologia='%s', bairro='%s'",
            tipologia,
            bairro,
        )
        return []

    comparaveis: list[ImovelComparavel] = []
    for row in rows:
        try:
            area = float(row.get("area_m2", 1) or 1)
            preco_total = float(row.get("preco_total", 0) or 0)
            preco_m2 = preco_total / area if area > 0 else float(row.get("preco_m2", 0) or 0)

            comparaveis.append(
                ImovelComparavel(
                    id=str(row.get("id", "")),
                    tipologia=str(row.get("tipologia", tipologia)),
                    bairro=str(row.get("bairro", bairro)),
                    area_m2=area,
                    preco_total=preco_total,
                    preco_m2=preco_m2,
                    similaridade=float(row.get("similaridade", 1.0)),
                )
            )
        except (TypeError, ValueError) as exc:
            logger.debug("Linha ignorada por erro de tipo: %s — %s", row, exc)
            continue

    logger.info("Comparáveis encontrados: %d", len(comparaveis))
    return comparaveis


def calcular_estatisticas_preco(
    comparaveis: list[ImovelComparavel],
    area_m2: float,
) -> tuple[float, float, float]:
    """
    Calcula média, mín e máx de preço/m² dos comparáveis.

    Returns:
        Tupla (preco_m2_medio, preco_m2_min, preco_m2_max).
    """
    if not comparaveis:
        return 0.0, 0.0, 0.0

    precos = [c.preco_m2 for c in comparaveis if c.preco_m2 > 0]
    if not precos:
        return 0.0, 0.0, 0.0

    return (
        sum(precos) / len(precos),
        min(precos),
        max(precos),
    )


def renderizar_markdown(dados: DadosDossie) -> str:
    """
    Renderiza o dossiê em Markdown estruturado usando template Jinja2.

    Args:
        dados: Objeto DadosDossie com todas as informações coletadas.

    Returns:
        String Markdown formatada.
    """
    try:
        from jinja2 import Environment, BaseLoader  # type: ignore

        env = Environment(loader=BaseLoader(), autoescape=False)
        template = env.from_string(MARKDOWN_TEMPLATE)
        return template.render(
            dados=dados,
            now=datetime.now().strftime("%d/%m/%Y %H:%M"),
            preco_estimado_min=dados.preco_m2_min * dados.area_m2,
            preco_estimado_medio=dados.preco_m2_medio * dados.area_m2,
            preco_estimado_max=dados.preco_m2_max * dados.area_m2,
        )
    except ImportError:
        logger.warning("Jinja2 não disponível — usando template simples")
        return _renderizar_markdown_simples(dados)


def _renderizar_markdown_simples(dados: DadosDossie) -> str:
    """Fallback de renderização sem Jinja2."""
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    preco_min = dados.preco_m2_min * dados.area_m2
    preco_medio = dados.preco_m2_medio * dados.area_m2
    preco_max = dados.preco_m2_max * dados.area_m2

    linhas_pois = "\n".join(
        f"- **{p.nome}** ({p.tipo})"
        + (f" — ⭐ {p.rating}" if p.rating else "")
        + (f" — {p.endereco}" if p.endereco else "")
        for p in dados.pois
    ) or "_Nenhum POI encontrado na vizinhança._"

    if dados.comparaveis:
        linhas_comp = "\n".join(
            f"| {c.id} | {c.tipologia} | {c.area_m2:.0f} m² | "
            f"R$ {c.preco_m2:,.0f}/m² | {c.similaridade:.2f} |"
            for c in dados.comparaveis
        )
        tabela_comp = (
            "| ID | Tipologia | Área | Preço/m² | Similaridade |\n"
            "|---|---|---|---|---|\n"
            + linhas_comp
        )
    else:
        tabela_comp = "_Base de dados vazia — sem comparáveis disponíveis._"

    return f"""# 📋 Dossiê de Captação — {dados.tipologia.title()} em {dados.bairro}

> Gerado em {now} pelo sistema ImobOne

---

## 1. Resumo do Imóvel

| Campo | Valor |
|---|---|
| Tipologia | {dados.tipologia.title()} |
| Bairro | {dados.bairro} |
| Área | {dados.area_m2:.0f} m² |
| Coordenadas | {dados.lat:.6f}, {dados.lng:.6f} |

---

## 2. Comparativos de Mercado

{tabela_comp}

**Estatísticas de preço/m²:**
- Mínimo: R$ {dados.preco_m2_min:,.0f}/m²
- Médio: R$ {dados.preco_m2_medio:,.0f}/m²
- Máximo: R$ {dados.preco_m2_max:,.0f}/m²

---

## 3. Vizinhança Premium

{linhas_pois}

---

## 4. Estratégia de Precificação

Com base nos **{len(dados.comparaveis)} imóveis comparáveis** analisados:

| Cenário | Preço Estimado |
|---|---|
| Conservador | R$ {preco_min:,.0f} |
| Mercado | R$ {preco_medio:,.0f} |
| Premium | R$ {preco_max:,.0f} |

**Recomendação:** Posicionar o imóvel entre R$ {preco_medio:,.0f} e R$ {preco_max:,.0f},
destacando os **{len(dados.pois)} pontos de interesse premium** na vizinhança.

---

_Dossiê gerado automaticamente pelo ImobOne. Dados sujeitos a validação do corretor._
"""


MARKDOWN_TEMPLATE = """\
# 📋 Dossiê de Captação — {{ dados.tipologia|title }} em {{ dados.bairro }}

> Gerado em {{ now }} pelo sistema ImobOne

---

## 1. Resumo do Imóvel

| Campo | Valor |
|---|---|
| Tipologia | {{ dados.tipologia|title }} |
| Bairro | {{ dados.bairro }} |
| Área | {{ dados.area_m2|int }} m² |
| Coordenadas | {{ "%.6f"|format(dados.lat) }}, {{ "%.6f"|format(dados.lng) }} |

---

## 2. Comparativos de Mercado

{% if dados.comparaveis %}
| ID | Tipologia | Área | Preço/m² | Similaridade |
|---|---|---|---|---|
{% for c in dados.comparaveis %}
| {{ c.id }} | {{ c.tipologia }} | {{ c.area_m2|int }} m² | R$ {{ "%.0f"|format(c.preco_m2) }}/m² | {{ "%.2f"|format(c.similaridade) }} |
{% endfor %}

**Estatísticas de preço/m²:**
- Mínimo: R$ {{ "%.0f"|format(dados.preco_m2_min) }}/m²
- Médio: R$ {{ "%.0f"|format(dados.preco_m2_medio) }}/m²
- Máximo: R$ {{ "%.0f"|format(dados.preco_m2_max) }}/m²
{% else %}
_Base de dados vazia — sem comparáveis disponíveis._
{% endif %}

---

## 3. Vizinhança Premium

{% if dados.pois %}
{% for p in dados.pois %}
- **{{ p.nome }}** ({{ p.tipo }}){% if p.rating %} — ⭐ {{ p.rating }}{% endif %}{% if p.endereco %} — {{ p.endereco }}{% endif %}

{% endfor %}
{% else %}
_Nenhum POI encontrado na vizinhança._
{% endif %}

---

## 4. Estratégia de Precificação

Com base nos **{{ dados.comparaveis|length }} imóveis comparáveis** analisados:

| Cenário | Preço Estimado |
|---|---|
| Conservador | R$ {{ "%.0f"|format(preco_estimado_min) }} |
| Mercado | R$ {{ "%.0f"|format(preco_estimado_medio) }} |
| Premium | R$ {{ "%.0f"|format(preco_estimado_max) }} |

**Recomendação:** Posicionar o imóvel entre R$ {{ "%.0f"|format(preco_estimado_medio) }}
e R$ {{ "%.0f"|format(preco_estimado_max) }}, destacando os **{{ dados.pois|length }} pontos
de interesse premium** na vizinhança.

---

_Dossiê gerado automaticamente pelo ImobOne. Dados sujeitos a validação do corretor._
"""


def gerar_pdf(markdown_content: str, output_path: Path) -> bool:
    """
    Converte Markdown em PDF usando ReportLab.

    Args:
        markdown_content: Conteúdo Markdown a converter.
        output_path: Caminho de destino do PDF.

    Returns:
        True se gerado com sucesso, False em caso de erro.
    """
    try:
        from reportlab.lib.pagesizes import A4  # type: ignore
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # type: ignore
        from reportlab.lib.units import cm  # type: ignore
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer  # type: ignore
        from reportlab.lib import colors  # type: ignore

        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2 * cm,
            bottomMargin=2 * cm,
        )

        styles = getSampleStyleSheet()
        story = []

        heading1 = ParagraphStyle(
            "Heading1Custom",
            parent=styles["Heading1"],
            fontSize=18,
            spaceAfter=12,
            textColor=colors.HexColor("#1a365d"),
        )
        heading2 = ParagraphStyle(
            "Heading2Custom",
            parent=styles["Heading2"],
            fontSize=14,
            spaceAfter=8,
            textColor=colors.HexColor("#2c5282"),
        )
        normal = styles["Normal"]

        for line in markdown_content.split("\n"):
            line_stripped = line.strip()
            if not line_stripped:
                story.append(Spacer(1, 6))
                continue

            if line_stripped.startswith("# "):
                text = line_stripped[2:].replace("📋", "").strip()
                story.append(Paragraph(text, heading1))
            elif line_stripped.startswith("## "):
                text = line_stripped[3:]
                story.append(Paragraph(text, heading2))
            elif line_stripped.startswith("---"):
                story.append(Spacer(1, 12))
            elif line_stripped.startswith("> "):
                text = line_stripped[2:]
                italic_style = ParagraphStyle(
                    "Italic",
                    parent=normal,
                    textColor=colors.HexColor("#718096"),
                )
                story.append(Paragraph(text, italic_style))
            elif line_stripped.startswith("- ") or line_stripped.startswith("* "):
                text = _md_to_reportlab(line_stripped[2:])
                bullet_style = ParagraphStyle(
                    "Bullet",
                    parent=normal,
                    leftIndent=20,
                    bulletIndent=10,
                )
                story.append(Paragraph(f"• {text}", bullet_style))
            elif line_stripped.startswith("|"):
                text = " | ".join(
                    c.strip()
                    for c in line_stripped.split("|")
                    if c.strip() and c.strip() not in ("---", "===")
                )
                if text:
                    story.append(Paragraph(text, normal))
            else:
                text = _md_to_reportlab(line_stripped)
                if text:
                    story.append(Paragraph(text, normal))

        doc.build(story)
        logger.info("PDF gerado em: %s", output_path)
        return True

    except ImportError:
        logger.warning("ReportLab não disponível — PDF não será gerado")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("Erro ao gerar PDF: %s", exc)
        return False


def _md_to_reportlab(text: str) -> str:
    """Converte formatação Markdown básica para tags ReportLab."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"_(.+?)_", r"<i>\1</i>", text)
    return text


def salvar_markdown(content: str, output_path: Path) -> bool:
    """Salva o conteúdo Markdown em arquivo."""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        logger.info("Markdown salvo em: %s", output_path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Erro ao salvar Markdown: %s", exc)
        return False


async def enviar_link_corretor(
    phone: str,
    asset_url: str,
    tipologia: str,
    bairro: str,
    evolution_send_fn: Optional[Callable[..., Any]],
) -> bool:
    """
    Envia link do dossiê ao corretor via Evolution API.

    Args:
        phone: Número do corretor (ex: "5511999999999").
        asset_url: URL ou caminho do dossiê gerado.
        tipologia: Tipologia do imóvel.
        bairro: Bairro do imóvel.
        evolution_send_fn: Função injetável de envio (None para skip).

    Returns:
        True se enviado, False caso contrário.
    """
    if evolution_send_fn is None:
        logger.info("Evolution API não configurada — link não será enviado")
        return False

    mensagem = (
        f"📋 *Dossiê de Captação Gerado*\n\n"
        f"Tipologia: {tipologia.title()}\n"
        f"Bairro: {bairro}\n\n"
        f"Acesse o dossiê completo:\n{asset_url}"
    )

    try:
        await evolution_send_fn(phone=phone, message=mensagem)
        logger.info("Link enviado ao corretor: %s", phone)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Erro ao enviar link ao corretor: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Tool principal
# ---------------------------------------------------------------------------


async def gerar_dossie_captacao(
    lat: float,
    lng: float,
    tipologia: str,
    bairro: str,
    area_m2: float,
    client_id: str,
    corretor_phone: str,
    places_client: Optional[PlacesClientProtocol] = None,
    db_query_fn: Optional[Callable[..., Any]] = None,
    evolution_send_fn: Optional[Callable[..., Any]] = None,
    assets_base_dir: Optional[Path] = None,
    raio_pois_m: int = 1000,
) -> dict[str, Any]:
    """
    Gera dossiê de captação e posicionamento de mercado para proprietários.

    Orquestra:
    1. Busca de POIs de luxo via Places API (fallback gracioso se indisponível)
    2. Query de similaridade no pgvector por tipologia+bairro
    3. Renderização de Markdown via Jinja2 e conversão para PDF via ReportLab
    4. Persistência em clients/{client_id}/assets/ e envio ao corretor

    Args:
        lat: Latitude do imóvel.
        lng: Longitude do imóvel.
        tipologia: Tipologia do imóvel (ex: "apartamento", "casa").
        bairro: Bairro do imóvel.
        area_m2: Área do imóvel em m².
        client_id: ID do cliente/proprietário.
        corretor_phone: Número do corretor para receber o link.
        places_client: Cliente Places API injetável (None = fallback gracioso).
        db_query_fn: Função de query pgvector injetável (None = sem comparáveis).
        evolution_send_fn: Função de envio Evolution API injetável (None = skip).
        assets_base_dir: Diretório base para assets (default: clients/).
        raio_pois_m: Raio de busca de POIs em metros (default: 1000m).

    Returns:
        Dict com chaves:
            - status: "ok" | "error"
            - markdown: conteúdo Markdown gerado
            - pdf_path: caminho do PDF gerado (ou None)
            - asset_url: URL/caminho do asset principal
            - pois_count: número de POIs encontrados
            - comparaveis_count: número de comparáveis encontrados
            - corretor_notificado: bool
            - error: mensagem de erro (ou None)
    """
    logger.info(
        "Iniciando dossiê de captação: tipologia='%s', bairro='%s', client_id='%s'",
        tipologia,
        bairro,
        client_id,
    )

    dados = DadosDossie(
        lat=lat,
        lng=lng,
        tipologia=tipologia,
        bairro=bairro,
        area_m2=area_m2,
        client_id=client_id,
        corretor_phone=corretor_phone,
    )

    # 1. Buscar POIs de luxo
    dados.pois = await buscar_pois_luxo(
        lat=lat,
        lng=lng,
        places_client=places_client,
        raio_m=raio_pois_m,
    )

    # 2. Buscar comparáveis via pgvector
    if db_query_fn is not None:
        dados.comparaveis = await buscar_comparaveis_pgvector(
            tipologia=tipologia,
            bairro=bairro,
            area_m2=area_m2,
            db_query_fn=db_query_fn,
        )
    else:
        logger.warning("db_query_fn não fornecido — comparáveis não serão buscados")

    # 3. Calcular estatísticas de preço
    dados.preco_m2_medio, dados.preco_m2_min, dados.preco_m2_max = (
        calcular_estatisticas_preco(dados.comparaveis, area_m2)
    )

    # 4. Renderizar Markdown
    dados.markdown = renderizar_markdown(dados)

    # 5. Persistir assets
    base_dir = assets_base_dir or Path("clients")
    assets_dir = base_dir / client_id / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = f"dossie_{tipologia}_{bairro}_{timestamp}".replace(" ", "_").lower()

    md_path = assets_dir / f"{slug}.md"
    pdf_path = assets_dir / f"{slug}.pdf"

    salvar_markdown(dados.markdown, md_path)
    pdf_ok = gerar_pdf(dados.markdown, pdf_path)

    dados.pdf_path = pdf_path if pdf_ok else None
    dados.asset_url = str(dados.pdf_path or md_path)

    # 6. Enviar link ao corretor
    corretor_notificado = await enviar_link_corretor(
        phone=corretor_phone,
        asset_url=dados.asset_url,
        tipologia=tipologia,
        bairro=bairro,
        evolution_send_fn=evolution_send_fn,
    )

    logger.info(
        "Dossiê gerado com sucesso: pois=%d, comparaveis=%d, pdf=%s",
        len(dados.pois),
        len(dados.comparaveis),
        pdf_ok,
    )

    return {
        "status": "ok",
        "markdown": dados.markdown,
        "pdf_path": str(dados.pdf_path) if dados.pdf_path else None,
        "asset_url": dados.asset_url,
        "pois_count": len(dados.pois),
        "comparaveis_count": len(dados.comparaveis),
        "corretor_notificado": corretor_notificado,
        "error": None,
    }