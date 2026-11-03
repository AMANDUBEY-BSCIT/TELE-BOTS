import logging
from typing import Any

from telegram.error import BadRequest

from handlers.callback_guard import answer_callback

logger = logging.getLogger(__name__)

HOME_SCREEN = {
    "type": "home",
    "text": "Welcome to CodeBazar 🛍️\n\nYour Trusted Digital Voucher Store",
}


async def ensure_private_chat(update, context=None) -> bool:
    chat = update.effective_chat
    query = update.callback_query
    if chat is None and query and query.message:
        chat = query.message.chat
    if chat is None or chat.type == "private":
        return True
    if query:
        if context is None:
            await query.answer(
                "Please open CodeBazar in a private chat to continue.", show_alert=True
            )
        else:
            await answer_callback(
                query,
                context,
                "Please open CodeBazar in a private chat to continue.",
                show_alert=True,
            )
    return False


def navigation_buttons(back: str = "nav:back") -> list[list]:
    from telegram import InlineKeyboardButton

    return [
        [InlineKeyboardButton("🔙 Back", callback_data=back)],
        [InlineKeyboardButton("🏠 Home", callback_data="home")],
    ]


def add_navigation(markup, back: str = "nav:back"):
    from telegram import InlineKeyboardMarkup

    rows = [list(row) for row in markup.inline_keyboard] if markup else []
    rows.extend(navigation_buttons(back))
    return InlineKeyboardMarkup(rows)


def _chat_id(update) -> int | None:
    if update.effective_chat:
        chat_id = getattr(update.effective_chat, "id", None)
        if chat_id is not None:
            return chat_id
    query = update.callback_query
    if query and query.message and query.message.chat:
        return getattr(query.message.chat, "id", None) or query.from_user.id
    return update.effective_user.id if update.effective_user else None


async def show_screen(
    update,
    context,
    text: str,
    reply_markup=None,
    parse_mode=None,
    screen: dict[str, Any] | None = None,
    push: bool = True,
    create_anchor: bool = False,
) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if not await ensure_private_chat(update, context):
        return False
    query = update.callback_query
    chat_id = _chat_id(update)
    if chat_id is None:
        return False
    if query and query.message and query.message.chat:
        if query.message.chat.type != "private" or chat_id != user.id:
            return False

    owner_id = context.user_data.get("customer_menu_owner")
    if owner_id is not None and owner_id != user.id:
        return False

    message_id = context.user_data.get("customer_menu_message_id")
    menu_chat_id = context.user_data.get("customer_menu_chat_id")
    if menu_chat_id is not None and menu_chat_id != chat_id:
        return False
    if (
        message_id is None
        and query
        and query.message
        and getattr(query.message, "message_id", None) is not None
        and not getattr(query.message, "photo", None)
    ):
        message_id = query.message.message_id
        context.user_data["customer_menu_message_id"] = message_id
        context.user_data["customer_menu_chat_id"] = chat_id
        context.user_data["customer_menu_owner"] = user.id

    try:
        if message_id is None:
            if query:
                sent = await query.message.reply_text(
                    text, reply_markup=reply_markup, parse_mode=parse_mode
                )
                if (
                    getattr(query.message, "photo", None)
                    and getattr(sent, "message_id", None) is not None
                ):
                    message_id = sent.message_id
                    context.user_data["customer_menu_message_id"] = message_id
                    context.user_data["customer_menu_chat_id"] = chat_id
                    context.user_data["customer_menu_owner"] = user.id
            elif update.effective_message:
                sent = await update.effective_message.reply_text(
                    text, reply_markup=reply_markup, parse_mode=parse_mode
                )
                if create_anchor and getattr(sent, "message_id", None) is not None:
                    message_id = sent.message_id
                    context.user_data["customer_menu_message_id"] = message_id
                    context.user_data["customer_menu_chat_id"] = chat_id
                    context.user_data["customer_menu_owner"] = user.id
            else:
                sent = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                message_id = sent.message_id
                context.user_data["customer_menu_message_id"] = message_id
                context.user_data["customer_menu_chat_id"] = chat_id
                context.user_data["customer_menu_owner"] = user.id
        elif not hasattr(context.bot, "edit_message_text"):
            # Lightweight test doubles and legacy adapters may expose only send_message.
            if query:
                await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            else:
                await update.effective_message.reply_text(
                    text, reply_markup=reply_markup, parse_mode=parse_mode
                )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
    except BadRequest as exc:
        error_message = str(exc).lower()
        if "message is not modified" in error_message:
            pass
        elif "message to edit not found" in error_message or "message can't be edited" in error_message:
            logger.info("Customer menu message is no longer editable; sending a replacement")
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            context.user_data["customer_menu_message_id"] = getattr(
                sent, "message_id", None
            )
            context.user_data["customer_menu_chat_id"] = chat_id
            context.user_data["customer_menu_owner"] = user.id
        else:
            raise

    if screen is not None:
        current = context.user_data.get("customer_current_screen")
        if push and current is not None and current != screen:
            history = context.user_data.setdefault("customer_screen_history", [])
            history.append(current)
        context.user_data["customer_current_screen"] = screen
    return True


async def edit_stored_menu(context, text: str, reply_markup=None, parse_mode=None) -> bool:
    chat_id = context.user_data.get("customer_menu_chat_id")
    message_id = context.user_data.get("customer_menu_message_id")
    if chat_id is None or message_id is None:
        return False
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except BadRequest as exc:
        error_message = str(exc).lower()
        if "message is not modified" in error_message:
            return True
        if "message to edit not found" in error_message or "message can't be edited" in error_message:
            logger.info("Stored customer menu is no longer editable")
            return False
        raise
    return True


async def delete_stored_payment_photo(context) -> None:
    chat_id = context.user_data.pop("customer_payment_photo_chat_id", None)
    message_id = context.user_data.pop("customer_payment_photo_id", None)
    if chat_id is None or message_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest:
        logger.debug("Could not remove the previous customer payment QR photo")


async def render_screen(update, context, screen: dict[str, Any], push: bool = False) -> None:
    kind = screen["type"]
    if kind == "home":
        from handlers.start import render_home

        await render_home(update, context, push=push)
    elif kind == "products":
        from handlers.products import show_products

        await show_products(update, context, push=push)
    elif kind == "product":
        from handlers.products import show_product

        await show_product(update, context, screen["product_id"], push=push)
    elif kind == "orders":
        from handlers.orders import show_orders

        await show_orders(update, context, screen["page"], push=push)
    elif kind == "support":
        from handlers.support import show_support

        await show_support(update, context, push=push)
    elif kind == "tickets":
        from handlers.support import show_customer_tickets

        await show_customer_tickets(update, context, screen["page"], push=push)
    elif kind == "ticket":
        from handlers.support import show_customer_ticket

        await show_customer_ticket(update, context, screen["ticket_id"], push=push)
    elif kind == "terms":
        from handlers.products import show_order_terms

        await show_order_terms(update, context, screen["order_id"], push=push)
    elif kind == "recover":
        from handlers.orders import show_recovery_prompt

        await show_recovery_prompt(update, context, push=push)
    elif kind == "custom_quantity":
        from handlers.products import show_custom_quantity_prompt

        await show_custom_quantity_prompt(
            update, context, screen["product_id"], push=push
        )
    elif kind == "support_input":
        from handlers.support import show_support_input_prompt

        await show_support_input_prompt(
            update, context, screen["category"], push=push
        )
    elif kind == "ticket_reply":
        from handlers.support import show_ticket_reply_prompt

        await show_ticket_reply_prompt(
            update, context, screen["ticket_id"], push=push
        )
    elif kind == "utr_input":
        from handlers.payments import show_utr_prompt

        await show_utr_prompt(update, context, screen["order_id"], push=push)
    elif kind == "payment":
        await show_screen(
            update,
            context,
            "💳 Payment QR is shown below. Pay the exact order total.",
            reply_markup=add_navigation(None),
            screen=screen,
            push=push,
        )


async def handle_back(update, context, answer: bool = True) -> None:
    query = update.callback_query
    if not await ensure_private_chat(update, context):
        return
    if answer:
        await answer_callback(query, context)
    current = context.user_data.get("customer_current_screen", {})
    if current.get("type") == "custom_quantity":
        context.user_data.pop("custom_product", None)
    if current.get("type") == "recover":
        context.user_data.pop("recover_waiting", None)
    if current.get("type") == "support_input":
        context.user_data.pop("support_pending_category", None)
    payment_photo_id = context.user_data.get("customer_payment_photo_id")
    if current.get("type") == "payment":
        await delete_stored_payment_photo(context)
    history = context.user_data.setdefault("customer_screen_history", [])
    target = history.pop() if history else HOME_SCREEN
    await render_screen(update, context, target, push=False)
    source_message_id = getattr(query.message, "message_id", None)
    if (
        source_message_id is not None
        and source_message_id != context.user_data.get("customer_menu_message_id")
        and source_message_id != payment_photo_id
    ):
        try:
            await context.bot.delete_message(
                chat_id=getattr(query.message, "chat_id", _chat_id(update)),
                message_id=source_message_id,
            )
        except BadRequest:
            logger.debug("Could not remove a previous customer navigation photo")


async def handle_home(update, context) -> None:
    context.user_data["customer_screen_history"] = []
    for key in (
        "custom_product",
        "awaiting_utr",
        "recover_waiting",
        "support_pending_category",
        "active_ticket_id",
    ):
        context.user_data.pop(key, None)
    payment_photo_id = context.user_data.get("customer_payment_photo_id")
    await render_screen(update, context, HOME_SCREEN, push=False)
    await delete_stored_payment_photo(context)
    query = update.callback_query
    source_message_id = getattr(query.message, "message_id", None) if query else None
    if (
        query
        and source_message_id is not None
        and source_message_id != context.user_data.get("customer_menu_message_id")
        and source_message_id != payment_photo_id
    ):
        try:
            await context.bot.delete_message(
                chat_id=getattr(query.message, "chat_id", _chat_id(update)),
                message_id=source_message_id,
            )
        except BadRequest:
            logger.debug("Could not remove a previous customer navigation photo")
