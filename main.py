"""StreamClipper — точка входа.

Вы кидаете ссылку на канал (в links.txt, channels в config.yaml или боту в
Telegram) — программа каждую секунду проверяет канал и, как только стример
выходит в эфир, сама подключается к записи и нарезает клипы РОВНО по 60 секунд
с субтитрами прямо во время трансляции. Готовый клип прилетает уведомлением
на любое устройство и появляется в веб-интерфейсе (телефон/GitHub Pages).

Запуск:      python main.py --config config.yaml
Enter в консоли — снять клип немедленно.
Ctrl+C — остановить программу.
"""

import argparse
import logging
import os
import sys
import threading

from clip_server import ClipServer
from links import ensure_links_file
from notifier import Notifier, _is_placeholder
from pipeline import Pipeline
from subtitle import whisper_available
from utils import check_binaries, load_config, setup_logging

log = logging.getLogger("main")

BANNER = """----------------------------------------------------------
  StreamClipper 2.0
  кидаете ссылку на канал -> следит каждую секунду -> сам заходит на стрим
  клип РОВНО 60 сек с субтитрами -> уведомление на любое устройство
----------------------------------------------------------"""


def _config_refs(cfg):
    """Ссылки на каналы из config.yaml: строки channels — готовые ссылки."""
    from links import refs_from_config  # noqa: PLC0415
    return refs_from_config(cfg)


def main():
    ap = argparse.ArgumentParser(description="StreamClipper: автоматические клипы хайп-моментов")
    ap.add_argument("--config", default="config.yaml", help="путь к config.yaml")
    args = ap.parse_args()

    setup_logging()
    print(BANNER)

    if not os.path.exists(args.config):
        log.error("Не найден конфиг %s — положите config.yaml рядом с main.py", args.config)
        return 2
    cfg = load_config(args.config)
    cfg["_config_path"] = os.path.abspath(args.config)

    missing = check_binaries()
    for m in missing:
        log.warning("Нет программы: %s", m)
    if any("ffmpeg —" in m for m in missing):
        log.error("Без ffmpeg работа невозможна. Установите (см. README, шаг 2) и запустите снова.")
        return 2

    base_dir = os.path.dirname(cfg["_config_path"])
    links_path = ensure_links_file(os.path.join(base_dir, str(cfg.get("links_file", "links.txt"))))

    notify_cfg = cfg.get("notify", {}) or {}
    provider = str(notify_cfg.get("provider") or "telegram").lower()
    tg = notify_cfg.get("telegram") or {}
    token = str(tg.get("bot_token") or "")
    tg_ready = provider == "telegram" and not _is_placeholder(token)
    if provider == "telegram" and not tg_ready:
        log.warning("Telegram-бот не настроен: заполните bot_token и chat_id в config.yaml "
                    "(инструкция в README.md, раздел «Настройка уведомлений»)")

    if (cfg.get("subtitle", {}) or {}).get("enabled", True) and not whisper_available():
        log.warning("faster-whisper не установлен — клипы будут БЕЗ субтитров "
                    "(субтитры делаются только из речи, чат не отслеживается). "
                    "Для субтитров выполните: pip install -r requirements.txt")

    refs = _config_refs(cfg)
    # ссылки из links.txt прочитает сам Pipeline — здесь только проверяем, что что-то есть
    try:
        with open(links_path, "r", encoding="utf-8") as f:
            file_lines = [ln.strip() for ln in f
                          if ln.strip() and not ln.strip().startswith("#")]
    except OSError:
        file_lines = []
    if not refs and not file_lines and not tg_ready:
        log.error("Нет ни одной ссылки на стримеров.")
        log.error("Как начать:")
        log.error("  1) Откройте %s и вставьте ССЫЛКИ НА КАНАЛЫ (twitch.tv/ник, youtube.com/@канал) —",
                  os.path.basename(links_path))
        log.error("     файл можно править прямо во время работы программы;")
        log.error("  2) или впишите ссылки в channels в config.yaml;")
        log.error("  3) или настройте Telegram-бота (README, шаг 4) и кидайте ссылки ему в чат.")
        return 2
    if not refs and not file_lines and tg_ready:
        log.info("Список ссылок пуст — пришлите Telegram-боту ссылку на стримера, "
                 "и он начнёт следить.")

    server = None
    server_cfg = cfg.get("server", {}) or {}
    notifier = Notifier(notify_cfg, server_base_url="")   # base_url подставим ниже
    pipeline = Pipeline(cfg, notifier, server_base_url="")

    if server_cfg.get("enabled", True):
        clips_dir = os.path.join(
            os.path.dirname(cfg["_config_path"]),
            str((cfg.get("storage", {}) or {}).get("clips_dir", "clips")))
        docs_dir = os.path.join(os.path.dirname(cfg["_config_path"]),
                                str(server_cfg.get("docs_dir", "docs")))
        server = ClipServer(clips_dir, port=int(server_cfg.get("port", 8765)),
                            docs_dir=docs_dir,
                            status_provider=lambda: pipeline.api_status(),
                            channels_api=pipeline)
        try:
            server.start()
        except OSError as e:
            log.warning("Не удалось занять порт %s (%s) — возможно, StreamClipper уже "
                        "запущен. Продолжаю без веб-интерфейса; укажите другой порт "
                        "в config.yaml (server.port).", server_cfg.get("port", 8765), e)
            server = None

    public = str(server_cfg.get("public_base_url") or "").rstrip("/")
    base_url = public or (server.base_url() if server else "")
    notifier.base_url = base_url
    pipeline.server_base_url = base_url

    pipeline.start()
    log.info("Готов к работе. Enter — снять клип сейчас, Ctrl+C — выход.")
    log.info("Ссылки можно добавлять на ходу: правьте links.txt или пришлите их боту.")

    try:
        if sys.stdin and sys.stdin.isatty():
            for _line in sys.stdin:
                pipeline.manual_clip("по нажатию Enter")
        else:
            threading.Event().wait()   # запуск без консоли: работать до Ctrl+C/kill
    except KeyboardInterrupt:
        pass

    log.info("Останавливаюсь...")
    pipeline.stop()
    if server:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
