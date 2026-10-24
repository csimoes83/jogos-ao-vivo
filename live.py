#!/usr/bin/env python3
"""Operador de jogos ao vivo do futsalportugal.com (Liga Placard, Liga Feminina, Liga Prime Futsal).

Corre de 5 em 5 minutos num GitHub Action (sem depender de nenhum computador ligado):
  1. lê o estado e o resultado de cada jogo no feed de futsal do Flashscore (Portugal e Espanha)
     e, para Espanha, confirma na página oficial da RFEF;
  2. liga o «ao vivo» (islive=1) quando o jogo arranca e vai atualizando o marcador;
  3. no fim do jogo grava o resultado final, desliga o «ao vivo» e recalcula a classificação.

Só escreve no site pelo MCP (update_match, recompute_standings) e é idempotente: se o site já está
como o feed diz, não faz nada. Nunca fecha um jogo sem resultado conhecido.

Modos:  --mode probe  (só lê, nunca escreve)   --mode dry  (pede ao site um dry-run)   --mode run  (aplica)
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
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
POST = dt.timedelta(hours=5, minutes=30)  # deixa de o vigiar 5h30 depois
LATE_START = dt.timedelta(minutes=10)   # alerta se 10 min depois da hora o feed ainda não mostra arranque
PREFILTER = dt.timedelta(hours=12)      # só pergunta ao site por jogos perto da hora prevista


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
    """Devolve (campos_a_alterar | None, fechar_jogo, alertas, notas). Função pura (testável)."""
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
        return None, False, alerts, notes
    if status == "other":
        alerts.append("estado do feed não reconhecido (adiado, cancelado ou interrompido?): não toquei no jogo")
        return None, False, alerts, notes

    if status == "finished":
        if score is None:
            alerts.append("jogo terminado no feed mas sem resultado: não fechei")
            return None, False, alerts, notes
        want = {"goalhome": score[0], "goalaway": score[1], "islive": 0}
    else:  # live
        h, a = score if score else (0, 0)
        want = {"goalhome": h, "goalaway": a, "islive": 1}
        if site.get("goalhome") is not None and site.get("goalaway") is not None \
                and (h + a) < (site["goalhome"] + site["goalaway"]):
            notes.append("o feed tem menos golos que o site (golo anulado ou erro do feed)")

    diff = {k: v for k, v in want.items() if site.get(k) != v}
    return (diff or None), (status == "finished" and bool(diff)), alerts, notes


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["probe", "dry", "run"], default="run")
    ap.add_argument("--only", help="ids de jogos separados por vírgula")
    ap.add_argument("--ignore-window", action="store_true", help="ignora a janela horária (testes)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    now = dt.datetime.fromisoformat(os.environ["LIVE_NOW"]).astimezone(dt.timezone.utc) if os.environ.get("LIVE_NOW") \
        else dt.datetime.now(dt.timezone.utc)
    only = {int(x) for x in args.only.split(",")} if args.only else None
    probe = args.mode == "probe"
    matches = [m for m in load_matches() if (only is None or m["id"] in only)]
    if probe or args.ignore_window:
        cand = matches
    else:
        cand = [m for m in matches if m["ko_utc"] - PREFILTER <= now <= m["ko_utc"] + PREFILTER]
    lines, alerts = [], []

    def log(msg):
        stamp = now.astimezone(LISBON).strftime("%H:%M")
        print(f"[{stamp}] {msg}", flush=True)
        lines.append(msg)

    if not cand:
        log("nenhum jogo perto da hora: nada a fazer")
        return finish(args, now, lines, alerts)

    # 1) feed do Flashscore (ontem, hoje, amanhã)
    feed = {}
    try:
        for d in (-1, 0, 1):
            feed.update(parse_feed(http_get(FS_FEED.format(d=d), FS_HEADERS)))
    except Exception as e:  # noqa: BLE001
        alerts.append(f"feed do Flashscore indisponível: {e}")
        if not any(m.get("rfef") for m in cand):
            return finish(args, now, lines, alerts, hard=True)

    # 2) site
    token = os.environ.get("NFP_MCP_TOKEN", "").strip()
    mcp = Mcp(token, apply=(args.mode == "run")) if token else None
    if token:  # só a forma do segredo, nunca o valor: ajuda a distinguir «colou-se outra coisa» de «o site recusa»
        log(f"segredo: {len(token)} caracteres, {'só letras e números' if token.isalnum() else 'com outros caracteres (espaços, | ou símbolos)'}"
            f"{', com | (id do token à frente)' if '|' in token else ''}")
    if mcp is None:
        alerts.append("falta o segredo NFP_MCP_TOKEN: só consigo ler os feeds" if probe
                      else "falta o segredo NFP_MCP_TOKEN: não consigo escrever no site")
        if not probe:
            return finish(args, now, lines, alerts, hard=True)

    to_recompute = {}
    for m in cand:
        tag = f"{m['id']} {m['home']}–{m['away']}"
        try:
            fs = classify_fs(feed.get(m["fs"]))
            rf = None
            if m.get("rfef"):
                try:
                    rf = parse_rfef(http_get(m["rfef"], {"Accept-Language": "es-ES,es;q=0.9"}))
                except Exception as e:  # noqa: BLE001
                    log(f"{tag}: RFEF indisponível ({e}); sigo só com o Flashscore")
            site = None
            ko = m["ko_utc"]
            if mcp:
                raw = mcp.get_match(m["id"])
                site, ko = norm_site(raw), site_ko(raw)
                if ko != m["ko_utc"]:
                    log(f"{tag}: hora no site diferente da prevista ({raw['date']}); uso a do site")
            if probe:
                log(f"{tag}: feed={fs[0] if fs else 'ausente'} {fmt_score(fs[1] if fs else None)}"
                    f" | rfef={('%s %s' % (rf['status'] or '?', fmt_score(rf['score']))) if rf else '-'}"
                    f" | site={('islive=%s %s-%s' % (site['islive'], site['goalhome'], site['goalaway'])) if site else '?'}")
                continue
            if not (args.ignore_window or ko - PRE <= now <= ko + POST):
                continue
            fields, close, al, notes = decide(site, fs, rf, now, ko)
            alerts += [f"{tag}: {a}" for a in al]
            for n in notes:
                log(f"{tag}: nota: {n}")
            if not fields:
                log(f"{tag}: sem alterações (feed={fs[0] if fs else 'ausente'} {fmt_score(fs[1] if fs else None)})")
                continue
            res = mcp.update_match(m["id"], fields)
            log(f"{tag}: {json.dumps(fields)} -> {'APLICADO' if mcp.apply else 'dry-run'}"
                f"{' (audit ' + str(res.get('audit_id')) + ')' if res.get('audit_id') else ''}")
            if close:
                to_recompute[m["comp"]] = tag
        except (McpError, RuntimeError, KeyError, ValueError) as e:
            alerts.append(f"{tag}: erro: {e}")

    for comp, tag in to_recompute.items():
        try:
            mcp.recompute(comp)
            log(f"classificação da competição {comp} recalculada ({tag})")
        except McpError as e:
            alerts.append(f"competição {comp}: não consegui recalcular a classificação ({e}); fazer à mão")
    return finish(args, now, lines, alerts)


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
    print("\nTUDO OK" if ok else "\nHÁ FALHAS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
