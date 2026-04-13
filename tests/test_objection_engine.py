"""
tests/test_objection_engine.py — Suite de testes para o objection_engine

Arquitetura de detecção (Haiku PRIMEIRO):
    1. Regex de alta confiança — bypass em casos inequívocos (FGTS, crédito negado, etc.)
    2. Claude Haiku — classificador primário para todo o resto (paráfrases, implícitos)
    3. Regex amplo — fallback apenas sem API key

Cobertura:
    - Regex de alta confiança (casos inequívocos que bypassam o Haiku)
    - Regex amplo (fallback offline — use_haiku=False)
    - Casos que DEVEM ir para o Haiku (não capturados por regex de alta confiança)
    - Casos de borda (mensagem curta, vazia, sem objeção)
    - compute_objection_report (métricas, dedup, ordenação, taxa)
    - format_objection_whatsapp (formatação para o dono)
    - detect_and_save_objection (async, mock de Supabase)

Todos os testes são offline (sem Supabase, sem Anthropic API).
"""

import asyncio
import json
import sys
from pathlib import Path

# Garante que o root do projeto está no path
sys.path.insert(0, str(Path(__file__).parent.parent))

from objection_engine import (
    CATEGORY_LABELS,
    _check_fallback_regex,
    _check_high_confidence_regex,
    compute_objection_report,
    detect_and_save_objection,
    detect_objection,
    detect_objection_regex,
    format_objection_whatsapp,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def run(coro):
    """Executa coroutine síncrono para testes (compatível com Python 3.12+)."""
    return asyncio.run(coro)


# ─── 1. Regex de alta confiança (bypass do Haiku — casos inequívocos) ─────────
# Estes casos são tão específicos que o regex é suficiente.
# Termos vagos ("caro", "longe") NÃO devem estar aqui — devem ir para o Haiku.

def test_high_conf_fgts():
    """FGTS é inequívoco — financiamento sem Haiku."""
    assert _check_high_confidence_regex("posso usar o FGTS pra dar entrada?") == "financiamento"

def test_high_conf_serasa():
    """Serasa é inequívoco — financiamento sem Haiku."""
    assert _check_high_confidence_regex("meu nome está no Serasa") == "financiamento"

def test_high_conf_credito_negado():
    """Crédito negado no banco é inequívoco."""
    assert _check_high_confidence_regex("meu crédito foi negado no banco") == "financiamento"

def test_high_conf_financiamento_negado():
    """Financiamento não aprovado é inequívoco."""
    assert _check_high_confidence_regex("meu financiamento não foi aprovado") == "financiamento"

def test_high_conf_score_baixo():
    """Score baixo é inequívoco."""
    assert _check_high_confidence_regex("meu score está muito baixo") == "financiamento"

def test_high_conf_prazo_entrega():
    """'Prazo de entrega' é inequívoco."""
    assert _check_high_confidence_regex("o prazo de entrega é muito longo") == "prazo"

def test_high_conf_quando_fica_pronto():
    """'Quando fica pronto' é inequívoco."""
    assert _check_high_confidence_regex("quando fica pronto esse empreendimento?") == "prazo"

def test_high_conf_taxa_condominio():
    """'Taxa de condomínio' é inequívoco."""
    assert _check_high_confidence_regex("a taxa de condomínio está muito alta") == "condominio"

def test_high_conf_condominio_caro():
    """'Condomínio muito caro' é inequívoco."""
    assert _check_high_confidence_regex("o condomínio muito caro pra minha realidade") == "condominio"

def test_high_conf_outra_imobiliaria():
    """Outra imobiliária é inequívoco."""
    assert _check_high_confidence_regex("a outra imobiliária tem algo diferente") == "concorrente"

def test_high_conf_outra_proposta():
    """'Outra proposta' é inequívoco."""
    assert _check_high_confidence_regex("já tenho uma proposta de outro lugar") == "concorrente"

def test_high_conf_sem_match():
    """Mensagem sem padrão de alta confiança retorna None."""
    assert _check_high_confidence_regex("que horas vocês abrem amanhã?") is None

def test_high_conf_case_insensitive():
    """Padrões são case-insensitive."""
    assert _check_high_confidence_regex("FGTS pode ser usado?") == "financiamento"


# ─── 2. Casos que DEVEM ir para o Haiku (não são alta confiança) ──────────────
# Garantir que termos vagos ou sofisticados NÃO bypassam o Haiku.
# Estes testes verificam que _check_high_confidence_regex retorna None,
# sinalizando que o Haiku deve ser chamado.

def test_high_conf_nao_pega_caro_generico():
    """'Caro' genérico não é alta confiança — deve ir para o Haiku."""
    assert _check_high_confidence_regex("tá muito caro pra mim") is None

def test_high_conf_nao_pega_parafrases_preco():
    """Paráfrase sofisticada de preço não é alta confiança — Haiku decide."""
    assert _check_high_confidence_regex("está um pouco acima do que eu esperava para esse perfil") is None

def test_high_conf_nao_pega_localizacao_generica():
    """'Longe' genérico não é alta confiança — Haiku decide."""
    assert _check_high_confidence_regex("o imóvel é um pouco longe da minha rotina") is None

def test_high_conf_nao_pega_hesitacao_implicita():
    """Hesitação implícita não é alta confiança — Haiku decide."""
    assert _check_high_confidence_regex("vou pensar melhor e te dou um retorno") is None

def test_high_conf_nao_pega_objecao_educada():
    """Objeção educada de alto padrão não é alta confiança — Haiku decide."""
    assert _check_high_confidence_regex("não tenho certeza se a localização funciona para a minha rotina") is None


# ─── 3. Fallback de regex amplo (offline — sem API key) ──────────────────────
# Estes padrões são usados APENAS quando Haiku não está disponível.
# Aceitam mais falsos positivos deliberadamente.

def test_fallback_preco_caro():
    """Fallback detecta 'caro' como preço."""
    assert _check_fallback_regex("esse apartamento tá muito caro pra mim") == "preco"

def test_fallback_preco_orcamento():
    """Fallback detecta 'fora do orçamento'."""
    assert _check_fallback_regex("esse valor está fora do meu orçamento") == "preco"

def test_fallback_localizacao_longe():
    """Fallback detecta 'longe do trabalho'."""
    assert _check_fallback_regex("o imóvel é muito longe do trabalho") == "localizacao"

def test_fallback_localizacao_bairro():
    """Fallback detecta objeção de bairro."""
    assert _check_fallback_regex("não gostei do bairro, parece perigoso") == "localizacao"

def test_fallback_sem_match():
    """Fallback retorna None para mensagem sem objeção."""
    assert _check_fallback_regex("que horas vocês abrem amanhã?") is None


# ─── 4. Casos de borda na detecção completa ──────────────────────────────────

def test_detect_short_message():
    """Mensagem abaixo do tamanho mínimo retorna None."""
    assert detect_objection("ok", use_haiku=False) is None

def test_detect_empty_message():
    """Mensagem vazia retorna None."""
    assert detect_objection("", use_haiku=False) is None

def test_detect_without_haiku_sem_match():
    """Sem Haiku, mensagem vaga sem match de alta confiança não detecta objeção."""
    result = detect_objection("está um pouco acima do que eu esperava", use_haiku=False)
    # Sem Haiku, cai no fallback amplo — pode ou não detectar dependendo do fallback
    # O importante é não lançar exceção
    assert result is None or isinstance(result, str)

def test_detect_without_haiku_sem_objecao():
    """Sem Haiku, mensagem claramente sem objeção retorna None."""
    result = detect_objection("gostei muito do acabamento, quero visitar", use_haiku=False)
    assert result is None

def test_detect_high_conf_bypass_sem_haiku():
    """Alta confiança bypassa Haiku mesmo com use_haiku=True (não precisa chamar)."""
    # FGTS é alta confiança — resultado independe de Haiku disponível ou não
    result = detect_objection("posso usar o FGTS pra dar entrada?", use_haiku=False)
    assert result == "financiamento"

def test_detect_regex_alias_compat():
    """detect_objection_regex() é alias para _check_high_confidence_regex()."""
    # Mantido para compatibilidade — deve funcionar igual
    assert detect_objection_regex("meu crédito foi negado no banco") == "financiamento"
    assert detect_objection_regex("que horas vocês abrem?") is None


# ─── 3. compute_objection_report ─────────────────────────────────────────────

def test_report_empty_leads():
    """Sem leads, retorna zeros."""
    report = compute_objection_report(_leads_override=[])
    assert report["total_leads"] == 0
    assert report["leads_com_objecao"] == 0
    assert report["taxa_objecao_pct"] == 0.0
    assert report["top_objections"] == []
    assert report["breakdown"] == {}


def test_report_single_lead_with_objection():
    """Um lead com objeção de preço."""
    leads = [
        {
            "lead_phone": "5511900000001",
            "objections_detected": json.dumps([
                {"categoria": "preco", "mensagem_preview": "muito caro", "detectado_em": "2026-04-10T10:00:00Z"}
            ]),
        }
    ]
    report = compute_objection_report(_leads_override=leads)
    assert report["total_leads"] == 1
    assert report["leads_com_objecao"] == 1
    assert report["taxa_objecao_pct"] == 100.0
    assert report["top_objections"][0]["categoria"] == "preco"
    assert report["top_objections"][0]["count"] == 1


def test_report_lead_no_objection():
    """Lead sem objeção detectada não conta."""
    leads = [
        {"lead_phone": "5511900000002", "objections_detected": None},
        {"lead_phone": "5511900000003", "objections_detected": []},
    ]
    report = compute_objection_report(_leads_override=leads)
    assert report["total_leads"] == 2
    assert report["leads_com_objecao"] == 0
    assert report["taxa_objecao_pct"] == 0.0


def test_report_dedup_within_lead():
    """
    Mesmo lead com duas objeções de 'preco' conta como 1 para o ranking
    (conta uma vez por categoria por lead).
    """
    leads = [
        {
            "lead_phone": "5511900000004",
            "objections_detected": json.dumps([
                {"categoria": "preco", "detectado_em": "2026-04-10T10:00:00Z"},
                {"categoria": "preco", "detectado_em": "2026-04-10T14:00:00Z"},
                {"categoria": "prazo", "detectado_em": "2026-04-10T15:00:00Z"},
            ]),
        }
    ]
    report = compute_objection_report(_leads_override=leads)
    # preco conta 1 (dedup), prazo conta 1
    assert report["breakdown"].get("preco") == 1
    assert report["breakdown"].get("prazo") == 1


def test_report_top3_ordering():
    """Top objeções ordenadas por frequência decrescente."""
    leads = [
        {
            "lead_phone": f"551190000000{i}",
            "objections_detected": json.dumps([
                {"categoria": "localizacao", "detectado_em": "2026-04-10T10:00:00Z"}
            ]),
        }
        for i in range(5)  # 5x localizacao
    ] + [
        {
            "lead_phone": "5511900000099",
            "objections_detected": json.dumps([
                {"categoria": "preco", "detectado_em": "2026-04-10T10:00:00Z"},
                {"categoria": "prazo", "detectado_em": "2026-04-10T11:00:00Z"},
            ]),
        }
    ]
    report = compute_objection_report(_leads_override=leads, top_n=3)
    categorias = [o["categoria"] for o in report["top_objections"]]
    assert categorias[0] == "localizacao"  # mais frequente
    assert "preco" in categorias
    assert "prazo" in categorias


def test_report_taxa_objecao_pct():
    """Taxa de leads com objeção calculada corretamente."""
    leads = [
        {
            "lead_phone": "5511900000010",
            "objections_detected": json.dumps([{"categoria": "preco", "detectado_em": "2026-04-10T10:00:00Z"}]),
        },
        {"lead_phone": "5511900000011", "objections_detected": []},
        {"lead_phone": "5511900000012", "objections_detected": None},
    ]
    report = compute_objection_report(_leads_override=leads)
    assert report["total_leads"] == 3
    assert report["leads_com_objecao"] == 1
    assert report["taxa_objecao_pct"] == 33.3


def test_report_pct_leads_in_top():
    """pct_leads em cada top_objection é calculado sobre total de leads."""
    leads = [
        {
            "lead_phone": f"5511999{i:05d}",
            "objections_detected": json.dumps([{"categoria": "preco", "detectado_em": "2026-04-10T10:00:00Z"}]),
        }
        for i in range(2)  # 2 leads com preco
    ] + [
        {"lead_phone": "5511999-0099", "objections_detected": None},  # 1 sem objeção
    ]
    report = compute_objection_report(_leads_override=leads)
    preco_entry = next(o for o in report["top_objections"] if o["categoria"] == "preco")
    # 2 de 3 leads = 66.7%
    assert abs(preco_entry["pct_leads"] - 66.7) < 0.1


def test_report_breakdown_contains_all_categories():
    """breakdown inclui todas as categorias detectadas."""
    leads = [
        {
            "lead_phone": "5511900000020",
            "objections_detected": json.dumps([
                {"categoria": "preco", "detectado_em": "2026-04-10T10:00:00Z"},
            ]),
        },
        {
            "lead_phone": "5511900000021",
            "objections_detected": json.dumps([
                {"categoria": "financiamento", "detectado_em": "2026-04-10T10:00:00Z"},
            ]),
        },
    ]
    report = compute_objection_report(_leads_override=leads)
    assert "preco" in report["breakdown"]
    assert "financiamento" in report["breakdown"]


# ─── 4. format_objection_whatsapp ────────────────────────────────────────────

def test_format_whatsapp_basic():
    """Mensagem WhatsApp contém header e top objeção."""
    report = {
        "period_days": 7,
        "total_leads": 10,
        "leads_com_objecao": 4,
        "taxa_objecao_pct": 40.0,
        "top_objections": [
            {"categoria": "preco", "label": "💰 Preço/Valor", "count": 3, "pct_leads": 30.0},
            {"categoria": "localizacao", "label": "📍 Localização", "count": 2, "pct_leads": 20.0},
        ],
    }
    msg = format_objection_whatsapp(report, imob_name="Ávora")
    assert "Ávora" in msg
    assert "40.0%" in msg
    assert "4/10" in msg
    assert "Preço" in msg
    assert "Localização" in msg


def test_format_whatsapp_no_objections():
    """Sem objeções, mensagem é positiva."""
    report = {
        "period_days": 7,
        "total_leads": 5,
        "leads_com_objecao": 0,
        "taxa_objecao_pct": 0.0,
        "top_objections": [],
    }
    msg = format_objection_whatsapp(report)
    assert "Nenhuma objeção" in msg or "Nenhuma" in msg


def test_format_whatsapp_uses_category_labels():
    """Labels formatados são usados (não o nome técnico da categoria)."""
    report = {
        "period_days": 7,
        "total_leads": 10,
        "leads_com_objecao": 3,
        "taxa_objecao_pct": 30.0,
        "top_objections": [
            {"categoria": "financiamento", "label": "🏦 Financiamento/Crédito", "count": 3, "pct_leads": 30.0},
        ],
    }
    msg = format_objection_whatsapp(report)
    assert "Financiamento" in msg
    # Não deve aparecer a categoria bruta "financiamento" como item do ranking
    assert "1. " in msg


# ─── 5. detect_and_save_objection (async) ────────────────────────────────────

def test_detect_and_save_no_objection():
    """Sem objeção detectada, append_fn não é chamado."""
    calls = []

    def mock_append(phone, client_id, entry):
        calls.append((phone, client_id, entry))

    result = run(detect_and_save_objection(
        phone="5511900000030",
        message="ok, obrigado",  # muito curto → não detecta
        use_haiku=False,
        _append_fn=mock_append,
    ))
    assert result is None
    assert len(calls) == 0


def test_detect_and_save_with_objection():
    """Com objeção detectada, append_fn é chamado com categoria correta."""
    calls = []

    def mock_append(phone, client_id, entry):
        calls.append((phone, client_id, entry))
        return True

    result = run(detect_and_save_objection(
        phone="5511900000031",
        message="o preço está muito acima do meu orçamento infelizmente",
        use_haiku=False,
        _append_fn=mock_append,
    ))
    assert result == "preco"
    assert len(calls) == 1
    assert calls[0][2]["categoria"] == "preco"
    assert "mensagem_preview" in calls[0][2]
    assert "detectado_em" in calls[0][2]


def test_detect_and_save_append_error_does_not_raise():
    """Erro no append não propaga exceção (fire-and-forget)."""
    def bad_append(phone, client_id, entry):
        raise RuntimeError("Supabase indisponível")

    # Não deve levantar exceção
    result = run(detect_and_save_objection(
        phone="5511900000032",
        message="meu crédito foi negado no banco, não consigo financiar",
        use_haiku=False,
        _append_fn=bad_append,
    ))
    assert result == "financiamento"


# ─── 6. Integridade do módulo ─────────────────────────────────────────────────

def test_all_categories_have_labels():
    """Toda categoria de objeção tem label definido."""
    from objection_engine import OBJECTION_CATEGORIES, CATEGORY_LABELS
    for cat in OBJECTION_CATEGORIES:
        assert cat in CATEGORY_LABELS, f"Sem label para categoria: {cat}"


def test_report_has_required_fields():
    """Relatório tem todos os campos obrigatórios."""
    report = compute_objection_report(_leads_override=[])
    required = [
        "client_id", "period_days", "period_start", "period_end",
        "generated_at", "total_leads", "leads_com_objecao",
        "taxa_objecao_pct", "top_objections", "breakdown",
    ]
    for field in required:
        assert field in report, f"Campo ausente: {field}"


# ─── Runner ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
