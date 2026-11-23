"""Уведомления о готовых клипах НА ЛЮБОЕ УСТРОЙСТВО.

Поддерживаются: Telegram (видео прямо в чат), ntfy (Android/iOS/десктоп),
Bark (iPhone), Pushover, универсальный webhook.

provider: "telegram" | "ntfy" | "bark" | "pushover" | "webhook" | "all"
  "all" — разослать клип ВО ВСЕ настроенные каналы одновременно
  (настраиваются заполнением полей соответствующего раздела).
"""

import logging
import os
import urllib.parse

import requests

from utils import human_size

log = logging.getLogger("notify")

PLACEHOLDER_MARKS = ("ВСТАВЬТЕ", "ВАШ_", "уникальное")


def _is_placeholder(value):
    v = str(value or "")
    return (not v) or any(mark in v for mark in PLACEHOLDER_MARKS)


def _http_header(value):
    """Значение HTTP-заголовка, безопасное для requests/HTTP.

    HTTP-заголовки передаются в latin-1: кириллица («Клип готов») падала с
    UnicodeEncodeError и ВСЕ push-уведомления ntfy молча терялись
    (прод-инцидент 2026-11-20). Не-latin-1 кодируем как RFC 2047 encoded-word
    (=?UTF-8?B?...?=) — ntfy официально умеет такие заголовки читать.
    """
    s = str(value or "")
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        from base64 import b64encode
        enc = b64encode(s.encode("utf-8")).decode("ascii")
        return "=?UTF-8?B?" + enc + "?="


class Notifier:
    """Отправка уведомления о готовом клипе на любое устройство."""

    def __init__(self, cfg, server_base_url=""):
        self.cfg = cfg or {}
        self.base_url = (server_base_url or "").rstrip("/")

    def configured_providers(self):
        """Список каналов уведомлений, которые заполнены в config.yaml."""
        c = self.cfg
        out = []
        tg = c.get("telegram") or {}
        if not _is_placeholder(tg.get("bot_token")) and not _is_placeholder(tg.get("chat_id")):
            out.append("telegram")
        nt = c.get("ntfy") or {}
        if not _is_placeholder(nt.get("topic")):
            out.append("ntfy")
        bk = c.get("bark") or {}
        if not _is_placeholder(bk.get("device_key")) and "ключ" not in str(bk.get("device_key", "")).lower():
            out.append("bark")
        po = c.get("pushover") or {}
        if not _is_placeholder(po.get("user_key")) and not _is_placeholder(po.get("api_token")):
            out.append("pushover")
        wh = c.get("webhook") or {}
        if not _is_placeholder(wh.get("url")):
            out.append("webhook")
        return out

    def notify_text(self, text, title="StreamClipper"):
        """Текстовая тревога (без видео): Telegram и ntfy, если настроены.

        Используется для событий, где файл прикладывать не к чему:
        TikTok просит приватный аккаунт, цепочка оборвана и т.п.
        """
        providers = self.configured_providers()
        ok_any = False
        if "telegram" in providers:
            tg = self.cfg.get("telegram") or {}
            try:
                api = f"https://api.telegram.org/bot{tg.get('bot_token')}"
                r = requests.post(f"{api}/sendMessage",
                                  data={"chat_id": tg.get("chat_id"),
                                        "text": f"{title}\n\n{text}"[:4000]},
                                  timeout=60)
                ok_any = ok_any or r.ok
            except Exception:
                log.exception("Telegram: тревога не ушла")
        if "ntfy" in providers:
            nt = self.cfg.get("ntfy") or {}
            try:
                server = str(nt.get("server") or "https://ntfy.sh").rstrip("/")
                r = requests.put(f"{server}/{nt.get('topic')}",
                                 data=str(text)[:4000].encode("utf-8"),
                                 headers={"Title": _http_header(title),
                                          "Priority": "high", "Tags": "warning"},
                                 timeout=60)
                ok_any = ok_any or r.ok
                if not r.ok:
                    log.warning("ntfy: %s", r.text[:200])
            except Exception:
                log.exception("ntfy: тревога не ушла")
        return ok_any

    def notify_clip(self, path, caption, **_extra):
        """Уведомление о готовом клипе.

        _extra (score, title, platform, display, subs) — для режима GitHub:
        там эти данные попадают в clips.json (карточка на сайте). Здесь
        они не нужны, но принимаются, чтобы вызывающий код был единый.
        """
        provider = str(self.cfg.get("provider") or "telegram").lower()
        if provider in ("all", "every", "все", "любое"):
            targets = self.configured_providers()
            if not targets:
                log.error("provider: all — но ни один канал уведомлений не настроен "
                          "(заполните telegram / ntfy / bark / pushover / webhook в config.yaml)")
                return False
            log.info("Рассылаю клип на все устройства: %s", ", ".join(targets))
        else:
            targets = [provider]
        ok_any = False
        for t in targets:
            fn = getattr(self, f"_{t}", None)
            if fn is None:
                log.error("Неизвестный канал уведомлений: %s (доступны telegram, ntfy, bark, pushover, webhook)", t)
                continue
            try:
                ok = fn(path, caption)
                if ok:
                    log.info("Уведомление отправлено через %s (%s)", t, human_size(os.path.getsize(path)))
                ok_any = ok_any or ok
            except Exception:
                log.exception("Не удалось отправить уведомление через %s", t)
        return ok_any

    def file_url(self, path):
        """Публичная/локальная ссылка на файл, если настроен сервер клипов."""
        if not self.base_url:
            return ""
        return f"{self.base_url}/files/{urllib.parse.quote(os.path.basename(str(path)))}"

    # --- Telegram ---------------------------------------------------------------

    def _telegram(self, path, caption):
        c = self.cfg.get("telegram") or {}
        token, chat = str(c.get("bot_token") or ""), str(c.get("chat_id") or "")
        if _is_placeholder(token) or _is_placeholder(chat):
            log.error("Telegram не настроен: заполните bot_token и chat_id в config.yaml (см. README)")
            return False
        api = f"https://api.telegram.org/bot{token}"
        size = os.path.getsize(path)
        if size <= 49 * 1024 * 1024:
            with open(path, "rb") as fh:
                r = requests.post(
                    f"{api}/sendVideo",
                    data={"chat_id": chat, "caption": caption[:1000],
                          "supports_streaming": "true"},
                    files={"video": (os.path.basename(path), fh)},
                    timeout=300)
            if r.ok:
                return True
            log.warning("Telegram sendVideo не удался: %s", r.text[:300])
        # запасной вариант: сообщение со ссылкой на сервер клипов
        url = self.file_url(path)
        text = caption[:3800]
        if url:
            text += f"\n\nСкачать: {url}"
        else:
            text += "\n\nФайл слишком большой для отправки — скачайте из папки clips на компьютере."
        r = requests.post(f"{api}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=60)
        return r.ok

    # --- ntfy ---------------------------------------------------------------------

    def _ntfy(self, path, caption):
        c = self.cfg.get("ntfy") or {}
        server = str(c.get("server") or "https://ntfy.sh").rstrip("/")
        topic = str(c.get("topic") or "")
        if _is_placeholder(topic):
            log.error("ntfy не настроен: укажите уникальный topic в config.yaml")
            return False
        size = os.path.getsize(path)
        headers = {"Title": _http_header("Клип готов"), "Priority": "high",
                   "Filename": _http_header(os.path.basename(path))}
        if size <= 14 * 1024 * 1024:
            with open(path, "rb") as fh:
                r = requests.put(f"{server}/{topic}", data=fh, headers=headers, timeout=600)
        else:
            url = self.file_url(path)
            if url:
                headers["Click"] = url
            r = requests.put(f"{server}/{topic}",
                             data=(caption + (f"\n\nСкачать: {url}" if url else "")).encode("utf-8"),
                             headers=headers, timeout=60)
        if not r.ok:
            log.warning("ntfy: %s", r.text[:200])
        return r.ok

    # --- Bark -----------------------------------------------------------------------

    def _bark(self, path, caption):
        c = self.cfg.get("bark") or {}
        key = str(c.get("device_key") or "")
        if _is_placeholder(key) or "ключ" in key.lower():
            log.error("Bark не настроен: укажите device_key в config.yaml")
            return False
        payload = {"device_key": key, "title": "Клип готов",
                   "body": caption[:900], "group": "StreamClipper"}
        url = self.file_url(path)
        if url:
            payload["url"] = url
        r = requests.post("https://api.day.app/push", json=payload, timeout=30)
        if not r.ok:
            log.warning("Bark: %s", r.text[:200])
        return r.ok

    # --- Pushover ----------------------------------------------------------------------

    def _pushover(self, path, caption):
        c = self.cfg.get("pushover") or {}
        user, token = str(c.get("user_key") or ""), str(c.get("api_token") or "")
        if _is_placeholder(user) or _is_placeholder(token):
            log.error("Pushover не настроен: укажите user_key и api_token в config.yaml")
            return False
        data = {"user": user, "token": token, "title": "Клип готов", "message": caption[:900]}
        url = self.file_url(path)
        if url:
            data["url"] = url
            data["url_title"] = "Скачать клип"
        files = None
        fh = None
        if os.path.getsize(path) <= 9 * 1024 * 1024:
            fh = open(path, "rb")
            files = {"attachment": (os.path.basename(path), fh)}
        try:
            r = requests.post("https://api.pushover.net/1/messages.json",
                              data=data, files=files, timeout=300)
        finally:
            if fh:
                fh.close()
        if not r.ok:
            log.warning("Pushover: %s", r.text[:200])
        return r.ok

    # --- Webhook (универсальный: Home Assistant, свой сервер, Discord и т.д.) ---------

    def _webhook(self, path, caption):
        c = self.cfg.get("webhook") or {}
        url = str(c.get("url") or "").strip()
        if _is_placeholder(url):
            log.error("Webhook не настроен: укажите url в config.yaml (notify.webhook)")
            return False
        payload = {
            "title": "Клип готов",
            "text": caption[:3800],
            "file": os.path.basename(str(path)),
            "size": os.path.getsize(path),
        }
        link = self.file_url(path)
        if link:
            payload["url"] = link            # открыть клип в браузере
        headers = {"Content-Type": "application/json"}
        secret = str(c.get("secret") or "")
        if secret and not _is_placeholder(secret):
            headers["X-StreamClipper-Secret"] = secret
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        if not r.ok:
            log.warning("Webhook: %s", r.text[:200])
        return r.ok
