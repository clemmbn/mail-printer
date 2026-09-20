"""End-to-end tests for the agent loop against a fake in-process WS server.

A real `websockets` server is started on 127.0.0.1 on an ephemeral port and
plays the role of an app's `/ws/printer` endpoint, so the handshake, the
Authorization header, framing and reconnection are all exercised for real. The
printer is faked (`FakePrinter`), so no hardware is needed.

Tests are plain sync functions driving `asyncio.run` on an async scenario:
the package has no pytest-asyncio dependency and doesn't need one.
"""

import asyncio
import threading

import pytest
from websockets.asyncio.server import serve

from mail_printer_print_agent.main import AgentSettings, AppConnection, run_agent
from mail_printer_print_agent.printer import PrinterSettings
from mail_printer_protocol.messages import Ack, Fail, Hello, Print, decode, encode

PRINTER_SETTINGS = PrinterSettings(usb_vendor_id=0x0483, usb_product_id=0x5743, profile="TM-T20II")
# Tight bounds keep the reconnect test fast while still being observable.
FAST_RECONNECT = {"reconnect_min_seconds": 0.05, "reconnect_max_seconds": 0.2}
# Any await-loop in a test gives up after this, so a bug fails instead of hanging.
TIMEOUT = 5.0


class FakePrinter:
    """Stand-in for `TicketPrinter`, recording what it was asked to print.

    Attributes:
        printed (list[str]): `fallback_text` of each job, in print order — the
            printer is the only place that observes the true global ordering.
        fail_texts (set[str]): jobs whose fallback_text is in here raise,
            to exercise the `fail` path.
        gate (threading.Event | None): when set to an unset Event, `print_job`
            blocks on it; lets a test hold a job "in flight".
    """

    def __init__(self, fail_texts: set[str] | None = None) -> None:
        self.printed: list[str] = []
        self.fail_texts = fail_texts or set()
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def print_job(self, png_b64: str, fallback_text: str) -> None:
        """Record (and optionally block on / fail) one job. Runs in a worker thread."""
        self.started.set()
        if self.gate is not None:
            self.gate.wait(TIMEOUT)
        if fallback_text in self.fail_texts:
            raise RuntimeError(f"boom: {fallback_text}")
        self.printed.append(fallback_text)


class FakeServer:
    """A fake app server exposing `/ws/printer` on an ephemeral local port.

    Attributes:
        name (str): app name, only used to make assertions readable.
        hellos (list[Hello]): every handshake frame received.
        replies (list): every ack/fail frame received, in order.
        headers (list[str]): the Authorization header of each connection.
        connections (int): how many connections have been accepted.
        close_immediately (bool): close each connection right after `hello`,
            which is what drives the reconnect test.
    """

    def __init__(self, name: str, close_immediately: bool = False) -> None:
        self.name = name
        self.hellos: list[Hello] = []
        self.replies: list = []
        self.headers: list[str] = []
        self.connections = 0
        self.close_immediately = close_immediately
        self.connected = asyncio.Event()
        self._websocket = None
        self._server = None
        self.url = ""

    async def __aenter__(self) -> "FakeServer":
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/ws/printer"
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, websocket) -> None:
        """Accept one agent connection: read hello, then collect ack/fail frames."""
        self.connections += 1
        self.headers.append(websocket.request.headers.get("Authorization", ""))
        self.hellos.append(decode(await websocket.recv()))
        if self.close_immediately:
            await websocket.close()
            return
        self._websocket = websocket
        self.connected.set()
        async for frame in websocket:
            self.replies.append(decode(frame))

    async def send_job(self, job_id: int, text: str) -> None:
        """Send one `print` frame to the connected agent (waits for the connection)."""
        await asyncio.wait_for(self.connected.wait(), TIMEOUT)
        await self._websocket.send(encode(Print(job_id=job_id, png_b64="", fallback_text=text)))

    def app(self, token: str = "secret") -> AppConnection:
        """Build the `AppConnection` pointing at this fake server."""
        return AppConnection(name=self.name, ws_url=self.url, token=token)


async def wait_for(predicate, message: str) -> None:
    """Poll `predicate` until true, failing the test after TIMEOUT.

    Polling (rather than another Event per assertion) keeps the tests short;
    the loop yields so the agent's tasks actually make progress.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for: {message}")
        await asyncio.sleep(0.01)


def settings_for(*apps: AppConnection) -> AgentSettings:
    """Build AgentSettings for the given apps with fast reconnect bounds."""
    return AgentSettings(apps=tuple(apps), printer=PRINTER_SETTINGS, **FAST_RECONNECT)


async def start_agent(settings: AgentSettings, printer: FakePrinter, queue=None):
    """Start `run_agent` as a background task; returns (task, stop_event)."""
    stop = asyncio.Event()
    task = asyncio.create_task(run_agent(settings, printer, stop=stop, queue=queue))
    return task, stop


async def stop_agent(task: asyncio.Task, stop: asyncio.Event) -> None:
    """Signal shutdown and wait for the agent to unwind its tasks."""
    stop.set()
    await asyncio.wait_for(task, TIMEOUT)


def test_hello_handshake_and_ack():
    """A printed job is acked on the same connection, with the right bearer token."""

    async def scenario():
        async with FakeServer("mailprinter") as server:
            printer = FakePrinter()
            task, stop = await start_agent(settings_for(server.app("mail-secret")), printer)
            await server.send_job(7, "hello world")
            await wait_for(lambda: server.replies, "an ack")
            await stop_agent(task, stop)

            assert server.headers == ["Bearer mail-secret"]
            assert server.hellos[0] == Hello(protocol_version=1)
            assert printer.printed == ["hello world"]
            assert server.replies == [Ack(job_id=7)]

    asyncio.run(scenario())


def test_failed_print_is_nacked_with_an_error():
    """A printer exception becomes a `fail` frame naming the error, not a crash."""

    async def scenario():
        async with FakeServer("mailprinter") as server:
            printer = FakePrinter(fail_texts={"bad job"})
            task, stop = await start_agent(settings_for(server.app()), printer)
            await server.send_job(1, "bad job")
            await wait_for(lambda: server.replies, "a fail")
            # The agent must still be alive and able to print the next job.
            await server.send_job(2, "good job")
            await wait_for(lambda: len(server.replies) == 2, "the second reply")
            await stop_agent(task, stop)

            assert isinstance(server.replies[0], Fail)
            assert server.replies[0].job_id == 1
            assert "boom: bad job" in server.replies[0].error
            assert server.replies[1] == Ack(job_id=2)
            assert printer.printed == ["good job"]

    asyncio.run(scenario())


def test_reconnects_with_growing_backoff():
    """A server that drops the connection is retried, with a growing delay."""

    async def scenario():
        async with FakeServer("mailprinter", close_immediately=True) as server:
            printer = FakePrinter()
            task, stop = await start_agent(settings_for(server.app()), printer)
            started = asyncio.get_running_loop().time()
            await wait_for(lambda: server.connections >= 3, "three connection attempts")
            elapsed = asyncio.get_running_loop().time() - started
            await stop_agent(task, stop)

            # Backoff is min, then 2*min (plus jitter), so three attempts can't
            # happen faster than min + 2*min; this is a lower bound, immune to
            # the random jitter and to slow CI.
            assert elapsed >= 3 * FAST_RECONNECT["reconnect_min_seconds"]
            assert server.connections >= 3

    asyncio.run(scenario())


def test_jobs_from_two_apps_print_in_strict_arrival_order():
    """One job in flight overall: two apps interleave FIFO on the shared printer."""

    async def scenario():
        async with FakeServer("mailprinter") as mail, FakeServer("taskprinter") as tasks:
            printer = FakePrinter()
            # Hold the first job "on the paper" so the others must queue behind it.
            printer.gate = threading.Event()
            queue = asyncio.Queue()
            settings = settings_for(mail.app("mail-secret"), tasks.app("task-secret"))
            task, stop = await start_agent(settings, printer, queue=queue)

            await mail.send_job(1, "mail-1")
            await wait_for(printer.started.is_set, "the first job to start printing")

            # Queue the next two in a known order: wait for each to actually be
            # enqueued before sending the next, so arrival order is not a race.
            await tasks.send_job(10, "task-10")
            await wait_for(lambda: queue.qsize() == 1, "task-10 to be queued")
            await mail.send_job(2, "mail-2")
            await wait_for(lambda: queue.qsize() == 2, "mail-2 to be queued")

            printer.gate.set()
            await wait_for(lambda: len(printer.printed) == 3, "all three jobs to print")
            await wait_for(
                lambda: len(mail.replies) == 2 and len(tasks.replies) == 1, "all three replies"
            )
            await stop_agent(task, stop)

            # Strict arrival order across apps, not round-robin or per-app FIFO.
            assert printer.printed == ["mail-1", "task-10", "mail-2"]
            # Acks are scoped per connection: each app only ever sees its own ids.
            assert mail.replies == [Ack(job_id=1), Ack(job_id=2)]
            assert tasks.replies == [Ack(job_id=10)]

    asyncio.run(scenario())


def test_invalid_frame_does_not_kill_the_connection():
    """A malformed frame is logged and dropped; the next job still prints."""

    async def scenario():
        async with FakeServer("mailprinter") as server:
            printer = FakePrinter()
            task, stop = await start_agent(settings_for(server.app()), printer)
            await asyncio.wait_for(server.connected.wait(), TIMEOUT)
            await server._websocket.send("{not json")
            await server.send_job(3, "still works")
            await wait_for(lambda: server.replies, "an ack after the bad frame")
            await stop_agent(task, stop)

            assert server.replies == [Ack(job_id=3)]
            assert server.connections == 1

    asyncio.run(scenario())


if __name__ == "__main__":  # pragma: no cover - convenience only
    raise SystemExit(pytest.main([__file__]))
