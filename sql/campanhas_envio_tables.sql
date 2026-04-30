-- ══════════════════════════════════════════════════════════════
-- TABELAS DE CAMPANHAS DE ENVIO
-- Executar no Supabase SQL Editor
-- ══════════════════════════════════════════════════════════════

-- 1. Campanhas de envio (por etiqueta ou etapa do kanban)
CREATE TABLE IF NOT EXISTS ia_campanhas_envio (
  id SERIAL PRIMARY KEY,
  account_id INT NOT NULL,
  nome TEXT NOT NULL DEFAULT '',
  tipo_filtro TEXT NOT NULL DEFAULT 'etiqueta',  -- 'etiqueta' ou 'etapa_kanban'
  valor_filtro TEXT NOT NULL DEFAULT '',           -- nome da etiqueta ou 'funnel_id:step_id'
  limite_diario INT NOT NULL DEFAULT 200,
  mensagem TEXT NOT NULL DEFAULT '',
  template_whatsapp TEXT,
  image_url TEXT,
  inbox_envio_id INT,
  ativo BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campanhas_envio_ativo
  ON ia_campanhas_envio (ativo) WHERE ativo = true;

CREATE INDEX IF NOT EXISTS idx_campanhas_envio_account
  ON ia_campanhas_envio (account_id);

-- 2. Log de envios
CREATE TABLE IF NOT EXISTS ia_campanhas_envio_log (
  id SERIAL PRIMARY KEY,
  campanha_id INT NOT NULL REFERENCES ia_campanhas_envio(id) ON DELETE CASCADE,
  account_id INT NOT NULL,
  conversation_id INT NOT NULL,
  enviado_em TIMESTAMPTZ DEFAULT now(),
  status TEXT DEFAULT 'enviado'
);

CREATE INDEX IF NOT EXISTS idx_campanhas_envio_log_campanha_data
  ON ia_campanhas_envio_log (campanha_id, enviado_em);

CREATE UNIQUE INDEX IF NOT EXISTS idx_campanhas_envio_log_unique
  ON ia_campanhas_envio_log (campanha_id, conversation_id);

CREATE INDEX IF NOT EXISTS idx_campanhas_envio_log_account
  ON ia_campanhas_envio_log (account_id);
