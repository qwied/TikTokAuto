"""Разовая проверка автопостинга TikTok из Actions (workflow «TikTok selftest»).

Публикует готовый клип из релиза репозитория ТОЧНО тем же кодом, что и
боевой автопостинг (tiktok.post_api): refresh-токен -> access -> init ->
загрузка -> статус публикации. Ротированный refresh_token сохраняется в
секреты тем же механизмом (_persist_refresh_token), что и в проде — это
одновременно и проверка самолечения OAuth.

Результат: свежий клип появляется в TikTok-аккаунте (пока приложение не
аудировано — «Только я»), в логе — publish_id и финальный статус.

Запуск: Actions -> TikTok selftest -> Run. Можно указать clip_id (пусто =
самый свежий из clips.json).
"""

import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tiktok import post_api                      # боевой путь публикации

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
           "privacy": "SELF_ONLY", "token_file": tf}
    ok = post_api(video, _caption(entry), cfg, tf)
    tok = json.load(open(tf, encoding="utf-8"))
    mins = int((float(tok.get("expires_at") or 0) - time.time()) / 60)
    rotated = "refresh_token РОТИРОВАН и сохранён в секреты" \
        if tok.get("refresh_token") != rt else "refresh_token остался прежним"
    print("selftest: post_api вернул %s; access_token живёт ещё %d мин; %s"
          % (ok, mins, rotated))
    print("selftest: ГОТОВО — видео в TikTok (пока «Только я», до аудита приложения)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
