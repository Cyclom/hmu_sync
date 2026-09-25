#!/usr/bin/env python3
"""trainex-sync: TraiNex-Studienplan -> iCal-Abo + Telegram-Bot.

Nur Python-Standardbibliothek (>= 3.9).

Befehle:
  trainex_sync.py [--env DATEI] run                Dienst (Telegram-Bot + Zeitplan)
  trainex_sync.py [--env DATEI] check [--file X]   Testabruf, zeigt Änderungen, schreibt nichts
  trainex_sync.py [--env DATEI] discover           Telegram-Chat-IDs anzeigen
  trainex_sync.py [--env DATEI] testmsg            Testnachricht an Admin + Kanal
"""
import datetime as dt
import html
import http.cookiejar
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

VERSION = "1.8"
TZ = ZoneInfo("Europe/Berlin")
UA = f"trainex-sync/{VERSION} (private calendar sync)"
WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
KIND_SHORT = {
    "Vorlesung": "VL",
    "Praktikum": "P",
    "Seminar": "S",
    "integrierte Seminare": "IS",
    "Seminar mit klin. Bezug": "KS",
}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ───────────────────────── Konfiguration ─────────────────────────

def load_env_file(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            os.environ[k] = v


class Cfg:
    def __init__(self):
        e = os.environ.get
        self.base = e("TRAINEX_BASE", "https://www.trainex32.de/hmu24").rstrip("/")
        self.user = e("TRAINEX_USER", "")
        self.pw = e("TRAINEX_PASS", "")
        self.bot = e("TELEGRAM_BOT_TOKEN", "")
        self.admin = e("TELEGRAM_ADMIN_ID", "").strip()
        self.channel = e("TELEGRAM_CHANNEL_ID", "").strip()
        self.ics_token = e("ICS_TOKEN", "").strip()
        self.public_url = e("PUBLIC_URL", "https://tillianbo.com/trainex").rstrip("/")
        self.web_dir = e("WEB_DIR", "/var/www/trainex")
        self.state_dir = e("STATE_DIRECTORY", e("STATE_DIR", "/var/lib/trainex-sync")).split(":")[0]
        self.cal_name = e("CAL_NAME", "HMU Stundenplan")
        self.access_log = e("ACCESS_LOG", "/var/log/nginx/trainex.access.log")
        self.default_interval = int(e("DEFAULT_INTERVAL", "30"))
        self.min_interval = int(e("MIN_INTERVAL", "15"))
        g = e("CAMPUS_GEO", "").replace(" ", "")
        self.campus_geo = [float(x) for x in g.split(",")] if re.fullmatch(r"-?\d+\.\d+,-?\d+\.\d+", g) else None
        q = e("QUIET_HOURS", "").strip()  # z. B. "22-6"
        self.quiet = tuple(int(x) for x in q.split("-")) if re.fullmatch(r"\d{1,2}-\d{1,2}", q) else None

    def require(self, *names):
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            m = {"user": "TRAINEX_USER", "pw": "TRAINEX_PASS", "bot": "TELEGRAM_BOT_TOKEN",
                 "admin": "TELEGRAM_ADMIN_ID", "ics_token": "ICS_TOKEN"}
            sys.exit("Fehlende Einträge in .env: " + ", ".join(m.get(n, n) for n in missing))


# ───────────────────────── TraiNex-Abruf ─────────────────────────

class SyncError(Exception):
    pass


def _snippet(body, cfg):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body.decode("utf-8", "replace"), flags=re.S | re.I)
    t = norm(html.unescape(re.sub(r"<[^>]+>", " ", t)))
    for secret in (cfg.pw, cfg.user):
        if secret and len(secret) >= 3:
            t = t.replace(secret, "***")
    return t[:400]


def _tok():
    """URL-Tokens wie im TraiNex-Frontend (aus dem Zeitstempel abgeleitet)."""
    t = str(int(time.time() * 1000))
    return f"TokCF19=0T0{t[4:]}&IDphp17=3P{t[7:]}&sec18m=7S{t[5:]}0{t[4:]}&{t}"


def _find_url(body, base_url, must, exclude=None):
    text = body.decode("utf-8", "replace")
    for m in re.finditer(r"""["']([^"'<>\s]*%s[^"'<>\s]*)["']""" % must, text, re.I):
        u = html.unescape(m.group(1))
        if exclude and re.search(exclude, u, re.I):
            continue
        return urllib.parse.urljoin(base_url, u)
    return None


def _n_events(body):
    t = body.decode("utf-8", "replace")
    return t.count("BEGIN:VEVENT") if t.lstrip().startswith("BEGIN:VCALENDAR") else -1


def fetch_export(cfg, req, b):
    """Klickt sich wie im Browser durch: Lernen → Studienplan → Listenansicht → iCal.
    Der iCal-Export liefert genau die Liste, die zuletzt in der Sitzung angezeigt wurde.
    Maßgeblich ist die Kurs-Listenansicht (einsatzplan_listenansicht_kt.cfm) aus dem Stundenplan."""
    today = dt.datetime.now(TZ)
    direct = (f"{b}/cfm/einsatzplan/einsatzplan_listenansicht_iCal.cfm?{_tok()}"
              f"&utag={today.day}&umonat={today.month}&ujahr={today.year}&ics=1")
    nav = f"{b}/navigation/student_layout.cfm?{_tok()}&area=Kursraum&subarea=studienplan"
    _, _, page = req("3 Studienplan", nav)
    frame = (_find_url(page, nav, r"einsatzplan_stundenplan\.cfm")
             or f"{b}/cfm/einsatzplan/einsatzplan_stundenplan.cfm?{_tok()}")
    _, _, splan = req("4 Stundenplan", frame)

    best = b""
    # A: Kurs-Listenansicht („Weiter zur Listenansicht“), B: persönliche Listenansicht
    for step, pattern in (("A", r"einsatzplan_listenansicht_kt\.cfm"),
                          ("B", r"einsatzplan_listenansicht\.cfm")):
        liste = _find_url(splan, frame, pattern)
        if not liste:
            continue
        _, _, page = req(f"5{step} Listenansicht", liste)
        ical = _find_url(page, liste, r"einsatzplan_listenansicht_iCal\.cfm") or direct
        _, _, body = req(f"6{step} Export", ical, extra={"Referer": liste})
        if _n_events(body) > 0:
            return body
        best = max((best, body), key=_n_events)
    return best or b"TraiNex: keine Listenansicht im Stundenplan gefunden"


def fetch_trainex(cfg, debug=False):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", UA), ("Accept-Language", "de-DE,de;q=0.9")]

    def dbg(step, status, url, hdr, body):
        if not debug:
            return
        path = urllib.parse.urlsplit(url).path
        forms = re.findall(r"<form[^>]*>", body.decode("utf-8", "replace"), re.I)
        print(f"\n── {step}: HTTP {status} → {path}")
        print(f"   Content-Type: {hdr.get('Content-Type')} · {len(body)} Bytes · Server: {hdr.get('Server')}")
        print(f"   Cookies: {', '.join(sorted(f'{c.name}@{c.domain}' for c in jar)) or '–'}")
        if forms:
            print(f"   Formulare: {' | '.join(f[:150] for f in forms[:3])}")
            txt = body.decode("utf-8", "replace")
            fields = []
            for m in re.finditer(r"<(input|select)\b[^>]*>", txt, re.I):
                tag = m.group(0)
                nm = re.search(r"""name\s*=\s*["']?([^"'\s>]+)""", tag, re.I)
                if not nm or re.search(r"(?i)pass", nm.group(1)):
                    continue
                ty = re.search(r"""type\s*=\s*["']?(\w+)""", tag, re.I)
                va = re.search(r"""value\s*=\s*["']([^"']{0,40})""", tag, re.I)
                fields.append(f"{nm.group(1)}[{(ty.group(1) if ty else m.group(1)).lower()}]"
                              + (f"={va.group(1)}" if va else ""))
            if fields:
                print(f"   Felder: {', '.join(fields[:30])}")
            for sm in re.finditer(r"<select\b[^>]*name=[\"']?(\w+)[^>]*>(.*?)</select>", txt, re.I | re.S):
                if sm.group(1) == "GoMenu":
                    continue
                opts = re.findall(r"<option\b([^>]*)>([^<]*)", sm.group(2), re.I)
                show = []
                for attrs, label in opts[:12]:
                    v = re.search(r"""value\s*=\s*["']?([^"'\s>]*)""", attrs, re.I)
                    sel = "*" if re.search(r"selected", attrs, re.I) else ""
                    show.append(f"{sel}{v.group(1) if v else ''}:{norm(label)[:40]}")
                print(f"   Auswahl {sm.group(1)} ({len(opts)}): {' | '.join(show)}")
        links = sorted({urllib.parse.urlsplit(html.unescape(m)).path.rsplit('/', 1)[-1] + '?' +
                        '&'.join(q for q in urllib.parse.urlsplit(html.unescape(m)).query.split('&')
                                 if q and not re.match(r"(TokCF19|IDphp17|sec18m|\d+$)", q))
                        for m in re.findall(r"[\"']([^\"'<>\s]*einsatzplan_\w+\.cfm[^\"'<>\s]*)[\"']",
                                            body.decode("utf-8", "replace"), re.I)})
        if links:
            print(f"   Links: {' , '.join(links[:15])}")
        n = _n_events(body)
        if n >= 0:
            ds = sorted(re.findall(r"DTSTART[^:]*:(\d{8})", body.decode("utf-8", "replace")))
            print(f"   iCal: {n} Termine" + (f" ({ds[0]} – {ds[-1]})" if ds else ""))
        print(f"   Text: {_snippet(body, cfg)[:200]}")

    def req(step, url, data=None, extra=None):
        rq = urllib.request.Request(url, data=data, headers=extra or {})
        try:
            with op.open(rq, timeout=30) as r:
                body = r.read()
                dbg(step, r.status, r.geturl(), r.headers, body)
                return r.status, r.headers, body
        except urllib.error.HTTPError as ex:
            body = ex.read() or b""
            dbg(step, ex.code, url, ex.headers, body)
            srv = (ex.headers.get("Server") or "").lower()
            if ex.code in (403, 429, 503) and ("cloudflare" in srv or ex.headers.get("cf-ray")
                                                or b"challenge" in body[:5000].lower()):
                raise SyncError(f"Cloudflare blockiert den Zugriff vom Server (HTTP {ex.code}).")
            raise SyncError(f"TraiNex antwortet mit HTTP {ex.code}.")
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise SyncError(f"TraiNex nicht erreichbar: {getattr(ex, 'reason', ex)}")

    if debug:
        print(f"trainex-sync {VERSION}")
    b = cfg.base
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(b))
    req("1 Login-Seite", f"{b}/logout.cfm")  # setzt Session-Cookies
    form = urllib.parse.urlencode({"Login": cfg.user, "Passwort": cfg.pw,
                                   "Domaene": "0", "einloggen": "Anmelden"}).encode()
    _, _, lbody = req("2 Login", f"{b}/start.cfm?eng=0", form,
                      {"Origin": origin, "Referer": f"{b}/logout.cfm",
                       "Content-Type": "application/x-www-form-urlencoded"})
    try:
        body = fetch_export(cfg, req, b)
    finally:
        try:
            req("9 Logout", f"{b}/logout.cfm")
        except SyncError:
            pass
    text = body.decode("utf-8", "replace")
    if not text.lstrip().startswith("BEGIN:VCALENDAR"):
        if re.search(r"""name=["']?Passwort""", lbody.decode("utf-8", "replace"), re.I):
            raise SyncError("TraiNex-Login fehlgeschlagen (Benutzername/Passwort prüfen).")
        raise SyncError("TraiNex lieferte keine iCal-Datei (unerwartete Antwort). "
                        "Details: sudo ./install.sh debug")
    return text


# ───────────────────────── iCal lesen ─────────────────────────

def ics_unescape(v):
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def parse_ics(text):
    text = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))
    events = []
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)\nEND:VEVENT", text, re.S):
        p = {}
        for line in block.split("\n"):
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            p[k.split(";")[0].upper()] = v
        if "DTSTART" not in p:
            continue
        s = p["DTSTART"].strip()
        events.append({
            "s": s,
            "e": p.get("DTEND", s).strip(),
            "sum": norm(ics_unescape(p.get("SUMMARY", ""))),
            "desc": norm(ics_unescape(p.get("DESCRIPTION", ""))),
            "loc": norm(ics_unescape(p.get("LOCATION", ""))),
        })
    return events


def norm(x):
    return re.sub(r"\s+", " ", x or "").strip()


# ───────────────────────── Termin-Ableitungen ─────────────────────────

def to_dt(v):
    if len(v) == 8:
        return dt.datetime.strptime(v, "%Y%m%d").replace(tzinfo=TZ)
    if v.endswith("Z"):
        return dt.datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).astimezone(TZ)
    return dt.datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=TZ)


def room(ev):
    return ev["loc"].split(" - HMU")[0].strip()


def title(ev):
    s = ev["sum"]
    parts = s.rsplit(" - ", 2)
    if len(parts) == 3 and re.fullmatch(r"t\d+_\w+", parts[2].strip()):
        return parts[0].strip()
    r = room(ev)
    if r and s.endswith(" - " + r):
        return s[: -len(r) - 3].strip()
    return s


def split_title(ev):
    t = title(ev)
    module, _, rest = t.partition(" - ")
    kind, _, topic = rest.partition("/")
    return module.strip(), kind.strip(), topic.strip()


def kind_short(kind):
    if kind in KIND_SHORT:
        return KIND_SHORT[kind]
    # unbekannte Art: Anfangsbuchstaben, z. B. „Tutorium“ -> „T“
    return "".join(w[0].upper() for w in re.findall(r"[A-Za-zÄÖÜäöü]{3,}", kind)) or kind


def short(ev):
    """z. B. „M11 Physiologie - VL - Nierenphysiologie I“."""
    module, kind, topic = split_title(ev)
    module = re.sub(r"\s*/\s*", "/", module)          # „Biochemie/ Molekularbiologie“ -> „Biochemie/Molekularbiologie“
    if kind.startswith("Seminar") or "Seminare" in kind:
        topic = re.sub(r"^Seminar:?\s+", "", topic)
    return " - ".join(x for x in (module, kind_short(kind) if kind else "", topic) if x)


def lecturer(ev):
    d = ev["desc"]
    m = re.search(r"Leiter/-in: (.*?)\)\s*$", d) or re.search(r"\)/(.+?) ab \d{1,2}:\d{2}", d)
    return norm(m.group(1)) if m else ""


def modkind(ev):
    m, k, _ = split_title(ev)
    return m + "|" + k


def fmt_day(ev):
    d = to_dt(ev["s"])
    return f"{WD[d.weekday()]} {d:%d.%m.}"


def fmt_time(ev):
    if len(ev["s"]) == 8:
        return "ganztägig"
    return f"{to_dt(ev['s']):%H:%M}–{to_dt(ev['e']):%H:%M}"


def is_past(ev, now):
    return to_dt(ev["e"]) < now


# ───────────────────────── Abgleich ─────────────────────────

def match(old, new):
    """Ordnet neue Termine alten zu. Liefert (pairs[(oi,ni)], unmatched_old, unmatched_new)."""
    uo, un = set(range(len(old))), set(range(len(new)))
    pairs = []
    rules = [
        lambda o, n: o["s"] == n["s"] and title(o) == title(n),
        lambda o, n: o["s"][:8] == n["s"][:8] and title(o) == title(n),
        lambda o, n: o["s"] == n["s"] and modkind(o) == modkind(n),
    ]
    for rule in rules:
        for ni in sorted(un):
            for oi in sorted(uo):
                if rule(old[oi], new[ni]):
                    pairs.append((oi, ni)); uo.discard(oi); un.discard(ni)
                    break
    # Verschobene Termine: gleicher Titel, nächstliegendes Datum (max. 21 Tage)
    cand = []
    for ni in un:
        for oi in uo:
            if title(old[oi]) == title(new[ni]):
                d = abs((to_dt(old[oi]["s"]) - to_dt(new[ni]["s"])).total_seconds())
                if d <= 21 * 86400:
                    cand.append((d, oi, ni))
    for _, oi, ni in sorted(cand):
        if oi in uo and ni in un:
            pairs.append((oi, ni)); uo.discard(oi); un.discard(ni)
    return pairs, sorted(uo), sorted(un)


def e(x):
    return html.escape(x, quote=False)


def diff_pair(o, n):
    """Liefert Liste (emoji, label, alt, neu) der meldenswerten Änderungen."""
    ch = []
    if o["s"][:8] != n["s"][:8]:
        ch.append(("📆", "Termin verschoben", f"{fmt_day(o)} {fmt_time(o)}", f"{fmt_day(n)} {fmt_time(n)}"))
    elif o["s"] != n["s"] or o["e"] != n["e"]:
        ch.append(("🕐", "Zeit geändert", fmt_time(o), fmt_time(n)))
    if room(o) != room(n):
        ch.append(("🚪", "Raum geändert", room(o), room(n)))
    if lecturer(o) != lecturer(n):
        ch.append(("👤", "Dozent geändert", lecturer(o) or "–", lecturer(n) or "–"))
    if title(o) != title(n):
        ch.append(("✏️", "Titel geändert", split_title(o)[2] or title(o), split_title(n)[2] or title(n)))
    return ch


def compute(state, new, now, force=False):
    """Gleicht neuen Export mit State ab. Liefert (events_neu, entries, stats)."""
    old = state.get("events", [])
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not new:
        raise SyncError("Export enthält keine Termine – nichts geändert.")
    first = not old
    pairs, uo, un = match(old, new)

    removed_future = [old[i] for i in uo if not is_past(old[i], now)]
    kept_past = [old[i] for i in uo if is_past(old[i], now)]
    old_future = sum(1 for o in old if not is_past(o, now))
    if not force and len(removed_future) > max(3, 0.2 * old_future):
        raise SyncError(
            f"{len(removed_future)} von {old_future} kommenden Terminen würden entfallen. "
            "Aus Sicherheit nichts geändert. Falls korrekt (z. B. Semesterwechsel): /sync force")

    out, entries = [], []
    for oi, ni in pairs:
        o, n = old[oi], new[ni]
        ev = dict(n, uid=o["uid"], seq=o.get("seq", 0), mod=o.get("mod", stamp))
        if any(o[k] != n[k] for k in ("s", "e", "sum", "desc", "loc")):
            ev["seq"] += 1
            ev["mod"] = stamp
            ch = diff_pair(o, n)
            if ch:
                entries.append((to_dt(n["s"]), "chg", n, ch))
        out.append(ev)
    for ni in un:
        n = new[ni]
        out.append(dict(n, uid=secrets.token_hex(12) + "@trainex-sync", seq=0, mod=stamp))
        if not first:
            entries.append((to_dt(n["s"]), "new", n, None))
    for o in removed_future:
        entries.append((to_dt(o["s"]), "del", o, None))
    # Vergangene Termine behalten (max. 1 Jahr), damit der Kalender die Historie zeigt
    limit = now - dt.timedelta(days=365)
    out += [o for o in kept_past if to_dt(o["e"]) > limit]
    out.sort(key=lambda x: (x["s"], x["sum"]))
    entries.sort(key=lambda x: x[0])
    return out, entries, {"first": first, "pairs": len(pairs)}


def format_entries(entries):
    blocks = []
    for _, typ, ev, ch in entries:
        head = f"{fmt_day(ev)} {fmt_time(ev)} · {e(short(ev))}"
        if typ == "new":
            extra = " · ".join(x for x in (room(ev), lecturer(ev)) if x)
            blocks.append(f"➕ <b>Neuer Termin:</b> {head}" + (f"\n      {e(extra)}" if extra else ""))
        elif typ == "del":
            blocks.append(f"❌ <b>Termin entfällt:</b> {head}")
        else:
            lines = [f"📌 <b>{head}</b>"]
            for emo, label, a, b in ch:
                lines.append(f"{emo} {label}: {e(a)} → <b>{e(b)}</b>")
            blocks.append("\n".join(lines))
    return blocks


# ───────────────────────── iCal schreiben ─────────────────────────

VTZ = """BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE""".split("\n")


def ics_escape(v):
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    out, cur, limit = [], b"", 75
    for ch in line:
        cb = ch.encode("utf-8")
        if len(cur) + len(cb) > limit:
            out.append(cur.decode("utf-8")); cur = b""; limit = 74
        cur += cb
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def dtprop(name, v):
    if len(v) == 8:
        return f"{name};VALUE=DATE:{v}"
    if v.endswith("Z"):
        return f"{name}:{v}"
    return f"{name};TZID=Europe/Berlin:{v[:15]}"


def place(ev):
    """Zerlegt TraiNex-LOCATION: „Raum - HMU … - Campus … // Straße // PLZ Ort“."""
    loc = ev["loc"]
    parts = [x.strip() for x in loc.split("//")]
    if len(parts) >= 3:
        head = parts[0]
        r = room(ev)
        org = head[len(r):].lstrip(" -").split(" - ")[0].strip() if head.startswith(r) else ""
        return {"room": r, "org": org, "street": parts[1], "city": parts[2]}
    return {"room": room(ev), "org": "", "street": "", "city": ""}


def addr_key(pl):
    return f"{pl['street']}, {pl['city']}" if pl["street"] else ""


def geocode(key):
    """OpenStreetMap/Nominatim, nur einmal pro neuer Adresse (Ergebnis wird gespeichert)."""
    street, _, city = key.partition(", ")
    street1 = re.sub(r"(\d+)\s*[-/]\s*\d+\w*", r"\1", street)  # „Kaistraße 16-16a“ -> „Kaistraße 16“
    for q in (f"{street1}, {city}, Deutschland", f"{city}, Deutschland"):
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"format": "jsonv2", "limit": 1, "q": q})
        rq = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de"})
        try:
            with urllib.request.urlopen(rq, timeout=15) as r:
                res = json.load(r)
        except Exception as ex:
            log("Geocoding fehlgeschlagen:", ex)
            return None
        if res:
            return [round(float(res[0]["lat"]), 6), round(float(res[0]["lon"]), 6)]
        time.sleep(1.1)
    return None


def geo_for(cfg, geo, pl):
    if cfg.campus_geo:
        return cfg.campus_geo
    v = (geo or {}).get(addr_key(pl))
    return v if isinstance(v, list) else None


def location_props(cfg, ev, geo):
    """LOCATION im Apple-Stil (Titel + Adresse) plus GEO und X-APPLE-STRUCTURED-LOCATION,
    damit iOS den Ort als Karte mit Pin anzeigt."""
    pl = place(ev)
    if not pl["street"]:
        return [f"LOCATION:{ics_escape(ev['loc'])}"], None
    title = f"{pl['room']} · HMU" if pl["room"] else (pl["org"] or "HMU")
    props = [f"LOCATION:{ics_escape(title + chr(10) + pl['street'] + ', ' + pl['city'])}"]
    ll = geo_for(cfg, geo, pl)
    if ll:
        lat, lon = ll
        adr = f"{pl['street']}\\n{pl['city']}\\nDeutschland".replace('"', "'")
        props += [f"GEO:{lat};{lon}",
                  f'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="{adr}";X-APPLE-RADIUS=100;'
                  f'X-TITLE="{title.replace(chr(34), chr(39))}":geo:{lat},{lon}']
    return props, ll


def build_ics(cfg, events, geo=None):
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//trainex-sync//{VERSION}//DE",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         f"X-WR-CALNAME:{ics_escape(cfg.cal_name)}",
         "X-WR-CALDESC:Automatisch aus TraiNex synchronisiert",
         "X-WR-TIMEZONE:Europe/Berlin",
         "REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M"] + VTZ
    for ev in events:
        module, kind, topic = split_title(ev)
        desc = [title(ev)]
        if room(ev):
            desc.append(f"Raum: {room(ev)}")
        if lecturer(ev):
            desc.append(f"Dozent/in: {lecturer(ev)}")
        locp, ll = location_props(cfg, ev, geo)
        pl = place(ev)
        if pl["street"]:
            desc.append(f"Ort: {pl['org'] or 'HMU'}, {pl['street']}, {pl['city']}")
        if ll:
            desc.append(f"Karte: https://maps.apple.com/?ll={ll[0]},{ll[1]}&q=HMU")
        desc += ["", "Quelle: TraiNex – aktuelle Termine immer im TraiNex prüfen."]
        L += ["BEGIN:VEVENT", f"UID:{ev['uid']}", f"DTSTAMP:{ev['mod']}", f"LAST-MODIFIED:{ev['mod']}",
              f"SEQUENCE:{ev.get('seq', 0)}", dtprop("DTSTART", ev["s"]), dtprop("DTEND", ev["e"]),
              f"SUMMARY:{ics_escape(short(ev))}"] + locp + [
              f"DESCRIPTION:{ics_escape(chr(10).join(desc))}", "TRANSP:OPAQUE", "END:VEVENT"]
    L.append("END:VCALENDAR")
    return ("\r\n".join(fold(x) for x in L) + "\r\n").encode("utf-8")


def write_atomic(path, data, mode=0o644):
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except FileNotFoundError:
        pass
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


# ───────────────────────── Abo-Seite ─────────────────────────

PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="color-scheme" content="light dark">
<title>{name} abonnieren</title>
<style>
:root{{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--mut:#6e6e73;--acc:#0071e3;--line:#d2d2d7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--card:#1c1c1e;--fg:#f5f5f7;--mut:#98989d;--acc:#0a84ff;--line:#38383a}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:24px 16px}}
main{{max-width:440px;margin:0 auto}}
.card{{background:var(--card);border-radius:18px;padding:24px 20px;margin-bottom:16px}}
h1{{font-size:24px;margin:4px 0 4px}}
.ico{{font-size:40px}}
p{{margin:0 0 12px;color:var(--mut)}}
a.btn,button{{display:block;width:100%;text-align:center;text-decoration:none;font-family:inherit;font-size:17px;font-weight:600;line-height:1;
  padding:15px;border-radius:12px;border:0;margin-top:10px;cursor:pointer}}
.pri{{background:var(--acc);color:#fff}}
.sec{{background:transparent;color:var(--acc);border:1px solid var(--line)!important}}
code{{display:block;word-break:break-all;font-size:12px;color:var(--mut);margin-top:12px}}
ol{{margin:0;padding-left:20px;color:var(--mut)}} li{{margin:4px 0}}
h2{{font-size:15px;margin:0 0 8px}}
</style></head><body><main>
<div class="card">
<div class="ico">📅</div>
<h1>{name}</h1>
<p>Stundenplan aus TraiNex, automatisch aktualisiert. Änderungen erscheinen von selbst im Kalender.</p>
<a class="btn pri" href="{webcal}">In Apple Kalender abonnieren</a>
<a class="btn sec" href="https://calendar.google.com/calendar/render?cid={webcal_q}">Google Kalender</a>
<a class="btn sec" href="https://outlook.live.com/calendar/0/addfromweb?url={https_q}&amp;name={name_q}">Outlook</a>
<button class="sec" onclick="navigator.clipboard.writeText('{https}').then(()=>this.textContent='Kopiert ✓')">Link kopieren</button>
<code>{https}</code>
</div>
<div class="card">
<h2>Schneller aktualisieren (iPhone)</h2>
<ol>
<li>Einstellungen → Kalender → Accounts → <b>Datenabgleich</b> (ab iOS 18: Einstellungen → Apps → Kalender → Kalenderaccounts)</li>
<li>„Abruf“ auf <b>Alle 15 Minuten</b> stellen</li>
</ol>
<p style="margin-top:12px">Ohne diese Einstellung lädt iOS das Abo seltener. Google Kalender aktualisiert Abos nur alle paar Stunden.</p>
</div>
</main></body></html>
"""


def build_page(cfg, https_url):
    webcal = "webcal://" + https_url.split("://", 1)[1]
    q = lambda x: urllib.parse.quote(x, safe="")
    return PAGE.format(name=html.escape(cfg.cal_name), webcal=html.escape(webcal), https=html.escape(https_url),
                       webcal_q=q(webcal), https_q=q(https_url), name_q=q(cfg.cal_name)).encode("utf-8")


# ───────────────────────── State ─────────────────────────

class State:
    def __init__(self, cfg):
        self.path = os.path.join(cfg.state_dir, "state.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.d = json.load(f)
        except FileNotFoundError:
            self.d = {}
        self.d.setdefault("events", [])
        s = self.d.setdefault("settings", {})
        s.setdefault("auto", True)
        s.setdefault("interval", cfg.default_interval)
        s.setdefault("token", "")
        self.d.setdefault("fails", 0)

    def save(self):
        write_atomic(self.path, json.dumps(self.d, ensure_ascii=False, indent=1).encode("utf-8"), 0o600)

    @property
    def settings(self):
        return self.d["settings"]


# ───────────────────────── Telegram ─────────────────────────

class TG:
    def __init__(self, token):
        self.url = f"https://api.telegram.org/bot{token}/"

    def call(self, method, http_timeout=20, **params):
        data = json.dumps(params).encode()
        rq = urllib.request.Request(self.url + method, data=data,
                                    headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(rq, timeout=http_timeout) as r:
                res = json.load(r)
        except urllib.error.HTTPError as ex:
            try:
                res = json.load(ex)
            except Exception:
                res = {"ok": False, "description": f"HTTP {ex.code}"}
        if not res.get("ok"):
            raise RuntimeError(f"Telegram {method}: {res.get('description')}")
        return res["result"]

    def send(self, chat, text):
        """Sendet HTML-Text, teilt bei Bedarf an Absatzgrenzen (Limit 4096)."""
        if not chat:
            return
        chunks, cur = [], ""
        for part in text.split("\n\n"):
            if cur and len(cur) + len(part) + 2 > 3900:
                chunks.append(cur); cur = ""
            cur = f"{cur}\n\n{part}" if cur else part
        chunks.append(cur)
        for c in chunks:
            for attempt in range(3):
                try:
                    self.call("sendMessage", chat_id=chat, text=c[:4096], parse_mode="HTML",
                              disable_web_page_preview=True)
                    break
                except Exception as ex:
                    log("Telegram-Sendefehler:", ex)
                    time.sleep(2 * (attempt + 1))


# ───────────────────────── Sync-Kern ─────────────────────────

class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.st = State(cfg)
        self.tg = TG(cfg.bot) if cfg.bot else None
        self.started = time.time()
        self.next_run = 0.0

    # URL / Dateien
    def token(self):
        return self.st.settings.get("token") or self.cfg.ics_token

    def ics_url(self):
        return f"{self.cfg.public_url}/{self.token()}.ics"

    def page_url(self):
        return f"{self.cfg.public_url}/{self.token()}"

    def webcal_url(self):
        return "webcal://" + self.ics_url().split("://", 1)[1]

    def ics_path(self, tok=None, ext="ics"):
        return os.path.join(self.cfg.web_dir, f"{tok or self.token()}.{ext}")

    def publish(self):
        write_atomic(self.ics_path(ext="html"), build_page(self.cfg, self.ics_url()))
        return write_atomic(self.ics_path(), build_ics(self.cfg, self.st.d["events"], self.st.d.get("geo")))

    def resolve_geo(self):
        """Neue Adressen einmalig geokodieren (fehlgeschlagene frühestens nach 7 Tagen erneut)."""
        if self.cfg.campus_geo:
            return
        geo = self.st.d.setdefault("geo", {})
        for key in {addr_key(place(x)) for x in self.st.d["events"]} - {""}:
            v = geo.get(key)
            if isinstance(v, list) or (isinstance(v, dict) and time.time() - v.get("fail", 0) < 7 * 86400):
                continue
            ll = geocode(key)
            geo[key] = ll if ll else {"fail": time.time()}
            log("Geokodiert:", key, "->", ll)
            time.sleep(1.1)

    def abo_link(self, label="📅 Kalender abonnieren"):
        return f'<a href="{e(self.page_url())}">{label}</a>'

    def notify_admin(self, text):
        if self.tg:
            self.tg.send(self.cfg.admin, text)

    def sync(self, force=False, source="auto"):
        t0 = time.time()
        now = dt.datetime.now(TZ)
        run = {"time": t0, "source": source}
        try:
            new = parse_ics(fetch_trainex(self.cfg))
            events, entries, info = compute(self.st.d, new, now, force)
        except Exception as ex:
            msg = str(ex) if isinstance(ex, SyncError) else f"Interner Fehler: {ex!r}"
            run.update(ok=False, msg=msg, dur=time.time() - t0)
            self.st.d["last_run"] = run
            self.st.d["fails"] += 1
            self.st.save()
            log("Sync fehlgeschlagen:", msg)
            # Nur beim ersten Fehler einer Serie (oder bei manuellem Sync) melden
            if self.st.d["fails"] == 1 or source == "manual":
                self.notify_admin(f"⚠️ <b>Sync fehlgeschlagen</b>\n{e(msg)}")
            return run
        recovered = self.st.d["fails"] > 0
        self.st.d["fails"] = 0
        self.st.d["events"] = events
        n_up = sum(1 for x in events if not is_past(x, now))
        run.update(ok=True, dur=time.time() - t0, count=len(new), changes=len(entries),
                   msg=f"{len(entries)} Änderung(en)" if entries else "keine Änderungen")
        self.st.d["last_run"] = run
        if entries:
            blocks = format_entries(entries)
            self.st.d["last_change"] = {"time": t0, "n": len(entries), "preview": blocks[:5]}
        self.resolve_geo()
        self.st.save()
        self.publish()
        log(f"Sync ok ({source}): {len(new)} Termine, {len(entries)} Änderungen, {run['dur']:.1f}s")

        if info["first"]:
            self.notify_admin(f"✅ <b>Erstimport:</b> {len(events)} Termine ({n_up} kommend).\n"
                              f"{self.abo_link()}\n{e(self.page_url())}")
        elif entries:
            text = (f"📅 <b>Stundenplan geändert</b> ({len(entries)})\n\n" + "\n\n".join(blocks)
                    + f"\n\n{self.abo_link()}")
            if self.tg:
                self.tg.send(self.cfg.channel or self.cfg.admin, text)
        if recovered:
            self.notify_admin("✅ Sync funktioniert wieder.")
        return run

    # ── Zeitplan ──
    def in_quiet(self, t):
        q = self.cfg.quiet
        if not q:
            return False
        h = dt.datetime.fromtimestamp(t, TZ).hour
        a, b = q
        return (a <= h or h < b) if a > b else (a <= h < b)

    def quiet_end(self, t):
        d = dt.datetime.fromtimestamp(t, TZ)
        end = d.replace(hour=self.cfg.quiet[1], minute=0, second=0, microsecond=0)
        if end <= d:
            end += dt.timedelta(days=1)
        return end.timestamp()

    def schedule_from(self, t):
        self.next_run = t + self.st.settings["interval"] * 60

    # ── Status ──
    def access_stats(self):
        try:
            size = os.path.getsize(self.cfg.access_log)
            with open(self.cfg.access_log, "rb") as f:
                f.seek(max(0, size - 4_000_000))
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return None
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
        path = f"/{self.token()}.ics"
        n, ips = 0, set()
        for line in data.splitlines():
            m = re.match(r'(\S+) \S+ \S+ \[([^\]]+)\] "\w+ (\S+)[^"]*" (\d{3})', line)
            if not m or not m.group(3).split("?")[0].endswith(path) or m.group(4) not in ("200", "304"):
                continue
            try:
                ts = dt.datetime.strptime(m.group(2), "%d/%b/%Y:%H:%M:%S %z")
            except ValueError:
                continue
            if ts >= cutoff:
                n += 1; ips.add(m.group(1))
        return n, len(ips)

    def status_text(self):
        s, d = self.st.settings, self.st.d
        now = dt.datetime.now(TZ)
        ft = lambda t: dt.datetime.fromtimestamp(t, TZ).strftime("%d.%m. %H:%M") if t else "–"
        L = ["📊 <b>trainex-sync Status</b>", ""]
        L.append(f"Auto-Sync: {'🟢 an' if s['auto'] else '🔴 aus'} · alle {s['interval']} min")
        if self.cfg.quiet:
            L.append(f"Ruhezeit: {self.cfg.quiet[0]}–{self.cfg.quiet[1]} Uhr")
        if s["auto"]:
            L.append(f"Nächster Lauf: {ft(self.next_run)}")
        lr = d.get("last_run")
        if lr:
            icon = "✅" if lr.get("ok") else "⚠️"
            src = "manuell" if lr.get("source") == "manual" else "auto"
            L.append(f"Letzter Lauf: {icon} {ft(lr['time'])} ({src}, {lr.get('dur', 0):.1f}s) – {e(lr.get('msg', ''))}")
        if d.get("fails", 0) > 1:
            L.append(f"Fehler in Folge: {d['fails']}")
        lc = d.get("last_change")
        L.append(f"Letzte Änderung: {ft(lc['time']) + ' (' + str(lc['n']) + ')' if lc else '–'}")
        ev = d.get("events", [])
        up = [x for x in ev if not is_past(x, now)]
        L += ["", f"Termine: {len(ev)} gesamt, {len(up)} kommend"]
        if ev:
            L.append(f"Zeitraum: {to_dt(ev[0]['s']):%d.%m.%Y} – {to_dt(ev[-1]['s']):%d.%m.%Y}")
        if up:
            nx = min(up, key=lambda x: x["s"])
            L.append(f"Nächster Termin: {fmt_day(nx)} {fmt_time(nx)} · {e(short(nx))} ({e(room(nx))})")
        st = self.access_stats()
        L.append(f"Abo-Abrufe 24 h: {st[0]} von {st[1]} Geräten/IPs" if st else "Abo-Abrufe 24 h: n/a")
        up_s = int(time.time() - self.started)
        L += ["", f"{self.abo_link()}: {e(self.page_url())}",
              f"Laufzeit: {up_s // 86400} d {up_s % 86400 // 3600} h {up_s % 3600 // 60} min · v{VERSION}"]
        if lc and lc.get("preview"):
            L += ["", "<b>Zuletzt geändert:</b>", "\n\n".join(lc["preview"])]
        return "\n".join(L)

    # ── Befehle ──
    HELP = ("🤖 <b>Befehle</b>\n"
            "/sync – jetzt abgleichen (/sync force: Löschschutz übergehen)\n"
            "/start – automatischen Sync einschalten\n"
            "/stop – automatischen Sync ausschalten\n"
            "/settime &lt;min&gt; – Intervall in Minuten setzen\n"
            "/status – Status anzeigen\n"
            "/url – Abo-URL anzeigen\n"
            "/newurl – neue geheime Abo-URL erzeugen (alte wird ungültig)\n"
            "/help – diese Hilfe")

    def handle(self, text):
        parts = text.strip().split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        s = self.st.settings
        if cmd == "/sync":
            self.notify_admin("🔄 Sync läuft …")
            r = self.sync(force=(arg.lower() == "force"), source="manual")
            if r["ok"]:
                self.notify_admin(f"✅ Fertig in {r['dur']:.1f}s: {e(r['msg'])}"
                                  + (" (im Kanal gepostet)" if r.get("changes") and self.cfg.channel else ""))
            if s["auto"]:
                self.schedule_from(time.time())
        elif cmd == "/start":
            s["auto"] = True; self.st.save()
            self.next_run = time.time() + 2
            self.notify_admin(f"🟢 Auto-Sync an, alle {s['interval']} min. Erster Lauf jetzt.")
        elif cmd == "/stop":
            s["auto"] = False; self.st.save()
            self.notify_admin("🔴 Auto-Sync aus. Manuell weiterhin mit /sync.")
        elif cmd == "/settime":
            if not arg.isdigit() or not (self.cfg.min_interval <= int(arg) <= 10080):
                self.notify_admin(f"Nutzung: /settime &lt;Minuten&gt; ({self.cfg.min_interval}–10080)")
                return
            s["interval"] = int(arg); self.st.save()
            last = (self.st.d.get("last_run") or {}).get("time", time.time())
            self.schedule_from(max(last, time.time() - s["interval"] * 60))
            self.notify_admin(f"⏱ Intervall: {arg} min. "
                              + (f"Nächster Lauf: {dt.datetime.fromtimestamp(self.next_run, TZ):%H:%M}"
                                 if s["auto"] else "(Auto-Sync ist aus – /start)"))
        elif cmd == "/status":
            self.notify_admin(self.status_text())
        elif cmd == "/url":
            self.notify_admin(f"{self.abo_link()}\n{e(self.page_url())}\n\n"
                              "Die Seite öffnet auf dem iPhone direkt den Abo-Dialog und funktioniert auch für "
                              f"Google/Outlook. Diesen Link kannst du an Kollegen weitergeben.\n\n"
                              f"Direkt: <code>{e(self.webcal_url())}</code>")
        elif cmd == "/newurl":
            old = [self.ics_path(), self.ics_path(ext="html")]
            s["token"] = secrets.token_urlsafe(24); self.st.save()
            self.publish()
            for f in old:
                try:
                    os.remove(f)
                except OSError:
                    pass
            self.notify_admin(f"🔑 Neue Abo-URL, die alte ist ab sofort ungültig:\n"
                              f"{self.abo_link()}\n{e(self.page_url())}")
        elif cmd == "/help":
            self.notify_admin(self.HELP)
        else:
            self.notify_admin("Unbekannter Befehl. /help")

    def run(self):
        c = self.cfg
        c.require("user", "pw", "bot", "admin", "ics_token")
        os.makedirs(c.web_dir, exist_ok=True)
        if self.st.d["events"]:
            self.publish()
        try:
            self.tg.call("setMyCommands", commands=[
                {"command": k, "description": v} for k, v in [
                    ("sync", "Jetzt abgleichen"), ("status", "Status anzeigen"),
                    ("start", "Auto-Sync einschalten"), ("stop", "Auto-Sync ausschalten"),
                    ("settime", "Intervall in Minuten setzen"), ("url", "Abo-URL"),
                    ("newurl", "Neue Abo-URL erzeugen"), ("help", "Hilfe")]],
                scope={"type": "chat", "chat_id": int(c.admin)})
        except Exception as ex:
            log("setMyCommands:", ex)
        last = (self.st.d.get("last_run") or {}).get("time", 0)
        self.next_run = max(time.time() + 5, last + self.st.settings["interval"] * 60)
        if self.st.d.get("version") != VERSION:
            prev = self.st.d.get("version")
            self.st.d["version"] = VERSION
            self.st.save()
            if prev:
                self.notify_admin(f"🚀 trainex-sync aktualisiert: v{e(prev)} → <b>v{VERSION}</b>")
        log(f"trainex-sync {VERSION} gestartet. Auto={self.st.settings['auto']}, "
            f"Intervall={self.st.settings['interval']} min")
        offset = None
        while True:
            now = time.time()
            if self.st.settings["auto"] and now >= self.next_run:
                if self.in_quiet(now):
                    self.next_run = self.quiet_end(now)
                    log("Ruhezeit – nächster Lauf", dt.datetime.fromtimestamp(self.next_run, TZ))
                else:
                    self.sync(source="auto")
                    self.schedule_from(time.time())
                continue
            wait = 50
            if self.st.settings["auto"]:
                wait = int(max(1, min(50, self.next_run - now)))
            try:
                params = {"timeout": wait, "allowed_updates": ["message"]}
                if offset is not None:
                    params["offset"] = offset
                updates = self.tg.call("getUpdates", http_timeout=wait + 15, **params)
            except Exception as ex:
                log("getUpdates:", ex)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                txt = m.get("text") or ""
                if not txt.startswith("/"):
                    continue
                if str((m.get("from") or {}).get("id")) != c.admin or (m.get("chat") or {}).get("type") != "private":
                    log("Befehl von fremdem Nutzer ignoriert:", (m.get("from") or {}).get("id"))
                    continue
                log("Befehl:", txt.split()[0])
                try:
                    self.handle(txt)
                except Exception as ex:
                    log("Fehler bei Befehl:", repr(ex))
                    self.notify_admin(f"⚠️ Fehler: {e(repr(ex))}")


# ───────────────────────── CLI ─────────────────────────

def cmd_check(cfg, file=None, debug=False):
    if file:
        with open(file, encoding="utf-8") as f:
            text = f.read()
    else:
        cfg.require("user", "pw")
        t0 = time.time()
        text = fetch_trainex(cfg, debug)
        print(f"\nLogin + Export ok ({time.time() - t0:.1f}s)")
    new = parse_ics(text)
    if not new:
        sys.exit("Fehler: Der Export ist leer (0 Termine). Bitte Ausgabe von 'sudo ./install.sh debug' schicken.")
    ss = sorted(x["s"] for x in new)
    print(f"{len(new)} Termine im Export ({to_dt(ss[0]):%d.%m.%Y} – {to_dt(ss[-1]):%d.%m.%Y})")
    st = State(cfg)
    if not st.d["events"]:
        print("Noch kein gespeicherter Stand – beim ersten Lauf werden alle Termine importiert.")
        return
    _, entries, _ = compute(st.d, new, dt.datetime.now(TZ), force=True)
    print(f"{len(entries)} Änderung(en) gegenüber gespeichertem Stand:")
    for b in format_entries(entries):
        print(re.sub(r"</?b>", "", html.unescape(b)), "\n")


def cmd_discover(cfg):
    cfg.require("bot")
    tg = TG(cfg.bot)
    me = tg.call("getMe")
    print(f"Bot: @{me['username']}\n")
    try:
        ups = tg.call("getUpdates", http_timeout=20, timeout=5)
    except RuntimeError as ex:
        sys.exit(f"{ex}\n(Läuft der Dienst schon? Dann erst: systemctl stop trainex-sync)")
    seen = {}
    for u in ups:
        for key in ("message", "channel_post", "my_chat_member"):
            m = u.get(key)
            if not m:
                continue
            ch = m.get("chat", {})
            seen[ch.get("id")] = (ch.get("type"), ch.get("title") or ch.get("username") or ch.get("first_name"))
            fr = m.get("from")
            if fr and key == "message":
                seen[fr["id"]] = ("user", fr.get("username") or fr.get("first_name"))
    if not seen:
        print("Keine Nachrichten gefunden. Schreib dem Bot eine Nachricht und poste etwas in den Kanal,\n"
              "dann erneut ausführen.")
    for cid, (typ, name) in seen.items():
        hint = {"user": "→ TELEGRAM_ADMIN_ID", "private": "→ TELEGRAM_ADMIN_ID",
                "channel": "→ TELEGRAM_CHANNEL_ID"}.get(typ, "")
        print(f"{typ:10} {cid:>16}  {name}  {hint}")


def cmd_testmsg(cfg):
    cfg.require("bot", "admin")
    tg = TG(cfg.bot)
    me = tg.call("getMe")
    print(f"Bot: @{me['username']} (Token ok)")
    targets = [("Admin", "TELEGRAM_ADMIN_ID", cfg.admin,
                f"Hast du @{me['username']} im privaten Chat schon /start geschickt? Bots dürfen nur "
                "Nutzern schreiben, die sie vorher angeschrieben haben. Die ID muss deine numerische "
                "User-ID sein (sudo ./install.sh discover), nicht die ID des Bots oder dein @Name.")]
    if cfg.channel:
        targets.append(("Kanal", "TELEGRAM_CHANNEL_ID", cfg.channel,
                        f"Ist @{me['username']} Admin im Kanal? Die Kanal-ID beginnt mit -100 "
                        "(sudo ./install.sh discover, vorher etwas im Kanal posten)."))
    ok = True
    for name, var, chat, hint in targets:
        try:
            tg.call("sendMessage", chat_id=chat, text=f"✅ trainex-sync: Test an {name}")
            print(f"{name}: ok")
        except RuntimeError as ex:
            ok = False
            print(f"{name}: FEHLER – {ex}\n   {var}={chat}\n   {hint}")
    sys.exit(0 if ok else 1)


def main(argv):
    if len(argv) >= 2 and argv[0] == "--env":
        load_env_file(argv[1]); argv = argv[2:]
    if hasattr(time, "tzset"):
        os.environ["TZ"] = "Europe/Berlin"; time.tzset()
    cfg = Cfg()
    cmd = argv[0] if argv else "help"
    if cmd == "run":
        App(cfg).run()
    elif cmd == "check":
        f = argv[argv.index("--file") + 1] if "--file" in argv else None
        try:
            cmd_check(cfg, f, debug="--debug" in argv)
        except SyncError as ex:
            sys.exit(f"Fehler: {ex}")
    elif cmd == "discover":
        cmd_discover(cfg)
    elif cmd == "testmsg":
        cmd_testmsg(cfg)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
