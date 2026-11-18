"""Тесты паузы нарезки во время рекламы (ad_guard) и пометок буфера.

Запуск: python -m pytest tests/test_ad_guard.py -q
"""

import os
import queue
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ad_guard import AdGuard, detect_ad  # noqa: E402
from models import HypeEvent  # noqa: E402
from notifier import Notifier  # noqa: E402
from pipeline import ActiveStream, Pipeline  # noqa: E402
from recorder import SegmentRecorder  # noqa: E402

AD_PLAYLIST = (
    "#EXTM3U\n"
    '#EXT-X-DATERANGE:ID="twitch-stitched-ad-1",START-TIME="0",CLASS="twitch-stitched-ad"\n'
    "#EXTINF:4.0,\nad-segment.ts\n"
    "#EXT-X-CUE-OUT:DURATION=30\n"
)
CLEAN_PLAYLIST = "#EXTM3U\n#EXTINF:4.0,\nseg_1.ts\n#EXTINF:4.0,\nseg_2.ts\n"


def make_cfg(base):
    return {
        "_config_path": os.path.join(base, "config.yaml"),
        "links_file": "links.txt",
        "clip": {"pre_roll": 25, "post_roll": 20, "cooldown": 60, "max_per_hour": 3},
        "hype": {"threshold": 62},
        "recorder": {"segment_time": 4, "buffer_keep": 300},
        "storage": {"clips_dir": "clips", "temp_dir": "temp"},
        "notify": {"provider": "none"},
        "subtitle": {"enabled": False},
        "ad": {"enabled": True, "poll": 1},
    }


def test_detect_ad_markers():
    assert detect_ad(AD_PLAYLIST) is True
    assert detect_ad(CLEAN_PLAYLIST) is False
    assert detect_ad("") is False
    assert detect_ad(None) is False
    assert detect_ad("#EXT-X-SCTE35:xyz", ["ext-x-scte35"]) is True
    assert detect_ad(CLEAN_PLAYLIST, ["no-such-marker"]) is False


def test_guard_manual_pause_and_resume():
    g = AdGuard("chan", {"poll": 1}, get_url=lambda: None)
    assert g.ad_active is False
    assert g.set_manual(True) is True
    assert g.ad_active is True and g.manual_ad is True
    assert g.intervals and g.intervals[-1][1] is None
    assert g.set_manual(False) is False
    assert g.intervals[-1][1] is not None, "рекламный интервал должен закрыться"
    # повторное set_manual(True) не создаёт второй интервал без изменения
    n = len(g.intervals)
    g.set_manual(True)
    assert len(g.intervals) == n + 1
    g.stop()


def test_guard_auto_from_playlist():
    state = {"text": CLEAN_PLAYLIST}
    g = AdGuard("chan", {"poll": 1}, get_url=lambda: "http://fake/pl.m3u8")
    g.fetcher = lambda url: state["text"]
    changes = []
    g.on_change = lambda active: changes.append(active)
    g._tick()
    assert g.auto_ad is False and g.ad_active is False
    state["text"] = AD_PLAYLIST
    g._tick()
    assert g.auto_ad is True and g.ad_active is True
    assert changes[-1] is True
    state["text"] = None          # сеть недоступна — не считаем рекламой
    g._tick()
    assert g.ad_active is True, "ошибка сети не должна сбрасывать рекламу"
    state["text"] = CLEAN_PLAYLIST
    g._tick()
    assert g.ad_active is False and changes[-1] is False
    g.stop()


def test_guard_run_thread(tmp_path=None):
    texts = {"v": AD_PLAYLIST}
    g = AdGuard("chan", {"poll": 1}, get_url=lambda: "http://fake/pl.m3u8")
    g.fetcher = lambda url: texts["v"]
    g.start()
    deadline = time.time() + 5
    while time.time() < deadline and not g.ad_active:
        time.sleep(0.05)
    assert g.ad_active is True
    g.set_manual(False)           # manual off — auto всё ещё True
    assert g.ad_active is True
    texts["v"] = CLEAN_PLAYLIST
    deadline = time.time() + 5
    while time.time() < deadline and g.ad_active:
        time.sleep(0.05)
    assert g.ad_active is False
    g.stop()


def test_recorder_marks_ad_segments():
    with tempfile.TemporaryDirectory() as base:
        rec = SegmentRecorder(os.path.join(base, "buf"), seg_time=4)
        t0 = time.time()
        paths = []
        for i in range(4):        # 4 сегмента подряд: [t0, t0+16)
            p = os.path.join(str(rec.workdir), f"seg_{i:06d}.ts")
            paths.append(p)
            rec.file_times[p] = t0 + i * 4
        # реклама внутри второго и третьего сегментов
        rec.mark_ad(t0 + 5, t0 + 11)
        got = rec.reserve_segments(t0, t0 + 16)
        names = [os.path.basename(p) for p in got]
        assert "seg_000000.ts" in names, names
        assert "seg_000001.ts" not in names, names
        assert "seg_000002.ts" not in names, names
        assert "seg_000003.ts" in names, names
        rec.release(paths)
        # если чистых сегментов почти нет — отдаём всё, лучше с рекламой
        rec2 = SegmentRecorder(os.path.join(base, "buf2"), seg_time=4)
        only = []
        for i in range(2):
            p = os.path.join(str(rec2.workdir), f"seg_{i:06d}.ts")
            only.append(p)
            rec2.file_times[p] = t0 + i * 4
        rec2.mark_ad(t0, t0 + 8)   # вся выборка — реклама
        got2 = rec2.reserve_segments(t0, t0 + 8)
        assert len(got2) == 2, "фолбэк: клип лучше с рекламой, чем никакого"


def _pipeline_with_stream(base):
    """Pipeline + один активный «эфир» с ручным управлением guard."""
    cfg = make_cfg(base)
    p = Pipeline(cfg, Notifier({"provider": "none"}, server_base_url=""))
    st = ActiveStream(ch=None, recorder=None, detector=None,
                      watcher=None, chat_q=queue.Queue())
    p.active["twitch:tester"] = st
    return p, st


def test_pipeline_hype_paused_during_ad():
    with tempfile.TemporaryDirectory() as base:
        p, st = _pipeline_with_stream(base)
        guard = AdGuard("twitch:tester", {"poll": 1}, get_url=lambda: None)
        st.guard = guard

        ev = HypeEvent(ts=time.time(), score=90.0, top_messages=[],
                       stats={"users": 5, "msgs": 20, "rate": 4.0})
        guard.set_manual(True)                 # реклама идёт
        p._on_hype("twitch:tester", ev)
        assert p.clip_queue.empty(), "хайп во время рекламы НЕ должен снимать клип"

        guard.set_manual(False)                # реклама кончилась
        p._on_hype("twitch:tester", ev)
        key, got = p.clip_queue.get(timeout=1)
        assert key == "twitch:tester" and got is ev
        guard.stop()

        # без guard — тоже снимает
        st.guard = None
        p._on_hype("twitch:tester", ev)
        assert p.clip_queue.qsize() == 1


def test_pipeline_manual_clip_blocked_and_api_ad():
    with tempfile.TemporaryDirectory() as base:
        p, st = _pipeline_with_stream(base)
        guard = AdGuard("twitch:tester", {"poll": 1}, get_url=lambda: None)
        st.guard = guard
        st.ch = type("Ch", (), {"display": lambda s: "twitch.tv/tester",
                                "short_id": lambda s: "tester"})()
        st.detector = type("D", (), {"manual_trigger": lambda s, r: None})()

        guard.set_manual(True)
        msg = p.manual_clip("тест")
        assert "РЕКЛАМА" in msg and "паузе" in msg, msg

        reply = p.api_manual_ad("off", "tester")
        assert "продолжается" in reply or "кончилась" in reply, reply
        assert guard.ad_active is False
        msg = p.manual_clip("тест")
        assert "Снимаю клип" in msg, msg
        guard.stop()
