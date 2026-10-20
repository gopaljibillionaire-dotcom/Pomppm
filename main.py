import asyncio
import logging
import base64
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, 
    WebAppInfo
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from telethon import TelegramClient
from telethon.sessions import StringSession
from cryptography.fernet import Fernet
from motor.motor_asyncio import AsyncIOMotorClient
import aiohttp
from aiohttp import web

from config import (
    BOT_TOKEN, ADMIN_ID, API_ID, API_HASH, MONGO_URI,
    OXAPAY_MERCHANT_API_KEY, SESSION_ENCRYPTION_KEY, BASE_URL,
    WEBHOOK_SECRET, PORT
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Encryption & Security Setup ---
def get_fernet() -> Fernet:
    key = SESSION_ENCRYPTION_KEY.encode()
    if len(key) < 32:
        key = base64.urlsafe_b64encode(key.ljust(32, b'0')[:32])
    return Fernet(key)

fernet = get_fernet()

# --- Database Setup ---
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.get_database("telegram_ads_bot")

users_col = db.users
accounts_col = db.accounts
groups_col = db.groups
campaigns_col = db.campaigns
subscriptions_col = db.subscriptions
payments_col = db.payments
campaign_logs_col = db.campaign_logs
admin_audit_logs_col = db.admin_audit_logs

async def setup_indexes():
    await users_col.create_index("user_id", unique=True)
    await accounts_col.create_index("user_id")
    await accounts_col.create_index([("user_id", 1), ("telegram_account_id", 1)], unique=True)
    await groups_col.create_index([("user_id", 1), ("group_id", 1)], unique=True)
    await campaigns_col.create_index([("user_id", 1), ("group_id", 1)], unique=True)
    await subscriptions_col.create_index("user_id")
    await payments_col.create_index("invoice_id", unique=True)

# --- Bot & Dispatcher ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# --- Subscription Plans & Rules ---
PLANS = {
    "basic": {
        "name": "🥉 BASIC",
        "price": 3.0,
        "accounts_limit": 1,
        "groups_limit": 1,
        "intervals": [10800, 14400], # 3h, 4h in seconds
        "stars_required": False
    },
    "advance": {
        "name": "🥈 ADVANCE",
        "price": 7.0,
        "accounts_limit": 1,
        "groups_limit": 5,
        "intervals": [3000, 3600, 7200, 14400], # 50m, 1h, 2h, 4h
        "stars_required": False
    },
    "ultra": {
        "name": "💎 ⚡ ULTRA",
        "price": 10.0,
        "accounts_limit": 1,
        "groups_limit": 10,
        "intervals": [10, 600, 1200, 1800, 3600, 7200, 14400, 21600], # 10s to 6h
        "stars_required": True
    }
}

# --- FSM States ---
class AuthState(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()

class GroupState(StatesGroup):
    waiting_for_group_id = State()
    waiting_for_stars_choice = State()

class CampaignState(StatesGroup):
    waiting_for_message = State()
    waiting_for_delay = State()

# --- Helper Functions ---
async def get_user_active_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    sub = await subscriptions_col.find_one({"user_id": user_id, "status": "active"})
    if not sub:
        return None
    if sub["expires_at"] < datetime.now(timezone.utc):
        await subscriptions_col.update_one({"_id": sub["_id"]}, {"$set": {"status": "expired"}})
        await campaigns_col.update_many({"user_id": user_id}, {"$set": {"enabled": False, "status": "paused"}})
        return None
    return sub

async def get_user_plan_name(user_id: int) -> str:
    sub = await get_user_active_subscription(user_id)
    if not sub:
        return "Free / Expired"
    return PLANS.get(sub["plan"], {}).get("name", "Unknown")

# --- Keyboards ---
def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="➕ Add Account", callback_data="add_account"),
            InlineKeyboardButton(text="👤 My Accounts", callback_data="my_accounts")
        ],
        [
            InlineKeyboardButton(text="➕ Add Group", callback_data="add_group"),
            InlineKeyboardButton(text="👥 My Groups", callback_data="my_groups")
        ],
        [
            InlineKeyboardButton(text="📢 My Campaigns", callback_data="my_campaigns"),
            InlineKeyboardButton(text="⏱️ Ad Controls", callback_data="ad_controls")
        ],
        [
            InlineKeyboardButton(text="💎 Subscription Plans", callback_data="plans"),
            InlineKeyboardButton(text="🧾 My Subscription", callback_data="my_subscription")
        ],
        [
            InlineKeyboardButton(text="💳 Payment History", callback_data="payment_history"),
            InlineKeyboardButton(text="⚙️ Settings", callback_data="settings")
        ],
        [
            InlineKeyboardButton(text="🆘 Support", callback_data="support"),
            InlineKeyboardButton(text="🔙 Back", callback_data="back_home")
        ]
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="🛡️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_kb(callback_data: str = "back_home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data=callback_data)]
    ])

# --- Handlers: Start & Main Menu ---
@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or "No Username"
    
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"username": username, "last_active": datetime.now(timezone.utc)},
         "$setOnInsert": {"registered_at": datetime.now(timezone.utc)}},
        upsert=True
    )
    
    plan_name = await get_user_plan_name(user_id)
    is_admin = (user_id == ADMIN_ID)
    
    text = (
        f"👋 **Welcome to Advanced Telegram Ads Manager Bot!**\n\n"
        f"Manage your accounts, automated advertising campaigns, groups, and subscriptions effortlessly.\n\n"
        f"🧾 **Current Subscription:** {plan_name}\n\n"
        f"Select an option below to get started:"
    )
    await message.answer(text, reply_markup=main_menu_kb(is_admin), parse_mode="Markdown")

@router.callback_query(F.data == "back_home")
async def cb_back_home(callback: CallbackQuery):
    user_id = callback.from_user.id
    plan_name = await get_user_plan_name(user_id)
    is_admin = (user_id == ADMIN_ID)
    text = (
        f"🏠 **Main Menu**\n\n"
        f"🧾 **Current Subscription:** {plan_name}\n\n"
        f"Select an option below:"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb(is_admin), parse_mode="Markdown")
    await callback.answer()

# --- Account Management Flow (Telethon) ---
@router.callback_query(F.data == "add_account")
async def cb_add_account(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    sub = await get_user_active_subscription(user_id)
    if not sub:
        await callback.message.edit_text(
            "❌ **Active Subscription Required**\n\nYou need an active subscription plan to connect a Telegram account.",
            reply_markup=back_kb()
        )
        await callback.answer()
        return

    acc_count = await accounts_col.count_documents({"user_id": user_id})
    max_accs = PLANS[sub["plan"]]["accounts_limit"]
    if acc_count >= max_accs:
        await callback.message.edit_text(
            f"❌ **Account Limit Reached**\n\nYour current plan ({PLANS[sub['plan']]['name']}) allows up to {max_accs} connected account(s).",
            reply_markup=back_kb()
        )
        await callback.answer()
        return

    await state.set_state(AuthState.waiting_for_phone)
    await callback.message.edit_text(
        "👤 **Add Telegram Account**\n\nPlease send your phone number in international format (e.g., `+1234567890`):",
        reply_markup=back_kb()
    )
    await callback.answer()

@router.message(AuthState.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    user_id = message.from_user.id
    
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash, session_string=client.session.save())
        await client.disconnect()
        
        await state.set_state(AuthState.waiting_for_code)
        await message.answer("📲 **Enter Code**\n\nA login code has been sent via Telegram. Please send the code here (e.g., `12345`):", reply_markup=back_kb())
    except Exception as e:
        logger.error(f"Telethon phone error: {e}")
        await message.answer(f"❌ **Error sending code:** {e}\n\nPlease try again.", reply_markup=back_kb())
        await state.clear()

@router.message(AuthState.waiting_for_code)
async def process_code(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")
    user_id = message.from_user.id
    
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except Exception as sign_in_err:
            if "Password" in str(sign_in_err) or "SessionPasswordNeededError" in str(sign_in_err):
                await state.update_data(temp_session=client.session.save())
                await client.disconnect()
                await state.set_state(AuthState.waiting_for_2fa)
                await message.answer("🔐 **Two-Step Verification**\n\nYour account has 2FA enabled. Please send your cloud password:", reply_markup=back_kb())
                return
            raise sign_in_err

        me = await client.get_me()
        session_str = client.session.save()
        await client.disconnect()
        
        encrypted_session = fernet.encrypt(session_str.encode()).decode()
        
        await accounts_col.update_one(
            {"user_id": user_id, "telegram_account_id": me.id},
            {
                "$set": {
                    "username": me.username or str(me.id),
                    "encrypted_session": encrypted_session,
                    "status": "connected",
                    "connected_at": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
        
        await state.clear()
        await message.answer(
            f"✅ **Account Connected Successfully!**\n\n"
            f"👤 Username: @{me.username or 'N/A'}\n"
            f"🆔 ID: `{me.id}`",
            reply_markup=main_menu_kb(user_id == ADMIN_ID),
            parse_mode="Markdown"
        )
        
        # Notify Admin with metadata only (no raw sessions)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔔 **New Account Connected**\n\n"
                f"👤 Bot User ID: `{user_id}`\n"
                f"📱 Telegram Username: @{me.username or 'N/A'}\n"
                f"🆔 Telegram ID: `{me.id}`\n"
                f"⏱️ Time: `{datetime.now(timezone.utc)}`",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Telethon sign_in error: {e}")
        await message.answer(f"❌ **Authentication Failed:** {e}", reply_markup=back_kb())
        await state.clear()

@router.message(AuthState.waiting_for_2fa)
async def process_2fa(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    user_id = message.from_user.id
    temp_session = data.get("temp_session")
    
    client = TelegramClient(StringSession(temp_session), API_ID, API_HASH)
    try:
        await client.connect()
        await client.sign_in(password=password)
        me = await client.get_me()
        session_str = client.session.save()
        await client.disconnect()
        
        encrypted_session = fernet.encrypt(session_str.encode()).decode()
        
        await accounts_col.update_one(
            {"user_id": user_id, "telegram_account_id": me.id},
            {
                "$set": {
                    "username": me.username or str(me.id),
                    "encrypted_session": encrypted_session,
                    "status": "connected",
                    "connected_at": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
        
        await state.clear()
        await message.answer(
            f"✅ **Account Connected Successfully via 2FA!**\n\n"
            f"👤 Username: @{me.username or 'N/A'}\n"
            f"🆔 ID: `{me.id}`",
            reply_markup=main_menu_kb(user_id == ADMIN_ID),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"2FA error: {e}")
        await message.answer(f"❌ **2FA Verification Failed:** {e}", reply_markup=back_kb())
        await state.clear()

@router.callback_query(F.data == "my_accounts")
async def cb_my_accounts(callback: CallbackQuery):
    user_id = callback.from_user.id
    accounts = await accounts_col.find({"user_id": user_id}).to_list(length=10)
    
    if not accounts:
        await callback.message.edit_text(
            "👤 **My Accounts**\n\nYou have no connected Telegram accounts.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Add Account", callback_data="add_account")],
                [InlineKeyboardButton(text="🔙 Back", callback_data="back_home")]
            ])
        )
        await callback.answer()
        return
    
    text = "👤 **My Connected Accounts:**\n\n"
    keyboard = []
    for acc in accounts:
        text += f"• @{acc['username']} (`{acc['telegram_account_id']}`) - Status: {acc['status']}\n"
        keyboard.append([InlineKeyboardButton(text=f"🗑️ Disconnect @{acc['username']}", callback_data=f"disc_acc_{acc['telegram_account_id']}")])
    
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="back_home")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("disc_acc_"))
async def cb_disconnect_account(callback: CallbackQuery):
    user_id = callback.from_user.id
    acc_id = int(callback.data.split("_")[2])
    
    await accounts_col.delete_one({"user_id": user_id, "telegram_account_id": acc_id})
    await callback.answer("✅ Account disconnected successfully.")
    await cb_my_accounts(callback)

# --- Subscription Plans & Payment Flow (OxaPay) ---
@router.callback_query(F.data == "plans")
async def cb_plans(callback: CallbackQuery):
    text = (
        "💎 **Subscription Plans (30 Days)**\n\n"
        "🥉 **BASIC — $3/month**\n"
        "• 1 Telegram Account\n"
        "• 1 Advertising Group\n"
        "• Intervals: 3h, 4h\n\n"
        "🥈 **ADVANCE — $7/month**\n"
        "• 1 Telegram Account\n"
        "• Up to 5 Advertising Groups\n"
        "• Intervals: 50m, 1h, 2h, 4h\n\n"
        "💎 ⚡ **ULTRA — $10/month**\n"
        "• 1 Telegram Account\n"
        "• Up to 10 Advertising Groups\n"
        "• Intervals: 10s to 6h\n"
        "• Optional Stars-required posting\n\n"
        "Select a plan below to subscribe:"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🥉 Buy Basic ($3)", callback_data="buy_basic"),
            InlineKeyboardButton(text="🥈 Buy Advance ($7)", callback_data="buy_advance")
        ],
        [
            InlineKeyboardButton(text="💎 ⚡ Buy Ultra ($10)", callback_data="buy_ultra")
        ],
        [
            InlineKeyboardButton(text="🔙 Back", callback_data="back_home")
        ]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

async def create_oxapay_invoice(user_id: int, plan_key: str) -> Optional[Dict[str, Any]]:
    plan = PLANS[plan_key]
    invoice_id = f"SUB-{user_id}-{int(datetime.now().timestamp())}"
    
    payload = {
        "merchant": OXAPAY_MERCHANT_API_KEY,
        "amount": plan["price"],
        "currency": "USD",
        "order_id": invoice_id,
        "desc": f"Subscription for {plan['name']}",
        "callback_url": f"{BASE_URL}/oxapay-webhook",
        "return_url": f"https://t.me/{(await bot.get_me()).username}"
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://api.oxapay.com/merchants/request", json=payload) as resp:
                data = await resp.json()
                if data.get("result") == 100:
                    return {
                        "invoice_id": invoice_id,
                        "pay_link": data.get("pay_link"),
                        "track_id": data.get("track_id")
                    }
        except Exception as e:
            logger.error(f"OxaPay API error: {e}")
    return None

@router.callback_query(F.data.in_({"buy_basic", "buy_advance", "buy_ultra"}))
async def cb_buy_plan(callback: CallbackQuery):
    user_id = callback.from_user.id
    plan_key = callback.data.split("_")[1]
    
    if not OXAPAY_MERCHANT_API_KEY:
        await callback.answer("❌ Payment gateway is not configured.", show_alert=True)
        return
        
    await callback.message.edit_text("⏳ Generating secure payment invoice...")
    inv = await create_oxapay_invoice(user_id, plan_key)
    
    if not inv:
        await callback.message.edit_text(
            "❌ **Payment Error**\n\nFailed to create payment invoice. Please try again later.",
            reply_markup=back_kb("plans")
        )
        return
        
    await payments_col.insert_one({
        "invoice_id": inv["invoice_id"],
        "track_id": inv.get("track_id"),
        "user_id": user_id,
        "plan": plan_key,
        "amount": PLANS[plan_key]["price"],
        "currency": "USD",
        "status": "pending",
        "created_at": datetime.now(timezone.utc)
    })
    
    text = (
        f"💳 **Payment Invoice Created**\n\n"
        f"Plan: {PLANS[plan_key]['name']}\n"
        f"Amount: ${PLANS[plan_key]['price']} USD\n\n"
        f"Click the button below to pay securely via OxaPay. Once confirmed, your subscription will activate automatically."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Pay Now", url=inv["pay_link"])],
        [InlineKeyboardButton(text="🔙 Back to Plans", callback_data="plans")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "my_subscription")
async def cb_my_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    sub = await get_user_active_subscription(user_id)
    
    if not sub:
        text = "🧾 **My Subscription**\n\nYou currently have no active subscription."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Plans", callback_data="plans")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="back_home")]
        ])
    else:
        plan = PLANS[sub["plan"]]
        expires = sub["expires_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
        text = (
            f"🧾 **My Subscription**\n\n"
            f"Plan: {plan['name']}\n"
            f"Status: Active ✅\n"
            f"Expires At: {expires}\n"
            f"Accounts Limit: {plan['accounts_limit']}\n"
            f"Groups Limit: {plan['groups_limit']}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Upgrade / Renew", callback_data="plans")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="back_home")]
        ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "payment_history")
async def cb_payment_history(callback: CallbackQuery):
    user_id = callback.from_user.id
    payments = await payments_col.find({"user_id": user_id}).sort("created_at", -1).to_list(length=10)
    
    if not payments:
        await callback.message.edit_text(
            "💳 **Payment History**\n\nNo payments found.",
            reply_markup=back_kb()
        )
        await callback.answer()
        return
        
    text = "💳 **Recent Payments:**\n\nfor p in payments:\n"
    for p in payments:
        dt = p["created_at"].strftime("%Y-%m-%d %H:%M")
        text += f"• `{p['invoice_id']}` | ${p['amount']} | {p['status'].upper()} | {dt}\n"
        
    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode="Markdown")
    await callback.answer()

# --- Group Management & Advertising Setup ---
@router.callback_query(F.data == "add_group")
async def cb_add_group(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    sub = await get_user_active_subscription(user_id)
    if not sub:
        await callback.message.edit_text("❌ Active subscription required.", reply_markup=back_kb())
        await callback.answer()
        return
        
    acc = await accounts_col.find_one({"user_id": user_id, "status": "connected"})
    if not acc:
        await callback.message.edit_text("❌ Please connect a Telegram account first.", reply_markup=back_kb())
        await callback.answer()
        return
        
    group_count = await groups_col.count_documents({"user_id": user_id})
    if group_count >= PLANS[sub["plan"]]["groups_limit"]:
        await callback.message.edit_text(f"❌ Group limit reached for your plan.", reply_markup=back_kb())
        await callback.answer()
        return
        
    await state.set_state(GroupState.waiting_for_group_id)
    await callback.message.edit_text(
        "👥 **Add Advertising Group**\n\nPlease send the Group Username (e.g., `@mygroup`) or Chat ID:",
        reply_markup=back_kb()
    )
    await callback.answer()

@router.message(GroupState.waiting_for_group_id)
async def process_group_id(message: Message, state: FSMContext):
    group_input = message.text.strip()
    user_id = message.from_user.id
    
    acc = await accounts_col.find_one({"user_id": user_id, "status": "connected"})
    if not acc:
        await message.answer("❌ No connected account found.", reply_markup=back_kb())
        await state.clear()
        return
        
    session_str = fernet.decrypt(acc["encrypted_session"].encode()).decode()
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    
    try:
        await client.connect()
        entity = await client.get_entity(group_input)
        group_id = entity.id
        group_name = getattr(entity, "title", str(group_id))
        
        # Verify access & permissions
        permissions = await client.permissions(entity, me=await client.get_me())
        await client.disconnect()
        
        await state.update_data(group_id=group_id, group_name=group_name, account_id=acc["telegram_account_id"])
        
        sub = await get_user_active_subscription(user_id)
        if sub and sub["plan"] == "ultra":
            await state.set_state(GroupState.waiting_for_stars_choice)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Yes (Stars Required)", callback_data="stars_yes"),
                    InlineKeyboardButton(text="No (Standard)", callback_data="stars_no")
                ],
                [InlineKeyboardButton(text="🔙 Back", callback_data="back_home")]
            ])
            await message.answer("💎 **Telegram Stars Requirement**\n\nDoes this group require Telegram Stars for posting?", reply_markup=kb)
            return
        else:
            await save_group_and_prompt_campaign(message, state, stars_required=False)
            
    except Exception as e:
        logger.error(f"Group validation error: {e}")
        await message.answer(f"❌ **Error validating group:** {e}\n\nMake sure your connected account is a member and has posting permissions.", reply_markup=back_kb())
        await state.clear()

@router.callback_query(F.data.in_({"stars_yes", "stars_no"}))
async def cb_stars_choice(callback: CallbackQuery, state: FSMContext):
    stars_required = (callback.data == "stars_yes")
    await save_group_and_prompt_campaign(callback.message, state, stars_required=stars_required)
    await callback.answer()

async def save_group_and_prompt_campaign(message: Message, state: FSMContext, stars_required: bool):
    data = await state.get_data()
    user_id = message.chat.id
    
    await groups_col.update_one(
        {"user_id": user_id, "group_id": data["group_id"]},
        {
            "$set": {
                "group_name": data["group_name"],
                "account_id": data["account_id"],
                "stars_required": stars_required,
                "created_at": datetime.now(timezone.utc)
            }
        },
        upsert=True
    )
    
    await state.set_state(CampaignState.waiting_for_message)
    await message.edit_text(
        f"✅ **Group Added Successfully!**\n\n"
        f"Group: {data['group_name']}\n\n"
        f"Now, please send the advertising message you want to publish in this group:",
        reply_markup=back_kb()
    )

@router.message(CampaignState.waiting_for_message)
async def process_campaign_message(message: Message, state: FSMContext):
    text_content = message.text or message.caption or ""
    if not text_content:
        await message.answer("❌ Message cannot be empty. Please send text.", reply_markup=back_kb())
        return
        
    await state.update_data(message_content=text_content)
    
    user_id = message.from_user.id
    sub = await get_user_active_subscription(user_id)
    plan_key = sub["plan"] if sub else "basic"
    intervals = PLANS[plan_key]["intervals"]
    
    kb_rows = []
    for interval in intervals:
        label = f"{interval // 60} minutes" if interval >= 60 else f"{interval} seconds"
        if interval >= 3600:
            label = f"{interval // 3600} hour(s)"
        kb_rows.append([InlineKeyboardButton(text=f"⏱️ {label}", callback_data=f"delay_{interval}")])
    kb_rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="back_home")])
    
    await state.set_state(CampaignState.waiting_for_delay)
    await message.answer(
        "⏱️ **Select Advertising Interval**\n\nChoose how often your message should be published:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )

@router.callback_query(F.data.startswith("delay_"))
async def cb_select_delay(callback: CallbackQuery, state: FSMContext):
    delay = int(callback.data.split("_")[1])
    data = await state.get_data()
    user_id = callback.from_user.id
    
    groups = await groups_col.find({"user_id": user_id}).to_list(length=10)
    target_group = groups[-1] if groups else None
    
    if not target_group:
        await callback.message.edit_text("❌ No group found.", reply_markup=back_kb())
        await state.clear()
        return
        
    await campaigns_col.update_one(
        {"user_id": user_id, "group_id": target_group["group_id"]},
        {
            "$set": {
                "message_content": data["message_content"],
                "delay": delay,
                "enabled": True,
                "status": "active",
                "next_execution": datetime.now(timezone.utc) + timedelta(seconds=delay),
                "last_success": None
            }
        },
        upsert=True
    )
    
    await state.clear()
    await callback.message.edit_text(
        "✅ **Campaign Created & Started Successfully!**\n\n"
        f"Group: {target_group['group_name']}\n"
        f"Interval: {delay} seconds\n"
        f"Status: Active 🚀",
        reply_markup=main_menu_kb(user_id == ADMIN_ID),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "my_groups")
async def cb_my_groups(callback: CallbackQuery):
    user_id = callback.from_user.id
    groups = await groups_col.find({"user_id": user_id}).to_list(length=20)
    
    if not groups:
        await callback.message.edit_text("👥 **My Groups**\n\nNo groups registered.", reply_markup=back_kb())
        await callback.answer()
        return
        
    text = "👥 **Registered Groups & Campaigns:**\n\n"
    keyboard = []
    for g in groups:
        camp = await campaigns_col.find_one({"user_id": user_id, "group_id": g["group_id"]})
        status = "Active 🚀" if camp and camp.get("enabled") else "Paused ⏸️"
        text += f"• **{g['group_name']}** | Status: {status}\n"
        keyboard.append([InlineKeyboardButton(text=f"⚙️ Manage {g['group_name'][:15]}", callback_data=f"manage_grp_{g['group_id']}")])
        
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="back_home")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("manage_grp_"))
async def cb_manage_group(callback: CallbackQuery):
    user_id = callback.from_user.id
    group_id = int(callback.data.split("_")[2])
    
    g = await groups_col.find_one({"user_id": user_id, "group_id": group_id})
    camp = await campaigns_col.find_one({"user_id": user_id, "group_id": group_id})
    
    if not g:
        await callback.answer("Group not found.", show_alert=True)
        return
        
    text = (
        f"⚙️ **Group Management:** {g['group_name']}\n\n"
        f"🆔 ID: `{group_id}`\n"
        f"⭐ Stars Required: {g.get('stars_required', False)}\n"
        f"📊 Campaign Status: {camp.get('status', 'inactive') if camp else 'Not Created'}\n"
        f"⏱️ Delay: {camp.get('delay', 'N/A')}s\n"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="▶️ Resume", callback_data=f"camp_res_{group_id}"),
            InlineKeyboardButton(text="⏸️ Pause", callback_data=f"camp_pau_{group_id}")
        ],
        [
            InlineKeyboardButton(text="🛑 Stop", callback_data=f"camp_stop_{group_id}"),
            InlineKeyboardButton(text="🗑️ Remove Group", callback_data=f"grp_rem_{group_id}")
        ],
        [InlineKeyboardButton(text="🔙 Back to Groups", callback_data="my_groups")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("camp_"))
async def cb_campaign_controls(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split("_")
    action = parts[1]
    group_id = int(parts[2])
    
    if action == "res":
        await campaigns_col.update_one({"user_id": user_id, "group_id": group_id}, {"$set": {"enabled": True, "status": "active"}})
        await callback.answer("▶️ Campaign resumed.")
    elif action == "pau":
        await campaigns_col.update_one({"user_id": user_id, "group_id": group_id}, {"$set": {"enabled": False, "status": "paused"}})
        await callback.answer("⏸️ Campaign paused.")
    elif action == "stop":
        await campaigns_col.update_one({"user_id": user_id, "group_id": group_id}, {"$set": {"enabled": False, "status": "stopped"}})
        await callback.answer("🛑 Campaign stopped.")
        
    await cb_manage_group(callback)

@router.callback_query(F.data.startswith("grp_rem_"))
async def cb_remove_group(callback: CallbackQuery):
    user_id = callback.from_user.id
    group_id = int(callback.data.split("_")[2])
    
    await groups_col.delete_one({"user_id": user_id, "group_id": group_id})
    await campaigns_col.delete_one({"user_id": user_id, "group_id": group_id})
    await callback.answer("🗑️ Group removed successfully.")
    await cb_my_groups(callback)

# --- Global Controls & Ad Controls ---
@router.callback_query(F.data == "ad_controls")
async def cb_ad_controls(callback: CallbackQuery):
    user_id = callback.from_user.id
    text = "⏱️ **Global Advertising Controls**\n\nManage all your active campaigns simultaneously:"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="▶️ Resume Eligible", callback_data="global_res_all"),
            InlineKeyboardButton(text="⏸️ Pause All Ads", callback_data="global_pau_all")
        ],
        [
            InlineKeyboardButton(text="🛑 Stop All Ads", callback_data="global_stop_all"),
            InlineKeyboardButton(text="📊 Statistics", callback_data="my_campaigns")
        ],
        [InlineKeyboardButton(text="🔙 Back", callback_data="back_home")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.in_({"global_res_all", "global_pau_all", "global_stop_all"}))
async def cb_global_controls(callback: CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.split("_")[1]
    
    if action == "res":
        sub = await get_user_active_subscription(user_id)
        if sub:
            await campaigns_col.update_many({"user_id": user_id}, {"$set": {"enabled": True, "status": "active"}})
            await callback.answer("▶️ All eligible campaigns resumed.")
        else:
            await callback.answer("❌ Active subscription required.", show_alert=True)
            return
    elif action == "pau":
        await campaigns_col.update_many({"user_id": user_id}, {"$set": {"enabled": False, "status": "paused"}})
        await callback.answer("⏸️ All campaigns paused.")
    elif action == "stop":
        await campaigns_col.update_many({"user_id": user_id}, {"$set": {"enabled": False, "status": "stopped"}})
        await callback.answer("🛑 All campaigns stopped.")
        
    await cb_ad_controls(callback)

@router.callback_query(F.data == "my_campaigns")
async def cb_my_campaigns(callback: CallbackQuery):
    user_id = callback.from_user.id
    campaigns = await campaigns_col.find({"user_id": user_id}).to_list(length=20)
    
    if not campaigns:
        await callback.message.edit_text("📢 **My Campaigns**\n\nNo campaigns found.", reply_markup=back_kb())
        await callback.answer()
        return
        
    text = "📢 **Active Campaigns & Delivery Status:**\n\n"
    for c in campaigns:
        g = await groups_col.find_one({"user_id": user_id, "group_id": c["group_id"]})
        g_name = g["group_name"] if g else str(c["group_id"])
        text += f"• **{g_name}**\n  Status: {c['status'].upper()} | Delay: {c['delay']}s\n  Last Success: {c.get('last_success', 'Never')}\n\n"
        
    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery):
    await callback.message.edit_text(
        "⚙️ **Settings**\n\nManage your bot preferences and security.",
        reply_markup=back_kb()
    )
    await callback.answer()

@router.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery):
    await callback.message.edit_text(
        "🆘 **Support & Help**\n\nFor assistance or inquiries, please contact the service administrator.",
        reply_markup=back_kb()
    )
    await callback.answer()

# --- Admin Panel ---
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await show_admin_dashboard(message)

async def show_admin_dashboard(message_or_callback):
    total_users = await users_col.count_documents({})
    active_subs = await subscriptions_col.count_documents({"status": "active"})
    expired_subs = await subscriptions_col.count_documents({"status": "expired"})
    connected_accs = await accounts_col.count_documents({})
    registered_grps = await groups_col.count_documents({})
    active_camps = await campaigns_col.count_documents({"enabled": True})
    
    payments = await payments_col.find({"status": "paid"}).to_list(length=1000)
    total_revenue = sum(p.get("amount", 0) for p in payments)
    
    text = (
        f"🛡️ **Admin Dashboard**\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🧾 Active Subscribers: {active_subs}\n"
        f"⌛ Expired Subscribers: {expired_subs}\n"
        f"👤 Connected Accounts: {connected_accs}\n"
        f"👥 Registered Groups: {registered_grps}\n"
        f"📢 Active Campaigns: {active_camps}\n"
        f"💰 Confirmed Revenue: ${total_revenue:.2f} USD\n"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="back_home")]
    ])
    
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message_or_callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Unauthorized", show_alert=True)
        return
    await show_admin_dashboard(callback)
    await callback.answer()

# --- OxaPay Webhook & Background Scheduler ---
async def oxapay_webhook_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        logger.info(f"OxaPay Webhook received: {data}")
        
        status = data.get("status")
        order_id = data.get("order_id")
        
        if status in ["Paid", "Complete"] and order_id:
            payment = await payments_col.find_one({"invoice_id": order_id, "status": "pending"})
            if payment:
                user_id = payment["user_id"]
                plan_key = payment["plan"]
                
                await payments_col.update_one(
                    {"invoice_id": order_id},
                    {"$set": {"status": "paid", "verified_at": datetime.now(timezone.utc)}}
                )
                
                expires_at = datetime.now(timezone.utc) + timedelta(days=30)
                await subscriptions_col.update_one(
                    {"user_id": user_id},
                    {
                        "$set": {
                            "plan": plan_key,
                            "status": "active",
                            "activated_at": datetime.now(timezone.utc),
                            "expires_at": expires_at
                        }
                    },
                    upsert=True
                )
                
                try:
                    await bot.send_message(
                        user_id,
                        f"✅ **Subscription Activated!**\n\nYour {PLANS[plan_key]['name']} plan is now active for 30 days until {expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                    
        return web.json_response({"status": "success"})
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=400)

async def advertising_scheduler_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            active_campaigns = await campaigns_col.find({"enabled": True, "next_execution": {"$lte": now}}).to_list(length=100)
            
            for camp in active_campaigns:
                user_id = camp["user_id"]
                sub = await get_user_active_subscription(user_id)
                if not sub:
                    await campaigns_col.update_one({"_id": camp["_id"]}, {"$set": {"enabled": False, "status": "expired"}})
                    continue
                    
                group = await groups_col.find_one({"user_id": user_id, "group_id": camp["group_id"]})
                if not group:
                    continue
                    
                acc = await accounts_col.find_one({"user_id": user_id, "telegram_account_id": group["account_id"], "status": "connected"})
                if not acc:
                    continue
                    
                try:
                    session_str = fernet.decrypt(acc["encrypted_session"].encode()).decode()
                    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
                    await client.connect()
                    
                    entity = await client.get_entity(group["group_id"])
                    await client.send_message(entity, camp["message_content"])
                    await client.disconnect()
                    
                    next_run = datetime.now(timezone.utc) + timedelta(seconds=camp["delay"])
                    await campaigns_col.update_one(
                        {"_id": camp["_id"]},
                        {
                            "$set": {
                                "next_execution": next_run,
                                "last_success": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                            }
                        }
                    )
                except Exception as e:
                    logger.error(f"Campaign execution error for group {camp['group_id']}: {e}")
                    # Apply exponential backoff or delay next execution temporarily on error
                    next_run = datetime.now(timezone.utc) + timedelta(minutes=10)
                    await campaigns_col.update_one({"_id": camp["_id"]}, {"$set": {"next_execution": next_run}})
                    
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
            
        await asyncio.sleep(10)

# --- Main Entrypoint ---
async def main():
    await setup_indexes()
    
    # Start aiohttp web server for OxaPay webhooks
    app = web.Application()
    app.router.add_post("/oxapay-webhook", oxapay_webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Aiohttp webhook server started on port {PORT}")
    
    # Start background advertising scheduler
    asyncio.create_task(advertising_scheduler_loop())
    
    logger.info("Starting Telegram Bot Polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
