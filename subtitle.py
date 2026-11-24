"""Субтитры: распознавание речи (faster-whisper) + стили ASS.

СУБТИТРЫ — ТОЛЬКО ИЗ РЕЧИ ЧЕЛОВЕКА (Whisper слышит голос и игнорирует чат).

Стили:
  tiktok  — караоке БЕЗ анимаций: ЖИРНЫЙ КАПС двумя строками ВНИЗУ ЭКРАНА.
            ЖЁЛТОЕ слово — РОВНО ТО, которое стример произносит сейчас;
            остальные слова — БЕЛЫЕ. Подход «одно событие на словный
            интервал» (взят из репо auto-caption, MIT): тайминг каждого
            события = интервал слова из Whisper, поэтому субтитры не могут
            появиться раньше речи и не «залагивают» — подсветка сменяется
            в момент произнесения, без вспышек и пружинок;
  classic — аккуратные строки с полупрозрачной плашкой;
  чат     — опциональный fallback (по умолчанию ВЫКЛЮЧЕН: чат не отслеживается).
"""

import logging
import os
import re
import shutil
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


_AV_PATCHED = False


_PROMPT_TOKENS_RE = re.compile(r"[а-яёa-z]+")


def _drop_prompt_echo(collected, prompt):
    """Убрать сегменты, где Whisper «спарротил» initial_prompt.

    На паузах модель иногда повторяет текст подсказки как «распознанную»
    речь (стрим — разреженная речь, классическая ловушка). Сегмент, чьи
    слова в основном совпадают со словами подсказки, мусор — вырезаем.
    Возвращает (коллекция, сколько вырезано).
    """
    if not prompt:
        return collected, 0
    ptoks = set(_PROMPT_TOKENS_RE.findall(str(prompt).lower()))
    if not ptoks:
        return collected, 0
    out, dropped = [], 0
    for seg, words in collected:
        stoks = set(_PROMPT_TOKENS_RE.findall(str(seg.get("text") or "").lower()))
        if stoks and len(stoks & ptoks) / len(stoks) >= 0.6:
            dropped += 1
            continue
        out.append((seg, words))
    return out, dropped


def _patch_av_open():
    """Совместимость faster-whisper с новыми PyAV.

    PyAV 15+ убрал аргумент metadata_errors из av.open, а faster-whisper
    продолжает его передавать — распознавание падало с TypeError и
    «субтитры не пишутся». Шим молча выбрасывает неизвестный аргумент,
    если новая версия PyAV на него заругалась. Старые версии PyAV не
    затрагиваются вообще.
    """
    global _AV_PATCHED
    if _AV_PATCHED:
        return
    _AV_PATCHED = True
    try:
        import av

        orig = av.open
        if getattr(orig, "_sc_compat", False):
            return

        def _open(*args, **kwargs):
            try:
                return orig(*args, **kwargs)
            except TypeError as e:
                if "metadata_errors" in str(e) and "metadata_errors" in kwargs:
                    kwargs = {k: v for k, v in kwargs.items()
                              if k != "metadata_errors"}
                    return orig(*args, **kwargs)
                raise

        _open._sc_compat = True
        av.open = _open
        log.info("PyAV %s: шим совместимости av.open включён", av.__version__)
    except Exception:
        log.debug("PyAV шим не потребовался/не установился", exc_info=True)


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
    Плюс: initial_prompt настраивает модель на стримскую лексику, а
    «галлюцинации» Whisper на паузах/музыке (шаблонные фразы-мусор)
    вырезаются до сборки субтитров.
    """
    if not whisper_available():
        raise SubtitleUnavailable("faster-whisper не установлен")
    _patch_av_open()
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
    if (lang or "").lower() == "ru":
        # Подсказка модели: стримская лексика — меньше ошибок на сленге,
        # донатах и игровых названиях (субтитры точнее, «мусора» меньше).
        kw["initial_prompt"] = ("Стрим, чат, донат, подписка, привет, "
                                "погнали, го, жесть, клип, лол, ахах, "
                                "брат, игра, момент.")
    if vad:
        kw["vad_filter"] = True
        kw["vad_parameters"] = {"min_silence_duration_ms": 700,
                                # подушка речи 250 мс сдвигала начало строк:
                                # субтитры появлялись РАНЬШЕ, чем стример
                                # заговорил. 100 мс — слово начинается ровно
                                # в момент речи (запрос пользователя)
                                "speech_pad_ms": 100,
                                "threshold": 0.30}
    else:
        kw["vad_filter"] = False
    gen, info = model.transcribe(str(wav_path), **kw)
    collected = []          # [(seg, [words])] — фильтруем ДО сборки результата
    for s in gen:
        seg = {"start": s.start, "end": s.end, "text": (s.text or "").strip()}
        swords = []
        for w in (s.words or []):
            word = (w.word or "").strip()
            if word:
                swords.append({"start": w.start, "end": w.end, "word": word})
        collected.append((seg, swords))
    # --- эхо initial_prompt (модель повторила подсказку вместо речи) --------
    collected, echo_n = _drop_prompt_echo(collected, kw.get("initial_prompt"))
    if echo_n:
        log.info("Эхо подсказки Whisper: %d сегментов вырезано", echo_n)
    # --- фильтр галлюцинаций: мусорные фразы на паузах/музыке не в субтитры ---
    segs, words = [], []
    dropped = 0
    for seg, swords in collected:
        if _is_hallucination(seg["text"]):
            dropped += 1
            continue
        segs.append(seg)
        words.extend(swords)
    if dropped:
        log.info("Галлюцинации Whisper: %d мусорных сегментов вырезано", dropped)
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


# --- «Галлюцинации» Whisper: мусорные фразы на паузах и музыке -----------------
# Модель на тишине/шуме любит выдавать шаблонные строки («Субтитры делал…»,
# «Продолжение следует», «Музыка», amara.org и т.п.) — в субтитрах им НЕ место.
HALLUCINATION_EXACT = {
    "...", "..", ".", "музыка", "музыка.", "аплодисменты", "музыка играет",
    "звучит музыка", "играет музыка", "инструментальная музыка",
    "смотреть позже", "подписаться", "подписаться на канал",
    "спасибо за просмотр", "спасибо за просмотр!", "продолжение следует",
    "до встречи в новом видео", "пока", "пока!", "bye", "bye.", "byebye",
    "thanks for watching", "thank you for watching", "like and subscribe",
    "subscribe", "hello everyone", "amara.org", "amaraorg",
    "subtitles by", "субтитры делал dimatorzok", "делал dimatorzok",
    "субтитры делал", "мандаринки", "mandarincas", "мандаринка",
    "он", "она", "i", "you", "м", "а",
}
HALLUCINATION_PREFIXES = (
    "субтитры делал", "продолжение следует", "спасибо за просмотр",
    "подписывайтесь", "субтитры: ", "subtitles by", "amara.org",
    "смотрите также", "перевод: ", "звук: ",
    # титры/творческая группа — Whisper любит «узнать» их в шуме эфира
    "редактор субтитров", "корректор", "оформление", "монтаж",
    "генеральный продюсер", "исполнительный продюсер", "в титрах",
)


def _is_hallucination(text):
    """True — строка почти наверняка мусор от модели, а не речь стримера."""
    t = str(text or "").strip().lower()
    t = t.strip("♪♫ .,!?;:«»\"'()[]-—_~")
    if not t:
        return True
    if t in HALLUCINATION_EXACT:
        return True
    if any(t.startswith(p) for p in HALLUCINATION_PREFIXES):
        return True
    # меньше двух буквенных символов — не речь
    if sum(1 for ch in t if ch.isalpha()) < 2:
        return True
    return False


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

    Защита от мусора: попытка 3 (язык АВТО без VAD) верит шуму игры —
    музыка/взрывы «распознаются» как английские фразы. Поэтому авто-попытка
    принимается, только если язык совпал с ожидаемым (или пользователь сам
    просил авто) — иначе возвращаем результат попытки 1 (честные пустые
    субтитры: «в моменте не было речи» лучше выдуманных строк).
    """
    attempts = []
    wav = str(wav_path)
    req_lang = str(language or "").strip().lower() or None

    def _auto_ok(meta):
        return (req_lang is None
                or str(meta.get("lang") or "").lower() == req_lang)

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
            if attempts[-1][1][2]["chars"] > 0 and _auto_ok(attempts[-1][1][2]):
                return attempts[-1][1]
        else:
            attempts.append(("язык=авто, beam 5, без VAD",
                             transcribe_full(wav, None, model_size, 5, vad=False)))
            if attempts[-1][1][2]["chars"] > 0 and _auto_ok(attempts[-1][1][2]):
                return attempts[-1][1]
        # ни одна попытка не дала честных субтитров — возвращаем первую
        # (мета честно покажет «мало речи», клип выйдет без субтитров)
        return attempts[0][1]
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


# --- АЛИГНМЕНТ ПО РЕЧИ (Silero VAD): тайминги Whisper -> фактическая речь -----
# Исследование прод-клипов 2026-11-22 (dmitry_lixxx 08:12). Простой
# энергетический порог «доказывает» систематический лаг ~0.3 c, но он ловит
# звуки ИГРЫ/музыки, а не речь. Против специализированного детектора речи
# Silero (вшит в faster-whisper — веса в пакете, никаких загрузок и новых
# зависимостей) старты строк Whisper сходятся с медианой −10 мс и MAE ~150 мс.
# Поэтому правка ТОЧЕЧНАЯ (подход whisperX — github.com/m-bain/whisperX —
# без его 3 ГБ зависимостей):
#   * границы сегментов, ошибшиеся на ±0.2-0.6 c, стягиваются к ФАКТИЧЕСКИМ
#     границам речевых интервалов (слова сегмента сдвигаются той же дельтой —
#     внутренний ритм Whisper сохраняется);
#   * слова, попавшие в тишину (галлюцинации на паузах), вырезаются;
#   * слово после настоящей паузы привязывается к началу нового интервала речи.
# Ошибка любого шага не фатальна: субтитры остаются с таймингами Whisper.

ALIGN_MAX_SHIFT = 0.65      # максимум сдвига границы сегмента, сек
ALIGN_NEAR = 0.65           # старт сегмента «рядом» с границей речи — правим
ALIGN_MIN_GAP_WORD = 0.22   # пауза перед словом, с которой слово привязывается к онсету
ALIGN_VAD_THRESHOLD = 0.35  # мягкий порог Silero (стримеры говорят тихо на фоне игры)
ALIGN_MIN_WORD_COVER = 0.35  # слово вырезается, если речью покрыто <35% его длины
                             # (реальные слова на границах интервалов могут
                             # наполовину лежать в тишине — вырезаем только
                             # явные галлюцинации, висящие в полной тишине)


def _silero_intervals(wav_path, workdir=None, threshold=None):
    """Фактические интервалы РЕЧИ клипа по Silero VAD (репо snakers4/silero-vad).

    Возвращает [(start, end)] в секундах или None (не получилось).
    Веса Silero вшиты в пакет faster-whisper (get_assets_path) — работает
    оффлайн и на Actions.
    """
    try:
        import numpy as np
        from faster_whisper.vad import (get_speech_timestamps, get_vad_model,
                                        VadOptions)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", os.path.basename(str(wav_path)),
               "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"]
        rc, out, _err = run_cmd(cmd, timeout=180,
                                cwd=str(workdir) if workdir else None,
                                binary=True)
        if rc != 0 or len(out) < 32000:
            log.warning("Silero VAD: аудио не прочиталось (rc=%s, %d байт)",
                        rc, len(out) if rc == 0 else 0)
            return None
        audio = np.frombuffer(out[:len(out) // 4 * 4], dtype=np.float32)
        model = get_vad_model()
        opts = VadOptions(threshold=float(threshold or ALIGN_VAD_THRESHOLD),
                          min_silence_duration_ms=250, speech_pad_ms=0)
        ts = get_speech_timestamps(audio, opts, sampling_rate=16000)
        iv = sorted((x["start"] / 16000.0, x["end"] / 16000.0) for x in ts)
        iv = [(s, e) for s, e in iv if e - s >= 0.08]
        return iv or None
    except Exception:
        log.warning("Silero VAD не удался", exc_info=True)
        return None


def refine_word_timings(wav_path, segs, words, workdir=None):
    """Стянуть тайминги Whisper к фактическим границам речи (Silero VAD).

    Возвращает (сегменты, слова, статистика). Любая ошибка -> вход как есть.
    В stats["speech"] лежат интервалы речи — на них опирается fail-closed
    гейт в make_subtitles_meta: без подтверждённой речи субтитров нет.
    """
    iv = _silero_intervals(wav_path, workdir)
    if not iv:
        # перебор порогов: шумный стрим может «топить» мягкий порог —
        # пробуем строже (подход silero-vad README: threshold 0.35..0.7)
        for thr in (0.55, 0.7):
            iv = _silero_intervals(wav_path, workdir, threshold=thr)
            if iv:
                log.info("Silero VAD: интервалы найдены на строгом пороге %.2f",
                         thr)
                break
    if not iv:
        return segs, words, {}

    def speech_cover(t0, t1):
        """Доля отрезка [t0, t1], покрытая интервалами речи."""
        if t1 <= t0:
            return 0.0
        c = 0.0
        for s, e in iv:
            c += max(0.0, min(e, t1) - max(s, t0))
        return c / (t1 - t0)

    # --- 1) сегменты: старт -> старт ближайшего интервала речи ---------------
    new_segs = []
    for seg in segs or []:
        s0 = float(seg.get("start") or 0.0)
        best = None
        for s, e in iv:
            d = abs(s - s0)
            # правим только ошибку на границе фразы (<=ALIGN_NEAR): глубоко
            # внутри длинного интервала сегмент не трогаем — это продолжение
            if d <= ALIGN_NEAR:
                # при почти равной дистанции берём РАННЮЮ границу: начало
                # речи почти всегда раньше/на месте, Whisper опаздывает чаще
                rank = d - (0.02 if s <= s0 else 0.0)
                if best is None or rank < best[1]:
                    best = (s, rank)
        n0 = best[0] if best else s0
        n0 = max(0.0, n0)
        new_segs.append(dict(seg, start=n0))
    deltas = [ns["start"] - float(os.get("start") or 0.0)
              for ns, os in zip(new_segs, segs or [])]
    # --- 2) слова: сдвиг с сегментом; в тишине — вырезать --------------------
    new_words = []
    for w in (words or []):
        w0 = float(w.get("start") or 0.0)
        w1 = float(w.get("end") or w0 + 0.05)
        bj, bov = None, None
        for j, seg in enumerate(new_segs):
            ov = min(seg["end"], w1) - max(seg["start"], w0)
            if bov is None or ov > bov:
                bj, bov = j, ov
        d = deltas[bj] if bj is not None and bov and bov > 0 else 0.0
        n0, n1 = max(0.0, w0 + d), w1 + d
        if speech_cover(n0, n1) < ALIGN_MIN_WORD_COVER:
            continue                      # галлюцинация в тишине — мусор
        new_words.append(dict(w, start=n0, end=max(n0 + 0.04, n1)))
    # --- 3) слово после настоящей паузы -> к началу нового интервала речи ----
    snapped = 0
    prev_end = 0.0
    for w in new_words:
        gap = w["start"] - prev_end
        prev_end = max(prev_end, float(w.get("end") or w["start"]))
        if gap < ALIGN_MIN_GAP_WORD:
            continue
        for s, e in iv:
            w_end = float(w.get("end") or w["start"] + 0.2)
            if (abs(s - w["start"]) <= 0.35 and s >= prev_end - 0.05
                    and s <= w_end - 0.04):
                w["start"] = s
                snapped += 1
                break
    new_words.sort(key=lambda w: float(w.get("start") or 0.0))
    stats = {"intervals": len(iv), "dropped": len(words or []) - len(new_words),
             "snapped": snapped, "speech": iv}
    log.info("Алигнмент по речи (Silero): интервалов %d, вырезано слов %d, "
             "привязано к онсету %d", len(iv), stats["dropped"], snapped)
    return new_segs, new_words, stats


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


# Шрифт субтитров: Montserrat ExtraBold — жирный «TikTok»-шрифт с полной
# кириллицей. Файл Montserrat-ExtraBold.ttf лежит рядом с программой и
# КОПИРУЕТСЯ в рабочую папку при вшивании (fontsdir в ffmpeg ass-фильтре):
# надпись выглядит ОДИНАКОВО везде — на Windows и на GitHub Actions (раньше
# Actions брали «Arial Black» из config, шрифта там нет, libass подставлял
# тонкий дефолтный — субтитры выглядели «кривыми»).
TIKTOK_FONT = "Montserrat ExtraBold"
FONT_FILENAMES = ("Montserrat-ExtraBold.ttf",)

YELLOW = "&H00FFFF&"    # подсветка слова, которое произносят СЕЙЧАС

# Хвост строки после последнего слова — короткий: субтитры не висят в
# тишине (жалоба пользователя «появляются раньше, чем надо» — из-за
# длинного хвоста строка продолжала висеть, когда речь уже кончилась).
CHUNK_TAIL = 0.35
MIN_LINE_SEC = 0.3


def _chunks(words, max_words=4, max_chars=22, gap_break=0.6):
    """Группировка слов в короткие строки (подход auto-caption, MIT).

    Разрывы строк: >max_words слов, >max_chars символов, ПАУЗА МЕЖДУ СЛОВАМИ
    больше gap_break секунд, конец фразы (знак препинания). Раньше строка
    клеилась через длинную паузу: линия появлялась на первом слове и ВИСЕЛА
    белой всю тишину до второго — субтитры «появлялись заранее».
    """
    out, cur = [], []
    for w in words:
        if cur:
            chars = sum(len(x["word"]) for x in cur) + 1 + len(w["word"])
            gap = float(w.get("start") or 0.0) - float(cur[-1].get("end") or 0.0)
            ends_phrase = str(cur[-1]["word"]).rstrip().endswith(
                (".", "!", "?", "…"))
            if (len(cur) >= max_words or chars > max_chars
                    or gap > gap_break or ends_phrase):
                out.append(cur)
                cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _snap_gaps(run):
    """Выравнивание интервалов слов внутри строки (auto-caption, MIT).

    Конец слова продлевается до НАЧАЛА следующего (минимум 0.02 c):
    событие текущего слова заканчивается ровно там, где начинается
    событие следующего — подсветка не «моргает» на микропаузах, и в любой
    момент времени активен РОВНО ОДИН событийный интервал.
    """
    snapped = []
    n = len(run)
    for i, w in enumerate(run):
        s = float(w.get("start") or 0.0)
        e = float(w.get("end") or 0.0)
        if e <= s:
            e = s + 0.05
        if i + 1 < n:
            nxt = float(run[i + 1].get("start") or 0.0)
            e = max(nxt, s + 0.02)
        snapped.append({"word": w["word"], "start": s, "end": e})
    return snapped


def _balanced_split(caps):
    """Делим chunk на две строки ПО ДЛИНЕ СИМВОЛОВ — строки выходят ровными,
    без «кривых» переносов (раньше делили пополам по словам)."""
    best, bestdiff = 1, None
    for k in range(1, len(caps)):
        diff = abs(len(" ".join(caps[:k])) - len(" ".join(caps[k:])))
        if bestdiff is None or diff < bestdiff:
            best, bestdiff = k, diff
    return best


def _overlaps(a0, a1, intervals, pad=0.15):
    """Пересекается ли отрезок [a0, a1] с каким-то интервалом паузы."""
    for s, e in (intervals or []):
        if a0 < e + pad and a1 > s - pad:
            return True
    return False


def _lip_gate(words, segs, lip_intervals, speech_iv=None):
    """Гейт по губам: оставить только слова, где стример реально говорит.

    lip_intervals — [(start, end)] от lipsync.speaking_intervals (движение
    рта, детектор лица YuNet). speech_iv — интервалы речи по Silero (аудио).
    ДВА независимых детектора речи сверяются между собой (арбитраж):
      * если губы «говорят» меньше половины аудио-речи — детект рта сломан
        (тёмное лицо, рывки камеры; прод-инцидент exigent_wires) — гейт НЕ
        применяется, субтитры остаются по аудио;
      * иначе гейт ЖЁСТКИЙ: слово сохраняется только при пересечении с
        интервалом движения рта (поле 0.35 c) — субтитры следят ИМЕННО за
        губами стримера, реплики из игры/войсчата вырезаются.
    Возвращает (слова, сегменты) или (None, None), если гейт вырезал бы
    слишком много честной речи — тогда субтитры остаются без гейта.
    """
    pad = 0.35
    if speech_iv:
        lip_s = sum(e - s for s, e in lip_intervals)
        aud_s = sum(e - s for s, e in speech_iv)
        inter = sum(min(e, ve) - max(s, vs)
                    for s, e in lip_intervals for vs, ve in speech_iv
                    if min(e, ve) > max(s, vs))
        cover = inter / max(lip_s, 0.01)      # губы подтверждены аудио
        recall = inter / max(aud_s, 0.01)     # аудио-речь покрыта губами
        log.info("Лип-синк vs аудио (арбитраж): губы %.1f c, аудио %.1f c, "
                 "пересечение %.1f c (точность %.0f%%, полнота %.0f%%)",
                 lip_s, aud_s, inter, 100 * cover, 100 * recall)
        # губы молчат там, где аудио явно речь (>= половина) — детект сломан
        if recall < 0.50:
            log.warning("Лип-детект покрывает только %.0f%% аудио-речи — рот "
                        "не виден, гейт по губам НЕ применяю", 100 * recall)
            return None, None
        # губы «говорят» втрое больше аудио — ROI рта ловит не то движение
        if lip_s > 3.0 * aud_s and cover < 0.45:
            log.warning("Лип-детект дал %.1f c «говорения» при %.1f c речи по "
                        "аудио — ROI рта неверный, гейт НЕ применяю", lip_s, aud_s)
            return None, None
    keep_w = [w for w in (words or [])
              if _overlaps(w["start"], w["end"], lip_intervals, pad=pad)]
    keep_s = [s for s in (segs or [])
              if _overlaps(s["start"], s["end"], lip_intervals, pad=pad)]
    n0, n1 = len(words or []), len(keep_w)
    if n0 and n1 < max(3, int(0.30 * n0)):
        log.warning("Лип-синк вырезал бы %d из %d слов — лицо видно рывками, "
                    "гейт не применяю", n0 - n1, n0)
        return None, None
    log.info("Лип-синк: оставлено %d/%d слов (стример молчал в остальное время)",
             n1, n0)
    return keep_w, keep_s


def _speech_cap(t, speech_intervals, tail=0.35):
    """Предел хвоста события: конец речевого интервала, содержащего t, +tail.

    Гарантирует «субтитры исчезают сразу после речи» (жалоба «появлялись
    заранее/висели в тишине»): строка не может висеть дольше конца речи,
    где бы Whisper ни поставил свои тайминги.
    """
    if not speech_intervals:
        return None
    best = None
    for s, e in speech_intervals:
        if s - 0.15 <= t <= e + 0.15:
            best = e if best is None else max(best, e)
    if best is None:
        return t
    return best + tail


def build_tiktok_ass(words, path, width=1280, height=720, font="Arial Black",
                     skip_intervals=None, speech_intervals=None):
    """Караоке-субтитры БЕЗ анимаций: жирный капс ВНИЗУ ПО ЦЕНТРУ, максимум
    две строки, палитра строго бело-жёлтая.

    ПОДХОД «ОДНО СОБЫТИЕ НА СЛОВНЫЙ ИНТЕРВАЛ» (репо auto-caption, MIT) —
    решение проблемы «субтитры появляются заранее / отстают»:
      * для КАЖДОГО слова создаётся своё Dialogue-событие с интервалом
        [слово.start; следующее слово.start] — тайминг берётся НЕПОСРЕДСТВЕННО
        из словных таймингов Whisper, группового времени больше нет;
      * в событии активное слово окрашено в жёлтый (\\c-тег), остальные слова
        белые: ЖЁЛТОЕ = РОВНО ТО СЛОВО, КОТОРОЕ СТРИМЕР ГОВОРИТ СЕЙЧАС,
        сказанное возвращается в белый;
      * строка появляется РОВНО в момент первого слова и исчезает сразу
        после последнего (короткий хвост 0.35 c на читаемость);
      * строки рвутся на паузах >0.6 c и по концу фразы — субтитры не висят
        в тишине;
      * хвост строки, кроме прочего, обрезается по концу речевого интервала
        (Silero VAD, speech_intervals): субтитры не могут висеть в паузе
        дольше 0.35 c после речи, даже если тайминги Whisper ошиблись;
      * никаких \\k / \\t / \\fad / \\fscx — чистая смена цвета, ноль анимаций;
      * деление на верх/низ — по длине символов, строки ровные.

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
    chunks = _chunks(words)
    # Чанки, попавшие на паузу баннера, делим на ПОКАЗЫ: слова до паузы и
    # после показываются отдельными строками (раньше строка, задевшая паузу,
    # выбрасывалась ЦЕЛИКОМ — субтитры «редко появлялись»). Слова, звучащие
    # внутри паузы (звук стрима там молчит), выбрасываются совсем.
    runs = []
    for chunk in chunks:
        cur = []
        for w in chunk:
            if _overlaps(w["start"], w["end"], skip_intervals, pad=0.0):
                if cur:
                    runs.append(cur)
                cur = []
            else:
                cur.append(w)
        if cur:
            runs.append(cur)
    events = []
    for i, run in enumerate(runs):
        snapped = _snap_gaps(run)
        start = max(snapped[0]["start"], 0.0)
        last_end = snapped[-1]["end"]
        for j, w in enumerate(snapped):
            s = max(float(w["start"]), 0.0)
            e = float(w["end"])
            if j == len(snapped) - 1:
                # Хвост на читаемость; ОБЯЗАТЕЛЬНО обрезаем по началу
                # следующего показа и по паузе баннера: раньше хвост залезал
                # в следующую строку, если та начиналась сразу после — два
                # события на экране одновременно, libass расталкивал их
                # «стопкой» (двойные субтитры в кадре).
                e = max(last_end + CHUNK_TAIL, start + MIN_LINE_SEC)
                # Хвост обрезается и по КОНЦУ РЕЧИ (Silero): строка не висит
                # в тишине дольше 0.35 c после последнего слова.
                cap = _speech_cap(last_end, speech_intervals)
                if cap is not None and cap > s + MIN_LINE_SEC:
                    e = min(e, cap)
                caps_e = []
                if i + 1 < len(runs):
                    caps_e.append(max(float(runs[i + 1][0]["start"]), 0.0))
                for s0, _e0 in (skip_intervals or []):
                    if float(s0) > last_end - 0.01:
                        caps_e.append(float(s0))
                for cap in caps_e:
                    if cap > s + 0.02 and e > cap - 0.05:
                        e = cap - 0.05
                if e <= s:
                    e = s + 0.02
            if e <= s:
                continue
            # ТЕКСТ СОБЫТИЯ: слово j — жёлтое, остальные — белые.
            # Активное слово может стоять в любой из двух строк —
            # раскраска не зависит от переноса.
            caps = [_esc(w2["word"].upper()) for w2 in snapped]
            k = _balanced_split(caps)          # верх/низ — ровно по длине
            rows = []
            for lo, hi in ((0, k), (k, len(caps))):
                parts = []
                for idx in range(lo, hi):
                    if idx == j:
                        parts.append("{\\c%s}%s{\\c%s}"
                                     % (YELLOW, caps[idx], WHITE))
                    else:
                        parts.append(caps[idx])
                rows.append(" ".join(parts))
            text = "\\N".join(r for r in rows if r)
            events.append(
                f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},TikTok,,0,0,0,,{text}")
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


def _bundled_fonts_dir():
    """Где лежит вшитый шрифт: рядом с модулем (прод-раскладка, плоский
    репо — Montserrat-ExtraBold.ttf в корне), в fonts/ рядом или на уровень
    выше (раскладка пакета clipper/), либо в корне проекта (исходник,
    модуль в clipper/). None — шрифта рядом нет (не ошибка)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (here, os.path.join(here, "fonts"),
                 os.path.join(os.path.dirname(here), "fonts"),
                 os.path.dirname(here),
                 os.path.dirname(os.path.dirname(here))):
        if any(os.path.isfile(os.path.join(cand, n)) for n in FONT_FILENAMES):
            return cand
    return None


def _stage_fonts(workdir):
    """Скопировать вшитые шрифты в workdir/fonts -> fontsdir для libass.

    Возвращает 'fonts' (относительное имя папки, ffmpeg запускается с
    cwd=workdir) или None. Копирование — чтобы не зависеть от того, где
    лежит проект и как экранировать абсолютные пути в фильтре ffmpeg.
    """
    src_dir = _bundled_fonts_dir()
    if not src_dir:
        return None
    dst_dir = os.path.join(str(workdir), "fonts")
    try:
        os.makedirs(dst_dir, exist_ok=True)
        copied = 0
        for name in FONT_FILENAMES:
            src = os.path.join(src_dir, name)
            dst = os.path.join(dst_dir, name)
            if os.path.exists(src) and (not os.path.exists(dst)
                                        or os.path.getsize(dst)
                                        != os.path.getsize(src)):
                shutil.copyfile(src, dst)
            copied += 1 if os.path.exists(dst) else 0
        if copied:
            return "fonts"
    except Exception:
        log.debug("Шрифты не скопировались", exc_info=True)
    return None


def burn_subtitles(video_in, ass_path, video_out, workdir, vcodec_args=None):
    """Вшить ASS-субтитры в видео (libass) — финальный mp4.

    vcodec_args — аргументы видеокодировщика (авто-детект NVENC/QSV/x264 из
    utils.detect_h264_encoder). Звук копируется без перекодирования (быстро
    и без потери качества); при несовместимом кодеке — перекодирование в AAC.

    Вшитый шрифт (fonts/, Montserrat ExtraBold): копируется в workdir и
    подключается через fontsdir — одинаковый жирный вид субтитров на любой
    машине (Windows/GitHub Actions), без подмены системным шрифтом.
    """
    va = list(vcodec_args) if vcodec_args else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    vf = f"ass={os.path.basename(str(ass_path))}"
    fonts_rel = _stage_fonts(workdir)
    if fonts_rel:
        vf += f":fontsdir={fonts_rel}"
    head = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
            "-i", os.path.basename(str(video_in)),
            "-vf", vf]
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
                   duration=60.0, skip_intervals=None, lip_intervals=None):
    """Полный конвейер субтитров для клипа. Возвращает путь к .ass или None.

    СУБТИТРЫ ТОЛЬКО ИЗ РЕЧИ ЧЕЛОВЕКА: Whisper с мягким VAD выписывает
    только отрезки, где человек реально говорит; чат по умолчанию НЕ
    используется (fallback_chat: false в config.yaml).

    skip_intervals — интервалы показа рекламного баннера: нарезка в это
    время «на паузе», звук стрима молчит — субтитры там не нужны.

    lip_intervals — интервалы говорения СТРИМЕРА по движению губ
    (clipper/lipsync.py): строки, где рот стримера неподвижен (речь из
    игры/войсчата/галлюцинация на паузе), в субтитры не попадают.
    """
    ass, _meta = make_subtitles_meta(video_path, workdir, sub_cfg, language,
                                     chat_messages, duration=duration,
                                     skip_intervals=skip_intervals,
                                     lip_intervals=lip_intervals)
    return ass


def make_subtitles_meta(video_path, workdir, sub_cfg, language, chat_messages,
                        duration=60.0, skip_intervals=None, lip_intervals=None):
    """То же, что make_subtitles, но возвращает (путь_к_.ass, мета_речи).

    мета = {"lang": "ru", "words": 56, "speech_pct": 62, "ok": True|False} —
    идёт на сайт (бейдж на карточке клипа) и в уведомление: сразу видно,
    что субтитры реально получились, а не «в моменте просто не говорили».
    lip_intervals — интервалы говорения по губам (см. lipsync.py).
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
    font = str(sub_cfg.get("font") or "").strip()
    if not font or font.lower() in ("arial black", "default"):
        # «Arial Black» остался в старых конфигах: на GitHub Actions его нет,
        # поэтому заменяем на вшитый жирный шрифт с кириллицей.
        font = TIKTOK_FONT if style == "tiktok" else "Arial"

    segs, words = None, None
    if sub_cfg.get("use_asr", True):
        try:
            wav = extract_audio(video_path, workdir)
            segs, words, smeta = transcribe_smart(
                wav, language, sub_cfg.get("model", "small"),
                beam_size=sub_cfg.get("beam_size", 1), workdir=str(workdir))
        except Exception as e:
            log.warning("Распознавание речи недоступно: %s", e)
            segs, words = None, None
            wav = None

    # --- АЛИГНМЕНТ ПО ЗВУКУ: правка таймингов Whisper по огибающей -----------
    # Whisper ставит слова на ~0.3 c позже реального звука (замер прод-2026-11-
    # 22) — субтитры «залагали». Привязываем сегменты/слова к фактическим
    # интервалам речи; слова в тишине (галлюцинации) вырезаются.
    speech_iv = None
    if wav and words:
        try:
            segs, words, al = refine_word_timings(wav, segs or [],
                                                  words, workdir=str(workdir))
            speech_iv = al.get("speech")
            speech_s = 0.0
            for s in segs or []:
                if s.get("text"):
                    speech_s += max(0.0, float(s["end"]) - float(s["start"]))
            if speech_s > 0:
                smeta["speech_sec"] = round(speech_s, 1)
            smeta["words"] = len(words)
            smeta["aligned"] = True
        except Exception:
            log.exception("Алигнмент по звуку не удался — тайминги Whisper как есть")

    # --- FAIL-CLOSED: без подтверждённой речи субтитров НЕТ ------------------
    # Жалоба пользователя: «либо убери субтитры, либо сделай реально
    # нормальные». Если Silero не смог подтвердить интервалы речи, Whisper-
    # тайминги непроверяемы — строки могут висеть в тишине. Лучше клип без
    # субтитров, чем кривые: они и не строятся вовсе.
    if not speech_iv and words:
        log.warning("Речь не подтверждена VAD — субтитры УБРАНЫ "
                    "(fail-closed: лучше без субтитров, чем кривые)")
        words, segs = [], []

    if wav:
        for f in (wav, os.path.join(str(workdir), "clip_audio_norm.wav")):
            try:
                os.remove(f)
            except OSError:
                pass

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

    # --- ЛИП-СИНК: субтитры только там, где стример говорит по губам ----------
    dur_s = max(float(duration or 60.0), 1.0)
    # Требование пользователя: «субтитры следят ИМЕННО за губами». Whisper
    # тянет речь из игры/войсчата — гейт по движению рта (clipper/lipsync.py,
    # детектор лица YuNet) с арбитражем против аудио-VAD: если детект рта
    # сломан (тёмное лицо, рывки) — гейт сам выключается.
    if lip_intervals and words:
        try:
            gated_w, gated_s = _lip_gate(words, segs, lip_intervals,
                                         speech_iv=speech_iv)
        except Exception:
            log.exception("Лип-гейт упал — субтитры без него")
            gated_w, gated_s = None, None
        if gated_w is not None:
            words, segs = gated_w, gated_s
            smeta["lip_gate"] = True
            lip_s = sum(e - s for s, e in lip_intervals)
            smeta["lip_speech_pct"] = min(100, round(100.0 * lip_s / dur_s))
        else:
            smeta["lip_gate"] = False

    smeta["words"] = len(words or [])   # слова, реально попавшие в субтитры
    smeta["speech_pct"] = min(100, round(100.0 * float(smeta.get("speech_sec") or 0.0)
                                         / dur_s))
    smeta["ok"] = bool(words)
    # --- ТРАНСКРИПТ В ФАЙЛ: его читает ai_caption (LLM-заголовки TikTok) ----
    # Финальный список слов = то, что реально в субтитрах (после лип-гейта
    # и VAD-фильтров): LLM описывает именно то, что зритель увидит в клипе.
    if words:
        try:
            t_txt = " ".join((w.get("word") or "").strip()
                             for w in words if (w.get("word") or "").strip())
            if t_txt:
                with open(os.path.join(str(workdir), "transcript.txt"),
                          "w", encoding="utf-8") as _tf:
                    _tf.write(t_txt)
        except OSError:
            pass
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
                             skip_intervals=skip_intervals,
                             speech_intervals=speech_iv)
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
