#!/usr/bin/env python3
"""
objection_engine.py — Detecção, classificação e análise de objeções de leads

ICP: Dono da imobiliária / Dono da construtora
    O dono precisa saber quais objeções estão bloqueando leads para ajustar
    argumentação da equipe e estratégia de portfólio.

Fluxo de detecção (wiring no whatsapp_webhook.py):
    # Após cálculo de score, fire-and-forget:
    asyncio.create_task(detect_and_save_objection(sender, body, client_id))

Pipeline de detecção:
    1. Regex — rápido, sem custo. Cobre ~80% dos casos.
    2. Claude Haiku — fallback para mensagens substantivas sem match regex.
       Custo: ~$0.001 por chamada. Skippado se ANTHROPIC_API_KEY ausente.

Categorias mapeadas:
    preco         — preço alto, caro, fora do orçamento
    prazo         — prazo de entrega longo, quando fica pronto
    localizacao   — bairro, longe, localização ruim
    financiamento — financiamento, banco, crédito, FGTS, score
    condominio    — taxa de condomínio ou IPTU caro
    concorrente   — comparação com outro produto/imobiliária
    outros        — objeção detectada mas não classificada

Migration necessária:
    ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS objections_detected JSONB DEFAULT '[]'::jsonb;
    (ver migrations/add_objections_detected.sql)

Uso standalone:
    python3 objection_engine.py --classify "o preço tá muito caro"
    python3 objection_engine.py --report --client-id demo_imobiliaria_vendas
    python3 objection_engine.py --report --days 30
"""

import json
import logging
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("objection_engine")

# ─── Config ───────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLIENT_ID         = os.getenv("DEMO_CLIENT_ID", "demo_imobiliaria_vendas")
IMOBILIARIA_NAME  = os.getenv("IMOBILIARIA_NAME", "Ávora Imóveis")

# Tamanho mínimo de mensagem para tentativa de classificação
# (evita classificar "ok", "sim", "entendi", etc.)
MIN_MSG_LEN = 15

# Modelo Haiku para classificação
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ─── Categorias e padrões regex ───────────────────────────────────────────────
OBJECTION_CATEGORIES = [
    "preco",
    "prazo",
    "localizacao",
    "financiamento",
    "condominio",
    "concorrente",
    "outros",
]

CATEGORY_LABELS: dict[str, str] = {
    "preco":         "💰 Preço/Valor",
    "prazo":         "⏳ Prazo de Entrega",
    "localizacao":   "📍 Localização",
    "financiamento": "🏦 Financiamento/Crédito",
    "condominio":    "🏢 Condomínio/IPTU",
    "concorrente":   "⚔️ Concorrente",
    "outros":        "❓ Outros",
}

# Padrões por categoria (lowercase — não são case-sensitive)
# ATENÇÃO: ordem importa — categorias mais específicas primeiro para evitar
# falsos positivos do padrão genérico 'caro' de preco.
_PATTERNS: dict[str, list[str]] = {
    "condominio": [
        r"condom[iíi]nio",
        r"taxa\s+de\s+condom[iíi]nio",
        r"iptu\s+(alto|caro|elevado|absurdo)",
        r"custo\s+de\s+manuten",
    ],
    "concorrente": [
        r"outra\s+imobili",
        r"outro\s+empreendimento",
        r"vi\s+(em\s+outro|num\s+outro|em\s+outra)",
        r"concorr[eê]nte",
        r"outra\s+proposta",
        r"prefiro\s+o\s+outro",
    ],
    "financiamento": [
        r"financiamento\s+(negado|recusado|n[aã]o\s+saiu)",
        r"cr.dito\s+(negado|recusado|n[aã]o\s+passou|ruim)",
        r"(negado|recusado|rejeitado)\s+(no|pelo)\s+banco",
        r"score\s+(baixo|ruim)",
        r"\bfgts\b",
        r"banco\s+(n[aã]o|recusou|negou)",
        r"(n[aã]o\s+)?consigo\s+financiar",
        r"restri..o\s+(no\s+cpf|cadastral|financeira)",
        r"entrada\s+(muito\s+)?(alta|cara|pesada)",
        r"serasa|spc\b",
    ],
    "prazo": [
        r"prazo\s+de\s+entrega",
        r"quando\s+(fica\s+)?(pronto|entrega[rm]?)",
        r"demora\s+(muito|demais|\d+\s+anos)",
        r"muitos?\s+anos?\s+(de\s+)?espera",
        r"data\s+de\s+entrega",
        r"previs..o\s+de\s+entrega",
        r"n[aã]o\s+quero\s+esperar",
    ],
    "localizacao": [
        r"localiza..o\s+(ruim|p.ssima|longe|n[aã]o\s+gostei)",
        r"muito\s+longe\s+(do|da|de)",
        r"longe\s+(do\s+trabalho|da\s+escola|do\s+metr|do\s+centro)",
        r"bairro\s+(ruim|perigoso|barulhento|n[aã]o\s+gostei)",
        r"sem\s+(infraestrutura|transporte|metr|.nibus)",
        r"n[aã]o\s+gostei\s+do\s+bairro",
        r"longe\s+demais",
        r"dif.cil\s+(acesso|chegar)",
        r"tr.nsito\s+(horr.vel|p.ssimo|pesado|terr.vel)",
    ],
    "preco": [
        r"(muito\s+)?caro",
        r"pre.o\s+(alto|elevado|pesado|abusivo|absurdo)",
        r"preco\s+(alto|elevado|pesado|abusivo|absurdo)",
        r"(fora|acima)\s+do\s+(meu\s+)?or.amento",
        r"n[aã]o\s+(consigo|tenho)\s+(pagar|arcar)",
        r"valor\s+(alto|elevado|pesado|abusivo|absurdo)",
        r"\bmais\s+barato\b",
        r"\bdesconto\b",
        r"\bnegocia[rr]\b",
        r"n[aã]o\s+cabe\s+no\s+(meu\s+)?bolso",
    ],
    "outros": [],
}

# Pré-compila todos os padrões
_COMPILED: dict[str, list[re.Pattern]] = {
    cat: [re.compile(p, re.IGNORECASE) for p in patterns]
    for cat, patterns in _PATTERNS.items()
}


# ─── Detecção por regex ───────────────────────────────────────────────────────
def detect_objection_regex(message: str) -> Optional[str]:
    """
    Tenta classificar a objeção via regex.
    Retorna a categoria ou None se não detectou.
    Determinístico, sem custo, ~microsegundos.
    """
    for category, compiled_patterns in _COMPILED.items():
        for pattern in compiled_patterns:
            if pattern.search(message):
                return category
    return None


# ─── Classificação via Claude Haiku ──────────────────────────────────────────
def classify_with_haiku(message: str) -> Optional[str]:
    """
    Usa Claude Haiku para classificar objeção quando regex não detectou.
    Retorna categoria ou None se não houver objeção.

    Custo: ~$0.001 por chamada.
    Skippado automaticamente se ANTHROPIC_API_KEY não estiver configurado.
    """
    if not ANTHROPIC_API_KEY:
        return None

    categories_desc = "\n".join([
        "- preco: preço alto, fora do orçamento, caro, quer desconto",
        "- prazo: prazo de entrega longo, demora, não quer esperar",
        "- localizacao: bairro ruim, localização distante ou problemática",
        "- financiamento: crédito negado, score baixo, FGTS, renda insuficiente",
        "- condominio: taxa de condomínio ou IPTU caro",
        "- concorrente: já tem outra proposta ou preferência por outro produto",
        "- outros: objeção que não se encaixa nas outras categorias",
        "- nenhuma: não há objeção nesta mensagem",
    ])

    prompt = (
        f"Classifique a objeção na mensagem abaixo em UMA categoria.\n"
        f"Responda APENAS com o nome da categoria, nada mais.\n\n"
        f"Categorias:\n{categories_desc}\n\n"
        f'Mensagem: "{message}"\n\n'
        f"Categoria:"
    )

    payload = json.dumps({
        "model": HAIKU_MODEL,
        "max_tokens": 20,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            resp = json.loads(r.read())
            category = resp["content"][0]["text"].strip().lower()
            if category == "nenhuma":
                return None
            if category in OBJECTION_CATEGORIES:
                return category
            return "outros"
    except Exception as e:
        log.warning("Haiku classification failed: %s", e)
        return None


# ─── Detecção completa ────────────────────────────────────────────────────────
def detect_objection(message: str, use_haiku: bool = True) -> Optional[str]:
    """
    Pipeline completo de detecção:
    1. Regex (rápido, sem custo)
    2. Claude Haiku se regex não detectou e mensagem é substantiva

    Retorna categoria ou None.
    """
    if not message or len(message.strip()) < MIN_MSG_LEN:
        return None

    category = detect_objection_regex(message)
    if category:
        return category

    if use_haiku and ANTHROPIC_API_KEY:
        return classify_with_haiku(message)

    return None


# ─── Supabase helpers ─────────────────────────────────────────────────────────
def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


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
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error("Supabase GET %s: %s", path, e)
        return []


def _sb_patch(path: str, params: str, body: dict) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/{path}{'?' + params if params else ''}"
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=15) as r:
            return r.status < 300
    except Exception as e:
        log.error("Supabase PATCH %s: %s", path, e)
        return False


# ─── Persistência de objeção no Supabase ─────────────────────────────────────
def _append_objection_to_lead(
    phone: str,
    client_id: str,
    entry: dict,
) -> bool:
    """
    Faz append da nova objeção ao array JSONB objections_detected do lead.
    Deduplicação: a mesma categoria não é registrada mais de uma vez por dia.
    """
    encoded_phone = urllib.parse.quote(phone)
    encoded_client = urllib.parse.quote(client_id)
    rows = _sb_get(
        "leads",
        f"lead_phone=eq.{encoded_phone}&client_id=eq.{encoded_client}"
        "&select=id,objections_detected",
    )
    if not rows:
        log.warning("Lead não encontrado para append de objeção: %s", phone)
        return False

    lead = rows[0]
    lead_id = lead.get("id")
    current = lead.get("objections_detected") or []
    if isinstance(current, str):
        try:
            current = json.loads(current)
        except Exception:
            current = []

    # Deduplicação: mesma categoria no mesmo dia não é registrada novamente
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    already_today = any(
        isinstance(e, dict)
        and e.get("categoria") == entry["categoria"]
        and e.get("detectado_em", "").startswith(hoje)
        for e in current
    )
    if already_today:
        log.debug(
            "Objeção '%s' já registrada hoje para %s — skip",
            entry["categoria"], phone,
        )
        return True

    current.append(entry)
    return _sb_patch(
        "leads",
        f"id=eq.{lead_id}",
        {"objections_detected": json.dumps(current)},
    )


async def detect_and_save_objection(
    phone: str,
    message: str,
    client_id: str = CLIENT_ID,
    use_haiku: bool = True,
    _append_fn=None,   # injetável em testes
) -> Optional[str]:
    """
    Detecta objeção na mensagem do lead e persiste em leads.objections_detected.

    Projetado para ser chamado como fire-and-forget pelo webhook:
        asyncio.create_task(detect_and_save_objection(sender, body, client_id))

    Retorna a categoria detectada ou None.
    """
    category = detect_objection(message, use_haiku=use_haiku)
    if not category:
        return None

    entry = {
        "categoria": category,
        "mensagem_preview": message[:100],
        "detectado_em": datetime.now(timezone.utc).isoformat(),
    }

    append_fn = _append_fn or _append_objection_to_lead
    try:
        append_fn(phone, client_id, entry)
    except Exception as e:
        log.error("Erro ao salvar objeção para %s: %s", phone, e)

    log.info(
        "Objeção detectada | client=%s lead=%s categoria=%s",
        client_id, phone, category,
    )
    return category


# ─── Relatório de objeções ────────────────────────────────────────────────────
def compute_objection_report(
    client_id: str = CLIENT_ID,
    days: int = 7,
    top_n: int = 3,
    _leads_override: Optional[list] = None,
) -> dict:
    """
    Agrega objeções detectadas no período e retorna ranking executivo.

    Retorna dict com:
        client_id, period_days, period_start, period_end,
        total_leads, leads_com_objecao, taxa_objecao_pct,
        top_objections (lista top_n com count + pct_leads),
        breakdown (todas as categorias com frequência),
        generated_at
    """
    now = datetime.now(timezone.utc)
    period_start = now - timedelta(days=days)
    period_start_str = period_start.strftime("%Y-%m-%dT%H:%M:%SZ")

    if _leads_override is not None:
        leads = _leads_override
    else:
        encoded_start = urllib.parse.quote(period_start_str)
        leads = _sb_get(
            "leads",
            (
                f"client_id=eq.{urllib.parse.quote(client_id)}"
                f"&created_at=gte.{encoded_start}"
                f"&select=lead_phone,objections_detected"
            ),
        )

    total_leads = len(leads)
    objection_counts: dict[str, int] = {}
    leads_com_objecao = 0

    for lead in leads:
        raw = lead.get("objections_detected") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []

        if not raw:
            continue

        leads_com_objecao += 1

        # Conta uma vez por lead por categoria (não duplica objeções do mesmo lead)
        seen_in_lead: set[str] = set()
        for obj in raw:
            cat = obj.get("categoria", "outros") if isinstance(obj, dict) else str(obj)
            if cat not in seen_in_lead:
                objection_counts[cat] = objection_counts.get(cat, 0) + 1
                seen_in_lead.add(cat)

    taxa_objecao_pct = (
        round(leads_com_objecao / total_leads * 100, 1) if total_leads > 0 else 0.0
    )

    sorted_objections = sorted(objection_counts.items(), key=lambda x: -x[1])
    top_objections = [
        {
            "categoria": cat,
            "label": CATEGORY_LABELS.get(cat, cat),
            "count": count,
            "pct_leads": round(count / total_leads * 100, 1) if total_leads > 0 else 0.0,
        }
        for cat, count in sorted_objections[:top_n]
    ]

    return {
        "client_id": client_id,
        "period_days": days,
        "period_start": period_start.isoformat(),
        "period_end": now.isoformat(),
        "generated_at": now.isoformat(),
        "total_leads": total_leads,
        "leads_com_objecao": leads_com_objecao,
        "taxa_objecao_pct": taxa_objecao_pct,
        "top_objections": top_objections,
        "breakdown": dict(sorted_objections),
    }


# ─── Formatação WhatsApp ──────────────────────────────────────────────────────
def format_objection_whatsapp(
    report: dict,
    imob_name: str = IMOBILIARIA_NAME,
) -> str:
    """
    Formata relatório de objeções para WhatsApp do dono.
    Tom: executivo, direto ao ponto. Destinatário: dono da imobiliária.
    """
    period   = report.get("period_days", 7)
    total    = report.get("total_leads", 0)
    com_obj  = report.get("leads_com_objecao", 0)
    taxa     = report.get("taxa_objecao_pct", 0)
    top      = report.get("top_objections", [])

    lines = [
        f"🧠 *Análise de Objeções — {imob_name}*",
        f"_{period} dias | {com_obj}/{total} leads com objeção ({taxa}%)_",
        "",
    ]

    if not top:
        lines.append("✅ Nenhuma objeção relevante detectada no período.")
    else:
        lines.append("*Top objeções que bloquearam leads:*")
        for i, obj in enumerate(top, 1):
            label = obj.get("label") or CATEGORY_LABELS.get(obj["categoria"], obj["categoria"])
            lines.append(
                f"{i}. {label}: *{obj['count']} leads* ({obj['pct_leads']}%)"
            )

    lines += [
        "",
        "_Histórico completo: GET /reports/objections_",
    ]
    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if "--classify" in sys.argv:
        idx = sys.argv.index("--classify")
        msg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if not msg:
            print("Uso: python3 objection_engine.py --classify 'mensagem aqui'")
            sys.exit(1)
        regex_result = detect_objection_regex(msg)
        if regex_result:
            print(f"Categoria: {regex_result} (via regex)")
        else:
            haiku_result = classify_with_haiku(msg) if ANTHROPIC_API_KEY else None
            if haiku_result:
                print(f"Categoria: {haiku_result} (via Haiku)")
            else:
                print("Nenhuma objeção detectada")

    elif "--report" in sys.argv:
        client_id = CLIENT_ID
        if "--client-id" in sys.argv:
            idx = sys.argv.index("--client-id")
            client_id = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else CLIENT_ID
        days = 7
        if "--days" in sys.argv:
            idx = sys.argv.index("--days")
            try:
                days = int(sys.argv[idx + 1])
            except (IndexError, ValueError):
                pass
        report = compute_objection_report(client_id=client_id, days=days)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print()
        print(format_objection_whatsapp(report))

    else:
        print("Uso:")
        print("  python3 objection_engine.py --classify 'mensagem do lead'")
        print("  python3 objection_engine.py --report [--client-id X] [--days N]")
        sys.exit(1)
