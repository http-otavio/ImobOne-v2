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
| FASE 2 — Consultor digital de luxo | 🔜 Próxima | — |

**Infraestrutura ativa:**
- VPS: `76.13.165.64` — Docker Swarm, serviço `imob_agents`
- Repositório: `https://github.com/http-otavio/ImobOne-v2` (privado)
- Clone no VPS: `/opt/ImobOne-v2` (deploy via `git pull + docker build + service update`)
- Instância legada: `/opt/imovel-ai/ImobOne-v2` (manter como backup)

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
    crm_webhook.py          # Webhook genérico para CRM do cliente
    embeddings.py           # Geração de embeddings (text-embedding-3-small)
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
- [ ] Supabase: criar projeto, rodar migrations (schema leads + pgvector)
- [ ] ElevenLabs: escolher/aprovar voz do primeiro cliente
- [ ] WhatsApp Business API: credenciais 360dialog (BSP escolhido — ver decisão abaixo)
- [ ] Primeiro `onboarding.json` de cliente real (ou fictício para smoke test)

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
| Supabase pgvector | ✅ configurada | 16+ imóveis indexados no demo |
| Redis | ✅ rodando | 127.0.0.1:6379 no container |
| ElevenLabs | ⏳ pendente | Credencial não configurada |
| WhatsApp BSP (360dialog) | ⏳ pendente | **Único bloqueio para cliente real** |

### Decisões técnicas tomadas durante a implementação da FASE 1
- **LLM do consultor:** `claude-sonnet-4-6` — não Haiku. O consultor do produto (QA jornadas + futuro FASE 2) usa Sonnet para qualidade de resposta. Haiku fica restrito ao evaluator e agentes de execução simples.
- **LLM evaluator:** usa assistant prefill `{"passou":` para forçar JSON válido — elimina falhas de parse
- **Prompts base:** baked no Docker via `_prompts_build/` (workaround para FUSE filesystem do Cowork). Fonte da verdade é `_prompts_build/consultant_base.md`, que é copiado para `/app/prompts/base/` no build.
- **Redis default:** sempre `127.0.0.1:6379`, nunca `localhost` (IPv6 quebra em Docker com ip6tables ativo)
- **Docker entry point:** `CMD`, não `ENTRYPOINT` — permite override via `command:` no Docker Swarm stack
- **Modelos:** orquestrador/auditor/consultor = `claude-sonnet-4-6`, agentes simples/evaluator = `claude-haiku-4-5-20251001`
- **qa_integration skip:** aceitável em deploy com credenciais reais pendentes; gate obrigatório antes de cliente real
- **portfolio_path é aninhado:** no `onboarding.json`, o caminho do CSV está em `onboarding["portfolio"]["portfolio_path"]`, não em `onboarding["portfolio_path"]`. O `setup_pipeline.py` lê com fallback para os dois formatos. Nunca remover esse fallback.
- **Critérios de QA:** devem ser objetivamente verificáveis pelo Haiku evaluator a partir do texto da resposta. Critérios como "não deve inventar X" falham porque o Haiku não tem como verificar a origem dos dados — preferir "deve atribuir fonte X na resposta" ou reduzir severidade para INFORMATIVO.

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

*Última atualização: Março 2026 — Fase 1 completa. QA 90% (deploy_ready). Google APIs configuradas. WhatsApp BSP pendente. Próximo passo: credenciais 360dialog → iniciar Fase 2.*
*Este documento é a fonte da verdade do projeto. Qualquer decisão que conflite com ele deve passar pelo arquiteto auditor antes de ser implementada.*