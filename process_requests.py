# -*- coding: utf-8 -*-
"""Запросы нарезки с сайта (окно «YouTube-нарезка») -> очередь jobs.json.

Как это работает:
  1. Пользователь на сайте youtube-clipper.html вставляет ссылку и жмёт
     «Нарезать». Сайт коммитом кладёт файл requests/req-<метка>.json.
  2. Коммит в requests/ сам запускает workflow Clip video (on: push).
  3. Workflow ПЕРЕД нарезкой вызывает этот скрипт: он разбирает каждый
     запрос, ставит задачу В ПЕРЁД очереди jobs.json и удаляет файл запроса.
     Дальше обычная нарезка (gh_run.py --vod) подхватывает задачу сама.

Формат запроса:
    {"url": "https://youtu.be/xxx",
     "mode": "all" | "hype",
     "win": 60,                 # длина клипа, сек (20..90)
     "limit": 0,                # 0 = весь ролик, иначе максимум клипов
     "start": "1:30", "end": "" # границы куска (мм:сс или ч:мм:сс), пусто = вся длина
    }

mode "all"  — РЕЗАТЬ ВСЁ ПОДРЯД окнами по win секунд (просьба пользователя:
              «чтоб всё резал на шортсы, а не только хайповые моменты»).
              Задача помечается force=true: гейты «нет лица», «нет речи»,
              «скучно» и порог TikTok такие клипы не выбрасывают.
mode "hype" — обычный отбор интересных минут (звук + чат-реплей).

Скрипт намеренно НЕ качает видео: только yt-dlp --dump-json (инфо о ролике:
название, длина, эфир ли) — это секунды. Скачивание и нарезка — в Clip video.
Если YouTube закрыл раннер бот-чеком, задача НЕ ставится, а в лог падает
понятное сообщение (нарежка не сможет скачаться всё равно).
"""

import glob
import json
import os
import re
import sys
import time

import requests

import vod  # probe_video(): yt-dlp с перебором плеер-клиентов при бот-чеке

REPO = os.environ.get("GITHUB_REPOSITORY") or "qwied/TikTokAuto"
TOKEN = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
API = "https://api.github.com/repos/%s/contents/%%s" % REPO
REQ_DIR = "requests"
JOBS_JSON = "jobs.json"

# YouTube-видео (не канал): watch / youtu.be / live / shorts
YT_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|live/|shorts/|embed/|v/)"
    r"|youtu\.be/)(?P<vid>[A-Za-z0-9_-]{5,})", re.IGNORECASE)


def log(msg, *a):
    print("[запросы] " + (msg % a if a else msg), flush=True)


def api_session():
    s = requests.Session()
    s.headers.update({
        "Authorization": "Bearer %s" % TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "StreamClipper-Requests",
    })
    return s


def get_file(s, path):
    """(содержимое bytes|None, sha|None) — 404 значит файла нет."""
    r = s.get(API % path, timeout=30)
    if r.status_code == 404:
        return None, None
    if r.status_code != 200:
        raise RuntimeError("GET %s: HTTP %s: %s" % (path, r.status_code, r.text[:200]))
    d = r.json()
    import base64
    content = base64.b64decode(d.get("content") or "") if d.get("content") else None
    return content, d.get("sha")


def put_file(s, path, content, sha, msg, attempts=5):
    """Коммит файла с ретраями на 409 (параллельный коммит чего-то другого).

    content — bytes ИЛИ callable(): при конфликте содержимое пересобирается
    заново из свежего файла (моя вставка в очередь поверх чужих изменений).
    """
    import base64
    payload = {"message": msg,
               "content": base64.b64encode(
                   content() if callable(content) else content).decode("ascii"),
               "branch": "main"}
    if sha:
        payload["sha"] = sha
    for attempt in range(attempts):
        r = s.put(API % path, json=payload, timeout=60)
        if r.status_code in (200, 201):
            return r.json().get("commit", {}).get("sha", "")
        if r.status_code == 409 and attempt < attempts - 1:
            log("конфликт записи %s (попытка %d) — пересобираю поверх свежего",
                path, attempt + 1)
            time.sleep(2 + attempt * 3)
            fresh, new_sha = get_file(s, path)
            if new_sha:
                payload["sha"] = new_sha
            if callable(content):
                payload["content"] = base64.b64encode(content(fresh)).decode("ascii")
            continue
        raise RuntimeError("PUT %s: HTTP %s: %s" % (path, r.status_code, r.text[:300]))
    raise RuntimeError("PUT %s: не удалось после %d попыток" % (path, attempts))


def del_file(s, path, sha, msg):
    try:
        r = s.delete(API % path, json={"sha": sha, "message": msg, "branch": "main"},
                     timeout=60)
        if r.status_code not in (200, 204):
            log("не удалить %s: HTTP %s", path, r.status_code)
    except Exception as e:
        log("не удалить %s: %s", path, e)


def parse_time(s):
    """'90' | '1:30' | '1:02:05' -> секунды (int|None). Пустое -> None."""
    s = str(s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return max(0, int(s))
    parts = s.split(":")
    if not all(p.strip().isdigit() for p in parts) or len(parts) > 3:
        return None
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.insert(0, 0)
    h, m, sec = nums
    return max(0, h * 3600 + m * 60 + sec)


def fmt_hms(sec):
    sec = int(max(0, sec))
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def build_all_windows(dur, win, limit, start, end):
    """Весь кусок [start..end] -> окна подряд по win секунд."""
    dur = int(dur)
    start = min(max(0, start or 0), max(0, dur - 5))
    end = dur if end is None else min(end, dur)
    if end - start < 5:
        return []
    wins = []
    t = start
    while t < end:
        wins.append([t, min(t + win, end)])
        t += win
    if limit and limit > 0:
        wins = wins[:limit]
    return wins


def main():
    if not TOKEN:
        log("НЕТ ТОКЕНА (GH_TOKEN) — запросы обработать нельзя")
        return 1
    # Cookies YouTube: либо ПУТЬ к файлу, либо сам файл (Netscape) из секрета
    # YOUTUBE_COOKIES_FILE. Без cookies раннеры GitHub бот-чеком закрываются.
    cookies = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    if cookies and not os.path.isfile(cookies) \
            and ("youtube" in cookies or cookies.startswith("#")):
        try:
            with open("cookies_secret.txt", "w", encoding="utf-8") as f:
                f.write(cookies if cookies.endswith("\n") else cookies + "\n")
            cookies = "cookies_secret.txt"
            log("Cookies YouTube получены ИЗ СЕКРЕТА (cookies_secret.txt)")
        except OSError:
            log("Cookies из секрета не сохранить в файл")
            cookies = ""
    if not cookies and os.path.isfile("cookies.txt"):
        cookies = "cookies.txt"
    if not cookies:
        log("ВНИМАНИЕ: cookies YouTube не заданы — YouTube обычно закрывает "
            "раннеры бот-чеком; нарезка ссылок может не скачаться")
    reqs = sorted(glob.glob(os.path.join(REQ_DIR, "*.json")))
    if not reqs:
        log("нет запросов с сайта — нечего делать")
        return 0

    s = api_session()
    my_jobs = []
    failed = 0
    for path in reqs:
        raw, sha = get_file(s, path)
        if raw is None:
            continue
        _name = os.path.basename(path)
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:
            failed += 1
            log("битый запрос %s (%s) — удаляю", _name, e)
            del_file(s, path, sha, "requests: битый запрос удалён")
            continue
        url = str(req.get("url") or "").strip()
        if not YT_URL_RE.match(url):
            failed += 1
            log("НЕ YouTube-ссылка в %s: %r — удаляю", _name, url[:80])
            del_file(s, path, sha, "requests: не youtube-ссылка — удалено")
            continue
        mode = "all" if str(req.get("mode") or "all").lower() != "hype" else "hype"
        try:
            win = int(req.get("win") or 60)
        except (TypeError, ValueError):
            win = 60
        win = max(20, min(90, win))
        try:
            limit = int(req.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        limit = max(0, min(300, limit))
        start = parse_time(req.get("start")) or 0
        end = parse_time(req.get("end"))

        log("разбираю %s: %s | режим %s | клип %d c | лимит %s | %s-%s",
            _name, url, mode, win, limit or "весь ролик",
            fmt_hms(start) if start else "начало",
            fmt_hms(end) if end else "конец")

        # Инфо о ролике: название + длина (yt-dlp, секунды; бот-чек ловим сразу)
        try:
            title, dur, live = vod.probe_video(url, cookies)
        except Exception as e:
            failed += 1
            if vod._looks_like_bot_check(str(e)):
                log("ОШИБКА: YouTube закрыл раннер бот-чеком (%s). Запрос удалён. "
                    "Нужно добавить cookies YouTube в секрет YOUTUBE_COOKIES_FILE "
                    "(файл cookies.txt из браузера) — тогда нарезка будет работать "
                    "на GitHub.", url)
            else:
                log("ОШИБКА: не удалось получить инфо о видео %s: %s — запрос удалён",
                    url, str(e)[:300])
            del_file(s, path, sha, "requests: видео недоступно (%s)" % _name)
            continue
        if str(live or "").lower() == "is_live":
            failed += 1
            log("видео сейчас В ЭФИРЕ — дождись окончания и кинь ссылку снова: %s", url)
            del_file(s, path, sha, "requests: видео в эфире — удалено")
            continue
        if not dur or dur < 10:
            failed += 1
            log("ОШИБКА: не понял длину видео (%.1f c) — запрос удалён", dur or 0)
            del_file(s, path, sha, "requests: не определилась длина — удалено")
            continue

        job = {"url": url, "kind": "video", "fails": 0, "tag": "manual",
               "display": (title or "video")[:120], "views": 0,
               "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "mode": mode, "win": win, "force": mode == "all"}
        if mode == "all":
            wins = build_all_windows(dur, win, limit, start, end)
            if not wins:
                failed += 1
                log("ОШИБКА: диапазон %s-%s слишком короткий — запрос удалён",
                    fmt_hms(start), fmt_hms(end) if end else "конец")
                del_file(s, path, sha, "requests: пустой диапазон — удалено")
                continue
            job["windows"] = wins
            job["scores"] = [1.0] * len(wins)
            log("OK: «%s» (%s, %.0f c) — %d клипов по %d c (%s–%s), режим «всё подряд»",
                title[:60], url, dur, len(wins), win,
                fmt_hms(wins[0][0]), fmt_hms(wins[-1][1]))
        else:
            log("OK: «%s» (%s, %.0f c) — отбор интересных моментов как обычно",
                title[:60], url, dur)
        my_jobs.append(job)
        del_file(s, path, sha, "requests: %s принят (%s)" % (_name, mode))

    if not my_jobs:
        if failed:
            log("НИ ОДИН запрос не принят (%d шт.) — запуск помечен ошибкой, "
                "смотри сообщения выше", failed)
            return 2
        return 0

    # Ставим свои задачи В ПЕРЁД очереди (ручные запросы важнее автопоиска).
    my_urls = {j["url"].lower() for j in my_jobs}

    def build_jobs_content(fresh=None):
        """Свежий jobs.json -> мои задачи в переди (вызывается и при 409)."""
        if fresh is None:
            raw, _sha = get_file(s, JOBS_JSON)
            fresh = raw
        try:
            data = json.loads(fresh.decode("utf-8")) if fresh else {}
        except Exception:
            data = {}
        jobs = [j for j in (data.get("jobs") or [])
                if isinstance(j, dict)
                and str(j.get("url") or "").lower() not in my_urls]
        data["jobs"] = my_jobs + jobs
        data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    _content, sha = get_file(s, JOBS_JSON)
    msg = ("vod: +%d ручных запросов с сайта (YouTube-нарезка, режим %s)"
           % (len(my_jobs), "/".join(sorted({j["mode"] for j in my_jobs}))))
    put_file(s, JOBS_JSON, build_jobs_content, sha, msg)
    log("очередь обновлена: задач с сайта %d — нарезка подхватит их в этом же "
        "запуске (Clip video)", len(my_jobs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
