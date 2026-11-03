from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🛒 Buy Voucher", callback_data="shop:list"),
                InlineKeyboardButton("📦 My Orders", callback_data="orders:0"),
            ],
            [
                InlineKeyboardButton("🔄 Recover Voucher", callback_data="recover:start"),
                InlineKeyboardButton("💬 Support", callback_data="support:menu"),
            ],
        ]
    )


def product_list(products):
    labels = {
        "gemini_pro": "GEMINI PRO 18 MONTH",
        "pvr_movie": "PVR MOVIE ₹200 OFF",
        "pvr_giftcard": "PVR ₹200 GIFTCARD",
    }
    buttons = [
        [
            InlineKeyboardButton(
                labels.get(product["id"], product["name"][:55]),
                callback_data=f"product:{product['id']}",
            )
        ]
        for product in products
    ]
    return InlineKeyboardMarkup(buttons)


def quantities(product_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1 CODE", callback_data=f"qty:{product_id}:1"),
                InlineKeyboardButton("5 CODE", callback_data=f"qty:{product_id}:5"),
            ],
            [
                InlineKeyboardButton("10 CODE", callback_data=f"qty:{product_id}:10"),
                InlineKeyboardButton("CUSTOM", callback_data=f"qty:{product_id}:custom"),
            ],
        ]
    )


def terms(order_id: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ I Agree / Continue", callback_data=f"agree:{order_id}"),
            InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel:{order_id}"),
        ],
         [InlineKeyboardButton("🔙 Back", callback_data="nav:back"),
          InlineKeyboardButton("🏠 Home", callback_data="home")]]
    )


def payment(order_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order_id}"),
                InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel:{order_id}"),
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data="nav:back"),
                InlineKeyboardButton("🏠 Home", callback_data="home"),
            ],
        ]
    )


def home_only(label: str = "🏠 Home"):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data="home")]]
    )


def pagination(prefix: str, page: int, has_next: bool):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"{prefix}:{page - 1}"))
    if has_next:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:{page + 1}"))
    return InlineKeyboardMarkup([row] if row else [[InlineKeyboardButton("🔙 Back", callback_data="home")]])
