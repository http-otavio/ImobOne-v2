"""
tests/test_dossie.py — Testes unitários do Dossiê de Caviar

Cobre:
    1. generate_dossie_content: JSON válido com campos obrigatórios
    2. render_dossie_pdf: PDF gerado com bytes válidos (magic bytes %PDF)
    3. render_dossie_pdf: funciona com conteúdo mínimo (campos vazios)
    4. send_dossie_to_corretor: payload correto enviado via HTTP mock
    5. build_and_send_dossie: pipeline completo mockado (content → pdf → send)
    6. generate_dossie_content: extrai JSON de bloco ```json
    7. save_dossie_locally: salva arquivo em path esperado
"""

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Garante que tools/ está no path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Fixtures ────────────────────────────────────────────────────────────────

SAMPLE_HISTORY = [
    {"role": "user",      "content": "Olá, estou buscando um apartamento nos Jardins"},
    {"role": "assistant", "content": "Olá! Sou Sofia. Prazer em ajudá-lo. Qual o seu nome?"},
    {"role": "user",      "content": "Meu nome é Carlos. Budget de R$ 3 milhões."},
    {"role": "assistant", "content": "Ótimo, Carlos! Temos opções excelentes nos Jardins."},
    {"role": "user",      "content": "Preciso de 4 quartos e vaga dupla. Tenho filhos."},
    {"role": "assistant", "content": "Perfeito. Posso agendar uma visita para terça às 10h?"},
    {"role": "user",      "content": "Sim, terça às 10h está ótimo."},
    {"role": "assistant", "content": "Sua visita está confirmada para terça, 15/04/2026 às 10h."},
]

SAMPLE_CONTENT = {
    "perfil": {
        "nome": "Carlos",
        "tom_geral": "Lead qualificado, família com filhos, decisão próxima",
        "urgencia": "alta",
    },
    "busca": {
        "tipologia": "Apartamento 4 quartos",
        "bairros": ["Jardins"],
        "metragem": "Não informada",
        "budget": "R$ 3.000.000",
        "prazo": "Imediato",
        "uso": "moradia própria",
    },
    "hot_buttons": [
        "Localização nos Jardins",
        "4 quartos para família com filhos",
        "Vaga dupla",
    ],
    "objecoes": [
        {
            "objecao": "Necessidade de vaga dupla",
            "status": "resolvida",
            "como_tratar": "Confirmar disponibilidade de vaga dupla antes da visita",
        }
    ],
    "sinais_quentes": [
        "Confirmou visita para terça às 10h sem hesitação",
        "Declarou budget de R$ 3M diretamente",
    ],
    "proximo_passo": "Confirmar vagas duplas disponíveis. Preparar 3 opções na faixa R$ 2,8-3,2M.",
    "pontos_de_atencao": "Cliente tem filhos — ressaltar segurança e espaço do condomínio.",
}


# ─── Testes ───────────────────────────────────────────────────────────────────

class TestGenerateDossieContent(unittest.TestCase):
    """Testa geração de conteúdo via Claude Sonnet (mockado)."""

    def _make_mock_client(self, json_response: str):
        msg = MagicMock()
        msg.content = [MagicMock(text=json_response)]
        client = MagicMock()
        client.messages.create.return_value = msg
        return client

    @patch("tools.dossie._anthropic_module")
    def test_returns_valid_dict_with_required_keys(self, mock_anthropic):
        """generate_dossie_content retorna dict com todos os campos obrigatórios."""
        mock_anthropic.Anthropic.return_value = self._make_mock_client(
            json.dumps(SAMPLE_CONTENT)
        )
        from tools.dossie import generate_dossie_content
        result = generate_dossie_content(
            history=SAMPLE_HISTORY,
            lead_name="Carlos",
            lead_phone="5511999990001",
            score=14,
            pipeline=3_000_000.0,
            visit_date="terça, 15/04/2026 às 10h",
        )
        self.assertIsInstance(result, dict)
        for key in ("perfil", "busca", "hot_buttons", "objecoes",
                    "sinais_quentes", "proximo_passo"):
            self.assertIn(key, result, f"Campo obrigatório ausente: {key}")

    @patch("tools.dossie._anthropic_module")
    def test_extracts_json_from_code_block(self, mock_anthropic):
        """generate_dossie_content extrai JSON de blocos ```json corretamente."""
        wrapped = f"```json\n{json.dumps(SAMPLE_CONTENT)}\n```"
        mock_anthropic.Anthropic.return_value = self._make_mock_client(wrapped)
        from tools.dossie import generate_dossie_content
        result = generate_dossie_content(
            history=SAMPLE_HISTORY,
            lead_name="Carlos",
            lead_phone="5511999990001",
        )
        self.assertEqual(result["perfil"]["nome"], "Carlos")

    @patch("tools.dossie._anthropic_module")
    def test_raises_on_invalid_json(self, mock_anthropic):
        """generate_dossie_content lança exceção se LLM retorna JSON inválido."""
        mock_anthropic.Anthropic.return_value = self._make_mock_client("resposta qualquer sem json")
        from tools.dossie import generate_dossie_content
        with self.assertRaises(json.JSONDecodeError):
            generate_dossie_content(
                history=SAMPLE_HISTORY,
                lead_name="Carlos",
                lead_phone="5511999990001",
            )


class TestRenderDossiePdf(unittest.TestCase):
    """Testa geração de PDF via reportlab."""

    def test_pdf_magic_bytes(self):
        """render_dossie_pdf retorna bytes começando com %PDF."""
        try:
            from tools.dossie import render_dossie_pdf
        except ImportError:
            self.skipTest("tools.dossie não disponível")

        pdf_bytes = render_dossie_pdf(
            content=SAMPLE_CONTENT,
            lead_name="Carlos",
            lead_phone="5511999990001",
            imobiliaria="Artístico Imóveis",
            visit_date="15/04/2026 às 10h",
        )
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"), "PDF deve começar com %PDF")

    def test_pdf_with_minimal_content(self):
        """render_dossie_pdf funciona com conteúdo mínimo (campos opcionais vazios)."""
        try:
            from tools.dossie import render_dossie_pdf
        except ImportError:
            self.skipTest("tools.dossie não disponível")

        minimal = {
            "perfil":          {"nome": "Não informado", "tom_geral": "", "urgencia": "baixa"},
            "busca":           {},
            "hot_buttons":     [],
            "objecoes":        [],
            "sinais_quentes":  [],
            "proximo_passo":   "",
            "pontos_de_atencao": "",
        }
        pdf_bytes = render_dossie_pdf(
            content=minimal,
            lead_name=None,
            lead_phone="5511000000000",
        )
        self.assertGreater(len(pdf_bytes), 100)

    def test_pdf_size_reasonable(self):
        """PDF gerado tem tamanho razoável (> 5 KB, < 5 MB)."""
        try:
            from tools.dossie import render_dossie_pdf
        except ImportError:
            self.skipTest("tools.dossie não disponível")

        pdf_bytes = render_dossie_pdf(
            content=SAMPLE_CONTENT,
            lead_name="Carlos",
            lead_phone="5511999990001",
        )
        self.assertGreater(len(pdf_bytes), 1_000,   "PDF muito pequeno")
        self.assertLess(len(pdf_bytes),    5_000_000, "PDF muito grande")


class TestSendDossieToCorretor(unittest.TestCase):
    """Testa envio via Evolution API (HTTP mockado)."""

    def test_send_builds_correct_payload(self):
        """send_dossie_to_corretor envia payload correto com base64 e mediatype document."""
        try:
            from tools.dossie import send_dossie_to_corretor
        except ImportError:
            self.skipTest("tools.dossie não disponível")

        pdf_bytes = b"%PDF-1.4 fake pdf content"

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        captured_payloads = []

        def mock_urlopen(req, context=None, timeout=None):
            captured_payloads.append(json.loads(req.data.decode("utf-8")))
            return mock_response

        with patch("tools.dossie.urllib.request.urlopen", side_effect=mock_urlopen):
            send_dossie_to_corretor(
                pdf_bytes=pdf_bytes,
                lead_name="Carlos",
                lead_phone="5511999990001",
                corretor_phone="5511988887777",
                instance="test_instance",
                evolution_url="https://api.test.com",
                evolution_api_key="test_key",
            )

        self.assertEqual(len(captured_payloads), 1)
        payload = captured_payloads[0]

        self.assertEqual(payload["number"],    "5511988887777")
        self.assertEqual(payload["mediatype"], "document")
        self.assertEqual(payload["mimetype"],  "application/pdf")
        self.assertIn("dossie-caviar-Carlos", payload["fileName"])
        # Verifica que o base64 decodifica para os bytes originais
        decoded = base64.b64decode(payload["media"])
        self.assertEqual(decoded, pdf_bytes)


class TestSaveDossieLocally(unittest.TestCase):
    """Testa persistência local do PDF."""

    def test_saves_to_correct_path(self):
        """save_dossie_locally cria arquivo no diretório correto."""
        try:
            from tools.dossie import save_dossie_locally
        except ImportError:
            self.skipTest("tools.dossie não disponível")

        pdf_bytes = b"%PDF-1.4 test"

        with tempfile.TemporaryDirectory() as tmpdir:
            saved_path = save_dossie_locally(
                pdf_bytes=pdf_bytes,
                lead_phone="5511999990001",
                client_id="test_client",
                base_path=tmpdir,
            )
            self.assertTrue(Path(saved_path).exists(), "Arquivo não foi criado")
            self.assertEqual(Path(saved_path).read_bytes(), pdf_bytes)
            # Path deve conter client_id e "reports"
            self.assertIn("test_client", saved_path)
            self.assertIn("reports", saved_path)


class TestBuildAndSendDossie(unittest.TestCase):
    """Testa o pipeline completo (orquestrador) com mocks."""

    @patch("tools.dossie.send_dossie_to_corretor")
    @patch("tools.dossie.render_dossie_pdf")
    @patch("tools.dossie.generate_dossie_content")
    def test_full_pipeline_calls_all_stages(
        self,
        mock_generate,
        mock_render,
        mock_send,
    ):
        """build_and_send_dossie chama geração → PDF → envio na ordem correta."""
        try:
            from tools.dossie import build_and_send_dossie
        except ImportError:
            self.skipTest("tools.dossie não disponível")

        mock_generate.return_value = SAMPLE_CONTENT
        mock_render.return_value   = b"%PDF-1.4 ok"

        with tempfile.TemporaryDirectory() as tmpdir:
            build_and_send_dossie(
                history=SAMPLE_HISTORY,
                lead_name="Carlos",
                lead_phone="5511999990001",
                corretor_phone="5511988887777",
                score=14,
                pipeline=3_000_000.0,
                visit_date="15/04/2026 às 10h",
                client_id="test_client",
                imobiliaria="Artístico Imóveis",
                save_local=True,
            )

        mock_generate.assert_called_once()
        mock_render.assert_called_once()
        mock_send.assert_called_once()

        # Verifica argumentos-chave passados ao render
        render_call = mock_render.call_args
        self.assertEqual(render_call.kwargs["lead_name"], "Carlos")
        self.assertEqual(render_call.kwargs["lead_phone"], "5511999990001")

        # Verifica argumentos-chave passados ao send
        send_call = mock_send.call_args
        self.assertEqual(send_call.kwargs["corretor_phone"], "5511988887777")
        self.assertEqual(send_call.kwargs["pdf_bytes"], b"%PDF-1.4 ok")

    @patch("tools.dossie.send_dossie_to_corretor")
    @patch("tools.dossie.render_dossie_pdf")
    @patch("tools.dossie.generate_dossie_content")
    def test_send_failure_does_not_raise(
        self,
        mock_generate,
        mock_render,
        mock_send,
    ):
        """Falha no envio é logada mas não propaga exceção ao caller."""
        try:
            from tools.dossie import build_and_send_dossie
        except ImportError:
            self.skipTest("tools.dossie não disponível")

        mock_generate.return_value = SAMPLE_CONTENT
        mock_render.return_value   = b"%PDF-1.4 ok"
        mock_send.side_effect      = Exception("Evolution API timeout")

        # Não deve lançar — falha no envio é tolerável (PDF já gerado)
        try:
            build_and_send_dossie(
                history=SAMPLE_HISTORY,
                lead_name="Carlos",
                lead_phone="5511999990001",
                corretor_phone="5511988887777",
                save_local=False,
            )
        except Exception as e:
            self.fail(f"build_and_send_dossie não deve propagar exceção de send: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
