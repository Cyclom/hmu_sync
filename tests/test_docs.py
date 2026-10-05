"""Tests für den Unterlagen-Abgleich (Lernen → Archiv) – synthetische Seiten, lokaler Fake-TraiNex.

Ausführen:  python3 -m unittest discover -s tests -v
"""
import datetime as dt
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import trainex_sync as T  # noqa: E402
from test_trainex_sync import FakeTG, base_events, to_ics  # noqa: E402

TODAY = dt.date(2026, 10, 5)
NOW = dt.datetime(2026, 10, 5, 12, tzinfo=T.TZ)

FORM = """<form name="filter" action="archiv_liste.cfm?TokCF19=0T0123&amp;kurs=7" method="post">
<input type="radio" name="zeitraum" value="alle" checked> Alle Semester<br>
<input type="radio" name="zeitraum" value="sem"> Nur Semester
<select name="semester">
<option value="2">2. Semester: 01.04.2026 bis 30.09.2026
<option value="3">3. Semester: 01.10.2026 bis 31.03.2027
<option value="4">4. Semester: 01.04.2027 bis 30.09.2027
</select>
<select name="sort"><option value="name">Name<option value="datum" selected>Datum</select>
<input type="hidden" name="kurs" value="77">
<input type="checkbox" name="nur_neu" value="1">
<input type="submit" name="anzeigen" value="anzeigen">
</form>"""

LIST = """<table>
<tr><th>Datei</th><th>Datum</th><th>Größe</th></tr>
<tr><td colspan=3><b>M11 Physiologie</b></td></tr>
<tr><td><a href="download.cfm?TokCF19=0T0123&amp;datei_id=501" target=_blank>Skript Niere</a></td>
    <td>02.10.2026</td><td>1,2 MB</td></tr>
<tr><td><a href="javascript:void(0)" onclick="window.open('download.cfm?datei_id=502&amp;TokCF19=0T0999')">
    <img src="pdf.gif"></a> VL3_Herz.pdf</td><td>03.10.2026</td></tr>
<tr><td colspan=3>M12 Biochemie</td></tr>
<tr><td><a href="/hmu24/upload/archiv/Enzyme.pptx">Enzyme</a> <a href="/hmu24/upload/archiv/Enzyme.pptx">Download</a></td>
    <td>01.10.2026</td></tr>
<tr><td><a href="archiv_liste.cfm?sort=name">sortieren</a></td></tr>
</table>"""


class Parse(unittest.TestCase):
    def test_listing(self):
        docs = T.parse_archive(FORM + LIST, "https://x.de/hmu24/cfm/archiv/archiv_liste.cfm?TokCF19=1")
        self.assertEqual([(d["folder"], d["title"]) for d in docs],
                         [("M11 Physiologie", "Skript Niere"), ("M11 Physiologie", "VL3_Herz.pdf"),
                          ("M12 Biochemie", "Enzyme")])
        self.assertEqual(docs[0]["key"], "/hmu24/cfm/archiv/download.cfm?datei_id=501")  # ohne Tokens
        self.assertEqual(docs[0]["info"], "02.10.2026 1,2 MB")
        self.assertTrue(docs[1]["url"].startswith("https://x.de/hmu24/cfm/archiv/download.cfm?datei_id=502"))

    def test_canon_and_tok(self):
        self.assertEqual(T.canon_url("/a/b.cfm?TokCF19=0T01&IDphp17=3P1&sec18m=7S&123456&id=5"), "/a/b.cfm?id=5")
        self.assertIn("TokCF19=", T.with_tok("https://x.de/a.cfm?TokCF19=old&id=5"))
        self.assertNotIn("old", T.with_tok("https://x.de/a.cfm?TokCF19=old&id=5"))

    def test_names(self):
        self.assertEqual(T.safe_name('a/b:c?.pdf'), "a-b-c-.pdf")
        self.assertEqual(T.display_name("Skript Niere", "pdf", None), "Skript Niere.pdf")
        self.assertEqual(T.display_name("VL3_Herz.pdf", "pdf", None), "VL3_Herz.pdf")
        self.assertEqual(T.display_name("Download", "pdf", "orig.pdf"), "orig.pdf")


class Form(unittest.TestCase):
    URL = "https://x.de/hmu24/cfm/archiv/archiv.cfm?TokCF19=1"

    def test_picks_current_semester_and_nur(self):
        method, url, data, label, preset = T.semester_form(FORM.encode(), self.URL, TODAY)
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://x.de/hmu24/cfm/archiv/archiv_liste.cfm?TokCF19=0T0123&kurs=7")
        self.assertEqual(label, "3. Semester: 01.10.2026 bis 31.03.2027")
        self.assertEqual(urllib.parse.parse_qs(data.decode()),
                         {"semester": ["3"], "sort": ["datum"], "kurs": ["77"], "zeitraum": ["sem"],
                          "anzeigen": ["anzeigen"]})
        self.assertFalse(preset)

    def test_by_number_and_missing(self):
        self.assertTrue(T.semester_form(FORM, self.URL, TODAY, "4")[3].startswith("4. Semester"))
        self.assertIsNone(T.semester_form(FORM, self.URL, dt.date(2030, 1, 1)))
        self.assertIsNone(T.semester_form("<form><select name=a><option>x</select></form>", self.URL, TODAY))

    def test_get_form_preset(self):
        f = FORM.replace('method="post"', "").replace('value="3">', 'value="3" selected>') \
                .replace('value="alle" checked', 'value="alle"').replace('value="sem">', 'value="sem" checked>')
        method, url, data, _, preset = T.semester_form(f, self.URL, TODAY)
        self.assertEqual(method, "GET")
        self.assertIsNone(data)
        self.assertIn("archiv_liste.cfm?semester=3", url)
        self.assertTrue(preset)


def listing(*specs):
    return [{"key": f"/dl.cfm?id={i}", "url": f"https://x/dl.cfm?id={i}", "title": t, "folder": f, "info": inf}
            for i, t, f, inf in specs]


class FakeGrab:
    def __init__(self):
        self.calls = []
        self.content = {}

    def __call__(self, d, item):
        self.calls.append(d["key"])
        c = self.content.get(d["key"], d["key"])
        if c == "BIG":
            return {"big": 80 * 1048576}
        if c == "HTML":
            return {"html": True}
        if c == "ERR":
            raise T.SyncError("HTTP 500")
        return {"sha": "s-" + c, "file": f"{abs(hash(c))}.pdf", "ext": "pdf", "cd": None, "size": 1000}


class Diff(unittest.TestCase):
    def setUp(self):
        self.g = FakeGrab()
        self.l1 = listing((1, "Skript", "M11", "02.10."), (2, "Folien", "M11", "03.10."), (3, "Enzyme", "M12", ""))
        self.st, entries, info = T.sync_docs({}, self.l1, "3. Sem", NOW, self.g)
        self.assertTrue(info["first"])
        self.assertEqual(entries, [])  # Erstimport meldet nichts
        self.assertEqual(len(self.st["items"]), 3)

    def run2(self, lst, **kw):
        self.g.calls.clear()
        return T.sync_docs(self.st, lst, kw.pop("label", "3. Sem"), NOW + dt.timedelta(hours=1), self.g, **kw)

    def test_unchanged_downloads_nothing(self):
        st, entries, _ = self.run2(self.l1)
        self.assertEqual((entries, self.g.calls), ([], []))
        self.assertEqual(st["items"], self.st["items"])

    def test_new_updated_removed(self):
        l2 = listing((1, "Skript", "M11", "05.10."), (2, "Folien", "M11", "03.10."), (4, "Neu", "M12", ""))
        self.g.content["/dl.cfm?id=1"] = "v2"
        st, entries, _ = self.run2(l2)
        self.assertEqual([(t, it["title"]) for t, it in entries], [("upd", "Skript"), ("new", "Neu"),
                                                                    ("del", "Enzyme")])
        self.assertEqual(sorted(self.g.calls), ["/dl.cfm?id=1", "/dl.cfm?id=4"])
        self.assertEqual(entries[1][1]["name"], "Neu.pdf")

    def test_same_content_is_silent(self):
        st, entries, _ = self.run2(listing((1, "Skript", "M11", "anders"), (2, "Folien", "M11", "03.10."),
                                           (3, "Enzyme", "M12", "")))
        self.assertEqual(entries, [])
        self.assertEqual(self.g.calls, ["/dl.cfm?id=1"])
        self.assertEqual([x["info"] for x in st["items"].values()][0], "anders")

    def test_reupload_with_new_link_is_update(self):
        self.g.content["/dl.cfm?id=9"] = "neu"
        st, entries, _ = self.run2(listing((9, "Skript", "M11", "06.10."), (2, "Folien", "M11", "03.10."),
                                           (3, "Enzyme", "M12", "")))
        self.assertEqual([(t, it["title"]) for t, it in entries], [("upd", "Skript")])
        self.assertEqual(len(st["items"]), 3)

    def test_failed_download_retried_next_time(self):
        self.g.content["/dl.cfm?id=4"] = "ERR"
        lst = self.l1 + listing((4, "Neu", "M12", ""))
        st, entries, info = self.run2(lst)
        self.assertEqual((entries, info["failed"], len(st["items"])), ([], 1, 3))
        self.st = st
        del self.g.content["/dl.cfm?id=4"]
        _, entries, _ = self.run2(lst)
        self.assertEqual([t for t, _ in entries], ["new"])

    def test_html_link_is_skipped(self):
        self.g.content["/dl.cfm?id=4"] = "HTML"
        lst = self.l1 + listing((4, "Ordner", "M12", ""))
        self.st, entries, _ = self.run2(lst)
        self.assertEqual(self.st["skip"], ["/dl.cfm?id=4"])
        _, entries, _ = self.run2(lst)
        self.assertEqual((entries, self.g.calls), ([], []))

    def test_too_big(self):
        self.g.content["/dl.cfm?id=4"] = "BIG"
        _, entries, _ = self.run2(self.l1 + listing((4, "Video.mp4", "M12", "")))
        it = entries[0][1]
        self.assertEqual((it["big"], it["file"]), (80 * 1048576, None))
        self.assertIn("Zu groß für Telegram (80.0 MB)", T.format_doc_caption("new", it))

    def test_guards(self):
        with self.assertRaises(T.SyncError):
            self.run2([])
        self.st["items"].update({f"x{i}": dict(key=f"/x{i}", title="x", folder="", info="") for i in range(5)})
        with self.assertRaises(T.SyncError):
            self.run2(self.l1)
        _, entries, _ = self.run2(self.l1, force=True)
        self.assertEqual(len(entries), 5)

    def test_semester_change_is_silent_reimport(self):
        st, entries, info = self.run2(listing((7, "Neu", "M20", "")), label="4. Sem")
        self.assertEqual((entries, info["sem_changed"], len(st["items"])), ([], True, 1))

    def test_dry_run(self):
        _, entries, _ = T.sync_docs(self.st, listing((1, "Skript", "M11", "x"), (8, "Neu", "", "")), "3. Sem", NOW)
        self.assertEqual([t for t, _ in entries], ["upd?", "new", "del", "del"])


class Output(unittest.TestCase):
    def test_paths_json_page(self):
        items = {"a": dict(id="a", title="Skript", name="Skript.pdf", folder="M11 Physiologie", file="aa.pdf",
                           size=2048, added=1, changed=1),
                 "b": dict(id="b", title="Skript", name="Skript.pdf", folder="M11 Physiologie", file="bb.pdf",
                           size=10, added=2, changed=2),
                 "c": dict(id="c", title="Film", name="Film.mp4", folder="", file=None, big=99, added=3, changed=3)}
        self.assertEqual(T.doc_paths(items), {"a": "M11 Physiologie/Skript.pdf", "b": "M11 Physiologie/Skript (2).pdf",
                                              "c": "Allgemein/Film.mp4"})
        data = json.loads(T.build_docs_json({"semester": "3. Sem", "items": items}, "https://h/d/tok", 5))
        self.assertEqual([f["path"] for f in data["files"]], ["M11 Physiologie/Skript (2).pdf",
                                                              "M11 Physiologie/Skript.pdf"])
        self.assertEqual(data["files"][1]["url"], "https://h/d/tok/f/aa.pdf")
        self.assertEqual(data["updated"], 5)
        page = T.build_docs_page({"semester": "3. Sem", "items": items}, 100).decode()
        self.assertIn('href="f/aa.pdf" download="Skript.pdf"', page)
        self.assertIn("zu groß, nur im TraiNex", page)


class DocTG(FakeTG):
    def __init__(self):
        super().__init__()
        self.docs = []

    def send_document(self, chat, caption, path=None, filename=None, file_id=None):
        self.docs.append((chat, caption, path, filename, file_id))
        return "FID"


class Announce(unittest.TestCase):
    def setUp(self):
        T.App.SEND_PAUSE = 0
        os.environ["STATE_DIRECTORY"] = tempfile.mkdtemp()
        os.environ["WEB_DIR"] = tempfile.mkdtemp()
        os.environ["TELEGRAM_ADMIN_ID"] = "111"
        self.app = T.App.__new__(T.App)
        self.app.cfg = T.Cfg()
        self.app.st = T.State(self.app.cfg)
        self.app.tg = DocTG()

    def entries(self):
        new = dict(id="a", title="Skript", name="Skript.pdf", folder="M11", file="aa.pdf", size=10)
        big = dict(id="b", title="Film", name="Film.mp4", folder="M11", file="bb.mp4", size=60 * 1048576)
        gone = dict(id="c", title="Alt", name="Alt.pdf", folder="M12")
        return [("new", new), ("upd", big), ("del", gone)]

    def test_to_channels_with_file_reuse(self):
        self.app.st.settings["channels"] = [{"id": -1, "title": "A"}, {"id": -2, "title": "B"}]
        self.app.announce_docs(self.entries())
        d = self.app.tg.docs
        self.assertEqual([(x[0], x[3], x[4]) for x in d], [(-1, "Skript.pdf", None), (-2, "Skript.pdf", "FID")])
        self.assertIn("📄 <b>Neues Dokument</b> · M11\nSkript.pdf", d[0][1])
        texts = self.app.tg.sent
        self.assertIn("🔁 <b>Dokument aktualisiert</b> · M11\nFilm.mp4\n⚠️ Zu groß", texts[0][1])
        self.assertEqual([c for c, t in texts if "entfernt" in t], [-1, -2])
        self.assertIn("• Alt.pdf (M12)", texts[-1][1])

    def test_without_channel_to_admin(self):
        self.app.announce_docs(self.entries()[:1])
        self.assertEqual(self.app.tg.docs[0][0], "111")

    def test_apply_first_import_and_error(self):
        st, _, info = T.sync_docs({}, listing((1, "Skript", "M11", "")), "3. Sem", NOW, FakeGrab())
        self.assertEqual(self.app.docs_apply({"result": (st, [], info)}), "Erstimport")
        self.assertIn("Erstimport Unterlagen:</b> 1 Dokumente", self.app.tg.sent[-1][1])
        self.assertTrue(os.path.exists(os.path.join(self.app.docs_dir(), "index.json")))
        self.assertEqual(self.app.docs_apply({"error": "kaputt"}), "Fehler")
        self.app.docs_apply({"error": "kaputt"})
        self.assertEqual(sum("Unterlagen-Abgleich fehlgeschlagen" in t for _, t in self.app.tg.sent), 1)
        self.app.handle("/docs")
        self.assertIn("⚠️ Letzter Abgleich fehlgeschlagen: kaputt", self.app.tg.sent[-1][1])
        self.app.docs_apply({"result": (st, [], dict(info, first=False))})
        self.assertIn("funktioniert wieder", self.app.tg.sent[-1][1])

    def test_docs_newurl_moves_files(self):
        st, _, info = T.sync_docs({}, listing((1, "Skript", "M11", "")), "3. Sem", NOW, FakeGrab())
        self.app.docs_apply({"result": (st, [], info)})
        old = self.app.docs_dir()
        self.app.handle("/docs newurl")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(os.path.join(self.app.docs_dir(), "index.html")))
        self.assertIn("Neue Unterlagen-Links", self.app.tg.sent[-1][1])


# ───────── Ende-zu-Ende gegen einen nachgebauten TraiNex ─────────

class FakeTrainex(http.server.BaseHTTPRequestHandler):
    files = {"501": b"%PDF-1.4 Niere v1", "502": b"%PDF-1.4 Herz"}
    log = []

    def log_message(self, *a):
        pass

    def reply(self, body, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = urllib.parse.parse_qs(self.rfile.read(n).decode())
        path = urllib.parse.urlsplit(self.path).path
        self.log.append(("POST", path, data))
        if path.endswith("archiv_liste.cfm"):
            ok = data.get("zeitraum") == ["sem"] and data.get("semester") == ["3"]
            return self.reply((LIST if ok else "<p>Alle Semester</p>").encode())
        self.reply(b"<p>Willkommen</p>")

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        self.log.append(("GET", u.path, q))
        p = u.path
        if p.endswith("student_layout.cfm") and q.get("subarea") == ["studienplan"]:
            return self.reply(b'<frame src="../cfm/einsatzplan/einsatzplan_stundenplan.cfm?TokCF19=1">')
        if p.endswith("einsatzplan_stundenplan.cfm"):
            return self.reply(b'<a href="einsatzplan_listenansicht_kt.cfm?TokCF19=1">Liste</a>')
        if p.endswith("einsatzplan_listenansicht_kt.cfm"):
            return self.reply(b'<a href="einsatzplan_listenansicht_iCal.cfm?TokCF19=1&ics=1">iCal</a>')
        if p.endswith("einsatzplan_listenansicht_iCal.cfm"):
            return self.reply(to_ics(base_events()).encode(), "text/calendar")
        if p.endswith("student_layout.cfm") and q.get("subarea") == ["archiv"]:
            return self.reply(b'<frameset><frame src="menu.cfm"><frame src="../cfm/archiv/archiv.cfm?TokCF19=1">'
                              b'</frameset>')
        if p.endswith("archiv.cfm"):
            return self.reply(FORM.encode())
        if p.endswith("download.cfm"):
            i = q["datei_id"][0]
            return self.reply(self.files[i], "application/pdf",
                              {"Content-Disposition": f'attachment; filename="datei{i}.pdf"'})
        if p.endswith("Enzyme.pptx"):
            return self.reply(b"PK pptx", "application/octet-stream")
        self.reply(b"<p>ok</p>")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTrainex)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        FakeTrainex.log.clear()
        T.App.SEND_PAUSE = 0
        for k, v in {"STATE_DIRECTORY": tempfile.mkdtemp(), "WEB_DIR": tempfile.mkdtemp(),
                     "TELEGRAM_ADMIN_ID": "111", "TRAINEX_USER": "u", "TRAINEX_PASS": "p",
                     "TRAINEX_BASE": f"http://127.0.0.1:{self.srv.server_port}/hmu24", "DOCS_SEMESTER": "3",
                     "QUIET_HOURS": "", "CAMPUS_GEO": "51.2,6.7"}.items():
            os.environ[k] = v
        self.app = T.App.__new__(T.App)
        self.app.cfg = T.Cfg()
        self.app.st = T.State(self.app.cfg)
        self.app.tg = DocTG()
        self.app.st.settings["channels"] = [{"id": -5, "title": "Kanal"}]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for k in ("TRAINEX_BASE", "DOCS_SEMESTER", "CAMPUS_GEO"):
            os.environ.pop(k, None)

    def test_one_session_and_only_changed_downloads(self):
        run = self.app.sync(source="manual")
        self.assertTrue(run["ok"], run)
        self.assertEqual(run["docs"], "Erstimport")
        logins = [x for x in FakeTrainex.log if x[1].endswith("start.cfm")]
        self.assertEqual(len(logins), 1)
        docs = self.app.st.d["docs"]
        self.assertEqual(sorted(x["name"] for x in docs["items"].values()),
                         ["Enzyme.pptx", "Skript Niere.pdf", "VL3_Herz.pdf"])
        store = os.path.join(self.app.docs_dir(), "f")
        self.assertEqual(len(os.listdir(store)), 3)
        with open(os.path.join(self.app.docs_dir(), "index.json"), encoding="utf-8") as f:
            feed = json.load(f)
        self.assertEqual(feed["files"][0]["path"], "M11 Physiologie/Skript Niere.pdf")
        self.assertEqual(self.app.tg.docs, [])  # Erstimport: nichts im Kanal

        # zweiter Lauf: unverändert → keine Downloads
        FakeTrainex.log.clear()
        self.app.sync()
        self.assertFalse([x for x in FakeTrainex.log if "download" in x[1] or x[1].endswith(".pptx")])
        self.assertEqual(self.app.tg.docs, [])

        # neue Fassung von 501 (anderes Datum in der Liste) → genau ein Download, Meldung im Kanal
        global LIST
        old_list = LIST
        try:
            LIST = LIST.replace("02.10.2026", "06.10.2026")
            FakeTrainex.files["501"] = b"%PDF-1.4 Niere v2"
            FakeTrainex.log.clear()
            run = self.app.sync(source="manual")
        finally:
            LIST = old_list
            FakeTrainex.files["501"] = b"%PDF-1.4 Niere v1"
        self.assertEqual(run["docs"], "1 geändert")
        self.assertEqual(len([x for x in FakeTrainex.log if "download" in x[1]]), 1)
        self.assertEqual([(c, f) for c, _, _, f, _ in self.app.tg.docs], [(-5, "Skript Niere.pdf")])
        self.assertEqual(len(os.listdir(store)), 3)  # alte Fassung gelöscht

    def test_docs_error_keeps_calendar(self):
        os.environ["DOCS_SEMESTER"] = "9"
        self.app.cfg = T.Cfg()
        run = self.app.sync(source="manual")
        self.assertTrue(run["ok"])
        self.assertEqual(run["docs"], "Fehler")
        self.assertEqual(len(self.app.st.d["events"]), 20)
        self.assertIn("Semester-Auswahl im Archiv nicht gefunden (DOCS_SEMESTER=9)", self.app.tg.sent[-1][1])


class Upload(unittest.TestCase):
    def test_multipart_streaming(self):
        got = {}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                got["body"] = self.rfile.read(n)
                got["ctype"] = self.headers["Content-Type"]
                body = json.dumps({"ok": True, "result": {"document": {"file_id": "F1"}}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            tg = T.TG("x")
            tg.url = f"http://127.0.0.1:{srv.server_port}/"
            fd, path = tempfile.mkstemp()
            os.write(fd, b"%PDF" + b"x" * 200000)
            os.close(fd)
            self.assertEqual(tg.send_document(-1, "<b>Neu</b>", path, 'Skript "Ä".pdf'), "F1")
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertIn(b'name="chat_id"\r\n\r\n-1\r\n', got["body"])
        self.assertIn('filename="Skript \'Ä\'.pdf"'.encode(), got["body"])
        self.assertIn(b"%PDF" + b"x" * 200000 + b"\r\n--", got["body"])
        self.assertTrue(got["ctype"].startswith("multipart/form-data; boundary="))


if __name__ == "__main__":
    unittest.main()
