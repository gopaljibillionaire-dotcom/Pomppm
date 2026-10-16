import asyncio
import logging
import time
import uuid
from decimal import Decimal, InvalidOperation
from io import BytesIO

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
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
EMOJI_THUNDER = "5992366958681527437"      
EMOJI_WATER = "5834455158829879029"        
EMOJI_PIN = "5292291996717690768"          

ADMIN_IDS = set(config.ADMIN_IDS)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def e(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def money(amount) -> str:
    return f"{Decimal(str(amount)):.2f}"

def qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

# --- Keyboards ---

def main_menu_kb(user_id: int):
    """Main menu showing available products as buttons only."""
    b = InlineKeyboardBuilder()
    
    # Fetch all active products to list them
    # Note: We will fetch products dynamically in the handler, but we need a placeholder here
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
        
    b.adjust(1, 1, 2, 1) 
    return b.as_markup()

def product_detail_kb(product_id: str, color: str):
    """Keyboard shown when a specific product is selected."""
    style = "success" if color == "success" else "danger" if color == "danger" else "primary"
    b = InlineKeyboardBuilder()
    b.button(text=f"{e(EMOJI_SHOP, '🛍️')} Buy Now", callback_data=f"buy:{product_id}", style=style)
    b.button(text=f"{e(EMOJI_START, '🏠')} Back to Main Menu", callback_data="home", style="danger")
    b.adjust(1)
    return b.as_markup()

def payment_keyboard(order_id: str):
    """Manual + Automatic payment options"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I Have Paid (Manual)", callback_data=f"manual_paid:{order_id}", style="success")],
        [InlineKeyboardButton(text="🔄 Auto Verify (Under Maintenance)", callback_data=f"auto_maintenance:{order_id}", style="primary")],
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
    # Remove awaiting states
    await users.update_one(
        {"_id": message.from_user.id}, 
        {"$unset": {"awaiting_topup": "", "awaiting_utr_for": "", "awaiting_photo_for": ""}},
        upsert=True
    )
    
    # Get available products to show in the menu
    items = await products.find({"active": True}).to_list(length=20)
    
    b = InlineKeyboardBuilder()
    for p in items:
        color = p.get("color", "primary")
        style = "success" if color == "success" else "danger" if color == "danger" else "primary"
        # Using the product name as the button text
        b.button(text=f"🔥 {p['name']} - ₹{money(p['price'])}", callback_data=f"view_prod:{p['_id']}", style=style)
    
    # Add standard menu buttons
    b.button(text="💳 Topup Wallet", callback_data="topup_wallet", style="success")
    b.button(text="❓ How to Buy", callback_data="how_to_buy", style="primary")
    
    if getattr(config, "SUPPORT_URL", None):
        b.button(text="🆘 Support", url=config.SUPPORT_URL, style="success")
    else:
        b.button(text="🆘 Support", callback_data="support", style="danger")
        
    if getattr(config, "PROOF_CHANNEL_URL", None):
        b.button(text="🛡️ Proof", url=config.PROOF_CHANNEL_URL, style="primary")
        
    if is_admin(message.from_user.id):
        b.button(text="⚙️ Admin Panel", callback_data="admin", style="danger")
        
    b.adjust(1, 1, 2, 2, 1) # Adjust layout as needed

    welcome_text = (
        f"{e(EMOJI_START, '🚀')} <b>ULTIMATE PREMIUM STORE</b> {e(EMOJI_HEART, '💖')}\n"
        f"{e(EMOJI_TEXT, '✨')} <i>Choose a product below to purchase!</i> {e(EMOJI_18PLUS, '🔞')}\n\n"
        f"{e(EMOJI_THUNDER, '⚡')} <b>Limited Time Offers!</b>"
    )
    
    if not items:
        welcome_text = f"{e(EMOJI_THUNDER, '⚠️')} <b>No products available yet.</b> Admins have not added any products. Please check back later."
    
    await message.answer(welcome_text, reply_markup=b.as_markup())

@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    # Re-trigger the start logic by calling the function directly
    await start(call.message)
    await call.answer()

@dp.callback_query(F.data.startswith("view_prod:"))
async def view_product(call: CallbackQuery):
    product_id = call.data.split(":", 1)[1]
    from bson import ObjectId
    try:
        p = await products.find_one({"_id": ObjectId(product_id), "active": True})
    except Exception:
        p = None
        
    if not p:
        await call.answer("Product unavailable.", show_alert=True)
        return
        
    # "Change full menu by the text of description heading and the button of buy and back to menu"
    product_desc = (
        f"<b>{e(EMOJI_SHOP, '🔥')} {p['name']}</b>\n\n"
        f"<b>Price:</b> <code>{p.get('currency', 'INR')} {money(p['price'])}</code>\n"
        f"<b>Description:</b> <i>{p.get('description', 'No description provided.')}</i>"
    )
    
    await call.message.edit_text(
        product_desc,
        reply_markup=product_detail_kb(str(p["_id"]), p.get("color", "primary")),
        disable_web_page_preview=True
    )
    await call.answer()

@dp.callback_query(F.data == "topup_wallet")
async def topup_wallet(call: CallbackQuery):
    user = await users.find_one({"_id": call.from_user.id})
    balance = user.get("balance", 0.0) if user else 0.0
    
    await call.message.answer(
        f"{e(EMOJI_GIFT, '💳')} <b>Wallet Topup System</b>\n\n"
        f"Your current balance: <b>₹{money(balance)}</b>\n\n"
        f"Please send the amount you wish to add (e.g., <code>100</code>, <code>500</code> INR):",
    )
    await users.update_one({"_id": call.from_user.id}, {"$set": {"awaiting_topup": True}}, upsert=True)
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
        "proof_type": None,
        "created_at": time.time(),
    }
    
    # QR Generation
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

@dp.callback_query(F.data.startswith("auto_maintenance:"))
async def auto_maintenance(call: CallbackQuery):
    # Popup for Automatic Verification
    await call.answer("⚠️ This feature is under maintenance. Please use Manual Payment.", show_alert=True)

@dp.callback_query(F.data.startswith("manual_paid:"))
async def manual_paid(call: CallbackQuery):
    """Triggered when user clicks 'I Have Paid'"""
    order_id = call.data.split(":", 1)[1]
    order = await orders.find_one({"order_id": order_id, "user_id": call.from_user.id})
    if not order:
        await call.answer("Order not found.", show_alert=True)
        return
        
    await users.update_one({"_id": call.from_user.id}, {"$set": {"awaiting_photo_for": order_id}})
    await call.message.answer(
        f"{e(EMOJI_PIN, '📌')} <b>Please send your Payment Screenshot</b> for order <code>{order_id}</code>:\n\n"
        f"{e(EMOJI_TEXT, '📸')} Send the screenshot now. Admin will verify it shortly."
    )
    await call.answer()

@dp.callback_query(F.data == "how_to_buy")
async def how_to_buy(call: CallbackQuery):
    await call.message.answer(
        f"{e(EMOJI_SUPPORT, '❓')} <b>How to Buy Guide</b>\n\n"
        f"1. Select a product from the main menu.\n"
        f"2. Click <b>Buy Now</b> to generate a payment QR.\n"
        f"3. Scan and pay using any UPI app.\n"
        f"4. Click <b>I Have Paid (Manual)</b>.\n"
        f"5. Send your payment screenshot.\n"
        f"6. Wait for Admin approval. Your link will be delivered instantly!"
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

# --- Text & Photo Router (Handles UTR/Proof, Topup, Admin Flows) ---

@dp.message(F.photo)
async def handle_proof_photo(message: Message):
    """Handles payment screenshots sent by users"""
    user = await users.find_one({"_id": message.from_user.id})
    
    if user and user.get("awaiting_photo_for"):
        order_id = user["awaiting_photo_for"]
        photo_file_id = message.photo[-1].file_id
        
        await orders.update_one(
            {"order_id": order_id, "user_id": message.from_user.id, "status": "pending"},
            {"$set": {"proof_file_id": photo_file_id, "utr_submitted_at": time.time()}},
        )
        await message.answer(f"{e(EMOJI_HEART, '✅')} Payment screenshot received! Your request has been sent to Admin. You will be confirmed shortly.")
        await users.update_one({"_id": message.from_user.id}, {"$unset": {"awaiting_photo_for": ""}})
        
        # Notify Admins with the photo
        order = await orders.find_one({"order_id": order_id})
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_photo(
                    admin_id,
                    photo=photo_file_id,
                    caption=f"🚨 <b>New Manual Payment Proof</b>\n\n"
                            f"👤 User: {message.from_user.full_name} (<code>{message.from_user.id}</code>)\n"
                            f"📦 Product: {order['product_name']}\n"
                            f"💰 Amount: {order['amount']} {order['currency']}\n"
                            f"🆔 Order ID: <code>{order_id}</code>",
                    reply_markup=admin_approval_kb(order_id, message.from_user.id)
                )
            except Exception as e:
                log.error(f"Failed to notify admin {admin_id}: {e}")
        return
    else:
        await message.answer(f"{e(EMOJI_THUNDER, '⚠️')} No pending order requires a photo right now. Please use the menu.")

@dp.message(F.text)
async def text_router(message: Message):
    user = await users.find_one({"_id": message.from_user.id})
    
    # 1. Handle Wallet Topup
    if user and user.get("awaiting_topup"):
        try:
            amount = Decimal(message.text.strip())
            if amount <= 0:
                raise InvalidOperation
            await users.update_one(
                {"_id": message.from_user.id}, 
                {"$inc": {"balance": float(amount)}, "$unset": {"awaiting_topup": ""}}
            )
            new_balance = user.get("balance", 0.0) + float(amount)
            await message.answer(f"{e(EMOJI_HEART, '✅')} Successfully added <b>₹{money(amount)}</b> to your wallet!\nNew Balance: <b>₹{money(new_balance)}</b>")
        except InvalidOperation:
            await message.answer(f"{e(EMOJI_THUNDER, '❌')} Invalid amount. Please send a valid positive number (e.g., 100):")
        return

    # 2. Handle Admin Add Product Flow
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
                prompt = "Send price in INR (e.g., 499):"
            elif step == "price":
                try:
                    data["price"] = money(Decimal(text_value))
                    data["currency"] = "INR"
                    flow["step"] = "description"
                    prompt = "Send product description:"
                except InvalidOperation:
                    await message.answer("Invalid price. Send a valid number (e.g., 499):")
                    return
            elif step == "description":
                data["description"] = text_value
                flow["step"] = "url"
                prompt = "Send product delivery URL (https://...):"
            elif step == "url":
                data["url"] = text_value
                data["color"] = "primary" # Default color
                
                await products.insert_one({**data, "active": True, "created_at": time.time()})
                await users.update_one({"_id": message.from_user.id}, {"$unset": {"admin_flow": ""}})
                await message.answer(f"{e(EMOJI_HEART, '✅')} Product added successfully!", reply_markup=main_menu_kb(message.from_user.id))
                return
                
            await users.update_one({"_id": message.from_user.id}, {"$set": {"admin_flow": flow}})
            await message.answer(prompt)
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
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home", style="primary")],
    ])
    await call.message.answer(f"{e(EMOJI_TEXT, '⚙️')} <b>Admin Control Panel</b>", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await users.update_one(
        {"_id": call.from_user.id}, 
        {"$set": {"admin_flow": {"action": "add", "step": "name", "data": {}}}}, 
        upsert=True
    )
    await call.message.answer("Send new product name:")
    await call.answer()

# --- Main ---

async def main():
    if not config.BOT_TOKEN or not config.MONGO_URI:
        raise RuntimeError("Set BOT_TOKEN and MONGO_URI in environment variables.")
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Premium Store Bot started. Admin must add products.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
