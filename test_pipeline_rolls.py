"""Тесты длины клипа (clip.total = ровно 60 секунд) и конфига каналов-ссылок.

Запуск: python tests/test_pipeline_rolls.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from links import refs_from_config  # noqa: E402
from pipeline import (find_logical_cut, pick_total,  # noqa: E402
                              resolve_rolls, trim_clip_end)
from utils import ffprobe_meta, run_cmd  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "test_out")


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


def test_pick_total_auto():
    """total: auto — длина клипа по силе момента 35/45/60."""
    assert pick_total(85.0, {"total": "auto"}) == 60.0      # жирный хайп
    assert pick_total(78.0, {"total": "auto"}) == 60.0      # граница
    assert pick_total(65.0, {"total": "auto"}) == 45.0      # хороший момент
    assert pick_total(60.0, {"total": "auto"}) == 45.0      # граница
    assert pick_total(55.0, {"total": "auto"}) == 35.0      # обычный
    assert pick_total(55.0, {}) == 35.0                     # конфиг пуст
    assert pick_total(55.0, {"total": 60}) == 60.0          # число = фикс
    assert pick_total(55.0, {"total": "60"}) == 60.0        # число строкой
    assert pick_total(55.0, {"total": "junk"}) == 35.0      # мусор -> авто
    assert pick_total(55.0, {"total": 200}) == 90.0         # потолок 90
    assert pick_total(55.0, {"total": 5}) == 25.0           # пол 25
    print("pick_total OK")


def _synth_audio_pattern(path):
    """wav: речь 0-20 c, ТИШИНА 20-22, речь 22-24, ТИШИНА 24-25.5, речь 25.5-34."""
    os.makedirs(OUT_DIR, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
           "-f", "lavfi", "-i", "sine=frequency=330:duration=2",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1.5",
           "-f", "lavfi", "-i", "sine=frequency=550:duration=8.5",
           "-filter_complex", "[0:a][1:a][2:a][3:a][4:a]concat=n=5:v=0:a=1",
           str(path)]
    rc, _o, err = run_cmd(cmd, timeout=120)
    assert rc == 0, "ffmpeg не собрал тестовый звук: " + err
    return path


def test_logical_cut_finds_pause():
    """Логический конец режется НА ПАУЗЕ, а не по расчётной длине."""
    wav = _synth_audio_pattern(os.path.join(OUT_DIR, "pattern.wav"))
    # пауза 20-22 накрывает расчётный конец 21 -> режем прямо там
    cut = find_logical_cut(wav, OUT_DIR, 21.0, tail=10)
    assert 20.0 <= cut <= 22.0, cut
    # расчётный конец 23 (речь 22-24) — хвост тянется до первой тишины
    # ПОСЛЕ anchor (24.0): фраза договаривается, не теряется
    cut = find_logical_cut(wav, OUT_DIR, 23.0, tail=10)
    assert 23.9 <= cut <= 24.6, cut
    # расчётный конец 25 — внутри паузы 24-25.5
    cut = find_logical_cut(wav, OUT_DIR, 25.0, tail=10)
    assert 24.0 <= cut <= 25.5, cut
    # без хвоста — режем ровно по расчётной длине
    cut = find_logical_cut(wav, OUT_DIR, 21.0, tail=0)
    assert abs(cut - 21.0) < 0.3, cut
    print("find_logical_cut OK")


def test_trim_clip_end():
    """Обрезка хвоста копированием: длина после обрезки = заданной."""
    os.makedirs(OUT_DIR, exist_ok=True)
    mp4 = os.path.join(OUT_DIR, "trim_src.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=30",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(mp4)]
    rc, _o, err = run_cmd(cmd, timeout=180)
    assert rc == 0, "ffmpeg не собрал тестовый клип: " + err
    out = trim_clip_end(mp4, 21.0, OUT_DIR)
    dur = float(ffprobe_meta(out).get("duration") or 0)
    assert 20.5 <= dur <= 21.5, dur
    # обрезать нечего: cut >= длительности — файл не трогается
    out2 = trim_clip_end(out, 99.0, OUT_DIR)
    assert out2 == out
    dur2 = float(ffprobe_meta(out2).get("duration") or 0)
    assert abs(dur2 - dur) < 0.5, (dur, dur2)
    print("trim_clip_end OK")


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
    test_pick_total_auto()
    test_channels_from_config()
    test_logical_cut_finds_pause()
    test_trim_clip_end()
    print("PIPELINE TESTS OK")
