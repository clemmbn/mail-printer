"""Tests for the /ws/printer endpoint and the queue flush loop.

Each test installs its own `PrinterHub` (pointing at a temp SQLite DB and a
temp data dir) as the process-wide hub, so nothing touches the real `data/`
directory and no state leaks between tests.

Note on TestClient: `client.websocket_connect` runs the app in a background
thread with its own event loop. DB assertions therefore have to *poll* (see
`wait_for_status`) rather than read once, because the flush loop settles a
job slightly after the test sends its ack/fail.
"""

from __future__ import annotations

import base64
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mail_printer_protocol.messages import PROTOCOL_VERSION, Ack, Fail, Hello, decode, encode
from mail_printer_server import db, printer_ws
from mail_printer_server.main import create_app

TOKEN = "test-printer-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Short enough to keep the timeout test fast, long enough that a loaded CI
# machine doesn't fail jobs the test meant to ack in time.
FAST_ACK_TIMEOUT = 0.3


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A temp stand-in for the runtime `data/` directory."""
    return tmp_path


@pytest.fixture
def conn(data_dir: Path) -> sqlite3.Connection:
    """A connection to the temp DB, for the test's own inserts/assertions."""
    connection = db.connect(data_dir / "app.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def hub(conn: sqlite3.Connection, data_dir: Path) -> printer_ws.PrinterHub:
    """Install a hub wired to the temp DB, and clear it again afterwards."""
    db_path = data_dir / "app.db"
    instance = printer_ws.PrinterHub(
        lambda: db.connect(db_path),
        token=TOKEN,
        ack_timeout=FAST_ACK_TIMEOUT,
    )
    printer_ws.set_hub(instance)
    yield instance
    printer_ws.set_hub(None)


@pytest.fixture
def client(hub: printer_ws.PrinterHub) -> TestClient:
    """A TestClient over the real app factory (so the router wiring is tested)."""
    return TestClient(create_app())


def queue_message(conn: sqlite3.Connection, data_dir: Path, *, body: str, png: bytes) -> int:
    """Insert a queued message with a stored ticket PNG on disk.

    Args:
        conn: the test DB connection.
        data_dir: temp data dir the ticket is written under.
        body: the message text.
        png: fake ticket bytes (the server only base64s them).

    Returns:
        int: the new message id.
    """
    message_id = db.insert_message(conn, name="Alice", message=body, ip="1.2.3.4")
    ticket_path = db.ticket_storage_path(data_dir, message_id)
    ticket_path.write_bytes(png)
    db.set_message_files(conn, message_id, photo_path=None, ticket_path=ticket_path)
    return message_id


def wait_for_status(conn: sqlite3.Connection, message_id: int, expected: str, timeout: float = 5.0):
    """Poll the DB until a message reaches `expected`, then return the row.

    Polling (rather than a single read) is required because the flush loop
    runs on the TestClient's background event loop.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = db.get_message(conn, message_id)
        if message is not None and message.status == expected:
            return message
        time.sleep(0.02)
    actual = db.get_message(conn, message_id)
    raise AssertionError(f"message {message_id} is {actual.status!r}, expected {expected!r}")


def connect_agent(client: TestClient):
    """Open an authenticated websocket and complete the hello handshake."""
    websocket = client.websocket_connect("/ws/printer", headers=AUTH).__enter__()
    websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
    return websocket


# --- auth / handshake -------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-token"}, {"Authorization": TOKEN}],
    ids=["missing", "wrong-token", "no-bearer-scheme"],
)
def test_connection_without_valid_token_is_closed(client: TestClient, headers: dict):
    with client.websocket_connect("/ws/printer", headers=headers) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()
    assert exc_info.value.code == printer_ws.CLOSE_UNAUTHORIZED


def test_unconfigured_token_refuses_every_connection(client: TestClient, hub):
    hub._token = ""
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()
    assert exc_info.value.code == printer_ws.CLOSE_UNAUTHORIZED


def test_protocol_version_mismatch_is_rejected(client: TestClient):
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION + 1)))
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()
    assert exc_info.value.code == printer_ws.CLOSE_PROTOCOL_ERROR


def test_first_frame_must_be_hello(client: TestClient):
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Ack(job_id=1)))
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()
    assert exc_info.value.code == printer_ws.CLOSE_PROTOCOL_ERROR


# --- connection state -------------------------------------------------------


def test_is_connected_tracks_the_agent(client: TestClient, hub, conn, data_dir):
    assert printer_ws.is_connected() is False
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        # Queue something and wait for it: proves the handshake completed
        # before we assert on the connection flag.
        message_id = queue_message(conn, data_dir, body="hi", png=b"png")
        hub.notify_queued()
        assert decode(websocket.receive_text()).job_id == message_id
        assert printer_ws.is_connected() is True
    # The server-side close is processed asynchronously.
    deadline = time.monotonic() + 5
    while printer_ws.is_connected() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert printer_ws.is_connected() is False


def test_new_connection_replaces_the_previous_one(client: TestClient, hub, conn, data_dir):
    first = connect_agent(client)
    queue_message(conn, data_dir, body="first", png=b"png")
    hub.notify_queued()
    first.receive_text()  # first agent is live and holding the job

    with client.websocket_connect("/ws/printer", headers=AUTH) as second:
        second.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        # The replacement flushes the still-queued job itself.
        job = decode(second.receive_text())
        second.send_text(encode(Ack(job_id=job.job_id)))
        assert wait_for_status(conn, job.job_id, "printed")

    with pytest.raises(WebSocketDisconnect):
        first.receive_text()


# --- flush / ack / fail -----------------------------------------------------


def test_flush_sends_queued_messages_oldest_first(client: TestClient, conn, data_dir):
    ids = [queue_message(conn, data_dir, body=f"msg {n}", png=f"png{n}".encode()) for n in range(3)]

    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        received = []
        for expected_id in ids:
            job = decode(websocket.receive_text())
            received.append(job.job_id)
            assert base64.b64decode(job.png_b64) == f"png{ids.index(expected_id)}".encode()
            assert "msg" in job.fallback_text
            assert "Alice" in job.fallback_text
            websocket.send_text(encode(Ack(job_id=job.job_id)))
    assert received == ids
    for message_id in ids:
        assert wait_for_status(conn, message_id, "printed").printed_at is not None


def test_ack_marks_the_message_printed(client: TestClient, conn, data_dir):
    message_id = queue_message(conn, data_dir, body="print me", png=b"png")
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        job = decode(websocket.receive_text())
        websocket.send_text(encode(Ack(job_id=job.job_id)))
        message = wait_for_status(conn, message_id, "printed")
    assert message.last_error is None


def test_fail_marks_the_message_failed(client: TestClient, conn, data_dir):
    message_id = queue_message(conn, data_dir, body="boom", png=b"png")
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        job = decode(websocket.receive_text())
        websocket.send_text(encode(Fail(job_id=job.job_id, error="out of paper")))
        message = wait_for_status(conn, message_id, "failed")
    assert message.last_error == "out of paper"


def test_missing_ack_times_out_and_fails_the_job(client: TestClient, conn, data_dir):
    message_id = queue_message(conn, data_dir, body="silence", png=b"png")
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        websocket.receive_text()  # job sent, agent stays silent on purpose
        message = wait_for_status(conn, message_id, "failed")
    assert "no ack" in message.last_error


def test_timed_out_job_does_not_block_the_next_one(client: TestClient, conn, data_dir):
    first = queue_message(conn, data_dir, body="silence", png=b"png1")
    second = queue_message(conn, data_dir, body="next", png=b"png2")
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        assert decode(websocket.receive_text()).job_id == first
        job = decode(websocket.receive_text())
        assert job.job_id == second
        websocket.send_text(encode(Ack(job_id=second)))
        wait_for_status(conn, second, "printed")
    assert wait_for_status(conn, first, "failed")


# --- offline queueing / reprint ---------------------------------------------


def test_messages_queued_while_offline_flush_on_reconnect(client: TestClient, conn, data_dir):
    # No agent connected: submissions keep landing in the DB as `queued`.
    offline_ids = [queue_message(conn, data_dir, body=f"offline {n}", png=b"png") for n in range(2)]
    assert printer_ws.is_connected() is False

    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        for expected_id in offline_ids:
            job = decode(websocket.receive_text())
            assert job.job_id == expected_id
            websocket.send_text(encode(Ack(job_id=job.job_id)))
    for message_id in offline_ids:
        assert wait_for_status(conn, message_id, "printed")


def test_notify_queued_wakes_an_idle_agent(client: TestClient, hub, conn, data_dir):
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        # Queue *after* the agent connected and went idle.
        message_id = queue_message(conn, data_dir, body="fresh", png=b"png")
        printer_ws.notify_queued()
        job = decode(websocket.receive_text())
        assert job.job_id == message_id
        websocket.send_text(encode(Ack(job_id=job.job_id)))
        assert wait_for_status(conn, message_id, "printed")


def test_reprint_requeues_and_flushes_the_stored_ticket(client: TestClient, conn, data_dir):
    message_id = queue_message(conn, data_dir, body="again", png=b"stored-png")
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        job = decode(websocket.receive_text())
        websocket.send_text(encode(Ack(job_id=job.job_id)))
        wait_for_status(conn, message_id, "printed")

        printer_ws.reprint(message_id)
        again = decode(websocket.receive_text())
        assert again.job_id == message_id
        # Reprint sends the stored PNG untouched, never a re-render.
        assert base64.b64decode(again.png_b64) == b"stored-png"
        websocket.send_text(encode(Ack(job_id=message_id)))
        assert wait_for_status(conn, message_id, "printed")


def test_job_without_a_stored_ticket_still_sends_fallback_text(client: TestClient, conn):
    message_id = db.insert_message(conn, name="", message="no ticket", ip="1.2.3.4")
    with client.websocket_connect("/ws/printer", headers=AUTH) as websocket:
        websocket.send_text(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        job = decode(websocket.receive_text())
        assert job.job_id == message_id
        assert job.png_b64 == ""
        assert "Anonymous" in job.fallback_text
        websocket.send_text(encode(Ack(job_id=job.job_id)))
        assert wait_for_status(conn, message_id, "printed")
