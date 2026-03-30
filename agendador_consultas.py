"""
Lembrete automático de consultas agendadas.
Roda em background a cada 2 minutos, verifica ia_agendamentos
e envia lembrete ao lead antes do horário marcado.
Funciona para todas as contas por padrão (10 min antes).
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

BR_TZ = timezone(timedelta(hours=-3))

_MINUTOS_ANTES_PADRAO = 10
_MENSAGEM_PADRAO = "{nome}, só passando para lembrar que seu atendimento com {advogada} é daqui a pouco, às {horario}. Até já!"


def _dentro_horario_comercial() -> bool:
    hora = datetime.now(BR_TZ).hour
    return 7 <= hora < 20


async def _loop():
    logger.info("[lembrete-consulta] Monitor iniciado (intervalo: 120s)")
    while True:
        await asyncio.sleep(120)
        try:
            await processar_lembretes()
        except Exception as e:
            logger.error(f"[lembrete-consulta] Erro no loop: {e}")


def iniciar_agendador_consultas():
    asyncio.create_task(_loop())


async def processar_lembretes():
    if not _dentro_horario_comercial():
        return

    from db import listar_agendamentos_pendentes, marcar_lembrete_enviado, carregar_config_cliente
    from ia import enviar_parte_chatwoot

    agendamentos = listar_agendamentos_pendentes()
    if not agendamentos:
        return

    now = datetime.now(BR_TZ)
    config_cache: dict[int, dict | None] = {}
    enviados = 0

    for ag in agendamentos:
        try:
            if ag.get("lembrete_enviado"):
                continue

            date_str = ag.get("scheduled_date", "")
            time_str = (ag.get("scheduled_time", "") or "")[:5]
            if not date_str or not time_str:
                continue

            try:
                ag_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                ag_dt = ag_dt.replace(tzinfo=BR_TZ)
            except ValueError:
                continue

            if ag_dt < now:
                continue

            account_id = ag["account_id"]
            if account_id not in config_cache:
                config_cache[account_id] = carregar_config_cliente(account_id)
            config = config_cache[account_id]
            if not config:
                continue

            # Config personalizada ou padrão
            cfg_lembrete = config.get("config_lembrete_consulta") or {}
            # Se a conta desativou explicitamente, pular
            if cfg_lembrete.get("ativo") is False:
                continue

            minutos_antes = cfg_lembrete.get("minutos_antes", _MINUTOS_ANTES_PADRAO)
            send_time = ag_dt - timedelta(minutes=minutos_antes)

            if not (send_time <= now <= send_time + timedelta(minutes=30)):
                continue

            conversation_id = ag.get("conversation_id")
            if not conversation_id:
                continue

            nome = (ag.get("contact_name") or "").split()[0] if ag.get("contact_name") else ""
            advogada = ag.get("advogada", "")
            horario = time_str

            mensagem_template = cfg_lembrete.get("mensagem", _MENSAGEM_PADRAO)
            mensagem = (
                mensagem_template
                .replace("{nome}", nome)
                .replace("{advogada}", advogada)
                .replace("{horario}", horario)
                .replace("{data}", date_str)
            )

            chatwoot_url = config["chatwoot_url"].rstrip("/")
            token = config["chatwoot_token"]

            try:
                await enviar_parte_chatwoot(chatwoot_url, token, account_id, conversation_id, mensagem)
                marcar_lembrete_enviado(ag["id"])
                enviados += 1
                logger.info(f"[lembrete-consulta] Enviado para {ag.get('contact_name','')} — agendamento {ag['id']}")
            except Exception as e:
                logger.error(f"[lembrete-consulta] Erro ao enviar: {e}")

        except Exception as e:
            logger.error(f"[lembrete-consulta] Erro ao processar agendamento {ag.get('id')}: {e}")

    if enviados:
        logger.info(f"[lembrete-consulta] Ciclo — {enviados} lembrete(s) enviado(s)")
