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
