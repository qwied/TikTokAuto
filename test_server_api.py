"""Тесты веб-сервера клипов: API, CORS, Range, раздача docs/.

Запуск: python tests/test_server_api.py  (сеть не нужна — локальный порт)
"""

import json
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clip_server import ClipServer  # noqa: E402


def get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, dict(r.headers), r.read()


def main():
    tmp = tempfile.mkdtemp(prefix="sc_test_")
    clips = os.path.join(tmp, "clips")
    docs = os.path.join(tmp, "docs")
    os.makedirs(os.path.join(clips, "thumbs"))
    os.makedirs(os.path.join(docs, "js"))

    # фейковый клип + метаданные + превью
    mp4 = os.path.join(clips, "clip1.mp4")
    with open(mp4, "wb") as f:
        f.write(b"\x00" * 4096)
    with open(os.path.join(clips, "clip1.json"), "w", encoding="utf-8") as f:
        json.dump({"file": "clip1.mp4", "channel": "test", "platform": "twitch",
                   "score": 87.5, "duration": 60.0,
                   "display": "twitch.tv/test", "title": "стрим",
                   "created": 1700000000}, f, ensure_ascii=False)
    with open(os.path.join(clips, "thumbs", "clip1.jpg"), "wb") as f:
        f.write(b"jpgdata")
    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
        f.write("<!doctype html><html lang=ru><title>SC</title>ok</html>")
    with open(os.path.join(docs, "js", "app.js"), "w", encoding="utf-8") as f:
        f.write("console.log(1);")

    srv = ClipServer(clips, port=0, docs_dir=docs,
                     status_provider=lambda: {"channels": [{"name": "test", "live": True}]})
    srv.start()
    port = srv.port
    assert port > 0, "порт не назначен"

    # 1) /api/health
    st, hdrs, body = get(port, "/api/health")
    assert st == 200 and json.loads(body)["ok"] is True
    assert hdrs.get("Access-Control-Allow-Origin") == "*", hdrs

    # 2) /api/clips — клип с метаданными и превью
    st, hdrs, body = get(port, "/api/clips")
    clips_data = json.loads(body)["clips"]
    assert st == 200 and len(clips_data) == 1, clips_data
    c = clips_data[0]
    assert c["id"] == "clip1" and c["channel"] == "test"
    assert c["score"] == 87.5 and c["duration"] == 60.0
    assert c["thumb"] == "/thumbs/clip1.jpg" and c["size"] == 4096
    assert c["url"] == "/files/clip1.mp4"

    # 3) /api/status от провайдера
    st, _h, body = get(port, "/api/status")
    assert json.loads(body)["channels"][0]["live"] is True

    # 4) файл: целиком
    st, hdrs, body = get(port, "/files/clip1.mp4")
    assert st == 200 and len(body) == 4096
    assert hdrs.get("Accept-Ranges") == "bytes"

    # 5) файл: Range-запрос (для просмотра видео с телефона)
    st, hdrs, body = get(port, "/files/clip1.mp4", {"Range": "bytes=0-99"})
    assert st == 206 and len(body) == 100
    assert hdrs.get("Content-Range") == "bytes 0-99/4096"

    # 6) превью
    st, _h, body = get(port, "/thumbs/clip1.jpg")
    assert st == 200 and body == b"jpgdata"

    # 7) веб-приложение из docs/
    st, hdrs, body = get(port, "/")
    assert st == 200 and b"SC" in body
    st, _h, body = get(port, "/js/app.js")
    assert st == 200 and b"console.log" in body

    # 8) защита от выхода за пределы папки
    try:
        get(port, "/../config.yaml")
        raise AssertionError("ожидались 403/404 для ../")
    except urllib.error.HTTPError as e:
        assert e.code in (403, 404), e.code

    # 9) OPTIONS preflight (для запросов с GitHub Pages)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/clips",
                                 method="OPTIONS")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 204
        assert r.headers.get("Access-Control-Allow-Origin") == "*"

    srv.stop()
    print("SERVER TESTS OK — 9/9")


if __name__ == "__main__":
    import urllib.error  # noqa: F401
    main()
