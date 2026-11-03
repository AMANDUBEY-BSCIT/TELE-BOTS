from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def dashboard():
    labels = [
        ("📊 Dashboard Overview", "overview"),
        ("👥 All Customers", "customers:0"),
        ("🟢 Live Customer Activity", "live:0"),
        ("💳 Payment Monitoring", "payments:0"),
        ("📦 All Orders", "orders:0"),
        ("⏳ Pending Payments", "pending:0"),
        ("💰 Sales & Revenue", "revenue"),
        ("🎟 Voucher Delivery", "delivery:0"),
        ("💬 Customer Support", "tickets:0"),
        ("🔍 Search Customer / Order", "search"),
        ("📈 Product Analytics", "analytics"),
        ("📢 Broadcast Message", "broadcast"),
        ("⚙️ Settings", "settings"),
    ]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(labels[index][0], callback_data=f"adm:{labels[index][1]}")
                for index in range(row, min(row + 2, len(labels)))
            ]
            for row in range(0, len(labels), 2)
        ]
        + [[InlineKeyboardButton("🔙 Back", callback_data="adm:back")]]
    )


def back_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Dashboard", callback_data="adm:home")]])
