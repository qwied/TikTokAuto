"""Детектор хайпа: оценивает «восторг» чата и решает, когда снять клип.

Сигналы: всплеск скорости сообщений, ключевые слова и смех (ахахах, KEKW, КЛИП...),
КАПС, повторяющийся спам одной фразы, число разных авторов.
"""

import logging
import re
import threading
import time
from collections import Counter, deque

from models import HypeEvent

log = logging.getLogger("hype")

DEFAULT_KEYWORDS = [
    "ахах", "хахах", "кек", "лол", "пгг", "рофл", "гоол", "клип", "аааа",
    "kekw", "lul", "lol", "lmao", "pog", "poggers", "rofl", "clip", "hype", "wtf",
]

# смех и восторженные повторы: ахахах, хахаха, оооо, ааааа ...
LAUGH_RE = re.compile(r"(?:ах|ха|хо|ох|оф|кек){2,}|([аоыиуе])\1{2,}", re.IGNORECASE)


def _norm(text):
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _all_caps(text):
    letters = [c for c in str(text) if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters)


def _is_excited(text, keywords):
    """Похоже ли сообщение на восторг/смех/хайп."""
    raw = str(text)
    t = _norm(raw)
    if not t:
        return False
    for kw in keywords:
        if not kw:
            continue
        kw = kw.lower()
        if len(kw) <= 4:
            # короткие слова ищем по границам слова: «гол» не должен ловить «голова»
            if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", t):
                return True
        elif kw in t:
            return True
    if LAUGH_RE.search(t):
        return True
    if _all_caps(raw):
        return True
    if "!!" in t or "!?" in t or "??" in t:
        return True
    return False


class HypeDetector(threading.Thread):
    """Оценивает чат раз в секунду; при устойчивом хайпе вызывает on_hype(event)."""

    def __init__(self, channel_key, hype_cfg, clip_cfg, on_hype):
        super().__init__(daemon=True, name=f"hype-{channel_key}")
        hype_cfg = hype_cfg or {}
        clip_cfg = clip_cfg or {}
        self.channel_key = channel_key
        self.window = float(hype_cfg.get("window", 5.0))
        self.threshold = float(hype_cfg.get("threshold", 62))
        self.sustain = max(1, int(hype_cfg.get("sustain", 2)))
        self.min_users = int(hype_cfg.get("min_users", 3))
        self.keywords = [str(k) for k in hype_cfg.get("keywords", DEFAULT_KEYWORDS)]
        self.cooldown = float(clip_cfg.get("cooldown", 180))
        self.max_per_hour = int(clip_cfg.get("max_per_hour", 6))
        self.on_hype = on_hype

        self.msgs = deque(maxlen=6000)
        self.baseline = 0.3          # фоновая скорость сообщений (сообщ/сек), EMA
        self.smooth = 0.0            # сглаженная оценка хайпа 0-100
        self.hot_streak = 0
        self.last_trigger = 0.0
        self.trigger_times = deque(maxlen=64)
        self._stop = threading.Event()

    def feed(self, msg):
        if msg and msg.text and msg.text.strip():
            self.msgs.append(msg)

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self.evaluate()
            except Exception:
                log.exception("Ошибка оценки хайпа")
            self._stop.wait(1.0)

    # --- оценка ----------------------------------------------------------------

    def evaluate(self, now=None):
        """Один «тик» оценки. now передаётся явно в тестах."""
        now = time.time() if now is None else now
        win = [m for m in self.msgs if now - m.ts <= self.window]
        rate = len(win) / self.window
        self.baseline = 0.97 * self.baseline + 0.03 * rate

        spike = rate / max(self.baseline, 0.1)
        spike_n = min(spike, 6.0) / 6.0
        users = {m.user for m in win}
        user_n = min(len(users) / 15.0, 1.0)
        kw_n = 0.0
        caps_n = 0.0
        dup_n = 0.0
        if win:
            hits = sum(1 for m in win if _is_excited(m.text, self.keywords))
            kw_n = hits / len(win)
            caps_n = sum(1 for m in win if _all_caps(m.text)) / len(win)
            cnt = Counter(_norm(m.text) for m in win)
            dup_n = min(sum(c for c in cnt.values() if c >= 3) / len(win), 1.0)

        score = 100.0 * (0.40 * spike_n + 0.25 * kw_n + 0.10 * caps_n
                         + 0.10 * dup_n + 0.15 * user_n)
        self.smooth = 0.5 * self.smooth + 0.5 * score

        if self.smooth >= self.threshold:
            self.hot_streak += 1
        else:
            self.hot_streak = 0

        hour_ago = now - 3600
        while self.trigger_times and self.trigger_times[0] < hour_ago:
            self.trigger_times.popleft()

        if (self.hot_streak >= self.sustain
                and len(users) >= self.min_users
                and now - self.last_trigger >= self.cooldown
                and len(self.trigger_times) < self.max_per_hour):
            self._trigger(now, self.smooth, win, users)

    def _trigger(self, now, score, win, users):
        self.last_trigger = now
        self.trigger_times.append(now)
        self.hot_streak = 0
        top = self._top_messages(win)
        stats = {
            "users": len(users),
            "msgs": len(win),
            "rate": round(len(win) / self.window, 2),
        }
        ev = HypeEvent(ts=now, score=round(float(score), 1), top_messages=top, stats=stats)
        log.info("ХАЙП %.0f/100 на %s (%.1f сообщ/сек, авторов %d) — снимаю клип",
                 ev.score, self.channel_key, stats["rate"], stats["users"])
        self.on_hype(ev)

    def manual_trigger(self, reason=""):
        """Клип по нажатию Enter — без проверок порога."""
        now = time.time()
        win = [m for m in self.msgs if now - m.ts <= 60]
        self._trigger(now, 100.0, win, {m.user for m in win})
        if reason:
            log.info("Клип запущен вручную (%s)", reason)

    def _top_messages(self, win, limit=5):
        good = [m for m in win if 2 < len(m.text) <= 80]
        good.sort(key=lambda m: (0 if _is_excited(m.text, self.keywords) else 1, -m.ts))
        seen, out = set(), []
        for m in good:
            if m.user in seen:
                continue
            seen.add(m.user)
            out.append(m)
            if len(out) >= limit:
                break
        return out

    def recent_messages(self, t0, t1):
        """Сообщения за интервал [t0, t1] — для субтитров из чата."""
        return [m for m in self.msgs if t0 <= m.ts <= t1]
