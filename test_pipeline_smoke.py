"""Дымовой тест связки: Pipeline + Telegram-бот + файл ссылок (без сети).

Запуск: python tests/test_pipeline_smoke.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import TelegramBot  # noqa: E402
from links import parse_link  # noqa: E402
from notifier import Notifier  # noqa: E402
from pipeline import Pipeline  # noqa: E402


def make_cfg(base):
    return {
        "_config_path": os.path.join(base, "config.yaml"),
        "links_file": "links.txt",
        "clip": {"pre_roll": 25, "post_roll": 20, "cooldown": 60, "max_per_hour": 3,
                 "quality": "720p"},
        "hype": {"threshold": 62},
        "recorder": {"segment_time": 4, "buffer_keep": 300},
        "storage": {"clips_dir": "clips", "temp_dir": "temp"},
        "notify": {"provider": "none"},
        "subtitle": {"enabled": False},
    }


def test_pipeline_dynamic_channels():
    with tempfile.TemporaryDirectory() as base:
        cfg = make_cfg(base)
        p = Pipeline(cfg, Notifier({"provider": "none"}, server_base_url=""))

        # кидаем ссылку «как в Telegram»
        reply = p.add_channel_from_text("twitch.tv/tester", source="telegram")
        assert "tester" in reply and "Добавил" in reply, reply
        assert p.monitor.has_channel("twitch:tester")
        links_path = os.path.join(base, "links.txt")
        with open(links_path, encoding="utf-8") as f:
            assert "twitch.tv/tester" in f.read(), "ссылка должна сохраниться в links.txt"

        # та же ссылка ещё раз — «уже слежу»
        again = p.add_channel_from_text("https://twitch.tv/tester", source="telegram")
        assert "Уже слежу" in again, again

        # мусор — внятная ошибка
        bad = p.add_channel_from_text("привет как дела")
        assert "Не похоже" in bad, bad

        # список и статус
        assert "1. twitch.tv/tester" in p.list_text()
        assert "Каналов в слежке: 1" in p.status_text()

        # /remove по номеру
        removed = p.remove_channel_from_text("1")
        assert "Убрал" in removed, removed
        assert not p.monitor.has_channel("twitch:tester")
        assert p.status_text().startswith("Список ссылок пуст")
        print("ok: pipeline — ссылка из бота -> слежка + links.txt; /remove; /list; /status")


def test_manual_clip_without_live():
    with tempfile.TemporaryDirectory() as base:
        p = Pipeline(make_cfg(base), Notifier({"provider": "none"}))
        assert "нет активного стрима" in p.manual_clip()
        print("ok: ручной клип без эфира — внятный ответ")


def test_bot_routing():
    with tempfile.TemporaryDirectory() as base:
        cfg = make_cfg(base)
        p = Pipeline(cfg, Notifier({"provider": "none"}))
        bot = TelegramBot({"telegram": {"bot_token": "123:abc", "chat_id": "42"}},
                          {"list": p.list_text, "status": p.status_text,
                           "add": p.add_channel_from_text, "remove": p.remove_channel_from_text,
                           "clip": p.manual_clip})
        # ссылка обычным сообщением
        r = bot._route("twitch.tv/router_test")
        assert "router_test" in r and "Добавил" in r, r
        assert p.monitor.has_channel("twitch:router_test")
        # команды
        assert "/router_test" in bot._route("/list")
        assert "Каналов в слежке: 1" in bot._route("/status")
        assert "Снимаю клип" in bot._route("/clip") or "нет активного" in bot._route("/clip")
        assert bot._route("/remove twitch.tv/router_test")  # удаляем
        assert "Список пуст" in bot._route("/list")
        assert bot._route("/help").startswith("Я — StreamClipper")
        assert "Не знаю" in bot._route("/whatever")
        print("ok: бот — ссылка сообщением, /list /status /clip /remove /help")


def test_bot_access_control():
    cfg_tg = {"telegram": {"bot_token": "123:abc", "chat_id": "42"}}
    bot = TelegramBot(cfg_tg, {})
    assert bot._check_access("42", "owner")
    assert not bot._check_access("999", "stranger")
    # chat_id не заполнен — запоминаем первый чат, чужих не пускаем
    bot2 = TelegramBot({"telegram": {"bot_token": "123:abc"}}, {})
    assert bot2._check_access("777", "first")
    assert not bot2._check_access("888", "second")
    assert bot2._learned_chat == "777"
    print("ok: бот — отвечает только владельцу (chat_id или первый написавший)")


if __name__ == "__main__":
    test_pipeline_dynamic_channels()
    test_manual_clip_without_live()
    test_bot_routing()
    test_bot_access_control()
    print("ВСЕ ДЫМОВЫЕ ТЕСТЫ ПРОЙДЕНЫ")
