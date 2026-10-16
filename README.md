# Telegram Selling Bot (Heroku + MongoDB)

## Features
- Aiogram Telegram bot, product catalog and URL delivery after payment verification
- MongoDB products, users and orders
- Admin-only product creation: name, price, currency, HTTPS delivery URL, description, button style
- My Orders, support link and how-to-buy guide
- Payment-provider checkout/QR integration scaffold and UTR submission as a reference
- Provider status lookup checks order ID, amount and currency before marking paid
- Idempotent delivery claim to prevent duplicate fulfillment
- Credentials are read from environment variables (`config.py`)

## Important payment integration note
Payment APIs differ. `PAYMENT_API_URL` and `PAYMENT_STATUS_URL` are generic integration points, not a claim of compatibility with any specific provider. Before enabling real payments, adapt request/response fields to your authorized gateway's official API documentation, validate webhook signatures if webhooks are used, verify transaction amount/currency/order/reference, and test duplicate callbacks and failed/late payments. Never accept a UTR as proof by itself. This starter does not implement a public webhook endpoint; the bot polls the provider status endpoint when the user presses **Check payment**.

QR is generated only from a QR/UPI payload returned by the configured provider. Do not construct a QR from a personal UPI ID and treat it as verified payment.

## Files
- `main.py`: bot logic
- `config.py`: environment variable configuration
- `requirements.txt`: dependencies
- `Procfile`: Heroku worker process
- `runtime.txt`: Python runtime

## Heroku deployment (GitHub)
1. Create a MongoDB Atlas database and a database user with a strong password. Allow only the network access required by your deployment where feasible.
2. Push these files to a private GitHub repository.
3. Create a Heroku app and connect it to the GitHub repository in **Deploy**.
4. Add Config Vars under **Settings → Config Vars**:
   - `BOT_TOKEN`
   - `ADMIN_IDS` (comma-separated numeric Telegram user IDs)
   - `MONGO_URI`
   - `MONGO_DB_NAME` (optional)
   - `SUPPORT_URL` (optional)
   - `PAYMENT_API_URL`
   - `PAYMENT_STATUS_URL`
   - `PAYMENT_API_KEY`
   - `PAYMENT_WEBHOOK_URL` (optional)
   - `PAYMENT_CURRENCY` (default `INR`)
5. Deploy the branch, then scale the `worker` dyno to 1.
6. Check logs and test with a low-value transaction in the provider's test/sandbox environment before production.

## Admin workflow
1. Start the bot with `/start`.
2. Open **Admin Panel → Add product**.
3. Enter product name, price, currency, HTTPS product URL, description (`-` for none), and color (`green`, `red`, or `blue`).
4. Use **List products** to review or deactivate products.

## Security and limitations
- Never put credentials in source code or commit a `.env` file.
- Restrict `ADMIN_IDS` to trusted administrators.
- Use HTTPS product links and ensure the content is lawful and authorized for sale.
- Telegram clients/API versions may not render custom emoji or button styles identically. The premium emoji IDs are included in message text; native inline-button colors are not universally supported by all Telegram clients/API versions.
- This is a starter implementation, not a production-audited payment system. Confirm the exact gateway API schema before taking real payments.
