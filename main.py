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
    contar_uso_por_periodo, historico_uso_por_periodos,
    contar_conversas, contar_leads_por_status, contar_agendamentos, contar_transcricoes,
)
from db import (
    listar_audiencias_db, get_audiencia_db, inserir_audiencia_db,
    atualizar_audiencia_db, deletar_audiencia_db,
    listar_hearing_types_db, get_hearing_type_db, inserir_hearing_type_db,
    atualizar_hearing_type_db, deletar_hearing_type_db,
)
from db import (
    listar_advogados, get_advogado, inserir_advogado,
    atualizar_advogado, deletar_advogado,
)
from db import (
    listar_campanhas_remarketing, get_campanha_remarketing,
    inserir_campanha_remarketing, atualizar_campanha_remarketing,
    deletar_campanha_remarketing, contar_envios_remarketing_hoje,
    contar_total_envios_remarketing, listar_envios_remarketing,
    contar_elegiveis_remarketing,
)
from db import (
    criar_onboarding, get_onboarding_by_token, get_onboarding_by_account,
    atualizar_onboarding_draft, submeter_onboarding,
)
from auth import hash_password, verify_password, create_token, get_current_user, require_super_admin
from inatividade import registrar_atividade, iniciar_monitoramento
from remarketing import iniciar_remarketing
import asyncio
import httpx
import json
import logging
import os
import random
import secrets
import string
from collections import OrderedDict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cache de message IDs já processados (evita duplicação por webhook retry)
_MSG_IDS_PROCESSADOS: OrderedDict[int, bool] = OrderedDict()
_MSG_IDS_MAX = 500


@asynccontextmanager
async def lifespan(app: FastAPI):
    iniciar_monitoramento()
    iniciar_remarketing()
    from agendador_audiencias import iniciar_agendador_audiencias
    iniciar_agendador_audiencias()
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


async def criar_funis_padrao(chatwoot_url: str, token: str, account_id: int):
    """Cria os funis (kanban) padrão na conta recém-criada."""
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    base = f"{chatwoot_url}/api/v1/accounts/{account_id}"

    FUNIS = [
        {
            "title": "Comercial",
            "description": "Funil comercial — do lead novo ao contrato",
            "identifier": "pipeline_comercial",
            "color": "#3B82F6",
            "steps": [
                {"title": "Lead Novo", "step_type": "start", "color": "#6366F1", "identifier": "lead_novo"},
                {"title": "Aguardando Atendimento", "step_type": "middle", "color": "#F59E0B", "identifier": "aguardando_atendimento"},
                {"title": "Aguardando Assinatura", "step_type": "middle", "color": "#06B6D4", "identifier": "aguardando_assinatura"},
                {"title": "Contrato Fechado", "step_type": "middle", "color": "#10B981", "identifier": "contrato_fechado"},
                {"title": "Follow-up", "step_type": "middle", "color": "#8B5CF6", "identifier": "followup"},
                {"title": "Leads Desqualificados", "step_type": "middle", "color": "#6B7280", "identifier": "leads_desqualificados"},
                {"title": "Não Respondeu", "step_type": "middle", "color": "#EF4444", "identifier": "nao_respondeu"},
                {"title": "Não Assinou", "step_type": "end", "color": "#9CA3AF", "identifier": "nao_assinou"},
            ],
        },
        {
            "title": "Triagem / Encaminhamento",
            "description": "Casos transferidos, inviáveis ou desqualificados",
            "identifier": "triagem_encaminhamento",
            "color": "#EF4444",
            "steps": [
                {"title": "Transferido", "step_type": "start", "color": "#F97316", "identifier": "transferido"},
                {"title": "Inviável", "step_type": "middle", "color": "#EF4444", "identifier": "inviavel"},
                {"title": "Desqualificado", "step_type": "middle", "color": "#6B7280", "identifier": "desqualificado"},
                {"title": "Não Alfabetizado", "step_type": "middle", "color": "#A855F7", "identifier": "nao_alfabetizado"},
                {"title": "Perdido", "step_type": "middle", "color": "#9CA3AF", "identifier": "perdido"},
                {"title": "Resolvido", "step_type": "end", "color": "#10B981", "identifier": "resolvido"},
            ],
        },
    ]

    async with httpx.AsyncClient(timeout=15) as client:
        for funil in FUNIS:
            try:
                r = await client.post(f"{base}/funnels", headers=headers, json={
                    "title": funil["title"],
                    "description": funil["description"],
                    "identifier": funil["identifier"],
                    "color": funil["color"],
                    "blocked": False,
                })
                if r.status_code not in (200, 201):
                    logger.warning(f"Falha ao criar funil '{funil['title']}': {r.status_code}")
                    continue
                funnel_id = r.json().get("id")
                logger.info(f"Funil criado: {funil['title']} (id={funnel_id})")

                for step in funil["steps"]:
                    rs = await client.post(f"{base}/funnels/{funnel_id}/funnel_steps", headers=headers, json=step)
                    if rs.status_code in (200, 201):
                        logger.info(f"  Etapa criada: {step['title']}")
                    else:
                        logger.warning(f"  Falha etapa '{step['title']}': {rs.status_code}")
            except Exception as e:
                logger.warning(f"Erro ao criar funil '{funil['title']}': {e}")


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



@app.get("/api/version")
def get_version():
    version_path = os.path.join(BASE_DIR, "version.txt")
    version = "0.0"
    updated_at = ""
    if os.path.exists(version_path):
        with open(version_path) as f:
            version = f.read().strip()
        # Data/hora da última modificação do version.txt = último deploy
        mtime = os.path.getmtime(version_path)
        from datetime import datetime, timezone, timedelta
        br_tz = timezone(timedelta(hours=-3))
        dt = datetime.fromtimestamp(mtime, tz=br_tz)
        updated_at = dt.strftime("%d/%m/%Y %H:%M")
    return {"version": version, "updated_at": updated_at}


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
        clientes.append({"account_id": row["account_id"], "nome": row.get("nome", ""), "ativo": row.get("ativo", True)})
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

    # Uso mensal — conta por período de datas (created_at), não por ciclo_id string
    # Isso garante que mudar dia_ciclo ou plano NÃO reseta a contagem
    conversas_mes = contar_uso_por_periodo(account_id, data_inicio, data_fim)

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

    # Histórico últimos 6 ciclos — também por período de datas
    periodos_hist = []
    dt = _dt.now()
    ciclos_vistos = set()
    for i in range(5, -1, -1):
        d = dt - timedelta(days=i * 30)
        cid, ini, fim_h = _ciclo_mes(dia_ciclo, d)
        if cid not in ciclos_vistos:
            ciclos_vistos.add(cid)
            periodos_hist.append({"ciclo_id": cid, "inicio": ini, "fim": fim_h})
    periodos_hist = periodos_hist[-6:]
    historico = historico_uso_por_periodos(account_id, periodos_hist)

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
        "nome", "ativo", "ia_ativa", "transcricao_ativa", "inatividade_ativa", "limite_followup", "chatwoot_url", "chatwoot_token", "openai_api_key", "ia_agent_id",
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

    # Criar funis (kanban) padrão
    try:
        await criar_funis_padrao(chatwoot_url, chatwoot_token, account_id)
    except Exception as e:
        logger.warning(f"Erro ao criar funis para conta {account_id}: {e}")

    # Criar webhook para receber mensagens
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            webhook_url = f"https://api.advbrasil.ai/webhook/chatwoot"
            await client.post(
                f"{chatwoot_url}/api/v1/accounts/{account_id}/webhooks",
                headers={"api_access_token": chatwoot_token, "Content-Type": "application/json"},
                json={"url": webhook_url, "subscriptions": ["message_created"]},
            )
            logger.info(f"Webhook criado para conta {account_id}: {webhook_url}")
    except Exception as e:
        logger.warning(f"Erro ao criar webhook para conta {account_id}: {e}")

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
            # 4b. Criar funis (kanban) padrão
            await criar_funis_padrao(chatwoot_url, user_access_token, account_id)
            # Criar webhook para receber mensagens
            try:
                webhook_url = "https://api.advbrasil.ai/webhook/chatwoot"
                await client.post(
                    f"{chatwoot_url}/api/v1/accounts/{account_id}/webhooks",
                    headers={"api_access_token": user_access_token, "Content-Type": "application/json"},
                    json={"url": webhook_url, "subscriptions": ["message_created"]},
                )
                logger.info(f"Webhook criado para conta {account_id}: {webhook_url}")
            except Exception as e:
                logger.warning(f"Erro ao criar webhook para conta {account_id}: {e}")
        else:
            logger.warning("Sem access_token do admin — labels, funis e webhook não criados")

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

    logger.info(f"🔔 WEBHOOK RECEBIDO — event={event} | keys={list(payload.keys())}")

    if event not in ("automation_event.message_created", "message_created"):
        logger.info(f"🔔 WEBHOOK IGNORADO — event={event}")
        return {"status": "ignorado", "event": event}

    # Normalizar payload: automation_event usa "messages", webhook direto usa campos raiz
    if event == "message_created":
        # Webhook direto: a mensagem vem no payload raiz
        account_id = payload.get("account", {}).get("id") or payload.get("account_id")
        # IMPORTANTE: usar APENAS o payload raiz (a mensagem que disparou o webhook).
        # NÃO usar conversation.messages — ele traz mensagens antigas que já foram
        # processadas, causando loop de respostas duplicadas e envios de madrugada.
        messages = [payload]
    else:
        # Automação: mensagens vêm dentro de "messages"
        account_id = payload.get("messages", [{}])[0].get("account_id") if payload.get("messages") else None
        messages = payload.get("messages", [])

    config = carregar_config_cliente(account_id) if account_id else None

    if not config:
        logger.warning(f"Cliente não encontrado para account_id={account_id}")
        return {"status": "cliente_nao_encontrado"}

    ia_agent_id = config.get("ia_agent_id")

    for msg in messages:
        conversation_id = msg.get("conversation_id") or payload.get("conversation", {}).get("id")
        inbox_id = msg.get("inbox_id") or payload.get("inbox", {}).get("id")
        msg_type = msg.get("message_type")
        contact = msg.get("sender", {})
        nome = contact.get("name", "")
        telefone = contact.get("phone_number", "")

        # Salvar mensagem no histórico (todas: incoming + outgoing)
        try:
            from db import salvar_mensagem
            tipo_str = "incoming" if msg_type in (0, "incoming") else "outgoing"
            attachments_raw = msg.get("attachments", [])
            att_resumo = [{"file_type": a.get("file_type"), "data_url": a.get("data_url", "")} for a in attachments_raw] if attachments_raw else None
            salvar_mensagem(
                account_id=account_id, conversation_id=conversation_id, inbox_id=inbox_id or 0,
                chatwoot_message_id=msg.get("id", 0), message_type=tipo_str,
                content=msg.get("content", ""), sender_name=nome, sender_phone=telefone,
                attachments=att_resumo, created_at=msg.get("created_at"),
            )
        except Exception as e:
            logger.debug(f"Erro ao salvar mensagem no histórico: {e}")

        # Ignorar mensagens do agente (só processar do cliente)
        if msg_type not in (0, "incoming"):
            continue

        # Deduplicação: ignorar mensagens já processadas (webhook retry)
        chatwoot_msg_id = msg.get("id")
        if chatwoot_msg_id and chatwoot_msg_id in _MSG_IDS_PROCESSADOS:
            logger.info(f"🔁 Mensagem {chatwoot_msg_id} já processada — ignorando duplicata")
            continue
        if chatwoot_msg_id:
            _MSG_IDS_PROCESSADOS[chatwoot_msg_id] = True
            if len(_MSG_IDS_PROCESSADOS) > _MSG_IDS_MAX:
                _MSG_IDS_PROCESSADOS.popitem(last=False)

        # Ignorar mensagens de grupo do WhatsApp (@g.us)
        identifier = contact.get("identifier") or ""
        if "@g.us" in identifier:
            logger.info(f"🚫 Ignorando mensagem de grupo WhatsApp: {identifier}")
            continue
        texto = msg.get("content") or ""
        attachments = msg.get("attachments", [])

        # assignee_id: pode vir de vários lugares dependendo do formato do webhook
        conv_data = msg.get("conversation", {}) if msg is not payload else payload.get("conversation", {})
        assignee = conv_data.get("assignee") or conv_data.get("meta", {}).get("assignee")
        assignee_id = assignee.get("id") if isinstance(assignee, dict) else None

        # Fallback: se não encontrou assignee no payload, consultar API do Chatwoot
        if assignee_id is None and ia_agent_id is not None and conversation_id:
            try:
                chatwoot_url = config["chatwoot_url"].rstrip("/")
                chatwoot_token = config["chatwoot_token"]
                async with httpx.AsyncClient(timeout=10) as hc:
                    url_conv = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}"
                    resp_conv = await hc.get(url_conv, headers={"api_access_token": chatwoot_token})
                    if resp_conv.is_success:
                        conv_json = resp_conv.json()
                        api_assignee = conv_json.get("meta", {}).get("assignee") or conv_json.get("assignee")
                        assignee_id = api_assignee.get("id") if isinstance(api_assignee, dict) else None
                        logger.info(f"[assignee-fallback] API retornou assignee_id={assignee_id} para conv={conversation_id}")
            except Exception as e:
                logger.warning(f"[assignee-fallback] Erro ao consultar assignee via API conv={conversation_id}: {e}")

        # Filtro por inboxes: se configurados, só responder nessas inboxes
        inboxes_permitidos = config.get("inboxes", [])
        inbox_permitido = not inboxes_permitidos or inbox_id in inboxes_permitidos

        # Verificar se IA deve responder nesta conversa (antes de qualquer efeito colateral)
        ia_ativa = config.get("ia_ativa", True) and ia_agent_id is not None and assignee_id == ia_agent_id and inbox_permitido

        # Registrar lead no Supabase (sempre — independente da IA)
        try:
            upsert_lead(account_id, inbox_id, conversation_id, nome, telefone)
        except Exception as e:
            logger.warning(f"Erro ao registrar lead no Supabase: {e}")

        # Registrar uso mensal e inatividade APENAS quando IA está ativa nesta conversa
        if ia_ativa:
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

        # === #reset: apagar histórico da conversa para testes ===
        if texto.strip().lower() == "#reset":
            logger.info(f"🔄 #reset detectado — apagando histórico da conversa {conversation_id}")
            try:
                chatwoot_url = config["chatwoot_url"].rstrip("/")
                chatwoot_token = config["chatwoot_token"]
                _headers = {"api_access_token": chatwoot_token}
                # Buscar todas as mensagens da conversa
                msgs_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
                async with httpx.AsyncClient(timeout=15) as hc:
                    r = await hc.get(msgs_url, headers=_headers)
                    all_msgs = r.json().get("payload", []) if r.is_success else []
                    deletados = 0
                    for m in all_msgs:
                        mid = m.get("id")
                        if mid:
                            dr = await hc.delete(f"{msgs_url}/{mid}", headers=_headers)
                            if dr.is_success:
                                deletados += 1
                    logger.info(f"🔄 #reset: {deletados} mensagens deletadas da conversa {conversation_id}")
            except Exception as e:
                logger.warning(f"🔄 #reset erro: {e}")
            return {"status": "reset", "conversation_id": conversation_id}

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


@app.post("/api/clientes/{account_id}/templates/upload-media")
async def upload_media_template(account_id: int, file: UploadFile = File(...)):
    """Faz upload de mídia para a Meta e retorna o handle para usar em templates."""
    from io import BytesIO
    waba_id, token = _get_meta_config(account_id)

    # Ler arquivo
    file_bytes = await file.read()
    file_name = file.filename or "upload"

    # 1. Criar sessão de upload
    async with httpx.AsyncClient(timeout=60) as client:
        # Upload via resumable upload API
        r = await client.post(
            f"{META_GRAPH}/app/uploads",
            headers=_meta_headers(token),
            params={
                "file_name": file_name,
                "file_length": len(file_bytes),
                "file_type": file.content_type or "application/octet-stream",
            },
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.json())

        upload_session_id = r.json().get("id")
        if not upload_session_id:
            raise HTTPException(status_code=500, detail="Falha ao criar sessão de upload")

        # 2. Enviar bytes do arquivo
        r2 = await client.post(
            f"{META_GRAPH}/{upload_session_id}",
            headers={
                "Authorization": f"OAuth {token}",
                "file_offset": "0",
                "Content-Type": file.content_type or "application/octet-stream",
            },
            content=file_bytes,
        )
        if r2.status_code != 200:
            raise HTTPException(status_code=r2.status_code, detail=r2.json())

        handle = r2.json().get("h")
        if not handle:
            raise HTTPException(status_code=500, detail="Falha ao obter handle do upload")

        return {"handle": handle, "file_name": file_name}


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


# ── REMARKETING ──────────────────────────────────────────────

@app.get("/api/remarketing/campanhas")
def listar_campanhas(account_id: int):
    return listar_campanhas_remarketing(account_id)


@app.get("/api/remarketing/campanhas/{campanha_id}")
def get_campanha(campanha_id: int):
    c = get_campanha_remarketing(campanha_id)
    if not c:
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return c


@app.post("/api/remarketing/campanhas")
async def criar_campanha(request: Request):
    dados = await request.json()
    account_id = dados.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id obrigatório")
    return inserir_campanha_remarketing(account_id, dados)


@app.put("/api/remarketing/campanhas/{campanha_id}")
async def atualizar_campanha(campanha_id: int, request: Request):
    dados = await request.json()
    result = atualizar_campanha_remarketing(campanha_id, dados)
    if not result:
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return result


@app.delete("/api/remarketing/campanhas/{campanha_id}")
def deletar_campanha(campanha_id: int):
    if not deletar_campanha_remarketing(campanha_id):
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return {"status": "deletado"}


@app.get("/api/remarketing/campanhas/{campanha_id}/envios")
def listar_envios(campanha_id: int, limite: int = 50, offset: int = 0):
    return listar_envios_remarketing(campanha_id, limite, offset)


@app.get("/api/remarketing/campanhas/{campanha_id}/stats")
def stats_campanha(campanha_id: int):
    campanha = get_campanha_remarketing(campanha_id)
    if not campanha:
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return {
        "envios_hoje": contar_envios_remarketing_hoje(campanha_id),
        "total_envios": contar_total_envios_remarketing(campanha_id),
        "elegiveis": contar_elegiveis_remarketing(
            campanha["account_id"], campanha["dias_inatividade"]
        ),
    }


# ── AUDIÊNCIAS ────────────────────────────────────────────────

@app.get("/api/audiencias/tipos")
def listar_tipos_audiencia(account_id: int = None):
    return listar_hearing_types_db(account_id)


@app.post("/api/audiencias/tipos")
async def criar_tipo_audiencia(request: Request):
    dados = await request.json()
    return inserir_hearing_type_db(
        nome=dados.get("nome", ""),
        descricao=dados.get("descricao", dados.get("nome", "")),
        ativo=dados.get("ativo", True),
        id_account=dados.get("account_id"),
    )


@app.put("/api/audiencias/tipos/{tipo_id}")
async def atualizar_tipo_audiencia(tipo_id: str, request: Request):
    dados = await request.json()
    resultado = atualizar_hearing_type_db(tipo_id, dados)
    if not resultado:
        raise HTTPException(status_code=404, detail="Tipo não encontrado")
    return resultado


@app.delete("/api/audiencias/tipos/{tipo_id}")
def deletar_tipo_audiencia(tipo_id: str):
    if not deletar_hearing_type_db(tipo_id):
        raise HTTPException(status_code=404, detail="Tipo não encontrado")
    return {"status": "deletado"}


# ── MENSAGENS DE TIPO DE AUDIÊNCIA ────────────────────────────

@app.get("/api/audiencias/tipos/{tipo_id}/mensagens")
def listar_mensagens_tipo(tipo_id: str):
    tipo = get_hearing_type_db(tipo_id)
    if not tipo:
        raise HTTPException(status_code=404, detail="Tipo não encontrado")
    return tipo.get("mensagens", [])


@app.post("/api/audiencias/tipos/{tipo_id}/mensagens")
async def criar_mensagem_tipo(tipo_id: str, request: Request):
    dados = await request.json()
    tipo = get_hearing_type_db(tipo_id)
    if not tipo:
        raise HTTPException(status_code=404, detail="Tipo não encontrado")
    mensagens = tipo.get("mensagens", [])
    novo_idx = max((m.get("idx", 0) for m in mensagens), default=0) + 1
    nova = {
        "id": secrets.token_hex(16),
        "idx": novo_idx,
        "conteudo": dados.get("conteudo", ""),
        "tempo_antes": dados.get("tempo_antes", 5),
        "unidade_tempo": dados.get("unidade_tempo", "dias"),
        "template_whatsapp": dados.get("template_whatsapp", ""),
        "template_vars": dados.get("template_vars", {}),
        "media_url": dados.get("media_url"),
        "media_type": dados.get("media_type"),
        "media_caption": dados.get("media_caption"),
    }
    mensagens.append(nova)
    atualizar_hearing_type_db(tipo_id, {"mensagens": mensagens})
    return nova


@app.put("/api/audiencias/tipos/{tipo_id}/mensagens/{msg_id}")
async def atualizar_mensagem_tipo(tipo_id: str, msg_id: str, request: Request):
    dados = await request.json()
    tipo = get_hearing_type_db(tipo_id)
    if not tipo:
        raise HTTPException(status_code=404, detail="Tipo não encontrado")
    mensagens = tipo.get("mensagens", [])
    for j, m in enumerate(mensagens):
        if m["id"] == msg_id:
            for campo in ["conteudo", "tempo_antes", "unidade_tempo", "template_whatsapp",
                          "template_vars", "media_url", "media_type", "media_caption", "idx"]:
                if campo in dados:
                    mensagens[j][campo] = dados[campo]
            atualizar_hearing_type_db(tipo_id, {"mensagens": mensagens})
            return mensagens[j]
    raise HTTPException(status_code=404, detail="Mensagem não encontrada")


@app.delete("/api/audiencias/tipos/{tipo_id}/mensagens/{msg_id}")
def deletar_mensagem_tipo(tipo_id: str, msg_id: str):
    tipo = get_hearing_type_db(tipo_id)
    if not tipo:
        raise HTTPException(status_code=404, detail="Tipo não encontrado")
    mensagens = tipo.get("mensagens", [])
    nova_lista = [m for m in mensagens if m["id"] != msg_id]
    if len(nova_lista) == len(mensagens):
        raise HTTPException(status_code=404, detail="Mensagem não encontrada")
    atualizar_hearing_type_db(tipo_id, {"mensagens": nova_lista})
    return {"status": "deletado"}


@app.get("/api/audiencias")
def listar_audiencias(account_id: int = None):
    return listar_audiencias_db(account_id)


@app.post("/api/audiencias/corrigir-inbox")
async def corrigir_inbox_audiencias():
    """Preenche inbox_id de todas as audiências que estão sem, buscando do Chatwoot."""
    audiencias = listar_audiencias_db()
    corrigidos = 0
    for aud in audiencias:
        if aud.get("inbox_id"):
            continue
        conv_id = aud.get("conversation_id")
        account_id = aud.get("account_id")
        if not conv_id or not account_id:
            continue
        config = carregar_config_cliente(account_id)
        if not config:
            continue
        chatwoot_url = config["chatwoot_url"].rstrip("/")
        token = config["chatwoot_token"]
        inbox_id = await _buscar_inbox_da_conversa(chatwoot_url, token, account_id, conv_id)
        if inbox_id:
            atualizar_audiencia_db(aud["id"], {"inbox_id": inbox_id})
            corrigidos += 1
            logger.info(f"[audiencia] inbox_id={inbox_id} corrigido para {aud['nome_cliente']}")
    return {"status": "ok", "corrigidos": corrigidos, "total": len(audiencias)}


@app.post("/api/audiencias")
async def criar_audiencia(request: Request):
    dados = await request.json()
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
        "status": "pendente",
    }
    return inserir_audiencia_db(nova)


@app.put("/api/audiencias/{audiencia_id}")
async def atualizar_audiencia(audiencia_id: str, request: Request):
    dados = await request.json()
    campos = ["nome_cliente", "tem_processo", "telefone", "conversation_id", "inbox_id",
              "data", "horario", "tipo_audiencia", "endereco", "link_zoom", "testemunhas", "account_id"]
    update = {campo: dados[campo] for campo in campos if campo in dados}
    resultado = atualizar_audiencia_db(audiencia_id, update)
    if not resultado:
        raise HTTPException(status_code=404, detail="Audiência não encontrada")
    return resultado


@app.delete("/api/audiencias/{audiencia_id}")
def deletar_audiencia(audiencia_id: str):
    if not deletar_audiencia_db(audiencia_id):
        raise HTTPException(status_code=404, detail="Audiência não encontrada")
    return {"status": "deletado"}


# ── ADVOGADOS ────────────────────────────────────────────────

@app.get("/api/advogados")
def api_listar_advogados(account_id: int):
    return listar_advogados(account_id)


@app.get("/api/advogados/{advogado_id}")
def api_get_advogado(advogado_id: str):
    adv = get_advogado(advogado_id)
    if not adv:
        raise HTTPException(status_code=404, detail="Advogado não encontrado")
    return adv


@app.post("/api/advogados")
async def api_criar_advogado(request: Request):
    dados = await request.json()
    campos = {
        "account_id": dados.get("account_id"),
        "nome": dados.get("nome", ""),
        "especialidade": dados.get("especialidade", ""),
        "cor_id": dados.get("cor_id", 0),
        "duracao_agendamento": dados.get("duracao_agendamento", 30),
        "horas_inicial_busca": dados.get("horas_inicial_busca", 0),
        "quantidade_dias_a_buscar": dados.get("quantidade_dias_a_buscar", 14),
        "disponibilidade": dados.get("disponibilidade", '{"0":[],"1":[],"2":[],"3":[],"4":[],"5":[],"6":[]}'),
        "ativo": dados.get("ativo", True),
    }
    if not campos["account_id"] or not campos["nome"]:
        raise HTTPException(status_code=400, detail="account_id e nome são obrigatórios")
    return inserir_advogado(campos)


@app.put("/api/advogados/{advogado_id}")
async def api_atualizar_advogado(advogado_id: str, request: Request):
    dados = await request.json()
    campos_editaveis = [
        "nome", "especialidade", "cor_id", "duracao_agendamento",
        "horas_inicial_busca", "quantidade_dias_a_buscar", "disponibilidade", "ativo",
    ]
    payload = {k: dados[k] for k in campos_editaveis if k in dados}
    resultado = atualizar_advogado(advogado_id, payload)
    if not resultado:
        raise HTTPException(status_code=404, detail="Advogado não encontrado")
    return resultado


@app.delete("/api/advogados/{advogado_id}")
def api_deletar_advogado(advogado_id: str):
    if not deletar_advogado(advogado_id):
        raise HTTPException(status_code=404, detail="Advogado não encontrado")
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


@app.get("/api/clientes/{account_id}/chatwoot/groups")
async def proxy_chatwoot_groups(account_id: int):
    """Busca contatos de grupos WhatsApp (@g.us / GRUPO) no Chatwoot."""
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    base = config["chatwoot_url"].rstrip("/")
    headers = {"api_access_token": config["chatwoot_token"]}
    groups = []
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{base}/api/v1/accounts/{account_id}/contacts/search",
                params={"q": "GRUPO", "page": 1},
                headers=headers,
            )
            if r.is_success:
                payload = r.json().get("payload", r.json())
                for c in payload if isinstance(payload, list) else []:
                    identifier = c.get("identifier") or ""
                    name = c.get("name") or ""
                    if "@g.us" in identifier or "(GRUPO)" in name.upper():
                        groups.append({"id": c["id"], "label": name})
    except Exception:
        pass
    return groups


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


@app.post("/api/chatwoot/buscar-inboxes")
async def buscar_inboxes_direto(request: Request):
    """Busca inboxes do Chatwoot usando URL, token e account_id informados diretamente."""
    body = await request.json()
    chatwoot_url = (body.get("chatwoot_url") or "").strip().rstrip("/")
    chatwoot_token = (body.get("chatwoot_token") or "").strip()
    account_id = body.get("account_id")
    if not chatwoot_url or not chatwoot_token or not account_id:
        raise HTTPException(status_code=400, detail="chatwoot_url, chatwoot_token e account_id são obrigatórios")
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/inboxes"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"api_access_token": chatwoot_token})
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=f"Chatwoot retornou {r.status_code}: {r.text[:200]}")
    data = r.json()
    return data.get("payload", data)


@app.post("/api/clientes/{account_id}/recriar-funis")
async def recriar_funis_conta(account_id: int):
    """Recria os funis (kanban) padrão em uma conta existente."""
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    chatwoot_url = config.get("chatwoot_url", "").strip()
    chatwoot_token = config.get("chatwoot_token", "").strip()
    if not chatwoot_url or not chatwoot_token:
        raise HTTPException(status_code=400, detail="chatwoot_url e chatwoot_token não configurados")
    await criar_funis_padrao(chatwoot_url, chatwoot_token, account_id)
    return {"status": "ok", "detail": f"Funis recriados para conta {account_id}"}


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

    # Salvar como audiência pendente no Supabase
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
    inserir_audiencia_db(nova)

    logger.info(f"[webhook-audiencia] Audiência pendente criada: {nome_cliente} ({telefone}) — conversa {conversation_id}")
    return {"status": "ok", "audiencia_id": nova["id"]}


@app.post("/api/audiencias/{audiencia_id}/enviar")
async def enviar_aviso_audiencia(audiencia_id: str, request: Request):
    """Envia aviso de audiência ao cliente + testemunhas."""
    dados = await request.json()
    msg_id = dados.get("mensagem_id")
    result = await _enviar_aviso_audiencia_core(audiencia_id, msg_id)
    if "error" in result:
        raise HTTPException(status_code=result.get("status_code", 400), detail=result["error"])
    return result


async def _enviar_aviso_audiencia_core(audiencia_id: str, msg_id: str = None) -> dict:
    """Core: envia aviso de audiência ao cliente + testemunhas, tratando WhatsApp oficial vs API."""
    from inatividade import _get_inbox_channel_type, _ultima_msg_cliente
    from datetime import datetime, timezone

    audiencia = get_audiencia_db(audiencia_id)
    if not audiencia:
        return {"error": "Audiência não encontrada", "status_code": 404}

    account_id = audiencia["account_id"]
    conversation_id = audiencia["conversation_id"]
    inbox_id = audiencia.get("inbox_id")

    config = carregar_config_cliente(account_id)
    if not config:
        return {"error": "Cliente não encontrado", "status_code": 404}

    # Buscar a mensagem do tipo de audiência
    tipos = listar_hearing_types_db()
    tipo = next((t for t in tipos if t["nome"] == audiencia.get("tipo_audiencia")), None)
    if not tipo:
        return {"error": "Tipo de audiência não encontrado", "status_code": 400}

    mensagens_tipo = tipo.get("mensagens", [])
    if not mensagens_tipo:
        return {"error": "Nenhuma mensagem configurada para este tipo", "status_code": 400}

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

    # Auto-buscar inbox_id se não tiver (a partir da conversa no Chatwoot)
    if not inbox_id and conversation_id:
        inbox_id = await _buscar_inbox_da_conversa(chatwoot_url, token, account_id, conversation_id)
        if inbox_id:
            atualizar_audiencia_db(audiencia_id, {"inbox_id": inbox_id})
            logger.info(f"[audiencia] inbox_id={inbox_id} auto-preenchido para {audiencia['nome_cliente']}")

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
                            # Montar processed_params com mapeamento de variáveis salvo
                            tpl_vars = msg.get("template_vars") or {}
                            pp = _build_template_params(audiencia_copy, tpl_vars)
                            await _enviar_template_audiencia_http(
                                http, chatwoot_url, token, account_id, conv_id, template_name, pp
                            )
                            # Nota privada com a mensagem completa como o cliente recebeu
                            msg_completa = await _montar_texto_template_completo(
                                template_name, pp, account_id
                            )
                            await _enviar_nota_privada_http(
                                http, chatwoot_url, token, account_id, conv_id,
                                f"📋 *[Aviso de Audiência — Enviado via Template]*\n\n{msg_completa}"
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

    # Registrar envios na audiência (Supabase)
    enviados = audiencia.get("mensagens_enviadas") or []
    now_iso = datetime.now(timezone.utc).isoformat()
    convs_enviadas = set()
    for r in resultados:
        if r["status"] == "enviado":
            enviados.append({
                "mensagem_id": r.get("msg_id"), "destinatario": r["destinatario"],
                "tipo": r["tipo"], "metodo": r["metodo"], "enviado_em": now_iso,
            })
    atualizar_audiencia_db(audiencia_id, {"mensagens_enviadas": enviados})

    # Abrir conversa no Chatwoot após envio
    async with httpx.AsyncClient(timeout=10) as http:
        for dest in destinatarios:
            conv_id = dest.get("conversation_id")
            if conv_id and conv_id not in convs_enviadas:
                convs_enviadas.add(conv_id)
                try:
                    await http.post(
                        f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conv_id}/toggle_status",
                        headers={"api_access_token": token, "Content-Type": "application/json"},
                        json={"status": "open"},
                    )
                except Exception:
                    pass

    logger.info(f"[audiencia] Envio concluído — {audiencia['nome_cliente']} | {len(resultados)} resultado(s)")
    return {"status": "ok", "resultados": resultados}


# ── HELPERS DE ENVIO AUDIÊNCIA ────────────────────────────────

def _montar_texto_template(template_name: str, pp: dict, tipos: list = None) -> str:
    """Monta o texto completo do template substituindo {{1}}, {{2}} pelos valores reais."""
    # Montar texto a partir das variáveis
    partes = []
    for num in sorted(pp.keys(), key=lambda x: int(x)):
        partes.append(pp[num])

    # Texto descritivo com os valores reais
    texto = f"*Template:* {template_name}\n\n"
    texto += "\n".join(f"• {v}" for v in partes if v and v != "-")
    return texto


# Cache de textos de templates da Meta (preenchido sob demanda)
_cache_template_body: dict[str, str] = {}


async def _montar_texto_template_completo(
    template_name: str, pp: dict, account_id: int
) -> str:
    """Busca o body do template na Meta e substitui as variáveis pelos valores reais."""
    # Tentar cache
    cache_key = f"{account_id}:{template_name}"
    if cache_key not in _cache_template_body:
        try:
            waba_id, token = _get_meta_config(account_id)
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{META_GRAPH}/{waba_id}/message_templates",
                    headers=_meta_headers(token),
                    params={"name": template_name, "fields": "components"},
                )
                if r.is_success:
                    templates = r.json().get("data", [])
                    if templates:
                        comps = templates[0].get("components", [])
                        body = next((c for c in comps if c["type"] == "BODY"), None)
                        if body:
                            _cache_template_body[cache_key] = body["text"]
        except Exception:
            pass

    body_text = _cache_template_body.get(cache_key, "")
    if body_text:
        # Substituir {{1}}, {{2}}, etc. pelos valores reais
        for num, valor in pp.items():
            body_text = body_text.replace(f"{{{{{num}}}}}", valor)
        return body_text

    # Fallback: listar valores
    return "\n".join(f"• {v}" for v in pp.values() if v and v != "-")


def _build_template_params(audiencia: dict, template_vars: dict) -> dict:
    """Monta processed_params para templates WhatsApp usando o mapeamento de variáveis salvo.
    template_vars ex: {"1": "[NOME]", "2": "[DATA]", "3": "[HORARIO]"}
    """
    placeholder_map = _build_placeholder_map(audiencia)
    params = {}
    for num, placeholder in template_vars.items():
        valor = placeholder_map.get(placeholder, "")
        params[str(num)] = valor or "-"
    return params


def _formatar_data_completa(data_raw: str, horario: str = "") -> str:
    """Formata data como 'Segunda-feira, dia 10 de Janeiro de 2026 às 14:30'."""
    if not data_raw or "-" not in data_raw:
        return ""
    try:
        from datetime import date as _date
        partes = data_raw.split("-")
        d = _date(int(partes[0]), int(partes[1]), int(partes[2]))
        dias_semana = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira",
                       "Sexta-feira", "Sábado", "Domingo"]
        meses = ["", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                 "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
        resultado = f"{dias_semana[d.weekday()]}, dia {d.day} de {meses[d.month]} de {d.year}"
        if horario:
            resultado += f" às {horario}"
        return resultado
    except (ValueError, IndexError):
        return ""


def _build_placeholder_map(audiencia: dict) -> dict:
    """Retorna mapa de placeholder -> valor para uma audiência."""
    data_raw = audiencia.get("data", "")
    data_fmt = data_raw
    if data_raw and "-" in data_raw:
        try:
            data_fmt = data_raw.split("-")[2] + "/" + data_raw.split("-")[1] + "/" + data_raw.split("-")[0]
        except IndexError:
            pass
    horario = audiencia.get("horario", "")
    return {
        "[NOME]": audiencia.get("nome_cliente", ""),
        "[DATA]": data_fmt,
        "[HORARIO]": horario,
        "[ENDERECO]": audiencia.get("endereco", ""),
        "[ZOOM]": audiencia.get("link_zoom", ""),
        "[TELEFONE]": audiencia.get("telefone", ""),
        "[TIPO]": audiencia.get("tipo_audiencia", ""),
        "[DATA_COMPLETA]": _formatar_data_completa(data_raw, horario),
    }


def _substituir_placeholders(texto: str, audiencia: dict) -> str:
    """Substitui placeholders [NOME], [DATA], [ZOOM], etc. no texto da mensagem."""
    data_raw = audiencia.get("data", "")
    data_fmt = data_raw
    if data_raw and "-" in data_raw:
        try:
            data_fmt = data_raw.split("-")[2] + "/" + data_raw.split("-")[1] + "/" + data_raw.split("-")[0]
        except IndexError:
            pass

    horario = audiencia.get("horario", "")
    texto = texto.replace("[NOME]", audiencia.get("nome_cliente", ""))
    texto = texto.replace("[DATA]", data_fmt)
    texto = texto.replace("[HORARIO]", horario)
    texto = texto.replace("[DATA_COMPLETA]", _formatar_data_completa(data_raw, horario))
    texto = texto.replace("[ENDERECO]", audiencia.get("endereco", ""))
    texto = texto.replace("[ZOOM]", audiencia.get("link_zoom", ""))
    texto = texto.replace("[TELEFONE]", audiencia.get("telefone", ""))
    texto = texto.replace("[TIPO]", audiencia.get("tipo_audiencia", ""))
    return texto


async def _buscar_inbox_da_conversa(chatwoot_url: str, token: str,
                                     account_id: int, conversation_id: int) -> int | None:
    """Busca o inbox_id de uma conversa existente no Chatwoot."""
    try:
        url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}"
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(url, headers={"api_access_token": token})
            if resp.is_success:
                return resp.json().get("inbox_id")
    except Exception as e:
        logger.warning(f"[audiencia] Erro ao buscar inbox da conversa {conversation_id}: {e}")
    return None


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
                                           account_id: int, conversation_id: int, template_name: str,
                                           processed_params: dict | None = None):
    """Envia template WhatsApp via Chatwoot (reutiliza httpx client)."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    r = await http.post(url, headers=headers, json={
        "message_type": "outgoing",
        "private": False,
        "template_params": {
            "name": template_name,
            "language": "pt_BR",
            "processed_params": processed_params or {},
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
# TERMINAL - EXPLORADOR DE ARQUIVOS
# ══════════════════════════════════════════════

REPO_DIR = "/repo" if os.path.isdir("/repo") else os.path.dirname(os.path.abspath(__file__))
EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", ".env", ".venv", "venv"}

def _scan_dir(base: str, rel: str = "") -> list:
    """Varre diretório recursivamente e retorna árvore de arquivos."""
    result = []
    full = os.path.join(base, rel) if rel else base
    try:
        entries = sorted(os.listdir(full), key=lambda x: (not os.path.isdir(os.path.join(full, x)), x.lower()))
    except PermissionError:
        return result
    for name in entries:
        if name in EXCLUDED_DIRS:
            continue
        entry_path = os.path.join(rel, name) if rel else name
        full_path = os.path.join(full, name)
        if os.path.isdir(full_path):
            children = _scan_dir(base, entry_path)
            result.append({"name": name, "type": "dir", "path": entry_path, "children": children})
        else:
            result.append({"name": name, "type": "file", "path": entry_path})
    return result

@app.get("/api/terminal/files")
async def terminal_list_files(user=Depends(require_super_admin)):
    """Lista arquivos do /repo em árvore (exclui .git, node_modules, etc.)."""
    if not os.path.isdir(REPO_DIR):
        raise HTTPException(404, "Diretório /repo não encontrado")
    tree = _scan_dir(REPO_DIR)
    return tree

@app.get("/api/terminal/file")
async def terminal_read_file(path: str, user=Depends(require_super_admin)):
    """Retorna conteúdo de um arquivo do /repo (somente leitura, max 500KB)."""
    if ".." in path:
        raise HTTPException(400, "Path inválido")
    full_path = os.path.join(REPO_DIR, path)
    if not os.path.isfile(full_path):
        raise HTTPException(404, "Arquivo não encontrado")
    size = os.path.getsize(full_path)
    if size > 500 * 1024:
        raise HTTPException(413, f"Arquivo muito grande ({size // 1024}KB). Limite: 500KB")
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(500, f"Erro ao ler arquivo: {e}")
    return {"path": path, "content": content, "size": size}


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

    # Garantir que o usuário claude tem acesso ao /repo e ao .claude
    try:
        for path in ["/repo", "/home/claude/.claude"]:
            fix_perms = await asyncio.create_subprocess_exec(
                "chown", "-R", "claude:claude", path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await fix_perms.wait()
    except Exception:
        pass

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
                # Restaurar .claude.json se necessário
                restore = await asyncio.create_subprocess_shell(
                    'BACKUP=$(ls -t /home/claude/.claude/backups/.claude.json.backup.* 2>/dev/null | head -1); '
                    '[ -n "$BACKUP" ] && [ ! -f /home/claude/.claude.json ] && cp "$BACKUP" /home/claude/.claude.json; '
                    'chown claude:claude /home/claude/.claude.json 2>/dev/null; true',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await restore.wait()

                cmd = ["su", "-s", "/bin/bash", "claude", "-c"]
                claude_cmd = f'claude -p "{prompt.replace(chr(34), chr(92)+chr(34))}" --dangerously-skip-permissions'
                if has_history:
                    claude_cmd += " --continue"
                cmd.append(claude_cmd)

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd="/repo",
                    env={**os.environ, "NO_COLOR": "1", "HOME": "/home/claude"},
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


# ══════════════════════════════════════════════
# PROMPT VIEWER
# ══════════════════════════════════════════════

import re as _re

def _limpar_prompt_tecnico(texto: str) -> str:
    """Remove seções técnicas do prompt (tools, function calls, variáveis)."""
    linhas = texto.split("\n")
    resultado = []
    pular_secao = False
    for linha in linhas:
        # Detecta início de seção de TOOLS
        if _re.match(r"^##\s*(TOOLS?\s+DISPON[IÍ]VEIS|FERRAMENTAS)", linha, _re.IGNORECASE):
            pular_secao = True
            continue
        # Nova seção ## encerra o pulo
        if pular_secao and _re.match(r"^##\s+", linha):
            pular_secao = False
        if pular_secao:
            continue
        # Remove linhas que mencionam acionar/chamar tools específicas
        if _re.match(r"^-\s*(ConsultarAgenda|Agendar|convertido|lead_disponivel|cliente_inviavel|TransferHuman|atualiza_contato|desqualificado|nao_alfabetizado|aguardando_cliente)\s*[:\(]", linha):
            continue
        # Remove referências a "acionar/Acionar TOOL_NAME"
        linha_limpa = _re.sub(r"\b[Aa]cionar\s+(cliente_inviavel|TransferHuman|ConsultarAgenda|Agendar|convertido|lead_disponivel|desqualificado|nao_alfabetizado|aguardando_cliente)\b\.?", "seguir o procedimento adequado", linha)
        # Remove menções soltas a nomes de tools no meio do texto
        linha_limpa = _re.sub(r"\b(ConsultarAgenda|TransferHuman|atualiza_contato|lead_disponivel|cliente_inviavel|aguardando_cliente)\b", "procedimento interno", linha_limpa)
        # Remove variáveis de template
        linha_limpa = _re.sub(r"\{data_hora_atual\}", "(data/hora atual)", linha_limpa)
        linha_limpa = _re.sub(r"\{conversa\}", "(histórico da conversa)", linha_limpa)
        resultado.append(linha_limpa)
    # Remove linhas vazias consecutivas (máx 2)
    texto_final = "\n".join(resultado)
    texto_final = _re.sub(r"\n{4,}", "\n\n\n", texto_final)
    return texto_final.strip()


@app.get("/api/clientes/{account_id}/prompt")
async def get_prompt_cliente(account_id: int, user: dict = Depends(get_current_user)):
    """Retorna o prompt limpo (sem partes técnicas) para visualização no dashboard."""
    pasta = pasta_cliente(account_id)
    if not pasta:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    prompt_dir = os.path.join(pasta, "prompt")
    if not os.path.isdir(prompt_dir):
        raise HTTPException(status_code=404, detail="Nenhum prompt encontrado para esta conta")

    # Ordem de leitura dos arquivos
    ordem = ["base.md", "identificacao.md", "vinculo.md", "coleta_caso.md",
             "avaliacao.md", "casos_especiais.md", "explicacao.md",
             "agendamento.md", "inatividade.md", "supervisor.md"]

    arquivos = []
    # Primeiro os da ordem, depois os extras
    existentes = set(os.listdir(prompt_dir))
    for arq in ordem:
        if arq in existentes:
            arquivos.append(arq)
    for arq in sorted(existentes):
        if arq.endswith(".md") and arq not in arquivos:
            arquivos.append(arq)

    prompts = []
    for arq in arquivos:
        caminho = os.path.join(prompt_dir, arq)
        if os.path.isfile(caminho):
            with open(caminho, "r", encoding="utf-8") as f:
                conteudo = f.read()
            conteudo_limpo = _limpar_prompt_tecnico(conteudo)
            nome_fase = arq.replace(".md", "").replace("_", " ").title()
            prompts.append({
                "arquivo": arq,
                "fase": nome_fase,
                "conteudo": conteudo_limpo
            })

    return {"account_id": account_id, "prompts": prompts}


# ── SUGESTÕES DE PROMPT ──────────────────────────────────────

@app.post("/api/clientes/{account_id}/prompt/sugestao")
async def salvar_sugestao_prompt(account_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Salva ou atualiza sugestão de prompt feita pelo cliente."""
    from db import upsert_sugestao
    body = await request.json()
    fase = body.get("fase", "").strip()
    conteudo = body.get("conteudo_sugerido", "").strip()
    if not fase or not conteudo:
        raise HTTPException(status_code=400, detail="fase e conteudo_sugerido são obrigatórios")
    sid = upsert_sugestao(
        account_id=account_id,
        fase=fase,
        conteudo_sugerido=conteudo,
        user_id=str(user.get("sub") or user.get("id") or ""),
        user_nome=user.get("name") or user.get("email", ""),
    )
    return {"id": sid, "status": "pendente", "message": "Sugestão salva"}


@app.get("/api/clientes/{account_id}/prompt/sugestoes")
async def listar_sugestoes_conta(account_id: int, user: dict = Depends(get_current_user)):
    """Lista sugestões pendentes do usuário para esta conta."""
    from db import listar_sugestoes_usuario
    return listar_sugestoes_usuario(account_id, str(user.get("sub") or user.get("id") or ""))


@app.get("/api/sugestoes")
async def listar_todas_sugestoes(user: dict = Depends(get_current_user)):
    """Lista todas as sugestões pendentes (admin/super_admin)."""
    if user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")
    from db import listar_sugestoes_pendentes
    sugestoes = listar_sugestoes_pendentes()
    # Enriquecer com o prompt original para comparação
    for s in sugestoes:
        pasta = pasta_cliente(s["account_id"])
        if pasta:
            caminho = os.path.join(pasta, "prompt", s["fase"])
            if os.path.isfile(caminho):
                with open(caminho, "r", encoding="utf-8") as f:
                    s["conteudo_original"] = _limpar_prompt_tecnico(f.read())
            else:
                s["conteudo_original"] = ""
        else:
            s["conteudo_original"] = ""
        # Adicionar nome da conta
        cfg = carregar_config_cliente(s["account_id"])
        s["nome_conta"] = cfg.get("nome", f"Conta #{s['account_id']}") if cfg else f"Conta #{s['account_id']}"
    return sugestoes


@app.patch("/api/sugestoes/{sugestao_id}")
async def atualizar_sugestao(sugestao_id: str, request: Request, user: dict = Depends(get_current_user)):
    """Aprova ou rejeita uma sugestão (admin/super_admin)."""
    if user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")
    from db import atualizar_status_sugestao
    body = await request.json()
    status = body.get("status", "")
    if status not in ("aprovada", "rejeitada"):
        raise HTTPException(status_code=400, detail="Status deve ser 'aprovada' ou 'rejeitada'")
    atualizar_status_sugestao(sugestao_id, status, body.get("admin_nota"))
    return {"status": "ok"}


# ── ONBOARDING ──────────────────────────────────────────────

@app.get("/onboarding")
def onboarding_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "onboarding.html"))


@app.post("/api/onboarding/generate-token/{account_id}")
async def api_gerar_token_onboarding(account_id: int, user: dict = Depends(get_current_user)):
    """Gera token único de onboarding para a conta."""
    require_super_admin(user)
    cfg = carregar_config_cliente(account_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    token = secrets.token_urlsafe(32)
    row = criar_onboarding(account_id, token)
    return {"token": token, "url": f"/onboarding?token={token}", "data": row}


@app.get("/api/onboarding/status/{account_id}")
async def api_status_onboarding(account_id: int, user: dict = Depends(get_current_user)):
    """Verifica status do onboarding da conta."""
    row = get_onboarding_by_account(account_id)
    if not row:
        return {"status": "not_started"}
    return {
        "status": row["status"],
        "token": row["token"],
        "url": f"/onboarding?token={row['token']}",
        "created_at": row.get("created_at"),
        "submitted_at": row.get("submitted_at"),
        "form_data": row.get("form_data", {}),
    }


@app.get("/api/onboarding/{token}")
async def api_get_onboarding(token: str):
    """Retorna dados do onboarding pelo token (público)."""
    row = get_onboarding_by_token(token)
    if not row:
        raise HTTPException(status_code=404, detail="Token inválido")
    # Buscar nome da conta para exibir no formulário
    cfg = carregar_config_cliente(row["account_id"]) or {}
    return {
        "status": row["status"],
        "form_data": row.get("form_data", {}),
        "account_id": row["account_id"],
        "nome_conta": cfg.get("nome", ""),
        "nome_escritorio": cfg.get("nome_escritorio", ""),
    }


@app.put("/api/onboarding/{token}")
async def api_salvar_rascunho_onboarding(token: str, request: Request):
    """Salva rascunho do onboarding (público, só se status=draft)."""
    row = get_onboarding_by_token(token)
    if not row:
        raise HTTPException(status_code=404, detail="Token inválido")
    if row["status"] != "draft":
        raise HTTPException(status_code=403, detail="Onboarding já foi enviado")
    body = await request.json()
    atualizar_onboarding_draft(token, body.get("form_data", {}))
    return {"status": "ok"}


@app.post("/api/onboarding/{token}/submit")
async def api_submeter_onboarding(token: str):
    """Envia onboarding final — bloqueia edição e auto-configura a conta."""
    row = get_onboarding_by_token(token)
    if not row:
        raise HTTPException(status_code=404, detail="Token inválido")
    if row["status"] != "draft":
        raise HTTPException(status_code=403, detail="Onboarding já foi enviado")
    form_data = row.get("form_data", {})
    account_id = row["account_id"]
    # Auto-configurar a conta com os dados do formulário
    _processar_onboarding(account_id, form_data)
    # Marcar como submitted (bloqueia acesso)
    submeter_onboarding(token)
    return {"status": "submitted"}


def _slugify(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:25]


def _detectar_especialidade(form_data: dict) -> str:
    """Detecta 'trabalhista' ou 'previdenciario' a partir dos dados do formulario."""
    for adv in form_data.get("advogados", []):
        esp = (adv.get("especialidade") or "").lower()
        if "trabalhist" in esp:
            return "trabalhista"
        if "previdenci" in esp:
            return "previdenciario"
    obs = (form_data.get("comportamento", {}).get("outras_instrucoes") or "").lower()
    if "trabalhist" in obs:
        return "trabalhista"
    if "previdenci" in obs:
        return "previdenciario"
    return "trabalhista"


def _build_placeholders_onboarding(form_data: dict, especialidade: str) -> dict:
    """Constroi o dicionario de placeholders para preenchimento dos templates."""
    escritorio = form_data.get("escritorio", {})
    personalidade = form_data.get("personalidade", {})
    comportamento = form_data.get("comportamento", {})
    anuncios = form_data.get("anuncios", {})
    regras = form_data.get("regras", {})

    nome_ia = (personalidade.get("nome_ia") or "Assistente").strip()
    nome_escritorio = (escritorio.get("nome") or "Escritorio de Advocacia").strip()
    especialidade_texto = "Direito do Trabalho" if especialidade == "trabalhista" else "Direito Previdenciario"

    # Endereco
    cidade = (escritorio.get("cidade") or "").strip()
    estado = (escritorio.get("estado") or "").strip()
    endereco = (escritorio.get("endereco") or "").strip()
    area = escritorio.get("area_atendimento") or "online"
    telefone = (escritorio.get("telefone") or "").strip()

    linhas_end = []
    if area == "online":
        linhas_end.append(f"Atendemos 100% online em todo o Brasil. Voce nao precisa sair de casa.")
    elif area == "presencial":
        loc = ", ".join(filter(None, [cidade, estado]))
        linhas_end.append(f"Nosso escritorio fica em {loc}." if loc else "Atendemos de forma presencial.")
        if endereco:
            linhas_end.append(endereco)
    else:
        loc = ", ".join(filter(None, [cidade, estado]))
        linhas_end.append(f"Temos escritorio em {loc} e atendemos tambem 100% online em todo o Brasil." if loc else "Atendemos presencialmente e de forma online em todo o Brasil.")
        if endereco:
            linhas_end.append(endereco)
    if telefone:
        linhas_end.append(f"\nTelefone: {telefone}")
    endereco_escritorio = "\n".join(linhas_end)

    # Custo
    explicacao_custo_raw = (comportamento.get("explicacao_custo") or "").strip()
    if not explicacao_custo_raw:
        explicacao_custo_raw = "Nao cobramos nada antecipado. Os honorarios sao pagos apenas no exito, sobre o valor recebido pelo cliente."

    # Apresentacao
    apresentacao_raw = (personalidade.get("apresentacao") or "").strip()
    if not apresentacao_raw:
        apresentacao_raw = (
            f"Cumprimento baseado no horario:\n"
            f"- 06h-12h: \"Bom dia!\"\n"
            f"- 12h-18h: \"Boa tarde!\"\n"
            f"- 18h-06h: \"Boa noite!\"\n\n"
            f"Seguido de:\n"
            f"\"{nome_ia}, do {nome_escritorio}. Como posso te ajudar?\""
        )

    # Palavras proibidas extras
    palavras_raw = (comportamento.get("palavras_proibidas") or "").strip()
    palavras_extra = ""
    if palavras_raw:
        linhas_p = [ln.strip() for ln in palavras_raw.splitlines() if ln.strip()]
        palavras_extra = "\n".join(f'- NUNCA usar a expressao "{p}".' for p in linhas_p)

    # Instrucoes adicionais compostas
    partes = []
    perguntas = (regras.get("perguntas_obrigatorias") or "").strip()
    assuntos = (regras.get("assuntos_especiais") or "").strip()
    valores = (regras.get("valores_atualizados") or "").strip()
    outras = (comportamento.get("outras_instrucoes") or "").strip()
    obs_regras = (regras.get("observacoes") or "").strip()
    if perguntas:
        partes.append(f"## PERGUNTAS OBRIGATORIAS\n\nAs perguntas a seguir DEVEM ser feitas ao cliente (uma por vez):\n\n{perguntas}")
    if assuntos:
        partes.append(f"## ASSUNTOS ESPECIAIS\n\n{assuntos}")
    if valores:
        partes.append(f"## VALORES ATUALIZADOS\n\n{valores}")
    if outras:
        partes.append(f"## INSTRUCOES ADICIONAIS\n\n{outras}")
    if obs_regras:
        partes.append(f"## OBSERVACOES\n\n{obs_regras}")
    instrucoes_adicionais = ("\n\n---\n\n".join(partes) + "\n\n---\n\n") if partes else ""

    # Anuncios
    usa_meta = bool(anuncios.get("usa_meta"))
    if usa_meta:
        temas = anuncios.get("temas") or []
        regra_anuncio = 'Se a conversa iniciar com "Mensagem de Anuncio!", o usuario obrigatoriamente deve passar por todo o atendimento da IA. Voce deve atender, qualificar, entender o caso e conduzir ate agendamento.'
        if temas:
            regra_anuncio += "\n\nMensagens de abertura por tema de anuncio:\n"
            for t in temas:
                if t.get("nome") and t.get("mensagem"):
                    regra_anuncio += f'\n- Tema "{t["nome"]}": {t["mensagem"]}'
    else:
        regra_anuncio = 'Se a conversa iniciar com "Mensagem de Anuncio!", atender normalmente e conduzir ao agendamento.'

    # Encerramento
    regra_encerramento = (
        f"Sempre que utilizar as ferramentas convertido ou TransferHuman, "
        f"informar ao cliente que um especialista da equipe vai analisar o caso e retornar em breve.\n\n"
        f"EXCECAO OBRIGATORIA: Quando o interlocutor declarar que e advogado(a) da reclamada/empresa ou estiver "
        f"representando a empresa, NAO enviar pedido de avaliacao. Finalize apenas com a confirmacao da proxima acao e encerre."
    )

    return {
        "{{NOME_IA}}": nome_ia,
        "{{NOME_ESCRITORIO}}": nome_escritorio,
        "{{ESPECIALIDADE_TEXTO}}": especialidade_texto,
        "{{APRESENTACAO_IA}}": apresentacao_raw,
        "{{ENDERECO_ESCRITORIO}}": endereco_escritorio,
        "{{TELEFONES_OFICIAIS}}": telefone or "(nao informado)",
        "{{EXPLICACAO_CUSTO}}": explicacao_custo_raw,
        "{{PALAVRAS_PROIBIDAS_EXTRA}}": palavras_extra,
        "{{INSTRUCOES_ADICIONAIS}}": instrucoes_adicionais,
        "{{REGRA_ANUNCIO}}": regra_anuncio,
        "{{REGRA_ENCERRAMENTO}}": regra_encerramento,
    }


def _copiar_e_preencher_template(src: str, dst: str, placeholders: dict):
    """Le um template, substitui placeholders e salva no destino."""
    with open(src, encoding="utf-8") as f:
        content = f.read()
    for key, value in placeholders.items():
        content = content.replace(key, value or "")
    with open(dst, encoding="utf-8", mode="w") as f:
        f.write(content)


def _gerar_prompts_onboarding(account_id: int, form_data: dict):
    """Gera os arquivos de prompt para a conta a partir dos templates de onboarding."""
    especialidade = _detectar_especialidade(form_data)
    placeholders = _build_placeholders_onboarding(form_data, especialidade)

    nome_escritorio = form_data.get("escritorio", {}).get("nome") or f"cliente-{account_id}"
    slug = _slugify(nome_escritorio)

    # Usar pasta existente (account_id-*) ou criar nova
    pasta_existente = pasta_cliente(account_id)
    if pasta_existente:
        pasta = pasta_existente
    else:
        pasta = os.path.join(CLIENTES_DIR, f"{account_id}-{slug}")

    prompt_dir = os.path.join(pasta, "prompt")
    os.makedirs(prompt_dir, exist_ok=True)

    templates_dir = os.path.join(BASE_DIR, "templates")
    shared_dir = os.path.join(templates_dir, "shared")
    specialty_dir = os.path.join(templates_dir, especialidade)

    for d in [shared_dir, specialty_dir]:
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if fname.endswith(".md"):
                src = os.path.join(d, fname)
                dst = os.path.join(prompt_dir, fname)
                try:
                    _copiar_e_preencher_template(src, dst, placeholders)
                except Exception as e:
                    logging.error(f"[onboarding] Erro ao gerar {fname}: {e}")

    logging.info(f"[onboarding] Prompts gerados para account_id={account_id} em '{pasta}' (especialidade={especialidade})")


def _processar_onboarding(account_id: int, form_data: dict):
    """Auto-configura a conta com os dados do onboarding."""
    # 1. Atualizar config do cliente
    escritorio = form_data.get("escritorio", {})
    config_update = {}
    if escritorio.get("nome"):
        config_update["nome_escritorio"] = escritorio["nome"]
    if escritorio.get("endereco"):
        config_update["endereco"] = escritorio["endereco"]
    if escritorio.get("telefone"):
        config_update["telefone"] = escritorio["telefone"]
    if config_update:
        salvar_config_cliente(account_id, config_update)

    # 2. Criar advogados
    advogados = form_data.get("advogados", [])
    especialidades = set()
    for i, adv in enumerate(advogados):
        if not adv.get("nome"):
            continue
        esp = adv.get("especialidade", "")
        if esp:
            for e in esp.split(","):
                especialidades.add(e.strip())
        # Converter disponibilidade do formato do form para JSONB
        disp = adv.get("disponibilidade", {})
        if isinstance(disp, str):
            try:
                disp = json.loads(disp)
            except Exception:
                disp = {"0":[],"1":[],"2":[],"3":[],"4":[],"5":[],"6":[]}
        campos = {
            "account_id": account_id,
            "nome": adv["nome"],
            "especialidade": esp,
            "cor_id": i % 12,
            "duracao_agendamento": adv.get("duracao_agendamento", 30),
            "horas_inicial_busca": adv.get("horas_inicial_busca", 0),
            "quantidade_dias_a_buscar": adv.get("quantidade_dias_a_buscar", 14),
            "disponibilidade": disp if isinstance(disp, str) else json.dumps(disp),
            "ativo": True,
        }
        inserir_advogado(campos)

    # 3. Atualizar especialidade agregada da conta
    if especialidades:
        salvar_config_cliente(account_id, {"especialidade": ", ".join(sorted(especialidades))})

    # 4. Salvar email_agenda se fornecido
    agenda = form_data.get("agenda", {})
    if agenda.get("email_calendar"):
        salvar_config_cliente(account_id, {"email_agenda": agenda["email_calendar"]})

    # 5. Gerar arquivos de prompt a partir dos templates de onboarding
    try:
        _gerar_prompts_onboarding(account_id, form_data)
    except Exception as e:
        logging.error(f"[onboarding] Erro ao gerar prompts para account_id={account_id}: {e}")
