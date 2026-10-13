#!/bin/bash
# deploy.sh <tag> [runtime-dir] — export a release tag into the eVOLVER runtime directory.
#
# Run from the source clone (`evolver-src`, /home/pi/evolver-src on the Pi). The tag is
# exported with `git archive`, which honours the `export-ignore` rules in .gitattributes, and
# rsynced into the runtime directory (/home/pi/evolver-gui by default) with --delete. The
# rig's state — calibration/, experiments/, exports/, logs/, machine/, .venv/ — is excluded
# from that sync and never touched. DEPLOYED_VERSION in the runtime directory records what
# is deployed, since the runtime directory has no git history of its own.
#
# rsync --delete is exactly as safe as the directory it is pointed at. On 2026-10-11 this
# script was run from inside a runtime directory that was still a git clone, and the sync
# deleted that clone's .git. Every check before the sync exists to turn a wrong target into
# a refusal instead of a deletion. See DEPLOY.md, "Installing updates".
set -euo pipefail

DEFAULT_RUNTIME=/home/pi/evolver-gui
STATE_DIRS=(calibration experiments exports logs machine .venv)
MARKER=DEPLOYED_VERSION
MANIFEST=.deploy-manifest
STAGE=

trap 'if [[ -n $STAGE ]]; then rm -rf "$STAGE"; fi' EXIT

die() {
    echo "ERROR: $*" >&2
    exit 1
}

in_list() {
    local needle=$1 item
    shift
    for item in "$@"; do
        [[ $needle == "$item" ]] && return 0
    done
    return 1
}

# Absolute, symlink-free path; the directory itself need not exist yet.
canonical() {
    local p=$1 parent
    [[ $p == /* ]] || die "the runtime directory must be an absolute path (got: $p)"
    if [[ -d $p ]]; then
        (cd "$p" && pwd -P)
    else
        parent=$(dirname "$p")
        [[ -d $parent ]] || die "parent of the runtime directory does not exist: $parent"
        echo "$(cd "$parent" && pwd -P)/$(basename "$p")"
    fi
}

main() {
    local tag=${1:-}
    [[ -n $tag ]] || die "usage: deploy.sh <tag> [runtime-dir]"
    [[ $EUID -ne 0 ]] || die "do not run as root — the runtime directory must stay owned by pi, the service user."
    command -v rsync >/dev/null || die "rsync is not installed (sudo apt install rsync)."

    local src runtime home
    src=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
    runtime=$(canonical "${2:-$DEFAULT_RUNTIME}")
    home=$(cd ~ && pwd -P)

    [[ $(git -C "$src" rev-parse --show-toplevel 2>/dev/null) == "$src" ]] \
        || die "$src is not the top of a git clone. Run deploy.sh from the source clone (evolver-src)."

    # --- The target. Each check below is a way the --delete sync destroys something. ---
    [[ $runtime != / && $runtime != "$home" ]] || die "refusing to deploy into $runtime."
    case "$runtime/" in
        "$src"/*) die "$runtime is the source clone, or inside it — deploying there deletes the clone's own .git." ;;
    esac
    case "$src/" in
        "$runtime"/*) die "the source clone $src is inside the runtime directory $runtime." ;;
    esac
    [[ ! -e $runtime/.git ]] \
        || die "$runtime contains .git, and a runtime directory is never a git checkout. If this rig still uses the old single-clone layout, migrate it first: DEPLOY.md, \"Migrating a single-clone rig\"."
    if [[ -d $runtime && -n $(ls -A "$runtime") && ! -f $runtime/server/app.py ]]; then
        die "$runtime is not empty and is not an eVOLVER runtime directory (no server/app.py)."
    fi
    if [[ $runtime == "$DEFAULT_RUNTIME" ]] && command -v systemctl >/dev/null \
        && systemctl is-active --quiet evolver; then
        die "evolver.service is running. Stop it first (this ends any running experiment): sudo systemctl stop evolver"
    fi

    # --- The release. ---
    git -C "$src" fetch --tags --quiet \
        || echo "WARNING: could not fetch from origin; deploying from the tags already in $src." >&2
    local commit
    if git -C "$src" show-ref --verify --quiet "refs/tags/$tag"; then
        commit=$(git -C "$src" rev-parse "refs/tags/$tag^{commit}")
    elif [[ $tag =~ ^[0-9a-f]{7,40}$ ]] && commit=$(git -C "$src" rev-parse --verify --quiet "$tag^{commit}"); then
        echo "NOTE: $tag is a commit, not a tag. Use this only when the release tag no longer exists." >&2
    else
        die "$tag is not a tag in $src. Deploy a release tag, never a branch. Available: git -C $src tag -l 'pilot-*'"
    fi

    STAGE=$(mktemp -d)
    echo "[1/3] Exporting $tag ($commit)..."
    git -C "$src" archive "$commit" | tar -x -C "$STAGE"
    [[ -f $STAGE/server/app.py ]] || die "$tag does not contain server/app.py, so it is not a deployable release."

    # rsync --delete removes every top-level entry the release doesn't carry. Anything the
    # operator put there (backups, scratch) is refused here rather than silently deleted;
    # entries a previous release installed (.deploy-manifest) are fair game.
    if [[ -d $runtime ]]; then
        local name unknown=""
        while IFS= read -r name; do
            [[ -e $STAGE/$name ]] && continue
            in_list "$name" "${STATE_DIRS[@]}" "$MARKER" "$MANIFEST" && continue
            [[ -f $runtime/$MANIFEST ]] && grep -qxF -- "$name" "$runtime/$MANIFEST" && continue
            unknown+="    $runtime/$name"$'\n'
        done < <(ls -A "$runtime")
        [[ -z $unknown ]] || die "these aren't part of the release and the sync would delete them — move them out of $runtime first:
$unknown"
        [[ ! -f $runtime/$MARKER ]] || echo "      currently deployed: $(grep '^tag=' "$runtime/$MARKER" | cut -d= -f2-)"
    fi

    echo "[2/3] Syncing into $runtime (state directories untouched)..."
    mkdir -p "$runtime"
    local d excludes=(--exclude=/.git --exclude="/$MARKER" --exclude="/$MANIFEST")
    for d in "${STATE_DIRS[@]}"; do
        excludes+=(--exclude="/$d")
    done
    rsync -a --delete "${excludes[@]}" "$STAGE"/ "$runtime"/

    # A first deploy seeds the state directories the release carries (the tracked
    # calibration, experiments/.gitkeep, exports/.gitkeep); an update never touches them.
    for d in "${STATE_DIRS[@]}"; do
        if [[ ! -e $runtime/$d && -e $STAGE/$d ]]; then
            cp -a "$STAGE/$d" "$runtime/$d"
        fi
    done

    echo "[3/3] Recording $runtime/$MARKER..."
    ls -A "$STAGE" >"$runtime/$MANIFEST"
    {
        echo "tag=$tag"
        echo "commit=$commit"
        echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "source=$src"
    } >"$runtime/$MARKER"

    cat <<EOF

Deployed $tag to $runtime (state directories left as they were).
Next:  cd $runtime && ./install.sh
Then:  sudo systemctl start evolver
EOF
}

# Everything runs from main so bash has parsed the whole file before any of it executes.
main "$@"
