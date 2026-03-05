from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from ia import agendar_processamento, processar_mensagem, transcrever_audio, enviar_nota_privada
from db import upsert_lead, salvar_transcricao
import httpx
import json
import logging
import os
import secrets
import string

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Agente de Atendimento - Da1Click")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENTES_DIR = os.path.join(BASE_DIR, "clientes")

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def carregar_config_cliente(account_id: int) -> dict | None:
    """Carrega o config.json do cliente pelo account_id."""
    for pasta in os.listdir(CLIENTES_DIR):
        if pasta.startswith(f"{account_id}-"):
            config_path = os.path.join(CLIENTES_DIR, pasta, "config.json")
            if os.path.exists(config_path):
                with open(config_path, encoding="utf-8") as f:
                    return json.load(f)
    return None


def salvar_config_cliente(account_id: int, config: dict):
    """Salva o config.json do cliente."""
    for pasta in os.listdir(CLIENTES_DIR):
        if pasta.startswith(f"{account_id}-"):
            config_path = os.path.join(CLIENTES_DIR, pasta, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            return
    raise FileNotFoundError(f"Cliente {account_id} não encontrado")


def gerar_senha(tamanho: int = 12) -> str:
    """Gera senha aleatória com maiúsculas, minúsculas, números e @ ou *."""
    especiais = "@*"
    pool = string.ascii_letters + string.digits + especiais
    while True:
        senha = [secrets.choice(pool) for _ in range(tamanho)]
        if (any(c.isupper() for c in senha)
                and any(c.islower() for c in senha)
                and any(c.isdigit() for c in senha)
                and any(c in especiais for c in senha)):
            return "".join(senha)


def pasta_cliente(account_id: int) -> str | None:
    for pasta in os.listdir(CLIENTES_DIR):
        if pasta.startswith(f"{account_id}-"):
            return os.path.join(CLIENTES_DIR, pasta)
    return None




@app.get("/")
def root():
    return {"status": "online", "service": "Agente de Atendimento Da1Click"}


@app.get("/dashboard")
def dashboard():
    return FileResponse(os.path.join(BASE_DIR, "static", "dashboard.html"))


# ── API CLIENTES ──────────────────────────────────────────────

@app.get("/api/clientes")
def listar_clientes():
    clientes = []
    if not os.path.exists(CLIENTES_DIR):
        return clientes
    for pasta in sorted(os.listdir(CLIENTES_DIR)):
        config_path = os.path.join(CLIENTES_DIR, pasta, "config.json")
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                c = json.load(f)
            clientes.append({"account_id": c["account_id"], "nome": c["nome"], "ativo": c.get("ativo", True)})
    return clientes


@app.get("/api/clientes/{account_id}")
def obter_cliente(account_id: int):
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    return config


@app.delete("/api/clientes/{account_id}")
async def deletar_cliente(account_id: int):
    chatwoot_url = os.getenv("CHATWOOT_URL", "").rstrip("/")
    platform_token = os.getenv("CHATWOOT_PLATFORM_TOKEN", "")

    # Remover conta no Chatwoot via Platform API
    if chatwoot_url and platform_token:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                r = await client.delete(
                    f"{chatwoot_url}/platform/api/v1/accounts/{account_id}",
                    headers={"api_access_token": platform_token},
                )
                if r.status_code not in (200, 204):
                    logger.warning(f"Falha ao deletar conta {account_id} no Chatwoot: {r.status_code} {r.text}")
            except Exception as e:
                logger.warning(f"Erro ao deletar conta {account_id} no Chatwoot: {e}")

    # Remover pasta local
    pasta = pasta_cliente(account_id)
    if pasta and os.path.exists(pasta):
        import shutil
        shutil.rmtree(pasta)
        logger.info(f"Conta {account_id} removida localmente")
        return {"status": "deletado"}

    raise HTTPException(status_code=404, detail="Cliente não encontrado")


@app.put("/api/clientes/{account_id}")
async def atualizar_cliente(account_id: int, request: Request):
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    dados = await request.json()
    campos_editaveis = ["ativo", "chatwoot_url", "chatwoot_token", "openai_api_key", "ia_agent_id"]
    for campo in campos_editaveis:
        if campo in dados:
            config[campo] = dados[campo]
    salvar_config_cliente(account_id, config)
    return {"status": "ok"}


@app.post("/api/clientes")
async def criar_cliente(request: Request):
    dados = await request.json()
    account_id = dados.get("account_id")
    nome = dados.get("nome", "").strip()
    if not account_id or not nome:
        raise HTTPException(status_code=400, detail="account_id e nome são obrigatórios")
    pasta = os.path.join(CLIENTES_DIR, f"{account_id}-{nome}")
    os.makedirs(os.path.join(pasta, "prompt"), exist_ok=True)
    config = {
        "account_id": account_id,
        "nome": nome,
        "ativo": True,
        "openai_api_key": "",
        "chatwoot_url": "",
        "chatwoot_token": ""
    }
    with open(os.path.join(pasta, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return {"status": "criado", "account_id": account_id}


@app.post("/api/criar-conta-chatwoot")
async def criar_conta_chatwoot(request: Request):
    """Cria conta no Chatwoot via Platform Apps API e salva o config local."""
    chatwoot_url = os.getenv("CHATWOOT_URL", "").rstrip("/")
    platform_token = os.getenv("CHATWOOT_PLATFORM_TOKEN", "")
    super_admin_token = os.getenv("CHATWOOT_SUPER_ADMIN_TOKEN", "")

    if not chatwoot_url or not platform_token:
        raise HTTPException(status_code=500, detail="CHATWOOT_URL ou CHATWOOT_PLATFORM_TOKEN não configurados no .env")

    dados = await request.json()
    account_name = dados.get("account_name", "").strip()
    admin_email = dados.get("admin_email", "").strip()
    admin_name = dados.get("admin_name", "").strip() or admin_email.split("@")[0].replace(".", " ").title()
    if not account_name:
        raise HTTPException(status_code=400, detail="Nome da conta é obrigatório")
    if not admin_email:
        raise HTTPException(status_code=400, detail="Email do admin é obrigatório")

    headers_platform = {"api_access_token": platform_token}

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Criar a conta
        try:
            resp = await client.post(
                f"{chatwoot_url}/platform/api/v1/accounts",
                json={"name": account_name, "locale": "pt_BR"},
                headers=headers_platform,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Erro ao conectar ao Chatwoot: {e}")

        if resp.status_code not in (200, 201):
            try:
                erro = resp.json()
            except Exception:
                erro = resp.text
            raise HTTPException(status_code=resp.status_code, detail=erro)

        account_id = resp.json().get("id")
        if not account_id:
            raise HTTPException(status_code=502, detail="Chatwoot não retornou o account_id")

        # 2. Criar o usuário admin do cliente com senha gerada
        senha_gerada = gerar_senha()
        user_id = None
        try:
            r = await client.post(
                f"{chatwoot_url}/platform/api/v1/users",
                json={"name": admin_name, "email": admin_email, "password": senha_gerada},
                headers=headers_platform,
            )
            if r.status_code in (200, 201):
                user_id = r.json().get("id")
                logger.info(f"Usuário criado: {admin_email} (id={user_id})")
            else:
                logger.warning(f"Falha ao criar usuário {admin_email}: {r.status_code} {r.text}")
                senha_gerada = None
        except Exception as e:
            logger.warning(f"Erro ao criar usuário: {e}")
            senha_gerada = None

        # 3. Vincular o usuário à conta como administrador
        if user_id:
            try:
                r = await client.post(
                    f"{chatwoot_url}/platform/api/v1/accounts/{account_id}/account_users",
                    json={"user_id": user_id, "role": "administrator"},
                    headers=headers_platform,
                )
                if r.status_code in (200, 201):
                    logger.info(f"Usuário {admin_email} vinculado à conta {account_id} como administrator")
                else:
                    logger.warning(f"Falha ao vincular usuário à conta: {r.status_code} {r.text}")
            except Exception as e:
                logger.warning(f"Erro ao vincular usuário à conta: {e}")

        # 4. Adicionar admins padrão Da1Click via Platform API
        default_admins = [e.strip() for e in os.getenv("CHATWOOT_DEFAULT_ADMINS", "").split(",") if e.strip()]
        for email in default_admins:
            nome_admin = email.split("@")[0].replace(".", " ").title()
            try:
                # Busca ou cria o usuário via Platform API
                r = await client.post(
                    f"{chatwoot_url}/platform/api/v1/users",
                    json={"name": nome_admin, "email": email, "password": gerar_senha()},
                    headers=headers_platform,
                )
                if r.status_code in (200, 201):
                    default_user_id = r.json().get("id")
                    # Vincula à conta como administrador
                    await client.post(
                        f"{chatwoot_url}/platform/api/v1/accounts/{account_id}/account_users",
                        json={"user_id": default_user_id, "role": "administrator"},
                        headers=headers_platform,
                    )
                    logger.info(f"Admin padrão vinculado: {email} → conta {account_id}")
                else:
                    logger.warning(f"Falha ao buscar/criar admin padrão {email}: {r.status_code} {r.text}")
            except Exception as e:
                logger.warning(f"Erro ao adicionar admin padrão {email}: {e}")

    # 5. Salvar config local
    nome_pasta = account_name.replace(" ", "_")
    pasta = os.path.join(CLIENTES_DIR, f"{account_id}-{nome_pasta}")
    os.makedirs(os.path.join(pasta, "prompt"), exist_ok=True)
    config = {
        "account_id": account_id,
        "nome": account_name,
        "ativo": True,
        "openai_api_key": "",
        "chatwoot_url": chatwoot_url,
        "chatwoot_token": super_admin_token,
    }
    with open(os.path.join(pasta, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    logger.info(f"Conta Chatwoot criada via Platform API: account_id={account_id}, nome={account_name}")
    return {"status": "criado", "account_id": account_id, "admin_email": admin_email, "senha": senha_gerada}


@app.post("/api/criar-usuarios-chatwoot")
async def criar_usuarios_chatwoot(request: Request):
    """Cria ou vincula múltiplos usuários a uma conta Chatwoot."""
    chatwoot_url = os.getenv("CHATWOOT_URL", "").rstrip("/")
    platform_token = os.getenv("CHATWOOT_PLATFORM_TOKEN", "")
    if not chatwoot_url or not platform_token:
        raise HTTPException(status_code=500, detail="CHATWOOT_URL ou CHATWOOT_PLATFORM_TOKEN não configurados")

    dados = await request.json()
    account_id = dados.get("account_id")
    usuarios = dados.get("usuarios", [])  # [{email, role}]

    if not account_id or not usuarios:
        raise HTTPException(status_code=400, detail="account_id e usuarios são obrigatórios")

    headers_platform = {"api_access_token": platform_token}
    resultados = []

    async with httpx.AsyncClient(timeout=30) as client:
        for u in usuarios:
            email = u.get("email", "").strip()
            role = u.get("role", "agent")
            if not email:
                continue

            senha_gerada = gerar_senha()
            nome = email.split("@")[0].replace(".", " ").title()

            # Cria ou encontra o usuário
            try:
                r = await client.post(
                    f"{chatwoot_url}/platform/api/v1/users",
                    json={"name": nome, "email": email, "password": senha_gerada},
                    headers=headers_platform,
                )
                if r.status_code not in (200, 201):
                    resultados.append({"email": email, "status": "erro", "detalhe": r.text})
                    continue

                user_data = r.json()
                user_id = user_data.get("id")
                ja_existia = user_data.get("created_at") != user_data.get("updated_at")

            except Exception as e:
                resultados.append({"email": email, "status": "erro", "detalhe": str(e)})
                continue

            # Vincula à conta com o role escolhido
            try:
                rv = await client.post(
                    f"{chatwoot_url}/platform/api/v1/accounts/{account_id}/account_users",
                    json={"user_id": user_id, "role": role},
                    headers=headers_platform,
                )
                if rv.status_code in (200, 201):
                    resultados.append({
                        "email": email,
                        "role": role,
                        "status": "vinculado" if ja_existia else "criado",
                        "senha": None if ja_existia else senha_gerada,
                    })
                    logger.info(f"Usuário {email} → conta {account_id} ({role})")
                else:
                    resultados.append({"email": email, "status": "erro_vinculo", "detalhe": rv.text})
            except Exception as e:
                resultados.append({"email": email, "status": "erro_vinculo", "detalhe": str(e)})

    return {"resultados": resultados}


@app.post("/webhook/chatwoot")
async def chatwoot_webhook(request: Request):
    payload = await request.json()
    event = payload.get("event", "")

    if event != "automation_event.message_created":
        return {"status": "ignorado", "event": event}

    account_id = payload.get("messages", [{}])[0].get("account_id") if payload.get("messages") else None
    config = carregar_config_cliente(account_id) if account_id else None

    if not config:
        logger.warning(f"Cliente não encontrado para account_id={account_id}")
        return {"status": "cliente_nao_encontrado"}

    ia_agent_id = config.get("ia_agent_id")
    messages = payload.get("messages", [])

    for msg in messages:
        # Ignorar mensagens do agente (só processar do cliente)
        if msg.get("message_type") != 0:
            continue

        conversation_id = msg.get("conversation_id")
        inbox_id = payload.get("inbox_id")
        contact = msg.get("sender", {})
        nome = contact.get("name", "")
        telefone = contact.get("phone_number", "")
        texto = msg.get("content") or ""
        attachments = msg.get("attachments", [])
        assignee_id = msg.get("conversation", {}).get("assignee_id")

        # Registrar lead no Supabase
        try:
            upsert_lead(account_id, inbox_id, conversation_id, nome, telefone)
        except Exception as e:
            logger.warning(f"Erro ao registrar lead no Supabase: {e}")

        ia_ativa = ia_agent_id is not None and assignee_id == ia_agent_id

        # Verificar se é áudio
        audio = next((a for a in attachments if a.get("file_type") == "audio"), None)

        if audio:
            logger.info(f"[{account_id}] Áudio de {nome} ({telefone}) — transcrevendo...")
            try:
                transcricao = await transcrever_audio(
                    url_audio=audio["data_url"],
                    openai_api_key=config["openai_api_key"],
                )
                logger.info(f"[{account_id}] Transcrição: {transcricao}")

                nota = f"🎙️ Transcrição de áudio de {nome}:\n\n{transcricao}"
                await enviar_nota_privada(
                    chatwoot_url=config["chatwoot_url"],
                    chatwoot_token=config["chatwoot_token"],
                    account_id=account_id,
                    conversation_id=conversation_id,
                    texto=nota,
                )
                # Salvar transcrição no Supabase
                try:
                    salvar_transcricao(
                        account_id=account_id,
                        inbox_id=inbox_id,
                        conversation_id=conversation_id,
                        chatwoot_message_id=msg.get("id"),
                        transcription=transcricao,
                        audio_url=audio.get("data_url", ""),
                    )
                except Exception as e:
                    logger.warning(f"Erro ao salvar transcrição no Supabase: {e}")
            except Exception as e:
                logger.error(f"Erro ao transcrever áudio: {e}")

        if ia_ativa:
            logger.info(f"[{account_id}] IA ativa — agendando processamento de {nome}: {texto or '[áudio]'}")
            try:
                agendar_processamento(config, account_id, conversation_id, inbox_id)
            except Exception as e:
                logger.error(f"Erro ao agendar processamento: {e}")
        else:
            logger.info(f"[{account_id}] IA inativa (assignee={assignee_id}, ia_agent_id={ia_agent_id}) — apenas transcrição")

    return {"status": "ok"}
