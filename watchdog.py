"""Watchdog StreamClipper: самолечение 24/7-автоматизации на GitHub Actions.

Слежение устроено ЭСТАФОТОЙ: задача Check live живёт ~55 минут и в конце
запускает следующую; Monitor живёт до ~4.7 часов и тоже перезапускает
цепочку. Цепочка надёжна, но её может оборвать инцидент GitHub, отмена
запуска или падение задачи — тогда слежение останавливается до ближайшего
резервного cron (а он редко срабатывает идеально).

Watchdog по cron раз в 20 минут проверяет здоровье и РЕМОНТИРУЕТ:

  1. Цепочка Check live: если НИ ОДИН запуск не идёт и не ждёт в очереди,
     а последний запуск стартовал больше CHECK_STALE_MIN минут назад —
     эстафета оборвана, перезапускаем check.yml.
  2. Очередь нарезки (jobs.json): задачи есть, но Clip video не запускался
     дольше CLIP_STALE_MIN минут и сейчас не идёт — перезапускаем clip.yml.
  3. Каждая починка коммитится в health.json (когда чинили и что) — видно
     в Actions и на сайте. Без починок коммитов НЕТ (репозиторий не пухнет).

Запускается как .github/workflows/watchdog.yml (cron */20 * * * *). Токен —
SC_PAT (нужны права actions:write на dispatch и contents:write на health).
"""

import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from urllib.request import Request, urlopen

API = "https://api.github.com"
CHECK_STALE_MIN = 35      # смена слежения идёт 55 мин; тишина 35+ мин = разрыв
CLIP_STALE_MIN = 100      # резервный cron Clip video — раз в 2 ч; тишина 100+ = стоп
HEALTH_JSON = "health.json"


def gh(*args):
    """gh CLI с токеном из GH_TOKEN (в workflow это SC_PAT)."""
    p = subprocess.run(["gh"] + [str(a) for a in args],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError("gh %s: %s" % (" ".join(str(a) for a in args[:3]),
                                          (p.stderr or "")[:300]))
    return p.stdout or ""


def _parse_ts(s):
    """ISO-время GitHub -> epoch (0, если распознать не удалось)."""
    if not s:
        return 0.0
    try:
        return datetime.strptime(str(s).replace("Z", "+0000"),
                                 "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return 0.0


def runs_of(workflow, limit=10):
    # ВНИМАНИЕ: поле runStartedAt старые gh не знают — берём createdAt
    out = gh("run", "list", "--repo", os.environ["GITHUB_REPOSITORY"],
             "--workflow", workflow, "--limit", str(limit),
             "--json", "databaseId,status,conclusion,createdAt")
    data = json.loads(out or "[]")
    for r in data:
        r["ts"] = _parse_ts(r.get("createdAt") or r.get("runStartedAt"))
    return data


def _active(runs):
    return [r for r in runs if r.get("status") in ("in_progress", "queued")]


def _last_started(runs):
    return max((r["ts"] for r in runs if r.get("ts")), default=0.0)


def repair_check():
    """Эстафета слежения оборвана? -> перезапуск check.yml."""
    runs = runs_of("check.yml")
    if _active(runs):
        return None
    last = _last_started(runs)
    if last and time.time() - last < CHECK_STALE_MIN * 60:
        return None                      # смена недавно кончилась, эстафета едет
    repo = os.environ["GITHUB_REPOSITORY"]
    gh("workflow", "run", "check.yml", "--repo", repo)
    silence = (time.time() - last) / 60 if last else -1
    return "check.yml: эстафета слежения молчала %.0f мин — перезапущена" % silence


def repair_clip():
    """Очередь нарезки стоит, а Clip video молчит? -> перезапуск clip.yml."""
    repo = os.environ["GITHUB_REPOSITORY"]
    try:
        out = gh("api", "repos/%s/contents/jobs.json" % repo,
                 "--jq", "(.jobs // []) | length")
        n_jobs = int((out or "0").strip() or "0")
    except Exception:
        return None                      # нет файла/не читается — не наша забота
    if not n_jobs:
        return None
    runs = runs_of("clip.yml")
    if _active(runs):
        return None
    last = _last_started(runs)
    if last and time.time() - last < CLIP_STALE_MIN * 60:
        return None
    gh("workflow", "run", "clip.yml", "--repo", repo)
    silence = (time.time() - last) / 60 if last else -1
    return ("clip.yml: в очереди нарезки %d задач, Clip video молчал %.0f мин "
            "— перезапущен" % (n_jobs, silence))


def write_health(repairs):
    """Одна починка за сессию -> коммит health.json (история в самом файле)."""
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GH_TOKEN") or ""
    headers = {"Authorization": "Bearer %s" % token,
               "Accept": "application/vnd.github+json",
               "User-Agent": "StreamClipper-watchdog"}
    sha, old = None, {}
    try:
        req = Request("%s/repos/%s/contents/%s" % (API, repo, HEALTH_JSON),
                      headers=headers)
        with urlopen(req, timeout=30) as r:
            j = json.load(r)
            sha = j.get("sha")
            try:
                old = json.loads(base64.b64decode(j.get("content") or b"").decode()
                                 or "{}")
            except Exception:
                old = {}
    except Exception:
        pass                             # файла нет — создадим без sha
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = {
        "updated": now,
        "repairs_total": int(old.get("repairs_total") or 0) + 1,
        "last_repair": {"when": now, "actions": repairs},
        "prev_repair": old.get("last_repair"),
    }
    payload = {"message": "watchdog: самолечение 24/7 — %s" % "; ".join(repairs),
               "content": base64.b64encode(json.dumps(
                   body, ensure_ascii=False, indent=1).encode()).decode("ascii")}
    if sha:
        payload["sha"] = sha
    req = Request("%s/repos/%s/contents/%s" % (API, repo, HEALTH_JSON),
                  data=json.dumps(payload).encode("utf-8"),
                  headers=dict(headers, **{"Content-Type": "application/json"}),
                  method="PUT")
    with urlopen(req, timeout=30) as r:
        r.read()


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("GITHUB_REPOSITORY не задан — режим GitHub Actions")
        return 0
    repairs = []
    for fn in (repair_check, repair_clip):
        try:
            res = fn()
        except Exception as e:
            print("watchdog: проверка не удалась: %s" % e, file=sys.stderr)
            res = None
        if res:
            repairs.append(res)
            print("watchdog: ПОЧИНЕНО — %s" % res)
    if repairs:
        try:
            write_health(repairs)
            print("watchdog: health.json обновлён")
        except Exception as e:
            print("watchdog: health.json не записан: %s" % e, file=sys.stderr)
    else:
        print("watchdog: всё живо — цепочки слежения и нарезки работают")
    return 0


if __name__ == "__main__":
    sys.exit(main())
