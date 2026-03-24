-- ══════════════════════════════════════════════════════════════
-- TABELAS DE REMARKETING
-- Executar no Supabase SQL Editor
-- ══════════════════════════════════════════════════════════════

-- 1. Campanhas de remarketing
CREATE TABLE IF NOT EXISTS ia_remarketing_campanhas (
  id SERIAL PRIMARY KEY,
  account_id INT NOT NULL,
  nome TEXT NOT NULL DEFAULT '',
  dias_inatividade INT NOT NULL DEFAULT 30,
  limite_diario INT NOT NULL DEFAULT 200,
  mensagem TEXT NOT NULL DEFAULT '',
  template_whatsapp TEXT,
  ativo BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Index para buscar campanhas ativas rapidamente
CREATE INDEX IF NOT EXISTS idx_remarketing_campanhas_ativo
  ON ia_remarketing_campanhas (ativo) WHERE ativo = true;

CREATE INDEX IF NOT EXISTS idx_remarketing_campanhas_account
  ON ia_remarketing_campanhas (account_id);

-- 2. Envios de remarketing (tracking)
CREATE TABLE IF NOT EXISTS ia_remarketing_envios (
  id SERIAL PRIMARY KEY,
  campanha_id INT NOT NULL REFERENCES ia_remarketing_campanhas(id) ON DELETE CASCADE,
  account_id INT NOT NULL,
  conversation_id INT NOT NULL,
  enviado_em TIMESTAMPTZ DEFAULT now(),
  status TEXT DEFAULT 'enviado'
);

-- Index para contar envios por dia
CREATE INDEX IF NOT EXISTS idx_remarketing_envios_campanha_data
  ON ia_remarketing_envios (campanha_id, enviado_em);

-- Index para evitar duplicatas (1 envio por campanha por conversa)
CREATE UNIQUE INDEX IF NOT EXISTS idx_remarketing_envios_unique
  ON ia_remarketing_envios (campanha_id, conversation_id);

-- Index para buscar envios de uma conta
CREATE INDEX IF NOT EXISTS idx_remarketing_envios_account
  ON ia_remarketing_envios (account_id);

-- 3. Habilitar RLS (Row Level Security) — opcional, depende da config do Supabase
-- ALTER TABLE ia_remarketing_campanhas ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE ia_remarketing_envios ENABLE ROW LEVEL SECURITY;

-- 4. Policies para service_role (o backend usa service_role key)
-- Se RLS estiver habilitado, descomente abaixo:
-- CREATE POLICY "service_role_all" ON ia_remarketing_campanhas FOR ALL USING (true);
-- CREATE POLICY "service_role_all" ON ia_remarketing_envios FOR ALL USING (true);
