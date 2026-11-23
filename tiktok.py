"""Автопостинг готовых клипов в TikTok.

Два режима (config.yaml -> tiktok.mode):

  browser — окно Chrome с вашим входом в TikTok (cookies.txt из браузера).
            Находит страницу загрузки, вставляет файл, пишет описание,
            нажимает «Опубликовать». Просто начать, но TikTok иногда
            показывает капчу — окно открыто, решаете руками (captcha_wait).

  api     — официальный TikTok Content Posting API (developers.tiktok.com).
            Надёжнее и без капчи. Цель — ПУБЛИЧНЫЕ публикации (PUBLIC_TO_
            EVERYONE); пока приложение не прошло аудит TikTok, API разрешает
            только приватный режим, поэтому модуль сам пробует PUBLIC и
            при отказе аккуратно падает на SELF_ONLY. Клипы при любом сбое
            НЕ ТЕРЯЮТСЯ: они сохраняются в очередь tiktok_pending.json
            (доливается в репозиторий между запусками Actions) и
            публикуются автоматически, как только TikTok разрешит.

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

CAPTION_MAX = 2200          # лимит TikTok: описание до 2200 символов (UTF-16)
API_BASE = "https://open.tiktokapis.com"

# База хэштегов (лучшие для русскоязычных рекомендаций + глобальный охват).
# Игровые теги подмешиваются автоматически по названию стрима (см. GAME_TAGS).
DEFAULT_HASHTAGS = ["fyp", "foryou", "рек", "рекомендации", "стрим",
                    "клипы", "прикол", "хайп", "twitch", "viral"]

# Ключевые слова в названии стрима -> игровые хэштеги TikTok (первыми
# в описании: конкретные теги дают рекомендации сильнее общих).
GAME_TAGS = [
    ("cs2", ("cs2", "cs:go", "csgo", "counter-strike", "counter strike",
             "ксго", "контра", "кс ", "ксв")),
    ("dota2", ("dota", "дота", "дотер")),
    ("standoff2", ("standoff", "stand off", "стандофф")),
    ("valorant", ("valorant", "валорант", "валор")),
    ("minecraft", ("minecraft", "майнкрафт", "майн", "hidragox", "хайпиксель")),
    ("gta5", ("gta", "гта", "аризона", "majestic", "radmir", "grand theft")),
    ("pubg", ("pubg", "пабг")),
    ("apexlegends", ("apex", "апекс")),
    ("fortnite", ("fortnite", "фортнайт", "форнайт")),
    ("rust", ("rust ", "rust,", "rust!", "раст ", "раст,", "rust main")),
    ("roblox", ("roblox", "роблокс", "блокс")),
    ("warzone", ("warzone", "call of duty", "кол оф дьюти", " cod", "cod ",
                 "codm")),
    ("brawlstars", ("brawl stars", "бравл", "brawl")),
    ("genshin", ("genshin", "геншин")),
    ("react", ("реакция", "реакции", "react", "стрим-реакция")),
]

# TikTok: неаудированное приложение разрешает ТОЛЬКО приватный режим, а с
# ПУБЛИЧНЫМ аккаунтом API отклоняет публикацию целиком с этим кодом.
UNAUDITED_MARK = "unaudited_client_can_only_post_to_private_accounts"
UNAUDITED_FIX = (
    "TikTok сам ограничивает приложения без аудита: видео выходят только «Только "
    "я», а с ПУБЛИЧНЫМ аккаунтом API отклоняет пост целиком. Клип сохранён в "
    "очередь (tiktok_pending.json) — НИЧЕГО НЕ ТЕРЯЕТСЯ: после аудита приложения "
    "система сама выложит его ПУБЛИЧНО с хэштегами. Аудит: developers.tiktok.com "
    "→ ваше приложение → Products → Content Posting API → Direct Post → Apply: "
    "заполнить форму (privacy policy: https://qwied.github.io/TikTokAuto/privacy.html, "
    "terms: https://qwied.github.io/TikTokAuto/terms.html), приложить демо-видео "
    "и отправить на проверку. Обычно проверка занимает несколько дней."
)

# Переходные ошибки TikTok/сети: ретраим с паузой (самолечение)
RETRYABLE_CODES = {"rate_limit_exceeded", "internal_server_error",
                   "speed_limit_exceeded", "service_unavailable"}


class UnauditedClientError(RuntimeError):
    """TikTok: приложение без аудита, а аккаунт публичный — API запретил пост.

    Клип НЕ потерян: воркер сохранит его в tiktok_pending.json, и после
    аудита приложения очередь разгрузится сама."""


def detect_game_tags(title):
    """Игровые хэштеги из названия стрима (без решёток, максимум 3)."""
    t = str(title or "").lower()
    if not t:
        return []
    out = []
    for tag, keys in GAME_TAGS:
        if any(k in t for k in keys) and tag not in out:
            out.append(tag)
            if len(out) >= 3:
                break
    return out


def build_caption(meta, hashtags=None):
    """Описание для TikTok: заголовок + канал + игровые и базовые хэштеги.

    Максимум 2200 символов; если длинный заголовок не оставляет места
    хэштегам — заголовок ужмётся, хэштеги не потеряются."""
    meta = meta or {}
    tags = [t if str(t).startswith("#") else "#" + str(t)
            for t in (hashtags or DEFAULT_HASHTAGS) if str(t).strip()]
    title = str(meta.get("title") or "").strip()
    # игровые теги — в начало описания (конкретика лучше общих тегов)
    game = ["#" + g for g in detect_game_tags(title)]
    if game:
        tags = game + [t for t in tags if t not in game]
    parts = []
    if title:
        parts.append(title)
    if meta.get("channel"):
        parts.append(f"{meta['channel']} {meta.get('platform', '').lower()}".strip())
    if tags:
        parts.append(" ".join(tags))
    text = " ".join(p for p in parts if p).strip()
    if len(text) > CAPTION_MAX and tags:
        # ужать заголовок, но сохранить хэштеги (они дают рекомендации)
        tail_parts = []
        if meta.get("channel"):
            tail_parts.append(
                f"{meta['channel']} {meta.get('platform', '').lower()}".strip())
        tail_parts.append(" ".join(tags))
        tail = " ".join(p for p in tail_parts if p)
        room = CAPTION_MAX - len(tail) - (1 if tail else 0)
        if room > 40:
            text = (title[:room] + " " + tail).strip()
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


PENDING_FILE = "tiktok_pending.json"   # очередь неудавшихся постов (файл в репо)
PENDING_TRIES_MAX = 3        # столько попыток скачать клип из релиза, потом дроп


class TikTokPoster:
    """Очередь клипов -> публикация с случайной паузой (антибан).

    Самолечение: любой сбой публикации (сеть, 5xx, запрет без аудита) НЕ
    теряет клип — он попадает в tiktok_pending.json; requeue_pending()
    вернет его в очередь при следующем запуске (Actions: файл доливается
    в репозиторий между запусками)."""

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
        # база ссылок релиза для восстановления очереди после смерти раннера
        self.release_url_base = str(self.cfg.get("release_url_base") or "")
        self.pending_file = os.path.join(self.base_dir, PENDING_FILE)
        self.q = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._load_pending_local()
        self.posted = 0
        self.failed = 0
        self._blocked_unaudited = False   # увидели «нужен аудит» — дальше в pending
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

    def enqueue(self, video_path, caption, url=None):
        if self._thread is None:
            return False
        if url and not self.release_url_base:
            self.release_url_base = str(url).rsplit("/", 1)[0] if "/" in str(url) else ""
        if self._blocked_unaudited:
            # дальше все равно откажет: сразу в сохранённую очередь, клип жив
            log.info("TikTok: %s — в отложенную очередь (ждём аудит)",
                     os.path.basename(str(video_path)))
            self._pending_add(video_path, caption,
                              "приложение без аудита, аккаунт публичный")
            return True
        self.q.put((str(video_path), caption or ""))
        return True

    def status(self):
        with self._lock:
            st = {
                "enabled": self._thread is not None,
                "mode": self.mode,
                "queued": self.q.qsize(),
                "posted": self.posted,
                "failed": self.failed,
                "min_score": self.min_score,
            }
        with self._pending_lock:
            st["pending"] = len(self._pending)
        return st

    # --- воркер ----------------------------------------------------------------

    def _worker(self):
        while not self._stop.is_set():
            try:
                path, caption = self.q.get(timeout=2.0)
            except queue.Empty:
                continue
            if self._blocked_unaudited:
                # приложение без аудита: не спамим API — клип ждёт в очереди
                self._pending_add(path, caption, "приложение без аудита")
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
                    self._pending_done(path)
                    log.info("TikTok: опубликовано %s", os.path.basename(path))
                except Exception as e:
                    with self._lock:
                        self.failed += 1
                    # клип НЕ теряем — в сохранённую очередь (протаем и после аудита)
                    self._pending_add(path, caption, str(e))
                    if isinstance(e, UnauditedClientError) or UNAUDITED_MARK in str(e):
                        self._blocked_unaudited = True
                        log.warning("TikTok: %s", UNAUDITED_FIX)
                        if self.notify is not None:
                            try:
                                self.notify.notify_text(
                                    UNAUDITED_FIX,
                                    title="StreamClipper: TikTok ждёт аудит")
                            except Exception:
                                log.exception("Тревога TikTok не ушла")
                    else:
                        log.error("TikTok: не удалось опубликовать %s: %s "
                                  "(клип в отложенной очереди)",
                                  os.path.basename(path), e)
            self.q.task_done()

    def stop(self):
        self._stop.set()
        # раннер Actions умирает: что осталось в очереди — в сохранённую очередь
        try:
            while True:
                path, caption = self.q.get_nowait()
                self._pending_add(path, caption, "запуск завершается")
                self.q.task_done()
        except queue.Empty:
            pass

    def _sleep(self, seconds):
        """Пауза с проверкой stop. Возвращает True, если доспали до конца."""
        end = time.time() + max(0, seconds)
        while time.time() < end:
            if self._stop.is_set():
                return False
            time.sleep(0.5)
        return not self._stop.is_set()

    # --- отложенная очередь (самолечение: клипы не теряются) --------------------

    def _load_pending_local(self):
        try:
            with open(self.pending_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._pending = {str(e.get("id")): dict(e) for e in data
                                 if isinstance(e, dict) and e.get("id")}
            elif isinstance(data, dict):
                self._pending = {str(k): dict(v) for k, v in data.items()
                                 if isinstance(v, dict)}
        except Exception:
            self._pending = {}

    def _save_pending(self):
        """Записать файл очереди (вызывать только под _pending_lock)."""
        try:
            items = sorted(self._pending.values(),
                           key=lambda e: float(e.get("ts") or 0))
            with open(self.pending_file, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
        except OSError:
            pass

    def pending_entries(self):
        with self._pending_lock:
            return [dict(e) for e in self._pending.values()]

    def _file_url(self, video_path):
        base = str(self.release_url_base or "").rstrip("/")
        name = os.path.basename(str(video_path))
        return f"{base}/{name}" if base and name else ""

    def _pending_add(self, video_path, caption, reason=""):
        vid = os.path.basename(str(video_path))
        if not vid:
            return
        with self._pending_lock:
            old = self._pending.get(vid) or {}
            self._pending[vid] = {
                "id": vid,
                "caption": caption or old.get("caption") or "",
                "url": old.get("url") or self._file_url(video_path),
                "ts": time.time(),
                "tries": int(old.get("tries") or 0),
                "last_error": str(reason or "")[:200],
            }
            self._save_pending()

    def _pending_done(self, video_path):
        vid = os.path.basename(str(video_path))
        with self._pending_lock:
            if vid in self._pending:
                del self._pending[vid]
                self._save_pending()

    def load_pending(self, entries):
        """Слить очередь из репозитория (свежая запись побеждает). Возвращает
        число добавленных клипов."""
        n = 0
        with self._pending_lock:
            for e in entries or []:
                eid = str((e or {}).get("id") or "")
                if not eid:
                    continue
                old = self._pending.get(eid)
                if old is None or float(e.get("ts") or 0) >= float(old.get("ts") or 0):
                    self._pending[eid] = dict(e)
                    n += 1
            self._save_pending()
        return n

    def requeue_pending(self, downloader=None, limit=4, tries_max=PENDING_TRIES_MAX):
        """Вернуть неудавшиеся клипы в очередь на публикацию.

        downloader(url, dest_path) — скачать файл из релиза (на Actions раннер
        чистый, файла локально нет). Клипы без ссылки и файла после
        tries_max неудач выбрасываются (релиз мог быть вычищен)."""
        if limit <= 0:
            return 0
        n = 0
        for e in self.pending_entries():
            if n >= limit:
                break
            vid = str(e.get("id") or "")
            if not vid:
                continue
            dest = os.path.join(self.base_dir, vid)
            url = str(e.get("url") or "")
            if not os.path.isfile(dest):
                if not url or downloader is None:
                    continue
                if int(e.get("tries") or 0) >= tries_max:
                    with self._pending_lock:
                        self._pending.pop(vid, None)
                        self._save_pending()
                    log.info("TikTok: очередь — %s не восстанавливается "
                             "(%d неудач), выброшен", vid, tries_max)
                    continue
                try:
                    downloader(url, dest)
                except Exception as ex:
                    with self._pending_lock:
                        ent = self._pending.get(vid)
                        if ent is not None:
                            ent["tries"] = int(ent.get("tries") or 0) + 1
                            ent["last_error"] = f"download: {ex}"[:200]
                            self._save_pending()
                    log.warning("TikTok: очередь — не скачался %s: %s", vid, ex)
                    continue
            self.enqueue(dest, str(e.get("caption") or ""))
            n += 1
        if n:
            log.info("TikTok: в очередь возвращено %d неудавшихся клипов "
                     "(самолечение)", n)
        return n

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


def _ensure_access_token(cfg, token_file, force_refresh=False):
    tok = _load_token(token_file)
    if not force_refresh and float(tok.get("expires_at", 0)) > time.time() + 60:
        return tok["access_token"]
    r = requests.post(API_BASE + "/v2/oauth/token/", data={
        "client_key": cfg.get("client_key"),
        "client_secret": cfg.get("client_secret"),
        "grant_type": "refresh_token",
        "refresh_token": tok.get("refresh_token", ""),
    }, timeout=30)
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError("token refresh: HTTP %s %s" % (r.status_code, r.text[:200]))
    if "access_token" not in j:
        raise RuntimeError("не удалось обновить токен: " + json.dumps(j)[:300])
    tok["access_token"] = j["access_token"]
    tok["refresh_token"] = j.get("refresh_token", tok.get("refresh_token"))
    tok["expires_at"] = time.time() + int(j.get("expires_in", 86400))
    _save_token(token_file, tok)
    _persist_refresh_token(tok.get("refresh_token", ""))
    return tok["access_token"]


def _init_post(token, privacy, caption, size):
    """init-запрос Direct Post. Возвращает (http_status, json)."""
    r = requests.post(API_BASE + "/v2/post/publish/video/init/",
                      headers={"Authorization": "Bearer " + token},
                      json={
                          "post_info": {
                              "title": caption,
                              "privacy_level": privacy,
                          },
                          "source_info": {
                              "source": "FILE_UPLOAD",
                              "video_size": size,
                              "chunk_size": size,
                              "total_chunk_count": 1,
                          },
                      }, timeout=60)
    try:
        j = r.json()
    except ValueError:
        j = {"error": {"code": "http_%d" % r.status_code,
                        "message": r.text[:300]}}
    return r.status_code, j


def post_api(video_path, caption, cfg, token_file):
    """Публикация через официальный TikTok Content Posting API.

    Публичность: пробуем privacy из конфига (PUBLIC_TO_EVERYONE); TikTok без
    аудита отклоняет — тогда автоматом пробуем SELF_ONLY (это сработает,
    если аккаунт приватный; у публичного аккаунта будет UnauditedClientError).
    Самолечение: сеть/5xx/rate-limit ретраятся с паузой; протухший токен
    обновляется форс-рефрешем прямо во время публикации."""
    cfg = cfg or {}
    size = os.path.getsize(video_path)
    privacy = str(cfg.get("privacy") or "PUBLIC_TO_EVERYONE")
    caption = str(caption or "")[:CAPTION_MAX]

    data, token = None, ""
    refreshed = False
    last_err = "?"
    for attempt in range(3):
        if attempt:
            time.sleep((20, 60)[min(attempt - 1, 1)])
        try:
            token = _ensure_access_token(cfg, token_file, force_refresh=refreshed)
            status, j = _init_post(token, privacy, caption, size)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = f"сеть: {e}"[:200]
            log.warning("TikTok init: %s (попытка %d/3, повторим)", last_err,
                        attempt + 1)
            continue
        err = (j.get("error") or {}).get("code", "?")
        if err == "ok" and (j.get("data") or {}).get("upload_url"):
            data = j["data"]
            break
        last_err = "TikTok init отказал (%s): %s" % (err, json.dumps(j)[:300])
        # протухший между refresh и init токен — один форс-рефреш и заново
        if not refreshed and err in ("invalid_access_token", "access_token_invalid",
                                     "authorization_failed"):
            refreshed = True
            log.warning("TikTok init: токен отклонён (%s) — форс-рефреш", err)
            continue
        # неаудированное приложение: сначала пробуем SELF_ONLY (для приватных
        # аккаунтов это работает), у публичного аккаунта — честная ошибка
        if err == UNAUDITED_MARK:
            if privacy != "SELF_ONLY":
                log.warning("TikTok: %s отклонён без аудита — пробую SELF_ONLY "
                            "(авто-фоллбэк)", privacy)
                privacy = "SELF_ONLY"
                continue
            raise UnauditedClientError(UNAUDITED_FIX)
        # переходные сбои — ретраим (самолечение)
        if err in RETRYABLE_CODES or status >= 500:
            log.warning("TikTok init: %s (HTTP %s) — попытка %d/3", err, status,
                        attempt + 1)
            continue
        raise RuntimeError(last_err)         # другая постоянная ошибка
    if data is None:
        raise RuntimeError(last_err)
    # загрузка файла: тоже ретраим сеть (большой файл, мобильный интернет)
    for attempt in range(2):
        try:
            with open(video_path, "rb") as f:
                put = requests.put(data["upload_url"], data=f,
                                   headers={
                                       "Content-Type": "video/mp4",
                                       "Content-Range": f"bytes 0-{size - 1}/{size}",
                                   }, timeout=600)
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt:
                raise RuntimeError(f"TikTok не принял файл (сеть): {e}") from e
            log.warning("TikTok upload: сеть оборвалась (%s) — повтор", e)
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
