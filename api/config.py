"""
config.py — centralised configuration loaded from .env
All secrets are pulled from environment variables ONLY.
"""
import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# Fix: Look for .env in the project root (BeeTrackDz), not in api folder
BASE_DIR = Path(__file__).resolve().parent.parent  # Goes up one level from api/ to project root
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path)

# Debug: Check if .env file was found
if not env_path.exists():
    print(f"⚠️ WARNING: .env file not found at {env_path}")
else:
    print(f"✅ Loading .env from {env_path}")

logger = logging.getLogger(__name__)

# ── Database ────────────────────────────────────────────────────────────────
IS_SERVERLESS = bool(
    os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
)

DB_PATH = "/tmp/smart_apiary.db" if IS_SERVERLESS else "smart_apiary.db"

# ── Auth ─────────────────────────────────────────────────────────────────────
SECRET_SALT: str = os.getenv("SECRET_SALT", "")
TOKEN_TTL_HOURS: int = int(os.getenv("TOKEN_TTL_HOURS", "8"))

# ── IoT ingestion ────────────────────────────────────────────────────────────
IOT_API_KEY: str = os.getenv("IOT_API_KEY", "")

# ── MQTT ─────────────────────────────────────────────────────────────────────
MQTT_BROKER: str       = os.getenv("MQTT_BROKER", "")
MQTT_PORT: int         = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USERNAME: str     = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD: str     = os.getenv("MQTT_PASSWORD", "")
MQTT_TOPIC: str        = os.getenv("MQTT_TOPIC", "rucher/data")
MQTT_TLS_ENABLED: bool = os.getenv("MQTT_TLS_ENABLED", "true").lower() == "true"

# ── Server ───────────────────────────────────────────────────────────────────
SERVER_HOST: str = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT: int = int(os.getenv("SERVER_PORT", "8000"))

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ── Email / SMTP ──────────────────────────────────────────────────────────────
SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("SMTP_USER", "")
SMTP_PASS: str = os.getenv("SMTP_PASS", "")

# ── Twilio ───────────────────────────────────────────────────────────────────
TWILIO_SID: str   = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN: str = os.getenv("TWILIO_TOKEN", "")
TWILIO_FROM: str  = os.getenv("TWILIO_FROM", "")

# ── Rate Limiting ────────────────────────────────────────────────────────────
RATE_LIMIT_GLOBAL: int = int(os.getenv("RATE_LIMIT_GLOBAL", "50"))
RATE_LIMIT_AUTH: int   = int(os.getenv("RATE_LIMIT_AUTH", "10"))
RATE_WINDOW_SECS: int  = int(os.getenv("RATE_WINDOW_SECS", "60"))

# ── CORS ─────────────────────────────────────────────────────────────────────
_cors_raw = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS: list[str] = [o.strip() for o in _cors_raw.split(",") if o.strip()]

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Startup warnings ──────────────────────────────────────────────────────────
def _warn_defaults() -> None:
    if not SECRET_SALT:
        logger.critical(
            "SECRET_SALT is not set. "
            "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
        sys.exit(1)
    if not IOT_API_KEY:
        logger.warning(
            "IOT_API_KEY is not set — /data endpoint is effectively open. "
            "Set IOT_API_KEY in .env to secure IoT ingestion."
        )
    if not MQTT_BROKER:
        logger.warning("MQTT_BROKER is not set — MQTT will be skipped.")
    if not MQTT_USERNAME:
        logger.warning("MQTT_USERNAME is not set — MQTT will be skipped.")