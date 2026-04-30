-- Tabela de configuração ZapSign por conta
CREATE TABLE IF NOT EXISTS ia_zapsign_config (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id INTEGER UNIQUE NOT NULL,
    api_token TEXT DEFAULT '',
    sandbox BOOLEAN DEFAULT FALSE,
    webhook_url TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Tabela de documentos ZapSign (cache local)
CREATE TABLE IF NOT EXISTS ia_zapsign_docs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id INTEGER NOT NULL,
    doc_token TEXT UNIQUE NOT NULL,
    nome TEXT DEFAULT '',
    status TEXT DEFAULT '',
    external_id TEXT DEFAULT '',
    created_at_zapsign TEXT DEFAULT '',
    signers JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_zapsign_docs_account ON ia_zapsign_docs(account_id);
CREATE INDEX IF NOT EXISTS idx_zapsign_docs_status ON ia_zapsign_docs(account_id, status);

-- RLS (Row Level Security) - desabilitado por padrão como nas outras tabelas
ALTER TABLE ia_zapsign_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE ia_zapsign_docs ENABLE ROW LEVEL SECURITY;

-- Policies permissivas (service key)
CREATE POLICY "Allow all for service" ON ia_zapsign_config FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow all for service" ON ia_zapsign_docs FOR ALL USING (true) WITH CHECK (true);
