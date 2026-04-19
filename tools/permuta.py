"""
tools/permuta.py — Detecção e Qualificação de Permutas (Imóvel por Imóvel)

Pipeline quando lead menciona troca/permuta/outro imóvel:
    1. Regex de alta confiança detecta menção a permuta (5+ padrões linguísticos)
    2. Flag permuta_detectada setada no Redis por sender (TTL 30 dias)
    3. Claude Haiku extrai dados do ativo oferecido (tipo, localização, valor estimado)
    4. Dados salvos em leads.permuta_dados (JSONB no Supabase)
    5. score_breakdown acrescenta categoria 'permuta' (+3 pts se ativo de alto padrão)
    6. Seção 'Análise de Permuta' injetada no briefing do corretor

Design decisions:
    - Detecção via regex, não LLM — baixo custo, sem latência, sem alucinação no trigger
    - Extração dos dados do ativo via Claude Haiku — linguagem implícita e variada
    - Score +3 configurável por cliente no onboarding.json (campo permuta_score_bonus)
    - Fire-and-forget via asyncio.create_task no webhook — não bloqueia resposta ao lead
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("permuta")

# ─── Configuração ─────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY", "")

try:
    import anthropic as _anthropic_module
except ImportError:
    _anthropic_module = None  # type: ignore[assignment]


# ─── Padrões de detecção de permuta ───────────────────────────────────────────

# 5+ variações linguísticas que leads de alto padrão usam para mencionar permuta
# Inclui linguagem formal, informal e implícita (sinônimos e construções equivalentes)
_PERMUTA_PATTERNS = [
    # Termos diretos
    r"\bpermuta\b",
    r"\btroca\b",
    r"\btrocar\b",
    # Linguagem implícita — "tenho um imóvel / apartamento / casa"
    r"\btenho\s+(um|uma)\s+(im[oó]vel|apartamento|casa|cobertura|terreno|sala)",
    r"\bpossuo\s+(um|uma)\s+(im[oó]vel|apartamento|casa|cobertura|terreno|sala)",
    # "oferecer / dar / usar como entrada / parte do pagamento"
    r"\bdar\s+como\s+entrada\b",
    r"\busado?\s+como\s+entrada\b",
    r"\bdar\s+como\s+parte\b",
    r"\bparte\s+do\s+pagamento\s+com\s+(im[oó]vel|apartamento|casa)",
    r"\bim[oó]vel\s+como\s+(parte|entrada)\b",
    # "trocar meu/minha ... pelo/pela ..."
    r"\btrocar\s+(meu|minha)\b",
    r"\bpermutando\b",
    r"\bpermutado\b",
    # Construções sofisticadas de alto padrão
    r"\bintegrali[sz]ar\s+com\b",
    r"\bdar\s+entrada\s+com\s+(o|a)\s+(meu|minha)\b",
    r"\butilizar\s+(meu|minha|um|uma)\s+(im[oó]vel|apartamento|casa)",
    # "financiar a diferença" implica que já tem um bem como parte
    r"\bfinanciar\s+(a\s+)?diferença\b",
]

_PERMUTA_RE = re.compile(
    "|".join(_PERMUTA_PATTERNS),
    re.IGNORECASE | re.UNICODE,
)

# ─── Prompt de extração do ativo de permuta ───────────────────────────────────

_EXTRACTION_PROMPT = """\
Você é um especialista em imóveis de alto padrão.
Analise o histórico de conversa abaixo e extraia os dados do imóvel que o lead quer
oferecer como parte do pagamento (troca/permuta).

HISTÓRICO DA CONVERSA:
{history}

Se o lead mencionou um imóvel para trocar ou usar como entrada, extraia:
{{
  "tipo_ativo":    "apartamento | casa | cobertura | terreno | sala_comercial | outro",
  "bairro":        "bairro ou região mencionada, ou null",
  "cidade":        "cidade, ou null",
  "metragem":      número em m² ou null,
  "valor_estimado": valor em reais estimado pelo lead ou mencionado, ou null,
  "caracteristicas": ["características mencionadas — ex: varanda, vista, reformado"],
  "descricao_lead": "transcrição literal ou paráfrase do que o lead disse sobre o ativo"
}}

Se o lead não forneceu detalhes suficientes, deixe os campos como null.
Responda APENAS com JSON válido. Nenhum texto antes ou depois.
"""


# ─── Helpers HTTP ─────────────────────────────────────────────────────────────

def _make_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def _supabase_headers() -> dict:
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


def _supabase_patch(path: str, payload: dict) -> bool:
    """PATCH para Supabase REST API. Retorna True se bem-sucedido."""
    url  = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(url, data=data,
                                   headers=_supabase_headers(), method="PATCH")
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=10):
            return True
    except Exception as e:
        log.error("[PERMUTA] Falha ao atualizar Supabase: %s", e)
        return False


# ─── 1. Detecção via regex ─────────────────────────────────────────────────────

def detect_permuta(message: str) -> bool:
    """
    Retorna True se a mensagem do lead contém menção a permuta/troca.
    Usa regex de alta confiança — não aciona LLM para reduzir custo e latência.
    Cobre 5+ variações linguísticas usadas por leads de alto padrão.
    """
    return bool(_PERMUTA_RE.search(message))


# ─── 2. Extração dos dados do ativo via Claude Haiku ──────────────────────────

def extract_permuta_data(conversation_history: list[dict]) -> dict:
    """
    Claude Haiku analisa o histórico da conversa e extrai dados estruturados
    do ativo que o lead quer oferecer como permuta.

    conversation_history: lista de dicts com 'role' ('user'/'assistant') e 'content'.
    Síncrono — projetado para run_in_executor.
    Lança exceção se JSON não puder ser parseado.
    """
    if _anthropic_module is None:
        raise ImportError("anthropic não instalado")

    # Formata histórico como texto para o prompt
    history_text = "\n".join(
        f"{'Lead' if m.get('role') == 'user' else 'Consultor'}: {m.get('content', '')[:300]}"
        for m in conversation_history[-10:]  # últimas 10 mensagens
    )

    client = _anthropic_module.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": _EXTRACTION_PROMPT.format(history=history_text),
        }],
    )
    raw = msg.content[0].text.strip()

    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()

    data = json.loads(raw)
    data["extracted_at"] = datetime.now(timezone.utc).isoformat()
    return data


# ─── 3. Cálculo do bonus de score ─────────────────────────────────────────────

def calculate_permuta_score_bonus(
    permuta_data: dict,
    onboarding: dict,
) -> int:
    """
    Retorna o bonus de pontuação para leads com permuta.
    Default: +3 pts se ativo de alto padrão (valor >= 1M ou tipologia premium).
    Configurável via onboarding.json: campo 'permuta_score_bonus'.

    Critérios de ativo de alto padrão:
        - valor_estimado >= 1.000.000
        - tipo_ativo in (apartamento, cobertura, casa) — não terreno genérico
    """
    default_bonus = onboarding.get("permuta_score_bonus", 3)

    valor = permuta_data.get("valor_estimado") or 0
    tipo  = (permuta_data.get("tipo_ativo") or "").lower()

    alto_padrao_tipos = {"apartamento", "cobertura", "casa"}
    is_alto_padrao = (valor >= 1_000_000) or (tipo in alto_padrao_tipos and valor >= 500_000)

    return default_bonus if is_alto_padrao else 1


# ─── 4. Seção de briefing para o corretor ─────────────────────────────────────

def format_permuta_briefing_section(permuta_data: dict) -> str:
    """
    Gera a seção 'Análise de Permuta' para incluir no briefing do corretor.
    Tom: conciso, objetivo, dados relevantes para negociação.
    """
    tipo  = permuta_data.get("tipo_ativo") or "imóvel não especificado"
    bairro = permuta_data.get("bairro")
    cidade = permuta_data.get("cidade")
    valor = permuta_data.get("valor_estimado")
    caract = permuta_data.get("caracteristicas") or []
    descricao = permuta_data.get("descricao_lead") or ""

    lines = ["*Análise de Permuta*"]
    lines.append(f"Ativo oferecido: {tipo.title()}")
    if bairro:
        loc = bairro
        if cidade:
            loc += f", {cidade}"
        lines.append(f"Localização: {loc}")
    if valor:
        lines.append(f"Valor estimado pelo lead: R$ {valor:,.0f}".replace(",", "."))
    if caract:
        lines.append(f"Características mencionadas: {', '.join(caract)}")
    if descricao:
        lines.append(f"O que disse: \"{descricao[:200]}\"")
    lines.append("→ Verificar se aceita permuta ou pode usar como entrada na proposta.")

    return "\n".join(lines)


# ─── 5. Persistência no Supabase ──────────────────────────────────────────────

def save_permuta_data(
    lead_phone: str,
    client_id: str,
    permuta_data: dict,
    score_bonus: int,
) -> bool:
    """
    Atualiza o lead no Supabase com dados de permuta e flag.
    Síncrono — projetado para run_in_executor.
    """
    path    = f"leads?lead_phone=eq.{lead_phone}&client_id=eq.{client_id}"
    payload = {
        "permuta_detectada":    True,
        "permuta_dados":        permuta_data,
        "permuta_detectada_em": datetime.now(timezone.utc).isoformat(),
    }
    ok = _supabase_patch(path, payload)
    if ok:
        log.info("[PERMUTA] Dados de permuta salvos para lead %s", lead_phone)
    return ok
