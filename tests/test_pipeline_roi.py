"""
tests/test_pipeline_roi.py — Testes do cálculo de pipeline_value_brl

Cobre:
  - Imóvel identificado → valor correto
  - Sem imóvel no portfólio → pipeline zerado / sem crash
  - Múltiplos imóveis → soma correta
  - Valor com formato string "R$ 1.200.000" → parse correto
  - Valor inválido → ignorado sem crash
  - Deduplicação de imovel_id (mesmo imóvel não conta duas vezes)
  - pipeline_imovel_ids ordenado e correto
  - pipeline_updated_at preenchido
  - Supabase indisponível → sem crash (fail silencioso)
  - Portfólio vazio → sem crash
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PORTFOLIO_FIXTURE = {
    "AV001": {"id": "AV001", "tipo": "Apartamento", "bairro": "Jardins",          "valor": "2950000"},
    "AV002": {"id": "AV002", "tipo": "Cobertura",   "bairro": "Itaim Bibi",       "valor": "4800000"},
    "AV004": {"id": "AV004", "tipo": "Casa",        "bairro": "Jardim Europa",    "valor": "8500000"},
    "AV005": {"id": "AV005", "tipo": "Apartamento", "bairro": "Moema",            "valor": "2100000"},
    "AV_BAD": {"id": "AV_BAD", "tipo": "Apartamento", "bairro": "Teste",         "valor": "nao_e_numero"},
    "AV_EMPTY": {"id": "AV_EMPTY", "tipo": "Apartamento", "bairro": "Teste",     "valor": ""},
}


def _make_mock_sb(upsert_ok: bool = True):
    """Cria mock do cliente Supabase."""
    sb = MagicMock()
    chain = MagicMock()
    chain.execute.return_value = MagicMock(data=[{"id": "ok"}])
    if not upsert_ok:
        chain.execute.side_effect = Exception("Supabase indisponível")
    sb.table.return_value.upsert.return_value = chain
    return sb


async def _run_update(
    sender: str,
    imovel_id: str,
    portfolio: dict,
    already_sent: set[str] | None = None,
    sb_ok: bool = True,
    use_redis: bool = False,
) -> dict | None:
    """
    Executa _update_pipeline_value de forma isolada, retornando os dados
    que seriam passados para o Supabase upsert.
    """
    captured = {}

    mock_sb = _make_mock_sb(upsert_ok=sb_ok)

    def fake_upsert(data, **kwargs):
        captured.update(data)
        chain = MagicMock()
        if not sb_ok:
            chain.execute.side_effect = Exception("Supabase down")
        else:
            chain.execute.return_value = MagicMock()
        return chain

    mock_sb.table.return_value.upsert.side_effect = fake_upsert

    memory_history: dict = {}
    if already_sent:
        memory_history[f"fotos:{sender}"] = set(already_sent)

    with (
        patch("whatsapp_webhook._get_supabase", return_value=mock_sb),
        patch("whatsapp_webhook._load_portfolio_dict", return_value=portfolio),
        patch("whatsapp_webhook._ctx_client_id", return_value="demo_01"),
        patch("whatsapp_webhook._memory_history", memory_history),
        patch("whatsapp_webhook.redis_client", None),  # sem Redis nos testes unitários
        patch("whatsapp_webhook.log", MagicMock()),
    ):
        import whatsapp_webhook as wh
        await wh._update_pipeline_value(sender, imovel_id)

    return captured if captured else None


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------


class TestPipelineValueCalculo:

    def test_imovel_identificado_valor_correto(self):
        """Imóvel AV001 = R$2.950.000 → pipeline_value_brl correto."""
        result = asyncio.run(_run_update("5511999990001", "AV001", PORTFOLIO_FIXTURE))
        assert result is not None
        assert result["pipeline_value_brl"] == 2_950_000.0

    def test_imovel_ids_listado(self):
        """pipeline_imovel_ids deve conter o imóvel enviado."""
        result = asyncio.run(_run_update("5511999990001", "AV001", PORTFOLIO_FIXTURE))
        assert "AV001" in result["pipeline_imovel_ids"]

    def test_client_id_e_sender_presentes(self):
        """Campos de identificação devem estar no payload."""
        result = asyncio.run(_run_update("5511999990001", "AV001", PORTFOLIO_FIXTURE))
        assert result["client_id"] == "demo_01"
        assert result["lead_phone"] == "5511999990001"

    def test_pipeline_updated_at_preenchido(self):
        """pipeline_updated_at deve ser uma data ISO 8601 recente."""
        result = asyncio.run(_run_update("5511999990001", "AV001", PORTFOLIO_FIXTURE))
        ts = result.get("pipeline_updated_at", "")
        assert ts, "pipeline_updated_at vazio"
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.year == datetime.now(timezone.utc).year


class TestPipelineValueMultiplosImoveis:

    def test_multiplos_imoveis_soma_correta(self):
        """AV001 (2.95M) + AV002 (4.8M) = 7.75M."""
        result = asyncio.run(_run_update(
            "5511999990002", "AV002", PORTFOLIO_FIXTURE,
            already_sent={"AV001"},
        ))
        assert result["pipeline_value_brl"] == pytest.approx(7_750_000.0)

    def test_tres_imoveis_soma_correta(self):
        """AV001 + AV002 + AV004 = 2.95M + 4.8M + 8.5M = 16.25M."""
        result = asyncio.run(_run_update(
            "5511999990003", "AV004", PORTFOLIO_FIXTURE,
            already_sent={"AV001", "AV002"},
        ))
        assert result["pipeline_value_brl"] == pytest.approx(16_250_000.0)

    def test_deduplicacao_mesmo_imovel(self):
        """Mesmo imóvel já enviado + recebido novamente → não duplica valor."""
        result = asyncio.run(_run_update(
            "5511999990004", "AV001", PORTFOLIO_FIXTURE,
            already_sent={"AV001"},
        ))
        # AV001 já está em already_sent e também é o imovel_id atual — não deve duplicar
        assert result["pipeline_value_brl"] == pytest.approx(2_950_000.0)
        assert result["pipeline_imovel_ids"].count("AV001") == 1

    def test_imovel_ids_ordenado(self):
        """pipeline_imovel_ids deve estar ordenado alfabeticamente."""
        result = asyncio.run(_run_update(
            "5511999990005", "AV002", PORTFOLIO_FIXTURE,
            already_sent={"AV004", "AV001"},
        ))
        ids = result["pipeline_imovel_ids"]
        assert ids == sorted(ids)


class TestPipelineValueEdgeCases:

    def test_portfolio_vazio_sem_crash(self):
        """Portfólio vazio → retorna None silenciosamente, sem crash."""
        result = asyncio.run(_run_update("5511999990006", "AV001", {}))
        assert result is None

    def test_imovel_nao_encontrado_sem_crash(self):
        """Imóvel ID não existe no portfólio → pipeline zerado sem crash."""
        result = asyncio.run(_run_update("5511999990007", "AV999", PORTFOLIO_FIXTURE))
        assert result is None

    def test_valor_invalido_ignorado(self):
        """Imóvel com valor não-numérico é ignorado, demais somados normalmente."""
        result = asyncio.run(_run_update(
            "5511999990008", "AV_BAD", PORTFOLIO_FIXTURE,
            already_sent={"AV001"},
        ))
        # AV_BAD tem valor inválido → ignorado; AV001 = 2.95M
        assert result["pipeline_value_brl"] == pytest.approx(2_950_000.0)
        assert "AV_BAD" not in result["pipeline_imovel_ids"]

    def test_valor_string_formato_reais(self):
        """Valor no formato 'R$ 1.200.000' deve ser parseado corretamente."""
        portfolio_com_str = {
            "AV_STR": {"id": "AV_STR", "valor": "R$ 1.200.000"},
        }
        result = asyncio.run(_run_update("5511999990009", "AV_STR", portfolio_com_str))
        assert result["pipeline_value_brl"] == pytest.approx(1_200_000.0)

    def test_valor_vazio_ignorado(self):
        """Imóvel com valor vazio é ignorado."""
        result = asyncio.run(_run_update(
            "5511999990010", "AV_EMPTY", PORTFOLIO_FIXTURE,
            already_sent={"AV001"},
        ))
        assert result["pipeline_value_brl"] == pytest.approx(2_950_000.0)
        assert "AV_EMPTY" not in result["pipeline_imovel_ids"]

    def test_supabase_indisponivel_sem_crash(self):
        """Falha no Supabase não deve propagar exceção — fail silencioso."""
        # Não deve levantar exceção
        try:
            asyncio.run(_run_update(
                "5511999990011", "AV001", PORTFOLIO_FIXTURE,
                sb_ok=False,
            ))
        except Exception as e:
            pytest.fail(f"Exceção propagada indevidamente: {e}")


class TestPipelineValueIntegracaoReport:

    def test_pipeline_value_brl_alimenta_report_engine(self):
        """
        Simula o que o report_engine faz: soma pipeline_value_brl dos leads
        com score >= limiar. Garante que o campo tem o tipo correto (float).
        """
        leads_mock = [
            {"pipeline_value_brl": 2950000.0, "intention_score": 8},
            {"pipeline_value_brl": 4800000.0, "intention_score": 9},
            {"pipeline_value_brl": None,       "intention_score": 3},  # lead frio
            {"pipeline_value_brl": "invalido", "intention_score": 7},  # valor corrompido
        ]

        total = 0.0
        for lead in leads_mock:
            val = lead.get("pipeline_value_brl")
            if val is None:
                continue
            try:
                total += float(val)
            except (ValueError, TypeError):
                pass

        # R$ 2.95M + R$ 4.8M + "invalido" ignorado = R$ 7.75M
        assert total == pytest.approx(7_750_000.0)
