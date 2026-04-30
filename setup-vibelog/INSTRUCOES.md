# Setup VibeLog — Sistema de Logs para Bots de IA

## O que e isso

VibeLog e um painel web simples para monitorar bots de IA que usam Chatwoot.
Ele mostra: conversas agrupadas, timeline de acoes da IA, erros, busca por contato.

## Arquitetura

```
Browser → subdominio-logs → Nginx (443) → vibelog:3000 (Next.js)
                                               ↓
                                         Loki HTTP API (:3100)
                                               ↑
                                    Promtail (coleta logs Docker)
                                               ↑
                                    Containers dos bots (stdout)
```

## Componentes

| Servico | Imagem | Funcao |
|---|---|---|
| **loki** | grafana/loki:3.0.0 | Armazena logs (30 dias retencao) |
| **promtail** | grafana/promtail:3.0.0 | Coleta logs dos containers e envia pro Loki |
| **vibelog** | ghcr.io/muriloorama/vibelog:latest | Painel web (Next.js) |

## Como implantar

### 1. Criar estrutura de pastas na VPS

```bash
mkdir -p /opt/vibecode/monitoring
mkdir -p /opt/vibecode/nginx
mkdir -p /opt/vibecode/envs
```

### 2. Copiar arquivos de config

Copiar os seguintes arquivos para a VPS:

- `loki-config.yml` → `/opt/vibecode/monitoring/loki-config.yml`
- `promtail-config.yml` → `/opt/vibecode/monitoring/promtail-config.yml`

### 3. Configurar promtail-config.yml

**IMPORTANTE**: Editar o arquivo `promtail-config.yml` e substituir os nomes dos containers
na secao `filters.values` pelos nomes reais dos containers de bots neste servidor.

Exemplo: se o servidor tem containers `bot-vendas` e `bot-suporte`, o filtro deve ser:
```yaml
filters:
  - name: name
    values:
      - bot-vendas
      - bot-suporte
```

### 4. Criar arquivo .env do VibeLog

Criar `/opt/vibecode/envs/vibelog.env`:
```
LOKI_URL=http://loki:3100
AUTH_PASSWORD=SUA_SENHA_AQUI
```

### 5. Adicionar servicos ao docker-compose.yml

Adicionar ao docker-compose.yml existente (secao services):

```yaml
  # ============================================
  # MONITORAMENTO (Loki + Promtail + VibeLog)
  # ============================================
  loki:
    image: grafana/loki:3.0.0
    container_name: loki
    restart: unless-stopped
    expose:
      - "3100"
    volumes:
      - loki-data:/loki
      - ./monitoring/loki-config.yml:/etc/loki/local-config.yaml
    command: -config.file=/etc/loki/local-config.yaml

  promtail:
    image: grafana/promtail:3.0.0
    container_name: promtail
    restart: unless-stopped
    volumes:
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /var/run/docker.sock:/var/run/docker.sock
      - ./monitoring/promtail-config.yml:/etc/promtail/config.yml
    command: -config.file=/etc/promtail/config.yml
    depends_on:
      - loki

  vibelog:
    image: ghcr.io/muriloorama/vibelog:latest
    container_name: vibelog
    restart: unless-stopped
    expose:
      - "3000"
    env_file:
      - ./envs/vibelog.env
    depends_on:
      - loki
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
```

Adicionar ao `volumes:` (no final do docker-compose.yml):
```yaml
  loki-data:
```

### 6. Configurar Nginx (subdominio para o VibeLog)

Adicionar ao arquivo de config do Nginx (ex: `default.conf`):

```nginx
# VibeLog (Logs)
server {
    listen 80;
    server_name SEU_SUBDOMINIO_LOGS;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}
server {
    listen 443 ssl;
    server_name SEU_SUBDOMINIO_LOGS;
    ssl_certificate /etc/letsencrypt/live/SEU_SUBDOMINIO_LOGS/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/SEU_SUBDOMINIO_LOGS/privkey.pem;
    location / {
        proxy_pass http://vibelog:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Substituir `SEU_SUBDOMINIO_LOGS` pelo subdominio real (ex: `logs.meudominio.com`).

### 7. Gerar certificado SSL (antes de subir o Nginx com SSL)

```bash
docker compose up -d nginx  # subir primeiro sem SSL
docker compose run --rm certbot certonly --webroot -w /var/www/certbot -d SEU_SUBDOMINIO_LOGS
docker compose restart nginx
```

### 8. Subir tudo

```bash
cd /opt/vibecode
docker compose up -d
```

### 9. Verificar

```bash
# Verificar se todos subiram
docker ps

# Verificar logs do vibelog
docker logs vibelog

# Verificar se Loki recebe dados (esperar 1-2 minutos)
curl -s http://localhost:3100/loki/api/v1/label/container/values
```

## Observacoes importantes

- O Promtail usa `docker_sd_configs` e precisa do docker.sock montado
- O label usado nos logs e `container` (NAO `container_name`)
- Retencao de logs: 30 dias (720h), configuravel em `loki-config.yml`
- O VibeLog filtra automaticamente containers de infra (nginx, loki, promtail, watchtower, vibelog, certbot)
- O VibeLog faz auto-refresh: dashboard 30s, conversas 15s, timeline 10s
- A imagem `ghcr.io/muriloorama/vibelog:latest` e a mesma para todos os servidores
- Se usar Watchtower, o VibeLog atualiza automaticamente quando houver nova versao
