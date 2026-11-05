import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456:LOCAL_TEST_TOKEN")
os.environ.setdefault("ADMIN_ID", "5940108626")

import config
from bot import callback_router, message_router, start
from database import connection, initialize_database, record_user
from keyboards import admin_dashboard, coupon_quantities, home_keyboard
from services import (
    add_coupon,
    coupon_stock,
    create_or_get_order,
    get_order,
    get_waiting_utr_order,
    get_waiting_utr_orders,
    submit_utr,
)
from telegram.error import Forbidden


class FakeBot:
    def __init__(self):
        self.messages = []
        self.photos = []
        self.fail_message_chat_ids = set()

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_message_chat_ids:
            raise Forbidden("bot can't initiate conversation")
        self.messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.messages))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append((chat_id, kwargs))
        return SimpleNamespace(message_id=len(self.photos))


class FakeContext:
    def __init__(self, bot=None):
        self.user_data = {}
        self.bot_data = {}
        self.bot = bot or FakeBot()


class FakeMessage:
    def __init__(self, chat_id, photo=False, text=None):
        self.chat_id = chat_id
        self.message_id = 1
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.photo = [object()] if photo else None
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeQuery:
    next_id = 0

    def __init__(self, user_id, data, photo=False):
        type(self).next_id += 1
        self.id = str(type(self).next_id)
        self.data = data
        self.from_user = SimpleNamespace(
            id=user_id,
            username=f"user{user_id}",
            first_name=f"User {user_id}",
        )
        self.message = FakeMessage(user_id, photo=photo)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text=None, **kwargs):
        self.edits.append(("text", {"text": text, **kwargs}))

    async def edit_message_caption(self, **kwargs):
        self.edits.append(("caption", kwargs))


def callback_update(user_id, data, photo=False):
    query = FakeQuery(user_id, data, photo)
    return SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=query.message.chat,
        effective_message=None,
    )


class TelegramCallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path = config.DATABASE_PATH
        config.DATABASE_PATH = Path(self.temp_dir.name) / "shein.db"
        initialize_database()
        with connection() as conn:
            conn.execute("DELETE FROM coupons")
        record_user(SimpleNamespace(id=101, username="buyer", first_name="Buyer"))

    def tearDown(self):
        config.DATABASE_PATH = self.old_path
        self.temp_dir.cleanup()

    async def test_admin_start_opens_panel_for_configured_admin(self):
        self.assertEqual(config.ADMIN_ID, 5940108626)
        update = SimpleNamespace(
            callback_query=None,
            effective_user=SimpleNamespace(
                id=config.ADMIN_ID, username="store_admin", first_name="Admin"
            ),
            effective_chat=SimpleNamespace(id=config.ADMIN_ID, type="private"),
            effective_message=FakeMessage(config.ADMIN_ID, text="/start"),
        )
        context = FakeContext()

        await start(update, context)

        self.assertIn("ADMIN PANEL", update.effective_message.replies[-1][0])
        self.assertIsNotNone(update.effective_message.replies[-1][1]["reply_markup"])

    async def test_every_static_menu_button_reaches_callback_router(self):
        for keyboard, user_id in (
            (home_keyboard(), 101),
            (coupon_quantities(), 101),
            (admin_dashboard(), config.ADMIN_ID),
        ):
            for row in keyboard.inline_keyboard:
                for button in row:
                    with self.subTest(callback_data=button.callback_data):
                        update = callback_update(user_id, button.callback_data)
                        context = FakeContext()
                        await callback_router(update, context)
                        self.assertTrue(update.callback_query.answers)
                        self.assertTrue(update.callback_query.edits)

    async def test_qr_utr_admin_approval_and_single_manual_delivery(self):
        add_coupon("TEST-COUPON-ONE")
        add_coupon("TEST-COUPON-TWO")
        context = FakeContext()

        update = callback_update(101, "buy:quantity:2")
        await callback_router(update, context)
        self.assertIn("Quantity: <b>2</b>", update.callback_query.edits[-1][1]["text"])
        self.assertIn("Total payment: <b>₹278</b>", update.callback_query.edits[-1][1]["text"])
        self.assertTrue(
            any(
                button.callback_data.startswith("buy:confirm:2:")
                for row in update.callback_query.edits[-1][1]["reply_markup"].inline_keyboard
                for button in row
            )
        )

        confirm_data = next(
            button.callback_data
            for row in update.callback_query.edits[-1][1]["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data.startswith("buy:confirm:2:")
        )
        update = callback_update(101, confirm_data)
        await callback_router(update, context)
        order = get_waiting_utr_order(101)
        self.assertIsNotNone(order)
        self.assertEqual(order["quantity"], 2)
        self.assertEqual(order["total_amount"], 278)
        self.assertEqual(len(context.bot.photos), 1)
        first_admin_notice = next(
            text
            for chat_id, text, _ in context.bot.messages
            if chat_id == config.ADMIN_ID
        )
        self.assertIn("Order status: Waiting for Payment", first_admin_notice)
        self.assertIn(order["id"], first_admin_notice)
        self.assertIn("Telegram ID: <code>101</code>", first_admin_notice)
        self.assertIn("@user101", first_admin_notice)
        self.assertIn("Coupon quantity: 2", first_admin_notice)
        self.assertIn("Total payment: ₹278", first_admin_notice)
        photo_caption = context.bot.photos[0][1]["caption"]
        self.assertIn("PAY ₹278", photo_caption)
        self.assertIn("Quantity: 2 coupon(s)", photo_caption)

        paid_update = callback_update(101, f"pay:paid:{order['id']}", photo=True)
        await callback_router(paid_update, context)
        self.assertEqual(paid_update.callback_query.edits[-1][0], "caption")

        message = FakeMessage(101, text="123456789012")
        await message_router(
            SimpleNamespace(
                callback_query=None,
                effective_user=SimpleNamespace(
                    id=101, username="buyer", first_name="Buyer"
                ),
                effective_chat=message.chat,
                effective_message=message,
            ),
            context,
        )
        self.assertIn("Payment Under Review", message.replies[-1][0])
        admin_notice = next(
            text
            for chat_id, text, _ in context.bot.messages
            if chat_id == config.ADMIN_ID and "PAYMENT UNDER REVIEW" in text
        )
        self.assertIn("Quantity: 2 coupon(s)", admin_notice)
        self.assertIn(order["id"], admin_notice)
        self.assertIn("Telegram ID: <code>101</code>", admin_notice)
        self.assertIn("@buyer", admin_notice)
        self.assertIn("Amount: ₹278", admin_notice)
        self.assertIn("UTR: <code>123456789012</code>", admin_notice)
        self.assertIn("Payment method: UPI QR", admin_notice)
        self.assertIn("Order status: Waiting for Admin Confirmation", admin_notice)

        admin_context = FakeContext(context.bot)
        review_notice = next(
            (kwargs.get("reply_markup") for chat_id, _, kwargs in context.bot.messages if chat_id == config.ADMIN_ID and kwargs.get("reply_markup")),
            None,
        )
        self.assertIsNotNone(review_notice)
        self.assertTrue(
            any(
                "APPROVE PAYMENT" in button.text
                for row in review_notice.inline_keyboard
                for button in row
            )
        )
        self.assertTrue(
            any(
                "REJECT PAYMENT" in button.text
                for row in review_notice.inline_keyboard
                for button in row
            )
        )
        review_callbacks = {
            button.callback_data
            for row in review_notice.inline_keyboard
            for button in row
        }
        self.assertIn(f"admin:approve:{order['id']}", review_callbacks)
        self.assertIn(f"admin:reject:{order['id']}", review_callbacks)
        approve_update = callback_update(
            config.ADMIN_ID, f"admin:approve:{order['id']}"
        )
        await callback_router(approve_update, admin_context)
        self.assertEqual(get_order(order["id"])["payment_status"], "APPROVED")
        result = get_order(order["id"])
        self.assertEqual(result["delivery_status"], "DELIVERED")
        customer_messages = [
            text
            for chat_id, text, _ in context.bot.messages
            if chat_id == 101 and text.startswith("🎟️ Your SHEIN Coupon Code:")
        ]
        self.assertEqual(len(customer_messages), 2)
        self.assertTrue(any("TEST-COUPON-ONE" in text for text in customer_messages))
        self.assertTrue(any("TEST-COUPON-TWO" in text for text in customer_messages))
        self.assertTrue(
            any(
                chat_id == config.ADMIN_ID and "Coupon delivery complete" in text
                for chat_id, text, _ in context.bot.messages
            )
        )

        duplicate_approve = callback_update(
            config.ADMIN_ID, f"admin:approve:{order['id']}"
        )
        before = len(context.bot.messages)
        await callback_router(duplicate_approve, admin_context)
        self.assertEqual(len(context.bot.messages), before)
        self.assertEqual(len(customer_messages), 2)

    async def test_duplicate_continue_callback_sends_one_payment_qr_and_notice(self):
        add_coupon("TEST-COUPON-ONE")
        context = FakeContext()
        for _ in range(2):
            update = callback_update(101, "buy:confirm:1")
            await callback_router(update, context)
        self.assertEqual(len(context.bot.photos), 1)
        payment_notices = [
            text for chat_id, text, _ in context.bot.messages if chat_id == config.ADMIN_ID
        ]
        self.assertEqual(len(payment_notices), 1)

    async def test_same_customer_multiple_orders_have_separate_notifications_and_reviews(self):
        add_coupon("TEST-MULTI-ORDER-ONE")
        add_coupon("TEST-MULTI-ORDER-TWO")
        context = FakeContext()

        await callback_router(callback_update(101, "buy:confirm:1:firstrequest"), context)
        first = get_waiting_utr_orders(101)[0]
        await callback_router(callback_update(101, "buy:confirm:1:secondrequest"), context)
        waiting = get_waiting_utr_orders(101)
        self.assertEqual(len(waiting), 2)
        second = next(order for order in waiting if order["id"] != first["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["total_amount"], 139)
        self.assertEqual(second["total_amount"], 139)
        self.assertEqual(
            sum(chat_id == config.ADMIN_ID and "NEW PAYMENT SCREEN" in text
                for chat_id, text, _ in context.bot.messages),
            2,
        )

        for order, utr in ((first, "111122223333"), (second, "444455556666")):
            await callback_router(
                callback_update(101, f"pay:paid:{order['id']}", photo=True),
                context,
            )
            message = FakeMessage(101, text=utr)
            await message_router(
                SimpleNamespace(
                    callback_query=None,
                    effective_user=SimpleNamespace(
                        id=101, username="buyer", first_name="Buyer"
                    ),
                    effective_chat=message.chat,
                    effective_message=message,
                ),
                context,
            )
            self.assertIn("Payment Under Review", message.replies[-1][0])

        review_notices = [
            (text, kwargs)
            for chat_id, text, kwargs in context.bot.messages
            if chat_id == config.ADMIN_ID and "PAYMENT UNDER REVIEW" in text
        ]
        self.assertEqual(len(review_notices), 2)
        for order, utr in ((first, "111122223333"), (second, "444455556666")):
            notice = next(text for text, _ in review_notices if order["id"] in text)
            self.assertIn(f"UTR: <code>{utr}</code>", notice)
            self.assertIn("Telegram ID: <code>101</code>", notice)

        await callback_router(
            callback_update(config.ADMIN_ID, f"admin:approve:{first['id']}"),
            FakeContext(context.bot),
        )
        await callback_router(
            callback_update(config.ADMIN_ID, f"admin:reject:{second['id']}"),
            FakeContext(context.bot),
        )
        self.assertEqual(get_order(first["id"])["payment_status"], "APPROVED")
        self.assertEqual(get_order(first["id"])["delivery_status"], "DELIVERED")
        self.assertEqual(get_order(second["id"])["payment_status"], "REJECTED")
        self.assertEqual(get_order(second["id"])["delivery_status"], "NOT_DELIVERED")

        orders_update = callback_update(101, "orders")
        await callback_router(orders_update, context)
        order_list = orders_update.callback_query.edits[-1][1]["text"]
        self.assertIn(first["id"], order_list)
        self.assertIn(second["id"], order_list)
        self.assertIn("APPROVED", order_list)
        self.assertIn("REJECTED", order_list)

    async def test_qr_admin_notification_failure_is_logged_and_shown_to_customer(self):
        add_coupon("TEST-QR-NOTICE-FAILURE")
        context = FakeContext()
        context.bot.fail_message_chat_ids.add(config.ADMIN_ID)
        update = callback_update(101, "buy:confirm:1")

        with self.assertLogs("bot", level="ERROR") as logs:
            await callback_router(update, context)

        warning = update.callback_query.edits[-1][1]["text"]
        self.assertIn("admin could not be notified", warning)
        self.assertIn("/start", warning)
        self.assertIn("/admin", warning)
        self.assertIn(config.ADMIN_ID.__str__(), logs.output[-1])
        self.assertIn("order_id=", logs.output[-1])
        self.assertEqual(len(context.bot.photos), 1)

    async def test_utr_admin_notification_failure_is_logged_and_not_reported_as_reviewed(self):
        add_coupon("TEST-UTR-NOTICE-FAILURE")
        context = FakeContext()
        await callback_router(callback_update(101, "buy:confirm:1"), context)
        order = get_waiting_utr_order(101)
        self.assertIsNotNone(order)
        context.bot.fail_message_chat_ids.add(config.ADMIN_ID)
        message = FakeMessage(101, text="123456789012")
        update = SimpleNamespace(
            callback_query=None,
            effective_user=SimpleNamespace(
                id=101, username="buyer", first_name="Buyer"
            ),
            effective_chat=message.chat,
            effective_message=message,
        )

        with self.assertLogs("bot", level="ERROR") as logs:
            await message_router(update, context)

        customer_notice = message.replies[-1][0]
        self.assertIn("UTR was saved", customer_notice)
        self.assertIn("Do not submit the UTR again", customer_notice)
        self.assertIn("/start", customer_notice)
        self.assertIn(order["id"], customer_notice)
        self.assertNotIn("Payment Under Review", customer_notice)
        self.assertIn(config.ADMIN_ID.__str__(), logs.output[-1])
        self.assertIn(order["id"], logs.output[-1])
        saved = get_order(order["id"])
        self.assertEqual(saved["payment_status"], "WAITING_FOR_ADMIN")

    async def test_fixed_quantity_buttons_show_exact_totals(self):
        for index in range(10):
            add_coupon(f"TEST-PRICE-{index}")
        context = FakeContext()
        expected_totals = {1: 139, 2: 278, 5: 695, 10: 1390}
        for quantity, total in expected_totals.items():
            update = callback_update(101, f"buy:quantity:{quantity}")
            await callback_router(update, context)
            text = update.callback_query.edits[-1][1]["text"]
            self.assertIn(f"Quantity: <b>{quantity}</b>", text)
            self.assertIn(f"Total payment: <b>₹{total}</b>", text)

    async def test_admin_rejection_releases_stock_and_duplicate_reject_is_blocked(self):
        add_coupon("TEST-REJECT-ONE")
        order = create_or_get_order(101, 1)
        submit_utr(order["id"], 101, "998877665544")
        context = FakeContext()
        reject = callback_update(config.ADMIN_ID, f"admin:reject:{order['id']}")
        await callback_router(reject, context)
        self.assertEqual(get_order(order["id"])["payment_status"], "REJECTED")
        self.assertEqual(coupon_stock()["available"], 1)
        self.assertEqual(coupon_stock()["delivered"], 0)
        self.assertTrue(
            any(chat_id == 101 and "was rejected" in text for chat_id, text, _ in context.bot.messages)
        )
        before = len(context.bot.messages)
        duplicate = callback_update(config.ADMIN_ID, f"admin:reject:{order['id']}")
        await callback_router(duplicate, context)
        self.assertEqual(len(context.bot.messages), before)

    async def test_non_admin_cannot_review_payment(self):
        add_coupon("TEST-ADMIN-ONLY")
        order = create_or_get_order(101)
        submit_utr(order["id"], 101, "112233445566")
        update = callback_update(101, f"admin:approve:{order['id']}")
        context = FakeContext()
        await callback_router(update, context)
        self.assertEqual(get_order(order["id"])["payment_status"], "WAITING_FOR_ADMIN")
        self.assertTrue(update.callback_query.answers[-1][1]["show_alert"])

    async def test_custom_quantity_rejects_invalid_input_and_checks_stock(self):
        add_coupon("TEST-CUSTOM-ONE")
        add_coupon("TEST-CUSTOM-TWO")
        context = FakeContext()
        custom = callback_update(101, "buy:custom")
        await callback_router(custom, context)
        self.assertTrue(context.user_data["awaiting_quantity"])

        for invalid in ("0", "-1", "1.5", "one"):
            message = FakeMessage(101, text=invalid)
            await message_router(
                SimpleNamespace(
                    callback_query=None,
                    effective_user=SimpleNamespace(
                        id=101, username="buyer", first_name="Buyer"
                    ),
                    effective_chat=message.chat,
                    effective_message=message,
                ),
                context,
            )
            self.assertIn("Invalid quantity", message.replies[-1][0])
            self.assertTrue(context.user_data["awaiting_quantity"])

        oversized = FakeMessage(101, text="3")
        await message_router(
            SimpleNamespace(
                callback_query=None,
                effective_user=SimpleNamespace(
                    id=101, username="buyer", first_name="Buyer"
                ),
                effective_chat=oversized.chat,
                effective_message=oversized,
            ),
            context,
        )
        self.assertIn("Only 2 coupon(s) are available", oversized.replies[-1][0])
        self.assertTrue(context.user_data["awaiting_quantity"])

        valid = FakeMessage(101, text="2")
        await message_router(
            SimpleNamespace(
                callback_query=None,
                effective_user=SimpleNamespace(
                    id=101, username="buyer", first_name="Buyer"
                ),
                effective_chat=valid.chat,
                effective_message=valid,
            ),
            context,
        )
        self.assertIn("Total payment: ₹278", valid.replies[-1][0])
        self.assertNotIn("awaiting_quantity", context.user_data)


if __name__ == "__main__":
    unittest.main()
