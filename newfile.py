"""
ATLANTA ESPORTS BOT — discord.py 2.x
Single-file esports management bot with interactive category Help, SQLite storage,
team registration, tournament/scrim management, match rooms/results, moderation,
tickets, logs, and owner-only developer tools.

Install: pip install -U discord.py
Environment variables:
  DISCORD_TOKEN=your_discord_bot_token
  OWNER_IDS=123456789012345678,987654321098765432   (optional, comma-separated)
Run: python atlanta_esports_bot.py
Enable Message Content, Server Members, and Presence intents in the Discord Developer Portal.
Use only in servers where you have permission. Test admin features in a test server first.
"""
import os
import sqlite3
import datetime as dt
from typing import Optional

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN", "PASTE_TOKEN_HERE")
OWNER_IDS = {int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()}
DB_FILE = os.getenv("DATABASE", "atlanta_esports.sqlite3")
DEFAULT_PREFIX = "?"
COLOR = 0x172554
ACCENT = 0x3B82F6

# ----------------------------- database -----------------------------
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(guild_id INTEGER PRIMARY KEY, prefix TEXT NOT NULL DEFAULT '¿', log_channel INTEGER, announcement_channel INTEGER);
        CREATE TABLE IF NOT EXISTS teams(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL, captain_id INTEGER NOT NULL, players TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL, slots INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_by INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS registrations(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, team_id INTEGER NOT NULL, registered_at TEXT NOT NULL, UNIQUE(event_id, team_id));
        CREATE TABLE IF NOT EXISTS matches(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, event_id INTEGER NOT NULL, team_a INTEGER NOT NULL, team_b INTEGER NOT NULL, match_time TEXT NOT NULL DEFAULT 'TBD', room_id TEXT, room_password TEXT, winner_team INTEGER, status TEXT NOT NULL DEFAULT 'scheduled', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS warnings(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS wins(guild_id INTEGER NOT NULL, team_id INTEGER NOT NULL, wins INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id, team_id));
        """)
        # Correct a friendly default if the database was created by an older build.
        c.execute("UPDATE settings SET prefix='!' WHERE prefix='! '")


def get_prefix_for(guild_id: Optional[int]) -> str:
    if guild_id is None:
        return DEFAULT_PREFIX
    with db() as c:
        row = c.execute("SELECT prefix FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
        return row["prefix"] if row else DEFAULT_PREFIX


async def dynamic_prefix(bot, message):
    return get_prefix_for(message.guild.id if message.guild else None)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=dynamic_prefix, intents=intents, help_command=None)


def embed(title: str, description: str = "", color: int = COLOR) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    e.set_footer(text="ATLANTA ESPORTS • Competitive Management")
    return e


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def is_owner_id(user_id: int) -> bool:
    return user_id in OWNER_IDS


def admin_check():
    return commands.has_guild_permissions(administrator=True)


async def log_action(guild: discord.Guild, title: str, description: str):
    with db() as c:
        row = c.execute("SELECT log_channel FROM settings WHERE guild_id=?", (guild.id,)).fetchone()
    if row and row["log_channel"]:
        channel = guild.get_channel(row["log_channel"])
        if channel:
            try:
                await channel.send(embed=embed(title, description, ACCENT))
            except discord.HTTPException:
                pass


def team_row(guild_id: int, team_id: int):
    with db() as c:
        return c.execute("SELECT * FROM teams WHERE guild_id=? AND id=?", (guild_id, team_id)).fetchone()


def event_row(guild_id: int, event_id: int):
    with db() as c:
        return c.execute("SELECT * FROM events WHERE guild_id=? AND id=?", (guild_id, event_id)).fetchone()

# ----------------------------- help categories -----------------------------
CATEGORIES = {
    "general": ("📌", "General & Setup", [
        ("help", "Open interactive categorized help", "Everyone"), ("ping", "Check latency", "Everyone"),
        ("prefix <symbol>", "Change this server's prefix", "Manage Server"),
        ("setup", "Create starter esports channels and roles", "Administrator"),
        ("setlog #channel", "Choose the bot log channel", "Administrator"),
        ("setannounce #channel", "Choose the announcement destination", "Administrator"),
        ("announce <message>", "Post an announcement to configured channel", "Manage Server")]),
    "teams": ("👥", "Teams & Rosters", [
        ("team create <name>", "Register a team; creator becomes captain", "Everyone"),
        ("team list", "List registered teams", "Everyone"), ("team info <team_id>", "View team roster", "Everyone"),
        ("team add <team_id> @member", "Captain adds a player", "Team Captain"),
        ("team leave", "Leave your current team", "Everyone"),
        ("team delete <team_id>", "Delete your team", "Team Captain/Admin")]),
    "events": ("🏆", "Tournaments & Scrims", [
        ("event create <name> <mode> <slots>", "Create a tournament", "Manage Server"),
        ("event list", "List tournaments", "Everyone"), ("event join <event_id> <team_id>", "Register a team", "Captain"),
        ("event close <event_id>", "Close tournament registrations", "Manage Server"),
        ("scrim create <name> <mode> <slots>", "Create a scrim", "Manage Server"),
        ("scrim list", "List scrims", "Everyone"), ("scrim join <event_id> <team_id>", "Register a team for a scrim", "Captain")]),
    "matches": ("🎮", "Matches & Results", [
        ("match create <event_id> <team_a> <team_b> <time>", "Schedule a match", "Manage Server"),
        ("match list", "Show scheduled matches", "Everyone"), ("match room <match_id> <room_id> <password>", "Set match room details", "Manage Server"),
        ("match result <match_id> <winner_team_id>", "Record match winner", "Manage Server"),
        ("leaderboard", "Show teams ranked by recorded wins", "Everyone")]),
    "moderation": ("🛡️", "Moderation", [
        ("mod warn @member <reason>", "Issue and log a warning", "Moderate Members"),
        ("mod warnings @member", "View warning count and reasons", "Moderate Members"),
        ("mod timeout @member <minutes> <reason>", "Timeout a member", "Moderate Members"),
        ("mod kick @member <reason>", "Kick a member", "Kick Members"),
        ("mod ban @member <reason>", "Ban a member", "Ban Members")]),
    "tickets": ("🎫", "Support Tickets", [
        ("ticket panel", "Post a ticket creation button", "Manage Channels"),
        ("ticket create", "Open a private support ticket", "Everyone"),
        ("ticket close", "Close the current ticket", "Ticket Owner/Staff")]),
    "developer": ("⚙️", "Developer (Owner Only)", [
        ("dev stats", "Show bot guild/user/database stats", "Bot Owner"),
        ("dev sync", "Sync slash commands", "Bot Owner"),
        ("dev shutdown", "Safely stop the bot", "Bot Owner")]),
}


def main_help(prefix: str) -> discord.Embed:
    e = embed("ATLANTA ESPORTS • Command Center", "**Tournament operations. Team management. Competitive control.**\n\nSelect a category from the menu to view its commands.")
    e.add_field(name="Available Modules", value="\n".join(f"{v[0]} **{v[1]}**" for v in CATEGORIES.values()), inline=False)
    e.add_field(name="Quick Start", value=f"`{prefix}setup` → server layout\n`{prefix}team create <name>` → register a team\n`{prefix}help` → browse all command categories", inline=False)
    e.set_footer(text=f"Prefix: {prefix} • Some commands require server permissions")
    return e


def category_help(key: str, prefix: str, requested_by: str) -> discord.Embed:
    icon, name, rows = CATEGORIES[key]
    desc = "\n".join(f"`{prefix}{cmd}` — {about} • **{perm}**" for cmd, about, perm in rows)
    e = embed(f"{icon} {name}", desc, ACCENT)
    e.set_footer(text=f"Requested by {requested_by} • Prefix: {prefix}")
    return e


class HelpView(discord.ui.View):
    def __init__(self, author_id: int, prefix: str):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.prefix = prefix
        options = [discord.SelectOption(label=v[1], value=k, emoji=v[0]) for k, v in CATEGORIES.items()]
        select = discord.ui.Select(placeholder="Choose a command category…", options=options)
        select.callback = self.select_category
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This help panel belongs to another user. Run the help command to open your own.", ephemeral=True)
            return False
        return True

    async def select_category(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        await interaction.response.edit_message(embed=category_help(key, self.prefix, str(interaction.user)), view=self)

# ----------------------------- general -----------------------------
@bot.event
async def on_ready():
    init_db()
    if not getattr(bot, "_ticket_view_registered", False):
        bot.add_view(TicketCreateView())
        bot._ticket_view_registered = True
    print(f"Logged in as {bot.user} | guilds={len(bot.guilds)}")

@bot.command(name="help")
async def help_command(ctx):
    prefix = get_prefix_for(ctx.guild.id if ctx.guild else None)
    await ctx.send(embed=main_help(prefix), view=HelpView(ctx.author.id, prefix))

@bot.command()
async def ping(ctx):
    await ctx.send(embed=embed("🏓 Pong", f"Latency: **{round(bot.latency * 1000)} ms**"))

@bot.command()
@commands.has_guild_permissions(manage_guild=True)
async def prefix(ctx, symbol: str):
    if not 1 <= len(symbol) <= 3 or any(ch.isspace() for ch in symbol):
        return await ctx.reply("Prefix must be 1–3 non-space characters.")
    with db() as c:
        c.execute("INSERT INTO settings(guild_id,prefix) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET prefix=excluded.prefix", (ctx.guild.id, symbol))
    await ctx.reply(embed=embed("Prefix Updated", f"New prefix: `{symbol}`"))

@bot.command()
@commands.has_guild_permissions(administrator=True)
async def setup(ctx):
    """Create basic esports channels and roles if they do not already exist."""
    category_names = ["ESPORTS INFORMATION", "TEAM REGISTRATION", "TOURNAMENT HUB", "STAFF OPERATIONS"]
    channel_specs = {
        "ESPORTS INFORMATION": [("announcements", discord.ChannelType.text), ("rules", discord.ChannelType.text), ("leaderboard", discord.ChannelType.text)],
        "TEAM REGISTRATION": [("team-registration", discord.ChannelType.text), ("team-help", discord.ChannelType.text)],
        "TOURNAMENT HUB": [("tournament-info", discord.ChannelType.text), ("scrim-info", discord.ChannelType.text), ("match-rooms", discord.ChannelType.text)],
        "STAFF OPERATIONS": [("staff-logs", discord.ChannelType.text), ("staff-chat", discord.ChannelType.text)],
    }
    existing_cats = {cat.name: cat for cat in ctx.guild.categories}
    made = []
    for cat_name in category_names:
        cat = existing_cats.get(cat_name) or await ctx.guild.create_category(cat_name, reason="Esports bot setup")
        for ch_name, _ in channel_specs[cat_name]:
            if not discord.utils.get(cat.text_channels, name=ch_name):
                await ctx.guild.create_text_channel(ch_name, category=cat, reason="Esports bot setup")
                made.append(ch_name)
    for role_name, colour in [("Esports Admin", 0xD4AF37), ("Tournament Staff", 0x3B82F6), ("Team Captain", 0x22C55E), ("Player", 0x94A3B8)]:
        if not discord.utils.get(ctx.guild.roles, name=role_name):
            await ctx.guild.create_role(name=role_name, colour=discord.Colour(colour), reason="Esports bot setup")
    announce_channel = discord.utils.get(ctx.guild.text_channels, name="announcements")
    if announce_channel:
        with db() as c:
            c.execute("INSERT INTO settings(guild_id,announcement_channel) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET announcement_channel=excluded.announcement_channel", (ctx.guild.id, announce_channel.id))
    await ctx.reply(embed=embed("Setup Complete", f"Starter layout checked. Newly created channels: **{len(made)}**.\nReview channel and role permissions manually."))

@bot.command()
@commands.has_guild_permissions(administrator=True)
async def setlog(ctx, channel: discord.TextChannel):
    with db() as c:
        c.execute("INSERT INTO settings(guild_id,log_channel) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET log_channel=excluded.log_channel", (ctx.guild.id, channel.id))
    await ctx.reply(embed=embed("Log Channel Set", f"Moderation and bot logs will be sent to {channel.mention}."))

@bot.command()
@commands.has_guild_permissions(administrator=True)
async def setannounce(ctx, channel: discord.TextChannel):
    with db() as c:
        c.execute("INSERT INTO settings(guild_id,announcement_channel) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET announcement_channel=excluded.announcement_channel", (ctx.guild.id, channel.id))
    await ctx.reply(embed=embed("Announcement Channel Set", f"Announcements will be posted in {channel.mention}."))

@bot.command()
@commands.has_guild_permissions(manage_guild=True)
async def announce(ctx, *, message: str):
    with db() as c:
        row = c.execute("SELECT announcement_channel FROM settings WHERE guild_id=?", (ctx.guild.id,)).fetchone()
    channel = ctx.guild.get_channel(row["announcement_channel"]) if row and row["announcement_channel"] else discord.utils.get(ctx.guild.text_channels, name="announcements")
    if not channel:
        return await ctx.reply("No announcement channel configured. Create `#announcements` first.")
    await channel.send(embed=embed("📢 Esports Announcement", message, ACCENT))
    await ctx.reply(f"Announcement posted in {channel.mention}.", delete_after=8)

# ----------------------------- teams -----------------------------
@bot.group(invoke_without_command=True)
async def team(ctx):
    await ctx.send(embed=category_help("teams", get_prefix_for(ctx.guild.id), str(ctx.author)))

@team.command(name="create")
async def team_create(ctx, *, name: str):
    name = name.strip()
    if len(name) < 2 or len(name) > 32:
        return await ctx.reply("Team name must be 2–32 characters.")
    with db() as c:
        old = c.execute("SELECT id,name FROM teams WHERE guild_id=? AND (captain_id=? OR lower(name)=lower(?))", (ctx.guild.id, ctx.author.id, name)).fetchone()
        if old:
            return await ctx.reply(f"You already captain a team or that name exists (Team ID: `{old['id']}`).")
        cur = c.execute("INSERT INTO teams(guild_id,name,captain_id,players,created_at) VALUES(?,?,?,?,?)", (ctx.guild.id, name, ctx.author.id, str(ctx.author.id), now()))
        team_id = cur.lastrowid
    await ctx.send(embed=embed("Team Registered", f"**{name}** is registered.\nTeam ID: `{team_id}`\nCaptain: {ctx.author.mention}"))
    await log_action(ctx.guild, "Team Created", f"{ctx.author} created **{name}** (ID {team_id}).")

@team.command(name="list")
async def team_list(ctx):
    with db() as c:
        rows = c.execute("SELECT id,name,captain_id,players FROM teams WHERE guild_id=? ORDER BY id DESC LIMIT 25", (ctx.guild.id,)).fetchall()
    if not rows:
        return await ctx.send("No teams registered yet. Use `team create <name>`.")
    lines = [f"`#{r['id']}` **{discord.utils.escape_markdown(r['name'])}** — Captain <@{r['captain_id']}> — {len([x for x in r['players'].split(',') if x])} player(s)" for r in rows]
    await ctx.send(embed=embed("Registered Teams", "\n".join(lines)))

@team.command(name="info")
async def team_info(ctx, team_id: int):
    r = team_row(ctx.guild.id, team_id)
    if not r:
        return await ctx.reply("Team not found in this server.")
    players = [f"<@{x}>" for x in r["players"].split(",") if x]
    await ctx.send(embed=embed(f"Team #{r['id']} • {r['name']}", f"Captain: <@{r['captain_id']}>\nPlayers ({len(players)}): " + (", ".join(players) if players else "None")))

@team.command(name="add")
async def team_add(ctx, team_id: int, member: discord.Member):
    r = team_row(ctx.guild.id, team_id)
    if not r:
        return await ctx.reply("Team not found.")
    if ctx.author.id != r["captain_id"] and not ctx.author.guild_permissions.manage_guild:
        return await ctx.reply("Only the team captain or server manager can add players.")
    players = [x for x in r["players"].split(",") if x]
    if str(member.id) in players:
        return await ctx.reply("That member is already on this roster.")
    if len(players) >= 4:
        return await ctx.reply("Roster limit is 4 players per team in this starter build.")
    players.append(str(member.id))
    with db() as c:
        c.execute("UPDATE teams SET players=? WHERE id=? AND guild_id=?", (",".join(players), team_id, ctx.guild.id))
    await ctx.send(embed=embed("Roster Updated", f"{member.mention} added to **{r['name']}**."))

@team.command(name="leave")
async def team_leave(ctx):
    with db() as c:
        rows = c.execute("SELECT id,name,captain_id,players FROM teams WHERE guild_id=?", (ctx.guild.id,)).fetchall()
        found = None
        for r in rows:
            ids = [x for x in r["players"].split(",") if x]
            if str(ctx.author.id) in ids and ctx.author.id != r["captain_id"]:
                found = (r, ids); break
        if found:
            r, ids = found
            ids.remove(str(ctx.author.id))
            c.execute("UPDATE teams SET players=? WHERE id=?", (",".join(ids), r["id"]))
    if not found:
        return await ctx.reply("You are not a member of a team, or you are the captain. Captains should delete or transfer their team.")
    await ctx.reply(f"You left **{found[0]['name']}**.")

@team.command(name="delete")
async def team_delete(ctx, team_id: int):
    r = team_row(ctx.guild.id, team_id)
    if not r:
        return await ctx.reply("Team not found.")
    if ctx.author.id != r["captain_id"] and not ctx.author.guild_permissions.manage_guild:
        return await ctx.reply("Only the captain or a server manager can delete this team.")
    with db() as c:
        c.execute("DELETE FROM registrations WHERE team_id=?", (team_id,))
        c.execute("DELETE FROM wins WHERE team_id=?", (team_id,))
        c.execute("DELETE FROM teams WHERE id=? AND guild_id=?", (team_id, ctx.guild.id))
    await ctx.reply(f"Team **{r['name']}** deleted.")

# ----------------------------- events / scrims -----------------------------
async def create_event(ctx, kind: str, name: str, mode: str, slots: int):
    if not 1 <= slots <= 100:
        return await ctx.reply("Slots must be between 1 and 100.")
    if mode.lower() not in {"solo", "duo", "squad", "t1", "t2", "t3", "t4", "t2q", "sq"}:
        return await ctx.reply("Mode examples: `solo`, `duo`, `squad`, `T2`, `T3`, `T4`, `T2Q`, `SQ`.")
    with db() as c:
        cur = c.execute("INSERT INTO events(guild_id,name,kind,mode,slots,status,created_by,created_at) VALUES(?,?,?,?,?,'open',?,?)", (ctx.guild.id, name[:70], kind, mode.upper(), slots, ctx.author.id, now()))
        eid = cur.lastrowid
    await ctx.send(embed=embed(f"{kind.title()} Created", f"**{name}**\nID: `{eid}` • Mode: **{mode.upper()}** • Team slots: **{slots}**\nRegistration: **OPEN**"))
    await log_action(ctx.guild, f"{kind.title()} Created", f"{ctx.author} created {name} (ID {eid}).")

@bot.group(invoke_without_command=True)
async def event(ctx):
    await ctx.send(embed=category_help("events", get_prefix_for(ctx.guild.id), str(ctx.author)))

@event.command(name="create")
@commands.has_guild_permissions(manage_guild=True)
async def event_create(ctx, name: str, mode: str, slots: int):
    await create_event(ctx, "tournament", name, mode, slots)

@event.command(name="list")
async def event_list(ctx):
    with db() as c:
        rows = c.execute("SELECT * FROM events WHERE guild_id=? AND kind='tournament' ORDER BY id DESC LIMIT 20", (ctx.guild.id,)).fetchall()
    await send_event_list(ctx, rows, "Tournaments")

@event.command(name="join")
async def event_join(ctx, event_id: int, team_id: int):
    await register_team(ctx, event_id, team_id, "tournament")

@event.command(name="close")
@commands.has_guild_permissions(manage_guild=True)
async def event_close(ctx, event_id: int):
    await close_event(ctx, event_id, "tournament")

@bot.group(invoke_without_command=True)
async def scrim(ctx):
    await ctx.send(embed=category_help("events", get_prefix_for(ctx.guild.id), str(ctx.author)))

@scrim.command(name="create")
@commands.has_guild_permissions(manage_guild=True)
async def scrim_create(ctx, name: str, mode: str, slots: int):
    await create_event(ctx, "scrim", name, mode, slots)

@scrim.command(name="list")
async def scrim_list(ctx):
    with db() as c:
        rows = c.execute("SELECT * FROM events WHERE guild_id=? AND kind='scrim' ORDER BY id DESC LIMIT 20", (ctx.guild.id,)).fetchall()
    await send_event_list(ctx, rows, "Scrims")

@scrim.command(name="join")
async def scrim_join(ctx, event_id: int, team_id: int):
    await register_team(ctx, event_id, team_id, "scrim")

async def send_event_list(ctx, rows, title):
    if not rows:
        return await ctx.send(embed=embed(title, "Nothing here yet."))
    lines = []
    with db() as c:
        for r in rows:
            count = c.execute("SELECT COUNT(*) n FROM registrations WHERE event_id=?", (r["id"],)).fetchone()["n"]
            lines.append(f"`#{r['id']}` **{r['name']}** • {r['mode']} • {count}/{r['slots']} teams • **{r['status'].upper()}**")
    await ctx.send(embed=embed(title, "\n".join(lines)))

async def register_team(ctx, event_id: int, team_id: int, kind: str):
    e = event_row(ctx.guild.id, event_id)
    t = team_row(ctx.guild.id, team_id)
    if not e or e["kind"] != kind:
        return await ctx.reply(f"{kind.title()} not found.")
    if not t:
        return await ctx.reply("Team not found in this server.")
    if ctx.author.id != t["captain_id"] and not ctx.author.guild_permissions.manage_guild:
        return await ctx.reply("Only the team captain or server manager can register this team.")
    if e["status"] != "open":
        return await ctx.reply("Registration is closed.")
    with db() as c:
        count = c.execute("SELECT COUNT(*) n FROM registrations WHERE event_id=?", (event_id,)).fetchone()["n"]
        if count >= e["slots"]:
            return await ctx.reply("This event is full.")
        try:
            c.execute("INSERT INTO registrations(event_id,team_id,registered_at) VALUES(?,?,?)", (event_id, team_id, now()))
        except sqlite3.IntegrityError:
            return await ctx.reply("That team is already registered.")
    await ctx.send(embed=embed("Registration Confirmed", f"**{t['name']}** registered for **{e['name']}** (ID `{event_id}`)."))

async def close_event(ctx, event_id: int, kind: str):
    e = event_row(ctx.guild.id, event_id)
    if not e or e["kind"] != kind:
        return await ctx.reply(f"{kind.title()} not found.")
    with db() as c:
        c.execute("UPDATE events SET status='closed' WHERE id=? AND guild_id=?", (event_id, ctx.guild.id))
    await ctx.reply(f"Registration closed for **{e['name']}**.")

# ----------------------------- matches / leaderboard -----------------------------
@bot.group(invoke_without_command=True)
async def match(ctx):
    await ctx.send(embed=category_help("matches", get_prefix_for(ctx.guild.id), str(ctx.author)))

@match.command(name="create")
@commands.has_guild_permissions(manage_guild=True)
async def match_create(ctx, event_id: int, team_a: int, team_b: int, *, match_time: str):
    e, a, b = event_row(ctx.guild.id, event_id), team_row(ctx.guild.id, team_a), team_row(ctx.guild.id, team_b)
    if not e or not a or not b:
        return await ctx.reply("Event or team ID not found in this server.")
    if team_a == team_b:
        return await ctx.reply("A team cannot play against itself.")
    with db() as c:
        cur = c.execute("INSERT INTO matches(guild_id,event_id,team_a,team_b,match_time,created_at) VALUES(?,?,?,?,?,?)", (ctx.guild.id, event_id, team_a, team_b, match_time[:80], now()))
        mid = cur.lastrowid
    await ctx.send(embed=embed("Match Scheduled", f"Match ID: `{mid}`\nEvent: **{e['name']}**\n**{a['name']}** vs **{b['name']}**\nTime: {match_time}"))

@match.command(name="list")
async def match_list(ctx):
    with db() as c:
        rows = c.execute("SELECT * FROM matches WHERE guild_id=? ORDER BY id DESC LIMIT 20", (ctx.guild.id,)).fetchall()
    if not rows:
        return await ctx.send("No matches scheduled.")
    lines = []
    for r in rows:
        a, b = team_row(ctx.guild.id, r["team_a"]), team_row(ctx.guild.id, r["team_b"])
        an = a["name"] if a else f"Team {r['team_a']}"
        bn = b["name"] if b else f"Team {r['team_b']}"
        lines.append(f"`#{r['id']}` **{an} vs {bn}** • {r['match_time']} • {r['status'].upper()}")
    await ctx.send(embed=embed("Match Schedule", "\n".join(lines)))

@match.command(name="room")
@commands.has_guild_permissions(manage_guild=True)
async def match_room(ctx, match_id: int, room_id: str, password: str):
    with db() as c:
        r = c.execute("SELECT id FROM matches WHERE guild_id=? AND id=?", (ctx.guild.id, match_id)).fetchone()
        if not r:
            return await ctx.reply("Match not found.")
        c.execute("UPDATE matches SET room_id=?,room_password=? WHERE id=?", (room_id[:40], password[:40], match_id))
    await ctx.reply(embed=embed("Room Details Saved", f"Match `{match_id}` room details updated. Share credentials only with registered players."))

@match.command(name="result")
@commands.has_guild_permissions(manage_guild=True)
async def match_result(ctx, match_id: int, winner_team_id: int):
    with db() as c:
        r = c.execute("SELECT * FROM matches WHERE guild_id=? AND id=?", (ctx.guild.id, match_id)).fetchone()
        if not r:
            return await ctx.reply("Match not found.")
        if winner_team_id not in (r["team_a"], r["team_b"]):
            return await ctx.reply("Winner must be one of the two teams in this match.")
        c.execute("UPDATE matches SET winner_team=?,status='completed' WHERE id=?", (winner_team_id, match_id))
        c.execute("INSERT INTO wins(guild_id,team_id,wins) VALUES(?,?,1) ON CONFLICT(guild_id,team_id) DO UPDATE SET wins=wins+1", (ctx.guild.id, winner_team_id))
    winner = team_row(ctx.guild.id, winner_team_id)
    await ctx.send(embed=embed("Match Result Recorded", f"Winner: **{winner['name'] if winner else winner_team_id}** • Match `#{match_id}`"))
    await log_action(ctx.guild, "Match Result", f"Match {match_id} winner recorded as team {winner_team_id} by {ctx.author}.")

@bot.command()
async def leaderboard(ctx):
    with db() as c:
        rows = c.execute("SELECT w.team_id,w.wins,t.name FROM wins w LEFT JOIN teams t ON t.id=w.team_id WHERE w.guild_id=? ORDER BY w.wins DESC LIMIT 10", (ctx.guild.id,)).fetchall()
    if not rows:
        return await ctx.send(embed=embed("Esports Leaderboard", "No match wins recorded yet."))
    lines = [f"**{i}.** {r['name'] or 'Deleted team'} — **{r['wins']}** win(s)" for i, r in enumerate(rows, 1)]
    await ctx.send(embed=embed("🏆 Esports Leaderboard", "\n".join(lines), ACCENT))

# ----------------------------- moderation -----------------------------
@bot.group(invoke_without_command=True)
async def mod(ctx):
    await ctx.send(embed=category_help("moderation", get_prefix_for(ctx.guild.id), str(ctx.author)))

@mod.command(name="warn")
@commands.has_guild_permissions(moderate_members=True)
async def mod_warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    with db() as c:
        c.execute("INSERT INTO warnings(guild_id,user_id,moderator_id,reason,created_at) VALUES(?,?,?,?,?)", (ctx.guild.id, member.id, ctx.author.id, reason[:500], now()))
        count = c.execute("SELECT COUNT(*) n FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id)).fetchone()["n"]
    await ctx.send(embed=embed("Member Warned", f"Member: {member.mention}\nReason: {reason}\nTotal warnings: **{count}**"))
    await log_action(ctx.guild, "Warning Issued", f"{member} warned by {ctx.author}: {reason}")

@mod.command(name="warnings")
@commands.has_guild_permissions(moderate_members=True)
async def mod_warnings(ctx, member: discord.Member):
    with db() as c:
        rows = c.execute("SELECT reason,created_at,moderator_id FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10", (ctx.guild.id, member.id)).fetchall()
    if not rows:
        return await ctx.send(f"{member.mention} has no recorded warnings.")
    lines = [f"• {r['reason']} — <@{r['moderator_id']}> ({r['created_at'][:10]})" for r in rows]
    await ctx.send(embed=embed(f"Warnings • {member}", "\n".join(lines)))

@mod.command(name="timeout")
@commands.has_guild_permissions(moderate_members=True)
async def mod_timeout(ctx, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
    if minutes < 1 or minutes > 40320:
        return await ctx.reply("Timeout duration must be 1–40320 minutes.")
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        return await ctx.reply("You cannot timeout a member with an equal or higher role.")
    await member.timeout(dt.timedelta(minutes=minutes), reason=reason)
    await ctx.send(embed=embed("Member Timed Out", f"{member.mention} • {minutes} minute(s)\nReason: {reason}"))
    await log_action(ctx.guild, "Timeout", f"{member} timed out by {ctx.author} for {minutes}m: {reason}")

@mod.command(name="kick")
@commands.has_guild_permissions(kick_members=True)
async def mod_kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        return await ctx.reply("You cannot kick a member with an equal or higher role.")
    await member.kick(reason=reason)
    await ctx.send(embed=embed("Member Kicked", f"{member} • Reason: {reason}"))
    await log_action(ctx.guild, "Member Kicked", f"{member} kicked by {ctx.author}: {reason}")

@mod.command(name="ban")
@commands.has_guild_permissions(ban_members=True)
async def mod_ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        return await ctx.reply("You cannot ban a member with an equal or higher role.")
    await member.ban(reason=reason, delete_message_seconds=0)
    await ctx.send(embed=embed("Member Banned", f"{member} • Reason: {reason}"))
    await log_action(ctx.guild, "Member Banned", f"{member} banned by {ctx.author}: {reason}")

# ----------------------------- tickets -----------------------------
class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Create Support Ticket", style=discord.ButtonStyle.primary, custom_id="atlanta_ticket_create")
    async def create_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this button in a server.", ephemeral=True)
        with db() as c:
            existing = c.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'", (guild.id, interaction.user.id)).fetchone()
        if existing and guild.get_channel(existing["channel_id"]):
            return await interaction.response.send_message(f"You already have an open ticket: <#{existing['channel_id']}>", ephemeral=True)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)}
        for role in guild.roles:
            if role.name.lower() in {"esports admin", "tournament staff", "administrator"}:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        channel = await guild.create_text_channel(f"ticket-{interaction.user.name}"[:90], overwrites=overwrites, reason="Support ticket opened")
        with db() as c:
            c.execute("INSERT INTO tickets(guild_id,channel_id,user_id,status,created_at) VALUES(?,?,?,'open',?)", (guild.id, channel.id, interaction.user.id, now()))
        await channel.send(embed=embed("Support Ticket", f"Welcome {interaction.user.mention}. Explain your issue here. Staff can use `{get_prefix_for(guild.id)}ticket close` to close this ticket."))
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)

@bot.group(invoke_without_command=True)
async def ticket(ctx):
    await ctx.send(embed=category_help("tickets", get_prefix_for(ctx.guild.id), str(ctx.author)))

@ticket.command(name="panel")
@commands.has_guild_permissions(manage_channels=True)
async def ticket_panel(ctx):
    await ctx.send(embed=embed("ATLANTA • Support Desk", "Need help with registration, matches, or server issues? Open a private ticket below."), view=TicketCreateView())

@ticket.command(name="create")
async def ticket_create(ctx):
    with db() as c:
        existing = c.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'", (ctx.guild.id, ctx.author.id)).fetchone()
    if existing and ctx.guild.get_channel(existing["channel_id"]):
        return await ctx.reply(f"You already have an open ticket: <#{existing['channel_id']}>")
    overwrites = {ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False), ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)}
    for role in ctx.guild.roles:
        if role.name.lower() in {"esports admin", "tournament staff", "administrator"}:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    channel = await ctx.guild.create_text_channel(f"ticket-{ctx.author.name}"[:90], overwrites=overwrites, reason="Support ticket opened")
    with db() as c:
        c.execute("INSERT INTO tickets(guild_id,channel_id,user_id,status,created_at) VALUES(?,?,?,'open',?)", (ctx.guild.id, channel.id, ctx.author.id, now()))
    await channel.send(embed=embed("Support Ticket", f"Welcome {ctx.author.mention}. Explain your issue here. Staff can use `{get_prefix_for(ctx.guild.id)}ticket close` to close this ticket."))
    await ctx.reply(f"Your private ticket has been created: {channel.mention}")

@ticket.command(name="close")
async def ticket_close(ctx):
    with db() as c:
        row = c.execute("SELECT * FROM tickets WHERE guild_id=? AND channel_id=? AND status='open'", (ctx.guild.id, ctx.channel.id)).fetchone()
    if not row:
        return await ctx.reply("This channel is not an open ticket.")
    if ctx.author.id != row["user_id"] and not ctx.author.guild_permissions.manage_channels:
        return await ctx.reply("Only the ticket opener or staff with Manage Channels can close it.")
    with db() as c:
        c.execute("UPDATE tickets SET status='closed' WHERE channel_id=?", (ctx.channel.id,))
    await ctx.send(embed=embed("Ticket Closed", f"Closed by {ctx.author}. This channel will be deleted in 5 seconds."))
    await discord.utils.sleep_until(discord.utils.utcnow() + dt.timedelta(seconds=5))
    await ctx.channel.delete(reason=f"Ticket closed by {ctx.author}")

# ----------------------------- owner-only dev tools -----------------------------
@bot.group(invoke_without_command=True, hidden=True)
async def dev(ctx):
    if not is_owner_id(ctx.author.id):
        return await ctx.reply("This developer command is restricted.", delete_after=5)
    await ctx.reply("Owner tools: `dev stats`, `dev sync`, `dev shutdown`.")

@dev.command(name="stats", hidden=True)
async def dev_stats(ctx):
    if not is_owner_id(ctx.author.id):
        return await ctx.reply("Not authorized.")
    with db() as c:
        teams = c.execute("SELECT COUNT(*) n FROM teams").fetchone()["n"]
        events = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
        matches = c.execute("SELECT COUNT(*) n FROM matches").fetchone()["n"]
    await ctx.reply(embed=embed("Developer Stats", f"Guilds: **{len(bot.guilds)}**\nTeams: **{teams}**\nEvents: **{events}**\nMatches: **{matches}**"))

@dev.command(name="sync", hidden=True)
async def dev_sync(ctx):
    if not is_owner_id(ctx.author.id):
        return await ctx.reply("Not authorized.")
    synced = await bot.tree.sync()
    await ctx.reply(f"Synced **{len(synced)}** application command(s). This build primarily uses prefix commands.")

@dev.command(name="shutdown", hidden=True)
async def dev_shutdown(ctx):
    if not is_owner_id(ctx.author.id):
        return await ctx.reply("Not authorized.")
    await ctx.reply("Shutting down bot.")
    await bot.close()

# ----------------------------- safety / error handling -----------------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        return await ctx.reply("You do not have permission to use this command.", mention_author=False)
    if isinstance(error, commands.BotMissingPermissions):
        return await ctx.reply("I am missing a required permission. Check my role and channel permissions.", mention_author=False)
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.reply(f"Missing argument: `{error.param.name}`. Use `{get_prefix_for(ctx.guild.id if ctx.guild else None)}help` to see examples.", mention_author=False)
    if isinstance(error, commands.BadArgument):
        return await ctx.reply("Invalid argument. Check member mentions, IDs, and command format with `help`.", mention_author=False)
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.reply(f"Try again in {error.retry_after:.1f}s.", mention_author=False)
    if isinstance(error, discord.Forbidden):
        return await ctx.reply("Discord denied that action. Check role hierarchy and permissions.", mention_author=False)
    print(f"Command error in {getattr(ctx.command, 'qualified_name', 'unknown')}: {error}")
    await ctx.reply("Something went wrong while running that command. Check the bot console logs.", mention_author=False)

@bot.event
async def on_guild_join(guild):
    init_db()
    print(f"Joined guild: {guild.name} ({guild.id})")

if __name__ == "__main__":
    init_db()
    if not TOKEN or TOKEN == "PASTE_TOKEN_HERE":
        raise SystemExit("Set DISCORD_TOKEN environment variable before starting the bot.")
    bot.run(TOKEN)
