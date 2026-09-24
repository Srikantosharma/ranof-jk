# Telegram Referral Bot — Render Ready

A Telegram referral bot built with Python + aiogram + PostgreSQL/SQLite, designed for Render Web Service deployment.

## Features

- Mandatory join gate for the 5 configured channels.
- Join buttons + `I Joined — Check` verification.
- Automatic unlock after the bot receives the final channel membership update (when the bot is an admin in the channels).
- Profile, Balance, Refer, Leaderboard, Withdraw.
- Unique deep-link referral URL per user.
- 4 internal Stars per successful referral.
- One withdrawal option: 15 internal Stars.
- 15 Stars are reserved when a withdrawal request is created.
- Admin receives the request with username, Telegram ID and request ID.
- Admin `/admin` panel: requests, search, ban/unban, broadcast, stats, stop/resume.
- Admin `/stop` and `/resume` maintenance mode.
- Telegram webhook protected by `X-Telegram-Bot-Api-Secret-Token`.
- Secrets supplied through Render environment variables, not source control.

## Important Telegram Stars limitation

A Telegram bot can query the bot's own Stars balance, but Bot API does not expose a user's personal Telegram Stars wallet balance to the bot, nor a generic direct bot-to-user Stars transfer method. Therefore the referral program in this repository stores an **internal Stars balance** in the database. The admin marks withdrawals as paid and sends the real Telegram Stars manually.

## Required channel setup

Add the bot as an administrator to all five channels. `getChatMember` is only guaranteed for other users when the bot is an administrator. This is required for the join gate.

## Local setup

1. Create a bot with `@BotFather` and keep the token private.
2. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`, `WEBHOOK_SECRET`, and `WEBHOOK_BASE_URL` if testing through a public HTTPS endpoint.
3. Install dependencies: `pip install -r requirements.txt`.
4. Run: `python app.py`.

For local webhook testing, use an HTTPS tunnel such as Cloudflare Tunnel or ngrok and set `WEBHOOK_BASE_URL` to that public URL.

## Render setup

### Recommended production storage

Use Render Postgres via `DATABASE_URL`. Render's local filesystem is ephemeral, so SQLite data on a normal Render web service can disappear after restart/redeploy/spindown. Render documents Postgres as the persistent relational-storage option.

1. Push this project to a **private** GitHub repository.
2. In Render, create a new Web Service from that repo.
3. Use build command:

   `pip install -r requirements.txt`

4. Use start command:

   `python app.py`

5. Set these environment variables:

   - `BOT_TOKEN` = your BotFather token
   - `ADMIN_ID` = `7652741479`
   - `WEBHOOK_SECRET` = long random value
   - `WEBHOOK_BASE_URL` = your Render URL, e.g. `https://telegram-refer-bot.onrender.com`
   - `DATABASE_URL` = your Render Postgres connection string

6. Deploy. The app calls Telegram `setWebhook` automatically on startup.

### Blueprint option

`render.yaml` is included, but its Free Postgres service is suitable for testing only. Render currently states that Free Postgres databases expire after 30 days. For long-term data, use a paid Postgres database.

## Admin commands

- `/admin` — open admin panel (ignored for normal users)
- `/stop` — pause user access
- `/resume` — resume user access
- `/user <id_or_username>` — search a user
- `/ban <telegram_id>` — ban
- `/unban <telegram_id>` — unban
- `/broadcast <text>` — immediate text broadcast
- `/broadcast` — then send the message in the next admin message

## Security notes

- Never commit `.env` or the BotFather token.
- Keep the GitHub repository private if you do not want the source copied.
- Use Render environment variables for secrets.
- The webhook endpoint rejects requests without the configured Telegram secret token.
- Telegram user IDs are used for referral ownership instead of usernames, because usernames can change.
- The database enforces unique user IDs and keeps withdrawal status server-side.

## Before launch checklist

1. Bot is admin in all 5 channels.
2. `WEBHOOK_SECRET` is set.
3. `DATABASE_URL` points to persistent Postgres.
4. `WEBHOOK_BASE_URL` is the exact public HTTPS Render URL.
5. Test `/start` from a fresh user account.
6. Test an entire referral flow with two accounts.
7. Test 15-Star withdrawal and admin approve/reject.
