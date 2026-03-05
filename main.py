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
