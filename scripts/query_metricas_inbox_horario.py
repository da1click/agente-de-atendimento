"""Metricas de leads novos por inbox + janela horaria (BR).

Conta quantas conversas novas chegaram em uma inbox dentro de um intervalo
de datas e horas. Classifica por origem (anuncio_meta / direto / site)
aplicando retroativamente a logica de main.classificar_origem na primeira
mensagem incoming, util quando a conta nao tem ia_tracking_events.

Janela horaria: pode atravessar a meia-noite. Ex: --hora-ini 18 --hora-fim 9
filtra leads cuja hora BR seja >= 18 OU < 9.

Uso:
  python scripts/query_metricas_inbox_horario.py \
      --account 18 --inbox 28 \
      --de 2026-05-01 --ate 2026-05-07 \
      --hora-ini 18 --hora-fim 9
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Permite rodar de qualquer lugar
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_db
from main import classificar_origem

TZ_BR = timezone(timedelta(hours=-3))


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", type=int, required=True, help="account_id")
    p.add_argument("--inbox", type=int, default=None, help="inbox_id (opcional — sem isto, agrega todas as inboxes da conta)")
    p.add_argument("--de", required=True, help="Data inicial BR (YYYY-MM-DD)")
    p.add_argument("--ate", required=True, help="Data final BR inclusiva (YYYY-MM-DD)")
    p.add_argument("--hora-ini", type=int, default=0, help="Hora inicial da janela (0-23). Default 0")
    p.add_argument("--hora-fim", type=int, default=24, help="Hora final exclusiva (1-24). Default 24 (dia inteiro). Pode ser < hora-ini para janelas que cruzam a meia-noite.")
    p.add_argument("--detalhe", action="store_true", help="Lista cada lead")
    return p.parse_args()


def hora_dentro(h: int, ini: int, fim: int) -> bool:
    """Janela [ini, fim). Se ini > fim, cruza meia-noite (ex: 18..9)."""
    if ini == fim:
        return False
    if ini < fim:
        return ini <= h < fim
    return h >= ini or h < fim


def buscar_leads(db, account_id: int, inbox_id: int | None, dt_ini_utc: str, dt_fim_utc: str) -> list[dict]:
    leads, offset = [], 0
    while True:
        q = (db.table("ia_leads")
             .select("conversation_id,inbox_id,contact_name,contact_phone,created_at")
             .eq("account_id", account_id)
             .gte("created_at", dt_ini_utc)
             .lte("created_at", dt_fim_utc)
             .order("created_at")
             .range(offset, offset + 999))
        if inbox_id is not None:
            q = q.eq("inbox_id", inbox_id)
        page = q.execute().data or []
        leads.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    return leads


def buscar_primeiras_mensagens(db, account_id: int, conv_ids: list[int]) -> dict[int, str]:
    primeiras: dict[int, str] = {}
    for i in range(0, len(conv_ids), 50):
        lote = conv_ids[i:i + 50]
        rows = (db.table("ia_mensagens")
                .select("conversation_id,content,created_at_chatwoot")
                .eq("account_id", account_id)
                .in_("conversation_id", lote)
                .eq("message_type", "incoming")
                .order("conversation_id")
                .order("created_at_chatwoot")
                .range(0, 999)
                .execute().data or [])
        for r in rows:
            cid = r["conversation_id"]
            if cid not in primeiras:
                primeiras[cid] = r.get("content") or ""
    return primeiras


def main_cli():
    args = parse_args()

    dia_ini = datetime.strptime(args.de, "%Y-%m-%d").date()
    dia_fim = datetime.strptime(args.ate, "%Y-%m-%d").date()

    # Janela amplia para garantir bordas (cruza meia-noite)
    inicio_busca_br = datetime.combine(dia_ini, datetime.min.time(), TZ_BR) - timedelta(hours=4)
    fim_busca_br = datetime.combine(dia_fim, datetime.min.time(), TZ_BR) + timedelta(days=1, hours=4)

    db = get_db()
    leads = buscar_leads(db, args.account, args.inbox,
                         inicio_busca_br.astimezone(timezone.utc).isoformat(),
                         fim_busca_br.astimezone(timezone.utc).isoformat())

    janela_dia_ini = datetime.combine(dia_ini, datetime.min.time(), TZ_BR)
    janela_dia_fim_excl = datetime.combine(dia_fim, datetime.min.time(), TZ_BR) + timedelta(days=1)

    filtrados = []
    for l in leads:
        dt = datetime.fromisoformat(l["created_at"].replace("Z", "+00:00")).astimezone(TZ_BR)
        if not (janela_dia_ini <= dt < janela_dia_fim_excl):
            continue
        if not hora_dentro(dt.hour, args.hora_ini, args.hora_fim):
            continue
        filtrados.append({**l, "_dt_br": dt})

    print(f"=== Conta {args.account} | Inbox {args.inbox if args.inbox else '(todas)'} ===")
    print(f"Periodo BR: {args.de} a {args.ate} | Janela horaria: {args.hora_ini:02d}:00 -> {args.hora_fim:02d}:00")
    print(f"Leads novos: {len(filtrados)}\n")

    if not filtrados:
        return

    primeiras = buscar_primeiras_mensagens(db, args.account, [l["conversation_id"] for l in filtrados])

    origens = {"anuncio_meta": 0, "direto": 0, "site": 0, "(sem_msg)": 0}
    for l in filtrados:
        c = primeiras.get(l["conversation_id"])
        o = classificar_origem(c)["origem"] if c is not None else "(sem_msg)"
        origens[o] = origens.get(o, 0) + 1
        l["_origem"] = o

    print("Por origem:")
    for o, q in sorted(origens.items(), key=lambda x: -x[1]):
        if q:
            print(f"  {o:15s} {q}")

    por_dia: dict = {}
    for l in filtrados:
        d = l["_dt_br"].date()
        por_dia[d] = por_dia.get(d, 0) + 1
    print("\nPor dia BR (ocorrencia dentro da janela horaria):")
    for d, q in sorted(por_dia.items()):
        print(f"  {d}  {q}")

    if args.detalhe:
        print("\nDetalhe:")
        for l in sorted(filtrados, key=lambda x: x["_dt_br"]):
            dt = l["_dt_br"]
            nome = (l.get("contact_name") or "").encode("ascii", "replace").decode()
            print(f"  {dt.strftime('%d/%m %H:%M')}  conv={l['conversation_id']}  "
                  f"{l['_origem']:13s} {l.get('contact_phone') or '':18s} {nome}")


if __name__ == "__main__":
    main_cli()
