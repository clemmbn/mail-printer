"""SQLite schema and query helpers for mail-printer.

Responsibilities: open a WAL-mode connection, create the schema on startup,
and provide every query the rest of the server needs (messages, bans, rate
limiting) — stdlib `sqlite3` only, no ORM.

Non-obvious constraints:
- Only the server package touches this DB: the Pi never sees SQLite.
- `messages.id` is used to derive on-disk file names for the photo/ticket, so
  a photo/ticket path can only be computed *after* the row is inserted.
- Deleting a message must also delete its files, so history/admin cleanup
  never leaves orphaned images on disk.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PHOTOS_DIRNAME = "photos"
TICKETS_DIRNAME = "tickets"


@dataclass(frozen=True)
class Message:
    """A single submitted message, mirroring the `messages` row.

    Attributes:
        id: primary key.
        created_at: ISO-8601 UTC timestamp string, set at insert time.
        name: sender name ("Anonymous" is applied at render time, not here).
        message: the message body.
        photo_path: on-disk path to the sanitised photo, or None.
        ticket_path: on-disk path to the rendered ticket PNG, or None.
        ip: submitter IP, for rate limiting / bans.
        status: one of "queued", "printed", "failed".
        printed_at: ISO-8601 UTC timestamp string once printed, else None.
        last_error: error string from the most recent failed print attempt.
    """

    id: int
    created_at: str
    name: str
    message: str
    photo_path: str | None
    ticket_path: str | None
    ip: str
    status: str
    printed_at: str | None
    last_error: str | None


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string (sortable, timezone-aware)."""
    return datetime.now(UTC).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection to the SQLite DB, configured for concurrent access.

    Args:
        db_path: path to the SQLite file (created if missing).

    Returns:
        sqlite3.Connection: row_factory set to `sqlite3.Row`, WAL journal
            mode enabled so the printer WebSocket loop and HTTP requests
            (single process, but multiple coroutines) don't block each other
            on writes.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if it doesn't exist yet. Safe to call every startup.

    Side effects:
        Creates the `messages`, `bans`, and `rate_events` tables.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            photo_path TEXT,
            ticket_path TEXT,
            ip TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            printed_at TEXT,
            last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS bans (
            ip TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            reason TEXT
        );

        CREATE TABLE IF NOT EXISTS rate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_status_created
            ON messages (status, created_at);
        CREATE INDEX IF NOT EXISTS idx_rate_events_ip_created
            ON rate_events (ip, created_at);
        """
    )
    conn.commit()


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        created_at=row["created_at"],
        name=row["name"],
        message=row["message"],
        photo_path=row["photo_path"],
        ticket_path=row["ticket_path"],
        ip=row["ip"],
        status=row["status"],
        printed_at=row["printed_at"],
        last_error=row["last_error"],
    )


# --- messages ----------------------------------------------------------------


def insert_message(
    conn: sqlite3.Connection,
    *,
    name: str,
    message: str,
    ip: str,
) -> int:
    """Insert a new message row with status "queued".

    Photo/ticket paths are set later via `set_message_files`, once the row's
    id is known and the files have been written to disk.

    Returns:
        int: the new row's id.
    """
    cursor = conn.execute(
        """
        INSERT INTO messages (created_at, name, message, ip, status)
        VALUES (?, ?, ?, ?, 'queued')
        """,
        (_now(), name, message, ip),
    )
    conn.commit()
    return cursor.lastrowid


def get_message(conn: sqlite3.Connection, message_id: int) -> Message | None:
    """Fetch a single message by id, or None if it doesn't exist."""
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return _row_to_message(row) if row is not None else None


def list_messages(conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0) -> list[Message]:
    """List messages newest-first, for the admin history browser.

    Args:
        limit: max rows to return.
        offset: rows to skip (pagination).
    """
    rows = conn.execute(
        "SELECT * FROM messages ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


def set_message_files(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    photo_path: Path | None,
    ticket_path: Path | None,
) -> None:
    """Record where a message's photo/ticket were written on disk."""
    conn.execute(
        "UPDATE messages SET photo_path = ?, ticket_path = ? WHERE id = ?",
        (
            str(photo_path) if photo_path else None,
            str(ticket_path) if ticket_path else None,
            message_id,
        ),
    )
    conn.commit()


def delete_message(conn: sqlite3.Connection, message_id: int) -> None:
    """Delete a message row and its on-disk photo/ticket files, if any.

    Side effects:
        Removes files from disk. Missing files are ignored (already gone is
        not an error here).
    """
    message = get_message(conn, message_id)
    if message is not None:
        for path_str in (message.photo_path, message.ticket_path):
            if path_str:
                Path(path_str).unlink(missing_ok=True)
    conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    conn.commit()


# --- queue helpers -------------------------------------------------------------


def next_queued_message(conn: sqlite3.Connection) -> Message | None:
    """Return the oldest still-queued message, or None if the queue is empty."""
    row = conn.execute(
        "SELECT * FROM messages WHERE status = 'queued' ORDER BY created_at ASC, id ASC LIMIT 1"
    ).fetchone()
    return _row_to_message(row) if row is not None else None


def mark_printed(conn: sqlite3.Connection, message_id: int) -> None:
    """Mark a message as printed (acked by the Pi)."""
    conn.execute(
        "UPDATE messages SET status = 'printed', printed_at = ?, last_error = NULL WHERE id = ?",
        (_now(), message_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, message_id: int, error: str) -> None:
    """Mark a message as failed (nacked by the Pi, or timed out)."""
    conn.execute(
        "UPDATE messages SET status = 'failed', last_error = ? WHERE id = ?",
        (error, message_id),
    )
    conn.commit()


def requeue_message(conn: sqlite3.Connection, message_id: int) -> None:
    """Put a message back in the queue (used by admin "reprint")."""
    conn.execute(
        "UPDATE messages SET status = 'queued', printed_at = NULL, last_error = NULL WHERE id = ?",
        (message_id,),
    )
    conn.commit()


# --- file storage paths --------------------------------------------------------


def photo_storage_path(data_dir: Path, message_id: int) -> Path:
    """Return (and ensure the parent dir for) the on-disk path for a message's photo."""
    path = Path(data_dir) / PHOTOS_DIRNAME / f"{message_id}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def ticket_storage_path(data_dir: Path, message_id: int) -> Path:
    """Return (and ensure the parent dir for) the on-disk path for a message's rendered ticket."""
    path = Path(data_dir) / TICKETS_DIRNAME / f"{message_id}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# --- bans ------------------------------------------------------------------------


def ban_ip(conn: sqlite3.Connection, ip: str, *, reason: str | None = None) -> None:
    """Ban an IP, or replace an existing ban's reason/timestamp."""
    conn.execute(
        "INSERT OR REPLACE INTO bans (ip, created_at, reason) VALUES (?, ?, ?)",
        (ip, _now(), reason),
    )
    conn.commit()


def is_banned(conn: sqlite3.Connection, ip: str) -> bool:
    """Return whether an IP is currently banned."""
    row = conn.execute("SELECT 1 FROM bans WHERE ip = ?", (ip,)).fetchone()
    return row is not None


# --- rate events -------------------------------------------------------------------


def record_rate_event(conn: sqlite3.Connection, ip: str) -> None:
    """Record a submission attempt for an IP, used by the burst/daily rate limits."""
    conn.execute("INSERT INTO rate_events (ip, created_at) VALUES (?, ?)", (ip, _now()))
    conn.commit()


def count_recent_events(conn: sqlite3.Connection, ip: str, *, since: str) -> int:
    """Count an IP's rate events at or after `since` (an ISO-8601 timestamp string)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM rate_events WHERE ip = ? AND created_at >= ?",
        (ip, since),
    ).fetchone()
    return row[0]


def prune_rate_events(conn: sqlite3.Connection, *, before: str) -> None:
    """Delete rate events older than `before` (an ISO-8601 timestamp string).

    Called periodically so `rate_events` doesn't grow forever.
    """
    conn.execute("DELETE FROM rate_events WHERE created_at < ?", (before,))
    conn.commit()
