"""Тесты управления с сайта: добавление каналов, реклама, TikTok-токен.

Проверяются и POST-маршруты ClipServer, и Pipeline как backend для них.
Запуск: python -m pytest tests/test_channels_api.py -q
"""

import json
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clip_server import ClipServer  # noqa: E402
from notifier import Notifier  # noqa: E402
from pipeline import Pipeline  # noqa: E402


def post(port, path, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read())


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return r.status, json.loads(r.read())


class FakePipeline:
    """Записывает вызовы и возвращает «успех» или «не получилось»."""

    def __init__(self):
        self.calls = []

    def api_add_channel(self, url):
        self.calls.append(("add", url))
        return "Добавил: twitch.tv/abc — слежу."

    def api_remove_channel(self, url):
        self.calls.append(("remove", url))
        return "Убрал: abc — больше не слежу."

    def api_manual_ad(self, state, channel=None):
        self.calls.append(("ad", state, channel))
        return "РЕКЛАМА (abc): нарезка на паузе"

    def api_set_tiktok_token(self, data):
        self.calls.append(("tk", data))
        return "TikTok-токен сохранён — автопостинг включён."


def test_post_routes():
    with tempfile.TemporaryDirectory() as tmp:
        clips = os.path.join(tmp, "clips")
        os.makedirs(clips)
        fake = FakePipeline()
        srv = ClipServer(clips, port=0, docs_dir=os.path.join(tmp, "docs"),
                         status_provider=lambda: {"channels": []},
                         channels_api=fake)
        srv.start()
        port = srv.port

        st, j = post(port, "/api/channels", {"url": "twitch.tv/abc"})
        assert st == 200 and j["ok"] is True and "Добавил" in j["message"], j
        assert fake.calls[-1] == ("add", "twitch.tv/abc")

        st, j = post(port, "/api/channels/remove", {"url": "abc"})
        assert j["ok"] is True, j

        st, j = post(port, "/api/ad", {"state": "on", "channel": "abc"})
        assert j["ok"] is True, j
        assert fake.calls[-1] == ("ad", "on", "abc")

        st, j = post(port, "/api/tiktok/token",
                     {"access_token": "act." + "x" * 40})
        assert j["ok"] is True and "сохранён" in j["message"], j

        # ошибка: понятное сообщение с ok=false
        fake2 = FakePipeline()
        fake2.api_add_channel = lambda url: "Не похоже на ссылку на стримера."
        srv2 = ClipServer(clips, port=0, docs_dir=os.path.join(tmp, "docs"),
                          status_provider=lambda: {}, channels_api=fake2)
        srv2.start()
        st, j = post(srv2.port, "/api/channels", {"url": "мусор"})
        assert j["ok"] is False and "Не похоже" in j["message"], j

        # JSON-мусор -> 400
        data = b"not-json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/channels", data=data, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("ожидались 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code

        srv.stop()
        srv2.stop()


def test_pipeline_api_add_remove_persists():
    with tempfile.TemporaryDirectory() as base:
        cfg = {
            "_config_path": os.path.join(base, "config.yaml"),
            "links_file": "links.txt",
            "clip": {"pre_roll": 25, "post_roll": 20},
            "hype": {"threshold": 62},
            "recorder": {"segment_time": 4, "buffer_keep": 300},
            "storage": {"clips_dir": "clips", "temp_dir": "temp"},
            "notify": {"provider": "none"},
            "subtitle": {"enabled": False},
        }
        p = Pipeline(cfg, Notifier({"provider": "none"}, server_base_url=""))

        # добавили С САЙТА один раз — ссылка живёт в links.txt
        msg = p.api_add_channel("twitch.tv/tester")
        assert "Добавил" in msg, msg
        with open(os.path.join(base, "links.txt"), encoding="utf-8") as f:
            assert "twitch.tv/tester" in f.read()

        # удалили по имени (как с сайта) — из links.txt тоже ушла
        msg = p.api_remove_channel("tester")
        assert "Убрал" in msg, msg
        assert not p.monitor.has_channel("twitch:tester")
        with open(os.path.join(base, "links.txt"), encoding="utf-8") as f:
            assert "twitch.tv/tester" not in f.read()


def test_pipeline_tiktok_token_saves_and_starts():
    with tempfile.TemporaryDirectory() as base:
        cfg = {
            "_config_path": os.path.join(base, "config.yaml"),
            "links_file": "links.txt",
            "storage": {"clips_dir": "clips", "temp_dir": "temp"},
            "notify": {"provider": "none"},
            "tiktok": {
                "enabled": True, "mode": "api",
                "client_key": "key", "client_secret": "secret",
                "token_file": "tiktok_tokens.json",
                "dry_run": False,
            },
        }
        p = Pipeline(cfg, Notifier({"provider": "none"}, server_base_url=""))
        assert p.tiktok is None, "пока нет токена — постер не должен подниматься"

        msg = p.api_set_tiktok_token({"access_token": "act." + "x" * 40,
                                      "refresh_token": "rtf", "expires_in": 86400})
        assert "сохранён" in msg and "включён" in msg, msg
        tf = os.path.join(base, "tiktok_tokens.json")
        assert os.path.isfile(tf), "токен должен сохраниться в файл"
        with open(tf, encoding="utf-8") as f:
            data = json.load(f)
        assert data["access_token"].startswith("act.") and data["expires_at"] > 0
        assert p.tiktok is not None and p.tiktok._thread is not None, \
            "после токена воркер автопостинга должен стартовать"
        assert p.api_status()["tiktok"]["enabled"] is True
        p.tiktok.stop()

        # слишком короткий «токен» — внятный отказ
        msg = p.api_set_tiktok_token({"access_token": "123"})
        assert msg.startswith("Нужен"), msg


def test_full_stack_site_adds_channel():
    """Интеграция: сайт -> ClipServer -> Pipeline -> links.txt + /api/status."""
    with tempfile.TemporaryDirectory() as base:
        cfg = {
            "_config_path": os.path.join(base, "config.yaml"),
            "links_file": "links.txt",
            "clip": {"pre_roll": 25, "post_roll": 20},
            "hype": {"threshold": 62},
            "recorder": {"segment_time": 4, "buffer_keep": 300},
            "storage": {"clips_dir": "clips", "temp_dir": "temp"},
            "notify": {"provider": "none"},
            "subtitle": {"enabled": False},
        }
        docs = os.path.join(base, "docs")
        os.makedirs(docs)
        p = Pipeline(cfg, Notifier({"provider": "none"}, server_base_url=""))
        srv = ClipServer(os.path.join(base, "clips"), port=0, docs_dir=docs,
                         status_provider=lambda: p.api_status(),
                         channels_api=p)
        srv.start()
        try:
            st, j = post(srv.port, "/api/channels",
                         {"url": "https://www.twitch.tv/fullstack"})
            assert j["ok"] is True, j
            st, j = get(srv.port, "/api/status")
            names = [c["name"] for c in j["channels"]]
            assert "fullstack" in names, names
            assert "ad" in j["channels"][0] and "ad_guard" in j, j
        finally:
            srv.stop()
