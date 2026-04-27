# Minecraft Player Tracker

Small VPS-friendly tracker for `mc.justvanilla.net`. It polls the same public status API that `mctracker.xyz` uses and sends a notification when one of your tracked nicknames appears in the visible player list.

## Important limitation

`mc.justvanilla.net` currently reports the total online count, but not always the full list of nicknames. For example, the API can say `26/70` online while returning only 11 visible names. This tracker can only detect a tracked player when the server/API includes that nickname in `players.list`.

For perfect tracking, you would need cooperation from the server side: Query with full player list, RCON/log access, or a server plugin.

## Local setup

1. Copy the example config:

   ```bash
   cp config.example.json config.json
   ```

2. Edit `config.json` and replace `ExampleNick1`, `ExampleNick2` with the nicknames you want to track.

3. Run:

   ```bash
   python player_tracker.py
   ```

Without Telegram or Discord settings, alerts are printed to stdout.

## Telegram alerts

1. Create a bot via Telegram `@BotFather`.
2. Send any message to the bot.
3. Get your chat id, for example from `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`.
4. Copy `.env.example` to `.env` and fill:

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456:abc...
   TELEGRAM_CHAT_ID=123456789
   ```

## Discord alerts

Create a Discord webhook and set this in `.env`:

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

## VPS deployment with Docker

On the VPS:

```bash
git clone <your-repo-url> mc-player-tracker
cd mc-player-tracker
cp config.example.json config.json
cp .env.example .env
nano config.json
nano .env
docker compose up -d --build
docker compose logs -f
```

The default polling interval is 300 seconds because the public API is cached for about 5 minutes.

For a one-shot check:

```bash
RUN_ONCE=1 python player_tracker.py
```

## Files

- `player_tracker.py` - tracker process.
- `config.json` - server, interval, and tracked nicknames.
- `.env` - optional notification secrets.
- `data/state.json` - persisted last seen state.
