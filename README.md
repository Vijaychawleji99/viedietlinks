# FluxProxy — Telegram Bot + Mini App

A free Telegram bot that delivers working Indian proxies with time-limited access keys.

## How it works

1. A user starts the bot with `/start` and receives one free key credit.
2. They run `/key` (or press **Generate Key**) to create a 12-character key containing uppercase letters and numbers, e.g. `A7K2M9QX4PL8`.
3. They open the **Mini App**, paste the key and activate it.
4. The **20-minute timer starts on activation**. Unlimited proxy generation is enabled until it expires.
5. To get another key after expiry, the user refers a friend from inside the bot. Each valid referral adds `+1` credit to both sides.

Referral links, referral counts and key generation all live **inside the bot**. The Mini App only handles key activation and proxy output.

## Delivery & gift codes

- **Proxy file in the bot chat** — every generation in the Mini App also sends a `.txt` document to the user's bot chat: a paste-ready proxy list plus a speed/capacity section. Delivery needs the user to have pressed `/start` at least once (that is when the chat id is stored), and repeats are rate-limited to one file per minute.
- **Only working proxies** — the engine keeps fetching and verifying in rounds until it has collected the amount the user requested; dead proxies are never shown. Every listed proxy carries a measured speed, an `MB/h` (or `GB/h`) capacity projection, a 20-minute session figure and a `TURBO`/`FAST`/`OK`/`SLOW` tier. A generation is capped at **10 proxies** (`MAX_PROXIES_PER_REQUEST`) so the batch stays high-quality and finishes within the time budget.
- **Force channel join** — `/start`, key generation, status and referrals are gated behind joining `@viediet` (`FORCE_CHANNELS`). The bot calls `getChatMember`, caches the result for 60 seconds and offers a *Join* button plus **✅ I joined — Verify**. A referral reward that arrives before the join is held as *pending* and paid out automatically on the first successful verification; the referrer is notified in their private chat.
- **Gift / redeem codes** — from `/admin` → *Create Gift / Redeem Code*, an admin mints one shareable 12-character code with a chosen session length (1 hour … 7 days) and audience size (10, 25, 50 or unlimited people). Anyone who redeems it gets their own active session; each user can redeem a given code once. Redemption happens in the Mini App key box or with `/redeem CODE` in the bot.

## Proxy quality

Every proxy is verified live before it is shown, and each row in the Mini App reports:

- **WORKING / DEAD badge** — DEAD proxies are shown greyed out with their copy button disabled.
- **Sustained speed** — the proxy is downloaded through for several seconds (Cloudflare, Google and other endpoints are tried until one responds), producing a real KB/s or MB/s figure instead of a single ping.
- **Capacity projection** — `transfer_1h_mb` shows how much data the proxy can move in one hour (`MB/h` or `GB/h`), and the UI also shows the expected volume for a 20-minute key session.
- **Quality tier** — `TURBO` (512+ KB/s), `FAST` (128+ KB/s), `OK` (32+ KB/s) or `SLOW`.
- **Provider status** — for dead proxies the upstream `status` and 24-hour uptime are displayed.

Results are sorted by measured speed, fastest first. Verification runs as a background job with 1.2s polling so long scans never break the tunnel connection, and a 45-second cache makes repeated taps instant.

## Files

| File | Purpose |
|---|---|
| `bot.py` | aiogram 3 bot — keys, referrals, status, help |
| `server.py` | FastAPI — Mini App host and verified JSON API |
| `webapp/index.html` | Mini App interface |
| `core.py` | SQLite storage, key generation, referral accounting |
| `proxy_engine.py` | Proxy fetching and verification |
| `config.py` | `.env` configuration |
| `run.py` / `run.bat` | Runs bot and server together |
| `setup.bat` | First-time setup helper |

## Setup

```bash
cd FluxProxyBot
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set:

- `BOT_TOKEN` — from @BotFather
- `BOT_USERNAME` — bot username without `@`
- `WEBAPP_URL` — public HTTPS URL of the Mini App (e.g. Netlify)
- `PUBLIC_API_URL` — public HTTPS URL of this FastAPI server (where `/api/*` lives)
- `ADMIN_IDS` / `ADMIN_USERNAMES` — administrators with unlimited key generation

Telegram Mini Apps require HTTPS. For local development:

```bash
ngrok http 8000
```

Put the ngrok URL in `WEBAPP_URL` and register the Mini App domain with BotFather via `/setdomain`.

If `index.html` is hosted separately (for example on Netlify), the page cannot call
relative `/api/*` paths — they would hit Netlify instead of this server. Set
`PUBLIC_API_URL` to the public backend URL; the server injects it when serving the
page. For a static deployment, replace `__FLUX_API_BASE__` in `webapp/index.html`
with that backend URL before deploying. You can also override it at runtime with
`?api=https://your-backend` or by setting `localStorage.flux_api`.

## Run

```bash
python run.py
```

Or run the two processes separately:

```bash
python bot.py
python server.py
```

## API

Every POST body contains `init_data` from the Telegram Web App, verified with HMAC-SHA256 using the bot token.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/session` | User state and active key |
| POST | `/api/key/apply` | Activate a key (starts the timer) |
| POST | `/api/status` | Refresh session state |
| POST | `/api/proxies` | Generate verified proxies |
| GET | `/health` | Health check |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KEY_DURATION_MINUTES` | 20 | Active session length |
| `KEY_CLAIM_HOURS` | 24 | Time window to activate an unused key |
| `STARTER_CREDITS` | 1 | Credits granted to a new user |
| `MAX_PROXIES_PER_REQUEST` | 10 | Maximum proxies per generation |
| `DEFAULT_PROXY_WORKERS` | 120 | Parallel verification threads |
| `FORCE_JOIN` | true | Require the channel join before keys/referrals |
| `FORCE_CHANNELS` | viediet | Comma-separated channel usernames |

## Administrators

Add your numeric Telegram ID (from @userinfobot) or username to `ADMIN_IDS` /
`ADMIN_USERNAMES` in `.env`, or directly in `config.py`.

Admins:
- generate unlimited keys with `/key` or the admin panel — no credits or referrals needed
- see `/admin` for database statistics
- can clear all data from the admin panel

Regular users keep the credit/referral rules described above.

Free to use. No payment required.
