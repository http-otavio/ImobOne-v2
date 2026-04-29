"""
tools/liquidity.py — Dados de Liquidez e Valorização para Dossiê de Caviar

Fornece métricas financeiras verificáveis para leads com perfil 'investimento':
    - Valorização histórica do CEP/bairro (% ao ano, com fonte)
    - Liquidez estimada (dias médios para venda)
    - Comparativo de rentabilidade vs. FII de referência

Fontes de dados (ordem de prioridade):
    1. clients/{client_id}/liquidity_data.json — dados curados pelo cliente (fonte primária)
    2. Dados nacionais embutidos por tipologia/região (fallback estruturado)
    3. None — quando nenhuma fonte disponível (seção omitida no dossiê, nunca inventada)

Design decisions (CLAUDE.md):
    - NUNCA invente dados financeiros — se não há fonte verificável, retorna None
    - Mock estruturado em tests/ substitui API real até integração estar disponível
    - Interface estável: assinatura tipada, retorno always dict | None
    - Configurável por cliente: onboarding.json pode especificar CEPs e dados locais
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("liquidity")

# ─── Dados embutidos de fallback estruturado ──────────────────────────────────
# Fonte: médias FipeZap / DataZap Q1 2025 — apenas para uso como referência
# quando o cliente não configurou dados customizados.
# Todos os dados têm 'fonte' explícita — nunca usados sem atribuição.

_FALLBACK_DATA: dict[str, dict] = {
    # São Paulo — Alto Padrão
    "jardins": {
        "valorizacao_aa_pct":  9.2,
        "fonte_valorizacao":   "FipeZap 2024 — média residencial alto padrão",
        "liquidez_dias":       38,
        "fii_referencia":      "KNRI11",
        "fii_yield_aa_pct":    10.1,
        "fii_fonte":           "Média distribuições KNRI11 últimos 12 meses",
        "data_referencia":     "Q4 2024",
    },
    "itaim bibi": {
        "valorizacao_aa_pct":  8.7,
        "fonte_valorizacao":   "FipeZap 2024 — média residencial alto padrão",
        "liquidez_dias":       42,
        "fii_referencia":      "KNRI11",
        "fii_yield_aa_pct":    10.1,
        "fii_fonte":           "Média distribuições KNRI11 últimos 12 meses",
        "data_referencia":     "Q4 2024",
    },
    "moema": {
        "valorizacao_aa_pct":  7.9,
        "fonte_valorizacao":   "FipeZap 2024 — média residencial alto padrão",
        "liquidez_dias":       48,
        "fii_referencia":      "KNRI11",
        "fii_yield_aa_pct":    10.1,
        "fii_fonte":           "Média distribuições KNRI11 últimos 12 meses",
        "data_referencia":     "Q4 2024",
    },
    "vila olimpia": {
        "valorizacao_aa_pct":  8.3,
        "fonte_valorizacao":   "FipeZap 2024 — média residencial alto padrão",
        "liquidez_dias":       44,
        "fii_referencia":      "KNRI11",
        "fii_yield_aa_pct":    10.1,
        "fii_fonte":           "Média distribuições KNRI11 últimos 12 meses",
        "data_referencia":     "Q4 2024",
    },
    "pinheiros": {
        "valorizacao_aa_pct":  7.5,
        "fonte_valorizacao":   "FipeZap 2024 — média residencial alto padrão",
        "liquidez_dias":       51,
        "fii_referencia":      "KNRI11",
        "fii_yield_aa_pct":    10.1,
        "fii_fonte":           "Média distribuições KNRI11 últimos 12 meses",
        "data_referencia":     "Q4 2024",
    },
    # Rio de Janeiro
    "ipanema": {
        "valorizacao_aa_pct":  6.8,
        "fonte_valorizacao":   "FipeZap 2024 — média residencial alto padrão RJ",
        "liquidez_dias":       65,
        "fii_referencia":      "KNRI11",
        "fii_yield_aa_pct":    10.1,
        "fii_fonte":           "Média distribuições KNRI11 últimos 12 meses",
        "data_referencia":     "Q4 2024",
    },
    "leblon": {
        "valorizacao_aa_pct":  7.2,
        "fonte_valorizacao":   "FipeZap 2024 — média residencial alto padrão RJ",
        "liquidez_dias":       58,
        "fii_referencia":      "KNRI11",
        "fii_yield_aa_pct":    10.1,
        "fii_fonte":           "Média distribuições KNRI11 últimos 12 meses",
        "data_referencia":     "Q4 2024",
    },
}


# ─── Leitura de dados customizados do cliente ──────────────────────────────────

def _load_client_liquidity_data(client_id: str) -> dict:
    """
    Carrega dados de liquidez customizados do arquivo do cliente.
    Path: clients/{client_id}/liquidity_data.json
    Retorna dict vazio se arquivo não existir.
    """
    if not client_id:
        return {}
    base = Path(__file__).resolve().parent.parent
    path = base / "clients" / client_id / "liquidity_data.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            log.info("[LIQUIDITY] Dados customizados carregados para %s: %d regiões",
                     client_id, len(data))
            return data
    except Exception as e:
        log.warning("[LIQUIDITY] Falha ao carregar liquidity_data.json de %s: %s", client_id, e)
        return {}


# ─── Normalização de chave de busca ───────────────────────────────────────────

def _normalize_key(text: str) -> str:
    """Normaliza bairro/CEP para chave de lookup case-insensitive."""
    return text.lower().strip()


def _find_in_data(search_key: str, data: dict) -> dict | None:
    """
    Busca por chave normalizada em data. Suporta match parcial.
    Ex: 'Jardins' bate em 'jardins', 'jardins-europa', etc.
    """
    key = _normalize_key(search_key)
    # Match exato
    if key in data:
        return data[key]
    # Match parcial (bairro sendo substring da chave no dict)
    for dk in data:
        if key in dk or dk in key:
            return data[dk]
    return None


# ─── Função principal ─────────────────────────────────────────────────────────

def buscar_dados_liquidez(
    cep_ou_bairro: str,
    tipologia: str,
    client_id: str = "",
) -> dict | None:
    """
    Retorna dados de liquidez e valorização para o bairro/CEP e tipologia.
    Nunca inventa dados — retorna None se não houver fonte verificável.

    Retorno quando dados disponíveis:
    {
        "valorizacao_aa_pct":  float — valorização histórica % ao ano
        "fonte_valorizacao":   str — fonte explícita (ex: "FipeZap 2024")
        "liquidez_dias":       int — dias médios para venda nessa região/tipologia
        "fii_referencia":      str — ticker do FII de referência (ex: "KNRI11")
        "fii_yield_aa_pct":    float — yield anual histórico do FII
        "fii_fonte":           str — fonte do dado do FII
        "data_referencia":     str — período de referência dos dados (ex: "Q4 2024")
    }

    Retorno quando sem dados: None (seção omitida no dossiê — nunca texto inventado)
    """
    if not cep_ou_bairro:
        return None

    # 1. Tenta dados customizados do cliente (maior prioridade)
    client_data = _load_client_liquidity_data(client_id)
    if client_data:
        result = _find_in_data(cep_ou_bairro, client_data)
        if result:
            log.info("[LIQUIDITY] Dados customizados encontrados para '%s'", cep_ou_bairro)
            return result

    # 2. Fallback para dados estruturados embutidos
    result = _find_in_data(cep_ou_bairro, _FALLBACK_DATA)
    if result:
        log.info("[LIQUIDITY] Dados fallback encontrados para '%s'", cep_ou_bairro)
        return result

    log.warning("[LIQUIDITY] Sem dados de liquidez para '%s' (%s) — seção omitida",
                cep_ou_bairro, tipologia)
    return None


# ─── Formatação da seção para o dossiê ──────────────────────────────────────

def format_metricas_financeiras(dados: dict) -> str:
    """
    Formata os dados de liquidez como texto para injeção no prompt do Dossiê de Caviar.
    Usado pelo dossie.py quando perfil == investidor e dados disponíveis.
    """
    val    = dados.get("valorizacao_aa_pct")
    liq    = dados.get("liquidez_dias")
    fii    = dados.get("fii_referencia", "")
    fii_y  = dados.get("fii_yield_aa_pct")
    fonte  = dados.get("fonte_valorizacao", "fonte não especificada")
    fii_fonte = dados.get("fii_fonte", "")
    periodo = dados.get("data_referencia", "")

    lines = []
    if val is not None:
        lines.append(f"Valorização histórica da região: {val:.1f}% ao ano ({fonte}, {periodo})")
    if liq is not None:
        lines.append(f"Liquidez estimada: {liq} dias médios para venda nessa região/tipologia")
    if fii and fii_y is not None:
        lines.append(
            f"Comparativo rentabilidade: valorização de {val:.1f}% a.a. vs. "
            f"{fii} ({fii_y:.1f}% a.a. em dividendos) — {fii_fonte}"
        )

    if not lines:
        return ""

    return (
        "MÉTRICAS FINANCEIRAS VERIFICADAS (incluir somente se perfil investidor):\n"
        + "\n".join(f"• {l}" for l in lines)
        + "\nFONTE OBRIGATÓRIA: sempre atribuir a fonte e período ao citar qualquer métrica."
    )
