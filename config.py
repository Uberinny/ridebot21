#!/usr/bin/env python3
"""Load / save partner bot settings (.env) with plain-language helpers for the setup wizard."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

# Shown in the setup wizard. order = display order.
FIELDS: list[dict[str, Any]] = [
    {
        "key": "DISCORD_TOKEN",
        "label": "Discord bot token",
        "help": "From Discord Developer Portal → your app → Bot → Reset Token / Copy.",
        "required": True,
        "secret": True,
        "placeholder": "Paste the bot token here",
        "section": "Discord",
    },
    {
        "key": "GUILD_ID",
        "label": "Discord server ID (optional)",
        "help": "Right-click your server → Copy Server ID (enable Developer Mode). Makes slash commands appear instantly.",
        "required": False,
        "placeholder": "e.g. 123456789012345678",
        "section": "Discord",
    },
    {
        "key": "API_BASE_URL",
        "label": "Ride API URL",
        "help": "Usually leave as the default unless we gave you a different URL.",
        "required": True,
        "default": "https://empower-production-3f06.up.railway.app",
        "placeholder": "https://…",
        "section": "Ride API",
    },
    {
        "key": "API_KEY_ID",
        "label": "API key ID",
        "help": "Starts with pk_… — from our Partner dashboard (we create this for your server).",
        "required": True,
        "placeholder": "pk_xxxxxxxxxxxx",
        "section": "Ride API",
    },
    {
        "key": "API_SECRET",
        "label": "API secret",
        "help": "Shown once when the partner key is created. Keep it private.",
        "required": True,
        "secret": True,
        "placeholder": "Paste the API secret",
        "section": "Ride API",
    },
    {
        "key": "DASHBOARD_PASSWORD",
        "label": "Admin dashboard password",
        "help": "At least 10 characters. Used at http://127.0.0.1:8080 when the bot is running.",
        "required": False,
        "secret": True,
        "placeholder": "Choose a strong password",
        "section": "Dashboard",
    },
    {
        "key": "DASHBOARD_HOST",
        "label": "Dashboard host",
        "required": False,
        "default": "127.0.0.1",
        "section": "Dashboard",
        "advanced": True,
    },
    {
        "key": "DASHBOARD_PORT",
        "label": "Dashboard port",
        "required": False,
        "default": "8080",
        "section": "Dashboard",
        "advanced": True,
    },
    {
        "key": "OPERATOR_MEMBER_ID",
        "label": "Operator member id",
        "help": "Card pool owner on the API. Leave blank to use “operator”.",
        "required": False,
        "default": "",
        "section": "Ride API",
        "advanced": True,
    },
    {
        "key": "PUBLIC_BASE_URL",
        "label": "Public HTTPS URL of this bot (optional)",
        "help": "Needed for Stripe / OxaPay webhooks. Leave blank if you only use manual Add Funds.",
        "required": False,
        "placeholder": "https://your-bot.example.com",
        "section": "Payments",
    },
    {
        "key": "STRIPE_SECRET_KEY",
        "label": "Stripe secret key (optional)",
        "help": "sk_live_… or sk_test_… — leave empty to hide card top-ups.",
        "required": False,
        "secret": True,
        "section": "Payments",
    },
    {
        "key": "STRIPE_WEBHOOK_SECRET",
        "label": "Stripe webhook secret (optional)",
        "required": False,
        "secret": True,
        "section": "Payments",
        "advanced": True,
    },
    {
        "key": "OXAPAY_MERCHANT_API_KEY",
        "label": "OxaPay merchant key (optional)",
        "help": "Leave empty to hide crypto top-ups.",
        "required": False,
        "secret": True,
        "section": "Payments",
    },
    {
        "key": "OXAPAY_SANDBOX",
        "label": "OxaPay sandbox",
        "help": "Must be false in production.",
        "required": False,
        "default": "false",
        "section": "Payments",
        "advanced": True,
    },
    {
        "key": "API_SIGN_ORDER",
        "label": "API signature field order",
        "required": False,
        "default": "timestamp,nonce,method,path,body_sha256",
        "section": "Ride API",
        "advanced": True,
    },
]

REQUIRED_KEYS = [f["key"] for f in FIELDS if f.get("required")]


def is_railway() -> bool:
    """True when running on Railway (or another host that sets RAILWAY_*)."""
    return bool(
        (os.getenv("RAILWAY_ENVIRONMENT") or "").strip()
        or (os.getenv("RAILWAY_PROJECT_ID") or "").strip()
        or (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
        or (os.getenv("RAILWAY_STATIC_URL") or "").strip()
    )


def is_hosted() -> bool:
    """Railway / any host that injects PORT and expects a public bind."""
    if is_railway():
        return True
    # Explicit opt-in for other PaaS
    return (os.getenv("HOSTED") or "").strip().lower() in ("1", "true", "yes", "on")


def load(override: bool = False) -> None:
    load_dotenv(ENV_PATH, override=override)
    load_dotenv(override=override)  # also allow process env / cwd
    _apply_hosted_defaults()


def _apply_hosted_defaults() -> None:
    """Fill Railway-friendly defaults into os.environ when unset."""
    if not is_hosted():
        return
    # Public HTTP bind — 127.0.0.1 would make Railway health checks fail.
    if not (os.getenv("DASHBOARD_HOST") or "").strip():
        os.environ["DASHBOARD_HOST"] = "0.0.0.0"
    # Railway always sets PORT.
    if (os.getenv("PORT") or "").strip() and not (os.getenv("DASHBOARD_PORT") or "").strip():
        os.environ["DASHBOARD_PORT"] = os.environ["PORT"].strip()
    # Persist SQLite on a mounted volume at /data when present.
    if not (os.getenv("DB_PATH") or "").strip():
        data_dir = Path("/data")
        if data_dir.is_dir():
            os.environ["DB_PATH"] = str(data_dir / "bot.db")
    # Public URL for Stripe/OxaPay return + webhooks.
    if not (os.getenv("PUBLIC_BASE_URL") or "").strip():
        domain = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
        if domain:
            if not domain.startswith("http"):
                domain = "https://" + domain
            os.environ["PUBLIC_BASE_URL"] = domain


def get(key: str, default: str | None = None) -> str:
    for f in FIELDS:
        if f["key"] == key and default is None and "default" in f:
            default = str(f.get("default") or "")
            break
    return (os.getenv(key) if os.getenv(key) is not None else default) or ""


def missing_required() -> list[str]:
    load()
    return [k for k in REQUIRED_KEYS if not get(k).strip()]


def is_configured() -> bool:
    return not missing_required()


def read_env_file() -> dict[str, str]:
    """Parse .env into a dict (does not expand variables)."""
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def current_values(*, mask_secrets: bool = False) -> dict[str, str]:
    load()
    file_vals = read_env_file()
    out: dict[str, str] = {}
    for f in FIELDS:
        key = f["key"]
        val = file_vals.get(key)
        if val is None:
            val = get(key, str(f.get("default") or ""))
        if mask_secrets and f.get("secret") and val:
            out[key] = "••••••••" if len(val) < 8 else (val[:4] + "••••" + val[-4:])
        else:
            out[key] = val
    return out


def _quote(value: str) -> str:
    if re.search(r'[\s#"\'\\]', value) or value == "":
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def save_env(values: dict[str, str]) -> Path:
    """Merge values into .env (preserve unknown keys and comment blocks best-effort)."""
    existing = read_env_file()
    merged = dict(existing)
    for f in FIELDS:
        key = f["key"]
        if key not in values:
            continue
        raw = values[key]
        if raw is None:
            continue
        text = str(raw).strip()
        # Keep previous secret when the form sent the mask placeholder
        if f.get("secret") and (text.startswith("••••") or text == ""):
            if key in existing and text.startswith("••••"):
                continue
            if text == "" and not f.get("required"):
                merged[key] = ""
                continue
        merged[key] = text

    for key in REQUIRED_KEYS:
        if not str(merged.get(key) or "").strip():
            raise ValueError(f"Missing required setting: {key}")

    lines = [
        "# Partner ride bot settings — created by the setup wizard.",
        "# You can re-open the wizard anytime:  python run.py --setup",
        "",
    ]
    sections: dict[str, list[dict[str, Any]]] = {}
    for f in FIELDS:
        sections.setdefault(f["section"], []).append(f)
    for section, items in sections.items():
        lines.append(f"# --- {section} ---")
        for f in items:
            key = f["key"]
            val = str(merged.get(key, f.get("default") or ""))
            lines.append(f"{key}={_quote(val)}")
        lines.append("")
    # Keep any extra keys the user had
    known = {f["key"] for f in FIELDS}
    extras = [k for k in merged if k not in known]
    if extras:
        lines.append("# --- Other ---")
        for key in extras:
            lines.append(f"{key}={_quote(str(merged[key]))}")
        lines.append("")

    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    load(override=True)
    return ENV_PATH


def validate_discord_token(token: str) -> str | None:
    token = (token or "").strip()
    if not token or token.startswith("••••"):
        return "Paste your Discord bot token."
    if "." not in token or len(token) < 50:
        return "That does not look like a Discord bot token."
    return None


def validate_api_key_id(key_id: str) -> str | None:
    key_id = (key_id or "").strip()
    if not key_id or key_id.startswith("••••"):
        return "Paste your API key ID (pk_…)."
    if not key_id.startswith("pk_"):
        return "API key ID usually starts with pk_."
    return None


def require_runtime() -> dict[str, str]:
    """Values bot.py needs. Exits with a clear message when setup is incomplete."""
    load()
    missing = missing_required()
    if missing:
        if is_hosted():
            raise SystemExit(
                "Bot is not configured on this host.\n"
                f"  Missing env vars: {', '.join(missing)}\n\n"
                "On Railway: Project → Variables → add DISCORD_TOKEN, API_KEY_ID, API_SECRET\n"
                "(and the rest from DEPLOY_RAILWAY.md), then Redeploy."
            )
        raise SystemExit(
            "Bot is not configured yet.\n"
            f"  Missing: {', '.join(missing)}\n\n"
            "Easy fix: double-click  start.bat\n"
            "  or run:  python run.py\n"
            "That opens a setup page in your browser."
        )
    sign = get("API_SIGN_ORDER", "timestamp,nonce,method,path,body_sha256")
    host_default = "0.0.0.0" if is_hosted() else "127.0.0.1"
    port_default = (os.getenv("PORT") or "").strip() or "8080"
    db_default = str(ROOT / "bot.db")
    if is_hosted() and Path("/data").is_dir():
        db_default = str(Path("/data") / "bot.db")
    return {
        "DISCORD_TOKEN": get("DISCORD_TOKEN").strip(),
        "API_BASE_URL": get("API_BASE_URL", "https://empower-production-3f06.up.railway.app").strip().rstrip("/"),
        "API_KEY_ID": get("API_KEY_ID").strip(),
        "API_SECRET": get("API_SECRET").strip(),
        "API_SIGN_ORDER": sign,
        "OPERATOR_MEMBER_ID": (get("OPERATOR_MEMBER_ID") or "").strip() or "operator",
        "GUILD_ID": get("GUILD_ID").strip(),
        "DB_PATH": get("DB_PATH", db_default) or db_default,
        "DASHBOARD_PASSWORD": get("DASHBOARD_PASSWORD").strip(),
        "DASHBOARD_HOST": get("DASHBOARD_HOST", host_default) or host_default,
        "DASHBOARD_PORT": get("DASHBOARD_PORT", port_default) or port_default,
        "PUBLIC_BASE_URL": get("PUBLIC_BASE_URL").strip().rstrip("/"),
        "HOSTED": "1" if is_hosted() else "0",
    }
