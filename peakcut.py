# -*- coding: utf-8 -*-
"""PeakCut-движок для StreamClipper: субтитры-«караоке» с поп-анимацией слов,
крючок-заголовок в белой плашке, AI-отбор моментов и хэштеги.

Портировано из открытого проекта PeakCut
(https://github.com/haziqahmedqasmi/PeakCut, лицензия MIT) и адаптировано:
  * шрифт Montserrat ExtraBold — уже вшит в репо (fontsdir для libass);
  * слова приходят в формате StreamClipper {"word", "start", "end"};
  * русский язык в подсветке ключевых слов и в стоп-словах хэштегов;
  * AI-отбор моментов крутится на ЛОКАЛЬНОЙ Qwen 2.5 1.5B (ai_caption) —
    бесплатно, без ключей и без регистрации, как просил пользователь.

Субтитры: три стиля (bounce/hype/clean), активное слово красится и
«выпрыгивает» (\t scale), слова кладутся ПО ОДНОМУ точной геометрией —
выделенное слово растёт, не толкая соседей. Плашка-крючок сверху: тёмный
текст в белом скруглённом боксе, две сбалансированные строки.
"""
import json
import math
import os
import re
import struct

log_pre = "[peakcut] "

W, H = 1080, 1920          # выходной кадр (vertical.py: WIDTH x HEIGHT)

# ASS-цвета — &HBBGGRR. size — размер шрифта ASS (высота строки).
STYLES = {
    "bounce": {"label": "Bounce", "font": "Montserrat ExtraBold", "size": 132,
               "base": "FFFFFF", "active": "00F0FF", "strong": "FFFFFF",
               "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Montserrat ExtraBold", "size": 152,
             "base": "FFFFFF", "active": "3C3CFF", "strong": "00D7FF",
             "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Montserrat ExtraBold", "size": 100,
              "base": "FFFFFF", "active": "6BD7FF", "strong": "FFFFFF",
              "outline": 5, "pop": 106, "upper": False, "per": 4},
}
HOOK = {"font": "Montserrat ExtraBold", "size": 96, "ink": "14100E",
        "box": "FFFFFF", "pad_x": 36, "pad_y": 22, "radius": 26, "max_w": 900}
FONT_NAMES = ("Montserrat-ExtraBold.ttf",)
CAP_MAX_W = 960            # субтитры никогда не шире этого
CAPTION_LEAD = 0.0


def _font_file():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "fonts"), here,
                 os.path.join(here, "..", "fonts")):
        for n in FONT_NAMES:
            p = os.path.join(cand, n)
            if os.path.isfile(p):
                return p
    return None


class _Font(object):
    """Метрики TTF: измеряем текст ровно как libass его рисует — каждое слово
    кладётся вручную, выделенное слово растёт без сдвига соседей."""
    _cache = {}
    _pil = {}

    @classmethod
    def get(cls, path=None):
        path = path or _font_file()
        if path not in cls._cache:
            cls._cache[path] = _Font(path)
        return cls._cache[path]

    def __init__(self, path):
        if not path:
            raise RuntimeError("шрифт субтитров не найден")
        d = open(path, "rb").read()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        self.upm = struct.unpack(">H", d[tables[b"head"] + 18:
                                          tables[b"head"] + 20])[0]
        o = tables[b"OS/2"]
        self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])
        self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] \
            or int(self.upm * 0.7)
        self.path = path

    def em(self, size):
        return size * self.upm / (self.asc + self.desc)

    def width(self, text, size):
        try:
            from PIL import ImageFont
            f = _Font._pil.get("f")
            if f is None:
                f = _Font._pil["f"] = ImageFont.truetype(self.path, 200)
            return f.getlength(text) * self.em(size) / 200
        except Exception:
            return len(text) * self.em(size) * 0.62

    def cap_shift(self, size):
        return ((self.asc - self.desc) / 2 - self.cap / 2) \
            * size / (self.asc + self.desc)


def geometry(src_w, src_h, layout="blur"):
    """Где в кадре 1080x1920 картинка и куда встают крючок и субтитры."""
    layout = (layout or "blur").lower()
    if not src_w or not src_h:
        src_w, src_h = 16, 9
    if src_h >= src_w and layout != "crop":
        layout = "crop"        # уже вертикально — только crop имеет смысл
    if layout in ("crop", "left", "right", "split"):
        return 300, 1380 if layout != "split" else (H // 2)
    if layout == "fit":
        return 300, 1380
    fg_h = min(H, int(round(W * float(src_h) / float(src_w) / 2)) * 2)
    top = (H - fg_h) // 2
    return max(230, top - 190), min(1640, top + fg_h + 180)


def _ts(t):
    t = max(0.0, float(t))
    h, m = int(t // 3600), int(t % 3600 // 60)
    return "%d:%02d:%05.2f" % (h, m, t % 60)


def _clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ",
                  re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word):
    return re.sub(r"[^a-zа-яё0-9']", "", str(word).lower())


def _groups(words, per):
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per
                    or sum(len(x["w"]) for x in cur) + len(w["w"]) > 16
                    or w["s"] - cur[-1]["e"] > 0.45
                    or cur[-1]["w"][-1:] in ".?!"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _hook_events(hook, dur, y):
    """Крючок: тёмный текст в белом скруглённом боксе, 1-2 ровные строки."""
    f, size = _Font.get(), HOOK["size"]
    words = _clean(hook).split()
    if not words:
        return []
    lines = [" ".join(words)]
    if f.width(lines[0], size) > HOOK["max_w"] and len(words) > 1:
        cut = min(range(1, len(words)),
                  key=lambda i: max(f.width(" ".join(words[:i]), size),
                                    f.width(" ".join(words[i:]), size)))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max(f.width(l, size) for l in lines)
    if widest > HOOK["max_w"]:
        size = max(40, int(size * HOOK["max_w"] / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h = f.em(size) * 1.16
    bw, bh = widest + 2 * HOOK["pad_x"], line_h * len(lines) + 2 * HOOK["pad_y"]
    x0, y0, r = (W - bw) / 2, y - bh / 2, min(HOOK["radius"], bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: "%.0f" % v                                # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} "
           f"b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} "
           f"{n(x1 - r)} {n(y1)} l {n(x0 + r)} {n(y1)} "
           f"b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} "
           f"{n(x0 + r)} {n(y0)}")
    span = "%s,%s" % (_ts(0), _ts(dur))
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)"
          f"\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,"
                  f"{{\\an5\\pos({W // 2},{cy:.0f})\\fad(160,0)\\fs{size}}}{line}")
    return ev


def build_ass(words, start, end, hook, style_name="bounce", highlight=(),
              hook_y=None, cap_y=None, width=W, height=H):
    """ASS с поп-караоке и плашкой-крючком. words — {"word","start","end"}."""
    global W, H
    W, H = width, height
    st = STYLES.get(str(style_name or "bounce").lower(), STYLES["bounce"])
    pk = [{"w": w.get("word") or w.get("w") or "",
           "s": float(w.get("start", w.get("s", 0)) or 0),
           "e": float(w.get("end", w.get("e", 0)) or 0)}
          for w in (words or [])]
    pk = [dict(w, s=w["s"] - CAPTION_LEAD, e=w["e"] - CAPTION_LEAD) for w in pk]
    strong = {_key(h) for h in (highlight or ()) if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},&H00{st['base']},&H00{st['base']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{st['outline']},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{HOOK['size']},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = max(0.1, float(end) - float(start))
    ev = _hook_events(hook or "", dur, hook_y if hook_y is not None else 300)
    f = _Font.get()
    inside = [dict(w, w=_clean(w["w"])) for w in pk
              if w["s"] >= float(start) - 0.05 and w["s"] < float(end)]
    inside = sorted((w for w in inside if w["w"]), key=lambda w: w["s"])
    for i, w in enumerate(inside[:-1]):
        w["e"] = max(w["s"], min(w["e"], inside[i + 1]["s"]))
    groups = _groups(inside, st["per"])
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"] - float(start))
        g_end = min(g[-1]["e"] + 0.25,
                    groups[gi + 1][0]["s"] if gi + 1 < len(groups)
                    else float(end)) - float(start)
        if g_end - g_start < 0.02:
            continue
        texts = []
        for x in g:
            t = re.sub(r"[.,;:]+$", "", x["w"])
            texts.append(t.upper() if st["upper"] else t)
        size = st["size"]
        gap = f.width("n n", size) - 2 * f.width("n", size) \
            + (2 * st["outline"] + 6) * size / st["size"]
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        if total > CAP_MAX_W:
            size = max(36, int(size * CAP_MAX_W / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) \
                + (2 * st["outline"] + 6) * size / st["size"]
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = (cap_y if cap_y is not None else H - 540) - f.cap_shift(size)
        x = (W - total) / 2
        for i, (w, t) in enumerate(zip(g, texts)):
            wd = f.width(t, size)
            p = max(104, min(st["pop"], 100 + int(2200 / max(wd, 1))))
            pos = f"\\an5\\pos({x + wd / 2:.0f},{cy:.0f})\\fs{size}"
            x += wd + gap
            a = max(g_start, w["s"] - float(start))
            b = (g[i + 1]["s"] - float(start)) if i + 1 < len(g) else g_end
            base = st["strong"] if _key(w["w"]) in strong else st["base"]
            if a - g_start >= 0.02:
                ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,"
                          f"{{{pos}\\c&H{base}&}}{t}")
            if b - a >= 0.02:
                ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,"
                          f"{{{pos}\\c&H{st['active']}&\\fscx94\\fscy94"
                          f"\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,"
                          f"\\fscx{p - 5}\\fscy{p - 5})}}{t}")
            if g_end - b >= 0.02:
                ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,"
                          f"{{{pos}\\c&H{base}&\\fscx{p - 5}\\fscy{p - 5}"
                          f"\\t(0,90,\\fscx100\\fscy100)}}{t}")
    return head + "\n".join(ev) + "\n"


def write_ass(words, path, width=W, height=H, hook="", style="bounce",
              highlight=(), layout="blur", src_w=16, src_h=9):
    """Собрать ASS в файл; layout/src_* — от макета 9:16 (blur/crop/split)."""
    global W, H
    W, H = int(width), int(height)
    hook_y, cap_y = geometry(src_w, src_h, layout)
    ass = build_ass(words, 0.0, float(words[-1]["e"]) + 0.6 if words else 60.0,
                    hook, style, highlight, hook_y=hook_y, cap_y=cap_y,
                    width=width, height=height)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(ass)
    return path


# --- AI-ОТБОР МОМЕНТОВ (порт brain.py PeakCut на локальную Qwen) ------------

LOUD_MARK_MIN = 1.0        # окно с score заметно выше среднего помечается (LOUD)


def _mmss(t):
    t = max(0, int(t))
    return "%d:%02d" % (t // 60, t % 60)


BRAIN_HEAD = (
    "Ты редактор вирусных шортсов по записям стримов (TikTok, Shorts, Reels). "
    "Ниже — нумерованные окна видео с транскриптом (время от начала ОКНА). "
    "Выбери окна, которые зацепят зрителя, никогда не видевшего этот стрим: "
    "смешной момент, фейл, клатч, рэж, шок, горячее мнение или история с "
    "развязкой. Скучные диалоги, паузы, реклама — score 0.\n"
    "Ответ: ТОЛЬКО JSON-массив вида "
    '[{"i": номер_окна, "score": 0-100, "hook": "крючок до 55 знаков"}] '
    "для ЛУЧШИХ окон (не более {n}).\n"
)


def ai_moments(workdir, wins, ask_fn=None, max_windows=14):
    """Переупорядочить окна [(s, e, score)] по смыслу транскрипта.

    moments.json (пишет vod._rerank_by_transcript) содержит транскрипты окон.
    Локальная Qwen читает их и возвращает score+hook. Любая ошибка — окна
    остаются как были (fail-open, нарезка не падает).
    """
    path = os.path.join(str(workdir), "moments.json")
    try:
        if not wins or not os.path.isfile(path):
            return wins
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        texts = meta.get("windows") or []
        if not texts:
            return wins
        if ask_fn is None:
            try:
                import ai_caption
                ask_fn = ai_caption.ask
            except Exception:
                return wins
        hi = max(float(w[2]) for w in wins) or 1.0
        cands = []
        for i, w in enumerate(texts):
            if i >= min(len(wins), max_windows):
                break
            s, e, sc = wins[i][0], wins[i][1], float(wins[i][2])
            txt = str(w.get("text") or "")[:900]
            loud = " (LOUD)" if sc >= 0.6 * hi else ""
            cands.append({"i": i + 1, "s": s, "e": e, "sc": sc,
                          "line": "[%s] окно %d%s: %s"
                                  % (_mmss(s), i + 1, loud, txt)})
        prompt = BRAIN_HEAD.replace("{n}", str(min(6, len(cands)))) \
            + "\n".join(c["line"] for c in cands)
        raw = ""
        try:
            raw = str(ask_fn(prompt, max_tokens=220) or "")
        except Exception:
            return wins
        m = re.search(r"\[.*\]", raw, re.S)
        if not m:
            return wins
        picks = json.loads(m.group(0))
        if not isinstance(picks, list):
            return wins
        boost = {}
        hooks = {}
        for p in picks:
            try:
                idx = int(p.get("i")) - 1
                if 0 <= idx < len(wins):
                    boost[idx] = max(0.0, min(100.0, float(p.get("score") or 0)))
                    hooks[idx] = _clean(p.get("hook") or "")[:80]
            except Exception:
                continue
        if not boost:
            return wins
        ranked = []
        for i, (s, e, sc) in enumerate(wins):
            ai = boost.get(i)
            ns = (5.0 + ai / 100.0 * 95.0) if ai is not None \
                else max(0.0, float(sc)) * 0.6
            ranked.append((s, e, ns))
        ranked.sort(key=lambda x: -float(x[2]))
        if hooks:
            for i, h in hooks.items():
                if i < len(texts) and h:
                    texts[i]["hook"] = h
            meta["windows"] = texts
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
        return ranked
    except Exception:
        return wins


# --- ХЭШТЕГИ (порт hashtags.py PeakCut + русские стоп-слова) ----------------

MOST, LEAST = 5, 3

STOP = set("""
about above after again against all also always among and another any anyone
anything are around because been before being between both but came can cannot
come could did does doing done dont down each else even ever every everyone
everything find first for from gave get gets getting give going gone gonna good
got great guys had has have having here him his how into its just keep kind
know last left let like little look looking made make makes many may maybe mean
more most much must myself need never next nobody not nothing now off okay once
one only onto other our out over own people put really right said same say says
see she should since some someone something still such sure take than that the
their them then there these they thing things think this those though through
time too took try under until upon use very want wanna was way we well went
were what when where whether which while who whole why will with without won
would yeah yes you your
я ты он она оно мы вы они мне тебя ему ей нам вам их меня тебя него неё них
нас вас свой своё всё все все все-таки был была было были будут будешь может
можно надо нужно нельзя есть это этот эта эти то что как так но а и же вот
короче да нет ну даже чтобы если когда там тут здесь очень ещё уже тоже тоже
типа блин вообще просто кто почему где потом сейчас сегодня вчера теперь зато
хотя разве ли не ли-ка скажи скажем знач значит воп щем вообще-то
реально буквально честно наверное вообще-то конкретно серьёзно полностью
бро мужик пацаны чат стрим видео клип лайф подписывайтесь донат донаты
""".split())

CATEGORY_TAGS = {"funny": "funny", "fail": "fails", "clutch": "clutch",
                 "rage": "rage", "shock": "shocking", "take": "hottake",
                 "story": "storytime", "смешно": "funny", "фейл": "fails"}
FILLER = ("shorts", "clips", "viral")
CHANNEL_NOISE = {"live", "official", "tv", "gaming", "clips", "channel",
                 "highlights", "youtube", "twitch", "kick", "tiktok", "stream"}
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9']{2,19}")
_TAG_IN_TEXT = re.compile(r"(?:^|\s)#\w+")


def _tag(s):
    t = re.sub(r"[^A-Za-zА-Яа-яЁё0-9]", "", str(s or "")).lower()[:30]
    return "" if len(t) < 3 or t.isdigit() or t in STOP else t


def channel_tag(uploader):
    words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", str(uploader or ""))
             if w.lower() not in CHANNEL_NOISE]
    t = _tag("".join(words))
    return t if 3 <= len(t) <= 25 else ""


def keywords(text, title="", count=2):
    raw = str(text or "").split()
    in_title = {w.lower() for w in _WORD.findall(str(title or ""))}
    score = {}
    for i, r in enumerate(raw):
        m = _WORD.search(r)
        if not m:
            continue
        w = m.group(0).strip("'").lower()
        if w in STOP or len(w) < 4 or not _tag(w):
            continue
        name = r[:1].isupper() and i > 0 and raw[i - 1][-1:] not in ".?!"
        score[w] = score.get(w, 0) + 1 + (2 if name else 0) \
            + (2 if w in in_title else 0)
    best = sorted((w for w in score if score[w] >= 2),
                  key=lambda w: (-score[w], -len(w), w))
    return best[:count]


def make_hashtags(text="", title="", uploader="", category="other",
                  ai_tags=(), limit=MOST):
    """3-5 хэштегов из реального контента, без нейросети — мгновенно."""
    out = []

    def add(t):
        t = _tag(t)
        if t and t not in out and len(out) < limit:
            out.append(t)

    add(channel_tag(uploader))
    for t in list(ai_tags or [])[:2]:
        add(t)
    for t in keywords(text, title):
        add(t)
    add(CATEGORY_TAGS.get(str(category or "").lower(), ""))
    for t in FILLER:
        if len(out) < LEAST:
            add(t)
    return out
