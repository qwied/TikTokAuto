"""Авто-детект окна вебкамеры стримера.

Нужен для макета 9:16 «стример сверху, игра снизу» (clip.vertical_mode: split):
  * ffmpeg отдаёт ~14 кадров клипа (серые, 640px — лица различимы);
  * OpenCV (haar cascade, идёт в комплекте opencv-python-headless) ищет лица;
  * ТЁМНЫЕ/МЕЛКИЕ КАМЕРЫ (типично на русских стримах: комната с LED-подсветкой,
    лицо ~60px на 1080p) ловятся детектом на кадре x2 двумя каскадами
    (frontalface_default + frontalface_alt2), мягкий порог minNeighbors=3
    как фолбэк — дубли вырезаются, мусор отсекает проверка постоянства;
  * лицо считается вебкамерой, если оно стабильно сидит НА ОДНОМ МЕСТЕ
    в большинстве кадров. Лица в игре прыгают вместе с камерой и проверку
    постоянства не проходят;
  * вокруг лица строится окно камеры (с запасом — в кадр входит голова
    и плечи, лицо — в верхней трети окна).

Если cv2 не установлен, лица нет или кадры не читаются — возвращается None:
клип уходит по старому макету (стрим по центру на размытом фоне).
Ничего не ломается в любом сценарии.

Режим weak=True: вернуть СЛАБОЕ окно лица — лучший кластер хотя бы из
WEAK_VOTES голосов, даже если строгая стабильность (CONSISTENCY) не набрана.
Слабое окно используется НЕ для макета, а для ЗАЩИТЫ ЛИЦА от замазки чужих
оверлеев: прод 2026-11-20 показал, что Хаар на мелких/тёмных лицах русских
стримов срабатывает рывками (2-3 голоса из 14) и строгий порог не набирается,
а зоны замазки при этом спокойно накрывают стримера (AFK-камера для детекта
оверлеев «статична», верхние углы — прямо поверх камеры в углу).
"""

import logging
import os

try:
    from utils import ffprobe_meta, run_cmd
except ImportError:                                    # плоский репозиторий
    from utils import ffprobe_meta, run_cmd

log = logging.getLogger("webcam")

ANALYSIS_W = 640          # ширина кадра для анализа: лицо на стримах часто
                          # мелкое (~90px на 1920) — 480px его не ловило
SAMPLES = 20              # сколько кадров смотрим по клипу (было 14: Хаар
                          # на мелких лицах срабатывает рывками — больше
                          # кадров, больше шансов набрать стабильность)
SAMPLES_FULL = 12         # кадров во ВТОРОЙ (полноформатной) попытке: Хаар
                          # на 1920x1080 медленный, 12 кадров ловят камеру
                          # и укладываются в ~5-8 c
CONSISTENCY = 0.18        # лицо должно быть в >= 18% кадров на одном месте:
                          # прод 2026-11-20 — три клипа подряд без split,
                          # кластеры набирали 2-3 голоса из 14 при need=4;
                          # ложные срабатывания по-прежнему отсекают
                          # проверка кожи и предпочтение краёв кадра
WEAK_VOTES = 2            # минимум голосов для слабого окна (защита лица)
MIN_FACE_W_FRAC = 0.028   # лицо уже 2.8% ширины кадра = шум, пропускаем
CAM_EXPAND = 4.0          # окно камеры во столько раз шире лица (в кадр
                          # входит голова и плечи, как у реального оверлея)
CAM_MIN_W_FRAC = 0.20     # окно камеры не может быть уже 20% ширины кадра
                          # (типичный оверлей камеры 420/1920 = 22%)
CAM_MAX_W_FRAC = 0.55     # и не шире половины кадра
CLUSTER_TOL_FRAC = 0.12   # допуск кластера центров (доля ширины кадра)
CASCADE_MIN_NEIGHBORS = 4 # мягкий порог: строгий (6) не находил мелкие лица,
                          # ложные срабатывания отсекает проверка постоянства
CAM_MAX_TEMPORAL_STD = 45.0  # «живость» зоны: игровой UI (плитки с мемами,
                          # обменными картинками) мигает — std времени >45;
                          # настоящая камера: 15-35 (фон стабилен, лицо шевелится).
                          # Прод 2026-11-20: welovegames собрал split по игровым
                          # плиткам (std 53) — сверху оказался не стример.
CAM_MIN_PAIR_DIFF = 0.8   # гейт НЕПРЕРЫВНОСТИ движения (медиана межкадровой
CAM_MAX_PAIR_DIFF = 13.0  # разницы, 40 с по 1 кадру/с): настоящая камера —
                          # человек шевелится ПОСТОЯННО, фон статичен -> медиана
                          # 4-9 (замеры на проде); геймплей с панорамой камеры —
                          # движется ВСЯ зона -> медиана 22-40 (dmitry NPC 37,
                          # тайлы welovegames 22-40); тайл между сменами картинок
                          # стоячий -> низкая медиана. Прод 2026-11-20: без гейта
                          # split строился на NPC из GTA и аниме-тайле.
SKIN_MIN_RATIO = 0.05     # кожа в окне камеры: порог снижен с 0.10 — LED-подсветка
                          # русских стримов красит лицо в синий, классическое
                          # правило кожи (R>G, R>B) на нём не срабатывает и
                          # настоящие камеры отбрасывались (прод: dmitry_lixxx)


def _extract_frames_gray(video_path, workdir):
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
            "-frames:v", str(SAMPLES + 2),
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    rc, out, _err = run_cmd(cmd, timeout=180, cwd=str(workdir), binary=True)
    if rc != 0 or not out:
        return None, (0, 0)
    frame = ANALYSIS_W * h_an
    n = len(out) // frame
    if n < 4:
        return None, (0, 0)
    arr = np.frombuffer(out[:n * frame], dtype=np.uint8).reshape(n, h_an, ANALYSIS_W)
    return arr, (w_src, h_src)


def _extract_frames_full(video_path, workdir):
    """~SAMPLES кадров В ПОЛНОМ разрешении (вторая попытка детекта).

    Мелкие/тёмные камеры (лицо ~60-100px на 1080p) при сжатии до 640px
    превращаются в ~25-30px — Хаар их ловит рывками. Полный размер
    увеличивает лицо в 3 раза против аналитического кадра.
    """
    import numpy as np
    meta = ffprobe_meta(video_path)
    w_src = int(meta.get("width") or 0)
    h_src = int(meta.get("height") or 0)
    dur = float(meta.get("duration") or 0)
    if w_src <= 0 or h_src <= 0 or dur <= 0:
        return None
    n_want = min(SAMPLES_FULL, SAMPLES)
    fps = min(1.0, n_want / max(dur, 1.0))
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", os.path.basename(str(video_path)),
            "-vf", "fps=%.6f" % fps,
            "-frames:v", str(n_want + 2),
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    rc, out, _err = run_cmd(cmd, timeout=240, cwd=str(workdir), binary=True)
    if rc != 0 or not out:
        return None
    frame = w_src * h_src
    n = len(out) // frame
    if n < 4:
        return None
    return np.frombuffer(out[:n * frame], dtype=np.uint8).reshape(n, h_src, w_src)


def _detect_faces_in_frames(frames, w_src, cascades):
    """Голосования Хаара по массиву кадров -> [(cx, cy, fw, fh)] в коорд. исходника."""
    h_f, w_f = frames.shape[1], frames.shape[2]
    scale = w_src / float(w_f)
    min_face = max(2, int(w_f * MIN_FACE_W_FRAC))
    votes = []
    for f in frames:
        # Мелкие лица: детект на УДВОЕННОМ кадре, два каскада, мягкий порог (3)
        # как фолбэк, если строгий (4) не нашёл ничего.
        if f.shape[0] > 800 or f.shape[1] > 1300:
            f2 = f                      # уже полный размер — удвоение дорого
            min2 = min_face
        else:
            f2 = None
        if f2 is None:
            f2 = _resize2(f)
            min2 = min_face * 2
        fl = []
        for casc in cascades:
            found = []
            for minN in (CASCADE_MIN_NEIGHBORS, 3):
                faces = casc.detectMultiScale(
                    f2, scaleFactor=1.1, minNeighbors=minN,
                    minSize=(min2, min2))
                try:
                    found = [(int(a), int(b), int(c), int(d))
                             for (a, b, c, d) in faces]
                except TypeError:
                    found = []
                if found:
                    break
            fl.extend(found)
        fl.sort(key=lambda r: r[2] * r[3], reverse=True)
        # на кадр НЕ БОЛЬШЕ двух РАЗНЫХ лиц (дубли от порогов/каскадов и
        # мусор отхватывается меньше): вебкамера обычно самая «живая» область
        taken = []
        for (x, y, fw, fh) in fl:
            if fw < min2:
                continue
            if any(abs(x - sx) < max(fw, sw) / 2 and abs(y - sy) < max(fh, sh) / 2
                   for sx, sy, sw, sh in taken):
                continue
            taken.append((x, y, fw, fh))
            votes.append((((x + fw / 2.0) / (2.0 if f2 is not f else 1.0)) * scale,
                          ((y + fh / 2.0) / (2.0 if f2 is not f else 1.0)) * scale,
                          (fw / (2.0 if f2 is not f else 1.0)) * scale,
                          (fh / (2.0 if f2 is not f else 1.0)) * scale))
            if len(taken) >= 2:
                break
    return votes


def _resize2(f):
    """Кадр x2 (inter_cubic) — для Хаара на мелких лицах."""
    import cv2
    return cv2.resize(f, (f.shape[1] * 2, f.shape[0] * 2),
                      interpolation=cv2.INTER_CUBIC)


def _temporal_std_ok(video_path, box, workdir):
    """True, если зона похожа на вебкамеру по «живости».

    Настоящая камера: фон стабилен, лицо шевелится — std времени 15-35.
    Игровые UI (плитки/меню, где Хаар находит «лица») мигают картинками —
    std > 45. Ошибка чтения = True (проверка только улучшает).
    """
    import numpy as np
    try:
        x, y, w, h = [int(v) for v in box]
        w = max(8, min(w, 2000))
        h = max(8, min(h, 2000))
        hh = max(8, int(round(160 * h / float(w) / 2.0)) * 2)
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", os.path.basename(str(video_path)),
                "-vf", "fps=0.45,crop=%d:%d:%d:%d,scale=160:%d" %
                       (w, h, x, y, hh),
                "-frames:v", "20", "-f", "rawvideo", "-pix_fmt", "gray",
                "pipe:1"])
        rc, out, _err = run_cmd(cmd, timeout=120, cwd=str(workdir), binary=True)
        if rc != 0 or len(out) < 160 * hh * 4:
            return True                    # не удалось прочитать — не мешаем
        n = len(out) // (160 * hh)
        arr = np.frombuffer(out[:n * 160 * hh], dtype=np.uint8)
        arr = arr.reshape(n, hh, 160).astype(np.float32)
        std_t = float(arr.std(axis=0).mean())
        log.info("Живость зоны камеры: std времени %.1f (порог %.0f)",
                 std_t, CAM_MAX_TEMPORAL_STD)
        return std_t <= CAM_MAX_TEMPORAL_STD
    except Exception:
        log.exception("Замер живости не удался — пропускаю (не блокирую)")
        return True


def _motion_ok(video_path, box, workdir):
    """True, если зона движется КАК настоящая камера (непрерывно и умеренно).

    Медиана межкадровой разницы за ~40 с: у настоящей камеры человек шевелится
    непрерывно на статичном фоне -> 4-9; геймплей с панорамой движется ВСЕЙ
    зоной -> 22-40; тайл между сменами картинок стоит -> ~0. Ошибка = True.
    """
    import numpy as np
    try:
        x, y, w, h = [int(v) for v in box]
        w = max(8, min(w, 2000))
        h = max(8, min(h, 2000))
        hh = max(8, int(round(160 * h / float(w) / 2.0)) * 2)
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", os.path.basename(str(video_path)),
                "-vf", "fps=1.0,crop=%d:%d:%d:%d,scale=160:%d" %
                       (w, h, x, y, hh),
                "-frames:v", "40", "-f", "rawvideo", "-pix_fmt", "gray",
                "pipe:1"])
        rc, out, _err = run_cmd(cmd, timeout=120, cwd=str(workdir), binary=True)
        if rc != 0 or len(out) < 160 * hh * 6:
            return True                    # не удалось прочитать — не мешаем
        n = len(out) // (160 * hh)
        arr = np.frombuffer(out[:n * 160 * hh], dtype=np.uint8)
        arr = arr.reshape(n, hh, 160).astype(np.float32)
        d = np.abs(arr[1:] - arr[:-1]).mean(axis=(1, 2))
        med = float(np.median(d))
        ok = CAM_MIN_PAIR_DIFF <= med <= CAM_MAX_PAIR_DIFF
        log.info("Непрерывность движения зоны камеры: медиана %.1f "
                 "(норма %.1f-%.1f) -> %s", med, CAM_MIN_PAIR_DIFF,
                 CAM_MAX_PAIR_DIFF, "камера" if ok else "НЕ камера")
        return ok
    except Exception:
        log.exception("Замер движения не удался — пропускаю (не блокирую)")
        return True


def _cluster_votes(votes, tol):
    """Голосования лиц -> [(центр_x, центр_y, [(w, h), ...])], жадная кластеризация."""
    clusters = []
    for cx, cy, fw, fh in votes:
        placed = False
        for cl in clusters:
            if abs(cl[0] - cx) <= tol and abs(cl[1] - cy) <= tol:
                cl[2].append((fw, fh))
                placed = True
                break
        if not placed:
            clusters.append([cx, cy, [(fw, fh)]])
    return clusters


def detect_cam_box(video_path, workdir=".", weak=False):
    """(x, y, w, h) окна вебкамеры в координатах ИСХОДНИКА или None.

    weak=True — ослабленный режим: строгая стабильность (CONSISTENCY) не
    обязательна, достаточно WEAK_VOTES голосов на одном месте. Возврат
    идентичный; вызов строго режима не меняет поведения.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        log.info("cv2 недоступен — клип собирается по макету blur (без split)")
        return None

    # OpenCV 5.x убрал CascadeClassifier из python-биндингов (прод-инцидент
    # 2026-11-20: requirements тянули 5.x и split молча отключался на всех
    # клипах). Требование закреплено как >=4.9,<5, но на всякий случай
    # выходим тихо, без трейсбэки на каждый клип.
    cascade_cls = getattr(cv2, "CascadeClassifier", None)
    if cascade_cls is None:
        log.warning("cv2 без CascadeClassifier (OpenCV 5.x?) — split выключен, "
                    "клип соберётся как blur. Поставьте opencv-python-headless<5")
        return None

    frames, (w_src, h_src) = _extract_frames_gray(video_path, workdir)
    if frames is None or not w_src or not h_src:
        return None
    cascade = cascade_cls(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        log.warning("haar cascade не загрузился — детект камеры выключен")
        return None

    cascade_alt2 = cascade_cls(
        cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")
    cascades = [cascade] + ([] if cascade_alt2.empty() else [cascade_alt2])

    votes = _detect_faces_in_frames(frames, w_src, cascades)
    n_frames = len(frames)
    candidates = _ranked_clusters(votes, n_frames, w_src, weak=weak)

    if not candidates and not weak:
        # ВТОРАЯ ПОПЫТКА: полный размер (мелкие/тёмные камеры, прод 2026-11-20:
        # dmitry_lixxx — лицо ~90px, на 640px-анализе Хаар ловил только камуфляж
        # куртки, клип уходил в blur и замазка накрывала стримера).
        # В слабом режиме (защита лица/лип-синк) полный размер не гоняем:
        # строгий детект уже отработал в pipeline, слабый должен быть быстрым.
        try:
            full_frames = _extract_frames_full(video_path, workdir)
        except Exception:
            log.exception("Полноформатная выборка не удалась")
            full_frames = None
        if full_frames is not None:
            fv = _detect_faces_in_frames(full_frames, w_src, cascades)
            log.info("Полноформатная попытка: %d голосов", len(fv))
            candidates = _ranked_clusters(fv, len(full_frames), w_src, weak=weak)

    if not candidates:
        log.info("Стабильного лица не нашлось — split не строю")
        return None

    # Перебираем кандидатов: первый, прошедший гейты (живость/движение/кожа),
    # становится окном камеры (dmitry_lixxx: лучший по score был NPC из игры)
    box = None
    for cl in candidates[:3]:
        cx = cl[0]
        cy = cl[1]
        fw = float(np.median([v[0] for v in cl[2]]))
        fh = float(np.median([v[1] for v in cl[2]]))

        cam_w = max(fw * CAM_EXPAND, w_src * CAM_MIN_W_FRAC)
        cam_w = min(cam_w, w_src * CAM_MAX_W_FRAC)
        cam_h = cam_w * 9.0 / 16.0
        if cy + cam_h / 2.0 > h_src:          # камера у нижнего края — поднимаем
            cy = h_src - cam_h / 2.0
        # лицо — в ВЕРХНЕЙ ТРЕТИ окна камеры (в кадр входят голова и плечи)
        x0 = max(0.0, min(cx - cam_w / 2.0, w_src - cam_w))
        y0 = max(0.0, min(cy - cam_h * 0.45, h_src - cam_h))
        cand = (int(round(x0)), int(round(y0)), int(round(cam_w)), int(round(cam_h)))

        # --- ГЕЙТЫ: отсекаем игровые UI, панорамирующий геймплей, мультики ----
        if not _temporal_std_ok(video_path, cand, workdir):
            continue
        if not _motion_ok(video_path, cand, workdir):
            continue
        if not _skin_ok(video_path, cand, workdir):
            continue
        box = cand
        votes_n = len(cl[2])
        break

    if box is None:
        log.info("Ни один кластер не прошёл гейты камеры — split не строю, "
                 "клип соберётся как 9:16-кроп (весь экран, без полос)")
        return None
    log.info("Вебкамера найдена: %dx%d@%d,%d (лицо в %d/%d кадрах)%s",
             box[2], box[3], box[0], box[1], votes_n, n_frames,
             " [weak]" if weak else "")
    return box


def _skin_ok(video_path, box, workdir):
    """True, если в окне камеры есть телесные тона (реальная вебкамера).

    ~6 цветных кадров из окна -> доля пикселей по классическому RGB-правилу
    (R>95, G>40, B>20, R>G, R>B, R-max(G,B)>15). Любая ошибка чтения = True
    (проверка только улучшает, никогда не блокирует сборку).
    """
    import numpy as np
    try:
        x, y, w, h = box
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", os.path.basename(str(video_path)),
                "-vf", "fps=0.15,crop=%d:%d:%d:%d,scale=160:90" % (w, h, x, y),
                "-frames:v", "6", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
        rc, out, _err = run_cmd(cmd, timeout=120, cwd=str(workdir), binary=True)
        if rc != 0 or len(out) < 160 * 90 * 3:
            return True                    # не удалось прочитать — не мешаем
        n = len(out) // (160 * 90 * 3)
        arr = np.frombuffer(out[:n * 160 * 90 * 3], dtype=np.uint8)
        arr = arr.reshape(n, 90, 160, 3).astype(np.int16)
        r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
        mx = np.maximum(g, b)
        skin = (r > 95) & (g > 40) & (b > 20) & (r > g) & (r >= b) & \
               ((r - mx) > 15)
        ratio = float(skin.mean())
        log.info("Проверка кожи в окне камеры: %.0f%% телесных пикселей", ratio * 100)
        return ratio >= SKIN_MIN_RATIO
    except Exception:
        log.exception("Проверка кожи не удалась — пропускаю (не блокирую)")
        return True


def _ranked_clusters(votes, n_frames, w_src, weak=False):
    """Кандидаты в камеру, отсортированные по убыванию score (лучший первый).

    Список — не одно окно: если лучший кластер не проходит гейты (живость/
    движение/кожа), перебираем следующие — у dmitry_lixxx самый высокий
    score имел NPC из игры, настоящая камера была вторым кандидатом.
    """
    if not votes or not n_frames:
        return []
    tol = w_src * CLUSTER_TOL_FRAC
    clusters = _cluster_votes(votes, tol)

    def _score(cl):
        """Голоса × краевой бонус: вебкамера почти всегда у края кадра
        (0.1 от края даёт множитель 2.35, центр — 1.0). Плавный приоритет
        вместо лексикографического: 3 голоса у края важнее 4 в центре
        игры, но 4 стабильных голоса в центре бьют 2 шумовых у края."""
        edge = min(cl[0], w_src - cl[0]) / float(w_src)
        return len(cl[2]) * (1.0 + 1.5 * (1.0 - edge))

    need = max(2, int(n_frames * CONSISTENCY))
    strong = [cl for cl in clusters if len(cl[2]) >= need]
    strong.sort(key=_score, reverse=True)
    if strong or not weak:
        return strong
    weak_cl = [cl for cl in clusters if len(cl[2]) >= WEAK_VOTES]
    weak_cl.sort(key=_score, reverse=True)
    if weak_cl:
        log.info("Слабое окно лица: %d голосов (строгий порог %d не набран) "
                 "— использую только для защиты лица от замазки",
                 len(weak_cl[0][2]), need)
    return weak_cl
