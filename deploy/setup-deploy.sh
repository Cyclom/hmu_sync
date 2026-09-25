#!/bin/bash
# Einmalige Einrichtung des automatischen Deploys von GitHub (als root ausführen).
#
#   sudo ./deploy/setup-deploy.sh [HOST] [SSH-PORT]
#   z. B. sudo ./deploy/setup-deploy.sh tillianbo.com 22
#
# Legt einen eigenen Nutzer „trainex-deploy“ an, dessen SSH-Schlüssel NUR das Deploy-Skript
# ausführen darf (keine Shell, kein Port-Forwarding). Erzeugt das Schlüsselpaar und gibt die
# Werte für die GitHub-Secrets aus.
set -euo pipefail

HOST=${1:-tillianbo.com}
PORT=${2:-22}
USER_NAME=trainex-deploy
HOME_DIR=/var/lib/$USER_NAME
SRC="$(cd "$(dirname "$0")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen."; exit 1; }
[[ -f /opt/trainex-sync/.env ]] || { echo "Erst die Erstinstallation mit ./install.sh durchführen."; exit 1; }
command -v ssh-keygen >/dev/null || { echo "ssh-keygen fehlt (apt install openssh-client)."; exit 1; }

# 1) Nutzer (Shell /bin/sh ist nötig, damit sshd den erzwungenen Befehl ausführen kann)
if ! id "$USER_NAME" &>/dev/null; then
    useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/sh "$USER_NAME"
fi
passwd -l "$USER_NAME" >/dev/null 2>&1 || true

# 2) Deploy-Skript + sudo-Regel (nur genau dieses Skript, ohne Argumente)
install -m 755 -o root -g root "$SRC/trainex-deploy" /usr/local/sbin/trainex-deploy
SUDOERS=/etc/sudoers.d/trainex-deploy
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/trainex-deploy \"\"" > "$SUDOERS.tmp"
chmod 440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null
mv "$SUDOERS.tmp" "$SUDOERS"

# 3) Schlüsselpaar erzeugen, öffentlichen Schlüssel mit Einschränkungen hinterlegen
KEYDIR=$(mktemp -d)
trap 'rm -rf "$KEYDIR"' EXIT
ssh-keygen -q -t ed25519 -N "" -C "github-actions-trainex-sync" -f "$KEYDIR/key"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.ssh"
echo "restrict,command=\"sudo -n /usr/local/sbin/trainex-deploy\" $(cat "$KEYDIR/key.pub")" \
    > "$HOME_DIR/.ssh/authorized_keys"
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.ssh/authorized_keys"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"

# 4) Hinweis, falls sshd nur bestimmte Nutzer zulässt
if grep -RhiE '^\s*(AllowUsers|AllowGroups)' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ 2>/dev/null | grep -q .; then
    echo
    echo "⚠️  Deine sshd-Konfiguration nutzt AllowUsers/AllowGroups."
    echo "    Ergänze dort '$USER_NAME' und lade sshd neu: sudo systemctl reload ssh"
fi

# 5) Werte für GitHub ausgeben
if [[ $PORT == 22 ]]; then KH_HOST=$HOST; else KH_HOST="[$HOST]:$PORT"; fi
KNOWN=$(for k in /etc/ssh/ssh_host_*_key.pub; do echo "$KH_HOST $(cut -d' ' -f1,2 "$k")"; done)

cat <<EOF

✓ Deploy-Zugang eingerichtet.

Trage in GitHub unter  Repo → Settings → Secrets and variables → Actions  ein:

── Secret DEPLOY_HOST ─────────────────────────────────────────────
$HOST
── Secret DEPLOY_SSH_KEY  (alles inkl. BEGIN/END-Zeilen) ───────────
$(cat "$KEYDIR/key")
── Secret DEPLOY_KNOWN_HOSTS ──────────────────────────────────────
$KNOWN
───────────────────────────────────────────────────────────────────
EOF
[[ $PORT == 22 ]] || echo "Außerdem unter 'Variables': DEPLOY_PORT = $PORT"
echo
echo "Der private Schlüssel wird jetzt gelöscht und existiert danach nur noch in GitHub."
