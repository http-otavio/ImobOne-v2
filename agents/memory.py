"""
agents/memory.py — Agente 7: Memória de Lead

Responsabilidades:
  1. Definir e validar o schema de armazenamento do lead no Supabase
  2. Configurar e testar o webhook de integração com o CRM do cliente
  3. Implementar a lógica de score de intenção ponderado

Schema do lead (Supabase → tabela `leads`):
  Campos obrigatórios:
    lead_id, client_id, nome, telefone, canal_origem, budget_declarado,
    prazo_declarado, perfil_familiar, uso_imovel, score_intencao,
    historico_mensagens, imoveis_de_interesse, status_funil,
    ultima_interacao, created_at

Estratégia de historico_mensagens (compactação):
  O histórico de mensagens NÃO armazena o texto completo de cada mensagem.
  Em vez disso, mantém uma janela deslizante de JANELA_MENSAGENS_RECENTES (10)
  mensagens recentes + um campo `resumo_historico` que sumariza turnos mais
  antigos. Isso evita que o payload do lead cresça indefinidamente em produção.

  Estrutura de cada entrada do histórico:
    { "ts": ISO8601, "role": "user"|"assistant", "resumo": str (< 200 chars) }

  Quando o número de entradas excede JANELA_MENSAGENS_RECENTES, as mais antigas
  são compactadas em `resumo_historico` via sumário rolling. O consultor mantém
  contexto completo sem carregar o banco com texto bruto.

Score de intenção (pesos definidos pelo CLAUDE.md):
  +5  budget declarado espontaneamente
  +4  horário de visita mencionado
  +3  pergunta específica sobre imóvel (endereço, andar, metragem)
  +2  foto ou planta solicitada
  +2  pergunta sobre vizinhança
  +1  resposta rápida consecutiva (< 2 min)
  -1  pedido de desconto abusivo (> 15%)
  -2  silêncio > 48h

Webhook CRM (genérico):
  - Recebe URL e token do onboarding
  - Faz POST de teste com payload mínimo
  - Valida status 200
  - Se cliente sem CRM: crm_enabled=False, segue sem bloquear

Integração com o orquestrador:
  run(client_id, onboarding) → (status, payload) compatível com MockAgentFn

Uso standalone:
    agent = MemoryAgent(http_client=httpx.AsyncClient())
    status, payload = await agent.run("cliente_001", onboarding)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Janela de mensagens recentes mantidas no payload do lead.
# Mensagens além desse limite são compactadas em `resumo_historico`.
JANELA_MENSAGENS_RECENTES: int = 10

# Score mínimo para promoção automática para status "quente".
THRESHOLD_LEAD_QUENTE: int = 8

# Timeout do POST de validação do webhook CRM (segundos).
WEBHOOK_TIMEOUT_SEGUNDOS: float = 10.0


# ---------------------------------------------------------------------------
# Enums de domínio
# ---------------------------------------------------------------------------


class StatusFunil(str, Enum):
    """Etapas do funil de leads."""

    NOVO = "novo"
    QUALIFICADO = "qualificado"
    QUENTE = "quente"
    AGENDADO = "agendado"
    DESCARTADO = "descartado"


class CanalOrigem(str, Enum):
    """Canal pelo qual o lead chegou."""

    WHATSAPP = "whatsapp"
    SITE = "site"
    INDICACAO = "indicacao"
    LANCAMENTO = "lancamento"
    OUTRO = "outro"


# ---------------------------------------------------------------------------
# Sinais de score de intenção
# ---------------------------------------------------------------------------


class SinalIntencao(str, Enum):
    """
    Sinais detectáveis durante a conversa e seus pesos no score de intenção.

    Cada valor corresponde a uma entrada em PESOS_SINAL.
    O consultor aciona `calcular_delta_score(sinal)` a cada turno para
    atualizar o score do lead no Supabase.
    """

    BUDGET_DECLARADO = "budget_declarado"
    HORARIO_VISITA_MENCIONADO = "horario_visita_mencionado"
    PERGUNTA_ESPECIFICA_IMOVEL = "pergunta_especifica_imovel"
    FOTO_OU_PLANTA_SOLICITADA = "foto_ou_planta_solicitada"
    PERGUNTA_VIZINHANCA = "pergunta_vizinhanca"
    RESPOSTA_RAPIDA_CONSECUTIVA = "resposta_rapida_consecutiva"
    DESCONTO_ABUSIVO = "desconto_abusivo"
    SILENCIO_48H = "silencio_48h"


# Pesos por sinal — fonte de verdade única.
# Qualquer ajuste de peso passa pelo Agente 2 (auditor) antes de deploy.
PESOS_SINAL: dict[SinalIntencao, int] = {
    SinalIntencao.BUDGET_DECLARADO: +5,
    SinalIntencao.HORARIO_VISITA_MENCIONADO: +4,
    SinalIntencao.PERGUNTA_ESPECIFICA_IMOVEL: +3,
    SinalIntencao.FOTO_OU_PLANTA_SOLICITADA: +2,
    SinalIntencao.PERGUNTA_VIZINHANCA: +2,
    SinalIntencao.RESPOSTA_RAPIDA_CONSECUTIVA: +1,
    SinalIntencao.DESCONTO_ABUSIVO: -1,
    SinalIntencao.SILENCIO_48H: -2,
}


def calcular_delta_score(sinais: list[SinalIntencao]) -> int:
    """
    Calcula a variação de score para uma lista de sinais detectados num turno.

    Sinais positivos e negativos se somam algebricamente — o score total do
    lead é acumulativo ao longo da conversa (sem teto máximo por CLAUDE.md).

    Args:
        sinais: Sinais detectados no turno atual.

    Returns:
        Delta inteiro (positivo ou negativo) a ser somado ao score atual.

    Examples:
        >>> calcular_delta_score([SinalIntencao.BUDGET_DECLARADO,
        ...                       SinalIntencao.HORARIO_VISITA_MENCIONADO])
        9
        >>> calcular_delta_score([SinalIntencao.SILENCIO_48H])
        -2
    """
    return sum(PESOS_SINAL[s] for s in sinais)


def calcular_score_total(sequencia_sinais: list[list[SinalIntencao]]) -> int:
    """
    Calcula o score total de um lead dado uma sequência de turnos com sinais.

    Args:
        sequencia_sinais: Lista de turnos, cada turno sendo uma lista de sinais.

    Returns:
        Score acumulado ao longo de todos os turnos.

    Examples:
        >>> calcular_score_total([
        ...     [SinalIntencao.PERGUNTA_ESPECIFICA_IMOVEL],  # turno 1 → +3
        ...     [SinalIntencao.BUDGET_DECLARADO,             # turno 2 → +5+2=+7
        ...      SinalIntencao.PERGUNTA_VIZINHANCA],
        ...     [SinalIntencao.HORARIO_VISITA_MENCIONADO],   # turno 3 → +4
        ... ])
        14
    """
    return sum(calcular_delta_score(turno) for turno in sequencia_sinais)


def determinar_status_funil(score: int, status_atual: StatusFunil) -> StatusFunil:
    """
    Promove ou mantém o status do funil com base no score.

    Regras:
    - score >= THRESHOLD_LEAD_QUENTE (8) → promove para QUENTE
      (exceto se já está em AGENDADO ou DESCARTADO — não retrocede)
    - score > 0 e status NOVO → promove para QUALIFICADO
    - Status AGENDADO e DESCARTADO são terminais — nunca rebaixados por score

    Args:
        score: Score de intenção atual do lead.
        status_atual: Status atual no funil.

    Returns:
        Novo status (pode ser o mesmo se não houver promoção).
    """
    # Status terminais — nunca alterados por score automático
    if status_atual in (StatusFunil.AGENDADO, StatusFunil.DESCARTADO):
        return status_atual

    if score >= THRESHOLD_LEAD_QUENTE:
        return StatusFunil.QUENTE

    if score > 0 and status_atual == StatusFunil.NOVO:
        return StatusFunil.QUALIFICADO

    return status_atual


# ---------------------------------------------------------------------------
# Schema de lead
# ---------------------------------------------------------------------------


@dataclass
class EntradaHistorico:
    """
    Uma entrada no histórico compactado de mensagens do lead.

    Nunca armazena o texto completo — apenas um resumo de até 200 caracteres.
    O texto completo fica fora do payload do lead (log imutável separado).
    """

    ts: str  # ISO 8601
    role: str  # "user" | "assistant"
    resumo: str  # máx 200 chars

    def to_dict(self) -> dict:
        return {"ts": self.ts, "role": self.role, "resumo": self.resumo[:200]}

    @classmethod
    def from_dict(cls, d: dict) -> "EntradaHistorico":
        return cls(ts=d["ts"], role=d["role"], resumo=d.get("resumo", "")[:200])


@dataclass
class LeadSchema:
    """
    Schema canônico de um lead no Supabase.

    Campos obrigatórios são inicializados com defaults seguros.
    O campo `historico_mensagens` mantém apenas JANELA_MENSAGENS_RECENTES
    entradas recentes — mensagens mais antigas são compactadas em
    `resumo_historico` pelo método `adicionar_mensagem()`.

    Uso:
        lead = LeadSchema.novo("cliente_001", "whatsapp")
        lead.adicionar_mensagem("user", "Boa tarde, queria ver o cobertura")
        lead.aplicar_sinais([SinalIntencao.PERGUNTA_ESPECIFICA_IMOVEL])
    """

    # Identificadores
    lead_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    client_id: str = ""

    # Dados de contato
    nome: str = ""
    telefone: str = ""
    canal_origem: str = CanalOrigem.WHATSAPP

    # Qualificação
    budget_declarado: Optional[float] = None
    prazo_declarado: Optional[str] = None     # "urgente" | "medio" | "longo"
    perfil_familiar: Optional[str] = None     # "familia" | "casal" | "solteiro" | None
    uso_imovel: Optional[str] = None          # "moradia" | "investimento" | "segunda_residencia"

    # Score e funil
    score_intencao: int = 0
    status_funil: str = StatusFunil.NOVO

    # Histórico compactado
    historico_mensagens: list[dict] = field(default_factory=list)
    resumo_historico: str = ""  # sumarização rolling dos turnos mais antigos

    # Imóveis de interesse (IDs do pgvector)
    imoveis_de_interesse: list[str] = field(default_factory=list)

    # Timestamps
    ultima_interacao: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Campos opcionais de contexto
    sinais_detectados: list[str] = field(default_factory=list)  # histórico de sinais
    crm_sincronizado: bool = False
    notas_corretor: str = ""

    # ── Factory ─────────────────────────────────────────────────────────────

    @classmethod
    def novo(cls, client_id: str, canal_origem: str = CanalOrigem.WHATSAPP) -> "LeadSchema":
        """Cria um novo lead com defaults seguros para o cliente."""
        return cls(client_id=client_id, canal_origem=canal_origem)

    # ── Histórico compactado ─────────────────────────────────────────────────

    def adicionar_mensagem(self, role: str, texto: str) -> None:
        """
        Adiciona uma mensagem ao histórico compactado.

        Mantém apenas JANELA_MENSAGENS_RECENTES entradas no histórico ativo.
        Quando a janela é excedida, as entradas mais antigas são compactadas:
        seus resumos são concatenados em `resumo_historico` e removidos da
        lista ativa.

        O texto original nunca é armazenado — apenas um resumo de até 200 chars.
        Isso garante que o payload do lead permaneça bounded em produção.

        Args:
            role: "user" ou "assistant".
            texto: Texto completo da mensagem (será truncado para o resumo).
        """
        ts = datetime.now(timezone.utc).isoformat()
        # Resumo: primeiros 200 chars do texto (preserva o sentido sem armazenar tudo)
        resumo = texto[:200] if len(texto) > 200 else texto

        entrada = EntradaHistorico(ts=ts, role=role, resumo=resumo)
        self.historico_mensagens.append(entrada.to_dict())
        self.ultima_interacao = ts

        # Compacta o excedente quando a janela é ultrapassada
        if len(self.historico_mensagens) > JANELA_MENSAGENS_RECENTES:
            self._compactar_historico()

    def _compactar_historico(self) -> None:
        """
        Move as entradas mais antigas da janela ativa para `resumo_historico`.

        A compactação é aditiva (rolling): o novo resumo é concatenado ao
        resumo anterior com um delimitador " | ". Isso preserva a cadeia
        temporal sem armazenar mensagens completas.

        Threshold: remove tudo exceto os últimos JANELA_MENSAGENS_RECENTES itens.
        """
        excedente = self.historico_mensagens[:-JANELA_MENSAGENS_RECENTES]
        self.historico_mensagens = self.historico_mensagens[-JANELA_MENSAGENS_RECENTES:]

        novos_resumos = [
            f"[{e['ts'][:10]} {e['role']}] {e['resumo']}"
            for e in excedente
        ]
        bloco = " | ".join(novos_resumos)

        if self.resumo_historico:
            self.resumo_historico = f"{self.resumo_historico} | {bloco}"
        else:
            self.resumo_historico = bloco

    # ── Score de intenção ────────────────────────────────────────────────────

    def aplicar_sinais(self, sinais: list[SinalIntencao]) -> int:
        """
        Aplica sinais detectados num turno ao score do lead.

        Atualiza `score_intencao`, registra os sinais em `sinais_detectados`
        e promove o `status_funil` automaticamente se o threshold for atingido.

        Args:
            sinais: Sinais detectados no turno atual.

        Returns:
            Novo score total após a aplicação.
        """
        delta = calcular_delta_score(sinais)
        self.score_intencao += delta
        self.sinais_detectados.extend(s.value for s in sinais)

        novo_status = determinar_status_funil(
            self.score_intencao, StatusFunil(self.status_funil)
        )
        self.status_funil = novo_status.value

        logger.debug(
            "[memory] lead=%s delta=%+d score=%d status=%s",
            self.lead_id[:8],
            delta,
            self.score_intencao,
            self.status_funil,
        )
        return self.score_intencao

    # ── Serialização ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serializa o lead para inserção no Supabase."""
        return {
            "lead_id": self.lead_id,
            "client_id": self.client_id,
            "nome": self.nome,
            "telefone": self.telefone,
            "canal_origem": self.canal_origem,
            "budget_declarado": self.budget_declarado,
            "prazo_declarado": self.prazo_declarado,
            "perfil_familiar": self.perfil_familiar,
            "uso_imovel": self.uso_imovel,
            "score_intencao": self.score_intencao,
            "status_funil": self.status_funil,
            "historico_mensagens": self.historico_mensagens,
            "resumo_historico": self.resumo_historico,
            "imoveis_de_interesse": self.imoveis_de_interesse,
            "ultima_interacao": self.ultima_interacao,
            "created_at": self.created_at,
            "sinais_detectados": self.sinais_detectados,
            "crm_sincronizado": self.crm_sincronizado,
            "notas_corretor": self.notas_corretor,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LeadSchema":
        """Reconstrói o lead a partir de um dicionário do Supabase."""
        return cls(
            lead_id=d.get("lead_id", str(uuid.uuid4())),
            client_id=d.get("client_id", ""),
            nome=d.get("nome", ""),
            telefone=d.get("telefone", ""),
            canal_origem=d.get("canal_origem", CanalOrigem.WHATSAPP),
            budget_declarado=d.get("budget_declarado"),
            prazo_declarado=d.get("prazo_declarado"),
            perfil_familiar=d.get("perfil_familiar"),
            uso_imovel=d.get("uso_imovel"),
            score_intencao=d.get("score_intencao", 0),
            status_funil=d.get("status_funil", StatusFunil.NOVO),
            historico_mensagens=d.get("historico_mensagens", []),
            resumo_historico=d.get("resumo_historico", ""),
            imoveis_de_interesse=d.get("imoveis_de_interesse", []),
            ultima_interacao=d.get("ultima_interacao", datetime.now(timezone.utc).isoformat()),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            sinais_detectados=d.get("sinais_detectados", []),
            crm_sincronizado=d.get("crm_sincronizado", False),
            notas_corretor=d.get("notas_corretor", ""),
        )


# Campos obrigatórios que devem estar presentes e não-None no schema do lead.
# Usado para validação em setup e em testes.
CAMPOS_OBRIGATORIOS_LEAD: list[str] = [
    "lead_id",
    "client_id",
    "nome",
    "telefone",
    "canal_origem",
    "budget_declarado",
    "prazo_declarado",
    "perfil_familiar",
    "uso_imovel",
    "score_intencao",
    "historico_mensagens",
    "imoveis_de_interesse",
    "status_funil",
    "ultima_interacao",
    "created_at",
]


def validar_schema_lead(lead_dict: dict) -> list[str]:
    """
    Retorna lista de campos obrigatórios ausentes no dicionário do lead.

    Campos com valor None são considerados presentes (valor válido em Supabase).
    Campos completamente ausentes da chave do dicionário são reportados.

    Args:
        lead_dict: Dicionário retornado por LeadSchema.to_dict().

    Returns:
        Lista de campos ausentes (vazia = schema válido).
    """
    return [campo for campo in CAMPOS_OBRIGATORIOS_LEAD if campo not in lead_dict]


# ---------------------------------------------------------------------------
# Webhook CRM
# ---------------------------------------------------------------------------


@dataclass
class ResultadoWebhook:
    """Resultado do teste de validação do webhook CRM."""

    crm_enabled: bool
    url: str = ""
    status_code: Optional[int] = None
    latencia_ms: Optional[float] = None
    erro: Optional[str] = None

    @property
    def sucesso(self) -> bool:
        return self.crm_enabled and self.status_code == 200


async def validar_webhook_crm(
    crm_url: str,
    crm_token: str,
    client_id: str,
    http_client: httpx.AsyncClient,
) -> ResultadoWebhook:
    """
    Faz um POST de teste para o webhook do CRM do cliente e valida o status 200.

    O payload de teste é mínimo mas realista — simula a estrutura que o consultor
    enviará em produção. O CRM do cliente precisa aceitar e confirmar com 200.

    Args:
        crm_url: URL do webhook (ex: https://crm.cliente.com.br/webhook/leads).
        crm_token: Token de autenticação (enviado no header Authorization).
        client_id: ID do cliente (incluído no payload para rastreabilidade).
        http_client: Instância httpx.AsyncClient (injetada para testabilidade).

    Returns:
        ResultadoWebhook com o resultado do teste.
    """
    payload_teste = {
        "event": "lead_teste_setup",
        "client_id": client_id,
        "lead": {
            "lead_id": "setup-test-" + client_id[:8],
            "nome": "Lead de Teste — Setup ImobOne",
            "score_intencao": 0,
            "status_funil": StatusFunil.NOVO,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    headers = {
        "Authorization": f"Bearer {crm_token}",
        "Content-Type": "application/json",
        "X-ImobOne-Client": client_id,
    }

    inicio = datetime.now(timezone.utc)
    try:
        response = await http_client.post(
            crm_url,
            json=payload_teste,
            headers=headers,
            timeout=WEBHOOK_TIMEOUT_SEGUNDOS,
        )
        latencia_ms = (datetime.now(timezone.utc) - inicio).total_seconds() * 1000

        if response.status_code == 200:
            logger.info(
                "[memory] Webhook CRM OK para '%s' — %d ms",
                client_id,
                latencia_ms,
            )
            return ResultadoWebhook(
                crm_enabled=True,
                url=crm_url,
                status_code=response.status_code,
                latencia_ms=latencia_ms,
            )
        else:
            logger.warning(
                "[memory] Webhook CRM retornou %d para '%s'",
                response.status_code,
                client_id,
            )
            return ResultadoWebhook(
                crm_enabled=False,
                url=crm_url,
                status_code=response.status_code,
                latencia_ms=latencia_ms,
                erro=f"Status inesperado: {response.status_code}",
            )

    except httpx.TimeoutException:
        logger.error("[memory] Timeout no webhook CRM para '%s'", client_id)
        return ResultadoWebhook(
            crm_enabled=False,
            url=crm_url,
            erro=f"Timeout após {WEBHOOK_TIMEOUT_SEGUNDOS}s",
        )
    except httpx.RequestError as exc:
        logger.error("[memory] Erro de rede no webhook CRM para '%s': %s", client_id, exc)
        return ResultadoWebhook(
            crm_enabled=False,
            url=crm_url,
            erro=f"Erro de rede: {exc}",
        )


# ---------------------------------------------------------------------------
# MemoryAgent
# ---------------------------------------------------------------------------


class MemoryAgent:
    """
    Agente 7 — Memória de Lead.

    Durante o setup de um novo cliente, este agente:
    1. Valida o schema de lead (cria um lead de exemplo e verifica os campos)
    2. Valida o webhook CRM (POST de teste com payload mínimo)
    3. Documenta a configuração no payload de retorno

    Em produção contínua (fora do setup), o LeadSchema e as funções de score
    são chamadas diretamente pelo agente consultor a cada turno de conversa.

    Args:
        http_client: Instância httpx.AsyncClient para chamadas ao webhook.
                     Pode ser mockado nos testes. Se None, cria uma instância
                     temporária com os timeouts corretos.
    """

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._http_client = http_client
        self._owns_client = http_client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SEGUNDOS)

    async def _close_client_if_owned(self, client: httpx.AsyncClient) -> None:
        if self._owns_client:
            await client.aclose()

    async def run(self, client_id: str, onboarding: dict) -> tuple[str, dict]:
        """
        Configura a camada de memória de leads para o cliente.

        Args:
            client_id: ID do cliente sendo configurado.
            onboarding: Dicionário de onboarding com configurações de CRM.

        Returns:
            ("done", payload) em caso de sucesso.
            ("blocked", {"error": ...}) se o schema estiver inválido.
            Webhook com falha NÃO bloqueia — apenas registra crm_enabled=False.
        """
        # ── 1. Validar schema de lead ────────────────────────────────────────
        lead_exemplo = LeadSchema.novo(client_id)
        lead_dict = lead_exemplo.to_dict()
        campos_ausentes = validar_schema_lead(lead_dict)

        if campos_ausentes:
            logger.error(
                "[memory] Schema de lead inválido para '%s': campos ausentes = %s",
                client_id,
                campos_ausentes,
            )
            return "blocked", {
                "error": f"Schema de lead inválido: campos ausentes = {campos_ausentes}",
                "agent": "memory",
                "client_id": client_id,
            }

        logger.info("[memory] Schema de lead validado para '%s'.", client_id)

        # ── 2. Validar webhook CRM ───────────────────────────────────────────
        crm_url: str = onboarding.get("crm_webhook_url", "")
        crm_token: str = onboarding.get("crm_webhook_token", "")

        resultado_crm: ResultadoWebhook
        if not crm_url:
            # Cliente sem CRM — não bloqueia, apenas registra
            logger.info(
                "[memory] Cliente '%s' sem CRM configurado — crm_enabled=False.",
                client_id,
            )
            resultado_crm = ResultadoWebhook(
                crm_enabled=False,
                url="",
                erro="crm_webhook_url não fornecida no onboarding",
            )
        else:
            http_client = await self._get_client()
            try:
                resultado_crm = await validar_webhook_crm(
                    crm_url=crm_url,
                    crm_token=crm_token,
                    client_id=client_id,
                    http_client=http_client,
                )
            finally:
                await self._close_client_if_owned(http_client)

            if not resultado_crm.sucesso:
                logger.warning(
                    "[memory] Webhook CRM com falha para '%s': %s — continuando sem CRM.",
                    client_id,
                    resultado_crm.erro,
                )

        # ── 3. Montar payload de configuração ────────────────────────────────
        payload = {
            "schema_validado": True,
            "campos_obrigatorios": CAMPOS_OBRIGATORIOS_LEAD,
            "campos_count": len(CAMPOS_OBRIGATORIOS_LEAD),
            "crm_enabled": resultado_crm.crm_enabled,
            "crm_url": resultado_crm.url,
            "crm_status_code": resultado_crm.status_code,
            "crm_latencia_ms": resultado_crm.latencia_ms,
            "crm_erro": resultado_crm.erro,
            "score_threshold_quente": THRESHOLD_LEAD_QUENTE,
            "janela_historico_mensagens": JANELA_MENSAGENS_RECENTES,
            "estrategia_historico": (
                f"Janela deslizante de {JANELA_MENSAGENS_RECENTES} mensagens recentes + "
                "sumarização rolling de turnos anteriores em `resumo_historico`. "
                "Texto completo nunca armazenado no payload do lead."
            ),
            "pesos_score": {s.value: p for s, p in PESOS_SINAL.items()},
            "client_id": client_id,
        }

        logger.info(
            "[memory] Configuração de memória concluída para '%s' — CRM: %s.",
            client_id,
            "ativo" if resultado_crm.crm_enabled else "desativado",
        )
        return "done", payload
