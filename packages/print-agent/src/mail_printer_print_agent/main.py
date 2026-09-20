"""`mail-printer-print-agent` entrypoint: the shared printer agent running at home.

This is the only process that touches the USB/ESC-POS printer. It opens one
**outbound** WebSocket connection *per app* (mail-printer, later task-printer,
...) to that app's `wss://<domain>/ws/printer` endpoint, authenticating with
that app's own bearer token, and relays print jobs to the single physical
printer. See docs/decisions/0001-shared-print-agent.md.

Task structure (all in one asyncio loop):

    ┌─ app task "mailprinter" ─┐
    │  connect → hello → recv  │──┐
    └──────────────────────────┘  │   shared asyncio.Queue (FIFO)   ┌──────────────┐
    ┌─ app task "taskprinter" ─┐  ├──────────────────────────────►  │ printer task │
    │  connect → hello → recv  │──┘   (arrival order across apps)   └──────┬───────┘
    └──────────────────────────┘                                           │
                       ack / fail sent back on that job's own connection ◄─┘

- One job is in flight **overall**, not per app: the single printer worker
  task pops one job at a time from the shared queue, so jobs from different
  apps interleave in strict arrival (FIFO) order on the shared printer.
- Job ids are scoped per connection: a queued job carries the connection it
  arrived on, and its ack/fail goes back only to that connection. Ids from
  different apps therefore never collide.
- Each app task reconnects forever, independently, with exponential backoff
  plus jitter, so a server outage on one app never stalls the others.

Environment variables (Pi `.env`, loaded by systemd):

- ``LOG_LEVEL``                  DEBUG / INFO / WARNING / ERROR (default INFO)
- ``PRINTER_APPS``               optional comma-separated list of app names
                                 (default: every app discovered below)
- ``SERVER_WS_URL_<APP>``        that app's ``wss://…/ws/printer`` URL
- ``PRINTER_TOKEN_<APP>``        that app's shared secret (Bearer token)
- ``PRINTER_USB_VENDOR_ID``      printer USB vendor id, hex (default 0x0483)
- ``PRINTER_USB_PRODUCT_ID``     printer USB product id, hex (default 0x5743)
- ``PRINTER_PROFILE``            python-escpos profile (default TM-T20II)
- ``RECONNECT_MIN_SECONDS``      first backoff delay (default 1.0)
- ``RECONNECT_MAX_SECONDS``      backoff ceiling (default 60.0)

``<APP>`` is the app name upper-cased, e.g. ``SERVER_WS_URL_MAILPRINTER`` /
``PRINTER_TOKEN_MAILPRINTER``. Apps are discovered by scanning the environment
for ``SERVER_WS_URL_*``; for now only mail-printer needs to be configured. The
legacy single-app pair ``SERVER_WS_URL`` / ``PRINTER_TOKEN`` is still accepted
and registered as the app ``mailprinter``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mail_printer_print_agent.printer import (
    PrinterSettings,
    TicketPrinter,
    load_printer_settings,
)
from mail_printer_protocol.logs import setup_logging
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

logger = logging.getLogger(__name__)

# Env var prefixes used to discover per-app configuration.
URL_PREFIX = "SERVER_WS_URL_"
TOKEN_PREFIX = "PRINTER_TOKEN_"
# The app the legacy, un-suffixed SERVER_WS_URL / PRINTER_TOKEN pair maps to.
LEGACY_APP_NAME = "mailprinter"


@dataclass(frozen=True)
class AppConnection:
    """One app's WebSocket endpoint and credentials.

    Attributes:
        name (str): lowercase app name, used in every log line ("mailprinter").
        ws_url (str): that app's ``wss://…/ws/printer`` URL.
        token (str): that app's shared secret, sent as a Bearer token. Never
            logged.
    """

    name: str
    ws_url: str
    token: str


@dataclass(frozen=True)
class AgentSettings:
    """Full print-agent configuration (see module docstring for env vars).

    Attributes:
        apps (tuple[AppConnection, ...]): one entry per configured app.
        printer (PrinterSettings): USB ids + python-escpos profile.
        reconnect_min_seconds (float): first backoff delay after a drop.
        reconnect_max_seconds (float): backoff ceiling.
    """

    apps: tuple[AppConnection, ...]
    printer: PrinterSettings
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0


@dataclass(frozen=True)
class QueuedJob:
    """A print job waiting for the shared printer.

    Attributes:
        app (str): name of the app the job came from (for logging + routing).
        job (Print): the decoded print frame.
        websocket (Any): the connection it arrived on; the ack/fail goes back
            there and nowhere else, which is what scopes job ids per app.
    """

    app: str
    job: Print
    websocket: Any


def load_settings() -> AgentSettings:
    """Build `AgentSettings` from the environment.

    Returns:
        AgentSettings: the resolved configuration.

    Raises:
        ValueError: if no app is configured, if an app has a URL but no token
            (or vice versa), or if a USB id isn't valid hex.
    """
    apps = _discover_apps(os.environ)
    if not apps:
        raise ValueError(
            "no app configured: set SERVER_WS_URL_<APP> and PRINTER_TOKEN_<APP> "
            "(e.g. SERVER_WS_URL_MAILPRINTER / PRINTER_TOKEN_MAILPRINTER)"
        )
    return AgentSettings(
        apps=apps,
        printer=load_printer_settings(),
        reconnect_min_seconds=float(os.environ.get("RECONNECT_MIN_SECONDS", "1.0")),
        reconnect_max_seconds=float(os.environ.get("RECONNECT_MAX_SECONDS", "60.0")),
    )


def _discover_apps(env: Mapping[str, str]) -> tuple[AppConnection, ...]:
    """Find every configured app in the environment.

    Scans for ``SERVER_WS_URL_<APP>`` variables (plus the legacy un-suffixed
    pair) and pairs each with its ``PRINTER_TOKEN_<APP>``. Apps are returned
    sorted by name so startup logs are stable and tests are deterministic.

    Args:
        env (Mapping[str, str]): the environment to read (injectable for tests).

    Returns:
        tuple[AppConnection, ...]: configured apps, possibly empty.

    Raises:
        ValueError: if an app has a URL but no token, or a token but no URL.
    """
    urls: dict[str, str] = {}
    # Legacy single-app configuration, kept working so an existing Pi .env
    # doesn't break on upgrade; it simply becomes the "mailprinter" app.
    if env.get("SERVER_WS_URL"):
        urls[LEGACY_APP_NAME] = env["SERVER_WS_URL"]
    for key, value in env.items():
        if key.startswith(URL_PREFIX) and value:
            urls[key[len(URL_PREFIX) :].lower()] = value

    tokens: dict[str, str] = {}
    if env.get("PRINTER_TOKEN"):
        tokens[LEGACY_APP_NAME] = env["PRINTER_TOKEN"]
    for key, value in env.items():
        if key.startswith(TOKEN_PREFIX) and value:
            tokens[key[len(TOKEN_PREFIX) :].lower()] = value

    # A half-configured app would loop forever failing to authenticate, so
    # refuse to start instead.
    missing_tokens = sorted(urls.keys() - tokens.keys())
    missing_urls = sorted(tokens.keys() - urls.keys())
    if missing_tokens or missing_urls:
        raise ValueError(
            f"incomplete app config: missing PRINTER_TOKEN for {missing_tokens}, "
            f"missing SERVER_WS_URL for {missing_urls}"
        )

    selected = _selected_app_names(env, sorted(urls))
    return tuple(AppConnection(name, urls[name], tokens[name]) for name in selected)


def _selected_app_names(env: Mapping[str, str], discovered: list[str]) -> list[str]:
    """Apply the optional ``PRINTER_APPS`` allow-list to the discovered apps.

    Args:
        env (Mapping[str, str]): the environment to read.
        discovered (list[str]): sorted names found in the environment.

    Returns:
        list[str]: the names to actually connect to.

    Raises:
        ValueError: if ``PRINTER_APPS`` names an app that has no config.
    """
    raw = env.get("PRINTER_APPS", "").strip()
    if not raw:
        return discovered
    wanted = [name.strip().lower() for name in raw.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in discovered]
    if unknown:
        raise ValueError(f"PRINTER_APPS names unconfigured apps: {unknown}")
    return wanted


async def run_agent(
    settings: AgentSettings,
    printer: TicketPrinter,
    connect: Callable[..., Any] | None = None,
    stop: asyncio.Event | None = None,
    queue: asyncio.Queue[QueuedJob] | None = None,
) -> None:
    """Run the agent: one connection task per app plus one printer worker.

    Args:
        settings (AgentSettings): apps to connect to and backoff parameters.
        printer (TicketPrinter): the shared printer (tests inject a fake).
        connect (Callable | None): websockets connect function, called as
            ``connect(url, additional_headers=...)`` and used as an async
            context manager. Defaults to `websockets.asyncio.client.connect`.
        stop (asyncio.Event | None): set it to shut the agent down; if omitted
            the agent runs until cancelled (the systemd case).
        queue (asyncio.Queue | None): the shared FIFO job queue. Injectable so
            tests can observe what has been queued; production passes nothing.

    Returns:
        None

    Side effects:
        Opens outbound WebSocket connections and prints paper.
    """
    if connect is None:
        from websockets.asyncio.client import connect as ws_connect

        connect = ws_connect

    stop = stop or asyncio.Event()
    # Unbounded: the servers already rate-limit and only send one job at a
    # time per connection, so the queue stays tiny; bounding it would risk
    # blocking a receive loop (and its keepalive) behind a slow print.
    if queue is None:
        queue = asyncio.Queue()

    tasks = [asyncio.create_task(_printer_worker(queue, printer, stop), name="printer-worker")]
    tasks += [
        asyncio.create_task(_app_loop(app, settings, queue, connect, stop), name=f"app-{app.name}")
        for app in settings.apps
    ]
    logger.info(
        "print-agent running: %d app connection(s) [%s], protocol v%d",
        len(settings.apps),
        ", ".join(app.name for app in settings.apps),
        PROTOCOL_VERSION,
    )

    try:
        await stop.wait()
    finally:
        logger.info("print-agent shutting down")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _app_loop(
    app: AppConnection,
    settings: AgentSettings,
    queue: asyncio.Queue[QueuedJob],
    connect: Callable[..., Any],
    stop: asyncio.Event,
) -> None:
    """Keep one app's WebSocket connection alive forever.

    Reconnects with exponential backoff + jitter. The delay resets only after a
    connection that actually *stayed up* (longer than the minimum delay): a
    server that accepts and immediately closes — rejecting our token, say — is
    a failure, and resetting on it would turn the backoff into a hot loop.

    Args:
        app (AppConnection): which app (url + token + name).
        settings (AgentSettings): backoff bounds.
        queue (asyncio.Queue[QueuedJob]): the shared FIFO printer queue.
        connect (Callable): websockets connect function.
        stop (asyncio.Event): shutdown signal.

    Returns:
        None

    Side effects:
        Network I/O; logs every connect/disconnect for this app.
    """
    loop = asyncio.get_running_loop()
    delay = settings.reconnect_min_seconds
    while not stop.is_set():
        started = loop.time()
        try:
            await _run_connection(app, queue, connect)
            logger.warning("[%s] connection closed by server", app.name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let one app kill the agent
            logger.warning("[%s] connection error: %s: %s", app.name, type(exc).__name__, exc)

        if loop.time() - started >= settings.reconnect_min_seconds:
            delay = settings.reconnect_min_seconds

        if stop.is_set():
            return
        # Full jitter on top of the exponential delay: several apps dropped by
        # the same network blip must not reconnect in lockstep.
        wait = delay + random.uniform(0, delay)
        logger.info("[%s] reconnecting in %.1fs", app.name, wait)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=wait)
        delay = min(delay * 2, settings.reconnect_max_seconds)


async def _run_connection(
    app: AppConnection,
    queue: asyncio.Queue[QueuedJob],
    connect: Callable[..., Any],
) -> None:
    """Open one connection, say hello, then relay jobs until it closes.

    Args:
        app (AppConnection): which app to connect to.
        queue (asyncio.Queue[QueuedJob]): the shared FIFO printer queue.
        connect (Callable): websockets connect function.

    Returns:
        None: when the server closes the connection.

    Raises:
        Exception: any connection/handshake error, handled by `_app_loop`.

    Side effects:
        Enqueues every received `print` job.
    """
    headers = {"Authorization": f"Bearer {app.token}"}
    logger.info("[%s] connecting to %s", app.name, app.ws_url)
    async with connect(app.ws_url, additional_headers=headers) as websocket:
        await websocket.send(encode(Hello(protocol_version=PROTOCOL_VERSION)))
        logger.info("[%s] connected, hello sent (protocol v%d)", app.name, PROTOCOL_VERSION)
        async for frame in websocket:
            _handle_frame(app, frame, queue, websocket)
    logger.info("[%s] disconnected", app.name)


def _handle_frame(
    app: AppConnection,
    frame: str | bytes,
    queue: asyncio.Queue[QueuedJob],
    websocket: Any,
) -> None:
    """Decode one incoming frame and enqueue it if it is a print job.

    Malformed or unexpected frames are logged and dropped rather than raised:
    a single bad frame must not tear down a healthy connection.

    Args:
        app (AppConnection): the app this frame came from.
        frame (str | bytes): raw WebSocket payload.
        queue (asyncio.Queue[QueuedJob]): the shared FIFO printer queue.
        websocket (Any): the connection, remembered for the ack/fail reply.

    Returns:
        None

    Side effects:
        May put an item on the shared queue.
    """
    try:
        message = decode(frame)
    except ProtocolError as exc:
        logger.error("[%s] ignoring invalid frame: %s", app.name, exc)
        return

    if not isinstance(message, Print):
        logger.warning("[%s] ignoring unexpected %s frame", app.name, type(message).__name__)
        return

    # put_nowait on an unbounded queue is what guarantees strict arrival order
    # across apps: the job is queued in the exact order frames were received.
    queue.put_nowait(QueuedJob(app=app.name, job=message, websocket=websocket))
    logger.info("[%s] job %d received (%d queued)", app.name, message.job_id, queue.qsize())


async def _printer_worker(
    queue: asyncio.Queue[QueuedJob],
    printer: TicketPrinter,
    stop: asyncio.Event,
    to_thread: Callable[..., Awaitable[None]] = asyncio.to_thread,
) -> None:
    """Print queued jobs one at a time, FIFO, across all apps.

    This single task is what enforces "one job in flight overall": there is
    exactly one worker for the one physical printer.

    Args:
        queue (asyncio.Queue[QueuedJob]): the shared FIFO printer queue.
        printer (TicketPrinter): the shared printer.
        stop (asyncio.Event): shutdown signal (checked between jobs).
        to_thread (Callable): runs the blocking print off the event loop.

    Returns:
        None

    Side effects:
        Prints paper; sends ack/fail on each job's own connection.
    """
    while not stop.is_set():
        item = await queue.get()
        try:
            await _print_one(item, printer, to_thread)
        finally:
            queue.task_done()


async def _print_one(
    item: QueuedJob,
    printer: TicketPrinter,
    to_thread: Callable[..., Awaitable[None]],
) -> None:
    """Print one job and reply ack/fail on the connection it came from.

    Args:
        item (QueuedJob): app name, print frame and originating connection.
        printer (TicketPrinter): the shared printer.
        to_thread (Callable): runs the blocking print off the event loop.

    Returns:
        None

    Side effects:
        Prints paper; sends one frame back to the server.
    """
    job = item.job
    logger.info("[%s] printing job %d", item.app, job.job_id)
    try:
        # python-escpos blocks on USB; keep the event loop (and the WebSocket
        # keepalive pongs of every app) responsive while paper is moving.
        await to_thread(printer.print_job, job.png_b64, job.fallback_text)
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never fatal
        logger.exception("[%s] job %d FAILED", item.app, job.job_id)
        await _reply(item, Fail(job_id=job.job_id, error=f"{type(exc).__name__}: {exc}"))
        return

    logger.info("[%s] job %d printed", item.app, job.job_id)
    await _reply(item, Ack(job_id=job.job_id))


async def _reply(item: QueuedJob, message: Ack | Fail) -> None:
    """Send an ack/fail back on the job's own connection.

    Args:
        item (QueuedJob): the job being answered.
        message (Ack | Fail): the outcome frame.

    Returns:
        None

    Side effects:
        Network I/O. A send failure (the connection dropped while printing) is
        logged and swallowed: the server re-queues unacked jobs on reconnect,
        so there is nothing useful to do here.
    """
    try:
        await item.websocket.send(encode(message))
        logger.debug("[%s] sent %s for job %d", item.app, message.TYPE, message.job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[%s] could not send %s for job %d (connection gone): %s",
            item.app,
            message.TYPE,
            message.job_id,
            exc,
        )


def main() -> None:
    """Entrypoint: configure logging, load settings, run the agent forever.

    Returns:
        None

    Side effects:
        Runs an asyncio event loop until the process is stopped (Ctrl-C or
        systemd SIGTERM, both of which surface as KeyboardInterrupt/cancel).
    """
    setup_logging()
    settings = load_settings()
    logger.info(
        "mail-printer-print-agent starting (protocol v%d), printer %04x:%04x profile %s",
        PROTOCOL_VERSION,
        settings.printer.usb_vendor_id,
        settings.printer.usb_product_id,
        settings.printer.profile,
    )
    for app in settings.apps:
        logger.info("[%s] configured -> %s", app.name, app.ws_url)

    printer = TicketPrinter(settings.printer)
    try:
        asyncio.run(run_agent(settings, printer))
    except KeyboardInterrupt:
        logger.info("interrupted, exiting")


if __name__ == "__main__":
    main()
