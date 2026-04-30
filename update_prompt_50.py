"""Atualiza prompt da conta 50 no Supabase (tabela clientes).
Executa dentro do container motor-ia, que ja tem SUPABASE_URL e SUPABASE_KEY."""
import sys
from services.store import update_client, get_client

NEW_PROMPT_PATH = "/tmp/prompt50_live.md"

with open(NEW_PROMPT_PATH, "r", encoding="utf-8") as f:
    new_prompt = f.read()

current = get_client(50, use_cache=False)
if not current:
    print("ERRO: cliente 50 nao encontrado")
    sys.exit(1)

old_len = len(current.get("prompt") or "")
new_len = len(new_prompt)
print(f"Antes: {old_len} chars | Depois: {new_len} chars | diff: {new_len - old_len:+d}")

res = update_client(50, {"prompt": new_prompt})
if res:
    print("OK: prompt atualizado no Supabase.")
else:
    print("ERRO: update falhou (ver logs).")
    sys.exit(2)
