import datetime
import hmac
import logging
import secrets
import time
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Awaitable, Callable

from aiohttp import web

from db import Database

log = logging.getLogger("dashboard")

MONEY_KEYS = {"min_ticket_credits", "cancel_fee", "service_fee", "min_topup", "max_topup"}
SESSION_COOKIE = "dash_session"
WEAK_PASSWORDS = {"change-me", "changeme", "password", "admin", "123456", "12345678"}
MIN_PASSWORD_LEN = 10
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 15 * 60


def password_problem(password: str) -> str | None:
    """Why this dashboard password must not be used, or None when it's acceptable."""
    if password.strip().lower() in WEAK_PASSWORDS:
        return "it is the example/default password"
    if len(password) < MIN_PASSWORD_LEN:
        return f"it is shorter than {MIN_PASSWORD_LEN} characters"
    return None

FIELDS = [
    ("brand_name", "Brand name (panel title)"),
    ("savings_percent", "Panel headline: “Save up to X%”"),
    ("min_ticket_credits", "Minimum credits to open a ticket ($)"),
    ("ticket_timeout_minutes", "Ticket inactivity auto-close (minutes)"),
    ("cancel_fee", "Cancellation fee ($)"),
    ("service_fee", "Service fee added to every ride ($)"),
    ("min_topup", "Smallest Add Funds payment ($)"),
    ("max_topup", "Largest Add Funds payment ($)"),
    ("price_multiplier", "Price multiplier (ride cost = quoted price × this)"),
    ("success_channel_id", "Success channel ID"),
    ("ticket_category_id", "Ride ticket category ID"),
    ("funds_category_id", "Add Funds ticket category ID"),
    ("staff_role_id", "Staff role ID"),
    ("operator_card_id", "API card ID to charge (blank = default card)"),
    ("add_funds_message", "Message shown when a user needs to add funds"),
]

STYLE = """
body{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;max-width:1100px;margin:auto;padding:24px}
h1{margin-top:0}h2{margin-top:36px;border-bottom:1px solid #2a2e37;padding-bottom:6px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 8px;border-bottom:1px solid #2a2e37;text-align:left}
input,select,button{background:#1a1d24;color:#e6e6e6;border:1px solid #333845;border-radius:6px;padding:6px 8px}
input[type=text]{width:100%}button{background:#5865f2;border:none;cursor:pointer;padding:8px 14px}
.grid{display:grid;grid-template-columns:320px 1fr;gap:10px 16px;align-items:center}
.flash{background:#1f3b24;padding:10px;border-radius:6px}.neg{color:#ff6b6b}.pos{color:#5ee28a}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.stat{background:#1a1d24;border:1px solid #2a2e37;border-radius:10px;padding:14px}
.stat b{display:block;font-size:22px;margin-top:4px}.stat span{color:#8a8fa3;font-size:13px}
.nav a{background:#5865f2;color:#fff;text-decoration:none;padding:6px 12px;border-radius:6px;font-size:14px;margin-right:6px}
.note{color:#8a8fa3;font-size:13px}
"""


def to_cents(value: str) -> int:
    try:
        return int((Decimal(value.strip().lstrip("$")) * 100).quantize(Decimal(1)))
    except (InvalidOperation, AttributeError):
        raise ValueError(f"Invalid amount: {value!r}")


def money(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def ts(value: int | None) -> str:
    return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M") if value else "—"


def create_app(
    db: Database,
    password: str,
    resolve_user: Callable[[str], str],
    on_settings_changed: Callable[[], Awaitable[None]] | None = None,
    public_routes: list[web.RouteDef] | None = None,
) -> web.Application:
    sessions: set[str] = set()
    failures: dict[str, list[float]] = {}
    public_paths = {r.path for r in public_routes or []} | {"/health", "/healthz"}

    @web.middleware
    async def require_login(request: web.Request, handler):
        if request.path in public_paths:
            return await handler(request)
        if not password:
            raise web.HTTPNotFound()
        if request.path == "/login" or request.cookies.get(SESSION_COOKIE) in sessions:
            return await handler(request)
        raise web.HTTPFound("/login")

    async def health(_request: web.Request):
        return web.json_response({"ok": True, "service": "ride-bot"})

    def login_page(error: str = "") -> web.Response:
        html = f"""<!doctype html><html><head><meta charset='utf-8'><title>Dashboard login</title>
<style>{STYLE} form{{max-width:320px;margin:80px auto;display:flex;flex-direction:column;gap:12px}}</style></head><body>
<form method='post' action='/login'><h1>Dashboard</h1>
{f"<p class='neg'>{escape(error)}</p>" if error else ""}
<input type='password' name='password' placeholder='Password' autofocus required>
<button type='submit'>Log in</button></form></body></html>"""
        return web.Response(text=html, content_type="text/html")

    async def login_get(request: web.Request):
        return login_page()

    def client_ip(request: web.Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[0].strip() or request.remote or "?"

    async def login_post(request: web.Request):
        ip = client_ip(request)
        now = time.time()
        recent = [t for t in failures.get(ip, []) if now - t < LOGIN_LOCK_SECONDS]
        # X-Forwarded-For can be spoofed, so a global cap backs up the per-IP one.
        overall = [t for t in failures.get("*", []) if now - t < LOGIN_LOCK_SECONDS]
        if len(recent) >= LOGIN_MAX_FAILS or len(overall) >= LOGIN_MAX_FAILS * 6:
            failures[ip], failures["*"] = recent, overall
            return login_page("Too many failed attempts. Try again in 15 minutes.")
        form = await request.post()
        if not hmac.compare_digest(str(form.get("password", "")).encode(), password.encode()):
            failures[ip], failures["*"] = recent + [now], overall + [now]
            log.warning("Dashboard login failed from %s (%d/%d)", ip, len(recent) + 1, LOGIN_MAX_FAILS)
            return login_page("Wrong password.")
        failures.pop(ip, None)
        token = secrets.token_urlsafe(32)
        sessions.add(token)
        response = web.HTTPFound("/")
        secure = request.secure or request.headers.get("X-Forwarded-Proto", "") == "https"
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Strict", secure=secure,
                            max_age=7 * 86400)
        raise response

    async def logout(request: web.Request):
        sessions.discard(request.cookies.get(SESSION_COOKIE, ""))
        response = web.HTTPFound("/login")
        response.del_cookie(SESSION_COOKIE)
        raise response

    def page(flash: str = "") -> web.Response:
        s = db.get_settings()
        settings_rows = []
        for key, label in FIELDS:
            value = money(int(s[key] or 0)).lstrip("$") if key in MONEY_KEYS else s[key]
            settings_rows.append(
                f"<label for='{key}'>{escape(label)}</label>"
                f"<input type='text' id='{key}' name='{key}' value='{escape(value, quote=True)}'>"
            )

        wallet_rows = "".join(
            f"<tr><td>{escape(resolve_user(w['user_id']))}</td><td><code>{w['user_id']}</code></td>"
            f"<td>{money(w['balance_cents'])}</td></tr>"
            for w in db.wallets()
        ) or "<tr><td colspan=3>No wallets yet</td></tr>"

        ride_rows = "".join(
            f"<tr><td>#{r['id']}</td><td>{escape(resolve_user(r['user_id']))}</td><td>{escape(r['tier'])}</td>"
            f"<td>{money(r['cost_cents'])}</td><td>{escape(r['status'])}</td><td>{money(r['refund_cents'])}</td>"
            f"<td>{ts(r['created_at'])}</td></tr>"
            for r in db.recent_rides()
        ) or "<tr><td colspan=7>No rides yet</td></tr>"

        tx_rows = "".join(
            f"<tr><td>{ts(t['created_at'])}</td><td>{escape(resolve_user(t['user_id']))}</td>"
            f"<td class='{'pos' if t['delta_cents'] >= 0 else 'neg'}'>{money(t['delta_cents'])}</td>"
            f"<td>{money(t['balance_after'])}</td><td>{escape(t['reason'])}</td></tr>"
            for t in db.transactions()
        ) or "<tr><td colspan=5>No transactions yet</td></tr>"

        payment_rows = "".join(
            f"<tr><td>{ts(p['created_at'])}</td><td>{escape(resolve_user(p['user_id']))}</td>"
            f"<td>{escape(p['provider'])}</td><td>{money(p['amount_cents'])}</td>"
            f"<td class='{'pos' if p['status'] == 'paid' else ''}'>{escape(p['status'])}</td>"
            f"<td><code>{escape(p['order_id'])}</code></td></tr>"
            for p in db.recent_payments()
        ) or "<tr><td colspan=6>No payments yet</td></tr>"

        p = db.profit_stats()
        stat_cards = "".join(
            f"<div class='stat'><span>{label}</span><b class='{cls}'>{value}</b></div>"
            for label, value, cls in (
                ("Rides booked", p["rides"], ""),
                ("Cancelled", p["cancelled"], ""),
                ("Customers paid", money(p["revenue"]), ""),
                ("Spent on card", money(p["card_cost"]), ""),
                ("Profit", money(p["profit"]), "pos" if p["profit"] >= 0 else "neg"),
                ("Top-ups received", money(p["topups"]), ""),
            )
        )

        ticket_rows = "".join(
            f"<tr><td><code>{t['channel_id']}</code></td><td>{escape(resolve_user(t['user_id']))}</td>"
            f"<td>{escape(t['state'])}</td><td>{ts(t['last_activity'])}</td></tr>"
            for t in db.open_tickets()
        ) or "<tr><td colspan=4>No open tickets</td></tr>"

        html = f"""<!doctype html><html><head><meta charset='utf-8'><title>Ride Bot Dashboard</title>
<style>{STYLE}</style></head><body>
<h1>{escape(s['brand_name'])} Dashboard <a href='/logout' style='float:right;font-size:14px;color:#8a8fa3'>Log out</a></h1>
{f"<p class='flash'>{escape(flash)}</p>" if flash else ""}
<p class='nav'><a href='#profits'>📈 Profits</a><a href='#settings'>Settings</a><a href='#wallets'>Wallets</a><a href='#payments'>Payments</a><a href='#rides'>Rides</a></p>
<h2 id='profits'>Profits</h2><div class='stats'>{stat_cards}</div>
<p class='note'>Customers paid = ride fares after refunds (cancel fees included). Spent on card = the ride price charged to the operator card, before your markup and service fee.</p>
<h2 id='settings'>Settings</h2>
<form method='post' action='/settings'><div class='grid'>{''.join(settings_rows)}</div>
<p><button type='submit'>Save settings</button></p></form>

<h2>Adjust wallet</h2>
<form method='post' action='/wallet' style='display:flex;gap:8px;flex-wrap:wrap'>
<input name='user_id' placeholder='Discord user ID' required>
<select name='action'><option value='add'>Add</option><option value='remove'>Remove</option><option value='set'>Set to</option></select>
<input name='amount' placeholder='Amount ($)' required>
<input name='reason' placeholder='Reason (optional)'>
<button type='submit'>Apply</button></form>

<h2 id='wallets'>Wallets</h2><table><tr><th>User</th><th>ID</th><th>Balance</th></tr>{wallet_rows}</table>
<h2>Open tickets</h2><table><tr><th>Channel</th><th>User</th><th>State</th><th>Last activity</th></tr>{ticket_rows}</table>
<h2 id='payments'>Payments (Add Funds)</h2><table><tr><th>When</th><th>User</th><th>Method</th><th>Amount</th><th>Status</th><th>Order</th></tr>{payment_rows}</table>
<h2 id='rides'>Recent rides</h2><table><tr><th>#</th><th>User</th><th>Tier</th><th>Cost</th><th>Status</th><th>Refund</th><th>Created</th></tr>{ride_rows}</table>
<h2>Transactions</h2><table><tr><th>When</th><th>User</th><th>Change</th><th>Balance after</th><th>Reason</th></tr>{tx_rows}</table>
</body></html>"""
        return web.Response(text=html, content_type="text/html")

    async def index(request: web.Request):
        return page(request.query.get("msg", ""))

    async def save_settings(request: web.Request):
        form = await request.post()
        try:
            for key, _ in FIELDS:
                if key not in form:
                    continue
                value = str(form[key]).strip()
                if key in MONEY_KEYS:
                    value = str(to_cents(value or "0"))
                elif key == "ticket_timeout_minutes":
                    value = str(max(1, int(value or 0)))
                elif key == "price_multiplier":
                    value = str(float(value or 1))
                elif key == "savings_percent":
                    value = str(max(0, min(100, int(float(value or 0)))))
                db.set(key, value)
        except ValueError as e:
            return page(f"Not saved: {e}")
        if on_settings_changed:
            await on_settings_changed()
        raise web.HTTPFound("/?msg=Settings+saved")

    async def adjust_wallet(request: web.Request):
        form = await request.post()
        user_id = str(form.get("user_id", "")).strip()
        if not user_id.isdigit():
            return page("User ID must be a numeric Discord ID.")
        try:
            amount = to_cents(str(form.get("amount", "")))
        except ValueError as e:
            return page(str(e))
        reason = str(form.get("reason") or "").strip() or "dashboard adjustment"
        action = form.get("action")
        if action == "set":
            before, after = db.set_balance(user_id, amount, reason)
        else:
            before, after = db.adjust(user_id, amount if action == "add" else -amount, reason)
        return page(f"{resolve_user(user_id)}: {money(before)} → {money(after)}")

    app = web.Application(middlewares=[require_login])
    app.add_routes([
        web.get("/health", health),
        web.get("/healthz", health),
        web.get("/login", login_get),
        web.post("/login", login_post),
        web.get("/logout", logout),
        web.get("/", index),
        web.post("/settings", save_settings),
        web.post("/wallet", adjust_wallet),
        *(public_routes or []),
    ])
    return app
