#!/usr/bin/env python3
"""Превью-копии последних клипов релиза -> preview/ (Pages) + ремонт clips.json.

Запускается workflow preview.yml (вручную) или локально:
  GH_TOKEN=... python make_previews.py --limit 12

Что делает:
  1. Берёт последние N mp4-ассетов релиза auto.
  2. Для каждого собирает 480p-копию (ffmpeg) и кладёт в preview/ —
     сайт отдаёт её same-origin, iPhone играет сразу (файлы Releases
     помечены Content-Disposition: attachment и без CORS: Safari их
     не играет ни через <video>, ни через fetch).
  3. Прописывает preview-URL в clips.json и ВОССТАНАВЛИВАЕТ потерянные
     записи: клип есть в релизе, но его нет в clips.json (гонка 409).
  4. Подчищает preview/: остаются только последние PREVIEW_KEEP файлов.
"""
import argparse
import base64
import json
import os
import subprocess
import tempfile
import time

import requests

API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY") or "qwied/TikTokAuto"
RELEASE_TAG = "auto"
CLIPS_JSON = "clips.json"
PREVIEW_DIR = "preview"
PREVIEW_KEEP = 12
PAGES_BASE = "https://qwied.github.io/TikTokAuto"

log = lambda *a: print("[previews]", *a, flush=True)


def session():
    tok = (os.environ.get("GH_TOKEN") or "").strip()
    if not tok:
        raise SystemExit("Нужен GH_TOKEN")
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "StreamClipper-previews",
    })
    return s


def req(s, method, url, ok=(200, 201, 204), **kw):
    r = s.request(method, url, timeout=kw.pop("timeout", 600), **kw)
    if r.status_code not in ok:
        raise RuntimeError(f"{method} {url}: HTTP {r.status_code} {r.text[:200]}")
    return r


def get_file(s, path):
    r = s.get(f"{API}/repos/{REPO}/contents/{path}", timeout=30)
    if r.status_code == 404:
        return None, None
    if r.status_code != 200:
        raise RuntimeError(f"get {path}: HTTP {r.status_code}")
    j = r.json()
    return base64.b64decode(j.get("content") or ""), j.get("sha")


def put_file(s, path, data, sha, message):
    body = {"message": message,
            "content": base64.b64encode(data).decode("ascii")}
    if sha:
        body["sha"] = sha
    req(s, "PUT", f"{API}/repos/{REPO}/contents/{path}", json=body)


def put_file_retry(s, path, data, message, tries=5):
    last = None
    for attempt in range(tries):
        try:
            _raw, sha = get_file(s, path)
            put_file(s, path, data, sha, message)
            return
        except RuntimeError as e:
            last = e
            if "409" not in str(e):
                raise
            log(f"409 на {path} — повторяю ({attempt + 1}/{tries})")
            time.sleep(2 + attempt * 3)
    raise last


def list_dir(s, path):
    r = s.get(f"{API}/repos/{REPO}/contents/{path}", timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"list {path}: HTTP {r.status_code}")
    return r.json()


def delete_file(s, path, sha, message):
    req(s, "DELETE", f"{API}/repos/{REPO}/contents/{path}",
        json={"message": message, "sha": sha})


def ffprobe_duration(path):
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], capture_output=True, timeout=60)
        return round(float(p.stdout.decode().strip() or 0), 1) or 45.0
    except Exception:
        return 45.0


def make_preview(src, dst):
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
         "-i", src, "-vf", "scale=480:-2", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "28",
         "-maxrate", "900k", "-bufsize", "1800k",
         "-c:a", "aac", "-b:a", "80k", "-movflags", "+faststart", dst],
        capture_output=True, timeout=600)
    if p.returncode != 0 or not os.path.exists(dst) \
            or os.path.getsize(dst) < 100000:
        raise RuntimeError("ffmpeg превью: " + (p.stderr or b"")[-300:].decode("utf-8", "replace"))


def entry_from_asset(asset, preview_url, dur):
    name = asset["name"]
    stem = os.path.splitext(name)[0]
    parts = stem.split("_")
    chan = parts[2] if len(parts) > 2 else ""
    return {
        "id": stem,
        "file": name,
        "url": asset.get("browser_download_url") or
               f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/{name}",
        "preview": preview_url,
        "thumb": "",
        "size": int(asset.get("size") or 0),
        "duration": dur,
        "channel": chan,
        "display": chan,
        "platform": "twitch",
        "title": "",
        "score": 0.0,
        "hashtags": [],
        "caption": "",
        "subs": {},
        "src": "",
        "start": 0,
        "created": int(time.mktime(time.strptime(
            asset["created_at"][:19], "%Y-%m-%dT%H:%M:%S"))),
        "gh": True,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    args = ap.parse_args()
    s = session()

    rel = req(s, "GET", f"{API}/repos/{REPO}/releases/tags/{RELEASE_TAG}").json()
    assets = [a for a in (rel.get("assets") or [])
              if str(a.get("name") or "").endswith(".mp4")]
    assets.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    todo = assets[:max(1, args.limit)]
    log(f"релиз {RELEASE_TAG}: {len(assets)} mp4, берём последние {len(todo)}")

    existing_previews = {
        (f.get("name") or "") for f in (list_dir(s, PREVIEW_DIR) or [])
    }

    raw, _sha = get_file(s, CLIPS_JSON)
    data = json.loads(raw.decode("utf-8"))
    clips = data.setdefault("clips", [])
    by_id = {c.get("id"): c for c in clips if isinstance(c, dict)}

    made, restored = 0, 0
    dirty = False
    for asset in todo:
        name = asset["name"]
        preview_url = f"{PAGES_BASE}/{PREVIEW_DIR}/{name}"
        entry = by_id.get(os.path.splitext(name)[0])
        if entry is None:
            log(f"в clips.json нет записи — восстанавливаю: {name}")
            entry = entry_from_asset(asset, preview_url, 45.0)
            clips.insert(0, entry)
            by_id[entry["id"]] = entry
            restored += 1
            dirty = True
        if entry.get("preview") != preview_url:
            entry["preview"] = preview_url
            dirty = True
        if name in existing_previews:
            continue
        log(f"превью для {name} ({round(asset.get('size') / 1e6, 1)} МБ)…")
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, name)
            r = s.get(asset["browser_download_url"], timeout=1800, stream=True)
            if r.status_code != 200:
                raise RuntimeError(f"скачивание {name}: HTTP {r.status_code}")
            with open(src, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
            new_dur = ffprobe_duration(src)
            if new_dur != entry.get("duration"):
                entry["duration"] = new_dur
                dirty = True
            dst = os.path.join(td, "preview.mp4")
            make_preview(src, dst)
            with open(dst, "rb") as f:
                data_bytes = f.read()
        put_file_retry(s, f"{PREVIEW_DIR}/{name}", data_bytes,
                       f"preview: {name}")
        made += 1
        dirty = True
        log(f"  ок: {round(len(data_bytes) / 1e6, 1)} МБ -> {preview_url}")

    # чистим старые превью: остаются только последние PREVIEW_KEEP по имени
    files = list_dir(s, PREVIEW_DIR) or []
    files.sort(key=lambda x: str(x.get("name") or ""))
    for f in files[:-PREVIEW_KEEP] if len(files) > PREVIEW_KEEP else []:
        try:
            delete_file(s, f["path"], f["sha"],
                        f"preview prune: {f.get('name')}")
            log(f"  старое превью удалено: {f.get('name')}")
        except Exception as e:
            log(f"  не удалилось {f.get('name')}: {e}")

    if not dirty:
        log("лента в порядке, превью на месте — записей не трогаю")
        return

    for _ in range(5):
        try:
            raw, sha = get_file(s, CLIPS_JSON)
            fresh = json.loads(raw.decode("utf-8"))
            have = {c.get("id") for c in fresh.setdefault("clips", [])
                    if isinstance(c, dict)}
            for c in clips:
                if c.get("id") in have:
                    continue
                fresh["clips"].insert(0, c)
                have.add(c.get("id"))
            # свежие записи сверху + preview уже вписан в объекты clips —
            # но fresh содержит СВОИ объекты: вливаем preview отдельно
            for c in fresh["clips"]:
                ent = by_id.get(c.get("id"))
                if ent and ent.get("preview"):
                    c["preview"] = ent["preview"]
                if ent and ent.get("duration"):
                    c["duration"] = ent["duration"]
            fresh["clips"] = fresh["clips"][:60]
            fresh["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime())
            put_file(s, CLIPS_JSON,
                     json.dumps(fresh, ensure_ascii=False, indent=1).encode(),
                     sha, f"previews: +{made} превью, восстановлено {restored}")
            break
        except RuntimeError as e:
            if "409" not in str(e) or _ == 4:
                raise
            log("409 на clips.json — перечитываю")
            time.sleep(2 + _ * 3)

    log(f"готово: превью собрано {made}, записей восстановлено {restored}")


if __name__ == "__main__":
    main()
