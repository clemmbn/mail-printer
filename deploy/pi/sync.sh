#!/usr/bin/env bash
#
# sync.sh — push mail-printer's print-agent code to the Raspberry Pi.
#
# Purpose
#   Same job as task-printer's sync.sh (rsync the tree to the Pi), extended
#   with the two steps that always followed it by hand: `uv sync` and a
#   service restart. Idempotent — run it as often as you like.
#
# Usage
#   ./deploy/pi/sync.sh
#   PI_HOST=clemmbn@raspzero.local ./deploy/pi/sync.sh
#   NO_RESTART=1 ./deploy/pi/sync.sh   # sync + install, don't touch systemd
#   DRY_RUN=1 ./deploy/pi/sync.sh      # show what would change
#
# Never touches the Pi's .env (holds the printer token) or its .venv.
set -euo pipefail

# ── Configuration (override via env vars) ─────────────────────────────────
# PLACEHOLDER: defaults match the existing task-printer Pi; change if needed.
PI_HOST="${PI_HOST:-clemmbn@raspzero.local}"
PI_PATH="${PI_PATH:-/home/clemmbn/Desktop/Printer/mail-printer}"
SERVICE_NAME="${SERVICE_NAME:-mail-printer-print-agent}"
NO_RESTART="${NO_RESTART:-0}"
DRY_RUN="${DRY_RUN:-0}"

# Repo root = two levels up, so the script works from any cwd.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SYNCIGNORE="$(dirname "${BASH_SOURCE[0]}")/.syncignore"

log() { printf '\033[1;35m[pi-sync]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[pi-sync] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v rsync >/dev/null || die "rsync not found locally"
[[ -f "$REPO_ROOT/pyproject.toml" ]] || die "not a mail-printer checkout: $REPO_ROOT"
[[ -f "$SYNCIGNORE" ]] || die "missing $SYNCIGNORE"

log "repo:    $REPO_ROOT"
log "target:  $PI_HOST:$PI_PATH"

# The Pi is on the LAN and mDNS (.local) can be flaky; fail fast with a
# useful message instead of a 2-minute rsync timeout.
log "checking SSH connectivity…"
ssh -o ConnectTimeout=10 -o BatchMode=yes "$PI_HOST" true \
  || die "cannot SSH to $PI_HOST (Pi awake? on the same network? key set up?)"

# ── 1. Sync ───────────────────────────────────────────────────────────────
# --delete mirrors deletions; .syncignore protects .env, data and caches.
# Note we sync the WHOLE workspace, not just packages/print-agent: uv needs
# the root pyproject.toml and uv.lock to resolve the workspace member.
RSYNC_ARGS=(
  --archive
  --compress
  --human-readable
  --delete
  --exclude-from="$SYNCIGNORE"
)
if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY_RUN=1 — showing changes only"
  RSYNC_ARGS+=(--dry-run --itemize-changes)
fi

log "syncing code…"
# Ensure the destination exists on a first run (rsync only creates the last
# path component, not the whole parent chain).
ssh "$PI_HOST" "mkdir -p '$PI_PATH'"
rsync "${RSYNC_ARGS[@]}" "$REPO_ROOT/" "$PI_HOST:$PI_PATH/"

if [[ "$DRY_RUN" == "1" ]]; then
  log "dry run complete — nothing installed or restarted"
  exit 0
fi

# ── 2. Install dependencies ───────────────────────────────────────────────
# --package mail-printer-print-agent installs ONLY the Pi member: the Zero 2W
# has 512 MB RAM and no business building fastapi/uvicorn. --frozen keeps it
# reproducible from uv.lock (and a resolve on a Pi Zero is slow).
log "installing dependencies with uv (this is slow on a Pi Zero)…"
ssh "$PI_HOST" "cd '$PI_PATH' && uv sync --frozen --package mail-printer-print-agent"

# ── 3. Restart ────────────────────────────────────────────────────────────
# Restarting drops the outbound WebSocket, which is harmless: the agent
# reconnects with backoff and the server re-sends every still-`queued` job.
if [[ "$NO_RESTART" == "1" ]]; then
  log "NO_RESTART=1 — skipping the service restart"
  exit 0
fi

log "restarting $SERVICE_NAME…"
ssh "$PI_HOST" "sudo systemctl restart '$SERVICE_NAME'"

log "waiting for the agent to settle…"
sleep 3
if ssh "$PI_HOST" "systemctl is-active --quiet '$SERVICE_NAME'"; then
  log "✅ $SERVICE_NAME is active"
  ssh "$PI_HOST" "journalctl -u '$SERVICE_NAME' -n 15 --no-pager"
else
  printf '\033[1;31m[pi-sync] ❌ %s failed to start — last 50 log lines:\033[0m\n' "$SERVICE_NAME" >&2
  ssh "$PI_HOST" "journalctl -u '$SERVICE_NAME' -n 50 --no-pager" >&2 || true
  exit 1
fi

log "done."
