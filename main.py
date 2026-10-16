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
    BufferedInputFile
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

# Premium custom emojis from user request & config
EMOJI_HEART = "5465262274031659421"
EMOJI_TEXT = "5258500400918587241"
EMOJI_GIFT = "5355193051193059834"
EMOJI_SHOP = "5451937962629544243"
EMOJI_SUPPORT = "5395804191769763641"
EMOJI_START = "5366119022792294762"
EMOJI_18PLUS = "5201920821043422202"

ADMIN_IDS = set(config.ADMIN_IDS)
COLOR_STYLES = {"success": "success", "danger": "danger", "primary": "primary"}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def e(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def money(amount) -> str:
    return f"{Decimal(str(amount)):.2f}"

def home_kb_for(user_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="💳 Topup Wallet", callback_data="topup_wallet")
    b.button(text="❓ How to Buy", callback_data="how_to_buy")
    if config.SUPPORT_URL:
        b.button(text="🆘 Support", url=config.SUPPORT_URL)
    else:
        b.button(text="🆘 Support", callback_data="support")
    if getattr(config, "PROOF_CHANNEL_URL", ""):
        b.button(text="🛡️ Proof Channel", url=config.PROOF_CHANNEL_URL)
    if is_admin(user_id):
        b.button(text="⚙️ Admin Panel", callback_data="admin")
    b.adjust(2, 2, 1)
    return b.as_markup()

def product_kb(product_id: str, color: str):
    style = COLOR_STYLES.get(color, "primary")
    kwargs = {"text": f"{e(EMOJI_SHOP, '🛍️')} Buy Now via Paytm/Gateway", "callback_data": f"buy:{product_id}"}
    try:
        button = InlineKeyboardButton(**kwargs, style=style)
    except (TypeError, ValueError):
        button = InlineKeyboardButton(**kwargs)
    return InlineKeyboardMarkup(inline_keyboard=[
        [button]
    ])

def payment_keyboard(order_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Check Payment Status", callback_data=f"checkpay:{order_id}")],
        [InlineKeyboardButton(text="✍️ Submit UTR", callback_data=f"utr:{order_id}")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home")],
    ])

def qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

async def make_payment(order_id: str, amount: str, user_id: int):
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
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(config.PAYMENT_API_URL, json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 300:
                    log.error("Payment provider create failed: status=%s body=%s", resp.status, body)
                    return None
                return body
    except Exception:
        log.exception("Payment creation request failed")
        return None

async def verify_payment(order_doc):
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
                return (
                    body.get("status") in {"paid", "success", "completed"}
                    and str(body.get("order_id", "")) == order_doc["order_id"]
                    and Decimal(str(body.get("amount", "0"))) == Decimal(str(order_doc["amount"]))
                )
    except Exception:
        log.exception("Provider verification failed")
        return False

async def deliver_order(order_doc):
    claimed = await orders.find_one_and_update(
        {"order_id": order_doc["order_id"], "status": "paid", "delivered": {"$ne": True}},
        {"$set": {"delivered": True, "delivered_at": time.time()}},
        return_document=True,
    )
    if not claimed:
        return
    product = await products.find_one({"_id": claimed["product_id"], "active": True})
    if not product:
        await bot.send_message(claimed["user_id"], f"{e(EMOJI_HEART, '❤️')} Payment confirmed, but this product is currently unavailable. Contact support.")
        return
    await bot.send_message(
        claimed["user_id"],
        f"{e(EMOJI_GIFT, '🎁')} <b>Payment Verified & Delivered Successfully!</b>\n\n"
        f"Product: <b>{product['name']}</b>\n"
        f"Access Link: {product['url']}",
        disable_web_page_preview=True,
    )

@dp.message(CommandStart())
async def start(message: Message):
    await users.update_one({"_id": message.from_user.id}, {"$set": {"name": message.from_user.full_name, "username": message.from_user.username}}, upsert=True)
    
    # Send welcome text with over-loaded custom premium emojis & text sizing
    welcome_text = (
        f"{e(EMOJI_START, '🚀')} <b>ULTIMATE PREMIUM STORE</b> {e(EMOJI_HEART, '💖')}\n"
        f"{e(EMOJI_TEXT, '✨')} <i>Explore our top exclusive products below instantly!</i> {e(EMOJI_18PLUS, '🔞')}\n"
        f"----------------------------------------------"
    )
    await message.answer(welcome_text, reply_markup=home_kb_for(message.from_user.id))

    # Display products directly right under /start command
    items = await products.find({"active": True}).sort("created_at", -1).to_list(length=10)
    if not items:
        await message.answer(f"{e(EMOJI_SHOP, '🛍️')} <b>No products available at the moment. Check back soon!</b>")
    else:
        for p in items:
            product_desc = (
                f"<b>{e(EMOJI_SHOP, '🔥')} {p['name']}</b>\n"
                f"<b>Price:</b> <code>{p['currency']} {money(p['price'])}</code>\n"
                f"<i>{p.get('description', '')}</i>"
            )
            await message.answer(
                product_desc,
                reply_markup=product_kb(str(p["_id"]), p.get("color", "primary")),
                disable_web_page_preview=True,
            )

@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    await call.message.answer(f"{e(EMOJI_START, '🏠')} <b>Main Menu Dashboard</b>", reply_markup=home_kb_for(call.from_user.id))
    items = await products.find({"active": True}).sort("created_at", -1).to_list(length=5)
    for p in items:
        await call.message.answer(
            f"<b>{e(EMOJI_SHOP, '🔥')} {p['name']}</b>\nPrice: <code>{p['currency']} {money(p['price'])}</code>",
            reply_markup=product_kb(str(p["_id"]), p.get("color", "primary")),
            disable_web_page_preview=True,
        )
    await call.answer()

@dp.callback_query(F.data == "topup_wallet")
async def topup_wallet(call: CallbackQuery):
    await call.message.answer(
        f"{e(EMOJI_GIFT, '💳')} <b>Wallet Topup System</b>\n\n"
        f"Send the amount you wish to add to your wallet (e.g., <code>100</code>, <code>500</code> INR):",
    )
    await users.update_one({"_id": call.from_user.id}, {"$set": {"awaiting_topup": True}})
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
        order["payment_url"] = provider.get("payment_url") or provider.get("checkout_url")
        order["qr_text"] = provider.get("qr_text") or provider.get("upi_uri")
    
    # Fallback Paytm UPI string generation if dynamic gateway URI isn't provided directly
    if not order.get("qr_text"):
        upi_id = getattr(config, "PAYTM_UPI_ID", "merchant@paytm")
        order["qr_text"] = f"upi://pay?pa={upi_id}&pn=Store&am={order['amount']}&cu={order['currency']}&tr={order_id}"

    await orders.insert_one(order)

    # Generate Paytm/Gateway QR code using Pillow
    photo = BufferedInputFile(qr_png(order["qr_text"]), filename="paytm-qr.png")
    await call.message.answer_photo(
        photo,
        caption=f"{e(EMOJI_SHOP, '🛍️')} <b>Paytm / UPI Dynamic QR Code Generated</b>\n\n"
                f"Order ID: <code>{order_id}</code>\n"
                f"Amount: <b>{order['currency']} {order['amount']}</b>\n\n"
                f"Scan via Paytm/GPay/PhonePe or click below to check verification instantly!",
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
            {"$set": {"status": "paid", "verified_at": time.time(), "verification_method": "webhook_gateway"}},
        )
        fresh = await orders.find_one({"order_id": order_id})
        await deliver_order(fresh)
        await call.answer("Payment verified successfully!")
    else:
        await call.answer("Payment not detected yet. Complete payment or submit UTR.", show_alert=True)

@dp.callback_query(F.data.startswith("utr:"))
async def ask_utr(call: CallbackQuery):
    order_id = call.data.split(":", 1)[1]
    await users.update_one({"_id": call.from_user.id}, {"$set": {"awaiting_utr_for": order_id}})
    await call.message.answer(f"{e(EMOJI_TEXT, '✍️')} Please send your Paytm UTR / Reference Number for order <code>{order_id}</code>:")
    await call.answer()

@dp.callback_query(F.data == "how_to_buy")
async def how_to_buy(call: CallbackQuery):
    await call.message.answer(
        f"{e(EMOJI_SUPPORT, '❓')} <b>How to Buy Guide</b>\n\n"
        f"1. Choose any product listed directly on the start menu.\n"
        f"2. Click <b>Buy Now</b> to generate the automated Paytm/UPI QR code.\n"
        f"3. Scan and pay using Paytm or any UPI app.\n"
        f"4. Click <b>Check Payment Status</b> or submit your UTR.\n"
        f"5. Instant delivery of your item link right inside the bot!"
    )
    await call.answer()

@dp.callback_query(F.data == "support")
async def support(call: CallbackQuery):
    await call.message.answer(f"{e(EMOJI_SUPPORT, '🆘')} Support contact: {config.SUPPORT_URL or 'Contact admin directly.'}")
    await call.answer()

@dp.message(F.text)
async def text_router(message: Message):
    user = await users.find_one({"_id": message.from_user.id})
    
    if user and user.get("awaiting_topup"):
        await users.update_one({"_id": message.from_user.id}, {"$unset": {"awaiting_topup": ""}})
        await message.answer(f"{e(EMOJI_GIFT, '💳')} Topup request received for amount: <b>{message.text}</b>. Please complete your transaction.")
        return

    if user and user.get("awaiting_utr_for"):
        order_id = user["awaiting_utr_for"]
        utr = message.text.strip()
        await orders.update_one(
            {"order_id": order_id, "user_id": message.from_user.id, "status": "pending"},
            {"$set": {"utr": utr, "utr_submitted_at": time.time()}},
        )
        await message.answer(f"{e(EMOJI_HEART, '✅')} UTR <code>{utr}</code> saved successfully. Admins will verify your transaction shortly.")
        await users.update_one({"_id": message.from_user.id}, {"$unset": {"awaiting_utr_for": ""}})
        return

    # Admin flow
    if is_admin(message.from_user.id) and user and user.get("admin_flow"):
        flow = user["admin_flow"]
        step = flow["step"]
        data = flow["data"]
        text_value = message.text.strip()
        if step == "name":
            data["name"] = text_value[:100]
            flow["step"] = "price"
            prompt = "Send price as a positive number (e.g. 149)."
        elif step == "price":
            data["price"] = money(Decimal(text_value))
            flow["step"] = "url"
            prompt = "Send product delivery URL (https://...)."
        elif step == "url":
            data["url"] = text_value
            flow["step"] = "description"
            prompt = "Send product description."
        elif step == "description":
            data["description"] = text_value
            flow["step"] = "color"
            prompt = "Choose color style: success, danger, or primary."
        elif step == "color":
            data["color"] = text_value
            await products.insert_one({**data, "active": True, "created_at": time.time()})
            await users.update_one({"_id": message.from_user.id}, {"$unset": {"admin_flow": ""}})
            await message.answer(f"Product added successfully!", reply_markup=home_kb_for(message.from_user.id))
            return
        await users.update_one({"_id": message.from_user.id}, {"$set": {"admin_flow": flow}})
        await message.answer(prompt)
        return

    await message.answer(f"{e(EMOJI_START, '🚀')} Send /start to open the store dashboard.")

@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Product", callback_data="admin_add")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home")],
    ])
    await call.message.answer(f"{e(EMOJI_TEXT, '⚙️')} <b>Admin Control Panel</b>", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await users.update_one({"_id": call.from_user.id}, {"$set": {"admin_flow": {"step": "name", "data": {}}}}, upsert=True)
    await call.message.answer("Send new product name:")
    await call.answer()

async def main():
    if not config.BOT_TOKEN or not config.MONGO_URI:
        raise RuntimeError("Set BOT_TOKEN and MONGO_URI in environment variables.")
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Colorful Emoji Bot started successfully with Paytm QR & Webhook support.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
