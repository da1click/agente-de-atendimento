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


# ── CONFIG ────────────────────────────────────────────────────

def carregar_config_inatividade() -> dict:
    path = os.path.join(BASE_DIR, "config", "inatividade.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _estagio_info(stagio: int) -> dict | None:
    """Retorna {'horas': X, 'label': Y} para o estágio dado, ou None se não existir."""
    cfg = carregar_config_inatividade()
    for e in cfg["estagios"]:
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
    cfg = carregar_config_inatividade()
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
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
    )
    return (resp.choices[0].message.content or "").strip()


# ── DISPARO DO ESTÁGIO ────────────────────────────────────────

async def disparar_estagio(config_cliente: dict, row: dict):
    account_id = row["account_id"]
    conversation_id = row["conversation_id"]
    inbox_id = row.get("inbox_id")
    stagio = row["stagio"]

    logger.info(f"[inatividade] Disparando estágio {stagio} — conv={conversation_id} account={account_id}")

    info = _estagio_info(stagio)
    if not info:
        logger.warning(f"[inatividade] Estágio {stagio} não encontrado no config — desativando")
        desativar_inatividade(account_id, conversation_id)
        return

    chatwoot_url = config_cliente["chatwoot_url"]
    token = config_cliente["chatwoot_token"]

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

    # 5. Atualizar label
    try:
        await _atualizar_labels(chatwoot_url, token, account_id, conversation_id, info["label"])
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao atualizar label conv={conversation_id}: {e}")

    # 6. Avançar estágio ou desativar
    proximo_stagio = stagio + 1
    proximo_info = _estagio_info(proximo_stagio)

    if proximo_info:
        proximo = _proximo_disparo(proximo_info["horas"])
        try:
            upsert_inatividade(account_id, conversation_id, inbox_id, stagio=proximo_stagio, proximo_disparo=proximo)
            logger.info(f"[inatividade] Avançou para estágio {proximo_stagio} — conv={conversation_id}")
        except Exception as e:
            logger.warning(f"[inatividade] Erro ao avançar estágio conv={conversation_id}: {e}")
    else:
        # Estágio 6 foi o último — desativa
        try:
            desativar_inatividade(account_id, conversation_id)
            logger.info(f"[inatividade] Monitoramento encerrado após estágio {stagio} — conv={conversation_id}")
        except Exception as e:
            logger.warning(f"[inatividade] Erro ao desativar conv={conversation_id}: {e}")


# ── LOOP BACKGROUND ───────────────────────────────────────────

async def _loop_inatividade():
    logger.info("[inatividade] Monitor iniciado (intervalo: 60s)")
    while True:
        await asyncio.sleep(60)
        try:
            await processar_inatividades()
        except Exception as e:
            logger.error(f"[inatividade] Erro no loop: {e}")


def _dentro_horario_comercial() -> bool:
    """Retorna True se o horário atual (Brasília) estiver entre 8h e 19h."""
    tz_br = timezone(timedelta(hours=-3))
    hora = datetime.now(tz_br).hour
    return 8 <= hora < 19


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
            logger.info(f"[inatividade] Inatividade desabilitada para account_id={account_id} — pulando")
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
