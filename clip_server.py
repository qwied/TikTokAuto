"""Мини-HTTP сервер: веб-интерфейс + API для телефона.

Отдаёт:
  /                      — веб-приложение (site-файлы лежат рядом с программой;
                           те же файлы загружаются на GitHub Pages)
  /api/health            — проверка связи
  /api/status            — каналы, эфиры, хайп, реклама, TikTok (живая лента)
  /api/clips             — список готовых клипов с метаданными и превью
  /files/<имя>           — сам mp4 (поддержка Range: можно смотреть с телефона)
  /thumbs/<имя>          — превью-кадры

POST (управление прямо с телефона):
  /api/channels          {"url": "twitch.tv/ник"}      — добавить канал ОДИН раз
  /api/channels/remove   {"url": "ник или ссылка"}     — убрать канал
  /api/ad                {"state": "on"|"off", "channel": "ник"}
                         — РЕКЛАМА началась/кончилась: нарезка стоп/продолжение
  /api/tiktok/token      {"access_token": "...", "refresh_token": "...", "expires_in": 86400}
                         — подключить автопостинг TikTok с сайта

Все ответы с CORS-заголовками, поэтому веб-приложение с GitHub Pages
(https) тоже может обращаться к этому серверу (если он доступен по https —
например через cloudflared). Телефон в той же Wi-Fi сети может просто
открыть http://<ip-компьютера>:8765 — всё работает без GitHub.
"""

import http.server
import json
import logging
import os
import re
import threading
import urllib.parse

from utils import local_ip

log = logging.getLogger("server")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")

# Начала сообщений, которые означают «не получилось» (для ok: false в ответе)
FAIL_PREFIXES = ("Не ", "Нет ", "Такого ", "Сейчас ", "Канал ", "Нужен ",
                 "Автопостинг ", "Не удалось ", "Ad-guard ")

# Только эти файлы отдаются как статики сайта. Это позволяет держать файлы
# сайта прямо в КОРНЕ репозитория/программы (без папки docs/ — как требуется
# для GitHub) и гарантировать, что никто не вытянет config.yaml, links.txt
# или клипы через статический обработчик.
SITE_FILES = {
    "index.html", "style.css", "app.js", "sw.js", "manifest.webmanifest",
    "icon-192.png", "icon-512.png", "apple-touch-icon.png", "favicon.png",
}


def list_clips(clips_dir):
    """Готовые клипы: [{id, file, url, thumb, size, duration, channel, ...}]."""
    out = []
    try:
        names = os.listdir(clips_dir)
    except OSError:
        return out
    for name in sorted(names, reverse=True):
        if not name.lower().endswith(".mp4"):
            continue
        fp = os.path.join(clips_dir, name)
        if not os.path.isfile(fp):
            continue
        stem = os.path.splitext(name)[0]
        meta = {}
        try:
            with open(os.path.join(clips_dir, stem + ".json"), "r",
                      encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        try:
            stat = os.stat(fp)
        except OSError:
            continue
        out.append({
            "id": stem,
            "file": name,
            "url": "/files/" + urllib.parse.quote(name),
            "thumb": "/thumbs/" + urllib.parse.quote(stem) + ".jpg"
                     if os.path.exists(os.path.join(clips_dir, "thumbs", stem + ".jpg")) else "",
            "size": stat.st_size,
            "created": int(stat.st_mtime),
            "channel": meta.get("channel", ""),
            "display": meta.get("display", ""),
            "platform": meta.get("platform", ""),
            "title": meta.get("title", ""),
            "score": meta.get("score", 0),
            "duration": meta.get("duration", 0),
        })
    return out


class ClipServer:
    def __init__(self, clips_dir, port=8765, docs_dir=None, status_provider=None,
                 channels_api=None):
        self.clips_dir = os.path.abspath(str(clips_dir))
        self.docs_dir = os.path.abspath(str(docs_dir)) if docs_dir else ""
        self.port = int(port)
        self.status_provider = status_provider or (lambda: {"channels": []})
        self.channels_api = channels_api          # Pipeline: каналы/реклама/TikTok
        self.httpd = None

    def start(self):
        clips_dir = self.clips_dir
        docs_dir = self.docs_dir
        status_cb = self.status_provider
        channels_api = self.channels_api
        os.makedirs(clips_dir, exist_ok=True)

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # --- служебное ----------------------------------------------------

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")

            def _reply(self, body, ctype, status=200, extra=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _reply_json(self, obj, status=200):
                self._reply(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                            "application/json; charset=utf-8", status)

            def _send_file(self, fp, ctype, name=None, as_attachment=False):
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    self.send_error(404)
                    return
                start, end = 0, size - 1
                rng = self.headers.get("Range") or ""
                m = RANGE_RE.match(rng.strip())
                status = 200
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    else:
                        start = max(0, size - int(m.group(2)))
                        end = size - 1
                    start = max(0, min(start, size - 1))
                    end = max(start, min(end, size - 1))
                    status = 206
                length = end - start + 1
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                if status == 206:
                    self.send_header("Content-Range",
                                     f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if as_attachment and name:
                    self.send_header(
                        "Content-Disposition",
                        f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}")
                self._cors()
                self.end_headers()
                try:
                    with open(fp, "rb") as fh:
                        fh.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = fh.read(min(1024 * 512, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _static(self, path):
                """Файлы веб-приложения (строго белый список — без выхода наружу)."""
                rel = path.lstrip("/") or "index.html"
                if rel not in SITE_FILES:
                    self.send_error(404)
                    return
                fp = os.path.normpath(os.path.join(docs_dir, rel))
                if not fp.startswith(os.path.normpath(docs_dir) + os.sep) \
                        and fp != os.path.normpath(docs_dir):
                    self.send_error(403)
                    return
                if not (docs_dir and os.path.isfile(fp)):
                    self.send_error(404)
                    return
                ext = os.path.splitext(fp)[1].lower()
                self._send_file(fp, MIME.get(ext, "application/octet-stream"))

            # --- маршруты -------------------------------------------------------

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Content-Length", "0")
                self.end_headers()

            # --- POST: управление с телефона ---------------------------------

            def _read_json(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 512 * 1024:
                    return None
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return None

            def do_POST(self):
                p = urllib.parse.urlparse(self.path).path
                body = self._read_json()
                if body is None:
                    self._reply_json({"ok": False,
                                      "message": "Ожидался JSON"}, 400)
                    return
                if channels_api is None:
                    self._reply_json({"ok": False,
                                      "message": "Управление недоступно"}, 503)
                    return
                try:
                    if p == "/api/channels":
                        msg = str(channels_api.api_add_channel(
                            str(body.get("url") or "")))
                    elif p == "/api/channels/remove":
                        msg = str(channels_api.api_remove_channel(
                            str(body.get("url") or "")))
                    elif p == "/api/ad":
                        msg = str(channels_api.api_manual_ad(
                            str(body.get("state") or ""),
                            str(body.get("channel") or "") or None))
                    elif p == "/api/tiktok/token":
                        msg = str(channels_api.api_set_tiktok_token(body))
                    else:
                        self._reply_json({"ok": False,
                                          "message": "Нет такого маршрута"}, 404)
                        return
                except Exception as e:
                    log.exception("POST %s упал", p)
                    self._reply_json({"ok": False,
                                      "message": "Внутренняя ошибка: %s" % e}, 500)
                    return
                ok = not msg.startswith(FAIL_PREFIXES)
                self._reply_json({"ok": ok, "message": msg})

            def do_GET(self):
                p = urllib.parse.urlparse(self.path).path
                if p in ("/", ""):
                    if docs_dir and os.path.isfile(os.path.join(docs_dir, "index.html")):
                        self._static("/")
                    else:
                        # запасная страница без docs/
                        clips = list_clips(clips_dir)
                        rows = "".join(
                            f'<div class="c"><a href="{c["url"]}" download>{c["file"]}</a>'
                            f'<div class="s">{c["size"] / (1024 * 1024):.1f} MB</div></div>'
                            for c in clips)
                        body = (PAGE_FALLBACK.format(items=rows or "<p>Пока нет клипов</p>")
                                ).encode("utf-8")
                        self._reply(body, "text/html; charset=utf-8")
                elif p == "/api/health":
                    self._reply_json({"ok": True, "app": "StreamClipper"})
                elif p == "/api/status":
                    try:
                        self._reply_json(status_cb() or {})
                    except Exception:
                        self._reply_json({"channels": []})
                elif p == "/api/clips":
                    self._reply_json({"clips": list_clips(clips_dir)})
                elif p.startswith("/files/"):
                    name = os.path.basename(
                        urllib.parse.unquote(p[len("/files/"):]))
                    fp = os.path.join(clips_dir, name)
                    if not os.path.isfile(fp):
                        self.send_error(404)
                        return
                    self._send_file(fp, "video/mp4", name=name, as_attachment=False)
                elif p.startswith("/thumbs/"):
                    name = os.path.basename(
                        urllib.parse.unquote(p[len("/thumbs/"):]))
                    fp = os.path.join(clips_dir, "thumbs", name)
                    if not os.path.isfile(fp):
                        self.send_error(404)
                        return
                    self._send_file(fp, "image/jpeg")
                else:
                    self._static(p)

            def log_message(self, fmt, *args):
                log.debug("http: %s", fmt % args)

        http.server.ThreadingHTTPServer.allow_reuse_address = True
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        if self.port == 0:
            self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True,
                         name="clip-server").start()
        log.info("Веб-интерфейс: http://%s:%d/  (откройте на телефоне в той же Wi-Fi)",
                 local_ip(), self.port)

    def base_url(self):
        return f"http://{local_ip()}:{self.port}"

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass


PAGE_FALLBACK = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StreamClipper - клипы</title><style>
body{{background:#121417;color:#e8eaed;font-family:-apple-system,Arial,sans-serif;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 12px}}
.c{{border:1px solid #2a2e33;border-radius:12px;padding:14px;margin:10px 0;background:#1a1d21}}
a{{color:#4fc3f7;text-decoration:none;font-weight:600;word-break:break-all}}
.s{{color:#9aa0a6;font-size:12px;margin-top:4px}}
</style></head><body><h1>StreamClipper - готовые клипы</h1>{items}</body></html>"""
