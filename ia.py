from openai import OpenAI
from datetime import datetime, timezone, timedelta
from db import upsert_conversation, upsert_lead, inserir_agendamento, listar_advogados_por_especialidade, normalizar_especialidade, existe_agendamento_ativo, cancelar_agendamentos_anteriores
import asyncio
import httpx
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENTES_DIR = os.path.join(BASE_DIR, "clientes")


def _eh_payload_anuncio(texto: str) -> bool:
    """Detecta payload estruturado de 'Mensagem de Anúncio' Meta (mesma heurística de classificar_origem)."""
    if not texto:
        return False
    limpo = re.sub(r"\*+", "", texto)
    return bool(
        re.search(r"Mensagem de An[uú]ncio", limpo, re.IGNORECASE)
        or re.search(r"Texto\s+An[uú]ncio\s*:", limpo, re.IGNORECASE)
        or (re.search(r"SourceID\s*:", limpo) and re.search(r"Fonte\s*:", limpo))
    )

# Debounce: conversation_id -> asyncio.Task
_debounce_tasks: dict[int, asyncio.Task] = {}

# Lock serial por conversa: impede duas invocações paralelas de processar_mensagem
# Sem isso, duas tasks de debounce podem disparar em paralelo e gerar propostas
# duplicadas (ex: cliente manda "Sim" + msg do bot anterior ainda ecoando no webhook
# → duas ConsultarAgenda independentes → dois horários diferentes propostos).
_processing_locks: dict[int, asyncio.Lock] = {}

# ── MAPEAMENTO DE TOOLS POR FASE (WAT Architecture) ─────────

TOOLS_POR_FASE = {
    "identificacao": ["atualiza_contato"],
    "vinculo": ["atualiza_contato", "cliente_inviavel", "TransferHuman"],
    "coleta_caso": ["atualiza_contato", "cliente_inviavel", "TransferHuman"],
    "avaliacao": ["atualiza_contato", "cliente_inviavel", "TransferHuman", "lead_disponivel"],
    "casos_especiais": ["atualiza_contato", "TransferHuman", "cliente_inviavel", "desqualificado", "nao_lead", "nao_alfabetizado"],
    "explicacao": ["atualiza_contato", "TransferHuman"],
    "agendamento": ["atualiza_contato", "ConsultarAgenda", "Agendar", "convertido"],
    "inatividade": ["aguardando_cliente", "desqualificado"],
}

# ── TOOLS DISPONÍVEIS PARA A IA ──────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cliente_inviavel",
            "description": "Marca o cliente como inviável. Adiciona label 'inviavel' na conversa do Chatwoot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "description": "Motivo técnico da inviabilidade"}
                },
                "required": ["motivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "TransferHuman",
            "description": "Transfere a conversa para um humano. Use APENAS quando: o cliente pede explicitamente para falar com humano/advogado/responsável, OU o assunto está completamente fora do seu escopo. NÃO use quando o cliente está pensando, pausou, deu resposta curta (sim, não, ok, tô pensando) ou fez pergunta que você consegue responder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "description": "Motivo da transferência"}
                },
                "required": ["motivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "convertido",
            "description": "Marca o cliente como convertido após agendamento confirmado. Adiciona label 'convertido'.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lead_disponivel",
            "description": "Cliente quer falar imediatamente ou ligar agora. Adiciona label 'lead-disponivel'.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ConsultarAgenda",
            "description": "Consulta os horários disponíveis na agenda para agendamento. Informe a especialidade do caso.",
            "parameters": {
                "type": "object",
                "properties": {
                    "especialidade": {"type": "string", "description": "Especialidade do caso (ex: Trabalhista, Previdenciário, Cível)"}
                },
                "required": ["especialidade"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "Agendar",
            "description": "Confirma o agendamento de uma consulta. Use os dados retornados pelo ConsultarAgenda.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Data e hora de início (ex: 2026-03-18 09:00)"},
                    "end": {"type": "string", "description": "Data e hora de fim (ex: 2026-03-18 09:30)"},
                    "advogado": {"type": "string", "description": "Nome do advogado escolhido"},
                    "cor_id": {"type": "integer", "description": "ID da cor do advogado (retornado pelo ConsultarAgenda)"},
                    "especialidade": {"type": "string", "description": "Especialidade do caso (ex: Trabalhista, Previdenciário, Cível)"},
                    "resumo": {"type": "string", "description": "Resumo breve do caso do cliente (tipo de acidente, sequela)"}
                },
                "required": ["start", "end", "advogado", "cor_id", "especialidade", "resumo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "atualiza_contato",
            "description": "Atualiza o nome do contato no Chatwoot quando o cliente informa um nome diferente do cadastrado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string", "description": "Nome informado pelo cliente"}
                },
                "required": ["nome"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "aguardando_cliente",
            "description": "Marca que o cliente pediu para falar depois ou vai retornar. Registra que a IA está aguardando resposta.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "description": "Ex: cliente disse que retorna depois"}
                },
                "required": ["motivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desqualificado",
            "description": "Marca o lead como desqualificado (sem interesse ou caso não se encaixa).",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "description": "Motivo da desqualificação"}
                },
                "required": ["motivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "nao_lead",
            "description": "Marca como não-lead (fornecedor, parceiro, prestador de serviço).",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "description": "Motivo (ex: fornecedor, parceiro)"}
                },
                "required": ["motivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "nao_alfabetizado",
            "description": "Marca que o cliente não sabe ler/escrever e precisa de atendimento humano.",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]

# ── NOTIFICAÇÕES ─────────────────────────────────────────────

def _gerar_resumo_caso(historico_texto: str, openai_api_key: str = None) -> str:
    """Extrai um resumo breve focado na qualificação do caso usando IA."""
    if not historico_texto.strip():
        return "Sem detalhes disponíveis"

    if openai_api_key:
        try:
            client = OpenAI(api_key=openai_api_key)
            resp = client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": (
                        "Resuma em 1-2 frases curtas APENAS as informações de qualificação do caso jurídico "
                        "a partir do histórico de conversa. Inclua: tipo de problema, situação do cliente, "
                        "detalhes relevantes (vínculo, acidente, doença, etc). "
                        "IGNORE completamente: mensagens sobre agendamento, horários, reagendamento, "
                        "saudações, confirmações genéricas (sim, ok, quero). "
                        "Responda direto, sem prefixos."
                    )},
                    {"role": "user", "content": historico_texto[-3000:]},
                ],
                max_tokens=150,
                temperature=0,
            )
            resumo = (resp.choices[0].message.content or "").strip()
            if resumo:
                return resumo
        except Exception as e:
            logger.warning(f"[resumo] Erro ao gerar resumo com IA: {e}")

    # Fallback: método simples
    linhas = historico_texto.strip().splitlines()
    msgs_cliente = [l.split(": ", 1)[1] if ": " in l else l for l in linhas if l.startswith("[Cliente]")]
    if not msgs_cliente:
        return "Sem detalhes disponíveis"
    texto = " | ".join(msgs_cliente[-10:])
    return texto[:300] + "..." if len(texto) > 300 else texto


# Contas que usam Chatwoot externo para notificações de grupo
# account_id_externo: ID da conta no Chatwoot externo (pode diferir do account_id local)
_NOTIF_CHATWOOT_EXTERNO = {
    8: {"token": "xJq2E7owxv89RaMbippvSV5J", "account_id_externo": 4},
    11: {"token": "xJq2E7owxv89RaMbippvSV5J", "account_id_externo": 4},
}


# Mapeamento fixo: conta → conversa do grupo "Novos Leads" (contas com Chatwoot externo)
_GRUPO_NOVOS_LEADS = {
    8: 75,
    11: 76,
}


def _get_grupo_novos_leads(account_id: int, config: dict) -> int | None:
    """Retorna o ID da conversa do grupo de novos leads para a conta."""
    # Primeiro: mapeamento fixo (contas com grupo em Chatwoot externo)
    if account_id in _GRUPO_NOVOS_LEADS:
        return _GRUPO_NOVOS_LEADS[account_id]
    # Fallback: campo id_notificacao_convertido do config
    notif = config.get("id_notificacao_convertido")
    return int(notif) if notif else None


# Cache de notificações de transferência já enviadas (evitar duplicação)
_transferencias_notificadas: dict[str, bool] = {}

async def _notificar_transferencia_humano(
    config: dict, account_id: int, conversation_id: int,
    contact_name: str, contact_phone: str, tipo: str, motivo: str = "",
):
    """Notifica o grupo de clientes sobre transferência para especialista (todas as contas com id_notificacao_cliente)."""
    notif_conv_id = config.get("id_notificacao_cliente")
    if not notif_conv_id:
        return

    # Evitar duplicação: mesma conta + conversa + tipo OU mesmo telefone em curto prazo
    chave_conv = f"{account_id}:{conversation_id}:{tipo}"
    chave_phone = f"{account_id}:{contact_phone}:{tipo}" if contact_phone else None
    if chave_conv in _transferencias_notificadas:
        logger.info(f"[notificação] Transferência já notificada — conv={conversation_id} tipo='{tipo}' — ignorando duplicata")
        return
    if chave_phone and chave_phone in _transferencias_notificadas:
        logger.info(f"[notificação] Transferência já notificada para telefone {contact_phone} tipo='{tipo}' — ignorando duplicata (outra conversa)")
        return
    _transferencias_notificadas[chave_conv] = True
    if chave_phone:
        _transferencias_notificadas[chave_phone] = True
    # Limitar tamanho do cache
    if len(_transferencias_notificadas) > 5000:
        # Remover metade mais antiga
        keys = list(_transferencias_notificadas.keys())
        for k in keys[:2500]:
            del _transferencias_notificadas[k]

    try:
        msg = (
            f"🔀 TRANSFERÊNCIA PARA ESPECIALISTA\n\n"
            f"Tipo: {tipo}\n"
            f"Nome: {contact_name}\n"
            f"Número: {contact_phone}\n"
        )
        if motivo:
            msg += f"Motivo: {motivo}\n"
        msg += f"Conversa: {conversation_id}"
        await _enviar_notificacao(config, account_id, int(notif_conv_id), msg)
        logger.info(f"[notificação] Transferência '{tipo}' notificada — conv={conversation_id}")
    except Exception as e:
        logger.warning(f"[notificação] Erro ao notificar transferência — conv={conversation_id}: {e}")


async def _enviar_notificacao(config: dict, account_id: int, conv_id_notif: int, mensagem: str):
    """Envia notificação para o grupo do Chatwoot (pode ser outro Chatwoot)."""
    externo = _NOTIF_CHATWOOT_EXTERNO.get(account_id)
    if externo:
        notif_url = externo.get("url") or config.get("chatwoot_url", "")
        notif_token = externo["token"]
        notif_account_id = externo.get("account_id_externo", account_id)
    else:
        notif_url = config.get("chatwoot_url", "")
        notif_token = config.get("chatwoot_token", "")
        notif_account_id = account_id
    notif_url = notif_url.rstrip("/")

    url = f"{notif_url}/api/v1/accounts/{notif_account_id}/conversations/{conv_id_notif}/messages"
    headers = {"api_access_token": notif_token, "Content-Type": "application/json"}
    logger.info(f"[notificação] Enviando para {notif_url} account={notif_account_id} conv={conv_id_notif}")
    async with httpx.AsyncClient() as http:
        resp = await http.post(url, headers=headers, json={"content": mensagem, "message_type": "outgoing", "private": False}, timeout=15)
        if not resp.is_success:
            logger.error(f"[notificação] ERRO: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    logger.info(f"[notificação] Mensagem enviada com sucesso para conversa {conv_id_notif}")


# ── EXECUÇÃO DAS TOOLS ────────────────────────────────────────

async def executar_tool(nome: str, args: dict, config: dict, conversation_id: int, context: dict) -> str:
    account_id = config["account_id"]
    inbox_id = context.get("inbox_id")
    contact_name = context.get("contact_name", "")
    contact_phone = context.get("contact_phone", "")
    chatwoot_url = config["chatwoot_url"]
    chatwoot_token = config["chatwoot_token"]

    # Kanban: mover/criar card automaticamente (fire-and-forget)
    if nome in KANBAN_TOOL_MAP:
        try:
            await kanban_mover_card(chatwoot_url, chatwoot_token, account_id, conversation_id, contact_name, nome)
        except Exception as e:
            logger.warning(f"[kanban] Erro ao processar card para tool {nome}: {e}")

    if nome == "cliente_inviavel":
        await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "inviavel")
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id, motivo=f"tool:cliente_inviavel — {args.get('motivo','')}")
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone,
                        status="inviavel", inviability_reason=args.get("motivo"))
        except Exception as e:
            logger.warning(f"Supabase erro (cliente_inviavel): {e}")
        # Notificar grupo de novos leads sobre inviável
        try:
            notif_conv_id = _get_grupo_novos_leads(account_id, config)
            if notif_conv_id:
                motivo_inviavel = args.get("motivo", "sem motivo informado")
                msg_notif = (
                    f"🚫 LEAD INVIÁVEL\n\n"
                    f"Nome: {contact_name}\n"
                    f"Número: {contact_phone}\n"
                    f"Motivo: {motivo_inviavel}\n"
                    f"Conversa: {conversation_id}"
                )
                await _enviar_notificacao(config, account_id, int(notif_conv_id), msg_notif)
                logger.info(f"[notificação] Inviável notificado no grupo novos leads — conv={conversation_id}")
        except Exception as e:
            logger.warning(f"[notificação] Erro ao notificar inviável — conv={conversation_id}: {e}")
        await _notificar_transferencia_humano(config, account_id, conversation_id, contact_name, contact_phone, "Cliente inviável", args.get("motivo", ""))
        logger.info(f"Tool: cliente_inviavel — {args.get('motivo')}")
        return json.dumps({"status": "ok"})

    if nome == "TransferHuman":
        # Proteção anti-desatribuição prematura (conta 17): bloquear TransferHuman
        # se a qualificação mínima ainda não foi coletada.
        if account_id == 17:
            historico = context.get("historico") or []
            msgs_cliente = [m for m in historico if m.get("message_type") == 0]
            historico_txt = " ".join([(m.get("content") or "") for m in historico]).lower()
            motivo_txt = (args.get("motivo", "") or "").lower()
            previdenciario_puro = any(p in historico_txt or p in motivo_txt for p in [
                "aposentadoria", "aposentar", "bpc", "loas", "auxilio-doenca", "auxílio-doença",
                "auxilio doenca", "auxilio-acidente", "auxílio-acidente", "pericia do inss", "perícia do inss"
            ])
            gestante_ou_acidente = any(p in historico_txt for p in [
                "gravid", "gestante", "gestação", "gestacao", "maternidade",
                "acidente de trabalho", "acidente no trabalho"
            ])
            inss_empresa = ("inss" in historico_txt) and any(p in historico_txt for p in [
                "não recolh", "nao recolh", "não pagou", "nao pagou", "não contribu", "nao contribu",
                "recolheu errado", "recolheu menos", "recolheu a menos", "patrão", "patrao", "empresa"
            ])
            # Se não é previdenciário puro e cliente teve poucas mensagens, bloquear
            if not previdenciario_puro and (gestante_ou_acidente or inss_empresa or len(msgs_cliente) <= 5):
                logger.warning(
                    f"🛑 Conta 17: TransferHuman bloqueado — qualificação incompleta "
                    f"(cliente_msgs={len(msgs_cliente)}, gestante/acidente={gestante_ou_acidente}, "
                    f"inss_empresa={inss_empresa}, motivo='{args.get('motivo','')}')"
                )
                return json.dumps({
                    "status": "bloqueado",
                    "motivo": "Qualificacao minima ainda nao coletada (tempo, carteira, funcao, motivo). Continue perguntando — NAO acione TransferHuman. Se o cliente falou em INSS nao recolhido pela empresa, salario-maternidade ou e gestante, isso e TRABALHISTA — continue a qualificacao normalmente."
                })

        # Proteção anti-desatribuição prematura (conta 11): bloquear TransferHuman
        # quando o motivo é "benefício ativo" mas o cliente não confirmou isso no presente.
        if account_id == 11:
            historico = context.get("historico") or []
            msgs_cliente = [m for m in historico if m.get("message_type") == 0]
            historico_txt = " ".join([(m.get("content") or "") for m in historico]).lower()
            motivo_txt = (args.get("motivo", "") or "").lower()
            # Motivo indica benefício ativo
            motivo_beneficio = any(p in motivo_txt for p in [
                "beneficio ativo", "benefício ativo", "beneficio cessando", "benefício cessando",
                "recebe beneficio", "recebe benefício", "esta recebendo", "está recebendo"
            ])
            # Confirmação explícita de benefício ativo no PRESENTE
            confirmacao_presente = any(p in historico_txt for p in [
                "recebo auxilio", "recebo auxílio", "recebo o auxilio", "recebo o auxílio",
                "estou recebendo auxilio", "estou recebendo auxílio", "estou recebendo bpc",
                "recebo bpc", "meu beneficio esta ativo", "meu benefício está ativo",
                "ainda recebo", "ainda estou recebendo", "continuo recebendo",
                "recebo mensalmente", "recebo do inss mensalmente"
            ])
            # Qualificação mínima: precisa ter pelo menos 4 mensagens do cliente para ter contexto
            qualificacao_incompleta = len(msgs_cliente) <= 4
            if motivo_beneficio and not confirmacao_presente:
                logger.warning(
                    f"🛑 Conta 11: TransferHuman bloqueado por 'benefício ativo' não confirmado "
                    f"(cliente_msgs={len(msgs_cliente)}, motivo='{args.get('motivo','')}')"
                )
                return json.dumps({
                    "status": "bloqueado",
                    "motivo": "Beneficio ativo NAO foi confirmado explicitamente pelo cliente no presente. O cliente pode ter recebido beneficio no passado, ou estar respondendo 'sim' a uma pergunta combinada. PERGUNTE: 'Voce ainda recebe esse beneficio hoje ou ja acabou?' antes de transferir."
                })
            if qualificacao_incompleta and not confirmacao_presente:
                logger.warning(
                    f"🛑 Conta 11: TransferHuman bloqueado — qualificação incompleta "
                    f"(cliente_msgs={len(msgs_cliente)}, motivo='{args.get('motivo','')}')"
                )
                return json.dumps({
                    "status": "bloqueado",
                    "motivo": "Qualificacao minima incompleta (menos de 5 mensagens do cliente). Continue perguntando sobre o caso, sequela, laudo e profissao. NAO acione TransferHuman sem confirmar os dados basicos."
                })
        # Roteamento especial conta 1 (Matsuda)
        motivo_txt = (args.get("motivo", "") or "").lower()

        # Dra. Christina Matias (agente 76) — andamento/acompanhamento processual
        routing_christina = account_id == 1 and any(
            chave in motivo_txt for chave in [
                "andamento processual", "acompanhamento processual",
                "consulta processual", "dra. christina", "dra christina",
            ]
        )

        # Dra. Fernanda Matsuda (agente 6) — advogado da reclamada, acordo, empresa
        routing_fernanda = account_id == 1 and not routing_christina and any(
            chave in motivo_txt for chave in [
                "advogado da reclamada", "advogada da reclamada",
                "representante da empresa", "representante legal",
                "dono da empresa", "dona da empresa",
                "socio da empresa", "sócia da empresa", "socia da empresa",
                "preposto", "dra. fernanda", "dra fernanda",
                "acordo", "adv reclamada",
            ]
        )

        routing_especial_conta1 = routing_christina or routing_fernanda

        if routing_christina:
            await chatwoot_transferir_humano(
                chatwoot_url, chatwoot_token, account_id, conversation_id,
                motivo=f"tool:TransferHuman — {args.get('motivo','')}",
                assignee_id=76,
            )
            try:
                notif_conv_id = config.get("id_notificacao_convertido")
                if notif_conv_id:
                    msg_notif = (
                        f"📂 ANDAMENTO PROCESSUAL\n\n"
                        f"Nome: {contact_name}\n"
                        f"Número: {contact_phone}\n"
                        f"Motivo: {args.get('motivo','')}\n"
                        f"Atribuído a: Dra. Christina Matias (agente 76)\n"
                        f"Conversa: {conversation_id}"
                    )
                    await _enviar_notificacao(config, account_id, int(notif_conv_id), msg_notif)
                    logger.info(f"[notificação] Andamento processual notificado no grupo — conv={conversation_id}")
            except Exception as e:
                logger.warning(f"[notificação] Erro ao notificar andamento processual — conv={conversation_id}: {e}")
        elif routing_fernanda:
            await chatwoot_transferir_humano(
                chatwoot_url, chatwoot_token, account_id, conversation_id,
                motivo=f"tool:TransferHuman — {args.get('motivo','')}",
                assignee_id=6,
            )
            try:
                notif_conv_id = config.get("id_notificacao_convertido")
                if notif_conv_id:
                    msg_notif = (
                        f"⚖️ ADVOGADO/REPRESENTANTE/ACORDO\n\n"
                        f"Nome: {contact_name}\n"
                        f"Número: {contact_phone}\n"
                        f"Motivo: {args.get('motivo','')}\n"
                        f"Atribuído a: Dra. Fernanda Matsuda (agente 6)\n"
                        f"Conversa: {conversation_id}"
                    )
                    await _enviar_notificacao(config, account_id, int(notif_conv_id), msg_notif)
                    logger.info(f"[notificação] Advogado/representante/acordo notificado no grupo — conv={conversation_id}")
            except Exception as e:
                logger.warning(f"[notificação] Erro ao notificar advogado/representante/acordo — conv={conversation_id}: {e}")
        else:
            await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id, motivo=f"tool:TransferHuman — {args.get('motivo','')}")
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone, status="transferido")
        except Exception as e:
            logger.warning(f"Supabase erro (TransferHuman): {e}")
        if not routing_especial_conta1:
            await _notificar_transferencia_humano(config, account_id, conversation_id, contact_name, contact_phone, "Solicitação do cliente", args.get("motivo", ""))
        logger.info(f"Tool: TransferHuman — {args.get('motivo')}")
        return json.dumps({"status": "ok"})

    if nome == "convertido":
        await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "convertido")
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone, status="convertido")
        except Exception as e:
            logger.warning(f"Supabase erro (convertido): {e}")
        # Notificação no grupo do Chatwoot
        # Notificação de convertido não envia mensagem própria — a notificação
        # principal já é disparada pelo Agendar (STATUS: SUCESSO) para evitar duplicidade.
        logger.info("Tool: convertido")
        return json.dumps({"status": "ok"})

    if nome == "lead_disponivel":
        await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "lead-disponivel")
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id, motivo="tool:lead_disponivel")
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone, status="transferido")
        except Exception as e:
            logger.warning(f"Supabase erro (lead_disponivel): {e}")
        await _notificar_transferencia_humano(config, account_id, conversation_id, contact_name, contact_phone, "Lead disponível (quer falar agora)")
        logger.info("Tool: lead_disponivel")
        return json.dumps({"status": "ok"})

    if nome == "ConsultarAgenda":
        try:
            especialidade_ia = normalizar_especialidade(args.get("especialidade", config.get("especialidade", "")))
            slots = await consultar_agenda_real(config, especialidade_ia)
            logger.info(f"Tool: ConsultarAgenda ({especialidade_ia}) → {len(slots)} advogados com slots")
            return json.dumps({"slots": slots})
        except Exception as e:
            logger.error(f"Tool: ConsultarAgenda ERRO: {e}")
            return json.dumps({"erro": f"Falha ao consultar agenda: {str(e)}"})

    if nome == "Agendar":
        is_reagendamento = context.get("is_reagendamento", False)
        # Proteção anti-duplicação: se já existe agendamento ativo pra esta conversa
        # e não é reagendamento, bloquear nova criação.
        if not is_reagendamento:
            try:
                existente = existe_agendamento_ativo(account_id, conversation_id)
                if existente:
                    logger.warning(
                        f"🚫 [agenda] Agendar duplicado bloqueado — conv={conversation_id} "
                        f"já tem agendamento em {existente.get('scheduled_date')} {existente.get('scheduled_time')} "
                        f"com {existente.get('advogada')}"
                    )
                    return json.dumps({
                        "STATUS": "JA_AGENDADO",
                        "mensagem_sistema": (
                            f"Ja existe agendamento ativo para esta conversa: "
                            f"{existente.get('scheduled_date')} {existente.get('scheduled_time')} "
                            f"com {existente.get('advogada')}. NAO agendar novamente — confirme ao cliente o horario ja reservado."
                        ),
                        "advogado": existente.get("advogada", ""),
                    })
            except Exception as e:
                logger.warning(f"[agenda] Erro ao checar agendamento existente: {e}")
        try:
            resultado = await agendar_real(args, config, context)
        except Exception as e:
            logger.error(f"Tool: Agendar ERRO: {e}")
            return json.dumps({"STATUS": "ERRO", "mensagem_sistema": f"Falha ao agendar: {str(e)}"})
        if resultado.get("STATUS") == "SUCESSO":
            try:
                # Extrair data e hora do start (formato "YYYY-MM-DD HH:MM")
                start_str = args.get("start", "")
                parts = start_str.split(" ", 1) if start_str else ["", ""]
                sched_date = parts[0] if len(parts) > 0 else ""
                sched_time = parts[1] if len(parts) > 1 else ""
                # Reagendamento: cancelar agendamentos anteriores para impedir
                # que o sistema de lembretes dispare o horário antigo.
                if is_reagendamento:
                    try:
                        cancelados = cancelar_agendamentos_anteriores(account_id, conversation_id)
                        if cancelados:
                            logger.info(f"🗑️ Reagendamento: {cancelados} agendamento(s) anterior(es) cancelado(s) — conv={conversation_id}")
                    except Exception as e:
                        logger.warning(f"Erro ao cancelar agendamentos anteriores: {e}")
                inserir_agendamento(
                    account_id=account_id,
                    inbox_id=inbox_id,
                    conversation_id=conversation_id,
                    contact_name=contact_name,
                    contact_phone=contact_phone,
                    scheduled_date=sched_date,
                    scheduled_time=sched_time,
                    advogada=args.get("advogado", ""),
                )
            except Exception as e:
                logger.warning(f"Supabase erro (Agendar): {e}")

            # Etiqueta e notificação
            advogado = args.get("advogado", "")
            if is_reagendamento:
                # Reagendamento: adicionar etiqueta e notificar
                await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "reagendamento")
                logger.info(f"🔄 Etiqueta 'reagendamento' adicionada — conv={conversation_id}")
                notif_conv_id = config.get("id_notificacao_convertido")
                logger.info(f"[notificação] account={account_id} id_notificacao_convertido={notif_conv_id} (reagendamento)")
                if notif_conv_id:
                    try:
                        msg_notif = (
                            f"🔄 REAGENDAMENTO!\n\n"
                            f"Nome: {contact_name}\n"
                            f"Número: {contact_phone}\n\n"
                            f"Reagendado: {sched_date} às {sched_time} com {advogado}."
                        )
                        await _enviar_notificacao(config, account_id, int(notif_conv_id), msg_notif)
                    except Exception as e:
                        logger.warning(f"[notificação] Erro ao notificar reagendamento: {e}")
            else:
                # Novo agendamento
                notif_conv_id = config.get("id_notificacao_convertido")
                logger.info(f"[notificação] account={account_id} id_notificacao_convertido={notif_conv_id}")
                if notif_conv_id:
                    try:
                        resumo = _gerar_resumo_caso(context.get("historico_texto", ""), config.get("openai_api_key"))
                        msg_notif = (
                            f"📅 NOVO AGENDAMENTO!\n\n"
                            f"Nome: {contact_name}\n"
                            f"Número: {contact_phone}\n\n"
                            f"Agendado: {sched_date} às {sched_time} com {advogado}.\n"
                            f"Resumo: {resumo}"
                        )
                        await _enviar_notificacao(config, account_id, int(notif_conv_id), msg_notif)
                    except Exception as e:
                        logger.warning(f"[notificação] Erro ao notificar agendamento: {e}")

            # Conta 17: desatribuir após agendar e notificar
            if account_id == 17:
                try:
                    await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id, motivo="tool:Agendar:desatribuir_pos_agendamento")
                    logger.info(f"🔓 Conta 17: conversa desatribuída após agendamento — conv={conversation_id}")
                except Exception as e:
                    logger.warning(f"Conta 17: erro ao desatribuir pós-agendamento: {e}")

        logger.info(f"Tool: Agendar → {resultado}")
        return json.dumps(resultado)

    if nome == "atualiza_contato":
        novo_nome = args.get("nome", "")
        if novo_nome:
            await chatwoot_atualizar_contato(chatwoot_url, chatwoot_token, account_id, conversation_id, novo_nome)
        logger.info(f"Tool: atualiza_contato → {novo_nome}")
        return json.dumps({"status": "ok"})

    if nome == "aguardando_cliente":
        await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "aguardando-cliente")
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone, status="aguardando")
        except Exception as e:
            logger.warning(f"Supabase erro (aguardando_cliente): {e}")
        logger.info(f"Tool: aguardando_cliente — {args.get('motivo')}")
        return json.dumps({"status": "ok"})

    if nome == "desqualificado":
        await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "desqualificado")
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id, motivo=f"tool:desqualificado — {args.get('motivo','')}")
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone,
                        status="desqualificado", inviability_reason=args.get("motivo"))
        except Exception as e:
            logger.warning(f"Supabase erro (desqualificado): {e}")
        await _notificar_transferencia_humano(config, account_id, conversation_id, contact_name, contact_phone, "Desqualificado", args.get("motivo", ""))
        logger.info(f"Tool: desqualificado — {args.get('motivo')}")
        return json.dumps({"status": "ok"})

    if nome == "nao_lead":
        await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "nao-lead")
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id, motivo=f"tool:nao_lead — {args.get('motivo','')}")
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone,
                        status="desqualificado", inviability_reason=args.get("motivo"))
        except Exception as e:
            logger.warning(f"Supabase erro (nao_lead): {e}")
        await _notificar_transferencia_humano(config, account_id, conversation_id, contact_name, contact_phone, "Não é lead", args.get("motivo", ""))
        logger.info(f"Tool: nao_lead — {args.get('motivo')}")
        return json.dumps({"status": "ok"})

    if nome == "nao_alfabetizado":
        await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "nao-alfabetizado")
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id, motivo="tool:nao_alfabetizado")
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone, status="transferido")
        except Exception as e:
            logger.warning(f"Supabase erro (nao_alfabetizado): {e}")
        await _notificar_transferencia_humano(config, account_id, conversation_id, contact_name, contact_phone, "Não alfabetizado")
        logger.info("Tool: nao_alfabetizado")
        return json.dumps({"status": "ok"})

    return json.dumps({"status": "tool_desconhecida"})


# ── CHATWOOT API ──────────────────────────────────────────────

async def chatwoot_adicionar_label(url: str, token: str, account_id: int, conversation_id: int, label: str):
    headers = {"api_access_token": token}
    labels_url = f"{url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"
    async with httpx.AsyncClient() as http:
        resp = await http.get(labels_url, headers=headers, timeout=10)
        existentes = resp.json().get("payload", []) if resp.is_success else []
        if label not in existentes:
            existentes.append(label)
        await http.post(labels_url, headers=headers, json={"labels": existentes}, timeout=10)
    logger.info(f"Label '{label}' adicionada na conversa {conversation_id}")


async def chatwoot_atualizar_contato(url: str, token: str, account_id: int, conversation_id: int, nome: str):
    """Atualiza o nome do contato na conversa do Chatwoot."""
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    # Buscar contact_id da conversa
    conv_url = f"{url}/api/v1/accounts/{account_id}/conversations/{conversation_id}"
    async with httpx.AsyncClient() as http:
        resp = await http.get(conv_url, headers=headers, timeout=10)
        if resp.is_success:
            contact_id = resp.json().get("meta", {}).get("sender", {}).get("id")
            if contact_id:
                contact_url = f"{url}/api/v1/accounts/{account_id}/contacts/{contact_id}"
                await http.put(contact_url, headers=headers, json={"name": nome}, timeout=10)
                logger.info(f"Contato {contact_id} atualizado para '{nome}'")


async def chatwoot_transferir_humano(url: str, token: str, account_id: int, conversation_id: int, motivo: str = "", assignee_id: int | None = None):
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    assign_url = f"{url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/assignments"
    async with httpx.AsyncClient() as http:
        await http.post(assign_url, headers=headers, json={"assignee_id": assignee_id}, timeout=10)
    # Log detalhado com stack trace para rastrear origem da atribuição/desatribuição
    import traceback
    caller = traceback.extract_stack()[-2]
    acao = f"ATRIBUIÇÃO→agente={assignee_id}" if assignee_id else "DESATRIBUIÇÃO"
    logger.info(f"🔴 {acao} — conta={account_id} conv={conversation_id} motivo='{motivo}' chamado_de={caller.filename}:{caller.lineno} ({caller.name})")

    # Desativar inatividade (follow-up) ao transferir para humano
    try:
        from db import desativar_inatividade
        desativar_inatividade(account_id, conversation_id)
        logger.info(f"[inatividade] Desativado ao transferir para humano — conv={conversation_id}")
    except Exception as e:
        logger.warning(f"[inatividade] Erro ao desativar na transferência — conv={conversation_id}: {e}")


# ── KANBAN (FUNNELS) ─────────────────────────────────────────

# Cache de funis por account_id para evitar chamadas repetidas
_funnel_cache: dict[int, dict] = {}

# Mapeamento: tool/ação → (funnel_identifier, step_identifier)
KANBAN_TOOL_MAP = {
    # Funil Comercial — fluxo principal
    "novo_lead": ("pipeline_comercial", "lead_novo"),
    "em_qualificacao": ("pipeline_comercial", "lead_novo"),
    "aguardando_cliente": ("pipeline_comercial", "aguardando_atendimento"),
    "Agendar": ("pipeline_comercial", "aguardando_assinatura"),
    "convertido": ("pipeline_comercial", "contrato_fechado"),
    "lead_disponivel": ("pipeline_comercial", "aguardando_atendimento"),
    "followup": ("pipeline_comercial", "followup"),
    "nao_assinou": ("pipeline_comercial", "nao_assinou"),
    "lead_perdido": ("pipeline_comercial", "nao_respondeu"),
    # Funil Triagem — casos fora do fluxo
    "TransferHuman": ("triagem_encaminhamento", "transferido"),
    "cliente_inviavel": ("triagem_encaminhamento", "inviavel"),
    "desqualificado": ("pipeline_comercial", "leads_desqualificados"),
    "nao_alfabetizado": ("triagem_encaminhamento", "nao_alfabetizado"),
    "nao_lead": ("pipeline_comercial", "leads_desqualificados"),
}


async def _carregar_funis(url: str, token: str, account_id: int, force_reload: bool = False) -> dict:
    """Carrega funis e etapas da conta, com cache."""
    if not force_reload and account_id in _funnel_cache:
        return _funnel_cache[account_id]

    headers = {"api_access_token": token}
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"{url}/api/v1/accounts/{account_id}/funnels", headers=headers, timeout=10)
            if not resp.is_success:
                logger.warning(f"[kanban] API de funis retornou {resp.status_code} para account_id={account_id}")
                return {}
            funnels = resp.json().get("payload", [])

        # Mapear: identifier → {funnel_id, steps: {step_identifier → step_id}}
        mapa = {}
        for f in funnels:
            steps = {}
            for s in f.get("funnel_steps", []):
                steps[s["identifier"]] = s["id"]
            mapa[f["identifier"]] = {"funnel_id": f["id"], "steps": steps}

        # Só cacheia se encontrou os funis esperados — evita gravar resultado vazio
        if mapa:
            _funnel_cache[account_id] = mapa
            logger.info(f"[kanban] Cache carregado para account_id={account_id}: {list(mapa.keys())}")
        else:
            logger.warning(f"[kanban] Nenhum funil encontrado para account_id={account_id} — cache NÃO persistido (tentará de novo)")
        return mapa
    except Exception as e:
        logger.warning(f"[kanban] Erro ao carregar funis (account_id={account_id}): {e}")
        return {}


async def kanban_mover_card(url: str, token: str, account_id: int, conversation_id: int,
                            contact_name: str, tool_name: str):
    """Cria ou move card no kanban baseado na tool acionada."""
    mapping = KANBAN_TOOL_MAP.get(tool_name)
    if not mapping:
        return

    funnel_identifier, step_identifier = mapping
    funis = await _carregar_funis(url, token, account_id)

    funil = funis.get(funnel_identifier)
    # Se funil não existe, tenta recarregar (pode ter sido criado após cache)
    if not funil and funis:
        logger.info(f"[kanban] Funil '{funnel_identifier}' ausente — tentando recarregar cache (account_id={account_id})")
        funis = await _carregar_funis(url, token, account_id, force_reload=True)
        funil = funis.get(funnel_identifier)
    if not funil:
        logger.warning(
            f"[kanban] Funil '{funnel_identifier}' NAO encontrado para account_id={account_id}. "
            f"Funis disponíveis: {list(funis.keys()) or 'nenhum'}. "
            f"Use POST /api/clientes/{account_id}/recriar-funis para recriar."
        )
        return

    funnel_id = funil["funnel_id"]
    step_id = funil["steps"].get(step_identifier)
    # Se etapa não existe, tenta recarregar
    if not step_id:
        logger.info(f"[kanban] Step '{step_identifier}' ausente — recarregando cache (account_id={account_id})")
        funis = await _carregar_funis(url, token, account_id, force_reload=True)
        funil = funis.get(funnel_identifier, {})
        step_id = funil.get("steps", {}).get(step_identifier)
    if not step_id:
        logger.warning(
            f"[kanban] Step '{step_identifier}' NAO encontrado no funil '{funnel_identifier}' (account_id={account_id}). "
            f"Etapas disponíveis: {list((funil or {}).get('steps', {}).keys())}"
        )
        return

    headers = {"api_access_token": token, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient() as http:
            # Buscar em todas as etapas do funil, PAGINANDO. Chatwoot retorna
            # 25 por página: sem paginar, contas com >25 cards por etapa falham
            # em achar o existente e o POST cria duplicata.
            item_existente = None
            step_atual = None
            for sid_name, sid_val in funil["steps"].items():
                if item_existente:
                    break
                for page in range(1, 21):  # até 500 cards por etapa
                    resp = await http.get(
                        f"{url}/api/v1/accounts/{account_id}/funnels/{funnel_id}/funnel_steps/{sid_val}/funnel_items",
                        headers=headers, params={"page": page}, timeout=10
                    )
                    if not resp.is_success:
                        break
                    data = resp.json()
                    items = data.get("payload") or data.get("items") or []
                    if not isinstance(items, list) or not items:
                        break
                    for item in items:
                        if item.get("conversation_id") == conversation_id:
                            item_existente = item
                            step_atual = sid_val
                            break
                    if item_existente:
                        break
                    if len(items) < 25:
                        break

            if item_existente and step_atual != step_id:
                # Mover card para nova etapa — formato sem wrapper conforme
                # doc oficial advbrasil.ai (chatwoot-kanban-api-curls.txt).
                resp_move = await http.put(
                    f"{url}/api/v1/accounts/{account_id}/funnels/{funnel_id}/funnel_steps/{step_atual}/funnel_items/{item_existente['id']}/update_step",
                    headers=headers,
                    json={"funnel_step_id": step_id},
                    timeout=10
                )
                if resp_move.is_success:
                    logger.info(f"[kanban] Card movido: conv={conversation_id} → {step_identifier} (funil={funnel_identifier})")
                else:
                    logger.warning(
                        f"[kanban] Falha ao MOVER card conv={conversation_id} para {step_identifier}: "
                        f"HTTP {resp_move.status_code} body={resp_move.text[:300]}"
                    )
            elif not item_existente:
                # Criar novo card via bulk_create — único endpoint POST que
                # aceita title + conversation_id e os persiste corretamente
                # (verificado em 2026-04-28 contra swagger.cwmkt.com.br/.../bulk_create).
                # POST direto em /funnel_items retorna 200 mas IGNORA os dados
                # silenciosamente, criando cards orfãos sem title/conversation.
                resp_create = await http.post(
                    f"{url}/api/v1/accounts/{account_id}/funnels/{funnel_id}/funnel_steps/{step_id}/funnel_items/bulk_create",
                    headers=headers,
                    json={
                        "items": [
                            {
                                "title": contact_name or f"Conversa #{conversation_id}",
                                "conversation_id": conversation_id,
                                "priority": "medium",
                            }
                        ]
                    },
                    timeout=10
                )
                if resp_create.is_success:
                    logger.info(f"[kanban] Card criado: conv={conversation_id} → {step_identifier} (funil={funnel_identifier})")
                else:
                    logger.warning(
                        f"[kanban] Falha ao CRIAR card conv={conversation_id} em {step_identifier}: "
                        f"HTTP {resp_create.status_code} body={resp_create.text[:300]}"
                    )
            else:
                logger.debug(f"[kanban] Card ja esta na etapa correta: conv={conversation_id}")

    except Exception as e:
        logger.warning(f"[kanban] Erro ao mover/criar card: {e}")


# ── AGENDA (INTEGRAÇÃO REAL VIA N8N) ──────────────────────────

WEBHOOK_CONSULTAR_AGENDA = "https://flow.advbrasil.ai/webhook/consultar-agenda"
WEBHOOK_AGENDAR = "https://flow.advbrasil.ai/webhook/agendar"


async def confirmar_evento_no_calendar(
    config: dict,
    scheduled_date: str,
    scheduled_time: str,
    advogada: str,
    contact_name: str = "",
) -> bool | None:
    """Verifica se existe evento no Google Calendar batendo com o agendamento.

    Retorna:
        True  → evento localizado (lembrete deve ser enviado)
        False → nenhum evento naquele horário (agendamento sumiu da agenda)
        None  → não foi possível verificar (falha de rede/config) — chamador deve fail-open
    """
    email_agenda = config.get("email_agenda", "")
    if not email_agenda:
        return None

    hhmm = (scheduled_time or "")[:5]
    if not scheduled_date or not hhmm:
        return None

    payload = {
        "email_agenda": email_agenda,
        "horas_inicial_busca": 0,
        "quantidade_dias_a_buscar": 14,
        "duracao_agendamento": 30,
        "disponibilidade": {"0": [], "1": [], "2": [], "3": [], "4": [], "5": [], "6": []},
        "especialidade": "",
    }

    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(WEBHOOK_CONSULTAR_AGENDA, json=payload, timeout=30)
            resp.raise_for_status()
            events = resp.json()
    except Exception as e:
        logger.warning(f"[agenda] Falha ao consultar calendar para confirmar evento: {e}")
        return None

    if not events or (len(events) == 1 and not events[0]):
        events = []

    target_prefix = f"{scheduled_date}T{hhmm}"
    adv_lower = (advogada or "").lower().strip()
    contact_lower = (contact_name or "").lower().strip()

    encontrou_no_horario = False
    for ev in events:
        start_raw = ev.get("start", {})
        start_str = start_raw.get("dateTime", "") if isinstance(start_raw, dict) else ""
        if not start_str or not start_str.startswith(target_prefix):
            continue
        encontrou_no_horario = True
        description = (ev.get("description") or "").lower()
        summary = (ev.get("summary") or "").lower()
        if (contact_lower and contact_lower in summary) or (adv_lower and adv_lower in description):
            return True

    # Achou evento(s) no mesmo horário mas sem match de nome — assume que é o nosso (lenient).
    if encontrou_no_horario:
        return True

    return False


async def consultar_agenda_real(config: dict, especialidade: str = "") -> list:
    """Consulta horários disponíveis: busca eventos do Google Calendar via n8n e calcula slots livres."""
    account_id = config["account_id"]
    email_agenda = config.get("email_agenda", "")
    if not especialidade:
        especialidade = config.get("especialidade", "")
    qtd_dias = config.get("quantidade_dias_a_buscar") or 14

    if not email_agenda:
        logger.warning(f"[agenda] email_agenda vazio para account_id={account_id}")
        return []

    # Buscar advogados ativos da especialidade
    advogados = listar_advogados_por_especialidade(account_id, especialidade)
    if not advogados and especialidade:
        # Fallback: buscar todos os advogados ativos da conta (sem filtro de especialidade)
        logger.info(f"[agenda] Nenhum advogado para '{especialidade}', tentando sem filtro (account_id={account_id})")
        advogados = listar_advogados_por_especialidade(account_id, "")
    if not advogados:
        logger.warning(f"[agenda] Nenhum advogado ativo para account_id={account_id}")
        return []

    # Payload para n8n — pedir eventos brutos do Google Calendar
    payload = {
        "email_agenda": email_agenda,
        "horas_inicial_busca": 0,
        "quantidade_dias_a_buscar": qtd_dias,
        "duracao_agendamento": 30,
        "disponibilidade": {"0":[],"1":[],"2":[],"3":[],"4":[],"5":[],"6":[]},
        "especialidade": especialidade,
    }

    logger.info(f"[agenda] ConsultarAgenda → POST {WEBHOOK_CONSULTAR_AGENDA} (advogados: {[a['nome'] for a in advogados]})")

    async with httpx.AsyncClient() as http:
        resp = await http.post(WEBHOOK_CONSULTAR_AGENDA, json=payload, timeout=30)
        resp.raise_for_status()
        events = resp.json()

    # events é uma lista de eventos do Google Calendar
    if not events or (len(events) == 1 and not events[0]):
        events = []

    logger.info(f"[agenda] {len(events)} eventos recebidos do Google Calendar")

    # Calcular slots livres para cada advogado
    return _calcular_slots_disponiveis(advogados, events, qtd_dias, account_id)


def _calcular_slots_disponiveis(advogados: list, events: list, qtd_dias: int, account_id: int = 0) -> list:
    """Calcula slots disponíveis subtraindo eventos ocupados da disponibilidade de cada advogado."""
    BR_TZ = timezone(timedelta(hours=-3))
    agora = datetime.now(BR_TZ)

    # Carregar bloqueios de agenda do banco
    bloqueios_por_adv: dict[str, list] = {}  # advogado_id → lista de {start, end}
    bloqueios_todos: list = []  # bloqueiam todos os advogados
    if account_id:
        try:
            from db import listar_bloqueios_agenda_ativos
            bloqueios = listar_bloqueios_agenda_ativos(account_id)
            for b in bloqueios:
                try:
                    b_start = datetime.fromisoformat(b["data_inicio"])
                    b_end = datetime.fromisoformat(b["data_fim"])
                    if b_start.tzinfo is None:
                        b_start = b_start.replace(tzinfo=BR_TZ)
                    if b_end.tzinfo is None:
                        b_end = b_end.replace(tzinfo=BR_TZ)
                    evento_bloq = {"start": b_start, "end": b_end}
                    adv_id = b.get("advogado_id")
                    if adv_id:
                        bloqueios_por_adv.setdefault(adv_id, []).append(evento_bloq)
                    else:
                        bloqueios_todos.append(evento_bloq)
                except Exception:
                    continue
            if bloqueios:
                logger.info(f"[agenda] {len(bloqueios)} bloqueio(s) de agenda carregados")
        except Exception as e:
            logger.warning(f"[agenda] Erro ao carregar bloqueios: {e}")

    # Mapear advogados: nome → chave interna, cor → chave interna
    # Cada advogado tem uma chave única baseada no nome
    nomes_advogados = [adv["nome"].lower() for adv in advogados]
    cores_advogados = {str(adv.get("cor_id", 0)): adv["nome"].lower() for adv in advogados if adv.get("cor_id", 0) != 0}

    # Separar eventos por advogado (pelo nome) e eventos bloqueantes
    eventos_por_adv: dict[str, list] = {adv["nome"].lower(): [] for adv in advogados}
    eventos_bloqueantes: list = []  # bloqueiam todos os advogados

    for ev in events:
        start_raw = ev.get("start", {})
        end_raw = ev.get("end", {})
        start_str = start_raw.get("dateTime", "") if isinstance(start_raw, dict) else ""
        end_str = end_raw.get("dateTime", "") if isinstance(end_raw, dict) else ""
        if not start_str or not end_str:
            continue

        ev_start = datetime.fromisoformat(start_str)
        ev_end = datetime.fromisoformat(end_str)
        color_id = str(ev.get("colorId", ""))
        description = ev.get("description") or ""
        summary = ev.get("summary") or ""
        texto_busca = f"{description} {summary}".lower()

        evento = {"start": ev_start, "end": ev_end}

        # 1. Tentar identificar por nome na descrição/summary
        adv_encontrado = False
        for nome_adv in nomes_advogados:
            if nome_adv in texto_busca:
                eventos_por_adv[nome_adv].append(evento)
                adv_encontrado = True
                break

        if adv_encontrado:
            continue

        # 2. Tentar identificar por cor (apenas se cor != 0 e cor pertence a um advogado)
        if color_id and color_id in cores_advogados:
            nome_adv = cores_advogados[color_id]
            eventos_por_adv[nome_adv].append(evento)
            continue

        # 3. Sem identificação por nome nem cor de advogado nosso
        if color_id:
            # Tem cor de outra pessoa (não é advogado nosso) → ignorar
            # É evento de outro profissional da mesma agenda
            continue
        elif not description.strip():
            # Sem cor + sem descrição → bloqueia todos por segurança
            eventos_bloqueantes.append(evento)
        else:
            # Sem cor + com descrição mas sem nome de advogado nosso → bloqueia todos
            eventos_bloqueantes.append(evento)

    logger.info(f"[agenda] Bloqueantes: {len(eventos_bloqueantes)} | Por adv: {{{', '.join(f'{k}:{len(v)}' for k,v in eventos_por_adv.items())}}}")

    DIAS_SEMANA = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
    resultado = []

    for adv in advogados:
        nome_key = adv["nome"].lower()
        duracao = adv.get("duracao_agendamento", 30)
        horas_min = adv.get("horas_inicial_busca", 0)
        disp_raw = adv.get("disponibilidade", {})
        if isinstance(disp_raw, str):
            disp_raw = json.loads(disp_raw)

        # Eventos que bloqueiam este advogado: os dele (por nome/cor) + os bloqueantes gerais + bloqueios de agenda
        # Adicionar bloqueios do banco: específicos deste advogado + bloqueios para todos
        adv_id = str(adv.get("id", ""))
        bloqueios_adv = bloqueios_por_adv.get(adv_id, []) + bloqueios_todos
        eventos_adv = eventos_por_adv.get(nome_key, []) + eventos_bloqueantes + bloqueios_adv

        horarios = []
        for dia_offset in range(qtd_dias):
            dia = agora.date() + timedelta(days=dia_offset)
            dia_semana = dia.isoweekday() % 7  # 0=domingo, 1=segunda...

            # Buscar disponibilidade para este dia da semana
            faixas = disp_raw.get(str(dia_semana), [])
            if not faixas:
                continue

            for faixa in faixas:
                faixa_start = datetime.combine(dia, datetime.strptime(faixa["start"], "%H:%M").time(), tzinfo=BR_TZ)
                faixa_end = datetime.combine(dia, datetime.strptime(faixa["end"], "%H:%M").time(), tzinfo=BR_TZ)

                # Gerar slots dentro da faixa
                slot_start = faixa_start
                while slot_start + timedelta(minutes=duracao) <= faixa_end:
                    slot_end = slot_start + timedelta(minutes=duracao)

                    # Verificar antecedência mínima
                    if slot_start < agora + timedelta(hours=horas_min):
                        slot_start = slot_end
                        continue

                    # Verificar conflito com eventos ocupados
                    conflito = False
                    for ev in eventos_adv:
                        if slot_start < ev["end"] and slot_end > ev["start"]:
                            conflito = True
                            break

                    if not conflito:
                        horarios.append({
                            "data": dia.strftime("%Y-%m-%d"),
                            "dia_semana": DIAS_SEMANA[dia.weekday()],
                            "inicio": slot_start.strftime("%H:%M"),
                            "fim": slot_end.strftime("%H:%M"),
                        })

                    slot_start = slot_end

        resultado.append({
            "advogado": adv["nome"],
            "cor_id": adv.get("cor_id", 0),
            "horarios": horarios,
        })
        logger.info(f"[agenda] {adv['nome']} (cor={adv.get('cor_id', 0)}): {len(horarios)} slots livres")

    return resultado


async def agendar_real(args: dict, config: dict, context: dict) -> dict:
    """Confirma agendamento via webhook n8n → Google Calendar."""
    email_agenda = config.get("email_agenda", "")
    especialidade = args.get("especialidade", config.get("especialidade", ""))
    contact_name = context.get("contact_name", "")
    contact_phone = context.get("contact_phone", "")

    start = args.get("start", "")
    end = args.get("end", "")
    advogado = args.get("advogado", "")
    cor_id = args.get("cor_id", 0)
    resumo = args.get("resumo", "")

    # Validar que o advogado existe na lista cadastrada — evita alucinação de nome.
    account_id = config.get("account_id")
    if account_id and advogado:
        advs_ativos = listar_advogados_por_especialidade(account_id, especialidade)
        if not advs_ativos:
            advs_ativos = listar_advogados_por_especialidade(account_id, "")
        nomes_validos = [a.get("nome", "") for a in advs_ativos]
        nomes_validos_lower = [n.lower().strip() for n in nomes_validos]
        if advogado.lower().strip() not in nomes_validos_lower:
            logger.warning(
                f"🚫 [agenda] Advogado '{advogado}' NAO esta cadastrado para account_id={account_id}. "
                f"Ativos: {nomes_validos}. Agendamento REJEITADO."
            )
            return {
                "STATUS": "ERRO",
                "mensagem_sistema": (
                    f"Advogado '{advogado}' nao esta cadastrado. Escolha apenas entre os advogados "
                    f"retornados pelo ConsultarAgenda. Ativos: {', '.join(nomes_validos) or 'nenhum'}."
                ),
                "advogado": advogado,
            }

    # Se recebeu data+horario no formato antigo, converter para start/end
    if not start and args.get("data") and args.get("horario"):
        data_str = args["data"]
        horario_str = args["horario"]
        # Tentar converter dd/mm/yyyy para yyyy-mm-dd
        try:
            dt = datetime.strptime(f"{data_str} {horario_str}", "%d/%m/%Y %H:%M")
            start = dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            start = f"{data_str} {horario_str}"

        # Calcular end baseado na duração do advogado
        account_id = config["account_id"]
        advogados = listar_advogados_por_especialidade(account_id, especialidade)
        duracao = 30  # default
        for adv in advogados:
            if adv["nome"].lower() == advogado.lower():
                duracao = adv.get("duracao_agendamento", 30)
                cor_id = cor_id or adv.get("cor_id", 0)
                break
        try:
            dt_start = datetime.strptime(start, "%Y-%m-%d %H:%M")
            dt_end = dt_start + timedelta(minutes=duracao)
            end = dt_end.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            end = start

    dh = datetime.now(timezone(timedelta(hours=-3)))
    horario_exec = dh.strftime("%d/%m %H:%Mh")

    payload = {
        "email_agenda": email_agenda,
        "Start": start,
        "End": end,
        "Color Name or ID": str(cor_id),
        "Summary": contact_name,
        "Description": (
            f"- Agendamento por IA ADV Brasil\n"
            f"especialidade: {especialidade}\n"
            f"Especialista: {advogado}\n"
            f"assunto: {resumo}\n"
            f"Horario execução agendamento: {horario_exec}\n\n"
            f"Telefone cliente: {contact_phone}"
        ),
        "numero": contact_phone,
    }

    logger.info(f"[agenda] Agendar → POST {WEBHOOK_AGENDAR} ({start} - {end}, {advogado})")

    async with httpx.AsyncClient() as http:
        resp = await http.post(WEBHOOK_AGENDAR, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()

    # Resposta pode vir como array [{}] ou objeto {}
    if isinstance(data, list) and data:
        data = data[0]

    logger.info(f"[agenda] Agendar resposta: {data}")

    # Normalizar resposta para o formato esperado pela IA
    msg = data.get("mensagem_sistema", "")
    if "SUCESSO" in msg.upper() and "ERRO" not in msg.upper():
        return {"STATUS": "SUCESSO", "mensagem_sistema": msg, "advogado": advogado}
    elif "JA_AGENDADO" in msg.upper():
        return {"STATUS": "JA_AGENDADO", "mensagem_sistema": msg, "advogado": advogado}
    elif "OCUPADO" in msg.upper() or "CONFLITO" in msg.upper():
        return {"STATUS": "ERRO_OCUPADO", "mensagem_sistema": msg}
    else:
        return {"STATUS": data.get("status", "ERRO"), "mensagem_sistema": msg}


# ── UTILIDADES ────────────────────────────────────────────────

def data_hora_atual() -> str:
    tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).strftime("%d/%m/%Y - %H:%M")


def pasta_cliente(account_id: int) -> str | None:
    for pasta in os.listdir(CLIENTES_DIR):
        if pasta.startswith(f"{account_id}-"):
            return os.path.join(CLIENTES_DIR, pasta)
    return None


def carregar_prompt(account_id: int, nome_arquivo: str) -> str:
    pasta = pasta_cliente(account_id)
    base_path = os.path.join(pasta, "prompt", "base.md")
    prompt_path = os.path.join(pasta, "prompt", nome_arquivo)

    base = ""
    if os.path.exists(base_path):
        with open(base_path, encoding="utf-8") as f:
            base = f.read()

    with open(prompt_path, encoding="utf-8") as f:
        conteudo = f.read()

    # Remove linhas de referência ao base.md (não devem ir para a IA)
    conteudo = "\n".join(
        linha for linha in conteudo.splitlines()
        if not linha.strip().startswith("> Regras de estilo") and
           not linha.strip().startswith("> ver base.md")
    )

    return f"{base}\n\n---\n\n{conteudo}"


def _filtrar_apos_reset(messages: list) -> list:
    """Descarta todas as mensagens antes do último #reset. Usado para testes."""
    ultimo_reset = -1
    for i, msg in enumerate(messages):
        content = (msg.get("content") or "").strip().lower()
        if content == "#reset" and msg.get("message_type") == 0:
            ultimo_reset = i
    if ultimo_reset >= 0:
        return messages[ultimo_reset + 1:]
    return messages


def formatar_conversa_texto(messages: list) -> str:
    """Formata o histórico para o supervisor (texto simples)."""
    # Mapear transcrições de áudio: created_at da nota → texto da transcrição
    transcricoes = {}
    for msg in messages:
        if msg.get("private") and (msg.get("content") or "").startswith("🎙️ Transcrição"):
            texto = (msg.get("content") or "").split("\n\n", 1)
            if len(texto) > 1:
                transcricoes[msg.get("created_at", 0)] = texto[1].strip()

    linhas = []
    for msg in messages:
        if msg.get("private"):
            continue
        tipo = msg.get("message_type")
        sender = msg.get("sender", {})
        nome = sender.get("name", "Desconhecido")
        content = msg.get("content")
        attachments = msg.get("attachments", [])

        is_audio = not content and any(a.get("file_type") == "audio" for a in attachments)
        if is_audio:
            msg_time = msg.get("created_at", 0)
            content = next(
                (t for ts, t in sorted(transcricoes.items()) if ts >= msg_time),
                "[áudio sem transcrição]"
            )

        content = content or "[mídia]"
        prefixo = "[Cliente]" if tipo == 0 else f"[{nome}]"
        linhas.append(f"{prefixo} {nome}: {content}")

    return "\n".join(linhas)


def formatar_conversa_openai(messages: list) -> list:
    """Formata o histórico para o agente (formato OpenAI messages)."""
    # Mapear transcrições
    transcricoes = {}
    for msg in messages:
        if msg.get("private") and (msg.get("content") or "").startswith("🎙️ Transcrição"):
            texto = (msg.get("content") or "").split("\n\n", 1)
            if len(texto) > 1:
                transcricoes[msg.get("created_at", 0)] = texto[1].strip()

    resultado = []
    for msg in messages:
        if msg.get("private"):
            continue
        tipo = msg.get("message_type")
        content = msg.get("content")
        attachments = msg.get("attachments", [])

        is_audio = not content and any(a.get("file_type") == "audio" for a in attachments)
        if is_audio:
            msg_time = msg.get("created_at", 0)
            content = next(
                (t for ts, t in sorted(transcricoes.items()) if ts >= msg_time),
                "[áudio sem transcrição]"
            )

        content = content or "[mídia]"

        if tipo == 0:
            resultado.append({"role": "user", "content": content})
        elif tipo == 1:
            resultado.append({"role": "assistant", "content": content})

    return resultado


def dividir_mensagem(texto: str, limite: int = 600) -> list[str]:
    """Divide a resposta em partes de até `limite` caracteres."""
    partes = []
    atual = ""

    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha:
            continue

        candidato = f"{atual}\n{linha}".strip() if atual else linha
        if len(candidato) <= limite:
            atual = candidato
        else:
            if atual:
                partes.append(atual)
            if len(linha) > limite:
                palavras = linha.split()
                atual = ""
                for palavra in palavras:
                    teste = f"{atual} {palavra}".strip() if atual else palavra
                    if len(teste) <= limite:
                        atual = teste
                    else:
                        if atual:
                            partes.append(atual)
                        atual = palavra
            else:
                atual = linha

    if atual:
        if partes and len(partes[-1]) + 1 + len(atual) <= limite:
            partes[-1] = f"{partes[-1]} {atual}"
        else:
            partes.append(atual)

    return partes


# ── CHATWOOT: BUSCAR HISTÓRICO E ENVIAR ───────────────────────

async def buscar_historico_chatwoot(chatwoot_url: str, chatwoot_token: str, account_id: int, conversation_id: int) -> list:
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": chatwoot_token}
    todas_msgs = []
    before = None

    async with httpx.AsyncClient() as http:
        for _ in range(10):  # máximo 10 páginas (~200 mensagens)
            params = {}
            if before:
                params["before"] = before
            resp = await http.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            msgs = resp.json().get("payload", [])
            if not msgs:
                break
            todas_msgs.extend(msgs)
            # Se retornou menos que o padrão (20), não há mais páginas
            if len(msgs) < 20:
                break
            # Próxima página: buscar mensagens antes da mais antiga desta página
            mais_antiga = min(m.get("id", 0) for m in msgs)
            before = mais_antiga

    # Deduplicar por ID (páginas podem sobrepor)
    vistos = set()
    unicas = []
    for m in todas_msgs:
        mid = m.get("id")
        if mid not in vistos:
            vistos.add(mid)
            unicas.append(m)

    return sorted(unicas, key=lambda m: m.get("created_at", 0))


async def enviar_parte_chatwoot(chatwoot_url: str, chatwoot_token: str, account_id: int, conversation_id: int, texto: str):
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": chatwoot_token, "Content-Type": "application/json"}
    async with httpx.AsyncClient() as http:
        resp = await http.post(url, headers=headers, json={"content": texto, "message_type": "outgoing", "private": False}, timeout=15)
        resp.raise_for_status()


async def enviar_nota_privada(chatwoot_url: str, chatwoot_token: str, account_id: int, conversation_id: int, texto: str):
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": chatwoot_token, "Content-Type": "application/json"}
    async with httpx.AsyncClient() as http:
        resp = await http.post(url, headers=headers, json={"content": texto, "message_type": "outgoing", "private": True}, timeout=15)
        resp.raise_for_status()


# Cache de últimas respostas enviadas por conversa (anti-duplicação)
_ultimas_respostas: dict[int, str] = {}
_ULTIMAS_RESPOSTAS_MAX = 500

async def enviar_resposta_chatwoot(chatwoot_url: str, chatwoot_token: str, account_id: int, conversation_id: int, texto: str, inbox_id: int | None = None, inatividade_ativa: bool = True):
    # Limpar \n literal que a IA às vezes gera como texto
    texto = texto.replace("\\n", "\n")

    # Anti-duplicação: se a última resposta enviada nesta conversa é idêntica, não reenviar
    texto_limpo = texto.strip()
    if conversation_id in _ultimas_respostas and _ultimas_respostas[conversation_id] == texto_limpo:
        logger.warning(f"[anti-dup] Resposta idêntica à anterior na conv={conversation_id} — ignorando envio duplicado")
        return
    _ultimas_respostas[conversation_id] = texto_limpo
    if len(_ultimas_respostas) > _ULTIMAS_RESPOSTAS_MAX:
        oldest = next(iter(_ultimas_respostas))
        del _ultimas_respostas[oldest]

    partes = dividir_mensagem(texto)
    logger.info(f"Enviando {len(partes)} parte(s) na conversa {conversation_id}")
    for i, parte in enumerate(partes):
        await enviar_parte_chatwoot(chatwoot_url, chatwoot_token, account_id, conversation_id, parte)
        if i < len(partes) - 1:
            await asyncio.sleep(0.5)

    # Resetar inatividade (IA respondeu — timer recomeça do estágio 1)
    if inatividade_ativa:
        try:
            from inatividade import registrar_atividade
            registrar_atividade(account_id, conversation_id, inbox_id)
        except Exception as e:
            logger.warning(f"Erro ao resetar inatividade após resposta IA: {e}")



# ── OPENAI: SUPERVISOR E AGENTE COM TOOLS ─────────────────────

def chamar_supervisor(config: dict, historico_texto: str) -> str:
    prompt = carregar_prompt(config["account_id"], "supervisor.md")
    prompt = (
        prompt
        .replace("{data_hora_atual}", data_hora_atual())
        .replace("{conversa}", historico_texto)
    )
    client = OpenAI(api_key=config["openai_api_key"])
    logger.info(f"🧠 Supervisor: enviando prompt ({len(prompt)} chars) ao gpt-5.2")
    resp = client.chat.completions.create(
        model="gpt-5.2",
        messages=[{"role": "user", "content": prompt}],
        reasoning_effort="low",
        response_format={"type": "json_object"},
    )
    raw_response = resp.choices[0].message.content.strip()
    logger.info(f"🧠 Supervisor resposta bruta: {raw_response}")
    data = json.loads(raw_response)
    fase = data.get("proxima_fase", "identificacao")
    logger.info(f"🧠 Supervisor → fase decidida: {fase}")
    return fase


def filtrar_tools_por_fase(fase: str) -> list:
    """Retorna apenas as tools permitidas para a fase atual (WAT Architecture)."""
    nomes_permitidos = TOOLS_POR_FASE.get(fase, [])
    if not nomes_permitidos:
        return []
    return [t for t in TOOLS if t["function"]["name"] in nomes_permitidos]


async def chamar_agente(config: dict, fase: str, messages_openai: list, conversation_id: int, context: dict) -> str | None:
    """Chama o agente com suporte a function calling em loop."""
    arquivo = f"{fase}.md"
    prompt = carregar_prompt(config["account_id"], arquivo)
    prompt = prompt.replace("{data_hora_atual}", data_hora_atual())

    client = OpenAI(api_key=config["openai_api_key"])
    msgs = [{"role": "system", "content": prompt}, *messages_openai]

    # Filtrar tools pela fase atual (reduz alucinação)
    tools_fase = filtrar_tools_por_fase(fase)
    tools_param = tools_fase if tools_fase else None
    tool_choice_param = "auto" if tools_fase else None

    logger.info(f"🤖 Agente [{fase}]: prompt ({len(prompt)} chars) + {len(messages_openai)} msgs | tools: {[t['function']['name'] for t in (tools_fase or [])]}")

    # Loop de tool calling
    for rodada in range(5):  # máximo 5 rodadas para evitar loop infinito
        logger.info(f"🤖 Agente [{fase}]: chamando gpt-5.2 (rodada {rodada+1})")
        call_kwargs = {
            "model": "gpt-5.2",
            "messages": msgs,
            "reasoning_effort": "low",
        }
        if tools_param:
            call_kwargs["tools"] = tools_param
            call_kwargs["tool_choice"] = tool_choice_param
        resp = client.chat.completions.create(**call_kwargs)
        choice = resp.choices[0]
        msg = choice.message

        # Sem tool call → resposta final
        if not msg.tool_calls:
            resposta = (msg.content or "").strip()
            logger.info(f"🤖 Agente [{fase}] → resposta final ({len(resposta)} chars):")
            # Log da resposta completa (dividida em linhas para legibilidade)
            for linha in resposta.splitlines():
                logger.info(f"   💬 {linha}")
            return resposta

        # Executar cada tool chamada
        msgs.append(msg)
        for tc in msg.tool_calls:
            nome = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            logger.info(f"Tool chamada: {nome}({args})")
            resultado = await executar_tool(nome, args, config, conversation_id, context)
            msgs.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": resultado,
            })

    # Loop esgotou sem gerar texto — forçar resposta final sem tools
    logger.warning(f"🤖 Agente [{fase}]: loop de 5 rodadas esgotado sem resposta textual — forçando resposta final")
    msgs.append({
        "role": "user",
        "content": "Responda agora com uma mensagem de texto para o cliente. Não acione nenhuma ferramenta.",
    })
    try:
        resp_final = client.chat.completions.create(
            model="gpt-5.2",
            messages=msgs,
            reasoning_effort="low",
        )
        resposta = (resp_final.choices[0].message.content or "").strip()
        if resposta:
            logger.info(f"🤖 Agente [{fase}] → resposta de fallback ({len(resposta)} chars)")
            return resposta
    except Exception as e_fallback:
        logger.error(f"🤖 Agente [{fase}]: erro no fallback final: {e_fallback}")
    return None  # esgotou todas as tentativas


# ── DEBOUNCE ──────────────────────────────────────────────────

async def _executar_com_debounce(config: dict, account_id: int, conversation_id: int, inbox_id: int | None):
    await asyncio.sleep(15)
    _debounce_tasks.pop(conversation_id, None)
    lock = _processing_locks.setdefault(conversation_id, asyncio.Lock())
    if lock.locked():
        logger.info(f"[debounce] Já há processamento em andamento para conv={conversation_id} — pulando esta task (a próxima mensagem reagendará)")
        return
    async with lock:
        logger.info(f"[debounce] Processando conversa {conversation_id} (account={account_id})")
        try:
            await processar_mensagem(config, account_id, conversation_id, inbox_id)
        except Exception as e:
            logger.error(f"[debounce] ERRO FATAL ao processar conv={conversation_id} account={account_id}: {e}", exc_info=True)
    # Limpar locks antigos para não vazar memória (mantém só conversas recentes)
    if len(_processing_locks) > 2000:
        for cid in list(_processing_locks.keys())[:500]:
            if not _processing_locks[cid].locked():
                _processing_locks.pop(cid, None)


def agendar_processamento(config: dict, account_id: int, conversation_id: int, inbox_id: int | None = None):
    task_existente = _debounce_tasks.get(conversation_id)
    if task_existente:
        task_existente.cancel()
        logger.info(f"[debounce] Timer resetado para conversa {conversation_id}")
    task = asyncio.create_task(_executar_com_debounce(config, account_id, conversation_id, inbox_id))
    _debounce_tasks[conversation_id] = task


# ── FLUXO PRINCIPAL ───────────────────────────────────────────

async def processar_mensagem(config: dict, account_id: int, conversation_id: int, inbox_id: int | None = None):
    logger.info(f"═══ PROCESSANDO [{account_id}] conv={conversation_id} ═══")


    historico = await buscar_historico_chatwoot(
        chatwoot_url=config["chatwoot_url"],
        chatwoot_token=config["chatwoot_token"],
        account_id=account_id,
        conversation_id=conversation_id,
    )
    logger.info(f"📜 Histórico: {len(historico)} mensagens carregadas")

    # Conta 18: re-cliques no anúncio inserem o payload "Mensagem de Anúncio: ..." no
    # meio da conversa, confundindo a IA. Mantém apenas a primeira ocorrência.
    if account_id == 18:
        primeiro_visto = False
        filtrado = []
        descartados = 0
        for m in historico:
            if m.get("message_type") == 0 and _eh_payload_anuncio(m.get("content") or ""):
                if primeiro_visto:
                    descartados += 1
                    continue
                primeiro_visto = True
            filtrado.append(m)
        if descartados:
            logger.info(f"[conta18] Removidas {descartados} mensagens de anúncio duplicadas do histórico (conv={conversation_id})")
        historico = filtrado

    historico_texto = formatar_conversa_texto(historico)
    historico_openai = formatar_conversa_openai(historico)

    # Log das últimas mensagens do histórico para contexto
    linhas = historico_texto.strip().splitlines()
    ultimas = linhas[-5:] if len(linhas) > 5 else linhas
    logger.info(f"📜 Últimas mensagens:\n" + "\n".join(f"   {l}" for l in ultimas))

    fase = chamar_supervisor(config, historico_texto)

    # Se já convertido e supervisor quer agendamento → tratar como reagendamento
    _is_reagendamento = False
    if fase == "agendamento":
        try:
            chatwoot_url = config["chatwoot_url"].rstrip("/")
            labels_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"
            async with httpx.AsyncClient() as http:
                resp = await http.get(labels_url, headers={"api_access_token": config["chatwoot_token"]}, timeout=10)
                labels = resp.json().get("payload", []) if resp.is_success else []
            if "convertido" in labels:
                _is_reagendamento = True
                logger.info(f"🔄 Conversa já convertida — tratando como reagendamento (conv={conversation_id})")
        except Exception as e:
            logger.warning(f"Erro ao verificar labels: {e}")

    # Extrair dados do contato do histórico
    contact_name = ""
    contact_phone = ""
    for msg in historico:
        if msg.get("message_type") == 0:
            sender = msg.get("sender", {})
            contact_name = sender.get("name", "")
            contact_phone = sender.get("phone_number", "")
            break

    # Proteção anti-desatribuição prematura: se supervisor quer transferir_humano
    # mas o cliente teve poucas interações, forçar qualificação primeiro
    if fase == "transferir_humano":
        msgs_cliente = [m for m in historico if m.get("message_type") == 0]
        msgs_ia = [m for m in historico if m.get("message_type") == 1]
        # Se o cliente mandou <= 3 mensagens e a IA mandou <= 2, é muito cedo pra transferir
        if len(msgs_cliente) <= 3 and len(msgs_ia) <= 2:
            logger.warning(f"⚠️ Supervisor quis transferir_humano com apenas {len(msgs_cliente)} msgs do cliente e {len(msgs_ia)} da IA. Forçando coleta_caso.")
            fase = "coleta_caso"

    if fase == "transferir_humano":
        logger.info("Supervisor → transferir_humano")
        context = {
            "inbox_id": inbox_id,
            "contact_name": contact_name,
            "contact_phone": contact_phone,
        }
        try:
            resposta = await chamar_agente(config, "transferir_humano", historico_openai, conversation_id, context)
            if resposta:
                await enviar_resposta_chatwoot(
                    chatwoot_url=config["chatwoot_url"],
                    chatwoot_token=config["chatwoot_token"],
                    account_id=account_id,
                    conversation_id=conversation_id,
                    texto=resposta,
                    inbox_id=inbox_id,
                    inatividade_ativa=False,
                )
                logger.info(f"✅ Mensagem de transferência enviada — conv={conversation_id}")
        except Exception as e:
            logger.warning(f"⚠️ Erro ao gerar mensagem de transferência — conv={conversation_id}: {e}")
        await chatwoot_transferir_humano(
            config["chatwoot_url"], config["chatwoot_token"], account_id, conversation_id,
            motivo="supervisor:transferir_humano"
        )
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone, status="transferido")
        except Exception as e:
            logger.warning(f"Supabase erro (transferir_humano supervisor): {e}")
        # Kanban: mover para Transferido
        try:
            await kanban_mover_card(config["chatwoot_url"], config["chatwoot_token"], account_id, conversation_id, contact_name, "TransferHuman")
        except Exception:
            pass
        # Conta 8: notificar grupo "clientes existentes" (conv 77) quando for trabalhista
        if account_id == 8:
            palavras_trab = ["trabalhista", "demissao", "demissão", "rescisao", "rescisão", "verbas", "desvio de funcao", "desvio de função", "assedio", "assédio", "insalubridade", "horas extras", "carteira não assinada", "carteira nao assinada"]
            hist_lower = historico_texto.lower()
            if any(p in hist_lower for p in palavras_trab):
                try:
                    msg_notif = (
                        f"⚖️ LEAD TRABALHISTA (fora do escopo da IA)\n\n"
                        f"Nome: {contact_name}\n"
                        f"Número: {contact_phone}\n"
                        f"Conversa: {conversation_id}"
                    )
                    await _enviar_notificacao(config, account_id, 77, msg_notif)
                    logger.info(f"[notificação] Lead trabalhista conta 8 notificado no grupo 77")
                except Exception as e:
                    logger.warning(f"[notificação] Erro ao notificar lead trabalhista conta 8: {e}")
        # Notificar transferência para especialista (contas habilitadas)
        try:
            resumo_transf = _gerar_resumo_caso(historico_texto, config.get("openai_api_key"))
        except Exception:
            resumo_transf = ""
        await _notificar_transferencia_humano(config, account_id, conversation_id, contact_name, contact_phone, "Transferência pelo supervisor", resumo_transf)
        return

    context = {
        "inbox_id": inbox_id,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
        "historico_texto": historico_texto,
        "historico": historico,
        "is_reagendamento": _is_reagendamento,
    }

    # Kanban: criar card "Novo Lead" ou mover para "Em Qualificação"
    try:
        kanban_fase = "novo_lead" if fase == "identificacao" else "em_qualificacao"
        await kanban_mover_card(config["chatwoot_url"], config["chatwoot_token"], account_id, conversation_id, contact_name, kanban_fase)
    except Exception:
        pass

    # Persistir fase atual no Supabase
    try:
        upsert_conversation(account_id, inbox_id, conversation_id, contact_name, contact_phone, fase)
    except Exception as e:
        logger.warning(f"Supabase erro (upsert_conversation): {e}")

    resposta = await chamar_agente(config, fase, historico_openai, conversation_id, context)

    if resposta:
        await enviar_resposta_chatwoot(
            chatwoot_url=config["chatwoot_url"],
            chatwoot_token=config["chatwoot_token"],
            account_id=account_id,
            conversation_id=conversation_id,
            texto=resposta,
            inbox_id=inbox_id,
            inatividade_ativa=config.get("inatividade_ativa", True),
        )
        logger.info(f"✅ Resposta enviada com sucesso — conv={conversation_id}")
    else:
        logger.warning(f"⚠️ Agente não retornou resposta — conv={conversation_id}")


# ── TRANSCRIÇÃO DE ÁUDIO ──────────────────────────────────────

async def transcrever_audio(url_audio: str, openai_api_key: str) -> str:
    async with httpx.AsyncClient() as http:
        resp = await http.get(url_audio, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        audio_bytes = resp.content

    client = OpenAI(api_key=openai_api_key)
    transcricao = client.audio.transcriptions.create(
        model="whisper-1",
        file=("audio.oga", audio_bytes, "audio/ogg"),
    )
    return transcricao.text
