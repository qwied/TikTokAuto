"""StreamClipper на GitHub Actions: ВСЁ работает внутри репозитория.

Режимы (запускаются workflow-ами из .github/workflows):

  python gh_run.py --check-loop [--max-minutes 55]
      ПОСТОЯННОЕ слежение: задача живёт до ~55 минут и каждые ~25 секунд
      проверяет по subscriptions.json, кто в эфире (внутри неё же видит новые
      видео/клипы и ставит их в очередь нарезки). Кто-то стримит — запускает
      Monitor. В конце задача-эстафета запускает следующую — слежение идёт
      без пауз, а выход в эфир замечается за считанные секунды.

  python gh_run.py --check
      Разовая проверка (резервный вход и финальная проверка Monitor).

  python gh_run.py --monitor [--channel URL] [--max-minutes 280]
      Сессия слежения до ~4.7 часов: подключается к эфиру, читает чат,
      по хайпу режет клипы РОВНО 60 секунд в формате 9:16 (TikTok),
      с субтитрами из речи и баннером RubUP; каждый клип заливается в
      Releases репозитория и добавляется в clips.json — сайт показывает
      его и отдаёт на телефон. TikTok-автопостинг включается секретами:
      TIKTOK_CLIENT_KEY + TIKTOK_CLIENT_SECRET + TIKTOK_REFRESH_TOKEN.

  python gh_run.py --vod [--max-minutes 260]
      Нарезка готовых видео и клипов из очереди jobs.json (YouTube-видео,
      записи и клипы Twitch): сам находит интересные минуты (по громкости),
      режет окна по 60 с, вертикальный формат, субтитры, баннер, релиз,
      уведомление (ntfy/Web Push). Длинное видео (даже 9 часов) нарезается
      на ~100 клипов — по ~40 за запуск, остаток цепочкой продолжит следующий.

Один и тот же файл работает и в пакете (clipper/…), и в плоском
репозитории, где модули лежат рядом (GitHub-режим).
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from urllib.parse import quote

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                       # дерево с пакетом clipper/
    from clipper.links import dedup_key, parse_link, refs_from_config
    from clipper.notifier import Notifier
    from clipper.pipeline import Pipeline
    from clipper.stream_monitor import StreamMonitor
    from clipper.utils import load_config, setup_logging
except ImportError:                        # плоский репозиторий
    from links import dedup_key, parse_link, refs_from_config
    from notifier import Notifier
    from pipeline import Pipeline
    from stream_monitor import StreamMonitor
    from utils import load_config, setup_logging

try:                                       # нарезка готовых видео
    from clipper import banner as banner_mod
    from clipper.utils import detect_h264_encoder, ensure_dir
    from clipper import vod as vod_mod
except ImportError:
    import banner as banner_mod
    from utils import detect_h264_encoder, ensure_dir
    import vod as vod_mod

log = logging.getLogger("gh_run")

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
RELEASE_TAG = "auto"
CLIPS_JSON = "clips.json"
SUBS_JSON = "subscriptions.json"
PUSH_SUBS = "push_subscriptions.json"   # подписки Web Push (пишет сайт)
JOBS_JSON = "jobs.json"                 # очередь нарезки видео (пишет сайт/Check)
PROCESSED_JSON = "processed.json"       # уже нарезанные видео (не повторяем)
MAX_CLIPS = 60          # сколько клипов держать в clips.json
MAX_ASSETS = 60         # сколько файлов держать в релизе (старые удаляются)
PROBE_WAIT = 15         # секунд на проверку эфиров (быстрые API платформ)
CHECK_EVERY_S = 25      # пауза проверки эфиров внутри --check-loop
CHECK_LOOP_MIN = 55     # длина одной задачи --check-loop (потом эстафета)
VOD_MAX_WINDOWS = 100   # максимальный клипов из одного длинного видео
VOD_MAX_JOBS = 40       # сколько окон обрабатывает один запуск --vod


# --- GitHub REST -------------------------------------------------------------------------

class GitHub:
    """Минимальный клиент REST API: релизы, файлы, коммиты."""

    def __init__(self, repo, token):
        self.repo = str(repo or "").strip()
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "StreamClipper",
        })

    def req(self, method, url, ok=(200, 201, 204), timeout=120, **kw):
        full = url if url.startswith("http") else API + url
        r = self.s.request(method, full, timeout=timeout, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {url}: HTTP {r.status_code} "
                               f"{r.text[:300]}")
        return r

    # --- релизы (клипы) ---------------------------------------------------------------

    def release_id(self):
        r = self.s.get(f"{API}/repos/{self.repo}/releases/tags/{RELEASE_TAG}",
                       timeout=30)
        if r.status_code == 200:
            return r.json()["id"]
        if r.status_code != 404:
            raise RuntimeError(f"releases: HTTP {r.status_code} {r.text[:200]}")
        j = self.req("POST", f"/repos/{self.repo}/releases", json={
            "tag_name": RELEASE_TAG,
            "name": "Клипы StreamClipper",
            "body": "Автоматические клипы (60 сек, 9:16, субтитры, баннер).",
        }).json()
        return j["id"]

    def upload_asset(self, rel_id, path):
        name = os.path.basename(str(path))
        url = (f"{UPLOADS}/repos/{self.repo}/releases/{rel_id}"
               f"/assets?name={quote(name)}")
        with open(str(path), "rb") as f:
            r = self.s.post(url, data=f,
                            headers={"Content-Type": "video/mp4"},
                            timeout=3600)
        if r.status_code not in (201, 202):
            raise RuntimeError(f"upload {name}: HTTP {r.status_code} "
                               f"{r.text[:300]}")
        return r.json()

    def list_assets(self, rel_id):
        r = self.req("GET", f"/repos/{self.repo}/releases/{rel_id}/assets",
                     ok=(200,))
        return r.json()

    def delete_asset(self, asset_id):
        self.req("DELETE", f"/repos/{self.repo}/releases/assets/{asset_id}")

    # --- файлы репозитория (clips.json / subscriptions.json) ---------------------------

    def get_file(self, path):
        """(байты, sha) или (None, None), если файла нет."""
        r = self.s.get(f"{API}/repos/{self.repo}/contents/{quote(path)}",
                       timeout=30)
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            raise RuntimeError(f"get {path}: HTTP {r.status_code}")
        import base64
        j = r.json()
        return base64.b64decode(j.get("content") or ""), j.get("sha")

    def put_file(self, path, data: bytes, sha, message):
        import base64
        body = {"message": message,
                "content": base64.b64encode(data).decode("ascii")}
        if sha:
            body["sha"] = sha
        self.req("PUT", f"/repos/{self.repo}/contents/{quote(path)}",
                 json=body)


# --- clips.json ---------------------------------------------------------------------------

def empty_clips():
    return {"updated": "", "meta": {"mode": "github", "channels": []},
            "clips": []}


def load_clips_json_bytes(raw):
    if not raw:
        return empty_clips()
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return empty_clips()
    if not isinstance(data, dict):
        return empty_clips()
    data.setdefault("meta", {})
    data.setdefault("clips", [])
    return data


def clip_entry(video_path, asset_url, thumb_b64=None, dur=None, size=None,
               platform="twitch", display=None, title=""):
    name = os.path.basename(str(video_path))
    stem = os.path.splitext(name)[0]
    parts = stem.split("_")
    # 2026-11-19_12-15-00_nick -> канал после даты и времени
    chan = parts[2] if len(parts) > 2 else ""
    return {
        "id": stem,
        "file": name,
        "url": asset_url,
        "thumb": thumb_b64 or "",
        "size": int(size or 0),
        "duration": round(float(dur or 60), 1),
        "channel": chan,
        "display": display or chan,
        "platform": platform,
        "title": title or "",
        "created": int(os.path.getmtime(str(video_path))),
        "gh": True,
    }


def compact_thumb(video_path, workdir):
    """Маленькое превью для clips.json (data-uri, чтобы не плодить файлов)."""
    out = os.path.join(str(workdir), "gh_thumb.jpg")
    try:
        if os.path.exists(out):
            os.remove(out)
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
             "-ss", "2", "-i", os.path.abspath(str(video_path)),
             "-frames:v", "1", "-vf", "scale=270:-2", "-q:v", "7", out],
            capture_output=True, timeout=120)
        if p.returncode != 0 or not os.path.exists(out):
            return None
        with open(out, "rb") as f:
            raw = f.read()
        import base64
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    except Exception:
        return None
    finally:
        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass


def prune_assets(gh, rel_id, max_assets=MAX_ASSETS):
    """Старые файлы из релиза удаляем, чтобы репозиторий не пух."""
    try:
        assets = sorted(gh.list_assets(rel_id),
                        key=lambda a: a.get("created_at") or "")
        for a in assets[:-max_assets] if len(assets) > max_assets else []:
            try:
                gh.delete_asset(a["id"])
                log.info("Релиз: удалил старый файл %s", a.get("name"))
            except Exception as e:
                log.warning("Не удалить ассет %s: %s", a.get("name"), e)
    except Exception as e:
        log.warning("Не получилось прочитать релиз для чистки: %s", e)


class GitHubPublisher:
    """Получает готовый клип: релиз + запись в clips.json (коммит) + пуш."""

    def __init__(self, gh, workdir, max_clips=MAX_CLIPS, max_assets=MAX_ASSETS,
                 push=None):
        self.gh = gh
        self.workdir = str(workdir or ".")
        self.max_clips = int(max_clips)
        self.max_assets = int(max_assets)
        self.base_url = ""
        self.posted = 0
        self.push = push          # Notifier по секретам NOTIFY_* (или None)
        self.last_url = ""

    def notify_clip(self, video_path, caption="", platform="twitch",
                    display=None, title=""):
        try:
            self._publish(video_path, caption, platform=platform,
                          display=display, title=title)
            self.posted += 1
            cap = str(caption or "Клип готов")
            if self.last_url:
                cap += f"\n\nСкачать: {self.last_url}"
            if self.push is not None:
                try:
                    self.push.notify_clip(str(video_path), cap)
                except Exception:
                    log.exception("Пуш-уведомление не ушло (клип опубликован)")
            try:
                send_web_pushes(self.gh, {
                    "title": "Клип готов",
                    "body": cap[:400],
                    "url": self.last_url or "",
                })
            except Exception:
                log.exception("Web Push не удался (клип опубликован)")
            return True
        except Exception:
            log.exception("Не удалось опубликовать клип в GitHub")
            return False

    def _publish(self, video_path, caption="", platform="twitch",
                 display=None, title=""):
        rel = self.gh.release_id()
        log.info("Заливаю клип в Releases: %s", os.path.basename(video_path))
        asset = self.gh.upload_asset(rel, video_path)
        asset_name = asset.get("name") or os.path.basename(video_path)
        url = (f"https://github.com/{self.gh.repo}/releases/download/"
               f"{RELEASE_TAG}/{quote(asset_name)}")
        entry = clip_entry(video_path, url,
                           thumb_b64=compact_thumb(video_path, self.workdir),
                           size=os.path.getsize(str(video_path)),
                           platform=platform, display=display, title=title)
        raw, sha = self.gh.get_file(CLIPS_JSON)
        data = load_clips_json_bytes(raw)
        ids = {c.get("id") for c in data["clips"]}
        if entry["id"] not in ids:
            data["clips"] = [entry] + data["clips"]
        data["clips"] = data["clips"][:self.max_clips]
        data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime())
        self.gh.put_file(CLIPS_JSON, json.dumps(data, ensure_ascii=False,
                                                indent=1).encode("utf-8"),
                         sha, f"clip: {entry['file']}")
        log.info("clips.json обновлён: %s", url)
        self.last_url = url
        prune_assets(self.gh, rel, max_assets=self.max_assets)

    def update_channels_meta(self, channels):
        """Статусы каналов для сайта; коммит только при изменении."""
        sig = json.dumps([[c.get("name"), bool(c.get("live"))]
                          for c in channels], ensure_ascii=False)
        raw, sha = self.gh.get_file(CLIPS_JSON)
        data = load_clips_json_bytes(raw)
        if data["meta"].get("_live_sig") == sig and data["meta"].get("channels"):
            data["meta"]["channels"] = channels
            data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.gh.put_file(CLIPS_JSON,
                             json.dumps(data, ensure_ascii=False,
                                        indent=1).encode("utf-8"),
                             sha, "channels: refresh")
            return
        if data["meta"].get("_live_sig") == sig:
            return
        data["meta"]["channels"] = channels
        data["meta"]["_live_sig"] = sig
        data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.gh.put_file(CLIPS_JSON,
                         json.dumps(data, ensure_ascii=False,
                                    indent=1).encode("utf-8"),
                         sha, "channels: live status")


# --- подписки и проверка эфиров ------------------------------------------------------------

def load_subscriptions(path=SUBS_JSON):
    """Список ссылок из subscriptions.json (правится с сайта или на github.com)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [str(x).strip() for x in (data.get("channels") or [])
                if str(x).strip()]
    except (OSError, ValueError):
        return []


def merged_refs(cfg, subs):
    """subscriptions.json + channels из config.yaml -> без дублей."""
    refs, seen = [], set()
    for s in list(subs):
        r = parse_link(s, source="subs")
        if r and dedup_key(r) not in seen:
            seen.add(dedup_key(r))
            refs.append(r)
    for r in refs_from_config(cfg):
        if dedup_key(r) not in seen:
            seen.add(dedup_key(r))
            refs.append(r)
    return refs


# --- очередь нарезки готовых видео (jobs.json / processed.json) ----------------------------

def vod_key(job):
    """Уникальный ключ задачи в очереди (URL + границы окна)."""
    u = str((job or {}).get("url") or "")
    if (job or {}).get("kind") == "window":
        return f"{u}|{int(job.get('start') or 0)}-{int(job.get('end') or 0)}"
    return u


def load_json_state(gh, path):
    """(данные, sha) файла состояния; нет файла или битый JSON -> ({}, sha)."""
    try:
        raw, sha = gh.get_file(path)
    except Exception:
        return {}, None
    data = {}
    if raw:
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                data = {}
        except ValueError:
            data = {}
    return data, sha


def put_json_state(gh, path, data, sha, message):
    gh.put_file(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"),
                sha, message)


def live_refs(refs):
    """Ссылки для ПОСТОЯННОГО мониторинга эфиров (без клипов и записей)."""
    return [r for r in refs if getattr(r, "kind", "") in ("", None)]


def vod_refs(refs):
    """Ссылки на ГОТОВЫЕ видео/клипы: нарезаем, а не следим за эфиром.

    Прямые ссылки на YouTube-видео обрабатываются отдельно: это может быть
    И текущая трансляция (тогда её подхватывает Monitor), И обычное видео
    (тогда нарезаем). Решение принимается по статусу эфира в момент проверки.
    """
    return [r for r in refs if getattr(r, "kind", "") in ("clip", "vod")]


def enqueue_vod_jobs(gh, refs, live_keys):
    """Новые видео/клипы -> jobs.json. Возвращает число добавленных.

    live_keys — set(f'{platform}:{short_id}') каналов, которые СЕЙЧАС в эфире:
    прямую ссылку на YouTube-эфир не ставим в очередь, её пишет Monitor.
    """
    if gh is None:
        return 0
    jobs_data, jsha = load_json_state(gh, JOBS_JSON)
    proc_data, _psha = load_json_state(gh, PROCESSED_JSON)
    jobs = [j for j in (jobs_data.get("jobs") or []) if isinstance(j, dict)]
    done = set(proc_data.get("done") or [])
    queued = {vod_key(j) for j in jobs}
    added = 0
    for r in refs:
        kind = getattr(r, "kind", "")
        is_yt_video = (r.platform == "youtube" and getattr(r, "video_id", ""))
        if not kind and not is_yt_video:
            continue
        if kind == "clip":
            url, jkind = r.vod_url(), "clip"
        elif kind == "vod":
            url, jkind = r.vod_url(), "video"
        else:
            url, jkind = r.video_url or r.raw, "video"
        if f"{r.platform}:{r.short_id()}" in (live_keys or set()):
            continue                          # эфир идёт — записываем, не режем
        job = {"url": url, "kind": jkind, "fails": 0,
               "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        key = vod_key(job)
        if key in done or key in queued:
            continue
        jobs.append(job)
        queued.add(key)
        added += 1
        log.info("В очередь нарезки: %s (%s)", url, jkind)
    if added:
        jobs_data["jobs"] = jobs
        jobs_data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            put_json_state(gh, JOBS_JSON, jobs_data, jsha,
                           f"vod: +{added} в очередь нарезки")
        except Exception:
            log.exception("Не записать jobs.json — очередь не обновлена")
            return 0
        gh_workflow_dispatch("clip.yml")      # нарезка стартует сразу
    return added


def probe_live(refs, cfg, wait_s=PROBE_WAIT):
    """Одна проверка «кто в эфире» тем же кодом, что и постоянный монитор.

    Клипы и записи (kind clip/vod) в мониторинг эфиров не входят: их нарезает
    Clip video, а не проверяет как канал.
    """
    refs = live_refs(refs)
    yt = cfg.get("youtube", {}) or {}
    mon = StreamMonitor(channels=refs, quality="best", poll_twitch=2,
                        poll_youtube=3, yt_workers=3,
                        cookies_file=str(yt.get("cookies_file") or ""),
                        cookies_from_browser=str(yt.get("cookies_from_browser") or ""),
                        links_file=None, refresh_refs=lambda: refs,
                        on_live=lambda ch: None, on_offline=lambda ch: None)
    mon.start()
    time.sleep(max(10, int(wait_s)))
    # ВАЖНО: именно status_rows() — кортежи (platform, name, live, title, viewers).
    # mon.snapshot() отдаёт объекты ChannelState, и channels_meta_from_snapshot
    # падала с TypeError ровно в момент, когда кто-то СТРИМИТ: Check live умирал
    # и Monitor никогда не запускался.
    snap = mon.status_rows()
    mon.stop()
    return snap or []


def channels_meta_from_snapshot(refs, snap):
    """[(platform, name, live, title, viewers)] -> meta для сайта."""
    by_key = {}
    for r in refs:
        by_key[(r.platform, r.short_id())] = r
    out = []
    for platform, name, live, title, viewers in snap:
        r = by_key.get((platform, name))
        out.append({
            "name": name,
            "display": r.display() if r else name,
            "platform": platform,
            "live": bool(live),
            "viewers": int(viewers or 0),
            "title": title or "",
        })
    return out


def gh_workflow_monitor_in_progress():
    """True, если задача Monitor сейчас работает (через gh CLI).

    Свой собственный запуск (GITHUB_RUN_ID) НЕ считается: финальная проверка
    внутри monitor.yml идёт, пока её же задача ещё «in_progress», — и должна
    уметь запустить следующую сессию, если эфир продолжается.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return False
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--repo", repo, "--workflow", "monitor.yml",
             "--status", "in_progress", "--limit", "10", "--json", "databaseId"],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            log.warning("gh run list: %s", (out.stderr or "")[:200])
            return False
        runs = json.loads(out.stdout or "[]")
        my_id = str(os.environ.get("GITHUB_RUN_ID") or "")
        return any(str(r.get("databaseId")) != my_id for r in runs)
    except Exception as e:
        log.warning("gh run list не удался: %s", e)
        return False


def gh_workflow_dispatch(workflow):
    """Запустить workflow через gh CLI (SC_PAT — от встроенного GITHUB_TOKEN
    GitHub молча отказывается запускать workflow из workflow)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        log.warning("GITHUB_REPOSITORY не задан — не запустить %s", workflow)
        return False
    cmd = ["gh", "workflow", "run", workflow, "--repo", repo]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            log.warning("gh workflow run %s: %s", workflow, (out.stderr or "")[:300])
            return False
        log.info("Workflow запущен: %s", workflow)
        return True
    except Exception as e:
        log.warning("gh workflow run %s не удался: %s", workflow, e)
        return False


def gh_trigger_monitor(channel_url):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        log.warning("GITHUB_REPOSITORY не задан — запускаю Monitor локально?")
        return False
    cmd = ["gh", "workflow", "run", "monitor.yml", "--repo", repo,
           "-f", f"channel={channel_url or ''}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            log.warning("gh workflow run: %s", (out.stderr or "")[:300])
            return False
        log.info("Monitor запущен: %s", channel_url or "(все каналы)")
        return True
    except Exception as e:
        log.warning("gh workflow run не удался: %s", e)
        return False


def env_gh():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not (repo and token):
        return None
    return GitHub(repo, token)


def apply_tiktok_secrets(cfg):
    """TikTok из секретов Actions: env -> token_file (обновится сам при посте)."""
    tk = cfg.setdefault("tiktok", {})
    ck = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    cs = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    rt = os.environ.get("TIKTOK_REFRESH_TOKEN", "").strip()
    if not (ck and cs and rt):
        tk["enabled"] = False
        log.info("TikTok-секреты не заданы — автопостинг выключен "
                 "(README: TIKTOK_CLIENT_KEY / CLIENT_SECRET / REFRESH_TOKEN)")
        return
    tf = os.path.join(os.path.dirname(cfg["_config_path"]),
                      str(tk.get("token_file") or "tiktok_tokens.json"))
    if not os.path.exists(tf):
        with open(tf, "w", encoding="utf-8") as f:
            json.dump({"access_token": "", "refresh_token": rt,
                       "expires_at": 0, "open_id": "", "scope": ""},
                      f, ensure_ascii=False)
    tk.update(enabled=True, mode="api", client_key=ck, client_secret=cs,
              token_file=os.path.basename(tf))
    log.info("TikTok-автопостинг включён (секреты GitHub, режим API)")


# --- режимы ---------------------------------------------------------------------------------

def run_check(max_probe=PROBE_WAIT):
    """Одна проверка: статусы -> clips.json, новые видео -> очередь, эфир -> Monitor."""
    setup_logging()
    cfg = load_config("config.yaml")
    cfg["_config_path"] = os.path.abspath("config.yaml")
    subs = load_subscriptions()
    refs = merged_refs(cfg, subs)
    gh = env_gh()
    if not refs:
        log.info("Каналов нет (subscriptions.json пуст) — проверять нечего")
        return 0
    snap = probe_live(refs, cfg, wait_s=max_probe)
    meta = channels_meta_from_snapshot(refs, snap)
    live = [m for m in meta if m["live"]]
    names = ", ".join(m["display"] for m in meta) or "—"
    log.info("Каналы (%d): %s", len(meta), names)
    for m in live:
        log.info("В ЭФИРЕ: %s — запускаю слежение", m["display"])
    if gh:
        try:
            pub = GitHubPublisher(gh, workdir=".")
            pub.update_channels_meta(meta)
        except Exception:
            log.exception("Не удалось обновить clips.json")
        try:
            # готовые видео/клипы из subscriptions.json -> очередь нарезки;
            # прямые ссылки на YouTube-эфир не ставим в очередь (их пишет Monitor)
            keys = {f"{m['platform']}:{m['name']}" for m in live}
            enqueue_vod_jobs(gh, refs, keys)
        except Exception:
            log.exception("Очередь нарезки: не удалось обновить")
    if live:
        if gh_workflow_monitor_in_progress():
            log.info("Monitor уже работает — не запускаю второй")
        else:
            gh_trigger_monitor(live[0]["name"])
    return 0


def run_check_loop(max_minutes=CHECK_LOOP_MIN):
    """ПОСТОЯННОЕ слежение: проверка каждые ~CHECK_EVERY_S секунд до дедлайна.

    Одна задача Actions живёт до ~55 минут, затем эстафета запускает
    следующую. Так выход стримера в эфир замечается за секунды, а не через
    «каждые 5-20 минут» (cron GitHub сам по себе не бывает чаще 5 минут).
    """
    setup_logging()
    deadline = time.time() + max(1, int(max_minutes)) * 60
    n = 0
    log.info("Постоянное слежение: проверка каждые %d c, %d мин, потом эстафета",
             CHECK_EVERY_S, max_minutes)
    while time.time() < deadline:
        started = time.time()
        try:
            run_check()
            n += 1
        except Exception:
            log.exception("Проверка не удалась — повторю после паузы")
        rest = CHECK_EVERY_S - (time.time() - started)
        if rest > 0:
            time.sleep(rest)
    log.info("Смена: %d проверок за %d мин — запускаю следующую задачу",
             n, max_minutes)
    return 0


# --- уведомления из секретов ---------------------------------------------------------------

def _vapid_key_for_pywebpush(key):
    """pywebpush ждёт ключ в base64url-DER; PEM приводим к нему."""
    k = str(key or "").strip()
    if not k.startswith("-----BEGIN"):
        return k
    body = "".join(line for line in k.splitlines()
                   if not line.startswith("-----"))
    import base64 as _b64
    der = _b64.b64decode(body)
    return _b64.urlsafe_b64encode(der).rstrip(b"=").decode("ascii")


def send_web_pushes(gh, payload, pusher=None):
    """Web Push всем подпискам сайта из push_subscriptions.json.

    Подписки пишет сайт (кнопка «Пуши на устройство» — push_subscriptions.json
    в репозитории). Подписки с ошибкой 404/410 (протухшие) вычищаются.
    Требуются секреты VAPID_PRIVATE_KEY (PEM) и, опционально, VAPID_SUB.
    pusher — точка подмены в тестах (pywebpush.webpush).
    """
    try:
        raw, sha = gh.get_file(PUSH_SUBS)
    except Exception:
        log.exception("Не прочитать %s", PUSH_SUBS)
        return
    if raw is None:
        return                                  # подписок ещё нет — тихо выходим
    try:
        subs = [s for s in (json.loads(raw.decode("utf-8")).get("subscriptions") or [])
                if isinstance(s, dict) and s.get("endpoint")]
    except ValueError:
        log.warning("%s не читается — пропускаю", PUSH_SUBS)
        return
    if not subs:
        return
    priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not priv:
        log.info("VAPID_PRIVATE_KEY не задан — Web Push пропущен")
        return
    priv = _vapid_key_for_pywebpush(priv)
    if pusher is None:
        try:
            from pywebpush import webpush as pusher
        except ImportError:
            log.info("pywebpush не установлен — Web Push пропущен")
            return
    claims = {"sub": os.environ.get("VAPID_SUB",
                                    "mailto:streamclipper@users.noreply.github.com")}
    kept, changed = [], False
    for s in subs:
        try:
            pusher(s, data=json.dumps(payload, ensure_ascii=False),
                   vapid_private_key=priv, vapid_claims=claims)
            kept.append(s)
            log.info("Web Push: отправлено …%s", s["endpoint"][-14:])
        except Exception as e:
            code = getattr(e, "status_code", 0) or \
                getattr(getattr(e, "response", None), "status_code", 0)
            if code in (404, 410):
                changed = True
                log.info("Web Push: подписка протухла (%s) — убираю", code)
            else:
                log.warning("Web Push: %s", str(e)[:200])
                kept.append(s)
    if changed:
        gh.put_file(PUSH_SUBS,
                    json.dumps({"subscriptions": kept}, ensure_ascii=False).encode("utf-8"),
                    sha, "push: почистить протухшие подписки")


def notify_cfg_from_env(cfg):
    """Секреты NOTIFY_* -> конфиг уведомлений (GitHub-режим).

    Поддержка: Telegram, ntfy, Bark, Pushover, webhook — провайдер "all"
    (каждый клип уходит во все заполненные каналы). Возвращает список
    настроенных провайдеров; без секретов уведомления не включаются.
    """
    n = cfg.setdefault("notify", {})
    tg_tok = os.environ.get("NOTIFY_TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("NOTIFY_TELEGRAM_CHAT_ID", "").strip()
    if tg_tok and tg_chat:
        tg = n.setdefault("telegram", {})
        tg["bot_token"], tg["chat_id"] = tg_tok, tg_chat
    topic = os.environ.get("NOTIFY_NTFY_TOPIC", "").strip()
    if topic:
        nt = n.setdefault("ntfy", {})
        nt["topic"] = topic
        srv = os.environ.get("NOTIFY_NTFY_SERVER", "").strip()
        if srv:
            nt["server"] = srv
    bark = os.environ.get("NOTIFY_BARK_DEVICE_KEY", "").strip()
    if bark:
        n.setdefault("bark", {})["device_key"] = bark
    po_user = os.environ.get("NOTIFY_PUSHOVER_USER_KEY", "").strip()
    po_tok = os.environ.get("NOTIFY_PUSHOVER_API_TOKEN", "").strip()
    if po_user and po_tok:
        po = n.setdefault("pushover", {})
        po["user_key"], po["api_token"] = po_user, po_tok
    hook = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if hook:
        n.setdefault("webhook", {})["url"] = hook
    providers = Notifier(n).configured_providers()
    if providers:
        n["provider"] = "all"
    return providers


def run_monitor(channel="", max_minutes=280):
    setup_logging()
    base = os.path.dirname(os.path.abspath(__file__))
    cfg = load_config(os.path.join(base, "config.yaml"))
    cfg["_config_path"] = os.path.join(base, "config.yaml")

    subs = load_subscriptions(os.path.join(base, SUBS_JSON))
    # клипы и записи Twitch — не эфиры: их нарезает Clip video, а не Monitor
    refs = live_refs(merged_refs(cfg, subs))
    if channel:
        r = parse_link(channel, source="arg")
        if r:
            keys = {dedup_key(x) for x in refs}
            if dedup_key(r) not in keys:
                refs.append(r)
    if not refs:
        log.error("Каналов нет — добавьте ссылки в subscriptions.json "
                  "(сайт → Каналы, или правьте файл на github.com)")
        return 2

    # CI-настройки: без веб-сервера и Telegram-бота; TikTok из секретов.
    cfg.setdefault("server", {})["enabled"] = False
    cfg.setdefault("notify", {})["provider"] = "none"
    providers = notify_cfg_from_env(cfg)      # секреты NOTIFY_* перекрывают none
    if providers:
        log.info("Уведомления включены: %s", ", ".join(providers))
    apply_tiktok_secrets(cfg)

    gh = env_gh()
    if gh is None:
        log.error("Нет GITHUB_REPOSITORY/GH_TOKEN — это режим GitHub Actions. "
                  "На компьютере запускайте main.py.")
        return 2
    # Уже идёт другая сессия слежения (например, резервный крон запустил
    # вторую) — не пишем эфир ДВАЖДЫ: выходим сразу.
    if gh_workflow_monitor_in_progress():
        log.info("Monitor уже работает (другая сессия) — не запускаю вторую "
                 "запись эфира")
        return 0
    push = Notifier(cfg.get("notify") or {}) if providers else None
    pub = GitHubPublisher(gh, workdir=base, push=push)

    log.info("Первичная проверка эфиров (%d с)...", PROBE_WAIT)
    snap = probe_live(refs, cfg)
    meta = channels_meta_from_snapshot(refs, snap)
    if not any(m["live"] for m in meta):
        log.info("Никто не в эфире — завершаю (Check запустит Monitor, "
                 "когда появится эфир)")
        try:
            pub.update_channels_meta(meta)
        except Exception:
            pass
        return 0

    # ГЛАВНЫЙ ФИКС: Pipeline берёт каналы ТОЛЬКО из config.yaml (channels) и
    # links.txt — в GitHub-режиме оба пусты, и он следил за 0 каналами
    # (в логе это выглядело как «Слежу за 0 канал(ами): список пуст», а клипов
    # не было НИКОГДА). Прокидываем ссылки из subscriptions.json в конфиг:
    # refs_from_config(cfg) прочитает их и Pipeline начнёт следить и писать.
    cfg["channels"] = [getattr(r, "raw", "") for r in refs if getattr(r, "raw", "")]
    log.info("Pipeline получает каналы: %s", ", ".join(cfg["channels"]) or "—")

    class _Notifier:
        """Мост: Pipeline -> релиз + clips.json."""
        base_url = ""
        notify_clip = pub.notify_clip

        @staticmethod
        def file_url(video_path):
            """Ссылка на файл в Releases (появится после заливки)."""
            name = quote(os.path.basename(str(video_path)))
            return (f"https://github.com/{gh.repo}/releases/download/"
                    f"{RELEASE_TAG}/{name}")

    pl = Pipeline(cfg, _Notifier())
    started = time.time()
    pl.start()
    deadline = started + max(15, int(max_minutes)) * 60
    offline_strikes = 0
    try:
        while time.time() < deadline:
            time.sleep(60)
            # Живой статус сессии раз в минуту: видно в логе Actions,
            # кто в записи и сколько клипов сделано. Заодно выходим сразу,
            # как только все каналы оффлайн 3 проверки подряд (не жжём
            # минуты раннера до конца 280-минутного лимита).
            try:
                rows = pl.monitor.status_rows() or []
                any_live = any(bool(r[2]) for r in rows)
                act = len(getattr(pl, "active", {}) or {})
                log.info("Статус: в записи %d, клипов за сессию %d, эфир идёт: %s",
                         act, int(getattr(pl, "_clips_made", 0) or 0),
                         "да" if any_live else "нет")
                if not any_live and not act:
                    offline_strikes += 1
                    if offline_strikes >= 3:
                        log.info("Все каналы оффлайн — сессия завершена раньше лимита")
                        break
                    log.info("Похоже, эфир закончился (%d/3) — проверю ещё раз",
                             offline_strikes)
                else:
                    offline_strikes = 0
            except Exception:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Сессия слежения закончена (%.0f мин) — останавливаюсь",
                 (time.time() - started) / 60)
        pl.stop()
    return 0


# --- нарезка готовых видео -------------------------------------------------------------------

def _vod_display(url):
    """Короткое имя для файлов/карточек из URL клипа/видео."""
    u = str(url or "").rstrip("/")
    tail = u.split("/")[-1].split("?")[0] or "video"
    return tail[:40]


def run_vod(max_minutes=260, max_jobs=VOD_MAX_JOBS):
    """Нарезка готовых видео и клипов из очереди jobs.json (workflow Clip video).

    Длинное видео (даже 9 часов) разворачивается в ~100 окон по 60 с и
    обрабатывается по ~max_jobs за запуск; если после запуска остались куски,
    пишем vod_remaining.txt="1" — workflow цепочкой запустит следующий.
    Каждый готовый клип: релиз + clips.json + ntfy/Web Push уведомление.
    """
    setup_logging()
    base = os.path.dirname(os.path.abspath(__file__))
    cfg = load_config(os.path.join(base, "config.yaml"))
    cfg["_config_path"] = os.path.join(base, "config.yaml")
    gh = env_gh()
    if gh is None:
        log.error("Нет GITHUB_REPOSITORY/GH_TOKEN — это режим GitHub Actions")
        return 2
    cfg.setdefault("server", {})["enabled"] = False
    cfg.setdefault("notify", {})["provider"] = "none"
    providers = notify_cfg_from_env(cfg)
    if providers:
        log.info("Уведомления включены: %s", ", ".join(providers))
    push = Notifier(cfg.get("notify") or {}) if providers else None
    pub = GitHubPublisher(gh, workdir=base, push=push)

    enc_name, vcodec_args = detect_h264_encoder(str((cfg.get("processing") or {}).get("encoder", "auto")))
    log.info("Кодировщик финальной обработки: %s", enc_name)
    banner_path = banner_mod.resolve_file(cfg.get("banner") or {}, base)
    if banner_path:
        log.info("Баннер: %s (в каждой нарезке)", os.path.basename(banner_path))

    cookies = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    if not cookies:
        for cand in ("cookies.txt", os.path.join(base, "cookies.txt")):
            if os.path.isfile(cand):
                cookies = cand
                break
    if cookies:
        log.info("Cookies для YouTube: %s", cookies)

    workdir = ensure_dir(os.path.join(base, "temp", "vod"))
    clips_dir = ensure_dir(os.path.join(base, "clips"))

    def save(path, jobs_list=None, done_set=None, msg="vod: очередь"):
        """Читаем-меняем-пишем: кто-то мог коммитнуть файл параллельно."""
        data, sha = load_json_state(gh, path)
        if jobs_list is not None:
            data["jobs"] = jobs_list
            data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if done_set is not None:
            data["done"] = sorted(done_set)[-5000:]
            data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        put_json_state(gh, path, data, sha, msg)

    jobs_data, _ = load_json_state(gh, JOBS_JSON)
    proc_data, _ = load_json_state(gh, PROCESSED_JSON)
    jobs = [j for j in (jobs_data.get("jobs") or []) if isinstance(j, dict)]
    done = set(proc_data.get("done") or [])
    log.info("Очередь нарезки: %d задач, уже сделано %d", len(jobs), len(done))
    if not jobs:
        log.info("Очередь пуста — нарезать нечего. Ссылки на видео кидаются на сайт "
                 "(Каналы) и попадают сюда через Check live")
        return 0

    deadline = time.time() + max(10, int(max_minutes)) * 60
    made = 0
    while jobs and made < max_jobs and time.time() < deadline - 12 * 60:
        job = jobs[0]
        key = vod_key(job)
        try:
            url = str(job.get("url") or "")
            display = str(job.get("display") or _vod_display(url))
            platform = "twitch" if "twitch." in url else "youtube"
            if job.get("kind") == "video" and not job.get("windows"):
                # ещё не размечено: ищем интересные моменты по громкости звука
                title, wins = vod_mod.expand_video(
                    url, workdir, cookies_file=cookies,
                    max_windows=int(os.environ.get("VOD_MAX_WINDOWS", VOD_MAX_WINDOWS)))
                job["display"] = title or display
                job["windows"] = [[int(a), int(b)] for a, b in wins]
                log.info("%s: %d интересных моментов", job["display"], len(job["windows"]))
                if not job["windows"]:
                    jobs.pop(0)
                    done.add(key)
                save(JOBS_JSON, jobs_list=jobs, msg="vod: разметка видео")
                continue
            if job.get("kind") == "video":
                start, end = job["windows"][0]
                src = vod_mod.download_section(url, start, end,
                                               "vod_part.mp4", workdir,
                                               cookies_file=cookies)
            else:                                       # готовый клип Twitch
                src = vod_mod.download_clip_full(url, "vod_clip.mp4", workdir,
                                                 cookies_file=cookies)
            out = vod_mod.process_clip(src, clips_dir, workdir, cfg,
                                       display, platform,
                                       vcodec_args=vcodec_args,
                                       banner_path=banner_path)
            size_mb = os.path.getsize(out) / 1e6
            caption = f"Клип готов: {display} ({platform}, нарезка видео) — {size_mb:.1f} МБ"
            if pub.notify_clip(out, caption, platform=platform,
                               display=display, title=display):
                made += 1
            # кусок обработан: сдвигаем очередь
            for f in (src, os.path.join(workdir, "vod_9x16.mp4"),
                      os.path.join(workdir, "vod_final.mp4"),
                      os.path.join(workdir, "vod_raw.mp4")):
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except OSError:
                    pass
            if job.get("kind") == "video":
                job["windows"].pop(0)
                if not job["windows"]:
                    jobs.pop(0)
                    done.add(key)
            else:
                jobs.pop(0)
                done.add(key)
            save(JOBS_JSON, jobs_list=jobs, msg="vod: очередь после клипа")
            save(PROCESSED_JSON, done_set=done, msg="vod: отмечаю сделанное")
        except Exception as e:
            job["fails"] = int(job.get("fails") or 0) + 1
            log.exception("Кусок не обработан (%s/3): %s", job["fails"], e)
            if job["fails"] >= 3:
                log.error("Не осилил после 3 попыток — пропускаю: %s", job.get("url"))
                jobs.pop(0)
                done.add(key)            # чтобы не зациклиться навсегда
            save(JOBS_JSON, jobs_list=jobs, msg="vod: очередь после ошибки")
            save(PROCESSED_JSON, done_set=done, msg="vod: отмечаю пропущенное")

    remaining = len(jobs)
    flag = "1" if remaining else "0"
    try:
        with open(os.path.join(base, "vod_remaining.txt"), "w") as f:
            f.write(flag)
    except OSError:
        pass
    log.info("Готово за этот запуск: %d клипов. Осталось в очереди: %d (%s)",
             made, remaining, "продолжу цепочкой" if remaining else "всё нарезано")
    return 0


def main():
    ap = argparse.ArgumentParser(description="StreamClipper на GitHub Actions")
    ap.add_argument("--check", action="store_true",
                    help="разовая проверка эфиров (резервный вход)")
    ap.add_argument("--check-loop", action="store_true",
                    help="постоянное слежение: проверка каждые ~25 сек до дедлайна")
    ap.add_argument("--monitor", action="store_true",
                    help="сессия слежения: запись, клипы 9:16, релиз, clips.json")
    ap.add_argument("--vod", action="store_true",
                    help="нарезка готовых видео/клипов из очереди jobs.json")
    ap.add_argument("--channel", default="", help="канал (для --monitor)")
    ap.add_argument("--max-minutes", type=int, default=280,
                    help="лимит времени текущего режима, минут")
    args = ap.parse_args()
    if args.check:
        return run_check()
    if args.check_loop:
        return run_check_loop(max_minutes=args.max_minutes)
    if args.monitor:
        return run_monitor(channel=args.channel, max_minutes=args.max_minutes)
    if args.vod:
        return run_vod(max_minutes=args.max_minutes)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
