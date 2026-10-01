#!/bin/bash
# deploy.sh <tag> — build/update the runtime directory from a tag of this source checkout.
#
# Run from a full git clone of this repo (DEPLOY.md calls that clone `evolver-src`; on the
# Pi it's expected to live at /home/pi/evolver-src). It fetches the tag, then exports it
# with `git archive`, which honors the `export-ignore` rules in `.gitattributes` — so the
# runtime directory only ever receives the files actually needed to run the server (see
# CLAUDE.md's repo structure section for what that set is). Docs, the pytest suite,
# benchmarking/verification scripts and the frozen 2016 reference code never reach it.
#
# The runtime directory's persistent state — calibration/, experiments/, logs/, machine/,
# exports/, .venv/ — is never touched: every sync here is rsync --delete with those
# directories excluded, so a deploy can only ever add/update/remove code and config files,
# never the rig's data. That's what makes re-running this safe for an update or a rollback.
set -euo pipefail

TAG="${1:?usage: deploy.sh <tag> [runtime-dir]}"
RUNTIME_DIR="${2:-/home/pi/evolver-gui}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -d "$SRC_DIR/.git" ]]; then
    echo "ERROR: $SRC_DIR is not a git checkout — deploy.sh must live at the root of the" >&2
    echo "       source clone (evolver-src), not be copied out on its own." >&2
    exit 1
fi

cd "$SRC_DIR"
echo "[1/3] Checking out $TAG in $SRC_DIR..."
git fetch --tags --quiet
git checkout --quiet "$TAG"

STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

echo "[2/3] Exporting $TAG (export-ignore'd paths excluded) to a staging tree..."
git archive "$TAG" | tar -x -C "$STAGE_DIR"

echo "[3/3] Syncing into $RUNTIME_DIR (state directories untouched)..."
mkdir -p "$RUNTIME_DIR"
rsync -a --delete \
    --exclude calibration \
    --exclude experiments \
    --exclude logs \
    --exclude machine \
    --exclude exports \
    --exclude .venv \
    "$STAGE_DIR"/ "$RUNTIME_DIR"/

# First-ever deploy: seed the state directories that install.sh/app.py expect to exist,
# without overwriting anything already there on an update.
for d in calibration experiments logs machine exports; do
    if [[ ! -e "$RUNTIME_DIR/$d" && -e "$STAGE_DIR/$d" ]]; then
        cp -r "$STAGE_DIR/$d" "$RUNTIME_DIR/$d"
    fi
done

cat <<EOF

Done. $RUNTIME_DIR is now $TAG (state directories left as they were).
Next: cd $RUNTIME_DIR && ./install.sh   (updates the venv, re-registers the systemd unit)
Then: sudo systemctl restart evolver    (only if the unit was already running)
EOF
