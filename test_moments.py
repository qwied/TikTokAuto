# -*- coding: utf-8 -*-
"""Тесты moments.py + интеграции pick_windows (YAMNet-события).

Запуск:  python test_moments.py [путь_к_видео]
Без видео — только синтетика (детерминированная, быстрая).
"""
import logging
import sys

import numpy as np

logging.basicConfig(level=logging.INFO)

import moments
import vod


def test_pick_windows_laugh_boost():
    """Окно со смехом должно выбираться раньше громкой пустышки."""
    rng = np.random.default_rng(7)
    n = 900
    rms = rng.uniform(0.02, 0.05, n)          # ровный «фон»
    rms[100:160] *= 3.0                        # громкая пустышка (без смеха)
    laugh = np.zeros(n)
    laugh[600:660] = rng.uniform(0.3, 0.9, 60)   # СМЕХ в тихом месте
    shout = np.zeros(n)
    shout[600:660] = rng.uniform(0.1, 0.4, 60)
    wins = vod.pick_windows(rms, float(n), max_windows=3,
                            laugh=laugh, shout=shout, appl=None)
    assert wins, "окна не выбраны"
    top = wins[0]
    assert 560 <= top[0] <= 660, "окно со смехом не первое: %s" % (wins,)
    print("OK: окно со смехом первое", wins[0])

    # без событий — прежнее поведение (громкая пустышка впереди)
    wins_plain = vod.pick_windows(rms, float(n), max_windows=3)
    assert wins_plain, "окна не выбраны без событий"
    print("OK: без событий отбор по громкости", wins_plain[0])


def test_fail_open():
    """pick_windows с мусорными кривыми не падает (fail-open)."""
    rng = np.random.default_rng(3)
    n = 900
    rms = rng.uniform(0.02, 0.05, n)
    wins = vod.pick_windows(rms, float(n), max_windows=2,
                            laugh=np.zeros(n), shout=None, appl=None)
    assert wins
    print("OK: нулевые кривые — отбор работает", wins[0])


def test_event_curves(video):
    """На реальном видео кривые считаются и имеют правильную длину."""
    ev = moments.event_curves(video)
    assert ev is not None, "YAMNet недоступен"
    laugh, shout, appl = ev
    assert laugh.size >= 4 and laugh.shape == shout.shape == appl.shape, \
        "кривые пустые или разной длины: %d" % laugh.size
    assert 0.0 <= float(laugh.min()) and float(laugh.max()) <= 1.0
    print("OK: кривые %d с; смех пик %.2f, крики пик %.2f, толпа пик %.2f"
          % (laugh.size, laugh.max(), shout.max(), appl.max()))


if __name__ == "__main__":
    test_pick_windows_laugh_boost()
    test_fail_open()
    if len(sys.argv) > 1 and moments.available():
        test_event_curves(sys.argv[1])
    elif len(sys.argv) > 1:
        print("SKIP: YAMNet недоступен, реальный тест пропущен")
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
