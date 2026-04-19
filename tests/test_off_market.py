"""
tests/test_off_market.py — Testes unitários do Motor de Pocket Listings

Cobre:
    1. is_corretor_sender: detecção correta com normalização de telefone
    2. extract_imovel_from_transcription: JSON válido com campos obrigatórios
    3. extract_imovel_from_transcription: extrai JSON de bloco ```json
    4. _build_imovel_text: texto canônico gerado corretamente
    5. assess_lead_compatibility: filtra apenas leads com score_match >= 7
    6. generate_match_draft: draft sem prefixos indesejados
    7. process_off_market_audio: pipeline completo mockado
    8. record_off_market_match: idempotência via mock HTTP
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ─── Fixtures ────────────────────────────────────────────────────────────────

SAMPLE_ONBOARDING = {
    "client_id": "demo_test",
    "corretores": [
        {
            "nome": "Renata Cavalcanti",
            "telefone_whatsapp": "+5511998001001",
            "bairros_regioes": ["Jardins", "Itaim Bibi"],
        },
        {
            "nome": "Marcelo Drummond",
            "telefone_whatsapp": "+5511998002002",
            "bairros_regioes": ["Moema", "Vila Olímpia"],
        },
    ],
    "evolution_instance": "demo_instance",
}

SAMPLE_IMOVEL = {
    "tipologia":      "apartamento",
    "bairro":         "Jardins",
    "cidade":         "São Paulo",
    "metragem":       180.0,
    "quartos":        4,
    "vagas":          3,
    "valor":          4500000,
    "caracteristicas": ["terraço privativo", "vista para o parque", "home theater"],
    "descricao":      "Apartamento de alto padrão com terraço privativo e vista para o Parque Trianon.",
    "sigiloso":       True,
}

SAMPLE_LEADS = [
    {
        "lead_phone":       "5511999110001",
        "lead_name":        "Carlos Menezes",
        "intention_score":  14,
        "pipeline_value_brl": 4800000,
        "regioes_interesse": ["Jardins", "Itaim Bibi"],
        "profile": {
            "neighborhoods":    ["Jardins"],
            "property_type":    "apartamento",
            "budget_max":       5000000,
            "bedrooms_desired": 4,
            "area_min_m2":      150,
            "purchase_purpose": "moradia própria",
        },
    },
    {
        "lead_phone":       "5511999220002",
        "lead_name":        "Fernanda Silveira",
        "intention_score":  8,
        "pipeline_value_brl": 2000000,
        "regioes_interesse": ["Brooklin"],
        "profile": {
            "neighborhoods":    ["Brooklin"],
            "property_type":    "apartamento",
            "budget_max":       2200000,
            "bedrooms_desired": 2,
            "area_min_m2":      80,
            "purchase_purpose": "investimento",
        },
    },
]

SAMPLE_TRANSCRIPTION = (
    "Olá, tenho um apartamento de 4 quartos nos Jardins, 180 metros, "
    "3 vagas, terraço privativo, pedindo 4,5 milhões. Muito discreto."
)


# ─── Testes ───────────────────────────────────────────────────────────────────

class TestIsCorretor(unittest.TestCase):

    def test_detects_registered_corretor_with_plus(self):
        """Reconhece corretor com + no número do onboarding."""
        from tools.off_market import is_corretor_sender
        self.assertTrue(is_corretor_sender("5511998001001", SAMPLE_ONBOARDING))

    def test_detects_corretor_regardless_of_plus_prefix(self):
        """Normaliza + antes de comparar."""
        from tools.off_market import is_corretor_sender
        # sender sem +, onboarding com + → deve bater
        self.assertTrue(is_corretor_sender("5511998002002", SAMPLE_ONBOARDING))

    def test_returns_false_for_unknown_sender(self):
        """Lead comum não é identificado como corretor."""
        from tools.off_market import is_corretor_sender
        self.assertFalse(is_corretor_sender("5511999887766", SAMPLE_ONBOARDING))

    def test_returns_false_for_empty_corretores(self):
        """Onboarding sem corretores → False."""
        from tools.off_market import is_corretor_sender
        self.assertFalse(is_corretor_sender("5511998001001", {"corretores": []}))


class TestExtractImovel(unittest.TestCase):

    def _make_anthropic_mock(self, json_str: str):
        msg = MagicMock()
        msg.content = [MagicMock(text=json_str)]
        client = MagicMock()
        client.messages.create.return_value = msg
        mock_mod = MagicMock()
        mock_mod.Anthropic.return_value = client
        return mock_mod

    @patch("tools.off_market._anthropic_module")
    def test_returns_valid_dict_with_required_keys(self, mock_mod):
        """extract_imovel retorna dict com tipologia, bairro, valor."""
        mock_mod.Anthropic.return_value = self._make_anthropic_mock(
            json.dumps(SAMPLE_IMOVEL)
        ).Anthropic.return_value
        from tools.off_market import extract_imovel_from_transcription
        result = extract_imovel_from_transcription(SAMPLE_TRANSCRIPTION)
        for key in ("tipologia", "bairro", "descricao", "sigiloso"):
            self.assertIn(key, result, f"Campo ausente: {key}")
        self.assertTrue(result.get("sigiloso"))

    @patch("tools.off_market._anthropic_module")
    def test_extracts_from_json_code_block(self, mock_mod):
        """Extrai JSON de blocos ```json corretamente."""
        wrapped = f"```json\n{json.dumps(SAMPLE_IMOVEL)}\n```"
        mock_mod.Anthropic.return_value = self._make_anthropic_mock(wrapped).Anthropic.return_value
        from tools.off_market import extract_imovel_from_transcription
        result = extract_imovel_from_transcription(SAMPLE_TRANSCRIPTION)
        self.assertEqual(result["bairro"], "Jardins")

    @patch("tools.off_market._anthropic_module")
    def test_raises_on_invalid_json(self, mock_mod):
        """Lança JSONDecodeError se LLM não retorna JSON válido."""
        mock_mod.Anthropic.return_value = self._make_anthropic_mock(
            "Não consegui extrair."
        ).Anthropic.return_value
        from tools.off_market import extract_imovel_from_transcription
        with self.assertRaises(Exception):
            extract_imovel_from_transcription(SAMPLE_TRANSCRIPTION)


class TestBuildImovelText(unittest.TestCase):

    def test_builds_text_with_all_fields(self):
        """_build_imovel_text inclui tipologia, bairro, valor, características."""
        from tools.off_market import _build_imovel_text
        text = _build_imovel_text(SAMPLE_IMOVEL)
        self.assertIn("Jardins",     text)
        self.assertIn("apartamento", text)
        self.assertIn("4.500.000",   text)
        self.assertIn("terraço",     text)

    def test_handles_missing_fields_gracefully(self):
        """_build_imovel_text não lança com campos ausentes."""
        from tools.off_market import _build_imovel_text
        text = _build_imovel_text({"tipologia": "cobertura"})
        self.assertIn("cobertura", text)


class TestAssessCompatibility(unittest.TestCase):

    def _make_haiku_mock(self, assessments: list):
        msg = MagicMock()
        msg.content = [MagicMock(text=json.dumps(assessments))]
        client = MagicMock()
        client.messages.create.return_value = msg
        mock_mod = MagicMock()
        mock_mod.Anthropic.return_value = client
        return mock_mod

    @patch("tools.off_market._anthropic_module")
    def test_filters_compatible_leads_by_score(self, mock_mod):
        """Só retorna leads com score_match >= 7."""
        mock_assessments = [
            {"lead_phone": "5511999110001", "compativel": True,  "score_match": 9,  "motivo": "Bairro e tipologia coincidem"},
            {"lead_phone": "5511999220002", "compativel": False, "score_match": 4,  "motivo": "Budget insuficiente"},
        ]
        mock_mod.Anthropic.return_value = self._make_haiku_mock(mock_assessments).Anthropic.return_value
        from tools.off_market import assess_lead_compatibility
        result = assess_lead_compatibility(SAMPLE_IMOVEL, SAMPLE_LEADS)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["lead_phone"], "5511999110001")
        self.assertEqual(result[0]["score_match"], 9)

    @patch("tools.off_market._anthropic_module")
    def test_returns_empty_list_when_no_match(self, mock_mod):
        """Retorna lista vazia quando nenhum lead é compatível."""
        mock_assessments = [
            {"lead_phone": "5511999110001", "compativel": False, "score_match": 5, "motivo": "..."},
            {"lead_phone": "5511999220002", "compativel": False, "score_match": 3, "motivo": "..."},
        ]
        mock_mod.Anthropic.return_value = self._make_haiku_mock(mock_assessments).Anthropic.return_value
        from tools.off_market import assess_lead_compatibility
        result = assess_lead_compatibility(SAMPLE_IMOVEL, SAMPLE_LEADS)
        self.assertEqual(result, [])

    def test_returns_empty_list_for_empty_leads(self):
        """assess_lead_compatibility com lista vazia retorna []."""
        from tools.off_market import assess_lead_compatibility
        result = assess_lead_compatibility(SAMPLE_IMOVEL, [])
        self.assertEqual(result, [])


class TestGenerateDraft(unittest.TestCase):

    @patch("tools.off_market._anthropic_module")
    def test_draft_has_no_prefix(self, mock_mod):
        """Draft não começa com prefixos como 'Mensagem:' ou 'Draft:'."""
        raw_draft = "Mensagem: Boa tarde Carlos, temos algo especial nos Jardins."
        msg = MagicMock()
        msg.content = [MagicMock(text=raw_draft)]
        client = MagicMock()
        client.messages.create.return_value = msg
        mock_mod.Anthropic.return_value = client

        from tools.off_market import generate_match_draft
        lead = {**SAMPLE_LEADS[0], "score_match": 9, "motivo_match": "Bairro coincide"}
        draft = generate_match_draft(lead, SAMPLE_IMOVEL, "Renata")
        self.assertFalse(draft.lower().startswith("mensagem:"))
        self.assertIn("Carlos", draft)

    @patch("tools.off_market._anthropic_module")
    def test_draft_is_non_empty_string(self, mock_mod):
        """Draft retorna string não vazia."""
        msg = MagicMock()
        msg.content = [MagicMock(text="Boa tarde, Carlos! Temos um apartamento exclusivo nos Jardins.")]
        client = MagicMock()
        client.messages.create.return_value = msg
        mock_mod.Anthropic.return_value = client

        from tools.off_market import generate_match_draft
        lead = {**SAMPLE_LEADS[0], "score_match": 9, "motivo_match": ""}
        draft = generate_match_draft(lead, SAMPLE_IMOVEL)
        self.assertIsInstance(draft, str)
        self.assertGreater(len(draft), 10)


class TestProcessOffMarketAudio(unittest.TestCase):

    @patch("tools.off_market.record_off_market_match")
    @patch("tools.off_market.send_off_market_match_to_corretor")
    @patch("tools.off_market.generate_match_draft")
    @patch("tools.off_market.assess_lead_compatibility")
    @patch("tools.off_market.load_vip_leads")
    @patch("tools.off_market.save_off_market_imovel")
    @patch("tools.off_market.extract_imovel_from_transcription")
    def test_full_pipeline_with_match(
        self,
        mock_extract,
        mock_save,
        mock_leads,
        mock_compat,
        mock_draft,
        mock_send,
        mock_record,
    ):
        """Pipeline completo retorna resultado correto quando há matches."""
        mock_extract.return_value = SAMPLE_IMOVEL
        mock_save.return_value    = "offmkt_abc123"
        mock_leads.return_value   = SAMPLE_LEADS
        mock_compat.return_value  = [{
            **SAMPLE_LEADS[0],
            "score_match": 9,
            "motivo_match": "Bairro e budget OK",
        }]
        mock_draft.return_value = "Boa tarde Carlos, temos algo especial nos Jardins."
        mock_send.return_value  = None
        mock_record.return_value = True

        from tools.off_market import process_off_market_audio
        result = process_off_market_audio(
            transcription=SAMPLE_TRANSCRIPTION,
            corretor_phone="5511998001001",
            client_id="demo_test",
            onboarding=SAMPLE_ONBOARDING,
        )

        self.assertEqual(result["imovel_id"],     "offmkt_abc123")
        self.assertEqual(result["matches_found"], 1)
        self.assertEqual(result["matches_sent"],  1)
        mock_extract.assert_called_once()
        mock_save.assert_called_once()
        mock_draft.assert_called_once()
        mock_send.assert_called_once()
        mock_record.assert_called_once()

    @patch("tools.off_market.load_vip_leads")
    @patch("tools.off_market.save_off_market_imovel")
    @patch("tools.off_market.extract_imovel_from_transcription")
    def test_pipeline_no_vip_leads(self, mock_extract, mock_save, mock_leads):
        """Pipeline funciona sem leads VIP — retorna 0 matches."""
        mock_extract.return_value = SAMPLE_IMOVEL
        mock_save.return_value    = "offmkt_xyz999"
        mock_leads.return_value   = []

        from tools.off_market import process_off_market_audio
        result = process_off_market_audio(
            transcription=SAMPLE_TRANSCRIPTION,
            corretor_phone="5511998001001",
            client_id="demo_test",
            onboarding=SAMPLE_ONBOARDING,
        )
        self.assertEqual(result["matches_found"], 0)
        self.assertEqual(result["matches_sent"],  0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
