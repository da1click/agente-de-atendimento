-- Tabela de eventos de trackeamento de origem da conversa.
-- Capturada no webhook message_created do Chatwoot (primeira mensagem incoming
-- de cada conversa). Classifica a origem em: anuncio_meta / site / direto.

CREATE TABLE IF NOT EXISTS ia_tracking_events (
    id BIGSERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    message_id BIGINT,
    inbox_id INTEGER,
    inbox_name TEXT DEFAULT '',
    contact_name TEXT DEFAULT '',
    contact_phone TEXT DEFAULT '',
    origem TEXT NOT NULL,               -- 'anuncio_meta' | 'site' | 'direto'
    content_raw TEXT DEFAULT '',

    -- Campos do anúncio Meta (Instagram / Facebook Click-to-WhatsApp)
    anuncio_fonte TEXT,                 -- INSTAGRAM | FACEBOOK | ...
    anuncio_texto TEXT,
    anuncio_url TEXT,
    anuncio_source_id TEXT,
    anuncio_ref_id TEXT,                -- ID longo de referência da Meta
    anuncio_tipo TEXT,                  -- "Anuncio"
    anuncio_resposta TEXT,              -- resposta do contato

    -- Campos do formulário de site
    site_nome TEXT,
    site_email TEXT,
    site_telefone TEXT,
    site_assunto TEXT,
    site_mensagem TEXT,

    -- Mensagem direta
    direto_frase TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (account_id, conversation_id)
);

CREATE INDEX IF NOT EXISTS ix_ia_tracking_account_created
    ON ia_tracking_events (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_ia_tracking_account_origem
    ON ia_tracking_events (account_id, origem);
CREATE INDEX IF NOT EXISTS ix_ia_tracking_account_inbox
    ON ia_tracking_events (account_id, inbox_id);
