#!/usr/bin/env python3
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_STATE_PATH = "data/bot-state.json"
DEFAULT_API_BASE = "https://api.mcsrvstat.us/3"
MAX_SERVERS = 3
MAX_PLAYERS = 8
USER_AGENT = "mc-player-alert-bot/1.0"


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"chats": {}}
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("chats", {})
        for chat in data["chats"].values():
            migrate_chat_state(chat)
        return data

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False, sort_keys=True)
                fh.write("\n")
            tmp_path.replace(self.path)

    def chat(self, chat_id: int | str) -> dict[str, Any]:
        chats = self.data.setdefault("chats", {})
        chat = chats.setdefault(str(chat_id), {})
        migrate_chat_state(chat)
        return chat


class TelegramApi:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    def request(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
        data = None
        headers = {"User-Agent": USER_AGENT}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(f"{self.base_url}/{method}", data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Telegram API request failed: {exc.reason}") from exc

        result = json.loads(raw)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload, timeout=35).get("result", [])

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self.request("sendMessage", payload)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.request("answerCallbackQuery", payload)


class PlayerAlertBot:
    def __init__(self, telegram: TelegramApi, store: StateStore, poll_interval_seconds: int, api_base: str) -> None:
        self.telegram = telegram
        self.store = store
        self.poll_interval_seconds = poll_interval_seconds
        self.api_base = api_base
        self.running = True
        self.offset: int | None = None

    def stop(self, _signum: int, _frame: Any) -> None:
        logging.info("Stopping bot")
        self.running = False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        monitor_thread = threading.Thread(target=self.monitor_loop, name="monitor-loop", daemon=True)
        monitor_thread.start()

        logging.info("Telegram control panel is running")
        while self.running:
            try:
                updates = self.telegram.get_updates(self.offset)
                for update in updates:
                    self.offset = update["update_id"] + 1
                    self.handle_update(update)
            except Exception:
                logging.exception("Telegram polling failed")
                time.sleep(5)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self.handle_message(update["message"])
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])

    def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message.get("chat", {}).get("id")
        text = str(message.get("text") or "").strip()
        if not chat_id or not text:
            return

        command = text.split()[0].lower()
        if command == "/start":
            self.clear_dialog(chat_id)
            self.telegram.send_message(chat_id, start_text(), main_keyboard())
        elif command in {"/addserver", "/server"}:
            self.start_add_server_dialog(chat_id)
        elif command in {"/addplayer", "/player"}:
            self.start_add_player_dialog(chat_id)
        elif command in {"/list", "/status"}:
            self.send_status(chat_id)
        elif command == "/check":
            self.check_chat_now(chat_id)
        elif command == "/help":
            self.telegram.send_message(chat_id, help_text(), main_keyboard())
        elif command == "/cancel":
            self.clear_dialog(chat_id)
            self.telegram.send_message(chat_id, "Действие отменено.", main_keyboard())
        else:
            self.handle_dialog_text(chat_id, text)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        data = str(callback.get("data") or "")
        if not chat_id:
            self.telegram.answer_callback_query(callback_id)
            return

        self.telegram.answer_callback_query(callback_id)
        if data == "panel":
            self.clear_dialog(chat_id)
            self.telegram.send_message(chat_id, start_text(), main_keyboard())
        elif data == "add_server":
            self.start_add_server_dialog(chat_id)
        elif data == "add_player":
            self.start_add_player_dialog(chat_id)
        elif data == "status":
            self.send_status(chat_id)
        elif data == "check":
            self.check_chat_now(chat_id)
        elif data == "help":
            self.telegram.send_message(chat_id, help_text(), main_keyboard())
        elif data.startswith("del_server:"):
            self.delete_server(chat_id, int(data.removeprefix("del_server:")))
        elif data.startswith("del_player:"):
            self.delete_player(chat_id, int(data.removeprefix("del_player:")))
        else:
            self.telegram.send_message(chat_id, "Неизвестное действие.", main_keyboard())

    def start_add_server_dialog(self, chat_id: int | str) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            if len(chat["servers"]) >= MAX_SERVERS:
                self.telegram.send_message(chat_id, f"Лимит серверов: {MAX_SERVERS}. Удалите один сервер и попробуйте снова.", manage_keyboard(chat))
                return
            chat["dialog"] = {"step": "server"}
            self.store.save()
        self.telegram.send_message(chat_id, "Введите IP или домен сервера Minecraft, например:\nmc.justvanilla.net")

    def start_add_player_dialog(self, chat_id: int | str) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            if len(chat["players"]) >= MAX_PLAYERS:
                self.telegram.send_message(chat_id, f"Лимит игроков: {MAX_PLAYERS}. Удалите одного игрока и попробуйте снова.", manage_keyboard(chat))
                return
            chat["dialog"] = {"step": "player"}
            self.store.save()
        self.telegram.send_message(chat_id, "Введите ник игрока, например:\nSteve")

    def handle_dialog_text(self, chat_id: int | str, text: str) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            step = chat.get("dialog", {}).get("step")

        if step == "server":
            self.add_server(chat_id, text)
        elif step == "player":
            self.add_player(chat_id, text)
        else:
            self.telegram.send_message(chat_id, "Откройте панель и выберите действие.", main_keyboard())

    def add_server(self, chat_id: int | str, value: str) -> None:
        server = normalize_server(value)
        if not server:
            self.telegram.send_message(chat_id, "Сервер пустой. Введите IP или домен сервера.")
            return

        with self.store.lock:
            chat = self.store.chat(chat_id)
            existing = {normalize_server(item).casefold() for item in chat["servers"]}
            if server.casefold() not in existing:
                if len(chat["servers"]) >= MAX_SERVERS:
                    self.telegram.send_message(chat_id, f"Лимит серверов: {MAX_SERVERS}.", manage_keyboard(chat))
                    return
                chat["servers"].append(server)
                message = f"Сервер добавлен: {server}"
            else:
                message = f"Этот сервер уже есть в списке: {server}"
            chat["dialog"] = {}
            self.store.save()

        self.telegram.send_message(chat_id, message, main_keyboard())
        self.check_chat_now(chat_id)

    def add_player(self, chat_id: int | str, value: str) -> None:
        player = normalize_player_input(value)
        if not player:
            self.telegram.send_message(chat_id, "Ник пустой. Введите ник игрока.")
            return

        with self.store.lock:
            chat = self.store.chat(chat_id)
            existing = {normalize_name(item) for item in chat["players"]}
            if normalize_name(player) not in existing:
                if len(chat["players"]) >= MAX_PLAYERS:
                    self.telegram.send_message(chat_id, f"Лимит игроков: {MAX_PLAYERS}.", manage_keyboard(chat))
                    return
                chat["players"].append(player)
                message = f"Игрок добавлен: {player}"
            else:
                message = f"Этот игрок уже есть в списке: {player}"
            chat["dialog"] = {}
            self.store.save()

        self.telegram.send_message(chat_id, message, main_keyboard())
        self.check_chat_now(chat_id)

    def delete_server(self, chat_id: int | str, index: int) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            if index < 0 or index >= len(chat["servers"]):
                self.telegram.send_message(chat_id, "Не нашел такой сервер.", manage_keyboard(chat))
                return
            server = chat["servers"].pop(index)
            clear_statuses_for_server(chat, server)
            self.store.save()
        self.telegram.send_message(chat_id, f"Сервер удален: {server}", manage_keyboard(self.store.chat(chat_id)))

    def delete_player(self, chat_id: int | str, index: int) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            if index < 0 or index >= len(chat["players"]):
                self.telegram.send_message(chat_id, "Не нашел такого игрока.", manage_keyboard(chat))
                return
            player = chat["players"].pop(index)
            clear_statuses_for_player(chat, player)
            self.store.save()
        self.telegram.send_message(chat_id, f"Игрок удален: {player}", manage_keyboard(self.store.chat(chat_id)))

    def clear_dialog(self, chat_id: int | str) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            chat["dialog"] = {}
            self.store.save()

    def send_status(self, chat_id: int | str) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            text = format_config_status(chat)
            keyboard = manage_keyboard(chat)
        self.telegram.send_message(chat_id, text, keyboard)

    def check_chat_now(self, chat_id: int | str) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            servers = list(chat["servers"])
            players = list(chat["players"])

        if not servers:
            self.telegram.send_message(chat_id, "Сначала добавьте хотя бы один сервер.", main_keyboard())
            return
        if not players:
            self.telegram.send_message(chat_id, "Сначала добавьте хотя бы одного игрока.", main_keyboard())
            return

        summaries = self.check_chat(str(chat_id), servers, players, notify_changes=False)
        self.telegram.send_message(chat_id, "\n".join(summaries), main_keyboard())

    def monitor_loop(self) -> None:
        while self.running:
            started = time.monotonic()
            try:
                self.check_all_chats()
            except Exception:
                logging.exception("Monitor cycle failed")

            elapsed = time.monotonic() - started
            sleep_for = max(1, self.poll_interval_seconds - elapsed)
            for _ in range(int(sleep_for)):
                if not self.running:
                    break
                time.sleep(1)

    def check_all_chats(self) -> None:
        with self.store.lock:
            chat_items = {
                chat_id: (list(chat.get("servers", [])), list(chat.get("players", [])))
                for chat_id, chat in self.store.data.get("chats", {}).items()
                if chat.get("servers") and chat.get("players")
            }

        for chat_id, (servers, players) in chat_items.items():
            self.check_chat(chat_id, servers, players, notify_changes=True)

    def check_chat(self, chat_id: str, servers: list[str], players: list[str], notify_changes: bool) -> list[str]:
        statuses = fetch_statuses(self.api_base, set(servers))
        summaries: list[str] = []

        with self.store.lock:
            chat = self.store.chat(chat_id)
            state = chat.setdefault("states", {})
            changed = False

            for server in servers:
                status = statuses.get(server)
                if isinstance(status, Exception):
                    summaries.append(f"{server}: ошибка проверки.")
                    logging.warning("Failed to check %s: %s", server, status)
                    continue

                if not status or not status.get("online"):
                    online_count = 0
                    max_players = "?"
                    visible_names: list[str] = []
                    visible_set: set[str] = set()
                    server_summary = f"{server}: сервер офлайн или недоступен."
                else:
                    visible_names = extract_player_names(status)
                    visible_set = {normalize_name(name) for name in visible_names}
                    online_count = status.get("players", {}).get("online", 0)
                    max_players = status.get("players", {}).get("max", "?")
                    server_summary = f"{server}: игроков {online_count}/{max_players}, видимых ников {len(visible_names)}."

                summaries.append(server_summary)

                for player in players:
                    key = state_key(server, player)
                    visible = normalize_name(player) in visible_set
                    previous = state.get(key, {}).get("visible")
                    state[key] = {
                        "server": server,
                        "player": player,
                        "visible": visible,
                        "last_checked_at": utc_now(),
                        "last_online_count": online_count,
                        "last_max_players": max_players,
                    }
                    changed = True

                    marker = "онлайн" if visible else "не виден"
                    summaries.append(f"  {player}: {marker}")

                    if notify_changes and previous is not None and previous != visible:
                        if visible:
                            self.telegram.send_message(
                                chat_id,
                                f"{player} зашел на {server}.\nИгроков: {online_count}/{max_players}",
                            )
                        else:
                            self.telegram.send_message(
                                chat_id,
                                (
                                    f"{player} вышел с {server} или пропал из видимого списка.\n"
                                    f"Игроков: {online_count}/{max_players}"
                                ),
                            )

            if changed:
                self.store.save()

        return summaries


def migrate_chat_state(chat: dict[str, Any]) -> None:
    chat.setdefault("servers", [])
    chat.setdefault("players", [])
    chat.setdefault("states", {})
    chat.setdefault("dialog", {})

    for monitor in chat.get("monitors", []):
        server = normalize_server(str(monitor.get("server", "")))
        player = normalize_player_input(str(monitor.get("player", "")))
        if server and server.casefold() not in {item.casefold() for item in chat["servers"]}:
            if len(chat["servers"]) < MAX_SERVERS:
                chat["servers"].append(server)
        if player and normalize_name(player) not in {normalize_name(item) for item in chat["players"]}:
            if len(chat["players"]) < MAX_PLAYERS:
                chat["players"].append(player)

    chat["servers"] = chat["servers"][:MAX_SERVERS]
    chat["players"] = chat["players"][:MAX_PLAYERS]


def fetch_statuses(api_base: str, servers: set[str]) -> dict[str, dict[str, Any] | Exception]:
    statuses: dict[str, dict[str, Any] | Exception] = {}
    for server in sorted(servers):
        try:
            statuses[server] = fetch_server_status(api_base, server)
        except Exception as exc:
            statuses[server] = exc
    return statuses


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
    raw_list = status.get("players", {}).get("list") or []
    names = []
    for item in raw_list:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


def main_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": f"Добавить сервер для отслеживания (до {MAX_SERVERS})", "callback_data": "add_server"}],
            [{"text": f"Добавить игрока для отслеживания (до {MAX_PLAYERS})", "callback_data": "add_player"}],
            [
                {"text": "Списки", "callback_data": "status"},
                {"text": "Проверить сейчас", "callback_data": "check"},
            ],
            [{"text": "Помощь", "callback_data": "help"}],
        ]
    }


def manage_keyboard(chat: dict[str, Any]) -> dict[str, Any]:
    buttons = [
        [{"text": f"Добавить сервер ({len(chat['servers'])}/{MAX_SERVERS})", "callback_data": "add_server"}],
        [{"text": f"Добавить игрока ({len(chat['players'])}/{MAX_PLAYERS})", "callback_data": "add_player"}],
    ]
    for index, server in enumerate(chat["servers"]):
        buttons.append([{"text": f"Удалить сервер: {server}", "callback_data": f"del_server:{index}"}])
    for index, player in enumerate(chat["players"]):
        buttons.append([{"text": f"Удалить игрока: {player}", "callback_data": f"del_player:{index}"}])
    buttons.append([{"text": "Проверить сейчас", "callback_data": "check"}, {"text": "Панель", "callback_data": "panel"}])
    return {"inline_keyboard": buttons}


def start_text() -> str:
    return (
        "Панель Player Alert.\n\n"
        f"Можно добавить до {MAX_SERVERS} серверов и до {MAX_PLAYERS} игроков. "
        "Бот будет проверять каждого игрока на каждом сервере и писать, когда игрок появился "
        "или пропал из видимого списка."
    )


def help_text() -> str:
    return (
        "Команды:\n"
        "/start - открыть панель\n"
        "/addserver - добавить сервер\n"
        "/addplayer - добавить игрока\n"
        "/list - показать списки\n"
        "/check - проверить сейчас\n"
        "/cancel - отменить ввод\n\n"
        "Важно: многие Minecraft-серверы показывают не все ники через публичный status API. "
        "Если сервер скрывает игрока, бот не сможет узнать о нем без доступа к логам, RCON или плагину."
    )


def format_config_status(chat: dict[str, Any]) -> str:
    servers = chat["servers"]
    players = chat["players"]
    lines = [f"Серверы ({len(servers)}/{MAX_SERVERS}):"]
    lines.extend(f"{index}. {server}" for index, server in enumerate(servers, start=1))
    if not servers:
        lines.append("нет")

    lines.append("")
    lines.append(f"Игроки ({len(players)}/{MAX_PLAYERS}):")
    lines.extend(f"{index}. {player}" for index, player in enumerate(players, start=1))
    if not players:
        lines.append("нет")
    return "\n".join(lines)


def clear_statuses_for_server(chat: dict[str, Any], server: str) -> None:
    keys = [key for key, value in chat.get("states", {}).items() if value.get("server") == server]
    for key in keys:
        chat["states"].pop(key, None)


def clear_statuses_for_player(chat: dict[str, Any], player: str) -> None:
    normalized = normalize_name(player)
    keys = [key for key, value in chat.get("states", {}).items() if normalize_name(value.get("player", "")) == normalized]
    for key in keys:
        chat["states"].pop(key, None)


def state_key(server: str, player: str) -> str:
    raw_key = f"{normalize_server(server).casefold()}:{normalize_name(player)}"
    return sha1(raw_key.encode("utf-8")).hexdigest()[:16]


def normalize_server(server: str) -> str:
    return server.strip().removeprefix("https://").removeprefix("http://").strip("/")


def normalize_player_input(player: str) -> str:
    return player.strip().lstrip("@")


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

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logging.error("TELEGRAM_BOT_TOKEN is required")
        return 2

    poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    if poll_interval_seconds < 60:
        logging.warning("POLL_INTERVAL_SECONDS is below 60; using 60 seconds")
        poll_interval_seconds = 60

    bot = PlayerAlertBot(
        telegram=TelegramApi(token),
        store=StateStore(Path(os.getenv("STATE_PATH", DEFAULT_STATE_PATH))),
        poll_interval_seconds=poll_interval_seconds,
        api_base=os.getenv("API_BASE", DEFAULT_API_BASE),
    )
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
