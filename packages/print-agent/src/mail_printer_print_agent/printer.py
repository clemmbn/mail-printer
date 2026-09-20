"""ESC/POS printing for the shared print-agent (Raspberry Pi only).

This module is the *only* place that touches the USB thermal printer. It is
adapted (copied, not imported) from the sibling project
`../task-printer/src/task_printer/main.py`, keeping its three key behaviours:

- **Lazy USB connection**: the device is opened on the first print, not at
  import/startup, so the agent can boot, connect its WebSockets and log
  clearly even when the printer is unplugged or powered off.
- **Env-configured device**: USB vendor/product id and the python-escpos
  profile come from env vars (defaults: Epson TM-T20II), so a different
  printer never requires a code change.
- **Plain-text fallback**: if printing the rendered PNG fails for any reason
  (bad image bytes, printer quirk), we still print the job's `fallback_text`
  so a message is never silently lost.

Non-obvious constraints:
- `python-escpos` is fully synchronous and talks to libusb; every call here
  blocks. Callers in `main.py` must run `TicketPrinter.print_job` in a worker
  thread (`asyncio.to_thread`) so the WebSocket connections keep their
  keepalive pongs flowing while a ticket is physically printing.
- Only one job may be printed at a time across *all* apps: the printer is a
  single shared physical device. That serialisation is enforced by the single
  printer worker task in `main.py`, not here.
- `escpos` is imported lazily inside the connect helper so this module (and
  the tests) can be imported on a machine without libusb.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Receipt width in monospace characters, used to draw separator rules in the
# plain-text fallback. 32 is the standard for 58/80mm ESC/POS font A.
TEXT_RULE_WIDTH = 32


@dataclass(frozen=True)
class PrinterSettings:
    """USB/ESC-POS device configuration.

    Attributes:
        usb_vendor_id (int): USB vendor id of the printer (e.g. 0x0483).
        usb_product_id (int): USB product id of the printer (e.g. 0x5743).
        profile (str): python-escpos profile name (e.g. "TM-T20II"), which
            tells the library the printer's paper width and capabilities.
    """

    usb_vendor_id: int
    usb_product_id: int
    profile: str


def load_printer_settings() -> PrinterSettings:
    """Read the printer device configuration from the environment.

    USB ids are hex strings ("0x0483"), the same convention as task-printer.

    Returns:
        PrinterSettings: resolved device configuration, with Epson TM-T20II
        defaults when the variables are absent.

    Raises:
        ValueError: if a USB id is not a valid hex integer.
    """
    return PrinterSettings(
        usb_vendor_id=int(os.environ.get("PRINTER_USB_VENDOR_ID", "0x0483"), 16),
        usb_product_id=int(os.environ.get("PRINTER_USB_PRODUCT_ID", "0x5743"), 16),
        profile=os.environ.get("PRINTER_PROFILE", "TM-T20II"),
    )


class PrintError(RuntimeError):
    """Raised when a job could not be printed at all (image *and* text failed)."""


class TicketPrinter:
    """Prints ticket PNGs on the USB ESC/POS printer, with a text fallback.

    The device connection is created on first use and then cached: reopening
    the USB handle for every ticket is slow and occasionally fails while the
    printer is still busy with the previous cut.

    Attributes:
        settings (PrinterSettings): USB ids + profile of the device.
    """

    def __init__(
        self,
        settings: PrinterSettings,
        connect: Callable[[PrinterSettings], Any] | None = None,
    ) -> None:
        """Create a printer wrapper.

        Args:
            settings (PrinterSettings): USB ids and python-escpos profile.
            connect (Callable | None): factory returning a connected
                python-escpos device. Defaults to a real USB connection;
                tests inject a fake device factory so no hardware is needed.

        Side effects:
            None — no USB access happens until the first print.
        """
        self.settings = settings
        self._connect = connect or _connect_usb
        self._device: Any | None = None

    def device(self) -> Any:
        """Return the cached device, connecting on first call.

        Returns:
            Any: a python-escpos printer object (`escpos.printer.Usb`).

        Raises:
            Exception: whatever python-escpos/libusb raises when the printer
                is missing, busy or not permitted (surfaced to the caller so
                the job is nacked with a useful error message).

        Side effects:
            Opens and caches the USB device handle.
        """
        if self._device is not None:
            return self._device
        logger.info(
            "connecting to USB printer %04x:%04x (profile %s)",
            self.settings.usb_vendor_id,
            self.settings.usb_product_id,
            self.settings.profile,
        )
        self._device = self._connect(self.settings)
        logger.info("USB printer connected")
        return self._device

    def forget_device(self) -> None:
        """Drop the cached device so the next print reconnects.

        Called after a failure that is likely to have left the USB handle in a
        bad state (unplugged printer, libusb I/O error). Reconnecting is cheap
        compared to staying stuck on a dead handle forever.

        Side effects:
            Clears the cached device; the handle itself is left to be garbage
            collected (python-escpos closes it in ``__del__``).
        """
        self._device = None

    def print_job(self, png_b64: str, fallback_text: str) -> None:
        """Print one ticket: the PNG if possible, otherwise the plain text.

        Args:
            png_b64 (str): the rendered ticket PNG, base64-encoded.
            fallback_text (str): timestamp + name + message, printed instead
                if the image path fails for any reason.

        Returns:
            None

        Raises:
            PrintError: if both the image and the text fallback failed. The
                caller turns this into a `fail` frame for the server.

        Side effects:
            Prints paper and cuts it; may open the USB device.
        """
        try:
            self._print_image(png_b64)
            return
        except Exception as exc:  # noqa: BLE001 - any failure must fall back
            logger.exception("image printing failed, falling back to text")
            # Python clears the `except ... as` name at the end of the block,
            # so keep the error around for the combined PrintError message.
            image_error = exc
            # The USB handle may be wedged; force a reconnect for the fallback.
            self.forget_device()

        try:
            self._print_text(fallback_text)
            logger.info("printed text fallback (%d chars)", len(fallback_text))
        except Exception as text_error:  # noqa: BLE001 - reported back to the server
            logger.exception("text fallback also failed")
            raise PrintError(
                f"image printing failed ({image_error}); text fallback failed ({text_error})"
            ) from text_error

    def _print_image(self, png_b64: str) -> None:
        """Decode the base64 PNG and print it, followed by a cut.

        Args:
            png_b64 (str): base64-encoded PNG bytes.

        Returns:
            None

        Raises:
            ValueError: if the base64 payload is malformed.
            Exception: any Pillow/python-escpos error (propagated so
                `print_job` can fall back to text).

        Side effects:
            Prints and cuts paper.
        """
        png_bytes = _decode_png(png_b64)
        # Pillow is imported here rather than at module scope to keep the
        # import cost off the agent's startup path on a Pi Zero.
        from PIL import Image

        image = Image.open(io.BytesIO(png_bytes))
        image.load()
        logger.debug("decoded ticket image: %s %s", image.mode, image.size)

        device = self.device()
        device.set(align="center")
        device.image(image)
        device.text("\n")
        device.cut()
        logger.info("printed ticket image (%dx%d)", image.width, image.height)

    def _print_text(self, fallback_text: str) -> None:
        """Print the plain-text version of a ticket, followed by a cut.

        Args:
            fallback_text (str): the text to print, newlines included.

        Returns:
            None

        Raises:
            Exception: any python-escpos error.

        Side effects:
            Prints and cuts paper.
        """
        device = self.device()
        device.set(align="left")
        device.text("-" * TEXT_RULE_WIDTH + "\n")
        device.text(fallback_text.rstrip("\n") + "\n")
        device.text("-" * TEXT_RULE_WIDTH + "\n\n")
        device.cut()


def _decode_png(png_b64: str) -> bytes:
    """Decode a base64 PNG payload coming off the wire.

    Args:
        png_b64 (str): base64 text from a `print` frame.

    Returns:
        bytes: the raw PNG bytes.

    Raises:
        ValueError: if the payload is not valid base64. `validate=True` makes
            base64 reject stray characters instead of silently skipping them,
            which would otherwise produce a corrupt image and a confusing
            Pillow error much further down.
    """
    try:
        return base64.b64decode(png_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 PNG payload: {exc}") from exc


def _connect_usb(settings: PrinterSettings) -> Any:
    """Open the real USB ESC/POS device.

    Args:
        settings (PrinterSettings): USB ids and profile.

    Returns:
        Any: an `escpos.printer.Usb` instance.

    Raises:
        Exception: libusb/python-escpos errors when the device is absent or
            not accessible (missing udev rule, already claimed).
    """
    # Imported lazily: `python-escpos[usb]` needs libusb, which only exists on
    # the Pi. Keeping it out of module import lets tests run anywhere.
    from escpos.printer import Usb

    return Usb(settings.usb_vendor_id, settings.usb_product_id, profile=settings.profile)
