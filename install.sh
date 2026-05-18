#!/bin/bash
# eVOLVER pilot installer — run as the `pi` user from /home/pi/evolver-gui
set -euo pipefail

REPO_DIR="/home/pi/evolver-gui"
VENV_DIR="$REPO_DIR/.venv"
SECRET_DIR="/etc/evolver"
SECRET_FILE="$SECRET_DIR/secret.env"
LOG_DIR="/var/log/evolver"

if [[ "$(pwd)" != "$REPO_DIR" ]]; then
    echo "ERROR: run this from $REPO_DIR (currently: $(pwd))" >&2
    exit 1
fi

echo "[1/6] Installing system packages (python3, python3-venv, python3-pip)..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

echo "[2/6] Creating Python virtualenv at $VENV_DIR..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo "[3/6] Adding pi to dialout group (for /dev/ttyAMA0 access)..."
sudo usermod -aG dialout pi

echo "[4/6] Creating log directory $LOG_DIR..."
sudo mkdir -p "$LOG_DIR"
sudo chown pi:pi "$LOG_DIR"

echo "[5/6] Generating Flask secret at $SECRET_FILE (if missing)..."
sudo mkdir -p "$SECRET_DIR"
if [[ ! -f "$SECRET_FILE" ]]; then
    SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
    echo "EVOLVER_SECRET_KEY=$SECRET" | sudo tee "$SECRET_FILE" >/dev/null
    sudo chmod 600 "$SECRET_FILE"
    sudo chown root:root "$SECRET_FILE"
    echo "  -> generated new secret"
else
    echo "  -> $SECRET_FILE already exists, keeping it"
fi

echo "[6/6] Installing systemd unit..."
sudo cp "$REPO_DIR/evolver.service" /etc/systemd/system/evolver.service
sudo systemctl daemon-reload

cat <<'EOF'

Install complete.

Next steps (do NOT skip the ordering — old supervisor processes hold /dev/ttyAMA0):

  1. Stop the legacy system:
       sudo supervisorctl stop all
       sudo supervisorctl status         # confirm all STOPPED
       sudo fuser /dev/ttyAMA0           # must print nothing

  2. Log out and back in so the dialout group membership takes effect:
       exit
       ssh pi@192.168.1.2

  3. Verify install with the mock first (does not touch serial):
       cd /home/pi/evolver-gui
       .venv/bin/python server/app.py --mock
       # In a browser: http://192.168.1.2:5000
       # Ctrl+C to stop.

  4. First real-hardware boot, foreground (still not systemd):
       cd /home/pi/evolver-gui
       .venv/bin/python server/app.py
       # Confirm "loaded calibration" in logs.
       # Run hardware validation per DEPLOY.md Phase 4.

  5. Once validation passes, enable autostart:
       sudo systemctl enable --now evolver
       sudo systemctl status evolver
       sudo journalctl -u evolver -f

See DEPLOY.md for the full validation + pilot procedure.
EOF
