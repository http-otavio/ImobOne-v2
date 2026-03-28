# Dockerfile — ImobOne-v2 (serviço agents + dashboard na mesma imagem)
#
# Build:
#   docker build -t imovel-ai-agents:latest .
#
# Entrypoint padrão: main.py (sistema de agentes)
# Dashboard usa command override no docker-compose.yml.

# ── Base ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Metadados
LABEL maintainer="otavio.mlemos20@gmail.com"
LABEL project="ImobOne-v2"
LABEL description="Sistema de agentes IA para imobiliárias de alto padrão"

# ── Variáveis de build ────────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# ── Dependências de sistema ───────────────────────────────────────────────────
# build-essential: necessário para compilar extensões C (pgvector, algumas libs)
# curl: usado no healthcheck e debugging
# libpq-dev: cliente PostgreSQL para Supabase/psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Diretório de trabalho ─────────────────────────────────────────────────────
WORKDIR /app

# ── Dependências Python ───────────────────────────────────────────────────────
# Copia requirements primeiro para aproveitar cache de camadas Docker.
# Se apenas o código mudar (não requirements.txt), esta camada não é reconstruída.
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# ── Código do projeto ─────────────────────────────────────────────────────────
COPY . .

# ── Prompts base — garantia contra FUSE/filesystem anomalias ─────────────────
# _prompts_build/ contém os templates que o filesystem montado (FUSE) pode não
# copiar corretamente via COPY. Movemos para o caminho canônico e removemos
# o diretório temporário para manter a imagem limpa.
RUN mkdir -p /app/prompts/base \
    && cp /app/_prompts_build/consultant_base.md /app/prompts/base/consultant_base.md \
    && cp /app/_prompts_build/auditor.md         /app/prompts/base/auditor.md \
    && rm -rf /app/_prompts_build

# ── Usuário não-root (segurança) ──────────────────────────────────────────────
RUN groupadd --gid 1001 appuser \
    && useradd --uid 1001 --gid 1001 --no-create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

# ── Porta exposta pelo dashboard ──────────────────────────────────────────────
EXPOSE 8000

# ── Entrypoint padrão: sistema de agentes ────────────────────────────────────
# Usando CMD (não ENTRYPOINT) para que docker-compose / Docker Swarm possam
# sobrescrever via `command:` no stack file — ENTRYPOINT bloquearia o override.
# O serviço dashboard usa:
#   command: ["python", "-m", "uvicorn", "dashboard.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["python", "main.py"]
