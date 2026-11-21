"""Субтитры: распознавание речи (faster-whisper) + стили ASS.

СУБТИТРЫ — ТОЛЬКО ИЗ РЕЧИ ЧЕЛОВЕКА (Whisper слышит голос и игнорирует чат).

Стили:
  tiktok  — караоке БЕЗ анимаций: ЖИРНЫЙ КАПС двумя строками ВНИЗУ ЭКРАНА.
            Ещё НЕ произнесённые слова — БЕЛЫЕ, текущее и уже сказанное —
            ЖЁЛТЫЕ; подсветка строго по таймингам слов (субтитры «следуют»
            за говорящим, без вспышек и пружинок);
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

YELLOW = "&H00FFFF&"    # подсветка сказанного (караоке «бело-жёлтые»)

# Строка держится ~1 c после последнего слова — субтитры не «мигают» на
# паузах между фразами; но не висят дольше начала следующей строки и не
# мелькают короче 0.4 c.
CHUNK_TAIL = 1.0
MIN_LINE_SEC = 0.4


def _karaoke_line(durs, caps, idxs):
    """Строка с караоке-подсветкой \\k — БЕЗ каких-либо анимаций.

    В ASS подсветка \\k накапливается от начала строки, поэтому длительность
    слота слова i = w_i.start - w_(i-1).start: слово i «желтеет» РОВНО в
    момент, когда его произносят (тайминги слов от Whisper). Пробел остаётся
    в слоте ПРЕДЫДУЩЕГО слова — пробел не рисуется, подсветка не смещается.

    До подсветки слово рисуется SecondaryColour (белый), после —
    PrimaryColour (жёлтый): несказанное — белое, сказанное — жёлтое.
    """
    parts = []
    for j, idx in enumerate(idxs):
        lead = "" if j == 0 else " "
        parts.append("%s{\\k%d}%s" % (lead, durs[idx], caps[idx]))
    return "".join(parts)


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


def _lip_gate(words, segs, lip_intervals):
    """Гейт по губам: оставить только слова, где стример реально говорит.

    lip_intervals — [(start, end)] от lipsync.speaking_intervals (движение
    рта). Слово сохраняется при пересечении с любым интервалом (поле 0.35 c).
    Возвращает (слова, сегменты) или (None, None), если гейт вырезал бы
    слишком много честной речи (лицо детектировалось рывками) — тогда
    субтитры остаются без гейта, лучше лишняя строка, чем потерянная.
    """
    pad = 0.35
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


def build_tiktok_ass(words, path, width=1280, height=720, font="Arial Black",
                     skip_intervals=None):
    """Караоке-субтитры БЕЗ анимаций: жирный капс ВНИЗУ ПО ЦЕНТРУ, максимум
    две строки, палитра строго бело-жёлтая.

    СЛЕДУЕТ ЗА ГОВОРЯЩИМ (запрос пользователя):
      * строка появляется РОВНО в момент первого слова и держится ~1 c после
        последнего (не дольше начала следующей строки — без наложений);
      * подсветка через караоке-теги \\k по таймингам слов Whisper: слово
        «желтеет» ровно в момент произнесения и остаётся жёлтым; ещё НЕ
        произнесённые слова — белые. Никаких пружинок/вспышек (\\t, \\fad,
        \\fscx) — только чистая смена цвета, в том числе на паузах;
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
        primary=YELLOW, secondary=WHITE,
        outline="&H00141414", back="&H80000000", bold=-1,
        border_style=1, outline_w=3, shadow=0,
        marginv=int((330 if vertical else 88)
                    * ((height / 1920.0) if vertical else scale)),
    )
    chunks = _chunks(words)
    events = []
    for i, chunk in enumerate(chunks):
        start = max(chunk[0]["start"], 0.0)
        last_end = max(chunk[-1]["end"], start + 0.2)
        end = last_end + CHUNK_TAIL
        if i + 1 < len(chunks):
            nxt = chunks[i + 1][0]["start"]
            if nxt > last_end:
                # держим строку до самой следующей — субтитры без «миганий»
                end = min(end, nxt - 0.05)
            else:
                end = last_end + 0.1      # тайминги пересеклись — не висеть
        if end - start < MIN_LINE_SEC:
            end = start + MIN_LINE_SEC
        if _overlaps(start, end, skip_intervals):
            continue
        caps = [_esc(w["word"].upper()) for w in chunk]
        # караоке-длительности: слово i желтеет в момент w_i.start
        durs = []
        prev = start
        for w in chunk:
            t = max(w["start"], start)
            durs.append(max(0, int(round((t - prev) * 100.0))))
            prev = t
        if durs:
            # строка появляется ровно на первом слове — оно сразу «текущее»
            durs[0] = max(durs[0], 1)
        if len(caps) == 1:
            text = "{\\k%d}%s" % (durs[0], caps[0])
        else:
            k = _balanced_split(caps)      # верх/низ — ровно по длине
            text = (_karaoke_line(durs, caps, range(0, k))
                    + "\\N"
                    + _karaoke_line(durs, caps, range(k, len(caps))))
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},TikTok,,0,0,0,,{text}")
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

    # --- ЛИП-СИНК: субтитры только там, где стример говорит по губам ----------
    # Жалоба пользователя: «субтитры произносятся, когда стример молчит» —
    # Whisper тянет речь из игры/войсчата. Гейт по движению рта
    # (clipper/lipsync.py); если лицо видно рывками — гейт не применяем.
    if lip_intervals and words:
        try:
            gated_w, gated_s = _lip_gate(words, segs, lip_intervals)
        except Exception:
            log.exception("Лип-гейт упал — субтитры без него")
            gated_w, gated_s = None, None
        if gated_w is not None:
            words, segs = gated_w, gated_s
            smeta["lip_gate"] = True
        else:
            smeta["lip_gate"] = False

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
