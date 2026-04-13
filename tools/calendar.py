"""
tools/calendar.py — Integração com Google Calendar para o corretor

Cria automaticamente um evento no Google Calendar quando Sofia confirma uma visita.
Usa conta de serviço (service account) — sem OAuth por corretor, sem login manual.

Setup (uma vez por cliente):
  1. Criar Service Account no Google Cloud Console com permissão ao Calendar API
  2. Compartilhar o calendário do corretor (ou calendário compartilhado da imobiliária)
     com o e-mail da service account (permissão "Fazer alterações em eventos")
  3. Definir no onboarding.json: corretores[*].corretor_email
  4. Definir nas env vars:
       GOOGLE_CALENDAR_CREDENTIALS_JSON — caminho para o JSON da service account
                                          OU o JSON em string (para Docker/env direto)
       GOOGLE_CALENDAR_ID               — ID do calendário (padrão: "primary")

Fallback: se as credenciais não estiverem configuradas, a função retorna None
silenciosamente — o fluxo de atendimento nunca é bloqueado.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("calendar_tool")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GOOGLE_CALENDAR_CREDENTIALS_JSON = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_JSON", "")
GOOGLE_CALENDAR_ID                = os.getenv("GOOGLE_CALENDAR_ID", "primary")
DEFAULT_VISIT_DURATION_MINUTES    = 60


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------

class CalendarEventResult:
    def __init__(
        self,
        success: bool,
        event_id: str | None = None,
        event_link: str | None = None,
        error: str | None = None,
    ):
        self.success    = success
        self.event_id   = event_id
        self.event_link = event_link
        self.error      = error

    def __repr__(self) -> str:
        if self.success:
            return f"CalendarEventResult(ok, id={self.event_id})"
        return f"CalendarEventResult(fail, error={self.error})"


# ---------------------------------------------------------------------------
# Client da Google Calendar API
# ---------------------------------------------------------------------------

def _build_calendar_service():
    """
    Constrói o cliente autenticado da Google Calendar API.
    Retorna None se as credenciais não estiverem configuradas.
    """
    if not GOOGLE_CALENDAR_CREDENTIALS_JSON:
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds_raw = GOOGLE_CALENDAR_CREDENTIALS_JSON.strip()

        # Aceita caminho para arquivo JSON ou o JSON em string diretamente
        if creds_raw.startswith("{"):
            creds_info = json.loads(creds_raw)
        else:
            p = Path(creds_raw)
            if not p.exists():
                log.warning("calendar: arquivo de credenciais não encontrado: %s", creds_raw)
                return None
            creds_info = json.loads(p.read_text())

        scopes = ["https://www.googleapis.com/auth/calendar"]
        credentials = service_account.Credentials.from_service_account_info(
            creds_info, scopes=scopes
        )
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    except ImportError:
        log.warning(
            "calendar: google-auth ou google-api-python-client não instalados. "
            "Execute: pip install google-auth google-api-python-client"
        )
        return None
    except Exception as e:
        log.warning("calendar: falha ao construir cliente: %s", e)
        return None


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------

def create_calendar_event(
    corretor_email: str,
    lead_name: str,
    lead_phone: str,
    imovel_id: str,
    imovel_descricao: str,
    visit_dt: datetime | None,
    resumo_conversa: str = "",
    lead_email: str | None = None,
    calendar_id: str | None = None,
) -> CalendarEventResult:
    """
    Cria um evento no Google Calendar do corretor para a visita confirmada.

    Args:
        corretor_email:     E-mail do corretor responsável pelo lead.
        lead_name:          Nome do lead (pode ser desconhecido).
        lead_phone:         Telefone do lead (formatado para exibição).
        imovel_id:          ID do imóvel de interesse (ex: "AV004").
        imovel_descricao:   Descrição curta (ex: "Casa Jardim Europa — 8.5M").
        visit_dt:           Data/hora da visita (UTC). Se None, usa +48h como placeholder.
        resumo_conversa:    Resumo gerado via Haiku da conversa com o lead.
        lead_email:         E-mail do lead para adicionar como convidado (opcional).
        calendar_id:        ID do calendário alvo. Usa GOOGLE_CALENDAR_ID se None.

    Returns:
        CalendarEventResult com success=True e event_id/event_link se criado com sucesso,
        ou success=False com error se falhou. Nunca levanta exceção.
    """
    service = _build_calendar_service()
    if not service:
        log.debug("calendar: serviço não disponível — fallback silencioso")
        return CalendarEventResult(success=False, error="credentials_not_configured")

    try:
        cal_id = calendar_id or GOOGLE_CALENDAR_ID

        # Data/hora da visita — placeholder +48h se não parseada
        if visit_dt is None:
            visit_dt = datetime.now(timezone.utc) + timedelta(hours=48)
            log.info("calendar: data de visita não parseada — usando placeholder +48h")

        start_dt = visit_dt
        end_dt   = start_dt + timedelta(minutes=DEFAULT_VISIT_DURATION_MINUTES)

        lead_display = lead_name if lead_name and lead_name.lower() != "lead" else "Lead"
        title = f"Visita — {lead_display} — {imovel_id}"

        description_parts = [
            f"📋 BRIEFING DA VISITA",
            f"",
            f"👤 Lead: {lead_display}",
            f"📱 Telefone: {lead_phone}",
            f"🏠 Imóvel: {imovel_descricao}" if imovel_descricao else f"🏠 Imóvel: {imovel_id}",
            f"",
        ]
        if resumo_conversa:
            description_parts += [
                f"💬 Resumo da conversa:",
                resumo_conversa,
                f"",
            ]
        description_parts.append("📊 Histórico completo disponível no dashboard ImobOne.")
        description = "\n".join(description_parts)

        attendees = [{"email": corretor_email}]
        if lead_email:
            attendees.append({"email": lead_email})

        event_body: dict[str, Any] = {
            "summary":     title,
            "description": description,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": "America/Sao_Paulo",
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": "America/Sao_Paulo",
            },
            "attendees":    attendees,
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email",  "minutes": 24 * 60},  # 1 dia antes
                    {"method": "popup",  "minutes": 60},        # 1 hora antes
                ],
            },
            "colorId": "2",  # verde — visita confirmada
        }

        result = (
            service.events()
            .insert(calendarId=cal_id, body=event_body, sendUpdates="all")
            .execute()
        )

        event_id   = result.get("id", "")
        event_link = result.get("htmlLink", "")
        log.info(
            "Evento de visita criado: %s | corretor: %s | imóvel: %s | data: %s",
            event_id, corretor_email, imovel_id, start_dt.strftime("%d/%m %H:%M"),
        )
        return CalendarEventResult(success=True, event_id=event_id, event_link=event_link)

    except Exception as e:
        log.warning("calendar: falha ao criar evento: %s", e)
        return CalendarEventResult(success=False, error=str(e))


# ---------------------------------------------------------------------------
# Helper: monta descrição do imóvel a partir do portfólio
# ---------------------------------------------------------------------------

def format_imovel_descricao(imovel: dict) -> str:
    """Formata linha curta de descrição do imóvel para o evento de calendário."""
    if not imovel:
        return ""
    tipo    = imovel.get("tipo", "")
    bairro  = imovel.get("bairro", "")
    valor   = imovel.get("valor", "")
    area    = imovel.get("area_m2", "")

    parts = []
    if tipo:
        parts.append(tipo)
    if bairro:
        parts.append(bairro)
    if area:
        parts.append(f"{area}m²")
    if valor:
        try:
            v = float(str(valor).replace(".", "").replace(",", "."))
            parts.append(f"R$ {v:,.0f}".replace(",", "."))
        except (ValueError, TypeError):
            parts.append(str(valor))

    return " — ".join(parts) if parts else ""
