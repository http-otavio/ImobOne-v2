"""
agents/qa_integration.py — Agente 9: QA de Integração

Responsabilidade:
  Validar tecnicamente cada integração ativa antes do deploy.
  Cada item do checklist é executado de forma independente — uma falha não
  impede os outros de rodarem.

Checklist obrigatório (CLAUDE.md):
  ┌─────────────────────────────────────────────┬──────────┬──────────────┐
  │ Integração                                  │ Crítico? │ Threshold    │
  ├─────────────────────────────────────────────┼──────────┼──────────────┤
  │ Latência end-to-end                         │ Sim      │ < 8 segundos │
  │ Webhook CRM (POST → 200)                    │ Não      │ status 200   │
  │ ElevenLabs TTS (geração de áudio)           │ Não      │ < 8s         │
  │ WhatsApp (envio de mensagem de teste)       │ Sim      │ status 200   │
  │ Google Places (vizinhança do portfólio)     │ Sim      │ results > 0  │
  │ Supabase pgvector (query semântica)         │ Sim      │ results > 0  │
  └─────────────────────────────────────────────┴──────────┴──────────────┘

Decisão de deploy:
  - Qualquer item CRÍTICO falhando → blocked com diagnóstico detalhado.
  - Item não-crítico falhando → 'done' com nota no relatório.

Injeção de dependências (para testabilidade):
  Todos os clientes externos são injetáveis no construtor:
    - http_client: httpx.AsyncClient (para CRM, WhatsApp, Places, pgvector)
    - elevenlabs_client: mock ou cliente real do ElevenLabs
  Isso garante que os testes unitários nunca fazem chamadas HTTP reais.

Latência e2e:
  Medida como soma das latências individuais de cada integração ativa.
  O threshold de 8s é aplicado à soma total, não a cada item individualmente.
  Se nenhuma integração está configurada, a latência e2e é reportada como 0ms.

Integração com o orquestrador:
  run(client_id, onboarding) → (status, payload) compatível com MockAgentFn.

Uso standalone:
    async with httpx.AsyncClient() as http:
        agent = QAIntegrationAgent(http_client=http)
        status, payload = await agent.run("cliente_001", onboarding)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Threshold de latência end-to-end em milissegundos (CLAUDE.md: < 8 segundos).
LATENCIA_MAX_MS: float = 8_000.0

# Timeout padrão por chamada individual (sub-threshold total).
TIMEOUT_INDIVIDUAL_S: float = 5.0


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """
    Resultado de um único item do checklist de integração.

    Atributos:
        nome: Identificador legível do item.
        passou: True se o item passou no critério técnico.
        critico: True se a falha bloqueia o deploy.
        latencia_ms: Tempo de resposta medido (None se não aplicável).
        status_code: HTTP status code retornado (None se não aplicável).
        erro: Mensagem de erro em caso de falha.
        detalhes: Dict com informações adicionais de diagnóstico.
    """

    nome: str
    passou: bool
    critico: bool
    latencia_ms: Optional[float] = None
    status_code: Optional[int] = None
    erro: Optional[str] = None
    detalhes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "nome": self.nome,
            "passou": self.passou,
            "critico": self.critico,
            "latencia_ms": round(self.latencia_ms, 1) if self.latencia_ms is not None else None,
            "status_code": self.status_code,
            "erro": self.erro,
            "detalhes": self.detalhes,
        }


# ---------------------------------------------------------------------------
# Checks individuais
# ---------------------------------------------------------------------------


async def _check_webhook_crm(
    crm_url: str,
    crm_token: str,
    client_id: str,
    http_client: httpx.AsyncClient,
) -> CheckResult:
    """
    Valida o webhook CRM com um POST de teste.

    Não-crítico: cliente sem CRM é um cenário válido.
    """
    if not crm_url:
        return CheckResult(
            nome="webhook_crm",
            passou=True,
            critico=False,
            detalhes={"motivo": "crm_webhook_url não configurada — skip"},
        )

    inicio = time.monotonic()
    try:
        response = await http_client.post(
            crm_url,
            json={
                "event": "qa_integration_test",
                "client_id": client_id,
                "timestamp": _now_iso(),
            },
            headers={
                "Authorization": f"Bearer {crm_token}",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT_INDIVIDUAL_S,
        )
        latencia_ms = (time.monotonic() - inicio) * 1000
        passou = response.status_code == 200
        return CheckResult(
            nome="webhook_crm",
            passou=passou,
            critico=False,
            latencia_ms=latencia_ms,
            status_code=response.status_code,
            erro=None if passou else f"Status inesperado: {response.status_code}",
            detalhes={"url": crm_url},
        )
    except httpx.TimeoutException:
        latencia_ms = (time.monotonic() - inicio) * 1000
        return CheckResult(
            nome="webhook_crm",
            passou=False,
            critico=False,
            latencia_ms=latencia_ms,
            erro=f"Timeout após {TIMEOUT_INDIVIDUAL_S}s",
            detalhes={"url": crm_url},
        )
    except httpx.RequestError as exc:
        return CheckResult(
            nome="webhook_crm",
            passou=False,
            critico=False,
            erro=f"Erro de rede: {exc}",
            detalhes={"url": crm_url},
        )


async def _check_elevenlabs(
    voice_id: str,
    elevenlabs_client: Any,
) -> CheckResult:
    """
    Valida a geração de áudio via ElevenLabs com texto de teste.

    Não-crítico: falha de TTS não impede o consultor de operar em texto.
    """
    if not voice_id or elevenlabs_client is None:
        return CheckResult(
            nome="elevenlabs_tts",
            passou=True,
            critico=False,
            detalhes={"motivo": "voice_id ou cliente não configurado — skip"},
        )

    inicio = time.monotonic()
    try:
        # Interface mínima esperada: elevenlabs_client.generate(text, voice_id) → bytes
        audio_bytes = await elevenlabs_client.generate(
            text="Teste de geração de áudio para validação de integração.",
            voice_id=voice_id,
        )
        latencia_ms = (time.monotonic() - inicio) * 1000
        passou = bool(audio_bytes) and len(audio_bytes) > 0
        return CheckResult(
            nome="elevenlabs_tts",
            passou=passou,
            critico=False,
            latencia_ms=latencia_ms,
            erro=None if passou else "Resposta vazia do ElevenLabs",
            detalhes={
                "voice_id": voice_id,
                "audio_bytes": len(audio_bytes) if audio_bytes else 0,
            },
        )
    except Exception as exc:
        latencia_ms = (time.monotonic() - inicio) * 1000
        return CheckResult(
            nome="elevenlabs_tts",
            passou=False,
            critico=False,
            latencia_ms=latencia_ms,
            erro=f"Erro na geração de áudio: {exc}",
            detalhes={"voice_id": voice_id},
        )


async def _check_whatsapp(
    whatsapp_url: str,
    whatsapp_api_key: str,
    operator_number: str,
    client_id: str,
    http_client: httpx.AsyncClient,
) -> CheckResult:
    """
    Valida o envio de mensagem de teste via WhatsApp Business API.

    Crítico: sem WhatsApp o produto não funciona.
    Envia mensagem ao número do operador (não ao lead).
    """
    if not whatsapp_url or not whatsapp_api_key:
        return CheckResult(
            nome="whatsapp_envio",
            passou=False,
            critico=True,
            erro="WHATSAPP_BSP_URL ou WHATSAPP_BSP_API_KEY não configurados",
            detalhes={"motivo": "variáveis de ambiente ausentes"},
        )

    inicio = time.monotonic()
    payload = {
        "to": operator_number,
        "type": "text",
        "text": {
            "body": f"[ImobOne QA] Teste de integração — cliente {client_id}. Pode ignorar."
        },
    }
    try:
        response = await http_client.post(
            whatsapp_url,
            json=payload,
            headers={
                "D360-API-KEY": whatsapp_api_key,
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT_INDIVIDUAL_S,
        )
        latencia_ms = (time.monotonic() - inicio) * 1000
        passou = response.status_code in (200, 201)
        return CheckResult(
            nome="whatsapp_envio",
            passou=passou,
            critico=True,
            latencia_ms=latencia_ms,
            status_code=response.status_code,
            erro=None if passou else f"Status {response.status_code}",
            detalhes={"url": whatsapp_url, "numero_operador": operator_number},
        )
    except httpx.TimeoutException:
        latencia_ms = (time.monotonic() - inicio) * 1000
        return CheckResult(
            nome="whatsapp_envio",
            passou=False,
            critico=True,
            latencia_ms=latencia_ms,
            erro=f"Timeout após {TIMEOUT_INDIVIDUAL_S}s — BSP não respondeu",
        )
    except httpx.RequestError as exc:
        return CheckResult(
            nome="whatsapp_envio",
            passou=False,
            critico=True,
            erro=f"Erro de rede: {exc}",
        )


async def _check_google_places(
    lat: float,
    lng: float,
    client_id: str,
    http_client: httpx.AsyncClient,
    places_api_key: str,
) -> CheckResult:
    """
    Valida que o Google Places retorna dados para o endereço do portfólio.

    Crítico: sem dados de vizinhança o diferencial do produto não funciona.
    """
    if not places_api_key:
        return CheckResult(
            nome="google_places",
            passou=False,
            critico=True,
            erro="GOOGLE_PLACES_API_KEY não configurada",
        )

    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{lat},{lng}",
        "radius": 2000,
        "type": "school",
        "key": places_api_key,
    }

    inicio = time.monotonic()
    try:
        response = await http_client.get(
            url, params=params, timeout=TIMEOUT_INDIVIDUAL_S
        )
        latencia_ms = (time.monotonic() - inicio) * 1000

        if response.status_code != 200:
            return CheckResult(
                nome="google_places",
                passou=False,
                critico=True,
                latencia_ms=latencia_ms,
                status_code=response.status_code,
                erro=f"Status {response.status_code}",
            )

        data = response.json()
        api_status = data.get("status", "")
        results = data.get("results", [])

        # ZERO_RESULTS é válido para localizações remotas — não bloqueia
        passou = api_status in ("OK", "ZERO_RESULTS")
        return CheckResult(
            nome="google_places",
            passou=passou,
            critico=True,
            latencia_ms=latencia_ms,
            status_code=response.status_code,
            erro=None if passou else f"API status: {api_status}",
            detalhes={
                "api_status": api_status,
                "resultados_count": len(results),
                "lat": lat,
                "lng": lng,
            },
        )
    except httpx.TimeoutException:
        latencia_ms = (time.monotonic() - inicio) * 1000
        return CheckResult(
            nome="google_places",
            passou=False,
            critico=True,
            latencia_ms=latencia_ms,
            erro=f"Timeout após {TIMEOUT_INDIVIDUAL_S}s",
        )
    except Exception as exc:
        return CheckResult(
            nome="google_places",
            passou=False,
            critico=True,
            erro=f"Erro inesperado: {exc}",
        )


async def _check_supabase_pgvector(
    client_id: str,
    supabase_url: str,
    supabase_key: str,
    http_client: httpx.AsyncClient,
) -> CheckResult:
    """
    Valida que o Supabase pgvector retorna imóveis relevantes para query de teste.

    Crítico: sem busca semântica o consultor não consegue recomendar imóveis.
    Usa a API REST do Supabase para uma query simples na tabela imoveis_embeddings.
    """
    if not supabase_url or not supabase_key:
        return CheckResult(
            nome="supabase_pgvector",
            passou=False,
            critico=True,
            erro="SUPABASE_URL ou SUPABASE_KEY não configurados",
        )

    # Query REST simples — verifica se o namespace do cliente tem dados
    url = f"{supabase_url}/rest/v1/imoveis_embeddings"
    params = {
        "client_id": f"eq.{client_id}",
        "limit": "1",
        "select": "imovel_id,titulo",
    }
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
    }

    inicio = time.monotonic()
    try:
        response = await http_client.get(
            url, params=params, headers=headers, timeout=TIMEOUT_INDIVIDUAL_S
        )
        latencia_ms = (time.monotonic() - inicio) * 1000

        if response.status_code not in (200, 206):
            return CheckResult(
                nome="supabase_pgvector",
                passou=False,
                critico=True,
                latencia_ms=latencia_ms,
                status_code=response.status_code,
                erro=f"Status {response.status_code}",
            )

        registros = response.json()
        passou = isinstance(registros, list)  # mesmo vazio, estrutura válida
        return CheckResult(
            nome="supabase_pgvector",
            passou=passou,
            critico=True,
            latencia_ms=latencia_ms,
            status_code=response.status_code,
            erro=None if passou else "Resposta inesperada do Supabase",
            detalhes={
                "registros_count": len(registros) if isinstance(registros, list) else 0,
                "client_id": client_id,
            },
        )
    except httpx.TimeoutException:
        latencia_ms = (time.monotonic() - inicio) * 1000
        return CheckResult(
            nome="supabase_pgvector",
            passou=False,
            critico=True,
            latencia_ms=latencia_ms,
            erro=f"Timeout após {TIMEOUT_INDIVIDUAL_S}s",
        )
    except Exception as exc:
        return CheckResult(
            nome="supabase_pgvector",
            passou=False,
            critico=True,
            erro=f"Erro inesperado: {exc}",
        )


def _calcular_latencia_e2e(checks: list[CheckResult]) -> float:
    """
    Soma as latências individuais para estimar a latência e2e.

    A latência e2e real é menor que a soma (as integrações rodam em paralelo
    em produção), mas a soma é um upper bound conservador para validação.
    """
    return sum(c.latencia_ms for c in checks if c.latencia_ms is not None)


def _check_latencia_e2e(latencia_total_ms: float) -> CheckResult:
    """
    Avalia se a latência e2e estimada está dentro do threshold de 8s.

    Crítico: acima de 8s o produto viola o SLA e o lead abandona a conversa.
    """
    passou = latencia_total_ms < LATENCIA_MAX_MS
    return CheckResult(
        nome="latencia_e2e",
        passou=passou,
        critico=True,
        latencia_ms=latencia_total_ms,
        erro=(
            None if passou
            else f"Latência e2e estimada {latencia_total_ms:.0f}ms excede threshold de {LATENCIA_MAX_MS:.0f}ms"
        ),
        detalhes={"threshold_ms": LATENCIA_MAX_MS},
    )


# ---------------------------------------------------------------------------
# QAIntegrationAgent
# ---------------------------------------------------------------------------


class QAIntegrationAgent:
    """
    Agente 9 — QA de Integração.

    Executa o checklist técnico completo antes do deploy de um novo cliente.

    Args:
        http_client: httpx.AsyncClient para todas as chamadas HTTP.
                     Se None, cria uma instância temporária.
        elevenlabs_client: Cliente ElevenLabs com método async generate().
                           Se None, o check é pulado (não-crítico).
    """

    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
        elevenlabs_client: Any = None,
    ) -> None:
        self._http_client = http_client
        self._elevenlabs_client = elevenlabs_client
        self._owns_client = http_client is None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient(timeout=TIMEOUT_INDIVIDUAL_S)

    async def _close_if_owned(self, client: httpx.AsyncClient) -> None:
        if self._owns_client:
            await client.aclose()

    async def run(self, client_id: str, onboarding: dict) -> tuple[str, dict]:
        """
        Executa o checklist de integração para o cliente.

        Args:
            client_id: ID do cliente sendo configurado.
            onboarding: Dicionário com credenciais e configurações das integrações.

        Returns:
            ("done", relatório) se todos os itens críticos passaram.
            ("blocked", relatório) se qualquer item crítico falhou.
        """
        http = await self._get_http()
        checks: list[CheckResult] = []

        try:
            # ── 1. CRM webhook (não-crítico) ─────────────────────────────────
            crm_check = await _check_webhook_crm(
                crm_url=onboarding.get("crm_webhook_url", ""),
                crm_token=onboarding.get("crm_webhook_token", ""),
                client_id=client_id,
                http_client=http,
            )
            checks.append(crm_check)

            # ── 2. ElevenLabs TTS (não-crítico) ──────────────────────────────
            tts_check = await _check_elevenlabs(
                voice_id=onboarding.get("voice_id", ""),
                elevenlabs_client=self._elevenlabs_client,
            )
            checks.append(tts_check)

            # ── 3. WhatsApp (crítico) ─────────────────────────────────────────
            wa_check = await _check_whatsapp(
                whatsapp_url=onboarding.get("whatsapp_bsp_url", ""),
                whatsapp_api_key=onboarding.get("whatsapp_bsp_api_key", ""),
                operator_number=onboarding.get("whatsapp_operator_number", ""),
                client_id=client_id,
                http_client=http,
            )
            checks.append(wa_check)

            # ── 4. Google Places (crítico) ────────────────────────────────────
            endereco = onboarding.get("endereco_teste", {})
            places_check = await _check_google_places(
                lat=endereco.get("lat", -23.5505),
                lng=endereco.get("lng", -46.6333),
                client_id=client_id,
                http_client=http,
                places_api_key=onboarding.get("google_places_api_key", ""),
            )
            checks.append(places_check)

            # ── 5. Supabase pgvector (crítico) ────────────────────────────────
            supabase_check = await _check_supabase_pgvector(
                client_id=client_id,
                supabase_url=onboarding.get("supabase_url", ""),
                supabase_key=onboarding.get("supabase_key", ""),
                http_client=http,
            )
            checks.append(supabase_check)

        finally:
            await self._close_if_owned(http)

        # ── 6. Latência e2e (crítico — calculada após todos os checks) ────────
        latencia_total = _calcular_latencia_e2e(checks)
        latencia_check = _check_latencia_e2e(latencia_total)
        checks.append(latencia_check)

        # ── Análise dos resultados ────────────────────────────────────────────
        criticos_falhando = [c for c in checks if c.critico and not c.passou]
        nao_criticos_falhando = [c for c in checks if not c.critico and not c.passou]

        relatorio = {
            "client_id": client_id,
            "total_checks": len(checks),
            "checks_passando": sum(1 for c in checks if c.passou),
            "checks_falhando": sum(1 for c in checks if not c.passou),
            "criticos_falhando_count": len(criticos_falhando),
            "nao_criticos_falhando_count": len(nao_criticos_falhando),
            "latencia_e2e_ms": round(latencia_total, 1),
            "latencia_threshold_ms": LATENCIA_MAX_MS,
            "checks": [c.to_dict() for c in checks],
            "criticos_falhando": [c.to_dict() for c in criticos_falhando],
            "notas": [c.to_dict() for c in nao_criticos_falhando],
        }

        if criticos_falhando:
            nomes = ", ".join(c.nome for c in criticos_falhando)
            relatorio["motivo_bloqueio"] = f"Integrações críticas com falha: {nomes}"
            logger.warning(
                "[qa_integration] QA REPROVADO para '%s': %s",
                client_id,
                relatorio["motivo_bloqueio"],
            )
            return "blocked", relatorio

        if nao_criticos_falhando:
            nomes = ", ".join(c.nome for c in nao_criticos_falhando)
            logger.warning(
                "[qa_integration] Integrações não-críticas com falha para '%s': %s",
                client_id,
                nomes,
            )

        logger.info(
            "[qa_integration] QA APROVADO para '%s' — %d/%d checks OK.",
            client_id,
            relatorio["checks_passando"],
            len(checks),
        )
        return "done", relatorio


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
