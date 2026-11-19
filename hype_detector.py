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
    "ахах", "хахах", "кек", "лол", "пгг", "рофл", "гоол", "гол", "клип", "аааа",
    "ооо", "вооо", "жесть", "шок", "ойбой", "огонь", "боже", "вот это",
    "ничоси", "капец", "ужас", "красава", "красиво", "нереально", "невероятно",
    "изи", "мув", "имба", "жёстко", "жестко", "душно", "топ",
    "kekw", "lul", "lol", "lmao", "pog", "poggers", "pogchamp", "rofl",
    "clip", "clip it", "clipped", "hype", "wtf", "no way", "insane", "crazy",
    "what the", "lets go", "letsgo", "omg", "jesus", "bro what", "gg",
]

# чистые эмоуты/смех одним словом — тоже восторг (KEKW, PogChamp, ахаха, ЛОЛ)
EMOTE_ONLY_RE = re.compile(
    r"^(?:kekw|lul|lol|lmao|pog|poggers|pogchamp|rofl|kappa|monkas|pepega|"
    r"омг|жесть|шок|кек|лол|ах[ах]*|х[ах]{2,}|о[оо]{2,}|а[аа]{2,}|го+л+|gg|wtf)+[!?0-9]*$",
    re.IGNORECASE)

# «Гарантированный» клип берёт лучший момент за последние 4 минуты:
# дальше кольцевой буфер записи (buffer_keep 300 с) уже не хранит.
IDLE_LOOKBACK_S = 240.0

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
    if EMOTE_ONLY_RE.match(t):
        return True
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
        self.bias = 0.0            # поправка порога от фидбека сайта (+ скучно / - интересно)
        # Гарантированная нарезка: если хайпа нет дольше N минут, клип всё
        # равно снимается (0 = выключено). Чтобы за любой стрим у пользователя
        # точно были клипы и уведомления, даже когда чат спокойный.
        self.max_idle = max(0.0, float(clip_cfg.get("max_idle_min", 20))) * 60.0
        self.started_at = 0.0          # момент первого сообщения чата
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
            if not self.started_at:
                self.started_at = float(msg.ts)
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

    def effective_threshold(self):
        """Порог с учётом ТИХОСТИ чата и обратной связи от сайта.

        У маленьких/спокойных чатов всплеск редко дотягивает до порога,
        рассчитанного на большие эфиры — из-за этого клипы выходят только
        «гарантированные» (раз в 12 минут) и часто скучные. Если фоновая
        скорость чата низкая, порог мягко опускается (до -15).
        bias — поправка от кнопок «Интересно/Скучно» на сайте: channel_bias
        (gh_run) поднимает порог каналу, чьи клипы отмечали скучными, и
        опускает тому, чьи клипы понравились.
        """
        quiet = max(0.0, 0.5 - min(self.baseline, 0.5))    # 0..0.5
        return max(30.0, self.threshold - quiet * 30.0 + float(self.bias))

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

        score = 100.0 * (0.35 * spike_n + 0.30 * kw_n + 0.10 * caps_n
                         + 0.10 * dup_n + 0.15 * user_n)
        # «моменты ещё лучше»: живая реакция чата (восторги/смех) весит чуть
        # больше чистой скорости сообщений — попадания точнее, мусора меньше
        self.smooth = 0.5 * self.smooth + 0.5 * score

        threshold = self.effective_threshold()
        if self.smooth >= threshold:
            self.hot_streak += 1
        else:
            self.hot_streak = 0

        # Быстрый канал: РЕЗКИЙ взрыв (многократный всплеск + явный восторг
        # в большинстве сообщений) не ждёт устойчивости sustain секунд —
        # «клип» лучше снять сразу на пике, чем полминуты спустя.
        fast_lane = (spike_n >= 0.62 and kw_n >= 0.30 and len(users) >= self.min_users)

        hour_ago = now - 3600
        while self.trigger_times and self.trigger_times[0] < hour_ago:
            self.trigger_times.popleft()

        if ((self.hot_streak >= self.sustain or fast_lane)
                and len(users) >= self.min_users
                and now - self.last_trigger >= self.cooldown
                and len(self.trigger_times) < self.max_per_hour):
            score_out = self.smooth if self.hot_streak >= self.sustain \
                else max(self.smooth, 72.0)     # явный взрыв — высокий балл сразу
            self._trigger(now, score_out, win, users)
            return

        # Гарантированный клип: давно ни хайпа, ни клипов — снимаем ВСЁ РАВНО,
        # но не «сейчас», а ЛУЧШИЙ момент за последние 4 минуты: ищем окно
        # чата с максимальной «горячестью» (скорость + восторг + авторы).
        # Так даже при спокойном чате в клип попадает самый живой момент,
        # а не случайная минута.
        anchor = max(self.last_trigger, self.started_at)
        if (self.max_idle > 0 and self.msgs and anchor > 0
                and now - anchor >= self.max_idle
                and len(self.trigger_times) < self.max_per_hour):
            ts = self._best_window_ts(now)
            log.info("Хайпа нет уже %.0f мин — снимаю клип с лучшего момента "
                     "за последние %.0f мин (гарантированная нарезка)",
                     (now - anchor) / 60, IDLE_LOOKBACK_S / 60)
            self._trigger(now, max(self.smooth, 55.0), win, users, ts=ts)

    def _trigger(self, now, score, win, users, ts=None):
        self.last_trigger = now
        self.trigger_times.append(now)
        self.hot_streak = 0
        top = self._top_messages(win)
        stats = {
            "users": len(users),
            "msgs": len(win),
            "rate": round(len(win) / self.window, 2),
        }
        ev = HypeEvent(ts=ts if ts else now, score=round(float(score), 1),
                       top_messages=top, stats=stats)
        log.info("ХАЙП %.0f/100 на %s (%.1f сообщ/сек, авторов %d) — снимаю клип",
                 ev.score, self.channel_key, stats["rate"], stats["users"])
        self.on_hype(ev)

    def _best_window_ts(self, now, bucket_s=5.0):
        """Лучший момент чата за последние IDLE_LOOKBACK_S секунд.

        Разбиваем сообщения на бакеты по bucket_s, каждый оцениваем теми же
        сигналами, что и основной хайп (всплеск скорости, восторженные
        сообщения, разные авторы), и берём конец самого горячего бакета.
        Pipeline от него отложит 30 с ДО и 30 с ПОСЛЕ — получится ровно 60 с
        с пиком внутри. Если чат совсем мёртвый — берём текущий момент.
        """
        t0 = max(self.last_trigger, now - IDLE_LOOKBACK_S)
        msgs = [m for m in self.msgs if t0 <= m.ts <= now]
        if not msgs:
            return now
        n_buckets = max(2, int((now - t0) // bucket_s))
        base = max(self.baseline, 0.1)
        best_s, best_ts = 0.0, now
        for i in range(n_buckets):
            a = t0 + i * bucket_s
            b = a + bucket_s
            bm = [m for m in msgs if a <= m.ts < b]
            if not bm:
                continue
            rate = min(len(bm) / bucket_s / base, 6.0) / 6.0
            hot = sum(1 for m in bm if _is_excited(m.text, self.keywords)) / len(bm)
            users_n = min(len({m.user for m in bm}) / 15.0, 1.0)
            # восторженные сообщения весомее скорости: спокойный поток из
            # «ок» даёт меньший балл, чем пара «АХАХА КЛИП КЕКВ» от своих
            s = 0.30 * rate + 0.45 * hot + 0.25 * users_n
            if s > best_s:
                best_s, best_ts = s, min(b, now)
        if best_s <= 0.05:
            return now
        return best_ts

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
