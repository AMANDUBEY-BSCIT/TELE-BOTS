from datetime import datetime, timedelta, timezone
from secrets import token_hex

import config
from database import add_payment_event, transaction, utc_now


class OrderError(ValueError):
    pass


def _order_id() -> str:
    return f"CB-{datetime.now(timezone.utc):%Y%m%d}-{token_hex(3).upper()}"


def available_stock(conn, product_id: str) -> int:
    release_expired_orders(conn)
    row = conn.execute(
        """SELECT p.stock - COALESCE(SUM(CASE WHEN o.reserved = 1 THEN o.quantity ELSE 0 END), 0) AS available
           FROM products p LEFT JOIN orders o ON o.product_id = p.id
           WHERE p.id = ? GROUP BY p.id""",
        (product_id,),
    ).fetchone()
    return max(0, int(row["available"])) if row else 0


def release_expired_orders(conn) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """UPDATE orders SET payment_status = 'EXPIRED', reserved = 0
           WHERE reserved = 1 AND payment_status IN ('TERMS_PENDING', 'PAYMENT_PENDING')
             AND expires_at <= ?""",
        (now,),
    )


def current_available_stock(product_id: str) -> int:
    with transaction() as conn:
        return available_stock(conn, product_id)


def create_order(user_id: int, product_id: str, quantity: int) -> str:
    if quantity < 1:
        raise OrderError("Quantity must be at least one.")
    with transaction() as conn:
        release_expired_orders(conn)
        recent_order = conn.execute(
            """SELECT id FROM orders WHERE user_id = ? AND product_id = ? AND quantity = ?
               AND payment_status = 'TERMS_PENDING' AND reserved = 1
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, product_id, quantity),
        ).fetchone()
        if recent_order:
            return recent_order["id"]
        product = conn.execute(
            "SELECT * FROM products WHERE id = ? AND enabled = 1", (product_id,)
        ).fetchone()
        if product is None:
            raise OrderError("This product is not currently available.")
        stock = available_stock(conn, product_id)
        if quantity > stock:
            raise OrderError(f"Only {stock} code(s) are currently available.")
        now = datetime.now(timezone.utc)
        order_id = _order_id()
        conn.execute(
            """INSERT INTO orders
               (id, user_id, product_id, product_name, quantity, unit_price, total_amount,
                created_at, expires_at, payment_status, delivery_status, reserved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'TERMS_PENDING', 'NOT_DELIVERED', 1)""",
            (
                order_id,
                user_id,
                product_id,
                product["name"],
                quantity,
                product["price"],
                product["price"] * quantity,
                now.isoformat(timespec="seconds"),
                (now + timedelta(minutes=config.PAYMENT_EXPIRY_MINUTES)).isoformat(timespec="seconds"),
            ),
        )
        return order_id


def accept_terms(order_id: str, user_id: int):
    with transaction() as conn:
        release_expired_orders(conn)
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
        ).fetchone()
        if order is None:
            raise OrderError("Order not found.")
        if order["payment_status"] == "EXPIRED" or not order["reserved"]:
            raise OrderError("This order has expired. Please start a new order.")
        if order["payment_status"] == "TERMS_PENDING":
            expires = datetime.now(timezone.utc) + timedelta(minutes=config.PAYMENT_EXPIRY_MINUTES)
            conn.execute(
                "UPDATE orders SET payment_status = 'PAYMENT_PENDING', expires_at = ? WHERE id = ?",
                (expires.isoformat(timespec="seconds"), order_id),
            )
            add_payment_event(
                conn,
                order_id,
                user_id,
                "TERMS_ACCEPTED",
                {"amount": order["total_amount"]},
                utc_now(),
            )
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return order


def cancel_order(order_id: str, user_id: int) -> bool:
    with transaction() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
        ).fetchone()
        if order is None:
            raise OrderError("Order not found.")
        if order["payment_status"] not in ("TERMS_PENDING", "PAYMENT_PENDING"):
            return False
        conn.execute(
            """UPDATE orders SET payment_status = 'CANCELLED', reserved = 0
               WHERE id = ? AND payment_status IN ('TERMS_PENDING', 'PAYMENT_PENDING')""",
            (order_id,),
        )
        return True


def expire_order_if_needed(order_id: str, user_id: int):
    with transaction() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
        ).fetchone()
        if order is None:
            raise OrderError("Order not found.")
        if order["payment_status"] == "PAYMENT_PENDING" and order["expires_at"]:
            deadline = datetime.fromisoformat(order["expires_at"])
            if datetime.now(timezone.utc) >= deadline:
                conn.execute(
                    """UPDATE orders SET payment_status = 'EXPIRED', reserved = 0
                       WHERE id = ? AND payment_status = 'PAYMENT_PENDING'""",
                    (order_id,),
                )
                order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return order
