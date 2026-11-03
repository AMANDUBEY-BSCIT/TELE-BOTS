import logging
import sqlite3
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import config
from database import connection
from database import is_user_blocked, record_order_payment_event, record_user
from handlers.callback_guard import answer_callback
from keyboards.user_keyboard import home_only, payment
from handlers.customer_ui import (
    add_navigation,
    delete_stored_payment_photo,
    edit_stored_menu,
    ensure_private_chat,
    show_screen,
)
from services.order_service import OrderError, accept_terms, cancel_order, expire_order_if_needed
from services.payment_service import PaymentError, submit_utr

logger = logging.getLogger(__name__)


async def handle_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await ensure_private_chat(update, context):
        return
    await answer_callback(query, context)
    record_user(query.from_user)
    if is_user_blocked(query.from_user.id):
        await query.message.reply_text("Access to this bot is currently restricted.")
        return
    action, order_id = query.data.split(":", 1)
    if action == "cancel":
        try:
            cancelled = cancel_order(order_id, query.from_user.id)
        except OrderError as exc:
            await show_screen(
                update, context, str(exc), reply_markup=add_navigation(None), push=False
            )
            return
        history = context.user_data.setdefault("customer_screen_history", [])
        if (
            history
            and history[-1].get("type") == "terms"
            and history[-1].get("order_id") == order_id
        ):
            history.pop()
        await show_screen(
            update,
            context,
            "Order cancelled." if cancelled else "This order can no longer be cancelled.",
            reply_markup=add_navigation(None),
            screen={"type": "order_cancelled", "order_id": order_id},
            push=False,
        )
        await delete_stored_payment_photo(context)
        return
    if action == "agree":
        if config.ADMIN_ID is None:
            await show_screen(
                update, context,
                "Payments are temporarily unavailable. Please try again later.",
                reply_markup=add_navigation(None),
                push=False,
            )
            return
        if not config.QR_IMAGE_PATH.is_file():
            await show_screen(
                update, context,
                "Payment is temporarily unavailable because the store QR has not been uploaded yet. "
                "Please try again later or contact Support.",
                reply_markup=add_navigation(None),
                push=False,
            )
            return
        try:
            order = accept_terms(order_id, query.from_user.id)
        except OrderError as exc:
            await show_screen(
                update, context, str(exc), reply_markup=add_navigation(None), push=False
            )
            return
        if order["payment_status"] != "PAYMENT_PENDING":
            await show_screen(
                update, context, "This order is no longer awaiting payment.",
                reply_markup=add_navigation(None), push=False,
            )
            return
        if order["qr_sent_at"]:
            return
        order = expire_order_if_needed(order_id, query.from_user.id)
        if order["payment_status"] != "PAYMENT_PENDING":
            await show_screen(
                update, context,
                "This payment session has expired. Please place a new order.",
                reply_markup=add_navigation(None), push=False,
            )
            return
        await show_screen(
            update,
            context,
            "💳 Payment QR is shown below. Pay the exact order total.",
            reply_markup=add_navigation(None),
            screen={"type": "payment", "order_id": order_id},
        )
        await _send_payment_details(query.message.chat_id, order, context)
        return
    if action == "paid":
        try:
            order = expire_order_if_needed(order_id, query.from_user.id)
        except OrderError as exc:
            await show_screen(
                update, context, str(exc), reply_markup=add_navigation(None), push=False
            )
            return
        if order["payment_status"] in {
            "PENDING_VERIFICATION",
            "UTR_SUBMITTED",
            "APPROVED",
        }:
            return
        if order["payment_status"] != "PAYMENT_PENDING":
            await show_screen(
                update,
                context,
                "This payment session has expired or is no longer active. Please contact support if you already paid.",
                reply_markup=add_navigation(None),
                push=False,
            )
            return
        if order["paid_clicked_at"]:
            if context.user_data.get("awaiting_utr") != order_id:
                await show_utr_prompt(update, context, order_id, push=False)
            return
        record_order_payment_event(
            order_id,
            "PAYMENT_BUTTON_CLICKED",
            {"amount": order["total_amount"]},
        )
        await _notify_admin_payment_event(context, order_id, "PAYMENT_BUTTON_CLICKED")
        await show_utr_prompt(update, context, order_id)


def _utr_prompt_text(error: str | None = None) -> str:
    prompt = "Please enter your 12-digit UTR number to verify your payment."
    return f"{escape(error)}\n\n{prompt}" if error else prompt


def _invalid_utr_text() -> str:
    return (
        "<b>⚠️ Invalid UTR Number</b>\n\n"
        "Please check your UTR number and try again.\n\n"
        "Your UTR must contain exactly <b>12 digits</b>.\n\n"
        "Please enter a valid 12-digit UTR number."
    )


async def show_utr_prompt(update, context, order_id: str, push: bool = True) -> None:
    await delete_stored_payment_photo(context)
    context.user_data["awaiting_utr"] = order_id
    screen = {"type": "utr_input", "order_id": order_id}
    current = context.user_data.get("customer_current_screen")
    if push and current is not None and current != screen:
        context.user_data.setdefault("customer_screen_history", []).append(current)
    context.user_data["customer_current_screen"] = screen
    text = _utr_prompt_text()
    markup = home_only()
    await show_screen(
        update,
        context,
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        screen=screen,
        push=False,
    )


async def _send_payment_details(chat_id, order, context) -> None:
    text = (
        "💳 MAKE PAYMENT HERE\n\n"
        f"🛍️ Product: {order['product_name']}\n"
        f"🔢 Quantity: {order['quantity']}\n"
        f"💰 Total Amount: ₹{order['total_amount']}\n"
        f"🆔 Order ID: {order['id']}\n\n"
        "⏳ QR Validity: 10 Minutes\n\n"
        "Please scan the QR code and pay the exact amount shown above.\n\n"
        "After completing payment, click the button below and submit your 12-digit UTR number."
    )
    try:
        with config.QR_IMAGE_PATH.open("rb") as image:
            sent_photo = await context.bot.send_photo(
                chat_id=chat_id,
                photo=image,
                caption=text,
                reply_markup=payment(order["id"]),
            )
            if getattr(sent_photo, "message_id", None) is not None:
                context.user_data["customer_payment_photo_id"] = sent_photo.message_id
                context.user_data["customer_payment_photo_chat_id"] = chat_id
    except OSError:
        logger.exception("Could not read the configured payment QR image")
        unavailable = await edit_stored_menu(
            context,
            "Payment is temporarily unavailable because the store QR could not be loaded. "
            "Please contact Support.",
            add_navigation(None),
        )
        if not unavailable:
            await context.bot.send_message(
                chat_id,
                "Payment is temporarily unavailable because the store QR could not be loaded. "
                "Please contact Support.",
            )
        return
    except TelegramError:
        logger.exception("Could not send the payment QR image for order %s", order["id"])
        unavailable = await edit_stored_menu(
            context,
            "Payment QR could not be delivered. Your order has not been marked as paid. "
            "Please try again or contact Support.",
            add_navigation(None),
        )
        if not unavailable:
            await context.bot.send_message(
                chat_id,
                "Payment QR could not be delivered. Your order has not been marked as paid. "
                "Please try again or contact Support.",
            )
        return
    record_order_payment_event(
        order["id"],
        "PAYMENT_SCREEN_REACHED",
        {"amount": order["total_amount"]},
    )
    await _notify_admin_payment_event(context, order["id"], "PAYMENT_SCREEN_REACHED")
    record_order_payment_event(
        order["id"],
        "QR_SENT",
        {"amount": order["total_amount"]},
    )
    await _notify_admin_payment_event(context, order["id"], "QR_SENT")


async def _notify_admin_payment_event(context, order_id: str, event_type: str) -> None:
    if config.ADMIN_ID is None:
        return
    with connection() as conn:
        order = conn.execute(
            """SELECT o.id, o.user_id, o.product_name, o.quantity, o.total_amount,
                      u.first_name, u.username
               FROM orders o JOIN users u ON u.telegram_id = o.user_id
               WHERE o.id = ?""",
            (order_id,),
        ).fetchone()
    if order is None:
        return
    username = f"@{order['username']}" if order["username"] else "not set"
    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_ID,
            text=(
                f"<b>🔔 {escape(event_type.replace('_', ' ').title())}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Customer: {escape(order['first_name'])} ({escape(username)})\n"
                f"Telegram ID: <code>{order['user_id']}</code>\n"
                f"Order: <code>{escape(order['id'])}</code>\n"
                f"Product: {escape(order['product_name'])} × {order['quantity']}\n"
                f"Amount: <b>₹{order['total_amount']}.00</b>"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.exception("Could not notify admin about payment event for order %s", order_id)


async def handle_utr_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    order_id = context.user_data.get("awaiting_utr")
    if order_id is None:
        return False
    message = update.effective_message
    utr = message.text if message and message.text is not None else ""
    try:
        order = submit_utr(order_id, update.effective_user.id, utr)
    except PaymentError as exc:
        error_text = str(exc)
        if "valid 12-digit numeric UTR" in error_text:
            prompt = _invalid_utr_text()
        else:
            prompt = _utr_prompt_text(error_text)
        await show_screen(
            update,
            context,
            prompt,
            reply_markup=home_only(),
            parse_mode=ParseMode.HTML,
            push=False,
        )
        if any(
            phrase in error_text.lower()
            for phrase in ("expired", "not awaiting", "not found")
        ):
            context.user_data.pop("awaiting_utr", None)
        return True
    except sqlite3.Error:
        logger.exception("Could not save UTR for order %s", order_id)
        prompt = _utr_prompt_text(
            "We could not save your payment details. Please try again."
        )
        await show_screen(
            update,
            context,
            prompt,
            reply_markup=home_only(),
            parse_mode=ParseMode.HTML,
            push=False,
        )
        return True
    context.user_data.pop("awaiting_utr", None)
    username = f"@{order['username']}" if order["username"] else "Not set"
    notification = (
        "<b>PAYMENT VERIFICATION REQUIRED</b>\n\n"
        f"Order ID: <code>{escape(order['id'])}</code>\n"
        f"Customer name: {escape(order['first_name'])}\n"
        f"Username: {escape(username)}\n"
        f"Telegram ID: <code>{order['user_id']}</code>\n"
        f"Product: {escape(order['product_name'])}\n"
        f"Quantity: {order['quantity']}\n"
        f"Amount: ₹{order['total_amount']}\n"
        f"UTR: <code>{order['utr']}</code>\n"
        f"Submission timestamp: {escape(order['submitted_at'])}\n"
        "Status: PENDING VERIFICATION\n\n"
        "Manually verify the actual UPI transaction before deciding."
    )
    if config.ADMIN_ID is not None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        try:
            await context.bot.send_message(
                chat_id=config.ADMIN_ID,
                text=notification,
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton("✅ APPROVE PAYMENT", callback_data=f"adm:approve:{order_id}"),
                        InlineKeyboardButton("❌ REJECT PAYMENT", callback_data=f"adm:reject:{order_id}"),
                    ]]
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            logger.exception("Could not notify admin about payment submission %s", order_id)
    confirmation = (
        "<b>⏳ Payment Under Review</b>\n"
        "Your payment details have been submitted successfully.\n\n"
        f"<b>Order ID:</b> {escape(order['id'])}\n\n"
        f"<b>Amount:</b> ₹{order['total_amount']}\n\n"
        f"<b>UTR:</b> {escape(order['utr'])}\n\n"
        "<b>Status:</b> Waiting for Admin Confirmation\n\n"
        "Our team is checking your payment. We will notify you once your payment has been verified.\n\n"
        "Thank you for choosing CodeBazar 🛍️"
    )
    markup = home_only("🏠 HOME")
    screen = {"type": "payment_submitted", "order_id": order_id}
    context.user_data["customer_current_screen"] = screen
    await show_screen(
        update,
        context,
        confirmation,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        screen=screen,
        push=False,
    )
    return True
