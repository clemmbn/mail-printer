#!/usr/bin/env bash
#
# backup.sh — nightly backup of mail-printer's runtime data on the VPS.
#
# Purpose
#   data/ is the only irreplaceable thing on this box: the SQLite DB
#   (messages, bans, rate limits) plus the photos/ and tickets/ images. The
#   code is in git; the secrets are in a password manager. So this script
#   backs up data/ and nothing else.
#
# Runs ON the VPS (not from a laptop), driven by the systemd timer in
# mail-printer-backup.timer. Safe to run by hand at any time.
#
# Usage
#   ./backup.sh                     # uses the defaults below
#   BACKUP_DIR=/mnt/backups ./backup.sh
#
# Why SQLite's .backup and not `cp app.db`
#   The server is running while this executes. Copying the file with cp can
#   capture a torn state (a write in flight, or a WAL that does not match the
#   main DB) and produce a backup that only fails when you try to restore it.
#   `sqlite3 .backup` uses the online backup API: it takes a consistent
#   snapshot of a live database, retrying around concurrent writers.
set -euo pipefail

# ── Configuration (override via env vars) ─────────────────────────────────
# PLACEHOLDER: adjust if the app does not live at /srv/mail-printer.
APP_DIR="${APP_DIR:-/srv/mail-printer}"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/mail-printer}"
# How many nightly archives to keep. 14 ≈ two weeks, a few hundred MB at
# most for a personal message printer — cheap insurance against "I deleted
# the wrong thing last Tuesday".
KEEP_DAYS="${KEEP_DAYS:-14}"

DB_PATH="$DATA_DIR/app.db"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$BACKUP_DIR/mail-printer-$STAMP.tar.gz"

log() { printf '[backup] %s\n' "$*"; }
die() { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }

command -v sqlite3 >/dev/null || die "sqlite3 not installed (sudo apt install sqlite3)"
[[ -d "$DATA_DIR" ]] || die "data dir not found: $DATA_DIR"

mkdir -p "$BACKUP_DIR"

# ── 1. Consistent DB snapshot in a scratch dir ────────────────────────────
# mktemp -d gives a private dir; the trap guarantees it is removed even if
# the script dies half-way, so a failed backup never leaves a stray copy of
# the database (which contains IPs and message text) lying around.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

if [[ -f "$DB_PATH" ]]; then
  log "snapshotting SQLite DB…"
  # .backup writes a single self-contained file, WAL already folded in — so
  # the archive does not need app.db-wal / app.db-shm to be restorable.
  sqlite3 "$DB_PATH" ".backup '$STAGE/app.db'"
  # Cheap integrity assertion: if the snapshot is corrupt we want to know
  # now, not on the day we need to restore it.
  result="$(sqlite3 "$STAGE/app.db" 'PRAGMA integrity_check;')"
  [[ "$result" == "ok" ]] || die "integrity check failed on the snapshot: $result"
  log "DB snapshot OK"
else
  log "no DB at $DB_PATH yet — backing up images only"
fi

# ── 2. Archive the snapshot + the images ──────────────────────────────────
# The images are immutable once written (a ticket PNG is never rewritten),
# so they can be tarred straight from the live directory without a snapshot.
# -C changes into each source dir so the archive has clean relative paths.
log "creating $ARCHIVE…"
TAR_ARGS=()
[[ -f "$STAGE/app.db" ]] && TAR_ARGS+=(-C "$STAGE" app.db)
for sub in photos tickets; do
  [[ -d "$DATA_DIR/$sub" ]] && TAR_ARGS+=(-C "$DATA_DIR" "$sub")
done
[[ ${#TAR_ARGS[@]} -gt 0 ]] || die "nothing to back up"

tar -czf "$ARCHIVE" "${TAR_ARGS[@]}"
# The archive holds message text, photos and IP addresses: owner-only.
chmod 600 "$ARCHIVE"
log "wrote $(du -h "$ARCHIVE" | cut -f1) to $ARCHIVE"

# ── 3. Rotate ─────────────────────────────────────────────────────────────
# Delete archives older than KEEP_DAYS. -mtime is good enough here (one
# archive per night); a count-based rotation would be fiddlier for no gain.
log "pruning backups older than $KEEP_DAYS days…"
find "$BACKUP_DIR" -maxdepth 1 -name 'mail-printer-*.tar.gz' -mtime "+$KEEP_DAYS" -print -delete

log "remaining: $(find "$BACKUP_DIR" -maxdepth 1 -name 'mail-printer-*.tar.gz' | wc -l) archive(s)"
log "done."

# ── Off-box copy (recommended, not automated here) ────────────────────────
# A backup on the same disk as the data only protects against "oops I
# deleted it", not against losing the VPS. Pull the archives down from your
# laptop periodically, e.g.:
#   rsync -av mailprinter@mail.example.com:/var/backups/mail-printer/ ~/Backups/mail-printer/
