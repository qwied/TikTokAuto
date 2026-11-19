# -*- coding: utf-8 -*-
"""gh_run: подписки, записи clips.json, вставка клипа, чистка релиза."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gh_run


def test_load_subscriptions(tmp_path):
    p = tmp_path / "subscriptions.json"
    p.write_text(json.dumps({"channels": ["twitch.tv/abc", "  ",
                                          "https://youtube.com/@x"]}),
                 encoding="utf-8")
    subs = gh_run.load_subscriptions(str(p))
    assert subs == ["twitch.tv/abc", "https://youtube.com/@x"]
    assert gh_run.load_subscriptions(str(tmp_path / "nope.json")) == []


def test_load_subscriptions_auto_channels(tmp_path):
    """auto_channels (топ Twitch) следятся наравне с каналами человека."""
    p = tmp_path / "subscriptions.json"
    p.write_text(json.dumps({"channels": ["twitch.tv/abc"],
                             "auto_channels": ["https://www.twitch.tv/big1",
                                               " ", "https://www.twitch.tv/big2"]}),
                 encoding="utf-8")
    subs = gh_run.load_subscriptions(str(p))
    assert subs == ["twitch.tv/abc", "https://www.twitch.tv/big1",
                    "https://www.twitch.tv/big2"]


def test_merged_refs_dedup():
    cfg = {"channels": ["https://www.twitch.tv/abc"]}
    subs = ["twitch.tv/ABC", "youtube.com/@chan"]
    refs = gh_run.merged_refs(cfg, subs)
    tw = [gh_run.dedup_key(r) for r in refs if r.platform == "twitch"]
    # twitch.tv/abc и twitch.tv/ABC — один канал, дубль убран
    assert len(tw) == 1
    assert any(k[0] == "youtube" for k in (gh_run.dedup_key(r) for r in refs))


def test_clip_entry_shape(tmp_path):
    v = tmp_path / "2026-11-19_12-15-00_nick.mp4"
    v.write_bytes(b"x" * 100)
    e = gh_run.clip_entry(str(v), "https://github.com/o/r/releases/download/auto/x.mp4",
                          thumb_b64="data:image/jpeg;base64,AAA", size=100)
    assert e["id"] == "2026-11-19_12-15-00_nick"
    assert e["channel"] == "nick"
    assert e["gh"] is True
    assert e["url"].startswith("https://github.com/o/r/releases/")
    assert e["thumb"].startswith("data:image/jpeg")
    assert e["score"] == 0                     # без оценки — ноль, карточка молчит
    e2 = gh_run.clip_entry(str(v), "https://github.com/o/r/releases/download/auto/x.mp4",
                           score=3.87)
    assert e2["score"] == 3.9                  # «насколько сочный момент» для сайта
    e3 = gh_run.clip_entry(str(v), "u", score="мусор")
    assert e3["score"] == 0                    # мусор не ломает публикацию


def test_make_hashtags_game_channel_and_cap():
    """Хэштеги: игра по названию -> ник -> платформа -> вирусные; без дублей."""
    tags = gh_run.make_hashtags(platform="twitch", channel="Honeymad",
                                title="CS2 ПАК РАНКЕД!!")
    assert tags[0] == "#cs2" and "#кс2" in tags
    assert "#honeymad" in tags and "#twitch" in tags
    assert "#рек" in tags and "#fyp" in tags
    assert len(tags) <= 14
    assert len(set(tags)) == len(tags)               # без дублей
    ru = gh_run.make_hashtags(platform="twitch", channel="стример",
                              title="Дота 2 мид")
    assert "#дота2" in ru and "#стример" in ru       # кириллические теги тоже ок
    assert gh_run.make_hashtags()[0].startswith("#")  # даже без данных — не пусто


def test_clip_entry_hashtags_kept():
    v = tmp_vfile()
    e = gh_run.clip_entry(str(v), "u", hashtags=["#рек", "#fyp"])
    assert e["hashtags"] == ["#рек", "#fyp"]
    assert gh_run.clip_entry(str(v), "u")["hashtags"] == []


def tmp_vfile():
    import pathlib
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    f = d / "2026-11-19_12-15-00_x.mp4"
    f.write_bytes(b"xx")
    return f


def test_load_clips_json_bytes():
    data = gh_run.load_clips_json_bytes(json.dumps({"clips": [{"id": "a"}]}).encode())
    assert data["clips"][0]["id"] == "a"
    assert "meta" in data
    assert gh_run.load_clips_json_bytes(b"not json")["clips"] == []
    assert gh_run.load_clips_json_bytes(None)["clips"] == []


def test_channels_meta_and_sig():
    meta = gh_run.channels_meta_from_snapshot(
        [], [("twitch", "abc", True, "Title!", 5)])
    assert meta[0]["live"] is True and meta[0]["display"] == "abc"
    assert meta[0]["title"] == "Title!"


def test_channels_meta_accepts_monitor_rows():
    """Регрессия: probe_live отдаёт StreamMonitor.status_rows() (кортежи).

    Раньше probe_live возвращал mon.snapshot() — список объектов ChannelState;
    channels_meta_from_snapshot падала с TypeError «cannot unpack non-iterable
    ChannelState object» РОВНО в момент, когда канал в эфире: Check live умирал,
    Monitor не запускался, клипы не появлялись.
    """
    from stream_monitor import StreamMonitor
    ref = gh_run.parse_link("https://www.twitch.tv/abc")
    m = StreamMonitor(channels=[ref], quality="best", poll_twitch=2,
                      poll_youtube=3, yt_workers=3, cookies_file="",
                      cookies_from_browser="", links_file=None,
                      refresh_refs=lambda: [ref],
                      on_live=lambda ch: None, on_offline=lambda ch: None)
    rows = m.status_rows()
    assert rows and isinstance(rows[0], tuple) and rows[0][0] == "twitch"
    meta = gh_run.channels_meta_from_snapshot([ref], rows)
    assert meta and meta[0]["name"] == "abc"


def test_publisher_updates_clips_json(tmp_path):
    class FakeGH:
        repo = "o/r"

        def __init__(self):
            self.store = {"clips.json": (json.dumps({
                "updated": "", "meta": {"mode": "github", "channels": []},
                "clips": []}).encode("utf-8"), "sha1")}
            self.assets = []

        def release_id(self):
            return 7

        def upload_asset(self, rel, path):
            self.assets.append(os.path.basename(path))
            return {"name": os.path.basename(path)}

        def list_assets(self, rel):
            return [{"id": i, "name": n, "created_at": str(i)}
                    for i, n in enumerate(self.assets)]

        def delete_asset(self, aid):
            self.assets.pop(int(aid))

        def get_file(self, path):
            return self.store.get(path, (None, None))

        def put_file(self, path, data, sha, message):
            self.store[path] = (data, "sha2")

    gh = FakeGH()
    pub = gh_run.GitHubPublisher(gh, workdir=str(tmp_path), max_clips=2,
                                 max_assets=1)
    for k in (1, 2, 3):
        v = tmp_path / f"2026-11-19_0{k}-00-00_c{k}.mp4"
        v.write_bytes(b"0" * 10)
        assert pub.notify_clip(str(v)) is True
    data = json.loads(gh.store["clips.json"][0].decode("utf-8"))
    assert len(data["clips"]) == 2                      # max_clips=2
    assert data["clips"][0]["channel"] == "c3"          # новый сверху
    assert len(gh.assets) == 1                          # max_assets=1


def test_notify_cfg_from_env(monkeypatch):
    """Секреты NOTIFY_* включают уведомления; без секретов — выключены."""
    for k in [k for k in os.environ if k.startswith("NOTIFY_")]:
        monkeypatch.delenv(k)
    cfg = {"notify": {"provider": "none"}}
    assert gh_run.notify_cfg_from_env(cfg) == []
    assert cfg["notify"]["provider"] == "none"      # ничего не настроено

    monkeypatch.setenv("NOTIFY_NTFY_TOPIC", "sc-test-topic-123")
    providers = gh_run.notify_cfg_from_env(cfg)
    assert providers == ["ntfy"]
    assert cfg["notify"]["provider"] == "all"
    assert cfg["notify"]["ntfy"]["topic"] == "sc-test-topic-123"

    monkeypatch.setenv("NOTIFY_TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("NOTIFY_TELEGRAM_CHAT_ID", "42")
    providers = gh_run.notify_cfg_from_env(cfg)
    assert sorted(providers) == ["ntfy", "telegram"]


def test_publisher_push_hook(tmp_path, monkeypatch):
    """После публикации клипа уходит пуш со ссылкой на релиз."""
    class FakePush:
        def __init__(self):
            self.got = []

        def notify_clip(self, path, caption):
            self.got.append((path, caption))

    pub = gh_run.GitHubPublisher(gh=None, workdir=str(tmp_path),
                                 push=FakePush())
    monkeypatch.setattr(pub, "_publish",
                        lambda p, c="", **kw: setattr(pub, "last_url", "https://x/1"))
    assert pub.notify_clip("/tmp/clip.mp4", "Новый клип") is True
    path, cap = pub.push.got[0]
    assert path == "/tmp/clip.mp4"
    assert "Новый клип" in cap and "https://x/1" in cap

    # пуш сломался — публикация всё равно считается успешной
    pub.push.got.clear()
    pub.push.notify_clip = lambda p, c: 1 / 0
    assert pub.notify_clip("/tmp/clip.mp4") is True


def test_send_web_pushes_prune_and_skip(tmp_path, monkeypatch):
    """Web Push: подписки из push_subscriptions.json, протухшие чистятся."""
    class FakeGH:
        def __init__(self, store):
            self.store = store

        def get_file(self, path):
            return self.store.get(path, (None, None))

        def put_file(self, path, data, sha, message):
            self.store[path] = (data, "sha-next")

    subs = {"subscriptions": [
        {"endpoint": "https://push/ok", "keys": {"p256dh": "a", "auth": "b"}},
        {"endpoint": "https://push/gone", "keys": {"p256dh": "a", "auth": "b"}},
    ]}
    store = {"push_subscriptions.json": (json.dumps(subs).encode(), "sha1")}
    gh = FakeGH(store)
    for k in ("VAPID_PRIVATE_KEY", "VAPID_SUB"):
        monkeypatch.delenv(k, raising=False)

    calls = []

    def pusher(sub, data, vapid_private_key, vapid_claims):
        calls.append(sub["endpoint"])
        if "gone" in sub["endpoint"]:
            err = Exception("gone")
            err.status_code = 410
            raise err

    gh_run.send_web_pushes(gh, {"title": "t"}, pusher=pusher)
    assert calls == []                       # нет VAPID — отправок нет, файл цел

    monkeypatch.setenv("VAPID_PRIVATE_KEY", "PEM-KEY")
    gh_run.send_web_pushes(gh, {"title": "t"}, pusher=pusher)
    assert sorted(calls) == ["https://push/gone", "https://push/ok"]
    data = json.loads(store["push_subscriptions.json"][0].decode())
    assert [s["endpoint"] for s in data["subscriptions"]] == ["https://push/ok"]


def test_send_web_pushes_no_file(tmp_path, monkeypatch):
    """Нет push_subscriptions.json — тихий выход, без ошибок."""
    class FakeGH:
        def get_file(self, path):
            return None, None

        def put_file(self, *a):
            raise AssertionError("не должен писать")

    monkeypatch.setenv("VAPID_PRIVATE_KEY", "PEM-KEY")
    gh_run.send_web_pushes(FakeGH(), {"title": "t"}, pusher=lambda *a, **k: None)


def test_monitor_in_progress_excludes_self(monkeypatch):
    """Свой запуск (GITHUB_RUN_ID) не считается «Monitor уже работает».

    Финальная проверка внутри monitor.yml должна мочь запустить СЛЕДУЮЩУЮ
    сессию, пока её же задача ещё in_progress, — иначе цепочка слежения
    обрывалась бы после каждой сессии.
    """
    import subprocess as sp

    class R:
        returncode = 0
        stdout = '[{"databaseId": 111}, {"databaseId": 222}]'
        stderr = ""

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return R()

    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "222")
    monkeypatch.setattr(sp, "run", fake_run)
    assert gh_run.gh_workflow_monitor_in_progress() is True   # 111 — чужой

    monkeypatch.setenv("GITHUB_RUN_ID", "111")
    assert gh_run.gh_workflow_monitor_in_progress() is True   # 222 — чужой

    monkeypatch.setattr(gh_run, "subprocess", sp)             # на всякий
    R.stdout = '[{"databaseId": 333}]'
    monkeypatch.setenv("GITHUB_RUN_ID", "333")
    assert gh_run.gh_workflow_monitor_in_progress() is False  # только свой


# --- автопоиск топовых стримов Twitch ----------------------------------------

TOP = [
    {"login": "caseoh_", "display": "caseoh_", "viewers": 86000,
     "title": "t", "game": "GTA", "language": "ru"},
    {"login": "big1", "display": "Big1", "viewers": 30000,
     "title": "t", "game": "GTA", "language": "ru"},
    {"login": "small", "display": "small", "viewers": 500,
     "title": "t", "game": "GTA", "language": "ru"},
    {"login": "big2", "display": "Big2", "viewers": 5000,
     "title": "t", "game": "GTA", "language": "ru"},
    # нерусские эфиры — фильтр языка их выкидывает (новое поведение)
    {"login": "fren", "display": "Fren", "viewers": 90000,
     "title": "hola", "game": "GTA", "language": "es"},
]


def test_discovery_urls_filters_and_caps():
    # discovery_urls сам язык НЕ фильтрует (фильтр применяет auto_discover):
    # нерусский "fren" с 90000 зрителей попадает первым
    urls = gh_run.discovery_urls(TOP, min_viewers=2000, count=2,
                                 exclude=["fren"])
    assert urls == ["https://www.twitch.tv/caseoh_", "https://www.twitch.tv/big1"]
    # порядок = порядок входа (реальный Twitch отдаёт уже отсортированный)
    assert gh_run.discovery_urls(TOP, min_viewers=2000, count=25) \
        == ["https://www.twitch.tv/caseoh_", "https://www.twitch.tv/big1",
            "https://www.twitch.tv/big2", "https://www.twitch.tv/fren"]


def test_filter_by_language_keeps_only_russian():
    """Автопоиск берёт ТОЛЬКО русскоязычные эфиры (запрос пользователя)."""
    ru = gh_run.filter_by_language(TOP, "ru")
    logins = [s["login"] for s in ru]
    assert "fren" not in logins                       # испанский — отфильтрован
    assert {"caseoh_", "big1", "big2"} <= set(logins)
    # Twitch не дал language — кириллица в тайтле считается русским
    heur = gh_run.filter_by_language(
        [{"login": "rus", "title": "GTA 6 МАРАФОН", "viewers": 3000},
         {"login": "ger", "title": "GTA 6 NACH 13 JAHREN", "viewers": 9000}],
        "ru")
    assert [s["login"] for s in heur] == ["rus"]
    # фильтр отключается
    assert len(gh_run.filter_by_language(TOP, "")) == len(TOP)
    assert gh_run.discovery_urls([], min_viewers=0) == []
    assert gh_run.discovery_urls(TOP, min_viewers=100000) == []


def test_subs_twitch_logins_ignores_clips_and_vods():
    got = gh_run._subs_twitch_logins(
        ["https://www.twitch.tv/ABC", "twitch.tv/x/clip/slug",
         "https://www.twitch.tv/videos/123", "youtube.com/@c", "", None])
    assert got == {"abc"}


class FakeGH:
    repo = "o/r"

    def __init__(self, subs=None):
        raw = json.dumps(subs if subs is not None else
                         {"channels": ["https://www.twitch.tv/honeymad"]})
        self.store = {gh_run.SUBS_JSON: (raw.encode("utf-8"), "sha1")}

    def get_file(self, path):
        return self.store.get(path, (None, None))

    def put_file(self, path, data, sha, message):
        self.store[path] = (data, "sha2")


def test_auto_discover_preserves_user_channels_and_dedups():
    """Автопоиск пишет ТОЛЬКО auto_channels; каналы человека нетронуты."""
    gh = FakeGH({"channels": ["https://www.twitch.tv/big1"],
                 "_help": "hi"})
    cfg = {"discovery": {"enabled": True, "count": 3, "min_viewers": 2000}}
    urls = gh_run.auto_discover(gh, cfg, top_fn=lambda n: TOP, probe_fn=lambda: [])
    # big1 уже у человека — в авто-список не попадает, дубли не заводятся
    assert urls == ["https://www.twitch.tv/caseoh_",
                    "https://www.twitch.tv/big2"]
    data = json.loads(gh.store[gh_run.SUBS_JSON][0].decode("utf-8"))
    assert data["channels"] == ["https://www.twitch.tv/big1"]
    assert data["auto_channels"] == urls
    assert data["_help"] == "hi"
    assert data["auto_updated"].endswith("Z")


def test_auto_discover_no_commit_when_same():
    gh = FakeGH({"channels": ["twitch.tv/honeymad"],
                 "auto_channels": ["https://www.twitch.tv/caseoh_",
                                   "https://www.twitch.tv/big1"]})
    cfg = {"discovery": {"count": 2, "min_viewers": 2000}}
    before = gh.store[gh_run.SUBS_JSON]
    urls = gh_run.auto_discover(gh, cfg, top_fn=lambda n: TOP, probe_fn=lambda: [])
    assert urls == ["https://www.twitch.tv/caseoh_", "https://www.twitch.tv/big1"]
    assert gh.store[gh_run.SUBS_JSON] is before          # коммита не было


def test_auto_discover_disabled_and_no_gh():
    gh = FakeGH()
    assert gh_run.auto_discover(gh, {"discovery": {"enabled": False}},
                                top_fn=lambda n: TOP, probe_fn=lambda: []) == []
    assert gh_run.auto_discover(None, {}, top_fn=lambda n: TOP, probe_fn=lambda: []) == []
    # файл не тронут
    data = json.loads(gh.store[gh_run.SUBS_JSON][0].decode("utf-8"))
    assert "auto_channels" not in data


def test_twitch_top_streams_parses_gql(monkeypatch):
    class R:
        status_code = 200

        def json(self):
            return {"data": {"streams": {"edges": [
                {"node": {"broadcaster": {"login": "Big1", "displayName": "Big1"},
                          "title": "LIVE", "viewersCount": 12345,
                          "game": {"displayName": "GTA VI"}}},
                {"node": {"broadcaster": {"login": ""},
                          "title": "x", "viewersCount": 10, "game": None}},
            ]}}}

    import requests as rq
    captured = {}

    def fake_post(url, json=None, timeout=0, headers=None):
        captured["url"], captured["json"], captured["headers"] = url, json, headers
        return R()

    monkeypatch.setattr(rq, "post", fake_post)
    top = gh_run.twitch_top_streams(10)
    assert captured["url"] == gh_run.TWITCH_GQL
    assert captured["headers"]["Client-ID"] == gh_run.TWITCH_WEB_CLIENT_ID
    assert captured["json"]["variables"]["n"] == 10
    assert len(top) == 1                      # узел без login отброшен
    assert top[0] == {"login": "big1", "display": "Big1", "viewers": 12345,
                      "title": "LIVE", "game": "GTA VI", "language": ""}


def test_twitch_top_streams_never_raises(monkeypatch):
    import requests as rq

    def boom(*a, **k):
        raise OSError("net down")

    monkeypatch.setattr(rq, "post", boom)
    assert gh_run.twitch_top_streams() == []


def test_twitch_top_streams_limit_capped_at_30(monkeypatch):
    """Twitch отдаёт максимум 30 стримов за запрос (first <= 30)."""
    import requests as rq

    class R:
        status_code = 200

        def json(self):
            return {"data": {"streams": {"edges": []}}}

    ns = []

    def fake_post(url, json=None, timeout=0, headers=None):
        ns.append(json["variables"]["n"])
        return R()

    monkeypatch.setattr(rq, "post", fake_post)
    assert gh_run.twitch_top_streams(60) == []
    assert ns == [30]                        # 60 -> порезано до 30
    ns.clear()
    gh_run.twitch_top_streams(10)
    assert ns == [10]
    ns.clear()
    gh_run.twitch_top_streams()
    assert ns == [gh_run.DISCOVERY_TOP_FETCH]


def test_twitch_top_streams_gql_errors(monkeypatch):
    """Ошибки GQL (integrity и т.п.) -> [] и предупреждение, не падение."""
    import requests as rq

    class R:
        status_code = 200

        def json(self):
            return {"errors": [{"message": "failed integrity check"}],
                    "data": {"streams": None}}

    monkeypatch.setattr(rq, "post",
                        lambda *a, **k: R())
    assert gh_run.twitch_top_streams(30) == []
