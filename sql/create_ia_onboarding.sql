-- Tabela de onboarding para clientes preencherem dados iniciais
CREATE TABLE IF NOT EXISTS ia_onboarding (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id INTEGER NOT NULL UNIQUE,
    token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'draft',
    form_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    submitted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ia_onboarding_token ON ia_onboarding(token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ia_onboarding_account ON ia_onboarding(account_id);

ALTER TABLE ia_onboarding ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all" ON ia_onboarding
    FOR ALL USING (true) WITH CHECK (true);
