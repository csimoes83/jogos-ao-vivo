#!/usr/bin/env python3
"""Operador de jogos ao vivo do futsalportugal.com (Liga Placard, Liga Feminina, Liga Prime Futsal).

Corre de 5 em 5 minutos num GitHub Action (sem depender de nenhum computador ligado):
  1. lê o estado e o resultado de cada jogo no feed de futsal do Flashscore (Portugal e Espanha)
     e, para Espanha, confirma na página oficial da RFEF;
  2. liga o «ao vivo» (islive=1) quando o jogo arranca e vai atualizando o marcador;
  3. no fim do jogo grava o resultado final, desliga o «ao vivo» e recalcula a classificação.

Só escreve no site pelo MCP (update_match, recompute_standings) e é idempotente: se o site já está
como o feed diz, não faz nada. Nunca fecha um jogo sem resultado conhecido.

Modos:  --mode check  (só confirma que o token tem as permissões certas)   --mode probe  (só lê, nunca escreve)   --mode dry  (pede ao site um dry-run)   --mode run  (aplica)
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

LISBON = ZoneInfo("Europe/Lisbon")
MCP_URL = "https://futsalportugal.com/api/mcp"
FS_FEED = "https://global.flashscore.ninja/2/x/feed/f_11_{d}_1_pt_1"
FS_HEADERS = {"x-fsign": "SW9D1eZo", "Referer": "https://www.flashscore.pt/"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PRE = dt.timedelta(minutes=15)          # começa a vigiar um jogo 15 min antes da hora
POST = dt.timedelta(hours=4)            # deixa de o vigiar 4h depois da hora (acaba antes: um jogo fechado sai logo da vigilância)
LATE_START = dt.timedelta(minutes=10)   # alerta se 10 min depois da hora o feed ainda não mostra arranque
START_GRACE = dt.timedelta(seconds=30)  # à hora do jogo (+30 s) liga o «ao vivo» a 0-0 mesmo que o feed ainda diga «agendado»
FS_INC = "https://global.flashscore.ninja/2/x/feed/df_sui_1_{mid}"
FS_LI = "https://global.flashscore.ninja/2/x/feed/df_li_1_{mid}"
# campos denormalizados dos eventos (gc_match_events) por competição: season_id, escalao_id, main_competition, main_competition_type
EVENT_META = {
    1213: {"season_id": 42, "escalao_id": 1, "main_competition": 26, "main_competition_type": 1},   # Liga Placard
    1923: {"season_id": 42, "escalao_id": 2, "main_competition": 27, "main_competition_type": 1},   # Liga Feminina Placard
    2758: {"season_id": 42, "escalao_id": 1, "main_competition": 33, "main_competition_type": 1},   # Liga Prime Futsal
    2763: {"season_id": 42, "escalao_id": 1, "main_competition": 28, "main_competition_type": 1},   # LNF Cresol (Brasil)
}
PREFILTER = dt.timedelta(hours=12)      # só pergunta ao site por jogos perto da hora prevista


def NOW_FN():
    return dt.datetime.now(dt.timezone.utc)


class McpError(Exception):
    pass


# ----------------------------------------------------------------------------- rede

def http_get(url, headers=None, tries=2, timeout=20):
    last = None
    for _ in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - qualquer falha de rede conta
            last = e
    raise RuntimeError(f"GET {url} falhou: {last}")


class Mcp:
    def __init__(self, token, apply):
        self.token, self.apply, self._n = token, apply, 0

    def call(self, name, args):
        self._n += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._n, "method": "tools/call",
                           "params": {"name": name, "arguments": args}}).encode()
        req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "Authorization": f"Bearer {self.token}", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                reply = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code} em {name}: {e.read().decode('utf-8', 'replace')[:200]}")
        except Exception as e:  # noqa: BLE001
            raise McpError(f"{name}: {e}")
        if "error" in reply:
            raise McpError(f"{name}: {reply['error']}")
        res = reply["result"]
        text = res["content"][0]["text"]
        if res.get("isError"):
            raise McpError(f"{name}: {text[:300]}")
        return json.loads(text)

    def list_tools(self):
        """tools/list: as ferramentas que ESTE token pode usar (mostra a permissão sem escrever nada)."""
        body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}).encode()
        req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "Authorization": f"Bearer {self.token}", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return [t["name"] for t in json.loads(r.read().decode())["result"]["tools"]]
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code} em tools/list: {e.read().decode('utf-8', 'replace')[:200]}")

    def rows(self, resource, where, columns=None, limit=100):
        a = {"resource": resource, "where": where, "limit": limit}
        if columns:
            a["columns"] = columns
        return self.call("records_list", a).get("rows", [])

    def person_by_external(self, source, ext):
        ids = self.call("external_ids", {"action": "list", "entity": "person", "source": source, "external_id": str(ext)}).get("ids", [])
        return ids[0]["person_id"] if len(ids) == 1 else None

    def save_match_fields(self, mid, fields):
        """Campos que o update_match não trata (ex.: part = 1.ª/2.ª parte): record_save resource=match."""
        return self.call("record_save", {"resource": "match", "id": mid, "fields": fields, "confirm": self.apply})

    def save_event(self, fields):
        return self.call("record_save", {"resource": "match_event", "fields": fields, "confirm": self.apply})

    def get_match(self, mid):
        return self.call("get_match", {"id": mid})["match"]

    def update_match(self, mid, fields):
        return self.call("update_match", {"id": mid, "fields": fields, "confirm": self.apply})

    def recompute(self, comp):
        return self.call("recompute_standings", {"competition_id": comp, "confirm": self.apply})


# ----------------------------------------------------------------------------- leitores

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_feed(raw):
    """Feed do Flashscore: blocos separados por '~', campos 'CHAVE÷valor' separados por '¬'."""
    out = {}
    for part in raw.split("~"):
        kv = {}
        for item in part.split("¬"):
            if "÷" in item:
                k, v = item.split("÷", 1)
                kv[k] = v
        if "AA" in kv and "AB" in kv:
            out[kv["AA"]] = {"ab": kv["AB"], "ac": kv.get("AC"), "h": _int(kv.get("AG")), "a": _int(kv.get("AH"))}
    return out


def classify_fs(entry):
    """-> (estado, (casa, fora) | None). Estados: scheduled | live | finished | other."""
    if entry is None:
        return None
    score = (entry["h"], entry["a"]) if entry["h"] is not None and entry["a"] is not None else None
    if entry["ab"] == "1":
        return "scheduled", None
    if entry["ab"] == "2":
        return "live", score
    if entry["ab"] == "3" and entry["ac"] == "3":
        return "finished", score
    return "other", score


def parse_rfef_events(html):
    """Eventos ao vivo da RFEF: [{minute, side (home|away), kind (alt do ícone), pid (id RFEF do jogador), name}]."""
    i = html.find('id="list_events_live"')
    if i < 0:
        return []
    j = html.find('events-legend', i)
    block = html[i:j if j > 0 else len(html)]
    out = []
    for chunk in block.split('class="event-content"')[1:]:
        mm = re.search(r'class="minute">\s*(\d+)', chunk)
        parts = chunk.split('<div class="event">')[1:]
        if not mm or len(parts) < 2:
            continue
        for side, part in (("home", parts[0]), ("away", parts[1])):
            pl = re.search(r'/jugador/[^"]*?/(\d+)"', part)
            if not pl:
                continue
            kind = re.search(r'event-ico"[^>]*?alt="([^"]*)"', part)
            nm = re.search(r'class="[^"]*name">\s*([^<]*?)\s*<', part)
            out.append({"minute": int(mm.group(1)), "side": side, "kind": kind.group(1) if kind else "",
                        "pid": pl.group(1), "name": nm.group(1) if nm else ""})
    return out


def parse_rfef(html):
    st = re.search(r'id="status_match"[^>]*>\s*([^<]*?)\s*<', html)
    rs = re.search(r'id="result_match"[^>]*>\s*(\d+)\s*-\s*(\d+)', html)
    status = st.group(1) if st else ""
    return {"status": status, "score": (int(rs.group(1)), int(rs.group(2))) if rs else None,
            "finished": status.lower().startswith("finaliz"), "events": parse_rfef_events(html)}


def norm_name(x):
    import unicodedata
    t = "".join(c for c in unicodedata.normalize("NFD", x.lower()) if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def match_player(name, squad):
    """name: como o Flashscore escreve («Antonio», «Wanderson Silva», «Lopes J.»); squad: [(person_id, nome normalizado)].
    Devolve o person_id só se houver UM candidato: as palavras do nome têm de estar no nome do site e cada inicial
    tem de abrir outra palavra. Em caso de dúvida devolve None."""
    toks = norm_name(re.sub(r"\(.*?\)", "", name)).split()
    words = [t for t in toks if len(t) > 1]
    inits = [t for t in toks if len(t) == 1]
    if not words:
        return None
    hits = []
    for pid, pn in squad:
        pt = pn.split()
        if all(w in pt for w in words) and all(any(x.startswith(i) for x in pt if x not in words) for i in inits):
            hits.append(pid)
    return hits[0] if len(hits) == 1 else None


def parse_fs_incidents(raw):
    """df_sui do Flashscore: blocos '~III÷id¬IA÷lado¬IB÷minuto'¬IF÷nome¬IK÷tipo¬'. -> eventos no mesmo formato da RFEF."""
    out = []
    for part in raw.split("~"):
        kv = {}
        for item in part.split("¬"):
            if "÷" in item:
                k, v = item.split("÷", 1)
                kv.setdefault(k, v)
        if "III" in kv and kv.get("IK") == "Goal" and kv.get("IF"):
            mm = re.match(r"(\d+)", kv.get("IB", ""))
            if mm and kv.get("IA") in ("1", "2"):
                out.append({"minute": int(mm.group(1)), "side": "home" if kv["IA"] == "1" else "away", "kind": "Gol",
                            "pid": "fs:" + kv["IF"], "name": kv["IF"]})
    return out


# ----------------------------------------------------------------------------- decisão

def decide(site, fs, rf, now, ko):
    """Devolve (campos_a_alterar | None, fechar_jogo, alertas, notas, terminado_e_gravado). Função pura (testável)."""
    alerts, notes = [], []
    status, score = fs if fs else (None, None)
    if fs is None and now >= ko:
        alerts.append("jogo não encontrado no feed do Flashscore")

    if rf:  # a RFEF é a fonte oficial para Espanha
        if rf["finished"] and rf["score"]:
            if status == "finished" and score and score != rf["score"]:
                alerts.append(f"Flashscore {score[0]}-{score[1]} ≠ RFEF {rf['score'][0]}-{rf['score'][1]}: usei a RFEF")
            status, score = "finished", rf["score"]
        elif rf["score"] and status in (None, "scheduled"):
            status, score = "live", rf["score"]
        elif rf["score"] and status == "live" and score is None:
            score = rf["score"]

    if status in (None, "scheduled"):
        if fs is not None and now >= ko + LATE_START:
            alerts.append("passaram mais de 10 min da hora e o feed ainda não mostra o arranque")
        # o jogo devia ter começado: liga já o «ao vivo» a 0-0 (o feed e a RFEF costumam atrasar-se uns minutos); só se o jogo ainda não tinha arrancado no site
        if fs is not None and now >= ko + START_GRACE and site.get("goalhome") is None and site.get("goalaway") is None:
            notes.append("arranque pela hora do jogo (o feed ainda mostra «agendado»)")
            return {"goalhome": 0, "goalaway": 0, "islive": 1}, False, alerts, notes, False
        return None, False, alerts, notes, False
    if status == "other":
        alerts.append("estado do feed não reconhecido (adiado, cancelado ou interrompido?): não toquei no jogo")
        return None, False, alerts, notes, True

    if status == "finished":
        if score is None:
            alerts.append("jogo terminado no feed mas sem resultado: não fechei")
            return None, False, alerts, notes, False
        want = {"goalhome": score[0], "goalaway": score[1], "islive": 0}
    else:  # live
        h, a = score if score else (0, 0)
        want = {"goalhome": h, "goalaway": a, "islive": 1}
        if site.get("goalhome") is not None and site.get("goalaway") is not None \
                and (h + a) < (site["goalhome"] + site["goalaway"]):
            notes.append("o feed tem menos golos que o site (golo anulado ou erro do feed)")

    diff = {k: v for k, v in want.items() if site.get(k) != v}
    return (diff or None), (status == "finished" and bool(diff)), alerts, notes, (status == "finished" and not diff)


# ----------------------------------------------------------------------------- execução

def load_matches():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "matches.json"), encoding="utf-8") as f:
        ms = json.load(f)["matches"]
    for m in ms:
        m["ko_utc"] = dt.datetime.fromisoformat(m["ko"]).replace(tzinfo=LISBON).astimezone(dt.timezone.utc)
    return ms


def site_ko(match):
    """Hora do jogo no site (Lisboa) -> UTC."""
    return dt.datetime.strptime(match["date"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=LISBON).astimezone(dt.timezone.utc)


def norm_site(match):
    return {"goalhome": _int(match.get("goalhome")), "goalaway": _int(match.get("goalaway")),
            "islive": _int(match.get("islive")) or 0}


def fmt_score(s):
    return "-" if not s else f"{s[0]}-{s[1]}"


def sync_goals(mcp, m, raw, rf, caches, log, alerts):
    """Põe na ficha os golos (minuto + marcador) que a RFEF mostra e que ainda não estão lá. Só liga o jogador por id RFEF
    ou, na falta dele, por nome único no plantel da equipa; se houver dúvida não escreve e avisa."""
    skip = {(str(a), int(b)) for a, b in m.get("skip_goals", [])}  # eventos da RFEF que se sabe estarem a mais (ex.: golo registado duas vezes)
    goals = [e for e in rf.get("events", []) if e["kind"].lower().startswith("gol") and (e["pid"], e["minute"]) not in skip]
    if not goals:
        return 0
    for e in rf["events"]:
        if "propia" in e["kind"].lower():
            alerts.append(f"{m['id']}: auto-golo de {e['name']} aos {e['minute']}' não registado automaticamente (fazer à mão)")
    comp = m["comp"]
    meta = EVENT_META.get(comp)
    if not meta:
        alerts.append(f"{m['id']}: competição {comp} sem campos de época configurados; golos não registados")
        return 0
    have = {(r["person_id"], r["time"]) for r in mcp.rows("match_event", {"match_id": m["id"], "event_id": 6}, ["person_id", "time"])}
    written = 0
    for e in goals:
        team = raw["gcteamhome_id"] if e["side"] == "home" else raw["gcteamaway_id"]
        key = (e["pid"], team)
        if key not in caches["person"]:
            pid = None if e["pid"].startswith("fs:") else mcp.person_by_external("rfef", e["pid"])
            if pid is None:  # nome único no plantel desta equipa
                if team not in caches["squad"]:
                    sq = mcp.rows("squad", {"gcteam_id": team, "season_id": meta["season_id"]}, ["person_id"])
                    ids = sorted({r["person_id"] for r in sq})
                    names = mcp.rows("person", {"id": ids}, ["id", "name"]) if ids else []
                    caches["squad"][team] = [(r["id"], norm_name(r["name"])) for r in names]
                pid = match_player(e["name"], caches["squad"][team])
            caches["person"][key] = pid
        pid = caches["person"][key]
        if pid is None:
            alerts.append(f"{m['id']}: golo de {e['name']} aos {e['minute']}' sem jogador certo no site ({e['pid']}): não registei")
            continue
        if (pid, e["minute"]) in have:
            continue
        fields = {"competition_id": comp, **meta, "match_id": m["id"], "team_id": team, "person_id": pid, "event_id": 6, "time": e["minute"]}
        res = mcp.save_event(fields)
        have.add((pid, e["minute"]))
        written += 1
        log(f"{m['id']}: golo {e['name']} {e['minute']}' ({e['side']}) -> {'registado' if mcp.apply else 'dry-run'}"
            f"{' (audit ' + str(res.get('audit_id')) + ')' if res.get('audit_id') else ''}")
    return written


def parse_fs_lineups(raw):
    """df_li do Flashscore -> {'home': [jogador], 'away': [jogador]}; jogador = {name, last, num, starter, inc: [(minuto, tipo)]}.
    tipo: 'Goal' | 'Yellow Card' | 'Red Card' | 'Own Goal' ... (como o Flashscore escreve)."""
    out = {"home": [], "away": []}
    side, role = None, "other"
    for part in raw.split("~"):
        items = [it.split("÷", 1) for it in part.split("¬") if "÷" in it]
        if not items:
            continue
        for k, v in items:
            if k == "LB":
                role = "starters" if v.lower().startswith("start") else ("subs" if v.lower().startswith("sub") else "other")
            elif k == "LC":
                side = "home" if v == "1" else "away"
        if not any(k == "LP" for k, _ in items) or side is None or role == "other":
            continue
        pl = {"name": "", "last": "", "num": None, "starter": role == "starters", "inc": []}
        cur = None
        for k, v in items:
            if k == "LI":
                pl["name"] = v
            elif k == "LN":
                pl["last"] = v
            elif k == "LJ":
                pl["num"] = _int(v)
            elif k == "LII":
                cur = {"min": None, "kind": ""}
                pl["inc"].append(cur)
            elif k == "LIT" and cur is not None:
                mm = re.match(r"(\d+)", v)
                cur["min"] = int(mm.group(1)) if mm else None
            elif k == "LIN" and cur is not None:
                cur["kind"] = v
        if pl["name"]:
            out[side].append(pl)
    return out


def desired_part(ac, status, max_minute):
    """1.ª ou 2.ª parte: devolve 2 quando o Flashscore diz que é a 2.ª parte (AC=13), o jogo acabou ou já houve algo depois dos 20';
    caso contrário None (não mexe; nunca volta atrás)."""
    if ac == "13" or status == "finished":
        return 2
    if max_minute is not None and max_minute > 20:
        return 2
    return None


FS_EVENT = {"Goal": 6, "Yellow Card": 3, "Red Card": 5, "Own Goal": 9}


def resolve_fs_player(pl, squad):
    """squad: [(person_id, nome normalizado, camisola)]. Só devolve se houver certeza: nome único no plantel, ou camisola única
    com o apelido dentro do nome do site; se o nome e a camisola apontarem para pessoas diferentes, devolve None."""
    by_name = match_player(pl["name"], [(i, n) for i, n, _ in squad])
    by_num = None
    if pl.get("num"):
        c = [i for i, _, nu in squad if nu == pl["num"]]
        if len(c) == 1:
            last = norm_name(pl.get("last") or "").split()
            nm = next(n for i, n, _ in squad if i == c[0]).split()
            if last and all(w in nm for w in last):
                by_num = c[0]
    if by_name and by_num and by_name != by_num:
        return None
    return by_name or by_num


def sync_fs_sheet(mcp, m, raw, lineup, caches, log, alerts, goals_cards=True):
    """Alinhamentos (convocados + cinco inicial) e, nos jogos sem RFEF, golos e cartões, a partir do df_li do Flashscore.
    Só acrescenta o que falta (nunca apaga) e só para jogadores ligados com certeza."""
    meta = EVENT_META.get(m["comp"])
    if not meta:
        alerts.append(f"{m['id']}: competição {m['comp']} sem campos de época configurados; ficha não registada")
        return 0
    have_rows = mcp.rows("match_event", {"match_id": m["id"]}, ["person_id", "event_id", "time"], limit=100)
    have = {(r["person_id"], r["event_id"], r["time"]) for r in have_rows}
    have_n = {}   # quantos golos/cartões de cada tipo cada pessoa já tem (venham de onde vierem, mesmo com outro minuto)
    for r in have_rows:
        have_n[(r["person_id"], r["event_id"])] = have_n.get((r["person_id"], r["event_id"]), 0) + 1
    written = 0
    for side in ("home", "away"):
        team = raw["gcteamhome_id"] if side == "home" else raw["gcteamaway_id"]
        if team not in caches["squad3"]:
            sq = mcp.rows("squad", {"gcteam_id": team, "season_id": meta["season_id"]}, ["person_id", "number"])
            ids = sorted({r["person_id"] for r in sq})
            names = {r["id"]: norm_name(f'{r["name"]} {r.get("fpf_name") or ""}') for r in mcp.rows("person", {"id": ids}, ["id", "name", "fpf_name"])} if ids else {}
            nums = {}
            for r in sq:
                nums.setdefault(r["person_id"], r.get("number") or 0)
            caches["squad3"][team] = [(i, names.get(i, ""), nums.get(i, 0)) for i in ids if names.get(i)]
        squad = caches["squad3"][team]
        unresolved = []
        for pl in lineup[side]:
            key = (side, pl["name"], pl["num"])
            if key not in caches["fsplayer"]:
                caches["fsplayer"][key] = resolve_fs_player(pl, squad)
            pid = caches["fsplayer"][key]
            if pid is None:
                unresolved.append(f"{pl['name']} ({pl['num']})")
                continue
            wanted = [(1, 0)] + ([(2, 0)] if pl["starter"] else [])
            if goals_cards:
                per_ev = {}
                for inc in pl["inc"]:
                    ev = FS_EVENT.get(inc["kind"])
                    if ev is None:
                        alerts.append(f"{m['id']}: evento «{inc['kind']}» de {pl['name']} aos {inc['min']}' não registado (fazer à mão)")
                    elif inc["min"] is not None:
                        per_ev.setdefault(ev, []).append(inc["min"])
                for ev, mins in per_ev.items():
                    if have_n.get((pid, ev), 0) >= len(mins):   # a ficha já tem tantos como o Flashscore: não mexo (o minuto pode vir de fonte melhor)
                        continue
                    wanted += [(ev, mn) for mn in mins if (pid, ev, mn) not in have][: len(mins) - have_n.get((pid, ev), 0)]
            for ev, minute in wanted:
                if (pid, ev, minute) in have:
                    continue
                fields = {"competition_id": m["comp"], **meta, "match_id": m["id"], "team_id": team, "person_id": pid, "event_id": ev, "time": minute}
                res = mcp.save_event(fields)
                have.add((pid, ev, minute))
                have_n[(pid, ev)] = have_n.get((pid, ev), 0) + 1
                written += 1
                if ev != 1:
                    log(f"{m['id']}: {['', '', 'cinco inicial', 'amarelo', '', 'vermelho', 'golo', '', '', 'auto-golo'][ev]} {pl['name']} {minute}' ({side}) -> "
                        f"{'registado' if mcp.apply else 'dry-run'}{' (audit ' + str(res.get('audit_id')) + ')' if res.get('audit_id') else ''}")
        if unresolved:
            alerts.append(f"{m['id']}: {len(unresolved)} jogador(es) do Flashscore sem ligação certa no plantel ({', '.join(unresolved[:6])}): fazer à mão")
    return written


def idle_wait(matches, done, now, horizon=dt.timedelta(hours=3)):
    """Segundos até abrir a próxima janela de vigilância, se for dentro do horizonte; senão None."""
    opens = [m["ko_utc"] - PRE for m in matches if m["id"] not in done and m["ko_utc"] - PRE > now]
    if not opens:
        return None
    w = (min(opens) - now).total_seconds()
    return w if w <= horizon.total_seconds() else None


class Fatal(Exception):
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["probe", "dry", "run", "check"], default="run")
    ap.add_argument("--only", help="ids de jogos separados por vírgula")
    ap.add_argument("--ignore-window", action="store_true", help="ignora a janela horária (testes)")
    ap.add_argument("--loop-seconds", type=int, default=0, help="repete a verificação durante N segundos (0 = uma só vez)")
    ap.add_argument("--interval", type=int, default=20, help="segundos entre verificações (com --loop-seconds)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    fixed_now = dt.datetime.fromisoformat(os.environ["LIVE_NOW"]).astimezone(dt.timezone.utc) if os.environ.get("LIVE_NOW") else None
    utcnow = lambda: fixed_now or NOW_FN()  # noqa: E731
    if args.mode == "check":
        token = os.environ.get("NFP_MCP_TOKEN", "").strip()
        if not token:
            print("::warning::falta o segredo NFP_MCP_TOKEN")
            return 1
        try:
            tools = Mcp(token, apply=False).list_tools()
        except McpError as e:
            print(f"::warning::{e}")
            return 1
        need = ["get_match", "update_match", "recompute_standings", "records_list", "record_save", "external_ids"]
        for t in need:
            print(f"{'OK  ' if t in tools else 'FALTA'} {t}")
        print(f"admin (não deve ter): {'sql_query' in tools}  | ferramentas visíveis: {len(tools)}")
        return 0 if all(t in tools for t in need) else 1

    only = {int(x) for x in args.only.split(",")} if args.only else None
    probe = args.mode == "probe"
    now = utcnow()
    matches = [m for m in load_matches() if (only is None or m["id"] in only)]
    if probe or args.ignore_window:
        cand = matches
    else:
        cand = [m for m in matches if m["ko_utc"] - PREFILTER <= now <= m["ko_utc"] + PREFILTER]
    lines, alerts = [], []
    done, last_msg, rf_cache = set(), {}, {}
    caches = {"meta": {}, "person": {}, "squad": {}, "squad3": {}, "fsplayer": {}}
    cur = [now]

    def log(msg, key=None):
        if key is not None:  # numa execução longa, só escreve quando a mensagem do jogo muda
            if last_msg.get(key) == msg:
                return
            last_msg[key] = msg
        print(f"[{cur[0].astimezone(LISBON):%H:%M:%S}] {msg}", flush=True)
        lines.append(f"{cur[0].astimezone(LISBON):%H:%M:%S} {msg}")

    if not cand:
        log("nenhum jogo perto da hora: nada a fazer")
        return finish(args, now, lines, alerts)

    token = os.environ.get("NFP_MCP_TOKEN", "").strip()
    mcp = Mcp(token, apply=(args.mode == "run")) if token else None
    if token:  # só a forma do segredo, nunca o valor
        log(f"segredo: {len(token)} caracteres, {'só letras e números' if token.isalnum() else 'com outros caracteres (espaços, | ou símbolos)'}"
            f"{', com | (id do token à frente)' if '|' in token else ''}")
    if mcp is None:
        alerts.append("falta o segredo NFP_MCP_TOKEN: só consigo ler os feeds" if probe
                      else "falta o segredo NFP_MCP_TOKEN: não consigo escrever no site")
        if not probe:
            return finish(args, now, lines, alerts, hard=True)

    def one_pass(now):
        """Uma verificação a todos os jogos em vigilância. Devolve quantos ainda ficam em vigilância."""
        cur[0] = now
        remaining = 0
        feed = None
        try:
            feed = {}
            # dias do feed: ontem, hoje, amanhã e os dias dos jogos em vigilância (a Champions vai até 31/10)
            days = {-1, 0, 1} | {(m["ko_utc"].astimezone(LISBON).date() - now.astimezone(LISBON).date()).days for m in cand}
            for d in sorted(days):
                feed.update(parse_feed(http_get(FS_FEED.format(d=d) + f"?t={int(time.time() * 1000)}", FS_HEADERS)))
        except Exception as e:  # noqa: BLE001
            alerts.append(f"feed do Flashscore indisponível: {e}")
            feed = None
        to_recompute = {}
        for m in cand:
            if m["id"] in done and not probe:
                continue
            tag = f"{m['id']} {m['home']}–{m['away']}"
            try:
                if not probe and not args.ignore_window and not (m["ko_utc"] - PREFILTER <= now <= m["ko_utc"] + PREFILTER):
                    continue
                fs = classify_fs(feed.get(m["fs"])) if feed is not None else None
                rf = None
                if m.get("rfef"):
                    hit = rf_cache.get(m["id"])
                    if hit and (now - hit[0]).total_seconds() < 60 and not probe:
                        rf = hit[1]
                    else:
                        try:
                            rf = parse_rfef(http_get(m["rfef"], {"Accept-Language": "es-ES,es;q=0.9"}))
                            rf_cache[m["id"]] = (now, rf)
                        except Exception as e:  # noqa: BLE001
                            log(f"{tag}: RFEF indisponível ({e}); sigo só com o Flashscore", key=("rf", m["id"]))
                if feed is None and rf is None and not probe:
                    remaining += 1
                    continue
                site, ko, raw = None, m["ko_utc"], None
                if mcp:
                    raw = mcp.get_match(m["id"])
                    site, ko = norm_site(raw), site_ko(raw)
                    if ko != m["ko_utc"]:
                        log(f"{tag}: hora no site diferente da prevista ({raw['date']}); uso a do site", key=("ko", m["id"]))
                if probe:
                    log(f"{tag}: feed={fs[0] if fs else 'ausente'} {fmt_score(fs[1] if fs else None)}"
                        f" | rfef={('%s %s' % (rf['status'] or '?', fmt_score(rf['score']))) if rf else '-'}"
                        f" | site={('islive=%s %s-%s' % (site['islive'], site['goalhome'], site['goalaway'])) if site else '?'}")
                    continue
                if not (args.ignore_window or ko - PRE <= now <= ko + POST):
                    continue
                lin = None
                if mcp and raw and fs and fs[0] in ("live", "finished"):
                    try:  # ficha do Flashscore: alinhamentos (todos os jogos) e, em Portugal, golos e cartões
                        lin = parse_fs_lineups(http_get(FS_LI.format(mid=m["fs"]) + f"?t={int(time.time() * 1000)}", FS_HEADERS))
                        if lin["home"] or lin["away"]:
                            sync_fs_sheet(mcp, m, raw, lin, caches, lambda msg, key=None: log(msg, key=key), alerts, goals_cards=not m.get("rfef"))
                        elif not m.get("rfef"):  # sem alinhamentos publicados: golos pelo feed de incidências
                            inc = parse_fs_incidents(http_get(FS_INC.format(mid=m["fs"]) + f"?t={int(time.time() * 1000)}", FS_HEADERS))
                            sync_goals(mcp, m, raw, {"events": inc}, caches, lambda msg, key=None: log(msg, key=key), alerts)
                    except (McpError, RuntimeError, KeyError, ValueError) as e:
                        alerts.append(f"{tag}: erro a registar a ficha (Flashscore): {e}")
                if rf and rf.get("events") and mcp and raw and (site["islive"] == 1 or (fs and fs[0] in ("live", "finished"))):
                    try:
                        sync_goals(mcp, m, raw, rf, caches, lambda msg, key=None: log(msg, key=key), alerts)
                    except (McpError, KeyError, ValueError) as e:
                        alerts.append(f"{tag}: erro a registar golos: {e}")
                if mcp and raw and fs and fs[0] in ("live", "finished") and int(raw.get("part") or 1) < 2:
                    try:  # 1.ª -> 2.ª parte (aos 20'): usa o estado do Flashscore e, em último caso, os minutos dos eventos
                        mins = [e["minute"] for e in (rf or {}).get("events", [])]
                        if lin:
                            mins += [i["min"] for s_ in ("home", "away") for pl in lin[s_] for i in pl["inc"] if i["min"] is not None]
                        entry = feed.get(m["fs"]) if feed else None
                        if desired_part(entry["ac"] if entry else None, fs[0], max(mins) if mins else None) == 2:
                            mcp.save_match_fields(m["id"], {"part": 2})
                            log(f"{tag}: passou à 2.ª parte -> {'APLICADO' if mcp.apply else 'dry-run'}")
                    except (McpError, KeyError, ValueError) as e:
                        alerts.append(f"{tag}: erro a passar à 2.ª parte: {e}")
                fields, close, al, notes, finished_saved = decide(site, fs, rf, now, ko)
                alerts.extend(f"{tag}: {a}" for a in al)
                for n in notes:
                    log(f"{tag}: nota: {n}", key=("nota", m["id"]))
                if finished_saved:
                    log(f"{tag}: terminado e gravado ({fmt_score(fs[1] if fs else None)}); deixo de vigiar", key=("st", m["id"]))
                    done.add(m["id"])
                    continue
                if not fields:
                    log(f"{tag}: sem alterações (feed={fs[0] if fs else 'ausente'} {fmt_score(fs[1] if fs else None)})", key=("st", m["id"]))
                    remaining += 1
                    continue
                res = mcp.update_match(m["id"], fields)
                log(f"{tag}: {json.dumps(fields)} -> {'APLICADO' if mcp.apply else 'dry-run'}"
                    f"{' (audit ' + str(res.get('audit_id')) + ')' if res.get('audit_id') else ''}")
                last_msg.pop(("st", m["id"]), None)
                if close:
                    to_recompute[m["comp"]] = tag
                    if mcp.apply:
                        done.add(m["id"])
                if m["id"] not in done:
                    remaining += 1
            except (McpError, RuntimeError, KeyError, ValueError) as e:
                alerts.append(f"{tag}: erro: {e}")
                remaining += 1
        for comp, tag in to_recompute.items():
            try:
                mcp.recompute(comp)
                log(f"classificação da competição {comp} recalculada ({tag})")
            except McpError as e:
                alerts.append(f"competição {comp}: não consegui recalcular a classificação ({e}); fazer à mão")
        return remaining

    t0 = time.monotonic()
    passes = 0
    while True:
        passes += 1
        remaining = one_pass(utcnow())
        if probe or args.loop_seconds <= 0 or remaining == 0:
            break
        if time.monotonic() - t0 + args.interval >= args.loop_seconds:
            break
        time.sleep(args.interval)
    if args.loop_seconds > 0:
        log(f"{passes} verificações em {time.monotonic() - t0:.0f}s; {remaining} jogo(s) ainda em vigilância")
    keepalive = remaining > 0
    if not keepalive and args.mode == "run" and args.loop_seconds > 0:
        # sem jogos agora, mas o próximo abre dentro de 3 h: dorme (sem gastar pedidos) e mantém a cadeia viva até lá
        wait = idle_wait(matches, done, utcnow())
        if wait is not None:
            nap = max(1, min(wait, args.loop_seconds))
            log(f"sem jogos em vigilância; o próximo abre dentro de {wait / 60:.0f} min: espero {nap / 60:.1f} min e mantenho a cadeia")
            time.sleep(nap)
            keepalive = True
    out = os.environ.get("GITHUB_OUTPUT")
    if out and args.mode == "run":  # há jogos em vigilância (ou abrem em breve) -> o workflow dispara já a execução seguinte
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"keepalive={'true' if keepalive else 'false'}\n")
    return finish(args, utcnow(), lines, list(dict.fromkeys(alerts)))


def finish(args, now, lines, alerts, hard=False):
    for a in alerts:
        print(f"::warning::{a}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Jogos ao vivo — {now.astimezone(LISBON):%d/%m %H:%M} (Lisboa), modo {args.mode}\n\n")
            f.write("\n".join(f"- {l}" for l in lines) + "\n")
            if alerts:
                f.write("\n**Alertas**\n\n" + "\n".join(f"- ⚠️ {a}" for a in alerts) + "\n")
    if not alerts:
        return 0
    if args.mode != "run":
        return 1 if hard else 0
    # em produção: 1 e-mail de falha por problema persistente a cada ~30 min, não um por execução
    return 1 if (hard or now.minute % 30 < 5) else 0


# ----------------------------------------------------------------------------- autoteste

def selftest():
    ko = dt.datetime(2026, 10, 24, 14, 0, tzinfo=dt.timezone.utc)
    t = lambda mins: ko + dt.timedelta(minutes=mins)  # noqa: E731
    site0 = {"goalhome": None, "goalaway": None, "islive": 0}
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(("OK  " if good else "FALHA"), name, "" if good else f"\n   obtido={got}\n   esperado={want}")

    r = decide(site0, ("scheduled", None), None, t(-5), ko)
    check("antes do jogo não faz nada", (r[0], r[1], r[2]), (None, False, []))
    r = decide(site0, ("scheduled", None), None, t(0.1), ko)
    check("à hora do jogo (+6 s) ainda espera", (r[0], r[2]), (None, []))
    r = decide(site0, ("scheduled", None), None, t(1), ko)
    check("1 min depois da hora liga ao vivo a 0-0 (feed ainda agendado)", (r[0], len(r[2])), ({"goalhome": 0, "goalaway": 0, "islive": 1}, 0))
    r = decide({"goalhome": None, "goalaway": None, "islive": 1}, ("scheduled", None), None, t(1), ko)
    check("já ao vivo à mão mas sem marcador -> põe 0-0", (r[0], len(r[2])), ({"goalhome": 0, "goalaway": 0, "islive": 1}, 0))
    r = decide({"goalhome": 0, "goalaway": 0, "islive": 1}, ("scheduled", None), None, t(2), ko)
    check("já ao vivo e o feed atrasado -> não mexe", (r[0], len(r[2])), (None, 0))
    r = decide({"goalhome": 1, "goalaway": 0, "islive": 1}, ("scheduled", None), None, t(3), ko)
    check("feed atrasado não regride o marcador", (r[0], len(r[2])), (None, 0))
    r = decide(site0, ("scheduled", None), None, t(12), ko)
    check("10+ min com o feed ainda agendado -> liga e avisa", (r[0], len(r[2])), ({"goalhome": 0, "goalaway": 0, "islive": 1}, 1))
    r = decide(site0, ("live", (0, 0)), None, t(2), ko)
    check("arranque liga o ao vivo a 0-0", (r[0], r[1]), ({"goalhome": 0, "goalaway": 0, "islive": 1}, False))
    r = decide({"goalhome": 0, "goalaway": 0, "islive": 1}, ("live", (1, 0)), None, t(9), ko)
    check("golo atualiza marcador", (r[0], r[1]), ({"goalhome": 1}, False))
    r = decide({"goalhome": 1, "goalaway": 0, "islive": 1}, ("live", (1, 0)), None, t(10), ko)
    check("sem mudanças -> nada", (r[0], r[1]), (None, False))
    r = decide({"goalhome": 3, "goalaway": 1, "islive": 1}, ("finished", (4, 1)), None, t(70), ko)
    check("fim de jogo fecha e recalcula", (r[0], r[1]), ({"goalhome": 4, "islive": 0}, True))
    r = decide({"goalhome": 4, "goalaway": 1, "islive": 0}, ("finished", (4, 1)), None, t(75), ko)
    check("fim já gravado -> nada (não recalcula outra vez)", (r[0], r[1]), (None, False))
    r = decide({"goalhome": 2, "goalaway": 2, "islive": 1}, ("finished", None), None, t(70), ko)
    check("terminado sem resultado não fecha", (r[0], len(r[2])), (None, 1))
    r = decide(site0, ("other", None), None, t(30), ko)
    check("estado estranho (adiado) não mexe", (r[0], len(r[2])), (None, 1))
    r = decide(site0, None, None, t(30), ko)
    check("fora do feed -> alerta", (r[0], len(r[2])), (None, 1))
    rf_fin = {"status": "Finalizado", "score": (3, 2), "finished": True}
    r = decide({"goalhome": 2, "goalaway": 2, "islive": 1}, ("live", (2, 2)), rf_fin, t(70), ko)
    check("RFEF finalizado fecha mesmo que o feed ainda esteja em live", (r[0], r[1]), ({"goalhome": 3, "islive": 0}, True))
    r = decide(site0, ("finished", (3, 3)), rf_fin, t(70), ko)
    check("Flashscore e RFEF divergem -> usa RFEF + alerta", (r[0]["goalhome"], r[0]["goalaway"], len(r[2])), (3, 2, 1))

    feed = parse_feed("SA÷11¬~ZA÷X¬~AA÷abc¬AD÷1¬AB÷2¬AC÷12¬AG÷2¬AH÷1¬AE÷A¬AF÷B¬~AA÷def¬AB÷1¬AC÷1¬AE÷C¬AF÷D¬")
    check("parse feed live", classify_fs(feed["abc"]), ("live", (2, 1)))
    check("parse feed agendado", classify_fs(feed["def"]), ("scheduled", None))
    check("parse RFEF pré-jogo", parse_rfef('<span id="status_match" class="x">&nbsp;</span><span id="result_match"> - </span>')["score"], None)
    check("parse RFEF terminado", parse_rfef('<div id="status_match" class="ph5 bg-red">Finalizado</div><span id="result_match">3 - 2</span>'),
          {"status": "Finalizado", "score": (3, 2), "finished": True, "events": []})
    # --- eventos da RFEF e registo de golos (amostra real do El Ejido–Barça, 24/10/2026) ---
    sample = """ltable">
<div id="list_events_live" class="match-events">
<div class="event-content">
<div class="event">
<a href="https://futsal.rfef.es/jugador/vandeson-manuel-10932/10932">
<h5 class="ib va-m name">Vandeson <b class="block color-lightblue">[1-2]</b></h5>
<div class="ib va-m event-avatar">
<img src="https://t.resfu.com/img_data/players/medium/3716616.jpg" alt="Vandeson">
</div>
<img class="ib va-m event-ico" src="https://thumb2.besoccerapps.com/lnfs/img_web/events/accion1.png?v1" alt="Gol"/>
</a>
</div>
<div class="minute">7'</div>
<div class="event">
</div>
</div>
<div class="event-content">
<div class="event">
</div>
<div class="minute">6'</div>
<div class="event">
<a href="https://futsal.rfef.es/jugador/adolfo-12/12">
<img class="ib va-m event-ico" src="https://thumb2.besoccerapps.com/lnfs/img_web/events/accion1.png?v1" alt="Gol"/>
<div class="ib va-m event-avatar">
<img src="https://t.resfu.com/img_data/players/medium/3622219.jpg" alt="Adolfo">
</div>
<h5 class="ib va-m name">Adolfo <b class="block color-lightblue">[0-2]</b></h5>
</a>
</div>
</div>
<div class="event-content">
<div class="event">
</div>
<div class="minute">3'</div>
<div class="event">
<a href="https://futsal.rfef.es/jugador/antonio-845/845">
<img class="ib va-m event-ico" src="https://thumb2.besoccerapps.com/lnfs/img_web/events/accion1.png?v1" alt="Gol"/>
<div class="ib va-m event-avatar">
<img src="https://t.resfu.com/img_data/players/medium/3622212.jpg" alt="Antonio">
</div>
<h5 class="ib va-m name">Antonio <b class="block color-lightblue">[0-1]</b></h5>
</a>
</div>
</div>
</div>
</div>"""
    evs = parse_rfef_events(sample)
    check("RFEF: 3 golos lidos (minuto, lado, id RFEF)", [(e["minute"], e["side"], e["pid"]) for e in evs],
          [(7, "home", "10932"), (6, "away", "12"), (3, "away", "845")])
    check("RFEF: tipo do evento", {e["kind"] for e in evs}, {"Gol"})

    class FakeSyncMcp:
        apply = True

        def __init__(self):
            self.saved = []

        def rows(self, resource, where, columns=None, limit=100):
            if resource == "match_event" and "competition_id" in where:
                return [{"season_id": 42, "escalao_id": 1, "main_competition": 33, "main_competition_type": 1}]
            if resource == "match_event":
                return [{"person_id": 11072, "time": 3}]          # o golo do Antonio já está na ficha
            if resource == "squad":
                return [{"person_id": 14363}, {"person_id": 99999}]
            if resource == "person":
                return [{"id": 14363, "name": "Vandeson Silva"}, {"id": 99999, "name": "Outro Jogador"}]

        def person_by_external(self, source, ext):
            return {"845": 11072, "12": 469}.get(ext)

        def save_event(self, fields):
            self.saved.append((fields["person_id"], fields["team_id"], fields["time"], fields["event_id"]))
            return {"audit_id": 1}

    fm, al2 = FakeSyncMcp(), []
    n = sync_goals(fm, {"id": 63457, "comp": 2758}, {"gcteamhome_id": 3603, "gcteamaway_id": 44},
                   {"events": evs}, {"meta": {}, "person": {}, "squad": {}}, lambda msg, key=None: None, al2)
    check("golos novos registados (sem duplicar o Antonio)", (n, sorted(fm.saved)), (2, [(469, 44, 6, 6), (14363, 3603, 7, 6)]))
    check("sem alertas", al2, [])
    fm2 = FakeSyncMcp()
    n2 = sync_goals(fm2, {"id": 63457, "comp": 2758, "skip_goals": [["10932", 7]]}, {"gcteamhome_id": 3603, "gcteamaway_id": 44},
                    {"events": evs}, {"meta": {}, "person": {}, "squad": {}}, lambda msg, key=None: None, [])
    check("skip_goals ignora um golo da RFEF", (n2, sorted(fm2.saved)), (1, [(469, 44, 6, 6)]))
    # --- golos de Portugal pelo feed de incidências do Flashscore (amostra real, El Ejido–Barça 24/10/2026) ---
    fs_sample = """~III÷nkImgrSp¬IA÷2¬IB÷3'¬IE÷3¬IF÷Antonio¬IU÷¬ICT÷¬IK÷Goal¬IM÷C0z5m3DU¬
~III÷4OZxfYcs¬IA÷1¬IB÷7'¬IE÷3¬IF÷Wanderson Silva¬IU÷¬ICT÷¬IK÷Goal¬IM÷tnINfsPq¬
~III÷M1i8Z4BD¬IA÷2¬IB÷10'¬IE÷3¬IF÷Gonzalez S.¬IU÷¬ICT÷¬IK÷Goal¬IM÷byqhbehC¬
~III÷0zPsRNBt¬IA÷1¬IB÷12'¬IE÷1¬IF÷Rosa N.¬IU÷¬ICT÷¬IK÷Yellow Card¬IM÷655Mdw5m¬
~III÷WdnP0sro¬IA÷1¬IB÷32'¬IE÷3¬IF÷Juanico¬IU÷¬ICT÷¬IK÷Goal¬IM÷OCOwOKs0¬
~III÷lG3f1O1C¬IA÷2¬IB÷33'¬IE÷3¬IF÷Gonzalez S.¬IU÷¬ICT÷¬IK÷Goal¬IM÷byqhbehC¬"""
    fe = parse_fs_incidents(fs_sample)
    check("Flashscore: só golos (sem cartões), com lado e minuto", [(e["minute"], e["side"], e["name"]) for e in fe],
          [(3, "away", "Antonio"), (7, "home", "Wanderson Silva"), (10, "away", "Gonzalez S."), (32, "home", "Juanico"), (33, "away", "Gonzalez S.")])
    sq = [(1, norm_name("João Lopes \"Cigano\"")), (2, norm_name("Pedro Lopes")), (3, norm_name("Bebé")), (4, norm_name("Sérgio González")),
          (5, norm_name("Rúben Freire")), (6, norm_name("Rui Freire"))]
    check("nome com inicial: «Lopes J.» -> João Lopes", match_player("Lopes J.", sq), 1)
    check("nome com inicial ambíguo: «Freire R.» -> None", match_player("Freire R.", sq), None)
    check("nome só com apelido ambíguo: «Lopes» -> None", match_player("Lopes", sq), None)
    check("sem acentos: «Bebe» -> Bebé", match_player("Bebe", sq), 3)
    check("«Gonzalez S.» -> Sérgio González", match_player("Gonzalez S.", sq), 4)
    check("desconhecido -> None", match_player("Xavier M.", sq), None)
    # --- ficha do Flashscore (df_li): alinhamentos, golos e cartões (amostra real, Portimonense–Rio Ave 24/10/2026) ---
    fs_li_sample = "LB÷Starting Lineups¬LGT÷1¬LC÷1¬~LH÷0¬LP÷67v7PzK9¬LI÷de Andrade Pinheiro J. Q.¬LR÷(G)¬LS÷Goalkeeper¬LR÷(C)¬LS÷Captain¬NU÷¬LN÷de Andrade Pinheiro¬LJ÷2¬LO÷39¬LQ÷Brazil¬LK÷15¬LG÷1¬~LH÷1¬LP÷AsUPCT0c¬LI÷Dzyalochynsky A.¬NU÷¬LN÷Dzyalochynsky¬LJ÷17¬LO÷155¬LQ÷Portugal¬LK÷1¬LG÷1¬LII÷3¬LIT÷38'¬LIN÷Goal¬~LH÷2¬LP÷YNZw2wB9¬LI÷Gui Marques¬NU÷¬LN÷Gui Marques¬LJ÷51¬LO÷39¬LQ÷Brazil¬LK÷1¬LG÷1¬LII÷3¬LIT÷29'¬LIN÷Goal¬~LH÷3¬LP÷4EjCGzj2¬LI÷Souza A.¬NU÷¬LN÷Souza¬LJ÷33¬LO÷39¬LQ÷Brazil¬LK÷1¬LG÷1¬LII÷3¬LIT÷35'¬LIN÷Goal¬~LH÷4¬LP÷WYiBmuyM¬LI÷Vitinho¬NU÷¬LN÷Vitinho¬LJ÷94¬LO÷39¬LQ÷Brazil¬LK÷1¬LG÷1¬~LC÷2¬~LH÷0¬LP÷Umto1pk8¬LI÷Moreira¬LR÷(G)¬LS÷Goalkeeper¬NU÷¬LN÷Moreira¬LJ÷12¬LO÷155¬LQ÷Portugal¬LK÷15¬LG÷1¬~LH÷1¬LP÷d8sCOWyQ¬LI÷Bebe¬NU÷¬LN÷Godinho¬LJ÷13¬LO÷155¬LQ÷Portugal¬LK÷1¬LG÷1¬LII÷3¬LIT÷8'¬LIN÷Goal¬~LH÷2¬LP÷trdQTODd¬LI÷Castela M.¬NU÷¬LN÷Castela¬LJ÷27¬LO÷155¬LQ÷Portugal¬LK÷1¬LG÷1¬~LH÷3¬LP÷vVyfWS13¬LI÷Dorda M.¬NU÷¬LN÷Dorda¬LJ÷19¬LO÷22¬LQ÷Argentina¬LK÷1¬LG÷1¬~LH÷4¬LP÷jehrcOiK¬LI÷Rodrigues P.¬LR÷(C)¬LS÷Captain¬NU÷¬LN÷Rodrigues¬LJ÷10¬LO÷155¬LQ÷Portugal¬LK÷1¬LG÷1¬~LB÷Substitutes¬LGT÷1¬LC÷1¬~LH÷5¬LP÷SbpQx6k7¬LI÷Filipinho¬NU÷¬LN÷Filipinho¬LJ÷8¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷6¬LP÷4AhJS4FB¬LI÷Luiggi Baptista¬NU÷¬LN÷Luiggi Baptista¬LJ÷29¬LO÷39¬LQ÷Brazil¬LK÷2¬LG÷1¬LII÷3¬LIT÷25'¬LIN÷Goal¬LII÷3¬LIT÷28'¬LIN÷Goal¬~LH÷7¬LP÷jq2cQBdm¬LI÷Montanha¬NU÷¬LN÷Montanha¬LJ÷70¬LO÷39¬LQ÷Brazil¬LK÷2¬LG÷1¬~LH÷8¬LP÷IVKYc56U¬LI÷Reis T.¬NU÷¬LN÷Reis¬LJ÷10¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬LII÷3¬LIT÷14'¬LIN÷Goal¬~LH÷9¬LP÷pjqqgRZm¬LI÷Rochato A.¬NU÷¬LN÷Rochato¬LJ÷9¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷10¬LP÷WbLQVXYP¬LI÷Santi¬NU÷¬LN÷Santi¬LJ÷16¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷11¬LP÷6LVgNrck¬LI÷Serra A.¬NU÷¬LN÷Serra¬LJ÷11¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷12¬LP÷zZ292sa3¬LI÷Vieira A.¬NU÷¬LN÷Vieira¬LJ÷24¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LC÷2¬~LH÷5¬LP÷WGyHZt1q¬LI÷Angelo M.¬NU÷¬LN÷Angelo¬LJ÷14¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬LII÷3¬LIT÷17'¬LIN÷Goal¬~LH÷6¬LP÷pEVSWhu7¬LI÷Cigano¬NU÷¬LN÷Sousa¬LJ÷15¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷7¬LP÷GCl0VsjB¬LI÷Dinis Ramos¬NU÷¬LN÷Dinis Ramos¬LJ÷3¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷8¬LP÷SIvjB5ft¬LI÷Freire R.¬NU÷¬LN÷Freire¬LJ÷7¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷9¬LP÷6upDiL0M¬LI÷Ramos T.¬LR÷(G)¬LS÷Goalkeeper¬NU÷¬LN÷Ramos¬LJ÷1¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷10¬LP÷lnGlJZA7¬LI÷Rodrigues G.¬NU÷¬LN÷Rodrigues¬LJ÷8¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬LII÷2¬LIT÷34'¬LIN÷Red Card¬~LH÷11¬LP÷6oTVQOsK¬LI÷Sacramento T.¬LR÷(G)¬LS÷Goalkeeper¬NU÷¬LN÷Sacramento¬LJ÷33¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷12¬LP÷Oh4W6yo4¬LI÷Serginho¬NU÷¬LN÷Serginho¬LJ÷18¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~LH÷13¬LP÷COLP91yQ¬LI÷Silva F.¬NU÷¬LN÷Silva¬LJ÷4¬LO÷155¬LQ÷Portugal¬LK÷2¬LG÷1¬~A1÷9a6949c472e0532fc57e32c46f73c720¬~"
    lin = parse_fs_lineups(fs_li_sample)
    check("df_li: cinco inicial de cada equipa", ({s_: sum(1 for pl in lin[s_] if pl["starter"]) for s_ in lin}), {"home": 5, "away": 5})
    luiggi = [pl for pl in lin["home"] if pl["name"] == "Luiggi Baptista"][0]
    check("df_li: Luiggi Baptista #29 marcou aos 25' e 28' (suplente)", (luiggi["num"], luiggi["starter"], [(i["min"], i["kind"]) for i in luiggi["inc"]]),
          (29, False, [(25, "Goal"), (28, "Goal")]))
    check("df_li: cartão vermelho lido", [(pl["name"], i["min"]) for s_ in lin for pl in lin[s_] for i in pl["inc"] if i["kind"] == "Red Card"], [("Rodrigues G.", 34)])
    sqh = [(101, norm_name("Tomás Reis"), 10), (102, norm_name("Luiggi Baptista"), 29), (103, norm_name("Gui Marques"), 51),
           (104, norm_name("André Souza"), 33), (105, norm_name("Andriy Dzyalochynskyy"), 17)]
    check("resolve: «Reis T.» -> Tomás Reis", resolve_fs_player({"name": "Reis T.", "last": "Reis", "num": 10}, sqh), 101)
    check("resolve: «Souza A.» -> André Souza", resolve_fs_player({"name": "Souza A.", "last": "Souza", "num": 33}, sqh), 104)
    check("resolve: «Cavalcante D.» -> Djaelson (pelo nome FPF completo)", resolve_fs_player({"name": "Cavalcante D.", "last": "Cavalcante", "num": 92}, [(104, norm_name("Djaelson Djaelson Cavalcante Alves Filho"), 92)]), 104)
    check("resolve: grafia diferente («Dzyalochynsky» vs «Dzyalochynskyy») -> None", resolve_fs_player({"name": "Dzyalochynsky A.", "last": "Dzyalochynsky", "num": 17}, sqh), None)
    check("resolve: nome único manda se a camisola não bate com ninguém compatível", resolve_fs_player({"name": "Reis T.", "last": "Reis", "num": 29}, sqh), 101)
    sq_dup = [(201, norm_name("Rui Lopes"), 7), (202, norm_name("Rui Silva"), 9)]
    check("resolve: nome e camisola apontam para pessoas diferentes -> None", resolve_fs_player({"name": "Lopes R.", "last": "Silva", "num": 9}, sq_dup), None)

    class FakeSheetMcp:
        apply = True

        def __init__(self):
            self.saved = []

        def rows(self, resource, where, columns=None, limit=100):
            if resource == "match_event":
                return [{"person_id": 101, "event_id": 1, "time": 0}]      # Reis já convocado
            if resource == "squad" and where["gcteam_id"] == 108:
                return [{"person_id": 101, "number": 10}, {"person_id": 102, "number": 29}, {"person_id": 103, "number": 51}]
            if resource == "squad":
                return []
            if resource == "person":
                return [{"id": 101, "name": "Tomás Reis"}, {"id": 102, "name": "Luiggi Baptista"}, {"id": 103, "name": "Gui Marques"},
                        {"id": 104, "name": "Djaelson", "fpf_name": "Djaelson Cavalcante Alves Filho"}]

        def save_event(self, fields):
            self.saved.append((fields["person_id"], fields["event_id"], fields["time"]))
            return {"audit_id": 1}

    fsm, al3 = FakeSheetMcp(), []
    n3 = sync_fs_sheet(fsm, {"id": 31804, "comp": 1213}, {"gcteamhome_id": 108, "gcteamaway_id": 5}, lin,
                       {"squad3": {}, "fsplayer": {}}, lambda msg, key=None: None, al3, goals_cards=True)
    got = sorted(fsm.saved)
    check("ficha: convocados, cinco e golos em falta (Reis já convocado; Gui Marques no cinco)", got,
          sorted([(101, 6, 14), (102, 1, 0), (102, 6, 25), (102, 6, 28), (103, 1, 0), (103, 2, 0), (103, 6, 29)]))
    check("ficha: aviso de jogadores sem ligação certa", [a for a in al3 if "sem ligação" in a] != [], True)
    class FakeSheetMcp2(FakeSheetMcp):
        def rows(self, resource, where, columns=None, limit=100):
            if resource == "match_event":
                return [{"person_id": 102, "event_id": 6, "time": 24}]   # Luiggi: 1 golo (a 24'), vindo de outra fonte
            return FakeSheetMcp.rows(self, resource, where, columns, limit)
    fsm3 = FakeSheetMcp2()
    sync_fs_sheet(fsm3, {"id": 31804, "comp": 1213}, {"gcteamhome_id": 108, "gcteamaway_id": 5}, lin,
                  {"squad3": {}, "fsplayer": {}}, lambda msg, key=None: None, [], goals_cards=True)
    luiggi_goals = sorted(e for e in fsm3.saved if e[0] == 102 and e[1] == 6)
    check("ficha: se já há 1 golo do jogador (outro minuto), só acrescenta o que falta (1, não 2)", len(luiggi_goals), 1)
    fsm2 = FakeSheetMcp()
    sync_fs_sheet(fsm2, {"id": 63457, "comp": 2758, "rfef": "x"}, {"gcteamhome_id": 108, "gcteamaway_id": 5}, lin,
                  {"squad3": {}, "fsplayer": {}}, lambda msg, key=None: None, [], goals_cards=False)
    check("ficha (jogo com RFEF): só alinhamentos, sem golos/cartões", sorted(e for e in fsm2.saved if e[1] not in (1, 2)), [])
    check("parte: AC=13 (2.ª parte do Flashscore) -> 2", desired_part("13", "live", None), 2)
    check("parte: 1.ª parte aos 18' -> não mexe", desired_part("12", "live", 18), None)
    check("parte: algum evento depois dos 20' -> 2", desired_part("12", "live", 22), 2)
    check("parte: intervalo (AC=38) sem eventos novos -> não mexe", desired_part("38", "live", 20), None)
    check("parte: jogo terminado -> 2", desired_part("3", "finished", None), 2)
    ms0 = load_matches()
    g31804 = [m for m in ms0 if m["id"] == 31804]
    check("espera até à próxima janela (75 min)", idle_wait(g31804, set(), dt.datetime(2026, 10, 24, 12, 30, tzinfo=dt.timezone.utc)), 4500.0)
    check("janela longe (4h45) -> não mantém a cadeia", idle_wait(g31804, set(), dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.timezone.utc)), None)
    check("jogo já terminado não conta", idle_wait(g31804, {31804}, dt.datetime(2026, 10, 24, 12, 30, tzinfo=dt.timezone.utc)), None)
    ms = load_matches()
    check("jogos no ficheiro (18 + 3 do Brasil + 48 da Champions)", len(ms), 69)
    check("hora de verão: 15:00 de Lisboa = 14:00Z a 24/10", [m for m in ms if m["id"] == 31804][0]["ko_utc"], ko)
    check("depois da mudança de hora (26/10 21:15) = 21:15Z", [m for m in ms if m["id"] == 31806][0]["ko_utc"].hour, 21)
    # --- simulação completa: agendado -> ao vivo 0-0 -> 1-0 -> terminado 3-2, com feed e site falsos ---
    g = globals()
    saved = {k: g[k] for k in ("http_get", "Mcp", "NOW_FN")}
    saved_sleep, saved_argv, saved_tok = time.sleep, sys.argv, os.environ.get("NFP_MCP_TOKEN")
    clock = {"t": dt.datetime(2026, 10, 24, 13, 50, tzinfo=dt.timezone.utc)}  # 14:50 de Lisboa; jogo às 15:00 (14:00Z)
    site = {"goalhome": None, "goalaway": None, "islive": 0}
    calls = []
    part_state = {"v": 1}

    def fake_now():
        t = clock["t"]
        clock["t"] = t + dt.timedelta(seconds=20)
        return t

    def fake_get(url, headers=None, tries=2, timeout=20):
        t = clock["t"]
        hhmm = t.hour * 60 + t.minute
        if "flashscore" not in url:
            raise RuntimeError("fonte inesperada " + url)
        if "f_11_0_1_pt_1" in url:
            if hhmm < 14 * 60 + 2:
                row = "AB÷1¬AC÷1¬"
            elif hhmm < 14 * 60 + 10:
                row = "AB÷2¬AC÷12¬AG÷0¬AH÷0¬"
            elif hhmm < 14 * 60 + 25:
                row = "AB÷2¬AC÷13¬AG÷1¬AH÷0¬"
            else:
                row = "AB÷3¬AC÷3¬AG÷3¬AH÷2¬"
            return "SA÷11¬~ZA÷PORTUGAL: Liga Placard¬~AA÷QuI0zEK0¬" + row + "~"
        return "SA÷11¬~"

    class FakeMcp:
        def __init__(self, token, apply):
            self.apply = apply

        def get_match(self, mid):
            return {"date": "2026-10-24 15:00:00", "part": part_state["v"], **site}

        def save_match_fields(self, mid, fields):
            calls.append(("save_match", dict(fields)))
            part_state["v"] = fields["part"]
            return {"audit_id": 2}

        def update_match(self, mid, fields):
            calls.append(("update", dict(fields)))
            site.update(fields)
            return {"audit_id": 1}

        def recompute(self, comp):
            calls.append(("recompute", comp))

    try:
        g["http_get"], g["Mcp"], g["NOW_FN"] = fake_get, FakeMcp, fake_now
        time.sleep = lambda s_: None
        os.environ["NFP_MCP_TOKEN"] = "x" * 48
        sys.argv = ["live.py", "--mode", "run", "--only", "31804", "--loop-seconds", "7200", "--interval", "20"]
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main()
    finally:
        g.update(saved)
        time.sleep, sys.argv = saved_sleep, saved_argv
        if saved_tok is None:
            os.environ.pop("NFP_MCP_TOKEN", None)
        else:
            os.environ["NFP_MCP_TOKEN"] = saved_tok
    check("simulação: escritas por ordem", calls, [
        ("update", {"goalhome": 0, "goalaway": 0, "islive": 1}),
        ("save_match", {"part": 2}),
        ("update", {"goalhome": 1}),
        ("update", {"goalhome": 3, "goalaway": 2, "islive": 0}),
        ("recompute", 1213),
    ])
    check("simulação: estado final no site", site, {"goalhome": 3, "goalaway": 2, "islive": 0})
    check("simulação: termina sozinha, sem alertas", (rc, "0 jogo(s) ainda em vigilância" in buf.getvalue()), (0, True))
    print("\nTUDO OK" if ok else "\nHÁ FALHAS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
