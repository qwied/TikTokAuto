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
import re
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
    from clipper.utils import ffprobe_meta, load_config, setup_logging
    from clipper.hype_detector import DEFAULT_KEYWORDS as HYPE_KEYWORDS
except ImportError:                        # плоский репозиторий
    from links import dedup_key, parse_link, refs_from_config
    from notifier import Notifier
    from pipeline import Pipeline
    from stream_monitor import StreamMonitor
    from utils import ffprobe_meta, load_config, setup_logging
    from hype_detector import DEFAULT_KEYWORDS as HYPE_KEYWORDS

try:                                       # нарезка готовых видео
    from clipper import banner as banner_mod
    from clipper.utils import detect_h264_encoder, ensure_dir
    from clipper import vod as vod_mod
    from clipper.tiktok import TikTokPoster
    from clipper import tiktok as tiktok_mod
except ImportError:
    import banner as banner_mod
    from utils import detect_h264_encoder, ensure_dir
    import vod as vod_mod
    from tiktok import TikTokPoster
    import tiktok as tiktok_mod

log = logging.getLogger("gh_run")

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
RELEASE_TAG = "auto"
CLIPS_JSON = "clips.json"
SUBS_JSON = "subscriptions.json"
PUSH_SUBS = "push_subscriptions.json"   # подписки Web Push (пишет сайт)
JOBS_JSON = "jobs.json"                 # очередь нарезки видео (пишет сайт/Check)
PROCESSED_JSON = "processed.json"       # уже нарезанные видео (не повторяем)
YT_STATE = "youtube_state.json"         # состояние YouTube-дискавери (пауза при бот-чеке)
SETTINGS_JSON = "settings.json"         # настройки качества клипов (пишет сайт)
FEEDBACK_JSON = "feedback.json"         # Интересно/Скучно по клипам (пишет сайт)
TIKTOK_PENDING_JSON = "tiktok_pending.json"  # отложенная очередь TikTok (самолечение)
TIKTOK_POSTED_JSON = "tiktok_posted.json"    # что уже выходило (защита от дублей)
MAX_CLIPS = 60          # сколько клипов держать в clips.json
MAX_ASSETS = 60         # сколько файлов держать в релизе (старые удаляются)
PREVIEW_DIR = "preview"  # лёгкие 480p-копии клипов в репо: Pages отдаёт их
                         # same-origin и iPhone играет сразу (файлы Releases
                         # помечены Content-Disposition: attachment и без CORS
                         # — Safari их в <video> не играет и fetch'ем не берёт)
PREVIEW_KEEP = 12       # сколько последних превью хранить в репо (bloat git-истории ограничиваем)
PAGES_BASE = "https://qwied.github.io/TikTokAuto"
MONITOR_SESSION_MIN = 100  # длина одной сессии --monitor (мин): раньше 280,
                           # но зависшая смена блокировала слежку на часы
                           # («Monitor уже работает») — теперь зомби живёт
                           # максимум 100 мин, дальше эстафета перезапускает
MONITOR_STALE_MIN = 110    # запуск Monitor старше этого числа минут считаем
                           # мёртвым (гейт «уже работает» его игнорирует)
WIPE_MARKER = "wipe_clips.now"   # маркер-файл в репо: стереть ВСЕ старые клипы
                         # (разовая операция: ассеты релиза + clips.json; файл
                         # удаляется после очистки, повтора не будет)
PROBE_WAIT = 15         # секунд на проверку эфиров (быстрые API платформ)
CHECK_EVERY_S = 15      # пауза чтения статуса внутри --check-loop (сам монитор
                        # опрашивает платформы каждые ~2-3 с — быстрее некуда:
                        # cron GitHub сам по себе не бывает чаще 5 минут)
CHECK_LOOP_MIN = 55     # длина одной задачи --check-loop (потом эстафета)
VOD_MAX_WINDOWS = 100   # максимальный клипов из одного длинного видео
VOD_MAX_JOBS = 40       # сколько окон обрабатывает один запуск --vod
YT_DISCOVER_EVERY = 1800   # YouTube-дискавери не чаще, чем раз в 30 минут

# --- автопоиск хайповых YouTube-видео (RU) --------------------------------------------------
# 2026-11-22: тематика снята (запрос: «не обязательно придерживаться тематики,
# ПРОСТО БЕРИ ВИДЕО КОТОРЫЕ САМЫЕ ПОПУЛЯРНЫЕ ИМЕННО РУССКИЕ»). Префикс "gta6:"
# остался поддерживаемым (кто-то может вернуть тему через config), но дефолтные
# запросы — только общие русскоязычные хайп-видео.
GTA6_RE = re.compile(r"gta\s*6|gta\s*vi|гта\s*6|гта\s*vi|gta6", re.IGNORECASE)
CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)

# Дефолтные поисковые запросы: самые популярные русскоязычные видео (стримы,
# приколы, шоу, подкасты) — сортировка всё равно по ПРОСМОТРАМ, тема любая.
DEFAULT_YT_QUERIES = [
    "ru:смешные моменты стрима", "ru:приколы стримеров",
    "ru:лучшие моменты", "ru:стрим угар", "ru:стример взбесился",
    "ru:русские стримы", "ru:пранк", "ru:хайп",
    "ru:подкаст", "ru:шоу", "ru:стендап", "ru:интервью",
    "ru:обзор", "ru:эксперимент",
]


def discover_youtube_hype(gh, cfg):
    """Самые просматриваемые РУССКОЯЗЫЧНЫЕ видео -> очередь нарезки.

    Ищем через yt-dlp (без API-ключей): несколько запросов раз в ~30 минут,
    фильтр — по просмотрам, длине (не Shorts, не двухчасовые марафоны) и
    кириллице в заголовке. ТЕМА ЛЮБАЯ (2026-11-22: «теперь просто бери видео
    которые самые популярные именно русские») — сортировка по просмотрам
    берёт самые хайповые. Повторы отсеиваются processed.json.
    Очередь не переполняем: докидываем не больше max_per_check за раз и
    только если в ней меньше queue_max задач. Найденные окна всё равно
    анализируются посекундно (громкость + чат), в TikTok идут только
    хайповые (порог tiktok.min_score).
    """
    if gh is None:
        return 0
    if os.environ.get("GITHUB_ACTIONS"):
        # YouTube с раннеров закрыт бот-чеком («Sign in to confirm you're
        # not a bot» — yt-dlp перепробовал все плеер-клиенты): задачи всё
        # равно сгорали после 3 попыток, забивая очередь нарезки. На
        # компьютере дискавери работает как раньше.
        return 0
    disc = (cfg.get("youtube_discover") or {})
    if disc.get("enabled", True) is False:
        return 0
    queries = [str(q).strip() for q in (disc.get("queries") or []) if str(q).strip()]
    if not queries:
        queries = list(DEFAULT_YT_QUERIES)
    # ТЕМА запроса через префикс "тема:запрос" (см. config.yaml):
    #   gta6 — только видео именно про GTA 6/VI (обязателен GTA в заголовке);
    #   ru   — любая русскоязычная хайп-нарезка (стрим-угар, приколы).
    tagged = []
    for q in queries:
        tag, _, rest = q.partition(":")
        if tag not in ("gta6", "ru") or not rest:
            tag, rest = "gta6", q          # без префикса — как раньше, GTA6
        tagged.append((tag, rest.strip()))
    queries = [(tag, rest) for tag, rest in tagged if rest]
    min_views = int(disc.get("min_views") or 50000)
    min_dur = int(disc.get("min_minutes") or 8) * 60
    max_dur = int(disc.get("max_minutes") or 180) * 60
    max_add = int(disc.get("max_per_check") or 2)
    per_query = int(disc.get("results_per_query") or 10)
    queue_max = int(disc.get("queue_max") or 10)

    # Самолечение бот-чека: если YouTube заблокировал раннер целиком, поиски
    # бесполезны — ставим паузу дискавери на 2 часа (youtube_state.json),
    # чтобы не жечь минуты раннера и не плодить ошибок в логе.
    yt_state, _ysha = load_json_state(gh, YT_STATE)
    blocked_until = 0.0
    try:
        blocked_until = float(yt_state.get("blocked_until") or 0)
    except (TypeError, ValueError):
        blocked_until = 0.0
    if blocked_until > time.time():
        log.info("YouTube-дискавери на паузе (бот-чек) до %s UTC — пропускаю",
                 time.strftime("%H:%M", time.gmtime(blocked_until)))
        return 0

    jobs_data, jsha = load_json_state(gh, JOBS_JSON)
    proc_data, _psha = load_json_state(gh, PROCESSED_JSON)
    jobs = [j for j in (jobs_data.get("jobs") or []) if isinstance(j, dict)]
    done = set(proc_data.get("done") or [])
    queued = {vod_key(j) for j in jobs}
    room = min(max_add, queue_max - len(jobs))
    if room <= 0:
        return 0

    found = {}
    bot_fails = 0          # поисков, упёршихся в бот-чек
    ok_queries = 0         # поисков, вернувших результат
    for tag, q in queries:
        try:
            rc, out, err = vod_mod.yt_search_json("ytsearch%d:%s" % (per_query, q))
            if rc != 0 or not (out or "").strip():
                log.warning("YouTube-поиск «%s»: пусто (rc=%d)", q, rc)
                if vod_mod._looks_like_bot_check(err):
                    bot_fails += 1
                continue
            data = json.loads(out)
        except Exception as e:
            log.warning("YouTube-поиск «%s» не удался: %s", q, e)
            continue
        ok_queries += 1
        for e in (data.get("entries") or []):
            vid = str(e.get("id") or "")
            title = str(e.get("title") or "")
            if not vid or not title:
                continue
            try:
                dur = float(e.get("duration") or 0)
                views = int(e.get("view_count") or 0)
            except (TypeError, ValueError):
                continue
            if not (min_dur <= dur <= max_dur):
                continue                      # Shorts и марафоны мимо
            if views < min_views:
                continue
            if tag == "gta6":
                # тема GTA 6: заголовок обязан содержать GTA 6/VI + быть русским
                if not (GTA6_RE.search(title) and CYRILLIC_RE.search(title)):
                    continue
            elif not CYRILLIC_RE.search(title):
                continue                      # тема ru: русскоязычное — обязательно
            url = "https://www.youtube.com/watch?v=" + vid
            if url in done or url in queued or url in found:
                continue
            found[url] = {"url": url, "kind": "video", "fails": 0,
                          "tag": tag, "display": title[:120],
                          "views": views,
                          "added": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime())}
    if not found:
        if ok_queries == 0 and bot_fails >= 2:
            # YouTube закрыл раннер бот-чеком по всем запросам: пауза на 2 ч,
            # чтобы не жечь раннер впустую каждые 30 минут
            yt_state["blocked_until"] = time.time() + 2 * 3600
            yt_state["reason"] = "bot-check: все поисковые запросы заблокированы"
            yt_state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                _raw, sha2 = load_json_state(gh, YT_STATE)
                put_json_state(gh, YT_STATE, yt_state, sha2,
                               "youtube: бот-чек — дискавери на паузе 2 ч (самолечение)")
            except Exception:
                log.exception("youtube_state.json не записан")
            log.warning("YouTube закрыл раннер бот-чеком (%d/%d запросов) — "
                        "дискавери на паузе 2 ч", bot_fails, len(queries))
        return 0
    if ok_queries and yt_state.get("blocked_until"):
        # YouTube снова отвечает — снимаем паузу дискавери
        yt_state["blocked_until"] = 0
        yt_state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            _raw, sha2 = load_json_state(gh, YT_STATE)
            put_json_state(gh, YT_STATE, yt_state, sha2,
                           "youtube: бот-чек снялся — дискавери снова активен")
        except Exception:
            log.exception("youtube_state.json не обновлён")
    best = sorted(found.values(), key=lambda j: -j["views"])[:room]
    for j in best:
        jobs.append(j)
        queued.add(vod_key(j))
        log.info("YouTube-дискавери: %s (%s просм.) — в очередь нарезки",
                 j["display"][:70], j["views"])
    jobs_data["jobs"] = jobs
    jobs_data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        put_json_state(gh, JOBS_JSON, jobs_data, jsha,
                       "vod: +хайповые RU-видео с YouTube (популярные, любая тема)")
    except Exception:
        log.exception("Не записать jobs.json — дискавери пропущен")
        return 0
    gh_workflow_dispatch("clip.yml")          # нарезка стартует сразу
    return len(best)


NO_SPEECH_SKIP_MAX = 10  # сколько окон без РЕЧИ пропускаем за запуск (чтобы
                         # не сжечь весь run на тихих кусках)
BORED_SKIP_MAX = 6      # сколько «скучных» окон (низкая оценка качества)
                        # откладываем за запуск; лучший из отложенных всё же
                        # публикуем, если за весь run не вышел ни один клип

# Гейт РЕЧИ (запрос: «не делай клипы, где вообще не говорят»): окно считается
# пустым не только при нулевых словах, но и когда речи почти нет —
# меньше 12 слов или речь занимает меньше 12% длины клипа (это и есть
# «клипы, где вообще никто ничего не говорит»: пара слов-галлюцинаций
# Whisper уже не считается речью).
MIN_WORDS_CLIP = 12
MIN_SPEECH_PCT = 12

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
# клипы весят 30-45 МБ, скучные — 2 МБ. Порог поднят (запрос: «моменты ещё
# лучше»). Регулируется кнопкой «отбор» на сайте.
MIN_PUBLISH_PRESETS = {
    "loose": 33.0,
    "normal": 45.0,
    "strict": 56.0,
}

# --- автопоиск лучших стримов Twitch ---------------------------------------
# Сам заходим на Twitch и берём в слежку ТОП ПО ЗРИТЕЛЯМ (публичный GQL
# сайта, без регистрации и ключей). Список авто-каналов живёт в отдельном
# ключе auto_channels файла subscriptions.json: каналы, добавленные
# человеком, НИКОГДА не трогаются и не удаляются.
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
DISCOVERY_COUNT = 60         # сколько русскоязычных стримеров держать в слежке
                             # (запрос: «каналов как можно больше»)
DISCOVERY_MIN_VIEWERS = 800   # отсечка по зрителям. Запрос «каналов как можно
                              # больше»: 800 вместо 2000 — берём и средние
                              # эфиры (скучные всё равно отсеет min_publish)
DISCOVERY_REFRESH_MIN = 20    # как часто обновлять топ, минут
DISCOVERY_TOP_FETCH = 30      # сколько стримов запрашивать (лимит Twitch: 30)
DISCOVERY_LANGUAGE = "ru"     # ТОЛЬКО русскоязычные эфиры (запрос пользователя);
                              # "" — все языки. Мелких русских не набралось —
                              # планка зрителей временно опускается до 500.
DISCOVERY_FALLBACK_VIEWERS = 500  # низкая планка, если русских с 2000+ мало

# Каналы-ИСКЛЮЧЕНИЯ (запрос 2026-11-22: «всех кроме frametamer нарезай»):
# frametamer666 (донатофон 24/7) вырезается из автопоиска, из слежки и из
# очереди нарезки — его клипы больше не делаем и на сайт не попадают.
CHANNEL_BLACKLIST = {"frametamer666"}


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


def make_caption(display="", hook=""):
    """Готовый текст описания для TikTok (копируется с карточки одним тапом).

    Живая цитата стримера из клипа работает в описании лучше шаблона —
    она «цепляет» и повышает досмотры (а досмотр решает просмотры).
    Мат уже замаскирован в subtitle._hook_from_words.
    """
    hook = str(hook or "").strip()
    display = str(display or "").strip() or "стрим"
    if hook:
        return "«%s» — %s" % (hook, display)
    return "Лучший момент со стрима %s" % display


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

    def delete_file(self, path, sha, message):
        """Удалить файл из репозитория (нужен sha файла)."""
        self.req("DELETE", f"/repos/{self.repo}/contents/{quote(path)}",
                 json={"message": message, "sha": sha})

    def list_dir(self, path):
        """Содержимое каталога репо: [{name, path, sha}] или None (нет кат.)."""
        r = self.s.get(f"{API}/repos/{self.repo}/contents/{quote(path)}",
                       timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RuntimeError(f"list {path}: HTTP {r.status_code}")
        return r.json()


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
               hashtags=None, subs=None, src=None, start=None, caption="",
               preview=""):
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
        "preview": preview or "",   # 480p-копия в репо (Pages): плеер сайта
                                     # играет её сразу, в т.ч. на iPhone
        "thumb": thumb_b64 or "",
        "size": int(size or 0),
        "duration": round(float(dur or 60), 1),
        "channel": chan,
        "display": display or chan,
        "platform": platform,
        "title": title or "",
        "score": score_val,          # «насколько сочный момент» (для карточки)
        "hashtags": list(hashtags or []),   # рекомендации для TikTok до скачивания
        "caption": str(caption or ""),      # готовый текст описания для TikTok
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


def wipe_old_clips_once(gh):
    """Разовая чистка ВСЕХ старых клипов (запрос пользователя 2026-11-20).

    Маркер-файл wipe_clips.now в корне репо (коммитится деплоем):
      1) удаляются ВСЕ файлы релиза auto (все старые клипы);
      2) clips.json сбрасывается в пустой;
      3) маркер удаляется — повторов не будет.
    Идемпотентно: если процесс упал посреди, на следующей смене недоделанное
    доработается (список ассетов уже пуст — удалять нечего). Любая ошибка не
    ломает смену — чистка повторится позже.
    """
    if gh is None:
        return False
    try:
        raw, sha = gh.get_file(WIPE_MARKER)
    except Exception:
        log.exception("Не прочитать маркер чистки %s", WIPE_MARKER)
        return False
    if raw is None:
        return False
    log.info("Маркер %s найден — стираю ВСЕ старые клипы (релиз + clips.json)",
             WIPE_MARKER)
    try:
        rel = gh.release_id()
        assets = gh.list_assets(rel) or []
        for a in assets:
            try:
                gh.delete_asset(a["id"])
                log.info("Релиз: стёрт %s", a.get("name"))
            except Exception as e:
                log.warning("Не стереть ассет %s: %s", a.get("name"), e)
        raw_clips, clips_sha = gh.get_file(CLIPS_JSON)
        data = load_clips_json_bytes(raw_clips)
        data["clips"] = []
        data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        gh.put_file(CLIPS_JSON, json.dumps(data, ensure_ascii=False,
                                           indent=1).encode("utf-8"),
                    clips_sha, "wipe: старые клипы удалены (все), clips.json пуст")
        try:
            gh.delete_file(WIPE_MARKER, sha,
                           "wipe: маркер чистки снят — старых клипов больше нет")
        except Exception:
            log.exception("Маркер не удалить — чистка повторится на следующей смене")
        log.info("Чистка старых клипов завершена: %d файлов релиза стёрто",
                 len(assets))
        return True
    except Exception:
        log.exception("Чистка старых клипов не удалась — повторю позже")
        return False


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
        self.last_caption = ""

    def notify_clip(self, video_path, caption="", platform="twitch",
                    display=None, title="", score=None, subs=None,
                    src=None, start=None):
        try:
            self._publish(video_path, caption, platform=platform,
                          display=display, title=title, score=score,
                          subs=subs, src=src, start=start)
            self.posted += 1
            cap = str(caption or "Клип готов")
            if self.last_tags:
                # хэштеги прямо в уведомление: видно ещё до скачивания
                cap += "\n\nХэштеги для TikTok:\n" + " ".join(self.last_tags)
            if self.last_caption:
                cap += "\n\nТекст для TikTok (копируй в описание):\n" \
                       + self.last_caption
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
                 display=None, title="", score=None, subs=None,
                 src=None, start=None):
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
        try:
            # РЕАЛЬНАЯ длительность финального файла: переменная длина клипов
            # (35-48 с) раньше писалась в clips.json как константные 60.0
            dur_real = (ffprobe_meta(video_path) or {}).get("duration") or None
        except Exception:
            dur_real = None
        try:
            # 480p-копия в репо — чтобы клип можно было СМОТРЕТЬ на сайте
            # (на iPhone файлы Releases не играют: attachment + без CORS)
            preview_url = self._make_preview(video_path)
        except Exception:
            preview_url = ""
        entry = clip_entry(video_path, url,
                           thumb_b64=compact_thumb(video_path, self.workdir),
                           size=os.path.getsize(str(video_path)),
                           dur=dur_real,
                           platform=platform, display=display, title=title,
                           score=score, subs=subs, src=src, start=start,
                           preview=preview_url,
                           hashtags=make_hashtags(platform=platform,
                                                  channel=entry_chan or display or "",
                                                  title=title),
                           caption=make_caption(display or entry_chan,
                                                str((subs or {}).get("hook") or "")))
        self.last_tags = list(entry.get("hashtags") or [])
        self.last_caption = str(entry.get("caption") or "")
        self._update_clips_json(entry)
        log.info("clips.json обновлён: %s", url)
        self.last_url = url
        prune_assets(self.gh, rel, max_assets=self.max_assets)

    # --- лёгкие превью для плеера сайта (iPhone) ---------------------------------

    def _put_file_retry(self, path, data, message, tries=4):
        """put_file с ретраем на 409 (параллельные писатели — та же гонка,
        что у clips.json)."""
        last_err = None
        for attempt in range(max(1, tries)):
            try:
                _raw, sha = self.gh.get_file(path)
                self.gh.put_file(path, data, sha, message)
                return
            except RuntimeError as e:
                last_err = e
                if "409" not in str(e):
                    raise
                time.sleep(2 + attempt * 3)
        raise last_err

    def _make_preview(self, video_path):
        """480p-копия клипа прямо в репо (Pages) — для плеера сайта.

        iPhone/Safari не играет файлы Releases (Content-Disposition:
        attachment, CORS нет — ни прямой <video>, ни fetch-фолбэк).
        Same-origin Pages-URL из preview/ играет сразу. Возврат URL или "".
        """
        name = os.path.basename(str(video_path))
        out = os.path.join(str(self.workdir or "."), "gh_preview.mp4")
        try:
            if os.path.exists(out):
                os.remove(out)
            p = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-nostdin", "-i", os.path.abspath(str(video_path)),
                 "-vf", "scale=480:-2", "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "28",
                 "-maxrate", "900k", "-bufsize", "1800k",
                 "-c:a", "aac", "-b:a", "80k",
                 "-movflags", "+faststart", out],
                capture_output=True, timeout=300)
            if p.returncode != 0 or not os.path.exists(out) \
                    or os.path.getsize(out) < 100000:
                log.warning("Превью не собралось: %s",
                            (p.stderr or b"")[-200:])
                return ""
            with open(out, "rb") as f:
                data = f.read()
            path = f"{PREVIEW_DIR}/{name}"
            self._put_file_retry(path, data, f"preview: {name}")
            self._prune_previews(keep=PREVIEW_KEEP)
            return f"{PAGES_BASE}/{path}"
        except Exception:
            log.exception("Превью упало — клип играет как раньше")
            return ""
        finally:
            try:
                if os.path.exists(out):
                    os.remove(out)
            except OSError:
                pass

    def _prune_previews(self, keep=PREVIEW_KEEP):
        """Держим в preview/ только последние keep файлов (bloat git)."""
        try:
            files = self.gh.list_dir(PREVIEW_DIR) or []
        except Exception:
            return
        if len(files) <= keep:
            return
        files.sort(key=lambda x: str(x.get("name") or ""))
        for f in files[:-keep]:
            try:
                self.gh.delete_file(f["path"], f["sha"],
                                    f"preview prune: {f.get('name')}")
            except Exception:
                log.warning("Превью %s не удалилось", f.get("name"))

    def _update_clips_json(self, entry, tries=4):
        """clips.json: слить клип в ФРЕШ-версию и закоммитить.

        Прод-инцидент 2026-11-22: параллельные писатели (Check live обновляет
        meta, Monitor добавляет клипы) ловили HTTP 409 на устаревший sha, и
        КЛИП ТЕРЯЛСЯ навсегда (нарезан, залит в Release, но без записи в
        clips.json). Теперь на 409 заново читаем файл, льём клип ПОВЕРХ
        свежей версии и повторяем до 4 раз.
        """
        last_err = None
        for attempt in range(max(1, tries)):
            try:
                raw, sha = self.gh.get_file(CLIPS_JSON)
                data = load_clips_json_bytes(raw)
                ids = {c.get("id") for c in data["clips"]}
                if entry["id"] not in ids:
                    data["clips"] = [entry] + data["clips"]
                data["clips"] = data["clips"][:self.max_clips]
                data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime())
                self.gh.put_file(CLIPS_JSON,
                                 json.dumps(data, ensure_ascii=False,
                                            indent=1).encode("utf-8"),
                                 sha, f"clip: {entry['file']}")
                return
            except RuntimeError as e:
                last_err = e
                log.warning("clips.json: попытка %d/%d не удалась (%s) — "
                            "перечитываю файл и повторяю", attempt + 1, tries, e)
                time.sleep(2 + attempt * 3)
        raise last_err

    def update_channels_meta(self, channels, tries=4):
        """Статусы каналов для сайта; коммит ТОЛЬКО при реальном изменении.

        sig учитывает LIVE-статус, заголовок и зрителей (округлённо до 25):
        слежение идёт каждые ~25 секунд, и без округления каждый цикл
        плодил бы коммит — GitHub такое не любит, а сайту это не нужно.
        На 409 перечитываем файл и повторяем (та же гонка, что у клипов).
        """
        sig = json.dumps(
            [[c.get("name"), bool(c.get("live")), c.get("title") or "",
              int(round((c.get("viewers") or 0) / 25.0)) * 25]
             for c in channels], ensure_ascii=False)
        last_err = None
        for attempt in range(max(1, tries)):
            try:
                raw, sha = self.gh.get_file(CLIPS_JSON)
                data = load_clips_json_bytes(raw)
                if data["meta"].get("_live_sig") == sig \
                        and data["meta"].get("channels"):
                    return                       # ничего не изменилось — без коммита
                data["meta"]["channels"] = channels
                data["meta"]["_live_sig"] = sig
                data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime())
                self.gh.put_file(CLIPS_JSON,
                                 json.dumps(data, ensure_ascii=False,
                                            indent=1).encode("utf-8"),
                                 sha, "channels: live status")
                return
            except RuntimeError as e:
                last_err = e
                log.warning("clips.json (meta): попытка %d/%d не удалась (%s) "
                            "— повторяю", attempt + 1, tries, e)
                time.sleep(2 + attempt * 3)
        raise last_err


# --- подписки и проверка эфиров ------------------------------------------------------------

def load_subscriptions(path=SUBS_JSON):
    """Список ссылок из subscriptions.json (правится с сайта или на github.com).

    channels — каналы человека; auto_channels — топ Twitch, найденный
    автопоиском. Оба списка следятся одинаково, человек всегда важнее.
    Каналы-исключения (CHANNEL_BLACKLIST, frametamer666) вырезаются и из
    ручного списка, и из авто — иначе они нарезались бы до следующего
    автопоиска (жалоба 2026-11-22: «всех кроме frametamer»).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = [str(x).strip() for x in (data.get("channels") or [])
               if str(x).strip()]
        out += [str(x).strip() for x in (data.get("auto_channels") or [])
                if str(x).strip()]

        def _bl(url):
            low = str(url).rsplit("/", 1)[-1].lower()
            return low not in CHANNEL_BLACKLIST

        return [u for u in out if _bl(u)]
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
    """Узел GQL-стрима -> {login, display, viewers, title, game, language} или None."""
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
            "game": str((node.get("game") or {}).get("displayName") or ""),
            "language": str(node.get("language") or "").lower()}


_RE_CYRILLIC = re.compile(r"[\u0400-\u04FF]")


def filter_by_language(top, language="ru"):
    """Только эфиры нужного языка (по умолчанию — русскоязычные).

    Поле language отдаёт сам Twitch (GQL). Нюанс: часть русскоязычных
    стримеров помечает стрим «uk»/«other» — им доверяем только при
    кириллическом названии (русский тайтл = русскоязычный эфир).
    language=None или "" отключает фильтр (все языки).
    """
    lang = str(language or "").strip().lower()
    if not lang or lang in ("any", "all", "off"):
        return list(top or [])
    out = []
    for s in top or []:
        s_lang = str(s.get("language") or "").lower()
        title = s.get("title") or ""
        cyr = bool(_RE_CYRILLIC.search(title))
        if s_lang == lang:
            out.append(s)
        elif s_lang in ("", "uk", "other") and cyr:
            out.append(s)          # язык не указан/помечен uk — но тайтл русский
    return out


def twitch_top_streams(limit=DISCOVERY_TOP_FETCH):
    """Топ Twitch ПО ЗРИТЕЛЯМ (сейчас в эфире), публичный GQL сайта.

    Без токенов и регистрации — тот же запрос, что делает сайт Twitch.
    За один запрос Twitch отдаёт НЕ БОЛЬШЕ 30 стримов (больше — уже
    «failed integrity check»: страницам нужен браузерный токен), поэтому
    лимит режется до 30. Возвращает [{login, display, viewers, title,
    game, language}]; при любой ошибке [] — слежение по списку каналов
    продолжается как работало.
    """
    limit = max(1, min(int(limit), 30))          # Twitch: first <= 30

    def _query(with_language=True):
        fields = ("broadcaster { login displayName } title viewersCount "
                  "game { displayName } language" if with_language else
                  "broadcaster { login displayName } title viewersCount "
                  "game { displayName }")
        return {"query": ("query($n: Int!) { streams(first: $n) { edges { node { "
                           + fields + " } } } }")}

    j = None
    for with_lang in (True, False):   # если Twitch не знает поле language —
        try:                          # повторяем запрос по старой схеме
            r = requests.post(TWITCH_GQL, json=dict(_query(with_lang),
                                                    variables={"n": limit}),
                              timeout=30,
                              headers={"Client-ID": TWITCH_WEB_CLIENT_ID})
            if r.status_code != 200:
                log.warning("Топ Twitch: HTTP %s", r.status_code)
                return []
            j = r.json()
            if not j.get("errors"):
                break
            log.warning("Топ Twitch (language=%s): %s", with_lang,
                        str(j["errors"][0].get("message") or "")[:160])
        except Exception as e:
            log.warning("Топ Twitch недоступен: %s", e)
            return []
    if j is None or j.get("errors"):
        return []
    edges = ((j.get("data") or {}).get("streams") or {}).get("edges") or []
    out = []
    for edge in edges:
        s = _parse_stream_node((edge or {}).get("node") or {})
        if s:
            out.append(s)
    return out


# Глобальный топ-30 может быть заполнен испанцами/немцами (крупный релиз
# игры) — русских там не будет вовсе. Поэтому русскоязычных ищем ДВУМЯ
# путями: фильтр глобального топа + прямая проверка живых эфиров у
# известных русских стримеров (публичный GQL, без ключей).
# Список 2026-11-21 (запрос «добавь КАК МОЖНО БОЛЬШЕ РУССКИХ СТРИМЕРОВ»):
# 189 ЛОГИНОВ, все проверены запросом users(logins:) в GQL и подписчиками
# (собран скриптом scripts/discover_ru_twitch.py: живые RU-эфиры по 45
# популярным категориям + широкий пул кандидатов; иностранцы и битые ники
# отброшены). Кто из них в эфире, решает filter_by_language в
# auto_discover — язык должен быть ru/русский тайтл.
RU_CANDIDATE_LOGINS = [
    "s1mple bratishkinoff deepins02 evelone2004 paradeev1ch t2x2 koreshzy "
    "strogo kamet0 recrent dmitry_lixxx sasavot stray228 "
    "dangerlyoha stintik xrohat rostislav_999 zarbex skywhywalker jltomy "
    "zloyn shadowkekw ravshann mokrivskyi nix danila_gorilla stylishnoob4 "
    "rostikfacekid ewroon 5opka morphe_ya skyyart alohadancetv silvername "
    "xaryu stariy_bog tpabomah gtasty lenagol0vach otplol_ deko nooreax "
    "leb1ga leemodopi mafanyaking zxcursed metashi12 welovegames leva2k "
    "renatko krokoboss dyrachyo murzofix chopperinho melharucos sasatikk "
    "blackufa znat51 tolkin senator__se jokerdtv solo m3wsu daxak vovapain "
    "followkudes yukiofps14 rejiboi on_rus batora324 kopsteep dunduk imdankar "
    "kanonxo mistafaker dawgonosik adekvat happasc2 velyalive keliencs "
    "ghostik jerry256cs2 hibako tixerok kiryuhaa betboom_dota_ru d1pro4 "
    "shirooutsider rad0s66 syslov171 dirkaes kerimch1k shur4 samiyka d1sstric "
    "uniquee blindzonexgod sinedd92 amor_ed fc_dynamo rasty_airsoft "
    "blackrabbit342 zxjem gadjiev57 n_masteroff shosha_433 bublikxxl raythis "
    "shevchik666 mozol6ka shiishak zer0truel0ve exigent_wires firiondhistory "
    "betboom_dota_ru2 mainfinitytv praden vlad_derevyashka markjoviic "
    "mag4shkaaa breeezzx raze_iam gen1usat ezpvp_lover dobra_divka sh0kerix "
    "filshtein mr_blo mantaq wildanimalhs mrsplinter_tv impatiya zxcpulsedx "
    "pustyshkaa extreme4ik_play phenybut nymphayia mrrojer akson7777 riokka11 "
    "magamedtv05 finfuryfast369 dandepuzo elfejan unsella oleg_insec "
    "liveforsurf nagibml mellonty_wtf sirfugaaa magiik777 l1wenyt 6rusko12 "
    "petrosuyn effectikkkkk drugcat33 kebastr shezhinka isawyourunaway "
    "tyanli_ buzzzzzzzzzzzzzzzzzz vadimdanerian myafych etojeyulya bezp "
    "vladaholod zaychik_off untouchable_me artlasoff iicoyote quzm1ch "
    "roha_wot vyacheslavlivee mcpleh cardiomlbb sabellya oden_337 mlmlka "
    "awsdayy londyy_gg skifler_wot verona_forma rinaruin twist666g0d "
    "stanranli sakres_stream remanga despot1ck "
    # Дополнение 2026-11-24 (запрос: «включи ещё популярных русскоязычных»):
    # крупные каналы, которых не было в пуле; GQL-проба сама отсеет
    # переименованных и несуществующих ники — битые ники безвредны.
    "installment watermills fritern evelone192 lomazov jesusavgn edisonpts "
    "evronna chipsa illidanstr boombl4 kuplinov 5opka alinarin zhekan "
    "idisbalance sruslannder mart_vorona cybervandam "
]

# РУССКОЯЗЫЧНЫЕ YouTube-КАНАЛЫ для слежки (запрос «добавь как можно больше …
# и ютубов»): 7 каналов, каждый проверен yt-dlp (вкладка /streams существует,
# контент русский, стримят). Куплинов и Bratishkin стримят в основном тут.
# Вливаются в auto_channels при каждом автопоиске (см. auto_discover) —
# опрашиваются youtubei-API, дёшево.
RU_YOUTUBE_CHANNELS = [
    "https://www.youtube.com/@welovegames",      # WELOVEGAMES
    "https://www.youtube.com/@bratishkinoff",    # Bratishkin
    "https://www.youtube.com/@KuplinovPlay",     # Куплинов
    "https://www.youtube.com/@5opka",            # ФУГА TV
    "https://www.youtube.com/@stariy_bog",       # Артём Ка (Старый Бог)
    "https://www.youtube.com/@AlinaRin",         # Alina Rin (стримит GTA VI)
    "https://www.youtube.com/@Okisebi",          # Okisebi (GTA VI, RU)
]



def ru_probe_streams(logins=None):
    """Живые эфиры среди известных русскоязычных стримеров (GQL users+stream).

    Возвращает тот же формат, что twitch_top_streams: [{login, display,
    viewers, title, game, language}]. Ошибка сети -> [] — слежка по
    остальным каналам продолжается.
    """
    # Список может быть и «один ник — один элемент», и длинными строками с
    # несколькими никами через пробел: режем по пробелам и убираем дубли.
    raw = logins or RU_CANDIDATE_LOGINS
    seen = set()
    logins = []
    for x in raw:
        for tok in str(x).split():
            tok = tok.strip().strip(',').lower()
            if tok and tok not in seen:
                seen.add(tok)
                logins.append(tok)
    if not logins:
        return []
    # GQL users принимает НЕ БОЛЬШЕ 100 логинов за запрос: список вырос до
    # 189 — режем на пачки и объединяем (одна пачка молча теряла бы хвост).
    out = []
    for start in range(0, len(logins), 100):
        out.extend(_probe_streams_batch(logins[start:start + 100]))
    return out


def _probe_streams_batch(logins):
    """Одна пачка (до 100) пробы живых эфиров — тело старой ru_probe_streams."""
    body = {"query": ("query($logins:[String!]) { users(logins:$logins) "
                      "{ login displayName stream { title viewersCount "
                      "language game { displayName } } } }")}
    try:
        r = requests.post(TWITCH_GQL, json=dict(body, variables={"logins": logins}),
                          timeout=30, headers={"Client-ID": TWITCH_WEB_CLIENT_ID})
        if r.status_code != 200:
            log.warning("RU-проба Twitch: HTTP %s", r.status_code)
            return []
        j = r.json()
        if j.get("errors"):
            log.warning("RU-проба Twitch: %s",
                        str(j["errors"][0].get("message") or "")[:160])
            return []
    except Exception as e:
        log.warning("RU-проба недоступна: %s", e)
        return []
    out = []
    for u in (j.get("data") or {}).get("users") or []:
        # Twitch отдаёт null для несуществующих/переименованных логинов:
        # один битый ник не должен хоронить весь автопоиск
        if not isinstance(u, dict):
            continue
        st = u.get("stream")
        if not st or not u.get("login"):
            continue
        try:
            viewers = int(st.get("viewersCount") or 0)
        except (TypeError, ValueError):
            viewers = 0
        out.append({"login": str(u["login"]).lower(),
                    "display": str(u.get("displayName") or u["login"]),
                    "viewers": viewers,
                    "title": str(st.get("title") or "")[:120],
                    "game": str((st.get("game") or {}).get("displayName") or ""),
                    "language": str(st.get("language") or "").lower()})
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


def auto_discover(gh, cfg, top_fn=None, probe_fn=None):
    """Обновить auto_channels в subscriptions.json русскоязычным топом Twitch.

    Читает subscriptions.json из репозитория, находит живых русскоязычных
    (глобальный топ + проба известных русских каналов), кладёт их в ключ
    auto_channels (каналы человека в channels не трогаются). Если топ
    не изменился — коммита нет. Возвращает СПИСОК авто-ссылок (даже если
    не изменился), чтобы слежка подхватила их сразу, не дожидаясь
    следующего запуска. probe_fn — подмена пробы в тестах.
    """
    disc = (cfg or {}).get("discovery") or {}
    if disc.get("enabled", True) is False:
        return []
    count = int(disc.get("count") or DISCOVERY_COUNT)
    min_viewers = int(disc.get("min_viewers") or DISCOVERY_MIN_VIEWERS)
    disc_lang = str(disc.get("language") or DISCOVERY_LANGUAGE)
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
    # человек важнее топа + каналы-исключения (frametamer666 и др.)
    exclude = _subs_twitch_logins(user_links) | CHANNEL_BLACKLIST
    fetch = (top_fn or twitch_top_streams)(max(count + 5,
                                                DISCOVERY_TOP_FETCH))
    # ТОЛЬКО РУССКОЯЗЫЧНЫЕ: глобальный топ + прямая проба известных русских
    # стримеров (в день крупного релиза топ-30 забит испанцами/немцами —
    # без пробы русских там просто не найдётся).
    fetch = filter_by_language(fetch, disc_lang)
    ru_live = filter_by_language(
        (probe_fn or ru_probe_streams)(), disc_lang)
    by_login = {}
    for s in list(fetch) + list(ru_live):
        prev = by_login.get(s.get("login", ""))
        if prev is None or int(s.get("viewers") or 0) > int(prev.get("viewers") or 0):
            by_login[s["login"]] = s
    fetch = sorted(by_login.values(),
                   key=lambda s: int(s.get("viewers") or 0), reverse=True)
    log.info("Автопоиск: %d живых русскоязычных (топ+проба)", len(fetch))
    urls = discovery_urls(fetch, min_viewers=min_viewers, count=count,
                          exclude=exclude)
    if len(urls) < min(8, count) and min_viewers > DISCOVERY_FALLBACK_VIEWERS:
        # Русских топов с 2000+ зрителями сейчас не набралось — опускаем
        # планку, чтобы слежка не осталась полупустой (качество моментов
        # всё равно отсекает скучные клипы после нарезки).
        log.info("Русскоязычных с %d+ зрителями мало (%d) — опускаю планку до %d",
                 min_viewers, len(urls), DISCOVERY_FALLBACK_VIEWERS)
        urls = discovery_urls(fetch, min_viewers=DISCOVERY_FALLBACK_VIEWERS,
                              count=count, exclude=exclude)
    # + русскоязычные YouTube-каналы (КУПЛИНОВ, Bratishkin и др.): их
    # проверяет тот же цикл слежки — youtubei-опрос дешёвый, а стримят
    # они часто именно на YouTube.
    yt_have = {str(x).strip().rstrip("/").lower() for x in old_auto}
    for yu in RU_YOUTUBE_CHANNELS:
        if str(yu).strip().rstrip("/").lower() not in yt_have:
            urls.append(yu)
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
    """subscriptions.json + channels из config.yaml -> без дублей и без
    каналов-исключений (frametamer666 — запрос «всех кроме frametamer»)."""
    refs, seen = [], set()
    for s in list(subs):
        r = parse_link(s, source="subs")
        if not r:
            continue
        if r.platform == "twitch" and str(r.id or "").lower() in CHANNEL_BLACKLIST:
            log.info("Канал в стоп-листе — не слежу: %s", r.id)
            continue
        if dedup_key(r) not in seen:
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


def _blacklisted_twitch_login(url):
    """Логин Twitch из URL записи/клипа (None — не определить).

    twitch.tv/<login>/video/<id>, twitch.tv/<login>/clip/<slug>,
    clips.twitch.tv/<login>/<slug>. Голые clips.twitch.tv/<uuid> без логина
    не фильтруются (логин там не фигурирует).
    """
    m = re.search(r"(?:www\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/(?:video|clip)/",
                  str(url or ""))
    if not m:
        m = re.search(r"clips\.twitch\.tv/([A-Za-z0-9_]{3,25})/", str(url or ""))
    return m.group(1).lower() if m else None


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
        if _blacklisted_twitch_login(url) in CHANNEL_BLACKLIST:
            log.info("В стоп-листе — в очередь нарезки не берём: %s", url)
            continue                          # frametamer666 и другие исключения
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
    now = time.time()
    for status in ("in_progress", "queued"):
        try:
            out = subprocess.run(
                ["gh", "run", "list", "--repo", repo, "--workflow",
                 "monitor.yml", "--status", status,
                 "--limit", "10", "--json", "databaseId,createdAt"],
                capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                log.warning("gh run list: %s", (out.stderr or "")[:200])
                continue
            runs = json.loads(out.stdout or "[]")
            for r in runs:
                rid = str(r.get("databaseId") or "")
                if not rid or rid == my_id:
                    continue
                started = _parse_gh_time(r.get("createdAt"))
                if started and now - started > MONITOR_STALE_MIN * 60:
                    # ЗОМБИ: сессия висит дольше нормы и молча блокирует
                    # слежку («Monitor уже работает» без самого Monitor).
                    log.warning("Monitor-запуск %s висит %d мин (>%d) — "
                                "считаю его мёртвым, слежку возобновляю",
                                rid, int((now - started) / 60),
                                MONITOR_STALE_MIN)
                    continue
                return True
        except Exception as e:
            log.warning("gh run list не удался: %s", e)
    return False


def _parse_gh_time(s):
    """'2026-11-24T10:50:05Z' -> epoch; None, если не распарсилось."""
    try:
        import calendar
        t = time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
        return calendar.timegm(t)
    except (TypeError, ValueError):
        return None


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
              token_file=os.path.basename(tf),
              release_url_base=(f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', '')}"
                                f"/releases/download/{RELEASE_TAG}"))
    log.info("TikTok-автопостинг включён (секреты GitHub, режим API)")


# --- TikTok: отложенная очередь (клипы переживают смерть раннера) ------------

def tiktok_pending_load(gh, base):
    """Очередь неудачных постов из репозитория (список или пусто)."""
    try:
        raw, _sha = gh.get_file(TIKTOK_PENDING_JSON)
        if raw:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _commit_repo_json(gh, path, base, msg):
    """Закоммитить json-файл (если есть что сохранять; пусто — коммита нет)."""
    exists = os.path.isfile(os.path.join(base, path))
    raw_old, sha = gh.get_file(path)
    if not exists and raw_old is None:
        return False                    # нечего сохранять
    data = b"[]"
    if exists:
        with open(os.path.join(base, path), "rb") as f:
            data = f.read()
    gh.put_file(path, data, sha, msg)
    return True


def tiktok_pending_commit(gh, base, msg="tiktok: очередь публикации сохранена"):
    """Закоммитить отложенную очередь + историю публикаций TikTok (самолечение:
    клипы не теряются, повторы не случаются)."""
    try:
        ok = _commit_repo_json(gh, TIKTOK_PENDING_JSON, base, msg)
        try:
            with open(os.path.join(base, TIKTOK_PENDING_JSON), "rb") as f:
                n = len(json.loads(f.read().decode("utf-8") or "[]"))
        except Exception:
            n = -1
        if ok:
            log.info("TikTok: очередь публикации закоммичена (ждут публикации: %s)", n)
        try:
            ok2 = _commit_repo_json(gh, TIKTOK_POSTED_JSON, base,
                                    "tiktok: история публикаций обновлена")
        except Exception:
            ok2 = False
        if ok2:
            log.info("TikTok: история публикаций закоммичена")
    except Exception:
        log.exception("TikTok: не удалось закоммитить очередь публикации")


def tiktok_requeue(tk, gh, base, cap=4):
    """Вернуть неудачные клипы в постинг (самолечение 24/7).

    Пока TikTok отвечает «приложение без аудита» и попытка была недавно —
    не качаем все клипы зря: один клип-пробник проверит, разрешил ли TikTok
    публикацию. Как только разрешит — очередь разгрузится сама."""
    if tk is None:
        return 0
    try:
        tk.load_pending(tiktok_pending_load(gh, base))
    except Exception:
        log.exception("TikTok: очередь из репозитория не читается")
        return 0
    # Клипы, уже выходившие через selftest, в боевой пост не возвращаем:
    # убираем их из очереди (защита от дублей между путями публикации).
    try:
        raw, _sha = gh.get_file(TIKTOK_POSTED_JSON)
        posted = set(json.loads((raw or b"[]").decode("utf-8")) or [])
        for eid in list(posted):
            tk.discard_pending(eid)
    except Exception:
        pass
    probe = any(tiktok_mod.UNAUDITED_MARK in str(e.get("last_error") or "")
                and time.time() - float(e.get("ts") or 0) < 90 * 60
                for e in tk.pending_entries())
    limit = 1 if probe else max(1, cap)

    def _download(url, dest):
        with requests.get(url, stream=True, timeout=600,
                          headers={"User-Agent": "StreamClipper"}) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)

    try:
        return tk.requeue_pending(_download, limit=limit)
    except Exception:
        log.exception("TikTok: возврат очереди не удался")
        return 0


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
        try:
            discover_youtube_hype(gh, cfg)    # хайповые RU GTA6 -> очередь
        except Exception:
            log.exception("YouTube-дискавери не удался")
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
    # Разовая чистка ВСЕХ старых клипов — маркер wipe_clips.now в репо.
    # Чистим ДО слежки: релиз опустошается, клипы на сайте обновятся сами.
    try:
        wipe_old_clips_once(gh)
    except Exception:
        log.exception("Разовая чистка клипов упала — продолжаю смену")

    auto_state = {"urls": [], "last": 0.0}
    set_state = {"s": None, "last": 0.0}
    yt_disc_state = {"last": 0.0}          # YouTube-дискавери (раз в ~30 мин)

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

    def maybe_youtube_discover():
        """Хайповые RU GTA6-видео — раз в ~30 минут, чтобы не жечь время раннера."""
        if gh is None:
            return
        if time.time() - yt_disc_state["last"] < YT_DISCOVER_EVERY:
            return
        yt_disc_state["last"] = time.time()
        try:
            discover_youtube_hype(gh, cfg)
        except Exception:
            log.exception("YouTube-дискавери не удался")

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
            maybe_youtube_discover()          # хайповые RU GTA6 -> очередь
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
    # Антибан-паузы в CI ужаты (60..180 c): очередь публикаций успевает
    # разгрузиться до конца сессии — клипы, нарезанные в последнюю минуту,
    # тоже уходят в TikTok, а не умирают вместе с раннером.
    tkcfg = cfg.setdefault("tiktok", {})
    if tkcfg.get("enabled") and not tkcfg.get("dry_run"):
        tkcfg["delay_min"] = min(int(tkcfg.get("delay_min") or 60), 60)
        tkcfg["delay_max"] = min(int(tkcfg.get("delay_max") or 300), 180)
        log.info("TikTok: антибан-паузы в CI %d..%d c", tkcfg["delay_min"],
                 tkcfg["delay_max"])

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
    if pl.tiktok is not None and push is not None:
        pl.tiktok.notify = push            # тревога TikTok на телефон (ntfy/Telegram)
    # Самолечение TikTok: клипы, которые не опубликовались в прошлый раз
    # (сеть/отказ TikTok/нет аудита), возвращаются в очередь и уйдут сейчас.
    try:
        tiktok_requeue(pl.tiktok, gh, base, cap=3)
    except Exception:
        log.exception("TikTok: requeue очереди не удался")

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
        # клипы из очереди TikTok, не успевшие уйти, уже в pending (pl.stop
        # сливает их); закоммитим очередь — следующий запуск её подхватит
        tiktok_pending_commit(gh, base)
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
    # Разовая чистка ВСЕХ старых клипов (маркер wipe_clips.now в репо) —
    # до нарезки новых: старые не должны смешиваться с новыми.
    try:
        wipe_old_clips_once(gh)
    except Exception:
        log.exception("Разовая чистка клипов упала — продолжаю нарезку")
    providers = notify_cfg_from_env(cfg)
    if providers:
        log.info("Уведомления включены: %s", ", ".join(providers))
    push = Notifier(cfg.get("notify") or {}) if providers else None
    pub = GitHubPublisher(gh, workdir=base, push=push)
    # TikTok-автопостинг из секретов Actions: VOD-клипы тоже постятся, но
    # ТОЛЬКО хайповые (порог tiktok.min_score) и не больше max_per_run за запуск.
    apply_tiktok_secrets(cfg)
    tk_cfg = cfg.get("tiktok") or {}
    tk = None
    tk_posts = 0
    tk_cap = max(1, int(tk_cfg.get("max_per_run") or 4))
    if tk_cfg.get("enabled") and not tk_cfg.get("dry_run"):
        # антибан-паузы в CI короче: очередь должна успеть разгрузиться за запуск
        tk_cfg["delay_min"] = min(int(tk_cfg.get("delay_min") or 60), 60)
        tk_cfg["delay_max"] = min(int(tk_cfg.get("delay_max") or 300), 180)
        try:
            cand = TikTokPoster(tk_cfg, base)
            if cand.ready():
                cand.start()
                tk = cand
                if push is not None:
                    tk.notify = push        # тревога TikTok на телефон
                log.info("TikTok: постинг включён (мин. оценка %d, до %d постов "
                         "за запуск)", tk.min_score, tk_cap)
            else:
                log.warning("TikTok: постер не готов — VOD-клипы не постятся")
        except Exception:
            log.exception("TikTok-постер не поднялся — VOD-клипы не постятся")
    # Самолечение TikTok: возврат неудачных клипов в очередь (лимит входит
    # в антибан-кап запуска). Пока без аудита — один клип-пробник.
    try:
        tk_requeued = tiktok_requeue(tk, gh, base, cap=tk_cap)
        tk_posts += tk_requeued
    except Exception:
        log.exception("TikTok: requeue очереди не удался")
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
        """Читаем-меняем-пишем: параллельный коммит (Check live кладёт новые
        задачи в jobs.json, пока идёт нарезка) даёт HTTP 409. После конфликта
        перечитываем свежий sha и пробуем снова — раньше это падало в цикл
        «очередь после ошибки» и валило весь запуск Clip video."""
        for attempt in range(5):
            data, sha = load_json_state(gh, path)
            if jobs_list is not None:
                data["jobs"] = jobs_list
                data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if done_set is not None:
                data["done"] = sorted(done_set)[-5000:]
                data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                put_json_state(gh, path, data, sha, msg)
                return
            except RuntimeError as e:
                if "409" not in str(e) or attempt == 4:
                    raise
                log.warning("Конфликт записи %s (попытка %d) — перечитываю sha",
                            path, attempt + 1)
                time.sleep(2 + attempt * 3)

    jobs_data, _ = load_json_state(gh, JOBS_JSON)
    proc_data, _ = load_json_state(gh, PROCESSED_JSON)
    jobs = [j for j in (jobs_data.get("jobs") or []) if isinstance(j, dict)]
    done = set(proc_data.get("done") or [])
    log.info("Очередь нарезки: %d задач, уже сделано %d", len(jobs), len(done))
    if not jobs:
        log.info("Очередь пуста — нарезать нечего. Ссылки на видео кидаются на сайт "
                 "(Каналы) и попадают сюда через Check live")
        if tk is not None and tk.status().get("queued"):
            # вернулись отложенные TikTok-клипы: дожидаемся публикации — иначе
            # раннер умрёт вместе с недопощенными клипами
            log.info("TikTok: дожидаемся публикации отложенных клипов (до 20 мин)")
            drain = time.time() + 20 * 60
            while tk.status().get("queued") and time.time() < drain:
                time.sleep(5)
            st_tk = tk.status()
            log.info("TikTok за запуск: опубликовано %d, осталось в очереди %d",
                     st_tk["posted"], st_tk["queued"])
        if tk is not None:
            tk.stop()
        tiktok_pending_commit(gh, base)
        return 0

    deadline = time.time() + max(10, int(max_minutes)) * 60
    made = 0
    skips = 0
    bored = 0
    holdout = []      # отложенные как «скучные»: (path, score, display, platform, smeta, url, start)
    while jobs and made < max_jobs and time.time() < deadline - 12 * 60:
        job = jobs[0]
        key = vod_key(job)
        # Ручной запрос «резать всё» с сайта: гейты качества отключены.
        force = bool(job.get("force"))
        # YouTube с раннеров закрыт бот-чеком («Sign in to confirm…»):
        # задача жгла 3 попытки × ~30 c и забивала очередь. Автопоиск
        # пропускаем мгновенно (на компьютере — как раньше), а вот РУЧНЫЕ
        # запросы с сайта пользователь ждёт: их пробуем по-настоящему —
        # yt-dlp сам перебирает плеер-клиенты (android_vr, tv_simply…).
        if (os.environ.get("GITHUB_ACTIONS")
                and "youtube." in str(job.get("url") or "")
                and not force):
            log.info("YouTube-задача пропущена (бот-чек на Actions): %s",
                     job.get("url"))
            jobs.pop(0)
            done.add(key)
            save(JOBS_JSON, jobs_list=jobs,
                 msg="vod: youtube пропущен (бот-чек)")
            save(PROCESSED_JSON, done_set=done,
                 msg="vod: отмечаю пропущенное (yt)")
            continue
        if force:
            log.info("РУЧНОЙ ЗАПРОС «резать всё»: %s — гейты «лицо/речь/скука» "
                     "и порог TikTok отключены, клипы пойдут все", job.get("url"))
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
            try:
                out, smeta = vod_mod.process_clip(src, clips_dir, workdir, cfg,
                                                  display, platform,
                                                  vcodec_args=vcodec_args,
                                                  banner_path=banner_path,
                                                  require_face=not force)
            except vod_mod.NoFaceClip:
                # Жалоба пользователя «опять без лица»: клипы с эфиров без
                # вебкамеры (турниры, стримы сайтов) не публикуются вовсе.
                # Окно выбрасывается, берём следующее (лимит — не сжечь run).
                log.info("%s: в окне нет стримера (вебкамера не найдена) — "
                         "клип не собирается", display)
                skips += 1
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
                save(JOBS_JSON, jobs_list=jobs,
                     msg="vod: окно без лица пропущено")
                continue
            # Настройка «пропускать клипы без речи»: в окне не распознано ни
            # слова (или речи почти нет — см. MIN_WORDS_CLIP/MIN_SPEECH_PCT) —
            # выбрасываем и берём следующее, чтобы вместо тихого скучного
            # клипа был говорящий (лимит на запуск — не сжечь run).
            words_n = int(smeta.get("words") or 0)
            speech_pct = int(smeta.get("speech_pct") or 0)
            low_speech = (words_n < MIN_WORDS_CLIP
                          or speech_pct < MIN_SPEECH_PCT)
            if (not force and settings.get("skip_no_speech")
                    and (not smeta.get("ok") or low_speech)
                    and skips < NO_SPEECH_SKIP_MAX):
                skips += 1
                try:
                    os.remove(out)
                except OSError:
                    pass
                log.info("Окно без речи (слов %d, речи %d%%) (%d/%d) — "
                         "пропускаю, беру следующее",
                         words_n, speech_pct, skips, NO_SPEECH_SKIP_MAX)
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
            # база 50 (они уже отобраны людьми). НЕ трогаем имя base — оно
            # занято рабочей папкой run_vod (os.path.join(base, ...) в конце;
            # прод 2026-11-21: base = 50.0 ломало vod_remaining.txt).
            qbase = 50.0
            if job.get("kind") == "video" and job.get("scores"):
                try:
                    qbase = max(5.0, min(95.0, 50.0 + 12.0 * float(job["scores"][0])))
                except (TypeError, ValueError):
                    qbase = 50.0
            min_score = float(cfg.get("_min_publish") or 40.0)
            try:
                final_score, qparts = vod_mod.clip_quality(out, smeta, base=qbase)
            except Exception:
                log.exception("Оценка качества не удалась — публикую как есть")
                final_score, qparts = qbase, {}
            # ЖЁСТКИЙ БРАК (статичный кадр ~2 МБ / обрезок < 45 с) выбрасывается
            # совсем — он не попадает даже в спасательный круг публикации.
            hard_reject = bool(qparts.get("hard"))
            if (not force and final_score < min_score
                    and (hard_reject or bored < BORED_SKIP_MAX)):
                bored += 1
                if hard_reject:
                    try:
                        os.remove(out)
                    except OSError:
                        pass
                    log.info("БРАК (%s): оценка %.0f — окно выброшено совсем "
                             "(%d брака за запуск)", qparts.get("hard"),
                             final_score, bored)
                else:
                    holdout.append((out, final_score, display, platform, smeta,
                                    url, int(start or 0)))
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
            if tk and tk_posts < tk_cap:
                # В TikTok — ТОЛЬКО говорящие хайповые: нет связной речи — не
                # постим вовсе (тихие клипы никогда не залетают в рекомендации).
                # РУЧНЫЕ запросы с сайта (force) постим все: пользователь сам
                # решил резать весь ролик — его воля. Кап за запуск остаётся.
                speech_ok = (bool(smeta.get("ok"))
                             and words_n >= 10
                             and speech_pct >= 10)
                if not speech_ok and not force:
                    log.info("TikTok: пропустил — нет связной речи (слов %d, "
                             "речи %d%%), тихие клипы не постим", words_n,
                             speech_pct)
                elif force or final_score >= tk.min_score:
                    tk_posts += 1
                    tk.enqueue(out, tiktok_mod.build_caption(
                        {"title": display, "channel": display,
                         "platform": platform}, tk.hashtags))
                    log.info("TikTok: в очередь (%d/%d за запуск, оценка %.0f)",
                             tk_posts, tk_cap, final_score)
                else:
                    log.info("TikTok: пропустил — оценка %.0f < %d "
                             "(только хайповые)", final_score, tk.min_score)
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
    # одного клипа — публикуем лучший ГОВОРЯЩИЙ из отложенных, чтобы лента не
    # пустовала. Клипы без речи не публикуем даже тут (требование: «не делать
    # клипы, где вообще не говорят»).
    if made == 0 and holdout:
        spoken = [x for x in holdout
                  if int((x[4] or {}).get("words") or 0) >= 8]
        if not spoken:
            log.info("Спасательный круг не нужен: говорящих клипов среди "
                     "отложенных нет — безречевые не публикуем")
        else:
            best = max(spoken, key=lambda x: x[1])
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
                    log.info("За запуск всё было скучным — опубликовал лучший "
                             "из ГОВОРЯЩИХ отложенных (оценка %.0f, слов %d), "
                             "чтобы лента не пустовала", bscore,
                             int(bsmeta.get("words") or 0))
            except Exception:
                log.exception("Не удалось опубликовать лучший из отложенных")
    for item in holdout:
        try:
            os.remove(item[0])
        except OSError:
            pass
    if tk:
        # дожидаемся публикации всех queued-клипов (до ~25 мин): пока постинг
        # идёт, ротированный refresh-токен сохраняется в секреты репозитория
        drain = time.time() + 25 * 60
        while tk.status()["queued"] and time.time() < drain:
            time.sleep(5)
        st_tk = tk.status()
        log.info("TikTok за запуск: опубликовано %d, осталось в очереди %d",
                 st_tk["posted"], st_tk["queued"])
        tk.stop()
    # очередь TikTok (успешно опубликованные клипы из неё удалены) -> репозиторий
    tiktok_pending_commit(gh, base)
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
