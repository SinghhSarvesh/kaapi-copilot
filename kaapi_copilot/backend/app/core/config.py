"""
Central configuration for Kaapi Copilot.
Reads env vars (and a .env file in the backend directory) then decides mock vs
real mode for payments and AI agent. Missing secrets automatically force the
corresponding subsystem into mock mode.
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

# Load .env from the backend directory (the directory that contains this file's
# parent package). Works regardless of where uvicorn is launched from.
try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).resolve().parent.parent.parent / ".env"
    load_dotenv(_env_file, override=False)  # override=False: real env vars take priority
except ImportError:
    pass  # python-dotenv is optional; env vars must be set manually if absent

logger = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int_env(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        logger.warning("Config: %s=%r is not a valid integer; using default %d", name, raw, default)
        return default


@dataclass
class Settings:
    # ---- Payment provider ----
    payment_mode_requested: str = field(default_factory=lambda: _env("PAYMENT_MODE", "mock").lower())
    razorpay_key_id: str = field(default_factory=lambda: _env("RAZORPAY_KEY_ID"))
    razorpay_key_secret: str = field(default_factory=lambda: _env("RAZORPAY_KEY_SECRET"))
    razorpay_webhook_secret: str = field(default_factory=lambda: _env("RAZORPAY_WEBHOOK_SECRET"))

    # ---- AI agent provider ----
    agent_mode_requested: str = field(default_factory=lambda: _env("AGENT_MODE", "mock").lower())
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY"))
    groq_model: str = field(default_factory=lambda: _env("GROQ_MODEL", "openai/gpt-oss-120b"))

    # ---- Guardrail limits (paise) ----
    session_spend_cap_paise: int = field(default_factory=lambda: _int_env("SESSION_SPEND_CAP_PAISE", 500000))      # ₹5,000
    transaction_spend_cap_paise: int = field(default_factory=lambda: _int_env("TRANSACTION_SPEND_CAP_PAISE", 300000))  # ₹3,000
    max_upsells_per_turn: int = 1

    # ---- Misc ----
    cart_hold_minutes: int = field(default_factory=lambda: _int_env("CART_HOLD_MINUTES", 10))
    db_path: str = field(default_factory=lambda: _env(
        "KAAPI_DB_PATH",
        "/data/kaapi_copilot.db" if Path("/data").exists() else "kaapi_copilot.db",
    ))
    webhook_base_url: str = field(default_factory=lambda: _env("WEBHOOK_BASE_URL", "http://localhost:8000"))
    # Comma-separated list of allowed CORS origins. Defaults to all (*) for local dev.
    allowed_origins: list = field(default_factory=lambda: _env("ALLOWED_ORIGINS", "*").split(","))

    @property
    def payment_mode(self) -> str:
        """Fall back to mock if razorpay requested but secrets missing."""
        if self.payment_mode_requested == "razorpay" and self.razorpay_key_id and self.razorpay_key_secret:
            return "razorpay"
        return "mock"

    @property
    def agent_mode(self) -> str:
        """Fall back to mock if groq requested but key missing."""
        if self.agent_mode_requested == "groq" and self.groq_api_key:
            return "groq"
        return "mock"

    def summary(self) -> dict:
        return {
            "payment_mode_requested": self.payment_mode_requested,
            "payment_mode_effective": self.payment_mode,
            "razorpay_keys_present": bool(self.razorpay_key_id and self.razorpay_key_secret),
            "agent_mode_requested": self.agent_mode_requested,
            "agent_mode_effective": self.agent_mode,
            "groq_key_present": bool(self.groq_api_key),
            "session_spend_cap_paise": self.session_spend_cap_paise,
            "transaction_spend_cap_paise": self.transaction_spend_cap_paise,
        }


settings = Settings()
