import asyncio
import logging
from functools import wraps

from telegram.error import BadRequest, NetworkError, TelegramError

logger = logging.getLogger(__name__)


async def answer_callback(query, context, text: str | None = None, show_alert: bool = False) -> None:
    acknowledged = context.bot_data.setdefault("_acknowledged_callback_ids", set())
    query_id = getattr(query, "id", id(query))
    if query_id in acknowledged:
        return
    acknowledged.add(query_id)
    try:
        if text is None and not show_alert:
            await query.answer()
        else:
            await query.answer(text=text, show_alert=show_alert)
    except BadRequest as exc:
        message = str(exc).lower()
        if "query is too old" not in message and "query id is invalid" not in message:
            raise
        logger.debug("Telegram callback query can no longer be answered: %s", exc)
    except NetworkError:
        logger.exception("Could not acknowledge Telegram callback query %s", query_id)


def guard_callback(handler):
    @wraps(handler)
    async def guarded(update, context):
        query = update.callback_query
        if query is None:
            return await handler(update, context)

        user_id = query.from_user.id
        message = query.message
        if message is not None:
            chat_id = getattr(message, "chat_id", None)
            if chat_id is None and getattr(message, "chat", None) is not None:
                chat_id = getattr(message.chat, "id", None)
            message_id = getattr(message, "message_id", None)
        else:
            chat_id = getattr(query, "inline_message_id", None)
            message_id = chat_id
        lock_keys = {("user", user_id), ("message", chat_id, message_id)}

        guard = context.bot_data.setdefault(
            "_callback_guard", {"lock": asyncio.Lock(), "active": set()}
        )
        chat = getattr(message, "chat", None) if message is not None else None
        if chat is not None and chat.type != "private":
            await answer_callback(
                query,
                context,
                "Please open CodeBazar in a private chat to continue.",
                show_alert=True,
            )
            context.bot_data.get("_acknowledged_callback_ids", set()).discard(
                getattr(query, "id", id(query))
            )
            return None

        async with guard["lock"]:
            busy = bool(lock_keys & guard["active"])
            if not busy:
                guard["active"].update(lock_keys)

        if busy:
            try:
                await query.answer(
                    text="⏳ Please wait, processing your request..."
                )
            except BadRequest as exc:
                logger.debug("Could not acknowledge repeated callback: %s", exc)
            except NetworkError:
                logger.exception("Could not acknowledge repeated callback")
            return None

        original_markup = getattr(message, "reply_markup", None)
        customer_menu_message = (
            message is not None
            and context.user_data.get("customer_menu_message_id") == message_id
            and context.user_data.get("customer_menu_chat_id") == chat_id
        )
        keyboard_disabled = False
        completed = False
        try:
            await answer_callback(
                query, context, "⏳ Please wait, processing your request..."
            )
            if (
                original_markup is not None
                and message is not None
                and not getattr(message, "photo", None)
            ):
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=message_id,
                        reply_markup=None,
                    )
                    keyboard_disabled = True
                except BadRequest as exc:
                    error_message = str(exc).lower()
                    if "message is not modified" in error_message:
                        logger.debug("Callback buttons are already disabled")
                    elif (
                        "message to edit not found" in error_message
                        or "message can't be edited" in error_message
                    ):
                        logger.debug("Callback message cannot be temporarily disabled")
                    else:
                        logger.exception("Could not temporarily disable callback buttons")
                except TelegramError:
                    logger.exception("Could not temporarily disable callback buttons")
            result = await handler(update, context)
            completed = True
            return result
        except BadRequest as exc:
            message_text = str(exc).lower()
            if any(
                phrase in message_text
                for phrase in (
                    "message is not modified",
                    "query is too old",
                    "query id is invalid",
                )
            ):
                logger.debug("Ignored harmless Telegram callback error: %s", exc)
                return None
            if (
                "message to edit not found" in message_text
                or "message can't be edited" in message_text
            ):
                logger.info("Callback targeted a Telegram message that cannot be edited")
                if message is not None:
                    try:
                        await message.reply_text(
                            "This menu is out of date. Please open the latest CodeBazar menu and try again."
                        )
                    except TelegramError:
                        logger.exception("Could not report an outdated Telegram menu")
                return None
            raise
        except NetworkError:
            logger.exception(
                "Telegram network error while handling callback %s", query.data
            )
            if message is not None:
                try:
                    await message.reply_text(
                        "Telegram is temporarily unavailable. Please check the order status before trying again."
                    )
                except TelegramError:
                    logger.exception("Could not report Telegram network error to customer")
            return None
        finally:
            final_action = (query.data or "").split(":", 2)
            keep_disabled = (
                completed
                and len(final_action) > 1
                and final_action[0] == "adm"
                and final_action[1] in {"approve", "reject", "deliver"}
            )
            if (
                keyboard_disabled
                and (not customer_menu_message or not completed)
                and not keep_disabled
            ):
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=message_id,
                        reply_markup=original_markup,
                    )
                except BadRequest as exc:
                    error_message = str(exc).lower()
                    if "message is not modified" in error_message:
                        pass
                    elif (
                        "message to edit not found" in error_message
                        or "message can't be edited" in error_message
                    ):
                        logger.debug("Callback message can no longer be restored")
                    else:
                        logger.exception("Could not restore callback buttons")
                except TelegramError:
                    logger.exception("Could not restore callback buttons")
            async with guard["lock"]:
                guard["active"].difference_update(lock_keys)
            context.bot_data.get("_acknowledged_callback_ids", set()).discard(
                getattr(query, "id", id(query))
            )

    return guarded
