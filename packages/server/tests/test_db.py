"""Tests for the SQLite schema and query helpers in `mail_printer_server.db`.

Every test opens a fresh temp-file DB (via the `conn` fixture) so tests never
share state and never touch the real `data/` directory.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mail_printer_server import db


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "app.db")
    db.init_db(connection)
    yield connection
    connection.close()


def insert_sample_message(conn: sqlite3.Connection, **overrides) -> int:
    fields = {
        "name": "Alice",
        "message": "hello there",
        "ip": "1.2.3.4",
    }
    fields.update(overrides)
    return db.insert_message(conn, **fields)


# --- schema / connection ---------------------------------------------------


def test_init_db_creates_expected_tables(conn: sqlite3.Connection):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {row["name"] for row in rows}
    assert {"messages", "bans", "rate_events"} <= table_names


def test_init_db_is_idempotent(conn: sqlite3.Connection):
    # Calling init_db again on an already-initialised connection must not raise.
    db.init_db(conn)


def test_connect_enables_wal_mode(tmp_path: Path):
    connection = db.connect(tmp_path / "wal.db")
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    connection.close()


# --- messages: insert / get / list -----------------------------------------


def test_insert_message_returns_id_and_defaults_to_queued(conn: sqlite3.Connection):
    message_id = insert_sample_message(conn)
    assert isinstance(message_id, int)

    message = db.get_message(conn, message_id)
    assert message is not None
    assert message.status == "queued"
    assert message.name == "Alice"
    assert message.message == "hello there"
    assert message.ip == "1.2.3.4"
    assert message.created_at
    assert message.photo_path is None
    assert message.ticket_path is None
    assert message.printed_at is None
    assert message.last_error is None


def test_get_message_returns_none_for_missing_id(conn: sqlite3.Connection):
    assert db.get_message(conn, 999) is None


def test_list_messages_returns_newest_first(conn: sqlite3.Connection):
    first_id = insert_sample_message(conn, message="first")
    second_id = insert_sample_message(conn, message="second")

    messages = db.list_messages(conn)
    assert [m.id for m in messages] == [second_id, first_id]


def test_list_messages_respects_limit_and_offset(conn: sqlite3.Connection):
    ids = [insert_sample_message(conn, message=f"msg {i}") for i in range(5)]

    page = db.list_messages(conn, limit=2, offset=1)
    assert [m.id for m in page] == list(reversed(ids))[1:3]


# --- queue helpers -----------------------------------------------------------


def test_next_queued_message_returns_none_when_empty(conn: sqlite3.Connection):
    assert db.next_queued_message(conn) is None


def test_next_queued_message_returns_oldest_first(conn: sqlite3.Connection):
    first_id = insert_sample_message(conn, message="first")
    insert_sample_message(conn, message="second")

    next_message = db.next_queued_message(conn)
    assert next_message is not None
    assert next_message.id == first_id


def test_next_queued_message_skips_non_queued(conn: sqlite3.Connection):
    first_id = insert_sample_message(conn, message="first")
    second_id = insert_sample_message(conn, message="second")
    db.mark_printed(conn, first_id)

    next_message = db.next_queued_message(conn)
    assert next_message is not None
    assert next_message.id == second_id


def test_mark_printed_sets_status_and_printed_at(conn: sqlite3.Connection):
    message_id = insert_sample_message(conn)
    db.mark_printed(conn, message_id)

    message = db.get_message(conn, message_id)
    assert message.status == "printed"
    assert message.printed_at is not None


def test_mark_failed_sets_status_and_error(conn: sqlite3.Connection):
    message_id = insert_sample_message(conn)
    db.mark_failed(conn, message_id, "printer offline")

    message = db.get_message(conn, message_id)
    assert message.status == "failed"
    assert message.last_error == "printer offline"


def test_requeue_message_resets_status_and_clears_fields(conn: sqlite3.Connection):
    message_id = insert_sample_message(conn)
    db.mark_failed(conn, message_id, "printer offline")

    db.requeue_message(conn, message_id)

    message = db.get_message(conn, message_id)
    assert message.status == "queued"
    assert message.last_error is None
    assert message.printed_at is None


# --- file storage paths + deletion ------------------------------------------


def test_photo_storage_path_is_under_data_dir(tmp_path: Path):
    path = db.photo_storage_path(tmp_path, 42)
    assert path == tmp_path / "photos" / "42.jpg"
    assert path.parent.is_dir()


def test_ticket_storage_path_is_under_data_dir(tmp_path: Path):
    path = db.ticket_storage_path(tmp_path, 42)
    assert path == tmp_path / "tickets" / "42.png"
    assert path.parent.is_dir()


def test_set_message_files_updates_paths(conn: sqlite3.Connection, tmp_path: Path):
    message_id = insert_sample_message(conn)
    photo_path = db.photo_storage_path(tmp_path, message_id)
    ticket_path = db.ticket_storage_path(tmp_path, message_id)

    db.set_message_files(conn, message_id, photo_path=photo_path, ticket_path=ticket_path)

    message = db.get_message(conn, message_id)
    assert message.photo_path == str(photo_path)
    assert message.ticket_path == str(ticket_path)


def test_delete_message_removes_row_and_files(conn: sqlite3.Connection, tmp_path: Path):
    message_id = insert_sample_message(conn)
    photo_path = db.photo_storage_path(tmp_path, message_id)
    ticket_path = db.ticket_storage_path(tmp_path, message_id)
    photo_path.write_bytes(b"fake-photo")
    ticket_path.write_bytes(b"fake-ticket")
    db.set_message_files(conn, message_id, photo_path=photo_path, ticket_path=ticket_path)

    db.delete_message(conn, message_id)

    assert db.get_message(conn, message_id) is None
    assert not photo_path.exists()
    assert not ticket_path.exists()


def test_delete_message_without_files_does_not_raise(conn: sqlite3.Connection):
    message_id = insert_sample_message(conn)
    db.delete_message(conn, message_id)
    assert db.get_message(conn, message_id) is None


# --- bans --------------------------------------------------------------------


def test_is_banned_false_by_default(conn: sqlite3.Connection):
    assert db.is_banned(conn, "9.9.9.9") is False


def test_ban_ip_then_is_banned_true(conn: sqlite3.Connection):
    db.ban_ip(conn, "9.9.9.9", reason="spam")
    assert db.is_banned(conn, "9.9.9.9") is True


# --- rate events ---------------------------------------------------------------


def test_record_rate_event_then_count_recent(conn: sqlite3.Connection):
    db.record_rate_event(conn, "5.6.7.8")
    db.record_rate_event(conn, "5.6.7.8")
    db.record_rate_event(conn, "1.1.1.1")

    assert db.count_recent_events(conn, "5.6.7.8", since="1970-01-01T00:00:00+00:00") == 2


def test_count_recent_events_excludes_events_before_cutoff(conn: sqlite3.Connection):
    db.record_rate_event(conn, "5.6.7.8")
    assert db.count_recent_events(conn, "5.6.7.8", since="2999-01-01T00:00:00+00:00") == 0


def test_prune_rate_events_removes_old_rows(conn: sqlite3.Connection):
    db.record_rate_event(conn, "5.6.7.8")

    db.prune_rate_events(conn, before="2999-01-01T00:00:00+00:00")

    assert db.count_recent_events(conn, "5.6.7.8", since="1970-01-01T00:00:00+00:00") == 0
