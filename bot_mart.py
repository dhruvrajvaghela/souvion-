#!/usr/bin/env python3
"""
BOT MART - Discord marketplace bot (single file)
------------------------------------------------
Sells access to private bot repositories. Features: product catalogue managed from
Discord, private purchase/support tickets, UPI / Google Pay / PhonePe / UPI-QR payments
with staff verification, real Razorpay Payment Links + signed webhook, management
dashboard (/martpanel), server setup (/martsetup), welcome/auto-role, moderation,
bot profile controls and an interactive /help.

Secrets are read from environment variables only. See README.txt.
"""
import asyncio
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import quote, urlparse

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

# ----------------------------------------------------------------------------
# Configuration (environment variables only - never hardcode secrets)
# ----------------------------------------------------------------------------


def _int_env(name: str, default: int = 0) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OWNER_ID = _int_env("OWNER_ID")
DATABASE_PATH = (os.getenv("DATABASE_PATH") or "bot_mart.db").strip()
UPI_ID = (os.getenv("UPI_ID") or "").strip()
UPI_QR_URL = (os.getenv("UPI_QR_URL") or "").strip()
RAZORPAY_KEY_ID = (os.getenv("RAZORPAY_KEY_ID") or "").strip()
RAZORPAY_KEY_SECRET = (os.getenv("RAZORPAY_KEY_SECRET") or "").strip()
RAZORPAY_WEBHOOK_SECRET = (os.getenv("RAZORPAY_WEBHOOK_SECRET") or "").strip()
PORT = _int_env("PORT", 8080)  # Railway injects PORT automatically
PUBLIC_DOMAIN = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()

BRAND = "BOT MART"
BLUE = 0x00A8FF   # electric blue
DARK = 0x0B0F1A   # near black
GREEN = 0x2ECC71
RED = 0xE74C3C
AMBER = 0xF1C40F

log = logging.getLogger("botmart")

# ----------------------------------------------------------------------------
# Database (SQLite, thread-safe, persistent)
# ----------------------------------------------------------------------------


class Database:
    def __init__(self, path: str):
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def run(self, sql: str, params: tuple = ()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount, cur.lastrowid

    def one(self, sql: str, params: tuple = ()):
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()):
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _init_schema(self):
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    description TEXT NOT NULL DEFAULT '',
                    price REAL NOT NULL DEFAULT 0,
                    image_url TEXT NOT NULL DEFAULT '',
                    repo_url TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    guild_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (guild_id, key)
                );
                CREATE TABLE IF NOT EXISTS staff_roles (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, role_id)
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    product_id INTEGER,
                    product_name TEXT,
                    amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open',
                    method TEXT,
                    reference TEXT,
                    rzp_link_id TEXT,
                    rzp_link_url TEXT,
                    rzp_payment_id TEXT,
                    verified_by TEXT,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    control_msg_id INTEGER,
                    subject TEXT,
                    created_at TEXT NOT NULL,
                    paid_at TEXT,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (guild_id, user_id, type, status);
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self.conn.commit()
        self._seed_products()

    def _seed_products(self):
        if self.one("SELECT COUNT(*) AS c FROM products")["c"]:
            return
        seed = [
            ("Souvion", "Premium Discord bot by BOT MART. Edit this description with /editproduct."),
            ("Kiyomi", "Premium Discord bot by BOT MART. Edit this description with /editproduct."),
            ("Sora 2.0", "Premium Discord bot by BOT MART. Edit this description with /editproduct."),
        ]
        for name, desc in seed:
            # price 0 / no repo => shown as "coming soon" until the owner sets /setprice and /setrepo
            self.run(
                "INSERT INTO products (name, description, price, image_url, repo_url, active, created_at) "
                "VALUES (?, ?, 0, '', '', 1, ?)",
                (name, desc, now_iso()),
            )


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


db = Database(DATABASE_PATH)

# ----------------------------------------------------------------------------
# Settings helpers (guild_id 0 = global bot settings)
# ----------------------------------------------------------------------------


def get_setting(gid: int, key: str, default=None):
    row = db.one("SELECT value FROM settings WHERE guild_id=? AND key=?", (gid, key))
    return row["value"] if row and row["value"] is not None else default


def set_setting(gid: int, key: str, value) -> None:
    db.run(
        "INSERT INTO settings (guild_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
        (gid, key, str(value)),
    )


def del_setting(gid: int, key: str) -> None:
    db.run("DELETE FROM settings WHERE guild_id=? AND key=?", (gid, key))


def get_int(gid: int, key: str) -> Optional[int]:
    v = get_setting(gid, key)
    if v is not None and re.fullmatch(r"-?\d+", str(v)):
        return int(v)
    return None


def staff_role_ids(gid: int) -> list:
    return [r["role_id"] for r in db.all("SELECT role_id FROM staff_roles WHERE guild_id=?", (gid,))]


# ----------------------------------------------------------------------------
# Permission helpers
# ----------------------------------------------------------------------------


def is_owner(user, guild: Optional[discord.Guild] = None) -> bool:
    """Bot owner (OWNER_ID env) or the owner of the server."""
    if OWNER_ID and user.id == OWNER_ID:
        return True
    g = guild or getattr(user, "guild", None)
    return bool(g and g.owner_id == user.id)


def is_staff(user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if is_owner(user, user.guild):
        return True
    ids = set(staff_role_ids(user.guild.id))
    return any(r.id in ids for r in user.roles)


def owner_only():
    return app_commands.check(lambda i: is_owner(i.user, i.guild))


def staff_only():
    return app_commands.check(lambda i: is_staff(i.user))


# ----------------------------------------------------------------------------
# Embed + response helpers
# ----------------------------------------------------------------------------


def make_embed(title: Optional[str] = None, description: Optional[str] = None, color: int = BLUE) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"{BRAND} • Premium Bot Marketplace")
    return e


def ok_embed(msg: str, title: str = "Done") -> discord.Embed:
    return make_embed(f"✅ {title}", msg, GREEN)


def err_embed(msg: str, title: str = "Error") -> discord.Embed:
    return make_embed(f"⛔ {title}", msg, RED)


def warn_embed(msg: str, title: str = "Notice") -> discord.Embed:
    return make_embed(f"⚠️ {title}", msg, AMBER)


async def reply(interaction: discord.Interaction, content=None, *, embed=None, embeds=None, view=None,
                ephemeral: bool = True, file=None):
    kw = {"ephemeral": ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
    if content is not None:
        kw["content"] = content
    if embed is not None:
        kw["embed"] = embed
    if embeds is not None:
        kw["embeds"] = embeds
    if view is not None:
        kw["view"] = view
    if file is not None:
        kw["file"] = file
    if interaction.response.is_done():
        return await interaction.followup.send(**kw)
    return await interaction.response.send_message(**kw)


async def require_owner(interaction: discord.Interaction) -> bool:
    if is_owner(interaction.user, interaction.guild):
        return True
    await reply(interaction, embed=err_embed("Only the server owner can do this.", "Owner only"))
    return False


async def require_staff(interaction: discord.Interaction) -> bool:
    if is_staff(interaction.user):
        return True
    await reply(interaction, embed=err_embed("Only authorized BOT MART staff can do this.", "Staff only"))
    return False


# ----------------------------------------------------------------------------
# Validation helpers
# ----------------------------------------------------------------------------


def fmt_price(value) -> str:
    v = float(value or 0)
    if v <= 0:
        return "Not set"
    return f"₹{v:,.0f}" if v == int(v) else f"₹{v:,.2f}"


def to_paise(value) -> int:
    return int(round(float(value) * 100))


def parse_price(raw) -> float:
    s = str(raw).strip().replace(",", "").replace("₹", "").replace("INR", "").replace("Rs.", "").strip()
    try:
        v = float(s)
    except ValueError:
        raise ValueError("Price must be a number, for example 499 or 999.50.")
    if v != v or v < 1 or v > 1_000_000:
        raise ValueError("Price must be between ₹1 and ₹1,000,000.")
    return round(v, 2)


def clean_url(value: Optional[str], *, https_only: bool = False, label: str = "URL") -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > 500 or any(c.isspace() for c in value):
        raise ValueError(f"{label} is invalid (too long or contains spaces).")
    p = urlparse(value)
    allowed = ("https",) if https_only else ("https", "http")
    if p.scheme not in allowed or not p.netloc:
        raise ValueError(f"{label} must be a valid {'https' if https_only else 'http(s)'} link.")
    return value


def clean_name(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    if not 2 <= len(value) <= 60:
        raise ValueError("Product name must be 2-60 characters.")
    return value


def slugify(value: str, limit: int = 20) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:limit] or "user"


def is_safe_public_url(url: str) -> bool:
    """Basic SSRF guard for avatar downloads: https only, no localhost / private IPs."""
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass
    return True


# ----------------------------------------------------------------------------
# Bot class
# ----------------------------------------------------------------------------


class MartBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # Server Members Intent (welcome / auto-role)
        intents.message_content = True  # Message Content Intent (anti-spam repeat detection)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.web_runner: Optional[web.AppRunner] = None
        self.spam = defaultdict(lambda: deque(maxlen=30))
        self.locks = defaultdict(asyncio.Lock)
        self._ready_once = False

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        # persistent views survive restarts
        self.add_view(PanelView())
        self.add_view(TicketView())
        self.add_view(DashboardView())
        await start_web_server()
        try:
            synced = await self.tree.sync()
            log.info("Synced %d application commands", len(synced))
        except discord.HTTPException:
            log.exception("Command sync failed")

    async def close(self):
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await super().close()


bot = MartBot()


# ----------------------------------------------------------------------------
# Activity / sales logging
# ----------------------------------------------------------------------------


async def log_activity(guild: Optional[discord.Guild], kind: str, text: str, *, sales: bool = False,
                       color: int = BLUE, file: Optional[discord.File] = None, modlog: bool = False) -> None:
    if guild is None:
        return
    db.run("INSERT INTO logs (guild_id, kind, message, created_at) VALUES (?, ?, ?, ?)",
           (guild.id, kind, text[:1500], now_iso()))
    key = "sales_logs_channel" if sales else "activity_logs_channel"
    channel_id = get_int(guild.id, key)
    if modlog:
        channel_id = get_int(guild.id, "mod_log_channel") or channel_id
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return
    e = make_embed(kind, text[:4000], color)
    try:
        kw = {"embed": e, "allowed_mentions": discord.AllowedMentions.none()}
        if file is not None:
            kw["file"] = file
        await channel.send(**kw)
    except discord.HTTPException:
        log.warning("Could not write to log channel %s", channel_id)


# ----------------------------------------------------------------------------
# Products
# ----------------------------------------------------------------------------


def get_product(pid: int):
    return db.one("SELECT * FROM products WHERE id=?", (pid,))


def find_product(name: str):
    return db.one("SELECT * FROM products WHERE name = ? COLLATE NOCASE", ((name or "").strip(),))


def list_products(active_only: bool = False):
    sql = "SELECT * FROM products" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    return db.all(sql)


def product_ready(p) -> bool:
    return bool(p and p["active"] and p["price"] >= 1 and p["repo_url"])


def add_product(name, price, description, image_url="", repo_url="", active=True) -> dict:
    name = clean_name(name)
    price = parse_price(price)
    description = (description or "").strip()[:1000]
    image_url = clean_url(image_url, label="Image URL")
    repo_url = clean_url(repo_url, https_only=True, label="Repository URL")
    if find_product(name):
        raise ValueError(f"A product named **{name}** already exists.")
    _, pid = db.run(
        "INSERT INTO products (name, description, price, image_url, repo_url, active, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, description, price, image_url, repo_url, 1 if active else 0, now_iso()),
    )
    return get_product(pid)


def product_card(p: dict, *, owner_view: bool = False) -> discord.Embed:
    e = make_embed(f"🤖 {p['name']}", p["description"] or "No description yet.")
    e.add_field(name="Price", value=fmt_price(p["price"]), inline=True)
    if owner_view:
        e.add_field(name="Status", value="🟢 Active" if p["active"] else "⚫ Inactive", inline=True)
        e.add_field(name="Repository", value="✅ Set" if p["repo_url"] else "❌ Not set", inline=True)
    else:
        e.add_field(name="Availability", value="✅ Available" if product_ready(p) else "🔒 Coming soon", inline=True)
    if p["image_url"]:
        e.set_thumbnail(url=p["image_url"])
    return e


def products_overview_embed(owner_view: bool = True) -> discord.Embed:
    items = list_products(active_only=not owner_view)
    e = make_embed("📦 Product Catalogue", None)
    if not items:
        e.description = "No products yet. Use **Add Product**."
        return e
    lines = []
    for p in items[:25]:
        state = "🟢" if p["active"] else "⚫"
        repo = "🔗" if p["repo_url"] else "❌repo"
        if owner_view:
            lines.append(f"{state} **{p['name']}** — {fmt_price(p['price'])} — {repo}")
        else:
            tag = "" if product_ready(p) else " *(coming soon)*"
            lines.append(f"**{p['name']}** — {fmt_price(p['price'])}{tag}\n{(p['description'] or '')[:150]}")
    e.description = "\n\n".join(lines) if not owner_view else "\n".join(lines)
    return e


async def publish_catalog(guild: discord.Guild) -> None:
    """Refresh the bot-catalog / premium-bots / prices channels created by /martsetup."""
    me = guild.me
    items = list_products(active_only=True)

    async def clear_own(ch: discord.TextChannel):
        try:
            async for m in ch.history(limit=50):
                if m.author.id == me.id:
                    await m.delete()
        except discord.HTTPException:
            pass

    cat = guild.get_channel(get_int(guild.id, "catalog_channel") or 0)
    if isinstance(cat, discord.TextChannel):
        await clear_own(cat)
        e = make_embed(f"{BRAND} — Premium Bot Catalogue",
                       "Professional Discord bots with private source access.\n"
                       "Head to the purchase channel, press **Purchase**, choose a bot and pay securely.\n"
                       "Your private repository link is delivered **only after payment is verified**.")
        e.add_field(name="Bots available", value=str(len(items)), inline=True)
        try:
            await cat.send(embed=e)
        except discord.HTTPException:
            pass

    prem = guild.get_channel(get_int(guild.id, "premium_channel") or 0)
    if isinstance(prem, discord.TextChannel):
        await clear_own(prem)
        cards = [product_card(p) for p in items]
        for i in range(0, len(cards), 10):
            try:
                await prem.send(embeds=cards[i:i + 10])
            except discord.HTTPException:
                break

    prices = guild.get_channel(get_int(guild.id, "prices_channel") or 0)
    if isinstance(prices, discord.TextChannel):
        await clear_own(prices)
        e = make_embed("💰 Price List (INR)")
        e.description = "\n".join(f"**{p['name']}** — {fmt_price(p['price'])}" for p in items) or "No products listed."
        try:
            await prices.send(embed=e)
        except discord.HTTPException:
            pass


def schedule_catalog_refresh() -> None:
    async def runner():
        for g in list(bot.guilds):
            try:
                await publish_catalog(g)
            except Exception:  # noqa: BLE001
                log.exception("catalog refresh failed")
    asyncio.create_task(runner())


# ----------------------------------------------------------------------------
# Payment configuration
# ----------------------------------------------------------------------------


def upi_id_for(gid: int) -> str:
    return get_setting(gid, "upi_id") or UPI_ID


def upi_qr_for(gid: int) -> str:
    return get_setting(gid, "upi_qr_url") or UPI_QR_URL


def razorpay_ready(gid: int) -> bool:
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET) and get_setting(gid, "razorpay_enabled", "1") == "1"


def upi_uri(gid: int, amount: float, ticket_id: int) -> str:
    return (f"upi://pay?pa={quote(upi_id_for(gid))}&pn={quote(BRAND)}&am={amount:.2f}"
            f"&cu=INR&tn={quote(f'Ticket {ticket_id}')}")


def qr_image_url(gid: int, amount: float, ticket_id: int) -> Optional[str]:
    custom = upi_qr_for(gid)
    if custom:
        return custom
    if upi_id_for(gid):
        return ("https://api.qrserver.com/v1/create-qr-code/?size=320x320&margin=10&data="
                + quote(upi_uri(gid, amount, ticket_id), safe=""))
    return None


# ----------------------------------------------------------------------------
# Razorpay (real Payment Links API + signed webhook)
# ----------------------------------------------------------------------------


class RazorpayError(Exception):
    pass


async def rzp_request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        raise RazorpayError("Razorpay is not configured.")
    auth = aiohttp.BasicAuth(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
    try:
        async with bot.http_session.request(
            method, f"https://api.razorpay.com/v1{path}", json=payload, auth=auth,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                desc = (data or {}).get("error", {}).get("description") if isinstance(data, dict) else None
                raise RazorpayError(desc or f"Razorpay returned HTTP {r.status}")
            return data
    except aiohttp.ClientError as exc:
        raise RazorpayError(f"Could not reach Razorpay: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise RazorpayError("Razorpay request timed out.") from exc


async def rzp_get_or_create_link(ticket: dict, member: discord.abc.User) -> str:
    """Return a live payment-link URL for the ticket (reuses an unpaid, unexpired link)."""
    if ticket.get("rzp_link_id"):
        try:
            existing = await rzp_request("GET", f"/payment_links/{ticket['rzp_link_id']}")
            if existing.get("status") in ("created", "partially_paid"):
                return existing.get("short_url") or ticket["rzp_link_url"]
        except RazorpayError:
            pass
    payload = {
        "amount": to_paise(ticket["amount"]),
        "currency": "INR",
        "accept_partial": False,
        "reference_id": f"botmart-{ticket['id']}-{int(time.time())}",
        "description": f"{BRAND}: {ticket['product_name']} (ticket #{ticket['id']})"[:255],
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "expire_by": int(time.time()) + 24 * 3600,
        "notes": {"ticket_id": str(ticket["id"]), "discord_user_id": str(member.id)},
    }
    data = await rzp_request("POST", "/payment_links", payload)
    link_id, url = data.get("id"), data.get("short_url")
    if not link_id or not url:
        raise RazorpayError("Razorpay did not return a payment link.")
    db.run("UPDATE tickets SET rzp_link_id=?, rzp_link_url=?, method='Razorpay' WHERE id=?",
           (link_id, url, ticket["id"]))
    return url


async def rzp_check_ticket(ticket: dict) -> tuple:
    """Ask Razorpay whether the ticket's payment link has been paid. Returns (paid, message)."""
    if not ticket.get("rzp_link_id"):
        return False, "No Razorpay payment link has been created for this ticket yet."
    data = await rzp_request("GET", f"/payment_links/{ticket['rzp_link_id']}")
    status = data.get("status")
    paid = int(data.get("amount_paid") or 0)
    if status == "paid" and paid >= to_paise(ticket["amount"]) and data.get("currency", "INR") == "INR":
        pay_id = None
        payments = data.get("payments") or []
        if payments and isinstance(payments[0], dict):
            pay_id = payments[0].get("payment_id")
        done = await finalize_payment(ticket["id"], verified_by="Razorpay (API)", payment_id=pay_id, method="Razorpay")
        return True, "Payment confirmed by Razorpay." if done else "This ticket was already processed."
    return False, f"Razorpay reports the payment link status as **{status or 'unknown'}**. No payment received yet."


async def razorpay_webhook(request: web.Request) -> web.Response:
    if not RAZORPAY_WEBHOOK_SECRET:
        return web.Response(status=503, text="webhook secret not configured")
    body = await request.read()
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        log.warning("Rejected Razorpay webhook with invalid signature")
        return web.Response(status=400, text="invalid signature")
    try:
        data = json.loads(body)
    except ValueError:
        return web.Response(status=400, text="invalid json")
    if data.get("event") != "payment_link.paid":
        return web.Response(text="ignored")
    try:
        payload = data.get("payload", {})
        link = payload.get("payment_link", {}).get("entity", {})
        link_id = link.get("id")
        ticket = db.one("SELECT * FROM tickets WHERE rzp_link_id=?", (link_id,)) if link_id else None
        if not ticket:
            return web.Response(text="unknown link")
        paid = int(link.get("amount_paid") or 0)
        if link.get("status") != "paid" or paid < to_paise(ticket["amount"]) or link.get("currency", "INR") != "INR":
            guild = bot.get_guild(ticket["guild_id"])
            await log_activity(guild, "⚠️ Razorpay mismatch",
                               f"Ticket #{ticket['id']}: webhook amount/status did not match; not auto-verified.",
                               color=AMBER)
            return web.Response(text="mismatch")
        pay_id = payload.get("payment", {}).get("entity", {}).get("id")
        await finalize_payment(ticket["id"], verified_by="Razorpay (webhook)", payment_id=pay_id, method="Razorpay")
        return web.Response(text="ok")
    except Exception:  # noqa: BLE001
        log.exception("Webhook processing failed")
        return web.Response(status=500, text="error")  # Razorpay will retry


async def health(_request: web.Request) -> web.Response:
    return web.Response(text=f"{BRAND} is online")


async def start_web_server() -> None:
    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/", health)
    app.router.add_post("/razorpay/webhook", razorpay_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    bot.web_runner = runner
    log.info("Web server listening on port %s", PORT)


# ----------------------------------------------------------------------------
# Tickets
# ----------------------------------------------------------------------------


def get_ticket(tid: int):
    return db.one("SELECT * FROM tickets WHERE id=?", (tid,))


def ticket_by_channel(channel_id: int):
    return db.one("SELECT * FROM tickets WHERE channel_id=?", (channel_id,))


def resolve_ticket_category(guild: discord.Guild) -> Optional[discord.CategoryChannel]:
    cid = get_int(guild.id, "ticket_category")
    cat = guild.get_channel(cid) if cid else None
    if isinstance(cat, discord.CategoryChannel):
        return cat
    return discord.utils.find(lambda c: c.name.lower() == "support", guild.categories)


def staff_mentions(guild: discord.Guild) -> str:
    roles = [guild.get_role(r) for r in staff_role_ids(guild.id)]
    return " ".join(r.mention for r in roles if r)


async def create_ticket_channel(guild: discord.Guild, member: discord.Member, kind: str) -> discord.TextChannel:
    po = discord.PermissionOverwrite
    overwrites = {
        guild.default_role: po(view_channel=False),
        member: po(view_channel=True, send_messages=True, read_message_history=True,
                   attach_files=True, embed_links=True),
        guild.me: po(view_channel=True, send_messages=True, read_message_history=True, embed_links=True,
                     attach_files=True, manage_channels=True, manage_messages=True),
    }
    for rid in staff_role_ids(guild.id):
        role = guild.get_role(rid)
        if role:
            overwrites[role] = po(view_channel=True, send_messages=True, read_message_history=True,
                                  attach_files=True, embed_links=True)
    name = f"{kind}-{slugify(member.display_name)}"
    kwargs = dict(overwrites=overwrites, topic=f"{BRAND} {kind} ticket • owner ID {member.id}",
                  reason=f"{kind} ticket for {member}")
    category = resolve_ticket_category(guild)
    try:
        return await guild.create_text_channel(name, category=category, **kwargs)
    except discord.HTTPException:
        if category is not None:  # category may be full; retry without it
            return await guild.create_text_channel(name, **kwargs)
        raise


def ticket_embeds(guild: discord.Guild, member: discord.Member, ticket: dict, product: dict) -> list:
    gid = guild.id
    top = make_embed(f"🛒 Purchase Ticket #{ticket['id']}",
                     f"Hello {member.mention}! Complete the payment below to receive **{product['name']}**.")
    top.add_field(name="Product", value=product["name"], inline=True)
    top.add_field(name="Price", value=fmt_price(ticket["amount"]), inline=True)
    top.add_field(name="Status", value="⏳ Awaiting payment", inline=True)
    top.add_field(name="Description", value=(product["description"] or "—")[:1000], inline=False)
    if product["image_url"]:
        top.set_thumbnail(url=product["image_url"])

    pay = make_embed("💳 Payment Instructions", None)
    steps = []
    uid = upi_id_for(gid)
    if uid:
        steps.append(
            f"**UPI / Google Pay / PhonePe**\nPay **{fmt_price(ticket['amount'])}** to UPI ID `{uid}` "
            f"(scan the QR below or use any UPI app). Add `Ticket {ticket['id']}` as the note.\n"
            "Then press **Submit Payment Reference** and enter your transaction/UTR number. "
            "Staff will verify it manually."
        )
    if razorpay_ready(gid):
        steps.append("**Razorpay (automatic)**\nPress **Pay with Razorpay** for a secure checkout link. "
                     "Your payment is verified automatically.")
    if not steps:
        steps.append("No payment method is configured yet. Please ask staff for help in this ticket.")
    steps.append("🔒 The private repository link is delivered **only after payment is verified**.")
    pay.description = "\n\n".join(steps)
    if uid:
        pay.add_field(name="UPI link", value=f"`{upi_uri(gid, ticket['amount'], ticket['id'])}`", inline=False)
        qr = qr_image_url(gid, ticket["amount"], ticket["id"])
        if qr:
            pay.set_image(url=qr)
            if upi_qr_for(gid):
                pay.set_footer(text=f"{BRAND} • Enter the exact amount {fmt_price(ticket['amount'])} when paying")
    return [top, pay]


async def start_purchase(interaction: discord.Interaction, product_id: int) -> None:
    guild, member = interaction.guild, interaction.user
    product = get_product(product_id)
    if not product_ready(product):
        await reply(interaction, embed=err_embed("That product is not available for purchase right now."))
        return
    async with bot.locks[(guild.id, member.id, "purchase")]:
        existing = db.one("SELECT * FROM tickets WHERE guild_id=? AND user_id=? AND type='purchase' AND status='open'",
                          (guild.id, member.id))
        if existing:
            ch = guild.get_channel(existing["channel_id"])
            if ch:
                await reply(interaction, embed=warn_embed(
                    f"You already have an open purchase ticket: {ch.mention}\n"
                    "Finish or close it before starting another one.", "Ticket already open"))
                return
            db.run("UPDATE tickets SET status='closed', closed_at=? WHERE id=?", (now_iso(), existing["id"]))
        try:
            channel = await create_ticket_channel(guild, member, "purchase")
        except discord.HTTPException as exc:
            await reply(interaction, embed=err_embed(
                f"I could not create the ticket channel ({exc.text or exc.status}). "
                "Make sure I have **Manage Channels** permission."))
            return
        _, tid = db.run(
            "INSERT INTO tickets (guild_id, channel_id, user_id, type, product_id, product_name, amount, status, created_at) "
            "VALUES (?, ?, ?, 'purchase', ?, ?, ?, 'open', ?)",
            (guild.id, channel.id, member.id, product["id"], product["name"], product["price"], now_iso()))
    ticket = get_ticket(tid)
    msg = await channel.send(
        content=f"{member.mention} {staff_mentions(guild)}".strip(),
        embeds=ticket_embeds(guild, member, ticket, product),
        view=TicketView(mode="open", razorpay=razorpay_ready(guild.id)),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )
    db.run("UPDATE tickets SET control_msg_id=? WHERE id=?", (msg.id, tid))
    await log_activity(guild, "🎫 Purchase ticket opened",
                       f"Ticket #{tid} by {member.mention} for **{product['name']}** ({fmt_price(product['price'])}) → {channel.mention}")
    await reply(interaction, embed=ok_embed(f"Your private purchase ticket is ready: {channel.mention}", "Ticket created"))


async def start_support(interaction: discord.Interaction, subject: str) -> None:
    guild, member = interaction.guild, interaction.user
    async with bot.locks[(guild.id, member.id, "support")]:
        existing = db.one("SELECT * FROM tickets WHERE guild_id=? AND user_id=? AND type='support' AND status='open'",
                          (guild.id, member.id))
        if existing:
            ch = guild.get_channel(existing["channel_id"])
            if ch:
                await reply(interaction, embed=warn_embed(f"You already have an open support ticket: {ch.mention}",
                                                          "Ticket already open"))
                return
            db.run("UPDATE tickets SET status='closed', closed_at=? WHERE id=?", (now_iso(), existing["id"]))
        try:
            channel = await create_ticket_channel(guild, member, "support")
        except discord.HTTPException as exc:
            await reply(interaction, embed=err_embed(f"I could not create the ticket channel ({exc.text or exc.status})."))
            return
        _, tid = db.run(
            "INSERT INTO tickets (guild_id, channel_id, user_id, type, subject, status, created_at) "
            "VALUES (?, ?, ?, 'support', ?, 'open', ?)", (guild.id, channel.id, member.id, subject, now_iso()))
    e = make_embed(f"🛟 Support Ticket #{tid}", f"Hello {member.mention}, staff will be with you shortly.")
    e.add_field(name="Subject", value=subject[:1000], inline=False)
    msg = await channel.send(content=f"{member.mention} {staff_mentions(guild)}".strip(), embed=e,
                             view=TicketView(mode="support"),
                             allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    db.run("UPDATE tickets SET control_msg_id=? WHERE id=?", (msg.id, tid))
    await log_activity(guild, "🛟 Support ticket opened", f"Ticket #{tid} by {member.mention} → {channel.mention}")
    await reply(interaction, embed=ok_embed(f"Your private support ticket is ready: {channel.mention}", "Ticket created"))


async def build_transcript(channel: discord.TextChannel, ticket: dict) -> Optional[discord.File]:
    product = get_product(ticket["product_id"]) if ticket.get("product_id") else None
    secrets = [product["repo_url"]] if product and product["repo_url"] else []
    lines = [f"{BRAND} transcript — ticket #{ticket['id']} ({ticket['type']}) — #{channel.name}", ""]
    try:
        async for m in channel.history(limit=500, oldest_first=True):
            text = m.content or ""
            for e in m.embeds:
                parts = [e.title or "", e.description or ""] + [f"{f.name}: {f.value}" for f in e.fields]
                text += " [embed] " + " | ".join(p for p in parts if p)
            for s in secrets:
                text = text.replace(s, "[REDACTED]")
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M:%S}] {m.author} : {text}")
    except discord.HTTPException:
        return None
    data = "\n".join(lines).encode("utf-8", "replace")
    return discord.File(io.BytesIO(data), filename=f"ticket-{ticket['id']}.txt")


async def close_ticket(channel: discord.TextChannel, closer: discord.abc.User) -> bool:
    ticket = ticket_by_channel(channel.id)
    if not ticket:
        return False
    rc, _ = db.run("UPDATE tickets SET status='closed', closed_at=? WHERE id=? AND status!='closed'",
                   (now_iso(), ticket["id"]))
    if rc != 1:
        return False
    transcript = await build_transcript(channel, ticket)
    await log_activity(channel.guild, "🔒 Ticket closed",
                       f"Ticket #{ticket['id']} ({ticket['type']}) closed by {closer.mention}.", file=transcript)
    try:
        await channel.send(embed=make_embed("🔒 Ticket closing", "This channel will be deleted in 5 seconds.", AMBER))
    except discord.HTTPException:
        pass
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except discord.HTTPException:
        log.warning("Could not delete ticket channel %s", channel.id)
    return True


async def finalize_payment(ticket_id: int, *, verified_by: str, reference: Optional[str] = None,
                           payment_id: Optional[str] = None, method: Optional[str] = None) -> bool:
    """Atomically mark a purchase ticket as paid (only once) and deliver the product."""
    rc, _ = db.run(
        "UPDATE tickets SET status='paid', paid_at=?, verified_by=?, reference=COALESCE(?, reference), "
        "rzp_payment_id=COALESCE(?, rzp_payment_id), method=COALESCE(?, method) "
        "WHERE id=? AND status='open' AND type='purchase'",
        (now_iso(), verified_by, reference, payment_id, method, ticket_id))
    if rc != 1:
        return False
    await deliver_product(ticket_id, first_time=True)
    return True


async def deliver_product(ticket_id: int, *, first_time: bool = False) -> str:
    """Send the private repository link. Only ever called for tickets whose status is 'paid'."""
    t = get_ticket(ticket_id)
    if not t or t["status"] != "paid":
        return "Ticket is not in a paid state."
    guild = bot.get_guild(t["guild_id"])
    channel = guild.get_channel(t["channel_id"]) if guild else None
    product = get_product(t["product_id"]) if t["product_id"] else None
    if not guild or not isinstance(channel, discord.TextChannel):
        return "Ticket channel no longer exists."
    if not product or not product["repo_url"]:
        await channel.send(embed=warn_embed(
            "Payment is verified, but no repository link is set for this product yet. "
            "The owner must run `/setrepo`, then staff can press **Deliver / Resend Link**.", "Delivery pending"),
            content=staff_mentions(guild) or None, allowed_mentions=discord.AllowedMentions(roles=True))
        await log_activity(guild, "⚠️ Delivery pending",
                           f"Ticket #{t['id']} paid but product repository link is missing.", color=AMBER)
        return "Repository link missing."
    e = make_embed(f"🎉 Payment verified — {product['name']}",
                   "Thank you for your purchase! Your private repository is ready.", GREEN)
    e.add_field(name="Private repository", value=product["repo_url"], inline=False)
    e.add_field(name="Keep it private",
                value="Do not share this link. Access is tied to your purchase.", inline=False)
    member = guild.get_member(t["user_id"])
    await channel.send(content=member.mention if member else None, embed=e,
                       allowed_mentions=discord.AllowedMentions(users=True))
    db.run("UPDATE tickets SET delivered=1 WHERE id=?", (t["id"],))
    dm_ok = False
    if member:
        try:
            await member.send(embed=e)
            dm_ok = True
        except discord.HTTPException:
            dm_ok = False
        for key in ("customer_role", "verified_role"):
            rid = get_int(guild.id, key)
            role = guild.get_role(rid) if rid else None
            if role and role not in member.roles and role < guild.me.top_role and not role.managed:
                try:
                    await member.add_roles(role, reason=f"Purchase verified (ticket #{t['id']})")
                except discord.HTTPException:
                    pass
    if t.get("control_msg_id"):
        try:
            msg = await channel.fetch_message(t["control_msg_id"])
            await msg.edit(view=TicketView(mode="paid"))
        except discord.HTTPException:
            pass
    if first_time:
        await log_activity(guild, "💰 Sale completed",
                           f"Ticket #{t['id']} • **{product['name']}** • {fmt_price(t['amount'])} • "
                           f"<@{t['user_id']}> • verified by {t.get('verified_by')}",
                           sales=True, color=GREEN)
    else:
        await log_activity(guild, "📤 Link re-sent", f"Repository link re-sent for ticket #{t['id']}.")
    if not dm_ok:
        await channel.send(embed=make_embed("ℹ️ Tip", "I could not DM you the link (DMs closed). "
                                                      "Save it from this ticket before it is closed.", AMBER))
    return "Delivered."


# ----------------------------------------------------------------------------
# Server setup (/martsetup)
# ----------------------------------------------------------------------------

ROLE_SPECS = [
    # key, name, colour, permissions, hoist
    ("admin_role", "Admin", 0x00A8FF,
     discord.Permissions(manage_channels=True, manage_messages=True, moderate_members=True,
                         kick_members=True, view_audit_log=True), True),
    ("staff_role", "Staff", 0x1E90FF,
     discord.Permissions(manage_messages=True, moderate_members=True), True),
    ("customer_role", "Customer", 0x2B6CB0, discord.Permissions.none(), False),
    ("verified_role", "Verified Customer", 0x00E5FF, discord.Permissions.none(), False),
]

STRUCTURE = [
    ("INFORMATION", "readonly", [("welcome", "welcome_channel", None), ("rules", "rules_channel", None)]),
    ("BOT MART", "readonly", [("bot-catalog", "catalog_channel", None), ("premium-bots", "premium_channel", None),
                              ("prices", "prices_channel", None)]),
    ("SUPPORT", None, [("purchase", "purchase_channel", "readonly"), ("reviews", "reviews_channel", "reviews")]),
    ("STAFF", "staff", [("staff-chat", "staff_chat_channel", None), ("sales-logs", "sales_logs_channel", None),
                        ("activity-logs", "activity_logs_channel", None)]),
]

RULES_TEXT = (
    "1. Be respectful to members and staff.\n"
    "2. No spam, self-promotion or scams.\n"
    "3. Purchases are made **only** through tickets created with the BOT MART panel.\n"
    "4. Staff will never ask for your password or token. Never share your bot token.\n"
    "5. Purchased repositories are private — do not share or resell access.\n"
    "6. Follow Discord's Terms of Service and Community Guidelines."
)


def send_panel_embed() -> discord.Embed:
    e = make_embed(f"🛍️ {BRAND} — Premium Bot Marketplace",
                   "Professional Discord bots with private source access.\n\n"
                   "🛒 **Store** — browse available bots and prices\n"
                   "💳 **Purchase** — pick a bot and open a private purchase ticket\n"
                   "🛟 **Support** — talk to our staff privately\n"
                   "❓ **Help** — how everything works\n\n"
                   "Payments: UPI • Google Pay • PhonePe • UPI QR • Razorpay\n"
                   "🔒 Your repository link is sent only after payment verification.")
    return e


async def send_panel(channel: discord.TextChannel) -> discord.Message:
    return await channel.send(embed=send_panel_embed(), view=PanelView())


def _overwrites(guild: discord.Guild, mode: Optional[str], roles: dict) -> dict:
    po = discord.PermissionOverwrite
    ow = {}
    staffish = [roles.get("admin_role"), roles.get("staff_role")]
    if mode == "readonly":
        ow[guild.default_role] = po(send_messages=False, add_reactions=False, create_public_threads=False,
                                    create_private_threads=False, send_messages_in_threads=False)
    elif mode == "reviews":
        ow[guild.default_role] = po(send_messages=False)
        for key in ("customer_role", "verified_role"):
            if roles.get(key):
                ow[roles[key]] = po(send_messages=True)
    elif mode == "staff":
        ow[guild.default_role] = po(view_channel=False)
        for r in staffish:
            if r:
                ow[r] = po(view_channel=True, send_messages=True, read_message_history=True)
    if ow:
        ow[guild.me] = po(view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                          read_message_history=True, manage_messages=True, manage_channels=True)
    return ow


async def run_setup(guild: discord.Guild) -> list:
    report = []
    roles = {}
    for key, name, colour, perms, hoist in ROLE_SPECS:
        role = discord.utils.find(lambda r, n=name: r.name.lower() == n.lower(), guild.roles)
        if role:
            report.append(f"• Role **{name}** already exists")
        else:
            try:
                try:
                    role = await guild.create_role(name=name, colour=discord.Colour(colour), permissions=perms,
                                                   hoist=hoist, reason="BOT MART setup")
                except discord.Forbidden:  # bot lacks some of the permissions it tried to grant
                    role = await guild.create_role(name=name, colour=discord.Colour(colour), hoist=hoist,
                                                   reason="BOT MART setup")
                report.append(f"✅ Created role **{name}**")
            except discord.HTTPException as exc:
                report.append(f"❌ Could not create role **{name}**: {exc.text or exc.status}")
                role = None
        roles[key] = role
        if role:
            set_setting(guild.id, key, role.id)
    for key in ("admin_role", "staff_role"):  # staff members: authorised, but NOT Administrator
        if roles.get(key):
            db.run("INSERT OR IGNORE INTO staff_roles (guild_id, role_id) VALUES (?, ?)", (guild.id, roles[key].id))
    if roles.get("customer_role") and not get_int(guild.id, "autorole") and get_setting(guild.id, "autorole_disabled") != "1":
        set_setting(guild.id, "autorole", roles["customer_role"].id)

    channels = {}
    for cat_name, cat_mode, chans in STRUCTURE:
        category = discord.utils.find(lambda c, n=cat_name: c.name.lower() == n.lower(), guild.categories)
        if category:
            report.append(f"• Category **{cat_name}** already exists")
        else:
            try:
                ow = _overwrites(guild, cat_mode, roles)
                category = await guild.create_category(cat_name, overwrites=ow, reason="BOT MART setup") if ow \
                    else await guild.create_category(cat_name, reason="BOT MART setup")
                report.append(f"✅ Created category **{cat_name}**")
            except discord.HTTPException as exc:
                report.append(f"❌ Could not create category **{cat_name}**: {exc.text or exc.status}")
                category = None
        if category and cat_name == "SUPPORT":
            set_setting(guild.id, "ticket_category", category.id)
        for ch_name, key, ch_mode in chans:
            ch = discord.utils.find(lambda c, n=ch_name: c.name.lower() == n, guild.text_channels)
            if ch:
                report.append(f"• Channel **#{ch_name}** already exists")
            else:
                try:
                    kw = {"reason": "BOT MART setup"}
                    if category:
                        kw["category"] = category
                    ow = _overwrites(guild, ch_mode, roles)
                    if ow:
                        kw["overwrites"] = ow
                    ch = await guild.create_text_channel(ch_name, **kw)
                    report.append(f"✅ Created channel **#{ch_name}**")
                except discord.HTTPException as exc:
                    report.append(f"❌ Could not create **#{ch_name}**: {exc.text or exc.status}")
                    ch = None
            if ch:
                channels[key] = ch
                set_setting(guild.id, key, ch.id)

    # one-time content (never duplicated)
    purchase = channels.get("purchase_channel")
    if purchase and not get_setting(guild.id, "setup_panel_posted"):
        try:
            await send_panel(purchase)
            set_setting(guild.id, "setup_panel_posted", "1")
            report.append("✅ Posted the store panel in #purchase")
        except discord.HTTPException:
            report.append("⚠️ Could not post the panel in #purchase (check my permissions)")
    rules = channels.get("rules_channel")
    if rules and not get_setting(guild.id, "setup_rules_posted"):
        try:
            await rules.send(embed=make_embed("📜 Server Rules", RULES_TEXT))
            set_setting(guild.id, "setup_rules_posted", "1")
            report.append("✅ Posted rules in #rules")
        except discord.HTTPException:
            pass
    await publish_catalog(guild)
    report.append("✅ Catalogue channels refreshed")
    report.append("ℹ️ Drag my role **above** Customer / Verified Customer so I can assign them.")
    await log_activity(guild, "🛠️ Server setup", "BOT MART server setup was run.")
    return report


# ----------------------------------------------------------------------------
# UI base classes
# ----------------------------------------------------------------------------


async def _report_ui_error(interaction: discord.Interaction, error: Exception) -> None:
    log.error("UI error: %r", error, exc_info=error)
    try:
        await reply(interaction, embed=err_embed("Something went wrong. Please try again."))
    except discord.HTTPException:
        pass


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _report_ui_error(interaction, error)


class DashView(discord.ui.View):
    """Ephemeral dashboard sub-view bound to the invoking user. level = 'owner' or 'staff'."""

    def __init__(self, user_id: int, level: str = "owner", timeout: float = 600):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.level = level

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await reply(interaction, embed=err_embed("This menu belongs to someone else."))
            return False
        ok = is_owner(interaction.user, interaction.guild) if self.level == "owner" else is_staff(interaction.user)
        if not ok:
            await reply(interaction, embed=err_embed("You no longer have permission for this."))
        return ok

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await _report_ui_error(interaction, error)


class ConfirmView(DashView):
    def __init__(self, user_id: int, on_confirm, level: str = "owner"):
        super().__init__(user_id, level, timeout=120)
        self.on_confirm = on_confirm

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        result = await self.on_confirm(interaction)
        await interaction.edit_original_response(embed=result, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(embed=make_embed("Cancelled", "No changes were made."), view=None)


# ----------------------------------------------------------------------------
# Product management UI
# ----------------------------------------------------------------------------


class AddProductModal(SafeModal, title="Add Product"):
    name = discord.ui.TextInput(label="Product name", max_length=60)
    price = discord.ui.TextInput(label="Price (INR)", max_length=12, placeholder="e.g. 999")
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=1000,
                                       required=False)
    image_url = discord.ui.TextInput(label="Image URL (optional)", max_length=500, required=False)
    repo_url = discord.ui.TextInput(label="Private repository URL (https)", max_length=500, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user, interaction.guild):
            return await reply(interaction, embed=err_embed("Only the server owner can add products."))
        try:
            p = add_product(self.name.value, self.price.value, self.description.value,
                            self.image_url.value, self.repo_url.value)
        except ValueError as exc:
            return await reply(interaction, embed=err_embed(str(exc)))
        await log_activity(interaction.guild, "📦 Product added", f"{interaction.user.mention} added **{p['name']}**.")
        schedule_catalog_refresh()
        await reply(interaction, embed=ok_embed(f"Added **{p['name']}** at {fmt_price(p['price'])}.", "Product added"))


class EditProductModal(SafeModal, title="Edit Product"):
    name = discord.ui.TextInput(label="Product name", max_length=60)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=1000,
                                       required=False)
    image_url = discord.ui.TextInput(label="Image URL (blank to remove)", max_length=500, required=False)

    def __init__(self, product: dict):
        super().__init__()
        self.pid = product["id"]
        self.name.default = product["name"]
        self.description.default = (product["description"] or "")[:1000]
        self.image_url.default = product["image_url"] or None

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user, interaction.guild):
            return await reply(interaction, embed=err_embed("Only the server owner can edit products."))
        try:
            msg = apply_product_edit(self.pid, name=self.name.value, description=self.description.value,
                                     image_url=self.image_url.value)
        except ValueError as exc:
            return await reply(interaction, embed=err_embed(str(exc)))
        await log_activity(interaction.guild, "✏️ Product edited", f"{interaction.user.mention}: {msg}")
        schedule_catalog_refresh()
        await reply(interaction, embed=ok_embed(msg, "Product updated"))


class SetPriceModal(SafeModal, title="Set Price"):
    price = discord.ui.TextInput(label="New price (INR)", max_length=12)

    def __init__(self, product: dict):
        super().__init__()
        self.pid = product["id"]
        self.price.default = f"{product['price']:g}" if product["price"] else None

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user, interaction.guild):
            return await reply(interaction, embed=err_embed("Only the server owner can change prices."))
        try:
            msg = apply_product_edit(self.pid, price=self.price.value)
        except ValueError as exc:
            return await reply(interaction, embed=err_embed(str(exc)))
        await log_activity(interaction.guild, "💲 Price changed", f"{interaction.user.mention}: {msg}")
        schedule_catalog_refresh()
        await reply(interaction, embed=ok_embed(msg, "Price updated"))


class SetRepoModal(SafeModal, title="Set Repository Link"):
    repo_url = discord.ui.TextInput(label="Private repository URL (https)", max_length=500)

    def __init__(self, product: dict):
        super().__init__()
        self.pid = product["id"]

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user, interaction.guild):  # repo links are owner-only
            return await reply(interaction, embed=err_embed("Only the server owner can change repository links."))
        try:
            msg = apply_product_edit(self.pid, repo_url=self.repo_url.value)
        except ValueError as exc:
            return await reply(interaction, embed=err_embed(str(exc)))
        await log_activity(interaction.guild, "🔗 Repository link changed",
                           f"{interaction.user.mention} updated a repository link ({msg.split(' ')[0]}). Link not logged.")
        await reply(interaction, embed=ok_embed(msg, "Repository updated"))


def apply_product_edit(pid: int, **fields) -> str:
    p = get_product(pid)
    if not p:
        raise ValueError("That product no longer exists.")
    updates, notes = {}, []
    if fields.get("name") is not None:
        new = clean_name(fields["name"])
        other = find_product(new)
        if other and other["id"] != pid:
            raise ValueError(f"A product named **{new}** already exists.")
        if new != p["name"]:
            updates["name"] = new
            notes.append("name")
    if fields.get("description") is not None:
        updates["description"] = fields["description"].strip()[:1000]
        notes.append("description")
    if fields.get("image_url") is not None:
        updates["image_url"] = clean_url(fields["image_url"], label="Image URL")
        notes.append("image")
    if fields.get("price") is not None:
        updates["price"] = parse_price(fields["price"])
        notes.append("price")
    if fields.get("repo_url") is not None:
        updates["repo_url"] = clean_url(fields["repo_url"], https_only=True, label="Repository URL")
        if not updates["repo_url"]:
            raise ValueError("Repository URL cannot be empty.")
        notes.append("repository link")
    if fields.get("active") is not None:
        updates["active"] = 1 if fields["active"] else 0
        notes.append("status")
    if not updates:
        raise ValueError("Nothing to change.")
    cols = ", ".join(f"{k}=?" for k in updates)
    db.run(f"UPDATE products SET {cols} WHERE id=?", tuple(updates.values()) + (pid,))
    return f"**{updates.get('name', p['name'])}**: updated {', '.join(notes)}."


async def do_remove_product(pid: int, guild: discord.Guild, user: discord.abc.User) -> discord.Embed:
    p = get_product(pid)
    if not p:
        return err_embed("That product no longer exists.")
    db.run("DELETE FROM products WHERE id=?", (pid,))
    await log_activity(guild, "🗑️ Product removed", f"{user.mention} removed **{p['name']}**.")
    schedule_catalog_refresh()
    return ok_embed(f"Removed **{p['name']}**. Past tickets keep their record.", "Product removed")


class ProductPickView(DashView):
    """Dropdown of products; `action` is edit | price | repo | toggle | remove."""

    def __init__(self, user_id: int, action: str):
        super().__init__(user_id, "owner", timeout=180)
        self.action = action
        opts = [discord.SelectOption(label=p["name"][:100], value=str(p["id"]),
                                     description=f"{fmt_price(p['price'])} • {'active' if p['active'] else 'inactive'}")
                for p in list_products()[:25]]
        sel = discord.ui.Select(placeholder="Choose a product…", options=opts)
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        p = get_product(int(self.sel.values[0]))
        if not p:
            return await reply(interaction, embed=err_embed("That product no longer exists."))
        if self.action == "edit":
            await interaction.response.send_modal(EditProductModal(p))
        elif self.action == "price":
            await interaction.response.send_modal(SetPriceModal(p))
        elif self.action == "repo":
            await interaction.response.send_modal(SetRepoModal(p))
        elif self.action == "toggle":
            msg = apply_product_edit(p["id"], active=not p["active"])
            await log_activity(interaction.guild, "🔁 Product status", f"{interaction.user.mention}: {msg}")
            schedule_catalog_refresh()
            await interaction.response.edit_message(embed=ok_embed(
                f"**{p['name']}** is now {'inactive' if p['active'] else 'active'}.", "Status changed"), view=None)
        elif self.action == "remove":
            async def go(i: discord.Interaction):
                return await do_remove_product(p["id"], i.guild, i.user)
            await interaction.response.edit_message(
                embed=warn_embed(f"Really remove **{p['name']}**? This cannot be undone.", "Confirm removal"),
                view=ConfirmView(interaction.user.id, go))


class ProductMgmtView(DashView):
    async def _pick(self, interaction: discord.Interaction, action: str, label: str):
        if not list_products():
            return await reply(interaction, embed=err_embed("There are no products yet."))
        await reply(interaction, embed=make_embed(label, "Select a product below."),
                    view=ProductPickView(interaction.user.id, action))

    @discord.ui.button(label="Add Product", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddProductModal())

    @discord.ui.button(label="Edit Details", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "edit", "✏️ Edit product")

    @discord.ui.button(label="Set Price", emoji="💲", style=discord.ButtonStyle.primary, row=0)
    async def price(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "price", "💲 Set price")

    @discord.ui.button(label="Set Repo Link", emoji="🔗", style=discord.ButtonStyle.primary, row=1)
    async def repo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "repo", "🔗 Set repository link")

    @discord.ui.button(label="Activate / Deactivate", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "toggle", "🔁 Toggle status")

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "remove", "🗑️ Remove product")

    @discord.ui.button(label="Refresh Catalogue Channels", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await publish_catalog(interaction.guild)
        await interaction.followup.send(embed=ok_embed("Catalogue channels refreshed."), ephemeral=True)


# ----------------------------------------------------------------------------
# Payment settings UI
# ----------------------------------------------------------------------------


class PaymentModal(SafeModal, title="UPI Payment Settings"):
    upi = discord.ui.TextInput(label="UPI ID", max_length=80, required=False, placeholder="name@bank")
    qr = discord.ui.TextInput(label="UPI QR image URL (https)", max_length=500, required=False)

    def __init__(self, gid: int):
        super().__init__()
        self.upi.default = upi_id_for(gid) or None
        self.qr.default = upi_qr_for(gid) or None

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user, interaction.guild):
            return await reply(interaction, embed=err_embed("Owner only."))
        upi = (self.upi.value or "").strip()
        if upi and not re.fullmatch(r"[A-Za-z0-9._\-]{2,64}@[A-Za-z0-9.\-]{2,40}", upi):
            return await reply(interaction, embed=err_embed("That does not look like a valid UPI ID (name@bank)."))
        try:
            qr = clean_url(self.qr.value, https_only=True, label="QR image URL")
        except ValueError as exc:
            return await reply(interaction, embed=err_embed(str(exc)))
        set_setting(interaction.guild.id, "upi_id", upi)
        set_setting(interaction.guild.id, "upi_qr_url", qr)
        await log_activity(interaction.guild, "💳 Payment settings", f"{interaction.user.mention} updated UPI settings.")
        await reply(interaction, embed=ok_embed("UPI settings saved.", "Payment settings"))


def payment_embed(gid: int) -> discord.Embed:
    e = make_embed("💳 Payment Settings")
    e.add_field(name="UPI ID", value=f"`{upi_id_for(gid)}`" if upi_id_for(gid) else "❌ Not set", inline=True)
    e.add_field(name="UPI QR", value="✅ Custom image" if upi_qr_for(gid) else
                ("✅ Auto-generated from UPI ID" if upi_id_for(gid) else "❌ Not set"), inline=True)
    keys = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)
    e.add_field(name="Razorpay API keys", value="✅ Configured" if keys else "❌ Missing env vars", inline=True)
    e.add_field(name="Razorpay webhook secret", value="✅ Configured" if RAZORPAY_WEBHOOK_SECRET else "❌ Missing", inline=True)
    e.add_field(name="Razorpay enabled", value="✅ On" if razorpay_ready(gid) else "⚫ Off", inline=True)
    hook = f"https://{PUBLIC_DOMAIN}/razorpay/webhook" if PUBLIC_DOMAIN else "https://<your-railway-domain>/razorpay/webhook"
    e.add_field(name="Webhook URL (event: payment_link.paid)", value=f"`{hook}`", inline=False)
    e.add_field(name="Accepted", value="UPI • Google Pay • PhonePe • UPI QR (manual verify) • Razorpay (automatic)", inline=False)
    e.set_footer(text="API keys and secrets are only ever read from environment variables.")
    return e


class PaymentView(DashView):
    @discord.ui.button(label="Edit UPI / QR", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PaymentModal(interaction.guild.id))

    @discord.ui.button(label="Toggle Razorpay", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        new = "0" if get_setting(gid, "razorpay_enabled", "1") == "1" else "1"
        set_setting(gid, "razorpay_enabled", new)
        await interaction.response.edit_message(embed=payment_embed(gid), view=self)

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=payment_embed(interaction.guild.id), view=self)


# ----------------------------------------------------------------------------
# Ticket UI (purchase / support control buttons)
# ----------------------------------------------------------------------------


class ReferenceModal(SafeModal, title="Submit Payment Reference"):
    app = discord.ui.TextInput(label="Payment app", max_length=30, placeholder="UPI / Google Pay / PhonePe / Other")
    reference = discord.ui.TextInput(label="Transaction ID / UTR", min_length=6, max_length=40)

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        t = get_ticket(self.ticket_id)
        if not t or t["status"] != "open" or t["user_id"] != interaction.user.id:
            return await reply(interaction, embed=err_embed("This ticket cannot accept a payment reference."))
        ref = self.reference.value.strip()
        if not re.fullmatch(r"[A-Za-z0-9\-_]{6,40}", ref):
            return await reply(interaction, embed=err_embed(
                "The reference may only contain letters, numbers, `-` and `_` (6-40 characters)."))
        dup = db.one("SELECT id FROM tickets WHERE reference=? COLLATE NOCASE AND id!=?", (ref, t["id"]))
        if dup:
            return await reply(interaction, embed=err_embed(
                "That reference was already used on another ticket. Contact staff if this is a mistake."))
        method = re.sub(r"[^A-Za-z0-9 ./+-]", "", self.app.value).strip()[:30] or "UPI"
        db.run("UPDATE tickets SET reference=?, method=? WHERE id=?", (ref, method, t["id"]))
        e = make_embed("🧾 Payment reference submitted",
                       f"{interaction.user.mention} submitted a reference. Staff: confirm the payment in your bank/UPI "
                       "app, then press **Verify Payment**.", AMBER)
        e.add_field(name="Reference", value=f"`{ref}`", inline=True)
        e.add_field(name="Via", value=method, inline=True)
        e.add_field(name="Amount due", value=fmt_price(t["amount"]), inline=True)
        await interaction.response.send_message(content=staff_mentions(interaction.guild) or None, embed=e,
                                                allowed_mentions=discord.AllowedMentions(roles=True))
        await log_activity(interaction.guild, "🧾 Payment reference",
                           f"Ticket #{t['id']}: reference submitted by {interaction.user.mention}.")


class TicketView(discord.ui.View):
    """Persistent ticket controls. mode: all (registration) | open | paid | support."""

    def __init__(self, mode: str = "all", razorpay: bool = True):
        super().__init__(timeout=None)
        if mode == "support":
            for b in (self.submit_ref, self.pay_rzp, self.check_rzp, self.verify, self.reject, self.deliver):
                self.remove_item(b)
        elif mode == "paid":
            for b in (self.submit_ref, self.pay_rzp, self.check_rzp, self.verify, self.reject):
                self.remove_item(b)
        elif mode == "open":
            self.remove_item(self.deliver)
        if mode in ("open",) and not razorpay:
            self.remove_item(self.pay_rzp)
            self.remove_item(self.check_rzp)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await _report_ui_error(interaction, error)

    async def _ticket(self, interaction: discord.Interaction) -> Optional[dict]:
        t = ticket_by_channel(interaction.channel_id)
        if not t:
            await reply(interaction, embed=err_embed("This channel is not an active ticket."))
        return t

    @discord.ui.button(label="Submit Payment Reference", emoji="🧾", style=discord.ButtonStyle.primary,
                       custom_id="mart:t:ref", row=0)
    async def submit_ref(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await self._ticket(interaction)
        if not t:
            return
        if t["type"] != "purchase" or t["status"] != "open":
            return await reply(interaction, embed=err_embed("Payment is not pending on this ticket."))
        if t["user_id"] != interaction.user.id:
            return await reply(interaction, embed=err_embed("Only the ticket owner can submit a reference."))
        await interaction.response.send_modal(ReferenceModal(t["id"]))

    @discord.ui.button(label="Pay with Razorpay", emoji="⚡", style=discord.ButtonStyle.success,
                       custom_id="mart:t:rzp", row=0)
    async def pay_rzp(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await self._ticket(interaction)
        if not t:
            return
        if t["user_id"] != interaction.user.id:
            return await reply(interaction, embed=err_embed("Only the ticket owner can create a payment link."))
        if t["status"] != "open" or t["type"] != "purchase":
            return await reply(interaction, embed=err_embed("Payment is not pending on this ticket."))
        if not razorpay_ready(interaction.guild.id):
            return await reply(interaction, embed=err_embed("Razorpay is not enabled right now. Use UPI instead."))
        await interaction.response.defer(ephemeral=True)
        try:
            url = await rzp_get_or_create_link(t, interaction.user)
        except RazorpayError as exc:
            return await interaction.followup.send(embed=err_embed(f"Razorpay error: {exc}"), ephemeral=True)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Open Razorpay checkout", url=url))
        await interaction.followup.send(embed=make_embed(
            "⚡ Razorpay checkout", f"Pay **{fmt_price(t['amount'])}** securely. Verification is automatic; "
            "you can also press **Check Razorpay Payment** afterwards."), view=view, ephemeral=True)

    @discord.ui.button(label="Check Razorpay Payment", emoji="🔍", style=discord.ButtonStyle.secondary,
                       custom_id="mart:t:rzpcheck", row=0)
    async def check_rzp(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await self._ticket(interaction)
        if not t:
            return
        if t["user_id"] != interaction.user.id and not is_staff(interaction.user):
            return await reply(interaction, embed=err_embed("Only the ticket owner or staff can check this."))
        if t["status"] != "open":
            return await reply(interaction, embed=warn_embed("This ticket is no longer awaiting payment."))
        await interaction.response.defer(ephemeral=True)
        try:
            paid, msg = await rzp_check_ticket(t)
        except RazorpayError as exc:
            return await interaction.followup.send(embed=err_embed(f"Razorpay error: {exc}"), ephemeral=True)
        await interaction.followup.send(embed=(ok_embed(msg) if paid else warn_embed(msg, "Not paid yet")),
                                        ephemeral=True)

    @discord.ui.button(label="Verify Payment", emoji="✅", style=discord.ButtonStyle.success,
                       custom_id="mart:t:verify", row=1)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction):
            return
        t = await self._ticket(interaction)
        if not t:
            return
        if t["type"] != "purchase" or t["status"] != "open":
            return await reply(interaction, embed=err_embed("This ticket is not awaiting payment verification."))
        if not t["reference"]:
            return await reply(interaction, embed=err_embed(
                "The customer has not submitted a payment reference yet. For Razorpay payments, the bot verifies "
                "automatically."))
        product = get_product(t["product_id"]) if t["product_id"] else None
        if not product or not product["repo_url"]:
            return await reply(interaction, embed=err_embed(
                "This product has no repository link set. Ask the owner to run `/setrepo` first."))
        tid, staff_name = t["id"], str(interaction.user)

        async def go(i: discord.Interaction):
            done = await finalize_payment(tid, verified_by=f"{staff_name} ({i.user.id})")
            return ok_embed("Payment verified and the repository link was delivered.") if done \
                else warn_embed("This ticket was already processed or closed.")

        await reply(interaction, embed=warn_embed(
            f"Confirm that **{fmt_price(t['amount'])}** with reference `{t['reference']}` has actually been received. "
            "The repository link will be released immediately.", "Confirm payment"),
            view=ConfirmView(interaction.user.id, go, level="staff"))

    @discord.ui.button(label="Reject Payment", emoji="❌", style=discord.ButtonStyle.danger,
                       custom_id="mart:t:reject", row=1)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction):
            return
        t = await self._ticket(interaction)
        if not t:
            return
        if t["type"] != "purchase" or t["status"] != "open":
            return await reply(interaction, embed=err_embed("Nothing to reject on this ticket."))
        if not t["reference"]:
            return await reply(interaction, embed=err_embed("No payment reference has been submitted."))
        db.run("UPDATE tickets SET reference=NULL WHERE id=? AND status='open'", (t["id"],))
        await interaction.response.send_message(
            content=f"<@{t['user_id']}>",
            embed=make_embed("❌ Payment not confirmed",
                             "Staff could not confirm that payment. Please check the transaction ID and submit it "
                             "again, or contact staff here.", RED),
            allowed_mentions=discord.AllowedMentions(users=True))
        await log_activity(interaction.guild, "❌ Payment rejected",
                           f"Ticket #{t['id']} reference rejected by {interaction.user.mention}.", color=RED)

    @discord.ui.button(label="Deliver / Resend Link", emoji="📤", style=discord.ButtonStyle.primary,
                       custom_id="mart:t:deliver", row=1)
    async def deliver(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction):
            return
        t = await self._ticket(interaction)
        if not t:
            return
        if t["status"] != "paid":
            return await reply(interaction, embed=err_embed("The link can only be sent for verified (paid) tickets."))
        await interaction.response.defer(ephemeral=True)
        result = await deliver_product(t["id"])
        await interaction.followup.send(embed=make_embed("📤 Delivery", result), ephemeral=True)

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.secondary,
                       custom_id="mart:t:close", row=1)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await self._ticket(interaction)
        if not t:
            return
        if t["user_id"] != interaction.user.id and not is_staff(interaction.user):
            return await reply(interaction, embed=err_embed("Only the ticket owner or staff can close this ticket."))
        await interaction.response.send_message(embed=make_embed("Closing…", "Saving transcript.", AMBER))
        await close_ticket(interaction.channel, interaction.user)


class CloseTicketSelect(DashView):
    def __init__(self, user_id: int, guild: discord.Guild):
        super().__init__(user_id, "staff", timeout=180)
        rows = db.all("SELECT * FROM tickets WHERE guild_id=? AND status!='closed' ORDER BY id DESC LIMIT 25",
                      (guild.id,))
        opts = []
        for t in rows:
            ch = guild.get_channel(t["channel_id"])
            opts.append(discord.SelectOption(label=f"#{t['id']} {t['type']} — {(ch.name if ch else 'missing')}"[:100],
                                             value=str(t["id"]), description=f"status: {t['status']}"))
        sel = discord.ui.Select(placeholder="Pick a ticket to close…", options=opts or
                                [discord.SelectOption(label="No open tickets", value="0")])
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        tid = int(self.sel.values[0])
        t = get_ticket(tid) if tid else None
        if not t or t["status"] == "closed":
            return await interaction.response.edit_message(embed=err_embed("No such open ticket."), view=None)
        ch = interaction.guild.get_channel(t["channel_id"])
        await interaction.response.edit_message(embed=make_embed("Closing ticket…", f"Ticket #{tid}", AMBER), view=None)
        if isinstance(ch, discord.TextChannel):
            await close_ticket(ch, interaction.user)
        else:
            db.run("UPDATE tickets SET status='closed', closed_at=? WHERE id=?", (now_iso(), tid))


def ticket_stats_embed(guild: discord.Guild) -> discord.Embed:
    gid = guild.id

    def count(where: str) -> int:
        return db.one(f"SELECT COUNT(*) AS c FROM tickets WHERE guild_id=? AND {where}", (gid,))["c"]

    e = make_embed("🎫 Ticket Management")
    e.add_field(name="Open purchase tickets", value=str(count("type='purchase' AND status='open'")), inline=True)
    e.add_field(name="Paid (open)", value=str(count("status='paid'")), inline=True)
    e.add_field(name="Open support tickets", value=str(count("type='support' AND status='open'")), inline=True)
    e.add_field(name="Total closed", value=str(count("status='closed'")), inline=True)
    cat = guild.get_channel(get_int(gid, "ticket_category") or 0)
    e.add_field(name="Ticket category", value=cat.mention if cat else "Auto (SUPPORT)", inline=True)
    rows = db.all("SELECT * FROM tickets WHERE guild_id=? AND status!='closed' ORDER BY id DESC LIMIT 10", (gid,))
    lines = []
    for t in rows:
        ch = guild.get_channel(t["channel_id"])
        lines.append(f"#{t['id']} • {t['type']} • {t['status']} • {ch.mention if ch else 'deleted'} • <@{t['user_id']}>")
    e.add_field(name="Latest open tickets", value="\n".join(lines) or "None", inline=False)
    return e


class TicketMgmtView(DashView):
    def __init__(self, user_id: int):
        super().__init__(user_id, "staff")

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=ticket_stats_embed(interaction.guild), view=self)

    @discord.ui.button(label="Close a Ticket", emoji="🔒", style=discord.ButtonStyle.danger, row=0)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(interaction, embed=make_embed("Close a ticket", "Select the ticket to close."),
                    view=CloseTicketSelect(interaction.user.id, interaction.guild))

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.category],
                       placeholder="Owner: set the category for new tickets", row=1)
    async def set_category(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if not await require_owner(interaction):
            return
        set_setting(interaction.guild.id, "ticket_category", select.values[0].id)
        await interaction.response.edit_message(embed=ticket_stats_embed(interaction.guild), view=self)


# ----------------------------------------------------------------------------
# Staff management UI
# ----------------------------------------------------------------------------


def staff_embed(guild: discord.Guild) -> discord.Embed:
    roles = [guild.get_role(r) for r in staff_role_ids(guild.id)]
    e = make_embed("👥 Staff Management")
    e.description = ("**Authorized staff roles:**\n" + ("\n".join(r.mention for r in roles if r) or "None configured")
                     + "\n\nMembers with these roles can verify payments, manage tickets, view activity logs and use "
                       "`/martpanel`. Staff roles **do not** receive Administrator permission.")
    return e


class RemoveStaffSelect(DashView):
    def __init__(self, user_id: int, guild: discord.Guild):
        super().__init__(user_id, "owner", timeout=180)
        opts = []
        for rid in staff_role_ids(guild.id):
            r = guild.get_role(rid)
            if r:
                opts.append(discord.SelectOption(label=r.name[:100], value=str(r.id)))
        sel = discord.ui.Select(placeholder="Role to remove…", options=opts[:25] or
                                [discord.SelectOption(label="No staff roles", value="0")])
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        rid = int(self.sel.values[0])
        if rid:
            db.run("DELETE FROM staff_roles WHERE guild_id=? AND role_id=?", (interaction.guild.id, rid))
            await log_activity(interaction.guild, "👥 Staff role removed", f"{interaction.user.mention} removed <@&{rid}>.")
        await interaction.response.edit_message(embed=staff_embed(interaction.guild), view=None)


class StaffMgmtView(DashView):
    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Add staff role(s)…", min_values=1, max_values=5, row=0)
    async def add(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        added = []
        for role in select.values:
            if role.is_default() or role.managed:
                continue
            db.run("INSERT OR IGNORE INTO staff_roles (guild_id, role_id) VALUES (?, ?)", (interaction.guild.id, role.id))
            added.append(role.mention)
        if added:
            await log_activity(interaction.guild, "👥 Staff role added", f"{interaction.user.mention} added {', '.join(added)}.")
        await interaction.response.edit_message(embed=staff_embed(interaction.guild), view=self)

    @discord.ui.button(label="Remove a staff role", emoji="➖", style=discord.ButtonStyle.danger, row=1)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(interaction, embed=make_embed("Remove staff role"), view=RemoveStaffSelect(interaction.user.id, interaction.guild))

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=staff_embed(interaction.guild), view=self)


# ----------------------------------------------------------------------------
# Welcome settings UI
# ----------------------------------------------------------------------------

DEFAULT_WELCOME = "Welcome {user} to **{server}**! You are member #{count}. Head to the purchase channel to browse our bots."


def render_welcome(template: str, member: discord.Member) -> str:
    return (template.replace("{user}", member.mention).replace("{server}", member.guild.name)
            .replace("{count}", str(member.guild.member_count or 0)))[:2000]


def welcome_embed(guild: discord.Guild) -> discord.Embed:
    ch = guild.get_channel(get_int(guild.id, "welcome_channel") or 0)
    e = make_embed("👋 Welcome Settings")
    e.add_field(name="Enabled", value="✅ On" if get_setting(guild.id, "welcome_enabled", "1") == "1" else "⚫ Off", inline=True)
    e.add_field(name="Channel", value=ch.mention if ch else "❌ Not set", inline=True)
    e.add_field(name="Message", value=get_setting(guild.id, "welcome_message", DEFAULT_WELCOME)[:1000], inline=False)
    e.set_footer(text="Placeholders: {user} {server} {count}")
    return e


class WelcomeMessageModal(SafeModal, title="Welcome Message"):
    message = discord.ui.TextInput(label="Message ({user} {server} {count})", style=discord.TextStyle.paragraph,
                                   max_length=1000)

    def __init__(self, gid: int):
        super().__init__()
        self.message.default = get_setting(gid, "welcome_message", DEFAULT_WELCOME)[:1000]

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user, interaction.guild):
            return await reply(interaction, embed=err_embed("Owner only."))
        set_setting(interaction.guild.id, "welcome_message", self.message.value.strip())
        await reply(interaction, embed=ok_embed("Welcome message saved.", "Welcome settings"))


class WelcomeView(DashView):
    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Choose the welcome channel…", row=0)
    async def channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        set_setting(interaction.guild.id, "welcome_channel", select.values[0].id)
        await interaction.response.edit_message(embed=welcome_embed(interaction.guild), view=self)

    @discord.ui.button(label="Edit Message", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WelcomeMessageModal(interaction.guild.id))

    @discord.ui.button(label="Toggle On/Off", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        set_setting(gid, "welcome_enabled", "0" if get_setting(gid, "welcome_enabled", "1") == "1" else "1")
        await interaction.response.edit_message(embed=welcome_embed(interaction.guild), view=self)

    @discord.ui.button(label="Send Test", emoji="📨", style=discord.ButtonStyle.success, row=1)
    async def test(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = interaction.guild.get_channel(get_int(interaction.guild.id, "welcome_channel") or 0)
        if not isinstance(ch, discord.TextChannel):
            return await reply(interaction, embed=err_embed("Set a welcome channel first."))
        try:
            await ch.send(embed=build_welcome(interaction.user), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return await reply(interaction, embed=err_embed("I cannot send messages in that channel."))
        await reply(interaction, embed=ok_embed(f"Test welcome sent to {ch.mention}."))


def build_welcome(member: discord.Member) -> discord.Embed:
    template = get_setting(member.guild.id, "welcome_message", DEFAULT_WELCOME)
    e = make_embed(f"Welcome to {member.guild.name}!", render_welcome(template, member))
    e.set_thumbnail(url=member.display_avatar.url)
    return e


# ----------------------------------------------------------------------------
# Auto role UI
# ----------------------------------------------------------------------------

DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "kick_members",
             "ban_members", "moderate_members", "manage_messages", "mention_everyone", "manage_nicknames")


def role_is_dangerous(role: discord.Role) -> Optional[str]:
    for name in DANGEROUS:
        if getattr(role.permissions, name, False):
            return name.replace("_", " ")
    return None


def autorole_embed(guild: discord.Guild) -> discord.Embed:
    rid = get_int(guild.id, "autorole")
    role = guild.get_role(rid) if rid else None
    e = make_embed("🏷️ Auto Role")
    e.description = (f"New members receive: {role.mention if role else '**nothing (disabled)**'}\n\n"
                     "The role must be below my highest role and must not carry moderation/admin permissions.")
    return e


class AutoRoleView(DashView):
    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Choose the auto-role…", row=0)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        guild = interaction.guild
        if role.is_default() or role.managed:
            return await reply(interaction, embed=err_embed("That role cannot be assigned."))
        if role >= guild.me.top_role:
            return await reply(interaction, embed=err_embed("That role is above my highest role. Move my role higher."))
        bad = role_is_dangerous(role)
        if bad:
            return await reply(interaction, embed=err_embed(f"For safety, roles with **{bad}** permission cannot be auto-roles."))
        set_setting(guild.id, "autorole", role.id)
        del_setting(guild.id, "autorole_disabled")
        await log_activity(guild, "🏷️ Auto-role changed", f"{interaction.user.mention} set the auto-role to {role.mention}.")
        await interaction.response.edit_message(embed=autorole_embed(guild), view=self)

    @discord.ui.button(label="Use Customer role", emoji="🧑", style=discord.ButtonStyle.primary, row=1)
    async def customer(self, interaction: discord.Interaction, button: discord.ui.Button):
        rid = get_int(interaction.guild.id, "customer_role")
        if not rid or not interaction.guild.get_role(rid):
            return await reply(interaction, embed=err_embed("No Customer role yet. Run Server Setup first."))
        set_setting(interaction.guild.id, "autorole", rid)
        del_setting(interaction.guild.id, "autorole_disabled")
        await interaction.response.edit_message(embed=autorole_embed(interaction.guild), view=self)

    @discord.ui.button(label="Disable", emoji="⛔", style=discord.ButtonStyle.danger, row=1)
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        del_setting(interaction.guild.id, "autorole")
        set_setting(interaction.guild.id, "autorole_disabled", "1")
        await interaction.response.edit_message(embed=autorole_embed(interaction.guild), view=self)


# ----------------------------------------------------------------------------
# Moderation settings UI
# ----------------------------------------------------------------------------


def moderation_embed(guild: discord.Guild) -> discord.Embed:
    gid = guild.id
    ch = guild.get_channel(get_int(gid, "mod_log_channel") or 0)
    e = make_embed("🛡️ Moderation")
    e.add_field(name="Anti-spam", value="✅ On" if get_setting(gid, "antispam_enabled", "1") == "1" else "⚫ Off", inline=True)
    e.add_field(name="Message burst", value=f"{get_setting(gid, 'spam_limit', '6')} msgs / {get_setting(gid, 'spam_window', '5')}s", inline=True)
    e.add_field(name="Repeat limit", value=f"{get_setting(gid, 'repeat_limit', '4')} identical msgs / 30s", inline=True)
    e.add_field(name="Auto-timeout", value=f"{get_setting(gid, 'spam_timeout', '5')} min", inline=True)
    e.add_field(name="Mod log channel", value=ch.mention if ch else "Uses activity-logs", inline=True)
    e.add_field(name="Commands", value="`/timeout` • `/clear`\nStaff and members with Manage Messages are exempt from anti-spam.", inline=False)
    return e


class SpamModal(SafeModal, title="Anti-spam Settings"):
    limit = discord.ui.TextInput(label="Messages allowed per window (3-20)", max_length=2)
    window = discord.ui.TextInput(label="Window in seconds (2-30)", max_length=2)
    repeat = discord.ui.TextInput(label="Identical messages allowed in 30s (2-10)", max_length=2)
    timeout_min = discord.ui.TextInput(label="Auto-timeout minutes (1-60)", max_length=2)

    def __init__(self, gid: int):
        super().__init__()
        self.limit.default = get_setting(gid, "spam_limit", "6")
        self.window.default = get_setting(gid, "spam_window", "5")
        self.repeat.default = get_setting(gid, "repeat_limit", "4")
        self.timeout_min.default = get_setting(gid, "spam_timeout", "5")

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user, interaction.guild):
            return await reply(interaction, embed=err_embed("Owner only."))
        try:
            vals = {"spam_limit": (self.limit.value, 3, 20), "spam_window": (self.window.value, 2, 30),
                    "repeat_limit": (self.repeat.value, 2, 10), "spam_timeout": (self.timeout_min.value, 1, 60)}
            clean = {}
            for k, (raw, lo, hi) in vals.items():
                n = int(raw.strip())
                if not lo <= n <= hi:
                    raise ValueError
                clean[k] = n
        except ValueError:
            return await reply(interaction, embed=err_embed("Every value must be a whole number inside its range."))
        for k, n in clean.items():
            set_setting(interaction.guild.id, k, n)
        await reply(interaction, embed=ok_embed("Anti-spam settings saved.", "Moderation"))


class ModerationView(DashView):
    @discord.ui.button(label="Toggle Anti-spam", emoji="🔁", style=discord.ButtonStyle.primary, row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        set_setting(gid, "antispam_enabled", "0" if get_setting(gid, "antispam_enabled", "1") == "1" else "1")
        await interaction.response.edit_message(embed=moderation_embed(interaction.guild), view=self)

    @discord.ui.button(label="Spam Settings", emoji="⚙️", style=discord.ButtonStyle.secondary, row=0)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SpamModal(interaction.guild.id))

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Set the moderation log channel…", row=1)
    async def modlog(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        set_setting(interaction.guild.id, "mod_log_channel", select.values[0].id)
        await interaction.response.edit_message(embed=moderation_embed(interaction.guild), view=self)


# ----------------------------------------------------------------------------
# Activity logs UI
# ----------------------------------------------------------------------------


def logs_embed(guild: discord.Guild) -> discord.Embed:
    rows = db.all("SELECT * FROM logs WHERE guild_id=? ORDER BY id DESC LIMIT 15", (guild.id,))
    act = guild.get_channel(get_int(guild.id, "activity_logs_channel") or 0)
    sales = guild.get_channel(get_int(guild.id, "sales_logs_channel") or 0)
    e = make_embed("📜 Activity Logs")
    text = "\n".join(f"`{r['created_at'][5:16]}` **{r['kind']}** — {r['message'][:90]}" for r in rows) or "No activity yet."
    e.description = text[:3800]
    e.add_field(name="Activity channel", value=act.mention if act else "❌ Not set", inline=True)
    e.add_field(name="Sales channel", value=sales.mention if sales else "❌ Not set", inline=True)
    return e


class LogsView(DashView):
    def __init__(self, user_id: int):
        super().__init__(user_id, "staff")

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=logs_embed(interaction.guild), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Owner: set the activity log channel", row=1)
    async def set_activity(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if not await require_owner(interaction):
            return
        set_setting(interaction.guild.id, "activity_logs_channel", select.values[0].id)
        await interaction.response.edit_message(embed=logs_embed(interaction.guild), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Owner: set the sales log channel", row=2)
    async def set_sales(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if not await require_owner(interaction):
            return
        set_setting(interaction.guild.id, "sales_logs_channel", select.values[0].id)
        await interaction.response.edit_message(embed=logs_embed(interaction.guild), view=self)


# ----------------------------------------------------------------------------
# Bot profile (owner only)
# ----------------------------------------------------------------------------

STATUS_MAP = {"online": discord.Status.online, "idle": discord.Status.idle,
              "dnd": discord.Status.dnd, "invisible": discord.Status.invisible}
ACTIVITY_MAP = {"playing": discord.ActivityType.playing, "watching": discord.ActivityType.watching,
                "listening": discord.ActivityType.listening, "competing": discord.ActivityType.competing}


async def apply_presence() -> None:
    status = STATUS_MAP.get(get_setting(0, "bot_status", "online"), discord.Status.online)
    text = get_setting(0, "bot_activity", "BOT MART | /help")
    atype = ACTIVITY_MAP.get(get_setting(0, "bot_activity_type", "watching"), discord.ActivityType.watching)
    activity = discord.Activity(type=atype, name=text) if text else None
    await bot.change_presence(status=status, activity=activity)


async def fetch_image(url: str) -> bytes:
    if not is_safe_public_url(url):
        raise ValueError("The image URL must be a public https link.")
    limit = 8 * 1024 * 1024
    try:
        async with bot.http_session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise ValueError(f"Could not download the image (HTTP {r.status}).")
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                raise ValueError("The link must point to a PNG, JPG, GIF or WEBP image.")
            data = await r.content.read(limit + 1)
    except aiohttp.ClientError as exc:
        raise ValueError(f"Could not download the image: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise ValueError("Downloading the image timed out.") from exc
    if len(data) > limit:
        raise ValueError("The image is larger than 8 MB.")
    return data


async def set_avatar_bytes(data: bytes) -> None:
    try:
        await bot.user.edit(avatar=data)
    except discord.HTTPException as exc:
        raise ValueError(f"Discord rejected the avatar change: {exc.text or exc.status}. "
                         "Avatar changes are rate limited — try again later.") from exc


def profile_embed() -> discord.Embed:
    e = make_embed("🤖 Bot Profile")
    e.add_field(name="Status", value=get_setting(0, "bot_status", "online"), inline=True)
    e.add_field(name="Activity", value=f"{get_setting(0, 'bot_activity_type', 'watching')} {get_setting(0, 'bot_activity', 'BOT MART | /help')}", inline=True)
    e.add_field(name="Not available", value="Discord does not let bots edit their bio or banner through the API, "
                                            "so those are not offered. Change them in the Developer Portal.", inline=False)
    if bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    return e


class ActivityModal(SafeModal, title="Bot Activity"):
    text = discord.ui.TextInput(label="Activity text", max_length=100)
    kind = discord.ui.TextInput(label="Type: playing / watching / listening / competing", max_length=12)

    def __init__(self):
        super().__init__()
        self.text.default = get_setting(0, "bot_activity", "BOT MART | /help")
        self.kind.default = get_setting(0, "bot_activity_type", "watching")

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user, interaction.guild):
            return await reply(interaction, embed=err_embed("Owner only."))
        kind = self.kind.value.strip().lower()
        if kind not in ACTIVITY_MAP:
            return await reply(interaction, embed=err_embed("Type must be playing, watching, listening or competing."))
        set_setting(0, "bot_activity", self.text.value.strip())
        set_setting(0, "bot_activity_type", kind)
        await apply_presence()
        await reply(interaction, embed=ok_embed("Activity updated.", "Bot profile"))


class AvatarModal(SafeModal, title="Bot Avatar"):
    url = discord.ui.TextInput(label="Image URL (https, PNG/JPG/GIF/WEBP, max 8 MB)", max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user, interaction.guild):
            return await reply(interaction, embed=err_embed("Owner only."))
        await interaction.response.defer(ephemeral=True)
        try:
            await set_avatar_bytes(await fetch_image(self.url.value.strip()))
        except ValueError as exc:
            return await interaction.followup.send(embed=err_embed(str(exc)), ephemeral=True)
        await interaction.followup.send(embed=ok_embed("Avatar updated.", "Bot profile"), ephemeral=True)


class ProfileView(DashView):
    @discord.ui.select(placeholder="Set online status…", row=0, options=[
        discord.SelectOption(label="Online", value="online", emoji="🟢"),
        discord.SelectOption(label="Idle", value="idle", emoji="🌙"),
        discord.SelectOption(label="Do Not Disturb", value="dnd", emoji="⛔"),
        discord.SelectOption(label="Invisible", value="invisible", emoji="⚫"),
    ])
    async def status(self, interaction: discord.Interaction, select: discord.ui.Select):
        set_setting(0, "bot_status", select.values[0])
        await apply_presence()
        await interaction.response.edit_message(embed=profile_embed(), view=self)

    @discord.ui.button(label="Change Activity", emoji="💬", style=discord.ButtonStyle.primary, row=1)
    async def activity(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ActivityModal())

    @discord.ui.button(label="Change Avatar", emoji="🖼️", style=discord.ButtonStyle.primary, row=1)
    async def avatar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AvatarModal())


# ----------------------------------------------------------------------------
# Help
# ----------------------------------------------------------------------------

HELP_PAGES = {
    "owner": ("👑 Owner commands",
              "`/martsetup` — create categories, channels and roles (no duplicates)\n"
              "`/martpanel` — management dashboard\n"
              "`/addproduct` `/editproduct` `/removeproduct` `/setprice` `/setrepo` `/products`\n"
              "`/staffrole` — add / remove / list staff roles\n"
              "`/setavatar` `/setstatus` `/setactivity` — bot profile\n"
              "Only the server owner can change repository links."),
    "staff": ("🛡️ Staff commands",
              "`/sendpanel` — publish the store panel in a channel\n"
              "`/martpanel` — tickets, activity logs and help\n"
              "`/timeout` — time out a member (role-hierarchy checked)\n"
              "`/clear` — bulk delete recent messages\n"
              "In tickets: **Verify Payment**, **Reject Payment**, **Deliver / Resend Link**, **Close Ticket**."),
    "customer": ("🛒 Customer commands",
                 "Use the panel buttons: **Store** (browse), **Purchase** (choose a bot from the dropdown and open a "
                 "private ticket), **Support** (private help) and **Help**.\n`/help` — this menu"),
    "payment": ("💳 Payment information",
                "Pay by **UPI**, **Google Pay**, **PhonePe** or **UPI QR** to the UPI ID shown in your ticket, then "
                "submit your transaction ID. Staff verify it manually.\n"
                "Or use **Razorpay** for automatic verification.\n"
                "Your repository link is sent **only after** payment is verified."),
    "tickets": ("🎫 Ticket information",
                "Each ticket is a private channel visible only to you and staff. You can have one open purchase "
                "ticket at a time. Use **Close Ticket** when done — a transcript is saved for staff."),
    "panel": ("🧭 Management panel",
              "`/martpanel` opens the dashboard: Server Setup, Product Management, Payment Settings, Ticket "
              "Management, Staff Management, Welcome Settings, Auto Role, Moderation, Activity Logs, Bot Profile and "
              "Help. Every button performs a real action; sensitive sections are owner-only."),
}


def help_embed(key: str) -> discord.Embed:
    title, body = HELP_PAGES[key]
    return make_embed(title, body)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        sel = discord.ui.Select(placeholder="Choose a help topic…", options=[
            discord.SelectOption(label="Owner commands", value="owner", emoji="👑"),
            discord.SelectOption(label="Staff commands", value="staff", emoji="🛡️"),
            discord.SelectOption(label="Customer commands", value="customer", emoji="🛒"),
            discord.SelectOption(label="Payment information", value="payment", emoji="💳"),
            discord.SelectOption(label="Ticket information", value="tickets", emoji="🎫"),
            discord.SelectOption(label="Management panel", value="panel", emoji="🧭"),
        ])
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=help_embed(self.sel.values[0]), view=self)


def help_intro_embed() -> discord.Embed:
    return make_embed(f"❓ {BRAND} Help", "Pick a topic from the dropdown below.")


# ----------------------------------------------------------------------------
# Public panel (Store / Purchase / Support / Help)
# ----------------------------------------------------------------------------


class SupportModal(SafeModal, title="Open Support Ticket"):
    subject = discord.ui.TextInput(label="How can we help?", style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await start_support(interaction, self.subject.value.strip())


class PurchaseSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        items = [p for p in list_products(active_only=True) if product_ready(p)][:25]
        opts = [discord.SelectOption(label=p["name"][:100], value=str(p["id"]),
                                     description=f"{fmt_price(p['price'])} — {(p['description'] or '')[:70]}")
                for p in items]
        sel = discord.ui.Select(placeholder="Select a bot to purchase…", options=opts)
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await reply(interaction, embed=err_embed("This menu belongs to someone else."))
            return False
        return True

    async def _picked(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await start_purchase(interaction, int(self.sel.values[0]))


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await _report_ui_error(interaction, error)

    @discord.ui.button(label="Store", emoji="🛒", style=discord.ButtonStyle.primary, custom_id="mart:panel:store")
    async def store(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = list_products(active_only=True)
        if not items:
            return await reply(interaction, embed=make_embed("🛒 Store", "No bots are listed yet. Check back soon!"))
        cards = [product_card(p) for p in items[:10]]
        await reply(interaction, embeds=cards)

    @discord.ui.button(label="Purchase", emoji="💳", style=discord.ButtonStyle.success, custom_id="mart:panel:purchase")
    async def purchase(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(product_ready(p) for p in list_products(active_only=True)):
            return await reply(interaction, embed=warn_embed("No bot is available for purchase right now.", "Store"))
        await reply(interaction, embed=make_embed("💳 Purchase", "Choose the bot you want from the dropdown. I will show the "
                                                                 "price and open a private ticket for payment."),
                    view=PurchaseSelectView(interaction.user.id))

    @discord.ui.button(label="Support", emoji="🛟", style=discord.ButtonStyle.secondary, custom_id="mart:panel:support")
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SupportModal())

    @discord.ui.button(label="Help", emoji="❓", style=discord.ButtonStyle.secondary, custom_id="mart:panel:help")
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(interaction, embed=help_intro_embed(), view=HelpView())


# ----------------------------------------------------------------------------
# Management dashboard (/martpanel)
# ----------------------------------------------------------------------------

DASH_BUTTONS = [
    ("setup", "Server Setup", "🛠️", 0), ("products", "Product Management", "📦", 0),
    ("payments", "Payment Settings", "💳", 0), ("tickets", "Ticket Management", "🎫", 0),
    ("staff", "Staff Management", "👥", 1), ("welcome", "Welcome Settings", "👋", 1),
    ("autorole", "Auto Role", "🏷️", 1), ("moderation", "Moderation", "🛡️", 1),
    ("logs", "Activity Logs", "📜", 2), ("profile", "Bot Profile", "🤖", 2), ("help", "Help Menu", "❓", 2),
]
STAFF_SECTIONS = {"tickets", "logs", "help"}


def dashboard_embed(guild: discord.Guild) -> discord.Embed:
    gid = guild.id
    open_t = db.one("SELECT COUNT(*) AS c FROM tickets WHERE guild_id=? AND status!='closed'", (gid,))["c"]
    e = make_embed(f"⚙️ {BRAND} — Management Dashboard",
                   "Every button below performs a real action. Owner-only sections are marked 👑.")
    e.add_field(name="Products", value=f"{len(list_products())} total • {len(list_products(True))} active", inline=True)
    e.add_field(name="Open tickets", value=str(open_t), inline=True)
    e.add_field(name="Staff roles", value=str(len(staff_role_ids(gid))), inline=True)
    e.add_field(name="Payments", value=("UPI ✅" if upi_id_for(gid) else "UPI ❌") + " • " +
                ("Razorpay ✅" if razorpay_ready(gid) else "Razorpay ⚫"), inline=True)
    e.add_field(name="Sections", value="👑 Setup, Products, Payments, Staff, Welcome, Auto Role, Moderation, Profile\n"
                                       "🛡️ Tickets, Activity Logs, Help (staff)", inline=False)
    return e


class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for key, label, emoji, row in DASH_BUTTONS:
            style = discord.ButtonStyle.secondary if key in STAFF_SECTIONS else discord.ButtonStyle.primary
            btn = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"mart:dash:{key}", row=row)
            btn.callback = self._make_callback(key)
            self.add_item(btn)

    def _make_callback(self, key: str):
        async def callback(interaction: discord.Interaction):
            await dashboard_dispatch(interaction, key)
        return callback

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await _report_ui_error(interaction, error)


async def dashboard_dispatch(interaction: discord.Interaction, key: str) -> None:
    if interaction.guild is None:
        return await reply(interaction, embed=err_embed("Use this inside a server."))
    ok = await (require_staff(interaction) if key in STAFF_SECTIONS else require_owner(interaction))
    if not ok:
        return
    guild, uid = interaction.guild, interaction.user.id
    if key == "setup":
        await interaction.response.defer(ephemeral=True)
        report = await run_setup(guild)
        return await interaction.followup.send(embed=make_embed("🛠️ Server Setup", "\n".join(report)[:4000]), ephemeral=True)
    if key == "products":
        return await reply(interaction, embed=products_overview_embed(), view=ProductMgmtView(uid))
    if key == "payments":
        return await reply(interaction, embed=payment_embed(guild.id), view=PaymentView(uid))
    if key == "tickets":
        return await reply(interaction, embed=ticket_stats_embed(guild), view=TicketMgmtView(uid))
    if key == "staff":
        return await reply(interaction, embed=staff_embed(guild), view=StaffMgmtView(uid))
    if key == "welcome":
        return await reply(interaction, embed=welcome_embed(guild), view=WelcomeView(uid))
    if key == "autorole":
        return await reply(interaction, embed=autorole_embed(guild), view=AutoRoleView(uid))
    if key == "moderation":
        return await reply(interaction, embed=moderation_embed(guild), view=ModerationView(uid))
    if key == "logs":
        return await reply(interaction, embed=logs_embed(guild), view=LogsView(uid))
    if key == "profile":
        return await reply(interaction, embed=profile_embed(), view=ProfileView(uid))
    if key == "help":
        return await reply(interaction, embed=help_intro_embed(), view=HelpView())


# ----------------------------------------------------------------------------
# Moderation helpers
# ----------------------------------------------------------------------------


def moderation_block_reason(guild: discord.Guild, actor: Optional[discord.Member], target: discord.Member) -> Optional[str]:
    """Return a reason string if `actor` (None = the bot itself) may NOT moderate `target`."""
    if target.id == guild.owner_id:
        return "You cannot moderate the server owner."
    if target.id == guild.me.id:
        return "I will not moderate myself."
    if actor is not None:
        if target.id == actor.id:
            return "You cannot moderate yourself."
        if actor.id != guild.owner_id and actor.top_role <= target.top_role:
            return "That member's highest role is equal to or above yours."
    if guild.me.top_role <= target.top_role:
        return "That member's highest role is equal to or above mine. Move my role higher."
    if target.guild_permissions.administrator:
        return "Discord does not allow timing out Administrators."
    return None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


async def handle_spam(message: discord.Message) -> None:
    guild, member = message.guild, message.author
    gid = guild.id
    if get_setting(gid, "antispam_enabled", "1") != "1" or not isinstance(member, discord.Member):
        return
    if is_staff(member) or is_owner(member, guild) or member.guild_permissions.manage_messages:
        return
    limit = int(get_setting(gid, "spam_limit", "6"))
    window = int(get_setting(gid, "spam_window", "5"))
    repeat = int(get_setting(gid, "repeat_limit", "4"))
    minutes = int(get_setting(gid, "spam_timeout", "5"))
    now = time.monotonic()
    key = (gid, member.id)
    dq = bot.spam[key]
    dq.append((now, normalize_text(message.content), message.id, message.channel.id))
    while dq and now - dq[0][0] > 30:
        dq.popleft()
    burst = [e for e in dq if now - e[0] <= window]
    norm = normalize_text(message.content)
    repeated = [e for e in dq if norm and e[1] == norm]
    reason = None
    victims = []
    if len(burst) >= limit:
        reason, victims = "Message spam", burst
    elif len(repeated) >= repeat:
        reason, victims = "Repeated messages", repeated
    if not reason:
        return
    dq.clear()
    ids = [discord.Object(id=e[2]) for e in victims if e[3] == message.channel.id]
    try:
        if len(ids) == 1:
            await message.delete()
        elif ids:
            await message.channel.delete_messages(ids)
    except discord.HTTPException:
        pass
    action = "messages deleted"
    if moderation_block_reason(guild, None, member) is None:
        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"BOT MART anti-spam: {reason}")
            action += f", timed out {minutes} min"
        except discord.HTTPException:
            pass
    try:
        await message.channel.send(f"{member.mention} anti-spam: {reason.lower()} detected.", delete_after=8,
                                   allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        pass
    await log_activity(guild, "🛡️ Anti-spam", f"{member.mention}: **{reason}** in {message.channel.mention} — {action}.",
                       color=AMBER, modlog=True)


# ----------------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------------


@bot.event
async def on_ready():
    if bot._ready_once:
        return
    bot._ready_once = True
    await apply_presence()
    log.info("Logged in as %s (%s) in %d guild(s)", bot.user, bot.user.id, len(bot.guilds))
    if not OWNER_ID:
        log.warning("OWNER_ID is not set; only each server's owner can use owner commands.")
    schedule_catalog_refresh()


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    try:
        await handle_spam(message)
    except Exception:  # noqa: BLE001
        log.exception("anti-spam failure")


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    role_note = ""
    if not member.bot and get_setting(guild.id, "autorole_disabled") != "1":
        rid = get_int(guild.id, "autorole") or get_int(guild.id, "customer_role")
        role = guild.get_role(rid) if rid else None
        if role and not role.managed and role < guild.me.top_role and not role_is_dangerous(role):
            try:
                await member.add_roles(role, reason="BOT MART auto-role")
                role_note = f" • role {role.mention}"
            except discord.HTTPException:
                role_note = " • auto-role failed"
    if get_setting(guild.id, "welcome_enabled", "1") == "1":
        ch = guild.get_channel(get_int(guild.id, "welcome_channel") or 0)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(embed=build_welcome(member), allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass
    age = (datetime.now(timezone.utc) - member.created_at).days
    await log_activity(guild, "📥 Member joined", f"{member.mention} ({member}) • account {age}d old{role_note}", color=GREEN)


@bot.event
async def on_member_remove(member: discord.Member):
    await log_activity(member.guild, "📤 Member left", f"{member} (`{member.id}`) left the server.", color=RED)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "You do not have permission to use this command."
    else:
        log.error("Command error: %r", error, exc_info=error)
        msg = "Something went wrong while running that command."
    try:
        await reply(interaction, embed=err_embed(msg))
    except discord.HTTPException:
        pass


# ----------------------------------------------------------------------------
# Slash commands — product management (owner only)
# ----------------------------------------------------------------------------


async def product_autocomplete(interaction: discord.Interaction, current: str):
    rows = db.all("SELECT name FROM products WHERE name LIKE ? ORDER BY name LIMIT 25", (f"%{current}%",))
    return [app_commands.Choice(name=r["name"][:100], value=r["name"]) for r in rows]


def _need_product(name: str) -> dict:
    p = find_product(name)
    if not p:
        raise ValueError(f"No product named **{name}**. Use `/products` to see the list.")
    return p


@bot.tree.command(name="addproduct", description="Add a new bot product (owner only)")
@app_commands.describe(name="Product name", price="Price in INR", description="Product description",
                       image_url="Product image URL (optional)", repo_url="Private repository URL (optional)",
                       active="List it in the store right away")
@app_commands.guild_only()
@owner_only()
async def addproduct_cmd(interaction: discord.Interaction, name: str, price: float, description: str,
                         image_url: Optional[str] = None, repo_url: Optional[str] = None, active: bool = True):
    try:
        p = add_product(name, price, description, image_url or "", repo_url or "", active)
    except ValueError as exc:
        return await reply(interaction, embed=err_embed(str(exc)))
    await log_activity(interaction.guild, "📦 Product added", f"{interaction.user.mention} added **{p['name']}**.")
    schedule_catalog_refresh()
    await reply(interaction, embed=product_card(p, owner_view=True))


@bot.tree.command(name="editproduct", description="Edit a product's name, description, image or status (owner only)")
@app_commands.describe(product="Product to edit", new_name="New name", description="New description",
                       image_url="New image URL (use - to remove)", active="Show in the store?")
@app_commands.autocomplete(product=product_autocomplete)
@app_commands.guild_only()
@owner_only()
async def editproduct_cmd(interaction: discord.Interaction, product: str, new_name: Optional[str] = None,
                          description: Optional[str] = None, image_url: Optional[str] = None,
                          active: Optional[bool] = None):
    try:
        p = _need_product(product)
        msg = apply_product_edit(p["id"], name=new_name, description=description,
                                 image_url=("" if image_url == "-" else image_url), active=active)
    except ValueError as exc:
        return await reply(interaction, embed=err_embed(str(exc)))
    await log_activity(interaction.guild, "✏️ Product edited", f"{interaction.user.mention}: {msg}")
    schedule_catalog_refresh()
    await reply(interaction, embed=ok_embed(msg, "Product updated"))


@bot.tree.command(name="removeproduct", description="Permanently remove a product (owner only)")
@app_commands.describe(product="Product to remove")
@app_commands.autocomplete(product=product_autocomplete)
@app_commands.guild_only()
@owner_only()
async def removeproduct_cmd(interaction: discord.Interaction, product: str):
    p = find_product(product)
    if not p:
        return await reply(interaction, embed=err_embed(f"No product named **{product}**."))

    async def go(i: discord.Interaction):
        return await do_remove_product(p["id"], i.guild, i.user)

    await reply(interaction, embed=warn_embed(f"Really remove **{p['name']}**? This cannot be undone.", "Confirm removal"),
                view=ConfirmView(interaction.user.id, go))


@bot.tree.command(name="setprice", description="Change a product's price in INR (owner only)")
@app_commands.describe(product="Product", price="New price in INR")
@app_commands.autocomplete(product=product_autocomplete)
@app_commands.guild_only()
@owner_only()
async def setprice_cmd(interaction: discord.Interaction, product: str, price: float):
    try:
        p = _need_product(product)
        msg = apply_product_edit(p["id"], price=price)
    except ValueError as exc:
        return await reply(interaction, embed=err_embed(str(exc)))
    await log_activity(interaction.guild, "💲 Price changed", f"{interaction.user.mention}: {msg}")
    schedule_catalog_refresh()
    await reply(interaction, embed=ok_embed(msg, "Price updated"))


@bot.tree.command(name="setrepo", description="Set a product's private repository link (server owner only)")
@app_commands.describe(product="Product", url="Private repository URL (https)")
@app_commands.autocomplete(product=product_autocomplete)
@app_commands.guild_only()
@owner_only()
async def setrepo_cmd(interaction: discord.Interaction, product: str, url: str):
    try:
        p = _need_product(product)
        msg = apply_product_edit(p["id"], repo_url=url)
    except ValueError as exc:
        return await reply(interaction, embed=err_embed(str(exc)))
    await log_activity(interaction.guild, "🔗 Repository link changed",
                       f"{interaction.user.mention} updated the link for **{p['name']}**. Link not logged.")
    await reply(interaction, embed=ok_embed(msg, "Repository updated"))


@bot.tree.command(name="products", description="List all products with status and repository state (owner only)")
@app_commands.guild_only()
@owner_only()
async def products_cmd(interaction: discord.Interaction):
    items = list_products()
    if not items:
        return await reply(interaction, embed=make_embed("📦 Products", "No products yet. Use `/addproduct`."))
    cards = [product_card(p, owner_view=True) for p in items[:10]]
    await reply(interaction, embeds=cards)


# ----------------------------------------------------------------------------
# Slash commands — panel, setup, dashboard, help, staff
# ----------------------------------------------------------------------------


@bot.tree.command(name="sendpanel", description="Publish the BOT MART store panel in a channel")
@app_commands.describe(channel="Channel to post the panel in")
@app_commands.guild_only()
@staff_only()
async def sendpanel_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await reply(interaction, embed=err_embed(f"I need View, Send Messages and Embed Links in {channel.mention}."))
    await send_panel(channel)
    await log_activity(interaction.guild, "🛍️ Panel sent", f"{interaction.user.mention} posted the panel in {channel.mention}.")
    await reply(interaction, embed=ok_embed(f"Panel posted in {channel.mention}."))


@bot.tree.command(name="martsetup", description="Create BOT MART categories, channels and roles (owner only)")
@app_commands.guild_only()
@owner_only()
async def martsetup_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    report = await run_setup(interaction.guild)
    await interaction.followup.send(embed=make_embed("🛠️ Server Setup", "\n".join(report)[:4000]), ephemeral=True)


@bot.tree.command(name="martpanel", description="Open the BOT MART management dashboard")
@app_commands.guild_only()
@staff_only()
async def martpanel_cmd(interaction: discord.Interaction):
    await reply(interaction, embed=dashboard_embed(interaction.guild), view=DashboardView())


@bot.tree.command(name="help", description="How BOT MART works")
async def help_cmd(interaction: discord.Interaction):
    await reply(interaction, embed=help_intro_embed(), view=HelpView())


@bot.tree.command(name="staffrole", description="Add, remove or list authorised staff roles (owner only)")
@app_commands.describe(action="What to do", role="Role (for add / remove)")
@app_commands.guild_only()
@owner_only()
async def staffrole_cmd(interaction: discord.Interaction, action: Literal["add", "remove", "list"],
                        role: Optional[discord.Role] = None):
    gid = interaction.guild.id
    if action == "list":
        return await reply(interaction, embed=staff_embed(interaction.guild))
    if role is None:
        return await reply(interaction, embed=err_embed("Choose a role."))
    if action == "add":
        if role.is_default() or role.managed:
            return await reply(interaction, embed=err_embed("That role cannot be used as a staff role."))
        db.run("INSERT OR IGNORE INTO staff_roles (guild_id, role_id) VALUES (?, ?)", (gid, role.id))
    else:
        db.run("DELETE FROM staff_roles WHERE guild_id=? AND role_id=?", (gid, role.id))
    await log_activity(interaction.guild, "👥 Staff roles", f"{interaction.user.mention} {action}ed {role.mention}.")
    await reply(interaction, embed=staff_embed(interaction.guild))


# ----------------------------------------------------------------------------
# Slash commands — moderation
# ----------------------------------------------------------------------------


@bot.tree.command(name="timeout", description="Time out a member (0 minutes removes a timeout)")
@app_commands.describe(member="Member", minutes="Duration in minutes (0-40320)", reason="Reason")
@app_commands.guild_only()
@app_commands.check(lambda i: is_staff(i.user) or i.user.guild_permissions.moderate_members)
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member,
                      minutes: app_commands.Range[int, 0, 40320], reason: Optional[str] = None):
    block = moderation_block_reason(interaction.guild, interaction.user, member)
    if block:
        return await reply(interaction, embed=err_embed(block))
    reason_text = (reason or "No reason provided")[:300]
    try:
        if minutes == 0:
            await member.timeout(None, reason=f"{interaction.user}: {reason_text}")
            text = f"Removed the timeout from {member.mention}."
        else:
            await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason_text}")
            text = f"Timed out {member.mention} for **{minutes}** minute(s)."
    except discord.Forbidden:
        return await reply(interaction, embed=err_embed("I lack the **Moderate Members** permission or role position."))
    except discord.HTTPException as exc:
        return await reply(interaction, embed=err_embed(f"Discord error: {exc.text or exc.status}"))
    await log_activity(interaction.guild, "⏱️ Timeout", f"{interaction.user.mention} → {member.mention}: {text} Reason: {reason_text}",
                       color=AMBER, modlog=True)
    await reply(interaction, embed=ok_embed(text, "Timeout"))


@bot.tree.command(name="clear", description="Delete recent messages in this channel")
@app_commands.describe(amount="How many messages to delete (1-100)")
@app_commands.guild_only()
@app_commands.check(lambda i: is_staff(i.user) or i.user.guild_permissions.manage_messages)
async def clear_cmd(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return await reply(interaction, embed=err_embed("This only works in text channels."))
    if not channel.permissions_for(interaction.guild.me).manage_messages:
        return await reply(interaction, embed=err_embed("I need the **Manage Messages** permission here."))
    if get_ticket_safe(channel.id):
        return await reply(interaction, embed=err_embed("Ticket channels cannot be cleared; close the ticket instead."))
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await channel.purge(limit=amount, reason=f"/clear by {interaction.user}")
    except discord.HTTPException as exc:
        return await interaction.followup.send(embed=err_embed(f"Discord error: {exc.text or exc.status}"), ephemeral=True)
    await log_activity(interaction.guild, "🧹 Messages cleared",
                       f"{interaction.user.mention} deleted {len(deleted)} message(s) in {channel.mention}.", modlog=True)
    await interaction.followup.send(embed=ok_embed(f"Deleted **{len(deleted)}** message(s)."), ephemeral=True)


def get_ticket_safe(channel_id: int):
    return ticket_by_channel(channel_id)


# ----------------------------------------------------------------------------
# Slash commands — bot profile (owner only)
# ----------------------------------------------------------------------------


@bot.tree.command(name="setavatar", description="Change the bot's avatar (owner only)")
@app_commands.describe(image="Upload an image", url="…or a public https image URL")
@app_commands.guild_only()
@owner_only()
async def setavatar_cmd(interaction: discord.Interaction, image: Optional[discord.Attachment] = None,
                        url: Optional[str] = None):
    if not image and not url:
        return await reply(interaction, embed=err_embed("Provide an image attachment or a URL."))
    await interaction.response.defer(ephemeral=True)
    try:
        if image:
            if not (image.content_type or "").startswith("image/") or image.size > 8 * 1024 * 1024:
                raise ValueError("Attach an image file smaller than 8 MB.")
            data = await image.read()
        else:
            data = await fetch_image(url.strip())
        await set_avatar_bytes(data)
    except ValueError as exc:
        return await interaction.followup.send(embed=err_embed(str(exc)), ephemeral=True)
    await interaction.followup.send(embed=ok_embed("Avatar updated.", "Bot profile"), ephemeral=True)


@bot.tree.command(name="setstatus", description="Change the bot's online status (owner only)")
@app_commands.describe(status="Online, Idle, Do Not Disturb or Invisible")
@app_commands.guild_only()
@owner_only()
async def setstatus_cmd(interaction: discord.Interaction, status: Literal["online", "idle", "dnd", "invisible"]):
    set_setting(0, "bot_status", status)
    await apply_presence()
    await reply(interaction, embed=ok_embed(f"Status set to **{status}**.", "Bot profile"))


@bot.tree.command(name="setactivity", description="Change the bot's activity text (owner only)")
@app_commands.describe(text="Activity text", type="Activity type")
@app_commands.guild_only()
@owner_only()
async def setactivity_cmd(interaction: discord.Interaction, text: app_commands.Range[str, 1, 100],
                          type: Literal["playing", "watching", "listening", "competing"] = "watching"):
    set_setting(0, "bot_activity", text)
    set_setting(0, "bot_activity_type", type)
    await apply_presence()
    await reply(interaction, embed=ok_embed(f"Activity set to **{type} {text}**.", "Bot profile"))


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set.")
    if not RAZORPAY_WEBHOOK_SECRET and RAZORPAY_KEY_ID:
        log.warning("RAZORPAY_WEBHOOK_SECRET is not set: automatic webhook verification is disabled "
                    "(the 'Check Razorpay Payment' button still works).")
    bot.run(BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
