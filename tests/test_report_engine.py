"""
tests/test_report_engine.py — Testes do engine de relatórios executivos

Cobertura:
  - compute_weekly_metrics: métricas básicas, pipeline_estimado_brl, objeções,
    leads por origem, taxa de conversão, sem leads, edge cases
  - format_whatsapp_message: campos obrigatórios, pipeline formatado, objeção
  - export_csv: cabeçalhos, valores, objeções, origens
  - save_report_json / load_reports_history: persistência e histórico

Todos os testes usam _leads_override (injeção de dados mock) — sem chamadas reais
ao Supabase ou WhatsApp.
"""

import csv
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─── Configura path para importar do root do projeto ─────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import report_engine as re_mod


# ─── Fixtures ─────────────────────────────────────────────────────────────────
def make_lead(
    phone="5511999990001",
    name="Carlos Silva",
    score=8,
    visita=False,
    descartado=False,
    pipeline_value=2_500_000.0,
    objections=None,
    source="whatsapp_organico",
):
    return {
        "lead_phone": phone,
        "lead_name": name,
        "intention_score": score,
        "score_breakdown": {"pergunta_especifica": 3, "horario_visita": 4, "foto_solicitada": 1},
        "pipeline_value_brl": pipeline_value,
        "visita_agendada": visita,
        "descartado": descartado,
        "source": source,
        "objections_detected": objections or [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── compute_weekly_metrics ───────────────────────────────────────────────────

class TestComputeWeeklyMetrics:
    def test_basic_counts(self):
        leads = [
            make_lead(phone="001", score=8, visita=True),
            make_lead(phone="002", score=3, visita=False),
            make_lead(phone="003", score=9, visita=True, descartado=False),
        ]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["total_leads"] == 3
        assert m["visitas_confirmadas"] == 2
        assert m["leads_quentes"] == 2  # score >= 6

    def test_no_leads_returns_zeros(self):
        m = re_mod.compute_weekly_metrics(_leads_override=[])
        assert m["total_leads"] == 0
        assert m["pipeline_estimado_brl"] == 0.0
        assert m["taxa_conversao_pct"] == 0.0
        assert m["top_objecao"] is None

    def test_pipeline_estimado_brl_somado(self):
        """Apenas leads com score >= threshold entram no pipeline."""
        leads = [
            make_lead(phone="001", score=8, pipeline_value=2_000_000.0),   # entra
            make_lead(phone="002", score=9, pipeline_value=3_500_000.0),   # entra
            make_lead(phone="003", score=3, pipeline_value=1_000_000.0),   # NÃO entra (score < 6)
        ]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["pipeline_estimado_brl"] == pytest.approx(5_500_000.0)

    def test_pipeline_ignora_leads_frios(self):
        leads = [make_lead(score=2, pipeline_value=5_000_000.0)]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["pipeline_estimado_brl"] == 0.0

    def test_pipeline_sem_pipeline_value(self):
        """Leads sem pipeline_value_brl não somam ao pipeline."""
        leads = [make_lead(score=9, pipeline_value=None)]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["pipeline_estimado_brl"] == 0.0

    def test_taxa_conversao(self):
        leads = [
            make_lead(phone="001", visita=True),
            make_lead(phone="002", visita=True),
            make_lead(phone="003", visita=False),
            make_lead(phone="004", visita=False),
        ]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["taxa_conversao_pct"] == pytest.approx(50.0)

    def test_taxa_conversao_zero_denominador(self):
        m = re_mod.compute_weekly_metrics(_leads_override=[])
        assert m["taxa_conversao_pct"] == 0.0

    def test_top_objecao_mais_frequente(self):
        leads = [
            make_lead(phone="001", objections=[{"categoria": "preco"}, {"categoria": "prazo"}]),
            make_lead(phone="002", objections=[{"categoria": "preco"}]),
            make_lead(phone="003", objections=[{"categoria": "localizacao"}]),
        ]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["top_objecao"] == "preco"
        assert m["top_objecao_count"] == 2

    def test_top_objecao_sem_objecoes(self):
        leads = [make_lead(objections=[])]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["top_objecao"] is None
        assert m["top_objecao_count"] == 0

    def test_objecao_string_json(self):
        """objections_detected pode vir como string JSON do Supabase."""
        lead = make_lead()
        lead["objections_detected"] = json.dumps([{"categoria": "financiamento"}])
        m = re_mod.compute_weekly_metrics(_leads_override=[lead])
        assert m["top_objecao"] == "financiamento"

    def test_leads_por_origem_breakdown(self):
        leads = [
            make_lead(phone="001", source="portal_zap"),
            make_lead(phone="002", source="portal_zap"),
            make_lead(phone="003", source="portal_vivareal"),
            make_lead(phone="004", source="whatsapp_organico"),
        ]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["leads_por_origem"]["portal_zap"] == 2
        assert m["leads_por_origem"]["portal_vivareal"] == 1
        assert m["leads_por_origem"]["whatsapp_organico"] == 1

    def test_score_medio(self):
        leads = [
            make_lead(phone="001", score=10),
            make_lead(phone="002", score=4),
            make_lead(phone="003", score=7),
        ]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["score_medio"] == pytest.approx(7.0)

    def test_leads_descartados_contados(self):
        leads = [
            make_lead(phone="001", descartado=True),
            make_lead(phone="002", descartado=True),
            make_lead(phone="003", descartado=False),
        ]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["leads_descartados"] == 2

    def test_metadados_do_periodo(self):
        m = re_mod.compute_weekly_metrics(_leads_override=[], days=14)
        assert m["period_days"] == 14
        assert "period_start" in m
        assert "period_end" in m
        assert "generated_at" in m
        assert "client_id" in m


# ─── format_whatsapp_message ─────────────────────────────────────────────────

class TestFormatWhatsappMessage:
    def _base_metrics(self, **overrides) -> dict:
        base = re_mod._empty_metrics("demo", 7, datetime.now(timezone.utc), datetime.now(timezone.utc))
        base.update({
            "total_leads": 10,
            "leads_quentes": 4,
            "visitas_confirmadas": 2,
            "taxa_conversao_pct": 20.0,
            "pipeline_estimado_brl": 7_500_000.0,
            "top_objecao": "prazo",
            "top_objecao_count": 3,
            "leads_por_origem": {"portal_zap": 6, "whatsapp_organico": 4},
        })
        base.update(overrides)
        return base

    def test_contem_campos_obrigatorios(self):
        msg = re_mod.format_whatsapp_message(self._base_metrics())
        assert "Relatório Semanal" in msg
        assert "Leads atendidos" in msg
        assert "Leads quentes" in msg
        assert "Visitas confirmadas" in msg
        assert "Pipeline ativo estimado" in msg

    def test_pipeline_formatado_em_reais(self):
        msg = re_mod.format_whatsapp_message(self._base_metrics(pipeline_estimado_brl=7_500_000.0))
        assert "7.500.000" in msg or "7500000" in msg  # aceita ambos os formatos

    def test_objecao_incluida_quando_presente(self):
        msg = re_mod.format_whatsapp_message(self._base_metrics(top_objecao="localizacao", top_objecao_count=5))
        assert "localizacao" in msg
        assert "5x" in msg

    def test_sem_objecao_nao_exibe_linha(self):
        msg = re_mod.format_whatsapp_message(self._base_metrics(top_objecao=None))
        assert "Principal objeção" not in msg

    def test_taxa_conversao_incluida(self):
        msg = re_mod.format_whatsapp_message(self._base_metrics(taxa_conversao_pct=25.0))
        assert "25.0%" in msg

    def test_nome_imobiliaria_no_cabecalho(self):
        msg = re_mod.format_whatsapp_message(self._base_metrics(), imob_name="Premier Imóveis")
        assert "Premier Imóveis" in msg

    def test_origem_dos_leads_incluida(self):
        msg = re_mod.format_whatsapp_message(self._base_metrics())
        assert "portal_zap" in msg


# ─── export_csv ──────────────────────────────────────────────────────────────

class TestExportCsv:
    def _metrics(self) -> dict:
        m = re_mod._empty_metrics("demo", 7, datetime.now(timezone.utc), datetime.now(timezone.utc))
        m.update({
            "total_leads": 15,
            "visitas_confirmadas": 3,
            "leads_quentes": 6,
            "pipeline_estimado_brl": 12_000_000.0,
            "taxa_conversao_pct": 20.0,
            "score_medio": 6.5,
            "top_objecao": "preco",
            "top_objecao_count": 4,
            "objection_breakdown": {"preco": 4, "prazo": 2},
            "leads_por_origem": {"portal_zap": 10, "whatsapp_organico": 5},
        })
        return m

    def test_csv_parseable(self):
        csv_content = re_mod.export_csv(self._metrics())
        reader = csv.reader(csv_content.splitlines())
        rows = list(reader)
        assert len(rows) > 0  # tem linhas

    def test_csv_contem_metricas_basicas(self):
        csv_content = re_mod.export_csv(self._metrics())
        assert "total_leads" in csv_content
        assert "15" in csv_content
        assert "pipeline_estimado_brl" in csv_content
        assert "12000000" in csv_content

    def test_csv_contem_objecoes(self):
        csv_content = re_mod.export_csv(self._metrics())
        assert "preco" in csv_content
        assert "prazo" in csv_content

    def test_csv_contem_origens(self):
        csv_content = re_mod.export_csv(self._metrics())
        assert "portal_zap" in csv_content
        assert "10" in csv_content

    def test_csv_vazio_nao_quebra(self):
        m = re_mod._empty_metrics("demo", 7, datetime.now(timezone.utc), datetime.now(timezone.utc))
        csv_content = re_mod.export_csv(m)
        assert "total_leads" in csv_content


# ─── Persistência em disco ────────────────────────────────────────────────────

class TestReportPersistence:
    def test_save_and_load_report(self, tmp_path):
        # Redireciona REPORTS_DIR para tmp
        original_dir = re_mod.REPORTS_DIR
        re_mod.REPORTS_DIR = tmp_path / "reports"

        try:
            metrics = re_mod._empty_metrics("test_client", 7, datetime.now(timezone.utc), datetime.now(timezone.utc))
            metrics["total_leads"] = 42

            path = re_mod.save_report_json(metrics)
            assert path.exists()

            history = re_mod.load_reports_history(client_id="test_client")
            assert len(history) == 1
            assert history[0]["total_leads"] == 42
        finally:
            re_mod.REPORTS_DIR = original_dir

    def test_load_history_limit(self, tmp_path):
        original_dir = re_mod.REPORTS_DIR
        re_mod.REPORTS_DIR = tmp_path / "reports"
        re_mod.REPORTS_DIR.mkdir(parents=True)

        try:
            # Cria 5 arquivos diretamente com nomes únicos para evitar colisão de timestamp
            for i in range(5):
                metrics = re_mod._empty_metrics("cli", 7, datetime.now(timezone.utc), datetime.now(timezone.utc))
                metrics["total_leads"] = i
                path = re_mod.REPORTS_DIR / f"weekly_cli_2026040{i}_120000.json"
                path.write_text(json.dumps(metrics, ensure_ascii=False))

            history = re_mod.load_reports_history(client_id="cli", limit=3)
            assert len(history) == 3
        finally:
            re_mod.REPORTS_DIR = original_dir

    def test_load_history_sem_arquivos(self, tmp_path):
        original_dir = re_mod.REPORTS_DIR
        re_mod.REPORTS_DIR = tmp_path / "empty"

        try:
            history = re_mod.load_reports_history(client_id="inexistente")
            assert history == []
        finally:
            re_mod.REPORTS_DIR = original_dir


# ─── run_weekly_report dry-run ────────────────────────────────────────────────

class TestRunWeeklyReportDryRun:
    def test_dry_run_nao_envia_whatsapp(self, tmp_path):
        """Dry-run calcula métricas e loga, sem enviar WhatsApp."""
        original_dir = re_mod.REPORTS_DIR
        re_mod.REPORTS_DIR = tmp_path / "reports"

        leads = [
            make_lead(phone="001", score=8, visita=True, pipeline_value=3_000_000.0),
            make_lead(phone="002", score=4),
        ]

        with patch.object(re_mod, "_send_whatsapp") as mock_send:
            with patch.object(re_mod, "compute_weekly_metrics", return_value=re_mod.compute_weekly_metrics(_leads_override=leads)):
                result = re_mod.run_weekly_report(dry_run=True)

            mock_send.assert_not_called()

        re_mod.REPORTS_DIR = original_dir

        assert result["total_leads"] >= 0  # retornou dict válido

    def test_retorna_metricas_validas(self, tmp_path):
        original_dir = re_mod.REPORTS_DIR
        re_mod.REPORTS_DIR = tmp_path / "reports"

        leads = [make_lead(score=8, visita=True, pipeline_value=5_000_000.0)]

        with patch.object(re_mod, "_send_whatsapp", return_value=True):
            with patch.object(re_mod, "_sb_get", return_value=leads):
                result = re_mod.run_weekly_report(dry_run=False)

        re_mod.REPORTS_DIR = original_dir

        assert "total_leads" in result
        assert "pipeline_estimado_brl" in result
        assert "generated_at" in result


# ─── Edge cases / robustez ────────────────────────────────────────────────────

class TestEdgeCases:
    def test_pipeline_value_brl_string_invalida_ignorada(self):
        lead = make_lead(score=9)
        lead["pipeline_value_brl"] = "nao_eh_numero"
        m = re_mod.compute_weekly_metrics(_leads_override=[lead])
        assert m["pipeline_estimado_brl"] == 0.0

    def test_objections_como_lista_de_strings(self):
        """Aceita lista de strings simples além de dicts."""
        lead = make_lead(objections=["prazo", "preco", "prazo"])
        m = re_mod.compute_weekly_metrics(_leads_override=[lead])
        assert m["top_objecao"] == "prazo"
        assert m["top_objecao_count"] == 2

    def test_lead_sem_source_usa_fallback(self):
        lead = make_lead()
        lead["source"] = None
        m = re_mod.compute_weekly_metrics(_leads_override=[lead])
        assert "whatsapp_organico" in m["leads_por_origem"]

    def test_unico_lead_sem_visita(self):
        m = re_mod.compute_weekly_metrics(_leads_override=[make_lead(visita=False)])
        assert m["taxa_conversao_pct"] == 0.0
        assert m["visitas_confirmadas"] == 0

    def test_todos_leads_com_visita(self):
        leads = [make_lead(phone=str(i), visita=True) for i in range(5)]
        m = re_mod.compute_weekly_metrics(_leads_override=leads)
        assert m["taxa_conversao_pct"] == 100.0
        assert m["visitas_confirmadas"] == 5
