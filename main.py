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
from bson import ObjectId

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

# --- Safe Premium Emojis Setup ---
# If an ID is invalid, the helper function will fallback to the text emoji.
EMOJI_HEART = "5465262274031659421"
EMOJI_TEXT = "5258500400918587241"
EMOJI_GIFT = "5355193051193059834"
EMOJI_SHOP = "5451937962629544243"
EMOJI_SUPPORT = "5395804191769763641"
EMOJI_START = "5366119022792294762"
EMOJI_PLUS = "5201920821043422202"
EMOJI_THUNDER = "5992366958681527437"
EMOJI_WATER = "5834455158829879029"
EMOJI_PIN = "5292291996717690768"
EMOJI_MONEY = "💰" 
EMOJI_FIRE = "🔥"

ADMIN_IDS = set(config.ADMIN_IDS)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def e(emoji_id: str, fallback: str) -> str:
    """Safely generate premium emoji HTML, falling back to text if ID is empty."""
    if not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def money(amount) -> str:
    return f"{Decimal(str(amount)):.2f}"

# --- Keyboards ---

def home_kb_for(user_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="ALL IN ONE 🛍️ - ₹249 ₹199", callback_data="show_all_products", style="danger")
    b.button(text="💳 Topup Wallet", callback_data="topup_wallet", style="primary")
    b.button(text="❓ How to Buy", callback_data="how_to_buy", style="success")
    if config.SUPPORT_URL:
        b.button(text="🆘 Support", url=config.SUPPORT_URL, style="danger")
    else:
        b.button(text="🆘 Support", callback_data="support", style="danger")
    if getattr(config, "PROOF_CHANNEL_URL", ""):
        b.button(text="🛡️ Proof Channel", url=config.PROOF_CHANNEL_URL, style="primary")
    if is_admin(user_id):
        b.button(text="⚙️ Admin Panel", callback_data="admin", style="success")
    b.adjust(1, 2, 2, 1)
    return b.as_markup()

def product_kb(product_id: str, color: str):
    style = color if color in ["success", "danger", "primary"] else "primary"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Buy Now", callback_data=f"buy:{product_id}", style=style)]
    ])

def payment_keyboard(order_id: str, is_manual: bool = False):
    buttons = []
    if is_manual:
        buttons.append([InlineKeyboardButton(text="✅ I Have Paid", callback_data=f"manual_paid:{order_id}", style="success")])
        buttons.append([InlineKeyboardButton(text="🔄 Check Payment Status", callback_data=f"checkpay:{order_id}", style="primary")])
    else:
        buttons.append([InlineKeyboardButton(text="🔄 Check Payment Status", callback_data=f"checkpay:{order_id}", style="primary")])
    
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="home", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_approval_kb(order_id: str, user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"adm_approve:{order_id}:{user_id}", style="success"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"adm_reject:{order_id}:{user_id}", style="danger")
        ]
    ])

# --- Helper Functions ---

def qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

async def make_payment(order_id: str, amount: str, user_id: int):
    if not (config.FAMPAY_API_URL and config.FAMPAY_API_KEY):
        return None
    payload = {
        "order_id": order_id,
        "amount": amount,
        "currency": "INR",
        "customer": {"telegram_user_id": str(user_id)},
    }
    headers = {"Authorization": f"Bearer {config.FAMPAY_API_KEY}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(config.FAMPAY_API_URL, json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 300:
                    return None
                return body
    except Exception:
        return None

async def verify_payment_gateway(order_doc):
    if not (config.FAMPAY_STATUS_URL and config.FAMPAY_API_KEY):
        return False
    url = config.FAMPAY_STATUS_URL.rstrip("/") + "/" + order_doc["order_id"]
    headers = {"Authorization": f"Bearer {config.FAMPAY_API_KEY}"}
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

# --- Handlers ---

@dp.message(CommandStart())
async def start(message: Message):
    await users.update_one(
        {"_id": message.from_user.id}, 
        {"$set": {"name": message.from_user.full_name, "username": message.from_user.username}}, 
        upsert=True
    )
    
    welcome_text = (
        f"{e(EMOJI_START, '🚀')} <b>WELCOME TO PREMIUM ADULT COLLECTION</b> {e(EMOJI_HEART, '💖')}\n\n"
        f"👋 Hello <b>{message.from_user.full_name}</b>!\n"
        f"🥇 <b>PREMIUM ADULT COLLECTION</b> 🎬\n\n"
        f"{e(EMOJI_FIRE, '🔥')} <b>1 Lakh+ videos At just ₹199/-</b>\n"
        f"🛒 All Categories in One Package\n\n"
        f"➡️ ONE TIME PAYMENT\n"
        f"♾️ LIFETIME ACCESS\n"
        f"✅ DIRECT VIDEOS GROUPS\n\n"
        f"Starting AT ₹29 /- Don't miss it !! 👑\n\n"
        f"<b>LIMITED TIME PAID GROUP ENTRY ONLY 199</b> 🙋‍♂️\n\n"
        f"⬇️ <b>Choose a plan below:</b>"
    )
    await message.answer(welcome_text, reply_markup=home_kb_for(message.from_user.id))

@dp.callback_query(F.data == "show_all_products")
async def show_all_products(call: CallbackQuery):
    items = await products.find({"active": True}).sort("created_at", -1).to_list(length=20)
    if not items:
        default_prods = [
            {"name": "ALL MIX 😍⚡", "price": "29.00", "currency": "INR", "url": "https://t.me/+example1", "description": "All mix premium collection.", "color": "danger", "active": True, "created_at": time.time()},
            {"name": "INDIAN DESI 😍⚡", "price": "49.00", "currency": "INR", "url": "https://t.me/+example2", "description": "Indian Desi exclusive.", "color": "primary", "active": True, "created_at": time.time()},
            {"name": "PUNJABI 😍⚡", "price": "79.00", "currency": "INR", "url": "https://t.me/+example3", "description": "Punjabi collection.", "color": "danger", "active": True, "created_at": time.time()},
            {"name": "ALL IN ONE ❤️🛍️", "price": "199.00", "currency": "INR", "url": "https://t.me/+example4", "description": "1 Lakh+ videos, Lifetime access.", "color": "danger", "active": True, "created_at": time.time()}
        ]
        await products.insert_many(default_prods)
        items = default_prods

    await call.message.answer(f"{e(EMOJI_SHOP, '🛍️')} <b>Available Premium Collections:</b>")
    for p in items:
        product_desc = (
            f"💎💎 <b>{p['name']}</b> — <s>₹{money(p['price'])}</s> <b>₹{money(p['price'])}</b> {e(EMOJI_FIRE, '🔥')}\n"
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

@dp.callback_query(F.data.startswith("buy:"))
async def buy_product(call: CallbackQuery):
    product_id = call.data.split(":", 1)[1]
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
    
    provider = await make_payment(order_id, order["amount"], call.from_user.id)
    is_manual = True
    
    if provider:
        order["payment_url"] = provider.get("payment_url") or provider.get("checkout_url")
        order["qr_text"] = provider.get("qr_text") or provider.get("upi_uri")
        is_manual = False
    
    if not order.get("qr_text"):
        upi_id = getattr(config, "PAYTM_UPI_ID", "merchant@paytm")
        order["qr_text"] = f"upi://pay?pa={upi_id}&pn=Store&am={order['amount']}&cu={order['currency']}&tr={order_id}"

    await orders.insert_one(order)

    photo = BufferedInputFile(qr_png(order["qr_text"]), filename="paytm-qr.png")
    
    caption = (
        f"{e(EMOJI_SHOP, '🛍️')} <b>Payment Required</b>\n\n"
        f"Order ID: <code>{order_id}</code>\n"
        f"Amount: <b>{order['currency']} {order['amount']}</b>\n\n"
        f"👇 <b>Scan the QR via any UPI App (Paytm/GPay/PhonePe)</b>\n\n"
    )
    if is_manual:
        caption += f"⚠️ <i>Automatic verification is off. Please pay and click 'I Have Paid' below.</i>"
    else:
        caption += f"⚡ <i>Automatic verification active. Click 'Check Payment Status' after paying.</i>"

    await call.message.answer_photo(
        photo,
        caption=caption,
        reply_markup=payment_keyboard(order_id, is_manual=is_manual),
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
        
    if await verify_payment_gateway(order):
        await orders.update_one(
            {"order_id": order_id, "status": "pending"},
            {"$set": {"status": "paid", "verified_at": time.time(), "verification_method": "gateway"}},
        )
        fresh = await orders.find_one({"order_id": order_id})
        await deliver_order(fresh)
        await call.answer("Payment verified successfully! Link sent.")
    else:
        await call.answer("Payment not detected yet. Complete payment or submit proof.", show_alert=True)

@dp.callback_query(F.data.startswith("manual_paid:"))
async def manual_paid(call: CallbackQuery):
    order_id = call.data.split(":", 1)[1]
    await users.update_one({"_id": call.from_user.id}, {"$set": {"awaiting_proof_for": order_id}})
    await call.message.answer(
        f"{e(EMOJI_PIN, '📌')} <b>Please send your Payment Proof (Screenshot) and UTR / Transaction ID.</b>\n\n"
        f"Send the screenshot first, then the UTR number, or send them together as a caption."
    )
    await call.answer()

@dp.message(F.photo | F.document)
async def handle_proof_photo(message: Message):
    user = await users.find_one({"_id": message.from_user.id})
    if not user or not user.get("awaiting_proof_for"):
        return
    
    order_id = user["awaiting_proof_for"]
    order = await orders.find_one({"order_id": order_id, "user_id": message.from_user.id})
    if not order:
        return

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    caption = message.caption or "No UTR provided in caption."
    
    await orders.update_one(
        {"order_id": order_id},
        {"$set": {"proof_file_id": file_id, "utr": caption, "status": "pending_approval", "proof_submitted_at": time.time()}}
    )
    
    await users.update_one({"_id": message.from_user.id}, {"$unset": {"awaiting_proof_for": ""}})
    
    await message.answer(f"{e(EMOJI_HEART, '✅')} Proof submitted! Admin will verify and approve your order shortly.")
    
    admin_msg = (
        f"🚨 <b>New Payment Proof Submitted</b>\n\n"
        f"Order ID: <code>{order_id}</code>\n"
        f"User: {message.from_user.full_name} (@{message.from_user.username})\n"
        f"Product: {order['product_name']}\n"
        f"Amount: ₹{order['amount']}\n"
        f"UTR/Caption: {caption}\n\n"
        f"Please verify and approve/reject below."
    )
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id, 
                photo=file_id, 
                caption=admin_msg, 
                reply_markup=admin_approval_kb(order_id, message.from_user.id)
            )
        except Exception as e:
            log.error(f"Failed to send proof to admin {admin_id}: {e}")

@dp.callback_query(F.data.startswith("adm_approve:"))
async def admin_approve(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return
        
    _, order_id, user_id = call.data.split(":")
    order = await orders.find_one({"order_id": order_id})
    
    if not order:
        await call.answer("Order not found.", show_alert=True)
        return
        
    await orders.update_one(
        {"order_id": order_id},
        {"$set": {"status": "paid", "verified_at": time.time(), "verification_method": "manual_admin"}}
    )
    
    fresh_order = await orders.find_one({"order_id": order_id})
    await deliver_order(fresh_order)
    
    await call.message.edit_caption(caption=call.message.caption + "\n\n✅ <b>Approved by Admin</b>")
    await call.answer("Order Approved and User Notified.")

@dp.callback_query(F.data.startswith("adm_reject:"))
async def admin_reject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return
        
    _, order_id, user_id = call.data.split(":")
    
    await orders.update_one(
        {"order_id": order_id},
        {"$set": {"status": "rejected", "rejected_at": time.time()}}
    )
    
    try:
        await bot.send_message(
            int(user_id),
            f"{e(EMOJI_WATER, '💦')} <b>Payment Rejected!</b>\n\n"
            f"Your proof for Order <code>{order_id}</code> was rejected by the admin.\n"
            f"Please contact support if you believe this is a mistake."
        )
    except Exception as e:
        log.error(f"Could not notify user {user_id}: {e}")
        
    await call.message.edit_caption(caption=call.message.caption + "\n\n❌ <b>Rejected by Admin</b>")
    await call.answer("Order Rejected and User Notified.")

@dp.callback_query(F.data == "how_to_buy")
async def how_to_buy(call: CallbackQuery):
    text = (
        f"🛒 <b>How to Buy from PREMIUM STORE</b>\n\n"
        f"1️⃣ Start the Bot\n"
        f"2️⃣ Select your Plan\n"
        f"3️⃣ Click Pay Now & scan the QR\n"
        f"4️⃣ Pay the exact amount via UPI\n"
        f"5️⃣ Click Verify Payment or I Have Paid\n"
        f"6️⃣ Send the payment screenshot to the bot\n"
        f"✅ Done! You'll receive your link within 5-10 minutes\n\n"
        f"⚠️ <b>IMPORTANT:</b> Always pay the exact amount shown by the bot."
    )
    await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data == "support")
async def support(call: CallbackQuery):
    await call.message.answer(f"{e(EMOJI_SUPPORT, '🆘')} Support contact: {config.SUPPORT_URL or 'Contact admin directly.'}")
    await call.answer()

@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Product", callback_data="admin_add", style="success")],
        [InlineKeyboardButton(text="✏️ Edit Product Price", callback_data="admin_list_edit", style="primary")],
        [InlineKeyboardButton(text="🗑️ Delete Product", callback_data="admin_list_delete", style="danger")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home", style="primary")],
    ])
    await call.message.answer(f"⚙️ <b>Admin Control Panel</b>", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await users.update_one({"_id": call.from_user.id}, {"$set": {"admin_flow": {"action": "add", "step": "name", "data": {}}}}, upsert=True)
    await call.message.answer("Send new product name:")
    await call.answer()

@dp.callback_query(F.data == "admin_list_edit")
async def admin_list_edit(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    items = await products.find({"active": True}).to_list(length=20)
    if not items:
        await call.answer("No products found.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in items:
        b.button(text=f"✏️ {p['name']} (₹{p['price']})", callback_data=f"edpr:{p['_id']}", style="primary")
    b.adjust(1)
    await call.message.answer("Select product to edit price:", reply_markup=b.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("edpr:"))
async def edit_product_price_prompt(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    product_id = call.data.split(":", 1)[1]
    await users.update_one({"_id": call.from_user.id}, {"$set": {"admin_flow": {"action": "edit_price", "product_id": product_id}}})
    await call.message.answer("Send the new price for this product (e.g., 499):")
    await call.answer()

@dp.callback_query(F.data == "admin_list_delete")
async def admin_list_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    items = await products.find({"active": True}).to_list(length=20)
    if not items:
        await call.answer("No products found.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in items:
        b.button(text=f"🗑️ Delete {p['name']}", callback_data=f"delp:{p['_id']}", style="danger")
    b.adjust(1)
    await call.message.answer("Select product to delete:", reply_markup=b.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("delp:"))
async def delete_product(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    product_id = call.data.split(":", 1)[1]
    try:
        await products.delete_one({"_id": ObjectId(product_id)})
        await call.message.answer("✅ Product deleted successfully!")
    except Exception:
        await call.message.answer("Failed to delete product.")
    await call.answer()

@dp.message(F.text)
async def text_router(message: Message):
    user = await users.find_one({"_id": message.from_user.id})
    
    if user and user.get("awaiting_proof_for"):
        order_id = user["awaiting_proof_for"]
        utr = message.text.strip()
        await orders.update_one(
            {"order_id": order_id, "user_id": message.from_user.id},
            {"$set": {"utr": utr, "status": "pending_approval", "proof_submitted_at": time.time()}}
        )
        await message.answer(f"{e(EMOJI_HEART, '✅')} UTR <code>{utr}</code> saved. Admin will verify shortly.")
        await users.update_one({"_id": message.from_user.id}, {"$unset": {"awaiting_proof_for": ""}})
        
        order = await orders.find_one({"order_id": order_id})
        admin_msg = (
            f"🚨 <b>New UTR Submitted</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"User: {message.from_user.full_name} (@{message.from_user.username})\n"
            f"Product: {order['product_name']}\n"
            f"Amount: ₹{order['amount']}\n"
            f"UTR: {utr}\n\n"
            f"Please verify and approve/reject below."
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_msg, reply_markup=admin_approval_kb(order_id, message.from_user.id))
            except Exception as e:
                log.error(f"Failed to send UTR to admin {admin_id}: {e}")
        return

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
                try:
                    data["price"] = money(Decimal(text_value))
                    flow["step"] = "currency"
                    prompt = "Send currency (e.g. INR):"
                except InvalidOperation:
                    await message.answer("Invalid price. Send a valid number:")
                    return
            elif step == "currency":
                data["currency"] = text_value.upper()
                flow["step"] = "url"
                prompt = "Send product delivery URL (https://...). This is the link users get after payment."
            elif step == "url":
                data["url"] = text_value
                flow["step"] = "description"
                prompt = "Send product description."
            elif step == "description":
                data["description"] = text_value
                flow["step"] = "color"
                prompt = "Choose color style: success, danger, or primary."
            elif step == "color":
                data["color"] = text_value.lower()
                await products.insert_one({**data, "active": True, "created_at": time.time()})
                await users.update_one({"_id": message.from_user.id}, {"$unset": {"admin_flow": ""}})
                await message.answer(f"✅ Product added successfully!", reply_markup=home_kb_for(message.from_user.id))
                return
                
            await users.update_one({"_id": message.from_user.id}, {"$set": {"admin_flow": flow}})
            await message.answer(prompt)
            return

        elif action == "edit_price":
            product_id = flow["product_id"]
            try:
                new_price = money(Decimal(message.text.strip()))
                await products.update_one({"_id": ObjectId(product_id)}, {"$set": {"price": new_price}})
                await users.update_one({"_id": message.from_user.id}, {"$unset": {"admin_flow": ""}})
                await message.answer(f"✅ Product price updated successfully to ₹{new_price}!", reply_markup=home_kb_for(message.from_user.id))
            except InvalidOperation:
                await message.answer("Invalid price format. Send a valid number (e.g. 499):")
            return

    await message.answer(f"{e(EMOJI_START, '🚀')} Send /start to open the store dashboard.")

async def main():
    if not config.BOT_TOKEN or not config.MONGO_URI:
        raise RuntimeError("Set BOT_TOKEN and MONGO_URI in environment variables.")
    
    await orders.create_index("order_id", unique=True)
    await users.create_index("_id")
    
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Premium Store Bot started successfully.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
