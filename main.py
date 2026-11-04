"""
ATLANTA ESPORTS LOBBY MANAGEMENT BOT
Play Fair. Compete Hard. Rise Together.

Python 3.12 | discord.py 2.x | SQLite | Railway
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("atlanta")


def env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Environment variable %s is not a valid integer: %r", name, raw)
        return default


TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
ADMIN_ROLE_ID = env_int("ADMIN_ROLE_ID")
PAYMENT_CATEGORY_ID = env_int("PAYMENT_CATEGORY_ID")
GUILD_ID = env_int("GUILD_ID")  # optional: instant command sync for one server
DATA_DIR = Path(os.getenv("DATA_DIR") or "./data")
DB_PATH = Path(os.getenv("DB_PATH") or str(DATA_DIR / "atlanta_esports.db"))

BRAND = "ATLANTA ESPORTS"
TAGLINE = "Play Fair. Compete Hard. Rise Together."
FOOTER = f"{BRAND}  |  {TAGLINE}"

NAVY = 0x0B1F4B
ACCENT = 0x2F6BFF
SUCCESS = 0x1E8E5A
DANGER = 0xB3261E

ACTIVE_SQL = "('pending_payment','pending_review','approved')"
STATUS_LABEL = {
    "pending_payment": "Awaiting Payment",
    "pending_review": "Awaiting Verification",
    "approved": "Approved",
    "rejected": "Rejected",
}
PAY_LABEL = {
    "awaiting": "Awaiting Screenshot",
    "submitted": "Screenshot Submitted",
    "approved": "Verified",
    "rejected": "Rejected",
}

DEFAULT_RULES = (
    "1. A squad must have exactly four registered players.\n"
    "2. Registration is confirmed only after an administrator verifies your payment.\n"
    "3. Hacks, cheats, emulators (unless announced) and teaming result in disqualification.\n"
    "4. Be present in the lobby on time. Room details are shared with role holders only.\n"
    "5. Only registered players may play for a registered team. No substitutions without approval.\n"
    "6. Entry fees are non-refundable once a lobby is confirmed, unless the organizers cancel it.\n"
    "7. Respect all players and staff. Administrator decisions are final."
)

# Roles with any of these permissions can never be used as a lobby role.
PRIVILEGED = discord.Permissions(
    administrator=True,
    manage_guild=True,
    manage_roles=True,
    manage_channels=True,
    manage_messages=True,
    manage_webhooks=True,
    manage_nicknames=True,
    kick_members=True,
    ban_members=True,
    moderate_members=True,
    mention_everyone=True,
    view_audit_log=True,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_ts(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        return f"<t:{int(datetime.fromisoformat(iso).timestamp())}:f>"
    except ValueError:
        return iso


def money(n: int) -> str:
    return "Free" if n == 0 else f"\u20b9{n:,}"


def progress_bar(current: int, total: int, length: int = 12) -> str:
    total = max(total, 1)
    filled = min(length, round(length * current / total))
    return "\u2588" * filled + "\u2591" * (length - filled)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:20] or "team"


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    qr_path TEXT,
    qr_updated_by INTEGER,
    qr_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS lobbies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    entry_fee INTEGER NOT NULL DEFAULT 0,
    prize_pool INTEGER NOT NULL DEFAULT 0,
    capacity INTEGER NOT NULL DEFAULT 25,
    match_time TEXT NOT NULL DEFAULT 'To be announced',
    role_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    panel_channel_id INTEGER,
    panel_message_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lobby_id INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
    guild_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    captain_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment'
        CHECK (status IN ('pending_payment','pending_review','approved','rejected')),
    payment_channel_id INTEGER,
    review_message_id INTEGER,
    approved_by INTEGER,
    approved_at TEXT,
    rejected_by INTEGER,
    rejected_at TEXT,
    reject_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id INTEGER NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
    slot INTEGER NOT NULL,
    discord_id INTEGER NOT NULL,
    bgmi_id TEXT NOT NULL,
    role_assigned INTEGER NOT NULL DEFAULT 0,
    assigned_role_id INTEGER,
    UNIQUE (registration_id, slot)
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id INTEGER NOT NULL UNIQUE REFERENCES registrations(id) ON DELETE CASCADE,
    amount INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'awaiting'
        CHECK (status IN ('awaiting','submitted','approved','rejected')),
    screenshot_url TEXT,
    screenshot_message_id INTEGER,
    submitted_at TEXT,
    reviewed_by INTEGER,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lobbies_guild ON lobbies(guild_id);
CREATE INDEX IF NOT EXISTS idx_reg_lobby ON registrations(lobby_id, status);
CREATE INDEX IF NOT EXISTS idx_reg_channel ON registrations(payment_channel_id);
CREATE INDEX IF NOT EXISTS idx_players_reg ON players(registration_id);
CREATE INDEX IF NOT EXISTS idx_players_discord ON players(discord_id);
"""

LOBBY_FIELDS = {
    "name", "description", "entry_fee", "prize_pool", "capacity", "match_time",
    "role_id", "status", "panel_channel_id", "panel_message_id",
}


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)

    # -- primitives ---------------------------------------------------------
    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def q1(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def x(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    # -- guild settings -----------------------------------------------------
    def get_qr(self, guild_id: int) -> Optional[str]:
        row = self.q1("SELECT qr_path FROM guild_settings WHERE guild_id=?", (guild_id,))
        return row["qr_path"] if row else None

    def set_qr(self, guild_id: int, path: str, admin_id: int) -> None:
        self.x(
            """INSERT INTO guild_settings (guild_id, qr_path, qr_updated_by, qr_updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(guild_id) DO UPDATE SET qr_path=excluded.qr_path,
               qr_updated_by=excluded.qr_updated_by, qr_updated_at=excluded.qr_updated_at""",
            (guild_id, path, admin_id, now_iso()),
        )

    # -- logs ---------------------------------------------------------------
    def log_admin(self, guild_id: int, admin_id: int, action: str, details: str = "") -> None:
        self.x(
            "INSERT INTO admin_logs (guild_id, admin_id, action, details, created_at) VALUES (?,?,?,?,?)",
            (guild_id, admin_id, action, details, now_iso()),
        )

    # -- lobbies ------------------------------------------------------------
    def create_lobby(self, guild_id, name, description, fee, prize, capacity, match_time, role_id, admin_id) -> int:
        cur = self.x(
            """INSERT INTO lobbies (guild_id,name,description,entry_fee,prize_pool,capacity,
               match_time,role_id,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (guild_id, name, description or "", fee, prize, capacity, match_time, role_id, admin_id, now_iso()),
        )
        return int(cur.lastrowid)

    def get_lobby(self, lobby_id: int, guild_id: Optional[int] = None) -> Optional[sqlite3.Row]:
        row = self.q1("SELECT * FROM lobbies WHERE id=?", (lobby_id,))
        if row and guild_id is not None and row["guild_id"] != guild_id:
            return None
        return row

    def lobby_by_message(self, message_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM lobbies WHERE panel_message_id=?", (message_id,))

    def list_lobbies(self, guild_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM lobbies WHERE guild_id=? ORDER BY id DESC", (guild_id,))

    def update_lobby(self, lobby_id: int, **fields) -> None:
        bad = set(fields) - LOBBY_FIELDS
        if bad or not fields:
            raise ValueError(f"Invalid lobby fields: {bad}")
        sets = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE lobbies SET {sets} WHERE id=?", (*fields.values(), lobby_id))

    def delete_lobby(self, lobby_id: int) -> list[int]:
        with self.transaction() as c:
            chans = [r["payment_channel_id"] for r in c.execute(
                "SELECT payment_channel_id FROM registrations WHERE lobby_id=? AND payment_channel_id IS NOT NULL",
                (lobby_id,)).fetchall()]
            c.execute("DELETE FROM lobbies WHERE id=?", (lobby_id,))
        return chans

    def lobby_counts(self, lobby_id: int) -> dict:
        row = self.q1(
            f"""SELECT
                COALESCE(SUM(status IN {ACTIVE_SQL}),0) AS active,
                COALESCE(SUM(status='approved'),0) AS approved,
                COALESCE(SUM(status IN ('pending_payment','pending_review')),0) AS pending,
                COALESCE(SUM(status='rejected'),0) AS rejected
                FROM registrations WHERE lobby_id=?""",
            (lobby_id,),
        )
        return dict(row)

    # -- registrations ------------------------------------------------------
    def create_registration(self, lobby_id: int, team_name: str, players: list[tuple[int, str]]):
        """Atomic: capacity, status and duplicate checks plus inserts. Returns (reg_id, error)."""
        with self.transaction() as c:
            lobby = c.execute("SELECT * FROM lobbies WHERE id=?", (lobby_id,)).fetchone()
            if not lobby:
                return None, "This lobby no longer exists."
            if lobby["status"] != "open":
                return None, "Registration is closed for this lobby."
            active = c.execute(
                f"SELECT COUNT(*) FROM registrations WHERE lobby_id=? AND status IN {ACTIVE_SQL}",
                (lobby_id,)).fetchone()[0]
            if active >= lobby["capacity"]:
                return None, "This lobby is full."
            if c.execute(
                f"SELECT 1 FROM registrations WHERE lobby_id=? AND status IN {ACTIVE_SQL} "
                "AND LOWER(team_name)=LOWER(?)", (lobby_id, team_name)).fetchone():
                return None, f"A team named **{team_name}** is already registered in this lobby."
            for slot, (did, bgmi) in enumerate(players, start=1):
                clash = c.execute(
                    f"""SELECT r.team_name FROM players p JOIN registrations r ON r.id=p.registration_id
                        WHERE r.lobby_id=? AND r.status IN {ACTIVE_SQL}
                        AND (p.discord_id=? OR p.bgmi_id=?)""",
                    (lobby_id, did, bgmi)).fetchone()
                if clash:
                    who = "The captain" if slot == 1 else f"Player {slot}"
                    return None, f"{who} (<@{did}> / BGMI {bgmi}) is already in team **{clash['team_name']}** in this lobby."
            cur = c.execute(
                "INSERT INTO registrations (lobby_id,guild_id,team_name,captain_id,status,created_at) "
                "VALUES (?,?,?,?, 'pending_payment', ?)",
                (lobby_id, lobby["guild_id"], team_name, players[0][0], now_iso()))
            reg_id = cur.lastrowid
            for slot, (did, bgmi) in enumerate(players, start=1):
                c.execute("INSERT INTO players (registration_id,slot,discord_id,bgmi_id) VALUES (?,?,?,?)",
                          (reg_id, slot, did, bgmi))
            c.execute("INSERT INTO payments (registration_id,amount,status) VALUES (?,?, 'awaiting')",
                      (reg_id, lobby["entry_fee"]))
            return int(reg_id), None

    def get_registration(self, reg_id: int, guild_id: Optional[int] = None) -> Optional[sqlite3.Row]:
        row = self.q1("SELECT * FROM registrations WHERE id=?", (reg_id,))
        if row and guild_id is not None and row["guild_id"] != guild_id:
            return None
        return row

    def reg_by_channel(self, channel_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM registrations WHERE payment_channel_id=?", (channel_id,))

    def get_players(self, reg_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM players WHERE registration_id=? ORDER BY slot", (reg_id,))

    def get_payment(self, reg_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM payments WHERE registration_id=?", (reg_id,))

    def delete_registration(self, reg_id: int) -> None:
        self.x("DELETE FROM registrations WHERE id=?", (reg_id,))

    def set_payment_channel(self, reg_id: int, channel_id: int) -> None:
        self.x("UPDATE registrations SET payment_channel_id=? WHERE id=?", (channel_id, reg_id))

    def set_review_message(self, reg_id: int, message_id: int) -> None:
        self.x("UPDATE registrations SET review_message_id=? WHERE id=?", (message_id, reg_id))

    def submit_payment(self, reg_id: int, url: str, message_id: int) -> bool:
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE registrations SET status='pending_review' "
                "WHERE id=? AND status IN ('pending_payment','pending_review')", (reg_id,))
            if cur.rowcount != 1:
                return False
            c.execute(
                "UPDATE payments SET status='submitted', screenshot_url=?, screenshot_message_id=?, "
                "submitted_at=? WHERE registration_id=?", (url, message_id, now_iso(), reg_id))
            return True

    def mark_approved(self, reg_id: int, admin_id: int) -> bool:
        ts = now_iso()
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE registrations SET status='approved', approved_by=?, approved_at=? "
                "WHERE id=? AND status IN ('pending_payment','pending_review')", (admin_id, ts, reg_id))
            if cur.rowcount != 1:
                return False
            c.execute("UPDATE payments SET status='approved', reviewed_by=?, reviewed_at=? "
                      "WHERE registration_id=?", (admin_id, ts, reg_id))
            return True

    def mark_rejected(self, reg_id: int, admin_id: int, reason: str) -> bool:
        ts = now_iso()
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE registrations SET status='rejected', rejected_by=?, rejected_at=?, reject_reason=? "
                "WHERE id=? AND status IN ('pending_payment','pending_review')", (admin_id, ts, reason, reg_id))
            if cur.rowcount != 1:
                return False
            c.execute("UPDATE payments SET status='rejected', reviewed_by=?, reviewed_at=? "
                      "WHERE registration_id=?", (admin_id, ts, reg_id))
            return True

    def set_player_role(self, player_id: int, assigned: bool, role_id: Optional[int]) -> None:
        self.x("UPDATE players SET role_assigned=?, assigned_role_id=? WHERE id=?",
               (1 if assigned else 0, role_id if assigned else None, player_id))

    def regs_for_lobby(self, lobby_id: int) -> list[sqlite3.Row]:
        return self.q(
            """SELECT r.*, p.status AS pay_status, p.amount AS pay_amount
               FROM registrations r LEFT JOIN payments p ON p.registration_id=r.id
               WHERE r.lobby_id=? ORDER BY r.id""", (lobby_id,))

    def pending_regs(self, guild_id: int) -> list[sqlite3.Row]:
        return self.q(
            """SELECT r.*, l.name AS lobby_name, p.status AS pay_status, p.amount AS pay_amount
               FROM registrations r JOIN lobbies l ON l.id=r.lobby_id
               LEFT JOIN payments p ON p.registration_id=r.id
               WHERE l.guild_id=? AND r.status IN ('pending_payment','pending_review')
               ORDER BY r.id""", (guild_id,))

    def regs_for_user(self, lobby_id: int, user_id: int) -> list[sqlite3.Row]:
        return self.q(
            """SELECT DISTINCT r.*, pay.status AS pay_status
               FROM registrations r JOIN players p ON p.registration_id=r.id
               LEFT JOIN payments pay ON pay.registration_id=r.id
               WHERE r.lobby_id=? AND p.discord_id=? ORDER BY r.id DESC""", (lobby_id, user_id))

    def export_rows(self, guild_id: int, lobby_id: Optional[int]) -> list[dict]:
        sql = """SELECT r.*, l.name AS lobby_name, p.amount AS pay_amount, p.status AS pay_status,
                        p.screenshot_url AS screenshot_url
                 FROM registrations r JOIN lobbies l ON l.id=r.lobby_id
                 LEFT JOIN payments p ON p.registration_id=r.id WHERE l.guild_id=?"""
        params: tuple = (guild_id,)
        if lobby_id:
            sql += " AND r.lobby_id=?"
            params += (lobby_id,)
        sql += " ORDER BY r.lobby_id, r.id"
        out = []
        for r in self.q(sql, params):
            d = dict(r)
            d["players"] = [dict(p) for p in self.get_players(r["id"])]
            out.append(d)
        return out

    def stats(self, guild_id: int) -> dict:
        lob = self.q1(
            """SELECT COUNT(*) AS total, COALESCE(SUM(status='open'),0) AS open_,
               COALESCE(SUM(status='closed'),0) AS closed_, COALESCE(SUM(prize_pool),0) AS prize
               FROM lobbies WHERE guild_id=?""", (guild_id,))
        reg = self.q1(
            f"""SELECT COALESCE(SUM(r.status IN {ACTIVE_SQL}),0) AS active,
                COALESCE(SUM(r.status='rejected'),0) AS rejected
                FROM registrations r JOIN lobbies l ON l.id=r.lobby_id WHERE l.guild_id=?""", (guild_id,))
        pay = self.q1(
            """SELECT COALESCE(SUM(p.status IN ('awaiting','submitted')),0) AS pending,
               COALESCE(SUM(p.status='approved'),0) AS approved,
               COALESCE(SUM(CASE WHEN p.status='approved' THEN p.amount ELSE 0 END),0) AS collected
               FROM payments p JOIN registrations r ON r.id=p.registration_id
               JOIN lobbies l ON l.id=r.lobby_id WHERE l.guild_id=?""", (guild_id,))
        per = self.q(
            f"""SELECT l.id, l.name, l.capacity, l.status,
                COALESCE(SUM(r.status IN {ACTIVE_SQL}),0) AS active,
                COALESCE(SUM(r.status='approved'),0) AS approved
                FROM lobbies l LEFT JOIN registrations r ON r.lobby_id=l.id
                WHERE l.guild_id=? GROUP BY l.id ORDER BY l.id""", (guild_id,))
        return {"lobbies": dict(lob), "regs": dict(reg), "pay": dict(pay), "per": [dict(p) for p in per]}


db = Database(DB_PATH)


# ----------------------------------------------------------------------------
# Permission and role helpers
# ----------------------------------------------------------------------------
def is_admin(user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    return bool(ADMIN_ROLE_ID) and any(r.id == ADMIN_ROLE_ID for r in user.roles)


def admin_mention() -> str:
    return f"<@&{ADMIN_ROLE_ID}>" if ADMIN_ROLE_ID else ""


def role_problem(guild: discord.Guild, role: discord.Role) -> Optional[str]:
    """Return a human-readable reason if the role cannot safely be used as a lobby role."""
    if role.is_default():
        return "The @everyone role cannot be used as a lobby role."
    if role.managed:
        return "That role is managed by an integration and cannot be assigned manually."
    me = guild.me
    if me is None:
        return "The bot is not fully loaded in this server yet. Try again shortly."
    if not me.guild_permissions.manage_roles:
        return "The bot needs the Manage Roles permission."
    if role >= me.top_role:
        return f"{role.mention} is above or equal to the bot's highest role. Move the bot's role higher in Server Settings > Roles."
    if role.permissions.value & PRIVILEGED.value:
        return f"{role.mention} has elevated permissions and cannot be used as a lobby role."
    return None


async def get_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


async def reply(interaction: discord.Interaction, *, content=None, embed=None, view=None, file=None):
    kw = {}
    if content is not None:
        kw["content"] = content
    if embed is not None:
        kw["embed"] = embed
    if view is not None:
        kw["view"] = view
    if file is not None:
        kw["file"] = file
    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=True, **kw)
    else:
        await interaction.response.send_message(ephemeral=True, **kw)


def make_embed(title: str, description: str = "", color: int = ACCENT) -> discord.Embed:
    e = discord.Embed(title=title, description=description or None, color=color,
                      timestamp=datetime.now(timezone.utc))
    e.set_footer(text=FOOTER)
    return e


def parse_int(text: str, label: str, lo: int, hi: int) -> int:
    cleaned = re.sub(r"[,\s\u20b9]", "", text or "")
    if not re.fullmatch(r"\d+", cleaned):
        raise ValueError(f"{label} must be a whole number.")
    n = int(cleaned)
    if not lo <= n <= hi:
        raise ValueError(f"{label} must be between {lo:,} and {hi:,}.")
    return n


def parse_player_line(text: str, label: str) -> tuple[int, str]:
    parts = [p for p in re.split(r"[,\s|/]+", (text or "").strip()) if p]
    if len(parts) != 2:
        raise ValueError(f"{label}: enter `DiscordID, BGMIID` (for example `123456789012345678, 5123456789`).")
    did = re.sub(r"[<@!>]", "", parts[0])
    if not did.isdigit() or not 15 <= len(did) <= 20:
        raise ValueError(f"{label}: the Discord ID is not valid. Use the numeric user ID or a mention.")
    if not re.fullmatch(r"\d{5,15}", parts[1]):
        raise ValueError(f"{label}: the BGMI ID must be 5 to 15 digits.")
    return int(did), parts[1]


# ----------------------------------------------------------------------------
# Embeds
# ----------------------------------------------------------------------------
def lobby_status_text(lobby: sqlite3.Row, counts: dict) -> str:
    if lobby["status"] == "closed":
        return "CLOSED"
    if counts["active"] >= lobby["capacity"]:
        return "FULL"
    return "OPEN"


def lobby_embed(lobby: sqlite3.Row) -> discord.Embed:
    c = db.lobby_counts(lobby["id"])
    status = lobby_status_text(lobby, c)
    desc = f"*{TAGLINE}*"
    if lobby["description"]:
        desc += f"\n\n{lobby['description']}"
    e = discord.Embed(title=f"{BRAND}  |  {lobby['name']}", description=desc, color=NAVY,
                      timestamp=datetime.now(timezone.utc))
    e.add_field(name="Lobby ID", value=f"`#{lobby['id']}`", inline=True)
    e.add_field(name="Status", value=f"**{status}**", inline=True)
    e.add_field(name="Entry Fee", value=money(lobby["entry_fee"]), inline=True)
    e.add_field(name="Prize Pool", value=money(lobby["prize_pool"]), inline=True)
    e.add_field(name="Match Schedule", value=lobby["match_time"], inline=True)
    e.add_field(name="Registration Role",
                value=f"<@&{lobby['role_id']}>" if lobby["role_id"] else "Not assigned", inline=True)
    e.add_field(
        name="Team Registration",
        value=f"`{progress_bar(c['active'], lobby['capacity'])}`  **{c['active']} / {lobby['capacity']}** teams\n"
              f"{c['approved']} confirmed  |  {c['pending']} pending verification",
        inline=False)
    e.set_footer(text=FOOTER)
    return e


def players_text(players: list[sqlite3.Row]) -> str:
    lines = []
    for p in players:
        tag = "Captain" if p["slot"] == 1 else f"Player {p['slot']}"
        lines.append(f"**{tag}:** <@{p['discord_id']}>  |  BGMI `{p['bgmi_id']}`")
    return "\n".join(lines)


def build_registrations_embed(lobby: sqlite3.Row) -> discord.Embed:
    regs = db.regs_for_lobby(lobby["id"])
    e = make_embed(f"Registrations  |  {lobby['name']}", color=NAVY)
    if not regs:
        e.description = "No teams have registered yet."
        return e
    lines, size = [], 0
    for i, r in enumerate(regs):
        line = (f"`#{r['id']}` **{r['team_name']}**  |  {STATUS_LABEL[r['status']]}  |  "
                f"Captain <@{r['captain_id']}>  |  Payment: {PAY_LABEL.get(r['pay_status'], '-')}")
        if size + len(line) > 3800:
            lines.append(f"... and {len(regs) - i} more. Use export for the full list.")
            break
        lines.append(line)
        size += len(line) + 1
    e.description = "\n".join(lines)
    return e


def build_pending_embed(guild_id: int) -> discord.Embed:
    regs = db.pending_regs(guild_id)
    e = make_embed("Pending Payments", color=NAVY)
    if not regs:
        e.description = "There are no pending payments."
        return e
    lines, size = [], 0
    for i, r in enumerate(regs):
        chan = f"<#{r['payment_channel_id']}>" if r["payment_channel_id"] else "-"
        line = (f"`#{r['id']}` **{r['team_name']}**  |  {r['lobby_name']}  |  {money(r['pay_amount'] or 0)}  |  "
                f"{PAY_LABEL.get(r['pay_status'], '-')}  |  {chan}")
        if size + len(line) > 3800:
            lines.append(f"... and {len(regs) - i} more.")
            break
        lines.append(line)
        size += len(line) + 1
    e.description = "\n".join(lines)
    return e


def build_stats_embed(guild_id: int) -> discord.Embed:
    s = db.stats(guild_id)
    lob, reg, pay = s["lobbies"], s["regs"], s["pay"]
    e = make_embed("Administrator Statistics", "All figures are calculated from stored registrations and verified payments.", NAVY)
    e.add_field(name="Total Lobbies", value=str(lob["total"]), inline=True)
    e.add_field(name="Open Lobbies", value=str(lob["open_"]), inline=True)
    e.add_field(name="Closed Lobbies", value=str(lob["closed_"]), inline=True)
    e.add_field(name="Total Registered Teams", value=str(reg["active"]), inline=True)
    e.add_field(name="Pending Payments", value=str(pay["pending"]), inline=True)
    e.add_field(name="Approved Payments", value=str(pay["approved"]), inline=True)
    e.add_field(name="Rejected Registrations", value=str(reg["rejected"]), inline=True)
    e.add_field(name="Total Entry Fee Collection", value=f"\u20b9{pay['collected']:,}", inline=True)
    e.add_field(name="Total Prize Pool", value=f"\u20b9{lob['prize']:,}", inline=True)
    if s["per"]:
        lines = []
        for p in s["per"][-20:]:
            lines.append(f"`#{p['id']}` {p['name']}: **{p['active']}/{p['capacity']}** teams, {p['approved']} approved")
        e.add_field(name="Lobby-wise Registrations", value="\n".join(lines)[:1024], inline=False)
    return e


def build_export_file(guild_id: int, lobby_id: Optional[int]) -> discord.File:
    rows = db.export_rows(guild_id, lobby_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    header = ["registration_id", "lobby_id", "lobby_name", "team_name", "status", "payment_status", "amount",
              "screenshot_url", "approved_by", "approved_at", "created_at"]
    for tag in ("captain", "player2", "player3", "player4"):
        header += [f"{tag}_discord_id", f"{tag}_bgmi_id", f"{tag}_role_assigned"]
    w.writerow(header)
    for r in rows:
        line = [r["id"], r["lobby_id"], r["lobby_name"], r["team_name"], r["status"], r["pay_status"],
                r["pay_amount"], r["screenshot_url"], r["approved_by"], r["approved_at"], r["created_at"]]
        by_slot = {p["slot"]: p for p in r["players"]}
        for slot in range(1, 5):
            p = by_slot.get(slot)
            line += [p["discord_id"], p["bgmi_id"], p["role_assigned"]] if p else ["", "", ""]
        w.writerow(line)
    name = f"atlanta_registrations_{'lobby_' + str(lobby_id) if lobby_id else 'all'}.csv"
    return discord.File(io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename=name)


# ----------------------------------------------------------------------------
# Panel management
# ----------------------------------------------------------------------------
async def refresh_panel(guild: discord.Guild, lobby_id: int) -> None:
    lobby = db.get_lobby(lobby_id)
    if not lobby or not lobby["panel_channel_id"] or not lobby["panel_message_id"]:
        return
    channel = guild.get_channel(lobby["panel_channel_id"])
    if channel is None:
        return
    try:
        await channel.get_partial_message(lobby["panel_message_id"]).edit(embed=lobby_embed(lobby))
    except discord.NotFound:
        db.update_lobby(lobby_id, panel_channel_id=None, panel_message_id=None)
    except discord.HTTPException as exc:
        log.warning("Could not refresh panel for lobby %s: %s", lobby_id, exc)


async def post_panel(guild: discord.Guild, lobby_id: int, channel: discord.abc.GuildChannel):
    lobby = db.get_lobby(lobby_id, guild.id)
    if not lobby:
        raise ValueError("Lobby not found.")
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("Panels can only be posted in text channels.")
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        raise ValueError(f"The bot needs View Channel, Send Messages and Embed Links in {channel.mention}.")
    if lobby["panel_channel_id"] and lobby["panel_message_id"]:
        old = guild.get_channel(lobby["panel_channel_id"])
        if old:
            try:
                await old.get_partial_message(lobby["panel_message_id"]).delete()
            except discord.HTTPException:
                pass
    msg = await channel.send(embed=lobby_embed(lobby), view=PanelView())
    db.update_lobby(lobby_id, panel_channel_id=channel.id, panel_message_id=msg.id)
    return msg


# ----------------------------------------------------------------------------
# Base UI classes
# ----------------------------------------------------------------------------
class AdminView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user):
            return True
        await reply(interaction, content="You are not authorised to use this control.")
        return False


class BaseModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal error", exc_info=error)
        try:
            await reply(interaction, content="Something went wrong while processing your request. Please try again.")
        except discord.HTTPException:
            pass


async def deny_non_admin(interaction: discord.Interaction) -> bool:
    if is_admin(interaction.user):
        return False
    await reply(interaction, content="You are not authorised to perform this action.")
    return True


class LobbyPicker(AdminView):
    def __init__(self, guild_id: int, handler: Callable[[discord.Interaction, int], Awaitable], *,
                 include_all: bool = False, placeholder: str = "Select a lobby"):
        super().__init__(timeout=180)
        self.handler = handler
        options: list[discord.SelectOption] = []
        rows = db.list_lobbies(guild_id)[: 24 if include_all else 25]
        if include_all:
            options.append(discord.SelectOption(label="All lobbies", value="0"))
        for r in rows:
            c = db.lobby_counts(r["id"])
            options.append(discord.SelectOption(
                label=f"#{r['id']}  {r['name']}"[:100],
                description=f"{r['status'].title()}  |  Fee {money(r['entry_fee'])}  |  {c['active']}/{r['capacity']} teams"[:100],
                value=str(r["id"])))
        self.select = discord.ui.Select(placeholder=placeholder, options=options)
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        await self.handler(interaction, int(self.select.values[0]))


async def open_picker(interaction: discord.Interaction, handler, *, include_all: bool = False,
                      text: str = "Select a lobby."):
    if not db.list_lobbies(interaction.guild_id):
        await reply(interaction, content="No lobbies exist yet. Create one first.")
        return
    await reply(interaction, content=text, view=LobbyPicker(interaction.guild_id, handler, include_all=include_all))


# ----------------------------------------------------------------------------
# Admin actions (shared by slash commands and the admin console)
# ----------------------------------------------------------------------------
async def action_set_status(interaction: discord.Interaction, lobby_id: int, status: str):
    lobby = db.get_lobby(lobby_id, interaction.guild_id)
    if not lobby:
        return await reply(interaction, content="Lobby not found.")
    db.update_lobby(lobby_id, status=status)
    db.log_admin(interaction.guild_id, interaction.user.id, f"lobby_{status}", f"lobby={lobby_id}")
    await refresh_panel(interaction.guild, lobby_id)
    note = ""
    if status == "open" and not lobby["role_id"]:
        note = "\nNo registration role is set yet. Assign one before approving payments."
    await reply(interaction, content=f"Lobby **#{lobby_id} {lobby['name']}** is now **{status.upper()}**.{note}")


async def action_set_role(guild: discord.Guild, lobby_id: int, role: discord.Role, admin: discord.Member):
    lobby = db.get_lobby(lobby_id, guild.id)
    if not lobby:
        return False, "Lobby not found."
    problem = role_problem(guild, role)
    if problem:
        return False, problem
    db.update_lobby(lobby_id, role_id=role.id)
    db.log_admin(guild.id, admin.id, "lobby_role", f"lobby={lobby_id} role={role.id}")
    await refresh_panel(guild, lobby_id)
    return True, f"Lobby **#{lobby_id} {lobby['name']}** will now assign {role.mention} after payment approval."


async def action_create_role(guild: discord.Guild, name: str, color_text: str, admin: discord.Member):
    color = ACCENT
    if color_text.strip():
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", color_text.strip())
        if not m:
            raise ValueError("Colour must be a hex value such as #2F6BFF.")
        color = int(m.group(1), 16)
    if not guild.me.guild_permissions.manage_roles:
        raise ValueError("The bot needs the Manage Roles permission to create roles.")
    role = await guild.create_role(
        name=name, colour=discord.Colour(color), permissions=discord.Permissions.none(),
        mentionable=False, reason=f"Lobby role created by {admin} via Atlanta Esports bot")
    db.log_admin(guild.id, admin.id, "create_role", f"role={role.id} name={name}")
    return role


async def action_save_qr(guild: discord.Guild, attachment: discord.Attachment, admin: discord.Member):
    ctype = (attachment.content_type or "").split(";")[0].lower()
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(ctype)
    if not ext:
        return False, "The QR must be a PNG, JPEG or WEBP image."
    if attachment.size > 8 * 1024 * 1024:
        return False, "The image is larger than 8 MB."
    data = await attachment.read()
    qr_dir = DATA_DIR / "payment_qr"
    qr_dir.mkdir(parents=True, exist_ok=True)
    final = qr_dir / f"shared_qr_{guild.id}{ext}"
    tmp = qr_dir / f"shared_qr_{guild.id}{ext}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, final)
    old = db.get_qr(guild.id)
    if old and Path(old) != final:
        try:
            Path(old).unlink(missing_ok=True)
        except OSError:
            pass
    db.set_qr(guild.id, str(final), admin.id)
    db.log_admin(guild.id, admin.id, "set_qr", attachment.filename)
    return True, "The shared payment QR has been saved. It is used by all lobbies and is only shown in private payment channels."


class ConfirmDeleteView(AdminView):
    def __init__(self, lobby_id: int):
        super().__init__(timeout=60)
        self.lobby_id = lobby_id

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lobby = db.get_lobby(self.lobby_id, guild.id)
        if not lobby:
            return await interaction.followup.send("Lobby not found.", ephemeral=True)
        if lobby["panel_channel_id"] and lobby["panel_message_id"]:
            ch = guild.get_channel(lobby["panel_channel_id"])
            if ch:
                try:
                    await ch.get_partial_message(lobby["panel_message_id"]).delete()
                except discord.HTTPException:
                    pass
        channel_ids = db.delete_lobby(self.lobby_id)
        removed = 0
        for cid in channel_ids:
            ch = guild.get_channel(cid)
            if ch:
                try:
                    await ch.delete(reason=f"Lobby {self.lobby_id} deleted by {interaction.user}")
                    removed += 1
                except discord.HTTPException:
                    pass
        db.log_admin(guild.id, interaction.user.id, "lobby_delete",
                     f"lobby={self.lobby_id} name={lobby['name']} channels_removed={removed}")
        self.stop()
        await interaction.followup.send(
            f"Lobby **#{self.lobby_id} {lobby['name']}** was deleted together with its registrations "
            f"and {removed} payment channel(s).", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.send_message("Deletion cancelled.", ephemeral=True)


class LobbyRoleView(AdminView):
    def __init__(self, lobby_id: int):
        super().__init__(timeout=300)
        self.lobby_id = lobby_id

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select the registration role", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        if not isinstance(role, discord.Role):
            role = interaction.guild.get_role(role.id)
        if role is None:
            return await reply(interaction, content="That role could not be found.")
        ok, msg = await action_set_role(interaction.guild, self.lobby_id, role, interaction.user)
        await reply(interaction, content=msg)


class PostChannelView(AdminView):
    def __init__(self, lobby_id: int):
        super().__init__(timeout=300)
        self.lobby_id = lobby_id

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Select the channel for the lobby panel", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.guild.get_channel(select.values[0].id)
        try:
            msg = await post_panel(interaction.guild, self.lobby_id, channel)
        except (ValueError, discord.HTTPException) as exc:
            return await interaction.followup.send(f"Could not post the panel: {exc}", ephemeral=True)
        db.log_admin(interaction.guild_id, interaction.user.id, "post_panel", f"lobby={self.lobby_id} channel={channel.id}")
        await interaction.followup.send(f"Panel posted in {channel.mention}: {msg.jump_url}", ephemeral=True)


# ----------------------------------------------------------------------------
# Lobby modals
# ----------------------------------------------------------------------------
def _lobby_inputs(lobby: Optional[sqlite3.Row] = None):
    d = lambda k, f=str: (f(lobby[k]) if lobby else None)
    return (
        discord.ui.TextInput(label="Lobby Name", min_length=2, max_length=40, default=d("name"), placeholder="BGMI Scrims Night"),
        discord.ui.TextInput(label="Entry Fee (INR)", max_length=9, default=d("entry_fee"), placeholder="100"),
        discord.ui.TextInput(label="Prize Pool (INR)", max_length=10, default=d("prize_pool"), placeholder="2000"),
        discord.ui.TextInput(label="Team Capacity (1-100)", max_length=3, default=d("capacity"), placeholder="25"),
        discord.ui.TextInput(label="Match Schedule", max_length=60, default=d("match_time"),
                             placeholder="15 Nov 2026, 9:00 PM IST"),
    )


def _parse_lobby_inputs(inputs):
    name = inputs[0].value.strip()
    fee = parse_int(inputs[1].value, "Entry fee", 0, 1_000_000)
    prize = parse_int(inputs[2].value, "Prize pool", 0, 100_000_000)
    cap = parse_int(inputs[3].value, "Team capacity", 1, 100)
    when = inputs[4].value.strip()
    if len(name) < 2 or not when:
        raise ValueError("Name and schedule cannot be empty.")
    return name, fee, prize, cap, when


class CreateLobbyModal(BaseModal, title="Create Lobby"):
    def __init__(self):
        super().__init__()
        self.inputs = _lobby_inputs()
        for i in self.inputs:
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        if await deny_non_admin(interaction):
            return
        try:
            name, fee, prize, cap, when = _parse_lobby_inputs(self.inputs)
        except ValueError as exc:
            return await reply(interaction, content=str(exc))
        lobby_id = db.create_lobby(interaction.guild_id, name, "", fee, prize, cap, when, None, interaction.user.id)
        db.log_admin(interaction.guild_id, interaction.user.id, "lobby_create", f"lobby={lobby_id} name={name}")
        await reply(
            interaction,
            content=f"Lobby **#{lobby_id} {name}** was created. Select its registration role below, then post the panel "
                    f"from the admin console or with `/lobby post`.",
            embed=lobby_embed(db.get_lobby(lobby_id)),
            view=LobbyRoleView(lobby_id))


class EditLobbyModal(BaseModal, title="Edit Lobby"):
    def __init__(self, lobby: sqlite3.Row):
        super().__init__()
        self.lobby_id = lobby["id"]
        self.inputs = _lobby_inputs(lobby)
        for i in self.inputs:
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        if await deny_non_admin(interaction):
            return
        try:
            name, fee, prize, cap, when = _parse_lobby_inputs(self.inputs)
        except ValueError as exc:
            return await reply(interaction, content=str(exc))
        lobby = db.get_lobby(self.lobby_id, interaction.guild_id)
        if not lobby:
            return await reply(interaction, content="Lobby not found.")
        active = db.lobby_counts(self.lobby_id)["active"]
        if cap < active:
            return await reply(interaction, content=f"Capacity cannot be lower than the {active} teams already registered.")
        db.update_lobby(self.lobby_id, name=name, entry_fee=fee, prize_pool=prize, capacity=cap, match_time=when)
        db.log_admin(interaction.guild_id, interaction.user.id, "lobby_edit", f"lobby={self.lobby_id}")
        await refresh_panel(interaction.guild, self.lobby_id)
        note = ""
        if fee != lobby["entry_fee"] and active:
            note = "\nThe new entry fee applies to new registrations. Existing registrations keep the fee they registered with."
        await reply(interaction, content="Lobby updated." + note, embed=lobby_embed(db.get_lobby(self.lobby_id)))


class DescriptionModal(BaseModal, title="Set Lobby Description"):
    def __init__(self, lobby: sqlite3.Row):
        super().__init__()
        self.lobby_id = lobby["id"]
        self.desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False,
                                         max_length=500, default=lobby["description"] or None)
        self.add_item(self.desc)

    async def on_submit(self, interaction: discord.Interaction):
        if await deny_non_admin(interaction):
            return
        db.update_lobby(self.lobby_id, description=self.desc.value.strip())
        db.log_admin(interaction.guild_id, interaction.user.id, "lobby_description", f"lobby={self.lobby_id}")
        await refresh_panel(interaction.guild, self.lobby_id)
        await reply(interaction, content="Lobby description updated.")


class CreateRoleModal(BaseModal, title="Create Lobby Role"):
    role_name = discord.ui.TextInput(label="Role Name", min_length=1, max_length=50, placeholder="Scrim Night Players")
    colour = discord.ui.TextInput(label="Colour (hex, optional)", required=False, max_length=7, placeholder="#2F6BFF")

    async def on_submit(self, interaction: discord.Interaction):
        if await deny_non_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            role = await action_create_role(interaction.guild, self.role_name.value.strip(), self.colour.value, interaction.user)
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"Discord refused to create the role: {exc}", ephemeral=True)

        async def assign(i: discord.Interaction, lobby_id: int):
            ok, msg = await action_set_role(i.guild, lobby_id, role, i.user)
            await reply(i, content=msg)

        if db.list_lobbies(interaction.guild_id):
            await interaction.followup.send(
                f"Role {role.mention} was created with no permissions. Pick a lobby to assign it to, or ignore this message.",
                view=LobbyPicker(interaction.guild_id, assign, placeholder="Assign this role to a lobby"),
                ephemeral=True)
        else:
            await interaction.followup.send(f"Role {role.mention} was created with no permissions.", ephemeral=True)


class RejectModal(BaseModal, title="Reject Payment"):
    reason = discord.ui.TextInput(label="Reason (shown to the captain)", style=discord.TextStyle.paragraph,
                                  required=False, max_length=300,
                                  placeholder="Payment not received / amount mismatch / unclear screenshot")

    async def on_submit(self, interaction: discord.Interaction):
        if await deny_non_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        reg = db.reg_by_channel(interaction.channel_id)
        if not reg:
            return await interaction.followup.send("This channel is not linked to a registration.", ephemeral=True)
        ok, text = await reject_registration(interaction.guild, reg["id"], interaction.user, self.reason.value.strip())
        await interaction.followup.send(text, ephemeral=True)


# ----------------------------------------------------------------------------
# Registration, payment channels, approval and rejection
# ----------------------------------------------------------------------------
async def create_payment_channel(guild: discord.Guild, lobby, reg_id: int, team: str,
                                 captain: discord.Member) -> discord.TextChannel:
    me = guild.me
    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        captain: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                             read_message_history=True, embed_links=True),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True,
                                        read_message_history=True, manage_channels=True, manage_messages=True),
    }
    admin_role = guild.get_role(ADMIN_ROLE_ID) if ADMIN_ROLE_ID else None
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                                             read_message_history=True)
    category = guild.get_channel(PAYMENT_CATEGORY_ID) if PAYMENT_CATEGORY_ID else None
    if category is not None and not isinstance(category, discord.CategoryChannel):
        category = None
    return await guild.create_text_channel(
        name=f"pay-{slugify(team)}-{reg_id}", category=category, overwrites=overwrites,
        topic=f"Private payment channel | Registration #{reg_id} | Lobby #{lobby['id']} {lobby['name']}",
        reason=f"Payment channel for registration #{reg_id}")


async def send_payment_instructions(channel: discord.TextChannel, guild: discord.Guild, lobby, reg_id: int,
                                    team: str, captain: discord.Member):
    e = discord.Embed(
        title="PAYMENT REQUIRED", color=NAVY, timestamp=datetime.now(timezone.utc),
        description="Pay the entry fee using the QR code below, then upload a clear screenshot of the successful "
                    "payment in this channel. Your slot is confirmed only after an administrator verifies the payment.")
    e.add_field(name="Team", value=team, inline=True)
    e.add_field(name="Registration ID", value=f"`#{reg_id}`", inline=True)
    e.add_field(name="Lobby", value=f"#{lobby['id']} {lobby['name']}", inline=True)
    e.add_field(name="Entry Fee", value=f"**{money(lobby['entry_fee'])}**", inline=True)
    e.add_field(name="Payment Status", value=PAY_LABEL["awaiting"], inline=True)
    e.add_field(name="Squad", value=players_text(db.get_players(reg_id)), inline=False)
    e.set_footer(text=FOOTER)

    kwargs: dict = {}
    qr = db.get_qr(guild.id)
    mention = captain.mention
    if qr and Path(qr).exists():
        f = discord.File(qr, filename=f"payment_qr{Path(qr).suffix}")
        e.set_image(url=f"attachment://{f.filename}")
        kwargs["file"] = f
    else:
        e.add_field(name="Payment QR", value="The payment QR has not been configured yet. Administrators have been notified.",
                    inline=False)
        mention += f" {admin_mention()}".rstrip()
    await channel.send(content=mention, embed=e, view=PaymentInfoView(), **kwargs)


class RegisterModal(BaseModal, title="Team Registration"):
    team_name = discord.ui.TextInput(label="Team Name", min_length=2, max_length=32)
    captain_bgmi = discord.ui.TextInput(label="Captain BGMI ID", min_length=5, max_length=15, placeholder="5123456789")
    player2 = discord.ui.TextInput(label="Player 2: Discord ID, BGMI ID", max_length=60,
                                   placeholder="123456789012345678, 5123456789")
    player3 = discord.ui.TextInput(label="Player 3: Discord ID, BGMI ID", max_length=60,
                                   placeholder="123456789012345678, 5123456789")
    player4 = discord.ui.TextInput(label="Player 4: Discord ID, BGMI ID", max_length=60,
                                   placeholder="123456789012345678, 5123456789")

    def __init__(self, lobby_id: int):
        super().__init__()
        self.lobby_id = lobby_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, captain = interaction.guild, interaction.user
        lobby = db.get_lobby(self.lobby_id, guild.id)
        if not lobby:
            return await interaction.followup.send("This lobby no longer exists.", ephemeral=True)
        if lobby["status"] != "open":
            return await interaction.followup.send("Registration is closed for this lobby.", ephemeral=True)

        errors: list[str] = []
        players: list[tuple[int, str]] = []
        cb = self.captain_bgmi.value.strip()
        if re.fullmatch(r"\d{5,15}", cb):
            players.append((captain.id, cb))
        else:
            errors.append("Captain: the BGMI ID must be 5 to 15 digits.")
        for label, field in (("Player 2", self.player2), ("Player 3", self.player3), ("Player 4", self.player4)):
            try:
                players.append(parse_player_line(field.value, label))
            except ValueError as exc:
                errors.append(str(exc))
        if errors:
            return await interaction.followup.send("Please fix the following:\n- " + "\n- ".join(errors), ephemeral=True)

        if len({p[0] for p in players}) != 4:
            errors.append("Each of the four players must have a different Discord account.")
        if len({p[1] for p in players}) != 4:
            errors.append("Each of the four players must have a different BGMI ID.")
        if errors:
            return await interaction.followup.send("\n".join(errors), ephemeral=True)

        for slot, (did, _) in enumerate(players, start=1):
            member = await get_member(guild, did)
            tag = "Captain" if slot == 1 else f"Player {slot}"
            if member is None:
                errors.append(f"{tag} (`{did}`) is not a member of this server.")
            elif member.bot:
                errors.append(f"{tag} (`{did}`) is a bot account.")
        if errors:
            return await interaction.followup.send("\n".join(errors), ephemeral=True)

        team = self.team_name.value.strip()
        reg_id, err = db.create_registration(self.lobby_id, team, players)
        if err:
            return await interaction.followup.send(err, ephemeral=True)

        try:
            channel = await create_payment_channel(guild, lobby, reg_id, team, captain)
            db.set_payment_channel(reg_id, channel.id)
            await send_payment_instructions(channel, guild, lobby, reg_id, team, captain)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.error("Payment channel creation failed for registration %s: %s", reg_id, exc)
            row = db.get_registration(reg_id)
            if row and row["payment_channel_id"]:
                ch = guild.get_channel(row["payment_channel_id"])
                if ch:
                    try:
                        await ch.delete(reason="Registration setup failed")
                    except discord.HTTPException:
                        pass
            db.delete_registration(reg_id)
            await refresh_panel(guild, self.lobby_id)
            return await interaction.followup.send(
                "Registration could not be completed because the private payment channel could not be created. "
                "Please contact an administrator. Your slot was not reserved.", ephemeral=True)

        await refresh_panel(guild, self.lobby_id)
        e = make_embed("REGISTRATION SUBMITTED", color=SUCCESS)
        e.add_field(name="Team", value=team, inline=True)
        e.add_field(name="Registration ID", value=f"`#{reg_id}`", inline=True)
        e.add_field(name="Next Step", value=f"Complete the payment in {channel.mention} and upload your screenshot.", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)


async def close_review_message(guild: discord.Guild, reg: sqlite3.Row) -> None:
    if not reg["review_message_id"] or not reg["payment_channel_id"]:
        return
    ch = guild.get_channel(reg["payment_channel_id"])
    if ch:
        try:
            await ch.get_partial_message(reg["review_message_id"]).edit(view=None)
        except discord.HTTPException:
            pass


async def handle_payment_upload(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return
    reg = db.reg_by_channel(message.channel.id)
    if not reg or message.author.id != reg["captain_id"]:
        return
    if reg["status"] not in ("pending_payment", "pending_review"):
        return
    images = [a for a in message.attachments if (a.content_type or "").startswith("image/")]
    if not images:
        return
    shot = images[0]
    if not db.submit_payment(reg["id"], shot.url, message.id):
        return
    lobby = db.get_lobby(reg["lobby_id"])
    pay = db.get_payment(reg["id"])
    await close_review_message(message.guild, reg)

    e = discord.Embed(title="PAYMENT REVIEW", color=NAVY, timestamp=datetime.now(timezone.utc),
                      description="A payment screenshot was submitted. Verify the actual payment in your account "
                                  "before approving. A screenshot alone does not grant access.")
    e.add_field(name="Team", value=reg["team_name"], inline=True)
    e.add_field(name="Registration ID", value=f"`#{reg['id']}`", inline=True)
    e.add_field(name="Lobby", value=f"#{lobby['id']} {lobby['name']}", inline=True)
    e.add_field(name="Amount Due", value=f"**{money(pay['amount'])}**", inline=True)
    e.add_field(name="Submitted By", value=message.author.mention, inline=True)
    e.add_field(name="Screenshot", value=f"[Open original]({message.jump_url})", inline=True)
    e.set_image(url=shot.url)
    e.set_footer(text=FOOTER)
    try:
        review = await message.channel.send(content=admin_mention() or None, embed=e, view=ReviewView())
        db.set_review_message(reg["id"], review.id)
        await message.add_reaction("\u2705")
    except discord.HTTPException as exc:
        log.warning("Could not post review controls for registration %s: %s", reg["id"], exc)
    await refresh_panel(message.guild, reg["lobby_id"])


async def assign_pending_roles(guild: discord.Guild, reg_id: int, role: discord.Role):
    results = []
    for p in db.get_players(reg_id):
        if p["role_assigned"]:
            results.append((p, True, "already assigned"))
            continue
        member = await get_member(guild, p["discord_id"])
        if member is None:
            db.set_player_role(p["id"], False, None)
            results.append((p, False, "not in server"))
            continue
        try:
            if role not in member.roles:
                await member.add_roles(role, reason=f"Atlanta Esports registration #{reg_id} approved")
            db.set_player_role(p["id"], True, role.id)
            results.append((p, True, "assigned"))
        except discord.Forbidden:
            db.set_player_role(p["id"], False, None)
            results.append((p, False, "missing permission or role hierarchy"))
        except discord.HTTPException as exc:
            db.set_player_role(p["id"], False, None)
            results.append((p, False, f"Discord error {exc.status}"))
    return results


async def approve_registration(guild: discord.Guild, reg_id: int, admin: discord.Member):
    reg = db.get_registration(reg_id, guild.id)
    if not reg:
        return False, "Registration not found."
    if reg["status"] == "rejected":
        return False, "This registration was rejected. The team must register again."
    lobby = db.get_lobby(reg["lobby_id"], guild.id)
    role = guild.get_role(lobby["role_id"]) if lobby and lobby["role_id"] else None
    if role is None:
        return False, "This lobby has no valid registration role. Set one with `/lobby role` first. Nothing was changed."
    problem = role_problem(guild, role)
    if problem:
        return False, problem + " Nothing was changed."

    resync = reg["status"] == "approved"
    if not resync and not db.mark_approved(reg_id, admin.id):
        return False, "The registration state changed. Refresh and try again."

    results = await assign_pending_roles(guild, reg_id, role)
    db.log_admin(guild.id, admin.id, "role_resync" if resync else "approve",
                 f"registration={reg_id} lobby={lobby['id']} role={role.id}")
    reg = db.get_registration(reg_id)
    await close_review_message(guild, reg)

    lines = []
    for p, ok, note in results:
        tag = "Captain" if p["slot"] == 1 else f"Player {p['slot']}"
        lines.append(f"{'Done' if ok else 'FAILED'}  |  {tag} <@{p['discord_id']}>  |  {note}")
    failed = [r for r in results if not r[1]]

    if reg["payment_channel_id"]:
        ch = guild.get_channel(reg["payment_channel_id"])
        if ch:
            e = make_embed("REGISTRATION APPROVED" if not resync else "ROLE SYNC", color=SUCCESS)
            e.add_field(name="Team", value=reg["team_name"], inline=True)
            e.add_field(name="Registration ID", value=f"`#{reg_id}`", inline=True)
            e.add_field(name="Role", value=role.mention, inline=True)
            e.add_field(name="Approved By", value=admin.mention, inline=True)
            e.add_field(name="Approved At", value=fmt_ts(reg["approved_at"]), inline=True)
            e.add_field(name="Role Assignment", value="\n".join(lines)[:1024], inline=False)
            try:
                await ch.send(content=f"<@{reg['captain_id']}>", embed=e)
            except discord.HTTPException:
                pass

    if not resync:
        dm = make_embed("REGISTRATION CONFIRMED",
                        f"Your team **{reg['team_name']}** is confirmed for **{lobby['name']}**.\n"
                        f"Match schedule: {lobby['match_time']}\nYou have been given the {role.name} role.", SUCCESS)
        for p in db.get_players(reg_id):
            member = await get_member(guild, p["discord_id"])
            if member:
                try:
                    await member.send(embed=dm)
                except discord.HTTPException:
                    pass
    await refresh_panel(guild, lobby["id"])

    summary = f"Registration **#{reg_id} {reg['team_name']}** {'role sync finished' if resync else 'approved'}.\n" + "\n".join(lines)
    if failed:
        summary += "\nSome roles were not assigned. Fix the issue and run `/lobby approve` again to retry only those players."
    return True, summary


async def reject_registration(guild: discord.Guild, reg_id: int, admin: discord.Member, reason: str):
    reg = db.get_registration(reg_id, guild.id)
    if not reg:
        return False, "Registration not found."
    if reg["status"] == "approved":
        return False, "This registration is already approved and cannot be rejected."
    if reg["status"] == "rejected":
        return False, "This registration is already rejected."
    reason = reason or "No reason provided."
    if not db.mark_rejected(reg_id, admin.id, reason):
        return False, "The registration state changed. Refresh and try again."
    db.log_admin(guild.id, admin.id, "reject", f"registration={reg_id} reason={reason}")
    reg = db.get_registration(reg_id)
    await close_review_message(guild, reg)

    if reg["payment_channel_id"]:
        ch = guild.get_channel(reg["payment_channel_id"])
        if ch:
            e = make_embed("REGISTRATION REJECTED", color=DANGER)
            e.add_field(name="Team", value=reg["team_name"], inline=True)
            e.add_field(name="Registration ID", value=f"`#{reg_id}`", inline=True)
            e.add_field(name="Rejected By", value=admin.mention, inline=True)
            e.add_field(name="Reason", value=reason[:1000], inline=False)
            e.add_field(name="What now?", value="Your slot was released. You may register again if the lobby is still open.", inline=False)
            try:
                await ch.send(content=f"<@{reg['captain_id']}>", embed=e)
                captain = guild.get_member(reg["captain_id"])
                if captain:
                    await ch.set_permissions(captain, view_channel=True, send_messages=False, read_message_history=True)
            except discord.HTTPException:
                pass
    captain = await get_member(guild, reg["captain_id"])
    if captain:
        try:
            await captain.send(embed=make_embed(
                "REGISTRATION REJECTED",
                f"Your registration for team **{reg['team_name']}** was rejected.\nReason: {reason}", DANGER))
        except discord.HTTPException:
            pass
    await refresh_panel(guild, reg["lobby_id"])
    return True, f"Registration **#{reg_id} {reg['team_name']}** was rejected and the slot was released."


# ----------------------------------------------------------------------------
# Persistent views
# ----------------------------------------------------------------------------
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _lobby(self, interaction: discord.Interaction):
        lobby = db.lobby_by_message(interaction.message.id)
        if not lobby:
            await reply(interaction, content="This panel is no longer linked to an active lobby.")
        return lobby

    @discord.ui.button(label="Register Team", style=discord.ButtonStyle.primary, custom_id="atl:panel:register", row=0)
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await self._lobby(interaction)
        if not lobby:
            return
        counts = db.lobby_counts(lobby["id"])
        if lobby["status"] != "open":
            return await reply(interaction, content="Registration for this lobby is closed.")
        if counts["active"] >= lobby["capacity"]:
            return await reply(interaction, content="This lobby is full.")
        await interaction.response.send_modal(RegisterModal(lobby["id"]))

    @discord.ui.button(label="View All Lobbies", style=discord.ButtonStyle.secondary, custom_id="atl:panel:all", row=0)
    async def view_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = db.list_lobbies(interaction.guild_id)
        e = make_embed(f"{BRAND}  |  All Lobbies", color=NAVY)
        if not rows:
            e.description = "No lobbies are available right now."
        for r in list(reversed(rows))[:25]:
            c = db.lobby_counts(r["id"])
            e.add_field(
                name=f"#{r['id']}  {r['name']}",
                value=f"{lobby_status_text(r, c)}  |  Fee {money(r['entry_fee'])}  |  Prize {money(r['prize_pool'])}\n"
                      f"{c['active']}/{r['capacity']} teams  |  {r['match_time']}",
                inline=False)
        await reply(interaction, embed=e)

    @discord.ui.button(label="My Registration", style=discord.ButtonStyle.secondary, custom_id="atl:panel:mine", row=0)
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await self._lobby(interaction)
        if not lobby:
            return
        regs = db.regs_for_user(lobby["id"], interaction.user.id)
        if not regs:
            return await reply(interaction, content="You are not part of any registration in this lobby.")
        embeds = []
        for r in regs[:5]:
            e = make_embed(f"Registration #{r['id']}  |  {r['team_name']}", color=NAVY)
            e.add_field(name="Status", value=STATUS_LABEL[r["status"]], inline=True)
            e.add_field(name="Payment", value=PAY_LABEL.get(r["pay_status"], "-"), inline=True)
            e.add_field(name="Registered", value=fmt_ts(r["created_at"]), inline=True)
            e.add_field(name="Squad", value=players_text(db.get_players(r["id"])), inline=False)
            if r["status"] == "rejected" and r["reject_reason"]:
                e.add_field(name="Rejection Reason", value=r["reject_reason"][:1000], inline=False)
            if r["captain_id"] == interaction.user.id and r["payment_channel_id"] and r["status"] in ("pending_payment", "pending_review"):
                e.add_field(name="Payment Channel", value=f"<#{r['payment_channel_id']}>", inline=False)
            embeds.append(e)
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

    @discord.ui.button(label="Lobby Information", style=discord.ButtonStyle.secondary, custom_id="atl:panel:info", row=1)
    async def info(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await self._lobby(interaction)
        if not lobby:
            return
        e = lobby_embed(lobby)
        e.add_field(name="How to join",
                    value="1. Press Register Team and submit your squad.\n2. Pay in your private payment channel.\n"
                          "3. Upload the screenshot.\n4. Receive the role after an administrator verifies payment.",
                    inline=False)
        await reply(interaction, embed=e)

    @discord.ui.button(label="Rules", style=discord.ButtonStyle.secondary, custom_id="atl:panel:rules", row=1)
    async def rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(interaction, embed=make_embed(f"{BRAND}  |  Rules", DEFAULT_RULES, NAVY))


class PaymentInfoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Payment Status", style=discord.ButtonStyle.secondary, custom_id="atl:pay:status")
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        reg = db.reg_by_channel(interaction.channel_id)
        if not reg:
            return await reply(interaction, content="This channel is not linked to a registration.")
        if interaction.user.id != reg["captain_id"] and not is_admin(interaction.user):
            return await reply(interaction, content="Only the captain and administrators can view this.")
        pay = db.get_payment(reg["id"])
        e = make_embed(f"Registration #{reg['id']}  |  {reg['team_name']}", color=NAVY)
        e.add_field(name="Registration", value=STATUS_LABEL[reg["status"]], inline=True)
        e.add_field(name="Payment", value=PAY_LABEL[pay["status"]], inline=True)
        e.add_field(name="Amount", value=money(pay["amount"]), inline=True)
        if pay["submitted_at"]:
            e.add_field(name="Screenshot Submitted", value=fmt_ts(pay["submitted_at"]), inline=True)
        if reg["status"] == "rejected" and reg["reject_reason"]:
            e.add_field(name="Reason", value=reg["reject_reason"][:1000], inline=False)
        await reply(interaction, embed=e)


class ReviewView(AdminView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve Payment", style=discord.ButtonStyle.success, custom_id="atl:review:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        reg = db.reg_by_channel(interaction.channel_id)
        if not reg:
            return await interaction.followup.send("This channel is not linked to a registration.", ephemeral=True)
        ok, text = await approve_registration(interaction.guild, reg["id"], interaction.user)
        await interaction.followup.send(text[:1900], ephemeral=True)

    @discord.ui.button(label="Reject Payment", style=discord.ButtonStyle.danger, custom_id="atl:review:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectModal())


class AdminConsoleView(AdminView):
    def __init__(self):
        super().__init__(timeout=None)

    # Row 0: lifecycle
    @discord.ui.button(label="Create Lobby", style=discord.ButtonStyle.primary, custom_id="atl:admin:create", row=0)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CreateLobbyModal())

    @discord.ui.button(label="Edit Lobby", style=discord.ButtonStyle.secondary, custom_id="atl:admin:edit", row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def h(i: discord.Interaction, lobby_id: int):
            lobby = db.get_lobby(lobby_id, i.guild_id)
            if not lobby:
                return await reply(i, content="Lobby not found.")
            await i.response.send_modal(EditLobbyModal(lobby))
        await open_picker(interaction, h, text="Select the lobby to edit (name, fee, prize pool, capacity, schedule).")

    @discord.ui.button(label="Delete Lobby", style=discord.ButtonStyle.danger, custom_id="atl:admin:delete", row=0)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def h(i: discord.Interaction, lobby_id: int):
            lobby = db.get_lobby(lobby_id, i.guild_id)
            if not lobby:
                return await reply(i, content="Lobby not found.")
            await reply(i, content=f"Delete **#{lobby_id} {lobby['name']}** with all registrations, payments and payment channels? This cannot be undone.",
                        view=ConfirmDeleteView(lobby_id))
        await open_picker(interaction, h, text="Select the lobby to delete.")

    @discord.ui.button(label="Open Lobby", style=discord.ButtonStyle.success, custom_id="atl:admin:open", row=0)
    async def open_(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_picker(interaction, lambda i, l: action_set_status(i, l, "open"), text="Select the lobby to open.")

    @discord.ui.button(label="Close Lobby", style=discord.ButtonStyle.secondary, custom_id="atl:admin:close", row=0)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_picker(interaction, lambda i, l: action_set_status(i, l, "closed"), text="Select the lobby to close.")

    # Row 1: configuration
    @discord.ui.button(label="Set Description", style=discord.ButtonStyle.secondary, custom_id="atl:admin:desc", row=1)
    async def desc(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def h(i: discord.Interaction, lobby_id: int):
            lobby = db.get_lobby(lobby_id, i.guild_id)
            if not lobby:
                return await reply(i, content="Lobby not found.")
            await i.response.send_modal(DescriptionModal(lobby))
        await open_picker(interaction, h, text="Select the lobby to describe.")

    @discord.ui.button(label="Set Role", style=discord.ButtonStyle.secondary, custom_id="atl:admin:role", row=1)
    async def role(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def h(i: discord.Interaction, lobby_id: int):
            await reply(i, content=f"Choose the role for lobby **#{lobby_id}** from your server roles.", view=LobbyRoleView(lobby_id))
        await open_picker(interaction, h, text="Select the lobby whose role you want to change.")

    @discord.ui.button(label="Create Role", style=discord.ButtonStyle.secondary, custom_id="atl:admin:newrole", row=1)
    async def new_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CreateRoleModal())

    @discord.ui.button(label="Upload Payment QR", style=discord.ButtonStyle.secondary, custom_id="atl:admin:qr", row=1)
    async def qr(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(interaction, content="Upload the shared payment QR image as a message in this channel within 90 seconds. "
                                         "The message will be deleted after saving. You can also use `/lobby setqr`.")

        def check(m: discord.Message) -> bool:
            return (m.author.id == interaction.user.id and m.channel.id == interaction.channel_id
                    and any((a.content_type or "").startswith("image/") for a in m.attachments))
        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=90)
        except asyncio.TimeoutError:
            return await interaction.followup.send("Timed out waiting for the image.", ephemeral=True)
        image = next(a for a in msg.attachments if (a.content_type or "").startswith("image/"))
        ok, text = await action_save_qr(interaction.guild, image, interaction.user)
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        await interaction.followup.send(text, ephemeral=True)

    @discord.ui.button(label="Post Panel", style=discord.ButtonStyle.primary, custom_id="atl:admin:post", row=1)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def h(i: discord.Interaction, lobby_id: int):
            await reply(i, content=f"Choose the channel for the panel of lobby **#{lobby_id}**.", view=PostChannelView(lobby_id))
        await open_picker(interaction, h, text="Select the lobby to post.")

    # Row 2: monitoring
    @discord.ui.button(label="Registrations", style=discord.ButtonStyle.secondary, custom_id="atl:admin:regs", row=2)
    async def regs(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def h(i: discord.Interaction, lobby_id: int):
            lobby = db.get_lobby(lobby_id, i.guild_id)
            if not lobby:
                return await reply(i, content="Lobby not found.")
            await reply(i, embed=build_registrations_embed(lobby))
        await open_picker(interaction, h, text="Select a lobby to view its teams.")

    @discord.ui.button(label="Pending Payments", style=discord.ButtonStyle.secondary, custom_id="atl:admin:pending", row=2)
    async def pending(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(interaction, embed=build_pending_embed(interaction.guild_id))

    @discord.ui.button(label="Statistics", style=discord.ButtonStyle.secondary, custom_id="atl:admin:stats", row=2)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(interaction, embed=build_stats_embed(interaction.guild_id))

    @discord.ui.button(label="Export Team Data", style=discord.ButtonStyle.secondary, custom_id="atl:admin:export", row=2)
    async def export(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def h(i: discord.Interaction, lobby_id: int):
            db.log_admin(i.guild_id, i.user.id, "export", f"lobby={lobby_id or 'all'}")
            await reply(i, content="Team data export (contains player IDs; keep it private).",
                        file=build_export_file(i.guild_id, lobby_id or None))
        await open_picker(interaction, h, include_all=True, text="Select what to export.")


# ----------------------------------------------------------------------------
# Slash commands
# ----------------------------------------------------------------------------
async def lobby_autocomplete(interaction: discord.Interaction, current: str):
    out = []
    for r in db.list_lobbies(interaction.guild_id or 0):
        label = f"#{r['id']} {r['name']}"
        if current.lower() in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=r["id"]))
    return out[:25]


async def registration_autocomplete(interaction: discord.Interaction, current: str):
    out = []
    for r in db.pending_regs(interaction.guild_id or 0):
        label = f"#{r['id']} {r['team_name']} ({r['lobby_name']})"
        if current.lower() in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=r["id"]))
    return out[:25]


class LobbyGroup(app_commands.Group, name="lobby", description="Atlanta Esports lobby management", guild_only=True):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return is_admin(interaction.user)

    @app_commands.command(name="admin", description="Open the administrator console")
    async def admin(self, interaction: discord.Interaction):
        e = make_embed(f"{BRAND}  |  Administrator Console",
                       "Manage lobbies, roles, payments and data. Every control is permission-protected and "
                       "responses are private. Post this console in an administrator-only channel.", NAVY)
        await interaction.response.send_message(embed=e, view=AdminConsoleView())

    @app_commands.command(name="create", description="Create a new lobby")
    @app_commands.describe(name="Lobby name", entry_fee="Entry fee in INR", prize_pool="Prize pool in INR",
                           capacity="Maximum number of teams", match_time="Match date and time",
                           description="Optional description", role="Role given after payment approval")
    async def create(self, interaction: discord.Interaction, name: app_commands.Range[str, 2, 40],
                     entry_fee: app_commands.Range[int, 0, 1000000], prize_pool: app_commands.Range[int, 0, 100000000],
                     capacity: app_commands.Range[int, 1, 100], match_time: app_commands.Range[str, 2, 60],
                     description: Optional[app_commands.Range[str, 0, 500]] = None,
                     role: Optional[discord.Role] = None):
        if role:
            problem = role_problem(interaction.guild, role)
            if problem:
                return await interaction.response.send_message(problem, ephemeral=True)
        lobby_id = db.create_lobby(interaction.guild_id, name, description or "", entry_fee, prize_pool, capacity,
                                   match_time, role.id if role else None, interaction.user.id)
        db.log_admin(interaction.guild_id, interaction.user.id, "lobby_create", f"lobby={lobby_id} name={name}")
        await interaction.response.send_message(
            content=f"Lobby **#{lobby_id}** created. Post its panel with `/lobby post`."
                    + ("" if role else " No role is set yet; use `/lobby role`."),
            embed=lobby_embed(db.get_lobby(lobby_id)), ephemeral=True)

    @app_commands.command(name="edit", description="Edit a lobby")
    @app_commands.autocomplete(lobby_id=lobby_autocomplete)
    async def edit(self, interaction: discord.Interaction, lobby_id: int,
                   name: Optional[app_commands.Range[str, 2, 40]] = None,
                   entry_fee: Optional[app_commands.Range[int, 0, 1000000]] = None,
                   prize_pool: Optional[app_commands.Range[int, 0, 100000000]] = None,
                   capacity: Optional[app_commands.Range[int, 1, 100]] = None,
                   match_time: Optional[app_commands.Range[str, 2, 60]] = None,
                   description: Optional[app_commands.Range[str, 0, 500]] = None):
        lobby = db.get_lobby(lobby_id, interaction.guild_id)
        if not lobby:
            return await interaction.response.send_message("Lobby not found.", ephemeral=True)
        changes = {k: v for k, v in dict(name=name, entry_fee=entry_fee, prize_pool=prize_pool, capacity=capacity,
                                         match_time=match_time, description=description).items() if v is not None}
        if not changes:
            return await interaction.response.send_message("Provide at least one field to change.", ephemeral=True)
        if "capacity" in changes:
            active = db.lobby_counts(lobby_id)["active"]
            if changes["capacity"] < active:
                return await interaction.response.send_message(
                    f"Capacity cannot be lower than the {active} teams already registered.", ephemeral=True)
        db.update_lobby(lobby_id, **changes)
        db.log_admin(interaction.guild_id, interaction.user.id, "lobby_edit", f"lobby={lobby_id} {list(changes)}")
        await refresh_panel(interaction.guild, lobby_id)
        await interaction.response.send_message("Lobby updated.", embed=lobby_embed(db.get_lobby(lobby_id)), ephemeral=True)

    @app_commands.command(name="delete", description="Delete a lobby and all of its registrations")
    @app_commands.autocomplete(lobby_id=lobby_autocomplete)
    async def delete(self, interaction: discord.Interaction, lobby_id: int):
        lobby = db.get_lobby(lobby_id, interaction.guild_id)
        if not lobby:
            return await interaction.response.send_message("Lobby not found.", ephemeral=True)
        await interaction.response.send_message(
            f"Delete **#{lobby_id} {lobby['name']}** with all registrations, payments and payment channels? This cannot be undone.",
            view=ConfirmDeleteView(lobby_id), ephemeral=True)

    @app_commands.command(name="post", description="Post the public lobby panel")
    @app_commands.autocomplete(lobby_id=lobby_autocomplete)
    async def post(self, interaction: discord.Interaction, lobby_id: int, channel: Optional[discord.TextChannel] = None):
        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel
        try:
            msg = await post_panel(interaction.guild, lobby_id, target)
        except (ValueError, discord.HTTPException) as exc:
            return await interaction.followup.send(f"Could not post the panel: {exc}", ephemeral=True)
        db.log_admin(interaction.guild_id, interaction.user.id, "post_panel", f"lobby={lobby_id} channel={target.id}")
        await interaction.followup.send(f"Panel posted: {msg.jump_url}", ephemeral=True)

    @app_commands.command(name="list", description="List all lobbies")
    async def list(self, interaction: discord.Interaction):
        rows = db.list_lobbies(interaction.guild_id)
        e = make_embed(f"{BRAND}  |  Lobbies", color=NAVY)
        if not rows:
            e.description = "No lobbies yet. Use `/lobby create`."
        for r in list(reversed(rows))[:25]:
            c = db.lobby_counts(r["id"])
            role = f"<@&{r['role_id']}>" if r["role_id"] else "no role"
            e.add_field(name=f"#{r['id']}  {r['name']}",
                        value=f"{lobby_status_text(r, c)}  |  Fee {money(r['entry_fee'])}  |  Prize {money(r['prize_pool'])}\n"
                              f"{c['active']}/{r['capacity']} teams  |  {r['match_time']}  |  {role}", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="open", description="Open registration for a lobby")
    @app_commands.autocomplete(lobby_id=lobby_autocomplete)
    async def open(self, interaction: discord.Interaction, lobby_id: int):
        await action_set_status(interaction, lobby_id, "open")

    @app_commands.command(name="close", description="Close registration for a lobby")
    @app_commands.autocomplete(lobby_id=lobby_autocomplete)
    async def close(self, interaction: discord.Interaction, lobby_id: int):
        await action_set_status(interaction, lobby_id, "closed")

    @app_commands.command(name="role", description="Select an existing role for a lobby")
    @app_commands.autocomplete(lobby_id=lobby_autocomplete)
    async def role(self, interaction: discord.Interaction, lobby_id: int, role: discord.Role):
        ok, msg = await action_set_role(interaction.guild, lobby_id, role, interaction.user)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="createrole", description="Create a new role, optionally assigning it to a lobby")
    @app_commands.autocomplete(lobby_id=lobby_autocomplete)
    async def createrole(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 50],
                         colour: Optional[str] = None, lobby_id: Optional[int] = None):
        await interaction.response.defer(ephemeral=True)
        if lobby_id is not None and not db.get_lobby(lobby_id, interaction.guild_id):
            return await interaction.followup.send("Lobby not found.", ephemeral=True)
        try:
            role = await action_create_role(interaction.guild, name, colour or "", interaction.user)
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"Discord refused to create the role: {exc}", ephemeral=True)
        text = f"Role {role.mention} created with no permissions."
        if lobby_id is not None:
            ok, msg = await action_set_role(interaction.guild, lobby_id, role, interaction.user)
            text += "\n" + msg
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="setqr", description="Upload or replace the shared payment QR")
    async def setqr(self, interaction: discord.Interaction, image: discord.Attachment):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await action_save_qr(interaction.guild, image, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="registrations", description="View the teams registered in a lobby")
    @app_commands.autocomplete(lobby_id=lobby_autocomplete)
    async def registrations(self, interaction: discord.Interaction, lobby_id: int):
        lobby = db.get_lobby(lobby_id, interaction.guild_id)
        if not lobby:
            return await interaction.response.send_message("Lobby not found.", ephemeral=True)
        await interaction.response.send_message(embed=build_registrations_embed(lobby), ephemeral=True)

    @app_commands.command(name="approve", description="Approve a payment and assign roles (or retry failed role assignments)")
    @app_commands.autocomplete(registration_id=registration_autocomplete)
    async def approve(self, interaction: discord.Interaction, registration_id: int):
        await interaction.response.defer(ephemeral=True)
        ok, text = await approve_registration(interaction.guild, registration_id, interaction.user)
        await interaction.followup.send(text[:1900], ephemeral=True)

    @app_commands.command(name="reject", description="Reject a registration payment")
    @app_commands.autocomplete(registration_id=registration_autocomplete)
    async def reject(self, interaction: discord.Interaction, registration_id: int,
                     reason: Optional[app_commands.Range[str, 0, 300]] = None):
        await interaction.response.defer(ephemeral=True)
        ok, text = await reject_registration(interaction.guild, registration_id, interaction.user, reason or "")
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="stats", description="Show administrator statistics")
    async def stats(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_stats_embed(interaction.guild_id), ephemeral=True)

    @app_commands.command(name="export", description="Export team data as CSV")
    @app_commands.autocomplete(lobby_id=lobby_autocomplete)
    async def export(self, interaction: discord.Interaction, lobby_id: Optional[int] = None):
        if lobby_id is not None and not db.get_lobby(lobby_id, interaction.guild_id):
            return await interaction.response.send_message("Lobby not found.", ephemeral=True)
        db.log_admin(interaction.guild_id, interaction.user.id, "export", f"lobby={lobby_id or 'all'}")
        await interaction.response.send_message(
            "Team data export (contains player IDs; keep it private).",
            file=build_export_file(interaction.guild_id, lobby_id), ephemeral=True)


# ----------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------
class AtlantaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # required to detect payment screenshots (enable in the Developer Portal)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        self.add_view(PanelView())
        self.add_view(PaymentInfoView())
        self.add_view(ReviewView())
        self.add_view(AdminConsoleView())
        self.tree.add_command(LobbyGroup())
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Commands synced globally (can take a while to appear)")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) | database: %s", self.user, self.user.id, DB_PATH)
        if not ADMIN_ROLE_ID:
            log.warning("ADMIN_ROLE_ID is not set: only members with the Administrator permission can manage lobbies.")

    async def on_message(self, message: discord.Message) -> None:
        try:
            await handle_payment_upload(message)
        except Exception:
            log.exception("Error while handling a payment upload")


bot = AtlantaBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "You are not authorised to use this command."
    else:
        log.exception("Command error", exc_info=error)
        msg = "Something went wrong while running that command. Please try again."
    try:
        await reply(interaction, content=msg)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN, log_handler=None)
