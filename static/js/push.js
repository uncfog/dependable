// "On this phone or computer" on Notification settings
// (templates/notifications/settings.html): asks the browser for permission,
// subscribes it to Web Push with the site's public key, and hands the
// subscription to the server. Turning off undoes both. Nothing is stored here.
(function () {
  'use strict';

  var box = document.querySelector('[data-push]');
  if (!box) return;
  var status = box.querySelector('[data-push-status]');
  var onButton = box.querySelector('[data-push-on]');
  var offButton = box.querySelector('[data-push-off]');
  var token = box.querySelector('input[name="csrfmiddlewaretoken"]');

  function say(text) { status.textContent = text; }
  function show(on) { onButton.hidden = on; offButton.hidden = !on; }

  if (box.getAttribute('data-push-ready') !== 'yes') return;

  var iphone = /iPhone|iPad|iPod/.test(navigator.userAgent);
  var installed = window.navigator.standalone === true ||
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    say(iphone && !installed
      ? 'On an iPhone or iPad, add SCHOOLCORD to your Home Screen first (Share, then Add to Home Screen), open it from there, and come back here.'
      : 'This browser cannot show notifications when SCHOOLCORD is closed.');
    return;
  }

  function keyBytes(base64) {
    var padded = (base64 + '===='.slice(base64.length % 4)).replace(/-/g, '+').replace(/_/g, '/');
    var raw = window.atob(padded);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token ? token.value : '' },
      body: JSON.stringify(body)
    }).then(function (response) {
      if (!response.ok) throw new Error('the server refused it (' + response.status + ')');
      return response;
    });
  }

  var settled = false;

  function refresh(registration) {
    return registration.pushManager.getSubscription().then(function (subscription) {
      settled = true;
      if (subscription && Notification.permission === 'granted') {
        say('On. This device shows your notifications, even when SCHOOLCORD is closed.');
        show(true);
        // Keep the server's copy current: a browser can renew its address.
        post(box.getAttribute('data-subscribe-url'), subscription.toJSON()).catch(function () {});
      } else if (Notification.permission === 'denied') {
        say('Blocked in this browser’s settings. Allow notifications for this site there, then come back.');
        onButton.hidden = true;
        offButton.hidden = true;
      } else {
        say('Off on this device.');
        show(false);
      }
    });
  }

  navigator.serviceWorker.ready.then(function (registration) {
    refresh(registration);

    onButton.addEventListener('click', function () {
      onButton.disabled = true;
      Notification.requestPermission().then(function (permission) {
        if (permission !== 'granted') return refresh(registration);
        return registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: keyBytes(box.getAttribute('data-push-key'))
        }).then(function (subscription) {
          return post(box.getAttribute('data-subscribe-url'), subscription.toJSON());
        }).then(function () { return refresh(registration); });
      }).catch(function (error) {
        say('Could not turn it on: ' + (error && error.message ? error.message : 'try again.'));
      }).then(function () { onButton.disabled = false; });
    });

    offButton.addEventListener('click', function () {
      offButton.disabled = true;
      registration.pushManager.getSubscription().then(function (subscription) {
        if (!subscription) return null;
        var endpoint = subscription.endpoint;
        return subscription.unsubscribe().then(function () {
          return post(box.getAttribute('data-unsubscribe-url'), { endpoint: endpoint });
        });
      }).catch(function () {}).then(function () {
        offButton.disabled = false;
        return refresh(registration);
      });
    });
  });

  // The service worker is registered by js/ui.js; on a first visit it may not be
  // running yet.
  window.setTimeout(function () {
    if (!settled) say('Reload this page once to finish setting up this device.');
  }, 6000);
})();
