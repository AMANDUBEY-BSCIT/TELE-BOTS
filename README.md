# TELE-BOTS

This repository contains two independent Telegram bot projects:

| Project | Folder | Runtime |
|---|---|---|
| CodeBazar Telegram Voucher Bot | [`CodeBazzar/`](./CodeBazzar/) | Python, `python-telegram-bot`, SQLite |
| SHEIN Coupon Store | [`SHEIN_Coupon_Store/`](./SHEIN_Coupon_Store/) | Python, `python-telegram-bot`, SQLite |

Each project has its own dependencies, `.env`, SQLite database, and startup
instructions. Install and run commands from inside the relevant project folder.
Never copy a bot token or `.env` file between projects.

## CodeBazar

Follow [`CodeBazzar/README.md`](./CodeBazzar/README.md). For a fresh checkout:

```powershell
cd CodeBazzar
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set CodeBazar's own `BOT_TOKEN` in `CodeBazzar/.env`, then run:

```powershell
python bot.py
```

The CodeBazar documentation also includes its Termux/operational details.

## SHEIN Coupon Store

Follow [`SHEIN_Coupon_Store/README.md`](./SHEIN_Coupon_Store/README.md) for
desktop instructions and [`SHEIN_Coupon_Store/README_TERMUX.md`](./SHEIN_Coupon_Store/README_TERMUX.md)
for Termux setup.

### Windows / desktop

```powershell
cd SHEIN_Coupon_Store
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set SHEIN's separate `BOT_TOKEN` in `SHEIN_Coupon_Store/.env`, then run:

```powershell
python bot.py
```

### Termux

From `SHEIN_Coupon_Store/`:

```bash
pkg update -y
pkg install python -y
pip install -r requirements.txt
bash setup_termux.sh
```

Set the SHEIN token in that folder's `.env`, then start it with:

```bash
bash run_termux.sh
```

The SHEIN project uses admin ID `5940108626`, the UPI QR image included in its
own `assets/` directory, and a separate SQLite database under its own
`instance/` directory. Its README documents coupon stock, quantity purchasing,
UTR review, order management, and manual coupon delivery.

## Isolation and secrets

- Start, configure, and update each bot from its own folder.
- `CodeBazzar/.env` and `SHEIN_Coupon_Store/.env` are local secrets and must not
  be committed.
- Each project stores its database independently in its own `instance/`.
- The SHEIN project directory contains no bot token or runtime database.
