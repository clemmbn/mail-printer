"""The `/ws/printer` WebSocket endpoint and the print queue flush loop.

Purpose: this is the server side of the link with `print-agent` (the
Raspberry Pi process that owns the USB thermal printer). print-agent dials
*out* to this endpoint (`wss://<domain>/ws/printer`), authenticates with
mail-printer's own bearer token, and then receives print jobs one at a time.

Main responsibilities:
- Authenticate the incoming connection (`Authorization: Bearer <token>`,
  compared in constant time) and check the protocol version in the first
  `hello` frame.
- Keep at most ONE live print-agent connection for this app; a newer
  connection replaces (and closes) the older one, with a log line.
- Flush the queue: the DB *is* the queue, so on every connect — and every
  time something new is queued — the oldest `queued` message is sent, and
  the loop waits for an `ack` (-> `printed`) or `fail` (-> `failed`) before
  sending the next one. No ack/fail within `ack_timeout` seconds marks the
  job `failed` and moves on, so a wedged agent can never block the queue
  forever.
- Expose `is_connected()` (admin console status) and `reprint()`
  (admin "reprint as-is" = requeue + wake the flush loop).

Non-obvious constraints:
- **Connection state lives in process memory.** `PrinterHub` holds the live
  WebSocket and the "work available" event as plain attributes, so uvicorn
  MUST run with a single worker: with several workers each process would
  have its own hub and only one of them would actually own the agent
  connection (the others would report "not connected" and never flush).
- Every DB touch opens its own short-lived connection through a factory
  instead of sharing one `sqlite3.Connection`. Starlette runs sync routes in
  a threadpool and `sqlite3` connections are bound to the thread that
  created them (`check_same_thread`), so a shared connection would blow up
  as soon as the admin console and this loop lived on different threads.
  SQLite opens are cheap and the traffic here is a handful of rows per
  print, so the simplicity is worth more than the reuse.
- The queries here are synchronous and run inside the event loop. They are
  single-row, indexed operations on a local file, so the blocking time is
  negligible; moving them to a threadpool would buy nothing but complexity.
- Auth failures are rejected *after* `accept()` rather than before. Closing
  before accepting makes Starlette answer with a plain HTTP 403, which hides
  the reason from the agent (and from tests); accepting first lets us send a
  real close code + reason. Nothing is ever read from an unauthenticated
  socket.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import logging
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mail_printer_protocol.messages import (
    PROTOCOL_VERSION,
    Ack,
    Fail,
    Hello,
    Print,
    ProtocolError,
    decode,
    encode,
)
from mail_printer_server import db
from mail_printer_server.config import Settings, load_settings

logger = logging.getLogger(__name__)

router = APIRouter()

# WebSocket close codes used by this endpoint (1000-1015 are the RFC 6455
# codes; 1008 "policy violation" and 1002 "protocol error" are the closest
# standard fits, which keeps the agent side free of custom code tables).
CLOSE_UNAUTHORIZED = 1008
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_REPLACED = 1001

# A factory returning a *fresh* SQLite connection (see the module docstring).
ConnectionFactory = Callable[[], sqlite3.Connection]


class _AgentDisconnected(Exception):
    """Raised internally when the print-agent socket goes away mid-loop."""


class PrinterHub:
    """Holds the live print-agent connection and drives the queue flush loop.

    One instance per process (see `get_hub`). All state is in memory.

    Attributes:
        ack_timeout: seconds to wait for an `ack`/`fail` before marking the
            in-flight job `failed`.
    """

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        token: str,
        ack_timeout: float,
    ) -> None:
        """Build a hub.

        Args:
            connection_factory (ConnectionFactory): callable returning a new
                SQLite connection to the messages DB. Called per operation.
            token (str): the expected bearer token
                (`PRINTER_TOKEN_MAILPRINTER`). An empty token disables the
                endpoint entirely: every connection is rejected.
            ack_timeout (float): seconds before an unanswered job is failed.
        """
        self._connection_factory = connection_factory
        self._token = token
        self.ack_timeout = ack_timeout
        # The currently connected agent, or None. Replaced (not stacked) when
        # a new agent connects.
        self._websocket: WebSocket | None = None
        # Set whenever new work may exist; the idle flush loop waits on it.
        self._work_available = asyncio.Event()
        # The loop the endpoint runs on, captured so `notify_queued` can be
        # called safely from a threadpool (sync) route.
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- public API (admin console / submit route) --------------------------

    def is_connected(self) -> bool:
        """Return whether a print-agent is currently connected to this app."""
        return self._websocket is not None

    def notify_queued(self) -> None:
        """Wake the flush loop because a message was just queued.

        Safe to call from any thread: Starlette runs sync routes in a
        threadpool, and `asyncio.Event.set` is not thread-safe, so the set is
        scheduled on the endpoint's own loop when we are not already on it.

        Side effects:
            Wakes the idle flush loop, which then drains the queue.
        """
        loop = self._loop
        if loop is None:
            logger.debug("notify_queued: no print-agent loop running, nothing to wake")
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._work_available.set()
            return
        loop.call_soon_threadsafe(self._work_available.set)

    def reprint(self, message_id: int) -> None:
        """Requeue a message and ask the flush loop to send it again.

        This is what the admin console's "reprint as-is" calls: the stored
        ticket PNG is re-sent untouched, never re-rendered.

        Args:
            message_id (int): id of the message to print again.

        Side effects:
            Sets the row back to `queued` and wakes the flush loop.
        """
        with self._connection() as conn:
            db.requeue_message(conn, message_id)
        logger.info("reprint requested for message %d (requeued)", message_id)
        self.notify_queued()

    # --- endpoint -----------------------------------------------------------

    async def handle(self, websocket: WebSocket) -> None:
        """Serve one print-agent WebSocket connection from accept to close.

        Args:
            websocket (WebSocket): the incoming connection.

        Side effects:
            Accepts or closes the socket, updates message rows, and holds the
            connection in `self._websocket` for as long as it lives.
        """
        await websocket.accept()

        if not self._authorized(websocket):
            logger.warning("print-agent connection rejected: bad or missing bearer token")
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="unauthorized")
            return

        if not await self._handshake(websocket):
            return

        await self._replace_existing_connection(websocket)
        self._websocket = websocket
        self._loop = asyncio.get_running_loop()
        # A fresh connection always means "check the queue now".
        self._work_available.set()
        logger.info("print-agent connected (protocol v%d)", PROTOCOL_VERSION)

        try:
            await self._flush_loop(websocket)
        except (_AgentDisconnected, WebSocketDisconnect):
            logger.info("print-agent disconnected")
        finally:
            # Only clear the slot if we still own it: a replacement connection
            # may already have taken over while we were unwinding.
            if self._websocket is websocket:
                self._websocket = None
                logger.info("print-agent connection closed, no agent connected")

    # --- handshake ----------------------------------------------------------

    def _authorized(self, websocket: WebSocket) -> bool:
        """Constant-time check of the `Authorization: Bearer <token>` header.

        Args:
            websocket (WebSocket): the incoming connection.

        Returns:
            bool: True only when the header carries exactly the configured
                token. An unconfigured (empty) token always returns False.
        """
        if not self._token:
            logger.error("PRINTER_TOKEN_MAILPRINTER is not set: refusing every agent connection")
            return False
        header = websocket.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        # compare_digest on str requires ASCII-only inputs; encode to be safe
        # with an arbitrary attacker-supplied header value.
        return hmac.compare_digest(presented.strip().encode(), self._token.encode())

    async def _handshake(self, websocket: WebSocket) -> bool:
        """Read and validate the mandatory first `hello` frame.

        Args:
            websocket (WebSocket): an accepted, authenticated connection.

        Returns:
            bool: True if the agent speaks our protocol version. On False the
                socket has already been closed with an explanatory code.
        """
        try:
            frame = await websocket.receive_text()
        except WebSocketDisconnect:
            logger.warning("print-agent disconnected before sending hello")
            return False

        try:
            message = decode(frame)
        except ProtocolError as exc:
            logger.warning("print-agent sent an undecodable first frame: %s", exc)
            await websocket.close(code=CLOSE_PROTOCOL_ERROR, reason="invalid hello frame")
            return False

        if not isinstance(message, Hello):
            logger.warning("print-agent first frame was %r, expected hello", type(message).__name__)
            await websocket.close(code=CLOSE_PROTOCOL_ERROR, reason="expected hello frame")
            return False

        if message.protocol_version != PROTOCOL_VERSION:
            logger.warning(
                "print-agent protocol version mismatch: agent v%s, server v%d",
                message.protocol_version,
                PROTOCOL_VERSION,
            )
            await websocket.close(
                code=CLOSE_PROTOCOL_ERROR,
                reason=f"protocol version mismatch: server speaks v{PROTOCOL_VERSION}",
            )
            return False

        return True

    async def _replace_existing_connection(self, new_websocket: WebSocket) -> None:
        """Close any previous agent connection so only one is ever live.

        Args:
            new_websocket (WebSocket): the connection taking over.

        Side effects:
            Closes the old socket; its `handle` coroutine then unwinds.
        """
        previous = self._websocket
        if previous is None or previous is new_websocket:
            return
        logger.warning("a new print-agent connected: replacing the previous connection")
        # Detach first so the old loop's `finally` doesn't clear the new slot.
        self._websocket = None
        with contextlib.suppress(Exception):
            await previous.close(code=CLOSE_REPLACED, reason="replaced by a newer connection")

    # --- flush loop ---------------------------------------------------------

    async def _flush_loop(self, websocket: WebSocket) -> None:
        """Drain the queue oldest-first, one job in flight, until disconnect.

        Args:
            websocket (WebSocket): the live agent connection.

        Raises:
            _AgentDisconnected: when the agent goes away (normal exit path).
        """
        inbox: asyncio.Queue[Ack | Fail | None] = asyncio.Queue()
        receiver = asyncio.create_task(self._receive_loop(websocket, inbox))
        try:
            while True:
                message = self._next_queued()
                if message is None:
                    # Nothing to do: sleep until something is queued (or the
                    # agent disconnects, which ends the receiver task).
                    await self._wait_for_work(receiver)
                    continue
                await self._run_job(websocket, message, inbox)
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver

    async def _receive_loop(
        self, websocket: WebSocket, inbox: asyncio.Queue[Ack | Fail | None]
    ) -> None:
        """Read agent frames forever and push ack/fail onto `inbox`.

        Running the reads in their own task (rather than reading inline after
        each `print`) is what lets the flush loop notice a disconnect while it
        is idle *and* while it is waiting for an ack, with one code path.

        Args:
            websocket (WebSocket): the live agent connection.
            inbox (asyncio.Queue): receives each Ack/Fail, then a single
                `None` sentinel when the socket closes.

        Side effects:
            Puts items on `inbox`. Never raises out of the task.
        """
        try:
            while True:
                frame = await websocket.receive_text()
                try:
                    message = decode(frame)
                except ProtocolError as exc:
                    logger.warning("ignoring malformed frame from print-agent: %s", exc)
                    continue
                if isinstance(message, Ack | Fail):
                    await inbox.put(message)
                    continue
                logger.warning(
                    "ignoring unexpected %s frame from print-agent", type(message).__name__
                )
        except WebSocketDisconnect:
            logger.info("print-agent socket closed while reading")
        except RuntimeError as exc:
            # Starlette raises RuntimeError when reading a socket that was
            # closed from our side (e.g. this connection was replaced).
            logger.info("print-agent socket no longer readable: %s", exc)
        finally:
            await inbox.put(None)

    async def _wait_for_work(self, receiver: asyncio.Task[None]) -> None:
        """Block until new work is signalled or the agent disconnects.

        Args:
            receiver (asyncio.Task): the receive loop task; if it finishes,
                the socket is gone.

        Raises:
            _AgentDisconnected: if the receive loop ended first.
        """
        waiter = asyncio.ensure_future(self._work_available.wait())
        done, _ = await asyncio.wait({waiter, receiver}, return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done:
            waiter.cancel()
            raise _AgentDisconnected
        self._work_available.clear()

    async def _run_job(
        self,
        websocket: WebSocket,
        message: db.Message,
        inbox: asyncio.Queue[Ack | Fail | None],
    ) -> None:
        """Send one print job and settle it from the agent's answer.

        Args:
            websocket (WebSocket): the live agent connection.
            message (db.Message): the queued message to print.
            inbox (asyncio.Queue): where the receive loop posts ack/fail.

        Side effects:
            Sends a `print` frame and marks the row `printed` or `failed`.

        Raises:
            _AgentDisconnected: if the socket closes before the job settles
                (the row is left `queued` so it flushes on reconnect).
        """
        job = Print(
            job_id=message.id,
            png_b64=_ticket_png_b64(message),
            fallback_text=_fallback_text(message),
        )
        logger.info("sending job %d to print-agent (%d b64 chars)", job.job_id, len(job.png_b64))
        try:
            await websocket.send_text(encode(job))
        except (WebSocketDisconnect, RuntimeError) as exc:
            logger.warning("could not send job %d, agent gone: %s", job.job_id, exc)
            raise _AgentDisconnected from exc

        answer = await self._await_answer(job.job_id, inbox)

        if answer is None:
            error = f"no ack within {self.ack_timeout:.0f}s"
            logger.error("job %d timed out: %s", job.job_id, error)
            self._mark_failed(job.job_id, error)
            return

        if isinstance(answer, Ack):
            logger.info("job %d acked by print-agent: printed", job.job_id)
            self._mark_printed(job.job_id)
            return

        logger.error("job %d failed on print-agent: %s", job.job_id, answer.error)
        self._mark_failed(job.job_id, answer.error)

    async def _await_answer(
        self, job_id: int, inbox: asyncio.Queue[Ack | Fail | None]
    ) -> Ack | Fail | None:
        """Wait for the ack/fail matching `job_id`, within `ack_timeout`.

        Answers for other job ids are logged and skipped (they can only be
        late frames from a previous, replaced connection), and they do not
        extend the deadline.

        Args:
            job_id (int): the in-flight job.
            inbox (asyncio.Queue): where the receive loop posts ack/fail.

        Returns:
            Ack | Fail | None: the matching answer, or None on timeout.

        Raises:
            _AgentDisconnected: if the receive loop posted its sentinel.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ack_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                answer = await asyncio.wait_for(inbox.get(), timeout=remaining)
            except TimeoutError:
                return None
            if answer is None:
                raise _AgentDisconnected
            if answer.job_id != job_id:
                logger.warning(
                    "ignoring %s for job %d while job %d is in flight",
                    type(answer).__name__,
                    answer.job_id,
                    job_id,
                )
                continue
            return answer

    # --- DB helpers ---------------------------------------------------------

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a short-lived SQLite connection and always close it."""
        conn = self._connection_factory()
        try:
            yield conn
        finally:
            conn.close()

    def _next_queued(self) -> db.Message | None:
        """Return the oldest `queued` message, or None if the queue is empty."""
        with self._connection() as conn:
            return db.next_queued_message(conn)

    def _mark_printed(self, message_id: int) -> None:
        """Mark a message `printed` in the DB."""
        with self._connection() as conn:
            db.mark_printed(conn, message_id)

    def _mark_failed(self, message_id: int, error: str) -> None:
        """Mark a message `failed` in the DB, recording `error`."""
        with self._connection() as conn:
            db.mark_failed(conn, message_id, error)


# --- ticket / fallback payloads --------------------------------------------


def _ticket_png_b64(message: db.Message) -> str:
    """Base64-encode a message's stored ticket PNG.

    The stored PNG is sent as-is (never re-rendered), so reprints of old
    messages keep their original design.

    Args:
        message (db.Message): the message being printed.

    Returns:
        str: base64 of the ticket file, or "" when the file is missing or
            unreadable — the agent then prints `fallback_text` instead.
    """
    if not message.ticket_path:
        logger.warning("message %d has no ticket PNG: sending text fallback only", message.id)
        return ""
    try:
        return base64.b64encode(Path(message.ticket_path).read_bytes()).decode("ascii")
    except OSError as exc:
        logger.error(
            "message %d ticket unreadable (%s): sending text fallback only", message.id, exc
        )
        return ""


def _fallback_text(message: db.Message) -> str:
    """Build the plain-text version printed if image printing fails.

    Args:
        message (db.Message): the message being printed.

    Returns:
        str: timestamp + sender name + body, as specified by the protocol.
    """
    name = message.name.strip() or "Anonymous"
    return f"{message.created_at}\n{name}\n\n{message.message}"


# --- process-wide hub -------------------------------------------------------

# Single in-memory hub for this process (see the module docstring on why a
# single uvicorn worker is mandatory). Built lazily on first use so importing
# this module never touches the environment or the filesystem — which also
# lets tests install their own hub first.
_hub: PrinterHub | None = None


def build_hub(settings: Settings) -> PrinterHub:
    """Create a hub wired to the DB and secrets described by `settings`.

    Args:
        settings (Settings): server configuration.

    Returns:
        PrinterHub: a new, unconnected hub.
    """
    db_path = settings.data_dir / "app.db"
    return PrinterHub(
        lambda: db.connect(db_path),
        token=settings.printer_token,
        ack_timeout=settings.printer_ack_timeout,
    )


def get_hub() -> PrinterHub:
    """Return the process-wide hub, creating it from the environment if needed."""
    global _hub
    if _hub is None:
        _hub = build_hub(load_settings())
        logger.info("printer hub created (ack timeout: %.0fs)", _hub.ack_timeout)
    return _hub


def set_hub(hub: PrinterHub | None) -> None:
    """Install (or clear, with None) the process-wide hub. Used by tests."""
    global _hub
    _hub = hub


def is_connected() -> bool:
    """Return whether a print-agent is connected (for the admin console)."""
    return get_hub().is_connected()


def notify_queued() -> None:
    """Tell the flush loop that a new message was queued (submit route)."""
    get_hub().notify_queued()


def reprint(message_id: int) -> None:
    """Requeue a message and flush it (admin "reprint as-is")."""
    get_hub().reprint(message_id)


@router.websocket("/ws/printer")
async def printer_websocket(websocket: WebSocket) -> None:
    """The print-agent endpoint: one authenticated connection, one queue."""
    await get_hub().handle(websocket)
