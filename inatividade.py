from openai import OpenAI
from datetime import datetime, timezone, timedelta
from db import upsert_inatividade, get_inatividades_pendentes, desativar_inatividade
import asyncio
import httpx
import json
import logging
import os

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Limite padrão: inatividade dispara no máximo 3 vezes (configurável por conta via config_inatividade.limite)
LIMITE_INATIVIDADE_PADRAO = 3


# Override de limite por conta (quando não está no banco ainda)
_LIMITE_POR_CONTA = {
    17: 5,
}


def _limite_inatividade(account_id: int = None) -> int:
    """Retorna o limite de follow-ups para a conta (padrão: 3)."""
    if account_id:
        # Override direto no código
        if account_id in _LIMITE_POR_CONTA:
            return _LIMITE_POR_CONTA[account_id]
        # Config do banco
        cfg = carregar_config_inatividade(account_id)
        limite = cfg.get("limite")
        if limite and isinstance(limite, int) and limite > 0:
            return limite
    return LIMITE_INATIVIDADE_PADRAO


# ── CONFIG ────────────────────────────────────────────────────

def _carregar_config_global() -> dict:
    """Carrega config global (fallback) do arquivo."""
    path = os.path.join(BASE_DIR, "config", "inatividade.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def carregar_config_inatividade(account_id: int = None) -> dict:
    """Carrega config de inatividade: primeiro tenta por conta, depois fallback global."""
    if account_id:
        try:
            from db import carregar_config_cliente
            config = carregar_config_cliente(account_id)
            if config:
                cfg_conta = config.get("config_inatividade")
                if cfg_conta:
                    if isinstance(cfg_conta, str):
                        cfg_conta = json.loads(cfg_conta)
                    if cfg_conta.get("estagios"):
                        return cfg_conta
        except Exception:
            pass
    return _carregar_config_global()


def _estagio_info(stagio: int, account_id: int = None) -> dict | None:
    """Retorna {'horas': X, 'label': Y} para o estágio dado, ou None se não existir."""
    cfg = carregar_config_inatividade(account_id)
    for e in cfg.get("estagios", []):
        if e["stagio"] == stagio:
            return e
    return None


def _proximo_disparo(horas: int) -> str:
    """Retorna ISO timestamp UTC de agora + N horas."""
    dt = datetime.now(timezone.utc) + timedelta(hours=horas)
    return dt.isoformat()


# ── REGISTRO DE ATIVIDADE ─────────────────────────────────────

def registrar_atividade(account_id: int, conversation_id: int, inbox_id: int | None = None):
    """
    Chamado sempre que há atividade na conversa (cliente ou IA).
    Reseta o monitoramento para o estágio 1 com timer de 2h.
    """
    info = _estagio_info(1)
    if not info:
        return
    proximo = _proximo_disparo(info["horas"])
    try:
        upsert_inatividade(account_id, conversation_id, inbox_id, stagio=1, proximo_disparo=proximo)
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao registrar atividade conv={conversation_id}: {e}")


# ── WHATSAPP OFICIAL ──────────────────────────────────────────

_cache_inbox_type: dict[tuple, str] = {}  # (account_id, inbox_id) → channel_type


async def _get_inbox_channel_type(config_cliente: dict, inbox_id: int | None) -> str:
    """Retorna channel_type do inbox (ex: 'Channel::Whatsapp', 'Channel::Api'). Usa cache."""
    if not inbox_id:
        return ""
    key = (config_cliente["account_id"], inbox_id)
    if key in _cache_inbox_type:
        return _cache_inbox_type[key]
    try:
        url = f"{config_cliente['chatwoot_url']}/api/v1/accounts/{config_cliente['account_id']}/inboxes"
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(url, headers={"api_access_token": config_cliente["chatwoot_token"]})
            if resp.is_success:
                for inbox in resp.json().get("payload", []):
                    if inbox.get("id") == inbox_id:
                        ct = inbox.get("channel_type", "")
                        _cache_inbox_type[key] = ct
                        return ct
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao buscar channel_type inbox={inbox_id}: {e}")
    return ""


def _ultima_msg_cliente(historico: list) -> datetime | None:
    """Retorna o timestamp UTC da última mensagem do cliente no histórico."""
    for msg in reversed(historico):
        if msg.get("message_type") == 0:
            ts = msg.get("created_at")
            if ts:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
    return None


async def _enviar_template_chatwoot(chatwoot_url: str, token: str, account_id: int,
                                     conversation_id: int, template_name: str):
    """Envia um template WhatsApp via Chatwoot (para inboxes WhatsApp Oficial)."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    payload = {
        "message_type": "outgoing",
        "private": False,
        "template_params": {
            "name": template_name,
            "language": "pt_BR",
            "processed_params": {},
        },
    }
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(url, headers=headers, json=payload)
        resp.raise_for_status()


# ── CHATWOOT HELPERS ──────────────────────────────────────────

async def _buscar_historico(chatwoot_url: str, token: str, account_id: int, conversation_id: int) -> list:
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.get(url, headers={"api_access_token": token})
        resp.raise_for_status()
    msgs = resp.json().get("payload", [])
    return sorted(msgs, key=lambda m: m.get("created_at", 0))


async def _atualizar_labels(chatwoot_url: str, token: str, account_id: int, conversation_id: int, nova_label: str):
    """Remove todas as labels de inatividade e adiciona a nova."""
    cfg = carregar_config_inatividade(account_id)
    labels_remover = set(cfg.get("labels_remover", []))
    headers = {"api_access_token": token}
    labels_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"

    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.get(labels_url, headers=headers)
        existentes = resp.json().get("payload", []) if resp.is_success else []
        atualizadas = [l for l in existentes if l not in labels_remover]
        if nova_label not in atualizadas:
            atualizadas.append(nova_label)
        await http.post(labels_url, headers=headers, json={"labels": atualizadas}, timeout=10)

    logger.info(f"[inatividade] Label '{nova_label}' aplicada na conversa {conversation_id}")


async def _enviar_mensagem(chatwoot_url: str, token: str, account_id: int, conversation_id: int, texto: str):
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(url, headers=headers, json={
            "content": texto,
            "message_type": "outgoing",
            "private": False,
        })
        resp.raise_for_status()


# ── NOME DO AGENTE IA ─────────────────────────────────────────

_cache_nomes_ia: dict[int, str] = {}  # account_id → nome do agente IA


async def buscar_nome_agente_ia(config_cliente: dict) -> str:
    """Busca o nome do agente IA no Chatwoot pelo ia_agent_id. Usa cache por account_id."""
    account_id = config_cliente["account_id"]
    if account_id in _cache_nomes_ia:
        return _cache_nomes_ia[account_id]

    ia_agent_id = config_cliente.get("ia_agent_id")
    if not ia_agent_id:
        return "Assistente"

    try:
        url = f"{config_cliente['chatwoot_url']}/api/v1/accounts/{account_id}/agents"
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(url, headers={"api_access_token": config_cliente["chatwoot_token"]})
            if resp.is_success:
                agentes = resp.json()
                for agente in agentes:
                    if agente.get("id") == ia_agent_id:
                        nome = agente.get("name", "Assistente")
                        _cache_nomes_ia[account_id] = nome
                        logger.info(f"[inatividade] Nome do agente IA (account={account_id}): {nome}")
                        return nome
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao buscar nome do agente IA: {e}")

    return "Assistente"


# ── FORMATAÇÃO DO HISTÓRICO ───────────────────────────────────

def _formatar_historico(messages: list, nome_ia: str = "IA") -> str:
    linhas = []
    for msg in messages:
        if msg.get("private"):
            continue
        tipo = msg.get("message_type")
        content = msg.get("content") or "[mídia]"
        prefixo = "[Cliente]" if tipo == 0 else f"[{nome_ia}]"
        linhas.append(f"{prefixo}: {content}")
    return "\n".join(linhas)


# ── GERAÇÃO DA MENSAGEM VIA GPT ───────────────────────────────

def _data_hora_atual() -> str:
    tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).strftime("%d/%m/%Y - %H:%M")


def _carregar_prompt_inatividade(account_id: int) -> str:
    from ia import pasta_cliente, carregar_prompt
    pasta = pasta_cliente(account_id)
    if not pasta:
        return ""
    prompt_path = os.path.join(pasta, "prompt", "inatividade.md")
    if not os.path.exists(prompt_path):
        logger.warning(f"[inatividade] Prompt não encontrado: {prompt_path}")
        return ""
    return carregar_prompt(account_id, "inatividade.md")


def _gerar_mensagem(config_cliente: dict, historico_texto: str, stagio: int, nome_ia: str) -> str | None:
    prompt = _carregar_prompt_inatividade(config_cliente["account_id"])
    if not prompt:
        return None

    prompt = (
        prompt
        .replace("{data_hora_atual}", _data_hora_atual())
        .replace("{conversa}", historico_texto)
        .replace("{stagio}", str(stagio))
        .replace("{nome_ia}", nome_ia)
    )

    client = OpenAI(api_key=config_cliente["openai_api_key"])
    resp = client.chat.completions.create(
        model="gpt-5.2",
        messages=[{"role": "user", "content": prompt}],
        reasoning_effort="low",
    )
    return (resp.choices[0].message.content or "").strip()


# ── LEAD PERDIDO ──────────────────────────────────────────────

async def _marcar_lead_perdido(config_cliente: dict, conversation_id: int,
                                chatwoot_url: str, token: str, account_id: int):
    """Marca o lead como perdido no banco e move no kanban após esgotar follow-ups."""
    from db import get_db
    from ia import kanban_mover_card

    # Atualizar label para 'perdido'
    try:
        await _atualizar_labels(chatwoot_url, token, account_id, conversation_id, "perdido")
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao aplicar label 'perdido' conv={conversation_id}: {e}")

    # Atualizar status do lead no banco (update direto, sem sobrescrever dados)
    try:
        db = get_db()
        db.table("ia_leads").update({"status": "perdido", "updated_at": "now()"}).eq(
            "account_id", account_id
        ).eq("conversation_id", conversation_id).execute()
        logger.info(f"[inatividade] Lead marcado como perdido — conv={conversation_id}")
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao marcar lead como perdido conv={conversation_id}: {e}")

    # Mover card no kanban — verificar se lead já foi agendado (Aguardando Assinatura)
    # Se sim → "Não Assinou"; senão → "Não Respondeu"
    try:
        db = get_db()
        lead = db.table("ia_leads").select("status").eq(
            "account_id", account_id
        ).eq("conversation_id", conversation_id).maybe_single().execute()
        lead_status = (lead.data or {}).get("status", "") if lead else ""
        # Se o lead chegou a agendar (convertido ou agendado), vai pra "Não Assinou"
        kanban_action = "nao_assinou" if lead_status in ("agendado", "convertido") else "lead_perdido"
        await kanban_mover_card(chatwoot_url, token, account_id, conversation_id, "", kanban_action)
        etapa_nome = "Não Assinou" if kanban_action == "nao_assinou" else "Não Respondeu"
        logger.info(f"[inatividade] Card kanban movido para '{etapa_nome}' — conv={conversation_id}")
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao mover card kanban conv={conversation_id}: {e}")


# ── DISPARO DO ESTÁGIO ────────────────────────────────────────

async def disparar_estagio(config_cliente: dict, row: dict):
    account_id = row["account_id"]
    conversation_id = row["conversation_id"]
    inbox_id = row.get("inbox_id")
    stagio = row["stagio"]

    logger.info(f"[inatividade] Disparando estágio {stagio} — conv={conversation_id} account={account_id}")

    # SEGURANÇA: verificar limite ANTES de qualquer envio
    limite = _limite_inatividade(account_id)
    if stagio > limite:
        logger.warning(f"[inatividade] Estágio {stagio} > limite {limite} — desativando conv={conversation_id}")
        desativar_inatividade(account_id, conversation_id)
        return

    info = _estagio_info(stagio, account_id)
    if not info:
        logger.warning(f"[inatividade] Estágio {stagio} não encontrado no config — desativando")
        desativar_inatividade(account_id, conversation_id)
        return

    chatwoot_url = config_cliente["chatwoot_url"]
    token = config_cliente["chatwoot_token"]

    # 0. Verificar se a IA ainda está atribuída à conversa antes de disparar
    ia_agent_id = config_cliente.get("ia_agent_id")
    if not ia_agent_id:
        # Sem ia_agent_id configurado — não deve disparar follow-up
        logger.warning(f"[inatividade] Sem ia_agent_id configurado para account={account_id} — desativando conv={conversation_id}")
        desativar_inatividade(account_id, conversation_id)
        return

    try:
        url_conv = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}"
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(url_conv, headers={"api_access_token": token})
            if resp.is_success:
                conv = resp.json()
                assignee = conv.get("meta", {}).get("assignee") or conv.get("assignee")
                current_assignee_id = assignee.get("id") if isinstance(assignee, dict) else None
                if current_assignee_id != ia_agent_id:
                    logger.info(
                        f"[inatividade] IA não está mais atribuída à conv={conversation_id} "
                        f"(assignee={current_assignee_id}) — desativando monitoramento"
                    )
                    desativar_inatividade(account_id, conversation_id)
                    return
            else:
                # API retornou erro — NÃO disparar follow-up por segurança
                logger.warning(
                    f"[inatividade] API retornou status={resp.status_code} ao verificar assignee "
                    f"conv={conversation_id} — pulando disparo por segurança"
                )
                return
    except Exception as e:
        # Falha na verificação — NÃO disparar follow-up por segurança
        logger.warning(f"[inatividade] Erro ao verificar assignee conv={conversation_id}: {e} — pulando disparo por segurança")
        return

    # 1. Buscar nome do agente IA
    nome_ia = await buscar_nome_agente_ia(config_cliente)

    # 2. Buscar histórico
    try:
        historico = await _buscar_historico(chatwoot_url, token, account_id, conversation_id)
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao buscar histórico conv={conversation_id}: {e}")
        return

    historico_texto = _formatar_historico(historico, nome_ia)

    # 3. Verificar tipo de inbox e janela de 24h (WhatsApp Oficial)
    channel_type = await _get_inbox_channel_type(config_cliente, inbox_id)
    is_whatsapp_oficial = "whatsapp" in channel_type.lower()

    ultima_msg = _ultima_msg_cliente(historico)
    janela_expirada = (
        ultima_msg is not None and
        (datetime.now(timezone.utc) - ultima_msg).total_seconds() > 86400
    )

    template_name = (info.get("template_whatsapp") or "").strip()
    usar_template = is_whatsapp_oficial and janela_expirada and bool(template_name)

    logger.info(
        f"[inatividade] conv={conversation_id} | inbox={channel_type or 'desconhecido'} | "
        f"janela_expirada={janela_expirada} | usar_template={usar_template}"
    )

    # 4. Enviar mensagem
    if usar_template:
        # WhatsApp Oficial fora da janela de 24h → usa template
        try:
            await _enviar_template_chatwoot(chatwoot_url, token, account_id, conversation_id, template_name)
            logger.info(f"[inatividade] Template '{template_name}' enviado — conv={conversation_id}")
        except Exception as e:
            logger.warning(f"[inatividade] Erro ao enviar template conv={conversation_id}: {e}")
    elif is_whatsapp_oficial and janela_expirada and not template_name:
        # WhatsApp Oficial + janela expirada + sem template → pula envio
        logger.warning(
            f"[inatividade] WhatsApp Oficial fora da janela 24h sem template configurado — "
            f"envio pulado conv={conversation_id} estágio={stagio}"
        )
    else:
        # Inbox normal OU dentro da janela 24h → texto gerado pela IA
        try:
            mensagem = _gerar_mensagem(config_cliente, historico_texto, stagio, nome_ia)
        except Exception as e:
            logger.warning(f"[inatividade] Erro ao gerar mensagem conv={conversation_id}: {e}")
            mensagem = None

        if mensagem:
            try:
                await _enviar_mensagem(chatwoot_url, token, account_id, conversation_id, mensagem)
                logger.info(f"[inatividade] Mensagem estágio {stagio} enviada — conv={conversation_id}")
            except Exception as e:
                logger.warning(f"[inatividade] Erro ao enviar mensagem conv={conversation_id}: {e}")

    # 5. Kanban: mover para Follow-up no primeiro estágio de inatividade
    if stagio == 1:
        try:
            from ia import kanban_mover_card
            await kanban_mover_card(chatwoot_url, token, account_id, conversation_id, "", "followup")
            logger.info(f"[inatividade] Card kanban movido para 'Follow-up' — conv={conversation_id}")
        except Exception as e:
            logger.warning(f"[inatividade] Erro ao mover card para Follow-up conv={conversation_id}: {e}")

    # 6. Atualizar label
    try:
        await _atualizar_labels(chatwoot_url, token, account_id, conversation_id, info["label"])
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao atualizar label conv={conversation_id}: {e}")

    # 6. Avançar estágio ou desativar
    proximo_stagio = stagio + 1
    proximo_info = _estagio_info(proximo_stagio, account_id)
    atingiu_limite = stagio >= limite

    if proximo_info and not atingiu_limite:
        proximo = _proximo_disparo(proximo_info["horas"])
        try:
            upsert_inatividade(account_id, conversation_id, inbox_id, stagio=proximo_stagio, proximo_disparo=proximo)
            logger.info(f"[inatividade] Avançou para estágio {proximo_stagio} — conv={conversation_id}")
        except Exception as e:
            logger.warning(f"[inatividade] Erro ao avançar estágio conv={conversation_id}: {e}")
    else:
        # Atingiu limite de follow-ups ou acabaram os estágios — marcar como perdido
        try:
            desativar_inatividade(account_id, conversation_id)
            logger.info(f"[inatividade] Monitoramento encerrado após estágio {stagio} (limite={limite}) — conv={conversation_id}")
        except Exception as e:
            logger.warning(f"[inatividade] Erro ao desativar conv={conversation_id}: {e}")

        # Marcar lead como perdido no banco e no kanban
        await _marcar_lead_perdido(config_cliente, conversation_id, chatwoot_url, token, account_id)


# ── LOOP BACKGROUND ───────────────────────────────────────────

async def _loop_inatividade():
    logger.info("[inatividade] Monitor iniciado (intervalo: 60s)")
    _ciclo = 0
    while True:
        await asyncio.sleep(60)
        _ciclo += 1
        try:
            await processar_inatividades()
        except Exception as e:
            logger.error(f"[inatividade] Erro no loop: {e}")
        # A cada 5 minutos, verificar reativações via label
        if _ciclo % 5 == 0:
            try:
                await verificar_reativacoes()
            except Exception as e:
                logger.error(f"[inatividade] Erro ao verificar reativações: {e}")


def _dentro_horario_comercial() -> bool:
    """Retorna True se o horário atual (Brasília) estiver entre 8h e 19h."""
    tz_br = timezone(timedelta(hours=-3))
    hora = datetime.now(tz_br).hour
    return 8 <= hora < 19


async def verificar_reativacoes():
    """Verifica conversas com label 'reativar-followup' e reinicia o ciclo de inatividade."""
    from main import carregar_config_cliente
    from db import get_db

    if not _dentro_horario_comercial():
        return

    try:
        db = get_db()
        # Buscar configs ativas
        configs = db.table("ia_clientes_config").select("account_id,chatwoot_url,chatwoot_token,inatividade_ativa").eq("ativo", True).execute()
        for cfg in (configs.data or []):
            if not cfg.get("inatividade_ativa", True):
                continue
            account_id = cfg["account_id"]
            chatwoot_url = cfg.get("chatwoot_url", "").rstrip("/")
            token = cfg.get("chatwoot_token", "")
            if not chatwoot_url or not token:
                continue

            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    # Buscar conversas com label "reativar-followup"
                    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations"
                    resp = await http.get(url, headers={"api_access_token": token}, params={"labels[]": "reativar-followup", "status": "open"})
                    if not resp.is_success:
                        continue
                    convs = resp.json().get("data", {}).get("payload", [])
                    for conv in convs:
                        conv_id = conv.get("id")
                        inbox_id = conv.get("inbox_id")
                        if not conv_id:
                            continue

                        # Buscar config completa para ia_agent_id
                        config_full = carregar_config_cliente(account_id)
                        ia_agent_id = config_full.get("ia_agent_id") if config_full else None

                        # Reatribuir a IA à conversa (necessário para o follow-up funcionar)
                        if ia_agent_id:
                            try:
                                assign_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conv_id}/assignments"
                                await http.post(assign_url, headers={"api_access_token": token, "Content-Type": "application/json"}, json={"assignee_id": ia_agent_id})
                                logger.info(f"[inatividade] ♻️ IA reatribuída à conv={conv_id}")
                            except Exception as e:
                                logger.warning(f"[inatividade] Erro ao reatribuir IA conv={conv_id}: {e}")

                        # Reiniciar ciclo de inatividade
                        info = _estagio_info(1, account_id)
                        if info:
                            proximo = _proximo_disparo(info["horas"])
                            upsert_inatividade(account_id, conv_id, inbox_id, stagio=1, proximo_disparo=proximo)
                            logger.info(f"[inatividade] ♻️ Follow-up reativado via label — conv={conv_id} account={account_id}")

                        # Remover a label "reativar-followup" para não reprocessar
                        try:
                            labels_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conv_id}/labels"
                            resp_labels = await http.get(labels_url, headers={"api_access_token": token})
                            if resp_labels.is_success:
                                labels_atuais = resp_labels.json().get("payload", [])
                                novas_labels = [l for l in labels_atuais if l != "reativar-followup"]
                                await http.post(labels_url, headers={"api_access_token": token, "Content-Type": "application/json"}, json={"labels": novas_labels})
                        except Exception:
                            pass

            except Exception as e:
                logger.warning(f"[inatividade] Erro ao verificar reativações account={account_id}: {e}")
    except Exception as e:
        logger.warning(f"[inatividade] Erro geral ao verificar reativações: {e}")


async def processar_inatividades():
    from main import carregar_config_cliente

    if not _dentro_horario_comercial():
        return

    pendentes = get_inatividades_pendentes()
    if not pendentes:
        return

    logger.info(f"[inatividade] {len(pendentes)} conversa(s) para processar")

    for row in pendentes:
        account_id = row["account_id"]
        config_cliente = carregar_config_cliente(account_id)

        if not config_cliente or not config_cliente.get("ativo", True):
            logger.info(f"[inatividade] Cliente {account_id} inativo ou não encontrado — pulando")
            continue

        if not config_cliente.get("inatividade_ativa", True):
            # Desabilitar: desativar o registro pendente para não ficar no banco
            try:
                desativar_inatividade(account_id, row["conversation_id"])
            except Exception:
                pass
            logger.info(f"[inatividade] Inatividade desabilitada para account_id={account_id} — desativando conv={row['conversation_id']}")
            continue

        if not config_cliente.get("openai_api_key") or not config_cliente.get("chatwoot_token"):
            logger.warning(f"[inatividade] Config incompleta para account_id={account_id} — pulando")
            continue

        try:
            await disparar_estagio(config_cliente, row)
        except Exception as e:
            logger.error(f"[inatividade] Erro ao disparar estágio — conv={row.get('conversation_id')}: {e}")


def iniciar_monitoramento():
    asyncio.create_task(_loop_inatividade())
