"""Проверка автопостинга TikTok из Actions (workflow «TikTok selftest»).

Публикует готовый клип из релиза репозитория ТОЧНО тем же кодом, что и
боевой автопостинг (tiktok.post_api): refresh-токен -> access -> init ->
загрузка -> статус. Ротированный refresh_token сохраняется в секреты тем
же механизмом (_persist_refresh_token), что и в проде — это одновременно
и проверка самолечения OAuth.

Цель — ПУБЛИЧНАЯ публикация (PUBLIC_TO_EVERYONE). Если TikTok отвечает
«приложение без аудита» — selftest печатает точную инструкцию аудита
(это ЕДИНСТВЕННЫЙ официальный путь к публичным постам через API).
Когда аудит пройдёт: selftest опубликует клип ПУБЛИЧНО и запустит
Clip video, чтобы отложенная очередь (tiktok_pending.json) разгрузилась.

Запуск: Actions -> TikTok selftest -> Run (или Watchdog раз в 3 часа
при непустой очереди). Можно указать clip_id (пусто = самый свежий).
"""

import json
import os
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tiktok import post_api, UnauditedClientError        # боевой путь публикации

API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "")


def _pick_entry(clips):
    want = os.environ.get("TIKTOK_SELFTEST_CLIP_ID", "").strip()
    if want:
        for c in clips:
            if str(c.get("id") or "") == want:
                return c
        raise SystemExit("selftest: клип %s не найден в clips.json" % want)
    return clips[0]                              # самый свежий


def _dispatch_clip_video():
    """Публикация удалась — TikTok снова разрешает посты: разгрузить очередь
    отложенных клипов (Clip video возвращает tiktok_pending.json в постинг)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return
    try:
        env = dict(os.environ)
        env["GH_TOKEN"] = os.environ.get("GH_TOKEN") or ""
        p = subprocess.run(
            ["gh", "workflow", "run", "clip.yml", "--repo", repo],
            env=env, capture_output=True, text=True, timeout=90)
        if p.returncode == 0:
            print("selftest: запустил Clip video — отложенная очередь "
                  "TikTok разгрузится (клипы выйдут ПУБЛИЧНО)")
        else:
            print("selftest: clip.yml не запущен: %s"
                  % (p.stderr or "").strip()[:200])
    except Exception as e:
        print("selftest: clip.yml не запущен: %s" % e)


def _caption(entry):
    cap = str(entry.get("caption") or "").strip()
    tags = " ".join(str(t) for t in (entry.get("hashtags") or [])[:12])
    title = str(entry.get("title") or entry.get("display") or "клип со стрима")
    if not cap:
        cap = "«%s»" % title
    return ("%s %s" % (cap, tags)).strip()[:2200]


def main():
    token = os.environ.get("GH_TOKEN") or ""
    ck = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    cs = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    rt = os.environ.get("TIKTOK_REFRESH_TOKEN", "").strip()
    if not (REPO and token and ck and cs and rt):
        print("selftest: нет секретов (GH_TOKEN/TIKTOK_*) — проверка невозможна")
        return 2
    headers = {"Authorization": "Bearer %s" % token,
               "Accept": "application/vnd.github.raw",
               "User-Agent": "StreamClipper-selftest"}
    r = requests.get("%s/repos/%s/contents/clips.json" % (API, REPO),
                     headers=headers, timeout=30)
    r.raise_for_status()
    clips = (r.json() or {}).get("clips") or []
    if not clips:
        print("selftest: clips.json пуст — клипов ещё нет, постить нечего")
        return 1
    entry = _pick_entry(clips)
    url = entry.get("url") or ""
    print("selftest: публикую клип %s (score %s) — %s"
          % (entry.get("id"), entry.get("score"), url))
    rr = requests.get(url, timeout=600,
                      headers={"User-Agent": "StreamClipper-selftest"})
    rr.raise_for_status()
    video = "selftest_clip.mp4"
    with open(video, "wb") as f:
        f.write(rr.content)
    print("selftest: скачан (%.1f МБ)" % (len(rr.content) / 1e6))

    tf = "tiktok_tokens_selftest.json"
    with open(tf, "w", encoding="utf-8") as f:
        json.dump({"access_token": "", "refresh_token": rt,
                   "expires_at": 0, "open_id": "", "scope": ""}, f)
    cfg = {"client_key": ck, "client_secret": cs,
           "privacy": "PUBLIC_TO_EVERYONE", "token_file": tf}
    try:
        ok = post_api(video, _caption(entry), cfg, tf)
    except UnauditedClientError:
        print("selftest: TikTok отклонил публикацию: приложение ещё БЕЗ АУДИТА, "
              "а аккаунт публичный — так TikTok ограничивает все неаудированные "
              "приложения.")
        print("  ПУБЛИЧНЫЕ посты = подать приложение на аудит (единственный "
              "официальный путь, вручную ~5 минут):")
        print("  1) developers.tiktok.com -> My apps -> ваше приложение")
        print("  2) Products -> Content Posting API -> Direct Post -> Apply")
        print("  3) в форме указать:")
        print("     - Privacy policy: https://qwied.github.io/TikTokAuto/privacy.html")
        print("     - Terms of service: https://qwied.github.io/TikTokAuto/terms.html")
        print("     - записать короткое видео-демо: сайт со списком клипов + "
              "описание авто-постинга")
        print("  4) Submit for review — TikTok отвечает за несколько дней")
        print("  После одобрения НИЧЕГО ДЕЛАТЬ НЕ НАДО: система сама начнёт "
              "публиковать клипы ПУБЛИЧНО с хэштегами, отложенная очередь "
              "(tiktok_pending.json) разгрузится автоматически.")
        return 3
    except RuntimeError as e:
        if "unaudited_client_can_only_post_to_private_accounts" in str(e):
            print("selftest: TikTok отклонил публикацию (приложение без аудита) "
                  "— см. инструкцию аудита выше в документации проекта")
            return 3
        raise
    tok = json.load(open(tf, encoding="utf-8"))
    mins = int((float(tok.get("expires_at") or 0) - time.time()) / 60)
    rotated = "refresh_token РОТИРОВАН и сохранён в секреты" \
        if tok.get("refresh_token") != rt else "refresh_token остался прежним"
    print("selftest: post_api вернул %s; access_token живёт ещё %d мин; %s"
          % (ok, mins, rotated))
    print("selftest: ГОТОВО — клип опубликован ПУБЛИЧНО (PUBLIC_TO_EVERYONE) "
          "с хэштегами!")
    _dispatch_clip_video()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
