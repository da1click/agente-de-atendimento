-- ─────────────────────────────────────────────────────────────
-- ia_uso_mensal: registro de conversas únicas por ciclo
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ia_uso_mensal (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          INT NOT NULL,
    conversation_id     INT NOT NULL,
    mes                 TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (account_id, conversation_id, mes)
);

-- Garantir que created_at existe (caso a tabela já exista sem essa coluna)
ALTER TABLE ia_uso_mensal ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

-- Índice para consultas por período
CREATE INDEX IF NOT EXISTS idx_uso_mensal_account_created ON ia_uso_mensal (account_id, created_at);
