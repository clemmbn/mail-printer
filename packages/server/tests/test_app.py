"""Smoke tests for the server app factory and settings."""

from pathlib import Path

from fastapi.testclient import TestClient

from mail_printer_server.config import load_settings
from mail_printer_server.main import create_app


def test_healthz():
    response = TestClient(create_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("PORT", "9001")
    monkeypatch.setenv("DATA_DIR", "/tmp/mp-data")
    settings = load_settings()
    assert settings.port == 9001
    assert settings.data_dir == Path("/tmp/mp-data")
