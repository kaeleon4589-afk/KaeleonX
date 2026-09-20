# Telegram registration verification v0.11

## Production flow

1. The user registers with phone + password.
2. The API creates a one-hour registration challenge and returns a Telegram deep link:
   `https://t.me/<BOT_USERNAME>?start=<challenge>`.
3. The user opens that link in the official Telegram bot.
4. The bot stores only the SHA-256 challenge hash against the Telegram chat while the challenge is active.
5. The bot shows a private reply-keyboard button requesting the user's own contact.
6. Telegram sends the contact to the bot only after the user authorizes the contact share. The bot requires `contact.user_id == message.from.id`.
7. The backend compares the normalized Telegram phone number with the phone stored for the registration challenge.
8. The bot tells the user to return to KAELEON.
9. `POST /auth/verify` consumes the same one-time challenge and activates the account.
10. Telegram is not used during login.

## Security model

- The browser no longer calls a public `/auth/telegram/contact` endpoint.
- Telegram verification is performed inside the webhook handler using server-side state.
- Telegram webhook requests are protected with `X-Telegram-Bot-Api-Secret-Token`.
- The contact must belong to the Telegram user who initiated the challenge.
- Registration challenges expire after one hour and become single-use after verification.
- The challenge is never stored in plaintext in MongoDB bot-session state.

## Railway variables

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_BOT_USERNAME=<bot username without @>
TELEGRAM_WEBHOOK_URL=https://<public-domain>/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=<random 32-64 char secret>
TELEGRAM_AUTO_SET_WEBHOOK=true
TELEGRAM_API_TIMEOUT_SECONDS=10
```

`TELEGRAM_WEBHOOK_SECRET` must contain only letters, digits, `_` or `-`, matching Telegram's `setWebhook.secret_token` constraints. Telegram sends it back in the `X-Telegram-Bot-Api-Secret-Token` header. The webhook URL must be HTTPS for normal production webhook use.

The app automatically calls `getMe` and `setWebhook` on startup when `TELEGRAM_AUTO_SET_WEBHOOK=true`.
