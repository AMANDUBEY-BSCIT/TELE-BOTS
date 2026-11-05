import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN or BOT_TOKEN.upper().startswith(("PASTE_", "YOUR_")):
    raise RuntimeError(
        "BOT_TOKEN is missing or still a placeholder. Set the SHEIN bot's new "
        "BotFather token in this project's .env file. Do not reuse the CodeBazar token."
    )

_expected_admin_id = 5940108626
_admin_id = os.getenv("ADMIN_ID", "").strip()
if not re.fullmatch(r"[0-9]+", _admin_id):
    raise RuntimeError("ADMIN_ID must be the configured numeric admin ID in .env.")
ADMIN_ID = int(_admin_id)
if ADMIN_ID != _expected_admin_id:
    raise RuntimeError("ADMIN_ID must match the configured CodeBazar admin.")

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "instance" / "shein.db")))
_qr_path = Path(os.getenv("PAYMENT_QR_PATH", "assets/payment_qr.png"))
QR_IMAGE_PATH = _qr_path if _qr_path.is_absolute() else BASE_DIR / _qr_path
PRICE_INR = 139
DISCOUNT_INR = 800
MINIMUM_CART_INR = 1000
INITIAL_STOCK_TARGET = 41
INITIAL_STOCK_ENTRIES = 43
PAYMENT_EXPIRY_MINUTES = 10
PAGE_SIZE = 8
