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
SPLIT_CAM_FRAC = 0.36   # доля высоты экрана под камеру стримера (верх);
                        # по референсу пользователя: ~36% кадра, лицо крупное

# Если камеру найти не удалось: клип собирается КАК 9:16-КРОП (весь экран
# занят, без размытых полос) — жалоба пользователя на «кривые соотношения
# сторон» была именно про letterbox-полосы blur-фолбэка (dmitry_lixxx,
# прод 2026-11-20). blur остаётся доступным через config
# clip.vertical_fallback: blur.
FALLBACK_DEFAULT = "crop"

# Замазка чужих оверлеев никогда не накрывает стримера: зона лица (окно
# камеры) защищена — маска отбрасывается, если перекрывает её сильнее
# PROTECT_MAX_OVERLAP доли площади самой маски (прод-инцидент 2026-11-20:
# AFK-камера детектилась как «виджет» и верхние углы били прямо по лицу).
PROTECT_MAX_OVERLAP = 0.08
# В split полоса камеры — ЦЕЛИКОМ зона стримера: даже 2% заезда на окно
# камеры после масштабирования в широкую полосу ложатся на лицо/плечи
# (прод 2026-11-20: zloyn — замазка виджета рядом с камерой размыла полкамеры).
PROTECT_MAX_OVERLAP_SPLIT = 0.02


def _overlap(a, b):
    """Пересекаются ли прямоугольники (x, y, w, h)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or
                ay + ah <= by or by + bh <= ay)


def box_overlap_frac(box, win):
    """Доля площади box, попадающая внутрь win (оба (x, y, w, h))."""
    bx, by, bw, bh = [float(v) for v in box]
    wx, wy, ww, wh = [float(v) for v in win]
    ix = max(0.0, min(bx + bw, wx + ww) - max(bx, wx))
    iy = max(0.0, min(by + bh, wy + wh) - max(by, wy))
    area = bw * bh
    return (ix * iy) / area if area > 0 else 0.0


def strip_cam_window(src_w, src_h, cam_box, cam_h, w=WIDTH):
    """Окно камеры с АСПЕКТОМ ПОЛОСЫ (w:cam_h) — лицо не режется по краям.

    detect_cam_box возвращает вертикальное окно ~9:16, а полоса камеры в
    макете широкая (1080:691 ≈ 1.56). Прежний путь «crop узкого окна ->
    scale=increase -> центр-кроп» срезал БОКА: лицо у края окна оказывалось
    за кадром (прод 2026-11-22 frametamer666: в полосе остался только край
    головы — «опять без лица»). Теперь окно строим сразу широким, с тем же
    аспектом, что и полоса: масштабирование без обрезки, лицо внутри.
    """
    cx = float(cam_box[0]) + float(cam_box[2]) / 2.0
    cy = float(cam_box[1]) + float(cam_box[3]) / 2.0
    ar = float(w) / float(max(1, cam_h))          # аспект полосы камеры
    win_h = float(cam_box[3])
    win_w = win_h * ar
    if win_w > src_w:                             # окно шире исходника
        win_w = float(src_w)
        win_h = win_w / ar
    if win_h > src_h:                             # и не выше исходника
        win_h = float(src_h)
        win_w = min(win_h * ar, float(src_w))
    x0 = max(0.0, min(cx - win_w / 2.0, src_w - win_w))
    # лицо в ВЕРХНЕЙ ТРЕТИ окна (голова и плечи целиком)
    y0 = max(0.0, min(cy - win_h * 0.45, src_h - win_h))
    return (x0, y0, win_w, win_h)


def looks_fullframe_cam(src_w, src_h, cam_box):
    """True — «камера» в ЦЕНТРЕ кадра и крупная: похоже на IRL-стрим.

    У IRL-стримов (frametamer666, прод 2026-11-22) весь кадр — вебкамера:
    split не имеет смысла (нижняя секция = та же комната), правильный макет
    — 9:16-кроп по лицу. У игровых стримеров камера почти всегда у края
    (webcam.py даёт краевой бонус), поэтому центр+крупно = фуллфрейм-камера.
    """
    w = max(1.0, float(src_w))
    h = max(1.0, float(src_h))
    cx = (float(cam_box[0]) + float(cam_box[2]) / 2.0) / w
    cy = (float(cam_box[1]) + float(cam_box[3]) / 2.0) / h
    big = float(cam_box[2]) >= 0.30 * w
    return (0.32 <= cx <= 0.68) and (0.24 <= cy <= 0.76) and big


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
    cam_h += cam_h % 2          # ЧЁТНАЯ высота секции: yuv420p округляет
                                # нечётные размеры вниз и vstack теряет пиксели
                                # (прод 2026-11-20: 691+1229 -> 1918 вместо 1920)
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


def split_cam_h(h=HEIGHT, cam_frac=SPLIT_CAM_FRAC):
    """ЧЁТНАЯ высота полосы камеры в готовом кадре (см. split_windows).

    Единая точка правды для lipsync/pipeline: без повторного детекта лица
    на вертикальном клипе — полоса известна из геометрии макета.
    """
    cam_h = int(round(h * float(cam_frac)))
    cam_h += cam_h % 2
    return cam_h


def split_graph(src_w, src_h, cam_box, cam_frac=SPLIT_CAM_FRAC,
                w=WIDTH, h=HEIGHT):
    """ffmpeg-граф макета «камера сверху, игра снизу».

    Возвращает (строка_фильтра, метка_выхода, cam_h). Обе секции заполняют
    свою часть экрана ЦЕЛИКОМ (scale=increase + crop): никаких полос и
    размытых фонов. Между секциями — тонкий разделитель (drawbox).
    Замазка чужих оверлеев накладывается ПОВЕРХ (blur_fragment), баннер
    RubUP рисуется ещё позже — его замазка не трогает.

    ОКНО КАМЕРЫ: перед геометрией окно расширяется до аспекта полосы
    (strip_cam_window) — scale=increase+crop больше не срезает лицо по краям.
    """
    cam_h0 = split_cam_h(h, cam_frac)
    wide = strip_cam_window(src_w, src_h, cam_box, cam_h0, w=w)
    cam_win, game_win, cam_h = split_windows(src_w, src_h, wide,
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
        # тонкий БЕЛЫЙ разделитель между секциями (как на референсе
        # пользователя IMG_3624: камера сверху, белая полоска, игра снизу)
        f"[vs]drawbox=x=0:y={max(0, cam_h - 5)}:w={w}:h=5:color=white@0.92:t=fill[vsep]"
    )
    return graph, "vsep", cam_h


def blur_graph(w=WIDTH, h=HEIGHT, blur=26):
    """Фон — размытый кадр на весь 9:16, стрим по центру во всю ширину."""
    return ("[0:v]split=2[vbg][vfg];"
            f"[vbg]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=luma_radius={blur}:luma_power=2,setsar=1[bg];"
            f"[vfg]scale={w}:-2,setsar=1[fgs];"
            "[bg][fgs]overlay=x=(W-w)/2:y=(H-h)/2[v]")


def crop_graph(w=WIDTH, h=HEIGHT, src_w=0, src_h=0, face_box=None):
    """Без фона: вертикальная полоса 9:16 растягивается на весь экран.

    Если найдено лицо (окно камеры / слабое окно лица) — полоса кропа
    центрируется ТАК, чтобы стример гарантированно попал в кадр:
    слепой центр-кроп резал камеру в углу (жалоба пользователя
    «опять без лица», прод 2026-11-21). Без лица — центр, как раньше.
    """
    if src_w > 0 and src_h > 0:
        cw = min(src_w, int(round(src_h * float(w) / float(h))))
        ch = src_h
        x = max(0, min((src_w - cw) // 2, src_w - cw))
        if face_box:
            fx, fy, fw2, fh2 = [float(v) for v in face_box]
            cx = fx + fw2 / 2.0
            x = int(round(max(0.0, min(cx - cw / 2.0, src_w - cw))))
            log = __import__("logging").getLogger("vertical")
            log.info("9:16 кроп с лицом: окно x=%d (ширина %d, лицо в %.0f/%.0f)",
                     x, cw, fx + fw2 / 2, fy + fh2 / 2)
        return (f"[0:v]crop={cw}:{ch}:{x}:0,"
                f"scale={w}:{h},setsar=1[v]")
    return (f"[0:v]crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})',"
            f"scale={w}:{h},setsar=1[v]")


def make_vertical(video_in, video_out, workdir, width=WIDTH, height=HEIGHT,
                  mode="blur", blur=26, vcodec_args=None, mask_boxes=None,
                  raw_boxes=None, cam_frac=SPLIT_CAM_FRAC, cam_box=None,
                  fallback=FALLBACK_DEFAULT):
    """Перевести клип в вертикальный формат 9:16.

    Принимает собранный сырой клип (ts/mp4), возвращает путь к вертикальному
    mp4. Длительность и звук сохраняются; звук перекодируется в AAC
    (совместимо с TikTok). Выход: h264 + yuv420p + faststart.

    Режим split: авто-детект вебкамеры (webcam.py) -> камера сверху,
    игра снизу. Камеру не нашли / cv2 нет -> клип собирается как
    9:16-КРОП (fallback, весь экран без полос; blur — по config).

    cam_box — уже найденное окно камеры (передаёт pipeline, чтобы не
    детектить дважды); None — детектим сами.

    ЗАЩИТА ЛИЦА: зоны замазки (raw_boxes) никогда не накрывают окно камеры
    (в split) / слабое окно лица (в blur/crop) — прод 2026-11-20: AFK-камера
    детектилась как «чужой виджет» и размывала стримера.

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
    fallback_low = str(fallback or FALLBACK_DEFAULT).lower()
    full_in = os.path.join(str(workdir), video_in)
    graph = None
    base_label = "v"
    face_override = None
    if mode_low == "split" and src_w > 0 and src_h > 0:
        if cam_box:
            cam = tuple(float(v) for v in cam_box)   # найдено заранее (pipeline)
            log.info("9:16 split: окно камеры передано пайплайном")
        else:
            try:
                try:
                    from webcam import detect_cam_box
                except ImportError:                       # плоский репозиторий
                    from webcam import detect_cam_box
                cam = detect_cam_box(full_in, str(workdir))
            except Exception:
                log.exception("Детект камеры упал — клип собирается как %s",
                              fallback_low)
                cam = None
        if cam and looks_fullframe_cam(src_w, src_h, cam):
            # IRL-стрим: весь кадр — вебкамера. split бессмысленен (внизу
            # окажется ТА ЖЕ комната, прод 2026-11-22 frametamer666), лицом
            # остаётся весь найденный кластер — макет 9:16-КРОП ПО ЛИЦУ.
            log.info("IRL: камера занимает центр всего кадра — макет %s "
                     "(9:16-кроп по лицу) вместо split", fallback_low)
            face_override = cam
            cam = None
            # Уточнение лица слабым детектом: кластер «камеры» может занимать
            # полкадра, а человек стоять у края — кроп по центру кластера
            # снова режет лицо. Если слабый детект нашёл лицо ВНУТРИ
            # кластера — центрируемся по НАСТОЯЩЕМУ лицу, не по кластеру.
            try:
                try:
                    from webcam import detect_cam_box as _dcbox
                except ImportError:                       # плоский репозиторий
                    from webcam import detect_cam_box as _dcbox
                _fw = _dcbox(full_in, str(workdir), weak=True)
            except Exception:
                log.exception("Слабый детект лица (IRL) не удался — кроп по кластеру")
                _fw = None
            if _fw and box_overlap_frac(_fw, face_override) > 0.3:
                log.info("IRL: лицо уточнено слабым детектом %s — кроп по нему",
                         [round(v) for v in _fw])
                face_override = _fw
        if cam:
            # ПРОВЕРКА ЛИЦА В ОКНЕ КАМЕРЫ (жалоба «опять без лица»): окно
            # «камеры» может оказаться чужим виджетом — тогда split кладёт
            # виджет наверх, а лицо режется в игровой секции. Слабый детект
            # лица: если лицо нашлось и оно ВНЕ окна камеры — доверяем ЛИЦУ,
            # а не окну: макет меняется на 9:16-кроп по лицу. Если слабый
            # детект ничего не нашёл — верим камере (не наказываем split
            # за каприз детектора).
            _fw = None
            try:
                try:
                    from webcam import detect_cam_box as _dcbox
                except ImportError:                       # плоский репозиторий
                    from webcam import detect_cam_box as _dcbox
                _fw = _dcbox(full_in, str(workdir), weak=True)
            except Exception:
                log.exception("Слабая проверка лица не удалась — верю окну камеры")
                _fw = None
            if _fw and box_overlap_frac(_fw, cam) < 0.25:
                log.info("Лицо %s вне окна камеры %s (пересечение %.0f%%) — "
                         "макет 9:16-кроп по лицу вместо split",
                         [round(v) for v in _fw], [round(v) for v in cam],
                         box_overlap_frac(_fw, cam) * 100)
                face_override = _fw
                cam = None
        if cam:
            try:
                graph, base_label, _cam_h = split_graph(
                    src_w, src_h, cam, cam_frac=cam_frac, w=width, h=height)
                log.info("9:16 макет split: камера сверху, игра снизу")
            except Exception:
                log.exception("split-граф не собрался — макет %s", fallback_low)
                cam = None
        else:
            log.info("Камеру не нашли — макет %s (весь экран без полос)",
                     fallback_low)
        if cam is None:
            mode_low = fallback_low

    # --- ЗОНА ЛИЦА (защита от замазки + кроп по лицу) ---------------------------
    # В split это окно камеры; в blur/crop камеру ищем СЛАБЫМ детектом
    # (достаточно 2 голосов и кожи): замазка чужих оверлеев не должна
    # накрывать стримера ни в одном макете, а кроп обязан показать лицо.
    face_box = None
    if src_w > 0 and src_h > 0:
        if face_override is not None:
            face_box = face_override             # IRL-детект уже сделан выше
        elif cam is not None:
            try:
                face_box = split_windows(src_w, src_h, cam, cam_frac=cam_frac,
                                         w=width, h=height)[0]
            except Exception:
                face_box = cam
        elif graph is None and mode_low in ("blur", "crop"):
            try:
                try:
                    from webcam import detect_cam_box
                except ImportError:                   # плоский репозиторий
                    from webcam import detect_cam_box
                face_box = detect_cam_box(full_in, str(workdir), weak=True)
            except Exception:
                log.exception("Слабый детект камеры не удался — "
                              "защита лица без него")
                face_box = None

    if graph is None:
        # split не собрался: фолбэк. В кропе окно смещаем К ЛИЦУ, если оно
        # найдено — иначе стример остаётся за кадром («без лица»).
        graph = blur_graph(width, height, blur=blur) \
            if mode_low != "crop" \
            else crop_graph(width, height, src_w, src_h, face_box=face_box)
        base_label = "v"

    # --- замазка чужих оверлеев: маппим ИСХОДНИК -> готовый кадр сами ---
    boxes = mask_boxes
    if boxes is None and raw_boxes and src_w > 0:
        if face_box:
            # В split полоса камеры — зона стримера ЦЕЛИКОМ: порог жёстче (2%),
            # плюс любое пересечение центром зоны маски с окном камеры = отказ.
            # В blur/crop лицо ищем слабым детектом — порог мягче (8%).
            thr = PROTECT_MAX_OVERLAP_SPLIT if cam is not None \
                else PROTECT_MAX_OVERLAP
            fx, fy, fw2, fh2 = [float(v) for v in face_box]
            kept = []
            for b in raw_boxes:
                try:
                    hit = box_overlap_frac(b, face_box)
                except Exception:
                    hit = 0.0
                bcx = float(b[0]) + float(b[2]) / 2.0
                bcy = float(b[1]) + float(b[3]) / 2.0
                center_inside = (fx <= bcx <= fx + fw2 and fy <= bcy <= fy + fh2)
                if hit > thr or center_inside:
                    log.info("Замазка пропускает зону лица %s (перекрытие %.0f%%, "
                             "центр в камере: %s)", b, hit * 100, center_inside)
                    continue
                kept.append(b)
            raw_boxes = kept
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
                # страховка: всё, что легло в ПОЛОСУ КАМЕРЫ (выше разделителя),
                # замазывать нельзя — там стример (прод 2026-11-20: zloyn)
                before_strip = len(boxes)
                boxes = [b for b in boxes if b[1] + b[3] > cam_h]
                if len(boxes) != before_strip:
                    log.info("Замазка: %d зон убраны из полосы камеры",
                             before_strip - len(boxes))
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
