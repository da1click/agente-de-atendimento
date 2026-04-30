"""
Sistema de Campanhas de Envio — envio por etiqueta ou etapa do kanban.

Campanhas enviam mensagens para conversas que possuem determinada etiqueta
ou estão em determinada etapa do kanban no Chatwoot.
Cada campanha tem:
  - tipo_filtro: "etiqueta" ou "etapa_kanban"
  - valor_filtro: nome da etiqueta ou "funnel_id:step_id"
  - limite_diario: máximo de envios por dia (distribuídos uniformemente)
  - mensagem: texto a enviar (ou template WhatsApp para canais oficiais)
"""

from datetime import datetime, timezone, timedelta
from db import (
    listar_todas_campanhas_envio_ativas,
    contar_envios_campanha_hoje,
    get_conversation_ids_ja_enviados_campanha,
    registrar_envio_campanha,
)
import asyncio
import httpx
import logging

logger = logging.getLogger(__name__)

TZ_BR = timezone(timedelta(hours=-3))
HORA_INICIO = 8
HORA_FIM = 19
TOTAL_MINUTOS_COMERCIAL = (HORA_FIM - HORA_INICIO) * 60


def _dentro_horario_comercial() -> bool:
    hora = datetime.now(TZ_BR).hour
    return HORA_INICIO <= hora < HORA_FIM


def _minutos_desde_inicio() -> int:
    agora = datetime.now(TZ_BR)
    return (agora.hour - HORA_INICIO) * 60 + agora.minute


async def _buscar_conversas_por_etiqueta(
    chatwoot_url: str, token: str, account_id: int, label: str, ja_enviados: set
) -> list:
    """Busca conversas com determinada etiqueta via Chatwoot filter API."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/filter"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    payload = {
        "payload": [
            {
                "attribute_key": "labels",
                "filter_operator": "contains",
                "values": [label],
                "query_operator": None,
            }
        ]
    }
    conversas = []
    page = 1
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            try:
                resp = await http.post(url, headers=headers, json={**payload, "page": page})
                if not resp.is_success:
                    logger.warning(f"[campanhas] Erro ao buscar etiqueta '{label}': {resp.status_code}")
                    break
                data = resp.json()
                items = data.get("payload", data.get("data", {}).get("payload", []))
                if isinstance(items, dict):
                    items = items.get("conversations", items.get("data", []))
                if not items:
                    break
                for conv in items:
                    conv_id = conv.get("id")
                    if conv_id and conv_id not in ja_enviados:
                        # Extrair telefone do contato
                        meta = conv.get("meta", {})
                        sender = meta.get("sender", {})
                        phone = sender.get("phone_number", "")
                        name = sender.get("name", "")
                        inbox_id = conv.get("inbox_id")
                        conversas.append({
                            "conversation_id": conv_id,
                            "contact_phone": phone,
                            "contact_name": name,
                            "inbox_id": inbox_id,
                        })
                # Chatwoot pagina com 25 por página
                meta_info = data.get("data", {}).get("meta", data.get("meta", {}))
                total_pages = meta_info.get("all_count", 0)
                if len(items) < 25 or page * 25 >= total_pages:
                    break
                page += 1
            except Exception as e:
                logger.warning(f"[campanhas] Erro ao buscar conversas por etiqueta: {e}")
                break
    return conversas


async def _buscar_conversas_por_etapa_kanban(
    chatwoot_url: str, token: str, account_id: int, valor_filtro: str, ja_enviados: set
) -> list:
    """Busca conversas em determinada etapa do kanban via Chatwoot funnel API.
    valor_filtro formato: "funnel_id:step_id"
    """
    try:
        funnel_id, step_id = valor_filtro.split(":")
        funnel_id = int(funnel_id)
        step_id = int(step_id)
    except (ValueError, AttributeError):
        logger.warning(f"[campanhas] valor_filtro inválido para kanban: {valor_filtro}")
        return []

    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/funnels/{funnel_id}/funnel_steps/{step_id}/funnel_items"
    headers = {"api_access_token": token}
    conversas = []
    async with httpx.AsyncClient(timeout=30) as http:
        try:
            resp = await http.get(url, headers=headers)
            if not resp.is_success:
                logger.warning(f"[campanhas] Erro ao buscar kanban {valor_filtro}: {resp.status_code}")
                return []
            data = resp.json()
            items = data.get("payload", data) if isinstance(data, dict) else data
            if isinstance(items, dict):
                items = items.get("data", [])
            for item in items:
                conv_id = item.get("conversation_id")
                if conv_id and conv_id not in ja_enviados:
                    conversas.append({
                        "conversation_id": conv_id,
                        "contact_phone": "",
                        "contact_name": item.get("title", ""),
                        "inbox_id": None,
                    })
        except Exception as e:
            logger.warning(f"[campanhas] Erro ao buscar kanban: {e}")
    return conversas


async def _obter_dados_conversa(chatwoot_url: str, token: str, account_id: int, conversation_id: int) -> dict | None:
    """Busca dados da conversa para obter telefone e inbox."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}"
    headers = {"api_access_token": token}
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            resp = await http.get(url, headers=headers)
            if resp.is_success:
                data = resp.json()
                meta = data.get("meta", {})
                sender = meta.get("sender", {})
                return {
                    "conversation_id": conversation_id,
                    "contact_phone": sender.get("phone_number", ""),
                    "contact_name": sender.get("name", ""),
                    "inbox_id": data.get("inbox_id"),
                }
        except Exception:
            pass
    return None


async def _enviar_mensagem(chatwoot_url: str, token: str, account_id: int,
                           conversation_id: int, texto: str, image_url: str = ""):
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token}
    async with httpx.AsyncClient(timeout=30) as http:
        if image_url:
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
                logger.warning(f"[campanhas] Erro imagem, tentando texto: {e}")
        headers["Content-Type"] = "application/json"
        resp = await http.post(url, headers=headers, json={
            "content": texto, "message_type": "outgoing", "private": False,
        })
        resp.raise_for_status()


async def _enviar_template(chatwoot_url: str, token: str, account_id: int,
                           conversation_id: int, template_name: str):
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
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as http:
        await http.post(url, headers=headers, json={
            "content": texto, "message_type": "outgoing", "private": True,
        })


async def _criar_conversa_inbox(chatwoot_url: str, token: str, account_id: int,
                                inbox_id: int, contact_phone: str) -> int | None:
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as http:
        search_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/contacts/search"
        resp = await http.get(search_url, headers=headers, params={"q": contact_phone})
        if not resp.is_success:
            return None
        contacts = resp.json().get("payload", [])
        if not contacts:
            return None
        contact_id = contacts[0]["id"]
        conv_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations"
        resp2 = await http.post(conv_url, headers=headers, json={
            "contact_id": contact_id, "inbox_id": inbox_id,
        })
        if resp2.is_success:
            return resp2.json().get("id")
    return None


async def _get_inbox_channel_type(chatwoot_url: str, token: str, account_id: int, inbox_id: int) -> str:
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
    except Exception:
        pass
    return ""


async def processar_campanhas():
    """Processa todas as campanhas de envio ativas."""
    from main import carregar_config_cliente

    if not _dentro_horario_comercial():
        return

    try:
        campanhas = listar_todas_campanhas_envio_ativas()
    except Exception as e:
        logger.error(f"[campanhas] Erro ao listar: {e}")
        return

    if not campanhas:
        return

    minutos = _minutos_desde_inicio()

    for campanha in campanhas:
        campanha_id = campanha["id"]
        account_id = campanha["account_id"]
        limite_diario = campanha["limite_diario"] or 200
        nome_campanha = campanha.get("nome", f"Campanha {campanha_id}")
        tipo_filtro = campanha.get("tipo_filtro", "etiqueta")
        valor_filtro = campanha.get("valor_filtro", "")
        mensagem = campanha.get("mensagem", "")
        template = (campanha.get("template_whatsapp") or "").strip()
        image_url = (campanha.get("image_url") or "").strip()
        inbox_envio_id = campanha.get("inbox_envio_id")

        if not mensagem and not template:
            continue

        if not valor_filtro:
            continue

        config_cliente = carregar_config_cliente(account_id)
        if not config_cliente or not config_cliente.get("ativo", True):
            continue
        if not config_cliente.get("chatwoot_token"):
            continue

        chatwoot_url = config_cliente["chatwoot_url"].rstrip("/")
        token = config_cliente["chatwoot_token"]

        try:
            enviados_hoje = contar_envios_campanha_hoje(campanha_id)
        except Exception:
            continue

        if enviados_hoje >= limite_diario:
            continue

        esperado = int(limite_diario * minutos / TOTAL_MINUTOS_COMERCIAL) if TOTAL_MINUTOS_COMERCIAL > 0 else 0
        if enviados_hoje >= esperado:
            continue

        # Buscar conversas já enviadas
        ja_enviados = get_conversation_ids_ja_enviados_campanha(campanha_id)

        # Buscar conversas elegíveis
        try:
            if tipo_filtro == "etiqueta":
                elegiveis = await _buscar_conversas_por_etiqueta(
                    chatwoot_url, token, account_id, valor_filtro, ja_enviados
                )
            elif tipo_filtro == "etapa_kanban":
                elegiveis = await _buscar_conversas_por_etapa_kanban(
                    chatwoot_url, token, account_id, valor_filtro, ja_enviados
                )
            else:
                continue
        except Exception as e:
            logger.warning(f"[campanhas] Erro ao buscar elegíveis campanha={campanha_id}: {e}")
            continue

        if not elegiveis:
            continue

        lead = elegiveis[0]
        conversation_id = lead["conversation_id"]
        contact_phone = lead.get("contact_phone", "")
        inbox_id_lead = lead.get("inbox_id")

        # Para kanban, pode não ter dados do contato — buscar via API
        if not contact_phone and tipo_filtro == "etapa_kanban":
            dados_conv = await _obter_dados_conversa(chatwoot_url, token, account_id, conversation_id)
            if dados_conv:
                contact_phone = dados_conv.get("contact_phone", "")
                inbox_id_lead = dados_conv.get("inbox_id") or inbox_id_lead

        envio_conv_id = conversation_id
        envio_inbox_id = inbox_envio_id or inbox_id_lead

        try:
            # Se inbox de envio é diferente, criar conversa lá
            if inbox_envio_id and inbox_envio_id != inbox_id_lead and contact_phone:
                nova_conv = await _criar_conversa_inbox(chatwoot_url, token, account_id, inbox_envio_id, contact_phone)
                if nova_conv:
                    envio_conv_id = nova_conv
                else:
                    registrar_envio_campanha(campanha_id, account_id, conversation_id, status="erro")
                    continue

            channel_type = await _get_inbox_channel_type(chatwoot_url, token, account_id, envio_inbox_id)
            is_whatsapp_oficial = "whatsapp" in channel_type.lower()

            if is_whatsapp_oficial:
                if template:
                    await _enviar_template(chatwoot_url, token, account_id, envio_conv_id, template)
                else:
                    registrar_envio_campanha(campanha_id, account_id, conversation_id, status="pulado_sem_template")
                    continue
            else:
                if mensagem or image_url:
                    await _enviar_mensagem(chatwoot_url, token, account_id, envio_conv_id, mensagem, image_url)
                else:
                    continue

            registrar_envio_campanha(campanha_id, account_id, conversation_id, status="enviado")

            # Re-resolver conversa original se enviou por inbox diferente
            if inbox_envio_id and inbox_envio_id != inbox_id_lead and envio_conv_id != conversation_id:
                try:
                    await asyncio.sleep(2)
                    resolve_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/toggle_status"
                    async with httpx.AsyncClient(timeout=10) as http:
                        await http.post(resolve_url, headers={"api_access_token": token, "Content-Type": "application/json"}, json={"status": "resolved"})
                except Exception:
                    pass

            # Nota privada
            try:
                filtro_label = f"Etiqueta: {valor_filtro}" if tipo_filtro == "etiqueta" else f"Etapa Kanban: {valor_filtro}"
                conteudo = f"Template: {template}" if (is_whatsapp_oficial and template) else (mensagem or "(sem texto)")
                if image_url and not is_whatsapp_oficial:
                    conteudo += f"\nImagem: {image_url}"
                nota = (
                    f"📢 [Campanha] Mensagem automática enviada\n"
                    f"Campanha: {nome_campanha}\n"
                    f"Filtro: {filtro_label}\n"
                    f"Conteúdo:\n{conteudo}"
                )
                await _enviar_nota_privada(chatwoot_url, token, account_id, envio_conv_id, nota)
            except Exception:
                pass

            logger.info(f"[campanhas] Enviado — campanha={campanha_id} conv={conversation_id} filtro={tipo_filtro}:{valor_filtro}")

        except Exception as e:
            logger.warning(f"[campanhas] Erro — campanha={campanha_id} conv={conversation_id}: {e}")
            try:
                registrar_envio_campanha(campanha_id, account_id, conversation_id, status="erro")
            except Exception:
                pass


async def _loop_campanhas():
    logger.info("[campanhas] Monitor de campanhas iniciado (intervalo: 60s)")
    while True:
        await asyncio.sleep(60)
        try:
            await processar_campanhas()
        except Exception as e:
            logger.error(f"[campanhas] Erro no loop: {e}")


def iniciar_campanhas():
    asyncio.create_task(_loop_campanhas())
