#!/bin/bash
set -e

echo "=== Setup VPS - Agente de Atendimento Da1Click ==="

# Atualizar sistema
echo "[1/5] Atualizando sistema..."
apt update && apt upgrade -y

# Instalar Docker
echo "[2/5] Instalando Docker..."
curl -fsSL https://get.docker.com | sh

# Criar diretório do projeto
echo "[3/5] Criando diretório do projeto..."
mkdir -p /opt/agente-de-atendimento
cd /opt/agente-de-atendimento

# Copiar arquivos necessários do repo
echo "[4/5] Clonando repositório..."
git clone https://github.com/da1click/agente-de-atendimento.git .  2>/dev/null || echo "Repo já clonado, pulando..."

# Gerar chave SSH para GitHub Actions
echo "[5/5] Gerando chave SSH para deploy..."
if [ ! -f ~/.ssh/deploy_key ]; then
    ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N "" -C "deploy-github-actions"
    cat ~/.ssh/deploy_key.pub >> ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    echo ""
    echo "=========================================="
    echo "CHAVE PRIVADA (copie e cole no GitHub Secrets como VPS_SSH_KEY):"
    echo "=========================================="
    cat ~/.ssh/deploy_key
    echo ""
    echo "=========================================="
fi

echo ""
echo "=== Setup concluído! ==="
echo ""
echo "Próximos passos:"
echo ""
echo "1. Crie o arquivo .env:"
echo "   nano /opt/agente-de-atendimento/.env"
echo ""
echo "2. Cole suas variáveis de ambiente (CHATWOOT_URL, SUPABASE_URL, etc.)"
echo ""
echo "3. Aponte os DNS:"
echo "   api.advbrasil.ai  → 5.78.154.62"
echo "   logs.advbrasil.ai → 5.78.154.62"
echo ""
echo "4. Suba os containers:"
echo "   cd /opt/agente-de-atendimento"
echo "   docker compose up -d"
echo ""
echo "5. Configure os secrets no GitHub:"
echo "   VPS_HOST     = 5.78.154.62"
echo "   VPS_USER     = root"
echo "   VPS_SSH_KEY  = (chave privada mostrada acima)"
echo ""
