"""Тесты парсера ссылок и динамического списка каналов. Запуск: python tests/test_links.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from links import LinksFile, dedup_key, parse_link  # noqa: E402
from models import ChannelRef  # noqa: E402
from stream_monitor import StreamMonitor  # noqa: E402


def test_parse_twitch():
    r = parse_link("twitch.tv/papanov")
    assert r and r.platform == "twitch" and r.id == "papanov", r
    r = parse_link("https://www.twitch.tv/SomeName?sr=a")
    assert r and r.platform == "twitch" and r.id == "somename", r
    r = parse_link("www.twitch.tv/name/")
    assert r and r.id == "name", r
    r = parse_link("PAPANOV")
    assert r and r.platform == "twitch" and r.id == "papanov", r   # голый ник -> Twitch
    print("ok: ссылки Twitch (ссылки, www, параметры, голый ник)")


def test_parse_youtube():
    r = parse_link("https://www.youtube.com/@MyStream")
    assert r and r.platform == "youtube" and "@MyStream" in r.id, r
    r = parse_link("youtube.com/@handle/live")
    assert r and r.platform == "youtube", r
    r = parse_link("https://www.youtube.com/channel/UC1234567890abcdefghij")
    assert r and "/channel/UC1234567890abcdefghij" in r.id, r
    r = parse_link("https://www.youtube.com/c/имяканала")
    assert r and r.platform == "youtube", r
    for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "youtube.com/watch?si=x&v=dQw4w9WgXcQ&t=30",
                "https://youtu.be/dQw4w9WgXcQ?t=1",
                "https://www.youtube.com/live/dQw4w9WgXcQ?feature=share"):
        r = parse_link(url)
        assert r and r.platform == "youtube" and r.video_id == "dQw4w9WgXcQ", (url, r)
        assert r.video_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ", r
    print("ok: ссылки YouTube (каналы @/c/user/channel, видео, youtu.be, /live)")


def test_parse_garbage():
    assert parse_link("") is None
    assert parse_link("# комментарий") is None
    assert parse_link("https://example.com/foo") is None
    assert parse_link("привет как дела") is None           # пробел внутри -> мусор
    assert parse_link("https://twitch.tv/") is None
    r = parse_link("twitch.tv/name — наш основной стример")
    assert r and r.id == "name", r                          # ссылка + комментарий в строке
    print("ok: мусор не превращается в канал; ссылка с комментарием распознаётся")


def test_dedup_key():
    a = parse_link("twitch.tv/Name")
    b = parse_link("NAME")
    assert dedup_key(a) == dedup_key(b), (a, b)
    assert dedup_key(a) != dedup_key(parse_link("youtube.com/@Name"))
    print("ok: одинаковые ссылки в разном виде дают один ключ")


def test_links_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "links.txt")
        lf = LinksFile(path)
        assert not lf.exists()
        a = parse_link("twitch.tv/aaa")
        b = parse_link("https://www.youtube.com/@bbb")
        assert lf.add(a) is True
        assert lf.add(b) is True
        assert lf.add(parse_link("twitch.tv/AAA")) is False   # тот же канал в другом регистре
        assert not lf.changed()
        with open(path, "a", encoding="utf-8") as f:
            f.write("twitch.tv/ccc\n")
        assert lf.changed()
        refs, bad = lf.read_refs()
        assert [x.short_id() for x in refs] == ["aaa", "bbb", "ccc"], refs
        assert bad == []
        assert lf.remove(b) is True
        refs, _bad = lf.read_refs()
        assert [x.short_id() for x in refs] == ["aaa", "ccc"], refs
        assert lf.remove(b) is False
        print("ok: links.txt — добавление, дедупликация, правка на ходу, удаление")


def test_monitor_dynamic():
    offline = []
    m = StreamMonitor([], on_offline=offline.append)
    assert m.snapshot() == []
    assert m.add_channel(parse_link("twitch.tv/aaa"))
    assert not m.add_channel(parse_link("https://twitch.tv/aaa"))   # дубликат
    assert m.has_channel("twitch:aaa")
    m.sync_channels([parse_link("twitch.tv/bbb"), parse_link("youtube.com/@ccc")])
    keys = [c.key for c in m.snapshot()]
    assert keys == ["twitch:bbb", "youtube:https://www.youtube.com/@ccc"], keys
    ch = m.snapshot()[0]
    ch.is_live = True
    assert m.remove_channel(ch.key)
    assert len(offline) == 1 and offline[0].key == ch.key          # ушёл из эфира -> остановка сессии
    assert m.remove_channel(ch.key) is False
    print("ok: монитор — добавление/синхронизация/удаление на ходу, остановка эфира")


def test_state_short_names():
    r = parse_link("https://www.youtube.com/@MyStream")
    assert r.short_id() == "MyStream", r.short_id()
    assert r.display() == "youtube.com/@MyStream", r.display()
    v = parse_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert v.short_id() == "dQw4w9WgXcQ", v.short_id()
    t = ChannelRef(platform="twitch", id="name")
    assert t.display() == "twitch.tv/name"
    assert t.page_url() == "https://twitch.tv/name"
    print("ok: короткие имена каналов для файлов и сообщений")


if __name__ == "__main__":
    test_parse_twitch()
    test_parse_youtube()
    test_parse_garbage()
    test_dedup_key()
    test_links_file()
    test_monitor_dynamic()
    test_state_short_names()
    print("ВСЕ ТЕСТЫ ССЫЛОК ПРОЙДЕНЫ")
