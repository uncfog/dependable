const DEFAULT_COLORS = [
  "#FF1AE6",
  "#FF2E5E",
  "#FF8A1A",
  "#FFC81F",
  "#E2E6EC",
  "#8DB6E6",
  "#2F74DC",
  "#1459E3",
  "#1C2566"
], DEFAULT_MAX_STRETCH = 280, DEFAULT_COLUMNS = 9, DEFAULT_BLUR = 16, DEFAULT_GLOW = 0, DEFAULT_STIFFNESS = 380, DEFAULT_DAMPING = 32, STRETCHY_FOOTER_PLAY = "stretchy-footer:play", RUBBER = 0.9, RELEASE_MS = 110, MOMENTUM_GAP_MS = 180, COAST_EVENTS = 6, MASS = 0.35, REST = 0.15, EDGE_COLUMN = 0.55, TOP_FADE = 0.12, FADE_IN = 0.08, STEP = 0.008333333333333333;
async function scrollToEnd(root) {
  const el = root ?? document.documentElement, max = () => Math.max(0, el.scrollHeight - el.clientHeight), top = () => root ? root.scrollTop : window.scrollY, scroll = (behavior) => (root ?? window).scrollTo({ top: max(), behavior });
  scroll("smooth");
  const started = performance.now();
  for (; performance.now() - started < 2e3 && top() < max() - 2; )
    await new Promise((r) => window.setTimeout(r, 32));
  scroll("instant");
}
async function playStretchyFooterDemo(detail = {}) {
  typeof window > "u" || (await scrollToEnd(detail.scrollRoot), window.dispatchEvent(
    new CustomEvent(STRETCHY_FOOTER_PLAY, { detail })
  ), await new Promise(
    (r) => window.setTimeout(r, (detail.holdMs ?? 700) + 900)
  ));
}
function columnScale(index, count, flip = !1) {
  if (count <= 1) return 1;
  const t = Math.abs(index / (count - 1) * 2 - 1);
  return 1 - (1 - 0.55) * (flip ? 1 - t : t);
}
function rubberBand(distance, max) {
  return max <= 0 || distance <= 0 ? 0 : max * (1 - 1 / (distance * 0.9 / max + 1));
}
function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}
function numberOr(value, fallback, min, max) {
  return typeof value != "number" || !Number.isFinite(value) ? fallback : clamp(value, min, max);
}
function wheelPixels(event) {
  return event.deltaMode === 1 ? event.deltaY * 16 : event.deltaMode === 2 ? event.deltaY * window.innerHeight : event.deltaY;
}
function nestedCanScroll(target, root, dy) {
  let el = target instanceof Element ? target : null;
  for (; el && el !== root && el !== document.documentElement; ) {
    if (el.scrollHeight > el.clientHeight + 1) {
      const overflow = getComputedStyle(el).overflowY;
      if ((overflow === "auto" || overflow === "scroll") && (dy > 0 ? el.scrollHeight - el.clientHeight - el.scrollTop : el.scrollTop) > 1)
        return !0;
    }
    el = el.parentElement;
  }
  return !1;
}
const STRIP = 256;
function makeStrip(colors) {
  const strip = document.createElement("canvas");
  strip.width = 1, strip.height = STRIP;
  const g = strip.getContext("2d");
  if (!g) return strip;
  const bands = g.createLinearGradient(0, 0, 0, STRIP);
  colors.forEach((color, i) => {
    const at = colors.length > 1 ? i / (colors.length - 1) : 1;
    bands.addColorStop(0.12 * 0.8 + (1 - 0.12 * 0.8) * at, color);
  }), g.fillStyle = bands, g.fillRect(0, 0, 1, STRIP);
  const fade = g.createLinearGradient(0, 0, 0, STRIP * 0.12);
  return fade.addColorStop(0, "rgba(0,0,0,0)"), fade.addColorStop(1, "rgba(0,0,0,1)"), g.globalCompositeOperation = "destination-in", g.fillStyle = fade, g.fillRect(0, 0, 1, STRIP), strip;
}
function makeBloom() {
  const bloom = document.createElement("canvas");
  bloom.width = 128, bloom.height = 64;
  const g = bloom.getContext("2d");
  if (!g) return bloom;
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  return grad.addColorStop(0, "rgba(255,255,255,1)"), grad.addColorStop(1, "rgba(255,255,255,0)"), g.fillStyle = grad, g.fillRect(0, 0, 128, 64), bloom;
}
function stepSpring(s, k, c, dt) {
  const accel = (-k * (s.x - s.target) - c * s.v) / 0.35;
  s.v += accel * dt, s.x += s.v * dt;
}
const atRest = (s) => Math.abs(s.x - s.target) < 0.15 && Math.abs(s.v) < 0.15;
function createStretchyFooter(mount, initial = {}) {
  const { canvas, scroller } = mount, ctx = canvas.getContext("2d");
  if (!ctx) return null;
  let options = { ...initial };
  const maxOf = () => numberOr(options.maxStretch, 280, 0, 4e3), blurOf = () => numberOr(options.blur, 16, 0, 64), glowOf = () => numberOr(options.glow, 0, 0, 1), stiffnessOf = () => numberOr(options.stiffness, 380, 10, 4e3), dampingOf = () => numberOr(options.damping, 32, 0, 400), countOf = () => Math.round(numberOr(options.columns, 9, 3, 96));
  let distance = 0, forced = null;
  const lift = { x: 0, v: 0, target: 0 };
  let columns = [], strip = document.createElement("canvas");
  const bloom = makeBloom(), rebuildColumns = () => {
    const count = countOf(), target2 = lift.target;
    columns = Array.from({ length: count }, (_, i) => {
      const share = columnScale(i, count, options.flip);
      return { x: lift.x * share, v: 0, target: target2 * share, share };
    });
  }, rebuildStrip = () => {
    strip = makeStrip(options.colors?.length ? options.colors : DEFAULT_COLORS);
  }, size = { w: 0, pad: 0, scale: 1 }, layout = () => {
    const parent = canvas.parentElement, blur = blurOf(), max = maxOf(), pad = Math.ceil(blur * 2), scale = blur >= 6 ? 0.5 : Math.min(window.devicePixelRatio || 1, 2);
    size.w = (parent?.clientWidth ?? 0) + pad * 2, size.pad = pad, size.scale = scale, canvas.width = Math.max(1, Math.ceil(size.w * scale)), canvas.height = Math.max(1, Math.ceil((max + pad) * scale)), Object.assign(canvas.style, {
      position: "absolute",
      left: `${-pad}px`,
      bottom: `${-pad}px`,
      width: `${size.w}px`,
      height: `${max + pad}px`,
      filter: blur > 0 ? `blur(${blur}px)` : "",
      pointerEvents: "none"
    }), draw();
  };
  let lifted = null, liftedTransform = "";
  const contentEl = () => {
    const content = mount.content;
    return content ? typeof content != "string" ? content : lifted?.isConnected && lifted.matches(content) ? lifted : (scroller instanceof HTMLElement ? scroller : document).querySelector(content) ?? document.querySelector(content) : null;
  }, liftContent = (y) => {
    const el = contentEl();
    el !== lifted && (lifted && (lifted.style.transform = liftedTransform), lifted = el, liftedTransform = el?.style.transform ?? ""), lifted && (y > 0.2 ? (lifted.style.transform = `translate3d(0, ${-y}px, 0)`, lifted.style.willChange = "transform") : (lifted.style.transform = liftedTransform, lifted.style.willChange = ""));
  };
  function draw() {
    const stretch = Math.max(0, lift.x);
    liftContent(stretch);
    const max = maxOf(), visible = stretch > 0.2 || columns.some((column) => column.x > 0.2);
    if (canvas.style.visibility = visible ? "visible" : "hidden", !visible) return;
    const { w, pad, scale } = size, floor = max * scale, bottom = canvas.height;
    ctx.setTransform(1, 0, 0, 1, 0, 0), ctx.clearRect(0, 0, canvas.width, canvas.height), ctx.globalAlpha = clamp(stretch / Math.max(1, max * 0.08), 0, 1);
    const rotated = options.rotate === 180;
    rotated && ctx.setTransform(-1, 0, 0, -1, canvas.width, 2 * floor - stretch * scale);
    const count = columns.length, inner = (w - pad * 2) * scale, edge = (i) => i <= 0 ? 0 : i >= count ? canvas.width : Math.round(pad * scale + inner * i / count);
    for (let i = 0; i < count; i++) {
      const h = Math.max(0, columns[i].x) * scale;
      if (h < 0.5) continue;
      const x0 = edge(i), x1 = edge(i + 1);
      ctx.drawImage(strip, x0, floor - h, x1 - x0, h), !rotated && ctx.drawImage(
        strip,
        0,
        STRIP - 1,
        1,
        1,
        x0,
        floor,
        x1 - x0,
        bottom - floor
      );
    }
    const glow = glowOf();
    if (glow > 0) {
      const rx = inner * 0.49, ry = stretch * 0.35 * scale;
      ctx.globalAlpha *= glow, ctx.drawImage(bloom, w * scale * 0.5 - rx, floor - ry, rx * 2, ry);
    }
    ctx.globalAlpha = 1, ctx.setTransform(1, 0, 0, 1, 0, 0);
  }
  let raf = 0, last = 0;
  const retarget = () => {
    const max = maxOf(), target2 = forced ?? rubberBand(distance, max);
    lift.target = target2;
    for (const column of columns) column.target = target2 * column.share;
    raf || (last = performance.now(), raf = requestAnimationFrame(frame));
  };
  function frame(now) {
    raf = 0;
    const dt = Math.min(0.05, Math.max(0, (now - last) / 1e3));
    last = now;
    const k = stiffnessOf(), c = dampingOf(), steps = Math.max(1, Math.ceil(dt / STEP));
    for (let n = 0; n < steps; n++) {
      stepSpring(lift, k, c, dt / steps);
      for (const column of columns) {
        const ck = k * (0.6 + 0.4 * column.share);
        stepSpring(column, ck, c * 0.5 * Math.sqrt(ck / k), dt / steps);
      }
    }
    const settled = atRest(lift) && columns.every(atRest);
    if (settled) {
      lift.x = lift.target, lift.v = 0;
      for (const column of columns)
        column.x = column.target, column.v = 0;
    }
    draw(), settled || (raf = requestAnimationFrame(frame));
  }
  const mqReduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let reduce = mqReduce.matches, releaseTimer = 0, holdTimer = 0, lastWheel = 0, lastDelta = 0, coasting = 0, cooling = !1, lastTouchY = 0;
  const root = scroller instanceof HTMLElement ? scroller : document.documentElement, atBottom = () => root.scrollTop + root.clientHeight >= root.scrollHeight - 2, release = () => {
    window.clearTimeout(releaseTimer), releaseTimer = 0, !(distance <= 0) && (distance = 0, cooling = !0, retarget());
  }, stopPlay = () => {
    forced !== null && (window.clearTimeout(holdTimer), forced = null, retarget());
  }, onWheel = (event) => {
    if (reduce || event.ctrlKey) return;
    const dy = wheelPixels(event);
    if (dy === 0 || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
    stopPlay();
    const now = performance.now(), gap = now - lastWheel;
    if (lastWheel = now, distance > 0) {
      if (event.preventDefault(), coasting = dy > 0 && Math.abs(dy) < Math.abs(lastDelta) ? coasting + 1 : 0, lastDelta = dy, coasting >= 6) {
        release();
        return;
      }
      distance = Math.max(0, distance + dy), retarget(), window.clearTimeout(releaseTimer), distance > 0 && (releaseTimer = window.setTimeout(release, 110));
      return;
    }
    if (cooling) {
      if (gap < 180) return;
      cooling = !1;
    }
    dy > 0 && atBottom() && !nestedCanScroll(event.target, root, dy) && (event.preventDefault(), distance = dy, lastDelta = dy, coasting = 0, retarget(), releaseTimer = window.setTimeout(release, 110));
  }, onTouchStart = (event) => {
    const touch = event.touches[0];
    touch && (stopPlay(), cooling = !1, lastTouchY = touch.clientY);
  }, onTouchMove = (event) => {
    const touch = event.touches[0];
    if (!touch || reduce) return;
    const dy = lastTouchY - touch.clientY;
    lastTouchY = touch.clientY, (distance > 0 || dy > 0 && atBottom() && !nestedCanScroll(event.target, root, dy)) && (event.cancelable && event.preventDefault(), distance = Math.max(0, distance + dy), retarget());
  }, onTouchEnd = () => release(), onPlayEvent = (event) => {
    const detail = event.detail ?? {};
    (detail.target ?? null) === (options.demoId ?? null) && instance.play(detail);
  }, onReduce = () => {
    reduce = mqReduce.matches, reduce && release();
  }, target = scroller, prevOverscroll = root.style.overscrollBehaviorY;
  root.style.overscrollBehaviorY = "none", target.addEventListener("wheel", onWheel, { passive: !1 }), target.addEventListener("touchstart", onTouchStart, {
    passive: !0
  }), target.addEventListener("touchmove", onTouchMove, {
    passive: !1
  }), target.addEventListener("touchend", onTouchEnd), target.addEventListener("touchcancel", onTouchEnd), window.addEventListener(STRETCHY_FOOTER_PLAY, onPlayEvent), mqReduce.addEventListener("change", onReduce);
  const ro = new ResizeObserver(layout);
  canvas.parentElement && ro.observe(canvas.parentElement), rebuildStrip(), rebuildColumns(), layout();
  const instance = {
    setOptions(next) {
      const prevColors = options.colors, prevMax = maxOf(), prevBlur = blurOf(), prevFlip = !!options.flip;
      options = { ...options, ...next }, options.colors !== prevColors && rebuildStrip(), (countOf() !== columns.length || !!options.flip !== prevFlip) && rebuildColumns(), (maxOf() !== prevMax || blurOf() !== prevBlur) && layout(), forced !== null && (forced = Math.min(forced, maxOf())), retarget();
    },
    play({ amount = 0.82, holdMs = 700 } = {}) {
      window.clearTimeout(releaseTimer), window.clearTimeout(holdTimer), distance = 0, forced = maxOf() * clamp(amount, 0, 1), retarget(), holdTimer = window.setTimeout(stopPlay, holdMs);
    },
    destroy() {
      cancelAnimationFrame(raf), raf = 0, window.clearTimeout(releaseTimer), window.clearTimeout(holdTimer), root.style.overscrollBehaviorY = prevOverscroll, target.removeEventListener("wheel", onWheel), target.removeEventListener("touchstart", onTouchStart), target.removeEventListener("touchmove", onTouchMove), target.removeEventListener("touchend", onTouchEnd), target.removeEventListener("touchcancel", onTouchEnd), window.removeEventListener(STRETCHY_FOOTER_PLAY, onPlayEvent), mqReduce.removeEventListener("change", onReduce), ro.disconnect(), lifted && (lifted.style.transform = liftedTransform, lifted.style.willChange = "");
    }
  };
  return instance;
}
export {
  DEFAULT_BLUR,
  DEFAULT_COLORS,
  DEFAULT_COLUMNS,
  DEFAULT_DAMPING,
  DEFAULT_GLOW,
  DEFAULT_MAX_STRETCH,
  DEFAULT_STIFFNESS,
  STRETCHY_FOOTER_PLAY,
  columnScale,
  createStretchyFooter,
  playStretchyFooterDemo,
  rubberBand,
  scrollToEnd
};
