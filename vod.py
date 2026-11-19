"""Нарезка ГОТОВЫХ видео и клипов: YouTube-видео, записи и клипы Twitch.

Ссылка на конкретное видео (не канал) -> бот сам находит самые интересные
минуты и превращает их в вертикальные клипы 60 секунд с субтитрами и баннером.

Как выбираются интересные моменты:
  1) yt-dlp скачивает ЗВУК всего видео в самом низком качестве (быстро);
  2) ffmpeg декодирует его в моно, считаем громкость по секундам (RMS);
  3) для каждой минуты считаем СКОЛЬКО признаков «накала» в ней:
       - средняя громкость выше обычного для этого видео;
       - ДИНАМИКА (всплески на фоне среднего), а не монотонный шум;
       - доля ЯРКИХ секунд (громкость в верхнем регистре видео);
       - ШТРАФ за тишину: минута с паузами почти всегда скучнее.
     Оценка в децибелах — тихие и громкие видео сравнимы между собой.
  4) окна сортируются по оценке: ПЕРВЫМ нарезается самое сочное, а не
     самое раннее — сотня клипов из 9-часовой записи начинается с лучших.

Дальше каждое окно: yt-dlp качает ТОЛЬКО этот кусок (--download-sections) ->
9:16 -> субтитры Whisper -> баннер -> готовый mp4 (публикует gh_run.py).

Работает и в пакете clipper/, и в плоском репозитории (GitHub-режим).
"""

import os
import re
import subprocess
import time

try:
    import banner
    import subtitle
    import vertical as vertical_mod
    import overlays as overlays_mod
    from hype_detector import DEFAULT_KEYWORDS, _is_excited
    from utils import ensure_dir, ffprobe_meta, run_cmd, sanitize
except ImportError:                                    # плоский репозиторий
    import banner
    import subtitle
    import vertical as vertical_mod
    import overlays as overlays_mod
    from hype_detector import DEFAULT_KEYWORDS, _is_excited
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


def pick_windows(rms, duration, max_windows=100, win=WIN_SECONDS,
                 chat=None, chat_exc=None, exclude=None):
    """Окна самых интересных минут: [(start, end, score), ...] по времени.

    «Интересное» — это взрыв смеха, крик, накал момента. В децибелах
    (dBFS) оцениваем каждую минуту видео:

      * средняя громкость окна выше медианы видео (z-оценка);
      * ДИНАМИКА std: громкие всплески на фоне среднего, а не ровный шум
        (музыка/двигатель/вентилятор дают высокий RMS без событий);
      * устойчивый пик (95-й перцентиль вместо максимума: один щелчок
        или звонок не должен решать);
      * доля ЯРКИХ секунд — окно целиком в накале ценится выше, чем
        минута с одним пиком и 50 секундами тишины;
      * ШТРАФ за тишину: доля секунд ниже «тихого» уровня этого видео.

    ЕСЛИ ЕСТЬ ЧАТ-РЕПЛЕЙ (записи Twitch), к аудио добавляется чат —
    он показывает то, чего звук не слышит: где было СМЕШНО. Итоговая
    оценка = аудио (динамика/пики) + активность чата + доля восторженных
    сообщений. Чат весит примерно наравне со звуком.

    exclude — интервалы «скучно» из фидбека сайта: такие окна не выбираются.

    Гарантируем непересечение: между окнами минимум max(240 с, длина/100) —
    так 9-часовое видео даёт ~100 клипов, а часовой — ~15, и все «лучшие».
    Возврат отсортирован ПО ОЦЕНКЕ (самое интересное — первым): нарезка
    длинного видео начинается с лучших минут, а остаток цепочкой продолжит
    следующий запуск.
    """
    import numpy as np
    n = len(rms)
    if duration <= 0:
        duration = float(n)
    if duration <= win + 30:
        return [(0, int(duration), 0.0)]
    w = int(win)
    if n < w * 2:
        return [(0, int(duration), 0.0)]

    db = 20.0 * np.log10(np.maximum(np.asarray(rms, dtype=np.float64), 1e-6))
    windows = np.lib.stride_tricks.sliding_window_view(db, w)
    mean_db = windows.mean(axis=1)
    std_db = windows.std(axis=1)
    peak_db = np.percentile(windows, 95, axis=1)

    # глобальные ориентиры ЭТОГО видео (тишина и накал у всех разные)
    floor_db = float(np.percentile(db, 10))
    loud_thr = float(np.percentile(db, 80))
    silent = (windows <= floor_db + 3.0).mean(axis=1)
    loud = (windows >= loud_thr).mean(axis=1)

    def _z(x):
        s = float(np.std(x))
        return (x - float(np.median(x))) / (s + 1e-9)

    score = (0.42 * _z(mean_db) + 0.20 * _z(peak_db) + 0.28 * _z(std_db)
             + 0.55 * _z(loud) - 1.10 * silent)

    # --- чат-реплей: где было СМЕШНО по ощущениям ЧАТА ----------------------
    if chat is not None:
        try:
            chat = np.asarray(chat, dtype=np.float64)
            exc = (np.asarray(chat_exc, dtype=np.float64)
                   if chat_exc is not None else np.zeros_like(chat))
            # выравниваем длину с rms (метаданные бывают чуть короче)
            if chat.size < n:
                chat = np.pad(chat, (0, n - chat.size))
                exc = np.pad(exc, (0, n - exc.size))
            elif chat.size > n:
                chat, exc = chat[:n], exc[:n]
            cw = np.lib.stride_tricks.sliding_window_view(chat, w)
            ew = np.lib.stride_tricks.sliding_window_view(exc, w)
            rate_w = cw.sum(axis=1) / float(w)              # сообщ/сек в окне
            hot_w = np.divide(ew.sum(axis=1), np.maximum(cw.sum(axis=1), 1.0))
            chat_score = 1.05 * _z(rate_w) + 0.75 * _z(hot_w)
            score = score + chat_score
            log.info("Чат учитан в оценке окон (среднее сообщ/сек: %.2f)",
                     float(rate_w.mean()))
        except Exception:
            log.exception("Чат-оценка не удалась — беру только громкость")

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
        if _exclude_hit(t, w, exclude):
            continue
        if any(abs(t - c) < min_gap for c in chosen):
            continue
        chosen.append(t)
        if len(chosen) >= max_windows:
            break
    # самое интересное — первым: длинное видео начинается с лучших минут
    chosen.sort(key=lambda t: -float(score[t]))
    return [(int(t), int(t + win), round(float(score[t]), 3)) for t in chosen]


def expand_video(url, workdir, cookies_file="", max_windows=100,
                 exclude_ranges=None):
    """(title, [(start, end, score), ...]) — разметка на интересные окна.

    Для записей Twitch (VOD) СКАЧИВАЕТСЯ ЧАТ-РЕПЛЕЙ: по чату момент видно
    лучше, чем по громкости — взрыв смеха и спама почти всегда интереснее
    просто громкого звука. Оценка окна = громкость (динамика, всплески)
    + активность чата + доля восторженных сообщений.

    exclude_ranges — интервалы [(начало, конец)] из фидбека сайта (кнопка
    «Скучно»): эти места видео при выборе окон ПРОПУСКАЮТСЯ.
    """
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
    is_twitch_vod = "twitch." in url and ("/videos/" in url or "/v/" in url)
    chat = chat_exc = None
    if is_twitch_vod:
        try:
            chat_path = fetch_chat_replay(url, str(workdir), cookies_file)
            if chat_path:
                chat, chat_exc = parse_chat_vtt(chat_path, dur)
                total = int(chat.sum()) if chat is not None else 0
                log.info("Чат-реплей: %d сообщений — отбора по чату включён", total)
        except Exception as e:
            log.warning("Чат-реплей недоступен (%s) — отбираю по громкости", e)
    wins = pick_windows(rms, dur, max_windows=max_windows,
                        chat=chat, chat_exc=chat_exc,
                        exclude=exclude_ranges)
    if wins:
        log.info("Выбрано окон: %d; лучшая минута на %d с (оценка %.2f)",
                 len(wins), wins[0][0], wins[0][2])
    else:
        log.info("Выбрано окон: 0")
    return title, wins


# --- чат-реплей Twitch: сообщения как главный сигнал «интересного» -------------------

def fetch_chat_replay(url, workdir, cookies_file=""):
    """Скачать чат-реплей записи Twitch (без регистрации и ключей).

    yt-dlp умеет качать чат как субтитры (язык "chat"): каждое сообщение
    с точным временем. Возвращает путь к .vtt или None (для клипов Twitch
    и YouTube чат-реплея нет — там отбор по громкости).
    """
    out = "chat_replay"
    cmd = ytdlp_base(cookies_file) + [
        "--skip-download", "--write-subs", "--sub-langs", "chat",
        "--sub-format", "vtt/srt", "-o", out, url]
    rc, _o, _e = run_cmd(cmd, timeout=600, cwd=str(workdir))
    if rc != 0:
        return None
    for cand in (os.path.join(str(workdir), out + ".chat.vtt"),
                 os.path.join(str(workdir), out + ".vtt")):
        if os.path.exists(cand):
            return cand
    return None


_VTT_TS_RE = re.compile(
    r"(\d+):(\d+):(\d+)\.(\d+)\s*-->\s*(\d+):(\d+):(\d+)\.(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")


def parse_chat_vtt(path, duration):
    """Чат-реплей .vtt -> (counts, excited) — сообщения по секундам.

    counts — сколько сообщений пришлось на каждую секунду видео,
    excited — сколько из них «восторженных» (смех, КАПС, эмоуты, ключевые
    слова — тот же детектор, что и в эфире). Это даёт отбору то, чего
    громкость не видит: смешно ли там было ЧАТУ.
    """
    import numpy as np
    n = int(max(float(duration or 0), 1.0)) + 1
    counts = np.zeros(n, dtype=np.float64)
    excited = np.zeros(n, dtype=np.float64)
    with open(str(path), "r", encoding="utf-8", errors="replace") as f:
        block_ts = None
        block_text = []

        def flush():
            nonlocal block_ts, block_text
            if block_ts is not None and block_text:
                sec = min(int(block_ts), n - 1)
                text = " ".join(block_text)
                user = ""
                body = text
                if ":" in text and text.index(":") < 40:
                    user, _, body = text.partition(":")
                counts[sec] += 1
                if _is_excited(body, DEFAULT_KEYWORDS) or \
                        _is_excited(user, DEFAULT_KEYWORDS):
                    excited[sec] += 1
            block_ts = None
            block_text = []

        for line in f:
            line = line.strip()
            if not line:
                continue
            m = _VTT_TS_RE.search(line)
            if m:
                flush()
                block_ts = int(m.group(1)) * 3600 + int(m.group(2)) * 60 \
                    + int(m.group(3))
                continue
            if line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", " cue")):
                continue
            clean = _TAG_RE.sub("", line).strip()
            if clean and block_ts is not None:
                block_text.append(clean)
        flush()
    if counts.sum() < 1:
        return None, None
    return counts, excited


def _exclude_hit(t, w, exclude):
    """Окно [t, t+w) сильно перекрывается с «скучным» диапазоном?"""
    for a, b in exclude or []:
        ov = min(t + w, float(b)) - max(t, float(a))
        if ov > w * 0.35:
            return True
    return False


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
    баннер по центру. Возвращает (путь к готовому mp4, мета субтитров):
    мета идёт в clips.json (бейдж «Субтитры» на карточке) и в уведомление,
    а gh_run по ней умеет ПРОПУСКАТЬ окна без речи (настройка на сайте).
    """
    ensure_dir(str(workdir))
    ensure_dir(str(clips_dir))
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    vraw = os.path.join(str(workdir), "vod_raw.mp4")
    vertical_mp4 = os.path.join(str(workdir), "vod_9x16.mp4")

    src_video = os.path.abspath(str(src_video))
    meta = ffprobe_meta(src_video)
    dur = float(meta.get("duration") or 0)

    # --- замазка ЧУЖИХ банеров/виджетов/рекламы (кроме RubUp) -----------------
    # Детект на исходнике (там оверлеи резкие), координаты -> вертикальный кадр;
    # сами зоны замазываются в цепочке 9:16 без лишнего перекодирования.
    clip_cfg = cfg.get("clip") or {}
    mask_boxes = None
    if bool(clip_cfg.get("hide_overlays", True)) and bool(clip_cfg.get("vertical", True)):
        try:
            raw_boxes = overlays_mod.detect_overlay_boxes(src_video, workdir)
            if raw_boxes:
                mask_boxes = overlays_mod.map_boxes(
                    raw_boxes, int(meta.get("width") or 0),
                    int(meta.get("height") or 0),
                    mode=str(clip_cfg.get("vertical_mode", "blur")))
        except Exception:
            log.exception("Замазка оверлеев не удалась — клип пойдёт без неё")
            mask_boxes = None

    cur = src_video
    if bool((cfg.get("clip") or {}).get("vertical", True)):
        try:
            cur = vertical_mod.make_vertical(
                src_video, vertical_mp4, workdir,
                mode=str((cfg.get("clip") or {}).get("vertical_mode", "blur")),
                vcodec_args=vcodec_args, mask_boxes=mask_boxes)
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
    smeta = {"lang": "", "words": 0, "speech_pct": 0, "ok": False}
    try:
        ass, smeta = subtitle.make_subtitles_meta(
            cur, str(workdir), cfg.get("subtitle") or {},
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
    return out_path, smeta


# --- контроль качества ПОСЛЕ нарезки ----------------------------------------------------------

def clip_quality(mp4_path, smeta=None, base=None, duration=None):
    """Финальная оценка «сочности» готового клипа 0..100.

    Практика показала: скучные клипы почти всегда весят ~2 МБ, а живые —
    30-45 МБ. Размер — честный сигнал: статичная сцена (игра стоит, камера
    не двигается, только голос поверх) сжимается в копейки, а взрыв действия
    (движение, огонь, толпа, драки) весит в 10-20 раз больше.

    Итог = база (хайп чата / оценка окна) + три добавки:
      * БИТРЕЙТ (Мбит/с): сколько «движения» было в кадре;
      * ЗВУК: средняя громкость + динамика (всплески) + штраф за тишину;
      * РЕЧЬ: сколько слов распознал Whisper — говорливые клипы живее.

    Возвращает (оценка, детали) — детали попадают в лог, чтобы было видно,
    ПОЧЕМУ клип забракован (и в отладку порогов).
    """
    import numpy as np
    try:
        meta = ffprobe_meta(mp4_path)
        dur = float(duration or meta.get("duration") or 0) or 60.0
        size = os.path.getsize(str(mp4_path))
    except Exception:
        dur, size = 60.0, 0
    mbps = size * 8.0 / max(dur, 1.0) / 1e6
    try:
        rms = loudness_curve(mp4_path)
        db = 20.0 * np.log10(np.maximum(rms, 1e-6))
        mean_db = float(np.mean(db))
        std_db = float(np.std(db))
        silent_r = float((db <= float(np.mean(db)) - 14.0).mean())
    except Exception:
        mean_db, std_db, silent_r = -30.0, 3.0, 0.5

    # --- битрейт: <0.9 Мбит/с — статичный кадр (типичный «скучный 2 МБ»)
    if mbps < 0.9:
        d_bit = -30.0
    elif mbps < 1.6:
        d_bit = -12.0
    elif mbps < 2.5:
        d_bit = 0.0
    elif mbps < 3.5:
        d_bit = 5.0
    else:
        d_bit = 9.0

    # --- звук: крик/смех/взрывы громче ровного фона; тишина — штраф
    if mean_db < -34.0:
        d_snd = -9.0
    elif mean_db < -22.0:
        d_snd = 0.0
    elif mean_db < -14.0:
        d_snd = 5.0
    else:
        d_snd = 8.0
    if std_db >= 6.0:
        d_snd += 3.0               # динамика: всплески на фоне среднего
    if silent_r > 0.5:
        d_snd -= 7.0               # половина клипа — мёртвая тишина

    # --- речь: сколько слов распознано за клип
    words = int((smeta or {}).get("words") or 0)
    if words >= 80:
        d_sub = 6.0
    elif words >= 40:
        d_sub = 3.0
    elif words < 10:
        d_sub = -9.0
    else:
        d_sub = 0.0

    b = 50.0 if base is None else max(0.0, min(100.0, float(base)))
    score = max(0.0, min(100.0, b + d_bit + d_snd + d_sub))

    # --- ЖЁСТКИЙ ФЛОР: практика показала, что скучные клипы — это почти
    # всегда либо статичная картинка (~0.3 Мбит/с, файл ~2 МБ), либо обрезок.
    # Такой клип не публикуется независимо от хайп-оценки окна.
    hard = ""
    if dur and dur < 45.0:
        score = min(score, 18.0)
        hard = "короче 45 с"
    elif mbps < 0.5:
        score = min(score, 22.0)
        hard = "статичный кадр (%.2f Мбит/с, ~2 МБ-класс)" % mbps

    parts = {"score": round(score, 1), "base": round(b, 1),
             "mbps": round(mbps, 2), "mean_db": round(mean_db, 1),
             "std_db": round(std_db, 1), "silent": round(silent_r, 2),
             "words": words, "d_bit": d_bit, "d_snd": round(d_snd, 1),
             "d_sub": d_sub, "hard": hard}
    if hard:
        log.info("Качество клипа: %.0f/100 — ЖЁСТКИЙ ФЛОР (%s): бракуем", score, hard)
        return score, parts
    log.info("Качество клипа: %.0f/100 (база %.0f, битрейт %.2f Мбит/с %+.0f, "
             "звук %.0f dB %+.0f, слов %d %+.0f)", score, b, mbps, d_bit,
             mean_db, d_snd, words, d_sub)
    return score, parts
