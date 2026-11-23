#!/usr/bin/env python3
"""Operador nativo da UEFA (Champions de futsal): segue um jogo ao vivo só pela API pública da UEFA e escreve no site,
pelo MCP, o mesmo que se fazia à mão no Mac (uefa_vigia.py + uefa_incidencias.py), sem o Mac ligado.

O que faz, por jogo (um «passo» de cada vez, de 20 em 20 segundos dentro do live.py):
  - ao pontapé de saída (UEFA = LIVE) liga o «ao vivo» (islive=1) e põe o marcador (0-0);
  - mantém goalhome/goalaway = resultado da UEFA; no fim (FINISHED) grava o resultado, islive=0 e pede a classificação;
  - faltas de equipa da parte (foulshome/foulsaway, 0 a 5) e part=2 (com faltas a zero) no início da 2.ª parte;
  - golos e cartões (match_event) só de jogadores JÁ ligados à UEFA (external_ids source uefa); nunca cria pessoas;
  - texto nas incidências (match_event_text, extra «auto», 5 línguas): início, golos (sempre com texto), defesas a remates,
    penáltis falhados, 5.ª falta, intervalo, início da 2.ª parte e fim. Cartões NUNCA em texto.

Regras: nunca repete uma linha que já exista (confere match_id + event_id + minuto + equipa, e o tipo de texto), nunca apaga,
nunca cria pessoas, nunca fecha um jogo que a UEFA ainda dá ao vivo. Os textos saem de dados estruturados (quem, minuto,
resultado) com frases nossas; não se copia nenhum relato.

NÃO faz: fichas completas (convocados, cinco inicial, treinadores, árbitros) nem pessoas novas (import_sheets precisa de admin,
fica no Mac), auto-golos como evento de jogador (só o texto; o evento fica à mão), cartões a treinadores/staff, amarelos sem
jogador na UEFA, prolongamento/penáltis (avisa e não adivinha).
"""
import datetime as dt
import gzip
import json
import math
import os
import re
import sys
import unicodedata
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://match.uefa.com/v5/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15"

# nomes curtos usados nos textos (os outros clubes usam o nome internacional da UEFA); chave = id da equipa no site
NOME = {14: "Sporting", 712: "Stalitsa Minsk", 6: "Benfica", 1088: "HIT Kyiv"}
FASES = {"FIRST_HALF": 1, "SECOND_HALF": 2, "EXTRA_TIME_FIRST_HALF": 3, "EXTRA_TIME_SECOND_HALF": 4}
SEGUNDO_AMARELO = ("SECOND_YELLOW_CARD", "YELLOW_RED_CARD", "YELLOW_CARD_SECOND", "RED_YELLOW_CARD")
CARTOES = {"YELLOW_CARD": "yellow", "RED_CARD": "red", **{t: "second_yellow" for t in SEGUNDO_AMARELO}}
EV_PESSOA = {"goal": 6, "yellow": 3, "second_yellow": 4, "red": 5}   # event_id do site (match_event)
SEM_JOGO = ("POSTPONED", "CANCELLED", "ABANDONED", "SUSPENDED")
DEFESAS = True            # linhas «Defesa de X a remate de Y» (pedido do Carlos a 23/11/2026: «remates perigosos também»)
ASSOCIA_SEG = 5           # uma defesa liga-se ao remate à baliza do mesmo guarda-redes até 5 s antes
ASSIST_SEG = 4            # uma assistência liga-se ao golo da mesma equipa até 4 s antes/depois
SETTLE_SEG = 25           # o intervalo e o fim só se escrevem 25 s depois de a UEFA os registar (um golo atrasado entra antes)
SETTLE_GOLO = 6           # o golo só se escreve 6 s depois de registado: a assistência chega sempre ~1 s depois do golo
CAUDA_MIN = 20            # depois do fim continua a ver durante 20 min (a UEFA corrige dados depois do apito)
JOGO_MIN = 40


# ----------------------------------------------------------------------------- rede (API da UEFA)

def fetch(path, tries=3, timeout=25):
    """GET à API pública da UEFA (responde a runners do GitHub com o cabeçalho Origin)."""
    last = None
    for _ in range(tries):
        req = urllib.request.Request(API + path, headers={"User-Agent": UA, "Origin": "https://www.uefa.com", "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - qualquer falha de rede conta
            last = e
    raise RuntimeError(f"UEFA {path}: {last}")


def load_mapa():
    with open(os.path.join(HERE, "mapa.json"), encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------------- dados da UEFA

def minuto(t):
    t = t or {}
    return max(1, math.ceil((t.get("minute") or 0) + ((t.get("second") or 0) / 60.0)))


def seg(e):
    t = e.get("time") or {}
    return (t.get("minute") or 0) * 60 + (t.get("second") or 0)


def ordem(e):
    """Ordem do jogo. Os marcos (fim da parte, início da 2.ª, fim do jogo) nem sempre trazem o minuto: ficam no fim/início da sua parte."""
    f = FASES.get(e.get("phase"), 9)
    if e.get("type") == "FULL_TIME":
        return (99, 0)
    if e.get("type") == "END_PHASE":
        return (f, 10 ** 6)
    if e.get("type") in ("START_PHASE", "CHANGE_PHASE"):
        return (f, -1)
    return (f, seg(e))


def pessoa(a):
    """(id, nome, clubId, é_jogador) de um primaryActor/secondaryActor, plano ou aninhado (staff)."""
    p = (a or {}).get("person") or {}
    if "internationalName" in p:
        return str(p.get("id")), p["internationalName"], str(p.get("clubId") or ""), True
    q = p.get("person") or {}
    nm = ((q.get("translations") or {}).get("name") or {})
    return str(q.get("id") or ""), nm.get("EN") or next(iter(nm.values()), ""), "", False


def quando(e):
    ts = e.get("timestamp")
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def contexto(site_id, mapa, mu):
    """Liga o jogo da UEFA ao nosso: {hu, au, ours (id UEFA -> id do site), nome}. None se as equipas não baterem certo."""
    um, hid, aid, comp = mapa["jogos"][str(site_id)]
    hu, au = str(mu["homeTeam"]["id"]), str(mu["awayTeam"]["id"])
    eq = mapa["equipas_uefa"]
    if hu != eq.get(str(hid)) or au != eq.get(str(aid)):
        return None
    ours = {hu: int(hid), au: int(aid)}
    nome = {hu: NOME.get(int(hid)) or mu["homeTeam"].get("internationalName") or "", au: NOME.get(int(aid)) or mu["awayTeam"].get("internationalName") or ""}
    return {"um": um, "hid": int(hid), "aid": int(aid), "comp": int(comp), "hu": hu, "au": au, "ours": ours, "nome": nome}


# ----------------------------------------------------------------------------- textos (5 línguas, frases nossas)

def t5(pt, en, es, fr, it):
    return {"pt": pt, "en": en, "es": es, "fr": fr, "it": it}


def txt_inicio():
    return t5("Início do jogo.", "Kick-off.", "Comienza el partido.", "Coup d'envoi.", "Inizia la partita.")


def txt_intervalo(p):
    return t5(f"Intervalo: {p}.", f"Half-time: {p}.", f"Descanso: {p}.", f"Mi-temps : {p}.", f"Intervallo: {p}.")


def txt_segunda():
    return t5("Início da 2.ª parte.", "Start of the 2nd half.", "Comienza la 2.ª parte.", "Début de la 2e mi-temps.", "Inizia il secondo tempo.")


def txt_fim(p):
    return t5(f"Fim do jogo: {p}.", f"Full time: {p}.", f"Final del partido: {p}.", f"Fin du match : {p}.", f"Fine della partita: {p}.")


def txt_golo(equipa, jog, mi, placar, assist, via, primeiro):
    """via: None | 'pen' | 'dez' (livre de 10 metros). primeiro = abre o marcador."""
    vias = {None: ("", "", "", "", ""),
            "pen": (" (penálti)", " (penalty)", " (penalti)", " (penalty)", " (rigore)"),
            "dez": (" (livre de 10 metros)", " (10-metre free kick)", " (tiro libre de 10 metros)", " (coup franc à 10 mètres)", " (tiro libero dai 10 metri)")}[via]
    j = [jog + v for v in vias]
    a = ("", "", "", "", "") if not assist else (f" Assistência de {assist}.", f" Assist: {assist}.", f" Asistencia de {assist}.",
                                                   f" Passe décisive de {assist}.", f" Assist di {assist}.")
    if primeiro:
        return t5(f"Golo de {equipa}: {j[0]} abre o marcador aos {mi}'. {placar}.{a[0]}", f"Goal for {equipa}: {j[1]} opens the scoring at {mi}'. {placar}.{a[1]}",
                  f"Gol de {equipa}: {j[2]} abre el marcador al {mi}'. {placar}.{a[2]}", f"But de {equipa} : {j[3]} ouvre le score ({mi}'). {placar}.{a[3]}",
                  f"Gol di {equipa}: {j[4]} sblocca il risultato al {mi}'. {placar}.{a[4]}")
    return t5(f"Golo de {equipa}: {j[0]} marca aos {mi}'. {placar}.{a[0]}", f"Goal for {equipa}: {j[1]} scores at {mi}'. {placar}.{a[1]}",
              f"Gol de {equipa}: {j[2]} anota al {mi}'. {placar}.{a[2]}", f"But de {equipa} : {j[3]} marque ({mi}'). {placar}.{a[3]}",
              f"Gol di {equipa}: {j[4]} segna al {mi}'. {placar}.{a[4]}")


def txt_autogolo(jog, placar, mi):
    return t5(f"Auto-golo de {jog}: {placar} aos {mi}'.", f"Own goal by {jog}: {placar} at {mi}'.", f"Gol en propia de {jog}: {placar} al {mi}'.",
              f"But contre son camp de {jog} : {placar} ({mi}').", f"Autogol di {jog}: {placar} al {mi}'.")


def txt_penalti_falhado(jog, mi):
    return t5(f"Penálti falhado: {jog} não marca aos {mi}'.", f"Penalty missed: {jog} fails to score at {mi}'.", f"Penalti fallado: {jog} no marca al {mi}'.",
              f"Penalty raté : {jog} ne marque pas ({mi}').", f"Rigore sbagliato: {jog} non segna al {mi}'.")


def txt_defesa(gr, rem):
    return t5(f"Defesa de {gr} a remate de {rem}.", f"{gr} saves a shot from {rem}.", f"Parada de {gr} a un disparo de {rem}.",
              f"Arrêt de {gr} devant la frappe de {rem}.", f"Parata di {gr} sul tiro di {rem}.")


def txt_5falta(eq, adv):
    cap = lambda s: s[:1].upper() + s[1:]  # noqa: E731
    return t5(f"{cap(eq)} já leva 5 faltas: a seguinte é livre direto para {adv}.", f"{eq} are on 5 fouls: the next one is a direct free kick for {adv}.",
              f"{cap(eq)} ya suma 5 faltas: la siguiente es tiro libre directo para {adv}.", f"{eq} en est à 5 fautes : la suivante sera un coup franc direct pour {adv}.",
              f"{eq} è a 5 falli: il prossimo è un tiro libero diretto per {adv}.")


# ----------------------------------------------------------------------------- análise de um jogo

def analisar(mu, ev, ctx):
    """mu = matches/<id>, ev = lista de events?filter=ALL, ctx = contexto(). Devolve:
    estado, fase, placar (h, a) oficial, faltas (h, a) da parte (ou None), começou/2ª parte/fim, golos e cartões (jogadores)
    e as linhas de texto candidatas (ainda sem confrontar com o site)."""
    hu, au, ours, nome = ctx["hu"], ctx["au"], ctx["ours"], ctx["nome"]
    adv = {hu: au, au: hu}
    st = str(mu.get("status") or "")
    ev = sorted(ev, key=ordem)
    live, fim = st == "LIVE", st.startswith("FINISHED")
    sc = {hu: 0, au: 0}
    golos, cartoes, linhas = [], [], []
    usadas = set()          # ids de assistências/remates já ligados
    seq = [0]

    def linha(tipo, ord_, event_id, time_, team_uefa, texto, ts=None, settle=0, extra=None):
        seq[0] += 1
        linhas.append({"tipo": tipo, "ord": ord_, "seq": seq[0], "event_id": event_id, "time": time_, "team_id": ours[team_uefa], "texto": texto,
                       "ts": ts, "settle": settle, **(extra or {})})

    def placar_str(h, a):
        return f"{nome[hu]} {h}-{a} {nome[au]}"

    def equipa_de(a):
        _p, _n, club, ok = pessoa(a)
        return club if club in ours else str(((a or {}).get("team") or {}).get("id") or "")

    # --- golos (com marcador corrente), assistências ligadas por proximidade no tempo
    for e in ev:
        if e.get("type") != "GOAL":
            continue
        pid, pn, _club, _ok = pessoa(e.get("primaryActor"))
        team = equipa_de(e.get("primaryActor"))
        if team not in ours:
            continue
        gt = str(e.get("goalType") or e.get("subType") or "")
        proprio = "OWN" in gt
        ben = adv[team] if proprio else team
        sc[ben] += 1     # marcador corrente por contagem (o `totalScore` dos eventos da UEFA é o resultado FINAL do jogo, não o corrente)
        ass, melhor = "", None
        for a in ev:
            if a.get("type") != "ASSIST" or a.get("id") in usadas or a.get("phase") != e.get("phase"):
                continue
            ap, an, _c, _o = pessoa(a.get("primaryActor"))
            d = abs(seg(a) - seg(e))
            if equipa_de(a.get("primaryActor")) == team and ap != pid and d <= ASSIST_SEG and (melhor is None or d < melhor):
                ass, melhor, aid_ = an, d, a.get("id")
        if melhor is not None:
            usadas.add(aid_)
        mi = minuto(e.get("time"))
        h, a_ = sc[hu], sc[au]
        g = {"id": e.get("id"), "ord": ordem(e), "min": mi, "team": ben, "autor_team": team, "pid": pid, "nome": pn, "proprio": proprio, "placar": (h, a_)}
        golos.append(g)
        if proprio:
            linha("golo", ordem(e), 12, mi, ben, txt_autogolo(pn, placar_str(h, a_), mi), ts=quando(e), settle=SETTLE_GOLO, extra={"placar": (h, a_), "chave": f"{h}-{a_}"})
        else:
            via = "dez" if "PENALTY_SECOND_MARK" in gt else ("pen" if "PENALTY" in gt else None)
            linha("golo", ordem(e), 12, mi, ben, txt_golo(nome[ben], pn, mi, placar_str(h, a_), ass, via, h + a_ == 1), ts=quando(e), settle=SETTLE_GOLO,
                  extra={"placar": (h, a_), "chave": f"{h}-{a_}"})

    # --- cartões de jogadores (os do staff ficam à mão)
    vistos_c = set()
    for e in ev:
        kind = CARTOES.get(e.get("type"))
        if not kind:
            continue
        pid, pn, _club, ok = pessoa(e.get("primaryActor"))
        team = equipa_de(e.get("primaryActor"))
        if team not in ours:
            continue
        mi = minuto(e.get("time"))
        if not ok or not pid:
            cartoes.append({"tipo": kind, "pid": None, "nome": pn, "min": mi, "team": team})
            continue
        if (pid, kind, mi) in vistos_c:     # a UEFA publica o 2.º amarelo em dois eventos (SECOND_YELLOW + RED_YELLOW): fica um só
            continue
        vistos_c.add((pid, kind, mi))
        cartoes.append({"tipo": kind, "pid": pid, "nome": pn, "min": mi, "team": team})

    # --- penáltis falhados e defesas (cada defesa liga-se ao remate à baliza anterior do mesmo guarda-redes)
    penaltis = [e for e in ev if e.get("type") == "PENALTY" and e.get("subType") == "MISSED"]
    for e in penaltis:
        pid, pn, _c, ok = pessoa(e.get("primaryActor"))
        team = equipa_de(e.get("primaryActor"))
        if ok and team in ours:
            linha("penalti", ordem(e), 12, minuto(e.get("time")), team, txt_penalti_falhado(pn, minuto(e.get("time"))))
    if DEFESAS:
        # um remate à baliza que acabou em golo não é defesa; o guarda-redes do campo nem sempre é o que a UEFA põe no remate (trocas), por isso
        # a defesa liga-se ao último remate de um adversário nos 5 s anteriores
        golo_de = [(e.get("phase"), pessoa(e.get("primaryActor"))[0], seg(e)) for e in ev if e.get("type") == "GOAL"]
        remates = [e for e in ev if e.get("type") == "SHOT_ON_GOAL"
                   and not any(f == e.get("phase") and p == pessoa(e.get("primaryActor"))[0] and 0 <= t - seg(e) <= 3 for f, p, t in golo_de)]
        for e in ev:
            if e.get("type") != "SAVE":
                continue
            gid, gn, gclub, gok = pessoa(e.get("primaryActor"))
            if not gok or not gid:
                continue
            if any(p.get("phase") == e.get("phase") and 0 <= seg(e) - seg(p) <= 3 for p in penaltis):
                continue   # defesa de penálti: já tem a sua linha
            cand = [s for s in remates if s.get("id") not in usadas and s.get("phase") == e.get("phase") and 0 <= seg(e) - seg(s) <= ASSOCIA_SEG
                    and pessoa(s.get("primaryActor"))[3] and equipa_de(s.get("primaryActor")) in ours and equipa_de(s.get("primaryActor")) != (gclub if gclub in ours else equipa_de(e.get("primaryActor")))]
            if not cand:
                continue
            s = max(cand, key=seg)
            sid, sn, _sc, _sok = pessoa(s.get("primaryActor"))
            usadas.add(s.get("id"))
            linha("defesa", ordem(e), 12, minuto(e.get("time")), equipa_de(s.get("primaryActor")), txt_defesa(gn, sn))

    # --- 5.ª falta de cada equipa em cada parte
    faltas = {}
    for e in ev:
        if e.get("type") != "FOUL":
            continue
        team = equipa_de(e.get("primaryActor"))
        if team not in ours:
            continue
        k = (team, e.get("phase"))
        faltas[k] = faltas.get(k, 0) + 1
        if faltas[k] == 5:
            linha("falta5", ordem(e), 11, minuto(e.get("time")), team, txt_5falta(nome[team], nome[adv[team]]))

    # --- marcos: início, intervalo, início da 2.ª parte, fim
    def placar_ate(o):
        """marcador depois de todos os golos anteriores à ordem `o`"""
        r = (0, 0)
        for g in golos:
            if g["ord"] < o:
                r = g["placar"]
        return r

    oficial = (mu.get("score") or {}).get("regular") or (mu.get("score") or {}).get("total") or {}
    final = (oficial.get("home"), oficial.get("away")) if oficial.get("home") is not None and oficial.get("away") is not None else None
    ultimo = golos[-1]["placar"] if golos else (0, 0)
    placar = final if final is not None else ultimo
    # a 2.ª parte começou: START_PHASE (o CHANGE_PHASE vem 10 s antes, também já depois do intervalo), a fase do jogo, ou qualquer lance dela
    ha_2a = (mu.get("phase") == "SECOND_HALF" or fim
             or any(e.get("phase") == "SECOND_HALF" and e.get("type") not in ("CHANGE_PHASE", "END_PHASE") for e in ev))
    if live or fim:
        linha("inicio", (0, -5), 12, 1, hu, txt_inicio())
    e_ht = next((e for e in ev if e.get("type") == "END_PHASE" and e.get("phase") == "FIRST_HALF"), None)
    if e_ht is not None or ha_2a or mu.get("phase") == "HALF_TIME_BREAK":
        o = (1, 10 ** 6)
        h, a_ = placar_ate(o)
        linha("intervalo", o, 10, 20, hu, txt_intervalo(placar_str(h, a_)), ts=quando(e_ht) if e_ht else None, settle=SETTLE_SEG)
    e_s2 = next((e for e in ev if e.get("type") == "START_PHASE" and e.get("phase") == "SECOND_HALF"), None)
    if e_s2 is not None or ha_2a:
        linha("segunda", (2, -1), 12, 21, hu, txt_segunda())
    e_ft = next((e for e in ev if e.get("type") == "FULL_TIME"), None)
    if fim or e_ft is not None:
        linha("fim", (99, 0), 12, JOGO_MIN, hu, txt_fim(placar_str(*placar)), ts=quando(e_ft) if e_ft else None, settle=SETTLE_SEG)
    linhas.sort(key=lambda r: (r["ord"], r["seq"]))

    # --- faltas de equipa da parte: o campo `fouls` do jogo (a UEFA repõe-no sozinha) ou, sem ele, os eventos da parte em curso
    f = mu.get("fouls") or {}
    if f.get("home") is not None and f.get("away") is not None:
        faltas_parte = (int(f["home"]), int(f["away"]))
    elif live:
        fase = "SECOND_HALF" if ha_2a and mu.get("phase") != "HALF_TIME_BREAK" else "FIRST_HALF"
        faltas_parte = (sum(1 for e in ev if e.get("type") == "FOUL" and e.get("phase") == fase and equipa_de(e.get("primaryActor")) == hu),
                        sum(1 for e in ev if e.get("type") == "FOUL" and e.get("phase") == fase and equipa_de(e.get("primaryActor")) == au))
    else:
        faltas_parte = None
    prolong = any(FASES.get(e.get("phase"), 0) >= 3 for e in ev) or "EXTRA" in str(mu.get("phase") or "") or "PENALT" in str(mu.get("phase") or "")
    return {"estado": st, "fase": mu.get("phase"), "ao_vivo": live, "terminado": fim, "placar": placar, "placar_oficial": final, "faltas": faltas_parte,
            "segunda_parte": bool(ha_2a), "golos": golos, "cartoes": cartoes, "linhas": linhas, "prolongamento": prolong,
            "fim_em": quando({"timestamp": mu.get("fullTimeAt")}) if mu.get("fullTimeAt") else None}


# ----------------------------------------------------------------------------- o que já está no site

def norm(x):
    t = "".join(c for c in unicodedata.normalize("NFD", (x or "").lower()) if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


MARCOS = (("inicio", "inicio do jogo"), ("intervalo", "intervalo"), ("segunda", "inicio da 2"), ("fim", "fim do jogo"))


def pt_de(row):
    try:
        return json.loads(row.get("text") or "{}").get("pt") or ""
    except (TypeError, ValueError, AttributeError):
        return ""


def tipo_texto(row):
    """Que tipo de linha é uma linha já gravada (pelo texto em português); None = comentário livre (de uma pessoa)."""
    pt = pt_de(row)
    n = norm(pt)
    if int(row.get("event_id") or 0) == 11:
        return "falta5"
    for tipo, prefixo in MARCOS:
        if n.startswith(prefixo):
            return tipo
    if n.startswith("defesa de "):
        return "defesa"
    if n.startswith("penalti falhado"):
        return "penalti"
    if int(row.get("event_id") or 0) == 12 and re.search(r"(?<!\d)\d+-\d+(?!\d)", pt):
        return "golo"
    return None


def ja_existe(c, rows):
    """True se a linha candidata `c` já está no site (ou foi escrita neste processo). Regra: mesmo event_id + minuto + equipa."""
    pt = c["texto"]["pt"]
    slot = [r for r in rows if int(r.get("event_id") or 0) == c["event_id"] and int(r.get("time") or 0) == c["time"] and int(r.get("team_id") or 0) == c["team_id"]]
    if c["tipo"] in ("inicio", "intervalo", "segunda", "fim"):
        # um marco existe se há um marco do mesmo tipo (em qualquer minuto/equipa) ou um comentário livre no mesmo sítio
        return any(tipo_texto(r) == c["tipo"] for r in rows) or any(tipo_texto(r) is None for r in slot)
    if c["tipo"] == "golo":
        chave = re.compile(r"(?<!\d)" + re.escape(c["chave"]) + r"(?!\d)")
        # o marcador é único por golo: o mesmo marcador numa linha de golo = o mesmo golo (dois golos no mesmo minuto não se confundem)
        return any(tipo_texto(r) == "golo" and chave.search(pt_de(r)) for r in rows) or any(tipo_texto(r) is None for r in slot)
    if c["tipo"] == "defesa":
        return any(tipo_texto(r) == "defesa" and pt_de(r) == pt for r in slot) or any(tipo_texto(r) is None for r in slot)
    if c["tipo"] == "penalti":
        return any(tipo_texto(r) == "penalti" for r in slot) or any(tipo_texto(r) is None for r in slot)
    if c["tipo"] == "falta5":    # uma 5.ª falta por equipa e parte: aceita o minuto ±1 (também escrita à mão)
        return any(int(r.get("event_id") or 0) == 11 and int(r.get("team_id") or 0) == c["team_id"] and abs(int(r.get("time") or 0) - c["time"]) <= 1 for r in rows)
    return bool(slot)


def rows_all(mcp, resource, where, columns, page=100, cap=3000):
    out, off = [], 0
    while off < cap:
        rs = mcp.rows(resource, where, columns, limit=page, offset=off)
        out += rs
        if len(rs) < page:
            break
        off += page
    return out


# ----------------------------------------------------------------------------- o operador

class Operador:
    """Um operador por execução do live.py. `passo(m, now)` trata um jogo (entrada de matches.json com o campo «uefa»)."""

    def __init__(self, mcp, log, alerts, event_meta, apply, probe=False, fetch_fn=None, mapa=None):
        self.mcp, self.log, self.alerts, self.meta = mcp, log, alerts, event_meta
        self.apply, self.probe = apply, probe
        self.fetch = fetch_fn or fetch
        self.mapa = mapa or load_mapa()
        self.est = {}           # por jogo: o que já escreveu neste processo, assinatura da última leitura, lineups
        self._dry_feito = set()

    # -- escrita (o probe nunca escreve, nem em dry-run)
    def _escreve(self, descr, fn, *args):
        if self.probe:
            self.log(f"probe: escreveria {descr}", key=("probe", descr))
            return None
        if not self.apply:           # dry-run: o site não muda, por isso não repete o mesmo pedido a cada passo
            if descr in self._dry_feito:
                return {}
            self._dry_feito.add(descr)
        res = fn(*args)
        self.log(f"{descr} -> {'APLICADO' if self.apply else 'dry-run'}{' (audit ' + str(res.get('audit_id')) + ')' if isinstance(res, dict) and res.get('audit_id') else ''}")
        return res or {}

    def passo(self, m, now):
        """Devolve {'vigiar': bool, 'recompute': competição|None, 'resumo': {...}}. Nunca levanta erros de rede: avisa e tenta no passo seguinte."""
        mid, um = m["id"], m["uefa"]
        tag = f"{mid} {m.get('home', '?')}–{m.get('away', '?')} (UEFA {um})"
        est = self.est.setdefault(mid, {"escritas": [], "sig": None, "lineups": None, "pessoas": {}, "avisos": set(), "recomp": None})
        mp = self.mapa["jogos"].get(str(mid))
        if not mp or int(mp[0]) != int(um):
            self.alerts.append(f"{tag}: o jogo não está no uefa/mapa.json com este id da UEFA: não toquei no jogo")
            return {"vigiar": False, "recompute": None}
        try:
            mu = self.fetch(f"matches/{um}")
        except Exception as e:  # noqa: BLE001
            self.alerts.append(f"{tag}: API da UEFA indisponível ({e})")
            return {"vigiar": True, "recompute": None}
        ctx = contexto(mid, self.mapa, mu)
        if ctx is None:
            self.alerts.append(f"{tag}: as equipas da UEFA não batem com as nossas: não toquei no jogo")
            return {"vigiar": False, "recompute": None}
        st = str(mu.get("status") or "")
        if st in SEM_JOGO:
            if ("sem_jogo", st) not in est["avisos"]:
                est["avisos"].add(("sem_jogo", st))
                self.alerts.append(f"{tag}: jogo {st} na UEFA: não mexi (decidir à mão)")
            return {"vigiar": False, "recompute": None}
        if not (st == "LIVE" or st.startswith("FINISHED")):
            self.log(f"{tag}: UEFA {st or '?'} (ainda não começou)", key=("uefa-st", mid, st))
            return {"vigiar": True, "recompute": None}

        try:
            ev = self.fetch(f"matches/{um}/events?filter=ALL&offset=0&limit=500")
            if est["lineups"] is None or est["lineups"].get("lineupStatus") != "AVAILABLE":
                est["lineups"] = self.fetch(f"matches/{um}/lineups")
        except Exception as e:  # noqa: BLE001
            self.alerts.append(f"{tag}: eventos/alinhamentos da UEFA indisponíveis ({e})")
            return {"vigiar": True, "recompute": None}
        an = analisar(mu, ev, ctx)
        if an["prolongamento"]:
            if ("prolong",) not in est["avisos"]:
                est["avisos"].add(("prolong",))
                self.alerts.append(f"{tag}: prolongamento/penáltis na UEFA: o operador só trata os 40 minutos; o resto fica à mão")
        sig = (st, an["fase"], an["placar"], an["faltas"], len(ev), max([e.get("timestamp") or "" for e in ev] or [""]))
        agora_ok = est["sig"] == sig and est.get("limpo") and (now - est.get("quando", now)).total_seconds() < 120
        tail = an["fim_em"] is None or now < an["fim_em"] + dt.timedelta(minutes=CAUDA_MIN)
        vigiar = (not an["terminado"]) or tail
        if agora_ok:
            return {"vigiar": vigiar, "recompute": None}
        est["quando"] = now
        if self.mcp is None:
            self.log(f"{tag}: UEFA {st} {an['placar'][0]}-{an['placar'][1]} (sem acesso ao site: só leitura)", key=("uefa-ro", mid, an["placar"], st))
            return {"vigiar": vigiar, "recompute": None}

        res = {"vigiar": vigiar, "recompute": None}
        try:
            raw = self.mcp.get_match(mid)
            if int(raw["gcteamhome_id"]) != ctx["hid"] or int(raw["gcteamaway_id"]) != ctx["aid"]:
                self.alerts.append(f"{tag}: as equipas do site não batem com o mapa.json: não toquei no jogo")
                return {"vigiar": False, "recompute": None}
            n_antes = len(est["escritas"])
            res["recompute"] = self._campos(mid, ctx, raw, an, tag, est)
            self._pessoas(mid, m, ctx, an, tag, est)
            adiado = self._textos(mid, ctx, an, tag, est, now)
        except Exception as e:  # noqa: BLE001 - um erro do site não derruba a vigilância (tenta outra vez no passo seguinte)
            self.alerts.append(f"{tag}: erro a escrever no site: {e}")
            est["limpo"] = False
            return res
        est["sig"] = sig
        est["limpo"] = not adiado
        res["resumo"] = {"estado": st, "placar": an["placar"], "escritas": len(est["escritas"]) - n_antes}
        self.log(f"{tag}: UEFA {st} {an['placar'][0]}-{an['placar'][1]}{' (falta %d-%d)' % an['faltas'] if an['faltas'] and an['ao_vivo'] else ''}",
                 key=("uefa-estado", mid, st, an["placar"], an["fase"]))
        return res

    # -- marcador, ao vivo, parte e faltas
    def _campos(self, mid, ctx, raw, an, tag, est):
        site = {"goalhome": _int(raw.get("goalhome")), "goalaway": _int(raw.get("goalaway")), "islive": _int(raw.get("islive")) or 0}
        h, a = an["placar"]
        want = {"goalhome": h, "goalaway": a, "islive": 1 if an["ao_vivo"] else 0}
        if an["ao_vivo"] and site["goalhome"] is not None and site["goalaway"] is not None and h + a < site["goalhome"] + site["goalaway"]:
            self.alerts.append(f"{tag}: a UEFA tem menos golos ({h}-{a}) que o site ({site['goalhome']}-{site['goalaway']}): golo anulado? rever à mão")
        if an["placar_oficial"] is not None and an["golos"] and tuple(an["golos"][-1]["placar"]) != tuple(an["placar_oficial"]):
            self.alerts.append(f"{tag}: o resultado da UEFA {h}-{a} não bate com o último golo {an['golos'][-1]['placar'][0]}-{an['golos'][-1]['placar'][1]}: confirmar")
        diff = {k: v for k, v in want.items() if site.get(k) != v}
        fechou = False
        if diff:
            fechou = an["terminado"]
            self._escreve(f"{tag}: update_match {json.dumps(diff)}", self.mcp.update_match, mid, diff)
        # 2.ª parte (part=2, faltas a zero) e contador de faltas da parte
        part = _int(raw.get("part")) or 1
        fh, fa = _int(raw.get("foulshome")) or 0, _int(raw.get("foulsaway")) or 0
        campos = {}
        if an["segunda_parte"] and part < 2:
            campos["part"] = 2
            if an["ao_vivo"]:
                campos["foulshome"], campos["foulsaway"] = (min(5, an["faltas"][0]), min(5, an["faltas"][1])) if an["faltas"] else (0, 0)
        elif an["ao_vivo"] and an["faltas"] is not None:
            w = (min(5, an["faltas"][0]), min(5, an["faltas"][1]))
            if (fh, fa) != w:
                campos["foulshome"], campos["foulsaway"] = w
        if campos:
            self._escreve(f"{tag}: match {json.dumps(campos)}", self.mcp.save_match_fields, mid, campos)
        return ctx["comp"] if (diff and fechou) else None

    # -- golos e cartões dos jogadores (só os já ligados à UEFA; nunca cria pessoas)
    def _pessoas(self, mid, m, ctx, an, tag, est):
        evs = [(g, "goal") for g in an["golos"] if not g["proprio"]] + [(c, c["tipo"]) for c in an["cartoes"]]
        for g in an["golos"]:
            if g["proprio"]:
                self.log(f"{tag}: auto-golo de {g['nome']} aos {g['min']}' — o evento do jogador fica À MÃO (o texto e o marcador já saem)", key=("own", mid, g["id"]))
        if not evs:
            return
        meta = self.meta.get(ctx["comp"])
        if not meta:
            self.alerts.append(f"{tag}: competição {ctx['comp']} sem campos de época configurados; golos/cartões não registados")
            return
        lu = est["lineups"] or {}
        ficha = set()
        if lu.get("lineupStatus") == "AVAILABLE":
            for lado in ("homeTeam", "awayTeam"):
                for x in (lu.get(lado) or {}).get("field", []) + (lu.get(lado) or {}).get("bench", []):
                    ficha.add(str(x["player"]["id"]))
        rows = rows_all(self.mcp, "match_event", {"match_id": mid}, ["person_id", "event_id", "time"])
        have = {(r["person_id"], r["event_id"], r["time"]) for r in rows}
        have_n = {}
        for r in rows:
            have_n[(r["person_id"], r["event_id"])] = have_n.get((r["person_id"], r["event_id"]), 0) + 1
        por = {}
        for e, kind in evs:
            if not e["pid"]:
                self.log(f"{tag}: {kind} aos {e['min']}' sem jogador na UEFA ({e['nome']}) — VER À MÃO", key=("sem-jog", mid, kind, e["min"]))
                continue
            por.setdefault((e["pid"], kind, e["team"]), []).append(e["min"])
        for (upid, kind, team), mins in por.items():
            if ficha and upid not in ficha:
                self.log(f"{tag}: {kind} de {upid} fora da ficha da UEFA — VER À MÃO", key=("fora", mid, upid, kind))
                continue
            pid = est["pessoas"].get(upid)
            if not pid:       # só se guarda quem já está ligado: se o Mac ligar o jogador a meio, o passo seguinte já o encontra
                pid = self.mcp.person_by_external("uefa", upid)
                if pid:
                    est["pessoas"][upid] = pid
            if not pid:
                self.log(f"{tag}: {kind} aos {sorted(mins)}' sem ligação certa ao jogador (UEFA {upid}) — VER À MÃO (a ficha completa vem do Mac)", key=("sem-lig", mid, upid, kind))
                continue
            ev_id = EV_PESSOA[kind]
            falta = [mn for mn in sorted(mins) if (pid, ev_id, mn) not in have][: max(0, len(mins) - have_n.get((pid, ev_id), 0))]
            for mn in falta:
                f = {"competition_id": ctx["comp"], **meta, "match_id": mid, "team_id": ctx["ours"][team], "person_id": pid, "event_id": ev_id, "time": mn}
                self._escreve(f"{tag}: {kind} {mn}' (pessoa {pid})", self.mcp.save_event, f)
                have.add((pid, ev_id, mn))
                have_n[(pid, ev_id)] = have_n.get((pid, ev_id), 0) + 1
                est["escritas"].append(("pessoa", pid, ev_id, mn))

    # -- texto nas incidências
    def _textos(self, mid, ctx, an, tag, est, now):
        rows = rows_all(self.mcp, "match_event_text", {"match_id": mid}, ["id", "event_id", "time", "team_id", "text"])
        rows += est.setdefault("rows_novas", [])      # o que já escrevi neste processo (também em dry-run)
        adiado = False
        for c in an["linhas"]:
            if ja_existe(c, rows):
                continue
            if c["settle"] and c["ts"] is not None and -120 < (now - c["ts"]).total_seconds() < c["settle"]:
                adiado = True       # espera um passo (a assistência chega ~1 s depois do golo; um golo registado depois do apito entra antes do fim)
                break               # e não escreve nada que venha depois, para as linhas ficarem pela ordem do jogo
            texto = json.dumps(c["texto"], ensure_ascii=False)
            fields = {"match_id": mid, "team_id": c["team_id"], "event_id": c["event_id"], "time": c["time"], "text": texto, "extra": "auto"}
            self._escreve(f"{tag}: texto {c['tipo']} {c['time']}' «{c['texto']['pt'][:70]}»", self._salva_texto, fields)
            novo = {"event_id": c["event_id"], "time": c["time"], "team_id": c["team_id"], "text": texto}
            rows.append(novo)
            if not self.probe:
                est["rows_novas"].append(novo)
                est["escritas"].append(("texto", c["tipo"], c["time"]))
        return adiado

    def _salva_texto(self, fields):
        return self.mcp.call("record_save", {"resource": "match_event_text", "confirm": self.apply, "fields": fields})


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def ativo(m, padrao, now):
    """O operador da UEFA só atua num jogo quando passou o `uefa_from_utc` desse jogo (ou, sem ele, o padrão do matches.json).
    Sem nenhum dos dois não atua: é o interruptor que evita escritas em duplicado com o Mac."""
    v = m.get("uefa_from_utc") or padrao
    if not v:
        return False
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return False
    return now >= (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc))      # sem fuso = UTC


# ----------------------------------------------------------------------------- autoteste (chamado por `python3 live.py --selftest`)

def _carrega(i):
    with open(os.path.join(HERE, "testdata", f"{i}.json"), encoding="utf-8") as f:
        return json.load(f)


def _foto(d, ate, mapa):
    """O jogo `d` (fixture real da UEFA) tal como a API o mostraria no instante `ate` (datetime UTC): eventos até aí, estado, fase, marcador e faltas."""
    m = json.loads(json.dumps(d["match"]))
    hu, au = str(m["homeTeam"]["id"]), str(m["awayTeam"]["id"])
    ev = [e for e in d["events"] if (quando(e) or ate) <= ate]
    ko = dt.datetime.fromisoformat(m["kickOffTime"]["dateTime"].replace("Z", "+00:00"))
    fim = dt.datetime.fromisoformat(m["fullTimeAt"].replace("Z", "+00:00"))
    if ate < ko - dt.timedelta(seconds=30):
        m.update(status="UPCOMING", phase=None, score=None, fouls=None)
        return m, []
    tipos = {(e["type"], e["phase"]) for e in ev}
    fase = "FIRST_HALF"
    if ("END_PHASE", "FIRST_HALF") in tipos:
        fase = "HALF_TIME_BREAK"
    if ("START_PHASE", "SECOND_HALF") in tipos:
        fase = "SECOND_HALF"
    h = a = fh = fa = 0
    for e in ev:
        club = str(((e.get("primaryActor") or {}).get("person") or {}).get("clubId") or "")
        if e["type"] == "GOAL":
            if club == hu:
                h += 1
            else:
                a += 1
        if e["type"] == "FOUL" and (e["phase"] == fase or (fase == "HALF_TIME_BREAK" and e["phase"] == "FIRST_HALF")):
            if club == hu:
                fh += 1
            else:
                fa += 1
    m["score"] = {"regular": {"home": h, "away": a}, "total": {"home": h, "away": a}}
    if ate >= fim:
        m.update(status="FINISHED", phase=None, fouls=None)
    else:
        m.update(status="LIVE", phase=fase, fouls={"home": fh, "away": fa})
        m.pop("fullTimeAt", None)
    return m, ev


class _SiteFalso:
    """Um site de brincar: guarda o que o operador escreve e responde ao que ele lê."""
    apply = True

    def __init__(self, mid, hid, aid, texts=None, events=None):
        self.match = {"id": mid, "gcteamhome_id": hid, "gcteamaway_id": aid, "goalhome": None, "goalaway": None, "islive": 0, "part": 1, "foulshome": 0, "foulsaway": 0}
        self.texts, self.events, self.writes, self.recomputed = list(texts or []), list(events or []), [], []
        self.pessoas = {}

    def get_match(self, mid):
        return dict(self.match)

    def rows(self, resource, where, columns=None, limit=100, offset=0):
        rs = {"match_event_text": self.texts, "match_event": self.events}[resource]
        return [dict(r) for r in rs][offset: offset + limit]

    def person_by_external(self, source, ext):
        return self.pessoas.get(str(ext))

    def recompute(self, comp):
        self.recomputed.append(comp)
        return {}

    def update_match(self, mid, fields):
        self.writes.append(("update", dict(fields)))
        self.match.update(fields)
        return {"audit_id": 1}

    def save_match_fields(self, mid, fields):
        self.writes.append(("match", dict(fields)))
        self.match.update(fields)
        return {"audit_id": 2}

    def save_event(self, fields):
        self.writes.append(("evento", fields["person_id"], fields["event_id"], fields["time"]))
        self.events.append({"person_id": fields["person_id"], "event_id": fields["event_id"], "time": fields["time"]})
        return {"audit_id": 3}

    def call(self, name, args):
        assert name == "record_save" and args["resource"] == "match_event_text" and args["confirm"] is True
        f = args["fields"]
        assert f["extra"] == "auto" and set(json.loads(f["text"])) == {"pt", "en", "es", "fr", "it"}
        self.writes.append(("texto", f["event_id"], f["time"], f["team_id"], json.loads(f["text"])["pt"]))
        self.texts.append({"event_id": f["event_id"], "time": f["time"], "team_id": f["team_id"], "text": f["text"]})
        return {"audit_id": 4}


def selftest(check):
    mapa = load_mapa()
    d85, d87 = _carrega(2050885), _carrega(2050887)
    # --- análise do Beitar–Sporting 2-18 (jogo real de 23/11/2026): as mesmas linhas que se escreviam à mão
    ctx = contexto(66614, mapa, d85["match"])
    an = analisar(d85["match"], d85["events"], ctx)
    pts = [(l["tipo"], l["time"], l["team_id"], l["texto"]["pt"]) for l in an["linhas"]]
    check("UEFA: 20 golos = 20 linhas de golo", sum(1 for t in pts if t[0] == "golo"), 20)
    check("UEFA: golo com marcador corrente e assistência", ("golo", 6, 14, "Golo de Sporting: Erick marca aos 6'. TFK Beitar 0-3 Sporting. Assistência de Ivan Chishkala.") in pts, True)
    check("UEFA: o 1.º golo abre o marcador", [t[3] for t in pts if t[0] == "golo"][0], "Golo de Sporting: Wesley França abre o marcador aos 1'. TFK Beitar 0-1 Sporting. Assistência de Bruno Pinto.")
    check("UEFA: dois golos no mesmo minuto levam marcadores diferentes", [t[3].split(". ")[1] for t in pts if t[0] == "golo" and t[1] == 1], ["TFK Beitar 0-1 Sporting", "TFK Beitar 0-2 Sporting."])
    check("UEFA: golo de livre de 10 metros", any("(livre de 10 metros) marca aos 37'. TFK Beitar 2-16 Sporting." in t[3] for t in pts), True)
    check("UEFA: intervalo com o marcador da 1.ª parte", ("intervalo", 20, 3602, "Intervalo: TFK Beitar 1-9 Sporting.") in pts, True)
    check("UEFA: fim do jogo com o resultado", ("fim", 40, 3602, "Fim do jogo: TFK Beitar 2-18 Sporting.") in pts, True)
    check("UEFA: 5.ª falta do Beitar aos 30'", ("falta5", 30, 3602, "TFK Beitar já leva 5 faltas: a seguinte é livre direto para Sporting.") in pts, True)
    check("UEFA: penálti falhado", ("penalti", 34, 14, "Penálti falhado: Bruno Pinto não marca aos 34'.") in pts, True)
    check("UEFA: defesa a remate (sem clubes entre parênteses)", ("defesa", 3, 14, "Defesa de Maksims Troickis a remate de Bruno Pinto.") in pts, True)
    check("UEFA: nenhuma linha de cartão em texto", [l for l in an["linhas"] if l["event_id"] in (3, 4, 5)], [])
    check("UEFA: nenhum texto com clube entre parênteses", [t[3] for t in pts if "(Sporting)" in t[3] or "(TFK" in t[3]], [])
    check("UEFA: o último golo conta o resultado oficial", (an["golos"][-1]["placar"], an["placar_oficial"]), ((2, 18), (2, 18)))
    check("UEFA: 2 amarelos de jogadores", [(c["tipo"], c["min"]) for c in an["cartoes"]], [("yellow", 30), ("yellow", 34)])
    ctx7 = contexto(66616, mapa, d87["match"])
    an7 = analisar(d87["match"], d87["events"], ctx7)
    check("UEFA: amarelo ao treinador/staff fica sem jogador (à mão)", [c["pid"] is None for c in an7["cartoes"]], [False, False, True])
    check("UEFA: Kauno–Benfica 2 faltas 5.ª (uma por equipa)", sorted(l["team_id"] for l in an7["linhas"] if l["tipo"] == "falta5"), [6, 891])
    check("UEFA: equipas que não batem -> sem contexto", contexto(66614, mapa, {"homeTeam": {"id": 1}, "awayTeam": {"id": 2}}), None)

    # --- deteção do que já existe (linhas das mesmas chaves do site: event_id + minuto + equipa)
    H = lambda ev, t, tm, pt: {"event_id": ev, "time": t, "team_id": tm, "text": json.dumps({"pt": pt}, ensure_ascii=False)}  # noqa: E731
    cand = {(l["tipo"], l["time"], l["team_id"], l["texto"]["pt"][:22]): l for l in an["linhas"]}
    g1 = [l for l in an["linhas"] if l["tipo"] == "golo" and l["time"] == 1]
    rows = [H(12, 1, 14, "Wesley França abre o marcador aos 1'. TFK Beitar 0-1 Sporting.")]
    check("existe: o golo 0-1 já escrito (outro formato) não se repete, o 0-2 do mesmo minuto escreve-se", [ja_existe(c, rows) for c in g1], [True, False])
    check("existe: comentário livre no mesmo minuto/equipa impede o golo", ja_existe([l for l in an["linhas"] if l["tipo"] == "golo" and l["time"] == 13][0], [H(12, 13, 14, "Golaço!")]), True)
    check("existe: golo e defesa no mesmo minuto convivem", ja_existe([l for l in an["linhas"] if l["tipo"] == "defesa" and l["time"] == 9][0], [H(12, 9, 14, "Golo de Sporting: X marca aos 9'. TFK Beitar 0-5 Sporting.")]), False)
    check("existe: o mesmo marco noutro minuto continua a ser o marco", ja_existe([l for l in an["linhas"] if l["tipo"] == "inicio"][0], [H(12, 0, 3602, "Início do jogo.")]), True)
    check("existe: 5.ª falta ±1 minuto", ja_existe([l for l in an["linhas"] if l["tipo"] == "falta5"][0], [H(11, 31, 3602, "x")]), True)

    # --- simulação completa do Kauno–Benfica (66616) com a API e o site falsos, passo a passo (20 s) — o site começa vazio
    ko = dt.datetime.fromisoformat(d87["match"]["kickOffTime"]["dateTime"].replace("Z", "+00:00"))
    fim = dt.datetime.fromisoformat(d87["match"]["fullTimeAt"].replace("Z", "+00:00"))
    agora = [ko - dt.timedelta(minutes=10)]

    def fetch_falso(path):
        m, ev = _foto(d87, agora[0], mapa)
        if path.endswith("/lineups"):
            return d87["lineups"]
        if "/events" in path:
            return ev
        return m

    site = _SiteFalso(66616, 891, 6)
    site.pessoas = {"250170576": 7001, "0": 0}      # só o Kutchy (Benfica) está ligado à UEFA
    logs, alertas = [], []
    op = Operador(site, lambda msg, key=None: logs.append(msg), alertas, {2853: {"season_id": 42, "escalao_id": 1, "main_competition": 57, "main_competition_type": 2}},
                  apply=True, fetch_fn=fetch_falso, mapa=mapa)
    entrada = {"id": 66616, "home": "Kauno", "away": "Benfica", "uefa": 2050887}
    recomp, passos, vigia_fim, antes = [], 0, None, None
    while agora[0] < fim + dt.timedelta(minutes=25):
        if antes is None and agora[0] >= ko - dt.timedelta(seconds=40):
            antes = len(site.writes)         # tudo o que se escreveu até ao pontapé de saída
        r = op.passo(entrada, agora[0])
        passos += 1
        if r.get("recompute"):
            recomp.append(r["recompute"])
        if not r["vigiar"]:
            vigia_fim = agora[0]
            break
        agora[0] += dt.timedelta(seconds=20)
    ws = site.writes
    check("sim UEFA: antes do KO (jogo UPCOMING) não escreve nada", antes, 0)
    check("sim UEFA: ao KO liga o ao vivo a 0-0", ws[0], ("update", {"goalhome": 0, "goalaway": 0, "islive": 1}))
    check("sim UEFA: o 1.º texto é o início do jogo (min 1, equipa da casa)", ws[1][:4], ("texto", 12, 1, 891))
    check("sim UEFA: marcador final e jogo fechado", (site.match["goalhome"], site.match["goalaway"], site.match["islive"], site.match["part"]), (2, 3, 0, 2))
    check("sim UEFA: pediu a classificação 1 vez (competição 2853)", recomp, [2853])
    textos = [w for w in ws if w[0] == "texto"]
    chaves = [(w[1], w[2], w[3], w[4]) for w in textos]
    check("sim UEFA: nenhuma linha repetida", len(chaves), len(set(chaves)))
    check("sim UEFA: a ordem é a do jogo (início, golos, intervalo, 2.ª parte, fim)", [w[4] for w in textos if w[4].startswith(("Início", "Intervalo", "Fim", "Golo"))],
          ["Início do jogo.", "Golo de Kauno Žalgiris: Albert Voskunovič abre o marcador aos 10'. Kauno Žalgiris 1-0 Benfica. Assistência de Ignas Raštutis.",
           "Golo de Benfica: Carlos Monteiro marca aos 16'. Kauno Žalgiris 1-1 Benfica. Assistência de Rúben Góis.", "Intervalo: Kauno Žalgiris 1-1 Benfica.", "Início da 2.ª parte.",
           "Golo de Benfica: Carlos Monteiro marca aos 25'. Kauno Žalgiris 1-2 Benfica. Assistência de Raúl Moreira.",
           "Golo de Benfica: Rúben Góis marca aos 30'. Kauno Žalgiris 1-3 Benfica. Assistência de Kutchy.",
           "Golo de Kauno Žalgiris: Francisco Gustavo Da Costa marca aos 37'. Kauno Žalgiris 2-3 Benfica. Assistência de Vinicius Carvalho.", "Fim do jogo: Kauno Žalgiris 2-3 Benfica."])
    check("sim UEFA: part=2 com as faltas a zero no início da 2.ª parte", [w for w in ws if w[0] == "match" and w[1].get("part") == 2][0][1].get("foulshome", 0) in (0, 1), True)
    check("sim UEFA: faltas da parte nunca passam de 5", all(max(w[1].get("foulshome", 0), w[1].get("foulsaway", 0)) <= 5 for w in ws if w[0] == "match"), True)
    check("sim UEFA: só o Kutchy (ligado) tem o evento de amarelo; os outros ficam à mão", [w for w in ws if w[0] == "evento"], [("evento", 7001, 3, 24)])
    check("sim UEFA: acaba de vigiar 20 min depois do apito", vigia_fim is not None and vigia_fim >= fim + dt.timedelta(minutes=20), True)
    check("sim UEFA: sem alertas", alertas, [])
    n = len(ws)
    agora[0] = fim + dt.timedelta(minutes=3)
    op2 = Operador(site, lambda *a, **k: None, [], op.meta, apply=True, fetch_fn=fetch_falso, mapa=mapa)
    op2.passo(entrada, agora[0])
    check("sim UEFA: um operador novo (outro arranque) não repete nada", len(site.writes), n)
    # o mesmo, com o site já com linhas escritas à mão: não duplica nem mexe no que lá está
    agora[0] = ko - dt.timedelta(minutes=10)
    site2 = _SiteFalso(66616, 891, 6, texts=[H(12, 1, 891, "Arrancou o jogo!"), H(12, 10, 891, "Golo do Kauno!"), H(12, 40, 891, "Acabou")])
    op3 = Operador(site2, lambda *a, **k: None, [], op.meta, apply=True, fetch_fn=fetch_falso, mapa=mapa)
    agora[0] = fim + dt.timedelta(minutes=3)
    op3.passo(entrada, agora[0])
    pts2 = [w[4] for w in site2.writes if w[0] == "texto"]
    check("sim UEFA: comentários do Carlos/Rui/Pedro (início, golo aos 10', fim) não se repetem", [p for p in pts2 if p.startswith(("Início do jogo", "Fim do jogo")) or "abre o marcador" in p], [])
    check("sim UEFA: as outras linhas (golos 16', 25', 30', 37') escrevem-se", sum(1 for p in pts2 if p.startswith("Golo de")), 4)
    # sem interruptor (uefa_from_utc) o jogo não é tocado; com ele aberto, sim
    now = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.timezone.utc)
    check("interruptor: sem uefa_from_utc nem padrão -> desligado", ativo({}, None, now), False)
    check("interruptor: 2099 -> desligado", ativo({"uefa_from_utc": "2099-01-01T00:00:00Z"}, None, now), False)
    check("interruptor: data passada -> ligado", ativo({"uefa_from_utc": "2026-11-24T11:00:00Z"}, None, now), True)
    check("interruptor: o do jogo manda sobre o padrão", ativo({"uefa_from_utc": "2099-01-01T00:00:00Z"}, "2026-01-01T00:00:00Z", now), False)
    check("interruptor: sem o do jogo vale o padrão", ativo({}, "2026-01-01T00:00:00Z", now), True)
    # estados especiais
    cancel = Operador(_SiteFalso(66616, 891, 6), lambda *a, **k: None, (al := []), op.meta, apply=True, fetch_fn=lambda p: {**d87["match"], "status": "POSTPONED"}, mapa=mapa)
    r = cancel.passo(entrada, ko)
    check("jogo adiado na UEFA: não escreve, avisa e deixa de vigiar", (r["vigiar"], len(al), cancel.mcp.writes), (False, 1, []))
    errada = Operador(_SiteFalso(66616, 1, 2), lambda *a, **k: None, (al2 := []), op.meta, apply=True, fetch_fn=fetch_falso, mapa=mapa)
    agora[0] = ko + dt.timedelta(minutes=5)
    r = errada.passo(entrada, agora[0])
    check("equipas do site diferentes do mapa: não escreve", (r["vigiar"], errada.mcp.writes, len(al2)), (False, [], 1))


if __name__ == "__main__":
    sys.exit("usa-se através do live.py (jogos com o campo «uefa» no matches.json)")
