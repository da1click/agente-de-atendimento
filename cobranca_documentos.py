"""Cobrança automatizada de documentos pendentes.

Fluxo:
1. Humano adiciona a label `cobrar-documentos` na conversa do Chatwoot.
2. Um loop background varre as contas habilitadas a cada 5 minutos e,
   para cada conversa com essa label, garante um registro ativo em
   `ia_cobranca_docs` com `proximo_envio` agendado.
3. Quando `proximo_envio` chega, envia uma mensagem de cobrança
   (alternando entre 3 variantes), incrementa `tentativas` e agenda o
   próximo envio em COBRANCA_INTERVALO_HORAS.
4. Para quando:
   - humano remove a label manualmente;
   - cliente envia anexo (detectado no webhook → chama
     `desativar_cobranca_docs(motivo='anexo_recebido')`);
   - tentativas >= limite (padrão 5).

Todas as operações só disparam dentro do horário comercial (8–19h BRT)
e apenas para contas listadas em `CONTAS_HABILITADAS`.
"""

from __future__ import annotations

import asyncio
import httpx
import logging
from datetime import datetime, timedelta, timezone

from db import (
    ativar_cobranca_docs,
    desativar_cobranca_docs,
    listar_cobrancas_docs_pendentes,
    registrar_envio_cobranca_docs,
    get_cobranca_docs,
)

logger = logging.getLogger(__name__)

BR_TZ = timezone(timedelta(hours=-3))
LABEL_COBRANCA = "cobrar-documentos"

# Contas onde o fluxo está habilitado. Por enquanto, apenas conta 17.
CONTAS_HABILITADAS: set[int] = {17}

# Intervalo entre cobranças e limite padrão
COBRANCA_INTERVALO_HORAS = 12
COBRANCA_LIMITE_PADRAO = 5
LOOP_INTERVALO_SEGUNDOS = 300  # 5 min

# 3 mensagens variantes — alternadas conforme o número da tentativa
_MENSAGENS_VARIANTES: list[str] = [
    (
        "Oi, {nome}! Passando pra reforçar que precisamos da sua "
        "Carteira de Trabalho Digital e do Extrato do FGTS em PDF pra "
        "avançar com a análise do seu caso. Consegue me mandar aqui?"
    ),
    (
        "Oi, tudo bem? Ainda estou aguardando sua Carteira de Trabalho "
        "Digital e o Extrato do FGTS em PDF. Assim que receber, já "
        "encaminho pro advogado conferir. Consegue enviar hoje?"
    ),
    (
        "{nome}, tudo certo por aí? Sem a Carteira de Trabalho Digital "
        "e o Extrato do FGTS a análise não avança. Me manda aqui quando "
        "puder, tá?"
    ),
]


def _dentro_horario_comercial() -> bool:
    hora = datetime.now(BR_TZ).hour
    return 8 <= hora < 19


def _proximo_envio_iso(horas: int = COBRANCA_INTERVALO_HORAS) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=horas)).isoformat()


async def _listar_conversas_com_label(config: dict, label: str) -> list[dict]:
    base = (config.get("chatwoot_url") or "").rstrip("/")
    token = config.get("chatwoot_token", "")
    account_id = config["account_id"]
    if not base or not token:
        return []
    convs: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            for page in range(1, 6):
                resp = await http.get(
                    f"{base}/api/v1/accounts/{account_id}/conversations",
                    headers={"api_access_token": token},
                    params={"labels[]": label, "status": "open", "page": page},
                )
                if not resp.is_success:
                    break
                page_convs = resp.json().get("data", {}).get("payload", []) or []
                convs.extend(page_convs)
                if len(page_convs) < 25:
                    break
    except Exception as e:
        logger.warning(f"[cobranca-docs] Erro listando conversas com label: {e}")
    return convs


async def _sincronizar_cobrancas():
    """Garante que cada conversa com a label tenha registro ativo, e desativa
    registros cujas conversas perderam a label."""
    from main import carregar_config_cliente

    for account_id in CONTAS_HABILITADAS:
        config = carregar_config_cliente(account_id)
        if not config or not config.get("ativo", True):
            continue
        convs = await _listar_conversas_com_label(config, LABEL_COBRANCA)
        ids_ativos = set()
        for conv in convs:
            conv_id = conv.get("id")
            if not conv_id:
                continue
            ids_ativos.add(conv_id)
            existente = get_cobranca_docs(account_id, conv_id)
            if existente and existente.get("ativo"):
                continue  # já está rodando
            # Cria (ou reativa) cobrança — primeira cobrança é quase imediata
            contact = (conv.get("meta") or {}).get("sender") or {}
            nome = contact.get("name", "") or ""
            phone = contact.get("phone_number", "") or ""
            inbox_id = conv.get("inbox_id")
            # Primeiro envio em 1 min — para dar tempo do humano confirmar que quis acionar
            primeiro = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
            ativar_cobranca_docs(
                account_id=account_id,
                conversation_id=conv_id,
                inbox_id=inbox_id,
                contact_name=nome,
                contact_phone=phone,
                proximo_envio=primeiro,
                limite=COBRANCA_LIMITE_PADRAO,
            )
            logger.info(f"[cobranca-docs] Ativada para conv={conv_id} account={account_id}")


async def _desativar_sem_label():
    """Desativa cobranças cujas conversas não têm mais a label (humano removeu)."""
    from main import carregar_config_cliente
    from db import get_db

    for account_id in CONTAS_HABILITADAS:
        config = carregar_config_cliente(account_id)
        if not config:
            continue
        try:
            db = get_db()
            resp = (
                db.table("ia_cobranca_docs")
                .select("conversation_id")
                .eq("account_id", account_id)
                .eq("ativo", True)
                .execute()
            )
            ativos_db = {r["conversation_id"] for r in (resp.data or [])}
        except Exception:
            continue
        if not ativos_db:
            continue
        convs = await _listar_conversas_com_label(config, LABEL_COBRANCA)
        com_label = {c.get("id") for c in convs if c.get("id")}
        for conv_id in ativos_db - com_label:
            desativar_cobranca_docs(account_id, conv_id, motivo="label_removida")
            logger.info(f"[cobranca-docs] Desativada (label removida) conv={conv_id} account={account_id}")


async def _disparar_cobrancas():
    """Envia cobranças pendentes."""
    from main import carregar_config_cliente
    from ia import enviar_parte_chatwoot

    pendentes = listar_cobrancas_docs_pendentes()
    if not pendentes:
        return

    for row in pendentes:
        account_id = row["account_id"]
        if account_id not in CONTAS_HABILITADAS:
            continue
        config = carregar_config_cliente(account_id)
        if not config or not config.get("ativo", True):
            continue
        conv_id = row["conversation_id"]
        tentativas = int(row.get("tentativas") or 0)
        limite = int(row.get("limite") or COBRANCA_LIMITE_PADRAO)
        if tentativas >= limite:
            desativar_cobranca_docs(account_id, conv_id, motivo="limite_atingido")
            logger.info(f"[cobranca-docs] Limite atingido — conv={conv_id} account={account_id}")
            continue
        nome = (row.get("contact_name") or "").split()[0] if row.get("contact_name") else ""
        template = _MENSAGENS_VARIANTES[tentativas % len(_MENSAGENS_VARIANTES)]
        mensagem = template.replace("{nome}", nome or "").replace("  ", " ").strip()
        if mensagem.startswith(", "):
            mensagem = mensagem[2:]

        chatwoot_url = (config.get("chatwoot_url") or "").rstrip("/")
        token = config.get("chatwoot_token", "")
        try:
            await enviar_parte_chatwoot(chatwoot_url, token, account_id, conv_id, mensagem)
        except Exception as e:
            logger.warning(f"[cobranca-docs] Erro ao enviar cobrança conv={conv_id}: {e}")
            continue

        nova_tentativa = tentativas + 1
        if nova_tentativa >= limite:
            registrar_envio_cobranca_docs(row["id"], proximo_envio=None, tentativas=nova_tentativa)
            desativar_cobranca_docs(account_id, conv_id, motivo="limite_atingido")
            logger.info(f"[cobranca-docs] Enviada ({nova_tentativa}/{limite}) e desativada — conv={conv_id}")
        else:
            registrar_envio_cobranca_docs(
                row["id"],
                proximo_envio=_proximo_envio_iso(COBRANCA_INTERVALO_HORAS),
                tentativas=nova_tentativa,
            )
            logger.info(f"[cobranca-docs] Enviada ({nova_tentativa}/{limite}) — conv={conv_id}")


async def _loop():
    logger.info(
        f"[cobranca-docs] Monitor iniciado (intervalo: {LOOP_INTERVALO_SEGUNDOS}s, "
        f"contas: {CONTAS_HABILITADAS})"
    )
    while True:
        await asyncio.sleep(LOOP_INTERVALO_SEGUNDOS)
        if not _dentro_horario_comercial():
            continue
        try:
            await _sincronizar_cobrancas()
        except Exception as e:
            logger.error(f"[cobranca-docs] Erro em sincronizar: {e}")
        try:
            await _desativar_sem_label()
        except Exception as e:
            logger.error(f"[cobranca-docs] Erro em desativar_sem_label: {e}")
        try:
            await _disparar_cobrancas()
        except Exception as e:
            logger.error(f"[cobranca-docs] Erro em disparar: {e}")


def iniciar_monitoramento():
    asyncio.create_task(_loop())
