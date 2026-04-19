"""
tools/sellers_dossier.py — Dossiê de Captação e Posicionamento de Mercado

Quando acionado para um imóvel sendo captado pelo corretor:
    1. Google Places API — POIs de luxo no raio de 1km (escolas, restaurantes, etc.)
    2. pgvector Supabase — imóveis comparáveis por tipologia + bairro
    3. Claude Sonnet — gera dossiê estruturado em Markdown
    4. reportlab — renderiza PDF premium
    5. Evolution API — envia PDF ao corretor via sendMedia (document)
    6. Salva em clients/{client_id}/assets/

Design decisions (CLAUDE.md):
    - Tool síncrona — projetada para run_in_executor no webhook
    - Fallback gracioso em Places API (sem api_key → aviso, não exceção)
    - Fallback gracioso em pgvector (base vazia → seção de comparativos omitida com warning)
    - Claude Sonnet, não Haiku — dossiê de captação é entregável ao dono/corretor
    - PDF com branding neutro (sem logo hardcoded) — cor e nome da imobiliária via imovel_data
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import ssl
import urllib.parse
import urllib.request
import uuid
from io import BytesIO
from pathlib import Path

log = logging.getLogger("sellers_dossier")

# ─── Configuração ─────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY         = os.getenv("OPENAI_API_KEY", "")
SUPABASE_URL           = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY           = os.getenv("SUPABASE_KEY", "")
GOOGLE_PLACES_API_KEY  = os.getenv("GOOGLE_PLACES_API_KEY", "")
EVOLUTION_URL          = os.getenv("EVOLUTION_URL", "https://api.otaviolabs.com")
EVOLUTION_API_KEY      = os.getenv("EVOLUTION_API_KEY", "")

PLACES_NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
LUXURY_RADIUS_M   = 1000  # 1km — foco em POIs premium de proximidade imediata

# Tipos de POI relevantes para posicionamento de alto padrão
_LUXURY_POI_TYPES = [
    ("school",        "Educação"),
    ("restaurant",    "Gastronomia"),
    ("gym",           "Fitness & Bem-estar"),
    ("shopping_mall", "Shopping"),
    ("art_gallery",   "Arte & Cultura"),
    ("park",          "Área Verde"),
    ("supermarket",   "Conveniência Premium"),
    ("bank",          "Serviços Financeiros"),
]

try:
    import anthropic as _anthropic_module
except ImportError:
    _anthropic_module = None  # type: ignore[assignment]

try:
    import openai as _openai_module
except ImportError:
    _openai_module = None  # type: ignore[assignment]

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, KeepTogether,
    )
    _reportlab_available = True
except ImportError:
    _reportlab_available = False


# ─── Helpers HTTP ─────────────────────────────────────────────────────────────

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def _http_get(url: str, timeout: int = 8) -> dict:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=timeout) as r:
            return {"ok": True, "data": json.loads(r.read().decode("utf-8"))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _http_post(url: str, payload: dict, headers: dict, timeout: int = 15) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=timeout) as r:
            body = r.read().decode("utf-8")
            return {"ok": True, "data": json.loads(body) if body else {}}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── 1. Google Places API — POIs de luxo ─────────────────────────────────────

def get_luxury_pois(
    lat: float,
    lng: float,
    api_key: str | None = None,
    radius_m: int = LUXURY_RADIUS_M,
) -> list[dict]:
    """
    Busca POIs de luxo via Google Places Nearby Search.
    Retorna lista de dicts com: nome, categoria, rating, endereco, distancia_estimada.
    Fallback gracioso: se api_key ausente ou API falhar, retorna [] com log.warning.
    Critério de luxo: rating >= 4.0 (top quartil do Places).
    """
    key = api_key or GOOGLE_PLACES_API_KEY
    if not key:
        log.warning("[SELLERS_DOSSIER] GOOGLE_PLACES_API_KEY não configurada — POIs omitidos")
        return []

    pois: list[dict] = []
    seen: set[str] = set()

    for place_type, categoria in _LUXURY_POI_TYPES:
        params = urllib.parse.urlencode({
            "location":  f"{lat},{lng}",
            "radius":    radius_m,
            "type":      place_type,
            "rankby":    "prominence",
            "key":       key,
        })
        url    = f"{PLACES_NEARBY_URL}?{params}"
        result = _http_get(url, timeout=6)

        if not result["ok"]:
            log.warning("[SELLERS_DOSSIER] Places API falhou para tipo %s: %s",
                        place_type, result.get("error"))
            continue

        data    = result["data"]
        status  = data.get("status", "")
        if status not in ("OK", "ZERO_RESULTS"):
            log.warning("[SELLERS_DOSSIER] Places API status=%s para tipo %s", status, place_type)
            continue

        for place in data.get("results", [])[:3]:  # top 3 por tipo
            nome    = place.get("name", "")
            rating  = place.get("rating", 0.0)
            place_id = place.get("place_id", "")

            # Deduplicação e filtro de qualidade
            if not nome or place_id in seen or rating < 4.0:
                continue
            seen.add(place_id)

            pois.append({
                "nome":       nome,
                "categoria":  categoria,
                "rating":     rating,
                "endereco":   place.get("vicinity", ""),
                "tipo_place": place_type,
            })

    log.info("[SELLERS_DOSSIER] %d POIs de luxo encontrados", len(pois))
    return pois


# ─── 2. pgvector — Imóveis comparáveis ───────────────────────────────────────

def _supabase_get(path: str) -> dict:
    url     = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=10) as r:
            body = r.read().decode("utf-8")
            return {"ok": True, "data": json.loads(body) if body else []}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_comparable_properties(
    tipologia: str,
    bairro: str,
    client_id: str,
    limit: int = 5,
) -> list[dict]:
    """
    Busca imóveis comparáveis no Supabase pgvector filtrando por tipologia + bairro.
    Usa filtro em metadata JSONB — sem pgvector call direto.
    Retorna lista de dicts com: imovel_id, conteudo, metadata.
    Fallback gracioso: retorna [] com log.warning se base vazia ou Supabase indisponível.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.warning("[SELLERS_DOSSIER] Supabase não configurado — comparáveis omitidos")
        return []

    # Normaliza para busca case-insensitive
    tipo_norm  = tipologia.lower().strip()
    bairro_norm = bairro.lower().strip()

    # Query: is_off_market=false, mesmo client_id, tipologia e bairro no metadata
    path = (
        f"imoveis_embeddings"
        f"?select=imovel_id,conteudo,metadata"
        f"&client_id=eq.{client_id}"
        f"&is_off_market=eq.false"
        f"&metadata->>tipologia=ilike.{urllib.parse.quote(tipo_norm + '%')}"
        f"&limit={limit}"
    )
    result = _supabase_get(path)

    if not result["ok"]:
        log.warning("[SELLERS_DOSSIER] Falha ao buscar comparáveis: %s", result.get("error"))
        return []

    rows = result["data"] or []

    # Filtro adicional de bairro (Supabase não suporta ILIKE em nested JSONB via query param)
    filtered = [
        r for r in rows
        if bairro_norm in (r.get("metadata", {}).get("bairro") or "").lower()
        or bairro_norm in (r.get("conteudo") or "").lower()
    ]

    if not filtered and rows:
        # Amplia: usa todos do mesmo client_id e tipologia se nenhum bate no bairro
        filtered = rows

    if not filtered:
        log.warning("[SELLERS_DOSSIER] Base pgvector vazia ou sem comparáveis para %s em %s",
                    tipologia, bairro)

    log.info("[SELLERS_DOSSIER] %d imóveis comparáveis encontrados", len(filtered))
    return filtered[:limit]


# ─── 3. Claude Sonnet — Geração do dossiê em Markdown ─────────────────────────

_DOSSIE_PROMPT = """\
Você é um consultor imobiliário de alto padrão especialista em estratégia de venda.
Crie um Dossiê de Captação e Posicionamento de Mercado para o imóvel descrito abaixo.

DADOS DO IMÓVEL:
{imovel_desc}

PONTOS DE INTERESSE PREMIUM NA REGIÃO (raio 1km):
{pois_text}

IMÓVEIS COMPARÁVEIS NO PORTFÓLIO:
{comparaveis_text}

Gere um dossiê estruturado profissional em Markdown com EXATAMENTE estas seções:

# Dossiê de Captação e Posicionamento de Mercado

## 1. Resumo do Imóvel
[Descrição sofisticada em 3-4 frases destacando os atributos mais relevantes para venda]

## 2. Comparativos de Mercado
[Análise dos imóveis comparáveis: como este imóvel se posiciona em preço/m², metragem, qualidade]

## 3. Vizinhança Premium
[POIs de maior relevância para o perfil do comprador: escolas de elite, gastronomia, serviços]

## 4. Estratégia de Precificação
[Posicionamento de preço recomendado com base nos comparativos, vantagens competitivas,
prazo estimado de venda para esse posicionamento]

## 5. Pontos de Venda Prioritários
[Top 3-5 argumentos mais fortes para uso nas abordagens comerciais]

Regras absolutas:
- NUNCA invente dados de rentabilidade, yield ou percentuais de valorização sem base nos comparativos
- NUNCA cite preços de outros imóveis que não estejam nos comparativos fornecidos
- Tom: sofisticado, direto, orientado a ação. Linguagem do consultor especialista, não do folheto
- Máximo 600 palavras no total
"""


def generate_captacao_markdown(
    imovel_data: dict,
    pois: list[dict],
    comparaveis: list[dict],
) -> str:
    """
    Claude Sonnet gera o dossiê de captação em Markdown.
    Síncrono — projetado para run_in_executor.
    """
    if _anthropic_module is None:
        raise ImportError("anthropic não instalado")

    # Formata imóvel
    imovel_parts = []
    if imovel_data.get("tipologia"):
        imovel_parts.append(f"Tipologia: {imovel_data['tipologia']}")
    if imovel_data.get("bairro"):
        imovel_parts.append(f"Bairro: {imovel_data['bairro']}, {imovel_data.get('cidade', 'São Paulo')}")
    if imovel_data.get("metragem"):
        imovel_parts.append(f"Metragem: {imovel_data['metragem']}m²")
    if imovel_data.get("quartos"):
        imovel_parts.append(f"Quartos: {imovel_data['quartos']}")
    if imovel_data.get("valor"):
        imovel_parts.append(f"Valor pretendido: R$ {imovel_data['valor']:,.0f}".replace(",", "."))
    if imovel_data.get("caracteristicas"):
        caract = imovel_data["caracteristicas"]
        imovel_parts.append(f"Características: {', '.join(caract) if isinstance(caract, list) else caract}")
    if imovel_data.get("descricao"):
        imovel_parts.append(f"Descrição: {imovel_data['descricao'][:400]}")
    imovel_desc = "\n".join(imovel_parts) or "Dados não fornecidos"

    # Formata POIs
    if pois:
        pois_lines = [f"- {p['nome']} ({p['categoria']}) — rating {p['rating']}" for p in pois[:10]]
        pois_text  = "\n".join(pois_lines)
    else:
        pois_text = "Dados de vizinhança não disponíveis no momento."

    # Formata comparáveis
    if comparaveis:
        comp_lines = []
        for c in comparaveis[:5]:
            meta   = c.get("metadata") or {}
            resumo = c.get("conteudo", "")[:200]
            bairro = meta.get("bairro") or ""
            valor  = meta.get("valor")
            line   = f"- {meta.get('tipologia', 'Imóvel')} em {bairro}"
            if valor:
                line += f" | R$ {valor:,.0f}".replace(",", ".")
            line += f" | {resumo[:100]}"
            comp_lines.append(line)
        comparaveis_text = "\n".join(comp_lines)
    else:
        comparaveis_text = "Nenhum comparável disponível no portfólio atual."

    client = _anthropic_module.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": _DOSSIE_PROMPT.format(
                imovel_desc=imovel_desc,
                pois_text=pois_text,
                comparaveis_text=comparaveis_text,
            ),
        }],
    )
    return msg.content[0].text.strip()


# ─── 4. reportlab — Renderização do PDF ──────────────────────────────────────

def render_captacao_pdf(
    markdown_text: str,
    imovel_data: dict,
    imobiliaria: str = "Imobiliária",
) -> bytes:
    """
    Renderiza o dossiê de captação em PDF usando reportlab.
    Layout premium: header escuro com nome da imobiliária, corpo limpo.
    """
    if not _reportlab_available:
        raise ImportError("reportlab não instalado")

    buf    = BytesIO()
    doc    = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=2.5 * cm, bottomMargin=2.5 * cm,
    )
    styles = getSampleStyleSheet()
    story  = []

    # ── Palette ──────────────────────────────────────────────────────────────
    DARK     = colors.HexColor("#1A1A2E")
    GOLD     = colors.HexColor("#C9A84C")
    MEDIUM   = colors.HexColor("#4A4A6A")
    LIGHT_BG = colors.HexColor("#F7F5F0")
    WHITE    = colors.white

    def st(name: str, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, parent=styles["Normal"], **kw)

    st_header  = st("SDHeader",  fontSize=9, textColor=WHITE,
                    fontName="Helvetica", leading=13, spaceAfter=2)
    st_title   = st("SDTitle",   fontSize=18, textColor=DARK,
                    fontName="Helvetica-Bold", leading=22, spaceBefore=12, spaceAfter=6)
    st_h2      = st("SDH2",      fontSize=13, textColor=DARK,
                    fontName="Helvetica-Bold", leading=16, spaceBefore=10, spaceAfter=4)
    st_body    = st("SDBody",    fontSize=10, textColor=MEDIUM,
                    fontName="Helvetica", leading=14, spaceAfter=3)
    st_bullet  = st("SDBullet",  fontSize=10, textColor=MEDIUM,
                    fontName="Helvetica", leading=14, leftIndent=14, spaceAfter=2)
    st_label   = st("SDLabel",   fontSize=8,  textColor=GOLD,
                    fontName="Helvetica-Bold", leading=11, spaceBefore=6, spaceAfter=2)

    # ── Cabeçalho (box escuro) ─────────────────────────────────────────────
    from reportlab.platypus import Table, TableStyle
    header_data = [[
        Paragraph(f"<b>{imobiliaria.upper()}</b>", st_header),
        Paragraph("DOSSIÊ DE CAPTAÇÃO", st_header),
    ]]
    header_table = Table(header_data, colWidths=[9 * cm, 7 * cm])
    header_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, -1), DARK),
        ("TEXTCOLOR",   (0, 0), (-1, -1), WHITE),
        ("TOPPADDING",  (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (0, -1), 12),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 12),
        ("ALIGN",       (1, 0), (1, -1), "RIGHT"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 0.5 * cm))

    # ── Metadata do imóvel ──────────────────────────────────────────────────
    bairro = imovel_data.get("bairro") or ""
    tipo   = imovel_data.get("tipologia") or ""
    valor  = imovel_data.get("valor")

    if bairro or tipo:
        subtitulo = f"{tipo.title()} — {bairro}" if bairro else tipo.title()
        story.append(Paragraph(subtitulo, st_title))
    if valor:
        story.append(Paragraph(
            f"Valor pretendido: R$ {valor:,.0f}".replace(",", "."),
            st_label,
        ))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=8))

    # ── Corpo — renderiza Markdown simplificado ─────────────────────────────
    for line in markdown_text.split("\n"):
        line_stripped = line.strip()
        if not line_stripped:
            story.append(Spacer(1, 0.2 * cm))
            continue
        # Títulos Markdown
        if line_stripped.startswith("# ") and not line_stripped.startswith("## "):
            # Título principal — já temos no header, skip
            continue
        elif line_stripped.startswith("## "):
            text = line_stripped.lstrip("# ").strip()
            story.append(Paragraph(text.upper(), st_h2))
            story.append(HRFlowable(width="100%", thickness=0.5, color=LIGHT_BG, spaceAfter=4))
        elif line_stripped.startswith("- ") or line_stripped.startswith("* "):
            text = line_stripped.lstrip("-* ").strip()
            # Limpa markdown básico
            text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
            text = re.sub(r'\*(.*?)\*', r'\1', text)
            story.append(Paragraph(f"• {text}", st_bullet))
        else:
            text = line_stripped
            text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
            text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
            story.append(Paragraph(text, st_body))

    # ── Rodapé ─────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.5 * cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GOLD))
    story.append(Paragraph(
        f"Gerado por Sofia IA — {imobiliaria} | Documento sigiloso — uso interno",
        st("SDFooter", fontSize=7, textColor=MEDIUM, fontName="Helvetica",
           leading=10, spaceBefore=4),
    ))

    doc.build(story)
    return buf.getvalue()


# ─── 5. Persistência e envio ──────────────────────────────────────────────────

def save_captacao_locally(
    pdf_bytes: bytes,
    client_id: str,
    imovel_id: str,
) -> str:
    """Salva PDF em clients/{client_id}/assets/. Retorna path absoluto."""
    base_dir = Path(__file__).resolve().parent.parent
    assets_dir = base_dir / "clients" / client_id / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    path = assets_dir / f"captacao_{imovel_id}.pdf"
    path.write_bytes(pdf_bytes)
    log.info("[SELLERS_DOSSIER] PDF salvo em %s", path)
    return str(path)


def send_captacao_to_corretor(
    corretor_phone: str,
    pdf_bytes: bytes,
    imovel_data: dict,
    imobiliaria: str = "Imobiliária",
    instance: str = "devlabz",
) -> None:
    """
    Envia PDF do dossiê ao corretor via Evolution API (sendMedia document).
    Síncrono — projetado para run_in_executor.
    """
    ev_url  = EVOLUTION_URL
    ev_key  = EVOLUTION_API_KEY
    tipo    = imovel_data.get("tipologia", "imóvel").title()
    bairro  = imovel_data.get("bairro", "")
    caption = f"Dossiê de Captação — {tipo} em {bairro} | {imobiliaria}"

    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    payload = {
        "number":   corretor_phone,
        "media":    b64,
        "mimetype": "application/pdf",
        "fileName": f"dossie_captacao_{bairro.lower().replace(' ', '_')}.pdf",
        "caption":  caption,
    }
    headers = {"apikey": ev_key, "Content-Type": "application/json"}
    result  = _http_post(f"{ev_url}/message/sendMedia/{instance}", payload, headers)

    if result["ok"]:
        log.info("[SELLERS_DOSSIER] Dossiê enviado ao corretor %s", corretor_phone)
    else:
        raise RuntimeError(f"Falha ao enviar dossiê: {result.get('error')}")


# ─── 6. Orquestrador principal ─────────────────────────────────────────────────

def generate_sellers_dossier(
    lat: float,
    lng: float,
    tipologia: str,
    imovel_data: dict,
    client_id: str,
    corretor_phone: str,
    imobiliaria: str = "Imobiliária",
    evolution_instance: str = "devlabz",
    places_api_key: str | None = None,
) -> dict:
    """
    Pipeline completo do Dossiê de Captação.
    Síncrono — projetado para asyncio.run_in_executor no webhook.

    Retorna dict com:
        markdown_text:   str — dossiê em Markdown
        pdf_path:        str — caminho do PDF salvo
        pois_count:      int — POIs encontrados
        comparaveis_count: int — imóveis comparáveis encontrados
        imovel_id:       str — ID gerado para este dossiê
    """
    imovel_id = f"captacao_{uuid.uuid4().hex[:10]}"
    log.info("[SELLERS_DOSSIER] Iniciando para %s em %s (lat=%s, lng=%s)",
             tipologia, imovel_data.get("bairro"), lat, lng)

    # 1. POIs de luxo
    pois = get_luxury_pois(lat, lng, api_key=places_api_key, radius_m=LUXURY_RADIUS_M)

    # 2. Comparáveis no pgvector
    bairro = imovel_data.get("bairro", "")
    comparaveis = get_comparable_properties(tipologia, bairro, client_id)

    # 3. Gera Markdown via Claude Sonnet
    markdown_text = generate_captacao_markdown(imovel_data, pois, comparaveis)
    log.info("[SELLERS_DOSSIER] Markdown gerado: %d chars", len(markdown_text))

    # 4. Renderiza PDF
    pdf_bytes = render_captacao_pdf(markdown_text, imovel_data, imobiliaria)
    log.info("[SELLERS_DOSSIER] PDF gerado: %d bytes", len(pdf_bytes))

    # 5. Salva localmente
    pdf_path = save_captacao_locally(pdf_bytes, client_id, imovel_id)

    # 6. Envia ao corretor (falha tolerável — não propaga)
    try:
        send_captacao_to_corretor(
            corretor_phone=corretor_phone,
            pdf_bytes=pdf_bytes,
            imovel_data=imovel_data,
            imobiliaria=imobiliaria,
            instance=evolution_instance,
        )
    except Exception as e:
        log.error("[SELLERS_DOSSIER] Falha no envio para corretor %s: %s", corretor_phone, e)

    log.info("[SELLERS_DOSSIER] Pipeline completo: %d POIs, %d comparáveis, PDF em %s",
             len(pois), len(comparaveis), pdf_path)

    return {
        "imovel_id":          imovel_id,
        "markdown_text":      markdown_text,
        "pdf_path":           pdf_path,
        "pois_count":         len(pois),
        "comparaveis_count":  len(comparaveis),
    }
