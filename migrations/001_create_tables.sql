-- ─────────────────────────────────────────────────────────────
-- ia_conversations: estado atual de cada conversa
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ia_conversations (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          INT NOT NULL,
    conversation_id     INT NOT NULL,
    contact_name        TEXT,
    contact_phone       TEXT,
    current_phase       TEXT DEFAULT 'identificacao',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (account_id, conversation_id)
);

-- ─────────────────────────────────────────────────────────────
-- ia_leads: dados coletados durante a qualificação
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ia_leads (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          INT NOT NULL,
    conversation_id     INT NOT NULL,
    contact_name        TEXT,
    contact_phone       TEXT,
    status              TEXT DEFAULT 'em_atendimento',
    -- status: em_atendimento | qualificado | inviavel | convertido | transferido
    inviability_reason  TEXT,
    qualification_data  JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (account_id, conversation_id)
);

-- ─────────────────────────────────────────────────────────────
-- ia_agendamentos: consultas agendadas
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ia_agendamentos (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          INT NOT NULL,
    conversation_id     INT NOT NULL,
    contact_name        TEXT,
    contact_phone       TEXT,
    scheduled_date      DATE,
    scheduled_time      TIME,
    advogada            TEXT,
    status              TEXT DEFAULT 'agendado',
    -- status: agendado | confirmado | cancelado | realizado
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- ia_transcricoes: histórico de áudios transcritos
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ia_transcricoes (
    id                      BIGSERIAL PRIMARY KEY,
    account_id              INT NOT NULL,
    conversation_id         INT NOT NULL,
    chatwoot_message_id     INT,
    transcription           TEXT NOT NULL,
    audio_url               TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);
