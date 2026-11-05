import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import config
from initial_inventory import INITIAL_COUPON_CODES


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
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                is_blocked INTEGER NOT NULL DEFAULT 0 CHECK (is_blocked IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                coupon_id INTEGER UNIQUE REFERENCES coupons(id),
                amount INTEGER NOT NULL CHECK (amount = 139),
                quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
                total_amount INTEGER NOT NULL DEFAULT 139 CHECK (total_amount > 0),
                purchase_request_id TEXT,
                payment_screen_status TEXT NOT NULL DEFAULT 'NOT_SENT',
                qr_sent_at TEXT,
                payment_status TEXT NOT NULL CHECK (
                    payment_status IN (
                        'WAITING_FOR_UTR', 'WAITING_FOR_ADMIN', 'APPROVED',
                        'REJECTED', 'EXPIRED'
                    )
                ),
                delivery_status TEXT NOT NULL DEFAULT 'NOT_DELIVERED' CHECK (
                    delivery_status IN (
                        'NOT_DELIVERED', 'SENDING', 'DELIVERED', 'DELIVERY_UNKNOWN'
                    )
                ),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coupons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK (
                    status IN (
                        'AVAILABLE', 'RESERVED', 'SENDING', 'DELIVERED',
                        'DELIVERY_UNKNOWN'
                    )
                ),
                reserved_order_id TEXT,
                added_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payments (
                order_id TEXT PRIMARY KEY REFERENCES orders(id),
                utr TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (
                    status IN ('WAITING_FOR_ADMIN', 'APPROVED', 'REJECTED')
                ),
                submitted_at TEXT NOT NULL,
                reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES support_tickets(id),
                sender_id INTEGER NOT NULL,
                sender_type TEXT NOT NULL CHECK (sender_type IN ('CUSTOMER', 'ADMIN')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orders_user_created
                ON orders(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_orders_status_created
                ON orders(payment_status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_coupons_status
                ON coupons(status, id);
            CREATE INDEX IF NOT EXISTS idx_tickets_status_updated
                ON support_tickets(status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        _ensure_column(
            conn,
            "orders",
            "quantity",
            "INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0)",
        )
        _ensure_column(
            conn,
            "orders",
            "total_amount",
            "INTEGER NOT NULL DEFAULT 139 CHECK (total_amount > 0)",
        )
        _ensure_column(conn, "orders", "purchase_request_id", "TEXT")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_purchase_request
               ON orders(purchase_request_id)
               WHERE purchase_request_id IS NOT NULL"""
        )
        _ensure_column(
            conn,
            "orders",
            "payment_screen_status",
            "TEXT NOT NULL DEFAULT 'NOT_SENT'",
        )
        _ensure_column(conn, "orders", "qr_sent_at", "TEXT")
        conn.execute(
            """INSERT INTO settings (key, value) VALUES ('initial_stock_target', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (str(config.INITIAL_STOCK_TARGET),),
        )
        conn.executemany(
            """INSERT OR IGNORE INTO coupons (code, status, added_at)
               VALUES (?, 'AVAILABLE', ?)""",
            [(code, utc_now()) for code in dict.fromkeys(INITIAL_COUPON_CODES)],
        )


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
            """INSERT INTO users (telegram_id, username, first_name, created_at, last_active_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   username = excluded.username,
                   first_name = excluded.first_name,
                   last_active_at = excluded.last_active_at""",
            (user.id, user.username, user.first_name or "Customer", now, now),
        )


def is_user_blocked(user_id: int) -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT is_blocked FROM users WHERE telegram_id = ?", (user_id,)
        ).fetchone()
    return bool(row and row["is_blocked"])


def set_user_blocked(user_id: int, blocked: bool) -> bool:
    with transaction() as conn:
        return conn.execute(
            "UPDATE users SET is_blocked = ? WHERE telegram_id = ?",
            (int(blocked), user_id),
        ).rowcount == 1
