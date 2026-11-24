"""
server.py — FastAPI backend + Mini App host.

Serves the single-file mini app at `/` and exposes the JSON API the app uses.
Every request is authenticated with Telegram WebApp `init_data` (HMAC verified).
Run with `python server.py`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uvicorn
import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

import config
import core
import proxy_engine

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("fluxproxy.server")

core.init_db()

app = FastAPI(title="FluxProxy Mini App", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEBAPP_FILE = Path(__file__).parent / "webapp" / "index.html"

# Short-lived cache + lock: repeated taps return instantly and only one
# generation runs at a time (important behind a public tunnel).
_proxy_cache: dict[tuple, tuple[float, list]] = {}
_proxy_lock = asyncio.Lock()
_jobs: dict[str, dict] = {}
_last_delivery: dict[int, float] = {}
PROXY_CACHE_SECONDS = 45
JOB_TTL_SECONDS = 180
DELIVERY_COOLDOWN = 60


# ───────────────────────────── auth ────────────────────────────────────
def verify_init_data(init_data: str) -> tuple[Optional[dict[str, Any]], str]:
    """Validate Telegram WebApp init data. Returns (user, reason)."""
    if not init_data:
        return None, "missing"
    if not config.BOT_TOKEN:
        return None, "no_token"
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None, "no_hash"

        check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        # Telegram spec: secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
        secret = hmac.new(
            b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None, "bad_signature"

        auth_date = int(parsed.get("auth_date", "0") or 0)
        if time.time() - auth_date > 86400:
            return None, "expired"

        user = json.loads(parsed.get("user", "{}") or "{}")
        if not user.get("id"):
            return None, "no_user"
        return user, "ok"
    except Exception as exc:
        log.warning("init_data parse error: %s", exc)
        return None, "malformed"


SESSION_ERRORS = {
    "missing": (
        "Telegram session missing. Open the Mini App using the button inside the "
        "bot in Telegram - opening this link in a browser will not work."
    ),
    "no_hash": (
        "Telegram session data is incomplete. Reopen the Mini App from the bot button."
    ),
    "bad_signature": (
        "Session signature mismatch. The bot token may have changed. Restart the bot "
        "and server, then reopen the Mini App from the bot."
    ),
    "expired": "Session expired. Reopen the Mini App from the bot button.",
    "no_user": "Telegram user data missing. Reopen the Mini App from the bot button.",
    "no_token": "BOT_TOKEN is not configured on the server.",
    "malformed": "Session data could not be read. Reopen the Mini App from the bot.",
}


def _user_from(body: dict[str, Any]) -> dict[str, Any]:
    tg, reason = verify_init_data(str(body.get("init_data", "")))
    if not tg:
        log.warning("init_data rejected: %s", reason)
        raise ValueError(SESSION_ERRORS.get(reason, "Invalid Telegram session."))
    user = core.ensure_user(int(tg["id"]), tg.get("username"), tg.get("first_name"))
    if not _is_channel_member(user["user_id"]):
        channels = ", ".join(f"@{c}" for c in config.FORCE_CHANNELS)
        raise ValueError(
            f"Join {channels} first, then reopen the Mini App from the bot."
        )
    return user


# Force channel join — mirrors the bot's check (60s cache, fails open so a
# Telegram API hiccup never locks users out).
_join_cache: dict[int, tuple[bool, float]] = {}
_JOIN_TTL = 60


def _is_channel_member(user_id: int) -> bool:
    if not config.FORCE_JOIN or not config.FORCE_CHANNELS or not config.BOT_TOKEN:
        return True
    if user_id in config.ADMIN_IDS:
        return True
    hit = _join_cache.get(user_id)
    if hit and time.time() - hit[1] < _JOIN_TTL:
        return hit[0]

    ok = True
    for channel in config.FORCE_CHANNELS:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/getChatMember",
                params={"chat_id": f"@{channel}", "user_id": user_id},
                timeout=6,
            )
            data = resp.json() or {}
            # A refused call (bot is not a channel admin, so the member list is
            # hidden) must never lock users out — fail open.
            if not data.get("ok") or "result" not in data:
                ok = True
                break
            status = str((data.get("result") or {}).get("status") or "").lower()
            if status not in ("creator", "administrator", "member"):
                ok = False
                break
        except Exception:
            ok = True  # fail open — never block on a network error
            break
    _join_cache[user_id] = (ok, time.time())
    return ok


def ok(data: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data})


def fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


# ───────────────────────────── routes ──────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_text = WEBAPP_FILE.read_text(encoding="utf-8")
    # Static hosts (e.g. Netlify) need a hardcoded backend URL in the file.
    # When served from here, leave PUBLIC_API_URL empty for same-origin calls.
    return HTMLResponse(html_text.replace("__FLUX_API_BASE__", config.PUBLIC_API_URL))


@app.get("/health")
async def health() -> JSONResponse:
    return ok({"status": "alive", "time": int(time.time())})


@app.get("/diag")
async def diag() -> HTMLResponse:
    """Small self-test page: shows whether the browser sent Telegram init data."""
    return HTMLResponse(
        """<!doctype html><meta charset="utf-8"><title>FluxProxy Diagnostics</title>
        <style>body{font:14px ui-monospace,monospace;background:#0b1020;color:#e6edf3;padding:24px}
        b{color:#5eead4} .bad{color:#fb7185}</style>
        <h2>FluxProxy Diagnostics</h2><div id="d">checking...</div>
        <script>
        const raw = Telegram && Telegram.WebApp ? Telegram.WebApp.initData : '';
        const box = document.getElementById('d');
        if (!raw) {
          box.innerHTML = '<span class="bad">NO TELEGRAM SESSION DATA.</span><br><br>' +
            'You opened this page outside the Telegram Mini App context.<br>' +
            'Open the Mini App from the button inside your bot in Telegram.';
        } else {
          box.innerHTML = 'init_data length: <b>' + raw.length + '</b><br>' +
            'contains hash: <b>' + (raw.indexOf('hash=') !== -1) + '</b><br>' +
            'Telegram WebApp object detected.';
        }
        </script>"""
    )


@app.post("/api/session")
async def api_session(request: Request) -> JSONResponse:
    try:
        user = _user_from(await _body(request))
        return ok(core.get_session(user["user_id"]))
    except ValueError as e:
        return fail(str(e), 401)
    except Exception as e:
        log.exception("session failed")
        return fail(f"Server error: {e}", 500)


@app.post("/api/key/apply")
async def api_apply_key(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        user = _user_from(body)
        success, message, expires = core.apply_key(
            user["user_id"], str(body.get("key", ""))
        )
        if not success:
            return fail(message)
        return ok({"message": message, "session": core.get_session(user["user_id"])})
    except ValueError as e:
        return fail(str(e), 401)
    except Exception as e:
        log.exception("apply failed")
        return fail(f"Server error: {e}", 500)


@app.post("/api/status")
async def api_status(request: Request) -> JSONResponse:
    try:
        user = _user_from(await _body(request))
        return ok(core.get_session(user["user_id"]))
    except ValueError as e:
        return fail(str(e), 401)
    except Exception as e:
        log.exception("status failed")
        return fail(f"Server error: {e}", 500)


def _proxy_txt(proxies: list, requested: int) -> str:
    """Plain-text proxy list — paste-ready, with speed details underneath."""
    lines = [
        "FluxProxy - Working Proxies",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Count: {len(proxies)} of {requested} requested",
        "",
        "PROXIES",
        "=" * 44,
    ]
    lines.extend(p.url for p in proxies)
    lines += ["", "SPEED & CAPACITY", "=" * 44]
    for p in proxies:
        lines.append(
            f"{p.url} | {p.speed_kbps:g} KB/s | {p.transfer_1h_mb:g} MB/h "
            f"| {p.tier or 'OK'}"
        )
    lines.append("")
    return "\n".join(lines)


async def _deliver_proxy_file(user_id: int, proxies: list, requested: int) -> None:
    """Send the working proxy list to the user's bot chat as a .txt document."""
    if not proxies or not config.BOT_TOKEN:
        return
    now = time.time()
    if now - _last_delivery.get(user_id, 0.0) < DELIVERY_COOLDOWN:
        return
    chat_id = core.get_chat_id(user_id)
    if not chat_id:
        log.info("No chat_id stored for user %s — proxy file not sent", user_id)
        return

    text = _proxy_txt(proxies, requested)
    filename = f"fluxproxy_{time.strftime('%Y%m%d_%H%M%S')}.txt"

    def _send() -> requests.Response:
        return requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument",
            data={
                "chat_id": chat_id,
                "caption": f"FluxProxy: {len(proxies)} working proxies ready.",
            },
            files={"document": (filename, text.encode("utf-8"), "text/plain")},
            timeout=25,
        )

    try:
        resp = await asyncio.to_thread(_send)
        if resp.status_code == 200:
            _last_delivery[user_id] = now
            log.info("Sent %s proxies to chat %s", len(proxies), chat_id)
        else:
            log.warning(
                "sendDocument failed for chat %s: %s %s",
                chat_id,
                resp.status_code,
                resp.text[:200],
            )
    except Exception:
        log.exception("sendDocument failed for chat %s", chat_id)


async def _run_proxy_job(job_id: str, count: int, protocol: Optional[str]) -> None:
    """Generate proxies in the background so no single HTTP call stays open."""
    cache_key = (count, protocol)
    try:
        async with _proxy_lock:
            cached = _proxy_cache.get(cache_key)
            if cached and time.time() - cached[0] < PROXY_CACHE_SECONDS:
                _jobs[job_id].update(status="done", proxies=cached[1])
                return

        log.info("Job %s: generating %s proxies (protocol=%s)", job_id, count, protocol)
        t0 = time.monotonic()
        proxies = await asyncio.to_thread(
            proxy_engine.get_tested_proxies,
            count,
            protocol,
            None,
            "IN",
            config.DEFAULT_PROXY_WORKERS,
        )
        _proxy_cache[cache_key] = (time.time(), proxies)
        _jobs[job_id].update(status="done", proxies=proxies)
        log.info(
            "Job %s: %s proxies in %.1fs", job_id, len(proxies), time.monotonic() - t0
        )
    except Exception as e:
        log.exception("proxy job %s failed", job_id)
        _jobs[job_id].update(status="error", error=str(e))


@app.post("/api/proxies")
async def api_proxies(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        user = _user_from(body)

        if not core.active_key(user["user_id"]):
            return fail(
                "No active key. Generate a key in the bot, then apply it here.",
                403,
            )

        try:
            count = int(body.get("count", 10))
        except (TypeError, ValueError):
            count = 10
        count = max(1, min(count, config.MAX_PROXIES_PER_REQUEST))

        protocol = str(body.get("protocol", "all") or "all").lower()
        if protocol not in (*proxy_engine.PROTOCOLS, "all"):
            protocol = "all"
        protocol = None if protocol == "all" else protocol

        for old_id in list(_jobs):
            if time.time() - _jobs[old_id]["created"] > JOB_TTL_SECONDS:
                _jobs.pop(old_id, None)

        cache_key = (count, protocol)
        cached = _proxy_cache.get(cache_key)
        if cached and time.time() - cached[0] < PROXY_CACHE_SECONDS:
            core.note_proxy_generation(user["user_id"])
            asyncio.create_task(
                _deliver_proxy_file(user["user_id"], cached[1], count)
            )
            return ok(
                {
                    "done": True,
                    "proxies": [p.to_dict() for p in cached[1]],
                    "count": len(cached[1]),
                    "session": core.get_session(user["user_id"]),
                }
            )

        # Reuse an existing job with the same parameters if one is running.
        for job_id, job in _jobs.items():
            if job["key"] == cache_key and job["status"] == "running":
                return ok({"done": False, "job_id": job_id})

        job_id = secrets.token_urlsafe(8)
        _jobs[job_id] = {
            "key": cache_key,
            "status": "running",
            "created": time.time(),
            "requested": count,
        }
        asyncio.create_task(_run_proxy_job(job_id, count, protocol))
        return ok({"done": False, "job_id": job_id})
    except ValueError as e:
        return fail(str(e), 401)
    except Exception as e:
        log.exception("proxy generation failed")
        return fail(f"Server error: {e}", 500)


@app.post("/api/proxies/result")
async def api_proxy_result(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        user = _user_from(body)
        job_id = str(body.get("job_id", ""))
        job = _jobs.get(job_id)
        if not job:
            return fail("Unknown or expired generation job.", 404)

        if job["status"] == "running":
            if time.time() - job["created"] > JOB_TTL_SECONDS:
                job["status"] = "error"
                job["error"] = "Generation timed out. Please try again."
            else:
                return ok({"done": False})

        if job["status"] == "error":
            return fail(job.get("error", "Generation failed."), 500)

        proxies = job["proxies"]
        core.note_proxy_generation(user["user_id"])
        asyncio.create_task(
            _deliver_proxy_file(
                user["user_id"], proxies, int(job.get("requested", len(proxies)))
            )
        )
        return ok(
            {
                "done": True,
                "proxies": [p.to_dict() for p in proxies],
                "count": len(proxies),
                "session": core.get_session(user["user_id"]),
            }
        )
    except ValueError as e:
        return fail(str(e), 401)
    except Exception as e:
        log.exception("proxy result failed")
        return fail(f"Server error: {e}", 500)


def main() -> None:
    log.info("Mini app → http://%s:%s", config.SERVER_HOST, config.SERVER_PORT)
    uvicorn.run(
        app,
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        log_level=config.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
