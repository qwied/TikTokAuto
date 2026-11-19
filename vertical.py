"""Вертикальный формат 9:16 (1080x1920) — как в TikTok.

Клип по умолчанию готовится ВЕРТИКАЛЬНЫМ: исходный кадр стрима (16:9)
уменьшается и ставится по центру вертикального полотна 1080x1920,
сверху и снизу — размытое продолжение этого же кадра (классика TikTok).
Субтитры ложатся в нижнюю зону, баннер RubUP — по центру кадра.

Режимы (config.yaml -> clip.vertical_mode):
  blur  — стрим по центру во всю ширину, фон — размытое видео (по умолчанию);
  crop  — без фона: центральная часть кадра растягивается на весь 9:16
          (края теряются, зато картинка занимает весь экран).

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
                  mode="blur", blur=26, vcodec_args=None, mask_boxes=None):
    """Перевести клип в вертикальный формат 9:16.

    Принимает собранный сырой клип (ts/mp4), возвращает путь к вертикальному
    mp4. Длительность и звук сохраняются; звук перекодируется в AAC
    (совместимо с TikTok). Выход: h264 + yuv420p + faststart.

    mask_boxes — прямоугольники [(x, y, w, h)] в координатах ГОТОВОГО
    вертикального кадра: чужие банеры/виджеты, найденные overlays.py.
    Замазываются boxblur-ом в ТОЙ ЖЕ цепочке фильтров (без лишнего
    перекодирования). Баннер RubUp рисуется позже — его замазка не трогает.
    """
    video_in = os.path.basename(str(video_in))
    video_out = os.path.basename(str(video_out))
    va = list(vcodec_args) if vcodec_args else \
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    graph = blur_graph(width, height, blur=blur) \
        if str(mode or "blur").lower() != "crop" else crop_graph(width, height)
    out_label = "v"
    if mask_boxes:
        try:
            try:
                from overlays import blur_fragment
            except ImportError:                       # плоский репозиторий
                from overlays import blur_fragment
            frag, out_label = blur_fragment(list(mask_boxes), "v")
            if frag:
                graph += ";" + frag
                log.info("9:16 с замазкой: %d зон чужих оверлеев", len(mask_boxes))
        except Exception:
            log.exception("Фрагмент замазки не собрался — клип без неё")
            graph = blur_graph(width, height, blur=blur) \
                if str(mode or "blur").lower() != "crop" else crop_graph(width, height)
            out_label = "v"
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
             float(meta.get("duration") or 0), mode)
    return os.path.join(str(workdir), video_out)
