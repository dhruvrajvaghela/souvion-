"""
Souvion Security clone - Python Discord bot (discord.py 2.x)
Install : pip install -U discord.py
Run     : set DISCORD_TOKEN=your_token   (Windows)  |  export DISCORD_TOKEN=your_token (Linux/Mac)
          python souvion_bot.py
Portal  : Developer Portal > Bot > enable ALL 3 Privileged Intents
          Invite bot with Administrator permission (bot role should be at the TOP of role list)
"""
import os, re, json, time, copy, asyncio
import datetime as dt
from collections import defaultdict, deque
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from discord.ext import voice_recv
from pathlib import Path
from datetime import datetime

TOKEN = os.getenv("DISCORD_TOKEN", "PASTE_TOKEN_HERE")
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()}
DATA_FILE = "souvion_data.json"
TZ = ZoneInfo("Asia/Kolkata")
COLOR = 0x5865F2

# ───────────────────────── storage ─────────────────────────
DEFAULTS = {
    "prefix": "!",
    "antinuke": {
        "enabled": False, "whitelist": [], "punishment": "ban",
        "thresholds": {  # action: [limit, seconds]
            "channel_delete": [2, 10], "channel_create": [3, 10],
            "role_delete": [2, 10], "role_create": [3, 10],
            "ban": [3, 10], "kick": [3, 10], "webhook": [3, 10],
        },
    },
    "superantinuke": {"enabled": False, "whitelist": [], "burst": [3, 10],
                      "bot_add": "kick_bot_and_punish_adder", "punishment": "ban"},
    "raidmode": {"enabled": False, "auto_joins": 10, "auto_seconds": 10},
    "automod": {"enabled": False, "whitelist": [], "spam_limit": 5, "spam_seconds": 5,
                "mention_limit": 5, "links": False, "invites": True, "badwords": []},
    "nightmode": {"enabled": False, "auto": False, "on": "23:00", "off": "06:00", "locked": []},
    "logs": {},
    "snapshot": {},
    "warns": {},
}
DATA = {}


def deep_fill(d, defaults):
    for k, v in defaults.items():
        if k not in d:
            d[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(d[k], dict):
            deep_fill(d[k], v)


def load():
    global DATA
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            DATA = json.load(f)


def save():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DATA, f, indent=1)


def gd(gid):
    k = str(gid)
    DATA.setdefault(k, {})
    deep_fill(DATA[k], DEFAULTS)
    return DATA[k]


# ───────────────────────── bot setup ─────────────────────────
async def get_prefix(bot_, msg):
    return gd(msg.guild.id)["prefix"] if msg.guild else "!"


intents = discord.Intents.all()
bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)
hits = defaultdict(deque)
join_times = defaultdict(deque)
spam_times = defaultdict(deque)


def emb(title, desc="", color=COLOR):
    return discord.Embed(title=title, description=desc, color=color)


def trusted(guild, uid, section):
    d = gd(guild.id)
    return (uid == guild.owner_id or uid in OWNER_IDS or uid == bot.user.id
            or uid in d[section]["whitelist"])


def admin():
    return commands.has_permissions(administrator=True)


async def send_log(guild, kind, title, desc, color=COLOR):
    cid = gd(guild.id)["logs"].get(kind)
    ch = guild.get_channel(cid) if cid else None
    if ch:
        try:
            e = emb(title, desc, color)
            e.timestamp = discord.utils.utcnow()
            await ch.send(embed=e)
        except discord.HTTPException:
            pass


# ───────────────────────── help panel ─────────────────────────
CATS = {
    "antinuke": ("🛡️", "Core Antinuke", [
        ("antinuke", "View Core Antinuke status", "Everyone"),
        ("antinuke enable", "Enable Core Antinuke", "Admin"),
        ("antinuke disable", "Disable Core Antinuke", "Admin"),
        ("antinuke status", "View Core/Super/Raid/Recovery status", "Everyone"),
        ("antinuke settings", "View all security thresholds", "Admin"),
        ("antinuke whitelist add @user", "Whitelist a user", "Admin"),
        ("antinuke whitelist remove @user", "Remove a user from whitelist", "Admin"),
        ("antinuke whitelist show", "Show Core Antinuke whitelist", "Admin"),
        ("antinuke whitelist reset", "Clear the whitelist", "Admin"),
        ("setthreshold <action> <limit> <period>", "Set a security action threshold", "Admin")]),
    "superantinuke": ("⚡", "SuperAntinuke", [
        ("superantinuke", "View SuperAntinuke status", "Everyone"),
        ("superantinuke enable", "Enable SuperAntinuke MAX", "Admin"),
        ("superantinuke disable", "Disable SuperAntinuke MAX", "Admin"),
        ("superantinuke config", "View burst/bot-add/punishment settings", "Admin"),
        ("superantinuke verify", "Check required bot permissions", "Everyone"),
        ("superantinuke whitelist add @user", "Whitelist a user", "Admin"),
        ("superantinuke whitelist remove @user", "Remove a user from whitelist", "Admin"),
        ("superantinuke whitelist show", "Show whitelist", "Admin"),
        ("superantinuke whitelist reset", "Clear the whitelist", "Admin")]),
    "raidmode": ("🚨", "Raidmode", [
        ("raidmode", "View Raidmode status", "Everyone"),
        ("raidmode on", "Enable Raidmode", "Admin"),
        ("raidmode off", "Disable Raidmode", "Admin"),
        ("raidmode status", "View Raidmode status", "Everyone")]),
    "automod": ("🤖", "AutoMod", [
        ("automod", "View AutoMod status", "Everyone"),
        ("automod on", "Enable AutoMod", "Admin"),
        ("automod off", "Disable AutoMod", "Admin"),
        ("automod config", "View AutoMod configuration", "Admin"),
        ("automod set <key> <value>", "Change an AutoMod setting live", "Admin"),
        ("automod whitelist add @user", "Whitelist a user", "Admin"),
        ("automod whitelist remove @user", "Remove a user from whitelist", "Admin")]),
    "nightmode": ("🌙", "Nightmode", [
        ("nightmode", "View Nightmode status and schedule", "Everyone"),
        ("nightmode enable", "Enable Nightmode", "Admin"),
        ("nightmode disable", "Disable Nightmode", "Admin"),
        ("nightmode status", "View Nightmode status", "Everyone"),
        ("nightmode schedule <on> <off>", "Set automatic schedule (HH:MM)", "Admin"),
        ("nightmode auto on/off", "Toggle automatic Nightmode", "Admin")]),
    "logging": ("📋", "Logging & Channels", [
        ("autologs", "View AutoLogs help", "Everyone"),
        ("autologs setup", "Create/reuse private security log channels", "Admin"),
        ("setlog <event> #channel", "Route an event to a log channel (security/mod/message/member)", "Admin")]),
    "recovery": ("🔄", "Recovery", [
        ("snapshot", "Save the current server structure", "Admin"),
        ("recovery", "Restore missing saved objects", "Admin")]),
    "tickets": ("🎫", "Tickets", [
        ("ticket", "View ticket help", "Everyone"),
        ("ticket setup", "Post the support ticket panel", "Manage Channels"),
        ("ticket create", "Create a private support ticket", "Everyone"),
        ("ticket close", "Close the current ticket", "Owner/Staff")]),
    "moderation": ("🔨", "Moderation", [
        ("mod", "View moderation help", "Everyone"),
        ("mod warn @user [reason]", "Warn a member", "Moderate Members"),
        ("mod timeout @user <minutes> [reason]", "Timeout a member", "Moderate Members"),
        ("mod kick @user [reason]", "Kick a member", "Kick Members"),
        ("mod ban @user [reason]", "Ban a member", "Ban Members")]),
    "tools": ("🖥️", "Server Tools", [
        ("help", "Open this interactive help panel", "Everyone"),
        ("ping", "Check bot latency", "Everyone"),
        ("prefix", "Show the current server prefix", "Manage Server"),
        ("prefix <new>", "Change the server prefix", "Manage Server"),
        ("autosetup", "Enable MAX security and private logs", "Admin"),
        ("securitysetup", "Open the interactive security setup panel", "Admin"),
        ("setupsecurity", "Alias for securitysetup", "Admin")]),
 
    "channelcontrol": ("🔒", "Channel Control", [
        ("lock", "Lock the current text channel", "Manage Channels"),
        ("unlock", "Unlock the current text channel", "Manage Channels"),
        ("lock #channel", "Lock a specified text channel", "Manage Channels"),
        ("unlock #channel", "Unlock a specified text channel", "Manage Channels")
    ]),

              "recording": ("🎙️", "Voice Recording", [
        ("record start <name>", "Start recording after everyone consents", "Manage Channels"),
        ("record stop", "Stop the active recording", "Manage Channels"),
        ("record list", "List saved recordings", "Manage Channels"),
    ]),

}


def main_help_embed(prefix):
    lines = "\n".join(f"{v[0]} {v[1]}" for v in CATS.values())
    e = emb("🛡️ Souvion Security • Help Panel",
            "**Advanced Protection • Server Control**\n\nSelect a category below to view every public command.")
    e.add_field(name="🚀 Security / Server Modules", value=lines, inline=False)
    e.add_field(name="⚙️ Quick Setup",
                value=f"`{prefix}securitysetup` • Interactive setup panel\n`{prefix}autosetup` • One-command MAX security setup",
                inline=False)
    e.set_footer(text=f"Prefix: {prefix} • Select a category below")
    return e


def cat_embed(key, prefix, user):
    icon, name, rows = CATS[key]
    desc = "\n".join(f"`{prefix}{c}` — {d} • **{p}**" for c, d, p in rows)
    e = emb(f"{icon} {name} Commands", desc)
    e.set_footer(text=f"Prefix: {prefix} • Requested by {user}")
    return e


class HelpView(discord.ui.View):
    def __init__(self, author_id, prefix):
        super().__init__(timeout=300)
        self.author_id, self.prefix = author_id, prefix
        sel = discord.ui.Select(
            placeholder="🚀 Select a Security / Server Module",
            options=[discord.SelectOption(label=v[1], value=k, emoji=v[0]) for k, v in CATS.items()])
        sel.callback = self.on_select
        self.add_item(sel)

    async def interaction_check(self, i):
        if i.user.id != self.author_id:
            await i.response.send_message("This panel isn't yours. Run the help command yourself.", ephemeral=True)
            return False
        return True

    async def on_select(self, i: discord.Interaction):
        await i.response.edit_message(embed=cat_embed(i.data["values"][0], self.prefix, i.user))

    @discord.ui.button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, i: discord.Interaction, b):
        await i.response.edit_message(embed=main_help_embed(self.prefix))


@bot.command(name="help")
async def help_cmd(ctx):
    p = gd(ctx.guild.id)["prefix"]
    await ctx.send(embed=main_help_embed(p), view=HelpView(ctx.author.id, p))


@bot.command()
async def ping(ctx):
    await ctx.send(embed=emb("🏓 Pong", f"Latency: **{round(bot.latency * 1000)}ms**"))


@bot.command()
@commands.has_permissions(manage_guild=True)
async def prefix(ctx, new: str = None):
    d = gd(ctx.guild.id)
    if not new:
        return await ctx.send(embed=emb("📌 Server Prefix", f"`{d['prefix']}`"))
    if len(new) > 3:
        return await ctx.send("Prefix max 3 characters.")
    d["prefix"] = new
    save()
    await ctx.send(embed=emb("📌 Prefix Updated", f"New prefix: `{new}`"))


# ───────────────────────── punish / recovery ─────────────────────────
async def punish(guild, user, reason, mode="ban"):
    member = guild.get_member(user.id)
    try:
        if mode == "ban":
            await guild.ban(user, reason=reason)
        elif mode == "kick" and member:
            await guild.kick(member, reason=reason)
        elif member:  # strip roles
            roles = [r for r in member.roles if r != guild.default_role and r < guild.me.top_role]
            await member.remove_roles(*roles, reason=reason)
    except discord.HTTPException:
        pass
    await send_log(guild, "security", "🚨 Antinuke Triggered",
                   f"User: {user.mention} (`{user.id}`)\nReason: {reason}\nAction: **{mode}**", 0xED4245)


async def take_snapshot(guild):
    snap = {
        "roles": [{"name": r.name, "color": r.color.value, "perms": r.permissions.value,
                   "hoist": r.hoist, "mention": r.mentionable}
                  for r in guild.roles if not r.managed and r != guild.default_role],
        "channels": [{"name": c.name, "type": c.type.name,
                      "category": c.category.name if c.category else None,
                      "topic": getattr(c, "topic", None), "nsfw": getattr(c, "nsfw", False),
                      "slow": getattr(c, "slowmode_delay", 0)}
                     for c in guild.channels
                     if c.type.name in ("text", "voice", "category")],
    }
    gd(guild.id)["snapshot"] = snap
    save()
    return len(snap["roles"]), len(snap["channels"])


async def restore(guild):
    snap = gd(guild.id).get("snapshot")
    if not snap:
        return None
    made = 0
    have_roles = {r.name for r in guild.roles}
    for r in snap["roles"]:
        if r["name"] not in have_roles:
            try:
                await guild.create_role(name=r["name"], colour=discord.Colour(r["color"]),
                                        permissions=discord.Permissions(r["perms"]),
                                        hoist=r["hoist"], mentionable=r["mention"])
                made += 1
            except discord.HTTPException:
                pass
    cats = {c.name: c for c in guild.categories}
    for c in [x for x in snap["channels"] if x["type"] == "category"]:
        if c["name"] not in cats:
            try:
                cats[c["name"]] = await guild.create_category(c["name"])
                made += 1
            except discord.HTTPException:
                pass
    have = {(c.name, c.type.name) for c in guild.channels}
    for c in [x for x in snap["channels"] if x["type"] != "category"]:
        if (c["name"], c["type"]) in have:
            continue
        cat = cats.get(c["category"])
        try:
            if c["type"] == "text":
                await guild.create_text_channel(c["name"], category=cat, topic=c["topic"],
                                                nsfw=c["nsfw"], slowmode_delay=c["slow"])
            else:
                await guild.create_voice_channel(c["name"], category=cat)
            made += 1
        except discord.HTTPException:
            pass
    return made


@bot.command()
@admin()
async def snapshot(ctx):
    r, c = await take_snapshot(ctx.guild)
    await ctx.send(embed=emb("📸 Snapshot Saved", f"Roles: **{r}** • Channels/Categories: **{c}**"))


@bot.command()
@admin()
async def recovery(ctx):
    made = await restore(ctx.guild)
    if made is None:
        return await ctx.send(f"No snapshot. Run `{gd(ctx.guild.id)['prefix']}snapshot` first.")
    await ctx.send(embed=emb("🔄 Recovery Complete", f"Restored **{made}** missing objects."))


# ───────────────────────── antinuke engine ─────────────────────────
async def audit_actor(guild, action, target_id=None):
    try:
        async for e in guild.audit_logs(limit=6, action=action):
            if (discord.utils.utcnow() - e.created_at).total_seconds() > 15:
                continue
            if target_id is None or (e.target and getattr(e.target, "id", None) == target_id):
                return e.user
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None


async def track(guild, user, key):
    if user is None:
        return False
    d = gd(guild.id)
    now = time.time()
    reasons, mode = [], "ban"
    an, sa = d["antinuke"], d["superantinuke"]
    if an["enabled"] and not trusted(guild, user.id, "antinuke"):
        lim, per = an["thresholds"][key]
        dq = hits[(guild.id, user.id, key)]
        dq.append(now)
        while dq and now - dq[0] > per:
            dq.popleft()
        if len(dq) >= lim:
            reasons.append(f"Core Antinuke: {key} x{len(dq)}")
            mode = an["punishment"]
    if sa["enabled"] and not trusted(guild, user.id, "superantinuke"):
        lim, per = sa["burst"]
        dq = hits[(guild.id, user.id, "burst")]
        dq.append(now)
        while dq and now - dq[0] > per:
            dq.popleft()
        if len(dq) >= lim:
            reasons.append(f"SuperAntinuke burst: {len(dq)} actions/{per}s")
            mode = sa["punishment"]
    if reasons:
        for k in list(hits):
            if k[0] == guild.id and k[1] == user.id:
                hits[k].clear()
        await punish(guild, user, " | ".join(reasons), mode)
        await restore(guild)
        return True
    return False


@bot.event
async def on_guild_channel_delete(ch):
    a = await audit_actor(ch.guild, discord.AuditLogAction.channel_delete, ch.id)
    await track(ch.guild, a, "channel_delete")


@bot.event
async def on_guild_channel_create(ch):
    a = await audit_actor(ch.guild, discord.AuditLogAction.channel_create, ch.id)
    if await track(ch.guild, a, "channel_create"):
        try:
            await ch.delete(reason="Antinuke: created by nuker")
        except discord.HTTPException:
            pass


@bot.event
async def on_guild_role_delete(role):
    a = await audit_actor(role.guild, discord.AuditLogAction.role_delete, role.id)
    await track(role.guild, a, "role_delete")


@bot.event
async def on_guild_role_create(role):
    a = await audit_actor(role.guild, discord.AuditLogAction.role_create, role.id)
    if await track(role.guild, a, "role_create"):
        try:
            await role.delete(reason="Antinuke: created by nuker")
        except discord.HTTPException:
            pass


@bot.event
async def on_member_ban(guild, user):
    a = await audit_actor(guild, discord.AuditLogAction.ban, user.id)
    await track(guild, a, "ban")
    await send_log(guild, "mod", "🔨 Member Banned", f"{user} (`{user.id}`) by {a.mention if a else 'unknown'}", 0xED4245)


@bot.event
async def on_webhooks_update(channel):
    a = await audit_actor(channel.guild, discord.AuditLogAction.webhook_create)
    await track(channel.guild, a, "webhook")


@bot.event
async def on_member_remove(member):
    g = member.guild
    a = await audit_actor(g, discord.AuditLogAction.kick, member.id)
    if a:
        await track(g, a, "kick")
    await send_log(g, "member", "📤 Member Left", f"{member} (`{member.id}`)", 0xFEE75C)


@bot.event
async def on_member_join(member):
    g = member.guild
    d = gd(g.id)
    # SuperAntinuke: unauthorized bot add
    if member.bot and d["superantinuke"]["enabled"]:
        adder = await audit_actor(g, discord.AuditLogAction.bot_add, member.id)
        if adder and not trusted(g, adder.id, "superantinuke"):
            try:
                await g.kick(member, reason="SuperAntinuke: unauthorized bot")
            except discord.HTTPException:
                pass
            await punish(g, adder, "Added a bot without whitelist", d["superantinuke"]["punishment"])
            return
    # Raidmode
    rm = d["raidmode"]
    now = time.time()
    dq = join_times[g.id]
    dq.append(now)
    while dq and now - dq[0] > rm["auto_seconds"]:
        dq.popleft()
    if not rm["enabled"] and d["antinuke"]["enabled"] and len(dq) >= rm["auto_joins"]:
        rm["enabled"] = True
        save()
        await send_log(g, "security", "🚨 Raidmode Auto-Enabled",
                       f"{len(dq)} joins in {rm['auto_seconds']}s", 0xED4245)
    if rm["enabled"] and not member.bot:
        try:
            await member.send(f"{g.name} is in Raidmode. Please try joining later.")
        except discord.HTTPException:
            pass
        try:
            await member.kick(reason="Raidmode active")
        except discord.HTTPException:
            pass
        return
    await send_log(g, "member", "📥 Member Joined", f"{member.mention} (`{member.id}`)", 0x57F287)


# ───────────────────────── automod ─────────────────────────
INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\w+", re.I)
LINK_RE = re.compile(r"https?://\S+", re.I)


async def automod_check(m: discord.Message):
    d = gd(m.guild.id)
    am = d["automod"]
    if not am["enabled"] or m.author.guild_permissions.administrator or m.author.id in am["whitelist"]:
        return
    reason = None
    txt = m.content.lower()
    if am["invites"] and INVITE_RE.search(m.content):
        reason = "Invite link"
    elif am["links"] and LINK_RE.search(m.content):
        reason = "Link"
    elif len(m.mentions) + len(m.role_mentions) >= am["mention_limit"]:
        reason = "Mass mention"
    elif any(w and w in txt for w in am["badwords"]):
        reason = "Blocked word"
    else:
        dq = spam_times[(m.guild.id, m.author.id)]
        now = time.time()
        dq.append(now)
        while dq and now - dq[0] > am["spam_seconds"]:
            dq.popleft()
        if len(dq) >= am["spam_limit"]:
            reason = "Spam"
            dq.clear()
            try:
                await m.author.timeout(dt.timedelta(minutes=5), reason="AutoMod spam")
            except discord.HTTPException:
                pass
    if reason:
        try:
            await m.delete()
            await m.channel.send(f"{m.author.mention} ⚠️ AutoMod: **{reason}** not allowed.", delete_after=5)
        except discord.HTTPException:
            pass
        await send_log(m.guild, "mod", "🤖 AutoMod", f"{m.author.mention} — **{reason}** in {m.channel.mention}", 0xFEE75C)


@bot.event
async def on_message(m):
    if m.author.bot or not m.guild:
        return
    await automod_check(m)
    await bot.process_commands(m)


@bot.event
async def on_message_delete(m):
    if m.guild and not m.author.bot:
        await send_log(m.guild, "message", "🗑️ Message Deleted",
                       f"{m.author.mention} in {m.channel.mention}\n{(m.content or '*no text*')[:900]}", 0xED4245)


@bot.event
async def on_message_edit(a, b):
    if a.guild and not a.author.bot and a.content != b.content:
        await send_log(a.guild, "message", "✏️ Message Edited",
                       f"{a.author.mention} in {a.channel.mention}\n**Before:** {a.content[:450]}\n**After:** {b.content[:450]}")


# ───────────────────────── whitelist helper ─────────────────────────
def add_whitelist(parent, section):
    @parent.group(name="whitelist", invoke_without_command=True)
    @admin()
    async def wl(ctx):
        await ctx.send("Use: `add @user` / `remove @user` / `show` / `reset`")

    @wl.command(name="add")
    @admin()
    async def wl_add(ctx, user: discord.User):
        w = gd(ctx.guild.id)[section]["whitelist"]
        if user.id not in w:
            w.append(user.id)
            save()
        await ctx.send(embed=emb("✅ Whitelisted", f"{user.mention} added."))

    @wl.command(name="remove")
    @admin()
    async def wl_remove(ctx, user: discord.User):
        w = gd(ctx.guild.id)[section]["whitelist"]
        if user.id in w:
            w.remove(user.id)
            save()
        await ctx.send(embed=emb("✅ Removed", f"{user.mention} removed."))

    @wl.command(name="show")
    @admin()
    async def wl_show(ctx):
        w = gd(ctx.guild.id)[section]["whitelist"]
        await ctx.send(embed=emb("📃 Whitelist", "\n".join(f"<@{i}>" for i in w) or "Empty"))

    @wl.command(name="reset")
    @admin()
    async def wl_reset(ctx):
        gd(ctx.guild.id)[section]["whitelist"] = []
        save()
        await ctx.send(embed=emb("♻️ Whitelist Cleared"))


def onoff(v):
    return "🟢 ON" if v else "🔴 OFF"


# ───────────────────────── core antinuke ─────────────────────────
def status_embed(guild):
    d = gd(guild.id)
    e = emb("🛡️ Security Status")
    e.add_field(name="Core Antinuke", value=onoff(d["antinuke"]["enabled"]))
    e.add_field(name="SuperAntinuke", value=onoff(d["superantinuke"]["enabled"]))
    e.add_field(name="Raidmode", value=onoff(d["raidmode"]["enabled"]))
    e.add_field(name="AutoMod", value=onoff(d["automod"]["enabled"]))
    e.add_field(name="Nightmode", value=onoff(d["nightmode"]["enabled"]))
    e.add_field(name="Recovery", value="🟢 Snapshot saved" if d["snapshot"] else "🔴 No snapshot")
    return e


@bot.group(invoke_without_command=True)
async def antinuke(ctx):
    d = gd(ctx.guild.id)["antinuke"]
    await ctx.send(embed=emb("🛡️ Core Antinuke", f"Status: {onoff(d['enabled'])}\nPunishment: `{d['punishment']}`"))


@antinuke.command(name="enable")
@admin()
async def an_enable(ctx):
    gd(ctx.guild.id)["antinuke"]["enabled"] = True
    save()
    await ctx.send(embed=emb("🛡️ Core Antinuke", "🟢 Enabled", 0x57F287))


@antinuke.command(name="disable")
@admin()
async def an_disable(ctx):
    gd(ctx.guild.id)["antinuke"]["enabled"] = False
    save()
    await ctx.send(embed=emb("🛡️ Core Antinuke", "🔴 Disabled", 0xED4245))


@antinuke.command(name="status")
async def an_status(ctx):
    await ctx.send(embed=status_embed(ctx.guild))


@antinuke.command(name="settings")
@admin()
async def an_settings(ctx):
    t = gd(ctx.guild.id)["antinuke"]["thresholds"]
    await ctx.send(embed=emb("⚙️ Thresholds", "\n".join(f"`{k}` — {v[0]} per {v[1]}s" for k, v in t.items())))


add_whitelist(antinuke, "antinuke")


@bot.command()
@admin()
async def setthreshold(ctx, action: str, limit: int, period: int):
    t = gd(ctx.guild.id)["antinuke"]["thresholds"]
    if action not in t:
        return await ctx.send("Actions: " + ", ".join(f"`{k}`" for k in t))
    if limit < 1 or period < 1:
        return await ctx.send("Limit and period must be 1 or more.")
    t[action] = [limit, period]
    save()
    await ctx.send(embed=emb("✅ Threshold Set", f"`{action}` → **{limit}** per **{period}s**"))


# ───────────────────────── superantinuke ─────────────────────────
@bot.group(invoke_without_command=True)
async def superantinuke(ctx):
    d = gd(ctx.guild.id)["superantinuke"]
    await ctx.send(embed=emb("⚡ SuperAntinuke", f"Status: {onoff(d['enabled'])}"))


@superantinuke.command(name="enable")
@admin()
async def sa_enable(ctx):
    gd(ctx.guild.id)["superantinuke"]["enabled"] = True
    save()
    await ctx.send(embed=emb("⚡ SuperAntinuke MAX", "🟢 Enabled", 0x57F287))


@superantinuke.command(name="disable")
@admin()
async def sa_disable(ctx):
    gd(ctx.guild.id)["superantinuke"]["enabled"] = False
    save()
    await ctx.send(embed=emb("⚡ SuperAntinuke MAX", "🔴 Disabled", 0xED4245))


@superantinuke.command(name="config")
@admin()
async def sa_config(ctx):
    d = gd(ctx.guild.id)["superantinuke"]
    await ctx.send(embed=emb("⚡ SuperAntinuke Config",
                             f"Burst: **{d['burst'][0]}** actions / **{d['burst'][1]}s**\n"
                             f"Bot-add: `{d['bot_add']}`\nPunishment: `{d['punishment']}`"))


@superantinuke.command(name="verify")
async def sa_verify(ctx):
    p = ctx.guild.me.guild_permissions
    need = ["ban_members", "kick_members", "manage_channels", "manage_roles",
            "view_audit_log", "manage_guild", "moderate_members", "manage_messages"]
    await ctx.send(embed=emb("🔍 Permission Check",
                             "\n".join(f"{'✅' if getattr(p, n) else '❌'} {n}" for n in need)))


add_whitelist(superantinuke, "superantinuke")


# ───────────────────────── raidmode ─────────────────────────
@bot.group(invoke_without_command=True)
async def raidmode(ctx):
    await ctx.send(embed=emb("🚨 Raidmode", onoff(gd(ctx.guild.id)["raidmode"]["enabled"])))


@raidmode.command(name="on")
@admin()
async def rm_on(ctx):
    gd(ctx.guild.id)["raidmode"]["enabled"] = True
    save()
    await ctx.send(embed=emb("🚨 Raidmode", "🟢 Enabled — new joins will be kicked", 0xED4245))


@raidmode.command(name="off")
@admin()
async def rm_off(ctx):
    gd(ctx.guild.id)["raidmode"]["enabled"] = False
    save()
    await ctx.send(embed=emb("🚨 Raidmode", "🔴 Disabled", 0x57F287))


@raidmode.command(name="status")
async def rm_status(ctx):
    await ctx.send(embed=emb("🚨 Raidmode", onoff(gd(ctx.guild.id)["raidmode"]["enabled"])))


# ───────────────────────── automod commands ─────────────────────────
@bot.group(invoke_without_command=True)
async def automod(ctx):
    await ctx.send(embed=emb("🤖 AutoMod", onoff(gd(ctx.guild.id)["automod"]["enabled"])))


@automod.command(name="on")
@admin()
async def am_on(ctx):
    gd(ctx.guild.id)["automod"]["enabled"] = True
    save()
    await ctx.send(embed=emb("🤖 AutoMod", "🟢 Enabled", 0x57F287))


@automod.command(name="off")
@admin()
async def am_off(ctx):
    gd(ctx.guild.id)["automod"]["enabled"] = False
    save()
    await ctx.send(embed=emb("🤖 AutoMod", "🔴 Disabled", 0xED4245))


@automod.command(name="config")
@admin()
async def am_config(ctx):
    a = gd(ctx.guild.id)["automod"]
    keys = ["spam_limit", "spam_seconds", "mention_limit", "links", "invites", "badwords"]
    await ctx.send(embed=emb("🤖 AutoMod Config", "\n".join(f"`{k}` = {a[k]}" for k in keys)))


@automod.command(name="set")
@admin()
async def am_set(ctx, key: str, *, value: str):
    a = gd(ctx.guild.id)["automod"]
    if key in ("spam_limit", "spam_seconds", "mention_limit"):
        if not value.isdigit():
            return await ctx.send("Value must be a number.")
        a[key] = int(value)
    elif key in ("links", "invites"):
        a[key] = value.lower() in ("on", "true", "yes", "1")
    elif key == "badwords":
        a[key] = [w.strip().lower() for w in value.split(",") if w.strip()]
    else:
        return await ctx.send("Keys: spam_limit, spam_seconds, mention_limit, links, invites, badwords")
    save()
    await ctx.send(embed=emb("✅ AutoMod Updated", f"`{key}` = {a[key]}"))


add_whitelist(automod, "automod")


# ───────────────────────── nightmode ─────────────────────────
async def night_apply(guild, enable):
    n = gd(guild.id)["nightmode"]
    ev = guild.default_role
    if enable:
        for ch in guild.text_channels:
            if (ch.topic or "").startswith("ticket:"):
                continue
            ow = ch.overwrites_for(ev)
            if ow.send_messages is not False:
                ow.send_messages = False
                try:
                    await ch.set_permissions(ev, overwrite=ow, reason="Nightmode ON")
                    n["locked"].append(ch.id)
                except discord.HTTPException:
                    pass
                await asyncio.sleep(0.3)
    else:
        for cid in n["locked"]:
            ch = guild.get_channel(cid)
            if ch:
                ow = ch.overwrites_for(ev)
                ow.send_messages = None
                try:
                    await ch.set_permissions(ev, overwrite=ow, reason="Nightmode OFF")
                except discord.HTTPException:
                    pass
                await asyncio.sleep(0.3)
        n["locked"] = []
    n["enabled"] = enable
    save()
    await send_log(guild, "security", "🌙 Nightmode", "Enabled" if enable else "Disabled")


def night_embed(guild):
    n = gd(guild.id)["nightmode"]
    return emb("🌙 Nightmode",
               f"Status: {onoff(n['enabled'])}\nAuto: {onoff(n['auto'])}\n"
               f"Schedule (IST): ON **{n['on']}** → OFF **{n['off']}**")


@bot.group(invoke_without_command=True)
async def nightmode(ctx):
    await ctx.send(embed=night_embed(ctx.guild))


@nightmode.command(name="status")
async def nm_status(ctx):
    await ctx.send(embed=night_embed(ctx.guild))


@nightmode.command(name="enable")
@admin()
async def nm_enable(ctx):
    async with ctx.typing():
        await night_apply(ctx.guild, True)
    await ctx.send(embed=emb("🌙 Nightmode", "🟢 Enabled — chat locked for @everyone"))


@nightmode.command(name="disable")
@admin()
async def nm_disable(ctx):
    async with ctx.typing():
        await night_apply(ctx.guild, False)
    await ctx.send(embed=emb("🌙 Nightmode", "🔴 Disabled — chat unlocked"))


@nightmode.command(name="schedule")
@admin()
async def nm_schedule(ctx, on: str, off: str):
    try:
        for t in (on, off):
            dt.datetime.strptime(t, "%H:%M")
    except ValueError:
        return await ctx.send("Use 24h format HH:MM, e.g. `23:00 06:00`")
    n = gd(ctx.guild.id)["nightmode"]
    n["on"], n["off"] = on, off
    save()
    await ctx.send(embed=night_embed(ctx.guild))


@nightmode.command(name="auto")
@admin()
async def nm_auto(ctx, mode: str):
    gd(ctx.guild.id)["nightmode"]["auto"] = mode.lower() in ("on", "true", "yes")
    save()
    await ctx.send(embed=night_embed(ctx.guild))


@tasks.loop(seconds=30)
async def night_loop():
    now = dt.datetime.now(TZ).strftime("%H:%M")
    for g in bot.guilds:
        n = gd(g.id)["nightmode"]
        if not n["auto"]:
            continue
        if now == n["on"] and not n["enabled"]:
            await night_apply(g, True)
        elif now == n["off"] and n["enabled"]:
            await night_apply(g, False)


# ───────────────────────── logging ─────────────────────────
LOG_EVENTS = ["security", "mod", "message", "member"]


async def setup_logs(guild):
    d = gd(guild.id)
    ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
          guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    cat = discord.utils.get(guild.categories, name="Souvion Logs") or \
        await guild.create_category("Souvion Logs", overwrites=ow)
    for ev in LOG_EVENTS:
        name = f"{ev}-logs"
        ch = discord.utils.get(cat.text_channels, name=name) or \
            await guild.create_text_channel(name, category=cat, overwrites=ow)
        d["logs"][ev] = ch.id
    save()


@bot.group(invoke_without_command=True)
async def autologs(ctx):
    p = gd(ctx.guild.id)["prefix"]
    await ctx.send(embed=emb("📋 AutoLogs",
                             f"`{p}autologs setup` — create private log channels\n"
                             f"`{p}setlog <event> #channel` — events: {', '.join(LOG_EVENTS)}"))


@autologs.command(name="setup")
@admin()
async def autologs_setup(ctx):
    await setup_logs(ctx.guild)
    await ctx.send(embed=emb("📋 AutoLogs", "Private log channels ready ✅", 0x57F287))


@bot.command()
@admin()
async def setlog(ctx, event: str, channel: discord.TextChannel):
    if event not in LOG_EVENTS:
        return await ctx.send("Events: " + ", ".join(LOG_EVENTS))
    gd(ctx.guild.id)["logs"][event] = channel.id
    save()
    await ctx.send(embed=emb("✅ Log Routed", f"`{event}` → {channel.mention}"))


# ───────────────────────── tickets ─────────────────────────
async def open_ticket(guild, user, respond):
    for c in guild.text_channels:
        if c.topic == f"ticket:{user.id}":
            return await respond(f"You already have a ticket: {c.mention}")
    cat = discord.utils.get(guild.categories, name="Tickets") or await guild.create_category("Tickets")
    ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
          user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
          guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
    for r in guild.roles:
        if r.permissions.manage_channels or r.permissions.administrator:
            if not r.is_default() and not r.managed:
                ow[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    ch = await guild.create_text_channel(f"ticket-{user.name}"[:90], category=cat,
                                         overwrites=ow, topic=f"ticket:{user.id}")
    await ch.send(f"{user.mention}", embed=emb("🎫 Support Ticket",
                  "Staff will be with you shortly. Use the ticket close command to close this ticket."))
    await respond(f"Ticket created: {ch.mention}")


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", emoji="🎫", style=discord.ButtonStyle.primary,
                       custom_id="souvion:ticket:create")
    async def create(self, i: discord.Interaction, b):
        await i.response.defer(ephemeral=True)
        await open_ticket(i.guild, i.user, lambda t: i.followup.send(t, ephemeral=True))


@bot.group(invoke_without_command=True)
async def ticket(ctx):
    p = gd(ctx.guild.id)["prefix"]
    await ctx.send(embed=emb("🎫 Tickets", f"`{p}ticket setup` • `{p}ticket create` • `{p}ticket close`"))


@ticket.command(name="setup")
@commands.has_permissions(manage_channels=True)
async def ticket_setup(ctx):
    await ctx.send(embed=emb("🎫 Support", "Need help? Click the button below to open a private ticket."),
                   view=TicketPanel())


@ticket.command(name="create")
async def ticket_create(ctx):
    await open_ticket(ctx.guild, ctx.author, ctx.send)


@ticket.command(name="close")
async def ticket_close(ctx):
    t = ctx.channel.topic or ""
    if not t.startswith("ticket:"):
        return await ctx.send("This isn't a ticket channel.")
    if not (ctx.author.guild_permissions.manage_channels or str(ctx.author.id) == t.split(":")[1]):
        return await ctx.send("Only the ticket owner or staff can close this.")
    await ctx.send("🔒 Closing ticket in 5 seconds...")
    await asyncio.sleep(5)
    await ctx.channel.delete(reason=f"Ticket closed by {ctx.author}")


# ───────────────────────── moderation ─────────────────────────
def can_act(ctx, m: discord.Member):
    return (m != ctx.author and m.id != ctx.guild.owner_id and
            (ctx.author.id == ctx.guild.owner_id or ctx.author.top_role > m.top_role) and
            ctx.guild.me.top_role > m.top_role)


@bot.group(invoke_without_command=True)
async def mod(ctx):
    p = gd(ctx.guild.id)["prefix"]
    await ctx.send(embed=cat_embed("moderation", p, ctx.author))


@mod.command(name="warn")
@commands.has_permissions(moderate_members=True)
async def mod_warn(ctx, member: discord.Member, *, reason: str = "No reason"):
    w = gd(ctx.guild.id)["warns"].setdefault(str(member.id), [])
    w.append({"by": ctx.author.id, "reason": reason, "t": int(time.time())})
    save()
    await ctx.send(embed=emb("⚠️ Warned", f"{member.mention} — {reason}\nTotal warns: **{len(w)}**", 0xFEE75C))
    await send_log(ctx.guild, "mod", "⚠️ Warn", f"{member.mention} by {ctx.author.mention}: {reason}")


@mod.command(name="timeout")
@commands.has_permissions(moderate_members=True)
async def mod_timeout(ctx, member: discord.Member, minutes: int, *, reason: str = "No reason"):
    if not can_act(ctx, member):
        return await ctx.send("❌ Can't act on that member (role hierarchy).")
    await member.timeout(dt.timedelta(minutes=minutes), reason=reason)
    await ctx.send(embed=emb("⏳ Timed Out", f"{member.mention} for **{minutes}m** — {reason}", 0xFEE75C))
    await send_log(ctx.guild, "mod", "⏳ Timeout", f"{member.mention} {minutes}m by {ctx.author.mention}: {reason}")


@mod.command(name="kick")
@commands.has_permissions(kick_members=True)
async def mod_kick(ctx, member: discord.Member, *, reason: str = "No reason"):
    if not can_act(ctx, member):
        return await ctx.send("❌ Can't act on that member (role hierarchy).")
    await member.kick(reason=reason)
    await ctx.send(embed=emb("👢 Kicked", f"{member} — {reason}", 0xED4245))
    await send_log(ctx.guild, "mod", "👢 Kick", f"{member} by {ctx.author.mention}: {reason}")


@mod.command(name="ban")
@commands.has_permissions(ban_members=True)
async def mod_ban(ctx, member: discord.Member, *, reason: str = "No reason"):
    if not can_act(ctx, member):
        return await ctx.send("❌ Can't act on that member (role hierarchy).")
    await member.ban(reason=reason)
    await ctx.send(embed=emb("🔨 Banned", f"{member} — {reason}", 0xED4245))


# ───────────────────────── setup commands ─────────────────────────
SETUP_KEYS = [("Core Antinuke", "antinuke"), ("SuperAntinuke", "superantinuke"),
              ("Raidmode", "raidmode"), ("AutoMod", "automod")]


def setup_embed(guild):
    d = gd(guild.id)
    e = emb("⚙️ Security Setup", "Click a button to toggle a module.")
    for label, key in SETUP_KEYS:
        e.add_field(name=label, value=onoff(d[key]["enabled"]))
    return e


class SetupView(discord.ui.View):
    def __init__(self, author_id, guild):
        super().__init__(timeout=300)
        self.author_id, self.guild = author_id, guild
        for label, key in SETUP_KEYS:
            self.add_item(self.make_btn(label, key))

    def make_btn(self, label, key):
        on = gd(self.guild.id)[key]["enabled"]
        b = discord.ui.Button(label=label, style=discord.ButtonStyle.success if on else discord.ButtonStyle.danger)

        async def cb(i: discord.Interaction):
            d = gd(self.guild.id)
            d[key]["enabled"] = not d[key]["enabled"]
            save()
            await i.response.edit_message(embed=setup_embed(self.guild), view=SetupView(self.author_id, self.guild))
        b.callback = cb
        return b

    async def interaction_check(self, i):
        if i.user.id != self.author_id:
            await i.response.send_message("Not your panel.", ephemeral=True)
            return False
        return True


@bot.command(aliases=["setupsecurity"])
@admin()
async def securitysetup(ctx):
    await ctx.send(embed=setup_embed(ctx.guild), view=SetupView(ctx.author.id, ctx.guild))


@bot.command()
@admin()
async def autosetup(ctx):
    async with ctx.typing():
        d = gd(ctx.guild.id)
        for _, key in SETUP_KEYS:
            d[key]["enabled"] = True
        save()
        await setup_logs(ctx.guild)
        r, c = await take_snapshot(ctx.guild)
    await ctx.send(embed=emb("🛡️ MAX Security Enabled",
                             "✅ Core Antinuke\n✅ SuperAntinuke\n✅ Raidmode\n✅ AutoMod\n"
                             f"✅ Private logs\n✅ Snapshot ({r} roles, {c} channels)", 0x57F287))


# ───────────────────────── errors / startup ─────────────────────────
@bot.event
async def on_command_error(ctx, err):
    if isinstance(err, commands.CommandNotFound):
        return
    if isinstance(err, commands.MissingPermissions):
        return await ctx.send("❌ You don't have permission for that.")
    if isinstance(err, (commands.MissingRequiredArgument, commands.BadArgument)):
        return await ctx.send(f"❌ Wrong usage: {err}")
    if isinstance(err, commands.CommandInvokeError) and isinstance(err.original, discord.Forbidden):
        return await ctx.send("❌ I lack permission (move my role higher / give Administrator).")
    print("Error:", repr(err))


@bot.event
async def setup_hook():
    load()
    bot.add_view(TicketPanel())
    night_loop.start()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | {len(bot.guilds)} servers")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching,
                                                        name="over your server | !help"))


# ───────────────────────── chat lock / unlock ─────────────────────────

@bot.command(name="lock")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def lock_chat(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel

    if not channel.permissions_for(ctx.guild.me).manage_channels:
        return await ctx.send("❌ Bot ko Manage Channels permission chahiye.")

    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False

    try:
        await channel.set_permissions(
            ctx.guild.default_role,
            overwrite=overwrite,
            reason=f"Chat locked by {ctx.author}"
        )
        await ctx.send(
            embed=emb(
                "🔒 Chat Locked",
                f"{channel.mention} mein @everyone ab message nahi bhej sakta.",
                0xED4245
            )
        )
    except discord.Forbidden:
        await ctx.send("❌ Bot ke paas required permission nahi hai.")
    except discord.HTTPException:
        await ctx.send("❌ Channel lock nahi ho paya.")


@bot.command(name="unlock")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def unlock_chat(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel

    if not channel.permissions_for(ctx.guild.me).manage_channels:
        return await ctx.send("❌ Bot ko Manage Channels permission chahiye.")

    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None

    try:
        await channel.set_permissions(
            ctx.guild.default_role,
            overwrite=overwrite,
            reason=f"Chat unlocked by {ctx.author}"
        )
        await ctx.send(
            embed=emb(
                "🔓 Chat Unlocked",
                f"{channel.mention} ka chat permission default par reset kar diya gaya.",
                0x57F287
            )
        )
    except discord.Forbidden:
        await ctx.send("❌ Bot ke paas required permission nahi hai.")
    except discord.HTTPException:
        await ctx.send("❌ Channel unlock nahi ho paya.")


# ─────────────── VOICE RECORDING SYSTEM ───────────────
import re

RECORD_DIR = Path("recordings")
RECORD_DIR.mkdir(exist_ok=True)

recording_sessions = {}
pending_recordings = {}


def recording_folder(guild_id):
    folder = RECORD_DIR / str(guild_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def safe_recording_name(name):
    name = re.sub(r"[^a-zA-Z0-9 _-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:60] or "voice_recording"


async def begin_voice_recording(ctx, voice_channel, name):
    guild = ctx.guild

    if guild.id in recording_sessions:
        raise RuntimeError("Is server mein recording already active hai.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_recording_name(name)}_{stamp}.wav"
    path = recording_folder(guild.id) / filename

    vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
    file_handle = open(path, "wb")
    sink = voice_recv.WaveSink(file_handle)

    loop = asyncio.get_running_loop()
    finished = asyncio.Event()

    def after_recording(error):
        loop.call_soon_threadsafe(finished.set)

    recording_sessions[guild.id] = {
        "vc": vc,
        "sink": sink,
        "path": path,
        "name": name,
        "owner_id": ctx.author.id,
        "text_channel": ctx.channel,
        "voice_channel_id": voice_channel.id,
        "finished": finished,
        "after_error": None,
    }

    try:
        vc.listen(sink, after=after_recording)
    except Exception:
        recording_sessions.pop(guild.id, None)
        file_handle.close()
        await vc.disconnect(force=True)
        raise

    await ctx.channel.send(
        embed=emb(
            "🔴 Recording Started",
            f"**Name:** {name}\n"
            f"**Voice Channel:** {voice_channel.mention}\n\n"
            "Recording active hai. Stop karne ke liye `!record stop` use karo.",
            0xED4245
        )
    )


class RecordingConsentView(discord.ui.View):
    def __init__(self, ctx, voice_channel, name):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.voice_channel = voice_channel
        self.name = name
        self.consented = set()
        self.message = None
        self.started = False

    @discord.ui.button(
        label="I Consent to Recording",
        emoji="🎙️",
        style=discord.ButtonStyle.success
    )
    async def consent(self, interaction, button):
        member = interaction.user

        if not isinstance(member, discord.Member):
            return await interaction.response.send_message(
                "Server members only.", ephemeral=True
            )

        if member.bot or not member.voice or member.voice.channel.id != self.voice_channel.id:
            return await interaction.response.send_message(
                "Consent dene ke liye usi voice channel mein hona zaroori hai.",
                ephemeral=True
            )

        self.consented.add(member.id)

        required = {
            m.id for m in self.voice_channel.members
            if not m.bot
        }

        if not required.issubset(self.consented):
            await interaction.response.send_message(
                f"Consent registered. Abhi {len(self.consented)}/{len(required)} "
                "members ne consent diya hai.",
                ephemeral=True
            )
            return

        if self.started:
            return await interaction.response.send_message(
                "Recording process already started.", ephemeral=True
            )

        self.started = True
        button.disabled = True

        await interaction.response.edit_message(
            content="✅ Sabhi current members ne consent diya. Recording connect ho rahi hai.",
            view=self
        )

        try:
            await begin_voice_recording(
                self.ctx, self.voice_channel, self.name
            )
        except Exception as error:
            await self.ctx.channel.send(
                f"❌ Recording start nahi hui: `{type(error).__name__}: {error}`"
            )

    async def on_timeout(self):
        if not self.started and self.message:
            try:
                await self.message.edit(
                    content="⌛ Consent request expire ho gayi. Recording start nahi hui.",
                    view=None
                )
            except discord.HTTPException:
                pass

        pending_recordings.pop(self.ctx.guild.id, None)


class RecordingDeliveryView(discord.ui.View):
    def __init__(self, guild_id, owner_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.selected_file = None
        self.selected_channel = None

        folder = recording_folder(guild_id)
        files = sorted(
            folder.glob("*.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )[:25]

        options = [
            discord.SelectOption(
                label=p.stem[:100],
                value=p.name,
                description="Saved voice recording"
            )
            for p in files
        ]

        self.record_select.options = options
        self.record_select.disabled = not options

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id and not (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Ye recording panel sirf recording owner ya administrator use kar sakta hai.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        placeholder="🎧 Select which recording to send",
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label="Loading recordings", value="none")],
        row=0
    )
    async def record_select(self, interaction, select):
        self.selected_file = select.values[0]
        await interaction.response.send_message(
            f"Selected recording: `{self.selected_file}`",
            ephemeral=True
        )

    @discord.ui.channel_select(
        placeholder="📨 Select destination text channel",
        channel_types=[discord.ChannelType.text],
        min_values=1,
        max_values=1,
        row=1
    )
    async def channel_select(self, interaction, select):
        self.selected_channel = select.values[0]
        await interaction.response.send_message(
            f"Destination selected: {self.selected_channel.mention}",
            ephemeral=True
        )

    @discord.ui.button(
        label="Send Recording",
        emoji="📤",
        style=discord.ButtonStyle.primary,
        row=2
    )
    async def send_recording(self, interaction, button):
        if not self.selected_file or not self.selected_channel:
            return await interaction.response.send_message(
                "Pehle recording aur destination channel dono select karo.",
                ephemeral=True
            )

        path = recording_folder(self.guild_id) / self.selected_file

        if not path.is_file() or path.parent != recording_folder(self.guild_id):
            return await interaction.response.send_message(
                "Recording file nahi mili.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            await self.selected_channel.send(
                content=f"🎙️ **Voice Recording:** `{path.stem}`\n"
                        "Share only with people who have permission to hear it.",
                file=discord.File(path, filename=path.name)
            )
            await interaction.followup.send(
                f"✅ Recording {self.selected_channel.mention} mein bhej di gayi.",
                ephemeral=True
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "❌ Upload fail hua. File size aur bot ke channel permissions check karo.",
                ephemeral=True
            )


async def finish_voice_recording(guild, reason="Recording stopped"):
    session = recording_sessions.get(guild.id)
    if not session:
        return

    vc = session["vc"]
    try:
        vc.stop_listening()
        try:
            await asyncio.wait_for(session["finished"].wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
    finally:
        if vc.is_connected():
            await vc.disconnect(force=True)

        recording_sessions.pop(guild.id, None)

    path = session["path"]

    if not path.exists() or path.stat().st_size < 100:
        await session["text_channel"].send(
            "❌ Recording file empty hai ya create nahi hui."
        )
        return

    await session["text_channel"].send(
        embed=emb(
            "⏹️ Recording Saved",
            f"**Name:** {session['name']}\n"
            f"**File:** `{path.name}`\n"
            f"**Status:** {reason}\n\n"
            "Neeche dropdown se recording aur destination channel select karo.",
            0x57F287
        ),
        view=RecordingDeliveryView(guild.id, session["owner_id"])
    )


@bot.group(name="record", invoke_without_command=True)
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def record_group(ctx):
    p = gd(ctx.guild.id)["prefix"]
    await ctx.send(
        embed=emb(
            "🎙️ Voice Recording",
            f"`{p}record start <name>` — consent request bhejo\n"
            f"`{p}record stop` — active recording stop karo\n"
            f"`{p}record list` — saved recordings ka panel kholo"
        )
    )


@record_group.command(name="start")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def record_start(ctx, *, name: str):
    if ctx.guild.id in recording_sessions or ctx.guild.id in pending_recordings:
        return await ctx.send("❌ Is server mein recording ya consent request already active hai.")

    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("Pehle voice channel join karo.")

    voice_channel = ctx.author.voice.channel
    members = [m for m in voice_channel.members if not m.bot]

    if not members:
        return await ctx.send("Voice channel mein koi member nahi hai.")

    view = RecordingConsentView(ctx, voice_channel, name.strip().strip('"'))
    pending_recordings[ctx.guild.id] = view

    view.message = await ctx.send(
        embed=emb(
            "🔔 Voice Recording Consent Required",
            f"**Proposed recording:** {name}\n"
            f"**Voice channel:** {voice_channel.mention}\n\n"
            "Audio record hogi aur baad mein selected text channel mein share ki ja sakti hai. "
            "Har non-bot member ko neeche button dabakar explicitly consent dena hoga. "
            "Consent na dene wale member ki audio record nahi honi chahiye; sabka consent "
            "milne se pehle recording start nahi hogi. Request 120 seconds mein expire hogi.",
            0xFEE75C
        ),
        view=view
    )


@record_group.command(name="stop")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def record_stop(ctx):
    session = recording_sessions.get(ctx.guild.id)

    if not session:
        return await ctx.send("❌ Koi active recording nahi hai.")

    if (
        ctx.author.id != session["owner_id"]
        and not ctx.author.guild_permissions.administrator
    ):
        return await ctx.send("❌ Sirf recording owner ya administrator stop kar sakta hai.")

    await finish_voice_recording(ctx.guild)


@record_group.command(name="list")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def record_list(ctx):
    await ctx.send(
        embed=emb(
            "🎧 Saved Recordings",
            "Recording select karo, phir destination text channel select karke **Send Recording** dabao."
        ),
        view=RecordingDeliveryView(ctx.guild.id, ctx.author.id)
    )


@bot.listen("on_voice_state_update")
async def recording_stop_if_new_member_joins(member, before, after):
    if member.bot or before.channel == after.channel or not after.channel:
        return

    session = recording_sessions.get(member.guild.id)

    # New member enters while recording: stop to avoid capturing them without consent.
    if session and after.channel.id == session["voice_channel_id"]:
        await finish_voice_recording(
            member.guild,
            reason="Automatically stopped because a new member joined the voice channel"
        )


bot.run(TOKEN)
