import re
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database import (
    connection,
    get_all_products,
    get_product,
    is_user_blocked,
    record_activity,
    record_user,
)
from handlers.callback_guard import answer_callback
from keyboards.user_keyboard import main_menu, product_list, quantities, terms
from services.order_service import OrderError, create_order, current_available_stock
from handlers.customer_ui import add_navigation, ensure_private_chat, show_screen

PRODUCT_DETAILS = {
    "gemini_pro": (
        "🤖 <b>Gemini Pro · 18 Months</b>\n"
        "🔐 Private access · No card required\n"
        "🛡️ 24-hour hold warranty"
    ),
    "pvr_movie": (
        "🎬 <b>PVR Movie ₹200 Off</b>\n"
        "🎟️ Discount voucher for 2 movie tickets\n"
        "📱 Valid on the PVR app and website"
    ),
    "pvr_giftcard": (
        "🎁 <b>PVR ₹200 GiftCard</b>\n"
        "🎟️ Redeem through the Gift Card option\n"
        "➕ Multiple codes can be combined"
    ),
}


async def show_products(
    update: Update, context: ContextTypes.DEFAULT_TYPE, push: bool = True
) -> None:
    if update.effective_user:
        record_user(update.effective_user)
        record_activity(update.effective_user.id, "STORE_OPENED")
    products = get_all_products(enabled_only=True)
    if not products:
        await show_screen(
            update,
            context,
            "<b>🛍️ Voucher Store</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "There are no vouchers available right now. Please check back soon.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="nav:back")],
                 [InlineKeyboardButton("🏠 Home", callback_data="home")]]
            ),
            parse_mode=ParseMode.HTML,
            screen={"type": "products"},
            push=push,
        )
        return
    buttons = product_list(products)
    cards = []
    for product in products:
        stock = current_available_stock(product["id"])
        description = PRODUCT_DETAILS.get(product["id"], "")
        cards.append(
            f"<b>{escape(product['name'])}</b>\n"
            f"{description}\n"
            f"💰 <b>₹{product['price']}</b> per code  ·  📦 {stock} available"
        )
    await show_screen(
        update,
        context,
        "<b>🛍️ THE CODEBAZAR STORE</b>\n"
        "<i>Your Trusted Digital Voucher Store</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(cards)
        + "\n\n👇 Tap a voucher to view details and choose quantity.",
        reply_markup=add_navigation(buttons),
        parse_mode=ParseMode.HTML,
        screen={"type": "products"},
        push=push,
    )


async def show_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    product_id: str,
    push: bool = True,
) -> None:
    product = get_product(product_id)
    if product is None or not product["enabled"]:
        await show_screen(
            update,
            context,
            "This product is currently unavailable.",
            reply_markup=add_navigation(None),
            screen={"type": "product", "product_id": product_id},
            push=push,
        )
        return
    stock = current_available_stock(product_id)
    details = (
        f"{PRODUCT_DETAILS.get(product_id, '<b>' + escape(product['name']) + '</b>')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>₹{product['price']} per voucher</b>\n"
        f"📦 <b>{stock}</b> available now\n\n"
        "Choose your quantity to start an order:"
    )
    await show_screen(
        update,
        context,
        details,
        reply_markup=add_navigation(quantities(product_id)),
        parse_mode=ParseMode.HTML,
        screen={"type": "product", "product_id": product_id},
        push=push,
    )


async def show_custom_quantity_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    product_id: str,
    push: bool = True,
    notice: str = "",
) -> None:
    product = get_product(product_id)
    if product is None or not product["enabled"]:
        await show_screen(
            update,
            context,
            "This product is currently unavailable.",
            reply_markup=add_navigation(None),
            screen={"type": "product", "product_id": product_id},
            push=push,
        )
        return
    context.user_data["custom_product"] = product_id
    stock = current_available_stock(product_id)
    await show_screen(
        update,
        context,
        (f"{escape(notice)}\n\n" if notice else "")
        + "🛒 <b>Custom Quantity</b>\n\n"
        "Send the number of voucher codes you want to purchase.\n\n"
        f"📦 Available Stock: <b>{stock}</b>",
        reply_markup=add_navigation(None),
        parse_mode=ParseMode.HTML,
        screen={"type": "custom_quantity", "product_id": product_id},
        push=push,
    )


async def show_order_terms(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order_id: str,
    push: bool = True,
) -> None:
    with connection() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?",
            (order_id, update.effective_user.id),
        ).fetchone()
    if order is None:
        await show_screen(
            update,
            context,
            "Order not found.",
            reply_markup=add_navigation(main_menu()),
            push=push,
        )
        return
    price_line = (
        f"₹{order['unit_price']}"
        if order["product_id"] == "gemini_pro"
        else f"₹{order['unit_price']} per code"
    )
    terms_text = (
        "<b>📋 PLEASE REVIEW YOUR ORDER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ Redeem links must be used within 24 hours of delivery. Report any issue "
        "within 24 hours; replacement or reissue is unavailable after this period.\n\n"
        "⏳ Links expire in 4–6 days (the link, not the plan).\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎟️ <b>{escape(order['product_name'])}</b>\n"
        f"🏷️ Unit price: <b>{price_line}</b>\n"
        f"📦 Quantity: <b>{order['quantity']}</b>\n"
        f"💰 Total: <b>₹{order['total_amount']}.00</b>\n\n"
        "Accept to continue to secure payment instructions."
    )
    await show_screen(
        update,
        context,
        terms_text,
        reply_markup=terms(order_id),
        parse_mode=ParseMode.HTML,
        screen={"type": "terms", "order_id": order_id},
        push=push,
    )


async def handle_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await ensure_private_chat(update, context):
        return
    await answer_callback(query, context)
    if query.from_user:
        record_user(query.from_user)
    if is_user_blocked(query.from_user.id):
        await query.message.reply_text("Access to this bot is currently restricted.")
        return
    data = query.data or ""
    if data == "shop:back":
        context.user_data.pop("custom_product", None)
        from handlers.customer_ui import handle_back

        await handle_back(update, context, answer=False)
        return
    if data == "shop:list":
        context.user_data.pop("custom_product", None)
        current = context.user_data.get("customer_current_screen")
        await show_products(update, context, push=not current or current.get("type") != "products")
        return
    if data == "shop:cancel_custom":
        product_id = context.user_data.pop("custom_product", None)
        from handlers.customer_ui import handle_back

        history = context.user_data.get("customer_screen_history", [])
        if not history and product_id:
            await show_product(update, context, product_id, push=False)
        else:
            await handle_back(update, context, answer=False)
        return
    if data.startswith("product:"):
        product_id = data.split(":", 1)[1]
        product = get_product(product_id)
        if product is None or not product["enabled"]:
            await show_product(update, context, product_id)
            return
        stock = current_available_stock(product_id)
        record_activity(
            query.from_user.id,
            "PRODUCT_VIEWED",
            {"product_id": product_id, "product_name": product["name"], "stock": stock},
        )
        await show_product(update, context, product_id)
        return
    if data.startswith("qty:"):
        _, product_id, requested = data.split(":", 2)
        current_screen = context.user_data.get("customer_current_screen")
        if current_screen and (
            current_screen.get("type") != "product"
            or current_screen.get("product_id") != product_id
        ):
            return
        product = get_product(product_id)
        if product is None or not product["enabled"]:
            await show_product(update, context, product_id)
            return
        stock = current_available_stock(product_id)
        if requested == "custom":
            record_activity(
                query.from_user.id,
                "CUSTOM_QUANTITY_SELECTED",
                {
                    "product_id": product_id,
                    "product_name": product["name"],
                    "unit_price": product["price"],
                },
            )
            await show_custom_quantity_prompt(update, context, product_id)
            return
        if requested not in {"1", "5", "10"}:
            await show_product(update, context, product_id, push=False)
            return
        record_activity(
            query.from_user.id,
            "QUANTITY_SELECTED",
            {
                "product_id": product_id,
                "product_name": product["name"],
                "quantity": int(requested),
                "amount": product["price"] * int(requested),
                "custom": False,
            },
        )
        await _create_and_show_terms(
            update, context, query.from_user.id, product_id, int(requested)
        )


async def handle_custom_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    product_id = context.user_data.get("custom_product")
    if product_id is None:
        return False
    message = update.effective_message
    text = (message.text or "").strip() if message else ""
    if not re.fullmatch(r"\d+", text) or not text.lstrip("0"):
        await show_custom_quantity_prompt(
            update,
            context,
            product_id,
            push=False,
            notice="❌ Invalid quantity. Send a valid positive whole number.",
        )
        return True
    product = get_product(product_id)
    if product is None or not product["enabled"]:
        context.user_data.pop("custom_product", None)
        await show_screen(
            update,
            context,
            "This product is currently unavailable.",
            reply_markup=add_navigation(None),
            push=False,
        )
        return True
    available_stock = current_available_stock(product_id)
    normalized_quantity = text.lstrip("0")
    stock_text = str(available_stock)
    if len(normalized_quantity) > len(stock_text) or (
        len(normalized_quantity) == len(stock_text)
        and normalized_quantity > stock_text
    ):
        await show_custom_quantity_prompt(
            update,
            context,
            product_id,
            push=False,
            notice=(
                "❌ Insufficient Stock!\n"
                f"Available stock: {available_stock}. Send a smaller quantity."
            ),
        )
        return True
    quantity = int(normalized_quantity)
    record_activity(
        update.effective_user.id,
        "QUANTITY_SELECTED",
        {
            "product_id": product_id,
            "product_name": product["name"],
            "quantity": quantity,
            "amount": product["price"] * quantity,
            "custom": True,
        },
    )
    created = await _create_and_show_terms(
        update, context, update.effective_user.id, product_id, quantity
    )
    if created:
        context.user_data.pop("custom_product", None)
    return True


async def _create_and_show_terms(
    update, context, user_id: int, product_id: str, quantity: int
) -> bool:
    try:
        order_id = create_order(user_id, product_id, quantity)
    except OrderError as exc:
        await show_screen(
            update,
            context,
            str(exc),
            reply_markup=add_navigation(None),
            push=False,
        )
        return False
    await show_order_terms(update, context, order_id)
    return True
