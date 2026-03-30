"""
Sistema de Remarketing — separado da inatividade (follow-up).

Campanhas de remarketing enviam mensagens para leads inativos há X dias.
Cada campanha tem:
  - dias_inatividade: quantos dias sem atividade para ser elegível
  - limite_diario: máximo de envios por dia (distribuídos uniformemente)
  - mensagem: texto a enviar (ou template WhatsApp para canais oficiais)
"""

from datetime import datetime, timezone, timedelta
from db import (
    listar_todas_campanhas_ativas,
    contar_envios_remarketing_hoje,
    buscar_conversas_elegiveis_remarketing,
    registrar_envio_remarketing,
)
import asyncio
import httpx
import logging

logger = logging.getLogger(__name__)

TZ_BR = timezone(timedelta(hours=-3))
HORA_INICIO = 8
HORA_FIM = 19
TOTAL_MINUTOS_COMERCIAL = (HORA_FIM - HORA_INICIO) * 60  # 660 min


def _dentro_horario_comercial() -> bool:
    hora = datetime.now(TZ_BR).hour
    return HORA_INICIO <= hora < HORA_FIM


def _minutos_desde_inicio() -> int:
    """Retorna minutos transcorridos desde o início do horário comercial."""
    agora = datetime.now(TZ_BR)
    return (agora.hour - HORA_INICIO) * 60 + agora.minute


async def _enviar_mensagem_remarketing(
    chatwoot_url: str, token: str, account_id: int,
    conversation_id: int, texto: str, image_url: str = ""
):
    """Envia mensagem de remarketing via Chatwoot, com imagem opcional."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token}
    async with httpx.AsyncClient(timeout=30) as http:
        if image_url:
            # Baixar imagem e enviar como multipart
            try:
                img_resp = await http.get(image_url, timeout=15)
                img_resp.raise_for_status()
                content_type = img_resp.headers.get("content-type", "image/jpeg")
                ext = content_type.split("/")[-1].split(";")[0]
                files = {"attachments[]": (f"image.{ext}", img_resp.content, content_type)}
                data = {"content": texto or "", "message_type": "outgoing", "private": "false"}
                resp = await http.post(url, headers=headers, data=data, files=files)
                resp.raise_for_status()
                return
            except Exception as e:
                logger.warning(f"[remarketing] Erro ao enviar imagem, tentando só texto: {e}")
        # Fallback: só texto
        headers["Content-Type"] = "application/json"
        resp = await http.post(url, headers=headers, json={
            "content": texto,
            "message_type": "outgoing",
            "private": False,
        })
        resp.raise_for_status()


async def _enviar_template_remarketing(
    chatwoot_url: str, token: str, account_id: int,
    conversation_id: int, template_name: str
):
    """Envia template WhatsApp via Chatwoot para remarketing."""
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


async def _get_inbox_channel_type(chatwoot_url: str, token: str, account_id: int, inbox_id: int) -> str:
    """Retorna channel_type do inbox."""
    if not inbox_id:
        return ""
    try:
        url = f"{chatwoot_url}/api/v1/accounts/{account_id}/inboxes"
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(url, headers={"api_access_token": token})
            if resp.is_success:
                for inbox in resp.json().get("payload", []):
                    if inbox.get("id") == inbox_id:
                        return inbox.get("channel_type", "")
    except Exception as e:
        logger.warning(f"[remarketing] Erro ao buscar channel_type inbox={inbox_id}: {e}")
    return ""


async def processar_remarketing():
    """Processa todas as campanhas ativas de remarketing."""
    from main import carregar_config_cliente

    if not _dentro_horario_comercial():
        return

    try:
        campanhas = listar_todas_campanhas_ativas()
    except Exception as e:
        logger.error(f"[remarketing] Erro ao listar campanhas: {e}")
        return

    if not campanhas:
        return

    minutos = _minutos_desde_inicio()

    for campanha in campanhas:
        campanha_id = campanha["id"]
        account_id = campanha["account_id"]
        dias = campanha["dias_inatividade"]
        limite_diario = campanha["limite_diario"] or 200
        mensagem = campanha.get("mensagem", "")
        template = (campanha.get("template_whatsapp") or "").strip()
        image_url = (campanha.get("image_url") or "").strip()

        if not mensagem and not template:
            logger.warning(f"[remarketing] Campanha {campanha_id} sem mensagem nem template — pulando")
            continue

        config_cliente = carregar_config_cliente(account_id)
        if not config_cliente or not config_cliente.get("ativo", True):
            continue
        if not config_cliente.get("chatwoot_token"):
            continue

        chatwoot_url = config_cliente["chatwoot_url"]
        token = config_cliente["chatwoot_token"]

        # Contar envios de hoje
        try:
            enviados_hoje = contar_envios_remarketing_hoje(campanha_id)
        except Exception as e:
            logger.warning(f"[remarketing] Erro ao contar envios campanha={campanha_id}: {e}")
            continue

        if enviados_hoje >= limite_diario:
            continue

        # Distribuição uniforme: calcular quantos deveriam ter sido enviados até agora
        esperado = int(limite_diario * minutos / TOTAL_MINUTOS_COMERCIAL) if TOTAL_MINUTOS_COMERCIAL > 0 else 0
        if enviados_hoje >= esperado:
            continue  # já está no ritmo certo, esperar

        # Buscar 1 conversa elegível para envio
        try:
            elegiveis = buscar_conversas_elegiveis_remarketing(account_id, campanha_id, dias, 1)
        except Exception as e:
            logger.warning(f"[remarketing] Erro ao buscar elegíveis campanha={campanha_id}: {e}")
            continue

        if not elegiveis:
            continue

        lead = elegiveis[0]
        conversation_id = lead["conversation_id"]
        inbox_id = lead.get("inbox_id")

        # Verificar tipo de inbox para decidir texto vs template
        try:
            channel_type = await _get_inbox_channel_type(chatwoot_url, token, account_id, inbox_id)
            is_whatsapp_oficial = "whatsapp" in channel_type.lower()

            if is_whatsapp_oficial:
                # WhatsApp Oficial: remarketing sempre fora da janela 24h → precisa template
                if template:
                    await _enviar_template_remarketing(chatwoot_url, token, account_id, conversation_id, template)
                    logger.info(f"[remarketing] Template '{template}' enviado — campanha={campanha_id} conv={conversation_id}")
                else:
                    # Sem template + WhatsApp Oficial = não pode enviar
                    logger.warning(f"[remarketing] WhatsApp Oficial sem template — pulando conv={conversation_id}")
                    registrar_envio_remarketing(campanha_id, account_id, conversation_id, status="pulado_sem_template")
                    continue
            else:
                # Inbox normal: enviar texto direto
                if mensagem or image_url:
                    await _enviar_mensagem_remarketing(chatwoot_url, token, account_id, conversation_id, mensagem, image_url)
                    logger.info(f"[remarketing] Mensagem enviada — campanha={campanha_id} conv={conversation_id}")
                else:
                    continue

            registrar_envio_remarketing(campanha_id, account_id, conversation_id, status="enviado")

        except Exception as e:
            logger.warning(f"[remarketing] Erro ao enviar — campanha={campanha_id} conv={conversation_id}: {e}")
            try:
                registrar_envio_remarketing(campanha_id, account_id, conversation_id, status="erro")
            except Exception:
                pass


async def _loop_remarketing():
    """Loop background do remarketing — roda a cada 60 segundos."""
    logger.info("[remarketing] Monitor de remarketing iniciado (intervalo: 60s)")
    while True:
        await asyncio.sleep(60)
        try:
            await processar_remarketing()
        except Exception as e:
            logger.error(f"[remarketing] Erro no loop: {e}")


def iniciar_remarketing():
    """Inicia o loop de remarketing como task background."""
    asyncio.create_task(_loop_remarketing())
