"""StreamClipper на GitHub Actions: ВСЁ работает внутри репозитория.

Режимы (запускаются workflow-ами из .github/workflows):

  python gh_run.py --check-loop [--max-minutes 55]
      ПОСТОЯННОЕ слежение: задача живёт до ~55 минут, внутри неё работает
      один постоянный монитор: платформенные API опрашиваются каждые ~2-3
      секунды, а раз в ~15 секунд мы читаем готовый статус, обновляем сайт
      и подхватываем новые видео/клипы из subscriptions.json (там же видит
      новые видео/клипы и ставит их в очередь нарезки). Кто-то стримит —
      запускает Monitor. В конце задача-эстафета запускает следующую —
      слежение идёт без пауз, а выход в эфир замечается за считанные секунды.

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
    from clipper.hype_detector import DEFAULT_KEYWORDS as HYPE_KEYWORDS
except ImportError:                        # плоский репозиторий
    from links import dedup_key, parse_link, refs_from_config
    from notifier import Notifier
    from pipeline import Pipeline
    from stream_monitor import StreamMonitor
    from utils import load_config, setup_logging
    from hype_detector import DEFAULT_KEYWORDS as HYPE_KEYWORDS

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
SETTINGS_JSON = "settings.json"         # настройки качества клипов (пишет сайт)
FEEDBACK_JSON = "feedback.json"         # Интересно/Скучно по клипам (пишет сайт)
MAX_CLIPS = 60          # сколько клипов держать в clips.json
MAX_ASSETS = 60         # сколько файлов держать в релизе (старые удаляются)
PROBE_WAIT = 15         # секунд на проверку эфиров (быстрые API платформ)
CHECK_EVERY_S = 15      # пауза чтения статуса внутри --check-loop (сам монитор
                        # опрашивает платформы каждые ~2-3 с — быстрее некуда:
                        # cron GitHub сам по себе не бывает чаще 5 минут)
CHECK_LOOP_MIN = 55     # длина одной задачи --check-loop (потом эстафета)
VOD_MAX_WINDOWS = 100   # максимальный клипов из одного длинного видео
VOD_MAX_JOBS = 40       # сколько окон обрабатывает один запуск --vod
NO_SPEECH_SKIP_MAX = 8  # сколько окон без речи пропускаем за запуск (чтобы
                        # не сжечь весь run на тихих кусках)
BORED_SKIP_MAX = 6      # сколько «скучных» окон (низкая оценка качества)
                        # откладываем за запуск; лучший из отложенных всё же
                        # публикуем, если за весь run не вышел ни один клип

# Настройки качества по умолчанию (их меняет сайт -> settings.json).
# speech_language: auto — Whisper сам определяет язык (стример может быть
# и иностранным — автопоиск берёт топ всего Twitch).
DEFAULT_SETTINGS = {
    "speech_language": "auto",       # auto | ru | en
    "selection": "normal",           # loose | normal | strict
    "skip_no_speech": True,          # окна без речи заменяются более говорливыми
}
SELECTION_PRESETS = {
    # selection -> (дельта порога хайпа, max_idle_min)
    "loose":  (-14, 7),     # клипов больше: порог ниже, ожидание короче
    "normal": (0, 12),
    "strict": (+12, 20),    # клипов меньше, но попадания точнее
}
# Порог публикации ПОСЛЕ нарезки (оценка clip_quality 0..100): скучные клипы
# (статичный кадр ~2 МБ, тишина, нет речи) не выходят вовсе. Практика: живые
# клипы весят 30-45 МБ, скучные — 2 МБ. Регулируется кнопкой «отбор» на сайте.
MIN_PUBLISH_PRESETS = {
    "loose": 30.0,
    "normal": 40.0,
    "strict": 50.0,
}

# --- автопоиск лучших стримов Twitch ---------------------------------------
# Сам заходим на Twitch и берём в слежку ТОП ПО ЗРИТЕЛЯМ (публичный GQL
# сайта, без регистрации и ключей). Список авто-каналов живёт в отдельном
# ключе auto_channels файла subscriptions.json: каналы, добавленные
# человеком, НИКОГДА не трогаются и не удаляются.
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
DISCOVERY_COUNT = 25         # сколько топовых стримеров держать в слежке
DISCOVERY_MIN_VIEWERS = 2000  # отсечка по зрителям (мелкие каналы не нужны)
DISCOVERY_REFRESH_MIN = 20    # как часто обновлять топ, минут
DISCOVERY_TOP_FETCH = 30      # сколько стримов запрашивать (лимит Twitch: 30)


# --- хэштеги для TikTok (видны на сайте ДО скачивания клипа) -------------------
# Подбираем по игре из названия, нику стримера и платформе: сначала
# игровые теги, затем ник, затем самые «рекомендательные» общего плана.
GAME_TAG_MAP = [
    (["cs2", "counter-strike", "counter strike", "cs:go", "csgo", "кс2", "ксго", "контр"],
     ["cs2", "кс2", "cs"]),
    (["dota", "дота"], ["dota2", "дота2"]),
    (["minecraft", "майнкрафт", "майн"], ["minecraft", "майнкрафт"]),
    (["gta", "гта", "grand theft auto", "rp "], ["gta", "гта5", "gtarp"]),
    (["valorant", "валорант"], ["valorant", "валорант"]),
    (["fortnite", "фортнайт"], ["fortnite", "фортнайт"]),
    (["pubg", "пабг"], ["pubg", "пабг"]),
    (["apex", "апекс"], ["apexlegends", "апекс"]),
    (["warzone", "call of duty", "callofduty", "варзон", "кал оф дьюти"],
     ["warzone", "callofduty"]),
    (["rust", " раст"], ["rustgame", "раст"]),
    (["league of legends", " lol ", "лига легенд"], ["leagueoflegends", "лол"]),
    (["world of warcraft", " wow ", " вов ", "вова"], ["wow", "вов"]),
    (["hearthstone", "хартстоун"], ["hearthstone"]),
    (["genshin", "геншин"], ["genshin", "геншин"]),
    (["roblox", "роблокс"], ["roblox", "роблокс"]),
    (["fifa", "ea fc", "eafc", "фифа"], ["fifa", "eafc"]),
    (["nba", "нба"], ["nba2k", "нба"]),
    (["stalker", "s.t.a.l.k.e.r", "сталкер"], ["stalker2", "сталкер"]),
    (["escape from tarkov", "tarkov", "тарков", " эфт"], ["tarkov", "тарков"]),
]
VIRAL_TAGS = ["рек", "рекомендации", "fyp", "foryou", "viral", "прикол",
              "смешно", "хайп", "стрим", "клипы", "стример"]


def make_hashtags(platform="", channel="", title="", game=""):
    """Подборка хэштегов под конкретный клип (показываем ДО скачивания).

    Порядок: теги ИГРЫ (по названию видео/стрима) -> ник стримера ->
    платформа -> общие «рекомендательные». Без дублей, максимум 14 —
    длиннее в описании TikTok всё равно не нужно.
    """
    text = " ".join(str(x or "") for x in (title, game)).lower()
    tags = []
    for keys, tg in GAME_TAG_MAP:
        if tg and any(k in text for k in keys):
            tags.extend(tg)
    chan = "".join(ch for ch in str(channel or "").lower()
                   if ch.isalnum() and ch.isascii()) or \
        "".join(ch for ch in str(channel or "").lower()
                if ch.isalnum())
    if chan:
        tags.append(chan)
    tags.append("twitch" if platform == "twitch" else "youtube")
    tags.extend(VIRAL_TAGS)
    out, seen = [], set()
    for t in tags:
        t = str(t).strip().lstrip("#").lower()
        if t and t not in seen:
            seen.add(t)
            out.append("#" + t)
    return out[:14]


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
               platform="twitch", display=None, title="", score=None,
               hashtags=None, subs=None, src=None, start=None):
    name = os.path.basename(str(video_path))
    stem = os.path.splitext(name)[0]
    parts = stem.split("_")
    # 2026-11-19_12-15-00_nick -> канал после даты и времени
    chan = parts[2] if len(parts) > 2 else ""
    try:
        score_val = round(float(score), 1) if score is not None else 0
    except (TypeError, ValueError):
        score_val = 0
    sub = dict(subs or {})
    sub.pop("chars", None)
    sub.pop("speech_sec", None)
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
        "score": score_val,          # «насколько сочный момент» (для карточки)
        "hashtags": list(hashtags or []),   # рекомендации для TikTok до скачивания
        "subs": sub,                 # субтитры: {lang, words, speech_pct, ok}
        "src": src or "",            # источник (для клипов из видео) — для
        "start": int(start or 0),    # кнопки «Ещё отсюда» и фидбека
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
        self.last_tags = []

    def notify_clip(self, video_path, caption="", platform="twitch",
                    display=None, title="", score=None, subs=None):
        try:
            self._publish(video_path, caption, platform=platform,
                          display=display, title=title, score=score,
                          subs=subs)
            self.posted += 1
            cap = str(caption or "Клип готов")
            if self.last_tags:
                # хэштеги прямо в уведомление: видно ещё до скачивания
                cap += "\n\nХэштеги для TikTok:\n" + " ".join(self.last_tags)
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
                 display=None, title="", score=None, subs=None):
        rel = self.gh.release_id()
        log.info("Заливаю клип в Releases: %s", os.path.basename(video_path))
        asset = self.gh.upload_asset(rel, video_path)
        asset_name = asset.get("name") or os.path.basename(video_path)
        url = (f"https://github.com/{self.gh.repo}/releases/download/"
               f"{RELEASE_TAG}/{quote(asset_name)}")
        entry_chan = ""
        _stem = os.path.splitext(os.path.basename(str(video_path)))[0]
        _parts = _stem.split("_")
        if len(_parts) > 2:
            entry_chan = _parts[2]
        entry = clip_entry(video_path, url,
                           thumb_b64=compact_thumb(video_path, self.workdir),
                           size=os.path.getsize(str(video_path)),
                           platform=platform, display=display, title=title,
                           score=score, subs=subs,
                           hashtags=make_hashtags(platform=platform,
                                                  channel=entry_chan or display or "",
                                                  title=title))
        self.last_tags = list(entry.get("hashtags") or [])
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
        """Статусы каналов для сайта; коммит ТОЛЬКО при реальном изменении.

        sig учитывает LIVE-статус, заголовок и зрителей (округлённо до 25):
        слежение идёт каждые ~25 секунд, и без округления каждый цикл
        плодил бы коммит — GitHub такое не любит, а сайту это не нужно.
        """
        sig = json.dumps(
            [[c.get("name"), bool(c.get("live")), c.get("title") or "",
              int(round((c.get("viewers") or 0) / 25.0)) * 25]
             for c in channels], ensure_ascii=False)
        raw, sha = self.gh.get_file(CLIPS_JSON)
        data = load_clips_json_bytes(raw)
        if data["meta"].get("_live_sig") == sig and data["meta"].get("channels"):
            return                       # ничего не изменилось — без коммита
        data["meta"]["channels"] = channels
        data["meta"]["_live_sig"] = sig
        data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.gh.put_file(CLIPS_JSON,
                         json.dumps(data, ensure_ascii=False,
                                    indent=1).encode("utf-8"),
                         sha, "channels: live status")


# --- подписки и проверка эфиров ------------------------------------------------------------

def load_subscriptions(path=SUBS_JSON):
    """Список ссылок из subscriptions.json (правится с сайта или на github.com).

    channels — каналы человека; auto_channels — топ Twitch, найденный
    автопоиском. Оба списка следятся одинаково, человек всегда важнее.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = [str(x).strip() for x in (data.get("channels") or [])
               if str(x).strip()]
        out += [str(x).strip() for x in (data.get("auto_channels") or [])
                if str(x).strip()]
        return out
    except (OSError, ValueError):
        return []


# --- настройки качества клипов (settings.json) и фидбек (feedback.json) ------
# Настройки и оценки «Интересно/Скучно» редактируются НА САЙТЕ и приезжают
# сюда экспорт-кодом. Все режимы (Check/Monitor/Clip video) читают их сами.

def load_site_settings(gh):
    """settings.json из репозитория -> dict (недостающее — из DEFAULT_SETTINGS)."""
    s = dict(DEFAULT_SETTINGS)
    if gh is None:
        return s
    try:
        raw, _sha = gh.get_file(SETTINGS_JSON)
    except Exception:
        log.warning("Не прочитать %s — настройки по умолчанию", SETTINGS_JSON)
        return s
    if raw:
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                for k in DEFAULT_SETTINGS:
                    if k in data and data[k] is not None:
                        s[k] = data[k]
        except ValueError:
            log.warning("%s не читается — настройки по умолчанию", SETTINGS_JSON)
    return s


def apply_settings(cfg, s):
    """Настройки сайта -> config.yaml-объект.

    * язык речи: auto — Whisper сам определит (клипы у зарубежных стримеров
      больше не теряются из-за жёсткого «ru»);
    * отбор (selection): loose/normal/strict меняет порог хайпа и «скучную»
      паузу max_idle — с сайта регулируется ЧАСТОТА и ТОЧНОСТЬ нарезки.
    """
    cfg["speech_language"] = str(s.get("speech_language") or "auto").lower()
    sel = str(s.get("selection") or "normal").lower()
    if sel not in SELECTION_PRESETS:
        sel = "normal"
    delta, idle = SELECTION_PRESETS[sel]
    hyp = cfg.setdefault("hype", {})
    if "_base_threshold" not in hyp:
        hyp["_base_threshold"] = float(hyp.get("threshold", 52))
    hyp["threshold"] = float(hyp["_base_threshold"]) + delta
    clip = cfg.setdefault("clip", {})
    if "_base_idle" not in clip:
        clip["_base_idle"] = float(clip.get("max_idle_min", 12))
    clip["max_idle_min"] = float(clip["_base_idle"]) if delta == 0 else idle
    cfg["_min_publish"] = float(MIN_PUBLISH_PRESETS[sel])
    cfg["_settings"] = s
    log.info("Настройки: язык=%s, отбор=%s (порог %.0f, пауза %.0f мин, "
             "браковка < %.0f), пропуск без речи=%s", cfg["speech_language"], sel,
             hyp["threshold"], clip["max_idle_min"], cfg["_min_publish"],
             "да" if s.get("skip_no_speech") else "нет")
    return cfg


def load_feedback(gh):
    """feedback.json -> список оценок [{id, file, v, channel, src, start}]."""
    if gh is None:
        return []
    try:
        raw, _sha = gh.get_file(FEEDBACK_JSON)
    except Exception:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
        items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
    except ValueError:
        return []
    return items


def feedback_exclude_ranges(items, url):
    """Оценки «Скучно» -> интервалы [(a, b)] этого источника для пропуска."""
    u = str(url or "")
    out = []
    for it in items:
        if int(it.get("v") or 0) >= 0:          # пропускаем только «скучно»
            continue
        src = str(it.get("src") or "")
        if src and src.split("?")[0] == u.split("?")[0]:
            a = int(it.get("start") or 0)
            out.append((max(0, a - 20), a + 80))
    return out


def feedback_channel_bias(items, channel):
    """Оценки клипов КАНАЛА -> поправка порога хайпа.

    «Скучно» поднимает порог (меньше клипов, точнее моменты), «Интересно»
    опускает. Итог в диапазоне [-15, +15].
    """
    bias = 0.0
    ch = str(channel or "").lower()
    if not ch:
        return 0.0
    for it in items:
        if str(it.get("channel") or "").lower() != ch:
            continue
        v = int(it.get("v") or 0)
        if v < 0:
            bias += 4.0                          # скучно -> строже
        elif v > 0:
            bias -= 2.0                          # интересно -> свободнее
    return max(-15.0, min(15.0, bias))


def subs_caption(subs):
    """Мета субтитров -> строка для уведомления."""
    s = dict(subs or {})
    if s.get("ok"):
        return (f"Субтитры: {s.get('lang') or '?'} · {s.get('words', 0)} слов · "
                f"речи {s.get('speech_pct', 0)}%")
    return "Субтитров нет — в моменте не было различимой речи"


# --- автопоиск топовых стримов Twitch ---------------------------------------

def _parse_stream_node(node):
    """Узел GQL-стрима -> {login, display, viewers, title, game} или None."""
    b = node.get("broadcaster") or {}
    login = str(b.get("login") or "").strip().lower()
    if not login:
        return None
    try:
        viewers = int(node.get("viewersCount") or 0)
    except (TypeError, ValueError):
        viewers = 0
    return {"login": login,
            "display": str(b.get("displayName") or login),
            "viewers": viewers,
            "title": str(node.get("title") or "")[:120],
            "game": str((node.get("game") or {}).get("displayName") or "")}


def twitch_top_streams(limit=DISCOVERY_TOP_FETCH):
    """Топ Twitch ПО ЗРИТЕЛЯМ (сейчас в эфире), публичный GQL сайта.

    Без токенов и регистрации — тот же запрос, что делает сайт Twitch.
    За один запрос Twitch отдаёт НЕ БОЛЬШЕ 30 стримов (больше — уже
    «failed integrity check»: страницам нужен браузерный токен), поэтому
    лимит режется до 30. Возвращает [{login, display, viewers, title,
    game}]; при любой ошибке [] — слежение по списку каналов продолжается
    как работало.
    """
    limit = max(1, min(int(limit), 30))          # Twitch: first <= 30
    body = {"query": (
        "query($n: Int!) { streams(first: $n) { edges { node { "
        "broadcaster { login displayName } title viewersCount "
        "game { displayName } } } } }"),
        "variables": {"n": limit}}
    try:
        r = requests.post(TWITCH_GQL, json=body, timeout=30,
                          headers={"Client-ID": TWITCH_WEB_CLIENT_ID})
        if r.status_code != 200:
            log.warning("Топ Twitch: HTTP %s", r.status_code)
            return []
        j = r.json()
        if j.get("errors"):
            log.warning("Топ Twitch: %s",
                        str(j["errors"][0].get("message") or "")[:200])
            return []
        edges = ((j.get("data") or {}).get("streams") or {}).get("edges") or []
    except Exception as e:
        log.warning("Топ Twitch недоступен: %s", e)
        return []
    out = []
    for edge in edges:
        s = _parse_stream_node((edge or {}).get("node") or {})
        if s:
            out.append(s)
    return out


def discovery_urls(top, min_viewers=DISCOVERY_MIN_VIEWERS,
                   count=DISCOVERY_COUNT, exclude=()):
    """Топ-стримы -> ссылки каналов для слежки.

    Отсекаем мелкие эфиры (min_viewers), пропускаем уже добавленные
    (exclude — ники Twitch в нижнем регистре) и возвращаем максимум count
    ссылок, от самых зрительных к менее зрительным.
    """
    ex = {str(x).strip().lower() for x in (exclude or ()) if str(x).strip()}
    out = []
    for s in top:
        if int(s.get("viewers") or 0) < int(min_viewers):
            continue
        if s.get("login", "") in ex:
            continue
        out.append("https://www.twitch.tv/" + s["login"])
        if len(out) >= int(count):
            break
    return out


def _subs_twitch_logins(links):
    """Ники Twitch из списка ссылок (нижний регистр) — кого НЕ трогать."""
    logins = set()
    for s in links or []:
        r = parse_link(s)
        if r and r.platform == "twitch" and not getattr(r, "kind", ""):
            logins.add(r.id.lower())
    return logins


def auto_discover(gh, cfg, top_fn=None):
    """Обновить auto_channels в subscriptions.json топом Twitch.

    Читает subscriptions.json из репозитория, находит топ по зрителям,
    кладёт его в ключ auto_channels (каналы человека в channels не
    трогаются). Если топ не изменился — коммита нет. Возвращает СПИСОК
    авто-ссылок (даже если не изменился), чтобы слежка подхватила их
    сразу, не дожидаясь следующего запуска.
    """
    disc = (cfg or {}).get("discovery") or {}
    if disc.get("enabled", True) is False:
        return []
    count = int(disc.get("count") or DISCOVERY_COUNT)
    min_viewers = int(disc.get("min_viewers") or DISCOVERY_MIN_VIEWERS)
    if gh is None:
        return []
    try:
        raw, sha = gh.get_file(SUBS_JSON)
    except Exception:
        log.exception("Не прочитать %s для автопоиска", SUBS_JSON)
        return []
    data = {}
    if raw:
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            data = {}
    user_links = [str(x).strip() for x in (data.get("channels") or [])
                  if str(x).strip()]
    old_auto = [str(x).strip() for x in (data.get("auto_channels") or [])
                if str(x).strip()]
    exclude = _subs_twitch_logins(user_links)          # человек важнее топа
    fetch = (top_fn or twitch_top_streams)(max(count + 5,
                                                DISCOVERY_TOP_FETCH))
    urls = discovery_urls(fetch, min_viewers=min_viewers, count=count,
                          exclude=exclude)
    if set(urls) == set(old_auto):
        if urls:
            log.info("Топ Twitch без изменений (%d каналов) — коммит не нужен",
                     len(urls))
        return list(old_auto or urls)
    data["channels"] = user_links
    data["auto_channels"] = urls
    data["auto_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        put_json_state(gh, SUBS_JSON, data, sha,
                       "discovery: топ %d Twitch по зрителям (автопоиск)"
                       % len(urls))
    except Exception:
        log.exception("Не записать %s — автопоиск отложен", SUBS_JSON)
        return list(old_auto)
    names = ", ".join(u.rsplit("/", 1)[-1] for u in urls[:8])
    log.info("Автопоиск: взял в слежку топ Twitch (%d): %s%s",
             len(urls), names, "…" if len(urls) > 8 else "")
    return urls


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
    """True, если задача Monitor РАБОТ или ЖДЁТ в очереди (через gh CLI).

    Свой собственный запуск (GITHUB_RUN_ID) НЕ считается: финальная проверка
    внутри monitor.yml идёт, пока её же задача ещё «in_progress», — и должна
    уметь запустить следующую сессию, если эфир продолжается.
    Считаются и queued-запуски: иначе, пока сессия ждёт свободный раннер,
    каждая проверка (раз в ~15 c) заводила бы ЛИШНИЙ dispatch Monitor.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return False
    my_id = str(os.environ.get("GITHUB_RUN_ID") or "")
    for status in ("in_progress", "queued"):
        try:
            out = subprocess.run(
                ["gh", "run", "list", "--repo", repo, "--workflow",
                 "monitor.yml", "--status", status,
                 "--limit", "10", "--json", "databaseId"],
                capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                log.warning("gh run list: %s", (out.stderr or "")[:200])
                continue
            runs = json.loads(out.stdout or "[]")
            if any(str(r.get("databaseId")) != my_id for r in runs):
                return True
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
    gh = env_gh()
    subs = load_subscriptions()
    if gh:
        try:
            subs += auto_discover(gh, cfg)
        except Exception:
            log.exception("Автопоиск топ Twitch не удался — работаем без него")
    refs = merged_refs(cfg, subs)
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
    """ПОСТОЯННОЕ слежение: статус каждые ~CHECK_EVERY_S секунд до дедлайна.

    Одна задача Actions живёт до ~55 минут. Внутри неё работает ОДИН
    постоянный StreamMonitor: платформенные API (Twitch GQL, YouTube
    youtubei) опрашиваются каждые ~2-3 секунды, а мы раз в 15 секунд
    читаем готовый статус (без прогрева 15 c на каждой итерации),
    обновляем clips.json, подхватываем новые каналы/видео из
    subscriptions.json и запускаем Monitor на эфир. В конце задача
    эстафетой запускает следующую. Так выход стримера в эфир замечается
    за считанные секунды, а не «каждые 5-20 минут» (cron GitHub сам по
    себе не бывает чаще 5 минут).
    """
    setup_logging()
    gh = env_gh()
    cfg = load_config("config.yaml")
    cfg["_config_path"] = os.path.abspath("config.yaml")
    yt = cfg.get("youtube", {}) or {}

    auto_state = {"urls": [], "last": 0.0}
    set_state = {"s": None, "last": 0.0}

    def refresh_settings():
        """Настройки качества с сайта (раз в ~2 минуты): язык, строгость отбора."""
        if gh is None:
            return
        if time.time() - set_state["last"] < 120:
            return
        set_state["last"] = time.time()
        try:
            set_state["s"] = load_site_settings(gh)
            apply_settings(cfg, set_state["s"])
        except Exception:
            log.exception("Настройки сайта не применились — работаем прежними")

    def current_refs():
        # subscriptions.json перечитываем КАЖДЫЙ цикл: новый канал или
        # ссылка на видео с сайта начинает отслеживаться за 15 секунд;
        # авто-топ Twitch живёт в auto_state, пока его коммит не доедет
        return merged_refs(cfg, load_subscriptions(SUBS_JSON)
                           + auto_state["urls"])

    def maybe_discover():
        disc = (cfg.get("discovery") or {})
        if disc.get("enabled", True) is False or gh is None:
            return
        refresh = float(disc.get("refresh_min") or DISCOVERY_REFRESH_MIN) * 60
        if time.time() - auto_state["last"] < max(refresh, 300):
            return
        auto_state["last"] = time.time()
        try:
            auto_state["urls"] = auto_discover(gh, cfg)
        except Exception:
            log.exception("Автопоиск топ Twitch не удался — работаем без него")

    maybe_discover()
    refs = current_refs()
    mon = StreamMonitor(channels=live_refs(refs), quality="best", poll_twitch=2,
                        poll_youtube=3, yt_workers=3,
                        cookies_file=str(yt.get("cookies_file") or ""),
                        cookies_from_browser=str(yt.get("cookies_from_browser") or ""),
                        links_file=None,
                        refresh_refs=lambda: live_refs(current_refs()),
                        on_live=lambda ch: None, on_offline=lambda ch: None)
    mon.start()
    deadline = time.time() + max(1, int(max_minutes)) * 60
    n = 0
    log.info("Постоянное слежение: статус каждые %d c, %d мин, потом эстафета",
             CHECK_EVERY_S, max_minutes)
    while time.time() < deadline:
        started = time.time()
        try:
            n += 1
            refresh_settings()                # язык субтитров / строгость отбора
            maybe_discover()                  # топ Twitch обновляется сам
            refs = current_refs()             # новые каналы — сразу в слежку
            mon.sync_channels(live_refs(refs))
            meta = channels_meta_from_snapshot(refs, mon.status_rows() or [])
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
                    # готовые видео/клипы из subscriptions.json -> очередь;
                    # прямые ссылки на эфир не ставим в очередь (их пишет Monitor)
                    keys = {f"{m['platform']}:{m['name']}" for m in live}
                    enqueue_vod_jobs(gh, refs, keys)
                except Exception:
                    log.exception("Очередь нарезки: не удалось обновить")
            if live:
                if gh_workflow_monitor_in_progress():
                    log.info("Monitor уже работает — не запускаю второй")
                else:
                    gh_trigger_monitor(live[0]["name"])
        except Exception:
            log.exception("Проверка не удалась — повторю после паузы")
        rest = CHECK_EVERY_S - (time.time() - started)
        if rest > 0:
            time.sleep(rest)
    log.info("Смена: %d проверок за %d мин — запускаю следующую задачу",
             n, max_minutes)
    mon.stop()
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
    # Настройки качества с сайта (язык субтитров, строгость отбора):
    # применяем в начале сессии и ОБНОВЛЯЕМ раз в минуту — смена на сайте
    # доезжает до работающего монитора за минуту, без перезапуска.
    apply_settings(cfg, load_site_settings(gh))
    fb_state = {"items": load_feedback(gh), "last": 0.0}
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

    def refresh_quality():
        """Настройки и фидбек с сайта -> работающие детекторы (раз в минуту)."""
        try:
            if time.time() - fb_state["last"] < 60:
                return
            fb_state["last"] = time.time()
            s = load_site_settings(gh)
            apply_settings(cfg, s)
            pl.speech_language = cfg["speech_language"]
            try:
                pl.min_publish = float(cfg.get("_min_publish") or 40.0)
            except Exception:
                pass
            items = load_feedback(gh)
            if items:
                fb_state["items"] = items
            sel_delta = SELECTION_PRESETS.get(
                str(s.get("selection") or "normal").lower(), (0, 12))[0]
            for key, st in list((getattr(pl, "active", {}) or {}).items()):
                det = getattr(st, "detector", None)
                if det is None:
                    continue
                ch_bias = feedback_channel_bias(fb_state["items"],
                                                key.split(":")[-1])
                det.bias = sel_delta + ch_bias
                det.max_idle = max(0.0, float(
                    (cfg.get("clip") or {}).get("max_idle_min", 12))) * 60.0
                det.keywords = [str(k) for k in
                                ((cfg.get("hype") or {}).get("keywords")
                                 or HYPE_KEYWORDS)]
            log.info("Качество: язык=%s, отбор=%s, фидбек-оценок %d",
                     cfg["speech_language"], s.get("selection"),
                     len(fb_state["items"]))
        except Exception:
            log.exception("Обновление настроек не удалось — работаем прежними")

    started = time.time()
    pl.start()
    deadline = started + max(15, int(max_minutes)) * 60
    offline_strikes = 0
    try:
        while time.time() < deadline:
            time.sleep(60)
            refresh_quality()          # настройки/фидбек с сайта — без перезапуска
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
    # Настройки качества + фидбек сайта: язык субтитров, пропуск окон без
    # речи, исключение «скучных» мест видео из фидбека.
    settings = apply_settings(cfg, load_site_settings(gh))
    fb_items = load_feedback(gh)
    if fb_items:
        log.info("Фидбек с сайта: %d оценок — учитываю при отборе", len(fb_items))

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
    skips = 0
    bored = 0
    holdout = []      # отложенные как «скучные»: (path, score, display, platform, smeta, url, start)
    while jobs and made < max_jobs and time.time() < deadline - 12 * 60:
        job = jobs[0]
        key = vod_key(job)
        try:
            url = str(job.get("url") or "")
            display = str(job.get("display") or _vod_display(url))
            platform = "twitch" if "twitch." in url else "youtube"
            if job.get("kind") == "video" and not job.get("windows"):
                # ещё не размечено: ищем интересные моменты (звук + чат-реплей
                # Twitch); окна приходят УЖЕ отсортированными по интересности;
                # места, отмеченные «Скучно» на сайте, исключаются
                excl = feedback_exclude_ranges(fb_items, url)
                title, wins = vod_mod.expand_video(
                    url, workdir, cookies_file=cookies,
                    max_windows=int(os.environ.get("VOD_MAX_WINDOWS", VOD_MAX_WINDOWS)),
                    exclude_ranges=excl)
                job["display"] = title or display
                job["windows"] = [[int(a), int(b)] for a, b, _s in wins]
                job["scores"] = [float(s) for _a, _b, s in wins]
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
                start = 0
                src = vod_mod.download_clip_full(url, "vod_clip.mp4", workdir,
                                                 cookies_file=cookies)
            out, smeta = vod_mod.process_clip(src, clips_dir, workdir, cfg,
                                              display, platform,
                                              vcodec_args=vcodec_args,
                                              banner_path=banner_path)
            # Настройка «пропускать клипы без речи»: в окне не распознано ни
            # слова — выбрасываем и берём следующее, чтобы вместо тихого
            # скучного клипа был говорящий (лимит на запуск — не сжечь run).
            if (settings.get("skip_no_speech")
                    and not smeta.get("ok")
                    and int(smeta.get("words") or 0) < 3
                    and skips < NO_SPEECH_SKIP_MAX):
                skips += 1
                try:
                    os.remove(out)
                except OSError:
                    pass
                log.info("Окно без речи (%d/%d) — пропускаю, беру следующее",
                         skips, NO_SPEECH_SKIP_MAX)
                if job.get("kind") == "video":
                    job["windows"].pop(0)
                    if job.get("scores"):
                        job["scores"].pop(0)
                    if not job["windows"]:
                        jobs.pop(0)
                        done.add(key)
                else:
                    jobs.pop(0)
                    done.add(key)
                save(JOBS_JSON, jobs_list=jobs, msg="vod: окно без речи пропущено")
                continue
            size_mb = os.path.getsize(out) / 1e6
            # --- КОНТРОЛЬ КАЧЕСТВА ПОСЛЕ НАРЕЗКИ ---------------------------------
            # база: оценка окна (z-единицы -> 0..100); у готовых клипов Twitch
            # база 50 (они уже отобраны людьми)
            base = 50.0
            if job.get("kind") == "video" and job.get("scores"):
                try:
                    base = max(5.0, min(95.0, 50.0 + 12.0 * float(job["scores"][0])))
                except (TypeError, ValueError):
                    base = 50.0
            min_score = float(cfg.get("_min_publish") or 40.0)
            try:
                final_score, qparts = vod_mod.clip_quality(out, smeta, base=base)
            except Exception:
                log.exception("Оценка качества не удалась — публикую как есть")
                final_score, qparts = base, {}
            if final_score < min_score and bored < BORED_SKIP_MAX:
                bored += 1
                holdout.append((out, final_score, display, platform, smeta, url,
                                int(start or 0)))
                log.info("СКУКА: %.0f/100 < порога %.0f (битрейт %.2f Мбит/с, "
                         "звук %.0f dB, слов %d) — окно отложено (%d/%d)",
                         final_score, min_score, qparts.get("mbps", 0),
                         qparts.get("mean_db", -99), qparts.get("words", 0),
                         bored, BORED_SKIP_MAX)
                for f in (src,):
                    try:
                        if os.path.exists(f):
                            os.remove(f)
                    except OSError:
                        pass
                if job.get("kind") == "video":
                    job["windows"].pop(0)
                    if job.get("scores"):
                        job["scores"].pop(0)
                    if not job["windows"]:
                        jobs.pop(0)
                        done.add(key)
                else:
                    jobs.pop(0)
                    done.add(key)
                save(JOBS_JSON, jobs_list=jobs, msg="vod: скучное окно отложено")
                continue
            win_score = final_score
            caption = (f"Клип готов: {display} ({platform}, нарезка видео) — "
                       f"{size_mb:.1f} МБ\n" + subs_caption(smeta))
            if pub.notify_clip(out, caption, platform=platform,
                               display=display, title=display,
                               score=win_score, subs=smeta,
                               src=url if job.get("kind") == "video" else "",
                               start=int(start or 0)):
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
                if job.get("scores"):
                    job["scores"].pop(0)
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
    # Спасательный круг: если гейт отбраковал ВСЁ и за запуск не published ни
    # одного клипа — публикуем лучший из отложенных, чтобы лента не пустовала.
    if made == 0 and holdout:
        best = max(holdout, key=lambda x: x[1])
        _p, bscore, bdisp, bplat, bsmeta, burl, bstart = best
        try:
            size_mb = os.path.getsize(_p) / 1e6
            caption = (f"Клип готов: {bdisp} ({bplat}, нарезка видео) — "
                       f"{size_mb:.1f} МБ\n" + subs_caption(bsmeta))
            if pub.notify_clip(_p, caption, platform=bplat, display=bdisp,
                               title=bdisp, score=bscore, subs=bsmeta,
                               src=burl, start=bstart):
                made += 1
                holdout.remove(best)
                log.info("За запуск всё было скучным — опубликовал лучший из "
                         "отложенных (оценка %.0f), чтобы лента не пустовала", bscore)
        except Exception:
            log.exception("Не удалось опубликовать лучший из отложенных")
    for item in holdout:
        try:
            os.remove(item[0])
        except OSError:
            pass
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
