#!/usr/bin/env python3
"""Stripe Checkout client for Discord credit top-ups + webhook signature check.

Flow mirrors OxaPay:
  1) POST /v1/checkout/sessions (mode=payment, hosted page) → session.url
  2) Discord shows the url; user pays with card / wallet on checkout.stripe.com
  3) Stripe → POST /stripe/webhook (checkout.session.completed | async_payment_succeeded)
  4) Verify Stripe-Signature, then live GET /v1/checkout/sessions/{id}
     and credit the *order* USD amount once (payment_status == "paid").

Secret key (sk_/rk_) stays server-side only — never in Discord or the client.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

API_BASE = "https://api.stripe.com/v1"
WEBHOOK_TOLERANCE_S = 300

# Stripe sends these for a Checkout payment that may settle later (bank debits etc.).
PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
FAILED_EVENTS = ("checkout.session.async_payment_failed", "checkout.session.expired")


def _env(*names: str) -> str:
    for n in names:
        v = (os.getenv(n) or "").strip().strip('"').strip("'")
        if v:
            return v
    return ""


def secret_key() -> str:
    return _env("STRIPE_SECRET_KEY", "STRIPE_API_KEY")


def webhook_secret() -> str:
    return _env("STRIPE_WEBHOOK_SECRET")


def api_version() -> str:
    return _env("STRIPE_API_VERSION")


def public_base_url() -> str:
    """Where Checkout returns the browser after pay/cancel (no credit logic there)."""
    base = _env("STRIPE_RETURN_BASE_URL", "PUBLIC_BASE_URL").rstrip("/")
    if base:
        return base
    cb = _env("OXAPAY_CALLBACK_URL", "OXAPAY_WEBHOOK_URL")
    if cb and "YOUR_PUBLIC_HOST" not in cb:
        u = urllib.parse.urlparse(cb)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    return ""


def enabled() -> bool:
    return bool(secret_key())


def test_mode() -> bool:
    return secret_key().startswith(("sk_test_", "rk_test_"))


class StripeError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _flatten(prefix: str, value: Any, out: list[tuple[str, str]]) -> None:
    """Encode nested dict/list as Stripe form fields: a[b][0][c]=v."""
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]" if prefix else str(k), v, out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    elif value is None:
        return
    elif isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
    else:
        out.append((prefix, str(value)))


def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    idempotency_key: str = "",
    timeout: float = 45.0,
) -> dict[str, Any]:
    key = secret_key()
    if not key:
        raise StripeError("STRIPE_SECRET_KEY is not set")
    pairs: list[tuple[str, str]] = []
    if params:
        _flatten("", params, pairs)
    encoded = urllib.parse.urlencode(pairs)
    url = f"{API_BASE}{path}"
    data = None
    if method.upper() == "GET":
        if encoded:
            url += "?" + encoded
    else:
        data = encoded.encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "EmpowerDiscordBot/1.0",
    }
    if api_version():
        headers["Stripe-Version"] = api_version()
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw}
        err = (payload or {}).get("error") or {}
        msg = (err.get("message") if isinstance(err, dict) else None) or raw or str(exc)
        code = err.get("code") if isinstance(err, dict) else ""
        raise StripeError(
            f"{msg}" + (f" [{code}]" if code else ""),
            status=int(exc.code),
            payload=payload,
        ) from exc
    except urllib.error.URLError as exc:
        raise StripeError(f"Stripe network error: {exc}") from exc
    try:
        payload = json.loads(raw) if raw else {}
    except Exception as exc:
        raise StripeError(f"Invalid JSON from Stripe: {raw[:300]}") from exc
    if not isinstance(payload, dict):
        raise StripeError("Unexpected Stripe response shape", payload=payload)
    return payload


def to_cents(amount_usd: Any) -> int:
    from decimal import ROUND_HALF_UP, Decimal

    return int((Decimal(str(amount_usd)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def create_checkout_session(
    amount_usd: Any,
    *,
    order_id: str,
    discord_user_id: Any,
    discord_username: str = "",
    lifetime_minutes: int = 60,
    product_name: str = "Empower",
    fallback_return_url: str = "",
) -> dict[str, Any]:
    """Hosted Checkout Session for a one-off credit top-up. Returns the session object."""
    cents = to_cents(amount_usd)
    if cents < 50:
        raise StripeError("Stripe minimum charge is $0.50")
    # Stripe accepts expires_at between 30 minutes and 24 hours from now.
    minutes = max(31, min(int(lifetime_minutes or 60), 24 * 60 - 1))
    base = public_base_url()
    if base:
        success_url = f"{base}/stripe/return?status=success&order={order_id}"
        cancel_url = f"{base}/stripe/return?status=cancel&order={order_id}"
    elif fallback_return_url:
        success_url = cancel_url = fallback_return_url
    else:
        raise StripeError(
            "Set STRIPE_RETURN_BASE_URL (or OXAPAY_CALLBACK_URL) to your public https host"
        )
    meta = {
        "order_id": order_id,
        "discord_user_id": str(discord_user_id),
        "discord_username": discord_username[:100],
        "purpose": "empower_credits",
    }
    params: dict[str, Any] = {
        "mode": "payment",
        "client_reference_id": order_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": cents,
                    "product_data": {
                        "name": f"{product_name} credits · ${cents / 100:.2f}",
                        "description": "1 credit = $1.00 · added to your Discord wallet",
                    },
                },
            }
        ],
        "metadata": meta,
        "payment_intent_data": {
            "metadata": meta,
            "description": f"{product_name} credits for Discord {discord_user_id}",
        },
        "expires_at": int(time.time()) + minutes * 60,
        "submit_type": "pay",
    }
    session = _request(
        "POST",
        "/checkout/sessions",
        params=params,
        idempotency_key=f"checkout_{order_id}",
    )
    if not session.get("id") or not session.get("url"):
        raise StripeError("Checkout Session missing id/url", payload=session)
    return session


def retrieve_session(session_id: str) -> dict[str, Any]:
    sid = str(session_id or "").strip()
    if not sid.startswith("cs_"):
        raise StripeError(f"Not a Checkout Session id: {sid!r}")
    return _request("GET", f"/checkout/sessions/{urllib.parse.quote(sid)}")


def expire_session(session_id: str) -> dict[str, Any]:
    return _request("POST", f"/checkout/sessions/{urllib.parse.quote(session_id)}/expire")


def confirm_session_paid(session_id: str) -> dict[str, Any]:
    """Live GET — normalized like oxapay_client.confirm_invoice_paid."""
    s = retrieve_session(session_id)
    status = str(s.get("status") or "")
    pay_status = str(s.get("payment_status") or "")
    paid = status == "complete" and pay_status in ("paid", "no_payment_required")
    return {
        "track_id": str(s.get("id") or session_id),
        "status": f"{status}/{pay_status}",
        "paid": paid,
        "pending": status == "open" or (status == "complete" and pay_status == "unpaid"),
        "expired": status == "expired",
        "sandbox": not bool(s.get("livemode")),
        "amount_cents": int(s.get("amount_total") or 0),
        "currency": str(s.get("currency") or ""),
        "order_id": str(s.get("client_reference_id") or (s.get("metadata") or {}).get("order_id") or ""),
        "payment_intent": str(s.get("payment_intent") or ""),
        "raw": s,
    }


def verify_webhook(
    raw_body: bytes | str,
    sig_header: str,
    *,
    secret: str = "",
    tolerance: int = WEBHOOK_TOLERANCE_S,
) -> Optional[dict[str, Any]]:
    """Return the parsed event when Stripe-Signature is valid, else None.

    Header: t=<unix>,v1=<hex HMAC-SHA256(secret, f"{t}.{raw_body}")>[,v1=…][,v0=…]
    Body must be the exact bytes Stripe sent (no JSON re-serialization).
    """
    secret = secret or webhook_secret()
    if not secret or not sig_header:
        return None
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    ts = ""
    sigs: list[str] = []
    for part in str(sig_header).split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1":
            sigs.append(v)
    if not ts.isdigit() or not sigs:
        return None
    if tolerance and abs(time.time() - int(ts)) > tolerance:
        return None
    signed = ts.encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        return None
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return None
    return event if isinstance(event, dict) else None


# Stripe amounts are in the currency's smallest unit; these are not 1/100.
# https://docs.stripe.com/currencies#zero-decimal
_ZERO_DECIMAL = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg",
    "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}
_THREE_DECIMAL = {"bhd", "jod", "kwd", "omr", "tnd"}
_SYMBOLS = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cad": "CA$", "aud": "A$"}

_BALANCE_CACHE_TTL_S = 30
_balance_cache: dict[str, Any] = {"at": 0.0, "key": "", "value": None}


def fmt_amount(minor: int, currency: str) -> str:
    cur = (currency or "").lower()
    digits = 0 if cur in _ZERO_DECIMAL else 3 if cur in _THREE_DECIMAL else 2
    value = int(minor) / (10 ** digits)
    sign = "-" if value < 0 else ""
    sym = _SYMBOLS.get(cur)
    num = f"{abs(value):,.{digits}f}"
    return f"{sign}{sym}{num}" if sym else f"{sign}{num} {cur.upper()}"


def _key_problem(key: str) -> str:
    """Catch keys that can never read /v1/balance before calling Stripe."""
    if not key:
        return "Set STRIPE_SECRET_KEY (sk_live_… / sk_test_… or rk_…)"
    if key.startswith("pk_"):
        return "STRIPE_SECRET_KEY is a publishable key (pk_…) — use the secret key sk_…"
    if key.startswith("whsec_"):
        return "STRIPE_SECRET_KEY holds the webhook secret (whsec_…) — use the secret key sk_…"
    if not key.startswith(("sk_", "rk_")):
        return "STRIPE_SECRET_KEY must start with sk_ or rk_"
    return ""


def _explain_error(exc: Exception) -> str:
    status = getattr(exc, "status", 0)
    if status == 401:
        return "Stripe rejected the key (401) — it is wrong, rolled, or deleted"
    if status == 403 or "permission" in str(exc).lower():
        return "Restricted key lacks Balance: Read (403) — enable it in Dashboard → API keys"
    if status == 429:
        return "Stripe rate limit (429) — try again shortly"
    return str(exc)[:160]


def _by_currency(rows: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("currency"):
            cur = str(r["currency"]).lower()
            out[cur] = out.get(cur, 0) + int(r.get("amount") or 0)
    return out


def _source_types(rows: Any) -> dict[str, dict[str, int]]:
    """{currency: {card: n, bank_account: n, …}} from available/pending rows."""
    out: dict[str, dict[str, int]] = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("source_types"), dict):
            continue
        cur = str(r.get("currency") or "").lower()
        bucket = out.setdefault(cur, {})
        for src, amt in r["source_types"].items():
            bucket[src] = bucket.get(src, 0) + int(amt or 0)
    return out


def get_balance() -> dict[str, Any]:
    """Raw GET /v1/balance with the server secret key. Raises StripeError."""
    problem = _key_problem(secret_key())
    if problem:
        raise StripeError(problem)
    return _request("GET", "/balance", timeout=20.0)


def wallet_summary(*, fresh: bool = False) -> dict[str, Any]:
    """Admin view of OUR Stripe balance (GET /v1/balance) — never Discord user credits.

    available = can be paid out now; pending = captured but not yet settled.
    Every currency on the account is reported; the headline prefers USD.
    """
    key = secret_key()
    problem = _key_problem(key)
    if problem:
        return {"ok": False, "headline": "—", "detail": problem, "test": test_mode()}

    now = time.time()
    cached = _balance_cache.get("value")
    if (
        not fresh
        and cached is not None
        and _balance_cache.get("key") == key
        and now - float(_balance_cache.get("at") or 0) < _BALANCE_CACHE_TTL_S
    ):
        return cached

    try:
        bal = get_balance()
    except Exception as exc:
        return {"ok": False, "headline": "—", "detail": _explain_error(exc), "test": test_mode()}

    available = _by_currency(bal.get("available"))
    pending = _by_currency(bal.get("pending"))
    instant = _by_currency(bal.get("instant_available"))
    reserved = _by_currency(bal.get("connect_reserved"))
    test = not bool(bal.get("livemode", not test_mode()))

    currencies = sorted(
        set(available) | set(pending),
        key=lambda c: (c != "usd", -(available.get(c, 0) + pending.get(c, 0)), c),
    )
    primary = currencies[0] if currencies else "usd"

    rows = []
    for cur in currencies:
        row = {
            "currency": cur,
            "available": available.get(cur, 0),
            "pending": pending.get(cur, 0),
            "available_fmt": fmt_amount(available.get(cur, 0), cur),
            "pending_fmt": fmt_amount(pending.get(cur, 0), cur),
        }
        if cur in instant:
            row["instant_available_fmt"] = fmt_amount(instant[cur], cur)
        if cur in reserved:
            row["connect_reserved_fmt"] = fmt_amount(reserved[cur], cur)
        rows.append(row)

    detail_bits = [f"pending {fmt_amount(pending.get(primary, 0), primary)}"]
    for cur in currencies[1:]:
        detail_bits.append(
            f"{cur.upper()} {fmt_amount(available.get(cur, 0), cur)}"
            f" (+{fmt_amount(pending.get(cur, 0), cur)} pending)"
        )
    if available.get(primary, 0) < 0:
        detail_bits.append("negative balance — refunds/disputes exceed funds")
    if test:
        detail_bits.append("TEST MODE")

    summary = {
        "ok": True,
        "headline": fmt_amount(available.get(primary, 0), primary),
        "detail": " · ".join(detail_bits),
        "currency": primary,
        "available_cents": available.get(primary, 0),
        "pending_cents": pending.get(primary, 0),
        "currencies": rows,
        "source_types": {
            "available": _source_types(bal.get("available")),
            "pending": _source_types(bal.get("pending")),
        },
        "livemode": not test,
        "test": test,
        "checked_at": int(now),
    }
    _balance_cache.update({"at": now, "key": key, "value": summary})
    return summary


_overview_cache: dict[str, Any] = {"at": 0.0, "key": "", "value": None}


def _pending_schedule(txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group pending balance transactions by the day they become available."""
    buckets: dict[tuple[int, str], int] = {}
    for t in txs:
        if t.get("status") != "pending":
            continue
        cur = str(t.get("currency") or "").lower()
        day = int(t.get("available_on") or 0)
        day -= day % 86400
        buckets[(day, cur)] = buckets.get((day, cur), 0) + int(t.get("net") or 0)
    return [
        {"available_on": day, "currency": cur, "net": net, "net_fmt": fmt_amount(net, cur)}
        for (day, cur), net in sorted(buckets.items())
    ]


def wallet_overview(*, fresh: bool = False, tx_limit: int = 100) -> dict[str, Any]:
    """Balance + settlement detail for the admin site.

    GET /v1/balance                → available / pending per currency
    GET /v1/balance_transactions   → what is pending and when it becomes available
    GET /v1/payouts                → recent payouts to the bank
    GET /v1/account                → payout schedule (restricted keys may lack this)
    Sub-calls that fail are reported in `errors`; the balance alone is enough to render.
    """
    summary = wallet_summary(fresh=fresh)
    if not summary.get("ok"):
        return {**summary, "schedule": [], "transactions": [], "payouts": [], "account": {}, "errors": {}}

    key = secret_key()
    now = time.time()
    cached = _overview_cache.get("value")
    if (
        not fresh
        and cached is not None
        and _overview_cache.get("key") == key
        and now - float(_overview_cache.get("at") or 0) < _BALANCE_CACHE_TTL_S
    ):
        return {**cached, **summary}

    errors: dict[str, str] = {}
    txs: list[dict[str, Any]] = []
    try:
        res = _request("GET", "/balance_transactions", params={"limit": max(1, min(int(tx_limit), 100))}, timeout=20.0)
        txs = [t for t in res.get("data") or [] if isinstance(t, dict)]
    except Exception as exc:
        errors["balance_transactions"] = _explain_error(exc)

    payouts: list[dict[str, Any]] = []
    try:
        res = _request("GET", "/payouts", params={"limit": 10}, timeout=20.0)
        for p in res.get("data") or []:
            cur = str(p.get("currency") or "")
            payouts.append(
                {
                    "id": str(p.get("id") or ""),
                    "amount_fmt": fmt_amount(int(p.get("amount") or 0), cur),
                    "status": str(p.get("status") or ""),
                    "created": int(p.get("created") or 0),
                    "arrival_date": int(p.get("arrival_date") or 0),
                    "automatic": bool(p.get("automatic")),
                }
            )
    except Exception as exc:
        errors["payouts"] = _explain_error(exc)

    account: dict[str, Any] = {}
    try:
        acct = _request("GET", "/account", timeout=20.0)
        sched = ((acct.get("settings") or {}).get("payouts") or {}).get("schedule") or {}
        account = {
            "country": acct.get("country") or "",
            "default_currency": acct.get("default_currency") or "",
            "payouts_enabled": acct.get("payouts_enabled"),
            "charges_enabled": acct.get("charges_enabled"),
            "interval": sched.get("interval") or "",
            "delay_days": sched.get("delay_days"),
        }
    except Exception as exc:
        errors["account"] = _explain_error(exc)

    transactions = [
        {
            "id": str(t.get("id") or ""),
            "type": str(t.get("type") or ""),
            "status": str(t.get("status") or ""),
            "created": int(t.get("created") or 0),
            "available_on": int(t.get("available_on") or 0),
            "amount_fmt": fmt_amount(int(t.get("amount") or 0), str(t.get("currency") or "")),
            "fee_fmt": fmt_amount(int(t.get("fee") or 0), str(t.get("currency") or "")),
            "net_fmt": fmt_amount(int(t.get("net") or 0), str(t.get("currency") or "")),
            "description": str(t.get("description") or ""),
        }
        for t in txs
    ]
    detail = {
        "schedule": _pending_schedule(txs),
        "transactions": transactions,
        "payouts": payouts,
        "account": account,
        "errors": errors,
    }
    _overview_cache.update({"at": now, "key": key, "value": detail})
    return {**detail, **summary}


def status_summary() -> dict[str, Any]:
    key = secret_key()
    return {
        "enabled": bool(key),
        "mode": "test" if test_mode() else ("live" if key else "off"),
        "webhook_secret_set": bool(webhook_secret()),
        "api_version": api_version() or "(account default)",
        "return_base_url": public_base_url() or "(unset)",
    }


if __name__ == "__main__":
    # python stripe_client.py  → verify STRIPE_SECRET_KEY can read the balance
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
    except ImportError:
        pass
    s = wallet_summary(fresh=True)
    if not s.get("ok"):
        print(f"Stripe balance FAILED: {s.get('detail')}")
        raise SystemExit(1)
    print(f"Stripe balance OK · {'TEST' if s['test'] else 'LIVE'} mode")
    for r in s["currencies"]:
        line = f"  {r['currency'].upper()}: available {r['available_fmt']} · pending {r['pending_fmt']}"
        if r.get("instant_available_fmt"):
            line += f" · instant {r['instant_available_fmt']}"
        if r.get("connect_reserved_fmt"):
            line += f" · reserved {r['connect_reserved_fmt']}"
        print(line)
    if not s["currencies"]:
        print("  (no balance rows — account has never processed a payment)")
