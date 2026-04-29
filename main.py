import discord
from discord.ext import commands
import asyncio
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
import re
import os
import json

# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────
ACTIVITY_FILE = "activity_data.json"   # persists last-seen timestamps
PAGE_SIZE = 15                          # members shown per confirmation page
KICK_BAN_DELAY = 1.0                    # seconds between each kick/ban (rate-limit safety)

# ──────────────────────────────────────────────
#  FLASK KEEP-ALIVE (for Render / UptimeRobot)
# ──────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "Purginator Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()

# ──────────────────────────────────────────────
#  BOT SETUP
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True
intents.presences = True  # needed for online/offline status tracking

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

# In-memory activity cache: { "guild_id:user_id": "ISO timestamp" }
activity_data: dict[str, str] = {}


# ──────────────────────────────────────────────
#  ACTIVITY PERSISTENCE
# ──────────────────────────────────────────────
def load_activity():
    global activity_data
    if os.path.exists(ACTIVITY_FILE):
        try:
            with open(ACTIVITY_FILE, "r") as f:
                activity_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            activity_data = {}


def save_activity():
    try:
        with open(ACTIVITY_FILE, "w") as f:
            json.dump(activity_data, f)
    except OSError:
        pass


def record_activity(guild_id: int, user_id: int):
    """Record the current UTC time as last activity for a member."""
    key = f"{guild_id}:{user_id}"
    activity_data[key] = datetime.now(timezone.utc).isoformat()


def get_last_active(guild_id: int, user_id: int) -> datetime | None:
    """Return the last-active datetime (UTC) or None if never recorded."""
    key = f"{guild_id}:{user_id}"
    ts = activity_data.get(key)
    if ts:
        return datetime.fromisoformat(ts)
    return None


# ──────────────────────────────────────────────
#  EVENTS — track activity
# ──────────────────────────────────────────────
@bot.event
async def on_ready():
    load_activity()
    print(f"✅ Logged in as {bot.user}  |  Tracking {len(activity_data)} activity records")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return
    record_activity(message.guild.id, message.author.id)
    await bot.process_commands(message)


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if user.bot or not reaction.message.guild:
        return
    record_activity(reaction.message.guild.id, user.id)


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if member.bot:
        return
    if after.channel is not None:
        record_activity(member.guild.id, member.id)


# Periodically save activity data to disk (every 5 min)
@bot.event
async def on_connect():
    bot.loop.create_task(_auto_save_loop())


async def _auto_save_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        save_activity()
        await asyncio.sleep(300)


# ──────────────────────────────────────────────
#  ARGUMENT PARSER (shared by masskick & massban)
# ──────────────────────────────────────────────
class ParsedArgs:
    def __init__(self):
        self.role: discord.Role | None = None
        self.date_filter: datetime | None = None
        self.date_type: str | None = None
        self.inactive_days: int | None = None


def parse_command_args(ctx: commands.Context, args: str) -> tuple[ParsedArgs | None, str | None]:
    parsed = ParsedArgs()

    # ── Role ──
    role_match = re.search(r"<@&(\d+)>", args)
    role_name_match = re.search(r"role:(\S+)", args, re.IGNORECASE)

    if role_match:
        parsed.role = ctx.guild.get_role(int(role_match.group(1)))
    elif role_name_match:
        name = role_name_match.group(1)
        parsed.role = discord.utils.find(lambda r: r.name.lower() == name.lower(), ctx.guild.roles)

    if not parsed.role:
        return None, (
            "❌ **Role not found.** Mention a role or use `role:RoleName`.\n"
            "Example: `!masskick @Visitors before:2025-08-08`"
        )

    # ── Date filter (optional) ──
    date_match = re.search(r"(before|after|on):(\S+)", args, re.IGNORECASE)
    if date_match:
        parsed.date_type = date_match.group(1).lower()
        date_str = date_match.group(2)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return None, (
                f"❌ Invalid date format `{date_str}`. Use **YYYY-MM-DD**.\n"
                f"Example: `!masskick @Visitors before:2025-08-08`"
            )
        try:
            parsed.date_filter = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return None, f"❌ Invalid date `{date_str}`. Make sure the date actually exists."

    # ── Inactive days (optional) ──
    inactive_match = re.search(r"inactive:(\d+)", args, re.IGNORECASE)
    if inactive_match:
        parsed.inactive_days = int(inactive_match.group(1))
        if parsed.inactive_days < 1:
            return None, "❌ `inactive:` value must be at least 1 day."

    if parsed.date_filter is None and parsed.inactive_days is None:
        return None, (
            "❌ **Please provide at least one filter:**\n"
            "• `before:YYYY-MM-DD` / `after:YYYY-MM-DD` / `on:YYYY-MM-DD`  — filter by join date\n"
            "• `inactive:30` — members with no activity for 30+ days\n"
            "You can combine both filters."
        )

    return parsed, None


# ──────────────────────────────────────────────
#  MEMBER FILTERING
# ──────────────────────────────────────────────
def filter_members(guild: discord.Guild, parsed: ParsedArgs) -> list[discord.Member]:
    now = datetime.now(timezone.utc)
    results = []

    for member in parsed.role.members:
        if member.bot:
            continue

        if parsed.date_filter and parsed.date_type:
            joined = member.joined_at
            if not joined:
                continue
            joined_naive = joined.replace(tzinfo=None)
            if parsed.date_type == "before" and joined_naive >= parsed.date_filter:
                continue
            if parsed.date_type == "after" and joined_naive <= parsed.date_filter:
                continue
            if parsed.date_type == "on" and joined_naive.date() != parsed.date_filter.date():
                continue

        if parsed.inactive_days is not None:
            last_active = get_last_active(guild.id, member.id)
            if last_active is None:
                last_active = member.joined_at
            if last_active is None:
                continue
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            days_since = (now - last_active).days
            if days_since < parsed.inactive_days:
                continue

        results.append(member)

    return results


# ──────────────────────────────────────────────
#  CONFIRMATION UI (shared)
# ──────────────────────────────────────────────
async def confirm_action(
    ctx: commands.Context,
    members: list[discord.Member],
    action_word: str,
    role: discord.Role,
    parsed: ParsedArgs,
) -> bool:
    now_utc = datetime.now(timezone.utc)

    def format_member(m: discord.Member) -> str:
        joined = m.joined_at.date() if m.joined_at else "?"
        last = get_last_active(m.guild.id, m.id)
        if last:
            days_ago = (now_utc - last).days
            last_str = f"{days_ago}d ago"
        else:
            last_str = "n/a"
        return f"{m.display_name}  |  joined: {joined}  |  last seen: {last_str}"

    entries = [format_member(m) for m in members]
    pages = [entries[i:i + PAGE_SIZE] for i in range(0, len(entries), PAGE_SIZE)]
    total_pages = len(pages)
    current_page = 0

    def build_embed(page_idx: int) -> discord.Embed:
        embed = discord.Embed(
            title=f"⚠️ Mass {action_word.title()} Confirmation",
            color=discord.Color.orange(),
        )
        header = f"**{len(members)}** members with role {role.mention} will be **{action_word}ed**."
        filters = []
        if parsed.date_type and parsed.date_filter:
            filters.append(f"Join date **{parsed.date_type}** `{parsed.date_filter.date()}`")
        if parsed.inactive_days is not None:
            filters.append(f"Inactive for **{parsed.inactive_days}+ days**")
        if filters:
            header += "\nFilters: " + " **AND** ".join(filters)
        embed.description = header
        body = "\n".join(pages[page_idx])
        if len(body) > 1000:
            body = body[:997] + "..."
        embed.add_field(name="Preview", value=f"```{body}```", inline=False)
        embed.set_footer(text=f"Page {page_idx + 1}/{total_pages}  •  ◀️▶️ navigate  •  ✅ confirm  •  ❌ cancel")
        return embed

    msg = await ctx.send(embed=build_embed(current_page))
    emojis = ["◀️", "▶️", "✅", "❌"]
    for em in emojis:
        try:
            await msg.add_reaction(em)
        except Exception:
            pass

    def check(reaction, user):
        return user == ctx.author and reaction.message.id == msg.id and str(reaction.emoji) in emojis

    while True:
        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=120.0, check=check)
        except asyncio.TimeoutError:
            try:
                await msg.clear_reactions()
            except Exception:
                pass
            await ctx.send("⏳ Timed out — action cancelled.")
            return False

        emoji = str(reaction.emoji)
        try:
            await msg.remove_reaction(reaction.emoji, user)
        except Exception:
            pass

        if emoji == "◀️":
            current_page = (current_page - 1) % total_pages
            await msg.edit(embed=build_embed(current_page))
        elif emoji == "▶️":
            current_page = (current_page + 1) % total_pages
            await msg.edit(embed=build_embed(current_page))
        elif emoji == "❌":
            try:
                await msg.clear_reactions()
            except Exception:
                pass
            await ctx.send(f"❌ Mass {action_word} cancelled.")
            return False
        elif emoji == "✅":
            try:
                await msg.clear_reactions()
            except Exception:
                pass
            return True


# ──────────────────────────────────────────────
#  !masskick
# ──────────────────────────────────────────────
@bot.command(name="masskick")
@commands.has_permissions(kick_members=True)
async def masskick(ctx: commands.Context, *, args: str = None):
    if not args:
        await ctx.send(
            "**Usage:** `!masskick @Role [before:|after:|on:YYYY-MM-DD] [inactive:DAYS]`\n"
            "Example: `!masskick @Visitors before:2025-08-08 inactive:30`"
        )
        return

    parsed, error = parse_command_args(ctx, args)
    if error:
        await ctx.send(error)
        return

    await ctx.send("🔍 Scanning members… this may take a moment.")
    members = filter_members(ctx.guild, parsed)

    if not members:
        await ctx.send("✅ No members matched all the given filters. Nothing to do.")
        return

    confirmed = await confirm_action(ctx, members, "kick", parsed.role, parsed)
    if not confirmed:
        return

    if not ctx.guild.me.guild_permissions.kick_members:
        await ctx.send("❌ I lack the **Kick Members** permission.")
        return

    kicked, failed = 0, []
    progress_msg = await ctx.send(f"⏳ Kicking 0/{len(members)}…")

    for i, member in enumerate(members, 1):
        try:
            await member.kick(reason=f"Mass kick by {ctx.author} | Role: {parsed.role.name}")
            kicked += 1
        except discord.Forbidden:
            failed.append(f"{member.display_name} — insufficient permissions / role hierarchy")
        except Exception as e:
            failed.append(f"{member.display_name} — {type(e).__name__}")

        if i % 10 == 0 or i == len(members):
            try:
                await progress_msg.edit(content=f"⏳ Kicking {i}/{len(members)}…")
            except Exception:
                pass

        await asyncio.sleep(KICK_BAN_DELAY)

    await _send_result_embed(ctx, "Kick", kicked, len(members), failed)


# ──────────────────────────────────────────────
#  !massban
# ──────────────────────────────────────────────
@bot.command(name="massban")
@commands.has_permissions(ban_members=True)
async def massban(ctx: commands.Context, *, args: str = None):
    if not args:
        await ctx.send(
            "**Usage:** `!massban @Role [before:|after:|on:YYYY-MM-DD] [inactive:DAYS]`\n"
            "Example: `!massban @Raiders after:2025-07-01 inactive:14`"
        )
        return

    parsed, error = parse_command_args(ctx, args)
    if error:
        await ctx.send(error)
        return

    await ctx.send("🔍 Scanning members… this may take a moment.")
    members = filter_members(ctx.guild, parsed)

    if not members:
        await ctx.send("✅ No members matched all the given filters. Nothing to do.")
        return

    confirmed = await confirm_action(ctx, members, "ban", parsed.role, parsed)
    if not confirmed:
        return

    if not ctx.guild.me.guild_permissions.ban_members:
        await ctx.send("❌ I lack the **Ban Members** permission.")
        return

    banned, failed = 0, []
    progress_msg = await ctx.send(f"⏳ Banning 0/{len(members)}…")

    for i, member in enumerate(members, 1):
        try:
            await member.ban(
                reason=f"Mass ban by {ctx.author} | Role: {parsed.role.name}",
                delete_message_days=0,
            )
            banned += 1
        except discord.Forbidden:
            failed.append(f"{member.display_name} — insufficient permissions / role hierarchy")
        except Exception as e:
            failed.append(f"{member.display_name} — {type(e).__name__}")

        if i % 10 == 0 or i == len(members):
            try:
                await progress_msg.edit(content=f"⏳ Banning {i}/{len(members)}…")
            except Exception:
                pass

        await asyncio.sleep(KICK_BAN_DELAY)

    await _send_result_embed(ctx, "Ban", banned, len(members), failed)


# ──────────────────────────────────────────────
#  RESULT EMBED (shared)
# ──────────────────────────────────────────────
async def _send_result_embed(ctx, action: str, success: int, total: int, failed: list[str]):
    embed = discord.Embed(
        title=f"{'✅' if not failed else '⚠️'} Mass {action} Results",
        color=discord.Color.green() if not failed else discord.Color.orange(),
    )
    embed.add_field(
        name="Summary",
        value=f"**{success}** / {total} members {action.lower()}ned successfully.",
        inline=False,
    )
    if failed:
        max_show = 25
        text = "\n".join(failed[:max_show])
        if len(failed) > max_show:
            text += f"\n…and {len(failed) - max_show} more"
        embed.add_field(name=f"Failed ({len(failed)})", value=f"```{text}```", inline=False)

    await ctx.send(embed=embed)


# ──────────────────────────────────────────────
#  !activity — check a single member's last seen
# ──────────────────────────────────────────────
@bot.command(name="activity")
@commands.has_permissions(kick_members=True)
async def activity_cmd(ctx: commands.Context, member: discord.Member = None):
    if not member:
        await ctx.send("**Usage:** `!activity @User` — shows their last recorded activity.")
        return

    last = get_last_active(ctx.guild.id, member.id)
    now = datetime.now(timezone.utc)

    embed = discord.Embed(title=f"Activity — {member.display_name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Joined", value=str(member.joined_at.date()) if member.joined_at else "Unknown", inline=True)

    if last:
        days_ago = (now - last).days
        embed.add_field(name="Last Seen", value=f"{last.strftime('%Y-%m-%d %H:%M UTC')}\n({days_ago} days ago)", inline=True)
    else:
        embed.add_field(name="Last Seen", value="No activity recorded yet", inline=True)

    roles = [r.mention for r in member.roles if r != ctx.guild.default_role]
    if roles:
        embed.add_field(name="Roles", value=" ".join(roles), inline=False)

    await ctx.send(embed=embed)


# ──────────────────────────────────────────────
#  !inactive — list inactive members of a role
# ──────────────────────────────────────────────
@bot.command(name="inactive")
@commands.has_permissions(kick_members=True)
async def inactive_cmd(ctx: commands.Context, *, args: str = None):
    if not args:
        await ctx.send(
            "**Usage:** `!inactive @Role DAYS`\n"
            "Example: `!inactive @Members 30` — lists members of that role inactive 30+ days."
        )
        return

    role_match = re.search(r"<@&(\d+)>", args)
    role_name_match = re.search(r"role:(\S+)", args, re.IGNORECASE)
    role = None
    if role_match:
        role = ctx.guild.get_role(int(role_match.group(1)))
    elif role_name_match:
        name = role_name_match.group(1)
        role = discord.utils.find(lambda r: r.name.lower() == name.lower(), ctx.guild.roles)

    if not role:
        await ctx.send("❌ Role not found. Mention a role or use `role:RoleName`.")
        return

    days_match = re.search(r"(\d+)", args.replace(str(role.id), ""))
    if not days_match:
        await ctx.send("❌ Please provide the number of days. Example: `!inactive @Role 30`")
        return
    days = int(days_match.group(1))

    now = datetime.now(timezone.utc)
    inactive_list = []
    for member in role.members:
        if member.bot:
            continue
        last = get_last_active(ctx.guild.id, member.id)
        if last is None:
            last = member.joined_at
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        days_since = (now - last).days
        if days_since >= days:
            inactive_list.append((member, days_since))

    if not inactive_list:
        await ctx.send(f"✅ No members with role **{role.name}** have been inactive for {days}+ days.")
        return

    inactive_list.sort(key=lambda x: x[1], reverse=True)

    entries = [f"{m.display_name} — {d} days" for m, d in inactive_list]
    pages = [entries[i:i + PAGE_SIZE] for i in range(0, len(entries), PAGE_SIZE)]

    embed = discord.Embed(
        title=f"😴 Inactive Members — {role.name}",
        description=f"**{len(inactive_list)}** members inactive for **{days}+** days.",
        color=discord.Color.greyple(),
    )
    body = "\n".join(pages[0])
    if len(body) > 1000:
        body = body[:997] + "..."
    embed.add_field(name="Members", value=f"```{body}```", inline=False)
    if len(pages) > 1:
        embed.set_footer(text=f"Showing page 1/{len(pages)} (first {PAGE_SIZE})")
    await ctx.send(embed=embed)


# ──────────────────────────────────────────────
#  !help
# ──────────────────────────────────────────────
@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="📜 Purginator Bot Commands",
        color=discord.Color.blue(),
    )

    embed.add_field(
        name="!masskick @Role [filters]",
        value=(
            "Kick all members of a role who match the filters.\n"
            "**Filters (use one or both):**\n"
            "• `before:YYYY-MM-DD` / `after:YYYY-MM-DD` / `on:YYYY-MM-DD`\n"
            "• `inactive:DAYS` — no activity for X+ days\n"
            "Example: `!masskick @Visitors before:2025-08-08 inactive:30`"
        ),
        inline=False,
    )
    embed.add_field(
        name="!massban @Role [filters]",
        value=(
            "Ban all members of a role who match the filters.\n"
            "Same filters as `!masskick`.\n"
            "Example: `!massban @Raiders after:2025-07-01 inactive:14`"
        ),
        inline=False,
    )
    embed.add_field(
        name="!inactive @Role DAYS",
        value=(
            "List members of a role who have been inactive for X+ days.\n"
            "Example: `!inactive @Members 30`"
        ),
        inline=False,
    )
    embed.add_field(
        name="!activity @User",
        value="Check a specific member's last recorded activity and join date.",
        inline=False,
    )

    embed.set_footer(text="⚠️ All kick/ban actions require confirmation before executing.")
    await ctx.send(embed=embed)


# ──────────────────────────────────────────────
#  ERROR HANDLING
# ──────────────────────────────────────────────
@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("🔒 You don't have permission to use this command.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Member not found. Make sure you're mentioning a valid user.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.send(f"❌ An error occurred: `{error}`")
        raise error


# ──────────────────────────────────────────────
#  RUN
# ──────────────────────────────────────────────
keep_alive()
bot.run(os.environ["DISCORD_TOKEN"])
