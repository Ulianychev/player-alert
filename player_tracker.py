#!/usr/bin/env python3
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


APP_NAME = "mc-player-tracker"
DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_STATE_PATH = "data/state.json"
DEFAULT_API_BASE = "https://api.mcsrvstat.us/3"
USER_AGENT = "mc-player-tracker/1.0 (+https://api.mcsrvstat.us/)"


@dataclass
class Config:
    server: str
    tracked_players: set[str]
    poll_interval_seconds: int
    notify_on_join: bool
    notify_on_leave: bool
    notify_on_server_offline: bool
    api_base: str


class Tracker:
    def __init__(self, config: Config, state_path: Path) -> None:
        self.config = config
        self.state_path = state_path
        self.state = self.load_state()
        self.running = True

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "last_seen_online": [],
                "last_seen_tracked_online": [],
                "server_was_online": None,
                "all_time_seen": [],
            }

        with self.state_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
        tmp_path.replace(self.state_path)

    def stop(self, _signum: int, _frame: Any) -> None:
        logging.info("Stopping after current cycle")
        self.running = False

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        logging.info("Tracking %s on %s", sorted(self.config.tracked_players), self.config.server)
        while self.running:
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                logging.exception("Polling cycle failed")

            elapsed = time.monotonic() - started
            sleep_for = max(1, self.config.poll_interval_seconds - elapsed)
            for _ in range(int(sleep_for)):
                if not self.running:
                    break
                time.sleep(1)

    def poll_once(self) -> None:
        status = fetch_server_status(self.config.api_base, self.config.server)
        now = utc_now()

        if not status.get("online"):
            logging.warning("Server %s is offline or unavailable", self.config.server)
            if self.config.notify_on_server_offline and self.state.get("server_was_online") is not False:
                notify(f"Minecraft server `{self.config.server}` is offline or unavailable.")
            self.state["server_was_online"] = False
            self.state["last_checked_at"] = now
            self.save_state()
            return

        player_names = extract_player_names(status)
        online_count = status.get("players", {}).get("online", 0)
        max_players = status.get("players", {}).get("max", "?")
        player_by_normalized_name = {normalize_name(name): name for name in player_names}
        visible_players = set(player_by_normalized_name)
        tracked_visible = visible_players & self.config.tracked_players

        previously_tracked = set(self.state.get("last_seen_tracked_online", []))
        joined = tracked_visible - previously_tracked
        left = previously_tracked - tracked_visible

        logging.info(
            "%s online: %s/%s, visible names: %s, tracked visible: %s",
            self.config.server,
            online_count,
            max_players,
            len(player_names),
            sorted(tracked_visible),
        )

        if joined and self.config.notify_on_join:
            notify(
                format_join_message(
                    self.config.server,
                    [player_by_normalized_name[name] for name in joined],
                    online_count,
                    max_players,
                    player_names,
                )
            )

        if left and self.config.notify_on_leave:
            notify(format_leave_message(self.config.server, left, online_count, max_players))

        all_time = set(self.state.get("all_time_seen", []))
        all_time.update(player_names)

        self.state.update(
            {
                "server_was_online": True,
                "last_checked_at": now,
                "last_seen_online": sorted(player_names, key=str.casefold),
                "last_seen_tracked_online": sorted(tracked_visible),
                "all_time_seen": sorted(all_time, key=str.casefold),
                "last_online_count": online_count,
                "last_max_players": max_players,
            }
        )
        self.save_state()


def fetch_server_status(api_base: str, server: str) -> dict[str, Any]:
    url = f"{api_base.rstrip('/')}/{quote(server, safe=':.-_')}"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"API returned HTTP {exc.code} for {server}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach status API for {server}: {exc.reason}") from exc


def extract_player_names(status: dict[str, Any]) -> list[str]:
    players = status.get("players", {})
    raw_list = players.get("list") or []
    names: list[str] = []
    for item in raw_list:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


def format_join_message(
    server: str,
    joined: list[str],
    online_count: int,
    max_players: Any,
    visible_players: list[str],
) -> str:
    names = ", ".join(sorted(joined, key=str.casefold))
    visible = ", ".join(sorted(visible_players, key=str.casefold)) or "no visible names"
    return (
        f"Tracked player online on `{server}`: {names}\n"
        f"Players: {online_count}/{max_players}\n"
        f"Visible list: {visible}"
    )


def format_leave_message(server: str, left: set[str], online_count: int, max_players: Any) -> str:
    names = ", ".join(sorted(left))
    return f"Tracked player no longer visible on `{server}`: {names}\nPlayers: {online_count}/{max_players}"


def notify(message: str) -> None:
    logging.info("Notification: %s", message.replace("\n", " | "))
    sent = False

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if telegram_token and telegram_chat_id:
        send_telegram(telegram_token, telegram_chat_id, message)
        sent = True

    discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if discord_webhook_url:
        send_discord(discord_webhook_url, message)
        sent = True

    if not sent:
        print(message, flush=True)


def send_telegram(token: str, chat_id: str, message: str) -> None:
    payload = urlencode({"chat_id": chat_id, "text": message})
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    request = Request(
        url,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        response.read()


def send_discord(webhook_url: str, message: str) -> None:
    payload = json.dumps({"content": message}).encode("utf-8")
    request = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        response.read()


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    server = str(raw.get("server", "")).strip()
    if not server:
        raise ValueError("config.json must contain a non-empty 'server'")

    tracked_players = {normalize_name(name) for name in raw.get("tracked_players", []) if str(name).strip()}
    if not tracked_players:
        raise ValueError("config.json must contain at least one tracked player")

    interval = int(raw.get("poll_interval_seconds", 300))
    if interval < 60:
        logging.warning("poll_interval_seconds is below 60; using 60 seconds")
        interval = 60

    return Config(
        server=server,
        tracked_players=tracked_players,
        poll_interval_seconds=interval,
        notify_on_join=bool(raw.get("notify_on_join", True)),
        notify_on_leave=bool(raw.get("notify_on_leave", False)),
        notify_on_server_offline=bool(raw.get("notify_on_server_offline", True)),
        api_base=str(raw.get("api_base", DEFAULT_API_BASE)),
    )


def normalize_name(name: str) -> str:
    return str(name).strip().casefold()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime


def main() -> int:
    setup_logging()
    config_path = Path(os.getenv("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    state_path = Path(os.getenv("STATE_PATH", DEFAULT_STATE_PATH))

    try:
        config = load_config(config_path)
    except Exception as exc:
        logging.error("Failed to load config: %s", exc)
        return 2

    tracker = Tracker(config, state_path)
    if os.getenv("RUN_ONCE", "").lower() in {"1", "true", "yes"}:
        tracker.poll_once()
        return 0

    tracker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
