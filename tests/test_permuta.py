"""
tests/test_permuta.py — Testes unitários do Motor de Permuta/Troca

Cobre:
    1. detect_permuta: 5+ variações linguísticas detectadas
    2. detect_permuta: mensagens sem permuta retornam False
    3. extract_permuta_data: JSON válido com campos esperados
    4. extract_permuta_data: extrai JSON de bloco ```json
    5. extract_permuta_data: lança exceção em JSON inválido
    6. calculate_permuta_score_bonus: +3 para ativo de alto padrão
    7. calculate_permuta_score_bonus: +1 para ativo sem dados suficientes
    8. calculate_permuta_score_bonus: bonus configurável via onboarding
    9. format_permuta_briefing_section: inclui todos os campos relevantes
    10. save_permuta_data: mock HTTP bem-sucedido
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ─── Fixtures ────────────────────────────────────────────────────────────────

SAMPLE_PERMUTA_DATA = {
    "tipo_ativo":      "apartamento",
    "bairro":          "Moema",
    "cidade":          "São Paulo",
    "metragem":        140.0,
    "valor_estimado":  2500000,
    "caracteristicas": ["reformado", "3 suítes", "2 vagas"],
    "descricao_lead":  "Tenho um apartamento em Moema de 140m², reformado, 3 suítes.",
}

SAMPLE_ONBOARDING = {
    "client_id": "demo_test",
    "permuta_score_bonus": 3,
}

SAMPLE_HISTORY = [
    {"role": "user",      "content": "Olá, tenho interesse num apartamento nos Jardins."},
    {"role": "assistant", "content": "Boa tarde! Posso te ajudar. O que você busca?"},
    {"role": "user",      "content": "Tenho um apartamento em Moema que quero usar como entrada."},
    {"role": "assistant", "content": "Interessante — me conte mais sobre o imóvel."},
    {"role": "user",      "content": "É 140m², reformado, valia uns 2,5 milhões."},
]


# ─── Detecção de permuta ─────────────────────────────────────────────────────

class TestDetectPermuta(unittest.TestCase):

    def _check(self, msg: str, expected: bool):
        from tools.permuta import detect_permuta
        result = detect_permuta(msg)
        self.assertEqual(result, expected, f"Falhou para: '{msg}'")

    def test_detects_permuta_direta(self):
        """Detecta a palavra 'permuta' diretamente."""
        self._check("Gostaria de fazer uma permuta pelo apartamento", True)

    def test_detects_troca(self):
        """Detecta 'troca' como sinônimo."""
        self._check("Posso fazer uma troca com meu imóvel atual?", True)

    def test_detects_tenho_apartamento(self):
        """Detecta 'tenho um apartamento' como sinal de permuta."""
        self._check("Tenho um apartamento em Moema, posso usá-lo como entrada?", True)

    def test_detects_dar_como_entrada(self):
        """Detecta 'dar como entrada' — linguagem implícita de alto padrão."""
        self._check("Posso dar meu imóvel como entrada no novo?", True)

    def test_detects_financiar_diferenca(self):
        """Detecta 'financiar a diferença' — implica ativo existente como parte."""
        self._check("Quero usar meu apart e financiar a diferença.", True)

    def test_detects_trocar_meu(self):
        """Detecta 'trocar meu' construção."""
        self._check("Pensei em trocar meu atual por um nos Jardins.", True)

    def test_detects_utilizar_meu_imovel(self):
        """Detecta 'utilizar meu imóvel' — linguagem formal."""
        self._check("Gostaria de utilizar meu imóvel atual como parte do pagamento.", True)

    def test_detects_possui_apartamento(self):
        """Detecta 'possuo um apartamento'."""
        self._check("Possuo um apartamento em Moema e quero um maior.", True)

    def test_no_false_positive_interest(self):
        """'Tenho interesse' não é permuta."""
        self._check("Tenho muito interesse nesse apartamento!", False)

    def test_no_false_positive_generic(self):
        """Mensagem genérica de qualificação não aciona detecção."""
        self._check("Qual o valor do imóvel nos Jardins?", False)

    def test_no_false_positive_investment(self):
        """Foco em investimento não é permuta."""
        self._check("Quero comprar para investimento, pensando em renda passiva.", False)

    def test_empty_string(self):
        """String vazia retorna False."""
        self._check("", False)


# ─── Extração de dados do ativo ──────────────────────────────────────────────

class TestExtractPermutaData(unittest.TestCase):

    def _make_mock(self, json_str: str):
        msg = MagicMock()
        msg.content = [MagicMock(text=json_str)]
        client = MagicMock()
        client.messages.create.return_value = msg
        mock_mod = MagicMock()
        mock_mod.Anthropic.return_value = client
        return mock_mod

    @patch("tools.permuta._anthropic_module")
    def test_returns_valid_dict(self, mock_mod):
        """extract_permuta_data retorna dict com campos esperados."""
        mock_mod.Anthropic.return_value = self._make_mock(
            json.dumps(SAMPLE_PERMUTA_DATA)
        ).Anthropic.return_value
        from tools.permuta import extract_permuta_data
        result = extract_permuta_data(SAMPLE_HISTORY)
        self.assertIn("tipo_ativo",      result)
        self.assertIn("valor_estimado",  result)
        self.assertIn("extracted_at",    result)
        self.assertEqual(result["bairro"], "Moema")

    @patch("tools.permuta._anthropic_module")
    def test_extracts_from_json_code_block(self, mock_mod):
        """Extrai JSON de blocos ```json."""
        wrapped = f"```json\n{json.dumps(SAMPLE_PERMUTA_DATA)}\n```"
        mock_mod.Anthropic.return_value = self._make_mock(wrapped).Anthropic.return_value
        from tools.permuta import extract_permuta_data
        result = extract_permuta_data(SAMPLE_HISTORY)
        self.assertEqual(result["tipo_ativo"], "apartamento")

    @patch("tools.permuta._anthropic_module")
    def test_raises_on_invalid_json(self, mock_mod):
        """Lança exceção se LLM não retorna JSON válido."""
        mock_mod.Anthropic.return_value = self._make_mock(
            "Não consegui extrair dados."
        ).Anthropic.return_value
        from tools.permuta import extract_permuta_data
        with self.assertRaises(Exception):
            extract_permuta_data(SAMPLE_HISTORY)


# ─── Cálculo de score bonus ──────────────────────────────────────────────────

class TestCalculatePermutaScore(unittest.TestCase):

    def test_high_value_asset_returns_full_bonus(self):
        """Ativo com valor >= 1M retorna bonus padrão (+3)."""
        from tools.permuta import calculate_permuta_score_bonus
        data = {**SAMPLE_PERMUTA_DATA, "valor_estimado": 2_500_000}
        bonus = calculate_permuta_score_bonus(data, SAMPLE_ONBOARDING)
        self.assertEqual(bonus, 3)

    def test_low_value_asset_returns_one(self):
        """Ativo sem valor informado e tipo não premium retorna +1."""
        from tools.permuta import calculate_permuta_score_bonus
        data = {"tipo_ativo": "terreno", "valor_estimado": None}
        bonus = calculate_permuta_score_bonus(data, {})
        self.assertEqual(bonus, 1)

    def test_apartment_medium_value_counts_as_premium(self):
        """Apartamento com valor >= 500k já é considerado alto padrão."""
        from tools.permuta import calculate_permuta_score_bonus
        data = {"tipo_ativo": "apartamento", "valor_estimado": 750_000}
        bonus = calculate_permuta_score_bonus(data, SAMPLE_ONBOARDING)
        self.assertEqual(bonus, 3)

    def test_custom_bonus_from_onboarding(self):
        """Bonus configurável via onboarding.json."""
        from tools.permuta import calculate_permuta_score_bonus
        data = {**SAMPLE_PERMUTA_DATA, "valor_estimado": 3_000_000}
        onboarding_custom = {"permuta_score_bonus": 5}
        bonus = calculate_permuta_score_bonus(data, onboarding_custom)
        self.assertEqual(bonus, 5)

    def test_no_value_no_premium_type(self):
        """Ativo genérico sem valor não recebe bonus alto."""
        from tools.permuta import calculate_permuta_score_bonus
        data = {"tipo_ativo": "outro", "valor_estimado": 200_000}
        bonus = calculate_permuta_score_bonus(data, {})
        self.assertEqual(bonus, 1)


# ─── Formatação do briefing ──────────────────────────────────────────────────

class TestFormatPermutaBriefing(unittest.TestCase):

    def test_includes_all_fields(self):
        """Briefing inclui tipo, localização, valor e características."""
        from tools.permuta import format_permuta_briefing_section
        section = format_permuta_briefing_section(SAMPLE_PERMUTA_DATA)
        self.assertIn("Análise de Permuta", section)
        self.assertIn("Moema",              section)
        self.assertIn("2.500.000",          section)
        self.assertIn("reformado",          section)
        self.assertIn("Apartamento",        section)

    def test_handles_missing_fields_gracefully(self):
        """Briefing não lança com campos ausentes."""
        from tools.permuta import format_permuta_briefing_section
        section = format_permuta_briefing_section({"tipo_ativo": "cobertura"})
        self.assertIn("Cobertura", section)
        self.assertIn("Análise de Permuta", section)

    def test_includes_action_note(self):
        """Briefing inclui instrução para o corretor verificar."""
        from tools.permuta import format_permuta_briefing_section
        section = format_permuta_briefing_section(SAMPLE_PERMUTA_DATA)
        self.assertIn("Verificar", section)


# ─── Persistência ────────────────────────────────────────────────────────────

class TestSavePermutaData(unittest.TestCase):

    @patch("tools.permuta._supabase_patch")
    def test_save_calls_supabase_patch(self, mock_patch):
        """save_permuta_data chama _supabase_patch com dados corretos."""
        mock_patch.return_value = True
        from tools.permuta import save_permuta_data
        result = save_permuta_data(
            lead_phone="5511999000001",
            client_id="demo_test",
            permuta_data=SAMPLE_PERMUTA_DATA,
            score_bonus=3,
        )
        self.assertTrue(result)
        mock_patch.assert_called_once()
        call_args = mock_patch.call_args
        path    = call_args[0][0]
        payload = call_args[0][1]
        self.assertIn("5511999000001", path)
        self.assertTrue(payload["permuta_detectada"])
        self.assertIsNotNone(payload["permuta_dados"])

    @patch("tools.permuta._supabase_patch")
    def test_save_returns_false_on_failure(self, mock_patch):
        """save_permuta_data retorna False quando Supabase falha."""
        mock_patch.return_value = False
        from tools.permuta import save_permuta_data
        result = save_permuta_data("55119", "demo_test", {}, 1)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
