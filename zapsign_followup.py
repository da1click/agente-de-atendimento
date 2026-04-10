"""
Follow-up automático ZapSign — envia lembretes quando contrato não foi assinado.
Segue o mesmo padrão de inatividade.py: loop 60s + estágios configuráveis.
"""

from datetime import datetime, timezone, timedelta
from db import (
    get_zapsign_followups_pendentes,
    upsert_zapsign_followup,
    avancar_zapsign_followup,
    desativar_zapsign_followup_conversa,
    get_zapsign_config,
)
import asyncio
import httpx
import json
import logging

logger = logging.getLogger(__name__)

TZ_BR = timezone(timedelta(hours=-3))
LIMITE_ESTAGIOS = 3


def _dentro_horario_comercial() -> bool:
    hora = datetime.now(TZ_BR).hour
    return 8 <= hora < 19


def _proximo_disparo(horas: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=horas)
    return dt.isoformat()


def _get_estagio_info(estagios: list, stagio: int) -> dict | None:
    for e in estagios:
        if e.get("stagio") == stagio:
            return e
    return None


def _parse_estagios(estagios) -> list:
    if isinstance(estagios, str):
        try:
            return json.loads(estagios)
        except Exception:
            return []
    return estagios if isinstance(estagios, list) else []


async def _enviar_mensagem(chatwoot_url: str, token: str, account_id: int,
                           conversation_id: int, texto: str):
    """Envia mensagem via Chatwoot API (mesmo padrão de inatividade.py)."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(url, headers=headers, json={
            "content": texto,
            "message_type": "outgoing",
            "private": False,
        })
        resp.raise_for_status()


async def _verificar_assignee_ia(config_cliente: dict, account_id: int,
                                  conversation_id: int) -> bool:
    """Verifica se a IA ainda está atribuída à conversa (segurança)."""
    ia_agent_id = config_cliente.get("ia_agent_id")
    if not ia_agent_id:
        return False

    chatwoot_url = config_cliente["chatwoot_url"]
    token = config_cliente["chatwoot_token"]

    try:
        url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}"
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(url, headers={"api_access_token": token})
            if resp.is_success:
                conv = resp.json()
                assignee = conv.get("meta", {}).get("assignee") or conv.get("assignee")
                current_id = assignee.get("id") if isinstance(assignee, dict) else None
                return current_id == ia_agent_id
    except Exception as e:
        logger.warning(f"[zapsign-followup] Erro ao verificar assignee conv={conversation_id}: {e}")
    return False


async def disparar_followup(config_cliente: dict, zapsign_cfg: dict, row: dict):
    account_id = row["account_id"]
    conversation_id = row["conversation_id"]
    doc_token = row["doc_token"]
    stagio = row["stagio"]

    logger.info(f"[zapsign-followup] Disparando estágio {stagio} — conv={conversation_id} doc={doc_token}")

    # Limite de segurança
    if stagio > LIMITE_ESTAGIOS:
        logger.warning(f"[zapsign-followup] Estágio {stagio} > limite — desativando")
        desativar_zapsign_followup_conversa(account_id, conversation_id)
        return

    estagios = _parse_estagios(zapsign_cfg.get("followup_estagios", []))
    info = _get_estagio_info(estagios, stagio)
    if not info:
        logger.warning(f"[zapsign-followup] Estágio {stagio} não encontrado no config — desativando")
        desativar_zapsign_followup_conversa(account_id, conversation_id)
        return

    # Verificar se o documento já foi assinado via API ZapSign antes de enviar
    api_token = zapsign_cfg.get("api_token", "")
    doc_tokens_list = row.get("doc_tokens") or [doc_token]
    if isinstance(doc_tokens_list, str):
        import json as _json
        doc_tokens_list = _json.loads(doc_tokens_list)

    if api_token and doc_tokens_list:
        todos_assinados = True
        for dt in doc_tokens_list:
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    r = await http.get(
                        f"https://api.zapsign.com.br/api/v1/docs/{dt}/",
                        headers={"Authorization": f"Bearer {api_token}"}
                    )
                    if r.is_success:
                        status_doc = r.json().get("status", "")
                        if status_doc in ("signed", "closed"):
                            logger.info(f"[zapsign-followup] Doc {dt[:12]}... já assinado — pulando")
                        else:
                            todos_assinados = False
            except Exception:
                todos_assinados = False  # na dúvida, não desativa

        if todos_assinados and doc_tokens_list:
            logger.info(f"[zapsign-followup] Todos docs já assinados — desativando conv={conversation_id}")
            desativar_zapsign_followup_conversa(account_id, conversation_id)
            return

    chatwoot_url = config_cliente["chatwoot_url"]
    token = config_cliente["chatwoot_token"]
    mensagem = info.get("mensagem", "")

    if not mensagem:
        logger.warning(f"[zapsign-followup] Sem mensagem para estágio {stagio} — desativando")
        desativar_zapsign_followup_conversa(account_id, conversation_id)
        return

    # Enviar mensagem
    try:
        await _enviar_mensagem(chatwoot_url, token, account_id, conversation_id, mensagem)
        logger.info(f"[zapsign-followup] Estágio {stagio} enviado — conv={conversation_id}")
    except Exception as e:
        logger.warning(f"[zapsign-followup] Erro ao enviar msg conv={conversation_id}: {e}")
        return

    # Avançar para próximo estágio ou desativar
    proximo_stagio = stagio + 1
    prox_info = _get_estagio_info(estagios, proximo_stagio)
    if prox_info:
        proximo = _proximo_disparo(prox_info["horas"])
        try:
            avancar_zapsign_followup(account_id, conversation_id, proximo_stagio, proximo)
            logger.info(f"[zapsign-followup] Avançado para estágio {proximo_stagio} — conv={conversation_id} próximo={proximo}")
        except Exception as e:
            logger.error(f"[zapsign-followup] ERRO ao avançar estágio — desativando conv={conversation_id}: {e}")
            desativar_zapsign_followup_conversa(account_id, conversation_id)
    else:
        desativar_zapsign_followup_conversa(account_id, conversation_id)
        logger.info(f"[zapsign-followup] Todos estágios esgotados — conv={conversation_id}")


async def processar_followups():
    from db import carregar_config_cliente

    if not _dentro_horario_comercial():
        return

    pendentes = get_zapsign_followups_pendentes()
    if not pendentes:
        return

    logger.info(f"[zapsign-followup] {len(pendentes)} follow-up(s) pendentes")

    for row in pendentes:
        account_id = row["account_id"]
        config_cliente = carregar_config_cliente(account_id)

        if not config_cliente or not config_cliente.get("ativo", True):
            continue

        if not config_cliente.get("chatwoot_token"):
            logger.warning(f"[zapsign-followup] Sem chatwoot_token para account={account_id}")
            continue

        zapsign_cfg = get_zapsign_config(account_id)
        if not zapsign_cfg or not zapsign_cfg.get("followup_ativo", False):
            desativar_zapsign_followup_conversa(account_id, row["conversation_id"])
            continue

        try:
            await disparar_followup(config_cliente, zapsign_cfg, row)
        except Exception as e:
            logger.error(f"[zapsign-followup] Erro — conv={row.get('conversation_id')}: {e}")


async def _loop_zapsign_followup():
    logger.info("[zapsign-followup] Monitor iniciado (intervalo: 60s)")
    while True:
        await asyncio.sleep(60)
        try:
            await processar_followups()
        except Exception as e:
            logger.error(f"[zapsign-followup] Erro no loop: {e}")


def iniciar_zapsign_followup():
    asyncio.create_task(_loop_zapsign_followup())
