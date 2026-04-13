-- Migration: add_objections_detected
-- Task: objection-analysis-report
-- Data: Abril 2026
--
-- Adiciona campo JSONB para armazenar objeções detectadas por lead.
-- Cada item no array tem:
--   { "categoria": "preco|prazo|localizacao|...", "mensagem_preview": "...", "detectado_em": "ISO8601" }
--
-- Alimentado por: objection_engine.detect_and_save_objection()
-- Wiring no webhook: asyncio.create_task(detect_and_save_objection(sender, body, client_id))
-- Consumido por: objection_engine.compute_objection_report() + report_engine.compute_weekly_metrics()

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS objections_detected JSONB DEFAULT '[]'::jsonb;

-- Índice GIN para queries de agregação eficientes
CREATE INDEX IF NOT EXISTS idx_leads_objections_detected
ON leads USING GIN (objections_detected);

-- Comentário para documentação do schema
COMMENT ON COLUMN leads.objections_detected IS
'Array JSONB de objeções detectadas nas mensagens do lead. '
'Cada item: {"categoria": str, "mensagem_preview": str, "detectado_em": ISO8601}. '
'Categorias: preco, prazo, localizacao, financiamento, condominio, concorrente, outros. '
'Populado pelo objection_engine via webhook (fire-and-forget).';
