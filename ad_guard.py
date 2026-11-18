"""Пауза нарезки во время рекламы (RubAp / RubUp и любой другой).

Как это работает:
  * пока на стриме идёт реклама, НАРЕЗКА СТАВИТСЯ НА СТОП — новые клипы
    не снимаются (хайп-события во время рекламы пропускаются);
  * как только реклама закончилась — нарезка ПРОДОЛЖАЕТСЯ автоматически.

Два механизма обнаружения:
  1. АВТОМАТИЧЕСКИЙ (HLS-плейлист): программа читает тот же плейлист потока,
     который используется для записи, и ищет в нём рекламные маркеры
     (twitch-stitched-ad, SCTE-35 CUE-OUT/IN и т.п.). Работает для
     серверной рекламы Twitch/YouTube.
  2. РУЧНОЙ (с сайта или из Telegram): часть рекламы (вставки RubAp/RubUp,
     которые стример запускает сам поверх эфира) в плейлист не попадает —
     такую рекламу помечают одной кнопкой на сайте: «РЕКЛАМА» на карточке
     канала. Повторное нажатие снимает паузу.

Сегменты буфера, записанные во время рекламы, помечаются и НЕ попадают
в клипы (если только это не единственные сегменты — тогда клип всё равно
соберётся, лучше с рекламой, чем никакого).
"""

import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("ad")

# Маркеры рекламы в HLS-плейлисте (ищутся без учёта регистра)
DEFAULT_MARKERS = [
    "twitch-stitched-ad",     # серверная реклама Twitch
    "twitch-ad",
    "ext-x-cue-out",          # SCTE-35: начало рекламной паузы
    "ext-x-cueout",
    "scte35-out",
    "ext-x-scte35",
    "bump-broadcast-ads",
    "stitched-ad",
]


def detect_ad(playlist_text, markers=None):
    """Есть ли в тексте плейлиста рекламные маркеры. None/пусто -> False."""
    if not playlist_text:
        return False
    low = str(playlist_text).lower()
    for m in (markers if markers is not None else DEFAULT_MARKERS):
        m = str(m or "").lower()
        if m and m in low:
            return True
    return False


def fetch_text(url, timeout=5.0):
    """Скачать текст плейлиста. Ошибка сети -> None (не считается рекламой)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StreamClipper/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(512 * 1024).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


class AdGuard(threading.Thread):
    """Смотрит за рекламой одного эфира: ad_active True/False.

    get_url — callable, возвращает актуальный URL плейлиста (или None).
    on_change(ad_active) вызывается при каждом переключении.
    """

    def __init__(self, channel_key, cfg, get_url, on_change=None):
        super().__init__(daemon=True, name=f"ad-{channel_key}")
        cfg = cfg or {}
        self.channel_key = channel_key
        self.poll = max(1.0, float(cfg.get("poll", 4)))
        self.markers = [str(m) for m in (cfg.get("markers") or DEFAULT_MARKERS)]
        self.get_url = get_url
        self.on_change = on_change
        self.fetcher = fetch_text          # подменяется в тестах

        self.auto_ad = False               # реклама найдена в плейлисте
        self.manual_ad = False             # пауза, поставленная человеком
        self.changed_at = 0.0              # момент последнего переключения
        self.intervals = []                # [(start, end), ...] рекламные паузы
        self.checks = 0
        self.fails = 0
        self._stop = threading.Event()

    @property
    def ad_active(self):
        """True — нарезка на паузе прямо сейчас."""
        return bool(self.auto_ad or self.manual_ad)

    def set_manual(self, state):
        """Ручная пауза с сайта: True — реклама идёт, False — кончилась."""
        state = bool(state)
        if state == self.manual_ad:
            return self.ad_active
        self.manual_ad = state
        self._note_change("вручную" if state else "вручную закончилась")
        return self.ad_active

    def _note_change(self, how):
        now = time.time()
        if self.ad_active:
            self.intervals.append([now, None])
            log.warning("РЕКЛАМА на %s (%s) — нарезка НА ПАУЗЕ", self.channel_key, how)
        else:
            if self.intervals and self.intervals[-1][1] is None:
                self.intervals[-1][1] = now
            log.info("Реклама на %s закончилась (%.0f c) — нарезка ПРОДОЛЖАЕТСЯ",
                     self.channel_key,
                     (self.intervals[-1][1] - self.intervals[-1][0])
                     if self.intervals and self.intervals[-1][1] else 0)
        self.changed_at = now
        if self.on_change:
            try:
                self.on_change(self.ad_active)
            except Exception:
                log.exception("on_change упал")

    def _tick(self):
        url = None
        try:
            url = self.get_url() if self.get_url else None
        except Exception:
            url = None
        if not url:
            return                      # ссылки на поток ещё нет — просто ждём
        self.checks += 1
        text = self.fetcher(url)
        if text is None:
            self.fails += 1
            return                      # не скачалось — не считаем рекламой
        found = detect_ad(text, self.markers)
        if found != self.auto_ad:
            self.auto_ad = found
            self._note_change("по плейлисту" if found else "по плейлисту")

    def run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("Проверка рекламы упала")
            self._stop.wait(self.poll)

    def stop(self):
        self._stop.set()

    def status(self):
        return {
            "ad": self.ad_active,
            "manual": self.manual_ad,
            "auto": self.auto_ad,
        }
