"""FastAPI app factory and the `mail-printer-server` entrypoint.

Responsibilities: build the app (router wiring will grow as features land)
and run it under uvicorn.

Constraint: uvicorn must run with a SINGLE worker, because the live Pi
WebSocket connection is held in process memory; several workers would each
see a different (or no) printer connection.
"""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from mail_printer_protocol.logs import setup_logging
from mail_printer_server import db
from mail_printer_server.config import load_settings

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create the FastAPI application.

    Returns:
        FastAPI: the app with all routes registered.
    """
    app = FastAPI(title="mail-printer", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness probe for Caddy / monitoring."""
        return {"status": "ok"}

    logger.info("app created")
    return app


def main() -> None:
    """Entrypoint: configure logging, load settings, and serve with uvicorn.

    Side effects:
        Creates the data directory if missing; blocks serving HTTP.
    """
    setup_logging()
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Schema creation is idempotent, so this is safe to run on every startup.
    connection = db.connect(settings.data_dir / "app.db")
    db.init_db(connection)
    connection.close()
    logger.info(
        "starting mail-printer-server on %s:%d (data dir: %s)",
        settings.host,
        settings.port,
        settings.data_dir.resolve(),
    )
    # log_config=None keeps our root logging format instead of uvicorn's own.
    uvicorn.run(create_app(), host=settings.host, port=settings.port, workers=1, log_config=None)


if __name__ == "__main__":
    main()
