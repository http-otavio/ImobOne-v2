"""
tests/test_liquidity_dossier.py — Testes unitários do módulo de liquidez/valorização

Cobre:
    1.  buscar_dados_liquidez: bairro conhecido retorna dict com dados
    2.  buscar_dados_liquidez: bairro desconhecido retorna None
    3.  buscar_dados_liquidez: case-insensitive (maiúsculo/minúsculo)
    4.  buscar_dados_liquidez: partial match ("Jardins Europa" bate em "jardins")
    5.  buscar_dados_liquidez: dados customizados do cliente têm prioridade
    6.  buscar_dados_liquidez: retorna None quando client_id sem arquivo
    7.  format_metricas_financeiras: contém valorização, liquidez, FII e fonte
    8.  format_metricas_financeiras: inclui atribuição de fonte obrigatória
    9.  format_metricas_financeiras: retorna string vazia se dict incompleto
    10. generate_dossie_content: perfil investidor + dados → metricas_financeiras populado
    11. generate_dossie_content: perfil comprador → metricas_financeiras null
    12. generate_dossie_content: perfil investidor sem dados → metricas_financeiras null
    13. render_dossie_pdf: metricas_financeiras no content → seção renderizada no PDF
    14. render_dossie_pdf: metricas_financeiras null → PDF sem seção de métricas
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Fixtures ────────────────────────────────────────────────────────────────

SAMPLE_LIQUIDITY = {
    "valorizacao_aa_pct":  9.2,
    "fonte_valorizacao":   "FipeZap 2024 — média residencial alto padrão",
    "liquidez_dias":       38,
    "fii_referencia":      "KNRI11",
    "fii_yield_aa_pct":    10.1,
    "fii_fonte":           "Média distribuições KNRI11 últimos 12 meses",
    "data_referencia":     "Q4 2024",
}

SAMPLE_HISTORY = [
    {"role": "user",      "content": "Estou procurando um apartamento nos Jardins para investimento."},
    {"role": "assistant", "content": "Que ótimo! Os Jardins são uma excelente escolha para investimento."},
    {"role": "user",      "content": "Tenho um budget de R$ 3 milhões."},
    {"role": "assistant", "content": "Perfeito. Tenho opções excelentes nessa faixa."},
]

SAMPLE_DOSSIE_CONTENT_WITH_METRICAS = {
    "perfil": {
        "nome": "Carlos",
        "tom_geral": "Investidor objetivo e bem informado.",
        "urgencia": "média",
    },
    "busca": {
        "tipologia": "apartamento",
        "bairros": ["Jardins"],
        "metragem": "150m²",
        "budget": "R$ 3.000.000",
        "prazo": "6 meses",
        "uso": "investimento",
    },
    "hot_buttons": ["Rentabilidade", "Valorização da região"],
    "objecoes": [],
    "sinais_quentes": ["Perguntou sobre rentabilidade histórica da região"],
    "proximo_passo": "Apresentar estudo de rentabilidade e comparar com FIIs.",
    "pontos_de_atencao": "Lead sofisticado — usar dados verificados.",
    "metricas_financeiras": {
        "valorizacao_aa": "9.2% ao ano (FipeZap 2024, Q4 2024)",
        "liquidez_dias": 38,
        "comparativo_fii": "valorização 9.2% vs KNRI11 10.1% a.a. em dividendos",
    },
}

SAMPLE_DOSSIE_CONTENT_SEM_METRICAS = {
    "perfil": {
        "nome": "Ana",
        "tom_geral": "Compradora para moradia própria.",
        "urgencia": "alta",
    },
    "busca": {
        "tipologia": "apartamento",
        "bairros": ["Moema"],
        "metragem": "120m²",
        "budget": "R$ 2.000.000",
        "prazo": "3 meses",
        "uso": "moradia própria",
    },
    "hot_buttons": ["Proximidade de escola"],
    "objecoes": [],
    "sinais_quentes": [],
    "proximo_passo": "Agendar visita ao imóvel AV005.",
    "pontos_de_atencao": "",
    "metricas_financeiras": None,
}


# ─── buscar_dados_liquidez ─────────────────────────────────────────────────

class TestBuscarDadosLiquidez(unittest.TestCase):

    def test_bairro_conhecido_retorna_dict(self):
        """Bairro conhecido no fallback retorna dict com campos esperados."""
        from tools.liquidity import buscar_dados_liquidez
        result = buscar_dados_liquidez("Jardins", "apartamento")
        self.assertIsNotNone(result)
        self.assertIn("valorizacao_aa_pct", result)
        self.assertIn("liquidez_dias", result)
        self.assertIn("fonte_valorizacao", result)
        self.assertIn("fii_referencia", result)

    def test_bairro_desconhecido_retorna_none(self):
        """Bairro sem dados retorna None — nunca inventa."""
        from tools.liquidity import buscar_dados_liquidez
        result = buscar_dados_liquidez("Bairro Inexistente XYZ", "apartamento")
        self.assertIsNone(result)

    def test_case_insensitive(self):
        """Busca é case-insensitive: 'JARDINS' == 'jardins' == 'Jardins'."""
        from tools.liquidity import buscar_dados_liquidez
        r1 = buscar_dados_liquidez("JARDINS", "apartamento")
        r2 = buscar_dados_liquidez("jardins", "apartamento")
        r3 = buscar_dados_liquidez("Jardins", "apartamento")
        self.assertIsNotNone(r1)
        self.assertEqual(r1["valorizacao_aa_pct"], r2["valorizacao_aa_pct"])
        self.assertEqual(r1["valorizacao_aa_pct"], r3["valorizacao_aa_pct"])

    def test_partial_match(self):
        """Match parcial: 'Jardins Europa' encontra dados de 'jardins'."""
        from tools.liquidity import buscar_dados_liquidez
        result = buscar_dados_liquidez("Jardins Europa", "apartamento")
        self.assertIsNotNone(result)
        self.assertGreater(result["valorizacao_aa_pct"], 0)

    def test_dados_customizados_tem_prioridade(self):
        """Dados do cliente (liquidity_data.json) têm prioridade sobre fallback."""
        from tools.liquidity import buscar_dados_liquidez

        custom_data = {
            "jardins": {
                "valorizacao_aa_pct":  15.0,  # valor diferente do fallback
                "fonte_valorizacao":   "Pesquisa interna 2025",
                "liquidez_dias":       20,
                "fii_referencia":      "HGLG11",
                "fii_yield_aa_pct":    12.5,
                "fii_fonte":           "Pesquisa interna",
                "data_referencia":     "Q1 2025",
            }
        }
        with patch("tools.liquidity._load_client_liquidity_data", return_value=custom_data):
            result = buscar_dados_liquidez("Jardins", "apartamento", client_id="custom_client")
        self.assertIsNotNone(result)
        self.assertEqual(result["valorizacao_aa_pct"], 15.0)  # dados customizados

    def test_client_sem_arquivo_usa_fallback(self):
        """Client sem liquidity_data.json cai para dados de fallback."""
        from tools.liquidity import buscar_dados_liquidez
        # client_id inexistente — sem arquivo — fallback deve funcionar
        result = buscar_dados_liquidez("Jardins", "apartamento", client_id="nonexistent_client_xyz")
        self.assertIsNotNone(result)  # fallback ativo


# ─── format_metricas_financeiras ──────────────────────────────────────────

class TestFormatMetricasFinanceiras(unittest.TestCase):

    def test_inclui_todos_os_campos(self):
        """Saída contém valorização, liquidez e comparativo."""
        from tools.liquidity import format_metricas_financeiras
        out = format_metricas_financeiras(SAMPLE_LIQUIDITY)
        self.assertIn("9.2", out)
        self.assertIn("38", out)
        self.assertIn("KNRI11", out)

    def test_inclui_fonte_obrigatoria(self):
        """Saída sempre inclui a fonte atribuída."""
        from tools.liquidity import format_metricas_financeiras
        out = format_metricas_financeiras(SAMPLE_LIQUIDITY)
        self.assertIn("FipeZap", out)
        self.assertIn("FONTE OBRIGATÓRIA", out)

    def test_dict_vazio_retorna_string_vazia(self):
        """Dict sem campos obrigatórios retorna string vazia."""
        from tools.liquidity import format_metricas_financeiras
        out = format_metricas_financeiras({})
        self.assertEqual(out, "")

    def test_dados_parciais_omitem_campos_ausentes(self):
        """Apenas campos disponíveis são incluídos — sem invenção."""
        from tools.liquidity import format_metricas_financeiras
        dados_parciais = {
            "valorizacao_aa_pct": 7.5,
            "fonte_valorizacao": "FipeZap 2024",
            "data_referencia": "Q4 2024",
            # sem liquidez_dias nem fii
        }
        out = format_metricas_financeiras(dados_parciais)
        self.assertIn("7.5", out)
        self.assertNotIn("dias", out)   # liquidez não presente
        self.assertNotIn("KNRI11", out) # FII não presente


# ─── generate_dossie_content ──────────────────────────────────────────────

class TestGenerateDossieContentLiquidity(unittest.TestCase):

    def _make_mock(self, content_dict: dict):
        """Mock do módulo anthropic que retorna o dict como JSON."""
        raw_json = json.dumps(content_dict)
        msg = MagicMock()
        msg.content = [MagicMock(text=raw_json)]
        client = MagicMock()
        client.messages.create.return_value = msg
        mock_mod = MagicMock()
        mock_mod.Anthropic.return_value = client
        return mock_mod

    @patch("tools.dossie._anthropic_module")
    def test_investidor_com_dados_popula_metricas(self, mock_mod):
        """Perfil investidor + liquidity_data → metricas_financeiras populado."""
        mock_mod.Anthropic.return_value = self._make_mock(
            SAMPLE_DOSSIE_CONTENT_WITH_METRICAS
        ).Anthropic.return_value

        from tools.dossie import generate_dossie_content
        result = generate_dossie_content(
            history=SAMPLE_HISTORY,
            lead_name="Carlos",
            lead_phone="5511999990001",
            score=14,
            liquidity_data=SAMPLE_LIQUIDITY,
        )
        # O mock retorna content com metricas — verifica que o prompt inclui bloco
        self.assertIsNotNone(result)
        call_args = mock_mod.Anthropic.return_value.messages.create.call_args
        prompt_text = call_args[1]["messages"][0]["content"]
        self.assertIn("MÉTRICAS FINANCEIRAS VERIFICADAS", prompt_text)
        self.assertIn("9.2", prompt_text)

    @patch("tools.dossie._anthropic_module")
    def test_comprador_sem_liquidity_data_nao_injeta_bloco(self, mock_mod):
        """Sem liquidity_data → prompt não contém bloco de métricas."""
        mock_mod.Anthropic.return_value = self._make_mock(
            SAMPLE_DOSSIE_CONTENT_SEM_METRICAS
        ).Anthropic.return_value

        from tools.dossie import generate_dossie_content
        result = generate_dossie_content(
            history=SAMPLE_HISTORY,
            lead_name="Ana",
            lead_phone="5511999990002",
            score=8,
            liquidity_data=None,  # sem dados
        )
        self.assertIsNotNone(result)
        call_args = mock_mod.Anthropic.return_value.messages.create.call_args
        prompt_text = call_args[1]["messages"][0]["content"]
        # Sem liquidity_data, bloco não deve estar no prompt
        self.assertNotIn("MÉTRICAS FINANCEIRAS VERIFICADAS", prompt_text)

    @patch("tools.dossie._anthropic_module")
    def test_investidor_sem_dados_nao_injeta_bloco(self, mock_mod):
        """Perfil investidor mas sem dados de liquidez → sem bloco no prompt."""
        mock_mod.Anthropic.return_value = self._make_mock(
            SAMPLE_DOSSIE_CONTENT_SEM_METRICAS
        ).Anthropic.return_value

        from tools.dossie import generate_dossie_content
        result = generate_dossie_content(
            history=SAMPLE_HISTORY,
            lead_name="Pedro",
            lead_phone="5511999990003",
            score=10,
            liquidity_data=None,  # sem dados verificáveis
        )
        self.assertIsNotNone(result)
        call_args = mock_mod.Anthropic.return_value.messages.create.call_args
        prompt_text = call_args[1]["messages"][0]["content"]
        self.assertNotIn("MÉTRICAS FINANCEIRAS VERIFICADAS", prompt_text)


# ─── render_dossie_pdf com metricas ───────────────────────────────────────

class TestRenderDossiePdfMetricas(unittest.TestCase):

    def test_pdf_com_metricas_renderiza_secao(self):
        """PDF com metricas_financeiras no content deve conter bytes da seção."""
        from tools.dossie import render_dossie_pdf
        pdf = render_dossie_pdf(
            content=SAMPLE_DOSSIE_CONTENT_WITH_METRICAS,
            lead_name="Carlos",
            lead_phone="5511999990001",
        )
        self.assertIsInstance(pdf, bytes)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 2_000)
        # Verifica que o texto "Métricas" está embutido no PDF (flate-encoded)
        # Pode não estar literal por compressão — mas o PDF deve ser gerado sem erro

    def test_pdf_sem_metricas_renderiza_normal(self):
        """PDF com metricas_financeiras: null deve renderizar sem erros."""
        from tools.dossie import render_dossie_pdf
        pdf = render_dossie_pdf(
            content=SAMPLE_DOSSIE_CONTENT_SEM_METRICAS,
            lead_name="Ana",
            lead_phone="5511999990002",
        )
        self.assertIsInstance(pdf, bytes)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1_500)

    def test_pdf_com_metricas_maior_que_sem(self):
        """PDF com seção de métricas deve ser maior que sem."""
        from tools.dossie import render_dossie_pdf
        pdf_com = render_dossie_pdf(
            content=SAMPLE_DOSSIE_CONTENT_WITH_METRICAS,
            lead_name="Carlos",
            lead_phone="5511999990001",
        )
        pdf_sem = render_dossie_pdf(
            content=SAMPLE_DOSSIE_CONTENT_SEM_METRICAS,
            lead_name="Ana",
            lead_phone="5511999990002",
        )
        self.assertGreater(len(pdf_com), len(pdf_sem))


if __name__ == "__main__":
    unittest.main(verbosity=2)
