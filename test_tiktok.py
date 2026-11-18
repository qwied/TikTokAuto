"""Тесты автопостинга TikTok: описание, cookies, очередь/сухой режим."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok import TikTokPoster, build_caption, load_cookies_netscape  # noqa: E402


def test_caption():
    cap = build_caption({"title": "Лучший момент", "channel": "frostpony",
                         "platform": "twitch"}, ["fyp", "стрим"])
    assert "ЛУЧШИЙ" in cap.upper() or "Лучший" in cap
    assert "#fyp" in cap and "#стрим" in cap
    assert len(cap) <= 2200
    long = build_caption({"title": "х" * 3000}, None)
    assert len(long) == 2200
    print(f"ok: описание -> {cap!r}")


def test_cookies_parse():
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "test_out_cookies.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(".tiktok.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\tabc123\n")
        f.write("#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t1999999999\ttt_webid\txyz\n")
        f.write(".google.com\tTRUE\t/\tTRUE\t1999999999\tnid\tzzz\n")
    out = load_cookies_netscape(tmp)
    os.remove(tmp)
    assert len(out) == 2, f"должны отобраться только tiktok-cookies: {out}"
    assert {c["name"] for c in out} == {"sessionid", "tt_webid"}
    print("ok: cookies.txt -> только tiktok.com (%d шт.)" % len(out))


def test_dry_run_queue():
    cfg = {"enabled": True, "mode": "browser", "dry_run": True,
           "delay_min": 0, "delay_max": 0,
           "cookies_file": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "nofile.txt")}
    poster = TikTokPoster(cfg, base_dir=os.path.dirname(os.path.abspath(__file__)))
    assert poster.ready(), "dry_run должен быть готов всегда"
    poster.start()
    poster.enqueue("clip1.mp4", "тест 1")
    poster.enqueue("clip2.mp4", "тест 2")
    deadline = time.time() + 10
    while time.time() < deadline and poster.posted < 2:
        time.sleep(0.2)
    poster.stop()
    assert poster.posted == 2, f"ожидали 2 публикации, а {poster.posted}"
    assert poster.failed == 0
    st = poster.status()
    assert st["enabled"] and st["mode"] == "browser" and st["posted"] == 2
    print("ok: очередь/сухой режим ->", st)


def main():
    test_caption()
    test_cookies_parse()
    test_dry_run_queue()
    print("ВСЕ ТЕСТЫ TIKTOK ПРОШЛИ")


if __name__ == "__main__":
    main()
