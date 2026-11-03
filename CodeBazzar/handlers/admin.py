import json
import logging
import re
import sqlite3
from html import escape
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import config
from config import PAGE_SIZE
from handlers.callback_guard import answer_callback
from database import (
    add_activity,
    connection,
    get_all_products,
    get_product,
    record_order_payment_event,
    transaction,
    utc_now,
)
from keyboards.admin_keyboard import back_home, dashboard
from keyboards.user_keyboard import main_menu
from services.delivery_service import DeliveryError, deliver_codes, voucher_code_messages
from services.order_service import current_available_stock
from services.payment_service import PaymentError, approve_payment, reject_payment

logger = logging.getLogger(__name__)


def _escape_html_limited(value: str, max_length: int) -> str:
    escaped = []
    length = 0
    for character in value:
        encoded = escape(character)
        if length + len(encoded) > max_length:
            break
        escaped.append(encoded)
        length += len(encoded)
    return "".join(escaped)


def is_admin(user_id: int | None) -> bool:
    return config.ADMIN_ID is not None and user_id == config.ADMIN_ID


def _is_private_admin(update: Update) -> bool:
    user = getattr(update, "effective_user", None)
    user_id = user.id if user else None
    chat = getattr(update, "effective_chat", None)
    return is_admin(user_id) and chat is not None and chat.type == ChatType.PRIVATE


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text("Admin access is not available.")
        return
    context.user_data.pop("admin_action", None)
    context.user_data.pop("delivery_codes", None)
    await update.effective_message.reply_text(
        "<b>🛠 CODEBAZAR CONTROL CENTER</b>\n"
        "<i>Store operations at a glance.</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Choose a section to manage:",
        reply_markup=dashboard(),
        parse_mode=ParseMode.HTML,
    )


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_private_admin(update):
        await answer_callback(query, context, "Unauthorized.", show_alert=True)
        return
    await answer_callback(query, context)
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "home":
        context.user_data.pop("admin_action", None)
        context.user_data.pop("delivery_codes", None)
        await query.message.reply_text(
            "<b>🛠 CODEBAZAR CONTROL CENTER</b>\n"
            "<i>Store operations at a glance.</i>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Choose a section to manage:",
            reply_markup=dashboard(),
            parse_mode=ParseMode.HTML,
        )
    elif action == "back":
        await query.message.reply_text(
            "<b>CodeBazar 🛍️</b>\n<i>Your Trusted Digital Voucher Store</i>",
            reply_markup=main_menu(),
            parse_mode=ParseMode.HTML,
        )
    elif action == "overview":
        await _show_overview(query.message)
    elif action == "customers" and len(parts) == 3:
        await _show_customers(query.message, int(parts[2]))
    elif action == "customer" and len(parts) == 3:
        await _show_customer(query.message, int(parts[2]))
    elif action == "customer_orders" and len(parts) == 4:
        await _show_customer_orders(query.message, int(parts[2]), int(parts[3]))
    elif action == "customer_payments" and len(parts) == 4:
        await _show_customer_payments(query.message, int(parts[2]), int(parts[3]))
    elif action == "customer_activity" and len(parts) == 4:
        await _show_customer_activity(query.message, int(parts[2]), int(parts[3]))
    elif action == "customer_messages" and len(parts) == 3:
        await _show_customer_messages(query.message, int(parts[2]))
    elif action == "inbox" and len(parts) == 3:
        await _show_customer_inbox(query.message, int(parts[2]))
    elif action == "customer_block" and len(parts) == 3:
        await _toggle_customer_block(query.message, int(parts[2]))
    elif action == "payments" and len(parts) == 3:
        await _show_payment_monitoring(query.message, int(parts[2]))
    elif action == "payment" and len(parts) == 3:
        await _show_payment_details(query.message, parts[2])
    elif action == "live" and len(parts) == 3:
        await _show_activity_feed(query.message, int(parts[2]), context)
    elif action == "activity_customer":
        context.user_data["admin_action"] = ("activity_filter_customer", "")
        await query.message.reply_text("Enter a Telegram customer ID to filter activity, or 0 for all.")
    elif action == "activity_type":
        event_types = (
            "BOT_STARTED",
            "STORE_OPENED",
            "VOUCHER_RECOVERY_OPENED",
            "SUPPORT_OPENED",
            "PRODUCT_VIEWED",
            "QUANTITY_SELECTED",
            "CUSTOM_QUANTITY_SELECTED",
            "TERMS_ACCEPTED",
            "PAYMENT_SCREEN_REACHED",
            "QR_SENT",
            "PAYMENT_BUTTON_CLICKED",
            "UTR_SUBMITTED",
            "PAYMENT_APPROVED",
            "PAYMENT_REJECTED",
            "VOUCHERS_DELIVERED",
            "SUPPORT_TICKET_SUBMITTED",
            "SUPPORT_REPLY_SUBMITTED",
            "CUSTOMER_MESSAGE_RECEIVED",
            "ADMIN_MESSAGE_SENT",
        )
        buttons = [
            [InlineKeyboardButton(event.replace("_", " ").title(), callback_data=f"adm:activity_event:{event}")]
            for event in event_types
        ]
        buttons.append([InlineKeyboardButton("🔙 Activity", callback_data="adm:live:0")])
        await query.message.reply_text(
            "Choose an activity type to filter:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    elif action == "activity_date":
        context.user_data["admin_action"] = ("activity_filter_date", "")
        await query.message.reply_text("Enter a date as YYYY-MM-DD, or 0 to clear the date filter.")
    elif action == "activity_event" and len(parts) == 3:
        context.user_data.setdefault("activity_filters", {})["event_type"] = (
            "" if parts[2] == "all" else parts[2]
        )
        await _show_activity_feed(query.message, 0, context)
    elif action == "activity_clear":
        context.user_data.pop("activity_filters", None)
        await _show_activity_feed(query.message, 0, context)
    elif action == "revenue":
        await _show_revenue(query.message)
    elif action == "delivery" and len(parts) == 3:
        await _show_delivery_queue(query.message, int(parts[2]))
    elif action == "analytics":
        await _show_product_analytics(query.message)
    elif action == "search":
        context.user_data["admin_action"] = ("search_customer_order", "")
        await query.message.reply_text(
            "Enter a customer Telegram ID, username, or an order ID to search."
        )
    elif action == "customer_message" and len(parts) == 3:
        target_id = int(parts[2])
        context.user_data["admin_action"] = ("customer_message", str(target_id))
        await query.message.reply_text(
            f"Type a private message for customer {target_id}."
        )
    elif action == "products":
        await _show_products(query.message)
    elif action == "product" and len(parts) == 3:
        await _show_product(query.message, parts[2])
    elif action in ("edit_name", "edit_price", "stock_add", "stock_set") and len(parts) == 3:
        context.user_data["admin_action"] = (action, parts[2])
        prompt = {
            "edit_name": "Send the new product name.",
            "edit_price": "Send the new whole-number price in ₹.",
            "stock_add": "Send the number of codes to add.",
            "stock_set": "Send the new total stock quantity.",
        }[action]
        await query.message.reply_text(prompt)
    elif action == "toggle" and len(parts) == 3:
        with transaction() as conn:
            conn.execute("UPDATE products SET enabled = 1 - enabled WHERE id = ?", (parts[2],))
        await _show_product(query.message, parts[2])
    elif action == "stock":
        await _show_stock(query.message)
    elif action == "stock_product" and len(parts) == 3:
        await _show_stock_product(query.message, parts[2])
    elif action in ("orders", "pending", "users", "tickets") and len(parts) == 3:
        await _show_page(query.message, action, int(parts[2]))
    elif action == "order" and len(parts) == 3:
        await _show_order(query.message, parts[2])
    elif action == "approve" and len(parts) == 3:
        await _approve(query.message, parts[2], context)
    elif action == "reject" and len(parts) == 3:
        await _reject(query.message, parts[2], context)
    elif action == "deliver" and len(parts) == 3:
        with connection() as conn:
            order = conn.execute(
                "SELECT quantity, payment_status, delivery_status FROM orders WHERE id = ?",
                (parts[2],),
            ).fetchone()
        if order:
            if order["payment_status"] != "APPROVED" or order["delivery_status"] == "DELIVERED":
                await query.message.reply_text("This order is not eligible for voucher delivery.")
                return
            context.user_data["admin_action"] = ("deliver", parts[2])
            context.user_data["delivery_codes"] = []
            await query.message.reply_text(
                f"Send exactly {order['quantity']} voucher code(s), one code per line. "
                "You may send multiple messages; each message adds a batch."
            )
        else:
            await query.message.reply_text("Order not found.")
    elif action == "report":
        await _show_report(query.message)
    elif action == "settings":
        await _show_settings(query.message)
    elif action == "qr":
        await query.message.reply_text(
            "<b>🖼 PAYMENT QR</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Status: <b>{'✅ Uploaded' if config.QR_IMAGE_PATH.is_file() else '⚠️ Not uploaded'}</b>\n"
            "Upload the QR customers will use at checkout.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⬆️ Upload / Replace", callback_data="adm:qr_upload"),
                        InlineKeyboardButton("👁 View QR", callback_data="adm:qr_view"),
                    ],
                    [InlineKeyboardButton("🔙 Dashboard", callback_data="adm:home")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
    elif action == "qr_upload":
        context.user_data["admin_action"] = ("qr", "")
        await query.message.reply_text(
            "Send the QR image as a photo. It will replace the current persistent payment QR."
        )
    elif action == "qr_view":
        if config.QR_IMAGE_PATH.is_file():
            with config.QR_IMAGE_PATH.open("rb") as photo:
                await context.bot.send_photo(query.message.chat_id, photo)
        else:
            await query.message.reply_text("No QR image has been uploaded.")
    elif action == "broadcast":
        context.user_data["admin_action"] = ("broadcast", "")
        await query.message.reply_text("Send the text message to broadcast to all registered users.")
    elif action == "search_order":
        context.user_data["admin_action"] = ("search_order", "")
        await query.message.reply_text("Enter the Order ID to search.")
    elif action == "ticket_reply" and len(parts) == 3:
        context.user_data["admin_action"] = ("ticket_reply", parts[2])
        await query.message.reply_text(f"Enter your reply for ticket {parts[2]}.")
    elif action == "ticket_resolve" and len(parts) == 3:
        await _resolve_ticket(query.message, parts[2], context)
    elif action == "ticket" and len(parts) == 3:
        await _show_ticket(query.message, parts[2])


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _is_private_admin(update):
        return False
    action_data = context.user_data.get("admin_action")
    if not action_data:
        return False
    action, target = action_data
    message = update.effective_message
    if message is None:
        return False
    if action == "customer_message":
        text = (message.text or "").strip()
        try:
            target_id = int(target)
        except ValueError:
            context.user_data.pop("admin_action", None)
            await message.reply_text("Customer ID is invalid.")
            return True
        if not text:
            await message.reply_text("Message cannot be empty.")
            return True
        if len(text) > 4000:
            await message.reply_text("Message must be 4000 characters or fewer.")
            return True
        with transaction() as conn:
            customer = conn.execute(
                "SELECT telegram_id, is_blocked FROM users WHERE telegram_id = ?",
                (target_id,),
            ).fetchone()
            if customer is None:
                context.user_data.pop("admin_action", None)
                await message.reply_text("Customer not found.")
                return True
            conn.execute(
                """INSERT INTO customer_messages
                   (telegram_id, sender_type, message, created_at)
                   VALUES (?, 'ADMIN', ?, ?)""",
                (target_id, text, utc_now()),
            )
            add_activity(
                conn,
                target_id,
                "ADMIN_MESSAGE_SENT",
                {"message_length": len(text)},
            )
        context.user_data.pop("admin_action", None)
        if customer["is_blocked"]:
            await message.reply_text(
                "Message saved to conversation history, but this customer is blocked by the bot. "
                "Unblock the customer before sending."
            )
            return True
        try:
            await context.bot.send_message(
                target_id,
                f"💬 <b>CodeBazar Support</b>\n━━━━━━━━━━━━━━━━━━━━\n{escape(text)}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            logger.exception("Could not send admin message to customer %s", target_id)
            await message.reply_text(
                "Message was saved, but Telegram could not deliver it. The customer may have blocked the bot."
            )
            return True
        await message.reply_text("Private message sent.")
        return True
    if action == "activity_filter_customer":
        value = (message.text or "").strip()
        if value == "0":
            context.user_data.setdefault("activity_filters", {})["telegram_id"] = None
        elif value.isdigit() and int(value) > 0:
            context.user_data.setdefault("activity_filters", {})["telegram_id"] = int(value)
        else:
            await message.reply_text("Enter a positive numeric Telegram ID, or 0 to clear.")
            return True
        context.user_data.pop("admin_action", None)
        await _show_activity_feed(message, 0, context)
        return True
    if action == "activity_filter_date":
        value = (message.text or "").strip()
        if value == "0":
            context.user_data.setdefault("activity_filters", {})["date"] = None
        else:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                await message.reply_text("Enter a valid date in YYYY-MM-DD format, or 0 to clear.")
                return True
            context.user_data.setdefault("activity_filters", {})["date"] = value
        context.user_data.pop("admin_action", None)
        await _show_activity_feed(message, 0, context)
        return True
    if action == "search_customer_order":
        value = (message.text or "").strip()
        context.user_data.pop("admin_action", None)
        with connection() as conn:
            order = conn.execute(
                "SELECT id FROM orders WHERE upper(id) = upper(?)", (value,)
            ).fetchone()
            if order is None:
                if value.isdigit():
                    customer = conn.execute(
                        "SELECT telegram_id FROM users WHERE telegram_id = ?",
                        (int(value),),
                    ).fetchone()
                else:
                    customer = conn.execute(
                        "SELECT telegram_id FROM users WHERE lower(username) = lower(?)",
                        (value.lstrip("@"),),
                    ).fetchone()
            else:
                customer = None
        if order:
            await _show_order(message, order["id"])
        elif customer:
            await _show_customer(message, customer["telegram_id"])
        else:
            await message.reply_text("No matching customer or order was found.")
        return True
    if action == "qr":
        if not message.photo:
            await message.reply_text("Please send the QR image as a photo.")
            return True
        config.QR_DIRECTORY.mkdir(parents=True, exist_ok=True)
        temp_path = config.QR_IMAGE_PATH.with_name(config.QR_IMAGE_PATH.stem + ".upload")
        try:
            telegram_file = await message.photo[-1].get_file()
            await telegram_file.download_to_drive(custom_path=str(temp_path))
            temp_path.replace(config.QR_IMAGE_PATH)
        except (OSError, TelegramError):
            logger.exception("Could not save uploaded payment QR")
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    logger.exception("Could not remove temporary QR upload %s", temp_path)
            await message.reply_text("The QR image could not be saved. Please try uploading it again.")
            return True
        context.user_data.pop("admin_action", None)
        await message.reply_text("Payment QR uploaded and stored persistently.")
        return True
    if action == "deliver":
        incoming = [code.strip() for code in (message.text or "").splitlines() if code.strip()]
        codes = context.user_data.get("delivery_codes", []) + incoming
        with connection() as conn:
            order = conn.execute("SELECT quantity FROM orders WHERE id = ?", (target,)).fetchone()
        if order is None:
            context.user_data.pop("admin_action", None)
            context.user_data.pop("delivery_codes", None)
            await message.reply_text("Order not found.")
            return True
        if not incoming:
            await message.reply_text("Send voucher codes as text, one code per line.")
            return True
        if len(codes) != len(set(codes)):
            await message.reply_text("Duplicate codes in this batch were not added. Send the remaining unique codes.")
            return True
        if len(codes) > order["quantity"]:
            await message.reply_text(
                f"Too many codes: expected {order['quantity']} total. This batch was not added."
            )
            return True
        if len(codes) < order["quantity"]:
            context.user_data["delivery_codes"] = codes
            remaining = order["quantity"] - len(codes)
            await message.reply_text(
                f"Received {len(codes)} of {order['quantity']} codes. Send {remaining} more."
            )
            return True
        try:
            delivered = deliver_codes(target, codes)
        except DeliveryError as exc:
            await message.reply_text(str(exc))
            return True
        context.user_data.pop("admin_action", None)
        context.user_data.pop("delivery_codes", None)
        customer_message = (
            "🎉 Your Order Has Been Delivered!\n\n"
            f"🧾 Order ID: {target}\n🎟 Product: {delivered['product_name']}\n"
            f"📦 Quantity: {delivered['quantity']}\n\n"
        )
        try:
            await context.bot.send_message(delivered["user_id"], customer_message)
            for codes_message in voucher_code_messages(
                target,
                delivered["codes"],
                suffix="\n\nThank you for shopping with CodeBazar! ❤️",
            ):
                await context.bot.send_message(delivered["user_id"], codes_message)
            record_order_payment_event(
                target,
                "VOUCHERS_DELIVERED",
                {"quantity": delivered["quantity"]},
            )
        except TelegramError:
            logger.exception("Could not send delivered voucher to customer for %s", target)
            await message.reply_text(
                "Codes are recorded as delivered, but Telegram could not deliver the message. "
                "The customer can retrieve the codes with their Order ID using voucher recovery."
            )
            return True
        await message.reply_text(f"Order {target} delivered and stock updated.")
        return True
    if action == "ticket_reply":
        reply = (message.text or "").strip()
        if not reply:
            await message.reply_text("Reply cannot be empty.")
            return True
        telegram_message_id = getattr(message, "message_id", None)
        try:
            with transaction() as conn:
                if telegram_message_id is not None and conn.execute(
                    """SELECT 1 FROM support_messages
                       WHERE sender_id = ? AND telegram_message_id = ?""",
                    (config.ADMIN_ID, telegram_message_id),
                ).fetchone():
                    return True
                ticket = conn.execute(
                    "SELECT * FROM support_tickets WHERE id = ?", (target,)
                ).fetchone()
                if ticket is None:
                    await message.reply_text("Ticket not found.")
                    context.user_data.pop("admin_action", None)
                    return True
                now = utc_now()
                conn.execute(
                    """INSERT INTO support_messages
                       (ticket_id, sender_id, sender_type, message, created_at,
                        telegram_message_id)
                       VALUES (?, ?, 'ADMIN', ?, ?, ?)""",
                    (
                        target,
                        config.ADMIN_ID,
                        reply,
                        now,
                        telegram_message_id,
                    ),
                )
                conn.execute(
                    "UPDATE support_tickets SET status = 'IN PROGRESS', updated_at = ? WHERE id = ?",
                    (now, target),
                )
                conn.execute(
                    """INSERT INTO customer_messages
                       (telegram_id, sender_type, message, created_at)
                       VALUES (?, 'ADMIN', ?, ?)""",
                    (ticket["user_id"], reply, now),
                )
                add_activity(
                    conn,
                    ticket["user_id"],
                    "ADMIN_SUPPORT_REPLY_SENT",
                    {"ticket_id": target, "message_length": len(reply)},
                    now,
                )
        except sqlite3.Error:
            logger.exception("Could not save admin reply for support ticket %s", target)
            await message.reply_text(
                "The reply could not be saved. Please try again; it has not been sent."
            )
            return True
        context.user_data.pop("admin_action", None)
        try:
            await context.bot.send_message(
                ticket["user_id"],
                f"💬 CodeBazar Support\n"
                f"🎫 Ticket: {target}\n"
                f"📌 Status: IN PROGRESS\n\n{reply}",
            )
        except TelegramError:
            logger.exception("Could not send support reply for ticket %s", target)
            await message.reply_text("The reply was saved, but Telegram could not deliver it to the customer.")
            return True
        context.user_data.pop("admin_action", None)
        await message.reply_text(f"Reply sent for {target}.")
        return True
    if action == "broadcast":
        text = (message.text or "").strip()
        if not text:
            await message.reply_text("Broadcast text cannot be empty.")
            return True
        if len(text) > 4000:
            await message.reply_text("Broadcast text must be 4000 characters or fewer.")
            return True
        with connection() as conn:
            users = conn.execute("SELECT telegram_id FROM users").fetchall()
        sent = 0
        failed = 0
        for user in users:
            try:
                await context.bot.send_message(user["telegram_id"], text)
                sent += 1
            except TelegramError:
                logger.warning("Broadcast delivery failed for user %s", user["telegram_id"])
                failed += 1
        context.user_data.pop("admin_action", None)
        await message.reply_text(f"Broadcast complete. Sent: {sent}; failed: {failed}.")
        return True
    if action == "search_order":
        order_id = (message.text or "").strip().upper()
        context.user_data.pop("admin_action", None)
        with connection() as conn:
            order = conn.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order:
            await _show_order(message, order_id)
        else:
            await message.reply_text("No matching Order ID was found.")
        return True

    text = (message.text or "").strip()
    if action in ("edit_name", "edit_price", "stock_add", "stock_set"):
        product = get_product(target)
        if product is None:
            context.user_data.pop("admin_action", None)
            await message.reply_text("Product not found.")
            return True
        try:
            if action == "edit_name":
                if not text or len(text) > 100:
                    raise ValueError("Product name must contain 1 to 100 characters.")
                with transaction() as conn:
                    conn.execute("UPDATE products SET name = ? WHERE id = ?", (text, target))
            elif action == "edit_price":
                if not re.fullmatch(r"\d{1,7}", text):
                    raise ValueError("Enter a whole-number price from ₹0 to ₹9,999,999.")
                with transaction() as conn:
                    conn.execute("UPDATE products SET price = ? WHERE id = ?", (int(text), target))
            elif action == "stock_add":
                if not re.fullmatch(r"[1-9]\d{0,6}", text):
                    raise ValueError("Enter a positive whole number of codes to add.")
                with transaction() as conn:
                    conn.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (int(text), target))
            else:
                if not re.fullmatch(r"\d{1,7}", text):
                    raise ValueError("Enter a non-negative whole-number stock quantity.")
                with transaction() as conn:
                    reserved = conn.execute(
                        "SELECT COALESCE(SUM(quantity), 0) AS qty FROM orders WHERE product_id = ? AND reserved = 1",
                        (target,),
                    ).fetchone()["qty"]
                    if int(text) < reserved:
                        raise ValueError(f"Stock cannot be set below {reserved} currently reserved code(s).")
                    conn.execute("UPDATE products SET stock = ? WHERE id = ?", (int(text), target))
        except ValueError as exc:
            await message.reply_text(str(exc))
            return True
        context.user_data.pop("admin_action", None)
        await message.reply_text("Product updated.", reply_markup=back_home())
        return True
    return False


async def _show_products(message) -> None:
    products = get_all_products()
    keyboard = [
        [InlineKeyboardButton(f"{p['name']} — {'ON' if p['enabled'] else 'OFF'}", callback_data=f"adm:product:{p['id']}")]
        for p in products
    ]
    keyboard.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm:home")])
    await message.reply_text(
        "<b>📦 PRODUCT CATALOG</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        "Select a product to update its details or visibility.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def _show_product(message, product_id: str) -> None:
    product = get_product(product_id)
    if product is None:
        await message.reply_text("Product not found.")
        return
    keyboard = [
        [InlineKeyboardButton("✏️ Edit Name", callback_data=f"adm:edit_name:{product_id}"),
         InlineKeyboardButton("💰 Edit Price", callback_data=f"adm:edit_price:{product_id}")],
        [InlineKeyboardButton("🎟 Manage Stock", callback_data=f"adm:stock_product:{product_id}"),
         InlineKeyboardButton("Enable/Disable", callback_data=f"adm:toggle:{product_id}")],
        [InlineKeyboardButton("🔙 Products", callback_data="adm:products")],
    ]
    await message.reply_text(
        f"<b>📦 {escape(product['name'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Price: <b>₹{product['price']}</b>\n"
        f"🗃️ Total stock: {product['stock']}\n"
        f"✅ Available: {max(0, product['stock'] - _reserved(product_id))}\n"
        f"Visibility: {'🟢 Enabled' if product['enabled'] else '⚪ Disabled'}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


def _reserved(product_id: str) -> int:
    product = get_product(product_id)
    if product is None:
        return 0
    return product["stock"] - current_available_stock(product_id)


async def _show_stock(message) -> None:
    keyboard = [
        [InlineKeyboardButton(f"{p['name']} — {p['stock']} total / {max(0, p['stock'] - _reserved(p['id']))} available",
                              callback_data=f"adm:stock_product:{p['id']}")]
        for p in get_all_products()
    ]
    keyboard.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm:home")])
    await message.reply_text(
        "<b>🗃️ STOCK CONTROL</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        "Select a product to add codes or update its total.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def _show_stock_product(message, product_id: str) -> None:
    product = get_product(product_id)
    if product is None:
        await message.reply_text("Product not found.")
        return
    keyboard = [
        [InlineKeyboardButton("➕ Add stock", callback_data=f"adm:stock_add:{product_id}"),
         InlineKeyboardButton("📝 Set total stock", callback_data=f"adm:stock_set:{product_id}")],
        [InlineKeyboardButton("🔙 Stock list", callback_data="adm:stock")],
    ]
    await message.reply_text(
        f"<b>🗃️ {escape(product['name'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Total: <b>{product['stock']}</b>  ·  Reserved: {_reserved(product_id)}\n"
        f"Available to sell: <b>{max(0, product['stock'] - _reserved(product_id))}</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def _show_page(message, kind: str, page: int) -> None:
    offset = page * PAGE_SIZE
    with connection() as conn:
        if kind == "orders":
            rows = conn.execute(
                "SELECT id, product_name, quantity, total_amount, payment_status FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (PAGE_SIZE + 1, offset),
            ).fetchall()
            title = "🛒 All Orders"
            buttons = [[InlineKeyboardButton(f"{r['id']} — {r['payment_status']}", callback_data=f"adm:order:{r['id']}")] for r in rows[:PAGE_SIZE]]
        elif kind == "pending":
            rows = conn.execute(
                """SELECT o.id, o.product_name, o.quantity, o.total_amount
                   FROM orders o WHERE o.payment_status IN
                     ('PAYMENT_PENDING', 'UTR_SUBMITTED', 'PENDING_VERIFICATION')
                   ORDER BY o.created_at DESC LIMIT ? OFFSET ?""",
                (PAGE_SIZE + 1, offset),
            ).fetchall()
            title = "💳 Pending Payments"
            buttons = [[InlineKeyboardButton(f"{r['id']} — ₹{r['total_amount']}", callback_data=f"adm:order:{r['id']}")] for r in rows[:PAGE_SIZE]]
        elif kind == "users":
            rows = conn.execute(
                "SELECT telegram_id, first_name, username FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (PAGE_SIZE + 1, offset),
            ).fetchall()
            title = "👥 Registered Users"
            buttons = []
        else:
            rows = conn.execute(
                """SELECT t.id, t.category, t.status, u.first_name, u.username, t.user_id
                   FROM support_tickets t JOIN users u ON u.telegram_id = t.user_id
                   ORDER BY t.created_at DESC LIMIT ? OFFSET ?""",
                (PAGE_SIZE + 1, offset),
            ).fetchall()
            title = "💬 Support Tickets"
            buttons = [
                [
                    InlineKeyboardButton(
                        f"{r['id']} · {r['first_name'][:18]} · {r['status']}",
                        callback_data=f"adm:ticket:{r['id']}",
                    )
                ]
                for r in rows[:PAGE_SIZE]
            ]
            buttons.append([InlineKeyboardButton("📥 Customer Inbox", callback_data="adm:inbox:0")])
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    if kind == "users":
        summary = "\n".join(
            f"👤 {escape(r['first_name'])} "
            f"({'@' + escape(r['username']) if r['username'] else 'no username'}) "
            f"— <code>{r['telegram_id']}</code>"
            for r in rows
        )
    elif kind == "orders":
        summary = "\n".join(
            f"🧾 <code>{escape(r['id'])}</code> · {escape(r['product_name'])} · "
            f"Qty {r['quantity']} · ₹{r['total_amount']} · "
            f"{escape(r['payment_status'].replace('_', ' ').title())}"
            for r in rows
        )
        buttons.append([InlineKeyboardButton("🔎 Search Order ID", callback_data="adm:search_order")])
    elif kind == "pending":
        summary = "\n".join(
            f"💳 <code>{escape(r['id'])}</code> · {escape(r['product_name'])} · "
            f"Qty {r['quantity']} · ₹{r['total_amount']}"
            for r in rows
        )
    else:
        summary = "\n".join(
            f"🎫 <code>{escape(r['id'])}</code> · {escape(r['first_name'])} · "
            f"{escape(r['category'])} · {escape(r['status'].replace('_', ' ').title())} · "
            f"<code>{r['user_id']}</code>"
            for r in rows
        )
    if not rows:
        summary = "No records found."
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adm:{kind}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:{kind}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm:home")])
    await message.reply_text(
        f"<b>{title}</b>\n━━━━━━━━━━━━━━━━━━━━\n{summary}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _show_order(message, order_id: str) -> None:
    with connection() as conn:
        order = conn.execute(
            """SELECT o.*, u.first_name, u.username, p.utr, p.submitted_at
               FROM orders o JOIN users u ON u.telegram_id = o.user_id
               LEFT JOIN payments p ON p.order_id = o.id WHERE o.id = ?""",
            (order_id,),
        ).fetchone()
        delivery = conn.execute(
            "SELECT codes_json, delivered_at FROM voucher_deliveries WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if order is None:
        await message.reply_text("Order not found.")
        return
    text = (
        f"🧾 {order['id']}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Customer: {order['first_name']} ({order['user_id']})\n"
        f"🔖 Username: @{order['username'] or 'not set'}\n"
        f"🎟️ Product: {order['product_name']}\n"
        f"📦 Quantity: {order['quantity']}  ·  💰 ₹{order['total_amount']}.00\n"
        f"💳 Payment: {order['payment_status']}  ·  📬 {order['delivery_status']}\n"
        f"🔎 UTR: {order['utr'] or 'Not submitted'}"
    )
    if delivery is not None:
        codes = json.loads(delivery["codes_json"])
        text += f"\nDelivered at: {delivery['delivered_at']}\nCodes:\n" + "\n".join(codes)
    if len(text) > 3900:
        text = text[:3800] + "\n\n[Order details truncated; voucher recovery retains the full codes.]"
    buttons = []
    if order["payment_status"] in ("UTR_SUBMITTED", "PENDING_VERIFICATION"):
        buttons.append([
            InlineKeyboardButton("✅ Approve", callback_data=f"adm:approve:{order_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"adm:reject:{order_id}"),
        ])
    if order["payment_status"] == "APPROVED" and order["delivery_status"] != "DELIVERED":
        buttons.append([InlineKeyboardButton("📩 SEND VOUCHER CODES", callback_data=f"adm:deliver:{order_id}")])
    buttons.append([InlineKeyboardButton("🔙 Orders", callback_data="adm:orders:0")])
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def _approve(message, order_id: str, context) -> None:
    try:
        order = approve_payment(order_id)
    except PaymentError as exc:
        await message.reply_text(str(exc))
        return
    customer_notified = True
    try:
        await context.bot.send_message(
            order["user_id"],
            "✅ Payment Approved!\n\n"
            f"🧾 Order ID: {order_id}\n\nYour payment has been verified successfully.\n\n"
            "Your voucher will be delivered shortly.",
        )
    except TelegramError:
        logger.exception("Could not notify customer about approved payment %s", order_id)
        customer_notified = False
    await message.reply_text(
        f"Payment approved for {order_id}. Manually verified by admin."
        + ("" if customer_notified else " Customer notification failed; please contact them through the bot."),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📩 SEND VOUCHER CODES", callback_data=f"adm:deliver:{order_id}")]]
        ),
    )


async def _reject(message, order_id: str, context) -> None:
    customer_notified = True
    try:
        order = reject_payment(order_id)
    except PaymentError as exc:
        await message.reply_text(str(exc))
        return
    try:
        await context.bot.send_message(
            order["user_id"],
            f"❌ Payment for order {order_id} could not be verified and was rejected.\n"
            "If you need help, please contact CodeBazar Support.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💬 Contact Support", callback_data="support:other")]]
            ),
        )
    except TelegramError:
        logger.exception("Could not notify customer about rejected payment %s", order_id)
        customer_notified = False
    await message.reply_text(
        f"Payment rejected for {order_id}. The reserved stock was released."
        + ("" if customer_notified else " Customer notification failed.")
    )


async def _show_report(message) -> None:
    with connection() as conn:
        counts = {
            "users": conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"],
            "orders": conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"],
            "approved": conn.execute("SELECT COUNT(*) AS n FROM orders WHERE payment_status IN ('APPROVED','DELIVERED')").fetchone()["n"],
            "pending": conn.execute(
                """SELECT COUNT(*) AS n FROM orders
                   WHERE payment_status IN ('UTR_SUBMITTED', 'PENDING_VERIFICATION')"""
            ).fetchone()["n"],
            "codes": conn.execute("SELECT COUNT(*) AS n FROM voucher_codes").fetchone()["n"],
            "sales": conn.execute("SELECT COALESCE(SUM(total_amount), 0) AS n FROM orders WHERE payment_status IN ('APPROVED','DELIVERED')").fetchone()["n"],
        }
    await message.reply_text(
        "<b>📊 STORE PERFORMANCE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Customers: <b>{counts['users']}</b>\n"
        f"📋 Orders: <b>{counts['orders']}</b>\n"
        f"✅ Approved payments: <b>{counts['approved']}</b>\n"
        f"⏳ Pending verification: <b>{counts['pending']}</b>\n"
        f"🎟️ Codes delivered: <b>{counts['codes']}</b>\n"
        f"💰 Total sales: <b>₹{counts['sales']}.00</b>",
        reply_markup=back_home(),
        parse_mode=ParseMode.HTML,
    )


async def _show_settings(message) -> None:
    qr_state = "Uploaded" if config.QR_IMAGE_PATH.is_file() else "Not uploaded"
    admin_state = "Enabled" if config.ADMIN_ID is not None else "Disabled — set ADMIN_ID in .env"
    await message.reply_text(
        "<b>⚙️ STORE SETTINGS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔐 Admin access: <b>{escape(admin_state)}</b>\n"
        f"⏳ Payment session: <b>{config.PAYMENT_EXPIRY_MINUTES} minutes</b>\n"
        f"🖼️ Payment QR: <b>{qr_state}</b>\n"
        "🗃️ Database: persistent SQLite\n\n"
        "The payment verification window and manual-verification requirement are fixed.",
        reply_markup=back_home(),
        parse_mode=ParseMode.HTML,
    )


async def _show_ticket(message, ticket_id: str) -> None:
    with connection() as conn:
        ticket = conn.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        customer = conn.execute(
            "SELECT first_name, username FROM users WHERE telegram_id = ?",
            (ticket["user_id"],),
        ).fetchone() if ticket else None
        messages = conn.execute(
            "SELECT sender_type, message, created_at FROM support_messages WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
    if ticket is None:
        await message.reply_text("Ticket not found.")
        return
    history_entries = []
    remaining = 3000
    for item in reversed(messages):
        header = f"<b>{escape(item['sender_type'])}</b> · {escape(item['created_at'])}\n"
        body = _escape_html_limited(item["message"], max(0, remaining - len(header) - 2))
        entry = header + body
        if len(entry) + 2 > remaining:
            break
        history_entries.append(entry)
        remaining -= len(entry) + 2
    history = "\n\n".join(reversed(history_entries))
    keyboard = [[InlineKeyboardButton("💬 REPLY", callback_data=f"adm:ticket_reply:{ticket_id}")]]
    if ticket["status"] != "RESOLVED":
        keyboard.append([InlineKeyboardButton("✅ MARK RESOLVED", callback_data=f"adm:ticket_resolve:{ticket_id}")])
    keyboard.append([InlineKeyboardButton("🔙 Tickets", callback_data="adm:tickets:0")])
    text = (
        f"<b>🎫 {escape(ticket_id)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Category: {escape(ticket['category'])}\n"
        f"Customer: {escape(customer['first_name'])} "
        f"({'@' + escape(customer['username']) if customer['username'] else 'no username'}) · "
        f"<code>{ticket['user_id']}</code>\n"
        f"Status: <b>{escape(ticket['status'])}</b>\n"
        f"Order: <code>{escape(ticket['order_id'] or 'Not provided')}</code>\n\n"
        f"{history}"
    )
    await message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
    )


async def _resolve_ticket(message, ticket_id: str, context) -> None:
    try:
        with transaction() as conn:
            ticket = conn.execute(
                "SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if ticket is None:
                await message.reply_text("Ticket not found.")
                return
            if ticket["status"] == "RESOLVED":
                await message.reply_text("Ticket is already resolved.")
                return
            conn.execute(
                "UPDATE support_tickets SET status = 'RESOLVED', updated_at = ? WHERE id = ?",
                (utc_now(), ticket_id),
            )
            conn.execute(
                """INSERT INTO support_messages
                   (ticket_id, sender_id, sender_type, message, created_at)
                   VALUES (?, ?, 'ADMIN', ?, ?)""",
                (ticket_id, config.ADMIN_ID, "Ticket marked resolved.", utc_now()),
            )
    except sqlite3.Error:
        logger.exception("Could not resolve support ticket %s", ticket_id)
        await message.reply_text("The ticket could not be resolved right now. Please try again.")
        return
    await message.reply_text(f"Ticket {ticket_id} marked resolved.")
    try:
        await context.bot.send_message(
            ticket["user_id"],
            f"✅ Your CodeBazar Support ticket {ticket_id} has been marked resolved. "
            "If you need more help, please open a new support ticket.",
        )
    except TelegramError:
        logger.exception("Could not notify customer that ticket %s was resolved", ticket_id)


async def _show_overview(message) -> None:
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_start = (now - timedelta(days=now.weekday())).date().isoformat()
    month_start = now.replace(day=1).date().isoformat()
    with connection() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users WHERE telegram_id != ?", (config.ADMIN_ID,)).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE telegram_id != ? AND last_active_at >= ?",
            (config.ADMIN_ID, (now - timedelta(minutes=15)).isoformat(timespec="seconds")),
        ).fetchone()[0]
        orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE payment_status = 'APPROVED'"
        ).fetchone()[0]
        pending = conn.execute(
            """SELECT COUNT(*) FROM orders
               WHERE payment_status IN ('PAYMENT_PENDING', 'UTR_SUBMITTED', 'PENDING_VERIFICATION')"""
        ).fetchone()[0]
        revenue = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN substr(COALESCE(p.reviewed_at, o.created_at), 1, 10) = ? THEN o.total_amount ELSE 0 END), 0) AS today,
                 COALESCE(SUM(CASE WHEN substr(COALESCE(p.reviewed_at, o.created_at), 1, 10) >= ? THEN o.total_amount ELSE 0 END), 0) AS week,
                 COALESCE(SUM(CASE WHEN substr(COALESCE(p.reviewed_at, o.created_at), 1, 7) = ? THEN o.total_amount ELSE 0 END), 0) AS month,
                 COALESCE(SUM(o.total_amount), 0) AS total
               FROM orders o LEFT JOIN payments p ON p.order_id = o.id
               WHERE o.payment_status = 'APPROVED'""",
            (today, week_start, month_start[:7]),
        ).fetchone()
    await message.reply_text(
        "<b>📊 CODEBAZAR OVERVIEW</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Customers: <b>{users}</b>  ·  🟢 Active in 15m: <b>{active}</b>\n"
        f"📦 Successful orders: <b>{orders}</b>  ·  ⏳ Pending: <b>{pending}</b>\n\n"
        "💰 <b>Verified revenue only</b>\n"
        f"Today ₹{revenue['today']}  ·  This week ₹{revenue['week']}\n"
        f"This month ₹{revenue['month']}  ·  All time ₹{revenue['total']}",
        reply_markup=dashboard(),
        parse_mode=ParseMode.HTML,
    )


async def _show_customers(message, page: int) -> None:
    page = max(0, page)
    with connection() as conn:
        rows = conn.execute(
            """SELECT u.telegram_id, u.first_name, u.username, u.created_at, u.is_blocked,
                      COUNT(o.id) AS order_count
               FROM users u LEFT JOIN orders o ON o.user_id = u.telegram_id
               WHERE u.telegram_id != ?
               GROUP BY u.telegram_id ORDER BY COALESCE(u.last_active_at, u.created_at) DESC
               LIMIT ? OFFSET ?""",
            (config.ADMIN_ID, PAGE_SIZE + 1, page * PAGE_SIZE),
        ).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    buttons = [
        [
            InlineKeyboardButton(
                f"{'🚫' if row['is_blocked'] else '👤'} {row['first_name'][:24]} · {row['order_count']} orders",
                callback_data=f"adm:customer:{row['telegram_id']}",
            )
        ]
        for row in rows
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adm:customers:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:customers:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm:home")])
    summary = "\n".join(
        f"@{escape(row['username']) if row['username'] else 'no username'} · "
        f"{row['created_at'][:10]}"
        for row in rows
    ) or "No customers found."
    await message.reply_text(
        f"<b>👥 CUSTOMERS · PAGE {page + 1}</b>\n━━━━━━━━━━━━━━━━━━━━\n{summary}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _show_customer(message, telegram_id: int) -> None:
    with connection() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ? AND telegram_id != ?",
            (telegram_id, config.ADMIN_ID),
        ).fetchone()
        if user is None:
            await message.reply_text("Customer not found.")
            return
        stats = conn.execute(
            """SELECT COUNT(*) AS order_count,
                      COALESCE(SUM(CASE WHEN payment_status = 'APPROVED' THEN quantity ELSE 0 END), 0) AS purchased_qty,
                      COALESCE(SUM(CASE WHEN payment_status = 'APPROVED' THEN total_amount ELSE 0 END), 0) AS verified,
                      COALESCE(SUM(CASE WHEN payment_status IN
                        ('PAYMENT_PENDING', 'UTR_SUBMITTED', 'PENDING_VERIFICATION')
                        THEN total_amount ELSE 0 END), 0) AS pending,
                      COALESCE(SUM(CASE WHEN payment_status = 'REJECTED' THEN total_amount ELSE 0 END), 0) AS rejected
               FROM orders WHERE user_id = ?""",
            (telegram_id,),
        ).fetchone()
        delivered = conn.execute(
            "SELECT COUNT(*) FROM voucher_codes vc JOIN orders o ON o.id = vc.order_id WHERE o.user_id = ?",
            (telegram_id,),
        ).fetchone()[0]
    username = f"@{escape(user['username'])}" if user["username"] else "Not set"
    buttons = [
        [
            InlineKeyboardButton("📦 View Orders", callback_data=f"adm:customer_orders:{telegram_id}:0"),
            InlineKeyboardButton("💳 View Payments", callback_data=f"adm:customer_payments:{telegram_id}:0"),
        ],
        [
            InlineKeyboardButton("🕘 View Activity", callback_data=f"adm:customer_activity:{telegram_id}:0"),
            InlineKeyboardButton("💬 Conversation", callback_data=f"adm:customer_messages:{telegram_id}"),
        ],
        [
            InlineKeyboardButton("✉️ Send Message", callback_data=f"adm:customer_message:{telegram_id}"),
            InlineKeyboardButton("↩️ Reply to Customer", callback_data=f"adm:customer_message:{telegram_id}"),
        ],
        [
            InlineKeyboardButton(
                "🔓 Unblock Customer" if user["is_blocked"] else "🚫 Block Customer",
                callback_data=f"adm:customer_block:{telegram_id}",
            )
        ],
        [InlineKeyboardButton("🔙 Customers", callback_data="adm:customers:0")],
    ]
    await message.reply_text(
        "<b>👤 CUSTOMER PROFILE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Name: <b>{escape(user['first_name'])}</b>\n"
        f"Username: {username}\n"
        f"Telegram ID: <code>{telegram_id}</code>\n"
        f"Registered: {user['created_at']}\n"
        f"Last active: {user['last_active_at'] or 'Not recorded'}\n"
        f"Bot access: {'🚫 Blocked' if user['is_blocked'] else '🟢 Active'}\n\n"
        f"📦 Orders: <b>{stats['order_count']}</b> · Quantity purchased: <b>{stats['purchased_qty']}</b>\n"
        f"✅ Verified payments: <b>₹{stats['verified']}</b>\n"
        f"⏳ Pending payments: <b>₹{stats['pending']}</b>\n"
        f"❌ Rejected payments: <b>₹{stats['rejected']}</b>\n"
        f"🎟 Delivered codes: <b>{delivered}</b>",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _show_customer_orders(message, telegram_id: int, page: int) -> None:
    with connection() as conn:
        rows = conn.execute(
            """SELECT id, product_name, quantity, total_amount, payment_status, delivery_status
               FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (telegram_id, PAGE_SIZE + 1, max(0, page) * PAGE_SIZE),
        ).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    buttons = [
        [InlineKeyboardButton(f"{row['id']} · {row['payment_status']}", callback_data=f"adm:order:{row['id']}")]
        for row in rows
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adm:customer_orders:{telegram_id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:customer_orders:{telegram_id}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("👤 Customer Profile", callback_data=f"adm:customer:{telegram_id}")])
    text = "\n".join(
        f"🧾 <code>{escape(row['id'])}</code> · {escape(row['product_name'])}\n"
        f"Qty {row['quantity']} · ₹{row['total_amount']} · "
        f"{escape(row['payment_status'])} · {escape(row['delivery_status'])}"
        for row in rows
    ) or "No orders found."
    await message.reply_text(
        f"<b>📦 CUSTOMER ORDERS</b>\n━━━━━━━━━━━━━━━━━━━━\n{text}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _show_customer_payments(message, telegram_id: int, page: int) -> None:
    with connection() as conn:
        rows = conn.execute(
            """SELECT o.id, o.product_name, o.quantity, o.total_amount, o.payment_status,
                      p.utr, p.submitted_at, p.reviewed_at
               FROM orders o LEFT JOIN payments p ON p.order_id = o.id
               WHERE o.user_id = ? AND (p.order_id IS NOT NULL OR o.payment_screen_at IS NOT NULL)
               ORDER BY o.created_at DESC LIMIT ? OFFSET ?""",
            (telegram_id, PAGE_SIZE + 1, max(0, page) * PAGE_SIZE),
        ).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    buttons = [
        [InlineKeyboardButton(f"{row['id']} · {row['payment_status']}", callback_data=f"adm:payment:{row['id']}")]
        for row in rows
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adm:customer_payments:{telegram_id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:customer_payments:{telegram_id}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("👤 Customer Profile", callback_data=f"adm:customer:{telegram_id}")])
    summary = "\n".join(
        f"💳 <code>{escape(row['id'])}</code> · ₹{row['total_amount']} · "
        f"{escape(row['payment_status'])} · UTR {escape(row['utr'] or 'Not submitted')}"
        for row in rows
    ) or "No payment records found."
    await message.reply_text(
        f"<b>💳 CUSTOMER PAYMENTS</b>\n━━━━━━━━━━━━━━━━━━━━\n{summary}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _show_customer_activity(message, telegram_id: int, page: int) -> None:
    await _render_activity(message, max(0, page), telegram_id=telegram_id)


async def _render_activity(
    message,
    page: int,
    telegram_id: int | None = None,
    event_type: str | None = None,
    date: str | None = None,
) -> None:
    clauses = []
    values: list[Any] = []
    if telegram_id is not None:
        clauses.append("a.telegram_id = ?")
        values.append(telegram_id)
    if event_type:
        clauses.append("a.event_type = ?")
        values.append(event_type)
    if date:
        clauses.append("substr(a.created_at, 1, 10) = ?")
        values.append(date)
    clauses.append("a.telegram_id != ?")
    values.append(config.ADMIN_ID)
    where = " WHERE " + " AND ".join(clauses)
    with connection() as conn:
        rows = conn.execute(
            """SELECT a.telegram_id, a.event_type, a.details_json, a.created_at,
                      u.first_name, u.username
               FROM user_activity a JOIN users u ON u.telegram_id = a.telegram_id"""
            + where
            + " ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            (*values, PAGE_SIZE + 1, page * PAGE_SIZE),
        ).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    entries = []
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except (TypeError, json.JSONDecodeError):
            details = {}
        extras = " · ".join(
            f"{escape(str(key).replace('_', ' ').title())}: {escape(str(value))}"
            for key, value in details.items()
            if key not in {"utr", "message"} and value not in (None, "")
        )
        entries.append(
            f"🕒 <code>{escape(row['created_at'])}</code>\n"
            f"🟢 <b>{escape(row['event_type'].replace('_', ' ').title())}</b>\n"
            f"👤 {escape(row['first_name'])} "
            f"({'@' + escape(row['username']) if row['username'] else 'no username'}) · "
            f"<code>{row['telegram_id']}</code>"
            + (f"\n{extras}" if extras else "")
        )
    buttons = []
    if page:
        buttons.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"adm:customer_activity:{telegram_id}:{page - 1}")
        )
    if has_next:
        buttons.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"adm:customer_activity:{telegram_id}:{page + 1}")
        )
    keyboard = []
    if buttons:
        keyboard.append(buttons)
    if telegram_id is not None:
        keyboard.append(
            [InlineKeyboardButton("👤 Customer Profile", callback_data=f"adm:customer:{telegram_id}")]
        )
    await message.reply_text(
        "<b>🟢 CUSTOMER ACTIVITY</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        + ("\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(entries) if entries else "No activity found."),
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        parse_mode=ParseMode.HTML,
    )


async def _show_activity_feed(message, page: int, context) -> None:
    filters = context.user_data.get("activity_filters", {})
    customer_id = filters.get("telegram_id")
    event_type = filters.get("event_type")
    date = filters.get("date")
    clauses = []
    values: list[Any] = []
    if customer_id:
        clauses.append("a.telegram_id = ?")
        values.append(customer_id)
    if event_type:
        clauses.append("a.event_type = ?")
        values.append(event_type)
    if date:
        clauses.append("substr(a.created_at, 1, 10) = ?")
        values.append(date)
    clauses.append("a.telegram_id != ?")
    values.append(config.ADMIN_ID)
    where = " WHERE " + " AND ".join(clauses)
    with connection() as conn:
        rows = conn.execute(
            """SELECT a.telegram_id, a.event_type, a.details_json, a.created_at,
                      u.first_name, u.username
               FROM user_activity a JOIN users u ON u.telegram_id = a.telegram_id"""
            + where
            + " ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            (*values, PAGE_SIZE + 1, max(0, page) * PAGE_SIZE),
        ).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    entries = []
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except (TypeError, json.JSONDecodeError):
            details = {}
        extras = " · ".join(
            f"{escape(str(key).replace('_', ' ').title())}: {escape(str(value))}"
            for key, value in details.items()
            if key not in {"utr", "message"} and value not in (None, "")
        )
        entries.append(
            f"🕒 <code>{escape(row['created_at'])}</code>\n"
            f"🟢 <b>{escape(row['event_type'].replace('_', ' ').title())}</b>\n"
            f"👤 {escape(row['first_name'])} "
            f"({'@' + escape(row['username']) if row['username'] else 'no username'}) · "
            f"<code>{row['telegram_id']}</code>"
            + (f"\n{extras}" if extras else "")
        )
    buttons = [
        [
            InlineKeyboardButton("👤 Customer", callback_data="adm:activity_customer"),
            InlineKeyboardButton("🏷️ Event", callback_data="adm:activity_type"),
            InlineKeyboardButton("🗓 Date", callback_data="adm:activity_date"),
        ],
        [
            InlineKeyboardButton("🧹 Clear Filters", callback_data="adm:activity_clear"),
            InlineKeyboardButton("🔙 Dashboard", callback_data="adm:home"),
        ],
        [
            InlineKeyboardButton("All Events", callback_data="adm:activity_event:all"),
            InlineKeyboardButton("Product Viewed", callback_data="adm:activity_event:PRODUCT_VIEWED"),
        ],
        [
            InlineKeyboardButton("Payments", callback_data="adm:activity_event:UTR_SUBMITTED"),
            InlineKeyboardButton("Support", callback_data="adm:activity_event:SUPPORT_TICKET_SUBMITTED"),
        ],
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adm:live:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:live:{page + 1}"))
    if nav:
        buttons.append(nav)
    filter_labels = []
    if customer_id:
        filter_labels.append(f"Customer {customer_id}")
    if event_type:
        filter_labels.append(event_type.replace("_", " ").title())
    if date:
        filter_labels.append(date)
    label = " · ".join(filter_labels) or "All customers · all dates"
    await message.reply_text(
        f"<b>🟢 LIVE CUSTOMER ACTIVITY</b>\n<i>{escape(label)}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        + ("\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(entries) if entries else "No activity found."),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _show_customer_messages(message, telegram_id: int) -> None:
    with connection() as conn:
        rows = conn.execute(
            """SELECT sender_type, substr(message, 1, 400) AS message, created_at FROM customer_messages
               WHERE telegram_id = ? ORDER BY id DESC LIMIT 6""",
            (telegram_id,),
        ).fetchall()
    history = "\n\n".join(
        f"<b>{escape(row['sender_type'])}</b> · <code>{escape(row['created_at'])}</code>\n"
        f"{_escape_html_limited(row['message'], 300)}"
        for row in reversed(rows)
    ) or "No direct messages yet."
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("↩️ Reply", callback_data=f"adm:customer_message:{telegram_id}")],
            [InlineKeyboardButton("👤 Customer Profile", callback_data=f"adm:customer:{telegram_id}")],
        ]
    )
    await message.reply_text(
        f"<b>💬 CUSTOMER CONVERSATION</b>\n━━━━━━━━━━━━━━━━━━━━\n{history}",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def _show_customer_inbox(message, page: int) -> None:
    with connection() as conn:
        rows = conn.execute(
            """SELECT m.telegram_id, u.first_name, u.username, m.message, m.created_at
               FROM customer_messages m JOIN users u ON u.telegram_id = m.telegram_id
               WHERE m.sender_type = 'CUSTOMER' AND m.id = (
                   SELECT MAX(m2.id) FROM customer_messages m2
                   WHERE m2.telegram_id = m.telegram_id AND m2.sender_type = 'CUSTOMER'
               )
               AND m.telegram_id != ?
               ORDER BY m.created_at DESC LIMIT ? OFFSET ?""",
            (config.ADMIN_ID, PAGE_SIZE + 1, max(0, page) * PAGE_SIZE),
        ).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    buttons = [
        [
            InlineKeyboardButton(
                f"💬 {row['first_name'][:24]} · {row['created_at'][:16]}",
                callback_data=f"adm:customer_messages:{row['telegram_id']}",
            )
        ]
        for row in rows
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adm:inbox:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:inbox:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Support Tickets", callback_data="adm:tickets:0")])
    summary = "\n\n".join(
        f"👤 <b>{escape(row['first_name'])}</b> · "
        f"{'@' + escape(row['username']) if row['username'] else 'no username'} · "
        f"<code>{row['telegram_id']}</code>\n"
        f"🕒 {escape(row['created_at'])}\n"
        f"{_escape_html_limited(row['message'], 250)}"
        for row in rows
    ) or "No customer messages found."
    await message.reply_text(
        f"<b>📥 CUSTOMER INBOX · PAGE {page + 1}</b>\n━━━━━━━━━━━━━━━━━━━━\n{summary}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _toggle_customer_block(message, telegram_id: int) -> None:
    with transaction() as conn:
        user = conn.execute(
            "SELECT is_blocked FROM users WHERE telegram_id = ? AND telegram_id != ?",
            (telegram_id, config.ADMIN_ID),
        ).fetchone()
        if user is None:
            await message.reply_text("Customer not found.")
            return
        blocked = not bool(user["is_blocked"])
        conn.execute(
            "UPDATE users SET is_blocked = ? WHERE telegram_id = ?",
            (int(blocked), telegram_id),
        )
        add_activity(
            conn,
            telegram_id,
            "CUSTOMER_BLOCKED" if blocked else "CUSTOMER_UNBLOCKED",
        )
    await message.reply_text(
        f"Customer access {'blocked' if blocked else 'restored'}."
    )
    await _show_customer(message, telegram_id)


async def _show_payment_monitoring(message, page: int) -> None:
    page = max(0, page)
    with connection() as conn:
        rows = conn.execute(
            """SELECT o.id, o.user_id, o.product_name, o.quantity, o.total_amount, o.payment_status,
                      p.status AS payment_review_status
               FROM orders o LEFT JOIN payments p ON p.order_id = o.id
               WHERE o.payment_screen_at IS NOT NULL OR p.order_id IS NOT NULL
               ORDER BY COALESCE(p.submitted_at, o.payment_screen_at, o.created_at) DESC
               LIMIT ? OFFSET ?""",
            (PAGE_SIZE + 1, page * PAGE_SIZE),
        ).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    buttons = [
        [
            InlineKeyboardButton(
                f"{'⏳' if row['payment_status'] in ('PAYMENT_PENDING', 'UTR_SUBMITTED', 'PENDING_VERIFICATION') else '💳'} "
                f"{row['id']} · ₹{row['total_amount']}",
                callback_data=f"adm:payment:{row['id']}",
            )
        ]
        for row in rows
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adm:payments:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:payments:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm:home")])
    summary = "\n".join(
        f"👤 {row['user_id']} · {escape(row['product_name'])} × {row['quantity']} · "
        f"₹{row['total_amount']} · {escape(row['payment_status'])}"
        for row in rows
    ) or "No payment activity recorded."
    await message.reply_text(
        f"<b>💳 PAYMENT MONITORING · PAGE {page + 1}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{summary}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _show_payment_details(message, order_id: str) -> None:
    with connection() as conn:
        row = conn.execute(
            """SELECT o.*, u.first_name, u.username, p.utr, p.status AS payment_status_detail,
                      p.submitted_at, p.reviewed_at
               FROM orders o JOIN users u ON u.telegram_id = o.user_id
               LEFT JOIN payments p ON p.order_id = o.id WHERE o.id = ?""",
            (order_id,),
        ).fetchone()
        events = conn.execute(
            """SELECT event_type, created_at FROM payment_events
               WHERE order_id = ? ORDER BY created_at DESC LIMIT 20""",
            (order_id,),
        ).fetchall()
    if row is None:
        await message.reply_text("Payment record not found.")
        return
    buttons = []
    if row["payment_status"] in ("UTR_SUBMITTED", "PENDING_VERIFICATION"):
        buttons.append(
            [
                InlineKeyboardButton("✅ Approve Payment", callback_data=f"adm:approve:{order_id}"),
                InlineKeyboardButton("❌ Reject Payment", callback_data=f"adm:reject:{order_id}"),
            ]
        )
    buttons.extend(
        [
            [InlineKeyboardButton("📦 View Order", callback_data=f"adm:order:{order_id}")],
            [InlineKeyboardButton("✉️ Message Customer", callback_data=f"adm:customer_message:{row['user_id']}")],
            [InlineKeyboardButton("🔙 Payment Monitoring", callback_data="adm:payments:0")],
        ]
    )
    timeline = "\n".join(
        f"• {escape(event['created_at'])} · {escape(event['event_type'].replace('_', ' ').title())}"
        for event in reversed(events)
    ) or "No payment events recorded."
    await message.reply_text(
        "<b>💳 PAYMENT RECORD</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Customer: <b>{escape(row['first_name'])}</b> "
        f"({'@' + escape(row['username']) if row['username'] else 'no username'})\n"
        f"Telegram ID: <code>{row['user_id']}</code>\n"
        f"Order: <code>{escape(row['id'])}</code>\n"
        f"Product: {escape(row['product_name'])} · Qty {row['quantity']}\n"
        f"Exact amount: <b>₹{row['total_amount']}.00</b>\n\n"
        f"Payment screen reached: {row['payment_screen_at'] or 'Not recorded'}\n"
        f"QR sent: {row['qr_sent_at'] or 'Not recorded'}\n"
        f"I've Paid click: {row['paid_clicked_at'] or 'Not recorded'}\n"
        f"UTR submitted: {row['utr_submitted_at'] or row['submitted_at'] or 'Not submitted'}\n"
        f"UTR: <code>{escape(row['utr'] or 'Not submitted')}</code>\n"
        f"Status: <b>{escape(row['payment_status_detail'] or row['payment_status'])}</b>\n"
        f"Reviewed at: {row['reviewed_at'] or 'Not reviewed'}\n\n"
        f"<b>Event history</b>\n{timeline}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _show_revenue(message) -> None:
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_start = (now - timedelta(days=now.weekday())).date().isoformat()
    month_prefix = now.strftime("%Y-%m")
    with connection() as conn:
        totals = conn.execute(
            """SELECT
                 COALESCE(SUM(o.total_amount), 0) AS all_time,
                 COALESCE(SUM(CASE WHEN substr(COALESCE(p.reviewed_at, o.created_at), 1, 10) = ? THEN o.total_amount ELSE 0 END), 0) AS today,
                 COALESCE(SUM(CASE WHEN substr(COALESCE(p.reviewed_at, o.created_at), 1, 10) >= ? THEN o.total_amount ELSE 0 END), 0) AS week,
                 COALESCE(SUM(CASE WHEN substr(COALESCE(p.reviewed_at, o.created_at), 1, 7) = ? THEN o.total_amount ELSE 0 END), 0) AS month,
                 COUNT(*) AS successful_orders
               FROM orders o LEFT JOIN payments p ON p.order_id = o.id
               WHERE o.payment_status = 'APPROVED'""",
            (today, week_start, month_prefix),
        ).fetchone()
        pending = conn.execute(
            """SELECT COUNT(*) FROM orders
               WHERE payment_status IN ('PAYMENT_PENDING', 'UTR_SUBMITTED', 'PENDING_VERIFICATION')"""
        ).fetchone()[0]
        rejected = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE payment_status = 'REJECTED'"
        ).fetchone()[0]
        products = conn.execute(
            """SELECT product_name, COUNT(*) AS order_count,
                      SUM(quantity) AS quantity_sold, SUM(total_amount) AS revenue
               FROM orders WHERE payment_status = 'APPROVED'
               GROUP BY product_id, product_name ORDER BY revenue DESC"""
        ).fetchall()
        customers = conn.execute(
            """SELECT u.first_name, u.username, o.user_id,
                      SUM(o.total_amount) AS spend, SUM(o.quantity) AS quantity
               FROM orders o JOIN users u ON u.telegram_id = o.user_id
               WHERE o.payment_status = 'APPROVED' AND o.user_id != ?
               GROUP BY o.user_id ORDER BY spend DESC LIMIT 8""",
            (config.ADMIN_ID,),
        ).fetchall()
    product_summary = "\n".join(
        f"• {escape(row['product_name'])}: {row['order_count']} orders · "
        f"{row['quantity_sold']} codes · ₹{row['revenue']}"
        for row in products
    ) or "No approved sales yet."
    customer_summary = "\n".join(
        f"• {escape(row['first_name'])} · {row['quantity']} codes · ₹{row['spend']}"
        for row in customers
    ) or "No approved customer purchases yet."
    await message.reply_text(
        "<b>💰 VERIFIED SALES & REVENUE</b>\n"
        "<i>Revenue includes manually approved payments only.</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"All time: <b>₹{totals['all_time']}</b>\n"
        f"Today: ₹{totals['today']} · This week: ₹{totals['week']}\n"
        f"This month: ₹{totals['month']}\n"
        f"Successful orders: {totals['successful_orders']} · Pending: {pending} · Rejected: {rejected}\n\n"
        f"<b>Product sales</b>\n{product_summary}\n\n"
        f"<b>Top customers</b>\n{customer_summary}",
        reply_markup=back_home(),
        parse_mode=ParseMode.HTML,
    )


async def _show_delivery_queue(message, page: int) -> None:
    with connection() as conn:
        rows = conn.execute(
            """SELECT id, user_id, product_name, quantity, total_amount FROM orders
               WHERE payment_status = 'APPROVED' AND delivery_status != 'DELIVERED'
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (PAGE_SIZE + 1, max(0, page) * PAGE_SIZE),
        ).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    buttons = [
        [InlineKeyboardButton(f"🎟 {row['id']} · {row['quantity']} codes", callback_data=f"adm:order:{row['id']}")]
        for row in rows
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adm:delivery:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:delivery:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm:home")])
    summary = "\n".join(
        f"{escape(row['product_name'])} × {row['quantity']} · ₹{row['total_amount']} · customer {row['user_id']}"
        for row in rows
    ) or "No approved orders are awaiting delivery."
    await message.reply_text(
        f"<b>🎟 VOUCHER DELIVERY QUEUE</b>\n━━━━━━━━━━━━━━━━━━━━\n{summary}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def _show_product_analytics(message) -> None:
    with connection() as conn:
        rows = conn.execute(
            """SELECT p.id, p.name, p.price, p.stock,
                      COALESCE(SUM(CASE WHEN o.payment_status = 'APPROVED' THEN o.quantity ELSE 0 END), 0) AS sold,
                      COALESCE(SUM(CASE WHEN o.payment_status = 'APPROVED' THEN o.total_amount ELSE 0 END), 0) AS revenue,
                      COUNT(CASE WHEN o.payment_status = 'APPROVED' THEN 1 END) AS successful_orders
               FROM products p LEFT JOIN orders o ON o.product_id = p.id
               GROUP BY p.id ORDER BY revenue DESC"""
        ).fetchall()
    text = "\n\n".join(
        f"<b>{escape(row['name'])}</b>\n"
        f"💰 ₹{row['price']} · 🗃 Stock {row['stock']} · 🎟 Sold {row['sold']}\n"
        f"📦 Approved orders {row['successful_orders']} · Verified revenue ₹{row['revenue']}"
        for row in rows
    ) or "No products found."
    await message.reply_text(
        "<b>📈 PRODUCT ANALYTICS</b>\n<i>Sales count only manually approved payments.</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{text}",
        reply_markup=back_home(),
        parse_mode=ParseMode.HTML,
    )
