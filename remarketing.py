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


async def _enviar_nota_privada(chatwoot_url: str, token: str, account_id: int, conversation_id: int, texto: str):
    """Envia nota privada (interna) na conversa do Chatwoot."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(url, headers=headers, json={
            "content": texto,
            "message_type": "outgoing",
            "private": True,
        })
        resp.raise_for_status()


async def _criar_conversa_inbox(chatwoot_url: str, token: str, account_id: int, inbox_id: int, contact_phone: str) -> int | None:
    """Cria ou busca conversa existente para o contato na inbox de envio. Retorna conversation_id."""
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as http:
        # Buscar contato pelo telefone
        search_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/contacts/search"
        resp = await http.get(search_url, headers=headers, params={"q": contact_phone})
        if not resp.is_success:
            return None
        contacts = resp.json().get("payload", [])
        if not contacts:
            return None
        contact_id = contacts[0]["id"]

        # Criar conversa na inbox de envio
        conv_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations"
        resp2 = await http.post(conv_url, headers=headers, json={
            "contact_id": contact_id,
            "inbox_id": inbox_id,
        })
        if resp2.is_success:
            return resp2.json().get("id")
        return None


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
        logger.info("[remarketing] Nenhuma campanha ativa encontrada")
        return

    logger.info(f"[remarketing] {len(campanhas)} campanha(s) ativa(s) encontrada(s)")

    minutos = _minutos_desde_inicio()

    for campanha in campanhas:
        campanha_id = campanha["id"]
        account_id = campanha["account_id"]
        dias = campanha["dias_inatividade"]
        limite_diario = campanha["limite_diario"] or 200
        nome_campanha = campanha.get("nome", f"Campanha {campanha_id}")
        mensagem = campanha.get("mensagem", "")
        template = (campanha.get("template_whatsapp") or "").strip()
        image_url = (campanha.get("image_url") or "").strip()
        campanha_inbox_id = campanha.get("inbox_id")  # inbox de origem (buscar leads)
        inbox_envio_id = campanha.get("inbox_envio_id")  # inbox de envio (pode ser diferente)

        if not mensagem and not template:
            logger.warning(f"[remarketing] Campanha {campanha_id} sem mensagem nem template — pulando")
            continue

        config_cliente = carregar_config_cliente(account_id)
        if not config_cliente or not config_cliente.get("ativo", True):
            logger.info(f"[remarketing] Campanha {campanha_id} — config_cliente ausente ou inativa para account={account_id}")
            continue
        if not config_cliente.get("chatwoot_token"):
            logger.info(f"[remarketing] Campanha {campanha_id} — sem chatwoot_token para account={account_id}")
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
            logger.info(f"[remarketing] Campanha {campanha_id} — limite diário atingido ({enviados_hoje}/{limite_diario})")
            continue

        # Distribuição uniforme: calcular quantos deveriam ter sido enviados até agora
        esperado = int(limite_diario * minutos / TOTAL_MINUTOS_COMERCIAL) if TOTAL_MINUTOS_COMERCIAL > 0 else 0
        if enviados_hoje >= esperado:
            logger.info(f"[remarketing] Campanha {campanha_id} — ritmo ok, aguardando (enviados={enviados_hoje}, esperado={esperado})")
            continue

        # Buscar 1 conversa elegível para envio
        try:
            elegiveis = buscar_conversas_elegiveis_remarketing(account_id, campanha_id, dias, 1, inbox_id=campanha_inbox_id)
        except Exception as e:
            logger.warning(f"[remarketing] Erro ao buscar elegíveis campanha={campanha_id}: {e}")
            continue

        if not elegiveis:
            logger.info(f"[remarketing] Campanha {campanha_id} — nenhuma conversa elegível (account={account_id}, dias={dias})")
            continue

        lead = elegiveis[0]
        conversation_id = lead["conversation_id"]
        inbox_id_lead = lead.get("inbox_id")
        contact_phone = lead.get("contact_phone", "")

        # Se tem inbox de envio diferente, criar conversa na inbox de envio
        envio_conv_id = conversation_id
        envio_inbox_id = inbox_envio_id or inbox_id_lead

        try:
            if inbox_envio_id and inbox_envio_id != inbox_id_lead and contact_phone:
                logger.info(f"[remarketing] Criando conversa na inbox {inbox_envio_id} para {contact_phone}")
                nova_conv = await _criar_conversa_inbox(chatwoot_url, token, account_id, inbox_envio_id, contact_phone)
                if nova_conv:
                    envio_conv_id = nova_conv
                    logger.info(f"[remarketing] Conversa criada/encontrada: conv={envio_conv_id} na inbox {inbox_envio_id}")
                else:
                    logger.warning(f"[remarketing] Não conseguiu criar conversa na inbox {inbox_envio_id} para {contact_phone} — pulando")
                    registrar_envio_remarketing(campanha_id, account_id, conversation_id, status="erro")
                    continue

            # Verificar tipo de inbox de envio para decidir texto vs template
            channel_type = await _get_inbox_channel_type(chatwoot_url, token, account_id, envio_inbox_id)
            is_whatsapp_oficial = "whatsapp" in channel_type.lower()

            if is_whatsapp_oficial:
                if template:
                    await _enviar_template_remarketing(chatwoot_url, token, account_id, envio_conv_id, template)
                    logger.info(f"[remarketing] Template '{template}' enviado — campanha={campanha_id} conv={envio_conv_id}")
                else:
                    logger.warning(f"[remarketing] WhatsApp Oficial sem template — pulando conv={conversation_id}")
                    registrar_envio_remarketing(campanha_id, account_id, conversation_id, status="pulado_sem_template")
                    continue
            else:
                if mensagem or image_url:
                    await _enviar_mensagem_remarketing(chatwoot_url, token, account_id, envio_conv_id, mensagem, image_url)
                    logger.info(f"[remarketing] Mensagem enviada — campanha={campanha_id} conv={envio_conv_id}")
                else:
                    continue

            registrar_envio_remarketing(campanha_id, account_id, conversation_id, status="enviado")

            # Se enviou por inbox diferente, re-resolver a conversa original para não reabrir
            if inbox_envio_id and inbox_envio_id != inbox_id_lead and envio_conv_id != conversation_id:
                try:
                    await asyncio.sleep(2)  # aguardar Chatwoot processar
                    resolve_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/toggle_status"
                    async with httpx.AsyncClient(timeout=10) as http:
                        await http.post(resolve_url, headers={"api_access_token": token, "Content-Type": "application/json"}, json={"status": "resolved"})
                    logger.info(f"[remarketing] Conversa original {conversation_id} (inbox {inbox_id_lead}) re-resolvida")
                except Exception as e_resolve:
                    logger.warning(f"[remarketing] Erro ao re-resolver conv original {conversation_id}: {e_resolve}")

            # Nota privada para visibilidade interna
            try:
                conteudo_enviado = f"Template: {template}" if (is_whatsapp_oficial and template) else (mensagem or "(sem texto)")
                if image_url and not is_whatsapp_oficial:
                    conteudo_enviado += f"\nImagem: {image_url}"
                inbox_info = f"\nInbox origem: #{inbox_id_lead} → Inbox envio: #{envio_inbox_id}" if inbox_envio_id and inbox_envio_id != inbox_id_lead else ""
                nota = (
                    f"📢 [Remarketing] Mensagem automática enviada\n"
                    f"Campanha: {nome_campanha}\n"
                    f"Critério: {dias} dias de inatividade{inbox_info}\n"
                    f"Conteúdo enviado:\n{conteudo_enviado}"
                )
                await _enviar_nota_privada(chatwoot_url, token, account_id, envio_conv_id, nota)
                logger.info(f"[remarketing] Nota privada enviada — conv={envio_conv_id}")
            except Exception as e_nota:
                logger.warning(f"[remarketing] Erro ao enviar nota privada conv={envio_conv_id}: {e_nota}")

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
