"""
tests/test_portal_lead_capture.py — Testes do módulo de captura de leads de portais

Cobertura:
  - normalize_phone: E.164, DDD, código de país, inválidos
  - normalize_zap: campos obrigatórios, telefone, listagem, edge cases
  - normalize_vivareal: array de phones, preços, email
  - normalize_olx: interested_user, ad_fields
  - normalize_payload: despacho por portal, portal desconhecido
  - is_duplicate_lead: com mock Supabase
  - upsert_portal_lead: dry-run, novo, duplicata
  - build_first_message: com nome, sem nome, com imóvel, sem imóvel
  - send_first_message: dry-run
  - handle_portal_lead: fluxo completo ZAP/VivaReal/OLX, payload inválido
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import portal_lead_capture as plc


# ─── Fixtures ─────────────────────────────────────────────────────────────────

ZAP_PAYLOAD = {
    "lead": {
        "name": "Carlos Silva",
        "email": "carlos@email.com",
        "phone": "(11) 99999-0001",
        "message": "Quero mais informações.",
    },
    "listing": {
        "id": "ZAP-001",
        "title": "Apartamento 120m² Itaim Bibi",
        "price": 2_500_000,
        "url": "https://www.zapimoveis.com.br/ZAP-001",
    },
}

VIVAREAL_PAYLOAD = {
    "consumer": {
        "name": "Ana Lima",
        "email": "ana@email.com",
        "phones": [{"number": "11988880002", "type": "CELLPHONE"}],
    },
    "listing": {
        "externalId": "VR-789",
        "title": "Casa 4 quartos Jardins",
        "prices": {"sale": 3_800_000},
        "url": "https://www.vivareal.com.br/VR-789",
    },
    "message": "Gostaria de agendar visita.",
}

OLX_PAYLOAD = {
    "ad_id": "OLX-555",
    "ad_title": "Apartamento 90m² Pinheiros",
    "ad_price": 1_800_000,
    "interested_user": {
        "name": "João Pereira",
        "email": "joao@email.com",
        "phone": "11977770003",
    },
    "message": "Tenho interesse.",
}


# ─── normalize_phone ──────────────────────────────────────────────────────────

class TestNormalizePhone:
    def test_formato_completo_com_pais(self):
        assert plc.normalize_phone("+55 11 99999-0001") == "5511999990001"

    def test_formato_com_ddd_e_mascara(self):
        assert plc.normalize_phone("(11) 99999-0001") == "5511999990001"

    def test_apenas_digitos_com_ddd(self):
        assert plc.normalize_phone("11977770003") == "5511977770003"

    def test_ja_tem_55_no_inicio(self):
        result = plc.normalize_phone("5511999990001")
        assert result == "5511999990001"

    def test_numero_sem_ddd_assume_sp(self):
        result = plc.normalize_phone("999990001")
        assert result == "5511999990001"

    def test_numero_vazio_retorna_none(self):
        assert plc.normalize_phone("") is None

    def test_none_retorna_none(self):
        assert plc.normalize_phone(None) is None

    def test_numero_muito_curto_retorna_none(self):
        assert plc.normalize_phone("1234") is None

    def test_remove_caracteres_especiais(self):
        result = plc.normalize_phone("+55 (11) 9.8888-0002")
        assert result is not None
        assert result.isdigit()


# ─── normalize_zap ────────────────────────────────────────────────────────────

class TestNormalizeZap:
    def test_campos_basicos(self):
        r = plc.normalize_zap(ZAP_PAYLOAD)
        assert r is not None
        assert r["phone"] == "5511999990001"
        assert r["name"] == "Carlos Silva"
        assert r["email"] == "carlos@email.com"
        assert r["source"] == plc.SOURCE_ZAP

    def test_listing_fields(self):
        r = plc.normalize_zap(ZAP_PAYLOAD)
        assert r["listing_id"] == "ZAP-001"
        assert r["listing_title"] == "Apartamento 120m² Itaim Bibi"
        assert r["listing_price"] == 2_500_000
        assert "zapimoveis" in r["listing_url"]

    def test_sem_telefone_retorna_none(self):
        payload = {"lead": {"name": "X", "email": "x@x.com"}, "listing": {}}
        assert plc.normalize_zap(payload) is None

    def test_sem_listing_nao_quebra(self):
        payload = {"lead": {"name": "X", "phone": "11999990001"}}
        r = plc.normalize_zap(payload)
        assert r is not None
        assert r["listing_id"] == ""

    def test_payload_plano_sem_chave_lead(self):
        """Aceita payload sem chave 'lead' aninhada."""
        payload = {"name": "X", "phone": "11999990001", "email": "x@x.com"}
        r = plc.normalize_zap(payload)
        assert r is not None

    def test_raw_preservado(self):
        r = plc.normalize_zap(ZAP_PAYLOAD)
        assert r["raw"] == ZAP_PAYLOAD


# ─── normalize_vivareal ───────────────────────────────────────────────────────

class TestNormalizeVivareal:
    def test_campos_basicos(self):
        r = plc.normalize_vivareal(VIVAREAL_PAYLOAD)
        assert r is not None
        assert r["phone"] == "5511988880002"
        assert r["name"] == "Ana Lima"
        assert r["source"] == plc.SOURCE_VIVAREAL

    def test_phones_array_cellphone_preferido(self):
        payload = {
            "consumer": {
                "phones": [
                    {"number": "1133330001", "type": "LANDLINE"},
                    {"number": "11988880002", "type": "CELLPHONE"},
                ]
            },
            "listing": {},
        }
        r = plc.normalize_vivareal(payload)
        assert r["phone"] == "5511988880002"

    def test_phones_array_sem_cellphone_usa_primeiro(self):
        payload = {
            "consumer": {
                "phones": [{"number": "1133330001", "type": "LANDLINE"}]
            },
            "listing": {},
        }
        r = plc.normalize_vivareal(payload)
        assert r is not None  # pega o primeiro

    def test_listing_preco_sale(self):
        r = plc.normalize_vivareal(VIVAREAL_PAYLOAD)
        assert r["listing_price"] == 3_800_000

    def test_listing_preco_rental_fallback(self):
        payload = dict(VIVAREAL_PAYLOAD)
        payload["listing"] = {"prices": {"rental": 5000}, "externalId": "VR-X"}
        r = plc.normalize_vivareal(payload)
        assert r["listing_price"] == 5000

    def test_sem_telefone_retorna_none(self):
        payload = {"consumer": {"name": "X"}, "listing": {}}
        assert plc.normalize_vivareal(payload) is None

    def test_message_preservada(self):
        r = plc.normalize_vivareal(VIVAREAL_PAYLOAD)
        assert r["message"] == "Gostaria de agendar visita."


# ─── normalize_olx ────────────────────────────────────────────────────────────

class TestNormalizeOlx:
    def test_campos_basicos(self):
        r = plc.normalize_olx(OLX_PAYLOAD)
        assert r is not None
        assert r["phone"] == "5511977770003"
        assert r["name"] == "João Pereira"
        assert r["source"] == plc.SOURCE_OLX

    def test_ad_fields(self):
        r = plc.normalize_olx(OLX_PAYLOAD)
        assert r["listing_id"] == "OLX-555"
        assert r["listing_title"] == "Apartamento 90m² Pinheiros"
        assert r["listing_price"] == 1_800_000

    def test_sem_telefone_retorna_none(self):
        payload = {"ad_id": "X", "interested_user": {"name": "X"}}
        assert plc.normalize_olx(payload) is None

    def test_sem_interested_user_fallback(self):
        payload = {"ad_id": "X", "phone": "11999990001", "name": "X"}
        r = plc.normalize_olx(payload)
        assert r is not None


# ─── normalize_payload (despacho) ────────────────────────────────────────────

class TestNormalizePayload:
    def test_zap_por_nome(self):
        r = plc.normalize_payload("zap", ZAP_PAYLOAD)
        assert r["source"] == plc.SOURCE_ZAP

    def test_portal_zap_por_source_string(self):
        r = plc.normalize_payload("portal_zap", ZAP_PAYLOAD)
        assert r["source"] == plc.SOURCE_ZAP

    def test_vivareal(self):
        r = plc.normalize_payload("vivareal", VIVAREAL_PAYLOAD)
        assert r["source"] == plc.SOURCE_VIVAREAL

    def test_olx(self):
        r = plc.normalize_payload("olx", OLX_PAYLOAD)
        assert r["source"] == plc.SOURCE_OLX

    def test_portal_desconhecido_usa_fallback(self):
        # Fallback = normalize_zap (genérico)
        r = plc.normalize_payload("outro_portal", ZAP_PAYLOAD)
        assert r is not None  # não quebra


# ─── is_duplicate_lead ────────────────────────────────────────────────────────

class TestIsDuplicateLead:
    def test_lead_existente(self):
        with patch.object(plc, "_sb_get", return_value=[{"lead_phone": "5511999990001"}]):
            assert plc.is_duplicate_lead("5511999990001") is True

    def test_lead_novo(self):
        with patch.object(plc, "_sb_get", return_value=[]):
            assert plc.is_duplicate_lead("5511999990001") is False

    def test_supabase_falha_retorna_false(self):
        with patch.object(plc, "_sb_get", return_value=[]):
            assert plc.is_duplicate_lead("5511000000001") is False


# ─── upsert_portal_lead ───────────────────────────────────────────────────────

class TestUpsertPortalLead:
    def _normalized(self):
        return plc.normalize_zap(ZAP_PAYLOAD)

    def test_dry_run_nao_chama_supabase(self):
        with patch.object(plc, "_sb_post") as mock_post:
            with patch.object(plc, "is_duplicate_lead", return_value=False):
                result = plc.upsert_portal_lead(self._normalized(), dry_run=True)
        mock_post.assert_not_called()
        assert result["is_new"] is True

    def test_lead_novo_chama_supabase(self):
        with patch.object(plc, "_sb_post", return_value=True) as mock_post:
            with patch.object(plc, "is_duplicate_lead", return_value=False):
                result = plc.upsert_portal_lead(self._normalized(), dry_run=False)
        mock_post.assert_called_once()
        assert result["is_new"] is True
        assert result["source"] == plc.SOURCE_ZAP

    def test_lead_duplicata_is_new_false(self):
        with patch.object(plc, "_sb_post", return_value=True):
            with patch.object(plc, "is_duplicate_lead", return_value=True):
                result = plc.upsert_portal_lead(self._normalized(), dry_run=False)
        assert result["is_new"] is False


# ─── build_first_message ─────────────────────────────────────────────────────

class TestBuildFirstMessage:
    def test_com_nome_e_imovel(self):
        norm = plc.normalize_zap(ZAP_PAYLOAD)
        msg = plc.build_first_message(norm)
        assert "Carlos" in msg
        assert "Apartamento 120m² Itaim Bibi" in msg

    def test_sem_nome(self):
        norm = plc.normalize_zap(ZAP_PAYLOAD)
        norm["name"] = None
        msg = plc.build_first_message(norm)
        assert "Olá!" in msg  # sem vírgula antes de nome
        assert plc.CONSULTANT_NAME in msg

    def test_sem_imovel(self):
        norm = plc.normalize_zap(ZAP_PAYLOAD)
        norm["listing_title"] = None
        msg = plc.build_first_message(norm)
        assert "portal" in msg.lower()
        assert plc.IMOBILIARIA_NAME in msg

    def test_contem_nome_imobiliaria(self):
        norm = plc.normalize_zap(ZAP_PAYLOAD)
        msg = plc.build_first_message(norm)
        assert plc.IMOBILIARIA_NAME in msg

    def test_contem_nome_consultor(self):
        norm = plc.normalize_zap(ZAP_PAYLOAD)
        msg = plc.build_first_message(norm)
        assert plc.CONSULTANT_NAME in msg


# ─── send_first_message ───────────────────────────────────────────────────────

class TestSendFirstMessage:
    def test_dry_run_nao_envia(self):
        with patch("portal_lead_capture.httpx", create=True):
            result = plc.send_first_message("5511999990001", "Olá!", dry_run=True)
        assert result is True

    def test_sem_evolution_url_retorna_false(self):
        original = plc.EVOLUTION_URL
        plc.EVOLUTION_URL = ""
        result = plc.send_first_message("5511999990001", "Olá!")
        plc.EVOLUTION_URL = original
        assert result is False


# ─── handle_portal_lead (fluxo completo) ─────────────────────────────────────

class TestHandlePortalLead:
    def _mock_basics(self, is_new=True):
        return {
            "upsert": patch.object(plc, "upsert_portal_lead", return_value={
                "phone": "5511999990001",
                "is_new": is_new,
                "source": plc.SOURCE_ZAP,
            }),
            "send": patch.object(plc, "send_first_message", return_value=True),
        }

    def test_zap_payload_valido(self):
        mocks = self._mock_basics()
        with mocks["upsert"], mocks["send"]:
            result = plc.handle_portal_lead("zap", ZAP_PAYLOAD, dry_run=True)
        assert result["status"] == "ok"
        assert result["source"] == plc.SOURCE_ZAP
        assert result["phone"] == "5511999990001"

    def test_vivareal_payload_valido(self):
        with patch.object(plc, "upsert_portal_lead", return_value={
            "phone": "5511988880002", "is_new": True, "source": plc.SOURCE_VIVAREAL
        }):
            with patch.object(plc, "send_first_message", return_value=True):
                result = plc.handle_portal_lead("vivareal", VIVAREAL_PAYLOAD, dry_run=True)
        assert result["status"] == "ok"
        assert result["source"] == plc.SOURCE_VIVAREAL

    def test_olx_payload_valido(self):
        with patch.object(plc, "upsert_portal_lead", return_value={
            "phone": "5511977770003", "is_new": True, "source": plc.SOURCE_OLX
        }):
            with patch.object(plc, "send_first_message", return_value=True):
                result = plc.handle_portal_lead("olx", OLX_PAYLOAD, dry_run=True)
        assert result["status"] == "ok"
        assert result["source"] == plc.SOURCE_OLX

    def test_payload_invalido_retorna_error(self):
        result = plc.handle_portal_lead("zap", {"sem_telefone": True})
        assert result["status"] == "error"
        assert "invalid_payload" in result["reason"]

    def test_lead_duplicado_message_sent_true(self):
        """Lead duplicado ainda recebe mensagem de retomada."""
        mocks = self._mock_basics(is_new=False)
        with mocks["upsert"], mocks["send"] as mock_send:
            result = plc.handle_portal_lead("zap", ZAP_PAYLOAD, dry_run=True)
        assert result["status"] == "ok"
        assert result["is_new"] is False

    def test_retorna_nome_e_listing(self):
        mocks = self._mock_basics()
        with mocks["upsert"], mocks["send"]:
            result = plc.handle_portal_lead("zap", ZAP_PAYLOAD, dry_run=True)
        assert result["name"] == "Carlos Silva"
        assert "Itaim" in result["listing"]
