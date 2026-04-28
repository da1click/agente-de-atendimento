"""
Lembrete automático de consultas agendadas.
Roda em background a cada 2 minutos, verifica ia_agendamentos
e envia lembretes ao lead antes do horário marcado.
Suporta múltiplos lembretes por agendamento (ex: 24h, 3h, 30min antes).
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

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


def _formatar_data_lembrete(date_str: str) -> str:
    """Converte 'YYYY-MM-DD' em 'DD/MM/YYYY'."""
    if not date_str or "-" not in date_str:
        return date_str or ""
    try:
        partes = date_str.split("-")
        return f"{partes[2]}/{partes[1]}/{partes[0]}"
    except (IndexError, ValueError):
        return date_str


def _build_processed_params_lembrete(template_vars: dict, ctx: dict) -> dict:
    """Mapeia placeholders ([NOME], [HORARIO], etc.) para processed_params do template.

    template_vars ex: {"1": "[NOME]", "2": "[HORARIO]"}
    ctx contém os valores ja resolvidos: nome, advogada, horario, data, contact_name, etc.
    """
    placeholder_map = {
        "[NOME]": ctx.get("nome", "") or ctx.get("contact_name", ""),
        "[CONTACT_NAME]": ctx.get("contact_name", ""),
        "[ADVOGADA]": ctx.get("advogada", ""),
        "[HORARIO]": ctx.get("horario", ""),
        "[DATA]": _formatar_data_lembrete(ctx.get("data", "")),
    }
    params = {}
    for num, placeholder in (template_vars or {}).items():
        valor = placeholder_map.get(placeholder, "")
        params[str(num)] = valor or "-"
    return params


async def _enviar_template_lembrete(
    chatwoot_url: str,
    token: str,
    account_id: int,
    conversation_id: int,
    template_name: str,
    processed_params: dict,
) -> None:
    """Envia template WhatsApp via Chatwoot e registra nota privada com conteúdo renderizado."""
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    payload = {
        "message_type": "outgoing",
        "private": False,
        "template_params": {
            "name": template_name,
            "language": "pt_BR",
            "processed_params": processed_params or {},
        },
    }
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            url,
            headers={"api_access_token": token, "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()

    # Nota privada com conteúdo renderizado para visibilidade interna
    try:
        from inatividade import _buscar_conteudo_template, _enviar_nota_privada
        conteudo = await _buscar_conteudo_template(account_id, template_name)
        if conteudo and processed_params:
            for num, val in (processed_params or {}).items():
                conteudo = conteudo.replace(f"{{{{{num}}}}}", str(val))
        nota = f"📎 Template enviado: *{template_name}*"
        if conteudo:
            nota += f"\n\n{conteudo}"
        await _enviar_nota_privada(chatwoot_url, token, account_id, conversation_id, nota)
    except Exception as e:
        logger.debug(f"[lembrete-consulta] Falha ao postar nota privada do template: {e}")


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

    from db import (
        listar_agendamentos_pendentes,
        marcar_lembrete_enviado,
        carregar_config_cliente,
        houve_reagendamento_na_conversa,
        cancelar_agendamento_por_id,
    )
    from ia import enviar_parte_chatwoot, confirmar_evento_no_calendar
    from inatividade import _get_inbox_channel_type

    agendamentos = listar_agendamentos_pendentes()
    if not agendamentos:
        return

    # Dedupe: se houver múltiplos agendamentos ativos para (conv, data, hora),
    # mantém só o mais recente (maior id). Evita lembrete duplicado quando o mesmo
    # horário foi inserido mais de uma vez na mesma conversa.
    _dedup: dict[tuple, dict] = {}
    for _ag in agendamentos:
        _key = (_ag.get("conversation_id"), _ag.get("scheduled_date"), (_ag.get("scheduled_time", "") or "")[:5])
        _prev = _dedup.get(_key)
        if _prev is None or (_ag.get("id") or 0) > (_prev.get("id") or 0):
            _dedup[_key] = _ag
    if len(_dedup) < len(agendamentos):
        logger.info(f"[lembrete-consulta] Dedupe: {len(agendamentos)} → {len(_dedup)} agendamento(s) únicos")
    agendamentos = list(_dedup.values())

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

            # Confirmação no calendar feita uma única vez por agendamento por ciclo.
            # None = ainda não verificou; True = ok pra enviar; False = pular
            confirmacao_calendar: bool | None = None
            agendamento_cancelado_por_calendar = False

            chatwoot_url = config["chatwoot_url"].rstrip("/")
            token = config["chatwoot_token"]

            # Detectar inbox WhatsApp Oficial: nesses casos, texto livre falha fora da janela
            # de 24h (e o lembrete quase sempre dispara fora da janela). Usar template.
            inbox_id_ag = ag.get("inbox_id")
            channel_type = ""
            if inbox_id_ag:
                try:
                    channel_type = await _get_inbox_channel_type(config, inbox_id_ag)
                except Exception as e:
                    logger.warning(f"[lembrete-consulta] Falha ao detectar channel_type ag {ag_id}: {e}")
            is_whatsapp_oficial = "whatsapp" in (channel_type or "").lower()

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

                # Janela de envio: do momento ideal até 10min depois.
                # Janela curta reduz risco de re-envio quando o cache em memória
                # é perdido (ex: deploy) e a persistência em banco falha silenciosa.
                if not (send_time <= now <= send_time + timedelta(minutes=10)):
                    continue

                # Antes de enviar, confirmar que o agendamento ainda existe na agenda.
                # Pulamos a confirmação se houve reagendamento na conversa: o evento antigo
                # permanece no Google Calendar (n8n nao deleta), entao consultar daria
                # falso-positivo. Confiamos no DB nesse caso.
                if confirmacao_calendar is None:
                    if houve_reagendamento_na_conversa(account_id, conversation_id):
                        confirmacao_calendar = True
                    else:
                        try:
                            confirmado = await confirmar_evento_no_calendar(
                                config, date_str, time_str, advogada, ag.get("contact_name", "")
                            )
                        except Exception as e:
                            logger.warning(f"[lembrete-consulta] Erro inesperado em confirmar_evento_no_calendar (ag {ag_id}): {e}")
                            confirmado = None
                        if confirmado is False:
                            logger.info(
                                f"[lembrete-consulta] Agendamento {ag_id} nao localizado no calendar "
                                f"({date_str} {time_str} — {advogada}) — marcando cancelado e pulando lembretes"
                            )
                            cancelar_agendamento_por_id(ag_id)
                            agendamento_cancelado_por_calendar = True
                            confirmacao_calendar = False
                            break  # sai do for lembrete
                        # True ou None (fail-open): segue o envio
                        confirmacao_calendar = True

                if confirmacao_calendar is False:
                    break

                template_name = (lembrete.get("template_whatsapp") or "").strip()
                template_vars = lembrete.get("template_vars") or {}

                # Inbox WhatsApp Oficial sem template configurado → não pode garantir
                # entrega fora da janela 24h. Pula com warning.
                if is_whatsapp_oficial and not template_name:
                    logger.warning(
                        f"[lembrete-consulta] WhatsApp Oficial sem template_whatsapp configurado — "
                        f"envio pulado conv={conversation_id} ag={ag_id} ({minutos_antes}min antes)"
                    )
                    continue

                mensagem_template = lembrete.get("mensagem", _LEMBRETES_PADRAO[0]["mensagem"])
                mensagem = (
                    mensagem_template
                    .replace("{nome}", nome)
                    .replace("{advogada}", advogada)
                    .replace("{horario}", horario)
                    .replace("{data}", date_str)
                )

                # Pré-reserva: marcar no banco ANTES de enviar. Se o container cair
                # entre a marcação e o envio, cliente perde um lembrete — aceitável.
                # Se marcarmos DEPOIS e a persistência falhar, duplicaria. Preferimos
                # a 1ª falha (silêncio) à 2ª (spam).
                reservado_em_banco = False
                try:
                    _db2 = get_db()
                    _db2.table("ia_agendamentos").update({
                        "lembretes_enviados_min": list(lembretes_ja_enviados | {minutos_antes})
                    }).eq("id", ag_id).execute()
                    reservado_em_banco = True
                except Exception as e:
                    logger.warning(f"[lembrete-consulta] Falha ao pré-reservar lembrete no banco (ag {ag_id}): {e} — caindo no cache em memória")

                _lembretes_enviados.add(cache_key)  # reserva em memória sempre
                try:
                    if is_whatsapp_oficial and template_name:
                        ctx_lembrete = {
                            "nome": nome,
                            "contact_name": ag.get("contact_name", ""),
                            "advogada": advogada,
                            "horario": horario,
                            "data": date_str,
                        }
                        processed_params = _build_processed_params_lembrete(template_vars, ctx_lembrete)
                        await _enviar_template_lembrete(
                            chatwoot_url, token, account_id, conversation_id, template_name, processed_params
                        )
                        logger.info(
                            f"[lembrete-consulta] Template '{template_name}' enviado ({minutos_antes}min antes) "
                            f"para {ag.get('contact_name','')} — ag {ag_id} params={processed_params}"
                        )
                    else:
                        await enviar_parte_chatwoot(chatwoot_url, token, account_id, conversation_id, mensagem)
                        logger.info(f"[lembrete-consulta] Enviado ({minutos_antes}min antes) para {ag.get('contact_name','')} — ag {ag_id}")
                    lembretes_ja_enviados.add(minutos_antes)
                    enviados += 1
                except Exception as e:
                    logger.error(f"[lembrete-consulta] Erro ao enviar (ag {ag_id}): {e}")
                    # Se envio falhou e reservamos no banco, tentar reverter para permitir retry no próximo ciclo
                    if reservado_em_banco:
                        try:
                            _db2.table("ia_agendamentos").update({
                                "lembretes_enviados_min": list(lembretes_ja_enviados)
                            }).eq("id", ag_id).execute()
                        except Exception:
                            pass

            # Marcar lembrete_enviado=true quando todos os lembretes configurados foram enviados
            if agendamento_cancelado_por_calendar:
                continue
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
