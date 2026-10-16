import asyncio
import logging
import os
import time
import uuid
from decimal import Decimal, InvalidOperation

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, BufferedInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from motor.motor_asyncio import AsyncIOMotorClient
import qrcode
from io import BytesIO

import config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("selling_bot")

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
mongo = AsyncIOMotorClient(config.MONGO_URI)
db = mongo[config.MONGO_DB_NAME]
products = db.products
orders = db.orders
users = db.users

# Telegram's standard inline buttons do not support per-button colors in all clients.
# "success"/"danger"/"primary" styles are used only if supported by the installed Bot API/client.
# Premium custom emoji IDs are included as HTML custom-emoji entities in messages.
EMOJI_TICK = "6296367896398399651"
EMOJI_BUY = "5440841102871517055"
EMOJI_FIRE = "5402406965252989103"

ADMIN_IDS = set(config.ADMIN_IDS)
COLOR_LABELS = {"success": "Green", "danger": "Red", "primary": "Blue"}
COLOR_STYLES = {"success": "success", "danger": "danger", "primary": "primary"}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def e(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def money(amount) -> str:
    return f"{Decimal(str(amount)):.2f}"

def home_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🛍️ View Products", callback_data="shop")
    b.button(text="📦 My Orders", callback_data="my_orders")
    b.button(text="❓ How to Buy", callback_data="how_to_buy")
    b.button(text="🆘 Support", url=config.SUPPORT_URL) if config.SUPPORT_URL else b.button(text="🆘 Support", callback_data="support")
    b.adjust(2, 2)
    if is_admin(0):  # no-op; admin button is added per-user in start handler
        pass
    return b.as_markup()

def home_kb_for(user_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="🛍️ View Products", callback_data="shop")
    b.button(text="📦 My Orders", callback_data="my_orders")
    b.button(text="❓ How to Buy", callback_data="how_to_buy")
    if config.SUPPORT_URL:
        b.button(text="🆘 Support", url=config.SUPPORT_URL)
    else:
        b.button(text="🆘 Support", callback_data="support")
    if is_admin(user_id):
        b.button(text="⚙️ Admin Panel", callback_data="admin")
    b.adjust(2, 2, 1)
    return b.as_markup()

def back_home_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home")]
    ])

def product_kb(product_id: str, color: str):
    style = COLOR_STYLES.get(color)
    kwargs = {"text": f"{e(EMOJI_BUY, '🛒')} Buy now", "callback_data": f"buy:{product_id}"}
    # aiogram versions differ in style support; keep compatibility if style isn't accepted.
    try:
        button = InlineKeyboardButton(**kwargs, style=style)
    except (TypeError, ValueError):
        button = InlineKeyboardButton(**kwargs)
    return InlineKeyboardMarkup(inline_keyboard=[
        [button],
        [InlineKeyboardButton(text="⬅️ Products", callback_data="shop")],
    ])

def payment_keyboard(order_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Check payment", callback_data=f"checkpay:{order_id}")],
        [InlineKeyboardButton(text="✍️ Submit UTR", callback_data=f"utr:{order_id}")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home")],
    ])

def qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

async def make_payment(order_id: str, amount: str, user_id: int):
    """Create a payment with the configured provider. Returns provider response or None.
    This uses a generic JSON API contract; adapt the endpoint/payload to your authorized gateway.
    """
    if not (config.PAYMENT_API_URL and config.PAYMENT_API_KEY):
        return None
    payload = {
        "order_id": order_id,
        "amount": amount,
        "currency": config.PAYMENT_CURRENCY,
        "customer": {"telegram_user_id": str(user_id)},
        "callback_url": config.PAYMENT_WEBHOOK_URL or None,
    }
    headers = {"Authorization": f"Bearer {config.PAYMENT_API_KEY}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(config.PAYMENT_API_URL, json=payload, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 300:
                log.error("Payment provider create failed: status=%s body=%s", resp.status, body)
                return None
            return body

async def verify_payment(order_doc):
    """Ask the provider for authoritative payment status. Never trust UTR alone."""
    if not (config.PAYMENT_STATUS_URL and config.PAYMENT_API_KEY):
        return False
    url = config.PAYMENT_STATUS_URL.rstrip("/") + "/" + order_doc["order_id"]
    headers = {"Authorization": f"Bearer {config.PAYMENT_API_KEY}"}
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 300:
                    return False
                # Adapt these fields to your provider's authenticated response.
                return (
                    body.get("status") in {"paid", "success", "completed"}
                    and str(body.get("order_id", "")) == order_doc["order_id"]
                    and Decimal(str(body.get("amount", "0"))) == Decimal(str(order_doc["amount"]))
                    and body.get("currency", config.PAYMENT_CURRENCY) == config.PAYMENT_CURRENCY
                )
    except Exception:
        log.exception("Provider verification failed")
        return False

async def deliver_order(order_doc):
    """Idempotent fulfillment: atomically claim a paid, not-yet-delivered order."""
    claimed = await orders.find_one_and_update(
        {"order_id": order_doc["order_id"], "status": "paid", "delivered": {"$ne": True}},
        {"$set": {"delivered": True, "delivered_at": time.time()}},
        return_document=True,
    )
    if not claimed:
        return
    product = await products.find_one({"_id": claimed["product_id"], "active": True})
    if not product:
        await bot.send_message(claimed["user_id"], "Payment is confirmed, but this product is no longer available. Please contact support.")
        return
    await bot.send_message(
        claimed["user_id"],
        f"{e(EMOJI_TICK, '✅')} <b>Payment verified!</b>\n"
        f"Product: <b>{product['name']}</b>\n"
        f"Your link: {product['url']}",
        disable_web_page_preview=True,
    )

@dp.message(CommandStart())
async def start(message: Message):
    await users.update_one({"_id": message.from_user.id}, {"$set": {"name": message.from_user.full_name, "username": message.from_user.username}}, upsert=True)
    await message.answer(
        f"{e(EMOJI_FIRE, '🔥')} <b>Welcome to the Store</b>\nChoose an option below.",
        reply_markup=home_kb_for(message.from_user.id),
    )

@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    await call.message.answer("Main menu:", reply_markup=home_kb_for(call.from_user.id))
    await call.answer()

@dp.callback_query(F.data == "shop")
async def shop(call: CallbackQuery):
    items = await products.find({"active": True}).sort("created_at", -1).to_list(length=100)
    if not items:
        await call.message.answer("No products are listed yet.", reply_markup=back_home_kb(call.from_user.id))
    else:
        for p in items:
            await call.message.answer(
                f"{e(EMOJI_FIRE, '🔥')} <b>{p['name']}</b>\n"
                f"Price: <b>{p['currency']} {money(p['price'])}</b>\n"
                f"{p.get('description', '')}",
                reply_markup=product_kb(str(p["_id"]), p.get("color", "primary")),
                disable_web_page_preview=True,
            )
    await call.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def buy_product(call: CallbackQuery):
    product_id = call.data.split(":", 1)[1]
    from bson import ObjectId
    try:
        p = await products.find_one({"_id": ObjectId(product_id), "active": True})
    except Exception:
        p = None
    if not p:
        await call.answer("Product unavailable.", show_alert=True)
        return
    order_id = uuid.uuid4().hex
    order = {
        "order_id": order_id,
        "user_id": call.from_user.id,
        "product_id": p["_id"],
        "product_name": p["name"],
        "amount": money(p["price"]),
        "currency": p["currency"],
        "status": "pending",
        "delivered": False,
        "utr": None,
        "created_at": time.time(),
    }
    provider = await make_payment(order_id, order["amount"], call.from_user.id)
    if provider:
        order["provider_payment_id"] = provider.get("payment_id") or provider.get("id")
        order["payment_url"] = provider.get("payment_url") or provider.get("checkout_url")
        order["qr_text"] = provider.get("qr_text") or provider.get("upi_uri")
        order["provider_response_status"] = provider.get("status")
    await orders.insert_one(order)

    if provider and order.get("payment_url"):
        await call.message.answer(
            f"Order <code>{order_id}</code>\nAmount: <b>{order['currency']} {order['amount']}</b>\n"
            "Complete payment using the secure provider checkout, then tap Check payment.",
            reply_markup=payment_keyboard(order_id),
        )
    elif provider and order.get("qr_text"):
        photo = BufferedInputFile(qr_png(order["qr_text"]), filename="payment-qr.png")
        await call.message.answer_photo(
            photo,
            caption=f"Order <code>{order_id}</code>\nAmount: <b>{order['currency']} {order['amount']}</b>\n"
                    "Scan this provider-generated QR. After paying, submit the UTR as a reference. "
                    "Delivery requires provider confirmation.",
            reply_markup=payment_keyboard(order_id),
        )
    else:
        await call.message.answer(
            f"Order <code>{order_id}</code> created.\nAmount: <b>{order['currency']} {order['amount']}</b>\n\n"
            "Payment provider is not configured or did not return a valid checkout/QR. "
            "Your order is pending; no product will be delivered until payment is independently verified.",
            reply_markup=payment_keyboard(order_id),
        )
    await call.answer()

@dp.callback_query(F.data.startswith("checkpay:"))
async def check_payment(call: CallbackQuery):
    order_id = call.data.split(":", 1)[1]
    order = await orders.find_one({"order_id": order_id, "user_id": call.from_user.id})
    if not order:
        await call.answer("Order not found.", show_alert=True)
        return
    if order["status"] == "paid":
        await deliver_order(order)
        await call.answer("Payment already verified.")
        return
    if await verify_payment(order):
        await orders.update_one(
            {"order_id": order_id, "status": "pending"},
            {"$set": {"status": "paid", "verified_at": time.time(), "verification_method": "provider_status_api"}},
        )
        fresh = await orders.find_one({"order_id": order_id})
        await deliver_order(fresh)
        await call.answer("Payment verified.")
    else:
        await call.answer("Payment is not confirmed yet. Check again later.", show_alert=True)

@dp.callback_query(F.data.startswith("utr:"))
async def ask_utr(call: CallbackQuery):
    order_id = call.data.split(":", 1)[1]
    order = await orders.find_one({"order_id": order_id, "user_id": call.from_user.id, "status": "pending"})
    if not order:
        await call.answer("Pending order not found.", show_alert=True)
        return
    await users.update_one({"_id": call.from_user.id}, {"$set": {"awaiting_utr_for": order_id}})
    await call.message.answer(f"Send the UTR/reference number for order <code>{order_id}</code>.\nSubmitting a UTR does not itself verify payment.")
    await call.answer()

@dp.message(F.text)
async def text_router(message: Message):
    user = await users.find_one({"_id": message.from_user.id})
    # Admin product-creation conversation
    if is_admin(message.from_user.id) and user and user.get("admin_flow"):
        flow = user["admin_flow"]
        step = flow["step"]
        data = flow["data"]
        text_value = message.text.strip()
        if step == "name":
            data["name"] = text_value[:100]
            flow["step"] = "price"
            prompt = "Send price as a positive number (e.g. 149 or 2.50)."
        elif step == "price":
            try:
                amount = Decimal(text_value)
                if amount <= 0 or amount > Decimal("100000000"):
                    raise InvalidOperation
                data["price"] = money(amount)
            except (InvalidOperation, ValueError):
                await message.answer("Invalid price. Send a positive number, e.g. 149.")
                return
            flow["step"] = "currency"
            prompt = f"Send currency code (default {config.PAYMENT_CURRENCY}, e.g. INR)."
        elif step == "currency":
            data["currency"] = (text_value.upper() or config.PAYMENT_CURRENCY)[:8]
            flow["step"] = "url"
            prompt = "Send the product delivery URL (https://...)."
        elif step == "url":
            if not text_value.startswith("https://") or len(text_value) > 1500:
                await message.answer("Please send a valid https:// product URL.")
                return
            data["url"] = text_value
            flow["step"] = "description"
            prompt = "Send a short description, or '-' for none."
        elif step == "description":
            data["description"] = "" if text_value == "-" else text_value[:500]
            flow["step"] = "color"
            prompt = "Choose button color by sending one word: green, red, or blue."
        elif step == "color":
            color_map = {"green": "success", "red": "danger", "blue": "primary"}
            color = color_map.get(text_value.lower())
            if not color:
                await message.answer("Send green, red, or blue.")
                return
            data["color"] = color
            await products.insert_one({**data, "active": True, "created_by": message.from_user.id, "created_at": time.time()})
            await users.update_one({"_id": message.from_user.id}, {"$unset": {"admin_flow": ""}})
            await message.answer(f"Product <b>{data['name']}</b> added.\nPrice: {data['currency']} {data['price']}\nColor: {COLOR_LABELS[color]}", reply_markup=home_kb_for(message.from_user.id))
            return
        await users.update_one({"_id": message.from_user.id}, {"$set": {"admin_flow": flow}})
        await message.answer(prompt)
        return
    if user and user.get("awaiting_utr_for"):
        order_id = user["awaiting_utr_for"]
        utr = message.text.strip()
        if len(utr) < 6 or len(utr) > 100:
            await message.answer("That reference looks invalid. Please send the UTR/reference number again.")
            return
        order = await orders.find_one({"order_id": order_id, "user_id": message.from_user.id, "status": "pending"})
        if order:
            await orders.update_one(
                {"order_id": order_id, "user_id": message.from_user.id, "status": "pending"},
                {"$set": {"utr": utr, "utr_submitted_at": time.time()}},
            )
            await message.answer("UTR saved. The order remains pending until the payment provider confirms the matching transaction.")
            if config.ADMIN_IDS:
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, f"UTR submitted\nOrder: <code>{order_id}</code>\nUser: <code>{message.from_user.id}</code>\nUTR: <code>{utr}</code>\nStatus: pending verification")
                    except Exception:
                        pass
        await users.update_one({"_id": message.from_user.id}, {"$unset": {"awaiting_utr_for": ""}})
        return
    await message.answer("Use /start to open the store.")

@dp.callback_query(F.data == "my_orders")
async def my_orders(call: CallbackQuery):
    rows = await orders.find({"user_id": call.from_user.id}).sort("created_at", -1).to_list(length=20)
    if not rows:
        await call.message.answer("You have no orders yet.")
    else:
        lines = ["<b>Your recent orders</b>"]
        for o in rows:
            lines.append(f"• <code>{o['order_id'][:12]}</code> — {o['product_name']} — {o['currency']} {o['amount']} — <b>{o['status']}</b>")
        await call.message.answer("\n".join(lines))
    await call.answer()

@dp.callback_query(F.data == "how_to_buy")
async def how_to_buy(call: CallbackQuery):
    await call.message.answer(
        "1. Open View Products.\n2. Select a product and press Buy now.\n"
        "3. Pay using the checkout/QR shown by the configured provider.\n"
        "4. You may submit the UTR as a reference.\n"
        "5. The product link is sent only after authenticated payment verification."
    )
    await call.answer()

@dp.callback_query(F.data == "support")
async def support(call: CallbackQuery):
    await call.message.answer(f"Support: {config.SUPPORT_URL or 'Please contact the bot administrator.'}")
    await call.answer()

@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add product", callback_data="admin_add")],
        [InlineKeyboardButton(text="📋 List products", callback_data="admin_list")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home")],
    ])
    await call.message.answer("<b>Admin Panel</b>", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await users.update_one({"_id": call.from_user.id}, {"$set": {"admin_flow": {"step": "name", "data": {}}}}, upsert=True)
    await call.message.answer("Send product name.")
    await call.answer()

@dp.callback_query(F.data == "admin_list")
async def admin_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    rows = await products.find({}).sort("created_at", -1).to_list(length=50)
    if not rows:
        await call.message.answer("No products yet.")
    else:
        for p in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑️ Deactivate", callback_data=f"deactivate:{p['_id']}")
            ]])
            await call.message.answer(
                f"<b>{p['name']}</b> — {p['currency']} {money(p['price'])}\n"
                f"Color: {COLOR_LABELS.get(p.get('color'), 'Blue')} | Active: {p.get('active', True)}\n{p['url']}",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
    await call.answer()

@dp.callback_query(F.data.startswith("deactivate:"))
async def deactivate_product(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    from bson import ObjectId
    try:
        result = await products.update_one({"_id": ObjectId(call.data.split(":", 1)[1])}, {"$set": {"active": False}})
        await call.answer("Product deactivated." if result.modified_count else "Product not changed.")
    except Exception:
        await call.answer("Invalid product.", show_alert=True)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    total_products = await products.count_documents({})
    active_products = await products.count_documents({"active": True})
    total_orders = await orders.count_documents({})
    paid_orders = await orders.count_documents({"status": "paid"})
    await call.message.answer(
        f"<b>Store stats</b>\nProducts: {total_products}\nActive products: {active_products}\n"
        f"Orders: {total_orders}\nVerified paid orders: {paid_orders}"
    )
    await call.answer()

@dp.message(F.text)
async def admin_product_flow(message: Message):
    if not is_admin(message.from_user.id):
        return
    user = await users.find_one({"_id": message.from_user.id})
    flow = user.get("admin_flow") if user else None
    if not flow:
        return
    step = flow["step"]
    data = flow["data"]
    text_value = message.text.strip()
    if step == "name":
        data["name"] = text_value[:100]
        flow["step"] = "price"
        prompt = "Send price as a positive number (e.g. 149 or 2.50)."
    elif step == "price":
        try:
            amount = Decimal(text_value)
            if amount <= 0 or amount > Decimal("100000000"):
                raise InvalidOperation
            data["price"] = money(amount)
        except (InvalidOperation, ValueError):
            await message.answer("Invalid price. Send a positive number, e.g. 149.")
            return
        flow["step"] = "currency"
        prompt = f"Send currency code (default {config.PAYMENT_CURRENCY}, e.g. INR)."
    elif step == "currency":
        data["currency"] = (text_value.upper() or config.PAYMENT_CURRENCY)[:8]
        flow["step"] = "url"
        prompt = "Send the product delivery URL (https://...)."
    elif step == "url":
        if not text_value.startswith("https://") or len(text_value) > 1500:
            await message.answer("Please send a valid https:// product URL.")
            return
        data["url"] = text_value
        flow["step"] = "description"
        prompt = "Send a short description, or '-' for none."
    elif step == "description":
        data["description"] = "" if text_value == "-" else text_value[:500]
        flow["step"] = "color"
        prompt = "Choose button color by sending one word: green, red, or blue."
    elif step == "color":
        color_map = {"green": "success", "red": "danger", "blue": "primary"}
        color = color_map.get(text_value.lower())
        if not color:
            await message.answer("Send green, red, or blue.")
            return
        data["color"] = color
        doc = {
            **data,
            "active": True,
            "created_by": message.from_user.id,
            "created_at": time.time(),
        }
        await products.insert_one(doc)
        await users.update_one({"_id": message.from_user.id}, {"$unset": {"admin_flow": ""}})
        await message.answer(f"Product <b>{data['name']}</b> added.\nPrice: {data['currency']} {data['price']}\nColor: {COLOR_LABELS[color]}", reply_markup=home_kb_for(message.from_user.id))
        return
    await users.update_one({"_id": message.from_user.id}, {"$set": {"admin_flow": flow}})
    await message.answer(prompt)

async def main():
    if not config.BOT_TOKEN or not config.MONGO_URI:
        raise RuntimeError("Set BOT_TOKEN and MONGO_URI as environment variables.")
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
