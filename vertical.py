"""Вертикальный формат 9:16 (1080x1920) — как в TikTok.

Клип по умолчанию готовится ВЕРТИКАЛЬНЫМ. Субтитры ложатся в нижнюю зону,
баннер RubUP — по центру кадра.

Режимы (config.yaml -> clip.vertical_mode):
  split — МАКЕТ «СТРИМЕР СВЕРХУ, ИГРА СНИЗУ» (по умолчанию): вебкамера
          стримера занимает верхнюю часть экрана (auto-детект по лицу,
          clipper/webcam.py), игра — нижнюю; обе части на всю ширину, весь
          экран TikTok занят без размытых полос. Если камеру найти не
          удалось — клип собирается как blur (ничего не ломается);
  blur  — стрим по центру во всю ширину, фон — размытое видео;
  crop  — без фона: центральная часть кадра растягивается на весь 9:16.

Попытка импорта сделана так, чтобы модуль работал и в пакете clipper,
и в плоском репозитории (файлы в корне) для GitHub-режима.
"""

import os

try:
    from utils import ffprobe_meta, run_cmd
except ImportError:                                    # плоский репозиторий
    from utils import ffprobe_meta, run_cmd

log = __import__("logging").getLogger("vertical")

WIDTH = 1080
HEIGHT = 1920
SPLIT_CAM_FRAC = 0.32   # доля высоты экрана под камеру стримера (верх)


def _overlap(a, b):
    """Пересекаются ли прямоугольники (x, y, w, h)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or
                ay + ah <= by or by + bh <= ay)


def split_windows(src_w, src_h, cam_box, cam_frac=SPLIT_CAM_FRAC,
                  w=WIDTH, h=HEIGHT):
    """Геометрия макета split.

    Возвращает (cam_win, game_win, cam_h): окна в координатах ИСХОДНИКА.
    Окно игры выбирается так, чтобы НЕ задевать камеру: сдвиг в сторону
    (камера справа -> окно слева), если не выходит — окно НАД камерой,
    в крайнем случае — центр (без гарантии, зато кадр всегда собирается).
    Значения нужны и для ffmpeg-графа, и для маппинга замазки
    (overlays.map_boxes_split) — единая точка правды по геометрии.
    """
    cam_h = int(round(h * float(cam_frac)))
    game_h = h - cam_h
    ar = w / float(game_h)                    # соотношение окна игры
    cx, cy, cw, ch = [float(v) for v in cam_box]
    m = max(8.0, src_w * 0.015)               # отступ окна игры от камеры

    # Вариант 1: окно на ВСЮ высоту, сдвинутое в сторону от камеры.
    # Предпочтение — ЦЕНТРУ (не режет игровой интерфейс по краям), затем
    # стороны, куда камера не попадает.
    gw = int(round(src_h * ar))
    if gw <= src_w:
        cands = [((src_w - gw) // 2, "центр"),
                 (0, "слева"), (src_w - gw, "справа")]
        for gx, side in cands:
            win = (gx, 0, gw, src_h)
            if not _overlap(win, cam_box):
                return (cx, cy, cw, ch), win, cam_h

    # Вариант 2: окно НАД камерой (типично камера снизу) во всю ширину
    gh2 = int(round(cy - m))
    if gh2 >= src_h * 0.45:
        return (cx, cy, cw, ch), (0, 0, src_w, gh2), cam_h

    # Вариант 3: уступка — центр на всю высоту (возможно задевание камеры)
    gw = min(int(src_w), max(1, int(round(src_h * ar))))
    return (cx, cy, cw, ch), (max(0, (src_w - gw) // 2), 0, gw, src_h), cam_h


def split_graph(src_w, src_h, cam_box, cam_frac=SPLIT_CAM_FRAC,
                w=WIDTH, h=HEIGHT):
    """ffmpeg-граф макета «камера сверху, игра снизу».

    Возвращает (строка_фильтра, метка_выхода, cam_h). Обе секции заполняют
    свою часть экрана ЦЕЛИКОМ (scale=increase + crop): никаких полос и
    размытых фонов. Между секциями — тонкий разделитель (drawbox).
    Замазка чужих оверлеев накладывается ПОВЕРХ (blur_fragment), баннер
    RubUP рисуется ещё позже — его замазка не трогает.
    """
    cam_win, game_win, cam_h = split_windows(src_w, src_h, cam_box,
                                             cam_frac=cam_frac, w=w, h=h)
    cx, cy, cw, ch = [int(round(v)) for v in cam_win]
    gx, gy, gw, gh = [int(round(v)) for v in game_win]
    game_h = h - cam_h
    cw = min(cw, src_w - cx)
    ch = min(ch, src_h - cy)
    gw = min(gw, src_w - gx)
    gh = min(gh, src_h - gy)
    graph = (
        "[0:v]split=2[csrc][gsrc];"
        # --- верх: вебкамера стримера ---
        f"[csrc]crop={cw}:{ch}:{cx}:{cy},"
        f"scale={w}:{cam_h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{cam_h},setsar=1[cam];"
        # --- низ: игра ---
        f"[gsrc]crop={gw}:{gh}:{gx}:{gy},"
        f"scale={w}:{game_h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{game_h},setsar=1[game];"
        f"[cam][game]vstack=inputs=2[vs];"
        # тонкий разделитель между секциями
        f"[vs]drawbox=x=0:y={max(0, cam_h - 3)}:w={w}:h=3:color=black@0.6:t=fill[vsep]"
    )
    return graph, "vsep", cam_h


def blur_graph(w=WIDTH, h=HEIGHT, blur=26):
    """Фон — размытый кадр на весь 9:16, стрим по центру во всю ширину."""
    return ("[0:v]split=2[vbg][vfg];"
            f"[vbg]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=luma_radius={blur}:luma_power=2,setsar=1[bg];"
            f"[vfg]scale={w}:-2,setsar=1[fgs];"
            "[bg][fgs]overlay=x=(W-w)/2:y=(H-h)/2[v]")


def crop_graph(w=WIDTH, h=HEIGHT):
    """Без фона: центральная часть 9:16 растягивается на весь экран."""
    return (f"[0:v]crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})',"
            f"scale={w}:{h},setsar=1[v]")


def make_vertical(video_in, video_out, workdir, width=WIDTH, height=HEIGHT,
                  mode="blur", blur=26, vcodec_args=None, mask_boxes=None,
                  raw_boxes=None, cam_frac=SPLIT_CAM_FRAC):
    """Перевести клип в вертикальный формат 9:16.

    Принимает собранный сырой клип (ts/mp4), возвращает путь к вертикальному
    mp4. Длительность и звук сохраняются; звук перекодируется в AAC
    (совместимо с TikTok). Выход: h264 + yuv420p + faststart.

    Режим split: авто-детект вебкамеры (webcam.py) -> камера сверху,
    игра снизу. Камеру не нашли / cv2 нет -> клип собирается как blur.

    Замазка чужих банеров (кроме RubUp, который рисуется ПОСЛЕ):
      raw_boxes — прямоугольники [(x, y, w, h)] в координатах ИСХОДНИКА
      (детект overlays.py + верхние углы); маппинг в координаты готового
      кадра делает сам make_vertical — по фактическому макету (split /
      blur / crop), так что геометрия всегда сходится;
      mask_boxes — уже готовые координаты вертикального кадра (старый
      интерфейс, оставлен для совместимости).
    """
    video_in = os.path.basename(str(video_in))
    video_out = os.path.basename(str(video_out))
    va = list(vcodec_args) if vcodec_args else \
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]

    src_w = src_h = 0
    try:
        meta = ffprobe_meta(os.path.join(str(workdir), video_in))
        src_w = int(meta.get("width") or 0)
        src_h = int(meta.get("height") or 0)
    except Exception:
        pass

    cam = None
    mode_low = str(mode or "blur").lower()
    if mode_low == "split" and src_w > 0 and src_h > 0:
        try:
            try:
                from webcam import detect_cam_box
            except ImportError:                       # плоский репозиторий
                from webcam import detect_cam_box
            cam = detect_cam_box(os.path.join(str(workdir), video_in),
                                 str(workdir))
        except Exception:
            log.exception("Детект камеры упал — клип собирается как blur")
            cam = None
        if cam:
            try:
                graph, base_label, _cam_h = split_graph(
                    src_w, src_h, cam, cam_frac=cam_frac, w=width, h=height)
                log.info("9:16 макет split: камера сверху, игра снизу")
            except Exception:
                log.exception("split-граф не собрался — макет blur")
                cam = None
        else:
            log.info("Камеру не нашли — макет blur (стрим по центру)")
        if cam is None:
            mode_low = "blur"
    if cam is None:
        graph = blur_graph(width, height, blur=blur) \
            if mode_low != "crop" else crop_graph(width, height)
        base_label = "v"

    # --- замазка чужих оверлеев: маппим ИСХОДНИК -> готовый кадр сами ---
    boxes = mask_boxes
    if boxes is None and raw_boxes and src_w > 0:
        try:
            try:
                from overlays import map_boxes, map_boxes_split
            except ImportError:                       # плоский репозиторий
                from overlays import map_boxes, map_boxes_split
            if cam is not None:
                cam_win, game_win, cam_h = split_windows(
                    src_w, src_h, cam, cam_frac=cam_frac, w=width, h=height)
                boxes = map_boxes_split(raw_boxes, src_w, src_h,
                                        cam_win, game_win, cam_h,
                                        out_w=width, out_h=height)
            else:
                boxes = map_boxes(raw_boxes, src_w, src_h, mode_low,
                                  out_w=width, out_h=height)
        except Exception:
            log.exception("Маппинг замазки не собрался — клип без неё")
            boxes = None

    out_label = base_label
    if boxes:
        try:
            try:
                from overlays import blur_fragment
            except ImportError:                       # плоский репозиторий
                from overlays import blur_fragment
            frag, out_label = blur_fragment(list(boxes), base_label)
            if frag:
                graph += ";" + frag
                log.info("9:16 с замазкой: %d зон чужих оверлеев", len(boxes))
        except Exception:
            log.exception("Фрагмент замазки не собрался — клип без неё")
            out_label = base_label
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
            "-i", video_in, "-filter_complex", graph,
            "-map", "[%s]" % out_label, "-map", "0:a?", "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
           + va + [video_out])
    rc, _out, err = run_cmd(cmd, timeout=900, cwd=str(workdir))
    if rc != 0:
        raise RuntimeError("ffmpeg (9:16): " + err[-400:])
    meta = ffprobe_meta(os.path.join(str(workdir), video_out))
    log.info("9:16 готов: %sx%s, %.1f с (режим %s)",
             meta.get("width"), meta.get("height"),
             float(meta.get("duration") or 0), "split" if cam else mode_low)
    return os.path.join(str(workdir), video_out)
