# Minecraft Player Alert Bot

Telegram bot with a small control panel for tracking Minecraft players on chosen servers.

The bot lets each chat:

- add up to 3 servers by IP/domain;
- add up to 8 player nicknames;
- receive alerts when the player appears in the visible player list;
- receive alerts when the player disappears from the visible player list;
- list or delete servers and players;
- run a manual check.

## Important limitation

The bot connects directly to the Minecraft server status protocol. It does not rely on the cached `api.mcsrvstat.us` response.

Many servers report the total online count but do not expose the full nickname list. In that case the bot can only detect players who appear in the server status sample. For perfect tracking, you need server-side access: logs, RCON, Query with a full player list, or a plugin.

## Telegram setup

1. Create a bot in Telegram through `@BotFather`.
2. Copy the token.
3. Copy `.env.example` to `.env`.
4. Put the token into `.env`:

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456:abc...
   POLL_INTERVAL_SECONDS=60
   MINECRAFT_PROTOCOL_VERSION=774
   STATE_PATH=/app/data/state.json
   LOG_LEVEL=INFO
   ```

You do not need to hard-code a chat id. Open the bot in Telegram and send `/start`.

## Bot controls

Commands:

```text
/start     - open the control panel
/addserver - add a server
/addplayer - add a player nickname
/list      - show servers and players
/check     - check now
/cancel    - cancel current input
```

The inline control panel exposes the same actions through buttons.

The bot checks every tracked player on every tracked server. With 3 servers and 8 players, that is 24 checks per polling cycle.

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

The default polling interval is 60 seconds. The bot connects directly to the Minecraft server, so there is no 5-minute third-party cache.

The compose file runs the container as `root` by default through:

```yaml
user: "${PUID:-0}:${PGID:-0}"
```

This is intentional for simple VPS deployment with a bind-mounted `./data` folder. Without it, Docker may create `./data` as `root`, while the app user inside the container cannot write `state.tmp`.

If you want to run it as a specific Linux user later, create/chown the data directory and set `PUID`/`PGID` in `.env`.

## Updating on VPS

From the app folder:

```bash
cd ~/apps/player-alert
git pull
docker compose up -d --build
docker compose logs -f
```

If you previously hit `PermissionError: /app/data/state.tmp`, this update fixes it. If the old `data` directory still has awkward permissions, this is also safe:

```bash
cd ~/apps/player-alert
mkdir -p data
chmod 755 data
docker compose up -d --build
```

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
