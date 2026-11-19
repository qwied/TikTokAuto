"""Нарезка ГОТОВЫХ видео и клипов: YouTube-видео, записи и клипы Twitch.

Ссылка на конкретное видео (не канал) -> бот сам находит самые интересные
минуты и превращает их в вертикальные клипы 60 секунд с субтитрами и баннером.

Как выбираются интересные моменты:
  1) yt-dlp скачивает ЗВУК всего видео в самом низком качестве (быстро);
  2) ffmpeg декодирует его в моно, считаем громкость по секундам (RMS);
  3) окно 60 с с самым громким «живым» пиком = самый интересный момент.
     Всплески громкости почти всегда совпадают со смехом, криками и
     яркими моментами — это и есть «интересное».

Дальше каждое окно: yt-dlp качает ТОЛЬКО этот кусок (--download-sections) ->
9:16 -> субтитры Whisper -> баннер -> готовый mp4 (публикует gh_run.py).

Работает и в пакете clipper/, и в плоском репозитории (GitHub-режим).
"""

import os
import subprocess
import time

try:
    import banner
    import subtitle
    import vertical as vertical_mod
    from utils import ensure_dir, ffprobe_meta, run_cmd, sanitize
except ImportError:                                    # плоский репозиторий
    import banner
    import subtitle
    import vertical as vertical_mod
    from utils import ensure_dir, ffprobe_meta, run_cmd, sanitize

log = __import__("logging").getLogger("vod")

WIN_SECONDS = 60.0          # длина клипа-окна
TRIM_HEAD = 120.0           # пропустить интро (не больше 2% длины)
TRIM_TAIL = 60.0            # и последние секунды (там обычно концовка/титры)


def _cookies_args(cookies_file):
    return ["--cookies", str(cookies_file)] if cookies_file else []


def ytdlp_base(cookies_file=""):
    return ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "30",
            "--retries", "5", "--force-ipv4"] + _cookies_args(cookies_file)


def probe_video(url, cookies_file=""):
    """(title, duration_seconds) без скачивания видео."""
    cmd = ytdlp_base(cookies_file) + [
        "--skip-download", "--print", "%(title)s|||%(duration)s", url]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError("yt-dlp (инфо): " + (p.stderr or "")[-400:])
    line = (p.stdout or "").strip().splitlines()[-1] if p.stdout.strip() else ""
    title, _, dur = line.partition("|||")
    try:
        dur = float(dur)
    except ValueError:
        dur = 0.0
    return (title or os.path.basename(url)).strip(), dur


def download_audio(url, out_path, workdir, cookies_file=""):
    """Самый дешёвый звуковой поток всего видео (для оценки интересности)."""
    out = os.path.basename(str(out_path))
    cmd = ytdlp_base(cookies_file) + [
        "-f", "worstaudio/bestaudio/worst", "-o", out, url]
    rc, _o, err = run_cmd(cmd, timeout=1800, cwd=str(workdir))
    if rc != 0:
        raise RuntimeError("yt-dlp (звук): " + err[-400:])
    path = os.path.join(str(workdir), out)
    if not os.path.exists(path):
        raise RuntimeError("yt-dlp не сохранил звук: " + err[-300:])
    return path


def loudness_curve(audio_path, sr=4000):
    """Громкость по секундам: ffmpeg -> моно -> RMS на каждую секунду.

    Декодирование идёт потоком: память не зависит от длины видео
    (даже 9-часовая запись -> список из ~32 тысяч чисел).
    """
    import numpy as np
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", str(audio_path), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    rms_out = []
    carry = np.empty(0, dtype=np.int16)
    try:
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            take = (len(chunk) // 2) * 2
            data = np.concatenate(
                [carry, np.frombuffer(chunk[:take], dtype=np.int16)])
            nsec = data.size // sr
            if nsec:
                block = data[:nsec * sr].reshape(nsec, sr).astype(np.float64)
                rms_out.extend(np.sqrt((block * block).mean(axis=1)) / 32768.0)
                data = data[nsec * sr:]
            carry = data
        # хвост меньше секунды — усредняем как частичную секунду
        if carry.size >= sr // 4:
            f = carry.astype(np.float64)
            rms_out.append(float(np.sqrt((f * f).mean()) / 32768.0))
    finally:
        proc.terminate()
    if not rms_out:
        raise RuntimeError("ffmpeg не декодировал звук")
    return np.asarray(rms_out, dtype=np.float64)


def pick_windows(rms, duration, max_windows=100, win=WIN_SECONDS):
    """Окна самых интересных минут: [(start_sec, end_sec), ...] по времени.

    Гарантируем непересечение: между окнами минимум max(240 с, длина/100) —
    так 9-часовое видео даёт ~100 клипов, а часовой — ~15, и все «лучшие».
    """
    import numpy as np
    n = len(rms)
    if duration <= 0:
        duration = float(n)
    if duration <= win + 30:
        return [(0, int(duration))]
    w = int(win)
    if n < w * 2:
        return [(0, int(duration))]

    # скользящее окно: средняя громкость + доля пиков (всплесков) сверху
    windows = np.lib.stride_tricks.sliding_window_view(rms, w)
    mean = windows.mean(axis=1)
    peak = windows.max(axis=1)
    score = mean + 0.5 * peak

    head = int(min(TRIM_HEAD, duration * 0.02))
    tail = int(min(TRIM_TAIL, duration * 0.02))
    lo, hi = head, max(head + 1, n - w - tail)
    if hi <= lo:
        lo, hi = 0, n - w

    cand = sorted(range(lo, hi), key=lambda i: -float(score[i]))
    min_gap = max(240.0, duration / max(1, max_windows))
    chosen = []
    for i in cand:
        t = i  # окно [i, i+win)
        if any(abs(t - c) < min_gap for c in chosen):
            continue
        chosen.append(t)
        if len(chosen) >= max_windows:
            break
    return [(int(t), int(t + win)) for t in sorted(chosen)]


def expand_video(url, workdir, cookies_file="", max_windows=100):
    """(title, [(start, end), ...]) — разметка видео на интересные окна."""
    ensure_dir(str(workdir))
    title, dur = probe_video(url, cookies_file)
    log.info("Видео: %s (%.0f c) — ищу интересные моменты", title, dur)
    audio = download_audio(url, "vod_audio.bin", workdir, cookies_file)
    try:
        rms = loudness_curve(audio)
    finally:
        try:
            os.remove(audio)
        except OSError:
            pass
    wins = pick_windows(rms, dur, max_windows=max_windows)
    log.info("Выбрано окон: %d (первое на %d с)", len(wins), wins[0][0] if wins else 0)
    return title, wins


def download_section(url, start, end, out_path, workdir, cookies_file=""):
    """Скачать ТОЛЬКО кусок [start, end] видео (yt-dlp --download-sections)."""
    out = os.path.basename(str(out_path))
    def hms(t):
        t = max(0, int(t))
        return "%02d:%02d:%02d" % (t // 3600, (t % 3600) // 60, t % 60)
    cmd = ytdlp_base(cookies_file) + [
        "--download-sections", "*%s-%s" % (hms(start), hms(end)),
        "--force-keyframes-at-cuts",
        "-f", "bv*[height<=1080][vcodec^=avc1]+ba/b[height<=1080][vcodec^=avc1]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "4",
        "-o", out, url]
    rc, _o, err = run_cmd(cmd, timeout=1800, cwd=str(workdir))
    path = os.path.join(str(workdir), out)
    if rc != 0 or not os.path.exists(path):
        raise RuntimeError("yt-dlp (кусок %s-%s): %s" % (hms(start), hms(end), err[-400:]))
    return path


def download_clip_full(url, out_path, workdir, cookies_file=""):
    """Скачать клип Twitch целиком (короткий)."""
    out = os.path.basename(str(out_path))
    cmd = ytdlp_base(cookies_file) + [
        "-f", "bv*+ba/b", "--merge-output-format", "mp4",
        "-o", out, url]
    rc, _o, err = run_cmd(cmd, timeout=1200, cwd=str(workdir))
    path = os.path.join(str(workdir), out)
    if rc != 0 or not os.path.exists(path):
        raise RuntimeError("yt-dlp (клип): " + err[-400:])
    return path


def process_clip(src_video, clips_dir, workdir, cfg, display, platform,
                 vcodec_args=None, banner_path=None):
    """Кусок видео -> вертикальный клип 60 с с субтитрами и баннером.

    Те же шаги, что у клипов из эфира (pipeline): 9:16 -> субтитры Whisper ->
    баннер по центру. Возвращает путь к готовому mp4 в clips/.
    """
    ensure_dir(str(workdir))
    ensure_dir(str(clips_dir))
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    vraw = os.path.join(str(workdir), "vod_raw.mp4")
    vertical_mp4 = os.path.join(str(workdir), "vod_9x16.mp4")

    src_video = os.path.abspath(str(src_video))
    meta = ffprobe_meta(src_video)
    dur = float(meta.get("duration") or 0)

    cur = src_video
    if bool((cfg.get("clip") or {}).get("vertical", True)):
        try:
            cur = vertical_mod.make_vertical(
                src_video, vertical_mp4, workdir,
                mode=str((cfg.get("clip") or {}).get("vertical_mode", "blur")),
                vcodec_args=vcodec_args)
        except Exception:
            log.exception("9:16 не удался — клип останется горизонтальным")
            cur = src_video

    intervals = None
    if banner_path:
        try:
            bmeta = ffprobe_meta(banner_path)
            intervals = banner.plan_intervals(
                dur or float(ffprobe_meta(cur).get("duration") or 60),
                float(bmeta.get("duration") or 0),
                start=(cfg.get("banner") or {}).get("start", 8))
        except Exception:
            intervals = None

    ass = None
    try:
        ass = subtitle.make_subtitles(cur, str(workdir), cfg.get("subtitle") or {},
                                      str(cfg.get("speech_language", "ru")), [],
                                      duration=dur, skip_intervals=intervals)
    except Exception:
        log.exception("Субтитры не собрались — клип будет без них")

    out_tmp = os.path.join(str(workdir), "vod_final.mp4")
    va = list(vcodec_args) if vcodec_args else \
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    done = False
    if banner_path:
        for enc in (va, x264):
            try:
                banner.compose(cur, banner_path, ass, out_tmp, str(workdir),
                               vcodec_args=enc,
                               cfg=cfg.get("banner") or {}, intervals=intervals)
                done = True
                break
            except Exception as e:
                log.warning("Баннер не собрался (%s) — пробую x264", e)
    if not done and ass:
        for enc in (va, x264):
            try:
                subtitle.burn_subtitles(cur, ass, out_tmp, str(workdir),
                                        vcodec_args=enc)
                done = True
                break
            except Exception:
                log.exception("Субтитры не вшились")
    if not done:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
               "-i", os.path.basename(cur), "-c", "copy",
               "-movflags", "+faststart", os.path.basename(out_tmp)]
        rc, _o, err = run_cmd(cmd, timeout=600, cwd=str(workdir))
        if rc != 0:
            raise RuntimeError("ffmpeg (упаковка): " + err[-300:])

    out_name = "%s_%s.mp4" % (stamp, sanitize(str(display) or "video"))
    out_path = os.path.join(str(clips_dir), out_name)
    os.replace(out_tmp, out_path)
    return out_path
