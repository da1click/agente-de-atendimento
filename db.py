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


# ── MENSAGENS (HISTÓRICO) ────────────────────────────────────

def salvar_mensagem(account_id: int, conversation_id: int, inbox_id: int,
                    chatwoot_message_id: int, message_type: str, content: str,
                    sender_name: str = "", sender_phone: str = "",
                    attachments: list = None, created_at: str = None):
    db = get_db()
    dados = {
        "account_id": account_id,
        "conversation_id": conversation_id,
        "inbox_id": inbox_id,
        "chatwoot_message_id": chatwoot_message_id,
        "message_type": message_type,
        "content": content or "",
        "sender_name": sender_name,
        "sender_phone": sender_phone,
    }
    if attachments:
        dados["attachments"] = attachments
    if created_at:
        dados["created_at_chatwoot"] = created_at
    try:
        db.table("ia_mensagens").upsert(
            dados, on_conflict="account_id,conversation_id,chatwoot_message_id"
        ).execute()
    except Exception:
        # Tabela pode não existir ainda — ignora silenciosamente
        pass


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


def listar_agendamentos_pendentes() -> list:
    """Retorna agendamentos com status 'agendado' de hoje em diante."""
    db = get_db()
    from datetime import date
    resp = (
        db.table("ia_agendamentos")
        .select("id,account_id,conversation_id,contact_name,contact_phone,scheduled_date,scheduled_time,advogada,lembrete_enviado")
        .eq("status", "agendado")
        .gte("scheduled_date", date.today().isoformat())
        .execute()
    )
    return resp.data if resp and resp.data else []


def marcar_lembrete_enviado(agendamento_id: int):
    """Marca que o lembrete foi enviado para este agendamento."""
    db = get_db()
    db.table("ia_agendamentos").update({
        "lembrete_enviado": True,
        "updated_at": "now()",
    }).eq("id", agendamento_id).execute()


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
    for tabela in ("ia_transcricoes", "ia_agendamentos", "ia_leads", "ia_conversations", "ia_inatividade", "ia_audiencias", "ia_advogados"):
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

_CONFIG_COLUMNS = (
    "account_id,nome,nome_completo,nome_escritorio,endereco,telefone,especialidade,"
    "plano,ativo,chatwoot_url,chatwoot_token,inbox_id,inboxes,team_id,ia_agent_id,"
    "ia_ativa,openai_api_key,transcricao_ativa,inatividade_ativa,limite_followup,email_agenda,"
    "disponibilidade,duracao_agendamento,horas_inicial_busca,quantidade_dias_a_buscar,"
    "dia_ciclo,meta_waba_id,meta_access_token,template_audiencia,"
    "id_notificacao_cliente,id_notificacao_convertido,"
    "config_lembrete_consulta,modo_teste"
)


def carregar_config_cliente(account_id: int) -> dict | None:
    db = get_db()
    try:
        resp = (
            db.table("ia_clientes_config")
            .select(_CONFIG_COLUMNS)
            .eq("account_id", account_id)
            .maybe_single()
            .execute()
        )
        if resp and resp.data:
            return resp.data
    except Exception:
        pass
    return None


def salvar_config_cliente(account_id: int, config: dict):
    db = get_db()
    payload = {"account_id": account_id, "updated_at": "now()"}
    campos_validos = _CONFIG_COLUMNS.split(",")
    for campo in campos_validos:
        campo = campo.strip()
        if campo in config and campo != "account_id":
            payload[campo] = config[campo]
    # Normalizar especialidade antes de salvar
    if "especialidade" in payload:
        payload["especialidade"] = normalizar_especialidade(payload["especialidade"])
    db.table("ia_clientes_config").upsert(payload, on_conflict="account_id").execute()


def listar_configs_clientes() -> list:
    db = get_db()
    resp = db.table("ia_clientes_config").select(_CONFIG_COLUMNS).order("account_id").execute()
    return resp.data or []


def deletar_config_cliente(account_id: int):
    db = get_db()
    db.table("ia_clientes_config").delete().eq("account_id", account_id).execute()


# ── SUGESTÕES DE PROMPT ──────────────────────────────────────

def upsert_sugestao(account_id: int, fase: str, conteudo_sugerido: str, user_id: str, user_nome: str = ""):
    db = get_db()
    # Verificar se já existe sugestão pendente para este account+fase+user
    existing = db.table("ia_prompt_sugestoes").select("id").eq(
        "account_id", account_id
    ).eq("fase", fase).eq("user_id", user_id).eq("status", "pendente").maybe_single().execute()
    if existing and existing.data:
        # Atualizar a existente
        db.table("ia_prompt_sugestoes").update({
            "conteudo_sugerido": conteudo_sugerido,
            "user_nome": user_nome,
            "updated_at": "now()",
        }).eq("id", existing.data["id"]).execute()
        return existing.data["id"]
    else:
        # Criar nova
        resp = db.table("ia_prompt_sugestoes").insert({
            "account_id": account_id,
            "fase": fase,
            "conteudo_sugerido": conteudo_sugerido,
            "user_id": user_id,
            "user_nome": user_nome,
            "status": "pendente",
        }).execute()
        return resp.data[0]["id"] if resp.data else None


def listar_sugestoes_pendentes(account_id: int = None) -> list:
    db = get_db()
    q = db.table("ia_prompt_sugestoes").select("*").eq("status", "pendente").order("updated_at", desc=True)
    if account_id:
        q = q.eq("account_id", account_id)
    return q.execute().data or []


def listar_sugestoes_por_status(status: str = "pendente", account_id: int = None) -> list:
    db = get_db()
    q = db.table("ia_prompt_sugestoes").select("*").eq("status", status).order("updated_at", desc=True)
    if account_id:
        q = q.eq("account_id", account_id)
    return q.execute().data or []


def listar_todas_sugestoes_recentes(limit: int = 50) -> list:
    """Lista todas as sugestões recentes (qualquer status) para o super_admin."""
    db = get_db()
    return db.table("ia_prompt_sugestoes").select("*").order("updated_at", desc=True).limit(limit).execute().data or []


def listar_sugestoes_usuario(account_id: int, user_id: str) -> list:
    db = get_db()
    return db.table("ia_prompt_sugestoes").select("*").eq(
        "account_id", account_id
    ).eq("user_id", user_id).eq("status", "pendente").execute().data or []


def buscar_sugestao(sugestao_id: str) -> dict | None:
    db = get_db()
    try:
        resp = db.table("ia_prompt_sugestoes").select("*").eq("id", sugestao_id).maybe_single().execute()
        return resp.data if resp else None
    except Exception:
        return None


def atualizar_status_sugestao(sugestao_id: str, status: str, admin_nota: str = None):
    db = get_db()
    dados = {"status": status, "updated_at": "now()"}
    if admin_nota:
        dados["admin_nota"] = admin_nota
    db.table("ia_prompt_sugestoes").update(dados).eq("id", sugestao_id).execute()


# ── RELATÓRIOS ───────────────────────────────────────────────

def contar_conversas(account_id: int, data_inicio: str, data_fim: str) -> int:
    db = get_db()
    try:
        resp = db.table("ia_conversations").select("id", count="exact").eq(
            "account_id", account_id
        ).gte("created_at", data_inicio).lte("created_at", data_fim).execute()
        return resp.count or 0
    except Exception:
        return 0


def contar_leads_por_status(account_id: int, data_inicio: str, data_fim: str) -> dict:
    db = get_db()
    resultado = {"em_atendimento": 0, "convertido": 0, "inviavel": 0, "transferido": 0, "perdido": 0}
    try:
        resp = db.table("ia_leads").select("status").eq(
            "account_id", account_id
        ).gte("created_at", data_inicio).lte("created_at", data_fim).execute()
        for r in (resp.data or []):
            s = r.get("status", "em_atendimento")
            if s in resultado:
                resultado[s] += 1
    except Exception:
        pass
    return resultado


def contar_agendamentos(account_id: int, data_inicio: str, data_fim: str) -> int:
    db = get_db()
    try:
        resp = db.table("ia_agendamentos").select("id", count="exact").eq(
            "account_id", account_id
        ).gte("created_at", data_inicio).lte("created_at", data_fim).execute()
        return resp.count or 0
    except Exception:
        return 0


def contar_transcricoes(account_id: int, data_inicio: str, data_fim: str) -> int:
    db = get_db()
    try:
        resp = db.table("ia_transcricoes").select("id", count="exact").eq(
            "account_id", account_id
        ).gte("created_at", data_inicio).lte("created_at", data_fim).execute()
        return resp.count or 0
    except Exception:
        return 0


# ── USO MENSAL ───────────────────────────────────────────────

def registrar_uso_mensal(account_id: int, conversation_id: int, mes: str):
    """Registra que uma conversa teve atividade neste mês (upsert, conta 1x por mês)."""
    db = get_db()
    try:
        db.table("ia_uso_mensal").upsert({
            "account_id": account_id,
            "conversation_id": conversation_id,
            "mes": mes,
        }, on_conflict="account_id,conversation_id,mes").execute()
    except Exception as e:
        logger.warning(f"Erro ao registrar uso mensal: {e}")


def contar_uso_mensal(account_id: int, mes: str) -> int:
    """Conta conversas únicas que tiveram atividade no mês."""
    db = get_db()
    try:
        resp = db.table("ia_uso_mensal").select("id", count="exact").eq(
            "account_id", account_id
        ).eq("mes", mes).execute()
        return resp.count or 0
    except Exception:
        return 0


def contar_uso_por_periodo(account_id: int, data_inicio: str, data_fim: str) -> int:
    """Conta conversas únicas por período de datas (created_at), independente do ciclo_id salvo.
    Fallback: se created_at não existir, conta pelo campo 'mes' derivado do mês da data_inicio."""
    db = get_db()
    try:
        resp = db.table("ia_uso_mensal").select("id", count="exact").eq(
            "account_id", account_id
        ).gte("created_at", data_inicio).lt("created_at", data_fim).execute()
        return resp.count or 0
    except Exception:
        # Fallback: usar o campo mes (ciclo_id derivado do mês de início)
        try:
            mes = data_inicio[:7]  # ex: "2026-03"
            return contar_uso_mensal(account_id, mes)
        except Exception:
            return 0


def historico_uso_mensal(account_id: int, meses: list[str]) -> dict:
    """Retorna contagem de conversas por mês."""
    resultado = {}
    for mes in meses:
        resultado[mes] = contar_uso_mensal(account_id, mes)
    return resultado


def historico_uso_por_periodos(account_id: int, periodos: list[dict]) -> dict:
    """Retorna contagem de conversas por período de datas.
    periodos: [{"ciclo_id": "2026-03", "inicio": "...", "fim": "..."}]"""
    resultado = {}
    for p in periodos:
        resultado[p["ciclo_id"]] = contar_uso_por_periodo(account_id, p["inicio"], p["fim"])
    return resultado


# ── AUDIÊNCIAS (Supabase) ────────────────────────────────────

def listar_audiencias_db(account_id: int = None) -> list:
    db = get_db()
    query = db.table("ia_audiencias").select("*")
    if account_id is not None:
        query = query.eq("account_id", account_id)
    resp = query.order("data").execute()
    return resp.data or []


def get_audiencia_db(audiencia_id: str) -> dict | None:
    db = get_db()
    resp = db.table("ia_audiencias").select("*").eq("id", audiencia_id).maybe_single().execute()
    return resp.data


def inserir_audiencia_db(audiencia: dict) -> dict:
    db = get_db()
    resp = db.table("ia_audiencias").insert(audiencia).execute()
    return resp.data[0] if resp.data else audiencia


def atualizar_audiencia_db(audiencia_id: str, dados: dict) -> dict | None:
    db = get_db()
    dados["updated_at"] = "now()"
    resp = db.table("ia_audiencias").update(dados).eq("id", audiencia_id).execute()
    return resp.data[0] if resp.data else None


def deletar_audiencia_db(audiencia_id: str) -> bool:
    db = get_db()
    resp = db.table("ia_audiencias").delete().eq("id", audiencia_id).execute()
    return bool(resp.data)


# ── TIPOS DE AUDIÊNCIA / HEARING_TYPES (Supabase) ────────────

def listar_hearing_types_db(account_id: int = None) -> list:
    """Lista hearing_types. Converte campos para o formato usado pelo dashboard."""
    db = get_db()
    query = db.table("ia_hearing_types").select("*")
    if account_id is not None:
        query = query.eq("id_account", account_id)
    resp = query.order("created_at").execute()
    tipos = []
    for r in (resp.data or []):
        tipos.append({
            "id": r["id"],
            "nome": r.get("name", ""),
            "descricao": r.get("description", ""),
            "ativo": r.get("is_active", True),
            "mensagens": r.get("mensagens") or [],
        })
    return tipos


def get_hearing_type_db(tipo_id: str) -> dict | None:
    db = get_db()
    resp = db.table("ia_hearing_types").select("*").eq("id", tipo_id).maybe_single().execute()
    if not resp.data:
        return None
    r = resp.data
    return {
        "id": r["id"],
        "nome": r.get("name", ""),
        "descricao": r.get("description", ""),
        "ativo": r.get("is_active", True),
        "mensagens": r.get("mensagens") or [],
    }


def inserir_hearing_type_db(nome: str, descricao: str, ativo: bool = True, id_account: int = None) -> dict:
    db = get_db()
    payload = {
        "name": nome,
        "description": descricao,
        "is_active": ativo,
        "mensagens": [],
    }
    if id_account is not None:
        payload["id_account"] = id_account
    resp = db.table("ia_hearing_types").insert(payload).execute()
    r = resp.data[0] if resp.data else payload
    return {
        "id": r.get("id", ""),
        "nome": r.get("name", nome),
        "descricao": r.get("description", descricao),
        "ativo": r.get("is_active", ativo),
        "mensagens": r.get("mensagens") or [],
    }


def atualizar_hearing_type_db(tipo_id: str, dados: dict) -> dict | None:
    db = get_db()
    payload = {"updated_at": "now()"}
    if "nome" in dados:
        payload["name"] = dados["nome"]
    if "descricao" in dados:
        payload["description"] = dados["descricao"]
    if "ativo" in dados:
        payload["is_active"] = dados["ativo"]
    if "mensagens" in dados:
        payload["mensagens"] = dados["mensagens"]
    resp = db.table("ia_hearing_types").update(payload).eq("id", tipo_id).execute()
    if not resp.data:
        return None
    r = resp.data[0]
    return {
        "id": r["id"],
        "nome": r.get("name", ""),
        "descricao": r.get("description", ""),
        "ativo": r.get("is_active", True),
        "mensagens": r.get("mensagens") or [],
    }


def deletar_hearing_type_db(tipo_id: str) -> bool:
    db = get_db()
    resp = db.table("ia_hearing_types").delete().eq("id", tipo_id).execute()
    return bool(resp.data)


# ── ADVOGADOS ────────────────────────────────────────────────

def listar_advogados(account_id: int) -> list:
    db = get_db()
    resp = (
        db.table("ia_advogados")
        .select("*")
        .eq("account_id", account_id)
        .order("nome")
        .execute()
    )
    return resp.data or []


def get_advogado(advogado_id: str) -> dict | None:
    db = get_db()
    resp = db.table("ia_advogados").select("*").eq("id", advogado_id).maybe_single().execute()
    return resp.data


def inserir_advogado(dados: dict) -> dict:
    db = get_db()
    if "especialidade" in dados:
        dados["especialidade"] = normalizar_especialidade(dados["especialidade"])
    resp = db.table("ia_advogados").insert(dados).execute()
    return resp.data[0] if resp.data else dados


def atualizar_advogado(advogado_id: str, dados: dict) -> dict | None:
    db = get_db()
    dados["updated_at"] = "now()"
    if "especialidade" in dados:
        dados["especialidade"] = normalizar_especialidade(dados["especialidade"])
    resp = db.table("ia_advogados").update(dados).eq("id", advogado_id).execute()
    return resp.data[0] if resp.data else None


def deletar_advogado(advogado_id: str) -> bool:
    db = get_db()
    resp = db.table("ia_advogados").delete().eq("id", advogado_id).execute()
    return bool(resp.data)


# ── BLOQUEIOS DE AGENDA ──────────────────────────────────────

def listar_bloqueios_agenda(account_id: int) -> list:
    db = get_db()
    resp = (
        db.table("ia_bloqueios_agenda")
        .select("*")
        .eq("account_id", account_id)
        .order("data_inicio", desc=False)
        .execute()
    )
    return resp.data or []


def inserir_bloqueio_agenda(dados: dict) -> dict:
    db = get_db()
    resp = db.table("ia_bloqueios_agenda").insert(dados).execute()
    return resp.data[0] if resp.data else dados


def deletar_bloqueio_agenda(bloqueio_id: str) -> bool:
    db = get_db()
    resp = db.table("ia_bloqueios_agenda").delete().eq("id", bloqueio_id).execute()
    return bool(resp.data)


def listar_bloqueios_agenda_ativos(account_id: int) -> list:
    """Retorna bloqueios cujo data_fim ainda não passou."""
    db = get_db()
    from datetime import datetime, timezone, timedelta
    agora = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    resp = (
        db.table("ia_bloqueios_agenda")
        .select("*")
        .eq("account_id", account_id)
        .gte("data_fim", agora)
        .execute()
    )
    return resp.data or []


# Mapa de sinônimos → especialidade padronizada
_ESPECIALIDADE_SINONIMOS = {
    # Previdenciário
    "previdenciario": "Previdenciário",
    "previdenciária": "Previdenciário",
    "previdenciaria": "Previdenciário",
    "previdenciário": "Previdenciário",
    "previdencia": "Previdenciário",
    "inss": "Previdenciário",
    "auxilio": "Previdenciário",
    "auxílio": "Previdenciário",
    "beneficio": "Previdenciário",
    "benefício": "Previdenciário",
    "aposentadoria": "Previdenciário",
    "bpc": "Previdenciário",
    "loas": "Previdenciário",
    # Trabalhista
    "trabalhista": "Trabalhista",
    "trabalhista": "Trabalhista",
    "trabalho": "Trabalhista",
    "clt": "Trabalhista",
    "rescisao": "Trabalhista",
    "rescisão": "Trabalhista",
    "demissao": "Trabalhista",
    "demissão": "Trabalhista",
    # Cível
    "civel": "Cível",
    "cível": "Cível",
    "civil": "Cível",
}


def normalizar_especialidade(texto: str) -> str:
    """Normaliza uma ou mais especialidades separadas por vírgula para o formato padrão."""
    if not texto:
        return ""
    partes = [p.strip() for p in texto.split(",") if p.strip()]
    normalizadas = set()
    for parte in partes:
        chave = parte.lower().strip()
        padrao = _ESPECIALIDADE_SINONIMOS.get(chave, parte.strip().title())
        normalizadas.add(padrao)
    return ",".join(sorted(normalizadas))


def listar_advogados_por_especialidade(account_id: int, especialidade: str) -> list:
    """Busca advogados ativos da conta cuja especialidade faz match com a especialidade da conta.
    Suporta múltiplas especialidades separadas por vírgula em ambos os lados."""
    db = get_db()
    resp = (
        db.table("ia_advogados")
        .select("*")
        .eq("account_id", account_id)
        .eq("ativo", True)
        .order("nome")
        .execute()
    )
    todos = resp.data or []
    if not especialidade:
        return todos

    # Normalizar especialidade buscada
    esp_normalizada = normalizar_especialidade(especialidade)
    esp_conta = {e.strip().lower() for e in esp_normalizada.split(",") if e.strip()}

    resultado = []
    for adv in todos:
        esp_adv_raw = normalizar_especialidade(adv.get("especialidade") or "")
        esp_adv = {e.strip().lower() for e in esp_adv_raw.split(",") if e.strip()}
        if esp_adv & esp_conta:
            resultado.append(adv)

    # Fallback: se nenhum advogado bateu com a especialidade, usar todos os ativos
    if not resultado and todos:
        return todos

    return resultado


# ── REMARKETING ──────────────────────────────────────────────

def listar_campanhas_remarketing(account_id: int) -> list:
    db = get_db()
    resp = (
        db.table("ia_remarketing_campanhas")
        .select("*")
        .eq("account_id", account_id)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


def listar_todas_campanhas_ativas() -> list:
    db = get_db()
    resp = (
        db.table("ia_remarketing_campanhas")
        .select("*")
        .eq("ativo", True)
        .execute()
    )
    return resp.data or []


def get_campanha_remarketing(campanha_id: int) -> dict | None:
    db = get_db()
    resp = (
        db.table("ia_remarketing_campanhas")
        .select("*")
        .eq("id", campanha_id)
        .maybe_single()
        .execute()
    )
    return resp.data if resp else None


def inserir_campanha_remarketing(account_id: int, dados: dict) -> dict:
    db = get_db()
    payload = {
        "account_id": account_id,
        "nome": dados.get("nome", ""),
        "dias_inatividade": dados.get("dias_inatividade", 30),
        "limite_diario": dados.get("limite_diario", 200),
        "mensagem": dados.get("mensagem", ""),
        "template_whatsapp": dados.get("template_whatsapp") or None,
        "image_url": dados.get("image_url") or None,
        "inbox_id": dados.get("inbox_id") or None,
        "inbox_envio_id": dados.get("inbox_envio_id") or None,
        "ativo": dados.get("ativo", False),
    }
    resp = db.table("ia_remarketing_campanhas").insert(payload).execute()
    return (resp.data or [{}])[0]


def atualizar_campanha_remarketing(campanha_id: int, dados: dict) -> dict | None:
    db = get_db()
    payload = {"updated_at": "now()"}
    campos = ["nome", "dias_inatividade", "limite_diario", "mensagem", "template_whatsapp", "image_url", "inbox_id", "inbox_envio_id", "ativo"]
    for c in campos:
        if c in dados:
            payload[c] = dados[c]
    resp = (
        db.table("ia_remarketing_campanhas")
        .update(payload)
        .eq("id", campanha_id)
        .execute()
    )
    return (resp.data or [None])[0]


def deletar_campanha_remarketing(campanha_id: int) -> bool:
    db = get_db()
    resp = db.table("ia_remarketing_campanhas").delete().eq("id", campanha_id).execute()
    return bool(resp.data)


def contar_envios_remarketing_hoje(campanha_id: int) -> int:
    """Conta quantos envios foram feitos hoje para uma campanha."""
    db = get_db()
    from datetime import datetime, timezone, timedelta
    tz_br = timezone(timedelta(hours=-3))
    hoje = datetime.now(tz_br).strftime("%Y-%m-%d")
    resp = (
        db.table("ia_remarketing_envios")
        .select("id", count="exact")
        .eq("campanha_id", campanha_id)
        .gte("enviado_em", f"{hoje}T00:00:00-03:00")
        .execute()
    )
    return resp.count or 0


def registrar_envio_remarketing(campanha_id: int, account_id: int, conversation_id: int, status: str = "enviado"):
    db = get_db()
    db.table("ia_remarketing_envios").insert({
        "campanha_id": campanha_id,
        "account_id": account_id,
        "conversation_id": conversation_id,
        "status": status,
    }).execute()


def buscar_conversas_elegiveis_remarketing(account_id: int, campanha_id: int, dias_inatividade: int, limite: int, inbox_id: int = None) -> list:
    """
    Busca leads inativos há X dias que ainda não foram contactados por esta campanha.
    Se inbox_id for fornecido, filtra apenas leads dessa inbox.
    Ordena do mais antigo para o mais recente.
    """
    db = get_db()
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=dias_inatividade)).isoformat()

    # Buscar todos os conversation_ids já contactados por esta campanha
    envios_resp = (
        db.table("ia_remarketing_envios")
        .select("conversation_id")
        .eq("campanha_id", campanha_id)
        .execute()
    )
    ja_enviados = {e["conversation_id"] for e in (envios_resp.data or [])}

    # Buscar leads inativos em lotes até encontrar suficientes não-contactados
    _offset = 0
    batch_size = 100
    elegiveis = []

    while len(elegiveis) < limite:
        query = (
            db.table("ia_leads")
            .select("account_id,conversation_id,inbox_id,contact_name,contact_phone")
            .eq("account_id", account_id)
            .lte("updated_at", cutoff)
            .order("updated_at")
        )
        if inbox_id:
            query = query.eq("inbox_id", inbox_id)
        resp = query.range(_offset, _offset + batch_size - 1).execute()
        leads = resp.data or []
        if not leads:
            break

        for l in leads:
            if l["conversation_id"] not in ja_enviados:
                elegiveis.append(l)
                if len(elegiveis) >= limite:
                    break

        _offset += batch_size

    return elegiveis[:limite]


def listar_envios_remarketing(campanha_id: int, limite: int = 50, offset: int = 0) -> list:
    db = get_db()
    resp = (
        db.table("ia_remarketing_envios")
        .select("*")
        .eq("campanha_id", campanha_id)
        .order("enviado_em", desc=True)
        .range(offset, offset + limite - 1)
        .execute()
    )
    return resp.data or []


def contar_total_envios_remarketing(campanha_id: int) -> int:
    db = get_db()
    resp = (
        db.table("ia_remarketing_envios")
        .select("id", count="exact")
        .eq("campanha_id", campanha_id)
        .execute()
    )
    return resp.count or 0


def contar_elegiveis_remarketing(account_id: int, dias_inatividade: int, inbox_id: int = None) -> int:
    """Conta quantas conversas estão elegíveis para remarketing."""
    db = get_db()
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=dias_inatividade)).isoformat()
    query = (
        db.table("ia_leads")
        .select("id", count="exact")
        .eq("account_id", account_id)
        .lte("updated_at", cutoff)
    )
    if inbox_id:
        query = query.eq("inbox_id", inbox_id)
    resp = query.execute()
    return resp.count or 0


# ── ONBOARDING ──────────────────────────────────────────────

def criar_onboarding(account_id: int, token: str) -> dict:
    """Cria ou recria onboarding para a conta (upsert)."""
    db = get_db()
    resp = db.table("ia_onboarding").upsert({
        "account_id": account_id,
        "token": token,
        "status": "draft",
        "form_data": {},
        "updated_at": "now()",
    }, on_conflict="account_id").execute()
    return resp.data[0] if resp.data else {}


def get_onboarding_by_token(token: str) -> dict | None:
    db = get_db()
    resp = db.table("ia_onboarding").select("*").eq("token", token).maybe_single().execute()
    return resp.data


def get_onboarding_by_account(account_id: int) -> dict | None:
    db = get_db()
    resp = db.table("ia_onboarding").select("*").eq("account_id", account_id).maybe_single().execute()
    return resp.data


def atualizar_onboarding_draft(token: str, form_data: dict):
    db = get_db()
    db.table("ia_onboarding").update({
        "form_data": form_data,
        "updated_at": "now()",
    }).eq("token", token).eq("status", "draft").execute()


def submeter_onboarding(token: str):
    db = get_db()
    db.table("ia_onboarding").update({
        "status": "submitted",
        "submitted_at": "now()",
        "updated_at": "now()",
    }).eq("token", token).eq("status", "draft").execute()


# ── ZAPSIGN ──────────────────────────────────────────────────

def get_zapsign_config(account_id: int) -> dict | None:
    """Busca config ZapSign da conta."""
    db = get_db()
    resp = db.table("ia_zapsign_config").select("*").eq("account_id", account_id).maybe_single().execute()
    return resp.data


def salvar_zapsign_config(account_id: int, config: dict):
    """Salva ou atualiza config ZapSign da conta (upsert)."""
    db = get_db()
    data = {"account_id": account_id, **config, "updated_at": "now()"}
    db.table("ia_zapsign_config").upsert(data, on_conflict="account_id").execute()


def listar_zapsign_docs(account_id: int, limit: int = 50, offset: int = 0) -> list:
    """Lista documentos ZapSign salvos localmente."""
    db = get_db()
    resp = (db.table("ia_zapsign_docs")
            .select("*")
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute())
    return resp.data or []


def upsert_zapsign_doc(account_id: int, doc: dict):
    """Salva/atualiza documento ZapSign (usa doc_token como chave única)."""
    db = get_db()
    data = {
        "account_id": account_id,
        "doc_token": doc["token"],
        "nome": doc.get("name", ""),
        "status": doc.get("status", ""),
        "external_id": doc.get("external_id", ""),
        "created_at_zapsign": doc.get("created_at", ""),
        "signers": doc.get("signers", []),
        "updated_at": "now()",
    }
    db.table("ia_zapsign_docs").upsert(data, on_conflict="doc_token").execute()


def contar_zapsign_docs_por_status(account_id: int) -> dict:
    """Conta documentos por status para métricas."""
    db = get_db()
    resp = db.table("ia_zapsign_docs").select("status").eq("account_id", account_id).execute()
    contagem = {}
    for row in (resp.data or []):
        s = row.get("status", "unknown")
        contagem[s] = contagem.get(s, 0) + 1
    return contagem


def atualizar_zapsign_doc_status(doc_token: str, status: str):
    """Atualiza status do doc ZapSign via webhook (pode não ter doc completo)."""
    db = get_db()
    db.table("ia_zapsign_docs").update({
        "status": status,
        "updated_at": "now()",
    }).eq("doc_token", doc_token).execute()


# ── ZAPSIGN FOLLOW-UP (por conversa, não por documento) ──────

def upsert_zapsign_followup(account_id: int, conversation_id: int, inbox_id: int | None,
                            doc_token: str, stagio: int, proximo_disparo: str):
    """Cria follow-up ou adiciona doc_token à lista de uma conversa existente.
    Lógica: 1 follow-up por conversa. Se já existe, apenas adiciona o doc_token à lista."""
    db = get_db()
    import json as _json

    # Verificar se já existe follow-up ativo pra essa conversa
    existing = (db.table("ia_zapsign_followup")
                .select("id, doc_tokens, ativo")
                .eq("account_id", account_id)
                .eq("conversation_id", conversation_id)
                .maybe_single().execute())

    if existing.data and existing.data.get("ativo"):
        # Já existe e está ativo — apenas adicionar doc_token à lista
        current_tokens = existing.data.get("doc_tokens") or []
        if isinstance(current_tokens, str):
            current_tokens = _json.loads(current_tokens)
        if doc_token not in current_tokens:
            current_tokens.append(doc_token)
            db.table("ia_zapsign_followup").update({
                "doc_tokens": current_tokens,
                "updated_at": "now()",
            }).eq("id", existing.data["id"]).execute()
    elif existing.data and not existing.data.get("ativo"):
        # Existe mas inativo — reativar com novo doc
        current_tokens = existing.data.get("doc_tokens") or []
        if isinstance(current_tokens, str):
            current_tokens = _json.loads(current_tokens)
        if doc_token not in current_tokens:
            current_tokens.append(doc_token)
        db.table("ia_zapsign_followup").update({
            "doc_tokens": current_tokens,
            "doc_token": doc_token,
            "stagio": stagio,
            "proximo_disparo": proximo_disparo,
            "ativo": True,
            "updated_at": "now()",
        }).eq("id", existing.data["id"]).execute()
    else:
        # Não existe — criar novo
        db.table("ia_zapsign_followup").insert({
            "account_id": account_id,
            "conversation_id": conversation_id,
            "inbox_id": inbox_id,
            "doc_token": doc_token,
            "doc_tokens": [doc_token],
            "stagio": stagio,
            "proximo_disparo": proximo_disparo,
            "ativo": True,
        }).execute()


def get_zapsign_followups_pendentes() -> list:
    """Busca follow-ups ativos com proximo_disparo <= agora."""
    db = get_db()
    from datetime import datetime, timezone
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    resp = (
        db.table("ia_zapsign_followup")
        .select("*")
        .eq("ativo", True)
        .lte("proximo_disparo", agora)
        .execute()
    )
    return resp.data or []


def desativar_zapsign_followup_conversa(account_id: int, conversation_id: int):
    """Desativa follow-up de uma conversa."""
    db = get_db()
    db.table("ia_zapsign_followup").update({
        "ativo": False, "updated_at": "now()",
    }).eq("account_id", account_id).eq(
        "conversation_id", conversation_id
    ).eq("ativo", True).execute()


def remover_doc_token_followup(doc_token: str) -> bool:
    """Remove doc_token da lista de um follow-up (quando doc é assinado).
    Se não sobrar nenhum doc pendente, desativa o follow-up.
    Retorna True se follow-up foi desativado (todos assinaram)."""
    db = get_db()
    import json as _json

    # Buscar todos follow-ups ativos que contêm esse doc_token
    resp = db.table("ia_zapsign_followup").select("*").eq("ativo", True).execute()
    desativou = False

    for row in (resp.data or []):
        tokens = row.get("doc_tokens") or []
        if isinstance(tokens, str):
            tokens = _json.loads(tokens)

        if doc_token in tokens:
            tokens.remove(doc_token)
            if len(tokens) == 0:
                # Todos docs assinados — desativar follow-up
                db.table("ia_zapsign_followup").update({
                    "doc_tokens": [],
                    "ativo": False,
                    "updated_at": "now()",
                }).eq("id", row["id"]).execute()
                desativou = True
            else:
                # Ainda faltam docs — atualizar lista
                db.table("ia_zapsign_followup").update({
                    "doc_tokens": tokens,
                    "updated_at": "now()",
                }).eq("id", row["id"]).execute()

    return desativou


def listar_zapsign_followups(account_id: int, limit: int = 20, offset: int = 0) -> dict:
    """Lista follow-ups com dados do lead e nomes dos documentos, paginado."""
    db = get_db()
    import json as _json

    # Buscar follow-ups paginado
    resp = (db.table("ia_zapsign_followup")
            .select("*")
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute())
    followups = resp.data or []

    # Contar total
    resp_total = (db.table("ia_zapsign_followup")
                  .select("id", count="exact")
                  .eq("account_id", account_id)
                  .execute())
    total = resp_total.count if resp_total.count else len(followups)

    if not followups:
        return {"items": [], "total": total}

    # Buscar leads por conversation_id
    conv_ids = list(set(f["conversation_id"] for f in followups))
    leads_resp = (db.table("ia_leads")
                  .select("conversation_id, contact_name, contact_phone")
                  .eq("account_id", account_id)
                  .in_("conversation_id", conv_ids)
                  .execute())
    leads_map = {l["conversation_id"]: l for l in (leads_resp.data or [])}

    # Buscar nomes dos docs pelos tokens
    all_tokens = []
    for f in followups:
        tokens = f.get("doc_tokens") or []
        if isinstance(tokens, str):
            tokens = _json.loads(tokens)
        all_tokens.extend(tokens)
    all_tokens = list(set(all_tokens))

    docs_map = {}
    if all_tokens:
        docs_resp = (db.table("ia_zapsign_docs")
                     .select("doc_token, nome, status")
                     .in_("doc_token", all_tokens)
                     .execute())
        docs_map = {d["doc_token"]: d for d in (docs_resp.data or [])}

    # Montar resultado
    items = []
    for f in followups:
        lead = leads_map.get(f["conversation_id"], {})
        tokens = f.get("doc_tokens") or []
        if isinstance(tokens, str):
            tokens = _json.loads(tokens)

        docs = []
        for t in tokens:
            doc_info = docs_map.get(t, {})
            docs.append({
                "token": t,
                "nome": doc_info.get("nome", t[:12] + "..."),
                "status": doc_info.get("status", "pending"),
            })

        items.append({
            "id": f["id"],
            "conversation_id": f["conversation_id"],
            "contact_name": lead.get("contact_name", "-"),
            "contact_phone": lead.get("contact_phone", "-"),
            "docs": docs,
            "stagio": f["stagio"],
            "ativo": f["ativo"],
            "proximo_disparo": f.get("proximo_disparo", ""),
            "created_at": f.get("created_at", ""),
        })

    return {"items": items, "total": total}


def get_zapsign_followup_stats(account_id: int) -> dict:
    """Retorna métricas dos follow-ups por conversa."""
    db = get_db()
    resp = db.table("ia_zapsign_followup").select(
        "ativo, stagio"
    ).eq("account_id", account_id).execute()
    rows = resp.data or []

    total = len(rows)
    pendentes = sum(1 for r in rows if r["ativo"])
    # Inativos com estágio <= 3 = assinaram todos os docs antes de esgotar
    assinados = sum(1 for r in rows if not r["ativo"] and r["stagio"] <= 3)
    # Inativos com estágio > 3 = lembretes esgotados sem assinar
    esgotados = sum(1 for r in rows if not r["ativo"] and r["stagio"] > 3)

    return {
        "total": total,
        "pendentes": pendentes,
        "assinados": assinados,
        "esgotados": esgotados,
    }
