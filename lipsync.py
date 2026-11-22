"""Синхронизация субтитров с губами стримера (lip-sync gate).

Жалоба пользователя: «субтитры произносятся, когда стример молчит — следи
по губам». Whisper распознаёт и ЧУЖУЮ речь (голоса из игры, NPC, собеседники
в войсчате), а на паузах галлюцинирует — строки появляются, хотя рот
стримера закрыт. Решение — смотреть на губы:

  1) webcam.py находит окно камеры (на вертикальном клипе split лицо сидит
     в верхней полосе — детект работает по самому клипу);
  2) внутри окна лица ищется рот (Хаар по ~8 кадрам + геометрический
     фолбэк: нижние 45% лица);
  3) ffmpeg отдаёт ~6 кадров/с области рта, numpy считает «энергию речи» —
     попиксельную разницу соседних кадров (открытый рот = движение);
  4) гистерезис + слияние коротких пауз + поля 0.25 c -> [(start, end)].

Интервалы едут в subtitle.make_subtitles_meta: слова вне «говорения» в
субтитры не попадают. Лицо не нашли — возвращаем None, субтитры остаются
как были (ничего не ломаем). Всё локально, без нейросетей: клип 45 c
анализируется за ~3-5 с.
"""

import logging
import os

try:
    from utils import ffprobe_meta, run_cmd
except ImportError:                                    # плоский репозиторий
    from utils import ffprobe_meta, run_cmd

log = logging.getLogger("lipsync")

FPS = 6.0            # кадров/с на анализ рта: темп речи хорошо виден
ROIS_W = 160         # ширина области рта для анализа (быстро и достаточно)
FACE_PROBE_W = 320   # ширина кадра для поиска лица внутри окна камеры
FACE_MIN_VOTES = 3   # из скольких кадров достаточно, чтобы поверить лицу
YUNET_FILE = "face_detection_yunet_2023mar.onnx"   # 232 КБ, вшит в репо
YUNET_SCORE = 0.6    # порог уверенности детекции YuNet
MOUTH_H_FRAC = 0.32  # рот — зона 50-82% высоты лица (губы + челюсть)
MOUTH_Y0_FRAC = 0.50 # начало зоны рта (прод-баг: ROI на 55% высоты попадал
                     # на подбородок/воротник — энергия речи занижалась)
MOUTH_W_FRAC = 0.60  # и центральные 60% ширины лица
MIN_INTERVAL = 0.5   # короче — не речь, а шум
MERGE_GAP = 0.8      # паузы короче 0.8 c склеиваем (речь с придыханием)
PAD = 0.25           # поля интервалов (субтитры чуть «дышат» вокруг речи)
THR_ABS = 2.5        # минимальная энергия речи (0..255)
THR_K = 1.30         # порог = K * медианная энергия (адаптивно к яркости)
OFF_K = 0.75         # «тишина», когда энергия ниже 75% порога 2 сэмпла подряд

_re_log = False


def _extract_roi_stream(video_path, workdir, roi, dur):
    """Кадры ROI через ffmpeg -> numpy (N, H, W) uint8, шаг 1/FPS."""
    import numpy as np
    x, y, w, h = [int(round(float(v))) for v in roi]
    if w < 8 or h < 8:
        return None
    hh = max(8, int(round(ROIS_W * h / float(w) / 2.0)) * 2)
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", "0", "-i", os.path.basename(str(video_path)),
            "-vf", "fps=%.3f,crop=%d:%d:%d:%d,scale=%d:%d" %
                   (FPS, w, h, x, y, ROIS_W, hh),
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    rc, out, _err = run_cmd(cmd, timeout=300, cwd=str(workdir), binary=True)
    if rc != 0 or not out:
        return None
    frame = ROIS_W * hh
    n = len(out) // frame
    if n < 4:
        return None
    return np.frombuffer(out[:n * frame], dtype=np.uint8).reshape(n, hh, ROIS_W)


def _yunet_model():
    """cv2.FaceDetectorYN или None (модель вшита в репо рядом с модулем)."""
    import cv2
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (here, os.path.join(os.path.dirname(here)),
                 os.path.join(os.path.dirname(here), "clipper")):
        p = os.path.join(cand, YUNET_FILE)
        if os.path.isfile(p):
            return cv2.FaceDetectorYN.create(p, "", (0, 0), YUNET_SCORE)
    return None


def _probe_frames(video_path, workdir, cam_box, n_frames=10, fps=0.5):
    """Пробные кадры окна камеры: (массив N x H x W gray, (hh, cw))."""
    import numpy as np
    cx, cy, cw, ch = [int(round(float(v))) for v in cam_box]
    cw = max(8, min(cw, 1920))
    ch = max(8, min(ch, 1080))
    hh = max(8, int(round(FACE_PROBE_W * ch / float(cw) / 2.0)) * 2)
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", os.path.basename(str(video_path)),
            "-vf", "fps=%.2f,crop=%d:%d:%d:%d,scale=%d:%d" %
                   (fps, cw, ch, cx, cy, FACE_PROBE_W, hh),
            "-frames:v", str(n_frames), "-f", "rawvideo", "-pix_fmt", "gray",
            "pipe:1"])
    rc, out, _err = run_cmd(cmd, timeout=120, cwd=str(workdir), binary=True)
    frame = FACE_PROBE_W * hh
    n = len(out) // frame if rc == 0 else 0
    if n < 3:
        return None
    return (np.frombuffer(out[:n * frame], dtype=np.uint8).reshape(n, hh, FACE_PROBE_W),
            (hh, cw))


def _median_face(faces_px):
    """Медианный bbox списка (x, y, w, h) в координатах пробного кадра."""
    import numpy as np
    fx = float(np.median([f[0] for f in faces_px]))
    fy = float(np.median([f[1] for f in faces_px]))
    fw = float(np.median([f[2] for f in faces_px]))
    fh = float(np.median([f[3] for f in faces_px]))
    return fx, fy, fw, fh


def _find_mouth_roi(video_path, workdir, cam_box, dur):
    """(x, y, w, h) области рта в координатах клипа или None.

    Лицо ищем YuNet (cv2.FaceDetectorYN, ONNX вшит в репо — находит лицо в
    2-3 раза надёжнее старого Хаара: прод-кейс daxak 2026-11-22 — Хаар молчал,
    YuNet дал лицо с score 0.95); если YuNet недоступен — Хаар; если и он
    молчит — геометрический фолбэк: рот в нижней части окна камеры (лицо по
    построению webcam.py сидит в его верхней трети).
    """
    import numpy as np
    probe = None
    try:
        probe = _probe_frames(video_path, workdir, cam_box)
    except Exception:
        log.debug("Пробные кадры окна камеры не читаются", exc_info=True)
    if probe is None:
        return None
    frames, (hh, cw) = probe
    k = cw / float(FACE_PROBE_W)   # координаты пробного кадра -> окно камеры
    faces_px = []
    try:
        import cv2
        # --- 1) YuNet: главный детектор (модель вшита, оффлайн) --------------
        try:
            det = _yunet_model()
            if det is not None:
                for img in frames:
                    big = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
                    det.setInputSize((big.shape[1], big.shape[0]))
                    _n, fnd = det.detect(big)
                    if fnd is None:
                        continue
                    # строки: x, y, w, h, landmarks..., score
                    best = max(fnd, key=lambda f: float(f[14]))
                    if float(best[14]) < YUNET_SCORE:
                        continue
                    faces_px.append((float(best[0]), float(best[1]),
                                     float(best[2]), float(best[3])))
                if len(faces_px) >= FACE_MIN_VOTES:
                    fx, fy, fw, fh = _median_face(faces_px)
                    fx, fy, fw, fh = fx * k, fy * k, fw * k, fh * k
                    if fw >= 12 and fh >= 12:
                        mx = fx + fw * (1.0 - MOUTH_W_FRAC) / 2.0
                        my = fy + fh * MOUTH_Y0_FRAC
                        cx0, cy0 = int(round(float(cam_box[0]))), int(round(float(cam_box[1])))
                        roi = (cx0 + mx, cy0 + my, fw * MOUTH_W_FRAC, fh * MOUTH_H_FRAC)
                        log.info("Лип-синк: лицо найдено YuNet (%d/%d кадров, "
                                 "ROI рта %.0fx%.0f)", len(faces_px), len(frames),
                                 roi[2], roi[3])
                        return roi
        except Exception:
            log.debug("YuNet недоступен — фолбэк на Хаар", exc_info=True)
        # --- 2) Хаар: старый фолбэк ------------------------------------------
        faces_px = []
        cascade_cls = getattr(cv2, "CascadeClassifier", None)
        if cascade_cls is not None:
            cascade = cascade_cls(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if not cascade.empty():
                for img in frames:
                    f2 = cv2.resize(img, (img.shape[1] * 2, img.shape[0] * 2),
                                    interpolation=cv2.INTER_CUBIC)
                    found = cascade.detectMultiScale(f2, scaleFactor=1.1,
                                                     minNeighbors=4,
                                                     minSize=(24, 24))
                    try:
                        fl = [(int(a), int(b), int(c), int(d))
                              for (a, b, c, d) in found]
                    except TypeError:
                        fl = []
                    if fl:
                        fl.sort(key=lambda r: r[2] * r[3], reverse=True)
                        x, y, fw2, fh2 = fl[0]
                        faces_px.append((x / 2.0, y / 2.0, fw2 / 2.0, fh2 / 2.0))
                if len(faces_px) >= FACE_MIN_VOTES:
                    fx, fy, fw, fh = _median_face(faces_px)
                    fx, fy, fw, fh = fx * k, fy * k, fw * k, fh * k
                    if fw >= 12 and fh >= 12:
                        mx = fx + fw * (1.0 - MOUTH_W_FRAC) / 2.0
                        my = fy + fh * MOUTH_Y0_FRAC
                        cx0, cy0 = int(round(float(cam_box[0]))), int(round(float(cam_box[1])))
                        roi = (cx0 + mx, cy0 + my, fw * MOUTH_W_FRAC, fh * MOUTH_H_FRAC)
                        log.info("Лип-синк: рот найден Хааром (%d/%d кадров): "
                                 "%.0fx%.0f в окне камеры", len(faces_px), len(frames),
                                 roi[2], roi[3])
                        return roi
    except Exception:
        log.debug("Поиск лица для лип-синка не удался", exc_info=True)
    # геометрический фолбэк: лицо — в верхней трети окна камеры
    # (webcam.py строит окно с запасом на голову и плечи)
    cx, cy, cw, ch = [float(v) for v in cam_box]
    face_h = ch * 0.45
    roi = (cx + cw * 0.28, cy + ch * 0.12 + face_h * MOUTH_Y0_FRAC,
           cw * 0.44, face_h * MOUTH_H_FRAC)
    global _re_log
    if not _re_log:
        log.info("Лип-синк: лицо Хааром не поймано — рот по геометрии окна "
                 "камеры (%.0fx%.0f)", roi[2], roi[3])
        _re_log = True
    return roi


def speaking_intervals(video_path, workdir, cam_box=None):
    """[(start, end)] — когда стример РЕАЛЬНО говорит (губы двигаются).

    None — лицо/камеру найти не удалось: гейт не применяем. Тайминги —
    в секундах ОТ НАЧАЛА КЛИПА (субтитры строятся по этому же клипу).
    """
    import numpy as np
    video_path = str(video_path)
    meta = ffprobe_meta(video_path)
    dur = float(meta.get("duration") or 0)
    if dur < 3.0:
        return None
    if cam_box is None:
        try:
            try:
                from webcam import detect_cam_box
            except ImportError:                       # плоский репозиторий
                from webcam import detect_cam_box
            cam_box = detect_cam_box(video_path, str(workdir), weak=True)
        except Exception:
            log.exception("Окно камеры для лип-синка не нашлось")
            cam_box = None
    if not cam_box:
        log.info("Лип-синк: камеры нет — субтитры без гейта по губам")
        return None

    roi = _find_mouth_roi(video_path, str(workdir), cam_box, dur)
    if not roi:
        return None
    frames = _extract_roi_stream(video_path, str(workdir), roi, dur)
    if frames is None:
        log.info("Лип-синк: кадры рта не прочитались — субтитры без гейта")
        return None

    # --- энергия речи: разница соседних кадров в области рта -----------------
    d = np.abs(frames[1:].astype(np.float32) - frames[:-1].astype(np.float32))
    e = d.mean(axis=(1, 2))                          # энергия между сэмплами
    if e.size < 4:
        return None
    # скользящее среднее (3 сэмпла = 0.5 c) — дрожание камеры не всплески
    if e.size >= 3:
        kern = np.ones(3) / 3.0
        e_s = np.convolve(e, kern, mode="same")
    else:
        e_s = e
    med = float(np.median(e_s))
    thr = max(THR_ABS, THR_K * med)
    log.info("Лип-синк: энергия рта медиана %.2f, порог %.2f (%d сэмплов)",
             med, thr, e_s.size)

    # --- гистерезис: речь вкл/выкл с инерцией --------------------------------
    on = e_s > thr
    intervals = []
    start = None
    off_run = 0
    step = 1.0 / FPS
    for i, v in enumerate(on):
        t0 = i * step
        if v:
            if start is None:
                start = max(0.0, t0 - PAD)
            off_run = 0
        else:
            off_run += 1
            if start is not None and off_run >= 2:
                intervals.append([start, max(start + 0.4, t0 + PAD)])
                start = None
                off_run = 0
    if start is not None:
        intervals.append([start, min(dur, (len(on) - 1) * step + PAD)])

    # --- слияние коротких пауз + выброс микроподёргиваний --------------------
    merged = []
    for iv in intervals:
        if merged and iv[0] - merged[-1][1] <= MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], iv[1])
        else:
            merged.append(list(iv))
    out = []
    for s, t in merged:
        if t - s >= MIN_INTERVAL:
            out.append((round(max(0.0, s), 2), round(min(dur, t), 2)))
    speech = sum(t - s for s, t in out)
    log.info("Лип-синк: стример говорит %.1f c из %.1f (%d интервалов)",
             speech, dur, len(out))
    if not out:
        # рот не двигался ВЕСЬ клип: либо фейковое «лицо», либо тишина —
        # честный гейт «всё молчание» рискован, оставляем субтитры как есть
        return None
    return out
