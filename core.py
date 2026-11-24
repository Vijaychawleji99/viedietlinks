"""
core.py — SQLite storage, random key generation, referral accounting.

All public functions are thread-safe and safe to call from both the bot
process and the FastAPI process (SQLite in WAL mode + a process-local lock).
"""
from __future__ import annotations

import secrets
import sqlite3
import string
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import config

_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()

KEY_ALPHABET = string.ascii_uppercase + string.digits
KEY_LENGTH = 12


# ──────────────────────────────── setup ────────────────────────────────
def init_db() -> None:
    """Create the database and tables. Safe to call multiple times."""
    global _conn
    with _lock:
        if _conn is not None:
            return
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(
            str(config.DB_PATH), check_same_thread=False, timeout=30
        )
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=30000")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                first_name     TEXT,
                ref_code       TEXT UNIQUE NOT NULL,
                referred_by    INTEGER,
                credits        INTEGER NOT NULL DEFAULT 1,
                total_referrals INTEGER NOT NULL DEFAULT 0,
                created_at     INTEGER NOT NULL,
                chat_id        INTEGER
            );

            CREATE TABLE IF NOT EXISTS keys (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                key               TEXT UNIQUE NOT NULL,
                user_id           INTEGER NOT NULL,
                generated_at      INTEGER NOT NULL,
                applied_at        INTEGER,
                expires_at        INTEGER,
                status            TEXT NOT NULL DEFAULT 'unused',
                proxies_generated INTEGER NOT NULL DEFAULT 0,
                duration_minutes  INTEGER,
                max_uses          INTEGER NOT NULL DEFAULT 0,
                uses              INTEGER NOT NULL DEFAULT 0,
                redeemed_gift     TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_keys_user ON keys(user_id);
            CREATE INDEX IF NOT EXISTS idx_keys_key  ON keys(key);

            CREATE TABLE IF NOT EXISTS referrals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                created_at  INTEGER NOT NULL,
                rewarded    INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        _migrate()
        _conn.commit()


def _migrate() -> None:
    """Add columns introduced after the first release (safe to run repeatedly)."""
    assert _conn is not None
    want = {
        "users": {"chat_id": "INTEGER"},
        "keys": {
            "duration_minutes": "INTEGER",
            "max_uses": "INTEGER NOT NULL DEFAULT 0",
            "uses": "INTEGER NOT NULL DEFAULT 0",
            "redeemed_gift": "TEXT",
        },
    }
    for table, columns in want.items():
        existing = {
            row["name"] for row in _conn.execute(f"PRAGMA table_info({table})")
        }
        for name, decl in columns.items():
            if name not in existing:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    with _lock:
        assert _conn is not None, "core.init_db() was not called"
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def _now() -> int:
    return int(time.time())


def _new_ref_code() -> str:
    return "R" + secrets.token_hex(3).upper()


def _new_key() -> str:
    """12 uppercase alphanumeric characters with at least one letter and one digit."""
    rng = secrets.SystemRandom()
    chars = [
        rng.choice(string.ascii_uppercase),
        rng.choice(string.digits),
        rng.choice(string.ascii_uppercase),
        rng.choice(string.digits),
    ]
    chars += [rng.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH - len(chars))]
    rng.shuffle(chars)
    return "".join(chars)


# ──────────────────────────────── users ────────────────────────────────
def get_user(user_id: int) -> Optional[dict[str, Any]]:
    with transaction() as c:
        row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_ref_code(ref_code: str) -> Optional[dict[str, Any]]:
    with transaction() as c:
        row = c.execute(
            "SELECT * FROM users WHERE ref_code = ?", (ref_code.strip().upper(),)
        ).fetchone()
        return dict(row) if row else None


def ensure_user(
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    chat_id: Optional[int] = None,
) -> dict[str, Any]:
    """Insert a user if missing, then return the current row.

    `chat_id` is remembered so the server can deliver proxy files to the bot.
    """
    with transaction() as c:
        row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            # Make sure the random ref code is unique (collisions are rare).
            while True:
                code = _new_ref_code()
                if c.execute(
                    "SELECT 1 FROM users WHERE ref_code = ?", (code,)
                ).fetchone() is None:
                    break
            c.execute(
                """
                INSERT INTO users
                    (user_id, username, first_name, ref_code, credits, created_at, chat_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username or "",
                    first_name or "",
                    code,
                    config.STARTER_CREDITS,
                    _now(),
                    chat_id,
                ),
            )
        else:
            # Keep profile fields fresh.
            c.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username or row["username"], first_name or row["first_name"], user_id),
            )
            if chat_id is not None and chat_id != row["chat_id"]:
                c.execute(
                    "UPDATE users SET chat_id = ? WHERE user_id = ?",
                    (chat_id, user_id),
                )
        row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row)


def get_chat_id(user_id: int) -> Optional[int]:
    """Private-chat id used to deliver proxy files through the bot."""
    with transaction() as c:
        row = c.execute(
            "SELECT chat_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["chat_id"]) if row and row["chat_id"] else None


def is_admin(user_id: int, username: Optional[str] = None) -> bool:
    """Admins have unlimited key generation and access to /admin."""
    if user_id in config.ADMIN_IDS:
        return True
    user = get_user(user_id) or {}
    name = (username or user.get("username", "") or "").strip().lower().lstrip("@")
    return bool(name and name in config.ADMIN_USERNAMES)


# ──────────────────────────────── referrals ────────────────────────────
def set_pending_referral(user_id: int, referrer_id: int) -> None:
    """Remember a referral that is waiting for the channel join."""
    with transaction() as c:
        c.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ?",
            (referrer_id, user_id),
        )


def take_pending_referral(user_id: int) -> Optional[int]:
    """Return the waiting referrer id once, then clear it."""
    with transaction() as c:
        row = c.execute(
            "SELECT referred_by FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        ref = int(row["referred_by"]) if row and row["referred_by"] else None
        if ref:
            c.execute(
                "UPDATE users SET referred_by = NULL WHERE user_id = ?", (user_id,)
            )
        return ref


def register_referral(referrer_id: int, referred_id: int) -> bool:
    """
    Reward both sides once for a valid referral.
    Returns True only when the referral was newly recorded.
    """
    if referrer_id == referred_id:
        return False
    with transaction() as c:
        referrer = c.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)
        ).fetchone()
        referred = c.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (referred_id,)
        ).fetchone()
        if not referrer or not referred:
            return False
        if c.execute(
            "SELECT 1 FROM referrals WHERE referred_id = ?", (referred_id,)
        ).fetchone():
            return False

        c.execute(
            """
            INSERT INTO referrals (referrer_id, referred_id, created_at, rewarded)
            VALUES (?, ?, ?, 1)
            """,
            (referrer_id, referred_id, _now()),
        )
        c.execute(
            """
            UPDATE users
               SET credits = credits + 1, total_referrals = total_referrals + 1
             WHERE user_id = ?
            """,
            (referrer_id,),
        )
        c.execute(
            "UPDATE users SET credits = credits + 1 WHERE user_id = ?", (referred_id,)
        )
        return True


# ──────────────────────────────── keys ─────────────────────────────────
def generate_key(user_id: int) -> Optional[str]:
    """Mint a key. Admins are never charged and never limited."""
    admin = is_admin(user_id)
    with transaction() as c:
        if not admin:
            row = c.execute(
                "SELECT credits FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row or row["credits"] < 1:
                return None

        for _ in range(10):
            key = _new_key()
            if c.execute("SELECT 1 FROM keys WHERE key = ?", (key,)).fetchone() is None:
                break
        else:
            return None

        c.execute(
            """
            INSERT INTO keys (key, user_id, generated_at, status)
            VALUES (?, ?, ?, 'unused')
            """,
            (key, user_id, _now()),
        )
        if not admin:
            c.execute(
                "UPDATE users SET credits = credits - 1 WHERE user_id = ?", (user_id,)
            )
        return key


def generate_gift_key(
    admin_id: int,
    duration_minutes: int,
    max_uses: int = 0,
) -> Optional[str]:
    """Mint a shareable redeem code (admin only).

    `max_uses = 0` means unlimited redemptions. Every user who redeems it gets
    their own active session lasting `duration_minutes`.
    """
    duration = max(1, int(duration_minutes))
    uses = max(0, int(max_uses))
    with transaction() as c:
        for _ in range(12):
            key = _new_key()
            if c.execute("SELECT 1 FROM keys WHERE key = ?", (key,)).fetchone() is None:
                break
        else:
            return None
        c.execute(
            """
            INSERT INTO keys
                (key, user_id, generated_at, status, duration_minutes, max_uses, uses)
            VALUES (?, ?, ?, 'gift', ?, ?, 0)
            """,
            (key, admin_id, _now(), duration, uses),
        )
        return key


def apply_key(user_id: int, key: str) -> tuple[bool, str, Optional[int]]:
    """
    Activate a key. The timer starts NOW (on apply).
    Personal keys work for their owner only; gift codes work for everybody.
    Returns (success, message, expires_at).
    """
    clean = (key or "").strip().upper()
    if not clean:
        return False, "Key empty.", None

    with transaction() as c:
        row = c.execute("SELECT * FROM keys WHERE UPPER(key) = ?", (clean,)).fetchone()
        if not row:
            return False, "Invalid key. Get a new key from the bot.", None

        now = _now()

        # ── Gift / redeem code ──────────────────────────────────────────
        if row["status"] == "gift":
            if row["max_uses"] > 0 and row["uses"] >= row["max_uses"]:
                return False, "This gift code has reached its usage limit.", None
            if c.execute(
                "SELECT 1 FROM keys WHERE user_id = ? AND redeemed_gift = ?",
                (user_id, row["key"]),
            ).fetchone():
                return False, "You already redeemed this gift code.", None

            duration = int(row["duration_minutes"] or config.KEY_DURATION_MINUTES)
            expires = now + duration * 60
            current = c.execute(
                """
                SELECT id, expires_at FROM keys
                 WHERE user_id = ? AND status = 'active' AND expires_at > ?
                 ORDER BY expires_at DESC LIMIT 1
                """,
                (user_id, now),
            ).fetchone()
            if current:
                # Never shorten an existing session — only extend it.
                expires = max(expires, int(current["expires_at"]))
                c.execute(
                    "UPDATE keys SET expires_at = ?, redeemed_gift = ? WHERE id = ?",
                    (expires, row["key"], current["id"]),
                )
            else:
                personal = _new_key()
                c.execute(
                    """
                    INSERT INTO keys
                        (key, user_id, generated_at, applied_at, expires_at,
                         status, redeemed_gift)
                    VALUES (?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (personal, user_id, now, now, expires, row["key"]),
                )
            c.execute("UPDATE keys SET uses = uses + 1 WHERE id = ?", (row["id"],))
            return True, f"Gift code redeemed! {duration} minutes of access activated.", expires

        # ── Personal key ────────────────────────────────────────────────
        if int(row["user_id"]) != int(user_id):
            return False, "This key does not belong to you. Get your key from the bot.", None

        if row["status"] == "active" and row["expires_at"] and row["expires_at"] > now:
            return True, "Key already active.", row["expires_at"]

        if row["status"] == "unused":
            claim_deadline = row["generated_at"] + config.KEY_CLAIM_HOURS * 3600
            if now > claim_deadline:
                c.execute(
                    "UPDATE keys SET status = 'expired' WHERE id = ?", (row["id"],)
                )
                return False, "This key was not activated in time. Get a new one from the bot.", None

            expires = now + config.KEY_DURATION_MINUTES * 60
            c.execute(
                """
                UPDATE keys
                   SET status = 'active', applied_at = ?, expires_at = ?
                 WHERE id = ?
                """,
                (now, expires, row["id"]),
            )
            return True, "Key activated!", expires

        return False, "This key has expired. Generate a new one from the bot.", None


def key_count(user_id: int) -> int:
    """How many keys this user has ever generated."""
    with transaction() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM keys WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["n"]) if row else 0


def active_key(user_id: int) -> Optional[dict[str, Any]]:
    """Return the currently active, unexpired key row (or None)."""
    with transaction() as c:
        row = c.execute(
            """
            SELECT * FROM keys
             WHERE user_id = ? AND status = 'active' AND expires_at > ?
             ORDER BY expires_at DESC LIMIT 1
            """,
            (user_id, _now()),
        ).fetchone()
        return dict(row) if row else None


def note_proxy_generation(user_id: int) -> None:
    """Bookkeeping only — generation stays unlimited while the key is live."""
    with transaction() as c:
        c.execute(
            """
            UPDATE keys SET proxies_generated = proxies_generated + 1
             WHERE id = (
                SELECT id FROM keys
                 WHERE user_id = ? AND status = 'active' AND expires_at > ?
                 ORDER BY expires_at DESC LIMIT 1
             )
            """,
            (user_id, _now()),
        )


def get_session(user_id: int) -> dict[str, Any]:
    """Everything the mini-app needs in one call."""
    user = get_user(user_id) or {}
    key = active_key(user_id)
    now = _now()
    return {
        "user": {
            "id": user.get("user_id"),
            "username": user.get("username", ""),
            "first_name": user.get("first_name", ""),
        },
        "key": (
            {
                "key": key["key"],
                "expires_at": key["expires_at"],
                "seconds_left": max(0, key["expires_at"] - now),
                "generated": key["proxies_generated"],
            }
            if key
            else None
        ),
        "server_time": now,
        "key_duration_minutes": config.KEY_DURATION_MINUTES,
    }


# ──────────────────────────────── admin ────────────────────────────────
def get_stats() -> dict[str, int]:
    """Database overview for the admin panel."""
    with transaction() as c:
        users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        keys = c.execute("SELECT COUNT(*) AS n FROM keys").fetchone()["n"]
        active = c.execute(
            "SELECT COUNT(*) AS n FROM keys WHERE status = 'active' AND expires_at > ?",
            (_now(),),
        ).fetchone()["n"]
        referrals = c.execute("SELECT COUNT(*) AS n FROM referrals").fetchone()["n"]
        gifts = c.execute("SELECT COUNT(*) AS n FROM keys WHERE status = 'gift'").fetchone()["n"]
        return {
            "users": int(users),
            "keys": int(keys),
            "active_keys": int(active),
            "referrals": int(referrals),
            "gift_codes": int(gifts),
        }


def clear_all_data() -> None:
    """Remove every user, key and referral (admin only)."""
    with transaction() as c:
        c.execute("DELETE FROM keys")
        c.execute("DELETE FROM referrals")
        c.execute("DELETE FROM users")
