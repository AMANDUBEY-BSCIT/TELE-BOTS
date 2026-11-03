# CodeBazar Telegram Voucher Bot

An asynchronous Telegram voucher store built with `python-telegram-bot` and SQLite. Payment is **manual UTR verification only**: a submitted UTR never approves an order automatically. The configured admin must check the actual UPI transaction before approving it.

## Requirements

- Python 3.10 or later
- A Telegram bot token from BotFather
- The numeric Telegram user ID of the single authorized admin

## Installation

Open this folder in VS Code, then in its terminal run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Edit the project-root `.env` and paste your new BotFather token. If it is absent (for example, in a fresh checkout), create it from `.env.example` first:

```powershell
Copy-Item .env.example .env
```

```dotenv
BOT_TOKEN=PASTE_MY_NEW_BOTFATHER_TOKEN_HERE
ADMIN_ID=5940108626
```

Replace `PASTE_MY_NEW_BOTFATHER_TOKEN_HERE` with the actual token from BotFather. The configured admin is the numeric Telegram user ID `5940108626`; usernames are never used for admin authentication. The bot will not start while the token is empty or still a placeholder, or if `ADMIN_ID` is missing, non-numeric, or different. Never share or commit `.env`.

## Run in VS Code

Activate the virtual environment in the VS Code terminal and run:

```powershell
py bot.py
```

The database is created at `instance\codebazar.db`. The payment QR image is loaded from `assets\payment_qr.png`; the admin's existing Upload / Replace feature also saves the QR at that location. Both persist across restarts. Stop the bot with Ctrl+C.

On startup, the bot verifies its Telegram connection with up to three attempts, waiting two seconds between temporary network failures. Invalid-token errors are reported separately and are not retried. If Telegram remains unreachable, check your internet/DNS connection and run `py bot.py` again.

## First-time setup

1. Start the bot and send `/start`.
2. The configured numeric admin should start a private chat with the bot using `/start`, then send `/admin`. Telegram does not let bots initiate a private conversation before the user starts it.
3. Ensure your static UPI QR image is available at `assets\payment_qr.png`, or use **🖼 Upload QR Image** to upload or replace it from the admin menu.
4. Open **📦 Manage Products** or **🎟 Manage Voucher Stock** to edit the seeded product names, prices, enabled state, or initial quantities.
5. Add voucher inventory quantities before selling. Orders reserve stock while pending; successful delivery decrements total stock. Rejections, cancellations, and expired payment sessions release the reservation.

The initial catalog is seeded once: Gemini Pro 18 Months (₹119, 45), PVR Movie ₹200 Off Voucher (2 Tickets) (₹35, 135), and PVR ₹200 GiftCard (₹109, 15). Existing product settings are not overwritten on later starts.

## Customer and admin flows

- Customer order history is scoped to the requesting Telegram user and paginated. Voucher recovery verifies both the order ID and owner before returning any delivery codes.
- Customer navigation uses one editable inline-keyboard menu message for catalog, order, recovery, support, terms, and UTR prompts. The payment QR is a separate photo because Telegram cannot convert a text message into a photo; its Back and Home controls return to the editable menu.
- Terms acceptance creates the order and reserves the requested stock. The QR payment window starts when the customer accepts the terms and lasts 10 minutes.
- Pressing **I've Paid** edits the QR message to request the UTR. UTR input must contain exactly 12 ASCII digits with no spaces or other characters, and each UTR can be used only once. Invalid input asks the customer to check and resubmit. A successful submission is stored as `PENDING_VERIFICATION`, not approved automatically; the bot notifies admin `5940108626` for manual verification. The customer sees a **Payment Under Review** summary with only a **🏠 HOME** button, and Home returns to the main menu.
- Admin delivery accepts exactly the purchased quantity, one code per line. A transaction prevents duplicate deliveries, duplicate voucher codes, and incorrect stock decrements.
- Support tickets are stored in SQLite. Customer replies and admin replies stay inside the bot; only the configured admin can respond.
- Admin broadcast sends plain text to registered users. Telegram delivery failures are logged and included in the completion count.

## Testing checklist

Run the automated workflow tests with:

```powershell
python -m unittest discover -s tests -v
```

For Telegram integration, run the bot with a test bot token and test account. Verify:

1. `/start`, catalog visibility, stock and price edits, and each quantity selection.
2. Terms cancel/agree, exact total, uploaded QR, and the 10-minute expiration.
3. Valid, invalid, duplicate, and expired UTR submissions. Confirm no UTR triggers automatic approval.
4. Admin approve/reject buttons with both the configured admin and a different Telegram account.
5. Exact-quantity voucher delivery, duplicate-code protection, stock update, order history, and recovery from only the owning account.
6. Support ticket creation with text/image, customer follow-up, admin reply, and resolution.
7. Restart the bot and confirm products, orders, stock, tickets, and QR are retained.

The database and QR are local files. Back them up securely, and restrict host access because they contain customer/order data and voucher codes.
