import logging

import httpx
import pytest

import src.main  # noqa: F401
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
