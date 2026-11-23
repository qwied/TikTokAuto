"""Автопостинг готовых клипов в TikTok.

Два режима (config.yaml -> tiktok.mode):

  browser — окно Chrome с вашим входом в TikTok (cookies.txt из браузера).
            Находит страницу загрузки, вставляет файл, пишет описание,
            нажимает «Опубликовать». Просто начать, но TikTok иногда
            показывает капчу — окно открыто, решаете руками (captcha_wait).

  api     — официальный TikTok Content Posting API (developers.tiktok.com).
            Надёжнее и без капчи; приложение без аудита публикует только
            в «Только я» (SELF_ONLY) — после аудита можно PUBLIC.

Антибан: между публикациями случайная пауза delay_min..delay_max секунд,
описание не длиннее 2200 символов, очередность клипов соблюдается.

dry_run: true — ничего не выкладывает, только пишет в лог (для проверки).
"""

import json
import logging
import os
import queue
import random
import subprocess
import threading
import time

import requests

log = logging.getLogger("tiktok")

CAPTION_MAX = 2200
API_BASE = "https://open.tiktokapis.com"

DEFAULT_HASHTAGS = ["fyp", "foryou", "рек", "рекомендации", "стрим",
                    "клипы", "прикол", "хайп", "twitch", "live"]

# TikTok: приложение без аудита постит ТОЛЬКО в приватный аккаунт. Если
# аккаунт публичный — init отклоняется с этим кодом; дальнейшие попытки
# в сессии бессмысленны (клип НЕ теряется: он остаётся на сайте/в релизе).
UNAUDITED_MARK = "unaudited_client_can_only_post_to_private_accounts"
UNAUDITED_FIX = ("TikTok не публикует: приложение ещё без аудита, а аккаунт "
                 "ПУБЛИЧНЫЙ. Включи в TikTok «Приватный аккаунт» "
                 "(Профиль → Настройки → Конфиденциальность) — и автопостинг "
                 "заработает. После аудита приложения верни публичный.")


def build_caption(meta, hashtags=None):
    """Описание для TikTok: заголовок + канал + хэштеги, максимум 2200 символов."""
    meta = meta or {}
    tags = [t if str(t).startswith("#") else "#" + str(t)
            for t in (hashtags or DEFAULT_HASHTAGS) if str(t).strip()]
    parts = []
    if meta.get("title"):
        parts.append(str(meta["title"]).strip())
    if meta.get("channel"):
        parts.append(f"{meta['channel']} {meta.get('platform', '').lower()}".strip())
    if tags:
        parts.append(" ".join(tags))
    text = " ".join(p for p in parts if p).strip()
    return text[:CAPTION_MAX]


def load_cookies_netscape(path):
    """cookies.txt (Netscape) -> список dict для Selenium. Только .tiktok.com."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip("\n")
                if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                    continue
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                cols = line.split("\t")
                if len(cols) != 7:
                    continue
                domain, _flag, cpath, secure, expiry, name, value = cols
                if "tiktok" not in domain:
                    continue
                cookie = {
                    "name": name, "value": value,
                    "domain": domain, "path": cpath or "/",
                    "secure": secure.upper() == "TRUE",
                }
                if expiry.isdigit():
                    cookie["expiry"] = int(expiry)
                out.append(cookie)
    except OSError as e:
        raise RuntimeError(f"не удалось прочитать cookies {path}: {e}")
    if not out:
        raise RuntimeError("в cookies.txt нет cookies для tiktok.com — "
                           "экспортируйте заново (README, раздел «Автопостинг»)")
    return out


class TikTokPoster:
    """Очередь клипов -> публикация с случайной паузой (антибан)."""

    def __init__(self, cfg, base_dir="."):
        self.cfg = cfg or {}
        self.base_dir = str(base_dir)
        self.mode = str(self.cfg.get("mode", "browser")).lower()
        self.dry_run = bool(self.cfg.get("dry_run", False))
        self.hashtags = list(self.cfg.get("hashtags") or DEFAULT_HASHTAGS)
        self.delay_min = max(0, int(self.cfg.get("delay_min", 60)))
        self.delay_max = max(self.delay_min, int(self.cfg.get("delay_max", 300)))
        # в TikTok уходят только хайповые: клипы с оценкой ниже порога не постим
        self.min_score = max(0, int(self.cfg.get("min_score", 65)))
        self.q = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.posted = 0
        self.failed = 0
        self._blocked_unaudited = False   # увидели «нужен приватный аккаунт»
        self.notify = None                # Notifier.notify_text — тревога на телефон
        self._thread = None

    # --- готовность ------------------------------------------------------------

    def _resolve(self, name):
        p = str(self.cfg.get(name) or "")
        if not p:
            return ""
        return p if os.path.isabs(p) else os.path.join(self.base_dir, p)

    def ready(self):
        if self.dry_run:
            return True
        if self.mode == "api":
            ok = bool(self.cfg.get("client_key") and self.cfg.get("client_secret")
                      and os.path.isfile(self._resolve("token_file")))
            if not ok:
                log.warning("TikTok mode=api: заполните client_key/client_secret "
                            "и получите токен (python tiktok_auth.py)")
            return ok
        cookies = self._resolve("cookies_file")
        if not os.path.isfile(cookies):
            log.warning("TikTok mode=browser: нет файла %s — экспортируйте cookies "
                        "из браузера (README, раздел «Автопостинг»)", cookies)
            return False
        return True

    # --- жизненный цикл --------------------------------------------------------

    def start(self):
        if self.ready():
            self._thread = threading.Thread(target=self._worker, daemon=True,
                                            name="tiktok-poster")
            self._thread.start()
            log.info("TikTok-автопостинг включён: mode=%s%s",
                     self.mode, " (DRY-RUN)" if self.dry_run else "")

    def ensure_started(self):
        """Запустить воркер, если он ещё не работает (например, токен добавили
        с сайта после старта программы)."""
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self.ready():
            return False
        self._stop.clear()
        self.start()
        return self._thread is not None

    def stop(self):
        self._stop.set()

    def enqueue(self, video_path, caption):
        if self._thread is None:
            return False
        self.q.put((str(video_path), caption or ""))
        return True

    def status(self):
        with self._lock:
            return {
                "enabled": self._thread is not None,
                "mode": self.mode,
                "queued": self.q.qsize(),
                "posted": self.posted,
                "failed": self.failed,
                "min_score": self.min_score,
            }

    # --- воркер ----------------------------------------------------------------

    def _worker(self):
        while not self._stop.is_set():
            try:
                path, caption = self.q.get(timeout=2.0)
            except queue.Empty:
                continue
            if self._blocked_unaudited:
                log.warning("TikTok: %s пропущен — аккаунт публичный, а без "
                            "аудита постить можно только в приватный аккаунт "
                            "(клип остаётся на сайте и в релизе)",
                            os.path.basename(path))
                self.q.task_done()
                continue
            delay = random.randint(self.delay_min, self.delay_max) \
                if self.delay_max > self.delay_min else self.delay_min
            if delay and not self.dry_run:
                log.info("TikTok: публикация через %d сек (пауза против блокировок)",
                         delay)
            if self._sleep(delay) and not self._stop.is_set():
                try:
                    self._post(path, caption)
                    with self._lock:
                        self.posted += 1
                    log.info("TikTok: опубликовано %s", os.path.basename(path))
                except Exception as e:
                    with self._lock:
                        self.failed += 1
                    if UNAUDITED_MARK in str(e):
                        self._blocked_unaudited = True
                        log.warning("TikTok: %s", UNAUDITED_FIX)
                        if self.notify is not None:
                            try:
                                self.notify.notify_text(
                                    UNAUDITED_FIX,
                                    title="StreamClipper: нужен приватный аккаунт")
                            except Exception:
                                log.exception("Тревога TikTok не ушла")
                    else:
                        log.error("TikTok: не удалось опубликовать %s: %s",
                                  os.path.basename(path), e)
            self.q.task_done()

    def _sleep(self, seconds):
        """Пауза с проверкой stop. Возвращает True, если доспали до конца."""
        end = time.time() + max(0, seconds)
        while time.time() < end:
            if self._stop.is_set():
                return False
            time.sleep(0.5)
        return not self._stop.is_set()

    def _post(self, video_path, caption):
        if self.dry_run:
            log.info("TikTok DRY-RUN: выложил бы %s («%s»)",
                     os.path.basename(video_path), caption[:80])
            return
        if self.mode == "api":
            post_api(video_path, caption, self.cfg, self._resolve("token_file"))
        else:
            post_browser(video_path, caption, self.cfg,
                         self._resolve("cookies_file"))


# --- режим browser (Selenium) ---------------------------------------------------

def post_browser(video_path, caption, cfg, cookies_file):
    """Публикация через окно Chrome, в котором вы залогинены в TikTok."""
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError as e:
        raise RuntimeError("не установлен selenium — выполните: pip install selenium") from e

    cfg = cfg or {}
    opts = webdriver.ChromeOptions()
    if cfg.get("headless"):
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--lang=ru")
    driver = webdriver.Chrome(options=opts)     # selenium сам скачает драйвер
    captcha_wait = max(0, int(cfg.get("captcha_wait", 90)))
    try:
        driver.get("https://www.tiktok.com/")
        for c in load_cookies_netscape(cookies_file):
            try:
                driver.add_cookie(c)
            except Exception:
                pass
        driver.get("https://www.tiktok.com/upload?lang=ru")

        # не выкинуло ли на страницу входа?
        time.sleep(3)
        if "/login" in driver.current_url or "/auth" in driver.current_url:
            raise RuntimeError("TikTok просит вход — cookies просрочены, "
                               "экспортируйте заново")

        # файл
        inp = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']")))
        inp.send_keys(os.path.abspath(str(video_path)))

        # описание
        try:
            box = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                                            "div.public-DraftEditor-content, "
                                            "[contenteditable='true']")))
            box.click()
            box.send_keys(caption[:CAPTION_MAX])
        except Exception as e:
            raise RuntimeError("не нашёл поле описания (TikTok поменял страницу?): " + str(e))

        # капча? — даём время решить руками
        _wait_captcha(driver, captcha_wait)

        # кнопка «Опубликовать» активируется после загрузки видео
        btn = WebDriverWait(driver, 600).until(
            lambda d: next((b for b in d.find_elements(
                By.CSS_SELECTOR, "button[data-e2e='post-button']")
                if b.is_enabled() and b.is_displayed()), None))
        btn.click()
        # ждём подтверждение публикации (тост/редирект)
        WebDriverWait(driver, 120).until(
            lambda d: "/upload" not in d.current_url
            or _has_text(d, ["published", "опублик", "загружен"]))
        time.sleep(2)
        return True
    finally:
        driver.quit()


def _has_text(driver, words):
    try:
        body = driver.find_element("tag name", "body").text.lower()
        return any(w in body for w in words)
    except Exception:
        return False


def _wait_captcha(driver, wait_seconds):
    """Если TikTok показывает капчу — держим окно открытым для ручного решения."""
    if wait_seconds <= 0:
        return
    end = time.time() + wait_seconds
    warned = False
    while time.time() < end:
        try:
            iframes = driver.find_elements("css selector",
                                           "iframe[src*='captcha'], iframe[title*='captcha']")
            if iframes:
                if not warned:
                    log.warning("TikTok: КАПЧА — решите её в открывшемся окне "
                                "(%d сек ожидания)", wait_seconds)
                    warned = True
            elif warned:
                return
        except Exception:
            return
        time.sleep(1.5)


# --- режим api (официальный Content Posting API) --------------------------------

def _load_token(token_file):
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise RuntimeError("нет токена TikTok — запустите: python tiktok_auth.py")


def _save_token(token_file, data):
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _persist_refresh_token(new_rt):
    """GitHub Actions: ротированный refresh_token обратно в секрет репозитория.

    TikTok выдаёт НОВЫЙ refresh_token при каждом обновлении, а раннер
    эфемерный — если не перезаписать секрет TIKTOK_REFRESH_TOKEN, следующий
    запуск не сможет обновить доступ. PAT берём из GH_TOKEN/SC_PAT (в наших
    workflow это один и тот же PAT с правами repo). Ошибки не роняем —
    постинг в этом запуске уже состоялся.
    """
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    pat = os.environ.get("SC_PAT") or os.environ.get("GH_TOKEN") or ""
    if not (repo and pat and new_rt):
        return False
    try:
        env = dict(os.environ)
        env["GH_TOKEN"] = pat
        p = subprocess.run(
            ["gh", "secret", "set", "TIKTOK_REFRESH_TOKEN",
             "--repo", repo, "--body", new_rt],
            env=env, capture_output=True, text=True, timeout=90)
        if p.returncode == 0:
            log.info("TikTok: новый refresh_token сохранён в секреты репозитория")
            return True
        log.warning("TikTok: gh secret set вернулся с ошибкой: %s",
                    (p.stderr or "").strip()[:200])
    except Exception as e:
        log.warning("TikTok: не удалось сохранить refresh_token в секреты: %s", e)
    return False


def _ensure_access_token(cfg, token_file):
    tok = _load_token(token_file)
    if float(tok.get("expires_at", 0)) > time.time() + 60:
        return tok["access_token"]
    r = requests.post(API_BASE + "/v2/oauth/token/", data={
        "client_key": cfg.get("client_key"),
        "client_secret": cfg.get("client_secret"),
        "grant_type": "refresh_token",
        "refresh_token": tok.get("refresh_token", ""),
    }, timeout=30)
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError("не удалось обновить токен: " + json.dumps(j)[:300])
    tok["access_token"] = j["access_token"]
    tok["refresh_token"] = j.get("refresh_token", tok.get("refresh_token"))
    tok["expires_at"] = time.time() + int(j.get("expires_in", 86400))
    _save_token(token_file, tok)
    _persist_refresh_token(tok.get("refresh_token", ""))
    return tok["access_token"]


def post_api(video_path, caption, cfg, token_file):
    """Публикация через официальный TikTok Content Posting API."""
    cfg = cfg or {}
    size = os.path.getsize(video_path)
    privacy = str(cfg.get("privacy") or "SELF_ONLY")
    token = _ensure_access_token(cfg, token_file)
    r = requests.post(API_BASE + "/v2/post/publish/video/init/",
                      headers={"Authorization": "Bearer " + token},
                      json={
                          "post_info": {
                              "title": caption[:CAPTION_MAX],
                              "privacy_level": privacy,
                          },
                          "source_info": {
                              "source": "FILE_UPLOAD",
                              "video_size": size,
                              "chunk_size": size,
                              "total_chunk_count": 1,
                          },
                      }, timeout=60)
    j = r.json()
    err = (j.get("error") or {}).get("code", "?")
    if err != "ok" or not (j.get("data") or {}).get("upload_url"):
        raise RuntimeError("TikTok init отказал (%s): %s" %
                           (err, json.dumps(j)[:300]))
    data = j["data"]
    with open(video_path, "rb") as f:
        put = requests.put(data["upload_url"], data=f,
                           headers={
                               "Content-Type": "video/mp4",
                               "Content-Range": f"bytes 0-{size - 1}/{size}",
                           }, timeout=600)
    if put.status_code not in (200, 201, 206):
        raise RuntimeError(f"TikTok не принял файл: HTTP {put.status_code}")
    # статус публикации
    publish_id = data.get("publish_id")
    for _ in range(40):
        time.sleep(3)
        s = requests.post(API_BASE + "/v2/post/publish/status/fetch/",
                          headers={"Authorization": "Bearer " + token},
                          json={"publish_id": publish_id}, timeout=30).json()
        st = (s.get("data") or {}).get("status", "")
        if "COMPLETE" in st.upper():
            return True
        if (s.get("error") or {}).get("code", "ok") not in ("ok", ""):
            raise RuntimeError("TikTok status: " + json.dumps(s)[:300])
    log.warning("TikTok: статус не подтверждён за 2 минуты (видео обычно "
                "появится через пару минут)")
    return True
