# ATLANTA ESPORTS LOBBY MANAGEMENT BOT

*Play Fair. Compete Hard. Rise Together.*

BGMI scrim and tournament lobby bot: lobby panels, team registration, private payment channels, admin payment verification and automatic role assignment.

## Project structure

```
atlanta-esports-bot/
  main.py            Whole bot: database, UI views, modals, slash commands
  requirements.txt   discord.py 2.x and python-dotenv
  .env.example       Environment variable template
  .python-version    Python 3.12 for Railway
  README.md          This guide
```

`DATA_DIR` holds the SQLite database (`DB_PATH`) and the shared payment QR (`DATA_DIR/payment_qr/`).

## How it works

1. An admin runs `/lobby admin` (or the slash commands) to create a lobby, pick its role and post the public panel.
2. A player presses **Register Team** and fills the modal. The captain Discord ID is taken from the person who clicked (Discord modals allow only five fields, and this also stops anyone registering as someone else). Players 2 to 4 are entered as `DiscordID, BGMIID`.
3. The bot validates the squad (four different people, all in the server, no duplicates, lobby open and not full) and creates a private payment channel visible only to the captain, admin role and bot.
4. The channel shows the shared QR, entry fee, team name and registration ID. The captain uploads a screenshot.
5. Review buttons appear for admins. A screenshot never assigns roles. Only **Approve Payment** (or `/lobby approve`) does, after the bot checks the role exists, is below the bot's role and has no privileged permissions.
6. Approval assigns the role to all four players, DMs them, records the admin and timestamp, and updates the panel. If any assignment fails, running `/lobby approve` again retries only those players.
7. Rejection releases the slot, notifies the captain and records the reason.

## Slash commands (administrators only)

`/lobby admin` (console), `create`, `edit`, `delete`, `post`, `list`, `open`, `close`, `role`, `createrole`, `setqr`, `registrations`, `approve`, `reject`, `stats`, `export`.

## Discord Developer Portal setup

1. Create an application, add a Bot, copy the token into `DISCORD_TOKEN`.
2. Under Bot > Privileged Gateway Intents enable **Message Content Intent** (needed to detect payment screenshots).
3. Invite with scopes `bot` and `applications.commands`, and permissions: View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Channels, Manage Roles, Manage Messages. Administrator also works.
4. In Server Settings > Roles drag the bot's role **above** every lobby role. Lobby roles must not have admin or moderation permissions.
5. Create a category for payment channels and copy its ID into `PAYMENT_CATEGORY_ID`. Copy your admin role ID into `ADMIN_ROLE_ID` (enable Developer Mode to copy IDs).

## Railway deployment

1. Push the project to a GitHub repository (do not commit `.env`).
2. In Railway: New Project > Deploy from GitHub repo.
3. **Variables** tab, add:
   - `DISCORD_TOKEN`
   - `ADMIN_ROLE_ID`
   - `PAYMENT_CATEGORY_ID`
   - `DATA_DIR=/data`
   - `DB_PATH=/data/atlanta_esports.db`
   - `GUILD_ID` (optional, instant command sync)
4. **Persistent volume**: open the service, press `Ctrl/Cmd+K` (or right-click the canvas) > Volume > attach a volume to the service with mount path `/data`. Without it the database and QR are wiped on every deploy.
5. **Settings > Deploy > Start Command**: `python main.py`. This is a worker, so no public domain or port is needed.
6. Deploy. The logs should show "Logged in as ..." and "Commands synced".
7. In Discord run `/lobby setqr` with your payment QR, then `/lobby create`, `/lobby post`.

Back up the database occasionally by running `/lobby export` (CSV) or downloading `/data/atlanta_esports.db` with the Railway CLI.

## Local run

```
pip install -r requirements.txt
cp .env.example .env   # fill in values, set DATA_DIR=./data
python main.py
```

## Notes

- Entry fees are stored per registration at the time of registering, so editing the fee later does not change existing records. Financial statistics only count payments an admin approved.
- Persistent buttons (panels, payment channel, review controls, admin console) are re-registered on every start.
- Deleting a lobby also deletes its registrations, payments and payment channels.
