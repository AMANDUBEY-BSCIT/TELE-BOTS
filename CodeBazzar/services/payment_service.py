import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from database import add_payment_event, transaction, utc_now


class PaymentError(ValueError):
    pass


def submit_utr(order_id: str, user_id: int, utr: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{12}", utr):
        raise PaymentError("Enter a valid 12-digit numeric UTR.")
    expired = False
    with transaction() as conn:
        order = conn.execute(
            """SELECT o.*, u.first_name, u.username
               FROM orders o JOIN users u ON u.telegram_id = o.user_id
               WHERE o.id = ? AND o.user_id = ?""",
            (order_id, user_id),
        ).fetchone()
        if order is None:
            raise PaymentError("Order not found.")
        if conn.execute("SELECT 1 FROM payments WHERE utr = ?", (utr,)).fetchone():
            raise PaymentError("This UTR has already been submitted.")
        if order["payment_status"] != "PAYMENT_PENDING":
            raise PaymentError("This order is not awaiting payment.")
        if not order["expires_at"] or datetime.now(timezone.utc) >= datetime.fromisoformat(order["expires_at"]):
            conn.execute(
                "UPDATE orders SET payment_status = 'EXPIRED', reserved = 0 WHERE id = ?",
                (order_id,),
            )
            expired = True
        else:
            try:
                submitted_at = utc_now()
                conn.execute(
                    """INSERT INTO payments (order_id, utr, submitted_at, status)
                       VALUES (?, ?, ?, 'PENDING_VERIFICATION')""",
                    (order_id, utr, submitted_at),
                )
                add_payment_event(
                    conn,
                    order_id,
                    user_id,
                    "UTR_SUBMITTED",
                    {"utr": utr},
                    submitted_at,
                )
                conn.execute(
                    "UPDATE orders SET utr_submitted_at = ? WHERE id = ?",
                    (submitted_at, order_id),
                )
                conn.execute(
                    """UPDATE payments SET
                         qr_sent_at = (SELECT qr_sent_at FROM orders WHERE id = ?),
                         paid_clicked_at = (SELECT paid_clicked_at FROM orders WHERE id = ?)
                       WHERE order_id = ?""",
                    (order_id, order_id, order_id),
                )
            except sqlite3.IntegrityError as exc:
                if "payments.utr" in str(exc):
                    raise PaymentError("This UTR has already been submitted.") from exc
                raise
            conn.execute(
                """UPDATE orders SET payment_status = 'PENDING_VERIFICATION'
                   WHERE id = ? AND payment_status = 'PAYMENT_PENDING'""",
                (order_id,),
            )
            submitted_order = dict(order)
            submitted_order.update(
                utr=utr,
                submitted_at=submitted_at,
                payment_status="PENDING_VERIFICATION",
            )
    if expired:
        raise PaymentError("The payment session has expired. Please place a new order.")
    return submitted_order


_PENDING_VERIFICATION_STATUSES = {"PENDING_VERIFICATION", "UTR_SUBMITTED"}
_PENDING_PAYMENT_STATUSES = {"PENDING_VERIFICATION", "PENDING"}


def approve_payment(order_id: str) -> dict:
    with transaction() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        payment = conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
        if order is None or payment is None:
            raise PaymentError("Payment submission was not found.")
        if (
            order["payment_status"] not in _PENDING_VERIFICATION_STATUSES
            or payment["status"] not in _PENDING_PAYMENT_STATUSES
        ):
            raise PaymentError("Payment is no longer awaiting review.")
        reviewed_at = utc_now()
        conn.execute(
            "UPDATE payments SET status = 'APPROVED', reviewed_at = ? WHERE order_id = ?",
            (reviewed_at, order_id),
        )
        conn.execute("UPDATE orders SET payment_status = 'APPROVED' WHERE id = ?", (order_id,))
        add_payment_event(
            conn,
            order_id,
            order["user_id"],
            "PAYMENT_APPROVED",
            {"amount": order["total_amount"]},
            reviewed_at,
        )
        return dict(order)


def reject_payment(order_id: str) -> dict:
    with transaction() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        payment = conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
        if order is None or payment is None:
            raise PaymentError("Payment submission was not found.")
        if (
            order["payment_status"] not in _PENDING_VERIFICATION_STATUSES
            or payment["status"] not in _PENDING_PAYMENT_STATUSES
        ):
            raise PaymentError("Payment is no longer awaiting review.")
        reviewed_at = utc_now()
        conn.execute(
            "UPDATE payments SET status = 'REJECTED', reviewed_at = ? WHERE order_id = ?",
            (reviewed_at, order_id),
        )
        conn.execute(
            "UPDATE orders SET payment_status = 'REJECTED', reserved = 0 WHERE id = ?",
            (order_id,),
        )
        add_payment_event(
            conn,
            order_id,
            order["user_id"],
            "PAYMENT_REJECTED",
            {"amount": order["total_amount"]},
            reviewed_at,
        )
        return dict(order)
