"""Tests for the ESC/POS layer, with a fake python-escpos device.

No printer and no libusb are involved: `TicketPrinter` takes a `connect`
factory, so these tests inject a recording fake. What matters here is the
behaviour the Pi depends on: lazy connection, image printing + cut, and the
plain-text fallback when the image path blows up.
"""

import base64
import io

import pytest
from PIL import Image

from mail_printer_print_agent.printer import PrintError, PrinterSettings, TicketPrinter

SETTINGS = PrinterSettings(usb_vendor_id=0x0483, usb_product_id=0x5743, profile="TM-T20II")


class FakeDevice:
    """Records the python-escpos calls the agent makes.

    Attributes:
        calls (list[tuple]): every call, in order, as (method, arg).
        fail_on_image (bool): make `image()` raise, to exercise the fallback.
        fail_on_text (bool): make `text()` raise, to exercise the hard failure.
    """

    def __init__(self, fail_on_image: bool = False, fail_on_text: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail_on_image = fail_on_image
        self.fail_on_text = fail_on_text

    def set(self, **kwargs):
        self.calls.append(("set", kwargs))

    def image(self, img):
        if self.fail_on_image:
            raise RuntimeError("usb write error")
        self.calls.append(("image", img.size))

    def text(self, value):
        if self.fail_on_text:
            raise RuntimeError("usb write error")
        self.calls.append(("text", value))

    def cut(self):
        self.calls.append(("cut", None))

    def methods(self) -> list[str]:
        """Return just the method names, in call order."""
        return [name for name, _ in self.calls]


def png_b64(width: int = 8, height: int = 4) -> str:
    """Build a tiny valid PNG and return it base64-encoded, as the wire carries it."""
    buffer = io.BytesIO()
    Image.new("L", (width, height), color=255).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def make_printer(device: FakeDevice) -> TicketPrinter:
    """Wrap a fake device in a TicketPrinter."""
    return TicketPrinter(SETTINGS, connect=lambda settings: device)


def test_connection_is_lazy():
    connects = []
    printer = TicketPrinter(SETTINGS, connect=lambda s: connects.append(s) or FakeDevice())
    assert connects == []
    printer.print_job(png_b64(), "fallback")
    assert connects == [SETTINGS]


def test_device_is_cached_between_jobs():
    calls = []
    printer = TicketPrinter(SETTINGS, connect=lambda s: calls.append(s) or FakeDevice())
    printer.print_job(png_b64(), "a")
    printer.print_job(png_b64(), "b")
    assert len(calls) == 1


def test_prints_image_then_cuts():
    device = FakeDevice()
    make_printer(device).print_job(png_b64(16, 9), "fallback")
    assert device.methods() == ["set", "image", "text", "cut"]
    assert ("image", (16, 9)) in device.calls


def test_falls_back_to_text_when_image_fails():
    device = FakeDevice(fail_on_image=True)
    make_printer(device).print_job(png_b64(), "12/05/2026 10:00\nAlice\nhello")
    # No image call recorded; the text rules + body + cut are.
    assert "image" not in device.methods()
    # "set" twice: the aborted image attempt, then the text fallback.
    assert device.methods() == ["set", "set", "text", "text", "text", "cut"]
    assert any("hello" in value for name, value in device.calls if name == "text")


def test_falls_back_to_text_when_png_is_invalid():
    device = FakeDevice()
    make_printer(device).print_job("not base64 !!", "fallback body")
    assert "image" not in device.methods()
    assert any("fallback body" in value for name, value in device.calls if name == "text")


def test_raises_print_error_when_both_paths_fail():
    device = FakeDevice(fail_on_image=True, fail_on_text=True)
    with pytest.raises(PrintError) as excinfo:
        make_printer(device).print_job(png_b64(), "fallback")
    # The message must name both failures so the server's `fail` frame is useful.
    assert "image printing failed" in str(excinfo.value)
    assert "text fallback failed" in str(excinfo.value)


def test_failed_image_forces_a_reconnect():
    devices = [FakeDevice(fail_on_image=True), FakeDevice()]
    printer = TicketPrinter(SETTINGS, connect=lambda s: devices.pop(0))
    printer.print_job(png_b64(), "fallback")
    # The wedged handle is dropped, so the text fallback ran on a fresh device.
    assert devices == []
