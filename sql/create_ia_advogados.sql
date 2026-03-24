-- Tabela de advogados por conta (agenda com separação por cor no Google Calendar)
CREATE TABLE IF NOT EXISTS ia_advogados (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id INTEGER NOT NULL,
    nome TEXT NOT NULL,
    especialidade TEXT NOT NULL DEFAULT '',
    cor_id INTEGER NOT NULL DEFAULT 0,
    duracao_agendamento INTEGER NOT NULL DEFAULT 30,
    horas_inicial_busca INTEGER NOT NULL DEFAULT 0,
    quantidade_dias_a_buscar INTEGER NOT NULL DEFAULT 14,
    disponibilidade JSONB NOT NULL DEFAULT '{"0":[],"1":[],"2":[],"3":[],"4":[],"5":[],"6":[]}',
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index para buscar advogados por conta + especialidade
CREATE INDEX IF NOT EXISTS idx_ia_advogados_account_esp ON ia_advogados(account_id, especialidade);
CREATE INDEX IF NOT EXISTS idx_ia_advogados_account ON ia_advogados(account_id);

-- Habilitar RLS (padrão do projeto)
ALTER TABLE ia_advogados ENABLE ROW LEVEL SECURITY;

-- Policy permissiva para service_role (Supabase admin)
CREATE POLICY "service_role_all" ON ia_advogados FOR ALL USING (true) WITH CHECK (true);
