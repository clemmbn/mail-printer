"""Shared logging setup for the server and the Pi agent.

Both entrypoints call ``setup_logging()`` once at startup so every log line
across both machines has the same format. The level comes from the
``LOG_LEVEL`` env var (default INFO). Stdlib only.
"""

from __future__ import annotations

import logging
import os

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str | None = None) -> None:
    """Configure the root logger with the project-wide format.

    Args:
        level (str | None): level name (e.g. "DEBUG"). Defaults to the
            ``LOG_LEVEL`` env var, then "INFO". Unknown names fall back to INFO.

    Side effects:
        Replaces any handlers already installed on the root logger
        (``force=True``), so calling it twice is harmless.
    """
    level_name = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    resolved = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    logging.basicConfig(level=resolved, format=LOG_FORMAT, datefmt=DATE_FORMAT, force=True)
    logging.getLogger(__name__).debug("logging configured at %s", logging.getLevelName(resolved))
