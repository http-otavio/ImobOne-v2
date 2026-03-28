"""
agents/dev_persona.py — Agente 4: Dev de Personalização

Responsabilidade:
  Calibrar o que torna o produto "desta imobiliária específica" — tom, nome
  do consultor virtual, voice_id ElevenLabs, linguagem proibida, regras
  específicas e exemplos de saudação aprovados.

  Entrega: arquivo `prompts/clients/{client_id}/persona.yaml` com todos os
  campos obrigatórios preenchidos. Usa Claude Haiku para processar o briefing
  de tom e gerar o yaml a partir das instruções do cliente.

Dependência:
  Apenas o briefing de onboarding — roda em paralelo com ingestion e context.
  Não depende de nenhum outro agente.

Campos obrigatórios do persona.yaml:
  nome_consultor       — Ex: "Julia", "Marco", "Sofia"
  voice_id             — ElevenLabs voice_id aprovado pelo cliente
  tom_descritivo       — Parágrafo descrevendo o tom esperado
  palavras_proibidas   — Lista de palavras/expressões a evitar
  frases_proibidas     — Lista de frases completas proibidas
  exemplos_saudacao    — Lista de exemplos aprovados de saudação
  regras_especificas   — Regras de negócio específicas do cliente

Integração com o orquestrador:
  O método run(client_id, onboarding) retorna (status, payload) compatível
  com MockAgentFn. O payload inclui o path do yaml gerado e um resumo dos
  campos configurados.

Uso standalone:
    agent = DevPersonaAgent(anthropic_client)
    status, payload = await agent.run("cliente_001", onboarding)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Campos obrigatórios do persona.yaml. Qualquer campo ausente → "blocked".
CAMPOS_OBRIGATORIOS: list[str] = [
    "nome_consultor",
    "voice_id",
    "tom_descritivo",
    "palavras_proibidas",
    "frases_proibidas",
    "exemplos_saudacao",
    "regras_especificas",
]

# Fallback de voice_id quando o cliente não especificou (voz neutra premium).
VOICE_ID_FALLBACK = "21m00Tcm4TlvDq8ikWAM"  # Rachel — ElevenLabs default premium

# Fallback de saudações premium para clientes sem exemplos no briefing.
SAUDACOES_PREMIUM_FALLBACK: list[str] = [
    "Boa tarde! Como posso ajudá-lo hoje?",
    "Bom dia! Seja bem-vindo. Estou à sua disposição.",
    "Boa noite! Vejo que chegou pelo nosso portfólio. Posso apresentar algo especial.",
]


# ---------------------------------------------------------------------------
# Prompt para geração de persona via Claude Haiku
# ---------------------------------------------------------------------------

_PERSONA_PROMPT = """\
Você é um especialista em branding de luxo para o mercado imobiliário brasileiro.

Com base no briefing abaixo, gere um arquivo de configuração de persona para um
consultor digital de alto padrão. O consultor atenderá leads via WhatsApp com tom
sofisticado, preciso e discreto — nunca genérico, nunca apressado.

BRIEFING DO CLIENTE:
{briefing}

INSTRUÇÕES OBRIGATÓRIAS:
1. Retorne APENAS um bloco YAML válido, sem markdown, sem texto antes ou depois.
2. O yaml deve conter exatamente estes campos:

nome_consultor: <string — nome do consultor virtual, ex: "Julia", "Marco">
voice_id: <string — ElevenLabs voice_id se especificado, senão "{voice_id_fallback}">
tom_descritivo: |
  <parágrafo descrevendo o tom: sofisticado, preciso, discreto.
   Personalize para o briefing do cliente. Mínimo 2 frases.>
palavras_proibidas:
  - <palavra ou expressão proibida 1>
  - <palavra ou expressão proibida 2>
  # pelo menos 5 itens obrigatórios
frases_proibidas:
  - <frase completa proibida 1>
  - <frase completa proibida 2>
  # pelo menos 3 itens obrigatórios
exemplos_saudacao:
  - <saudação calibrada 1>
  - <saudação calibrada 2>
  - <saudação calibrada 3>
regras_especificas: |
  <regras de negócio específicas do cliente. Se não houver, escreva
   "Seguir as regras padrão do consultor de luxo sem adições.">

3. Palavras proibidas SEMPRE devem incluir no mínimo:
   "barato", "oportunidade imperdível", "não perca", "aproveite", "correndo"
   Adicione as palavras do briefing do cliente além destas.

4. Frases proibidas SEMPRE devem incluir no mínimo:
   "Essa é a melhor oferta do mercado!"
   "Corre que vai acabar!"
   Adicione as frases do briefing do cliente além destas.

5. Saudações devem ser naturais, sofisticadas e variadas por horário do dia.
   NUNCA use "Olá" isolado. Use "Bom dia", "Boa tarde", "Boa noite".

Retorne APENAS o YAML.
""".strip()


# ---------------------------------------------------------------------------
# DevPersonaAgent
# ---------------------------------------------------------------------------


class DevPersonaAgent:
    """
    Agente 4 — Dev de Personalização.

    Usa Claude Haiku para processar o briefing de tom do onboarding e gerar
    o arquivo persona.yaml com todos os campos obrigatórios para o cliente.

    Args:
        anthropic_client: Instância de anthropic.AsyncAnthropic (ou mock).
        output_base_dir: Diretório base para prompts/clients/. Default: projeto raiz.
    """

    def __init__(
        self,
        anthropic_client: Any,
        output_base_dir: Path | None = None,
    ) -> None:
        self._client = anthropic_client
        self._output_base = output_base_dir or (
            Path(__file__).parent.parent / "prompts" / "clients"
        )

    # ── Geração de persona via LLM ──────────────────────────────────────────

    async def _gerar_persona(self, briefing: str) -> dict:
        """
        Chama Claude Haiku com o briefing do cliente e retorna o yaml parseado.

        Args:
            briefing: Texto do briefing de tom e identidade do cliente.

        Returns:
            Dicionário com os campos do persona.yaml.

        Raises:
            ValueError: Se o YAML retornado for inválido ou campos obrigatórios
                        estiverem ausentes.
        """
        prompt = _PERSONA_PROMPT.format(
            briefing=briefing,
            voice_id_fallback=VOICE_ID_FALLBACK,
        )

        response = await self._client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text: str = response.content[0].text.strip()

        # Remove blocos de código markdown se o modelo os incluir
        raw_text = re.sub(r"^```ya?ml\s*", "", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"\s*```$", "", raw_text)
        raw_text = raw_text.strip()

        try:
            persona: dict = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML inválido retornado pelo Haiku: {exc}\n\nTexto:\n{raw_text}") from exc

        if not isinstance(persona, dict):
            raise ValueError(f"Esperava dict, recebeu {type(persona).__name__}: {raw_text}")

        return persona

    # ── Fallbacks e garantias ───────────────────────────────────────────────

    @staticmethod
    def _aplicar_fallbacks(persona: dict, onboarding: dict) -> dict:
        """
        Aplica valores padrão para campos ausentes ou vazios.

        Garante que o yaml nunca saia sem campos críticos, mesmo que o
        Haiku omita algum. Fallbacks são marcados como gerados automaticamente
        para revisão durante QA.

        Args:
            persona: Dicionário parseado do yaml gerado pelo Haiku.
            onboarding: Briefing original do cliente.

        Returns:
            Dicionário enriquecido com fallbacks aplicados.
        """
        # nome_consultor
        if not persona.get("nome_consultor"):
            persona["nome_consultor"] = onboarding.get("nome_consultor", "Julia")

        # voice_id
        if not persona.get("voice_id"):
            persona["voice_id"] = onboarding.get("voice_id", VOICE_ID_FALLBACK)

        # tom_descritivo
        if not persona.get("tom_descritivo"):
            persona["tom_descritivo"] = (
                "Tom sofisticado, preciso e discreto. Nunca genérico, nunca apressado. "
                "Trata o cliente de alto padrão com a atenção que merece."
            )

        # palavras_proibidas — garante lista e adiciona palavras do onboarding
        proibidas_base = {"barato", "oportunidade imperdível", "não perca", "aproveite", "correndo"}
        proibidas_existentes: list = persona.get("palavras_proibidas") or []
        proibidas_onboarding: list = onboarding.get("palavras_proibidas", [])
        todas_proibidas = list(proibidas_base | set(proibidas_existentes) | set(proibidas_onboarding))
        persona["palavras_proibidas"] = todas_proibidas

        # frases_proibidas — garante lista mínima
        frases_base = [
            "Essa é a melhor oferta do mercado!",
            "Corre que vai acabar!",
        ]
        frases_existentes: list = persona.get("frases_proibidas") or []
        frases_onboarding: list = onboarding.get("frases_proibidas", [])
        todas_frases = list({f.strip() for f in frases_base + frases_existentes + frases_onboarding if f})
        persona["frases_proibidas"] = todas_frases

        # exemplos_saudacao — fallback premium se vazio
        if not persona.get("exemplos_saudacao"):
            exemplos_onboarding: list = onboarding.get("exemplos_saudacao", [])
            persona["exemplos_saudacao"] = (
                exemplos_onboarding if exemplos_onboarding else SAUDACOES_PREMIUM_FALLBACK
            )

        # regras_especificas
        if not persona.get("regras_especificas"):
            persona["regras_especificas"] = onboarding.get(
                "regras_especificas",
                "Seguir as regras padrão do consultor de luxo sem adições.",
            )

        return persona

    @staticmethod
    def _validar_campos(persona: dict) -> list[str]:
        """
        Retorna lista de campos obrigatórios ausentes.

        Args:
            persona: Dicionário do persona.yaml.

        Returns:
            Lista de nomes de campos ausentes (vazia = OK).
        """
        ausentes = []
        for campo in CAMPOS_OBRIGATORIOS:
            valor = persona.get(campo)
            if valor is None or valor == "" or valor == [] or valor == {}:
                ausentes.append(campo)
        return ausentes

    # ── Escrita do arquivo ──────────────────────────────────────────────────

    def _escrever_yaml(self, client_id: str, persona: dict) -> Path:
        """
        Serializa o dicionário para YAML e salva em
        prompts/clients/{client_id}/persona.yaml.

        Args:
            client_id: ID do cliente.
            persona: Dicionário validado da persona.

        Returns:
            Path absoluto do arquivo gerado.
        """
        client_dir = self._output_base / client_id
        client_dir.mkdir(parents=True, exist_ok=True)

        yaml_path = client_dir / "persona.yaml"

        # Cabeçalho de documentação no topo do arquivo
        header = (
            f"# persona.yaml — Cliente: {client_id}\n"
            f"# Gerado pelo Agente 4 (dev_persona) — NÃO EDITAR MANUALMENTE.\n"
            f"# Alterações passam pelo Agente 2 (auditor) antes de serem aplicadas.\n"
            f"# voice_id ElevenLabs deve ser aprovado pelo cliente antes do go-live.\n\n"
        )

        yaml_content = yaml.dump(
            persona,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            indent=2,
        )

        yaml_path.write_text(header + yaml_content, encoding="utf-8")
        logger.info("[dev_persona] Persona escrita → %s", yaml_path)
        return yaml_path

    # ── Interface pública ───────────────────────────────────────────────────

    async def run(self, client_id: str, onboarding: dict) -> tuple[str, dict]:
        """
        Gera e persiste o arquivo persona.yaml para o cliente.

        Args:
            client_id: ID do cliente sendo configurado.
            onboarding: Dicionário de onboarding com o briefing de tom.

        Returns:
            ("done", payload) em caso de sucesso.
            ("blocked", {"error": mensagem}) em caso de falha.
        """
        # Monta o briefing a partir dos campos relevantes do onboarding
        briefing = self._montar_briefing(client_id, onboarding)

        try:
            persona_raw = await self._gerar_persona(briefing)
        except ValueError as exc:
            logger.error("[dev_persona] Falha na geração via Haiku: %s", exc)
            return "blocked", {
                "error": f"Falha ao gerar persona via LLM: {exc}",
                "agent": "dev_persona",
                "client_id": client_id,
            }
        except Exception as exc:
            logger.error("[dev_persona] Erro inesperado na chamada ao Haiku: %s", exc)
            return "blocked", {
                "error": f"Erro inesperado: {exc}",
                "agent": "dev_persona",
                "client_id": client_id,
            }

        # Aplica fallbacks defensivos
        persona = self._aplicar_fallbacks(persona_raw, onboarding)

        # Valida campos obrigatórios após fallbacks
        ausentes = self._validar_campos(persona)
        if ausentes:
            logger.error("[dev_persona] Campos obrigatórios ausentes após fallbacks: %s", ausentes)
            return "blocked", {
                "error": f"Campos obrigatórios ausentes no persona.yaml: {ausentes}",
                "agent": "dev_persona",
                "client_id": client_id,
                "campos_ausentes": ausentes,
            }

        try:
            yaml_path = self._escrever_yaml(client_id, persona)
        except OSError as exc:
            logger.error("[dev_persona] Erro de I/O ao escrever yaml: %s", exc)
            return "blocked", {
                "error": f"Erro ao escrever persona.yaml: {exc}",
                "agent": "dev_persona",
                "client_id": client_id,
            }

        payload = {
            "persona_path": str(yaml_path),
            "nome_consultor": persona["nome_consultor"],
            "voice_id": persona["voice_id"],
            "palavras_proibidas_count": len(persona.get("palavras_proibidas", [])),
            "exemplos_saudacao_count": len(persona.get("exemplos_saudacao", [])),
            "client_id": client_id,
        }

        logger.info(
            "[dev_persona] Persona gerada para '%s' — consultor: %s, voice: %s.",
            client_id,
            persona["nome_consultor"],
            persona["voice_id"],
        )
        return "done", payload

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _montar_briefing(client_id: str, onboarding: dict) -> str:
        """
        Consolida os campos de identidade do onboarding em um texto de briefing
        para enviar ao Haiku.

        Inclui apenas campos disponíveis — campos ausentes são ignorados
        (o Haiku usará seus próprios padrões, e os fallbacks cobrem o restante).
        """
        linhas = [f"Cliente ID: {client_id}"]

        campos_briefing = {
            "nome_imobiliaria": "Nome da imobiliária",
            "nome_consultor": "Nome desejado para o consultor virtual",
            "cidade_atuacao": "Cidade de atuação",
            "tipo_atuacao": "Tipo de atuação (venda, locação, lançamentos)",
            "voice_id": "ElevenLabs voice_id aprovado",
            "briefing_tom": "Briefing de tom e identidade",
            "palavras_proibidas": "Palavras proibidas (lista)",
            "frases_proibidas": "Frases proibidas (lista)",
            "exemplos_saudacao": "Exemplos de saudação aprovados (lista)",
            "exemplos_comunicacao": "Exemplos de comunicação aprovados",
            "regras_especificas": "Regras de negócio específicas",
            "publico_alvo": "Público-alvo principal",
        }

        for campo, label in campos_briefing.items():
            valor = onboarding.get(campo)
            if valor is not None and valor != "" and valor != []:
                if isinstance(valor, list):
                    linhas.append(f"{label}:")
                    for item in valor:
                        linhas.append(f"  - {item}")
                else:
                    linhas.append(f"{label}: {valor}")

        return "\n".join(linhas)
