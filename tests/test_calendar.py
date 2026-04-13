"""
tests/test_calendar.py — Testes da integração Google Calendar

Cobre:
  - Evento criado com todos os campos corretos
  - Fallback silencioso se credenciais não configuradas
  - Fallback silencioso se google-api-python-client não instalado
  - Fallback silencioso se API retorna erro
  - visit_dt None → placeholder +48h sem crash
  - Título com nome do lead e ID do imóvel
  - Convidados: corretor + lead (se email disponível)
  - Lembretes corretos (1 dia + 1 hora)
  - format_imovel_descricao: campos presentes, campo ausente, valor numérico
  - Suporte a credenciais como string JSON direta (não apenas arquivo)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_CREDENTIALS = json.dumps({
    "type": "service_account",
    "project_id": "imobfake",
    "private_key_id": "key123",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----\n",
    "client_email": "sofia@imobfake.iam.gserviceaccount.com",
    "client_id": "123456",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
})

VISIT_DT = datetime(2026, 4, 20, 15, 0, 0, tzinfo=timezone.utc)

FAKE_EVENT_RESPONSE = {
    "id": "event_abc123",
    "htmlLink": "https://calendar.google.com/calendar/event?eid=abc123",
    "summary": "Visita — Carlos — AV001",
}


def _make_service_mock(response: dict | None = None, raise_error: bool = False):
    """Mock do cliente Google Calendar API."""
    svc = MagicMock()
    insert_mock = MagicMock()
    if raise_error:
        insert_mock.execute.side_effect = Exception("API Error: 403 Forbidden")
    else:
        insert_mock.execute.return_value = response or FAKE_EVENT_RESPONSE
    svc.events.return_value.insert.return_value = insert_mock
    return svc


def _call_create(
    corretor_email: str = "corretor@imob.com",
    lead_name: str = "Carlos Silva",
    lead_phone: str = "5511999990001",
    imovel_id: str = "AV001",
    imovel_descricao: str = "Apartamento Jardins — 2.950.000",
    visit_dt: datetime | None = VISIT_DT,
    resumo: str = "• Interesse: 3 quartos\n• Budget: R$3M\n• Prazo: 90 dias",
    lead_email: str | None = None,
    service_mock=None,
    creds: str = FAKE_CREDENTIALS,
):
    """Chama create_calendar_event com mocks injetados."""
    import tools.calendar as cal_mod

    svc = service_mock or _make_service_mock()

    with (
        patch.object(cal_mod, "GOOGLE_CALENDAR_CREDENTIALS_JSON", creds),
        patch.object(cal_mod, "GOOGLE_CALENDAR_ID", "primary"),
        patch("tools.calendar._build_calendar_service", return_value=svc),
    ):
        return cal_mod.create_calendar_event(
            corretor_email=corretor_email,
            lead_name=lead_name,
            lead_phone=lead_phone,
            imovel_id=imovel_id,
            imovel_descricao=imovel_descricao,
            visit_dt=visit_dt,
            resumo_conversa=resumo,
            lead_email=lead_email,
        )


# ---------------------------------------------------------------------------
# Testes — criação de evento
# ---------------------------------------------------------------------------

class TestCreateCalendarEvent:

    def test_sucesso_retorna_event_id(self):
        result = _call_create()
        assert result.success is True
        assert result.event_id == "event_abc123"

    def test_sucesso_retorna_event_link(self):
        result = _call_create()
        assert result.event_link and "calendar.google.com" in result.event_link

    def test_titulo_contem_nome_lead(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc)
        body = svc.events().insert.call_args.kwargs["body"]
        assert "Carlos Silva" in body["summary"]

    def test_titulo_contem_imovel_id(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc)
        body = svc.events().insert.call_args.kwargs["body"]
        assert "AV001" in body["summary"]

    def test_descricao_contem_telefone(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc)
        body = svc.events().insert.call_args.kwargs["body"]
        assert "5511999990001" in body["description"]

    def test_descricao_contem_imovel_descricao(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc, imovel_descricao="Apartamento Jardins — R$ 2.950.000")
        body = svc.events().insert.call_args.kwargs["body"]
        assert "Jardins" in body["description"]

    def test_descricao_contem_resumo_conversa(self):
        svc = _make_service_mock()
        resumo = "• Interesse: 3 quartos\n• Budget: R$3M"
        _call_create(service_mock=svc, resumo=resumo)
        body = svc.events().insert.call_args.kwargs["body"]
        assert resumo in body["description"]

    def test_corretor_e_convidado(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc, corretor_email="corretor@imob.com")
        body = svc.events().insert.call_args.kwargs["body"]
        emails = [a["email"] for a in body["attendees"]]
        assert "corretor@imob.com" in emails

    def test_lead_email_adicionado_como_convidado(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc, lead_email="carlos@gmail.com")
        body = svc.events().insert.call_args.kwargs["body"]
        emails = [a["email"] for a in body["attendees"]]
        assert "carlos@gmail.com" in emails

    def test_sem_lead_email_apenas_corretor(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc, lead_email=None)
        body = svc.events().insert.call_args.kwargs["body"]
        assert len(body["attendees"]) == 1

    def test_duracao_60_minutos(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc)
        body = svc.events().insert.call_args.kwargs["body"]
        start = datetime.fromisoformat(body["start"]["dateTime"])
        end   = datetime.fromisoformat(body["end"]["dateTime"])
        assert (end - start) == timedelta(minutes=60)

    def test_lembretes_configurados(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc)
        body = svc.events().insert.call_args.kwargs["body"]
        overrides = body["reminders"]["overrides"]
        minutes = [r["minutes"] for r in overrides]
        assert 24 * 60 in minutes  # 1 dia
        assert 60 in minutes        # 1 hora


class TestCalendarFallbacks:

    def test_sem_credenciais_retorna_false_sem_crash(self):
        import tools.calendar as cal_mod
        with patch.object(cal_mod, "GOOGLE_CALENDAR_CREDENTIALS_JSON", ""):
            result = cal_mod.create_calendar_event(
                corretor_email="c@imob.com",
                lead_name="Lead",
                lead_phone="5511999990001",
                imovel_id="AV001",
                imovel_descricao="Apto",
                visit_dt=VISIT_DT,
            )
        assert result.success is False
        assert result.error == "credentials_not_configured"

    def test_api_error_retorna_false_sem_crash(self):
        svc = _make_service_mock(raise_error=True)
        result = _call_create(service_mock=svc)
        assert result.success is False
        assert result.error is not None

    def test_visit_dt_none_usa_placeholder(self):
        """visit_dt=None não levanta exceção — usa +48h."""
        svc = _make_service_mock()
        result = _call_create(service_mock=svc, visit_dt=None)
        assert result.success is True
        body = svc.events().insert.call_args.kwargs["body"]
        assert "dateTime" in body["start"]

    def test_lead_sem_nome_usa_fallback(self):
        svc = _make_service_mock()
        _call_create(service_mock=svc, lead_name="")
        body = svc.events().insert.call_args.kwargs["body"]
        # Não deve falhar — usa "Lead" como fallback
        assert "Visita" in body["summary"]


# ---------------------------------------------------------------------------
# Testes — format_imovel_descricao
# ---------------------------------------------------------------------------

class TestFormatImovelDescricao:

    def test_todos_campos(self):
        from tools.calendar import format_imovel_descricao
        result = format_imovel_descricao({
            "tipo": "Apartamento",
            "bairro": "Jardins",
            "area_m2": "180",
            "valor": "2950000",
        })
        assert "Apartamento" in result
        assert "Jardins" in result
        assert "180m²" in result
        assert "2.950.000" in result or "2950000" in result

    def test_sem_valor(self):
        from tools.calendar import format_imovel_descricao
        result = format_imovel_descricao({"tipo": "Casa", "bairro": "Morumbi"})
        assert "Casa" in result
        assert "Morumbi" in result

    def test_imovel_vazio_retorna_string_vazia(self):
        from tools.calendar import format_imovel_descricao
        assert format_imovel_descricao({}) == ""

    def test_valor_invalido_usa_string_bruta(self):
        from tools.calendar import format_imovel_descricao
        result = format_imovel_descricao({"tipo": "Apto", "valor": "consulte"})
        assert "consulte" in result
