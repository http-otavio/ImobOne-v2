#!/usr/bin/env python3
"""
objection_engine.py — Detecção, classificação e análise de objeções de leads

ICP: Dono da imobiliária / Dono da construtora
    O dono precisa saber quais objeções estão bloqueando leads para ajustar
    argumentação da equipe e estratégia de portfólio.

Fluxo de detecção (wiring no whatsapp_webhook.py):
    # Após cálculo de score, fire-and-forget:
    asyncio.create_task(detect_and_save_objection(sender, body, client_id))

Pipeline de detecção — Haiku PRIMEIRO, regex como atalho:
    1. Regex de alta confiança — bypass imediato em casos óbvios (sem custo, ~microsegundos).
       Só é considerado "match de alta confiança" se o padrão for específico o suficiente
       para ser inequívoco (ex: "FGTS", "score baixo", "prazo de entrega").
    2. Claude Haiku — classificador primário para toda mensagem substantiva.
       Recebe contexto da conversa (persona da Sofia + produto imobiliário premium).
       Retorna categoria ou "nenhuma". Custo: ~$0.001/chamada. Irrelevante frente
       ao custo de perder uma objeção de lead de R$2M+.
    3. Fallback de regex amplo — usado APENAS quando Haiku não está disponível
       (ANTHROPIC_API_KEY ausente). Nunca é o caminho principal.

    Princípio: leads de alto padrão não dizem "tá caro". Dizem "está um pouco
    acima do que eu esperava para esse perfil de imóvel". O Haiku entende isso.
    O regex, não.

Categorias mapeadas:
    preco         — preço alto, caro, fora do orçamento, expectativa de valor
    prazo         — prazo de entrega longo, quando fica pronto, não quer esperar
    localizacao   — bairro, longe, localização ruim, rotina incompatível
    financiamento — financiamento, banco, crédito, FGTS, score, entrada
    condominio    — taxa de condomínio ou IPTU caro
    concorrente   — comparação com outro produto/imobiliária, outra proposta
    outros        — objeção detectada mas não classificável nas categorias acima

Migration necessária:
    ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS objections_detected JSONB DEFAULT '[]'::jsonb;
    (ver migrations/add_objections_detected.sql)

Uso standalone:
    python3 objection_engine.py --classify "o preço tá muito caro"
    python3 objection_engine.py --classify "está um pouco acima do que eu esperava"
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

# ─── Padrões de ALTA CONFIANÇA (bypass do Haiku) ─────────────────────────────
#
# Estes padrões são usados APENAS como atalho de velocidade — quando o match
# é tão inequívoco que chamar o Haiku seria redundante.
#
# Critério de inclusão: o padrão deve ser ESPECÍFICO o suficiente para que
# nenhuma outra interpretação seja razoável. Termos vagos (ex: "caro", "longe")
# NÃO entram aqui — ficam para o Haiku julgar em contexto.
#
# Exemplo de raciocínio:
#   "FGTS" → inequívoco, sempre é contexto de financiamento → entra
#   "caro" → pode ser comentário positivo ("caro cliente") ou em contexto
#             irônico → NÃO entra, Haiku decide
#
_HIGH_CONFIDENCE_PATTERNS: dict[str, list[str]] = {
    "financiamento": [
        r"\bfgts\b",
        r"\bserasa\b",
        r"\bspc\b",
        r"\bscore\b.{0,30}(baixo|ruim|negativado)",
        r"financiamento\s+(negado|recusado|n[aã]o\s+saiu|n[aã]o\s+foi\s+aprovado)",
        r"cr[eé]dito\s+(negado|recusado|n[aã]o\s+passou|bloqueado)",
        r"(negado|recusado|rejeitado)\s+(no|pelo)\s+banco",
        r"restri[cç][aã]o\s+(no\s+cpf|cadastral|financeira)",
        r"nome\s+(sujo|negativado|restrito)",
    ],
    "prazo": [
        r"prazo\s+de\s+entrega",
        r"data\s+de\s+entrega",
        r"previs[aã]o\s+de\s+entrega",
        r"quando\s+(fica\s+)?pronto",
    ],
    "concorrente": [
        r"outra\s+imobili",  # cobre imobiliária (á não casa com literal 'a' em regex ASCII)
        r"outra\s+proposta",
        r"outro\s+empreendimento",
        r"j[aá]\s+tenho\s+(uma\s+)?(proposta|oferta)",
        r"concorr[eê]nte",
    ],
    "condominio": [
        r"taxa\s+de\s+condom[ií]nio",
        r"condom[ií]nio\s+(muito\s+)?(caro|alto|elevado|absurdo)",
        r"iptu\s+(muito\s+)?(alto|caro|elevado|absurdo)",
    ],
}

# Pré-compila padrões de alta confiança
_COMPILED_HIGH_CONFIDENCE: dict[str, list[re.Pattern]] = {
    cat: [re.compile(p, re.IGNORECASE) for p in patterns]
    for cat, patterns in _HIGH_CONFIDENCE_PATTERNS.items()
}

# ─── Padrões amplos — fallback APENAS quando Haiku indisponível ───────────────
#
# Usados como plano B quando ANTHROPIC_API_KEY não está configurado.
# Não são o caminho principal. Aceitam mais falsos positivos porque
# sem Haiku, melhor ter mais dados do que nenhum.
#
_FALLBACK_PATTERNS: dict[str, list[str]] = {
    "condominio": [
        r"condom[ií]nio",
        r"iptu\s+(alto|caro|elevado)",
    ],
    "concorrente": [
        r"outra\s+imobiliar",
        r"outro\s+empreendimento",
        r"outra\s+proposta",
        r"concorr[eê]nte",
    ],
    "financiamento": [
        r"\bfgts\b", r"\bserasa\b", r"\bspc\b",
        r"financiamento\s+(negado|recusado)",
        r"cr[eé]dito\s+(negado|ruim)",
        r"score\s+(baixo|ruim)",
        r"entrada\s+(alta|cara|pesada)",
    ],
    "prazo": [
        r"prazo\s+de\s+entrega",
        r"quando\s+(fica\s+)?pronto",
        r"demora\s+(muito|demais)",
        r"n[aã]o\s+quero\s+esperar",
    ],
    "localizacao": [
        r"muito\s+longe",
        r"longe\s+(do\s+trabalho|da\s+escola|do\s+metr[ôo])",
        r"bairro\s+(ruim|perigoso|n[aã]o\s+gostei)",
        r"n[aã]o\s+gostei\s+do\s+bairro",
        r"tr[âa]nsito\s+(horr[íi]vel|p[éê]ssimo|pesado)",
    ],
    "preco": [
        r"(muito\s+)?caro",
        r"pre[çc]o\s+(alto|elevado|abusivo|absurdo)",
        r"(fora|acima)\s+do\s+(meu\s+)?or[çc]amento",
        r"n[aã]o\s+(consigo|tenho)\s+(pagar|arcar)",
        r"valor\s+(alto|elevado|abusivo)",
        r"mais\s+barato",
        r"\bdesconto\b",
    ],
    "outros": [],
}

_COMPILED_FALLBACK: dict[str, list[re.Pattern]] = {
    cat: [re.compile(p, re.IGNORECASE) for p in patterns]
    for cat, patterns in _FALLBACK_PATTERNS.items()
}


# ─── Atalho de alta confiança (regex inequívoco) ─────────────────────────────
def _check_high_confidence_regex(message: str) -> Optional[str]:
    """
    Verifica padrões inequívocos que dispensam o Haiku.
    Só retorna categoria se o match for específico o suficiente para que
    nenhuma outra interpretação seja razoável (ex: "FGTS", "crédito negado").
    Retorna None para qualquer ambiguidade — o Haiku decide.
    """
    for category, compiled_patterns in _COMPILED_HIGH_CONFIDENCE.items():
        for pattern in compiled_patterns:
            if pattern.search(message):
                return category
    return None


def _check_fallback_regex(message: str) -> Optional[str]:
    """
    Regex amplo usado APENAS quando Haiku não está disponível.
    Aceita mais falsos positivos deliberadamente — sem Haiku, melhor
    ter dados imperfeitos do que nenhum dado.
    """
    for category, compiled_patterns in _COMPILED_FALLBACK.items():
        if not compiled_patterns:
            continue
        for pattern in compiled_patterns:
            if pattern.search(message):
                return category
    return None


# ─── Classificação via Claude Haiku ──────────────────────────────────────────
def classify_with_haiku(message: str) -> Optional[str]:
    """
    Classificador primário de objeções.

    Recebe a mensagem do lead e retorna a categoria da objeção ou None.
    Entende paráfrases, linguagem sofisticada, objeções implícitas e
    expressões regionais — o que regex estruturalmente não consegue.

    Custo: ~$0.001/chamada. Para leads de R$2M+, o custo de perder
    uma objeção é ordens de magnitude maior.

    Retorna None se não houver objeção na mensagem.
    Retorna "outros" se houver objeção mas não se encaixar nas categorias.
    """
    if not ANTHROPIC_API_KEY:
        return None

    categories_desc = "\n".join([
        "- preco: valor acima da expectativa, fora do orçamento, quer negociar, acha caro (mesmo sem usar a palavra)",
        "- prazo: prazo de entrega longo, demora para ficar pronto, não quer esperar",
        "- localizacao: bairro não funciona para a rotina, localização distante ou problemática",
        "- financiamento: crédito negado, score baixo, FGTS, entrada alta, dificuldade de aprovação",
        "- condominio: taxa de condomínio ou IPTU acima do esperado",
        "- concorrente: já tem outra proposta, está comparando com outro produto ou imobiliária",
        "- outros: há uma objeção clara mas não se encaixa nas categorias acima",
        "- nenhuma: não há objeção — pode ser dúvida, elogio, agendamento, saudação ou qualquer coisa sem resistência",
    ])

    prompt = (
        "Você analisa mensagens de leads interessados em imóveis de alto padrão no Brasil (R$2M+).\n"
        "Identifique se há uma OBJEÇÃO na mensagem abaixo e classifique em UMA categoria.\n"
        "Objeção = qualquer sinal de resistência, hesitação, barreira ou preocupação que pode "
        "impedir a compra. Pode ser explícita ('tá caro') ou implícita ('está um pouco acima do que eu esperava').\n"
        "Responda APENAS com o nome da categoria, nada mais.\n\n"
        f"Categorias:\n{categories_desc}\n\n"
        f'Mensagem do lead: "{message}"\n\n'
        "Categoria:"
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
            log.debug("Haiku retornou categoria desconhecida '%s' — mapeando para 'outros'", category)
            return "outros"
    except Exception as e:
        log.warning("Haiku classification failed: %s", e)
        return None


# ─── Detecção completa ────────────────────────────────────────────────────────
def detect_objection(message: str, use_haiku: bool = True) -> Optional[str]:
    """
    Pipeline de detecção com Haiku como classificador primário.

    Ordem:
    1. Filtra mensagens curtas/vazias — sem custo.
    2. Regex de alta confiança — bypass do Haiku em casos inequívocos
       (ex: "FGTS", "crédito negado", "prazo de entrega"). Sem falsos positivos.
    3. Claude Haiku — classificador principal para todo o resto.
       Entende paráfrases, linguagem sofisticada, objeções implícitas.
    4. Regex amplo — fallback APENAS se Haiku indisponível (sem API key).
       Aceita mais falsos positivos; melhor que nenhum dado.

    Retorna categoria ou None.
    """
    if not message or len(message.strip()) < MIN_MSG_LEN:
        return None

    # Passo 1: atalho inequívoco — sem custo, sem ambiguidade
    high_conf = _check_high_confidence_regex(message)
    if high_conf:
        log.debug("Objeção detectada via regex alta confiança: %s", high_conf)
        return high_conf

    # Passo 2: Haiku como classificador primário
    if use_haiku and ANTHROPIC_API_KEY:
        return classify_with_haiku(message)

    # Passo 3: fallback sem Haiku — regex amplo
    return _check_fallback_regex(message)


# Mantido para compatibilidade com testes legados que chamam diretamente
def detect_objection_regex(message: str) -> Optional[str]:
    """
    Alias legado. Em produção, usar detect_objection() que inclui Haiku.
    Verifica APENAS os padrões de alta confiança (inequívocos).
    """
    return _check_high_confidence_regex(message)


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
