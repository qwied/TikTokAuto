"""Субтитры: распознавание речи (faster-whisper) + стили ASS.

СУБТИТРЫ — ТОЛЬКО ИЗ РЕЧИ ЧЕЛОВЕКА (Whisper слышит голос и игнорирует чат).

Стили:
  tiktok  — как на образце заказчика: ЖИРНЫЙ КАПС двумя строками ВНИЗУ ЭКРАНА,
            верхняя строка белая, нижняя — жёлтая (современный «viral»-стиль);
  classic — аккуратные строки с полупрозрачной плашкой;
  чат     — опциональный fallback (по умолчанию ВЫКЛЮЧЕН: чат не отслеживается).
"""

import logging
import os
import threading
import zlib

from utils import ffprobe_meta, run_cmd

log = logging.getLogger("subtitle")

_model = None
_model_name = None
_model_lock = threading.Lock()

# палитра подсветки активного слова и ников (формат ASS: &HBBGGRR&)
# современная неоновая палитра активного слова: горячий розовый -> сирень -> циан
MODERN_PALETTE = ["&H9B2DFF&", "&HFA8BA7&", "&HEED322&"]
ACCENT = MODERN_PALETTE[0]    # горячий розовый #FF2D9B
WHITE = "&HFFFFFF&"
PALETTE = ["&H00D7FF&", "&H00A5FF&", "&HFFFF00&", "&HFF00FF&",
           "&H7FFF00&", "&HEBCE87&", "&H4763FF&"]

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {name},{font},{fontsize},{primary},{secondary},{outline},{back},{bold},0,0,0,100,100,0,0,{border_style},{outline_w},{shadow},2,60,60,{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


class SubtitleUnavailable(Exception):
    """Распознавание речи недоступно (не установлено/ошибка)."""


def whisper_available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _get_model(size):
    global _model, _model_name
    with _model_lock:
        if _model is None or _model_name != size:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            log.info("Загружаю модель Whisper «%s» (при первом запуске скачается)", size)
            _model = WhisperModel(size, device="cpu", compute_type="int8")
            _model_name = size
        return _model


def transcribe(wav_path, language="ru", model_size="small", beam_size=1):
    """Речь -> [(сегменты), (слова с таймингами)].

    beam_size=1 — самый быстрый режим распознавания (точность small-модели
    при этом остаётся высокой); можно поднять в config.yaml (subtitle.beam_size).
    """
    segs, words, _meta = transcribe_full(wav_path, language, model_size, beam_size)
    return segs, words


def transcribe_full(wav_path, language="ru", model_size="small", beam_size=1,
                    vad=True):
    """Речь -> (сегменты, слова, мета {язык, слов, символов, секунд речи}).

    Мягкий VAD: стандартный агрессивно вырезает речь на фоне шума игры и
    музыки — из-за этого «стример говорит, а субтитров нет». Здесь пороги
    смягчены (длинные паузы разрешены, речь с запасом по краям).
    """
    if not whisper_available():
        raise SubtitleUnavailable("faster-whisper не установлен")
    model = _get_model(model_size)
    lang = None if (not language or str(language).lower() == "auto") else str(language)
    kw = {"language": lang, "word_timestamps": True,
          "beam_size": max(1, int(beam_size or 1)),
          "condition_on_previous_text": False,
          # Мягкие пороги «немой речи»: значения по умолчанию (0.6 / -1.0)
          # выкидывали сегменты, где стример говорит на фоне шума игры —
          # «стример говорит, а субтитров нет». Снижаем оба порога:
          # сегмент выбраковывается только при ЯВНО отсутствующей речи.
          "no_speech_threshold": 0.35,
          "log_prob_threshold": -0.9}
    if vad:
        kw["vad_filter"] = True
        kw["vad_parameters"] = {"min_silence_duration_ms": 700,
                                "speech_pad_ms": 250,
                                "threshold": 0.30}
    else:
        kw["vad_filter"] = False
    gen, info = model.transcribe(str(wav_path), **kw)
    segs, words = [], []
    for s in gen:
        segs.append({"start": s.start, "end": s.end, "text": (s.text or "").strip()})
        for w in (s.words or []):
            word = (w.word or "").strip()
            if word:
                words.append({"start": w.start, "end": w.end, "word": word})
    lang_seen = getattr(info, "language", None)
    speech_s = 0.0
    for s in segs:
        if s["text"]:
            speech_s += max(0.0, s["end"] - s["start"])
    meta = {"lang": lang_seen or "?", "words": len(words),
            "chars": sum(len(w["word"]) for w in words),
            "speech_sec": round(speech_s, 1)}
    log.info("Whisper: язык=%s, слов=%d, речи %.1f c", meta["lang"],
             meta["words"], meta["speech_sec"])
    return segs, words, meta


MIN_CHARS = 25          # меньше символов за клип = «распознавание не удалось»


# Мат маскируется в «тексте для TikTok» (публичное описание ролика)
PROFANITY_STEMS = [
    "бляд", "хуй", "хуе", "хуё", "пизд", "еба", "ёба", "еби", "ёби", "ебу",
    "сука", "суки", "мудак", "гандон", "гондон", "шлюх", "долбо", "пидор",
    "пидар", "пидр", "залуп", "заёб", "заеб", "охуе", "ахуе", "хер",
    "fuck", "shit", "bitch", "whore", "cunt", "asshole", "nigg",
]


def _mask_profanity(text):
    """Слова с матерными корнями -> «***» (описание ролика всё равно публичное)."""
    out = []
    for word in str(text or "").split():
        low = word.lower()
        if any(stem in low for stem in PROFANITY_STEMS):
            out.append("***")
        else:
            out.append(word)
    return " ".join(out)


def _hook_from_words(words, max_chars=90):
    """Первая фраза речи — «крючок» для описания ролика в TikTok.

    Из первых распознанных слов собирается короткая цитата стримера: живая
    подача работает в описании лучше шаблонного текста. Мат маскируется.
    """
    text = " ".join(str(w.get("word") or "").strip()
                    for w in (words or [])[:14]).strip()
    text = _mask_profanity(text)
    if len(text) > max_chars:
        cut = text.rfind(" ", 0, max_chars)
        text = text[:cut if cut > 30 else max_chars].rstrip(",;:-")
    return text.strip()


def transcribe_smart(wav_path, language="ru", model_size="small", beam_size=1,
                     workdir=None):
    """Надёжное распознавание с ЛЕСТНИЦЕЙ ПОПЫТОК.

    Субтитры пропадали в трёх случаях: 1) мягкий VAD сглатывал тихую речь
    на фоне шума игры; 2) язык был жёстко «ru», а стример говорил по-английски
    (автопоиск берёт и топ-зарубежные эфиры) — Whisper выдавал пустоту или
    кашу; 3) звук был тихий/грязный. Теперь:

      попытка 1 — заданный язык, beam 1, мягкий VAD (быстрый путь);
      попытка 2 — beam 5 (медленнее и точнее) тем же языком;
      попытка 3 — громкость нормализуется (loudnorm) и язык АВТО:
                  Whisper сам определяет язык по голосу.

    Возвращает (сегменты, слова, мета). Мета -> сайт и уведомление показывают,
    какие субтитры получились (язык, слов, % речи), а «нет речи» теперь
    отличается от «распознать не удалось».
    """
    attempts = []
    wav = str(wav_path)
    # Нормализация ОДИН раз в начале: тихие и шумные эфиры распознаются
    # одинаково хорошо уже с ПЕРВОЙ попытки (раньше громкость выравнивалась
    # только на третьей попытке — две попытки уходили впустую на тихих клипах).
    norm = normalize_audio(wav, str(workdir)) if workdir else None
    fast_src = norm or wav
    try:
        attempts.append(("язык=%s, beam 1" % (language or "авто"),
                         transcribe_full(fast_src, language, model_size, beam_size, vad=True)))
        if attempts[-1][1][2]["chars"] >= MIN_CHARS:
            return attempts[-1][1]
        attempts.append(("язык=%s, beam 5" % (language or "авто"),
                         transcribe_full(fast_src, language, model_size, 5, vad=True)))
        if attempts[-1][1][2]["chars"] >= MIN_CHARS:
            return attempts[-1][1]
        if norm:
            # совсем тихо: пробуем БЕЗ VAD — вдруг фильтр съел всю речь
            attempts.append(("нормализованный звук, язык=авто, без VAD",
                             transcribe_full(norm, None, model_size, 5, vad=False)))
            if attempts[-1][1][2]["chars"] > 0:
                return attempts[-1][1]
        else:
            attempts.append(("язык=авто, beam 5, без VAD",
                             transcribe_full(wav, None, model_size, 5, vad=False)))
            if attempts[-1][1][2]["chars"] > 0:
                return attempts[-1][1]
        return attempts[-1][1]
    except SubtitleUnavailable:
        raise
    except Exception:
        log.exception("Распознавание не удалось, пробую последнюю попытку без VAD")
        return transcribe_full(wav, None, model_size, 5, vad=False)


def normalize_audio(wav_path, workdir):
    """Громкость -> единый уровень (loudnorm): тихие и шумные клипы
    распознаются одинаково хорошо. Возвращает путь к wav или None."""
    out = os.path.join(str(workdir), "clip_audio_norm.wav")
    rc, _o, err = run_cmd(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
         "-i", os.path.basename(str(wav_path)),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
         "-ac", "1", "-ar", "16000", os.path.basename(out)],
        timeout=300, cwd=str(workdir))
    if rc != 0 or not os.path.exists(out):
        log.warning("Нормализация звука не удалась: %s", err[-200:])
        return None
    return out


def _words_from_segments(segs):
    """Синтез таймингов слов из сегментов (если Whisper не дал word timestamps)."""
    words = []
    for s in segs:
        parts = s["text"].split()
        if not parts:
            continue
        dur = max(s["end"] - s["start"], 0.2)
        step = dur / len(parts)
        for i, p in enumerate(parts):
            words.append({"start": s["start"] + i * step,
                          "end": s["start"] + (i + 1) * step,
                          "word": p})
    return words


# --- ASS ---------------------------------------------------------------------

def _ass_time(t):
    t = max(float(t), 0.0)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text):
    """Экранирование служебных символов ASS."""
    return str(text).replace("\\", "/").replace("{", "(").replace("}", ")")


def _chunks(words, max_words=4, max_chars=22):
    out, cur = [], []
    for w in words:
        cur.append(w)
        chars = sum(len(x["word"]) for x in cur)
        if len(cur) >= max_words or chars >= max_chars:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


YELLOW = "&H00FFFF&"    # нижняя строка, как на образце заказчика


def _overlaps(a0, a1, intervals, pad=0.15):
    """Пересекается ли отрезок [a0, a1] с каким-то интервалом паузы."""
    for s, e in (intervals or []):
        if a0 < e + pad and a1 > s - pad:
            return True
    return False


def build_tiktok_ass(words, path, width=1280, height=720, font="Arial Black",
                     skip_intervals=None):
    """Стиль «как на образце»: жирный капс ВНИЗУ ПО ЦЕНТРУ, максимум две
    строки — верхняя БЕЛАЯ, нижняя ЖЁЛТАЯ (современный viral-стиль).

    Вертикальный клип (9:16) — субтитры крупнее и выше от низа, чтобы не
    перекрывались интерфейсом TikTok.

    skip_intervals — интервалы показа рекламного баннера [(начало, конец)]:
    пока нарезка «на паузе», звук стрима молчит, поэтому субтитры на это
    время не показываются совсем.
    """
    vertical = height > width
    scale = (width / 1080.0) if vertical else (height / 720.0)
    header = ASS_HEADER.format(
        width=width, height=height, name="TikTok", font=font,
        fontsize=int((64 if vertical else 58) * scale),
        primary=WHITE, secondary=WHITE,
        outline="&H00141414", back="&H80000000", bold=-1,
        border_style=1, outline_w=3, shadow=0,
        marginv=int((330 if vertical else 88)
                    * ((height / 1920.0) if vertical else scale)),
    )
    events = []
    for chunk in _chunks(words):
        start = max(chunk[0]["start"], 0.0)
        end = max(chunk[-1]["end"], start + 0.7)
        if end - start < 0.7:
            end = start + 0.7
        if _overlaps(start, end, skip_intervals):
            continue
        caps = [_esc(w["word"].upper()) for w in chunk]
        if len(caps) == 1:
            text = "{\\c" + YELLOW + "}" + caps[0]
        else:
            k = (len(caps) + 1) // 2          # верхняя строка — чуть больше слов
            top = " ".join(caps[:k])
            bottom = " ".join(caps[k:])
            text = ("{\\c" + WHITE + "}" + top
                    + "{\\c" + YELLOW + "}\\N" + bottom)
        lead = "{\\fad(70,70)}"
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},TikTok,,0,0,0,,{lead}{text}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
    return path


def build_classic_ass(segs, path, width=1280, height=720, font="Arial"):
    """Классика: строки снизу на полупрозрачной плашке."""
    vertical = height > width
    scale = (width / 1080.0) if vertical else (height / 720.0)
    header = ASS_HEADER.format(
        width=width, height=height, name="Classic", font=font,
        fontsize=int((44 if vertical else 38) * scale),
        primary=WHITE, secondary=WHITE,
        outline="&HA0000000", back="&H50000000", bold=-1,
        border_style=3, outline_w=4, shadow=0,
        marginv=int((300 if vertical else 36)
                    * ((height / 1920.0) if vertical else scale)),
    )
    events = []
    for s in segs:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        text = _esc(text)
        if len(text) > 46:
            mid = text.rfind(" ", 0, 46)
            mid = mid if mid > 0 else 46
            text = text[:mid].rstrip() + "\\N" + text[mid:].lstrip()
        events.append(
            f"Dialogue: 0,{_ass_time(s['start'])},{_ass_time(s['end'])},Classic,,0,0,0,,"
            f"{{\\fad(120,120)}}{text}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
    return path


def build_chat_ass(messages, duration, path, width=1280, height=720, font="Arial"):
    """Fallback: сообщения чата как анимированные субтитры."""
    vertical = height > width
    scale = (width / 1080.0) if vertical else (height / 720.0)
    msgs = [m for m in messages if m.text.strip()][:12]
    if not msgs:
        return None
    header = ASS_HEADER.format(
        width=width, height=height, name="Chat", font=font,
        fontsize=int((46 if vertical else 42) * scale),
        primary=WHITE, secondary=WHITE,
        outline="&H00141414", back="&H80000000", bold=-1,
        border_style=1, outline_w=3, shadow=1,
        marginv=int((380 if vertical else 150)
                    * ((height / 1920.0) if vertical else scale)),
    )
    n = len(msgs)
    seg = max(duration / n, 1.2)
    t = 0.4
    events = []
    for m in msgs:
        color = PALETTE[zlib.crc32(m.user.encode("utf-8")) % len(PALETTE)]
        start = t
        end = min(start + 1.6, max(duration, 1.0))
        text = ("{\\c" + color + "}" + _esc(m.user) + ": {\\c" + WHITE + "}"
                + _esc(m.text[:70]))
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Chat,,0,0,0,,"
            f"{{\\fad(80,140)}}{text}")
        t += seg
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
    return path


# --- ffmpeg -------------------------------------------------------------------

def extract_audio(video, workdir):
    """16 kHz mono wav для Whisper (пути относительные — безопасно для Windows)."""
    wav = os.path.join(str(workdir), "clip_audio.wav")
    rc, _out, err = run_cmd(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
         "-i", os.path.basename(str(video)),
         "-vn", "-ac", "1", "-ar", "16000", os.path.basename(wav)],
        timeout=300, cwd=str(workdir))
    if rc != 0:
        raise RuntimeError("ffmpeg (извлечение аудио): " + err[-400:])
    return wav


def burn_subtitles(video_in, ass_path, video_out, workdir, vcodec_args=None):
    """Вшить ASS-субтитры в видео (libass) — финальный mp4.

    vcodec_args — аргументы видеокодировщика (авто-детект NVENC/QSV/x264 из
    utils.detect_h264_encoder). Звук копируется без перекодирования (быстро
    и без потери качества); при несовместимом кодеке — перекодирование в AAC.
    """
    va = list(vcodec_args) if vcodec_args else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    head = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
            "-i", os.path.basename(str(video_in)),
            "-vf", f"ass={os.path.basename(str(ass_path))}"]
    tail = ["-pix_fmt", "yuv420p", "-movflags", "+faststart",
            os.path.basename(str(video_out))]
    last_err = ""
    for audio in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "192k"]):
        rc, _out, err = run_cmd(head + va + audio + tail,
                                timeout=900, cwd=str(workdir))
        if rc == 0:
            return video_out
        last_err = err
        if audio[1] == "copy":
            log.warning("Субтитры: звук не скопировался, перекодирую в AAC")
    raise RuntimeError("ffmpeg (субтитры): " + last_err[-400:])


def make_subtitles(video_path, workdir, sub_cfg, language, chat_messages,
                   duration=60.0, skip_intervals=None):
    """Полный конвейер субтитров для клипа. Возвращает путь к .ass или None.

    СУБТИТРЫ ТОЛЬКО ИЗ РЕЧИ ЧЕЛОВЕКА: Whisper с мягким VAD выписывает
    только отрезки, где человек реально говорит; чат по умолчанию НЕ
    используется (fallback_chat: false в config.yaml).

    skip_intervals — интервалы показа рекламного баннера: нарезка в это
    время «на паузе», звук стрима молчит — субтитры там не нужны.
    """
    ass, _meta = make_subtitles_meta(video_path, workdir, sub_cfg, language,
                                     chat_messages, duration=duration,
                                     skip_intervals=skip_intervals)
    return ass


def make_subtitles_meta(video_path, workdir, sub_cfg, language, chat_messages,
                        duration=60.0, skip_intervals=None):
    """То же, что make_subtitles, но возвращает (путь_к_.ass, мета_речи).

    мета = {"lang": "ru", "words": 56, "speech_pct": 62, "ok": True|False} —
    идёт на сайт (бейдж на карточке клипа) и в уведомление: сразу видно,
    что субтитры реально получились, а не «в моменте просто не говорили».
    """
    sub_cfg = sub_cfg or {}
    smeta = {"lang": "", "words": 0, "speech_pct": 0, "ok": False}
    if not sub_cfg.get("enabled", True):
        return None, smeta
    width, height = 1280, 720
    meta_ff = ffprobe_meta(video_path)
    if meta_ff.get("width") and meta_ff.get("height"):
        width, height = meta_ff["width"], meta_ff["height"]
    ass_path = os.path.join(str(workdir), "subs.ass")
    style = str(sub_cfg.get("style", "tiktok")).lower()
    font = sub_cfg.get("font") or ("Arial Black" if style == "tiktok" else "Arial")

    segs, words = None, None
    if sub_cfg.get("use_asr", True):
        try:
            wav = extract_audio(video_path, workdir)
            segs, words, smeta = transcribe_smart(
                wav, language, sub_cfg.get("model", "small"),
                beam_size=sub_cfg.get("beam_size", 1), workdir=str(workdir))
            for f in (wav, os.path.join(str(workdir), "clip_audio_norm.wav")):
                try:
                    os.remove(f)
                except OSError:
                    pass
        except Exception as e:
            log.warning("Распознавание речи недоступно: %s", e)
            segs, words = None, None

    if skip_intervals:
        if words:
            words = [w for w in words
                     if not _overlaps(w["start"], w["end"], skip_intervals)]
        if segs:
            segs = [s for s in segs
                    if not _overlaps(s["start"], s["end"], skip_intervals)]

    if not words and segs:
        segs = [s for s in segs if (s.get("text") or "").strip()]
        words = _words_from_segments(segs)

    dur_s = max(float(duration or 60.0), 1.0)
    smeta["words"] = max(smeta.get("words") or 0, len(words or []))
    smeta["speech_pct"] = min(100, round(100.0 * float(smeta.get("speech_sec") or 0.0)
                                         / dur_s))
    smeta["ok"] = bool(words)
    if smeta.get("lang"):
        smeta["lang"] = str(smeta["lang"]).lower()[:2]
    if words:
        # «крючок» для описания TikTok: первая фраза стримера из клипа
        try:
            smeta["hook"] = _hook_from_words(words)
        except Exception:
            smeta["hook"] = ""

    if words:
        if style == "classic":
            build_classic_ass(segs or [], ass_path, width=width, height=height, font=font)
        else:
            build_tiktok_ass(words, ass_path, width=width, height=height, font=font,
                             skip_intervals=skip_intervals)
        log.info("Субтитры из речи: %d слов (язык %s, речи %d%%), стиль %s",
                 len(words), smeta.get("lang") or "?", smeta["speech_pct"], style)
        return ass_path, smeta

    if sub_cfg.get("fallback_chat", False) and chat_messages:
        built = build_chat_ass(chat_messages, duration, ass_path,
                               width=width, height=height, font=font)
        if built:
            log.info("Субтитры собраны из сообщений чата (%d шт.)", len(chat_messages))
            smeta["ok"] = True
            smeta["lang"] = smeta["lang"] or "chat"
            return ass_path, smeta

    log.info("Субтитров нет: речь в этом клипе не распознана "
             "(язык %s, слов %d)", smeta.get("lang") or "?", smeta["words"])
    return None, smeta
