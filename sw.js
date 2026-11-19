/* StreamClipper — сервис-воркер: сайт работает офлайн всегда.
   При первом открытии кэширует весь интерфейс (оболочку), обновляет его
   в фоне при следующих визитах, а последние ответы API (лента каналов и
   список клипов) держит как офлайн-фолбэк. Видео скачанных клипов
   приложение хранит отдельно в IndexedDB — плеер работает без сети. */

"use strict";

var CACHE = "sc-shell-v12";  // v12: адаптивная длина клипов 35-60 с (по силе
                              // момента), RubUP в середине клипа, логический
                              // конец на паузе речи, split-макет (webcam.py) в проде
var SHELL = [
  "./",
  "./index.html",
  "./style.css",
  "./app.js",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png"
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(CACHE).then(function (c) {
      return c.addAll(SHELL);
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        return k === CACHE ? null : caches.delete(k);
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;

  var url = new URL(req.url);

  // Чужие источники (сервер ПК, туннель) не кэшируем: клипы хранит IndexedDB.
  if (url.origin !== self.location.origin) return;

  // API и лента клипов (clips.json в GitHub-режиме): сеть в приоритете,
  // при офлайне — последний ответ из кэша.
  if (url.pathname.indexOf("/api/") === 0 ||
      /(^|\/)clips\.json$/.test(url.pathname)) {
    e.respondWith(
      fetch(req).then(function (res) {
        if (res && res.ok) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(req, copy); });
        }
        return res;
      }).catch(function () {
        return caches.match(req).then(function (hit) {
          return hit || new Response(JSON.stringify({ offline: true }),
            { status: 503, headers: { "Content-Type": "application/json; charset=utf-8" } });
        });
      })
    );
    return;
  }

  // Навигация: оболочка из кэша + обновление в фоне; офлайн — всегда отдаём.
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put("./index.html", copy); });
        return res;
      }).catch(function () {
        return caches.match("./index.html");
      })
    );
    return;
  }

  // Файлы интерфейса (app.js/style.css/…): СНАЧАЛА СЕТЬ, кэш — только офлайн.
  // Раньше было «сперва кэш» — телефон неделями показывал старый интерфейс
  // (из-за этого «хэштеги не появляются»: они были в новой версии сайта,
  // а телефон держал старую). Теперь любое обновление видно сразу.
  var SHELL_NET_FIRST = {
    "./index.html": 1, "index.html": 1,
    "./app.js": 1, "app.js": 1,
    "./style.css": 1, "style.css": 1,
    "./manifest.webmanifest": 1, "manifest.webmanifest": 1
  };
  e.respondWith(
    (SHELL_NET_FIRST[url.pathname.split("/").pop()] ||
     url.pathname === "./" || url.pathname === "/")
      ? fetch(req).then(function (res) {
          if (res && res.ok && res.type === "basic") {
            var copy = res.clone();
            caches.open(CACHE).then(function (c) { c.put(req, copy); });
          }
          return res;
        }).catch(function () {
          return caches.match(req).then(function (hit) {
            return hit || (req.mode === "navigate"
              ? caches.match("./index.html") : Response.error());
          });
        })
      : caches.match(req).then(function (hit) {   // иконки: кэш, обновление в фоне
          var net = fetch(req).then(function (res) {
            if (res && res.ok && res.type === "basic") {
              var copy = res.clone();
              caches.open(CACHE).then(function (c) { c.put(req, copy); });
            }
            return res;
          }).catch(function () {
            return hit || Response.error();
          });
          return hit || net;
        })
  );
});

// --- Web Push: уведомление «Клип готов» прямо от иконки сайта ----------------

self.addEventListener("push", function (e) {
  var d = {};
  try { d = e.data.json(); } catch (_) {
    d = { title: "Клип готов", body: (e.data && e.data.text()) || "" };
  }
  e.waitUntil(self.registration.showNotification(d.title || "Клип готов", {
    body: d.body || "",
    icon: "./icon-192.png",
    badge: "./icon-192.png",
    tag: d.tag || "streamclipper",
    data: { url: d.url || "./" }
  }));
});

self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  e.waitUntil(self.clients.openWindow((e.notification.data || {}).url || "./"));
});
