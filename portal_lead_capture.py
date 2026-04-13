"""
portal_lead_capture.py — Captura automática de leads de portais imobiliários

Portais suportados:
  - ZAP Imóveis     (https://www.zapimoveis.com.br)
  - VivaReal         (https://www.vivareal.com.br)
  - OLX Imóveis     (https://www.olx.com.br)

Fluxo:
  Portal dispara webhook POST /portal-lead
  → Normaliza payload para schema interno
  → Verifica duplicata por telefone no Supabase
  → Cria/atualiza lead com source = portal_zap | portal_vivareal | portal_olx
  → Dispara primeira mensagem da Sofia em até 2 minutos

Endpoints adicionados ao whatsapp_webhook.py / pipeline_runner.py:
  POST /portal-lead   — recebe payload do portal

Uso standalone:
  python3 portal_lead_capture.py --test-zap       # simula payload ZAP
  python3 portal_lead_capture.py --test-vivareal   # simula payload VivaReal
  python3 portal_lead_capture.py --test-olx        # simula payload OLX
"""

import json
import logging
import os
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("portal_lead_capture")

# ─── Config (mesmas vars do webhook) ─────────────────────────────────────────
SUPABASE_URL       = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY       = os.getenv("SUPABASE_KEY", "")
EVOLUTION_URL      = os.getenv("EVOLUTION_URL", "https://api.otaviolabs.com")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "devlabz")
CLIENT_ID          = os.getenv("DEMO_CLIENT_ID", "demo_imobiliaria_vendas")
CONSULTANT_NAME    = os.getenv("CONSULTANT_NAME", "Sofia")
IMOBILIARIA_NAME   = os.getenv("IMOBILIARIA_NAME", "Ávora Imóveis")

# Fontes de portal conhecidas
SOURCE_ZAP      = "portal_zap"
SOURCE_VIVAREAL = "portal_vivareal"
SOURCE_OLX      = "portal_olx"
SOURCE_UNKNOWN  = "portal_desconhecido"

KNOWN_SOURCES = {SOURCE_ZAP, SOURCE_VIVAREAL, SOURCE_OLX, SOURCE_UNKNOWN}


# ─── SSL helper ──────────────────────────────────────────────────────────────
def _ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ─── Normalização de telefone ─────────────────────────────────────────────────
def normalize_phone(phone: str) -> Optional[str]:
    """
    Normaliza telefone para formato E.164 sem '+': 5511999990001
    Aceita: (11) 99999-0001, +55 11 99999-0001, 11999990001, etc.
    Retorna None se o número for inválido.
    """
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    # Já com código de país
    if digits.startswith("55") and len(digits) >= 12:
        return digits[:13]  # máx 13 dígitos com DDI
    # Só DDD + número (10-11 dígitos)
    if 10 <= len(digits) <= 11:
        return "55" + digits
    # Número sem DDD (8-9 dígitos) — assume SP
    if 8 <= len(digits) <= 9:
        return "5511" + digits
    return None


# ─── Normalizadores por portal ────────────────────────────────────────────────

def normalize_zap(payload: dict) -> Optional[dict]:
    """
    Normaliza payload do ZAP Imóveis para schema interno.

    Formato ZAP (webhook de lead):
    {
        "lead": {
            "name": "Carlos Silva",
            "email": "carlos@email.com",
            "phone": "(11) 99999-0001",
            "message": "Tenho interesse no apartamento..."
        },
        "listing": {
            "id": "123456",
            "title": "Apartamento 120m² Itaim Bibi",
            "price": 2500000,
            "url": "https://..."
        }
    }
    """
    try:
        lead = payload.get("lead") or payload
        listing = payload.get("listing") or {}

        phone = normalize_phone(
            lead.get("phone") or lead.get("telefone") or lead.get("celular") or ""
        )
        if not phone:
            return None

        return {
            "phone": phone,
            "name": (lead.get("name") or lead.get("nome") or "").strip() or None,
            "email": lead.get("email") or lead.get("e_mail"),
            "message": lead.get("message") or lead.get("mensagem") or lead.get("texto"),
            "source": SOURCE_ZAP,
            "listing_id": str(listing.get("id") or ""),
            "listing_title": listing.get("title") or listing.get("titulo"),
            "listing_price": listing.get("price") or listing.get("valor"),
            "listing_url": listing.get("url"),
            "raw": payload,
        }
    except Exception as e:
        log.warning("Erro ao normalizar payload ZAP: %s", e)
        return None


def normalize_vivareal(payload: dict) -> Optional[dict]:
    """
    Normaliza payload do VivaReal para schema interno.

    Formato VivaReal (webhook de lead):
    {
        "consumer": {
            "name": "Ana Lima",
            "email": "ana@email.com",
            "phones": [{"number": "11988880002", "type": "CELLPHONE"}]
        },
        "listing": {
            "externalId": "VR-789",
            "title": "Casa 4 quartos Jardins",
            "prices": {"sale": 3800000}
        },
        "message": "Gostaria de mais informações..."
    }
    """
    try:
        consumer = payload.get("consumer") or payload
        listing = payload.get("listing") or {}

        # VivaReal usa array de phones
        phones = consumer.get("phones") or []
        raw_phone = ""
        if phones:
            # Prefere CELLPHONE, senão pega o primeiro
            cell = next((p for p in phones if p.get("type") == "CELLPHONE"), phones[0])
            raw_phone = cell.get("number") or cell.get("phone") or ""
        if not raw_phone:
            raw_phone = consumer.get("phone") or consumer.get("telefone") or ""

        phone = normalize_phone(raw_phone)
        if not phone:
            return None

        prices = listing.get("prices") or {}
        price = prices.get("sale") or prices.get("rental") or listing.get("price")

        return {
            "phone": phone,
            "name": (consumer.get("name") or consumer.get("nome") or "").strip() or None,
            "email": consumer.get("email"),
            "message": payload.get("message") or payload.get("mensagem"),
            "source": SOURCE_VIVAREAL,
            "listing_id": str(listing.get("externalId") or listing.get("id") or ""),
            "listing_title": listing.get("title") or listing.get("titulo"),
            "listing_price": price,
            "listing_url": listing.get("url") or listing.get("link"),
            "raw": payload,
        }
    except Exception as e:
        log.warning("Erro ao normalizar payload VivaReal: %s", e)
        return None


def normalize_olx(payload: dict) -> Optional[dict]:
    """
    Normaliza payload OLX para schema interno.

    Formato OLX (webhook de interesse):
    {
        "ad_id": "OLX-555",
        "ad_title": "Apartamento 90m² Pinheiros",
        "ad_price": 1800000,
        "interested_user": {
            "name": "João Pereira",
            "email": "joao@email.com",
            "phone": "11977770003"
        },
        "message": "Tenho interesse. Pode me ligar?"
    }
    """
    try:
        user = payload.get("interested_user") or payload.get("user") or payload

        raw_phone = (
            user.get("phone") or user.get("telefone") or
            payload.get("phone") or payload.get("telefone") or ""
        )
        phone = normalize_phone(raw_phone)
        if not phone:
            return None

        return {
            "phone": phone,
            "name": (user.get("name") or user.get("nome") or "").strip() or None,
            "email": user.get("email"),
            "message": payload.get("message") or payload.get("mensagem"),
            "source": SOURCE_OLX,
            "listing_id": str(payload.get("ad_id") or ""),
            "listing_title": payload.get("ad_title") or payload.get("titulo"),
            "listing_price": payload.get("ad_price") or payload.get("valor"),
            "listing_url": payload.get("ad_url") or payload.get("link"),
            "raw": payload,
        }
    except Exception as e:
        log.warning("Erro ao normalizar payload OLX: %s", e)
        return None


def normalize_payload(portal: str, payload: dict) -> Optional[dict]:
    """Despacha para o normalizador correto pelo nome do portal."""
    normalizers = {
        SOURCE_ZAP: normalize_zap,
        "zap": normalize_zap,
        SOURCE_VIVAREAL: normalize_vivareal,
        "vivareal": normalize_vivareal,
        SOURCE_OLX: normalize_olx,
        "olx": normalize_olx,
    }
    fn = normalizers.get(portal.lower())
    if not fn:
        log.warning("Portal desconhecido: %s — tentando normalização genérica", portal)
        return normalize_zap(payload)  # fallback genérico
    return fn(payload)


# ─── Supabase ─────────────────────────────────────────────────────────────────
def _sb_get(path: str, params: str = "") -> list:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{path}{'?' + params if params else ''}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error("Supabase GET %s: %s", path, e)
        return []


def _sb_post(path: str, data: dict, method: str = "POST") -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15):
            return True
    except Exception as e:
        log.error("Supabase %s %s: %s", method, path, e)
        return False


# ─── Verificação de duplicata ─────────────────────────────────────────────────
def is_duplicate_lead(phone: str, client_id: str = CLIENT_ID) -> bool:
    """
    Retorna True se o lead já existe na base (pelo telefone).
    Nesse caso, não criamos duplicata — apenas registramos novo contato.
    """
    import urllib.parse
    results = _sb_get(
        "leads",
        (
            f"client_id=eq.{urllib.parse.quote(client_id)}"
            f"&lead_phone=eq.{urllib.parse.quote(phone)}"
            f"&select=lead_phone"
        ),
    )
    return len(results) > 0


# ─── Upsert do lead no Supabase ───────────────────────────────────────────────
def upsert_portal_lead(
    normalized: dict,
    client_id: str = CLIENT_ID,
    dry_run: bool = False,
) -> dict:
    """
    Cria ou atualiza o lead no Supabase com origem do portal.
    Retorna dict com {phone, is_new, source}.
    """
    phone = normalized["phone"]
    is_new = not is_duplicate_lead(phone, client_id)

    if dry_run:
        log.info(
            "[DRY-RUN] Lead %s — novo=%s | source=%s | nome=%s",
            phone, is_new, normalized["source"], normalized.get("name"),
        )
        return {"phone": phone, "is_new": is_new, "source": normalized["source"]}

    data = {
        "client_id":       client_id,
        "lead_phone":      phone,
        "source":          normalized["source"],
        "ultima_interacao": datetime.now(timezone.utc).isoformat(),
    }
    if normalized.get("name"):
        data["lead_name"] = normalized["name"]
    if normalized.get("email"):
        data["lead_email"] = normalized.get("email")

    # Inclui título do imóvel como contexto inicial se disponível
    if normalized.get("listing_title"):
        data["listing_context"] = normalized["listing_title"]

    ok = _sb_post("leads", data)
    if ok:
        log.info(
            "Lead %s upserted | novo=%s | source=%s",
            phone, is_new, normalized["source"],
        )
    else:
        log.error("Falha ao upsert lead %s no Supabase", phone)

    return {"phone": phone, "is_new": is_new, "source": normalized["source"]}


# ─── Primeira mensagem da Sofia ───────────────────────────────────────────────
def build_first_message(normalized: dict) -> str:
    """
    Constrói a primeira mensagem da Sofia para o lead do portal.
    Personalizada com o nome (se disponível) e o imóvel de interesse.
    """
    name = normalized.get("name")
    listing_title = normalized.get("listing_title")
    nome_str = f", {name.split()[0]}" if name else ""

    if listing_title:
        return (
            f"Olá{nome_str}! Aqui é a {CONSULTANT_NAME}, da {IMOBILIARIA_NAME}. 😊\n\n"
            f"Vi que você demonstrou interesse em *{listing_title}*. "
            f"Posso te passar mais detalhes sobre este imóvel e outros similares "
            f"que podem te interessar?\n\n"
            f"Em que momento fica melhor para conversarmos?"
        )
    else:
        return (
            f"Olá{nome_str}! Aqui é a {CONSULTANT_NAME}, da {IMOBILIARIA_NAME}. 😊\n\n"
            f"Vi que você entrou em contato pelo portal. "
            f"Pode me contar um pouco mais sobre o que está buscando? "
            f"Vou encontrar as melhores opções do nosso portfólio para você."
        )


def send_first_message(phone: str, message: str, dry_run: bool = False) -> bool:
    """Envia primeira mensagem da Sofia via Evolution API."""
    if dry_run:
        log.info("[DRY-RUN] Enviaria para %s:\n%s", phone, message)
        return True
    try:
        import httpx
        resp = httpx.post(
            f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}",
            json={"number": phone, "text": message},
            headers={"apikey": EVOLUTION_API_KEY},
            verify=False,
            timeout=15,
        )
        log.info("Primeira mensagem enviada → %s | HTTP %s", phone, resp.status_code)
        return resp.status_code < 300
    except Exception as e:
        log.error("Falha ao enviar primeira mensagem → %s: %s", phone, e)
        return False


# ─── Handler principal ────────────────────────────────────────────────────────
def handle_portal_lead(
    portal: str,
    payload: dict,
    client_id: str = CLIENT_ID,
    dry_run: bool = False,
) -> dict:
    """
    Processa um lead recebido de portal imobiliário.

    Args:
        portal:    Nome do portal ("zap", "vivareal", "olx")
        payload:   Payload bruto recebido do webhook do portal
        client_id: ID do cliente ImobOne
        dry_run:   Se True, calcula tudo mas não escreve no Supabase nem envia WhatsApp

    Returns:
        dict com status, phone, is_new, source, message_sent
    """
    log.info("Portal lead recebido | portal=%s | dry_run=%s", portal, dry_run)

    # 1. Normaliza payload
    normalized = normalize_payload(portal, payload)
    if not normalized:
        log.warning("Payload inválido ou sem telefone — lead descartado")
        return {"status": "error", "reason": "invalid_payload_or_missing_phone"}

    phone = normalized["phone"]
    log.info("Lead normalizado: phone=%s | nome=%s | source=%s", phone, normalized.get("name"), normalized["source"])

    # 2. Upsert no Supabase (com deduplicação)
    result = upsert_portal_lead(normalized, client_id=client_id, dry_run=dry_run)
    is_new = result["is_new"]

    # 3. Monta e envia primeira mensagem da Sofia
    # Para duplicatas: apenas registra novo contato, mas continua a conversa existente
    message = build_first_message(normalized)

    msg_sent = False
    if is_new:
        # Lead novo: envia apresentação completa
        msg_sent = send_first_message(phone, message, dry_run=dry_run)
    else:
        # Lead existente: envia mensagem mais curta de follow-up
        name = normalized.get("name")
        nome_str = f", {name.split()[0]}" if name else ""
        listing_title = normalized.get("listing_title")
        if listing_title:
            follow_msg = (
                f"Olá{nome_str}! Vi que você também demonstrou interesse em "
                f"*{listing_title}*. Posso te enviar mais detalhes?"
            )
        else:
            follow_msg = f"Olá{nome_str}! Vi que você entrou em contato novamente. Em que posso te ajudar?"
        msg_sent = send_first_message(phone, follow_msg, dry_run=dry_run)
        log.info("Lead duplicado %s — mensagem de retomada enviada", phone)

    return {
        "status": "ok",
        "phone": phone,
        "is_new": is_new,
        "source": normalized["source"],
        "name": normalized.get("name"),
        "listing": normalized.get("listing_title"),
        "message_sent": msg_sent,
    }


# ─── FastAPI router (para montar no pipeline_runner) ─────────────────────────
def create_portal_router():
    """
    Cria o router FastAPI com o endpoint POST /portal-lead.
    Montado no pipeline_runner.py via app.include_router().
    """
    from fastapi import APIRouter
    from pydantic import BaseModel
    from typing import Any

    router = APIRouter(tags=["Portal Lead Capture"])

    class PortalLeadRequest(BaseModel):
        portal: str                  # "zap" | "vivareal" | "olx"
        payload: dict[str, Any]      # payload bruto do portal
        client_id: str = CLIENT_ID
        dry_run: bool = False

    @router.post("/portal-lead")
    def receive_portal_lead(req: PortalLeadRequest):
        """
        Recebe lead de portal imobiliário (ZAP, VivaReal, OLX).

        O portal deve configurar seu webhook para apontar para este endpoint.
        Sofia enviará a primeira mensagem ao lead em até 2 minutos.

        ICP: Dono da imobiliária — nenhum lead de portal escapa.
        """
        result = handle_portal_lead(
            portal=req.portal,
            payload=req.payload,
            client_id=req.client_id,
            dry_run=req.dry_run,
        )
        return result

    return router


# ─── CLI para testes ──────────────────────────────────────────────────────────
_MOCK_PAYLOADS = {
    "zap": {
        "lead": {
            "name": "Carlos Silva",
            "email": "carlos.silva@email.com",
            "phone": "(11) 99999-0001",
            "message": "Tenho interesse no apartamento. Podem me dar mais informações?",
        },
        "listing": {
            "id": "ZAP-001",
            "title": "Apartamento 120m² Itaim Bibi — 3 suítes",
            "price": 2_500_000,
            "url": "https://www.zapimoveis.com.br/imovel/ZAP-001",
        },
    },
    "vivareal": {
        "consumer": {
            "name": "Ana Lima",
            "email": "ana.lima@email.com",
            "phones": [{"number": "11988880002", "type": "CELLPHONE"}],
        },
        "listing": {
            "externalId": "VR-789",
            "title": "Casa 4 quartos Jardins — piscina e churrasqueira",
            "prices": {"sale": 3_800_000},
            "url": "https://www.vivareal.com.br/imovel/VR-789",
        },
        "message": "Gostaria de agendar uma visita.",
    },
    "olx": {
        "ad_id": "OLX-555",
        "ad_title": "Apartamento 90m² Pinheiros — 2 vagas",
        "ad_price": 1_800_000,
        "ad_url": "https://www.olx.com.br/imovel/OLX-555",
        "interested_user": {
            "name": "João Pereira",
            "email": "joao.p@email.com",
            "phone": "11977770003",
        },
        "message": "Tenho interesse. Pode me ligar?",
    },
}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] portal: %(message)s",
    )

    portais = []
    if "--test-zap" in sys.argv:
        portais = ["zap"]
    elif "--test-vivareal" in sys.argv:
        portais = ["vivareal"]
    elif "--test-olx" in sys.argv:
        portais = ["olx"]
    else:
        portais = ["zap", "vivareal", "olx"]

    dry_run = "--dry-run" not in sys.argv  # por segurança, dry-run é padrão no CLI

    for portal in portais:
        print(f"\n{'='*50}")
        print(f"Testando portal: {portal.upper()}")
        print(f"{'='*50}")
        result = handle_portal_lead(
            portal=portal,
            payload=_MOCK_PAYLOADS[portal],
            dry_run=True,  # sempre dry-run no CLI
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
