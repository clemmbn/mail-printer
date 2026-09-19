"""Tests for print-agent's env-based settings."""

import pytest

from mail_printer_print_agent.main import load_settings


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("SERVER_WS_URL", "wss://example.test/ws/printer")
    monkeypatch.setenv("PRINTER_TOKEN", "secret")
    for var in ("PRINTER_USB_VENDOR_ID", "PRINTER_USB_PRODUCT_ID", "PRINTER_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    settings = load_settings()
    assert settings.usb_vendor_id == 0x0483
    assert settings.usb_product_id == 0x5743
    assert settings.printer_profile == "TM-T20II"


def test_settings_require_url_and_token(monkeypatch):
    monkeypatch.delenv("SERVER_WS_URL", raising=False)
    monkeypatch.setenv("PRINTER_TOKEN", "secret")
    with pytest.raises(ValueError):
        load_settings()
