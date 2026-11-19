"""Основные структуры данных StreamClipper."""

import re
from dataclasses import dataclass, field


@dataclass
class ChatMessage:
    """Одно сообщение чата."""

    ts: float          # время сообщения (epoch, секунды)
    user: str          # автор
    text: str          # текст
    platform: str      # twitch | youtube


@dataclass
class HypeEvent:
    """Момент, когда чат «взорвался»."""

    ts: float                      # время пика (epoch, секунды)
    score: float                   # оценка хайпа 0-100
    top_messages: list = field(default_factory=list)   # самые яркие сообщения (ChatMessage)
    stats: dict = field(default_factory=dict)          # служебная статистика


@dataclass
class ChannelRef:
    """Ссылка на стримера: платформа + адрес канала (или конкретной трансляции).

    Появляется из файла links.txt, из Telegram-бота или из config.yaml.

    kind: "" — канал в эфире (следим постоянно); "clip" — готовый клип
    Twitch; "vod" — запись трансляции (Twitch videos). Клипы и записи не
    «стримят» — они не мониторятся, а сразу нарезаются на клипы (vod.py).
    """

    platform: str = ""     # twitch | youtube
    id: str = ""           # Twitch: ник; YouTube: ссылка на канал или на видео
    video_id: str = ""     # YouTube: id конкретной трансляции, если ссылка прямая
    video_url: str = ""    # YouTube: https://www.youtube.com/watch?v=...
    raw: str = ""          # строка, как её ввёл пользователь
    source: str = ""       # file | telegram | config
    kind: str = ""         # "" | "clip" | "vod"

    @property
    def key(self):
        return f"{self.platform}:{self.id}"

    def page_url(self):
        """Страница канала в браузере."""
        if self.platform == "twitch":
            return f"https://twitch.tv/{self.id}"
        return self.id if self.id.startswith("http") else f"https://www.youtube.com/{self.id}"

    def short_id(self):
        """Короткое имя для файлов, папок и логов."""
        s = self.id
        if s.startswith("http"):
            m = re.search(r"(?:v=|youtu\.be/|/@|/c/|/user/|/channel/)([\w.\-]+)", s)
            if m:
                return m.group(1).lstrip("@")
            return s.rstrip("/").split("/")[-1] or "youtube"
        return s.lstrip("@#").rstrip("/").split("/")[-1]

    def display(self):
        """Компактная ссылка для сообщений человеку."""
        if self.kind == "clip" and self.id.startswith("clip_"):
            return f"clips.twitch.tv/{self.id[5:]}"
        if self.kind == "vod" and self.id.startswith("vod_"):
            return f"twitch.tv/videos/{self.id[4:]}"
        if self.platform == "twitch":
            return f"twitch.tv/{self.id}"
        m = re.search(r"youtube\.com/(.+)$", self.id)
        return "youtube.com/" + (m.group(1) if m else self.id)

    def vod_url(self):
        """Каноническая ссылка для нарезки клипов и клипа Twitch."""
        if self.kind == "clip" and self.id.startswith("clip_"):
            return f"https://clips.twitch.tv/{self.id[5:]}"
        if self.kind == "vod" and self.id.startswith("vod_"):
            return f"https://www.twitch.tv/videos/{self.id[4:]}"
        return self.video_url or self.raw


@dataclass
class StreamState:
    """Состояние канала для передачи между компонентами."""

    platform: str = ""
    channel: str = ""
    title: str = ""
