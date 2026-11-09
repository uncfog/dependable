/* SCHOOLCORD interaction layer.
 *
 * No dependencies and no build step: this file is served as written. Every
 * behaviour here is an enhancement over markup that already works without it,
 * so a blocked or failed script costs polish and never content.
 *
 * Contents:
 *   - performance tier      what this device can afford
 *   - theme                 light/dark switching
 *   - scroll reveals        sections arriving as they are reached
 *   - landing nav           sticky state and scroll-spy
 *   - auth panel swap       sign-in <-> sign-up
 *   - repeatable rows       bulk entry: classes, subjects, staff, fee items
 *   - toasts                every action's answer, from the bottom of the screen
 */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  /* ====================================================================== *
   * Performance tier
   *
   * Frosted glass and a blurred full-bleed hero are the two most expensive
   * things this design does, and both are decoration. Rather than have each
   * component guess, one attribute on <html> says what the device can
   * afford and the stylesheet's `[data-perf="lite"]` block turns the
   * expensive half off in one place.
   *
   * The signals are coarse on purpose. `deviceMemory` and `hardwareConcurrency`
   * are not precise, but a 2GB/4-core Android is exactly the machine a
   * Nigerian school office actually runs, and getting that case right matters
   * more than classifying every laptop correctly. Absent values mean "assume
   * capable", because Safari reports neither and is not a slow browser.
   * ====================================================================== */
  function initPerfTier() {
    var root = document.documentElement;
    var memory = navigator.deviceMemory;          // GB, in steps, Chromium only
    var cores = navigator.hardwareConcurrency;    // logical processors

    var lite =
      (typeof memory === 'number' && memory > 0 && memory <= 4) ||
      (typeof cores === 'number' && cores > 0 && cores <= 4);

    // A data saver request is an explicit statement about this connection and
    // outranks anything inferred from the hardware.
    var connection = navigator.connection;
    if (connection && connection.saveData) lite = true;

    if (lite) root.setAttribute('data-perf', 'lite');
  }

  /* ====================================================================== *
   * Theme
   *
   * The stored value is one of "light", "dark" or absent. Absent means
   * "follow the OS", which is the default and stays live: a user who has
   * never chosen sees their system preference change under them, as they
   * should. The <html data-theme> attribute is applied by a tiny inline
   * script in <head> so the first paint is already correct; this half only
   * handles switching after load.
   * ====================================================================== */
  var STORAGE_KEY = 'schoolcord-theme';

  function storedTheme() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      // Private mode, or storage disabled by policy. Not an error worth
      // surfacing -- the theme simply stops persisting between pages.
      return null;
    }
  }

  function systemTheme() {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function activeTheme() {
    return storedTheme() || systemTheme();
  }

  function applyTheme(theme, animate) {
    var root = document.documentElement;

    // Colour-only transition, and only for the swap itself -- left on
    // permanently it would smear every hover state in the app.
    if (animate && !reduceMotion.matches) {
      root.classList.add('theme-anim');
      window.setTimeout(function () { root.classList.remove('theme-anim'); }, 220);
    }

    root.setAttribute('data-theme', theme);

    // Keep the mobile browser chrome in step with the page.
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      meta.setAttribute('content', getComputedStyle(root)
        .getPropertyValue('--color-panel').trim() || '#0f2547');
    }

    syncToggles(theme);
  }

  function syncToggles(theme) {
    var options = document.querySelectorAll('[data-theme-set]');
    for (var i = 0; i < options.length; i++) {
      var pressed = options[i].getAttribute('data-theme-set') === theme;
      options[i].setAttribute('aria-pressed', pressed ? 'true' : 'false');
    }
  }

  function initTheme() {
    // A page that is light-only by design says so on <html>; neither a stored
    // choice nor the OS following dark mode may repaint it.
    if (document.documentElement.hasAttribute('data-theme-lock')) return;

    // The markup ships the switch hidden; it only makes sense once the code
    // that services it is running.
    var toggles = document.querySelectorAll('[data-theme-toggle]');
    for (var t = 0; t < toggles.length; t++) toggles[t].hidden = false;

    syncToggles(activeTheme());

    document.addEventListener('click', function (event) {
      var option = event.target.closest && event.target.closest('[data-theme-set]');
      if (!option) return;
      var choice = option.getAttribute('data-theme-set');

      // Replay the icon's entrance. Removing the class and forcing a reflow
      // before re-adding it is what lets the same animation run twice.
      option.classList.remove('is-switching');
      void option.offsetWidth;
      option.classList.add('is-switching');

      try {
        window.localStorage.setItem(STORAGE_KEY, choice);
      } catch (e) { /* see storedTheme */ }
      applyTheme(choice, true);
    });

    // Only meaningful while the user has made no explicit choice.
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      if (!storedTheme()) applyTheme(systemTheme(), true);
    });
  }

  /* ====================================================================== *
   * Scroll reveal
   *
   * Armed from JS rather than in the stylesheet: the markup ships visible, so
   * the failure mode of this whole section is "no animation", never "no
   * content".
   *
   * `data-reveal-group` staggers its children instead of revealing them as
   * one block. The delay is capped at four steps -- past that a grid stops
   * looking sequenced and starts looking slow.
   * ====================================================================== */
  var STAGGER_MS = 70;
  var STAGGER_CAP = 4;

  function initReveals() {
    var targets = Array.prototype.slice.call(document.querySelectorAll('.reveal'));
    if (!targets.length || reduceMotion.matches || !('IntersectionObserver' in window)) return;

    // Stagger before arming, so the delay is in place the first time each
    // element is painted in its armed state.
    var groups = document.querySelectorAll('[data-reveal-group]');
    for (var g = 0; g < groups.length; g++) {
      var children = groups[g].querySelectorAll('.reveal');
      for (var c = 0; c < children.length; c++) {
        var step = Math.min(c, STAGGER_CAP);
        children[c].style.setProperty('--reveal-delay', (step * STAGGER_MS) + 'ms');
      }
    }

    targets.forEach(function (el) { el.classList.add('reveal-armed'); });

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.remove('reveal-armed');
        entry.target.classList.add('reveal-in');
        observer.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });

    targets.forEach(function (el) { observer.observe(el); });

    // A reveal that never fires is content nobody can read. The net below
    // catches that: a few seconds in, anything still armed *and already on
    // screen* is shown regardless of what the observer thinks.
    //
    // Scoped to what is on screen on purpose. Un-arming everything would mean
    // that a visitor who spends ten seconds on the hero gets no animation for
    // the rest of the page -- the net would have quietly disabled the effect
    // it exists to protect.
    window.setTimeout(function () {
      targets.forEach(function (el) {
        if (!el.classList.contains('reveal-armed')) return;
        var box = el.getBoundingClientRect();
        var onScreen = box.top < window.innerHeight && box.bottom > 0;
        if (!onScreen) return;
        el.classList.remove('reveal-armed');
        el.classList.add('reveal-in');
      });
    }, 4000);
  }

  /* ====================================================================== *
   * Lazy images
   *
   * `loading="lazy"` is on every uploaded image already and does most of this
   * job. IntersectionObserver adds the part the attribute cannot: a margin of
   * our choosing, so a receipt starts fetching a screen and a half before it
   * is needed rather than when the browser decides, and a fade as it lands
   * instead of a pop.
   *
   * Opt-in via `data-src`. The markup carries a `<noscript>` twin with a plain
   * `src`, so a blocked script costs nothing -- see payments/pending_queue.html.
   * ====================================================================== */
  function initLazyImages() {
    var images = document.querySelectorAll('img[data-src]');
    if (!images.length) return;

    function load(img) {
      var src = img.getAttribute('data-src');
      if (!src) return;
      img.addEventListener('load', function () {
        img.classList.add('is-loaded');
      }, { once: true });
      img.src = src;
      img.removeAttribute('data-src');
    }

    if (!('IntersectionObserver' in window)) {
      // No observer: fetch them all rather than leave blanks on the page.
      Array.prototype.forEach.call(images, load);
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        load(entry.target);
        observer.unobserve(entry.target);
      });
    }, { rootMargin: '150% 0px' });

    Array.prototype.forEach.call(images, function (img) { observer.observe(img); });
  }

  /* ====================================================================== *
   * Landing navigation
   *
   * Two jobs: tell the bar when it has left the top of the hero, and mark
   * which section is currently being read.
   *
   * The scroll-spy sets `aria-current` rather than a class of its own, so the
   * underline and the announced state are the same fact. Scrolling itself is
   * the browser's -- `scroll-behavior: smooth` plus `scroll-padding-top` in
   * the stylesheet -- which means the links stay real anchors that work with
   * the keyboard, open in a new tab, and survive this script failing.
   * ====================================================================== */
  function initLandingNav() {
    var nav = document.querySelector('[data-lp-nav]');
    if (!nav) return;

    var links = Array.prototype.slice.call(nav.querySelectorAll('[data-lp-link]'));
    var sections = links
      .map(function (link) {
        var id = (link.getAttribute('href') || '').replace(/^#/, '');
        return id ? document.getElementById(id) : null;
      })
      .filter(Boolean);

    // The bar's own state. rAF-throttled: a scroll handler that writes a class
    // on every event is the classic way to make a smooth page stutter.
    var ticking = false;
    function onScroll() {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(function () {
        nav.classList.toggle('is-scrolled', window.scrollY > 24);
        ticking = false;
      });
    }
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();

    if (!sections.length || !('IntersectionObserver' in window)) return;

    // While this is in the future the scroll-spy stands down: a click has
    // claimed the marker and the scroll it triggered must not argue with it.
    var claimedUntil = 0;

    function mark(id) {
      links.forEach(function (link) {
        var mine = (link.getAttribute('href') || '') === '#' + id;
        if (mine) link.setAttribute('aria-current', 'true');
        else link.removeAttribute('aria-current');
      });
    }

    // Which section "is being read" is the topmost one whose box crosses the
    // upper third of the viewport. Tracking visibility ratios instead gives
    // the wrong answer whenever one section is much taller than another.
    var visible = {};
    var spy = new IntersectionObserver(function (entries) {
      // While a click-scroll is in flight the observer is reporting every
      // section the page flies past. Honouring that would drag the underline
      // across the whole bar on the way to the one that was actually asked
      // for, so the spy is muted until the scroll settles.
      if (claimedUntil > Date.now()) return;
      entries.forEach(function (entry) {
        visible[entry.target.id] = entry.isIntersecting;
      });
      for (var i = 0; i < sections.length; i++) {
        if (visible[sections[i].id]) { mark(sections[i].id); return; }
      }
    }, { rootMargin: '-25% 0px -60% 0px', threshold: 0 });

    sections.forEach(function (section) { spy.observe(section); });

    // A click moves the underline *now*. Waiting for the smooth scroll to
    // carry the section into the spy's band means pressing a nav item and
    // watching nothing happen for most of a second -- the control looks
    // broken even though it is working.
    nav.addEventListener('click', function (event) {
      var link = event.target.closest('[data-lp-link]');
      if (!link) return;
      var id = (link.getAttribute('href') || '').replace(/^#/, '');
      if (!id || !document.getElementById(id)) return;

      mark(id);
      // Long enough for a smooth scroll across the page to land, short enough
      // that a user who starts scrolling by hand straight afterwards is not
      // left with a stale marker. Reduced motion jumps, so it needs no grace.
      claimedUntil = Date.now() + (reduceMotion.matches ? 0 : 700);
    });
  }

  /* ====================================================================== *
   * Sidebar
   *
   * Two jobs, both about not losing the user's place.
   *
   * **Collapsing** narrows the rail to its icons. The state lives on <html>
   * as `data-sidebar`, which is what lets one attribute drive both the rail's
   * width and the content column's padding -- they have to move together or
   * the page tears down the middle. It is written to localStorage and read
   * back by a tiny inline script in <head>, the same trick the theme uses, so
   * a collapsed rail is already collapsed at first paint instead of snapping
   * shut a moment after the page appears.
   *
   * **Scroll position** is remembered per page load. A long menu scrolled to
   * Payments jumped back to the top on every navigation, which on a rail this
   * tall means hunting for where you were on every single click.
   * ====================================================================== */
  var SIDEBAR_KEY = 'schoolcord-sidebar';
  var SIDEBAR_SCROLL_KEY = 'schoolcord-sidebar-scroll';

  function initSidebar() {
    var root = document.documentElement;
    var toggles = document.querySelectorAll('[data-sidebar-toggle]');

    for (var t = 0; t < toggles.length; t++) toggles[t].hidden = false;

    function syncToggles() {
      var collapsed = root.getAttribute('data-sidebar') === 'collapsed';
      var buttons = document.querySelectorAll('[data-sidebar-toggle]');
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        var label = collapsed ? 'Expand the sidebar' : 'Collapse the sidebar';
        buttons[i].setAttribute('title', label);
        var sr = buttons[i].querySelector('.sr-only');
        if (sr) sr.textContent = label;
      }
    }

    syncToggles();

    document.addEventListener('click', function (event) {
      var button = event.target.closest && event.target.closest('[data-sidebar-toggle]');
      if (!button) return;
      var collapsed = root.getAttribute('data-sidebar') === 'collapsed';
      if (collapsed) root.removeAttribute('data-sidebar');
      else root.setAttribute('data-sidebar', 'collapsed');
      try {
        window.localStorage.setItem(SIDEBAR_KEY, collapsed ? 'expanded' : 'collapsed');
      } catch (e) { /* private mode: the choice stops persisting, nothing more */ }
      syncToggles();
    });

    initSidebarScroll();
  }

  /* ======================================================================
   * The school logo's file input
   *
   * A file input hidden behind a styled label says nothing about what was
   * chosen, so the page would sit there looking unchanged after somebody picked
   * a file -- the exact confusion this control was rebuilt to remove. This
   * echoes the filename beside the button.
   *
   * Progressive: with the script blocked the span keeps its server-rendered "No
   * file chosen", the input still works, and the form still saves.
   * ====================================================================== */
  function initLogoInput() {
    var inputs = document.querySelectorAll('.logo-file');
    for (var i = 0; i < inputs.length; i++) {
      bindLogoInput(inputs[i]);
    }
  }

  function bindLogoInput(input) {
    // The echo lives after the input in the same row -- see
    // templates/core/_logo_field.html.
    var echo = input.parentNode
      ? input.parentNode.querySelector('[data-logo-filename]')
      : null;
    if (!echo) return;

    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (!file) {
        echo.textContent = 'No file chosen';
        echo.classList.remove('font-medium', 'text-ink-900');
        return;
      }
      // Kilobytes, because the limit people meet is stated in megabytes and a
      // raw byte count is not something anyone checks against it.
      var kb = Math.max(1, Math.round(file.size / 1024));
      echo.textContent = file.name + ' (' + kb.toLocaleString() + ' KB)';
      echo.classList.add('font-medium', 'text-ink-900');
    });
  }

  /* ======================================================================
   * Buttons in flight
   *
   * A form submit is a page load away, and on a slow connection the only thing
   * that happens when you press Save is nothing. So the button that fired it
   * swaps its own glyph for a spinner and stops accepting clicks -- which is also
   * what stops somebody pressing Save three times and creating three of a thing.
   *
   * One listener on the document, on `submit`. That covers every form on the app
   * in one place: sign in, sign up, save, invite, record a payment, confirm,
   * void -- and every form written after this, without anybody remembering to
   * opt in.
   *
   * Three details that matter:
   *
   *   - `event.submitter` is the button that was pressed, which is the one to
   *     mark. A form with Save and "Save and add another" must not light both;
   *   - the button is **not** given the `disabled` attribute. Several submit
   *     buttons here carry a name and value the view reads
   *     (`save_and_add_another`, `action=import`), and a button disabled inside
   *     its own submit handler can be dropped from the payload -- silently
   *     changing what the form did. `pointer-events: none` via the class blocks
   *     the second click and touches nothing that gets sent;
   *   - nothing here calls `preventDefault`. If the browser is going to submit,
   *     it submits; if HTML validation refuses, `submit` never fires and no
   *     button is left spinning.
   *
   * Opt out with `data-no-loading` on the form or the button -- for a submit that
   * does not navigate, where a spinner would never stop.
   * ====================================================================== */
  function initButtonLoading() {
    document.addEventListener('submit', function (event) {
      var form = event.target;
      if (!form || form.tagName !== 'FORM') return;
      if (form.hasAttribute('data-no-loading')) return;

      // A form already in flight. Nothing to do -- and nothing to re-mark.
      if (form.dataset.submitting === 'true') return;
      form.dataset.submitting = 'true';

      var button = event.submitter;
      if (!button) {
        // No submitter: an Enter press in a text field, or `form.submit()`.
        // Mark the form's own default button so something still responds.
        button = form.querySelector(
          'button[type="submit"]:not([data-no-loading]), input[type="submit"]'
        );
      }
      if (!button || button.hasAttribute('data-no-loading')) return;
      if (button.tagName !== 'BUTTON') return;   // <input> has no ::after to spin

      markLoading(button);
    }, true);

    // A navigation that never happened -- the back button, or a browser
    // restoring a cached page -- must not leave a button spinning forever.
    window.addEventListener('pageshow', clearAllLoading);
  }

  function markLoading(button) {
    button.classList.add('is-loading');
    button.setAttribute('aria-busy', 'true');
    button.setAttribute('aria-disabled', 'true');
  }

  function clearAllLoading() {
    var stuck = document.querySelectorAll('.is-loading');
    for (var i = 0; i < stuck.length; i++) {
      stuck[i].classList.remove('is-loading');
      stuck[i].removeAttribute('aria-busy');
      stuck[i].removeAttribute('aria-disabled');
    }
    var forms = document.querySelectorAll('form[data-submitting]');
    for (var f = 0; f < forms.length; f++) delete forms[f].dataset.submitting;
  }

  /** Public, for anything doing its own async work. `ui.loading(button)`. */
  function setLoading(button, on) {
    if (!button) return;
    if (on === false) {
      button.classList.remove('is-loading');
      button.removeAttribute('aria-busy');
      button.removeAttribute('aria-disabled');
    } else {
      markLoading(button);
    }
  }

  function initSidebarScroll() {
    var panes = document.querySelectorAll('[data-sidebar-scroll]');
    if (!panes.length) return;

    // Restored before paint would be better still, but the rail's height is
    // not known until layout -- so this runs as early as the script does and
    // the jump is imperceptible.
    var saved = 0;
    try {
      saved = parseInt(window.sessionStorage.getItem(SIDEBAR_SCROLL_KEY) || '0', 10);
    } catch (e) { /* see below */ }

    Array.prototype.forEach.call(panes, function (pane) {
      if (saved > 0 && pane.scrollHeight > pane.clientHeight) pane.scrollTop = saved;

      // Written on scroll rather than on unload: a `beforeunload` handler is
      // unreliable on mobile, where a page is often frozen rather than
      // unloaded, and it blocks the back-forward cache.
      var pending;
      pane.addEventListener('scroll', function () {
        window.clearTimeout(pending);
        pending = window.setTimeout(function () {
          try {
            window.sessionStorage.setItem(SIDEBAR_SCROLL_KEY, String(pane.scrollTop));
          } catch (e) { /* private mode, storage disabled by policy */ }
        }, 150);
      }, { passive: true });
    });
  }

  /* ====================================================================== *
   * Auth panel swap
   *
   * Sign-in and sign-up are two ordinary pages, not one page with a toggle --
   * which is the right shape for a form that posts, and keeps both URLs
   * bookmarkable. The swap is therefore an *entry* animation on the page you
   * land on: the visual panel slides in from the side it now lives on, and
   * the form slides in from the other.
   *
   * A flag in sessionStorage is what tells the second page it was reached
   * from the first, so the crossing is played at full travel when the two
   * screens genuinely traded places, and kept to a short settle otherwise.
   * ====================================================================== */
  /* The in-place switch.
   *
   * Both panels are in the DOM (registration/_auth_switch.html), so moving
   * between sign-in and sign-up is a class change and an animation -- no
   * fetch, no navigation, nothing to wait for. The URL is rewritten with
   * `pushState` so the address bar, the back button and a bookmark all still
   * agree with what is on screen, and `popstate` puts it back.
   *
   * The links stay real hrefs to real URLs. With this script blocked they
   * navigate, the other page loads, and the only thing lost is the transition.
   */
  function initAuthSwitch() {
    var shell = document.querySelector('[data-auth-shell][data-auth-panel]');
    if (!shell) return;

    var URLS = { login: '/accounts/login/', signup: '/signup/' };
    var TITLES = { login: 'Sign in', signup: 'Create your school account' };

    function show(panel, push) {
      if (shell.getAttribute('data-auth-panel') === panel) return;

      // Which way each half travels. The visual panel is moving to the side
      // the form is leaving, so they cross rather than chase each other.
      var goingRight = panel === 'signup';
      shell.style.setProperty('--auth-cross-from', goingRight ? '100%' : '-100%');
      shell.style.setProperty('--auth-cross-to', goingRight ? '-100%' : '100%');

      shell.setAttribute('data-auth-panel', panel);
      togglePanels('data-auth-form-for', panel);
      togglePanels('data-auth-visual-for', panel);

      if (!reduceMotion.matches) {
        // Removed and re-added with a reflow between, so pressing the link
        // twice replays the movement rather than doing nothing the second time.
        shell.classList.remove('is-switching');
        void shell.offsetWidth;
        shell.classList.add('is-switching');
      }

      if (push) {
        try {
          window.history.pushState({ authPanel: panel }, '', URLS[panel]);
        } catch (e) { /* file:// and the like -- the panel still switches */ }
      }
      document.title = TITLES[panel] + ' · SCHOOLCORD';

      // The first field of whatever just arrived, so the keyboard follows the
      // eye instead of staying on a form that is no longer on screen.
      var first = document.querySelector(
        '[data-auth-form-for="' + panel + '"] input:not([type="hidden"])'
      );
      if (first) first.focus({ preventScroll: true });
    }

    /* `hidden` rather than a class: the inactive panel holds a whole second
       form, and it must be out of the accessibility tree and out of the tab
       order, not merely invisible. */
    function togglePanels(attribute, panel) {
      var nodes = document.querySelectorAll('[' + attribute + ']');
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].hidden = nodes[i].getAttribute(attribute) !== panel;
      }
    }

    document.addEventListener('click', function (event) {
      var link = event.target.closest && event.target.closest('[data-auth-switch]');
      if (!link) return;
      // Anything but a plain left click is the user asking for a real
      // navigation -- a new tab, a new window, a saved link.
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      show(link.getAttribute('data-auth-switch'), true);
    });

    window.addEventListener('popstate', function (event) {
      var panel = (event.state && event.state.authPanel) ||
        (window.location.pathname.indexOf('signup') > -1 ? 'signup' : 'login');
      show(panel, false);
    });
  }

  var SWAP_KEY = 'schoolcord-auth-swap';

  function initAuthSwap() {
    var shell = document.querySelector('[data-auth-shell]');

    // Arm the flag on the way out, from whichever page carries the link.
    document.addEventListener('click', function (event) {
      var link = event.target.closest && event.target.closest('[data-auth-swap]');
      if (!link) return;
      try {
        window.sessionStorage.setItem(SWAP_KEY, String(Date.now()));
      } catch (e) { /* storage disabled: the swap simply plays short */ }
    });

    if (!shell || reduceMotion.matches) return;

    var swapped = false;
    try {
      var stamp = parseInt(window.sessionStorage.getItem(SWAP_KEY) || '0', 10);
      // Only counts as a swap if it happened just now. A stale flag from
      // yesterday's session should not animate a cold visit.
      swapped = stamp > 0 && (Date.now() - stamp) < 5000;
      window.sessionStorage.removeItem(SWAP_KEY);
    } catch (e) { /* see above */ }

    // Where cross-document view transitions are supported, the browser is
    // already moving these two panels from where they were to where they now
    // are -- the stylesheet names them on both pages for exactly that. Adding
    // the CSS fallback on top would play the movement twice.
    if (typeof document.startViewTransition === 'function') return;

    if (swapped) shell.classList.add('is-swapping');

    var visual = shell.querySelector('[data-auth-visual]');
    var form = shell.querySelector('[data-auth-form]');
    if (visual) visual.classList.add('auth-swap-visual');
    if (form) form.classList.add('auth-swap-form');
  }

  /* ====================================================================== *
   * Repeatable rows
   *
   * One implementation behind every "add several without going back to the
   * list" screen: classes, subjects, staff invitations and fee line items.
   * They are all Django formsets, so they all need the same three things --
   * clone the empty form, renumber what follows it, and keep TOTAL_FORMS
   * honest.
   *
   * Markup contract (see templates/partials/_repeat_rows.html):
   *   [data-repeat][data-repeat-prefix="<formset prefix>"]
   *     [data-repeat-rows]        the container
   *       [data-repeat-row][data-new="true|false"]
   *     [data-repeat-template]    a <template> holding one blank row
   *     [data-repeat-add]         the button
   *     [data-repeat-count]       optional live count
   *
   * Without this script the server still renders `extra` blank rows and the
   * form still submits -- you simply get the rows you were given.
   * ====================================================================== */
  function initRepeat(root) {
    var prefix = root.getAttribute('data-repeat-prefix');
    var container = root.querySelector('[data-repeat-rows]');
    var template = root.querySelector('[data-repeat-template]');
    var addButtons = root.querySelectorAll('[data-repeat-add]');
    var totalForms = document.getElementById('id_' + prefix + '-TOTAL_FORMS');
    var initialField = document.getElementById('id_' + prefix + '-INITIAL_FORMS');
    if (!container || !template || !totalForms) return;

    var initialForms = initialField ? parseInt(initialField.value, 10) : 0;
    var maxForms = parseInt(
      (document.getElementById('id_' + prefix + '-MAX_NUM_FORMS') || {}).value || '1000', 10
    );

    function rows() {
      return Array.prototype.slice.call(
        container.querySelectorAll('[data-repeat-row]')
      );
    }

    function liveRows() {
      return rows().filter(function (row) {
        return !row.classList.contains('is-removed');
      });
    }

    function updateCount() {
      var count = liveRows().length;
      var outputs = root.querySelectorAll('[data-repeat-count]');
      for (var i = 0; i < outputs.length; i++) {
        outputs[i].textContent = String(count);
      }
      // The remove control is hidden on the last remaining row: a formset with
      // nothing in it is a dead end, and re-adding a row is an extra step
      // nobody asked for.
      var only = count <= 1;
      rows().forEach(function (row) {
        var button = row.querySelector('[data-repeat-remove]');
        if (button) button.hidden = only && !row.classList.contains('is-removed');
      });
      for (var b = 0; b < addButtons.length; b++) {
        addButtons[b].disabled = rows().length >= maxForms;
      }
      // Renumber the visible position markers, which are display-only and owe
      // nothing to the formset index.
      liveRows().forEach(function (row, index) {
        var marker = row.querySelector('[data-repeat-index]');
        if (marker) marker.textContent = String(index + 1);
      });
    }

    // Only rows after INITIAL_FORMS are renumbered: Django maps the first
    // INITIAL_FORMS entries to existing objects by position, so those must
    // keep the index they were rendered with.
    function reindexNewRows() {
      var newRows = container.querySelectorAll('[data-repeat-row][data-new="true"]');
      Array.prototype.forEach.call(newRows, function (row, offset) {
        var index = initialForms + offset;
        var fields = row.querySelectorAll('input, select, textarea, label');
        Array.prototype.forEach.call(fields, function (el) {
          ['name', 'id', 'for'].forEach(function (key) {
            var current = el.getAttribute(key);
            if (!current) return;
            el.setAttribute(
              key,
              current.replace(
                new RegExp(prefix + '-(\\d+|__prefix__)-'),
                prefix + '-' + index + '-'
              )
            );
          });
        });
      });
      totalForms.value = String(initialForms + newRows.length);
    }

    function addRow(focus) {
      if (rows().length >= maxForms) return null;
      var index = parseInt(totalForms.value, 10);
      var markup = template.innerHTML.replace(/__prefix__/g, String(index));
      var wrapper = document.createElement('div');
      wrapper.innerHTML = markup.trim();
      var row = wrapper.firstElementChild;
      if (!row) return null;
      container.appendChild(row);
      totalForms.value = String(index + 1);
      if (!reduceMotion.matches) {
        row.classList.add('is-fresh');
        row.addEventListener('animationend', function () {
          row.classList.remove('is-fresh');
        }, { once: true });
      }
      if (focus !== false) {
        var first = row.querySelector('input:not([type="hidden"]), select, textarea');
        if (first) first.focus();
      }
      updateCount();
      root.dispatchEvent(new CustomEvent('repeat:added', { detail: { row: row } }));
      return row;
    }

    function removeRow(row) {
      if (row.getAttribute('data-new') === 'true') {
        row.remove();
        reindexNewRows();
      } else {
        // An existing record: tick DELETE and leave the row on screen so a
        // mis-click can be undone before the form is submitted.
        row.classList.add('is-removed');
        var box = row.querySelector('input[name$="-DELETE"]');
        if (box) box.checked = true;
        var button = row.querySelector('[data-repeat-remove]');
        if (button) {
          button.setAttribute('aria-label', 'Restore');
          button.setAttribute('data-repeat-remove', 'undo');
        }
      }
      updateCount();
      root.dispatchEvent(new CustomEvent('repeat:changed'));
    }

    function restoreRow(row) {
      row.classList.remove('is-removed');
      var box = row.querySelector('input[name$="-DELETE"]');
      if (box) box.checked = false;
      var button = row.querySelector('[data-repeat-remove]');
      if (button) {
        button.setAttribute('aria-label', 'Remove');
        button.setAttribute('data-repeat-remove', '');
      }
      updateCount();
      root.dispatchEvent(new CustomEvent('repeat:changed'));
    }

    for (var a = 0; a < addButtons.length; a++) {
      addButtons[a].addEventListener('click', function () { addRow(true); });
      addButtons[a].hidden = false;
    }

    container.addEventListener('click', function (event) {
      var button = event.target.closest('[data-repeat-remove]');
      if (!button) return;
      var row = button.closest('[data-repeat-row]');
      if (!row) return;
      if (button.getAttribute('data-repeat-remove') === 'undo') restoreRow(row);
      else removeRow(row);
    });

    // Enter moves down the table instead of submitting it: to the next row, or
    // to a new one when you are already on the last. On a screen whose whole
    // purpose is entering nineteen of something, a stray Enter that saves and
    // navigates away is the exact round trip this is here to remove -- and
    // losing your place at row seven is worse than the small surprise of an
    // extra blank row (which is ignored on save anyway).
    //
    // Ctrl/Cmd+Enter still submits, and so does the Save button: there is a
    // deliberate way out, it is just not the one you hit by accident.
    container.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' || event.shiftKey) return;
      if (event.ctrlKey || event.metaKey) return;
      var field = event.target;
      // Text-ish inputs only. A select has its own meaning for Enter and a
      // textarea needs it for newlines.
      if (!field.matches || !field.matches('input:not([type="hidden"])')) return;
      var row = field.closest('[data-repeat-row]');
      if (!row) return;

      event.preventDefault();

      var all = liveRows();
      var next = all[all.indexOf(row) + 1];
      if (next) {
        var first = next.querySelector('input:not([type="hidden"]), select, textarea');
        if (first) first.focus();
        return;
      }
      addRow(true);
    });

    // Rows a failed submit marked for deletion come back dimmed rather than
    // silently gone.
    var deleted = container.querySelectorAll('input[name$="-DELETE"]');
    Array.prototype.forEach.call(deleted, function (box) {
      if (box.checked) {
        var row = box.closest('[data-repeat-row]');
        if (row && row.getAttribute('data-new') !== 'true') removeRow(row);
      }
    });

    // Published for the screens that need to fill rows from a preset.
    root.repeat = { addRow: addRow, rows: rows, liveRows: liveRows,
                    updateCount: updateCount };
    updateCount();
  }

  /* ====================================================================== *
   * Presets
   *
   * The class ladder, one press. A Nigerian school adding classes is almost
   * never inventing names: it is typing out Primary 1 to Primary 6, or JSS 1
   * to JSS 3, in order, every time. A preset button fills the rows it would
   * have taken to type -- and they are ordinary form rows afterwards, so
   * anything wrong is fixed in place rather than by starting over.
   *
   * The preset data lives in the markup as JSON, because what a ladder is
   * called is a product decision that belongs with the template, not here.
   * ====================================================================== */
  function initPresets() {
    var buttons = document.querySelectorAll('[data-preset]');
    if (!buttons.length) return;

    Array.prototype.forEach.call(buttons, function (button) {
      button.hidden = false;
      button.addEventListener('click', function () {
        var target = document.querySelector(
          button.getAttribute('data-preset-target') || '[data-repeat]'
        );
        if (!target || !target.repeat) return;

        var entries;
        try {
          entries = JSON.parse(button.getAttribute('data-preset'));
        } catch (e) {
          return;
        }
        if (!Array.isArray(entries)) return;

        // Fill blank rows before adding new ones, so pressing a preset on a
        // screen that opened with three empty rows does not leave three empty
        // rows stranded under the filled ones.
        var blanks = target.repeat.liveRows().filter(function (row) {
          var first = row.querySelector('input[type="text"]');
          return first && !first.value.trim();
        });

        entries.forEach(function (entry) {
          var row = blanks.shift() || target.repeat.addRow(false);
          if (!row) return;
          Object.keys(entry).forEach(function (field) {
            var input = row.querySelector('[name$="-' + field + '"]');
            if (!input) return;
            input.value = entry[field];
            input.dispatchEvent(new Event('change', { bubbles: true }));
          });
        });

        target.repeat.updateCount();
        var focusTarget = target.querySelector('[data-repeat-row] input[type="text"]');
        if (focusTarget) focusTarget.focus();
      });
    });
  }

  /* ====================================================================== *
   * Live totals
   *
   * Any container marked `data-total-of="<suffix>"` sums the numeric inputs
   * whose name ends in that suffix and writes the figure into its
   * `[data-total-out]`. Display only -- the server recomputes from what was
   * actually saved, so a wrong number here can never become a wrong number
   * in the database.
   * ====================================================================== */
  function initTotals() {
    var scopes = document.querySelectorAll('[data-total-of]');
    if (!scopes.length) return;

    Array.prototype.forEach.call(scopes, function (scope) {
      var suffix = scope.getAttribute('data-total-of');
      var out = scope.querySelector('[data-total-out]');
      if (!out) return;

      function recalc() {
        var sum = 0;
        var inputs = scope.querySelectorAll('input[name$="-' + suffix + '"]');
        Array.prototype.forEach.call(inputs, function (input) {
          var row = input.closest('[data-repeat-row]');
          if (row && row.classList.contains('is-removed')) return;
          var value = parseFloat(input.value);
          if (!isNaN(value)) sum += value;
        });
        // Match the server-side `naira` filter: kobo only when there are any.
        var fixed = Math.round(sum * 100) / 100;
        var opts = Number.isInteger(fixed)
          ? { minimumFractionDigits: 0, maximumFractionDigits: 0 }
          : { minimumFractionDigits: 2, maximumFractionDigits: 2 };
        out.textContent = '₦' + fixed.toLocaleString('en-NG', opts);
      }

      scope.addEventListener('input', recalc);
      scope.addEventListener('repeat:changed', recalc);
      scope.addEventListener('repeat:added', recalc);
      recalc();
    });
  }

  /* ====================================================================== *
   * Toasts
   *
   * One notification path for the whole app. Two ways in:
   *
   *   - the server renders Django messages into the region as real markup,
   *     already open, and this code takes them over on load;
   *   - anything on the page calls `window.toast(message, level)`.
   *
   * The movement is scripted rather than declared because it has four stages
   * with a hold in the middle -- rise, open, wait, close, drop -- and a CSS
   * animation cannot be paused on hover halfway through. Each stage is a
   * class change; the stylesheet owns every duration and curve.
   *
   * Nothing here is load-bearing: with the script blocked, the server-rendered
   * toasts simply stay on screen as readable notices.
   * ====================================================================== */
  var TOAST_LEVELS = {
    success: { label: 'Success', hold: 4000 },
    error: { label: 'Error', hold: 8000, dismissible: true },
    warning: { label: 'Warning', hold: 6500, dismissible: true },
    info: { label: 'Information', hold: 4500 }
  };

  //: Above this, the oldest is retired early. Four is about what fits above
  //: the fold on a phone before the stack starts covering the page.
  var TOAST_MAX = 4;

  //: The three stage durations, each in step with a token in assets/app.css.
  //: They have to agree: the stylesheet animates, this file decides when the
  //: next stage starts, and a mismatch shows up as a stage cut short.
  //:
  //:   TOAST_FADE_MS  <-> --toast-fade   the words appearing / going
  //:   TOAST_OPEN_MS  <-> --toast-open   the panel widening / closing
  //:   TOAST_RISE_MS  <-> --toast-rise   the circle arriving / leaving
  //:
  //: Each is a little longer than its token so a stage is never interrupted by
  //: the next one, and a toast is never removed from the DOM while it is still
  //: visibly moving.
  var TOAST_FADE_MS = 280;
  var TOAST_OPEN_MS = 580;
  var TOAST_RISE_MS = 480;

  //: A beat between the circle landing and the panel opening. Without it the two
  //: overlap into one indistinct movement; with it the toast reads as a thing
  //: that arrives and then speaks.
  var TOAST_BEAT_MS = 120;

  var ICON_PATHS = {
    success: ['M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z'],
    error: ['M9.75 9.75l4.5 4.5m0-4.5-4.5 4.5M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z'],
    warning: ['M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 ' +
              '2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 ' +
              '0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z'],
    info: ['M11.25 11.25l.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 ' +
           '1.063.852l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z']
  };

  var toastRegion = null;

  function ensureRegion() {
    if (toastRegion && document.body.contains(toastRegion)) return toastRegion;
    toastRegion = document.querySelector('[data-toast-region]');
    if (!toastRegion) {
      toastRegion = document.createElement('div');
      toastRegion.className = 'toast-region';
      toastRegion.setAttribute('data-toast-region', '');
      // Polite, not assertive: a toast reports what just happened, it does
      // not interrupt. `aria-atomic` so each toast is read whole rather than
      // as the words that changed.
      toastRegion.setAttribute('role', 'status');
      toastRegion.setAttribute('aria-live', 'polite');
      toastRegion.setAttribute('aria-atomic', 'false');
      document.body.appendChild(toastRegion);
    }
    return toastRegion;
  }

  function levelOf(name) {
    return TOAST_LEVELS[name] ? name : 'info';
  }

  function buildToast(message, level) {
    var meta = TOAST_LEVELS[level];
    var el = document.createElement('div');
    el.className = 'toast toast-' + level;
    el.setAttribute('data-toast', '');
    el.setAttribute('data-toast-level', level);

    var icon = document.createElement('span');
    icon.className = 'toast-icon';
    icon.innerHTML = iconMarkup(level);
    el.appendChild(icon);

    var panel = document.createElement('div');
    panel.className = 'toast-panel';
    var inner = document.createElement('div');
    var body = document.createElement('p');
    body.className = 'toast-message';
    // The category as a word, for anyone who cannot see the colour or the
    // glyph. This is the "never colour alone" rule, in the one place the rule
    // is easiest to forget.
    var prefix = document.createElement('span');
    prefix.className = 'sr-only';
    prefix.textContent = meta.label + ': ';
    body.appendChild(prefix);
    body.appendChild(document.createTextNode(message));
    inner.appendChild(body);
    // Inside the panel, not beside it: a control outside would not collapse
    // with the message, and the closed toast would be a lozenge rather than a
    // circle. Mirrors partials/_toast.html exactly.
    if (meta.dismissible) inner.appendChild(buildClose());
    panel.appendChild(inner);
    el.appendChild(panel);
    return el;
  }

  function iconMarkup(level) {
    var paths = (ICON_PATHS[level] || ICON_PATHS.info)
      .map(function (d) {
        return '<path stroke-linecap="round" stroke-linejoin="round" d="' + d + '" />';
      })
      .join('');
    return '<svg class="size-6" fill="none" viewBox="0 0 24 24" stroke-width="1.8" ' +
           'stroke="currentColor" aria-hidden="true">' + paths + '</svg>';
  }

  function buildClose() {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'toast-close';
    button.setAttribute('data-toast-close', '');
    button.setAttribute('aria-label', 'Dismiss');
    button.innerHTML =
      '<svg class="size-4" fill="none" viewBox="0 0 24 24" stroke-width="2" ' +
      'stroke="currentColor" aria-hidden="true">' +
      '<path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" />' +
      '</svg>';
    return button;
  }

  /* Drive one toast through its six beats.
   *
   *   in    rise the circle  ->  open the panel  ->  fade the words in
   *   out   fade the words out  ->  close the panel  ->  drop the circle
   *
   * The order on the way out is the whole point, and it is what the reported
   * bug was about. Closing the panel while the message is still opaque makes the
   * text reflow into an ever-narrower column -- visibly crushed -- because the
   * track it sits in is a grid column shrinking to zero. Fading the words first
   * means the reflow still happens and nobody sees it.
   *
   * Scripted rather than declared because there is a hold in the middle that has
   * to be interruptible: a CSS animation cannot be paused halfway because a
   * cursor arrived. The stylesheet still owns every duration and curve; this
   * decides only when each stage begins.
   *
   * Two timer lists, not one. Hovering clears the *hold* so the toast waits for
   * you -- it must not also cancel a stage of the entrance that has not run yet,
   * which is what a single list did: hover a toast mid-arrival and it froze
   * half-open.
   */
  function runToast(el, level) {
    var meta = TOAST_LEVELS[level] || TOAST_LEVELS.info;
    var holdTimers = [];
    var stageTimers = [];
    var finished = false;
    var still = reduceMotion.matches;

    function stage(fn, ms) {
      stageTimers.push(window.setTimeout(fn, still ? 0 : ms));
    }

    function later(fn, ms) {
      holdTimers.push(window.setTimeout(fn, ms));
    }

    function clearHold() {
      holdTimers.forEach(window.clearTimeout);
      holdTimers = [];
    }

    function clearStages() {
      stageTimers.forEach(window.clearTimeout);
      stageTimers = [];
    }

    function dismiss() {
      if (finished) return;
      finished = true;
      clearHold();
      clearStages();
      // The entrance played backwards, which is what makes arriving and leaving
      // read as one object rather than two effects.
      el.classList.remove('is-lit');
      stage(function () {
        el.classList.remove('is-open');
        stage(function () {
          el.classList.remove('is-in');
          stage(function () {
            if (el.parentNode) el.parentNode.removeChild(el);
          }, TOAST_RISE_MS);
        }, TOAST_OPEN_MS);
      }, TOAST_FADE_MS);
    }

    function hold() {
      clearHold();
      later(dismiss, meta.hold);
    }

    el.dismissToast = dismiss;

    // Reading takes longer than the timer allows sometimes. Hovering or
    // focusing anything inside restarts the clock rather than freezing it,
    // so a toast never sits on screen forever because a cursor was parked.
    el.addEventListener('mouseenter', clearHold);
    el.addEventListener('mouseleave', hold);
    el.addEventListener('focusin', clearHold);
    el.addEventListener('focusout', hold);
    el.addEventListener('click', function (event) {
      if (event.target.closest('[data-toast-close]')) dismiss();
    });

    // Two frames before the first move: one to commit the closed start state the
    // stylesheet declares, one to leave it. Without the gap the browser collapses
    // both into a single style recalculation and nothing transitions.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        el.classList.add('is-in');
        stage(function () {
          el.classList.add('is-open');
          stage(function () {
            el.classList.add('is-lit');
            // The clock starts when there is something to read, not when the
            // toast began arriving -- otherwise a good second of the hold is
            // spent on an empty circle.
            hold();
          }, TOAST_OPEN_MS);
        }, TOAST_RISE_MS - TOAST_BEAT_MS);
      });
    });
  }

  function trimRegion(region) {
    var toasts = region.querySelectorAll('[data-toast]');
    for (var i = 0; i < toasts.length - TOAST_MAX; i++) {
      if (toasts[i].dismissToast) toasts[i].dismissToast();
    }
  }

  /** Public API. `toast("Saved", "success")`. */
  function showToast(message, level) {
    if (!message) return null;
    var resolved = levelOf(level);
    var region = ensureRegion();
    var el = buildToast(String(message), resolved);
    region.appendChild(el);
    trimRegion(region);
    runToast(el, resolved);
    return el;
  }

  function initToasts() {
    var region = document.querySelector('[data-toast-region]');
    if (region) {
      toastRegion = region;
      // Server-rendered toasts ship open so they are readable without this
      // script. Close them, then let each play its entrance -- staggered, so
      // three messages from one request arrive as a sequence rather than a
      // wall.
      //
      // Each is detached and re-appended rather than merely re-classed. A live
      // region announces what is *added* to it, and content already present
      // when the region is parsed is not an addition -- so without the round
      // trip a screen reader would stay silent on exactly the messages that
      // came from the server.
      var existing = Array.prototype.slice.call(region.querySelectorAll('[data-toast]'));
      existing.forEach(function (el, index) {
        var level = levelOf(el.getAttribute('data-toast-level'));
        el.classList.remove('is-in', 'is-open', 'is-lit');
        el.parentNode.removeChild(el);
        window.setTimeout(function () {
          region.appendChild(el);
          runToast(el, level);
        }, index * 220);
      });
    }

    // Declarative toasts: anything can carry `data-toast-on-click`, which is
    // how a control that is visibly present but not permitted explains itself
    // without a dialog. See partials/_nav.html.
    document.addEventListener('click', function (event) {
      var trigger = event.target.closest && event.target.closest('[data-toast-on-click]');
      if (!trigger) return;
      showToast(
        trigger.getAttribute('data-toast-on-click'),
        trigger.getAttribute('data-toast-on-click-level') || 'info'
      );
    });

    announceFormErrors(region);
  }

  /* A rejected form, announced once.
   *
   * Django re-renders the page with the errors printed beside the fields it
   * rejected, which is the right place for them -- and easy to miss on a long
   * form when the thing you were looking at was the Save button. `.field-error`
   * only exists on a form that has been submitted and refused, so its presence
   * is the signal; no view has to remember to flash anything.
   *
   * Skipped when the server already sent a toast, because a page that has just
   * said "Sign-in failed" does not also need "2 fields need attention".
   */
  function announceFormErrors(region) {
    if (region && region.querySelector('[data-toast]')) return;

    var errors = document.querySelectorAll('.field-error');
    if (!errors.length) return;

    // Counted per field, not per message: two complaints about the same input
    // are still one place to go and look. Deduped by the element the error
    // sits under, which is the field's own wrapper.
    var owners = [];
    Array.prototype.forEach.call(errors, function (node) {
      var owner = node.parentElement;
      if (owner && owners.indexOf(owner) === -1) owners.push(owner);
    });
    var total = owners.length || errors.length;

    showToast(
      total === 1
        ? 'One field needs attention. Nothing was saved.'
        : total + ' fields need attention. Nothing was saved.',
      'warning'
    );
  }

  // Exposed before init so anything inline on a page can call it.
  window.ui = window.ui || {};
  window.ui.loading = setLoading;

  window.toast = showToast;
  window.toast.success = function (m) { return showToast(m, 'success'); };
  window.toast.error = function (m) { return showToast(m, 'error'); };
  window.toast.warning = function (m) { return showToast(m, 'warning'); };
  window.toast.info = function (m) { return showToast(m, 'info'); };

  /* ====================================================================== *
   * Tables as cards, on a phone
   *
   * Every table in the signed-in shell gets its column headings copied onto its
   * cells, which the stylesheet shows as labels below `sm`. Here rather than in
   * each template so a new table is covered without anyone remembering, and so
   * that with scripting off it stays an ordinary table that scrolls. A table opts
   * out with `data-no-cards`.
   * ====================================================================== */
  function initServiceWorker() {
    if (!document.querySelector('meta[name="schoolcord-pwa"]')) return;
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.register('/sw.js').catch(function () {
      /* The site works exactly the same without it. */
    });
  }

  /* ====================================================================== *
   * Offering the home screen
   *
   * Android hands us a prompt to show when we choose. iPhones have none, so they
   * get the steps. Once dismissed it stays away, and it never shows inside the
   * installed app.
   * ====================================================================== */
  var INSTALL_DISMISSED_KEY = 'schoolcord-install-dismissed';

  function initInstallPrompt() {
    var box = document.querySelector('[data-install-prompt]');
    if (!box) return;
    var installed = window.navigator.standalone === true ||
      (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
    if (installed) return;
    try {
      if (window.localStorage.getItem(INSTALL_DISMISSED_KEY)) return;
    } catch (e) { /* Blocked storage only means it may be offered again. */ }

    var accept = box.querySelector('[data-install-accept]');
    var offer = null;
    function hide() { box.hidden = true; }
    function dismiss() {
      hide();
      try { window.localStorage.setItem(INSTALL_DISMISSED_KEY, 'yes'); } catch (e) { /* as above */ }
    }

    box.querySelector('[data-install-dismiss]').addEventListener('click', dismiss);
    accept.addEventListener('click', function () {
      if (!offer) return;
      offer.prompt();
      offer = null;
      hide();
    });

    window.addEventListener('beforeinstallprompt', function (event) {
      event.preventDefault();
      offer = event;
      box.hidden = false;
    });
    window.addEventListener('appinstalled', dismiss);

    var isIos = /iphone|ipad|ipod/i.test(window.navigator.userAgent);
    if (isIos) {
      box.querySelector('[data-install-text]').hidden = true;
      box.querySelector('[data-install-ios]').hidden = false;
      accept.hidden = true;
      box.hidden = false;
    }
  }

  function initCardTables() {
    var main = document.getElementById('main');
    if (!main) return;
    var tables = main.querySelectorAll('table:not([data-no-cards]):not([role="grid"])');
    for (var i = 0; i < tables.length; i++) cardTable(tables[i]);
  }

  function cardTable(table) {
    var headings = [];
    var head = table.tHead && table.tHead.rows[0];
    if (head) {
      for (var h = 0; h < head.cells.length; h++) {
        headings.push(head.cells[h].textContent.replace(/\s+/g, ' ').trim());
      }
    }
    var rows = table.querySelectorAll('tbody tr, tfoot tr');
    for (var r = 0; r < rows.length; r++) {
      var column = 0;
      for (var c = 0; c < rows[r].cells.length; c++) {
        var cell = rows[r].cells[c];
        if (cell.colSpan > 1) {
          cell.classList.add('table-card-span');
        } else if (headings[column]) {
          cell.setAttribute('data-label', headings[column]);
        }
        column += cell.colSpan;
      }
    }
    table.classList.add('table-cards');
  }

  /* ====================================================================== *
   * Filters folded away on a phone
   *
   * A form marked `data-filter-sheet` starts closed on a phone behind a button,
   * so results are the first thing on screen, and open when a search is already
   * applied, so what is narrowing the list stays visible. The label is the
   * attribute's value.
   * ====================================================================== */
  function initFilterSheets() {
    var forms = document.querySelectorAll('form[data-filter-sheet]');
    var applied = /[?&](?!page=)[^=&]+=[^&]/.test(window.location.search);
    for (var i = 0; i < forms.length; i++) foldFilterSheet(forms[i], i, applied);
  }

  function foldFilterSheet(form, index, applied) {
    if (!form.id) form.id = 'filter-sheet-' + index;
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-secondary filter-sheet-toggle text-sm';
    button.setAttribute('aria-controls', form.id);
    button.setAttribute('data-no-loading', '');
    button.textContent = form.getAttribute('data-filter-sheet');

    function set(open) {
      form.classList.toggle('filter-sheet-closed', !open);
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    button.addEventListener('click', function () {
      set(form.classList.contains('filter-sheet-closed'));
    });
    set(applied);
    form.parentNode.insertBefore(button, form);
  }

  /* ====================================================================== *
   * The notification bell
   * ----------------------------------------------------------------------
   * The bell is a link to the notifications page. With this script it opens
   * a panel instead, whose list is fetched from notifications:panel the first
   * time it opens (so no page pays for the list until someone asks). Escape,
   * a click outside or pressing the bell again closes it.
   * ====================================================================== */
  function initBell() {
    var bell = document.querySelector('[data-bell]');
    if (!bell || !window.fetch) return;
    var button = bell.querySelector('[data-bell-button]');
    var panel = bell.querySelector('.bell-panel');
    var body = bell.querySelector('[data-bell-body]');
    var loaded = false;

    function close() {
      panel.hidden = true;
      button.setAttribute('aria-expanded', 'false');
    }
    function open() {
      panel.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      if (loaded) return;
      loaded = true;
      fetch(bell.getAttribute('data-panel-url'), {
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
        .then(function (html) { body.innerHTML = html; })
        .catch(function () {
          loaded = false;
          body.innerHTML = '<p class="px-4 py-6 text-center text-sm text-ink-500">' +
            'Could not load. Open the notifications page instead.</p>';
        });
    }
    button.addEventListener('click', function (e) {
      e.preventDefault();
      if (panel.hidden) open(); else close();
    });
    document.addEventListener('click', function (e) {
      if (!panel.hidden && !bell.contains(e.target)) close();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !panel.hidden) { close(); button.focus(); }
    });
  }

  /* ====================================================================== *
   * Boot
   * ====================================================================== */
  function init() {
    // The performance tier is decided first: it sets the attribute the
    // stylesheet reads to switch glass off, and a toast is a glass surface
    // that starts animating immediately after.
    initPerfTier();
    initToasts();
    initTheme();
    initSidebar();
    initReveals();
    initLazyImages();
    initLandingNav();
    initAuthSwap();
    initAuthSwitch();

    var repeats = document.querySelectorAll('[data-repeat]');
    for (var i = 0; i < repeats.length; i++) initRepeat(repeats[i]);

    initLogoInput();
    initButtonLoading();
    initPresets();
    initTotals();
    initCardTables();
    initServiceWorker();
    initInstallPrompt();
    initFilterSheets();
    initBell();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
