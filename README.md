# trainex-sync – Anleitung

Der Dienst holt den Studienplan regelmäßig aus TraiNex und stellt ihn als iCal-Abo unter
`https://tillianbo.com/trainex/<geheim>.ics` bereit. Im selben Durchlauf gleicht er die Unterlagen aus
**Lernen → Archiv** (aktuelles Semester) ab. Änderungen an beidem meldet er in einem Telegram-Kanal, neue
Dokumente kommen dort direkt als Datei an. Steuern kannst nur du ihn, per Telegram.

```
trainex_sync.py              der Dienst (nur Python-Standardbibliothek)
.env.example                 Vorlage für Zugangsdaten (die echte .env liegt nur auf dem Server)
trainex-sync.service         systemd-Unit (abgesichert, eigener Nutzer „trainex“)
trainex.conf                 nginx-Snippet
install.sh                   Erstinstallation + Hilfsbefehle
deploy/setup-deploy.sh       einmalig: Deploy-Zugang für GitHub einrichten
deploy/trainex-deploy        spielt Releases auf dem Server ein (mit Prüfung + Rollback)
.github/workflows/deploy.yml Pipeline: Tests → Deploy bei Push auf main
tests/                       Unit-Tests (synthetische Daten)
```

## 1. Telegram vorbereiten (ca. 5 Minuten)

1. **Bot anlegen:** In Telegram **@BotFather** öffnen, `/newbot` senden und Name und Username vergeben,
   z. B. `HMU Stundenplan` / `hmu_stundenplan_bot`. Den **Token** (`123456:ABC…`) brauchst du gleich.
2. **Kanal anlegen:** In Telegram einen neuen Kanal erstellen, z. B. „HMU Stundenplan WS25-II“.
   Wenn er privat ist, treten Kollegen über einen Einladungslink bei.
3. **Bot zum Admin machen:** Kanal → Verwalten → Administratoren → Bot hinzufügen.
   Die Berechtigung „Nachrichten senden“ reicht.
4. **IDs sichtbar machen:** Schreib dem Bot im privaten Chat irgendeine Nachricht (z. B. `hallo`).
   Danach findet `discover` deine Admin-ID (Schritt 2.4). Den Kanal selbst musst du nicht mehr per
   `.env`/`discover` eintragen – das geht direkt in Telegram, siehe Abschnitt 4.

## 2. Installation auf dem vServer

```bash
# Ordner auf den Server kopieren, z. B.:
scp -r trainex-sync cyclom@tillianbo.com:~
ssh cyclom@tillianbo.com
cd ~/trainex-sync
chmod +x install.sh

sudo ./install.sh                      # 2.1 installieren
sudo nano /opt/trainex-sync/.env       # 2.2 TRAINEX_USER, TRAINEX_PASS, TELEGRAM_BOT_TOKEN eintragen
sudo ./install.sh check                # 2.3 TraiNex-Abruf testen
sudo ./install.sh discover             # 2.4 Admin-ID anzeigen
sudo nano /opt/trainex-sync/.env       # 2.5 TELEGRAM_ADMIN_ID eintragen
sudo ./install.sh testmsg              # 2.6 Testnachricht an dich (+ Kanal, falls schon hinzugefügt)
```

Den Kanal fürs Änderungs-Feed richtest du nach dem Start direkt in Telegram ein (Abschnitt 4, `/addchannel`).

Wenn `check` die Meldung *„Login + Export ok … 145 Termine“* zeigt, funktioniert der Abruf.
Meldet es **Cloudflare blockiert**, lässt TraiNex Anfragen von deinem Server nicht durch. Dann bitte
nicht weiter installieren, sondern melde dich bei mir.

### nginx

In der bestehenden Konfiguration von tillianbo.com (meist `/etc/nginx/sites-available/…`) fügst du im
`server { … }`-Block mit `listen 443 ssl` **eine Zeile** ein:

```nginx
    include /etc/nginx/snippets/trainex.conf;
```

Danach prüfen und neu laden:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### Starten

```bash
sudo ./install.sh start
```

Der erste Lauf erfolgt nach etwa 5 Sekunden. Er importiert alle Termine und schickt dir die Abo-URL.
Den Log siehst du mit `journalctl -u trainex-sync -f`.

## 3. Kalender abonnieren

Schick dem Bot `/url`. Du bekommst einen Link wie `https://tillianbo.com/trainex/<geheim>`. Er führt zu einer
kleinen Abo-Seite mit diesen Buttons:

- **In Apple Kalender abonnieren**: öffnet auf iPhone und Mac direkt den Dialog „Kalenderabo hinzufügen“ (per `webcal://`)
- **Google Kalender** und **Outlook**: fügen das Abo dort hinzu
- **Link kopieren**: für alle anderen Kalender-Apps

Diesen Link gibst du an deine Kollegen weiter, am besten als angepinnte Nachricht im Kanal.
Jede Änderungsmeldung im Kanal enthält ihn ebenfalls.

**Wie schnell kommen Änderungen an?** Der Server ist nach jedem Sync sofort aktuell. Wann die Kalender-App
nachlädt, entscheidet die App selbst:

| Gerät | Aktualisierung |
|---|---|
| iPhone | nach Einstellung unter Einstellungen → Kalender → Accounts → **Datenabgleich** → „Abruf: Alle 15 Minuten“ (ab iOS 18 unter Apps → Kalender) |
| Mac | Kalender → Rechtsklick auf das Abo → Informationen → „Automatisch aktualisieren: Alle 5 Minuten“ |
| Google Kalender | alle 8–24 Stunden (lässt sich nicht einstellen) |
| Outlook | meist alle paar Stunden |

Für sofortige Hinweise gibt es die Telegram-Nachricht im Kanal.

**Ort und Karte:** Jeder Termin hat als Ort z. B. „ZH 003 (Hörsaal) · HMU“ mit der Adresse darunter.
Dazu kommen Koordinaten, sodass iOS eine Karte mit Pin anzeigt und Wegbeschreibung/„Zeit zum Aufbruch“
funktioniert. Die Koordinaten ermittelt der Dienst einmalig über OpenStreetMap. Liegt der Pin daneben,
trag die genauen Koordinaten als `CAMPUS_GEO=51.2…,6.7…` in die `.env` ein. In Apple Karten: Pin setzen →
nach oben wischen → Koordinaten kopieren.

## 4. Telegram-Befehle (nur von deiner ID)

| Befehl | Funktion |
|---|---|
| `/sync` | sofort abgleichen |
| `/sync force` | abgleichen und den Löschschutz übergehen (z. B. beim Semesterwechsel) |
| `/start` / `/stop` | automatischen Sync ein- oder ausschalten |
| `/settime 60` | Intervall in Minuten (15–10080) |
| `/status` | Auto-Sync, Intervall, nächster/letzter Lauf, letzte Änderungen, Terminzahl, nächster Termin, Abo-Abrufe der letzten 24 h, registrierte Kanäle |
| `/url` | Abo-URL anzeigen |
| `/newurl` | neue geheime URL erzeugen; die alte ist sofort ungültig und alle müssen neu abonnieren |
| `/channels` | registrierte Kanäle anzeigen |
| `/modules` | Module mit Terminzahl, Minuten und Fehlzeit-Budget je Veranstaltungsart (siehe unten) |
| `/modules M11` | alle Termine eines Moduls, mit Buttons zum Eintragen von Fehlzeiten |
| `/modules alle` | auch Module ohne kommende Termine |
| `/absent M11 3 [min]` | Fehlzeit für Termin Nr. 3 eintragen: ohne Minuten der ganze Termin, `0` löscht |
| `/docs` | Unterlagen: Semester, Anzahl, letzte Änderungen, Link zur Übersicht und Feed für den Kurzbefehl (Abschnitt 5a) |
| `/docs newurl` | neue private Unterlagen-Links; danach die Feed-Adresse im Kurzbefehl ersetzen |
| `/help` | Hilfe |

Einstellungen bleiben bei einem Neustart erhalten. In der Ruhezeit (`QUIET_HOURS`, standardmäßig
22–6 Uhr) laufen keine automatischen Syncs, `/sync` funktioniert aber immer.

### Module und Fehlzeiten (`/modules`)

`/modules` fasst alle gespeicherten Termine pro Modul zusammen. Die Anwesenheit wird **je
Veranstaltungsart** erfasst: Vorlesung, Seminar, Praktikum usw. eines Moduls haben jeweils ein eigenes
Budget von 20 % ihrer Minuten. Ein verpasstes Praktikum geht also nicht vom Vorlesungs-Budget ab.
Termine können unterschiedlich lang sein, gezählt wird deshalb in Minuten:

```
🟡 M11 Physiologie
→ Insgesamt 13 Termine - 1620 min
Vorlesung (VL): 10 Termine - 900 min
→ Maximale Fehlzeit (20%): 180 min
→ 🟢 Gefehlt 90 min · übrig 90 min
→ Kommend: 6 Termine (540 min) · noch 1 davon verpassbar
Praktikum (P): 3 Termine - 720 min
→ Maximale Fehlzeit (20%): 144 min
→ 🟡 Gefehlt 0 min · übrig 144 min
→ Kommend: 2 Termine (480 min) · kein weiterer Termin verpassbar
```

- **Gefehlt / geplant / übrig:** Fehlzeiten trägst du selbst ein. Bei begonnenen Terminen zählen sie als
  „gefehlt“, bei kommenden als „geplant“. Beide gehen vom Budget der jeweiligen Veranstaltungsart ab.
- **verpassbar:** So viele der kommenden Termine dieser Art kannst du noch ganz auslassen, ohne ihre
  Grenze zu reißen. Gerechnet wird mit den kürzesten Terminen zuerst.
- **Ampel:** je Art 🟢 mehr als die Hälfte des Budgets ist frei · 🟡 weniger als die Hälfte frei oder kein
  weiterer Termin mehr verpassbar · 🔴 Grenze überschritten. Vor dem Modulnamen und auf seinem Button
  steht die schlechteste Ampel seiner Arten.
- Tippst du auf ein Modul, siehst du alle seine Termine, nummeriert und mit Dauer, Art und Status
  (✅ da · ❌ gefehlt · 🟠 teilweise · 💤 geplant · ▫️ kommend) sowie das Budget jeder Art (VL, P, S …).
  Ein Tipp auf eine Nummer markiert den ganzen Termin, ein zweiter Tipp nimmt die Markierung zurück;
  die Rückmeldung nennt, was danach in dieser Art noch übrig ist.
  Für Teil-Fehlzeiten, z. B. 30 min zu spät, schickst du `/absent M11 3 30`.
- Module ohne kommende Termine werden ausgeblendet, mit `/modules alle` siehst du sie trotzdem.
  Ganztägige Einträge zählen nicht mit.
- Die Grenze stellst du mit `ABSENCE_LIMIT` in der `.env` ein (Standard `20`, in Prozent je
  Veranstaltungsart).
  Fehlzeiten bleiben gespeichert, wenn ein Termin verlegt wird. Entfällt ein Termin, wird auch seine
  Fehlzeit gelöscht.

### Kanal direkt in Telegram hinzufügen/entfernen

Kein `.env`-Eintrag mehr nötig: Bot als Admin in einen Kanal holen (Nachrichten senden reicht) und dort
**direkt im Kanal** `/addchannel` posten. Der Bot bestätigt im Kanal und meldet sich zusätzlich bei dir
privat. `/removechannel` im selben Kanal nimmt ihn wieder raus. Es können mehrere Kanäle gleichzeitig
registriert sein – Änderungsmeldungen gehen dann an alle. `TELEGRAM_CHANNEL_ID` in der `.env` funktioniert
weiterhin als Fallback, wird aber ignoriert, sobald mindestens ein Kanal über `/addchannel` registriert ist.

## 5. Was im Kanal gemeldet wird

➕ neuer Termin · ❌ Termin entfällt · 📆 Termin verschoben · 🕐 Zeit geändert · 🚪 Raum geändert ·
👤 Dozent geändert · ✏️ Titel geändert. Mehrere Änderungen am selben Termin landen in einem Eintrag.

Dazu aus dem Archiv: 📄 neues Dokument und 🔁 aktualisiertes Dokument, jeweils mit der Datei als Anhang
(auf dem iPad: Datei antippen → Teilen → GoodNotes), sowie 🗑 entfernte Dokumente als Liste.

Fehler wie ein fehlgeschlagener Login oder ein nicht erreichbares TraiNex gehen **nur an dich**,
und zwar einmal pro Fehlerserie. Sobald es wieder funktioniert, bekommst du eine Entwarnung.

**Löschschutz:** Würden auf einmal mehr als 20 % der kommenden Termine (mindestens 4) wegfallen,
ändert der Dienst nichts und fragt dich. Mit `/sync force` bestätigst du die Änderung.

## 5a. Unterlagen aus dem Archiv

Nach dem Stundenplan-Export öffnet der Dienst in **derselben TraiNex-Sitzung** Lernen → Archiv, wählt
„Nur Semester“ mit dem aktuellen Semester (das, dessen Zeitraum heute enthält, z. B. *3. Semester: 01.10.2026
bis 31.03.2027*) und klickt „anzeigen“. Das sind 2–3 zusätzliche Seitenabrufe pro Lauf. Dateien lädt er nur,
wenn sie neu sind oder sich ihr Eintrag in der Liste geändert hat. Bei gleichem Inhalt meldet er nichts.

- **Erster Lauf:** lädt alle Dokumente des Semesters, meldet aber nichts im Kanal. Du bekommst eine Nachricht
  mit der Anzahl und dem Link zur Übersicht.
- **Semesterwechsel:** passiert automatisch (April/Oktober). Auch hier gibt es nur eine Nachricht an dich.
  Soll ein bestimmtes Semester fest gelten, trägst du `DOCS_SEMESTER=3` in die `.env` ein.
- **Löschschutz:** Ist die Liste plötzlich leer oder würde mehr als die Hälfte wegfallen, ändert der Dienst
  nichts und fragt dich. `/sync force` bestätigt.
- **Große Dateien:** Telegram nimmt höchstens 50 MB. Größere Dateien werden nur gemeldet. Über
  `DOCS_MAX_MB` (Standard 100) lädt der Dienst sie gar nicht.
- **Ordner:** Die Abschnitte im Archiv werden dem Modul aus dem Stundenplan zugeordnet. „M12 Biochemie (2/3) -
  Vorlesung WiSe26“ landet also unter *M12 Biochemie/Molekularbiologie*. Abschnitte ohne Modulnummer behalten
  ihren Namen aus dem Archiv. Die Trennstriche, die TraiNex in lange Namen einfügt („Feinplan- ung“), entfernt der Dienst.
- Fehler beim Unterlagen-Abgleich stören den Stundenplan nicht. Du bekommst sie einmal pro Fehlerserie gemeldet.

### Übersicht und Spiegel in der Dateien-App

`/docs` zeigt dir zwei private Links (nicht in den Kanal posten):

- **Übersicht** (`…/trainex/d/<geheim>/`): alle Dokumente nach Ordnern sortiert, zum Herunterladen.
- **Feed** (`…/trainex/d/<geheim>/index.json`): für den Kurzbefehl, der alles nach iCloud Drive spiegelt.

**Kurzbefehl einrichten (einmalig, auf dem iPad):** Lege in der Dateien-App unter iCloud Drive einen Ordner
`TraiNex` an. Dann in der Kurzbefehle-App einen neuen Kurzbefehl „TraiNex-Unterlagen“ mit diesen Aktionen bauen:

1. **Inhalte von URL abrufen**: die Feed-Adresse aus `/docs`
2. **Datei aus Ordner abrufen**: Ordner `TraiNex`, Pfad `stand.txt`, „Fehler, wenn nicht gefunden“ **aus**
3. **Wenn** *Datei* **hat keinen Wert** → **Text** `0` · **Sonst** → **Text** *Datei* · **Ende**.
   Das Ergebnis mit **Variable festlegen** als `Stand` speichern.
4. **Wörterbuchwert abrufen**: Schlüssel `files` aus *Inhalte von URL*
5. **Wiederholen mit jedem Objekt** in *Wörterbuchwert*:
   1. **Wenn** *Wiederholungsobjekt → Schlüssel `changed`* (Typ **Zahl**) **ist größer als** *Stand*:
   2. **Inhalte von URL abrufen**: *Wiederholungsobjekt → Schlüssel `url`*
   3. **Name festlegen**: *Wiederholungsobjekt → Schlüssel `name`*
   4. **Datei sichern**: Ordner `TraiNex`, Unterpfad *Wiederholungsobjekt → Schlüssel `folder`*,
      „Ziel erfragen“ **aus**, „Überschreiben, falls Datei existiert“ **an**
   5. **Ende Wenn**
6. **Ende Wiederholen**
7. **Wörterbuchwert abrufen**: Schlüssel `updated` aus *Inhalte von URL* → **Text** mit diesem Wert →
   **Datei sichern**: Ordner `TraiNex`, Unterpfad `stand.txt`, „Ziel erfragen“ **aus**, „Überschreiben“ **an**

Danach in Kurzbefehle → **Automation** → **App** → GoodNotes → „Wird geöffnet“ → „Sofort ausführen“ den
Kurzbefehl wählen. Jedes Mal, wenn du GoodNotes öffnest, liegen die neuen Dateien bereits in
`TraiNex/<Modul>/`. Du importierst sie dann in GoodNotes über **+ → Importieren** oder per Drag & Drop
aus der Dateien-App. Aktualisierte Dokumente überschreiben die alte Datei in `TraiNex`. Entfernte bleiben
dort liegen. Deine Notizen in GoodNotes sind davon nicht betroffen, sie hängen an der importierten Kopie.

Feed-Format: `{"semester", "updated", "files": [{"path", "folder", "name", "url", "size", "added", "changed"}]}`
(Zeitangaben als Unix-Sekunden).

### Wenn das Archiv nicht gefunden wird

Der Dienst sucht die Archiv-Seite und den Semesterfilter selbst. Findet er sie nicht, meldet er
„Archiv-Seite … nicht gefunden“ bzw. „Semester-Auswahl … nicht gefunden“. Dann:

```bash
sudo ./install.sh debug      # zeigt jeden Schritt; die Archiv-Seiten landen in ./trainex-debug/
```

Schick mir die Ausgabe ab „D1 Archiv“ und die Dateien aus `trainex-debug/`. Sie enthalten deinen Namen und
Kurs, kürze sie also vorher, falls nötig. Als Notlösung kannst du den Pfad der Archiv-Seite (aus der Zeile
„Frames:“) als `DOCS_URL=…` in die `.env` eintragen. `DOCS=0` schaltet den Abgleich ganz ab.

## 6. Wartung

```bash
journalctl -u trainex-sync -n 50        # Log
sudo systemctl restart trainex-sync     # nach Änderungen an .env
sudo ./install.sh                       # manuelles Update (ohne GitHub): neue Dateien kopieren, dann ausführen
```

- **Passwort geändert:** Neues Passwort in `/opt/trainex-sync/.env` eintragen, dann `sudo systemctl restart trainex-sync`.
- **Daten:** `/var/lib/trainex-sync/state.json` (Termine, Einstellungen, eingetragene Fehlzeiten, Stand der
  Unterlagen), `/var/www/trainex/*.ics`, `/var/www/trainex/d/<geheim>/` (Unterlagen).
- **Entfernen:** `sudo systemctl disable --now trainex-sync`, dann die Include-Zeile aus nginx entfernen und
  `/opt/trainex-sync`, `/var/lib/trainex-sync`, `/var/www/trainex`, `/etc/nginx/snippets/trainex.conf` und
  `/etc/systemd/system/trainex-sync.service` löschen.

## 7. Automatisches Deployment über GitHub

Nach der Erstinstallation (Abschnitt 2) läuft jedes Update so: Du pushst auf `main`, dann laufen die
Tests auf GitHub, und bei Erfolg wird die neue Version auf dem vServer eingespielt. Wenn sie nicht sauber
startet, wird sie automatisch zurückgerollt. Nach einem erfolgreichen Update meldet der Bot dir
„🚀 trainex-sync aktualisiert: v1.7 → v1.8“.

```
git push ──► GitHub Actions: Tests ──► ssh trainex-deploy@tillianbo.com  (tar.gz über stdin)
                                          └─► sudo trainex-deploy: prüfen → installieren → Neustart
                                                                    └─ Fehler? → Rollback
```

### Einmalig einrichten

Auf dem Server, im geklonten Repo:

```bash
sudo ./deploy/setup-deploy.sh tillianbo.com 22
```

Das Skript legt den Nutzer `trainex-deploy` an. Dessen SSH-Schlüssel darf **nur** das Deploy-Skript
ausführen: keine Shell, kein Port-Forwarding. Außerdem erzeugt es das Schlüsselpaar und zeigt drei Werte an.
Die trägst du in GitHub unter **Settings → Secrets and variables → Actions → New repository secret** ein:

| Secret | Inhalt |
|---|---|
| `DEPLOY_HOST` | `tillianbo.com` |
| `DEPLOY_SSH_KEY` | der private Schlüssel inklusive der Zeilen `-----BEGIN…` / `-----END…` |
| `DEPLOY_KNOWN_HOSTS` | die Host-Key-Zeilen (schützt davor, dass sich jemand als dein Server ausgibt) |

Läuft SSH nicht auf Port 22, legst du zusätzlich unter „Variables“ `DEPLOY_PORT` an.

Danach unter **Actions → Test & Deploy → Run workflow** einmal manuell starten und prüfen, ob es grün wird.

### Was deployt wird – und was nicht

- **Deployt:** `trainex_sync.py`, `trainex-sync.service`, `trainex.conf` und `.env.example`
- **Nie angefasst:** deine `.env` mit den Zugangsdaten, `state.json` und die Abo-Dateien
- **Abgelehnt** wird ein Release mit Syntaxfehlern, eine Unit, die nicht als `User=trainex` läuft, und ein
  nginx-Snippet, bei dem `nginx -t` fehlschlägt.

### Sicherheit

Wer auf `main` pushen kann, bestimmt, welcher Code auf dem Server läuft. Halte das Repo also privat,
aktiviere 2FA auf GitHub und schütze auf Wunsch `main` (Settings → Branches). Im Environment `production`
kannst du außerdem „Required reviewers“ setzen. Dann wartet jeder Deploy auf deinen Klick.

### Lokal testen

```bash
python3 -m unittest discover -s tests -v
```

## Hinweise

- Der Export ist **dein** persönlicher Plan (Gruppe WS 25-II / 2a). Für Kollegen aus anderen Gruppen
  ist er teilweise falsch.
- Wer die Abo-URL hat, sieht den Plan. Falls sie in falsche Hände gerät: `/newurl`.
- Zugangsdaten stehen nur in `/opt/trainex-sync/.env` (root, 600). Sie tauchen nicht im Log, nicht in
  Telegram und nicht in `/status` auf.
