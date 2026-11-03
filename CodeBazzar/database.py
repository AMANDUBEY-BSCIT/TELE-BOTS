import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import config

logger = logging.getLogger(__name__)

INITIAL_PRODUCTS = (
    ("gemini_pro", "Gemini Pro 18 Months", 119, 45),
    ("pvr_movie", "PVR Movie ₹200 Off Voucher (2 Tickets)", 35, 135),
    ("pvr_giftcard", "PVR ₹200 GiftCard", 109, 15),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def initialize_database() -> None:
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_active_at TEXT,
                is_blocked INTEGER NOT NULL DEFAULT 0 CHECK (is_blocked IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price >= 0),
                stock INTEGER NOT NULL CHECK (stock >= 0),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                product_id TEXT NOT NULL REFERENCES products(id),
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
                total_amount INTEGER NOT NULL CHECK (total_amount >= 0),
                created_at TEXT NOT NULL,
                expires_at TEXT,
                payment_screen_at TEXT,
                qr_sent_at TEXT,
                paid_clicked_at TEXT,
                utr_submitted_at TEXT,
                payment_status TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'NOT_DELIVERED',
                reserved INTEGER NOT NULL DEFAULT 1 CHECK (reserved IN (0, 1))
            );
            CREATE INDEX IF NOT EXISTS idx_orders_user_created
                ON orders(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_orders_payment_status
                ON orders(payment_status, created_at DESC);
            CREATE TABLE IF NOT EXISTS payments (
                order_id TEXT PRIMARY KEY REFERENCES orders(id),
                utr TEXT NOT NULL UNIQUE,
                submitted_at TEXT NOT NULL,
                status TEXT NOT NULL,
                qr_sent_at TEXT,
                paid_clicked_at TEXT,
                reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_activity_customer_time
                ON user_activity(telegram_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_user_activity_type_time
                ON user_activity(event_type, created_at DESC);
            CREATE TABLE IF NOT EXISTS payment_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL REFERENCES orders(id),
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_payment_events_order_time
                ON payment_events(order_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_payment_events_type_time
                ON payment_events(event_type, created_at DESC);
            CREATE TABLE IF NOT EXISTS voucher_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
                codes_json TEXT NOT NULL,
                delivered_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voucher_codes (
                code TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(id),
                delivered_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS support_tickets (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                category TEXT NOT NULL,
                order_id TEXT REFERENCES orders(id),
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_status_created
                ON support_tickets(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL REFERENCES support_tickets(id),
                sender_id INTEGER NOT NULL,
                sender_type TEXT NOT NULL CHECK (sender_type IN ('CUSTOMER', 'ADMIN')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                telegram_message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS customer_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                sender_type TEXT NOT NULL CHECK (sender_type IN ('CUSTOMER', 'ADMIN')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_customer_messages_customer_time
                ON customer_messages(telegram_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_customer_messages_inbox
                ON customer_messages(sender_type, created_at DESC);
            CREATE TABLE IF NOT EXISTS admins (
                telegram_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        _ensure_column(conn, "users", "last_active_at", "TEXT")
        _ensure_column(
            conn,
            "users",
            "is_blocked",
            "INTEGER NOT NULL DEFAULT 0 CHECK (is_blocked IN (0, 1))",
        )
        _ensure_column(conn, "payments", "qr_sent_at", "TEXT")
        _ensure_column(conn, "payments", "paid_clicked_at", "TEXT")
        _ensure_column(conn, "payments", "reviewed_at", "TEXT")
        _ensure_column(conn, "orders", "payment_screen_at", "TEXT")
        _ensure_column(conn, "orders", "qr_sent_at", "TEXT")
        _ensure_column(conn, "orders", "paid_clicked_at", "TEXT")
        _ensure_column(conn, "orders", "utr_submitted_at", "TEXT")
        _ensure_column(conn, "support_messages", "telegram_message_id", "INTEGER")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_support_messages_source
               ON support_messages(sender_id, telegram_message_id)
               WHERE telegram_message_id IS NOT NULL"""
        )
        for product in INITIAL_PRODUCTS:
            conn.execute(
                """INSERT OR IGNORE INTO products (id, name, price, stock, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                product,
            )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('brand_name', 'CodeBazar')"
        )
        if config.ADMIN_ID is not None:
            conn.execute(
                "INSERT OR IGNORE INTO admins (telegram_id, created_at) VALUES (?, ?)",
                (config.ADMIN_ID, utc_now()),
            )
    logger.info("Database initialized at %s", config.DATABASE_PATH)


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def record_user(user) -> None:
    now = utc_now()
    with connection() as conn:
        conn.execute(
            """INSERT INTO users (telegram_id, username, first_name, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 username = excluded.username, first_name = excluded.first_name,
                 last_active_at = excluded.created_at""",
            (user.id, user.username, user.first_name or "Customer", now),
        )
        conn.execute(
            "UPDATE users SET last_active_at = ? WHERE telegram_id = ?",
            (now, user.id),
        )


def add_activity(
    conn: sqlite3.Connection,
    telegram_id: int,
    event_type: str,
    details: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO user_activity (telegram_id, event_type, details_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (
            telegram_id,
            event_type,
            json.dumps(details or {}, ensure_ascii=False),
            created_at or utc_now(),
        ),
    )


def record_activity(
    telegram_id: int,
    event_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    with connection() as conn:
        add_activity(conn, telegram_id, event_type, details)


def add_payment_event(
    conn: sqlite3.Connection,
    order_id: str,
    telegram_id: int,
    event_type: str,
    details: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> None:
    timestamp = created_at or utc_now()
    conn.execute(
        """INSERT INTO payment_events
           (order_id, telegram_id, event_type, details_json, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            order_id,
            telegram_id,
            event_type,
            json.dumps(details or {}, ensure_ascii=False),
            timestamp,
        ),
    )
    add_activity(
        conn,
        telegram_id,
        event_type,
        {"order_id": order_id, **(details or {})},
        timestamp,
    )


def record_order_payment_event(
    order_id: str,
    event_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    with connection() as conn:
        order = conn.execute(
            "SELECT user_id FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            raise ValueError(f"Order {order_id} was not found.")
        timestamp = utc_now()
        timestamp_column = {
            "PAYMENT_SCREEN_REACHED": "payment_screen_at",
            "QR_SENT": "qr_sent_at",
            "PAYMENT_BUTTON_CLICKED": "paid_clicked_at",
            "UTR_SUBMITTED": "utr_submitted_at",
        }.get(event_type)
        if timestamp_column is not None:
            conn.execute(
                f"UPDATE orders SET {timestamp_column} = ? WHERE id = ?",
                (timestamp, order_id),
            )
        if event_type in {"QR_SENT", "PAYMENT_BUTTON_CLICKED"}:
            payment_column = {
                "QR_SENT": "qr_sent_at",
                "PAYMENT_BUTTON_CLICKED": "paid_clicked_at",
            }[event_type]
            conn.execute(
                f"UPDATE payments SET {payment_column} = ? WHERE order_id = ?",
                (timestamp, order_id),
            )
        add_payment_event(
            conn, order_id, order["user_id"], event_type, details, timestamp
        )


def set_user_blocked(telegram_id: int, blocked: bool) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE users SET is_blocked = ? WHERE telegram_id = ?",
            (int(blocked), telegram_id),
        )


def is_user_blocked(telegram_id: int) -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT is_blocked FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return bool(row and row["is_blocked"])


def get_product(product_id: str):
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()


def get_all_products(enabled_only: bool = False):
    query = "SELECT * FROM products"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY CASE id WHEN 'gemini_pro' THEN 1 WHEN 'pvr_movie' THEN 2 ELSE 3 END"
    with connection() as conn:
        return conn.execute(query).fetchall()


def get_setting(key: str):
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )


def decode_codes(codes_json: str) -> list[str]:
    value = json.loads(codes_json)
    if not isinstance(value, list) or not all(isinstance(code, str) for code in value):
        raise ValueError("Invalid voucher data stored for this order.")
    return value
