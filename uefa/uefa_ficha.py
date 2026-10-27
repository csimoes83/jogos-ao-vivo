#!/usr/bin/env python3
"""Ficha da Champions (nfp-sheets/1, source uefa) jogo a jogo, a partir da API pública da UEFA (match.uefa.com, lê-se do Mac).

Uso:  python3 uefa_ficha.py <saida.json> <id do jogo no site> [<id> ...]

- Antes do jogo (escalação já publicada, ~1 h antes): ficha só com convocados, cinco inicial e treinadores (score null, sem eventos).
- Depois do jogo (status FINISHED): + golos e cartões, com o minuto corrido (16:15 = minuto 17) e o resultado.
Pessoas = id da UEFA + data de nascimento (a melhor prova). Importar com import_sheets source uefa, existing=fill (nunca apaga).
"""
import json
import math
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MAPA = json.load(open(os.path.join(HERE, "mapa.json")))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15"
POS = {"GOALKEEPER": "GR", "DEFENDER": "FIXO", "MIDFIELDER": "ALA", "FORWARD": "PIVOT"}
COMP = {2746: "UEFA Futsal Champions League - Ronda Preliminar - Grupo A",
        2747: "UEFA Futsal Champions League - Ronda Principal - Grupo 8", 2748: "UEFA Futsal Champions League - Ronda Principal - Grupo 7",
        2749: "UEFA Futsal Champions League - Ronda Principal - Grupo 6", 2750: "UEFA Futsal Champions League - Ronda Principal - Grupo 5",
        2751: "UEFA Futsal Champions League - Ronda Principal - Grupo 4", 2752: "UEFA Futsal Champions League - Ronda Principal - Grupo 3",
        2753: "UEFA Futsal Champions League - Ronda Principal - Grupo 2", 2754: "UEFA Futsal Champions League - Ronda Principal - Grupo 1"}


def get(path):
    req = urllib.request.Request("https://match.uefa.com/v5/" + path, headers={"User-Agent": UA, "Origin": "https://www.uefa.com"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))


def en(tr, key):
    d = (tr or {}).get(key) or {}
    return d.get("EN") or next(iter(d.values()), "")


def person(p):
    out = {"id": str(p["id"]), "name": p.get("internationalName") or "", "country": p.get("countryCode") or ""}
    if p.get("birthDate"):
        out["birth_date"] = p["birthDate"]
    pos = POS.get(p.get("fieldPosition") or "")
    if pos:
        out["position"] = pos
    return out


def minute(t):
    t = t or {}
    return max(1, math.ceil((t.get("minute") or 0) + ((t.get("second") or 0) / 60.0)))


def build(ids):
    teams, people, matches, notes = {}, {}, [], []
    comps = {}
    for mid in ids:
        um, hid, aid, comp = MAPA["jogos"][str(mid)]
        m = get(f"matches/{um}")
        hu, au = str(m["homeTeam"]["id"]), str(m["awayTeam"]["id"])
        if hu != MAPA["equipas_uefa"].get(str(hid)) or au != MAPA["equipas_uefa"].get(str(aid)):
            notes.append(f"{mid}: equipas da UEFA ({hu}/{au}) não batem com as nossas ({hid}/{aid}) — JOGO SALTADO")
            continue
        for uid, ours, tm in ((hu, hid, m["homeTeam"]), (au, aid, m["awayTeam"])):
            teams[uid] = {"id": uid, "name": tm.get("internationalName") or "", "our_team_id": ours, "country": tm.get("countryCode") or ""}
        finished = m.get("status") in ("FINISHED", "LIVE")  # ao vivo: golos e cartões até agora
        lu = get(f"matches/{um}/lineups")
        sheets, on = None, {hu: set(), au: set()}
        if lu.get("lineupStatus") == "AVAILABLE":
            sheets = {}
            for side, uid in (("homeTeam", hu), ("awayTeam", au)):
                t = lu[side]
                st = [str(x["player"]["id"]) for x in t.get("field", [])]
                bn = [str(x["player"]["id"]) for x in t.get("bench", [])]
                for x in t.get("field", []) + t.get("bench", []):
                    people[str(x["player"]["id"])] = person(x["player"])
                on[uid] = set(st) | set(bn)
                co = (t.get("coaches") or [None])[0]
                coach = None
                if co:
                    pr = co["person"]
                    coach = {"id": str(pr["id"]), "name": (en(pr.get("translations"), "firstName") + " " + en(pr.get("translations"), "lastName")).strip(),
                             "country": pr.get("countryCode") or ""}
                sheets["home" if side == "homeTeam" else "away"] = {"starters": st, "bench": bn, **({"coach": coach} if coach else {})}
        else:
            notes.append(f"{mid}: escalação da UEFA ainda não publicada ({lu.get('lineupStatus')})")
        events, score = [], None
        if finished:
            sc = m["score"].get("regular") or m["score"].get("total")
            score = [sc["home"], sc["away"]]
            for g in (m.get("playerEvents") or {}).get("scorers", []):
                pid = str(g["player"]["id"])
                people.setdefault(pid, person(g["player"]))
                uid = str(g.get("teamId") or "")
                if g.get("goalType") not in ("SCORED", "PENALTY", None):
                    notes.append(f"{mid}: golo tipo {g.get('goalType')} de {g['player'].get('internationalName')} {minute(g.get('time'))}' — VER À MÃO (auto-golo? equipa {uid})")
                    typ = "own_goal" if "OWN" in str(g.get("goalType")) else "goal"
                else:
                    typ = "goal"
                events.append({"type": typ, "person": pid, "minute": minute(g.get("time"))})
                if uid in on and pid not in on[uid]:
                    on[uid].add(pid)
            for c in get(f"matches/{um}/events?filter=CARDS&offset=0&limit=200"):
                if not (c.get("primaryActor") or {}).get("person", {}).get("id"):
                    notes.append(f"{mid}: cartão {c.get('type')} aos {minute(c.get('time'))}' sem jogador na UEFA — VER À MÃO")
                    continue
                pid = str(c["primaryActor"]["person"]["id"])
                people.setdefault(pid, person(c["primaryActor"]["person"]))
                kind = {"YELLOW_CARD": "yellow", "RED_CARD": "red", "SECOND_YELLOW_CARD": "second_yellow", "YELLOW_RED_CARD": "second_yellow"}.get(c.get("type"))
                if not kind:
                    notes.append(f"{mid}: cartão {c.get('type')} de {pid} — VER À MÃO")
                    continue
                events.append({"type": kind, "person": pid, "minute": minute(c.get("time"))})
            if sheets is None:
                notes.append(f"{mid}: jogo terminado mas sem escalação — eventos sem ficha, o importador pode recusar")
            n_h = sum(1 for e in events if e["type"] == "goal" and e["person"] in on[hu]) + sum(1 for e in events if e["type"] == "own_goal" and e["person"] in on[au])
            n_a = sum(1 for e in events if e["type"] == "goal" and e["person"] in on[au]) + sum(1 for e in events if e["type"] == "own_goal" and e["person"] in on[hu])
            if [n_h, n_a] != score:
                notes.append(f"{mid}: golos nos eventos {n_h}-{n_a} ≠ resultado {score[0]}-{score[1]} — VER")
        comps[str(comp)] = {"key": str(comp), "name": COMP[comp], "type": "group", "order": 1}
        refs = []   # o importador aceita no máximo 3: árbitro 1, 2 e 3 (a UEFA envia também cronometrista, delegado e observador)
        by_role = {r.get("role"): r.get("person") or {} for r in m.get("referees") or []}
        for role in ("REFEREE_ONE", "REFEREE_TWO", "REFEREE_THREE"):
            pr = by_role.get(role) or {}
            nm = (en(pr.get("translations"), "firstName") + " " + en(pr.get("translations"), "lastName")).strip()
            if pr.get("id") and nm:
                refs.append({"id": str(pr["id"]), "name": nm, "country": pr.get("countryCode") or ""})
        matches.append({"id": str(um), "our_match_id": mid, "competition": str(comp), "phase": (m.get("matchday") or {}).get("longName") or "",
                        "date": m["kickOffTime"]["dateTime"].replace(".000", ""), "home": hu, "away": au, "score": score, "extra_time": False,
                        "penalties": None, "referees": refs, "sheets": sheets, "events": events})
    used = {p for mm in matches if mm["sheets"] for s in mm["sheets"].values() for p in s["starters"] + s["bench"]}
    used |= {e["person"] for mm in matches for e in mm["events"]}
    bundle = {"format": "nfp-sheets/1", "source": "uefa", "scope": "uefa-cl-" + "-".join(map(str, ids)), "target": {"edition_id": MAPA["edition_id"]},
              "escalao_id": 1, "placement": "after", "teams": list(teams.values()), "people": [people[p] for p in sorted(used) if p in people],
              "competitions": list(comps.values()), "options": {}, "matches": matches}
    return bundle, notes


if __name__ == "__main__":
    out, ids = sys.argv[1], [int(a) for a in sys.argv[2:]]
    b, notes = build(ids)
    json.dump(b, open(out, "w"), ensure_ascii=False, separators=(",", ":"))
    print(len(b["matches"]), "jogos;", len(b["people"]), "pessoas;", len(json.dumps(b)), "bytes")
    for n in notes:
        print("NOTA", n)
