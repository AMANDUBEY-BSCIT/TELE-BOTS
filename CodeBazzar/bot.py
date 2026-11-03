import asyncio
import logging
import traceback

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import (
    BadRequest,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.request import HTTPXRequest

import config
from database import initialize_database, is_user_blocked, record_user
from handlers.admin import admin_command, handle_admin_callback, handle_admin_input, is_admin
from handlers.callback_guard import guard_callback
from handlers.customer_ui import handle_back, show_screen
from handlers.orders import handle_orders_callback
from handlers.payments import handle_payment_callback, handle_utr_input
from handlers.products import handle_custom_quantity, handle_product_callback
from handlers.recovery import handle_recovery
from handlers.start import handle_menu_text, show_home, start
from handlers.support import handle_support_callback, handle_support_input
from keyboards.user_keyboard import main_menu

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_STARTUP_ATTEMPTS = 3
TELEGRAM_STARTUP_RETRY_DELAY = 2.0
TELEGRAM_UNREACHABLE_MESSAGE = (
    "Telegram server is unreachable. Please check your internet/DNS connection and try again."
)


class _TokenRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        token = config.BOT_TOKEN
        if not token:
            return True

        message = record.getMessage()
        if token in message:
            record.msg = message.replace(token, "[REDACTED]")
            record.args = ()

        if record.exc_info:
            exception_text = "".join(traceback.format_exception(*record.exc_info))
            if token in exception_text:
                record.exc_text = exception_text.replace(token, "[REDACTED]")
                record.exc_info = None
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_TokenRedactionFilter())


async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_message is None:
        return
    admin_user = is_admin(update.effective_user.id)
    if not admin_user:
        record_user(update.effective_user)
    if not admin_user and is_user_blocked(update.effective_user.id):
        await update.effective_message.reply_text(
            "Access to this bot is currently restricted. Please contact support through your existing ticket."
        )
        return
    if (
        not admin_user
        and update.effective_chat is not None
        and update.effective_chat.type != "private"
    ):
        await update.effective_message.reply_text(
            "Please open CodeBazar in a private chat to continue."
        )
        return
    if update.effective_message.text in (
        "🛒 Buy Voucher",
        "📦 My Orders",
        "🔄 Recover Voucher",
        "💬 Support",
    ):
        await handle_menu_text(update, context)
        return
    if admin_user:
        await handle_admin_input(update, context)
        return
    if await handle_custom_quantity(update, context):
        return
    if await handle_utr_input(update, context):
        return
    if await handle_recovery(update, context):
        return
    if await handle_support_input(update, context):
        return
    if update.effective_message.text:
        await show_screen(
            update,
            context,
            "⚠️ Please choose an option from the menu below.\n\n"
            "🛍️ Select one of the available options to continue using CodeBazar.",
            reply_markup=main_menu(),
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, BadRequest):
        error_message = str(error).lower()
        if any(
            phrase in error_message
            for phrase in (
                "message is not modified",
                "query is too old",
                "query id is invalid",
                "message to edit not found",
                "message can't be edited",
            )
        ):
            logger.debug("Ignored harmless Telegram request error: %s", error)
            return
    if error is not None:
        logger.error(
            "Unhandled bot error",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Unhandled bot error without exception details")

    if isinstance(update, Update) and update.callback_query:
        try:
            if update.callback_query.message:
                if isinstance(error, (NetworkError, RetryAfter, TimedOut)):
                    notice = "Telegram is temporarily unavailable. Please try again shortly."
                else:
                    notice = "That action could not be completed. Please try again."
                await update.callback_query.message.reply_text(notice)
        except TelegramError:
            logger.exception("Could not send callback error notice")
        return
    if isinstance(update, Update) and update.effective_message:
        try:
            if isinstance(error, (NetworkError, RetryAfter, TimedOut)):
                message = (
                    "Telegram is temporarily unavailable. Please try again shortly."
                )
            else:
                message = "We couldn't complete that request. Please try again."
            await update.effective_message.reply_text(
                message
            )
        except TelegramError:
            logger.exception("Could not send error notice to the user")


def build_application() -> Application:
    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=5.0,
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=20.0,
        pool_timeout=5.0,
    )
    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(
        CallbackQueryHandler(guard_callback(show_home), pattern=r"^home$")
    )
    application.add_handler(
        CallbackQueryHandler(guard_callback(handle_back), pattern=r"^nav:back$")
    )
    application.add_handler(
        CallbackQueryHandler(
            guard_callback(handle_product_callback), pattern=r"^(shop:|product:|qty:)"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            guard_callback(handle_payment_callback),
            pattern=r"^(agree|paid|cancel):",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            guard_callback(handle_orders_callback),
            pattern=r"^(orders:\d+|recover:(start|cancel))$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            guard_callback(handle_support_callback),
            pattern=r"^support:(order|payment|voucher|other|cancel|menu|tickets:\d+|ticket:[A-Z0-9-]+|reply:[A-Z0-9-]+)$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(guard_callback(handle_admin_callback), pattern=r"^adm:")
    )
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, route_message)
    )
    application.add_error_handler(error_handler)
    return application


def _ensure_event_loop() -> asyncio.AbstractEventLoop:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


async def _initialize_telegram(application: Application) -> bool:
    for attempt in range(1, TELEGRAM_STARTUP_ATTEMPTS + 1):
        try:
            await application.bot.initialize()
            return True
        except InvalidToken:
            logger.error(
                "Telegram rejected the configured bot token. Verify BOT_TOKEN in .env; "
                "the token value was not logged."
            )
            return False
        except NetworkError as exc:
            if isinstance(exc, BadRequest):
                raise
            if attempt == TELEGRAM_STARTUP_ATTEMPTS:
                logger.error(TELEGRAM_UNREACHABLE_MESSAGE)
                return False
            logger.warning(
                "Telegram connection failed during startup (%s, attempt %d/%d). "
                "Retrying in %.1f seconds.",
                type(exc).__name__,
                attempt,
                TELEGRAM_STARTUP_ATTEMPTS,
                TELEGRAM_STARTUP_RETRY_DELAY,
            )
            await asyncio.sleep(TELEGRAM_STARTUP_RETRY_DELAY)
    return False


def main() -> None:
    config.QR_DIRECTORY.mkdir(parents=True, exist_ok=True)
    initialize_database()
    app = build_application()
    loop = _ensure_event_loop()
    logger.info(
        "Starting CodeBazar. Admin access is %s.",
        "enabled" if config.ADMIN_ID is not None else "disabled until ADMIN_ID is configured",
    )
    if not loop.run_until_complete(_initialize_telegram(app)):
        return
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=0)
    except NetworkError as exc:
        if isinstance(exc, BadRequest):
            raise
        logger.error(TELEGRAM_UNREACHABLE_MESSAGE)


if __name__ == "__main__":
    main()
