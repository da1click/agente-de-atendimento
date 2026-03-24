-- Adiciona inbox_id nas tabelas para rastreamento por canal

ALTER TABLE ia_conversations  ADD COLUMN IF NOT EXISTS inbox_id INT;
ALTER TABLE ia_leads          ADD COLUMN IF NOT EXISTS inbox_id INT;
ALTER TABLE ia_agendamentos   ADD COLUMN IF NOT EXISTS inbox_id INT;
ALTER TABLE ia_transcricoes   ADD COLUMN IF NOT EXISTS inbox_id INT;

-- Índices para facilitar consultas por conta e inbox
CREATE INDEX IF NOT EXISTS idx_conversations_account  ON ia_conversations (account_id);
CREATE INDEX IF NOT EXISTS idx_conversations_inbox    ON ia_conversations (account_id, inbox_id);
CREATE INDEX IF NOT EXISTS idx_leads_account          ON ia_leads (account_id);
CREATE INDEX IF NOT EXISTS idx_leads_inbox            ON ia_leads (account_id, inbox_id);
CREATE INDEX IF NOT EXISTS idx_agendamentos_account   ON ia_agendamentos (account_id);
CREATE INDEX IF NOT EXISTS idx_transcricoes_account   ON ia_transcricoes (account_id);
