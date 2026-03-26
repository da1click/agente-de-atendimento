from openai import OpenAI
from datetime import datetime, timezone, timedelta
from db import upsert_conversation, upsert_lead, inserir_agendamento, listar_advogados_por_especialidade, normalizar_especialidade
import asyncio
import httpx
import json
import logging
import os

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENTES_DIR = os.path.join(BASE_DIR, "clientes")

# Debounce: conversation_id -> asyncio.Task
_debounce_tasks: dict[int, asyncio.Task] = {}

# ── MAPEAMENTO DE TOOLS POR FASE (WAT Architecture) ─────────

TOOLS_POR_FASE = {
    "identificacao": ["atualiza_contato"],
    "vinculo": ["cliente_inviavel", "TransferHuman"],
    "coleta_caso": ["cliente_inviavel", "TransferHuman"],
    "avaliacao": ["cliente_inviavel", "TransferHuman"],
    "casos_especiais": ["TransferHuman", "cliente_inviavel", "desqualificado", "nao_lead", "nao_alfabetizado"],
    "explicacao": ["TransferHuman"],
    "agendamento": ["ConsultarAgenda", "Agendar", "convertido"],
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
}


async def _enviar_notificacao(config: dict, account_id: int, conv_id_notif: int, mensagem: str):
    """Envia notificação para o grupo do Chatwoot (pode ser outro Chatwoot)."""
    externo = _NOTIF_CHATWOOT_EXTERNO.get(account_id)
    if externo:
        notif_url = externo["url"]
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
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id)
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone,
                        status="inviavel", inviability_reason=args.get("motivo"))
        except Exception as e:
            logger.warning(f"Supabase erro (cliente_inviavel): {e}")
        logger.info(f"Tool: cliente_inviavel — {args.get('motivo')}")
        return json.dumps({"status": "ok"})

    if nome == "TransferHuman":
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id)
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone, status="transferido")
        except Exception as e:
            logger.warning(f"Supabase erro (TransferHuman): {e}")
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
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id)
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone, status="transferido")
        except Exception as e:
            logger.warning(f"Supabase erro (lead_disponivel): {e}")
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
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id)
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone,
                        status="desqualificado", inviability_reason=args.get("motivo"))
        except Exception as e:
            logger.warning(f"Supabase erro (desqualificado): {e}")
        logger.info(f"Tool: desqualificado — {args.get('motivo')}")
        return json.dumps({"status": "ok"})

    if nome == "nao_lead":
        await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "nao-lead")
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id)
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone,
                        status="desqualificado", inviability_reason=args.get("motivo"))
        except Exception as e:
            logger.warning(f"Supabase erro (nao_lead): {e}")
        logger.info(f"Tool: nao_lead — {args.get('motivo')}")
        return json.dumps({"status": "ok"})

    if nome == "nao_alfabetizado":
        await chatwoot_adicionar_label(chatwoot_url, chatwoot_token, account_id, conversation_id, "nao-alfabetizado")
        await chatwoot_transferir_humano(chatwoot_url, chatwoot_token, account_id, conversation_id)
        try:
            upsert_lead(account_id, inbox_id, conversation_id, contact_name, contact_phone, status="transferido")
        except Exception as e:
            logger.warning(f"Supabase erro (nao_alfabetizado): {e}")
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


async def chatwoot_transferir_humano(url: str, token: str, account_id: int, conversation_id: int):
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    assign_url = f"{url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/assignments"
    async with httpx.AsyncClient() as http:
        await http.post(assign_url, headers=headers, json={"assignee_id": None}, timeout=10)
    logger.info(f"Conversa {conversation_id} desatribuída do agente IA")

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


async def _carregar_funis(url: str, token: str, account_id: int) -> dict:
    """Carrega funis e etapas da conta, com cache."""
    if account_id in _funnel_cache:
        return _funnel_cache[account_id]

    headers = {"api_access_token": token}
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"{url}/api/v1/accounts/{account_id}/funnels", headers=headers, timeout=10)
            if not resp.is_success:
                return {}
            funnels = resp.json().get("payload", [])

        # Mapear: identifier → {funnel_id, steps: {step_identifier → step_id}}
        mapa = {}
        for f in funnels:
            steps = {}
            for s in f.get("funnel_steps", []):
                steps[s["identifier"]] = s["id"]
            mapa[f["identifier"]] = {"funnel_id": f["id"], "steps": steps}

        _funnel_cache[account_id] = mapa
        logger.info(f"[kanban] Cache carregado para account_id={account_id}: {list(mapa.keys())}")
        return mapa
    except Exception as e:
        logger.warning(f"[kanban] Erro ao carregar funis: {e}")
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
    if not funil:
        logger.debug(f"[kanban] Funil '{funnel_identifier}' nao encontrado para account_id={account_id}")
        return

    funnel_id = funil["funnel_id"]
    step_id = funil["steps"].get(step_identifier)
    if not step_id:
        logger.debug(f"[kanban] Step '{step_identifier}' nao encontrado no funil '{funnel_identifier}'")
        return

    headers = {"api_access_token": token, "Content-Type": "application/json"}

    try:
        # Buscar se já existe um item para esta conversa em QUALQUER etapa deste funil
        async with httpx.AsyncClient() as http:
            # Buscar todas as etapas do funil para encontrar o card
            item_existente = None
            step_atual = None
            for sid_name, sid_val in funil["steps"].items():
                resp = await http.get(
                    f"{url}/api/v1/accounts/{account_id}/funnels/{funnel_id}/funnel_steps/{sid_val}/funnel_items",
                    headers=headers, timeout=10
                )
                if resp.is_success:
                    data = resp.json()
                    items = data.get("items", data) if isinstance(data, dict) else data
                    if isinstance(items, list):
                        for item in items:
                            if item.get("conversation_id") == conversation_id:
                                item_existente = item
                                step_atual = sid_val
                                break
                if item_existente:
                    break

            if item_existente and step_atual != step_id:
                # Mover card para nova etapa
                await http.put(
                    f"{url}/api/v1/accounts/{account_id}/funnels/{funnel_id}/funnel_steps/{step_atual}/funnel_items/{item_existente['id']}/update_step",
                    headers=headers,
                    json={"funnel_item": {"funnel_step_id": step_id}},
                    timeout=10
                )
                logger.info(f"[kanban] Card movido: conv={conversation_id} → {step_identifier} (funil={funnel_identifier})")
            elif not item_existente:
                # Criar novo card
                await http.post(
                    f"{url}/api/v1/accounts/{account_id}/funnels/{funnel_id}/funnel_steps/{step_id}/funnel_items",
                    headers=headers,
                    json={
                        "title": contact_name or f"Conversa #{conversation_id}",
                        "conversation_id": conversation_id,
                        "status": "active",
                        "priority": "medium",
                    },
                    timeout=10
                )
                logger.info(f"[kanban] Card criado: conv={conversation_id} → {step_identifier} (funil={funnel_identifier})")
            else:
                logger.debug(f"[kanban] Card ja esta na etapa correta: conv={conversation_id}")

    except Exception as e:
        logger.warning(f"[kanban] Erro ao mover/criar card: {e}")


# ── AGENDA (INTEGRAÇÃO REAL VIA N8N) ──────────────────────────

WEBHOOK_CONSULTAR_AGENDA = "https://flow.advbrasil.ai/webhook/consultar-agenda"
WEBHOOK_AGENDAR = "https://flow.advbrasil.ai/webhook/agendar"


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
    return _calcular_slots_disponiveis(advogados, events, qtd_dias)


def _calcular_slots_disponiveis(advogados: list, events: list, qtd_dias: int) -> list:
    """Calcula slots disponíveis subtraindo eventos ocupados da disponibilidade de cada advogado."""
    BR_TZ = timezone(timedelta(hours=-3))
    agora = datetime.now(BR_TZ)

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

        # Eventos que bloqueiam este advogado: os dele (por nome/cor) + os bloqueantes gerais
        eventos_adv = eventos_por_adv.get(nome_key, []) + eventos_bloqueantes

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


def dividir_mensagem(texto: str, limite: int = 250) -> list[str]:
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


async def enviar_resposta_chatwoot(chatwoot_url: str, chatwoot_token: str, account_id: int, conversation_id: int, texto: str, inbox_id: int | None = None, inatividade_ativa: bool = True):
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

    return None  # fallback se loop esgotar


# ── DEBOUNCE ──────────────────────────────────────────────────

async def _executar_com_debounce(config: dict, account_id: int, conversation_id: int, inbox_id: int | None):
    await asyncio.sleep(10)
    _debounce_tasks.pop(conversation_id, None)
    logger.info(f"[debounce] Processando conversa {conversation_id}")
    await processar_mensagem(config, account_id, conversation_id, inbox_id)


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
            config["chatwoot_url"], config["chatwoot_token"], account_id, conversation_id
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
        return

    context = {
        "inbox_id": inbox_id,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
        "historico_texto": historico_texto,
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
