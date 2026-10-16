"""Environment-based configuration. Set these as Heroku Config Vars."""
import os

def _csv_ints(value: str):
    result = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            try:
                result.add(int(item))
            except ValueError:
                raise RuntimeError("ADMIN_IDS must be comma-separated Telegram numeric user IDs.")
    return result

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = _csv_ints(os.getenv("ADMIN_IDS", ""))
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "telegram_selling_bot").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()

# Configure only after obtaining authorized credentials and the provider's API specification.
PAYMENT_API_URL = os.getenv("PAYMENT_API_URL", "").strip()       # Create checkout/payment endpoint
PAYMENT_STATUS_URL = os.getenv("PAYMENT_STATUS_URL", "").strip() # Status endpoint base; order ID appended
PAYMENT_API_KEY = os.getenv("PAYMENT_API_KEY", "").strip()
PAYMENT_WEBHOOK_URL = os.getenv("PAYMENT_WEBHOOK_URL", "").strip()
PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "INR").strip().upper()
