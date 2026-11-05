# SHEIN Coupon Store Telegram Bot

Independent Telegram coupon-store bot using Python, `python-telegram-bot` 21.x,
and SQLite. It is designed to run on Termux or a regular Python environment.

## What is included

- One SHEIN coupon product: ₹139; up to ₹800 discount; ₹1,000 minimum cart.
- Quantity choices of 1, 2, 5, 10, or a custom positive whole number. Totals are
  quantity × ₹139, and stock is reserved transactionally before payment.
- Manual UPI QR payment and admin-only UTR approval/rejection. Submitting a UTR
  never marks a payment as paid automatically.
- Exactly 12 ASCII digits are accepted as a UTR, and each UTR is unique.
- Admin-managed coupon codes, reservation and delivery tracking. A coupon is
  never automatically delivered before payment approval.
- Customer order history, block controls, support tickets and admin replies.
- SQLite persistence at `instance/shein.db`.

The configured QR image is copied from the existing CodeBazar project to
`assets/payment_qr.png`. The admin ID in `.env.example` is the same configured
admin ID as CodeBazar. This project does not copy or reuse CodeBazar's bot token
or database.

## Initial inventory

The supplied list contains 43 entries but only 41 unique codes. Those 41 actual
codes are seeded into SQLite automatically; the two duplicate entries are
ignored. No additional codes are fabricated. Orders cannot be placed without
enough unique available codes. The admin may add more codes at any time; codes
already in inventory are rejected as duplicates.

## Install on Termux

From the project directory:

```bash
pkg update -y
pkg install python -y
pip install -r requirements.txt
bash setup_termux.sh
```

Edit `.env` and set `BOT_TOKEN` to a new token from BotFather. The admin ID and
QR path are already configured; do not replace them if you want to retain the
CodeBazar admin/payment setup.

Keep the token private. If a token has been pasted into a message, rotate it
with BotFather and put the replacement only in this `.env` file.

Start the bot:

```bash
bash run_termux.sh
```

Keep the Termux session alive while the bot is running. Stop it with Ctrl+C.

## Windows / desktop setup

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `BOT_TOKEN` in `.env`, then run:

```powershell
python bot.py
```

Do not share or commit `.env`. The bot token is required to start; no token is
included in this project.

## Customer flow

1. `/start` opens the store menu.
2. **🛒 Buy Coupon** offers 1, 2, 5, 10, and custom quantity selections.
3. Custom quantities accept positive whole numbers only and cannot exceed
   currently available stock.
4. The confirmation shows quantity, unit price, total, discount, and minimum
   shopping value. **Continue to Payment** creates one unique order and reserves
   that many real coupon codes in one transaction. Customers may have multiple
   active orders; each confirmation creates an independent order with its own
   stock reservation, payment and delivery state.
5. The bot displays the copied UPI QR and requests the exact total
   (₹139 × selected quantity). As soon as the QR is sent, the admin receives a
   “Waiting for Payment” order notice. A 10-minute UTR-entry window is used.
6. After payment, the customer clicks **✅ I'VE PAID** for the relevant order
   and submits its 12-digit UTR. If several orders are waiting for UTRs, each
   UTR is associated with the order the customer selected. Invalid, duplicate,
   or late submissions are rejected with an explanation.
7. The order becomes **Waiting for Admin Confirmation**. The admin receives
   a second order/customer/quantity/total/UTR/QR-method notice with **Approve
   Payment** / **Reject Payment** controls.
8. Rejection releases the reserved coupons. Approval rechecks the reservation
   and sends exactly one private Telegram message per coupon code. Inventory
   and order delivery status are updated per code, then the admin is notified
   when delivery is complete.

The QR message includes **I'VE PAID**; a customer may also submit a UTR as a
message while an order is waiting for one. The order, payment state, UTR,
inventory and delivery status persist across bot restarts.

## Admin

Before customer payments are accepted, the configured admin must open the SHEIN
bot in a private Telegram chat and send `/start` once. Telegram does not let a bot
start a private conversation with an admin who has never started that bot.
For admin ID `5940108626`, `/start` opens the admin panel; `/admin` can also
reopen it. The panel provides dashboard, order/payment lists, coupon inventory
and stock, coupon entry, user management/blocking, and customer-support
inbox/replies. All admin buttons and commands are checked against the configured
Telegram ID.

The QR-screen and UTR-review notifications are sent by this SHEIN bot to the
configured admin ID. Telegram send failures are logged with the order and admin
IDs. If a notification cannot be delivered, the customer is told rather than
being shown a false success; after starting the bot, the admin can check pending
orders in `/admin`.

Coupon delivery is one-shot. Each code is marked as sending before delivery to
prevent repeated sends. If delivery cannot be confirmed, that code and any
remaining unsent codes are locked with an unknown outcome; do not retry them.

## Verify locally

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

Existing one-coupon databases are upgraded on startup with quantity, total
amount, and payment-screen fields; existing orders default to one coupon. The
41 unique initial codes are inserted idempotently; existing inventory states
are preserved. Back up `instance/shein.db`
securely. It contains customer IDs, payment UTRs, support messages, and coupon
inventory. Protect `.env` and the QR image too.
