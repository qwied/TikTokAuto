"""StreamClipper на GitHub Actions: ВСЁ работает внутри репозитория.

Два режима (запускаются workflow-ами из .github/workflows):

  python gh_run.py --check
      Каждые 5 минут: проверяет по subscriptions.json, кто в эфире,
      обновляет статусы в clips.json (их показывает сайт) и, если кто-то
      стримит, а задачи Monitor ещё нет — запускает её (gh workflow run).

  python gh_run.py --monitor [--channel URL] [--max-minutes 280]
      Сессия слежения до ~4.7 часов: подключается к эфиру, читает чат,
      по хайпу режет клипы РОВНО 60 секунд в формате 9:16 (TikTok),
      с субтитрами из речи и баннером RubUP; каждый клип заливается в
      Releases репозитория и добавляется в clips.json — сайт показывает
      его и отдаёт на телефон. TikTok-автопостинг включается секретами:
      TIKTOK_CLIENT_KEY + TIKTOK_CLIENT_SECRET + TIKTOK_REFRESH_TOKEN.

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

log = logging.getLogger("gh_run")

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
RELEASE_TAG = "auto"
CLIPS_JSON = "clips.json"
SUBS_JSON = "subscriptions.json"
PUSH_SUBS = "push_subscriptions.json"   # подписки Web Push (пишет сайт)
MAX_CLIPS = 60          # сколько клипов держать в clips.json
MAX_ASSETS = 60         # сколько файлов держать в релизе (старые удаляются)
PROBE_WAIT = 45         # секунд на первую проверку эфиров


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


def clip_entry(video_path, asset_url, thumb_b64=None, dur=None, size=None):
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
        "display": chan,
        "platform": "twitch",
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

    def notify_clip(self, video_path, caption=""):
        try:
            self._publish(video_path, caption)
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

    def _publish(self, video_path, caption=""):
        rel = self.gh.release_id()
        log.info("Заливаю клип в Releases: %s", os.path.basename(video_path))
        asset = self.gh.upload_asset(rel, video_path)
        asset_name = asset.get("name") or os.path.basename(video_path)
        url = (f"https://github.com/{self.gh.repo}/releases/download/"
               f"{RELEASE_TAG}/{quote(asset_name)}")
        entry = clip_entry(video_path, url,
                           thumb_b64=compact_thumb(video_path, self.workdir),
                           size=os.path.getsize(str(video_path)))
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


def probe_live(refs, cfg, wait_s=PROBE_WAIT):
    """Одна проверка «кто в эфире» тем же кодом, что и постоянный монитор."""
    yt = cfg.get("youtube", {}) or {}
    mon = StreamMonitor(channels=refs, quality="best", poll_twitch=2,
                        poll_youtube=3, yt_workers=3,
                        cookies_file=str(yt.get("cookies_file") or ""),
                        cookies_from_browser=str(yt.get("cookies_from_browser") or ""),
                        links_file=None, refresh_refs=lambda: refs,
                        on_live=lambda ch: None, on_offline=lambda ch: None)
    mon.start()
    time.sleep(max(10, int(wait_s)))
    snap = mon.snapshot()
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
    """True, если задача Monitor сейчас работает (через gh CLI)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return False
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--repo", repo, "--workflow", "monitor.yml",
             "--status", "in_progress", "--limit", "1", "--json", "databaseId"],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            log.warning("gh run list: %s", (out.stderr or "")[:200])
            return False
        return bool(json.loads(out.stdout or "[]"))
    except Exception as e:
        log.warning("gh run list не удался: %s", e)
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
    if live:
        if gh_workflow_monitor_in_progress():
            log.info("Monitor уже работает — не запускаю второй")
        else:
            gh_trigger_monitor(live[0]["name"])
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
    refs = merged_refs(cfg, subs)
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
    try:
        while time.time() < deadline:
            time.sleep(10)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Сессия слежения закончена (%.0f мин) — останавливаюсь",
                 (time.time() - started) / 60)
        pl.stop()
    return 0


def main():
    ap = argparse.ArgumentParser(description="StreamClipper на GitHub Actions")
    ap.add_argument("--check", action="store_true",
                    help="проверить эфиры и запустить Monitor при необходимости")
    ap.add_argument("--monitor", action="store_true",
                    help="сессия слежения: запись, клипы 9:16, релиз, clips.json")
    ap.add_argument("--channel", default="", help="канал (для --monitor)")
    ap.add_argument("--max-minutes", type=int, default=280,
                    help="максимальная длина сессии --monitor, минут")
    args = ap.parse_args()
    if args.check:
        return run_check()
    if args.monitor:
        return run_monitor(channel=args.channel, max_minutes=args.max_minutes)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
