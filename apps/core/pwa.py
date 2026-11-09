"""Installing Schoolcord to a phone's home screen, without keeping anything private on it.

The service worker keeps static files and a plain offline page, and nothing else. Pages
are never stored, whoever is signed in, because staff share office computers and these
pages hold children's and financial data. So signing out has nothing to clear, and there
is no offline mode for records or money.

A faulty service worker outlives the page that installed it, so ``PWA_ENABLED=false``
serves one whose only job is to remove itself from every phone that has it.
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.templatetags.static import static
from django.views import View

THEME = "#0f2547"

_WORKER = """'use strict';
// Static files and the offline page only. Pages are never stored.
const CACHE = 'schoolcord-static-%(version)s';
const OFFLINE = '%(offline)s';
const STATIC_PREFIX = '%(prefix)s';
const ICON = '%(icon)s';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.add(OFFLINE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith(STATIC_PREFIX)) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
        if (response.ok && response.type === 'basic') {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      }))
    );
    return;
  }

  // A page: from the network, always. Without one, the offline page and no copy of it.
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE)));
  }
});

// A notification pushed by the server (apps/notifications/push.py), shown by the
// phone or computer even when SCHOOLCORD is closed. Nothing is stored: the text
// arrives in the push and goes to the system's notification centre.
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (error) { data = {}; }
  event.waitUntil(self.registration.showNotification(data.title || 'SCHOOLCORD', {
    body: data.body || '',
    tag: data.tag || undefined,
    icon: ICON,
    badge: ICON,
    data: { url: data.url || '/notifications/' },
  }));
});

// A tap opens the page it is about, in an open SCHOOLCORD window if there is one.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || '/notifications/',
                         self.location.origin);
  if (target.origin !== self.location.origin) return;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if (new URL(client.url).origin === target.origin && 'focus' in client) {
          return client.focus().then((focused) => (focused || client).navigate(target.href));
        }
      }
      return self.clients.openWindow(target.href);
    })
  );
});
"""

_REMOVER = """'use strict';
// Switched off on the server: clear everything and step aside.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.map((name) => caches.delete(name))))
      .then(() => self.registration.unregister())
  );
});
"""


def _version() -> str:
    """Changes when the static files do, so a release replaces the cache."""
    seen = "|".join(static(path) for path in ("css/app.css", "js/ui.js", "offline.html"))
    return hashlib.sha256(seen.encode()).hexdigest()[:12]


class ServiceWorkerView(View):
    def get(self, request):
        if settings.PWA_ENABLED:
            body = _WORKER % {
                "version": _version(),
                "offline": static("offline.html"),
                "prefix": settings.STATIC_URL,
                "icon": static("img/icon-192.png"),
            }
        else:
            body = _REMOVER
        response = HttpResponse(body, content_type="application/javascript")
        # Browsers must keep asking for this file, or the off switch can't reach them.
        response["Cache-Control"] = "no-cache"
        return response


class ManifestView(View):
    def get(self, request):
        icons = [
            {"src": static("img/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("img/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {
                "src": static("img/icon-maskable-512.png"), "sizes": "512x512",
                "type": "image/png", "purpose": "maskable",
            },
        ]
        response = JsonResponse({
            "name": "SCHOOLCORD",
            "short_name": "SCHOOLCORD",
            "description": "School management: students, fees and payments.",
            "start_url": "/dashboard/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": THEME,
            "icons": icons,
        }, content_type="application/manifest+json")
        response["Cache-Control"] = "public, max-age=3600"
        return response


def pwa_context(request) -> dict:
    return {"pwa_enabled": settings.PWA_ENABLED}


class PrivatePagesMiddleware:
    """A signed-in page isn't kept by the browser either, for the next person at the desk."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and "Cache-Control" not in response:
            response["Cache-Control"] = "private, no-store"
        return response
