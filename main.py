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
    listar_campanhas_envio, get_campanha_envio,
    inserir_campanha_envio, atualizar_campanha_envio,
    deletar_campanha_envio, contar_envios_campanha_hoje,
    contar_total_envios_campanha,
    get_conversation_ids_ja_enviados_campanha,
)
from db import (
    criar_onboarding, get_onboarding_by_token, get_onboarding_by_account,
    atualizar_onboarding_draft, submeter_onboarding,
)
from db import (
    get_zapsign_config, salvar_zapsign_config,
    listar_zapsign_docs, upsert_zapsign_doc, contar_zapsign_docs_por_status,
)
from db import (
    remover_doc_token_followup, atualizar_zapsign_doc_status,
    get_zapsign_followup_stats, listar_zapsign_followups,
)
from auth import hash_password, verify_password, create_token, get_current_user, require_super_admin
from inatividade import registrar_atividade, iniciar_monitoramento
from remarketing import iniciar_remarketing
from campanhas import iniciar_campanhas
import asyncio
import httpx
import json
import logging
import os
import random
import re
import secrets
import string
from collections import OrderedDict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cache de message IDs já processados (evita duplicação por webhook retry)
_MSG_IDS_PROCESSADOS: OrderedDict[int, bool] = OrderedDict()
_MSG_IDS_MAX = 500


async def _recuperar_conversas_pos_deploy():
    """Após deploy, aguarda 90s e verifica conversas abertas atribuídas à IA sem resposta."""
    await asyncio.sleep(90)
    logger.info("[pos-deploy] Verificando conversas sem resposta...")

    try:
        from db import get_db
        db = get_db()
        configs = db.table("ia_clientes_config").select(
            "account_id,chatwoot_url,chatwoot_token,ia_agent_id,ia_ativa,inboxes"
        ).eq("ativo", True).execute()

        total_reprocessadas = 0

        for cfg in (configs.data or []):
            if not cfg.get("ia_ativa", True) or not cfg.get("ia_agent_id"):
                continue

            account_id = cfg["account_id"]
            base_url = (cfg.get("chatwoot_url") or "").rstrip("/")
            token = cfg.get("chatwoot_token", "")
            ia_agent_id = cfg["ia_agent_id"]

            if not base_url or not token:
                continue

            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    # Buscar conversas abertas atribuídas à IA (até 3 páginas)
                    convs = []
                    for page in range(1, 4):
                        url = f"{base_url}/api/v1/accounts/{account_id}/conversations"
                        resp = await http.get(url, headers={"api_access_token": token}, params={
                            "status": "open", "assignee_type": "assigned", "page": page,
                        })
                        if not resp.is_success:
                            break
                        page_convs = resp.json().get("data", {}).get("payload", [])
                        convs.extend(page_convs)
                        if len(page_convs) < 25:  # última página
                            break

                    for conv in convs:
                        try:
                            # Verificar se está atribuída à IA
                            assignee = conv.get("meta", {}).get("assignee") or {}
                            if assignee.get("id") != ia_agent_id:
                                continue

                            conv_id = conv.get("id")
                            inbox_id = conv.get("inbox_id")

                            # Buscar últimas mensagens
                            msgs_url = f"{base_url}/api/v1/accounts/{account_id}/conversations/{conv_id}/messages"
                            msgs_resp = await http.get(msgs_url, headers={"api_access_token": token})
                            if not msgs_resp.is_success:
                                continue

                            msgs = msgs_resp.json().get("payload", [])
                            if not msgs:
                                continue

                            # Ordenar por created_at
                            msgs = sorted(msgs, key=lambda m: m.get("created_at", 0))
                            ultima = msgs[-1]

                            # Se a última mensagem é do CLIENTE (incoming) e foi nos últimos 15 min
                            if ultima.get("message_type") == 0:
                                from datetime import datetime, timezone
                                created = ultima.get("created_at", 0)
                                if isinstance(created, (int, float)):
                                    msg_time = datetime.fromtimestamp(created, tz=timezone.utc)
                                else:
                                    continue
                                agora = datetime.now(timezone.utc)
                                diff_min = (agora - msg_time).total_seconds() / 60

                                # Só reprocessar mensagens dos últimos 30 minutos (janela do deploy)
                                if diff_min <= 30:
                                    config_full = carregar_config_cliente(account_id)
                                    if config_full:
                                        from ia import agendar_processamento
                                        agendar_processamento(config_full, account_id, conv_id, inbox_id)
                                        total_reprocessadas += 1
                                        logger.info(f"[pos-deploy] Reprocessando conv={conv_id} account={account_id} (última msg cliente há {diff_min:.0f}min)")

                        except Exception as e:
                            logger.warning(f"[pos-deploy] Erro ao verificar conv={conv.get('id')}: {e}")
                            continue

            except Exception as e:
                logger.warning(f"[pos-deploy] Erro ao verificar conta {account_id}: {e}")
                continue

        logger.info(f"[pos-deploy] Concluído — {total_reprocessadas} conversa(s) reprocessada(s)")

    except Exception as e:
        logger.error(f"[pos-deploy] Erro geral: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    iniciar_monitoramento()
    iniciar_remarketing()
    iniciar_campanhas()
    from agendador_audiencias import iniciar_agendador_audiencias
    iniciar_agendador_audiencias()
    from zapsign_followup import iniciar_zapsign_followup
    iniciar_zapsign_followup()
    from agendador_consultas import iniciar_agendador_consultas
    iniciar_agendador_consultas()
    from cobranca_documentos import iniciar_monitoramento as iniciar_cobranca_docs
    iniciar_cobranca_docs()
    # Recuperar conversas perdidas durante deploy
    asyncio.create_task(_recuperar_conversas_pos_deploy())
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
        "team_id", "inbox_id", "inboxes", "email_agenda", "horas_inicial_busca",
        "quantidade_dias_a_buscar", "duracao_agendamento", "disponibilidade",
        "especialidade", "id_notificacao_convertido", "id_notificacao_cliente",
        "meta_waba_id", "meta_access_token", "template_audiencia",
        "nome_escritorio", "nome_completo", "telefone", "endereco",
        "modo_teste", "config_lembrete_consulta", "config_inatividade",
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


# ── ZAPSIGN URL DETECTION (para webhook Chatwoot) ────────────

_ZAPSIGN_URL_PATTERN = re.compile(r'https?://(?:app\.)?zapsign\.com\.br/[^\s)\]]+')
_ZAPSIGN_TOKEN_PATTERN = re.compile(r'/(?:verificar|doc|assinar)/([a-f0-9-]+)')

_REENGAJAMENTO_MSG_PADRAO = (
    "Olá, tudo bem?\n\n"
    "Fui revisar os atendimentos e vi que o seu ficou em aberto, "
    "então passei aqui pra não te deixar sem retorno.\n\n"
    "Ficou alguma dúvida, faltou tempo ou não era bem o que você esperava?\n\n"
    "Me conta pra gente continuar seu atendimento do jeito certo pra você!"
)

_REENGAJAMENTO_TEMPLATE = "remarketing"  # template padrão para WhatsApp Oficial (fora da janela 24h)


async def _processar_reengajamento(config: dict, account_id: int, conversation_id: int, inbox_id: int | None, conteudo: str):
    """Envia mensagem de reengajamento ao cliente quando agente envia @@ em nota privada."""
    from ia import enviar_parte_chatwoot

    chatwoot_url = config["chatwoot_url"].rstrip("/")
    token = config["chatwoot_token"]

    # Texto customizado após @@ (ex: "@@Oi, tudo bem? Vamos retomar?")
    texto_custom = conteudo.strip()[2:].strip()  # remove @@

    try:
        # Verificar tipo de inbox (oficial ou não)
        channel_type = ""
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(
                    f"{chatwoot_url}/api/v1/accounts/{account_id}/inboxes",
                    headers={"api_access_token": token}
                )
                if r.is_success:
                    for ib in r.json().get("payload", []):
                        if ib.get("id") == inbox_id:
                            channel_type = ib.get("channel_type", "")
                            break
        except Exception:
            pass

        is_whatsapp_oficial = "whatsapp" in channel_type.lower()

        if is_whatsapp_oficial:
            # WhatsApp Oficial: verificar janela de 24h
            # Buscar última mensagem incoming para checar se está dentro da janela
            fora_janela = True
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    msgs_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
                    r = await http.get(msgs_url, headers={"api_access_token": token})
                    if r.is_success:
                        from datetime import datetime, timezone, timedelta
                        msgs = r.json().get("payload", [])
                        for m in sorted(msgs, key=lambda x: x.get("created_at", ""), reverse=True):
                            if m.get("message_type") == 0:  # incoming
                                created = m.get("created_at", "")
                                if created:
                                    try:
                                        msg_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                                        if datetime.now(timezone.utc) - msg_time < timedelta(hours=24):
                                            fora_janela = False
                                    except Exception:
                                        pass
                                break
            except Exception:
                pass

            mensagem_enviada = ""
            metodo = ""

            if fora_janela:
                # Fora da janela: enviar template
                template_name = texto_custom if texto_custom else _REENGAJAMENTO_TEMPLATE
                try:
                    url_msg = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
                    headers_msg = {"api_access_token": token, "Content-Type": "application/json"}
                    payload_tpl = {
                        "message_type": "outgoing",
                        "private": False,
                        "template_params": {
                            "name": template_name,
                            "language": "pt_BR",
                            "processed_params": {},
                        },
                    }
                    async with httpx.AsyncClient(timeout=15) as http:
                        resp = await http.post(url_msg, headers=headers_msg, json=payload_tpl)
                        if resp.is_success:
                            mensagem_enviada = f"Template: {template_name}"
                            metodo = "template (fora da janela 24h)"
                            logger.info(f"[@@] Template '{template_name}' enviado — conv={conversation_id}")
                        else:
                            logger.warning(f"[@@] Erro ao enviar template: {resp.status_code} {resp.text[:200]}")
                            # Fallback: tentar texto direto
                            msg_fallback = texto_custom or _REENGAJAMENTO_MSG_PADRAO
                            await enviar_parte_chatwoot(chatwoot_url, token, account_id, conversation_id, msg_fallback)
                            mensagem_enviada = msg_fallback
                            metodo = "texto (fallback — template falhou)"
                except Exception as e:
                    logger.warning(f"[@@] Erro template: {e}")
                    mensagem_enviada = f"ERRO: {e}"
                    metodo = "erro"
            else:
                # Dentro da janela: enviar texto
                mensagem = texto_custom or _REENGAJAMENTO_MSG_PADRAO
                await enviar_parte_chatwoot(chatwoot_url, token, account_id, conversation_id, mensagem)
                mensagem_enviada = mensagem
                metodo = "texto (dentro da janela 24h)"
                logger.info(f"[@@] Mensagem de texto enviada (dentro da janela) — conv={conversation_id}")
        else:
            # API não oficial: enviar texto customizado ou padrão (sem IA — direto e confiável)
            mensagem = texto_custom or _REENGAJAMENTO_MSG_PADRAO
            await enviar_parte_chatwoot(chatwoot_url, token, account_id, conversation_id, mensagem)
            mensagem_enviada = mensagem
            metodo = "texto customizado" if texto_custom else "texto padrao"
            logger.info(f"[@@] Mensagem enviada (API nao oficial) — conv={conversation_id}")

        # Nota privada com detalhes do envio
        try:
            from ia import enviar_nota_privada
            nota = f"✅ Reengajamento @@ enviado\nMétodo: {metodo}\n\nMensagem:\n{mensagem_enviada[:500]}"
            await enviar_nota_privada(chatwoot_url, token, account_id, conversation_id, nota)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"[@@] Erro ao processar reengajamento conv={conversation_id}: {e}")
        try:
            from ia import enviar_nota_privada
            await enviar_nota_privada(chatwoot_url, token, account_id, conversation_id,
                                      f"❌ Erro ao enviar reengajamento: {str(e)[:100]}")
        except Exception:
            pass


def _detectar_zapsign_url_webhook(texto: str, account_id: int, conversation_id: int, inbox_id: int | None):
    """Detecta URLs ZapSign em mensagens outgoing (agente ou IA) e cria follow-up."""
    from db import get_zapsign_config, upsert_zapsign_followup
    from datetime import datetime, timezone, timedelta

    urls = _ZAPSIGN_URL_PATTERN.findall(texto)
    if not urls:
        logger.info(f"[zapsign-followup] Nenhuma URL ZapSign encontrada no texto — account={account_id} conv={conversation_id}")
        return

    logger.info(f"[zapsign-followup] {len(urls)} URL(s) ZapSign encontrada(s) — account={account_id} conv={conversation_id}: {urls}")

    zapsign_cfg = get_zapsign_config(account_id)
    if not zapsign_cfg:
        logger.warning(f"[zapsign-followup] Sem config ZapSign para account={account_id} — follow-up NÃO criado")
        return
    if not zapsign_cfg.get("followup_ativo", False):
        logger.warning(f"[zapsign-followup] followup_ativo=False para account={account_id} — follow-up NÃO criado")
        return

    estagios = zapsign_cfg.get("followup_estagios", [])
    if isinstance(estagios, str):
        estagios = json.loads(estagios)

    first_stage = next((e for e in estagios if e.get("stagio") == 1), None)
    if not first_stage:
        return

    proximo = (datetime.now(timezone.utc) + timedelta(hours=first_stage["horas"])).isoformat()

    for url in urls:
        # Limpar caracteres de formatação markdown (backticks, underscores)
        url_limpa = url.rstrip('`_*>')
        match = _ZAPSIGN_TOKEN_PATTERN.search(url_limpa)
        doc_token = match.group(1) if match else url_limpa

        try:
            upsert_zapsign_followup(account_id, conversation_id, inbox_id, doc_token, 1, proximo)
            logger.info(f"[zapsign-followup] Follow-up criado via webhook — conv={conversation_id} doc={doc_token}")
        except Exception as e:
            logger.warning(f"[zapsign-followup] Erro ao criar follow-up: {e}")


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
        conteudo_msg = msg.get("content") or msg.get("body") or ""
        try:
            from db import salvar_mensagem
            tipo_str = "incoming" if msg_type in (0, "incoming") else "outgoing"
            attachments_raw = msg.get("attachments", [])
            att_resumo = [{"file_type": a.get("file_type"), "data_url": a.get("data_url", "")} for a in attachments_raw] if attachments_raw else None
            salvar_mensagem(
                account_id=account_id, conversation_id=conversation_id, inbox_id=inbox_id or 0,
                chatwoot_message_id=msg.get("id", 0), message_type=tipo_str,
                content=conteudo_msg, sender_name=nome, sender_phone=telefone,
                attachments=att_resumo, created_at=msg.get("created_at"),
            )
        except Exception as e:
            logger.debug(f"Erro ao salvar mensagem no histórico: {e}")

        # Detectar URL ZapSign em QUALQUER mensagem (incoming ou outgoing)
        # Links ZapSign são enviados por agentes mas podem chegar com msg_type variado
        if conteudo_msg and "zapsign" in conteudo_msg.lower():
            try:
                logger.info(f"[zapsign-followup] URL ZapSign detectada — conv={conversation_id} account={account_id} msg_type={msg_type}")
                _detectar_zapsign_url_webhook(conteudo_msg, account_id, conversation_id, inbox_id)
            except Exception as e:
                logger.warning(f"[zapsign-followup] Erro ao detectar URL: {e}")

        # === @@ em nota privada: enviar mensagem de reengajamento ao cliente ===
        is_private = msg.get("private", False)
        if is_private and conteudo_msg.strip().startswith("@@"):
            # Anti-duplicação: só processar 1x por conversa (cache 60s)
            _at_at_key = f"@@_{account_id}_{conversation_id}"
            if _at_at_key not in _MSG_IDS_PROCESSADOS:
                _MSG_IDS_PROCESSADOS[_at_at_key] = True
                if len(_MSG_IDS_PROCESSADOS) > _MSG_IDS_MAX:
                    _MSG_IDS_PROCESSADOS.popitem(last=False)
                logger.info(f"[@@] Comando @@ detectado — conv={conversation_id} account={account_id}")
                asyncio.create_task(_processar_reengajamento(config, account_id, conversation_id, inbox_id, conteudo_msg))
            else:
                logger.info(f"[@@] Comando @@ duplicado ignorado — conv={conversation_id}")
            continue

        # Mensagens outgoing (agente humano ou IA): não processar como IA
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

        # Ignorar mensagens de grupo do WhatsApp — checagem robusta em múltiplos pontos
        identifier = (contact.get("identifier") or "").lower()
        conv_raw = msg.get("conversation") if msg is not payload else payload.get("conversation")
        conv_raw = conv_raw or {}
        conv_meta = conv_raw.get("meta") or {}
        meta_sender = conv_meta.get("sender") if isinstance(conv_meta, dict) else {}
        meta_sender = meta_sender or {}
        meta_identifier = (meta_sender.get("identifier") or "").lower()
        meta_name_upper = (meta_sender.get("name") or "").upper()
        conv_extra = conv_raw.get("additional_attributes") or {}
        conv_type = (conv_extra.get("type") or "").lower() if isinstance(conv_extra, dict) else ""
        contact_extra = contact.get("additional_attributes") or {}
        contact_type = (contact_extra.get("type") or "").lower() if isinstance(contact_extra, dict) else ""
        nome_upper = (nome or "").upper()

        is_group = (
            "@g.us" in identifier
            or "@g.us" in meta_identifier
            or "(GRUPO)" in nome_upper
            or "(GRUPO)" in meta_name_upper
            or conv_type == "group"
            or contact_type == "group"
        )
        if is_group:
            logger.info(
                f"🚫 Ignorando mensagem de grupo — id='{identifier}' meta_id='{meta_identifier}' "
                f"nome='{nome}' conv_type='{conv_type}' contact_type='{contact_type}'"
            )
            continue
        texto = msg.get("content") or ""
        attachments = msg.get("attachments", [])

        # Cobrança de documentos: se cliente enviou anexo de documento (PDF/imagem)
        # em conta habilitada, desativar a cobrança e remover a label.
        try:
            from cobranca_documentos import CONTAS_HABILITADAS as _contas_cobr, LABEL_COBRANCA as _label_cobr
            if account_id in _contas_cobr and attachments:
                tipos_doc = {"file", "image"}
                if any((a.get("file_type") in tipos_doc) for a in attachments):
                    from db import desativar_cobranca_docs, get_cobranca_docs
                    if get_cobranca_docs(account_id, conversation_id):
                        desativar_cobranca_docs(account_id, conversation_id, motivo="anexo_recebido")
                        logger.info(f"[cobranca-docs] Anexo recebido — desativada conv={conversation_id}")
                        # Remover label no Chatwoot (best-effort)
                        try:
                            _cw = config["chatwoot_url"].rstrip("/")
                            _tk = config["chatwoot_token"]
                            async with httpx.AsyncClient(timeout=10) as _hc:
                                _lu = f"{_cw}/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"
                                _r = await _hc.get(_lu, headers={"api_access_token": _tk})
                                if _r.is_success:
                                    _ls = _r.json().get("payload", []) or []
                                    _novas = [l for l in _ls if l != _label_cobr]
                                    if len(_novas) != len(_ls):
                                        await _hc.post(_lu, headers={"api_access_token": _tk, "Content-Type": "application/json"}, json={"labels": _novas})
                        except Exception:
                            pass
        except Exception as _e:
            logger.debug(f"[cobranca-docs] Hook attachment erro: {_e}")

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

        # Transcrição de áudio: rodar ANTES do filtro de modo teste (transcreve sempre)
        audio = next((a for a in attachments if a.get("file_type") == "audio"), None)
        if audio and config.get("transcricao_ativa", True):
            msg_id_audio = msg.get("id")
            if msg_id_audio not in _transcricoes_processadas:
                _transcricoes_processadas.add(msg_id_audio)
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
                    try:
                        salvar_transcricao(
                            account_id=account_id, inbox_id=inbox_id,
                            conversation_id=conversation_id,
                            chatwoot_message_id=msg_id_audio,
                            transcription=transcricao,
                            audio_url=audio.get("data_url", ""),
                        )
                    except Exception as e:
                        logger.warning(f"Erro ao salvar transcrição no Supabase: {e}")
                except Exception as e:
                    logger.error(f"Erro ao transcrever áudio: {e}")

        # Modo teste: se ativo, IA só responde conversas com label "ia-teste"
        modo_teste = config.get("modo_teste", False)
        conv_labels = payload.get("conversation", {}).get("labels", [])
        if modo_teste and "ia-teste" not in conv_labels:
            logger.info(f"[modo-teste] Ignorando conv={conversation_id} — sem label 'ia-teste' (account={account_id})")
            continue

        # Verificar se IA deve responder nesta conversa (antes de qualquer efeito colateral)
        # Modo teste com label "ia-teste": forçar IA ativa (não precisa de ia_ativa ligado na config)
        if modo_teste and "ia-teste" in conv_labels:
            ia_ativa = ia_agent_id is not None and assignee_id == ia_agent_id and inbox_permitido
        else:
            ia_ativa = config.get("ia_ativa", True) and ia_agent_id is not None and assignee_id == ia_agent_id and inbox_permitido

        # Race condition: se assignee_id é None ou diferente, pode ser que a atribuição
        # ainda não chegou. Aguardar e re-checar via API (2 tentativas com intervalo crescente).
        ia_config_ok = (modo_teste and "ia-teste" in conv_labels) or config.get("ia_ativa", True)
        if not ia_ativa and ia_config_ok and ia_agent_id is not None and inbox_permitido and conversation_id:
            if assignee_id is None or assignee_id != ia_agent_id:
                chatwoot_url_rc = config["chatwoot_url"].rstrip("/")
                chatwoot_token_rc = config["chatwoot_token"]
                for tentativa, espera in enumerate([3, 5], 1):
                    try:
                        await asyncio.sleep(espera)
                        async with httpx.AsyncClient(timeout=10) as hc:
                            url_rc = f"{chatwoot_url_rc}/api/v1/accounts/{account_id}/conversations/{conversation_id}"
                            resp_rc = await hc.get(url_rc, headers={"api_access_token": chatwoot_token_rc})
                            if resp_rc.is_success:
                                rc_json = resp_rc.json()
                                rc_assignee = rc_json.get("meta", {}).get("assignee") or rc_json.get("assignee")
                                rc_assignee_id = rc_assignee.get("id") if isinstance(rc_assignee, dict) else None
                                if rc_assignee_id == ia_agent_id:
                                    assignee_id = rc_assignee_id
                                    ia_ativa = True
                                    logger.info(f"[race-condition] IA atribuída após tentativa {tentativa} — conv={conversation_id}")
                                    break
                                else:
                                    logger.info(f"[race-condition] Tentativa {tentativa}: assignee={rc_assignee_id} != ia_agent={ia_agent_id} — conv={conversation_id}")
                    except Exception as e:
                        logger.warning(f"[race-condition] Erro tentativa {tentativa} — conv={conversation_id}: {e}")

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
        if not ia_ativa and assignee_id == ia_agent_id:
            # Assignee bate mas IA inativa — logar motivo detalhado
            motivos = []
            if not config.get("ia_ativa", True) and not (modo_teste and "ia-teste" in conv_labels):
                motivos.append("ia_ativa=False na config")
            if not inbox_permitido:
                motivos.append(f"inbox {inbox_id} não está em inboxes={inboxes_permitidos}")
            logger.info(f"🤖 IA ativa: False (assignee={assignee_id}, ia_agent={ia_agent_id}) — MOTIVO: {', '.join(motivos) or 'desconhecido'}")
        else:
            logger.info(f"🤖 IA ativa: {ia_ativa} (assignee={assignee_id}, ia_agent={ia_agent_id})")

        # Transcrição já foi feita acima (antes do filtro de modo teste)

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


@app.get("/api/clientes/{account_id}/whatsapp-oficiais")
async def listar_whatsapp_oficiais(account_id: int):
    """Retorna todas as inboxes WhatsApp oficiais (com WABA ID e token) da conta."""
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    chatwoot_url = config.get("chatwoot_url", "").strip().rstrip("/")
    chatwoot_token = config.get("chatwoot_token", "").strip()
    if not chatwoot_url or not chatwoot_token:
        raise HTTPException(status_code=400, detail="Chatwoot não configurado para esta conta")

    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/inboxes"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"api_access_token": chatwoot_token})
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail="Erro ao buscar inboxes do Chatwoot")

    data = r.json()
    inboxes = data.get("payload", data) if isinstance(data, dict) else data

    # Filtrar só inboxes WhatsApp que tenham business_account_id (WABA) e api_key (token)
    oficiais = []
    for inbox in inboxes:
        channel = (inbox.get("channel_type") or "").lower()
        if "whatsapp" not in channel:
            continue
        pc = inbox.get("provider_config") or {}
        waba_id = (pc.get("business_account_id") or "").strip()
        api_key = (pc.get("api_key") or "").strip()
        if not waba_id or not api_key:
            continue
        phone = inbox.get("phone_number") or pc.get("phone_number") or ""
        oficiais.append({
            "inbox_id": inbox["id"],
            "name": inbox.get("name", ""),
            "phone": phone,
            "waba_id": waba_id,
            "token": api_key,
        })

    # Também incluir o WABA configurado direto na conta (meta_waba_id), se existir
    config_waba = config.get("meta_waba_id", "").strip()
    config_token = config.get("meta_access_token", "").strip()
    if config_waba and config_token:
        # Verificar se já não está na lista (evitar duplicata)
        already = any(o["waba_id"] == config_waba for o in oficiais)
        if not already:
            oficiais.insert(0, {
                "inbox_id": None,
                "name": f"Config da conta ({config.get('nome', '')})",
                "phone": "",
                "waba_id": config_waba,
                "token": config_token,
            })

    return oficiais


@app.get("/api/clientes/{account_id}/templates")
async def listar_templates(account_id: int, status: str = "", waba_id: str = "", waba_token: str = ""):
    # Se recebeu waba_id e token via query, usar esses (vindo do seletor de API oficial)
    if waba_id and waba_token:
        w_id, tok = waba_id.strip(), waba_token.strip()
    else:
        w_id, tok = _get_meta_config(account_id)
    params = {"fields": META_TEMPLATE_FIELDS, "limit": 100}
    if status:
        params["status"] = status.upper()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{META_GRAPH}/{w_id}/message_templates", headers=_meta_headers(tok), params=params)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json())
    return r.json()


@app.post("/api/clientes/{account_id}/templates")
async def criar_template(account_id: int, request: Request):
    payload = await request.json()
    # Extrair waba_id e token do payload se fornecidos (vindo do seletor de API oficial)
    custom_waba = (payload.pop("_waba_id", "") or "").strip()
    custom_token = (payload.pop("_waba_token", "") or "").strip()
    if custom_waba and custom_token:
        waba_id, token = custom_waba, custom_token
    else:
        waba_id, token = _get_meta_config(account_id)
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
async def upload_media_template(account_id: int, file: UploadFile = File(...), waba_id: str = "", waba_token: str = ""):
    """Faz upload de mídia para a Meta e retorna o handle para usar em templates."""
    from io import BytesIO
    # Usar WABA/token custom se fornecidos via query params
    if waba_id.strip() and waba_token.strip():
        w_id, tok = waba_id.strip(), waba_token.strip()
    else:
        w_id, tok = _get_meta_config(account_id)

    # Ler arquivo
    file_bytes = await file.read()
    file_name = file.filename or "upload"

    # 1. Criar sessão de upload
    async with httpx.AsyncClient(timeout=60) as client:
        # Upload via resumable upload API
        r = await client.post(
            f"{META_GRAPH}/app/uploads",
            headers=_meta_headers(tok),
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
                "Authorization": f"OAuth {tok}",
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
async def deletar_template(account_id: int, template_name: str, waba_id: str = "", waba_token: str = ""):
    if waba_id.strip() and waba_token.strip():
        w_id, tok = waba_id.strip(), waba_token.strip()
    else:
        w_id, tok = _get_meta_config(account_id)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(
            f"{META_GRAPH}/{w_id}/message_templates",
            headers=_meta_headers(tok),
            params={"name": template_name},
        )
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=r.status_code, detail=r.json())
    return {"status": "deletado", "template": template_name}


# ── CONFIG INATIVIDADE ────────────────────────────────────────

@app.get("/api/config/inatividade")
def get_inatividade_config(account_id: int = None):
    # Se tem account_id, buscar config por conta primeiro
    if account_id:
        config = carregar_config_cliente(account_id)
        if config:
            cfg_conta = config.get("config_inatividade")
            if cfg_conta:
                if isinstance(cfg_conta, str):
                    cfg_conta = json.loads(cfg_conta)
                if cfg_conta.get("estagios"):
                    return cfg_conta
    # Fallback: config global (com defaults de limite e ativo)
    path = os.path.join(BASE_DIR, "config", "inatividade.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("limite", 3)
    cfg.setdefault("ativo", True)
    return cfg


@app.put("/api/config/inatividade")
async def put_inatividade_config(request: Request):
    dados = await request.json()
    account_id = dados.pop("account_id", None)
    if account_id:
        # Salvar por conta no banco
        config = carregar_config_cliente(account_id)
        if config:
            config["config_inatividade"] = dados
            salvar_config_cliente(account_id, config)
            return {"status": "ok", "scope": "account"}
    # Fallback: salvar global (arquivo)
    path = os.path.join(BASE_DIR, "config", "inatividade.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
    return {"status": "ok", "scope": "global"}


# ── LEMBRETES DE CONSULTA ────────────────────────────────────

@app.get("/api/config/lembretes-consulta")
def get_lembretes_consulta_config(account_id: int):
    config = carregar_config_cliente(account_id)
    if config:
        cfg = config.get("config_lembrete_consulta")
        if cfg:
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            return cfg
    # Padrão
    return {
        "ativo": True,
        "lembretes": [
            {"minutos": 10, "mensagem": "{nome}, só passando para lembrar que seu atendimento com {advogada} é daqui a pouco, às {horario}. Até já!"}
        ]
    }


@app.put("/api/config/lembretes-consulta")
async def put_lembretes_consulta_config(request: Request):
    dados = await request.json()
    account_id = dados.pop("account_id", None)
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id obrigatório")
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    config["config_lembrete_consulta"] = dados
    salvar_config_cliente(account_id, config)
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
    account_id = campanha["account_id"]
    campanha_inbox = campanha.get("inbox_id")
    from db import get_db as _get_db
    _db = _get_db()
    q = _db.table("ia_leads").select("id", count="exact").eq("account_id", account_id)
    if campanha_inbox:
        q = q.eq("inbox_id", campanha_inbox)
    total_leads = q.execute().count or 0
    return {
        "envios_hoje": contar_envios_remarketing_hoje(campanha_id),
        "total_envios": contar_total_envios_remarketing(campanha_id),
        "elegiveis": contar_elegiveis_remarketing(
            account_id, campanha["dias_inatividade"], inbox_id=campanha_inbox
        ),
        "total_leads": total_leads,
        "dias": campanha["dias_inatividade"],
    }


# ── CAMPANHAS DE ENVIO ────────────────────────────────────────

@app.get("/api/campanhas-envio")
def api_listar_campanhas_envio(account_id: int):
    return listar_campanhas_envio(account_id)


@app.get("/api/campanhas-envio/{campanha_id}")
def api_get_campanha_envio(campanha_id: int):
    c = get_campanha_envio(campanha_id)
    if not c:
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return c


@app.post("/api/campanhas-envio")
async def api_criar_campanha_envio(request: Request):
    dados = await request.json()
    account_id = dados.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id obrigatório")
    return inserir_campanha_envio(account_id, dados)


@app.put("/api/campanhas-envio/{campanha_id}")
async def api_atualizar_campanha_envio(campanha_id: int, request: Request):
    dados = await request.json()
    result = atualizar_campanha_envio(campanha_id, dados)
    if not result:
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return result


@app.delete("/api/campanhas-envio/{campanha_id}")
def api_deletar_campanha_envio(campanha_id: int):
    if not deletar_campanha_envio(campanha_id):
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return {"status": "deletado"}


@app.get("/api/campanhas-envio/{campanha_id}/stats")
def api_stats_campanha_envio(campanha_id: int):
    campanha = get_campanha_envio(campanha_id)
    if not campanha:
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return {
        "envios_hoje": contar_envios_campanha_hoje(campanha_id),
        "total_envios": contar_total_envios_campanha(campanha_id),
        "filtro": f"{campanha.get('tipo_filtro', '')}: {campanha.get('valor_filtro', '')}",
    }


@app.get("/api/clientes/{account_id}/chatwoot/labels")
async def proxy_chatwoot_labels(account_id: int):
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    url = f"{config['chatwoot_url'].rstrip('/')}/api/v1/accounts/{account_id}/labels"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"api_access_token": config["chatwoot_token"]})
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json()
    return data.get("payload", data)


@app.get("/api/clientes/{account_id}/chatwoot/funnels")
async def proxy_chatwoot_funnels(account_id: int):
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    url = f"{config['chatwoot_url'].rstrip('/')}/api/v1/accounts/{account_id}/funnels"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"api_access_token": config["chatwoot_token"]})
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json()
    return data.get("payload", data)


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


# ── BLOQUEIOS DE AGENDA ───────────────────────────────────────

WEBHOOK_AGENDAR_BLOQUEIO = "https://flow.advbrasil.ai/webhook/agendar"


async def _criar_evento_gcal_bloqueio(account_id: int, data_inicio: str, data_fim: str, advogado_nome: str, motivo: str, cor_id: int = 0) -> str | None:
    """Cria evento bloqueante no Google Calendar via n8n. Retorna google_event_id ou None."""
    config = carregar_config_cliente(account_id)
    if not config or not config.get("email_agenda"):
        return None
    # Formatar start/end para o formato esperado pelo n8n (YYYY-MM-DD HH:MM)
    start = data_inicio.replace("T", " ")[:16]
    end = data_fim.replace("T", " ")[:16]
    summary = f"BLOQUEIO - {advogado_nome}" + (f" ({motivo})" if motivo else "")
    payload = {
        "email_agenda": config["email_agenda"],
        "Start": start,
        "End": end,
        "Color Name or ID": str(cor_id) if cor_id else "11",
        "Summary": summary,
        "Description": f"Bloqueio de agenda criado pelo sistema\nAdvogado: {advogado_nome}\nMotivo: {motivo or 'Não informado'}",
        "numero": "",
    }
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(WEBHOOK_AGENDAR_BLOQUEIO, json=payload, timeout=30)
            if resp.is_success:
                data = resp.json()
                if isinstance(data, list) and data:
                    data = data[0]
                event_id = data.get("google_event_id") or data.get("eventId") or data.get("id", "")
                logger.info(f"[bloqueio] Evento Google Calendar criado: {event_id}")
                return event_id
            else:
                logger.warning(f"[bloqueio] Erro ao criar evento GCal: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"[bloqueio] Erro ao criar evento GCal: {e}")
    return None


async def _deletar_evento_gcal_bloqueio(account_id: int, google_event_id: str):
    """Deleta evento do Google Calendar via API."""
    if not google_event_id:
        return
    config = carregar_config_cliente(account_id)
    if not config or not config.get("email_agenda"):
        return
    # Usar webhook de deletar ou API direta — por ora logar apenas
    logger.info(f"[bloqueio] Evento GCal {google_event_id} deveria ser deletado (account={account_id})")


@app.get("/api/bloqueios-agenda")
def api_listar_bloqueios(account_id: int):
    from db import listar_bloqueios_agenda
    return listar_bloqueios_agenda(account_id)


@app.post("/api/bloqueios-agenda")
async def api_criar_bloqueio(request: Request):
    from db import inserir_bloqueio_agenda
    dados = await request.json()
    account_id = dados.get("account_id")
    advogado_nome = dados.get("advogado_nome", "Todos")
    motivo = dados.get("motivo", "")
    # Garantir timezone de Brasília nos horários (evita conversão indesejada pelo Supabase)
    from datetime import timezone as tz, timedelta
    BR_OFFSET = tz(timedelta(hours=-3))
    data_inicio_raw = dados.get("data_inicio", "")
    data_fim_raw = dados.get("data_fim", "")
    # Se veio sem timezone, adicionar -03:00 (Brasília)
    if data_inicio_raw and "+" not in data_inicio_raw and "-03" not in data_inicio_raw and "Z" not in data_inicio_raw:
        data_inicio_raw = data_inicio_raw + "-03:00"
    if data_fim_raw and "+" not in data_fim_raw and "-03" not in data_fim_raw and "Z" not in data_fim_raw:
        data_fim_raw = data_fim_raw + "-03:00"

    campos = {
        "account_id": account_id,
        "advogado_id": dados.get("advogado_id"),
        "advogado_nome": advogado_nome,
        "data_inicio": data_inicio_raw,
        "data_fim": data_fim_raw,
        "motivo": motivo,
    }
    if not campos["account_id"] or not campos["data_inicio"] or not campos["data_fim"]:
        raise HTTPException(status_code=400, detail="account_id, data_inicio e data_fim são obrigatórios")

    # Buscar cor_id do advogado se especificado
    cor_id = 0
    if dados.get("advogado_id"):
        try:
            from db import get_advogado
            adv = get_advogado(dados["advogado_id"])
            if adv:
                cor_id = adv.get("cor_id", 0)
        except Exception:
            pass

    # Criar evento no Google Calendar
    google_event_id = await _criar_evento_gcal_bloqueio(
        account_id, campos["data_inicio"], campos["data_fim"], advogado_nome, motivo, cor_id
    )
    if google_event_id:
        campos["google_event_id"] = google_event_id

    return inserir_bloqueio_agenda(campos)


@app.put("/api/bloqueios-agenda/{bloqueio_id}")
async def api_editar_bloqueio(bloqueio_id: str, request: Request):
    from db import get_db
    dados = await request.json()
    db = get_db()
    # Garantir timezone de Brasília nos horários
    for campo_dt in ["data_inicio", "data_fim"]:
        v = dados.get(campo_dt, "")
        if v and "+" not in v and "-03" not in v and "Z" not in v:
            dados[campo_dt] = v + "-03:00"

    payload = {}
    for campo in ["advogado_id", "advogado_nome", "data_inicio", "data_fim", "motivo"]:
        if campo in dados:
            payload[campo] = dados[campo]
    if not payload:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")
    resp = db.table("ia_bloqueios_agenda").update(payload).eq("id", bloqueio_id).execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Bloqueio não encontrado")

    # Se mudou datas, recriar evento no Google Calendar
    bloqueio = resp.data[0]
    account_id = bloqueio.get("account_id")
    if account_id and ("data_inicio" in dados or "data_fim" in dados):
        cor_id = 0
        if bloqueio.get("advogado_id"):
            try:
                from db import get_advogado
                adv = get_advogado(bloqueio["advogado_id"])
                if adv:
                    cor_id = adv.get("cor_id", 0)
            except Exception:
                pass
        new_event_id = await _criar_evento_gcal_bloqueio(
            account_id, bloqueio["data_inicio"], bloqueio["data_fim"],
            bloqueio.get("advogado_nome", "Todos"), bloqueio.get("motivo", ""), cor_id
        )
        if new_event_id:
            db.table("ia_bloqueios_agenda").update({"google_event_id": new_event_id}).eq("id", bloqueio_id).execute()

    return bloqueio


@app.delete("/api/bloqueios-agenda/{bloqueio_id}")
async def api_deletar_bloqueio(bloqueio_id: str):
    from db import get_db, deletar_bloqueio_agenda
    db = get_db()
    # Buscar bloqueio antes de deletar para pegar google_event_id
    bloqueio_resp = db.table("ia_bloqueios_agenda").select("account_id,google_event_id").eq("id", bloqueio_id).maybe_single().execute()
    if bloqueio_resp and bloqueio_resp.data:
        await _deletar_evento_gcal_bloqueio(
            bloqueio_resp.data.get("account_id"),
            bloqueio_resp.data.get("google_event_id", "")
        )
    if not deletar_bloqueio_agenda(bloqueio_id):
        raise HTTPException(status_code=404, detail="Bloqueio não encontrado")
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
    """Busca grupos WhatsApp no Chatwoot e retorna conversation_id para notificações."""
    from ia import _NOTIF_CHATWOOT_EXTERNO
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    # Grupos permitidos por conta no Chatwoot externo (conversation_id → label)
    _GRUPOS_PERMITIDOS = {
        8: {75: "Novos Leads", 77: "Clientes Existentes"},
        11: {76: "Novos Leads", 74: "Clientes Existentes"},
    }

    # Se conta tem grupos pré-definidos, retorna direto sem buscar na API
    if account_id in _GRUPOS_PERMITIDOS:
        return [{"id": cid, "label": label} for cid, label in _GRUPOS_PERMITIDOS[account_id].items()]

    # Demais contas: buscar grupos no Chatwoot da conta
    base = config["chatwoot_url"].rstrip("/")
    headers = {"api_access_token": config["chatwoot_token"]}

    groups = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
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
                        contact_id = c["id"]
                        r2 = await client.get(
                            f"{base}/api/v1/accounts/{account_id}/contacts/{contact_id}/conversations",
                            headers=headers,
                        )
                        if r2.is_success:
                            convs = r2.json().get("payload", [])
                            if convs:
                                groups.append({"id": convs[0]["id"], "label": name})
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
    # Invalidar cache para que a próxima tool leia os funis recém-criados
    try:
        from ia import _funnel_cache
        _funnel_cache.pop(account_id, None)
    except Exception:
        pass
    return {"status": "ok", "detail": f"Funis recriados para conta {account_id}"}


@app.post("/api/admin/kanban/teste/{account_id}")
async def testar_criacao_kanban(account_id: int, conversation_id: int | None = None, tool: str = "novo_lead"):
    """Testa a criação/movimentação de um card no kanban com captura completa de erros.

    Se conversation_id não for passado, pega a primeira conversa aberta da conta.
    Parâmetro tool: nome da tool em KANBAN_TOOL_MAP (default: novo_lead).
    """
    from ia import kanban_mover_card, _carregar_funis, KANBAN_TOOL_MAP
    import traceback

    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    base = (config.get("chatwoot_url") or "").rstrip("/")
    token = config.get("chatwoot_token", "")

    # Se não passou conversation_id, pega uma conversa aberta qualquer
    if conversation_id is None:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(
                    f"{base}/api/v1/accounts/{account_id}/conversations",
                    headers={"api_access_token": token},
                    params={"status": "open", "page": 1},
                )
                if r.is_success:
                    convs = r.json().get("data", {}).get("payload", []) or []
                    if convs:
                        conversation_id = convs[0].get("id")
        except Exception as e:
            return {"erro": f"Falha ao listar conversas: {e}"}
    if not conversation_id:
        return {"erro": "Nenhuma conversa aberta encontrada para testar"}

    if tool not in KANBAN_TOOL_MAP:
        return {"erro": f"Tool inválida: {tool}", "validas": list(KANBAN_TOOL_MAP.keys())}

    # Forçar reload do cache de funis
    funis = await _carregar_funis(base, token, account_id, force_reload=True)

    resultado = {
        "conversation_id": conversation_id,
        "tool": tool,
        "mapeamento": KANBAN_TOOL_MAP[tool],
        "funis_carregados": list(funis.keys()),
    }

    try:
        await kanban_mover_card(base, token, account_id, conversation_id, f"Teste Kanban {conversation_id}", tool)
        resultado["status"] = "chamada_concluida_sem_excecao"
    except Exception as e:
        resultado["status"] = "excecao"
        resultado["erro"] = str(e)
        resultado["traceback"] = traceback.format_exc()

    # Verificar se o card existe agora
    funil_ident, step_ident = KANBAN_TOOL_MAP[tool]
    funil = funis.get(funil_ident, {})
    funnel_id = funil.get("funnel_id")
    step_id = funil.get("steps", {}).get(step_ident)
    if funnel_id and step_id:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(
                    f"{base}/api/v1/accounts/{account_id}/funnels/{funnel_id}/funnel_steps/{step_id}/funnel_items",
                    headers={"api_access_token": token},
                )
                if r.is_success:
                    data = r.json()
                    items = data.get("items", data) if isinstance(data, dict) else data
                    if isinstance(items, list):
                        match = [i for i in items if i.get("conversation_id") == conversation_id]
                        resultado["card_apos_teste"] = {
                            "encontrado_na_etapa_esperada": len(match) > 0,
                            "total_na_etapa": len(items),
                        }
                        if match:
                            resultado["card_apos_teste"]["card"] = {
                                "id": match[0].get("id"),
                                "title": match[0].get("title"),
                                "created_at": match[0].get("created_at"),
                            }
        except Exception as e:
            resultado["erro_verificacao"] = str(e)

    return resultado


@app.get("/api/admin/kanban/estado/{account_id}")
async def estado_kanban_conta(account_id: int):
    """Mostra o estado atual do kanban de uma conta: funis, etapas, quantos
    cards em cada e amostra dos mais recentes.

    Serve para auditar se a IA está movimentando os cards conforme esperado.
    """
    from ia import KANBAN_TOOL_MAP

    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    base = (config.get("chatwoot_url") or "").rstrip("/")
    token = config.get("chatwoot_token", "")
    if not base or not token:
        raise HTTPException(status_code=400, detail="chatwoot_url/token não configurados")

    # Mapa reverso: (funil_id, step_id) → nomes de tools que disparam essa etapa
    tools_por_etapa: dict[tuple, list[str]] = {}
    for tool_name, (funil_ident, step_ident) in KANBAN_TOOL_MAP.items():
        key = (funil_ident, step_ident)
        tools_por_etapa.setdefault(key, []).append(tool_name)

    funis_resp = []
    async with httpx.AsyncClient(timeout=20) as http:
        # Listar funis
        r = await http.get(
            f"{base}/api/v1/accounts/{account_id}/funnels",
            headers={"api_access_token": token},
        )
        if not r.is_success:
            raise HTTPException(status_code=500, detail=f"Chatwoot retornou {r.status_code}")
        funnels = r.json().get("payload", []) or []

        for funil in funnels:
            f_id = funil.get("id")
            f_ident = funil.get("identifier", "")
            etapas_out = []
            for step in funil.get("funnel_steps", []) or []:
                s_id = step.get("id")
                s_ident = step.get("identifier", "")
                # Buscar items dessa etapa
                items = []
                try:
                    ri = await http.get(
                        f"{base}/api/v1/accounts/{account_id}/funnels/{f_id}/funnel_steps/{s_id}/funnel_items",
                        headers={"api_access_token": token},
                    )
                    if ri.is_success:
                        data = ri.json()
                        items = data.get("items", data) if isinstance(data, dict) else data
                        if not isinstance(items, list):
                            items = []
                except Exception:
                    items = []
                amostra = []
                for it in items[:5]:
                    amostra.append({
                        "id": it.get("id"),
                        "title": it.get("title"),
                        "conversation_id": it.get("conversation_id"),
                        "created_at": it.get("created_at"),
                        "updated_at": it.get("updated_at"),
                    })
                etapas_out.append({
                    "titulo": step.get("title"),
                    "identifier": s_ident,
                    "total_cards": len(items),
                    "tools_que_disparam": tools_por_etapa.get((f_ident, s_ident), []),
                    "amostra_recentes": amostra,
                })
            funis_resp.append({
                "titulo": funil.get("title"),
                "identifier": f_ident,
                "etapas": etapas_out,
            })

    total_cards = sum(e["total_cards"] for f in funis_resp for e in f["etapas"])
    cards_ia = sum(
        1
        for f in funis_resp
        for e in f["etapas"]
        for c in e["amostra_recentes"]
        if c.get("conversation_id")
    )

    return {
        "account_id": account_id,
        "total_cards_no_kanban": total_cards,
        "cards_com_conversation_id_amostra": cards_ia,
        "funis": funis_resp,
        "legenda_tools": {
            "novo_lead / em_qualificacao": "ao entrar em identificacao ou outra fase inicial",
            "Agendar": "quando a IA agenda uma consulta (sucesso)",
            "convertido": "após agendamento confirmado",
            "TransferHuman": "quando transfere para humano",
            "cliente_inviavel": "quando marca inviável",
            "desqualificado / nao_lead": "quando descarta o lead",
            "followup / aguardando_cliente / lead_perdido / nao_assinou / nao_alfabetizado": "tools auxiliares",
        },
    }


@app.post("/api/admin/cobranca-docs/forcar-ciclo")
async def forcar_ciclo_cobranca_docs():
    """Dispara manualmente um ciclo do loop de cobrança de documentos.

    Útil logo após adicionar/remover a label para não esperar o próximo
    intervalo automático (5min). Executa: sincronizar → desativar-sem-label →
    disparar cobranças pendentes. Só opera dentro do horário comercial.
    """
    from cobranca_documentos import (
        _dentro_horario_comercial,
        _sincronizar_cobrancas,
        _desativar_sem_label,
        _disparar_cobrancas,
    )

    if not _dentro_horario_comercial():
        return {"status": "fora_do_horario_comercial", "detalhe": "Ciclo só roda entre 8h e 19h BRT"}

    erros = {}
    try:
        await _sincronizar_cobrancas()
    except Exception as e:
        erros["sincronizar"] = str(e)
    try:
        await _desativar_sem_label()
    except Exception as e:
        erros["desativar_sem_label"] = str(e)
    try:
        await _disparar_cobrancas()
    except Exception as e:
        erros["disparar"] = str(e)

    return {"status": "ok" if not erros else "erros", "erros": erros}


@app.get("/api/admin/cobranca-docs/labels-chatwoot/{account_id}")
async def listar_labels_chatwoot(account_id: int):
    """Lista todas as labels da conta no Chatwoot + amostra de conversas abertas com seus labels.

    Útil para confirmar o slug exato da label que o humano adicionou (pode ser
    diferente do título visível, ex: "Cobrar Documentos" vs "cobrar-documentos").
    """
    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    base = (config.get("chatwoot_url") or "").rstrip("/")
    token = config.get("chatwoot_token", "")
    if not base or not token:
        raise HTTPException(status_code=400, detail="chatwoot_url/token não configurados")

    labels = []
    conversas = []
    async with httpx.AsyncClient(timeout=15) as http:
        # 1. Labels cadastradas na conta
        try:
            r = await http.get(
                f"{base}/api/v1/accounts/{account_id}/labels",
                headers={"api_access_token": token},
            )
            if r.is_success:
                labels = r.json().get("payload", []) or []
        except Exception as e:
            labels = [{"erro": str(e)}]

        # 2. Amostra de 25 conversas abertas com seus labels
        try:
            r2 = await http.get(
                f"{base}/api/v1/accounts/{account_id}/conversations",
                headers={"api_access_token": token},
                params={"status": "open", "page": 1},
            )
            if r2.is_success:
                payload = r2.json().get("data", {}).get("payload", []) or []
                for c in payload[:25]:
                    labels_conv = c.get("labels") or []
                    if labels_conv:
                        conversas.append({
                            "id": c.get("id"),
                            "nome": ((c.get("meta") or {}).get("sender") or {}).get("name"),
                            "labels": labels_conv,
                        })
        except Exception:
            pass

    return {
        "account_id": account_id,
        "labels_totais_na_conta": len(labels),
        "labels": [
            {"title": l.get("title"), "slug": l.get("slug") or l.get("title"), "color": l.get("color")}
            for l in labels if isinstance(l, dict)
        ],
        "conversas_abertas_com_label": conversas,
    }


@app.get("/api/admin/cobranca-docs/diagnostico/{account_id}")
async def diagnostico_cobranca_docs(account_id: int):
    """Diagnóstico do fluxo de cobrança de documentos para uma conta.

    Retorna: se a tabela existe, se a conta está habilitada, quantas conversas
    têm a label 'cobrar-documentos', quantos registros ativos no banco,
    próximo envio agendado e estado do horário comercial.
    """
    from cobranca_documentos import (
        CONTAS_HABILITADAS, LABEL_COBRANCA, _dentro_horario_comercial,
        _listar_conversas_com_label,
    )
    from db import get_db

    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    # 1. Tabela existe?
    tabela_ok = False
    erro_tabela = None
    registros_ativos = 0
    amostra = []
    try:
        db = get_db()
        resp = (
            db.table("ia_cobranca_docs")
            .select("conversation_id,inbox_id,tentativas,limite,ativo,proximo_envio,ultimo_envio,motivo_desativacao")
            .eq("account_id", account_id)
            .order("updated_at", desc=True)
            .limit(10)
            .execute()
        )
        amostra = resp.data or []
        tabela_ok = True
        # Contar ativos
        resp2 = (
            db.table("ia_cobranca_docs")
            .select("id", count="exact")
            .eq("account_id", account_id)
            .eq("ativo", True)
            .execute()
        )
        registros_ativos = resp2.count or 0
    except Exception as e:
        erro_tabela = str(e)

    # 2. Conversas com a label no Chatwoot
    convs_com_label = []
    erro_chatwoot = None
    try:
        convs = await _listar_conversas_com_label(config, LABEL_COBRANCA)
        convs_com_label = [
            {"id": c.get("id"), "status": c.get("status"), "labels": c.get("labels")}
            for c in convs
        ]
    except Exception as e:
        erro_chatwoot = str(e)

    return {
        "account_id": account_id,
        "habilitada": account_id in CONTAS_HABILITADAS,
        "label_esperada": LABEL_COBRANCA,
        "horario_comercial_agora": _dentro_horario_comercial(),
        "tabela_ia_cobranca_docs": {
            "existe": tabela_ok,
            "erro": erro_tabela,
            "registros_ativos": registros_ativos,
            "amostra_recentes": amostra,
        },
        "chatwoot": {
            "conversas_com_label": len(convs_com_label),
            "amostra": convs_com_label[:10],
            "erro": erro_chatwoot,
        },
    }


@app.get("/api/admin/inatividade/diagnostico/{account_id}")
async def diagnostico_inatividade_conta(account_id: int):
    """Diagnóstico de follow-up/inatividade de uma conta.

    Retorna: config ativa, estágios configurados, inatividades ativas no banco,
    quantas estão pendentes de disparo agora.
    """
    try:
        from db import get_db
        db = get_db()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro DB: {e}")

    config = carregar_config_cliente(account_id)
    if not config:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    # Carrega config de inatividade (por conta OU global)
    from inatividade import carregar_config_inatividade, _limite_inatividade
    cfg_inat = carregar_config_inatividade(account_id)
    estagios = cfg_inat.get("estagios", []) if isinstance(cfg_inat, dict) else []
    limite = _limite_inatividade(account_id)

    # Buscar registros ativos em ia_inatividade
    try:
        resp = (
            db.table("ia_inatividade")
            .select("conversation_id,inbox_id,stagio,proximo_disparo,ativo,updated_at")
            .eq("account_id", account_id)
            .eq("ativo", True)
            .order("proximo_disparo")
            .execute()
        )
        ativos = resp.data or []
    except Exception as e:
        ativos = []
        erro_tabela = str(e)
    else:
        erro_tabela = None

    from datetime import datetime, timezone
    agora = datetime.now(timezone.utc)
    pendentes_agora = 0
    for a in ativos:
        try:
            dt = datetime.fromisoformat(str(a.get("proximo_disparo")).replace("Z", "+00:00"))
            if dt <= agora:
                pendentes_agora += 1
        except Exception:
            pass

    return {
        "account_id": account_id,
        "nome": config.get("nome"),
        "ativo_conta": config.get("ativo", True),
        "inatividade_ativa": config.get("inatividade_ativa", True),
        "limite_followup": limite,
        "estagios_configurados": len(estagios),
        "estagios": estagios,
        "registros_ativos": len(ativos),
        "pendentes_disparo_agora": pendentes_agora,
        "amostra_registros": ativos[:10],
        "erro_tabela": erro_tabela,
    }


@app.get("/api/admin/kanban/diagnostico")
async def diagnostico_kanban_todas_contas():
    """Verifica o estado dos funis/etapas do kanban em todas as contas ativas.

    Retorna lista com status de cada conta: funis presentes, etapas faltando,
    e se o kanban está 100% funcional para o fluxo da IA.
    """
    FUNIS_ESPERADOS = {
        "pipeline_comercial": [
            "lead_novo", "aguardando_atendimento", "aguardando_assinatura",
            "contrato_fechado", "followup", "leads_desqualificados",
            "nao_respondeu", "nao_assinou",
        ],
        "triagem_encaminhamento": [
            "transferido", "inviavel", "desqualificado",
            "nao_alfabetizado", "perdido", "resolvido",
        ],
    }

    try:
        from db import get_db
        db = get_db()
        configs = db.table("ia_clientes_config").select(
            "account_id,nome,chatwoot_url,chatwoot_token,ativo"
        ).eq("ativo", True).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar contas: {e}")

    resultado = []
    async with httpx.AsyncClient(timeout=10) as http:
        for cfg in (configs.data or []):
            account_id = cfg.get("account_id")
            nome_conta = cfg.get("nome", "")
            base = (cfg.get("chatwoot_url") or "").rstrip("/")
            token = cfg.get("chatwoot_token", "")
            if not base or not token:
                resultado.append({
                    "account_id": account_id,
                    "nome": nome_conta,
                    "status": "sem_credencial",
                    "detalhe": "chatwoot_url ou chatwoot_token não configurados",
                })
                continue
            try:
                resp = await http.get(
                    f"{base}/api/v1/accounts/{account_id}/funnels",
                    headers={"api_access_token": token},
                )
                if not resp.is_success:
                    resultado.append({
                        "account_id": account_id,
                        "nome": nome_conta,
                        "status": "api_erro",
                        "http_status": resp.status_code,
                    })
                    continue
                funnels = resp.json().get("payload", [])
                por_identifier = {f.get("identifier"): f for f in funnels}

                faltando_funis = []
                faltando_steps = {}
                for funil_id, steps_esperados in FUNIS_ESPERADOS.items():
                    if funil_id not in por_identifier:
                        faltando_funis.append(funil_id)
                        continue
                    steps_atuais = {s.get("identifier") for s in por_identifier[funil_id].get("funnel_steps", [])}
                    faltando = [s for s in steps_esperados if s not in steps_atuais]
                    if faltando:
                        faltando_steps[funil_id] = faltando

                if not faltando_funis and not faltando_steps:
                    status = "ok"
                else:
                    status = "incompleto"

                resultado.append({
                    "account_id": account_id,
                    "nome": nome_conta,
                    "status": status,
                    "funis_existentes": list(por_identifier.keys()),
                    "funis_faltando": faltando_funis,
                    "etapas_faltando": faltando_steps,
                })
            except Exception as e:
                resultado.append({
                    "account_id": account_id,
                    "nome": nome_conta,
                    "status": "erro",
                    "detalhe": str(e),
                })

    resumo = {
        "total": len(resultado),
        "ok": sum(1 for r in resultado if r.get("status") == "ok"),
        "incompleto": sum(1 for r in resultado if r.get("status") == "incompleto"),
        "erro": sum(1 for r in resultado if r.get("status") in ("erro", "api_erro", "sem_credencial")),
    }
    return {"resumo": resumo, "contas": resultado}


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
        logger.warning(f"[audiencia-envio] Audiência {audiencia_id} não encontrada")
        return {"error": "Audiência não encontrada", "status_code": 404}

    account_id = audiencia["account_id"]
    conversation_id = audiencia["conversation_id"]
    inbox_id = audiencia.get("inbox_id")
    logger.info(f"[audiencia-envio] Iniciando envio: audiencia={audiencia_id} account={account_id} conv={conversation_id} inbox={inbox_id} tipo_audiencia='{audiencia.get('tipo_audiencia')}'")

    config = carregar_config_cliente(account_id)
    if not config:
        logger.warning(f"[audiencia-envio] Config não encontrada para account_id={account_id}")
        return {"error": "Cliente não encontrado", "status_code": 404}

    # Buscar a mensagem do tipo de audiência (filtrando por account_id)
    tipos = listar_hearing_types_db(account_id)
    tipo = next((t for t in tipos if t["nome"] == audiencia.get("tipo_audiencia")), None)
    if not tipo:
        # Fallback: buscar sem filtro de account
        tipos = listar_hearing_types_db()
        tipo = next((t for t in tipos if t["nome"] == audiencia.get("tipo_audiencia")), None)
    if not tipo:
        logger.warning(f"[audiencia-envio] Tipo '{audiencia.get('tipo_audiencia')}' não encontrado. Tipos disponíveis: {[t['nome'] for t in tipos]}")
        return {"error": "Tipo de audiência não encontrado", "status_code": 400}

    mensagens_tipo = tipo.get("mensagens", [])
    logger.info(f"[audiencia-envio] Tipo encontrado: '{tipo['nome']}' com {len(mensagens_tipo)} mensagem(ns)")
    if not mensagens_tipo:
        logger.warning(f"[audiencia-envio] Nenhuma mensagem configurada para tipo '{tipo['nome']}'")
        return {"error": "Nenhuma mensagem configurada para este tipo", "status_code": 400}

    # Se msg_id específico, envia essa; senão, envia todas
    if msg_id:
        msg = next((m for m in mensagens_tipo if m["id"] == msg_id), None)
        if not msg:
            logger.warning(f"[audiencia-envio] msg_id={msg_id} não encontrado. IDs disponíveis: {[m.get('id') for m in mensagens_tipo]}")
            raise HTTPException(status_code=404, detail="Mensagem não encontrada no tipo")
        msgs_para_enviar = [msg]
    else:
        msgs_para_enviar = sorted(mensagens_tipo, key=lambda m: m.get("idx", 0))
    logger.info(f"[audiencia-envio] Mensagens a enviar: {[m.get('id','?') for m in msgs_para_enviar]}")

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
    logger.info(f"[audiencia-envio] channel_type='{channel_type}' is_whatsapp_oficial={is_whatsapp_oficial}")

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
                    logger.info(f"[audiencia-envio] Dest '{dest['nome']}' conv={conv_id}: ultima_msg={ultima_msg} janela_expirada={dest_janela_expirada}")
                except Exception as e:
                    dest_janela_expirada = True  # assume expirada por segurança
                    logger.warning(f"[audiencia-envio] Erro ao checar janela 24h conv={conv_id}: {e}")

            # Enviar cada mensagem para este destinatário
            for msg in msgs_para_enviar:
                # Substituir [NOME] pelo nome do destinatário (não do cliente)
                audiencia_copy = {**audiencia, "nome_cliente": dest["nome"]}
                conteudo = _substituir_placeholders(msg.get("conteudo", ""), audiencia_copy)
                template_name = (msg.get("template_whatsapp") or "").strip()
                logger.info(f"[audiencia-envio] Processando msg={msg.get('id')} para '{dest['nome']}': whatsapp_oficial={is_whatsapp_oficial} janela_expirada={dest_janela_expirada} template='{template_name}'")

                try:
                    if is_whatsapp_oficial and template_name:
                        # Template configurado → sempre envia via template (funciona dentro e fora da janela 24h)
                        tpl_vars = msg.get("template_vars") or {}
                        pp = _build_template_params(audiencia_copy, tpl_vars)
                        logger.info(f"[audiencia-envio] Enviando template '{template_name}' conv={conv_id} params={pp}")
                        await _enviar_template_audiencia_http(
                            http, chatwoot_url, token, account_id, conv_id, template_name, pp
                        )
                        logger.info(f"[audiencia-envio] Template enviado com sucesso para conv={conv_id}")
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
                    elif not is_whatsapp_oficial or not dest_janela_expirada:
                        # Sem template + janela aberta (ou inbox não-WhatsApp) → texto livre
                        if conteudo.strip():
                            await _enviar_texto_audiencia_http(
                                http, chatwoot_url, token, account_id, conv_id, conteudo
                            )
                            resultados.append({
                                "destinatario": dest["nome"], "tipo": dest["tipo"],
                                "msg_id": msg["id"], "metodo": "texto", "status": "enviado",
                            })
                        else:
                            logger.warning(f"[audiencia-envio] Conteúdo vazio para msg={msg.get('id')} — sem template e sem texto")
                            resultados.append({
                                "destinatario": dest["nome"], "tipo": dest["tipo"],
                                "msg_id": msg["id"], "metodo": "texto", "status": "erro",
                                "detalhe": "Conteúdo da mensagem vazio e sem template configurado",
                            })
                    else:
                        # Janela expirada + sem template → erro
                        resultados.append({
                            "destinatario": dest["nome"], "tipo": dest["tipo"],
                            "msg_id": msg["id"], "metodo": "template", "status": "erro",
                            "detalhe": "Fora da janela 24h e sem template configurado",
                        })
                except Exception as e:
                    logger.error(f"[audiencia-envio] ERRO ao enviar msg={msg.get('id')} para '{dest['nome']}' conv={conv_id}: {e}")
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

    # Nota: não forçar toggle_status "open" — o próprio envio da mensagem
    # já reabre a conversa no Chatwoot. Forçar aqui causava reabertura
    # indesejada de conversas em outras inboxes do mesmo contato.

    logger.info(f"[audiencia-envio] Envio concluído — {audiencia['nome_cliente']} | resultados={resultados}")
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
async def listar_todas_sugestoes(status: str = "pendente", user: dict = Depends(get_current_user)):
    """Lista sugestões por status (admin/super_admin). Admin vê apenas suas contas."""
    if user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")
    from db import listar_sugestoes_por_status, listar_todas_sugestoes_recentes
    if status == "todas":
        sugestoes = listar_todas_sugestoes_recentes(limit=100)
    else:
        sugestoes = listar_sugestoes_por_status(status)
    # Admin: filtrar apenas sugestões das contas que ele tem acesso
    if user.get("role") == "admin":
        contas_permitidas = set(get_contas_do_usuario(user["sub"]))
        sugestoes = [s for s in sugestoes if s["account_id"] in contas_permitidas]
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
    """Aprova ou rejeita uma sugestão. Apenas super_admin pode aprovar/rejeitar. Ao aprovar, aplica no arquivo de prompt."""
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Apenas super_admin pode aprovar ou rejeitar sugestões")
    from db import atualizar_status_sugestao, buscar_sugestao
    body = await request.json()
    status = body.get("status", "")
    if status not in ("aprovada", "rejeitada"):
        raise HTTPException(status_code=400, detail="Status deve ser 'aprovada' ou 'rejeitada'")
    # Buscar sugestão para aplicar no arquivo
    sugestao = buscar_sugestao(sugestao_id)
    if not sugestao:
        raise HTTPException(status_code=404, detail="Sugestão não encontrada")
    # Se aprovada, aplicar conteúdo no arquivo de prompt
    if status == "aprovada":
        pasta = pasta_cliente(sugestao["account_id"])
        if pasta:
            caminho = os.path.join(pasta, "prompt", sugestao["fase"])
            try:
                os.makedirs(os.path.dirname(caminho), exist_ok=True)
                with open(caminho, "w", encoding="utf-8") as f:
                    f.write(sugestao["conteudo_sugerido"])
                logger.info(f"[sugestão] Prompt aplicado: {caminho}")
            except Exception as e:
                logger.error(f"[sugestão] Erro ao aplicar prompt {caminho}: {e}")
                raise HTTPException(status_code=500, detail=f"Erro ao aplicar prompt: {e}")
        else:
            raise HTTPException(status_code=404, detail=f"Pasta da conta {sugestao['account_id']} não encontrada")
    atualizar_status_sugestao(sugestao_id, status, body.get("admin_nota"))
    return {"status": "ok", "aplicado": status == "aprovada"}


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


@app.post("/api/onboarding/regenerar-prompts/{account_id}")
async def api_regenerar_prompts(account_id: int, user: dict = Depends(get_current_user)):
    """Regenera os prompts da conta a partir dos dados do onboarding já submetido."""
    row = get_onboarding_by_account(account_id)
    if not row:
        raise HTTPException(status_code=404, detail="Onboarding não encontrado para esta conta")
    form_data = row.get("form_data", {})
    if not form_data:
        raise HTTPException(status_code=400, detail="Onboarding sem dados de formulário")
    # Regenerar apenas os prompts (passo 5 do _processar_onboarding)
    try:
        from onboarding_prompts import gerar_prompts_cliente
        cfg = carregar_config_cliente(account_id) or {}
        nome_conta = cfg.get("nome_escritorio") or cfg.get("nome", f"Conta{account_id}")
        gerar_prompts_cliente(account_id, nome_conta, form_data, CLIENTES_DIR)
        logger.info(f"Prompts regenerados para conta {account_id}")
        return {"status": "ok", "message": f"Prompts regenerados para conta {account_id}"}
    except Exception as e:
        logger.error(f"Erro ao regenerar prompts conta {account_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erro ao gerar prompts: {str(e)}")


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

    # 4. Salvar email_agenda se fornecido (do primeiro advogado que tiver)
    for adv in advogados:
        if adv.get("email_calendar"):
            salvar_config_cliente(account_id, {"email_agenda": adv["email_calendar"]})
            break

    # 5. Gerar pasta e prompts do cliente
    try:
        from onboarding_prompts import gerar_prompts_cliente
        cfg = carregar_config_cliente(account_id) or {}
        nome_conta = cfg.get("nome", f"Conta{account_id}")
        gerar_prompts_cliente(account_id, nome_conta, form_data, CLIENTES_DIR)
        logger.info(f"Prompts gerados automaticamente para conta {account_id}")
    except Exception as e:
        logger.error(f"Erro ao gerar prompts para conta {account_id}: {e}")


# ══════════════════════════════════════════════════════════════
# ZAPSIGN
# ══════════════════════════════════════════════════════════════

ZAPSIGN_API_BASE = "https://api.zapsign.com.br/api/v1"


@app.get("/api/zapsign/config")
async def api_get_zapsign_config(account_id: int, user=Depends(get_current_user)):
    """Busca configuração ZapSign da conta."""
    cfg = get_zapsign_config(account_id)
    if not cfg:
        return {"account_id": account_id, "api_token": "", "sandbox": False, "webhook_url": ""}
    # Mascarar token na resposta (mostrar só últimos 8 chars)
    token = cfg.get("api_token", "")
    cfg["api_token_masked"] = f"***{token[-8:]}" if len(token) > 8 else token
    return cfg


@app.post("/api/zapsign/config")
async def api_save_zapsign_config(request: Request, user=Depends(get_current_user)):
    """Salva configuração ZapSign da conta."""
    body = await request.json()
    account_id = body.get("account_id")
    if not account_id:
        raise HTTPException(400, "account_id obrigatório")
    config = {
        "api_token": body.get("api_token", ""),
        "sandbox": body.get("sandbox", False),
        "webhook_url": body.get("webhook_url", ""),
    }
    salvar_zapsign_config(account_id, config)
    return {"ok": True}


@app.get("/api/zapsign/docs")
async def api_list_zapsign_docs(account_id: int, user=Depends(get_current_user)):
    """Lista documentos ZapSign da conta (busca via API ZapSign e salva localmente)."""
    cfg = get_zapsign_config(account_id)
    if not cfg or not cfg.get("api_token"):
        return {"docs": [], "error": "Token ZapSign não configurado"}

    api_token = cfg["api_token"]
    docs_remote = []

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{ZAPSIGN_API_BASE}/docs/",
                headers={"Authorization": f"Bearer {api_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
            docs_remote = data if isinstance(data, list) else data.get("results", [])

        # Salvar/atualizar docs localmente
        for doc in docs_remote:
            upsert_zapsign_doc(account_id, doc)

    except Exception as e:
        logger.warning(f"Erro ao buscar docs ZapSign conta {account_id}: {e}")
        # Retorna docs locais como fallback
        docs_local = listar_zapsign_docs(account_id)
        return {"docs": docs_local, "error": str(e), "source": "cache"}

    docs_local = listar_zapsign_docs(account_id)
    return {"docs": docs_local, "source": "api"}


@app.get("/api/zapsign/docs/{doc_token}")
async def api_get_zapsign_doc(doc_token: str, account_id: int, user=Depends(get_current_user)):
    """Busca detalhes de um documento específico via API ZapSign."""
    cfg = get_zapsign_config(account_id)
    if not cfg or not cfg.get("api_token"):
        raise HTTPException(400, "Token ZapSign não configurado")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{ZAPSIGN_API_BASE}/docs/{doc_token}/",
                headers={"Authorization": f"Bearer {cfg['api_token']}"},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise HTTPException(502, f"Erro ao buscar documento: {e}")


@app.get("/api/zapsign/metrics")
async def api_zapsign_metrics(account_id: int, user=Depends(get_current_user)):
    """Retorna métricas dos documentos ZapSign da conta."""
    contagem = contar_zapsign_docs_por_status(account_id)
    total = sum(contagem.values())
    return {
        "total": total,
        "signed": contagem.get("signed", 0),
        "pending": contagem.get("pending", 0),
        "expired": contagem.get("expired", 0),
        "canceled": contagem.get("canceled", 0),
        "refunded": contagem.get("refunded", 0),
        "by_status": contagem,
    }


@app.post("/api/zapsign/test-connection")
async def api_zapsign_test_connection(request: Request, user=Depends(get_current_user)):
    """Testa conexão com a API ZapSign usando o token fornecido."""
    body = await request.json()
    api_token = body.get("api_token", "")
    if not api_token:
        return {"ok": False, "error": "Token vazio"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{ZAPSIGN_API_BASE}/docs/",
                headers={"Authorization": f"Bearer {api_token}"},
                params={"page_size": 1},
            )
            if resp.status_code == 200:
                return {"ok": True, "message": "Conexão OK"}
            else:
                return {"ok": False, "error": f"Status {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


ZAPSIGN_WEBHOOK_URL = "https://api.advbrasil.ai/webhook/zapsign"


@app.post("/api/zapsign/implantar")
async def api_zapsign_implantar(request: Request, user=Depends(get_current_user)):
    """Implanta ZapSign na conta: salva token + registra webhook automaticamente."""
    body = await request.json()
    account_id = body.get("account_id")
    api_token = body.get("api_token", "").strip()

    if not account_id or not api_token:
        return {"ok": False, "error": "account_id e api_token obrigatórios"}

    resultados = {"token_salvo": False, "webhook_criado": False, "webhook_ja_existe": False, "erros": []}

    # 1. Testar se o token é válido
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(
                f"{ZAPSIGN_API_BASE}/docs/",
                headers={"Authorization": f"Bearer {api_token}"},
                params={"page_size": 1},
            )
            if resp.status_code != 200:
                return {"ok": False, "error": f"Token inválido (status {resp.status_code})"}
    except Exception as e:
        return {"ok": False, "error": f"Erro ao validar token: {e}"}

    # 2. Salvar token na config
    try:
        salvar_zapsign_config(account_id, {"api_token": api_token})
        resultados["token_salvo"] = True
    except Exception as e:
        resultados["erros"].append(f"Erro ao salvar token: {e}")

    # 3. Verificar se webhook já existe
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(
                f"{ZAPSIGN_API_BASE}/user/company/webhook/",
                headers={"Authorization": f"Bearer {api_token}"},
            )
            if resp.status_code == 200:
                webhooks = resp.json()
                if isinstance(webhooks, list):
                    for wh in webhooks:
                        wh_url = wh.get("url", "")
                        if "advbrasil" in wh_url or "webhook/zapsign" in wh_url:
                            resultados["webhook_ja_existe"] = True
                            break
    except Exception:
        pass  # Se falhar ao listar, tenta criar mesmo assim

    # 4. Criar webhook se não existe
    if not resultados["webhook_ja_existe"]:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    f"{ZAPSIGN_API_BASE}/user/company/webhook/",
                    headers={
                        "Authorization": f"Bearer {api_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "url": ZAPSIGN_WEBHOOK_URL,
                        "type": "",  # vazio = todos os eventos
                    },
                )
                if resp.status_code in (200, 201):
                    resultados["webhook_criado"] = True
                    logger.info(f"[zapsign-implantar] Webhook criado para conta {account_id}")
                else:
                    resultados["erros"].append(f"Webhook: status {resp.status_code} — {resp.text[:200]}")
        except Exception as e:
            resultados["erros"].append(f"Erro ao criar webhook: {e}")
    else:
        logger.info(f"[zapsign-implantar] Webhook já existe para conta {account_id}")

    ok = resultados["token_salvo"] and (resultados["webhook_criado"] or resultados["webhook_ja_existe"])
    return {"ok": ok, **resultados}


# ══════════════════════════════════════════════════════════════
# ZAPSIGN WEBHOOK
# ══════════════════════════════════════════════════════════════

@app.post("/webhook/zapsign")
async def webhook_zapsign(request: Request):
    """Recebe webhooks de status do ZapSign (sem auth — endpoint público para webhook)."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "Invalid JSON"}

    event_type = body.get("event_type", "")
    doc = body.get("doc", {}) if isinstance(body.get("doc"), dict) else {}
    doc_token = doc.get("token", "") or body.get("token", "")
    status = doc.get("status", "") or body.get("status", "")

    logger.info(f"[zapsign-webhook] event={event_type} doc={doc_token} status={status}")

    if not doc_token:
        return {"ok": False, "error": "doc_token não encontrado"}

    # Atualizar status local do documento
    try:
        atualizar_zapsign_doc_status(doc_token, status)
    except Exception as e:
        logger.warning(f"[zapsign-webhook] Erro ao atualizar status doc: {e}")

    # Se documento foi assinado/fechado, remover da lista de pendentes
    # Se era o último doc pendente, desativa o follow-up da conversa
    if status in ("signed", "closed") or event_type in ("doc_signed", "doc_closed"):
        try:
            todos_assinaram = remover_doc_token_followup(doc_token)
            if todos_assinaram:
                logger.info(f"[zapsign-webhook] Todos docs assinados — follow-up desativado (doc={doc_token})")
            else:
                logger.info(f"[zapsign-webhook] Doc {doc_token} assinado, mas ainda faltam docs pendentes")
        except Exception as e:
            logger.warning(f"[zapsign-webhook] Erro ao processar assinatura: {e}")

    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# ZAPSIGN FOLLOW-UP CONFIG
# ══════════════════════════════════════════════════════════════

_DEFAULT_FOLLOWUP_ESTAGIOS = [
    {"stagio": 1, "horas": 2, "mensagem": "Olá! Notei que o contrato ainda não foi assinado. Precisa de alguma ajuda?"},
    {"stagio": 2, "horas": 6, "mensagem": "Oi! Só passando para lembrar sobre o contrato pendente. Posso ajudar com alguma dúvida?"},
    {"stagio": 3, "horas": 12, "mensagem": "Olá! Este é nosso último lembrete sobre o contrato. Por favor, assine para darmos continuidade."},
]


@app.get("/api/zapsign/followup-config")
async def api_get_zapsign_followup_config(account_id: int, user=Depends(get_current_user)):
    """Busca configuração de follow-up ZapSign da conta."""
    cfg = get_zapsign_config(account_id)
    if not cfg:
        return {"followup_ativo": False, "followup_estagios": _DEFAULT_FOLLOWUP_ESTAGIOS}

    estagios = cfg.get("followup_estagios", _DEFAULT_FOLLOWUP_ESTAGIOS)
    if isinstance(estagios, str):
        try:
            estagios = json.loads(estagios)
        except Exception:
            estagios = _DEFAULT_FOLLOWUP_ESTAGIOS

    return {
        "followup_ativo": cfg.get("followup_ativo", False),
        "followup_estagios": estagios,
    }


@app.post("/api/zapsign/followup-config")
async def api_save_zapsign_followup_config(request: Request, user=Depends(get_current_user)):
    """Salva configuração de follow-up ZapSign da conta."""
    body = await request.json()
    account_id = body.get("account_id")
    if not account_id:
        raise HTTPException(400, "account_id obrigatório")

    salvar_zapsign_config(account_id, {
        "followup_ativo": body.get("followup_ativo", False),
        "followup_estagios": body.get("followup_estagios", _DEFAULT_FOLLOWUP_ESTAGIOS),
    })
    return {"ok": True}


@app.get("/api/zapsign/followup-list")
async def api_zapsign_followup_list(account_id: int, limit: int = 20, offset: int = 0, user=Depends(get_current_user)):
    """Lista follow-ups com dados do lead, documentos e estágio. Paginado."""
    return listar_zapsign_followups(account_id, limit, offset)


@app.get("/api/zapsign/followup-stats")
async def api_zapsign_followup_stats(account_id: int, user=Depends(get_current_user)):
    """Retorna métricas dos follow-ups ZapSign: enviados, assinados, pendentes, esgotados."""
    return get_zapsign_followup_stats(account_id)
