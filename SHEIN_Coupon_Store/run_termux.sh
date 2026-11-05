#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo ".env is missing. Run: bash setup_termux.sh" >&2
    exit 1
fi

exec python bot.py
