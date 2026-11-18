"""Ссылки на стримеров: разбор любого формата + файл links.txt.

Пользователь просто кидает ссылки — в файл links.txt (по одной в строке) или
боту в Telegram. Программа сама определяет платформу:

    twitch.tv/ник
    https://www.twitch.tv/ник?sr=a
    youtube.com/@канал
    https://www.youtube.com/channel/UCxxxx
    https://www.youtube.com/c/имя
    https://www.youtube.com/watch?v=VIDEO      (прямая ссылка на трансляцию)
    https://youtu.be/VIDEO
    https://www.youtube.com/live/VIDEO

Просто ник без точки и слэшей считается ником на Twitch.
"""

import logging
import os
import re
import threading

from models import ChannelRef

log = logging.getLogger("links")

TWITCH_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?twitch\.tv/(?P<name>[A-Za-z0-9_]{2,25})(?:[/?#].*)?$",
    re.IGNORECASE)

YT_VIDEO_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|live/|embed/|v/)|youtu\.be/)"
    r"(?P<vid>[A-Za-z0-9_-]{5,})",
    re.IGNORECASE)

YT_CHANNEL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/"
    r"(?P<path>@[\w.\-]{2,60}|c/[\w.\-]{2,60}|user/[\w.\-]{2,60}|channel/UC[\w\-]{20,})"
    r"(?:[/?#].*)?$",
    re.IGNORECASE)

NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")


def parse_link(text, source=""):
    """Строка -> ChannelRef или None, если это не похоже на ссылку на стримера."""
    s = str(text or "").strip().strip('"').strip("'").strip("<>").rstrip(",;")
    if not s or s.startswith("#") or s.startswith("//"):
        return None
    s = s.split()[0]                      # «twitch.tv/xxx — комментарий» -> ссылка
    m = TWITCH_RE.match(s)
    if m:
        return ChannelRef(platform="twitch", id=m.group("name").lower(),
                          raw=s, source=source)
    m = YT_VIDEO_RE.match(s)
    if m:
        vid = m.group("vid")
        url = f"https://www.youtube.com/watch?v={vid}"
        return ChannelRef(platform="youtube", id=url, video_id=vid,
                          video_url=url, raw=s, source=source)
    m = YT_CHANNEL_RE.match(s)
    if m:
        url = f"https://www.youtube.com/{m.group('path')}"
        return ChannelRef(platform="youtube", id=url, raw=s, source=source)
    if NICK_RE.match(s) and "." not in s and "/" not in s and ":" not in s:
        # голый ник без платформы — считаем Twitch
        return ChannelRef(platform="twitch", id=s.lower(), raw=s, source=source)
    return None


def dedup_key(ref):
    """Ключ для сравнения ссылок между собой (регистр не важен)."""
    return (ref.platform, ref.id.lower())


def refs_from_config(cfg):
    """channels из config.yaml -> список ChannelRef.

    Каждая строка списка — просто ССЫЛКА на канал в любом виде:
        - "https://www.twitch.tv/ник"
        - "https://www.youtube.com/@канал"
        - "twitch.tv/ник"
    Поддерживается и старый формат словарём: {platform: ..., id: ...}.
    """
    refs = []
    for c in (cfg or {}).get("channels", []) or []:
        ref = None
        if isinstance(c, str):
            ref = parse_link(c, source="config")
        elif isinstance(c, dict):
            raw = str(c.get("id") or c.get("url") or "").strip()
            ref = parse_link(raw, source="config")
            if ref is None and str(c.get("platform", "")).lower() in ("twitch", "youtube") and raw:
                ref = ChannelRef(platform=str(c["platform"]).lower(), id=raw,
                                 raw=raw, source="config")
        if ref and ref.platform in ("twitch", "youtube") and ref.id:
            refs.append(ref)
        elif c is not None:
            log.warning("config.yaml: не понял ссылку на канал: %r", c)
    return refs


class LinksFile:
    """Файл links.txt: чтение, слежение за изменениями, добавление/удаление строк.

    Список можно править прямо во время работы программы — она подхватит
    изменения сама (следит за датой изменения файла).
    """

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._stat = self._read_stat()

    def _read_stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def exists(self):
        return os.path.exists(self.path)

    def changed(self):
        """True, если файл изменился с момента последнего чтения (время или размер)."""
        return self._read_stat() != self._stat

    def read_lines(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self._stat = self._read_stat()
            return lines
        except FileNotFoundError:
            self._stat = self._read_stat()
            return []
        except OSError as e:
            log.warning("Не удалось прочитать %s: %s", self.path, e)
            return []

    def read_refs(self):
        """Разобрать файл. Возвращает (список ссылок, список некорректных строк)."""
        refs, bad = [], []
        for line in self.read_lines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            ref = parse_link(stripped, source="file")
            if ref:
                refs.append(ref)
            else:
                bad.append(stripped)
        return refs, bad

    def add(self, ref):
        """Дописать ссылку в файл (если её ещё там нет). False — уже есть/не записалось."""
        with self._lock:
            try:
                lines = self.read_lines()
                key = dedup_key(ref)
                for ln in lines:
                    r = parse_link(ln.strip())
                    if r and dedup_key(r) == key:
                        return False
                line = ref.raw.strip()
                if lines and not lines[-1].endswith("\n"):
                    line = "\n" + line
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._stat = self._read_stat()
                log.info("Ссылка дописана в %s: %s", self.path, ref.display())
                return True
            except OSError as e:
                log.warning("Не удалось дописать ссылку в %s: %s", self.path, e)
                return False

    def remove(self, ref):
        """Убрать ссылку из файла. True, если убрали."""
        with self._lock:
            try:
                lines = self.read_lines()
                key = dedup_key(ref)
                kept, removed = [], False
                for ln in lines:
                    r = parse_link(ln.strip())
                    if r and dedup_key(r) == key and not ln.strip().startswith("#"):
                        removed = True
                        continue
                    kept.append(ln)
                if removed:
                    with open(self.path, "w", encoding="utf-8") as f:
                        f.writelines(kept)
                    self._stat = self._read_stat()
                    log.info("Ссылка удалена из %s: %s", self.path, ref.display())
                return removed
            except OSError as e:
                log.warning("Не удалось убрать ссылку из %s: %s", self.path, e)
                return False


TEMPLATE = """\
# ============================================================
#  StreamClipper — ссылки на стримеров
#  Вставьте по одной ссылке в строку и сохраните файл.
#  Программа подхватит изменения за несколько секунд, перезапуск не нужен.
#
#  Можно кидать в любом виде:
#    twitch.tv/ник
#    https://www.twitch.tv/ник
#    https://www.youtube.com/@канал
#    https://www.youtube.com/channel/UCxxxx
#    https://www.youtube.com/watch?v=VIDEO      (прямая ссылка на трансляцию)
#
#  Строки, начинающиеся с # — комментарии, они не учитываются.
#  Ещё способ: пришлите ссылку боту в Telegram — она добавится сюда сама.
# ============================================================

"""


def ensure_links_file(path):
    """Создать links.txt с подсказкой, если файла нет."""
    if os.path.exists(path):
        return path
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(TEMPLATE)
        log.info("Создан файл %s — впишите в него ссылки на стримеров", path)
    except OSError as e:
        log.warning("Не удалось создать %s: %s", path, e)
    return path
