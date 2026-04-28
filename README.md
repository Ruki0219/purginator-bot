# Discord Mass Kick / Mass Ban Bot

## Setup

1. **Install dependencies:**
   ```bash
   pip install discord.py
   ```

2. **Set your bot token as an environment variable:**
   ```bash
   export DISCORD_TOKEN="your-bot-token-here"
   ```
   On Render / hosting platforms, add `DISCORD_TOKEN` as an environment variable in the dashboard.

3. **Required Bot Permissions:**
   - Kick Members
   - Ban Members
   - Manage Messages (for clearing reactions)
   - Read Message History
   - Send Messages
   - Add Reactions

4. **Required Intents (enable in Discord Developer Portal → Bot):**
   - Server Members Intent ✅
   - Message Content Intent ✅
   - Presence Intent ✅

5. **Run:**
   ```bash
   python bot.py
   ```

---

## Commands

### `!masskick @Role [filters]`
Kick all non-bot members of a role who match **all** provided filters.

### `!massban @Role [filters]`
Ban all non-bot members of a role who match **all** provided filters.

**Available filters (use one or both):**

| Filter | Description | Example |
|---|---|---|
| `before:YYYY-MM-DD` | Joined before this date | `before:2025-06-01` |
| `after:YYYY-MM-DD` | Joined after this date | `after:2025-01-01` |
| `on:YYYY-MM-DD` | Joined on this exact date | `on:2025-03-15` |
| `inactive:DAYS` | No message/reaction/voice activity for X+ days | `inactive:30` |

**Filters stack with AND logic** — a member must match every filter to be included.

**Examples:**
```
!masskick @Visitors before:2025-08-08
!masskick @Trial inactive:30
!masskick @Newbies after:2025-01-01 inactive:14
!massban @Raiders on:2025-08-01
!massban @Suspicious before:2025-06-01 inactive:60
```

### `!inactive @Role DAYS`
Preview which members of a role have been inactive for X+ days (no kick/ban).

```
!inactive @Members 30
```

### `!activity @User`
Check a specific member's last recorded activity and join date.

```
!activity @SomeUser
```

### `!help`
Show all commands.

---

## How Activity Tracking Works

The bot records a timestamp every time a member:
- Sends a message
- Adds a reaction
- Joins a voice channel

This data is saved to `activity_data.json` every 5 minutes so it persists across restarts.

**Important:** The bot can only track activity from the moment it starts running. For members who have never been active since the bot was added, their **join date** is used as a fallback for "last seen."

---

## How Confirmation Works

Before any kick or ban executes, the bot shows a paginated preview embed listing every affected member with their join date and last-seen info. You navigate with ◀️▶️ and confirm with ✅ or cancel with ❌. The confirmation times out after 2 minutes.

---

## Notes

- Bots are always excluded from mass actions.
- Members the bot cannot kick/ban (higher role, server owner) are skipped and reported in the results.
- A 1-second delay between each kick/ban prevents Discord rate-limit issues.
- The `role:RoleName` syntax works as an alternative to `@Role` mentions.
