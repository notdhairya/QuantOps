"""
utils/telegram_notifier.py

Small Telegram notification helper.

Configuration is read from environment variables through config.py:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

The notifier is intentionally quiet: it sends startup/shutdown and trade
events, and responds to /status when command polling is enabled.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from collections.abc import Callable

import config
from utils.logger import get_logger

logger = get_logger(__name__)


class TelegramNotifier:
    def __init__(self) -> None:
        self._offset = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._status_provider: Callable[[], str] | None = None

    @property
    def enabled(self) -> bool:
        return bool(
            config.TELEGRAM_ENABLED
            and config.TELEGRAM_BOT_TOKEN
            and config.TELEGRAM_CHAT_ID
        )

    def send_message(self, text: str) -> bool:
        """Send one Telegram message. Returns False if disabled or failed."""
        if not self.enabled:
            return False

        data = urllib.parse.urlencode(
            {
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"

        try:
            with urllib.request.urlopen(url, data=data, timeout=config.TELEGRAM_TIMEOUT_SECONDS) as response:
                return 200 <= response.status < 300
        except HTTPError as e:
            logger.warning("Telegram send failed: %s | %s", e, _read_error_body(e))
            return False
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)
            return False

    def start_command_listener(self, status_provider: Callable[[], str]) -> None:
        """Start a background listener for /status messages."""
        if not self.enabled or not config.TELEGRAM_STATUS_COMMANDS:
            return
        if self._running:
            self._status_provider = status_provider
            return

        self._status_provider = status_provider
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, name="telegram-status", daemon=True)
        self._thread.start()

    def stop_command_listener(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._poll_once()
            except HTTPError as e:
                logger.warning("Telegram status polling failed: %s | %s", e, _read_error_body(e))
            except Exception as e:
                logger.warning("Telegram status polling failed: %s", e)
            time.sleep(config.TELEGRAM_POLL_INTERVAL_SECONDS)

    def _poll_once(self) -> None:
        params = urllib.parse.urlencode(
            {
                "timeout": 1,
                "offset": self._offset,
                "allowed_updates": json.dumps(["message"]),
            }
        )
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates?{params}"

        with urllib.request.urlopen(url, timeout=config.TELEGRAM_TIMEOUT_SECONDS + 2) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if not payload.get("ok"):
            return

        for update in payload.get("result", []):
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            text = str(message.get("text", "")).strip().lower()

            if chat_id != str(config.TELEGRAM_CHAT_ID):
                continue

            if text in {"/status", "status"}:
                status = self._status_provider() if self._status_provider else "Bot status unavailable."
                self.send_message(status)


telegram_notifier = TelegramNotifier()


def _read_error_body(error: HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
