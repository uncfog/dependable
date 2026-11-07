/* WCAG contrast audit for the design tokens.
 *
 * The dark theme is a second set of values behind the same token names, so a
 * regression there is invisible in review -- nothing in a template changes.
 * This reads the real values out of assets/app.css and checks every pair the
 * UI actually renders, in both themes.
 *
 *   node scripts/check-contrast.mjs      (npm run check:contrast)
 *
 * Exits non-zero on any failure, so it can gate a build.
 */
import { readFileSync } from 'node:fs';

const CSS = readFileSync(new URL('../assets/app.css', import.meta.url), 'utf8');

/* --- Token extraction ---------------------------------------------------- */

function block(source, opener) {
  const start = source.indexOf(opener);
  if (start === -1) throw new Error(`could not find "${opener}" in app.css`);
  let depth = 0;
  for (let i = start; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i);
  }
  throw new Error(`unbalanced braces after "${opener}"`);
}

function tokens(text) {
  const found = {};
  // Hex only: rgb()/shadow tokens are not colour pairs we contrast-check.
  for (const [, name, value] of text.matchAll(/--color-([\w-]+):\s*(#[0-9a-fA-F]{3,8})\s*;/g)) {
    found[name] = value;
  }
  return found;
}

const light = tokens(block(CSS, '@theme {'));
const dark = { ...light, ...tokens(block(CSS, ':root[data-theme="dark"] {')) };

/* --- WCAG 2.1 relative luminance ----------------------------------------- */

function channel(v) {
  const c = v / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

function luminance(hex) {
  let h = hex.slice(1);
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function ratio(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
}

/* --- The pairs the UI actually renders ------------------------------------
 * `min` is the threshold that applies to that pair: 4.5 for body text, 3.0 for
 * large text (the 3xl dashboard figures), non-text boundaries, and the focus
 * ring. `white` is not a token; it is literal in both themes.
 */
const WHITE = '#ffffff';

const PAIRS = [
  // Body text on the two planes it sits on.
  ['ink-900', 'surface', 4.5, 'body text on a card'],
  ['ink-900', 'ink-50', 4.5, 'body text on the page ground'],
  ['ink-700', 'surface', 4.5, 'secondary text on a card'],
  ['ink-600', 'surface', 4.5, 'muted text on a card'],
  ['ink-600', 'ink-50', 4.5, 'muted text on the page ground'],
  ['ink-500', 'surface', 4.5, 'hint text on a card'],
  ['ink-500', 'ink-50', 4.5, 'hint text on the page ground (.table-head)'],
  ['ink-400', 'surface', 3.0, 'placeholder / disabled glyph'],
  ['ink-400', 'ink-50', 3.0, 'placeholder on the page ground'],

  // Headings and links.
  ['brand-900', 'surface', 4.5, 'heading on a card'],
  ['brand-900', 'ink-50', 4.5, 'heading on the page ground'],
  ['brand-800', 'surface', 4.5, 'link / .btn-secondary label'],
  ['brand-700', 'surface', 4.5, 'inline link'],
  ['brand-800', 'brand-50', 4.5, 'step number on its brand chip'],

  // The primary button, at all three of its states.
  ['on-accent', 'accent', 4.5, '.btn-primary label'],
  ['on-accent', 'accent-hover', 4.5, '.btn-primary label, hover'],
  ['on-accent', 'accent-active', 4.5, '.btn-primary label, pressed'],

  // Lime: the one highlight on a screen. Dark ink on it only. The active
  // sidebar row and the header band's action are the two places it appears.
  ['on-lime', 'lime', 4.5, 'lime action label and active sidebar row'],
  ['lime', 'panel', 3.0, 'lime rail and accent on the blue band (non-text)'],

  // Payment status pills -- the soft/strong pair is the whole contract.
  ['paid-strong', 'paid-soft', 4.5, 'Paid pill'],
  ['partial-strong', 'partial-soft', 4.5, 'Part paid pill'],
  ['unpaid-strong', 'unpaid-soft', 4.5, 'Unpaid pill'],
  ['overdue-strong', 'overdue-soft', 4.5, 'Overdue pill'],

  // ...and the same four as figures on a card, where they are large text.
  ['paid-strong', 'surface', 3.0, 'Paid figure on a card'],
  ['overdue-strong', 'surface', 3.0, 'Outstanding figure on a card'],
  ['overdue', 'surface', 3.0, 'required-field marker on a card'],

  // The navy panel, which stays dark in both themes.
  ['panel-fg', 'panel', 4.5, 'sidebar product name'],
  // Body copy on any navy surface -- the hero subheadline, the problem cards,
  // the auth visual panel. This pair is what `text-brand-100` used to be, and
  // that was 1.01:1 in dark mode.
  ['panel-copy', 'panel', 4.5, 'body copy on the navy plane'],
  ['panel-muted', 'panel', 4.5, 'sidebar nav link'],
  ['panel-subtle', 'panel', 3.0, 'sidebar section label (uppercase, small)'],

  // Boundaries and the focus ring: non-text, so 3:1.
  ['ink-200', 'surface', 1.0, 'card border (decorative)'],
  ['brand-500', 'surface', 3.0, 'focus ring on a card'],
  ['brand-500', 'ink-50', 3.0, 'focus ring on the page ground'],

  // Icon tiles: a glyph on its own tinted square. Non-text graphics, so the
  // threshold is 3:1 (WCAG 1.4.11) rather than 4.5. Every accent is checked
  // in both themes because the dark theme re-points both ends of each pair --
  // the tile darkens and the glyph brightens -- and getting one of those two
  // moves wrong is exactly the kind of regression nothing in a template shows.
  ...['blue', 'indigo', 'violet', 'teal', 'emerald', 'amber', 'rose'].map(
    (hue) => [`acc-${hue}`, `acc-${hue}-soft`, 3.0, `icon-tile accent-${hue}`]
  ),
];

const LITERAL = [
  [WHITE, 'overdue', 4.5, '.btn-danger label'],
];

/* --- Run ------------------------------------------------------------------ */

let failures = 0;

for (const [themeName, theme] of [['light', light], ['dark', dark]]) {
  console.log(`\n${themeName.toUpperCase()}`);
  const rows = [
    ...PAIRS.map(([fg, bg, min, what]) => [theme[fg], theme[bg], min, what, `${fg} on ${bg}`]),
    ...LITERAL.map(([fg, bg, min, what]) => [fg, theme[bg], min, what, `white on ${bg}`]),
  ];

  for (const [fg, bg, min, what, label] of rows) {
    if (!fg || !bg) {
      console.log(`  MISSING  ${label} -- token not defined`);
      failures++;
      continue;
    }
    const r = ratio(fg, bg);
    const ok = r >= min;
    if (!ok) failures++;
    console.log(
      `  ${ok ? 'pass' : 'FAIL'}  ${r.toFixed(2).padStart(6)}:1  (min ${min.toFixed(1)})  ` +
      `${label.padEnd(34)} ${what}`
    );
  }
}

console.log(
  failures === 0
    ? '\nAll token pairs meet their threshold in both themes.'
    : `\n${failures} pair(s) below threshold.`
);
process.exit(failures === 0 ? 0 : 1);
