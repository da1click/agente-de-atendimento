-- Tabela para rastrear cobrança de documentos pendentes
-- Acionada quando um humano adiciona a label 'cobrar-documentos' na conversa.
-- Desativada quando: humano remove a label OU cliente envia anexo OU atinge limite de tentativas.

CREATE TABLE IF NOT EXISTS ia_cobranca_docs (
    id BIGSERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    inbox_id INTEGER,
    contact_name TEXT DEFAULT '',
    contact_phone TEXT DEFAULT '',
    tentativas INTEGER NOT NULL DEFAULT 0,
    limite INTEGER NOT NULL DEFAULT 5,
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    ultimo_envio TIMESTAMPTZ,
    proximo_envio TIMESTAMPTZ,
    motivo_desativacao TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (account_id, conversation_id)
);

CREATE INDEX IF NOT EXISTS ix_ia_cobranca_docs_ativo ON ia_cobranca_docs (ativo, proximo_envio)
    WHERE ativo = TRUE;
