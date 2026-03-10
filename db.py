from supabase import create_client, Client
from dotenv import load_dotenv
import logging
import os

load_dotenv()

logger = logging.getLogger(__name__)

_client: Client | None = None


def get_db() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL e SUPABASE_KEY não configurados no .env")
        _client = create_client(url, key)
    return _client


# ── CONVERSATIONS ─────────────────────────────────────────────

def upsert_conversation(account_id: int, inbox_id: int, conversation_id: int, contact_name: str, contact_phone: str, phase: str):
    db = get_db()
    db.table("ia_conversations").upsert({
        "account_id": account_id,
        "inbox_id": inbox_id,
        "conversation_id": conversation_id,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
        "current_phase": phase,
        "updated_at": "now()",
    }, on_conflict="account_id,conversation_id").execute()


def get_conversation(account_id: int, conversation_id: int) -> dict | None:
    db = get_db()
    resp = (
        db.table("ia_conversations")
        .select("*")
        .eq("account_id", account_id)
        .eq("conversation_id", conversation_id)
        .maybe_single()
        .execute()
    )
    return resp.data


# ── LEADS ─────────────────────────────────────────────────────

def upsert_lead(account_id: int, inbox_id: int, conversation_id: int, contact_name: str, contact_phone: str, status: str = "em_atendimento", inviability_reason: str = None, qualification_data: dict = None):
    db = get_db()
    payload = {
        "account_id": account_id,
        "inbox_id": inbox_id,
        "conversation_id": conversation_id,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
        "status": status,
        "updated_at": "now()",
    }
    if inviability_reason is not None:
        payload["inviability_reason"] = inviability_reason
    if qualification_data is not None:
        payload["qualification_data"] = qualification_data

    db.table("ia_leads").upsert(payload, on_conflict="account_id,conversation_id").execute()


# ── AGENDAMENTOS ──────────────────────────────────────────────

def inserir_agendamento(account_id: int, inbox_id: int, conversation_id: int, contact_name: str, contact_phone: str, scheduled_date: str, scheduled_time: str, advogada: str):
    db = get_db()
    db.table("ia_agendamentos").insert({
        "account_id": account_id,
        "inbox_id": inbox_id,
        "conversation_id": conversation_id,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
        "scheduled_date": scheduled_date,
        "scheduled_time": scheduled_time,
        "advogada": advogada,
        "status": "agendado",
    }).execute()


# ── TRANSCRIÇÕES ──────────────────────────────────────────────

# ── INATIVIDADE ───────────────────────────────────────────────

def upsert_inatividade(account_id: int, conversation_id: int, inbox_id: int | None, stagio: int, proximo_disparo: str):
    db = get_db()
    db.table("ia_inatividade").upsert({
        "account_id": account_id,
        "conversation_id": conversation_id,
        "inbox_id": inbox_id,
        "stagio": stagio,
        "proximo_disparo": proximo_disparo,
        "ativo": True,
        "updated_at": "now()",
    }, on_conflict="account_id,conversation_id").execute()


def get_inatividades_pendentes() -> list:
    db = get_db()
    resp = (
        db.table("ia_inatividade")
        .select("*")
        .eq("ativo", True)
        .lte("proximo_disparo", "now()")
        .execute()
    )
    return resp.data or []


def desativar_inatividade(account_id: int, conversation_id: int):
    db = get_db()
    db.table("ia_inatividade").update({"ativo": False, "updated_at": "now()"}).eq(
        "account_id", account_id
    ).eq("conversation_id", conversation_id).execute()


def deletar_dados_conta(account_id: int):
    db = get_db()
    for tabela in ("ia_transcricoes", "ia_agendamentos", "ia_leads", "ia_conversations", "ia_inatividade"):
        try:
            db.table(tabela).delete().eq("account_id", account_id).execute()
            logger.info(f"Supabase: {tabela} limpa para account_id={account_id}")
        except Exception as e:
            logger.warning(f"Supabase: erro ao limpar {tabela} (account_id={account_id}): {e}")


# ── DASHBOARD USERS ───────────────────────────────────────────

def criar_usuario(email: str, password_hash: str, nome: str, role: str) -> dict:
    db = get_db()
    resp = db.table("dashboard_users").insert({
        "email": email,
        "password_hash": password_hash,
        "nome": nome,
        "role": role,
        "ativo": True,
    }).execute()
    return resp.data[0] if resp.data else None


def get_usuario_por_email(email: str) -> dict | None:
    db = get_db()
    try:
        resp = db.table("dashboard_users").select("*").eq("email", email).eq("ativo", True).maybe_single().execute()
        return resp.data if resp else None
    except Exception:
        return None


def get_usuario_por_id(user_id: str) -> dict | None:
    db = get_db()
    try:
        resp = db.table("dashboard_users").select("*").eq("id", user_id).maybe_single().execute()
        return resp.data if resp else None
    except Exception:
        return None


def listar_usuarios() -> list:
    db = get_db()
    resp = db.table("dashboard_users").select("id,email,nome,role,ativo,created_at").order("created_at").execute()
    return resp.data or []


def atualizar_usuario(user_id: str, dados: dict):
    db = get_db()
    db.table("dashboard_users").update(dados).eq("id", user_id).execute()


def super_admin_existe() -> bool:
    db = get_db()
    resp = db.table("dashboard_users").select("id", count="exact").eq("role", "super_admin").execute()
    return (resp.count or 0) > 0


def get_contas_do_usuario(user_id: str) -> list:
    db = get_db()
    resp = db.table("dashboard_user_accounts").select("account_id").eq("user_id", user_id).execute()
    return [r["account_id"] for r in (resp.data or [])]


def atribuir_conta_usuario(user_id: str, account_id: int):
    db = get_db()
    db.table("dashboard_user_accounts").upsert(
        {"user_id": user_id, "account_id": account_id},
        on_conflict="user_id,account_id"
    ).execute()


def remover_conta_usuario(user_id: str, account_id: int):
    db = get_db()
    db.table("dashboard_user_accounts").delete().eq("user_id", user_id).eq("account_id", account_id).execute()


def listar_usuarios_com_contas() -> list:
    db = get_db()
    usuarios = db.table("dashboard_users").select("id,email,nome,role,ativo,created_at").order("created_at").execute().data or []
    for u in usuarios:
        contas = db.table("dashboard_user_accounts").select("account_id").eq("user_id", u["id"]).execute().data or []
        u["contas"] = [c["account_id"] for c in contas]
    return usuarios


# ── TRANSCRIÇÕES ──────────────────────────────────────────────

def salvar_transcricao(account_id: int, inbox_id: int, conversation_id: int, chatwoot_message_id: int, transcription: str, audio_url: str):
    db = get_db()
    db.table("ia_transcricoes").insert({
        "account_id": account_id,
        "inbox_id": inbox_id,
        "conversation_id": conversation_id,
        "chatwoot_message_id": chatwoot_message_id,
        "transcription": transcription,
        "audio_url": audio_url,
    }).execute()


# ── CLIENTES CONFIG ──────────────────────────────────────────

def carregar_config_cliente(account_id: int) -> dict | None:
    db = get_db()
    try:
        resp = (
            db.table("ia_clientes_config")
            .select("config")
            .eq("account_id", account_id)
            .maybe_single()
            .execute()
        )
        if resp and resp.data:
            return resp.data["config"]
    except Exception:
        pass
    return None


def salvar_config_cliente(account_id: int, config: dict):
    db = get_db()
    db.table("ia_clientes_config").upsert({
        "account_id": account_id,
        "config": config,
        "updated_at": "now()",
    }, on_conflict="account_id").execute()


def listar_configs_clientes() -> list:
    db = get_db()
    resp = db.table("ia_clientes_config").select("account_id,config").order("account_id").execute()
    return resp.data or []


def deletar_config_cliente(account_id: int):
    db = get_db()
    db.table("ia_clientes_config").delete().eq("account_id", account_id).execute()
