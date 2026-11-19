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
import shutil
import threading
import time

import banner
import subtitle
import vertical as vertical_mod
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
        if self.tiktok:
            self.tiktok.stop()
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
        start_ts = max(ev.ts - self.pre_roll, st.started_at)
        # Конец клипа: пик + post_roll. У «лучшего момента из прошлого»
        # (гарантированная нарезка при тихом чате) этот конец уже в прошлом —
        # тогда ждать ничего не нужно: берём [пик-30, пик+30] из буфера.
        end_ts = ev.ts + self.post_roll
        log.info("Клип: %.0f c до пика + %.0f c после = РОВНО %d c",
                 self.pre_roll, self.post_roll, int(self.pre_roll + self.post_roll))
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
        # Ждём «хвост» клипа только если пик настолько свежий, что 30 с после
        # него ещё не записались (у момента из прошлого хвост уже на диске).
        deadline = now + max(1.0, min(self.post_roll, end_ts - now)) if end_ts > now else 0.0
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
        if self.vertical:
            try:
                raw_ts = vertical_mod.make_vertical(
                    orig_ts, os.path.join(workdir, f"clip_{stamp}_9x16.mp4"),
                    workdir, mode=self.vertical_mode,
                    vcodec_args=self._vcodec_args)
            except Exception:
                log.exception("9:16 не удался — клип останется горизонтальным")
                raw_ts = orig_ts

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
                    start=self.banner_cfg.get("start", 8))
            except Exception:
                log.warning("Не удалось рассчитать показы баннера — без пауз")
                intervals = None
        try:
            ass = subtitle.make_subtitles(raw_ts, workdir, self.sub_cfg,
                                          self.speech_language,
                                          chat_msgs, duration=dur_est,
                                          skip_intervals=intervals)
        except Exception:
            log.exception("Ошибка построения субтитров — клип будет без них")
            ass = None

        try:
            self._finalize(raw_ts, out_path, workdir, ass, intervals)
        except Exception:
            log.exception("Не удалось собрать финальный mp4 — клип не сохранён")
            return

        dur_real = ffprobe_meta(out_path).get("duration") or dur_est
        self._write_meta(out_path, ch, ev, dur_real)
        self._make_thumb(out_path)

        caption = self._caption(st, ev, out_path, dur_real)
        ok = self.notifier.notify_clip(out_path, caption)
        if self.tiktok:
            tk_caption = tiktok_mod.build_caption(
                {"title": st.ch.title or "", "channel": st.ch.display(),
                 "platform": st.ch.platform}, self.tiktok.hashtags)
            self.tiktok.enqueue(out_path, tk_caption)
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

    def _write_meta(self, out_path, ch, ev, dur_s):
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
        }
        try:
            with open(str(out_path) + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
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
        return {
            "channels": chans,
            "clips_today": clips_today,
            "uptime_s": int(time.time() - self._t0),
            "clip_seconds": int(self.pre_roll + self.post_roll),
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
