"""Tests für trainex_sync – nur Standardbibliothek, synthetische Daten (keine echten Stundenpläne im Repo).

Ausführen:  python3 -m unittest discover -s tests -v
"""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import trainex_sync as T  # noqa: E402

ADDR = "HMU Health and Medical University - Campus Düsseldorf/Krefeld // Kaistraße 16-16a // 40221 Düsseldorf"


def vevent(start, end, module, kind, topic, room, lead="Dr. Muster"):
    summ = f"{module} - {kind}/{topic} - {room}  -  t25_hmu21"
    desc = (f"{module} - {kind}/{topic} - {room} - Grp. Vorklinik DKR_Med WS 25-II  ab {start[9:11]}:{start[11:13]}"
            f"  Uhr - Aktuelle Termine immer im TraiNex prüfen. (Leiter/-in: {lead})")
    return (f"BEGIN:VEVENT\nSUMMARY:{summ}\nDESCRIPTION:{desc}\nDTSTART:{start}\nDTEND:{end}\n"
            f"CATEGORIES:TraiNex\nLOCATION:{room} - {ADDR}\nEND:VEVENT\n")


def base_events():
    ev = []
    day = dt.date(2030, 10, 7)  # Montag, weit in der Zukunft
    rooms = ["ZH 003 (Hörsaal)", "R0.0 (Hörsaal)", "ZH 207.3 (Biochemie 1)"]
    kinds = [("M11 Physiologie", "Vorlesung"), ("M12 Biochemie/ Molekularbiologie", "Praktikum"),
             ("M10 Anatomie", "Seminar mit klin. Bezug"), ("M05 Medizinische Psychologie und Soziologie",
                                                         "integrierte Seminare")]
    for i in range(20):
        d = day + dt.timedelta(days=i)
        m, k = kinds[i % 4]
        ev.append(dict(s=f"{d:%Y%m%d}T094500", e=f"{d:%Y%m%d}T111500", m=m, k=k,
                       t=f"Thema {i}", r=rooms[i % 3], l="Dr. Muster"))
    return ev


def to_ics(evs):
    return "BEGIN:VCALENDAR\n" + "".join(
        vevent(x["s"], x["e"], x["m"], x["k"], x["t"], x["r"], x["l"]) for x in evs) + "END:VCALENDAR\n"


NOW = dt.datetime(2030, 9, 1, 12, tzinfo=T.TZ)


class Parsing(unittest.TestCase):
    def test_parse_and_fields(self):
        evs = T.parse_ics(to_ics(base_events()))
        self.assertEqual(len(evs), 20)
        e = evs[0]
        self.assertEqual(T.room(e), "ZH 003 (Hörsaal)")
        self.assertEqual(T.lecturer(e), "Dr. Muster")
        self.assertEqual(T.short(e), "M11 Physiologie - VL - Thema 0")

    def test_short_titles(self):
        evs = T.parse_ics(to_ics(base_events()))
        self.assertEqual(T.short(evs[1]), "M12 Biochemie/Molekularbiologie - P - Thema 1")
        self.assertEqual(T.short(evs[2]), "M10 Anatomie - KS - Thema 2")
        self.assertEqual(T.short(evs[3]), "M05 Medizinische Psychologie und Soziologie - IS - Thema 3")
        self.assertEqual(T.kind_short("Tutorium"), "T")

    def test_folded_and_escaped_input(self):
        text = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:M11 Physiologie - Vorlesung/Blut\\, Gerinnung\r\n  I - R0.0 (H\r\n örsaal)  -  t25_hmu21\r\nDTSTART:20301001T080000\r\nDTEND:20301001T093000\r\nLOCATION:R0.0 (Hörsaal) - " + ADDR + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        e = T.parse_ics(text)[0]
        self.assertEqual(T.short(e), "M11 Physiologie - VL - Blut, Gerinnung I")

    def test_place(self):
        e = T.parse_ics(to_ics(base_events()))[0]
        p = T.place(e)
        self.assertEqual(p["street"], "Kaistraße 16-16a")
        self.assertEqual(p["city"], "40221 Düsseldorf")
        self.assertEqual(p["org"], "HMU Health and Medical University")


class Diff(unittest.TestCase):
    def setUp(self):
        self.old_raw = base_events()
        state = {}
        events, entries, info = T.compute(state, T.parse_ics(to_ics(self.old_raw)), NOW)
        self.assertTrue(info["first"])
        self.assertEqual(entries, [])
        self.state = {"events": events}

    def run_new(self, mutate, force=False):
        new = [dict(x) for x in self.old_raw]
        mutate(new)
        return T.compute(self.state, T.parse_ics(to_ics(new)), NOW, force)

    def labels(self, entries):
        out = []
        for _, typ, _, ch in entries:
            out += [typ] if typ != "chg" else [c[1] for c in ch]
        return out

    def test_no_change(self):
        _, entries, _ = self.run_new(lambda n: None)
        self.assertEqual(entries, [])

    def test_time_room_lecturer(self):
        def m(n):
            n[0]["s"], n[0]["e"] = n[0]["s"][:9] + "134500", n[0]["e"][:9] + "151500"
            n[1]["r"] = "ZH 104.1 (Seminarraum)"
            n[2]["l"] = "Prof. Neu"
        events, entries, _ = self.run_new(m)
        self.assertEqual(sorted(self.labels(entries)), ["Dozent geändert", "Raum geändert", "Zeit geändert"])
        # UIDs bleiben stabil, SEQUENCE steigt
        old = {x["uid"] for x in self.state["events"]}
        self.assertEqual({x["uid"] for x in events}, old)
        self.assertEqual(max(x["seq"] for x in events), 1)

    def test_moved_new_removed(self):
        def m(n):
            n[5]["s"] = "20301015T094500"; n[5]["e"] = "20301015T111500"   # verschoben
            del n[8]                                                           # entfällt
            n.append(dict(n[0], s="20301201T080000", e="20301201T093000", t="Neu"))  # neu
        _, entries, _ = self.run_new(m)
        self.assertEqual(sorted(self.labels(entries)), ["Termin verschoben", "del", "new"])

    def test_title_change(self):
        def m(n):
            n[3]["t"] = "Anderes Thema"
        _, entries, _ = self.run_new(m)
        self.assertEqual(self.labels(entries), ["Titel geändert"])

    def test_guard(self):
        with self.assertRaises(T.SyncError):
            self.run_new(lambda n: n.__delitem__(slice(0, 10)))
        _, entries, _ = self.run_new(lambda n: n.__delitem__(slice(0, 10)), force=True)
        self.assertEqual(len(entries), 10)

    def test_empty_export(self):
        with self.assertRaises(T.SyncError):
            T.compute(self.state, [], NOW)

    def test_past_events_kept(self):
        later = dt.datetime(2030, 10, 20, 12, tzinfo=T.TZ)
        new = [x for x in self.old_raw if x["s"] >= "20301020"]
        events, entries, _ = T.compute(self.state, T.parse_ics(to_ics(new)), later)
        self.assertEqual(entries, [])
        self.assertEqual(len(events), 20)

    def test_format_entries(self):
        def m(n):
            n[1]["r"] = "ZH 104.1 (Seminarraum)"
        _, entries, _ = self.run_new(m)
        text = "\n".join(T.format_entries(entries))
        self.assertIn("🚪 Raum geändert", text)
        self.assertIn("M12 Biochemie/Molekularbiologie - P - Thema 1", text)


class Output(unittest.TestCase):
    def setUp(self):
        os.environ["CAMPUS_GEO"] = ""
        self.cfg = T.Cfg()
        events, _, _ = T.compute({}, T.parse_ics(to_ics(base_events())), NOW)
        self.events = events

    def test_ics_structure(self):
        geo = {"Kaistraße 16-16a, 40221 Düsseldorf": [51.2, 6.75]}
        raw = T.build_ics(self.cfg, self.events, geo).decode()
        self.assertTrue(raw.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertEqual(raw.count("BEGIN:VEVENT"), 20)
        self.assertIn("GEO:51.2;6.75", raw)
        self.assertIn("X-APPLE-STRUCTURED-LOCATION", raw)
        self.assertIn("TZID=Europe/Berlin", raw)
        self.assertLessEqual(max(len(line.encode()) for line in raw.split("\r\n")), 75)

    def test_ics_without_geo(self):
        raw = T.build_ics(self.cfg, self.events, {}).decode()
        self.assertNotIn("GEO:", raw)
        self.assertIn("LOCATION:ZH 003 (Hörsaal) · HMU\\nKaistraße 16-16a\\, 40221 Düsseldorf", raw)

    def test_ics_roundtrip(self):
        raw = T.build_ics(self.cfg, self.events, {}).decode()
        back = T.parse_ics(raw)
        self.assertEqual(len(back), 20)
        self.assertEqual(back[0]["s"], self.events[0]["s"])

    def test_page(self):
        page = T.build_page(self.cfg, "https://example.org/trainex/abcdefghijklmnop.ics").decode()
        self.assertIn('href="webcal://example.org/trainex/abcdefghijklmnop.ics"', page)

    def test_write_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.ics")
            self.assertTrue(T.write_atomic(p, b"a"))
            self.assertFalse(T.write_atomic(p, b"a"))  # unverändert -> kein Schreiben (ETag bleibt)
            self.assertTrue(T.write_atomic(p, b"b"))


class Misc(unittest.TestCase):
    def test_env_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".env") as f:
            f.write("# Kommentar\nTRAINEX_PASS='a$b#c'\nexport CAL_NAME=\"X Y\"\n")
        T.load_env_file(f.name)
        os.unlink(f.name)
        self.assertEqual(os.environ["TRAINEX_PASS"], "a$b#c")
        self.assertEqual(os.environ["CAL_NAME"], "X Y")

    def test_quiet_hours(self):
        os.environ["QUIET_HOURS"] = "22-6"
        app = T.App.__new__(T.App)
        app.cfg = T.Cfg()
        at = lambda h: dt.datetime(2030, 10, 1, h, tzinfo=T.TZ).timestamp()
        self.assertTrue(app.in_quiet(at(23)))
        self.assertTrue(app.in_quiet(at(3)))
        self.assertFalse(app.in_quiet(at(6)))
        self.assertFalse(app.in_quiet(at(12)))


class FakeTG:
    def __init__(self):
        self.sent = []  # (chat_id, text)
        self.markups = []
        self.calls = []

    def send(self, chat, text, markup=None):
        self.sent.append((chat, text))
        self.markups.append(markup)

    def edit(self, chat, msg_id, text, markup=None):
        self.send(chat, text, markup)

    def call(self, method, **params):
        self.calls.append((method, params))


class Channels(unittest.TestCase):
    def make_app(self):
        os.environ["STATE_DIRECTORY"] = tempfile.mkdtemp()
        os.environ["TELEGRAM_ADMIN_ID"] = "111"
        app = T.App.__new__(T.App)
        app.cfg = T.Cfg()
        app.st = T.State(app.cfg)
        app.tg = FakeTG()
        return app

    def test_addchannel_registers_and_confirms(self):
        app = self.make_app()
        app.handle_channel({"text": "/addchannel", "chat": {"id": -100123, "title": "HMU Plan"}})
        self.assertEqual(app.st.settings["channels"], [{"id": -100123, "title": "HMU Plan"}])
        chats = [c for c, _ in app.tg.sent]
        self.assertIn(-100123, chats)   # Bestätigung im Kanal
        self.assertIn("111", [str(c) for c in chats])  # Info an Admin

    def test_addchannel_twice_is_noop(self):
        app = self.make_app()
        msg = {"text": "/addchannel", "chat": {"id": -100123, "title": "HMU Plan"}}
        app.handle_channel(msg)
        app.handle_channel(msg)
        self.assertEqual(len(app.st.settings["channels"]), 1)

    def test_removechannel(self):
        app = self.make_app()
        app.st.settings["channels"] = [{"id": -100123, "title": "HMU Plan"}]
        app.handle_channel({"text": "/removechannel", "chat": {"id": -100123, "title": "HMU Plan"}})
        self.assertEqual(app.st.settings["channels"], [])

    def test_removechannel_unknown_channel_is_noop(self):
        app = self.make_app()
        app.handle_channel({"text": "/removechannel", "chat": {"id": -100999, "title": "Anderer"}})
        self.assertEqual(app.st.settings["channels"], [])

    def test_channels_helper_falls_back_to_env(self):
        os.environ["TELEGRAM_CHANNEL_ID"] = "-100555"
        app = self.make_app()
        self.assertEqual(app.channels(), [{"id": "-100555", "title": ""}])
        del os.environ["TELEGRAM_CHANNEL_ID"]


class Modules(unittest.TestCase):
    """/modules: Termine, Minuten und Fehlzeit-Budget pro Modul."""

    def setUp(self):
        raw = base_events()
        # zusätzlich ein langes M11-Praktikum (240 min) und ein ganztägiger Eintrag (zählt nicht)
        raw.append(dict(raw[0], s="20301030T080000", e="20301030T120000", k="Praktikum", t="Lang"))
        ics = to_ics(raw).replace("END:VCALENDAR", "BEGIN:VEVENT\nSUMMARY:M11 Physiologie - Klausur\n"
                                  "DTSTART:20301101\nDTEND:20301102\nEND:VEVENT\nEND:VCALENDAR")
        self.events, _, _ = T.compute({}, T.parse_ics(ics), NOW)
        self.now = dt.datetime(2030, 10, 15, 12, tzinfo=T.TZ)   # M11: 07./11./15.10. vorbei
        self.m11 = [x for x in self.events if x["sum"].startswith("M11") and len(x["s"]) > 8]

    def mod(self, absences=None, pct=20):
        mods = T.module_stats(self.events, absences or {}, self.now, pct)
        return {m["name"]: m for m in mods}

    def test_totals_and_limit(self):
        m = self.mod()["M11 Physiologie"]
        self.assertEqual((m["n"], m["total"]), (6, 5 * 90 + 240))   # ganztägiger Eintrag zählt nicht
        self.assertEqual(m["limit"], 138)                          # 20 % von 690
        self.assertEqual((m["past"], m["up"], m["up_min"]), (3, 3, 420))
        self.assertEqual(m["kinds"], {"VL": (5, 450), "P": (1, 240)})
        self.assertIn("M12 Biochemie/Molekularbiologie", self.mod())

    def test_absences_full_partial_planned(self):
        ab = {self.m11[0]["uid"]: "all", self.m11[1]["uid"]: 30, self.m11[4]["uid"]: "all",
              self.m11[2]["uid"]: 500}                              # mehr als Termindauer -> gedeckelt
        m = self.mod(ab)["M11 Physiologie"]
        self.assertEqual(m["missed"], 90 + 30 + 90)
        self.assertEqual(m["planned"], 90)
        self.assertEqual(m["rest"], 138 - 300)
        self.assertEqual(m["skippable"], 0)
        self.assertEqual(T.budget_icon(m), "🔴")
        self.assertIn("überschritten um 162 min", T.budget_line(m))

    def test_skippable_shortest_first(self):
        m = self.mod(pct=50)["M11 Physiologie"]                  # Budget 345 min
        self.assertEqual(m["skippable"], 2)                        # 90 + 90, nicht das 240-min-Praktikum
        self.assertEqual(self.mod(pct=0)["M11 Physiologie"]["skippable"], 0)

    def test_find_module(self):
        mods = T.module_stats(self.events, {}, self.now, 20)
        self.assertEqual([m["name"] for m in T.find_module(mods, "m11")], ["M11 Physiologie"])
        self.assertEqual([m["name"] for m in T.find_module(mods, "anat")], ["M10 Anatomie"])
        self.assertEqual(len(T.find_module(mods, "M1")), 3)
        self.assertEqual(T.find_module(mods, "Chemie-Physik"), [])

    def test_overview_format(self):
        text = T.format_modules(list(self.mod().values()), 20)
        self.assertIn("<b>M11 Physiologie</b>\n→ Insgesamt 6 Termine - 690 min\n"
                      "→ Maximale Fehlzeit (20%): 138 min", text)
        later = dt.datetime(2031, 1, 1, tzinfo=T.TZ)
        mods = T.module_stats(self.events, {}, later, 20)
        self.assertIn("4 abgeschlossene(s) Modul(e) ausgeblendet", T.format_modules(mods, 20))
        self.assertIn("→ Abgeschlossen", T.format_modules(mods, 20, show_all=True))

    def test_detail_and_keyboard(self):
        ab = {self.m11[0]["uid"]: "all", self.m11[1]["uid"]: 30, self.m11[4]["uid"]: "all"}
        m = self.mod(ab)["M11 Physiologie"]
        text, first = T.format_module(m, 20, ab, self.now)
        self.assertEqual(first, 0)
        self.assertIn("❌ Mo 07.10. 09:45–11:15 · 90 min · VL - Thema 0 · <b>gefehlt</b>", text)
        self.assertIn("🟠 Fr 11.10.", text)
        self.assertIn("−30 min", text)
        self.assertIn("💤 Mi 23.10.", text)
        self.assertIn("fehlen geplant", text)
        kb = T.module_keyboard(m, ab, self.now, first)["inline_keyboard"]
        btns = [b for row in kb[:-1] for b in row]
        self.assertEqual([b["text"] for b in btns], ["❌1", "🟠2", "3", "4", "💤5", "6"])
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in btns))

    def test_detail_trimmed_when_too_long(self):
        m = self.mod()["M11 Physiologie"]
        text, first = T.format_module(m, 20, {}, self.now, max_len=600)
        self.assertGreater(first, 0)
        self.assertIn(f"… {first} frühere Termine ausgeblendet", text)
        kb = T.module_keyboard(m, {}, self.now, first)["inline_keyboard"]
        self.assertEqual(sum(len(r) for r in kb[:-1]), 6 - first)


class ModuleCommands(unittest.TestCase):
    def setUp(self):
        os.environ["STATE_DIRECTORY"] = tempfile.mkdtemp()
        os.environ["TELEGRAM_ADMIN_ID"] = "111"
        os.environ["ABSENCE_LIMIT"] = "20"
        self.app = T.App.__new__(T.App)
        self.app.cfg = T.Cfg()
        self.app.st = T.State(self.app.cfg)
        self.app.tg = FakeTG()
        # Termine in der Zukunft relativ zu „jetzt“, damit alles „kommend“ ist
        self.app.st.d["events"], _, _ = T.compute({}, T.parse_ics(to_ics(base_events())), NOW)

    def last(self):
        return self.app.tg.sent[-1][1], self.app.tg.markups[-1]

    def test_modules_overview(self):
        self.app.handle("/modules")
        text, kb = self.last()
        self.assertIn("→ Insgesamt 5 Termine - 450 min", text)
        self.assertIn("→ Maximale Fehlzeit (20%): 90 min", text)
        self.assertEqual(len(kb["inline_keyboard"]), 4)

    def test_modules_detail_and_unknown(self):
        self.app.handle("/modules M11")
        self.assertIn("📘 <b>M11 Physiologie</b>", self.last()[0])
        self.app.handle("/modules M1")
        self.assertIn("Mehrere Treffer", self.last()[0])
        self.app.handle("/modules Chirurgie")
        self.assertIn("Kein Modul", self.last()[0])

    def test_absent_command(self):
        ab = self.app.st.d["absences"]
        self.app.handle("/absent M11 2 30")
        uid2 = [x for x in self.app.st.d["events"] if x["sum"].startswith("M11")][1]["uid"]
        self.assertEqual(ab, {uid2: 30})
        self.assertIn("−30 min geplant", self.last()[0])
        self.app.handle("/absent M11 2")
        self.assertEqual(ab, {uid2: "all"})
        self.app.handle("/absent M11 2 0")
        self.assertEqual(ab, {})
        self.app.handle("/absent M11 99")
        self.assertIn("1–5", self.last()[0])
        self.app.handle("/absent M11")
        self.assertIn("Nutzung", self.last()[0])
        # gespeichert
        self.app.handle("/absent Medizinische Psychologie 1")
        self.assertEqual(len(T.State(self.app.cfg).d["absences"]), 1)

    def test_buttons_toggle(self):
        self.app.handle("/modules")
        key = self.last()[1]["inline_keyboard"][2][0]["callback_data"]       # M11
        self.app.handle_callback({"id": "q1", "data": key, "message": {"chat": {"id": 111}, "message_id": 5}})
        text, kb = self.last()
        self.assertIn("M11 Physiologie", text)
        toggle = kb["inline_keyboard"][0][0]["callback_data"]
        self.app.handle_callback({"id": "q2", "data": toggle, "message": {"chat": {"id": 111}, "message_id": 5}})
        self.assertEqual(list(self.app.st.d["absences"].values()), ["all"])
        self.assertEqual(self.app.tg.calls[-1][1]["text"], "Fehlen geplant")
        self.assertEqual(self.last()[1]["inline_keyboard"][0][0]["text"], "💤1")
        self.app.handle_callback({"id": "q3", "data": toggle, "message": {"chat": {"id": 111}, "message_id": 5}})
        self.assertEqual(self.app.st.d["absences"], {})
        self.app.handle_callback({"id": "q4", "data": "o", "message": {"chat": {"id": 111}, "message_id": 5}})
        self.assertIn("Module & Fehlzeiten", self.last()[0])
        self.app.handle_callback({"id": "q5", "data": "m:deadbeef", "message": {}})
        self.assertIn("nicht mehr", self.app.tg.calls[-1][1]["text"])


if __name__ == "__main__":
    unittest.main()
