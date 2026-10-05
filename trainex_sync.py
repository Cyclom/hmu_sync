#!/usr/bin/env python3
"""trainex-sync: TraiNex-Studienplan -> iCal-Abo + Telegram-Bot.

Nur Python-Standardbibliothek (>= 3.9).

Befehle:
  trainex_sync.py [--env DATEI] run                Dienst (Telegram-Bot + Zeitplan)
  trainex_sync.py [--env DATEI] check [--file X]   Testabruf, zeigt Änderungen, schreibt nichts
                                [--debug] [--dump DIR]   … mit Details zu jedem Schritt, Seiten nach DIR speichern
  trainex_sync.py [--env DATEI] discover           Telegram-Chat-IDs anzeigen
  trainex_sync.py [--env DATEI] testmsg            Testnachricht an Admin + Kanal
"""
import datetime as dt
import hashlib
import mimetypes
import html
import http.cookiejar
import json
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

VERSION = "1.12"
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
        a = e("ABSENCE_LIMIT", "20").strip().rstrip("%")
        self.absence_limit = int(a) if a.isdigit() and 0 <= int(a) <= 100 else 20
        g = e("CAMPUS_GEO", "").replace(" ", "")
        self.campus_geo = [float(x) for x in g.split(",")] if re.fullmatch(r"-?\d+\.\d+,-?\d+\.\d+", g) else None
        self.docs = e("DOCS", "1").strip().lower() not in ("0", "nein", "no", "off", "false")
        self.docs_semester = e("DOCS_SEMESTER", "").strip().rstrip(".")
        self.docs_url = e("DOCS_URL", "").strip()
        m = e("DOCS_MAX_MB", "100").strip()
        self.docs_max = (int(m) if m.isdigit() and int(m) > 0 else 100) * 1024 * 1024
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


class TooBig(SyncError):
    def __init__(self, size):
        super().__init__(f"Datei zu groß ({size / 1048576:.0f} MB)")
        self.size = size


def fetch_trainex(cfg, debug=False, extra=None):
    """Login, Stundenplan-Export, Logout. extra(req, dl) läuft in derselben Sitzung nach dem Export
    (Unterlagen), damit pro Zyklus nur ein Login nötig ist."""
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
                        for m in re.findall(r"[\"']([^\"'<>\s]*(?:einsatzplan_\w+|\w*(?:archiv|download|datei|"
                                            r"dokument|layout)\w*)\.cfm[^\"'<>\s]*)[\"']",
                                            body.decode("utf-8", "replace"), re.I)})
        if links:
            print(f"   Links: {' , '.join(links[:25])}")
        frames = [canon_url(f) for f in _frames(body, url)]
        if frames:
            print(f"   Frames: {' , '.join(frames[:6])}")
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

    def dl(step, url, fh, max_bytes, referer=None):
        """Lädt eine Datei gestreamt nach fh (der Dienst hat nur 128 MB RAM). Liefert die Antwort-Header."""
        rq = urllib.request.Request(url, headers={"Referer": referer} if referer else {})
        try:
            with op.open(rq, timeout=60) as r:
                size = int(r.headers.get("Content-Length") or 0)
                if size > max_bytes:
                    raise TooBig(size)
                n = 0
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > max_bytes:
                        raise TooBig(n)
                    fh.write(chunk)
                if debug:
                    print(f"\n── {step}: HTTP {r.status} → {urllib.parse.urlsplit(r.geturl()).path} · {n} Bytes · "
                          f"{r.headers.get('Content-Type')} · {r.headers.get('Content-Disposition') or '–'}")
                return r.headers
        except urllib.error.HTTPError as ex:
            raise SyncError(f"HTTP {ex.code} beim Download")
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise SyncError(f"Download fehlgeschlagen: {getattr(ex, 'reason', ex)}")

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
        if extra and _n_events(body) >= 0:
            extra(req, dl)
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


def module_name(ev):
    # „Biochemie/ Molekularbiologie“ -> „Biochemie/Molekularbiologie“
    return re.sub(r"\s*/\s*", "/", split_title(ev)[0])


def short(ev):
    """z. B. „M11 Physiologie - VL - Nierenphysiologie I“."""
    _, kind, topic = split_title(ev)
    module = module_name(ev)
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


# ───────────────────────── Module & Fehlzeiten ─────────────────────────

def duration_min(ev):
    """Dauer in Minuten; ganztägige Einträge zählen nicht als Unterricht."""
    if len(ev["s"]) == 8:
        return 0
    return max(0, int((to_dt(ev["e"]) - to_dt(ev["s"])).total_seconds() // 60))


def module_key(name):
    """Kurzer, stabiler Schlüssel für Button-Daten (Telegram erlaubt max. 64 Bytes)."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def uid_key(ev):
    return ev["uid"].split("@")[0][:24]


def absent_min(ev, absences):
    """Gefehlte Minuten eines Termins: „all“ = ganzer Termin, Zahl = Teil (z. B. verspätet)."""
    v = (absences or {}).get(ev["uid"])
    if v is None:
        return 0
    d = duration_min(ev)
    return d if v == "all" else max(0, min(int(v), d))


def kind_label(kind):
    """„Vorlesung (VL)“; Termine ohne Art landen in „Ohne Art“."""
    if not kind:
        return "Ohne Art"
    s = kind_short(kind)
    return kind if s == kind else f"{kind} ({s})"


def kind_order(kind):
    """Bekannte Arten in der Reihenfolge von KIND_SHORT, unbekannte alphabetisch dahinter."""
    order = list(KIND_SHORT)
    return (order.index(kind), "") if kind in order else (len(order), kind or "~")


def budget_stats(evs, absences, now, pct):
    """Fehlzeit-Budget für eine Termingruppe (eine Veranstaltungsart eines Moduls)."""
    past = [x for x in evs if to_dt(x["s"]) <= now]
    up = [x for x in evs if to_dt(x["s"]) > now]
    total = sum(map(duration_min, evs))
    limit = total * pct // 100
    missed = sum(absent_min(x, absences) for x in past)
    planned = sum(absent_min(x, absences) for x in up)
    rest = limit - missed - planned
    # Wie viele weitere kommende Termine passen noch ins Budget (kürzeste zuerst)?
    budget, skippable = max(rest, 0), 0
    for d in sorted(duration_min(x) for x in up if not absent_min(x, absences)):
        if d > budget:
            break
        budget -= d
        skippable += 1
    return {"events": evs, "n": len(evs), "total": total, "limit": limit, "past": len(past), "up": len(up),
            "up_min": sum(map(duration_min, up)), "missed": missed, "planned": planned, "rest": rest,
            "skippable": skippable}


def module_stats(events, absences, now, pct):
    """Fasst Termine pro Modul zusammen. Die Anwesenheit wird je Veranstaltungsart (VL, S, P …)
    erfasst, deshalb hat jede Art eines Moduls ihr eigenes Budget von pct % ihrer Minuten.
    Begonnene Termine gelten als stattgefunden, Fehlzeiten bei kommenden Terminen als „geplant“."""
    groups = {}
    for ev in events:
        if duration_min(ev):
            groups.setdefault(module_name(ev), []).append(ev)
    out = []
    for name in sorted(groups):
        evs = sorted(groups[name], key=lambda x: (x["s"], x["sum"]))
        by_kind = {}
        for x in evs:
            by_kind.setdefault(split_title(x)[1], []).append(x)
        kinds = []
        for kind in sorted(by_kind, key=kind_order):
            g = budget_stats(by_kind[kind], absences, now, pct)
            g.update(kind=kind, short=kind_short(kind) or "–", label=kind_label(kind))
            kinds.append(g)
        up = [x for x in evs if to_dt(x["s"]) > now]
        out.append({"name": name, "key": module_key(name), "events": evs, "n": len(evs),
                    "total": sum(g["total"] for g in kinds), "past": len(evs) - len(up), "up": len(up),
                    "up_min": sum(map(duration_min, up)), "missed": sum(g["missed"] for g in kinds),
                    "planned": sum(g["planned"] for g in kinds), "kinds": kinds})
    return out


def find_module(mods, q):
    """„M11“, „physio“ oder der volle Name; liefert alle Treffer der genauesten Stufe."""
    ql = norm(q).lower()
    tests = (lambda n: n == ql, lambda n: n.split()[0] == ql, lambda n: n.startswith(ql), lambda n: ql in n)
    for test in tests:
        hit = [m for m in mods if test(m["name"].lower())]
        if hit:
            return hit
    return []


def kind_of(m, ev):
    """Die Veranstaltungsart-Gruppe eines Moduls, zu der ein Termin gehört."""
    return next(g for g in m["kinds"] if any(x["uid"] == ev["uid"] for x in g["events"]))


ICONS = ["🟢", "🟡", "🔴"]


def budget_icon(g):
    """Ampel einer Art; für ein Modul die schlechteste seiner Arten."""
    if "kinds" in g:
        return max((budget_icon(k) for k in g["kinds"]), key=ICONS.index, default="🟢")
    if g["rest"] < 0:
        return "🔴"
    if g["rest"] * 2 < g["limit"] or (g["up"] and not g["skippable"]):
        return "🟡"
    return "🟢"


def rest_text(g):
    return f"übrig {g['rest']} min" if g["rest"] >= 0 else f"überschritten um {-g['rest']} min"


def budget_line(g):
    parts = [f"Gefehlt {g['missed']} min"]
    if g["planned"]:
        parts.append(f"geplant {g['planned']} min")
    parts.append(rest_text(g) if g["rest"] >= 0 else f"<b>{rest_text(g)}</b>")
    return f"{budget_icon(g)} " + " · ".join(parts)


def skippable_text(g):
    if not g["skippable"]:
        return "<b>kein weiterer Termin verpassbar</b>"
    return f"noch {g['skippable']} davon verpassbar"


def n_termine(n):
    return f"{n} Termin" if n == 1 else f"{n} Termine"


def kind_block(g, pct):
    """Budget einer Veranstaltungsart, z. B. Vorlesung oder Seminar."""
    up = (f"→ Kommend: {n_termine(g['up'])} ({g['up_min']} min) · {skippable_text(g)}" if g["up"]
          else "→ Abgeschlossen")
    return [f"<u>{e(g['label'])}</u>: {n_termine(g['n'])} - {g['total']} min",
            f"→ Maximale Fehlzeit ({pct}%): {g['limit']} min",
            f"→ {budget_line(g)}", up]


def event_icon(ev, absences, now):
    d, a = duration_min(ev), absent_min(ev, absences)
    if not a:
        return "✅" if to_dt(ev["s"]) <= now else "▫️"
    if to_dt(ev["s"]) > now:
        return "💤"
    return "❌" if a >= d else "🟠"


def fmt_hours(mins):
    return f"{mins // 60} h {mins % 60:02d} min"


def format_modules(mods, pct, show_all=False):
    if not mods:
        return "Noch keine Termine gespeichert – erst /sync."
    shown = [m for m in mods if m["up"] or show_all]
    L = [f"📚 <b>Module & Fehlzeiten</b>\nGrenze {pct} % je Veranstaltungsart (Vorlesung, Seminar, Praktikum …)"]
    for m in shown:
        block = [f"{budget_icon(m)} <b>{e(m['name'])}</b>", f"→ Insgesamt {n_termine(m['n'])} - {m['total']} min"]
        for g in m["kinds"]:
            block += kind_block(g, pct)
        L.append("\n".join(block))
    hidden = len(mods) - len(shown)
    if hidden:
        L.append(f"<i>{hidden} abgeschlossene(s) Modul(e) ausgeblendet – /modules alle</i>")
    elif not shown:
        L.append("Keine kommenden Termine.")
    L.append("Tippe auf ein Modul für alle Termine und zum Eintragen von Fehlzeiten.")
    return "\n\n".join(L)


def modules_keyboard(mods, show_all=False):
    rows = [[{"text": f"{budget_icon(m)} {m['name'][:40]}", "callback_data": f"m:{m['key']}"}]
            for m in mods if m["up"] or show_all]
    if len(rows) < len(mods) or show_all:
        rows.append([{"text": "Nur aktuelle" if show_all else "Alle Module", "callback_data": "o" if show_all else "oa"}])
    return {"inline_keyboard": rows}


def event_line(i, ev, m, absences, now):
    d, a = duration_min(ev), absent_min(ev, absences)
    plan = " geplant" if to_dt(ev["s"]) > now else ""
    if not a:
        note = ""
    elif a >= d:
        note = " · <b>fehlen geplant</b>" if plan else " · <b>gefehlt</b>"
    else:
        note = f" · <b>−{a} min{plan}</b>"
    icon = event_icon(ev, absences, now)
    what = short(ev).removeprefix(m["name"] + " - ")
    return f"<code>{i:>2}</code> {icon} {fmt_day(ev)} {fmt_time(ev)} · {d} min · {e(what)}{note}"


def format_module(m, pct, absences, now, note="", max_len=3800):
    head = ([note, ""] if note else []) + [
        f"📘 <b>{e(m['name'])}</b>",
        f"→ Insgesamt {n_termine(m['n'])} - {m['total']} min ({fmt_hours(m['total'])})",
        f"→ Grenze {pct} % je Veranstaltungsart",
    ]
    for g in m["kinds"]:
        head += [""] + kind_block(g, pct)
    if any(g["skippable"] for g in m["kinds"]):
        head.append("<i>verpassbar: kürzeste Termine zuerst, je Art gerechnet</i>")
    lines = [event_line(i, ev, m, absences, now) for i, ev in enumerate(m["events"], 1)]
    foot = ["", "✅ da · ❌ gefehlt · 🟠 teilweise · 💤 geplant · ▫️ kommend",
            "Nummer antippen = ganzer Termin gefehlt/geplant (nochmal = zurück). "
            f"Teilweise, z. B. 30 min zu spät: <code>/absent {e(m['name'].split()[0])} 3 30</code>"]
    # Zu lang für eine Nachricht: älteste Termine zusammenfassen
    first = 0
    while first < len(lines) - 1 and len("\n".join(head + [""] + lines[first:] + foot)) > max_len:
        first += 1
    body = ([f"<i>… {first} frühere Termine ausgeblendet</i>"] if first else []) + lines[first:]
    return "\n".join(head + [""] + body + foot), first


def module_keyboard(m, absences, now, first=0):
    btns = []
    for i, ev in enumerate(m["events"], 1):
        if i <= first:
            continue
        mark = event_icon(ev, absences, now) if absent_min(ev, absences) else ""
        btns.append({"text": f"{mark}{i}", "callback_data": f"t:{m['key']}:{uid_key(ev)}"})
    btns = btns[-90:]  # Telegram begrenzt die Anzahl der Buttons
    rows = [btns[i:i + 6] for i in range(0, len(btns), 6)]
    rows.append([{"text": "« Übersicht", "callback_data": "o"}])
    return {"inline_keyboard": rows}


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


# ───────────────────────── Unterlagen (Lernen → Archiv) ─────────────────────────

# Endungen, die als Dokument gelten und so auf dem Server abgelegt werden (alles andere als .bin,
# damit z. B. HTML/SVG nie als Webseite unter der eigenen Domain ausgeliefert wird)
DOC_EXT = {"pdf", "doc", "docx", "ppt", "pptx", "pps", "ppsx", "xls", "xlsx", "odt", "odp", "ods", "rtf", "txt",
           "csv", "png", "jpg", "jpeg", "gif", "heic", "zip", "mp3", "m4a", "mp4", "mov", "epub", "key", "pages",
           "numbers"}
DL_HINT = re.compile(r"(?i)download|datei|dokument|\bdok|anhang|attach|file|upload|(?<![a-z])dl(?![a-z])")
NOT_DOC = re.compile(r"(?i)navigation|layout|logout|start\.cfm|einsatzplan")
TOKEN_PARAM = re.compile(r"(?:TokCF19|IDphp17|sec18m)=.*|\d+")
GENERIC_NAME = re.compile(r"(?i)\W*(download|herunterladen|öffnen|oeffnen|anzeigen|ansehen|datei|dokument|pdf|"
                          r"link|hier|mehr)?\W*")
HEADER_WORDS = re.compile(r"(?i)\b(datei(name)?|name|datum|größe|groesse|titel|beschreibung|typ|download|art|"
                          r"bezeichnung|hochgeladen|dozent(in)?|von|am|aktion(en)?)\b")
SEM_RANGE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(?:bis|-|–)\s*(\d{1,2})\.(\d{1,2})\.(\d{4})")
TG_MAX_FILE = 50 * 1024 * 1024  # Limit für Uploads über die Bot-API


def _dec(body):
    if isinstance(body, str):
        return body
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", "replace")


def _text(fragment):
    t = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", fragment)
    return norm(html.unescape(re.sub(r"<[^>]+>", " ", t)))


def _attr(tag, name):
    m = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % name, tag, re.I)
    return html.unescape(next(g for g in m.groups() if g is not None)) if m else None


def canon_url(u):
    """URL ohne die zeitabhängigen TraiNex-Tokens – bleibt über Sitzungen hinweg gleich."""
    p = urllib.parse.urlsplit(u)
    q = sorted(x for x in p.query.split("&") if x and not TOKEN_PARAM.fullmatch(x))
    return p.path + ("?" + "&".join(q) if q else "")


def with_tok(u):
    p = urllib.parse.urlsplit(u)
    q = [x for x in p.query.split("&") if x and not TOKEN_PARAM.fullmatch(x)] + [_tok()]
    return urllib.parse.urlunsplit(p._replace(query="&".join(q)))


def _frames(body, base_url):
    return [urllib.parse.urljoin(base_url, html.unescape(m))
            for m in re.findall(r"""(?is)<i?frame\b[^>]*?\bsrc\s*=\s*["']([^"']+)""", _dec(body))]


def _urls_in(attrs):
    """Ziel-URLs eines Links: href, oder bei javascript:/onclick die URL in window.open('…') o. Ä."""
    out = []
    for m in re.finditer(r"""\b(href|onclick)\s*=\s*(["'])(.*?)\2""", attrs, re.I | re.S):
        v = html.unescape(m.group(3)).strip()
        if m.group(1).lower() == "href" and not re.match(r"(?i)javascript:|#|mailto:", v):
            out.append(v)
        else:
            out += re.findall(r"""["']([^"'\s]+?\.\w{2,5}(?:\?[^"'\s]*)?)["']""", v)
    return out


def _ext(name):
    tail = (name or "").rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[-1].lower() if "." in tail else ""


def is_doc_url(u):
    p = urllib.parse.urlsplit(u)
    if p.scheme not in ("", "http", "https") or not p.path or NOT_DOC.search(p.path):
        return False
    return _ext(p.path) in DOC_EXT or bool(DL_HINT.search(p.path + "?" + p.query))


def safe_name(x, limit=120):
    x = norm(re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", x or "")).strip(" .")
    return x[:limit].strip(" .") or "Dokument"


def _generic(x):
    return not x or len(x) < 2 or bool(GENERIC_NAME.fullmatch(x))


def _is_header(text):
    words = re.findall(r"\w+", text)
    return len(text) <= 80 and len(HEADER_WORDS.findall(text)) >= max(2, len(words) // 2)


def url_filename(url):
    """Dateiname aus dem Link: letzter Pfadteil oder ein Parameter wie Filename=…pdf (TraiNex: datei_laden.cfm)."""
    p = urllib.parse.urlsplit(url)
    for c in [urllib.parse.unquote(p.path.rsplit("/", 1)[-1])] + [v for _, v in urllib.parse.parse_qsl(p.query)]:
        if _ext(c) in DOC_EXT:
            return c
    return ""


def doc_title(link_text, title_attr, row_text, url):
    for c in (link_text, title_attr):
        if not _generic(c):
            return c[:150]
    m = re.search(r"[^\s/\\<>|]+\.(?:%s)\b" % "|".join(sorted(DOC_EXT)), row_text, re.I)
    if m:
        return m.group(0)
    return url_filename(url) or row_text[:80] or "Dokument"


def parse_archive(body, page_url):
    """Dokument-Links der Archiv-Liste. Zeilen ohne Dokument-Link gelten als Überschrift (Ordner/Modul)
    für die folgenden Dokumente. Liefert [{key, url, title, folder, info}]."""
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", _dec(body))
    own = urllib.parse.urlsplit(page_url).path
    splitter = r"(?is)<tr\b.*?(?=<tr\b|</table>|\Z)" if re.search(r"(?i)<tr\b", text) \
        else r"(?is)<(?:li|div|p|h\d)\b.*?(?=<(?:li|div|p|h\d)\b|\Z)"
    docs, seen, folder = [], {}, ""
    for row in re.findall(splitter, text):
        links = []
        for m in re.finditer(r"(?is)<a\b([^>]*)>(.*?)</a>", row):
            for u in _urls_in(m.group(1)):
                full = urllib.parse.urljoin(page_url, u)
                p = urllib.parse.urlsplit(full)
                if is_doc_url(full) and (p.path != own or DL_HINT.search(p.query)):
                    links.append((full, _text(m.group(2)), _attr(m.group(1), "title") or ""))
                    break
        rtext = _text(row)
        if not links:
            if 0 < len(rtext) <= 150 and not re.search(r"(?i)<th\b|<input|<select|<form", row) \
                    and not _is_header(rtext):
                folder = rtext
            continue
        for full, ltxt, ttl in links:
            key = canon_url(full)
            title = doc_title(ltxt, ttl, rtext, full)
            if key in seen:  # dasselbe Dokument zweimal verlinkt (Symbol + Name)
                if _generic(seen[key]["title"]) or seen[key]["title"] == rtext[:80]:
                    seen[key]["title"] = title
                continue
            info = norm(rtext.replace(ltxt, " ")) if ltxt else rtext
            seen[key] = d = {"key": key, "url": full, "title": title, "folder": folder, "info": info[:300]}
            docs.append(d)
    return docs


def _sem_num(label):
    """„3.“ / „3. Semester“ / „3. Semester: 01.10.2026 bis …“ → 3; „alle“ → None."""
    m = re.match(r"\s*(\d{1,2})\s*\.?\s*(?:Semester\b|:|$)", label, re.I)
    return int(m.group(1)) if m else None


def _in_range(m, today):
    a = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    z = dt.date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
    return a <= today <= z


def semester_hint(text, today):
    """Nummer aus einem Hinweis wie „3. Semester: 01.10.2026 bis 31.03.2027“, dessen Zeitraum heute enthält."""
    for m in re.finditer(r"(\d{1,2})\.\s*Semester[^0-9<]{0,12}(" + SEM_RANGE.pattern + ")", text, re.I):
        if _in_range(SEM_RANGE.search(m.group(2)), today):
            return int(m.group(1))
    return None


def pick_semester(options, today, want="", page=""):
    """options: [(value, label, selected, attrs)]. Reihenfolge: DOCS_SEMESTER; Option, deren Zeitraum
    (im Text oder z. B. im title) heute enthält; Zeitraum-Hinweis irgendwo auf der Seite; Vorauswahl.
    Liefert (value, „3. Semester“) oder None. „alle“ wird nie gewählt."""
    opts = [(v, _sem_num(lab) or (int(v) if v.isdigit() and _sem_num(lab + ".") else None), sel, lab + " " + a)
            for v, lab, sel, a in options]
    opts = [o for o in opts if o[1]]
    found = None
    if want:
        found = next((o for o in opts if str(o[1]) == want or o[0] == want), None)
    else:
        for o in opts:
            m = SEM_RANGE.search(html.unescape(o[3]))
            if m and _in_range(m, today):
                found = o
                break
        if not found:
            n = semester_hint(html.unescape(page), today)
            found = next((o for o in opts if o[1] == n), None) if n else None
        if not found:
            found = next((o for o in opts if o[2]), None)
    return (found[0], f"{found[1]}. Semester") if found else None


def semester_form(body, page_url, today, want=""):
    """Formular mit der Semester-Auswahl (TraiNex: „Nur [1.|2.|3.|alle] Semester“ + „anzeigen“).
    Liefert (method, url, data, label) oder None."""
    page = _dec(body)
    for fm in re.finditer(r"(?is)<form\b([^>]*)>(.*?)</form>", page):
        attrs, inner = fm.groups()
        target = None
        for sm in re.finditer(r"(?is)<select\b([^>]*)>(.*?)</select>", inner):
            opts = []
            for o in re.finditer(r"(?is)<option\b([^>]*)>(.*?)(?=<option\b|\Z)", sm.group(2)):
                label = _text(o.group(2))
                v = _attr(o.group(1), "value")
                opts.append((label if v is None else v, label, bool(re.search(r"(?i)\bselected\b", o.group(1))),
                             o.group(1)))
            if not (re.search(r"(?i)sem", _attr(sm.group(1), "name") or "")
                    or any(re.search(r"(?i)semester", x[1]) for x in opts)):
                continue
            pick = pick_semester(opts, today, want, page)
            if pick:
                target = (_attr(sm.group(1), "name"), pick)
                break
        if not target:
            continue
        sel_name, (sel_val, label) = target
        fields, radios, submits = [], {}, []
        for m in re.finditer(r"(?is)<(input|select|button)\b([^>]*)>", inner):
            tag, a = m.group(1).lower(), m.group(2)
            name = _attr(a, "name")
            if not name:
                continue
            typ = (_attr(a, "type") or ("submit" if tag == "button" else "text")).lower()
            if tag == "select":
                if name == sel_name:
                    fields.append((name, sel_val))
                    continue
                body_sel = inner[m.end():inner.find("</select>", m.end())]
                os_ = re.findall(r"(?is)<option\b([^>]*)>([^<]*)", body_sel)
                chosen = next((o for o in os_ if re.search(r"(?i)\bselected\b", o[0])), os_[0] if os_ else None)
                if chosen:
                    v = _attr(chosen[0], "value")
                    fields.append((name, norm(html.unescape(chosen[1])) if v is None else v))
            elif typ == "radio":
                stop = r"(?i)<(?:input|select|br|/?td|/label|/div|/p)\b"
                lab = _text(re.split(stop, inner[m.end():m.end() + 300])[0])
                if not re.search(r"(?i)semester|alle|nur", lab):
                    lab = _text(re.split(stop, inner[max(0, m.start() - 300):m.start()])[-1])
                radios.setdefault(name, []).append(
                    (_attr(a, "value") or "on", lab, bool(re.search(r"(?i)\bchecked\b", a))))
            elif typ == "checkbox":
                if re.search(r"(?i)\bchecked\b", a):
                    fields.append((name, _attr(a, "value") or "on"))
            elif typ in ("submit", "image"):
                txt = _attr(a, "value") or _text(inner[m.end():inner.find("</button>", m.end())]
                                                  if tag == "button" else "")
                submits.append((name, _attr(a, "value") or "", typ, txt))
            elif typ not in ("reset", "file", "button"):
                fields.append((name, _attr(a, "value") or ""))
        for name, opts in radios.items():
            sem = [o for o in opts if re.search(r"(?i)semester", o[1])]
            want_r = (next((o for o in sem if re.match(r"(?i)\s*nur\b", o[1])), None)
                      or next((o for o in sem if not re.search(r"(?i)\balle\b", o[1])), None))
            if want_r:
                fields.append((name, want_r[0]))
            else:
                cur = next((o for o in opts if o[2]), None)
                if cur:
                    fields.append((name, cur[0]))
        sub = (next((x for x in submits if norm(x[3] + " " + x[1]).lower() in ("anzeigen", "anzeigen anzeigen")), None)
               or next((x for x in submits if re.search(r"(?i)anzeigen", x[3] + x[1])
                        and not re.search(r"(?i)alle", x[3] + x[1])), None)
               or (submits[0] if submits else None))
        if sub:
            if sub[2] == "image":
                fields += [(sub[0] + ".x", "1"), (sub[0] + ".y", "1")]
            else:
                fields.append((sub[0], sub[1]))
        method = (_attr(attrs, "method") or "get").upper()
        action = urllib.parse.urljoin(page_url, _attr(attrs, "action") or page_url)
        data = urllib.parse.urlencode(fields)
        if method == "GET":
            return "GET", action.split("?")[0] + "?" + data, None, label
        return "POST", action, data.encode(), label
    return None


def find_archive(cfg, req, b):
    """URL der Archiv-Seite (Lernen → Archiv). Im Menü heißt der Bereich „Lernen“, intern „Kursraum“."""
    if cfg.docs_url:
        return with_tok(urllib.parse.urljoin(b + "/", cfg.docs_url))
    nav = f"{b}/navigation/student_layout.cfm?{_tok()}&area=Kursraum&subarea=archiv"
    _, _, page = req("D1 Archiv (Navigation)", nav)
    for step in ("D1b", "D1c"):
        hit = (next((f for f in _frames(page, nav) if re.search(r"(?i)archiv", f) and not NOT_DOC.search(f)), None)
               or _find_url(page, nav, r"archiv\w*\.cfm", exclude=r"navigation|layout"))
        if hit:
            return hit
        link = None
        for m in re.finditer(r"(?is)<a\b([^>]*)>(.*?)</a>", _dec(page)):
            if re.fullmatch(r"(?i)archiv", _text(m.group(2))) and _urls_in(m.group(1)):
                link = urllib.parse.urljoin(nav, _urls_in(m.group(1))[0])
                break
        if not link:
            break
        if not re.search(r"(?i)navigation|layout", link):
            return link
        nav = link
        _, _, page = req(f"{step} Archiv (Menü)", nav)
    raise SyncError("Archiv-Seite (Lernen → Archiv) nicht gefunden. Details: sudo ./install.sh debug")


def _page(r):
    """(status, headers, body) → Text im angegebenen Zeichensatz (das Archiv ist ISO-8859-1)."""
    cs = r[1].get_content_charset() if r[1] is not None else None
    try:
        return r[2].decode(cs) if cs else _dec(r[2])
    except (LookupError, UnicodeDecodeError):
        return _dec(r[2])


def _submit(req, step, method, target, data, referer):
    hdr = {"Referer": referer}
    if method == "POST":
        hdr["Content-Type"] = "application/x-www-form-urlencoded"
    return _page(req(step, target, data, hdr))


def fetch_archive(cfg, req, today, cache=None):
    """Archiv öffnen, „Nur Semester“ + aktuelles Semester wählen, „anzeigen“. Ohne Filter zeigt TraiNex alle
    Semester, „anzeigen“ ist also immer nötig. Liefert (Liste, Semester, URL, Cache).
    Mit Cache (Formular aus dem letzten Lauf) wird direkt „anzeigen“ geschickt: 1 statt 3 Seitenabrufe.
    Die Antwort enthält das Formular erneut; passt das Semester nicht mehr (Semesterwechsel) oder fehlt es,
    geht es den normalen Weg."""
    if cache:
        try:
            target = with_tok(urllib.parse.urljoin(cfg.base + "/", cache["url"]))
            if cache["method"] == "GET":
                target = target.replace("?", "?" + cache["data"] + "&", 1)
            page = _submit(req, "D3 anzeigen (direkt)", cache["method"], target,
                           cache["data"].encode() if cache["method"] == "POST" else None, target)
            form = semester_form(page, target, today, cfg.docs_semester)
            if form and form[3] == cache["label"]:
                return parse_archive(page, target), cache["label"], target, cache
            log("Archiv: Direktabruf passt nicht mehr – normaler Weg")
        except SyncError as ex:
            log("Archiv: Direktabruf fehlgeschlagen –", ex)
    url = find_archive(cfg, req, cfg.base)
    page = _page(req("D2 Archiv", url))
    form = semester_form(page, url, today, cfg.docs_semester)
    if not form:
        raise SyncError("Semester-Auswahl im Archiv nicht gefunden"
                        + (f" (DOCS_SEMESTER={cfg.docs_semester})" if cfg.docs_semester else "")
                        + ". Fest einstellen mit DOCS_SEMESTER=3 in der .env. Details: sudo ./install.sh debug")
    method, target, data, label = form
    page = _submit(req, "D3 anzeigen", method, target, data, url)
    if method == "GET":
        p = urllib.parse.urlsplit(target)
        cache = {"method": "GET", "url": p.path,
                 "data": "&".join(x for x in p.query.split("&") if x and not TOKEN_PARAM.fullmatch(x))}
    else:
        cache = {"method": "POST", "url": canon_url(target), "data": data.decode()}
    cache["label"] = label
    return parse_archive(page, target), label, target, cache


def doc_id(key):
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def plan_docs(items, listing, skip=()):
    """Ordnet die Archiv-Liste gespeicherten Dokumenten zu: erst über den Link, dann (neu hochgeladene
    Fassung mit neuem Link) über Ordner + Titel. Liefert (pairs[(id, d)], neu[d], entfernt[id])."""
    by_key = {v["key"]: k for k, v in items.items()}
    pairs, rest, used = [], [], set()
    for d in listing:
        if d["key"] in skip:
            continue
        k = by_key.get(d["key"])
        if k and k not in used:
            pairs.append((k, d)); used.add(k)
        else:
            rest.append(d)
    new = []
    for d in rest:
        k = next((k for k, v in items.items() if k not in used
                  and v["folder"] == d["folder"] and v["title"] == d["title"]), None)
        if k:
            pairs.append((k, d)); used.add(k)
        else:
            new.append(d)
    return pairs, new, [k for k in items if k not in used]


def file_ext(headers, url, title):
    fn = None
    if headers is not None:
        fn = headers.get_filename()
        if fn:
            try:
                fn = fn.encode("latin-1").decode("utf-8")  # rohes UTF-8 im Header
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
    for c in (fn, url_filename(url), title):
        if _ext(c) in DOC_EXT:
            return _ext(c), fn
    ct = (headers.get_content_type() if headers is not None else "") or ""
    guess = (mimetypes.guess_extension(ct) or "").lstrip(".")
    return (guess if guess in DOC_EXT else (_ext(fn) or "bin")), fn


def display_name(title, ext, cd_name):
    """Dateiname für Telegram/iCloud: Titel aus TraiNex + Endung (Titel sind lesbarer als Upload-Namen)."""
    base = title if not _generic(title) else (cd_name or "Dokument")
    base = safe_name(base)
    if ext and ext != "bin" and _ext(base) != ext:
        base = f"{base}.{ext}"
    return base


def sync_docs(old, listing, label, now, grab=None, force=False):
    """Gleicht die Archiv-Liste mit dem gespeicherten Stand ab und lädt neue/geänderte Dateien über
    grab(d, item) → {sha, file, ext, cd, size} | {"big": Bytes} | {"html": True} (None = Probelauf).
    Liefert (state_neu, entries[(typ, item)], info)."""
    items = {k: dict(v) for k, v in old.get("items", {}).items()}
    skip = set(old.get("skip", []))
    sem_changed = bool(items) and old.get("semester") != label
    first = not items or sem_changed
    if sem_changed:
        items = {}
    pairs, new, removed = plan_docs(items, listing, skip)
    if not first and not force:
        if not listing:
            raise SyncError(f"Archiv-Liste ist leer ({len(items)} Dokumente gespeichert) – nichts geändert. "
                            "Falls korrekt: /sync force")
        if len(removed) > max(3, len(items) // 2):
            raise SyncError(f"{len(removed)} von {len(items)} Dokumenten würden entfallen – nichts geändert. "
                            "Falls korrekt: /sync force")
    ts = int(now.timestamp())
    entries, failed = [], 0

    def fetch(d, item):
        nonlocal failed
        if grab is None:
            return None
        try:
            got = grab(d, item)
        except SyncError as ex:
            log(f"Download fehlgeschlagen ({d['title']}):", ex)
            failed += 1
            return False
        if got.get("html"):  # Link führt auf eine Seite, nicht auf eine Datei → künftig überspringen
            skip.add(d["key"])
            return False
        return got

    for k, d in pairs:
        o = items[k]
        changed = d["key"] != o["key"] or d["info"] != o["info"] or not o.get("present", True)
        o.update(title=d["title"], folder=d["folder"])
        if not changed:
            continue
        got = fetch(d, o)
        if got is None:
            entries.append(("upd?", o))
        elif got:  # erst nach erfolgreichem Download übernehmen, sonst im nächsten Lauf erneut
            o.update(key=d["key"], info=d["info"])
            prev = o.get("sha")
            if got.get("big"):
                o.update(big=got["big"], sha=None, file=None)
            else:
                o.update(sha=got["sha"], file=got["file"], size=got["size"], big=0,
                         name=display_name(d["title"], got["ext"], got.get("cd")))
            o["present"] = True
            if prev != o.get("sha") or got.get("big"):
                o["changed"] = ts
                entries.append(("upd", o))
    for d in new:
        item = {"id": doc_id(d["key"]), "key": d["key"], "title": d["title"], "folder": d["folder"],
                "info": d["info"], "added": ts, "changed": ts, "present": True}
        got = fetch(d, item)
        if got is False:
            continue
        if got:
            if got.get("big"):
                item.update(big=got["big"], sha=None, file=None, name=display_name(d["title"], _ext(d["title"]), None))
            else:
                item.update(sha=got["sha"], file=got["file"], size=got["size"], big=0,
                            name=display_name(d["title"], got["ext"], got.get("cd")))
        while item["id"] in items:
            item["id"] = doc_id(item["id"] + d["key"])
        items[item["id"]] = item
        if not first:
            entries.append(("new", item))
    gone = []
    for k in removed:
        gone.append(items.pop(k))
        if not first:
            entries.append(("del", gone[-1]))
    state = {"semester": label, "items": items, "skip": sorted(skip), "checked": ts}
    return state, entries, {"first": first, "sem_changed": sem_changed, "failed": failed,
                            "listed": len(listing), "removed": gone}


def doc_paths(items):
    """Pfad je Dokument für den Spiegel in der Dateien-App: „Ordner/Name“, doppelte Namen mit (2), (3) …"""
    out, used = {}, set()
    for it in sorted(items.values(), key=lambda x: (x.get("added", 0), x["id"])):
        folder = safe_name(it["folder"] or "Allgemein", 80)
        name = it.get("name") or safe_name(it["title"])
        stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
        p, n = f"{folder}/{name}", 2
        while p.lower() in used:
            p = f"{folder}/{stem} ({n}){dot}{ext}"; n += 1
        used.add(p.lower())
        out[it["id"]] = p
    return out


def fmt_size(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB"


def build_docs_json(docs, base_url, updated):
    paths = doc_paths(docs.get("items", {}))
    files = []
    for it in sorted(docs.get("items", {}).values(), key=lambda x: paths[x["id"]].lower()):
        if not it.get("file"):
            continue
        files.append({"path": paths[it["id"]], "folder": paths[it["id"]].split("/")[0],
                      "name": paths[it["id"]].split("/", 1)[1], "url": f"{base_url}/f/{it['file']}",
                      "size": it.get("size", 0), "added": it.get("added", 0), "changed": it.get("changed", 0)})
    data = {"semester": docs.get("semester", ""), "updated": int(updated), "files": files}
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


DOCS_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="color-scheme" content="light dark">
<title>Unterlagen</title>
<style>
:root{{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--mut:#6e6e73;--acc:#0071e3;--line:#d2d2d7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--card:#1c1c1e;--fg:#f5f5f7;--mut:#98989d;--acc:#0a84ff;--line:#38383a}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:24px 16px}}
main{{max-width:640px;margin:0 auto}}
.card{{background:var(--card);border-radius:18px;padding:18px 20px;margin-bottom:16px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:17px;margin:0 0 8px}}
p,.m{{color:var(--mut)}} p{{margin:0}} .m{{font-size:13px;white-space:nowrap}}
ul{{list-style:none;margin:0;padding:0}} li{{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-top:1px solid var(--line)}}
li:first-child{{border-top:0}} a{{color:var(--acc);text-decoration:none;word-break:break-word}}
.new{{background:var(--acc);color:#fff;border-radius:6px;font-size:11px;padding:1px 6px;margin-left:6px;vertical-align:2px}}
</style></head><body><main>
<div class="card"><h1>📚 Unterlagen</h1><p>{semester} · {n} Dokumente · Stand {updated}</p></div>
{folders}
</main></body></html>
"""


def build_docs_page(docs, updated):
    paths = doc_paths(docs.get("items", {}))
    groups = {}
    for it in docs.get("items", {}).values():
        groups.setdefault(paths[it["id"]].split("/")[0], []).append(it)
    cards = []
    for folder in sorted(groups, key=str.lower):
        rows = []
        for it in sorted(groups[folder], key=lambda x: paths[x["id"]].lower()):
            name = paths[it["id"]].split("/", 1)[1]
            badge = '<span class="new">neu</span>' if updated - it.get("changed", 0) < 7 * 86400 else ""
            when = dt.datetime.fromtimestamp(it.get("changed", 0), TZ).strftime("%d.%m.%Y")
            if it.get("file"):
                link = f'<a href="f/{e(it["file"])}" download="{html.escape(name)}">{e(name)}</a>'
                meta = f"{when} · {fmt_size(it.get('size', 0))}"
            else:
                link = e(name)
                meta = f"{when} · zu groß, nur im TraiNex"
            rows.append(f'<li><span>{link}{badge}</span><span class="m">{meta}</span></li>')
        cards.append(f'<div class="card"><h2>{e(folder)}</h2><ul>{"".join(rows)}</ul></div>')
    return DOCS_PAGE.format(semester=e(docs.get("semester") or "–"), n=len(docs.get("items", {})),
                            updated=dt.datetime.fromtimestamp(updated, TZ).strftime("%d.%m.%Y %H:%M"),
                            folders="\n".join(cards) or '<div class="card"><p>Noch keine Dokumente.</p></div>'
                            ).encode("utf-8")


def format_doc_caption(typ, it):
    head = {"new": "📄 <b>Neues Dokument</b>", "upd": "🔁 <b>Dokument aktualisiert</b>"}[typ]
    folder = f" · {e(it['folder'])}" if it.get("folder") else ""
    text = f"{head}{folder}\n{e(it.get('name') or it['title'])}"
    big = it.get("big") or (it.get("size", 0) > TG_MAX_FILE and it["size"])
    if big:
        text += f"\n⚠️ Zu groß für Telegram ({fmt_size(big)}) – bitte im TraiNex herunterladen."
    return text


class _Hashing:
    def __init__(self, fh, h):
        self.fh, self.h = fh, h

    def write(self, chunk):
        self.h.update(chunk)
        self.fh.write(chunk)


def download_doc(dl, d, store, max_bytes, referer=None):
    """Lädt ein Dokument nach store/<sha256>.<endung> (gleicher Inhalt = gleiche Datei)."""
    fd, tmp = tempfile.mkstemp(dir=store, prefix=".dl-")
    try:
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as f:
            try:
                hdr = dl(f"D4 {d['title'][:40]}", d["url"], _Hashing(f, h), max_bytes, referer)
            except TooBig as ex:
                return {"big": ex.size}
        if hdr.get_content_type() == "text/html" and not hdr.get_filename():
            return {"html": True}
        ext, cd = file_ext(hdr, d["url"], d["title"])
        sha = h.hexdigest()
        name = f"{sha[:32]}.{ext if ext in DOC_EXT else 'bin'}"
        size = os.path.getsize(tmp)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(store, name))
        return {"sha": sha, "file": name, "ext": ext, "cd": cd, "size": size}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def format_doc_removed(items):
    lines = [f"🗑 <b>Dokument{'e' if len(items) > 1 else ''} entfernt</b>"]
    lines += [f"• {e(it.get('name') or it['title'])}" + (f" ({e(it['folder'])})" if it.get("folder") else "")
              for it in items]
    return "\n".join(lines)


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
        s.setdefault("channels", [])
        s.setdefault("docs_token", "")
        self.d.setdefault("fails", 0)
        self.d.setdefault("absences", {})  # uid -> "all" | Minuten

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
        return self._do(method, rq, http_timeout)

    def upload(self, method, field, path, filename, http_timeout=180, **params):
        """multipart/form-data, Datei gestreamt (nicht komplett im Speicher)."""
        bd = secrets.token_hex(16)
        head = b"".join(f'--{bd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                        for k, v in params.items())
        fn = re.sub(r'["\r\n]', "'", filename)
        head += (f'--{bd}\r\nContent-Disposition: form-data; name="{field}"; filename="{fn}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{bd}--\r\n".encode()

        def body():
            yield head
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
            yield tail
        size = len(head) + os.path.getsize(path) + len(tail)
        rq = urllib.request.Request(self.url + method, data=body(), headers={
            "Content-Type": f"multipart/form-data; boundary={bd}", "Content-Length": str(size), "User-Agent": UA})
        return self._do(method, rq, http_timeout)

    def _do(self, method, rq, http_timeout):
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

    def send(self, chat, text, markup=None):
        """Sendet HTML-Text, teilt bei Bedarf an Absatzgrenzen (Limit 4096).
        Buttons (markup) hängen an der letzten Teilnachricht."""
        if not chat:
            return
        chunks, cur = [], ""
        for part in text.split("\n\n"):
            if cur and len(cur) + len(part) + 2 > 3900:
                chunks.append(cur); cur = ""
            cur = f"{cur}\n\n{part}" if cur else part
        chunks.append(cur)
        for i, c in enumerate(chunks):
            extra = {"reply_markup": markup} if markup and i == len(chunks) - 1 else {}
            for attempt in range(3):
                try:
                    self.call("sendMessage", chat_id=chat, text=c[:4096], parse_mode="HTML",
                              disable_web_page_preview=True, **extra)
                    break
                except Exception as ex:
                    log("Telegram-Sendefehler:", ex)
                    time.sleep(2 * (attempt + 1))

    def send_document(self, chat, caption, path=None, filename=None, file_id=None):
        """Schickt eine Datei (oder eine schon hochgeladene per file_id). Liefert die file_id zum Weiterverwenden."""
        opts = {"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML"}
        if file_id:
            res = self.call("sendDocument", document=file_id, **opts)
        else:
            res = self.upload("sendDocument", "document", path, filename, **opts)
        return ((res or {}).get("document") or {}).get("file_id")

    def edit(self, chat, msg_id, text, markup=None):
        """Ersetzt eine Nachricht (für Buttons). Zu lange Texte gehen als neue Nachricht raus."""
        if len(text) > 4096:
            return self.send(chat, text, markup)
        try:
            self.call("editMessageText", chat_id=chat, message_id=msg_id, text=text, parse_mode="HTML",
                      disable_web_page_preview=True, reply_markup=markup or {"inline_keyboard": []})
        except RuntimeError as ex:
            if "not modified" not in str(ex):
                raise


# ───────────────────────── Sync-Kern ─────────────────────────

class App:
    SEND_PAUSE = 1.0  # Sekunden zwischen Dateien (Telegram erlaubt ~20 Nachrichten/min pro Kanal)

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

    def channels(self):
        """Über /addchannel registrierte Kanäle, sonst Fallback auf TELEGRAM_CHANNEL_ID aus .env."""
        chans = self.st.settings.get("channels", [])
        if chans:
            return chans
        return [{"id": self.cfg.channel, "title": ""}] if self.cfg.channel else []

    def notify_channels(self, text):
        for c in self.channels():
            self.tg.send(c["id"], text)

    def sync(self, force=False, source="auto"):
        t0 = time.time()
        now = dt.datetime.now(TZ)
        run = {"time": t0, "source": source}
        docs = {"manual": source == "manual"}
        extra = (lambda req, dl: self.docs_fetch(docs, force, req, dl)) if self.cfg.docs else None
        try:
            new = parse_ics(fetch_trainex(self.cfg, extra=extra))
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
            run["docs"] = self.docs_apply(docs)
            return run
        recovered = self.st.d["fails"] > 0
        self.st.d["fails"] = 0
        self.st.d["events"] = events
        # Fehlzeiten zu entfallenen Terminen verwerfen
        uids = {x["uid"] for x in events}
        self.st.d["absences"] = {k: v for k, v in self.st.d["absences"].items() if k in uids}
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
                if self.channels():
                    self.notify_channels(text)
                else:
                    self.tg.send(self.cfg.admin, text)
        if recovered:
            self.notify_admin("✅ Sync funktioniert wieder.")
        run["docs"] = self.docs_apply(docs)
        return run

    # ── Unterlagen ──
    def docs_token(self):
        s = self.st.settings
        if not s.get("docs_token"):
            s["docs_token"] = secrets.token_urlsafe(24)
            self.st.save()
        return s["docs_token"]

    def docs_dir(self, tok=None):
        return os.path.join(self.cfg.web_dir, "d", tok or self.docs_token())

    def docs_url(self):
        return f"{self.cfg.public_url}/d/{self.docs_token()}"

    def docs_fetch(self, box, force, req, dl):
        """Läuft in der TraiNex-Sitzung direkt nach dem Stundenplan: Archiv-Liste holen, abgleichen und
        nur neue/geänderte Dateien laden. Fehler hier lassen den Stundenplan-Sync unberührt."""
        try:
            now = dt.datetime.now(TZ)
            old = self.st.d.get("docs", {})
            listing, label, page, form = fetch_archive(self.cfg, req, now.date(), old.get("form"))
            store = os.path.join(self.docs_dir(), "f")
            os.makedirs(store, exist_ok=True)
            for it in old.get("items", {}).values():
                if it.get("file") and not os.path.exists(os.path.join(store, it["file"])):
                    it["present"] = False  # z. B. Webordner geleert → still neu laden
            box["result"] = sync_docs(old, listing, label, now,
                                      lambda d, item: download_doc(dl, d, store, self.cfg.docs_max, page), force)
            box["result"][0]["form"] = form
        except Exception as ex:
            box["error"] = str(ex) if isinstance(ex, SyncError) else f"Interner Fehler: {ex!r}"

    def docs_apply(self, box):
        """Übernimmt das Ergebnis von docs_fetch, schreibt Übersicht/Feed und meldet Änderungen.
        Liefert eine Kurzinfo für /sync."""
        d = self.st.d
        if "error" in box:
            d["docs_fails"] = d.get("docs_fails", 0) + 1
            d["docs_error"] = box["error"]
            self.st.save()
            log("Unterlagen fehlgeschlagen:", box["error"])
            if d["docs_fails"] == 1 or box.get("manual"):
                self.notify_admin(f"⚠️ <b>Unterlagen-Abgleich fehlgeschlagen</b>\n{e(box['error'])}")
            return "Fehler"
        if "result" not in box:
            return ""
        state, entries, info = box["result"]
        recovered = d.get("docs_fails", 0) > 0
        d["docs_fails"] = 0
        d.pop("docs_error", None)
        d["docs"] = state
        if entries:
            d["docs_last"] = {"time": state["checked"],
                              "items": [[t, it.get("name") or it["title"], it.get("folder", "")]
                                        for t, it in entries][:15]}
        self.st.save()
        self.publish_docs()
        n = {t: sum(1 for x, _ in entries if x == t) for t in ("new", "upd", "del")}
        log(f"Unterlagen ok: {info['listed']} in der Liste, {n['new']} neu, {n['upd']} geändert, "
            f"{n['del']} entfernt" + (f", {info['failed']} Download(s) fehlgeschlagen" if info["failed"] else ""))
        if info["first"]:
            what = "Semesterwechsel – Unterlagen" if info["sem_changed"] else "Erstimport Unterlagen"
            self.notify_admin(f"📚 <b>{what}:</b> {len(state['items'])} Dokumente\n{e(state['semester'])}\n"
                              f'<a href="{e(self.docs_url())}/">Übersicht öffnen</a> · Details: /docs')
        elif entries:
            self.announce_docs(entries)
        if recovered:
            self.notify_admin("✅ Unterlagen-Abgleich funktioniert wieder.")
        parts = [f"{n['new']} neu" if n["new"] else "", f"{n['upd']} geändert" if n["upd"] else "",
                 f"{n['del']} entfernt" if n["del"] else ""]
        return ", ".join(x for x in parts if x) or ("Erstimport" if info["first"] else "keine Änderungen")

    def announce_docs(self, entries):
        """Neue/geänderte Dateien als Dokument in alle Kanäle (sonst an dich), Löschungen gesammelt als Text."""
        targets = [c["id"] for c in self.channels()] or [self.cfg.admin]
        store = os.path.join(self.docs_dir(), "f")
        for typ, it in entries:
            if typ not in ("new", "upd"):
                continue
            cap = format_doc_caption(typ, it)
            path = os.path.join(store, it["file"]) if it.get("file") else None
            fid = None
            for chat in targets:
                try:
                    if path and it.get("size", 0) <= TG_MAX_FILE:
                        fid = self.tg.send_document(chat, cap, path, it["name"], fid) or fid
                    else:
                        self.tg.send(chat, cap)
                except Exception as ex:
                    log("Telegram-Dokument:", ex)
                    self.tg.send(chat, cap)
                time.sleep(self.SEND_PAUSE)
        removed = [it for typ, it in entries if typ == "del"]
        if removed:
            for chat in targets:
                self.tg.send(chat, format_doc_removed(removed))

    def publish_docs(self):
        """Übersichtsseite + Feed für den Kurzbefehl schreiben, nicht mehr benötigte Dateien löschen."""
        docs = self.st.d.get("docs")
        if not docs:
            return
        base = self.docs_dir()
        store = os.path.join(base, "f")
        os.makedirs(store, exist_ok=True)
        write_atomic(os.path.join(base, "index.json"),
                     build_docs_json(docs, self.docs_url(), docs.get("checked", time.time())))
        write_atomic(os.path.join(base, "index.html"), build_docs_page(docs, time.time()))
        keep = {it["file"] for it in docs["items"].values() if it.get("file")}
        for f in os.listdir(store):
            if f not in keep and not f.startswith(".dl-"):
                try:
                    os.remove(os.path.join(store, f))
                except OSError:
                    pass

    def docs_text(self):
        if not self.cfg.docs:
            return "📚 Unterlagen-Abgleich ist aus (<code>DOCS=0</code> in der .env)."
        docs, d = self.st.d.get("docs"), self.st.d
        ft = lambda t: dt.datetime.fromtimestamp(t, TZ).strftime("%d.%m. %H:%M")
        if not docs:
            return ("📚 Noch keine Unterlagen abgeglichen."
                    + (f"\n⚠️ {e(d['docs_error'])}" if d.get("docs_error") else "\nDer nächste Sync holt sie (/sync)."))
        items = docs["items"].values()
        L = [f"📚 <b>Unterlagen</b> · {e(docs['semester'])}",
             f"{len(docs['items'])} Dokumente in {len({x['folder'] for x in items})} Ordnern · "
             f"geprüft {ft(docs['checked'])}"]
        if d.get("docs_error"):
            L.append(f"⚠️ Letzter Abgleich fehlgeschlagen: {e(d['docs_error'])}")
        L += ["", f'<a href="{e(self.docs_url())}/">Übersicht öffnen</a> (alle Dateien zum Herunterladen)',
              f"Feed für den Kurzbefehl (Dateien-App):\n<code>{e(self.docs_url())}/index.json</code>"]
        last = d.get("docs_last")
        if last:
            icon = {"new": "📄", "upd": "🔁", "del": "🗑"}
            L += ["", f"<b>Zuletzt geändert</b> ({ft(last['time'])}):"]
            L += [f"{icon.get(t, '•')} {e(n)}" + (f" · {e(f)}" if f else "") for t, n, f in last["items"]]
        L += ["", "Die Links sind privat – nicht weitergeben. Neue Links: /docs newurl"]
        return "\n".join(L)

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
        docs = d.get("docs")
        if not self.cfg.docs:
            L.append("Unterlagen: aus")
        elif docs:
            L.append(f"Unterlagen: {len(docs['items'])} Dokumente · {e(docs['semester'].split(':')[0])}"
                     + (" · ⚠️ Fehler" if d.get("docs_error") else "") + " (/docs)")
        st = self.access_stats()
        L.append(f"Abo-Abrufe 24 h: {st[0]} von {st[1]} Geräten/IPs" if st else "Abo-Abrufe 24 h: n/a")
        chans = self.st.settings.get("channels", [])
        L.append("Kanäle: " + (", ".join(e(c["title"] or str(c["id"])) for c in chans) if chans
                                else (f"– (Fallback aus .env: <code>{self.cfg.channel}</code>)" if self.cfg.channel
                                      else "keiner registriert – /addchannel im Kanal posten")))
        up_s = int(time.time() - self.started)
        L += ["", f"{self.abo_link()}: {e(self.page_url())}",
              f"Laufzeit: {up_s // 86400} d {up_s % 86400 // 3600} h {up_s % 3600 // 60} min · v{VERSION}"]
        if lc and lc.get("preview"):
            L += ["", "<b>Zuletzt geändert:</b>", "\n\n".join(lc["preview"])]
        return "\n".join(L)

    # ── Module & Fehlzeiten ──
    def modules(self):
        return module_stats(self.st.d["events"], self.st.d["absences"], dt.datetime.now(TZ),
                            self.cfg.absence_limit)

    def modules_view(self, show_all=False):
        mods = self.modules()
        return format_modules(mods, self.cfg.absence_limit, show_all), modules_keyboard(mods, show_all)

    def module_view(self, m, note=""):
        ab, now = self.st.d["absences"], dt.datetime.now(TZ)
        text, first = format_module(m, self.cfg.absence_limit, ab, now, note)
        return text, module_keyboard(m, ab, now, first)

    def pick_module(self, q):
        """Liefert (Modul, None) oder (None, Fehlermeldung)."""
        hits = find_module(self.modules(), q)
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, f"Kein Modul zu „{e(q)}“ gefunden. Übersicht: /modules"
        return None, "Mehrere Treffer – bitte genauer:\n" + "\n".join(f"• {e(m['name'])}" for m in hits)

    def cmd_absent(self, args):
        """/absent <Modul> <Nr> [Minuten] – ohne Minuten: ganzer Termin, 0: Eintrag löschen."""
        nums = []
        while args and args[-1].isdigit() and len(nums) < 2:
            nums.insert(0, int(args.pop()))
        if not args or not nums:
            self.notify_admin("Nutzung: <code>/absent &lt;Modul&gt; &lt;Nr&gt; [Minuten]</code>\n"
                              "z. B. <code>/absent M11 3</code> (ganzer Termin) oder "
                              "<code>/absent M11 3 30</code> (30 min). 0 Minuten löscht den Eintrag.\n"
                              "Die Nummern stehen in <code>/modules M11</code>.")
            return
        m, err = self.pick_module(" ".join(args))
        if err:
            self.notify_admin(err)
            return
        nr, mins = nums[0], (nums[1] if len(nums) > 1 else None)
        if not 1 <= nr <= m["n"]:
            self.notify_admin(f"{e(m['name'])} hat die Termine 1–{m['n']}.")
            return
        ev, ab = m["events"][nr - 1], self.st.d["absences"]
        what = f"Nr. {nr} ({kind_of(m, ev)['short']})"
        if mins == 0:
            ab.pop(ev["uid"], None)
            note = f"🗑 {what}: Fehlzeit gelöscht."
        elif mins is None or mins >= duration_min(ev):
            ab[ev["uid"]] = "all"
            note = f"✏️ {what}: ganzer Termin ({duration_min(ev)} min) eingetragen."
        else:
            ab[ev["uid"]] = mins
            note = f"✏️ {what}: {mins} min eingetragen."
        self.st.save()
        m, _ = self.pick_module(m["name"])
        g = kind_of(m, ev)
        note += f"\n{budget_icon(g)} {e(g['label'])}: {rest_text(g)}"
        self.tg.send(self.cfg.admin, *self.module_view(m, note))

    def handle_callback(self, cq):
        """Buttons unter /modules: o/oa = Übersicht, m:<Modul> = Details, t:<Modul>:<Termin> = Fehlzeit umschalten."""
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        toast = ""
        if data in ("o", "oa"):
            text, kb = self.modules_view(show_all=(data == "oa"))
        else:
            kind, _, rest = data.partition(":")
            key, _, uk = rest.partition(":")
            m = next((x for x in self.modules() if x["key"] == key), None)
            if not m:
                self.tg.call("answerCallbackQuery", callback_query_id=cq["id"],
                             text="Modul nicht mehr im Plan – /modules")
                return
            if kind == "t":
                ev = next((x for x in m["events"] if uid_key(x) == uk), None)
                if ev:
                    ab = self.st.d["absences"]
                    if ab.pop(ev["uid"], None) is not None:
                        toast = "Fehlzeit entfernt"
                    else:
                        ab[ev["uid"]] = "all"
                        toast = ("Als gefehlt markiert" if to_dt(ev["s"]) <= dt.datetime.now(TZ)
                                 else "Fehlen geplant")
                    self.st.save()
                    m = next(x for x in self.modules() if x["key"] == key)
                    g = kind_of(m, ev)
                    toast += f" · {g['label']}: {rest_text(g)}"
            text, kb = self.module_view(m)
        self.tg.edit((msg.get("chat") or {}).get("id", self.cfg.admin), msg.get("message_id"), text, kb)
        self.tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=toast)

    # ── Befehle ──
    HELP = ("🤖 <b>Befehle</b>\n"
            "/sync – jetzt abgleichen (/sync force: Löschschutz übergehen)\n"
            "/start – automatischen Sync einschalten\n"
            "/stop – automatischen Sync ausschalten\n"
            "/settime &lt;min&gt; – Intervall in Minuten setzen\n"
            "/status – Status anzeigen\n"
            "/url – Abo-URL anzeigen\n"
            "/newurl – neue geheime Abo-URL erzeugen (alte wird ungültig)\n"
            "/channels – registrierte Kanäle anzeigen\n"
            "/modules – Module mit Terminen, Minuten und Fehlzeit-Budget je Veranstaltungsart\n"
            "/modules &lt;Modul&gt; – alle Termine eines Moduls (z. B. /modules M11)\n"
            "/modules alle – auch abgeschlossene Module\n"
            "/absent &lt;Modul&gt; &lt;Nr&gt; [min] – Fehlzeit eintragen (0 = löschen)\n"
            "/docs – Unterlagen aus dem TraiNex-Archiv: Übersicht und Feed für die Dateien-App\n"
            "/docs newurl – neue private Unterlagen-Links erzeugen\n"
            "/help – diese Hilfe\n\n"
            "📡 <b>Kanal hinzufügen/entfernen</b>\n"
            "Bot als Admin in den Kanal holen, dann direkt im Kanal <code>/addchannel</code> posten "
            "(bzw. <code>/removechannel</code> zum Entfernen). Kein .env-Eintrag mehr nötig.")

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
                                  + (" (im Kanal gepostet)" if r.get("changes") and self.channels() else "")
                                  + (f"\n📚 Unterlagen: {e(r['docs'])}" if r.get("docs") else ""))
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
        elif cmd == "/channels":
            chans = self.st.settings.get("channels", [])
            if chans:
                lines = [f"• {e(c['title'] or str(c['id']))} (<code>{c['id']}</code>)" for c in chans]
                self.notify_admin("📡 <b>Registrierte Kanäle</b>\n" + "\n".join(lines))
            elif self.cfg.channel:
                self.notify_admin(f"Kein Kanal über Telegram registriert. Fallback aus .env: "
                                  f"<code>{e(self.cfg.channel)}</code>")
            else:
                self.notify_admin("Kein Kanal registriert. Bot als Admin in einen Kanal holen und dort "
                                  "/addchannel posten.")
        elif cmd == "/modules":
            q = " ".join(parts[1:])
            if q and q.lower() not in ("alle", "all"):
                m, err = self.pick_module(q)
                if err:
                    self.notify_admin(err)
                else:
                    self.tg.send(self.cfg.admin, *self.module_view(m))
            else:
                self.tg.send(self.cfg.admin, *self.modules_view(show_all=bool(q)))
        elif cmd == "/absent":
            self.cmd_absent(parts[1:])
        elif cmd == "/docs":
            if arg.lower() == "newurl":
                old = self.docs_dir()
                s["docs_token"] = secrets.token_urlsafe(24); self.st.save()
                if os.path.isdir(old):
                    os.replace(old, self.docs_dir())
                self.publish_docs()
                self.notify_admin("🔑 Neue Unterlagen-Links, die alten sind ab sofort ungültig. "
                                  "Im Kurzbefehl die Feed-Adresse ersetzen:\n"
                                  f"<code>{e(self.docs_url())}/index.json</code>")
            else:
                self.notify_admin(self.docs_text())
        elif cmd == "/help":
            self.notify_admin(self.HELP)
        else:
            self.notify_admin("Unbekannter Befehl. /help")

    def handle_channel(self, m):
        """Verarbeitet /addchannel und /removechannel, die direkt im Kanal gepostet werden."""
        txt = (m.get("text") or "").strip()
        if not txt.startswith("/"):
            return
        cmd = txt.split()[0].split("@")[0].lower()
        if cmd not in ("/addchannel", "/removechannel"):
            return
        chat = m.get("chat") or {}
        cid = chat.get("id")
        title = chat.get("title") or chat.get("username") or str(cid)
        if cid is None:
            return
        chans = self.st.settings.setdefault("channels", [])
        if cmd == "/addchannel":
            if any(c["id"] == cid for c in chans):
                self.tg.send(cid, "ℹ️ Dieser Kanal ist bereits registriert.")
                return
            chans.append({"id": cid, "title": title})
            self.st.save()
            self.tg.send(cid, "✅ Dieser Kanal ist jetzt für Änderungsmeldungen registriert.")
            self.notify_admin(f"➕ Kanal hinzugefügt: {e(title)} (<code>{cid}</code>)")
        else:
            before = len(chans)
            chans[:] = [c for c in chans if c["id"] != cid]
            if len(chans) == before:
                self.tg.send(cid, "ℹ️ Dieser Kanal war nicht registriert.")
                return
            self.st.save()
            self.tg.send(cid, "✅ Dieser Kanal wurde entfernt.")
            self.notify_admin(f"➖ Kanal entfernt: {e(title)} (<code>{cid}</code>)")

    def run(self):
        c = self.cfg
        c.require("user", "pw", "bot", "admin", "ics_token")
        os.makedirs(c.web_dir, exist_ok=True)
        if self.st.d["events"]:
            self.publish()
        if self.cfg.docs:
            self.publish_docs()
        last = (self.st.d.get("last_run") or {}).get("time", 0)
        self.next_run = max(time.time() + 5, last + self.st.settings["interval"] * 60)
        # Startmeldung vor allen Telegram-Aufrufen: der Deploy wartet nur wenige Sekunden auf diese Zeile,
        # und Telegram antwortet vom Server aus manchmal erst nach dem Timeout.
        log(f"trainex-sync {VERSION} gestartet. Auto={self.st.settings['auto']}, "
            f"Intervall={self.st.settings['interval']} min")
        try:
            self.tg.call("setMyCommands", http_timeout=10, commands=[
                {"command": k, "description": v} for k, v in [
                    ("sync", "Jetzt abgleichen"), ("status", "Status anzeigen"),
                    ("start", "Auto-Sync einschalten"), ("stop", "Auto-Sync ausschalten"),
                    ("settime", "Intervall in Minuten setzen"), ("url", "Abo-URL"),
                    ("newurl", "Neue Abo-URL erzeugen"), ("channels", "Registrierte Kanäle anzeigen"),
                    ("modules", "Module & Fehlzeiten"), ("absent", "Fehlzeit eintragen"),
                    ("docs", "Unterlagen aus dem Archiv"),
                    ("help", "Hilfe")]],
                scope={"type": "chat", "chat_id": int(c.admin)})
        except Exception as ex:
            log("setMyCommands:", ex)
        if self.st.d.get("version") != VERSION:
            prev = self.st.d.get("version")
            self.st.d["version"] = VERSION
            self.st.save()
            if prev:
                self.notify_admin(f"🚀 trainex-sync aktualisiert: v{e(prev)} → <b>v{VERSION}</b>")
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
                params = {"timeout": wait, "allowed_updates": ["message", "channel_post", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                updates = self.tg.call("getUpdates", http_timeout=wait + 15, **params)
            except Exception as ex:
                log("getUpdates:", ex)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                cq = u.get("callback_query")
                if cq:
                    if str((cq.get("from") or {}).get("id")) != c.admin:
                        log("Button von fremdem Nutzer ignoriert:", (cq.get("from") or {}).get("id"))
                        continue
                    try:
                        self.handle_callback(cq)
                    except Exception as ex:
                        log("Fehler bei Button:", repr(ex))
                    continue
                cp = u.get("channel_post")
                if cp:
                    try:
                        self.handle_channel(cp)
                    except Exception as ex:
                        log("Fehler bei Kanal-Befehl:", repr(ex))
                    continue
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

def check_docs(cfg, req, out, dump=None):
    """Probelauf Unterlagen: nur die Liste, keine Downloads. Mit dump werden die Seiten gespeichert."""
    def rec(step, url, data=None, extra=None):
        r = req(step, url, data, extra)
        out["last"] = r
        if dump:
            os.makedirs(dump, exist_ok=True)
            fn = os.path.join(dump, re.sub(r"\W+", "_", step).strip("_") + ".html")
            with open(fn, "wb") as f:
                f.write(r[2])
            out.setdefault("files", []).append(fn)
        return r
    try:
        out["listing"], out["label"], _, _ = fetch_archive(cfg, rec, dt.date.today())
    except Exception as ex:  # Probelauf: Fehler anzeigen, Stundenplan-Check trotzdem ausgeben
        out["error"] = str(ex) if isinstance(ex, SyncError) else f"Interner Fehler: {ex!r}"


def print_docs(cfg, out):
    print("\n── Unterlagen (Lernen → Archiv)")
    for fn in out.get("files", []):
        print(f"   gespeichert: {fn}")
    if out.get("files"):
        print("   (Die Seiten enthalten deinen Namen/Kurs – vor dem Weitergeben ggf. kürzen.)")
    if out.get("last"):
        page = _page(out["last"])
        sel = re.search(r"(?is)<select\b[^>]*sem.*?</select>", page)
        if sel:
            print("   Semesterauswahl (HTML):", norm(page[max(0, sel.start() - 300):sel.end() + 200])[:900])
        hits = list(re.finditer(r"(?i)datei_laden\.cfm", page)) or list(re.finditer(r"(?i)download", page))
        for m in hits[:2]:
            print("   Umfeld eines Dokument-Links (HTML):", norm(page[max(0, m.start() - 900):m.start() + 300]))
    if "error" in out:
        print(f"   Fehler: {out['error']}")
        return
    listing = out["listing"]
    print(f"   Semester: {out['label']}\n   {len(listing)} Dokumente in der Liste")
    for d in listing[:60]:
        print(f"   • [{d['folder'] or '–'}] {d['title']}  ·  {d['info'][:60]}  ·  {d['key'][:80]}")
    if len(listing) > 60:
        print(f"   … und {len(listing) - 60} weitere")
    old = State(cfg).d.get("docs", {})
    if not old.get("items"):
        print("   Noch kein gespeicherter Stand – beim ersten Lauf werden alle geladen (ohne Meldung im Kanal).")
        return
    _, entries, info = sync_docs(old, listing, out["label"], dt.datetime.now(TZ), force=True)
    if info["sem_changed"]:
        print("   Neues Semester – der nächste Lauf importiert die Liste neu (ohne Meldung im Kanal).")
        return
    label = {"new": "neu", "upd?": "evtl. geändert", "del": "entfernt"}
    print(f"   {len(entries)} Änderung(en) gegenüber gespeichertem Stand:")
    for t, it in entries:
        print(f"   {label.get(t, t)}: {it['title']} ({it['folder'] or '–'})")


def cmd_check(cfg, file=None, debug=False, dump=None):
    if file:
        with open(file, encoding="utf-8") as f:
            text = f.read()
    else:
        cfg.require("user", "pw")
        t0 = time.time()
        docs = {}
        text = fetch_trainex(cfg, debug, extra=(lambda req, dl: check_docs(cfg, req, docs, dump))
                             if cfg.docs else None)
        print(f"\nLogin + Export ok ({time.time() - t0:.1f}s)")
        if cfg.docs:
            print_docs(cfg, docs)
            print()
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
                "channel": "(optional in TELEGRAM_CHANNEL_ID; einfacher: /addchannel direkt im Kanal posten)"
                }.get(typ, "")
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
    chan_hint = f"Ist @{me['username']} Admin im Kanal? Die Kanal-ID beginnt mit -100."
    chans = State(cfg).settings.get("channels", [])
    if chans:
        for c in chans:
            targets.append((f"Kanal „{c['title'] or c['id']}“", "state.json", c["id"], chan_hint))
    elif cfg.channel:
        targets.append(("Kanal", "TELEGRAM_CHANNEL_ID", cfg.channel,
                        chan_hint + " (sudo ./install.sh discover, vorher etwas im Kanal posten)."))
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
            dump = argv[argv.index("--dump") + 1] if "--dump" in argv else None
            cmd_check(cfg, f, debug="--debug" in argv, dump=dump)
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
