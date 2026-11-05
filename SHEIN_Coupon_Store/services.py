import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from database import transaction, utc_now


class StoreError(ValueError):
    pass


def _new_order_id() -> str:
    return f"SH-{datetime.now(timezone.utc):%y%m%d}-{secrets.token_hex(3).upper()}"


def _release_expired(conn: sqlite3.Connection) -> None:
    now = utc_now()
    rows = conn.execute(
        """SELECT id FROM orders
           WHERE payment_status = 'WAITING_FOR_UTR' AND expires_at <= ?""",
        (now,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """UPDATE coupons SET status = 'AVAILABLE', reserved_order_id = NULL
               WHERE status = 'RESERVED' AND reserved_order_id = ?""",
            (row["id"],),
        )
        conn.execute(
            "UPDATE orders SET payment_status = 'EXPIRED', coupon_id = NULL WHERE id = ?",
            (row["id"],),
        )


def create_or_get_order(
    user_id: int, quantity: int = 1, purchase_request_id: str | None = None
) -> dict:
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise StoreError("Coupon quantity must be a positive whole number.")
    with transaction() as conn:
        _release_expired(conn)
        if purchase_request_id is not None:
            existing = conn.execute(
                "SELECT * FROM orders WHERE purchase_request_id = ?",
                (purchase_request_id,),
            ).fetchone()
            if existing is not None:
                if existing["user_id"] != user_id or existing["quantity"] != quantity:
                    raise StoreError("This purchase confirmation has already been used.")
                return dict(existing)
        available = int(
            conn.execute(
                "SELECT COUNT(*) FROM coupons WHERE status = 'AVAILABLE'"
            ).fetchone()[0]
        )
        if quantity > available:
            raise StoreError(
                f"Only {available} coupon(s) are available; you selected {quantity}."
            )
        coupons = conn.execute(
            """SELECT id FROM coupons WHERE status = 'AVAILABLE'
               ORDER BY id LIMIT ?""",
            (quantity,),
        ).fetchall()
        if len(coupons) < quantity:
            raise StoreError("Coupon stock changed before the order could be reserved.")
        now = datetime.now(timezone.utc)
        for _ in range(5):
            order_id = _new_order_id()
            exists = conn.execute(
                "SELECT 1 FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if exists is None:
                break
        else:
            raise StoreError("Could not generate a unique order ID. Please try again.")
        expires_at = (now + timedelta(minutes=config.PAYMENT_EXPIRY_MINUTES)).isoformat(
            timespec="seconds"
        )
        conn.execute(
            """INSERT INTO orders
               (id, user_id, amount, quantity, total_amount, purchase_request_id,
                payment_status, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, 'WAITING_FOR_UTR', ?, ?)""",
            (
                order_id,
                user_id,
                config.PRICE_INR,
                quantity,
                config.PRICE_INR * quantity,
                purchase_request_id,
                now.isoformat(timespec="seconds"),
                expires_at,
            ),
        )
        conn.executemany(
            """UPDATE coupons SET status = 'RESERVED', reserved_order_id = ?
               WHERE id = ? AND status = 'AVAILABLE'""",
            [(order_id, coupon["id"]) for coupon in coupons],
        )
        return dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def claim_payment_screen(order_id: str, user_id: int) -> dict | None:
    with transaction() as conn:
        _release_expired(conn)
        order = conn.execute(
            """SELECT o.*, u.username, u.first_name FROM orders o
               JOIN users u ON u.telegram_id = o.user_id
               WHERE o.id = ? AND o.user_id = ?""",
            (order_id, user_id),
        ).fetchone()
        if order is None or order["payment_status"] != "WAITING_FOR_UTR":
            raise StoreError("This order is not awaiting payment.")
        changed = conn.execute(
            """UPDATE orders SET payment_screen_status = 'SENDING'
               WHERE id = ? AND payment_status = 'WAITING_FOR_UTR'
                 AND payment_screen_status = 'NOT_SENT'""",
            (order_id,),
        ).rowcount
        if changed != 1:
            return None
        return dict(order)


def finish_payment_screen(order_id: str, sent: bool) -> None:
    status = "SENT" if sent else "FAILED"
    with transaction() as conn:
        changed = conn.execute(
            """UPDATE orders SET payment_screen_status = ?, qr_sent_at = ?
               WHERE id = ? AND payment_screen_status = 'SENDING'""",
            (status, utc_now() if sent else None, order_id),
        ).rowcount
        if changed != 1:
            raise StoreError("Payment screen status changed unexpectedly.")


def get_order_for_user(order_id: str, user_id: int) -> dict | None:
    with transaction() as conn:
        _release_expired(conn)
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def get_waiting_utr_order(user_id: int) -> dict | None:
    orders = get_waiting_utr_orders(user_id, limit=2)
    return orders[0] if len(orders) == 1 else None


def get_waiting_utr_orders(user_id: int, limit: int = 100) -> list[dict]:
    with transaction() as conn:
        _release_expired(conn)
        rows = conn.execute(
            """SELECT * FROM orders
               WHERE user_id = ? AND payment_status = 'WAITING_FOR_UTR'
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def submit_utr(order_id: str, user_id: int, utr: str) -> dict:
    if not re.fullmatch(r"[0-9]{12}", utr):
        raise StoreError("Invalid UTR. Enter exactly 12 digits with no spaces or other characters.")
    with transaction() as conn:
        _release_expired(conn)
        order = conn.execute(
            """SELECT o.*, u.username, u.first_name FROM orders o
               JOIN users u ON u.telegram_id = o.user_id
               WHERE o.id = ? AND o.user_id = ?""",
            (order_id, user_id),
        ).fetchone()
        if order is None:
            raise StoreError("Order not found.")
        if order["payment_status"] != "WAITING_FOR_UTR":
            raise StoreError("This order is not awaiting a UTR. Duplicate submissions are not accepted.")
        try:
            submitted_at = utc_now()
            conn.execute(
                """INSERT INTO payments (order_id, utr, status, submitted_at)
                   VALUES (?, ?, 'WAITING_FOR_ADMIN', ?)""",
                (order_id, utr, submitted_at),
            )
        except sqlite3.IntegrityError as exc:
            if "payments.utr" in str(exc):
                raise StoreError("This UTR has already been submitted.") from exc
            raise
        conn.execute(
            "UPDATE orders SET payment_status = 'WAITING_FOR_ADMIN' WHERE id = ?",
            (order_id,),
        )
        return {**dict(order), "utr": utr, "submitted_at": submitted_at}


def review_payment(order_id: str, approved: bool) -> dict:
    with transaction() as conn:
        row = conn.execute(
            """SELECT o.*, p.utr, u.username, u.first_name
               FROM orders o JOIN payments p ON p.order_id = o.id
               JOIN users u ON u.telegram_id = o.user_id
               WHERE o.id = ?""",
            (order_id,),
        ).fetchone()
        if row is None or row["payment_status"] != "WAITING_FOR_ADMIN":
            raise StoreError("Payment is no longer waiting for admin confirmation.")
        if approved:
            coupons = conn.execute(
                """SELECT id, code FROM coupons
                   WHERE reserved_order_id = ? AND status = 'RESERVED'
                   ORDER BY id""",
                (order_id,),
            ).fetchall()
            codes = [coupon["code"] for coupon in coupons]
            if (
                len(coupons) != row["quantity"]
                or len(codes) != len(set(codes))
            ):
                raise StoreError(
                    "The exact number of unique coupons is not reserved for this order."
                )
            changed = conn.execute(
                """UPDATE coupons SET status = 'SENDING'
                   WHERE reserved_order_id = ? AND status = 'RESERVED'""",
                (order_id,),
            ).rowcount
            if changed != row["quantity"]:
                raise StoreError("Coupon reservations changed; payment was not approved.")
        status = "APPROVED" if approved else "REJECTED"
        conn.execute(
            "UPDATE payments SET status = ?, reviewed_at = ? WHERE order_id = ?",
            (status, utc_now(), order_id),
        )
        conn.execute(
            """UPDATE orders SET payment_status = ?, delivery_status = ?
               WHERE id = ?""",
            (status, "SENDING" if approved else row["delivery_status"], order_id),
        )
        if not approved:
            conn.execute(
                """UPDATE coupons SET status = 'AVAILABLE', reserved_order_id = NULL
                   WHERE status = 'RESERVED' AND reserved_order_id = ?""",
                (order_id,),
            )
            conn.execute(
                "UPDATE orders SET coupon_id = NULL WHERE id = ?", (order_id,)
            )
        return {
            **dict(row),
            "payment_status": status,
            "delivery_status": "SENDING" if approved else row["delivery_status"],
            "codes": (
                [
                    {"id": coupon["id"], "code": coupon["code"]}
                    for coupon in coupons
                ]
                if approved
                else []
            ),
        }


def record_coupon_delivery(
    order_id: str, coupon_id: int, succeeded: bool
) -> str:
    coupon_status = "DELIVERED" if succeeded else "DELIVERY_UNKNOWN"
    with transaction() as conn:
        order = conn.execute(
            """SELECT quantity, delivery_status FROM orders WHERE id = ?""",
            (order_id,),
        ).fetchone()
        if order is None:
            raise StoreError("Order not found.")
        if order["delivery_status"] != "SENDING":
            recorded = conn.execute(
                """SELECT 1 FROM coupons
                   WHERE id = ? AND reserved_order_id = ?
                     AND status IN ('DELIVERED', 'DELIVERY_UNKNOWN')""",
                (coupon_id, order_id),
            ).fetchone()
            if recorded:
                raise StoreError("Coupon delivery was already recorded or is locked.")
            raise StoreError("This order is not currently being delivered.")
        changed = conn.execute(
            """UPDATE coupons SET status = ?, delivered_at = ?
               WHERE id = ? AND reserved_order_id = ? AND status = 'SENDING'""",
            (coupon_status, utc_now() if succeeded else None, coupon_id, order_id),
        ).rowcount
        if changed != 1:
            raise StoreError("Coupon delivery was already recorded or coupon does not belong to this order.")
        if succeeded:
            remaining = int(
                conn.execute(
                    """SELECT COUNT(*) FROM coupons
                       WHERE reserved_order_id = ? AND status != 'DELIVERED'""",
                    (order_id,),
                ).fetchone()[0]
            )
            if remaining == 0:
                conn.execute(
                    """UPDATE orders SET delivery_status = 'DELIVERED'
                       WHERE id = ? AND delivery_status = 'SENDING'""",
                    (order_id,),
                )
                return "DELIVERED"
            return "SENDING"
        conn.execute(
            """UPDATE coupons SET status = 'DELIVERY_UNKNOWN'
               WHERE reserved_order_id = ? AND status = 'SENDING'""",
            (order_id,),
        )
        conn.execute(
            """UPDATE orders SET delivery_status = 'DELIVERY_UNKNOWN'
               WHERE id = ? AND delivery_status = 'SENDING'""",
            (order_id,),
        )
        return "DELIVERY_UNKNOWN"


def claim_coupon_delivery(order_id: str) -> dict:
    """Compatibility helper for a previously approved order awaiting a send."""
    with transaction() as conn:
        row = conn.execute(
            """SELECT o.*, u.telegram_id, u.username, u.first_name
               FROM orders o JOIN users u ON u.telegram_id = o.user_id
               WHERE o.id = ?""",
            (order_id,),
        ).fetchone()
        if row is None or row["payment_status"] != "APPROVED":
            raise StoreError("Only an approved order can be delivered.")
        if row["delivery_status"] != "NOT_DELIVERED":
            raise StoreError("Coupon delivery was already started and cannot be repeated.")
        coupons = conn.execute(
            """SELECT id, code FROM coupons
               WHERE status = 'RESERVED' AND reserved_order_id = ?
               ORDER BY id""",
            (order_id,),
        ).fetchall()
        if len(coupons) != row["quantity"]:
            raise StoreError(
                "The reserved coupon count does not match the order quantity; no codes were sent."
            )
        changed = conn.execute(
            """UPDATE coupons SET status = 'SENDING'
               WHERE status = 'RESERVED' AND reserved_order_id = ?""",
            (order_id,),
        ).rowcount
        if changed != row["quantity"]:
            raise StoreError("The reserved coupons are unavailable; no codes were sent.")
        codes = conn.execute(
            "SELECT id, code FROM coupons WHERE reserved_order_id = ? AND status = 'SENDING' ORDER BY id",
            (order_id,),
        ).fetchall()
        changed = conn.execute(
            """UPDATE orders SET delivery_status = 'SENDING'
               WHERE id = ? AND delivery_status = 'NOT_DELIVERED'""",
            (order_id,),
        ).rowcount
        if changed != 1:
            raise StoreError("Delivery status changed unexpectedly; no codes were sent.")
        return {**dict(row), "codes": [dict(coupon) for coupon in codes]}


def add_coupon(code: str) -> None:
    value = code.strip()
    if not value or len(value) > 500:
        raise StoreError("Coupon code must contain 1 to 500 characters.")
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO coupons (code, status, added_at) VALUES (?, 'AVAILABLE', ?)",
                (value, utc_now()),
            )
    except sqlite3.IntegrityError as exc:
        if "coupons.code" in str(exc):
            raise StoreError("That coupon code already exists; duplicates are not accepted.") from exc
        raise


def coupon_stock() -> dict[str, int]:
    with transaction() as conn:
        _release_expired(conn)
        rows = conn.execute(
            "SELECT status, COUNT(*) AS quantity FROM coupons GROUP BY status"
        ).fetchall()
    counts = {row["status"]: int(row["quantity"]) for row in rows}
    return {
        "available": counts.get("AVAILABLE", 0),
        "reserved": counts.get("RESERVED", 0),
        "sending": counts.get("SENDING", 0),
        "delivered": counts.get("DELIVERED", 0),
        "unknown": counts.get("DELIVERY_UNKNOWN", 0),
        "total": sum(counts.values()),
    }


def list_coupons() -> list[dict]:
    with transaction() as conn:
        _release_expired(conn)
        rows = conn.execute(
            """SELECT c.code, c.status, c.reserved_order_id, c.added_at,
                      c.delivered_at, o.user_id
               FROM coupons c LEFT JOIN orders o ON o.id = c.reserved_order_id
               ORDER BY c.id DESC LIMIT 10000"""
        ).fetchall()
    return [dict(row) for row in rows]


def list_orders(status: str | None = None, limit: int = 50) -> list[dict]:
    with transaction() as conn:
        _release_expired(conn)
        if status:
            rows = conn.execute(
                """SELECT o.*, u.username, u.first_name, p.utr
                   FROM orders o JOIN users u ON u.telegram_id = o.user_id
                   LEFT JOIN payments p ON p.order_id = o.id
                   WHERE o.payment_status = ? ORDER BY o.created_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT o.*, u.username, u.first_name, p.utr
                   FROM orders o JOIN users u ON u.telegram_id = o.user_id
                   LEFT JOIN payments p ON p.order_id = o.id
                   ORDER BY o.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def get_order(order_id: str) -> dict | None:
    with transaction() as conn:
        _release_expired(conn)
        row = conn.execute(
            """SELECT o.*, u.username, u.first_name, p.utr
               FROM orders o JOIN users u ON u.telegram_id = o.user_id
               LEFT JOIN payments p ON p.order_id = o.id
               WHERE o.id = ?""",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        codes = conn.execute(
            """SELECT code FROM coupons WHERE reserved_order_id = ?
               ORDER BY id""",
            (order_id,),
        ).fetchall()
    return {**dict(row), "coupon_codes": [coupon["code"] for coupon in codes]}


def list_user_orders(user_id: int, limit: int = 10000) -> list[dict]:
    with transaction() as conn:
        _release_expired(conn)
        rows = conn.execute(
            """SELECT o.*, p.utr FROM orders o
               LEFT JOIN payments p ON p.order_id = o.id
               WHERE o.user_id = ? ORDER BY o.created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def list_users(limit: int = 50) -> list[dict]:
    with transaction() as conn:
        rows = conn.execute(
            """SELECT u.telegram_id, u.username, u.first_name, u.is_blocked,
                      COUNT(o.id) AS order_count
               FROM users u LEFT JOIN orders o ON o.user_id = u.telegram_id
               GROUP BY u.telegram_id ORDER BY u.last_active_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_support_message(user_id: int, text: str) -> int:
    clean = text.strip()
    if not clean or len(clean) > 4000:
        raise StoreError("Support messages must contain 1 to 4,000 characters.")
    with transaction() as conn:
        ticket = conn.execute(
            "SELECT id FROM support_tickets WHERE user_id = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        now = utc_now()
        if ticket is None:
            cursor = conn.execute(
                """INSERT INTO support_tickets (user_id, status, created_at, updated_at)
                   VALUES (?, 'OPEN', ?, ?)""",
                (user_id, now, now),
            )
            ticket_id = int(cursor.lastrowid)
        else:
            ticket_id = int(ticket["id"])
        conn.execute(
            """INSERT INTO support_messages
               (ticket_id, sender_id, sender_type, message, created_at)
               VALUES (?, ?, 'CUSTOMER', ?, ?)""",
            (ticket_id, user_id, clean, now),
        )
        conn.execute(
            "UPDATE support_tickets SET updated_at = ? WHERE id = ?", (now, ticket_id)
        )
        return ticket_id


def list_support_tickets(limit: int = 50) -> list[dict]:
    with transaction() as conn:
        rows = conn.execute(
            """SELECT t.id, t.user_id, t.status, t.updated_at,
                      u.username, u.first_name,
                      (SELECT message FROM support_messages m
                       WHERE m.ticket_id = t.id ORDER BY m.id DESC LIMIT 1) AS last_message
               FROM support_tickets t JOIN users u ON u.telegram_id = t.user_id
               WHERE t.status = 'OPEN' ORDER BY t.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_support_ticket(ticket_id: int) -> dict | None:
    with transaction() as conn:
        ticket = conn.execute(
            """SELECT t.*, u.username, u.first_name
               FROM support_tickets t JOIN users u ON u.telegram_id = t.user_id
               WHERE t.id = ?""",
            (ticket_id,),
        ).fetchone()
        if ticket is None:
            return None
        messages = conn.execute(
            "SELECT * FROM support_messages WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
    return {**dict(ticket), "messages": [dict(row) for row in messages]}


def save_admin_reply(ticket_id: int, message: str) -> dict:
    clean = message.strip()
    if not clean or len(clean) > 4000:
        raise StoreError("Replies must contain 1 to 4,000 characters.")
    with transaction() as conn:
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE id = ? AND status = 'OPEN'",
            (ticket_id,),
        ).fetchone()
        if ticket is None:
            raise StoreError("That support ticket is not open.")
        now = utc_now()
        conn.execute(
            """INSERT INTO support_messages
               (ticket_id, sender_id, sender_type, message, created_at)
               VALUES (?, ?, 'ADMIN', ?, ?)""",
            (ticket_id, config.ADMIN_ID, clean, now),
        )
        conn.execute(
            "UPDATE support_tickets SET updated_at = ? WHERE id = ?", (now, ticket_id)
        )
        return dict(ticket)
