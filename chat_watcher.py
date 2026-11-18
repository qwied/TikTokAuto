"""Чаты Twitch (IRC over WebSocket) и YouTube (live_chat API) в реальном времени."""

import json
import logging
import random
import re
import threading

import requests
import websocket

from models import ChatMessage

log = logging.getLogger("chat")

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ru,en;q=0.9",
}


class BaseChatWatcher(threading.Thread):
    """Базовый класс: поток-читатель чата с обратным вызовом on_message."""

    def __init__(self, on_message, name):
        super().__init__(daemon=True, name=name)
        self.on_message = on_message
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _emit(self, msg: ChatMessage):
        try:
            self.on_message(msg)
        except Exception:
            log.exception("Ошибка обработчика чата")


class TwitchChatWatcher(BaseChatWatcher):
    """Анонимное подключение к IRC-чату Twitch (без токена и логина)."""

    URL = "wss://irc-ws.chat.twitch.tv:443"

    def __init__(self, channel, on_message):
        super().__init__(on_message, f"twitch-chat-{channel}")
        self.channel = str(channel).lower().lstrip("#").rstrip("/")
        self.ws = None

    def stop(self):
        super().stop()
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass

    def run(self):
        if not self.channel:
            log.warning("Twitch: пустое имя канала — чат не подключается")
            return
        backoff = 5
        while not self._stop.is_set():
            try:
                self.ws = websocket.WebSocketApp(
                    self.URL,
                    on_open=self._on_open,
                    on_message=self._on_msg,
                    on_error=lambda ws, e: log.warning("Twitch чат: ошибка соединения: %s", e),
                )
                self.ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as e:
                log.warning("Twitch чат: %s", e)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30)
        log.info("Twitch чат остановлен (#%s)", self.channel)

    def _on_open(self, ws):
        ws.send("CAP REQ :twitch.tv/tags")
        ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
        ws.send(f"JOIN #{self.channel}")
        log.info("Twitch чат подключен: #%s", self.channel)

    def _on_msg(self, ws, raw):
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("PING"):
                ws.send(line.replace("PING", "PONG", 1))
                continue
            tags, text, user = self._parse(line)
            if text is not None:
                import time  # noqa: PLC0415
                self._emit(ChatMessage(ts=time.time(), user=user, text=text, platform="twitch"))

    @staticmethod
    def _parse(line):
        """Возвращает (tags, text, user); text=None, если это не сообщение чата."""
        tags = {}
        rest = line
        if line.startswith("@"):
            try:
                raw_tags, rest = line.split(" ", 1)
            except ValueError:
                return tags, None, ""
            for item in raw_tags[1:].split(";"):
                if "=" in item:
                    k, v = item.split("=", 1)
                    tags[k] = v
        m = re.match(r":(\S+?)!\S* PRIVMSG #?(\S+) :(.*)$", rest)
        if not m:
            return tags, None, ""
        user = tags.get("display-name") or m.group(1)
        return tags, m.group(3), user


class YouTubeChatWatcher(BaseChatWatcher):
    """Live-чат YouTube через внутренний API youtubei (без ключей и квот)."""

    API = ("https://www.youtube.com/youtubei/v1/live_chat/get_live_chat"
           "?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8&prettyPrint=false")
    CTX = {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20241126.01.00", "hl": "ru"}}}

    def __init__(self, video_id, video_url, on_message):
        super().__init__(on_message, f"yt-chat-{video_id}")
        self.video_id = video_id
        self.video_url = video_url
        # страница открывается «как браузер», а API-запросы ходят обычным клиентом:
        # часть эндпоинтов youtubei не доверяет браузерному UA без cookies (403)
        self.session = requests.Session()
        self.session.headers.update(UA)
        self.session_plain = requests.Session()
        self.continuation = None
        self.last_ts = 0.0
        self._first_batch = True
        self._api_use_browser_ua = True

    def run(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                if self.continuation is None:
                    self.continuation = self._find_continuation()
                    if self.continuation is None:
                        log.warning("YouTube: токен live-чата не найден (стрим ещё не готов?)")
                        if self._stop.wait(15):
                            break
                        continue
                timeout = self._poll()
                backoff = 5
                if self._stop.wait(timeout):
                    break
            except Exception as e:
                log.warning("YouTube чат: %s; переподключение через %d c", e, backoff)
                self.continuation = None
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 60)
        log.info("YouTube чат остановлен (%s)", self.video_id)

    # --- внутреннее -----------------------------------------------------------

    def _find_continuation(self):
        """Вытащить из страницы стрима токен live-чата."""
        r = self.session.get(f"https://www.youtube.com/watch?v={self.video_id}&hl=ru", timeout=20)
        r.raise_for_status()
        html = r.text
        idx = html.find("var ytInitialData")
        if idx < 0:
            return None
        start = html.find("{", idx)
        data, _ = json.JSONDecoder().raw_decode(html[start:])
        tokens = []
        self._collect_tokens(data, [], tokens)
        # сначала кандидаты, у которых в пути упоминается livechat
        tokens.sort(key=lambda t: 0 if "livechat" in t[1] or "conversation" in t[1] else 1)
        for token, _where in tokens:
            try:
                resp = self._api(token)
            except Exception:
                continue
            cont = (resp.get("continuationContents") or {}).get("liveChatContinuation")
            if cont is not None:
                log.info("YouTube чат подключен (%s)", self.video_id)
                return token
        return None

    def _collect_tokens(self, node, path, out):
        if isinstance(node, dict):
            for k, v in node.items():
                p2 = path + [str(k)]
                if isinstance(v, dict) and v.get("token"):
                    if k in ("continuationCommand", "reloadContinuationData",
                             "timedContinuationData", "invalidationContinuationData"):
                        out.append((v["token"], " ".join(p2[-4:]).lower()))
                self._collect_tokens(v, p2, out)
        elif isinstance(node, list):
            for v in node:
                self._collect_tokens(v, path, out)

    def _api(self, token):
        body = dict(self.CTX)
        body["continuation"] = token
        sess = self.session if self._api_use_browser_ua else self.session_plain
        r = sess.post(self.API, json=body, timeout=20)
        if r.status_code != 200 and self._api_use_browser_ua:
            # запасной вариант: тот же запрос без браузерного UA
            r2 = self.session_plain.post(self.API, json=body, timeout=20)
            if r2.status_code == 200:
                self._api_use_browser_ua = False
                log.info("YouTube чат: API отвечает без браузерного UA — переключаюсь")
                r = r2
        if r.status_code != 200:
            raise RuntimeError(f"get_live_chat вернул {r.status_code}")
        return r.json()

    def _poll(self):
        data = self._api(self.continuation)
        cont = (data.get("continuationContents") or {}).get("liveChatContinuation")
        if not cont:
            raise RuntimeError("в ответе нет liveChatContinuation")
        batch = []
        for act in cont.get("actions", []):
            item = (act.get("addChatItemAction") or {}).get("item") or {}
            r = (item.get("liveChatTextMessageRenderer")
                 or item.get("liveChatPaidMessageRenderer")
                 or item.get("liveChatMembershipItemRenderer"))
            if not r:
                continue
            text = "".join(run.get("text", "") for run in (r.get("message") or {}).get("runs", []))
            if not text:
                continue
            ts = int(r.get("timestampUsec", "0")) / 1e6
            user = (r.get("authorName") or {}).get("simpleText") or "?"
            batch.append((ts, user, text))
        if batch:
            if self._first_batch:
                # первый ответ содержит историю чата — не скармливаем её детектору
                self._first_batch = False
            else:
                for ts, user, text in batch:
                    if ts > self.last_ts:
                        self._emit(ChatMessage(ts=ts, user=user, text=text, platform="youtube"))
            self.last_ts = max(self.last_ts, max(b[0] for b in batch))
        # следующий токен и пауза
        timeout_s = 2.0
        for c in cont.get("continuations", []):
            for kind in ("timedContinuationData", "invalidationContinuationData",
                         "reloadContinuationData"):
                d = c.get(kind)
                if d and d.get("token"):
                    self.continuation = d["token"]
                    timeout_s = max(d.get("timeoutMillis", 2000) / 1000, 1.0)
        return timeout_s
