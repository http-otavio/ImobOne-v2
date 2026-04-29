"""
tools/dossie.py — Dossiê de Caviar

Quando Sofia confirma visita, gera automaticamente um PDF de briefing estratégico
do lead e envia ao corretor via WhatsApp como documento.

Pipeline:
    1. Claude Sonnet analisa histórico completo → dict estruturado (perfil, busca,
       hot buttons, objeções, sinais quentes, próximo passo)
    2. reportlab renderiza PDF com layout premium
    3. Evolution API envia como documento ao corretor

Design decisions (CLAUDE.md):
    - Claude Sonnet, não Haiku — requer síntese psicológica e narrativa
    - PDF gerado no VPS, dados sensíveis não saem da infra
    - Fire-and-forget via asyncio.create_task — não bloqueia resposta ao lead
    - Arquivo salvo em clients/{client_id}/reports/ por rastreabilidade
"""

import base64
import io
import json
import logging
import os
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("dossie")

try:
    import anthropic as _anthropic_module
except ImportError:
    _anthropic_module = None  # type: ignore[assignment]

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
EVOLUTION_URL     = os.getenv("EVOLUTION_URL", "https://api.otaviolabs.com")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
IMOBILIARIA_NAME  = os.getenv("IMOBILIARIA_NAME", "Artístico Imóveis")


# ─── Cores do layout ────────────────────────────────────────────────────────

_DARK   = "#0f0f18"    # fundo do header
_GOLD   = "#c9a84c"    # acento dourado
_SLATE  = "#2d2d3d"    # texto principal
_LIGHT  = "#f7f7f9"    # fundo alternado de linhas
_BORDER = "#e0e0e8"    # bordas de tabela
_WHITE  = "#ffffff"


# ─── Prompt de geração ─────────────────────────────────────────────────────

_DOSSIE_PROMPT = """\
Você é um analista sênior especializado em psicologia de vendas de imóveis de alto padrão.
Analise a conversa completa entre Sofia (IA consultora) e o lead abaixo,
e produza um dossiê estratégico para o corretor que vai conduzir a visita.

DADOS DO LEAD
─────────────
Nome: {lead_name}
Telefone: {lead_phone}
Score de intenção: {score}/20{visita_flag}
Pipeline estimado: {pipeline}
Data da visita: {visit_date}

HISTÓRICO COMPLETO DA CONVERSA
────────────────────────────────
{history_text}
{metricas_financeiras_bloco}
Produza o dossiê em JSON com EXATAMENTE esta estrutura (sem campos extras):
{{
  "perfil": {{
    "nome": "nome declarado pelo lead ou 'Não informado'",
    "tom_geral": "descrição em 1 frase do perfil comportamental e emocional do lead",
    "urgencia": "alta | média | baixa — justificada por sinais concretos da conversa"
  }},
  "busca": {{
    "tipologia": "tipo exato de imóvel buscado (ex: cobertura duplex, casa com piscina)",
    "bairros": ["bairro 1", "bairro 2"],
    "metragem": "metragem ou faixa declarada, ou 'Não informada'",
    "budget": "budget declarado ou faixa estimada com base nos imóveis mencionados",
    "prazo": "prazo declarado ou estimado",
    "uso": "moradia própria | investimento | lazer | moradia + investimento | não declarado"
  }},
  "hot_buttons": [
    "critério que mais importa para o lead — em ordem de prioridade, máx 5",
    "critério 2",
    "critério 3"
  ],
  "objecoes": [
    {{
      "objecao": "objeção levantada de forma concisa",
      "status": "resolvida | em aberto | latente",
      "como_tratar": "como o corretor deve abordar na visita"
    }}
  ],
  "sinais_quentes": [
    "comportamento ou frase que indica alta intenção — específico, com contexto"
  ],
  "proximo_passo": "ação concreta e específica para o corretor na visita ou imediatamente após. Seja prescritivo.",
  "pontos_de_atencao": "sensibilidades, tópicos a evitar ou riscos de perder o lead — máx 2 linhas",
  "metricas_financeiras": null
}}

Regras absolutas:
- Baseie-se APENAS no que foi dito na conversa. Nunca invente dados.
- Se não houver informação sobre um campo, use "Não informado".
- hot_buttons e sinais_quentes devem citar frases ou comportamentos reais da conversa.
- proximo_passo deve ser prescritivo e específico — não genérico.
- DADOS FINANCEIROS — CRÍTICO: NUNCA invente valorização, yield, cap rate, rentabilidade
  ou qualquer percentual financeiro. Se o bloco MÉTRICAS FINANCEIRAS estiver presente
  acima, use APENAS os dados dele (com a fonte explícita). Se não estiver presente OU
  o perfil do lead não for investimento, defina "metricas_financeiras": null.
  A seção metricas_financeiras só deve ser preenchida quando AMBAS as condições forem
  verdadeiras: (1) busca.uso == "investimento" E (2) dados financeiros estão no bloco acima.
- Se metricas_financeiras for preenchida, use este formato:
  {{"valorizacao_aa": "X% ao ano (Fonte Y, Período Z)", "liquidez_dias": N,
    "comparativo_fii": "valorização X% vs FII Y Z% a.a."}}
- Responda APENAS com o JSON válido. Sem texto antes ou depois.\
"""


def generate_dossie_content(
    history: list[dict],
    lead_name: str | None,
    lead_phone: str,
    score: int = 0,
    pipeline: float | None = None,
    visit_date: str | None = None,
    liquidity_data: dict | None = None,
) -> dict:
    """
    Claude Sonnet analisa o histórico e retorna dict estruturado do dossiê.
    Síncrono — chamar via asyncio.run_in_executor.
    Lança exceção se o JSON não puder ser parseado.

    liquidity_data: dados de liquidez de tools/liquidity.py — injetados na seção
    'Métricas Financeiras' SOMENTE quando perfil é investidor. Quando None, a seção
    é omitida (nunca inventada).
    """
    if _anthropic_module is None:
        raise ImportError("anthropic não instalado")

    # Monta histórico legível (últimos 30 turnos, truncado a 300 chars/mensagem)
    recent = history[-30:] if len(history) > 30 else history
    history_text = "\n".join(
        f"{'Lead' if m['role'] == 'user' else 'Sofia'}: {m['content'][:300]}"
        for m in recent
    ) or "(sem histórico disponível)"

    pipeline_str = f"R$ {pipeline:,.0f}".replace(",", ".") if pipeline else "Não informado"
    visit_str    = visit_date or "A confirmar"
    visita_flag  = " — VISITA CONFIRMADA" if visit_date else ""

    # Prepara bloco de métricas financeiras (apenas se dados verificáveis disponíveis)
    if liquidity_data:
        try:
            from tools.liquidity import format_metricas_financeiras
            metricas_bloco = "\n\n" + format_metricas_financeiras(liquidity_data) + "\n"
        except Exception:
            metricas_bloco = ""
    else:
        metricas_bloco = ""

    prompt = _DOSSIE_PROMPT.format(
        lead_name=lead_name or "Não informado",
        lead_phone=lead_phone,
        score=score,
        visita_flag=visita_flag,
        pipeline=pipeline_str,
        visit_date=visit_str,
        history_text=history_text,
        metricas_financeiras_bloco=metricas_bloco,
    )

    client = _anthropic_module.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1800,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()

    # Extrai JSON se vier embrulhado em ```
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()

    return json.loads(raw)


# ─── Renderização PDF ───────────────────────────────────────────────────────

def render_dossie_pdf(
    content: dict,
    lead_name: str | None,
    lead_phone: str,
    imobiliaria: str = IMOBILIARIA_NAME,
    visit_date: str | None = None,
) -> bytes:
    """
    Renderiza o dossiê como PDF via reportlab.
    Retorna bytes do PDF.
    Lança ImportError se reportlab não estiver instalado.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table,
            TableStyle, HRFlowable,
        )
    except ImportError as e:
        raise ImportError("reportlab não instalado. Execute: pip install reportlab") from e

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=0,       # header ocupa o topo
        bottomMargin=1.8 * cm,
        leftMargin=2.2 * cm,
        rightMargin=2.2 * cm,
    )

    W = A4[0] - 4.4 * cm   # largura útil
    styles = getSampleStyleSheet()

    # ── Estilos customizados ────────────────────────────────────────────────

    def style(name, **kw):
        return ParagraphStyle(name, parent=styles["Normal"], **kw)

    st_header_title = style("HdrTitle",
        fontSize=20, leading=24, textColor=colors.HexColor(_GOLD),
        fontName="Helvetica-Bold", spaceAfter=2,
    )
    st_header_sub = style("HdrSub",
        fontSize=9, textColor=colors.HexColor("#8888aa"),
        fontName="Helvetica", spaceAfter=0,
    )
    st_section = style("Section",
        fontSize=9, leading=11,
        textColor=colors.HexColor(_GOLD),
        fontName="Helvetica-Bold",
        spaceBefore=14, spaceAfter=4,
    )
    st_body = style("Body",
        fontSize=10, leading=15,
        textColor=colors.HexColor(_SLATE),
        spaceAfter=4,
    )
    st_label = style("Label",
        fontSize=8, leading=10,
        textColor=colors.HexColor("#888899"),
        fontName="Helvetica",
    )
    st_value = style("Value",
        fontSize=10, leading=14,
        textColor=colors.HexColor(_SLATE),
        fontName="Helvetica-Bold",
    )
    st_bullet = style("Bullet",
        fontSize=10, leading=15,
        textColor=colors.HexColor(_SLATE),
        leftIndent=12, spaceAfter=3,
    )
    st_warning = style("Warning",
        fontSize=10, leading=15,
        textColor=colors.HexColor("#c0392b"),
        leftIndent=12, spaceAfter=3,
    )
    st_action = style("Action",
        fontSize=11, leading=16,
        textColor=colors.HexColor(_SLATE),
        fontName="Helvetica-Bold",
        backColor=colors.HexColor("#f9f6ee"),
        borderPadding=(8, 10, 8, 10),
    )
    st_footer = style("Footer",
        fontSize=8, textColor=colors.HexColor("#aaaacc"),
        alignment=1,  # centered
    )

    c_dark  = colors.HexColor(_DARK)
    c_gold  = colors.HexColor(_GOLD)
    c_light = colors.HexColor(_LIGHT)
    c_bord  = colors.HexColor(_BORDER)

    story = []

    # ── HEADER (tabela para fundo escuro full-width) ────────────────────────
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    name_display = lead_name or "Lead"
    visit_str = visit_date or "A confirmar"

    header_data = [[
        Paragraph("DOSSIÊ DE CAVIAR", st_header_title),
        Paragraph(
            f"{imobiliaria}&nbsp;&nbsp;·&nbsp;&nbsp;Gerado em {now_str}",
            st_header_sub,
        ),
    ]]
    header_tbl = Table(header_data, colWidths=[W])
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), c_dark),
        ("LEFTPADDING",  (0, 0), (-1, -1), 22),
        ("RIGHTPADDING", (0, 0), (-1, -1), 22),
        ("TOPPADDING",   (0, 0), (-1, -1), 20),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 20),
        ("ROWSPAN",      (0, 0), (0, -1), 1),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 0.3 * cm))

    # ── INFO RÁPIDA (lead + visita) ─────────────────────────────────────────
    def info_pair(label, value):
        return [Paragraph(label, st_label), Paragraph(value or "—", st_value)]

    info_rows = [
        info_pair("LEAD", name_display),
        info_pair("TELEFONE", lead_phone),
        info_pair("VISITA", visit_str),
        info_pair("URGÊNCIA", (content.get("perfil") or {}).get("urgencia", "—").upper()),
    ]
    info_tbl = Table(info_rows, colWidths=[3 * cm, W - 3 * cm])
    info_tbl.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [_WHITE, _LIGHT]),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.3, c_bord),
    ]))
    story.append(info_tbl)

    # ── Seção genérica ──────────────────────────────────────────────────────

    def section(title):
        story.append(Paragraph(title.upper(), st_section))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=c_gold, spaceAfter=4))

    def kv_table(rows):
        """rows = [(label, value), ...]"""
        data = [[Paragraph(k, st_label), Paragraph(str(v) or "—", st_body)]
                for k, v in rows]
        tbl = Table(data, colWidths=[4 * cm, W - 4 * cm])
        tbl.setStyle(TableStyle([
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [_WHITE, _LIGHT]),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.3, c_bord),
        ]))
        story.append(tbl)

    def bullets(items, style=None):
        for item in items:
            s = style or st_bullet
            story.append(Paragraph(f"• &nbsp;{item}", s))

    # ── PERFIL ──────────────────────────────────────────────────────────────
    perfil = content.get("perfil") or {}
    section("Perfil do Lead")
    if perfil.get("tom_geral"):
        story.append(Paragraph(perfil["tom_geral"], st_body))

    # ── BUSCA ───────────────────────────────────────────────────────────────
    busca = content.get("busca") or {}
    section("O Que Busca")
    kv_rows = []
    if busca.get("tipologia"):   kv_rows.append(("Tipologia",  busca["tipologia"]))
    if busca.get("bairros"):     kv_rows.append(("Bairros",    ", ".join(busca["bairros"])))
    if busca.get("metragem"):    kv_rows.append(("Metragem",   busca["metragem"]))
    if busca.get("budget"):      kv_rows.append(("Budget",     busca["budget"]))
    if busca.get("prazo"):       kv_rows.append(("Prazo",      busca["prazo"]))
    if busca.get("uso"):         kv_rows.append(("Uso previsto", busca["uso"]))
    if kv_rows:
        kv_table(kv_rows)

    # ── HOT BUTTONS ─────────────────────────────────────────────────────────
    hot = content.get("hot_buttons") or []
    if hot:
        section("Hot Buttons — O Que Fecha a Venda")
        for i, item in enumerate(hot, 1):
            story.append(Paragraph(f"<b>{i}.</b> &nbsp;{item}", st_bullet))

    # ── SINAIS QUENTES ──────────────────────────────────────────────────────
    sinais = content.get("sinais_quentes") or []
    if sinais:
        section("Sinais de Alta Intenção")
        bullets(sinais)

    # ── OBJEÇÕES ────────────────────────────────────────────────────────────
    objecoes = content.get("objecoes") or []
    if objecoes:
        section("Objeções e Como Tratar")
        for obj in objecoes:
            objecao    = obj.get("objecao", "")
            status     = obj.get("status", "")
            como_tratar = obj.get("como_tratar", "")
            status_icon = {"resolvida": "✓", "em aberto": "⚠", "latente": "◉"}.get(
                status.lower(), "•"
            )
            story.append(Paragraph(
                f"<b>{status_icon} {objecao}</b> <font color='#888899' size='8'>[{status}]</font>",
                st_bullet,
            ))
            if como_tratar:
                story.append(Paragraph(
                    f"<font color='#555566' size='9'>→ {como_tratar}</font>",
                    style("ObjHow",
                          leftIndent=24, fontSize=9, leading=13,
                          textColor=colors.HexColor("#555566"), spaceAfter=6),
                ))

    # ── PRÓXIMO PASSO ───────────────────────────────────────────────────────
    proximo = content.get("proximo_passo", "")
    if proximo:
        section("Próximo Passo — Ação do Corretor")
        action_data = [[Paragraph(f"⚡ &nbsp;{proximo}", st_action)]]
        action_tbl = Table(action_data, colWidths=[W])
        action_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f9f6ee")),
            ("BOX",        (0, 0), (-1, -1), 1.5, c_gold),
            ("LEFTPADDING",  (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING",   (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 10),
        ]))
        story.append(action_tbl)

    # ── PONTOS DE ATENÇÃO ───────────────────────────────────────────────────
    atencao = content.get("pontos_de_atencao", "")
    if atencao:
        section("⚠ Pontos de Atenção")
        story.append(Paragraph(atencao, st_warning))

    # ── MÉTRICAS FINANCEIRAS (apenas perfil investidor, dados verificados) ──
    metricas = content.get("metricas_financeiras")
    if metricas and isinstance(metricas, dict):
        section("Métricas Financeiras Verificadas")
        mf_rows = []
        if metricas.get("valorizacao_aa"):
            mf_rows.append(("Valorização histórica", str(metricas["valorizacao_aa"])))
        if metricas.get("liquidez_dias") is not None:
            mf_rows.append(("Liquidez estimada",
                            f"{metricas['liquidez_dias']} dias médios para venda na região"))
        if metricas.get("comparativo_fii"):
            mf_rows.append(("Comparativo FII", str(metricas["comparativo_fii"])))
        if mf_rows:
            kv_table(mf_rows)
        story.append(Paragraph(
            "<font color='#888899' size='8'>"
            "Dados verificados com fonte explícita. Não representam projeção ou garantia."
            "</font>",
            style("MfDisclaimer", fontSize=8, leading=10,
                  textColor=colors.HexColor("#888899"),
                  fontName="Helvetica", spaceAfter=4),
        ))

    # ── FOOTER ──────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.8 * cm))
    story.append(HRFlowable(width="100%", thickness=0.3, color=c_bord))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(
        f"Dossiê de Caviar · {imobiliaria} · Gerado por Sofia (ImobOne) · {now_str}",
        st_footer,
    ))

    doc.build(story)
    return buf.getvalue()


# ─── Envio via Evolution API ────────────────────────────────────────────────

def _make_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def send_dossie_to_corretor(
    pdf_bytes: bytes,
    lead_name: str | None,
    lead_phone: str,
    corretor_phone: str,
    instance: str = "",
    evolution_url: str = "",
    evolution_api_key: str = "",
) -> None:
    """
    Envia o PDF do dossiê ao corretor via Evolution API como documento.
    Síncrono — chamar via asyncio.run_in_executor.
    """
    ev_url = evolution_url or EVOLUTION_URL
    ev_key = evolution_api_key or EVOLUTION_API_KEY
    inst   = instance or "devlabz"

    name_safe = (lead_name or lead_phone).replace(" ", "_").replace("/", "-")
    filename  = f"dossie-caviar-{name_safe}.pdf"
    caption   = f"📋 *Dossiê de Caviar*\n{lead_name or lead_phone}\nGerado por Sofia"

    payload = json.dumps({
        "number":    corretor_phone,
        "mediatype": "document",
        "mimetype":  "application/pdf",
        "media":     base64.b64encode(pdf_bytes).decode("utf-8"),
        "fileName":  filename,
        "caption":   caption,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{ev_url}/message/sendMedia/{inst}",
        data=payload,
        headers={
            "apikey":       ev_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=30) as r:
            log.info(
                "Dossiê PDF enviado para %s | lead: %s | status: %s",
                corretor_phone, lead_phone, r.status,
            )
    except Exception as e:
        log.error(
            "Falha ao enviar dossiê para %s | lead: %s | erro: %s",
            corretor_phone, lead_phone, e,
        )
        raise


# ─── Persistência local do PDF ──────────────────────────────────────────────

def save_dossie_locally(
    pdf_bytes: bytes,
    lead_phone: str,
    client_id: str,
    base_path: str = "/opt/ImobOne-v2/clients",
) -> str:
    """
    Salva PDF em clients/{client_id}/reports/ para rastreabilidade.
    Retorna o caminho do arquivo salvo.
    """
    ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_phone = lead_phone.replace("+", "").replace(" ", "")
    fname = f"dossie_{safe_phone}_{ts}.pdf"
    report_dir = Path(base_path) / client_id / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    fpath = report_dir / fname
    fpath.write_bytes(pdf_bytes)
    log.info("Dossiê salvo em %s", fpath)
    return str(fpath)


# ─── Entrypoint principal ───────────────────────────────────────────────────

def build_and_send_dossie(
    *,
    history: list[dict],
    lead_name: str | None,
    lead_phone: str,
    corretor_phone: str,
    score: int = 0,
    pipeline: float | None = None,
    visit_date: str | None = None,
    client_id: str = "demo",
    imobiliaria: str = IMOBILIARIA_NAME,
    instance: str = "",
    evolution_url: str = "",
    evolution_api_key: str = "",
    save_local: bool = True,
    liquidity_data: dict | None = None,
) -> None:
    """
    Orquestra o pipeline completo: gera conteúdo → PDF → envia ao corretor.
    Síncrono — projetado para rodar via asyncio.run_in_executor.
    Lança exceção em caso de falha crítica (sem conteúdo ou sem PDF).
    Falha no envio é logada mas não lança exceção — o PDF já foi gerado.
    """
    log.info("[DOSSIE] Gerando para lead %s → corretor %s", lead_phone, corretor_phone)

    # 1. Gera conteúdo via Claude Sonnet
    content = generate_dossie_content(
        history=history,
        lead_name=lead_name,
        lead_phone=lead_phone,
        score=score,
        pipeline=pipeline,
        visit_date=visit_date,
        liquidity_data=liquidity_data,
    )
    log.info("[DOSSIE] Conteúdo gerado OK para %s", lead_phone)

    # 2. Renderiza PDF
    pdf_bytes = render_dossie_pdf(
        content=content,
        lead_name=lead_name,
        lead_phone=lead_phone,
        imobiliaria=imobiliaria,
        visit_date=visit_date,
    )
    log.info("[DOSSIE] PDF gerado: %d bytes para %s", len(pdf_bytes), lead_phone)

    # 3. Persiste localmente
    if save_local:
        try:
            save_dossie_locally(pdf_bytes, lead_phone, client_id)
        except Exception as e:
            log.warning("[DOSSIE] Falha ao salvar localmente: %s", e)

    # 4. Envia ao corretor — falha tolerável (PDF já gerado e salvo)
    try:
        send_dossie_to_corretor(
            pdf_bytes=pdf_bytes,
            lead_name=lead_name,
            lead_phone=lead_phone,
            corretor_phone=corretor_phone,
            instance=instance,
            evolution_url=evolution_url,
            evolution_api_key=evolution_api_key,
        )
    except Exception as e:
        log.error("[DOSSIE] Falha no envio para corretor %s (lead %s): %s",
                  corretor_phone, lead_phone, e)

    log.info("[DOSSIE] Pipeline completo para %s", lead_phone)
