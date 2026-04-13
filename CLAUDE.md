# CLAUDE.md — Projeto: Plataforma de IA para Imobiliárias de Alto Padrão

## Visão geral do projeto

Estamos construindo uma plataforma SaaS de automação + IA para imobiliárias de alto padrão brasileiras que trabalham com vendas, aluguéis e lançamentos de imóveis.

O produto final é um **consultor digital de luxo** que atende leads via WhatsApp com nível de sofisticação que justifica o posicionamento premium — respostas em áudio com voz natural, dados reais de vizinhança via Google Maps em tempo real, qualificação conversacional, e integração com CRM.

**Modelo de negócio:** R$ 15–25k setup + R$ 3,5–5k/mês por cliente. SLA de entrega de até 5 dias úteis por nova instância.

**Concorrente principal:** Lais.ai (produto de massa, não atende alto padrão com profundidade).

---

## Status do projeto

| Fase | Status | Data |
|------|--------|------|
| FASE 1 — Time de 10 agentes | ✅ **COMPLETA** — rodando em produção no VPS | Março 2026 |
| FASE 2 — Consultor digital de luxo | 🟡 **EM ANDAMENTO** — infra base pronta, demo rodando | Março 2026 |
| FASE 3 — Serviço de luxo (roadmap) | 🔵 **PLANEJADA** — features mapeadas, ancora o preço de R$25k | Abril 2026 |
| CAMADA DE AUTONOMIA — Nightly Squad | ✅ **ATIVA** — time de agentes desenvolve o produto às 02:00, PRs abertos para revisão manual | Abril 2026 |

**Infraestrutura ativa:**
- VPS: `76.13.165.64` — Docker Swarm, serviço `imob_agents`
- SSH: `ssh vps-imob` (alias em `~/.ssh/config`, chave `~/.ssh/cowork_deploy`)
- Python venv: `/opt/webhook-venv/bin/python3` (contém anthropic, supabase, openai, etc.)
- Repositório: `https://github.com/http-otavio/ImobOne-v2` (privado) — remote git: `git@github-imob:http-otavio/ImobOne-v2.git`
- Clone no VPS: `/opt/ImobOne-v2` (deploy via `git pull + docker build + service update`)
- Instância legada: `/opt/imovel-ai/ImobOne-v2` (manter como backup)
- Webhook demo: `/opt/whatsapp_webhook.py` — systemd `whatsapp-webhook.service` — porta 8001
- Follow-up engine: `/opt/ImobOne-v2/followup_engine.py` — systemd `imob-followup.timer` (hourly)
- **Pipeline Runner:** `/opt/ImobOne-v2/pipeline_runner.py` — systemd `imob-runner.service` — porta 8003 — dispara pipelines autonomamente e notifica operador via WhatsApp
- Dashboard do gestor: `/opt/ImobOne-v2/dashboard.html` — HTML puro + Chart.js + Supabase JS (sem backend extra)
- Env vars do webhook: `/opt/webhook.env` — inclui `CORRETOR_NUMBER=5511973722075`, `OPERATOR_NUMBER=5511973722075`, `RUNNER_PORT=8003`, `GITHUB_TOKEN` (configurado), `GITHUB_REPO=http-otavio/ImobOne-v2`
- Supabase: projeto `imobonev2` (id: `ksqtyjucvldlvuzqmnjh`) — sa-east-1 — pgvector ativo
- Evolution API (demo): instância `devlabz` em `https://api.otaviolabs.com`

**Pipeline de deploy (VPS):**
```bash
cd /opt/ImobOne-v2
git pull
docker build -t imovel-ai-agents:latest .
docker stack deploy -c docker-compose.yml imob
```

---

## Ordem de construção — NÃO INVERTER

```
FASE 1: Time de 10 agentes (o sistema que constrói o produto)  ← COMPLETA
FASE 2: Produto — consultor digital de luxo (o que os agentes entregam)  ← PRÓXIMA
```

Os agentes constroem, testam e fazem deploy de uma instância do produto para cada novo cliente. O produto nunca é construído manualmente.

---

## Stack técnica

| Camada | Tecnologia | Motivo |
|--------|-----------|--------|
| Orquestração de agentes | LangGraph (Python) | Grafo de estados explícito, controle fino de fluxo |
| LLM principal | Claude Sonnet (Anthropic API) | Raciocínio profundo para orquestrador e auditor |
| LLM auxiliar | Claude Haiku | Agentes de execução simples — reduz custo |
| Memória persistente | Supabase + pgvector | Portfólio, histórico de leads, logs de auditoria |
| Estado quente / filas | Redis | Shared state board entre agentes, pub/sub, session locks |
| Canal de comunicação | WhatsApp Business API via **360dialog** (BSP escolhido) | Oficial Meta, acesso direto sem camada proprietária, zero risco de ban |
| TTS (voz) | ElevenLabs API | Voz natural configurável por cliente |
| Dados de vizinhança | Google Places API + Distance Matrix API | Escolas, mercados, trajetos em tempo real |
| Backend | FastAPI + WebSocket | API REST + alertas em tempo real |
| Frontend dashboard | Next.js | Dashboard do gestor por cliente |
| Dev tooling | Claude Code + Claude Cowork | Desenvolvimento, iteração e orquestração |
| Infraestrutura | VPS próprio + Docker Compose | Controle total, multi-instância por cliente |

---

## Arquitetura do sistema

### Dois sistemas distintos

**Sistema A — Time de agentes (construtores)**
Roda internamente. Responsável por configurar, testar e fazer deploy de uma nova instância do produto para cada cliente contratado.

**Sistema B — Produto (consultor de luxo)**
Roda em produção por cliente. É o que o lead final interage via WhatsApp.

---

## FASE 1: Time de 10 agentes ✅ COMPLETA

### Agente 1 — Orquestrador master
- **Função:** visão global do pipeline de setup. Não executa — planeja, delega, consolida e decide deploy.
- **LLM:** Claude Sonnet
- **Implementação:** nó raiz do grafo LangGraph com acesso total ao Redis shared state board
- **Inputs:** JSON de briefing do cliente (formulário de onboarding)
- **Outputs:** tasks distribuídas para cada agente + decisão final de deploy/retrabalho
- **Regra:** único agente com autoridade de mover uma task para `status: approved` ou `status: deploy_ready`

### Agente 2 — Arquiteto auditor
- **Função:** questiona o raciocínio por trás de cada entrega. Opera em paralelo ao orquestrador. Tem direito a veto fundamentado mas não executa tarefas.
- **LLM:** Claude Sonnet
- **Prompt structure:** Chain-of-Thought adversarial obrigatório antes de qualquer veredito:
  ```
  argumento_a_favor → argumento_contra → alternativa_mais_simples → reversibilidade → veredicto → justificativa_em_uma_frase
  ```
- **Escopo de auditoria obrigatória:** escolha de ferramenta/API, estrutura de memória do lead, tom e persona, dependências de terceiros, resultado final pré-deploy
- **Fora do escopo:** ajustes de prompt dentro de módulo aprovado, correções de bug, formatação de resposta
- **Output:** `audit_result` com campos: `status` (approved | approved_with_note | vetoed), `justification`, `proposed_alternative`

### Agente 3 — Dev de fluxo
- **Função:** constrói a lógica central do agente consultor para o cliente — grafo de conversação, tools disponíveis, prompts base
- **LLM:** Claude Sonnet
- **Entrega:** arquivo `agents/consultant.py` parametrizado com as variáveis do cliente
- **Responsabilidade:** fluxo de saudação → qualificação → recomendação → objeção → agendamento

### Agente 4 — Dev de personalização
- **Função:** calibra o que torna o produto "dessa imobiliária" — tom, nome do consultor virtual, voz TTS, linguagem proibida, regras específicas
- **LLM:** Claude Haiku
- **Entrega:** arquivo `prompts/clients/{client_id}/persona.yaml` + configuração ElevenLabs voice_id
- **Inputs:** briefing de tom, nome da imobiliária, exemplos de comunicação aprovados pelo cliente

### Agente 5 — Agente de ingestão
- **Função:** processa o portfólio de imóveis e regras de negócio do cliente → gera embeddings → salva no Supabase pgvector com namespace isolado
- **LLM:** Claude Haiku (para extração estruturada)
- **Aceita:** PDF, JSON, planilha Excel/CSV
- **Entrega:** namespace `{client_id}` populado no pgvector + relatório de cobertura (quantos imóveis indexados, campos faltantes)

### Agente 6 — Agente de contexto
- **Função:** configura e valida as tools de dados externos para a localização do cliente
- **LLM:** Claude Haiku
- **Tools que configura e testa:**
  - `buscar_vizinhanca(lat, lng, tipo)` → Google Places API
  - `calcular_trajeto(origem, destino, modo)` → Google Distance Matrix API
- **Entrega:** validação de que as tools retornam dados corretos para o endereço do portfólio do cliente

### Agente 7 — Agente de memória
- **Função:** define o schema de armazenamento do lead para esse cliente + conecta com o CRM via webhook
- **LLM:** Claude Haiku
- **Entrega:** schema de lead no Supabase + webhook configurado para o CRM do cliente + lógica de score de intenção
- **Score de intenção:** soma ponderada de sinais (pergunta específica = +3, foto solicitada = +2, horário de visita mencionado = +4, etc.)

### Agente 8 — QA de jornadas
- **Função:** simula conversas reais e avalia se o agente consultor responde corretamente
- **LLM:** Claude Sonnet (precisa raciocinar sobre qualidade)
- **Biblioteca de jornadas padrão (20 mínimo):**
  - Comprador qualificado com família → pergunta sobre escola
  - Investidor sem interesse em visita → quer rentabilidade
  - Inquilino sem fiador → pergunta sobre garantias
  - Lead VIP de lançamento → quer posição na fila
  - Lead frio reativado após 30 dias
  - Cliente ligando às 23h → áudio no WhatsApp
  - Lead agressivo/impaciente
  - Cliente falando português com erros → não pode corrigir
  - Pergunta fora do escopo (política, religião)
  - Solicitação de desconto abusivo
- **Critério de aprovação:** ≥85% das jornadas aprovadas antes de liberar para gate de auditoria
- **Output:** relatório com % aprovado, falhas detalhadas, sugestão de correção por jornada

### Agente 9 — QA de integração
- **Função:** valida tecnicamente cada integração ativa
- **LLM:** Claude Haiku
- **Checklist obrigatório:**
  - Latência de resposta < 8 segundos end-to-end
  - Webhook do CRM recebendo e confirmando
  - Geração de áudio ElevenLabs dentro do tempo
  - Envio de mensagem no WhatsApp funcionando (texto e PTT de áudio)
  - Google Places retornando dados para o endereço do portfólio
  - Supabase pgvector retornando imóveis relevantes para query de teste
- **Output:** relatório técnico com status por integração + latências medidas

### Agente 10 — Monitor de produção
- **Função:** único agente que continua rodando após o deploy. Detecta anomalias e alerta antes que o cliente perceba.
- **LLM:** Claude Haiku
- **Monitora:** taxa de erro de API, latência acima do threshold, respostas fora do padrão de qualidade, drift de comportamento
- **Thresholds:** resposta > 8s = alerta warning, taxa de erro > 2% = alerta crítico, 3 falhas consecutivas = alerta emergencial
- **Canal de alerta:** WhatsApp do operador (via número dedicado) ou webhook Slack

---

## Shared State Board (Redis)

Todo estado do pipeline de setup trafega via Redis. Schema obrigatório de cada mensagem:

```python
{
    "task_id": str,           # UUID único por tarefa
    "client_id": str,         # ID do cliente sendo configurado
    "agent_from": str,        # nome do agente que publicou
    "agent_to": str,          # nome do agente destinatário (ou "orchestrator")
    "status": str,            # pending | in_progress | blocked | done | vetoed | approved | deploy_ready
    "payload": dict,          # entregável ou dados da tarefa
    "audit_result": dict,     # preenchido pelo auditor após review
    "requires_review": bool,  # se true, auditor é acionado antes de avançar
    "error": str | None,      # descrição do erro se status == blocked
    "timestamp": str,         # ISO 8601
    "iteration": int          # número de iterações para detectar loops
}
```

**Regras do shared state:**
- Orquestrador é o único que escreve `status: approved` ou `status: deploy_ready`
- Auditor escreve apenas no campo `audit_result`
- Agentes de execução escrevem apenas no seu próprio `payload`
- Nenhum agente lê o state de outro agente diretamente — tudo via Redis pub/sub
- Se `iteration > 3` na mesma task, orquestrador escala para revisão humana

---

## Estrutura de pastas

```
/imovel-ai
  /agents
    orchestrator.py         # Agente 1 — grafo LangGraph master
    auditor.py              # Agente 2 — arquiteto auditor CoT adversarial
    dev_flow.py             # Agente 3 — lógica do consultor
    dev_persona.py          # Agente 4 — personalização por cliente
    ingestion.py            # Agente 5 — ingestão de portfólio
    context.py              # Agente 6 — Google Maps tools
    memory.py               # Agente 7 — schema de lead + CRM
    qa_journeys.py          # Agente 8 — simulação de jornadas
    qa_integration.py       # Agente 9 — validação técnica
    monitor.py              # Agente 10 — produção
  /tools
    places_api.py           # Google Places API wrapper
    distance_api.py         # Distance Matrix API wrapper
    tts.py                  # ElevenLabs wrapper
    whatsapp.py             # WhatsApp Business API wrapper (360dialog) — único canal
    crm_webhook.py          # Bridge CRM: roteia para CRMRouter (novo) ou POST genérico (legado)
    embeddings.py           # Geração de embeddings (text-embedding-3-small)
    /crm                    # ✅ Camada de integração CRM — 6 providers implementados
      __init__.py           # Exports públicos do pacote
      base.py               # CRMAdapter (abstract), LeadPayload, CRMResult, enums
      router.py             # CRMRouter: factory + dispatcher + retry (2x em 5xx)
      c2s.py                # Contact2Sale adapter (prospect atual usa este CRM)
      cvcrm.py              # CV CRM adapter (dominante no mercado imobiliário BR)
      pipedrive.py          # Pipedrive adapter (Person + Deal, owner_id)
      rdstation.py          # RD Station CRM adapter (deals + activities)
      jetimob.py            # Jetimob adapter (vertical imobiliário)
      kenlo.py              # Kenlo adapter (ex-InGaia, buyer/tenant/investor)
  /state
    schema.py               # Definição e validação do shared state board
    board.py                # Leitura/escrita no Redis com locks
    pubsub.py               # Pub/sub entre agentes via Redis
  /prompts
    /base                   # Prompts parametrizáveis base
      consultant_base.md    # Prompt base do consultor de luxo
      auditor.md            # Prompt do arquiteto auditor
      orchestrator.md       # Prompt do orquestrador master
    /clients                # Overrides por cliente (gerados pelos agentes)
      /{client_id}
        persona.yaml        # Tom, nome, voz, regras específicas
        rules.md            # Regras de negócio do cliente
  /clients
    /{client_id}            # Namespace isolado por cliente
      config.json           # Configuração completa do cliente
      onboarding.json       # Briefing original do formulário
  /tests
    /journeys               # Biblioteca de jornadas para QA
      journeys_base.json    # 20 jornadas padrão
    test_tools.py           # Testes unitários das tools
    test_state.py           # Testes do shared state board
    test_crm_adapters.py    # ✅ 66 testes — CRMRouter, 6 adapters, bridge, retry, LeadPayload
  /dashboard
    /backend
      main.py               # FastAPI entry point
      routes/               # Endpoints REST
      websocket.py          # Alertas em tempo real
    /frontend               # Next.js dashboard do gestor
  /docker
    docker-compose.yml      # Redis + Supabase local + serviços
    Dockerfile              # Imagem do sistema de agentes
  _prompts_build/           # ⚠️ FONTE DA VERDADE dos prompts — copiado para /app/prompts/base/ no Docker build
    consultant_base.md      # Prompt base do consultor — editar AQUI, nunca direto no container
    auditor.md
    orchestrator.md
  setup_pipeline.py         # Script principal: onboarding → setup → deploy
  main.py                   # Entry point do sistema
  requirements.txt
  .env.example              # Variáveis de ambiente necessárias
  CLAUDE.md                 # Este arquivo
```

---

## Variáveis de ambiente necessárias

```bash
# Anthropic
ANTHROPIC_API_KEY=

# Google APIs
GOOGLE_PLACES_API_KEY=
GOOGLE_DISTANCE_MATRIX_API_KEY=

# Supabase
SUPABASE_URL=
SUPABASE_KEY=
SUPABASE_DB_URL=

# Redis
REDIS_URL=redis://localhost:6379

# ElevenLabs
ELEVENLABS_API_KEY=
ELEVENLABS_DEFAULT_VOICE_ID=

# WhatsApp Business API (único canal)
WHATSAPP_BSP_API_KEY=
WHATSAPP_BSP_URL=
WHATSAPP_OPERATOR_NUMBER=

# Alertas internos (Slack webhook ou WhatsApp dedicado)
ALERT_SLACK_WEBHOOK=
```

---

## Pipeline de setup de novo cliente

Quando um cliente é contratado, o fluxo é:

```
1. Preenche formulário de onboarding → gera clients/{client_id}/onboarding.json
2. setup_pipeline.py aciona o orquestrador master com o onboarding.json
3. Orquestrador delega em paralelo:
   - Agente 5 (ingestão) → processa portfólio
   - Agente 4 (persona) → configura tom e voz
   - Agente 7 (memória) → configura schema de lead + CRM
   - Agente 6 (contexto) → valida tools de Maps para a região
4. Agente 3 (dev de fluxo) → constrói o consultor com base nos outputs anteriores
5. Agente 2 (auditor) → audita todas as decisões de arquitetura
6. Agente 8 (QA jornadas) → simula 20+ conversas
7. Agente 9 (QA integração) → valida todas as integrações técnicas
8. Orquestrador master → gate final: aprovado ou retrabalho com diagnóstico
9. Deploy aprovado → Agente 10 (monitor) ativado para esse client_id
```

Meta de tempo total: < 4 horas de execução dos agentes para setup completo.

---

## Produto — consultor digital de luxo (FASE 2) 🔜 PRÓXIMA

> Esta seção descreve o que os agentes vão construir para cada cliente. Não construir manualmente.

**Pré-requisitos para iniciar a FASE 2:**
- [x] Supabase: criar projeto, rodar migrations (schema leads + pgvector) ✅
- [x] ElevenLabs: integrado com voz Sarah multilingual — Yasmin (BR nativo) requer Starter $5/mês ✅
- [ ] WhatsApp Business API: credenciais 360dialog (BSP escolhido — ver decisão abaixo) ⏳ **BLOQUEIO**
- [x] Primeiro `onboarding.json` de cliente real (demo_imobiliaria_vendas) ✅

### Identidade
- Nome configurável por cliente (ex: "Julia", "Marco", "Sofia")
- Voz TTS via ElevenLabs — aprovada pelo cliente antes do go-live
- Tom: sofisticado, preciso, discreto. Nunca genérico, nunca apressado.
- Nunca revela que é IA a menos que diretamente perguntado

### Fluxo de conversa
```
Saudação calibrada (horário, canal, origem do lead)
→ Qualificação conversacional (budget, prazo, perfil, uso do imóvel)
→ Recomendação de imóveis (busca no pgvector do cliente)
→ Respostas contextuais (vizinhança via Google Maps em tempo real)
→ Geração de áudio para respostas-chave (ElevenLabs)
→ Score de intenção atualizado a cada turno
→ Lead quente: agenda visita + notifica corretor
→ Lead frio: nutrição automatizada + follow-up programado
→ Sincronização com CRM do cliente
```

### Tools disponíveis para o consultor
- `buscar_imoveis(query, filtros)` → pgvector do cliente
- `buscar_vizinhanca(lat, lng, tipo)` → Google Places
- `calcular_trajeto(origem, destino, modo)` → Google Distance Matrix
- `gerar_audio(texto, voice_id)` → ElevenLabs → enviado como PTT no WhatsApp
- `atualizar_lead(lead_id, dados)` → Supabase + CRM webhook
- `notificar_corretor(lead_id, urgencia, resumo)` → WhatsApp do corretor (número cadastrado no onboarding)
- `agendar_visita(lead_id, slot)` → integração com calendário

### Resposta de vizinhança (exemplo)
Quando o lead pergunta "tem escola boa perto?", o consultor:
1. Detecta a intenção → aciona `buscar_vizinhanca(lat, lng, "school")`
2. Aciona `calcular_trajeto(imovel, escola_mais_proxima, "driving")`
3. Sintetiza: "Aqui do [Nome do Empreendimento], o Colégio X fica a 6 minutos de carro e está entre os 5 melhores avaliados da cidade. O Pão de Açúcar Premium fica a 3 minutos. Posso te enviar isso em áudio se preferir."
4. Se lead confirmar → `gerar_audio(resposta)` → envia como PTT

---

## Regras de desenvolvimento

### Para Claude Code e Claude Cowork
- Sempre ler o CLAUDE.md antes de iniciar qualquer sessão de desenvolvimento
- Nunca construir o produto (FASE 2) antes de todos os 9 agentes da FASE 1 estarem funcionando
- Nunca duplicar responsabilidade entre agentes — cada um tem escopo único
- Todo novo agente deve escrever e ler o shared state board via `board.py`, nunca diretamente no Redis
- Toda tool externa deve ter timeout configurado e fallback definido
- Nenhum dado de cliente deve vazar para namespace de outro cliente (isolamento por `client_id`)
- Iteração `> 3` na mesma task = escala para revisão humana, não tenta resolver automaticamente

### Ordem de implementação dentro da FASE 1 ✅ CONCLUÍDA
```
1. schema.py + board.py ✅
2. orchestrator.py ✅
3. ingestion.py + context.py ✅
4. auditor.py ✅
5. dev_flow.py + dev_persona.py ✅
6. memory.py ✅
7. qa_journeys.py + qa_integration.py ✅
8. monitor.py ✅
9. setup_pipeline.py ✅
10. Teste interno completo — deploy_ready, QA 90% (com --skip qa_integration) ✅
```

**Status de QA (demo_imobiliaria_vendas):**
- Score: **90% — deploy_ready** (9/10 jornadas aprovadas)
- j06 (lead às 23h → áudio PTT): falha conhecida, critério de geração de áudio em runtime — não bloqueante enquanto ElevenLabs não estiver integrada via credencial real
- Tempo médio de pipeline: ~150 segundos (com --skip qa_integration)

**Status das integrações em produção (VPS):**
| Integração | Status | Observação |
|-----------|--------|-----------|
| Anthropic API | ✅ configurada | Claude Sonnet + Haiku ativos |
| Google Places API | ✅ configurada | Dados reais de vizinhança funcionando |
| Google Distance Matrix | ✅ configurada | Mesma chave do Places |
| Supabase pgvector | ✅ configurada | 18 imóveis indexados no demo; leads + conversas persistindo |
| Redis | ✅ rodando | 127.0.0.1:6379 — hot storage + locks por sender |
| OpenAI Whisper | ✅ configurada | Transcrição de áudio PTT recebido |
| ElevenLabs TTS | ✅ configurada | Geração de áudio PTT — voz Sarah (multilingual). Voz Yasmin (BR nativo) requer plano Starter ($5/mês) |
| Evolution API (demo) | ✅ ativa | Instância `devlabz` — apenas para demo, não usar em produção |
| WhatsApp BSP (360dialog) | ⏳ pendente | **Bloqueio para cliente real** — substituir Evolution API |

**Webhook demo (`whatsapp_webhook.py`) — capacidades ativas:**
- Recebe texto, áudio PTT (transcreve via Whisper), imagem (descreve via Claude Vision), documento
- Responde em texto com Sofia (claude-sonnet-4-6)
- Envia fotos do imóvel via tag `[FOTOS:ID]` com dedup por conversa
- Gera e envia áudio PTT de resposta via tag `[AUDIO]` + ElevenLabs
- Persiste leads + histórico de conversas no Supabase em tempo real
- Lock por sender — sem race condition em mensagens simultâneas
- Data real injetada no system prompt por chamada — agendamentos corretos
- **Score de intenção:** calculado a cada mensagem do lead com 7 categorias de sinais (horario_visita=4, dados_pessoais=3, pergunta_especifica=3, interesse_imovel=3, foto_solicitada=2, financiamento=2, pergunta_valor=2). Acumulado no Redis (7 dias) + persistido em `leads.intention_score` + `leads.score_breakdown` no Supabase
- **Notificação ao corretor:** quando score ≥ threshold (padrão 8), envia WhatsApp ao corretor com briefing estratégico gerado via Claude Haiku — inclui: Perfil, Busca, Budget, Prazo, Sinais quentes, Objeções, Próximo passo. Cooldown configurável (padrão 24h). Config: `CORRETOR_NUMBER`, `CORRETOR_SCORE_THRESHOLD`, `CORRETOR_COOLDOWN_HOURS`
- **Detecção de descarte:** regex sobre mensagem do lead detecta 5 sinais (nao_e_momento, ja_comprou, sem_budget, desistencia) → marca `descartado=true`, `descartado_em`, `motivo_descarte` no Supabase
- **Detecção de confirmação de visita:** regex sobre resposta da Sofia detecta confirmação de agendamento → seta `visita_agendada=true`, `visita_confirmada_at` no Supabase
- **Endpoint `/new-property`:** recebe JSON de novo imóvel via POST, faz match semântico com leads quentes/mornos, envia mensagem personalizada via Claude Haiku para matches relevantes

**Follow-up engine (`followup_engine.py`) — script standalone (systemd timer hourly):**
- **Cenário 1 — Silêncio 24h:** mensagem de reengajamento após 24h sem resposta (máx. 1 por lead)
- **Cenário 2 — Silêncio 48h:** segunda tentativa com ângulo diferente após 48h (só se 24h já enviado)
- **Cenário 3 — Silêncio 7d:** reativação gentil após 7 dias, pergunta se situação mudou
- **Cenário 4 — Pós-visita:** mensagem de follow-up 24h após visita confirmada
- **Cenário 5 — Novo imóvel:** match e mensagem personalizada para leads com perfil compatível
- **Cenário 6 — Reativação CRM:** leads inativos >30 dias recebem mensagem com match atual do portfólio
- **Cenário 7 — Nutrição de descartados:** sequência linear 30d → 60d → 90d com 3 ângulos distintos (oportunidade, ângulo alternativo, porta aberta). Não pula etapas — 60d só dispara se 30d já foi enviado.
- **Idempotência:** tabela `followup_events` + TTL por tipo de evento — sem duplicatas
- **Modos de execução:** `--dry-run`, `--new-property '{"id":"AV010",...}'`, `--crm`, `--discard`
- **Infraestrutura:** `/etc/systemd/system/imob-followup.service` + `.timer` (OnCalendar=hourly, RandomizedDelaySec=300)

**Estratégia multi-tenant (decisão arquitetural — Março 2026):**
- Não é necessário uma VPS por cliente
- Cada cliente vira um Docker service no Swarm com env vars isoladas (portfólio, persona, voz, número WhatsApp)
- Supabase e Redis já isolam por `client_id` — sem vazamento entre clientes
- Escalar VPS verticalmente (RAM/CPU) antes de adicionar hardware — suporta ~8–10 clientes ativos com folga
- Custo por cliente: 360dialog ~R$290/mês + taxa Meta por conversa (~R$0,40)

### Decisões técnicas tomadas durante a implementação da FASE 1 e FASE 2
- **LLM do consultor:** `claude-sonnet-4-6` — não Haiku. O consultor do produto (QA jornadas + futuro FASE 2) usa Sonnet para qualidade de resposta. Haiku fica restrito ao evaluator e agentes de execução simples.
- **LLM evaluator:** usa assistant prefill `{"passou":` para forçar JSON válido — elimina falhas de parse
- **Prompts base:** baked no Docker via `_prompts_build/` (workaround para FUSE filesystem do Cowork). Fonte da verdade é `_prompts_build/consultant_base.md`, que é copiado para `/app/prompts/base/` no build.
- **Redis default:** sempre `127.0.0.1:6379`, nunca `localhost` (IPv6 quebra em Docker com ip6tables ativo)
- **Docker entry point:** `CMD`, não `ENTRYPOINT` — permite override via `command:` no Docker Swarm stack
- **Modelos:** orquestrador/auditor/consultor = `claude-sonnet-4-6`, agentes simples/evaluator = `claude-haiku-4-5-20251001`
- **qa_integration skip:** aceitável em deploy com credenciais reais pendentes; gate obrigatório antes de cliente real
- **portfolio_path é aninhado:** no `onboarding.json`, o caminho do CSV está em `onboarding["portfolio"]["portfolio_path"]`, não em `onboarding["portfolio_path"]`. O `setup_pipeline.py` lê com fallback para os dois formatos. Nunca remover esse fallback.
- **Critérios de QA:** devem ser objetivamente verificáveis pelo Haiku evaluator a partir do texto da resposta. Critérios como "não deve inventar X" falham porque o Haiku não tem como verificar a origem dos dados — preferir "deve atribuir fonte X na resposta" ou reduzir severidade para INFORMATIVO.
- **Score de intenção — LLM não avalia:** o score é calculado via regex/heurística no webhook, não pelo LLM. Isso garante baixo custo, baixa latência e sem risco de alucinação no cálculo.
- **Resumo estratégico ao corretor via Haiku:** Claude Haiku gera o briefing da conversa (não Sonnet) — custo ~$0,001 por notificação. Fire-and-forget via `asyncio.create_task` — não bloqueia a resposta ao lead.
- **consultant_base.md — bugs corrigidos (Março 2026):** proibição explícita de dados financeiros inventados (rentabilidade, yield, cap rate); reconhecimento implícito de slot de agendamento ("terça às 10h" = confirmação); coleta de nome em todos os perfis incluindo investidor; "nome e sobrenome" em vez de "nome completo".
- **Detecção de descarte — regex, não LLM:** mesma lógica do score. Baixo custo, sem latência extra, sem risco de alucinação. 5 padrões mapeados → 3 motivos (nao_e_momento, ja_comprou, sem_budget/desistencia).
- **Detecção de visita confirmada — sobre resposta da Sofia, não do lead:** Sofia é quem diz "sua visita está confirmada para..." — regex garante precisão e não gera false positives por ambiguidade do lead.
- **Supabase URL encoding no followup_engine:** timestamps com `+00:00` quebram query params — usar `strftime("%Y-%m-%dT%H:%M:%SZ")` + `urllib.parse.quote()` em todas as queries com filtro de data.
- **Nutrição de descartados — sem LLM para classificar etapa:** progressão 30→60→90 calculada por dias desde descarte + verificação de `followup_events` — determinístico, sem custo adicional de inferência.
- **Haiku não prefixar mensagem de novo imóvel:** prompt deve incluir explicitamente "A resposta deve conter APENAS a mensagem final. Não inclua prefixos como 'Compatibilidade:' ou 'Mensagem:'".
- **CRM: auth CVCRM é `cv-email` + `cv-token` no header, não Bearer** — não confundir com os outros 5 providers que usam Bearer. O `CVCRMAdapter._headers()` nunca deve incluir `Authorization`.
- **CRM: onboarding.json schema v2.0** — seção `crm` agora tem `provider` + `api_token` + mappings. Legado `crm_webhook_url` ainda funciona via bridge. Nunca remover o fallback legado enquanto houver clientes configurados no formato antigo.
- **CRM: retry apenas em 5xx/timeout** — erros 4xx (dados inválidos) não são retentados. O `CRMRouter._with_retry` verifica `status_code >= 500` antes de retentar. Máx 2 retries com delay de 1s.
- **CRM: seller_mapping por telefone do corretor** — `seller_mapping` no onboarding usa o telefone WhatsApp do corretor como chave (formato `5511999990001` sem `+`). Se o telefone não estiver mapeado, `assign_seller` retorna sucesso silencioso — não bloqueia o fluxo.
- **CRM: `external_id` deve ser salvo no Supabase** — após `push_lead_to_crm` retornar `external_id`, salvar em `leads.crm_external_id` para uso em `update_lead_in_crm` e `add_note_to_crm` subsequentes. Sem isso, atualizações são impossíveis. Cache em memória (`_CRM_ID_CACHE`) + Redis (TTL 7d) evitam queries repetidas ao Supabase.
- **CRM wiring no whatsapp_webhook.py — ✅ implementado (Abril 2026):** `_crm_push_new_lead` cria o lead no CRM na primeira mensagem (idempotente via external_id check). `_crm_add_briefing` adiciona nota quando score atinge threshold e corretor é notificado. `_crm_update_lead_status("visita_agendada")` chamado quando Sofia confirma visita. `_crm_update_lead_status("descartado")` chamado quando lead descarta. Todos fire-and-forget via `asyncio.create_task`. Ativação: só executa se `_crm_available = True` (ONBOARDING tem `crm.provider` definido).
- **Multi-corretor routing no calendar (Abril 2026):**  roteia o evento do Google Calendar para o corretor responsável pelo bairro do imóvel. Busca  no onboarding (partial match, case-insensitive). Fallback:  env var ou campo único no . Chamado APÓS  ser resolvido, não antes. Suporta N corretores por cliente sem alteração de env vars.

- **CRM import lazy no webhook:** `from tools.crm_webhook import ...` dentro de try/except — falha silenciosa com WARNING se `tools/` não estiver no path. Nunca quebra o fluxo de atendimento mesmo sem CRM configurado.
- **Nome de lead no briefing do corretor: fallback em histórico** — `_extract_name_from_reply` falhou em casos como "Ótima escolha, Carlos" (variante feminina não coberta). Fix: expandir `_NOT_NAMES` + adicionar `_get_lead_name_from_history()` que varre o histórico completo quando extração da mensagem atual falha. `notify_name = lead_name or _get_lead_name_from_history(full_history)` antes de acionar a notificação.

### Estrutura de dados Supabase — migrations aplicadas
| Migration | Campos adicionados |
|-----------|-------------------|
| `add_intention_score_and_corretor_notified` | `leads.intention_score`, `leads.score_breakdown`, `leads.corretor_notified_at`, `leads.corretor_notified_score` |
| `create_followup_events_table` | tabela `followup_events` (phone, event_type, sent_at, message_preview, lead_name); `leads.visita_agendada`, `leads.visita_confirmada_at` |
| `add_lead_discard_fields` | `leads.descartado`, `leads.descartado_em`, `leads.motivo_descarte`; constraint `followup_events.event_type` expandido para incluir tipos de descarte e pós-visita |
| `add_crm_external_id_and_provider` ✅ Abril 2026 | `leads.crm_external_id` (TEXT), `leads.crm_provider` (TEXT), `leads.crm_synced_at` (TIMESTAMPTZ), `leads.crm_sync_error` (TEXT); índice em `crm_external_id` |

### Integrações CRM — ✅ IMPLEMENTADA (Abril 2026)

Camada completa de adapters tipados em `tools/crm/`. 6 providers prontos para uso em produção.

**Providers implementados:**
| Provider | Arquivo | Auth | Usado por |
|----------|---------|------|-----------|
| C2S (Contact2Sale) | `c2s.py` | Bearer token | Prospect atual (maior house SP) |
| CV CRM | `cvcrm.py` | `cv-email` + `cv-token` header | Dominante no mercado imobiliário BR |
| Pipedrive | `pipedrive.py` | `api_token` query param | Imobiliárias menores e médias |
| RD Station CRM | `rdstation.py` | `token` query param | Quem usa RD Marketing |
| Jetimob | `jetimob.py` | Bearer token | Plataforma vertical (site + CRM) |
| Kenlo (ex-InGaia) | `kenlo.py` | Bearer token | Forte em locação + venda simultânea |

**Arquitetura da camada:**
- `base.py` — `CRMAdapter` (ABC), `LeadPayload` (canônico), `CRMResult`, `LeadStatus`, `LeadProfile`, `LeadSource`
- `router.py` — `CRMRouter`: factory por `provider` string + dispatcher + retry automático (2x em 5xx, sem retry em 4xx)
- `crm_webhook.py` — bridge: detecta config nova (CRMRouter) vs legado (`crm_webhook_url`). Zero breaking change nos agentes existentes.

**Como ativar CRM num cliente novo (onboarding.json v2.0):**
```json
"crm": {
  "provider": "c2s",
  "api_token": "TOKEN_DO_CRM",
  "queue_id": "fila_jardins",
  "seller_mapping": { "5511999990001": "seller_id_no_crm" },
  "status_mapping": { "visita_agendada": "visit_scheduled" },
  "source_mapping": { "WhatsApp": "WhatsApp" }
}
```

**Uso no código:**
```python
from tools.crm_webhook import push_lead_to_crm, add_note_to_crm, update_status_in_crm

result = await push_lead_to_crm(onboarding, lead_payload, client_id="demo_01")
# result = {"success": True, "external_id": "crm_lead_123", "provider": "C2S"}
```

**Providers pendentes (Tier 2):** HubSpot, Salesforce, Bitrix24 — implementar quando primeiro cliente real confirmar uso.

**Webhook de entrada (CRM → Sofia):** endpoint no webhook para receber eventos do CRM (novo lead, atualização de status) — base para cenário 6 (reativação CRM). Pendente de implementação.

### Testes mínimos antes de avançar de agente
- Cada agente deve ter ao menos 3 testes unitários antes de ser integrado ao grafo
- O shared state board deve ter testes de concorrência (dois agentes escrevendo simultaneamente)
- QA de jornadas deve atingir ≥85% de aprovação antes de qualquer gate de deploy

---

## Decisões de arquitetura já tomadas (não reabrir sem auditoria)

| Decisão | Motivo | Reversibilidade |
|---------|--------|----------------|
| LangGraph sobre CrewAI | Controle fino de estado, não delega decisões implicitamente | Custosa |
| Redis para shared state | Acesso em microssegundos, pub/sub nativo, sem polling | Moderada |
| Supabase pgvector sobre Pinecone | Self-hosted possível, SQL familiar, sem vendor lock | Moderada |
| ElevenLabs sobre OpenAI TTS | Qualidade de voz superior, voz por cliente configurável | Fácil |
| WhatsApp Business API oficial (único canal) | Zero risco de ban, requisito inegociável para produto pago e premium | N/A |
| Evolution API | **DESCARTADO** — alto risco de ban, inaceitável para produto pago | — |
| 360dialog sobre Gupshup | Acesso direto à API Meta sem camada proprietária; zero lock-in de BSP; gestão multi-conta via Partner API (1 subaccount por cliente); sem markup em mensagens | Fácil |
| CRMAdapter pattern sobre webhook genérico | 6 providers com particularidades irredutíveis (auth dual CVCRM, Person+Deal Pipedrive, stages RD Station) — adapter tipado elimina if-chains e torna cada provider testável em isolamento | Fácil |
| CRMRouter como único ponto de entrada | Retry, logging centralizado e resolução de provider em um lugar só — callers não conhecem o provider | Fácil |
| Backward compat via crm_webhook.py bridge | Agentes existentes (memory.py, qa_integration.py) usam `crm_webhook_url` legado — bridge detecta automaticamente qual path usar sem alterar código dos agentes | N/A |

**Modelo de contratação 360dialog:**
- Até ~6 clientes: plano Regular direto — €49/mês por número (~R$ 290)
- A partir de 7 clientes: Partner Growth — €500/mês para até 20 clientes (mais barato por cliente)
- Mais taxas Meta por conversa (~$0,083 USD/conversa business-initiated, Brasil)
- Gupshup descartado: features de chatbot builder são redundantes com o orquestrador LangGraph próprio

---

## Contexto de negócio

- **Público-alvo:** imobiliárias brasileiras de alto padrão (imóveis R$ 2M+)
- **Dores reais:** leads de alto valor sem atendimento imediato fora do horário comercial, corretores sobrecarregados com leads não qualificados, nenhum produto de IA com tom adequado ao mercado premium
- **Diferencial central:** único produto de IA treinado para o cliente de alto padrão brasileiro — tom, dados reais de vizinhança em tempo real, áudio natural, e arquiteto auditor garantindo qualidade de entrega
- **O que NÃO somos:** não somos chatbot genérico, não somos concorrente da Lais no mercado de massa
- **Pricing:** R$ 15–25k setup + R$ 3,5–5k/mês + R$ 8–15k por evento de lançamento

---

## ICP — Persona primária: Dono da imobiliária / Dono da construtora

> **Regra de ouro:** toda feature nova deve primeiro responder à pergunta "o que o dono enxerga ou ganha com isso?". Corretores são usuários do sistema, não o ICP.

**Quem é o ICP:**
- Dono de imobiliária de alto padrão (3–50 corretores, carteira R$ 2M+)
- Dono / sócio de construtora de médio/alto padrão com lançamentos
- Diretor comercial com autonomia de compra de tecnologia

**O que o dono precisa ver/ter:**
- **Relatório semanal automático** no WhatsApp com: leads atendidos, visitas, pipeline estimado em R$, top objeção da semana — sem precisar entrar em dashboard
- **Dashboard corporativo** com KPIs executivos: pipeline ativo em R$, taxa de conversão Sofia → visita, leads por origem (portal, WhatsApp orgânico, indicação), satisfação pós-visita
- **Exportação** de todos os relatórios em PDF e CSV para apresentar em reuniões de sócio ou board
- **Inteligência de mercado** gerada pela base de conversas: objeções recorrentes, perfil dos leads que convertem, bairros de maior demanda
- **Visibilidade sobre o Modo Lançamento** com painel especial e relatório diário durante eventos

**O que o corretor precisa ver/ter (secundário):**
- Notificação WhatsApp quando lead atinge score threshold
- Briefing estratégico da conversa antes de abordar o lead
- Dossiê de Caviar quando visita é confirmada
- Google Calendar atualizado automaticamente

**Princípio de design do dashboard:**
- Tier 1 (dono): pipeline em R$, KPIs de negócio, relatórios exportáveis, análise de objeções, histórico de relatórios semanais
- Tier 2 (corretor): fila de leads quentes, histórico de conversa, status da visita
- Nunca misturar os dois tiers na mesma view — o dono não quer ver transcrição de mensagem, quer ver número

---

## FASE 3 — Serviço de luxo 🔵 PLANEJADA

> Esta seção define as features de diferenciação premium que serão construídas após a FASE 2 estar estável em produção com ao menos 2 clientes reais. Não implementar antes disso.

São os 4 pontos cegos que separam um robô premium de um serviço de luxo — e que ancoram o posicionamento de R$25k de setup na conversa comercial.

### Feature 1 — Lifestyle Mapping
- **O que é:** substituir os POIs genéricos do Google Places (escola, mercado, farmácia) por um mapeamento do ecossistema de luxo da região: heliportos, marinas, clubes de golfe, colégios de elite, restaurantes estrelados, academias exclusivas.
- **Por que importa:** o comprador de R$5M+ não quer saber se tem "escola boa perto" — quer saber se tem o Colégio X a 6 minutos e o Iate Clube a 15. O consultor precisa falar essa língua.
- **Como implementar:** curar lista de categorias de luxo por cidade → criar camada de enriquecimento sobre `buscar_vizinhanca()` com filtro por rating + segmento premium → treinamento do prompt para referenciar por nome, não por categoria genérica.
- **Dependências:** Google Places API (já integrada) + curadoria manual por região.

### Feature 2 — Follow-up em Áudio
- **O que é:** o follow-up engine atual envia texto. Para leads VIP (score alto + silêncio >48h), enviar mensagem de voz personalizada gerada via ElevenLabs — com o nome do lead, referência ao imóvel específico e tom humano.
- **Por que importa:** texto reengaja 8%. Áudio com voz natural e contexto pessoal reengaja muito mais — especialmente com o perfil de comprador que ignora texto mas ouve mensagem de voz.
- **Como implementar:** cenário 8 no `followup_engine.py` → detectar leads VIP silenciosos → `gerar_audio(texto_personalizado, voice_id)` → enviar como PTT via WhatsApp.
- **Dependências:** ElevenLabs (já integrada), lógica de score (já existe), follow-up engine (já existe).

### Feature 3 — Dossiê de Caviar
- **O que é:** quando Sofia confirma uma visita, gerar automaticamente um PDF/HTML de briefing do lead para o corretor — perfil psicológico, hot buttons mapeados ao longo da conversa, objeções levantadas, imóveis de interesse, orçamento declarado e prazo. Formato: consultor de private banking, não formulário de CRM.
- **Por que importa:** o corretor chega à visita sabendo o que o lead valoriza, o que o preocupa e o que evitar. Aumenta taxa de conversão e justifica o CRM como mais do que captura de dados.
- **Como implementar:** ao detectar `visita_agendada=true` → acionar Claude Sonnet com histórico completo da conversa → gerar dossier estruturado em Markdown → converter para PDF via `reportlab` ou HTML estilizado → enviar ao corretor via WhatsApp ou e-mail.
- **Schema do dossiê:** perfil (nome, contato, canal), busca (tipologia, região, budget, prazo), hot buttons (lista priorizada), objeções (e como foram tratadas), próximo passo sugerido.

### Feature 4 — Transição Concierge
- **O que é:** comando `/assumir` no WhatsApp do corretor transfere o fio da conversa com o lead para o corretor humano — sem quebrar o contexto, sem o lead perceber a troca. Sofia reconhece que o corretor assumiu e para de responder automaticamente para aquele lead.
- **Por que importa:** leads VIP ou conversas complexas precisam de toque humano no momento certo. Hoje não existe transição elegante — o corretor entra "do nada" ou a Sofia continua respondendo em paralelo, criando ruído.
- **Como implementar:** webhook escuta mensagens do número do corretor → detecta `/assumir [número_do_lead]` → seta flag `human_takeover=true` no Redis + Supabase para aquele `sender` → Sofia para de responder automaticamente → envia ao lead uma mensagem de transição neutra ("Vou te conectar com nossa equipe para os próximos detalhes") → corretor recebe contexto completo da conversa no terminal ou via dossiê.
- **Reversão:** `/sofia [número_do_lead]` devolve o controle à Sofia com o contexto atualizado.
- **Dependências:** tabela `leads.human_takeover` (nova migration), lógica de routing no `whatsapp_webhook.py`.

### Decisões de arquitetura da Fase 3 (não implementar até fase 2 estável)
| Decisão | Motivo |
|---------|--------|
| Dossiê via Claude Sonnet, não Haiku | Requer síntese psicológica e narrativa — custo justificado pelo valor entregue ao corretor |
| PDF/HTML gerado no VPS, não SaaS externo | Controle de layout, dados sensíveis do lead não saem da infraestrutura |
| `/assumir` via número do corretor, não app separado | Zero fricção — o corretor já usa WhatsApp, não precisa de nova ferramenta |
| Lifestyle Mapping como curadoria + Places, não base própria | Escala mais rápido; curadoria manual por cidade pode ser terceirizada por cliente |

---

## Pipeline Runner autônomo ✅ (Abril 2026)

`pipeline_runner.py` — FastAPI na porta 8003, systemd `imob-runner.service`. Permite disparar o pipeline de setup sem precisar ficar na sessão do terminal. Notifica o operador via WhatsApp ao terminar.

**Endpoints:**
```
POST /pipeline/start          → dispara pipeline (onboarding.json já em disco)
POST /pipeline/start-json     → dispara pipeline com onboarding inline no body
GET  /pipeline/status/{id}    → status atual do job (queued | running | done | failed | human_review)
GET  /pipeline/jobs           → lista todos os jobs
GET  /health                  → liveness check
```

**Como disparar um novo cliente:**
```bash
# Via curl (no VPS ou de qualquer máquina com acesso à rede)
curl -X POST http://76.13.165.64:8003/pipeline/start \
     -H "Content-Type: application/json" \
     -d '{"client_id": "alfa_imoveis"}'

# Retorna imediatamente. Pipeline roda em background.
# WhatsApp enviado ao operador (5511973722075) ao terminar.
```

**Notificações enviadas ao operador:**
- 🚀 Pipeline iniciado
- ✅ deploy_ready — com tempo total
- ⚠️ human_review — com agentes bloqueados e comando de reset
- ❌ falha — com erros resumidos

**Variáveis de ambiente relevantes:**
- `OPERATOR_NUMBER` — número WhatsApp do operador (padrão: 5511973722075)
- `RUNNER_PORT` — porta do serviço (padrão: 8003)
- `RUNNER_SECRET` — se definido, endpoint exige `{"secret": "..."}` no body

**Decisão arquitetural:** runner usa `BackgroundTasks` do FastAPI — o pipeline roda na mesma thread de eventos async que o webhook, sem precisar de processo separado. Isso evita overhead de subprocess e aproveita o mesmo venv e credenciais. Se o pipeline crescer para usar multiprocessing, migrar para Celery + Redis queue (infra já existe).

---

## Nightly Squad — Time autônomo de desenvolvimento ✅ (Abril 2026)

`nightly_squad.py` — time de agentes LangGraph que acorda às 02:00, lê o backlog, escreve código, testa no sandbox e abre PRs no GitHub. Você acorda com WhatsApp e aprova ou rejeita. Nenhum código é mergeado automaticamente.

**Agentes do time:**
| Agente | Função | LLM |
|--------|--------|-----|
| PO Agent | Lê backlog + histórico Redis → seleciona 1-3 tasks | Haiku |
| Tech Lead | Projeta solução técnica por task | Sonnet |
| Dev Agent | Escreve código + loop de autocorreção (max 3x) | Sonnet |
| QA Agent | Roda suite completa de testes no sandbox | Sonnet |
| Auditor | CoT adversarial sobre as mudanças | Sonnet |
| Deploy Agent | Cria branch + commit + abre PR (nunca faz merge) | — |
| Briefing Agent | WhatsApp às 07:00 com resumo da noite | Haiku |

**Ferramentas implementadas:**
- `tools/github_controller.py` — lê repo, cria branches, commita, abre PRs via GitHub API
- `tools/sandbox_executor.py` — executa pytest em tmpdir isolado, captura stdout/stderr para autocorreção
- `state/intelligence.py` — histórico de execuções no Redis, priorização dinâmica por score

**Backlog:**
- `backlog/tasks.json` — fonte da verdade das tasks. 11 tasks mapeadas (v1.1).
- Para adicionar task: editar `backlog/tasks.json` com `id`, `title`, `description`, `acceptance_criteria`, `priority`, `context_files`.
- `priority: "critical"` = score 10 no PO Agent — entra antes de qualquer task numérica.

| id | título | prioridade | ICP |
|----|--------|-----------|-----|
| `off-market-engine` | Motor de Pocket Listings e Matchmaking Sigiloso | critical | Dono |
| `sellers-ai-dossier` | Dossiê de Captação e Posicionamento de Mercado | critical | Dono |
| `permuta-triage` | Fluxo de Qualificação Complexa de Permutas | critical | Dono/Corretor |
| `liquidity-yield-dossier` | Expansão Hard Skills do Dossiê de Caviar | critical | Dono |
| `weekly-owner-report` | Relatório semanal automático para o dono (WhatsApp + Dashboard) | critical | **Dono** |
| `portal-lead-capture` | Captura automática de leads de portais (ZAP, VivaReal, OLX) | 9 | **Dono** |
| `pipeline-roi-calc` | Cálculo de pipeline em R$ e ROI estimado para o dono | 9 | **Dono** |
| `multi-corretor-routing` | Multi-corretor routing por bairro | 9 | Corretor |
| `human-takeover-concierge` | Transição Concierge — /assumir e /sofia | 8 | Corretor |
| `launch-mode-sku` | Modo Lançamento — SKU separado (R$ 8–15k/evento) | 8 | **Dono** |
| `google-calendar-integration` | Integração Google Calendar do corretor | 8 | Corretor |
| `followup-audio-vip` | Follow-up em áudio para leads VIP | 7 | Lead |
| `dossie-de-caviar` | Dossiê de Caviar — PDF de briefing para o corretor | 7 | Corretor |
| `objection-analysis-report` | Análise de objeções recorrentes — inteligência de mercado | 7 | **Dono** |
| `post-visit-satisfaction` | Pesquisa de satisfação pós-visita automática | 6 | **Dono** |
| `crm-webhook-inbound` | Webhook de entrada CRM → Sofia | 6 | Dono/Corretor |
| `test-coverage-improvement` | Aumentar cobertura de testes | 6 | Eng |
| `dashboard-realtime` | Dashboard com atualizações em tempo real | 5 | Dono/Corretor |

**Systemd:**
- `imob-nightly.service` — oneshot, roda o squad
- `imob-nightly.timer` — dispara `*-*-* 05:00:00 UTC` (02:00 horário de Brasília), `RandomizedDelaySec=600`

**Variáveis de ambiente em `/opt/webhook.env`:**
- `GITHUB_TOKEN` — ✅ configurado (token `cowork-imobOne-v2`, permissões `repo` + `pull_requests` validadas)
- `GITHUB_REPO` — ✅ `http-otavio/ImobOne-v2`

**Como disparar manualmente:**
```bash
# Dry-run (sem escrever código ou abrir PR)
python3 /opt/ImobOne-v2/nightly_squad.py --dry-run

# Forçar uma task específica
python3 /opt/ImobOne-v2/nightly_squad.py --task-id multi-corretor-routing
```

**Restrição arquitetural inegociável:** Deploy Agent nunca faz merge. Para no PR. Operador aprova pela manhã.

### Bugs corrigidos no Nightly Squad — ✅ Abril 2026

Identificados nos logs da primeira execução real (02:00 de 13/04/2026). Três problemas corrigidos:

| Bug | Causa | Fix |
|-----|-------|-----|
| Notificação WhatsApp não chegou | `urllib.request` rejeita certificado self-signed da Evolution API com `SSL: CERTIFICATE_VERIFY_FAILED` | `_notify()` trocado para `httpx.Client(verify=False)` — mesmo padrão do `whatsapp_webhook.py` |
| Dev Agent falhou 9/9 tentativas (`Não foi possível parsear JSON`) | LLM gerava código Python embrulhado em strings JSON — newlines, `"`, regex com `\b` quebravam o parser | Formato de resposta trocado de JSON para tags XML `<file path="...">` e `<test path="...">` — imune a escaping de código |
| QA Agent falhou com `FileNotFoundError: pytest` | `pytest` não estava instalado no venv `/opt/webhook-venv` | `pip install pytest` executado no venv; `VENV_PYTEST` já apontava para o caminho correto |

**Instância WhatsApp `devlabz`:** desconectou durante a madrugada (`state: close`) — reconectada via QR code em 13/04/2026, `state: open` confirmado. Quando desconectar novamente: `curl -sk https://api.otaviolabs.com/instance/connect/devlabz -H 'apikey: ...'` retorna `base64` do QR para escanear.

**Decisões técnicas registradas (não reabrir):**
- `_notify()` usa `httpx` com `verify=False` — Evolution API usa cert self-signed. Não reverter para `urllib.request`.
- Dev Agent usa formato XML `<file>/<test>` — não JSON. JSON com código Python embrulhado em strings é inerentemente frágil.
- `pytest` é dependência obrigatória do venv. Incluir em qualquer nova instalação de venv.

---

### Report Engine semanal executivo — ✅ Abril 2026

`report_engine.py` — engine de relatório semanal para o dono da imobiliária.

**Métricas calculadas:** total_leads, visitas_confirmadas, leads_quentes, pipeline_estimado_brl, top_objecao, taxa_conversao, leads_por_origem. Dados buscados direto do Supabase (`leads` + `followup_events`).

**Outputs:**
- WhatsApp ao operador com resumo executivo formatado
- PDF via reportlab salvo em `clients/{client_id}/reports/`
- CSV para exportação em reuniões

**Endpoints no Pipeline Runner (porta 8003):**
- `GET /reports/weekly?client_id=...` — gera e envia relatório + retorna métricas
- `GET /reports/history?client_id=...&limit=10` — lista relatórios salvos
- `GET /reports/export/csv?client_id=...` — download CSV do relatório mais recente
- `GET /reports/export/pdf?client_id=...` — download PDF do relatório mais recente

**Testes:** 36 testes unitários em `tests/test_report_engine.py` — 36/36 passando no VPS.

**Dependências adicionadas:** `reportlab` instalado em `/opt/webhook-venv`.

**Decisão de deploy:** workflow padrão — SCP do Cowork para VPS (contorna OneDrive/git local quebrado). Git commitado diretamente no VPS após validação.

---

*Última atualização: Abril 2026 — google-calendar-integration deployado: tools/calendar.py (create_calendar_event via service account), _create_calendar_event_for_visit() fire-and-forget no webhook, corretor_email no onboarding, 20 testes. pipeline-roi-calc: migration Supabase (pipeline_value_brl), 15 testes. 121 testes totais passando (20 calendar + 15 pipeline + 50 portal + 36 report). portal_lead_capture.py deployado. Report Engine semanal (timer domingo 21h BRT). Nightly Squad executou 02:00 de 13/04 — 3 bugs corrigidos. WhatsApp devlabz state: open. Pipeline Runner ativo (porta 8003). Fase 3 mapeada. CRM 6 adapters. Ativar calendar: GOOGLE_CALENDAR_CREDENTIALS_JSON no webhook.env + corretor_email por corretor no onboarding.json (multi-corretor routing automático por bairro). Pendente: 360dialog para primeiro cliente real.*
*Este documento é a fonte da verdade do projeto. Qualquer decisão que conflite com ele deve passar pelo arquiteto auditor antes de ser implementada.*
