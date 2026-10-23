import os
import uuid
import asyncio
import logging
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("NOVA_API_BASE", "https://api.novalabs.solutions/v1").rstrip("/")
API_KEY = os.getenv("NOVA_API_KEY", "").strip()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=130, connect=15)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nova-bot")


class NovaAPIError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class NovaClient:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        if self.session is None or self.session.closed:
            headers = {"X-API-Key": API_KEY, "Accept": "application/json"}
            self.session = aiohttp.ClientSession(headers=headers, timeout=REQUEST_TIMEOUT)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        await self.start()
        assert self.session is not None
        url = f"{API_BASE}{path}"
        try:
            async with self.session.request(method, url, json=body) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    raw = await response.text()
                    raise NovaAPIError(f"Nova Labs returned an unreadable response (HTTP {response.status}).")
                if response.status >= 400 or (isinstance(data, dict) and data.get("error") and "status" not in data):
                    err = data.get("error", {}) if isinstance(data, dict) else {}
                    raise NovaAPIError(
                        err.get("message", f"Nova Labs request failed (HTTP {response.status})."),
                        response.status, err.get("code")
                    )
                if not isinstance(data, dict):
                    raise NovaAPIError("Nova Labs returned an unexpected response.")
                return data
        except asyncio.TimeoutError:
            raise NovaAPIError("The Nova Labs request timed out. Check order history before retrying a confirmation.")
        except aiohttp.ClientError as exc:
            raise NovaAPIError(f"Could not connect to Nova Labs: {exc.__class__.__name__}.")

    async def settle(self, order: dict[str, Any]) -> dict[str, Any]:
        # The API's wait parameter can hold a request for up to 100 seconds.
        while order.get("status") in ("preparing", "placing"):
            oid = order.get("id")
            if not oid:
                break
            order = await self.request("GET", f"/orders/{oid}?wait=100")
        return order


nova = NovaClient()
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "N/A"


def review_embed(order: dict[str, Any]) -> discord.Embed:
    review = order.get("review") or {}
    price = review.get("price") or {}
    store = review.get("store") or {}
    embed = discord.Embed(
        title=f"Review order · {store.get('name', 'Store')}",
        description="**Nothing has been charged.** Confirm only if you agree to the total below.",
        color=discord.Color.blurple()
    )
    address = store.get("address")
    if address:
        embed.add_field(name="Store address", value=str(address)[:1024], inline=False)
    items = review.get("items") or []
    item_lines = []
    for item in items[:20]:
        qty = item.get("quantity", 1)
        name = discord.utils.escape_markdown(str(item.get("name", "Item")))
        item_lines.append(f"**{qty}×** {name}")
    if len(items) > 20:
        item_lines.append(f"…and {len(items) - 20} more item(s)")
    embed.add_field(name="Items", value="\n".join(item_lines)[:1024] or "No item details returned", inline=False)
    embed.add_field(name="Subtotal", value=money(price.get("subtotal")))
    discount = (float(price.get("promo") or 0) + float(price.get("credit") or 0))
    embed.add_field(name="Promo + credit", value=f"−{money(discount)}")
    embed.add_field(name="Service fee", value=money(price.get("service_fee", 0)))
    embed.add_field(name="Delivery fee", value=money(price.get("delivery_fee", 0)))
    embed.add_field(name="Tax", value=money(price.get("tax", 0)))
    if review.get("fulfillment") == "DELIVERY":
        embed.add_field(name="Tip", value=money(price.get("tip", 0)))
    embed.add_field(name="TOTAL TO BE CHARGED", value=f"**{money(price.get('total'))}**", inline=False)
    card = review.get("card") or {}
    chain = review.get("chain", "?")
    mode = str(review.get("fulfillment") or order.get("fulfillment") or "PICKUP").title()
    footer = f"{mode} · Chain {chain} · Review expires in 5 minutes"
    if card.get("last4"):
        footer += f" · Card ••••{card['last4']}"
    embed.set_footer(text=footer)
    return embed


def placed_message(order: dict[str, Any]) -> str:
    placed = order.get("order") or {}
    number = placed.get("number", "unknown")
    tracking = placed.get("tracking_url")
    total = placed.get("total")
    msg = f"✅ Order **#{number}** placed successfully."
    if total is not None:
        msg += f"\nTotal charged: **{money(total)}**"
    if placed.get("pickup_name"):
        msg += f"\nPickup name: **{discord.utils.escape_markdown(str(placed['pickup_name']))}**"
    if tracking:
        msg += f"\n[Track your order]({tracking})"
    if placed.get("note"):
        msg += f"\nNote: {placed['note']}"
    return msg


class ReviewView(discord.ui.View):
    def __init__(self, order_id: str, user_id: int):
        super().__init__(timeout=275)
        self.order_id = order_id
        self.user_id = user_id
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the person who started this order can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm & Pay", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.send_message("This review is no longer active.", ephemeral=True)
            return
        self.finished = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="⏳ Confirming and placing your order…", embed=None, view=self)
        try:
            order = await nova.request("POST", f"/orders/{self.order_id}/confirm?wait=100")
            order = await nova.settle(order)
            status = order.get("status")
            if status == "placed":
                message = placed_message(order)
            elif (order.get("error") or {}).get("code") == "placement_unknown":
                # Do not blindly retry; check placed orders first.
                message = ("⚠️ Nova Labs could not confirm whether placement succeeded. "
                           "Do **not** start the same cart again yet. Use `/placed` to check recent placed orders.")
            else:
                message = f"❌ {((order.get('error') or {}).get('message')) or 'Order was not placed.'}"
            await interaction.edit_original_response(content=message, embed=None, view=None)
        except NovaAPIError as exc:
            await interaction.edit_original_response(
                content=f"⚠️ {exc}. If the request timed out during confirmation, use `/placed` to check before retrying.",
                embed=None, view=None
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.send_message("This review is no longer active.", ephemeral=True)
            return
        self.finished = True
        await interaction.response.defer()
        try:
            await nova.request("DELETE", f"/orders/{self.order_id}")
            await interaction.edit_original_response(content="Cancelled. Nothing was placed.", embed=None, view=None)
        except NovaAPIError as exc:
            await interaction.edit_original_response(content=f"Could not confirm cancellation with Nova Labs: {exc}", embed=None, view=None)

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        try:
            await nova.request("DELETE", f"/orders/{self.order_id}")
        except Exception as exc:
            log.warning("Could not cancel timed-out review %s: %s", self.order_id, exc)


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %s command(s) to guild %s", len(synced), GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Synced %s global command(s)", len(synced))
    except Exception:
        log.exception("Command sync failed")


@bot.event
async def on_close():
    await nova.close()


@bot.tree.command(name="order", description="Review and place an order from a Nova Labs cart code")
@app_commands.describe(code="Your Nova Labs cart code", delivery="Choose delivery instead of pickup")
async def order_command(interaction: discord.Interaction, code: str, delivery: bool = False):
    code = code.strip().upper()
    if not code or len(code) > 80:
        await interaction.response.send_message("Please enter a valid cart code.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        idem_key = str(uuid.uuid4())
        # API docs support Idempotency-Key; request helper's normal path doesn't expose custom headers,
        # so use the shared session with this one extra header.
        await nova.start()
        assert nova.session is not None
        async with nova.session.post(
            f"{API_BASE}/orders?wait=100",
            json={"cart_code": code, "fulfillment": "DELIVERY" if delivery else "PICKUP"},
            headers={"Idempotency-Key": idem_key}
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                err = data.get("error", {}) if isinstance(data, dict) else {}
                raise NovaAPIError(err.get("message", f"Nova Labs request failed (HTTP {response.status})."),
                                   response.status, err.get("code"))
        order = await nova.settle(data)
        if order.get("status") != "review":
            error = (order.get("error") or {}).get("message") or f"Order status: {order.get('status', 'unknown')}"
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return
        view = ReviewView(order["id"], interaction.user.id)
        await interaction.followup.send(embed=review_embed(order), view=view, ephemeral=True)
    except NovaAPIError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
    except Exception:
        log.exception("Unexpected error starting order")
        await interaction.followup.send("Something went wrong while starting the order. Check the bot logs.", ephemeral=True)


@bot.tree.command(name="price", description="Check a cart's item subtotal without starting an order")
@app_commands.describe(code="Cart code to price")
async def price_command(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=True)
    try:
        data = await nova.request("GET", f"/price?cart={code.strip().upper()}")
        embed = discord.Embed(title=f"Cart price · {data.get('store', 'Store')}", color=discord.Color.blurple())
        embed.add_field(name="Subtotal", value=money(data.get("subtotal")))
        embed.add_field(name="Fulfillment", value=str(data.get("fulfillment") or "Unknown"))
        items = data.get("items") or []
        lines = [f"{x.get('quantity', 1)}× {x.get('name', 'Item')} — {money(x.get('line_total'))}" for x in items[:20]]
        embed.add_field(name="Items", value="\n".join(lines)[:1024] or "No items returned", inline=False)
        embed.set_footer(text="Subtotal only; final order price may include fees, tax, promo, and credit.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except NovaAPIError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="orders", description="List your recent in-progress and recent orders")
async def orders_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        data = await nova.request("GET", "/orders")
        orders = data.get("orders") or []
        if not orders:
            await interaction.followup.send("No recent orders found.", ephemeral=True)
            return
        lines = []
        for o in orders[:15]:
            status = str(o.get("status", "unknown")).upper()
            lines.append(f"• `{o.get('id', '?')}` — **{status}** — cart `{o.get('cart_code', '?')}`")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except NovaAPIError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="placed", description="List orders that have been placed")
@app_commands.describe(limit="Number of orders to show (1–100)")
async def placed_command(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 100] = 10):
    await interaction.response.defer(ephemeral=True)
    try:
        data = await nova.request("GET", f"/placed?limit={limit}")
        orders = data.get("orders") or []
        if not orders:
            await interaction.followup.send("No placed orders found.", ephemeral=True)
            return
        lines = []
        for o in orders[:20]:
            line = f"• **#{o.get('number', '?')}** — {discord.utils.escape_markdown(str(o.get('store', 'Store')))} — **{o.get('status', 'UNKNOWN')}**"
            if o.get("tracking_url"):
                line += f" · [Track]({o['tracking_url']})"
            lines.append(line)
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)
    except NovaAPIError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="track", description="Check live status for a placed order")
@app_commands.describe(number="Nova Labs placed-order number, e.g. L812340")
async def track_command(interaction: discord.Interaction, number: str):
    number = number.strip().upper()
    await interaction.response.defer(ephemeral=True)
    try:
        data = await nova.request("GET", f"/placed/{number}/track?refresh=1")
        if data.get("error"):
            await interaction.followup.send(data["error"].get("message", "Tracking data not found."), ephemeral=True)
            return
        store = data.get("store") or {}
        eta = data.get("eta") or {}
        embed = discord.Embed(
            title=f"Order #{data.get('number', number)} · {store.get('name', 'Order tracking')}",
            description=f"**Status:** {data.get('status', 'Unknown')}\n**Stage:** {data.get('stage', 'unknown').replace('_', ' ').title()}",
            color=discord.Color.green() if data.get("stage") == "ready" else discord.Color.blurple()
        )
        if data.get("pickup_name"):
            embed.add_field(name="Pickup name", value=str(data["pickup_name"]))
        if eta.get("to") and not data.get("completed"):
            embed.add_field(name="ETA (UTC)", value=str(eta["to"]).replace("T", " ")[:16])
        items = data.get("items") or []
        lines = []
        for item in items[:15]:
            opts = ", ".join(item.get("options") or [])
            line = f"{item.get('quantity', 1)}× {item.get('name', 'Item')}"
            if opts:
                line += f" ({opts})"
            lines.append(line)
        if lines:
            embed.add_field(name="Items", value="\n".join(lines)[:1024], inline=False)
        if data.get("tracking_url"):
            embed.add_field(name="Tracking page", value=data["tracking_url"], inline=False)
        embed.set_footer(text="Tracking data may be the last saved state if live refresh is unavailable.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except NovaAPIError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="cards", description="List saved cards (only masked details are returned)")
async def cards_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        data = await nova.request("GET", "/cards")
        cards = data.get("cards") or []
        if not cards:
            await interaction.followup.send("No saved cards found. Add cards through the Nova Labs bot.", ephemeral=True)
            return
        lines = [f"• {c.get('brand', 'Card')} ••••{c.get('last4', '????')} · exp {c.get('exp', 'unknown')} · ID `{c.get('id', '?')}`" for c in cards[:20]]
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except NovaAPIError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="chains", description="Show chain availability and account counts")
async def chains_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        data = await nova.request("GET", "/chains")
        chains = data.get("chains") or []
        if not chains:
            await interaction.followup.send("No chains found. Set them up through the Nova Labs bot.", ephemeral=True)
            return
        lines = []
        for c in chains[:20]:
            lines.append(f"• Chain **{c.get('chain', '?')}** — **{str(c.get('status', 'unknown')).upper()}** — unused: {c.get('unused', 0)} — credit: {money(c.get('credit', 0))}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except NovaAPIError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="novahealth", description="Check whether the Nova Labs API is reachable")
async def health_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        data = await nova.request("GET", "/health")
        await interaction.followup.send("✅ Nova Labs API is reachable." if data.get("ok") else "⚠️ API responded, but did not report healthy.", ephemeral=True)
    except NovaAPIError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


async def main():
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    if not API_KEY:
        raise SystemExit("Missing NOVA_API_KEY in .env")
    async with bot:
        await nova.start()
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
