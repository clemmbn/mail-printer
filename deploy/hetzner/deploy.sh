#!/usr/bin/env bash
#
# deploy.sh — push mail-printer's server code to the Hetzner VPS and restart it.
#
# Purpose
#   One idempotent command to go from a local working tree to a running,
#   up-to-date server: rsync the code, install deps with uv, restart systemd,
#   then verify the unit actually came back up.
#
# Usage
#   ./deploy/hetzner/deploy.sh
#   REMOTE_HOST=mailprinter@1.2.3.4 ./deploy/hetzner/deploy.sh
#   DRY_RUN=1 ./deploy/hetzner/deploy.sh      # show what rsync would change
#
# What it deliberately does NOT do
#   - It never touches the remote .env (secrets live only on the VPS).
#   - It never touches the remote data/ dir (SQLite DB, photos, tickets).
#   - It never installs Caddy/systemd units: that is the one-time setup in
#     README.md. Unit file changes are copied but you are told to reload.
#
# set -euo pipefail:
#   -e  stop at the first failing command (never restart a half-synced app)
#   -u  a typo'd variable is an error, not an empty string
#   -o pipefail  a failure anywhere in a pipeline fails the whole pipeline
set -euo pipefail

# ── Configuration (override via env vars) ─────────────────────────────────
# PLACEHOLDER: change these defaults, or export them in your shell.
REMOTE_HOST="${REMOTE_HOST:-mailprinter@mail.example.com}"
REMOTE_PATH="${REMOTE_PATH:-/srv/mail-printer}"
SERVICE_NAME="${SERVICE_NAME:-mail-printer-server}"
# Set DRY_RUN=1 to preview the file changes without applying anything.
DRY_RUN="${DRY_RUN:-0}"

# Repo root = two levels up from this script, so the script works no matter
# which directory it is invoked from.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────
# Fail early and loudly rather than half-way through an rsync.
command -v rsync >/dev/null || die "rsync not found locally"
command -v ssh   >/dev/null || die "ssh not found locally"
[[ -f "$REPO_ROOT/pyproject.toml" ]] || die "not a mail-printer checkout: $REPO_ROOT"

log "repo:    $REPO_ROOT"
log "target:  $REMOTE_HOST:$REMOTE_PATH"
log "service: $SERVICE_NAME"

# Verify SSH works before doing anything else, with a short timeout so a bad
# host fails in seconds instead of hanging.
log "checking SSH connectivity…"
ssh -o ConnectTimeout=10 -o BatchMode=yes "$REMOTE_HOST" true \
  || die "cannot SSH to $REMOTE_HOST (key set up? host reachable?)"

# ── 1. Sync the code ──────────────────────────────────────────────────────
# --delete keeps the remote tree an exact mirror of local (so deleted files
# really disappear), which is why the excludes below are load-bearing: .env
# and data/ only exist remotely and must survive the mirror.
RSYNC_ARGS=(
  --archive           # preserve perms/times, recurse
  --compress          # the VPS link is the bottleneck, not the CPU
  --human-readable
  --delete
  --exclude '.git/'
  --exclude '.venv/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude '.pytest_cache/'
  --exclude '.ruff_cache/'
  --exclude '.DS_Store'
  --exclude '.claude/'
  --exclude '.env'    # secrets live only on the VPS
  --exclude 'data/'   # SQLite DB + photos + tickets live only on the VPS
  --exclude 'packages/print-agent/'  # that package belongs on the Pi, not here
)
if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY_RUN=1 — showing changes only"
  RSYNC_ARGS+=(--dry-run --itemize-changes)
fi

log "syncing code…"
rsync "${RSYNC_ARGS[@]}" "$REPO_ROOT/" "$REMOTE_HOST:$REMOTE_PATH/"

if [[ "$DRY_RUN" == "1" ]]; then
  log "dry run complete — nothing was installed or restarted"
  exit 0
fi

# ── 2. Install dependencies ───────────────────────────────────────────────
# `uv sync --package mail-printer-server` installs ONLY the server member and
# its deps (not the Pi's python-escpos/usb stack, which has no business on the
# VPS). --frozen makes the install reproducible: it uses uv.lock as-is and
# fails if the lockfile is out of date, instead of silently resolving new
# versions on the production box.
log "installing dependencies with uv…"
ssh "$REMOTE_HOST" "cd '$REMOTE_PATH' && uv sync --frozen --package mail-printer-server"

# ── 3. Restart the service ────────────────────────────────────────────────
# `restart` (not reload): uvicorn has no reload signal, and a restart drops
# the print-agent WebSocket — which is fine, print-agent reconnects with
# backoff and the server re-flushes every `queued` message on reconnect.
log "restarting $SERVICE_NAME…"
ssh "$REMOTE_HOST" "sudo systemctl restart '$SERVICE_NAME'"

# ── 4. Verify ─────────────────────────────────────────────────────────────
# A restart "succeeding" only means systemd forked it; the process can still
# die a second later on a bad config. Give it a moment, then assert it is
# genuinely active, and print the tail of the log either way.
log "waiting for the service to settle…"
sleep 3
if ssh "$REMOTE_HOST" "systemctl is-active --quiet '$SERVICE_NAME'"; then
  log "✅ $SERVICE_NAME is active"
  ssh "$REMOTE_HOST" "journalctl -u '$SERVICE_NAME' -n 15 --no-pager"
else
  printf '\033[1;31m[deploy] ❌ %s failed to start — last 50 log lines:\033[0m\n' "$SERVICE_NAME" >&2
  ssh "$REMOTE_HOST" "journalctl -u '$SERVICE_NAME' -n 50 --no-pager" >&2 || true
  exit 1
fi

log "done."
