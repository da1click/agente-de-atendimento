from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from ia import agendar_processamento, processar_mensagem, transcrever_audio, enviar_nota_privada
from db import upsert_lead, salvar_transcricao, deletar_dados_conta
from db import (
    super_admin_existe, criar_usuario, get_usuario_por_email, get_usuario_por_id,
    listar_usuarios_com_contas, atualizar_usuario, get_contas_do_usuario,
    atribuir_conta_usuario, remover_conta_usuario,
)
from db import (
    carregar_config_cliente as db_carregar_config,
    salvar_config_cliente as db_salvar_config,
    listar_configs_clientes, deletar_config_cliente,
)
from db import (
    registrar_uso_mensal, contar_uso_mensal, historico_uso_mensal,
    contar_conversas, contar_leads_por_status, contar_agendamentos, contar_transcricoes,
)
from auth import hash_password, verify_password, create_token, get_current_user, require_super_admin
from inatividade import registrar_atividade, iniciar_monitoramento
import asyncio
import httpx
import json
import logging
import os
import random
import secrets
import string

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    iniciar_monitoramento()
    # Cria super_admin inicial se não existir
    try:
        if not super_admin_existe():
            criar_usuario(
                email="muriloa@gmail.com",
                password_hash=hash_password("Mu368456*"),
                nome="Murilo",
                role="super_admin",
            )
            logger.info("Super admin criado: muriloa@gmail.com")
    except Exception as e:
        logger.warning(f"Erro ao verificar/criar super admin: {e}")
    yield


app = FastAPI(title="Agente de Atendimento - Da1Click", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENTES_DIR = os.path.join(BASE_DIR, "clientes")

# Set para deduplicação de transcrições de áudio (evita loop)
_transcricoes_processadas: set[int] = set()

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def carregar_config_cliente(account_id: int) -> dict | None:
    """Carrega o config do cliente pelo account_id (Supabase)."""
    return db_carregar_config(account_id)


def salvar_config_cliente(account_id: int, config: dict):
    """Salva o config do cliente (Supabase)."""
    db_salvar_config(account_id, config)


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


def _carregar_labels() -> list:
    path = os.path.join(BASE_DIR, "config", "labels.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []

LABELS_PADRAO = _carregar_labels()

_CORES_LABELS = [
    "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6",
    "#EC4899", "#14B8A6", "#F97316", "#6366F1", "#84CC16",
]


async def criar_labels_padrao(chatwoot_url: str, token: str, account_id: int):
    """Cria as labels padrão Da1Click na conta recém-criada."""
    labels = _carregar_labels()
    if not labels:
        logger.warning("config/labels.json vazio ou não encontrado — nenhuma label criada")
        return
    cores = _CORES_LABELS.copy()
    random.shuffle(cores)
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/labels"
    async with httpx.AsyncClient(timeout=30) as client:
        for i, label in enumerate(labels):
            cor = cores[i % len(cores)]
            try:
                r = await client.post(url, headers=headers, json={
                    "title": label["title"],
                    "description": label["description"],
                    "color": cor,
                    "show_on_sidebar": True,
                })
                if r.status_code in (200, 201):
                    logger.info(f"Label criada: {label['title']} ({cor})")
                else:
                    logger.warning(f"Falha ao criar label '{label['title']}': {r.status_code} {r.text}")
            except Exception as e:
                logger.warning(f"Erro ao criar label '{label['title']}': {e}")


def pasta_cliente(account_id: int) -> str | None:
    for pasta in os.listdir(CLIENTES_DIR):
        if pasta.startswith(f"{account_id}-"):
            return os.path.join(CLIENTES_DIR, pasta)
    return None




@app.get("/health")
def health():
    return {"status": "online", "service": "Agente de Atendimento Da1Click"}


@app.get("/dashboard")
def dashboard():
    return FileResponse(os.path.join(BASE_DIR, "static", "dashboard.html"))


@app.get("/lp")
@app.get("/")
def landing_page(request: Request):
    return FileResponse(os.path.join(BASE_DIR, "static", "lp.html"))


@app.post("/api/demo-chat")
async def demo_chat(request: Request):
    """Chat demo para a landing page — usa OpenAI diretamente."""
    demo_key = os.getenv("OPENAI_DEMO_KEY", "")
    if not demo_key:
        raise HTTPException(status_code=503, detail="Demo indisponível")
    body = await request.json()
    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="Mensagens vazias")
    system_prompt = {
        "role": "system",
        "content": (
            "Você é a Camila, responsável pelo atendimento da AdvBrasil Advogados. "
            "NUNCA diga que é assistente virtual, IA ou robô. Você é a Camila, uma pessoa real que trabalha no escritório. "
            "Esta é uma demonstração ao vivo no site. Seu objetivo é mostrar ao visitante como funciona o atendimento na prática. "
            "Seja simpática, profissional e objetiva. Responda em português brasileiro. "
            "Mostre como você faria a triagem de um caso jurídico: pergunte sobre o tipo de problema (acidente de trabalho, "
            "questão previdenciária, consumidor, etc.), faça perguntas de qualificação (tem laudo? recebe benefício do INSS? "
            "quanto tempo faz?), e ao final diga que o caso parece viável e que pode agendar uma consulta gratuita. "
            "Mantenha as respostas curtas (máximo 2-3 frases). Seja natural e conversacional como no WhatsApp. "
            "Se o visitante perguntar sobre o sistema/produto, explique brevemente o que a AdvBrasil.AI faz: "
            "atendimento automatizado via WhatsApp, triagem inteligente, agendamento automático, disponível 24/7. "
            "Não invente dados reais. Isso é apenas uma demonstração."
        )
    }
    try:
        client = OpenAI(api_key=demo_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[system_prompt] + messages[-10:],
            max_tokens=200,
            temperature=0.7,
        )
        reply = resp.choices[0].message.content
        return {"reply": reply}
    except Exception as e:
        logger.error(f"Erro no demo-chat: {e}")
        raise HTTPException(status_code=500, detail="Erro ao processar mensagem")


@app.get("/api/version")
def get_version():
    version_path = os.path.join(BASE_DIR, "version.txt")
    if os.path.exists(version_path):
        with open(version_path) as f:
            return {"version": f.read().strip()}
    return {"version": "0.0"}


@app.post("/api/clientes/{account_id}/avatar")
async def upload_avatar(account_id: int, file: UploadFile = File(...)):
    avatars_dir = os.path.join(BASE_DIR, "static", "avatars")
    os.makedirs(avatars_dir, exist_ok=True)
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "png"
    if ext not in ("png", "jpg", "jpeg", "webp"):
        raise HTTPException(status_code=400, detail="Formato não suportado. Use PNG, JPG ou WebP.")
    path = os.path.join(avatars_dir, f"{account_id}.{ext}")
    # Remove avatar anterior (pode ter extensão diferente)
    for old in os.listdir(avatars_dir):
        if old.startswith(f"{account_id}."):
            os.remove(os.path.join(avatars_dir, old))
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)
    return {"url": f"/static/avatars/{account_id}.{ext}"}


@app.get("/api/clientes/{account_id}/avatar")
def get_avatar(account_id: int):
    avatars_dir = os.path.join(BASE_DIR, "static", "avatars")
    for ext in ("png", "jpg", "jpeg", "webp"):
        path = os.path.join(avatars_dir, f"{account_id}.{ext}")
        if os.path.exists(path):
            return {"url": f"/static/avatars/{account_id}.{ext}"}
    return {"url": None}


# ── AUTH ──────────────────────────────────────────────────────

@app.post("/auth/login")
async def login(request: Request):
    dados = await request.json()
    email = dados.get("email", "").strip().lower()
    senha = dados.get("senha", "")
    if not email or not senha:
        raise HTTPException(status_code=400, detail="Email e senha são obrigatórios")
    user = get_usuario_por_email(email)
    if not user or not verify_password(senha, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    token = create_token(str(user["id"]), user["email"], user["role"])
    return {
        "token": token,
        "user": {"id": user["id"], "email": user["email"], "nome": user["nome"], "role": user["role"]},
    }


@app.post("/auth/cadastro")
async def cadastro(request: Request):
    """Cadastro público de novo usuário (role=viewer, sem contas)."""
    dados = await request.json()
    nome = dados.get("nome", "").strip()
    email = dados.get("email", "").strip().lower()
    senha = dados.get("senha", "")
    if not nome or not email or not senha:
        raise HTTPException(status_code=400, detail="Nome, email e senha são obrigatórios")
    if len(senha) < 6:
        raise HTTPException(status_code=400, detail="Senha deve ter no mínimo 6 caracteres")
    existente = get_usuario_por_email(email)
    if existente:
        raise HTTPException(status_code=409, detail="Email já cadastrado")
    user = criar_usuario(email=email, password_hash=hash_password(senha), nome=nome, role="viewer")
    if not user:
        raise HTTPException(status_code=500, detail="Erro ao criar usuário")
    token = create_token(str(user["id"]), user["email"], user["role"])
    return {
        "token": token,
        "user": {"id": user["id"], "email": user["email"], "nome": user["nome"], "role": user["role"]},
    }


@app.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    u = get_usuario_por_id(user["sub"])
    if not u or not u.get("ativo"):
        raise HTTPException(status_code=401, detail="Usuário inativo ou não encontrado")
    contas = get_contas_do_usuario(u["id"]) if u["role"] != "super_admin" else []
    return {"id": u["id"], "email": u["email"], "nome": u["nome"], "role": u["role"], "contas": contas}


@app.get("/auth/users")
async def listar_users(user: dict = Depends(get_current_user)):
    require_super_admin(user)
    return listar_usuarios_com_contas()


@app.post("/auth/users")
async def criar_user(request: Request, user: dict = Depends(get_current_user)):
    require_super_admin(user)
    dados = await request.json()
    email = dados.get("email", "").strip().lower()
    senha = dados.get("senha", "").strip()
    nome = dados.get("nome", "").strip()
    role = dados.get("role", "admin")
    if not email or not senha or not nome:
        raise HTTPException(status_code=400, detail="email, senha e nome são obrigatórios")
    if role not in ("super_admin", "admin", "moderador", "agente"):
        raise HTTPException(status_code=400, detail="role inválido")
    existente = get_usuario_por_email(email)
    if existente:
        raise HTTPException(status_code=409, detail="Email já cadastrado")
    novo = criar_usuario(email=email, password_hash=hash_password(senha), nome=nome, role=role)
    return {"id": novo["id"], "email": novo["email"], "nome": novo["nome"], "role": novo["role"]}


@app.put("/auth/users/{user_id}")
async def atualizar_user(user_id: str, request: Request, user: dict = Depends(get_current_user)):
    require_super_admin(user)
    dados = await request.json()
    permitidos = {}
    if "nome" in dados:
        permitidos["nome"] = dados["nome"]
    if "role" in dados and dados["role"] in ("super_admin", "admin", "moderador", "agente"):
        permitidos["role"] = dados["role"]
    if "ativo" in dados:
        permitidos["ativo"] = bool(dados["ativo"])
    if "senha" in dados and dados["senha"]:
        permitidos["password_hash"] = hash_password(dados["senha"])
    if permitidos:
        atualizar_usuario(user_id, permitidos)
    return {"status": "ok"}


@app.post("/auth/users/{user_id}/accounts/{account_id}")
async def atribuir_conta(user_id: str, account_id: int, user: dict = Depends(get_current_user)):
    require_super_admin(user)
    atribuir_conta_usuario(user_id, account_id)
    return {"status": "ok"}


@app.delete("/auth/users/{user_id}/accounts/{account_id}")
async def remover_conta(user_id: str, account_id: int, user: dict = Depends(get_current_user)):
    require_super_admin(user)
    remover_conta_usuario(user_id, account_id)
    return {"status": "ok"}


# ── API CLIENTES ──────────────────────────────────────────────

@app.get("/api/clientes")
def listar_clientes(user: dict = Depends(get_current_user)):
    rows = listar_configs_clientes()
    clientes = []
    for row in rows:
        c = row["config"]
        clientes.append({"account_id": c["account_id"], "nome": c["nome"], "ativo": c.get("ativo", True)})
    # Se não for super_admin, filtra pelas contas atribuídas
    if user.get("role") != "super_admin":
        contas_permitidas = get_contas_do_usuario(user["sub"])
        clientes = [c for c in clientes if c["account_id"] in contas_permitidas]
    return clientes


@app.get("/api/clientes/{account_id}")
def obter_cliente(account_id: int, user: dict = Depends(get_current_user)):
    # Se não for super_admin, verifica permissão
    if user.get("role") != "super_admin":
        contas_permitidas = get_contas_do_usuario(user["sub"])
        if account_id not in contas_permitidas:
            raise HTTPException(status_code=403, detail="Sem permissão para acessar esta conta")
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    return config


PLANOS = {
    1: {"nome": "Plano Inicial", "limite_conversas": 1000, "valor": 1890.00, "excedente_conversa": 1.89},
    2: {"nome": "Plano 2", "limite_conversas": 2000, "valor": 3390.00, "excedente_conversa": 1.70},
    3: {"nome": "Plano 3", "limite_conversas": 3000, "valor": 4320.00, "excedente_conversa": 1.44},
    4: {"nome": "Plano 4", "limite_conversas": 5000, "valor": 6190.00, "excedente_conversa": 1.24},
}


def _ciclo_mes(dia_ciclo: int, data=None):
    """Retorna (ciclo_id, data_inicio, data_fim) com base no dia do ciclo.
    Se dia_ciclo=15 e data=10/mar → ciclo '2026-02' (15/fev a 14/mar).
    Se dia_ciclo=15 e data=20/mar → ciclo '2026-03' (15/mar a 14/abr).
    Se dia_ciclo=1 → funciona como mês calendário normal."""
    from datetime import datetime as _dt, timedelta
    import calendar
    if data is None:
        data = _dt.now()
    dia_ciclo = max(1, min(28, dia_ciclo))  # limita a 28 para evitar problemas com fev

    if data.day >= dia_ciclo:
        # Estamos no ciclo que começa neste mês
        inicio = data.replace(day=dia_ciclo, hour=0, minute=0, second=0, microsecond=0)
        # Fim = dia_ciclo do próximo mês
        if data.month == 12:
            fim = data.replace(year=data.year + 1, month=1, day=dia_ciclo, hour=0, minute=0, second=0, microsecond=0)
        else:
            fim = data.replace(month=data.month + 1, day=dia_ciclo, hour=0, minute=0, second=0, microsecond=0)
        ciclo_id = data.strftime("%Y-%m")
    else:
        # Estamos no ciclo que começou no mês passado
        if data.month == 1:
            inicio = data.replace(year=data.year - 1, month=12, day=dia_ciclo, hour=0, minute=0, second=0, microsecond=0)
        else:
            inicio = data.replace(month=data.month - 1, day=dia_ciclo, hour=0, minute=0, second=0, microsecond=0)
        fim = data.replace(day=dia_ciclo, hour=0, minute=0, second=0, microsecond=0)
        ciclo_id = inicio.strftime("%Y-%m")

    return ciclo_id, inicio.isoformat(), fim.isoformat()


@app.get("/api/clientes/{account_id}/relatorio")
def relatorio_conta(account_id: int, user: dict = Depends(get_current_user)):
    """Relatório de uso da conta baseado no dia do ciclo."""
    if user.get("role") != "super_admin":
        contas_permitidas = get_contas_do_usuario(user["sub"])
        if account_id not in contas_permitidas:
            raise HTTPException(status_code=403, detail="Sem permissão")

    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    from datetime import datetime as _dt, timedelta

    dia_ciclo = config.get("dia_ciclo", 1)
    ciclo_id, data_inicio, data_fim = _ciclo_mes(dia_ciclo)

    # Uso mensal (conversas únicas)
    conversas_mes = contar_uso_mensal(account_id, ciclo_id)

    # Leads por status
    leads = contar_leads_por_status(account_id, data_inicio, data_fim)

    # Agendamentos e transcrições
    agendamentos = contar_agendamentos(account_id, data_inicio, data_fim)
    transcricoes = contar_transcricoes(account_id, data_inicio, data_fim)

    # Plano
    plano_id = config.get("plano", 1)
    plano = PLANOS.get(plano_id, PLANOS[1])
    limite = plano["limite_conversas"]
    excedente = max(0, conversas_mes - limite)
    valor_excedente = round(excedente * plano["excedente_conversa"], 2)

    # Histórico últimos 6 ciclos
    meses_hist = []
    dt = _dt.now()
    for i in range(5, -1, -1):
        d = dt - timedelta(days=i * 30)
        cid, _, _ = _ciclo_mes(dia_ciclo, d)
        meses_hist.append(cid)
    meses_hist = sorted(set(meses_hist))[-6:]
    historico = historico_uso_mensal(account_id, meses_hist)

    return {
        "account_id": account_id,
        "nome": config.get("nome", ""),
        "ciclo_id": ciclo_id,
        "dia_ciclo": dia_ciclo,
        "periodo": {"inicio": data_inicio[:10], "fim": data_fim[:10]},
        "plano": {
            "id": plano_id,
            "nome": plano["nome"],
            "limite_conversas": limite,
            "valor": plano["valor"],
            "excedente_por_conversa": plano["excedente_conversa"],
        },
        "uso": {
            "conversas": conversas_mes,
            "percentual": round((conversas_mes / limite) * 100, 1) if limite else 0,
            "excedente": excedente,
            "valor_excedente": valor_excedente,
        },
        "leads": leads,
        "agendamentos": agendamentos,
        "transcricoes": transcricoes,
        "historico": historico,
    }


@app.get("/api/planos")
def listar_planos():
    return PLANOS


@app.delete("/api/clientes/{account_id}")
async def deletar_cliente(account_id: int, apenas_dashboard: bool = False, user: dict = Depends(get_current_user)):
    require_super_admin(user)
    if not apenas_dashboard:
        # Remover conta no Chatwoot via Platform API
        chatwoot_url = os.getenv("CHATWOOT_URL", "").rstrip("/")
        platform_token = os.getenv("CHATWOOT_PLATFORM_TOKEN", "")
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

    # Remover dados do Supabase
    try:
        deletar_dados_conta(account_id)
    except Exception as e:
        logger.warning(f"Erro ao limpar Supabase para conta {account_id}: {e}")

    # Remover config do Supabase
    try:
        deletar_config_cliente(account_id)
    except Exception as e:
        logger.warning(f"Erro ao remover config Supabase para conta {account_id}: {e}")

    # Remover pasta local (prompts)
    pasta = pasta_cliente(account_id)
    if pasta and os.path.exists(pasta):
        import shutil
        shutil.rmtree(pasta)

    logger.info(f"Conta {account_id} removida (apenas_dashboard={apenas_dashboard})")
    return {"status": "deletado"}


@app.put("/api/clientes/{account_id}")
async def atualizar_cliente(account_id: int, request: Request, user: dict = Depends(get_current_user)):
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    dados = await request.json()
    campos_editaveis = [
        "nome", "ativo", "ia_ativa", "transcricao_ativa", "inatividade_ativa", "chatwoot_url", "chatwoot_token", "openai_api_key", "ia_agent_id",
        "team_id", "inbox_id", "email_agenda", "horas_inicial_busca",
        "quantidade_dias_a_buscar", "duracao_agendamento", "disponibilidade",
        "especialidade", "id_notificacao_convertido", "id_notificacao_cliente",
        "meta_waba_id", "meta_access_token", "template_audiencia",
        "nome_escritorio", "nome_completo", "telefone", "endereco",
    ]
    # Campos restritos ao super_admin
    if user.get("role") == "super_admin":
        campos_editaveis += ["plano", "dia_ciclo"]

    # Detectar mudança no campo "ativo" para suspender/reativar no Chatwoot
    ativo_anterior = config.get("ativo", True)
    ativo_novo = dados.get("ativo", ativo_anterior)

    for campo in campos_editaveis:
        if campo in dados:
            config[campo] = dados[campo]
    salvar_config_cliente(account_id, config)

    # Se o campo "ativo" mudou, atualizar status da conta no Chatwoot (suspended)
    if "ativo" in dados and ativo_novo != ativo_anterior:
        chatwoot_url = os.getenv("CHATWOOT_URL", "").rstrip("/")
        platform_token = os.getenv("CHATWOOT_PLATFORM_TOKEN", "")
        if chatwoot_url and platform_token:
            # status: "suspended" para desativar, "active" para reativar
            status_chatwoot = "active" if ativo_novo else "suspended"
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.patch(
                        f"{chatwoot_url}/platform/api/v1/accounts/{account_id}",
                        headers={"api_access_token": platform_token},
                        json={"status": status_chatwoot},
                    )
                    if r.is_success:
                        logger.info(f"Chatwoot: conta {account_id} → {status_chatwoot}")
                    else:
                        logger.warning(f"Chatwoot: erro ao mudar status da conta {account_id}: {r.status_code} {r.text}")
            except Exception as e:
                logger.warning(f"Erro ao atualizar status Chatwoot da conta {account_id}: {e}")

    return {"status": "ok"}


@app.post("/api/clientes")
async def criar_cliente(request: Request):
    dados = await request.json()
    account_id = dados.get("account_id")
    nome = dados.get("nome", "").strip()
    if not account_id or not nome:
        raise HTTPException(status_code=400, detail="account_id e nome são obrigatórios")
    # Criar pasta de prompts
    pasta = os.path.join(CLIENTES_DIR, f"{account_id}-{nome}")
    os.makedirs(os.path.join(pasta, "prompt"), exist_ok=True)
    # Salvar config no Supabase
    config = {
        "account_id": account_id,
        "nome": nome,
        "ativo": True,
        "openai_api_key": "",
        "chatwoot_url": "",
        "chatwoot_token": ""
    }
    salvar_config_cliente(account_id, config)
    return {"status": "criado", "account_id": account_id}


@app.post("/api/buscar-conta-chatwoot")
async def buscar_conta_chatwoot(request: Request):
    """Busca dados de uma conta existente no Chatwoot (nome, agentes, inboxes)."""
    dados = await request.json()
    chatwoot_url = dados.get("chatwoot_url", "").strip().rstrip("/")
    chatwoot_token = dados.get("chatwoot_token", "").strip()
    account_id = dados.get("account_id")

    if not chatwoot_url or not chatwoot_token or not account_id:
        raise HTTPException(status_code=400, detail="chatwoot_url, chatwoot_token e account_id são obrigatórios")

    headers = {"api_access_token": chatwoot_token}
    base = f"{chatwoot_url}/api/v1/accounts/{account_id}"

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            # Buscar agentes
            r_agents = await client.get(f"{base}/agents", headers=headers)
            if not r_agents.is_success:
                raise HTTPException(status_code=400, detail=f"Falha ao conectar ao Chatwoot: {r_agents.status_code}")
            agents = r_agents.json()

            # Buscar inboxes
            r_inboxes = await client.get(f"{base}/inboxes", headers=headers)
            inboxes_raw = r_inboxes.json() if r_inboxes.is_success else []
            if isinstance(inboxes_raw, dict):
                inboxes_raw = inboxes_raw.get("payload", [])

            # Buscar info da conta
            r_account = await client.get(f"{base}", headers=headers)
            account_name = ""
            if r_account.is_success:
                acc_data = r_account.json()
                account_name = acc_data.get("name", "")

        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Erro ao conectar: {e}")

    agentes = [{"id": a["id"], "name": a.get("name", a.get("email", ""))} for a in agents] if isinstance(agents, list) else []
    inboxes = [{"id": i["id"], "name": i.get("name", ""), "channel_type": i.get("channel_type", "")} for i in inboxes_raw] if isinstance(inboxes_raw, list) else []

    return {"account_name": account_name, "agentes": agentes, "inboxes": inboxes}


@app.post("/api/importar-conta")
async def importar_conta(request: Request):
    """Importa uma conta Chatwoot já existente (só salva config no Supabase)."""
    dados = await request.json()
    account_id = dados.get("account_id")
    nome = dados.get("nome", "").strip()
    chatwoot_url = dados.get("chatwoot_url", "").strip().rstrip("/")
    chatwoot_token = dados.get("chatwoot_token", "").strip()

    if not account_id or not nome:
        raise HTTPException(status_code=400, detail="account_id e nome são obrigatórios")
    if not chatwoot_url or not chatwoot_token:
        raise HTTPException(status_code=400, detail="chatwoot_url e chatwoot_token são obrigatórios")

    # Verificar se a conta já existe
    existente = carregar_config_cliente(account_id)
    if existente:
        raise HTTPException(status_code=409, detail=f"Conta {account_id} já está cadastrada")

    # Testar conexão com o Chatwoot
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                f"{chatwoot_url}/api/v1/accounts/{account_id}/agents",
                headers={"api_access_token": chatwoot_token},
            )
            if not r.is_success:
                raise HTTPException(status_code=400, detail=f"Falha ao conectar ao Chatwoot: {r.status_code} — verifique URL, token e account_id")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Erro ao conectar: {e}")

    # Criar pasta de prompts
    pasta = os.path.join(CLIENTES_DIR, f"{account_id}-{nome.replace(' ', '_')}")
    os.makedirs(os.path.join(pasta, "prompt"), exist_ok=True)

    # Salvar config no Supabase
    config = {
        "account_id": account_id,
        "nome": nome,
        "ativo": True,
        "openai_api_key": dados.get("openai_api_key", ""),
        "chatwoot_url": chatwoot_url,
        "chatwoot_token": chatwoot_token,
        "ia_agent_id": dados.get("ia_agent_id"),
        "team_id": None,
        "inbox_id": None,
        "inboxes": dados.get("inboxes", []),
    }
    salvar_config_cliente(account_id, config)
    logger.info(f"Conta importada: account_id={account_id}, nome={nome}")
    return {"status": "importado", "account_id": account_id}


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
        user_access_token = None
        try:
            r = await client.post(
                f"{chatwoot_url}/platform/api/v1/users",
                json={"name": admin_name, "email": admin_email, "password": senha_gerada},
                headers=headers_platform,
            )
            if r.status_code in (200, 201):
                user_data = r.json()
                user_id = user_data.get("id")
                user_access_token = user_data.get("access_token")
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

        # 4. Criar labels padrão (usa token do admin vinculado à conta)
        if user_access_token:
            await criar_labels_padrao(chatwoot_url, user_access_token, account_id)
        else:
            logger.warning("Sem access_token do admin — labels não criadas")

        # 5. Adicionar admins padrão Da1Click via Platform API
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

    # 6. Salvar config no Supabase + criar pasta de prompts
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
    salvar_config_cliente(account_id, config)

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

        # Registrar uso mensal (1 conversa por ciclo por conversation_id)
        try:
            dia_ciclo = config.get("dia_ciclo", 1)
            ciclo_id, _, _ = _ciclo_mes(dia_ciclo)
            registrar_uso_mensal(account_id, conversation_id, ciclo_id)
        except Exception as e:
            logger.warning(f"Erro ao registrar uso mensal: {e}")

        # Resetar inatividade (cliente respondeu)
        if config.get("inatividade_ativa", True):
            try:
                registrar_atividade(account_id, conversation_id, inbox_id)
            except Exception as e:
                logger.warning(f"Erro ao registrar atividade: {e}")

        ia_ativa = config.get("ia_ativa", True) and ia_agent_id is not None and assignee_id == ia_agent_id

        # Log da mensagem recebida
        logger.info(f"━━━ WEBHOOK [{account_id}] conv={conversation_id} ━━━")
        logger.info(f"📩 Cliente: {nome} ({telefone})")
        logger.info(f"💬 Mensagem: {texto[:300] if texto else '[sem texto]'}")
        if attachments:
            tipos = [a.get('file_type', '?') for a in attachments]
            logger.info(f"📎 Anexos: {tipos}")
        logger.info(f"🤖 IA ativa: {ia_ativa} (assignee={assignee_id}, ia_agent={ia_agent_id})")

        # Verificar se é áudio (só transcreve se transcricao_ativa)
        audio = next((a for a in attachments if a.get("file_type") == "audio"), None)

        if audio and config.get("transcricao_ativa", True):
            msg_id = msg.get("id")
            # Deduplicação: evitar transcrever o mesmo áudio múltiplas vezes (loop)
            if msg_id in _transcricoes_processadas:
                logger.info(f"[{account_id}] Áudio msg_id={msg_id} já transcrito — ignorando")
            else:
                _transcricoes_processadas.add(msg_id)
                # Limitar tamanho do set para não consumir memória indefinidamente
                if len(_transcricoes_processadas) > 10000:
                    _transcricoes_processadas.clear()
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
                            chatwoot_message_id=msg_id,
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


# ── MIGRAÇÃO CONFIG.JSON → SUPABASE ───────────────────────────

@app.post("/api/migrar-configs")
def migrar_configs_para_supabase():
    """Migra todos os config.json locais para o Supabase (usar uma vez)."""
    migrados = []
    if not os.path.exists(CLIENTES_DIR):
        return {"migrados": migrados}
    for pasta in os.listdir(CLIENTES_DIR):
        config_path = os.path.join(CLIENTES_DIR, pasta, "config.json")
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
            account_id = config.get("account_id")
            if account_id:
                salvar_config_cliente(account_id, config)
                migrados.append({"account_id": account_id, "nome": config.get("nome")})
                logger.info(f"Config migrado para Supabase: account_id={account_id}")
    return {"migrados": migrados, "total": len(migrados)}


# ── META TEMPLATES ─────────────────────────────────────────────

META_GRAPH = "https://graph.facebook.com/v19.0"
META_TEMPLATE_FIELDS = "id,name,status,category,language,components,rejected_reason,quality_score"


def _meta_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get_meta_config(account_id: int) -> tuple[str, str]:
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    waba_id = config.get("meta_waba_id", "").strip()
    token = config.get("meta_access_token", "").strip()
    if not waba_id or not token:
        raise HTTPException(status_code=400, detail="meta_waba_id e meta_access_token não configurados para esta conta")
    return waba_id, token


@app.get("/api/clientes/{account_id}/templates")
async def listar_templates(account_id: int, status: str = ""):
    waba_id, token = _get_meta_config(account_id)
    params = {"fields": META_TEMPLATE_FIELDS, "limit": 100}
    if status:
        params["status"] = status.upper()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{META_GRAPH}/{waba_id}/message_templates", headers=_meta_headers(token), params=params)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json())
    return r.json()


@app.post("/api/clientes/{account_id}/templates")
async def criar_template(account_id: int, request: Request):
    waba_id, token = _get_meta_config(account_id)
    payload = await request.json()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{META_GRAPH}/{waba_id}/message_templates",
            headers={**_meta_headers(token), "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=r.json())
    return r.json()


@app.delete("/api/clientes/{account_id}/templates/{template_name}")
async def deletar_template(account_id: int, template_name: str):
    waba_id, token = _get_meta_config(account_id)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(
            f"{META_GRAPH}/{waba_id}/message_templates",
            headers=_meta_headers(token),
            params={"name": template_name},
        )
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=r.status_code, detail=r.json())
    return {"status": "deletado", "template": template_name}


# ── CONFIG INATIVIDADE ────────────────────────────────────────

@app.get("/api/config/inatividade")
def get_inatividade_config():
    path = os.path.join(BASE_DIR, "config", "inatividade.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@app.put("/api/config/inatividade")
async def put_inatividade_config(request: Request):
    dados = await request.json()
    path = os.path.join(BASE_DIR, "config", "inatividade.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
    return {"status": "ok"}


# ── AUDIÊNCIAS ────────────────────────────────────────────────

AUDIENCIAS_PATH = os.path.join(BASE_DIR, "config", "audiencias.json")
TIPOS_AUDIENCIA_PATH = os.path.join(BASE_DIR, "config", "tipos_audiencia.json")

_TIPOS_AUDIENCIA_DEFAULT = [
    {"id": 1, "nome": "Audiência de Conciliação Presencial", "descricao": "Audiência de Conciliação Presencial", "ativo": True, "mensagens": []},
    {"id": 2, "nome": "Audiência de Conciliação Tele", "descricao": "Audiência de Conciliação Tele", "ativo": True, "mensagens": []},
    {"id": 3, "nome": "Audiência de Instrução Presencial", "descricao": "Audiência de Instrução Presencial", "ativo": True, "mensagens": []},
    {"id": 4, "nome": "Audiência de Instrução Tele", "descricao": "Audiência de Instrução Tele", "ativo": True, "mensagens": []},
    {"id": 5, "nome": "Pericia Medica", "descricao": "Pericia Tecnica", "ativo": True, "mensagens": []},
    {"id": 6, "nome": "Pericia Tecnica", "descricao": "Pericia Tecnica", "ativo": True, "mensagens": []},
]


def _load_audiencias() -> list:
    if os.path.exists(AUDIENCIAS_PATH):
        with open(AUDIENCIAS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_audiencias(data: list):
    os.makedirs(os.path.dirname(AUDIENCIAS_PATH), exist_ok=True)
    with open(AUDIENCIAS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _load_tipos_audiencia() -> list:
    if os.path.exists(TIPOS_AUDIENCIA_PATH):
        with open(TIPOS_AUDIENCIA_PATH, encoding="utf-8") as f:
            return json.load(f)
    return [t.copy() for t in _TIPOS_AUDIENCIA_DEFAULT]


def _save_tipos_audiencia(data: list):
    os.makedirs(os.path.dirname(TIPOS_AUDIENCIA_PATH), exist_ok=True)
    with open(TIPOS_AUDIENCIA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


@app.get("/api/audiencias/tipos")
def listar_tipos_audiencia():
    return _load_tipos_audiencia()


@app.post("/api/audiencias/tipos")
async def criar_tipo_audiencia(request: Request):
    dados = await request.json()
    tipos = _load_tipos_audiencia()
    novo_id = max((t["id"] for t in tipos), default=0) + 1
    novo = {
        "id": novo_id,
        "nome": dados.get("nome", ""),
        "descricao": dados.get("descricao", dados.get("nome", "")),
        "ativo": dados.get("ativo", True),
        "mensagens": [],
    }
    tipos.append(novo)
    _save_tipos_audiencia(tipos)
    return novo


@app.put("/api/audiencias/tipos/{tipo_id}")
async def atualizar_tipo_audiencia(tipo_id: int, request: Request):
    dados = await request.json()
    tipos = _load_tipos_audiencia()
    for i, t in enumerate(tipos):
        if t["id"] == tipo_id:
            for campo in ["nome", "descricao", "ativo"]:
                if campo in dados:
                    tipos[i][campo] = dados[campo]
            _save_tipos_audiencia(tipos)
            return tipos[i]
    raise HTTPException(status_code=404, detail="Tipo não encontrado")


@app.delete("/api/audiencias/tipos/{tipo_id}")
def deletar_tipo_audiencia(tipo_id: int):
    tipos = _load_tipos_audiencia()
    nova_lista = [t for t in tipos if t["id"] != tipo_id]
    if len(nova_lista) == len(tipos):
        raise HTTPException(status_code=404, detail="Tipo não encontrado")
    _save_tipos_audiencia(nova_lista)
    return {"status": "deletado"}


# ── MENSAGENS DE TIPO DE AUDIÊNCIA ────────────────────────────

@app.get("/api/audiencias/tipos/{tipo_id}/mensagens")
def listar_mensagens_tipo(tipo_id: int):
    tipos = _load_tipos_audiencia()
    tipo = next((t for t in tipos if t["id"] == tipo_id), None)
    if not tipo:
        raise HTTPException(status_code=404, detail="Tipo não encontrado")
    return tipo.get("mensagens", [])


@app.post("/api/audiencias/tipos/{tipo_id}/mensagens")
async def criar_mensagem_tipo(tipo_id: int, request: Request):
    dados = await request.json()
    tipos = _load_tipos_audiencia()
    for i, t in enumerate(tipos):
        if t["id"] == tipo_id:
            mensagens = t.get("mensagens", [])
            novo_idx = max((m.get("idx", 0) for m in mensagens), default=0) + 1
            nova = {
                "id": secrets.token_hex(16),
                "idx": novo_idx,
                "conteudo": dados.get("conteudo", ""),
                "tempo_antes": dados.get("tempo_antes", 5),
                "unidade_tempo": dados.get("unidade_tempo", "dias"),
                "template_whatsapp": dados.get("template_whatsapp", ""),
                "media_url": dados.get("media_url"),
                "media_type": dados.get("media_type"),
                "media_caption": dados.get("media_caption"),
            }
            mensagens.append(nova)
            tipos[i]["mensagens"] = mensagens
            _save_tipos_audiencia(tipos)
            return nova
    raise HTTPException(status_code=404, detail="Tipo não encontrado")


@app.put("/api/audiencias/tipos/{tipo_id}/mensagens/{msg_id}")
async def atualizar_mensagem_tipo(tipo_id: int, msg_id: str, request: Request):
    dados = await request.json()
    tipos = _load_tipos_audiencia()
    for i, t in enumerate(tipos):
        if t["id"] == tipo_id:
            mensagens = t.get("mensagens", [])
            for j, m in enumerate(mensagens):
                if m["id"] == msg_id:
                    for campo in ["conteudo", "tempo_antes", "unidade_tempo", "template_whatsapp",
                                  "media_url", "media_type", "media_caption", "idx"]:
                        if campo in dados:
                            mensagens[j][campo] = dados[campo]
                    tipos[i]["mensagens"] = mensagens
                    _save_tipos_audiencia(tipos)
                    return mensagens[j]
            raise HTTPException(status_code=404, detail="Mensagem não encontrada")
    raise HTTPException(status_code=404, detail="Tipo não encontrado")


@app.delete("/api/audiencias/tipos/{tipo_id}/mensagens/{msg_id}")
def deletar_mensagem_tipo(tipo_id: int, msg_id: str):
    tipos = _load_tipos_audiencia()
    for i, t in enumerate(tipos):
        if t["id"] == tipo_id:
            mensagens = t.get("mensagens", [])
            nova_lista = [m for m in mensagens if m["id"] != msg_id]
            if len(nova_lista) == len(mensagens):
                raise HTTPException(status_code=404, detail="Mensagem não encontrada")
            tipos[i]["mensagens"] = nova_lista
            _save_tipos_audiencia(tipos)
            return {"status": "deletado"}
    raise HTTPException(status_code=404, detail="Tipo não encontrado")


@app.get("/api/audiencias")
def listar_audiencias(account_id: int = None):
    audiencias = _load_audiencias()
    if account_id is not None:
        audiencias = [a for a in audiencias if a.get("account_id") == account_id]
    return audiencias


@app.post("/api/audiencias")
async def criar_audiencia(request: Request):
    dados = await request.json()
    audiencias = _load_audiencias()
    nova = {
        "id": secrets.token_hex(16),
        "account_id": dados.get("account_id"),
        "conversation_id": dados.get("conversation_id"),
        "inbox_id": dados.get("inbox_id"),
        "nome_cliente": dados.get("nome_cliente", ""),
        "tem_processo": dados.get("tem_processo", False),
        "telefone": dados.get("telefone", ""),
        "data": dados.get("data", ""),
        "horario": dados.get("horario", ""),
        "tipo_audiencia": dados.get("tipo_audiencia", ""),
        "endereco": dados.get("endereco", ""),
        "link_zoom": dados.get("link_zoom", ""),
        "testemunhas": dados.get("testemunhas", []),
    }
    audiencias.append(nova)
    _save_audiencias(audiencias)
    return nova


@app.put("/api/audiencias/{audiencia_id}")
async def atualizar_audiencia(audiencia_id: str, request: Request):
    dados = await request.json()
    audiencias = _load_audiencias()
    campos = ["nome_cliente", "tem_processo", "telefone", "conversation_id", "inbox_id",
              "data", "horario", "tipo_audiencia", "endereco", "link_zoom", "testemunhas", "account_id"]
    for i, a in enumerate(audiencias):
        if a["id"] == audiencia_id:
            for campo in campos:
                if campo in dados:
                    audiencias[i][campo] = dados[campo]
            _save_audiencias(audiencias)
            return audiencias[i]
    raise HTTPException(status_code=404, detail="Audiência não encontrada")


@app.delete("/api/audiencias/{audiencia_id}")
def deletar_audiencia(audiencia_id: str):
    audiencias = _load_audiencias()
    nova_lista = [a for a in audiencias if a["id"] != audiencia_id]
    if len(nova_lista) == len(audiencias):
        raise HTTPException(status_code=404, detail="Audiência não encontrada")
    _save_audiencias(nova_lista)
    return {"status": "deletado"}


# ── CHATWOOT PROXY ────────────────────────────────────────────

@app.get("/api/clientes/{account_id}/chatwoot/contatos/buscar")
async def buscar_contato_chatwoot(account_id: int, q: str = ""):
    """Busca contato no Chatwoot por telefone/nome e retorna dados + conversa mais recente."""
    if not q.strip():
        return []
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    chatwoot_url = config["chatwoot_url"].rstrip("/")
    token = config["chatwoot_token"]
    headers = {"api_access_token": token}

    async with httpx.AsyncClient(timeout=15) as client:
        # Buscar contatos
        r = await client.get(
            f"{chatwoot_url}/api/v1/accounts/{account_id}/contacts/search",
            params={"q": q.strip()}, headers=headers,
        )
        if not r.is_success:
            raise HTTPException(status_code=r.status_code, detail=r.text)

        contatos = r.json().get("payload", [])
        resultado = []

        for c in contatos[:10]:
            item = {
                "contact_id": c.get("id"),
                "nome": c.get("name", ""),
                "telefone": c.get("phone_number", ""),
                "email": c.get("email", ""),
                "conversation_id": None,
                "inbox_id": None,
            }

            # Buscar conversas desse contato para pegar a mais recente
            try:
                rc = await client.get(
                    f"{chatwoot_url}/api/v1/accounts/{account_id}/contacts/{c['id']}/conversations",
                    headers=headers,
                )
                if rc.is_success:
                    conversas = rc.json().get("payload", [])
                    if conversas:
                        # Pegar a conversa mais recente
                        mais_recente = max(conversas, key=lambda x: x.get("last_activity_at", 0))
                        item["conversation_id"] = mais_recente.get("id")
                        item["inbox_id"] = mais_recente.get("inbox_id")
            except Exception:
                pass

            resultado.append(item)

        return resultado


@app.get("/api/clientes/{account_id}/chatwoot/agents")
async def proxy_chatwoot_agents(account_id: int):
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    url = f"{config['chatwoot_url'].rstrip('/')}/api/v1/accounts/{account_id}/agents"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"api_access_token": config["chatwoot_token"]})
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


@app.get("/api/clientes/{account_id}/chatwoot/teams")
async def proxy_chatwoot_teams(account_id: int):
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    url = f"{config['chatwoot_url'].rstrip('/')}/api/v1/accounts/{account_id}/teams"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"api_access_token": config["chatwoot_token"]})
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


@app.get("/api/clientes/{account_id}/chatwoot/inboxes")
async def proxy_chatwoot_inboxes(account_id: int):
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    url = f"{config['chatwoot_url'].rstrip('/')}/api/v1/accounts/{account_id}/inboxes"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"api_access_token": config["chatwoot_token"]})
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json()
    return data.get("payload", data)


# ── WEBHOOK AUDIÊNCIA ─────────────────────────────────────────

@app.post("/webhook/envia-audiencia")
async def webhook_envia_audiencia(request: Request):
    """Recebe webhook de macro do Chatwoot (via n8n) e salva audiência pendente."""
    payload = await request.json()

    # O n8n envia como array — pega o primeiro elemento
    if isinstance(payload, list):
        if not payload:
            raise HTTPException(status_code=400, detail="Payload vazio")
        payload = payload[0]

    body = payload.get("body", payload)

    # Extrair dados do contato e conversa
    messages = body.get("messages", [])
    first_msg = messages[0] if messages else {}
    sender = body.get("meta", {}).get("sender", first_msg.get("sender", {}))
    contact_inbox = body.get("contact_inbox", {})

    account_id = first_msg.get("account_id") or body.get("account_id")
    conversation_id = body.get("id")
    nome_cliente = sender.get("name", "")
    telefone = sender.get("phone_number", "") or contact_inbox.get("source_id", "")

    if not account_id or not conversation_id:
        raise HTTPException(status_code=400, detail="account_id e conversation_id são obrigatórios")

    inbox_id = body.get("inbox_id")

    # Salvar como audiência pendente
    audiencias = _load_audiencias()
    nova = {
        "id": secrets.token_hex(16),
        "account_id": account_id,
        "conversation_id": conversation_id,
        "inbox_id": inbox_id,
        "nome_cliente": nome_cliente,
        "telefone": telefone,
        "tem_processo": False,
        "data": "",
        "horario": "",
        "tipo_audiencia": "",
        "endereco": "",
        "link_zoom": "",
        "testemunhas": [],
        "status": "pendente",
    }
    audiencias.append(nova)
    _save_audiencias(audiencias)

    logger.info(f"[webhook-audiencia] Audiência pendente criada: {nome_cliente} ({telefone}) — conversa {conversation_id}")
    return {"status": "ok", "audiencia_id": nova["id"]}


@app.post("/api/audiencias/{audiencia_id}/enviar")
async def enviar_aviso_audiencia(audiencia_id: str, request: Request):
    """Envia aviso de audiência ao cliente + testemunhas, tratando WhatsApp oficial vs API."""
    from inatividade import _get_inbox_channel_type, _ultima_msg_cliente
    from datetime import datetime, timezone

    dados = await request.json()
    msg_id = dados.get("mensagem_id")

    audiencias = _load_audiencias()
    audiencia = next((a for a in audiencias if a["id"] == audiencia_id), None)
    if not audiencia:
        raise HTTPException(status_code=404, detail="Audiência não encontrada")

    account_id = audiencia["account_id"]
    conversation_id = audiencia["conversation_id"]
    inbox_id = audiencia.get("inbox_id")

    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    # Buscar a mensagem do tipo de audiência
    tipos = _load_tipos_audiencia()
    tipo = next((t for t in tipos if t["nome"] == audiencia.get("tipo_audiencia")), None)
    if not tipo:
        raise HTTPException(status_code=400, detail="Tipo de audiência não encontrado")

    mensagens_tipo = tipo.get("mensagens", [])
    if not mensagens_tipo:
        raise HTTPException(status_code=400, detail="Nenhuma mensagem configurada para este tipo")

    # Se msg_id específico, envia essa; senão, envia todas
    if msg_id:
        msg = next((m for m in mensagens_tipo if m["id"] == msg_id), None)
        if not msg:
            raise HTTPException(status_code=404, detail="Mensagem não encontrada no tipo")
        msgs_para_enviar = [msg]
    else:
        msgs_para_enviar = sorted(mensagens_tipo, key=lambda m: m.get("idx", 0))

    chatwoot_url = config["chatwoot_url"].rstrip("/")
    token = config["chatwoot_token"]

    # Verificar tipo de inbox e janela de 24h (para o cliente principal)
    channel_type = await _get_inbox_channel_type(config, inbox_id)
    is_whatsapp_oficial = "whatsapp" in channel_type.lower()

    # ── Montar lista de destinatários: cliente + testemunhas ──
    destinatarios = [
        {"nome": audiencia["nome_cliente"], "telefone": audiencia.get("telefone", ""),
         "conversation_id": conversation_id, "tipo": "cliente"},
    ]
    for t in audiencia.get("testemunhas", []):
        if isinstance(t, dict) and (t.get("whatsapp") or "").strip():
            destinatarios.append({
                "nome": t.get("nome", "Testemunha"),
                "telefone": t["whatsapp"].strip(),
                "conversation_id": None,  # será buscado/criado
                "tipo": "testemunha",
            })

    resultados = []

    async with httpx.AsyncClient(timeout=20) as http:
        headers = {"api_access_token": token, "Content-Type": "application/json"}

        for dest in destinatarios:
            conv_id = dest["conversation_id"]

            # Para testemunhas: buscar ou criar contato e conversa no Chatwoot
            if conv_id is None and inbox_id:
                try:
                    conv_id = await _buscar_ou_criar_conversa(
                        http, chatwoot_url, token, account_id, inbox_id,
                        dest["nome"], dest["telefone"]
                    )
                except Exception as e:
                    resultados.append({
                        "destinatario": dest["nome"], "telefone": dest["telefone"],
                        "tipo": dest["tipo"], "status": "erro",
                        "detalhe": f"Erro ao criar conversa: {e}",
                    })
                    continue

            if not conv_id:
                resultados.append({
                    "destinatario": dest["nome"], "tipo": dest["tipo"],
                    "status": "erro", "detalhe": "Sem conversation_id e sem inbox_id",
                })
                continue

            # Checar janela 24h para este destinatário
            dest_janela_expirada = False
            if is_whatsapp_oficial:
                try:
                    url_msgs = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conv_id}/messages"
                    resp = await http.get(url_msgs, headers={"api_access_token": token})
                    historico = resp.json().get("payload", []) if resp.is_success else []
                    historico = sorted(historico, key=lambda m: m.get("created_at", 0))
                    ultima_msg = _ultima_msg_cliente(historico)
                    dest_janela_expirada = (
                        ultima_msg is None or
                        (datetime.now(timezone.utc) - ultima_msg).total_seconds() > 86400
                    )
                except Exception:
                    dest_janela_expirada = True  # assume expirada por segurança

            # Enviar cada mensagem para este destinatário
            for msg in msgs_para_enviar:
                # Substituir [NOME] pelo nome do destinatário (não do cliente)
                audiencia_copy = {**audiencia, "nome_cliente": dest["nome"]}
                conteudo = _substituir_placeholders(msg.get("conteudo", ""), audiencia_copy)
                template_name = (msg.get("template_whatsapp") or "").strip()

                try:
                    if is_whatsapp_oficial and dest_janela_expirada:
                        if template_name:
                            await _enviar_template_audiencia_http(
                                http, chatwoot_url, token, account_id, conv_id, template_name
                            )
                            await _enviar_nota_privada_http(
                                http, chatwoot_url, token, account_id, conv_id,
                                f"[Aviso de Audiência] Template enviado: *{template_name}*\nDestinatário: {dest['nome']} ({dest['tipo']})"
                            )
                            resultados.append({
                                "destinatario": dest["nome"], "tipo": dest["tipo"],
                                "msg_id": msg["id"], "metodo": "template", "status": "enviado",
                            })
                        else:
                            resultados.append({
                                "destinatario": dest["nome"], "tipo": dest["tipo"],
                                "msg_id": msg["id"], "metodo": "template", "status": "erro",
                                "detalhe": "Fora da janela 24h e sem template configurado",
                            })
                    else:
                        await _enviar_texto_audiencia_http(
                            http, chatwoot_url, token, account_id, conv_id, conteudo
                        )
                        resultados.append({
                            "destinatario": dest["nome"], "tipo": dest["tipo"],
                            "msg_id": msg["id"], "metodo": "texto", "status": "enviado",
                        })
                except Exception as e:
                    resultados.append({
                        "destinatario": dest["nome"], "tipo": dest["tipo"],
                        "msg_id": msg["id"], "status": "erro", "detalhe": str(e),
                    })

    # Registrar envios na audiência
    enviados = audiencia.get("mensagens_enviadas", [])
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in resultados:
        if r["status"] == "enviado":
            enviados.append({
                "mensagem_id": r.get("msg_id"), "destinatario": r["destinatario"],
                "tipo": r["tipo"], "metodo": r["metodo"], "enviado_em": now_iso,
            })
    for i, a in enumerate(audiencias):
        if a["id"] == audiencia_id:
            audiencias[i]["mensagens_enviadas"] = enviados
            break
    _save_audiencias(audiencias)

    logger.info(f"[audiencia] Envio concluído — {audiencia['nome_cliente']} | {len(resultados)} resultado(s)")
    return {"status": "ok", "resultados": resultados}


# ── HELPERS DE ENVIO AUDIÊNCIA ────────────────────────────────

def _substituir_placeholders(texto: str, audiencia: dict) -> str:
    """Substitui placeholders [NOME], [DATA], [ZOOM], etc. no texto da mensagem."""
    data_raw = audiencia.get("data", "")
    data_fmt = data_raw
    if data_raw and "-" in data_raw:
        try:
            data_fmt = data_raw.split("-")[2] + "/" + data_raw.split("-")[1] + "/" + data_raw.split("-")[0]
        except IndexError:
            pass

    texto = texto.replace("[NOME]", audiencia.get("nome_cliente", ""))
    texto = texto.replace("[DATA]", data_fmt)
    texto = texto.replace("[HORARIO]", audiencia.get("horario", ""))
    texto = texto.replace("[ENDERECO]", audiencia.get("endereco", ""))
    texto = texto.replace("[ZOOM]", audiencia.get("link_zoom", ""))
    texto = texto.replace("[TELEFONE]", audiencia.get("telefone", ""))
    texto = texto.replace("[TIPO]", audiencia.get("tipo_audiencia", ""))
    return texto


async def _buscar_ou_criar_conversa(http: httpx.AsyncClient, chatwoot_url: str, token: str,
                                     account_id: int, inbox_id: int,
                                     nome: str, telefone: str) -> int:
    """Busca contato por telefone no Chatwoot. Se não existe, cria. Depois busca/cria conversa."""
    headers = {"api_access_token": token, "Content-Type": "application/json"}

    # 1. Buscar contato pelo telefone
    search_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/contacts/search"
    resp = await http.get(search_url, params={"q": telefone}, headers={"api_access_token": token})
    contact_id = None
    if resp.is_success:
        contatos = resp.json().get("payload", [])
        for c in contatos:
            if c.get("phone_number", "").replace("+", "").replace(" ", "") == telefone.replace("+", "").replace(" ", ""):
                contact_id = c["id"]
                break

    # 2. Se não encontrou, criar contato
    if not contact_id:
        resp = await http.post(
            f"{chatwoot_url}/api/v1/accounts/{account_id}/contacts",
            headers=headers,
            json={"name": nome, "phone_number": telefone, "inbox_id": inbox_id},
        )
        if resp.is_success:
            contact_id = resp.json().get("id") or resp.json().get("payload", {}).get("contact", {}).get("id")
        if not contact_id:
            raise Exception(f"Falha ao criar contato: {resp.status_code} {resp.text}")

    # 3. Buscar conversa existente do contato neste inbox
    resp = await http.get(
        f"{chatwoot_url}/api/v1/accounts/{account_id}/contacts/{contact_id}/conversations",
        headers={"api_access_token": token},
    )
    if resp.is_success:
        conversas = resp.json().get("payload", [])
        for conv in conversas:
            if conv.get("inbox_id") == inbox_id:
                return conv["id"]

    # 4. Criar nova conversa
    resp = await http.post(
        f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations",
        headers=headers,
        json={"contact_id": contact_id, "inbox_id": inbox_id},
    )
    if resp.is_success:
        return resp.json().get("id")

    raise Exception(f"Falha ao criar conversa: {resp.status_code} {resp.text}")


async def _enviar_template_audiencia_http(http: httpx.AsyncClient, chatwoot_url: str, token: str,
                                           account_id: int, conversation_id: int, template_name: str):
    """Envia template WhatsApp via Chatwoot (reutiliza httpx client)."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    r = await http.post(url, headers=headers, json={
        "message_type": "outgoing",
        "private": False,
        "template_params": {
            "name": template_name,
            "language": "pt_BR",
            "processed_params": {},
        },
    })
    if not r.is_success:
        raise Exception(f"Erro ao enviar template: {r.status_code} {r.text}")


async def _enviar_texto_audiencia_http(http: httpx.AsyncClient, chatwoot_url: str, token: str,
                                        account_id: int, conversation_id: int, texto: str):
    """Envia mensagem de texto livre (reutiliza httpx client)."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    r = await http.post(url, headers=headers, json={
        "content": texto,
        "message_type": "outgoing",
        "private": False,
    })
    if not r.is_success:
        raise Exception(f"Erro ao enviar mensagem: {r.status_code} {r.text}")


async def _enviar_nota_privada_http(http: httpx.AsyncClient, chatwoot_url: str, token: str,
                                     account_id: int, conversation_id: int, texto: str):
    """Envia nota privada na conversa do Chatwoot."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    await http.post(url, headers=headers, json={
        "content": texto,
        "message_type": "outgoing",
        "private": True,
    })


# ══════════════════════════════════════════════
# TERMINAL CLAUDE CODE (WebSocket)
# ══════════════════════════════════════════════

@app.websocket("/ws/terminal")
async def websocket_terminal(ws: WebSocket):
    """WebSocket que conecta ao Claude Code na VPS. Apenas super_admin."""
    # Autenticação via query param
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=4001, reason="Token ausente")
        return
    from auth import decode_token
    try:
        payload = decode_token(token)
        user = get_usuario_por_id(payload["sub"])
        if not user or user.get("role") != "super_admin":
            await ws.close(code=4003, reason="Acesso negado")
            return
    except Exception:
        await ws.close(code=4001, reason="Token inválido")
        return

    await ws.accept()
    await ws.send_text("Claude Code pronto. Digite seu prompt.\n")

    has_history = False  # Controla se já tem conversa ativa

    try:
        while True:
            msg = await ws.receive_text()

            # Comando especial: nova conversa
            if msg.strip() == "__NEW_CONVERSATION__":
                has_history = False
                await ws.send_text("\n🔄 Nova conversa iniciada.\n")
                continue

            # Comando especial: reiniciar app
            if msg.strip() == "__RESTART_APP__":
                await ws.send_text("\n🔄 Reiniciando aplicação...\n")
                try:
                    restart_proc = await asyncio.create_subprocess_exec(
                        "kill", "-HUP", "1",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    await restart_proc.wait()
                    await ws.send_text("✅ Sinal de reinício enviado. A app vai reiniciar em instantes.\n")
                except Exception as e:
                    await ws.send_text(f"[ERRO] {str(e)}\n")
                continue

            prompt = msg.strip()
            if not prompt:
                continue

            await ws.send_text("\n⏳ Processando...\n")

            try:
                cmd = ["claude", "-p", prompt]
                if has_history:
                    cmd.append("--continue")

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd="/app",
                    env={**os.environ, "NO_COLOR": "1"},
                )

                # Stream da resposta em tempo real
                while True:
                    chunk = await proc.stdout.read(1024)
                    if not chunk:
                        break
                    await ws.send_text(chunk.decode("utf-8", errors="replace"))

                await proc.wait()
                has_history = True  # A partir de agora usa --continue
                await ws.send_text("\n\n✅ Pronto.\n")

            except FileNotFoundError:
                await ws.send_text("\n[ERRO] Claude Code não instalado na VPS.\n")
            except Exception as e:
                await ws.send_text(f"\n[ERRO] {str(e)}\n")

    except WebSocketDisconnect:
        pass


@app.post("/api/restart")
async def restart_app(user=Depends(get_current_user)):
    """Reinicia a aplicação (super_admin only)."""
    require_super_admin(user)
    import signal
    os.kill(1, signal.SIGHUP)
    return {"status": "ok", "message": "Reiniciando..."}
