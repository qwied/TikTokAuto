"""Тесты длины клипа (clip.total = ровно 60 секунд) и конфига каналов-ссылок.

Запуск: python tests/test_pipeline_rolls.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from links import refs_from_config  # noqa: E402
from pipeline import resolve_rolls  # noqa: E402


def test_total_60():
    # по умолчанию: 30 до пика + 30 после = 60
    pre, post = resolve_rolls({"pre_roll": 30, "post_roll": 30, "total": 60})
    assert pre + post == 60, (pre, post)

    # пользователь поменял pre_roll — total всё равно 60
    pre, post = resolve_rolls({"pre_roll": 25, "post_roll": 20, "total": 60})
    assert pre == 25 and pre + post == 60, (pre, post)

    # без total — как указано в конфиге
    pre, post = resolve_rolls({"pre_roll": 30, "post_roll": 30})
    assert pre == 30 and post == 30

    # пустой конфиг — дефолт 30+30
    pre, post = resolve_rolls({})
    assert pre + post == 60

    # некорректный total игнорируется
    pre, post = resolve_rolls({"pre_roll": 30, "post_roll": 30, "total": "abc"})
    assert pre == 30 and post == 30

    # pre_roll слишком большой: post не меньше 5 c
    pre, post = resolve_rolls({"pre_roll": 58, "post_roll": 2, "total": 60})
    assert post == 5 and pre + post == 60, (pre, post)
    print("resolve_rolls OK")


def test_channels_from_config():
    cfg = {"channels": [
        "https://www.twitch.tv/SomeStreamer",
        "twitch.tv/other",
        "https://www.youtube.com/@KanalName",
        "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8tQyp6Q",
        "https://www.youtube.com/watch?v=VIDEOID12345",   # прямая ссылка на трансляцию
        "это не ссылка",                                   # отбрасывается с warning
        {"platform": "twitch", "id": "dict_style"},        # старый формат тоже работает
    ]}
    refs = refs_from_config(cfg)
    plats = [(r.platform, r.id) for r in refs]
    assert ("twitch", "somestreamer") in plats, plats
    assert ("twitch", "other") in plats
    assert ("youtube", "https://www.youtube.com/@KanalName") in plats
    assert ("youtube", "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8tQyp6Q") in plats
    assert ("youtube", "https://www.youtube.com/watch?v=VIDEOID12345") in plats
    assert ("twitch", "dict_style") in plats
    assert len(refs) == 6, plats        # «это не ссылка» отброшена
    print("refs_from_config OK")


if __name__ == "__main__":
    test_total_60()
    test_channels_from_config()
    print("PIPELINE TESTS OK")
