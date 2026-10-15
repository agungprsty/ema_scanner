import logging
from pathlib import Path

import httpx
import pytest

import src.main  # noqa: F401
from src.data_feed import binance_client
from src.services import telegram

pytestmark = pytest.mark.unit

FAKE_TOKEN = "123456789:AAFakeSecretTokenValue_xyz"


@pytest.fixture(autouse=True)
def telegram_credentials(monkeypatch):
    monkeypatch.setattr(telegram, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(telegram, "TELEGRAM_CHAT_ID", "42")


def _all_log_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


async def test_send_alert_exception_does_not_log_token(monkeypatch, caplog):
    async def failing_post(self, url, **kwargs):
        raise httpx.ConnectError(f"connection failed for {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", failing_post)

    with caplog.at_level(logging.DEBUG):
        ok = await telegram.send_alert("hello")

    assert ok is False
    text = _all_log_text(caplog)
    assert "Failed to send Telegram alert" in text
    assert "ConnectError" in text
    assert FAKE_TOKEN not in text


async def test_send_alert_error_response_does_not_log_token(monkeypatch, caplog):
    async def error_post(self, url, **kwargs):
        return httpx.Response(401, text=f"Unauthorized request to {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", error_post)

    with caplog.at_level(logging.DEBUG):
        ok = await telegram.send_alert("hello")

    assert ok is False
    text = _all_log_text(caplog)
    assert "Telegram API error 401" in text
    assert FAKE_TOKEN not in text


def test_httpx_loggers_silenced_below_warning():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


# Binance credentials stay out of the web process unless explicitly enabled

@pytest.fixture
def binance_secrets(monkeypatch):
    monkeypatch.setattr(binance_client, "BINANCE_API_KEY", "fake-api-key")
    monkeypatch.setattr(binance_client, "BINANCE_API_SECRET", "fake-api-secret")
    monkeypatch.setattr(binance_client, "BINANCE_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----fake")
    monkeypatch.setattr(binance_client, "BINANCE_PRIVATE_KEY_PASSPHRASE", "")


def test_binance_client_is_public_by_default(monkeypatch, binance_secrets):
    monkeypatch.setattr(binance_client, "BINANCE_AUTH_ENABLED", False)
    loaded = []
    monkeypatch.setattr(binance_client, "_load_private_key", lambda: loaded.append(True))
    kwargs = binance_client._client_kwargs()
    assert kwargs == {"timeout": 15}
    assert loaded == []


def test_binance_client_uses_rsa_key_only_when_enabled(monkeypatch, binance_secrets):
    monkeypatch.setattr(binance_client, "BINANCE_AUTH_ENABLED", True)
    kwargs = binance_client._client_kwargs()
    assert kwargs["key"] == "fake-api-key"
    assert kwargs["private_key"].startswith("-----BEGIN")
    assert "secret" not in kwargs


def test_binance_client_falls_back_to_hmac_when_enabled(monkeypatch, binance_secrets):
    monkeypatch.setattr(binance_client, "BINANCE_AUTH_ENABLED", True)
    monkeypatch.setattr(binance_client, "BINANCE_PRIVATE_KEY", None)
    monkeypatch.setattr(binance_client, "BINANCE_PRIVATE_KEY_PATH", None)
    kwargs = binance_client._client_kwargs()
    assert (kwargs["key"], kwargs["secret"]) == ("fake-api-key", "fake-api-secret")


def test_settings_do_not_read_binance_secrets_unless_enabled(monkeypatch):
    import importlib

    from src.config import settings

    monkeypatch.setenv("BINANCE_API_KEY", "env-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "env-secret")
    monkeypatch.setenv("BINANCE_PRIVATE_KEY_PATH", "/run/secrets/binance.pem")
    try:
        monkeypatch.setenv("BINANCE_AUTH_ENABLED", "false")
        importlib.reload(settings)
        assert (settings.BINANCE_API_KEY, settings.BINANCE_API_SECRET) == ("", "")
        assert settings.BINANCE_PRIVATE_KEY_PATH is None

        monkeypatch.setenv("BINANCE_AUTH_ENABLED", "true")
        importlib.reload(settings)
        assert (settings.BINANCE_API_KEY, settings.BINANCE_API_SECRET) == ("env-key", "env-secret")
        assert settings.BINANCE_PRIVATE_KEY_PATH == "/run/secrets/binance.pem"
    finally:
        monkeypatch.undo()
        importlib.reload(settings)


ROOT = Path(__file__).resolve().parent.parent


def test_nginx_frame_and_xss_headers_agree_with_the_csp():
    conf = (ROOT / "nginx.conf.example").read_text()
    assert "frame-ancestors 'none'" in conf
    assert "add_header X-Frame-Options DENY always;" in conf
    assert "SAMEORIGIN" not in conf
    assert 'add_header X-XSS-Protection "0" always;' in conf
    assert "mode=block" not in conf


def test_ci_checks_pull_requests_with_postgres_but_deploys_only_from_pushes():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    triggers = workflow.split("\njobs:")[0]
    assert "\n  pull_request:\n" in triggers and "\n  push:\n" in triggers
    jobs = workflow.split("\njobs:")[1]
    test_job = jobs.split("\n  backtest-gate:")[0]
    assert "image: postgres:16-alpine" in test_job
    assert "TEST_DATABASE_URL: postgresql+asyncpg://" in test_job
    for job in ("build-and-push", "deploy"):
        body = jobs.split(f"\n  {job}:")[1].split("\n    steps:")[0]
        assert "if: github.event_name != 'pull_request'" in body
