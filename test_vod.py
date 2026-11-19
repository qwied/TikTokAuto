# -*- coding: utf-8 -*-
"""vod: выбор интересных окон по громкости; gh_run: очередь нарезки."""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vod
from links import parse_link


def test_pick_windows_prefers_loud_peaks():
    """Окна берутся там, где громкие всплески, а не в тихих местах."""
    sr = 1                                     # rms-точка = 1 секунда
    dur = 3600
    rms = np.full(dur, 0.05)
    rms[1000:1060] = 0.9                       # громкий момент №1
    rms[2500:2560] = 0.8                       # громкий момент №2
    wins = vod.pick_windows(rms, dur, max_windows=10, win=60)
    assert wins and len(wins) <= 10
    starts = [w[0] for w in wins]
    # оба громких пика накрыты окнами
    assert any(s <= 1000 < s + 60 for s in starts), starts
    assert any(s <= 2500 < s + 60 for s in starts), starts
    # каждое окно несёт оценку интересности
    assert all(len(w) == 3 and isinstance(w[2], float) for w in wins)


def test_pick_windows_best_first():
    """Самое сочное окно — ПЕРВОЕ: нарезка длинного видео начинается с лучших минут."""
    dur = 7200
    rms = np.full(dur, 0.05)
    rms[3000:3060] = 0.95                      # САМЫЙ громкий и плотный момент
    rms[6000:6060] = 0.4                       # момент послабее
    wins = vod.pick_windows(rms, dur, max_windows=10, win=60)
    starts = [w[0] for w in wins]
    scores = [w[2] for w in wins]
    assert starts[0] <= 3000 < starts[0] + 60, starts       # лучший — первым
    assert scores == sorted(scores, reverse=True), scores   # по убыванию оценки


def test_pick_windows_silence_penalty():
    """Минута с паузами проигрывает минуте сплошного накала при том же среднем."""
    dur = 3600
    base = np.full(dur, 0.05)
    # окно A: постоянный громкий гомон (без пауз)
    base[1000:1060] = 0.6
    # окно B: тот же средний уровень, но с тишиной и одним криком
    seg = np.full(60, 0.05)
    seg[10:20] = 1.0                            # 10 c крика + 50 c тишины
    base[2000:2060] = seg
    wins = vod.pick_windows(base, dur, max_windows=10, win=60)
    starts = [w[0] for w in wins]
    score_a = next(w[2] for w in wins if w[0] <= 1000 < w[0] + 60)
    score_b = next(w[2] for w in wins if w[0] <= 2000 < w[0] + 60)
    assert any(s <= 1000 < s + 60 for s in starts)
    assert any(s <= 2000 < s + 60 for s in starts)
    assert score_a > score_b, (score_a, score_b)   # сплошной накал ценнее


def test_pick_windows_short_video_single_window():
    rms = np.full(45, 0.2)
    assert vod.pick_windows(rms, 45, win=60) == [(0, 45, 0.0)]


def test_pick_windows_spacing():
    """Окна не слипаются: между ними минимум max(240, длина/макс_окон)."""
    dur = 9000
    rms = np.full(dur, 0.05)
    idx = np.arange(500, dur - 500, 61)
    rms[idx] = 0.95                            # много равных пиков
    wins = vod.pick_windows(rms, dur, max_windows=100, win=60)
    starts = [w[0] for w in wins]
    for a, b in zip(sorted(starts), sorted(starts)[1:]):
        assert b - a >= 240, (a, b)
    assert len(wins) <= 100


def test_links_clip_and_vod_kinds():
    c1 = parse_link("https://clips.twitch.tv/FunnyPleasantMooseAMAZE")
    c2 = parse_link("https://www.twitch.tv/honeymad/clip/SomeClipName123")
    v = parse_link("https://www.twitch.tv/videos/123456789")
    ch = parse_link("https://www.twitch.tv/honeymad")
    yt = parse_link("https://www.youtube.com/watch?v=abcDEF12345")
    assert c1.kind == "clip" and c1.vod_url().startswith("https://clips.twitch.tv/")
    assert c2.kind == "clip" and c2.id.startswith("clip_")
    assert v.kind == "vod" and v.vod_url().endswith("/videos/123456789")
    assert ch.kind == "" and ch.id == "honeymad"      # канал не спутали с клипом
    assert yt.kind == "" and yt.video_id == "abcDEF12345"


def test_vod_key_window_vs_video():
    import gh_run
    assert gh_run.vod_key({"url": "u", "kind": "video"}) == "u"
    assert gh_run.vod_key({"url": "u", "kind": "window", "start": 0, "end": 60}) == "u|0-60"
    assert gh_run.vod_key({"url": "u", "kind": "window", "start": 60, "end": 120}) != "u|0-60"


def test_enqueue_vod_jobs(tmp_path, monkeypatch):
    """Новые видео/клипы попадают в jobs.json; дубли не добавляются дважды."""
    import json

    import gh_run

    class FakeGH:
        repo = "o/r"

        def __init__(self):
            self.store = {}

        def get_file(self, path):
            if path not in self.store:
                return None, None
            return self.store[path], "sha-%s" % path

        def put_file(self, path, data, sha, message):
            self.store[path] = data

    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)   # dispatch -> False
    gh = FakeGH()
    refs = [
        parse_link("https://www.twitch.tv/honeymad"),               # канал — мимо
        parse_link("https://clips.twitch.tv/ClipSlugOne"),
        parse_link("https://www.twitch.tv/videos/777"),
        parse_link("https://www.youtube.com/watch?v=video1234567"), # видео — в очередь
    ]
    added = gh_run.enqueue_vod_jobs(gh, refs, live_keys=set())
    assert added == 3
    jobs = json.loads(gh.store[gh_run.JOBS_JSON].decode())["jobs"]
    kinds = sorted(j["kind"] for j in jobs)
    assert kinds == ["clip", "video", "video"]
    # повторный вызов ничего не добавляет
    assert gh_run.enqueue_vod_jobs(gh, refs, live_keys=set()) == 0
    # прямая ссылка на идущий эфир не ставится в очередь
    live = {"youtube:video1234567"}
    gh.store.pop(gh_run.JOBS_JSON, None)
    added2 = gh_run.enqueue_vod_jobs(gh, refs, live_keys=live)
    assert added2 == 2
