import logging

from telegram.error import BadRequest
from telegram.ext import ContextTypes

import config
from handlers.callback_guard import answer_callback
from database import record_activity, record_user
from handlers.customer_ui import (
    HOME_SCREEN,
    delete_stored_payment_photo,
    ensure_private_chat,
    handle_home,
    show_screen,
)
from keyboards.user_keyboard import main_menu

logger = logging.getLogger(__name__)


async def render_home(
    update, context, push: bool = True, create_anchor: bool = False
) -> None:
    await show_screen(
        update,
        context,
        HOME_SCREEN["text"],
        reply_markup=main_menu(),
        screen=HOME_SCREEN,
        push=push,
        create_anchor=create_anchor,
    )


async def start(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_private_chat(update, context):
        if update.effective_message:
            await update.effective_message.reply_text(
                "Please open CodeBazar in a private chat to continue."
            )
        return
    user = update.effective_user
    if user is not None and user.id != config.ADMIN_ID:
        record_user(user)
        record_activity(user.id, "BOT_STARTED")
        from database import is_user_blocked

        if is_user_blocked(user.id):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "Access to this bot is currently restricted."
                )
            return

    old_message_id = context.user_data.get("customer_menu_message_id")
    old_chat_id = context.user_data.get("customer_menu_chat_id")
    if old_message_id is not None and old_chat_id is not None:
        try:
            await context.bot.delete_message(old_chat_id, old_message_id)
        except BadRequest:
            logger.exception("Could not remove the previous customer menu before /start")
    await delete_stored_payment_photo(context)
    for key in (
        "custom_product",
        "awaiting_utr",
        "recover_waiting",
        "support_pending_category",
        "active_ticket_id",
        "customer_menu_message_id",
        "customer_menu_chat_id",
        "customer_menu_owner",
        "customer_screen_history",
        "customer_current_screen",
    ):
        context.user_data.pop(key, None)
    if update.effective_message:
        await render_home(update, context, push=False, create_anchor=True)


async def show_home(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await ensure_private_chat(update, context):
        return
    await answer_callback(query, context)
    if update.effective_user and update.effective_user.id != config.ADMIN_ID:
        record_user(update.effective_user)
        from database import is_user_blocked

        if is_user_blocked(update.effective_user.id):
            await query.message.reply_text("Access to this bot is currently restricted.")
            return
    await handle_home(update, context)


async def handle_menu_text(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.text is None:
        return
    if update.effective_user:
        record_user(update.effective_user)
    for key in ("custom_product", "awaiting_utr", "recover_waiting", "support_pending_category"):
        context.user_data.pop(key, None)
    if message.text == "🛒 Buy Voucher":
        from handlers.products import show_products

        await show_products(update, context)
    elif message.text == "📦 My Orders":
        from handlers.orders import show_orders

        await show_orders(update, context, 0)
    elif message.text == "🔄 Recover Voucher":
        from handlers.orders import show_recovery_prompt

        await show_recovery_prompt(update, context)
    elif message.text == "💬 Support":
        from handlers.support import show_support

        await show_support(update, context)
