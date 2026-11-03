import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ADMIN_ID", "5940108626")

from telegram.error import BadRequest, InvalidToken, NetworkError

from bot import (
    TELEGRAM_STARTUP_ATTEMPTS,
    TELEGRAM_STARTUP_RETRY_DELAY,
    _initialize_telegram,
)


class TelegramStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_network_failure_then_initializes(self):
        initialize = AsyncMock(side_effect=[NetworkError("offline"), None])
        application = SimpleNamespace(bot=SimpleNamespace(initialize=initialize))

        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            initialized = await _initialize_telegram(application)

        self.assertTrue(initialized)
        self.assertEqual(initialize.await_count, 2)
        sleep.assert_awaited_once_with(TELEGRAM_STARTUP_RETRY_DELAY)

    async def test_stops_after_bounded_network_attempts(self):
        initialize = AsyncMock(side_effect=NetworkError("offline"))
        application = SimpleNamespace(bot=SimpleNamespace(initialize=initialize))

        with (
            patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep,
            self.assertLogs("bot", level="ERROR") as logs,
        ):
            initialized = await _initialize_telegram(application)

        self.assertFalse(initialized)
        self.assertEqual(initialize.await_count, TELEGRAM_STARTUP_ATTEMPTS)
        self.assertEqual(sleep.await_count, TELEGRAM_STARTUP_ATTEMPTS - 1)
        self.assertIn("Telegram server is unreachable", logs.output[-1])

    async def test_invalid_token_is_not_retried(self):
        initialize = AsyncMock(side_effect=InvalidToken("rejected"))
        application = SimpleNamespace(bot=SimpleNamespace(initialize=initialize))

        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            initialized = await _initialize_telegram(application)

        self.assertFalse(initialized)
        initialize.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_bad_request_is_not_treated_as_network_failure(self):
        initialize = AsyncMock(side_effect=BadRequest("invalid request"))
        application = SimpleNamespace(bot=SimpleNamespace(initialize=initialize))

        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(BadRequest):
                await _initialize_telegram(application)

        initialize.assert_awaited_once()
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
