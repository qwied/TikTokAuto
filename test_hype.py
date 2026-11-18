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
    test_no_false_alarm()
    test_recent_messages_window()
    print("ВСЕ ТЕСТЫ ДЕТЕКТОРА ПРОШЛИ")


if __name__ == "__main__":
    main()
