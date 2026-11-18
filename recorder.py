"""Кольцевой буфер записи потока через ffmpeg.

Поток пишется короткими сегментами mpegts. На диске держатся только последние
~buffer_keep секунд, поэтому «забрать в прошлое» можно мгновенно: перед срезом
сегменты резервируются и уже не удаляются.
"""

import glob
import logging
import os
import re
import subprocess
import threading
import time

from utils import ensure_dir

log = logging.getLogger("recorder")

SEG_RE = re.compile(r"seg_(\d+)\.ts$")


class SegmentRecorder:
    """Пишет поток в короткие сегменты и позволяет резервировать моменты из прошлого."""

    def __init__(self, workdir, seg_time=4.0, buffer_keep=300.0, resolve_url=None):
        self.workdir = ensure_dir(str(workdir))
        self.seg_time = float(seg_time)
        self.buffer_keep = float(buffer_keep)
        self.resolve_url = resolve_url      # callable -> свежая ссылка при переподключении
        self.url = None
        self.proc = None
        self.counter = 0                    # продолжение нумерации после переподключений
        self.file_times = {}                # путь сегмента -> время его появления (wall clock)
        self.reserved = set()
        self.ad_ranges = []                 # [(t0, t1), ...] реклама в wall-clock времени
        self.last_new_file_ts = 0.0
        self.is_recording = False
        self._stop = threading.Event()
        self._thread = None
        self._fflog = None

    def start(self, url):
        self.url = url
        self._stop.clear()
        self.is_recording = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="recorder")
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=20)
        self.is_recording = False

    # --- внутренний цикл записи -------------------------------------------------

    def _loop(self):
        fails = 0
        while not self._stop.is_set():
            self._open_fflog()
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-i", self.url,
                "-c", "copy",
                "-f", "segment", "-segment_time", str(self.seg_time),
                "-segment_format", "mpegts", "-reset_timestamps", "1",
                "-segment_start_number", str(self.counter),
                os.path.join(self.workdir, "seg_%06d.ts"),
            ]
            log.info("Запись потока начата (буфер: %s)", self.workdir)
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=self._fflog if self._fflog else subprocess.DEVNULL,
                )
            except FileNotFoundError:
                log.error("ffmpeg не найден — запись невозможна")
                self.is_recording = False
                return
            while self.proc.poll() is None and not self._stop.is_set():
                self._discover()
                self._retain()
                time.sleep(1.0)
            self._discover()
            if self._stop.is_set():
                break
            log.warning("ffmpeg завершился (код %s) — переподключение через 3 c", self.proc.poll())
            time.sleep(3.0)
            new_url = self.resolve_url() if self.resolve_url else None
            if not new_url:
                fails += 1
                if fails >= 40:
                    log.warning("Не удалось получить ссылку на поток 40 раз подряд "
                                "(~5 мин) — запись остановлена до конца эфира")
                    break
                # ссылка не получена: подождём и попробуем снова (эфир мог
                # перезапуститься, или платформа временно не отвечает)
                self._stop.wait(5.0)
            else:
                fails = 0
                self.url = new_url
        self.is_recording = False
        self._close_fflog()

    def _discover(self):
        """Новые сегменты на диске -> карта времени появления."""
        for path in glob.glob(os.path.join(self.workdir, "seg_*.ts")):
            if path in self.file_times:
                continue
            m = SEG_RE.search(path)
            if not m:
                continue
            self.file_times[path] = time.time()
            self.counter = max(self.counter, int(m.group(1)) + 1)
            self.last_new_file_ts = time.time()

    def _retain(self):
        """Удаление старых незарезервированных сегментов за пределами буфера."""
        files = sorted(self.file_times.items(), key=lambda kv: kv[1])
        total = len(files) * self.seg_time
        if total <= self.buffer_keep:
            return
        drop = len(files) - int(self.buffer_keep / self.seg_time)
        for path, _t in files[:drop]:
            if path in self.reserved:
                continue
            try:
                os.remove(path)
            except OSError:
                continue  # файл занят (Windows) — уберём в следующий раз
            self.file_times.pop(path, None)

    def _open_fflog(self):
        if self._fflog is None:
            try:
                self._fflog = open(os.path.join(self.workdir, "ffmpeg.log"), "ab")
            except OSError:
                self._fflog = None

    def _close_fflog(self):
        if self._fflog:
            try:
                self._fflog.close()
            except Exception:
                pass
        self._fflog = None

    # --- работа с сегментами ------------------------------------------------------

    def mark_ad(self, t0, t1=None):
        """Пометить интервал [t0, t1] как рекламу (сегменты не попадут в клипы)."""
        if t1 is None:
            t1 = time.time()
        if t1 <= t0:
            return
        if self.ad_ranges and abs(self.ad_ranges[-1][1] - t0) < self.seg_time \
                and self.ad_ranges[-1][1] <= t0:
            # склеиваем подряд идущие рекламные интервалы
            self.ad_ranges[-1][1] = max(self.ad_ranges[-1][1], t1)
            return
        self.ad_ranges.append([t0, t1])

    def _in_ad(self, t0, t1):
        """Перекрывается ли интервал [t0, t1] с какой-то рекламной паузой."""
        for a0, a1 in self.ad_ranges:
            if a1 >= t0 and a0 <= t1:
                return True
        return False

    def reserve_segments(self, start_ts, end_ts=None, skip_ad=True):
        """Зарезервировать сегменты, перекрывающие интервал [start_ts, end_ts].

        Резерв защищает файлы от вытеснения из буфера. Возвращает пути в
        хронологическом порядке. Границы сегментов известны с точностью
        до длительности сегмента (сегмент появляется на диске в момент записи).

        skip_ad: сегменты, записанные во время рекламы, пропускаются;
        если рекламных сегментов в интервале слишком много и чистых почти
        нет — клип всё равно соберётся (лучше с рекламой, чем никакого).
        """
        now = time.time()
        end_ts = now if end_ts is None else end_ts
        picked = []
        for path, t0 in sorted(self.file_times.items(), key=lambda kv: kv[1]):
            t1 = t0 + self.seg_time
            if t1 >= start_ts and t0 <= end_ts:
                picked.append(path)
        if skip_ad and picked:
            clean = [p for p in picked
                     if not self._in_ad(self.file_times[p],
                                        self.file_times[p] + self.seg_time)]
            # есть хоть какие-то чистые сегменты — берём только их; совсем
            # чистых нет (весь интервал — реклама) — берём всё: лучше клип
            # с рекламой, чем никакого
            if clean:
                picked = clean
        self.reserved.update(picked)
        return picked

    def release(self, paths):
        for p in paths or []:
            self.reserved.discard(p)

    def cleanup(self, keep=None):
        """Удалить временные сегменты (кроме зарезервированных и указанных)."""
        keep = set(keep or [])
        for path in list(self.file_times):
            if path in self.reserved or path in keep:
                continue
            try:
                os.remove(path)
            except OSError:
                pass
            else:
                self.file_times.pop(path, None)
