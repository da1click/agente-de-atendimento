from openai import OpenAI
from datetime import datetime, timezone, timedelta
from db import upsert_conversation, upsert_lead, inserir_agendamento
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
            "description": "Transfere a conversa para análise de um humano. Remove a atribuição do agente IA.",
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
            "description": "Consulta os horários disponíveis na agenda para agendamento.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "Agendar",
            "description": "Confirma o agendamento de uma consulta.",
            "parameters": {
                "type": "object",
                "properties": {
                    "horario": {"type": "string", "description": "Horário escolhido (ex: 14:30)"},
                    "data": {"type": "string", "description": "Data (ex: 06/03/2026)"},
                    "advogada": {"type": "string", "description": "Nome da advogada ou 'plantao'"}
                },
                "required": ["horario", "data"]
            }
        }
    }
]

# ── EXECUÇÃO DAS TOOLS ────────────────────────────────────────

async def executar_tool(nome: str, args: dict, config: dict, conversation_id: int, context: dict) -> str:
    account_id = config["account_id"]
    inbox_id = context.get("inbox_id")
    contact_name = context.get("contact_name", "")
    contact_phone = context.get("contact_phone", "")
    chatwoot_url = config["chatwoot_url"]
    chatwoot_token = config["chatwoot_token"]

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
        slots = consultar_agenda_mock()
        logger.info(f"Tool: ConsultarAgenda → {len(slots)} slots")
        return json.dumps({"slots": slots})

    if nome == "Agendar":
        resultado = agendar_mock(args)
        if resultado.get("STATUS") == "SUCESSO":
            try:
                inserir_agendamento(
                    account_id=account_id,
                    inbox_id=inbox_id,
                    conversation_id=conversation_id,
                    contact_name=contact_name,
                    contact_phone=contact_phone,
                    scheduled_date=args.get("data", ""),
                    scheduled_time=args.get("horario", ""),
                    advogada=args.get("advogada", ""),
                )
            except Exception as e:
                logger.warning(f"Supabase erro (Agendar): {e}")
        logger.info(f"Tool: Agendar → {resultado}")
        return json.dumps(resultado)

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


async def chatwoot_transferir_humano(url: str, token: str, account_id: int, conversation_id: int):
    headers = {"api_access_token": token, "Content-Type": "application/json"}
    assign_url = f"{url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/assignments"
    async with httpx.AsyncClient() as http:
        await http.post(assign_url, headers=headers, json={"assignee_id": None}, timeout=10)
    logger.info(f"Conversa {conversation_id} desatribuída do agente IA")


# ── AGENDA (STUBS — integrar depois) ─────────────────────────

def consultar_agenda_mock() -> list:
    """Retorna slots mock. Substituir por integração real de agenda."""
    dh = datetime.now(timezone(timedelta(hours=-3)))
    data_hoje = dh.strftime("%d/%m/%Y")
    return [
        {"data": data_hoje, "horario": "09:00", "advogada": "Ana"},
        {"data": data_hoje, "horario": "10:30", "advogada": "Barbara"},
        {"data": data_hoje, "horario": "14:00", "advogada": "Ana"},
        {"data": data_hoje, "horario": "15:30", "advogada": "Barbara"},
    ]


def agendar_mock(args: dict) -> dict:
    """Mock de agendamento. Substituir por integração real."""
    return {
        "STATUS": "SUCESSO",
        "mensagem_sistema": f"Agendamento confirmado para {args.get('data')} às {args.get('horario')}",
        "advogada": args.get("advogada", "Ana"),
    }


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
        prefixo = "[Cliente]" if tipo == 0 else "[Camila]"
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
        partes.append(atual)

    return partes


# ── CHATWOOT: BUSCAR HISTÓRICO E ENVIAR ───────────────────────

async def buscar_historico_chatwoot(chatwoot_url: str, chatwoot_token: str, account_id: int, conversation_id: int) -> list:
    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": chatwoot_token}
    async with httpx.AsyncClient() as http:
        resp = await http.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    msgs = resp.json().get("payload", [])
    return sorted(msgs, key=lambda m: m.get("created_at", 0))


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
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content.strip())
    fase = data.get("proxima_fase", "identificacao")
    logger.info(f"Supervisor → fase: {fase}")
    return fase


async def chamar_agente(config: dict, fase: str, messages_openai: list, conversation_id: int, context: dict) -> str | None:
    """Chama o agente com suporte a function calling em loop."""
    arquivo = f"{fase}.md"
    prompt = carregar_prompt(config["account_id"], arquivo)
    prompt = prompt.replace("{data_hora_atual}", data_hora_atual())

    client = OpenAI(api_key=config["openai_api_key"])
    msgs = [{"role": "system", "content": prompt}, *messages_openai]

    # Loop de tool calling
    for _ in range(5):  # máximo 5 rodadas para evitar loop infinito
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=msgs,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.7,
        )
        choice = resp.choices[0]
        msg = choice.message

        # Sem tool call → resposta final
        if not msg.tool_calls:
            resposta = (msg.content or "").strip()
            logger.info(f"Agente [{fase}] → {resposta[:120]}...")
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
    historico = await buscar_historico_chatwoot(
        chatwoot_url=config["chatwoot_url"],
        chatwoot_token=config["chatwoot_token"],
        account_id=account_id,
        conversation_id=conversation_id,
    )

    historico_texto = formatar_conversa_texto(historico)
    historico_openai = formatar_conversa_openai(historico)

    fase = chamar_supervisor(config, historico_texto)

    if fase == "transferir_humano":
        logger.info("Supervisor → transferir_humano")
        await chatwoot_transferir_humano(
            config["chatwoot_url"], config["chatwoot_token"], account_id, conversation_id
        )
        return

    # Extrair dados do contato do histórico
    contact_name = ""
    contact_phone = ""
    for msg in historico:
        if msg.get("message_type") == 0:
            sender = msg.get("sender", {})
            contact_name = sender.get("name", "")
            contact_phone = sender.get("phone_number", "")
            break

    context = {
        "inbox_id": inbox_id,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
    }

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
