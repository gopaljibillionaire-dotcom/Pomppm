import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
MONGO_URI = os.getenv("MONGO_URI")
OXAPAY_MERCHANT_API_KEY = os.getenv("OXAPAY_MERCHANT_API_KEY")
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY")
BASE_URL = os.getenv("BASE_URL", "https://your-app.herokuapp.com")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "default_webhook_secret")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN or not MONGO_URI or not SESSION_ENCRYPTION_KEY:
    raise ValueError("Missing critical environment variables (BOT_TOKEN, MONGO_URI, SESSION_ENCRYPTION_KEY).")
