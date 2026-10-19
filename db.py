from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

DEFAULT_SETTINGS: dict[str, str] = {
    "min_ticket_credits": "1000",       # cents a user needs to open a ticket
    "ticket_timeout_minutes": "15",     # inactivity before a ticket auto-closes
    "cancel_fee": "300",                # cents kept when a ride is cancelled
    "service_fee": "0",                 # cents added to every ride's fare
    "min_topup": "500",                 # cents, smallest Add Funds payment
    "max_topup": "50000",               # cents, largest Add Funds payment
    "price_multiplier": "1.0",          # ride_cost = quoted total * multiplier
    "success_channel_id": "",
    "ticket_category_id": "",
    "funds_category_id": "",            # category for Add Funds tickets (falls back to ticket_category_id)
    "staff_role_id": "",
    "operator_card_id": "",             # card charged on the API side; empty = default card
    "operator_member_id": "",           # overrides OPERATOR_MEMBER_ID (set automatically when adding a card)
    "add_funds_message": "Please contact a staff member to add funds to your wallet.",
    "brand_name": "Ride Bot",
    "savings_percent": "50",            # headline "Save up to X%" on the panel
    "guide_message": "",                # "channel_id:message_id" of the posted how-to guide
    "panel_messages": "[]",             # JSON list of [channel_id, message_id] to keep refreshed
    "funds_panel_messages": "[]",       # same, for the Add Funds panel
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wallets (user_id TEXT PRIMARY KEY, balance_cents INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    delta_cents INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    channel_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    state TEXT NOT NULL,
    quote_json TEXT,
    created_at INTEGER NOT NULL,
    last_activity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    cost_cents INTEGER NOT NULL,
    full_price_cents INTEGER,
    base_cents INTEGER,
    balance_before INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    origin TEXT,
    destination TEXT,
    order_id TEXT,
    status TEXT NOT NULL,
    refund_cents INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE TABLE IF NOT EXISTS payments (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    track_id TEXT,
    payment_url TEXT,
    status TEXT NOT NULL,
    credited INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    paid_at INTEGER
);
CREATE INDEX IF NOT EXISTS payments_track ON payments (track_id);
"""

# Ticket states
OPEN = "open"
QUOTED = "quoted"
RIDE_ACTIVE = "ride_active"
RIDE_ENDED = "ride_ended"
AWAITING_STAFF = "awaiting_staff"   # funds ticket waiting on a manual payment; never auto-closes
CLOSED = "closed"

RIDE = "ride"
FUNDS = "funds"


class InsufficientCredits(Exception):
    def __init__(self, balance: int, required: int):
        super().__init__(f"balance {balance} < required {required}")
        self.balance = balance
        self.required = required


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            ride_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(rides)")}
            if "full_price_cents" not in ride_cols:
                self.conn.execute("ALTER TABLE rides ADD COLUMN full_price_cents INTEGER")
            if "base_cents" not in ride_cols:
                self.conn.execute("ALTER TABLE rides ADD COLUMN base_cents INTEGER")
            if "member_id" not in ride_cols:
                self.conn.execute("ALTER TABLE rides ADD COLUMN member_id TEXT")
            if "payment_method" not in ride_cols:
                self.conn.execute("ALTER TABLE rides ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'wallet'")
            if "card_id" not in ride_cols:
                self.conn.execute("ALTER TABLE rides ADD COLUMN card_id TEXT")
            if "card_last4" not in ride_cols:
                self.conn.execute("ALTER TABLE rides ADD COLUMN card_last4 TEXT")
            ticket_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tickets)")}
            if "trip_json" not in ticket_cols:
                self.conn.execute("ALTER TABLE tickets ADD COLUMN trip_json TEXT")
            if "is_test" not in ticket_cols:
                self.conn.execute("ALTER TABLE tickets ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0")
            if "kind" not in ticket_cols:
                self.conn.execute("ALTER TABLE tickets ADD COLUMN kind TEXT NOT NULL DEFAULT 'ride'")
            for key, value in DEFAULT_SETTINGS.items():
                self.conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # ---------- settings ----------

    def get_settings(self) -> dict[str, str]:
        with self.lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        return {**DEFAULT_SETTINGS, **{r["key"]: r["value"] for r in rows}}

    def get(self, key: str) -> str:
        return self.get_settings()[key]

    def get_int(self, key: str) -> int:
        try:
            return int(self.get(key) or 0)
        except ValueError:
            return int(DEFAULT_SETTINGS[key] or 0)

    def get_float(self, key: str) -> float:
        try:
            return float(self.get(key))
        except ValueError:
            return float(DEFAULT_SETTINGS[key])

    def set(self, key: str, value: Any) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # ---------- wallets ----------

    def balance(self, user_id: int | str) -> int:
        with self.lock:
            row = self.conn.execute("SELECT balance_cents FROM wallets WHERE user_id = ?", (str(user_id),)).fetchone()
        return row["balance_cents"] if row else 0

    def adjust(self, user_id: int | str, delta: int, reason: str, *, require_funds: bool = False) -> tuple[int, int]:
        """Atomically add `delta` cents. Returns (before, after). Raises InsufficientCredits if
        `require_funds` and the balance would go negative."""
        uid = str(user_id)
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("INSERT OR IGNORE INTO wallets (user_id, balance_cents) VALUES (?, 0)", (uid,))
                before = self.conn.execute("SELECT balance_cents FROM wallets WHERE user_id = ?", (uid,)).fetchone()[0]
                after = before + delta
                if require_funds and after < 0:
                    raise InsufficientCredits(before, -delta)
                self.conn.execute("UPDATE wallets SET balance_cents = ? WHERE user_id = ?", (after, uid))
                self.conn.execute(
                    "INSERT INTO transactions (user_id, delta_cents, balance_after, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                    (uid, delta, after, reason, int(time.time())),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return before, after

    def set_balance(self, user_id: int | str, amount: int, reason: str) -> tuple[int, int]:
        return self.adjust(user_id, amount - self.balance(user_id), reason)

    def wallets(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM wallets ORDER BY balance_cents DESC").fetchall()

    def transactions(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---------- tickets ----------

    def create_ticket(self, channel_id: int, guild_id: int, user_id: int, is_test: bool = False, kind: str = RIDE) -> None:
        now = int(time.time())
        with self.lock:
            self.conn.execute(
                "INSERT INTO tickets (channel_id, guild_id, user_id, state, created_at, last_activity, is_test, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(channel_id), str(guild_id), str(user_id), OPEN, now, now, int(is_test), kind),
            )

    def get_ticket(self, channel_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM tickets WHERE channel_id = ?", (str(channel_id),)).fetchone()

    def open_ticket_for(self, user_id: int, kind: str = RIDE) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM tickets WHERE user_id = ? AND state != ? AND is_test = 0 AND kind = ?",
                (str(user_id), CLOSED, kind),
            ).fetchone()

    def update_ticket(self, channel_id: int, **fields: Any) -> None:
        fields["last_activity"] = int(time.time())
        if "quote" in fields:
            quote = fields.pop("quote")
            fields["quote_json"] = json.dumps(quote) if quote is not None else None
        if "trip" in fields:
            trip = fields.pop("trip")
            fields["trip_json"] = json.dumps(trip) if trip is not None else None
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE tickets SET {cols} WHERE channel_id = ?", (*fields.values(), str(channel_id)))

    def touch_ticket(self, channel_id: int) -> None:
        self.update_ticket(channel_id)

    def ticket_quote(self, ticket: sqlite3.Row) -> dict | None:
        return json.loads(ticket["quote_json"]) if ticket["quote_json"] else None

    def ticket_trip(self, ticket: sqlite3.Row) -> dict:
        return json.loads(ticket["trip_json"]) if ticket["trip_json"] else {}

    def stale_tickets(self, timeout_sec: int) -> list[sqlite3.Row]:
        cutoff = int(time.time()) - timeout_sec
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM tickets WHERE state IN (?, ?, ?) AND last_activity < ?",
                (OPEN, QUOTED, RIDE_ENDED, cutoff),
            ).fetchall()

    def open_tickets(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM tickets WHERE state != ? ORDER BY created_at DESC", (CLOSED,)).fetchall()

    # ---------- rides ----------

    def create_ride(self, **fields: Any) -> int:
        fields.setdefault("created_at", int(time.time()))
        for key in ("origin", "destination"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key])
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.lock:
            cur = self.conn.execute(f"INSERT INTO rides ({cols}) VALUES ({marks})", tuple(fields.values()))
            return cur.lastrowid

    def update_ride(self, ride_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE rides SET {cols} WHERE id = ?", (*fields.values(), ride_id))

    def active_ride(self, channel_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM rides WHERE channel_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (str(channel_id),),
            ).fetchone()

    def busy_slots(self) -> set[str]:
        with self.lock:
            rows = self.conn.execute("SELECT slot_id FROM rides WHERE status = 'active'").fetchall()
        return {r["slot_id"] for r in rows}

    def busy_cards(self) -> set[str]:
        """Operator cards still attached to an active (not yet completed) ride."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT card_id FROM rides WHERE status = 'active' AND card_id IS NOT NULL AND card_id != ''"
            ).fetchall()
        return {r["card_id"] for r in rows}

    def stats(self) -> dict[str, int | None]:
        """Real numbers for the panel: booked rides (not cancelled/failed), total saved, last booking time."""
        with self.lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS rides,
                          COALESCE(SUM(MAX(COALESCE(full_price_cents, cost_cents) - cost_cents
                                           - CASE WHEN payment_method = 'card' THEN COALESCE(base_cents, 0) ELSE 0 END,
                                           0)), 0) AS saved,
                          MAX(created_at) AS last_ride
                   FROM rides WHERE status IN ('active', 'completed')"""
            ).fetchone()
        return {"rides": row["rides"], "saved": row["saved"], "last_ride": row["last_ride"]}

    def profit_stats(self) -> dict[str, int]:
        """Revenue = what customers paid net of refunds. Card cost = the API price charged to the operator card,
        counted for wallet rides that weren't cancelled (customer-card rides cost the operator nothing).
        Rides booked before base_cents existed count at zero profit."""
        with self.lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS rides,
                          COALESCE(SUM(status = 'completed'), 0) AS completed,
                          COALESCE(SUM(status = 'active'), 0) AS active,
                          COALESCE(SUM(status = 'cancelled'), 0) AS cancelled,
                          COALESCE(SUM(cost_cents - refund_cents), 0) AS revenue,
                          COALESCE(SUM(CASE WHEN status IN ('active', 'completed') AND payment_method != 'card'
                                            THEN COALESCE(base_cents, cost_cents) ELSE 0 END), 0) AS card_cost
                   FROM rides"""
            ).fetchone()
            topups = self.conn.execute(
                "SELECT COALESCE(SUM(delta_cents), 0) FROM transactions "
                "WHERE delta_cents > 0 AND (reason LIKE 'top-up via%' OR reason LIKE 'manual top-up%')"
            ).fetchone()[0]
        stats = dict(row)
        stats["profit"] = stats["revenue"] - stats["card_cost"]
        stats["topups"] = topups
        return stats

    def wallet_summary(self, user_id: int | str) -> dict[str, int]:
        uid = str(user_id)
        with self.lock:
            topped_up = self.conn.execute(
                "SELECT COALESCE(SUM(delta_cents), 0) FROM transactions "
                "WHERE user_id = ? AND delta_cents > 0 AND reason NOT LIKE 'refund%'", (uid,)
            ).fetchone()[0]
            rides = self.conn.execute(
                "SELECT COUNT(*) AS rides, COALESCE(SUM(cost_cents - refund_cents), 0) AS spent "
                "FROM rides WHERE user_id = ?", (uid,)
            ).fetchone()
        return {"topped_up": topped_up, "spent": rides["spent"], "rides": rides["rides"]}

    def recent_transactions(self, user_id: int | str, limit: int = 5) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?", (str(user_id), limit)
            ).fetchall()

    def panel_messages(self, key: str = "panel_messages") -> list[list[int]]:
        try:
            return json.loads(self.get(key) or "[]")
        except json.JSONDecodeError:
            return []

    def add_panel_message(self, channel_id: int, message_id: int, key: str = "panel_messages") -> None:
        panels = [p for p in self.panel_messages(key) if p[0] != channel_id]
        panels.append([channel_id, message_id])
        self.set(key, json.dumps(panels))

    def remove_panel_message(self, message_id: int, key: str = "panel_messages") -> None:
        self.set(key, json.dumps([p for p in self.panel_messages(key) if p[1] != message_id]))

    # ---------- payments ----------

    def create_payment(self, order_id: str, user_id: int | str, provider: str, amount_cents: int,
                       track_id: str, payment_url: str, expires_at: int) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO payments (order_id, user_id, provider, amount_cents, track_id, payment_url, status, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (order_id, str(user_id), provider, amount_cents, track_id, payment_url, int(time.time()), expires_at),
            )

    def get_payment(self, order_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()

    def payment_by_track(self, provider: str, track_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM payments WHERE provider = ? AND track_id = ?", (provider, track_id)
            ).fetchone()

    def pending_payments(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM payments WHERE status = 'pending' ORDER BY created_at").fetchall()

    def set_payment_status(self, order_id: str, status: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE payments SET status = ? WHERE order_id = ? AND credited = 0", (status, order_id)
            )

    def credit_payment(self, order_id: str) -> tuple[int, int] | None:
        """Mark a payment paid and add its amount to the wallet, exactly once.
        Returns (before, after) when this call credited it, None if it was already credited."""
        now = int(time.time())
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute(
                    "UPDATE payments SET status = 'paid', credited = 1, paid_at = ? WHERE order_id = ? AND credited = 0",
                    (now, order_id),
                )
                if cur.rowcount != 1:
                    self.conn.execute("ROLLBACK")
                    return None
                row = self.conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
                uid, amount = row["user_id"], row["amount_cents"]
                self.conn.execute("INSERT OR IGNORE INTO wallets (user_id, balance_cents) VALUES (?, 0)", (uid,))
                before = self.conn.execute("SELECT balance_cents FROM wallets WHERE user_id = ?", (uid,)).fetchone()[0]
                after = before + amount
                self.conn.execute("UPDATE wallets SET balance_cents = ? WHERE user_id = ?", (after, uid))
                self.conn.execute(
                    "INSERT INTO transactions (user_id, delta_cents, balance_after, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                    (uid, amount, after, f"top-up via {row['provider']} ({order_id})", now),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return before, after

    def recent_payments(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def recent_rides(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM rides ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
