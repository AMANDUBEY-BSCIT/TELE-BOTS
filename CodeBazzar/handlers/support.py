import logging
import sqlite3
from secrets import token_hex
from datetime import datetime, timezone
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import config
from config import PAGE_SIZE
from database import (
    add_activity,
    connection,
    is_user_blocked,
    record_activity,
    record_user,
    transaction,
    utc_now,
)
from handlers.callback_guard import answer_callback
from handlers.customer_ui import (
    add_navigation,
    ensure_private_chat,
    handle_back,
    show_screen,
)

logger = logging.getLogger(__name__)

CATEGORIES = {
    "order": "Order Related Issue",
    "payment": "Payment Issue",
    "voucher": "Voucher Related Issue",
    "other": "Other Query",
}


async def show_support(
    update: Update, context: ContextTypes.DEFAULT_TYPE, push: bool = True
) -> None:
    if update.effective_user:
        record_user(update.effective_user)
        if is_user_blocked(update.effective_user.id):
            await show_screen(
                update,
                context,
                "Access to this bot is currently restricted."
            )
            return
        record_activity(update.effective_user.id, "SUPPORT_OPENED")
    buttons = [
        [
            InlineKeyboardButton("📦 Order", callback_data="support:order"),
            InlineKeyboardButton("💳 Payment", callback_data="support:payment"),
        ],
        [
            InlineKeyboardButton("🎟️ Voucher", callback_data="support:voucher"),
            InlineKeyboardButton("💬 Other", callback_data="support:other"),
        ],
        [InlineKeyboardButton("📋 My Support Tickets", callback_data="support:tickets:0")],
    ]
    await show_screen(
        update,
        context,
        "<b>💬 CODEBAZAR CARE</b>\n"
        "<i>Here for you, every step of the way.</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Choose a topic and tell us what happened. Our team will follow up here "
        "and you can reply to the same ticket anytime.",
        reply_markup=add_navigation(InlineKeyboardMarkup(buttons)),
        parse_mode=ParseMode.HTML,
        screen={"type": "support"},
        push=push,
    )


async def show_support_input_prompt(
    update,
    context: ContextTypes.DEFAULT_TYPE,
    category_key: str,
    push: bool = True,
) -> None:
    if category_key not in CATEGORIES:
        await show_support(update, context, push=push)
        return
    context.user_data["support_pending_category"] = category_key
    await show_screen(
        update,
        context,
        f"<b>✍️ {escape(CATEGORIES[category_key])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Send a message describing the issue. You can also attach a screenshot.",
        reply_markup=add_navigation(None),
        parse_mode=ParseMode.HTML,
        screen={"type": "support_input", "category": category_key},
        push=push,
    )


async def handle_support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await ensure_private_chat(update, context):
        return
    await answer_callback(query, context)
    record_user(query.from_user)
    if is_user_blocked(query.from_user.id):
        await query.message.reply_text("Access to this bot is currently restricted.")
        return
    parts = (query.data or "").split(":")
    category_key = parts[1] if len(parts) > 1 else ""
    if category_key == "menu":
        current = context.user_data.get("customer_current_screen")
        await show_support(
            update,
            context,
            push=not current or current.get("type") != "support",
        )
        return
    if category_key == "cancel":
        context.user_data.pop("support_pending_category", None)
        context.user_data.pop("active_ticket_id", None)
        await handle_back(update, context, answer=False)
        return
    if category_key == "tickets" and len(parts) == 3:
        try:
            page = max(0, int(parts[2]))
        except ValueError:
            await show_screen(
                update, context, "That ticket page is invalid.",
                reply_markup=add_navigation(None), push=False,
            )
            return
        current = context.user_data.get("customer_current_screen")
        await show_customer_tickets(
            update,
            context,
            page,
            push=not current or current.get("type") != "tickets",
        )
        return
    if category_key == "ticket" and len(parts) == 3:
        await show_customer_ticket(update, context, parts[2])
        return
    if category_key == "reply" and len(parts) == 3:
        with connection() as conn:
            ticket = conn.execute(
                "SELECT id FROM support_tickets WHERE id = ? AND user_id = ?",
                (parts[2], query.from_user.id),
            ).fetchone()
        if ticket is None:
            await show_screen(
                update, context, "That support ticket was not found.",
                reply_markup=add_navigation(None), push=False,
            )
            return
        context.user_data["active_ticket_id"] = parts[2]
        context.user_data.pop("support_pending_category", None)
        await show_ticket_reply_prompt(update, context, parts[2])
        return
    if category_key not in CATEGORIES:
        return
    context.user_data["support_pending_category"] = category_key
    with connection() as conn:
        open_ticket = conn.execute(
            """SELECT id FROM support_tickets
               WHERE user_id = ? AND status != 'RESOLVED'
               ORDER BY updated_at DESC LIMIT 1""",
            (query.from_user.id,),
        ).fetchone()
    if open_ticket:
        context.user_data["active_ticket_id"] = open_ticket["id"]
    await show_screen(
        update,
        context,
        f"<b>✍️ {escape(CATEGORIES[category_key])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Describe the issue in a message. You can also attach a screenshot.\n"
        + (
            f"\nYour existing ticket <code>{escape(open_ticket['id'])}</code> is still open; "
            "your message will be added to it."
            if open_ticket
            else ""
        ),
        reply_markup=add_navigation(None),
        parse_mode=ParseMode.HTML,
        screen={"type": "support_input", "category": category_key},
    )


async def show_ticket_reply_prompt(update, context, ticket_id: str, push: bool = True) -> None:
    await show_screen(
        update,
        context,
        f"Send your reply for support ticket <code>{escape(ticket_id)}</code>.",
        reply_markup=add_navigation(None),
        parse_mode=ParseMode.HTML,
        screen={"type": "ticket_reply", "ticket_id": ticket_id},
        push=push,
    )


async def handle_support_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    if message is None or update.effective_user is None:
        return False
    category_key = context.user_data.get("support_pending_category")
    if category_key:
        if category_key not in CATEGORIES:
            context.user_data.pop("support_pending_category", None)
            await show_support(update, context, push=False)
            return True
        if config.ADMIN_ID is None:
            await show_screen(
                update,
                context,
                "Support is temporarily unavailable. Please try again later.",
                reply_markup=add_navigation(None),
                push=False,
            )
            return True
        body = message.text or message.caption or "[Attachment]"
        telegram_message_id = getattr(message, "message_id", None)
        try:
            with transaction() as conn:
                if telegram_message_id is not None and conn.execute(
                    """SELECT 1 FROM support_messages
                       WHERE sender_id = ? AND telegram_message_id = ?""",
                    (update.effective_user.id, telegram_message_id),
                ).fetchone():
                    return True
                ticket = None
                ticket_id = context.user_data.get("active_ticket_id")
                if ticket_id:
                    ticket = conn.execute(
                        """SELECT * FROM support_tickets
                           WHERE id = ? AND user_id = ? AND status != 'RESOLVED'""",
                        (ticket_id, update.effective_user.id),
                    ).fetchone()
                if ticket is None:
                    ticket = conn.execute(
                        """SELECT * FROM support_tickets
                           WHERE user_id = ? AND status != 'RESOLVED'
                           ORDER BY updated_at DESC LIMIT 1""",
                        (update.effective_user.id,),
                    ).fetchone()

                now = utc_now()
                if ticket is None:
                    order = conn.execute(
                        "SELECT id FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                        (update.effective_user.id,),
                    ).fetchone()
                    ticket_id = (
                        f"CBT-{datetime.now(timezone.utc):%Y%m%d}-{token_hex(6).upper()}"
                    )
                    conn.execute(
                        """INSERT INTO support_tickets
                           (id, user_id, category, order_id, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, 'OPEN', ?, ?)""",
                        (
                            ticket_id,
                            update.effective_user.id,
                            CATEGORIES[category_key],
                            order["id"] if order else None,
                            now,
                            now,
                        ),
                    )
                    event_type = "SUPPORT_TICKET_SUBMITTED"
                else:
                    ticket_id = ticket["id"]
                    conn.execute(
                        "UPDATE support_tickets SET updated_at = ? WHERE id = ?",
                        (now, ticket_id),
                    )
                    event_type = "SUPPORT_REPLY_SUBMITTED"

                conn.execute(
                    """INSERT INTO support_messages
                       (ticket_id, sender_id, sender_type, message, created_at,
                        telegram_message_id)
                       VALUES (?, ?, 'CUSTOMER', ?, ?, ?)""",
                    (
                        ticket_id,
                        update.effective_user.id,
                        body,
                        now,
                        telegram_message_id,
                    ),
                )
                add_activity(
                    conn,
                    update.effective_user.id,
                    event_type,
                    {"ticket_id": ticket_id, "category": CATEGORIES[category_key]},
                    now,
                )
                conn.execute(
                    """INSERT INTO customer_messages
                       (telegram_id, sender_type, message, created_at)
                       VALUES (?, 'CUSTOMER', ?, ?)""",
                    (update.effective_user.id, body, now),
                )
                ticket = conn.execute(
                    "SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)
                ).fetchone()
        except sqlite3.Error:
            logger.exception(
                "Could not save support ticket for customer %s",
                update.effective_user.id,
            )
            await show_screen(
                update,
                context,
                "Your support request could not be saved right now. Please try again.",
                reply_markup=add_navigation(None),
                push=False,
            )
            return True

        context.user_data.pop("support_pending_category", None)
        context.user_data["active_ticket_id"] = ticket_id
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("💬 Reply", callback_data=f"adm:ticket_reply:{ticket_id}"),
                InlineKeyboardButton(
                    "✅ Mark Resolved", callback_data=f"adm:ticket_resolve:{ticket_id}"
                ),
                InlineKeyboardButton("📋 View Ticket", callback_data=f"adm:ticket:{ticket_id}"),
            ]]
        )
        confirmation = (
            "💬 <b>Support Request Submitted!</b>\n\n"
            "Thank you for contacting CodeBazar Support! 🛍️\n\n"
            "Your support request has been successfully received.\n\n"
            f"🎫 Ticket ID: <code>{escape(ticket_id)}</code>\n\n"
            f"📌 Category: {escape(ticket['category'])}\n\n"
            "⏳ Status: Waiting for Admin Reply\n\n"
            "Our support team will review your issue and respond to you here on Telegram.\n\n"
            "Please wait for our response. Thank you for your patience! ❤️\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "CodeBazar Customer Support"
        )
        try:
            await context.bot.send_message(
                config.ADMIN_ID,
                "💬 <b>NEW SUPPORT TICKET</b>\n\n"
                f"🎫 Ticket ID: <code>{escape(ticket_id)}</code>\n"
                f"👤 Customer: {escape(update.effective_user.full_name)}\n"
                f"🔗 Username: "
                f"{'@' + escape(update.effective_user.username) if update.effective_user.username else 'Not set'}\n"
                f"🆔 Telegram ID: <code>{update.effective_user.id}</code>\n"
                f"📌 Category: {escape(ticket['category'])}\n"
                f"📝 Issue: {escape(body[:500])}\n"
                f"🕒 Time: {escape(ticket['created_at'])}",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            if len(body) > 500:
                await context.bot.send_message(
                    config.ADMIN_ID,
                    f"📝 Issue continued for {ticket_id}:\n{body[500:]}",
                )
            if message.photo or message.document or message.video:
                await message.copy(
                    chat_id=config.ADMIN_ID, caption=f"Attachment for {ticket_id}"
                )
        except TelegramError:
            logger.exception("Could not notify admin about support ticket %s", ticket_id)
            logger.error(
                "Ticket %s is persisted and available in the admin ticket dashboard",
                ticket_id,
            )
        try:
            await show_screen(
                update,
                context,
                confirmation,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("📋 View Ticket", callback_data=f"support:ticket:{ticket_id}")],
                        [InlineKeyboardButton("🔙 Back", callback_data="nav:back")],
                        [InlineKeyboardButton("🏠 Home", callback_data="home")],
                    ]
                ),
                parse_mode=ParseMode.HTML,
                screen={"type": "ticket", "ticket_id": ticket_id},
                push=False,
            )
        except TelegramError:
            logger.exception(
                "Could not send support confirmation to customer %s for ticket %s",
                update.effective_user.id,
                ticket_id,
            )
        return True

    ticket_id = context.user_data.get("active_ticket_id")
    if ticket_id is None:
        with connection() as conn:
            row = conn.execute(
                """SELECT id FROM support_tickets WHERE user_id = ? AND status != 'RESOLVED'
                   ORDER BY updated_at DESC LIMIT 1""",
                (update.effective_user.id,),
            ).fetchone()
        ticket_id = row["id"] if row else None
    if ticket_id is None:
        return False
    body = message.text or message.caption or "[Attachment]"
    telegram_message_id = getattr(message, "message_id", None)
    try:
        with transaction() as conn:
            if telegram_message_id is not None and conn.execute(
                """SELECT 1 FROM support_messages
                   WHERE sender_id = ? AND telegram_message_id = ?""",
                (update.effective_user.id, telegram_message_id),
            ).fetchone():
                return True
            ticket = conn.execute(
                "SELECT * FROM support_tickets WHERE id = ? AND user_id = ?",
                (ticket_id, update.effective_user.id),
            ).fetchone()
            if ticket is None or ticket["status"] == "RESOLVED":
                context.user_data.pop("active_ticket_id", None)
                return False
            conn.execute(
                "UPDATE support_tickets SET updated_at = ? WHERE id = ?",
                (utc_now(), ticket_id),
            )
            conn.execute(
                """INSERT INTO support_messages
                   (ticket_id, sender_id, sender_type, message, created_at,
                    telegram_message_id)
                   VALUES (?, ?, 'CUSTOMER', ?, ?, ?)""",
                (
                    ticket_id,
                    update.effective_user.id,
                    body,
                    utc_now(),
                    telegram_message_id,
                ),
            )
            conn.execute(
                """INSERT INTO customer_messages
                   (telegram_id, sender_type, message, created_at)
                   VALUES (?, 'CUSTOMER', ?, ?)""",
                (update.effective_user.id, body, utc_now()),
            )
            add_activity(
                conn,
                update.effective_user.id,
                "SUPPORT_REPLY_SUBMITTED",
                {"ticket_id": ticket_id},
            )
    except sqlite3.Error:
        logger.exception(
            "Could not save customer reply for support ticket %s", ticket_id
        )
        await show_screen(
            update,
            context,
            "Your reply could not be saved right now. Please try again.",
            reply_markup=add_navigation(None),
            push=False,
        )
        return True
    if config.ADMIN_ID is None:
        await show_screen(
            update,
            context,
            f"Your reply was saved to ticket {ticket_id}, but support is temporarily unavailable."
        )
        return True
    try:
        await context.bot.send_message(
            config.ADMIN_ID,
            f"💬 CUSTOMER REPLY — {ticket_id}\n"
            f"Customer: {escape(update.effective_user.full_name)} "
            f"(@{escape(update.effective_user.username or 'not set')})\n"
            f"Telegram ID: {update.effective_user.id}\n\n{body[:3600]}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "💬 Reply to Ticket",
                            callback_data=f"adm:ticket_reply:{ticket_id}",
                        ),
                        InlineKeyboardButton(
                            "✅ Mark Resolved",
                            callback_data=f"adm:ticket_resolve:{ticket_id}",
                        ),
                        InlineKeyboardButton(
                            "📋 View Ticket",
                            callback_data=f"adm:ticket:{ticket_id}",
                        ),
                        InlineKeyboardButton(
                            "👤 Customer Profile",
                            callback_data=f"adm:customer:{update.effective_user.id}",
                        ),
                    ]
                ]
            ),
        )
        if message.photo or message.document or message.video:
            await message.copy(chat_id=config.ADMIN_ID, caption=f"Attachment for {ticket_id}")
    except TelegramError:
        logger.exception("Could not forward customer reply for support ticket %s", ticket_id)
        await show_screen(
            update,
            context,
            f"Your reply was saved to ticket {ticket_id}, but CodeBazar Support could not be notified.",
            reply_markup=add_navigation(None),
        )
        return True


    try:
        await show_screen(
            update,
            context,
            "✅ <b>Reply added to your ticket</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎫 Ticket: <code>{escape(ticket_id)}</code>\n"
            "📌 Status: <b>In progress</b>\n"
            "Our support team has received your update.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🎫 View Ticket", callback_data=f"support:ticket:{ticket_id}")],
                    [InlineKeyboardButton("🔙 Back", callback_data="nav:back")],
                    [InlineKeyboardButton("🏠 Home", callback_data="home")],
                ]
            ),
            parse_mode=ParseMode.HTML,
            screen={"type": "ticket", "ticket_id": ticket_id},
            push=False,
        )
    except TelegramError:
        logger.exception("Could not confirm customer reply for support ticket %s", ticket_id)
    return True


async def show_customer_tickets(
    update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    push: bool = True,
) -> None:
    user_id = update.effective_user.id
    offset = page * PAGE_SIZE
    with connection() as conn:
        tickets = conn.execute(
            """SELECT id, category, status, created_at FROM support_tickets
               WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (user_id, PAGE_SIZE + 1, offset),
        ).fetchall()
    has_next = len(tickets) > PAGE_SIZE
    tickets = tickets[:PAGE_SIZE]
    if not tickets:
        await show_screen(
            update,
            context,
            "📋 <b>My Support Tickets</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "You don't have any support tickets yet.",
            reply_markup=add_navigation(None),
            parse_mode=ParseMode.HTML,
            screen={"type": "tickets", "page": page},
            push=push,
        )
        return

    buttons = [
        [
            InlineKeyboardButton(
                f"🎫 {ticket['id']} · {ticket['status']}",
                callback_data=f"support:ticket:{ticket['id']}",
            )
        ]
        for ticket in tickets
    ]
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"support:tickets:{page - 1}")
        )
    if has_next:
        navigation.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"support:tickets:{page + 1}")
        )
    if navigation:
        buttons.append(navigation)
    entries = [
        f"🎫 <b>{escape(ticket['id'])}</b> · {escape(ticket['status'])}\n"
        f"📌 {escape(ticket['category'])} · {escape(ticket['created_at'])}"
        for ticket in tickets
    ]
    await show_screen(
        update,
        context,
        "📋 <b>MY SUPPORT TICKETS</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n\n".join(entries)
        + "\n\nSelect a ticket to view its conversation.",
        reply_markup=add_navigation(InlineKeyboardMarkup(buttons)),
        parse_mode=ParseMode.HTML,
        screen={"type": "tickets", "page": page},
        push=push,
    )


async def show_customer_ticket(
    update,
    context: ContextTypes.DEFAULT_TYPE,
    ticket_id: str,
    push: bool = True,
) -> None:
    user_id = update.effective_user.id
    with connection() as conn:
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE id = ? AND user_id = ?",
            (ticket_id, user_id),
        ).fetchone()
        if ticket is None:
            await show_screen(
                update,
                context,
                "That support ticket was not found.",
                reply_markup=add_navigation(None),
                push=push,
            )
            return
        messages = conn.execute(
            """SELECT sender_type, message, created_at FROM support_messages
               WHERE ticket_id = ? ORDER BY id DESC LIMIT 10""",
            (ticket_id,),
        ).fetchall()
    entries = [
        f"<b>{'You' if row['sender_type'] == 'CUSTOMER' else 'CodeBazar Support'}</b> "
        f"· {escape(row['created_at'])}\n{escape(row['message'][:700])}"
        for row in reversed(messages)
    ]
    context.user_data["active_ticket_id"] = ticket_id
    await show_screen(
        update,
        context,
        f"🎫 <b>{escape(ticket_id)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {escape(ticket['category'])}\n"
        f"Status: <b>{escape(ticket['status'])}</b>\n\n"
        + ("\n\n".join(entries) if entries else "No messages yet."),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💬 Reply", callback_data=f"support:reply:{ticket_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="nav:back")],
                [InlineKeyboardButton("🏠 Home", callback_data="home")],
            ]
        ),
        parse_mode=ParseMode.HTML,
        screen={"type": "ticket", "ticket_id": ticket_id},
        push=push,
    )


async def capture_customer_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return False
    body = message.text or message.caption
    if not body:
        return False
    with transaction() as conn:
        conn.execute(
            """INSERT INTO customer_messages
               (telegram_id, sender_type, message, created_at)
               VALUES (?, 'CUSTOMER', ?, ?)""",
            (user.id, body, utc_now()),
        )
        add_activity(
            conn,
            user.id,
            "CUSTOMER_MESSAGE_RECEIVED",
            {"message_length": len(body)},
        )
    try:
        await context.bot.send_message(
            config.ADMIN_ID,
            f"💬 <b>NEW CUSTOMER MESSAGE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Customer: {escape(user.full_name)} "
            f"(@{escape(user.username or 'not set')})\n"
            f"Telegram ID: <code>{user.id}</code>\n\n"
            f"{escape(body[:3200])}",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "👤 Customer Profile", callback_data=f"adm:customer:{user.id}"
                    ),
                    InlineKeyboardButton(
                        "↩️ Reply", callback_data=f"adm:customer_message:{user.id}"
                    ),
                ]]
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.exception("Could not notify admin about customer message from %s", user.id)
        await message.reply_text(
            "Your message was saved, but the support team could not be notified right now."
        )
        return True
    await message.reply_text("✅ Your message has been sent to CodeBazar Support.")
    return True
