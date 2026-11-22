"""Тесты детектора хайпа. Запуск: python tests/test_hype.py"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hype_detector import HypeDetector, _is_excited  # noqa: E402
from models import ChatMessage  # noqa: E402

KW = ["ахах", "клип", "kekw"]


def make_detector():
    hype_cfg = {"window": 5.0, "threshold": 60, "sustain": 2, "min_users": 3}
    clip_cfg = {"cooldown": 0, "max_per_hour": 10}
    events = []
    d = HypeDetector("test:twitch", hype_cfg, clip_cfg, on_hype=events.append)
    return d, events


def test_excited_words():
    assert _is_excited("АХАХАХАХА", KW)
    assert _is_excited("KEKW", KW)
    assert _is_excited("ааааааа", KW)
    assert _is_excited("КЛИП КЛИП КЛИП", KW)
    assert _is_excited("ЭТО ЖЕ ГОЛ!!!", KW)          # капс + «!!!»
    assert not _is_excited("привет, как дела?", KW)
    assert not _is_excited("голова болит", KW)        # «гол» не должен ловиться внутри слова
    assert not _is_excited("", KW)
    print("ok: распознавание восторженных сообщений")


def test_burst_triggers_clip():
    d, events = make_detector()
    now = time.time()
    # спокойный фон
    for i in range(10):
        d.feed(ChatMessage(ts=now - 100 + i, user=f"bg{i}", text="обычное сообщение",
                           platform="twitch"))
    d.evaluate(now - 60)
    # всплеск: 12 авторов, 30 сообщений за 4 секунды
    for i in range(30):
        d.feed(ChatMessage(ts=now - 4 + (i % 5) * 0.8, user=f"u{i % 12}",
                           text="АХАХАХ КЛИП", platform="twitch"))
    for t in range(4):
        d.evaluate(now - 2 + t)
    assert events, "всплеск хайпа должен создать событие"
    assert events[0].score >= 60
    assert events[0].top_messages, "у события должны быть топ-сообщения"
    print(f"ok: всплеск -> клип (score={events[0].score})")


def test_idle_fallback():
    """Гарантированная нарезка: клип с лучшего момента — но ТОЛЬКО если чат
    хоть немного живой (жалоба «скучные моменты»: клип при мёртвом чате
    почти всегда скучный, лучше подождать оживления)."""
    cfg = {"window": 5.0, "threshold": 90, "sustain": 2, "min_users": 3}
    clip_cfg = {"cooldown": 0, "max_per_hour": 10, "max_idle_min": 20}
    events = []
    d = HypeDetector("test:twitch", cfg, clip_cfg, on_hype=events.append)
    now = time.time()
    # мёртвый чат: 8 сообщений 25 минут назад, последние 4 минуты — тишина
    for i in range(8):
        d.feed(ChatMessage(ts=now - 1500 + i, user=f"c{i}", text="тихое сообщение",
                           platform="twitch"))
    d.evaluate(now)
    assert not events, "при мёртвом чате принудительный клип НЕ снимается"
    # чат ожил: лёгкая реакция в последних 4 минутах -> клип с лучшего момента
    for i in range(10):
        d.feed(ChatMessage(ts=now - 30 + (i % 10) * 0.5, user=f"u{i}",
                           text=f"хахах ну и момент {i}", platform="twitch"))
    d.evaluate(now + 5)
    assert events, "после оживления чата принудительный клип снимается"
    # сразу второй раз не стреляет: last_trigger обновился
    d.evaluate(now + 6)
    assert len(events) == 1, "принудительные клипы не должны идти подряд"
    # а с выключенной настройкой — не стреляет вовсе
    events.clear()
    d2 = HypeDetector("test:twitch", cfg, {"cooldown": 0, "max_per_hour": 10,
                                           "max_idle_min": 0}, on_hype=events.append)
    for i in range(8):
        d2.feed(ChatMessage(ts=now - 1500 + i, user=f"c{i}", text="тихое сообщение",
                            platform="twitch"))
    d2.evaluate(now)
    assert not events, "max_idle_min: 0 должен отключать принудительную нарезку"
    print("ok: гарантированный клип — только при живом чате")


def test_clip_requests_fast_lane():
    """Чат ПРЯМО просит клип («КЛИП», «клипни его») от 2+ авторов —
    самый жирный сигнал: снимаем немедленно, без ожидания sustain."""
    d, events = make_detector()
    now = time.time()
    for i in range(10):
        d.feed(ChatMessage(ts=now - 100 + i, user=f"bg{i}",
                           text="спокойно всё", platform="twitch"))
    d.evaluate(now - 60)
    for i in range(8):
        d.feed(ChatMessage(ts=now - 2 + (i % 4) * 0.5, user=f"r{i % 3}",
                           text="КЛИП ЭТОТ МОМЕНТ" if i % 2 else "клипни его",
                           platform="twitch"))
    for t in range(3):
        d.evaluate(now - 1 + t)
    assert events, "просьбы «клип» от >=2 авторов должны сразу давать клип"
    print(f"ok: просьбы чата «клип» -> мгновенный клип (score={events[0].score})")


def test_peak_anchor():
    """Клип привязывается к ИСТИННОМУ пику чата, а не к моменту обнаружения:
    ранний маленький всплеск не должен утащить якорь на 40 секунд назад."""
    d, events = make_detector()
    now = time.time()
    # ранний малый всплеск — в историю, но слабее большого
    for i in range(6):
        d.feed(ChatMessage(ts=now - 40 + i * 0.5, user=f"p{i}",
                           text="хахах", platform="twitch"))
    d.evaluate(now - 30)
    # большой всплеск -> триггер
    for i in range(30):
        d.feed(ChatMessage(ts=now - 3 + (i % 12) * 0.25, user=f"u{i % 12}",
                           text="АХАХАХ КЛИП", platform="twitch"))
    for t in range(4):
        d.evaluate(now - 2 + t)
    assert events, "большой всплеск должен дать клип"
    assert now - 20 <= events[0].ts <= now, (
        "якорь должен стоять у большого пика, а не у раннего всплеска")
    print(f"ok: якорь на истинном пике чата (ts отстал на {now - events[0].ts:.1f} с)")


def test_no_false_alarm():
    d, events = make_detector()
    now = time.time()
    for i in range(20):
        d.feed(ChatMessage(ts=now - 10 + (i % 5), user=f"u{i}",
                           text=f"спокойное сообщение номер {i}", platform="twitch"))
    for t in range(6):
        d.evaluate(now - 5 + t)
    assert not events, "обычный чат не должен вызывать клип"
    print("ok: нет ложных срабатываний")


def test_recent_messages_window():
    d, _ = make_detector()
    now = time.time()
    d.feed(ChatMessage(ts=now - 100, user="old", text="старое", platform="twitch"))
    d.feed(ChatMessage(ts=now - 5, user="new", text="свежее", platform="twitch"))
    got = d.recent_messages(now - 30, now)
    assert len(got) == 1 and got[0].user == "new"
    print("ok: выборка сообщений за интервал")


def main():
    test_excited_words()
    test_burst_triggers_clip()
    test_clip_requests_fast_lane()
    test_peak_anchor()
    test_no_false_alarm()
    test_idle_fallback()
    test_recent_messages_window()
    print("ВСЕ ТЕСТЫ ДЕТЕКТОРА ПРОШЛИ")


if __name__ == "__main__":
    main()
