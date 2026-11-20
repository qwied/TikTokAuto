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
SAMPLES = 14              # сколько кадров смотрим по клипу
CONSISTENCY = 0.35        # лицо должно быть в >= 35% кадров на одном месте:
                          # Haar на мелких лицах срабатывает не каждый кадр,
                          # строгий порог (0.5) не проходил даже живой камере
MIN_FACE_W_FRAC = 0.028   # лицо уже 2.8% ширины кадра = шум, пропускаем
CAM_EXPAND = 4.0          # окно камеры во столько раз шире лица (в кадр
                          # входит голова и плечи, как у реального оверлея)
CAM_MIN_W_FRAC = 0.20     # окно камеры не может быть уже 20% ширины кадра
                          # (типичный оверлей камеры 420/1920 = 22%)
CAM_MAX_W_FRAC = 0.55     # и не шире половины кадра
CLUSTER_TOL_FRAC = 0.12   # допуск кластера центров (доля ширины кадра)
CASCADE_MIN_NEIGHBORS = 4 # мягкий порог: строгий (6) не находил мелкие лица,
                          # ложные срабатывания отсекает проверка постоянства


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


def detect_cam_box(video_path, workdir="."):
    """(x, y, w, h) окна вебкамеры в координатах ИСХОДНИКА или None."""
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

    h_f, w_f = frames.shape[1], frames.shape[2]
    scale = w_src / float(w_f)
    min_face = max(2, int(w_f * MIN_FACE_W_FRAC))
    cascade_alt2 = cascade_cls(
        cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")
    cascades = [cascade] + ([] if cascade_alt2.empty() else [cascade_alt2])
    votes = []
    n_frames = 0
    for f in frames:
        n_frames += 1
        # Тёмные/мелкие лица: детект на УДВОЕННОМ кадре двумя каскадами.
        # Мягкий порог (3) — фолбэк, если строгий (4) не нашёл ничего.
        f2 = cv2.resize(f, (f.shape[1] * 2, f.shape[0] * 2),
                        interpolation=cv2.INTER_CUBIC)
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
            votes.append((((x + fw / 2.0) / 2.0) * scale,
                          ((y + fh / 2.0) / 2.0) * scale,
                          (fw / 2.0) * scale, (fh / 2.0) * scale))
            if len(taken) >= 2:
                break

    if not votes or not n_frames:
        return None
    tol = w_src * CLUSTER_TOL_FRAC
    clusters = _cluster_votes(votes, tol)
    need = max(2, int(n_frames * CONSISTENCY))
    best = None
    for cl in clusters:
        if len(cl[2]) >= need:
            # предпочитаем кластер у краёв кадра: игровые «портреты» обычно
            # в центре, вебкамера — в углу/у края
            edge = min(cl[0], w_src - cl[0]) / float(w_src)
            key = (len(cl[2]), -edge)
            if best is None or key > best[0]:
                best = (key, cl)
    if best is None:
        log.info("Стабильного лица не нашлось (%d голосов/%d кадров) — split не строю",
                 len(votes), n_frames)
        return None

    cx = best[1][0]
    cy = best[1][1]
    fw = float(np.median([v[0] for v in best[1][2]]))
    fh = float(np.median([v[1] for v in best[1][2]]))

    cam_w = max(fw * CAM_EXPAND, w_src * CAM_MIN_W_FRAC)
    cam_w = min(cam_w, w_src * CAM_MAX_W_FRAC)
    cam_h = cam_w * 9.0 / 16.0
    if cy + cam_h / 2.0 > h_src:          # камера у нижнего края — поднимаем
        cy = h_src - cam_h / 2.0
    # лицо — в ВЕРХНЕЙ ТРЕТИ окна камеры (в кадр входят голова и плечи)
    x0 = max(0.0, min(cx - cam_w / 2.0, w_src - cam_w))
    y0 = max(0.0, min(cy - cam_h * 0.45, h_src - cam_h))
    box = (int(round(x0)), int(round(y0)), int(round(cam_w)), int(round(cam_h)))
    log.info("Вебкамера найдена: %dx%d@%d,%d (лицо в %d/%d кадрах)",
             box[2], box[3], box[0], box[1], len(best[1][2]), n_frames)
    return box
