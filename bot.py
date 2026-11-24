"""
bot.py — Telegram bot (aiogram 3).

Handles: /start with referral deep links, key creation, referral rewards,
account status and launching the mini app. Run with `python bot.py`.
"""
from __future__ import annotations

import asyncio
import html
import logging
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

import config
import core

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("fluxproxy.bot")

bot: Bot | None = None
dp = Dispatcher()
_BOT_USERNAME = ""


# ──────────────────────────────── keyboards ────────────────────────────
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Open Mini App", web_app=WebAppInfo(url=config.WEBAPP_URL)
                )
            ],
            [
                InlineKeyboardButton(text="Generate Key", callback_data="gen_key"),
                InlineKeyboardButton(text="My Status", callback_data="status"),
            ],
            [
                InlineKeyboardButton(text="Refer & Earn", callback_data="refer"),
                InlineKeyboardButton(text="Help", callback_data="help"),
            ],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Main Menu", callback_data="back")]]
    )


def app_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Open Mini App & Activate",
                    web_app=WebAppInfo(url=config.WEBAPP_URL),
                )
            ],
            [InlineKeyboardButton(text="« Main Menu", callback_data="back")],
        ]
    )


def refer_kb() -> InlineKeyboardMarkup:
    """Referral is handled entirely inside the bot."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Generate Key", callback_data="gen_key")],
            [InlineKeyboardButton(text="Refresh Referral Status", callback_data="refer")],
            [InlineKeyboardButton(text="« Main Menu", callback_data="back")],
        ]
    )


def no_credit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Refer & Earn a Key", callback_data="refer")],
            [InlineKeyboardButton(text="« Main Menu", callback_data="back")],
        ]
    )


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Generate Unlimited Key", callback_data="gen_key")],
            [InlineKeyboardButton(text="🎁 Create Gift / Redeem Code", callback_data="admin_gift")],
            [InlineKeyboardButton(text="Refresh Stats", callback_data="admin")],
            [InlineKeyboardButton(text="Clear All Data", callback_data="admin_clear_confirm")],
            [InlineKeyboardButton(text="« Main Menu", callback_data="back")],
        ]
    )


def gift_kb() -> InlineKeyboardMarkup:
    """Preset gift codes: duration per redemption + how many people can use it."""
    presets = [
        ("1 hour · 10 people", 60, 10),
        ("6 hours · 25 people", 360, 25),
        ("24 hours · 50 people", 1440, 50),
        ("7 days · Unlimited people", 10080, 0),
    ]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"gift:{mins}:{uses}")]
        for label, mins, uses in presets
    ]
    rows.append([InlineKeyboardButton(text="« Back to Admin", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def human_minutes(minutes: int) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440} day(s)"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60} hour(s)"
    return f"{minutes} minute(s)"


def admin_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Yes, Clear Everything", callback_data="admin_clear"),
                InlineKeyboardButton(text="Cancel", callback_data="admin"),
            ]
        ]
    )


def referral_link(ref_code: str) -> str:
    username = _BOT_USERNAME or config.BOT_USERNAME
    return f"https://t.me/{username}?start=ref_{ref_code}"


# ──────────────────────────────── force join ────────────────────────────
JOIN_CACHE: dict[str, tuple[bool, float]] = {}
JOIN_TTL = 60


def channel_link(username: str) -> str:
    return f"https://t.me/{username}"


async def is_joined(user_id: int) -> bool:
    """True when the user is inside every force-join channel (60s cache)."""
    if not config.FORCE_JOIN or not config.FORCE_CHANNELS:
        return True
    if core.is_admin(user_id):
        return True
    key = str(user_id)
    hit = JOIN_CACHE.get(key)
    if hit and time.time() - hit[1] < JOIN_TTL:
        return hit[0]

    ok = True
    for username in config.FORCE_CHANNELS:
        try:
            member = await bot.get_chat_member(f"@{username}", user_id)
            status = str(getattr(member, "status", "")).lower()
            if status not in ("creator", "administrator", "member"):
                ok = False
                break
        except Exception as exc:
            # Channels hide their member list unless the bot is an admin of
            # that channel. Until those rights are granted we fail open so
            # nobody gets locked out — and say so once in the logs.
            log.warning(
                "force-join check unavailable for @%s (%s) — allowing access. "
                "Add the bot as a channel administrator with 'Manage members' "
                "to enable verification.",
                username,
                str(exc)[:90],
            )
            ok = True
            break
    JOIN_CACHE[key] = (ok, time.time())
    return ok


def force_join_text(note: str = "") -> str:
    channels = "\n".join(f"• @{u}" for u in config.FORCE_CHANNELS)
    return (
        "🔒 <b>Join the channel first</b>\n\n"
        "To use FluxProxy you must join these channels:\n\n"
        f"{channels}\n\n"
        "Tap <b>Join</b>, then press <b>✅ I joined — Verify</b>."
        + (f"\n\n{note}" if note else "")
    )


def force_join_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📢 Join @{u}", url=channel_link(u))]
        for u in config.FORCE_CHANNELS
    ]
    rows.append(
        [InlineKeyboardButton(text="✅ I joined — Verify", callback_data="verify")]
    )
    rows.append([InlineKeyboardButton(text="« Main Menu", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_referrer(referrer_id: int, newcomer: str) -> None:
    """Tell the referrer they earned a credit (best effort)."""
    chat_id = core.get_chat_id(referrer_id)
    if not chat_id or bot is None:
        return
    try:
        await bot.send_message(
            chat_id,
            f"🎉 <b>New referral!</b>\n\n{html.escape(newcomer)} joined FluxProxy "
            "with your link.\n➕ <b>+1 key credit</b> added to your account.",
        )
    except Exception:
        log.debug("referrer notification failed", exc_info=True)


# ──────────────────────────────── texts ────────────────────────────────
def welcome_text(user: dict) -> str:
    name = html.escape(user.get("first_name") or "there")
    credits = user["credits"]
    admin = core.is_admin(user["user_id"])
    role = "Administrator — unlimited keys. Use /admin." if admin else f"Available key credits: <b>{credits}</b>"
    return f"""<b>FluxProxy — India Proxy Vault</b>

Welcome, {name}.

Get <b>working Indian proxies</b> — unlimited with an access key.

<b>How it works</b>
1. Create a <b>key</b> here in the bot.
2. Open the <b>Mini App</b> and apply the key.
3. Enjoy <b>{config.KEY_DURATION_MINUTES} minutes</b> of unlimited proxy access.

The timer starts only when you apply the key in the Mini App.
Need another key later? Refer one friend — you both earn a credit.

{role}
Get started below."""


def status_text(user: dict, key: dict | None) -> str:
    name = html.escape(user.get("first_name") or "there")
    admin = core.is_admin(user["user_id"])
    lines = [
        "<b>Account Status</b>",
        "",
        f"Name: <b>{name}</b>",
        f"Role: <b>{'Administrator' if admin else 'User'}</b>",
        f"Key credits: <b>{'Unlimited' if admin else user['credits']}</b>",
        f"Referrals: <b>{user['total_referrals']}</b>",
        f"Keys generated: <b>{core.key_count(user['user_id'])}</b>",
    ]
    if key:
        remaining = max(0, key["expires_at"] - int(time.time()))
        lines += [
            "",
            "<b>Active key</b>",
            f"<code>{html.escape(key['key'])}</code>",
            f"Time remaining: <b>{remaining // 60}m {remaining % 60}s</b>",
            f"Proxies generated: <b>{key.get('proxies_generated', 0)}</b>",
        ]
    else:
        lines += [
            "",
            "<b>No active key.</b>",
            "Apply a key in the Mini App or generate a new one here.",
        ]
    return "\n".join(lines)


def refer_text(user: dict) -> str:
    link = referral_link(user["ref_code"])
    return f"""<b>Refer & Earn — Free Keys</b>

Earn <b>+1 key credit</b> for every valid referral.
Your friend also receives <b>+1 credit</b>.

<b>Your referral link</b>
<code>{html.escape(link)}</code>

Each friend must start the bot with this link.
Credits are added instantly and duplicates are ignored.

<b>Your referrals:</b> {user['total_referrals']}
<b>Available credits:</b> {user['credits']}
<b>Key duration:</b> {config.KEY_DURATION_MINUTES} minutes of unlimited proxies

One key credit becomes one key. Generate it right here."""


def help_text() -> str:
    return f"""<b>How to use FluxProxy</b>

<b>1. Create a key</b>
Use <code>/key</code> or the Generate Key button. Each key is a 12-character code with uppercase letters and numbers.

<b>2. Apply it in the Mini App</b>
Tap Open Mini App, paste your key and activate it.

<b>3. Generate unlimited proxies</b>
The <b>{config.KEY_DURATION_MINUTES}-minute</b> timer starts on activation. Create as many proxies as you need.

<b>4. Get another key</b>
After expiry, refer a friend to earn +1 credit and generate a new key here.

<b>5. Your proxy file arrives in this chat</b>
Every generation in the Mini App also sends a <code>.txt</code> file to this chat, containing every working proxy you just generated.

<b>Redeem a gift code</b>
<code>/redeem YOURCODE</code> — redeem a gift code shared by the admin. You can also paste it into the Mini App key box.

<b>Commands</b>
<code>/start</code> — Main menu
<code>/key</code> — Generate a key
<code>/status</code> — Account status
<code>/refer</code> — Referral link and count
<code>/redeem</code> — Redeem a gift code
<code>/help</code> — This guide
<code>/admin</code> — Administrator panel (admins only)

Free to use. No payment required."""


def admin_text() -> str:
    s = core.get_stats()
    return f"""<b>Administrator Panel</b>

Unlimited key generation is enabled for your account.
Generate as many keys as you need — no credits or referrals required.

<b>Database overview</b>
Users: <b>{s['users']}</b>
Total keys: <b>{s['keys']}</b>
Active keys: <b>{s['active_keys']}</b>
Gift codes: <b>{s['gift_codes']}</b>
Referrals: <b>{s['referrals']}</b>

<b>🎁 Gift / Redeem Code</b>
Create one code that many people can redeem — each redemption gives that
user their own access session for the duration you choose.

Clear All Data removes every user, key, gift code and referral."""


# ──────────────────────────────── handlers ─────────────────────────────
@dp.message(CommandStart())
async def on_start(message: Message, command: CommandStart) -> None:
    tg = message.from_user
    existed = core.get_user(tg.id) is not None
    user = core.ensure_user(tg.id, tg.username, tg.first_name, message.chat.id)

    payload = (command.args or "").strip()
    bonus = ""
    pending = False
    if payload.startswith("ref_"):
        referrer = core.get_user_by_ref_code(payload[4:].strip().upper())
        if referrer and not existed and referrer["user_id"] != tg.id:
            if await is_joined(tg.id):
                if core.register_referral(referrer["user_id"], tg.id):
                    user = core.get_user(tg.id)
                    bonus = (
                        "\n\nReferral successful. You and your friend each received "
                        "+1 key credit."
                    )
                    asyncio.create_task(
                        notify_referrer(
                            referrer["user_id"], tg.first_name or "A new user"
                        )
                    )
            else:
                # Reward is held back until the channel join is verified.
                core.set_pending_referral(tg.id, referrer["user_id"])
                pending = True

    if not await is_joined(tg.id):
        note = (
            "Your referral bonus will be credited the moment you join and verify."
            if pending
            else ""
        )
        await message.answer(force_join_text(note), reply_markup=force_join_kb())
        return

    await message.answer(welcome_text(user) + bonus, reply_markup=main_kb())


async def _complete_pending_referral(user_id: int, newcomer: str) -> bool:
    """Pay out a referral that was waiting for the channel join."""
    ref = core.take_pending_referral(user_id)
    if not ref:
        return False
    if core.register_referral(ref, user_id):
        await notify_referrer(ref, newcomer)
        return True
    return False


@dp.callback_query(F.data == "verify")
async def on_verify(call: CallbackQuery) -> None:
    uid = call.from_user.id
    JOIN_CACHE.pop(str(uid), None)  # force a fresh membership check
    if await is_joined(uid):
        await call.answer("Verified — welcome! 🎉", show_alert=True)
        completed = await _complete_pending_referral(
            uid, call.from_user.first_name or "A new user"
        )
        user = core.ensure_user(
            uid,
            call.from_user.username,
            call.from_user.first_name,
            call.message.chat.id,
        )
        text = welcome_text(user)
        if completed:
            text += (
                "\n\n✅ Referral verified — you and your friend each "
                "received +1 key credit."
            )
        try:
            await call.message.edit_text(text, reply_markup=main_kb())
        except Exception:
            await call.message.answer(text, reply_markup=main_kb())
    else:
        await call.answer(
            "Still not joined — open the channel first.", show_alert=True
        )
        try:
            await call.message.edit_text(force_join_text(), reply_markup=force_join_kb())
        except Exception:
            await call.message.answer(force_join_text(), reply_markup=force_join_kb())


@dp.message(Command("key"))
async def on_key_command(message: Message) -> None:
    await _create_key(message.from_user.id, message)


@dp.message(Command("status"))
async def on_status(message: Message) -> None:
    if not await is_joined(message.from_user.id):
        await message.answer(force_join_text(), reply_markup=force_join_kb())
        return
    user = core.ensure_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.chat.id,
    )
    await message.answer(
        status_text(user, core.active_key(user["user_id"])), reply_markup=main_kb()
    )


@dp.message(Command("refer"))
async def on_refer_command(message: Message) -> None:
    if not await is_joined(message.from_user.id):
        await message.answer(force_join_text(), reply_markup=force_join_kb())
        return
    user = core.ensure_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.chat.id,
    )
    await message.answer(refer_text(user), reply_markup=refer_kb())


@dp.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(help_text(), reply_markup=main_kb())


@dp.message(Command("admin"))
async def on_admin(message: Message) -> None:
    user = core.ensure_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.chat.id,
    )
    if not core.is_admin(user["user_id"], message.from_user.username):
        await message.answer("Restricted. Administrator access only.")
        return
    await message.answer(admin_text(), reply_markup=admin_kb())


@dp.callback_query(F.data == "admin")
async def on_admin_cb(call: CallbackQuery) -> None:
    await call.answer()
    user = core.ensure_user(
        call.from_user.id,
        call.from_user.username,
        call.from_user.first_name,
        call.message.chat.id,
    )
    if not core.is_admin(user["user_id"], call.from_user.username):
        await call.answer("Restricted.", show_alert=True)
        return
    try:
        await call.message.edit_text(admin_text(), reply_markup=admin_kb())
    except Exception:
        await call.message.answer(admin_text(), reply_markup=admin_kb())


@dp.callback_query(F.data == "admin_clear_confirm")
async def on_admin_clear_confirm(call: CallbackQuery) -> None:
    await call.answer()
    if not core.is_admin(call.from_user.id, call.from_user.username):
        return
    await call.message.answer(
        "Clear every user, key and referral? This cannot be undone.",
        reply_markup=admin_confirm_kb(),
    )


@dp.callback_query(F.data == "admin_clear")
async def on_admin_clear(call: CallbackQuery) -> None:
    if not core.is_admin(call.from_user.id, call.from_user.username):
        await call.answer("Restricted.", show_alert=True)
        return
    core.clear_all_data()
    await call.answer("All data cleared.")
    await call.message.answer("Database cleared. All users, keys and referrals removed.", reply_markup=admin_kb())


@dp.callback_query(F.data == "admin_gift")
async def on_admin_gift(call: CallbackQuery) -> None:
    await call.answer()
    if not core.is_admin(call.from_user.id, call.from_user.username):
        await call.answer("Restricted.", show_alert=True)
        return
    text = """<b>🎁 Gift / Redeem Code</b>

One code, many users. Everyone who redeems it gets their own access
session for the duration you pick (one redemption per user).

Share the code in your channel or group — users redeem it in the Mini App
key box or with <code>/redeem CODE</code>.

Choose duration and audience size:"""
    try:
        await call.message.edit_text(text, reply_markup=gift_kb())
    except Exception:
        await call.message.answer(text, reply_markup=gift_kb())


@dp.callback_query(F.data.startswith("gift:"))
async def on_gift_create(call: CallbackQuery) -> None:
    if not core.is_admin(call.from_user.id, call.from_user.username):
        await call.answer("Restricted.", show_alert=True)
        return
    try:
        _, minutes_s, uses_s = (call.data or "").split(":")
        minutes, uses = int(minutes_s), int(uses_s)
    except (ValueError, AttributeError):
        await call.answer("Invalid option.", show_alert=True)
        return
    await call.answer("Gift code created.")
    key = core.generate_gift_key(call.from_user.id, minutes, uses)
    if not key:
        await call.message.answer(
            "Could not create a gift code. Please try again.",
            reply_markup=gift_kb(),
        )
        return
    scope = "Unlimited" if uses == 0 else f"{uses} people"
    await call.message.answer(
        f"""<b>🎁 Gift code created</b>

<code>{html.escape(key)}</code>

Session length: <b>{human_minutes(minutes)}</b>
Redemptions: <b>{scope}</b>
Status: <b>Active</b>

Share it anywhere. Each user redeems it once — in the Mini App key box or
with <code>/redeem {html.escape(key)}</code>.""",
        reply_markup=admin_kb(),
    )


@dp.message(Command("redeem"))
async def on_redeem(message: Message) -> None:
    core.ensure_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.chat.id,
    )
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not code:
        await message.answer(
            "Usage: <code>/redeem YOURCODE</code>\n\n"
            "The code is case-insensitive. You can also paste it into the "
            "Mini App key box."
        )
        return
    success, text, expires = core.apply_key(message.from_user.id, code)
    if success and expires:
        text += f"\nAccess until: <b>{time.strftime('%Y-%m-%d %H:%M', time.localtime(expires))}</b>"
    await message.answer(
        f"{'✅' if success else '❌'} {html.escape(text) if not success else text}",
        reply_markup=app_kb() if success else main_kb(),
    )


@dp.callback_query(F.data == "gen_key")
async def on_gen_key(call: CallbackQuery) -> None:
    await call.answer()
    await _create_key(call.from_user.id, call.message)


async def _create_key(user_id: int, target) -> None:
    if not await is_joined(user_id):
        await target.answer(force_join_text(), reply_markup=force_join_kb())
        return
    key = core.generate_key(user_id)
    if not key:
        await target.answer(
            "<b>No key credits available.</b>\n\n"
            "Refer one friend to earn +1 credit and generate a new key.",
            reply_markup=no_credit_kb(),
        )
        return

    await target.answer(
        f"""<b>Your new key is ready</b>

<code>{html.escape(key)}</code>

Open the Mini App and apply this key.
The {config.KEY_DURATION_MINUTES}-minute timer starts when you apply it.""",
        reply_markup=app_kb(),
    )


@dp.callback_query(F.data == "refer")
async def on_refer(call: CallbackQuery) -> None:
    await call.answer()
    if not await is_joined(call.from_user.id):
        await call.message.answer(force_join_text(), reply_markup=force_join_kb())
        return
    user = core.ensure_user(
        call.from_user.id,
        call.from_user.username,
        call.from_user.first_name,
        call.message.chat.id,
    )
    await call.message.answer(refer_text(user), reply_markup=refer_kb())


@dp.callback_query(F.data == "status")
async def on_status_cb(call: CallbackQuery) -> None:
    await call.answer()
    if not await is_joined(call.from_user.id):
        await call.message.answer(force_join_text(), reply_markup=force_join_kb())
        return
    user = core.ensure_user(
        call.from_user.id,
        call.from_user.username,
        call.from_user.first_name,
        call.message.chat.id,
    )
    await call.message.answer(
        status_text(user, core.active_key(user["user_id"])), reply_markup=back_kb()
    )


@dp.callback_query(F.data == "help")
async def on_help_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer(help_text(), reply_markup=back_kb())


@dp.callback_query(F.data == "back")
async def on_back(call: CallbackQuery) -> None:
    await call.answer()
    user = core.ensure_user(
        call.from_user.id,
        call.from_user.username,
        call.from_user.first_name,
        call.message.chat.id,
    )
    if not await is_joined(call.from_user.id):
        try:
            await call.message.edit_text(
                force_join_text(), reply_markup=force_join_kb()
            )
        except Exception:
            await call.message.answer(force_join_text(), reply_markup=force_join_kb())
        return
    try:
        await call.message.edit_text(welcome_text(user), reply_markup=main_kb())
    except Exception:
        await call.message.answer(welcome_text(user), reply_markup=main_kb())


@dp.message()
async def on_any(message: Message) -> None:
    await message.answer(
        "I did not understand that. Choose an option below or use /help.",
        reply_markup=main_kb(),
    )


async def main() -> None:
    global bot, _BOT_USERNAME
    problems = config.validate()
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    for problem in problems:
        if problem != "BOT_TOKEN is not set":
            log.warning("config: %s", problem)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    core.init_db()
    me = await bot.get_me()
    _BOT_USERNAME = me.username or ""
    log.info("Bot started as @%s", _BOT_USERNAME)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
