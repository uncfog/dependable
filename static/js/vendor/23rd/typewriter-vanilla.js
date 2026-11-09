const DEFAULT_TEXT = `Dear reader,
every key is wired to a typebar. Press one and its bar swings up out of the basket, strikes the ribbon, and the carriage steps left for the next letter.

Yours, 23rd`, DEFAULT_COLOR = "#8EC3B5", DEFAULT_SPEED = 12, DEFAULT_COLUMNS = 32, DEFAULT_HOLD = 2600, DEFAULT_START_DELAY = 600, DEFAULT_JITTER = 0.3, BELL_CELLS = 6;
function layoutTypewriter(text, columns) {
  const width = Math.max(1, Math.floor(columns)), source = Array.from(text.replace(/\r\n?/g, `
`).replace(/\t/g, "  ")), strokes = [];
  let line = 0, col = 0, pendingWraps = [];
  const feed = (src) => {
    const stroke = {
      kind: "return",
      line: line + 1,
      from: col,
      src
    };
    return strokes.push(stroke), line += 1, col = 0, stroke;
  }, wrap = () => pendingWraps.push(feed(1 / 0)), type = (ch, src) => {
    for (const stroke of pendingWraps) stroke.src = src - 0.5;
    pendingWraps = [], strokes.push({ kind: "char", ch, line, col, src }), col += 1;
  };
  let i = 0;
  for (; i < source.length; ) {
    const ch = source[i];
    if (ch === `
`) {
      for (const stroke of pendingWraps) stroke.src = i - 0.5;
      pendingWraps = [], feed(i), i += 1;
      continue;
    }
    if (/\s/.test(ch)) {
      col < width && type(" ", i), i += 1;
      continue;
    }
    let end = i;
    for (; end < source.length && !/\s/.test(source[end]); ) end += 1;
    const length = end - i;
    col > 0 && col + length > width && wrap();
    for (let k = i; k < end; k++)
      col >= width && wrap(), type(source[k], k);
    i = end;
  }
  return strokes;
}
function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}
function numberOr(value, fallback, min, max) {
  return typeof value != "number" || !Number.isFinite(value) ? fallback : clamp(value, min, max);
}
function hash(n) {
  let x = Math.imul(n + 1, 668265261) ^ 374761393;
  return x = Math.imul(x ^ x >>> 15, 2246822507), x = Math.imul(x ^ x >>> 13, 3266489909), ((x ^ x >>> 16) >>> 0) / 4294967296;
}
const SVG_NS = "http://www.w3.org/2000/svg";
function createSound() {
  const Ctx = window.AudioContext ?? window.webkitAudioContext;
  if (!Ctx) return null;
  let audio = null, out = null, noise = null, closed = !1;
  const ready = () => {
    if (closed) return null;
    if (!audio) {
      audio = new Ctx(), out = audio.createGain(), out.gain.value = 0.55, out.connect(audio.destination), noise = audio.createBuffer(
        1,
        Math.floor(audio.sampleRate * 0.4),
        audio.sampleRate
      );
      const data = noise.getChannelData(0);
      for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
    }
    return audio.state === "suspended" && audio.resume().catch(() => {
    }), audio.state === "running" ? audio : null;
  }, burst = (a, at, freq, q, peak, decay) => {
    const src = a.createBufferSource();
    src.buffer = noise;
    const band = a.createBiquadFilter();
    band.type = "bandpass", band.frequency.value = freq, band.Q.value = q;
    const gain = a.createGain();
    gain.gain.setValueAtTime(1e-4, at), gain.gain.exponentialRampToValueAtTime(peak, at + 2e-3), gain.gain.exponentialRampToValueAtTime(1e-4, at + decay), src.connect(band).connect(gain).connect(out), src.start(at, Math.random() * 0.2), src.stop(at + decay + 0.02);
  }, thump = (a, at, freq, peak) => {
    const osc = a.createOscillator();
    osc.frequency.setValueAtTime(freq, at), osc.frequency.exponentialRampToValueAtTime(freq * 0.5, at + 0.05);
    const gain = a.createGain();
    gain.gain.setValueAtTime(1e-4, at), gain.gain.exponentialRampToValueAtTime(peak, at + 3e-3), gain.gain.exponentialRampToValueAtTime(1e-4, at + 0.06), osc.connect(gain).connect(out), osc.start(at), osc.stop(at + 0.08);
  };
  return {
    key(space) {
      const a = ready();
      if (!a) return;
      const at = a.currentTime;
      space ? (burst(a, at, 900, 0.9, 0.22, 0.05), thump(a, at, 110, 0.3)) : (burst(a, at, 2600 + Math.random() * 900, 1.3, 0.34, 0.035), thump(a, at, 150 + Math.random() * 30, 0.2));
    },
    bell() {
      const a = ready();
      if (!a) return;
      const at = a.currentTime;
      for (const [freq, peak] of [
        [2093, 0.16],
        [5776, 0.05]
      ]) {
        const osc = a.createOscillator();
        osc.frequency.value = freq;
        const gain = a.createGain();
        gain.gain.setValueAtTime(1e-4, at), gain.gain.exponentialRampToValueAtTime(peak, at + 4e-3), gain.gain.exponentialRampToValueAtTime(1e-4, at + 1.1), osc.connect(gain).connect(out), osc.start(at), osc.stop(at + 1.2);
      }
    },
    carriage(ms) {
      const a = ready();
      if (!a) return;
      const at = a.currentTime, seconds = ms / 1e3;
      for (let i = 0; i < 3; i++) burst(a, at + i * 0.028, 1800, 2, 0.16, 0.02);
      const src = a.createBufferSource();
      src.buffer = noise, src.loop = !0;
      const low = a.createBiquadFilter();
      low.type = "bandpass", low.Q.value = 0.8, low.frequency.setValueAtTime(700, at), low.frequency.linearRampToValueAtTime(1400, at + seconds);
      const gain = a.createGain();
      gain.gain.setValueAtTime(1e-4, at), gain.gain.exponentialRampToValueAtTime(0.07, at + seconds * 0.3), gain.gain.exponentialRampToValueAtTime(1e-4, at + seconds), src.connect(low).connect(gain).connect(out), src.start(at), src.stop(at + seconds + 0.02), thump(a, at + seconds, 120, 0.28);
    },
    close() {
      closed = !0, audio?.close().catch(() => {
      }), audio = null;
    }
  };
}
const TAU = Math.PI * 2, CW = 10, LH = 22, FONT = 16.5, PAD = 30, TOP_MARGIN = 38, VIEW_TOP = -178, VIEW_BOTTOM = 314, STRIKE_Y = -5, BASKET_Y = 42, PIVOT_R = 56, REST_R = 27, SPREAD = 74 * Math.PI / 180, KNOB_R = 21, RIDGES = 11, SPOOL_Y = 80, SPOOL_X = 150, KEY_ROWS = ["1234567890", "qwertyuiop", "asdfghjkl;", "zxcvbnm,./"], KEY_Y = [136, 166, 196, 226], KEY_PITCH = 35, KEY_R = 12.5, KEY_TILT = 0.64, KEY_TRAVEL = 4, FLEX_GAIN = 42e-4, FLEX_MAX = 5, FLEX_K = 170, FLEX_C = 9, FLEX_ANCHOR = 40, SHIFTED = {
  "!": "1",
  '"': "2",
  "#": "3",
  $: "4",
  "%": "5",
  _: "6",
  "&": "7",
  "'": "8",
  "(": "9",
  ")": "0",
  ":": ";",
  "?": "/",
  "<": ",",
  ">": "."
}, INK = "#24221F", LEGEND = "#EFE7D1", RIBBON_BLACK = "#16171A", RIBBON_RED = "#C4342B", CHROME = "#D3D8DC", CHROME_EDGE = "#5E656C", SHEET = "var(--typewriter-sheet, #FBF9F3)";
function draw(tag, attrs, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs))
    key === "fill" && value === "sheet" ? node.style.fill = SHEET : node.setAttribute(key, String(value));
  return parent?.append(node), node;
}
let swatch = null;
function rgbOf(color, fallback) {
  if (swatch ?? (swatch = document.createElement("canvas").getContext("2d", { willReadFrequently: !0 })), !swatch) return fallback;
  swatch.clearRect(0, 0, 1, 1), swatch.fillStyle = `rgb(${fallback.join(" ")})`, swatch.fillStyle = color, swatch.fillRect(0, 0, 1, 1);
  const [r = 0, g = 0, b = 0] = swatch.getImageData(0, 0, 1, 1).data;
  return [r, g, b];
}
function mix(a, b, t) {
  return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(" ")})`;
}
const linear = (t) => t, easeIn = (t) => t * t * t, easeOut = (t) => 1 - (1 - t) ** 3, easeInOut = (t) => t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
function pulse(t, from, rise, hold) {
  return t < rise ? from + (1 - from) * easeOut(t / rise) : t < rise + hold ? 1 : 1 - easeInOut((t - rise - hold) / Math.max(1e-3, 1 - rise - hold));
}
function polar(r, angle) {
  return [r * Math.sin(angle), BASKET_Y + r * Math.cos(angle)];
}
function drawMachine(svg, id, columns, color, label) {
  const span = columns * CW, platenL = -PAD - 22, platenR = span + PAD + 22, knobW = 15, leverX = platenL - knobW - 4, left = leverX - 56 - (span + CW / 2), right = platenR + knobW - CW / 2, half = Math.ceil(Math.max(-left, right, 262) + 14);
  svg.replaceChildren(), svg.setAttribute(
    "viewBox",
    `${-half} ${VIEW_TOP} ${half * 2} ${VIEW_BOTTOM - VIEW_TOP}`
  );
  const base = rgbOf(color, [142, 195, 181]), white = [255, 255, 255], black = [0, 0, 0], enamel = {
    shine: mix(base, white, 0.62),
    hi: mix(base, white, 0.34),
    light: mix(base, white, 0.14),
    mid: mix(base, base, 0),
    low: mix(base, black, 0.16),
    deep: mix(base, black, 0.34),
    edge: mix(base, black, 0.55)
  }, defs = draw("defs", {}, svg), gradient = (name, kind, attrs, stops) => {
    const el = draw(kind, { id: `${id}-${name}`, ...attrs }, defs);
    for (const [offset, stopColor, alpha = 1] of stops)
      draw(
        "stop",
        { offset, "stop-color": stopColor, "stop-opacity": alpha },
        el
      );
    return `url(#${id}-${name})`;
  }, down = { x1: 0, y1: 0, x2: 0, y2: 1 }, across = { x1: 0, y1: 0, x2: 1, y2: 0 }, fills = {
    body: gradient("body", "linearGradient", down, [
      [0, enamel.hi],
      [0.18, enamel.light],
      [0.62, enamel.mid],
      [1, enamel.low]
    ]),
    sides: gradient("sides", "linearGradient", across, [
      [0, "#000", 0.26],
      [0.1, "#000", 0.04],
      [0.5, "#fff", 0.05],
      [0.9, "#000", 0.04],
      [1, "#000", 0.3]
    ]),
    front: gradient("front", "linearGradient", down, [
      [0, enamel.low],
      [1, enamel.deep]
    ]),
    rest: gradient("rest", "linearGradient", down, [
      [0, enamel.hi],
      [0.5, enamel.light],
      [1, enamel.low]
    ]),
    chrome: gradient("chrome", "linearGradient", down, [
      [0, "#FDFEFE"],
      [0.38, "#D2D7DB"],
      [0.52, "#8B9298"],
      [0.7, "#C3C8CC"],
      [1, "#EEF0F2"]
    ]),
    chromeV: gradient("chrome-v", "linearGradient", across, [
      [0, "#8B9298"],
      [0.35, "#F4F6F7"],
      [0.6, "#BFC4C9"],
      [1, "#737A80"]
    ]),
    rubber: gradient("rubber", "linearGradient", down, [
      [0, "#55595E"],
      [0.16, "#2A2D31"],
      [0.55, "#0D0E10"],
      [0.85, "#050607"],
      [1, "#1E2124"]
    ]),
    bakelite: gradient("bakelite", "linearGradient", down, [
      [0, "#62666C"],
      [0.2, "#2C2F33"],
      [0.6, "#111214"],
      [1, "#2A2D31"]
    ]),
    capTop: gradient(
      "cap-top",
      "radialGradient",
      { cx: 0.38, cy: 0.3, r: 0.75 },
      [
        [0, "#575C63"],
        [0.55, "#202326"],
        [1, "#0C0D0F"]
      ]
    ),
    capSide: gradient("cap-side", "linearGradient", down, [
      [0, "#9AA1A7"],
      [0.5, "#4B5157"],
      [1, "#2A2E32"]
    ]),
    well: gradient("well", "linearGradient", down, [
      [0, "#08090A"],
      [0.18, "#151719"],
      [1, "#24272A"]
    ]),
    recess: gradient("recess", "radialGradient", { cx: 0.5, cy: 0, r: 1 }, [
      [0, "#030304"],
      [0.7, "#141618"],
      [1, "#24272A"]
    ]),
    spoolRibbon: gradient(
      "spool-ribbon",
      "radialGradient",
      { cx: 0.42, cy: 0.38, r: 0.7 },
      [
        [0, "#3B3E43"],
        [0.7, "#151618"],
        [1, "#060607"]
      ]
    ),
    flange: gradient(
      "flange",
      "radialGradient",
      { cx: 0.36, cy: 0.3, r: 0.85 },
      [
        [0, "#FFFFFF"],
        [0.45, "#CDD2D6"],
        [1, "#7D848A"]
      ]
    ),
    ground: gradient("ground", "radialGradient", { cx: 0.5, cy: 0.5, r: 0.5 }, [
      [0, "#000", 0.34],
      [0.6, "#000", 0.12],
      [1, "#000", 0]
    ]),
    keyShadow: gradient(
      "key-shadow",
      "radialGradient",
      { cx: 0.5, cy: 0.5, r: 0.5 },
      [
        [0, "#000", 0.7],
        [1, "#000", 0]
      ]
    ),
    curl: gradient(
      "curl",
      "linearGradient",
      { gradientUnits: "userSpaceOnUse", x1: 0, y1: 12, x2: 0, y2: 41 },
      [
        [0, "#3A2E1C", 0],
        [0.55, "#3A2E1C", 0.1],
        [1, "#3A2E1C", 0.34]
      ]
    ),
    contact: gradient("contact", "linearGradient", down, [
      [0, "#000", 0.3],
      [1, "#000", 0]
    ])
  }, inkFills = [
    [down, 0, 0.36],
    [down, 1, 0.4],
    [across, 0, 0.42],
    [across, 1, 0.42]
  ].map(
    ([dir, faintEnd, alpha], i) => gradient(
      `ink-${i}`,
      "linearGradient",
      dir,
      faintEnd === 0 ? [
        [0, INK, alpha],
        [0.55, INK, 1],
        [1, INK, 1]
      ] : [
        [0, INK, 1],
        [0.45, INK, 1],
        [1, INK, alpha]
      ]
    )
  ), fade = gradient(
    "fade-g",
    "linearGradient",
    {
      gradientUnits: "userSpaceOnUse",
      x1: 0,
      x2: 0,
      y1: VIEW_TOP,
      y2: 41
    },
    [
      [0, "#fff", 0],
      [84 / (41 - VIEW_TOP), "#fff", 1],
      [0.998, "#fff", 1],
      [1, "#fff", 0]
    ]
  ), box = { x: -4e3, y: VIEW_TOP - 4, width: 8e3, height: 50 - VIEW_TOP }, mask = draw(
    "mask",
    { id: `${id}-fade`, maskUnits: "userSpaceOnUse", ...box },
    defs
  );
  draw("rect", { ...box, fill: fade }, mask), draw("ellipse", { cx: 0, cy: 302, rx: 300, ry: 15, fill: fills.ground }, svg);
  const rig = draw("g", {}, svg), carriage = draw("g", {}, rig);
  draw(
    "rect",
    {
      x: platenL - knobW - 8,
      y: 40,
      width: platenR - platenL + knobW * 2 + 16,
      height: 6,
      rx: 3,
      fill: fills.chrome
    },
    carriage
  );
  const tL = -PAD - 16, tR = span + PAD + 16;
  draw(
    "path",
    {
      d: `M${tL} 10V-80Q${tL} -98 ${tL + 18} -98H${tR - 18}Q${tR} -98 ${tR} -80V10Z`,
      fill: fills.rest
    },
    carriage
  ), draw(
    "path",
    {
      d: `M${tL + 1} -80Q${tL + 1} -97 ${tL + 18} -97H${tR - 18}Q${tR - 1} -97 ${tR - 1} -80`,
      fill: "none",
      stroke: enamel.shine,
      "stroke-width": 1.4,
      "stroke-opacity": 0.85
    },
    carriage
  ), draw(
    "rect",
    {
      x: platenL,
      y: 6,
      width: platenR - platenL,
      height: 34,
      rx: 4,
      fill: fills.rubber
    },
    carriage
  ), draw(
    "rect",
    {
      x: platenL + 4,
      y: 10.5,
      width: platenR - platenL - 8,
      height: 1.6,
      rx: 0.8,
      fill: "#fff",
      "fill-opacity": 0.22
    },
    carriage
  );
  for (const x of [platenL - 4, platenR])
    draw(
      "rect",
      { x, y: 9, width: 4, height: 28, rx: 1, fill: fills.chrome },
      carriage
    );
  const knobs = [platenL - 4 - knobW, platenR + 4].map((x) => {
    draw(
      "rect",
      { x, y: 1, width: knobW, height: 44, rx: 6, fill: fills.bakelite },
      carriage
    );
    const lines = Array.from(
      { length: RIDGES },
      () => draw(
        "line",
        {
          x1: x + 2.5,
          x2: x + knobW - 2.5,
          stroke: "#B8BEC4",
          "stroke-width": 1,
          "stroke-linecap": "round"
        },
        carriage
      )
    );
    return draw(
      "rect",
      {
        x: x + 2,
        y: 3,
        width: knobW - 4,
        height: 6,
        rx: 3,
        fill: "#fff",
        "fill-opacity": 0.12
      },
      carriage
    ), { x, lines };
  }), lever = draw("g", { "data-key": "return" }, carriage), leverPivot = [leverX, 30];
  draw(
    "rect",
    {
      x: leverX - 5,
      y: 20,
      width: 10,
      height: 22,
      rx: 3,
      fill: fills.chromeV
    },
    lever
  );
  const arm = `M${leverX} 28L${leverX - 13} 4L${leverX - 36} -14`;
  for (const [stroke, width] of [
    [CHROME_EDGE, 6.4],
    [CHROME, 4]
  ])
    draw(
      "path",
      {
        d: arm,
        fill: "none",
        stroke,
        "stroke-width": width,
        "stroke-linecap": "round",
        "stroke-linejoin": "round"
      },
      lever
    );
  draw(
    "path",
    {
      d: `M${leverX - 1} 26L${leverX - 13} 4.5L${leverX - 34} -12`,
      fill: "none",
      stroke: "#fff",
      "stroke-width": 1.2,
      "stroke-opacity": 0.8,
      "stroke-linecap": "round",
      "stroke-linejoin": "round"
    },
    lever
  ), draw(
    "rect",
    {
      x: leverX - 56,
      y: -25,
      width: 28,
      height: 11,
      rx: 5.5,
      fill: fills.bakelite,
      transform: `rotate(-24 ${leverX - 42} -19.5)`
    },
    lever
  );
  const wrap = draw("g", { mask: `url(#${id}-fade)` }, carriage), paper = draw("g", {}, wrap), shadow = draw(
    "rect",
    {
      x: -PAD + 3,
      y: -TOP_MARGIN + 3,
      width: span + PAD * 2,
      height: 600,
      fill: "#000",
      "fill-opacity": 0.12
    },
    paper
  ), sheet = draw(
    "rect",
    {
      x: -PAD,
      y: -TOP_MARGIN,
      width: span + PAD * 2,
      height: 600,
      fill: "sheet",
      stroke: "#000",
      "stroke-opacity": 0.07,
      "stroke-width": 0.8
    },
    paper
  ), ink = draw(
    "g",
    { "text-anchor": "middle", "font-size": FONT, fill: INK },
    paper
  );
  draw(
    "rect",
    {
      x: -PAD,
      y: 12,
      width: span + PAD * 2,
      height: 30,
      fill: fills.curl
    },
    wrap
  ), draw(
    "rect",
    {
      x: -PAD + 2,
      y: 8.5,
      width: span + PAD * 2 - 4,
      height: 6,
      rx: 3,
      fill: fills.chrome
    },
    carriage
  );
  let ticks = "";
  for (let c = 0; c <= columns; c++) {
    const x = c * CW;
    ticks += `M${x} 9.4V${c % 10 === 0 ? 13.6 : c % 5 === 0 ? 12.4 : 11.2}`;
  }
  draw(
    "path",
    {
      d: ticks,
      stroke: "#3E444A",
      "stroke-width": 0.7,
      fill: "none"
    },
    carriage
  );
  const body = draw("g", {}, rig), shell = "M-192 42Q-214 42 -219 62C-228 120 -240 190 -247 246Q-249 262 -232 264H232Q249 262 247 246C240 190 228 120 219 62Q214 42 192 42Z";
  if (draw("path", { d: shell, fill: fills.body }, body), draw("path", { d: shell, fill: fills.sides }, body), draw(
    "rect",
    { x: -206, y: 42.5, width: 412, height: 15, fill: fills.contact },
    body
  ), draw(
    "path",
    {
      d: "M-247 248Q-249 262 -232 264H232Q249 262 247 248L245 284Q243 296 227 296H-227Q-243 296 -245 284Z",
      fill: fills.front
    },
    body
  ), draw(
    "path",
    {
      d: "M-240 264.5H240",
      stroke: enamel.shine,
      "stroke-width": 1.2,
      "stroke-opacity": 0.7
    },
    body
  ), draw(
    "rect",
    {
      x: -236,
      y: 274,
      width: 472,
      height: 3,
      rx: 1.5,
      fill: fills.chrome
    },
    body
  ), label) {
    const w = Math.max(36, label.length * 7.4 + 18);
    draw(
      "rect",
      {
        x: -w / 2,
        y: 268.5,
        width: w,
        height: 14,
        rx: 3,
        fill: fills.chrome,
        stroke: CHROME_EDGE,
        "stroke-width": 0.8
      },
      body
    );
    for (const [dy2, fill] of [
      [0.6, "#fff"],
      [0, "#30353A"]
    ]) {
      const plate = draw(
        "text",
        {
          x: 0,
          y: 276 + dy2,
          "text-anchor": "middle",
          "dominant-baseline": "central",
          "font-size": 8.4,
          "font-weight": 700,
          "letter-spacing": 1.6,
          fill
        },
        body
      );
      plate.textContent = label;
    }
  }
  for (const x of [-228, 188])
    draw(
      "rect",
      { x, y: 293, width: 40, height: 10, rx: 4, fill: "#151618" },
      body
    );
  const sheen = gradient("sheen", "linearGradient", across, [
    [0, "#fff", 0.34],
    [0.55, "#fff", 0.1],
    [1, "#fff", 0]
  ]);
  draw(
    "path",
    { d: "M-210 48H-96L-150 110H-224Q-222 82 -210 48Z", fill: sheen },
    body
  ), draw(
    "path",
    {
      d: "M-194 43.4H194",
      stroke: enamel.shine,
      "stroke-width": 1.6,
      "stroke-linecap": "round"
    },
    body
  ), draw(
    "path",
    {
      d: "M-208 56Q-212 70 -216 96",
      stroke: enamel.shine,
      "stroke-width": 2.4,
      "stroke-linecap": "round",
      "stroke-opacity": 0.55,
      fill: "none"
    },
    body
  ), draw(
    "path",
    {
      d: `M-68 ${BASKET_Y}A68 68 0 0 0 68 ${BASKET_Y}Z`,
      fill: fills.recess
    },
    body
  ), draw(
    "path",
    {
      d: `M-68 ${BASKET_Y}A68 68 0 0 0 68 ${BASKET_Y}`,
      fill: "none",
      stroke: enamel.shine,
      "stroke-width": 1.3,
      "stroke-opacity": 0.75
    },
    body
  );
  const basket = draw("g", {}, body), [ax, ay] = polar(62, -SPREAD - 0.12), [bx, by] = polar(62, SPREAD + 0.12), [cx, cy] = polar(53, SPREAD + 0.12), [dx, dy] = polar(53, -SPREAD - 0.12);
  draw(
    "path",
    {
      d: `M${ax} ${ay}A62 62 0 0 0 ${bx} ${by}L${cx} ${cy}A53 53 0 0 1 ${dx} ${dy}Z`,
      fill: fills.chrome
    },
    basket
  );
  const spools = [-SPOOL_X, SPOOL_X].map((x) => {
    draw(
      "circle",
      { cx: x, cy: SPOOL_Y + 2, r: 27, fill: "#000", "fill-opacity": 0.22 },
      body
    ), draw("circle", { cx: x, cy: SPOOL_Y, r: 26, fill: fills.flange }, body), draw("circle", { cx: x, cy: SPOOL_Y, r: 22, fill: fills.spoolRibbon }, body);
    const turn = draw("g", {}, body);
    let windows = "";
    for (let i = 0; i < 3; i++) {
      const a0 = i / 3 * TAU + 0.25, a1 = a0 + 1.35, p = (r, a) => `${(x + Math.cos(a) * r).toFixed(2)} ${(SPOOL_Y + Math.sin(a) * r).toFixed(2)}`;
      windows += `M${p(19, a0)}A19 19 0 0 1 ${p(19, a1)}L${p(8.5, a1)}A8.5 8.5 0 0 0 ${p(8.5, a0)}Z`;
    }
    return draw(
      "path",
      {
        d: `M${x - 21} ${SPOOL_Y}a21 21 0 1 0 42 0a21 21 0 1 0 -42 0Z${windows}`,
        fill: fills.flange,
        "fill-rule": "evenodd"
      },
      turn
    ), draw("circle", { cx: x, cy: SPOOL_Y, r: 5, fill: fills.chrome }, turn), draw("circle", { cx: x, cy: SPOOL_Y, r: 1.6, fill: "#2A2D31" }, turn), turn;
  });
  for (const x of [-23, 17])
    draw(
      "rect",
      {
        x,
        y: 0,
        width: 6,
        height: 16,
        rx: 1.6,
        fill: fills.chromeV,
        stroke: CHROME_EDGE,
        "stroke-width": 0.8
      },
      body
    );
  const ribbon = [
    {
      path: draw(
        "path",
        {
          fill: "none",
          stroke: RIBBON_BLACK,
          "stroke-width": 5.8,
          "stroke-linejoin": "round"
        },
        body
      ),
      dy: 0
    },
    {
      path: draw(
        "path",
        {
          fill: "none",
          stroke: RIBBON_RED,
          "stroke-width": 2.6,
          "stroke-linejoin": "round"
        },
        body
      ),
      dy: 1.5
    }
  ], vibrator = draw("g", {}, body);
  for (const [stroke, width] of [
    [CHROME_EDGE, 3.8],
    [CHROME, 2]
  ])
    draw(
      "path",
      {
        d: "M-12 2.5V11.5H12V2.5",
        fill: "none",
        stroke,
        "stroke-width": width,
        "stroke-linejoin": "round",
        "stroke-linecap": "round"
      },
      vibrator
    );
  draw(
    "path",
    {
      d: "M-204 112H204Q217 112 219 125L233 240Q235 254 220 254H-220Q-235 254 -233 240L-219 125Q-217 112 -204 112Z",
      fill: fills.well
    },
    body
  ), draw(
    "path",
    {
      d: "M-219.5 126Q-217.5 111 -204 111H204Q217.5 111 219.5 126",
      fill: "none",
      stroke: enamel.shine,
      "stroke-width": 1.3,
      "stroke-opacity": 0.8
    },
    body
  ), draw(
    "path",
    {
      d: "M-226 254.6H226",
      stroke: enamel.shine,
      "stroke-width": 1.2,
      "stroke-opacity": 0.6
    },
    body
  );
  const ry = KEY_R * KEY_TILT, height = 4.5, keyAt = (x, y, label2, ch) => {
    draw(
      "ellipse",
      {
        cx: x + 2,
        cy: y + 21,
        rx: 11,
        ry: 4,
        fill: fills.keyShadow
      },
      body
    ), draw(
      "rect",
      {
        x: x - 1.5,
        y: y + 4,
        width: 3,
        height: 19,
        rx: 1,
        fill: fills.chromeV
      },
      body
    );
    const cap = draw("g", {}, body);
    draw(
      "path",
      {
        d: `M${x - KEY_R} ${y}V${y + height}A${KEY_R} ${ry} 0 0 0 ${x + KEY_R} ${y + height}V${y}A${KEY_R} ${ry} 0 0 1 ${x - KEY_R} ${y}Z`,
        fill: fills.capSide
      },
      cap
    ), draw("ellipse", { cx: x, cy: y, rx: KEY_R, ry, fill: fills.chrome }, cap), draw(
      "ellipse",
      {
        cx: x,
        cy: y + 0.3,
        rx: KEY_R - 2.3,
        ry: ry - 1.6,
        fill: fills.capTop
      },
      cap
    ), draw(
      "ellipse",
      {
        cx: x - 3.4,
        cy: y - 2.6,
        rx: 4.4,
        ry: 1.6,
        fill: "#fff",
        "fill-opacity": 0.2
      },
      cap
    );
    const text = draw(
      "text",
      {
        x,
        y: y + 0.7,
        "text-anchor": "middle",
        "dominant-baseline": "central",
        "font-size": 9.6,
        "font-weight": 600,
        fill: LEGEND,
        transform: `matrix(1 0 0 0.74 0 ${(y * 0.26).toFixed(2)})`
      },
      cap
    );
    return text.textContent = label2, draw(
      "path",
      {
        d: `M${x - KEY_R + 3.6} ${y - 0.4}A${KEY_R - 3.6} ${ry - 2.3} 0 0 1 ${x + KEY_R - 3.6} ${y - 0.4}`,
        fill: "none",
        stroke: "#fff",
        "stroke-width": 0.9,
        "stroke-opacity": 0.32,
        "stroke-linecap": "round"
      },
      cap
    ), cap.setAttribute("data-key", ch), cap;
  }, keys = /* @__PURE__ */ new Map(), placed = [];
  KEY_ROWS.forEach((row, r) => {
    Array.from(row).forEach((ch, i) => {
      const x = -157.5 + (r - 1.5) * 9 + i * KEY_PITCH, key = {
        cap: keyAt(x, KEY_Y[r], ch.toUpperCase(), ch),
        bar: null,
        v: 0
      };
      keys.set(ch, key), placed.push({ x, key });
    });
  });
  const pill = (x, y, w, legend) => {
    for (const sx of [x - w * 0.3, x + w * 0.3])
      draw(
        "rect",
        {
          x: sx - 1.5,
          y: y + 4,
          width: 3,
          height: 17,
          rx: 1,
          fill: fills.chromeV
        },
        body
      );
    draw(
      "ellipse",
      {
        cx: x + 2,
        cy: y + 19,
        rx: w / 2 + 2,
        ry: 4,
        fill: fills.keyShadow
      },
      body
    );
    const cap = draw("g", {}, body), r = ry;
    return draw(
      "rect",
      {
        x: x - w / 2,
        y: y - r + height,
        width: w,
        height: r * 2,
        rx: r,
        fill: fills.capSide
      },
      cap
    ), draw(
      "rect",
      {
        x: x - w / 2,
        y: y - r,
        width: w,
        height: r * 2,
        rx: r,
        fill: fills.chrome
      },
      cap
    ), draw(
      "rect",
      {
        x: x - w / 2 + 2.3,
        y: y - r + 1.6,
        width: w - 4.6,
        height: r * 2 - 3.2,
        rx: r - 1.6,
        fill: fills.capTop
      },
      cap
    ), legend && draw("path", { d: legend, fill: LEGEND }, cap), cap;
  }, y3 = KEY_Y[3], shifts = [-1, 1].map((side) => {
    const x = side * 201, arrow = `M${x} ${y3 - 3.6}L${x + 4} ${y3 + 0.6}H${x + 1.6}V${y3 + 3.4}H${x - 1.6}V${y3 + 0.6}H${x - 4}Z`, cap = pill(x, y3, 30, arrow);
    return cap.setAttribute("data-key", "shift"), { cap, bar: null, v: 0 };
  });
  for (const sx of [-80, 80])
    draw(
      "rect",
      {
        x: sx - 1.5,
        y: 247,
        width: 3,
        height: 7,
        fill: fills.chromeV
      },
      body
    );
  const spaceCap = draw("g", { "data-key": " " }, body);
  draw(
    "rect",
    {
      x: -112,
      y: 242,
      width: 224,
      height: 9,
      rx: 4.5,
      fill: "#4E555B"
    },
    spaceCap
  ), draw(
    "rect",
    {
      x: -112,
      y: 239.5,
      width: 224,
      height: 8,
      rx: 4,
      fill: fills.chrome
    },
    spaceCap
  );
  const space = { cap: spaceCap, bar: null, v: 0 }, overlay = draw("g", {}, rig);
  placed.sort((a, b) => a.x - b.x);
  const bars = placed.map(({ key }, i) => {
    const angle = -SPREAD + 2 * SPREAD * i / (placed.length - 1), pivot = polar(PIVOT_R, angle), tip = polar(REST_R, angle), rest = draw("g", {}, basket);
    draw(
      "line",
      {
        x1: pivot[0],
        y1: pivot[1],
        x2: tip[0],
        y2: tip[1],
        stroke: i % 2 ? "#C4C9CE" : "#E6E9EC",
        "stroke-width": 1.25,
        "stroke-linecap": "round"
      },
      rest
    );
    const [sx, sy] = polar(REST_R + 3.4, angle);
    draw(
      "line",
      {
        x1: sx,
        y1: sy,
        x2: tip[0],
        y2: tip[1],
        stroke: "#7D848A",
        "stroke-width": 2.6,
        "stroke-linecap": "round"
      },
      rest
    );
    const strike = draw("g", { display: "none" }, overlay), line = (stroke, width) => draw(
      "line",
      { stroke, "stroke-width": width, "stroke-linecap": "round" },
      strike
    ), edge = line("#1C1E21", 3.9), core = line("#E2E5E8", 1.8), slug = line("#2A2D31", 5.6), bar = { rest, strike, edge, core, slug, pivot, tip, v: 0 };
    return key.bar = bar, bar;
  });
  return {
    columns,
    rig,
    inkFills,
    carriage,
    paper,
    sheet,
    shadow,
    ink,
    lever,
    leverPivot,
    knobs,
    keys,
    shifts,
    space,
    basket,
    bars,
    ribbon,
    vibrator,
    spools
  };
}
let machineIds = 0;
function createTypewriter(root, initial = {}) {
  let options = { ...initial };
  const textOf = () => options.text ?? DEFAULT_TEXT, speedOf = () => numberOr(options.speed, 12, 1, 60), holdOf = () => numberOr(options.hold, 2600, 0, 6e4), startOf = () => numberOr(options.startDelay, 600, 0, 6e4), jitterOf = () => numberOr(options.jitter, 0.3, 0, 1), columnsOf = () => Math.round(numberOr(options.columns, 32, 16, 60)), id = `typewriter-${++machineIds}`, spoken = document.createElement("span");
  Object.assign(spoken.style, {
    position: "absolute",
    width: "1px",
    height: "1px",
    margin: "-1px",
    padding: "0",
    overflow: "hidden",
    clip: "rect(0 0 0 0)",
    whiteSpace: "nowrap",
    border: "0"
  });
  const svg = draw("svg", {
    width: "100%",
    height: "100%",
    preserveAspectRatio: "xMidYMid meet",
    "aria-hidden": "true"
  });
  svg.style.display = "block", root.append(spoken, svg);
  let M;
  const build = () => {
    M = drawMachine(
      svg,
      id,
      columnsOf(),
      options.color ?? DEFAULT_COLOR,
      options.label
    ), setInteractive();
  }, pose = {
    carriage: 0,
    paper: 0,
    knob: 0,
    spool: 0,
    lever: 0,
    shift: 0,
    ribbon: 0
  }, carriageAt = (col) => -(col * CW + CW / 2), setCarriage = (x) => {
    pose.carriage = x, M.carriage.setAttribute("transform", `translate(${x.toFixed(2)} 0)`);
  }, flex = { angle: 0, vel: 0, x: 0, t: 0 }, placePaper = () => {
    const y = pose.paper, a = flex.angle;
    M.paper.setAttribute(
      "transform",
      Math.abs(a) < 0.01 ? `translate(0 ${y.toFixed(2)})` : `translate(0 ${FLEX_ANCHOR}) skewX(${a.toFixed(3)}) translate(0 ${(y - FLEX_ANCHOR).toFixed(2)})`
    );
  }, setPaper = (y) => {
    pose.paper = y, placePaper();
  }, setKnob = (phase2) => {
    pose.knob = phase2;
    for (const knob of M.knobs)
      knob.lines.forEach((line, i) => {
        const a = phase2 + i / RIDGES * TAU, facing = Math.cos(a);
        if (facing <= 0.05) {
          line.setAttribute("stroke-opacity", "0");
          return;
        }
        const y = (23 + Math.sin(a) * 19).toFixed(2);
        line.setAttribute("y1", y), line.setAttribute("y2", y), line.setAttribute("stroke-opacity", (0.18 + facing * 0.42).toFixed(2));
      });
  }, setSpool = (deg) => {
    pose.spool = deg, M.spools.forEach((spool, i) => {
      const x = i === 0 ? -SPOOL_X : SPOOL_X;
      spool.setAttribute(
        "transform",
        `rotate(${(i === 0 ? -deg : deg).toFixed(1)} ${x} ${SPOOL_Y})`
      );
    });
  }, setLever = (deg) => {
    pose.lever = deg;
    const [x, y] = M.leverPivot;
    M.lever.setAttribute("transform", `rotate(${deg.toFixed(2)} ${x} ${y})`);
  }, setShift = (v) => {
    pose.shift = v, M.basket.setAttribute("transform", `translate(0 ${(v * 4).toFixed(2)})`);
    for (const key of M.shifts) setKey(key, v);
  }, setRibbon = (v) => {
    pose.ribbon = v;
    const lift = v * 13, y = 7.5 - lift, top = SPOOL_Y - 24;
    for (const { path, dy } of M.ribbon)
      path.setAttribute(
        "d",
        `M${-SPOOL_X} ${top + dy}L-12 ${(y + dy).toFixed(2)}H12L${SPOOL_X} ${top + dy}`
      );
    M.vibrator.setAttribute("transform", `translate(0 ${(-lift).toFixed(2)})`);
  }, setKey = (key, v) => {
    key.v = v, key.cap.setAttribute(
      "transform",
      v > 1e-3 ? `translate(0 ${(v * KEY_TRAVEL).toFixed(2)})` : ""
    );
  }, setBar = (bar, v) => {
    if (bar.v = v, v <= 1e-3) {
      bar.rest.removeAttribute("display"), bar.strike.setAttribute("display", "none");
      return;
    }
    bar.rest.setAttribute("display", "none"), bar.strike.removeAttribute("display");
    const drop = pose.shift * 4, [px, py0] = bar.pivot, py = py0 + drop, tx = bar.tip[0] + (0 - bar.tip[0]) * v, ty = bar.tip[1] + drop + (STRIKE_Y - bar.tip[1] - drop) * v, len = Math.hypot(tx - px, ty - py) || 1, ux = (tx - px) / len, uy = (ty - py) / len;
    for (const line of [bar.edge, bar.core])
      line.setAttribute("x1", px.toFixed(2)), line.setAttribute("y1", py.toFixed(2)), line.setAttribute("x2", tx.toFixed(2)), line.setAttribute("y2", ty.toFixed(2));
    bar.slug.setAttribute("x1", (tx - ux * 6).toFixed(2)), bar.slug.setAttribute("y1", (ty - uy * 6).toFixed(2)), bar.slug.setAttribute("x2", tx.toFixed(2)), bar.slug.setAttribute("y2", ty.toFixed(2));
  }, restPose = () => {
    setCarriage(carriageAt(cursor.col)), setPaper(-cursor.line * LH), setKnob(pose.knob), setSpool(pose.spool), setLever(0), setShift(capsLock ? 1 : 0), setRibbon(0), flex.angle = 0, flex.vel = 0, placePaper();
    for (const key of M.keys.values()) setKey(key, 0);
    setKey(M.space, 0);
    for (const bar of M.bars) setBar(bar, 0);
  }, queued = [], running = /* @__PURE__ */ new Map();
  let raf = 0;
  const frame = (now) => {
    for (raf = 0, queued.sort((a, b) => a.start - b.start); queued.length && queued[0].start <= now; ) {
      const motion = queued.shift();
      motion.begin?.(), running.set(motion.key, motion);
    }
    for (const [key, motion] of running) {
      const t = clamp((now - motion.start) / motion.ms, 0, 1);
      motion.run(motion.ease(t)), t >= 1 && running.delete(key);
    }
    const dt = flex.t ? Math.min(0.05, (now - flex.t) / 1e3) : 0;
    if (flex.t = now, dt > 0) {
      const speed = (pose.carriage - flex.x) / dt, target = clamp(speed * FLEX_GAIN, -FLEX_MAX, FLEX_MAX);
      flex.vel += (-FLEX_K * (flex.angle - target) - FLEX_C * flex.vel) * dt, flex.angle += flex.vel * dt, placePaper();
    }
    flex.x = pose.carriage;
    const settling = Math.abs(flex.angle) > 0.02 || Math.abs(flex.vel) > 0.05;
    queued.length || running.size || settling ? raf = requestAnimationFrame(frame) : flex.t = 0;
  }, wake = () => {
    raf || (raf = requestAnimationFrame(frame));
  }, play = (key, ms, run, {
    delay = 0,
    ease = linear,
    begin
  } = {}) => {
    queued.push({
      key,
      start: performance.now() + delay,
      ms: Math.max(1, ms),
      ease,
      run,
      begin
    }), wake();
  }, tween = (key, get, set, to, ms, opts = {}) => {
    let from = 0;
    play(key, ms, (v) => set(from + (to - from) * v), {
      ...opts,
      begin: () => {
        from = get();
      }
    });
  }, stopMotion = () => {
    queued.length = 0, running.clear(), cancelAnimationFrame(raf), raf = 0;
  }, press = (name, key, ms, delay = 0) => {
    let from = 0;
    play(name, ms, (t) => setKey(key, pulse(t, from, 0.22, 0.28)), {
      delay,
      begin: () => {
        from = key.v;
      }
    });
  };
  let strokes = [], pos = 0, cursor = { line: 0, col: 0 }, sheetH = 600, phase = "waiting", capsLock = !1, nextSrc = 0, rang = !1, destroyed = !1, reduce = !1, onScreen = !0, sound = null, pending = null, timer = 0, remaining = 0, dueAt = 0;
  const active = () => !destroyed && onScreen && !reduce && document.visibilityState !== "hidden", arm = () => {
    dueAt = performance.now() + remaining, timer = window.setTimeout(() => {
      timer = 0;
      const run = pending;
      pending = null, run?.();
    }, remaining);
  }, schedule = (run, ms) => {
    window.clearTimeout(timer), timer = 0, pending = run, remaining = Math.max(0, ms), active() && arm();
  }, cancel = () => {
    window.clearTimeout(timer), timer = 0, pending = null;
  }, sync = () => {
    active() ? pending && !timer && arm() : timer && (window.clearTimeout(timer), timer = 0, remaining = Math.max(0, dueAt - performance.now()));
  }, soundOn = () => options.sound ? (sound ?? (sound = createSound()), sound) : null, after = (stroke) => stroke ? stroke.kind === "char" ? { line: stroke.line, col: stroke.col + 1 } : { line: stroke.line, col: 0 } : { line: 0, col: 0 }, letter = (stroke) => {
    const jitter = jitterOf(), n = Math.floor(stroke.src) * 8, x = stroke.col * CW + CW / 2, y = stroke.line * LH, group = draw("g", {}, M.ink), glyph = (attrs) => {
      const el = draw("text", { x, y, ...attrs }, group);
      return el.textContent = stroke.ch, el;
    };
    if (jitter <= 0)
      return glyph({}), group;
    const dx = (hash(n) - 0.5) * jitter * 1.1, dy = (hash(n + 1) - 0.5) * jitter * 1.6, tilt = (hash(n + 2) - 0.5) * jitter * 3.2;
    group.setAttribute(
      "transform",
      `translate(${dx.toFixed(2)} ${dy.toFixed(2)}) rotate(${tilt.toFixed(2)} ${x} ${y})`
    );
    const ink = 1 - hash(n + 3) * jitter * 0.38, worn = hash(n + 4) < jitter * 0.6;
    return glyph({
      "fill-opacity": ink.toFixed(3),
      ...worn ? {
        fill: M.inkFills[Math.floor(hash(n + 5) * M.inkFills.length)]
      } : {}
    }), hash(n + 6) < jitter * 0.14 && glyph({
      "fill-opacity": (ink * 0.42).toFixed(3),
      transform: `translate(${(0.45 + hash(n + 7) * 0.3).toFixed(2)} 0.3)`
    }), group;
  }, sizeSheet = () => {
    const lines = (strokes.at(-1)?.line ?? 0) + 1;
    sheetH = Math.max(
      TOP_MARGIN + lines * LH + 60,
      (M.columns * CW + PAD * 2) * 1.3
    ), M.sheet.setAttribute("height", sheetH.toFixed(0)), M.shadow.setAttribute("height", sheetH.toFixed(0));
  }, paint = () => {
    M.ink.replaceChildren();
    for (let i = 0; i < pos; i++) {
      const stroke = strokes[i];
      stroke.kind === "char" && stroke.ch !== " " && letter(stroke);
    }
    phase !== "manual" && (cursor = after(strokes[pos - 1])), rang = cursor.col >= M.columns - 6, restPose();
  }, keyFor = (ch) => {
    const lower = ch.toLowerCase(), own = M.keys.get(lower);
    if (own) return { key: own, shift: ch !== lower };
    const base = SHIFTED[ch];
    if (base) return { key: M.keys.get(base), shift: !0 };
    const all = [...M.keys.values()];
    return {
      key: all[(ch.codePointAt(0) ?? 0) % all.length],
      shift: !1
    };
  }, pace = (ch) => {
    const base = 1e3 / speedOf();
    if (!(options.humanize ?? !0)) return base;
    let factor = 0.55 + Math.random() * 0.9;
    return ch === " " ? factor *= 1.15 : /[,;:]/.test(ch) ? factor *= 2.4 : /[.!?]/.test(ch) && (factor *= 3.4), Math.random() < 0.04 && (factor *= 2.6), base * factor;
  }, strike = (stroke, fixed) => {
    const base = 1e3 / speedOf(), ms = fixed ?? clamp(base * 1.7, 90, 170), audio = soundOn(), step2 = carriageAt(stroke.col + 1);
    if (cursor = { line: stroke.line, col: stroke.col + 1 }, stroke.ch === " ")
      press("space", M.space, ms), tween("carriage", () => pose.carriage, setCarriage, step2, ms * 0.4, {
        delay: ms * 0.2,
        ease: easeOut
      }), audio?.key(!0);
    else {
      const { key, shift } = keyFor(stroke.ch), lead = shift ? ms * 0.25 : 0, impact = lead + ms * 0.42;
      if (shift && !capsLock) {
        let from = 0;
        play("shift", ms * 1.6, (t) => setShift(pulse(t, from, 0.16, 0.5)), {
          begin: () => {
            from = pose.shift;
          }
        });
      }
      press(`key-${stroke.ch.toLowerCase()}`, key, ms * 1.1, lead);
      const bar = key.bar;
      if (bar) {
        let from = 0;
        play(
          `bar-${M.bars.indexOf(bar)}`,
          ms,
          (t) => {
            const v = t < 0.42 ? from + (1 - from) * easeIn(t / 0.42) : 1 - easeOut((t - 0.42) / 0.58);
            setBar(bar, v);
          },
          {
            delay: lead,
            begin: () => {
              from = bar.v;
            }
          }
        );
      }
      let lifted = 0;
      play("ribbon", ms * 0.9, (t) => setRibbon(pulse(t, lifted, 0.4, 0.08)), {
        delay: lead + ms * 0.08,
        begin: () => {
          lifted = pose.ribbon;
        }
      });
      const mark = letter(stroke);
      mark.setAttribute("opacity", "0"), play(
        `ink-${stroke.src}`,
        60,
        (v) => mark.setAttribute("opacity", v.toFixed(3)),
        { delay: impact }
      ), play(
        "shudder",
        110,
        (v) => {
          const y = Math.sin(v * Math.PI) * (1 - v) * 1.6;
          M.rig.setAttribute(
            "transform",
            y > 0.01 ? `translate(0 ${y.toFixed(2)})` : ""
          );
        },
        { delay: impact }
      ), tween("carriage", () => pose.carriage, setCarriage, step2, ms * 0.36, {
        delay: impact + 6,
        ease: easeOut
      }), tween("spool", () => pose.spool, setSpool, pose.spool + 5, ms * 0.3, {
        delay: impact
      }), audio && window.setTimeout(() => audio.key(!1), impact);
    }
    !rang && cursor.col >= M.columns - 6 && M.columns > 12 && (rang = !0, audio?.bell());
  }, carriageReturn = (stroke) => {
    const glide = clamp(220 + stroke.from * 8, 260, 640);
    return cursor = { line: stroke.line, col: 0 }, rang = !1, play("lever", 420, (t) => setLever(-24 * pulse(t, 0, 0.3, 0.12))), tween("paper", () => pose.paper, setPaper, -stroke.line * LH, 220, {
      delay: 60,
      ease: easeInOut
    }), tween("knob", () => pose.knob, setKnob, pose.knob + LH / KNOB_R, 220, {
      delay: 60,
      ease: easeInOut
    }), tween("carriage", () => pose.carriage, setCarriage, carriageAt(0), glide, {
      delay: 130,
      ease: easeInOut
    }), soundOn()?.carriage(glide), glide + 130;
  }, setPhase = (next) => {
    phase = next;
  }, finish = () => {
    setPhase("done"), options.onDone?.(), (options.loop ?? !0) && schedule(feedOut, holdOf());
  }, step = () => {
    if (pos >= strokes.length) {
      finish();
      return;
    }
    setPhase("typing");
    const stroke = strokes[pos++];
    if (stroke.kind === "char")
      strike(stroke), schedule(step, pace(stroke.ch));
    else {
      const ms = carriageReturn(stroke);
      schedule(step, ms + pace(" ") * 0.8);
    }
  }, feedIn = (then) => {
    setPaper(40 + TOP_MARGIN), tween("paper", () => pose.paper, setPaper, 0, 900, { ease: easeOut }), tween("knob", () => pose.knob, setKnob, pose.knob + 5, 900, {
      ease: easeOut
    }), schedule(then, 940);
  }, feedOut = () => {
    setPhase("feeding");
    const ms = 950;
    tween("paper", () => pose.paper, setPaper, VIEW_TOP - sheetH - 20, ms, {
      ease: easeIn
    }), tween("knob", () => pose.knob, setKnob, pose.knob + 7, ms, {
      ease: easeIn
    }), tween("carriage", () => pose.carriage, setCarriage, carriageAt(0), 720, {
      ease: easeInOut
    }), cursor.col > 0 && play("lever", 420, (t) => setLever(-24 * pulse(t, 0, 0.3, 0.12))), schedule(() => {
      pos = 0, paint(), setPhase("waiting"), feedIn(() => schedule(step, startOf() * 0.5));
    }, ms + 40);
  }, start = () => {
    if (cancel(), stopMotion(), capsLock = !1, strokes = layoutTypewriter(textOf(), M.columns), spoken.textContent = textOf(), sizeSheet(), reduce) {
      pos = strokes.length, setPhase("done"), paint();
      return;
    }
    pos = 0, setPhase("waiting"), paint(), feedIn(() => schedule(step, startOf()));
  }, rebuild = () => {
    const at = strokes[pos - 1]?.src ?? -1;
    if (stopMotion(), build(), phase === "manual") {
      sizeSheet(), paint();
      return;
    }
    if (strokes = layoutTypewriter(textOf(), M.columns), phase === "done") pos = strokes.length;
    else
      for (pos = 0; pos < strokes.length && strokes[pos].src <= at; ) pos += 1;
    sizeSheet(), paint(), phase === "typing" || phase === "feeding" ? (setPhase("typing"), schedule(step, 200)) : phase === "waiting" && schedule(step, startOf());
  }, interactiveOf = () => options.interactive ?? !0, takeOver = () => {
    phase !== "manual" && (cancel(), phase === "feeding" || phase === "waiting" ? (stopMotion(), strokes = phase === "feeding" ? [] : strokes.slice(0, pos)) : strokes = strokes.slice(0, pos), pos = strokes.length, cursor = after(strokes[pos - 1]), nextSrc = Math.floor(strokes.at(-1)?.src ?? -1) + 1, setPhase("manual"), sizeSheet(), running.size || paint());
  }, typeReturn = () => {
    takeOver();
    const stroke = {
      kind: "return",
      line: cursor.line + 1,
      from: cursor.col,
      src: nextSrc++
    };
    if (strokes.push(stroke), pos = strokes.length, sizeSheet(), reduce) {
      cursor = after(stroke), setPaper(-cursor.line * LH), setCarriage(carriageAt(0));
      return;
    }
    carriageReturn(stroke);
  }, typeKey = (ch) => {
    takeOver(), cursor.col >= M.columns && typeReturn();
    const stroke = {
      kind: "char",
      ch,
      line: cursor.line,
      col: cursor.col,
      src: nextSrc++
    };
    if (strokes.push(stroke), pos = strokes.length, reduce) {
      ch !== " " && letter(stroke), cursor = after(stroke), setCarriage(carriageAt(cursor.col));
      return;
    }
    strike(stroke, 120);
  }, typeBack = () => {
    if (takeOver(), cursor.col !== 0) {
      if (cursor = { line: cursor.line, col: cursor.col - 1 }, reduce) {
        setCarriage(carriageAt(cursor.col));
        return;
      }
      tween(
        "carriage",
        () => pose.carriage,
        setCarriage,
        carriageAt(cursor.col),
        90,
        {
          ease: easeOut
        }
      ), soundOn()?.key(!0);
    }
  }, setShiftLock = (on) => {
    capsLock = on, reduce ? setShift(on ? 1 : 0) : tween("shift", () => pose.shift, setShift, on ? 1 : 0, 130, {
      ease: easeOut
    });
  }, onKeyDown = (event) => {
    if (!interactiveOf() || event.ctrlKey || event.metaKey || event.altKey)
      return;
    const { key } = event;
    if (key === "Enter")
      event.repeat || typeReturn();
    else if (key === "Backspace")
      typeBack();
    else if (key.length === 1)
      event.repeat || typeKey(key);
    else
      return;
    event.preventDefault();
  }, onPointerDown = (event) => {
    if (!interactiveOf() || event.button !== 0) return;
    root.focus({ preventScroll: !0 });
    const key = event.target?.closest?.("[data-key]")?.getAttribute("data-key");
    key != null && (event.preventDefault(), key === "shift" ? setShiftLock(!capsLock) : key === "return" ? typeReturn() : typeKey(capsLock ? key.toUpperCase() : key));
  };
  function setInteractive() {
    const on = interactiveOf();
    on ? (root.tabIndex = 0, root.setAttribute("role", "group"), root.setAttribute(
      "aria-label",
      "Typewriter. Focus it and type, or click its keys."
    )) : (root.removeAttribute("tabindex"), root.removeAttribute("role"), root.removeAttribute("aria-label"));
    for (const el of svg.querySelectorAll("[data-key]"))
      el.style.cursor = on ? "pointer" : "";
  }
  root.addEventListener("keydown", onKeyDown), svg.addEventListener("pointerdown", onPointerDown);
  const isDark = () => {
    const html = document.documentElement;
    if (html.classList.contains("dark")) return !0;
    if (html.classList.contains("light")) return !1;
    const attr = html.getAttribute("data-theme");
    return attr === "dark" ? !0 : attr === "light" ? !1 : window.matchMedia("(prefers-color-scheme: dark)").matches;
  }, applyTheme = () => {
    root.style.setProperty(
      "--typewriter-sheet",
      isDark() ? "#ECE7DA" : "#FBF9F3"
    );
  };
  build(), applyTheme();
  const mqReduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  reduce = mqReduce.matches;
  const onReduce = () => {
    reduce = mqReduce.matches, start();
  };
  mqReduce.addEventListener("change", onReduce), start();
  const theme = new MutationObserver(applyTheme);
  theme.observe(document.documentElement, {
    attributes: !0,
    attributeFilter: ["class", "data-theme", "style"]
  });
  const mqDark = window.matchMedia("(prefers-color-scheme: dark)");
  mqDark.addEventListener("change", applyTheme);
  const io = new IntersectionObserver(([entry]) => {
    onScreen = entry?.isIntersecting ?? !0, sync();
  });
  io.observe(root);
  const onVisibility = () => sync();
  return document.addEventListener("visibilitychange", onVisibility), {
    setOptions(next) {
      const prev = options;
      if (options = { ...options, ...next }, textOf() !== (prev.text ?? DEFAULT_TEXT)) {
        start();
        return;
      }
      if ((columnsOf() !== M.columns || options.color !== prev.color || options.label !== prev.label) && rebuild(), !options.sound && sound && (sound.close(), sound = null), options.interactive !== prev.interactive) setInteractive();
      else if (options.jitter !== prev.jitter) {
        for (const motion of running.values()) motion.run(1);
        stopMotion(), paint();
      }
      const loop = options.loop ?? !0;
      loop !== (prev.loop ?? !0) && phase === "done" && (loop ? schedule(feedOut, holdOf()) : cancel());
    },
    restart: start,
    destroy() {
      destroyed = !0, cancel(), stopMotion(), theme.disconnect(), mqDark.removeEventListener("change", applyTheme), io.disconnect(), mqReduce.removeEventListener("change", onReduce), document.removeEventListener("visibilitychange", onVisibility), root.removeEventListener("keydown", onKeyDown), svg.removeEventListener("pointerdown", onPointerDown);
      for (const name of ["tabindex", "role", "aria-label"])
        root.removeAttribute(name);
      sound?.close(), root.style.removeProperty("--typewriter-sheet"), spoken.remove(), svg.remove();
    }
  };
}
export {
  DEFAULT_COLOR,
  DEFAULT_COLUMNS,
  DEFAULT_HOLD,
  DEFAULT_JITTER,
  DEFAULT_SPEED,
  DEFAULT_START_DELAY,
  DEFAULT_TEXT,
  createTypewriter,
  layoutTypewriter
};
