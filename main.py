import asyncio
import logging
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
buttons_collection = db.custom_buttons
users = db.users
transactions = db.transactions

ADMIN_IDS = set(config.ADMIN_IDS)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def money(amount) -> str:
    return f"{Decimal(str(amount)):.2f}"

def home_kb(user_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="🎯 Tier 1 Link", callback_data="btn_slot_1")
    b.button(text="🚀 Tier 2 Link", callback_data="btn_slot_2")
    b.button(text="💎 Tier 3 Link", callback_data="btn_slot_3")
    b.button(text="🔥 Tier 4 Link", callback_data="btn_slot_4")
    b.button(text="👑 Tier 5 Link", callback_data="btn_slot_5")
    b.button(text="💰 My Wallet", callback_data="wallet")
    b.button(text="➕ Top-up Wallet (FamPay)", callback_data="topup")
    if is_admin(user_id):
        b.button(text="⚙️ Admin Panel", callback_data="admin")
    b.adjust(2, 2, 1, 1, 1 if not is_admin(user_id) else 2)
    return b.as_markup()

def back_home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home")]
    ])

def qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

@dp.message(CommandStart())
async def start(message: Message):
    await users.update_one(
        {"_id": message.from_user.id}, 
        {"$setOnInsert": {"balance": Decimal("0.00")}, "$set": {"name": message.from_user.full_name, "username": message.from_user.username}}, 
        upsert=True
    )
    await message.answer(
        "✨ <b>Welcome to the Exclusive Portal</b>\n\nChoose an option below:",
        reply_markup=home_kb(message.from_user.id),
    )

@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    await call.message.edit_text("✨ <b>Main Menu</b>\n\nChoose an option below:", reply_markup=home_kb(call.from_user.id))
    await call.answer()

@dp.callback_query(F.data == "wallet")
async def view_wallet(call: CallbackQuery):
    user = await users.find_one({"_id": call.from_user.id})
    balance = user.get("balance", Decimal("0.00")) if user else Decimal("0.00")
    await call.message.edit_text(
        f"💰 <b>Your Wallet Balance</b>\n\nBalance: <b>₹{money(balance)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Top-up Balance", callback_data="topup")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home")]
        ])
    )
    await call.answer()

@dp.callback_query(F.data == "topup")
async def topup_prompt(call: CallbackQuery):
    await users.update_one({"_id": call.from_user.id}, {"$set": {"awaiting_topup": True}})
    await call.message.edit_text(
        "💳 <b>Wallet Top-up via FamPay / UPI</b>\n\n"
        "Please send the amount you wish to add (e.g. <code>100</code>):",
        reply_markup=back_home_kb()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("btn_slot_"))
async def handle_custom_button(call: CallbackQuery):
    slot_id = call.data
    btn_data = await buttons_collection.find_one({"slot_id": slot_id})
    
    if not btn_data or not btn_data.get("active"):
        await call.answer("⚠️ This button has not been configured by the admin yet.", show_alert=True)
        return

    price = btn_data["price"]
    title = btn_data["title"]
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Pay Now (₹{price})", callback_data=f"pay_direct:{slot_id}")],
        [InlineKeyboardButton(text=f"🪙 Deduct from Wallet", callback_data=f"pay_wallet:{slot_id}")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="home")]
    ])
    
    await call.message.edit_text(
        f"🔥 <b>{title}</b>\n\nPrice: <b>₹{money(price)}</b>\n\nChoose your payment method:",
        reply_markup=kb
    )
    await call.answer()

@dp.callback_query(F.data.startswith("pay_wallet:"))
async def pay_with_wallet(call: CallbackQuery):
    slot_id = call.data.split(":", 1)[1]
    btn_data = await buttons_collection.find_one({"slot_id": slot_id})
    if not btn_data:
        await call.answer("Item not found.", show_alert=True)
        return

    user = await users.find_one({"_id": call.from_user.id})
    balance = Decimal(str(user.get("balance", "0.00")))
    price = Decimal(str(btn_data["price"]))

    if balance < price:
        await call.answer("❌ Insufficient wallet balance! Please top-up first.", show_alert=True)
        return

    # Deduct balance
    new_balance = balance - price
    await users.update_one({"_id": call.from_user.id}, {"$set": {"balance": new_balance}})

    await call.message.edit_text(
        f"✅ <b>Payment Successful!</b>\n\n"
        f"Item: <b>{btn_data['title']}</b>\n"
        f"Your Links:\n<code>{btn_data['links']}</code>",
        reply_markup=back_home_kb()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("pay_direct:"))
async def pay_direct(call: CallbackQuery):
    slot_id = call.data.split(":", 1)[1]
    btn_data = await buttons_collection.find_one({"slot_id": slot_id})
    if not btn_data:
        await call.answer("Item not found.", show_alert=True)
        return

    price = btn_data["price"]
    fampay_id = getattr(config, "FAMPAY_UPI_ID", "fampay@upi") # Configure UPI ID in config.py
    upi_uri = f"upi://pay?pa={fampay_id}&pn=StoreBot&am={price}&cu=INR"
    
    photo = BufferedInputFile(qr_png(upi_uri), filename="fampay-qr.png")
    await call.message.answer_photo(
        photo,
        caption=f"📱 <b>FamPay / UPI QR Code</b>\n\n"
                f"Scan and pay <b>₹{price}</b> using FamPay or any UPI app.\n"
                f"After payment, click the button below to verify.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ I Have Paid", callback_data=f"verify_direct:{slot_id}")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home")]
        ])
    )
    await call.answer()

@dp.callback_query(F.data.startswith("verify_direct:"))
async def verify_direct(call: CallbackQuery):
    slot_id = call.data.split(":", 1)[1]
    btn_data = await buttons_collection.find_one({"slot_id": slot_id})
    
    # Auto-grant access for demonstration or simulate manual verification queue
    await call.message.answer(
        f"✅ <b>Access Granted!</b>\n\n"
        f"Here are your links for <b>{btn_data['title']}</b>:\n"
        f"<code>{btn_data['links']}</code>",
        reply_markup=back_home_kb()
    )
    await call.answer()

@dp.message(F.text)
async def message_router(message: Message):
    user = await users.find_one({"_id": message.from_user.id})
    
    # Handle Top-up Amount Input
    if user and user.get("awaiting_topup"):
        try:
            amount = Decimal(message.text.strip())
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            await message.answer("❌ Invalid amount. Please enter a valid number:")
            return

        await users.update_one({"_id": message.from_user.id}, {"$unset": {"awaiting_topup": ""}})
        fampay_id = getattr(config, "FAMPAY_UPI_ID", "fampay@upi")
        upi_uri = f"upi://pay?pa={fampay_id}&pn=WalletTopup&am={amount}&cu=INR"
        
        photo = BufferedInputFile(qr_png(upi_uri), filename="topup-qr.png")
        await message.answer_photo(
            photo,
            caption=f"💳 <b>Top-up QR Code (FamPay / UPI)</b>\n\n"
                    f"Scan to pay <b>₹{money(amount)}</b>.\n"
                    f"Send your UTR / Screenshot or contact Admin to credit balance.",
            reply_markup=back_home_kb()
        )
        return

    # Handle Admin Button Setup Flow
    if is_admin(message.from_user.id) and user and user.get("admin_flow"):
        flow = user["admin_flow"]
        step = flow["step"]
        slot_id = flow["slot_id"]
        text_val = message.text.strip()

        if step == "title":
            flow["title"] = text_val
            flow["step"] = "price"
            await users.update_one({"_id": message.from_user.id}, {"$set": {"admin_flow": flow}})
            await message.answer("Send the price for this button (e.g. 49):")
        elif step == "price":
            try:
                price = Decimal(text_val)
            except InvalidOperation:
                await message.answer("Invalid price. Enter a number:")
                return
            flow["price"] = str(price)
            flow["step"] = "links"
            await users.update_one({"_id": message.from_user.id}, {"$set": {"admin_flow": flow}})
            await message.answer("Send the links/content to deliver upon purchase:")
        elif step == "links":
            title = flow["title"]
            price = flow["price"]
            links = text_val

            style_map = {
                "btn_slot_1": "success", # Green
                "btn_slot_2": "danger",  # Red
                "btn_slot_3": "primary", # Blue
                "btn_slot_4": "success", # Green
                "btn_slot_5": "danger"   # Red
            }

            await buttons_collection.update_one(
                {"slot_id": slot_id},
                {"$set": {
                    "slot_id": slot_id,
                    "title": title,
                    "price": price,
                    "links": links,
                    "color": style_map.get(slot_id, "primary"),
                    "active": True
                }},
                upsert=True
            )
            await users.update_one({"_id": message.from_user.id}, {"$unset": {"admin_flow": ""}})
            await message.answer(f"✅ Button <b>{slot_id}</b> successfully configured!", reply_markup=home_kb(message.from_user.id))
        return

    await message.answer("Use /start to open the menu.")

@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Configure Button 1", callback_data="aset:btn_slot_1")],
        [InlineKeyboardButton(text="🔴 Configure Button 2", callback_data="aset:btn_slot_2")],
        [InlineKeyboardButton(text="🔵 Configure Button 3", callback_data="aset:btn_slot_3")],
        [InlineKeyboardButton(text="🟢 Configure Button 4", callback_data="aset:btn_slot_4")],
        [InlineKeyboardButton(text="🔴 Configure Button 5", callback_data="aset:btn_slot_5")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="home")],
    ])
    await call.message.edit_text("⚙️ <b>Admin Panel - Button Management</b>", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("aset:"))
async def admin_set_button(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    slot_id = call.data.split(":", 1)[1]
    await users.update_one(
        {"_id": call.from_user.id}, 
        {"$set": {"admin_flow": {"step": "title", "slot_id": slot_id}} }, 
        upsert=True
    )
    await call.message.edit_text(f"📝 Configuring <b>{slot_id}</b>\n\nSend the display title for this button:", reply_markup=back_home_kb())
    await call.answer()

async def main():
    if not config.BOT_TOKEN or not config.MONGO_URI:
        raise RuntimeError("Set BOT_TOKEN and MONGO_URI as environment variables.")
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Bot started successfully.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
