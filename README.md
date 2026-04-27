# Minecraft Player Alert Bot

Telegram bot with a small control panel for tracking Minecraft players on chosen servers.

The bot lets each chat:

- add a server by IP/domain;
- add a player nickname;
- receive alerts when the player appears in the visible player list;
- receive alerts when the player disappears from the visible player list;
- list or delete active trackers;
- run a manual check.

## Important limitation

The bot uses the public Minecraft status API at `api.mcsrvstat.us`, similar to `mctracker.xyz`.

Many servers report the total online count but do not expose the full nickname list. In that case the bot can only detect players who appear in `players.list`. For perfect tracking, you need server-side access: logs, RCON, Query with a full player list, or a plugin.

## Telegram setup

1. Create a bot in Telegram through `@BotFather`.
2. Copy the token.
3. Copy `.env.example` to `.env`.
4. Put the token into `.env`:

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456:abc...
   POLL_INTERVAL_SECONDS=300
   STATE_PATH=/app/data/state.json
   LOG_LEVEL=INFO
   ```

You do not need to hard-code a chat id. Open the bot in Telegram and send `/start`.

## Bot controls

Commands:

```text
/start  - open the control panel
/add    - add a server + player tracker
/list   - show trackers
/check  - check now
/cancel - cancel current input
```

The inline control panel exposes the same actions through buttons.

## VPS deployment with Docker

On the VPS:

```bash
git clone https://github.com/Ulianychev/player-alert.git
cd player-alert
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f
```

The default polling interval is 300 seconds because the public status API is cached for about 5 minutes.

## Local run without Docker

On Windows PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN="123456:abc..."
python .\telegram_player_bot.py
```

On Linux:

```bash
export TELEGRAM_BOT_TOKEN="123456:abc..."
python telegram_player_bot.py
```

## Files

- `telegram_player_bot.py` - Telegram control panel and monitoring loop.
- `player_tracker.py` - older config-file tracker kept for manual/legacy use.
- `.env` - local secrets, ignored by git.
- `data/state.json` - persisted bot state and chat trackers.
