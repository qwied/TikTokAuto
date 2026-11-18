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
