import asyncio
import logging
import os
import time
import uuid
from decimal import Decimal, InvalidOperation
from io import BytesIO

import aiohttp
import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from motor.motor_asyncio import AsyncIOMotorClient

import config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("premium_store_bot")

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
mongo = AsyncIOMotorClient(config.MONGO_URI)
db = mongo[config.MONGO_DB_NAME]
products = db.products
orders = db.orders
users = db.users

# --- Premium Emoji IDs ---
EMOJI_HEART = "5465262274031659421"
EMOJI_TEXT = "5258500400918587241"
EMOJI_GIFT = "5355193051193059834"
EMOJI_SHOP = "5451937962629544243"
EMOJI_SUPPORT = "5395804191769763641"
EMOJI_START = "5366119022792294762"
EMOJI_18PLUS = "5201920821043422202"
EMOJI_THUNDER = "5992366958681527437"      # New
EMOJI_WATER = "5834455158829879029"        # New
EMOJI_PIN = "5292291996717690768"          # New

ADMIN_IDS = set(config.ADMIN_IDS)
COLOR_STYLES = {"success": "success", "danger": "danger", "primary": "primary"}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def e(emoji_id: str, fallback: str) -> str:
    """Helper to generate premium emoji HTML tag"""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def money(amount) -> str:
    return f"{Decimal(str(amount)):.2f}"

def qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

# --- Keyboards ---

def home_kb_for(user_id: int):
    """Replicates the main UI from screenshot 1 & 4"""
    b = InlineKeyboardBuilder()
    # Top banner style buttons
    b.button(text="Buy ❤️ ALL IN ONE ❤️ - ₹249 ₹199 🔥", callback_data="show_all_products", style="danger")
    b.button(text="💎 ALL MIX 😍⚡ - ₹58 ₹29 🔥 50% OFF", callback_data="show_all_products", style="success")
    b.button(text="💎 INDIAN DESI 😍⚡ - ₹49", callback_data="show_all_products", style="primary")
    
    # Navigation
    b.button(text="🛍️ View All Products", callback_data="show_all_products", style="primary")
    b.button(text="💳 Topup Wallet", callback_data="topup_wallet", style="success")
    b.button(text="❓ How to Buy", callback_data="how_to_buy", style="primary")
    
    if getattr(config, "SUPPORT_URL", None):
        b.button(text="🆘 Support", url=config.SUPPORT_URL, style="success")
    else:
        b.button(text="🆘 Support", callback_data="support", style="danger")
        
    if getattr(config, "PROOF_CHANNEL_URL", None):
        b.button(text="🛡️ Proof", url=config.PROOF_CHANNEL_URL, style="primary")
        
    if is_admin(user_id):
        b.button(text="⚙️ Admin Panel", callback_data="admin", style="danger")
        
    b.adjust(1, 1, 1, 2, 2) # Adjusts layout to match screenshot style
    return b.as_markup()

def product_kb(product_id: str, color: str):
    style = COLOR_STYLES.get(color, "primary")
    kwargs = {"text": f"{e(EMOJI_SHOP, '🛍️')} Buy Now via Paytm/Gateway", "callback_data": f"buy:{product_id}"}
    try:
        button = InlineKeyboardButton(**kwargs, style=style)
    except (TypeError, ValueError):
        button = InlineKeyboardButton(**kwargs)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])

def payment_keyboard(order_id: str):
    """Manual + Automatic payment options"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I Have Paid (Manual Verify)", callback_data=f"manual_paid:{order_id}", style="success")],
        [InlineKeyboardButton(text="🔄 Check Auto Payment Status", callback_data=f"checkpay:{order_id}", style="primary")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home", style="danger")],
    ])

def admin_approval_kb(order_id: str, user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"admin_approve:{order_id}:{user_id}", style="success"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"admin_reject:{order_id}:{user_id}", style="danger")
        ]
    ])

# --- Payment Gateway Logic ---

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
                    log.error(f"Payment provider create failed: {resp.status} {body}")
                    return None
                return body
    except Exception:
        log.exception("Payment creation request failed")
        return None

async def verify_payment(order_doc):
    """Automatic Payment Verification using API"""
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
    """Delivers the product link stored in the product document"""
    claimed = await orders.find_one_and_update(
        {"order_id": order_doc["order_id"], "status": "paid", "delivered": {"$ne": True}},
        {"$set": {"delivered": True, "delivered_at": time.time()}},
        return_document=True,
    )
    if not claimed:
        return
        
    from bson import ObjectId
    product = await products.find_one({"_id": claimed["product_id"], "active": True})
    if not product:
        await bot.send_message(claimed["user_id"], f"{e(EMOJI_HEART, '❤️')} Payment confirmed, but product unavailable. Contact support.")
        return
        
    # Sends the link that admin added via button names or product URL
    await bot.send_message(
        claimed["user_id"],
        f"{e(EMOJI_GIFT, '🎁')} <b>Payment Verified & Delivered Successfully!</b>\n\n"
        f"Product: <b>{product['name']}</b>\n"
        f"Access Link: {product['url']}",
        disable_web_page_preview=True,
    )

# --- Handlers ---

@dp.message(CommandStart())
async def start(message: Message):
    await users.update_one(
        {"_id": message.from_user.id}, 
        {"$set": {"name": message.from_user.full_name, "username": message.from_user.username}}, 
        upsert=True
    )
    
    welcome_text = (
        f"{e(EMOJI_START, '🚀')} <b>ULTIMATE PREMIUM STORE</b> {e(EMOJI_HEART, '💖')}\n"
        f"{e(EMOJI_TEXT, '✨')} <i>Click the button below to view all exclusive products instantly!</i> {e(EMOJI_18PLUS, '🔞')}\n\n"
        f"{e(EMOJI_THUNDER, '⚡')} <b>Limited Time Offers!</b>"
    )
    await message.answer(welcome_text, reply_markup=home_kb_for(message.from_user.id))

@dp.callback_query(F.data == "show_all_products")
async def show_all_products(call: CallbackQuery):
    items = await products.find({"active": True}).sort("created_at", -1).to_list(length=20)
    if not items:
        default_prod = {
            "name": "Premium Adult Collection (1 Lakh+ Videos)",
            "price": "199.00",
            "currency": "INR",
            "url": "https://t.me/+example_access_link",
            "description": "One time payment • Lifetime access • Direct videos groups",
            "color": "success",
            "active": True,
            "created_at": time.time()
        }
        await products.insert_one(default_prod)
        items = [default_prod]

    await call.message.answer(f"{e(EMOJI_SHOP, '🛍️')} <b>Available Products Catalog:</b>")
    for p in items:
        product_desc = (
            f"<b>{e(EMOJI_SHOP, '🔥')} {p['name']}</b>\n"
            f"<b>Price:</b> <code>{p.get('currency', 'INR')} {money(p['price'])}</code>\n"
            f"<i>{p.get('description', '')}</i>"
        )
        await call.message.answer(
            product_desc,
            reply_markup=product_kb(str(p["_id"]), p.get("color", "primary")),
            disable_web_page_preview=True,
        )
    await call.answer()

@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    await call.message.answer(f"{e(EMOJI_START, '🏠')} <b>Main Menu Dashboard</b>", reply_markup=home_kb_for(call.from_user.id))
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
        "currency": p.get("currency", "INR"),
        "status": "pending",
        "delivered": False,
        "utr": None,
        "created_at": time.time(),
    }
    
    # Try Auto Payment Gateway First
    provider = await make_payment(order_id, order["amount"], call.from_user.id)
    if provider:
        order["payment_url"] = provider.get("payment_url") or provider.get("checkout_url")
        order["qr_text"] = provider.get("qr_text") or provider.get("upi_uri")
    
    # Fallback to manual QR
    if not order.get("qr_text"):
        upi_id = getattr(config, "PAYTM_UPI_ID", "merchant@paytm")
        order["qr_text"] = f"upi://pay?pa={upi_id}&pn=Store&am={order['amount']}&cu={order['currency']}&tr={order_id}"

    await orders.insert_one(order)

    photo = BufferedInputFile(qr_png(order["qr_text"]), filename="paytm-qr.png")
    await call.message.answer_photo(
        photo,
        caption=f"{e(EMOJI_SHOP, '🛍️')} <b>Dynamic QR Code Generated</b>\n\n"
                f"Order ID: <code>{order_id}</code>\n"
                f"Amount: <b>{order['currency']} {order['amount']}</b>\n\n"
                f"{e(EMOJI_WATER, '💧')} Scan & pay. Then choose an option below:",
        reply_markup=payment_keyboard(order_id),
    )
    await call.answer()

@dp.callback_query(F.data.startswith("manual_paid:"))
async def manual_paid(call: CallbackQuery):
    """Triggered when user clicks 'I Have Paid'"""
    order_id = call.data.split(":", 1)[1]
    order = await orders.find_one({"order_id": order_id, "user_id": call.from_user.id})
    if not order:
        await call.answer("Order not found.", show_alert=True)
        return
        
    await users.update_one({"_id": call.from_user.id}, {"$set": {"awaiting_utr_for": order_id}})
    await call.message.answer(
        f"{e(EMOJI_PIN, '📌')} <b>Please send your Payment Proof / UTR / Transaction ID</b> for order <code>{order_id}</code>:\n\n"
        f"{e(EMOJI_TEXT, '✍️')} Send the screenshot or text reference now. Admin will verify it shortly."
    )
    await call.answer()

@dp.callback_query(F.data.startswith("checkpay:"))
async def check_payment(call: CallbackQuery):
    """Auto verify payment via API"""
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
        await call.answer("Payment auto-verified successfully!")
    else:
        await call.answer("Payment not detected yet. Complete payment or submit UTR manually.", show_alert=True)

@dp.callback_query(F.data == "how_to_buy")
async def how_to_buy(call: CallbackQuery):
    await call.message.answer(
        f"{e(EMOJI_SUPPORT, '❓')} <b>How to Buy Guide</b>\n\n"
        f"1. Click the <b>View All Products</b> button on the main menu.\n"
        f"2. Select your desired product and click <b>Buy Now</b>.\n"
        f"3. Scan and pay using any UPI app via the generated QR.\n"
        f"4. Click <b>I Have Paid</b> (Manual) or <b>Check Auto Status</b> (Automatic).\n"
        f"5. If manual, send screenshot/UTR to admin. If auto, system verifies instantly.\n"
        f"6. Instant delivery of your item link right inside the bot!"
    )
    await call.answer()

@dp.callback_query(F.data == "support")
async def support(call: CallbackQuery):
    await call.message.answer(f"{e(EMOJI_SUPPORT, '🆘')} Support contact: {getattr(config, 'SUPPORT_URL', 'Contact admin directly.')}")
    await call.answer()

# --- Admin Approval/Rejection Handlers ---

@dp.callback_query(F.data.startswith("admin_approve:"))
async def admin_approve(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return
    _, order_id, user_id = call.data.split(":")
    order = await orders.find_one({"order_id": order_id, "user_id": int(user_id)})
    if not order:
        await call.answer("Order not found.", show_alert=True)
        return
        
    await orders.update_one(
        {"order_id": order_id},
        {"$set": {"status": "paid", "verified_at": time.time(), "verification_method": "admin_manual"}}
    )
    fresh = await orders.find_one({"order_id": order_id})
    await deliver_order(fresh)
    
    await call.message.edit_text(f"{e(EMOJI_HEART, '✅')} <b>Order Approved!</b> User has been delivered the product.")
    await call.answer("Approved successfully!")

@dp.callback_query(F.data.startswith("admin_reject:"))
async def admin_reject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return
    _, order_id, user_id = call.data.split(":")
    
    await orders.update_one(
        {"order_id": order_id, "user_id": int(user_id)},
        {"$set": {"status": "rejected", "rejected_at": time.time()}}
    )
    
    await bot.send_message(int(user_id), f"{e(EMOJI_THUNDER, '❌')} Your payment proof for order <code>{order_id}</code> was rejected by Admin. Please contact support.")
    await call.message.edit_text(f"{e(EMOJI_THUNDER, '❌')} <b>Order Rejected.</b> User has been notified.")
    await call.answer("Rejected successfully!")

# --- Text Router (Handles UTR/Proof, Topup, Admin Flows) ---

@dp.message(F.text)
async def text_router(message: Message):
    user = await users.find_one({"_id": message.from_user.id})
    
    if user and user.get("awaiting_topup"):
        await users.update_one({"_id": message.from_user.id}, {"$unset": {"awaiting_topup": ""}})
        await message.answer(f"{e(EMOJI_GIFT, '💳')} Topup request received for amount: <b>{message.text}</b>. Please complete your transaction.")
        return

    # Manual Payment Proof Handling
    if user and user.get("awaiting_utr_for"):
        order_id = user["awaiting_utr_for"]
        utr = message.text.strip()
        await orders.update_one(
            {"order_id": order_id, "user_id": message.from_user.id, "status": "pending"},
            {"$set": {"utr": utr, "utr_submitted_at": time.time()}},
        )
        await message.answer(f"{e(EMOJI_HEART, '✅')} UTR/Proof <code>{utr}</code> saved successfully. Admins will verify your transaction shortly.")
        await users.update_one({"_id": message.from_user.id}, {"$unset": {"awaiting_utr_for": ""}})
        
        # Notify Admins
        order = await orders.find_one({"order_id": order_id})
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🚨 <b>New Manual Payment Proof</b>\n\n"
                    f"👤 User: {message.from_user.full_name} (<code>{message.from_user.id}</code>)\n"
                    f"📦 Product: {order['product_name']}\n"
                    f"💰 Amount: {order['amount']} {order['currency']}\n"
                    f"📝 UTR/Proof: <code>{utr}</code>",
                    reply_markup=admin_approval_kb(order_id, message.from_user.id)
                )
            except Exception as e:
                log.error(f"Failed to notify admin {admin_id}: {e}")
        return

    # Admin Product Add Flow
    if is_admin(message.from_user.id) and user and user.get("admin_flow"):
        flow = user["admin_flow"]
        action = flow.get("action")
        
        if action == "add":
            step = flow["step"]
            data = flow["data"]
            text_value = message.text.strip()
            if step == "name":
                data["name"] = text_value[:100]
                flow["step"] = "price"
                prompt = "Send price as a positive number (e.g. 499)."
            elif step == "price":
                data["price"] = money(Decimal(text_value))
                flow["step"] = "currency"
                prompt = "Send currency (e.g. INR):"
            elif step == "currency":
                data["currency"] = text_value.upper()
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
                await message.answer(f"{e(EMOJI_HEART, '✅')} Product added successfully!", reply_markup=home_kb_for(message.from_user.id))
                return
            await users.update_one({"_id": message.from_user.id}, {"$set": {"admin_flow": flow}})
            await message.answer(prompt)
            return

        elif action == "edit_price":
            product_id = flow["product_id"]
            try:
                new_price = money(Decimal(message.text.strip()))
                from bson import ObjectId
                await products.update_one({"_id": ObjectId(product_id)}, {"$set": {"price": new_price}})
                await users.update_one({"_id": message.from_user.id}, {"$unset": {"admin_flow": ""}})
                await message.answer(f"{e(EMOJI_HEART, '✅')} Product price updated successfully to {new_price}!", reply_markup=home_kb_for(message.from_user.id))
            except InvalidOperation:
                await message.answer("Invalid price format. Send a valid number (e.g. 499):")
            return
            
        elif action == "edit_button_name":
            product_id = flow["product_id"]
            new_name = message.text.strip()
            from bson import ObjectId
            await products.update_one({"_id": ObjectId(product_id)}, {"$set": {"name": new_name}})
            await users.update_one({"_id": message.from_user.id}, {"$unset": {"admin_flow": ""}})
            await message.answer(f"{e(EMOJI_HEART, '✅')} Product button name updated to '{new_name}'!", reply_markup=home_kb_for(message.from_user.id))
            return

    await message.answer(f"{e(EMOJI_START, '🚀')} Send /start to open the store dashboard.")

# --- Admin Panel Handlers ---

@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Product", callback_data="admin_add", style="success")],
        [InlineKeyboardButton(text="✏️ Edit Product Price", callback_data="admin_list_edit", style="primary")],
        [InlineKeyboardButton(text="🏷️ Edit Button Name", callback_data="admin_list_edit_name", style="primary")],
        [InlineKeyboardButton(text="🗑️ Delete Product", callback_data="admin_list_delete", style="danger")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home", style="primary")],
    ])
    await call.message.answer(f"{e(EMOJI_TEXT, '⚙️')} <b>Admin Control Panel</b>", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await users.update_one({"_id": call.from_user.id}, {"$set": {"admin_flow": {"action": "add", "step": "name", "data": {}}}}, upsert=True)
    await call.message.answer("Send new product name:")
    await call.answer()

@dp.callback_query(F.data == "admin_list_edit")
async def admin_list_edit(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    items = await products.find({"active": True}).to_list(length=20)
    if not items:
        await call.answer("No products found.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in items:
        b.button(text=f"✏️ {p['name']} ({p.get('currency', 'INR')} {p['price']})", callback_data=f"edpr:{p['_id']}")
    b.adjust(1)
    await call.message.answer("Select product to edit price:", reply_markup=b.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("edpr:"))
async def edit_product_price_prompt(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    product_id = call.data.split(":", 1)[1]
    await users.update_one({"_id": call.from_user.id}, {"$set": {"admin_flow": {"action": "edit_price", "product_id": product_id}}})
    await call.message.answer("Send the new price for this product (e.g., 499):")
    await call.answer()

@dp.callback_query(F.data == "admin_list_edit_name")
async def admin_list_edit_name(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    items = await products.find({"active": True}).to_list(length=20)
    if not items:
        await call.answer("No products found.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in items:
        b.button(text=f"🏷️ {p['name']}", callback_data=f"edname:{p['_id']}")
    b.adjust(1)
    await call.message.answer("Select product to edit button name:", reply_markup=b.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("edname:"))
async def edit_product_name_prompt(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    product_id = call.data.split(":", 1)[1]
    await users.update_one({"_id": call.from_user.id}, {"$set": {"admin_flow": {"action": "edit_button_name", "product_id": product_id}}})
    await call.message.answer("Send the new button name for this product:")
    await call.answer()

@dp.callback_query(F.data == "admin_list_delete")
async def admin_list_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    items = await products.find({"active": True}).to_list(length=20)
    if not items:
        await call.answer("No products found.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in items:
        b.button(text=f"🗑️ Delete {p['name']}", callback_data=f"delp:{p['_id']}")
    b.adjust(1)
    await call.message.answer("Select product to delete:", reply_markup=b.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("delp:"))
async def delete_product(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    product_id = call.data.split(":", 1)[1]
    from bson import ObjectId
    try:
        await products.delete_one({"_id": ObjectId(product_id)})
        await call.message.answer(f"{e(EMOJI_HEART, '✅')} Product deleted successfully!")
    except Exception:
        await call.message.answer(f"{e(EMOJI_THUNDER, '❌')} Failed to delete product.")
    await call.answer()

# --- Main ---

async def main():
    if not config.BOT_TOKEN or not config.MONGO_URI:
        raise RuntimeError("Set BOT_TOKEN and MONGO_URI in environment variables.")
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Premium Store Bot started with Auto & Manual Payment, UI Replicated.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
