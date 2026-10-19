#!/usr/bin/env python3
"""OxaPay v1 API client — merchant / payout / general / common + webhook HMAC."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

BASE_URL = (os.getenv("OXAPAY_BASE_URL") or "https://api.oxapay.com/v1").rstrip("/")


def _env(*names: str) -> str:
    for n in names:
        v = (os.getenv(n) or "").strip().strip('"').strip("'")
        if v:
            return v
    return ""


def merchant_key() -> str:
    return _env("OXAPAY_MERCHANT_API_KEY", "MERCHANT_API_KEY")


def payout_key() -> str:
    return _env("OXAPAY_PAYOUT_API_KEY", "PAYOUT_API_KEY")


def general_key() -> str:
    return _env("OXAPAY_GENERAL_API_KEY", "GENERAL_API_KEY")


def callback_url() -> str:
    return _env("OXAPAY_CALLBACK_URL", "OXAPAY_WEBHOOK_URL")


def sandbox_mode() -> bool:
    v = (_env("OXAPAY_SANDBOX") or "false").lower()
    return v in ("1", "true", "yes", "on")


class OxaPayError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _request(
    method: str,
    path: str,
    *,
    api_key: str,
    key_header: str,
    body: Optional[dict[str, Any]] = None,
    query: Optional[dict[str, Any]] = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    if not api_key:
        raise OxaPayError(f"Missing OxaPay key for {key_header}")
    url = f"{BASE_URL}{path}"
    if query:
        q = {k: v for k, v in query.items() if v is not None and v != ""}
        if q:
            url += "?" + urllib.parse.urlencode(q)
    data = None
    headers = {
        key_header: api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "EmpowerDiscordBot/1.0",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            http_status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        http_status = int(exc.code)
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw}
        err = (payload or {}).get("error") or {}
        msg = (
            (err.get("message") if isinstance(err, dict) else None)
            or (payload or {}).get("message")
            or raw
            or str(exc)
        )
        raise OxaPayError(str(msg), status=http_status, payload=payload) from exc
    except urllib.error.URLError as exc:
        raise OxaPayError(f"OxaPay network error: {exc}") from exc

    try:
        payload = json.loads(raw) if raw else {}
    except Exception as exc:
        raise OxaPayError(f"Invalid JSON from OxaPay: {raw[:300]}", status=http_status) from exc

    if not isinstance(payload, dict):
        raise OxaPayError("Unexpected OxaPay response shape", status=http_status, payload=payload)

    api_status = payload.get("status")
    if api_status not in (None, 200, "200") and http_status >= 400:
        err = payload.get("error") or {}
        msg = (err.get("message") if isinstance(err, dict) else None) or payload.get("message") or "OxaPay error"
        raise OxaPayError(str(msg), status=int(api_status or http_status), payload=payload)

    # Some errors return HTTP 200 with error object
    if payload.get("error") and api_status not in (None, 200, "200"):
        err = payload["error"]
        msg = (err.get("message") if isinstance(err, dict) else str(err)) or payload.get("message") or "OxaPay error"
        raise OxaPayError(str(msg), status=int(api_status or 400), payload=payload)

    return payload


def _merchant(method: str, path: str, **kwargs) -> dict[str, Any]:
    return _request(method, path, api_key=merchant_key(), key_header="merchant_api_key", **kwargs)


def _payout(method: str, path: str, **kwargs) -> dict[str, Any]:
    return _request(method, path, api_key=payout_key(), key_header="payout_api_key", **kwargs)


def _general(method: str, path: str, **kwargs) -> dict[str, Any]:
    return _request(method, path, api_key=general_key(), key_header="general_api_key", **kwargs)


def _common(method: str, path: str, **kwargs) -> dict[str, Any]:
    # Commons often accept merchant key; fall back to general then empty merchant
    key = merchant_key() or general_key()
    header = "merchant_api_key" if merchant_key() else "general_api_key"
    if not key:
        # prices/currencies/monitor may work without key — still send Content-Type
        return _request(method, path, api_key="none", key_header="merchant_api_key", **kwargs)
    return _request(method, path, api_key=key, key_header=header, **kwargs)


# ── Merchant: invoices / white-label / static / payments ─────────────────────

def generate_invoice(
    amount: float | str,
    *,
    currency: str = "USD",
    lifetime: int = 60,
    order_id: str = "",
    description: str = "",
    email: str = "",
    callback: str = "",
    return_url: str = "",
    thanks_message: str = "",
    fee_paid_by_payer: Optional[float] = None,
    under_paid_coverage: Optional[float] = None,
    to_currency: str = "",
    auto_withdrawal: Optional[bool] = None,
    mixed_payment: Optional[bool] = None,
    sandbox: Optional[bool] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "amount": float(amount),
        "lifetime": int(lifetime),
    }
    if currency:
        body["currency"] = currency
    cb = callback or callback_url()
    if cb:
        body["callback_url"] = cb
    if return_url:
        body["return_url"] = return_url
    if email:
        body["email"] = email
    if order_id:
        body["order_id"] = order_id
    if description:
        body["description"] = description
    if thanks_message:
        body["thanks_message"] = thanks_message
    if fee_paid_by_payer is not None:
        body["fee_paid_by_payer"] = fee_paid_by_payer
    if under_paid_coverage is not None:
        body["under_paid_coverage"] = under_paid_coverage
    if to_currency:
        body["to_currency"] = to_currency
    if auto_withdrawal is not None:
        body["auto_withdrawal"] = auto_withdrawal
    if mixed_payment is not None:
        body["mixed_payment"] = mixed_payment
    sand = sandbox_mode() if sandbox is None else bool(sandbox)
    if sand:
        body["sandbox"] = True
    payload = _merchant("POST", "/payment/invoice", body=body)
    data = payload.get("data") or {}
    if not data.get("payment_url") or not data.get("track_id"):
        raise OxaPayError("Invoice response missing payment_url/track_id", payload=payload)
    return data


def generate_white_label(
    amount: float | str,
    pay_currency: str,
    **kwargs: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "amount": float(amount),
        "pay_currency": pay_currency,
    }
    for k in (
        "currency",
        "network",
        "lifetime",
        "fee_paid_by_payer",
        "under_paid_coverage",
        "to_currency",
        "auto_withdrawal",
        "callback_url",
        "email",
        "order_id",
        "description",
    ):
        if kwargs.get(k) is not None and kwargs.get(k) != "":
            body[k] = kwargs[k]
    if "callback_url" not in body and callback_url():
        body["callback_url"] = callback_url()
    payload = _merchant("POST", "/payment/white-label", body=body)
    return payload.get("data") or {}


def generate_static_address(network: str, **kwargs: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"network": network}
    for k in ("to_currency", "auto_withdrawal", "callback_url", "email", "order_id", "description"):
        if kwargs.get(k) is not None and kwargs.get(k) != "":
            body[k] = kwargs[k]
    if "callback_url" not in body and callback_url():
        body["callback_url"] = callback_url()
    payload = _merchant("POST", "/payment/static-address", body=body)
    return payload.get("data") or {}


def revoke_static_address(address: str) -> dict[str, Any]:
    payload = _merchant("POST", "/payment/static-address/revoke", body={"address": address})
    return payload.get("data") or {}


def list_static_addresses(**query: Any) -> dict[str, Any]:
    payload = _merchant("GET", "/payment/static-address", query=query)
    return payload.get("data") or {}


def payment_information(track_id: str) -> dict[str, Any]:
    payload = _merchant("GET", f"/payment/{urllib.parse.quote(str(track_id))}")
    return payload.get("data") or {}


def payment_history(**query: Any) -> dict[str, Any]:
    payload = _merchant("GET", "/payment", query=query)
    return payload.get("data") or {}


def payment_stats(**query: Any) -> dict[str, Any]:
    payload = _merchant("GET", "/payment/stats", query=query)
    return payload.get("data") or {}


# ── Payouts ──────────────────────────────────────────────────────────────────

def generate_payout(address: str, currency: str, amount: float | str, **kwargs: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "address": address,
        "currency": currency,
        "amount": float(amount),
    }
    for k in ("network", "callback_url", "memo", "description"):
        if kwargs.get(k) is not None and kwargs.get(k) != "":
            body[k] = kwargs[k]
    payload = _payout("POST", "/payout", body=body)
    return payload.get("data") or {}


def payout_information(track_id: str) -> dict[str, Any]:
    payload = _payout("GET", f"/payout/{urllib.parse.quote(str(track_id))}")
    return payload.get("data") or {}


def payout_history(**query: Any) -> dict[str, Any]:
    payload = _payout("GET", "/payout", query=query)
    return payload.get("data") or {}


# ── General: swap + balance ──────────────────────────────────────────────────

def account_balance(currency: str = "") -> dict[str, Any]:
    q = {"currency": currency} if currency else None
    payload = _general("GET", "/general/account/balance", query=q)
    return payload.get("data") or {}


def wallet_summary() -> dict[str, Any]:
    """Admin view of OUR OxaPay account — never Discord user credit balances.

    Returns {ok, headline, detail, nonzero:[{currency, amount}]}.
    Headline prefers USDT, then other stables, then the largest other coin.
    """
    if not general_key():
        return {
            "ok": False,
            "headline": "—",
            "detail": "Set OXAPAY_GENERAL_API_KEY",
            "nonzero": [],
        }
    try:
        raw = account_balance()
    except Exception as exc:
        return {"ok": False, "headline": "—", "detail": str(exc)[:120], "nonzero": []}
    if not isinstance(raw, dict):
        return {"ok": False, "headline": "—", "detail": "Unexpected balance payload", "nonzero": []}

    def _f(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def _fmt(amount: float, code: str) -> str:
        if abs(amount) >= 1 or amount == 0:
            return f"{amount:,.2f} {code}"
        return f"{amount:.6f}".rstrip("0").rstrip(".") + f" {code}"

    nonzero = [
        {"currency": str(k), "amount": _f(v)}
        for k, v in raw.items()
        if _f(v) != 0
    ]
    nonzero.sort(key=lambda r: abs(r["amount"]), reverse=True)
    stables = ("USDT", "USDC", "DAI", "USD")
    primary = next((r for r in nonzero if r["currency"] in stables), None)
    if primary is None and nonzero:
        primary = nonzero[0]
    if primary is None:
        headline = "0.00 USDT"
        detail = "OxaPay account · all coins 0"
    else:
        headline = _fmt(primary["amount"], primary["currency"])
        rest = [
            _fmt(r["amount"], r["currency"])
            for r in nonzero
            if r["currency"] != primary["currency"]
        ][:4]
        detail = " · ".join(rest) if rest else "OxaPay merchant account"
    return {"ok": True, "headline": headline, "detail": detail, "nonzero": nonzero}


def swap_pairs() -> dict[str, Any]:
    payload = _general("GET", "/general/swap/pairs")
    return payload.get("data") or {}


def swap_calculate(from_currency: str, to_currency: str, amount: float | str) -> dict[str, Any]:
    payload = _general(
        "POST",
        "/general/swap/calculate",
        body={
            "from_currency": from_currency,
            "to_currency": to_currency,
            "amount": float(amount),
        },
    )
    return payload.get("data") or {}


def swap_rate(from_currency: str, to_currency: str) -> dict[str, Any]:
    payload = _general(
        "POST",
        "/general/swap/rate",
        body={"from_currency": from_currency, "to_currency": to_currency},
    )
    return payload.get("data") or {}


def swap_request(from_currency: str, to_currency: str, amount: float | str) -> dict[str, Any]:
    payload = _general(
        "POST",
        "/general/swap",
        body={
            "from_currency": from_currency,
            "to_currency": to_currency,
            "amount": float(amount),
        },
    )
    return payload.get("data") or {}


def swap_history(**query: Any) -> dict[str, Any]:
    payload = _general("GET", "/general/swap", query=query)
    return payload.get("data") or {}


# ── Common ───────────────────────────────────────────────────────────────────

def prices() -> dict[str, Any]:
    payload = _common("GET", "/common/prices")
    return payload.get("data") or {}


def currencies() -> dict[str, Any]:
    payload = _common("GET", "/common/currencies")
    return payload.get("data") or {}


def fiats() -> dict[str, Any]:
    payload = _common("GET", "/common/fiats")
    return payload.get("data") or {}


def networks() -> dict[str, Any]:
    payload = _common("GET", "/common/networks")
    return payload.get("data") or {}


def monitor() -> dict[str, Any]:
    payload = _common("GET", "/common/monitor")
    return payload.get("data") or {}


# ── Webhook HMAC ─────────────────────────────────────────────────────────────

def verify_webhook_hmac(raw_body: bytes | str, hmac_header: str, *, kind: str = "payment") -> bool:
    """Validate OxaPay callback. kind: payment|invoice|merchant → merchant key; payout → payout key."""
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    key = payout_key() if str(kind).lower() == "payout" else merchant_key()
    if not key or not hmac_header:
        return False
    calc = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(calc.lower(), str(hmac_header).strip().lower())


def is_paid_status(status: str) -> bool:
    s = (status or "").strip().lower().replace(" ", "_")
    return s in ("paid", "manual_accept")


def is_pending_status(status: str) -> bool:
    s = (status or "").strip().lower().replace(" ", "_")
    return s in ("new", "waiting", "paying", "")


def confirm_invoice_paid(track_id: str) -> dict[str, Any]:
    """Live-check OxaPay GET /payment/{track_id}. Returns normalized result.

    Credits must only be applied when status is Paid / manual_accept.
    Never trust webhook crypto `amount` for USD credit sizing — use our order.
    """
    tid = str(track_id or "").strip()
    if not tid:
        raise OxaPayError("track_id required")
    data = payment_information(tid)
    status = str(data.get("status") or "")
    return {
        "track_id": str(data.get("track_id") or tid),
        "status": status,
        "paid": is_paid_status(status),
        "pending": is_pending_status(status),
        "amount": data.get("amount"),
        "currency": data.get("currency"),
        "order_id": str(data.get("order_id") or ""),
        "type": str(data.get("type") or ""),
        "expired_at": data.get("expired_at"),
        "raw": data,
    }
