from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🛒 Buy Coupon", callback_data="buy"),
                InlineKeyboardButton("📦 My Orders", callback_data="orders"),
            ],
            [
                InlineKeyboardButton("🎟️ Coupon Information", callback_data="info"),
                InlineKeyboardButton("💬 Customer Support", callback_data="support"),
            ],
        ]
    )


def home_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 HOME", callback_data="home")]]
    )


def coupon_quantities() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1️⃣ Buy 1 Coupon", callback_data="buy:quantity:1"),
                InlineKeyboardButton("2️⃣ Buy 2 Coupons", callback_data="buy:quantity:2"),
            ],
            [
                InlineKeyboardButton("5️⃣ Buy 5 Coupons", callback_data="buy:quantity:5"),
                InlineKeyboardButton("🔟 Buy 10 Coupons", callback_data="buy:quantity:10"),
            ],
            [InlineKeyboardButton("✏️ Custom Quantity", callback_data="buy:custom")],
            [
                InlineKeyboardButton("🔙 Back", callback_data="home"),
                InlineKeyboardButton("🏠 Home", callback_data="home"),
            ],
        ]
    )


def back_home(back: str = "admin:dashboard") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔙 BACK", callback_data=back)],
            [InlineKeyboardButton("🏠 HOME", callback_data="home")],
        ]
    )


def admin_dashboard() -> InlineKeyboardMarkup:
    labels = [
        ("📊 Dashboard", "admin:dashboard"),
        ("📦 View Orders", "admin:orders:all"),
        ("⏳ Pending Payments", "admin:orders:WAITING_FOR_ADMIN"),
        ("✅ Approved Payments", "admin:orders:APPROVED"),
        ("❌ Rejected Payments", "admin:orders:REJECTED"),
        ("🎟️ Manage Coupons", "admin:coupons"),
        ("➕ Add Coupon", "admin:add_coupon"),
        ("📉 Coupon Stock", "admin:stock"),
        ("👥 Manage Users", "admin:users"),
        ("🚫 Block User", "admin:block_user"),
        ("💬 Customer Support", "admin:support"),
    ]
    rows = [
        [
            InlineKeyboardButton(label, callback_data=action)
            for label, action in labels[index : index + 2]
        ]
        for index in range(0, len(labels), 2)
    ]
    rows.append([InlineKeyboardButton("🏠 Customer Home", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def payment_review(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ APPROVE PAYMENT", callback_data=f"admin:approve:{order_id}"
                ),
                InlineKeyboardButton(
                    "❌ REJECT PAYMENT", callback_data=f"admin:reject:{order_id}"
                ),
            ],
            [InlineKeyboardButton("📊 Admin Panel", callback_data="admin:dashboard")],
        ]
    )
