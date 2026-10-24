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


def parse_rfef(html):
    st = re.search(r'id="status_match"[^>]*>\s*([^<]*?)\s*<', html)
    rs = re.search(r'id="result_match"[^>]*>\s*(\d+)\s*-\s*(\d+)', html)
    status = st.group(1) if st else ""
    return {"status": status, "score": (int(rs.group(1)), int(rs.group(2))) if rs else None,
            "finished": status.lower().startswith("finaliz")}


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
        return None, False, alerts, notes, False
    if status == "other":
        alerts.append("estado do feed não reconhecido (adiado, cancelado ou interrompido?): não toquei no jogo")
        return None, False, alerts, notes, False

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
        need = ["get_match", "update_match", "recompute_standings"]
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
            for d in (-1, 0, 1):
                feed.update(parse_feed(http_get(FS_FEED.format(d=d), FS_HEADERS)))
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
                site, ko = None, m["ko_utc"]
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
    r = decide(site0, ("scheduled", None), None, t(12), ko)
    check("10+ min depois sem arranque -> alerta", (r[0], len(r[2])), (None, 1))
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
          {"status": "Finalizado", "score": (3, 2), "finished": True})
    ms = load_matches()
    check("18 jogos no ficheiro", len(ms), 18)
    check("hora de verão: 15:00 de Lisboa = 14:00Z a 24/10", [m for m in ms if m["id"] == 31804][0]["ko_utc"], ko)
    check("depois da mudança de hora (26/10 21:15) = 21:15Z", [m for m in ms if m["id"] == 31806][0]["ko_utc"].hour, 21)
    # --- simulação completa: agendado -> ao vivo 0-0 -> 1-0 -> terminado 3-2, com feed e site falsos ---
    g = globals()
    saved = {k: g[k] for k in ("http_get", "Mcp", "NOW_FN")}
    saved_sleep, saved_argv, saved_tok = time.sleep, sys.argv, os.environ.get("NFP_MCP_TOKEN")
    clock = {"t": dt.datetime(2026, 10, 24, 13, 50, tzinfo=dt.timezone.utc)}  # 14:50 de Lisboa; jogo às 15:00 (14:00Z)
    site = {"goalhome": None, "goalaway": None, "islive": 0}
    calls = []

    def fake_now():
        t = clock["t"]
        clock["t"] = t + dt.timedelta(seconds=20)
        return t

    def fake_get(url, headers=None, tries=2, timeout=20):
        t = clock["t"]
        hhmm = t.hour * 60 + t.minute
        if "flashscore" not in url:
            raise RuntimeError("fonte inesperada " + url)
        if url.endswith("f_11_0_1_pt_1"):
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
            return {"date": "2026-10-24 15:00:00", **site}

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
