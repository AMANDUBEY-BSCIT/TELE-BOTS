#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python >/dev/null 2>&1; then
    echo "Python is required. Install it with: pkg install python -y" >&2
    exit 1
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example. Add your BotFather token before starting the bot."
fi

mkdir -p instance
chmod +x run_termux.sh

if [ ! -f assets/payment_qr.png ]; then
    echo "The UPI QR image is missing: assets/payment_qr.png" >&2
    exit 1
fi

echo "Setup complete. Ensure BOT_TOKEN is set in .env, then run: bash run_termux.sh"
