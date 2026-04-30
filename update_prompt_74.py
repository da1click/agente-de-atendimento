from services.store import update_client, get_client
import sys
with open('/tmp/prompt74_live.md','r',encoding='utf-8') as f: p=f.read()
cur=get_client(74,use_cache=False)
if not cur: print('ERRO'); sys.exit(1)
print(f'Antes: {len(cur.get("prompt") or "")} | Depois: {len(p)} | diff: {len(p)-len(cur.get("prompt") or ""):+d}')
r=update_client(74,{'prompt':p})
print('OK' if r else 'ERRO')
