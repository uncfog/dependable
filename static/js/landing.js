// The landing page's moving parts (templates/core/landing.html).
//
// Five effects from the 23rd registry, compiled to plain JavaScript in
// static/js/vendor/23rd and passed in by the page, plus the example ledger that
// gains a row now and then. Everything here is decoration over a page that
// already reads in full: each effect is started inside its own try, so one that
// fails (no WebGL, an old browser) leaves the rest running, and under
// prefers-reduced-motion the effects hold still and the ledger never changes.
//
// Nothing below makes a request. The page is dark whatever the visitor's
// setting, so every effect is told `theme: "dark"` rather than reading the
// <html> element, which stays pinned to light for the demo's sake. The sign-in
// and sign-up pages use the same start(), with only the wash to start.

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

// The logo's navy and blues, with a little of the lime the buttons use.
const WASH = ["#2E63B0", "#1B4680", "#5F7A14", "#081A38"];
const STRETCH = ["#081A38", "#0F2A52", "#1B4680", "#2E63B0", "#5886CB",
                 "#B8CDEC", "#EEF3FB", "#D2F04E", "#A8C23A"];

function attempt(name, start) {
  try {
    return start();
  } catch (error) {
    if (window.console) console.warn("landing: " + name + " did not start", error);
    return null;
  }
}

// --- The example ledger ------------------------------------------------------
// Invented payments, recorded by the bursar and checked against the bank. The
// first four are in the page; these join at the top one at a time.
const MORE = [
  { name: "Aisha Bello", cls: "SS 2C", how: "Bank transfer", paid: 95000, fee: 95000 },
  { name: "Ejiro Akpobome", cls: "Primary 6", how: "Cash at the office", paid: 30000, fee: 62000 },
  { name: "Daniel Uwejeyah", cls: "JSS 1B", how: "Bank transfer", paid: 85000, fee: 85000 },
  { name: "Chidinma Okoro", cls: "JSS 2B", how: "Bank transfer", paid: 35000, fee: 35000 },
];

function naira(n) {
  return "₦" + n.toLocaleString("en-NG");
}

function ledgerRow(entry, time) {
  const row = document.createElement("div");
  row.className = "sc-ledger-row sc-fresh";
  const cell = (cls, text) => {
    const p = document.createElement("p");
    p.className = cls;
    p.textContent = text;
    return p;
  };
  row.append(
    cell("sc-who", entry.name),
    cell("sc-amt sc-mono", naira(entry.paid)),
    cell("sc-meta sc-mono", entry.cls + " · " + entry.how + " · " + time),
  );
  const status = document.createElement("p");
  status.className = "sc-amt";
  const pill = document.createElement("span");
  const full = entry.paid >= entry.fee;
  pill.className = "sc-pill " + (full ? "sc-pill-paid" : "sc-pill-part");
  pill.textContent = full ? "Paid in full" : "Bal " + naira(entry.fee - entry.paid);
  status.append(pill);
  row.append(status);
  return row;
}

function startLedger() {
  const rows = document.querySelector("[data-sc-ledger-rows]");
  const total = document.querySelector("[data-sc-ledger-total]");
  if (!rows || !total || reduceMotion.matches) return;
  let sum = 247000;
  let next = 0;
  let minutes = 9 * 60 + 42;
  // One morning's worth: each example payment arrives once, then the ledger
  // holds still, so the total never climbs into figures no school would see.
  const timer = window.setInterval(() => {
    if (document.hidden) return;
    if (next >= MORE.length) { window.clearInterval(timer); return; }
    const entry = MORE[next % MORE.length];
    next += 1;
    minutes += 3 + (next % 4);
    const time = String(Math.floor(minutes / 60) % 24).padStart(2, "0") + ":" +
                 String(minutes % 60).padStart(2, "0");
    rows.prepend(ledgerRow(entry, time));
    while (rows.children.length > 4) rows.lastElementChild.remove();
    sum += entry.paid;
    total.textContent = naira(sum);
  }, 3400);
}

// --- Start everything ------------------------------------------------------------
export function start(engines) {
  const wash = document.querySelector("[data-sc-wash] canvas");
  if (wash && engines.shaderGradient) {
    attempt("colour wash", () => engines.shaderGradient.createShaderGradient(wash, {
      colors: WASH, speed: 0.12, grain: 0.25, interactive: true, theme: "dark",
    }));
  }

  const typewriter = document.querySelector("[data-sc-typewriter]");
  if (typewriter && engines.typewriter) {
    attempt("typewriter", () => {
      const text = typewriter.getAttribute("data-text");
      typewriter.replaceChildren();
      typewriter.classList.add("sc-typewriter-live");
      return engines.typewriter.createTypewriter(typewriter, {
        text, color: "#2E63B0", speed: 16, columns: 30, hold: 3200,
        label: "SCHOOLCORD", sound: false,
      });
    });
  }

  const iso = document.querySelector("[data-sc-iso]");
  if (iso && engines.isoLattice) {
    attempt("isometric figure", () => engines.isoLattice.createIsoLattice(iso, {
      shapes: ["steps", "pillars", "cube"], interval: 4, color: "#D2F04E",
      glow: 0.55, size: 0.62, theme: "dark",
    }));
  }

  const orb = document.querySelector("[data-sc-orb]");
  if (orb && engines.liveOrb) {
    attempt("orb", () => engines.liveOrb.createLiveOrb(orb, {
      variant: "custom", color: "#D2F04E", eyeColor: "#0F2A52", theme: "dark",
    }));
  }

  const stretch = document.querySelector("[data-sc-stretch]");
  if (stretch && engines.stretchyFooter) {
    attempt("footer stretch", () => engines.stretchyFooter.createStretchyFooter(
      { canvas: stretch, scroller: window, content: "[data-stretchy-page]" },
      { colors: STRETCH, glow: 0.2 },
    ));
  }

  startLedger();
}
