import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456:LOCAL_TEST_TOKEN")
os.environ.setdefault("ADMIN_ID", "5940108626")

import config
from database import connection, initialize_database, record_user
from initial_inventory import INITIAL_COUPON_CODES
from services import (
    StoreError,
    add_coupon,
    coupon_stock,
    create_or_get_order,
    create_support_message,
    get_support_ticket,
    record_coupon_delivery,
    review_payment,
    save_admin_reply,
    submit_utr,
)


class StoreWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path = config.DATABASE_PATH
        config.DATABASE_PATH = Path(self.temp_dir.name) / "shein.db"
        initialize_database()
        initialize_database()
        with connection() as conn:
            conn.execute("DELETE FROM coupons")
        record_user(SimpleNamespace(id=101, username="buyer", first_name="Buyer"))
        record_user(SimpleNamespace(id=202, username="second", first_name="Second"))

    def tearDown(self):
        config.DATABASE_PATH = self.old_path
        self.temp_dir.cleanup()

    def test_order_confirm_is_idempotent_for_its_request_and_reserves_real_codes(self):
        with self.assertRaises(StoreError):
            create_or_get_order(101)
        add_coupon("TEST-COUPON-ONE")
        first = create_or_get_order(101, purchase_request_id="confirm-once")
        second = create_or_get_order(101, purchase_request_id="confirm-once")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(coupon_stock()["reserved"], 1)
        self.assertEqual(coupon_stock()["available"], 0)

    def test_same_customer_can_have_independent_pending_orders(self):
        for code in ("ORDER-A-COUPON", "ORDER-B-COUPON-1", "ORDER-B-COUPON-2"):
            add_coupon(code)
        first = create_or_get_order(101, 1, "request-order-a")
        second = create_or_get_order(101, 2, "request-order-b")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["total_amount"], 139)
        self.assertEqual(second["total_amount"], 278)
        self.assertEqual(coupon_stock()["reserved"], 3)

        submit_utr(first["id"], 101, "111122223333")
        with self.assertRaisesRegex(StoreError, "already been submitted"):
            submit_utr(second["id"], 101, "111122223333")
        submit_utr(second["id"], 101, "444455556666")

        rejected = review_payment(first["id"], approved=False)
        self.assertEqual(rejected["payment_status"], "REJECTED")
        still_pending = review_payment(second["id"], approved=True)
        self.assertEqual(still_pending["payment_status"], "APPROVED")
        self.assertEqual(len(still_pending["codes"]), 2)
        for coupon in still_pending["codes"]:
            record_coupon_delivery(second["id"], coupon["id"], succeeded=True)

        with connection() as conn:
            statuses = {
                row["id"]: (row["payment_status"], row["delivery_status"])
                for row in conn.execute(
                    "SELECT id, payment_status, delivery_status FROM orders WHERE user_id = ?",
                    (101,),
                )
            }
        self.assertEqual(statuses[first["id"]][0], "REJECTED")
        self.assertEqual(statuses[second["id"]], ("APPROVED", "DELIVERED"))
        self.assertEqual(coupon_stock()["available"], 1)
        self.assertEqual(coupon_stock()["delivered"], 2)

    def test_simultaneous_orders_cannot_oversell_inventory(self):
        add_coupon("ONLY-ONE-COUPON")

        def place_order(user_id):
            try:
                return create_or_get_order(user_id, 1)
            except StoreError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(place_order, (101, 202)))
        self.assertEqual(sum(result is not None for result in results), 1)
        stock = coupon_stock()
        self.assertEqual(stock["reserved"], 1)
        self.assertEqual(stock["available"], 0)

    def test_quantity_prices_and_available_stock_limits(self):
        for index in range(18):
            add_coupon(f"TEST-QUANTITY-{index:02d}")
        expected_totals = (139, 278, 695, 1390)
        for offset, (quantity, total) in enumerate(
            zip((1, 2, 5, 10), expected_totals), start=301
        ):
            record_user(
                SimpleNamespace(id=offset, username=f"user{offset}", first_name="Buyer")
            )
            order = create_or_get_order(offset, quantity)
            self.assertEqual(order["quantity"], quantity)
            self.assertEqual(order["amount"], 139)
            self.assertEqual(order["total_amount"], total)
        self.assertEqual(coupon_stock()["reserved"], 18)
        with self.assertRaisesRegex(StoreError, "Only 0 coupon"):
            record_user(SimpleNamespace(id=400, username="last", first_name="Last"))
            create_or_get_order(400, 1)
        for invalid in (0, -1, 1.5, "2", True):
            with self.subTest(quantity=invalid), self.assertRaises(StoreError):
                create_or_get_order(400, invalid)

    def test_utr_validation_uniqueness_and_admin_review(self):
        add_coupon("TEST-COUPON-ONE")
        add_coupon("TEST-COUPON-TWO")
        first = create_or_get_order(101)
        for invalid in ("12345678901", "1234567890123", "12345x789012", "123 56789012"):
            with self.subTest(utr=invalid), self.assertRaisesRegex(StoreError, "exactly 12 digits"):
                submit_utr(first["id"], 101, invalid)
        submitted = submit_utr(first["id"], 101, "123456789012")
        self.assertEqual(submitted["utr"], "123456789012")
        with self.assertRaisesRegex(StoreError, "not awaiting a UTR"):
            submit_utr(first["id"], 101, "123456789012")

        second = create_or_get_order(202)
        with self.assertRaisesRegex(StoreError, "already been submitted"):
            submit_utr(second["id"], 202, "123456789012")
        approved = review_payment(first["id"], approved=True)
        self.assertEqual(approved["user_id"], 101)
        with self.assertRaisesRegex(StoreError, "no longer waiting"):
            review_payment(first["id"], approved=True)

    def test_reject_releases_coupon_and_delivery_is_one_shot(self):
        add_coupon("TEST-COUPON-ONE")
        order = create_or_get_order(101)
        submit_utr(order["id"], 101, "987654321098")
        review_payment(order["id"], approved=False)
        self.assertEqual(coupon_stock()["available"], 1)

        replacement = create_or_get_order(202)
        submit_utr(replacement["id"], 202, "987654321099")
        approval = review_payment(replacement["id"], approved=True)
        with connection() as conn:
            coupon_id = conn.execute(
                "SELECT id FROM coupons WHERE code = 'TEST-COUPON-ONE'"
            ).fetchone()["id"]
        self.assertEqual(len(approval["codes"]), 1)
        self.assertEqual(
            record_coupon_delivery(replacement["id"], coupon_id, succeeded=True),
            "DELIVERED",
        )
        with self.assertRaisesRegex(StoreError, "already recorded"):
            record_coupon_delivery(replacement["id"], coupon_id, succeeded=True)
        self.assertEqual(coupon_stock()["delivered"], 1)
        self.assertEqual(coupon_stock()["available"], 0)

    def test_multiple_codes_are_claimed_and_delivered_as_order_quantity(self):
        add_coupon("TEST-COUPON-ONE")
        add_coupon("TEST-COUPON-TWO")
        order = create_or_get_order(101, 2)
        self.assertEqual(order["total_amount"], 278)
        submit_utr(order["id"], 101, "222233334444")
        approval = review_payment(order["id"], approved=True)
        self.assertEqual(
            {coupon["code"] for coupon in approval["codes"]},
            {"TEST-COUPON-ONE", "TEST-COUPON-TWO"},
        )
        self.assertEqual(len(approval["codes"]), order["quantity"])
        self.assertEqual(approval["delivery_status"], "SENDING")
        for coupon in approval["codes"]:
            record_coupon_delivery(order["id"], coupon["id"], succeeded=True)
        with self.assertRaisesRegex(StoreError, "no longer waiting"):
            review_payment(order["id"], approved=True)
        self.assertEqual(coupon_stock()["delivered"], 2)

    def test_approval_is_refused_when_reserved_quantity_is_incomplete(self):
        add_coupon("RESERVED-ONE")
        add_coupon("RESERVED-TWO")
        order = create_or_get_order(101, 2)
        submit_utr(order["id"], 101, "333344445555")
        with connection() as conn:
            conn.execute(
                """UPDATE coupons SET status = 'AVAILABLE', reserved_order_id = NULL
                   WHERE code = 'RESERVED-TWO'"""
            )
        with self.assertRaisesRegex(StoreError, "exact number of unique coupons"):
            review_payment(order["id"], approved=True)
        with connection() as conn:
            status = conn.execute(
                "SELECT payment_status FROM orders WHERE id = ?", (order["id"],)
            ).fetchone()["payment_status"]
        self.assertEqual(status, "WAITING_FOR_ADMIN")

    def test_duplicate_coupon_codes_and_support_reply(self):
        add_coupon("TEST-COUPON-ONE")
        with self.assertRaisesRegex(StoreError, "already exists"):
            add_coupon("TEST-COUPON-ONE")
        ticket_id = create_support_message(101, "Please help with my order.")
        ticket = get_support_ticket(ticket_id)
        self.assertEqual(ticket["messages"][0]["message"], "Please help with my order.")
        saved_ticket = save_admin_reply(ticket_id, "We will help you.")
        self.assertEqual(saved_ticket["user_id"], 101)
        self.assertEqual(len(get_support_ticket(ticket_id)["messages"]), 2)

    def test_uncertain_delivery_is_never_retried_or_counted_as_delivered(self):
        add_coupon("TEST-COUPON-ONE")
        order = create_or_get_order(101)
        submit_utr(order["id"], 101, "111122223333")
        approval = review_payment(order["id"], approved=True)
        record_coupon_delivery(order["id"], approval["codes"][0]["id"], succeeded=False)
        stock = coupon_stock()
        self.assertEqual(stock["unknown"], 1)
        self.assertEqual(stock["delivered"], 0)
        with self.assertRaisesRegex(StoreError, "no longer waiting"):
            review_payment(order["id"], approved=True)

    def test_initial_inventory_seeds_only_41_unique_codes_idempotently(self):
        self.assertEqual(len(INITIAL_COUPON_CODES), 43)
        self.assertEqual(len(set(INITIAL_COUPON_CODES)), 41)
        initialize_database()
        with connection() as conn:
            conn.execute(
                "UPDATE coupons SET status = 'DELIVERED' WHERE code = ?",
                (INITIAL_COUPON_CODES[0],),
            )
        initialize_database()
        stock = coupon_stock()
        self.assertEqual(stock["available"], 40)
        self.assertEqual(stock["delivered"], 1)
        self.assertEqual(stock["total"], 41)

    def test_existing_one_coupon_database_gets_quantity_columns(self):
        config.DATABASE_PATH.unlink()
        legacy_conn = sqlite3.connect(config.DATABASE_PATH)
        try:
            conn = legacy_conn
            conn.executescript(
                """
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    is_blocked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE coupons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'AVAILABLE',
                    reserved_order_id TEXT,
                    added_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE TABLE orders (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    coupon_id INTEGER UNIQUE REFERENCES coupons(id),
                    amount INTEGER NOT NULL CHECK (amount = 139),
                    payment_status TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'NOT_DELIVERED',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                INSERT INTO users VALUES (101, 'buyer', 'Buyer', 'now', 'now', 0);
                INSERT INTO coupons VALUES
                    (1, 'LEGACY-CODE', 'RESERVED', 'SH-LEGACY', 'now', NULL);
                INSERT INTO orders VALUES
                    ('SH-LEGACY', 101, 1, 139, 'WAITING_FOR_UTR',
                     'NOT_DELIVERED', 'now', '2999-01-01');
                """
            )
            conn.commit()
        finally:
            legacy_conn.close()

        initialize_database()
        with connection() as conn:
            order = conn.execute(
                "SELECT quantity, amount, total_amount FROM orders WHERE id = 'SH-LEGACY'"
            ).fetchone()
            target = conn.execute(
                "SELECT value FROM settings WHERE key = 'initial_stock_target'"
            ).fetchone()
            coupon_count = conn.execute("SELECT COUNT(*) FROM coupons").fetchone()[0]
        self.assertEqual(tuple(order), (1, 139, 139))
        self.assertEqual(target["value"], "41")
        self.assertEqual(coupon_count, 42)


if __name__ == "__main__":
    unittest.main()
