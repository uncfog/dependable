const DARK_COLOR = "#FFB25B", LIGHT_COLOR = "#C2560C", SHAPES = ["cube", "steps", "pillars", "coil"], DEFAULT_INTERVAL = 4, DEFAULT_SIZE = 0.6, DEFAULT_SPACING = 8, DEFAULT_GLOW = 0.6, DEFAULT_SPEED = 1, MORPH_SECONDS = 1.6, MASK = 512, EXTENT = 1.6, COS30 = Math.cos(Math.PI / 6), VERT = `
attribute vec2 a_position;
void main() {
  gl_Position = vec4(a_position, 0.0, 1.0);
}
`, FRAG = `
precision highp float;

uniform vec2 u_resolution;
uniform vec2 u_center;
uniform float u_radius;
uniform float u_spacing;
uniform float u_pixel;
// Red is the figure, green its blurred halo, blue the floor grid.
uniform sampler2D u_maskA;
uniform sampler2D u_maskB;
uniform float u_progress;
uniform float u_time;
uniform float u_sweep;
uniform float u_glow;
uniform float u_floor;
uniform vec2 u_mouse;
uniform float u_pointer;
uniform float u_dark;
uniform vec3 u_color;
uniform vec3 u_deep;
uniform vec3 u_hot;

const float SQ3 = 1.7320508;
const float EXTENT = ${1.6.toFixed(2)};

// Dave Hoskins' hash: no sin(), so it stays stable on mobile GPUs.
float hash(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

vec2 toUv(vec2 q) {
  return vec2(q.x, -q.y) / (2.0 * EXTENT) + 0.5;
}

vec2 sampleShape(vec2 q, float k) {
  vec2 uv = toUv(q);
  return mix(texture2D(u_maskA, uv).rg, texture2D(u_maskB, uv).rg, k);
}

// Whether a whole tile fits on the figure: its four tips, pulled in a touch,
// must all land on it. A cell whose centre is inside but whose tips are not
// would poke past the silhouette as a ragged half-lit column.
float fits(sampler2D mask, vec2 q, vec2 tip) {
  float c = 1.0;
  c = min(c, texture2D(mask, toUv(q + vec2(tip.x, 0.0))).r);
  c = min(c, texture2D(mask, toUv(q - vec2(tip.x, 0.0))).r);
  c = min(c, texture2D(mask, toUv(q + vec2(0.0, tip.y))).r);
  c = min(c, texture2D(mask, toUv(q - vec2(0.0, tip.y))).r);
  return step(0.08, c);
}

void main() {
  vec2 frag = gl_FragCoord.xy;

  // Hex lattice in vertical columns, so its rows run at 30 degrees, the
  // same angles as an isometric edge. Each pixel finds its nearest cell.
  vec2 p = frag - 0.5 * u_resolution;
  vec2 period = vec2(SQ3, 1.0) * u_spacing;
  vec2 a = mod(p, period) - 0.5 * period;
  vec2 b = mod(p - 0.5 * period, period) - 0.5 * period;
  vec2 off = dot(a, a) < dot(b, b) ? a : b;
  vec2 cell = p - off;
  vec2 id = floor(cell / (0.5 * period) + 0.5);

  vec2 cellPx = cell + 0.5 * u_resolution;
  vec2 q = (cellPx - u_center) / u_radius;

  // The next figure builds from the floor up: each cell turns over when the
  // build front passes its height, give or take a little.
  float rise = clamp((q.y + 1.3) / 2.6, 0.0, 1.0);
  float delay = 0.62 * rise + 0.08 * hash(id);
  float k = smoothstep(0.0, 1.0, clamp((u_progress - delay) / 0.3, 0.0, 1.0));
  vec3 shapeA = texture2D(u_maskA, toUv(q)).rgb;
  vec3 shapeB = texture2D(u_maskB, toUv(q)).rgb;
  // The mask's soft outline would leave half-strength specks along every
  // edge; a cell is either on the figure or off it, and only when the whole
  // tile (at full size, so the call never depends on brightness) fits.
  vec2 tip = vec2(1.0, 1.0 / SQ3) * u_spacing * 0.866 * 0.84 * 0.85 / u_radius;
  shapeA.r *= smoothstep(0.12, 0.26, shapeA.r) * fits(u_maskA, q, tip);
  shapeB.r *= smoothstep(0.12, 0.26, shapeB.r) * fits(u_maskB, q, tip);
  float m = mix(shapeA.r, shapeB.r, k);
  // The build front burns brighter as it passes.
  float flare = 4.0 * k * (1.0 - k) * max(shapeA.r, shapeB.r);

  // A level band of light rises through the figure, then rests.
  float glint = exp(-pow((q.y - u_sweep) / 0.14, 2.0));
  float shimmer = 0.93 + 0.07 * sin(u_time * (1.1 + 1.6 * hash(id + 7.1)) + 6.2832 * hash(id + 3.7));

  // The pointer is a lamp: it raises the bare lattice and the figure under it.
  vec2 lamp = (cellPx - u_mouse) / (150.0 * u_pixel);
  float lit = exp(-dot(lamp, lamp)) * u_pointer;

  // Size follows only what the figure is, so tiles never pulse in and out;
  // the shimmer, the glint and the build front change light, not size.
  float floorLevel = shapeA.b * u_floor;
  float body = max(m, floorLevel) + lit * (0.14 + 0.3 * m);
  float level = max(m * shimmer * (1.0 + 0.6 * glint) + 0.7 * flare, floorLevel);
  level += lit * (0.14 + 0.45 * m);

  // Each cell is an isometric diamond; at full strength neighbours meet, so
  // lit faces read as solid tiles and dim ones break into a dotted grain.
  float fill = clamp(body, 0.0, 1.0);
  float w = u_spacing * 0.866 * mix(0.2, 0.84, sqrt(fill));
  float n = abs(off.x) + abs(off.y) * SQ3;
  float dotA = 1.0 - smoothstep(w - 0.9, w + 0.9, n);
  dotA *= smoothstep(0.015, 0.08, body);
  dotA *= mix(0.35, 1.0, smoothstep(0.0, 0.7, clamp(level, 0.0, 1.0)));
  vec3 dotC = mix(u_deep, u_color, smoothstep(0.0, 0.8, level));
  dotC = mix(dotC, u_hot, smoothstep(0.85, 1.6, level));

  // Halo: the blurred figure where this pixel sits, plus a broad wash.
  vec2 qp = (frag - u_center) / u_radius;
  float halo = sampleShape(qp, u_progress).g;
  float wash = exp(-dot(qp, qp) * 0.8);
  float glowA = u_glow * (halo * mix(0.18, 0.5, u_dark) + wash * mix(0.04, 0.16, u_dark));
  // Interleaved gradient noise, half a step, so the dark falloff never bands.
  float ign = fract(52.9829189 * fract(dot(frag, vec2(0.06711056, 0.00583715))));
  glowA = clamp(glowA + (ign - 0.5) / 255.0, 0.0, 1.0);

  // Dot over halo, premultiplied for a transparent canvas.
  float outA = dotA + glowA * (1.0 - dotA);
  vec3 outC = dotC * dotA + u_color * glowA * (1.0 - dotA);
  gl_FragColor = vec4(outC, outA);
}
`;
function iso(x, y, z) {
  return [(x - z) * COS30, y - (x + z) * 0.5];
}
function gray(level) {
  const v = Math.round(clamp(level, 0, 1) * 255);
  return `rgb(${v},${v},${v})`;
}
function polyline(ctx, points, close = !1) {
  ctx.beginPath(), points.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)), close && ctx.closePath();
}
function stroke(ctx, points, level, width) {
  polyline(ctx, points), ctx.strokeStyle = gray(level), ctx.lineWidth = width, ctx.stroke();
}
function fill(ctx, points, level) {
  polyline(ctx, points, !0), ctx.fillStyle = gray(level), ctx.fill();
}
const FLOOR = -0.4;
function box(ctx, [x0, x1], [y0, y1], [z0, z1]) {
  ctx.globalCompositeOperation = "source-over", ctx.lineCap = "butt", ctx.lineJoin = "round", fill(
    ctx,
    [iso(x1, y1, z1), iso(x0, y1, z1), iso(x0, y0, z1), iso(x1, y0, z1)],
    0.6
  ), fill(
    ctx,
    [iso(x1, y1, z1), iso(x1, y1, z0), iso(x1, y0, z0), iso(x1, y0, z1)],
    0.42
  ), fill(
    ctx,
    [iso(x1, y1, z1), iso(x1, y1, z0), iso(x0, y1, z0), iso(x0, y1, z1)],
    0.78
  ), stroke(ctx, [iso(x0, y1, z1), iso(x1, y1, z1), iso(x1, y1, z0)], 1, 0.035), stroke(ctx, [iso(x1, y1, z1), iso(x1, y0, z1)], 0.7, 0.035);
}
function drawCube(ctx) {
  box(ctx, [-0.5, 0.5], [FLOOR, FLOOR + 1], [-0.5, 0.5]);
}
function drawSteps(ctx) {
  for (let i = 3; i >= 0; i--) {
    const near = 0.5 - i * 0.25;
    box(ctx, [-0.5, 0.5], [FLOOR, FLOOR + (i + 1) * 0.25], [near - 0.25, near]);
  }
}
function drawPillars(ctx) {
  const heights = [
    [0.35, 0.6, 1],
    [0.55, 0.85, 0.45],
    [0.9, 0.3, 0.65]
  ], cells = [];
  for (let i = 0; i < 3; i++)
    for (let j = 0; j < 3; j++)
      cells.push([(i - 1) * 0.34, (j - 1) * 0.34, heights[i][j]]);
  cells.sort((a, b) => a[0] + a[1] - (b[0] + b[1]));
  const half = 0.13;
  for (const [x, z, height] of cells)
    box(
      ctx,
      [x - half, x + half],
      [FLOOR, FLOOR + height],
      [z - half, z + half]
    );
}
function drawCoil(ctx) {
  ctx.globalCompositeOperation = "source-over", ctx.lineCap = "round";
  const turns = 3.5, steps = 280, radius = 0.5, points = [];
  for (let i = 0; i <= steps; i++) {
    const t = i / steps * turns * Math.PI * 2, x = radius * Math.cos(t), z = radius * Math.sin(t), y = FLOOR + 0.06 + i / steps * 0.92;
    points.push([iso(x, y, z), x + z]);
  }
  polyline(ctx, ellipse(radius + 0.1, FLOOR), !0), ctx.strokeStyle = gray(0.22), ctx.lineWidth = 0.04, ctx.stroke();
  for (const near of [!1, !0])
    for (let i = 1; i < points.length; i++) {
      const [a, depth] = points[i - 1], [b] = points[i];
      depth > 0 === near && stroke(ctx, [a, b], near ? 1 : 0.38, 0.085);
    }
}
function ellipse(radius, y) {
  const out = [];
  for (let i = 0; i < 96; i++) {
    const t = i / 96 * Math.PI * 2;
    out.push(iso(radius * Math.cos(t), y, radius * Math.sin(t)));
  }
  return out;
}
function drawFloor(ctx) {
  ctx.globalCompositeOperation = "source-over", fill(
    ctx,
    [
      iso(0.8, FLOOR, 0.8),
      iso(0.8, FLOOR, -0.8),
      iso(-0.8, FLOOR, -0.8),
      iso(-0.8, FLOOR, 0.8)
    ],
    0.05
  );
  for (let i = -2; i <= 2; i++) {
    const v = i / 2 * 0.8;
    stroke(ctx, [iso(v, FLOOR, -0.8), iso(v, FLOOR, 0.8)], 0.15, 0.07), stroke(ctx, [iso(-0.8, FLOOR, v), iso(0.8, FLOOR, v)], 0.15, 0.07);
  }
}
const DRAW = {
  cube: drawCube,
  steps: drawSteps,
  pillars: drawPillars,
  coil: drawCoil
};
function softBlur(source, into) {
  let current = source;
  for (let size = 512 / 2; size >= 512 / 16; size /= 2) {
    const next = document.createElement("canvas");
    next.width = next.height = size;
    const ctx = next.getContext("2d");
    ctx.imageSmoothingQuality = "high", ctx.drawImage(current, 0, 0, size, size), current = next;
  }
  into.imageSmoothingQuality = "high", into.drawImage(current, 0, 0, 512, 512);
}
const maskCache = /* @__PURE__ */ new Map();
function figureCanvas(draw) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 512;
  const ctx = canvas.getContext("2d", { willReadFrequently: !0 });
  ctx.fillStyle = "#000", ctx.fillRect(0, 0, 512, 512);
  const unit = 512 / (2 * 1.6);
  return ctx.setTransform(unit, 0, 0, -unit, 512 / 2, 512 / 2), draw(ctx), ctx.setTransform(1, 0, 0, 1, 0, 0), { canvas, ctx };
}
let floorPixels = null;
function renderMask(shape) {
  const cached = maskCache.get(shape);
  if (cached) return cached;
  const { canvas: sharp, ctx } = figureCanvas(DRAW[shape]);
  floorPixels ?? (floorPixels = figureCanvas(drawFloor).ctx.getImageData(
    0,
    0,
    512,
    512
  ).data);
  const soft = document.createElement("canvas");
  soft.width = soft.height = 512;
  const sctx = soft.getContext("2d", { willReadFrequently: !0 });
  sctx.fillStyle = "#000", sctx.fillRect(0, 0, 512, 512), typeof sctx.filter == "string" ? (sctx.filter = `blur(${512 / 36}px)`, sctx.drawImage(sharp, 0, 0)) : softBlur(sharp, sctx);
  const a = ctx.getImageData(0, 0, 512, 512).data, b = sctx.getImageData(0, 0, 512, 512).data, out = new Uint8Array(512 * 512 * 4);
  for (let i = 0; i < out.length; i += 4)
    out[i] = a[i], out[i + 1] = b[i], out[i + 2] = floorPixels[i], out[i + 3] = 255;
  return maskCache.set(shape, out), out;
}
function isDev() {
  return typeof process < "u" && !0;
}
function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}
function numberOr(value, fallback, min, max) {
  return typeof value != "number" || !Number.isFinite(value) ? fallback : clamp(value, min, max);
}
function hexToRgb(hex) {
  const h = hex.replace("#", "").trim(), full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h.padEnd(6, "0").slice(0, 6), n = Number.parseInt(full, 16);
  return Number.isNaN(n) ? [1, 0.7, 0.36] : [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
}
function mixRgb(a, b, t) {
  return [
    a[0] + (b[0] - a[0]) * t,
    a[1] + (b[1] - a[1]) * t,
    a[2] + (b[2] - a[2]) * t
  ];
}
function shapesOf(value) {
  if (!Array.isArray(value)) return SHAPES;
  const valid = value.filter((s) => s in DRAW);
  return valid.length ? valid : SHAPES;
}
function isDarkTheme() {
  if (typeof document > "u") return !1;
  const root = document.documentElement;
  if (root.classList.contains("dark")) return !0;
  if (root.classList.contains("light")) return !1;
  const dataTheme = root.getAttribute("data-theme");
  return dataTheme === "dark" ? !0 : dataTheme === "light" ? !1 : window.matchMedia("(prefers-color-scheme: dark)").matches;
}
function resolveDark(theme) {
  return theme === "dark" ? !0 : theme === "light" ? !1 : isDarkTheme();
}
function compile(gl, type, source) {
  const shader = gl.createShader(type);
  return shader ? (gl.shaderSource(shader, source), gl.compileShader(shader), gl.getShaderParameter(shader, gl.COMPILE_STATUS) ? shader : (isDev() && console.warn(
    `IsoLattice: shader failed to compile
`,
    gl.getShaderInfoLog(shader)
  ), gl.deleteShader(shader), null)) : null;
}
function createIsoLattice(canvas, initial = {}) {
  let options = {
    floor: !0,
    interactive: !0,
    theme: "auto",
    ...initial
  };
  const mouse = { x: 0, y: 0 }, target = { x: 0, y: 0 };
  let lamp = 0, lampTarget = 0, dark = resolveDark(options.theme ?? "auto");
  options.onThemeChange?.(dark);
  const gl = canvas.getContext("webgl", {
    alpha: !0,
    premultipliedAlpha: !0,
    antialias: !1,
    depth: !1,
    stencil: !1,
    powerPreference: "high-performance"
  });
  if (!gl) return null;
  let vs = null, fs = null, program = null, buf = null, textures = /* @__PURE__ */ new Map();
  const u = {}, texture = (shape) => {
    const existing = textures.get(shape);
    if (existing) return existing;
    const tex = gl.createTexture();
    return gl.bindTexture(gl.TEXTURE_2D, tex), gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR), gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR), gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE), gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE), gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.RGBA,
      512,
      512,
      0,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      renderMask(shape)
    ), textures.set(shape, tex), tex;
  }, initGl = () => {
    if (vs = compile(gl, gl.VERTEX_SHADER, VERT), fs = compile(gl, gl.FRAGMENT_SHADER, FRAG), !vs || !fs)
      return vs && gl.deleteShader(vs), fs && gl.deleteShader(fs), !1;
    if (program = gl.createProgram(), !program)
      return gl.deleteShader(vs), gl.deleteShader(fs), !1;
    if (gl.attachShader(program, vs), gl.attachShader(program, fs), gl.linkProgram(program), !gl.getProgramParameter(program, gl.LINK_STATUS))
      return isDev() && console.warn(
        `IsoLattice: program failed to link
`,
        gl.getProgramInfoLog(program)
      ), gl.deleteProgram(program), gl.deleteShader(vs), gl.deleteShader(fs), !1;
    gl.useProgram(program), buf = gl.createBuffer(), gl.bindBuffer(gl.ARRAY_BUFFER, buf), gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
      gl.STATIC_DRAW
    );
    const loc = gl.getAttribLocation(program, "a_position");
    gl.enableVertexAttribArray(loc), gl.vertexAttribPointer(loc, 2, gl.FLOAT, !1, 0, 0), gl.viewport(0, 0, canvas.width, canvas.height);
    const linked = program;
    for (const name of [
      "resolution",
      "center",
      "radius",
      "spacing",
      "pixel",
      "maskA",
      "maskB",
      "progress",
      "time",
      "sweep",
      "glow",
      "floor",
      "mouse",
      "pointer",
      "dark",
      "color",
      "deep",
      "hot"
    ])
      u[name] = gl.getUniformLocation(linked, `u_${name}`);
    return gl.uniform1i(u.maskA, 0), gl.uniform1i(u.maskB, 1), textures = /* @__PURE__ */ new Map(), !0;
  };
  if (!initGl()) return null;
  let pixel = 1;
  const resize = () => {
    const parent = canvas.parentElement;
    if (!parent) return !1;
    const w = parent.clientWidth, h = parent.clientHeight;
    if (w <= 0 || h <= 0) return !1;
    pixel = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.floor(w * pixel)), height = Math.max(1, Math.floor(h * pixel));
    return canvas.width === width && canvas.height === height ? !1 : (canvas.width = width, canvas.height = height, canvas.style.width = `${w}px`, canvas.style.height = `${h}px`, gl.viewport(0, 0, canvas.width, canvas.height), wake(), !0);
  };
  let colorKey = "";
  const syncColor = () => {
    const hex = options.color || (dark ? DARK_COLOR : LIGHT_COLOR), key = `${hex}|${dark}`;
    if (key === colorKey) return;
    colorKey = key;
    const base = hexToRgb(hex), white = [1, 1, 1], black = [0, 0, 0], deep = mixRgb(base, dark ? black : white, 0.55), hot = mixRgb(base, dark ? white : black, dark ? 0.62 : 0.5);
    gl.uniform3fv(u.color, base), gl.uniform3fv(u.deep, deep), gl.uniform3fv(u.hot, hot), gl.uniform1f(u.dark, dark ? 1 : 0);
  };
  let shapes = shapesOf(options.shapes), index = 0, held = 0, progress = 0;
  const mqReduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let onScreen = !0, raf = 0, destroyed = !1, lost = !1, last = 0, clock = 0;
  const moving = () => onScreen && !mqReduce.matches && document.visibilityState !== "hidden";
  function wake() {
    raf || destroyed || lost || (last = performance.now(), raf = requestAnimationFrame(tick));
  }
  function redraw() {
    destroyed || lost || (cancelAnimationFrame(raf), raf = 0, last = performance.now(), tick(last));
  }
  const advance = (dt) => {
    if (shapes.length < 2) {
      progress = 0;
      return;
    }
    progress > 0 || held >= numberOr(options.interval, 4, 0.5, 60) ? (progress += dt / 1.6, progress >= 1 && (index = (index + 1) % shapes.length, progress = 0, held = 0)) : held += dt;
  }, tick = (now) => {
    if (raf = 0, lost) return;
    const dt = Math.min(0.1, Math.max(0, (now - last) / 1e3));
    last = now;
    const live = moving(), speed = numberOr(options.speed, 1, 0, 4);
    live && (clock += dt * speed, advance(dt));
    const still = mqReduce.matches;
    still && (index = 0, progress = 0);
    const ease = 1 - Math.exp(-dt * 6);
    mouse.x += (target.x - mouse.x) * ease, mouse.y += (target.y - mouse.y) * ease, lamp += (lampTarget - lamp) * (1 - Math.exp(-dt * 3)), syncColor();
    const w = canvas.width, h = canvas.height, radius = numberOr(options.size, 0.6, 0.2, 1) * Math.min(w, h) / 2, pointer = options.interactive === !1 || still ? 0 : lamp, cx = w / 2, cy = h / 2;
    gl.activeTexture(gl.TEXTURE0), gl.bindTexture(gl.TEXTURE_2D, texture(shapes[index % shapes.length])), gl.activeTexture(gl.TEXTURE1), gl.bindTexture(gl.TEXTURE_2D, texture(shapes[(index + 1) % shapes.length])), gl.uniform2f(u.resolution, w, h), gl.uniform2f(u.center, cx, cy), gl.uniform1f(u.radius, radius), gl.uniform1f(
      u.spacing,
      numberOr(options.spacing, 8, 4, 24) * pixel
    ), gl.uniform1f(u.pixel, pixel), gl.uniform1f(u.progress, progress), gl.uniform1f(u.time, clock), gl.uniform1f(u.sweep, still ? -9 : -1.6 + 4.2 * (clock / 4.2 % 1)), gl.uniform1f(u.glow, numberOr(options.glow, 0.6, 0, 1)), gl.uniform1f(u.floor, options.floor === !1 ? 0 : 1), gl.uniform2f(u.mouse, mouse.x, mouse.y), gl.uniform1f(u.pointer, pointer), gl.clearColor(0, 0, 0, 0), gl.clear(gl.COLOR_BUFFER_BIT), gl.drawArrays(gl.TRIANGLES, 0, 6);
    const settling = Math.abs(lampTarget - lamp) > 2e-3;
    (live || settling && !still) && (raf = requestAnimationFrame(tick));
  }, onLost = (e) => {
    e.preventDefault(), lost = !0, cancelAnimationFrame(raf), raf = 0, vs = fs = program = buf = null, textures = /* @__PURE__ */ new Map();
  }, onRestored = () => {
    destroyed || !initGl() || (lost = !1, colorKey = "", wake());
  };
  canvas.addEventListener("webglcontextlost", onLost), canvas.addEventListener("webglcontextrestored", onRestored);
  const ro = new ResizeObserver(() => {
    resize() && redraw();
  });
  canvas.parentElement && ro.observe(canvas.parentElement);
  const io = new IntersectionObserver(([entry]) => {
    onScreen = entry?.isIntersecting ?? !0, wake();
  });
  io.observe(canvas);
  const onVisibility = () => wake();
  document.addEventListener("visibilitychange", onVisibility);
  const onReduce = () => wake();
  mqReduce.addEventListener("change", onReduce);
  const syncTheme = () => {
    const next = resolveDark(options.theme ?? "auto");
    next !== dark && (dark = next, options.onThemeChange?.(dark), wake());
  }, mo = new MutationObserver(syncTheme);
  mo.observe(document.documentElement, {
    attributes: !0,
    attributeFilter: ["class", "data-theme", "style"]
  });
  const mqDark = window.matchMedia("(prefers-color-scheme: dark)");
  mqDark.addEventListener("change", syncTheme);
  const onMove = (e) => {
    if (options.interactive === !1) return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const x = (e.clientX - rect.left) / rect.width, y = 1 - (e.clientY - rect.top) / rect.height, inside = x >= 0 && x <= 1 && y >= 0 && y <= 1;
    inside && (lamp < 0.01 && (mouse.x = x * canvas.width, mouse.y = y * canvas.height), target.x = x * canvas.width, target.y = y * canvas.height);
    const next = inside ? 1 : 0;
    next !== lampTarget && (lampTarget = next, wake());
  }, onLeave = () => {
    lampTarget = 0, wake();
  };
  return window.addEventListener("pointermove", onMove, { passive: !0 }), document.documentElement.addEventListener("pointerleave", onLeave), resize(), wake(), {
    setOptions(next) {
      options = { ...options, ...next };
      const nextShapes = shapesOf(options.shapes);
      nextShapes.join() !== shapes.join() && (shapes = nextShapes, index = 0, held = 0, progress = 0), syncTheme(), wake();
    },
    destroy() {
      destroyed = !0, cancelAnimationFrame(raf), raf = 0, ro.disconnect(), io.disconnect(), mo.disconnect(), document.removeEventListener("visibilitychange", onVisibility), mqReduce.removeEventListener("change", onReduce), mqDark.removeEventListener("change", syncTheme), window.removeEventListener("pointermove", onMove), document.documentElement.removeEventListener("pointerleave", onLeave), canvas.removeEventListener("webglcontextlost", onLost), canvas.removeEventListener("webglcontextrestored", onRestored);
      for (const tex of textures.values()) gl.deleteTexture(tex);
      textures.clear(), gl.deleteProgram(program), gl.deleteShader(vs), gl.deleteShader(fs), gl.deleteBuffer(buf);
      const lose = gl.getExtension("WEBGL_lose_context");
      queueMicrotask(() => {
        !canvas.isConnected && !gl.isContextLost() && lose?.loseContext();
      });
    }
  };
}
export {
  DARK_COLOR,
  DEFAULT_GLOW,
  DEFAULT_INTERVAL,
  DEFAULT_SIZE,
  DEFAULT_SPACING,
  DEFAULT_SPEED,
  LIGHT_COLOR,
  SHAPES,
  createIsoLattice,
  isDarkTheme,
  resolveDark
};
