/* StreamClipper — логика веб-приложения (офлайн-PWA).
   Работает с GitHub Pages (https), напрямую с сервера ПК (http://IP:8765)
   и ПОЛНОСТЬЮ офлайн: сервис-воркер держит оболочку сайта, скачанные клипы
   хранятся копиями в IndexedDB — «Загруженное» и плеер работают без сети.
   Скачанное исчезает из списка и попадает в «Загруженное» (кнопка в углу). */

(function () {
  "use strict";

  // --- состояние ---------------------------------------------------------------

  var LS_SERVER = "sc.server";
  var LS_DOWNLOADED = "sc.downloaded";   // { "<server|github>": { "<clipId>": {...} } }
  var LS_GH_REPO = "sc.ghrepo";          // репозиторий GitHub (owner/name)
  var LS_GH_TOKEN = "sc.ghtoken";        // fine-grained токен (Contents: Read and write)
  var LS_PENDING = "sc.pending";         // каналы, добавленные с телефона, до синхронизации
  var LS_QUALITY = "sc.quality";         // качество клипов: язык субтитров, строгость отбора
  var LS_FEEDBACK = "sc.feedback";       // оценки «Интересно/Скучно» до синхронизации
  var LS_REQUESTS = "sc.requests";       // «Ещё отсюда» — запросы нарезки соседних минут
  var LS_HIDDEN = "sc.hidden";           // клипы, скрытые крестиком на карточке
  var OFFLINE_CAP = 150 * 1024 * 1024;   // офлайн-копия: не сохраняем файлы больше 150 МБ

  var state = {
    server: null,          // базовый адрес сервера, напр. http://192.168.1.5:8765
    clips: [],             // последний ответ /api/clips (или clips.json в GitHub-режиме)
    downloaded: {},        // { id: {file, size, ts} } для текущего сервера
    knownIds: {},          // уже показанные карточки (для анимации «новый»)
    lastStatus: {},        // последний ответ /api/status (каналы + TikTok)
    online: false,
    everLoaded: false,     // получили ли хоть один ответ от сервера
    everAnswered: false,   // сервер хоть раз РЕАЛЬНО ответил (успешный ответ)
    polls: 0,
    gh: { repo: "", token: "" },   // GitHub-режим: всё работает в Actions репозитория
    ghClips: [], ghMeta: null, ghOk: false, ghEver: false,
    timers: { clips: null, status: null, gh: null }
  };

  function $(id) { return document.getElementById(id); }

  var el = {
    clips: $("clips"), empty: $("empty"),
    emptyTitle: $("empty-title"), emptyText: $("empty-text"),
    connectCard: $("connect-card"), inpEmpty: $("inp-server-empty"),
    btnEmpty: $("btn-connect-empty"), connectStatus: $("connect-status"),
    mixNoteEmpty: $("mix-note-empty"),
    skeleton: $("skeleton"),
    liveStrip: $("live-strip"),
    freshLine: $("fresh-line"),
    connPill: $("conn-pill"), connText: $("conn-text"),
    dlCorner: $("dl-corner"), dlBadge: $("dl-badge"),
    sheet: $("sheet"), sheetBackdrop: $("sheet-backdrop"), sheetList: $("sheet-list"),
    sheetSub: $("sheet-sub"), sheetClear: $("sheet-clear"),
    player: $("player"), playerBackdrop: $("player-backdrop"),
    playerVideo: $("player-video"), playerTitle: $("player-title"),
    playerNote: $("player-note"), playerDl: $("player-dl"),
    playerClose: $("player-close"),
    settings: $("settings"), modalBackdrop: $("modal-backdrop"),
    inpServer: $("inp-server"), mixWarn: $("mix-warning"),
    btnCheck: $("btn-check"), btnSave: $("btn-save"), btnSettings: $("btn-settings"),
    checkResult: $("check-result"),
    inpTkToken: $("inp-tk-token"), btnTkSave: $("btn-tk-save"),
    inpGhRepo: $("inp-gh-repo"), inpGhToken: $("inp-gh-token"),
    btnGhCheck: $("btn-gh-check"), btnGhSave: $("btn-gh-save"),
    ghStatus: $("gh-status"),
    btnPushOn: $("btn-push-on"), btnPushOff: $("btn-push-off"),
    pushStatus: $("push-status"),
    btnChannels: $("btn-channels"), channels: $("channels"),
    channelsBackdrop: $("channels-backdrop"), channelsClose: $("channels-close"),
    inpChannel: $("inp-channel"), btnAddChannel: $("btn-add-channel"),
    addChStatus: $("add-channel-status"), channelList: $("channel-list"),
    btnSync: $("btn-sync"), syncCode: $("sync-code"),
    qLang: $("q-lang"), qSel: $("q-sel"), qSkip: $("q-skip"),
    tkStatus: $("tk-status"),
    toast: $("toast")
  };

  // --- утилиты ------------------------------------------------------------------

  function toast(text, kind) {
    el.toast.textContent = text;
    el.toast.className = "toast" + (kind ? " " + kind : "");
    el.toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.toast.hidden = true; }, 2600);
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n >= 1024 * 1024 * 1024) return (n / 1024 / 1024 / 1024).toFixed(1) + " ГБ";
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " МБ";
    return Math.max(1, Math.round(n / 1024)) + " КБ";
  }

  function fmtDate(unix) {
    try {
      return new Date(unix * 1000).toLocaleString("ru-RU", {
        day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"
      });
    } catch (e) { return ""; }
  }

  function fmtDur(sec) {
    sec = Math.round(Number(sec) || 0);
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function lsGet(key, fallback) {
    try {
      var v = localStorage.getItem(key);
      return v ? JSON.parse(v) : fallback;
    } catch (e) { return fallback; }
  }

  function lsSet(key, val) {
    try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) {}
  }

  function httpsPageHttpServer(url) {
    return location.protocol === "https:" && url &&
           url.indexOf("http://") === 0;
  }

  var ICON_DL = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
  var ICON_PLAY = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" stroke="none"><path d="M8 5.5v13l11-6.5z"/></svg>';
  var ICON_FILM = '<svg viewBox="0 0 24 24" width="54" height="54" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="4.5" width="19" height="15" rx="3.5"/><path d="M10 9.2l5 2.8-5 2.8z" fill="currentColor" stroke="none"/><path d="M2.5 9h2M2.5 15h2M19.5 9h2M19.5 15h2"/></svg>';
  var ICON_FLAME = '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" stroke="none"><path d="M12 2c.6 3.2-.7 5-2.3 6.6C7.9 10.4 6 12.1 6 15a6 6 0 0 0 12 0c0-2.2-1-3.9-2.2-5.2-.4 1-1 1.7-1.8 2.2.3-3.4-.9-7.4-2-10z"/></svg>';

  // --- IndexedDB: офлайн-копии клипов ----------------------------------------------
  // Видео, сохранённые в приложении: плеер и повторное скачивание работают
  // даже без сервера и без интернета. Если IndexedDB недоступна — приложение
  // просто продолжает работать без офлайн-копий.

  var db = null;

  function idbOpen() {
    if (db) return Promise.resolve(db);
    return new Promise(function (resolve, reject) {
      if (!window.indexedDB) { reject(new Error("no idb")); return; }
      var rq = indexedDB.open("sc-offline", 1);
      rq.onupgradeneeded = function () {
        var d = rq.result;
        if (!d.objectStoreNames.contains("clips")) {
          d.createObjectStore("clips", { keyPath: "id" });
        }
      };
      rq.onsuccess = function () { db = rq.result; resolve(db); };
      rq.onerror = function () { reject(rq.error || new Error("idb error")); };
      rq.onblocked = function () { reject(new Error("idb blocked")); };
    });
  }

  function idbReq(mode, runFn) {
    return idbOpen().then(function (d) {
      return new Promise(function (resolve, reject) {
        var tx = d.transaction("clips", mode);
        var rq = runFn(tx.objectStore("clips"));
        rq.onsuccess = function () { resolve(rq.result); };
        rq.onerror = function () { reject(rq.error || new Error("idb req")); };
      });
    }).catch(function () { return null; });   // без IndexedDB приложение живёт
  }

  function idbPut(rec)   { return idbReq("readwrite", function (s) { return s.put(rec); }); }
  function idbGet(id)    { return idbReq("readonly",  function (s) { return s.get(id); }); }
  function idbDelete(id) { return idbReq("readwrite", function (s) { return s.delete(id); }); }
  function idbClear()    { return idbReq("readwrite", function (s) { return s.clear(); }); }
  function idbAll()      { return idbReq("readonly",  function (s) { return s.getAll(); }); }

  // --- «Загруженное» (localStorage по каждому серверу / github) -------------------

  function dlKey() {
    // GitHub-режим тоже должен помнить скачанное НАВСЕГДА: ключ "github"
    return state.server || "github";
  }

  function loadDownloaded() {
    var all = lsGet(LS_DOWNLOADED, {});
    state.downloaded = all[dlKey()] || {};
  }

  function saveDownloaded() {
    var all = lsGet(LS_DOWNLOADED, {});
    all[dlKey()] = state.downloaded;
    lsSet(LS_DOWNLOADED, all);
  }

  function isDownloaded(id) { return !!state.downloaded[id]; }

  function markDownloaded(clip) {
    state.downloaded[clip.id] = {
      file: clip.file, size: clip.size, ts: Date.now(),
      channel: clip.display || clip.channel || "", platform: clip.platform || ""
    };
    saveDownloaded();
    updateBadge();
  }

  function unmarkDownloaded(id) {
    delete state.downloaded[id];
    saveDownloaded();
    idbDelete(id);
    updateBadge();
  }

  function downloadedIds() { return Object.keys(state.downloaded); }

  // --- каналы, добавленные с телефона (до синхронизации с GitHub) -----------------

  function loadPending() {
    return lsGet(LS_PENDING, []);
  }

  function savePending(list) {
    lsSet(LS_PENDING, list);
  }

  function addPending(url) {
    var list = loadPending();
    var s = String(url || "").trim();
    if (!s) return false;
    var low = s.toLowerCase();
    for (var i = 0; i < list.length; i++) {
      if (String(list[i]).trim().toLowerCase() === low) return false;
    }
    list.push(s);
    savePending(list);
    return true;
  }

  function removePending(url) {
    var list = loadPending().filter(function (c) {
      return String(c).trim().toLowerCase() !== String(url).toLowerCase();
    });
    savePending(list);
    return list;
  }

  // --- качество клипов: настройки + фидбек (до синхронизации с GitHub) ------------

  var DEFAULT_QUALITY = { speech_language: "auto", selection: "normal",
                          skip_no_speech: true };

  function loadQuality() {
    var q = lsGet(LS_QUALITY, {});
    var out = {};
    for (var k in DEFAULT_QUALITY) {
      out[k] = (q && q[k] !== undefined && q[k] !== null) ? q[k] : DEFAULT_QUALITY[k];
    }
    return out;
  }

  function saveQuality(q) { lsSet(LS_QUALITY, q); }

  function loadFb() {
    return lsGet(LS_FEEDBACK, {});     // { clipId: {v, file, src, start, channel, ts} }
  }

  function saveFb(o) { lsSet(LS_FEEDBACK, o); }

  function fbItems(o) {
    return Object.keys(o || {}).map(function (id) {
      return Object.assign({ id: id }, o[id]);
    });
  }

  function loadRequests() {
    return lsGet(LS_REQUESTS, []);     // [{url, start, end, display, ts}]
  }

  function saveRequests(list) { lsSet(LS_REQUESTS, list); }

  // --- скрытые крестиком: пропадают из ленты, можно вернуть одним нажатием -------

  function loadHidden() { return lsGet(LS_HIDDEN, []); }

  function saveHidden(list) { lsSet(LS_HIDDEN, list.slice(-500)); }

  function isHidden(id) { return loadHidden().indexOf(String(id)) !== -1; }

  function hideClip(id) {
    var list = loadHidden();
    if (list.indexOf(String(id)) === -1) { list.push(String(id)); saveHidden(list); }
  }

  function hiddenCount() { return loadHidden().length; }

  // --- хэштеги: гарантированный показ на КАЖДОЙ карточке --------------------------
  // Клипы из старого clips.json могут не иметь поля hashtags — тогда теги
  // собираются прямо в браузере (игра по названию/нику + платформа + виральные).
  var GAME_TAGS_CLIENT = [
    ["gta|гта|grand theft", ["gta", "гта5", "gtarp"]],
    ["cs2|counter|кс2|ксго|контр", ["cs2", "кс2", "cs"]],
    ["dota|дота", ["dota2", "дота2"]],
    ["minecraft|майнкрафт|майн", ["minecraft", "майнкрафт"]],
    ["valorant|валорант", ["valorant", "валорант"]],
    ["fortnite|фортнайт", ["fortnite", "фортнайт"]],
    ["pubg|пабг", ["pubg", "пабг"]],
    ["apex|апекс", ["apexlegends", "апекс"]],
    ["warzone|call of duty|варзон", ["warzone", "callofduty"]],
    ["rust| раст", ["rustgame", "раст"]],
    ["league of legends|лига легенд", ["leagueoflegends", "лол"]],
    ["world of warcraft| wow |вов", ["wow", "вов"]],
    ["genshin|геншин", ["genshin", "геншин"]],
    ["roblox|роблокс", ["roblox", "роблокс"]],
    ["tarkov|тарков", ["tarkov", "тарков"]],
    ["stalker|сталкер", ["stalker2", "сталкер"]]
  ];

  function ensureHashtags(clip) {
    if (clip.hashtags && clip.hashtags.length) return clip.hashtags;
    var text = ((clip.title || "") + " " + (clip.display || "") + " "
                + (clip.channel || "")).toLowerCase();
    var tags = [];
    GAME_TAGS_CLIENT.forEach(function (p) {
      try { if (new RegExp(p[0]).test(text)) tags = tags.concat(p[1]); } catch (e) {}
    });
    var ch = String(clip.channel || clip.display || "").toLowerCase()
      .replace(/[^a-z0-9]/g, "");
    if (ch) tags.push(ch);
    tags.push(clip.platform === "youtube" ? "youtube" : "twitch");
    tags = tags.concat(["рек", "рекомендации", "fyp", "foryou", "стрим", "клипы"]);
    var seen = {}, out = [];
    tags.forEach(function (t) {
      t = "#" + String(t).replace(/^#/, "").toLowerCase();
      if (!seen[t]) { seen[t] = 1; out.push(t); }
    });
    out = out.slice(0, 14);
    clip.hashtags = out;
    return out;
  }

  // Совет по времени постинга: часы максимальной активности RU TikTok
  var POST_TIP = "Совет для просмотров: выкладывай в 11:00–13:00 или 19:00–22:00 по МСК — в эти часы лента TikTok активнее всего";

  function qualityIsDefault() {
    var q = loadQuality(), d = DEFAULT_QUALITY, same = true;
    for (var k in d) { if (String(q[k]) !== String(d[k])) same = false; }
    return same;
  }

  function updateBadge() {
    var n = downloadedIds().length;
    el.dlBadge.textContent = n;
    el.dlBadge.className = "badge" + (n ? "" : " zero");
    el.dlCorner.hidden = false;   // кнопка в углу всегда доступна
  }

  // --- связь с сервером -------------------------------------------------------------

  function api(path) {
    return fetch(state.server + path, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }

  function apiPost(path, body) {
    return fetch(state.server + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      if (!r.ok && r.status !== 400 && r.status !== 404) {
        throw new Error("HTTP " + r.status);
      }
      return r.json();
    });
  }

  function setOnline(on) {
    state.online = on;
    if (on && !navigator.onLine) on = false;   // сеть устройства недоступна — офлайн
    el.connPill.className = "conn-pill " + (on ? "on" : "off");
    el.connText.textContent = on ? "на связи"
      : (navigator.onLine ? (state.server ? "нет связи"
        : (state.gh.repo ? "GitHub" : "не подключено")) : "офлайн");
  }

  function setGhPill(on) {
    state.ghOk = on;
    el.connPill.className = "conn-pill " + (on ? "on" : "off");
    el.connText.textContent = on ? "GitHub" : "GitHub…";
  }

  function renderEmpty() {
    var list = visibleClips();
    var show = !list.length;
    el.empty.hidden = !show;
    if (!show) return;

    if (!state.server) {
      if (state.ghEver) {
        el.emptyTitle.textContent = "GitHub следит за каналами";
        el.emptyText.textContent = "Репозиторий проверяет каналы каждые ~20 секунд и включает запись сам. Как только чат стримера «взорвётся», готовый вертикальный клип появится здесь, а на телефон придёт уведомление.";
        el.connectCard.hidden = true;
      } else {
        el.emptyTitle.textContent = "Подключите компьютер — или всё сделает GitHub";
        el.emptyText.textContent = "Два варианта: программа на ПК (адрес из её окна) или полностью автоматический режим — репозиторий на GitHub сам следит, нарезает и публикует (README → «Всё на GitHub»).";
        el.connectCard.hidden = false;
      }
    } else if (!state.online) {
      el.emptyTitle.textContent = "Нет связи с сервером";
      el.emptyText.textContent = "Программа не запущена или адрес неверный. Проверьте компьютер и адрес — подсказки ниже.";
      el.connectCard.hidden = false;
      if (!el.inpEmpty.value) el.inpEmpty.value = state.server;
    } else {
      el.emptyTitle.textContent = "Следим за каналами";
      el.emptyText.textContent = "Клип появится здесь сразу, как только будет готов — обычно через считанные секунды после взрыва чата.";
      el.connectCard.hidden = true;
    }
  }

  function refreshClips() {
    if (!state.server) return;
    state.polls++;
    api("/api/clips").then(function (data) {
      state.everLoaded = true;
      state.everAnswered = true;
      setOnline(true);
      el.skeleton.hidden = true;
      state.clips = (data && data.clips) || [];
      state.freshUpdated = (data && data.updated) || "";
      renderClips();
      renderFreshness();
    }).catch(function () {
      state.everLoaded = true;
      el.skeleton.hidden = true;
      if (state.online || state.polls % 4 === 1) {
        setOnline(false);
        renderClips();
      }
    });
  }

  function refreshStatus() {
    if (!state.server) return;
    api("/api/status").then(function (st) {
      renderStatus(st || {});
    }).catch(function () { /* молча: лента просто не обновится */ });
  }

  // --- индикатор «живости» нарезки --------------------------------------------------
  // Пользователь должен сразу видеть: клипы делаются прямо сейчас или пауза.
  // Раньше лента выглядела одинаково и в работе, и в тишине — и это читалось
  // как «нарезки не делаются». Возраст считаем от последнего клипа и от
  // метки updated в clips.json (обновляется при каждом новом клипе).
  function renderFreshness() {
    if (!el.freshLine) return;
    var now = Date.now() / 1000;
    var lastTs = 0;
    for (var i = 0; i < state.clips.length; i++) {
      var c = Number(state.clips[i] && state.clips[i].created) || 0;
      if (c > lastTs) lastTs = c;
    }
    if (state.freshUpdated) {
      var up = Date.parse(state.freshUpdated) / 1000 || 0;
      if (up > lastTs) lastTs = up;
    }
    if (!lastTs) { el.freshLine.hidden = true; return; }
    var ageMin = Math.max(0, Math.round((now - lastTs) / 60));
    var dot = "on", txt;
    if (ageMin < 8) {
      txt = "Нарезаю сейчас — последний клип " +
            (ageMin < 1 ? "только что" : ageMin + " мин назад");
    } else if (ageMin < 45) {
      dot = "idle";
      txt = "Слежу за эфиром — последний клип " + ageMin + " мин назад";
    } else if (ageMin < 240) {
      dot = "idle";
      txt = "Тихий момент — последний клип " + ageMin + " мин назад";
    } else {
      dot = "off";
      txt = "Ждём эфир — последний клип " + Math.round(ageMin / 60) + " ч назад";
    }
    el.freshLine.hidden = false;
    el.freshLine.innerHTML = '<span class="dot ' + dot + '"></span>' + txt;
  }

  // --- отрисовка клипов ---------------------------------------------------------------

  function visibleClips() {
    return state.clips.filter(function (c) {
      return !isDownloaded(c.id) && !isHidden(c.id);
    });
  }

  function renderClips() {
    var list = visibleClips();

    // Перерисовываем только если список изменился: кнопки не «улетают»
    // из-под пальца во время поллинга, картинки не мигают.
    var sig = list.map(function (c) { return c.id; }).join(",");
    if (sig === renderClips._lastSig && el.clips.childNodes.length) {
      renderEmpty();
      return;
    }
    renderClips._lastSig = sig;

    el.clips.innerHTML = "";
    list.forEach(function (clip, i) { el.clips.appendChild(cardEl(clip, i)); });
    // полоска «показать скрытые»: крестик не приговор — вернуть легко
    var hid = hiddenCount();
    if (hid > 0) {
      var bar = document.createElement("button");
      bar.type = "button";
      bar.className = "restore-bar";
      bar.textContent = "Показать скрытые клипы (" + hid + ")";
      bar.addEventListener("click", function () {
        saveHidden([]);
        renderClips._lastSig = null;
        renderClips();
        toast("Скрытые клипы вернулись в ленту", "ok");
      });
      el.clips.appendChild(bar);
    }
    renderEmpty();
  }

  function cardEl(clip, idx) {
    var card = document.createElement("article");
    card.className = "card";
    card.dataset.id = clip.id;
    card.style.animationDelay = Math.min((idx || 0) * 55, 330) + "ms";

    var fresh = clip.created && (Date.now() / 1000 - clip.created) < 120 &&
                !state.knownIds[clip.id];
    state.knownIds[clip.id] = true;
    if (fresh) card.classList.add("fresh");

    var thumb = document.createElement("div");
    thumb.className = "thumb-wrap has-player";
    thumb.title = "Нажмите, чтобы посмотреть видео до скачивания";
    thumb.addEventListener("click", function () { openPlayerForClip(clip); });
    if (clip.thumb) {
      var img = document.createElement("img");
      img.src = (/^(https?:|data:)/i.test(clip.thumb) || !state.server)
        ? clip.thumb : state.server + clip.thumb;
      img.alt = "";
      img.loading = "lazy";
      img.onerror = function () { thumb.innerHTML = '<div class="thumb-ph">' + ICON_FILM + "</div>"; };
      thumb.appendChild(img);
    } else {
      thumb.innerHTML = '<div class="thumb-ph">' + ICON_FILM + "</div>";
    }

    var chips = document.createElement("div");
    chips.className = "thumb-chips";

    var left = document.createElement("div");
    left.style.cssText = "display:flex;gap:6px;flex-wrap:wrap;";
    var plat = document.createElement("span");
    plat.className = "tag " + (clip.platform === "twitch" ? "tw" : "yt");
    plat.innerHTML = '<i class="plat-dot"></i>' + (clip.platform === "twitch" ? "TWITCH" : "YOUTUBE");
    left.appendChild(plat);
    if (clip.score) {
      var hyp = document.createElement("span");
      hyp.className = "tag hype";
      hyp.innerHTML = ICON_FLAME + " хайп " + Math.round(clip.score);
      left.appendChild(hyp);
    }
    // Бейдж субтитров: видно ДО скачивания, что речь распознана
    var sb = clip.subs || null;
    if (sb && sb.ok) {
      var subOk = document.createElement("span");
      subOk.className = "tag sub-ok";
      subOk.textContent = "СУБ " + String(sb.lang || "").toUpperCase()
        + (sb.speech_pct ? " " + sb.speech_pct + "%" : "");
      subOk.title = "Субтитры распознаны из речи";
      left.appendChild(subOk);
    } else if (sb && !sb.ok) {
      var subNo = document.createElement("span");
      subNo.className = "tag sub-no";
      subNo.textContent = "без речи";
      subNo.title = "В этом моменте не было различимой речи";
      left.appendChild(subNo);
    }
    chips.appendChild(left);

    var right = document.createElement("div");
    right.style.cssText = "display:flex;gap:6px;";
    if (fresh) {
      var ft = document.createElement("span");
      ft.className = "tag fresh-tag";
      ft.textContent = "НОВЫЙ";
      right.appendChild(ft);
    }
    var dur = document.createElement("span");
    dur.className = "tag";
    dur.textContent = fmtDur(clip.duration || 60);
    right.appendChild(dur);
    chips.appendChild(right);

    thumb.appendChild(chips);

    // подсказка «смотреть» прямо на превью: видео открывается ДО скачивания
    var watch = document.createElement("span");
    watch.className = "watch-chip";
    watch.innerHTML = ICON_PLAY + " Смотреть";
    thumb.appendChild(watch);
    card.appendChild(thumb);

    var body = document.createElement("div");
    body.className = "card-body";

    var title = document.createElement("div");
    title.className = "card-title";
    var nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = clip.display || clip.channel || "клип";
    title.appendChild(nm);
    // крестик: скучный клип убирается из ленты одним нажатием
    var xBtn = document.createElement("button");
    xBtn.type = "button";
    xBtn.className = "x-hide";
    xBtn.setAttribute("aria-label", "Скрыть клип");
    xBtn.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>';
    xBtn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      hideClip(clip.id);
      renderClips._lastSig = null;
      renderClips();
      toast("Клип скрыт — вернётся кнопкой «Показать скрытые»", "ok");
    });
    title.appendChild(xBtn);
    body.appendChild(title);

    var sub = document.createElement("div");
    sub.className = "card-sub";
    [fmtDate(clip.created), fmtSize(clip.size)].forEach(function (b, i) {
      if (i) {
        var sep = document.createElement("span");
        sep.className = "sep";
        sep.textContent = "•";
        sub.appendChild(sep);
      }
      var s = document.createElement("span");
      s.textContent = b;
      sub.appendChild(s);
    });
    body.appendChild(sub);

    if (clip.title) {
      var tl = document.createElement("div");
      tl.className = "clip-title-line";
      tl.textContent = clip.title;
      body.appendChild(tl);
    }

    if (clip.caption) {
      var capLine = document.createElement("div");
      capLine.className = "caption-line";
      var capTxt = String(clip.caption);
      capLine.textContent = capTxt.length > 120 ? capTxt.slice(0, 117) + "…" : capTxt;
      capLine.title = "Готовый текст описания для TikTok — скопируется кнопкой ниже";
      body.appendChild(capLine);
    }

    // Хэштеги ДО скачивания: сразу видно, что вставить в описание TikTok.
    // ensureHashtags: даже старые клипы без поля hashtags получают теги.
    var cardTags = ensureHashtags(clip);
    if (cardTags.length) {
      var tagsLine = document.createElement("div");
      tagsLine.className = "tags-line";
      var chips = document.createElement("div");
      chips.className = "tags-chips";
      cardTags.slice(0, 12).forEach(function (t) {
        var chip = document.createElement("span");
        chip.className = "tag-chip";
        chip.textContent = t;
        chips.appendChild(chip);
      });
      var copyBtn = document.createElement("button");
      copyBtn.className = "tags-copy";
      copyBtn.type = "button";
      copyBtn.textContent = "Копировать";
      copyBtn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var text = (clip.caption ? clip.caption + "\n\n" : "") + cardTags.join(" ");
        function done() {
          copyBtn.textContent = "Скопировано";
          toast("Текст и хэштеги скопированы — вставь в описание TikTok", "ok");
          setTimeout(function () { copyBtn.textContent = "Копировать"; }, 2200);
        }
        function fallback() {
          var ta = document.createElement("textarea");
          ta.value = text;
          ta.style.cssText = "position:fixed;opacity:0;";
          document.body.appendChild(ta);
          ta.select();
          try { document.execCommand("copy"); done(); }
          catch (e) { toast("Не удалось скопировать", "err"); }
          ta.remove();
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, fallback);
        } else {
          fallback();
        }
      });
      tagsLine.appendChild(chips);
      tagsLine.appendChild(copyBtn);
      body.appendChild(tagsLine);

      var tip = document.createElement("div");
      tip.className = "tip-line";
      tip.textContent = POST_TIP;
      body.appendChild(tip);
    }

    var dlRow = document.createElement("div");
    dlRow.className = "dl-row";
    var btn = document.createElement("button");
    btn.className = "dl-btn";
    btn.innerHTML = '<span class="prog"></span>' + ICON_DL + "<span>Скачать</span>";
    btn.addEventListener("click", function () { downloadClip(clip, btn); });
    dlRow.appendChild(btn);
    var ttBtn = document.createElement("button");
    ttBtn.type = "button";
    ttBtn.className = "tt-btn";
    ttBtn.title = "Открыть TikTok — загрузка видео";
    ttBtn.textContent = "TikTok";
    ttBtn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var ttTags = ensureHashtags(clip);
      try { navigator.clipboard && ttTags.length && navigator.clipboard.writeText(ttTags.join(" ")); } catch (e) {}
      window.open("https://www.tiktok.com/upload", "_blank", "noopener");
      toast("Открыл TikTok. Хэштеги уже скопированы — вставьте в описание", "ok");
    });
    dlRow.appendChild(ttBtn);
    body.appendChild(dlRow);

    // --- обратная связь: помогаем боту отбирать интересные моменты ---------
    var fb = loadFb()[clip.id] || null;
    var fbRow = document.createElement("div");
    fbRow.className = "fb-row";
    var bLike = document.createElement("button");
    bLike.type = "button";
    bLike.className = "fb-btn like" + (fb && fb.v > 0 ? " on" : "");
    bLike.textContent = "Интересно";
    var bDis = document.createElement("button");
    bDis.type = "button";
    bDis.className = "fb-btn dislike" + (fb && fb.v < 0 ? " on" : "");
    bDis.textContent = "Скучно";
    function vote(v, btnSelf, other) {
      var all = loadFb();
      if (all[clip.id] && all[clip.id].v === v) return;   // уже так оценено
      all[clip.id] = { v: v, file: clip.file || "",
                       src: clip.src || "", start: clip.start || 0,
                       channel: clip.channel || clip.display || "", ts: Date.now() };
      saveFb(all);
      btnSelf.classList.add("on");
      other.classList.remove("on");
      toast(v > 0 ? "Отметил — таких моментов будет больше"
                  : "Понял — бот станет строже к таким моментам", "ok");
    }
    bLike.addEventListener("click", function (ev) {
      ev.stopPropagation();
      vote(1, bLike, bDis);
    });
    bDis.addEventListener("click", function (ev) {
      ev.stopPropagation();
      vote(-1, bDis, bLike);
    });
    fbRow.appendChild(bLike);
    fbRow.appendChild(bDis);
    // «Ещё отсюда» — запрос нарезки следующей минуты того же видео
    if (clip.src && clip.start) {
      var bMore = document.createElement("button");
      bMore.type = "button";
      bMore.className = "fb-btn more";
      bMore.textContent = "Ещё отсюда";
      bMore.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var reqs = loadRequests();
        var s = Number(clip.start) || 0;
        reqs.push({ url: clip.src, start: s + 60, end: s + 120,
                    display: clip.display || clip.channel || "", ts: Date.now() });
        saveRequests(reqs);
        bMore.classList.add("on");
        bMore.textContent = "Заказал";
        toast("Заказал следующую минуту — синхронизируйте в «Каналах»", "ok");
      });
      fbRow.appendChild(bMore);
    }
    body.appendChild(fbRow);

    card.appendChild(body);
    return card;
  }

  // --- скачивание: файл на устройство + офлайн-копия в приложении ------------------

  function saveBlob(blob, filename) {
    var obj = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = obj;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(obj); }, 60000);
  }

  function fetchBlob(url, onProgress) {
    return fetch(url, { cache: "no-store" }).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      if (!res.body || !res.body.getReader) return res.blob();
      var total = Number(res.headers.get("Content-Length") || 0);
      var reader = res.body.getReader();
      var chunks = [], got = 0;
      function step(r) {
        if (r.done) return null;
        chunks.push(r.value);
        got += r.value.length;
        if (onProgress) onProgress(got, total);
        return reader.read().then(step);
      }
      return reader.read().then(step).then(function () {
        return new Blob(chunks, { type: "video/mp4" });
      });
    });
  }

  function downloadClip(clip, btn) {
    // GitHub-режим: файл лежит в Releases — открываем прямую ссылку
    // (GitHub отдаёт файл с «attachment», телефон сохраняет его в загрузки).
    if (clip.gh && clip.url) {
      var a = document.createElement("a");
      a.href = clip.url;
      a.download = clip.file || "";
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      setTimeout(function () { a.remove(); }, 1500);
      finishDownload(clip);
      return;
    }
    if (!state.server) { openSettings(); return; }
    if (httpsPageHttpServer(state.server)) {
      el.mixWarn.hidden = false;
      toast("GitHub Pages (https) не может скачать с http-сервера. Откройте адрес сервера напрямую или настройте https-туннель (README)", "err");
      return;
    }
    var label = btn && btn.querySelector ? btn.querySelector("span:last-child") : null;
    var prog = btn && btn.querySelector ? btn.querySelector(".prog") : null;
    if (btn && btn.disabled !== undefined) btn.disabled = true;
    if (label) label.textContent = "Скачиваю…";

    idbGet(clip.id).then(function (stored) {
      if (stored && stored.blob) {
        // офлайн-копия уже есть: сохраняем файл прямо из неё — сеть не нужна
        saveBlob(stored.blob, clip.file);
        finishDownload(clip);
        return null;
      }
      return fetchBlob(state.server + clip.url, function (got, total) {
        if (total && prog) prog.style.width = Math.min(100, got / total * 100).toFixed(1) + "%";
      }).then(function (blob) {
        saveBlob(blob, clip.file);
        finishDownload(clip, blob);
      });
    }).catch(function (e) {
      if (btn && btn.disabled !== undefined) btn.disabled = false;
      if (label) label.textContent = "Скачать";
      toast("Не удалось скачать: " + (e && e.message ? e.message : "ошибка сети"), "err");
    });
  }

  function finishDownload(clip, blob) {
    markDownloaded(clip);
    if (blob && blob.size <= OFFLINE_CAP) {
      idbPut({
        id: clip.id, file: clip.file, size: blob.size, ts: Date.now(),
        channel: clip.display || clip.channel || "", platform: clip.platform || "",
        blob: blob
      }).then(function () { updateBadge(); });
      toast("Готово: файл скачан + офлайн-копия в приложении", "ok");
    } else {
      toast("Сохранено в «Загруженное»", "ok");
    }
    animateOut(clip.id);
  }

  function animateOut(id) {
    var card = el.clips.querySelector("[data-id='" + id + "']");
    function gone() { renderClips(); }
    if (card && card.classList) {
      card.classList.add("leaving");
      setTimeout(gone, 330);
    } else {
      gone();
    }
  }

  // --- плеер: офлайн-копия или поток с сервера ---------------------------------------

  var player = { url: null, file: null, urlSrc: null, previewSrc: null };

  function closePlayer() {
    el.playerVideo.pause();
    el.player.hidden = true;
    el.playerBackdrop.hidden = true;
    if (player.url) { URL.revokeObjectURL(player.url); player.url = null; }
    el.playerVideo.removeAttribute("src");
    try { el.playerVideo.load(); } catch (e) {}
    player.file = null;
    player.urlSrc = null;
    player.previewSrc = null;
  }

  function openPlayer(item, rec) {
    el.playerTitle.textContent = item.display || item.channel || "Клип";
    el.player.hidden = false;
    el.playerBackdrop.hidden = false;
    player.file = item.file;

    if (rec && rec.blob) {
      if (player.url) URL.revokeObjectURL(player.url);
      player.url = URL.createObjectURL(rec.blob);
      el.playerVideo.src = player.url;
      el.playerNote.textContent = "Офлайн-копия на этом устройстве — работает без сети · " + fmtSize(rec.blob.size);
      el.playerDl.hidden = false;
      return;
    }
    if (item.gh && item.url) {
      player.urlSrc = item.url;          // полный файл в Releases — для «Скачать»
      if (item.preview) {
        // 480p-копия в репо: Pages отдаёт её same-origin с video/mp4 —
        // играет сразу, в том числе на iPhone (файлы Releases Safari
        // не играет: Content-Disposition: attachment + нет CORS)
        player.previewSrc = item.preview;
        el.playerVideo.src = item.preview;
        el.playerNote.textContent = "Играет сразу (лёгкая копия 480p) — полное качество по кнопке «Скачать»";
      } else {
        player.previewSrc = null;
        el.playerVideo.src = item.url;
        el.playerNote.textContent = "Видео из GitHub (Releases) — если не заиграет, через мгновение кэширую сюда";
      }
      el.playerDl.hidden = false;
      return;
    }
    player.urlSrc = null;
    if (state.server && !httpsPageHttpServer(state.server)) {
      el.playerVideo.src = state.server + "/files/" + encodeURIComponent(item.file);
      el.playerNote.textContent = "Поток с сервера — нужна сеть";
      el.playerDl.hidden = false;
      return;
    }
    el.playerVideo.removeAttribute("src");
    el.playerNote.textContent = "Нет ни офлайн-копии, ни связи с сервером";
    el.playerDl.hidden = true;
  }

  // Смотреть ДО скачивания: тап по превью открывает плеер с видео из Releases
  function openPlayerForClip(clip) {
    if (clip.gh && clip.url) { openPlayer(clip, null); return; }
    if (state.server && !httpsPageHttpServer(state.server)) {
      openPlayer(clip, null);
      return;
    }
    toast("Видео станет доступно, когда появится связь с GitHub/сервером", "err");
  }

  // --- фолбэк плеера: iOS Safari не играет файлы GitHub Releases напрямую
  // (Content-Disposition: attachment) — тогда качаем через fetch в blob
  // и играем локально; прогресс виден в подписи плеера.
  function ghBlobFallback() {
    var url = player.urlSrc;
    if (!url || player.url || player.blobBusy) return;
    player.blobBusy = true;
    el.playerNote.textContent = "Кэширую видео: 0%";
    fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      var total = Number(r.headers.get("content-length") || 0);
      if (!r.body || !total) return r.blob();
      var reader = r.body.getReader(), chunks = [], got = 0;
      return reader.read().then(function pump(res) {
        if (res.done) return new Blob(chunks);
        chunks.push(res.value);
        got += res.value.byteLength;
        el.playerNote.textContent = "Кэширую видео: "
          + Math.min(99, Math.round(100 * got / total)) + "%";
        return reader.read().then(pump);
      });
    }).then(function (blob) {
      player.blobBusy = false;
      if (!player.urlSrc || el.player.hidden) return;   // плеер уже закрыли
      if (player.url) URL.revokeObjectURL(player.url);
      player.url = URL.createObjectURL(blob);
      el.playerVideo.src = player.url;
      el.playerNote.textContent = "Видео в кэше браузера — играет даже без сети";
      try { el.playerVideo.play().catch(function () {}); } catch (e) {}
    }).catch(function () {
      player.blobBusy = false;
      if (!el.player.hidden) {
        el.playerNote.textContent = "Видео не открылось — нажми «Скачать», файл сохранится в «Загруженное»";
      }
    });
  }

  // Ошибка воспроизведения: если играли превью — пробуем Releases;
  // если Releases — blob-фолбэк (кроме офлайн-копии)
  el.playerVideo.addEventListener("error", function () {
    if (player.previewSrc) {
      player.previewSrc = null;
      el.playerNote.textContent = "Превью не открылось — пробую полное видео…";
      el.playerVideo.src = player.urlSrc;
      return;
    }
    if (player.urlSrc && !player.url) ghBlobFallback();
  });

  function playerDownload() {
    if (!player.file) return;
    if (player.urlSrc) {
      downloadClip({ id: String(player.file).replace(/\.[a-z0-9]+$/i, ""),
                     file: player.file, url: player.urlSrc, gh: true }, null);
      return;
    }
    // ищем офлайн-копию по имени файла
    idbAll().then(function (records) {
      var rec = null;
      (records || []).forEach(function (r) {
        if (r.file === player.file) rec = r;
      });
      if (rec && rec.blob) {
        saveBlob(rec.blob, rec.file);
        toast("Файл сохранён из офлайн-копии", "ok");
        return;
      }
      if (state.server) {
        var clip = findClip(player.file) || { id: player.file, file: player.file, url: "/files/" + encodeURIComponent(player.file), size: 0 };
        downloadClip(clip, null);
        return;
      }
      toast("Нет офлайн-копии и сервера", "err");
    });
  }

  // --- лента статусов каналов -------------------------------------------------------

  function renderStatus(st) {
    var chans = (st && st.channels) || [];
    state.lastStatus = st || {};
    if (!chans.length) { el.liveStrip.hidden = true; return; }
    el.liveStrip.hidden = false;
    el.liveStrip.innerHTML = "";
    chans.forEach(function (c) {
      var chip = document.createElement("span");
      chip.className = "chip";
      var plat = document.createElement("span");
      plat.className = "plat " + (c.platform === "twitch" ? "tw" : "yt");
      plat.textContent = c.platform === "twitch" ? "TW" : "YT";
      var nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = c.name;
      chip.appendChild(plat);
      chip.appendChild(nm);
      if (c.live) {
        var live = document.createElement("span");
        live.className = "badge-live";
        live.textContent = "LIVE";
        chip.appendChild(live);
        if (c.ad) {
          var ad = document.createElement("span");
          ad.className = "badge-ad";
          ad.textContent = "РЕКЛАМА";
          chip.appendChild(ad);
        }
        if (c.viewers) {
          var v = document.createElement("span");
          v.className = "viewers";
          v.textContent = c.viewers;
          chip.appendChild(v);
        }
        var hb = document.createElement("span");
        hb.className = "hypebar";
        var fill = document.createElement("i");
        fill.style.width = Math.max(3, Math.min(100, Number(c.hype) || 0)) + "%";
        hb.appendChild(fill);
        chip.appendChild(hb);
      } else {
        var off = document.createElement("span");
        off.className = "off";
        off.textContent = "офлайн";
        chip.appendChild(off);
      }
      el.liveStrip.appendChild(chip);
    });
    // синхронизируем открытую шторку «Каналы» с актуальными статусами
    if (!el.channels.hidden) renderChannelList(chans, state.lastStatus.tiktok);
  }

  // --- шторка «Каналы»: добавил ОДИН раз — следим всегда -------------------------

  function openChannels() {
    closeSettings();
    el.channels.hidden = false;
    el.channelsBackdrop.hidden = false;
    el.addChStatus.textContent = "";
    el.addChStatus.className = "check-result";
    el.tkStatus.textContent = "Проверяю…";
    refreshChannels();
  }

  function closeChannels() {
    el.channels.hidden = true;
    el.channelsBackdrop.hidden = true;
  }

  function refreshChannels() {
    if (!state.server) {
      if (state.gh.repo) { ghRefreshChannels(); return; }   // список виден и без токена
      el.channelList.innerHTML = "";
      setChannelsOffline("Подключите компьютер (Настройки) — или GitHub: Настройки → «GitHub без компьютера»");
      return;
    }
    api("/api/status").then(function (st) {
      state.lastStatus = st || {};
      renderChannelList((st && st.channels) || [], st && st.tiktok);
    }).catch(function (e) {
      setChannelsOffline("Нет связи с сервером — «Каналы» недоступны");
    });
  }

  function setChannelsOffline(text) {
    el.tkStatus.textContent = text;
    var p = document.createElement("p");
    p.className = "sheet-empty";
    p.textContent = text;
    el.channelList.innerHTML = "";
    el.channelList.appendChild(p);
  }

  function renderChannelList(chans, tk) {
    el.channelList.innerHTML = "";
    if (!chans.length) {
      var p = document.createElement("p");
      p.className = "sheet-empty";
      p.textContent = "Пока ни одного канала. Вставьте ссылку выше — и всё, следим сами.";
      el.channelList.appendChild(p);
    }
    chans.forEach(function (c) {
      var row = document.createElement("div");
      row.className = "ch-row";

      var plat = document.createElement("span");
      plat.className = "plat " + (c.platform === "twitch" ? "tw" : "yt");
      plat.textContent = c.platform === "twitch" ? "TW" : "YT";
      row.appendChild(plat);

      var meta = document.createElement("div");
      meta.className = "ch-meta";
      var nm = document.createElement("div");
      nm.className = "nm";
      nm.textContent = c.display || c.name;
      meta.appendChild(nm);
      var st = document.createElement("div");
      st.className = "ch-state" + (c.pending ? "" : (c.live ? " live" : ""));
      st.textContent = c.pending
        ? "добавлен на сайте — попадёт в монитор после синхронизации"
        : (c.live
          ? (c.ad ? "LIVE · РЕКЛАМА — нарезка на паузе"
                  : "LIVE · " + (c.viewers ? c.viewers + " зрителей" : "записываем"))
          : "следим — зайдём на стрим автоматически");
      meta.appendChild(st);
      row.appendChild(meta);

      if (c.live) {
        var adBtn = document.createElement("button");
        adBtn.className = "btn-ad" + (c.ad ? " on" : "");
        adBtn.textContent = c.ad ? "Реклама кончилась" : "РЕКЛАМА";
        adBtn.title = "Реклама RubAp/RubUp: пауза нарезки на время рекламы";
        adBtn.addEventListener("click", function () {
          toggleAd(c.name, adBtn);
        });
        row.appendChild(adBtn);
      }

      var drop = document.createElement("button");
      drop.className = "ch-remove";
      drop.textContent = "✕";
      drop.title = c.pending ? "Убрать из списка синхронизации"
                             : "Убрать из слежки";
      drop.addEventListener("click", function () {
        if (c.pending) {
          removePending(c.name);
          toast("Убрано из списка синхронизации");
          ghRefreshChannels();
          return;
        }
        removeChannel(c.name);
      });
      row.appendChild(drop);

      el.channelList.appendChild(row);
    });
    renderTkStatus(tk);
  }

  function renderTkStatus(tk) {
    if (!tk) {
      el.tkStatus.innerHTML = "<b>TikTok не подключён.</b> Настройки → вставьте " +
        "access_token и нажмите «TikTok». После этого каждый клип будет " +
        "публиковаться сам: 35–60 с по силе момента, субтитры, " +
        "баннер RubUP в середине, хештеги.";
      return;
    }
    el.tkStatus.innerHTML = "<b>TikTok подключён</b> (режим " +
      (tk.mode === "api" ? "API" : "браузер") +
      (tk.enabled ? "" : ", но очередь не работает — проверьте config.yaml") + "): " +
      "опубликовано " + (tk.posted || 0) +
      ", в очереди " + (tk.queued || 0) +
      (tk.failed ? ", ошибок " + tk.failed : "") +
      ". Новый клип уйдёт в TikTok сам через 1–5 минут (антибан-пауза).";
  }

  function addChannel() {
    var url = String(el.inpChannel.value || "").trim();
    el.addChStatus.className = "check-result";
    if (!url) {
      el.addChStatus.textContent = "Вставьте ссылку на канал";
      el.addChStatus.className = "check-result err";
      return;
    }
    if (!state.server) {
      if (state.gh.repo) { ghAddChannel(url); return; }     // проверка токена — внутри
      el.addChStatus.textContent = "Подключите компьютер или GitHub — оба варианта в Настройках";
      el.addChStatus.className = "check-result err";
      return;
    }
    if (httpsPageHttpServer(state.server)) {
      el.addChStatus.textContent = "Нет https-связи с сервером — «Каналы» работают " +
        "при подключении к компьютеру (см. Настройки)";
      el.addChStatus.className = "check-result err";
      return;
    }
    el.btnAddChannel.disabled = true;
    el.addChStatus.textContent = "Добавляю…";
    apiPost("/api/channels", { url: url }).then(function (j) {
      el.addChStatus.textContent = (j && j.message) || "";
      el.addChStatus.className = "check-result " + (j && j.ok ? "ok" : "err");
      if (j && j.ok) {
        el.inpChannel.value = "";
        toast("Канал добавлен — следим постоянно", "ok");
        refreshChannels();
        refreshStatus();
      }
    }).catch(function (e) {
      el.addChStatus.textContent = "Ошибка: " + (e && e.message ? e.message : "сеть");
      el.addChStatus.className = "check-result err";
    }).finally(function () { el.btnAddChannel.disabled = false; });
  }

  function removeChannel(name) {
    if (!state.server) {
      if (state.gh.repo) ghRemoveChannel(name);
      return;
    }
    apiPost("/api/channels/remove", { url: name }).then(function (j) {
      toast((j && j.message) || (j && j.ok ? "Убрал" : "Не получилось"),
            j && j.ok ? "ok" : "err");
      refreshChannels();
      refreshStatus();
    }).catch(function (e) {
      toast("Ошибка: " + (e && e.message ? e.message : "сеть"), "err");
    });
  }

  function toggleAd(name, btn) {
    if (!state.server) return;
    var turningOn = !btn.classList.contains("on");
    apiPost("/api/ad", { state: turningOn ? "on" : "off", channel: name })
      .then(function (j) {
        toast((j && j.message) || (j && j.ok ? "Готово" : "Не получилось"),
              j && j.ok ? "ok" : "err");
        refreshStatus();
        refreshChannels();
      }).catch(function (e) {
        toast("Ошибка: " + (e && e.message ? e.message : "сеть"), "err");
      });
  }

  function saveTikTokToken() {
    var tok = String(el.inpTkToken.value || "").trim();
    if (!state.server || httpsPageHttpServer(state.server)) {
      el.checkResult.textContent = "Нет https-связи с сервером — токен нельзя сохранить";
      el.checkResult.className = "check-result err";
      return;
    }
    if (!tok) {
      el.checkResult.textContent = "Вставьте access_token (README → «Автопостинг TikTok»)";
      el.checkResult.className = "check-result err";
      return;
    }
    el.checkResult.textContent = "Сохраняю токен…";
    el.checkResult.className = "check-result";
    apiPost("/api/tiktok/token", { access_token: tok }).then(function (j) {
      el.checkResult.textContent = (j && j.message) || "";
      el.checkResult.className = "check-result " + (j && j.ok ? "ok" : "err");
      if (j && j.ok) {
        el.inpTkToken.value = "";
        toast("TikTok подключён — публикация автоматическая", "ok");
        refreshStatus();
      }
    }).catch(function (e) {
      el.checkResult.textContent = "Ошибка: " + (e && e.message ? e.message : "сеть");
      el.checkResult.className = "check-result err";
    });
  }

  // --- GitHub-режим: всё крутится в Actions репозитория ------------------------------

  function ghConfigured() {
    return !!(state.gh.repo && state.gh.token);
  }

  function ghHeaders(extra) {
    // Публичный репозиторий читается и БЕЗ токена: авторизацию добавляем
    // только когда токен реально есть — иначе GitHub отвечает 401 на всё.
    var h = { "Accept": "application/vnd.github+json" };
    if (state.gh.token) h["Authorization"] = "Bearer " + state.gh.token;
    return Object.assign(h, extra || {});
  }

  function ghErr(status) {
    if (status === 401)
      return new Error("токен не принят (401). Настройки → GitHub → «Токен»: вставьте его ЦЕЛИКОМ, начинается с ghp_ или github_pat_, без пробелов");
    if (status === 403)
      return new Error("нет доступа (403). Токену нужно право Contents: Read and write на этот репозиторий");
    return new Error("HTTP " + status);
  }

  function fetchGhClips() {
    if (state.server && state.everAnswered) return;   // живой сервер ПК — приоритет
    fetch("clips.json?ts=" + Date.now(), { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (j) {
        state.ghEver = true;
        state.ghMeta = (j && j.meta) || {};
        if (state.server) {
          // адрес был угадан, но сервер молчит — переключаемся на GitHub
          if (state.everAnswered || state.polls < 3) return;
          state.server = null;
          lsSet(LS_SERVER, "");
          loadDownloaded();
          updateBadge();
        }
        state.clips = (j && j.clips) || [];
        state.freshUpdated = (j && j.updated) || "";
        setGhPill(true);
        el.skeleton.hidden = true;
        renderStatus({ channels: state.ghMeta.channels || [] });
        renderClips();
        renderFreshness();
      })
      .catch(function () {
        if (!state.server) setGhPill(false);
        renderClips();
        renderFreshness();
      });
  }

  function ghApi(path, opts) {
    opts = opts || {};
    var hadToken = !!state.gh.token;
    opts.headers = ghHeaders(opts.headers);
    var isGet = !opts.method || opts.method === "GET";
    return fetch("https://api.github.com" + path, opts).then(function (r) {
      if (!r.ok) {
        // Сохранённый токен испорчен, но репозиторий публичный: чтение
        // пробуем ещё раз анонимно, чтобы сайт продолжал работать.
        if (r.status === 401 && hadToken && isGet) {
          var h2 = Object.assign({}, opts.headers);
          delete h2.Authorization;
          return ghApi(path, { method: opts.method, headers: h2 });
        }
        throw ghErr(r.status);
      }
      return r.json();
    });
  }

  function ghGetSubs() {
    return ghApi("/repos/" + state.gh.repo + "/contents/subscriptions.json")
      .then(function (j) {
        var txt = "";
        try { txt = decodeURIComponent(escape(atob((j.content || "").replace(/\s/g, "")))); }
        catch (e) {}
        var data = {};
        try { data = JSON.parse(txt); } catch (e) {}
        return { sha: j.sha, channels: (data.channels || []).map(String) };
      });
  }

  function ghPutSubs(channels, sha, message) {
    var json = JSON.stringify({
      _help: "Ссылки на стримеров — по одной. Добавляйте на сайте или правьте прямо здесь.",
      channels: channels
    }, null, 2);
    return ghApi("/repos/" + state.gh.repo + "/contents/subscriptions.json", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: message,
        content: btoa(unescape(encodeURIComponent(json))),
        sha: sha
      })
    });
  }

  function ghChanItem(raw) {
    var s = String(raw || "").trim();
    var yt = /youtube\.com|youtu\.be/i.test(s);
    return { name: s, display: s, platform: yt ? "youtube" : "twitch",
             live: false, viewers: 0, title: "" };
  }

  function ghRefreshChannels() {
    var pending = loadPending();
    ghGetSubs().then(function (j) {
      var items = j.channels.map(ghChanItem);
      // каналы, добавленные с телефона, но ещё не залитые в репозиторий
      var repoLow = j.channels.map(function (c) {
        return String(c).trim().toLowerCase();
      });
      pending.forEach(function (p) {
        if (repoLow.indexOf(String(p).trim().toLowerCase()) < 0) {
          var it = ghChanItem(p);
          it.pending = true;
          items.push(it);
        }
      });
      renderChannelList(items, null);
      el.tkStatus.innerHTML = "<b>Слежение в GitHub</b>: задача Check live проверяет " +
        "каналы каждые ~20 секунд, Monitor включает запись сам, клипы с видео " +
        "режет Clip video. Уведомление о готовом клипе придёт на это устройство " +
        "(если включены пуши или приложение ntfy).";
    }).catch(function (e) {
      setChannelsOffline("GitHub: " + (e && e.message ? e.message : "ошибка") +
        " — клипы всё равно показываются; для правки каналов вставьте токен в Настройках");
    });
  }

  function syncCode() {
    // Код синхронизации: base64(JSON {каналы, настройки качества, фидбек,
    // заказы «Ещё отсюда»}). Пользователь пришлёт его в чат — и всё это
    // окажется в репозитории (публичный сайт не может писать в GitHub сам:
    // токен в JS не положишь).
    var list = loadPending();
    var fb = fbItems(loadFb());
    var reqs = loadRequests();
    var q = loadQuality();
    var qDirty = !qualityIsDefault();
    if (!list.length && !fb.length && !reqs.length && !qDirty) {
      el.syncCode.textContent = "Нет новых данных — всё уже в GitHub " +
        "(каналы добавьте выше, а настройки и оценки станут кодом, когда измените)";
      el.syncCode.className = "check-result sync-code";
      return;
    }
    var json = JSON.stringify({ v: 2, channels: list, settings: q,
                                feedback: fb, requests: reqs });
    var code = btoa(unescape(encodeURIComponent(json)));
    el.syncCode.textContent = code;
    el.syncCode.className = "check-result sync-code show";
    var done = function () {
      toast("Код скопирован — пришлите его в чат Z.ai", "ok");
      el.addChStatus.textContent = "Код готов: каналы + настройки качества + " +
        "оценки «Интересно/Скучно» + заказы. Пришлите его в чат Z.ai — залью за минуту.";
      el.addChStatus.className = "check-result ok";
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code).then(done, done);   // даже без клипборда код виден
    } else {
      done();
    }
  }

  function ghAddChannel(url) {
    if (!state.gh.token) {
      // Без токена канал нельзя записать в GitHub прямо с сайта — сохраняем
      // локально: он виден в списке с пометкой и попадёт в монитор после
      // синхронизации (кнопка ниже).
      if (addPending(url)) {
        el.addChStatus.textContent = "Сохранил канал на устройстве. Нажмите «Синхронизировать каналы» " +
          "ниже и пришлите код в чат Z.ai — канал попадёт в монитор.";
        el.addChStatus.className = "check-result ok";
        el.inpChannel.value = "";
        toast("Канал сохранён — синхронизируйте с GitHub", "ok");
        ghRefreshChannels();
      } else {
        el.addChStatus.textContent = "Этот канал уже ждёт синхронизации";
        el.addChStatus.className = "check-result err";
      }
      return;
    }
    el.addChStatus.textContent = "Добавляю в subscriptions.json…";
    ghGetSubs().then(function (j) {
      var dup = (j.channels || []).some(function (c) {
        return String(c).trim().toLowerCase() === url.toLowerCase();
      });
      if (dup) {
        el.addChStatus.textContent = "Этот канал уже отслеживается";
        el.addChStatus.className = "check-result err";
        return null;
      }
      j.channels.push(url);
      return ghPutSubs(j.channels, j.sha, "channel: add " + url).then(function () {
        el.addChStatus.textContent = "Добавил: канал в subscriptions.json. " +
          "Проверка эфира подхватит его в течение 5 минут и будет следить постоянно.";
        el.addChStatus.className = "check-result ok";
        el.inpChannel.value = "";
        toast("Канал добавлен — следим постоянно", "ok");
        ghRefreshChannels();
      });
    }).catch(function (e) {
      el.addChStatus.textContent = "Ошибка GitHub: " +
        (e && e.message ? e.message : "сеть");
      el.addChStatus.className = "check-result err";
    });
  }

  function ghRemoveChannel(name) {
    ghGetSubs().then(function (j) {
      var kept = (j.channels || []).filter(function (c) {
        return String(c).trim() !== name;
      });
      if (kept.length === j.channels.length) { toast("Канал не найден", "err"); return null; }
      return ghPutSubs(kept, j.sha, "channel: remove " + name).then(function () {
        toast("Убрал из слежки", "ok");
        ghRefreshChannels();
      });
    }).catch(function (e) {
      toast("Ошибка GitHub: " + (e && e.message ? e.message : "сеть"), "err");
    });
  }

  function ghNormalizeRepo(v) {
    return String(v || "").trim()
      .replace(/^https?:\/\/github\.com\//i, "")
      .replace(/\.git$/i, "")
      .replace(/\/+$/, "");
  }

  function ghCheck() {
    var repo = ghNormalizeRepo(el.inpGhRepo.value);
    var tok = String(el.inpGhToken.value || "").trim();
    if (!repo || repo.indexOf("/") < 0) {
      el.ghStatus.textContent = "Введите репозиторий в виде владелец/имя";
      el.ghStatus.className = "check-result err";
      return;
    }
    if (tok && !/^(ghp|gho|ghu|ghs|ghr|github_pat)_/.test(tok)) {
      el.ghStatus.textContent = "Это не похоже на токен GitHub: классический начинается с ghp_, fine-grained — с github_pat_. Скопируйте ВСЮ строку целиком";
      el.ghStatus.className = "check-result err";
      return;
    }
    if (!tok) {
      el.ghStatus.textContent = "Токен пустой: просмотр клипов и каналов работает и так. Чтобы менять каналы с сайта — вставьте токен (ghp_…)";
      el.ghStatus.className = "check-result";
      return;
    }
    el.ghStatus.textContent = "Проверяю доступ…";
    el.ghStatus.className = "check-result";
    fetch("https://api.github.com/repos/" + repo + "/contents/subscriptions.json", {
      headers: ghHeaders()
    }).then(function (r) {
      if (r.status === 401) throw new Error("токен не принят (401)");
      if (r.status === 403) throw new Error("токен без права Contents: Read and write (403)");
      if (r.status === 404) {
        // различаем: нет репозитория / нет файла (репозиторий пуст)
        return fetch("https://api.github.com/repos/" + repo, {
          headers: ghHeaders()
        }).then(function (rr) {
          if (rr.status === 404)
            throw new Error("репозиторий не найден — проверьте имя владелец/репозиторий и доступ токена (404)");
          throw new Error("репозиторий есть, но subscriptions.json ещё нет — загрузите файлы StreamClipper-GitHub.zip в репозиторий (404)");
        });
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      el.ghStatus.textContent = "Доступ есть: " + repo + " — нажмите «Сохранить», и «Каналы» будут работать с телефона";
      el.ghStatus.className = "check-result ok";
    }).catch(function (e) {
      el.ghStatus.textContent = "Не вышло: " + (e && e.message ? e.message : "сеть");
      el.ghStatus.className = "check-result err";
    });
  }

  function ghSave() {
    var repo = ghNormalizeRepo(el.inpGhRepo.value);
    var tok = String(el.inpGhToken.value || "").trim();
    state.gh.repo = repo;
    state.gh.token = tok;
    lsSet(LS_GH_REPO, repo);
    lsSet(LS_GH_TOKEN, tok);
    el.ghStatus.textContent = tok ? "Сохранено: GitHub-режим включён"
                                  : "Сохранено: только просмотр — для правки каналов вставьте токен (ghp_…)";
    el.ghStatus.className = "check-result ok";
    toast(tok ? "GitHub-режим сохранён" : "GitHub-режим очищен");
    if (!state.server) { setGhPill(false); fetchGhClips(); }
    renderClips();
  }

  // --- панель «Загруженное» -----------------------------------------------------------

  function openSheet() {
    el.sheet.hidden = false;
    el.sheetBackdrop.hidden = false;
    renderSheet();
  }

  function closeSheet() {
    el.sheet.hidden = true;
    el.sheetBackdrop.hidden = true;
  }

  function renderSheet() {
    var items = downloadedIds().map(function (id) {
      return Object.assign({ id: id }, state.downloaded[id]);
    }).sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });
    el.sheetList.innerHTML = "";
    if (!items.length) {
      el.sheetSub.textContent = "Видео, которые вы скачали, прячутся из общей ленты и остаются здесь.";
      var p = document.createElement("p");
      p.className = "sheet-empty";
      p.textContent = "Пока пусто. Скачайте клип — и он появится здесь.";
      el.sheetList.appendChild(p);
      return;
    }
    idbAll().then(function (records) {
      var off = {};
      var total = 0;
      (records || []).forEach(function (r) {
        off[r.id] = r;
        if (r.blob) total += r.blob.size;
      });
      el.sheetSub.textContent = "Скачанные видео" +
        (total ? " · офлайн-копий на " + fmtSize(total) : "") +
        " — «Смотреть» и «Скачать» работают даже без сети.";
      items.forEach(function (it) {
        el.sheetList.appendChild(sheetRow(it, off[it.id] || null));
      });
    });
  }

  function sheetRow(it, rec) {
    var row = document.createElement("div");
    row.className = "dl-item";

    var ic = document.createElement("span");
    ic.className = "ic";
    ic.innerHTML = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';
    row.appendChild(ic);

    var meta = document.createElement("div");
    meta.className = "meta";
    var nm = document.createElement("div");
    nm.className = "nm";
    nm.textContent = (it.channel ? it.channel : "клип") + (rec ? "  ·  офлайн" : "");
    var dt = document.createElement("div");
    dt.className = "dt";
    dt.textContent = fmtDate((it.ts || 0) / 1000 | 0) + " · " + fmtSize(it.size);
    meta.appendChild(nm);
    meta.appendChild(dt);
    row.appendChild(meta);

    if (rec) {
      var watch = document.createElement("button");
      watch.className = "watch";
      watch.title = "Смотреть офлайн";
      watch.innerHTML = ICON_PLAY;
      watch.addEventListener("click", function () { closeSheet(); openPlayer(it, rec); });
      row.appendChild(watch);
    }

    var again = document.createElement("button");
    again.className = "again";
    again.textContent = "Скачать";
    again.addEventListener("click", function () {
      if (rec && rec.blob) {
        saveBlob(rec.blob, it.file);
        toast("Файл сохранён из офлайн-копии", "ok");
        return;
      }
      var clip = findClip(it.id) || { id: it.id, file: it.file, size: it.size,
                                      url: "/files/" + encodeURIComponent(it.file) };
      downloadClip(clip, {
        disabled: false,
        querySelector: function () { return null; }
      });
      toast("Скачиваю…");
    });
    row.appendChild(again);

    var drop = document.createElement("button");
    drop.className = "drop";
    drop.textContent = "✕";
    drop.title = "Убрать из списка (офлайн-копия тоже удалится)";
    drop.addEventListener("click", function () {
      unmarkDownloaded(it.id);
      renderSheet();
      renderClips();
      toast("Убрано из «Загруженного» — клип вернулся в общий список");
    });
    row.appendChild(drop);

    return row;
  }

  function findClip(id) {
    for (var i = 0; i < state.clips.length; i++) {
      if (state.clips[i].id === id) return state.clips[i];
    }
    return null;
  }

  // --- настройки ----------------------------------------------------------------------

  function openSettings() {
    el.inpServer.value = state.server || "";
    el.inpGhRepo.value = state.gh.repo || "";
    el.inpGhToken.value = state.gh.token || "";
    updateMixWarning();
    el.checkResult.textContent = "";
    el.checkResult.className = "check-result";
    el.settings.hidden = false;
    el.modalBackdrop.hidden = false;
  }

  function closeSettings() {
    el.settings.hidden = true;
    el.modalBackdrop.hidden = true;
  }

  function normalizeServer(v) {
    v = String(v || "").trim();
    if (!v) return "";
    if (!/^https?:\/\//i.test(v)) v = "http://" + v;
    return v.replace(/\/+$/, "");
  }

  function updateMixWarning() {
    var v = normalizeServer(el.inpServer.value);
    el.mixWarn.hidden = !(location.protocol === "https:" && v.indexOf("http://") === 0);
  }

  function updateMixNoteEmpty() {
    var v = normalizeServer(el.inpEmpty.value);
    el.mixNoteEmpty.hidden = !(location.protocol === "https:" && v.indexOf("http://") === 0);
  }

  function checkServer(cb) {
    var v = normalizeServer(el.inpServer.value);
    if (!v) {
      el.checkResult.textContent = "Введите адрес сервера";
      el.checkResult.className = "check-result err";
      return;
    }
    el.checkResult.textContent = "Проверяю…";
    el.checkResult.className = "check-result";
    fetch(v + "/api/health", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (j) {
        el.checkResult.textContent = "Сервер на связи" + (j && j.app ? " (" + j.app + ")" : "");
        el.checkResult.className = "check-result ok";
        if (cb) cb(true);
      })
      .catch(function (e) {
        el.checkResult.textContent = "Не подключилось: " + e.message +
          (location.protocol === "https:" ? " (https-страница не может обращаться к http — см. предупреждение выше)" : "");
        el.checkResult.className = "check-result err";
        if (cb) cb(false);
      });
  }

  function connectFromEmpty() {
    var v = normalizeServer(el.inpEmpty.value);
    el.connectStatus.className = "connect-status busy";
    el.connectStatus.textContent = v ? "Подключаюсь…" : "Введите адрес сервера";
    if (!v) return;
    if (httpsPageHttpServer(v)) {
      el.mixNoteEmpty.hidden = false;
      el.connectStatus.className = "connect-status err";
      el.connectStatus.textContent = "https-страница не может открыть http-адрес";
      return;
    }
    el.mixNoteEmpty.hidden = true;
    fetch(v + "/api/health", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (j) {
        el.connectStatus.className = "connect-status ok";
        el.connectStatus.textContent = "Готово: сервер на связи" + (j && j.app ? " (" + j.app + ")" : "");
        applyServer(v, true);
        refreshClips();
        refreshStatus();
        startPolling();
        toast("Подключено к серверу", "ok");
      })
      .catch(function (e) {
        el.connectStatus.className = "connect-status err";
        el.connectStatus.textContent = "Не подключилось: " + (e && e.message ? e.message : "ошибка сети");
      });
  }

  // --- запуск ------------------------------------------------------------------------

  function applyServer(url, silent) {
    state.server = url;
    lsSet(LS_SERVER, url);
    loadDownloaded();
    updateBadge();
    state.everLoaded = false;
    el.skeleton.hidden = !url;          // на время первой загрузки — скелетон
    renderClips._lastSig = null;        // новый сервер — перерисовываем принудительно
    renderClips();
    setOnline(false);
    if (url) {
      refreshClips();
      refreshStatus();
    }
    if (!silent) toast(url ? "Сервер сохранён" : "Адрес сервера очищен");
  }

  function detectServer() {
    // 1) ?server=... в адресе страницы
    try {
      var q = new URLSearchParams(location.search).get("server");
      if (q) return normalizeServer(q);
    } catch (e) {}
    // 2) сохранённый выбор
    var saved = lsGet(LS_SERVER, "");
    if (saved) return saved;
    // 3) страница открыта прямо с сервера ПК
    if (location.protocol === "http:" && !/github\.io|githubusercontent|localhost\.github/.test(location.host)) {
      return location.origin;
    }
    return "";
  }

  function startPolling() {
    clearInterval(state.timers.clips);
    clearInterval(state.timers.status);
    clearInterval(state.timers.gh);
    state.timers.clips = setInterval(refreshClips, 4000);
    state.timers.status = setInterval(refreshStatus, 9000);
    state.timers.gh = setInterval(fetchGhClips, 60000);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && state.server) {
      refreshClips();
      refreshStatus();
    } else if (!document.hidden) {
      fetchGhClips();
    }
  });

  // события
  el.btnSettings.addEventListener("click", openSettings);
  el.connPill.addEventListener("click", openSettings);
  el.btnChannels.addEventListener("click", openChannels);
  el.channelsClose.addEventListener("click", closeChannels);
  el.channelsBackdrop.addEventListener("click", closeChannels);
  el.btnAddChannel.addEventListener("click", addChannel);
  el.inpChannel.addEventListener("keydown", function (e) {
    if (e.key === "Enter") addChannel();
  });
  el.btnTkSave.addEventListener("click", saveTikTokToken);
  el.btnGhCheck.addEventListener("click", ghCheck);
  el.btnGhSave.addEventListener("click", ghSave);
  el.btnSync.addEventListener("click", syncCode);

  // --- панель «Качество клипов»: подставляем сохранённое, ловим изменения ---------

  function applyQualityUi() {
    var q = loadQuality();
    if (el.qLang) el.qLang.value = String(q.speech_language || "auto");
    if (el.qSel) el.qSel.value = String(q.selection || "normal");
    if (el.qSkip) el.qSkip.checked = q.skip_no_speech !== false;
  }

  function onQualityChange() {
    var q = {
      speech_language: el.qLang ? el.qLang.value : "auto",
      selection: el.qSel ? el.qSel.value : "normal",
      skip_no_speech: el.qSkip ? el.qSkip.checked : true
    };
    saveQuality(q);
    toast("Сохранено — синхронизируйте в «Каналах», чтобы бот применил", "ok");
  }

  applyQualityUi();
  if (el.qLang) el.qLang.addEventListener("change", onQualityChange);
  if (el.qSel) el.qSel.addEventListener("change", onQualityChange);
  if (el.qSkip) el.qSkip.addEventListener("change", onQualityChange);

  // --- Web Push: уведомления прямо от иконки сайта ----------------------------------
  // Публичный VAPID-ключ (не секрет): приватный — в секретах репозитория.
  var VAPID_PUB = "BLSwyfg4ClPCi8o0imc7HAdgbVR0WMhmhyJZp0WjCVmBlO88yP3y4dA4amqBFCVEhfbO6fVyjDCFJAUFeL9Jilk";
  var PUSH_FILE = "push_subscriptions.json";

  function urlB64ToUint8(b64) {
    var pad = "=".repeat((4 - (b64.length % 4)) % 4);
    var raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function pushSay(text, ok) {
    el.pushStatus.textContent = text;
    el.pushStatus.className = "check-result " + (ok ? "ok" : "err");
  }

  function pushStandalone() {
    return !!(window.matchMedia && matchMedia("(display-mode: standalone)").matches) ||
           window.navigator.standalone === true;
  }

  function ghPutPushSubs(subs, sha, message) {
    var json = JSON.stringify({ subscriptions: subs });
    return ghApi("/repos/" + state.gh.repo + "/contents/" + PUSH_FILE, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: message,
        content: btoa(unescape(encodeURIComponent(json))),
        sha: sha
      })
    });
  }

  function savePushSubscription(sub) {
    // Подписка -> push_subscriptions.json в репозитории (читаем-меняем-пишем).
    var rec = { endpoint: sub.endpoint, keys: sub.toJSON().keys || {} };
    return ghApi("/repos/" + state.gh.repo + "/contents/" + PUSH_FILE, {
      headers: ghHeaders()
    }).then(function (r) {
      if (r.status === 404) return { sha: null, subs: [] };
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json().then(function (j) {
        var subs = [];
        try { subs = (JSON.parse(b64decodeUtf8(j.content)) || {}).subscriptions || []; } catch (_) {}
        return { sha: j.sha, subs: subs };
      });
    }).then(function (cur) {
      var kept = cur.subs.filter(function (s) {
        return s && s.endpoint !== rec.endpoint;
      });
      kept.unshift(rec);
      if (kept.length > 10) kept = kept.slice(0, 10);   // не раздуваем файл
      return ghPutPushSubs(kept, cur.sha, "push: подписка на уведомления");
    });
  }

  function b64decodeUtf8(b64) {
    return decodeURIComponent(escape(atob(String(b64 || "").replace(/\n/g, ""))));
  }

  function enablePush() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) ||
        !("Notification" in window)) {
      pushSay("Браузер не поддерживает Web Push (нужен Safari iOS 16.4+ или современный Chrome)");
      return;
    }
    if (!state.gh.repo || !state.gh.token) {
      pushSay("Сначала заполните «GitHub без компьютера» выше — подписку надо сохранить в репозиторий");
      return;
    }
    var ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
    if (ios && !pushStandalone()) {
      pushSay("Сначала добавьте сайт на экран «Домой»: Safari → Поделиться → «На экран „Домой“», откройте с иконки и нажмите снова", false);
      return;
    }
    pushSay("Запрашиваю разрешение…", true);
    Notification.requestPermission().then(function (perm) {
      if (perm !== "granted") {
        pushSay("Разрешение не выдано — в Настройках iOS: Safari/StreamClipper → Уведомления → Разрешить");
        return;
      }
      navigator.serviceWorker.ready.then(function (reg) {
        return reg.pushManager.getSubscription().then(function (old) {
          if (old) return old;
          return reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlB64ToUint8(VAPID_PUB)
          });
        });
      }).then(function (sub) {
        return savePushSubscription(sub).then(function () {
          return sub;
        });
      }).then(function (sub) {
        return navigator.serviceWorker.ready.then(function (reg) {
          reg.showNotification("Пуши включены", {
            body: "Так придёт сообщение о готовом клипе",
            icon: "icon-192.png",
            tag: "streamclipper-test",
            data: { url: "./" }
          });
          return sub;
        });
      }).then(function () {
        pushSay("Готово: уведомления включены — тест-пуш уже отправлен", true);
      }).catch(function (e) {
        pushSay("Не вышло: " + (e && e.message ? e.message : "сеть/права токена"));
      });
    });
  }

  function disablePush() {
    navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.getSubscription();
    }).then(function (sub) {
      var ep = sub && sub.endpoint;
      var un = sub ? sub.unsubscribe() : Promise.resolve();
      return un.then(function () {
        if (!ep || !state.gh.repo || !state.gh.token) return;
        return ghApi("/repos/" + state.gh.repo + "/contents/" + PUSH_FILE, {
          headers: ghHeaders()
        }).then(function (r) {
          if (!r.ok) return;
          return r.json().then(function (j) {
            var subs = [];
            try { subs = (JSON.parse(b64decodeUtf8(j.content)) || {}).subscriptions || []; } catch (_) {}
            var kept = subs.filter(function (s) { return s && s.endpoint !== ep; });
            if (kept.length !== subs.length)
              return ghPutPushSubs(kept, j.sha, "push: подписка удалена");
          });
        });
      });
    }).then(function () {
      pushSay("Пуши выключены", true);
    }).catch(function (e) {
      pushSay("Не вышло: " + (e && e.message ? e.message : "сеть"));
    });
  }

  el.btnPushOn.addEventListener("click", enablePush);
  el.btnPushOff.addEventListener("click", disablePush);
  navigator.serviceWorker.ready.then(function (reg) {
    return reg.pushManager.getSubscription();
  }).then(function (sub) {
    if (sub) pushSay("Пуши включены на этом устройстве", true);
  }).catch(function () {});
  el.modalBackdrop.addEventListener("click", closeSettings);
  el.btnSave.addEventListener("click", function () {
    var v = normalizeServer(el.inpServer.value);
    checkServer(function (ok) {
      if (ok || !v) {
        applyServer(v);
        closeSettings();
        startPolling();
      }
      // если не подключилось — настройки остаются открытыми, чтобы поправить адрес
    });
  });
  el.btnCheck.addEventListener("click", function () { checkServer(); });
  el.inpServer.addEventListener("input", updateMixWarning);
  el.inpEmpty.addEventListener("input", updateMixNoteEmpty);
  el.btnEmpty.addEventListener("click", connectFromEmpty);
  el.inpEmpty.addEventListener("keydown", function (e) {
    if (e.key === "Enter") connectFromEmpty();
  });
  el.dlCorner.addEventListener("click", function () {
    if (el.sheet.hidden) openSheet(); else closeSheet();
  });
  el.sheetBackdrop.addEventListener("click", closeSheet);
  el.sheetClear.addEventListener("click", function () {
    state.downloaded = {};
    saveDownloaded();
    idbClear();
    updateBadge();
    renderSheet();
    renderClips();
    toast("Список «Загруженное» и офлайн-копии очищены");
  });
  el.playerClose.addEventListener("click", closePlayer);
  // Браузер (особенно iPhone/Safari) иногда не может проиграть файл напрямую
  // из Releases (content-type octet-stream) — объясняем, что делать: скачать.
  el.playerVideo.addEventListener("error", function () {
    if (player.urlSrc) {
      el.playerNote.textContent = "Браузер не смог проиграть видео прямо с GitHub — " +
        "нажмите «Скачать файл»: телефон откроет его в своём плеере";
    }
  });
  el.playerBackdrop.addEventListener("click", closePlayer);
  el.playerDl.addEventListener("click", playerDownload);

  // старт
  if ("serviceWorker" in navigator) {
    try {
      navigator.serviceWorker.register("sw.js").catch(function () {
        /* например, http-адрес без secure context — сайт всё равно работает */
      });
      // НОВЫЙ сервис-воркер взял управление (обновление сайта) — перезагружаем
      // страницу ОДИН раз, чтобы телефон сразу показал свежий интерфейс.
      // Раньше старая версия жила в кэше неделями, и обновления «не доезжали»
      // (выглядело как «хэштеги не появились» / «нарезки не делаются»).
      navigator.serviceWorker.addEventListener("controllerchange", function () {
        try {
          var prev = Number(sessionStorage.getItem("sc.swReload") || 0);
          var now = Date.now();
          if (now - prev > 20000 && performance.now() > 5000) {
            sessionStorage.setItem("sc.swReload", String(now));
            location.reload();
          }
        } catch (e) {}
      });
    } catch (e) {}
  }

  var srv = detectServer();
  state.server = srv || "";
  var DEFAULT_GH_REPO = "qwied/TikTokAuto";   // этот сайт живёт в этом репозитории
  // Миграция: в старых версиях сайта репо по умолчанию было неверным
  // (qwied/streamclipper) — пользователь сохранил его, и раздел «Каналы»
  // ловил ошибки. Тихо заменяем на настоящий репозиторий.
  var savedRepo = lsGet(LS_GH_REPO, "");
  if (String(savedRepo).toLowerCase() === "qwied/streamclipper") {
    savedRepo = "";
    try { lsSet(LS_GH_REPO, DEFAULT_GH_REPO); } catch (e) {}
  }
  state.gh = { repo: savedRepo || DEFAULT_GH_REPO,
               token: lsGet(LS_GH_TOKEN, "") };
  loadDownloaded();
  updateBadge();
  setOnline(false);
  renderClips();
  renderFreshness();
  startPolling();
  setInterval(renderFreshness, 30000);   // счётчик «N мин назад» тикает сам
  if (srv) {
    el.skeleton.hidden = false;
    refreshClips();
    refreshStatus();
  } else {
    fetchGhClips();
  }
})();
