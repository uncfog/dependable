const WHITE = { color: "#F4F4F5", eyeColor: "#09090B" }, BLACK = { color: "#18181B", eyeColor: "#F4F4F5" }, CUSTOM_DEFAULT = { color: "#7C5CFF", eyeColor: "#FAFAFA" }, WEBGL_COLORS = ["#7C6AF7", "#7DD3C7", "#E8B4D4"];
function isDarkTheme() {
  if (typeof document > "u") return !1;
  const root = document.documentElement;
  if (root.classList.contains("dark")) return !0;
  if (root.classList.contains("light")) return !1;
  const dataTheme = root.getAttribute("data-theme");
  return dataTheme === "dark" ? !0 : dataTheme === "light" ? !1 : window.matchMedia("(prefers-color-scheme: dark)").matches;
}
function resolveDark(theme = "auto") {
  return theme === "dark" ? !0 : theme === "light" ? !1 : isDarkTheme();
}
function watchTheme(onChange) {
  if (typeof document > "u") return () => {
  };
  const mo = new MutationObserver(onChange);
  mo.observe(document.documentElement, {
    attributes: !0,
    attributeFilter: ["class", "data-theme", "style"]
  });
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  return mq.addEventListener("change", onChange), () => {
    mo.disconnect(), mq.removeEventListener("change", onChange);
  };
}
function defaultVariant(dark) {
  return dark ? "white" : "black";
}
const VERT = `
attribute vec2 a_position;
void main() {
  gl_Position = vec4(a_position, 0.0, 1.0);
}
`, FRAG = `
precision highp float;

uniform vec2 u_resolution;
uniform float u_time;
uniform float u_speed;
uniform vec2 u_look;
uniform float u_blink;
uniform float u_mode;
uniform vec3 u_body;
uniform vec3 u_eye;
uniform vec3 u_c1;
uniform vec3 u_c2;
uniform vec3 u_c3;

float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(a, b, u.x) + (c - a) * u.y * (1.0 - u.x) + (d - b) * u.x * u.y;
}

float fbm(vec2 p) {
  float v = 0.0;
  float a = 0.5;
  mat2 m = mat2(1.6, 1.2, -1.2, 1.6);
  for (int i = 0; i < 5; i++) {
    v += a * noise(p);
    p = m * p;
    a *= 0.5;
  }
  return v;
}

vec3 orient(vec3 p, vec2 look) {
  float yaw = look.x * 0.92;
  float pitch = -look.y * 0.78;
  float cy = cos(yaw);
  float sy = sin(yaw);
  float cp = cos(pitch);
  float sp = sin(pitch);
  vec3 q = vec3(p.x, p.y * cp - p.z * sp, p.y * sp + p.z * cp);
  return vec3(q.x * cy + q.z * sy, q.y, -q.x * sy + q.z * cy);
}

float eyeMask(vec3 n, vec3 e, vec3 right, vec3 up, float halfH, float rad) {
  float facing = dot(n, e);
  float x = dot(n, right) - dot(e, right);
  float y = dot(n, up) - dot(e, up);
  y -= clamp(y, -halfH, halfH);
  float d = length(vec2(x, y)) - rad;
  float fill = 1.0 - smoothstep(-0.01, 0.01, d);
  return fill * smoothstep(0.12, 0.32, facing);
}

vec3 wash(vec3 n, float t) {
  vec2 q = n.xy * 2.2 + n.z * 0.85;
  vec2 w1 = vec2(
    fbm(q * 1.4 + vec2(t * 0.22, t * 0.18)),
    fbm(q * 1.4 + vec2(-t * 0.16, t * 0.24) + 4.1)
  );
  q += (w1 - 0.5) * 0.72;
  float f = fbm(q * 2.1 + vec2(0.0, t * 0.12));
  float g = fbm(q * 4.6 - vec2(t * 0.2, 0.0));
  vec3 col = mix(u_c1, u_c2, smoothstep(0.28, 0.72, f));
  return mix(col, u_c3, pow(smoothstep(0.42, 0.9, g), 1.4));
}

void main() {
  vec2 uv = (gl_FragCoord.xy / u_resolution.xy) * 2.0 - 1.0;
  float aspect = u_resolution.x / max(u_resolution.y, 1.0);
  uv.x *= aspect;

  float radius = 0.94;
  vec2 p = uv / radius;
  float r2 = dot(p, p);
  float edge = 1.0 - smoothstep(0.985, 1.012, sqrt(max(r2, 0.0)));
  if (edge <= 0.001) {
    gl_FragColor = vec4(0.0);
    return;
  }

  float z = sqrt(max(1.0 - r2, 0.0));
  vec3 n = normalize(vec3(p, z));

  vec2 look = u_look;
  float lm = length(look);
  if (lm > 1.0) look /= lm;

  vec3 right = orient(vec3(1.0, 0.0, 0.0), look);
  vec3 up = orient(vec3(0.0, 1.0, 0.0), look);
  vec3 eL = orient(normalize(vec3(-0.32, 0.08, 1.0)), look);
  vec3 eR = orient(normalize(vec3(0.32, 0.08, 1.0)), look);

  float halfH = mix(0.128, 0.012, u_blink);
  float rad = mix(0.054, 0.062, u_blink);
  float eyes = max(
    eyeMask(n, eL, right, up, halfH, rad),
    eyeMask(n, eR, right, up, halfH, rad)
  );

  vec3 body = u_mode > 0.5 ? wash(n, u_time * u_speed) : u_body;
  vec3 col = mix(body, u_eye, clamp(eyes, 0.0, 1.0));

  gl_FragColor = vec4(clamp(col, 0.0, 1.0), edge);
}
`;
function isDev() {
  return typeof process < "u" && !0;
}
function hexToRgb(hex) {
  const h = hex.replace("#", "").trim(), full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h.padEnd(6, "0").slice(0, 6), n = Number.parseInt(full, 16);
  return Number.isNaN(n) ? [0.96, 0.96, 0.96] : [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
}
function compile(gl, type, source) {
  const shader = gl.createShader(type);
  return shader ? (gl.shaderSource(shader, source), gl.compileShader(shader), gl.getShaderParameter(shader, gl.COMPILE_STATUS) ? shader : (isDev() && console.warn(
    `LiveOrb: shader failed to compile
`,
    gl.getShaderInfoLog(shader)
  ), gl.deleteShader(shader), null)) : null;
}
function resolveVariant(variant, color, eyeColor, colors, dark = !1) {
  let body = WHITE.color, eye = WHITE.eyeColor, mode = 0, palette = WEBGL_COLORS;
  const preset = variant ?? defaultVariant(dark);
  return preset === "black" ? (body = BLACK.color, eye = BLACK.eyeColor) : preset === "webgl" ? (palette = colors && colors.length > 0 ? colors : WEBGL_COLORS, body = palette[1] ?? WEBGL_COLORS[1], eye = "#0C0C10", mode = 1) : preset === "custom" && (body = color ?? CUSTOM_DEFAULT.color, eye = eyeColor ?? CUSTOM_DEFAULT.eyeColor), { body, eye, mode, palette };
}
function fallbackFaceStyle(resolved) {
  if (resolved.mode === 1) {
    const a = resolved.palette[0] ?? WEBGL_COLORS[0], b = resolved.palette[1] ?? WEBGL_COLORS[1], c = resolved.palette[2] ?? WEBGL_COLORS[2];
    return {
      backgroundImage: `linear-gradient(135deg, ${a}, ${b}, ${c})`
    };
  }
  return { backgroundColor: resolved.body };
}
const IDLE_MS = 3e3, REST_LOOK = { x: 0, y: 0.08 };
function createLiveOrb(canvas, initial = {}) {
  let options = {
    interactive: !0,
    blink: !0,
    ...initial
  }, dark = resolveDark(options.theme);
  const look = { x: 0, y: 0.08 }, targetLook = { x: 0, y: 0.08 }, glance = { x: 0, y: 0.08 }, gl = canvas.getContext("webgl", {
    alpha: !0,
    antialias: !1,
    depth: !1,
    stencil: !1,
    premultipliedAlpha: !1,
    powerPreference: "high-performance"
  });
  if (!gl) return null;
  let vs = null, fs = null, program = null, buf = null, uResolution = null, uTime = null, uSpeed = null, uLook = null, uBlink = null, uMode = null, uBody = null, uEye = null, uC1 = null, uC2 = null, uC3 = null;
  const initGl = () => {
    if (vs = compile(gl, gl.VERTEX_SHADER, VERT), fs = compile(gl, gl.FRAGMENT_SHADER, FRAG), !vs || !fs) return !1;
    const prog = gl.createProgram();
    if (!prog) return !1;
    if (program = prog, gl.attachShader(prog, vs), gl.attachShader(prog, fs), gl.linkProgram(prog), !gl.getProgramParameter(prog, gl.LINK_STATUS))
      return isDev() && console.warn(
        `LiveOrb: program failed to link
`,
        gl.getProgramInfoLog(prog)
      ), !1;
    gl.useProgram(prog), buf = gl.createBuffer(), gl.bindBuffer(gl.ARRAY_BUFFER, buf), gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
      gl.STATIC_DRAW
    );
    const loc = gl.getAttribLocation(prog, "a_position");
    return gl.enableVertexAttribArray(loc), gl.vertexAttribPointer(loc, 2, gl.FLOAT, !1, 0, 0), uResolution = gl.getUniformLocation(prog, "u_resolution"), uTime = gl.getUniformLocation(prog, "u_time"), uSpeed = gl.getUniformLocation(prog, "u_speed"), uLook = gl.getUniformLocation(prog, "u_look"), uBlink = gl.getUniformLocation(prog, "u_blink"), uMode = gl.getUniformLocation(prog, "u_mode"), uBody = gl.getUniformLocation(prog, "u_body"), uEye = gl.getUniformLocation(prog, "u_eye"), uC1 = gl.getUniformLocation(prog, "u_c1"), uC2 = gl.getUniformLocation(prog, "u_c2"), uC3 = gl.getUniformLocation(prog, "u_c3"), gl.enable(gl.BLEND), gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA), gl.viewport(0, 0, canvas.width, canvas.height), !0;
  };
  if (!initGl()) return null;
  options.onHasGl?.(!0);
  let raf = 0, destroyed = !1, lost = !1, onScreen = !0, last = 0, clock = 0, nextBlink = 1800 + Math.random() * 2400, blinkAt = -1e4, nextGlance = 600 + Math.random() * 900, pointerAt = -1 / 0;
  const resize = () => {
    const parent = canvas.parentElement;
    if (!parent) return !1;
    const dpr = Math.min(window.devicePixelRatio || 1, 2), w = parent.clientWidth, h = parent.clientHeight;
    if (w <= 0 || h <= 0) return !1;
    const width = Math.max(1, Math.floor(w * dpr)), height = Math.max(1, Math.floor(h * dpr));
    return canvas.style.width = `${w}px`, canvas.style.height = `${h}px`, width === canvas.width && height === canvas.height ? !1 : (canvas.width = width, canvas.height = height, gl.viewport(0, 0, canvas.width, canvas.height), !0);
  }, mqReduce = window.matchMedia("(prefers-reduced-motion: reduce)"), moving = () => onScreen && !mqReduce.matches && document.visibilityState !== "hidden";
  function wake() {
    raf || destroyed || lost || (last = performance.now(), raf = requestAnimationFrame(tick));
  }
  function redraw() {
    destroyed || lost || (cancelAnimationFrame(raf), raf = 0, last = performance.now(), tick(last));
  }
  const onMove = (e) => {
    if (!options.interactive) return;
    const parent = canvas.parentElement;
    if (!parent) return;
    const rect = parent.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const dx = (e.clientX - (rect.left + rect.width / 2)) / (rect.width / 2), dy = (rect.top + rect.height / 2 - e.clientY) / (rect.height / 2);
    targetLook.x = Math.min(1, Math.max(-1, dx)), targetLook.y = Math.min(1, Math.max(-1, dy)), pointerAt = performance.now(), onScreen && wake();
  };
  window.addEventListener("pointermove", onMove, { passive: !0 });
  const tick = (now) => {
    if (raf = 0, lost) return;
    const dt = Math.min(50, Math.max(0, now - last));
    last = now;
    const live = moving();
    live && (clock += dt);
    const reduce = mqReduce.matches, resolved = resolveVariant(
      options.variant,
      options.color,
      options.eyeColor,
      options.colors,
      dark
    ), time = clock / 1e3, steering = options.interactive && now - pointerAt < IDLE_MS;
    if (!steering && live && clock >= nextGlance) {
      const back = Math.random() < 0.3;
      glance.x = back ? 0 : (Math.random() * 2 - 1) * 0.8, glance.y = back ? 0.08 : (Math.random() * 2 - 1) * 0.5, nextGlance = clock + 900 + Math.random() * 2100;
    }
    const goal = steering ? targetLook : reduce ? REST_LOOK : glance, ease = live ? steering ? 0.16 : 0.1 : 1;
    look.x += (goal.x - look.x) * ease, look.y += (goal.y - look.y) * ease, !reduce && options.blink && clock >= nextBlink && (blinkAt = clock, nextBlink = clock + 2200 + Math.random() * 3800);
    const bt = (clock - blinkAt) / 1e3;
    let b = 0;
    !reduce && options.blink && (bt < 0.055 ? b = bt / 0.055 : bt < 0.1 ? b = 1 : bt < 0.18 && (b = 1 - (bt - 0.1) / 0.08));
    const body = hexToRgb(resolved.body), eye = hexToRgb(resolved.eye), palette = [0, 1, 2].map(
      (i) => hexToRgb(
        resolved.palette[i % resolved.palette.length] ?? WEBGL_COLORS[i]
      )
    );
    gl.clearColor(0, 0, 0, 0), gl.clear(gl.COLOR_BUFFER_BIT), gl.uniform2f(uResolution, canvas.width, canvas.height), gl.uniform1f(uTime, reduce ? 0 : time), gl.uniform1f(uSpeed, reduce ? 0 : 0.55), gl.uniform2f(uLook, look.x, look.y), gl.uniform1f(uBlink, b), gl.uniform1f(uMode, resolved.mode), gl.uniform3f(uBody, body[0], body[1], body[2]), gl.uniform3f(uEye, eye[0], eye[1], eye[2]), gl.uniform3f(uC1, palette[0][0], palette[0][1], palette[0][2]), gl.uniform3f(uC2, palette[1][0], palette[1][1], palette[1][2]), gl.uniform3f(uC3, palette[2][0], palette[2][1], palette[2][2]), gl.drawArrays(gl.TRIANGLES, 0, 6), live && !destroyed && (raf = requestAnimationFrame(tick));
  }, ro = new ResizeObserver(() => {
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
  const unwatchTheme = watchTheme(() => {
    const next = resolveDark(options.theme);
    next !== dark && (dark = next, wake());
  }), onLost = (e) => {
    e.preventDefault(), lost = !0, cancelAnimationFrame(raf), raf = 0, canvas.style.visibility = "hidden", options.onHasGl?.(!1);
  }, onRestored = () => {
    destroyed || initGl() && (lost = !1, canvas.style.visibility = "", options.onHasGl?.(!0), wake());
  };
  return canvas.addEventListener("webglcontextlost", onLost), canvas.addEventListener("webglcontextrestored", onRestored), resize(), wake(), {
    setOptions(next) {
      options = { ...options, ...next }, dark = resolveDark(options.theme), wake();
    },
    destroy() {
      destroyed = !0, cancelAnimationFrame(raf), raf = 0, ro.disconnect(), io.disconnect(), document.removeEventListener("visibilitychange", onVisibility), mqReduce.removeEventListener("change", onReduce), unwatchTheme(), window.removeEventListener("pointermove", onMove), canvas.removeEventListener("webglcontextlost", onLost), canvas.removeEventListener("webglcontextrestored", onRestored), lost && (canvas.style.visibility = ""), options.onHasGl?.(!1), gl.deleteProgram(program), gl.deleteShader(vs), gl.deleteShader(fs), gl.deleteBuffer(buf);
      const lose = gl.getExtension("WEBGL_lose_context");
      queueMicrotask(() => {
        !canvas.isConnected && !gl.isContextLost() && lose?.loseContext();
      });
    }
  };
}
export {
  BLACK,
  CUSTOM_DEFAULT,
  WEBGL_COLORS,
  WHITE,
  createLiveOrb,
  defaultVariant,
  fallbackFaceStyle,
  isDarkTheme,
  resolveDark,
  resolveVariant,
  watchTheme
};
