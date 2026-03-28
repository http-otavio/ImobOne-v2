"""
onboarding_schema.py — Schema Pydantic do Onboarding de Cliente

Valida o JSON de onboarding antes de iniciar qualquer pipeline.
Todos os agentes da Fase 1 consomem os dados daqui como fonte da verdade.

Campos obrigatórios vs opcionais variam por segmento:
  - lancamentos:  campos de lançamento obrigatórios
  - alugueis:     garantias_aceitas obrigatório
  - vendas:       ticket_medio_vendas obrigatório

Uso:
    from onboarding_schema import OnboardingSchema, validar_onboarding_pydantic

    dados = json.load(open("clients/alfa/onboarding.json"))
    schema, erros = validar_onboarding_pydantic(dados)
    if erros:
        for e in erros:
            print(e)
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Segmento(str, Enum):
    VENDAS     = "vendas"
    ALUGUEIS   = "alugueis"
    LANCAMENTOS = "lancamentos"
    TEMPORADA  = "temporada"


class TomConsultor(str, Enum):
    SOFISTICADO  = "sofisticado"
    CONSULTIVO   = "consultivo"
    AMIGAVEL     = "amigavel"


class GeneroConsultor(str, Enum):
    FEMININO  = "feminino"
    MASCULINO = "masculino"
    NEUTRO    = "neutro"


class IdentidadeIA(str, Enum):
    MEMBRO_EQUIPE = "membro_equipe"   # nunca revela ser IA espontaneamente
    IA_DECLARADA  = "ia_declarada"    # apresenta-se como assistente virtual


class BSPProvider(str, Enum):
    DIALOG360  = "360dialog"
    GUPSHUP    = "gupshup"
    TWILIO     = "twilio"


class EstrategiaDistribuicao(str, Enum):
    ROUND_ROBIN   = "round_robin"
    POR_ESPECIALIDADE = "por_especialidade"
    POR_BAIRRO    = "por_bairro"
    MANUAL        = "manual"


class GarantiaAluguel(str, Enum):
    FIADOR         = "fiador"
    SEGURO_FIANCA  = "seguro_fianca"
    DEPOSITO       = "deposito"
    TITULO_CAP     = "titulo_cap"
    CARTAO_CREDITO = "cartao_credito"


class CanalAlerta(str, Enum):
    WHATSAPP = "whatsapp"
    SLACK    = "slack"
    EMAIL    = "email"


# ---------------------------------------------------------------------------
# Seção 1 — Dados da imobiliária
# ---------------------------------------------------------------------------

class DadosImobiliaria(BaseModel):
    nome_imobiliaria:    str              = Field(..., min_length=2, max_length=100)
    cidade_atuacao:      str              = Field(..., min_length=2, max_length=80)
    segmentos:           list[Segmento]   = Field(..., min_length=1)
    site:                str | None       = Field(None, description="URL do site, se houver")
    portais_anuncio:     list[str]        = Field(default_factory=list,
                                               description="ZAP, VivaReal, OLX, site próprio...")
    ticket_medio_vendas:    float | None  = Field(None, description="Obrigatório se segmento inclui vendas")
    ticket_medio_aluguel:   float | None  = Field(None, description="Obrigatório se segmento inclui alugueis")
    ticket_medio_temporada: float | None  = Field(None, description="Obrigatório se segmento inclui temporada")

    @field_validator("site")
    @classmethod
    def validar_site(cls, v: str | None) -> str | None:
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("site deve começar com http:// ou https://")
        return v


# ---------------------------------------------------------------------------
# Seção 2 — Portfólio
# ---------------------------------------------------------------------------

class Portfolio(BaseModel):
    portfolio_path: str | None = Field(
        None,
        description="Caminho relativo ao projeto para CSV/Excel/PDF/JSON do portfólio"
    )
    portfolio_url: str | None = Field(
        None,
        description="URL do portal ou site para ingestão automática"
    )
    total_imoveis_estimado: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def ao_menos_uma_fonte(self) -> "Portfolio":
        if not self.portfolio_path and not self.portfolio_url:
            raise ValueError(
                "É necessário fornecer portfolio_path (arquivo) "
                "ou portfolio_url (site/portal) — ao menos um dos dois."
            )
        return self


# ---------------------------------------------------------------------------
# Seção 3 — Corretores
# ---------------------------------------------------------------------------

class Corretor(BaseModel):
    nome:              str        = Field(..., min_length=2)
    telefone_whatsapp: str        = Field(..., description="Número com DDI, ex: +5511999999999")
    especialidade:     list[Segmento] = Field(..., min_length=1)
    bairros_regioes:   list[str]  = Field(default_factory=list)
    horario_inicio:    str        = Field(..., pattern=r"^\d{2}:\d{2}$",
                                         description="Formato HH:MM, ex: 08:00")
    horario_fim:       str        = Field(..., pattern=r"^\d{2}:\d{2}$",
                                         description="Formato HH:MM, ex: 19:00")
    dias_atendimento:  list[str]  = Field(
        default_factory=lambda: ["seg", "ter", "qua", "qui", "sex"],
        description="Lista com seg, ter, qua, qui, sex, sab, dom"
    )

    @field_validator("telefone_whatsapp")
    @classmethod
    def validar_telefone(cls, v: str) -> str:
        digitos = "".join(c for c in v if c.isdigit())
        if len(digitos) < 10 or len(digitos) > 15:
            raise ValueError("Telefone deve ter entre 10 e 15 dígitos")
        return v


# ---------------------------------------------------------------------------
# Seção 4 — Consultor virtual
# ---------------------------------------------------------------------------

class ConsultorVirtual(BaseModel):
    nome_consultor:       str            = Field(..., min_length=2, max_length=30)
    genero:               GeneroConsultor = GeneroConsultor.FEMININO
    tom_desejado:         TomConsultor   = TomConsultor.SOFISTICADO
    identidade:           IdentidadeIA   = IdentidadeIA.MEMBRO_EQUIPE
    resposta_se_perguntado_ia: str       = Field(
        ...,
        description="O que o consultor responde se o lead perguntar se é IA"
    )
    frases_abertura:      list[str]      = Field(..., min_length=1,
                                                  description="Frases de saudação inicial")
    frases_encerramento:  list[str]      = Field(..., min_length=1,
                                                  description="Frases de despedida")
    palavras_proibidas:   list[str]      = Field(default_factory=list,
                                                  description="Palavras que o consultor nunca usa")
    palavras_preferidas:  list[str]      = Field(default_factory=list,
                                                  description="Palavras e termos preferidos da marca")


# ---------------------------------------------------------------------------
# Seção 5 — WhatsApp
# ---------------------------------------------------------------------------

class ConfigWhatsApp(BaseModel):
    numero_whatsapp: str        = Field(..., description="Número dedicado do consultor, com DDI")
    bsp_provider:    BSPProvider = BSPProvider.DIALOG360
    bsp_api_key:     str        = Field(..., min_length=4, description="Chave da API do BSP")
    bsp_url:         str | None = Field(None, description="URL base do BSP (se necessário)")

    @field_validator("numero_whatsapp")
    @classmethod
    def validar_numero(cls, v: str) -> str:
        digitos = "".join(c for c in v if c.isdigit())
        if len(digitos) < 10 or len(digitos) > 15:
            raise ValueError("numero_whatsapp deve ter entre 10 e 15 dígitos")
        return v


# ---------------------------------------------------------------------------
# Seção 6 — Distribuição de leads
# ---------------------------------------------------------------------------

class DistribuicaoLeads(BaseModel):
    estrategia:                   EstrategiaDistribuicao = EstrategiaDistribuicao.POR_ESPECIALIDADE
    timeout_corretor_minutos:     int   = Field(15, ge=1, le=1440,
                                                description="Minutos até considerar corretor sem resposta")
    acao_sem_resposta_corretor:   str   = Field(
        "notificar_proximo_da_lista",
        description="O que fazer se o corretor não responder: notificar_proximo_da_lista | escalar_gerente | agendar_retorno"
    )
    notificar_corretor_score_minimo: int = Field(
        5, ge=0, le=20,
        description="Score de intenção mínimo para notificar corretor imediatamente"
    )


# ---------------------------------------------------------------------------
# Seção 7 — Qualificação
# ---------------------------------------------------------------------------

class QualificacaoSegmento(BaseModel):
    perguntas_obrigatorias: list[str] = Field(..., min_length=1)
    criterios_lead_quente:  list[str] = Field(..., min_length=1,
                                               description="Critérios que classificam o lead como quente")
    criterios_lead_frio:    list[str] = Field(..., min_length=1,
                                               description="Critérios que classificam o lead como frio")


class ConfigQualificacao(BaseModel):
    vendas:      QualificacaoSegmento | None = None
    alugueis:    QualificacaoSegmento | None = None
    lancamentos: QualificacaoSegmento | None = None
    temporada:   QualificacaoSegmento | None = None


# ---------------------------------------------------------------------------
# Seção 8 — Regras de negócio
# ---------------------------------------------------------------------------

class RegrasNegocio(BaseModel):
    pode_falar_desconto:      bool  = False
    desconto_maximo_percent:  float | None = Field(None, ge=0, le=100)
    parceiros_financiamento:  list[str]    = Field(default_factory=list,
                                                    description="Bancos e financeiras parceiras")
    garantias_aceitas_aluguel: list[GarantiaAluguel] = Field(
        default_factory=list,
        description="Obrigatório se segmento inclui alugueis"
    )
    regras_adicionais:        list[str]    = Field(default_factory=list,
                                                    description="Regras específicas da imobiliária")

    @model_validator(mode="after")
    def desconto_coerente(self) -> "RegrasNegocio":
        if self.pode_falar_desconto and self.desconto_maximo_percent is None:
            raise ValueError(
                "Se pode_falar_desconto=true, defina desconto_maximo_percent"
            )
        return self


# ---------------------------------------------------------------------------
# Seção 9 — CRM
# ---------------------------------------------------------------------------

class ConfigCRM(BaseModel):
    crm_nome:         str | None = Field(None, description="Nome do CRM (Pipedrive, RD Station, HubSpot...)")
    crm_webhook_url:  str | None = Field(None, description="URL do webhook para envio de leads")
    crm_api_key:      str | None = None
    campos_customizados: dict[str, str] = Field(
        default_factory=dict,
        description="Mapeamento campo_imob → campo_crm"
    )

    @field_validator("crm_webhook_url")
    @classmethod
    def validar_webhook(cls, v: str | None) -> str | None:
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("crm_webhook_url deve começar com http:// ou https://")
        return v


# ---------------------------------------------------------------------------
# Seção 10 — Horários de atendimento
# ---------------------------------------------------------------------------

class HorarioAtendimento(BaseModel):
    horario_inicio:    str  = Field("08:00", pattern=r"^\d{2}:\d{2}$")
    horario_fim:       str  = Field("20:00", pattern=r"^\d{2}:\d{2}$")
    atende_sabado:     bool = True
    atende_domingo:    bool = False
    feriados_nacionais: bool = Field(True, description="Segue feriados nacionais brasileiros")
    feriados_locais:   list[str] = Field(
        default_factory=list,
        description="Datas específicas fora de atendimento, formato YYYY-MM-DD"
    )
    comportamento_fora_horario: str = Field(
        "informar_horario_e_registrar_lead",
        description="O que o consultor faz fora do horário comercial"
    )
    mensagem_fora_horario: str = Field(
        "Olá! Nosso horário de atendimento é de {horario_inicio} às {horario_fim}. "
        "Vou registrar sua mensagem e {nome_consultor} entrará em contato assim que possível.",
        description="Mensagem enviada fora do horário. Suporta {horario_inicio}, {horario_fim}, {nome_consultor}"
    )


# ---------------------------------------------------------------------------
# Seção 11 — Escalação para humano
# ---------------------------------------------------------------------------

class ConfigEscalacao(BaseModel):
    gatilhos_escalacao: list[str] = Field(
        ...,
        min_length=1,
        description="Situações que disparam transferência para humano"
    )
    mensagem_transferencia: str = Field(
        ...,
        description="O que o consultor diz ao lead ao transferir"
    )
    canal_alerta_operador:  CanalAlerta = CanalAlerta.WHATSAPP
    contato_alerta:         str         = Field(
        ...,
        description="Número WhatsApp, canal Slack (#canal) ou e-mail para alertas"
    )
    escalar_apos_n_mensagens_sem_resolucao: int = Field(5, ge=1, le=50)


# ---------------------------------------------------------------------------
# Seção 12 — Dashboard
# ---------------------------------------------------------------------------

class ConfigDashboard(BaseModel):
    usuarios_dashboard: list[str] = Field(
        default_factory=list,
        description="E-mails dos usuários com acesso ao dashboard"
    )
    relatorio_semanal:    bool       = True
    email_relatorio:      str | None = None
    metricas_prioritarias: list[str] = Field(
        default_factory=lambda: [
            "leads_atendidos",
            "taxa_qualificacao",
            "score_medio_intenção",
            "agendamentos_realizados",
            "tempo_medio_resposta",
        ]
    )


# ---------------------------------------------------------------------------
# Schema raiz — OnboardingSchema
# ---------------------------------------------------------------------------

class OnboardingSchema(BaseModel):
    """
    Schema completo de onboarding de cliente.

    Validações cross-seção:
    - Se segmentos inclui 'lancamentos', requer campos de lançamento
    - Se segmentos inclui 'alugueis', requer garantias_aceitas_aluguel
    - Se segmentos inclui 'vendas', requer ticket_medio_vendas
    - qualificacao deve ter entrada para cada segmento ativo
    """

    # Identificação
    client_id:    str = Field(
        ...,
        pattern=r"^[a-z0-9_]{3,50}$",
        description="Identificador único do cliente (snake_case, sem espaços)"
    )
    versao_schema: str = Field("1.0", description="Versão do schema utilizado")

    # Seções
    imobiliaria:    DadosImobiliaria
    portfolio:      Portfolio
    corretores:     list[Corretor]   = Field(..., min_length=1)
    consultor:      ConsultorVirtual
    whatsapp:       ConfigWhatsApp
    distribuicao:   DistribuicaoLeads = Field(default_factory=DistribuicaoLeads)
    qualificacao:   ConfigQualificacao
    regras:         RegrasNegocio
    crm:            ConfigCRM        = Field(default_factory=ConfigCRM)
    horarios:       HorarioAtendimento = Field(default_factory=HorarioAtendimento)
    escalacao:      ConfigEscalacao
    dashboard:      ConfigDashboard  = Field(default_factory=ConfigDashboard)

    # Campos de compatibilidade — lidos diretamente pelos agentes Fase 1
    # (aliases para evitar refatoração dos agentes existentes)
    @property
    def nome_imobiliaria(self) -> str:
        return self.imobiliaria.nome_imobiliaria

    @property
    def cidade_atuacao(self) -> str:
        return self.imobiliaria.cidade_atuacao

    @property
    def tipo_atuacao(self) -> list[str]:
        return [s.value for s in self.imobiliaria.segmentos]

    @property
    def portfolio_path(self) -> str | None:
        return self.portfolio.portfolio_path

    @property
    def tom_desejado(self) -> str:
        return self.consultor.tom_desejado.value

    @property
    def nome_consultor(self) -> str:
        return self.consultor.nome_consultor

    # Validações cross-seção
    @model_validator(mode="after")
    def validar_consistencia_segmentos(self) -> "OnboardingSchema":
        segmentos = {s.value for s in self.imobiliaria.segmentos}
        erros: list[str] = []

        if "vendas" in segmentos and self.imobiliaria.ticket_medio_vendas is None:
            erros.append("ticket_medio_vendas é obrigatório quando segmento inclui 'vendas'")

        if "alugueis" in segmentos:
            if self.imobiliaria.ticket_medio_aluguel is None:
                erros.append("ticket_medio_aluguel é obrigatório quando segmento inclui 'alugueis'")
            if not self.regras.garantias_aceitas_aluguel:
                erros.append("regras.garantias_aceitas_aluguel é obrigatório quando segmento inclui 'alugueis'")
            if self.qualificacao.alugueis is None:
                erros.append("qualificacao.alugueis é obrigatório quando segmento inclui 'alugueis'")

        if "lancamentos" in segmentos:
            if self.qualificacao.lancamentos is None:
                erros.append("qualificacao.lancamentos é obrigatório quando segmento inclui 'lancamentos'")

        if "vendas" in segmentos and self.qualificacao.vendas is None:
            erros.append("qualificacao.vendas é obrigatório quando segmento inclui 'vendas'")

        if "temporada" in segmentos and self.imobiliaria.ticket_medio_temporada is None:
            erros.append("ticket_medio_temporada é obrigatório quando segmento inclui 'temporada'")

        if erros:
            raise ValueError("\n".join(erros))

        return self

    def to_legacy_dict(self) -> dict[str, Any]:
        """
        Retorna dicionário compatível com os agentes da Fase 1.
        Preserva os campos que os agentes leem diretamente.
        """
        d = self.model_dump(mode="json")
        # Injeta aliases no nível raiz para compatibilidade
        d["nome_imobiliaria"]   = self.nome_imobiliaria
        d["cidade_atuacao"]     = self.cidade_atuacao
        d["tipo_atuacao"]       = self.tipo_atuacao
        d["portfolio_path"]     = self.portfolio_path
        d["tom_desejado"]       = self.tom_desejado
        d["nome_consultor"]     = self.nome_consultor
        return d


# ---------------------------------------------------------------------------
# Função pública de validação
# ---------------------------------------------------------------------------

def validar_onboarding_pydantic(dados: dict) -> tuple[OnboardingSchema | None, list[str]]:
    """
    Valida um dicionário de onboarding contra o OnboardingSchema.

    Returns:
        (schema, []) se válido
        (None, [erros]) se inválido
    """
    from pydantic import ValidationError
    try:
        schema = OnboardingSchema.model_validate(dados)
        return schema, []
    except ValidationError as exc:
        erros = []
        for e in exc.errors():
            loc = " → ".join(str(p) for p in e["loc"])
            erros.append(f"[{loc}] {e['msg']}")
        return None, erros


# ---------------------------------------------------------------------------
# CLI de validação rápida
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python onboarding_schema.py <caminho/para/onboarding.json>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Arquivo não encontrado: {path}")
        sys.exit(1)

    dados = json.loads(path.read_text(encoding="utf-8"))
    schema, erros = validar_onboarding_pydantic(dados)

    if erros:
        print(f"✗ Onboarding INVÁLIDO — {len(erros)} erro(s):")
        for e in erros:
            print(f"  {e}")
        sys.exit(2)
    else:
        print(f"✓ Onboarding válido — cliente: {schema.client_id} | "
              f"segmentos: {schema.tipo_atuacao} | "
              f"corretores: {len(schema.corretores)}")
        sys.exit(0)
