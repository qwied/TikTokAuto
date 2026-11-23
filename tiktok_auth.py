# -*- coding: utf-8 -*-
"""Получение токена TikTok для режима api (один раз, потом авто-обновление).

Как пользоваться:
  1. developers.tiktok.com -> Manage apps -> Create app
     (Product: Content Posting API, scopes: video.upload + video.publish).
  2. В config.yaml -> tiktok: заполните client_key, client_secret,
     redirect: http://localhost:8321/callback  (пропишите её же в настройках
     приложения на developers.tiktok.com -> Redirect domains: localhost).
  3. Запустите: python tiktok_auth.py
     Откроется браузер -> войдите в TikTok -> разрешите доступ.
     Токен сохранится в tiktok_tokens.json рядом с программой.

БЕЗ аудита приложения TikTok публикует видео только в приватном режиме,
а с публичным аккаунтом отклоняет пост целиком: подайте приложение на аудит
(privacy.html и terms.html уже лежат на сайте) — после одобрения публикации
автоматически станут публичными (очередь tiktok_pending.json разгрузится сама).
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

API_BASE = "https://open.tiktokapis.com"
AUTH_BASE = "https://www.tiktok.com/v2/auth/authorize/"
SCOPE = "user.info.basic,video.upload,video.publish"
PORT = 8321

result = {}


def main():
    ap = argparse.ArgumentParser(description="Токен TikTok Content Posting API")
    ap.add_argument("--config", default="config.yaml", help="путь к config.yaml")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from utils import load_config
    cfg = load_config(args.config)
    tk = cfg.get("tiktok", {}) or {}
    base_dir = os.path.dirname(os.path.abspath(args.config))
    client_key = str(tk.get("client_key") or "")
    client_secret = str(tk.get("client_secret") or "")
    redirect = str(tk.get("redirect") or f"http://localhost:{PORT}/callback")
    token_file = os.path.join(base_dir, str(tk.get("token_file") or "tiktok_tokens.json"))
    if not client_key or not client_secret:
        print("Заполните tiktok.client_key и tiktok.client_secret в config.yaml "
              "(developers.tiktok.com).")
        return 2

    state = "sc" + str(int(time.time()))
    url = (AUTH_BASE + "?" + urllib.parse.urlencode({
        "client_key": client_key, "scope": SCOPE, "response_type": "code",
        "redirect_uri": redirect, "state": state,
    }))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (q.get("code") or [""])[0]
            result["state"] = (q.get("state") or [""])[0]
            result["error"] = (q.get("error_description") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = "<h2 style='font-family:sans-serif'>TikTok-токен получен — " \
                   "можно закрыть вкладку.</h2>"
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("Открываю браузер для входа в TikTok...")
    print("Если не открылся сам — откройте ссылку вручную:\n" + url)
    webbrowser.open(url)

    deadline = time.time() + 300
    while time.time() < deadline and "code" not in result:
        time.sleep(0.5)
    srv.shutdown()
    if "code" not in result:
        print("Не дождался ответа TikTok (5 минут). Попробуйте ещё раз.")
        return 2
    if result.get("error"):
        print("TikTok вернул ошибку:", result["error"])
        return 2
    if result.get("state") != state:
        print("state не совпал — повторите (подозрение на подмену запроса).")
        return 2

    r = requests.post(API_BASE + "/v2/oauth/token/", data={
        "client_key": client_key,
        "client_secret": client_secret,
        "code": result["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
    }, timeout=30)
    j = r.json()
    if "access_token" not in j:
        print("TikTok не выдал токен:", json.dumps(j, ensure_ascii=False)[:400])
        return 2
    j["expires_at"] = time.time() + int(j.get("expires_in", 86400))
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=2)
    print("Готово! Токен сохранён:", token_file)
    print("Теперь в config.yaml -> tiktok: mode: api и enabled: true.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
