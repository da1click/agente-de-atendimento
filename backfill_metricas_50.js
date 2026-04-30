// Backfill de `conversas_metricas` para conta 50 — ultimos 7 dias.
//
// Pre-requisito: a migration 2026-04-21_conversas_metricas.sql ja rodou no Supabase.
// Como rodar: node backfill_metricas_50.js
//
// O script chama o endpoint /webhook do motor-ia passando um evento falso de
// "tool call" para forcar a execucao do extract_and_save para cada conversa
// fechada (label convertido/desqualificado/abandono) nos ultimos 7 dias.
//
// Em vez de chamar endpoint, melhor: chamar o extrator direto via script Python
// no container. Este JS so lista as conversas elegiveis e imprime os comandos.

const CHATWOOT_URL = 'https://app.clearchat.com.br';
const TOKEN = 'EogvXWpg9s7gQgM8rGPL6PS6';
const ACCOUNT_ID = 50;
const DAYS = 7;

async function main() {
  const now = Math.floor(Date.now() / 1000);
  const fromTs = now - DAYS * 24 * 3600;
  const statuses = ['open', 'resolved', 'pending'];
  const all = [];
  const seen = new Set();

  for (const status of statuses) {
    let page = 1;
    while (page <= 40) {
      const r = await fetch(
        `${CHATWOOT_URL}/api/v1/accounts/${ACCOUNT_ID}/conversations?status=${status}&page=${page}`,
        { headers: { api_access_token: TOKEN } }
      );
      const j = await r.json();
      const list = (j.data && j.data.payload) || [];
      if (!list.length) break;
      for (const c of list) {
        if (c.created_at >= fromTs && !seen.has(c.id)) {
          seen.add(c.id);
          all.push(c);
        }
      }
      // Termina quando last_activity mais velho da pagina ja e anterior a janela
      const oldestActivity = Math.min(...list.map(c => c.last_activity_at || c.created_at));
      if (oldestActivity < fromTs) break;
      if (list.length < 25) break;
      page++;
    }
  }

  const mapLabelToDesfecho = {
    convertido: 'conversao',
    desqualificado: 'desqualificacao',
    abandono: 'abandono',
  };

  const elegiveis = [];
  for (const c of all) {
    const labels = c.labels || [];
    for (const [label, desfecho] of Object.entries(mapLabelToDesfecho)) {
      if (labels.includes(label)) {
        elegiveis.push({ conv_id: c.id, desfecho, labels });
        break;
      }
    }
  }

  console.log(`Conta ${ACCOUNT_ID} — ultimos ${DAYS} dias:`);
  console.log(`  Conversas totais: ${all.length}`);
  console.log(`  Elegiveis para extract: ${elegiveis.length}`);
  console.log('');
  console.log('Gerando comando Python para rodar DENTRO do container motor-ia:');
  console.log('');

  const pyLines = elegiveis.map(
    e => `    ('${e.desfecho}', ${e.conv_id}),`
  ).join('\n');

  const py = `
import asyncio
from services.metrics_extractor import extract_and_save

ACCOUNT_ID = ${ACCOUNT_ID}
ITEMS = [
${pyLines}
]

async def run():
    for desfecho, conv_id in ITEMS:
        print(f'  [{desfecho}] conv {conv_id} ...', flush=True)
        await extract_and_save(ACCOUNT_ID, conv_id, desfecho, 'backfill')
    print('done')

asyncio.run(run())
`;

  require('fs').writeFileSync('backfill_50.py', py);
  console.log('Salvo em backfill_50.py — rodar com:');
  console.log('  scp backfill_50.py root@VPS:/tmp/backfill_50.py');
  console.log('  docker exec -i $(docker ps -q -f name=motor-ia) python /tmp/backfill_50.py');
}

main().catch(e => { console.error(e); process.exit(1); });
