#!/usr/bin/env python3
"""Descobre, todos os dias, os jogos novos do site e liga-os ao Flashscore, para o operador (live.py) os vigiar.

Como funciona:
  1. lê as páginas públicas das competições do site (os jogos da jornada, com o id de cada um);
  2. para os que ainda não estão em matches.json e acontecem nos próximos dias, pergunta ao site a hora e as equipas;
  3. cruza com o feed do Flashscore (mesma hora e equipas) e acrescenta a matches.json o jogo com o id do Flashscore.
Nunca cria jogos no site e nunca liga quando há dúvida (hora diferente, equipas que não batem ou mais de um candidato).
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live  # noqa: E402  (reutiliza o feed, o casamento por equipas e o cliente do site)

SITE = "https://futsalportugal.com/pt/maincompetition/{id}"
# competições principais do site (menu): Portugal, Espanha, Itália, Brasil, Europa e seleções
COMPETICOES = [26, 45, 43, 40, 1, 3, 27, 46, 44, 41, 2, 48,        # Portugal masculino e feminino
               33, 15, 37, 32, 16, 38,                              # Espanha
               34, 18, 39, 20, 21, 19,                              # Itália
               28, 35,                                              # Brasil
               57, 76, 30,                                          # Champions, Finalíssima, Masters Futsal Cup
               6, 74, 72, 4, 5, 70, 71, 58, 14]                     # Mundial, Europeu, Copa América
MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}
BLOCO = re.compile(r"location\.href='/pt/gamesheet/(\d+)'.{0,1500}?class=\"mct-date\">\s*(\d{1,2})\s+([a-zç]{3})", re.S)


def ids_da_pagina(html, hoje, dias):
    """-> {id_do_jogo} dos jogos da página cuja data cai entre ontem e daqui a `dias` dias."""
    out = set()
    for gid, dia, mes in BLOCO.findall(html):
        if mes not in MESES:
            continue
        ano = hoje.year
        d = dt.date(ano, MESES[mes], int(dia))
        if (d - hoje).days < -200:  # jogo de janeiro visto em dezembro
            d = dt.date(ano + 1, MESES[mes], int(dia))
        if -1 <= (d - hoje).days <= dias:
            out.add(int(gid))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=3, help="quantos dias à frente")
    ap.add_argument("--dry", action="store_true", help="só mostra, não grava matches.json")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "matches.json")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    conhecidos = {m["id"] for m in doc["matches"]}
    agora = live.NOW_FN()
    hoje = agora.astimezone(live.LISBON).date()

    token = os.environ.get("NFP_MCP_TOKEN", "").strip()
    if not token:
        print("::error::falta o segredo NFP_MCP_TOKEN")
        return 1
    mcp = live.Mcp(token, apply=False)

    # 1. jogos do site nos próximos dias que ainda não estão na lista
    candidatos = set()
    for cid in COMPETICOES:
        try:
            candidatos |= ids_da_pagina(live.http_get(SITE.format(id=cid)), hoje, args.dias)
        except RuntimeError as e:
            print(f"::warning::competição {cid}: {e}")
    novos = sorted(candidatos - conhecidos)
    print(f"{len(candidatos)} jogos do site nos próximos {args.dias} dias; {len(novos)} ainda fora da lista")

    # 2. hora e equipas de cada um, pelo site
    site_jogos = []
    for gid in novos:
        try:
            r = mcp.call("get_match", {"id": gid})
        except live.McpError as e:
            print(f"::warning::jogo {gid}: {e}")
            continue
        ko = live.site_ko(r["match"])
        if not (agora - dt.timedelta(hours=6) <= ko <= agora + dt.timedelta(days=args.dias + 1)):
            continue
        site_jogos.append({"id": gid, "ko_utc": ko, "home": r["home"], "away": r["away"], "comp": r["match"]["competition_id"],
                           "ko_lisboa": ko.astimezone(live.LISBON).strftime("%Y-%m-%dT%H:%M")})

    # 3. feed do Flashscore dos mesmos dias
    feed = {}
    for d in range(-1, args.dias + 2):
        try:
            feed.update(live.parse_feed(live.http_get(live.FS_FEED.format(d=d), live.FS_HEADERS)))
        except RuntimeError as e:
            print(f"::warning::feed do Flashscore (dia {d}): {e}")
    ligados, ambiguos = live.casar_feed_com_site(feed, site_jogos)

    adicionados = []
    for s in sorted(site_jogos, key=lambda x: x["ko_utc"]):
        nome = f"{s['id']} {s['home']}–{s['away']} ({s['ko_lisboa']})"
        if s["id"] in ligados:
            e = feed[ligados[s["id"]]]
            print(f"LIGADO  {nome}  <->  Flashscore: {e['home']} – {e['away']} [{e['torneio']}]")
            doc["matches"].append({"id": s["id"], "ko": s["ko_lisboa"], "home": s["home"], "away": s["away"],
                                   "comp": s["comp"], "fs": ligados[s["id"]]})
            adicionados.append(s["id"])
        else:
            print(f"SEM LIGAÇÃO  {nome}: o Flashscore não tem este jogo à mesma hora com estas equipas (fica por vigiar)")
    if ambiguos:
        print(f"::warning::{len(ambiguos)} jogo(s) do Flashscore com mais de um candidato no site: não liguei ({', '.join(ambiguos)})")

    if adicionados and not args.dry:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"novos={len(adicionados)}\n")
    print(f"{len(adicionados)} jogo(s) acrescentado(s) a matches.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
