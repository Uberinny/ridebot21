import asyncio
import json
import logging
import secrets
import sqlite3
import time
from html import escape
from typing import Awaitable, Callable

from aiohttp import web

import oxapay_client as oxapay
import stripe_client as stripe
from db import Database

log = logging.getLogger("payments")

LIFETIME_MINUTES = 60
PROVIDER_LABELS = {"stripe": "Card", "oxapay": "Crypto"}
PROVIDER_EMOJI = {"stripe": "💳", "oxapay": "🪙"}

Credited = Callable[[sqlite3.Row, int, int], Awaitable[None]]


def enabled_providers() -> list[str]:
    providers = []
    if stripe.enabled():
        providers.append("stripe")
    if oxapay.merchant_key():
        providers.append("oxapay")
    return providers


class Payments:
    """Top-ups through Stripe Checkout or OxaPay invoices.

    A payment is only credited after a live status check with the provider (webhooks just trigger
    that check), and always for the amount we recorded — never an amount reported by the payer."""

    def __init__(self, db: Database, brand_name: Callable[[], str], on_credited: Credited):
        self.db = db
        self.brand_name = brand_name
        self.on_credited = on_credited
        self.locks: dict[str, asyncio.Lock] = {}

    async def create(self, user_id: int, username: str, amount_cents: int, provider: str) -> sqlite3.Row:
        order_id = f"top_{secrets.token_hex(8)}"
        now = int(time.time())
        if provider == "stripe":
            session = await asyncio.to_thread(
                stripe.create_checkout_session,
                amount_cents / 100,
                order_id=order_id,
                discord_user_id=user_id,
                discord_username=username,
                lifetime_minutes=LIFETIME_MINUTES,
                product_name=self.brand_name(),
                fallback_return_url="https://discord.com/app",
            )
            track_id, url = session["id"], session["url"]
            expires_at = int(session.get("expires_at") or now + LIFETIME_MINUTES * 60)
        elif provider == "oxapay":
            invoice = await asyncio.to_thread(
                oxapay.generate_invoice,
                amount_cents / 100,
                currency="USD",
                lifetime=LIFETIME_MINUTES,
                order_id=order_id,
                description=f"{self.brand_name()} credits for {username}",
            )
            track_id, url = str(invoice["track_id"]), invoice["payment_url"]
            expires_at = int(invoice.get("expired_at") or now + LIFETIME_MINUTES * 60)
        else:
            raise ValueError(f"Unknown provider {provider!r}")

        self.db.create_payment(order_id, user_id, provider, amount_cents, track_id, url, expires_at)
        return self.db.get_payment(order_id)

    async def check(self, order_id: str) -> str:
        """Ask the provider for the live status and credit once if paid. Returns paid|pending|expired."""
        lock = self.locks.setdefault(order_id, asyncio.Lock())
        async with lock:
            row = self.db.get_payment(order_id)
            if row is None:
                raise ValueError(f"Unknown payment {order_id}")
            if row["credited"]:
                return "paid"
            if row["status"] == "expired":
                return "expired"

            if row["provider"] == "stripe":
                info = await asyncio.to_thread(stripe.confirm_session_paid, row["track_id"])
                paid = (
                    info["paid"]
                    and info["currency"].lower() == "usd"
                    and info["amount_cents"] >= row["amount_cents"]
                    and info["order_id"] in ("", order_id)
                )
                expired = info.get("expired", False)
            else:
                info = await asyncio.to_thread(oxapay.confirm_invoice_paid, row["track_id"])
                paid = info["paid"] and info["order_id"] in ("", order_id)
                expired = info["status"].strip().lower() == "expired"

            if paid:
                result = self.db.credit_payment(order_id)
                if result:
                    log.info("Credited %s (%s cents) to %s", order_id, row["amount_cents"], row["user_id"])
                    try:
                        await self.on_credited(self.db.get_payment(order_id), *result)
                    except Exception:
                        log.exception("on_credited failed for %s", order_id)
                return "paid"

            overdue = row["expires_at"] and time.time() > row["expires_at"] + 3600 and not info.get("pending")
            if expired or overdue:
                self.db.set_payment_status(order_id, "expired")
                return "expired"
            return "pending"

    async def poll_pending(self) -> None:
        for row in self.db.pending_payments():
            try:
                await self.check(row["order_id"])
            except Exception as e:
                log.warning("Payment check failed for %s: %s", row["order_id"], e)

    # ---------- webhooks (need a public URL; polling covers payments without them) ----------

    async def stripe_webhook(self, request: web.Request) -> web.Response:
        raw = await request.read()
        event = stripe.verify_webhook(raw, request.headers.get("Stripe-Signature", ""))
        if event is None:
            return web.Response(status=400, text="invalid signature")
        if event.get("type") in stripe.PAID_EVENTS + stripe.FAILED_EVENTS:
            session_id = ((event.get("data") or {}).get("object") or {}).get("id", "")
            row = self.db.payment_by_track("stripe", session_id)
            if row:
                await self.check(row["order_id"])
        return web.Response(text="ok")

    async def oxapay_webhook(self, request: web.Request) -> web.Response:
        raw = await request.read()
        if not oxapay.verify_webhook_hmac(raw, request.headers.get("HMAC", ""), kind="payment"):
            return web.Response(status=400, text="invalid hmac")
        try:
            track_id = str(json.loads(raw).get("track_id") or "")
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid json")
        row = self.db.payment_by_track("oxapay", track_id)
        if row:
            await self.check(row["order_id"])
        return web.Response(text="ok")

    async def stripe_return(self, request: web.Request) -> web.Response:
        success = request.query.get("status") == "success"
        title = "Payment received" if success else "Payment cancelled"
        body = (
            "Your credits will appear in your wallet in a moment. You can close this tab and go back to Discord."
            if success else "No charge was made. You can close this tab and go back to Discord."
        )
        html = (
            "<!doctype html><meta charset='utf-8'><title>{t}</title>"
            "<body style='font-family:system-ui;background:#0f1115;color:#e6e6e6;text-align:center;padding-top:15vh'>"
            "<h1>{icon} {t}</h1><p>{b}</p></body>"
        ).format(t=escape(title), b=escape(body), icon="✅" if success else "↩️")
        return web.Response(text=html, content_type="text/html")

    def routes(self) -> list[web.RouteDef]:
        return [
            web.post("/stripe/webhook", self.stripe_webhook),
            web.post("/oxapay/webhook", self.oxapay_webhook),
            web.get("/stripe/return", self.stripe_return),
        ]
