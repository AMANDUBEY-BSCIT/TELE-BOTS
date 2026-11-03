import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from telegram.error import TelegramError

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")

import config
from database import (
    connection,
    get_all_products,
    get_product,
    initialize_database,
    is_user_blocked,
    record_order_payment_event,
    record_user,
)
from handlers.admin import (
    _show_activity_feed,
    _show_customer,
    _show_customer_activity,
    _show_customer_inbox,
    _show_customer_messages,
    _show_customer_orders,
    _show_customer_payments,
    _show_customers,
    _show_delivery_queue,
    _show_overview,
    _show_payment_details,
    _show_payment_monitoring,
    _show_product_analytics,
    _show_page,
    _show_revenue,
    _show_settings,
    _show_stock,
    handle_admin_callback,
    handle_admin_input,
    is_admin,
)
from handlers.orders import handle_recovery
from handlers.support import (
    capture_customer_message,
    handle_support_callback,
    handle_support_input,
)
from keyboards.admin_keyboard import dashboard
from keyboards.user_keyboard import main_menu, product_list, quantities
from services.delivery_service import DeliveryError, deliver_codes
from services.order_service import (
    OrderError,
    accept_terms,
    create_order,
    current_available_stock,
)
from services.payment_service import (
    PaymentError,
    approve_payment,
    reject_payment,
    submit_utr,
)


class FakeMessage:
    next_message_id = 1000

    def __init__(self, text: str = ""):
        self.text = text
        self.message_id = None
        self.chat_id = None
        self.photo = None
        self.caption = None
        self.document = None
        self.video = None
        self.replies = []
        self.reply_kwargs = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)
        self.reply_kwargs.append(kwargs)
        FakeMessage.next_message_id += 1
        return SimpleNamespace(message_id=FakeMessage.next_message_id)

    async def copy(self, **kwargs):
        return None


class FakeBot:
    def __init__(self):
        self.sent = []
        self.sent_photos = []
        self.sent_kwargs = []
        self.edited = []
        self.edited_markup = []
        self.deleted = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        self.sent_kwargs.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        self.edited_markup.append(kwargs)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.sent.append((chat_id, "photo"))
        self.sent_photos.append((chat_id, photo.read(), kwargs))
        FakeMessage.next_message_id += 1
        return SimpleNamespace(message_id=FakeMessage.next_message_id)

    async def edit_message_caption(self, **kwargs):
        self.edited.append(kwargs)


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.bot_data = {}
        self.bot = FakeBot()


class FakeTelegramFile:
    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(b"customer-provided-qr-bytes")


class FakePhoto:
    async def get_file(self):
        return FakeTelegramFile()


class FakeCallbackQuery:
    next_query_id = 0

    def __init__(self, user_id: int, data: str):
        FakeCallbackQuery.next_query_id += 1
        self.id = str(FakeCallbackQuery.next_query_id)
        self.from_user = SimpleNamespace(
            id=user_id,
            username=f"user{user_id}",
            first_name=f"User {user_id}",
            full_name=f"User {user_id}",
        )
        self.data = data
        self.message = FakeMessage()
        self.message.chat = SimpleNamespace(type="private")
        self.effective_chat = self.message.chat
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def make_update(user_id: int, username: str, message: FakeMessage):
    user = SimpleNamespace(
        id=user_id,
        username=username,
        first_name=username,
        full_name=username,
    )
    return SimpleNamespace(
        effective_user=user,
        effective_message=message,
        effective_chat=SimpleNamespace(type="private"),
        callback_query=None,
    )


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_database_path = config.DATABASE_PATH
        self.old_admin_id = config.ADMIN_ID
        config.DATABASE_PATH = Path(self.temp_dir.name) / "codebazar.db"
        config.ADMIN_ID = 9001
        initialize_database()
        record_user(SimpleNamespace(id=101, username="buyer", first_name="Buyer"))
        record_user(SimpleNamespace(id=202, username="other", first_name="Other"))

    def tearDown(self):
        config.DATABASE_PATH = self.old_database_path
        config.ADMIN_ID = self.old_admin_id
        self.temp_dir.cleanup()

    async def test_admin_reply_keyboard_buttons_reach_customer_menu_router(self):
        from bot import route_message

        menu_labels = (
            "🛒 Buy Voucher",
            "📦 My Orders",
            "🔄 Recover Voucher",
            "💬 Support",
        )
        context = FakeContext()
        menu_handler = AsyncMock()
        admin_input_handler = AsyncMock()
        with (
            patch("bot.handle_menu_text", menu_handler),
            patch("bot.handle_admin_input", admin_input_handler),
        ):
            for label in menu_labels:
                with self.subTest(button=label):
                    message = FakeMessage(label)
                    update = make_update(9001, "admin", message)
                    await route_message(update, context)
            self.assertEqual(menu_handler.await_count, len(menu_labels))
            self.assertEqual(
                [call.args[0].effective_message.text for call in menu_handler.await_args_list],
                list(menu_labels),
            )
            admin_input_handler.assert_not_awaited()

    async def test_customer_menu_buttons_reach_their_existing_features(self):
        from bot import route_message

        expectations = {
            "🛒 Buy Voucher": "THE CODEBAZAR STORE",
            "📦 My Orders": "YOUR ORDERS",
            "🔄 Recover Voucher": "VOUCHER RECOVERY",
            "💬 Support": "CODEBAZAR CARE",
        }
        for label, expected in expectations.items():
            with self.subTest(button=label):
                message = FakeMessage(label)
                await route_message(
                    make_update(101, "buyer", message),
                    FakeContext(),
                )
                self.assertIn(expected, "\n".join(message.replies))

    async def test_unexpected_text_repeats_menu_without_creating_data(self):
        from bot import route_message

        context = FakeContext()
        messages = [
            FakeMessage("hello"),
            FakeMessage("What is the weather?"),
            FakeMessage("unrecognized request"),
        ]
        for message in messages:
            await route_message(make_update(101, "buyer", message), context)
            self.assertEqual(
                message.replies[0],
                "⚠️ Please choose an option from the menu below.\n\n"
                "🛍️ Select one of the available options to continue using CodeBazar.",
            )
            self.assertIsNotNone(message.reply_kwargs[0]["reply_markup"])

        with connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM customer_messages").fetchone()[0], 0)

    async def test_active_customer_inputs_are_not_swallowed_by_fallback(self):
        from bot import route_message

        custom_context = FakeContext()
        custom_context.user_data["custom_product"] = "gemini_pro"
        custom_message = FakeMessage("not a quantity")
        await route_message(make_update(101, "buyer", custom_message), custom_context)
        self.assertIn("valid positive", custom_message.replies[0].lower())
        self.assertEqual(custom_context.user_data["custom_product"], "gemini_pro")

        utr_context = FakeContext()
        utr_context.user_data["awaiting_utr"] = "missing-order"
        utr_message = FakeMessage("not a UTR")
        await route_message(make_update(101, "buyer", utr_message), utr_context)
        self.assertIn("12-digit", utr_message.replies[0].lower())
        self.assertEqual(utr_context.user_data["awaiting_utr"], "missing-order")

        support_context = FakeContext()
        support_context.user_data["support_pending_category"] = "payment"
        support_message = FakeMessage("My payment is delayed.")
        await route_message(make_update(101, "buyer", support_message), support_context)
        self.assertIn("Support Request Submitted", support_message.replies[0])

    def test_customer_and_admin_keyboard_layouts_keep_callback_routes(self):
        from bot import build_application

        menu = main_menu()
        self.assertEqual(
            [[button.text for button in row] for row in menu.inline_keyboard],
            [
                ["🛒 Buy Voucher", "📦 My Orders"],
                ["🔄 Recover Voucher", "💬 Support"],
            ],
        )
        self.assertEqual(
            [button.callback_data for row in menu.inline_keyboard for button in row],
            ["shop:list", "orders:0", "recover:start", "support:menu"],
        )

        products = product_list(
            [
                {"id": "gemini_pro", "name": "Gemini Pro"},
                {"id": "pvr_movie", "name": "PVR Movie"},
            ]
        )
        self.assertEqual(
            [row[0].callback_data for row in products.inline_keyboard],
            ["product:gemini_pro", "product:pvr_movie"],
        )

        quantity_buttons = quantities("gemini_pro").inline_keyboard
        self.assertEqual([len(row) for row in quantity_buttons], [2, 2])
        self.assertEqual(
            [button.callback_data for row in quantity_buttons for button in row],
            [
                "qty:gemini_pro:1",
                "qty:gemini_pro:5",
                "qty:gemini_pro:10",
                "qty:gemini_pro:custom",
            ],
        )

        admin_rows = dashboard().inline_keyboard
        self.assertEqual([len(row) for row in admin_rows], [2, 2, 2, 2, 2, 2, 1, 1])
        self.assertEqual(
            [button.callback_data for row in admin_rows for button in row],
            [
                "adm:overview",
                "adm:customers:0",
                "adm:live:0",
                "adm:payments:0",
                "adm:orders:0",
                "adm:pending:0",
                "adm:revenue",
                "adm:delivery:0",
                "adm:tickets:0",
                "adm:search",
                "adm:analytics",
                "adm:broadcast",
                "adm:settings",
                "adm:back",
            ],
        )
        callback_handlers = [
            handler
            for group in build_application().handlers.values()
            for handler in group
            if hasattr(handler, "pattern")
        ]
        callback_data = [
            "home",
            "nav:back",
            "shop:back",
            "shop:list",
            "shop:cancel_custom",
            "product:gemini_pro",
            "product:pvr_movie",
            "product:pvr_giftcard",
            "qty:gemini_pro:1",
            "qty:gemini_pro:5",
            "qty:gemini_pro:10",
            "qty:gemini_pro:custom",
            "agree:CB-ORDER",
            "paid:CB-ORDER",
            "cancel:CB-ORDER",
            "orders:0",
            "recover:start",
            "recover:cancel",
            "support:order",
            "support:payment",
            "support:voucher",
            "support:other",
            "support:cancel",
            "support:menu",
            "support:tickets:0",
            "support:ticket:CBT-20261031-ABC123",
            "support:reply:CBT-20261031-ABC123",
            "adm:products",
            "adm:approve:CB-ORDER",
            "adm:reject:CB-ORDER",
            "adm:back",
            "adm:ticket_reply:CBT-123",
            "adm:ticket_resolve:CBT-123",
            "adm:customer:101",
            "adm:customer_orders:101:0",
            "adm:customer_payments:101:0",
            "adm:customer_activity:101:0",
            "adm:customer_messages:101",
            "adm:customer_block:101",
            "adm:payments:0",
            "adm:payment:CB-ORDER",
            "adm:live:0",
            "adm:activity_customer",
            "adm:activity_type",
            "adm:activity_date",
            "adm:activity_event:QR_SENT",
            "adm:activity_clear",
            "adm:revenue",
            "adm:delivery:0",
            "adm:analytics",
            "adm:search",
            "adm:customer_message:101",
            "adm:inbox:0",
        ]
        for callback in callback_data:
            with self.subTest(callback=callback):
                self.assertTrue(
                    any(handler.pattern.match(callback) for handler in callback_handlers),
                    f"No callback handler is registered for {callback}",
                )

    def test_seeded_catalog_and_stock_reservation(self):
        products = {row["id"]: row for row in get_all_products()}
        self.assertEqual(
            (products["gemini_pro"]["name"], products["gemini_pro"]["price"], products["gemini_pro"]["stock"]),
            ("Gemini Pro 18 Months", 119, 45),
        )
        self.assertEqual((products["pvr_movie"]["price"], products["pvr_movie"]["stock"]), (35, 135))
        self.assertEqual((products["pvr_giftcard"]["price"], products["pvr_giftcard"]["stock"]), (109, 15))

        order_id = create_order(101, "gemini_pro", 2)
        self.assertEqual(create_order(101, "gemini_pro", 2), order_id)
        self.assertEqual(current_available_stock("gemini_pro"), 43)
        order = accept_terms(order_id, 101)
        self.assertEqual(order["total_amount"], 238)
        with self.assertRaises(OrderError):
            accept_terms(order_id, 202)

    def test_manual_payment_and_single_delivery_updates_stock_once(self):
        order_id = create_order(101, "pvr_movie", 2)
        accept_terms(order_id, 101)
        record_order_payment_event(order_id, "PAYMENT_SCREEN_REACHED")
        record_order_payment_event(order_id, "QR_SENT")
        record_order_payment_event(order_id, "PAYMENT_BUTTON_CLICKED")
        submit_utr(order_id, 101, "123456789012")
        with self.assertRaises(PaymentError):
            submit_utr(order_id, 101, "123456789012")
        approved = approve_payment(order_id)
        self.assertEqual(approved["payment_status"], "PENDING_VERIFICATION")
        with self.assertRaises(PaymentError):
            approve_payment(order_id)
        delivered = deliver_codes(order_id, ["MOVIE-A", "MOVIE-B"])
        self.assertEqual(delivered["codes"], ["MOVIE-A", "MOVIE-B"])
        with connection() as conn:
            payment = conn.execute(
                """SELECT reviewed_at, qr_sent_at, paid_clicked_at
                   FROM payments WHERE order_id = ?""",
                (order_id,),
            ).fetchone()
            order_timestamps = conn.execute(
                """SELECT payment_screen_at, qr_sent_at, paid_clicked_at, utr_submitted_at
                   FROM orders WHERE id = ?""",
                (order_id,),
            ).fetchone()
            event_types = {
                row["event_type"]
                for row in conn.execute(
                    "SELECT event_type FROM payment_events WHERE order_id = ?",
                    (order_id,),
                )
            }
            self.assertIsNotNone(payment["reviewed_at"])
            self.assertIsNotNone(payment["qr_sent_at"])
            self.assertIsNotNone(payment["paid_clicked_at"])
            self.assertTrue(all(order_timestamps))
            self.assertTrue(
                {
                    "TERMS_ACCEPTED",
                    "UTR_SUBMITTED",
                    "PAYMENT_APPROVED",
                    "VOUCHER_DELIVERY_RECORDED",
                }.issubset(event_types)
            )
        self.assertEqual(get_product("pvr_movie")["stock"], 133)
        with self.assertRaises(DeliveryError):
            deliver_codes(order_id, ["MOVIE-A", "MOVIE-B"])
        with connection() as conn:
            order = conn.execute(
                "SELECT payment_status, delivery_status FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            self.assertEqual(tuple(order), ("APPROVED", "DELIVERED"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM voucher_codes").fetchone()[0], 2)

    def test_duplicate_codes_and_utr_reuse_are_rejected(self):
        first = create_order(101, "pvr_giftcard", 1)
        accept_terms(first, 101)
        submit_utr(first, 101, "111122223333")
        approve_payment(first)
        deliver_codes(first, ["GIFT-CODE"])

        second = create_order(101, "pvr_giftcard", 1)
        accept_terms(second, 101)
        with self.assertRaisesRegex(PaymentError, "already"):
            submit_utr(second, 101, "111122223333")
        submit_utr(second, 101, "444455556666")
        approve_payment(second)
        with self.assertRaisesRegex(DeliveryError, "already delivered"):
            deliver_codes(second, ["GIFT-CODE"])
        self.assertEqual(get_product("pvr_giftcard")["stock"], 14)

    def test_rejection_and_expiration_release_reservations(self):
        rejected = create_order(101, "gemini_pro", 3)
        accept_terms(rejected, 101)
        submit_utr(rejected, 101, "777788889999")
        reject_payment(rejected)
        self.assertEqual(current_available_stock("gemini_pro"), 45)

        expired = create_order(101, "gemini_pro", 4)
        accept_terms(expired, 101)
        with connection() as conn:
            conn.execute(
                "UPDATE orders SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (expired,),
            )
        with self.assertRaisesRegex(PaymentError, "expired"):
            submit_utr(expired, 101, "888899990000")
        with connection() as conn:
            row = conn.execute(
                "SELECT payment_status, reserved FROM orders WHERE id = ?", (expired,)
            ).fetchone()
        self.assertEqual(tuple(row), ("EXPIRED", 0))
        self.assertEqual(current_available_stock("gemini_pro"), 45)

    async def test_support_reply_persistence_and_admin_allowlist(self):
        self.assertTrue(is_admin(9001))
        self.assertFalse(is_admin(101))
        config.ADMIN_ID = None
        self.assertFalse(is_admin(9001))
        config.ADMIN_ID = 9001

        customer_context = FakeContext()
        customer_context.user_data["support_pending_category"] = "payment"
        created_message = FakeMessage("My payment is pending.")
        await handle_support_input(
            make_update(101, "buyer", created_message),
            customer_context,
        )
        with connection() as conn:
            ticket = conn.execute("SELECT * FROM support_tickets").fetchone()
            self.assertEqual(ticket["status"], "OPEN")
            ticket_id = ticket["id"]
        initialize_database()
        with connection() as conn:
            persisted_ticket = conn.execute(
                "SELECT id, category FROM support_tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            self.assertEqual(persisted_ticket["id"], ticket_id)
            self.assertEqual(
                conn.execute(
                    "SELECT message FROM support_messages WHERE ticket_id = ? ORDER BY id LIMIT 1",
                    (ticket_id,),
                ).fetchone()["message"],
                "My payment is pending.",
            )
        self.assertIn("💬 <b>Support Request Submitted!</b>", created_message.replies[0])
        self.assertIn("📌 Category: Payment Issue", created_message.replies[0])
        self.assertIn(ticket_id, created_message.replies[0])
        self.assertEqual(customer_context.bot.sent[0][0], 9001)
        self.assertIn("💬 <b>NEW SUPPORT TICKET</b>", customer_context.bot.sent[0][1])
        self.assertIn("📝 Issue: My payment is pending.", customer_context.bot.sent[0][1])
        self.assertIn("🆔 Telegram ID: <code>101</code>", customer_context.bot.sent[0][1])
        admin_markup = customer_context.bot.sent_kwargs[0]["reply_markup"]
        callback_data = {
            button.callback_data
            for row in admin_markup.inline_keyboard
            for button in row
        }
        self.assertEqual(
            callback_data,
            {
                f"adm:ticket_reply:{ticket_id}",
                f"adm:ticket_resolve:{ticket_id}",
                f"adm:ticket:{ticket_id}",
            },
        )

        await handle_support_input(
            make_update(101, "buyer", FakeMessage("Here is another detail.")),
            customer_context,
        )
        admin_context = FakeContext()
        admin_context.user_data["admin_action"] = ("ticket_reply", ticket_id)
        await handle_admin_input(
            make_update(9001, "admin", FakeMessage("We are checking this.")),
            admin_context,
        )
        self.assertEqual(admin_context.bot.sent[0][0], 101)
        self.assertIn("IN PROGRESS", admin_context.bot.sent[0][1])
        with connection() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM support_messages WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM support_tickets WHERE id = ?", (ticket_id,)
                ).fetchone()[0],
                "IN PROGRESS",
            )
        from handlers.admin import _resolve_ticket

        await _resolve_ticket(FakeMessage(), ticket_id, admin_context)
        with connection() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM support_tickets WHERE id = ?", (ticket_id,)
                ).fetchone()[0],
                "RESOLVED",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT message FROM support_messages WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
                    (ticket_id,),
                ).fetchone()[0],
                "Ticket marked resolved.",
            )
        self.assertIn("marked resolved", admin_context.bot.sent[-1][1])

    async def test_support_menu_ticket_list_and_customer_scoped_ticket_history(self):
        context = FakeContext()
        context.user_data["support_pending_category"] = "payment"
        await handle_support_input(
            make_update(101, "buyer", FakeMessage("Payment did not arrive.")),
            context,
        )
        with connection() as conn:
            ticket_id = conn.execute("SELECT id FROM support_tickets").fetchone()["id"]

        list_query = FakeCallbackQuery(101, "support:tickets:0")
        await handle_support_callback(
            SimpleNamespace(
                callback_query=list_query,
                effective_user=list_query.from_user,
                effective_chat=list_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(len(list_query.answers), 1)
        self.assertIn(ticket_id, list_query.message.replies[0])
        ticket_markup = list_query.message.reply_kwargs[0]["reply_markup"]
        self.assertTrue(
            any(
                button.callback_data == f"support:ticket:{ticket_id}"
                for row in ticket_markup.inline_keyboard
                for button in row
            )
        )

        owner_query = FakeCallbackQuery(101, f"support:ticket:{ticket_id}")
        await handle_support_callback(
            SimpleNamespace(
                callback_query=owner_query,
                effective_user=owner_query.from_user,
                effective_chat=owner_query.effective_chat,
            ),
            context,
        )
        self.assertIn("Payment did not arrive.", owner_query.message.replies[0])

        other_query = FakeCallbackQuery(202, f"support:ticket:{ticket_id}")
        await handle_support_callback(
            SimpleNamespace(
                callback_query=other_query,
                effective_user=other_query.from_user,
                effective_chat=other_query.effective_chat,
            ),
            context,
        )
        self.assertIn("not found", other_query.message.replies[0].lower())
        self.assertNotIn("Payment did not arrive.", other_query.message.replies[0])

    async def test_duplicate_support_submission_reuses_open_ticket(self):
        context = FakeContext()
        context.user_data["support_pending_category"] = "payment"
        first_message = FakeMessage("First message.")
        first_message.message_id = 2001
        await handle_support_input(
            make_update(101, "buyer", first_message),
            context,
        )
        context.user_data["support_pending_category"] = "order"
        second_message = FakeMessage("Second message.")
        second_message.message_id = 2002
        await handle_support_input(
            make_update(101, "buyer", second_message),
            context,
        )
        context.user_data["support_pending_category"] = "order"
        await handle_support_input(
            make_update(101, "buyer", second_message),
            context,
        )
        with connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0], 1
            )
            ticket = conn.execute("SELECT id FROM support_tickets").fetchone()
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM support_messages WHERE ticket_id = ?",
                    (ticket["id"],),
                ).fetchone()[0],
                2,
            )
        self.assertIn(ticket["id"], context.user_data["active_ticket_id"])

    async def test_callback_guard_acknowledges_and_drops_rapid_duplicate(self):
        from handlers.callback_guard import guard_callback

        entered = asyncio.Event()
        finish = asyncio.Event()
        calls = 0

        async def slow_handler(update, context):
            nonlocal calls
            calls += 1
            entered.set()
            await finish.wait()

        guarded = guard_callback(slow_handler)
        context = FakeContext()
        first = FakeCallbackQuery(101, "qty:pvr_movie:5")
        second = FakeCallbackQuery(101, "qty:pvr_movie:5")
        for query in (first, second):
            query.message.message_id = 456
            query.message.chat_id = 101
        first_task = asyncio.create_task(
            guarded(
                SimpleNamespace(callback_query=first),
                context,
            )
        )
        await entered.wait()
        await guarded(
            SimpleNamespace(callback_query=second),
            context,
        )
        self.assertEqual(calls, 1)
        self.assertEqual(len(first.answers), 1)
        self.assertIn("processing", second.answers[0][0].lower())
        finish.set()
        await first_task
        self.assertFalse(context.bot_data["_callback_guard"]["active"])

    async def test_callback_guard_releases_user_lock_after_handler_failure(self):
        from handlers.callback_guard import guard_callback
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        async def failing_handler(update, context):
            raise RuntimeError("expected test failure")

        query = FakeCallbackQuery(101, "agree:CB-ORDER")
        query.message.chat_id = 101
        query.message.message_id = 321
        original_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Accept", callback_data="agree:CB-ORDER")]]
        )
        query.message.reply_markup = original_markup
        context = FakeContext()
        with self.assertRaisesRegex(RuntimeError, "expected test failure"):
            await guard_callback(failing_handler)(
                SimpleNamespace(callback_query=query),
                context,
            )
        self.assertFalse(context.bot_data["_callback_guard"]["active"])
        self.assertNotIn(
            query.id,
            context.bot_data["_acknowledged_callback_ids"],
        )
        self.assertEqual(len(context.bot.edited_markup), 2)
        self.assertIsNone(context.bot.edited_markup[0]["reply_markup"])
        self.assertIs(
            context.bot.edited_markup[1]["reply_markup"],
            original_markup,
        )

    async def test_callback_guard_does_not_restore_stale_keyboard(self):
        from handlers.callback_guard import guard_callback
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.error import BadRequest

        async def failing_handler(update, context):
            raise RuntimeError("expected test failure")

        query = FakeCallbackQuery(101, "home")
        query.message.chat_id = 101
        query.message.message_id = 321
        query.message.reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Home", callback_data="home")]]
        )
        context = FakeContext()
        context.bot.edit_message_reply_markup = AsyncMock(
            side_effect=BadRequest("Message is not modified")
        )

        with self.assertRaisesRegex(RuntimeError, "expected test failure"):
            await guard_callback(failing_handler)(
                SimpleNamespace(callback_query=query),
                context,
            )

        context.bot.edit_message_reply_markup.assert_awaited_once()

    async def test_callback_acknowledges_expired_queries_without_failing_action(self):
        from handlers.callback_guard import answer_callback
        from telegram.error import BadRequest

        query = FakeCallbackQuery(101, "home")
        query.answer = AsyncMock(
            side_effect=BadRequest("Query is too old and response timeout expired")
        )
        await answer_callback(query, FakeContext())

    async def test_error_handler_suppresses_duplicate_edit_notice(self):
        from bot import error_handler
        from telegram import Update
        from telegram.error import BadRequest

        message = FakeMessage("current message")
        context = SimpleNamespace(error=BadRequest("Message is not modified"))
        await error_handler(Update(update_id=1, message=message), context)
        self.assertEqual(message.replies, [])

    def test_database_migrates_support_message_id_without_losing_existing_rows(self):
        with connection() as conn:
            now = "2026-11-03T00:00:00+00:00"
            conn.execute(
                """INSERT INTO support_tickets
                   (id, user_id, category, status, created_at, updated_at)
                   VALUES ('CBT-LEGACY', 101, 'Payment Issue', 'OPEN', ?, ?)""",
                (now, now),
            )
            conn.execute(
                """INSERT INTO support_messages
                   (ticket_id, sender_id, sender_type, message, created_at)
                   VALUES ('CBT-LEGACY', 101, 'CUSTOMER', 'Existing support record', ?)""",
                (now,),
            )
            conn.execute("DROP INDEX idx_support_messages_source")
            conn.execute("ALTER TABLE support_messages DROP COLUMN telegram_message_id")

        initialize_database()
        with connection() as conn:
            row = conn.execute(
                """SELECT message, telegram_message_id FROM support_messages
                   WHERE ticket_id = 'CBT-LEGACY'"""
            ).fetchone()
        self.assertEqual(tuple(row), ("Existing support record", None))

    async def test_support_save_failure_does_not_confirm_ticket(self):
        context = FakeContext()
        context.user_data["support_pending_category"] = "payment"
        message = FakeMessage("Payment is pending.")
        with patch(
            "handlers.support.transaction",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            handled = await handle_support_input(
                make_update(101, "buyer", message),
                context,
            )
        self.assertTrue(handled)
        self.assertIn("could not be saved", message.replies[0].lower())
        self.assertNotIn("Support Request Submitted", message.replies[0])
        self.assertEqual(context.user_data["support_pending_category"], "payment")
        with connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0], 0)

    async def test_support_confirmation_sent_even_if_admin_notification_fails(self):
        context = FakeContext()
        context.user_data["support_pending_category"] = "payment"
        message = FakeMessage("I need payment help.")
        with patch.object(
            context.bot,
            "send_message",
            new=AsyncMock(side_effect=TelegramError("admin unavailable")),
        ):
            await handle_support_input(
                make_update(101, "buyer", message),
                context,
            )
        self.assertIn("💬 <b>Support Request Submitted!</b>", message.replies[0])
        with connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0], 1)

    async def test_custom_quantities_create_exact_orders_and_totals(self):
        from handlers.products import handle_custom_quantity

        for quantity in (2, 7, 15):
            with self.subTest(quantity=quantity):
                context = FakeContext()
                context.user_data["custom_product"] = "pvr_movie"
                message = FakeMessage(str(quantity))
                handled = await handle_custom_quantity(
                    make_update(101, "buyer", message),
                    context,
                )
                self.assertTrue(handled)
                self.assertNotIn("custom_product", context.user_data)
                terms_markup = message.reply_kwargs[-1]["reply_markup"]
                terms_callback = terms_markup.inline_keyboard[0][0].callback_data
                order_id = terms_callback.split(":", 1)[1]
                with connection() as conn:
                    order = conn.execute(
                        "SELECT quantity, total_amount FROM orders WHERE id = ?",
                        (order_id,),
                    ).fetchone()
                self.assertEqual(tuple(order), (quantity, 35 * quantity))
                self.assertIn("PLEASE REVIEW YOUR ORDER", "\n".join(message.replies))
                self.assertIn(str(quantity), "\n".join(message.replies))

    async def test_custom_quantity_invalid_stock_cancel_and_duplicate_guard(self):
        from handlers.products import handle_custom_quantity, handle_product_callback

        context = FakeContext()
        context.user_data["custom_product"] = "gemini_pro"
        for invalid in ("0", "-2", "words"):
            message = FakeMessage(invalid)
            self.assertTrue(
                await handle_custom_quantity(make_update(101, "buyer", message), context)
            )
            self.assertIn("positive", message.replies[0].lower())
            self.assertEqual(context.user_data["custom_product"], "gemini_pro")
        with connection() as conn:
            stock = conn.execute(
                "SELECT stock FROM products WHERE id = 'gemini_pro'"
            ).fetchone()["stock"]
        insufficient = FakeMessage(str(stock + 1))
        await handle_custom_quantity(
            make_update(101, "buyer", insufficient),
            context,
        )
        self.assertIn("Insufficient Stock", insufficient.replies[0])
        self.assertIn(str(stock), insufficient.replies[0])
        self.assertEqual(context.user_data["custom_product"], "gemini_pro")
        with connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

        cancel_query = FakeCallbackQuery(101, "shop:cancel_custom")
        context.user_data["custom_product"] = "gemini_pro"
        await handle_product_callback(
            SimpleNamespace(
                callback_query=cancel_query,
                effective_user=cancel_query.from_user,
                effective_chat=cancel_query.effective_chat,
            ),
            context,
        )
        self.assertNotIn("custom_product", context.user_data)
        self.assertEqual(cancel_query.answers, [(None, {})])
        cancel_markup = cancel_query.message.reply_kwargs[0]["reply_markup"]
        self.assertTrue(
            any(
                button.callback_data.startswith("qty:gemini_pro:")
                for row in cancel_markup.inline_keyboard
                for button in row
            )
        )

        custom_query = FakeCallbackQuery(101, "qty:pvr_movie:custom")
        context.user_data["customer_current_screen"] = {
            "type": "product",
            "product_id": "pvr_movie",
        }
        await handle_product_callback(
            SimpleNamespace(
                callback_query=custom_query,
                effective_user=custom_query.from_user,
                effective_chat=custom_query.effective_chat,
            ),
            context,
        )
        self.assertIn("🛒 <b>Custom Quantity</b>", custom_query.message.replies[0])
        custom_markup = custom_query.message.reply_kwargs[0]["reply_markup"]
        self.assertEqual(
            custom_markup.inline_keyboard[0][0].text,
            "🔙 Back",
        )

    async def test_customer_navigation_edits_the_same_menu_message_and_back_restores_context(self):
        from handlers.customer_ui import handle_back
        from handlers.products import handle_product_callback

        context = FakeContext()
        context.user_data.update(
            customer_menu_message_id=501,
            customer_menu_chat_id=101,
            customer_menu_owner=101,
            customer_current_screen={"type": "home", "text": "Welcome"},
        )
        shop_query = FakeCallbackQuery(101, "shop:list")
        await handle_product_callback(
            SimpleNamespace(
                callback_query=shop_query,
                effective_user=shop_query.from_user,
                effective_chat=shop_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], 501)
        self.assertEqual(context.bot.edited[-1]["chat_id"], 101)
        product_button = next(
            button
            for row in context.bot.edited[-1]["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data == "product:pvr_movie"
        )
        self.assertEqual(product_button.text, "PVR MOVIE ₹200 OFF")

        product_query = FakeCallbackQuery(101, product_button.callback_data)
        await handle_product_callback(
            SimpleNamespace(
                callback_query=product_query,
                effective_user=product_query.from_user,
                effective_chat=product_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], 501)
        self.assertIn("available now", context.bot.edited[-1]["text"])

        quantity_query = FakeCallbackQuery(101, "qty:pvr_movie:5")
        await handle_product_callback(
            SimpleNamespace(
                callback_query=quantity_query,
                effective_user=quantity_query.from_user,
                effective_chat=quantity_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], 501)
        self.assertIn("PLEASE REVIEW YOUR ORDER", context.bot.edited[-1]["text"])
        self.assertEqual(context.user_data["customer_current_screen"]["type"], "terms")

        back_query = FakeCallbackQuery(101, "nav:back")
        await handle_back(
            SimpleNamespace(
                callback_query=back_query,
                effective_user=back_query.from_user,
                effective_chat=back_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], 501)
        self.assertIn("available now", context.bot.edited[-1]["text"])
        self.assertEqual(
            context.user_data["customer_current_screen"],
            {"type": "product", "product_id": "pvr_movie"},
        )

    async def test_start_orders_and_recovery_navigation_use_the_customer_menu_message(self):
        from handlers.customer_ui import handle_back
        from handlers.orders import handle_orders_callback, handle_recovery
        from handlers.start import start

        context = FakeContext()
        start_message = FakeMessage("/start")
        await start(make_update(101, "buyer", start_message), context)
        self.assertEqual(
            start_message.replies[0],
            "Welcome to CodeBazar 🛍️\n\nYour Trusted Digital Voucher Store",
        )
        self.assertEqual(context.user_data["customer_menu_owner"], 101)
        menu_message_id = context.user_data["customer_menu_message_id"]
        menu_markup = start_message.reply_kwargs[0]["reply_markup"]
        self.assertFalse(
            any(
                button.text == "🔙 Back"
                for row in menu_markup.inline_keyboard
                for button in row
            )
        )

        orders_query = FakeCallbackQuery(101, "orders:0")
        await handle_orders_callback(
            SimpleNamespace(
                callback_query=orders_query,
                effective_user=orders_query.from_user,
                effective_chat=orders_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], menu_message_id)
        self.assertIn("YOUR ORDERS", context.bot.edited[-1]["text"])

        recover_query = FakeCallbackQuery(101, "recover:start")
        await handle_orders_callback(
            SimpleNamespace(
                callback_query=recover_query,
                effective_user=recover_query.from_user,
                effective_chat=recover_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], menu_message_id)
        self.assertIn("Send your Order ID", context.bot.edited[-1]["text"])
        recovery_message = FakeMessage("not-an-order")
        await handle_recovery(
            make_update(101, "buyer", recovery_message),
            context,
        )
        self.assertIn("Order not found", context.bot.edited[-1]["text"])

        back_query = FakeCallbackQuery(101, "nav:back")
        await handle_back(
            SimpleNamespace(
                callback_query=back_query,
                effective_user=back_query.from_user,
                effective_chat=back_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], menu_message_id)
        self.assertIn("YOUR ORDERS", context.bot.edited[-1]["text"])

    async def test_support_category_ticket_and_reply_screens_edit_the_same_message(self):
        from handlers.support import handle_support_callback, handle_support_input

        context = FakeContext()
        context.user_data.update(
            customer_menu_message_id=777,
            customer_menu_chat_id=101,
            customer_menu_owner=101,
            customer_current_screen={"type": "home"},
        )
        support_query = FakeCallbackQuery(101, "support:menu")
        await handle_support_callback(
            SimpleNamespace(
                callback_query=support_query,
                effective_user=support_query.from_user,
                effective_chat=support_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], 777)
        category_query = FakeCallbackQuery(101, "support:payment")
        await handle_support_callback(
            SimpleNamespace(
                callback_query=category_query,
                effective_user=category_query.from_user,
                effective_chat=category_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], 777)
        self.assertIn("Payment Issue", context.bot.edited[-1]["text"])

        await handle_support_input(
            make_update(101, "buyer", FakeMessage("Payment status question.")),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], 777)
        self.assertIn("Support Request Submitted", context.bot.edited[-1]["text"])
        with connection() as conn:
            ticket_id = conn.execute(
                "SELECT id FROM support_tickets WHERE user_id = ?", (101,)
            ).fetchone()["id"]

        ticket_query = FakeCallbackQuery(101, f"support:ticket:{ticket_id}")
        await handle_support_callback(
            SimpleNamespace(
                callback_query=ticket_query,
                effective_user=ticket_query.from_user,
                effective_chat=ticket_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], 777)
        self.assertIn("Payment status question.", context.bot.edited[-1]["text"])
        reply_query = FakeCallbackQuery(101, f"support:reply:{ticket_id}")
        await handle_support_callback(
            SimpleNamespace(
                callback_query=reply_query,
                effective_user=reply_query.from_user,
                effective_chat=reply_query.effective_chat,
            ),
            context,
        )
        self.assertIn("Send your reply", context.bot.edited[-1]["text"])
        await handle_support_input(
            make_update(101, "buyer", FakeMessage("Additional details.")),
            context,
        )
        self.assertEqual(context.bot.edited[-1]["message_id"], 777)
        self.assertIn("Reply added", context.bot.edited[-1]["text"])

    async def test_navigation_ignores_not_modified_and_never_edits_another_users_menu(self):
        from telegram.error import BadRequest

        from handlers.customer_ui import show_screen

        context = FakeContext()
        context.user_data.update(
            customer_menu_message_id=888,
            customer_menu_chat_id=101,
            customer_menu_owner=101,
        )
        update = make_update(101, "buyer", FakeMessage())
        with patch.object(
            context.bot,
            "edit_message_text",
            new=AsyncMock(side_effect=BadRequest("Message is not modified")),
        ):
            self.assertTrue(
                await show_screen(
                    update,
                    context,
                    "Same screen",
                    screen={"type": "home"},
                )
            )

        other_query = FakeCallbackQuery(202, "home")
        previous_edits = list(context.bot.edited)
        self.assertFalse(
            await show_screen(
                SimpleNamespace(
                    callback_query=other_query,
                    effective_user=other_query.from_user,
                    effective_chat=other_query.effective_chat,
                ),
                context,
                "Do not edit another user's menu",
                screen={"type": "home"},
            )
        )
        self.assertEqual(context.bot.edited, previous_edits)

    async def test_uploaded_qr_is_persisted_and_used_for_payment(self):
        from handlers.admin import handle_admin_input
        from handlers.payments import _send_payment_details

        old_qr_directory = config.QR_DIRECTORY
        old_qr_path = config.QR_IMAGE_PATH
        config.QR_DIRECTORY = Path(self.temp_dir.name) / "media" / "qr_code"
        config.QR_IMAGE_PATH = config.QR_DIRECTORY / "current_qr.jpg"
        try:
            config.QR_DIRECTORY.mkdir(parents=True)
            admin_context = FakeContext()
            admin_context.user_data["admin_action"] = ("qr", "")
            upload_message = FakeMessage()
            upload_message.photo = [FakePhoto()]
            await handle_admin_input(
                make_update(9001, "admin", upload_message),
                admin_context,
            )
            self.assertTrue(config.QR_IMAGE_PATH.is_file())
            self.assertEqual(
                config.QR_IMAGE_PATH.read_bytes(),
                b"customer-provided-qr-bytes",
            )

            order_id = create_order(101, "pvr_movie", 1)
            order = accept_terms(order_id, 101)
            payment_context = FakeContext()
            await _send_payment_details(101, order, payment_context)
            self.assertEqual(
                payment_context.bot.sent_photos[0][1],
                b"customer-provided-qr-bytes",
            )
            initialize_database()
            self.assertEqual(
                config.QR_IMAGE_PATH.read_bytes(),
                b"customer-provided-qr-bytes",
            )
        finally:
            config.QR_DIRECTORY = old_qr_directory
            config.QR_IMAGE_PATH = old_qr_path

    async def test_payment_requires_uploaded_qr_before_terms_are_accepted(self):
        from handlers.payments import handle_payment_callback

        order_id = create_order(101, "pvr_movie", 1)
        old_qr_path = config.QR_IMAGE_PATH
        config.QR_IMAGE_PATH = Path(self.temp_dir.name) / "missing-qr.jpg"
        try:
            query = FakeCallbackQuery(101, f"agree:{order_id}")
            await handle_payment_callback(
                SimpleNamespace(
                    callback_query=query,
                    effective_user=query.from_user,
                    effective_chat=query.effective_chat,
                ),
                FakeContext(),
            )
        finally:
            config.QR_IMAGE_PATH = old_qr_path
        self.assertIn("temporarily unavailable", query.message.replies[0].lower())
        with connection() as conn:
            status = conn.execute(
                "SELECT payment_status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()["payment_status"]
        self.assertEqual(status, "TERMS_PENDING")

    async def test_customer_payment_flow_uses_order_details_and_preserves_utr_and_cancel(self):
        from handlers.payments import handle_payment_callback, handle_utr_input
        from handlers.products import handle_product_callback

        context = FakeContext()
        context.user_data.update(
            customer_menu_message_id=1234,
            customer_menu_chat_id=101,
            customer_menu_owner=101,
            customer_current_screen={"type": "home"},
        )
        product_query = FakeCallbackQuery(101, "product:pvr_movie")
        await handle_product_callback(
            SimpleNamespace(
                callback_query=product_query,
                effective_user=product_query.from_user,
                effective_chat=product_query.effective_chat,
            ),
            context,
        )
        quantity_query = FakeCallbackQuery(101, "qty:pvr_movie:5")
        await handle_product_callback(
            SimpleNamespace(
                callback_query=quantity_query,
                effective_user=quantity_query.from_user,
                effective_chat=quantity_query.effective_chat,
            ),
            context,
        )
        terms_screen = context.bot.edited[-1]
        self.assertEqual(terms_screen["message_id"], 1234)
        self.assertIn("PLEASE REVIEW YOUR ORDER", terms_screen["text"])
        terms_markup = terms_screen["reply_markup"]
        agree_data = terms_markup.inline_keyboard[0][0].callback_data
        order_id = agree_data.split(":", 1)[1]
        with connection() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        self.assertEqual(
            (order["product_name"], order["quantity"], order["total_amount"]),
            ("PVR Movie ₹200 Off Voucher (2 Tickets)", 5, 175),
        )
        edits_after_order = len(context.bot.edited)
        await handle_product_callback(
            SimpleNamespace(
                callback_query=quantity_query,
                effective_user=quantity_query.from_user,
                effective_chat=quantity_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(len(context.bot.edited), edits_after_order)
        with connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)

        agree_query = FakeCallbackQuery(101, agree_data)
        agree_query.message.chat_id = 101
        await handle_payment_callback(
            SimpleNamespace(
                callback_query=agree_query,
                effective_user=agree_query.from_user,
                effective_chat=agree_query.effective_chat,
            ),
            context,
        )
        repeated_agree_query = FakeCallbackQuery(101, agree_data)
        repeated_agree_query.message.chat_id = 101
        await handle_payment_callback(
            SimpleNamespace(
                callback_query=repeated_agree_query,
                effective_user=repeated_agree_query.from_user,
                effective_chat=repeated_agree_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(len(context.bot.sent_photos), 1)
        photo = context.bot.sent_photos[0]
        self.assertEqual(photo[1], config.QR_IMAGE_PATH.read_bytes())
        self.assertEqual(
            photo[2]["caption"],
            "💳 MAKE PAYMENT HERE\n\n"
            "🛍️ Product: PVR Movie ₹200 Off Voucher (2 Tickets)\n"
            "🔢 Quantity: 5\n"
            "💰 Total Amount: ₹175\n"
            f"🆔 Order ID: {order_id}\n\n"
            "⏳ QR Validity: 10 Minutes\n\n"
            "Please scan the QR code and pay the exact amount shown above.\n\n"
            "After completing payment, click the button below and submit your 12-digit UTR number.",
        )
        payment_buttons = photo[2]["reply_markup"].inline_keyboard[0]
        self.assertEqual(
            [(button.text, button.callback_data) for button in payment_buttons],
            [
                ("✅ I've Paid", f"paid:{order_id}"),
                ("❌ Cancel Order", f"cancel:{order_id}"),
            ],
        )

        paid_query = FakeCallbackQuery(101, f"paid:{order_id}")
        paid_query.from_user.first_name = "Buyer"
        paid_query.from_user.username = "buyer"
        paid_query.message.message_id = context.user_data["customer_payment_photo_id"]
        paid_query.message.chat_id = 101
        paid_query.message.photo = True
        await handle_payment_callback(
            SimpleNamespace(
                callback_query=paid_query,
                effective_user=paid_query.from_user,
                effective_chat=paid_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(len(paid_query.answers), 1)
        self.assertEqual(context.user_data["awaiting_utr"], order_id)
        self.assertIn(
            (101, paid_query.message.message_id),
            context.bot.deleted,
        )
        prompt_screen = context.bot.edited[-1]
        self.assertEqual(
            prompt_screen["text"],
            "Please enter your 12-digit UTR number to verify your payment.",
        )
        self.assertEqual(
            [[button.text for button in row] for row in prompt_screen["reply_markup"].inline_keyboard],
            [["🏠 Home"]],
        )
        self.assertNotIn("caption", prompt_screen)
        utr_message = FakeMessage("123456789012")
        self.assertTrue(
            await handle_utr_input(make_update(101, "buyer", utr_message), context)
        )
        self.assertNotIn("awaiting_utr", context.user_data)
        with connection() as conn:
            submitted = conn.execute(
                """SELECT o.payment_status, p.utr, p.status
                   FROM orders o JOIN payments p ON p.order_id = o.id WHERE o.id = ?""",
                (order_id,),
            ).fetchone()
        self.assertEqual(
            tuple(submitted),
            ("PENDING_VERIFICATION", "123456789012", "PENDING_VERIFICATION"),
        )
        confirmation = context.bot.edited[-1]
        self.assertEqual(
            confirmation["text"],
            "<b>⏳ Payment Under Review</b>\n"
            "Your payment details have been submitted successfully.\n\n"
            f"<b>Order ID:</b> {order_id}\n\n"
            "<b>Amount:</b> ₹175\n\n"
            "<b>UTR:</b> 123456789012\n\n"
            "<b>Status:</b> Waiting for Admin Confirmation\n\n"
            "Our team is checking your payment. We will notify you once your payment has been verified.\n\n"
            "Thank you for choosing CodeBazar 🛍️",
        )
        self.assertEqual(
            [[button.text for button in row] for row in confirmation["reply_markup"].inline_keyboard],
            [["🏠 HOME"]],
        )
        admin_notification = next(
            text for chat_id, text in context.bot.sent if chat_id == config.ADMIN_ID and "PAYMENT VERIFICATION REQUIRED" in text
        )
        for expected in (
            "Order ID:",
            "Customer name: Buyer",
            "Username: @buyer",
            "Telegram ID: <code>101</code>",
            "Product: PVR Movie ₹200 Off Voucher (2 Tickets)",
            "Quantity: 5",
            "Amount: ₹175",
            "UTR: <code>123456789012</code>",
            "Submission timestamp:",
            "Status: PENDING VERIFICATION",
        ):
            self.assertIn(expected, admin_notification)

        previous_screen_edits = len(context.bot.edited)
        repeated_paid_query = FakeCallbackQuery(101, f"paid:{order_id}")
        await handle_payment_callback(
            SimpleNamespace(
                callback_query=repeated_paid_query,
                effective_user=repeated_paid_query.from_user,
                effective_chat=repeated_paid_query.effective_chat,
            ),
            context,
        )
        self.assertEqual(len(repeated_paid_query.answers), 1)
        self.assertEqual(len(context.bot.edited), previous_screen_edits)
        with connection() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM payments WHERE order_id = ?", (order_id,)
                ).fetchone()[0],
                1,
            )

        cancelled_order_id = create_order(101, "pvr_movie", 1)
        accept_terms(cancelled_order_id, 101)
        cancel_query = FakeCallbackQuery(101, f"cancel:{cancelled_order_id}")
        await handle_payment_callback(
            SimpleNamespace(
                callback_query=cancel_query,
                effective_user=cancel_query.from_user,
                effective_chat=cancel_query.effective_chat,
            ),
            context,
        )
        with connection() as conn:
            cancelled = conn.execute(
                "SELECT payment_status, reserved FROM orders WHERE id = ?",
                (cancelled_order_id,),
            ).fetchone()
        self.assertEqual(tuple(cancelled), ("CANCELLED", 0))

    async def test_invalid_utr_is_rejected_without_trimming_and_reprompts(self):
        from handlers.payments import handle_utr_input

        order_id = create_order(101, "pvr_movie", 1)
        accept_terms(order_id, 101)
        context = FakeContext()
        context.user_data.update(
            awaiting_utr=order_id,
            customer_menu_message_id=5678,
            customer_menu_chat_id=101,
            customer_menu_owner=101,
        )
        invalid_utrs = (
            " 123456789012",
            "123456789012 ",
            "12345 789012",
            "１２３４５６７８９０１２",
            "abcdefghijkl",
            "12345678901",
            "1234567890123",
            "1234-6789012",
        )
        for value in invalid_utrs:
            with self.subTest(value=value):
                message = FakeMessage(value)
                self.assertTrue(await handle_utr_input(make_update(101, "buyer", message), context))
                self.assertEqual(
                    context.bot.edited[-1]["text"],
                    "<b>⚠️ Invalid UTR Number</b>\n\n"
                    "Please check your UTR number and try again.\n\n"
                    "Your UTR must contain exactly <b>12 digits</b>.\n\n"
                    "Please enter a valid 12-digit UTR number.",
                )
                self.assertEqual(
                    [[button.text for button in row] for row in context.bot.edited[-1]["reply_markup"].inline_keyboard],
                    [["🏠 Home"]],
                )
                self.assertNotIn("caption", context.bot.edited[-1])
                with connection() as conn:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0],
                        0,
                    )
        self.assertEqual(context.user_data["awaiting_utr"], order_id)

    async def test_utr_save_failure_does_not_show_success_or_clear_pending_input(self):
        from handlers.payments import handle_utr_input

        context = FakeContext()
        context.user_data.update(
            awaiting_utr="order-1",
            customer_menu_message_id=5678,
            customer_menu_chat_id=101,
            customer_menu_owner=101,
        )
        message = FakeMessage("123456789012")
        with patch(
            "handlers.payments.submit_utr",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            self.assertTrue(await handle_utr_input(make_update(101, "buyer", message), context))
        self.assertEqual(context.user_data["awaiting_utr"], "order-1")
        self.assertIn("could not save your payment details", context.bot.edited[-1]["text"].lower())
        self.assertNotIn("caption", context.bot.edited[-1])
        self.assertNotIn(
            "Payment Under Review",
            "\n".join(edit.get("text", "") for edit in context.bot.edited),
        )

    async def test_qr_read_error_reports_payment_unavailable(self):
        from handlers.payments import handle_payment_callback

        order_id = create_order(101, "pvr_movie", 1)
        query = FakeCallbackQuery(101, f"agree:{order_id}")
        query.message.chat_id = 101
        context = FakeContext()
        with patch.object(Path, "open", side_effect=OSError("read failure")):
            await handle_payment_callback(
                SimpleNamespace(
                    callback_query=query,
                    effective_user=query.from_user,
                    effective_chat=query.effective_chat,
                ),
                context,
            )
        self.assertTrue(
            any("temporarily unavailable" in message.lower() for _, message in context.bot.sent)
        )

    async def test_admin_callback_rejects_non_admin_and_customer_message_is_persisted(self):
        context = FakeContext()
        query = FakeCallbackQuery(101, "adm:customers:0")
        await handle_admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        )
        self.assertEqual(query.answers[0][0], "Unauthorized.")
        self.assertEqual(query.answers[0][1]["show_alert"], True)
        admin_group_query = FakeCallbackQuery(9001, "adm:customers:0")
        await handle_admin_callback(
            SimpleNamespace(
                callback_query=admin_group_query,
                effective_chat=SimpleNamespace(type="group"),
            ),
            context,
        )
        self.assertEqual(admin_group_query.answers[0][0], "Unauthorized.")

        message = FakeMessage("I need help with my order.")
        handled = await capture_customer_message(
            make_update(101, "buyer", message),
            FakeContext(),
        )
        self.assertTrue(handled)
        with connection() as conn:
            saved = conn.execute(
                """SELECT sender_type, message FROM customer_messages
                   WHERE telegram_id = 101 ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            event = conn.execute(
                """SELECT event_type FROM user_activity
                   WHERE telegram_id = 101 ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        self.assertEqual(tuple(saved), ("CUSTOMER", "I need help with my order."))
        self.assertEqual(event["event_type"], "CUSTOMER_MESSAGE_RECEIVED")

    async def test_admin_private_reply_and_customer_block_persist(self):
        context = FakeContext()
        context.user_data["admin_action"] = ("customer_message", "101")
        await handle_admin_input(
            make_update(9001, "admin", FakeMessage("We are happy to help.")),
            context,
        )
        self.assertEqual(context.bot.sent[0][0], 101)
        with connection() as conn:
            saved = conn.execute(
                """SELECT sender_type, message FROM customer_messages
                   WHERE telegram_id = 101 ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        self.assertEqual(tuple(saved), ("ADMIN", "We are happy to help."))

        query = FakeCallbackQuery(9001, "adm:customer_block:101")
        await handle_admin_callback(
            SimpleNamespace(
                callback_query=query,
                effective_user=query.from_user,
                effective_chat=query.effective_chat,
            ),
            FakeContext(),
        )
        self.assertTrue(is_user_blocked(101))

    async def test_admin_dashboard_screens_query_persisted_data(self):
        context = FakeContext()
        screens = [
            lambda message: _show_overview(message),
            lambda message: _show_customers(message, 0),
            lambda message: _show_customer(message, 101),
            lambda message: _show_customer_orders(message, 101, 0),
            lambda message: _show_customer_payments(message, 101, 0),
            lambda message: _show_customer_activity(message, 101, 0),
            lambda message: _show_customer_messages(message, 101),
            lambda message: _show_customer_inbox(message, 0),
            lambda message: _show_payment_monitoring(message, 0),
            lambda message: _show_payment_details(message, "missing-order"),
            lambda message: _show_revenue(message),
            lambda message: _show_delivery_queue(message, 0),
            lambda message: _show_product_analytics(message),
            lambda message: _show_page(message, "tickets", 0),
            lambda message: _show_page(message, "orders", 0),
            lambda message: _show_page(message, "pending", 0),
            lambda message: _show_page(message, "users", 0),
            lambda message: _show_settings(message),
            lambda message: _show_stock(message),
        ]
        for render in screens:
            await render(FakeMessage())
        await _show_activity_feed(FakeMessage(), 0, context)

        query = FakeCallbackQuery(9001, "adm:overview")
        await handle_admin_callback(
            SimpleNamespace(
                callback_query=query,
                effective_user=query.from_user,
                effective_chat=query.effective_chat,
            ),
            context,
        )
        self.assertIn("CODEBAZAR OVERVIEW", query.message.replies[0])

    async def test_qr_send_tracking_records_sent_not_viewed(self):
        from handlers.payments import _send_payment_details

        order_id = create_order(101, "pvr_movie", 1)
        order = accept_terms(order_id, 101)
        old_qr_path = config.QR_IMAGE_PATH
        qr_path = Path(self.temp_dir.name) / "test-qr.jpg"
        qr_path.write_bytes(b"test QR image bytes")
        config.QR_IMAGE_PATH = qr_path
        context = FakeContext()
        try:
            await _send_payment_details(101, order, context)
        finally:
            config.QR_IMAGE_PATH = old_qr_path

        with connection() as conn:
            row = conn.execute(
                """SELECT payment_screen_at, qr_sent_at FROM orders WHERE id = ?""",
                (order_id,),
            ).fetchone()
            event_types = {
                item["event_type"]
                for item in conn.execute(
                    "SELECT event_type FROM payment_events WHERE order_id = ?",
                    (order_id,),
                )
            }
        self.assertTrue(row["payment_screen_at"])
        self.assertTrue(row["qr_sent_at"])
        self.assertIn("QR_SENT", event_types)
        self.assertNotIn("QR_VIEWED", event_types)
        self.assertEqual(context.bot.sent[0], (101, "photo"))
        self.assertEqual(len([item for item in context.bot.sent if item[0] == 9001]), 2)

    async def test_recovery_is_scoped_to_the_order_owner(self):
        order_id = create_order(101, "pvr_movie", 1)
        accept_terms(order_id, 101)
        submit_utr(order_id, 101, "555566667777")
        approve_payment(order_id)
        deliver_codes(order_id, ["PRIVATE-CODE"])

        other_context = FakeContext()
        other_context.user_data["recover_waiting"] = True
        other_message = FakeMessage(order_id)
        await handle_recovery(make_update(202, "other", other_message), other_context)
        self.assertIn("not found", other_message.replies[0].lower())
        self.assertNotIn("PRIVATE-CODE", other_message.replies[0])

        owner_context = FakeContext()
        owner_context.user_data["recover_waiting"] = True
        owner_message = FakeMessage(order_id)
        await handle_recovery(make_update(101, "buyer", owner_message), owner_context)
        self.assertIn("PRIVATE-CODE", "\n".join(owner_message.replies))

    async def test_admin_can_deliver_codes_in_batches(self):
        order_id = create_order(101, "pvr_movie", 2)
        accept_terms(order_id, 101)
        submit_utr(order_id, 101, "666677778888")
        approve_payment(order_id)
        context = FakeContext()
        context.user_data["admin_action"] = ("deliver", order_id)
        context.user_data["delivery_codes"] = []

        first_batch = FakeMessage("CODE-ONE")
        await handle_admin_input(make_update(9001, "admin", first_batch), context)
        self.assertIn("1 of 2", first_batch.replies[0])
        self.assertEqual(get_product("pvr_movie")["stock"], 135)

        second_batch = FakeMessage("CODE-TWO")
        await handle_admin_input(make_update(9001, "admin", second_batch), context)
        self.assertIn("stock updated", second_batch.replies[0])
        self.assertEqual(get_product("pvr_movie")["stock"], 133)
        self.assertTrue(any("CODE-ONE" in sent[1] for sent in context.bot.sent))
        self.assertTrue(any("CODE-TWO" in sent[1] for sent in context.bot.sent))

    def test_database_survives_reopening_connections(self):
        order_id = create_order(101, "pvr_movie", 1)
        with connection() as conn:
            self.assertIsNotNone(
                conn.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
            )
        with connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
                3,
            )


if __name__ == "__main__":
    unittest.main()
