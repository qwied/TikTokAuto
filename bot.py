"""Telegram-бот управления StreamClipper.

Пользователь кидает боту ссылку на стримера — программа начинает следить
и нарезать клипы. Плюс команды:

    /list    — список ссылок в слежке
    /status  — что происходит прямо сейчас
    /remove N — убрать канал из слежки
    /clip    — снять клип немедленно
    /help    — подсказка

Бот отвечает только владельцу: если в config.yaml заполнен chat_id, сообщения
из других чатов игнорируются; если не заполнен — запоминает первый чат, который
написал боту, и общается только с ним.
"""

import logging
import threading

import requests

from notifier import _is_placeholder

log = logging.getLogger("bot")

HELP = (
    "Я — StreamClipper. Кидайте мне ссылки на стримеров, я начинаю следить и "
    "нарезать клипы, когда чат взрывается:\n\n"
    "twitch.tv/ник\n"
    "https://www.youtube.com/@канал\n"
    "https://www.youtube.com/watch?v=VIDEO\n\n"
    "Команды:\n"
    "/list — список ссылок в слежке\n"
    "/status — что происходит сейчас\n"
    "/remove 2 — убрать канал №2 из слежки\n"
    "/clip — снять клип немедленно\n\n"
    "Готовые клипы будут приходить сюда же видео."
)


class TelegramBot(threading.Thread):
    """Long-polling getUpdates: команды и ссылки от владельца."""

    def __init__(self, notify_cfg, handlers):
        super().__init__(daemon=True, name="telegram-bot")
        tg = (notify_cfg or {}).get("telegram") or {}
        self.token = str(tg.get("bot_token") or "")
        self.allowed = str(tg.get("chat_id") or "")
        self.handlers = handlers or {}          # list/status/add/remove/clip -> callable
        self.session = requests.Session()
        self._stop = threading.Event()
        self._offset = 0
        self._learned_chat = ""

    def usable(self):
        return not _is_placeholder(self.token)

    def stop(self):
        self._stop.set()

    # --- основной цикл ---------------------------------------------------------------

    def run(self):
        if not self.usable():
            log.warning("Telegram-бот не запущен: не заполнен bot_token")
            return
        api = f"https://api.telegram.org/bot{self.token}"
        log.info("Telegram-бот запущен: кидайте ему ссылки, /list /status /clip")
        backoff = 5
        while not self._stop.is_set():
            try:
                r = self.session.get(f"{api}/getUpdates", params={
                    "timeout": 25, "offset": self._offset,
                    "allowed_updates": '["message"]'}, timeout=40)
                r.raise_for_status()
                data = r.json()
                backoff = 5
            except Exception as e:
                log.warning("Telegram-бот: связь потеряна (%s), повтор через %d c", e, backoff)
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 60)
                continue
            for upd in data.get("result") or []:
                self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                try:
                    self._handle(upd)
                except Exception:
                    log.exception("Не удалось обработать сообщение")
            if not data.get("result"):
                self._stop.wait(1.0)
        log.info("Telegram-бот остановлен")

    # --- обработка сообщений ------------------------------------------------------------

    def _handle(self, upd):
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        text = str(msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        if not self._check_access(chat_id, chat.get("username") or chat.get("first_name") or ""):
            return
        reply = self._route(text)
        if reply:
            self.send(chat_id, reply)

    def _check_access(self, chat_id, who):
        if self.allowed and not _is_placeholder(self.allowed):
            if chat_id == self.allowed:
                return True
            log.warning("Бот: сообщение из чужого чата %s — игнорирую", chat_id)
            return False
        # chat_id в конфиге не заполнен: запоминаем первый чат и работаем только с ним
        if self._learned_chat and chat_id != self._learned_chat:
            log.warning("Бот: сообщение из чужого чата %s — игнорирую", chat_id)
            return False
        if not self._learned_chat:
            self._learned_chat = chat_id
            log.warning(
                "chat_id в config.yaml не заполнен — запомнил чат %s (%s). "
                "Впишите этот chat_id в config.yaml, чтобы не потерять доступ после перезапуска",
                chat_id, who)
        return True

    def _route(self, text):
        low = text.strip().lower()
        if low.startswith("/"):
            cmd, _, arg = low.partition(" ")
            arg = arg.strip()
            if cmd in ("/start", "/help"):
                return HELP
            if cmd == "/list":
                return self._call("list")
            if cmd == "/status":
                return self._call("status")
            if cmd == "/clip":
                return self._call("clip")
            if cmd == "/add":
                if not arg:
                    return "Пришлите ссылку после команды: /add twitch.tv/ник"
                return self._call("add", arg, "telegram")
            if cmd == "/remove":
                if not arg:
                    return "Укажите номер канала: /remove 2 (список: /list)"
                return self._call("remove", arg)
            return "Не знаю такую команду. Пришлите ссылку на стримера или /help"
        # обычное сообщение: пробуем понять как ссылку
        return self._call("add", text, "telegram")

    def _call(self, name, *args):
        fn = self.handlers.get(name)
        if fn is None:
            return ""
        try:
            return fn(*args) or ""
        except Exception:
            log.exception("Обработчик %s упал", name)
            return "Ошибка, посмотрите лог программы на компьютере"

    # --- ответы ---------------------------------------------------------------------------

    def send(self, chat_id, text):
        """sendMessage с разбиением длинных текстов."""
        api = f"https://api.telegram.org/bot{self.token}"
        chunk = 3800
        parts = [text[i:i + chunk] for i in range(0, len(text), chunk)] or [""]
        for part in parts:
            try:
                r = self.session.post(f"{api}/sendMessage",
                                      data={"chat_id": chat_id, "text": part}, timeout=30)
                if not r.ok:
                    log.warning("Бот: sendMessage не удался: %s", r.text[:200])
            except Exception as e:
                log.warning("Бот: не удалось отправить ответ: %s", e)
