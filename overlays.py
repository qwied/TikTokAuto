"""Замазка ЧУЖИХ банеров, виджетов и рекламы в клипе (кроме нашего RubUp).

На стримах топ-каналов поверх картинки висят ОВЕРЛЕИ самого стримера:
виджеты донатов, «мои соцсети», спонсорские плашки, логотипы, таблицы
сабов. В TikTok это чужая реклама и шум — зритель должен видеть игру и
субтитры, а брендинг остаётся только НАШ: баннер RubUp накладывается
ПОСЛЕ замазки, поэтому он никогда не размывается.

Как это работает (полностью локально, без нейросетей — быстро и стабильно):
  1) из клипа берётся ~14 кадров через равные интервалы (320px, grayscale);
  2) numpy считает попиксельную «подвижность» (std по времени): баннер/виджет
     стоит на месте всю минуту, а игра — движется;
  3) статичные пятна у верхней и нижней кромки собираются в прямоугольники
     (чистка маски + BFS по связным компонентам); центр кадра и мелочь не
     трогаются — там геймплей и прицел;
  4) прямоугольники переводятся в координаты вертикального кадра 9:16 и
     замазываются boxblur-ом ПРЯМО в цепочке вертикализации — лишнего
     перекодирования нет.

Защита от ложных срабатываний:
  * если «статично» больше 42% кадра (стоп-кадр/меню/пауза) — замазки нет;
  * пятна меньше 0.6% или больше 22% площади не считаются баннером;
  * максимум 6 зон — крупнее всех.
"""

import logging
import os
from collections import deque

try:
    from utils import ffprobe_meta, run_cmd
except ImportError:                                    # плоский репозиторий
    from utils import ffprobe_meta, run_cmd

log = logging.getLogger("overlays")

ANALYSIS_W = 320          # ширина кадра для анализа (быстро и достаточно)
SAMPLES = 14              # сколько кадров смотрим
STD_THRESHOLD = 6.0       # std ниже этого = пиксель «стоит на месте» (0..255)
MAX_STATIC_FRACTION = 0.90
MIN_BOX_FRACTION = 0.004
MAX_BOX_FRACTION = 0.22
MIN_FILL = 0.24
MIN_TEXTURE = 3.0         # средний градиент в зоне: у банера всегда есть текст/рамка
TOP_BAND = 0.38           # верхние 38% кадра — зона виджетов/соцсетей
BOTTOM_BAND = 0.30        # нижние 30% — зона чата/плашек
MARGIN_PX = 10            # расширение зоны (px анализа ~60px исходника: задевает панель виджета)
MERGE_GAP = 22            # слияние зон ближе 22 px (~130px исходника: хвосты текста)
MAX_BOXES = 6

# --- Слой 2: КОНСТАНТНЫЕ УГЛЫ ------------------------------------------------
# Полупрозрачные/анимированные виджеты донатов («51%», бегущие строки) просвечивают
# фон и потому невидимы для статичного детекта. Но такие виджеты почти всегда
# висят в ВЕРХНИХ УГЛАХ кадра — замазываем оба верхних угла всегда, без детекта.
CORNER_W = 0.30           # ширина угла (доля ширины кадра)
CORNER_H = 0.17           # высота угла (доля высоты кадра)


def corner_boxes(w, h):
    """[(x, y, w, h)] — верхний левый и верхний правый углы исходника."""
    cw = int(float(w) * CORNER_W)
    ch = int(float(h) * CORNER_H)
    if cw < 40 or ch < 40:
        return []
    return [(0, 0, cw, ch), (int(w) - cw, 0, cw, ch)]


def _extract_frames(video_path, workdir):
    """~SAMPLES кадров через равные интервалы -> numpy (N, H, W) uint8."""
    import numpy as np
    meta = ffprobe_meta(video_path)
    w_src = int(meta.get("width") or 0)
    h_src = int(meta.get("height") or 0)
    dur = float(meta.get("duration") or 0)
    if w_src <= 0 or h_src <= 0 or dur <= 0:
        return None, (0, 0)
    h_an = max(2, int(round(ANALYSIS_W * h_src / float(w_src) / 2.0)) * 2)
    fps = min(1.0, SAMPLES / max(dur, 1.0))
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", os.path.basename(str(video_path)),
            "-vf", "fps=%.6f,scale=%d:%d" % (fps, ANALYSIS_W, h_an),
            "-frames:v", str(SAMPLES + 4),
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    rc, out, _err = run_cmd(cmd, timeout=180, cwd=str(workdir), binary=True)
    if rc != 0 or not out:
        return None, (0, 0)
    frame = ANALYSIS_W * h_an
    n = len(out) // frame
    if n < 4:
        return None, (0, 0)
    arr = np.frombuffer(out[:n * frame], dtype=np.uint8).reshape(n, h_an, ANALYSIS_W)
    return arr.astype(np.float32), (w_src, h_src)


def _clean_mask(mask):
    """Морфологическая чистка: эрозия 1 шаг + дилатация 2 шага (3x3)."""
    m = mask
    for _ in range(1):                       # эрозия: убирает шум-пыль
        p = _shift_or(m, 1, 1) & _shift_or(m, 1, -1) & \
            _shift_or(m, -1, 1) & _shift_or(m, -1, -1)
        m = m & p
    for _ in range(2):                       # дилатация: склеивает рваные края
        q = m.copy()
        q[1:, :] |= m[:-1, :]
        q[:-1, :] |= m[1:, :]
        q[:, 1:] |= m[:, :-1]
        q[:, :-1] |= m[:, 1:]
        m = q
    return m


def _shift_or(m, dy, dx):
    out = m.copy()
    h, w = m.shape
    ys, yd = (slice(0, h - dy), slice(dy, h)) if dy > 0 else \
             (slice(-dy, h), slice(0, h + dy)) if dy < 0 else (slice(0, h), slice(0, h))
    xs, xd = (slice(0, w - dx), slice(dx, w)) if dx > 0 else \
             (slice(-dx, w), slice(0, w + dx)) if dx < 0 else (slice(0, w), slice(0, w))
    out[ys, xs] |= m[yd, xd]
    return out


def _components(mask):
    """BFS по связным компонентам -> [(x0, y0, x1, y1, area)]."""
    h, w = mask.shape
    seen = mask.copy()
    out = []
    ys, xs = None, None
    idx = seen.argmax() if seen.any() else -1
    while idx >= 0:
        y0, x0 = divmod(int(idx), w)
        q = deque([(y0, x0)])
        seen[y0, x0] = False
        minx = maxx = x0
        miny = maxy = y0
        area = 0
        while q:
            cy, cx = q.popleft()
            area += 1
            if cx < minx:
                minx = cx
            if cx > maxx:
                maxx = cx
            if cy < miny:
                miny = cy
            if cy > maxy:
                maxy = cy
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = cy + dy, cx + dx
                if 0 <= ny < h and 0 <= nx < w and seen[ny, nx]:
                    seen[ny, nx] = False
                    q.append((ny, nx))
        out.append((minx, miny, maxx + 1, maxy + 1, area))
        if not seen.any():
            break
        idx = seen.argmax()
    return out


def _merge_boxes(boxes):
    """Слияние прямоугольников с зазором <= MERGE_GAP."""
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                gap_x = max(a[0] - b[2], b[0] - a[2], 0)
                gap_y = max(a[1] - b[3], b[1] - a[3], 0)
                if gap_x <= MERGE_GAP and gap_y <= MERGE_GAP:
                    boxes[i] = (min(a[0], b[0]), min(a[1], b[1]),
                                max(a[2], b[2]), max(a[3], b[3]))
                    boxes.pop(j)
                    changed = True
                    break
            if changed:
                break
    return boxes


def _texture(img, box):
    """Средняя «резкость» зоны в усреднённом кадре: у банера всегда
    есть текст, рамка или логотип (градиент высокий); у просто статичной
    травы/неба/градиента — низкий. Отсекает замазывание живых сцен."""
    import numpy as np
    x0, y0, x1, y1 = box
    reg = img[max(0, y0):max(1, y1), max(0, x0):max(1, x1)].astype(np.float32)
    if reg.size == 0:
        return 0.0
    g = 0.0
    if reg.shape[1] > 1:
        g += float(np.abs(np.diff(reg, axis=1)).mean())
    if reg.shape[0] > 1:
        g += float(np.abs(np.diff(reg, axis=0)).mean())
    return g / 2.0


def detect_overlay_boxes(video_path, workdir=".", exclude_boxes=()):
    """[(x, y, w, h)] в координатах ИСХОДНОГО видео — зоны чужих оверлеев.

    exclude_boxes — зоны-исключения [(x, y, w, h)] (окно камеры стримера):
    вебкамера СТАТИЧНА, поэтому детектор «неподвижности» видит в ней чужой
    баннер и замазывал стримера (прод 2026-11-20: dmitry_lixxx — маска
    накрыла нижнюю половину лица). Зоны-исключения передаёт pipeline.
    """
    import numpy as np
    frames, (w_src, h_src) = _extract_frames(video_path, workdir)
    if frames is None:
        return []
    n, h, w = frames.shape
    std = frames.std(axis=0)
    static = std < STD_THRESHOLD

    frac = float(static.mean())
    if frac > MAX_STATIC_FRACTION:
        log.info("Замазка: %.0f%% кадра статично (заморозка?) — пропускаю", frac * 100)
        return []

    band = np.zeros_like(static)
    band[:int(h * TOP_BAND), :] = True
    band[int(h * (1.0 - BOTTOM_BAND)):, :] = True
    static = static & band
    if not static.any():
        return []

    static = _clean_mask(static)
    mean_img = frames.mean(axis=0)
    comp = _components(static)
    full = float(w * h)
    boxes = []
    for (x0, y0, x1, y1, area) in comp:
        bw, bh = x1 - x0, y1 - y0
        fa = bw * bh / full
        fill = area / float(bw * bh)
        if fa < MIN_BOX_FRACTION or fa > MAX_BOX_FRACTION or fill < MIN_FILL:
            continue
        if bh > h * 0.34:
            continue                     # полоса выше трети кадра — не виджет
        if _texture(mean_img, (x0, y0, x1, y1)) < MIN_TEXTURE:
            continue                     # статично, но гладко (небо/трава) — не банер
        box_src = (x0, y0, x1 - x0, y1 - y0)
        if _inside_excluded(box_src, exclude_boxes, w_src, h_src, w, h):
            log.info("Замазка: зона %s — это окно камеры стримера, не трогаю",
                     box_src)
            continue
        boxes.append((x0, y0, x1, y1))
    if not boxes:
        return []
    boxes = _merge_boxes(boxes)
    boxes.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    boxes = boxes[:MAX_BOXES]

    sx = w_src / float(w)
    sy = h_src / float(h)
    out = []
    for (x0, y0, x1, y1) in boxes:
        px0 = max(0, int((x0 - MARGIN_PX) * sx))
        py0 = max(0, int((y0 - MARGIN_PX) * sy))
        px1 = min(w_src, int((x1 + MARGIN_PX) * sx))
        py1 = min(h_src, int((y1 + MARGIN_PX) * sy))
        if px1 - px0 >= 24 and py1 - py0 >= 24:
            out.append((px0, py0, px1 - px0, py1 - py0))
    if out:
        log.info("Замазка: найдено зон %d: %s", len(out),
                 "; ".join("%dx%d@%d,%d" % b for b in out))
    return out


def _inside_excluded(box, exclude_boxes, w_src, h_src, w_an, h_an):
    """True, если зона (в координатах анализа) по большей части внутри
    одной из зон-исключений (в координатах ИСХОДНИКА) — это камера."""
    sx = w_src / float(w_an or 1)
    sy = h_src / float(h_an or 1)
    x, y, bw, bh = [float(v) for v in box]
    for ex in exclude_boxes or ():
        try:
            exx, exy, exw, exh = [float(v) for v in ex]
        except (TypeError, ValueError):
            continue
        # пересечение площади зоны с исключением, в долях площади зоны
        ix = max(0.0, min(x + bw, (exx + exw) / sx) - max(x, exx / sx))
        iy = max(0.0, min(y + bh, (exy + exh) / sy) - max(y, exy / sy))
        if bw * bh > 0 and (ix * iy) / (bw * bh) > 0.10:
            return True
    return False


def map_boxes(boxes, src_w, src_h, mode="blur", out_w=1080, out_h=1920):
    """Координаты ИСХОДНИКА -> координаты вертикального кадра (blur/crop)."""
    out = []
    mode = str(mode or "blur").lower()
    for (x, y, w, h) in boxes:
        if mode == "crop":
            cw = src_h * out_w / float(out_h)
            cx = (src_w - cw) / 2.0
            f = out_h / float(src_h)
            nx = (x - cx) * f
            ny = y * f
            nw = w * f
            nh = h * f
        else:
            f = out_w / float(src_w)
            fg_h = src_h * f
            y0 = (out_h - fg_h) / 2.0
            nx = x * f
            ny = y0 + y * f
            nw = w * f
            nh = h * f
        x0 = max(0, int(nx))
        y0 = max(0, int(ny))
        x1 = min(out_w, int(nx + nw))
        y1 = min(out_h, int(ny + nh))
        if x1 - x0 >= 40 and y1 - y0 >= 40:
            out.append((x0, y0, x1 - x0, y1 - y0))
    return out


def map_boxes_split(boxes, src_w, src_h, cam_win, game_win, cam_h,
                    out_w=1080, out_h=1920):
    """Координаты ИСХОДНИКА -> координаты вертикального кадра для макета
    split (камера сверху, игра снизу).

    cam_win/game_win — окна секций в исходнике (vertical.split_windows),
    cam_h — высота секции камеры в ГОТОВОМ кадре. Зона чужого виджета
    относится к той секции, где её центр; внутри секции повторяется та же
    трансформация, что и в ffmpeg (scale=increase + центральный кроп).
    """
    out = []
    cx, cy, cw, ch = [float(v) for v in cam_win]
    gx, gy, gw, gh = [float(v) for v in game_win]
    game_h = out_h - cam_h

    def _clamp(nx, ny, nw, nh, y_top, y_bot):
        x0 = max(0, int(nx))
        y0 = max(y_top, int(ny))
        x1 = min(out_w, int(nx + nw))
        y1 = min(y_bot, int(ny + nh))
        if x1 - x0 >= 40 and y1 - y0 >= 40:
            out.append((x0, y0, x1 - x0, y1 - y0))

    for (x, y, w, h) in boxes:
        bcx, bcy = x + w / 2.0, y + h / 2.0
        if (cx <= bcx <= cx + cw and cy <= bcy <= cy + ch):
            # секция камеры: scale=increase -> центральный кроп
            s = max(out_w / cw, cam_h / ch)
            nx = (x - cx) * s - (cw * s - out_w) / 2.0
            ny = (y - cy) * s - (ch * s - cam_h) / 2.0
            _clamp(nx, ny, w * s, h * s, 0, cam_h)
        elif (gx <= bcx <= gx + gw and gy <= bcy <= gy + gh):
            # секция игры: та же математика, сдвиг вниз на высоту камеры
            s = max(out_w / gw, game_h / gh)
            nx = (x - gx) * s - (gw * s - out_w) / 2.0
            ny = cam_h + (y - gy) * s - (gh * s - game_h) / 2.0
            _clamp(nx, ny, w * s, h * s, cam_h, out_h)
        # зоны вне обоих окон (замазка не видна в макете) пропускаем
    return out


def blur_fragment(boxes, in_label="v"):
    """ffmpeg-фрагмент: boxblur на каждом прямоугольнике поверх кадра.

    Возвращает (строка_фильтра, выходная_метка). Пустые боксы -> ("", метка).
    """
    n = len(boxes)
    if not n:
        return "", in_label
    parts = ["[%s]split=%d%s[bsend]" % (in_label, n + 1,
                                        "".join("[bs%d]" % i for i in range(n)))]
    last = "bsend"
    for i, (x, y, w, h) in enumerate(boxes):
        outlbl = "bm%d" % i if i < n - 1 else "vm"
        parts.append("[bs%d]crop=%d:%d:%d:%d,"
                     "boxblur=luma_radius=16:luma_power=2:"
                     "chroma_radius=8:chroma_power=2[bb%d]" % (i, w, h, x, y, i))
        parts.append("[%s][bb%d]overlay=%d:%d[%s]" % (last, i, x, y, outlbl))
        last = outlbl
    return ";".join(parts), last
