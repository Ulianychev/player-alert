#!/usr/bin/env python3
import json
import logging
import os
import signal
import sys
import threading
import time
from hashlib import sha1
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_STATE_PATH = "data/bot-state.json"
DEFAULT_API_BASE = "https://api.mcsrvstat.us/3"
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
        chat.setdefault("monitors", [])
        chat.setdefault("dialog", {})
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

        if text.startswith("/start"):
            self.clear_dialog(chat_id)
            self.telegram.send_message(chat_id, start_text(), main_keyboard())
        elif text.startswith("/help"):
            self.telegram.send_message(chat_id, help_text(), main_keyboard())
        elif text.startswith("/add"):
            self.start_add_dialog(chat_id)
        elif text.startswith("/list"):
            self.send_monitor_list(chat_id)
        elif text.startswith("/check"):
            self.check_chat_now(chat_id)
        elif text.startswith("/cancel"):
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
        elif data == "add":
            self.start_add_dialog(chat_id)
        elif data == "list":
            self.send_monitor_list(chat_id)
        elif data == "check":
            self.check_chat_now(chat_id)
        elif data == "help":
            self.telegram.send_message(chat_id, help_text(), main_keyboard())
        elif data.startswith("delete:"):
            self.delete_monitor(chat_id, data.removeprefix("delete:"))
        else:
            self.telegram.send_message(chat_id, "Неизвестное действие.", main_keyboard())

    def start_add_dialog(self, chat_id: int | str) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            chat["dialog"] = {"step": "server"}
            self.store.save()
        self.telegram.send_message(chat_id, "Введите IP или домен сервера Minecraft, например:\nmc.justvanilla.net")

    def handle_dialog_text(self, chat_id: int | str, text: str) -> None:
        with self.store.lock:
            dialog = dict(self.store.chat(chat_id).get("dialog", {}))

        if dialog.get("step") == "server":
            server = normalize_server(text)
            if not server:
                self.telegram.send_message(chat_id, "Сервер пустой. Введите IP или домен сервера.")
                return
            with self.store.lock:
                chat = self.store.chat(chat_id)
                chat["dialog"] = {"step": "player", "server": server}
                self.store.save()
            self.telegram.send_message(chat_id, f"Сервер: {server}\nТеперь введите ник игрока.")
            return

        if dialog.get("step") == "player":
            player = normalize_player_input(text)
            if not player:
                self.telegram.send_message(chat_id, "Ник пустой. Введите ник игрока.")
                return

            with self.store.lock:
                chat = self.store.chat(chat_id)
                server = chat.get("dialog", {}).get("server")
                if not server:
                    chat["dialog"] = {}
                    self.store.save()
                    self.telegram.send_message(chat_id, "Не нашел выбранный сервер. Начните заново.", main_keyboard())
                    return

                item = {
                    "id": monitor_id(server, player),
                    "server": server,
                    "player": player,
                    "normalized_player": normalize_name(player),
                    "last_visible": None,
                    "created_at": utc_now(),
                    "last_checked_at": None,
                }
                monitors = chat.setdefault("monitors", [])
                existing = next((monitor for monitor in monitors if monitor["id"] == item["id"]), None)
                if existing:
                    existing.update(item)
                    message = f"Уже отслеживаю {player} на {server}. Настройка обновлена."
                else:
                    monitors.append(item)
                    message = f"Готово. Буду оповещать, когда {player} зайдет на {server} или выйдет."
                chat["dialog"] = {}
                self.store.save()

            self.telegram.send_message(chat_id, message, main_keyboard())
            self.check_chat_now(chat_id)
            return

        self.telegram.send_message(chat_id, "Откройте панель и выберите действие.", main_keyboard())

    def clear_dialog(self, chat_id: int | str) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            chat["dialog"] = {}
            self.store.save()

    def send_monitor_list(self, chat_id: int | str) -> None:
        with self.store.lock:
            monitors = list(self.store.chat(chat_id).get("monitors", []))

        if not monitors:
            self.telegram.send_message(chat_id, "Пока нет отслеживаний.", main_keyboard())
            return

        lines = ["Ваши отслеживания:"]
        buttons = []
        for index, item in enumerate(monitors, start=1):
            visible = item.get("last_visible")
            if visible is True:
                status = "онлайн в видимом списке"
            elif visible is False:
                status = "не виден"
            else:
                status = "еще не проверялся"
            lines.append(f"{index}. {item['player']} на {item['server']} - {status}")
            buttons.append([{"text": f"Удалить {index}", "callback_data": f"delete:{item['id']}"}])

        buttons.append([{"text": "Добавить", "callback_data": "add"}, {"text": "Проверить сейчас", "callback_data": "check"}])
        buttons.append([{"text": "Панель", "callback_data": "panel"}])
        self.telegram.send_message(chat_id, "\n".join(lines), {"inline_keyboard": buttons})

    def delete_monitor(self, chat_id: int | str, item_id: str) -> None:
        with self.store.lock:
            chat = self.store.chat(chat_id)
            before = len(chat.get("monitors", []))
            chat["monitors"] = [item for item in chat.get("monitors", []) if item.get("id") != item_id]
            deleted = len(chat["monitors"]) != before
            self.store.save()

        if deleted:
            self.telegram.send_message(chat_id, "Отслеживание удалено.", main_keyboard())
        else:
            self.telegram.send_message(chat_id, "Не нашел такое отслеживание.", main_keyboard())

    def check_chat_now(self, chat_id: int | str) -> None:
        with self.store.lock:
            monitors = list(self.store.chat(chat_id).get("monitors", []))

        if not monitors:
            self.telegram.send_message(chat_id, "Пока нет отслеживаний. Нажмите Добавить отслеживание.", main_keyboard())
            return

        summaries = self.check_monitors_for_chat(str(chat_id), monitors, notify_changes=False)
        self.telegram.send_message(chat_id, "\n".join(summaries), main_keyboard())

    def monitor_loop(self) -> None:
        while self.running:
            started = time.monotonic()
            try:
                self.check_all_monitors()
            except Exception:
                logging.exception("Monitor cycle failed")

            elapsed = time.monotonic() - started
            sleep_for = max(1, self.poll_interval_seconds - elapsed)
            for _ in range(int(sleep_for)):
                if not self.running:
                    break
                time.sleep(1)

    def check_all_monitors(self) -> None:
        with self.store.lock:
            chat_items = {
                chat_id: list(chat.get("monitors", []))
                for chat_id, chat in self.store.data.get("chats", {}).items()
                if chat.get("monitors")
            }

        for chat_id, monitors in chat_items.items():
            self.check_monitors_for_chat(chat_id, monitors, notify_changes=True)

    def check_monitors_for_chat(
        self,
        chat_id: str,
        monitors: list[dict[str, Any]],
        notify_changes: bool,
    ) -> list[str]:
        statuses = fetch_statuses(self.api_base, {item["server"] for item in monitors})
        summaries = []
        changed = False

        with self.store.lock:
            chat = self.store.chat(chat_id)
            stored_by_id = {item["id"]: item for item in chat.get("monitors", [])}

            for monitor in monitors:
                stored = stored_by_id.get(monitor["id"])
                if not stored:
                    continue

                status = statuses.get(monitor["server"])
                if isinstance(status, Exception):
                    summaries.append(f"{monitor['player']} на {monitor['server']}: ошибка проверки.")
                    logging.warning("Failed to check %s: %s", monitor["server"], status)
                    continue

                if not status or not status.get("online"):
                    visible = False
                    summary = f"{monitor['player']} на {monitor['server']}: сервер офлайн или недоступен."
                    online_count = 0
                    max_players = "?"
                    visible_names: list[str] = []
                else:
                    visible_names = extract_player_names(status)
                    visible_set = {normalize_name(name) for name in visible_names}
                    visible = monitor["normalized_player"] in visible_set
                    players = status.get("players", {})
                    online_count = players.get("online", 0)
                    max_players = players.get("max", "?")
                    marker = "онлайн" if visible else "не виден"
                    summary = (
                        f"{monitor['player']} на {monitor['server']}: {marker}. "
                        f"Игроков {online_count}/{max_players}, видимых ников {len(visible_names)}."
                    )

                previous = stored.get("last_visible")
                stored["last_visible"] = visible
                stored["last_checked_at"] = utc_now()
                stored["last_online_count"] = online_count
                stored["last_max_players"] = max_players
                changed = True
                summaries.append(summary)

                if notify_changes and previous is not None and previous != visible:
                    if visible:
                        self.telegram.send_message(
                            chat_id,
                            f"{monitor['player']} зашел на {monitor['server']}.\nИгроков: {online_count}/{max_players}",
                        )
                    else:
                        self.telegram.send_message(
                            chat_id,
                            (
                                f"{monitor['player']} вышел с {monitor['server']} "
                                f"или пропал из видимого списка.\nИгроков: {online_count}/{max_players}"
                            ),
                        )

            if changed:
                self.store.save()

        return summaries


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
            [{"text": "Добавить отслеживание", "callback_data": "add"}],
            [
                {"text": "Мои отслеживания", "callback_data": "list"},
                {"text": "Проверить сейчас", "callback_data": "check"},
            ],
            [{"text": "Помощь", "callback_data": "help"}],
        ]
    }


def start_text() -> str:
    return (
        "Панель Player Alert.\n\n"
        "Добавьте сервер и ник игрока. Бот будет писать сюда, когда игрок появится "
        "в видимом списке сервера или пропадет из него."
    )


def help_text() -> str:
    return (
        "Команды:\n"
        "/start - открыть панель\n"
        "/add - добавить отслеживание\n"
        "/list - список отслеживаний\n"
        "/check - проверить сейчас\n"
        "/cancel - отменить ввод\n\n"
        "Важно: многие Minecraft-серверы показывают не все ники через публичный status API. "
        "Если сервер скрывает игрока, бот не сможет узнать о нем без доступа к логам, RCON или плагину."
    )


def monitor_id(server: str, player: str) -> str:
    raw_id = f"{normalize_server(server)}:{normalize_name(player)}"
    return sha1(raw_id.encode("utf-8")).hexdigest()[:16]


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
