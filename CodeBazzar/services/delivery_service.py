import json
import sqlite3

from database import add_payment_event, transaction, utc_now


class DeliveryError(ValueError):
    pass


def voucher_code_messages(
    order_id: str,
    codes: list[str],
    prefix: str = "Your Voucher Codes:\n\n",
    suffix: str = "",
) -> list[str]:
    max_length = 3900
    messages = []
    current = prefix
    for code in codes:
        line = f"{code}\n"
        if len(line) > max_length:
            raise DeliveryError("A voucher code is too long to send through Telegram.")
        if len(current) + len(line) > max_length:
            messages.append(current.rstrip())
            current = f"More voucher codes for order {order_id}:\n\n"
        current += line
    if suffix and len(current) + len(suffix) > max_length:
        messages.append(current.rstrip())
        current = suffix.strip()
    else:
        current += suffix
    if current.strip():
        messages.append(current.rstrip())
    return messages


def deliver_codes(order_id: str, codes: list[str]) -> dict:
    normalized = [code.strip() for code in codes if code.strip()]
    if len(normalized) != len(set(normalized)):
        raise DeliveryError("Each voucher code must be unique.")
    if any(len(code) > 1000 for code in normalized):
        raise DeliveryError("Each voucher code must be 1000 characters or fewer.")
    with transaction() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise DeliveryError("Order not found.")
        if order["payment_status"] != "APPROVED" or order["delivery_status"] == "DELIVERED":
            raise DeliveryError("This order is not eligible for delivery.")
        if len(normalized) != order["quantity"]:
            raise DeliveryError(f"Enter exactly {order['quantity']} voucher code(s).")
        now = utc_now()
        try:
            conn.executemany(
                "INSERT INTO voucher_codes (code, order_id, delivered_at) VALUES (?, ?, ?)",
                [(code, order_id, now) for code in normalized],
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: voucher_codes.code" in str(exc):
                raise DeliveryError("One or more codes were already delivered on another order.") from exc
            raise
        conn.execute(
            "INSERT INTO voucher_deliveries (order_id, codes_json, delivered_at) VALUES (?, ?, ?)",
            (order_id, json.dumps(normalized, ensure_ascii=False), now),
        )
        changed = conn.execute(
            """UPDATE products SET stock = stock - ?
               WHERE id = ? AND stock >= ?""",
            (order["quantity"], order["product_id"], order["quantity"]),
        ).rowcount
        if changed != 1:
            raise DeliveryError("Stock changed unexpectedly; delivery was not recorded.")
        conn.execute(
            """UPDATE orders SET delivery_status = 'DELIVERED', reserved = 0
               WHERE id = ? AND payment_status = 'APPROVED'
                 AND delivery_status != 'DELIVERED'""",
            (order_id,),
        )
        add_payment_event(
            conn,
            order_id,
            order["user_id"],
            "VOUCHER_DELIVERY_RECORDED",
            {"quantity": order["quantity"]},
            now,
        )
        return {**dict(order), "codes": normalized}
