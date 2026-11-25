#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""io_worker — локальный воркер io-очереди (запуск на ноутбуке пользователя).

Почему локально: YouTube с 2026 не отдаёт стримы датацентровым IP
(GitHub Actions) — ни cookies, ни PO-токены не помогают (проверено
2026-11-25 тремя тестами). Домашний IP + живые cookies из своего браузера
работают как обычно.

Что делает воркер (цикл):
  1) тянет youtube_queue_io.json + processed.json из репозитория;
  2) берёт первые --count ещё НЕ нарезанных видео (самые популярные);
  3) ставит их в локальный jobs.json: окно --win секунд С НАЧАЛА,
     force=true (без гейтов «лицо/речь» — геймплей без вебкамеры);
  4) запускает gh_run.py --vod — тот качает через cookies браузера
     (config.yaml -> youtube.cookies_from_browser: chrome), собирает
     вертикальный клип 9:16 с караоке-субтитрами и сам публикует:
     Releases + preview/ + clips.json + processed.json.
     Сайт и страница YouTube-нарезки зачёркивают нарезанное сами.
  5) повторяет цикл, пока очередь не кончится.

Запуск:  start_io_worker.bat            (двойной клик, параметры по умолчанию)
         python io_worker.py --count 10 --max-minutes 115 --once
Токен:   файл gh_token.txt рядом с io_worker.py — тот же GitHub PAT,
         что вставляется на странице YouTube-нарезки (права Contents+Releases).
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time

import requests

REPO = os.environ.get("SC_REPO", "qwied/TikTokAuto")
RAW = "https://raw.githubusercontent.com/%s/main/" % REPO
HERE = os.path.dirname(os.path.abspath(__file__))


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("SC_PAT") or ""
    if not t:
        p = os.path.join(HERE, "gh_token.txt")
        if os.path.isfile(p):
            t = open(p, encoding="utf-8").read().strip()
    if not t:
        print("НЕТ ТОКЕНА: положи GitHub PAT в файл gh_token.txt рядом с io_worker.py")
        sys.exit(2)
    return t


def raw_json(path, tok):
    r = requests.get(RAW + path, headers={"Authorization": "Bearer " + tok,
                      "User-Agent": "StreamClipper-IoWorker"}, timeout=60)
    r.raise_for_status()
    return r.json()


def done_urls(tok):
    try:
        p = raw_json("processed.json", tok)
        return {str(u).strip().lower().rstrip("/") for u in p.get("done") or []}
    except Exception as e:
        print("! processed.json не прочитан (%s) — считаю всё не нарезанным" % e)
        return set()


def build_batch(todo, win, limit, count):
    jobs = []
    for j in todo[:count]:
        url = j.get("url") or ""
        if not url:
            continue
        jobs.append({
            "url": url, "kind": "video", "fails": 0,
            "tag": "io", "display": str(j.get("display") or url)[:120],
            "views": j.get("views", 0),
            "windows": [[0 + i * win, min((i + 1) * win, 600)] for i in range(limit)],
            "scores": [1.0] * limit,
            "force": True,
        })
    return jobs


def run_clipper(batch, max_minutes, tok):
    jobs_path = os.path.join(HERE, "jobs.json")
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump({"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "jobs": batch}, f, ensure_ascii=False, indent=1)
    env = dict(os.environ, GH_TOKEN=tok, PYTHONIOENCODING="utf-8")
    # убрать stale-секрет cookies, если оставался от CI-экспериментов
    env.pop("YOUTUBE_COOKIES_FILE", None)
    print("→ gh_run.py --vod: %d видео, лимит %d мин (клипы пойдут в Releases "
          "и на сайт сами)" % (len(batch), max_minutes))
    r = subprocess.run([sys.executable, os.path.join(HERE, "gh_run.py"),
                        "--vod", "--max-minutes", str(max_minutes)],
                       env=env)
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description="io_worker: локальная нарезка io-очереди")
    ap.add_argument("--count", type=int, default=10, help="видео за цикл (по умолчанию 10)")
    ap.add_argument("--max-minutes", type=int, default=115,
                    help="лимит работы gh_run за цикл (по умолчанию 115)")
    ap.add_argument("--win", type=int, default=45,
                    help="длина клипа, сек (30-50 по запросу; по умолчанию 45)")
    ap.add_argument("--limit", type=int, default=1, help="клипов с одного видео")
    ap.add_argument("--once", action="store_true", help="один цикл и выход")
    a = ap.parse_args()

    tok = token()
    cycle = 0
    while True:
        cycle += 1
        print("\n===== цикл %d | %s =====" % (cycle, time.strftime("%H:%M:%S")))
        try:
            queue = raw_json("youtube_queue_io.json", tok)
        except Exception as e:
            print("! не прочитать youtube_queue_io.json: %s" % e)
            return 1
        jobs = queue.get("jobs") or []
        done = done_urls(tok)
        todo = [j for j in jobs if str(j.get("url") or "").strip().lower().rstrip("/")
                not in done]
        print("в очереди %d видео | нарезано ранее %d | осталось %d"
              % (len(jobs), len(done), len(todo)))
        if not todo:
            print("ВСЁ НАРЕЗАНО — очередь пуста. Хорошая работа!")
            return 0

        batch = build_batch(todo, a.win, a.limit, a.count)
        rc = run_clipper(batch, a.max_minutes, tok)
        print("gh_run завершился (код %s)" % rc)
        if a.once:
            return 0
        time.sleep(20)


if __name__ == "__main__":
    sys.exit(main())
