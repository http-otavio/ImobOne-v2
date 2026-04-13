#!/usr/bin/env python3
"""
report_engine.py — Engine de relatórios executivos para o DONO da imobiliária/construtora

ICP: Dono da imobiliária / Dono da construtora
    O dono não quer ver transcrição de mensagem. Quer ver número.
    Este módulo gera:
        1. Relatório semanal via WhatsApp (todo domingo 21h, configurável)
        2. Relatório semanal em JSON (consumido pelo dashboard via /reports/weekly)
        3. Exportação PDF (via reportlab)
        4. Exportação CSV

Métricas obrigatórias por relatório:
    - total_leads          — leads atendidos no período
    - visitas_confirmadas  — visitas confirmadas pela Sofia
    - leads_quentes        — leads com score >= threshold
    - pipeline_estimado_brl — soma dos valores dos imóveis de interesse (score >= 6)
    - top_objecao          — objeção mais frequente detectada
    - taxa_conversao_pct   — visitas / leads * 100

Uso:
    # Gerar e enviar relatório semanal
    python3 report_engine.py --weekly

    # Dry-run (calcula métricas sem enviar WhatsApp)
    python3 report_engine.py --weekly --dry-run

    # Exportar CSV do período
    python3 report_engine.py --export-csv --days 7

    # Importado pelo pipeline_runner.py nos endpoints /reports/
"""

import csv
import io
import json
import logging
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("report_engine")

# ─── Config ──────────────────────────────────────────────────────────────────
SUPABASE_URL       = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY       = os.getenv("SUPABASE_KEY", "")
EVOLUTION_URL      = os.getenv("EVOLUTION_URL", "https://api.otaviolabs.com")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "devlabz")
OPERATOR_NUMBER    = os.getenv("OPERATOR_NUMBER", "")
CLIENT_ID          = os.getenv("DEMO_CLIENT_ID", "demo_imobiliaria_vendas")
IMOBILIARIA_NAME   = os.getenv("IMOBILIARIA_NAME", "Ávora Imóveis")
CONSULTANT_NAME    = os.getenv("CONSULTANT_NAME", "Sofia")

# Score mínimo para considerar lead "quente"
HOT_SCORE_THRESHOLD = int(os.getenv("HOT_SCORE_THRESHOLD", "6"))

# Diretório de saída dos relatórios persistidos em disco
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "/opt/ImobOne-v2")) / "reports"


# ─── SSL helper ──────────────────────────────────────────────────────────────
def _ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ─── Supabase REST client (síncrono) ─────────────────────────────────────────
def _sb_get(path: str, params: str = "") -> list:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{path}{'?' + params if params else ''}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error("Supabase GET %s: %s", path, e)
        return []


# ─── WhatsApp notification ────────────────────────────────────────────────────
def _send_whatsapp(to: str, text: str) -> bool:
    """Envia mensagem WhatsApp via Evolution API. Mesmo padrão do followup_engine."""
    if not to or not EVOLUTION_URL or not EVOLUTION_API_KEY:
        log.warning("WhatsApp não configurado — mensagem não enviada.")
        return False
    try:
        import httpx
        payload = {"number": to, "text": text}
        resp = httpx.post(
            f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}",
            json=payload,
            headers={"apikey": EVOLUTION_API_KEY},
            verify=False,
            timeout=15,
        )
        log.info("WhatsApp enviado → %s | HTTP %s", to, resp.status_code)
        return resp.status_code < 300
    except Exception as e:
        log.error("Falha WhatsApp → %s: %s", to, e)
        return False


# ─── Cálculo de métricas ──────────────────────────────────────────────────────
def compute_weekly_metrics(
    client_id: str = CLIENT_ID,
    days: int = 7,
    _leads_override: Optional[list] = None,  # usado em testes
) -> dict:
    """
    Calcula métricas executivas do período (últimos N dias).

    Retorna dict com:
        period_days, period_start, period_end,
        total_leads, visitas_confirmadas, leads_quentes,
        pipeline_estimado_brl, top_objecao, taxa_conversao_pct,
        leads_descartados, leads_por_origem, score_medio,
        generated_at, client_id
    """
    now = datetime.now(timezone.utc)
    period_start = now - timedelta(days=days)
    period_start_str = period_start.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── 1. Busca leads do período ──────────────────────────────────────────
    if _leads_override is not None:
        leads = _leads_override
    else:
        encoded_start = urllib.parse.quote(period_start_str)
        leads = _sb_get(
            "leads",
            (
                f"client_id=eq.{urllib.parse.quote(client_id)}"
                f"&created_at=gte.{encoded_start}"
                f"&select=lead_phone,lead_name,intention_score,score_breakdown,"
                f"pipeline_value_brl,visita_agendada,descartado,source,"
                f"objections_detected,created_at"
            ),
        )

    if not leads:
        log.info("Nenhum lead encontrado no período de %d dias.", days)
        return _empty_metrics(client_id, days, period_start, now)

    # ── 2. Métricas básicas ────────────────────────────────────────────────
    total_leads = len(leads)
    visitas_confirmadas = sum(1 for l in leads if l.get("visita_agendada"))
    leads_descartados = sum(1 for l in leads if l.get("descartado"))
    leads_quentes = sum(
        1 for l in leads if (l.get("intention_score") or 0) >= HOT_SCORE_THRESHOLD
    )

    scores = [l.get("intention_score") or 0 for l in leads]
    score_medio = round(sum(scores) / len(scores), 1) if scores else 0.0

    taxa_conversao_pct = (
        round(visitas_confirmadas / total_leads * 100, 1) if total_leads > 0 else 0.0
    )

    # ── 3. Pipeline estimado em R$ ─────────────────────────────────────────
    # Usa pipeline_value_brl se disponível (campo calculado pelo webhook).
    # Fallback: leads com score >= threshold contam como "possível interesse"
    # usando os valores do score_breakdown como proxy.
    pipeline_estimado_brl = 0.0
    for lead in leads:
        val = lead.get("pipeline_value_brl")
        if val and (lead.get("intention_score") or 0) >= HOT_SCORE_THRESHOLD:
            try:
                pipeline_estimado_brl += float(val)
            except (TypeError, ValueError):
                pass

    # ── 4. Objeção mais frequente ──────────────────────────────────────────
    objection_counts: dict[str, int] = {}
    for lead in leads:
        raw = lead.get("objections_detected") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        for obj in raw:
            cat = obj.get("categoria", obj) if isinstance(obj, dict) else str(obj)
            objection_counts[cat] = objection_counts.get(cat, 0) + 1

    top_objecao = None
    top_objecao_count = 0
    if objection_counts:
        top_objecao = max(objection_counts, key=lambda k: objection_counts[k])
        top_objecao_count = objection_counts[top_objecao]

    # ── 5. Breakdown por origem ────────────────────────────────────────────
    leads_por_origem: dict[str, int] = {}
    for lead in leads:
        src = lead.get("source") or "whatsapp_organico"
        leads_por_origem[src] = leads_por_origem.get(src, 0) + 1

    return {
        "client_id": client_id,
        "period_days": days,
        "period_start": period_start.isoformat(),
        "period_end": now.isoformat(),
        "generated_at": now.isoformat(),
        # KPIs principais (dono)
        "total_leads": total_leads,
        "visitas_confirmadas": visitas_confirmadas,
        "leads_quentes": leads_quentes,
        "leads_descartados": leads_descartados,
        "pipeline_estimado_brl": pipeline_estimado_brl,
        "taxa_conversao_pct": taxa_conversao_pct,
        "score_medio": score_medio,
        # Inteligência de mercado
        "top_objecao": top_objecao,
        "top_objecao_count": top_objecao_count,
        "objection_breakdown": objection_counts,
        "leads_por_origem": leads_por_origem,
    }


def _empty_metrics(client_id: str, days: int, start: datetime, end: datetime) -> dict:
    return {
        "client_id": client_id,
        "period_days": days,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "generated_at": end.isoformat(),
        "total_leads": 0,
        "visitas_confirmadas": 0,
        "leads_quentes": 0,
        "leads_descartados": 0,
        "pipeline_estimado_brl": 0.0,
        "taxa_conversao_pct": 0.0,
        "score_medio": 0.0,
        "top_objecao": None,
        "top_objecao_count": 0,
        "objection_breakdown": {},
        "leads_por_origem": {},
    }


# ─── Formatação da mensagem WhatsApp ─────────────────────────────────────────
def format_whatsapp_message(metrics: dict, imob_name: str = IMOBILIARIA_NAME) -> str:
    """
    Formata relatório executivo para WhatsApp.
    Destinatário: dono da imobiliária. Tom: executivo, conciso.
    """
    pipeline = metrics.get("pipeline_estimado_brl", 0)
    pipeline_fmt = f"R$ {pipeline:,.0f}".replace(",", ".")

    taxa = metrics.get("taxa_conversao_pct", 0)
    top_obj = metrics.get("top_objecao")
    top_obj_count = metrics.get("top_objecao_count", 0)
    period = metrics.get("period_days", 7)

    lines = [
        f"📊 *Relatório Semanal — {imob_name}*",
        f"_{period} dias encerrados_",
        "",
        f"👥 Leads atendidos: *{metrics.get('total_leads', 0)}*",
        f"🔥 Leads quentes (score ≥ 6): *{metrics.get('leads_quentes', 0)}*",
        f"🏠 Visitas confirmadas: *{metrics.get('visitas_confirmadas', 0)}*",
        f"📈 Taxa Sofia → visita: *{taxa}%*",
        f"💰 Pipeline ativo estimado: *{pipeline_fmt}*",
    ]

    if top_obj:
        lines.append(f"⚠️ Principal objeção: *{top_obj}* ({top_obj_count}x)")

    origem = metrics.get("leads_por_origem", {})
    if origem:
        origem_str = " | ".join(f"{k}: {v}" for k, v in sorted(origem.items(), key=lambda x: -x[1]))
        lines.append(f"📍 Origem: {origem_str}")

    lines += [
        "",
        "_Relatório completo disponível no dashboard._",
    ]

    return "\n".join(lines)


# ─── Persistência em disco ────────────────────────────────────────────────────
def save_report_json(metrics: dict) -> Path:
    """Salva relatório em disco para histórico e consumo pelo dashboard."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    client_id = metrics.get("client_id", "unknown")
    path = REPORTS_DIR / f"weekly_{client_id}_{ts}.json"
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    log.info("Relatório JSON salvo: %s", path)
    return path


def load_reports_history(client_id: str = CLIENT_ID, limit: int = 12) -> list[dict]:
    """Carrega histórico de relatórios do disco para o dashboard."""
    if not REPORTS_DIR.exists():
        return []
    reports = []
    pattern = f"weekly_{client_id}_*.json"
    for f in sorted(REPORTS_DIR.glob(pattern), reverse=True)[:limit]:
        try:
            reports.append(json.loads(f.read_text()))
        except Exception:
            pass
    return reports


# ─── Exportação PDF ───────────────────────────────────────────────────────────
def export_pdf(metrics: dict) -> bytes:
    """
    Gera PDF executivo com as métricas do relatório.
    Requer: pip install reportlab
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        )
    except ImportError:
        log.error("reportlab não instalado. Execute: pip install reportlab")
        raise

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=2 * cm, bottomMargin=2 * cm,
        leftMargin=2 * cm, rightMargin=2 * cm,
    )
    styles = getSampleStyleSheet()
    story = []

    # Título
    title_style = ParagraphStyle(
        "Title", parent=styles["Title"],
        fontSize=18, spaceAfter=6,
        textColor=colors.HexColor("#1a1a2e"),
    )
    sub_style = ParagraphStyle(
        "Sub", parent=styles["Normal"],
        fontSize=10, textColor=colors.grey, spaceAfter=20,
    )

    imob = IMOBILIARIA_NAME
    period = metrics.get("period_days", 7)
    generated = metrics.get("generated_at", "")[:10]

    story.append(Paragraph(f"Relatório Executivo — {imob}", title_style))
    story.append(Paragraph(f"Período: {period} dias | Gerado em: {generated}", sub_style))

    # KPIs principais
    pipeline = metrics.get("pipeline_estimado_brl", 0)
    pipeline_fmt = f"R$ {pipeline:,.0f}".replace(",", ".")

    kpi_data = [
        ["Métrica", "Valor"],
        ["Leads atendidos", str(metrics.get("total_leads", 0))],
        ["Leads quentes (score ≥ 6)", str(metrics.get("leads_quentes", 0))],
        ["Visitas confirmadas", str(metrics.get("visitas_confirmadas", 0))],
        ["Taxa Sofia → visita", f"{metrics.get('taxa_conversao_pct', 0)}%"],
        ["Score médio dos leads", str(metrics.get("score_medio", 0))],
        ["Pipeline estimado", pipeline_fmt],
        ["Leads descartados", str(metrics.get("leads_descartados", 0))],
    ]

    top_obj = metrics.get("top_objecao")
    if top_obj:
        kpi_data.append(["Principal objeção", f"{top_obj} ({metrics.get('top_objecao_count', 0)}x)"])

    tbl = Table(kpi_data, colWidths=[9 * cm, 7 * cm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 11),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 0.5 * cm))

    # Origem dos leads
    origem = metrics.get("leads_por_origem", {})
    if origem:
        story.append(Paragraph("Origem dos leads", styles["Heading2"]))
        origem_data = [["Origem", "Quantidade"]] + [
            [k, str(v)] for k, v in sorted(origem.items(), key=lambda x: -x[1])
        ]
        tbl2 = Table(origem_data, colWidths=[9 * cm, 7 * cm])
        tbl2.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d6a4f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(tbl2)

    story.append(Spacer(1, 0.5 * cm))
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    story.append(Paragraph(
        f"Gerado automaticamente por {CONSULTANT_NAME} — ImobOne v2 | {generated}",
        footer_style,
    ))

    doc.build(story)
    buf.seek(0)
    return buf.read()


# ─── Exportação CSV ───────────────────────────────────────────────────────────
def export_csv(metrics: dict) -> str:
    """Gera CSV com as métricas do relatório."""
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["metrica", "valor"])
    writer.writerow(["client_id", metrics.get("client_id", "")])
    writer.writerow(["periodo_dias", metrics.get("period_days", 7)])
    writer.writerow(["periodo_inicio", metrics.get("period_start", "")])
    writer.writerow(["periodo_fim", metrics.get("period_end", "")])
    writer.writerow(["gerado_em", metrics.get("generated_at", "")])
    writer.writerow(["total_leads", metrics.get("total_leads", 0)])
    writer.writerow(["leads_quentes", metrics.get("leads_quentes", 0)])
    writer.writerow(["visitas_confirmadas", metrics.get("visitas_confirmadas", 0)])
    writer.writerow(["taxa_conversao_pct", metrics.get("taxa_conversao_pct", 0)])
    writer.writerow(["score_medio", metrics.get("score_medio", 0)])
    writer.writerow(["pipeline_estimado_brl", metrics.get("pipeline_estimado_brl", 0)])
    writer.writerow(["leads_descartados", metrics.get("leads_descartados", 0)])
    writer.writerow(["top_objecao", metrics.get("top_objecao", "")])
    writer.writerow(["top_objecao_count", metrics.get("top_objecao_count", 0)])

    # Objeções
    writer.writerow([])
    writer.writerow(["objecao", "frequencia"])
    for obj, freq in sorted(
        metrics.get("objection_breakdown", {}).items(), key=lambda x: -x[1]
    ):
        writer.writerow([obj, freq])

    # Origens
    writer.writerow([])
    writer.writerow(["origem", "quantidade"])
    for src, count in sorted(
        metrics.get("leads_por_origem", {}).items(), key=lambda x: -x[1]
    ):
        writer.writerow([src, count])

    return output.getvalue()


# ─── Entry point principal ────────────────────────────────────────────────────
def run_weekly_report(
    client_id: str = CLIENT_ID,
    dry_run: bool = False,
    days: int = 7,
) -> dict:
    """
    Gera e envia o relatório semanal completo.
    Retorna as métricas calculadas.
    """
    log.info("=== Relatório semanal iniciado | cliente=%s | dry_run=%s ===", client_id, dry_run)

    metrics = compute_weekly_metrics(client_id=client_id, days=days)

    # Salva JSON em disco (histórico para o dashboard)
    try:
        save_report_json(metrics)
    except Exception as e:
        log.warning("Não foi possível salvar JSON: %s", e)

    # Formata e envia WhatsApp ao dono
    msg = format_whatsapp_message(metrics)
    if dry_run:
        log.info("[DRY-RUN] Mensagem que seria enviada para %s:\n%s", OPERATOR_NUMBER, msg)
    else:
        sent = _send_whatsapp(OPERATOR_NUMBER, msg)
        if sent:
            log.info("Relatório semanal enviado ao dono (%s).", OPERATOR_NUMBER)
        else:
            log.error("Falha ao enviar relatório via WhatsApp.")

    log.info(
        "Métricas: leads=%d, quentes=%d, visitas=%d, pipeline=R$%.0f, top_obj=%s",
        metrics["total_leads"],
        metrics["leads_quentes"],
        metrics["visitas_confirmadas"],
        metrics["pipeline_estimado_brl"],
        metrics["top_objecao"],
    )
    return metrics


# ─── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] report: %(message)s",
    )

    dry_run = "--dry-run" in sys.argv
    days = 7

    if "--days" in sys.argv:
        try:
            idx = sys.argv.index("--days")
            days = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            log.error("--days requer um número inteiro.")
            sys.exit(1)

    if "--weekly" in sys.argv:
        run_weekly_report(dry_run=dry_run, days=days)

    elif "--export-csv" in sys.argv:
        metrics = compute_weekly_metrics(days=days)
        print(export_csv(metrics))

    elif "--export-pdf" in sys.argv:
        metrics = compute_weekly_metrics(days=days)
        pdf_bytes = export_pdf(metrics)
        out = Path(f"report_{datetime.now().strftime('%Y%m%d')}.pdf")
        out.write_bytes(pdf_bytes)
        log.info("PDF gerado: %s (%d bytes)", out, len(pdf_bytes))

    else:
        print("Uso:")
        print("  python3 report_engine.py --weekly [--dry-run] [--days N]")
        print("  python3 report_engine.py --export-csv [--days N]")
        print("  python3 report_engine.py --export-pdf [--days N]")
        sys.exit(1)
