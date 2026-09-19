"""`mail-printer-print-agent` entrypoint: the shared printer agent running at home.

Skeleton for now: loads and validates the configuration and logs it. The
WebSocket client loop (one outbound connection per app, hello handshake,
print/ack/fail, reconnect with exponential backoff) and ESC/POS printing are
implemented in CLE-136, per the decision in
docs/decisions/0001-shared-print-agent.md. This skeleton still assumes a
single app connection (mail-printer); CLE-136 generalises it to one
connection per app, each with its own token.

Configuration comes from env vars (the Pi `.env`, loaded by systemd); every
variable is listed in the root `.env.example`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from mail_printer_protocol.logs import setup_logging
from mail_printer_protocol.messages import PROTOCOL_VERSION

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSettings:
    """print-agent settings (single-app skeleton; see module docstring).

    Attributes:
        server_ws_url: wss:// URL of the server's printer endpoint.
        printer_token: shared secret sent as a Bearer token (never logged).
        usb_vendor_id: printer USB vendor id.
        usb_product_id: printer USB product id.
        printer_profile: python-escpos printer profile name.
    """

    server_ws_url: str
    printer_token: str
    usb_vendor_id: int
    usb_product_id: int
    printer_profile: str


def load_settings() -> AgentSettings:
    """Build `AgentSettings` from the environment.

    USB ids are hex strings (e.g. "0x0483"), same convention as task-printer.

    Returns:
        AgentSettings: the resolved configuration.

    Raises:
        ValueError: if a required variable is missing or a USB id isn't hex.
    """
    server_ws_url = os.environ.get("SERVER_WS_URL", "")
    printer_token = os.environ.get("PRINTER_TOKEN", "")
    # Fail fast at startup rather than looping forever on a doomed connection.
    if not server_ws_url or not printer_token:
        raise ValueError("SERVER_WS_URL and PRINTER_TOKEN must be set")
    return AgentSettings(
        server_ws_url=server_ws_url,
        printer_token=printer_token,
        usb_vendor_id=int(os.environ.get("PRINTER_USB_VENDOR_ID", "0x0483"), 16),
        usb_product_id=int(os.environ.get("PRINTER_USB_PRODUCT_ID", "0x5743"), 16),
        printer_profile=os.environ.get("PRINTER_PROFILE", "TM-T20II"),
    )


def main() -> None:
    """Entrypoint: configure logging, load settings, and (later) run the agent loop."""
    setup_logging()
    settings = load_settings()
    logger.info(
        "mail-printer-print-agent starting (protocol v%d) -> %s, printer %04x:%04x profile %s",
        PROTOCOL_VERSION,
        settings.server_ws_url,
        settings.usb_vendor_id,
        settings.usb_product_id,
        settings.printer_profile,
    )
    logger.warning("agent loop not implemented yet (CLE-136); exiting")


if __name__ == "__main__":
    main()
