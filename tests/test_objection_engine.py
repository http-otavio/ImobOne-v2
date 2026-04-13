"""
tests/test_objection_engine.py — Suite de testes para o objection_engine

Cobre:
    - Detecção por regex (preco, prazo, localizacao, financiamento, condominio, concorrente)
    - Casos de borda (mensagem curta, sem objeção)
    - compute_objection_report (métricas, dedup por lead, ordenação, taxa)
    - format_objection_whatsapp (formatação WhatsApp do dono)
    - detect_and_save_objection (async, com injection de mock para Supabase)

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



# ─── 1. Detecção por regex ────────────────────────────────────────────────────

def test_regex_preco_caro():
    """Detecta 'caro' como objeção de preço."""
    assert detect_objection_regex("esse apartamento tá muito caro pra mim") == "preco"


def test_regex_preco_orcamento():
    """Detecta 'fora do orçamento' como objeção de preço."""
    assert detect_objection_regex("esse valor está fora do meu orçamento") == "preco"


def test_regex_preco_desconto():
    """Detecta pedido de desconto como objeção de preço."""
    assert detect_objection_regex("vocês têm desconto pra pagamento à vista?") == "preco"


def test_regex_prazo_entrega():
    """Detecta 'prazo de entrega' como objeção."""
    assert detect_objection_regex("o prazo de entrega é muito longo") == "prazo"


def test_regex_prazo_quando_fica_pronto():
    """Detecta 'quando fica pronto' como objeção de prazo."""
    assert detect_objection_regex("quando fica pronto esse empreendimento?") == "prazo"


def test_regex_localizacao_longe():
    """Detecta 'longe do trabalho' como objeção de localização."""
    assert detect_objection_regex("o imóvel é muito longe do trabalho") == "localizacao"


def test_regex_localizacao_bairro():
    """Detecta objeção de bairro ruim."""
    assert detect_objection_regex("não gostei do bairro, parece perigoso") == "localizacao"


def test_regex_financiamento_credito():
    """Detecta financiamento negado no banco como objeção de financiamento."""
    assert detect_objection_regex("meu credito foi negado no banco") == "financiamento"


def test_regex_financiamento_fgts():
    """Detecta menção a FGTS como objeção de financiamento."""
    assert detect_objection_regex("posso usar o FGTS pra dar entrada?") == "financiamento"


def test_regex_condominio():
    """Detecta 'condomínio caro' como objeção."""
    assert detect_objection_regex("o condomínio é muito caro pra minha realidade") == "condominio"


def test_regex_concorrente():
    """Detecta comparação com outra imobiliária como objeção."""
    assert detect_objection_regex("a outra imobiliária tem algo mais barato") == "concorrente"


def test_regex_sem_objecao():
    """Mensagem sem objeção retorna None."""
    assert detect_objection_regex("que horas vocês abrem amanhã?") is None


def test_regex_case_insensitive():
    """Padrões são case-insensitive."""
    assert detect_objection_regex("MUITO CARO isso aí") == "preco"


# ─── 2. Casos de borda na detecção completa ───────────────────────────────────

def test_detect_short_message():
    """Mensagem abaixo do tamanho mínimo retorna None."""
    assert detect_objection("ok", use_haiku=False) is None


def test_detect_empty_message():
    """Mensagem vazia retorna None."""
    assert detect_objection("", use_haiku=False) is None


def test_detect_without_haiku_no_match():
    """Sem Haiku e sem match regex retorna None."""
    result = detect_objection("gostei bastante do imóvel, vou pensar", use_haiku=False)
    assert result is None


def test_detect_regex_hit_no_haiku_needed():
    """Com match regex, não aciona Haiku (usa 'caro' que bate diretamente)."""
    result = detect_objection("isso aqui ta muito caro pra mim não vou conseguir pagar", use_haiku=True)
    assert result == "preco"


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
