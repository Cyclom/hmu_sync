#!/usr/bin/env bash
# trainex-sync – Installation & Hilfsbefehle (als root ausführen)
#   sudo ./install.sh            installieren / aktualisieren
#   sudo ./install.sh check      Testabruf von TraiNex (schreibt nichts)
#   sudo ./install.sh debug      Testabruf mit Details zu jedem Schritt
#   sudo ./install.sh discover   Telegram-Chat-IDs anzeigen
#   sudo ./install.sh testmsg    Testnachricht an dich + Kanal
#   sudo ./install.sh start      Dienst aktivieren und starten
set -euo pipefail

APP=/opt/trainex-sync
WEB=/var/www/trainex
SNIPPET=/etc/nginx/snippets/trainex.conf
UNIT=/etc/systemd/system/trainex-sync.service
SRC="$(cd "$(dirname "$0")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen."; exit 1; }

run_py() { /usr/bin/python3 "$APP/trainex_sync.py" --env "$APP/.env" "$@"; }

case "${1:-install}" in
install)
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
        || { echo "Python >= 3.9 nötig."; exit 1; }

    id trainex &>/dev/null || useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin trainex
    usermod -aG adm trainex   # Lesezugriff auf nginx-Logs (Abrufstatistik)

    install -d -m 755 -o root -g root "$APP"
    install -m 755 -o root -g root "$SRC/trainex_sync.py" "$APP/trainex_sync.py"
    install -d -m 755 -o trainex -g trainex "$WEB"

    if [[ ! -f $APP/.env ]]; then
        install -m 600 -o root -g root "$SRC/.env.example" "$APP/.env"
        echo "→ $APP/.env angelegt."
    fi
    chmod 600 "$APP/.env"; chown root:root "$APP/.env"
    if grep -q '^ICS_TOKEN=$' "$APP/.env"; then
        TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
        sed -i "s|^ICS_TOKEN=$|ICS_TOKEN=$TOKEN|" "$APP/.env"
        echo "→ Geheime Abo-URL erzeugt."
    fi

    install -d /etc/nginx/snippets
    install -m 644 "$SRC/trainex.conf" "$SNIPPET"
    install -m 644 "$SRC/trainex-sync.service" "$UNIT"
    systemctl daemon-reload

    if systemctl is-active --quiet trainex-sync; then
        systemctl restart trainex-sync
        echo "✓ Aktualisiert und neu gestartet."
    else
        cat <<EOF

✓ Installiert. Nächste Schritte (Details in README.md):
  1. sudo nano $APP/.env        TraiNex-Login + Bot-Token eintragen
  2. sudo ./install.sh check                  TraiNex-Abruf testen
  3. sudo ./install.sh discover               Chat-IDs ermitteln, in .env eintragen
  4. sudo ./install.sh testmsg                Telegram testen
  5. In den server-Block von tillianbo.com:  include $SNIPPET;
     sudo nginx -t && sudo systemctl reload nginx
  6. sudo ./install.sh start
EOF
    fi
    ;;
check)    run_py check ;;
debug)    run_py check --debug ;;
discover) run_py discover ;;
testmsg)  run_py testmsg ;;
start)
    systemctl enable --now trainex-sync
    sleep 3
    systemctl --no-pager --lines=5 status trainex-sync || true
    echo
    echo "Logs: journalctl -u trainex-sync -f"
    ;;
*)
    sed -n '2,9p' "$0"; exit 1 ;;
esac
