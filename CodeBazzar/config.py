import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN or BOT_TOKEN.upper().startswith(("PASTE_", "YOUR_")):
    raise RuntimeError(
        "BOT_TOKEN is missing or still a placeholder. "
        "Paste your new BotFather token into the project-root .env file."
    )

_expected_admin_id = 5940108626
_admin_id = os.getenv("ADMIN_ID", "").strip()
if not re.fullmatch(r"[0-9]+", _admin_id):
    raise RuntimeError("ADMIN_ID must be a positive numeric Telegram user ID in the .env file.")

ADMIN_ID = int(_admin_id)
if ADMIN_ID <= 0 or ADMIN_ID != _expected_admin_id:
    raise RuntimeError("ADMIN_ID must be the configured admin Telegram user ID 5940108626.")

DATABASE_PATH = BASE_DIR / "instance" / "codebazar.db"
QR_DIRECTORY = BASE_DIR / "assets"
QR_IMAGE_PATH = QR_DIRECTORY / "payment_qr.png"
PAYMENT_EXPIRY_MINUTES = 10
PAGE_SIZE = 8
