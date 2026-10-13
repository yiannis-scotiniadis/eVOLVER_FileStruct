#!/bin/bash
# eVOLVER installer — run as the `pi` user from /home/pi/evolver-gui, after deploy.sh.
#
# This directory is a runtime export, not a git checkout: `deploy.sh` (run from the
# `evolver-src` source clone) builds it via `git archive`, so it never contains docs, tests,
# or DEPLOY.md itself — see DEPLOY.md in evolver-src, or on GitHub, for the full procedure.
#
# Safe to re-run on every update. The venv is rebuilt only when the interpreter's version
# changes, and pip runs only when requirements.txt has changed since the last successful
# install — so a routine update needs no network once the service is stopped.
set -euo pipefail

REPO_DIR="/home/pi/evolver-gui"
VENV_DIR="$REPO_DIR/.venv"
REQ_STAMP="$VENV_DIR/.requirements.sha256"
SECRET_DIR="/etc/evolver"
SECRET_FILE="$SECRET_DIR/secret.env"
if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run this as pi, not root — it calls sudo itself where it needs to." >&2
    exit 1
fi
if [[ "$(pwd)" != "$REPO_DIR" ]]; then
    echo "ERROR: run this from $REPO_DIR (currently: $(pwd))" >&2
    exit 1
fi

# Pick a Python 3.10+ interpreter: prefer the system python3 (Bookworm ships
# 3.11, Trixie ships 3.13), fall back to a hand-built /opt/python3.11 (the
# Jessie source-build escape path in DEPLOY.md Addendum 1).
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PYTHON_BIN="$(command -v python3)"
elif [[ -x /opt/python3.11/bin/python3.11 ]]; then
    PYTHON_BIN="/opt/python3.11/bin/python3.11"
else
    echo "ERROR: need Python 3.10+ and none was found." >&2
    echo "Modern Raspberry Pi OS (Bookworm/Trixie) ships it as python3." >&2
    echo "On an ancient OS, build /opt/python3.11 per DEPLOY.md Addendum 1 (N3-N5) in evolver-src." >&2
    exit 1
fi

echo "[1/5] Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

echo "[2/5] Python virtualenv at $VENV_DIR..."
want=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
have=$("$VENV_DIR/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
if [[ "$have" != "$want" ]]; then
    # A venv built for another interpreter (e.g. after an OS upgrade) keeps importing the old
    # site-packages; `python -m venv` over it does not rebuild them.
    [[ -z "$have" ]] || echo "  -> venv is Python $have but the interpreter is $want: rebuilding"
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
req_hash=$(sha256sum "$REPO_DIR/requirements.txt" | cut -d' ' -f1)
if [[ -f "$REQ_STAMP" && "$(cat "$REQ_STAMP")" == "$req_hash" ]]; then
    echo "  -> requirements.txt unchanged since the last install; skipping pip"
else
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"
    echo "$req_hash" >"$REQ_STAMP"
fi

echo "[3/5] Adding pi to dialout group (for /dev/ttyAMA0 access)..."
sudo usermod -aG dialout pi

echo "[4/5] Generating Flask secret at $SECRET_FILE (if missing)..."
sudo mkdir -p "$SECRET_DIR"
if [[ ! -f "$SECRET_FILE" ]]; then
    SECRET=$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))')
    echo "EVOLVER_SECRET_KEY=$SECRET" | sudo tee "$SECRET_FILE" >/dev/null
    sudo chmod 600 "$SECRET_FILE"
    sudo chown root:root "$SECRET_FILE"
    echo "  -> generated new secret"
else
    echo "  -> $SECRET_FILE already exists, keeping it"
fi

echo "[5/5] Installing systemd unit..."
sudo cp "$REPO_DIR/evolver.service" /etc/systemd/system/evolver.service
sudo systemctl daemon-reload

if systemctl is-enabled --quiet evolver 2>/dev/null; then
    cat <<'EOF'

Install complete. evolver.service is already enabled — start it and check it came up:
     sudo systemctl start evolver
     systemctl status evolver --no-pager
     tail -n 40 /home/pi/evolver-gui/logs/evolver.log
EOF
    exit 0
fi

cat <<'EOF'

Install complete — first-install next steps:

  1. Legacy cards only — stop the 2016 supervisor processes, which hold /dev/ttyAMA0:
       sudo supervisorctl stop all
       sudo fuser /dev/ttyAMA0           # must print nothing

  2. Log out and back in so the dialout group membership takes effect:
       exit
       ssh pi@evolver-001

  3. Verify install with the mock first (does not touch serial):
       cd /home/pi/evolver-gui
       .venv/bin/python server/app.py --mock
       # In a browser: http://evolver-001:5000
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

See DEPLOY.md in evolver-src (or on GitHub) for the full validation + pilot procedure —
it isn't in this directory on purpose; see the comment at the top of this script.
EOF
