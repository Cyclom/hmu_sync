# trainex-sync – Anleitung

Der Dienst holt den Studienplan regelmäßig aus TraiNex und stellt ihn als iCal-Abo unter
`https://tillianbo.com/trainex/<geheim>.ics` bereit. Änderungen meldet er in einem Telegram-Kanal.
Steuern kannst nur du ihn, per Telegram.

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
| `/help` | Hilfe |

Einstellungen bleiben bei einem Neustart erhalten. In der Ruhezeit (`QUIET_HOURS`, standardmäßig
22–6 Uhr) laufen keine automatischen Syncs, `/sync` funktioniert aber immer.

### Kanal direkt in Telegram hinzufügen/entfernen

Kein `.env`-Eintrag mehr nötig: Bot als Admin in einen Kanal holen (Nachrichten senden reicht) und dort
**direkt im Kanal** `/addchannel` posten. Der Bot bestätigt im Kanal und meldet sich zusätzlich bei dir
privat. `/removechannel` im selben Kanal nimmt ihn wieder raus. Es können mehrere Kanäle gleichzeitig
registriert sein – Änderungsmeldungen gehen dann an alle. `TELEGRAM_CHANNEL_ID` in der `.env` funktioniert
weiterhin als Fallback, wird aber ignoriert, sobald mindestens ein Kanal über `/addchannel` registriert ist.

## 5. Was im Kanal gemeldet wird

➕ neuer Termin · ❌ Termin entfällt · 📆 Termin verschoben · 🕐 Zeit geändert · 🚪 Raum geändert ·
👤 Dozent geändert · ✏️ Titel geändert. Mehrere Änderungen am selben Termin landen in einem Eintrag.

Fehler wie ein fehlgeschlagener Login oder ein nicht erreichbares TraiNex gehen **nur an dich**,
und zwar einmal pro Fehlerserie. Sobald es wieder funktioniert, bekommst du eine Entwarnung.

**Löschschutz:** Würden auf einmal mehr als 20 % der kommenden Termine (mindestens 4) wegfallen,
ändert der Dienst nichts und fragt dich. Mit `/sync force` bestätigst du die Änderung.

## 6. Wartung

```bash
journalctl -u trainex-sync -n 50        # Log
sudo systemctl restart trainex-sync     # nach Änderungen an .env
sudo ./install.sh                       # manuelles Update (ohne GitHub): neue Dateien kopieren, dann ausführen
```

- **Passwort geändert:** Neues Passwort in `/opt/trainex-sync/.env` eintragen, dann `sudo systemctl restart trainex-sync`.
- **Daten:** `/var/lib/trainex-sync/state.json` (Termine, Einstellungen), `/var/www/trainex/*.ics`.
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
