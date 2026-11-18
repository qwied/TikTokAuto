"""Мониторинг статусов стримов Twitch и YouTube — проверка почти каждую секунду.

Список ссылок — динамический: каналы можно добавлять и убирать на ходу
(правка links.txt или команда боту). Статус «в эфире или нет» проверяется
быстрыми внутренними API самих платформ, без ключей и логинов:

    Twitch  — GQL: ОДИН пакетный запрос на все каналы (~0.1 c), каждые ~3 c;
    YouTube — внутренний API youtubei, вкладка «стримы» канала (~0.5 c),
              каждые ~8 c на канал (несколько каналов — параллельно).

Поэтому выход стримера в эфир замечается за считанные секунды, и программа
сразу же подключается к записи. Если быстрые API недоступны, автоматически
включаются запасные пути: streamlink (Twitch) и yt-dlp (YouTube).
"""

import json
import logging
import queue
import random
import re
import threading
import time
from dataclasses import dataclass

import requests

from models import ChannelRef
from utils import run_cmd

log = logging.getLogger("monitor")

# --- Twitch: GQL (тот же публичный client-id, что использует веб-плеер) ---------

GQL_URL = "https://gql.twitch.tv/gql"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_CHUNK = 50                      # каналов в одном запросе (с запасом)

# --- YouTube: внутренний API youtubei -------------------------------------------

YT_BROWSE = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
YT_RESOLVE = ("https://www.youtube.com/youtubei/v1/navigation/resolve_url"
              "?prettyPrint=false")
YT_CTX = {"context": {"client": {"clientName": "WEB",
                                 "clientVersion": "2.20241126.01.00", "hl": "en"}}}
YT_STREAMS_PARAMS = "EgdzdHJlYW1z8gYECgJ6AA%3D%3D"     # вкладка «Трансляции»

TWITCH_QUALITY_PREF = {
    "720p": ["720p60", "720p", "720p_high", "medium"],
    "1080p": ["1080p60", "1080p", "1080p60 (source)", "source", "best"],
    "best": ["best", "source"],
}

YT_FORMAT = {
    "720p": "b[height<=720][vcodec^=avc1]/b[height<=720]/bv*[height<=720]+ba/b",
    "1080p": "b[height<=1080][vcodec^=avc1]/b[height<=1080]/bv*[height<=1080]+ba/b",
    # best = максимальное доступное качество; H.264 в приоритете — он без
    # перекодирования пишется в буфер и совместим с любым телефоном
    "best": "b[vcodec^=avc1]/b",
}


# --- чистые функции разбора ответов (покрыты тестами без сети) -------------------

def build_gql_query(logins):
    """GraphQL-запрос со псевдонимами: один запрос на список каналов."""
    fields = "login displayName stream { type title viewersCount createdAt game { displayName } }"
    parts = [f'u{i}: user(login: "{login}") {{ {fields} }}'
             for i, login in enumerate(logins)]
    return "{ " + " ".join(parts) + " }"


def parse_gql_response(data, channels):
    """Ответ GQL -> {key: (live, title, viewers)} по каналам из запроса."""
    out = {}
    node_data = (data or {}).get("data") or {}
    for i, ch in enumerate(channels):
        node = node_data.get(f"u{i}")
        if node is None:
            continue
        st = node.get("stream") or {}
        if str(st.get("type") or "") == "live":
            try:
                viewers = int(st.get("viewersCount") or 0)
            except (TypeError, ValueError):
                viewers = 0
            out[ch.key] = (True, str(st.get("title") or ""), viewers)
        else:
            out[ch.key] = (False, "", 0)
    return out


def _badge_live_in(node):
    """Есть ли в поддереве бейдж «LIVE» (новый формат YouTube)."""
    found = []

    def visit(o):
        if isinstance(o, dict):
            b = o.get("thumbnailBadgeViewModel")
            if isinstance(b, dict):
                text = str(b.get("text") or "").upper()
                style = str(b.get("badgeStyle") or "").upper()
                if text == "LIVE" or style.endswith("_LIVE"):
                    found.append(True)
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(node or {})
    return bool(found)


def _lockup_title(lv):
    try:
        t = lv["metadata"]["lockupMetadataViewModel"]["title"]
        if isinstance(t, dict):
            return str(t.get("content") or "")
    except (KeyError, TypeError):
        pass
    return ""


def _renderer_is_live(r):
    """Старые отрисовки YouTube: videoRenderer / gridVideoRenderer."""
    if str(r.get("liveBroadcastContent") or "").lower() == "live":
        return True
    vc = json.dumps(r.get("viewCountText") or {}, ensure_ascii=False).lower()
    if "watching" in vc:
        return True
    for b in r.get("badges") or []:
        if '"live"' in json.dumps(b, ensure_ascii=False).lower():
            return True
    return False


def _renderer_title(r):
    t = r.get("title") or {}
    if isinstance(t, dict):
        runs = t.get("runs") or []
        if runs and isinstance(runs[0], dict):
            return str(runs[0].get("text") or "")
        return str(t.get("simpleText") or "")
    return str(t)


def yt_parse_streams_tab(data):
    """Вкладка «Трансляции» канала -> (video_id, title) текущего эфира."""
    result = ("", "")

    def visit(node):
        nonlocal result
        if isinstance(node, dict):
            lv = node.get("lockupViewModel")
            if isinstance(lv, dict):
                vid = str(lv.get("contentId") or "")
                if vid and _badge_live_in(lv.get("contentImage") or {}):
                    result = (vid, _lockup_title(lv))
                    return True
            for key in ("videoRenderer", "gridVideoRenderer", "radioRenderer"):
                r = node.get(key)
                if isinstance(r, dict):
                    vid = str(r.get("videoId") or "")
                    if vid and _renderer_is_live(r):
                        result = (vid, _renderer_title(r))
                        return True
            for v in node.values():
                if visit(v):
                    return True
        elif isinstance(node, list):
            for v in node:
                if visit(v):
                    return True
        return False

    visit(data)
    return result


def yt_parse_resolve(data):
    """Ответ navigation/resolve_url -> id канала (UC...)."""
    found = []

    def visit(o):
        if isinstance(o, dict):
            be = o.get("browseEndpoint")
            if isinstance(be, dict):
                bid = str(be.get("browseId") or "")
                if bid.startswith("UC"):
                    found.append(bid)
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(data or {})
    return found[0] if found else ""


@dataclass
class ChannelState(ChannelRef):
    """Один канал в списке наблюдения + его текущее состояние."""

    is_live: bool = False
    title: str = ""
    viewers: int = 0

    @classmethod
    def from_ref(cls, ref):
        st = cls(platform=ref.platform, id=ref.id, video_id=ref.video_id,
                 video_url=ref.video_url, raw=ref.raw, source=ref.source)
        if isinstance(ref, ChannelState):
            st.is_live = ref.is_live
            st.title = ref.title
            st.viewers = ref.viewers
        return st

    @property
    def is_direct_video(self):
        """True — пользователь кинул прямую ссылку на трансляцию (watch?v=...)."""
        return bool(self.video_url)


def _state_from(item):
    """ChannelRef / dict из config.yaml -> ChannelState."""
    if isinstance(item, ChannelRef):
        return ChannelState.from_ref(item)
    st = ChannelState(str(item.get("platform", "")), str(item.get("id", "")))
    st.source = "config"
    st.raw = st.id
    return st


class StreamMonitor(threading.Thread):
    """Опрашивает каналы и сообщает о переходах онлайн/оффлайн.

    Список каналов меняется на ходу: add_channel / remove_channel /
    sync_channels. Новые каналы проверяются немедленно — если стрим уже идёт,
    запись включается через секунды после добавления ссылки.
    """

    def __init__(self, channels=None, quality="720p", poll_twitch=3,
                 poll_youtube=8, yt_workers=3, cookies_file=None,
                 cookies_from_browser=None, links_file=None, refresh_refs=None,
                 on_live=None, on_offline=None):
        super().__init__(daemon=True, name="StreamMonitor")
        self.quality = str(quality)
        self.poll = {"twitch": max(2, int(poll_twitch)),
                     "youtube": max(5, int(poll_youtube))}
        self.yt_workers = max(1, int(yt_workers))
        self.cookies_file = str(cookies_file or "")
        self.cookies_from_browser = str(cookies_from_browser or "")
        self.links_file = links_file          # объект LinksFile (следит за правками)
        self.refresh_refs = refresh_refs      # callable -> актуальный список ChannelRef
        self.on_live = on_live or (lambda ch: None)
        self.on_offline = on_offline or (lambda ch: None)

        self.channels = []
        self._lock = threading.RLock()
        self._stop = threading.Event()

        # состояние опросов
        self._yt_next = {}            # key -> время следующей проверки YouTube
        self._yt_inflight = set()     # каналы, проверяемые прямо сейчас
        self._yt_q = queue.Queue()
        self._yt_browse = {}          # key -> browseId канала (UC...)
        self._yt_fails = {}           # key -> подряд неудачных быстрых проверок
        self._yt_slow_last = {}       # key -> время последней проверки через yt-dlp
        self._sl_next = {}            # key -> следующая проверка через streamlink
        self._twitch_backoff = 1.0    # множитель паузы при недоступности GQL

        self._gql = requests.Session()
        # Важно: у внутреннего API YouTube НЕ должно быть браузерного User-Agent —
        # YouTube считает такой запрос подозрительным (без cookies) и отвечает 403.
        self._yt = requests.Session()

        for c in channels or []:
            st = _state_from(c)
            if st.platform not in ("twitch", "youtube") or not st.id:
                log.warning("Пропускаю некорректную ссылку: %r", c)
                continue
            self.channels.append(st)

    # --- управление списком каналов --------------------------------------------------

    def add_channel(self, ref):
        """Добавить канал в слежку. False, если уже есть."""
        st = _state_from(ref)
        if st.platform not in ("twitch", "youtube") or not st.id:
            return False
        with self._lock:
            if any(c.key == st.key for c in self.channels):
                return False
            self.channels.append(st)
            self._yt_next[st.key] = 0.0      # YouTube: проверить немедленно
            self._yt_fails[st.key] = 0
        log.info("Теперь слежу за: %s (%s)", st.display(), st.platform)
        return True

    def remove_channel(self, key):
        """Убрать канал из слежки. Если он в эфире — придёт on_offline."""
        removed = None
        with self._lock:
            for c in list(self.channels):
                if c.key == key:
                    self.channels.remove(c)
                    self._yt_next.pop(c.key, None)
                    self._yt_fails.pop(c.key, None)
                    removed = c
                    break
        if removed is None:
            return False
        log.info("Убрал из списка: %s", removed.display())
        if removed.is_live:
            removed.is_live = False
            try:
                self.on_offline(removed)
            except Exception:
                log.exception("on_offline вызвал ошибку")
        return True

    def sync_channels(self, refs):
        """Привести список к переданному (после правки links.txt)."""
        wanted = []
        seen = set()
        for r in refs:
            st = _state_from(r)
            if st.platform in ("twitch", "youtube") and st.id and st.key not in seen:
                seen.add(st.key)
                wanted.append(st)
        with self._lock:
            current = {c.key for c in self.channels}
            target = {s.key for s in wanted}
        for st in wanted:
            if st.key not in current:
                self.add_channel(st)
        for key in list(current - target):
            self.remove_channel(key)

    def has_channel(self, key):
        with self._lock:
            return any(c.key == key for c in self.channels)

    def snapshot(self):
        with self._lock:
            return list(self.channels)

    def status_rows(self):
        """Для /status: [(platform, короткое имя, is_live, title, viewers)]."""
        return [(c.platform, c.short_id(), c.is_live, c.title, int(c.viewers or 0))
                for c in self.snapshot()]

    # --- основной цикл -----------------------------------------------------------------

    def stop(self):
        self._stop.set()

    def run(self):
        chans = self.snapshot()
        log.info("Слежу за %d канал(ами): %s", len(chans),
                 ", ".join(f"{c.platform}:{c.short_id()}" for c in chans) or "список пуст")
        log.info("Выход в эфир замечаю за ~%d–%d c и сразу подключаюсь к записи",
                 min(self.poll.values()), max(self.poll.values()))
        threading.Thread(target=self._twitch_loop, daemon=True,
                         name="twitch-poll").start()
        threading.Thread(target=self._youtube_scheduler, daemon=True,
                         name="youtube-scheduler").start()
        for i in range(self.yt_workers):
            threading.Thread(target=self._youtube_worker, daemon=True,
                             name=f"youtube-worker-{i}").start()
        while not self._stop.is_set():
            self._watch_links_file()
            self._stop.wait(1.0)

    def _watch_links_file(self):
        """links.txt поправили вручную — обновляем список каналов."""
        if self.links_file is None or self.refresh_refs is None:
            return
        try:
            if not self.links_file.changed():
                return
        except Exception:
            return
        try:
            self.sync_channels(self.refresh_refs())
        except Exception:
            log.exception("Не удалось обновить список ссылок из файла")

    # --- применение результата проверки -------------------------------------------------

    def _apply(self, ch, live, title, viewers=0, video_id=""):
        """Обновить состояние канала; на переходах вызвать on_live/on_offline."""
        with self._lock:
            if live and not ch.is_live:
                ch.is_live = True
                ch.title = title or ch.title
                ch.viewers = int(viewers or 0)
                if video_id:
                    ch.video_id = video_id
                log.info("СТРИМ НАЧАЛСЯ: %s (%s) — подключаюсь к записи немедленно",
                         ch.short_id(), ch.platform)
                if title:
                    log.info("Название: %s", title)
                self._fire_on_live(ch)
            elif not live and ch.is_live:
                ch.is_live = False
                ch.viewers = 0
                log.info("СТРИМ ЗАКОНЧИЛСЯ: %s (%s)", ch.short_id(), ch.platform)
                self._fire_offline(ch)
            else:
                if title and title != ch.title:
                    ch.title = title
                if viewers:
                    ch.viewers = int(viewers)
                if live and video_id and ch.video_id and video_id != ch.video_id:
                    # трансляция перезапущена новым видео: запись сама переключится
                    log.info("YouTube %s: новая трансляция (%s) — запись переключится сама",
                             ch.short_id(), video_id)
                    ch.video_id = video_id

    def _fire_on_live(self, ch):
        """on_live в отдельном потоке: подключение не тормозит опрос."""
        st = ChannelState.from_ref(ch)
        threading.Thread(target=self._on_live_safe, args=(st,), daemon=True,
                         name=f"connect-{st.short_id()}").start()

    def _fire_offline(self, ch):
        st = ChannelState.from_ref(ch)
        threading.Thread(target=self._on_offline_safe, args=(st,), daemon=True,
                         name=f"disconnect-{st.short_id()}").start()

    def _on_live_safe(self, ch):
        try:
            self.on_live(ch)
        except Exception:
            log.exception("on_live вызвал ошибку")

    def _on_offline_safe(self, ch):
        try:
            self.on_offline(ch)
        except Exception:
            log.exception("on_offline вызвал ошибку")

    # --- Twitch: пакетный GQL ------------------------------------------------------------

    def _twitch_loop(self):
        while not self._stop.is_set():
            pause = self.poll["twitch"] * self._twitch_backoff + random.uniform(0, 1)
            if self._stop.wait(pause):
                break
            chans = [c for c in self.snapshot() if c.platform == "twitch"]
            if not chans:
                continue
            results = self._twitch_batch(chans)
            if results is None:
                # GQL недоступен — запасной путь streamlink с умеренной частотой
                self._twitch_backoff = min(self._twitch_backoff * 2, 20.0)
                now = time.time()
                for ch in chans:
                    if now < self._sl_next.get(ch.key, 0.0):
                        continue
                    self._sl_next[ch.key] = now + 15.0
                    try:
                        live, title = self._twitch_via_streamlink(ch)
                    except Exception:
                        log.exception("streamlink не смог проверить %s", ch.display())
                        live, title = False, ""
                    self._apply(ch, live, title)
                continue
            self._twitch_backoff = 1.0
            for ch in chans:
                res = results.get(ch.key)
                if res is None:
                    continue
                self._apply(ch, res[0], res[1], res[2])

    def _twitch_batch(self, chans):
        """Один GQL-запрос на все Twitch-каналы. None — запрос не удался."""
        valid = []
        for ch in chans:
            login = re.sub(r"[^A-Za-z0-9_]", "", ch.id)
            if login:
                valid.append((ch, login))
            else:
                log.warning("Twitch: некорректный ник канала: %r", ch.id)
        if not valid:
            return {}
        merged = {}
        for start in range(0, len(valid), GQL_CHUNK):
            chunk = valid[start:start + GQL_CHUNK]
            query = build_gql_query(login for _c, login in chunk)
            try:
                r = self._gql.post(GQL_URL, json={"query": query},
                                   headers={"Client-ID": GQL_CLIENT_ID}, timeout=10)
            except Exception as e:
                log.warning("Twitch GQL недоступен (%s) — запасной путь: streamlink", e)
                return None
            if r.status_code == 429:
                log.warning("Twitch: лимит запросов (429) — увеличьте monitor.poll_twitch")
                return None
            if r.status_code != 200:
                log.warning("Twitch GQL: HTTP %s — запасной путь: streamlink", r.status_code)
                return None
            try:
                data = r.json()
            except ValueError:
                log.warning("Twitch GQL: некорректный ответ")
                return None
            if not isinstance(data, dict) or "data" not in data:
                log.warning("Twitch GQL: неожиданный формат ответа")
                return None
            merged.update(parse_gql_response(data, [ch for ch, _l in chunk]))
        return merged

    def _twitch_via_streamlink(self, ch):
        """Проверка Twitch через streamlink (запасной путь)."""
        rc, out, _err = run_cmd(["streamlink", ch.page_url(), "--json"], timeout=45)
        if rc != 0 or not out.strip():
            return False, ""
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return False, ""
        streams = data.get("streams") or {}
        meta = data.get("metadata") or {}
        return bool(streams), str(meta.get("title") or "")

    # --- YouTube: внутренний API + yt-dlp запасной ---------------------------------------

    def _youtube_scheduler(self):
        """Раздаёт каналы YouTube, время проверки которых подошло."""
        while not self._stop.is_set():
            now = time.time()
            for ch in self.snapshot():
                if ch.platform != "youtube":
                    continue
                if self._yt_next.get(ch.key, 0.0) > now:
                    continue
                if ch.key in self._yt_inflight:
                    continue
                self._yt_inflight.add(ch.key)
                self._yt_q.put(ch)
            self._stop.wait(0.5)

    def _youtube_worker(self):
        while not self._stop.is_set():
            try:
                ch = self._yt_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if self._stop.is_set():
                self._yt_inflight.discard(ch.key)
                break
            try:
                live, title, vid = self._youtube_check(ch)
            except Exception:
                log.exception("Ошибка опроса YouTube %s", ch.key)
                live, title, vid = False, "", ""
            self._apply(ch, live, title, 0, vid)
            self._yt_next[ch.key] = time.time() + self.poll["youtube"] + random.uniform(0, 2)
            self._yt_inflight.discard(ch.key)

    def _youtube_check(self, ch):
        """(live, title, video_id) для YouTube-ссылки: быстрый API + yt-dlp при сбоях."""
        if ch.is_direct_video:
            live, title = self._yt_video_status(ch.video_id)
            return live, title, ch.video_id
        try:
            live, title, vid = self._yt_channel_fast(ch)
            self._yt_fails[ch.key] = 0
            return live, title, vid
        except Exception as e:
            fails = self._yt_fails.get(ch.key, 0) + 1
            self._yt_fails[ch.key] = fails
            log.warning("YouTube: быстрый опрос %s не удался (%s)", ch.display(), e)
        if (self._yt_fails.get(ch.key, 0) >= 3
                and time.time() - self._yt_slow_last.get(ch.key, 0.0) >= 90):
            self._yt_slow_last[ch.key] = time.time()
            log.info("YouTube %s: проверяю через yt-dlp (внутренний API недоступен)",
                     ch.display())
            return self._yt_channel_status_slow(ch)
        return False, "", ""

    def _yt_channel_fast(self, ch):
        """Быстрая проверка канала через youtubei (~0.5 c). Исключения = сбой."""
        bid = self._yt_browse.get(ch.key)
        if not bid:
            bid = self._yt_resolve_browse_id(ch)
            if not bid:
                raise RuntimeError("не удалось определить id канала YouTube")
            self._yt_browse[ch.key] = bid
        r = self._yt.post(YT_BROWSE,
                          json={**YT_CTX, "browseId": bid, "params": YT_STREAMS_PARAMS},
                          timeout=12)
        if r.status_code != 200:
            raise RuntimeError(f"browse: HTTP {r.status_code}")
        vid, title = yt_parse_streams_tab(r.json())
        return (bool(vid), title, vid)

    def _yt_resolve_browse_id(self, ch):
        """Ссылку на канал (@handle, /c/, /user/) -> внутренний id UC..."""
        url = ch.page_url()
        m = re.search(r"/channel/(UC[\w\-]{20,})", url)
        if m:
            return m.group(1)
        r = self._yt.post(YT_RESOLVE, json={**YT_CTX, "url": url}, timeout=12)
        if r.status_code != 200:
            raise RuntimeError(f"resolve: HTTP {r.status_code}")
        return yt_parse_resolve(r.json())

    def _yt_channel_status_slow(self, ch):
        """Запасная проверка канала через yt-dlp (медленнее, но надёжно)."""
        url = ch.page_url()
        if not url.rstrip("/").endswith("/streams"):
            url = url.rstrip("/") + "/streams"
        rc, out, _err = run_cmd(
            ["yt-dlp", "--flat-playlist", "--no-warnings"] + self._ytdlp_args() +
            ["-I", "1:20",
             "--print", "%(id)s %(live_status)s", "--print", "%(title)s", url],
            timeout=150)
        if rc != 0:
            return False, "", ""
        # пары строк: "id статус" и "заголовок"
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        for i in range(0, len(lines) - 1, 2):
            parts = lines[i].split()
            if len(parts) >= 2 and parts[1] == "is_live":
                return True, lines[i + 1], parts[0]
        return False, "", ""

    def _yt_video_status(self, vid):
        """Проверка прямой ссылки на трансляцию (yt-dlp)."""
        url = f"https://www.youtube.com/watch?v={vid}"
        rc, out, _err = run_cmd(
            ["yt-dlp", "--no-warnings", "--skip-download"] + self._ytdlp_args() +
            ["--print", "%(live_status)s", "--print", "%(title)s", url],
            timeout=150)
        if rc != 0:
            return False, ""
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        status = lines[0] if lines else ""
        title = lines[1] if len(lines) > 1 else ""
        return status == "is_live", title

    # --- ссылки на поток -----------------------------------------------------------------

    def _ytdlp_args(self):
        """Дополнительные аргументы yt-dlp (cookies, если настроены)."""
        args = []
        if self.cookies_file:
            args += ["--cookies", self.cookies_file]
        if self.cookies_from_browser:
            args += ["--cookies-from-browser", self.cookies_from_browser]
        return args

    def get_playback_url(self, ch):
        """Ссылка на поток для ffmpeg (HLS/DASH). None, если эфир не идёт."""
        if ch.platform == "twitch":
            return self._twitch_playback(ch)
        if ch.is_direct_video:
            live, title = self._yt_video_status(ch.video_id)
            if title:
                ch.title = title
            if not live:
                return None
            return self._yt_playback(ch.video_id)
        # канал YouTube: берём текущую трансляцию (она могла смениться)
        live, title, vid = self._youtube_check(ch)
        if live and vid:
            ch.video_id = vid
            if title:
                ch.title = title
            return self._yt_playback(vid)
        return None

    def _twitch_playback(self, ch):
        url = ch.page_url()
        if self.quality == "best":
            # быстрый путь: одна команда вместо двух — подключение на ~2 c раньше
            rc2, out2, _err2 = run_cmd(["streamlink", url, "best", "--stream-url"], timeout=45)
            lines = [ln.strip() for ln in out2.splitlines() if ln.strip()]
            return lines[0] if lines and lines[0].startswith("http") else None
        rc, out, _err = run_cmd(["streamlink", url, "--json"], timeout=45)
        if rc != 0:
            return None
        try:
            streams = json.loads(out).get("streams") or {}
        except json.JSONDecodeError:
            return None
        if not streams:
            return None
        name = None
        for q in TWITCH_QUALITY_PREF.get(self.quality, ["best"]):
            if q in streams:
                name = q
                break
        if name is None:
            name = "best" if "best" in streams else next(iter(streams))
        rc2, out2, _err2 = run_cmd(["streamlink", url, name, "--stream-url"], timeout=45)
        lines = [ln.strip() for ln in out2.splitlines() if ln.strip()]
        return lines[0] if lines and lines[0].startswith("http") else None

    def _yt_playback(self, video_id):
        fmt = YT_FORMAT.get(self.quality, "b")
        rc, out, _err = run_cmd(
            ["yt-dlp", "-g", "--no-warnings"] + self._ytdlp_args() +
            ["-f", fmt,
             f"https://www.youtube.com/watch?v={video_id}"],
            timeout=150)
        if rc != 0:
            return None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("http"):
                return line
        return None
