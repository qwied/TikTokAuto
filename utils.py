"""Общие утилиты StreamClipper: логи, конфиг, запуск внешних команд."""

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time


def setup_logging(level=logging.INFO):
    """Единый формат логов для всех модулей."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_config(path):
    """Загрузка YAML-конфига (импорт yaml ленивый — модуль легче)."""
    import yaml  # noqa: PLC0415

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"Конфиг {path} пуст или имеет неверный формат")
    return cfg


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def run_cmd(args, timeout=60, cwd=None):
    """Запуск внешней команды.

    Возвращает (returncode, stdout, stderr) в utf-8.
    """
    try:
        p = subprocess.run(
            [str(a) for a in args],
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
        )
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        return p.returncode, out, err
    except FileNotFoundError:
        return 127, "", f"Не найдена команда: {args[0]} (установите её и добавьте в PATH)"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def now_str(fmt="%Y-%m-%d_%H-%M-%S"):
    return time.strftime(fmt)


def human_size(n):
    """Размер файла в человекочитаемом виде."""
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"


def local_ip():
    """Локальный IP (для ссылок на сервер клипов в LAN)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def sanitize(name):
    """Безопасное имя файла/папки из произвольной строки (кириллица сохраняется)."""
    clean = re.sub(r"[^\w\-. ]+", "_", str(name), flags=re.UNICODE).strip("_ .")
    return (clean[:40] or "channel")


def ffprobe_meta(path):
    """Ширина/высота/длительность видео через ffprobe."""
    rc, out, err = run_cmd(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,duration", "-of", "json", str(path)],
        timeout=30,
    )
    if rc != 0:
        return {}
    try:
        st = json.loads(out)["streams"][0]
        return {
            "width": int(st.get("width") or 0),
            "height": int(st.get("height") or 0),
            "duration": float(st.get("duration") or 0),
        }
    except (KeyError, IndexError, ValueError, TypeError):
        return {}


def check_binaries():
    """Проверка внешних программ. Список строк с отсутствующими."""
    missing = []
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg — установите: winget install Gyan.FFmpeg (или с ffmpeg.org, добавить в PATH)")
    if shutil.which("ffprobe") is None:
        missing.append("ffprobe — ставится вместе с ffmpeg")
    return missing


# --- авто-выбор кодировщика видео (скорость финальной обработки) ---------------------

ENCODERS = {
    "nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "19", "-b:v", "0"],
    "qsv": ["-c:v", "h264_qsv", "-global_quality", "20", "-preset", "faster"],
    "amf": ["-c:v", "h264_amf", "-quality", "speed"],
    "x264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"],
}

_encoder_cache = None
_encoder_lock = threading.Lock()


def detect_h264_encoder(choice="auto", force=False):
    """Подобрать кодировщик H.264, который реально работает на этом ПК.

    Порядок для auto: аппаратные NVENC -> QSV -> AMF, затем программный x264.
    Каждый кандидат проверяется коротким тестовым кодированием (0.3 с видео).
    Возвращает (имя, список-аргументов ffmpeg для -vcodec). Результат кешируется.
    """
    global _encoder_cache
    choice = str(choice or "auto").lower()
    order = [choice] if choice in ENCODERS else ["nvenc", "qsv", "amf", "x264"]
    with _encoder_lock:
        if _encoder_cache is not None and not force:
            if choice == "auto" or _encoder_cache[0] == choice:
                return _encoder_cache
    for name in order:
        args = ENCODERS[name]
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
                "-f", "lavfi", "-i", "color=black:s=320x320:d=0.3", "-frames:v", "3"]
               + args + ["-f", "null", "-"])
        rc, _out, _err = run_cmd(cmd, timeout=60)
        if rc == 0:
            log_once = logging.getLogger("encoder")
            label = {"nvenc": "NVIDIA NVENC (видеокарта)", "qsv": "Intel QSV (встроенная графика)",
                     "amf": "AMD AMF (видеокарта)", "x264": "программный x264 (CPU)"}[name]
            log_once.info("Кодировщик финальной обработки: %s", label)
            with _encoder_lock:
                _encoder_cache = (name, list(args))
            return _encoder_cache
    with _encoder_lock:
        _encoder_cache = ("x264", list(ENCODERS["x264"]))
    return _encoder_cache
