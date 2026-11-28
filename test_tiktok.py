"""Тесты автопостинга TikTok: описание, cookies, очередь/сухой режим."""

import json
import os
import sys
import tempfile
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
    tmp = tempfile.mkdtemp(prefix="tt_dry_")   # в репо ничего не пишем
    cfg = {"enabled": True, "mode": "browser", "dry_run": True,
           "delay_min": 0, "delay_max": 0,
           "cookies_file": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "nofile.txt")}
    poster = TikTokPoster(cfg, base_dir=tmp)
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


def _api_cfg(tmp, **extra):
    """Готовый api-постер без сети: токен-файл создаётся в tmp."""
    with open(os.path.join(tmp, "tok.json"), "w", encoding="utf-8") as f:
        json.dump({"access_token": "x", "refresh_token": "r"}, f)
    cfg = {"enabled": True, "mode": "api", "dry_run": False,
           "delay_min": 0, "delay_max": 0, "spread_through_day": False,
           "client_key": "k", "client_secret": "s", "token_file": "tok.json"}
    cfg.update(extra)
    return cfg


def test_daily_cap():
    """Сверх дневного лимита клипы НЕ постятся и НЕ теряются: ждут в
    отложенной очереди до завтра."""
    tmp = tempfile.mkdtemp(prefix="tt_cap_")
    tk = TikTokPoster(_api_cfg(tmp, max_per_day=2), base_dir=tmp)
    assert tk.ready()
    # имитируем 2 уже вышедших сегодня поста
    tk._log_post("a.mp4")
    tk._log_post("b.mp4")
    assert tk._posts_today() == 2
    tk._post = lambda path, cap: None          # публикация без сети
    tk.start()
    tk.enqueue("c.mp4", "лимит 1")
    tk.enqueue("d.mp4", "лимит 2")
    deadline = time.time() + 8
    while time.time() < deadline and len(tk.pending_entries()) < 2:
        time.sleep(0.2)
    tk.stop()
    assert tk.posted == 0, "сверх дневного лимита постов быть не должно"
    pend = {e["id"]: e for e in tk.pending_entries()}
    assert {"c.mp4", "d.mp4"} <= set(pend), pend
    assert "лимит" in str(pend["c.mp4"].get("last_error"))
    assert tk._posts_today() == 2, "лимитные клипы не должны попасть в журнал"
    print("ok: дневной лимит 2 -> сверх лимита клипы ждут завтра в очереди")


def test_post_log_days():
    """Вчерашние посты не съедают сегодняшний лимит; журнал переживает
    перезапуск (персистентность между запусками Actions)."""
    tmp = tempfile.mkdtemp(prefix="tt_log_")
    tk = TikTokPoster(_api_cfg(tmp, max_per_day=2), base_dir=tmp)
    tk._post_log = [{"id": "old.mp4", "ts": time.time() - 86400 * 1.2}]
    assert tk._posts_today() == 0, "вчерашний пост не считается сегодняшним"
    tk._log_post("new.mp4")
    assert tk._posts_today() == 1
    tk._save_post_log()
    tk2 = TikTokPoster(_api_cfg(tmp, max_per_day=2), base_dir=tmp)
    assert tk2._posts_today() == 1, "журнал должен читаться с диска"
    # лимит 2: один пост уже был — ещё один проходит, третий откладывается
    tk2._post = lambda path, cap: None
    tk2.start()
    tk2.enqueue("n2.mp4", "ок")
    deadline = time.time() + 8
    while time.time() < deadline and tk2.posted < 1:
        time.sleep(0.2)
    assert tk2.posted == 1
    tk2.enqueue("n3.mp4", "сверх")
    deadline = time.time() + 8
    while time.time() < deadline and len(tk2.pending_entries()) < 1:
        time.sleep(0.2)
    tk2.stop()
    assert tk2._posts_today() == 2 and tk2.posted == 1
    print("ok: журнал постов — вчерашнее не в счёт, лимит держится между запусками")


def main():
    test_caption()
    test_cookies_parse()
    test_dry_run_queue()
    test_daily_cap()
    test_post_log_days()
    print("ВСЕ ТЕСТЫ TIKTOK ПРОШЛИ")


if __name__ == "__main__":
    main()
