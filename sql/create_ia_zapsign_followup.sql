-- Tabela de follow-up ZapSign (lembretes automáticos)
CREATE TABLE IF NOT EXISTS ia_zapsign_followup (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    inbox_id INTEGER,
    doc_token TEXT NOT NULL,
    stagio INTEGER DEFAULT 1,
    proximo_disparo TIMESTAMPTZ NOT NULL,
    ativo BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(account_id, conversation_id, doc_token)
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_zapsign_followup_ativo ON ia_zapsign_followup(ativo, proximo_disparo);
CREATE INDEX IF NOT EXISTS idx_zapsign_followup_doc ON ia_zapsign_followup(doc_token);

-- RLS
ALTER TABLE ia_zapsign_followup ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all for service" ON ia_zapsign_followup FOR ALL USING (true) WITH CHECK (true);

-- Colunas extras na tabela de config ZapSign (follow-up settings por conta)
ALTER TABLE ia_zapsign_config ADD COLUMN IF NOT EXISTS followup_ativo BOOLEAN DEFAULT FALSE;
ALTER TABLE ia_zapsign_config ADD COLUMN IF NOT EXISTS followup_estagios JSONB DEFAULT '[
    {"stagio": 1, "horas": 2, "mensagem": "Olá! Notei que o contrato ainda não foi assinado. Precisa de alguma ajuda?"},
    {"stagio": 2, "horas": 6, "mensagem": "Oi! Só passando para lembrar sobre o contrato pendente. Posso ajudar com alguma dúvida?"},
    {"stagio": 3, "horas": 12, "mensagem": "Olá! Este é nosso último lembrete sobre o contrato. Por favor, assine para darmos continuidade."}
]'::jsonb;
