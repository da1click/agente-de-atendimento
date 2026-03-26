-- Migrar follow-up de "por documento" para "por conversa"
-- Adicionar coluna doc_tokens (lista de tokens) e remover constraint antiga

-- 1. Adicionar coluna doc_tokens
ALTER TABLE ia_zapsign_followup ADD COLUMN IF NOT EXISTS doc_tokens JSONB DEFAULT '[]'::jsonb;

-- 2. Migrar dados existentes: mover doc_token para doc_tokens array
UPDATE ia_zapsign_followup
SET doc_tokens = jsonb_build_array(doc_token)
WHERE doc_tokens = '[]'::jsonb AND doc_token IS NOT NULL AND doc_token != '';

-- 3. Remover constraint antiga (account_id, conversation_id, doc_token)
ALTER TABLE ia_zapsign_followup DROP CONSTRAINT IF EXISTS ia_zapsign_followup_account_id_conversation_id_doc_token_key;

-- 4. Criar nova constraint (account_id, conversation_id) - 1 follow-up por conversa
ALTER TABLE ia_zapsign_followup ADD CONSTRAINT ia_zapsign_followup_account_conversation_unique
    UNIQUE (account_id, conversation_id);

-- 5. Tornar doc_token nullable (agora usamos doc_tokens)
ALTER TABLE ia_zapsign_followup ALTER COLUMN doc_token DROP NOT NULL;
ALTER TABLE ia_zapsign_followup ALTER COLUMN doc_token SET DEFAULT '';
