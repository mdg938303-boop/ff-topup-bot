"""
Central configuration — everything comes from environment variables.

Render notes:
  * DATABASE_URL  — injected from the Render Postgres service (render.yaml).
                    postgres:// / postgresql:// URLs are converted to the asyncpg
                    driver automatically (and Neon-style ?sslmode=... is fixed).
  * REDIS_URL     — injected from the Render Key Value service (optional).
  * RENDER_EXTERNAL_URL / PORT — injected by Render automatically.
"""
import hashlib
import os
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _decimal_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except InvalidOperation:
        return Decimal(default)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def normalize_database_url(url: str) -> str:
    """
    Accepts whatever the hosting provider gives us and returns a SQLAlchemy
    async URL:
      postgres://...      -> postgresql+asyncpg://...
      postgresql://...    -> postgresql+asyncpg://...
      ?sslmode=require    -> ?ssl=require        (asyncpg spelling)
      ?channel_binding=.. -> removed             (asyncpg rejects it)
    """
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        if "sslmode" in query:
            query["ssl"] = query.pop("sslmode")
        query.pop("channel_binding", None)
        url = urlunsplit(parts._replace(query=urlencode(query)))
    return url


BOT_TOKEN: str = _require("BOT_TOKEN")
DATABASE_URL: str = normalize_database_url(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db"))
REDIS_URL: str | None = os.getenv("REDIS_URL") or None

ADMIN_IDS: set[int] = set()
for _x in os.getenv("ADMIN_IDS", "").split(","):
    _x = _x.strip()
    if _x.lstrip("-").isdigit():
        ADMIN_IDS.add(int(_x))

SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "@support")

CURRENCY: str = "৳"
PAGE_SIZE: int = 6
GAME_NAME: str = "Free Fire"
SERVER_LABEL: str = "Bangladesh (BD) Server"

# ---- Limits (all overridable from the environment) -------------------------
MIN_DEPOSIT: Decimal = _decimal_env("MIN_DEPOSIT", "10")
MAX_DEPOSIT: Decimal = _decimal_env("MAX_DEPOSIT", "50000")
MAX_PENDING_DEPOSITS_PER_USER: int = _int_env("MAX_PENDING_DEPOSITS_PER_USER", 3)
REFERRAL_MIN_DEPOSIT: Decimal = _decimal_env("REFERRAL_MIN_DEPOSIT", "50")
ORDER_STUCK_MINUTES: int = _int_env("ORDER_STUCK_MINUTES", 30)

# ---- Security --------------------------------------------------------------
# Encrypts provider API keys / gateway keys stored in the database.
# Leave empty to store them in plaintext (not recommended in production).
SECRET_ENCRYPTION_KEY: str | None = os.getenv("SECRET_ENCRYPTION_KEY") or None

# Telegram sends this in X-Telegram-Bot-Api-Secret-Token on every webhook call;
# requests without it are rejected. Derived from the bot token if not set.
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(
    ("webhook-secret:" + BOT_TOKEN).encode()
).hexdigest()[:48]

# Public HTTPS base URL (Render injects RENDER_EXTERNAL_URL). Needed for the
# Telegram webhook and for the Mr Ai Pay payment redirect pages.
PUBLIC_BASE_URL: str | None = (
    (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_BASE_URL") or "").rstrip("/") or None
)
