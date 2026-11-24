"""Configuration for FluxProxy Bot + Mini App. All values overridable via .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "FluxProxyBot")

# Public HTTPS URL of the Mini App (this FastAPI server).
# tunnel.py writes webapp_url.txt automatically; it takes priority.
_url_file = Path(__file__).parent / "webapp_url.txt"
_dynamic_url = _url_file.read_text(encoding="utf-8").strip() if _url_file.exists() else ""
WEBAPP_URL: str = (
    _dynamic_url or os.getenv("WEBAPP_URL", "https://fluxyproxy.netlify.app")
).rstrip("/")

# Public HTTPS URL of the FastAPI backend (where /api/* is served).
# Leave empty when the Mini App is hosted by this same server.
PUBLIC_API_URL: str = os.getenv("PUBLIC_API_URL", "").rstrip("/")

# Administrators — unrestricted key generation and the /admin panel.
# Add your numeric Telegram ID and/or your username (without @).
# Get your ID from @userinfobot in Telegram.
ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "1364476174").split(",")
    if x.strip().isdigit()
}
ADMIN_USERNAMES: set[str] = {
    x.strip().lower().lstrip("@")
    for x in os.getenv("ADMIN_USERNAMES", "Kmx6999").split(",")
    if x.strip()
}

# Storage
DB_PATH: Path = Path(os.getenv("DB_PATH", "fluxproxy.db"))

# Server — PaaS platforms (Railway, Render, Heroku) inject $PORT.
SERVER_HOST: str = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT: int = int(os.getenv("PORT") or os.getenv("SERVER_PORT", "8000"))

# Key rules
KEY_DURATION_MINUTES: int = int(os.getenv("KEY_DURATION_MINUTES", "20"))
KEY_CLAIM_HOURS: int = int(os.getenv("KEY_CLAIM_HOURS", "24"))
STARTER_CREDITS: int = int(os.getenv("STARTER_CREDITS", "1"))

# Proxy rules — max 10 per generation keeps quality high and results reliable.
MAX_PROXIES_PER_REQUEST: int = int(os.getenv("MAX_PROXIES_PER_REQUEST", "10"))
DEFAULT_PROXY_WORKERS: int = int(os.getenv("DEFAULT_PROXY_WORKERS", "120"))

# Force channel join (required before keys/referrals work).
FORCE_JOIN: bool = os.getenv("FORCE_JOIN", "true").strip().lower() in ("1", "true", "yes")
FORCE_CHANNELS: list[str] = [
    c.strip().lstrip("@")
    for c in os.getenv("FORCE_CHANNELS", "viediet").split(",")
    if c.strip()
]

# Logging
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


def validate() -> list[str]:
    """Return a list of human-readable config problems."""
    problems: list[str] = []
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN is not set")
    if not BOT_USERNAME:
        problems.append("BOT_USERNAME is not set")
    if "your-domain" in WEBAPP_URL:
        problems.append("WEBAPP_URL is still the placeholder")
    return problems
