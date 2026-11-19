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
