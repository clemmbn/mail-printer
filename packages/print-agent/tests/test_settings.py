"""Tests for print-agent's env-based, per-app settings discovery.

Covers the multi-app discovery rules documented in
`mail_printer_print_agent.main`: one `SERVER_WS_URL_<APP>` /
`PRINTER_TOKEN_<APP>` pair per app, the legacy single-app pair, the optional
`PRINTER_APPS` allow-list, and the printer device defaults.
"""

import pytest

from mail_printer_print_agent.main import _discover_apps, load_settings
from mail_printer_print_agent.printer import load_printer_settings

# Every variable the loader looks at, cleared before each test so a real
# developer .env in the environment can't make these tests pass or fail.
ENV_VARS = (
    "SERVER_WS_URL",
    "PRINTER_TOKEN",
    "SERVER_WS_URL_MAILPRINTER",
    "PRINTER_TOKEN_MAILPRINTER",
    "SERVER_WS_URL_TASKPRINTER",
    "PRINTER_TOKEN_TASKPRINTER",
    "PRINTER_APPS",
    "PRINTER_USB_VENDOR_ID",
    "PRINTER_USB_PRODUCT_ID",
    "PRINTER_PROFILE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove every print-agent env var before each test."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_discovers_one_app_per_suffixed_pair():
    apps = _discover_apps(
        {
            "SERVER_WS_URL_MAILPRINTER": "wss://mail.test/ws/printer",
            "PRINTER_TOKEN_MAILPRINTER": "mail-secret",
            "SERVER_WS_URL_TASKPRINTER": "wss://task.test/ws/printer",
            "PRINTER_TOKEN_TASKPRINTER": "task-secret",
        }
    )
    assert [app.name for app in apps] == ["mailprinter", "taskprinter"]
    assert apps[0].ws_url == "wss://mail.test/ws/printer"
    assert apps[1].token == "task-secret"


def test_legacy_unsuffixed_pair_becomes_mailprinter():
    apps = _discover_apps({"SERVER_WS_URL": "wss://mail.test/ws/printer", "PRINTER_TOKEN": "s"})
    assert [app.name for app in apps] == ["mailprinter"]


def test_half_configured_app_is_rejected():
    with pytest.raises(ValueError, match="incomplete app config"):
        _discover_apps({"SERVER_WS_URL_MAILPRINTER": "wss://mail.test/ws/printer"})


def test_printer_apps_selects_a_subset():
    env = {
        "SERVER_WS_URL_MAILPRINTER": "wss://mail.test/ws/printer",
        "PRINTER_TOKEN_MAILPRINTER": "a",
        "SERVER_WS_URL_TASKPRINTER": "wss://task.test/ws/printer",
        "PRINTER_TOKEN_TASKPRINTER": "b",
        "PRINTER_APPS": "taskprinter",
    }
    assert [app.name for app in _discover_apps(env)] == ["taskprinter"]


def test_printer_apps_rejects_unknown_app():
    env = {
        "SERVER_WS_URL_MAILPRINTER": "wss://mail.test/ws/printer",
        "PRINTER_TOKEN_MAILPRINTER": "a",
        "PRINTER_APPS": "nope",
    }
    with pytest.raises(ValueError, match="unconfigured apps"):
        _discover_apps(env)


def test_load_settings_requires_at_least_one_app():
    with pytest.raises(ValueError, match="no app configured"):
        load_settings()


def test_load_settings_defaults(monkeypatch):
    monkeypatch.setenv("SERVER_WS_URL_MAILPRINTER", "wss://mail.test/ws/printer")
    monkeypatch.setenv("PRINTER_TOKEN_MAILPRINTER", "secret")
    settings = load_settings()
    assert settings.printer.usb_vendor_id == 0x0483
    assert settings.printer.usb_product_id == 0x5743
    assert settings.printer.profile == "TM-T20II"
    assert settings.reconnect_min_seconds == 1.0
    assert settings.reconnect_max_seconds == 60.0


def test_printer_settings_from_env(monkeypatch):
    monkeypatch.setenv("PRINTER_USB_VENDOR_ID", "0x04b8")
    monkeypatch.setenv("PRINTER_USB_PRODUCT_ID", "0x0e15")
    monkeypatch.setenv("PRINTER_PROFILE", "TM-T88III")
    printer = load_printer_settings()
    assert (printer.usb_vendor_id, printer.usb_product_id) == (0x04B8, 0x0E15)
    assert printer.profile == "TM-T88III"
