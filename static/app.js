'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// Two tiers. The basic four plus length are what an actual choice gets made on;
// raag and writer are real filters that are almost never the question, and
// leaving them in the open turns the panel into a wall you scroll past. Behind
// a disclosure they cost nothing until wanted.
//
// `length` is a BASIC filter and a server-defined one -- its bands and their
// boundaries come from /api/filters, so the UI never hardcodes where "Short"
// ends. Multi-select makes one control cover caps, minimums and bands alike:
// short+medium is "at most 16", medium+long is "at least 9".
const BASIC_KINDS = ['status', 'rarity', 'genre', 'speed', 'length'];
const ADVANCED_KINDS = ['raag', 'writer'];
const FILTER_KINDS = [...BASIC_KINDS, ...ADVANCED_KINDS];
// mode must match the <option selected> in index.html
const state = { filters: {}, options: {}, q: '', sort: 'id', mode: 'firstletter' };

// A token counts as a word only if it holds a Gurmukhi letter -- ॥, ੧੨੩ and
// ॥ਰਹਾਉ॥ markers aren't counted in first_letters, so they must not shift the
// alignment between letters and words.
const GURMUKHI_LETTER = /[ਅ-ੜੲੳ]/;

// Split a query the same way the server does -- bare words are independent
// keywords, anything double-quoted is one literal phrase. Must stay in step
// with keywords() in api.py or the highlight won't match what was searched.
function terms(q) {
  const out = [];
  const re = /"([^"]*)"|(\S+)/g;
  let m;
  while ((m = re.exec(q || '')) !== null) {
    const t = (m[1] !== undefined ? m[1] : m[2].replace(/"/g, '')).trim();
    if (t) out.push(t);
  }
  return out;
}

// Wrap every occurrence of every keyword.
// Spans are collected then merged before wrapping: searching "name names"
// finds overlapping hits, and emitting those directly would nest <mark> tags
// and produce broken markup.
function mark(text, needle) {
  const s = String(text ?? '');
  const list = terms(needle);
  if (!list.length) return esc(s);

  const lower = s.toLowerCase();
  const spans = [];
  for (const t of list) {
    const find = t.toLowerCase();
    for (let i = lower.indexOf(find); i >= 0; i = lower.indexOf(find, i + 1)) {
      spans.push([i, i + find.length]);
    }
  }
  if (!spans.length) return esc(s);

  spans.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged = [spans[0]];
  for (const [a, b] of spans.slice(1)) {
    const last = merged[merged.length - 1];
    if (a <= last[1]) last[1] = Math.max(last[1], b);
    else merged.push([a, b]);
  }

  let out = '', from = 0;
  for (const [a, b] of merged) {
    out += esc(s.slice(from, a)) + '<mark>' + esc(s.slice(a, b)) + '</mark>';
    from = b;
  }
  return out + esc(s.slice(from));
}

// Highlight the WORDS whose first letters matched, the way STTM does.
// first_letters has exactly one character per word, so a hit at index i in the
// letter string means words i .. i+len-1 are the ones that matched.
function markLetters(gurmukhi, letters, q) {
  const s = String(gurmukhi ?? '');
  if (!q || !letters) return esc(s);

  const strict = /[A-Z]/.test(q);              // same smart-case rule as the server
  const hay = strict ? letters : letters.toLowerCase();
  const needle = strict ? q : q.toLowerCase();

  const spans = [];
  for (let i = hay.indexOf(needle); i >= 0; i = hay.indexOf(needle, i + 1)) {
    spans.push([i, i + needle.length]);
  }
  if (!spans.length) return esc(s);

  let word = -1;
  return s.split(/(\s+)/).map((tok) => {
    if (!tok.trim() || !GURMUKHI_LETTER.test(tok)) return esc(tok);
    word += 1;
    const hit = spans.some(([a, b]) => word >= a && word < b);
    return hit ? `<mark>${esc(tok)}</mark>` : esc(tok);
  }).join('');
}

// pick the right highlighter for the active search mode
function hi(text, letters) {
  return state.mode === 'firstletter'
    ? markLetters(text, letters, state.q)
    : mark(text, state.q);
}

// Notion's colours, so the app reads the same as the table it replaces
const TAG_COLOUR = {
  'Ready': 'green', 'In Progress': 'orange', 'Heard': 'gray',
  'Common': 'green', 'Uncommon': 'gray',
  'AKJ': 'blue', 'Raag': 'green', 'General': 'gray',
  'Slow': 'orange', 'Medium': 'blue', 'Fast': 'green',
  'Not chosen': 'dim',
};
const tag = (v) => v == null || v === ''
  ? '' : `<span class="tag t-${TAG_COLOUR[v] || 'gray'}">${esc(v)}</span>`;
const tagList = (vals) => (vals || []).map(tag).join('');

function toast(msg, isError) {
  const t = $('toast');
  t.textContent = msg;
  t.className = isError ? 'error' : '';
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 2600);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail;
    try { detail = (await res.json()).detail; } catch { detail = res.statusText; }
    throw Object.assign(new Error(typeof detail === 'string' ? detail : detail?.message),
                        { detail, status: res.status });
  }
  return res.json();
}

const json = (method, body) => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

const VIEWS = ['list', 'detail', 'deck', 'saved', 'learn', 'practice', 'history', 'add',
               'similar', 'settings', 'status'];

// The deck sizes itself to the window, so it needs the header's real height.
// Hardcoding it would be wrong on one screen or the other: the nav wraps onto a
// second row when it stops fitting, which nearly doubles it.
function measureHeader() {
  const h = document.querySelector('header').getBoundingClientRect().height;
  document.documentElement.style.setProperty('--header-h', h + 'px');
}

function show(view) {
  for (const v of VIEWS) $('view-' + v).hidden = v !== view;
  for (const [btn, v] of [['nav-list', 'list'], ['nav-deck', 'deck'],
                          ['nav-saved', 'saved'], ['nav-learn', 'learn'],
                          ['nav-history', 'history'], ['nav-add', 'add'],
                          ['nav-settings', 'settings']])
    // the control panel is reached through Settings and has no button of its
    // own, so the gear stays lit while you are in there
    $(btn).classList.toggle('active', view === v
                            || (v === 'settings' && view === 'status'));
  // stops the page itself scrolling behind the deck -- the card scrolls instead
  document.body.classList.toggle('deck-view', view === 'deck');
  measureHeader();          // the deck sizes to it, and the detail bar sticks below it
  show.current = view;
}

// ---------- filters ----------

// One chip renderer for both the library and the deck. `filters` is the object
// the chips mutate, so each view keeps its own selection -- narrowing the
// library must not silently change what the deck is drawing from.
// An option is either a bare string, or {value,label,count} for kinds where
// what you post back isn't what you read -- length posts "short" and reads
// "Short 1–8". Both shapes go through one renderer so no kind needs its own.
const optValue = (v) => (typeof v === 'string' ? v : v.value);
const optLabel = (v) => (typeof v === 'string' ? v : v.label);

function chipGroup(kinds, filters) {
  return kinds.map((kind) => {
    const vals = state.options[kind] || [];
    if (!vals.length) return '';
    return `<div class="chips"><b>${kind}</b>` + vals.map((v) => {
      const val = optValue(v);
      const on = (filters[kind] || []).includes(val) ? ' on' : '';
      const n = (v && v.count != null) ? `<span class="chip-n">${v.count}</span>` : '';
      return `<button class="chip${on}" data-kind="${kind}"
                data-val="${esc(val)}">${esc(optLabel(v))}${n}</button>`;
    }).join('') + '</div>';
  }).join('');
}

function renderChips(container, filters, onChange) {
  // If an advanced filter is active it must not also be hidden -- a raag set
  // last week silently narrowing today's results, with nothing on screen
  // explaining why, is the worst kind of bug: everything looks correct.
  const advN = ADVANCED_KINDS.reduce((n, k) => n + (filters[k] || []).length, 0);
  $(container).innerHTML = chipGroup(BASIC_KINDS, filters) + `
    <details class="adv"${advN ? ' open' : ''}>
      <summary>Advanced filters${advN ? `<span class="fcount">${advN}</span>` : ''}</summary>
      ${chipGroup(ADVANCED_KINDS, filters)}
    </details>`;

  $(container).onclick = (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    const { kind, val } = chip.dataset;
    const set = filters[kind] || (filters[kind] = []);
    const i = set.indexOf(val);
    if (i < 0) set.push(val); else set.splice(i, 1);
    chip.classList.toggle('on', i < 0);
    // Repaint the advanced badge without rebuilding the panel, which would
    // collapse the disclosure the moment you used it.
    const badge = $(container).querySelector('.adv > summary .fcount');
    const n = ADVANCED_KINDS.reduce((a, k) => a + (filters[k] || []).length, 0);
    if (badge) badge.textContent = n || '';
    else if (n) $(container).querySelector('.adv > summary')
      .insertAdjacentHTML('beforeend', `<span class="fcount">${n}</span>`);
    onChange();
  };
}

// how many filter values are active, for the collapsed summary
function countFilters(filters) {
  return Object.values(filters).reduce((n, v) => n + v.length, 0);
}

function filterParams(filters, p = new URLSearchParams()) {
  for (const kind of FILTER_KINDS)
    for (const v of filters[kind] || []) p.append(kind, v);
  return p;
}

// keeps the collapsed "Filters" summary honest about what's hidden inside it.
// Defaults are the library's, so the three panels share one implementation
// without every existing caller having to name them.
function paintFilterCount(badgeId = 'lib-filter-count', filters = state.filters,
                          clearId = 'f-clear') {
  const n = countFilters(filters);
  $(badgeId).textContent = n ? String(n) : '';
  if (clearId && $(clearId)) $(clearId).disabled = !n;
}

async function loadFilters() {
  state.options = await api('/api/filters');
  renderChips('filter-groups', state.filters, () => { paintFilterCount(); loadList(); });
  renderChips('deck-filter-groups', deck.filters, () => { deck.cards = []; loadDeck(); });
  paintFilterCount();
}

// ---------- library list ----------

function emptyState() {
  if (!state.q) return '<p class="muted">Nothing matches these filters.</p>';
  return `<p class="muted">No shabad in <b>your library</b> matches
      &ldquo;${esc(state.q)}&rdquo;.</p>
    <button id="go-search-banidb" class="secondary">Search all of BaniDB for it &rarr;</button>`;
}

async function loadList() {
  const p = new URLSearchParams();
  if (state.q) { p.set('q', state.q); p.set('mode', state.mode); }
  p.set('sort', state.sort);
  filterParams(state.filters, p);

  const data = await api('/api/shabads?' + p);
  listLoaded = true;
  $('count').textContent = `${data.count} of ${state.options.total}`;

  $('list').innerHTML = data.shabads.length ? data.shabads.map((s) => `
    <div class="card" data-id="${s.id}"${
      // A search hit on an inner line should open there, highlighted, not at
      // the line the shabad happens to be filed under. Only when the match IS
      // a different line -- otherwise the normal blue highlight already says it.
      s.match && s.match.line_no !== s.source_line_no
        ? ` data-line="${s.match.line_id}"` : ''}>
      <div class="card-main">
        ${s.match ? `
        <div class="gurmukhi">${hi(s.match.gurmukhi, s.match.first_letters)}</div>
        <div class="en">${state.mode === 'firstletter'
          ? esc(s.match.translation_en || '') : mark(s.match.translation_en || '', state.q)}</div>
        ${s.match.line_no !== s.source_line_no ? `
        <div class="in-shabad">
          <span class="no">line ${s.match.line_no} of</span>
          <div class="in-shabad-line">${esc(s.source_line)}</div>
        </div>` : ''}`
        : `
        <div class="gurmukhi">${esc(s.source_line)}</div>
        <div class="en">${esc(s.source_translation || '')}</div>`}
      </div>
      <div class="card-side">
        <div class="tags-row">
          ${tag(s.status)}${tag(s.rarity)}${tagList(s.tags.speed)}${tagList(s.tags.genre)}
        </div>
        <div class="meta">
          <span>ang ${esc(s.ang)}</span>
          <span>${esc((s.raag_en || '').replace(/^Raag /, ''))}</span>
          <span>${esc(s.writer)}</span>
          <span>${s.line_count} lines</span>
        </div>
      </div>
      <button class="heart${s.shortlisted ? ' on' : ''}" data-id="${s.id}"
              title="${s.shortlisted ? 'Remove from Interested' : 'Add to Interested'}"
              aria-label="Toggle Interested">${s.shortlisted ? '&hearts;' : '&#9825;'}</button>
    </div>`).join('') : emptyState();

  $('list').onclick = (e) => {
    const heart = e.target.closest('.heart');
    if (heart) { e.stopPropagation(); toggleShortlist(heart); return; }
    const card = e.target.closest('.card');
    // data-line is only set when the hit was on some other line of the shabad
    if (card) go('detail', { id: card.dataset.id, line: card.dataset.line });
  };

  const jump = $('go-search-banidb');
  if (jump) jump.onclick = () => {
    // an empty library result and a broken search look identical -- make the
    // next step obvious instead of leaving a dead end
    show('add');
    $('s-q').value = state.q;
    // "English Translation" has no equivalent when searching BaniDB, so a word
    // search carries over as a Gurmukhi word search
    $('s-mode').value = state.mode === 'firstletter' ? 'firstletter' : 'fullword';
    runSearch();
  };
}

// ---------- detail + edit ----------

function checkboxes(kind, selected) {
  // "Not chosen" is what you get by picking nothing -- offering it as something
  // to tick is meaningless, and it sorted into the middle of the real options.
  const all = new Set([...(state.options[kind] || []), ...(selected || [])]);
  all.delete('Not chosen');
  return [...all].map((v) => `
    <label class="chip ${selected.includes(v) ? 'on' : ''}">
      <input type="checkbox" name="${kind}" value="${esc(v)}" hidden
             ${selected.includes(v) ? 'checked' : ''}> ${esc(v)}
    </label>`).join('');
}

// Deep link into SikhiToTheMax. Verified against banidb.db: `id` is the
// ShabadID and `highlight` is the VerseID of the line to jump to -- verse 1799
// really does belong to shabad 130 in their example url.
function sttmUrl(s, mine) {
  if (!s.banidb_shabad_id) return null;
  const p = new URLSearchParams({
    id: s.banidb_shabad_id, type: '0', source: 'all',
  });
  if (mine.first_letters) p.set('q', mine.first_letters);
  if (mine.banidb_verse_id) p.set('highlight', mine.banidb_verse_id);
  return 'https://sikhitothemax.org/shabad?' + p;
}

// Read-only view. Everything editable now lives in the dialog, so opening a
// shabad goes straight to the Gurbani instead of a form.
function detailBody(s, focusLineId) {
  return `
    <div class="hero">
      <div class="tags-row">
        ${tag(s.status)}${tag(s.rarity)}${tagList(s.tags.speed)}${tagList(s.tags.genre)}
      </div>
      <div class="meta">
        <span>ang ${esc(s.ang)}</span><span>${esc(s.raag_en)}</span>
        <span>${esc(s.writer)}</span><span>${esc(s.source_en)}</span>
        <span>${s.lines.length} lines</span>
        ${indexBadge(s.indexing)}
      </div>
      ${s.notes ? `<div class="hero-notes">${esc(s.notes)}</div>` : ''}
    </div>

    <div class="all-lines">
      ${s.lines.map((l) => {
        // blue = the line this shabad is filed under. yellow = the line you
        // arrived for. They are usually different, and both are worth seeing.
        const mine = l.line_no === s.source_line_no;
        const came = focusLineId && l.id === focusLineId;
        return `
        <div class="line tappable ${mine ? 'mine' : ''} ${came ? 'came-for' : ''}"
             data-line-id="${l.id}" role="button" tabindex="0"
             title="Find lines with a similar meaning"
             ${came ? 'id="focus-line"' : (!focusLineId && mine ? 'id="focus-line"' : '')}>
          <div class="no">${l.line_no}</div>
          <div class="gurmukhi">${esc(l.gurmukhi)}</div>
          ${l.translation_en ? `<div class="en">${esc(l.translation_en)}</div>` : ''}
          ${l.teeka_pa ? `<div class="pa">${esc(l.teeka_pa)}</div>` : ''}
        </div>`;
      }).join('')}
    </div>`;
}

let current = null;           // the shabad on screen in the detail view

// ---------- router ----------
//
// The browser's history IS the record of where you've been -- there is no
// second copy to keep in step. Back always means "the previous entry", so a new
// view can't be forgotten in a lookup table and silently send you to the wrong
// place, which is how Full Search and Learn both ended up returning to the
// library. Real paths, not #fragments, so urls are shareable and reloadable.
const ROUTES = {
  list:     { path: '/',           load: loadList },
  deck:     { path: '/deck',       load: loadDeck },   // always a fresh shuffle
  saved:    { path: '/interested', load: loadSaved },
  learn:    { path: '/learn',      load: loadLearn },
  history:  { path: '/history',    load: loadHistory },
  add:      { path: '/search',     load: () => $('s-q').focus() },
  // Anything defined in similar.js or settings.js MUST be wrapped in an arrow.
  // Those files load after this one, so naming the function bare here reads an
  // undefined variable while this object literal is being built -- that throws,
  // app.js never finishes, and every button on the page goes dead at once.
  // Inside an arrow the name is looked up when the route runs, by which time
  // both files have loaded.
  settings: { path: '/settings',   load: () => loadSettings() },
  status:   { path: '/status',     load: () => loadStatus() },
  practice: { path: '/practice',   load: null },       // only entered via Start
  detail:   { path: null,          load: null },       // /shabad/<id>
  similar:  { path: null,          load: (st) => loadSimilar(st.id) },  // /similar/<line_id>
};

// Views addressed by an id rather than a fixed path. Keeping them in one place
// means pathFor and parsePath can't disagree about the url shape.
const ID_ROUTES = { detail: '/shabad/', similar: '/similar/' };

// Views that put themselves somewhere specific -- a shabad opens on the line
// you came for, not at the top. Everything else starts at the top.
const SELF_SCROLL = new Set(['detail']);

let pushes = 0;               // so Back on a deep-linked page doesn't leave the app

// ?line= carries which tuk to land on, so the url stays shareable and survives
// a reload -- history state alone would be lost the moment the page reloads.
const pathFor = (view, id, line) =>
  (ID_ROUTES[view] ? ID_ROUTES[view] + id + (line ? '?line=' + line : '')
                   : ROUTES[view].path);

function parsePath(full) {
  const [p, qs] = String(full).split('?');
  const line = new URLSearchParams(qs || '').get('line');
  for (const [view, prefix] of Object.entries(ID_ROUTES)) {
    const m = p.match(new RegExp('^' + prefix + '(\\d+)/?$'));
    if (m) return { view, id: m[1], ...(line ? { line } : {}) };
  }
  const hit = Object.keys(ROUTES).find((k) => ROUTES[k].path === p.replace(/\/$/, '') || ROUTES[k].path === p);
  return { view: hit || 'list' };
}

// remember the scroll position of the page being left, so Back restores it
function stashScroll() {
  history.replaceState({ ...(history.state || {}), scrollY: window.scrollY }, '');
}

function go(view, opts = {}) {
  stashScroll();
  const st = { view, ...opts };
  history.pushState(st, '', pathFor(view, opts.id, opts.line));
  pushes += 1;
  render(st);
}

// Whether #list currently matches the server. Going Back to a list we already
// have is then a repaint of nothing at all -- no fetch, no re-render, no jump
// to the top and back down. Anything that changes a shabad clears it.
let listLoaded = false;

async function render(st, viaPop) {
  const view = ROUTES[st.view] ? st.view : 'list';
  const y = st.scrollY;
  show(view);

  // Returning to a list that is still good: put the page back BEFORE any await,
  // so the restored position is the first thing painted rather than a correction
  // half a second later.
  const reuse = view === 'list' && viaPop && listLoaded;
  if (reuse && y != null) window.scrollTo(0, y);

  if (view === 'detail') {
    // navigating history isn't opening it afresh, so don't log another visit
    await openDetail(st.id, { silent: viaPop, focusLine: st.line,
                              keepScroll: y != null });
  } else if (!reuse && ROUTES[view].load) {
    await ROUTES[view].load(st);        // id-addressed views read st.id
  }

  // A history entry carries where you were; honour it. Without one, a view that
  // positions itself (a shabad opening on its line) is left alone, and
  // everything else starts at the top.
  if (y != null) window.scrollTo(0, y);
  else if (!SELF_SCROLL.has(view)) window.scrollTo(0, 0);
}

window.addEventListener('popstate', (e) => {
  pushes = Math.max(0, pushes - 1);
  render(e.state || parsePath(location.pathname + location.search), true);
});

function goBack() {
  if (pushes > 0) history.back();
  else go('list');            // landed here directly; Back should stay in the app
}

async function openDetail(id, opts = {}) {
  const s = await api('/api/shabads/' + id);
  current = s;
  show('detail');

  // The line asked for, if any -- arriving from Similar you want the tuk that
  // matched, which is usually not the line the shabad is filed under.
  const focus = Number(opts.focusLine) || null;
  $('detail').innerHTML = detailBody(s, focus);

  const mine = s.lines.find((l) => l.line_no === s.source_line_no) || {};
  const url = sttmUrl(s, mine);
  $('d-sttm').href = url || '#';
  $('d-sttm').hidden = !url;               // manual entries have no BaniDB id
  paintHeart($('d-heart'), s.shortlisted);
  $('d-heart').dataset.id = s.id;
  paintLearnBtn(s.learning, s.learning_stage);
  watchIndexing();          // no-op unless a run is actually live

  // Land on the line worth reading -- the one you arrived for, or failing that
  // the line the shabad is filed under, never the raag header. Centred, so
  // there's context above and below.
  //
  // Skipped when returning through history: there the browser position is the
  // right answer, and jumping to the line would undo the restore.
  if (!opts.keepScroll) {
    requestAnimationFrame(() => {
      const focus = $('focus-line');
      if (focus) focus.scrollIntoView({ block: 'center' });
      else window.scrollTo(0, 0);
    });
  }

  // fire and forget -- a failed log must never stop you reading the shabad
  if (!opts.silent) api('/api/history', json('POST', { shabad_id: Number(id) })).catch(() => {});
}

// Whether the derived layer exists for this shabad -- the LLM summary and its
// embedding (CLAUDE.md §6/§7). It sits with ang/raag/writer rather than among
// the buttons: it describes the shabad, it isn't something you can press.
// A traffic light for the derived layer (CLAUDE.md §6/§7): red nothing, amber
// partly, green every enabled model has every line. Pressable, because with
// more than one model the single word can no longer tell the whole story --
// the breakdown lives in a dialog rather than a tooltip a phone can't show.
const IX_LABEL = {
  done: 'Indexed', part: 'Partly indexed', none: 'Not indexed', running: 'Indexing…',
};

function indexBadge(ix) {
  if (!ix || !ix.total) return '';
  return `<button class="idx ${ix.state}" data-idx-badge
            title="Which models have indexed this shabad">${IX_LABEL[ix.state]}</button>`;
}

function jobLine(j) {
  const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
  const what = j.phase === 'embed' ? 'building vectors' : 'writing summaries';
  return `<div class="ix-job">
      <div class="row-between">
        <b>${esc(what)}</b>
        <span class="muted">${j.done}/${j.total}${j.spent ? ` &middot; $${j.spent.toFixed(2)}` : ''}</span>
      </div>
      <div class="ix-bar"><span style="width:${pct}%"></span></div>
    </div>`;
}

function openIndexDialog() {
  const ix = current && current.indexing;
  if (!ix) return;
  $('index-body').innerHTML = `
    ${(ix.jobs || []).length
      ? `<div class="ix-live">
           <div class="muted">Running now &mdash; this dialog updates itself.</div>
           ${ix.jobs.map(jobLine).join('')}
         </div>`
      : ''}
    <p class="muted">A line counts as done only once it has both a summary and a
      vector &mdash; a summary with no vector is invisible to search, so calling
      it indexed would be a lie in exactly the case that matters.</p>
    <div class="ix-list">
      ${ix.models.map((m) => `
        <div class="ix-row ${m.enabled ? '' : 'off'}">
          <span class="dot ${m.state}"></span>
          <div class="ix-name">
            <b>${esc(m.label)}</b>
            ${m.enabled ? '' : '<span class="muted"> &mdash; switched off</span>'}
          </div>
          <div class="ix-nums muted">
            ${Math.min(m.summarised, m.embedded)}/${m.total} lines
          </div>
        </div>`).join('')}
    </div>
    ${ix.models.some((m) => m.enabled) ? '' :
      '<p class="muted">No model is switched on, so nothing will be indexed.</p>'}`;
  // showModal() on an already-open dialog throws, and the poller repaints this
  // while it is open -- so only open it if it is closed.
  if (!$('index-dialog').open) $('index-dialog').showModal();
}

// While a run is live, re-fetch the shabad every few seconds so the badge and
// the dialog move on their own. Indexing a shabad takes about a minute, most of
// it the model loading in silence -- which is exactly when a static badge looks
// like nothing is happening at all.
let ixPoll = null;

function stopIndexPoll() {
  clearInterval(ixPoll);
  ixPoll = null;
}

function watchIndexing() {
  stopIndexPoll();
  if (!current || !current.indexing || current.indexing.state !== 'running') return;
  ixPoll = setInterval(async () => {
    if (show.current !== 'detail' || !current) return stopIndexPoll();
    try {
      const fresh = await api('/api/shabads/' + current.id);
      current.indexing = fresh.indexing;
      const badge = document.querySelector('#detail [data-idx-badge]');
      if (badge) {
        badge.className = 'idx ' + fresh.indexing.state;
        badge.textContent = IX_LABEL[fresh.indexing.state];
      }
      if ($('index-dialog').open) openIndexDialog();
      if (fresh.indexing.state !== 'running') {
        stopIndexPoll();
        toast('Indexing finished');
      }
    } catch { stopIndexPoll(); }
  }, 4000);
}

function paintLearnBtn(on, stage) {
  const b = $('d-learn');
  b.classList.toggle('on', !!on);
  b.textContent = on ? (stage || 'Learning') : 'Learn';
  b.title = on ? 'Stop memorizing this shabad' : 'Memorize this shabad';
}

function paintHeart(btn, on) {
  btn.classList.toggle('on', !!on);
  btn.innerHTML = on ? '&hearts;' : '&#9825;';
  btn.title = on ? 'Remove from Interested' : 'Add to Interested';
}

// ---------- edit dialog ----------

function openEditDialog() {
  const s = current;
  if (!s) return;
  $('edit-body').innerHTML = `
    <div class="form-grid">
      <div class="field">
        <label>Status</label>
        <select id="e-status">${['', 'Ready', 'In Progress', 'Heard']
          .map((v) => `<option ${v === (s.status || '') ? 'selected' : ''}>${v}</option>`).join('')}</select>
      </div>
      <div class="field">
        <label>Rarity</label>
        <select id="e-rarity">${['', 'Common', 'Uncommon']
          .map((v) => `<option ${v === (s.rarity || '') ? 'selected' : ''}>${v}</option>`).join('')}</select>
      </div>
      <div class="field"><label>Genre</label>
        <div class="chips" id="e-genre">${checkboxes('genre', s.tags.genre || [])}</div></div>
      <div class="field"><label>Speed</label>
        <div class="chips" id="e-speed">${checkboxes('speed', s.tags.speed || [])}</div></div>
      <div class="field wide"><label>Notes</label>
        <textarea id="e-notes" placeholder="whose tune, where you heard it, anything else"
          >${esc(s.notes || '')}</textarea></div>
    </div>`;

  // toggling a chip flips its hidden checkbox
  for (const kind of ['genre', 'speed']) {
    $('e-' + kind).onclick = (e) => {
      const label = e.target.closest('label');
      if (!label) return;
      const box = label.querySelector('input');
      box.checked = !box.checked;
      label.classList.toggle('on', box.checked);
    };
  }
  $('edit-dialog').showModal();
}

const pickedTags = (kind) =>
  [...$('edit-body').querySelectorAll(`input[name=${kind}]:checked`)].map((b) => b.value);

$('e-save').onclick = async () => {
  try {
    await api('/api/shabads/' + current.id, json('PATCH', {
      status: $('e-status').value, rarity: $('e-rarity').value,
      notes: $('e-notes').value, genre: pickedTags('genre'), speed: pickedTags('speed'),
    }));
    $('edit-dialog').close();
    toast('Saved');
    listLoaded = false;               // that card now shows the wrong tags
    await loadFilters();
    // a redraw, not a fresh open -- must not add a history row
    await openDetail(current.id, { silent: true });
  } catch (err) { toast(err.message, true); }
};

$('e-delete').onclick = async () => {
  // this metadata exists nowhere else -- CLAUDE.md §5
  if (!confirm(`Delete this shabad and all ${current.lines.length} of its lines?\n\n`
             + `Your status, rarity, tags and notes for it cannot be recovered.`)) return;
  try {
    await api('/api/shabads/' + current.id, { method: 'DELETE' });
    $('edit-dialog').close();
    toast('Deleted');
    listLoaded = false;               // that row is gone
    await loadFilters();
    go('list');                               // that row is gone; don't go back to it
  } catch (err) { toast(err.message, true); }
};

$('e-cancel').onclick = () => $('edit-dialog').close();
$('e-close').onclick = () => $('edit-dialog').close();

// ---------- swipe deck ----------

// How far a card must travel before it counts as a swipe. Below this it springs
// back, so a scroll or a misplaced tap never silently files a shabad.
const SWIPE_PX = 110;
const STACK = 3;                       // cards drawn at once, for depth

// filters are the deck's own, deliberately separate from the library's.
// history is a stack of swipes that can still be taken back.
const deck = { cards: [], remaining: 0, busy: false, filters: {}, history: [] };
const UNDO_DEPTH = 25;

function setSavedCount(n) {
  $('saved-count').textContent = n > 0 ? n : '';
}

async function refreshSavedCount() {
  try { setSavedCount((await api('/api/shortlist')).count); } catch { /* badge only */ }
}

// Toggle straight from the library, without going through the deck. Flips the
// button first and rolls back if the request fails -- a heart that waits on the
// network feels broken when you're tapping down a list.
async function toggleShortlist(btn) {
  const id = btn.dataset.id;
  const wasOn = btn.classList.contains('on');
  paintHeart(btn, !wasOn);
  try {
    const r = await api('/api/shortlist/' + id, { method: wasOn ? 'DELETE' : 'POST' });
    setSavedCount(r.shortlist_count);
    deck.cards = [];              // a shortlisted shabad is no longer deck material
    listLoaded = false;           // the heart on the list card is now stale
    if (current && String(current.id) === String(id)) current.shortlisted = !wasOn;
    // hearting from the detail view leaves the list card behind it stale

  } catch (err) {
    paintHeart(btn, wasOn);
    toast(err.message, true);
  }
}

async function loadDeck() {
  const p = filterParams(deck.filters);
  p.set('limit', '30');
  if ($('deck-include').checked) p.set('include_shortlisted', 'true');
  try {
    const d = await api('/api/deck?' + p);
    deck.cards = d.shabads;
    deck.remaining = d.remaining;
  } catch (err) {
    toast(err.message, true);
    deck.cards = [];
  }
  const n = countFilters(deck.filters);
  $('deck-filter-count').textContent = n ? `(${n} active)` : '';
  $('deck-filter-clear').disabled = !n;
  renderDeck();
}

function deckCard(s, i) {
  // The whole shabad, not just the line I know it by -- the card scrolls, and
  // renderDeck() positions it on my line so that's what's in frame first.
  const lines = (s.lines || []).map((l) => `
    <div class="dline${l.line_no === s.source_line_no ? ' mine' : ''}">
      <div class="gurmukhi">${esc(l.gurmukhi)}</div>
      ${l.translation_en ? `<div class="en">${esc(l.translation_en)}</div>` : ''}
    </div>`).join('') || '<p class="muted">No lines stored for this shabad.</p>';

  return `
    <article class="swipe-card" data-i="${i}" data-id="${s.id}">
      <div class="swipe-badge keep">Interested</div>
      <div class="swipe-badge pass">Pass</div>
      <div class="card-lines">${lines}</div>
      <div class="card-foot">
        <div class="tags-row">
          ${tag(s.status)}${tag(s.rarity)}${tagList(s.tags.speed)}${tagList(s.tags.genre)}
        </div>
        <div class="meta">
          <span>ang ${esc(s.ang)}</span>
          <span>${esc((s.raag_en || '').replace(/^Raag /, ''))}</span>
          <span>${esc(s.writer)}</span>
          <span>${s.line_count} lines</span>
        </div>
      </div>
    </article>`;
}

function renderDeck() {
  const stack = deck.cards.slice(0, STACK);
  // reversed so the top card is last in the DOM and paints above the rest --
  // no z-index juggling needed
  const filtered = countFilters(deck.filters) > 0;
  $('deck').innerHTML = stack.length
    ? stack.map(deckCard).reverse().join('')
    : `<div class="deck-empty">
         <p class="muted">Nothing left to swipe.</p>
         <p class="muted">${filtered
           ? 'No shabad matches these deck filters, or they are all shortlisted already.'
           : 'Everything is in the Interested folder &mdash; clear it to start again.'}</p>
       </div>`;

  $('deck-status').textContent = stack.length
    ? `${deck.remaining} to go` : '';
  for (const id of ['deck-pass', 'deck-keep', 'deck-open'])
    $(id).disabled = !stack.length;
  // undo stays available with an empty deck -- that's exactly when you notice
  // you swiped the wrong way on the last one
  $('deck-undo').disabled = !deck.history.length;

  // Open each card on the line I know the shabad by, centred rather than at the
  // top -- a tuk with nothing above it reads like the start of the shabad even
  // when it isn't. Done for every card in the stack, not just the top one, so
  // there's no visible jump as cards come forward.
  for (const el of $('deck').querySelectorAll('.swipe-card')) {
    const body = el.querySelector('.card-lines');
    const mine = body && body.querySelector('.dline.mine');
    if (body && mine) {
      body.scrollTop = Math.max(0, mine.offsetTop - (body.clientHeight - mine.offsetHeight) / 2);
    }
  }

  armTopCard();
}

// Pointer events rather than a gesture library (CLAUDE.md §9).
//
// The card both swipes sideways and scrolls up and down, so a gesture has to be
// assigned to one axis or the other. Nothing happens until the pointer has
// moved AXIS_LOCK_PX; whichever axis moved further wins and keeps the gesture
// for its whole duration. Until that decision the pointer is NOT captured, so a
// vertical drag scrolls the shabad natively -- capturing on pointerdown would
// swallow the scroll entirely.
const AXIS_LOCK_PX = 8;

function armTopCard() {
  const el = $('deck').querySelector('.swipe-card[data-i="0"]');
  if (!el) return;
  let startX = 0, startY = 0, dx = 0, axis = null, down = false;

  const lean = (v) => el.style.setProperty('--lean', String(v));

  el.addEventListener('pointerdown', (e) => {
    if (deck.busy) return;
    down = true; axis = null; dx = 0;
    startX = e.clientX; startY = e.clientY;
  });

  el.addEventListener('pointermove', (e) => {
    if (!down) return;
    dx = e.clientX - startX;
    const dy = e.clientY - startY;

    if (!axis) {
      if (Math.abs(dx) < AXIS_LOCK_PX && Math.abs(dy) < AXIS_LOCK_PX) return;
      axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      if (axis === 'x') {
        el.setPointerCapture(e.pointerId);
        el.classList.add('dragging');
      }
    }
    if (axis !== 'x') return;                  // vertical: let the card scroll

    el.style.transform = `translateX(${dx}px) rotate(${dx / 24}deg)`;
    lean(Math.max(-1, Math.min(1, dx / SWIPE_PX)));
  });

  const settle = (commit) => {
    if (!down) return;
    down = false;
    el.classList.remove('dragging');
    if (commit && axis === 'x' && Math.abs(dx) >= SWIPE_PX) {
      commitSwipe(dx > 0 ? 'right' : 'left');
    } else if (axis === 'x') {
      el.style.transform = '';                 // spring back
      lean(0);
    }
    axis = null;
  };

  el.addEventListener('pointerup', () => settle(true));
  // cancel means the browser took the gesture (usually to scroll) -- never
  // treat that as a decision about the shabad
  el.addEventListener('pointercancel', () => settle(false));
}

async function commitSwipe(direction) {
  if (deck.busy || !deck.cards.length) return;
  deck.busy = true;

  const card = deck.cards[0];
  const el = $('deck').querySelector('.swipe-card[data-i="0"]');
  if (el) {
    el.classList.add('gone');
    const way = direction === 'right' ? 1 : -1;
    el.style.transform = `translateX(${way * 140}%) rotate(${way * 22}deg)`;
  }

  try {
    const r = await api(`/api/deck/${card.id}/swipe`, json('POST', { direction }));
    setSavedCount(r.shortlist_count);
    deck.history.push({ card, direction, previous: r.previous_surfaced });
    if (deck.history.length > UNDO_DEPTH) deck.history.shift();
  } catch (err) {
    toast(err.message, true);
    deck.busy = false;
    renderDeck();                              // nothing was recorded -- put it back
    return;
  }

  // let the card finish flying out before the stack collapses under it
  setTimeout(async () => {
    deck.cards.shift();
    deck.remaining = Math.max(0, deck.remaining - 1);
    deck.busy = false;
    if (!deck.cards.length && deck.remaining > 0) await loadDeck();
    else renderDeck();
  }, 170);
}

async function undoSwipe() {
  if (deck.busy || !deck.history.length) return;
  deck.busy = true;
  const last = deck.history.pop();

  try {
    const r = await api(`/api/deck/${last.card.id}/undo`, json('POST', {
      direction: last.direction,
      previous_surfaced: last.previous,
    }));
    setSavedCount(r.shortlist_count);
    // A passed card isn't shortlisted, so a deck loaded since the swipe may
    // already hold it -- drop that copy rather than showing it twice.
    const dup = deck.cards.findIndex((c) => c.id === last.card.id);
    if (dup >= 0) deck.cards.splice(dup, 1); else deck.remaining += 1;
    deck.cards.unshift(last.card);             // straight back on top
    toast(last.direction === 'right' ? 'Removed from Interested' : 'Card brought back');
  } catch (err) {
    deck.history.push(last);                   // nothing changed -- stay undoable
    toast(err.message, true);
  }
  deck.busy = false;
  renderDeck();
}

// ---------- interested ----------

async function loadSaved() {
  const d = await api('/api/shortlist');
  setSavedCount(d.count);
  $('saved-head').textContent = `${d.list} (${d.count})`;
  $('saved-clear').disabled = !d.count;

  $('saved-list').innerHTML = d.count ? d.shabads.map((s) => `
    <div class="card" data-id="${s.id}">
      <div class="card-main">
        <div class="gurmukhi">${esc(s.source_line)}</div>
        <div class="en">${esc(s.source_translation || '')}</div>
      </div>
      <div class="card-side">
        <div class="tags-row">
          ${tag(s.status)}${tag(s.rarity)}${tagList(s.tags.speed)}${tagList(s.tags.genre)}
        </div>
        <div class="meta">
          <span>ang ${esc(s.ang)}</span>
          <span>${esc((s.raag_en || '').replace(/^Raag /, ''))}</span>
          <button class="unsave secondary" data-id="${s.id}" title="Remove">&times;</button>
        </div>
      </div>
    </div>`).join('')
    : `<p class="muted">Nothing here yet. Swipe right in the Deck to add shabads.</p>`;

  $('saved-list').onclick = async (e) => {
    const rm = e.target.closest('.unsave');
    if (rm) {
      e.stopPropagation();
      await api('/api/shortlist/' + rm.dataset.id, { method: 'DELETE' });
      loadSaved();
      return;
    }
    const card = e.target.closest('.card');
    if (card) go('detail', { id: card.dataset.id });
  };
}

// ---------- memorization ----------
//
// A list, and three ways to test yourself on one shabad. No schedule, no
// levels, no gates, no daily caps, no streaks.
//
// There was a full SM-2 implementation here. It worked and it went unused: the
// scheduling was what killed it, because being told what to practise and when
// turns a thing you want to do into a thing you are behind on. What replaced it
// gives you the same drills with none of the bookkeeping -- open a shabad you
// are learning, pick a mode, go at your own pace, stop whenever.

const practice = { shabad: null, lines: [], i: 0, mode: 'letters' };

// Case is ignored on purpose. In the encoding B is ਭ and b is ਬ, but that's an
// encoding detail -- the thing being tested is whether the sequence of words is
// remembered, not whether the right shift key was pressed.
const normLetters = (s) => String(s || '').replace(/[^a-z]/gi, '').toLowerCase();

async function refreshLearnBadge() {
  try {
    const d = await api('/api/learning');
    // Counts what is not finished, not the whole list -- a badge that never
    // goes down as you learn things is just a total wearing a badge's clothes.
    const left = (d.counts.not_started || 0) + (d.counts.in_progress || 0);
    $('learn-due').textContent = left > 0 ? left : '';
  } catch { /* badge only */ }
}

// Set by hand, never derived. Nothing measures your recall any more, so you are
// the only thing that can say where a shabad has got to.
const LEARN_STATES = [
  { id: 'not_started', label: 'Not started', cls: 'red' },
  { id: 'in_progress', label: 'In progress', cls: 'amber' },
  { id: 'memorized',   label: 'Memorized',   cls: 'green' },
];
const learnState = (id) => LEARN_STATES.find((s) => s.id === id) || LEARN_STATES[0];

let learnFilter = null;          // null = show everything

async function loadLearn() {
  const p = new URLSearchParams();
  if (learnFilter) p.set('status', learnFilter);
  const d = await api('/api/learning?' + p);

  $('learn-title').textContent = `Memorizing (${d.total})`;
  $('learn-sub').textContent = d.total
    ? 'Tap one to practise it. Nothing is scheduled — go whenever you like.' : '';

  // Filter chips double as the tally, so the split is visible without counting.
  $('learn-filters').innerHTML = [
    { id: null, label: 'All', cls: '', n: d.total },
    ...LEARN_STATES.map((s) => ({ ...s, n: d.counts[s.id] || 0 })),
  ].map((s) => `
    <button class="chip learn-chip ${s.cls}${learnFilter === s.id ? ' on' : ''}"
            data-status="${s.id === null ? '' : s.id}">
      ${esc(s.label)}<span class="chip-n">${s.n}</span>
    </button>`).join('');

  $('learn-list').innerHTML = d.count ? d.shabads.map((s) => {
    const st = learnState(s.learn_status);
    return `
    <div class="card learn-card st-${st.cls}" data-id="${s.id}">
      <div class="card-main">
        <div class="gurmukhi">${esc(s.source_line)}</div>
        <div class="en">${esc(s.source_translation || '')}</div>
      </div>
      <div class="card-side">
        <select class="learn-status ${st.cls}" data-id="${s.id}"
                aria-label="How well do you know this?">
          ${LEARN_STATES.map((o) => `<option value="${o.id}"${
            o.id === st.id ? ' selected' : ''}>${o.label}</option>`).join('')}
        </select>
        <div class="meta">
          <span>${s.line_count} lines</span>
          <span>${s.last_practised ? 'practised ' + whenLabel(s.last_practised)
                                    : 'not practised yet'}</span>
        </div>
      </div>
      <div class="card-btns">
        <button class="unlearn secondary" data-id="${s.id}"
                title="Remove from the list">&times;</button>
      </div>
    </div>`;
  }).join('')
    : `<p class="muted">${learnFilter
        ? 'Nothing with that status.'
        : 'Nothing here yet. Open a shabad and press <b>Learn</b> to add it.'}</p>`;

  $('learn-filters').onclick = (e) => {
    const chip = e.target.closest('.learn-chip');
    if (!chip) return;
    learnFilter = chip.dataset.status || null;
    loadLearn();
  };

  // change, not click: a <select> inside a tappable card must not also open it
  $('learn-list').onchange = async (e) => {
    const sel = e.target.closest('.learn-status');
    if (!sel) return;
    try {
      await api('/api/learning/' + sel.dataset.id,
                json('PATCH', { status: sel.value }));
      loadLearn();
    } catch (err) {
      toast('Could not save that: ' + err.message, true);
      loadLearn();
    }
  };

  $('learn-list').onclick = async (e) => {
    if (e.target.closest('.learn-status')) return;      // the dropdown, not the card
    const drop = e.target.closest('.unlearn');
    if (drop) {
      e.stopPropagation();
      // No confirm: there is no progress to lose any more, just a list entry,
      // and adding it back is one tap.
      try {
        await api('/api/learning/' + drop.dataset.id, { method: 'DELETE' });
        toast('Removed from Memorizing');
        loadLearn(); refreshLearnBadge();
      } catch (err) { toast(err.message, true); }
      return;
    }
    const card = e.target.closest('.card');
    if (card) startPractice(card.dataset.id);
  };
}

// ---- practising one shabad ----

async function startPractice(shabadId) {
  try {
    const d = await api('/api/learning/' + shabadId + '/lines');
    practice.shabad = d.shabad;
    practice.lines = d.lines;
    practice.i = 0;
    go('practice');
    renderPractice();
    // Stamping it here, not at the end: opening it IS practising it, and a
    // stamp that only lands if you reach the last line would under-report.
    api('/api/learning/' + shabadId + '/practised', json('POST', {})).catch(() => {});
  } catch (err) { toast(err.message, true); }
}

function setMode(mode) {
  practice.mode = mode;
  for (const b of $('view-practice').querySelectorAll('.mode-btn'))
    b.classList.toggle('on', b.dataset.mode === mode);
  renderPractice();
}

function renderPractice() {
  if (!practice.lines.length) { $('practice').innerHTML = ''; return; }
  if (practice.mode === 'perform') return renderPerform();

  const item = practice.lines[practice.i];
  $('practice-progress').textContent = `${practice.i + 1} of ${practice.lines.length}`;
  if (practice.mode === 'meaning') return renderMeaning(item);
  return renderLetters(item);
}

// Always available, never forced. Prev works from the first line (wraps to the
// end) because there is no "correct" direction through a shabad you are
// revising -- sometimes you want the line before the one you just fluffed.
function practiceNav(extra = '') {
  return `
    <div class="drill-nav">
      <button id="pr-prev" class="secondary">&larr; Prev</button>
      ${extra}
      <button id="pr-next" class="secondary">Next &rarr;</button>
    </div>`;
}

function wireNav() {
  const n = practice.lines.length;
  $('pr-prev').onclick = () => { practice.i = (practice.i - 1 + n) % n; renderPractice(); };
  $('pr-next').onclick = () => { practice.i = (practice.i + 1) % n; renderPractice(); };
}

function renderLetters(item) {
  $('practice').innerHTML = `
    <div class="drill">
      <div class="drill-tag">Line ${item.line_no} &mdash; type the first letters</div>
      ${item.translation_en ? `<div class="drill-en">${esc(item.translation_en)}</div>` : ''}
      <input id="dr-input" type="text" autocomplete="off" autocapitalize="off"
             spellcheck="false" placeholder="e.g. gkbvv">
      <div class="drill-actions">
        <button id="dr-check">Check</button>
        <button id="dr-reveal" class="secondary">Reveal</button>
      </div>
      <div id="dr-result"></div>
      ${practiceNav()}
    </div>`;
  wireNav();

  const input = $('dr-input');
  input.focus();

  const reveal = (verdict) => {
    $('dr-result').innerHTML = `
      ${verdict}
      <div class="gurmukhi big">${esc(item.gurmukhi)}</div>
      <div class="mono letters">${esc(item.first_letters || '')}</div>
      ${item.teeka_pa ? `<div class="drill-teeka">${esc(item.teeka_pa)}</div>` : ''}`;
  };

  $('dr-check').onclick = () => {
    const got = normLetters(input.value);
    if (!got) return;
    const right = got === normLetters(item.first_letters);
    reveal(`<div class="verdict ${right ? 'right' : 'wrong'}">${
      right ? 'Correct' : 'Not quite'}</div>`);
  };
  // No grading buttons. Whether you got it is between you and the line -- the
  // app has no opinion and records nothing.
  $('dr-reveal').onclick = () => reveal('');
  input.onkeydown = (e) => { if (e.key === 'Enter') $('dr-check').onclick(); };
}

async function renderMeaning(item) {
  $('practice').innerHTML = '<p class="muted">Loading…</p>';
  let q;
  try { q = await api('/api/quiz/' + item.id); }
  catch (err) { $('practice').innerHTML =
    `<p class="muted">Could not load: ${esc(err.message)}</p>`; return; }

  $('practice').innerHTML = `
    <div class="drill">
      <div class="drill-tag">Line ${item.line_no} &mdash; what does it mean?</div>
      <div class="gurmukhi big">${esc(q.gurmukhi)}</div>
      <div class="options">
        ${q.options.map((o, i) =>
          `<button class="option" data-i="${i}">${esc(o)}</button>`).join('')}
      </div>
      ${practiceNav()}
    </div>`;
  wireNav();

  $('practice').querySelectorAll('.option').forEach((b, i) => {
    b.onclick = () => {
      const right = q.options[i] === q.answer;
      b.classList.add(right ? 'right' : 'wrong');
      $('practice').querySelectorAll('.option').forEach((x, j) => {
        x.disabled = true;
        if (!right && q.options[j] === q.answer) x.classList.add('right');
      });
    };
  });
}

// CLAUDE.md §3: the whole shabad, first letters only, no interruptions. This is
// the one that maps onto actually singing it -- the others are for learning a
// line, this is for holding the shape of the whole thing.
function renderPerform() {
  $('practice-progress').textContent = `${practice.lines.length} lines`;
  $('practice').innerHTML = `
    <div class="perform">
      <div class="drill-tag">Tap any line to reveal it</div>
      ${practice.lines.map((l, i) => `
        <div class="perform-line" data-i="${i}">
          <span class="mono letters">${esc(l.first_letters || '—')}</span>
          <div class="gurmukhi reveal" hidden>${esc(l.gurmukhi)}</div>
        </div>`).join('')}
      <div class="drill-actions">
        <button id="pf-all" class="secondary">Reveal all</button>
        <button id="pf-none" class="secondary">Hide all</button>
      </div>
    </div>`;

  $('practice').onclick = (e) => {
    const row = e.target.closest('.perform-line');
    if (row) {
      const g = row.querySelector('.reveal');
      g.hidden = !g.hidden;
    }
  };
  const setAll = (hidden) => $('practice').querySelectorAll('.reveal')
    .forEach((g) => { g.hidden = hidden; });
  $('pf-all').onclick = () => setAll(false);
  $('pf-none').onclick = () => setAll(true);
}

$('view-practice').querySelectorAll('.mode-btn').forEach((b) => {
  b.onclick = () => setMode(b.dataset.mode);
});
$('practice-quit').onclick = () => { go('learn'); refreshLearnBadge(); };

// ---------- history ----------

// SQLite writes datetime('now') as UTC with no zone marker, so it must be told
// it's UTC -- otherwise every entry reads as hours off.
function whenLabel(stamp) {
  if (!stamp) return '';
  const t = new Date(String(stamp).replace(' ', 'T') + 'Z');
  if (isNaN(t)) return '';
  const mins = (Date.now() - t.getTime()) / 60000;
  if (mins < 1) return 'just now';
  if (mins < 60) return `${Math.floor(mins)}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  if (mins < 10080) return `${Math.floor(mins / 1440)}d ago`;
  return t.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

const exactWhen = (stamp) => {
  const t = new Date(String(stamp).replace(' ', 'T') + 'Z');
  return isNaN(t) ? '' : t.toLocaleString();
};

async function loadHistory() {
  const d = await api('/api/history');
  $('history-head').textContent = `Recently opened (${d.count})`;
  $('history-clear').disabled = !d.count;

  $('history-list').innerHTML = d.count ? d.shabads.map((s) => `
    <div class="card" data-id="${s.id}">
      <div class="card-main">
        <div class="gurmukhi">${esc(s.source_line)}</div>
        <div class="en">${esc(s.source_translation || '')}</div>
      </div>
      <div class="card-side">
        <div class="tags-row">
          ${tag(s.status)}${tag(s.rarity)}${tagList(s.tags.speed)}${tagList(s.tags.genre)}
        </div>
        <div class="meta">
          <span class="when" title="${esc(exactWhen(s.opened_at))}">${esc(whenLabel(s.opened_at))}</span>
          <span>ang ${esc(s.ang)}</span>
        </div>
      </div>
      <div class="card-btns">
        <button class="heart${s.shortlisted ? ' on' : ''}" data-id="${s.id}"
                aria-label="Toggle Interested">${s.shortlisted ? '&hearts;' : '&#9825;'}</button>
        <button class="unlog secondary" data-hid="${s.history_id}"
                title="Remove this entry">&times;</button>
      </div>
    </div>`).join('')
    : `<p class="muted">Nothing yet. Shabads you open will show up here &mdash;
       the last ${d.limit}, repeats included.</p>`;

  $('history-list').onclick = async (e) => {
    const heart = e.target.closest('.heart');
    if (heart) { e.stopPropagation(); toggleShortlist(heart); return; }

    const drop = e.target.closest('.unlog');
    if (drop) {
      e.stopPropagation();
      // only this visit disappears -- other opens of the same shabad stay
      try {
        await api('/api/history/' + drop.dataset.hid, { method: 'DELETE' });
        loadHistory();
      } catch (err) { toast(err.message, true); }
      return;
    }

    const card = e.target.closest('.card');
    if (card) go('detail', { id: card.dataset.id });
  };
}

// ---------- add ----------

let searchTimer;

async function runSearch() {
  const q = $('s-q').value.trim();
  if (q.length < 2) { $('results').innerHTML = ''; $('s-count').textContent = ''; return; }

  const p = new URLSearchParams({ q, mode: $('s-mode').value });
  if ($('s-source').value) p.set('source', $('s-source').value);

  const data = await api('/api/search?' + p);
  const mode = $('s-mode').value;
  $('s-count').textContent = `${data.count} results`;
  $('results').innerHTML = data.results.map((r) => `
    <div class="result ${r.already_have ? 'have' : ''}"
         data-shabad="${r.shabad_id}" data-line="${r.line_no}">
      <div class="gurmukhi">${mode === 'firstletter'
        ? markLetters(r.gurmukhi, r.first_letters, q) : mark(r.gurmukhi, q)}</div>
      ${r.english ? `<div class="en">${esc(r.english)}</div>` : ''}
      <div class="meta">
        <span>ang ${esc(r.ang)}</span><span>${esc(r.raag_en)}</span>
        <span>${esc(r.writer)}</span>
      </div>
      ${r.already_have ? '<div class="tags-row"><span class="tag t-dim">already in library</span></div>' : ''}
    </div>`).join('') || '<p class="muted">No matches.</p>';

  $('results').onclick = (e) => {
    const hit = e.target.closest('.result');
    if (hit) openPreview(+hit.dataset.shabad, +hit.dataset.line);
  };
}

async function openPreview(shabadId, lineNo) {
  const p = await api('/api/preview/' + shabadId);
  $('preview').innerHTML = `
    <h2 style="margin-top:1.5rem">Shabad ${shabadId} &mdash; ${p.verses.length} lines</h2>
    <div class="meta">
      <span>ang ${esc(p.ang)}</span><span>${esc(p.raag_en)}</span>
      <span>${esc(p.writer)}</span><span>${esc(p.source_en)}</span>
    </div>
    ${p.already_have_id
      ? `<p class="muted">Already in your library.
           <a href="#" id="go-existing">Open it</a></p>`
      : `
      <div class="field" style="margin-top:.8rem">
        <label>Which line do you know it by?</label>
        <select id="a-line">${p.verses.map((v) =>
          `<option value="${v.line_no}" ${v.line_no === lineNo ? 'selected' : ''}>
             ${v.line_no}. ${esc(v.gurmukhi)}</option>`).join('')}</select>
      </div>
      <div class="field"><label>Status</label>
        <select id="a-status"><option>Heard</option><option>In Progress</option><option>Ready</option></select></div>
      <div class="field"><label>Rarity</label>
        <select id="a-rarity"><option value="">Not chosen</option><option>Common</option><option>Uncommon</option></select></div>
      <div class="field"><label>Genre</label>
        <div class="chips" id="a-genre">${checkboxes('genre', [])}</div></div>
      <div class="field"><label>Speed</label>
        <div class="chips" id="a-speed">${checkboxes('speed', [])}</div></div>
      <div class="field"><label>Notes</label><textarea id="a-notes"></textarea></div>
      <button id="a-save">Add to library</button>`}

    <details class="lines-toggle" open>
      <summary>Show all ${p.verses.length} lines</summary>
      ${p.verses.map((v) => `
        <div class="line ${v.line_no === lineNo ? 'mine' : ''}">
          <div class="no">${v.line_no}</div>
          <div class="gurmukhi">${esc(v.gurmukhi)}</div>
          <div class="en">${esc(v.english)}</div>
          <div class="pa">${esc(v.teeka)}</div>
        </div>`).join('')}
    </details>`;

  $('preview').scrollIntoView({ behavior: 'smooth' });

  if (p.already_have_id) {
    $('go-existing').onclick = (e) => { e.preventDefault(); go('detail', { id: p.already_have_id }); };
    return;
  }

  for (const kind of ['genre', 'speed']) {
    $('a-' + kind).onclick = (e) => {
      const label = e.target.closest('label');
      if (!label) return;
      const box = label.querySelector('input');
      box.checked = !box.checked;
      label.classList.toggle('on', box.checked);
    };
  }

  $('a-save').onclick = async () => {
    const picked = (kind) => [...$('a-' + kind).querySelectorAll('input:checked')]
      .map((b) => b.value);
    try {
      const res = await api('/api/shabads', json('POST', {
        banidb_shabad_id: shabadId,
        source_line_no: +$('a-line').value,
        status: $('a-status').value,
        rarity: $('a-rarity').value || null,
        notes: $('a-notes').value || null,
        genre: picked('genre'), speed: picked('speed'),
      }));
      toast(`Added with ${res.lines} lines`);
      $('preview').innerHTML = '';
      $('s-q').value = '';
      $('results').innerHTML = '';
      await loadFilters();
      await loadList();
      go('detail', { id: res.id });
    } catch (err) {
      toast(err.status === 409 ? 'Already in your library' : err.message, true);
    }
  };
}

// ---------- wiring ----------

$('nav-list').onclick = () => go('list');
$('nav-add').onclick = () => go('add');
$('nav-settings').onclick = () => go('settings');
$('back-to-list').onclick = goBack;
$('d-edit').onclick = openEditDialog;
$('d-heart').onclick = (e) => toggleShortlist(e.currentTarget);

// Delegated, because #detail is rebuilt from scratch on every open -- handlers
// bound to the old nodes would quietly stop firing.
$('detail').addEventListener('click', (e) => {
  if (e.target.closest('[data-idx-badge]')) return openIndexDialog();
  const line = e.target.closest('.line[data-line-id]');
  // Selecting the text of a tuk to read or copy it ends in a click, and that
  // must not navigate away from what you were reading. A non-empty selection
  // is the signal that the click was the end of a drag, not a tap.
  if (line && !String(window.getSelection() || '').trim()) {
    go('similar', { id: line.dataset.lineId });
  }
});
$('detail').addEventListener('keydown', (e) => {
  const line = e.target.closest('.line[data-line-id]');
  if (line && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    go('similar', { id: line.dataset.lineId });
  }
});
$('ix-close').onclick = () => $('index-dialog').close();
$('ix-done').onclick = () => $('index-dialog').close();

// ---- nav: a menu on a phone, buttons on a desktop ----
const navOpen = (on) => {
  $('nav').classList.toggle('open', on);
  $('nav-toggle').setAttribute('aria-expanded', String(on));
};
$('nav-toggle').onclick = (e) => {
  e.stopPropagation();
  navOpen(!$('nav').classList.contains('open'));
};
$('nav').onclick = (e) => { if (e.target.closest('button')) navOpen(false); };
document.addEventListener('click', () => navOpen(false));

// only build a deck on first visit -- coming back should find it as you left it
$('nav-deck').onclick = () => {
  show('deck');
  if (deck.cards.length) renderDeck(); else loadDeck();
};
$('nav-saved').onclick = () => go('saved');
$('nav-history').onclick = () => go('history');
$('nav-learn').onclick = () => go('learn');

$('d-learn').onclick = async () => {
  const on = $('d-learn').classList.contains('on');
  if (on && !confirm('Stop memorizing this shabad?\n\nAll its progress — levels, '
                   + 'review history, schedule — is deleted and cannot be recovered.')) return;
  try {
    const r = await api('/api/learning/' + current.id, { method: on ? 'DELETE' : 'POST' });
    current.learning = !on;
    paintLearnBtn(!on);
    toast(on ? 'Removed from Learning' : `Added — ${r.lines} lines to memorize`);
    refreshLearnBadge();
  } catch (err) { toast(err.message, true); }
};

$('history-clear').onclick = async () => {
  if (!confirm('Clear the open history?\n\nOnly the log of what you opened — '
             + 'no shabads, tags or notes are touched.')) return;
  try {
    const r = await api('/api/history', { method: 'DELETE' });
    toast(`Cleared ${r.cleared} entr${r.cleared === 1 ? 'y' : 'ies'}`);
    loadHistory();
  } catch (err) { toast(err.message, true); }
};

$('deck-pass').onclick = () => commitSwipe('left');
$('deck-keep').onclick = () => commitSwipe('right');
$('deck-open').onclick = () => { if (deck.cards.length) go('detail', { id: deck.cards[0].id }); };
$('deck-undo').onclick = undoSwipe;
$('deck-reshuffle').onclick = loadDeck;
$('deck-include').onchange = () => { deck.cards = []; loadDeck(); };
$('deck-filter-clear').onclick = () => {
  deck.filters = {};
  renderChips('deck-filter-groups', deck.filters, () => { deck.cards = []; loadDeck(); });
  loadDeck();
};

$('saved-clear').onclick = async () => {
  if (!confirm(`Empty the Interested folder?\n\nThis only clears the shortlist -- `
             + `the shabads themselves, their tags and notes are untouched.`)) return;
  try {
    const r = await api('/api/shortlist', { method: 'DELETE' });
    toast(`Cleared ${r.cleared} shabad${r.cleared === 1 ? '' : 's'}`);
    loadSaved();
  } catch (err) { toast(err.message, true); }
};

// rotating the phone or opening the filters changes what's left for the card
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    measureHeader();
    if (show.current === 'deck') renderDeck();   // re-centre on my line
  }, 120);
});
$('deck-filter-wrap').addEventListener('toggle', () => {
  if (show.current === 'deck') renderDeck();
});

// arrow keys are the desktop equivalent of a swipe
document.addEventListener('keydown', (e) => {
  if (show.current !== 'deck') return;
  if (e.target.matches('input, textarea, select')) return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); commitSwipe('left'); }
  if (e.key === 'ArrowRight') { e.preventDefault(); commitSwipe('right'); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault(); undoSwipe();
  }
});

$('f-q').oninput = (e) => {
  state.q = e.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadList, 200);
};
$('f-sort').onchange = (e) => { state.sort = e.target.value; loadList(); };

$('f-mode').onchange = (e) => {
  state.mode = e.target.value;
  const first = state.mode === 'firstletter';
  $('f-q').placeholder = first
    ? 'First letters of a line, e.g. gkbvv'
    : 'Keywords in the meaning, e.g. love name';
  $('mode-hint').innerHTML = first
    ? 'first letter of each word, like STTM &mdash; capitals are exact, so gkBBj is ਭ not ਬ'
    : 'all your words must appear in one line, in any order &mdash; '
      + 'wrap in &ldquo;quotes&rdquo; for an exact phrase';
  if (state.q) loadList();
};
$('f-clear').onclick = () => {
  state.filters = {}; state.q = ''; $('f-q').value = '';
  document.querySelectorAll('#filter-groups .chip.on').forEach((c) => c.classList.remove('on'));
  paintFilterCount();
  loadList();
};

$('s-q').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 200); };
$('s-mode').onchange = () => {
  $('s-q').placeholder = $('s-mode').value === 'firstletter'
    ? 'Type first letters, e.g. gkbvv'
    : 'Type Gurmukhi words, e.g. ਨਾਮੁ ਧਿਆਇ';
  runSearch();
};
$('s-source').onchange = runSearch;

(async function start() {
  // Fail loudly if the markup and this script disagree about what exists --
  // a missing element otherwise throws deep inside a handler and the page just
  // quietly stops working.
  const missing = ['f-mode', 'f-q', 's-mode', 's-q', 'list', 'filter-groups',
                   'nav-deck', 'nav-saved', 'deck', 'deck-pass', 'deck-keep',
                   'deck-open', 'deck-reshuffle', 'saved-list', 'saved-clear',
                   'saved-count', 'saved-head', 'deck-status',
                   'deck-filter-groups', 'deck-filter-clear', 'deck-filter-count',
                   'deck-filter-wrap', 'deck-undo', 'deck-include',
                   'nav', 'nav-toggle', 'lib-filter-wrap', 'lib-filter-count',
                   'd-heart', 'd-sttm', 'd-edit', 'edit-dialog', 'edit-body',
                   'e-save', 'e-delete', 'e-cancel', 'e-close', 'f-clear',
                   'nav-history', 'history-list', 'history-clear', 'history-head',
                   'nav-learn', 'learn-due', 'learn-list', 'learn-title',
                   'learn-sub', 'd-learn', 'practice', 'practice-quit',
                   'practice-progress',
                   'nav-settings', 'set-models', 'set-scores', 'set-auto-index',
                   'similar', 'sim-back', 'sim-blind', 'sim-open',
                   'index-dialog', 'index-body', 'ix-close', 'ix-done',
                   ...VIEWS.map((v) => 'view-' + v)]
    .filter((id) => !$(id));
  if (missing.length) {
    toast('markup is missing: ' + missing.join(', '), true);
    console.error('missing elements', missing);
    return;
  }
  // single source of truth for the default: whichever <option> is selected
  state.mode = $('f-mode').value;
  measureHeader();

  // we restore scroll ourselves from history.state; the browser's own attempt
  // fights it because the content isn't there yet when it fires
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

  try {
    await loadFilters();
    refreshSavedCount();          // badges only -- not worth blocking the page on
    refreshLearnBadge();

    // whatever url was opened, including a reloaded or shared /shabad/123
    const start = parsePath(location.pathname);
    history.replaceState(start, '', pathFor(start.view, start.id));
    await render(start);
  } catch (err) { toast(err.message, true); }
})();
