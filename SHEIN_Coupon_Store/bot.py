import asyncio
import html
import logging
import secrets
import sqlite3
import traceback

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.error import (
    BadRequest,
    InvalidToken,
    NetworkError,
    TelegramError,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import config
from database import (
    initialize_database,
    is_user_blocked,
    record_user,
    set_user_blocked,
)
from keyboards import (
    admin_dashboard,
    back_home,
    coupon_quantities,
    home_keyboard,
    home_only,
    payment_review,
)
from services import (
    StoreError,
    add_coupon,
    claim_coupon_delivery,
    claim_payment_screen,
    coupon_stock,
    create_or_get_order,
    create_support_message,
    finish_payment_screen,
    get_order,
    get_order_for_user,
    get_support_ticket,
    get_waiting_utr_orders,
    list_coupons,
    list_orders,
    list_support_tickets,
    list_user_orders,
    list_users,
    review_payment,
    record_coupon_delivery,
    save_admin_reply,
    submit_utr,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_STARTUP_ATTEMPTS = 3
TELEGRAM_STARTUP_RETRY_DELAY = 2.0
TELEGRAM_UNREACHABLE_MESSAGE = (
    "Telegram server is unreachable. Check your internet connection and try again."
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
            details = "".join(traceback.format_exception(*record.exc_info))
            if token in details:
                record.exc_text = details.replace(token, "[REDACTED]")
                record.exc_info = None
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_TokenRedactionFilter())


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(
        user
        and user.id == config.ADMIN_ID
        and chat
        and chat.type == ChatType.PRIVATE
    )


async def _show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
    parse_mode=None,
) -> None:
    query = update.callback_query
    if query and query.message:
        try:
            if query.message.photo:
                await query.edit_message_caption(
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            else:
                await query.edit_message_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
        except BadRequest as exc:
            message = str(exc).lower()
            if "message is not modified" in message:
                return
            if "message can't be edited" in message or "message to edit not found" in message:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return
            raise
    elif update.effective_message:
        await update.effective_message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )


async def _customer_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None:
        return False
    record_user(user)
    if is_user_blocked(user.id):
        await _show(
            update,
            context,
            "Access to this bot is currently restricted. Please contact Customer Support.",
            home_only(),
        )
        return False
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await _show(update, context, "Please open SHEIN Coupon Store in a private chat.")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_admin(update):
        context.user_data.pop("admin_action", None)
        await _show(
            update,
            context,
            "🛠 SHEIN COUPON STORE — ADMIN PANEL\n\nChoose an option:",
            admin_dashboard(),
        )
        return
    if not await _customer_allowed(update, context):
        return
    context.user_data.pop("awaiting_utr", None)
    context.user_data.pop("awaiting_quantity", None)
    context.user_data.pop("awaiting_support", None)
    await _show(
        update,
        context,
        "🛍️ Welcome to SHEIN Coupon Store!\n\n"
        "Get a SHEIN discount coupon for just ₹139.\n\n"
        "🎟️ Coupon Benefits:\n"
        "• Up to ₹800 discount\n"
        "• Minimum shopping value ₹1,000",
        home_keyboard(),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text("Admin access is not available.")
        return
    context.user_data.pop("admin_action", None)
    await _show(
        update,
        context,
        "🛠 SHEIN COUPON STORE — ADMIN PANEL\n\nChoose an option:",
        admin_dashboard(),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_admin(update):
        context.user_data.pop("admin_action", None)
        if update.effective_message:
            await update.effective_message.reply_text("Admin action cancelled.")
        return
    context.user_data.pop("awaiting_support", None)
    context.user_data.pop("awaiting_utr", None)
    context.user_data.pop("awaiting_quantity", None)
    if update.effective_message:
        await update.effective_message.reply_text(
            "Current action cancelled.", reply_markup=home_keyboard()
        )


async def _send_payment_qr(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order: dict,
) -> bool:
    if not config.QR_IMAGE_PATH.is_file():
        await _show(
            update,
            context,
            "Payment is temporarily unavailable because the configured UPI QR image is missing. "
            "Please contact Support.",
            home_only(),
        )
        return False
    text = (
        f"💳 <b>PAY ₹{order['total_amount']}</b>\n\n"
        "🎟️ SHEIN Discount Coupon\n"
        f"Quantity: {order['quantity']} coupon(s)\n"
        f"Price per coupon: ₹{config.PRICE_INR}\n"
        "Discount: Up to ₹800\n"
        "Minimum shopping value: ₹1,000\n"
        f"🆔 Order ID: <code>{html.escape(order['id'])}</code>\n\n"
        f"⏳ Complete payment within {config.PAYMENT_EXPIRY_MINUTES} minutes.\n"
        "After paying, tap “✅ I'VE PAID” and enter the 12-digit UTR.\n"
        "Payment is not approved automatically."
    )
    try:
        with config.QR_IMAGE_PATH.open("rb") as image:
            await context.bot.send_photo(
                chat_id=update.effective_user.id,
                photo=image,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ I'VE PAID", callback_data=f"pay:paid:{order['id']}"
                            )
                        ],
                        [InlineKeyboardButton("🏠 HOME", callback_data="home")],
                    ]
                ),
            )
            return True
    except OSError:
        logger.exception("Could not read the configured payment QR image")
        await _show(
            update,
            context,
            "Payment QR could not be loaded. Your payment has not been marked as received. "
            "Please try again or contact Support.",
            home_only(),
        )
        return False
    except TelegramError:
        logger.exception("Could not send payment QR for order %s", order["id"])
        await _show(
            update,
            context,
            "Telegram could not deliver the payment QR. Your order is still unpaid; "
            "please try again or contact Support.",
            home_only(),
        )
        return False


async def _notify_admin_payment_screen(
    context: ContextTypes.DEFAULT_TYPE,
    order: dict,
) -> None:
    username = f"@{order['username']}" if order.get("username") else "not set"
    text = (
        "<b>🧾 NEW PAYMENT SCREEN</b>\n\n"
        f"Customer name: {html.escape(order.get('first_name') or 'Customer')}\n"
        f"Telegram username: {html.escape(username)}\n"
        f"Telegram ID: <code>{order['user_id']}</code>\n"
        f"Order ID: <code>{html.escape(order['id'])}</code>\n"
        f"Coupon quantity: {order['quantity']}\n"
        f"Total payment: ₹{order['total_amount']}\n"
        "Order status: Waiting for Payment"
    )
    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.exception(
            "Could not send initial payment notice to admin_id=%s for order_id=%s",
            config.ADMIN_ID,
            order["id"],
        )
        raise


async def _notify_admin_payment(
    context: ContextTypes.DEFAULT_TYPE, order: dict
) -> None:
    username = f"@{order['username']}" if order.get("username") else "not set"
    text = (
        "<b>💳 PAYMENT UNDER REVIEW</b>\n\n"
        f"Order ID: <code>{html.escape(order['id'])}</code>\n"
        f"Customer: {html.escape(order.get('first_name') or 'Customer')} "
        f"({html.escape(username)})\n"
        f"Telegram ID: <code>{order['user_id']}</code>\n"
        "Product: SHEIN Discount Coupon\n"
        f"Quantity: {order['quantity']} coupon(s)\n"
        f"Amount: ₹{order['total_amount']}\n"
        "Payment method: UPI QR\n"
        f"Payment screen: {html.escape(order.get('payment_screen_status', 'SENT'))}\n"
        "Order status: Waiting for Admin Confirmation\n"
        f"UTR: <code>{html.escape(order['utr'])}</code>\n"
        f"Submitted: {html.escape(order['submitted_at'])}\n\n"
        "Verify the actual UPI transaction manually before deciding."
    )
    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=payment_review(order["id"]),
        )
    except TelegramError:
        logger.exception(
            "Could not send payment review to admin_id=%s for order_id=%s",
            config.ADMIN_ID,
            order["id"],
        )
        raise


async def _show_order_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status: str | None,
    title: str,
    page: int = 0,
) -> None:
    orders = list_orders(status, limit=10000)
    if not orders:
        await _show(update, context, f"{title}\n\nNo orders found.", back_home())
        return
    page_size = config.PAGE_SIZE
    page = max(0, min(page, (len(orders) - 1) // page_size))
    visible = orders[page * page_size : (page + 1) * page_size]
    buttons = [
        [
            InlineKeyboardButton(
                f"{row['id']} · {row['quantity']}× · {row['payment_status']}",
                callback_data=f"admin:order:{row['id']}",
            )
        ]
        for row in visible
    ]
    page_row = []
    status_key = status or "all"
    if page:
        page_row.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"admin:orders:{status_key}:{page - 1}")
        )
    if (page + 1) * page_size < len(orders):
        page_row.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"admin:orders:{status_key}:{page + 1}")
        )
    if page_row:
        buttons.append(page_row)
    buttons.append([InlineKeyboardButton("🔙 Admin Dashboard", callback_data="admin:dashboard")])
    await _show(
        update,
        context,
        f"{title}\n\nShowing {len(visible)} of {len(orders)} orders · Page {page + 1}.",
        InlineKeyboardMarkup(buttons),
    )


async def _show_admin_order(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str
) -> None:
    order = get_order(order_id)
    if order is None:
        await _show(update, context, "Order not found.", back_home())
        return
    username = f"@{order['username']}" if order["username"] else "not set"
    text = (
        "<b>📦 ORDER DETAILS</b>\n\n"
        f"Order ID: <code>{html.escape(order['id'])}</code>\n"
        f"Customer: {html.escape(order['first_name'])} ({html.escape(username)})\n"
        f"Telegram ID: <code>{order['user_id']}</code>\n"
        f"Quantity: {order['quantity']} coupon(s)\n"
        f"Price per coupon: ₹{order['amount']}\n"
        f"Total amount: ₹{order['total_amount']}\n"
        f"UTR: <code>{html.escape(order.get('utr') or 'Not submitted')}</code>\n"
        f"Payment: {html.escape(order['payment_status'])}\n"
        f"Delivery: {html.escape(order['delivery_status'])}"
    )
    if order["delivery_status"] == "DELIVERY_UNKNOWN" and order.get("coupon_codes"):
        text += (
            "\n\n<b>Delivery outcome unknown.</b> Do not use the send button again. "
            "These codes may already have reached the customer:\n"
            + "\n".join(
                f"<code>{html.escape(code)}</code>"
                for code in order["coupon_codes"]
            )
        )
    buttons = []
    if order["payment_status"] == "WAITING_FOR_ADMIN":
        buttons.append(
            [
                InlineKeyboardButton("✅ APPROVE", callback_data=f"admin:approve:{order_id}"),
                InlineKeyboardButton("❌ REJECT", callback_data=f"admin:reject:{order_id}"),
            ]
        )
    if (
        order["payment_status"] == "APPROVED"
        and order["delivery_status"] == "NOT_DELIVERED"
    ):
        buttons.append(
            [InlineKeyboardButton("🎟️ SEND COUPON", callback_data=f"admin:send:{order_id}")]
        )
    buttons.append([InlineKeyboardButton("🔙 All Orders", callback_data="admin:orders:all")])
    buttons.append([InlineKeyboardButton("🏠 Admin Dashboard", callback_data="admin:dashboard")])
    await _show(update, context, text, InlineKeyboardMarkup(buttons), ParseMode.HTML)


async def _show_coupon_stock(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    stock = coupon_stock()
    text = (
        "📉 <b>COUPON STOCK</b>\n\n"
        f"Initial unique coupons supplied: {config.INITIAL_STOCK_TARGET}\n"
        f"Entries provided: {config.INITIAL_STOCK_ENTRIES} "
        f"({config.INITIAL_STOCK_ENTRIES - config.INITIAL_STOCK_TARGET} duplicates ignored)\n"
        f"Available (real codes): {stock['available']}\n"
        f"Reserved for orders: {stock['reserved']}\n"
        f"Delivery in progress: {stock['sending']}\n"
        f"Delivered: {stock['delivered']}\n"
        f"Delivery outcome unknown: {stock['unknown']}\n"
        f"Total codes entered: {stock['total']}\n\n"
        "Only codes entered by an admin are counted as stock."
    )
    await _show(update, context, text, back_home(), ParseMode.HTML)


async def _show_coupons(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0
) -> None:
    coupons = list_coupons()
    lines = ["🎟️ <b>COUPON INVENTORY</b>\n"]
    page_size = 20
    page = max(0, min(page, (len(coupons) - 1) // page_size)) if coupons else 0
    visible = coupons[page * page_size : (page + 1) * page_size]
    if visible:
        for coupon in visible:
            code = html.escape(coupon["code"][:100])
            extra = f" · order {html.escape(coupon['reserved_order_id'])}" if coupon["reserved_order_id"] else ""
            lines.append(f"• <code>{code}</code> — {coupon['status']}{extra}")
    else:
        lines.append("No coupon codes have been entered yet.")
    lines.append("\nUse ➕ Add Coupon to enter actual codes.")
    buttons = []
    page_row = []
    if page:
        page_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admin:coupons:{page - 1}"))
    if (page + 1) * page_size < len(coupons):
        page_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:coupons:{page + 1}"))
    if page_row:
        buttons.append(page_row)
    buttons.append([InlineKeyboardButton("🔙 Admin Dashboard", callback_data="admin:dashboard")])
    await _show(update, context, "\n".join(lines), InlineKeyboardMarkup(buttons), ParseMode.HTML)


async def _show_users(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0
) -> None:
    users = list_users(limit=10000)
    if not users:
        await _show(update, context, "👥 No customers have started the bot yet.", back_home())
        return
    page_size = config.PAGE_SIZE
    page = max(0, min(page, (len(users) - 1) // page_size))
    visible = users[page * page_size : (page + 1) * page_size]
    buttons = []
    for user in visible:
        name = user["first_name"][:24]
        label = f"{'🚫' if user['is_blocked'] else '👤'} {name} · {user['telegram_id']}"
        buttons.append(
            [
                InlineKeyboardButton(
                    label, callback_data=f"admin:user_toggle:{user['telegram_id']}:{page}"
                )
            ]
        )
    page_row = []
    if page:
        page_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admin:users:{page - 1}"))
    if (page + 1) * page_size < len(users):
        page_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:users:{page + 1}"))
    if page_row:
        buttons.append(page_row)
    buttons.append([InlineKeyboardButton("🔙 Admin Dashboard", callback_data="admin:dashboard")])
    await _show(update, context, f"👥 CUSTOMER LIST · Page {page + 1}\nTap a customer to block or unblock.", InlineKeyboardMarkup(buttons))


async def _show_support_tickets(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0
) -> None:
    tickets = list_support_tickets(limit=10000)
    if not tickets:
        await _show(update, context, "💬 No open support messages.", back_home())
        return
    page_size = config.PAGE_SIZE
    page = max(0, min(page, (len(tickets) - 1) // page_size))
    visible = tickets[page * page_size : (page + 1) * page_size]
    buttons = []
    for ticket in visible:
        name = ticket["first_name"][:20]
        buttons.append(
            [
                InlineKeyboardButton(
                    f"#{ticket['id']} · {name} · {ticket['user_id']}",
                    callback_data=f"admin:ticket:{ticket['id']}",
                )
            ]
        )
    page_row = []
    if page:
        page_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admin:support:{page - 1}"))
    if (page + 1) * page_size < len(tickets):
        page_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:support:{page + 1}"))
    if page_row:
        buttons.append(page_row)
    buttons.append([InlineKeyboardButton("🔙 Admin Dashboard", callback_data="admin:dashboard")])
    await _show(update, context, f"💬 OPEN CUSTOMER SUPPORT TICKETS · Page {page + 1}", InlineKeyboardMarkup(buttons))


async def _show_ticket(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: int
) -> None:
    ticket = get_support_ticket(ticket_id)
    if ticket is None:
        await _show(update, context, "Support ticket not found.", back_home())
        return
    username = f"@{ticket['username']}" if ticket["username"] else "not set"
    lines = [
        f"<b>💬 TICKET #{ticket_id}</b>",
        f"Customer: {html.escape(ticket['first_name'])} ({html.escape(username)})",
        f"Telegram ID: <code>{ticket['user_id']}</code>",
        "",
    ]
    for message in ticket["messages"][-10:]:
        sender = "Customer" if message["sender_type"] == "CUSTOMER" else "Admin"
        lines.append(f"<b>{sender}:</b> {html.escape(message['message'][:700])}")
    buttons = [
        [InlineKeyboardButton("↩️ Reply", callback_data=f"admin:reply:{ticket_id}")],
        [InlineKeyboardButton("🔙 Support Inbox", callback_data="admin:support")],
    ]
    await _show(
        update,
        context,
        "\n".join(lines),
        InlineKeyboardMarkup(buttons),
        ParseMode.HTML,
    )


async def _deliver_coupon(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order_id: str,
) -> None:
    query = update.callback_query
    try:
        delivery = claim_coupon_delivery(order_id)
    except StoreError as exc:
        await _show(update, context, str(exc), back_home())
        return
    for coupon in delivery["codes"]:
        try:
            await context.bot.send_message(
                chat_id=delivery["telegram_id"],
                text=f"🎟️ Your SHEIN Coupon Code:\n{coupon['code']}",
            )
        except TelegramError:
            logger.exception(
                "Could not confirm delivery of coupon %s for order %s",
                coupon["id"],
                order_id,
            )
            status = "DELIVERY_UNKNOWN"
            try:
                status = record_coupon_delivery(
                    order_id, coupon["id"], succeeded=False
                )
            except (StoreError, sqlite3.Error):
                logger.exception("Could not persist delivery outcome for %s", order_id)
            try:
                await context.bot.send_message(
                    chat_id=config.ADMIN_ID,
                    text=(
                        f"⚠️ Delivery interrupted for {order_id}; "
                        f"status: {status}. Do not retry."
                    ),
                )
            except TelegramError:
                logger.exception("Could not report delivery failure for %s", order_id)
            await query.edit_message_text(
                f"⚠️ Delivery interrupted for order {order_id}. Do not retry."
            )
            return
        try:
            record_coupon_delivery(order_id, coupon["id"], succeeded=True)
        except (StoreError, sqlite3.Error):
            logger.exception("Could not record coupon delivery for %s", order_id)
            await query.edit_message_text(
                f"Telegram accepted a coupon for {order_id}, but its delivery record "
                "could not be saved. Do not retry."
            )
            return
    await query.edit_message_text(
        f"✅ Sent {delivery['quantity']} coupon(s) to customer for order {order_id}."
    )


async def _deliver_approved_coupons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order: dict,
) -> None:
    query = update.callback_query
    for coupon in order["codes"]:
        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=f"🎟️ Your SHEIN Coupon Code:\n{coupon['code']}",
            )
        except TelegramError:
            logger.exception(
                "Could not confirm delivery of coupon %s for order %s",
                coupon["id"],
                order["id"],
            )
            try:
                status = record_coupon_delivery(
                    order["id"], coupon["id"], succeeded=False
                )
            except (StoreError, sqlite3.Error):
                logger.exception(
                    "Could not record uncertain coupon delivery for order %s",
                    order["id"],
                )
                status = "DELIVERY_UNKNOWN"
            try:
                await context.bot.send_message(
                    chat_id=config.ADMIN_ID,
                    text=(
                        f"⚠️ Coupon delivery for order {order['id']} was interrupted.\n"
                        f"Payment: Approved\n"
                        f"Quantity: {order['quantity']}\n"
                        f"Completed before interruption: {order['codes'].index(coupon)}\n"
                        f"Delivery status: {status}\n"
                        "All remaining codes are locked; do not retry delivery."
                    ),
                )
            except TelegramError:
                logger.exception("Could not notify admin about partial delivery %s", order["id"])
            await query.edit_message_text(
                f"⚠️ Delivery for order {order['id']} was interrupted. "
                "Codes are locked to prevent duplicate delivery; check the order details."
            )
            return
        try:
            record_coupon_delivery(order["id"], coupon["id"], succeeded=True)
        except (StoreError, sqlite3.Error):
            logger.exception(
                "Could not record sent coupon %s for order %s",
                coupon["id"],
                order["id"],
            )
            try:
                await context.bot.send_message(
                    chat_id=config.ADMIN_ID,
                    text=(
                        f"⚠️ Telegram accepted coupon {coupon['id']} for order {order['id']}, "
                        "but its delivery status could not be saved. Do not retry."
                    ),
                )
            except TelegramError:
                logger.exception("Could not notify admin about delivery tracking failure")
            await query.edit_message_text(
                f"⚠️ A coupon was sent for order {order['id']}, but the delivery record "
                "could not be saved. Do not retry; inspect the order."
            )
            return

    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_ID,
            text=(
                f"✅ Coupon delivery complete\n"
                f"Order ID: {order['id']}\n"
                f"Customer Telegram ID: {order['user_id']}\n"
                f"Coupons delivered: {len(order['codes'])}/{order['quantity']}\n"
                f"Total payment: ₹{order['total_amount']}"
            ),
        )
    except TelegramError:
        logger.exception("Could not send delivery completion notice for %s", order["id"])
    await query.edit_message_text(
        f"✅ Payment approved and {len(order['codes'])} coupon(s) sent to the customer "
        f"for order {order['id']}."
    )


async def _handle_admin_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    query = update.callback_query
    if not _is_admin(update):
        await query.answer("Unauthorized.", show_alert=True)
        return
    await query.answer()
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "dashboard":
        context.user_data.pop("admin_action", None)
        await _show(update, context, "🛠 SHEIN COUPON STORE — ADMIN PANEL", admin_dashboard())
    elif action == "orders":
        status = parts[2] if len(parts) > 2 and parts[2] != "all" else None
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        title = "📦 ALL ORDERS" if status is None else f"💳 {status.replace('_', ' ')} PAYMENTS"
        await _show_order_list(update, context, status, title, page)
    elif action == "order" and len(parts) == 3:
        await _show_admin_order(update, context, parts[2])
    elif action in ("approve", "reject") and len(parts) == 3:
        try:
            order = review_payment(parts[2], approved=action == "approve")
        except StoreError as exc:
            await _show(update, context, str(exc), back_home())
            return
        if action == "approve":
            await _deliver_approved_coupons(update, context, order)
        else:
            try:
                await context.bot.send_message(
                    chat_id=order["user_id"],
                    text=(
                        f"❌ Payment for order {order['id']} was rejected. "
                        "Please contact Support if you believe this is an error."
                    ),
                )
            except TelegramError:
                logger.exception("Could not notify customer of rejection for %s", order["id"])
            await _show_admin_order(update, context, order["id"])
    elif action == "send" and len(parts) == 3:
        await _deliver_coupon(update, context, parts[2])
    elif action == "coupons":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await _show_coupons(update, context, page)
    elif action == "stock":
        await _show_coupon_stock(update, context)
    elif action == "add_coupon":
        context.user_data["admin_action"] = "add_coupon"
        await _show(
            update,
            context,
            "Send one actual coupon code per line. Duplicate codes are rejected. "
            "Send /cancel to stop.",
            back_home(),
        )
    elif action == "users":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await _show_users(update, context, page)
    elif action == "user_toggle" and len(parts) >= 3 and parts[2].isdigit():
        user_id = int(parts[2])
        current = is_user_blocked(user_id)
        set_user_blocked(user_id, not current)
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await _show_users(update, context, page)
    elif action == "block_user":
        context.user_data["admin_action"] = "block_user"
        await _show(update, context, "Send the customer's numeric Telegram ID to block. Send /cancel to stop.", back_home())
    elif action == "support":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await _show_support_tickets(update, context, page)
    elif action == "ticket" and len(parts) == 3 and parts[2].isdigit():
        await _show_ticket(update, context, int(parts[2]))
    elif action == "reply" and len(parts) == 3 and parts[2].isdigit():
        ticket = get_support_ticket(int(parts[2]))
        if ticket is None or ticket["status"] != "OPEN":
            await _show(update, context, "That support ticket is not open.", back_home())
            return
        context.user_data["admin_action"] = f"reply:{parts[2]}"
        await _show(update, context, f"Send your reply to support ticket #{parts[2]}. Send /cancel to stop.", back_home())
    else:
        await _show(update, context, "That admin action is not available.", admin_dashboard())


def _parse_positive_quantity(value: str) -> int | None:
    if not value or len(value) > 18 or not value.isascii() or not value.isdigit():
        return None
    try:
        quantity = int(value)
    except ValueError:
        return None
    return quantity if quantity > 0 else None


async def _show_purchase_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    quantity: int,
) -> None:
    if quantity <= 0:
        await _show(update, context, "Enter a positive whole-number quantity.", coupon_quantities())
        return
    available = coupon_stock()["available"]
    if quantity > available:
        await _show(
            update,
            context,
            f"Only {available} coupon(s) are available. Choose a quantity within stock.",
            coupon_quantities(),
        )
        return
    total = quantity * config.PRICE_INR
    purchase_request_id = secrets.token_hex(8)
    await _show(
        update,
        context,
        "🛍️ <b>CONFIRM YOUR ORDER</b>\n\n"
        "Product: SHEIN Discount Coupon\n"
        f"Quantity: <b>{quantity}</b>\n"
        f"Price per coupon: ₹{config.PRICE_INR}\n"
        f"Total payment: <b>₹{total}</b>\n"
        "Discount: Up to ₹800 per coupon\n"
        "Minimum shopping value: ₹1,000 per coupon\n\n"
        "Confirm to reserve your coupons and continue to UPI payment. "
        "Payment is manually checked by the admin.",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💳 CONTINUE TO PAYMENT",
                        callback_data=(
                            f"buy:confirm:{quantity}:{purchase_request_id}"
                        ),
                    )
                ],
                [InlineKeyboardButton("🔙 BACK", callback_data="buy")],
                [InlineKeyboardButton("🏠 HOME", callback_data="home")],
            ]
        ),
        ParseMode.HTML,
    )


async def callback_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    if data.startswith("admin:"):
        await _handle_admin_callback(update, context, data)
        return
    await query.answer()
    if not await _customer_allowed(update, context):
        return
    user_id = update.effective_user.id
    if data == "home":
        context.user_data.pop("awaiting_support", None)
        context.user_data.pop("awaiting_utr", None)
        context.user_data.pop("awaiting_quantity", None)
        await _show(
            update,
            context,
            "🛍️ Welcome to SHEIN Coupon Store!\n\n"
            "Get a SHEIN discount coupon for just ₹139.\n\n"
            "🎟️ Coupon Benefits:\n• Up to ₹800 discount\n• Minimum shopping value ₹1,000",
            home_keyboard(),
        )
    elif data == "buy":
        context.user_data.pop("awaiting_quantity", None)
        stock = coupon_stock()
        await _show(
            update,
            context,
            "🛒 <b>SHEIN DISCOUNT COUPON</b>\n\n"
            "Price: <b>₹139</b>\n"
            "Discount: Up to ₹800\n"
            "Minimum shopping value: ₹1,000\n"
            f"Available coupon codes: {stock['available']}\n\n"
            "Choose how many coupons you want to buy. Each coupon costs ₹139.",
            coupon_quantities(),
            ParseMode.HTML,
        )
    elif data == "buy:custom":
        available = coupon_stock()["available"]
        context.user_data["awaiting_quantity"] = True
        await _show(
            update,
            context,
            "✏️ Enter the number of coupons to buy as a positive whole number.\n"
            f"Available stock: {available}\n"
            "Decimals, zero and negative quantities are not accepted.",
            back_home("buy"),
        )
    elif data.startswith("buy:quantity:"):
        raw_quantity = data.removeprefix("buy:quantity:")
        quantity = _parse_positive_quantity(raw_quantity)
        if quantity is None:
            await _show(update, context, "Invalid coupon quantity.", coupon_quantities())
            return
        await _show_purchase_confirmation(update, context, quantity)
    elif data.startswith("buy:confirm:"):
        parts = data.split(":")
        raw_quantity = parts[2] if len(parts) >= 3 else ""
        purchase_request_id = parts[3] if len(parts) == 4 else None
        quantity = _parse_positive_quantity(raw_quantity)
        if quantity is None:
            await _show(update, context, "Invalid coupon quantity.", coupon_quantities())
            return
        if not config.QR_IMAGE_PATH.is_file():
            await _show(update, context, "Payment is unavailable because the UPI QR image is missing. Contact Support.", home_only())
            return
        if purchase_request_id is None:
            message_id = getattr(query.message, "message_id", query.id)
            purchase_request_id = (
                f"legacy-{user_id}-{query.message.chat_id}-{message_id}-{quantity}"
            )
        try:
            order = create_or_get_order(user_id, quantity, purchase_request_id)
        except StoreError as exc:
            await _show(update, context, str(exc), coupon_quantities())
            return
        if order["payment_status"] == "WAITING_FOR_ADMIN":
            await _show(update, context, f"Payment for order {order['id']} is under admin review. Duplicate submissions are not accepted.", home_only())
        elif order["payment_status"] == "APPROVED":
            await _show(update, context, f"Payment for order {order['id']} is approved. The admin will send your coupon(s).", home_only())
        else:
            try:
                payment_screen = claim_payment_screen(order["id"], user_id)
            except StoreError as exc:
                await _show(update, context, str(exc), home_only())
                return
            if payment_screen is None:
                context.user_data["awaiting_utr"] = order["id"]
                await _show(
                    update,
                    context,
                    "The payment screen for this order has already been sent. "
                    "Use the existing QR message to continue and avoid duplicate payment.",
                    home_only(),
                )
                return
            context.user_data["awaiting_utr"] = order["id"]
            await _show(
                update,
                context,
                f"Order {order['id']} created for {order['quantity']} coupon(s). "
                f"Scan the UPI QR below and pay exactly ₹{order['total_amount']}.",
                home_only(),
            )
            qr_sent = await _send_payment_qr(update, context, payment_screen)
            finish_payment_screen(order["id"], sent=qr_sent)
            if qr_sent:
                try:
                    await _notify_admin_payment_screen(context, payment_screen)
                except TelegramError:
                    await _show(
                        update,
                        context,
                        "⚠️ Your payment QR was sent, but the admin could not be notified. "
                        "Please do not pay yet. Ask the store admin to open this SHEIN bot "
                        "and send /start, then check the order in /admin. "
                        f"Order ID: {order['id']}",
                        home_only(),
                    )
    elif data == "info":
        await _show(
            update,
            context,
            "🎟️ <b>COUPON INFORMATION</b>\n\n"
            "• SHEIN discount coupon\n"
            "• Up to ₹800 discount\n"
            "• Minimum shopping value: ₹1,000\n"
            "• Selling price: ₹139\n\n"
            "Coupon terms and validity are set by the coupon issuer. Contact Support if you need help.",
            back_home("home"),
            ParseMode.HTML,
        )
    elif data == "orders" or data.startswith("orders:page:"):
        page = 0
        if data.startswith("orders:page:"):
            try:
                page = max(0, int(data.removeprefix("orders:page:")))
            except ValueError:
                page = 0
        orders = list_user_orders(user_id)
        if not orders:
            await _show(update, context, "📦 You have no orders yet.", back_home("home"))
            return
        lines = ["📦 <b>MY ORDERS</b>\n"]
        page_size = 8
        page_count = (len(orders) + page_size - 1) // page_size
        page = min(page, page_count - 1)
        visible_orders = orders[page * page_size : (page + 1) * page_size]
        rows = []
        for order in visible_orders:
            lines.append(
                f"• <code>{html.escape(order['id'])}</code> · "
                f"{order['quantity']} coupon(s) · ₹{order['total_amount']} · "
                f"{order['payment_status'].replace('_', ' ')} · "
                f"{order['delivery_status'].replace('_', ' ')}"
            )
            if order["payment_status"] == "WAITING_FOR_UTR":
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"💳 Submit UTR · {order['id']}",
                            callback_data=f"pay:select:{order['id']}",
                        )
                    ]
                )
        if page_count > 1:
            navigation = []
            if page > 0:
                navigation.append(
                    InlineKeyboardButton(
                        "⬅️ Newer", callback_data=f"orders:page:{page - 1}"
                    )
                )
            if page + 1 < page_count:
                navigation.append(
                    InlineKeyboardButton(
                        "Older ➡️", callback_data=f"orders:page:{page + 1}"
                    )
                )
            if navigation:
                rows.append(navigation)
        rows.append([InlineKeyboardButton("🏠 HOME", callback_data="home")])
        lines.append(f"\nPage {page + 1} of {page_count}")
        await _show(
            update,
            context,
            "\n".join(lines),
            InlineKeyboardMarkup(rows),
            ParseMode.HTML,
        )
    elif data == "support":
        context.user_data["awaiting_support"] = True
        await _show(
            update,
            context,
            "💬 Send your support message in your next message. The store admin will reply here.",
            back_home("home"),
        )
    elif data.startswith("pay:paid:") or data.startswith("pay:select:"):
        order_id = data.split(":", 2)[2]
        order = get_order_for_user(order_id, user_id)
        if order is None or order["payment_status"] != "WAITING_FOR_UTR":
            await _show(update, context, "This order is not awaiting a UTR. Contact Support if you already paid.", home_only())
            return
        context.user_data["awaiting_utr"] = order_id
        await _show(update, context, "Please enter your 12-digit UTR number. It must contain exactly 12 digits.", home_only())
    else:
        await _show(update, context, "Please choose an option from the menu.", home_keyboard())


async def _handle_customer_utr(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order: dict,
    utr: str,
) -> None:
    try:
        submitted = submit_utr(order["id"], update.effective_user.id, utr)
    except StoreError as exc:
        await update.effective_message.reply_text(
            f"⚠️ {exc}\n\nPlease enter exactly 12 digits, without spaces."
        )
        return
    except sqlite3.Error:
        logger.exception("Could not save UTR for order %s", order["id"])
        await update.effective_message.reply_text(
            "We could not save your payment information. Please try again or contact Support."
        )
        return
    context.user_data.pop("awaiting_utr", None)
    try:
        await _notify_admin_payment(context, submitted)
    except TelegramError:
        await update.effective_message.reply_text(
            "⚠️ Your UTR was saved, but Telegram could not deliver the admin notification. "
            "Do not submit the UTR again. Ask the store admin to open this SHEIN bot "
            "and send /start, then review this order in /admin.\n"
            f"Order ID: {submitted['id']}"
        )
        return
    await update.effective_message.reply_text(
        f"⏳ Payment Under Review\n\n"
        f"Order ID: {submitted['id']}\n"
        "Status: Waiting for Admin Confirmation\n"
        "Your payment will not be automatically approved."
    )


async def message_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.text is None:
        return
    if user.id == config.ADMIN_ID:
        if update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
            await message.reply_text("Admin controls are available only in a private chat.")
            return
        if context.user_data.get("admin_action") and message.text.strip() == "/cancel":
            context.user_data.pop("admin_action", None)
            await message.reply_text("Admin action cancelled.")
            return
        action = context.user_data.get("admin_action")
        if action == "add_coupon":
            codes = [line.strip() for line in message.text.splitlines() if line.strip()]
            added, duplicates = 0, 0
            for code in codes:
                try:
                    add_coupon(code)
                    added += 1
                except StoreError as exc:
                    if "already exists" in str(exc):
                        duplicates += 1
                    else:
                        await message.reply_text(str(exc))
                        return
            context.user_data.pop("admin_action", None)
            await message.reply_text(
                f"Coupon entry complete. Added: {added}. Duplicate codes skipped: {duplicates}."
            )
            return
        if action == "block_user":
            value = message.text.strip()
            if not value.isascii() or not value.isdigit() or int(value) <= 0:
                await message.reply_text("Enter a positive numeric Telegram ID.")
                return
            if not set_user_blocked(int(value), True):
                await message.reply_text("That user has not started the bot yet.")
                return
            context.user_data.pop("admin_action", None)
            await message.reply_text("Customer blocked.")
            return
        if isinstance(action, str) and action.startswith("reply:"):
            ticket_id = int(action.split(":", 1)[1])
            ticket = get_support_ticket(ticket_id)
            if ticket is None or ticket["status"] != "OPEN":
                context.user_data.pop("admin_action", None)
                await message.reply_text("That support ticket is no longer open.")
                return
            try:
                await context.bot.send_message(
                    chat_id=ticket["user_id"],
                    text=f"💬 Reply from SHEIN Coupon Store Support:\n\n{message.text.strip()}",
                )
                save_admin_reply(ticket_id, message.text)
            except StoreError as exc:
                await message.reply_text(str(exc))
                return
            except TelegramError:
                logger.exception("Could not deliver support reply for ticket %s", ticket_id)
                await message.reply_text("Telegram could not deliver the reply. It was not recorded.")
                return
            context.user_data.pop("admin_action", None)
            await message.reply_text(f"Reply sent to ticket #{ticket_id}.")
            return
        await message.reply_text("Use /admin to open the admin panel.")
        return

    if not await _customer_allowed(update, context):
        return
    if context.user_data.get("awaiting_quantity"):
        raw_quantity = message.text.strip()
        quantity = _parse_positive_quantity(raw_quantity)
        if quantity is None:
            await message.reply_text(
                "Invalid quantity. Enter a positive whole number only "
                "(no zero, negatives or decimals)."
            )
            return
        available = coupon_stock()["available"]
        if quantity > available:
            await message.reply_text(
                f"Only {available} coupon(s) are available. Enter a quantity no greater than stock."
            )
            return
        context.user_data.pop("awaiting_quantity", None)
        total = quantity * config.PRICE_INR
        purchase_request_id = secrets.token_hex(8)
        await message.reply_text(
            "🛍️ Order summary\n\n"
            f"Quantity: {quantity} coupon(s)\n"
            f"Price per coupon: ₹{config.PRICE_INR}\n"
            f"Total payment: ₹{total}\n\n"
            "Confirm the purchase below to reserve stock.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ CONFIRM PURCHASE",
                            callback_data=(
                                f"buy:confirm:{quantity}:{purchase_request_id}"
                            ),
                        )
                    ],
                    [InlineKeyboardButton("🔙 BACK", callback_data="buy")],
                    [InlineKeyboardButton("🏠 HOME", callback_data="home")],
                ]
            ),
        )
        return
    if context.user_data.get("awaiting_support"):
        try:
            ticket_id = create_support_message(user.id, message.text)
        except StoreError as exc:
            await message.reply_text(str(exc))
            return
        context.user_data.pop("awaiting_support", None)
        try:
            await context.bot.send_message(
                chat_id=config.ADMIN_ID,
                text=(
                    "💬 New customer support message\n"
                    f"Ticket: #{ticket_id}\n"
                    f"Customer: {user.first_name or 'Customer'} (@{user.username or 'not set'})\n"
                    f"Telegram ID: {user.id}\n\n{message.text[:3000]}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Reply", callback_data=f"admin:ticket:{ticket_id}")]]
                ),
            )
        except TelegramError:
            logger.exception("Could not notify admin about support ticket %s", ticket_id)
            await message.reply_text(
                f"Your message was saved as support ticket #{ticket_id}, but the admin could not be notified. "
                "Please try again later."
            )
            return
        await message.reply_text(f"✅ Your message was sent to Support. Ticket #{ticket_id}.")
        return

    selected_order_id = context.user_data.get("awaiting_utr")
    if isinstance(selected_order_id, str):
        order = get_order_for_user(selected_order_id, user.id)
        if order is not None and order["payment_status"] == "WAITING_FOR_UTR":
            await _handle_customer_utr(update, context, order, message.text)
            return
        context.user_data.pop("awaiting_utr", None)
    waiting_orders = get_waiting_utr_orders(user.id)
    if len(waiting_orders) == 1:
        await _handle_customer_utr(update, context, waiting_orders[0], message.text)
        return
    if len(waiting_orders) > 1:
        buttons = [
            [
                InlineKeyboardButton(
                    f"💳 Order {order['id']} · ₹{order['total_amount']}",
                    callback_data=f"pay:select:{order['id']}",
                )
            ]
            for order in waiting_orders[:20]
        ]
        await message.reply_text(
            "You have multiple orders awaiting payment details. Select the order "
            "whose UTR you are submitting, then send the 12-digit UTR.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    await message.reply_text("Choose an option from the menu below.", reply_markup=home_keyboard())


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    error = context.error
    if isinstance(error, BadRequest) and any(
        phrase in str(error).lower()
        for phrase in ("message is not modified", "query is too old", "query id is invalid")
    ):
        logger.debug("Ignored harmless Telegram callback error: %s", error)
        return
    if error is not None:
        logger.error(
            "Unhandled bot error",
            exc_info=(type(error), error, error.__traceback__),
        )
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "We couldn't complete that request. Please try again or contact Support."
            )
        except TelegramError:
            logger.exception("Could not send error notice")


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
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    application.add_error_handler(error_handler)
    return application


async def _initialize_telegram(application: Application) -> bool:
    for attempt in range(1, TELEGRAM_STARTUP_ATTEMPTS + 1):
        try:
            await application.bot.initialize()
            return True
        except InvalidToken:
            logger.error(
                "Telegram rejected BOT_TOKEN. Check the value in .env; the token was not logged."
            )
            return False
        except NetworkError as exc:
            if isinstance(exc, BadRequest):
                raise
            if attempt == TELEGRAM_STARTUP_ATTEMPTS:
                logger.error(TELEGRAM_UNREACHABLE_MESSAGE)
                return False
            logger.warning(
                "Telegram connection failed during startup (%s, attempt %d/%d); retrying in %.1f seconds.",
                type(exc).__name__,
                attempt,
                TELEGRAM_STARTUP_ATTEMPTS,
                TELEGRAM_STARTUP_RETRY_DELAY,
            )
            await asyncio.sleep(TELEGRAM_STARTUP_RETRY_DELAY)
    return False


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


def main() -> None:
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    initialize_database()
    application = build_application()
    loop = _ensure_event_loop()
    logger.info("Starting SHEIN Coupon Store bot.")
    if not loop.run_until_complete(_initialize_telegram(application)):
        return
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=0)
    except NetworkError as exc:
        if isinstance(exc, BadRequest):
            raise
        logger.error(TELEGRAM_UNREACHABLE_MESSAGE)


if __name__ == "__main__":
    main()
