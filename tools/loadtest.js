/* Do the frontend scripts actually LOAD?
 *
 *     node tools/loadtest.js
 *
 * `node --check` only parses. It cannot catch the failure that matters here: a
 * top-level statement that throws at load time. That already happened once --
 * ROUTES named a function defined in a file loaded later, which threw a
 * ReferenceError while app.js was still evaluating, so app.js never finished
 * and EVERY button on the page went dead at once, nav and url bar included.
 * Syntax was perfect throughout.
 *
 * So this runs the scripts the way a browser does, against a DOM stub, and
 * fails loudly if any of them throws on the way in.
 *
 * It also checks that every id the scripts ask for via $('...') exists either
 * in index.html or in markup the scripts build themselves -- a missing one
 * means $() returns null and the first property access on it throws, which is
 * the same catastrophe by a different route.
 */
const fs = require('fs');
const path = require('path');

const root = process.argv[2] || path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));

const el = (id) => ({
  id, style: {}, dataset: {}, checked: true, value: 'firstletter',
  innerHTML: '', textContent: '', hidden: false, placeholder: '', disabled: false,
  classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
  addEventListener() {}, removeEventListener() {}, setAttribute() {},
  getAttribute() { return ''; }, appendChild() {}, focus() {},
  showModal() {}, close() {}, closest() { return null; },
  insertAdjacentHTML() {},
  querySelector() { return null; }, querySelectorAll() { return []; },
  getBoundingClientRect() { return { height: 50 }; },
});

const cache = new Map();
global.document = {
  getElementById: (id) => {
    if (!cache.has(id)) cache.set(id, ids.has(id) ? el(id) : null);
    return cache.get(id);
  },
  querySelector: () => el('stub'),
  querySelectorAll: () => [],
  createElement: () => el('new'),
  addEventListener() {},
  documentElement: { style: { setProperty() {} } },
  body: { classList: { toggle() {}, add() {}, remove() {} } },
};
global.window = {
  addEventListener() {}, scrollTo() {}, scrollY: 0,
  location: { pathname: '/' }, matchMedia: () => ({ matches: false }),
};
global.history = { pushState() {}, replaceState() {}, state: null, back() {} };
global.fetch = () => Promise.reject(new Error('no network in loadtest'));
global.requestAnimationFrame = () => {};
global.localStorage = { getItem: () => null, setItem() {} };
global.navigator = { userAgent: 'node' };
global.CSS = { escape: (s) => String(s) };

// Read the script tags out of index.html rather than hardcoding a list -- a new
// one added there is then covered automatically instead of silently untested.
const files = [...html.matchAll(/<script src="\/static\/([^"]+)"><\/script>/g)]
  .map((m) => 'static/' + m[1]);
if (!files.length) {
  console.log('  no <script src> tags found in index.html');
  process.exit(1);
}

// Classic <script> tags share one global lexical scope, so a `const` in app.js
// is visible to similar.js. Running each file in its own new Function() would
// break that and report failures the browser would never see -- so run them
// CUMULATIVELY in one scope. The first file that fails is the one at fault.
const srcs = files.map((f) => fs.readFileSync(path.join(root, f), 'utf8'));
for (let i = 0; i < files.length; i++) {
  try {
    new Function(srcs.slice(0, i + 1).join('\n;\n'))();
    console.log('  OK    ' + files[i]);
  } catch (e) {
    console.log('  FAIL  ' + files[i]);
    console.log('        ' + e.constructor.name + ': ' + e.message);
    process.exitCode = 1;
    break;
  }
}

const asked = new Set();
const built = new Set(ids);
for (let i = 0; i < files.length; i++) {
  for (const m of srcs[i].matchAll(/\$\('([\w-]+)'\)/g)) asked.add(m[1]);
  for (const m of srcs[i].matchAll(/id="([\w-]+)"/g)) built.add(m[1]);
}
const missing = [...asked].filter((id) => !built.has(id));
console.log(missing.length ? '  MISSING IDS: ' + missing.join(', ')
                           : '  OK    every $(id) referenced exists');
if (missing.length) process.exitCode = 1;
