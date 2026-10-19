import asyncio
import logging
import math
import os
import re
import uuid
from collections import defaultdict

import discord
from aiohttp import web
from discord import app_commands, ui
from discord.ext import tasks

import config as app_config
import db as store
import payments as pay
from api import ApiError, PartnerClient
from dashboard import create_app, password_problem
from oxapay_client import sandbox_mode as oxapay_sandbox_mode

# Prefer: python run.py  /  start.bat  (opens a setup page if .env is missing)
_cfg = app_config.require_runtime()
DISCORD_TOKEN = _cfg["DISCORD_TOKEN"]
API_BASE_URL = _cfg["API_BASE_URL"]
API_KEY_ID = _cfg["API_KEY_ID"]
API_SECRET = _cfg["API_SECRET"]
API_SIGN_ORDER = [p.strip() for p in _cfg["API_SIGN_ORDER"].split(",") if p.strip()]
OPERATOR_MEMBER_ID = _cfg["OPERATOR_MEMBER_ID"]
GUILD_ID = _cfg["GUILD_ID"] or None
DB_PATH = _cfg["DB_PATH"]
DASHBOARD_PASSWORD = _cfg["DASHBOARD_PASSWORD"]
DASHBOARD_HOST = _cfg["DASHBOARD_HOST"]
DASHBOARD_PORT = int(os.getenv("PORT") or _cfg["DASHBOARD_PORT"] or 8080)

STATE_EMOJI = {
    "searching": "🔎", "assigned": "✅", "arriving": "🚗", "arrived": "📍", "in_progress": "🛣️",
    "completed": "🏁", "cancelled": "❌", "idle": "💤", "other": "❔",
}

BLURPLE = discord.Color.blurple()
GREEN = discord.Color.green()
RED = discord.Color.red()
ORANGE = discord.Color.orange()
GOLD = discord.Color.gold()
GREY = discord.Color.light_grey()

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

db = store.Database(DB_PATH)


# ---------- helpers ----------

def money(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def dollars_to_cents(amount: float) -> int:
    return int(round(amount * 100))


def location_line(loc: dict | None) -> str:
    if not loc:
        return "—"
    name = loc.get("name") or loc.get("title") or f"{loc.get('lat')}, {loc.get('lon')}"
    return f"[{name}]({loc['map_url']})" if loc.get("map_url") else name


def location_input(loc: dict) -> dict:
    out = {"lat": loc["lat"], "lon": loc["lon"]}
    if loc.get("name") or loc.get("title"):
        out["name"] = loc.get("name") or loc.get("title")
    if loc.get("place_id"):
        out["place_id"] = loc["place_id"]
    return out


def is_staff(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    role_id = db.get("staff_role_id")
    return bool(role_id) and any(str(r.id) == role_id for r in member.roles)


def timeout_minutes() -> int:
    return max(1, db.get_int("ticket_timeout_minutes"))


# ---------- Components V2 builders ----------

SEP = object()
SEP_LARGE = object()


def button(label: str, custom_id: str, style=discord.ButtonStyle.secondary, disabled: bool = False, emoji=None):
    return ui.Button(label=label, custom_id=custom_id, style=style, disabled=disabled, emoji=emoji)


def close_button() -> ui.Button:
    return button("Close Ticket", "tk:close", discord.ButtonStyle.danger, emoji="🔒")


def with_thumbnail(text: str, url: str | None):
    return ui.Section(ui.TextDisplay(text), accessory=ui.Thumbnail(url)) if url else text


def card(*blocks, color: discord.Color | None = BLURPLE) -> ui.LayoutView:
    """Build a Components V2 message: one container of text blocks, separators and button rows.
    A list/tuple block becomes action rows (5 buttons per row)."""
    container = ui.Container(accent_color=color)
    for block in blocks:
        if block is None or block == "":
            continue
        if block is SEP:
            container.add_item(ui.Separator())
        elif block is SEP_LARGE:
            container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.large))
        elif isinstance(block, str):
            container.add_item(ui.TextDisplay(block[:4000]))
        elif isinstance(block, (list, tuple)):
            buttons = [b for b in block if b is not None]
            for i in range(0, len(buttons), 5):
                container.add_item(ui.ActionRow(*buttons[i:i + 5]))
        else:
            container.add_item(block)
    view = ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def notice(title: str, body: str = "", color: discord.Color = BLURPLE, buttons=None) -> ui.LayoutView:
    return card(f"### {title}", body, SEP if buttons else None, buttons, color=color)


def error_card(err: Exception | str, title: str = "Something went wrong", buttons=None) -> ui.LayoutView:
    if isinstance(err, ApiError):
        body = f"`{err.code}` {err.message}"
        if err.request_id:
            body += f"\n-# request_id: {err.request_id}"
    else:
        if isinstance(err, Exception) and not isinstance(err, UserError):
            log.exception("Unexpected error", exc_info=err)
        body = str(err)
    return notice(f"❌ {title}", body, RED, buttons)


def kv(*pairs: tuple[str, str | None]) -> str:
    return "\n".join(f"**{k}:** {v}" for k, v in pairs if v)


# ---------- bot ----------

def is_admin(user: discord.abc.User) -> bool:
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


class AdminCommandTree(app_commands.CommandTree):
    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if is_admin(inter.user):
            return True
        await inter.response.send_message(view=notice("⛔ Admins only", "Only server admins can use bot commands.", RED),
                                          ephemeral=True)
        return False


class RideBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = AdminCommandTree(self)
        self.api = PartnerClient(API_BASE_URL, API_KEY_ID, API_SECRET, API_SIGN_ORDER)
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.test_rides: dict[int, dict] = {}
        self.web_runner: web.AppRunner | None = None
        self.stock: int | None = None

    async def setup_hook(self) -> None:
        await self.api.start()
        try:
            ping = await self.api.ping()
            log.info("API signature OK, caller: %s", ping.get("caller"))
        except ApiError as e:
            log.error("API ping failed (%s). Check API_KEY_ID / API_SECRET / API_SIGN_ORDER.", e)

        providers = pay.enabled_providers()
        dashboard_password = DASHBOARD_PASSWORD
        problem = password_problem(dashboard_password) if dashboard_password else None
        if problem:
            log.error("Web dashboard DISABLED: DASHBOARD_PASSWORD is unsafe (%s). Set a strong one.", problem)
            dashboard_password = ""
        if oxapay_sandbox_mode():
            log.warning("OXAPAY_SANDBOX is ON — sandbox (fake) crypto payments will credit real wallets. "
                        "Turn it off in production.")
        # Hosted (Railway): always listen on 0.0.0.0:$PORT for health checks + webhooks.
        # Local: start only when dashboard password or payment webhooks are configured.
        hosted = app_config.is_hosted()
        if dashboard_password or providers or hosted:
            app = create_app(db, dashboard_password, self.resolve_user, refresh_panels, payments.routes())
            self.web_runner = web.AppRunner(app)
            await self.web_runner.setup()
            await web.TCPSite(self.web_runner, DASHBOARD_HOST, DASHBOARD_PORT).start()
            log.info(
                "Web server on http://%s:%s (dashboard %s · hosted=%s · public=%s)",
                DASHBOARD_HOST,
                DASHBOARD_PORT,
                "on" if dashboard_password else "off",
                hosted,
                _cfg.get("PUBLIC_BASE_URL") or "(unset)",
            )
        log.info("Payment providers: %s", ", ".join(providers) or "none (Add Funds shows the manual message)")
        log.info("Database: %s", DB_PATH)

        auto_close_tickets.start()
        refresh_panels_loop.start()
        poll_payments.start()

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def resolve_user(self, user_id: str) -> str:
        user = self.get_user(int(user_id)) if str(user_id).isdigit() else None
        return f"{user.name}" if user else str(user_id)

    async def close(self) -> None:
        if self.web_runner:
            await self.web_runner.cleanup()
        await self.api.close()
        await super().close()


bot = RideBot()


async def notify_credited(payment, before: int, after: int):
    user = bot.get_user(int(payment["user_id"])) or await bot.fetch_user(int(payment["user_id"]))
    try:
        await user.send(view=card(
            "### ✅ Funds added",
            f"**{money(payment['amount_cents'])}** was added to your wallet.",
            SEP,
            kv(("Previous balance", money(before)), ("New balance", money(after))),
            f"-# Order `{payment['order_id']}`",
            color=GREEN,
        ))
    except discord.HTTPException:
        pass


payments = pay.Payments(db, lambda: db.get("brand_name"), notify_credited)


@tasks.loop(seconds=20)
async def poll_payments():
    await payments.poll_pending()


@poll_payments.before_loop
async def before_poll_payments():
    await bot.wait_until_ready()


# ---------- booking panel ----------

def guide_channel_id() -> str | None:
    value = db.get("guide_message")
    return value.split(":")[0] if value else None


def panel_view() -> ui.LayoutView:
    s = db.get_settings()
    percent = max(0, min(100, int(float(s["savings_percent"] or 0))))
    guide = guide_channel_id()
    funds_panels = db.panel_messages("funds_panel_messages")
    funds_where = f" in <#{funds_panels[0][0]}>" if funds_panels else ""
    if bot.stock is None:
        status = "🟢 **ONLINE**"
    elif bot.stock > 0:
        status = f"🟢 **ONLINE**  ·  📦 **{bot.stock} Available**"
    else:
        status = "🟢 **ONLINE**  ·  🔴 **Out of stock**"

    return card(
        f"# 🚗 {s['brand_name']} — Rides\n{status}",
        SEP,
        f"### 💸 Save Up to {percent}%\nOpen a ticket and see exactly what you'll pay before you confirm.",
        SEP_LARGE,
        "### 🛒 How It Works\n"
        f"`01` **Add Funds** to your wallet{funds_where}\n"
        "`02` Click **Book Ride** to open a private ticket\n"
        "`03` Enter your trip and pick your ride\n"
        "`04` Track your driver live in your ticket\n\n"
        f"💰 **Minimum balance:** {money(db.get_int('min_ticket_credits'))}\n"
        f"🧾 **Service fee:** {money(db.get_int('service_fee'))} per ride",
        SEP,
        f"📖 New here? Read the guide in <#{guide}>" if guide else None,
        [
            button("Book Ride", "tk:book", discord.ButtonStyle.primary, emoji="🚗"),
            button("My Wallet", "tk:wallet", discord.ButtonStyle.secondary, emoji="👛"),
        ],
        SEP,
        "-# *You only pay once you pick your ride.*",
    )


def guide_view() -> ui.LayoutView:
    s = db.get_settings()
    panels = db.panel_messages()
    book_where = f" in <#{panels[0][0]}>" if panels else ""
    fee = money(db.get_int("service_fee"))
    return card(
        f"# 📖 How to Book a Ride with {s['brand_name']}",
        SEP,
        f"### ✨ Why book with us?\n"
        "💸 **Cheaper fares** than booking directly on Uber or Lyft\n"
        "⚡ **Fast booking** — your ride is booked in a couple of clicks\n"
        "🔒 **Private tickets** — only you and staff can see your booking\n"
        "📍 **Live tracking** — driver, car and plate right here in Discord\n"
        "👛 **Wallet credits** — top up once, ride whenever you want\n"
        f"↩️ **Easy cancellations** — refunded to your wallet (minus a {money(db.get_int('cancel_fee'))} cancellation fee)",
        SEP_LARGE,
        "### 🛒 Steps\n"
        f"`01` **Add funds** — click **Add Funds** on the booking panel (minimum {money(db.get_int('min_ticket_credits'))})\n"
        f"`02` **Open a ticket** — click **Book Ride**{book_where}\n"
        "`03` **Enter your pickup** — paste your address from Google Maps\n"
        "`04` **Confirm your pickup** — check the map link, or choose another match from the dropdown\n"
        "`05` **Enter and confirm your drop-off** the same way\n"
        f"`06` **Pick your ride** — the fare (including the {fee} service fee) is taken from your wallet\n"
        "`07` **Track your ride** — tap **Check Status** for live updates, or **Cancel Ride** if plans change",
        SEP_LARGE,
        "### 📍 Getting your address from Google Maps\n"
        "1. Open **Google Maps** and search for the place, or long-press the exact spot on the map\n"
        "2. Tap the address at the bottom of the screen and **copy** it\n"
        "3. **Paste** it into the pickup or drop-off box\n\n"
        "**Example:** `350 5th Ave, New York, NY 10118`",
        "-# Tip: include the street number, city and ZIP code for the best match.",
        SEP,
        "-# Questions? Ask a staff member.",
    )


async def refresh_guide():
    value = db.get("guide_message")
    if not value:
        return
    channel_id, message_id = (int(x) for x in value.split(":"))
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        await channel.get_partial_message(message_id).edit(view=guide_view())
    except discord.NotFound:
        db.set("guide_message", "")
    except discord.HTTPException as e:
        log.warning("Could not refresh guide: %s", e)


async def update_stock():
    """Ready slots that aren't carrying an active ride."""
    try:
        slots = await bot.api.list_slots()
    except Exception as e:
        log.warning("Couldn't refresh slot stock: %s", e)
        return
    busy = db.busy_slots()
    bot.stock = sum(1 for s in slots if s["slot_id"] not in busy and s.get("status") != "disabled")


async def refresh_panels():
    await update_stock()
    await refresh_guide()
    for channel_id, message_id in db.panel_messages():
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            continue
        try:
            await channel.get_partial_message(message_id).edit(content=None, embeds=[], view=panel_view())
        except discord.NotFound:
            db.remove_panel_message(message_id)
        except discord.HTTPException:
            # Old embed-style panels can't be switched to V2 in place; repost them.
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass
            msg = await channel.send(view=panel_view())
            db.add_panel_message(channel_id, msg.id)
    for channel_id, message_id in db.panel_messages("funds_panel_messages"):
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            continue
        try:
            await channel.get_partial_message(message_id).edit(view=funds_panel_view())
        except discord.NotFound:
            db.remove_panel_message(message_id, key="funds_panel_messages")
        except discord.HTTPException as e:
            log.warning("Couldn't refresh Add Funds panel %s: %s", message_id, e)


@tasks.loop(minutes=2)
async def refresh_panels_loop():
    await refresh_panels()


@refresh_panels_loop.before_loop
async def before_refresh_panels():
    await bot.wait_until_ready()


TOPUP_PRESETS = (1000, 2500, 5000, 10000)


def topup_range() -> str:
    return f"{money(db.get_int('min_topup'))} – {money(db.get_int('max_topup'))}"


def funds_panel_view() -> ui.LayoutView:
    return card(
        f"# 💵 {db.get('brand_name')} — Add Funds",
        SEP,
        "Top up your wallet to book rides. Click **Add Funds** to open a private ticket and pick how you'd like to pay.",
        SEP,
        [
            button("Add Funds", "tk:funds", discord.ButtonStyle.success, emoji="💵"),
            button("Check Wallet", "tk:wallet", discord.ButtonStyle.secondary, emoji="👛"),
        ],
        color=GREEN,
    )


def funds_methods_view(user_id: int, welcome: bool = False) -> ui.LayoutView:
    balance = db.balance(user_id)
    minimum = db.get_int("min_ticket_credits")
    providers = pay.enabled_providers()
    return card(
        "### 💵 Add Funds Ticket",
        f"Welcome <@{user_id}>!" if welcome else None,
        kv(
            ("Your balance", money(balance)),
            ("Minimum to book a ride", money(minimum)),
            ("You need", money(minimum - balance) if balance < minimum else None),
        ),
        SEP,
        "**How would you like to pay?**",
        [button(pay.PROVIDER_LABELS[p], f"tk:fund:method:{p}", discord.ButtonStyle.primary, emoji=pay.PROVIDER_EMOJI[p])
         for p in providers]
        + [button("Other", "tk:fund:other", discord.ButtonStyle.secondary, emoji="🧾"), close_button()],
        "-# Card & crypto are credited automatically · **Other** connects you with a staff member"
        if providers else "-# A staff member will help you complete the payment.",
        color=GREEN,
    )


def funds_amount_view(provider: str) -> ui.LayoutView:
    low, high = db.get_int("min_topup"), db.get_int("max_topup")
    presets = [c for c in TOPUP_PRESETS if low <= c <= high]
    return card(
        f"### {pay.PROVIDER_EMOJI[provider]} Pay by {pay.PROVIDER_LABELS[provider]}",
        "**How much would you like to add?**",
        [button(money(c), f"tk:fund:pay:{provider}:{c}", discord.ButtonStyle.success) for c in presets]
        + [button("Custom amount", f"tk:fund:custom:{provider}", emoji="✏️")],
        [button("Back", "tk:fund:methods", emoji="↩️"), close_button()],
        f"-# Payments between {topup_range()} · 1 credit = $1.00",
        color=GREEN,
    )


def funds_invoice_view(payment, note: str | None = None) -> ui.LayoutView:
    provider = payment["provider"]
    return card(
        "### 🧾 Complete your payment",
        kv(
            ("Amount", money(payment["amount_cents"])),
            ("Method", f"{pay.PROVIDER_EMOJI[provider]} {pay.PROVIDER_LABELS[provider]}"),
            ("Link expires", f"<t:{payment['expires_at']}:R>" if payment["expires_at"] else None),
        ),
        note,
        [
            ui.Button(label=f"Pay {money(payment['amount_cents'])}", url=payment["payment_url"], emoji="🔗"),
            button("Check Payment", f"tk:fund:check:{payment['order_id']}", discord.ButtonStyle.secondary, emoji="🔄"),
            close_button(),
        ],
        "-# Credits are added automatically once the payment confirms — crypto can take a few minutes.",
        color=GREEN,
    )


def validate_topup(amount: int) -> str | None:
    low, high = db.get_int("min_topup"), db.get_int("max_topup")
    if amount < low or amount > high:
        return f"Amount must be between {money(low)} and {money(high)}."
    return None


async def start_payment(inter: discord.Interaction, amount: int, provider: str):
    """Create the provider payment and show the pay link. The interaction must already be deferred."""
    try:
        payment = await payments.create(inter.user.id, inter.user.name, amount, provider)
    except Exception as e:
        log.warning("Creating %s payment failed: %s", provider, e)
        return await inter.edit_original_response(view=error_card(
            str(e), "Couldn't create the payment",
            [button("Try Again", "tk:fund:methods", discord.ButtonStyle.primary), close_button()],
        ))
    await inter.edit_original_response(view=funds_invoice_view(payment))


def parse_amount(value: str) -> int | None:
    try:
        return dollars_to_cents(float(value.strip().lstrip("$").replace(",", "")))
    except ValueError:
        return None


class TopupAmountModal(ui.Modal, title="Add funds"):
    amount = ui.TextInput(label="Amount in USD", placeholder="25.00", min_length=1, max_length=8)

    def __init__(self, provider: str):
        super().__init__()
        self.provider = provider

    async def on_submit(self, inter: discord.Interaction):
        cents = parse_amount(self.amount.value)
        problem = "Enter a number like `25` or `25.50`." if cents is None else validate_topup(cents)
        if problem:
            return await inter.response.send_message(view=notice(problem, color=ORANGE), ephemeral=True)
        await inter.response.edit_message(view=notice("⏳ Creating your payment…", color=GOLD))
        await start_payment(inter, cents, self.provider)


def manual_request_view(user_id: int, amount: int, method: str, note: str, role: discord.Role | None) -> ui.LayoutView:
    ping = role.mention if role else "Staff"
    return card(
        "### 🧾 Manual payment request",
        f"{ping} — <@{user_id}> wants to add funds with another payment method.",
        SEP,
        kv(("Amount", f"**{money(amount)}**"), ("Payment method", method), ("Note", note or None)),
        SEP,
        [
            button(f"Confirm {money(amount)}", f"tk:fund:approve:{amount}", discord.ButtonStyle.success, emoji="✅"),
            button("Decline", "tk:fund:decline", discord.ButtonStyle.danger, emoji="✖️"),
            close_button(),
        ],
        "-# Staff: send payment instructions here. Click **Confirm** only after you've received the payment.",
        color=GOLD,
    )


class OtherPaymentModal(ui.Modal, title="Other payment method"):
    amount = ui.TextInput(label="How much do you want to add? (USD)", placeholder="25.00", min_length=1, max_length=8)
    method = ui.TextInput(label="Payment method", placeholder="The payment app or method you'd like to use",
                          max_length=60)
    note = ui.TextInput(label="Anything staff should know? (optional)", style=discord.TextStyle.paragraph,
                        required=False, max_length=300)

    async def on_submit(self, inter: discord.Interaction):
        cents = parse_amount(self.amount.value)
        problem = "Enter a number like `25` or `25.50`." if cents is None else validate_topup(cents)
        if problem:
            return await inter.response.send_message(view=notice(problem, color=ORANGE), ephemeral=True)
        ticket = db.get_ticket(inter.channel_id)
        if not ticket or ticket["state"] == store.CLOSED:
            return await inter.response.send_message(view=notice("This ticket is no longer active."), ephemeral=True)

        db.update_ticket(inter.channel_id, state=store.AWAITING_STAFF)
        await inter.response.edit_message(view=card(
            "### 🧾 Request sent",
            f"A staff member has been notified and will help you pay **{money(cents)}** via **{self.method.value}**.",
            [close_button()],
            "-# This ticket stays open until staff finishes your request.",
            color=GREEN,
        ))
        role = staff_role(inter.guild) if inter.guild else None
        await inter.channel.send(
            view=manual_request_view(inter.user.id, cents, self.method.value.strip(), self.note.value.strip(), role),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False),
        )


async def handle_staff_funds_decision(inter: discord.Interaction, ticket, step: str, parts: list[str]):
    if not is_staff(inter.user):
        return await inter.response.send_message(
            view=notice("⛔ Admins / staff only", "Only an admin or staff member can confirm or decline payments.", RED),
            ephemeral=True,
        )
    async with bot.locks[inter.channel_id]:
        ticket = db.get_ticket(inter.channel_id)
        if not ticket or ticket["state"] != store.AWAITING_STAFF:
            return await inter.response.send_message(view=notice("This request was already handled."), ephemeral=True)
        db.update_ticket(inter.channel_id, state=store.OPEN)
        user_id = int(ticket["user_id"])
        if step == "decline":
            return await inter.response.edit_message(view=notice(
                "✖️ Request declined", f"Declined by {inter.user.mention}. No credits were added.", GREY,
                [button("Pay another way", "tk:fund:methods", discord.ButtonStyle.primary), close_button()],
            ))
        amount = int(parts[2])
        before, after = db.adjust(user_id, amount, f"manual top-up approved by {inter.user}")
    await inter.response.edit_message(view=card(
        "### ✅ Funds added",
        f"**{money(amount)}** was added to <@{user_id}>'s wallet by {inter.user.mention}.",
        SEP,
        kv(("Previous balance", money(before)), ("New balance", money(after))),
        [close_button()],
        color=GREEN,
    ), allowed_mentions=discord.AllowedMentions.none())


async def handle_fund_action(inter: discord.Interaction, action: str):
    parts = action.split(":")
    step = parts[1] if len(parts) > 1 else ""
    ticket = db.get_ticket(inter.channel_id)
    if ticket and ticket["state"] != store.CLOSED:
        db.touch_ticket(inter.channel_id)

    if step in ("approve", "decline"):
        return await handle_staff_funds_decision(inter, ticket, step, parts)
    if step != "check" and ticket and ticket["kind"] == store.FUNDS and ticket["user_id"] != str(inter.user.id):
        return await inter.response.send_message(view=notice("Only the ticket owner can choose a payment."),
                                                 ephemeral=True)

    if step == "methods":
        if ticket and ticket["state"] == store.AWAITING_STAFF:
            return await inter.response.send_message(
                view=notice("Waiting on staff", "Your manual payment request is still open."), ephemeral=True
            )
        return await inter.response.edit_message(view=funds_methods_view(inter.user.id))
    if step == "method":
        provider = parts[2]
        if provider not in pay.enabled_providers():
            return await inter.response.edit_message(view=funds_methods_view(inter.user.id))
        return await inter.response.edit_message(view=funds_amount_view(provider))
    if step == "custom":
        return await inter.response.send_modal(TopupAmountModal(parts[2]))
    if step == "other":
        if ticket and ticket["state"] == store.AWAITING_STAFF:
            return await inter.response.send_message(
                view=notice("Waiting on staff", "Your manual payment request is still open."), ephemeral=True
            )
        return await inter.response.send_modal(OtherPaymentModal())
    if step == "pay":
        provider, amount = parts[2], int(parts[3])
        if provider not in pay.enabled_providers() or validate_topup(amount):
            return await inter.response.edit_message(view=funds_methods_view(inter.user.id))
        await inter.response.edit_message(view=notice("⏳ Creating your payment…", color=GOLD))
        return await start_payment(inter, amount, provider)
    if step == "check":
        order_id = parts[2]
        payment = db.get_payment(order_id)
        if not payment or (payment["user_id"] != str(inter.user.id) and not is_staff(inter.user)):
            return await inter.response.send_message(view=notice("Payment not found."), ephemeral=True)
        await inter.response.defer()
        try:
            status = await payments.check(order_id)
        except Exception as e:
            return await inter.edit_original_response(
                view=funds_invoice_view(payment, f"⚠️ Couldn't check the payment right now: {e}")
            )
        if status == "paid":
            return await inter.edit_original_response(view=card(
                "### ✅ Payment received",
                f"**{money(payment['amount_cents'])}** was added to your wallet.",
                SEP,
                f"**New balance:** {money(db.balance(int(payment['user_id'])))}",
                [close_button()],
                color=GREEN,
            ))
        if status == "expired":
            return await inter.edit_original_response(view=notice(
                "⌛ Payment link expired", "No charge was made. Start a new payment to add funds.", GREY,
                [button("Start over", "tk:fund:methods", discord.ButtonStyle.primary), close_button()],
            ))
        now = int(discord.utils.utcnow().timestamp())
        return await inter.edit_original_response(
            view=funds_invoice_view(payment, f"⏳ **Not received yet** (checked <t:{now}:R>). "
                                             "If you just paid, give it a minute and check again.")
        )


async def handle_funds(inter: discord.Interaction):
    guild = inter.guild
    if guild is None:
        return await inter.response.send_message(view=notice("Use this in a server."), ephemeral=True)

    existing = db.open_ticket_for(inter.user.id, store.FUNDS)
    if existing:
        if guild.get_channel(int(existing["channel_id"])):
            return await inter.response.send_message(
                view=notice("You already have an open Add Funds ticket", f"<#{existing['channel_id']}>"), ephemeral=True
            )
        db.update_ticket(int(existing["channel_id"]), state=store.CLOSED)

    await inter.response.defer(ephemeral=True, thinking=True)
    channel = await create_ticket_channel(inter, "funds", "funds_category_id", "ticket_category_id")
    if channel is None:
        return
    db.create_ticket(channel.id, guild.id, inter.user.id, kind=store.FUNDS)
    await channel.send(view=funds_methods_view(inter.user.id, welcome=True),
                       allowed_mentions=discord.AllowedMentions(users=True))
    await inter.followup.send(view=notice("✅ Ticket opened", f"Add funds here: {channel.mention}", GREEN), ephemeral=True)


async def handle_wallet(inter: discord.Interaction):
    summary = db.wallet_summary(inter.user.id)
    await inter.response.send_message(
        view=card(
            "### 👛 My Wallet",
            f"**Balance:** {money(db.balance(inter.user.id))}",
            SEP,
            kv(
                ("Total topped up", money(summary["topped_up"])),
                ("Total spent on rides", money(summary["spent"])),
                ("Rides taken", str(summary["rides"])),
            ),
        ),
        ephemeral=True,
    )


async def handle_book(inter: discord.Interaction):
    guild = inter.guild
    if guild is None:
        return await inter.response.send_message(view=notice("Use this in a server."), ephemeral=True)

    existing = db.open_ticket_for(inter.user.id)
    if existing:
        if guild.get_channel(int(existing["channel_id"])):
            return await inter.response.send_message(
                view=notice("You already have an open ticket", f"<#{existing['channel_id']}>"), ephemeral=True
            )
        db.update_ticket(int(existing["channel_id"]), state=store.CLOSED)

    balance = db.balance(inter.user.id)
    minimum = db.get_int("min_ticket_credits")
    if balance < minimum:
        return await inter.response.send_message(
            view=card(
                "### ⚠️ Insufficient credits",
                f"You need at least **{money(minimum)}** in your wallet to book a ride.\n\n{db.get('add_funds_message')}",
                SEP,
                kv(("Your balance", money(balance)), ("Required", money(minimum)), ("Add", money(minimum - balance))),
                color=ORANGE,
            ),
            ephemeral=True,
        )

    await inter.response.defer(ephemeral=True, thinking=True)
    await open_ticket_channel(inter, test=False)


TEST_BANNER = "-# 🧪 **TEST MODE** — nothing here is real: no wallet charges, no API orders, simulated driver."


def staff_role(guild: discord.Guild) -> discord.Role | None:
    return guild.get_role(int(db.get("staff_role_id"))) if db.get("staff_role_id") else None


TICKETS_CATEGORY_NAME = "🎫 Tickets"
CATEGORY_CHANNEL_LIMIT = 50


async def tickets_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    """A private "Tickets" category at the bottom of the server, created on first use and saved as the ticket category."""
    for existing in guild.categories:
        if existing.name == TICKETS_CATEGORY_NAME and len(existing.channels) < CATEGORY_CHANNEL_LIMIT:
            db.set("ticket_category_id", existing.id)
            return existing
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True, send_messages=True),
    }
    role = staff_role(guild)
    if role:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True)
    try:
        category = await guild.create_category(
            TICKETS_CATEGORY_NAME, overwrites=overwrites, position=len(guild.categories), reason="Ticket category"
        )
    except discord.HTTPException as e:
        log.warning("Couldn't create the tickets category: %s", e)
        return None
    db.set("ticket_category_id", category.id)
    return category


async def create_ticket_channel(inter: discord.Interaction, prefix: str, *category_keys: str) -> discord.TextChannel | None:
    """Private channel for the user + staff, in the first configured category. Sends an error and returns None on failure."""
    guild = inter.guild
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, embed_links=True),
    }
    role = staff_role(guild)
    if role:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    category = None
    for key in category_keys:
        found = guild.get_channel(int(db.get(key))) if db.get(key) else None
        if isinstance(found, discord.CategoryChannel):
            category = found
            break
    if category is None or len(category.channels) >= CATEGORY_CHANNEL_LIMIT:
        category = await tickets_category(guild)

    name = re.sub(r"[^a-z0-9-]", "", f"{prefix}-{inter.user.name}".lower())[:90] or prefix
    try:
        return await guild.create_text_channel(name, category=category, overwrites=overwrites, reason=f"{prefix} ticket")
    except discord.Forbidden:
        await inter.followup.send(
            view=error_card("I need the **Manage Channels** permission to open tickets.", "Missing permission"),
            ephemeral=True,
        )
        return None


async def open_ticket_channel(inter: discord.Interaction, test: bool):
    """Create the private ticket channel and post the welcome message. The interaction must already be deferred."""
    guild = inter.guild
    channel = await create_ticket_channel(inter, "test-ride" if test else "ride", "ticket_category_id")
    if channel is None:
        return

    db.create_ticket(channel.id, guild.id, inter.user.id, is_test=test)

    await channel.send(
        view=card(
            f"### 🎫 Ride Ticket{' (Test)' if test else ''}\n"
            f"Welcome {inter.user.mention}! Click **Enter Pickup** to start your booking.",
            TEST_BANNER if test else None,
            SEP,
            "`01` Enter and confirm your **pickup**\n"
            "`02` Enter and confirm your **drop-off**\n"
            "`03` Pick your ride\n\n"
            f"**Wallet balance:** {money(db.balance(inter.user.id))}",
            [button("Enter Pickup", "tk:details", discord.ButtonStyle.primary, emoji="📍"), close_button()],
            f"-# This ticket closes automatically after {timeout_minutes()} min of inactivity.",
            color=GOLD if test else BLURPLE,
        ),
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    title = "🧪 Test ticket opened" if test else "✅ Ticket opened"
    await inter.followup.send(view=notice(title, f"Your ticket is ready: {channel.mention}", GREEN), ephemeral=True)


# ---------- trip details & quote ----------

STOP_LABEL = {"pickup": "Pickup", "dropoff": "Drop-off"}
STOP_EMOJI = {"pickup": "📍", "dropoff": "🏁"}


class AddressModal(ui.Modal):
    def __init__(self, kind: str):
        super().__init__(title=f"{STOP_LABEL[kind]} address")
        self.kind = kind
        self.address = ui.TextInput(
            label=f"{STOP_LABEL[kind]} address or place", min_length=3, max_length=200,
            placeholder="350 5th Ave, New York",
        )
        self.add_item(self.address)

    async def on_submit(self, inter: discord.Interaction):
        ticket = db.get_ticket(inter.channel_id)
        if not ticket or ticket["state"] in (store.CLOSED, store.RIDE_ACTIVE):
            return await inter.response.send_message(view=notice("This ticket can't take a new booking."), ephemeral=True)
        await inter.response.defer(thinking=True)

        trip = db.ticket_trip(ticket) if self.kind == "dropoff" else {}
        bias = trip.get("pickup")
        retry = [button("Try Again", f"tk:enter:{self.kind}", discord.ButtonStyle.primary, emoji="🔁"), close_button()]
        try:
            options = await search_address(self.address.value, bias)
        except Exception as e:
            db.touch_ticket(inter.channel_id)
            return await inter.followup.send(view=error_card(e, "Address search failed", retry))
        if not options:
            db.touch_ticket(inter.channel_id)
            return await inter.followup.send(view=notice(
                f"❌ No results for “{self.address.value}”", "Try a more specific address.", RED, retry
            ))

        trip[f"{self.kind}_options"] = options
        trip[f"{self.kind}_choice"] = 0
        trip.pop(self.kind, None)
        db.update_ticket(inter.channel_id, state=store.OPEN, trip=trip, quote=None)
        await inter.followup.send(view=confirm_stop_view(self.kind, trip))


async def search_address(query: str, bias: dict | None) -> list[dict]:
    result = await bot.api.geo_search(query, (bias["lat"], bias["lon"]) if bias else None)
    options: list[dict] = []
    best = result.get("best")
    if best and best.get("lat") is not None:
        options.append({
            "title": best.get("title") or best.get("name") or query,
            "subtitle": best.get("subtitle") or "",
            "lat": best["lat"], "lon": best["lon"],
            "place_id": best.get("place_id"), "map_url": best.get("map_url"),
        })
    seen = {o["place_id"] for o in options if o.get("place_id")}
    for c in result.get("candidates") or []:
        if c.get("place_id") and c["place_id"] in seen:
            continue
        options.append({
            "title": c.get("title") or c.get("description") or "Unknown place",
            "subtitle": c.get("subtitle") or "",
            "lat": c.get("lat"), "lon": c.get("lon"),
            "place_id": c.get("place_id"), "map_url": None,
        })
        if len(options) >= 10:
            break
    return options


def option_line(option: dict) -> str:
    title = f"[{option['title']}]({option['map_url']})" if option.get("map_url") else f"**{option['title']}**"
    return f"{title}\n{option['subtitle']}" if option.get("subtitle") else title


def confirm_stop_view(kind: str, trip: dict) -> ui.LayoutView:
    options = trip[f"{kind}_options"]
    choice = trip[f"{kind}_choice"]
    blocks: list = [
        f"### {STOP_EMOJI[kind]} Confirm {STOP_LABEL[kind].lower()}",
        option_line(options[choice]),
    ]
    if kind == "dropoff" and trip.get("pickup"):
        blocks.insert(1, f"-# Pickup: {trip['pickup']['name']}")
    if len(options) > 1:
        select = ui.Select(
            custom_id=f"tk:choose:{kind}",
            placeholder="Not right? Choose another match",
            options=[
                discord.SelectOption(
                    label=o["title"][:100], description=(o.get("subtitle") or None) and o["subtitle"][:100],
                    value=str(i), default=i == choice,
                )
                for i, o in enumerate(options[:25])
            ],
        )
        blocks += [SEP, ui.ActionRow(select)]
    blocks.append([
        button(f"Confirm {STOP_LABEL[kind]}", f"tk:confirm:{kind}", discord.ButtonStyle.success, emoji="✅"),
        button("Change", f"tk:enter:{kind}", emoji="✏️"),
        close_button(),
    ])
    return card(*blocks, color=BLURPLE)


def confirmed_stop_view(kind: str, location: dict) -> ui.LayoutView:
    return card(f"### ✅ {STOP_LABEL[kind]} confirmed", location_line(location), color=GREEN)


async def resolve_option(option: dict) -> dict:
    if option.get("lat") is None:
        loc = await bot.api.geo_resolve(option["place_id"], option.get("title"), option.get("subtitle"))
        option = {**option, "lat": loc["lat"], "lon": loc["lon"], "map_url": loc.get("map_url") or option.get("map_url")}
    name = ", ".join(x for x in (option.get("title"), option.get("subtitle")) if x)
    return {"lat": option["lat"], "lon": option["lon"], "name": name, "place_id": option.get("place_id"),
            "map_url": option.get("map_url")}


async def handle_choose(inter: discord.Interaction, ticket, kind: str):
    trip = db.ticket_trip(ticket)
    if f"{kind}_options" not in trip:
        return await inter.response.send_modal(AddressModal(kind))
    values = (inter.data or {}).get("values") or ["0"]
    trip[f"{kind}_choice"] = min(int(values[0]), len(trip[f"{kind}_options"]) - 1)
    db.update_ticket(inter.channel_id, trip=trip)
    await inter.response.edit_message(view=confirm_stop_view(kind, trip))


async def handle_confirm(inter: discord.Interaction, ticket, kind: str):
    trip = db.ticket_trip(ticket)
    if f"{kind}_options" not in trip:
        return await inter.response.send_modal(AddressModal(kind))
    option = trip[f"{kind}_options"][trip[f"{kind}_choice"]]

    if kind == "pickup" and option.get("lat") is not None:
        # Coordinates already known: confirm and jump straight to the drop-off form.
        trip["pickup"] = await resolve_option(option)
        db.update_ticket(inter.channel_id, trip=trip)
        await inter.response.send_modal(AddressModal("dropoff"))
        try:
            await inter.message.edit(view=confirmed_stop_view("pickup", trip["pickup"]))
        except discord.HTTPException:
            pass
        return

    await inter.response.defer()
    try:
        location = await resolve_option(option)
    except Exception as e:
        return await inter.followup.send(view=error_card(e, "Couldn't look up that place"), ephemeral=True)
    trip[kind] = location
    db.update_ticket(inter.channel_id, trip=trip)
    await inter.edit_original_response(view=confirmed_stop_view(kind, location))

    if kind == "pickup":
        await inter.followup.send(view=card(
            "### 🏁 Where are you going?",
            [button("Enter Drop-off", "tk:enter:dropoff", discord.ButtonStyle.primary, emoji="🏁"), close_button()],
        ))
        return

    if not trip.get("pickup"):
        return await inter.followup.send(view=notice(
            "Pickup missing", "Please enter your pickup again.", ORANGE,
            [button("Enter Pickup", "tk:enter:pickup", discord.ButtonStyle.primary, emoji="📍")],
        ))
    try:
        quote = await get_quote(ticket, trip["pickup"], trip["dropoff"])
    except Exception as e:
        retry = [button("Try Again", "tk:requote", discord.ButtonStyle.primary, emoji="🔁"),
                 button("Change Trip", "tk:details", emoji="✏️"), close_button()]
        return await inter.followup.send(view=error_card(e, "Couldn't get a price", retry))
    db.update_ticket(inter.channel_id, state=store.QUOTED, quote=quote)
    await inter.followup.send(view=quote_view(quote, db.balance(ticket["user_id"])))


async def handle_requote(inter: discord.Interaction, ticket):
    trip = db.ticket_trip(ticket)
    if not (trip.get("pickup") and trip.get("dropoff")):
        return await inter.response.send_modal(AddressModal("pickup"))
    await inter.response.defer(thinking=True)
    try:
        quote = await get_quote(ticket, trip["pickup"], trip["dropoff"])
    except Exception as e:
        db.touch_ticket(inter.channel_id)
        retry = [button("Try Again", "tk:requote", discord.ButtonStyle.primary, emoji="🔁"),
                 button("Change Trip", "tk:details", emoji="✏️"), close_button()]
        return await inter.followup.send(view=error_card(e, "Couldn't get a price", retry))
    db.update_ticket(inter.channel_id, state=store.QUOTED, quote=quote)
    await inter.followup.send(view=quote_view(quote, db.balance(ticket["user_id"])))


class UserError(Exception):
    """An expected problem shown to the user as-is, without logging a traceback."""


async def get_quote(ticket, pickup: dict, dropoff: dict) -> dict:
    return fake_quote(pickup, dropoff) if ticket["is_test"] else await fetch_quote(pickup, dropoff)


def fake_quote(pickup: dict, dropoff: dict) -> dict:
    """Plausible prices from straight-line distance, with the real multiplier and service fee applied."""
    lat1, lon1, lat2, lon2 = map(math.radians, (pickup["lat"], pickup["lon"], dropoff["lat"], dropoff["lon"]))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    km = 2 * 6371 * math.asin(math.sqrt(a)) * 1.3
    base = 250 + km * 150
    multiplier = db.get_float("price_multiplier")
    service_fee = db.get_int("service_fee")
    tiers = []
    for tier, label, factor, seats, eta in (
        ("standard", "Standard", 1.0, 4, 4),
        ("standard_xl", "Standard XL", 1.4, 6, 7),
        ("premium", "Premium", 1.8, 4, 6),
        ("premium_xl", "Premium XL", 2.3, 6, 9),
    ):
        full = math.ceil(base * factor)
        tiers.append({
            "tier": tier, "label": label, "cost": math.ceil(full * multiplier) + service_fee, "full": full,
            "eta": eta, "capacity": seats, "available": True,
        })
    return {
        "slot_id": "test",
        "origin": pickup,
        "destination": dropoff,
        "origin_label": location_line(pickup),
        "destination_label": location_line(dropoff),
        "route_url": None,
        "tiers": tiers,
        "selected": None,
    }


TEST_DRIVER = {
    "agent_name": "Marcus T.",
    "agent_contact": "+1 (555) 010-4821",
    "unit_color": "Silver",
    "unit_name": "Toyota Camry",
    "unit_plate": "TST-4821",
    "agent_photo_url": "https://api.dicebear.com/9.x/avataaars/png?seed=Marcus&size=256",
}
TEST_STEPS = ["assigned", "arriving", "arrived", "in_progress", "completed"]


def test_driver_details() -> str:
    vehicle = f"{TEST_DRIVER['unit_color']} {TEST_DRIVER['unit_name']}"
    return order_details(None, TEST_DRIVER["agent_name"], vehicle, TEST_DRIVER["unit_plate"], TEST_DRIVER["agent_contact"])


async def book_test(inter: discord.Interaction, ticket, quote: dict, tier: dict):
    channel_id = inter.channel_id
    user_id = int(ticket["user_id"])
    cost = tier["cost"]
    before = db.balance(user_id)
    after = before - cost
    bot.test_rides[channel_id] = {"tier": tier["label"], "cost": cost, "step": 0, "pickup": quote["origin"]}
    db.update_ticket(channel_id, state=store.RIDE_ACTIVE, quote=None)

    await inter.followup.send(view=card(
        "### ✅ Ride booked",
        TEST_BANNER,
        SEP,
        kv(
            ("From", quote["origin_label"]),
            ("To", quote["destination_label"]),
            ("Ride", tier["label"]),
            ("Charged", f"{money(cost)} *(not charged — test)*"),
            ("New balance", f"{money(after)} *(would be)*"),
        ),
        SEP,
        with_thumbnail(
            f"**Ride status:** {STATE_EMOJI['assigned']} Assigned\n{test_driver_details()}",
            TEST_DRIVER["agent_photo_url"],
        ),
        ride_buttons(),
        color=GREEN,
    ))
    await post_success(user_id, tier["label"], cost, before, after, test=True)


async def handle_status_test(inter: discord.Interaction):
    ride = bot.test_rides.get(inter.channel_id)
    if not ride:
        db.update_ticket(inter.channel_id, state=store.RIDE_ENDED)
        return await inter.response.edit_message(view=notice(
            "Test ride expired", "The bot restarted, so the simulated ride was lost.",
            GREY, [close_button()],
        ))
    ride["step"] = min(ride["step"] + 1, len(TEST_STEPS) - 1)
    state = TEST_STEPS[ride["step"]]
    ended = state == "completed"
    db.touch_ticket(inter.channel_id)

    details = f"{test_driver_details()}\n**Fare paid:** {money(ride['cost'])} *(test)*"

    if ended:
        bot.test_rides.pop(inter.channel_id, None)
        db.update_ticket(inter.channel_id, state=store.RIDE_ENDED)

    now = int(discord.utils.utcnow().timestamp())
    step_note = f"Step {ride['step'] + 1}/{len(TEST_STEPS)} — click **Check Status** again to advance."
    await inter.response.edit_message(view=card(
        f"### {STATE_EMOJI.get(state, '')} {state.replace('_', ' ').title()}",
        TEST_BANNER,
        SEP,
        with_thumbnail(details, TEST_DRIVER["agent_photo_url"]),
        [close_button()] if ended else ride_buttons(),
        f"-# Updated <t:{now}:R> · " + ("ride completed" if ended else step_note),
        color=GREY if ended else GREEN,
    ))


async def fetch_quote(pickup, dropoff, exclude: set[str] = frozenset()) -> dict:
    busy = db.busy_slots() | set(exclude)
    slots = [s for s in await bot.api.list_slots() if s["slot_id"] not in busy]
    if not slots:
        raise UserError("No drivers are available right now. Please try again in a few minutes.")

    last_error: Exception | None = None
    for slot in slots[:3]:
        try:
            q = await bot.api.quote(operator_member(), slot["slot_id"], pickup, dropoff)
            break
        except ApiError as e:
            if e.code not in SLOT_FAIL_CODES:
                raise
            last_error = e
    else:
        raise last_error or UserError("No drivers are available right now.")

    multiplier = db.get_float("price_multiplier")
    service_fee = db.get_int("service_fee")
    tiers = []
    for t in q.get("tiers", []):
        total = t.get("total_cents")
        cost = math.ceil(total * multiplier) + service_fee if total is not None else None
        before = t.get("before_credit_cents")
        tiers.append({
            "tier": t["tier"],
            "label": t.get("label") or t["tier"].replace("_", " ").title(),
            "cost": cost,
            "base": math.ceil(total) if total is not None else None,
            "full": math.ceil(before) if before is not None else (math.ceil(total) if total is not None else None),
            "eta": t.get("eta_minutes"),
            "capacity": t.get("capacity"),
            "available": bool(t.get("available", True)) and cost is not None,
        })
    return {
        "slot_id": q.get("slot_id") or slot["slot_id"],
        "origin": location_input(q["origin"]),
        "destination": location_input(q["destination"]),
        "origin_label": location_line(q["origin"]),
        "destination_label": location_line(q["destination"]),
        "route_url": q.get("route_url"),
        "tiers": tiers,
        "selected": None,
    }


def quote_view(quote: dict, balance: int) -> ui.LayoutView:
    options = []
    buttons = []
    for t in quote["tiers"]:
        if t["available"]:
            details = [f"**{money(t['cost'])}**"]
            if t.get("eta") is not None:
                details.append(f"ETA {t['eta']:.0f} min")
            if t.get("capacity"):
                details.append(f"{t['capacity']} seats")
            if t["cost"] > balance:
                details.append("⚠️ not enough credits")
            options.append(f"**{t['label']}** — " + " · ".join(details))
        else:
            options.append(f"~~{t['label']}~~ — unavailable")
        buttons.append(button(
            f"{t['label']} · {money(t['cost'])}"[:80], f"tk:tier:{t['tier']}",
            discord.ButtonStyle.primary, disabled=not t["available"],
        ))
    route = f"\n[View route]({quote['route_url']})" if quote.get("route_url") else ""
    return card(
        "### 🚘 Choose your ride",
        TEST_BANNER if quote.get("slot_id") == "test" else None,
        f"**From:** {quote['origin_label']}\n**To:** {quote['destination_label']}{route}",
        SEP,
        "\n".join(options) or "No ride options returned.",
        buttons,
        [button("Change Trip", "tk:details", emoji="✏️"), close_button()],
        f"-# Prices include the {money(db.get_int('service_fee'))} service fee · Wallet balance: {money(balance)} · "
        "The fare is deducted when you pick a ride.",
    )


# ---------- operator card pool ----------

# Order errors meaning "this card can't pay" - the bot rotates to the next operator card.
CARD_FAIL_CODES = {"card_invalid", "card_declined", "no_card", "card_not_found", "card_unavailable"}


# ---------- booking ----------

def retry_buttons() -> list[ui.Button]:
    return [button("Try Again", "tk:retry", discord.ButtonStyle.primary, emoji="🔁"), close_button()]


def ride_buttons() -> list[ui.Button]:
    return [
        button("Check Status", "tk:status", discord.ButtonStyle.primary, emoji="🔄"),
        button("Cancel Ride", "tk:cancel", discord.ButtonStyle.danger, emoji="✖️"),
    ]


def auto_close_note() -> str:
    return f"-# This ticket closes automatically after {timeout_minutes()} min of inactivity."


def insufficient_view(cost: int, balance: int, test: bool = False) -> ui.LayoutView:
    return card(
        "### ⚠️ Insufficient credits. Please add funds.",
        TEST_BANNER if test else None,
        db.get("add_funds_message"),
        SEP,
        kv(("Ride cost", money(cost)), ("Your balance", money(balance)), ("Missing", money(cost - balance))),
        [button("Add Funds", "tk:funds", discord.ButtonStyle.success, emoji="💵"), *retry_buttons()],
        auto_close_note(),
        color=ORANGE,
    )


def order_details(state=None, driver=None, vehicle=None, plate=None, contact=None) -> str:
    lines = []
    if state:
        lines.append(f"**Ride status:** {STATE_EMOJI.get(state, '')} {state.replace('_', ' ').title()}")
    if driver:
        lines.append(f"**Driver:** {driver}")
    if contact:
        lines.append(f"**Driver contact:** {contact}")
    if vehicle:
        lines.append(f"**Vehicle:** {vehicle}")
    if plate:
        lines.append(f"**Plate number:** `{plate}`")
    return "\n".join(lines)


async def place_order(member: str, quote: dict, tier_name: str, card_id: str | None, channel_id: int) -> dict:
    """Create the order and wait for it. A sync API error comes back as a failed task."""
    try:
        task = await bot.api.create_order(
            member_id=member,
            slot_id=quote["slot_id"],
            origin=quote["origin"],
            destination=quote["destination"],
            tier=tier_name,
            card_id=card_id,
            idempotency_key=f"{channel_id}-{uuid.uuid4().hex}",
        )
        if task.get("status") not in ("succeeded", "failed"):
            task = await bot.api.wait_for_task(task["id"], timeout=300)
        return task
    except ApiError as e:
        return {"status": "failed", "error": {"code": e.code, "message": e.message, "details": e.details}}


def card_failure(task: dict) -> str | None:
    """Card error code when the order failed because the card couldn't pay, else None."""
    if task.get("status") == "failed":
        code = (task.get("error") or {}).get("code")
        return code if code in CARD_FAIL_CODES else None
    result = task.get("result") or {}
    if task.get("status") == "succeeded" and result.get("status") == "payment_required":
        return result.get("payment_code") or "card_declined"
    return None


# Order errors meaning "this ride account can't be used any more" (e.g. another server just booked it).
SLOT_FAIL_CODES = {"slot_taken", "slot_unavailable", "slot_not_found", "slot_not_ready"}
MAX_SLOT_SWITCHES = 2


def slot_failure(task: dict) -> str | None:
    if task.get("status") == "failed":
        code = (task.get("error") or {}).get("code")
        return code if code in SLOT_FAIL_CODES else None
    return None


async def requote_on_free_slot(quote: dict, tier_name: str) -> dict | None:
    """Same trip priced on another free slot, or None when there's none."""
    try:
        return await fetch_quote(quote["origin"], quote["destination"], exclude={quote["slot_id"]})
    except Exception as e:
        log.warning("Re-quote on another slot failed: %s", e)
        return None


async def book(inter: discord.Interaction, ticket, quote: dict, tier_name: str):
    """Charge the wallet, then order with the owner's card pool: the bot picks the card and rotates on a decline.
    Customers never see or choose cards."""
    channel_id = inter.channel_id
    user_id = int(ticket["user_id"])
    tier = next((t for t in quote["tiers"] if t["tier"] == tier_name), None)
    if not tier or not tier["available"]:
        return await inter.response.send_message(
            view=notice("That ride option isn't available", "Enter your trip again."), ephemeral=True
        )

    lock = bot.locks[channel_id]
    if lock.locked():
        return await inter.response.send_message(view=notice("A booking is already in progress."), ephemeral=True)

    async with lock:
        await inter.response.defer(thinking=True)
        if inter.message:
            try:
                await inter.message.delete()
            except discord.HTTPException:
                pass

        quote["selected"] = tier_name
        db.update_ticket(channel_id, quote=quote)
        if ticket["is_test"]:
            balance = db.balance(user_id)
            if balance < tier["cost"]:
                return await inter.followup.send(view=insufficient_view(tier["cost"], balance, test=True))
            return await book_test(inter, ticket, quote, tier)

        cost = tier["cost"]
        try:
            balance_before, balance_after = db.adjust(
                user_id, -cost, f"ride booking ({tier['label']})", require_funds=True
            )
        except store.InsufficientCredits as e:
            return await inter.followup.send(view=insufficient_view(cost, e.balance))

        member = operator_member()
        task: dict = {}
        tried_card: str | None = None
        declined: list[str] = []
        slot_switches = 0
        try:
            cards = await operator_card_order()
            while cards:
                tried_card = cards[0]
                if tried_card:
                    _cards_in_flight.add(tried_card)
                try:
                    task = await place_order(member, quote, tier_name, tried_card, channel_id)
                finally:
                    _cards_in_flight.discard(tried_card)
                code = card_failure(task)
                if code:
                    cards.pop(0)
                    declined.append(f"{tried_card or 'default'} ({code})")
                    log.warning("Operator card %s failed (%s) — removing it and trying the next card", tried_card, code)
                    if tried_card and code != "card_not_found":
                        await delete_used_card(tried_card, f"failed: {code}")
                    continue
                if slot_failure(task) and slot_switches < MAX_SLOT_SWITCHES:
                    slot_switches += 1
                    new_quote = await requote_on_free_slot(quote, tier_name)
                    new_tier = next((t for t in (new_quote or {}).get("tiers", []) if t["tier"] == tier_name), None)
                    if new_quote:
                        new_quote["selected"] = tier_name
                        db.update_ticket(channel_id, quote=new_quote)
                    if new_tier and new_tier["available"] and new_tier["cost"] <= cost:
                        log.info("Slot %s was taken — continuing on slot %s", quote["slot_id"], new_quote["slot_id"])
                        quote = new_quote
                        tier = {**new_tier, "cost": cost}
                        continue
                    db.adjust(user_id, cost, "refund: ride slot taken")
                    if new_quote:
                        await inter.followup.send(view=notice(
                            "🔄 Prices were updated",
                            "That ride just got taken — your credits were refunded. Pick a ride again below.", ORANGE))
                        return await inter.followup.send(view=quote_view(new_quote, db.balance(user_id)))
                    return await inter.followup.send(view=card_view_failed(
                        "No drivers are available right now. Please try again in a few minutes."))
                break
        except Exception as e:
            db.adjust(user_id, cost, "refund: booking failed")
            return await inter.followup.send(view=error_card(e, "Booking failed — you were refunded", retry_buttons()))

        if not task or card_failure(task):
            db.adjust(user_id, cost, "refund: booking failed (payment)")
            log.error("No operator card could pay for channel %s: %s", channel_id,
                      ", ".join(declined) or "all cards busy with other bookings")
            asyncio.create_task(alert_staff_cards_failed(declined))
            return await inter.followup.send(view=card_view_failed(
                "We couldn't complete the booking right now. Staff have been notified — please try again shortly."
            ))

        status = task.get("status")
        result = task.get("result") or {}
        pending = status not in ("succeeded", "failed")
        confirmed = status == "succeeded" and result.get("status") == "confirmed"

        if not (confirmed or pending):
            db.adjust(user_id, cost, "refund: booking not confirmed")
            if status == "failed":
                err = task.get("error") or {}
                reason = f"`{err.get('code', 'unknown')}` {err.get('message', '')}"
            else:
                reason = result.get("message") or f"Order status: {result.get('status')}"
            return await inter.followup.send(view=card_view_failed(reason))

        used_card_id = result.get("card_id") or tried_card or ""
        order = result.get("order") or {}
        db.create_ride(
            channel_id=str(channel_id),
            user_id=str(user_id),
            slot_id=quote["slot_id"],
            tier=tier_name,
            cost_cents=cost,
            full_price_cents=tier.get("full"),
            base_cents=tier.get("base"),
            member_id=member,
            card_id=used_card_id,
            card_last4=result.get("card_last4") or "",
            balance_before=balance_before,
            balance_after=balance_after,
            origin=quote["origin"],
            destination=quote["destination"],
            order_id=order.get("order_id"),
            status="active",
        )
        db.update_ticket(channel_id, state=store.RIDE_ACTIVE, quote=None)
        asyncio.create_task(refresh_panels())
        if confirmed:
            asyncio.create_task(delete_used_card(used_card_id))

        title = "### ✅ Ride booked" if confirmed else "### ⏳ Ride is being booked\nThe booking is still processing. Use **Check Status** for updates."
        trip = kv(
            ("From", quote["origin_label"]),
            ("To", quote["destination_label"]),
            ("Ride", tier["label"]),
            ("Charged", money(cost)),
            ("New balance", money(balance_after)),
        )
        details = order_details(order.get("state"), order.get("agent_name"), order.get("unit"), order.get("unit_plate"))
        await inter.followup.send(view=card(
            title,
            SEP,
            trip,
            SEP if details else None,
            with_thumbnail(details, order.get("agent_photo_url")) if details else None,
            ride_buttons(),
            color=GREEN if confirmed else GOLD,
        ))
        await post_success(user_id, tier["label"], cost, balance_before, balance_after)
        asyncio.create_task(refresh_panels())


_last_owner_alert: dict[str, float] = {}


async def alert_owners(kind: str, title: str, body: str) -> None:
    """DM each server owner about the card pool (at most once per 10 min per kind). Never shown to customers."""
    now = asyncio.get_running_loop().time()
    if now - _last_owner_alert.get(kind, -1e9) < 600:
        return
    _last_owner_alert[kind] = now
    for guild in bot.guilds:
        try:
            owner = guild.owner or await bot.fetch_user(guild.owner_id)
            await owner.send(view=notice(title, f"{body}\n-# Server: {guild.name}", ORANGE))
        except discord.HTTPException as e:
            log.warning("Couldn't DM the owner of %s: %s", guild.name, e)


async def alert_staff_cards_failed(declined: list[str]) -> None:
    if not declined:
        return await alert_owners(
            "cards_busy", "⚠️ All cards are busy",
            "A customer's booking failed because every card in your pool was already in use by other "
            "bookings — they were refunded.\n\nAdd more cards with **/dashboard → Add Card**.",
        )
    await alert_owners(
        "cards_failed",
        "⚠️ No card in your pool could pay",
        "A customer's booking failed because no card in your pool could pay — they were refunded.\n"
        f"Failed cards were removed from the pool: {', '.join(f'`{d}`' for d in declined)}\n\n"
        "Add fresh cards with **/dashboard → Add Card**.",
    )


def card_view_failed(reason: str) -> ui.LayoutView:
    return card("### ❌ Booking failed — your credits were refunded", reason, retry_buttons(), auto_close_note(),
                color=RED)


def success_embed(user_id: int, tier_label: str, cost: int, before: int, after: int, test: bool = False) -> discord.Embed:
    embed = discord.Embed(title="✅ Ride Booked", color=GOLD if test else GREEN)
    embed.add_field(name="👤 Customer", value=f"<@{user_id}>")
    embed.add_field(name="🚗 Ride", value=f"✅ **{tier_label}**")
    embed.add_field(name="💵 Total", value=f"**{money(cost)}**")
    embed.add_field(name="👛 Balance", value=f"{money(before)} → **{money(after)}**", inline=False)
    footer = "🧪 Test message — no ride was booked and no credits were used" if test else "1 ride"
    embed.set_footer(text=footer)
    return embed


async def post_success(user_id: int, tier_label: str, cost: int, before: int, after: int, test: bool = False) -> bool:
    channel_id = db.get("success_channel_id")
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if not isinstance(channel, discord.abc.Messageable):
        return False
    try:
        await channel.send(
            embed=success_embed(user_id, tier_label, cost, before, after, test),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        return True
    except discord.HTTPException as e:
        log.warning("Could not post to success channel: %s", e)
        return False


# ---------- active ride ----------

async def handle_status(inter: discord.Interaction, ticket):
    if ticket["is_test"]:
        return await handle_status_test(inter)
    ride = db.active_ride(inter.channel_id)
    if not ride:
        return await inter.response.send_message(view=notice("There's no active ride in this ticket."), ephemeral=True)
    await inter.response.defer()
    db.touch_ticket(inter.channel_id)
    try:
        r = await bot.api.order_status(ride["member_id"] or operator_member(), ride["slot_id"])
    except Exception as e:
        return await inter.followup.send(view=error_card(e), ephemeral=True)

    state = r.get("state", "other")
    ended = state in ("completed", "cancelled")
    vehicle = " ".join(x for x in (r.get("unit_color"), r.get("unit_name")) if x)
    details = order_details(None, r.get("agent_name"), vehicle, r.get("unit_plate"), r.get("agent_contact"))
    details = "\n".join(x for x in (details, f"**Fare paid:** {money(ride['cost_cents'])}") if x)

    note = None
    if ended:
        refund = ride["cost_cents"] if state == "cancelled" else 0
        if refund:
            db.adjust(ride["user_id"], refund, "refund: ride cancelled by provider")
            note = f"The ride was cancelled by the provider. **{money(refund)}** was refunded to your wallet."
        db.update_ride(ride["id"], status=state, refund_cents=refund, ended_at=int(discord.utils.utcnow().timestamp()))
        db.update_ticket(inter.channel_id, state=store.RIDE_ENDED)
        if state == "completed":
            asyncio.create_task(delete_used_card(ride["card_id"]))
        asyncio.create_task(refresh_panels())

    now = int(discord.utils.utcnow().timestamp())
    view = card(
        f"### {STATE_EMOJI.get(state, '')} {state.replace('_', ' ').title()}",
        note,
        SEP,
        with_thumbnail(details, r.get("agent_photo_url")),
        [close_button()] if ended else ride_buttons(),
        f"-# Updated <t:{now}:R>" + (f" · ride ended, ticket closes after {timeout_minutes()} min of inactivity" if ended else ""),
        color=GREY if ended else GREEN,
    )
    await inter.edit_original_response(view=view)


def ride_cost(ticket, channel_id: int) -> int | None:
    if ticket["is_test"]:
        ride = bot.test_rides.get(channel_id)
        return ride["cost"] if ride else None
    ride = db.active_ride(channel_id)
    return ride["cost_cents"] if ride else None


async def handle_cancel(inter: discord.Interaction, ticket):
    cost = ride_cost(ticket, inter.channel_id)
    if cost is None:
        return await inter.response.send_message(view=notice("There's no active ride in this ticket."), ephemeral=True)
    fee = min(db.get_int("cancel_fee"), cost)
    await inter.response.send_message(
        view=notice(
            "Cancel this ride?" + (" (test)" if ticket["is_test"] else ""),
            f"You'll be refunded **{money(cost - fee)}** (cancellation fee: {money(fee)}).",
            ORANGE,
            [button("Yes, cancel ride", "tk:cancel_yes", discord.ButtonStyle.danger), button("Keep ride", "tk:cancel_no")],
        ),
        ephemeral=True,
    )


async def handle_cancel_confirm(inter: discord.Interaction, ticket):
    if ticket["is_test"]:
        ride = bot.test_rides.pop(inter.channel_id, None)
        if not ride:
            return await inter.response.edit_message(view=notice("There's no active ride anymore."))
        fee = min(db.get_int("cancel_fee"), ride["cost"])
        db.update_ticket(inter.channel_id, state=store.RIDE_ENDED)
        await inter.response.edit_message(view=notice("Ride cancelled.", color=RED))
        return await inter.channel.send(view=card(
            "### ❌ Ride cancelled",
            TEST_BANNER,
            SEP,
            kv(
                ("Refunded", f"{money(ride['cost'] - fee)} *(would be)*"),
                ("Cancellation fee", money(fee)),
                ("New balance", f"{money(db.balance(ticket['user_id']))} *(unchanged — test)*"),
            ),
            [close_button()],
            auto_close_note(),
            color=RED,
        ))

    ride = db.active_ride(inter.channel_id)
    if not ride:
        return await inter.response.edit_message(view=notice("There's no active ride anymore."))

    async with bot.locks[inter.channel_id]:
        await inter.response.edit_message(view=notice("⏳ Cancelling…", color=GOLD))
        try:
            result = await bot.api.cancel_order(ride["member_id"] or operator_member(), ride["slot_id"])
        except Exception as e:
            return await inter.edit_original_response(view=error_card(e, "Couldn't cancel the ride"))
        if not result.get("cancelled"):
            return await inter.edit_original_response(view=notice(
                "Couldn't cancel the ride", "It may have already ended. Use **Check Status**.", ORANGE
            ))

        fee = min(db.get_int("cancel_fee"), ride["cost_cents"])
        refund = ride["cost_cents"] - fee
        _, after = db.adjust(ride["user_id"], refund, f"refund: ride cancelled (fee {money(fee)})")
        db.update_ride(ride["id"], status="cancelled", refund_cents=refund, ended_at=int(discord.utils.utcnow().timestamp()))
        db.update_ticket(inter.channel_id, state=store.RIDE_ENDED)
        asyncio.create_task(refresh_panels())

    await inter.edit_original_response(view=notice("Ride cancelled.", color=RED))
    await inter.channel.send(view=card(
        "### ❌ Ride cancelled",
        SEP,
        kv(("Refunded", money(refund)), ("Cancellation fee", money(fee)), ("New balance", money(after))),
        [close_button()],
        auto_close_note(),
        color=RED,
    ))


# ---------- closing ----------

async def close_ticket(channel, channel_id: int, reason: str):
    db.update_ticket(channel_id, state=store.CLOSED, quote=None)
    bot.locks.pop(channel_id, None)
    bot.test_rides.pop(channel_id, None)
    if channel is None:
        return
    try:
        await channel.delete(reason=reason)
    except discord.HTTPException as e:
        log.warning("Could not delete ticket channel %s: %s", channel_id, e)


async def handle_close(inter: discord.Interaction, ticket):
    if ticket["state"] == store.RIDE_ACTIVE and not is_staff(inter.user):
        return await inter.response.send_message(
            view=notice("You have an active ride", "Cancel it first or wait until it's finished.", ORANGE), ephemeral=True
        )
    await inter.response.send_message(view=notice("🔒 Closing this ticket in 5 seconds…", color=GREY))
    db.update_ticket(inter.channel_id, state=store.CLOSED, quote=None)
    await asyncio.sleep(5)
    await close_ticket(inter.channel, inter.channel_id, f"Closed by {inter.user}")


@tasks.loop(seconds=30)
async def auto_close_tickets():
    for t in db.stale_tickets(timeout_minutes() * 60):
        channel = bot.get_channel(int(t["channel_id"]))
        if channel:
            try:
                await channel.send(view=notice("⌛ Closing this ticket due to inactivity.", color=GREY))
                await asyncio.sleep(3)
            except discord.HTTPException:
                pass
        await close_ticket(channel, int(t["channel_id"]), "Inactive ticket")


@auto_close_tickets.before_loop
async def before_auto_close():
    await bot.wait_until_ready()


# ---------- component router ----------

@bot.event
async def on_interaction(inter: discord.Interaction):
    if inter.type != discord.InteractionType.component:
        return
    custom_id = (inter.data or {}).get("custom_id", "")
    if custom_id.startswith("adm:"):
        try:
            return await handle_admin_action(inter, custom_id[4:])
        except Exception as e:
            view = error_card(e)
            if inter.response.is_done():
                return await inter.followup.send(view=view, ephemeral=True)
            return await inter.response.send_message(view=view, ephemeral=True)
    if not custom_id.startswith("tk:"):
        return
    action = custom_id[3:]

    try:
        if action == "book":
            return await handle_book(inter)
        if action == "funds":
            return await handle_funds(inter)
        if action.startswith("fund:"):
            return await handle_fund_action(inter, action)
        if action == "wallet":
            return await handle_wallet(inter)

        ticket = db.get_ticket(inter.channel_id)
        if not ticket or ticket["state"] == store.CLOSED:
            return await inter.response.send_message(view=notice("This ticket is no longer active."), ephemeral=True)
        if str(inter.user.id) != ticket["user_id"] and not is_staff(inter.user):
            return await inter.response.send_message(view=notice("This isn't your ticket."), ephemeral=True)

        if action == "close":
            return await handle_close(inter, ticket)
        booking_actions = ("details", "enter:", "choose:", "confirm:", "requote", "tier:", "retry")
        if action.startswith(booking_actions) and ticket["state"] == store.RIDE_ACTIVE:
            return await inter.response.send_message(view=notice("You already have an active ride."), ephemeral=True)

        if action == "details":
            db.touch_ticket(inter.channel_id)
            return await inter.response.send_modal(AddressModal("pickup"))
        if action.startswith("enter:"):
            kind = action[6:]
            if kind == "dropoff" and not db.ticket_trip(ticket).get("pickup"):
                kind = "pickup"
            db.touch_ticket(inter.channel_id)
            return await inter.response.send_modal(AddressModal(kind))
        if action.startswith("choose:"):
            return await handle_choose(inter, ticket, action[7:])
        if action.startswith("confirm:"):
            return await handle_confirm(inter, ticket, action[8:])
        if action == "requote":
            return await handle_requote(inter, ticket)
        if action.startswith("tier:") or action == "retry":
            quote = db.ticket_quote(ticket)
            tier = action[5:] if action.startswith("tier:") else (quote or {}).get("selected")
            if not quote or not tier:
                db.touch_ticket(inter.channel_id)
                return await inter.response.send_modal(AddressModal("pickup"))
            return await book(inter, ticket, quote, tier)
        if action == "status":
            return await handle_status(inter, ticket)
        if action == "cancel":
            return await handle_cancel(inter, ticket)
        if action == "cancel_yes":
            return await handle_cancel_confirm(inter, ticket)
        if action == "cancel_no":
            return await inter.response.edit_message(view=notice("Your ride was kept.", color=GREEN))
    except Exception as e:
        view = error_card(e)
        if inter.response.is_done():
            await inter.followup.send(view=view, ephemeral=True)
        else:
            await inter.response.send_message(view=view, ephemeral=True)


# ---------- admin commands ----------

admin_perms = app_commands.default_permissions(administrator=True)


async def post_ride_panel(target: discord.abc.Messageable):
    await update_stock()
    msg = await target.send(view=panel_view())
    db.add_panel_message(target.id, msg.id)


async def post_funds_panel(target: discord.abc.Messageable):
    msg = await target.send(view=funds_panel_view())
    db.add_panel_message(target.id, msg.id, key="funds_panel_messages")
    await refresh_panels()


async def post_guide(target: discord.abc.Messageable):
    old = db.get("guide_message")
    if old:
        old_channel_id, old_message_id = (int(x) for x in old.split(":"))
        old_channel = bot.get_channel(old_channel_id)
        if isinstance(old_channel, discord.abc.Messageable):
            try:
                await old_channel.get_partial_message(old_message_id).delete()
            except discord.HTTPException:
                pass
    msg = await target.send(view=guide_view())
    db.set("guide_message", f"{target.id}:{msg.id}")
    await refresh_panels()


PANEL_KEYS = {"ride": "panel_messages", "funds": "funds_panel_messages"}


def panel_channel(kind: str) -> str | None:
    if kind == "guide":
        return guide_channel_id()
    panels = db.panel_messages(PANEL_KEYS[kind])
    return str(panels[0][0]) if panels else None


async def move_panel(kind: str, target: discord.abc.Messageable):
    """Delete the existing panel(s) of this kind and post a fresh one in `target`."""
    if kind in PANEL_KEYS:
        key = PANEL_KEYS[kind]
        for channel_id, message_id in db.panel_messages(key):
            channel = bot.get_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await channel.get_partial_message(message_id).delete()
                except discord.HTTPException:
                    pass
            db.remove_panel_message(message_id, key=key)
    await POSTERS[kind][1](target)


POSTERS = {
    "ride": ("Book Ride panel", post_ride_panel),
    "funds": ("Add Funds panel", post_funds_panel),
    "guide": ("How-to guide", post_guide),
}


# ---------- admin dashboard (/dashboard) ----------

ADMIN_ONLY = "Only server admins can use bot commands."


def dashboard_view() -> ui.LayoutView:
    s = db.get_settings()
    stock = "unknown" if bot.stock is None else (f"{bot.stock} available" if bot.stock else "out of stock")
    providers = ", ".join(pay.PROVIDER_LABELS[p] for p in pay.enabled_providers()) or "none"
    return card(
        f"# 🛠️ {s['brand_name']} — Admin Dashboard",
        SEP,
        kv(
            ("Stock", stock),
            ("Payments", providers),
            ("Rides booked", str(db.stats()["rides"])),
            ("Active rides", str(len(db.busy_slots()))),
        ),
        SEP,
        "**⚙️ Server**",
        [
            button("Config", "adm:config", discord.ButtonStyle.primary, emoji="⚙️"),
            button("Profits", "adm:profits", discord.ButtonStyle.success, emoji="📈"),
            button("Slots", "adm:slots", discord.ButtonStyle.secondary, emoji="🎰"),
            button("Ping API", "adm:ping", discord.ButtonStyle.secondary, emoji="📡"),
        ],
        "**👛 Wallets**",
        [
            button("Add Balance", "adm:wallet:add", discord.ButtonStyle.success, emoji="➕"),
            button("Remove Balance", "adm:wallet:remove", discord.ButtonStyle.danger, emoji="➖"),
            button("Set Balance", "adm:wallet:set", discord.ButtonStyle.primary, emoji="✏️"),
            button("View Balance", "adm:wallet:view", discord.ButtonStyle.secondary, emoji="🔍"),
        ],
        "**💳 Operator card**",
        [
            button("Add Card", "adm:card:add", discord.ButtonStyle.success, emoji="➕"),
            button("List Cards", "adm:card:list", discord.ButtonStyle.secondary, emoji="📋"),
            button("Remove Card", "adm:card:remove", discord.ButtonStyle.danger, emoji="🗑️"),
        ],
        SEP,
        [button("Refresh", "adm:home", emoji="🔄")],
        "-# Admins only · everything here is only visible to you",
        color=BLURPLE,
    )


def profits_view() -> ui.LayoutView:
    p = db.profit_stats()
    return card(
        "### 📈 Profits",
        SEP,
        kv(
            ("Rides booked", f"**{p['rides']}**"),
            ("Cancelled", str(p["cancelled"])),
        ),
        SEP,
        kv(
            ("Customers paid", money(p["revenue"])),
            ("Spent on card", money(p["card_cost"])),
            ("Profit", f"**{money(p['profit'])}**"),
        ),
        SEP,
        kv(("Top-ups received", money(p["topups"]))),
        [button("Refresh", "adm:profits", emoji="🔄"), button("Back", "adm:home", emoji="↩️")],
        "-# Customers paid = ride fares after refunds (cancel fees included). "
        "Spent on card = the ride price charged to the operator card, before your markup and service fee.",
        color=GREEN,
    )


WALLET_ACTIONS = {
    "add": ("➕ Add Balance", "Amount to add ($)"),
    "remove": ("➖ Remove Balance", "Amount to remove ($)"),
    "set": ("✏️ Set Balance", "New balance ($)"),
    "view": ("🔍 View Balance", None),
}


def wallet_pick_view(action: str) -> ui.LayoutView:
    title, _ = WALLET_ACTIONS[action]
    return card(
        f"### {title}",
        "Pick the user:",
        ui.ActionRow(ui.UserSelect(custom_id=f"adm:wuser:{action}", placeholder="Search for a user…")),
        [button("Back", "adm:home", emoji="↩️")],
    )


def wallet_balance_view(user_id: int, title: str = "🔍 Wallet", note: str | None = None) -> ui.LayoutView:
    history = db.recent_transactions(user_id)
    activity = "\n".join(f"`{money(t['delta_cents']):>9}` {t['reason']} · <t:{t['created_at']}:R>" for t in history)
    return card(
        f"### {title}",
        note,
        f"<@{user_id}> · **Balance: {money(db.balance(user_id))}**",
        SEP if history else None,
        f"**Recent activity**\n{activity}" if history else None,
        [
            button("Add", f"adm:wedit:add:{user_id}", discord.ButtonStyle.success, emoji="➕"),
            button("Remove", f"adm:wedit:remove:{user_id}", discord.ButtonStyle.danger, emoji="➖"),
            button("Set", f"adm:wedit:set:{user_id}", discord.ButtonStyle.primary, emoji="✏️"),
            button("Back", "adm:home", emoji="↩️"),
        ],
        color=GREEN if note else BLURPLE,
    )


class WalletModal(ui.Modal):
    def __init__(self, action: str, user_id: int):
        title, label = WALLET_ACTIONS[action]
        super().__init__(title=title[2:].strip())
        self.action, self.user_id = action, user_id
        self.amount = ui.TextInput(label=label, placeholder="10.00", max_length=10)
        self.reason = ui.TextInput(label="Reason (optional)", required=False, max_length=100)
        self.add_item(self.amount)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        if not is_admin(inter.user):
            return await inter.response.send_message(view=notice("⛔ Admins only", ADMIN_ONLY, RED), ephemeral=True)
        cents = parse_amount(self.amount.value)
        if cents is None or cents < 0 or (cents == 0 and self.action != "set"):
            return await inter.response.send_message(
                view=notice("Enter an amount like `10` or `12.50`.", color=ORANGE), ephemeral=True
            )
        reason = self.reason.value.strip()
        try:
            if self.action == "add":
                before, after = db.adjust(self.user_id, cents, reason or f"added by {inter.user}")
            elif self.action == "remove":
                before, after = db.adjust(self.user_id, -cents, reason or f"removed by {inter.user}", require_funds=True)
            else:
                before, after = db.set_balance(self.user_id, cents, reason or f"set by {inter.user}")
        except store.InsufficientCredits as e:
            return await inter.response.send_message(
                view=notice("Not enough balance", f"They only have {money(e.balance)}.", ORANGE), ephemeral=True
            )
        await inter.response.edit_message(
            view=wallet_balance_view(self.user_id, "✅ Wallet updated", f"{money(before)} → **{money(after)}**")
        )


def config_view() -> ui.LayoutView:
    s = db.get_settings()

    def channel_ref(key: str) -> str:
        return f"<#{s[key]}>" if s[key] else "not set"

    def default(value: str | None, kind: discord.SelectDefaultValueType):
        return [discord.SelectDefaultValue(id=int(value), type=kind)] if value and str(value).isdigit() else []

    channel = discord.SelectDefaultValueType.channel
    return card(
        "### ⚙️ Server config",
        SEP,
        "**Channels & roles**",
        ui.ActionRow(ui.ChannelSelect(
            custom_id="adm:sel:success_channel_id", placeholder="Success channel (booking posts)",
            channel_types=[discord.ChannelType.text], default_values=default(s["success_channel_id"], channel),
        )),
        *(
            ui.ActionRow(ui.ChannelSelect(
                custom_id=f"adm:place:{kind}", placeholder=placeholder,
                channel_types=[discord.ChannelType.text], default_values=default(panel_channel(kind), channel),
            ))
            for kind, placeholder in (
                ("ride", "Book Ride panel channel"),
                ("funds", "Add Funds panel channel"),
                ("guide", "Help / guide channel"),
            )
        ),
        ui.ActionRow(ui.RoleSelect(
            custom_id="adm:sel:staff_role_id", placeholder="Staff role (can see tickets)",
            default_values=default(s["staff_role_id"], discord.SelectDefaultValueType.role),
        )),
        SEP,
        kv(
            ("Brand", s["brand_name"]),
            ("Save up to", f"{s['savings_percent']}%"),
            ("Min balance to book", money(int(s["min_ticket_credits"]))),
            ("Service fee", money(int(s["service_fee"]))),
            ("Cancel fee", money(int(s["cancel_fee"]))),
            ("Price multiplier", f"× {s['price_multiplier']}"),
            ("Ticket timeout", f"{s['ticket_timeout_minutes']} min"),
            ("Top-up range", f"{money(int(s['min_topup']))} – {money(int(s['max_topup']))}"),
            ("Success channel", channel_ref("success_channel_id")),
            ("Staff role", f"<@&{s['staff_role_id']}>" if s["staff_role_id"] else "not set"),
        ),
        [
            button("Fees & Limits", "adm:edit:fees", discord.ButtonStyle.primary, emoji="💰"),
            button("Branding & Top-ups", "adm:edit:brand", discord.ButtonStyle.primary, emoji="🏷️"),
            button("Back", "adm:home", emoji="↩️"),
        ],
        color=BLURPLE,
    )


def parse_money_input(value: str, label: str, minimum: float = 0) -> int:
    try:
        amount = float(value.strip().lstrip("$"))
    except ValueError:
        raise UserError(f"**{label}** must be a number like `5` or `2.50`.")
    if amount < minimum:
        raise UserError(f"**{label}** can't be less than {minimum}.")
    return dollars_to_cents(amount)


class FeesModal(ui.Modal, title="Fees & limits"):
    def __init__(self):
        super().__init__()
        s = db.get_settings()
        self.min_credits = ui.TextInput(label="Minimum balance to book ($)", default=f"{int(s['min_ticket_credits']) / 100:.2f}")
        self.service_fee = ui.TextInput(label="Service fee / markup per ride ($)", default=f"{int(s['service_fee']) / 100:.2f}")
        self.cancel_fee = ui.TextInput(label="Cancellation fee ($)", default=f"{int(s['cancel_fee']) / 100:.2f}")
        self.multiplier = ui.TextInput(label="Price multiplier (keep 1.0 · 1.2 = +20%)", default=str(s["price_multiplier"]))
        self.timeout_minutes = ui.TextInput(label="Ticket auto-close (minutes of inactivity)",
                                            default=str(s["ticket_timeout_minutes"]))
        for item in (self.min_credits, self.service_fee, self.cancel_fee, self.multiplier, self.timeout_minutes):
            self.add_item(item)

    async def on_submit(self, inter: discord.Interaction):
        try:
            values = {
                "min_ticket_credits": parse_money_input(self.min_credits.value, "Minimum balance"),
                "service_fee": parse_money_input(self.service_fee.value, "Service fee"),
                "cancel_fee": parse_money_input(self.cancel_fee.value, "Cancellation fee"),
            }
            try:
                multiplier = float(self.multiplier.value)
                timeout = int(self.timeout_minutes.value)
            except ValueError:
                raise UserError("Multiplier must be a number and timeout a whole number of minutes.")
            if not 0.01 <= multiplier <= 100 or not 1 <= timeout <= 10080:
                raise UserError("Multiplier must be 0.01–100 and timeout 1–10080 minutes.")
        except UserError as e:
            return await inter.response.send_message(view=notice("❌ Not saved", str(e), ORANGE), ephemeral=True)
        for key, value in values.items():
            db.set(key, value)
        db.set("price_multiplier", multiplier)
        db.set("ticket_timeout_minutes", timeout)
        await inter.response.edit_message(view=config_view())
        await refresh_panels()


class BrandingModal(ui.Modal, title="Branding & top-ups"):
    def __init__(self):
        super().__init__()
        s = db.get_settings()
        self.brand = ui.TextInput(label="Brand name", default=s["brand_name"], max_length=60)
        self.savings = ui.TextInput(label="Save up to (%)", default=str(s["savings_percent"]), max_length=3)
        self.min_topup = ui.TextInput(label="Minimum top-up ($)", default=f"{int(s['min_topup']) / 100:.2f}")
        self.max_topup = ui.TextInput(label="Maximum top-up ($)", default=f"{int(s['max_topup']) / 100:.2f}")
        self.funds_message = ui.TextInput(
            label="Add Funds message (when no payments set up)", default=s["add_funds_message"] or "",
            style=discord.TextStyle.paragraph, required=False, max_length=1000,
        )
        for item in (self.brand, self.savings, self.min_topup, self.max_topup, self.funds_message):
            self.add_item(item)

    async def on_submit(self, inter: discord.Interaction):
        try:
            low = parse_money_input(self.min_topup.value, "Minimum top-up", 0.5)
            high = parse_money_input(self.max_topup.value, "Maximum top-up", 0.5)
            if high < low:
                raise UserError("Maximum top-up must be at least the minimum.")
            try:
                savings = int(self.savings.value)
            except ValueError:
                raise UserError("Savings must be a whole number like `50`.")
            if not 0 <= savings <= 100:
                raise UserError("Savings must be between 0 and 100.")
        except UserError as e:
            return await inter.response.send_message(view=notice("❌ Not saved", str(e), ORANGE), ephemeral=True)
        db.set("brand_name", self.brand.value.strip())
        db.set("savings_percent", savings)
        db.set("min_topup", low)
        db.set("max_topup", high)
        db.set("add_funds_message", self.funds_message.value.strip())
        await inter.response.edit_message(view=config_view())
        await refresh_panels()


async def ping_view() -> ui.LayoutView:
    try:
        ping = await bot.api.ping()
    except Exception as e:
        return error_card(e, "API unreachable")
    caller = ping.get("caller", {})
    return notice("✅ API OK", f"Caller **{caller.get('name')}** ({caller.get('role')})", GREEN)


async def slots_view() -> ui.LayoutView:
    try:
        slots = await bot.api.list_slots(include_warming=True, include_disabled=True)
    except Exception as e:
        return error_card(e)
    busy = db.busy_slots()
    lines = []
    for s in slots[:25]:
        if s.get("status") == "disabled":
            ready = "🚫 Disabled"
        elif s.get("ready"):
            ready = "✅ Ready"
        elif s.get("ready_at"):
            ready = f"⏳ Warming up · ready <t:{s['ready_at']}:R>"
        else:
            ready = f"⏳ Warming up · {s.get('ready_in_label', '?')}"
        in_use = " · 🚗 in use" if s["slot_id"] in busy else ""
        lines.append(
            f"`{s['slot_id']}`\n"
            f"{ready}{in_use}\n"
            f"Region: {s.get('region', '—')} · Credit: {money(s.get('credit_cents'))}"
        )
    ready_count = sum(1 for s in slots if s.get("ready") and s.get("status") != "disabled")
    more = f"\n\n-# …and {len(slots) - 25} more" if len(slots) > 25 else ""
    return card(
        f"### 🎰 Slots ({len(slots)}) · {ready_count} ready for bookings",
        SEP,
        ("\n\n".join(lines) or "No slots on this API account yet.") + more,
        [button("Refresh", "adm:slots", emoji="🔄"), button("Back", "adm:home", emoji="↩️")],
    )


CARD_FORMAT = "4111111111111111,10/30,123,10001"


def operator_member() -> str:
    """Must never equal a customer's Discord ID, or the operator card pool would mix with that customer's cards."""
    return db.get("operator_member_id") or OPERATOR_MEMBER_ID


async def save_operator_card(card_data: dict, label: str | None) -> dict:
    """Add a card to the operator's pool on the API (many cards per member; first one becomes default)."""
    return await bot.api.save_card(operator_member(), card_data, label=label)


# Cards an order is running on right now — never handed to a second booking at the same time.
_cards_in_flight: set[str] = set()


async def operator_card_order() -> list[str | None]:
    """Operator cards to try for one booking: pinned card, then default, then the rest.
    Skips cards in use by another booking or a still-pending ride.
    [None] lets the API pick its default when the list can't be read."""
    try:
        cards = await bot.api.list_cards(operator_member())
    except ApiError as e:
        log.warning("Couldn't list operator cards: %s", e)
        return [None]
    if not cards:
        return [None]
    taken = _cards_in_flight | db.busy_cards()
    pinned = db.get("operator_card_id")
    return [c["card_id"] for c in sorted(cards, key=lambda c: (c["card_id"] != pinned, not c.get("is_default")))
            if c["card_id"] not in taken]


async def delete_used_card(card_id: str | None, reason: str = "used for a booked ride") -> None:
    """Remove an operator card from the pool: after it paid a ride (single-use VCC) or after it failed."""
    if not card_id:
        return
    try:
        await bot.api.delete_card(operator_member(), card_id)
        if db.get("operator_card_id") == card_id:
            db.set("operator_card_id", "")
        log.info("Deleted operator card %s (%s)", card_id, reason)
    except ApiError as e:
        if e.code != "card_not_found":
            log.warning("Couldn't delete operator card %s (%s): %s", card_id, reason, e)
        return
    except Exception:
        log.exception("Couldn't delete operator card %s (%s)", card_id, reason)
        return
    try:
        left = len(await bot.api.list_cards(operator_member()))
    except Exception:
        return
    if left <= LOW_CARD_POOL:
        await alert_owners(
            "cards_low", "💳 Card pool is running low",
            f"Only **{left}** card(s) left — new bookings fail once it's empty.\n"
            "Add more with **/dashboard → Add Card**.",
        )


def parse_card_line(line: str) -> dict:
    """`PAN,MM/YY,CVV,ZIP` (commas or | between fields) -> API card fields. Raises UserError on bad input."""
    parts = [p.strip() for p in re.split(r"[,|]", line.strip())]
    if len(parts) != 4:
        raise UserError(f"Enter 4 parts separated by commas: `{CARD_FORMAT}`")
    number, expiry, cvc, zip_code = parts
    number = re.sub(r"[\s-]", "", number)
    if not re.fullmatch(r"\d{12,19}", number):
        raise UserError("The card number should be 12–19 digits.")
    match = re.fullmatch(r"(\d{1,2})\s*/\s*(\d{2}|\d{4})", expiry)
    if not match or not 1 <= int(match.group(1)) <= 12:
        raise UserError("The expiry should look like `MM/YY`, e.g. `10/30`.")
    if not re.fullmatch(r"\d{3,4}", cvc):
        raise UserError("The CVV should be 3 or 4 digits.")
    if not re.fullmatch(r"\d{5}(-\d{4})?", zip_code):
        raise UserError("The ZIP should be a 5-digit US ZIP, e.g. `10001`.")
    return {"number": number, "exp_month": match.group(1).zfill(2), "exp_year": match.group(2), "cvc": cvc, "zip": zip_code}


MAX_CARDS_PER_ADD = 25
LOW_CARD_POOL = 2


class CardModal(ui.Modal):
    """Admins add cards to the operator pool — one card per line, many at once."""

    def __init__(self):
        super().__init__(title="Add cards to the pool")
        self.card_lines = ui.TextInput(
            label=f"Cards — one per line (max {MAX_CARDS_PER_ADD})", style=discord.TextStyle.paragraph,
            placeholder=f"{CARD_FORMAT}\n5555555555554444,11/29,456,90210", min_length=20, max_length=4000,
        )
        self.label = ui.TextInput(label="Label (optional)", required=False, max_length=40,
                                  placeholder="e.g. Batch 12")
        self.add_item(self.card_lines)
        self.add_item(self.label)

    async def on_submit(self, inter: discord.Interaction):
        if not is_admin(inter.user):
            return await inter.response.send_message(view=notice("⛔ Admins only", ADMIN_ONLY, RED), ephemeral=True)
        lines = [ln.strip() for ln in self.card_lines.value.splitlines() if ln.strip()]
        if len(lines) > MAX_CARDS_PER_ADD:
            return await inter.response.send_message(
                view=notice("❌ Too many cards", f"Add at most {MAX_CARDS_PER_ADD} cards at a time.", ORANGE),
                ephemeral=True)
        await inter.response.defer(ephemeral=True)
        saved, failed = [], []
        for n, line in enumerate(lines, 1):
            try:
                c = await save_operator_card(parse_card_line(line), self.label.value or None)
                saved.append(f"✅ {card_label(c)}")
            except UserError as e:
                failed.append(f"❌ Line {n}: {e}")
            except ApiError as e:
                failed.append(f"❌ Line {n}: {e.message}")
                if e.code == "card_pool_full":
                    failed.append(f"-# Pool is full — {len(lines) - n} line(s) not tried. Remove used cards first.")
                    break
            except Exception as e:
                log.exception("Couldn't save operator card")
                failed.append(f"❌ Line {n}: {e}")
        title = (f"✅ {len(saved)} card(s) added" if not failed
                 else f"⚠️ {len(saved)} added · {sum(1 for f in failed if f.startswith('❌'))} failed")
        await inter.followup.send(
            view=notice(title, "\n".join(saved + failed)[:3800], GREEN if not failed else ORANGE,
                        [button("List Cards", "adm:card:list", emoji="📋")]),
            ephemeral=True,
        )


def card_label(c) -> str:
    return (f"{'★' if c.get('is_default') else '•'} {c.get('label') or 'Card'} •••• {c.get('last4')} "
            f"({c.get('exp_month')}/{c.get('exp_year')})")


def card_options(cards: list[dict]) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=card_label(c)[:100], value=c["card_id"]) for c in cards[:25]]


async def cards_view(remove: bool = False) -> ui.LayoutView:
    try:
        cards = await bot.api.list_cards(operator_member())
    except Exception as e:
        return error_card(e)
    back = [button("Add Card", "adm:card:add", discord.ButtonStyle.success, emoji="➕"),
            button("Back", "adm:home", emoji="↩️")]
    if not cards:
        return notice("💳 No operator cards", "Add the cards the API charges for rides.", buttons=back)
    if not remove:
        pinned = db.get("operator_card_id")
        lines = [f"{card_label(c)} `{c['card_id']}`" + (" · 📌 pinned" if c["card_id"] == pinned else "")
                 for c in cards]
        default_select = ui.Select(custom_id="adm:carddef", placeholder="⭐ Charge this card first",
                                   options=card_options(cards))
        return card(
            f"### 💳 Operator cards ({len(cards)})",
            SEP,
            "\n".join(lines),
            ui.ActionRow(default_select),
            back,
            "-# ★ = charged first. If a card is declined the next one is tried automatically, "
            "and each card is deleted after the ride it paid for.",
        )
    select = ui.Select(custom_id="adm:cardrm", placeholder="Choose a card to delete", options=card_options(cards))
    return card("### 🗑️ Remove operator card", "Pick the card to delete. This can't be undone.",
                ui.ActionRow(select), [button("Back", "adm:home", emoji="↩️")], color=RED)


async def handle_admin_action(inter: discord.Interaction, action: str):
    if not is_admin(inter.user):
        return await inter.response.send_message(view=notice("⛔ Admins only", ADMIN_ONLY, RED), ephemeral=True)

    # Buttons on the public dashboard open a private copy; buttons inside private copies edit in place.
    private = bool(inter.message and inter.message.flags.ephemeral)

    async def show(view_or_coro):
        if not inter.response.is_done():
            if private:
                await inter.response.defer()
            else:
                await inter.response.defer(ephemeral=True, thinking=True)
        view = await view_or_coro if asyncio.iscoroutine(view_or_coro) else view_or_coro
        if private:
            await inter.edit_original_response(view=view)
        else:
            await inter.followup.send(view=view, ephemeral=True)

    if action == "home":
        if private:
            return await show(dashboard_view())
        await inter.response.edit_message(view=dashboard_view())
        return
    if action == "config":
        return await show(config_view())
    if action == "profits":
        return await show(profits_view())
    if action == "slots":
        return await show(slots_view())
    if action == "ping":
        return await show(ping_view())
    if action.startswith("wallet:"):
        return await show(wallet_pick_view(action[7:]))
    if action.startswith("wuser:"):
        wallet_action = action[6:]
        user_id = int(((inter.data or {}).get("values") or ["0"])[0])
        if wallet_action == "view":
            return await inter.response.edit_message(view=wallet_balance_view(user_id))
        return await inter.response.send_modal(WalletModal(wallet_action, user_id))
    if action.startswith("wedit:"):
        _, wallet_action, user_id = action.split(":")
        return await inter.response.send_modal(WalletModal(wallet_action, int(user_id)))
    if action == "card:add":
        return await inter.response.send_modal(CardModal())
    if action == "card:list":
        return await show(cards_view())
    if action == "card:remove":
        return await show(cards_view(remove=True))
    if action == "edit:fees":
        return await inter.response.send_modal(FeesModal())
    if action == "edit:brand":
        return await inter.response.send_modal(BrandingModal())
    if action.startswith("place:"):
        kind = action[6:]
        channel_id = int(((inter.data or {}).get("values") or ["0"])[0])
        target = inter.guild.get_channel(channel_id) if inter.guild else None
        if not isinstance(target, discord.TextChannel):
            return await inter.response.send_message(view=notice("Pick a text channel."), ephemeral=True)
        await inter.response.defer()
        try:
            await move_panel(kind, target)
        except discord.Forbidden:
            return await inter.followup.send(
                view=notice("Missing permission", f"I can't post in {target.mention}.", ORANGE), ephemeral=True
            )
        await inter.edit_original_response(view=config_view())
        return await inter.followup.send(
            view=notice(f"✅ {POSTERS[kind][0]} posted", target.mention, GREEN), ephemeral=True
        )
    if action.startswith("sel:"):
        key = action[4:]
        values = (inter.data or {}).get("values") or []
        db.set(key, values[0] if values else "")
        await inter.response.edit_message(view=config_view())
        await refresh_panels()
        return
    if action == "cardrm":
        card_id = ((inter.data or {}).get("values") or [""])[0]
        await inter.response.defer()
        try:
            await bot.api.delete_card(operator_member(), card_id)
        except Exception as e:
            return await inter.edit_original_response(view=error_card(e))
        if db.get("operator_card_id") == card_id:
            db.set("operator_card_id", "")
        return await inter.edit_original_response(
            view=notice("🗑️ Card deleted", f"`{card_id}`", GREY, [button("Back", "adm:home", emoji="↩️")])
        )
    if action == "carddef":
        card_id = ((inter.data or {}).get("values") or [""])[0]
        await inter.response.defer()
        try:
            await bot.api.set_default_card(operator_member(), card_id)
        except Exception as e:
            return await inter.edit_original_response(view=error_card(e))
        db.set("operator_card_id", "")
        return await inter.edit_original_response(view=await cards_view())


@bot.tree.command(name="dashboard", description="Open the admin dashboard")
@admin_perms
async def dashboard_cmd(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    await update_stock()
    await inter.followup.send(view=dashboard_view(), ephemeral=True)


def admin_help_view() -> ui.LayoutView:
    return card(
        "# 📖 Admin guide",
        "Everything is run from **`/dashboard`** (admins only, only you can see it).",
        SEP,
        "**🚀 First-time setup**\n"
        "1. `/dashboard` → **Config** → pick the **Book Ride**, **Add Funds** and **Help / guide** channels. "
        "The panels post there automatically (pick another channel to move them).\n"
        "2. Pick the **Success channel** (booking posts) and the **Staff role** (sees tickets, confirms payments).\n"
        "3. **Fees & Limits** → set your markup as the **Service fee** and keep the **Price multiplier** at 1.0.\n"
        "4. **Add Card** → add the card rides are charged to, then give users balance.",
        SEP,
        "**⚙️ Server**\n"
        "• **Config** — channels, staff role, Fees & Limits (service fee, cancel fee, minimum balance, "
        "ticket auto-close), Branding & Top-ups (name, \"save up to %\", top-up range).\n"
        "• **Profits** — rides booked, cancelled, customers paid, spent on card, profit, top-ups.\n"
        "• **Slots** — ride accounts and which are ready. Stock on the panel = ready slots not in a ride.\n"
        "• **Ping API** — checks the ride API is reachable.",
        "**👛 Wallets**\n"
        "**Add / Remove / Set / View Balance** → search a user, then enter an amount in dollars.",
        "**💳 Card pool** (only admins — customers never see cards)\n"
        f"• **Add Card** — one card per line: `{CARD_FORMAT}` (card, MM/YY, CVV, ZIP), up to "
        f"{MAX_CARDS_PER_ADD} per add.\n"
        "• Every ride is paid from the pool automatically: ★ default first; a card that fails is "
        "**removed from the pool** and the **next card is tried automatically**.\n"
        "• The card that paid is **deleted automatically after the ride**, and the ride account (slot) it "
        "used leaves the pool, so neither is used twice.\n"
        "• If every card is declined the customer is refunded and **you get a DM**; you're also DMed when "
        "the pool runs low.\n"
        "• **List Cards** → *Charge this card first* to pick the default · **Remove Card** to delete.",
        SEP,
        "**🎫 How customers use it**\n"
        "• **Book Ride** opens a private ticket → pickup → drop-off → pick a ride → paid from their balance. "
        "They can **Check Status** or **Cancel** (cancel fee applies).\n"
        "• **Add Funds** opens a ticket → Card (Stripe) / Crypto (OxaPay) are credited automatically. "
        "**Other** pings staff, who press **Confirm** or **Decline** (staff/admins only).\n"
        "• Tickets go in the **🎫 Tickets** category and close after inactivity or with **Close Ticket**.",
        SEP,
        [button("Open Dashboard", "adm:home", discord.ButtonStyle.primary, emoji="🛠️")],
        "-# Admins only · only you can see this",
        color=BLURPLE,
    )


@bot.tree.command(name="adminhelp", description="How to use the bot and the dashboard")
@admin_perms
async def adminhelp_cmd(inter: discord.Interaction):
    await inter.response.send_message(view=admin_help_view(), ephemeral=True)


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")


if __name__ == "__main__":
    # Prefer: python run.py  or  start.bat  (setup wizard when .env is missing)
    bot.run(DISCORD_TOKEN, log_handler=None)
