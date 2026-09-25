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


if __name__ == "__main__":
    unittest.main()
