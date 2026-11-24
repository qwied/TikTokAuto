"""Оркестратор: ссылки -> монитор -> чат -> хайп -> клип -> субтитры -> уведомление.

Список ссылок живой: правьте links.txt или channels в config.yaml. Клипы
снимаются прямо во время эфира: как только чат «взрывается», берём 30 секунд
из кольцевого буфера до пика и 30 секунд дописываем после — клип ровно 60
секунд, без ожидания конца стрима. Финальную обработку ускоряет
автоматический выбор видеокодировщика (NVENC/QSV/AMF/x264).
"""

import json
import logging
import os
import queue
import re
import shutil
import threading
import time

import banner
import lipsync as lipsync_mod
import overlays as overlays_mod
import subtitle
import vod as vod_mod
import vertical as vertical_mod
import webcam as webcam_mod
try:                       # AI-заголовки TikTok (локальная LLM; нет — fallback)
    import ai_caption
except ImportError:
    ai_caption = None
from ad_guard import AdGuard
from bot import TelegramBot
from chat_watcher import TwitchChatWatcher, YouTubeChatWatcher
from hype_detector import HypeDetector
from links import LinksFile, dedup_key, parse_link, refs_from_config
from models import ChannelRef
from notifier import Notifier, _is_placeholder
from recorder import SegmentRecorder
from stream_monitor import StreamMonitor
import tiktok as tiktok_mod
from tiktok import TikTokPoster
from utils import (detect_h264_encoder, ensure_dir, ffprobe_meta, human_size,
                    run_cmd, sanitize)

log = logging.getLogger("pipeline")


def resolve_rolls(clip_cfg):
    """(pre_roll, post_roll) с поддержкой clip.total: ровно столько секунд

    занимает готовый клип. Если задан total, post пересчитывается так, чтобы
    длина клипа была точно равна total; после пика остаётся минимум 5 секунд
    (иначе клип обрывается прямо на пике).
    """
    clip_cfg = clip_cfg or {}
    pre = float(clip_cfg.get("pre_roll", 30))
    post = float(clip_cfg.get("post_roll", 30))
    total = clip_cfg.get("total")
    try:
        total = float(total) if total else 0.0
    except (TypeError, ValueError):
        total = 0.0
    if total > 0:
        max_pre = max(5.0, total - 5.0)
        if pre > max_pre:
            pre = max_pre
            log.info("pre_roll уменьшен до %.0f c — после пика остаётся минимум 5 c", pre)
        if abs(pre + post - total) > 0.01:
            post = total - pre
            log.info("Длина клипа фиксируется на %.0f c: %.0f c до пика + %.0f c после",
                     total, pre, post)
    return pre, post


# --- адаптивная длина клипа (clip.total: auto) ---------------------------------
# Жирный момент -> длиннее: хайп 78+ тянет 60 секунд, 60+ — 45, обычный — 35.
AUTO_TOTALS = ((78.0, 60.0), (60.0, 45.0))
DEFAULT_TOTAL = 35.0


def pick_total(score, clip_cfg):
    """Длина клипа (сек) по СИЛЕ момента.

    clip.total: auto (по умолчанию) — 35/45/60 по оценке хайпа: короткий
    клип досматривают до конца чаще (это главный сигнал алгоритма TikTok),
    а длинный оправдан только когда момент реально жирный. Число в конфиге
    — фиксированная длина, как раньше. Границы 25-90 с.
    """
    raw = (clip_cfg or {}).get("total", "auto")
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("", "auto", "adaptive", "smart"):
            for thr, tot in AUTO_TOTALS:
                if float(score or 0) >= thr:
                    return tot
            return DEFAULT_TOTAL
        try:
            raw = float(s)
        except ValueError:
            return DEFAULT_TOTAL
    try:
        total = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TOTAL
    if total <= 0:
        return DEFAULT_TOTAL
    return max(25.0, min(total, 90.0))


# --- логическое завершение клипа ------------------------------------------------
# После пика хвост тянется до первой ПАУЗЫ речи: клип обрывается на конце
# фразы, а не посреди слова. Максимум +logical_tail секунд к расчётной длине.
LOGICAL_TAIL_DEFAULT = 10.0
_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


def find_logical_cut(ts_path, workdir, anchor, tail=LOGICAL_TAIL_DEFAULT,
                     min_len=25.0):
    """Секунда ЛОГИЧЕСКОГО конца клипа (пауза речи вокруг расчётного конца).

    anchor — расчётная длина (пик + post_roll). Тишина ищется ffmpeg
    silencedetect в окне [anchor-4, anchor+tail]. Приоритет:
      1) пауза, накрывающая anchor — режем прямо по anchor (лишнего нет);
      2) первая пауза ПОСЛЕ anchor — режем на её начале: стример договорил
         фразу, клип заканчивается на конце речи, а не посреди слова;
      3) пауза ДО anchor (в окне) — режем в её середине (фallback, когда
         речь уже кончилась и хвост тянуть нечем).
    Паузы нет вовсе — режем по anchor (как раньше, без хвоста).
    """
    anchor = float(anchor or 0)
    tail = max(0.0, float(tail or 0))
    meta = ffprobe_meta(ts_path)
    dur = float(meta.get("duration") or 0)
    if dur <= 0:
        # у аудио-файла (wav) нет видеопотока — берём длительность контейнера
        rc, out, _err = run_cmd(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(ts_path)], timeout=30)
        try:
            dur = float(json.loads(out).get("format", {}).get("duration") or 0)
        except (ValueError, TypeError, AttributeError):
            dur = 0.0
    if dur <= 0 or tail <= 0:
        return max(5.0, min(anchor, dur if dur > 0 else anchor))
    anchor = min(anchor, max(dur - 0.3, 5.0))
    lo = max(5.0, anchor - 4.0)
    hi = min(dur, anchor + tail)
    if hi <= lo:
        return anchor
    rc, _out, err = run_cmd(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i",
         os.path.basename(str(ts_path)),
         "-af", "silencedetect=noise=-32dB:d=0.45", "-f", "null", "-"],
        timeout=180, cwd=str(workdir))
    sil, cur = [], None
    for line in ((err or "") + "\n").splitlines():
        m = _SILENCE_START_RE.search(line)
        if m:
            cur = float(m.group(1))
            continue
        m = _SILENCE_END_RE.search(line)
        if m and cur is not None:
            sil.append((cur, float(m.group(1))))
            cur = None
    if cur is not None:
        sil.append((cur, dur))          # тишина до самого конца файла
    straddle = None
    future, past = [], []
    for s, e in sil:
        if e <= lo or s >= hi:
            continue
        if s <= anchor <= e:
            straddle = (s, e)
        elif s >= anchor:
            future.append((s, e))
        else:
            past.append((s, e))
    if straddle:
        return anchor
    if future:
        s, e = min(future, key=lambda p: p[0])   # первая тишина после anchor
        return min(max(s + 0.15, lo), hi)        # режем сразу за концом речи
    if past:
        s, e = max(past, key=lambda p: p[1])     # ближайшая тишина сзади
        return min(max((s + e) / 2.0, lo), hi)
    return anchor


def trim_clip_end(ts_path, cut_dur, workdir):
    """Обрезать хвост клипа до cut_dur секунд (БЕЗ перекодирования).

    Возвращает путь (тот же файл, содержимое заменено). Обрезка конца
    копированием точна до кадра: паковщику не нужны ключевые кадры,
    чтение просто останавливается на заданной секунде.
    """
    cut_dur = float(cut_dur or 0)
    meta = ffprobe_meta(ts_path)
    dur = float(meta.get("duration") or 0)
    if dur <= 0 or cut_dur >= dur - 0.3 or cut_dur <= 0:
        return ts_path                  # обрезать нечего
    base, ext = os.path.splitext(str(ts_path))
    tmp = base + ".cut" + ext
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
            "-i", os.path.basename(str(ts_path)), "-t", f"{cut_dur:.3f}",
            "-c", "copy", "-map", "0", os.path.basename(tmp)])
    rc, _out, err = run_cmd(cmd, timeout=300, cwd=str(workdir))
    if rc != 0:
        raise RuntimeError("ffmpeg (логический конец): " + (err or "")[-300:])
    os.replace(tmp, str(ts_path))
    return ts_path


class ActiveStream:
    """Всё, что связано с одним текущим эфиром."""

    def __init__(self, ch, recorder, detector, watcher, chat_q):
        self.ch = ch
        self.recorder = recorder
        self.detector = detector
        self.watcher = watcher
        self.chat_q = chat_q
        self.consumer = None
        self.guard = None                  # AdGuard: пауза нарезки во время рекламы
        self.started_at = time.time()
        self.clips = 0


class Pipeline:
    """Управляет полным жизненным циклом: стрим начался -> клипы -> стрим закончился."""

    def __init__(self, cfg, notifier: Notifier, server_base_url=""):
        self.cfg = cfg
        clip_cfg = cfg.get("clip", {}) or {}
        self.pre_roll, self.post_roll = resolve_rolls(clip_cfg)
        self.quality = str(clip_cfg.get("quality", "best"))   # best = исходное качество стрима
        # 9:16 — вертикальный формат TikTok по умолчанию (1080x1920)
        self.vertical = bool(clip_cfg.get("vertical", True))
        self.vertical_mode = str(clip_cfg.get("vertical_mode", "blur"))
        if self.vertical:
            log.info("Формат клипов: вертикальный 9:16 (1080x1920, режим %s) — как в TikTok",
                     self.vertical_mode)
        proc_cfg = cfg.get("processing", {}) or {}
        self.workers = max(1, int(proc_cfg.get("workers", 2)))
        self.encoder_choice = str(proc_cfg.get("encoder", "auto"))
        self._vcodec_args = None          # определяется при старте (авто-детект)

        base_dir = os.path.dirname(os.path.abspath(cfg.get("_config_path", "config.yaml")))
        storage = cfg.get("storage", {}) or {}
        self.clips_dir = ensure_dir(os.path.join(base_dir, str(storage.get("clips_dir", "clips"))))
        self.temp_root = ensure_dir(os.path.join(base_dir, str(storage.get("temp_dir", "temp"))))
        self.keep_temp = bool(storage.get("keep_temp", False))

        self.sub_cfg = cfg.get("subtitle", {}) or {}
        # Порог публикации: клипы с оценкой ниже — «скучные» — не выходят
        # вообще (задается сайтом: loose/normal/strict). 0 — выключить гейт.
        self.min_publish = float((cfg.get("_min_publish")) or 40.0)
        self.banner_cfg = cfg.get("banner", {}) or {}
        self.ad_cfg = cfg.get("ad", {}) or {}      # пауза нарезки во время рекламы
        self.banner_path = banner.resolve_file(self.banner_cfg, base_dir)
        if self.banner_path:
            log.info("Рекламный баннер: %s (будет в каждой нарезке)", self.banner_path)
        self.speech_language = str(cfg.get("speech_language", "ru"))
        self.notifier = notifier
        self.server_base_url = server_base_url

        # --- автопостинг в TikTok (очередь + антибан-пауза) ---------------------
        tk_cfg = cfg.get("tiktok", {}) or {}
        self.tiktok = None
        if tk_cfg.get("enabled"):
            self.tiktok = TikTokPoster(tk_cfg, base_dir)
            if not self.tiktok.ready():
                log.warning("TikTok-автопостинг НЕ готов к работе (см. сообщение "
                            "выше) — клипы будут только уведомлениями")
                self.tiktok = None

        self.links_file = LinksFile(os.path.join(
            base_dir, str(cfg.get("links_file", "links.txt"))))

        monitor_cfg = cfg.get("monitor", {}) or {}
        yt_cfg = cfg.get("youtube", {}) or {}
        self.monitor = StreamMonitor(
            self._merged_refs(), quality=self.quality,
            poll_twitch=int(monitor_cfg.get("poll_twitch", 3)),
            poll_youtube=int(monitor_cfg.get("poll_youtube", 5)),
            yt_workers=int(monitor_cfg.get("yt_workers", 3)),
            cookies_file=str(yt_cfg.get("cookies_file") or ""),
            cookies_from_browser=str(yt_cfg.get("cookies_from_browser") or ""),
            links_file=self.links_file, refresh_refs=self._merged_refs,
            on_live=self._on_live, on_offline=self._on_offline)

        self.active = {}
        self.clip_queue = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._connecting = set()        # каналы, к которым подключаемся прямо сейчас
        self._pending_offline = set()   # эфир кончился до завершения подключения
        self._clips_made = 0
        self._clips_date = time.strftime("%Y-%m-%d")
        self._t0 = time.time()
        self.bot = None

    # --- ссылки: config.yaml + links.txt ---------------------------------------------

    def _config_refs(self):
        """Ссылки из config.yaml: строки channels — это ГОТОВЫЕ ссылки на каналы."""
        return refs_from_config(self.cfg)

    def _merged_refs(self):
        """Актуальный список ссылок: из config.yaml + из links.txt."""
        refs, seen = [], set()
        for ref in self._config_refs() + self.links_file.read_refs()[0]:
            if dedup_key(ref) not in seen:
                seen.add(dedup_key(ref))
                refs.append(ref)
        return refs

    def _sync_from_file(self):
        self.monitor.sync_channels(self._merged_refs())

    # --- жизненный цикл ------------------------------------------------------------------

    def start(self):
        self._sync_from_file()
        self.monitor.start()
        # Кодировщик финальной обработки выбираем один раз при старте
        enc_name, self._vcodec_args = detect_h264_encoder(self.encoder_choice)
        for i in range(self.workers):
            threading.Thread(target=self._clip_worker, daemon=True,
                             name=f"clip-worker-{i}").start()
        if self.tiktok:
            self.tiktok.start()
        self._start_bot()

    def stop(self):
        self._stop.set()
        if self.bot:
            self.bot.stop()
        self.monitor.stop()
        for st in list(self.active.values()):
            try:
                st.detector.stop()
                if st.watcher:
                    st.watcher.stop()
                st.chat_q.put(None)
                st.recorder.stop()
            except Exception:
                pass
        self.active.clear()
        # TikTok гасим ПОСЛЕДНИМ: сначала дожидаемся публикации всех клипов,
        # которые уже стояли в очереди (раньше ролик, нарезанный в конце
        # сессии, выбрасывался: воркер просыпался, видел _stop и терял файл).
        if self.tiktok:
            self._drain_tiktok_queue()
            self.tiktok.stop()

    def _drain_tiktok_queue(self, max_wait_s=30 * 60):
        """Дождаться разгрузки очереди TikTok-публикаций (до 30 мин).

        Антибан-пауза + загрузка ролика занимают минуты, поэтому очередь
        почти никогда не пуста в момент конца сессии. Пустая очередь
        возвращается мгновенно — обычные остановки не задерживаются.
        """
        try:
            st = self.tiktok.status()
        except Exception:
            return
        if not st or not int(st.get("queued") or 0):
            return
        log.info("TikTok: в очереди %d клип(ов) — дожидаюсь публикации "
                 "(до %d мин), чтобы ни один клип не потерялся",
                 int(st.get("queued") or 0), max_wait_s // 60)
        deadline = time.time() + max_wait_s
        last_q = None
        while time.time() < deadline:
            try:
                q = int(self.tiktok.status().get("queued") or 0)
            except Exception:
                break
            if q <= 0:
                log.info("TikTok: очередь разгружена — все клипы отправлены")
                return
            if q != last_q:
                log.info("TikTok: осталось опубликовать %d", q)
                last_q = q
            time.sleep(5)
        log.warning("TikTok: очередь не разгрузилась за %d мин — останавливаюсь "
                    "(недопостившиеся клипы уйдут со следующей сессией)",
                    max_wait_s // 60)

    def _start_bot(self):
        """Telegram-бот управления: кидаете ссылку — начинает следить."""
        notify_cfg = self.cfg.get("notify", {}) or {}
        if str(notify_cfg.get("provider") or "telegram").lower() != "telegram":
            return
        tg = notify_cfg.get("telegram") or {}
        if str(tg.get("accept_commands", True)).lower() in ("0", "false", "no", "off"):
            return
        if _is_placeholder(tg.get("bot_token")):
            return
        handlers = {
            "list": self.list_text,
            "status": self.status_text,
            "add": self.add_channel_from_text,
            "remove": self.remove_channel_from_text,
            "clip": self.manual_clip,
        }
        self.bot = TelegramBot(notify_cfg, handlers)
        self.bot.start()

    # --- управление ссылками (для бота и консоли) ------------------------------------

    def add_channel_from_text(self, text, source="telegram"):
        """Разобрать строку как ссылку и начать следить. Возвращает ответ человеку.

        Ссылка сохраняется в links.txt (переживает перезапуск) из ЛЮБОГО
        источника, кроме config.yaml — канал добавили ОДИН раз и следим всегда.
        """
        ref = parse_link(text, source=source)
        if ref is None:
            return ("Не похоже на ссылку на стримера. Пришлите ссылку вида\n"
                    "twitch.tv/ник или https://www.youtube.com/@канал")
        if self.monitor.has_channel(ref.key):
            return f"Уже слежу за {ref.display()}"
        ok = self.monitor.add_channel(ref)
        if not ok:
            return f"Уже слежу за {ref.display()}"
        if source != "config":
            self.links_file.add(ref)      # сохраняем, чтобы ссылка пережила перезапуск
        return (f"Добавил: {ref.display()} — слежу.\n"
                f"Как только стример выйдет в эфир, включу запись и буду нарезать клипы.")

    def remove_channel_from_text(self, text):
        """Убрать ссылку из слежки. Понимает ссылку, имя канала или номер из /list."""
        text = str(text or "").strip()
        target = None
        if text.isdigit():
            n = int(text)
            chans = self.monitor.snapshot()
            if 1 <= n <= len(chans):
                target = chans[n - 1]
            else:
                return f"Нет канала с номером {n}. Посмотрите список: /list"
        else:
            ref = parse_link(text)
            if ref is None:
                # с сайта приходит имя канала (frostpony / Kuznya Show)
                for ch in self.monitor.snapshot():
                    if text.lower() in (ch.short_id().lower(),
                                        ch.display().lower(), ch.key):
                        target = ch
                        break
                if target is None:
                    return ("Не понял, что убрать. Пришлите ссылку как в /list "
                            "или номер канала, например: /remove 2")
            else:
                for ch in self.monitor.snapshot():
                    if ch.key == ref.key:
                        target = ch
                        break
        if target is None:
            return "Такого канала в списке нет — посмотрите /list"
        self.monitor.remove_channel(target.key)
        self.links_file.remove(target)
        self._teardown(target.key)
        return f"Убрал: {target.display()} — больше не слежу."

    def list_text(self):
        chans = self.monitor.snapshot()
        if not chans:
            return ("Список пуст. Пришлите мне ссылку на стримера "
                    "(twitch.tv/... или youtube.com/@...) — добавлю в слежку.")
        lines = ["Слежу за каналами:"]
        for i, ch in enumerate(chans, 1):
            mark = f"LIVE, {int(getattr(ch, 'viewers', 0) or 0)} зрителей" if ch.is_live else "не в эфире"
            lines.append(f"{i}. {ch.display()} — {mark}")
        lines.append("\nДобавить: пришлите ссылку. Убрать: /remove номер")
        return "\n".join(lines)

    def status_text(self):
        chans = self.monitor.snapshot()
        if not chans:
            return "Список ссылок пуст — пришлите мне ссылку на стримера."
        live_n = sum(1 for c in chans if c.is_live)
        lines = [f"Каналов в слежке: {len(chans)} (в эфире сейчас: {live_n})"]
        with self._lock:
            for key, st in self.active.items():
                mins = max(1, int((time.time() - st.started_at) // 60))
                buffered = len(st.recorder.file_times) * st.recorder.seg_time
                lines.append(
                    f"LIVE {st.ch.display()} — пишу {mins} мин, буфер {int(buffered)} c, "
                    f"клипов за эфир: {st.clips}, "
                    f"зрителей: {int(getattr(st.ch, 'viewers', 0) or 0)}, "
                    f"хайп сейчас: {st.detector.smooth:.0f}/100")
            if not self.active and live_n == 0:
                lines.append("Стримов нет — как только кто-то выйдет в эфир, включу запись.")
            if self._clips_made:
                lines.append(f"Всего клипов сегодня: {self._clips_made}")
        return "\n".join(lines)

    def manual_clip(self, reason=""):
        """Немедленно снять клип. key не задан — берём первый активный канал."""
        with self._lock:
            if not self.active:
                return "Сейчас нет активного стрима — клип снять нельзя"
            key, st = next(iter(self.active.items()))
        if st.guard and st.guard.ad_active:
            return "Сейчас идёт РЕКЛАМА — нарезка на паузе, клип будет после её окончания"
        log.info("Ручной клип для %s%s", st.ch.display(), f" ({reason})" if reason else "")
        st.detector.manual_trigger(reason or "по команде")
        return f"Снимаю клип с {st.ch.display()} — пришлю через пару минут"

    # --- события монитора ------------------------------------------------------------------

    def _on_live(self, ch):
        """Монитор сообщил о выходе в эфир. Защита от дублей: подключение
        может начаться, только если оно ещё не идёт и сессии ещё нет."""
        with self._lock:
            if (self._stop.is_set() or not self.monitor.has_channel(ch.key)
                    or ch.key in self.active or ch.key in self._connecting):
                return
            if ch.key in self._pending_offline:
                # эфир закончился раньше, чем мы успели подключиться
                self._pending_offline.discard(ch.key)
                log.info("%s: эфир закончился до подключения — запись не включаю",
                         ch.short_id())
                return
            self._connecting.add(ch.key)
        try:
            self._connect(ch)
        finally:
            with self._lock:
                self._connecting.discard(ch.key)

    def _connect(self, ch):
        log.info("Подключаюсь к эфиру: %s (%s)", ch.short_id(), ch.platform)
        rec_cfg = self.cfg.get("recorder", {}) or {}
        workdir = ensure_dir(os.path.join(self.temp_root,
                                          f"{ch.platform}_{sanitize(ch.short_id())}"))
        # YouTube-канал: id текущей трансляции нужен и для потока, и для чата —
        # выясняем его до создания ридера чата
        url = self.monitor.get_playback_url(ch)
        if not url:
            log.warning("Ссылка на поток не получена сразу — рекордер продолжит попытки")
        recorder = SegmentRecorder(
            workdir,
            seg_time=float(rec_cfg.get("segment_time", 4)),
            buffer_keep=float(rec_cfg.get("buffer_keep", 300)),
            resolve_url=lambda: self.monitor.get_playback_url(ch))
        detector = HypeDetector(
            ch.key, self.cfg.get("hype", {}) or {}, self.cfg.get("clip", {}) or {},
            on_hype=lambda ev, k=ch.key: self._on_hype(k, ev))
        chat_q = queue.Queue()
        if ch.platform == "twitch":
            watcher = TwitchChatWatcher(ch.id, chat_q.put)
        elif ch.video_id:
            page = ch.video_url or f"https://www.youtube.com/watch?v={ch.video_id}"
            watcher = YouTubeChatWatcher(ch.video_id, page, chat_q.put)
        else:
            log.warning("YouTube: не известен id трансляции — чат будет недоступен, "
                        "клипы снимаются по КАПС-спаму без чата")
            watcher = None

        st = ActiveStream(ch, recorder, detector, watcher, chat_q)
        with self._lock:
            # канал могли убрать из слежки / эфир кончился, пока получали ссылку на поток
            if (self._stop.is_set() or not self.monitor.has_channel(ch.key)
                    or not ch.is_live or ch.key in self._pending_offline):
                self._pending_offline.discard(ch.key)
                log.info("Канал %s больше не в эфире/в слежке — запись не включаю",
                         ch.display())
                return
            self.active[ch.key] = st
        st.consumer = threading.Thread(target=self._chat_consumer, args=(ch.key,),
                                       daemon=True, name=f"chat-consumer-{ch.short_id()}")
        st.consumer.start()
        detector.start()
        if watcher:
            watcher.start()

        recorder.start(url or "invalid_url_placeholder")

        # --- пауза нарезки во время рекламы (RubAp / RubUp и др.) -----------------
        if self.ad_cfg.get("enabled", True):
            st.guard = AdGuard(
                ch.key, self.ad_cfg,
                get_url=lambda: recorder.url,
                on_change=lambda active, k=ch.key: None)
            st.guard.start()

        log.info("Запись включена: %s. Жду всплеска в чате — клипы снимаю прямо во время эфира",
                 ch.display())

    def _on_hype(self, key, ev):
        """Хайп -> в очередь клипов. Во время рекламы нарезка НА СТОП."""
        st = self.active.get(key)
        if st and st.guard and st.guard.ad_active:
            log.info("Хайп %.0f/100 на %s пропущен: идёт РЕКЛАМА — нарезка на паузе",
                     ev.score, key)
            return
        self.clip_queue.put((key, ev))

    def _on_offline(self, ch):
        st = self.active.get(ch.key)
        if st is None:
            # подключение ещё не завершилось, а эфир уже кончился
            with self._lock:
                if ch.key in self._connecting:
                    self._pending_offline.add(ch.key)
            return
        log.info("Стрим %s завершился: за эфир снято клипов: %d", ch.short_id(), st.clips)
        if st.guard:
            st.guard.stop()
        st.detector.stop()
        if st.watcher:
            st.watcher.stop()
        st.chat_q.put(None)

        def _delayed_stop():
            time.sleep(self.post_roll + 10)
            try:
                st.recorder.stop()
                st.recorder.cleanup()
            except Exception:
                pass
            with self._lock:
                self.active.pop(ch.key, None)

        threading.Thread(target=_delayed_stop, daemon=True).start()

    def _teardown(self, key):
        """Мгновенно остановить сессию (канал убрали из списка)."""
        with self._lock:
            st = self.active.pop(key, None)
            self._pending_offline.discard(key)
            self._connecting.discard(key)
        if st is None:
            return
        log.info("Останавливаю сессию: %s", st.ch.display())
        try:
            if st.guard:
                st.guard.stop()
            st.detector.stop()
            if st.watcher:
                st.watcher.stop()
            st.chat_q.put(None)
            st.recorder.stop()
            st.recorder.cleanup()
        except Exception:
            pass

    def _chat_consumer(self, key):
        st = self.active.get(key)
        if st is None:
            return
        while not self._stop.is_set():
            try:
                item = st.chat_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                st.detector.feed(item)
            except Exception:
                pass

    # --- сборка клипа ------------------------------------------------------------------------

    def _clip_worker(self):
        while not self._stop.is_set():
            try:
                key, ev = self.clip_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._make_clip(key, ev)
            except Exception:
                log.exception("Не удалось собрать клип")

    def _make_clip(self, key, ev):
        st = self.active.get(key)
        if st is None:
            log.info("Клип отменён: стрим уже завершился и буфер закрыт")
            return
        ch = st.ch
        now = time.time()
        # --- длина клипа ПО СИЛЕ МОМЕНТА (clip.total: auto -> 35/45/60 c) -----
        clip_cfg = self.cfg.get("clip") or {}
        total = pick_total(ev.score, clip_cfg)
        pre = max(5.0, min(round(total / 2.0), total - 5.0))
        post = total - pre
        tail = max(0.0, float(clip_cfg.get("logical_tail", LOGICAL_TAIL_DEFAULT)))
        start_ts = max(ev.ts - pre, st.started_at)
        # Конец клипа: пик + post (+ хвост до паузы). У «лучшего момента из
        # прошлого» (гарантированная нарезка при тихом чате) этот конец уже
        # в прошлом — тогда ждать ничего не нужно: берём окно из буфера.
        end_ts = ev.ts + post + tail
        log.info("Клип: сила момента %.0f/100 -> %d c (%.0f c до пика + %.0f c "
                 "после) + до %d c хвоста до паузы",
                 ev.score, int(total), pre, post, int(tail))
        # рекламные интервалы из guard -> в пометки буфера: сегменты, записанные
        # во время рекламы, не попадут в клип
        if st.guard and st.guard.intervals:
            for a0, a1 in st.guard.intervals:
                if a1 is not None:
                    st.recorder.mark_ad(a0, a1)
        segs = st.recorder.reserve_segments(start_ts, min(end_ts, now))
        if not segs:
            log.warning("Буфер пуст — клип не собран (стрим только что начался?)")
            return
        # Ждём «хвост» клипа только если пик настолько свежий, что post_roll
        # и логический хвост ещё не записались (у момента из прошлого хвост
        # уже на диске).
        deadline = now + max(1.0, min(post + tail, end_ts - now)) if end_ts > now else 0.0
        while deadline and time.time() < deadline:
            time.sleep(0.5)
            if (not st.recorder.is_recording
                    and time.time() - st.recorder.last_new_file_ts > 6):
                break  # стрим кончился и новых сегментов нет — берём что есть
        segs = st.recorder.reserve_segments(start_ts, end_ts if end_ts <= time.time() else None)   # добавляем пост-ролл
        dur_est = len(segs) * st.recorder.seg_time

        workdir = st.recorder.workdir
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        raw_ts = os.path.join(workdir, f"clip_{stamp}.ts")
        self._assemble(segs, raw_ts, workdir)
        st.recorder.release(segs)
        orig_ts = raw_ts

        # --- вертикальный формат 9:16 (TikTok) -------------------------------------
        # До субтитров: ASS рисуется уже под полотно 1080x1920. Если перевод
        # не удался, клип останется горизонтальным — клип не теряется.
        # ЗАМАЗКА ЧУЖИХ БАНЕРОВ/ВИДЖЕТОВ (кроме RubUp): детект статичных
        # оверлеев + верхние углы — передаём ЗОНЫ В ИСХОДНИКЕ (raw_boxes),
        # маппинг под фактический макет (split/blur) делает make_vertical.
        # RubUp рисуется ПОСЛЕ замазки — остаётся чётким.
        if self.vertical:
            raw_boxes = None
            cam_box = None
            clip_cfg = self.cfg.get("clip") or {}
            if bool(clip_cfg.get("hide_overlays", True)):
                try:
                    raw_meta = ffprobe_meta(orig_ts)
                    # Окно камеры ищем ОДИН раз: (а) исключаем его из детекта
                    # чужих оверлеев — вебкамера статична и выглядит детектору
                    # «баннером», из-за этого замазка накрывала лицо стримера
                    # (прод 2026-11-20: dmitry_lixxx); (б) передаём в
                    # make_vertical, чтобы не детектить второй раз.
                    try:
                        cam_box = webcam_mod.detect_cam_box(orig_ts, workdir)
                    except Exception:
                        log.exception("Детект камеры упал — замазка без исключения")
                        cam_box = None
                    excl = [cam_box] if cam_box else []
                    raw_boxes = overlays_mod.detect_overlay_boxes(
                        orig_ts, workdir, exclude_boxes=excl)
                    if bool(clip_cfg.get("mask_corners", True)):
                        raw_boxes = raw_boxes + overlays_mod.corner_boxes(
                            int(raw_meta.get("width") or 0),
                            int(raw_meta.get("height") or 0))
                    if raw_boxes:
                        log.info("Замазка (эфир): %d зон чужих оверлеев",
                                 len(raw_boxes))
                except Exception:
                    log.exception("Замазка оверлеев (эфир) не удалась — клип без неё")
                    raw_boxes = None
            try:
                raw_ts = vertical_mod.make_vertical(
                    orig_ts, os.path.join(workdir, f"clip_{stamp}_9x16.mp4"),
                    workdir, mode=self.vertical_mode,
                    vcodec_args=self._vcodec_args, raw_boxes=raw_boxes,
                    cam_box=cam_box,
                    fallback=str(clip_cfg.get("vertical_fallback",
                                              vertical_mod.FALLBACK_DEFAULT)))
            except Exception:
                log.exception("9:16 не удался — клип останется горизонтальным")
                raw_ts = orig_ts

        # --- ГЕЙТ ЛИЦА: клипы без стримера в кадре не публикуем -------------------
        # Жалоба пользователя «опять без лица»: если вебкамера не найдена и
        # слабый детект лица тоже молчит — стримера в кадре нет (турниры,
        # стримы сайтов), такой клип TikTok не нужен. Гейт активен только
        # при рабочем OpenCV и для вертикальных клипов.
        if self.vertical and webcam_mod.cv2_available() \
                and str(raw_ts).endswith("_9x16.mp4"):
            face_seen = cam_box is not None
            if not face_seen:
                try:
                    face_seen = webcam_mod.detect_cam_box(
                        orig_ts, workdir, weak=True) is not None
                except Exception:
                    log.exception("Слабый детект лица не удался — не блокирую клип")
                    face_seen = True
            if not face_seen:
                log.info("Лица в кадре нет (вебкамера не найдена) — "
                         "клип %s не публикую", stamp)
                for f in (orig_ts, raw_ts):
                    try:
                        if os.path.exists(f):
                            os.remove(f)
                    except OSError:
                        pass
                return

        # --- ЛОГИЧЕСКИЙ КОНЕЦ: хвост режется на паузе речи, не посреди слова --
        # Расчётная длина (пик + post). Буфер запасён с хвостом +tail: если
        # после пика стример договаривает фразу — клип тянется до первой
        # тишины (но не дольше +tail); если тишина уже у расчётного конца —
        # лишнего хвоста нет. Стрим оборвался — режем по фактической длине.
        intended = (ev.ts + post) - start_ts
        try:
            cut_dur = find_logical_cut(raw_ts, workdir, intended, tail)
        except Exception:
            log.exception("Анализ паузы не удался — режу по расчётной длине")
            cut_dur = intended
        if abs(cut_dur - intended) > 0.25:
            try:
                raw_ts = trim_clip_end(raw_ts, cut_dur, workdir)
                log.info("Логический конец: %.1f c (расчёт %.1f c, %+0.1f c)",
                         cut_dur, intended, cut_dur - intended)
            except Exception:
                log.exception("Обрезка хвоста не удалась — клип прежней длины")
                cut_dur = intended
        try:
            dur_est = float(ffprobe_meta(raw_ts).get("duration") or dur_est)
        except Exception:
            pass

        out_path = os.path.join(self.clips_dir, f"{stamp}_{sanitize(ch.short_id())}.mp4")

        chat_msgs = st.detector.recent_messages(start_ts, time.time()) if st.detector else []
        # интервалы показа баннера нужны ДО субтитров: пока идёт RubUP,
        # нарезка «на паузе» (звук стрима молчит) — субтитры там не нужны
        intervals = None
        if self.banner_path:
            try:
                bmeta = ffprobe_meta(self.banner_path)
                intervals = banner.plan_intervals(
                    float(ffprobe_meta(raw_ts).get("duration") or dur_est),
                    float(bmeta.get("duration") or 0),
                    start=self.banner_cfg.get("start", "middle"))
            except Exception:
                log.warning("Не удалось рассчитать показы баннера — без пауз")
                intervals = None
        # --- ЛИП-СИНК: когда стример РЕАЛЬНО говорит (по движению губ) -----------
        # Считаем ПОСЛЕ логической обрезки (raw_ts уже финальной длины) и
        # на вертикальном клипе (субтитры строятся по нему же — тайминги
        # сходятся). Жалоба пользователя: субтитры «проносятся», когда
        # стример молчит — Whisper тянет речь из игры/войсчата.
        # Геометрия: в split полоса камеры известна из макета (0..cam_h) —
        # повторный детект лица не нужен; в crop-фолбэке лицо за кадром —
        # гейт невозможен; в blur лицо по центру — lipsync детектит сам.
        lip_iv = None
        # Лип-гейт ВКЛЮЧЁН по умолчанию (требование пользователя: «субтитры
        # следят ИМЕННО за губами стримера»). Старый прод-инцидент exigent_wires
        # (гейт резал половину честной речи на тёмном лице) лечится YuNet-детектом
        # лица (lipsync.py) и арбитражем «губы vs аудио-VAD» в subtitle._lip_gate:
        # детект рта сломан — гейт сам выключается, субтитры остаются по аудио.
        # Отключается осознанно: subtitle.lip_sync: false в config.yaml.
        sub_lip_on = bool((self.sub_cfg or {}).get("lip_sync", True))
        if sub_lip_on and os.path.exists(raw_ts):
            try:
                if (cam_box and self.vertical_mode == "split"
                        and str(raw_ts).endswith("_9x16.mp4")):
                    strip_h = vertical_mod.split_cam_h()
                    lip_iv = lipsync_mod.speaking_intervals(
                        raw_ts, workdir, cam_box=(0, 0, 1080, strip_h))
                elif cam_box or str(clip_cfg.get("vertical_fallback",
                                                 "crop")) == "blur":
                    lip_iv = lipsync_mod.speaking_intervals(raw_ts, workdir)
                # иначе (crop без камеры): лица в кадре нет — гейт не нужен
            except Exception:
                log.exception("Лип-синк не удался — субтитры без гейта по губам")
                lip_iv = None

        try:
            ass, smeta = subtitle.make_subtitles_meta(
                raw_ts, workdir, self.sub_cfg, self.speech_language,
                chat_msgs, duration=dur_est, skip_intervals=intervals,
                lip_intervals=lip_iv)
        except Exception:
            log.exception("Ошибка построения субтитров — клип будет без них")
            ass, smeta = None, {}

        try:
            self._finalize(raw_ts, out_path, workdir, ass, intervals)
        except Exception:
            log.exception("Не удалось собрать финальный mp4 — клип не сохранён")
            return

        dur_real = ffprobe_meta(out_path).get("duration") or dur_est

        # --- КОНТРОЛЬ КАЧЕСТВА ПОСЛЕ НАРЕЗКИ -------------------------------------
        # Хайп чата — оценка ДО нарезки. Теперь смотрим и на сам клип:
        # битрейт (движение в кадре), громкость, распознанная речь. Скучное
        # («гарантированная» нарезка при мёртвом чате, статичный кадр 2 МБ)
        # не публикуем вовсе: лучше меньше клипов, но все — огонь.
        try:
            q_score, q_parts = vod_mod.clip_quality(out_path, smeta,
                                                    base=float(ev.score),
                                                    duration=dur_real)
        except Exception:
            log.exception("Оценка качества не удалась — публикую как есть")
            q_score, q_parts = float(ev.score), {}
        if self.min_publish > 0 and q_score < self.min_publish:
            log.info("СКУКА: %.0f/100 < порога %.0f — клип не публикую "
                     "(битрейт %.2f Мбит/с, звук %.0f dB, слов %d)",
                     q_score, self.min_publish, q_parts.get("mbps", 0),
                     q_parts.get("mean_db", -99), q_parts.get("words", 0))
            self._drop_clip(out_path)
            for f in (orig_ts, raw_ts):
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except OSError:
                    pass
            return

        self._write_meta(out_path, ch, ev, dur_real, subs=smeta)
        self._make_thumb(out_path)

        caption = self._caption(st, ev, out_path, dur_real)
        if smeta.get("ok"):
            caption += (f"\nСубтитры: {smeta.get('lang') or '?'} · "
                        f"{smeta.get('words', 0)} слов · речи {smeta.get('speech_pct', 0)}%")
        else:
            caption += "\nСубтитров нет — в этом моменте не было распознаваемой речи"
        ok = self.notifier.notify_clip(out_path, caption,
                                       platform=st.ch.platform,
                                       display=st.ch.display(),
                                       title=st.ch.title or "",
                                       score=q_score, subs=smeta)
        if self.tiktok:
            if q_score >= self.tiktok.min_score:
                # AI-заголовок: LLM по транскрипту клипа (transcript.txt из
                # subtitle.py); нет модели/речи — выжимка; совсем пусто —
                # название стрима. Ошибки не роняют публикацию.
                tk_title = st.ch.title or ""
                if ai_caption:
                    try:
                        tk_title = (ai_caption.make_title(
                            ai_caption.transcript_of(workdir),
                            fallback=tk_title) or tk_title)
                        if tk_title != (st.ch.title or ""):
                            log.info("AI-заголовок TikTok: %r", tk_title)
                    except Exception:
                        log.exception("AI-заголовок не удался — прежний")
                        tk_title = st.ch.title or ""
                tk_caption = tiktok_mod.build_caption(
                    {"title": tk_title, "channel": st.ch.display(),
                     "platform": st.ch.platform}, self.tiktok.hashtags)
                self.tiktok.enqueue(out_path, tk_caption)
            else:
                log.info("TikTok: пропустил %s — оценка %.0f < порога %d "
                         "(в TikTok уходят только хайповые)",
                         os.path.basename(out_path), q_score,
                         self.tiktok.min_score)
        with self._lock:
            st.clips += 1
            self._clips_made += 1
        log.info("Клип готов: %s (%s), уведомление %s",
                 out_path, human_size(os.path.getsize(out_path)),
                 "отправлено" if ok else "НЕ отправлено — проверьте настройки")

        if not self.keep_temp:
            for f in (orig_ts, raw_ts,
                      os.path.join(workdir, "subs.ass"),
                      os.path.join(workdir, "concat.txt")):
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except OSError:
                    pass

    def _assemble(self, seg_paths, raw_ts, workdir):
        """Склейка зарезервированных сегментов в один ts (без перекодирования)."""
        list_path = os.path.join(str(workdir), "concat.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for p in seg_paths:
                f.write("file '" + os.path.basename(p) + "'\n")
        rc, _out, err = self._ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", "concat.txt",
             "-c", "copy", os.path.basename(raw_ts)], workdir, timeout=300)
        if rc != 0:
            raise RuntimeError("ffmpeg (склейка сегментов): " + err[-400:])
        return raw_ts

    def _remux(self, raw_ts, out_mp4, workdir):
        rc, _out, err = self._ffmpeg(
            ["-i", os.path.basename(raw_ts), "-c", "copy",
             "-movflags", "+faststart", os.path.basename(out_mp4)],
            workdir, timeout=300)
        if rc != 0:
            raise RuntimeError("ffmpeg (упаковка): " + err[-400:])

    def _finalize(self, raw_ts, out_path, workdir, ass=None, intervals=None):
        """Финальный mp4 в clips/.

        Сборка ОДНИМ проходом ffmpeg: рекламный баннер по центру кадра
        (нарезка на паузе, пока идёт ролик RubUP: кадр заморожен, звук
        стрима молчит; после баннера эфир продолжается) + субтитры
        + выбранный кодировщик (авто: NVENC -> QSV -> AMF -> x264).
        При сбое аппаратного кодировщика — повтор на x264; если и это
        не помогло — клип сохраняется по старой схеме (только субтитры
        или remux без баннера), чтобы клип не потерялся.

        ffmpeg запускается с cwd=workdir (короткие относительные пути —
        безопасно для Windows и фильтров ass=/overlay), файл собирается
        внутри workdir и переносится в clips/ в конце.
        """
        out_tmp = os.path.join(str(workdir), os.path.basename(out_path))
        va = self._vcodec_args or ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
        x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
        done = False
        if self.banner_path:
            for enc, tag in ((va, "выбранным кодировщиком"), (x264, "на x264")):
                try:
                    banner.compose(raw_ts, self.banner_path, ass, out_tmp, workdir,
                                   vcodec_args=enc, cfg=self.banner_cfg,
                                   intervals=intervals)
                    done = True
                    break
                except Exception as e:
                    log.warning("Баннер не собрался (%s) — повторяю %s", e, tag)
        if not done and ass:
            burned = False
            try:
                subtitle.burn_subtitles(raw_ts, ass, out_tmp, workdir, vcodec_args=va)
                burned = True
            except Exception:
                log.warning("Субтитры не вшились выбранным кодировщиком — повторяю на x264")
            if not burned:
                try:
                    subtitle.burn_subtitles(raw_ts, ass, out_tmp, workdir,
                                            vcodec_args=x264)
                    burned = True
                except Exception:
                    log.exception("Не удалось вшить субтитры — сохраняю клип без них")
            if burned:
                done = True
        if not done:
            self._remux(raw_ts, out_tmp, workdir)
        try:
            os.replace(out_tmp, out_path)
        except OSError:
            shutil.move(out_tmp, out_path)
        return out_path

    def _write_meta(self, out_path, ch, ev, dur_s, subs=None):
        """JSON-метаданные рядом с клипом — их читает веб-интерфейс (/api/clips)."""
        meta = {
            "file": os.path.basename(str(out_path)),
            "channel": ch.short_id(),
            "display": ch.display(),
            "platform": ch.platform,
            "title": ch.title or "",
            "score": round(float(ev.score), 1),
            "duration": round(float(dur_s or 0), 1),
            "created": int(os.path.getmtime(str(out_path))),
            "subs": dict(subs or {}),
        }
        try:
            with open(str(out_path) + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except OSError:
            pass

    def _drop_clip(self, out_path):
        """Удалить забракованный клип и его побочные файлы (превью, мета)."""
        stem = os.path.splitext(os.path.basename(str(out_path)))[0]
        thumb = os.path.join(self.clips_dir, "thumbs", stem + ".jpg")
        for f in (str(out_path), str(out_path) + ".json", thumb):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass

    def _make_thumb(self, out_path):
        """Превью для веб-интерфейса: clips/thumbs/<имя>.jpg (1 кадр из клипа)."""
        try:
            thumbs_dir = ensure_dir(os.path.join(self.clips_dir, "thumbs"))
            stem = os.path.splitext(os.path.basename(str(out_path)))[0]
            thumb = os.path.join(thumbs_dir, stem + ".jpg")
            rc, _o, _e = run_cmd(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
                 "-ss", "2", "-i", str(out_path), "-frames:v", "1",
                 "-vf", "scale=480:-2", "-q:v", "4", "-update", "1", thumb],
                timeout=60)
            if rc != 0:
                log.debug("Превью не создано для %s", out_path)
        except Exception:
            log.debug("Превью не создано для %s", out_path, exc_info=True)

    def api_status(self):
        """Состояние программы для веб-интерфейса (/api/status)."""
        chans = []
        for c in self.monitor.snapshot():
            st = self.active.get(c.key)
            chans.append({
                "name": c.short_id(),
                "display": c.display(),
                "platform": c.platform,
                "live": bool(c.is_live),
                "recording": bool(st and st.recorder.is_recording),
                "viewers": int(c.viewers or 0),
                "title": c.title or "",
                "hype": round(float(st.detector.smooth), 1) if st else 0.0,
                "ad": bool(st and st.guard and st.guard.ad_active),
            })
        with self._lock:
            clips_today = self._clips_made
        total_cfg = (self.cfg.get("clip") or {}).get("total", "auto")
        try:
            clip_len = int(float(total_cfg))
        except (TypeError, ValueError):
            clip_len = "35-60 (авто, по силе момента)"
        return {
            "channels": chans,
            "clips_today": clips_today,
            "uptime_s": int(time.time() - self._t0),
            "clip_seconds": clip_len,
            "quality": self.quality,
            "ad_guard": bool(self.ad_cfg.get("enabled", True)),
            "tiktok": self.tiktok.status() if self.tiktok else None,
        }

    # --- API для веб-интерфейса (POST) --------------------------------------------------

    def api_add_channel(self, text):
        """Добавить канал с сайта. Канал добавляется ОДИН раз и следит всегда."""
        return self.add_channel_from_text(text, source="web")

    def api_remove_channel(self, text):
        """Убрать канал (с сайта). Принимает ссылку или имя канала."""
        return self.remove_channel_from_text(text)

    def api_manual_ad(self, state, channel=None):
        """Ручная пауза нарезки во время рекламы (кнопка РЕКЛАМА на сайте).

        state: "on" — реклама началась, "off" — закончилась.
        channel: имя канала (не задано — первый активный эфир).
        """
        state = str(state or "").lower()
        if state not in ("on", "off"):
            return "Не понял: state должен быть on или off"
        with self._lock:
            if not self.active:
                return "Сейчас нет активного стрима"
            st = None
            if channel:
                for key, cand in self.active.items():
                    if channel.lower() in (key.lower(),
                                           cand.ch.short_id().lower(),
                                           cand.ch.display().lower()):
                        st = cand
                        break
            else:
                _key, st = next(iter(self.active.items()))
        if st is None:
            return f"Канал {channel} сейчас не в эфире"
        if st.guard is None:
            return "Ad-guard выключен в config.yaml (ad.enabled)"
        active = st.guard.set_manual(state == "on")
        ch = st.ch.display()
        return (f"РЕКЛАМА ({ch}): нарезка на паузе" if active
                else f"Реклама закончилась ({ch}) — нарезка продолжается")

    def api_set_tiktok_token(self, data):
        """Сохранить TikTok access_token с сайта и включить автопостинг.

        Ожидаются access_token (обязательно), refresh_token и expires_in.
        Токен кладётся в tiktok_tokens.json — как делает tiktok_auth.py.
        """
        data = data or {}
        token = str(data.get("access_token") or "").strip()
        if len(token) < 20:
            return ("Нужен настоящий TikTok access_token (developers.tiktok.com — "
                    "инструкция в README)")
        tk_cfg = self.cfg.get("tiktok", {}) or {}
        if not tk_cfg.get("enabled"):
            return "Автопостинг выключен: поставьте tiktok.enabled: true в config.yaml"
        token_file = os.path.join(os.path.dirname(os.path.abspath(
            self.cfg.get("_config_path", "config.yaml"))),
            str(tk_cfg.get("token_file") or "tiktok_tokens.json"))
        payload = {
            "access_token": token,
            "refresh_token": str(data.get("refresh_token") or ""),
            "open_id": str(data.get("open_id") or ""),
            "expires_at": time.time() + int(data.get("expires_in") or 86400),
        }
        try:
            with open(token_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError as e:
            return f"Не удалось сохранить токен: {e}"
        # токен появился — поднимаем постер, если он ещё не работал
        if self.tiktok is None:
            from tiktok import TikTokPoster  # noqa: PLC0415
            self.tiktok = TikTokPoster(tk_cfg, os.path.dirname(token_file))
        self.tiktok.ensure_started()
        return "TikTok-токен сохранён — автопостинг включён. Клипы будут выкладываться сами."

    @staticmethod
    def _ffmpeg(args, workdir, timeout=300):
        from utils import run_cmd
        return run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin"] + args,
                       timeout=timeout, cwd=str(workdir))

    def _caption(self, st, ev, out_path, dur_s):
        size = human_size(os.path.getsize(out_path))
        lines = [f"Клип готов: {st.ch.display()} ({st.ch.platform})"]
        if st.ch.title:
            lines.append(st.ch.title)
        lines.append(f"Хайп: {ev.score:.0f}/100 · {int(dur_s)} сек · {size}")
        link = self.notifier.file_url(out_path)
        if link:
            lines.append("")
            lines.append(f"Скачать: {link}")
        if ev.top_messages:
            lines.append("")
            lines.append("Чат в момент хайпа:")
            for m in ev.top_messages[:4]:
                lines.append(f"  {m.user}: {m.text[:60]}")
        return "\n".join(lines)[:1000]
