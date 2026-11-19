"""Рекламный баннер внутри каждой нарезки (файл banner.mp4 рядом с программой).

Правила рекламодателя, которые модуль соблюдает автоматически:
  * НАРЕЗКА НА ПАУЗЕ, пока идёт баннер: кадр стрима замирает, звук стрима
    молчит; после окончания ролика баннера эфир продолжается с того же места;
  * баннер показывается СО ЗВУКОМ (звук самого ролика баннера);
  * занимает НЕ МЕНЬШЕ 25% площади экрана (по умолчанию ~30%, с запасом);
  * НЕ В УГЛУ — баннер ставится строго по центру кадра;
  * ПО СЕРЕДИНЕ клипа (start: middle): начало («крючок») и финал момента
    чистые, зритель к моменту показа уже вовлёкся;
  * видео длиннее минуты — баннер показывается РАЗ В МИНУТУ.

Хотите другой баннер — просто замените файл banner.mp4, перезапускать
программу не обязательно (файл читается при сборке каждого клипа).
"""

import json
import logging
import math
import os

from utils import ffprobe_meta, run_cmd

log = logging.getLogger("banner")

AREA_RULE = 0.25    # требование правил: не меньше 25% площади экрана
PERIOD = 60.0       # длинные клипы: показ раз в минуту

MIDDLE_WORDS = ("", "middle", "mid", "center", "auto", "середина")


def _fnum(x):
    """Число для выражений ffmpeg: точка-разделитель, без хвостовых нулей."""
    s = f"{float(x):.3f}".rstrip("0").rstrip(".")
    return s if s else "0"


def start_time(start, clip_dur, banner_dur):
    """Секунда ПЕРВОГО показа баннера.

    "middle" (по умолчанию и в config.yaml) — баннер ровно ПО СЕРЕДИНЕ
    клипа: показ начинается на (длительность - длина баннера) / 2. Так
    начало клипа («крючок» момента) и финал остаются чистыми, а зритель
    уже вовлёкся к моменту показа. Число в конфиге — показ на заданной
    секунде (как раньше). Некорректное значение — середина.
    """
    mid = max(0.0, (max(float(clip_dur or 0), 1.0) - max(float(banner_dur or 0), 0.5)) / 2.0)
    if start is None:
        return mid
    if isinstance(start, str):
        s = start.strip().lower()
        if s in MIDDLE_WORDS:
            return mid
        try:
            start = float(s)
        except ValueError:
            return mid
    try:
        return max(0.0, float(start))
    except (TypeError, ValueError):
        return mid


def resolve_file(cfg, base_dir):
    """Путь к ролику баннера или None (выключен в конфиге / файла нет)."""
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return None
    name = str(cfg.get("file") or "banner.mp4")
    path = name if os.path.isabs(name) else os.path.join(str(base_dir), name)
    return path if os.path.isfile(path) else None


def has_audio(path):
    """Есть ли у файла звуковая дорожка (правила требуют звук у баннера)."""
    rc, out, _err = run_cmd(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "json", str(path)],
        timeout=30)
    if rc != 0:
        return False
    try:
        return bool(json.loads(out).get("streams"))
    except ValueError:
        return False


def banner_geometry(clip_w, clip_h, ban_w, ban_h, area=0.30):
    """Размер баннера под нужную долю площади экрана (не шире кадра).

    Возвращает (bw, bh, area_real). area можно задавать и долей (0.30),
    и процентами (30) — в config.yaml указывают проценты. По правилам
    area_real >= 25% — модуль берёт 30% с запасом и предупреждает в логе,
    если рекламодатель запросил площадь меньше допустимой.
    """
    area = float(area or 0.30)
    if area > 1.0:                       # проценты -> доля
        area /= 100.0
    area = max(area, AREA_RULE)
    ar = float(ban_w) / max(float(ban_h), 1.0)
    # (ar * h * h) = area * W * H  ->  высота баннера из целевой площади
    bh = math.sqrt(area * float(clip_w) * float(clip_h) / ar)
    bw = ar * bh
    if bw > clip_w:                      # баннер не должен быть шире кадра
        bw, bh = float(clip_w), float(clip_w) / ar
    bw, bh = int(round(bw)), int(round(bh))
    real = (bw * bh) / (float(clip_w) * float(clip_h))
    return bw, bh, real


def plan_intervals(clip_dur, banner_dur, start="middle", period=PERIOD):
    """Интервалы показа [(начало, конец)].

    По умолчанию первый показ — ПО СЕРЕДИНЕ клипа (start="middle"):
    «крючок» в начале и финал момента остаются чистыми. Число — показ
    на заданной секунде. Если клип короткий и показ не влезает целиком,
    он сдвигается так, чтобы ролик баннера проигрался ЦЕЛИКОМ.
    """
    clip_dur = max(float(clip_dur or 0), 1.0)
    banner_dur = max(float(banner_dur or 0), 0.5)
    start = start_time(start, clip_dur, banner_dur)
    if start + banner_dur > clip_dur:
        start = max(0.0, clip_dur - banner_dur - 0.5)
    out, t = [], start
    while t < clip_dur - 0.5:
        out.append((t, min(t + banner_dur, clip_dur)))
        t += period
    if not out:                          # совсем короткий клип
        out.append((max(0.0, clip_dur - banner_dur), clip_dur))
    return out


def extract_still(raw_ts, t, out_png, workdir):
    """Кадр-«пауза»: стоп-кадр эфира перед началом показа баннера."""
    ts = max(float(t) - 0.4, 0.0)
    rc, _out, err = run_cmd(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
         "-ss", _fnum(ts), "-i", os.path.basename(str(raw_ts)),
         "-frames:v", "1", "-q:v", "2", os.path.basename(str(out_png))],
        timeout=120, cwd=str(workdir))
    if rc != 0:
        raise RuntimeError("ffmpeg (стоп-кадр): " + err[-300:])
    return out_png


def compose(raw_ts, banner_path, ass_path, video_out, workdir,
            vcodec_args=None, cfg=None, intervals=None):
    """Финальный mp4 одним проходом ffmpeg: нарезка на паузе + баннер + субтитры.

    Пока проигрывается ролик баннера, эфир «стоит на стопе»: кадр стрима
    заморожен (стоп-кадр), звук стрима молчит — слышен только звук баннера.
    Как только ролик баннера закончился, эфир продолжается дальше. Клип при
    этом НЕ укорачивается: стоп-кадр занимает ровно столько же времени,
    сколько шёл баннер, поэтому тайминги субтитров остаются верными.

    Субтитры вшиваются тем же проходом, если передан ass_path. Возвращает
    путь к готовому файлу.
    """
    cfg = cfg or {}
    meta = ffprobe_meta(raw_ts)
    w = int(meta.get("width") or 1280)
    h = int(meta.get("height") or 720)
    dur = float(meta.get("duration") or 0)
    if dur <= 0:
        raise RuntimeError("не удалось определить длительность клипа")

    bmeta = ffprobe_meta(banner_path)
    bw0 = int(bmeta.get("width") or 0)
    bh0 = int(bmeta.get("height") or 0)
    bdur = float(bmeta.get("duration") or 0)
    if bw0 <= 0 or bh0 <= 0 or bdur <= 0:
        raise RuntimeError("не удалось прочитать баннер (banner.mp4 повреждён?)")

    if not intervals:
        intervals = plan_intervals(dur, bdur, start=cfg.get("start", "middle"))
    bw, bh, real = banner_geometry(w, h, bw0, bh0, area=cfg.get("area", 0.30))
    log.info("Баннер: %dx%d (%.1f%% экрана, правило >=25%%), показы: %s",
             bw, bh, real * 100,
             ", ".join(f"{s:.1f}-{e:.1f}с" for s, e in intervals))
    if real + 1e-9 < AREA_RULE:
        log.warning("Площадь баннера %.1f%% меньше 25%% — нарушение правил "
                    "рекламодателя, увеличьте banner.area", real * 100)
    if not has_audio(banner_path):
        log.warning("У banner.mp4 НЕТ звуковой дорожки — правила рекламодателя "
                    "требуют баннер со звуком!")

    n = len(intervals)

    # --- стоп-кадры: по одному на каждый показ --------------------------------
    stills = []
    for k, (s, _e) in enumerate(intervals):
        still = os.path.join(str(workdir), f"still_{k}.png")
        try:
            extract_still(raw_ts, s, still, workdir)
            stills.append(still)
        except Exception as e:
            raise RuntimeError(f"не удалось взять стоп-кадр для показа {k}: {e}")

    # --- границы кусков эфира между показами баннера ---------------------------
    seg_bounds = []
    prev = 0.0
    for s, e in intervals:
        seg_bounds.append((prev, s))          # кусок ЭФИРА (звук и картинка живые)
        prev = e
    seg_bounds.append((prev, dur))            # последний кусок — до конца клипа

    vol = max(0.0, float(cfg.get("volume", 1.0)))
    in_banner = has_audio(banner_path)
    in_clip = has_audio(raw_ts)
    fmt = "aformat=sample_rates=44100:channel_layouts=stereo"

    parts = []

    # --- видео: куски эфира + «паузы» (стоп-кадр с баннером поверх) -----------
    nseg = len(seg_bounds)                       # всегда n + 1 кусков эфира
    parts.append("[0:v]split=%d%s" % (nseg, "".join(f"[sv{i}]" for i in range(nseg))))
    for k, (a, b) in enumerate(seg_bounds[:-1]):
        parts.append(f"[sv{k}]trim=start={_fnum(a)}:end={_fnum(b)},"
                     "setpts=PTS-STARTPTS[c%d]" % k)
    parts.append(f"[sv{nseg - 1}]trim=start={_fnum(seg_bounds[-1][0])},"
                 "setpts=PTS-STARTPTS[c%d]" % (nseg - 1))
    for k in range(n):
        parts.append(f"[{2 * k + 1}:v]setsar=1[st{k}]")
        parts.append(f"[{2 * k + 2}:v]scale={bw}:{bh}:flags=bicubic,setsar=1[b{k}]")
        parts.append(f"[st{k}][b{k}]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1[bv{k}]")
    vid = ""
    for k in range(n):
        vid += f"[c{k}][bv{k}]"
    vid += f"[c{n}]"
    parts.append(vid + f"concat=n={2 * n + 1}:v=1:a=0[cv]")
    vsrc = "cv"
    if ass_path:
        parts.append(f"[cv]ass={os.path.basename(str(ass_path))}[vout]")
        vsrc = "vout"

    # --- звук: куски эфира целиком, на паузе — только звук баннера ------------
    if in_clip:
        parts.append("[0:a]asplit=%d%s" % (nseg, "".join(f"[sa{i}]" for i in range(nseg))))
        for k, (a, b) in enumerate(seg_bounds[:-1]):
            parts.append(f"[sa{k}]{fmt},atrim=start={_fnum(a)}:end={_fnum(b)},"
                         "asetpts=PTS-STARTPTS[ca%d]" % k)
        parts.append(f"[sa{nseg - 1}]{fmt},"
                     f"atrim=start={_fnum(seg_bounds[-1][0])},"
                     "asetpts=PTS-STARTPTS[ca%d]" % (nseg - 1))
    else:
        for k, (a, b) in enumerate(seg_bounds[:-1]):
            parts.append(f"anullsrc=r=44100:cl=stereo,"
                         f"atrim=end={_fnum(b - a)},asetpts=PTS-STARTPTS[ca{k}]")
        parts.append(f"anullsrc=r=44100:cl=stereo,"
                     f"atrim=end={_fnum(dur - seg_bounds[-1][0])},"
                     "asetpts=PTS-STARTPTS[ca%d]" % (nseg - 1))
    for k, (s, e) in enumerate(intervals):
        d = _fnum(e - s)
        if in_banner:
            parts.append(f"[{2 * k + 2}:a]{fmt},volume={_fnum(vol)},apad,"
                         f"atrim=end={d},asetpts=PTS-STARTPTS[ab{k}]")
        else:
            parts.append(f"anullsrc=r=44100:cl=stereo,atrim=end={d},"
                         f"asetpts=PTS-STARTPTS[ab{k}]")
    aud = ""
    for k in range(n):
        aud += f"[ca{k}][ab{k}]"
    aud += f"[ca{n}]"
    parts.append(aud + f"concat=n={2 * n + 1}:v=0:a=1[aout]")

    graph = ";".join(parts)
    va = list(vcodec_args) if vcodec_args else ["-c:v", "libx264", "-preset",
                                                "veryfast", "-crf", "18"]
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
            "-i", os.path.basename(str(raw_ts))]
           + [x for k in range(n) for x in
              ("-loop", "1", "-t", _fnum(intervals[k][1] - intervals[k][0]),
               "-i", os.path.basename(stills[k]))]
           + ["-i", os.path.abspath(str(banner_path))] * n
           + ["-filter_complex", graph,
              "-map", f"[{vsrc}]", "-map", "[aout]"]
           + va
           + ["-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
              "-movflags", "+faststart", os.path.basename(str(video_out))])
    rc, _out, err = run_cmd(cmd, timeout=900, cwd=str(workdir))
    if rc != 0:
        raise RuntimeError("ffmpeg (баннер): " + err[-500:])
    for f in stills:
        try:
            os.remove(f)
        except OSError:
            pass
    return video_out
