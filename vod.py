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

import json
import os
import re
import subprocess
import time

try:
    import banner
    import lipsync as lipsync_mod
    import subtitle
    import vertical as vertical_mod
    import overlays as overlays_mod
    import webcam as webcam_mod
    from hype_detector import DEFAULT_KEYWORDS, _is_excited
    from utils import ensure_dir, ffprobe_meta, run_cmd, sanitize
except ImportError:                                    # плоский репозиторий
    import banner
    import lipsync as lipsync_mod
    import subtitle
    import vertical as vertical_mod
    import overlays as overlays_mod
    import webcam as webcam_mod
    from hype_detector import DEFAULT_KEYWORDS, _is_excited
    from utils import ensure_dir, ffprobe_meta, run_cmd, sanitize

log = __import__("logging").getLogger("vod")

WIN_SECONDS = 60.0          # длина клипа-окна
TRIM_HEAD = 120.0           # пропустить интро (не больше 2% длины)
TRIM_TAIL = 60.0            # и последние секунды (там обычно концовка/титры)


class NoFaceClip(Exception):
    """Стримера в кадре нет (вебкамера не найдена) — клип не собирается.

    Жалоба пользователя «опять без лица»: клипы с турнирных трансляций и
    стримов без камеры для TikTok не годятся — их не публикуем вовсе,
    а не выкладываем «кривые». Гейт активен только при рабочем OpenCV.
    """


def _cookies_args(cookies_file):
    return ["--cookies", str(cookies_file)] if cookies_file else []


def ytdlp_base(cookies_file=""):
    return ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "30",
            "--retries", "5", "--force-ipv4"] + _cookies_args(cookies_file)


def _is_youtube(url):
    u = str(url or "")
    return "youtube.com/watch" in u or "youtu.be/" in u or "youtube.com/live/" in u


def probe_video(url, cookies_file=""):
    """(title, duration_seconds, live_status) без скачивания видео.

    live_status нужен чат-реплею: архивный чат YouTube есть только у видео,
    которые БЫЛИ трансляциями (was_live/post_live). У обычных загрузок чата
    нет — отбор пойдёт по громкости.
    """
    cmd = ytdlp_base(cookies_file) + [
        "--skip-download", "--print",
        "%(title)s|||%(duration)s|||%(live_status)s", url]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError("yt-dlp (инфо): " + (p.stderr or "")[-400:])
    line = (p.stdout or "").strip().splitlines()[-1] if p.stdout.strip() else ""
    parts = line.split("|||")
    title = parts[0] if parts else ""
    try:
        dur = float(parts[1]) if len(parts) > 1 else 0.0
    except ValueError:
        dur = 0.0
    live_status = (parts[2].strip() if len(parts) > 2 else "")
    if live_status.lower() in ("", "none", "na", "null"):
        live_status = ""
    return (title or os.path.basename(url)).strip(), dur, live_status


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
    # Жалоба «скучные моменты»: когда чат известен, ОН ГЛАВНЫЙ сигнал,
    # а громкость — вторична (чат весит ~60% итога). Мёртвый реплей
    # (< 0.02 сообщ/сек в среднем) игнорируется как шум.
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
            if float(chat.mean()) >= 0.02:
                cw = np.lib.stride_tricks.sliding_window_view(chat, w)
                ew = np.lib.stride_tricks.sliding_window_view(exc, w)
                rate_w = cw.sum(axis=1) / float(w)              # сообщ/сек в окне
                hot_w = np.divide(ew.sum(axis=1), np.maximum(cw.sum(axis=1), 1.0))
                audio_score = (0.42 * _z(mean_db) + 0.20 * _z(peak_db)
                               + 0.28 * _z(std_db) + 0.55 * _z(loud) - 1.10 * silent)
                chat_score = 1.30 * _z(rate_w) + 1.00 * _z(hot_w)
                score = 0.70 * audio_score + 1.60 * chat_score
                log.info("Чат — ГЛАВНЫЙ сигнал отбора (среднее сообщ/сек: %.2f); "
                         "окна с пиком чата идут первыми", float(rate_w.mean()))
            else:
                log.info("Чат-реплей мёртвый (< 0.02 сообщ/сек) — отбор по громкости")
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

    # Адаптивная длина окна (как у живых клипов): жирный момент -> длиннее.
    # Одинаковые «минутные» клипы наскучивают, а короткий досматривают
    # до конца чаще (главный сигнал алгоритма TikTok).
    def _win_len(s):
        if s >= 1.2:
            return int(win)          # 60 с — реально жирный момент
        if s >= 0.4:
            return 45                # средний момент
        return 35                    # обычный момент

    out = []
    for t in chosen:
        end = int(min(t + _win_len(float(score[t])), max(t + 1, duration)))
        out.append((int(t), end, round(float(score[t]), 3)))
    return out


def expand_video(url, workdir, cookies_file="", max_windows=100,
                 exclude_ranges=None):
    """(title, [(start, end, score), ...]) — разметка на интересные окна.

    ЧАТ-РЕПЛЕЙ — главный сигнал «интересного» (жалоба «скучные моменты»):
      * записи Twitch: yt-dlp качает чат как субтитры (язык "chat");
      * YouTube-записи трансляций (was_live/post_live): yt-dlp качает
        АРХИВНЫЙ live-чат (язык "live_chat", каждая строка — action с
        videoOffsetTimeMsec); по чату видно, где было СМЕШНО.
    Оценка окна = активность чата + восторженные сообщения (вес больше)
    + громкость (динамика, всплески). Если чата нет — только громкость.

    exclude_ranges — интервалы [(начало, конец)] из фидбека сайта (кнопка
    «Скучно»): эти места видео при выборе окон ПРОПУСКАЮТСЯ.
    """
    ensure_dir(str(workdir))
    title, dur, live_status = probe_video(url, cookies_file)
    log.info("Видео: %s (%.0f c, эфир: %s) — ищу интересные моменты",
             title, dur, live_status or "нет")
    audio = download_audio(url, "vod_audio.bin", workdir, cookies_file)
    try:
        rms = loudness_curve(audio)
    finally:
        try:
            os.remove(audio)
        except OSError:
            pass
    is_twitch_vod = "twitch." in url and ("/videos/" in url or "/v/" in url)
    yt_archived = _is_youtube(url) and live_status in ("was_live", "post_live")
    chat = chat_exc = None
    if is_twitch_vod or yt_archived:
        try:
            chat_path = fetch_chat_replay(url, str(workdir), cookies_file)
            if chat_path:
                if yt_archived:
                    chat, chat_exc = parse_chat_jsonl(chat_path, dur)
                else:
                    chat, chat_exc = parse_chat_vtt(chat_path, dur)
                total = int(chat.sum()) if chat is not None else 0
                log.info("Чат-реплей: %d сообщений — отбор идёт ПО ЧАТУ", total)
            else:
                log.info("Чат-реплей пуст — отбираю по громкости")
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
    """Скачать чат-реплей записи (без регистрации и ключей).

    Twitch (VOD): yt-dlp качает чат как субтитры (язык "chat"), формат .vtt.
    YouTube (запись трансляции): yt-dlp качает АРХИВНЫЙ live-чат как субтитры
    (язык "live_chat", формат json3) — каждая строка JSON с позицией в видео.
    Возвращает путь к файлу чата или None (чат-реплея нет — отбор по громкости).
    """
    out = "chat_replay"
    if _is_youtube(url):
        cmd = ytdlp_base(cookies_file) + [
            "--skip-download", "--write-subs", "--sub-langs", "live_chat",
            "--sub-format", "json3", "-o", out, url]
        rc, _o, _e = run_cmd(cmd, timeout=900, cwd=str(workdir))
        if rc != 0:
            return None
        for cand in (os.path.join(str(workdir), out + ".live_chat.json"),
                     os.path.join(str(workdir), out + ".live_chat.json3"),
                     os.path.join(str(workdir), out + ".live_chat.vtt")):
            if os.path.exists(cand):
                return cand
        return None
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


def parse_chat_jsonl(path, duration):
    """Архивный чат YouTube (yt-dlp live_chat json3) -> (counts, excited).

    Каждая строка файла — action реплея чата: replayChatItemAction содержит
    videoOffsetTimeMsec (позиция в видео, мс) и вложенные действия чата
    (addChatItemAction.item.liveChat*MessageRenderer): автор, текст,
    суперчат-донат. counts — сообщений по секундам видео, excited — сколько
    из них восторженных/донатов (тот же детектор, что и в эфире).
    """
    import numpy as np
    n = int(max(float(duration or 0), 1.0)) + 1
    counts = np.zeros(n, dtype=np.float64)
    excited = np.zeros(n, dtype=np.float64)
    with open(str(path), "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                act = json.loads(line)
            except ValueError:
                continue
            rp = act.get("replayChatItemAction") or {}
            off_ms = rp.get("videoOffsetTimeMsec")
            if off_ms in (None, ""):
                continue
            try:
                sec = min(int(int(off_ms) / 1000), n - 1)
            except (TypeError, ValueError):
                continue
            if sec < 0:
                continue
            for sub in rp.get("actions") or []:
                item = (sub.get("addChatItemAction") or {}).get("item") or {}
                r = (item.get("liveChatTextMessageRenderer")
                     or item.get("liveChatPaidMessageRenderer")
                     or item.get("liveChatMembershipItemRenderer"))
                if not r:
                    continue
                text = "".join(run.get("text", "")
                               for run in (r.get("message") or {}).get("runs", []))
                user = (r.get("authorName") or {}).get("simpleText") or ""
                counts[sec] += 1
                paid = bool(item.get("liveChatPaidMessageRenderer"))
                if paid or _is_excited(text, DEFAULT_KEYWORDS) or \
                        _is_excited(user, DEFAULT_KEYWORDS):
                    excited[sec] += 1
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
    # Слой 1: детект СТАТИЧНЫХ оверлеев (виджеты с непрозрачной панелью,
    # соцсети, спонсорские плашки) по «неподвижности» пикселей за минуту.
    # Слой 2: КОНСТАНТНЫЕ ВЕРХНИЕ УГЛЫ — полупрозрачные и анимированные
    # виджеты донатов просвечивают фон (для детекта они «движутся»), но
    # висят они почти всегда в верхних углах, поэтому углы замазываются
    # всегда. Зоны передаются В КООРДИНАТАХ ИСХОДНИКА (raw_boxes): маппинг
    # под фактический макет (split/blur) делает make_vertical.
    # RubUp рисуется ПОСЛЕ — его замазка не трогает.
    clip_cfg = cfg.get("clip") or {}
    raw_boxes = None
    cam_box = None
    if bool(clip_cfg.get("hide_overlays", True)) and bool(clip_cfg.get("vertical", True)):
        try:
            # Окно камеры ищем ОДИН раз: исключаем его из детекта оверлеев
            # (вебкамера статична и выглядит «баннером» — прод 2026-11-20:
            # dmitry_lixxx, маска на лице) и передаём в make_vertical.
            try:
                cam_box = webcam_mod.detect_cam_box(src_video, workdir)
            except Exception:
                log.exception("Детект камеры упал — замазка без исключения")
                cam_box = None
            raw_boxes = overlays_mod.detect_overlay_boxes(
                src_video, workdir, exclude_boxes=[cam_box] if cam_box else [])
            if bool(clip_cfg.get("mask_corners", True)):
                raw_boxes = raw_boxes + overlays_mod.corner_boxes(
                    int(meta.get("width") or 0), int(meta.get("height") or 0))
        except Exception:
            log.exception("Замазка оверлеев не удалась — клип пойдёт без неё")
            raw_boxes = None

    # --- ГЕЙТ ЛИЦА: клипы без стримера в кадре не собираем ---------------------
    # Турниры (betboom_dota_ru2), стримы сайтов (solo) и прочие эфиры без
    # вебкамеры дают вертикальные клипы «без лица» — пользователь просил
    # такие не делать. Слабый детект (2 голоса) ловит тёмные/мелкие камеры;
    # гейт отключается сам, если на машине нет OpenCV.
    if bool(clip_cfg.get("vertical", True)) and webcam_mod.cv2_available():
        face_seen = cam_box is not None
        if not face_seen:
            try:
                face_seen = webcam_mod.detect_cam_box(
                    src_video, workdir, weak=True) is not None
            except Exception:
                log.exception("Слабый детект лица не удался — не блокирую клип")
                face_seen = True
        if not face_seen:
            log.info("Лица в кадре нет (вебкамера не найдена) — клип не собираю")
            raise NoFaceClip("вебкамера/лицо в исходнике не найдено")

    cur = src_video
    if bool((cfg.get("clip") or {}).get("vertical", True)):
        try:
            cur = vertical_mod.make_vertical(
                src_video, vertical_mp4, workdir,
                mode=str((cfg.get("clip") or {}).get("vertical_mode", "blur")),
                vcodec_args=vcodec_args, raw_boxes=raw_boxes, cam_box=cam_box,
                fallback=str(clip_cfg.get("vertical_fallback",
                                          vertical_mod.FALLBACK_DEFAULT)))
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
    # Лип-синк: субтитры только когда стример говорит (по движению губ).
    # В split полоса камеры известна из макета — без повторного детекта;
    # в crop без камеры лица в кадре нет — гейт невозможен.
    lip_iv = None
    vmode = str((cfg.get("clip") or {}).get("vertical_mode", "blur"))
    # Лип-гейт ВЫКЛ по умолчанию (см. pipeline.py — прод 2026-11-21).
    if bool((cfg.get("subtitle") or {}).get("lip_sync", False)):
        try:
            if cam_box and vmode == "split" and cur == vertical_mp4:
                strip_h = vertical_mod.split_cam_h()
                lip_iv = lipsync_mod.speaking_intervals(
                    cur, str(workdir), cam_box=(0, 0, 1080, strip_h))
            elif cam_box or str(clip_cfg.get("vertical_fallback", "crop")) == "blur":
                lip_iv = lipsync_mod.speaking_intervals(cur, str(workdir))
        except Exception:
            log.exception("Лип-синк не удался — субтитры без гейта по губам")
            lip_iv = None
    try:
        ass, smeta = subtitle.make_subtitles_meta(
            cur, str(workdir), cfg.get("subtitle") or {},
            str(cfg.get("speech_language", "ru")), [],
            duration=dur, skip_intervals=intervals, lip_intervals=lip_iv)
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

    # --- говор СТРИМЕРА (лип-гейт): сколько клипа он реально говорил
    # Жалоба «скучные моменты»: окно может совпасть с паузой стримера
    # (хайп чата без речи). lip_speech_pct — доля клипа, где стример
    # реально говорит (движение рта, lipsync.py). Считается только когда
    # лип-гейт применился — иначе не наказываем (не знаем).
    lip_pct = int((smeta or {}).get("lip_speech_pct") or 0)
    if lip_pct and lip_pct < 18:
        d_lip = -12.0               # стример молчал почти весь клип
    elif lip_pct and lip_pct < 35:
        d_lip = -5.0
    else:
        d_lip = 0.0

    b = 50.0 if base is None else max(0.0, min(100.0, float(base)))
    score = max(0.0, min(100.0, b + d_bit + d_snd + d_sub + d_lip))

    # --- ЖЁСТКИЙ ФЛОР: практика показала, что скучные клипы — это почти
    # всегда либо статичная картинка (~0.3 Мбит/с, файл ~2 МБ), либо обрезок.
    # Такой клип не публикуется независимо от хайп-оценки окна.
    # ВАЖНО: порог 25 с, а НЕ 45 с — переменная длина клипов (auto: цель
    # 35-45 с) сделала клипы 35-44 с НОРМОЙ, и старый флор «короче 45 с»
    # браковал большинство хороших нарезок со скором 18/100 «СКУКА»
    # (прод-инцидент 2026-11-20). Бракуем только реально обрезанные (<25 с).
    hard = ""
    if dur and dur < 25.0:
        score = min(score, 18.0)
        hard = "короче 25 с"
    elif mbps < 0.5:
        score = min(score, 22.0)
        hard = "статичный кадр (%.2f Мбит/с, ~2 МБ-класс)" % mbps

    parts = {"score": round(score, 1), "base": round(b, 1),
             "mbps": round(mbps, 2), "mean_db": round(mean_db, 1),
             "std_db": round(std_db, 1), "silent": round(silent_r, 2),
             "words": words, "d_bit": d_bit, "d_snd": round(d_snd, 1),
             "d_sub": d_sub, "d_lip": d_lip, "lip_pct": lip_pct,
             "hard": hard}
    if hard:
        log.info("Качество клипа: %.0f/100 — ЖЁСТКИЙ ФЛОР (%s): бракуем", score, hard)
        return score, parts
    log.info("Качество клипа: %.0f/100 (база %.0f, битрейт %.2f Мбит/с %+.0f, "
             "звук %.0f dB %+.0f, слов %d %+.0f)", score, b, mbps, d_bit,
             mean_db, d_snd, words, d_sub)
    return score, parts
