# -*- coding: utf-8 -*-
"""Аудио-события «интересности» по звуку: СМЕХ, КРИКИ, аплодисменты (YAMNet).

Взято из открытых клипперов топ-репозиториев (sharkyclips — «громкость +
смех + реакционные слова», laughter-detection jrgillick — смех по аудио):
громкость говорит «здесь ШУМЕЛО», но не «здесь СМЕШНО». Google YAMNet
(AudioSet, 521 класс, Apache-2.0, TFLite 4.1 МБ — лежит в репо) прямо в
звуке находит смех, крики, аплодисменты и толпу — главный сигнал «сочного
момента» для видео БЕЗ чата (обычные YouTube-ролики), где отбор раньше
шёл только по громкости.

Как работает:
  * ffmpeg декодирует звук в моно 16 кГц ПОТОКОМ (память не зависит от
    длины: даже 9-часовая запись не съест ОЗУ);
  * каждый кадр 0.975 с -> YAMNet -> вероятности 521 класса;
  * собираем три кривые ПО СЕКУНДАМ: смех (Laughter/Giggle/Chuckle/
    Snicker/Belly laugh), крики (Shout/Yell/Screaming/Whooping),
    толпа (Applause/Clapping/Cheering/Crowd);
  * тишину пропускаем без нейросети (экономия CPU на паузах).

Fail-open: если модели нет или рантайм не установлен — модуль возвращает
None и нарезка работает как раньше (громкость + чат), ничего не падает.
Скорость: ~60x realtime на 4 ядрах Actions (час видео ~1 мин).
"""
import logging
import os
import subprocess

import numpy as np

log = logging.getLogger("moments")

MODEL_NAME = "yamnet.tflite"
CLASS_MAP = "yamnet_class_map.csv"
SR = 16000

# Классы AudioSet (display_name в yamnet_class_map.csv)
LAUGH_CLASSES = ("Laughter", "Giggle", "Snicker", "Belly laugh",
                 "Chuckle, chortle")
SHOUT_CLASSES = ("Shout", "Whoop", "Yell", "Screaming")
CROWD_CLASSES = ("Cheering", "Applause", "Clapping", "Crowd")

_cache = {}


def _model_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (here, os.path.join(here, "models"),
                 os.path.join(here, "moments")):
        p = os.path.join(cand, MODEL_NAME)
        if os.path.isfile(p):
            return p, os.path.join(os.path.dirname(p), CLASS_MAP)
    return None, None


def _interpreter():
    """TFLite-интерпретатор: ai-edge-litert -> tflite_runtime -> tensorflow."""
    if "interp" in _cache:
        return _cache["interp"]
    mp, cm = _model_path()
    if not mp:
        _cache["interp"] = None
        return None
    interp = None
    try:
        try:
            from ai_edge_litert.interpreter import Interpreter
        except ImportError:
            try:
                from tflite_runtime.interpreter import Interpreter
            except ImportError:
                from tensorflow.lite import Interpreter
        interp = Interpreter(model_path=mp, num_threads=2)
        interp.allocate_tensors()
        _cache["interp"] = interp
        _cache["class_map"] = cm
    except Exception:
        _cache["interp"] = None
    return _cache["interp"]


def available():
    """Модель и рантайм на месте? (для логов и fail-open)."""
    return _interpreter() is not None


def _class_indices(csv_path):
    """Индексы классов смеха/криков/толпы по class_map.csv."""
    idx = {"laugh": [], "shout": [], "appl": []}
    want = {"laugh": LAUGH_CLASSES, "shout": SHOUT_CLASSES,
            "appl": CROWD_CLASSES}
    try:
        import csv
        with open(csv_path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("display_name") or "").strip()
                for key, names in want.items():
                    if name in names:
                        idx[key].append(int(row["index"]))
    except Exception:
        return None
    if not idx["laugh"] or not idx["shout"]:
        return None
    return idx


def _decode_16k(audio_path):
    """ffmpeg -> поток моно 16 кГц (s16le). Возвращает итератор блоков int16."""
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-i", str(audio_path), "-ac", "1", "-ar", str(SR),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc


def event_curves(audio_path):
    """(laugh, shout, appl) — кривые вероятностей ПО СЕКУНДАМ, либо None.

    Длина каждой кривой ~ длительность аудио в секундах (1 значение/с).
    Ошибки -> None (нарезка продолжает по громкости/чату).
    """
    interp = _interpreter()
    if interp is None:
        log.info("YAMNet недоступен (нет модели/рантайма) — только громкость")
        return None
    idx = _class_indices(_cache.get("class_map"))
    if idx is None:
        return None
    try:
        inp = interp.get_input_details()[0]
        out = interp.get_output_details()[0]
        frame_n = int(inp["shape"][-1])            # 15600 сэмплов = 0.975 c
        li, si, ai = (np.array(idx["laugh"]), np.array(idx["shout"]),
                      np.array(idx["appl"]))
        laugh, shout, appl = [], [], []
        carry = np.empty(0, dtype=np.int16)
        proc = _decode_16k(audio_path)
        try:
            while True:
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    break
                take = (len(chunk) // 2) * 2
                data = np.concatenate(
                    [carry, np.frombuffer(chunk[:take], dtype=np.int16)])
                n = data.size // frame_n
                if not n:
                    carry = data
                    continue
                block = data[:n * frame_n].reshape(n, frame_n)
                rms = np.sqrt((block.astype(np.float32) ** 2).mean(axis=1))
                for j in range(n):
                    if rms[j] < 0.004:            # тишина — без нейросети
                        laugh.append(0.0); shout.append(0.0)
                        appl.append(0.0)
                        continue
                    seg = block[j].astype(np.float32) / 32768.0
                    interp.set_tensor(inp["index"], seg)
                    interp.invoke()
                    sc = interp.get_tensor(out["index"]).reshape(-1)
                    laugh.append(float(sc[li].max()))
                    shout.append(float(sc[si].max()))
                    appl.append(float(sc[ai].max()))
                carry = data[n * frame_n:]
            if carry.size >= frame_n // 2:
                seg = carry.astype(np.float32) / 32768.0
                seg = np.pad(seg, (0, frame_n - seg.size))
                interp.set_tensor(inp["index"], seg)
                interp.invoke()
                sc = interp.get_tensor(out["index"]).reshape(-1)
                laugh.append(float(sc[li].max()))
                shout.append(float(sc[si].max()))
                appl.append(float(sc[ai].max()))
        finally:
            proc.terminate()
        laugh = np.asarray(laugh, dtype=np.float64)
        shout = np.asarray(shout, dtype=np.float64)
        appl = np.asarray(appl, dtype=np.float64)
        thr = max(0.05, float(np.median(laugh)) * 6.0)
        log.info("YAMNet: %d с аудио — смех %d с (пик %.2f), крики %d с "
                 "(пик %.2f), толпа %d с (пик %.2f)",
                 laugh.size, int((laugh >= thr).sum()), float(laugh.max()),
                 int((shout >= 0.1).sum()), float(shout.max()),
                 int((appl >= 0.1).sum()), float(appl.max()))
        return laugh, shout, appl
    except Exception:
        log.exception("YAMNet упал — отбор по громкости/чату без событий")
        return None
