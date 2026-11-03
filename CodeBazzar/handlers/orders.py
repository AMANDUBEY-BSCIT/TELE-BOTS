from html import escape
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import PAGE_SIZE
from database import connection, is_user_blocked, record_user
from handlers.callback_guard import answer_callback
from handlers.customer_ui import (
    add_navigation,
    ensure_private_chat,
    handle_back,
    show_screen,
)
from services.delivery_service import voucher_code_messages


async def show_orders(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    push: bool = True,
) -> None:
    user_id = update.effective_user.id
    with connection() as conn:
        rows = conn.execute(
            """SELECT * FROM orders WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (user_id, PAGE_SIZE + 1, page * PAGE_SIZE),
        ).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    if not rows:
        text = (
            "<b>📦 YOUR ORDERS</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
            "No orders yet. Your next great find is waiting in the store."
        )
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛍️ Browse Vouchers", callback_data="shop:list")],
             [InlineKeyboardButton("🔙 Back", callback_data="nav:back")],
             [InlineKeyboardButton("🏠 Home", callback_data="home")]]
        )
    else:
        entries = []
        for order in rows:
            created = datetime.fromisoformat(order["created_at"]).strftime("%d %b %Y")
            payment_status = order["payment_status"]
            status_emoji = {
                "TERMS_PENDING": "📝",
                "PAYMENT_PENDING": "⏳",
                "UTR_SUBMITTED": "🔎",
                "PENDING_VERIFICATION": "🔎",
                "APPROVED": "✅",
                "REJECTED": "❌",
                "CANCELLED": "🚫",
                "EXPIRED": "⌛",
            }.get(payment_status, "📄")
            delivery_status = order["delivery_status"]
            delivery_emoji = "🎉" if delivery_status == "DELIVERED" else "📦"
            entries.append(
                f"🧾 <code>{escape(order['id'])}</code>\n"
                f"🎟️ <b>{escape(order['product_name'])}</b>\n"
                f"📦 Qty {order['quantity']}  ·  💰 ₹{order['total_amount']}.00\n"
                f"{status_emoji} Payment: <b>{escape(payment_status.replace('_', ' ').title())}</b>\n"
                f"{delivery_emoji} Delivery: <b>{escape(delivery_status.replace('_', ' ').title())}</b>\n"
                f"🗓 {created}"
            )
        text = (
            "<b>📦 YOUR ORDERS</b>\n"
            "<i>Every order, clearly tracked.</i>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            + "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(entries)
        )
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"orders:{page - 1}"))
        if has_next:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"orders:{page + 1}"))
        rows_markup = [nav] if nav else []
        rows_markup.append([InlineKeyboardButton("🔄 Recover Voucher", callback_data="recover:start")])
        rows_markup.extend(
            [
                [InlineKeyboardButton("🔙 Back", callback_data="nav:back")],
                [InlineKeyboardButton("🏠 Home", callback_data="home")],
            ]
        )
        markup = InlineKeyboardMarkup(rows_markup)
    await show_screen(
        update,
        context,
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        screen={"type": "orders", "page": page},
        push=push,
    )


async def show_recovery_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    push: bool = True,
    notice: str = "",
) -> None:
    context.user_data["recover_waiting"] = True
    await show_screen(
        update,
        context,
        (f"{notice}\n\n" if notice else "")
        + "<b>🔄 VOUCHER RECOVERY</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        "Send your Order ID to securely retrieve your voucher.",
        reply_markup=add_navigation(None),
        parse_mode=ParseMode.HTML,
        screen={"type": "recover"},
        push=push,
    )


async def handle_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await ensure_private_chat(update, context):
        return
    if query.data == "recover:start":
        await answer_callback(query, context)
        record_user(query.from_user)
        if is_user_blocked(query.from_user.id):
            await query.message.reply_text("Access to this bot is currently restricted.")
            return
        await show_recovery_prompt(update, context)
        return
    if query.data == "recover:cancel":
        await answer_callback(query, context)
        context.user_data.pop("recover_waiting", None)
        await handle_back(update, context, answer=False)
        return
    await answer_callback(query, context)
    record_user(query.from_user)
    if is_user_blocked(query.from_user.id):
        await query.message.reply_text("Access to this bot is currently restricted.")
        return
    page = int(query.data.split(":", 1)[1])
    current = context.user_data.get("customer_current_screen")
    await show_orders(
        update,
        context,
        page,
        push=not current or current.get("type") != "orders",
    )


async def handle_recovery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.pop("recover_waiting", False):
        return False
    message = update.effective_message
    order_id = (message.text or "").strip().upper() if message else ""
    with connection() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?",
            (order_id, update.effective_user.id),
        ).fetchone()
        delivery = conn.execute(
            "SELECT codes_json FROM voucher_deliveries WHERE order_id = ?", (order_id,)
        ).fetchone()
    if order is None:
        await show_recovery_prompt(
            update,
            context,
            push=False,
            notice="Order not found for your account. Check the Order ID and try again.",
        )
        return True
    text = (
        f"🧾 Order ID: {order['id']}\n🎟 Product: {order['product_name']}\n"
        f"📦 Quantity: {order['quantity']}\n💰 Amount: ₹{order['total_amount']}.00\n"
        f"Payment: {order['payment_status']}\nDelivery: {order['delivery_status']}"
    )
    extra_codes = ""
    if delivery is not None:
        import json

        codes = json.loads(delivery["codes_json"])
        extra_codes = "\n\n".join(voucher_code_messages(order_id, codes))
    combined = f"{text}\n\n{extra_codes}" if extra_codes else text
    if len(combined) <= 3900:
        await show_screen(
            update,
            context,
            combined,
            reply_markup=add_navigation(None),
            push=False,
        )
    else:
        await show_screen(
            update,
            context,
            text,
            reply_markup=add_navigation(None),
            push=False,
        )
        for codes_message in voucher_code_messages(order_id, codes):
            await message.reply_text(codes_message)
    return True
