"""Server configuration loaded from environment variables.

All settings (and, later, secrets) come from the environment: in production
systemd loads them from the VPS `.env` (EnvironmentFile); locally use
`uv run --env-file .env mail-printer-server`. Every variable is listed in
the root `.env.example`.

Only the settings needed by the skeleton live here for now; the secrets
(Turnstile, admin, session, printer token) are added by the features that
use them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Server settings.

    Attributes:
        host: interface uvicorn binds to (Caddy proxies to it, so localhost).
        port: port uvicorn listens on.
        data_dir: runtime data directory (SQLite DB, photos, tickets).
        printer_token: shared secret print-agent presents as
            `Authorization: Bearer <token>` on /ws/printer. Empty means
            "not configured", and every agent connection is refused.
        printer_ack_timeout: seconds to wait for a print-agent ack/fail
            before marking the in-flight job failed and moving on, so one
            wedged print never blocks the whole queue.
    """

    host: str
    port: int
    data_dir: Path
    printer_token: str = ""
    printer_ack_timeout: float = 60.0


def load_settings() -> Settings:
    """Build `Settings` from the environment, applying defaults.

    Returns:
        Settings: the resolved configuration.

    Raises:
        ValueError: if `PORT` or `PRINTER_ACK_TIMEOUT` is not a number.
    """
    return Settings(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        data_dir=Path(os.environ.get("DATA_DIR", "data")),
        # Named per app (see docs/decisions/0001-shared-print-agent.md): the
        # Pi holds one connection per app, each with its own token.
        printer_token=os.environ.get("PRINTER_TOKEN_MAILPRINTER", ""),
        printer_ack_timeout=float(os.environ.get("PRINTER_ACK_TIMEOUT", "60")),
    )
