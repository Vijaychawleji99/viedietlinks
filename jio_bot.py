#!/usr/bin/env python3
# language: python, file: jio_bot.py, runtime: python 3.10+
# -*- coding: utf-8 -*-
"""
JioCoupons Telegram Bot — SINGLE SCRIPT.

Users khud apna number + OTP daal ke login karte hain, phir button dabake
links generate karte hain (unke apne jiocoupons account se).

  1) FORCE JOIN -> pehle channels join karo (@crunchyroollLinksalert, @viediet)
  2) LOGIN      -> number bhejo -> OTP aayega -> OTP bhejo -> done
  3) GENERATE   -> EK click = 50 links PARALLEL (10 workers).
                   50 ka hard cap — jitne mile sab ek .txt file me user ko.
  4) TXT        -> har generate ke baad txt file, aur "My Links" me poori history
  5) QUOTA      -> har user ko shuru me LINKS_PER_REFERRAL links milte hain.
                   Quota khatam = bot ruk jata hai aur REFERRAL link deta hai.
  6) ADMIN      -> admin panel: quota/refer, max links, planId, product, amount
  7) NOTIFY     -> har batch pe DUSRA bot se:
                   • Group 1 (admin): masked purchase message
                   • Group 2 (-5501225557): per-user report + txt file

Setup — top wale constants bhar do:
  MAIN_BOT_TOKEN          -> users wala bot
  NOTIFY_BOT_TOKEN        -> dono groups me report bhejne wala DUSRA bot
  NOTIFY_GROUP_ID         -> group 1 (0 chhodo -> group me /id bhejo)
  SECOND_NOTIFY_GROUP_ID  -> group 2 (-5501225557 set hai)
  ADMIN_IDS               -> apna telegram user id
  FORCE_CHANNELS          -> jin channels ka join zaroori hai
  PROXY                   -> "http://user:pass@host:port" ya khali = direct

Chalao: python jio_bot.py
Data:   jio_bot_data.json | links/ folder me txt files
"""

import json
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import escape as esc

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

# ━━━━━━━━━━━━━ SETTINGS (yahi bhar na) ━━━━━━━━━━━━━
MAIN_BOT_TOKEN = "9269553305:AAGAjje3dkdHSGZeHt2UboOtn_H0pIPJPyw"          # users wala bot
NOTIFY_BOT_TOKEN = "9227155375:AAFXcMS26IaiPTLJKeEp11GBFBkc3HrG0u8"        # dusra bot -> dono notification groups
NOTIFY_GROUP_ID = -5501225557          # 0 chhodo -> admin group me /id bhejo, auto-set
SECOND_NOTIFY_GROUP_ID = -5501225557   # dusra group (per-user generate report)
ADMIN_IDS = [1364476174]               # e.g. [123456789]
LINKS_PER_REFERRAL = 50      # default quota per referral (admin panel se badal sakta)
MAX_LINKS_PER_RUN = 50       # ek click = itne links parallel, phir process RUK
PARALLEL_WORKERS = 8         # 2 se zyada parallel = server 500 deta hai
PLAN_ID = "CRUNCPCROAUG26"   # active offer planId (admin panel se badal sakta)
PRODUCT_NAME = "CrunchyRoll 1 Month Link"   # notification me product line
AMOUNT_TEXT = "50x"             # "Free" / "$0.70" ... khali = line skip
PROXY = ""                   # khali = direct, warna "http://user:pass@ip:port"
MIN_CLAIM_GAP = 2            # ek user ke 2 batch me min gap (seconds)
SECOND_GROUP_FILE = True     # dusre group me txt file bhi bhejein

# ---- FORCE JOIN ----
FORCE_JOIN = True            # True = pehle join karo, phir bot use karo
FORCE_CHANNELS = [           # "@username" ya numeric id (-100...)
    "crunchyroollLinksalert",
    "viediet",
]

BASE = "https://jiocoupons.in/clientapi"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1")
HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(HERE, "jio_bot_data.json")
LOG_FILE = os.path.join(HERE, "jio_bot.log")
LINKS_DIR = os.path.join(HERE, "links")

PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

# ━━━━━━━━━━━━━ LOG ━━━━━━━━━━━━━
def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} | {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n")
    except Exception:
        pass

# ━━━━━━━━━━━━━ DB ━━━━━━━━━━━━━
DB = {"users": {}, "settings": {}, "stats": {"links": 0}}
DB_LOCK = threading.RLock()


def load_db():
    global DB
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "r", encoding="utf-8") as f:
                DB = json.load(f)
            DB.setdefault("users", {})
            DB.setdefault("settings", {})
            DB.setdefault("stats", {"links": 0})
            for u in DB["users"].values():
                u.setdefault("links", [])
                u.setdefault("quota_used", 0)
                u.setdefault("quota_total", 0)
    except Exception as e:
        log(f"db load fail: {e}")


def save_db():
    try:
        with DB_LOCK:
            with open(DB_FILE, "w", encoding="utf-8") as f:
                json.dump(DB, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"db save fail: {e}")


def s_get(key, default):
    return DB["settings"].get(key, default)


def s_set(key, val):
    DB["settings"][key] = val
    save_db()


def user_get(uid):
    return DB["users"].get(str(uid))


def user_new(uid, name, username):
    u = {
        "name": name or "",
        "username": username or "",
        "mobile": "",
        "token": "",
        "quota_total": s_get("quota_per_ref", LINKS_PER_REFERRAL),
        "quota_used": 0,
        "refs": 0,
        "ref_by": 0,
        "links": [],
        "joined": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    DB["users"][str(uid)] = u
    save_db()
    return u


# ━━━━━━━━━━━━━ HELPERS ━━━━━━━━━━━━━
def mask_name(s):
    """D••••l type — pehla + aakhri letter, beech chhupa."""
    s = (s or "").strip()
    if not s:
        return "U•••"
    if len(s) <= 2:
        return s[0] + "••"
    return s[0] + "••••" + s[-1]


def mask_mobile(m):
    m = (m or "").strip()
    return f"+91-{m[:2]}******{m[-2:]}" if len(m) >= 4 else "+91-••••••"


def is_admin(uid):
    return int(uid) in ADMIN_IDS


def quota_left(u):
    return max(0, u["quota_total"] - u["quota_used"])


def bot_username():
    me = tg("getMe")
    return (me.get("result", {}) or {}).get("username", "")


def ref_link(uid):
    return f"https://t.me/{BOT_UNAME}?start=REF{uid}"


def gid_main():
    return s_get("notify_group", 0) or NOTIFY_GROUP_ID


def gid_second():
    return s_get("second_group", 0) or SECOND_NOTIFY_GROUP_ID


def ch_label(ch):
    ch = str(ch)
    if ch.startswith("@"):
        ch = ch[1:]
    return f"https://t.me/{ch}" if not ch.startswith("-") else ch


# ━━━━━━━━━━━━━ TELEGRAM API ━━━━━━━━━━━━━
def tg(method, **params):
    try:
        r = requests.post(f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/{method}",
                          json=params, timeout=70, proxies=PROXIES)
        if r.status_code == 200:
            return r.json()
        return {"ok": False, "error": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def tg_notify(method, **params):
    try:
        r = requests.post(f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/{method}",
                          json=params, timeout=70, proxies=PROXIES)
        if r.status_code == 200:
            return r.json()
        return {"ok": False, "error": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def send_msg(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    return tg("sendMessage", **p)


def send_kb(chat_id, text, kb):
    """Reply keyboard nahi — inline hi simple rehta hai."""
    return send_msg(chat_id, text, kb)


def answer_cb(cb_id, text=""):
    tg("answerCallbackQuery", callback_query_id=cb_id, text=text or None)


def send_document(chat_id, path, caption="", notify=False):
    """TXT file bhejo — main bot ya notify bot se."""
    token = NOTIFY_BOT_TOKEN if notify else MAIN_BOT_TOKEN
    if not token:
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": (caption or "")[:1000],
                      "parse_mode": "HTML"},
                files={"document": (os.path.basename(path), f, "text/plain")},
                timeout=180, proxies=PROXIES)
        if r.status_code != 200:
            log(f"sendDocument fail: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        log(f"sendDocument err: {str(e)[:120]}")
        return False


def build_links_txt(uid, name, mobile, items):
    """items = [{code, link, ts}] -> txt file path."""
    try:
        os.makedirs(LINKS_DIR, exist_ok=True)
    except Exception:
        pass
    fn = f"links_{uid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path = os.path.join(LINKS_DIR, fn)
    lines = [
        "🎁 JioCoupons Links",
        f"User  : {name or 'User'} ({uid})",
        f"Mobile: {mobile or '-'}",
        f"Date  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total : {len(items)}",
        "=" * 46,
        "",
    ]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it.get('link') or '(no link)'}")
        lines.append(f"   CODE: {it.get('code', '')}")
        if it.get("ts"):
            lines.append(f"   AT  : {it['ts']}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ━━━━━━━━━━━━━ FORCE JOIN ━━━━━━━━━━━━━
JOIN_CACHE = {}
JOIN_TTL = 60


def channel_username(ch):
    ch = str(ch).strip()
    if ch.startswith("@"):
        return ch[1:]
    if ch.lstrip("-").isdigit():
        return None
    return ch


def join_link(ch):
    un = channel_username(ch)
    return f"https://t.me/{un}" if un else ""


def is_joined(uid):
    """True = saare force channels join hain (60s cache)."""
    if not FORCE_JOIN or not FORCE_CHANNELS or is_admin(uid):
        return True
    key = str(uid)
    hit = JOIN_CACHE.get(key)
    if hit and time.time() - hit[1] < JOIN_TTL:
        return hit[0]
    ok = True
    for ch in FORCE_CHANNELS:
        target = ch if str(ch).startswith("@") else (
            f"@{ch}" if not str(ch).lstrip("-").isdigit() else ch)
        try:
            r = tg("getChatMember", chat_id=target, user_id=int(uid))
            st = ((r.get("result") or {}).get("status") or "").lower()
            if st not in ("creator", "administrator", "member"):
                ok = False
                break
        except Exception:
            ok = False
            break
    JOIN_CACHE[key] = (ok, time.time())
    return ok


def force_join_screen(uid, edit=None):
    if not FORCE_CHANNELS:
        return
    rows = []
    for ch in FORCE_CHANNELS:
        ln = join_link(ch)
        if ln:
            rows.append([{"text": f"📢 Join @{channel_username(ch)}", "url": ln}])
    rows.append([{"text": "✅ Join ho gaya — Verify", "callback_data": "verify"}])
    t = ("🔒 <b>Pehle join karo</b>\n\n"
         "Bot use karne ke liye in channels me join karna zaroori hai:\n\n"
         + "\n".join(f"• @{channel_username(ch)}" for ch in FORCE_CHANNELS)
         + "\n\nJoin karke <b>Verify</b> dabao.")
    if edit:
        tg("editMessageText", chat_id=edit["chat"]["id"], message_id=edit["message_id"],
           text=t, parse_mode="HTML", disable_web_page_preview=True,
           reply_markup={"inline_keyboard": rows})
    else:
        send_msg(uid, t, rows)


# ━━━━━━━━━━━━━ NOTIFY BOTS (2 groups) ━━━━━━━━━━━━━
def notify_send(gid, text, doc_path=None, caption=""):
    """NOTIFY bot se message (+ optional txt) bhejo. True = gaya."""
    if not gid or not NOTIFY_BOT_TOKEN:
        return False
    sent = False
    try:
        r = requests.post(f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage",
                          json={"chat_id": gid, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True},
                          timeout=20, proxies=PROXIES)
        if r.status_code != 200:
            log(f"notify fail ({gid}): {r.text[:150]} "
                "-> id galat ya notify bot group me nahi (/setg2 se set karo)")
        else:
            sent = True
    except Exception as e:
        log(f"notify err: {str(e)[:150]}")
    if doc_path:
        try:
            with open(doc_path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendDocument",
                    data={"chat_id": gid, "caption": caption[:1000],
                          "parse_mode": "HTML"},
                    files={"document": (os.path.basename(doc_path), f, "text/plain")},
                    timeout=180, proxies=PROXIES)
        except Exception as e:
            log(f"notify doc err: {str(e)[:120]}")
    return sent


def notify_probe(gid):
    """Naya group set karte waqt test message. (ok, error)"""
    if not NOTIFY_BOT_TOKEN:
        return False, "NOTIFY_BOT_TOKEN khali hai"
    try:
        r = requests.post(f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage",
                          json={"chat_id": gid, "parse_mode": "HTML",
                                "text": "✅ <b>Connected!</b>\n"
                                        "Ab is group me generate report aayegi 🎉"},
                          timeout=20, proxies=PROXIES)
        j = {}
        try:
            j = r.json()
        except Exception:
            pass
        if r.status_code == 200 and j.get("ok"):
            return True, ""
        return False, j.get("description") or r.text[:150]
    except Exception as e:
        return False, str(e)[:150]


def list_notify_chats(uid):
    """/chats — notify bot jahan jahan hai wanh ke chat ids."""
    if not NOTIFY_BOT_TOKEN:
        send_msg(uid, "⚠️ NOTIFY_BOT_TOKEN khali hai.")
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/getUpdates",
            params={"limit": 100, "timeout": 0,
                    "allowed_updates": json.dumps(["message", "my_chat_member",
                                                   "channel_post"])},
            timeout=25, proxies=PROXIES).json()
    except Exception as e:
        send_msg(uid, f"❌ Fetch fail: {esc(str(e)[:120])}")
        return
    found = {}
    for upd in r.get("result", []):
        obj = (upd.get("message") or upd.get("my_chat_member")
               or upd.get("channel_post") or {})
        c = obj.get("chat") or {}
        if c.get("id"):
            found[str(c["id"])] = f"{c.get('type')} | {c.get('title') or c.get('username') or ''}"
    if not found:
        send_msg(uid, "🤷 Kuch nahi mila.\n\n"
                      "<b>Notify bot</b> ko group me add karo (admin banao), "
                      "wahan koi message bhejo, phir <b>/chats</b> dabao.")
        return
    lines = ["🆔 <b>Notify bot ke chats</b>\n"]
    for cid, label in found.items():
        lines.append(f"<code>{cid}</code> — {esc(label)}")
    lines.append("\nSet karne ke liye:\n<code>/setg2 &lt;id&gt;</code>")
    send_msg(uid, "\n".join(lines))


def notify_group(text):
    notify_send(gid_main(), text)


def notify_second(text, doc_path=None, caption=""):
    notify_send(gid_second(), text, doc_path, caption)


# ━━━━━━━━━━━━━ JIOCOUPONS API ━━━━━━━━━━━━━
_TL = threading.local()


def ses():
    """Har thread ka apna session (parallel safe)."""
    s = getattr(_TL, "s", None)
    if s is None:
        s = requests.Session()
        if PROXIES:
            s.proxies.update(PROXIES)
        _TL.s = s
    return s


def jc_headers(token=None):
    h = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://jiocoupons.in",
        "referer": "https://jiocoupons.in/",
        "user-agent": UA,
    }
    if token:
        h["authorization"] = f"Bearer {token}"
    return h


def jc_send_otp(mobile):
    """OTP bhejo — 10-digit number."""
    try:
        r = SES.post(f"{BASE}/login", json={"mobileNo": mobile, "consentFlag": True},
                     headers=jc_headers(), timeout=15)
        return r.status_code == 200 and r.json().get("success")
    except Exception as e:
        log(f"otp send err: {str(e)[:120]}")
        return False


def jc_verify_otp(mobile, otp):
    """OTP verify -> session {mobile, token, user_id} ya None."""
    try:
        r = SES.post(f"{BASE}/verify", json={"mobileNo": mobile, "otp": otp},
                     headers=jc_headers(), timeout=15)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        j = r.json()
        if not j.get("success"):
            return None, j.get("statusMessage") or j.get("message") or "OTP galat"
        token = (j.get("token", {}) or {}).get("access", {}).get("token")
        user = j.get("user", {}) or {}
        if not token:
            return None, "token nahi mila"
        return {"mobile": user.get("phone", mobile), "token": token,
                "user_id": user.get("id")}, None
    except Exception as e:
        return None, str(e)[:120]


def jc_claim(token, plan_id):
    """Ek claim = ek code+link. Parallel safe (thread-local session)."""
    try:
        r = ses().post(f"{BASE}/coupon/claim",
                       json={"planId": plan_id, "isStatic": False},
                       headers=jc_headers(token), timeout=15)
        if r.status_code == 200:
            j = r.json()
            if j.get("success") and j.get("couponCode"):
                return {"code": j["couponCode"], "link": j.get("spUIURL", "")}
            err = j.get("statusMessage") or j.get("message") or json.dumps(j)[:120]
            return {"error": err}
        return {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)[:120]}


def claim_err_kind(err):
    """Error ka type — sirf dikhane ke liye, koi workaround nahi."""
    e = str(err or "").lower()
    if any(k in e for k in ("cap", "limit", "max", "reached", "exceed", "quota",
                            "already", "duplicate")):
        return "cap"
    if any(k in e for k in ("unauthorized", "401", "token", "session", "login")):
        return "auth"
    return "other"


# ━━━━━━━━━━━━━ KEYBOARDS ━━━━━━━━━━━━━
def main_kb(uid):
    kb = [[{"text": f"🎁 Generate {s_get('max_links', MAX_LINKS_PER_RUN)} Links",
           "callback_data": "gen"}],
          [{"text": "📥 My Links (.txt)", "callback_data": "mylinks"}],
          [{"text": "🔑 Login", "callback_data": "login"},
           {"text": "💰 Mera Quota", "callback_data": "quota"}],
          [{"text": "👥 Refer & Earn", "callback_data": "refer"},
           {"text": "ℹ️ Help", "callback_data": "help"}]]
    if is_admin(uid):
        kb.append([{"text": "🛠 Admin Panel", "callback_data": "admin"}])
    return kb


def admin_kb():
    return [
        [{"text": "📊 Stats", "callback_data": "a_stats"},
         {"text": "👥 Top Users", "callback_data": "a_users"}],
        [{"text": f"⚙️ Quota/Refer: {s_get('quota_per_ref', LINKS_PER_REFERRAL)}",
          "callback_data": "a_setquota"},
         {"text": f"🔢 Max/run: {s_get('max_links', MAX_LINKS_PER_RUN)}",
          "callback_data": "a_setmax"}],
        [{"text": f"🎯 PlanId: {str(s_get('plan_id', PLAN_ID))[:10]}…",
          "callback_data": "a_setplan"},
         {"text": f"🏷 Product: {str(s_get('product', PRODUCT_NAME))[:12]}…",
          "callback_data": "a_setprod"}],
        [{"text": "💵 Amount", "callback_data": "a_setamount"},
         {"text": "➕ Quota Do (user)", "callback_data": "a_addq"}],
        [{"text": "⬅️ Menu", "callback_data": "back"}],
    ]


def notify_text(name, product, amount, qty=1):
    t = ("🔗 <b>Reseller Purchase!</b> 🚀\n\n"
         f"😀 User: {esc(mask_name(name))}\n"
         f"📦 Product: {esc(product)}\n"
         f"0️⃣ QTY: {qty}\n")
    if amount:
        t += f"💵 Amount: {esc(amount)}\n"
    t += "\n⚡ Auto-delivered via Bot API!"
    return t


def second_group_text(uid, u, ok_n, fail_n):
    prod = s_get("product", PRODUCT_NAME)
    return ("🔗 <b>New Links Generated</b>\n\n"
            f"👤 User: {esc(mask_name(u.get('name') or 'User'))}\n"
            f"🆔 UID: <code>{uid}</code>\n"
            f"📱 Mobile: {esc(mask_mobile(u.get('mobile')))}\n"
            f"✅ Links: <b>{ok_n}</b>\n"
            f"❌ Fail: {fail_n}\n"
            f"📦 {esc(prod)}\n"
            f"💰 Quota: {u.get('quota_used', 0)}/{u.get('quota_total', 0)}")


# ━━━━━━━━━━━━━ SCREENS ━━━━━━━━━━━━━
def screen_main(uid, u, edit=None):
    t = (f"👋 Welcome {esc(mask_name(u['name'] or 'User'))}!\n\n"
         "🎁 Apne number se login karke free offer links\n"
         "generate karo — saare links ek TXT file me.\n\n"
         f"💰 Quota: {u['quota_used']}/{u['quota_total']} used | "
         f"🟢 Bache: {quota_left(u)}")
    if edit:
        tg("editMessageText", chat_id=edit["chat"]["id"], message_id=edit["message_id"],
           text=t, parse_mode="HTML", disable_web_page_preview=True,
           reply_markup={"inline_keyboard": main_kb(uid)})
    else:
        send_msg(uid, t, main_kb(uid))


def screen_login(uid):
    STATES[str(uid)] = {"st": "num"}
    send_msg(uid, "🔑 <b>Login</b>\n\n"
                  "Apna 10-digit mobile number bhejo:\n"
                  "(jispe OTP aayega)")


def screen_otp(uid, mobile):
    STATES[str(uid)] = {"st": "otp", "mobile": mobile}
    send_msg(uid, f"📩 OTP bhej diya <b>{mask_mobile(mobile)}</b> pe.\n\n"
                  "6-digit OTP yahan bhejo:")


def screen_quota_block(uid, u):
    """Quota khatam = bot RUK jata hai, referral ke alawa kuch nahi dega."""
    send_msg(uid,
             f"🚫 <b>Quota khatam!</b> ({u['quota_used']}/{u['quota_total']} use ho gaya)\n\n"
             "🔗 Aur links ke liye <b>REFER karo</b>:\n"
             f"{ref_link(uid)}\n\n"
             f"🔥 Har naye referral pe +{s_get('quota_per_ref', LINKS_PER_REFERRAL)} links!")


def screen_quota(uid, u):
    send_msg(uid,
             "💰 <b>Mera Quota</b>\n\n"
             f"✅ Used: {u['quota_used']}\n"
             f"🎯 Total: {u['quota_total']}\n"
             f"🟢 Bache: {quota_left(u)}\n\n"
             "🔗 Refer karke aur badhao:\n"
             f"{ref_link(uid)}",
             [[{"text": "👥 Refer & Earn", "callback_data": "refer"}],
              [{"text": "⬅️ Menu", "callback_data": "back"}]])


def screen_refer(uid, u):
    send_msg(uid,
             "👥 <b>Refer & Earn</b>\n\n"
             "🔗 Apna referral link:\n"
             f"{ref_link(uid)}\n\n"
             f"🎁 Har naye user = +{s_get('quota_per_ref', LINKS_PER_REFERRAL)} links\n"
             f"📊 Ab tak referrals: {u['refs']}",
             [[{"text": "💰 Mera Quota", "callback_data": "quota"}],
              [{"text": "⬅️ Menu", "callback_data": "back"}]])


def screen_help(uid):
    n = s_get("max_links", MAX_LINKS_PER_RUN)
    send_msg(uid,
             "ℹ️ <b>Kaise use karein</b>\n\n"
             "1️⃣ <b>Login</b> dabao → apna 10-digit number bhejo\n"
             "2️⃣ OTP aayega → 6-digit OTP bhejo\n"
             f"3️⃣ <b>Generate</b> dabao → {n} links parallel bante hain\n"
             "4️⃣ TXT file me saare links + codes milte hain\n\n"
             "📥 <b>My Links (.txt)</b> = poori history\n"
             "💡 Har referral pe quota badhta hai.\n"
             "⚠️ Offer limits platform ke hisaab se hoti hain.",
             [[{"text": "⬅️ Menu", "callback_data": "back"}]])


def screen_admin(uid):
    n_users = len(DB["users"])
    total = DB["stats"].get("links", 0)
    logged = sum(1 for u in DB["users"].values() if u.get("token"))
    send_msg(uid,
             "🛠 <b>Admin Panel</b>\n\n"
             f"👥 Users: {n_users} | 🔑 Logged: {logged}\n"
             f"🔗 Total links nikaale: {total}\n"
             f"⚙️ Quota/refer: {s_get('quota_per_ref', LINKS_PER_REFERRAL)}\n"
             f"🔢 Max links/run: {s_get('max_links', MAX_LINKS_PER_RUN)}\n"
             f"🎯 PlanId: {s_get('plan_id', PLAN_ID)}\n"
             f"🏷 Product: {s_get('product', PRODUCT_NAME)}\n"
             f"💵 Amount: {s_get('amount', AMOUNT_TEXT) or '(khali)'}\n"
             f"📢 Group 1: {gid_main() or 'set nahi (/id bhejo group me)'}\n"
             f"📢 Group 2: {gid_second() or 'set nahi (/id2 bhejo group me)'}",
             admin_kb())


def screen_admin_users(uid):
    us = sorted(DB["users"].items(), key=lambda kv: kv[1].get("quota_used", 0),
                reverse=True)[:20]
    if not us:
        return send_msg(uid, "👥 Abhi koi user nahi.", [[{"text": "⬅️ Panel", "callback_data": "admin"}]])
    lines = ["👥 <b>Top Users (links nikale)</b>\n"]
    for i, (uid_s, u) in enumerate(us, 1):
        nm = mask_name(u.get("name") or u.get("mobile") or "User")
        lines.append(f"{i}. {nm} — {u.get('quota_used', 0)} links "
                     f"(refs {u.get('refs', 0)})")
    send_msg(uid, "\n".join(lines), [[{"text": "⬅️ Panel", "callback_data": "admin"}]])


# ━━━━━━━━━━━━━ ACTIONS ━━━━━━━━━━━━━
LAST_CLAIM = {}
GEN_RUNNING = set()          # jis user ka batch chal raha hai


def send_links_file(uid, u):
    hist = u.get("links", [])
    if not hist:
        send_msg(uid, "📄 Abhi tak koi link nahi — pehle Generate dabao.")
        return
    path = build_links_txt(uid, u.get("name"), mask_mobile(u.get("mobile")), hist)
    send_document(uid, path, f"📄 <b>Total {len(hist)} links</b> — TXT file")


def do_generate(uid, u):
    # login check
    if not u.get("token"):
        send_msg(uid, "🔑 Pehle login karo!", [[{"text": "🔑 Login", "callback_data": "login"}]])
        return
    # force join
    if not is_joined(uid):
        force_join_screen(uid)
        return
    # quota check — khatam = RUK JAO + referral CTA
    if quota_left(u) <= 0:
        screen_quota_block(uid, u)
        return
    # double click guard
    if str(uid) in GEN_RUNNING:
        send_msg(uid, "⏳ Pehla batch abhi chal raha hai — usko pura hone do.")
        return
    # rate gap
    now = time.time()
    if now - LAST_CLAIM.get(str(uid), 0) < MIN_CLAIM_GAP:
        send_msg(uid, f"⏳ Thoda rukho — {MIN_CLAIM_GAP}s me ek batch.")
        return

    maxrun = max(1, min(500, int(s_get("max_links", MAX_LINKS_PER_RUN))))
    count = min(maxrun, quota_left(u))       # 50 ka HARD cap + quota
    if count <= 0:
        screen_quota_block(uid, u)
        return

    LAST_CLAIM[str(uid)] = now
    GEN_RUNNING.add(str(uid))
    threading.Thread(target=_gen_wrapper,
                     args=(uid, count, u["token"], s_get("plan_id", PLAN_ID)),
                     daemon=True).start()


def _gen_wrapper(uid, count, token, plan_id):
    try:
        _gen_worker(uid, count, token, plan_id)
    except Exception:
        log("[gen] worker err:\n" + traceback.format_exc(limit=4))
        try:
            send_msg(uid, "❌ Batch me error aa gaya — dobara try karo.")
        except Exception:
            pass
    finally:
        GEN_RUNNING.discard(str(uid))


def _gen_worker(uid, count, token, plan_id):
    """count links PARALLEL me claim karo -> txt file -> dono groups me report."""
    prod = s_get("product", PRODUCT_NAME)
    amt = s_get("amount", AMOUNT_TEXT)
    ok, errs = [], {}

    msg = send_msg(uid, f"⏳ <b>{count} links</b> parallel generate ho rahe hain…\n"
                        "Sab links ek hi TXT file me aayenge.")
    mid = ((msg.get("result") or {}).get("message_id"))
    last_upd = [0]

    def progress(done):
        if not mid:
            return
        try:
            tg("editMessageText", chat_id=uid, message_id=mid,
               text=f"⏳ Generate ho raha hai… <b>{done}/{count}</b>\n"
                    f"✅ Mile: {len(ok)} | 🔁 Retry me baaki",
               parse_mode="HTML")
        except Exception:
            pass

    def claim_soft(i):
        # server burst pe 500 deta hai -> thoda stagger
        time.sleep((i % max(1, PARALLEL_WORKERS)) * 0.2)
        return jc_claim(token, plan_id)

    retry_errs = []
    with ThreadPoolExecutor(max_workers=max(1, min(PARALLEL_WORKERS, count))) as ex:
        futs = [ex.submit(claim_soft, i) for i in range(count)]
        for f in as_completed(futs):
            try:
                res = f.result()
            except Exception as e:
                res = {"error": str(e)[:80]}
            if "code" in res:
                ok.append(res)
            else:
                retry_errs.append(str(res.get("error", "?"))[:80])
            done = len(ok) + len(retry_errs)
            if done - last_upd[0] >= 10 or done == count:
                last_upd[0] = done
                progress(done)

    # ---- RETRY PASSES: jo fail hua use aaram se dobara (3 rounds) ----
    rounds = 0
    while retry_errs and len(ok) < count and rounds < 3:
        rounds += 1
        still = []
        for _ in retry_errs:
            if len(ok) >= count:
                break
            time.sleep(0.35)
            res = jc_claim(token, plan_id)
            if "code" in res:
                ok.append(res)
            else:
                still.append(str(res.get("error", "?"))[:80])
            progress(len(ok) + len(still))
        retry_errs = still

    for e_ in retry_errs:
        errs[e_] = errs.get(e_, 0) + 1

    fail_n = sum(errs.values())
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with DB_LOCK:
        uu = user_get(uid) or {}
        if ok:
            uu["quota_used"] = min(uu.get("quota_total", 0),
                                   uu.get("quota_used", 0) + len(ok))
            DB["stats"]["links"] = DB["stats"].get("links", 0) + len(ok)
            hist = uu.setdefault("links", [])
            for r in ok:
                hist.append({"code": r["code"], "link": r.get("link", ""), "ts": ts})
            if len(hist) > 500:
                uu["links"] = hist[-500:]
        auth_dead = bool(errs) and not ok and all(
            claim_err_kind(k) == "auth" for k in errs)
        if auth_dead:
            uu["token"] = ""
        save_db()
        name_s = uu.get("name") or ""
        mobile_s = uu.get("mobile") or ""
        used_s = uu.get("quota_used", 0)
        total_s = uu.get("quota_total", 0)

    # ---- txt file -> user ----
    txt_path = None
    if ok:
        txt_path = build_links_txt(uid, name_s, mask_mobile(mobile_s), ok)
        ok_sent = send_document(uid, txt_path, f"📄 <b>{len(ok)} links</b> — TXT file")
        if not ok_sent:
            # fallback: file fail -> seedha text me links (10-10 ke chunk)
            for i in range(0, min(len(ok), 30), 10):
                part = ok[i:i + 10]
                body = "\n".join(
                    f"{i + j}. {esc(p.get('link') or p['code'])}"
                    for j, p in enumerate(part, 1))
                send_msg(uid, f"🔗 <b>Links {i + 1}-{i + len(part)}</b>\n\n{body}")

    # ---- group 1 (masked purchase style) ----
    if ok:
        notify_group(notify_text(name_s or mobile_s or "User", prod, amt, len(ok)))

    # ---- group 2 (per-user generate report + txt) ----
    if ok:
        snap = {"name": name_s, "mobile": mobile_s,
                "quota_used": used_s, "quota_total": total_s}
        notify_second(second_group_text(uid, snap, len(ok), fail_n),
                      txt_path if SECOND_GROUP_FILE else None,
                      f"📄 {esc(mask_name(name_s or 'User'))} — {len(ok)} links")

    # ---- final message ----
    if ok:
        final = (f"✅ <b>Batch pura!</b>\n\n"
                 f"🔗 Links mile: <b>{len(ok)}</b>\n"
                 f"❌ Fail: {fail_n}\n"
                 f"💰 Quota: {used_s}/{total_s} | 🟢 Bache: {max(0, total_s - used_s)}\n\n"
                 "📄 TXT file upar bheji hai.")
    else:
        top = "\n".join(f"• {esc(k)} ×{v}" for k, v in list(errs.items())[:3])
        final = f"❌ <b>Koi link nahi mila.</b>\n\n{top}"
    if auth_dead:
        final += "\n\n🔑 Session expire — dobara login karo."

    if mid:
        tg("editMessageText", chat_id=uid, message_id=mid, text=final,
           parse_mode="HTML", disable_web_page_preview=True,
           reply_markup={"inline_keyboard": main_kb(uid)})
    else:
        send_msg(uid, final, main_kb(uid))

    log(f"[gen] {uid} ok={len(ok)} fail={fail_n}")


def do_login_otp_check(uid, otp):
    st = STATES.get(str(uid), {})
    mobile = st.get("mobile")
    if not mobile:
        screen_login(uid)
        return
    send_msg(uid, "⏳ Verify kar raha hoon…")
    sess, err = jc_verify_otp(mobile, otp)
    if not sess:
        send_msg(uid, f"❌ OTP galat / fail: {str(err)[:100]}\n"
                      "Sahi 6-digit OTP bhejo (ya /start dabake phir se):")
        return
    u = user_get(uid)
    if not u:
        u = user_new(uid, "", "")
    u["mobile"] = sess["mobile"]
    u["token"] = sess["token"]
    save_db()
    STATES.pop(str(uid), None)
    log(f"[login] {mask_mobile(sess['mobile'])} OK")
    send_msg(uid, f"✅ <b>Login ho gaya!</b> {mask_mobile(sess['mobile'])}\n"
                  "Ab Generate Link daba sakte ho 🎁")
    screen_main(uid, u)


# ━━━━━━━━━━━━━ STATE ━━━━━━━━━━━━━
STATES = {}


# ━━━━━━━━━━━━━ REFERRAL ━━━━━━━━━━━━━
def apply_referral(new_uid, ref_uid):
    """Naya user aya REF<id> se -> referrer ko quota bonus."""
    if ref_uid == new_uid:
        return
    ru = user_get(ref_uid)
    if not ru:
        return
    add = s_get("quota_per_ref", LINKS_PER_REFERRAL)
    ru["quota_total"] = ru.get("quota_total", 0) + add
    ru["refs"] = ru.get("refs", 0) + 1
    save_db()
    try:
        send_msg(ref_uid,
                 f"🎉 <b>Naya referral!</b>\n\n"
                 f"😀 {mask_name(user_get(new_uid).get('name') or 'User')} join hua\n"
                 f"➕ +{add} links mile!\n"
                 f"💰 Ab total: {ru['quota_total']} quota")
    except Exception:
        pass


def parse_ref_payload(text):
    m = re.match(r"^/start\s+REF(\d+)$", (text or "").strip())
    return int(m.group(1)) if m else 0


# ━━━━━━━━━━━━━ HANDLERS ━━━━━━━━━━━━━
def ensure_user(msg):
    uid = msg["from"]["id"]
    u = user_get(uid)
    if not u:
        u = user_new(uid, msg["from"].get("first_name", ""),
                     msg["from"].get("username", ""))
    else:
        if msg["from"].get("first_name"):
            u["name"] = msg["from"]["first_name"]
        if msg["from"].get("username"):
            u["username"] = msg["from"]["username"]
    return uid, u


def handle_start(msg):
    uid, u = ensure_user(msg)
    ref_uid = parse_ref_payload(msg.get("text", ""))
    is_new = u.get("quota_used", 0) == 0 and u.get("refs", 0) == 0
    if ref_uid and is_new and not u.get("ref_by"):
        u["ref_by"] = ref_uid
        save_db()
        apply_referral(uid, ref_uid)
    if not is_joined(uid):
        force_join_screen(uid)
        return
    screen_main(uid, u)


def _set_group(uid, which, raw):
    val = (raw or "").strip()
    if not val:
        send_msg(uid, "❌ id ya @username bhejo:\n<code>/setg2 -1001234567890</code>")
        return
    gid = int(val) if val.lstrip("-").isdigit() else (
        val if val.startswith("@") else "@" + val)
    ok, err = notify_probe(gid)
    if not ok:
        send_msg(uid, f"❌ Test fail ({esc(str(gid))}):\n{esc(str(err)[:200])}\n\n"
                      "Check: notify bot us group/channel me <b>admin</b> hai?\n"
                      "Sahi id nikalne ke liye: <b>/chats</b>")
        return
    s_set("notify_group" if which == 1 else "second_group", gid)
    send_msg(uid, f"✅ Group {which} set: <code>{esc(str(gid))}</code>\n"
                  "Test message wahan chala gaya — check karo.")


def handle_text(msg):
    uid, u = ensure_user(msg)
    text = (msg.get("text") or "").strip()
    st = STATES.get(str(uid), {})

    # /id — admin group me chat id nikal ke NOTIFY_GROUP_ID auto-set
    if text == "/id":
        cid = msg["chat"]["id"]
        if is_admin(uid) and msg["chat"]["type"] in ("group", "supergroup"):
            s_set("notify_group", cid)
            send_msg(uid, f"📢 Group 1 id set: <code>{cid}</code>\n"
                          "Ab notification isi group me aayenge.")
        else:
            send_msg(uid, f"🆔 chat: <code>{cid}</code>\n👤 user: <code>{uid}</code>")
        return

    # /id2 — DUSRA group (per-user generate report)
    if text == "/id2":
        cid = msg["chat"]["id"]
        if is_admin(uid) and msg["chat"]["type"] in ("group", "supergroup"):
            s_set("second_group", cid)
            send_msg(uid, f"📢 Group 2 id set: <code>{cid}</code>\n"
                          "Ab har user ka generate report yahan aayega.")
        else:
            send_msg(uid, f"🆔 chat: <code>{cid}</code>\n👤 user: <code>{uid}</code>")
        return

    # /setg1 <id|@username> — group 1 (admin report) set + test
    if text.startswith("/setg1 ") and is_admin(uid):
        _set_group(uid, 1, text.split(None, 1)[1])
        return

    # /setg2 <id|@username> — group 2 (per-user report) set + test
    if text.startswith("/setg2 ") and is_admin(uid):
        _set_group(uid, 2, text.split(None, 1)[1])
        return

    # /chats — notify bot ke groups/channels discover karo
    if text == "/chats" and is_admin(uid):
        list_notify_chats(uid)
        return

    if text == "/start":
        handle_start(msg)
        return

    if is_admin(uid) and text == "/admin":
        screen_admin(uid)
        return

    if text == "/mylinks":
        send_links_file(uid, u)
        return

    # ---- force join gate (admin free) ----
    if not is_joined(uid):
        force_join_screen(uid)
        return

    # ---- conversation states ----
    s = st.get("st")
    if s == "num":
        digits = re.sub(r"\D", "", text)
        if len(digits) != 10:
            send_msg(uid, "❌ 10-digit number bhejo (jaise 9876543210)")
            return
        send_msg(uid, f"📩 OTP bhej raha hoon {mask_mobile(digits)} pe…")
        if jc_send_otp(digits):
            screen_otp(uid, digits)
        else:
            send_msg(uid, "❌ OTP send fail — thodi der baad Login dobara dabao.")
        return

    if s == "otp":
        digits = re.sub(r"\D", "", text)
        if len(digits) != 6:
            send_msg(uid, "❌ 6-digit OTP bhejo")
            return
        do_login_otp_check(uid, digits)
        return

    # ---- admin set-value states ----
    if s == "a_quota" and is_admin(uid):
        if text.isdigit() and int(text) > 0:
            s_set("quota_per_ref", int(text))
            send_msg(uid, f"✅ Quota/refer = {text}")
        else:
            send_msg(uid, "❌ Number bhejo (jaise 50)")
            return
        STATES.pop(str(uid), None)
        screen_admin(uid)
        return

    if s == "a_plan" and is_admin(uid):
        if text:
            s_set("plan_id", text.upper())
            send_msg(uid, f"✅ PlanId = {text.upper()}")
        STATES.pop(str(uid), None)
        screen_admin(uid)
        return

    if s == "a_prod" and is_admin(uid):
        s_set("product", text or PRODUCT_NAME)
        send_msg(uid, f"✅ Product = {text}")
        STATES.pop(str(uid), None)
        screen_admin(uid)
        return

    if s == "a_amount" and is_admin(uid):
        s_set("amount", text.strip())
        send_msg(uid, f"✅ Amount = {text.strip() or '(khali)'}")
        STATES.pop(str(uid), None)
        screen_admin(uid)
        return

    if s == "a_addq" and is_admin(uid):
        parts = text.split()
        if len(parts) == 2 and parts[0].lstrip("-").isdigit() and parts[1].lstrip("-").isdigit():
            tu, add = parts[0], int(parts[1])
            tu_obj = user_get(tu)
            if tu_obj:
                tu_obj["quota_total"] = max(0, tu_obj.get("quota_total", 0) + add)
                save_db()
                send_msg(uid, f"✅ User {tu} quota {add:+d} -> total {tu_obj['quota_total']}")
            else:
                send_msg(uid, "❌ Wo user nahi mila")
        else:
            send_msg(uid, "❌ Format: <code>user_id amount</code> (jaise 123456789 100)")
            return
        STATES.pop(str(uid), None)
        screen_admin(uid)
        return

    if s == "a_max" and is_admin(uid):
        if text.isdigit() and 1 <= int(text) <= 500:
            s_set("max_links", int(text))
            send_msg(uid, f"✅ Max links/run = {text}")
        else:
            send_msg(uid, "❌ 1 se 500 ke beech number bhejo (jaise 50)")
            return
        STATES.pop(str(uid), None)
        screen_admin(uid)
        return

    # default -> main menu
    screen_main(uid, u)


def handle_cb(cb):
    uid = cb["from"]["id"]
    u = user_get(uid) or user_new(uid, cb["from"].get("first_name", ""),
                                   cb["from"].get("username", ""))
    data = cb.get("data", "")
    answer_cb(cb["id"])

    if data == "verify":
        JOIN_CACHE.pop(str(uid), None)
        if is_joined(uid):
            send_msg(uid, "✅ Verify ho gaya — ab bot use karo 🎁")
            screen_main(uid, u)
        else:
            force_join_screen(uid)
        return

    if not is_joined(uid):
        force_join_screen(uid)
        return

    if data == "back":
        screen_main(uid, u)
    elif data == "gen":
        do_generate(uid, u)
    elif data == "mylinks":
        send_links_file(uid, u)
    elif data == "login":
        screen_login(uid)
    elif data == "quota":
        screen_quota(uid, u)
    elif data == "refer":
        screen_refer(uid, u)
    elif data == "help":
        screen_help(uid)
    elif data == "admin" and is_admin(uid):
        screen_admin(uid)
    elif data == "a_stats" and is_admin(uid):
        screen_admin(uid)
    elif data == "a_users" and is_admin(uid):
        screen_admin_users(uid)
    elif data == "a_setquota" and is_admin(uid):
        STATES[str(uid)] = {"st": "a_quota"}
        send_msg(uid, f"⚙️ Naya <b>quota per referral</b> bhejo "
                      f"(abhi: {s_get('quota_per_ref', LINKS_PER_REFERRAL)}):")
    elif data == "a_setmax" and is_admin(uid):
        STATES[str(uid)] = {"st": "a_max"}
        send_msg(uid, f"🔢 Naya <b>max links per run</b> bhejo "
                      f"(abhi: {s_get('max_links', MAX_LINKS_PER_RUN)}, 1-500):")
    elif data == "a_setplan" and is_admin(uid):
        STATES[str(uid)] = {"st": "a_plan"}
        send_msg(uid, f"🎯 Naya <b>planId</b> bhejo (abhi: {s_get('plan_id', PLAN_ID)}):")
    elif data == "a_setprod" and is_admin(uid):
        STATES[str(uid)] = {"st": "a_prod"}
        send_msg(uid, f"🏷 Naya <b>product name</b> bhejo "
                      f"(abhi: {s_get('product', PRODUCT_NAME)}):")
    elif data == "a_setamount" and is_admin(uid):
        STATES[str(uid)] = {"st": "a_amount"}
        send_msg(uid, f"💵 Notification me <b>amount</b> bhejo "
                      f"(abhi: {s_get('amount', AMOUNT_TEXT) or '(khali)'}, "
                      "khali bhejoge to line skip hogi):")
    elif data == "a_addq" and is_admin(uid):
        STATES[str(uid)] = {"st": "a_addq"}
        send_msg(uid, "➕ Quota dene ke liye bhejo: <code>user_id amount</code>\n"
                      "(jaise <code>123456789 100</code>, minus bhi chalega)")
    else:
        screen_main(uid, u)


def handle_update(upd):
    if "message" in upd and upd["message"].get("from"):
        handle_text(upd["message"])
    elif "callback_query" in upd:
        handle_cb(upd["callback_query"])


# ━━━━━━━━━━━━━ MAIN LOOP ━━━━━━━━━━━━━
BOT_UNAME = ""
SES = requests.Session()


def check_force_channels():
    """Startup probe — bot ko force channels me member/admin hona chahiye."""
    if not FORCE_JOIN or not FORCE_CHANNELS:
        return
    for ch in FORCE_CHANNELS:
        target = ch if str(ch).startswith("@") else (
            f"@{ch}" if not str(ch).lstrip("-").isdigit() else ch)
        r = tg("getChat", chat_id=target)
        if not r.get("ok"):
            print(f"[!] Force channel {ch}: {str(r.get('error'))[:90]} "
                  "— bot ko wahan member/admin banao.")


def prune_files(days=3):
    try:
        if not os.path.isdir(LINKS_DIR):
            return
        now = time.time()
        for fn in os.listdir(LINKS_DIR):
            p = os.path.join(LINKS_DIR, fn)
            if os.path.isfile(p) and now - os.path.getmtime(p) > days * 86400:
                os.remove(p)
    except Exception:
        pass


def main():
    global BOT_UNAME
    print("=" * 50)
    print("  JioCoupons Bot — single script")
    print("=" * 50)

    if not MAIN_BOT_TOKEN:
        print("[!] MAIN_BOT_TOKEN khali hai — top me bhar do.")
        return

    if PROXY:
        SES.proxies.update(PROXIES)
        print(f"[+] Proxy: {PROXY}")

    load_db()
    prune_files(3)

    me = tg("getMe")
    if not me.get("ok"):
        print(f"[!] Telegram connect fail: {me.get('error')}")
        return
    BOT_UNAME = me["result"].get("username", "")
    print(f"[+] Bot: @{BOT_UNAME} | admin: {ADMIN_IDS} | "
          f"quota/refer: {s_get('quota_per_ref', LINKS_PER_REFERRAL)} | "
          f"max/run: {s_get('max_links', MAX_LINKS_PER_RUN)}")

    if FORCE_JOIN and FORCE_CHANNELS:
        print(f"[+] Force join: {FORCE_CHANNELS}")
        check_force_channels()

    if gid_main():
        print(f"[+] Notify group 1: {gid_main()}")
    else:
        print("[!] Group 1 set nahi — group me /id bhejo.")

    if gid_second():
        print(f"[+] Notify group 2: {gid_second()}")
    else:
        print("[!] Group 2 set nahi — group me /id2 bhejo.")

    if not NOTIFY_BOT_TOKEN:
        print("[!] NOTIFY_BOT_TOKEN khali — group notifications nahi jayenge.")

    offset = 0
    print("[+] Polling start — bas chalne do (Ctrl+C = band)\n")
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=50,
                   allowed_updates=["message", "callback_query"])
            if not r.get("ok"):
                log(f"poll fail: {r.get('error')}")
                time.sleep(3)
                continue
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception:
                    log("handler err:\n" + traceback.format_exc(limit=3))
        except KeyboardInterrupt:
            print("\n[*] Band (user).")
            break
        except Exception as e:
            log(f"poll err: {str(e)[:150]}")
            time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] Stopped.")
