# -*- coding: utf-8 -*-
"""AI-заголовки TikTok: локальная нейросеть — БЕСПЛАТНО и БЕЗ КЛЮЧЕЙ.

Что делает:
  * из транскрипта клипа (Whisper пишет transcript.txt в рабочей папке при
    сборке субтитров) LLM придумывает ОДНУ цепляющую русскую фразу —
    заголовок описания TikTok вместо скучного ника канала;
  * нет модели / llama-cpp / транскрипта — запасной план БЕЗ нейросети:
    «выжимка» самой сочной фразы из транскрипта (extractive hook);
  * нет и транскрипта — прежнее поведение (fallback-строка вызывающего).

Модель: Qwen2.5-1.5B-Instruct (GGUF, q4_k_m, ~1.1 ГБ) — крутится локально
на CPU раннера GitHub Actions, скачивается ОДИН раз в кэш ~/.cache/
streamclipper-ai (workflow кэширует папку actions/cache). Никаких внешних
API, никаких ключей, никакого участия пользователя: бесплатно навсегда.

Безопасность TikTok: любая сгенерированная строка проходит маску мата
(как субтитры) и ужимается до 70 знаков. Ошибки никогда не роняют клип —
плоховатый AI-заголовок лучше упавшего workflow.
"""

import logging
import os
import re
import threading

log = logging.getLogger("ai_caption")

DEFAULT_MODEL_NAME = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
MODEL_URL = ("https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/"
             "resolve/main/" + DEFAULT_MODEL_NAME)
CACHE_DIR = os.path.expanduser("~/.cache/streamclipper-ai")
TITLE_MAX = 70                      # TikTok-заголовок: короче читается чаще

_lock = threading.Lock()
_llm = None

# «Взрывные» слова для запасной выжимки (что цепляет зрителя в первых фразах)
_KICK_RE = re.compile(
    r"(смешно|прикол|жесть|ого|вау|вот это|не поверишь|невероятно|нереально|"
    r"шок|капец|блин|лол|ору|ржу|ржач|никогда|серьёзно|серьезно|залип|"
    r"не ожидал|да ладно|жесть|топ)", re.I)
_CYR_RE = re.compile(r"[а-яёА-ЯЁ]")
_JUNK_RE = re.compile(
    r"^(субтитры|смотри до конца|подпишись|ставь лайк|привет|друзья)\b", re.I)

# маты (маска — как в subtitle._mask_profanity, но модуль сам по себе)
_PROFANITY_RE = re.compile(
    r"\b(бля[а-яё]*|х[уy][йеёия][а-яё]*|пизд[а-яё]*|пзд|еб[а-яё]*|"
    r"ёб[а-яё]*|заеб[а-яё]*|охуе[а-яё]*|ахуе[а-яё]*|на[хф]уй|похер|"
    r"них[уя]я|сука[а-яё]*|мудак[а-яё]*|мудил[а-яё]*|гандон|гондон|"
    r"долбо[её]б[а-яё]*|дебил[а-яё]*)\b", re.I)
_PROF_MASK = lambda m: (m.group(0)[:1] + "*" * max(1, len(m.group(0)) - 1))


def _mask_profanity(text):
    """Мат -> б******** (TikTok банит описания с явным матом)."""
    return _PROFANITY_RE.sub(_PROF_MASK, str(text or ""))


def _clean(text, max_len=0):
    """Одна строка, без мусора по краям и кавычек-обёрток."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = t.strip('"«»\'“”„“')
    t = re.sub(r"^(заголовок|title|вариант)\s*[:\-–—]\s*", "", t, flags=re.I)
    if max_len and len(t) > max_len:
        t = t[:max_len]
    return t.strip()


def model_path():
    """Путь к GGUF-модели: env AI_LLM_PATH или кэш. Пусто — файла нет."""
    p = os.environ.get("AI_LLM_PATH") or os.path.join(CACHE_DIR,
                                                      DEFAULT_MODEL_NAME)
    return p if os.path.exists(p) else ""


def available():
    """LLM реально готова к работе? (файл модели + llama-cpp стоит)"""
    if not model_path():
        return False
    try:
        import llama_cpp  # noqa: F401
        return True
    except ImportError:
        return False


def _load_llm():
    """Ленивый старт модели (один раз на процесс; ~1-2 с на CPU)."""
    global _llm
    with _lock:
        if _llm is not None:
            return _llm
        p = model_path()
        if not p:
            return None
        try:
            from llama_cpp import Llama
            _llm = Llama(model_path=p, n_ctx=1024, n_gpu_layers=0,
                         n_threads=max(2, (os.cpu_count() or 2) // 2),
                         verbose=False)
            log.info("AI-заголовки: LLM загружена (%s)", os.path.basename(p))
        except Exception as e:
            log.warning("AI-заголовки: LLM недоступна (%s) — режим выжимки", e)
            _llm = None
        return _llm


def _gen_title(llm, transcript, max_len=TITLE_MAX):
    """Одна фаза генерации; возвращает "" при любом мусоре."""
    snippet = _clean(transcript, 700)
    if not _CYR_RE.search(snippet):
        return ""
    out = llm.create_chat_completion(
        messages=[
            {"role": "system",
             "content": ("Ты редактор вирусных TikTok-клипов русских "
                         "стримеров. Придумай ОДИН цепляющий заголовок на "
                         "русском языке по реплике стримера. Правила: "
                         "максимум 55 знаков и ЗАКОНЧЕННОЕ предложение; "
                         "ТОЛЬКО заголовок — без кавычек, без хэштегов, "
                         "без пояснений; живой разговорный тон, интрига "
                         "или эмоция; без мата. Примеры хороших "
                         "заголовков: «Он не ожидал ТАКОГО от неё», "
                         "«Такого ещё не было за весь стрим», "
                         "«Вы не поверите, что он сделал дальше».")},
            {"role": "user",
             "content": "Реплика стримера: %s" % snippet},
        ],
        temperature=0.75, top_p=0.9, max_tokens=44, stop=["\n"],
    )
    raw = (out.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _clean(raw, max_len + 10)


def extractive_hook(text, max_len=TITLE_MAX):
    """Заголовок БЕЗ нейросети: самая сочная фраза транскрипта.

    Порядок: фраза с «взрывным» словом -> любая фраза 25..70 знаков ->
    ужимок первой фразы по границе слова. Пусто — транскрипта не было.
    """
    t = _mask_profanity(_clean(text))
    if not t or not _CYR_RE.search(t):
        return ""
    parts = [p.strip(" —–-«»\"") for p in re.split(r"(?<=[.!?…])\s+", t)]
    parts = [p for p in parts
             if len(p) >= 12 and not _JUNK_RE.match(p)]
    if not parts:
        return ""
    juicy = [p for p in parts if len(p) <= max_len and _KICK_RE.search(p)]
    if juicy:
        return juicy[0][:max_len].rstrip(" ,.;:—–-")
    in_range = [p for p in parts if 25 <= len(p) <= max_len]
    if in_range:
        return in_range[0][:max_len].rstrip(" ,.;:—–-")
    p0 = parts[0]
    if len(p0) <= max_len:
        return p0
    cut = p0[:max_len]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 20 else cut).rstrip(" ,.;:—–-")


def _clean_title(title, max_len=TITLE_MAX):
    """Финальный контроль AI-строки: мат, длина, кириллица, мусор.

    Обрезка — по границе ЗАКОНЧЕННОГО предложения (последнее .!? до
    лимита), иначе по границе слова: незаконченная фраза в описании
    выглядит как баг и режет досматриваемость.
    """
    t = _mask_profanity(_clean(title))     # БЕЗ обрезки: граница ниже
    if not t or len(t) < 10 or not _CYR_RE.search(t):
        return ""
    if _JUNK_RE.match(t):
        return ""
    if len(t) > max_len:
        cut = t[:max_len]
        best = -1
        for m in re.finditer(r"[.!?…](?=$|[\s»\"'”])", cut):
            if m.end() >= 25:
                best = m.end()
        if best > 0:
            cut = cut[:best]
        else:
            sp = cut.rfind(" ")
            cut = cut[:sp] if sp > 20 else cut
            # висячий предлог/союз («момент за») — дотираем хвост
            cut = re.sub(r"\s+(и|а|но|что|как|за|в|на|с|к|по|у|о|об|от|до|"
                         r"из|под|для|же|ли|бы|не)$", "", cut, flags=re.I)
        t = cut.rstrip(" ,.;:—–-")
    # незакрытая скобка/кавычка после обрезки — убираем хвост
    for a, b in (("(", ")"), ("«", "»"), ('"', '"')):
        if t.count(a) > t.count(b):
            i = t.rfind(a)
            if i > 10:
                t = t[:i].rstrip(" ,.;:—–-")
    return t


def make_title(transcript, fallback="", max_len=TITLE_MAX):
    """Главный вход: транскрипт -> заголовок. Никогда не бросает исключений.

    Порядок: LLM (если есть модель) -> выжимка из транскрипта -> fallback
    (ник канала / название стрима) -> "".
    """
    try:
        text = _clean(transcript)
        title = ""
        if text and available():
            m = _load_llm()
            if m is not None:
                try:
                    title = _gen_title(m, text, max_len)
                except Exception as e:
                    log.warning("AI-заголовки: генерация упала (%s) — "
                                "выжимка из транскрипта", e)
        if not title:
            title = extractive_hook(text, max_len)
        title = _clean_title(title, max_len)
        if not title:
            # fallback проходит БЕЗ требования кириллицы: ник канала
            # (dmitry_lixxx и пр.) — легитимный прежний заголовок
            fb = _mask_profanity(_clean(fallback))
            title = fb[:max_len] if fb else ""
        return title or ""
    except Exception as e:
        log.warning("AI-заголовки: %s — вернул fallback", e)
        return _clean(str(fallback or ""), max_len)


def transcript_of(workdir):
    """Транскрипт клипа: transcript.txt в рабочей папке (пишет subtitle.py
    после Whisper). Пусто — файла нет (клип без речи)."""
    p = os.path.join(str(workdir), "transcript.txt")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def ensure_model(target_dir=CACHE_DIR, timeout_s=600):
    """Скачать GGUF-модель, если её нет (вызывается из workflow-шага;
    в Python — для локальных запусков). Возвращает путь или ""."""
    os.makedirs(str(target_dir), exist_ok=True)
    dst = os.path.join(str(target_dir), DEFAULT_MODEL_NAME)
    if os.path.exists(dst) and os.path.getsize(dst) > 400_000_000:
        return dst
    import subprocess as _sp
    log.info("AI-заголовки: качаю модель %s (~400 МБ)", MODEL_URL)
    r = _sp.run(["curl", "-sL", "--retry", "3", "-o", dst, MODEL_URL],
                timeout=timeout_s)
    if r.returncode != 0 or os.path.getsize(dst) < 100_000_000:
        log.warning("AI-заголовки: модель не скачалась — режим выжимки")
        return ""
    return dst


if __name__ == "__main__":   # ручная проверка: python ai_caption.py "текст..."
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    sample = (sys.argv[1] if len(sys.argv) > 1 else
              "Вот это поворот, ребята! Я вообще не ожидал, что он так "
              "сможет. Вы видели это? Это просто жесть, я в шоке. "
              "Ладно, дальше будет ещё интереснее, оставайтесь со мной.")
    print("модель:", available(), model_path())
    print("заголовок:", make_title(sample, fallback="Тест Канал twitch"))
