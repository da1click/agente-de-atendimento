"""
Agendador automático de avisos de audiência.
Roda em background a cada 5 minutos, verifica quais mensagens devem ser enviadas
com base no tempo_antes configurado, e dispara o envio automaticamente.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

BR_TZ = timezone(timedelta(hours=-3))


def _dentro_horario_comercial() -> bool:
    hora = datetime.now(BR_TZ).hour
    return 8 <= hora < 19


async def _loop_agendador():
    logger.info("[agendador-aud] Monitor iniciado (intervalo: 300s)")
    while True:
        await asyncio.sleep(300)  # 5 minutos
        try:
            await processar_audiencias_pendentes()
        except Exception as e:
            logger.error(f"[agendador-aud] Erro no loop: {e}")


def iniciar_agendador_audiencias():
    asyncio.create_task(_loop_agendador())


async def processar_audiencias_pendentes():
    if not _dentro_horario_comercial():
        return

    from db import listar_audiencias_db, listar_hearing_types_db
    from main import _enviar_aviso_audiencia_core

    audiencias = listar_audiencias_db()
    if not audiencias:
        return

    now = datetime.now(BR_TZ)
    tipos_cache = {}
    enviados_total = 0

    for aud in audiencias:
        try:
            # Pular audiências sem data/horário/tipo ou não pendentes
            if aud.get("status") != "pendente":
                continue
            data_str = aud.get("data", "")
            horario_str = aud.get("horario", "")
            tipo_nome = aud.get("tipo_audiencia", "")
            if not data_str or not horario_str or not tipo_nome:
                continue

            # Montar datetime da audiência em horário de Brasília
            try:
                aud_dt = datetime.strptime(f"{data_str} {horario_str}", "%Y-%m-%d %H:%M")
                aud_dt = aud_dt.replace(tzinfo=BR_TZ)
            except ValueError:
                continue

            # Pular audiências que já passaram
            if aud_dt < now:
                continue

            # Buscar tipo de audiência (com cache)
            account_id = aud.get("account_id")
            cache_key = f"{account_id}:{tipo_nome}"
            if cache_key not in tipos_cache:
                tipos = listar_hearing_types_db(account_id)
                tipo = next((t for t in tipos if t["nome"] == tipo_nome), None)
                tipos_cache[cache_key] = tipo
            tipo = tipos_cache[cache_key]

            if not tipo:
                continue

            mensagens = tipo.get("mensagens", [])
            if not mensagens:
                continue

            # Mensagens já enviadas para esta audiência
            enviadas_ids = set()
            for env in aud.get("mensagens_enviadas") or []:
                if env.get("mensagem_id"):
                    enviadas_ids.add(env["mensagem_id"])

            # Verificar cada mensagem
            for msg in mensagens:
                msg_id = msg.get("id")
                if not msg_id or msg_id in enviadas_ids:
                    continue

                tempo = msg.get("tempo_antes", 0)
                unidade = msg.get("unidade_tempo", "dias")

                if unidade == "minutos":
                    send_time = aud_dt - timedelta(minutes=tempo)
                elif unidade == "horas":
                    send_time = aud_dt - timedelta(hours=tempo)
                else:
                    send_time = aud_dt - timedelta(days=tempo)

                # Enviar se: já passou do horário de envio E
                # não passou mais de 24h do horário de envio (evita envios muito atrasados)
                if send_time <= now <= send_time + timedelta(hours=24):
                    logger.info(
                        f"[agendador-aud] Enviando msg {msg_id} para {aud['nome_cliente']} "
                        f"(audiência {aud['id']}, {tempo} {unidade} antes)"
                    )
                    try:
                        result = await _enviar_aviso_audiencia_core(aud["id"], msg_id)
                        if result.get("status") == "ok":
                            enviados_total += 1
                            logger.info(f"[agendador-aud] Enviado com sucesso: {aud['nome_cliente']} msg={msg_id}")
                        else:
                            logger.warning(f"[agendador-aud] Falha: {result.get('error', result)}")
                    except Exception as e:
                        logger.error(f"[agendador-aud] Erro ao enviar msg {msg_id}: {e}")

        except Exception as e:
            logger.error(f"[agendador-aud] Erro ao processar audiência {aud.get('id')}: {e}")

    if enviados_total > 0:
        logger.info(f"[agendador-aud] Ciclo concluído — {enviados_total} mensagem(ns) enviada(s)")
