"""
Lembrete automático de consultas agendadas.
Roda em background a cada 2 minutos, verifica ia_agendamentos
e envia lembretes ao lead antes do horário marcado.
Suporta múltiplos lembretes por agendamento (ex: 24h, 3h, 30min antes).
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

BR_TZ = timezone(timedelta(hours=-3))

_LEMBRETES_PADRAO = [
    {"minutos": 10, "mensagem": "{nome}, só passando para lembrar que seu atendimento com {advogada} é daqui a pouco, às {horario}. Até já!"}
]

# Cache de lembretes já enviados: set de (agendamento_id, minutos_antes)
_lembretes_enviados: set[tuple[int, int]] = set()
_MAX_CACHE = 5000


def _dentro_horario_comercial() -> bool:
    hora = datetime.now(BR_TZ).hour
    return 7 <= hora < 22


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
            date_str = ag.get("scheduled_date", "")
            time_str = (ag.get("scheduled_time", "") or "")[:5]
            if not date_str or not time_str:
                continue

            try:
                ag_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                ag_dt = ag_dt.replace(tzinfo=BR_TZ)
            except ValueError:
                continue

            # Agendamento já passou
            if ag_dt < now:
                continue

            account_id = ag["account_id"]
            ag_id = ag["id"]

            if account_id not in config_cache:
                config_cache[account_id] = carregar_config_cliente(account_id)
            config = config_cache[account_id]
            if not config:
                continue

            # Config personalizada ou padrão
            cfg_lembrete = config.get("config_lembrete_consulta") or {}
            if cfg_lembrete.get("ativo") is False:
                continue

            # Suportar formato antigo (minutos_antes + mensagem) e novo (lembretes: lista)
            lembretes_config = cfg_lembrete.get("lembretes")
            if not lembretes_config:
                # Formato antigo ou padrão
                minutos = cfg_lembrete.get("minutos_antes", 10)
                msg = cfg_lembrete.get("mensagem", _LEMBRETES_PADRAO[0]["mensagem"])
                lembretes_config = [{"minutos": minutos, "mensagem": msg}]

            conversation_id = ag.get("conversation_id")
            if not conversation_id:
                continue

            nome = (ag.get("contact_name") or "").split()[0] if ag.get("contact_name") else ""
            advogada = ag.get("advogada", "")
            horario = time_str

            chatwoot_url = config["chatwoot_url"].rstrip("/")
            token = config["chatwoot_token"]

            # Buscar quais lembretes já foram enviados para este agendamento (do banco)
            lembretes_ja_enviados = set()
            try:
                from db import get_db
                _db = get_db()
                _ag_row = _db.table("ia_agendamentos").select("lembretes_enviados_min").eq("id", ag_id).maybe_single().execute()
                if _ag_row and _ag_row.data and _ag_row.data.get("lembretes_enviados_min"):
                    raw = _ag_row.data["lembretes_enviados_min"]
                    if isinstance(raw, str):
                        import json as _json
                        raw = _json.loads(raw)
                    lembretes_ja_enviados = set(raw)
            except Exception:
                pass  # coluna pode não existir ainda, usa cache em memória

            # Verificar cada lembrete configurado
            for lembrete in lembretes_config:
                minutos_antes = lembrete.get("minutos", 10)
                cache_key = (ag_id, minutos_antes)

                # Já enviou este lembrete? (cache em memória OU banco)
                if cache_key in _lembretes_enviados or minutos_antes in lembretes_ja_enviados:
                    continue

                send_time = ag_dt - timedelta(minutes=minutos_antes)

                # Janela de envio: do momento ideal até 30min depois
                if not (send_time <= now <= send_time + timedelta(minutes=30)):
                    continue

                mensagem_template = lembrete.get("mensagem", _LEMBRETES_PADRAO[0]["mensagem"])
                mensagem = (
                    mensagem_template
                    .replace("{nome}", nome)
                    .replace("{advogada}", advogada)
                    .replace("{horario}", horario)
                    .replace("{data}", date_str)
                )

                try:
                    await enviar_parte_chatwoot(chatwoot_url, token, account_id, conversation_id, mensagem)
                    _lembretes_enviados.add(cache_key)
                    lembretes_ja_enviados.add(minutos_antes)
                    enviados += 1
                    logger.info(f"[lembrete-consulta] Enviado ({minutos_antes}min antes) para {ag.get('contact_name','')} — ag {ag_id}")

                    # Salvar no banco imediatamente (persistente entre deploys)
                    try:
                        _db2 = get_db()
                        _db2.table("ia_agendamentos").update({
                            "lembretes_enviados_min": list(lembretes_ja_enviados)
                        }).eq("id", ag_id).execute()
                    except Exception:
                        pass  # coluna pode não existir

                except Exception as e:
                    logger.error(f"[lembrete-consulta] Erro ao enviar: {e}")

            # Marcar lembrete_enviado=true quando todos os lembretes configurados foram enviados
            todos_minutos = {l.get("minutos", 10) for l in lembretes_config}
            if todos_minutos.issubset(lembretes_ja_enviados) and ag.get("lembrete_enviado") is not True:
                try:
                    marcar_lembrete_enviado(ag_id)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[lembrete-consulta] Erro ao processar agendamento {ag.get('id')}: {e}")

    # Limpar cache se ficou muito grande
    if len(_lembretes_enviados) > _MAX_CACHE:
        _lembretes_enviados.clear()

    if enviados:
        logger.info(f"[lembrete-consulta] Ciclo — {enviados} lembrete(s) enviado(s)")
