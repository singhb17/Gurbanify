"""Build a standalone page for judging model similarity results by hand.

    python search/compare.py            # writes search/compare.html
    python search/compare.py --top 20   # more results per model

The bench in `bench.py` scores models by R-precision: of the lines you grouped
together, how many land in each other's top results. That measures agreement
with your grouping, NOT the quality of a connection -- a line from a different
group can be a better match than one from the same group, and no `if` statement
can tell the difference. Only you can.

So this reads the summaries and vectors already cached by the bench, works out
each model's nearest neighbours for every cached line, and writes one
self-contained HTML file. Open it, tap a line, and thumb the results up or
down. Results are merged, deduplicated and unlabelled: you cannot see which
model produced what until you ask, so a model you already like cannot flatter
itself.

Nothing here calls an API. Every summary and vector it needs is already paid
for and on disk, so regenerating the page costs nothing.

CLAUDE.md §7: the actual tuk is always shown next to the score, and no result
is ever merged away silently. §12: every line of Gurmukhi is read from the
database, never generated.
"""

import argparse
import glob
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
# Generated output lives apart from source and is gitignored -- it is rebuilt
# from the cache in seconds, so versioning it only creates noisy diffs.
OUT_DIR = os.path.join(HERE, "out")
OUT_PATH = os.path.join(OUT_DIR, "compare.html")


def build_data(top):
    import numpy as np
    from bench import (load_topics, load_summaries, load_vectors, embed_all,
                       MODELS_PATH, TOPICS_DIR)

    sets = load_topics(sorted(glob.glob(os.path.join(TOPICS_DIR, "*.txt"))))
    rows, seen = [], set()
    for r, _, _ in sets.values():
        for x in r:
            if x["gurmukhi"] not in seen:
                seen.add(x["gurmukhi"])
                rows.append(x)

    registry = {k: v for k, v in json.load(io.open(MODELS_PATH, encoding="utf-8")).items()
                if not k.startswith("_")}

    # Only models with a summary for EVERY line can be compared fairly -- a
    # model missing 70 of 108 lines would look artificially specialised.
    models, caches = [], {}
    for m in registry:
        c = load_summaries(m)
        if all(r["gurmukhi"] in c for r in rows):
            models.append(m)
            caches[m] = c
        elif c:
            print(f"  skipping {m}: only {len(c)} of {len(rows)} lines cached")

    if not models:
        sys.exit("No fully-cached model. Run bench.py first.")

    vc = load_vectors()
    neighbours = {}
    for m in models:
        v = embed_all([caches[m][r["gurmukhi"]] for r in rows], vc)
        v = v / np.linalg.norm(v, axis=1, keepdims=True)
        sim = v @ v.T
        np.fill_diagonal(sim, -9)                    # never match a line to itself
        neighbours[m] = [[int(j) for j in np.argsort(-sim[i])[:top]]
                         for i in range(len(rows))]

    return {
        "lines": [{"g": r["gurmukhi"], "e": r["translation_en"] or ""} for r in rows],
        "models": models,
        "top": top,
        "neighbours": neighbours,
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Model compare</title>
<style>
:root{--bg:#faf9f7;--card:#fff;--ink:#1c1a17;--dim:#6b6560;--line:#e5e0d8;
      --up:#1a7f4b;--down:#b3261e;--accent:#7a5c2e}
@media(prefers-color-scheme:dark){:root{--bg:#16151a;--card:#1e1d24;--ink:#ece9e4;
      --dim:#9a938c;--line:#32303a;--up:#4ade80;--down:#f87171;--accent:#d4b483}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
.gur{font-size:1.22em;line-height:1.85}
header{position:sticky;top:0;z-index:5;background:var(--bg);
       border-bottom:1px solid var(--line);padding:10px 14px}
h1{font-size:15px;margin:0 0 8px;font-weight:600}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--card);
       color:var(--ink);border-radius:7px;padding:5px 10px}
button.on{background:var(--accent);border-color:var(--accent);color:#fff}
button:hover{border-color:var(--accent)}
.wrap{display:grid;grid-template-columns:minmax(260px,1fr) 2fr;gap:14px;
      padding:14px;max-width:1400px;margin:0 auto;align-items:start}
@media(max-width:800px){.wrap{grid-template-columns:1fr}
  #list{max-height:38vh}}
#list{background:var(--card);border:1px solid var(--line);border-radius:10px;
      max-height:78vh;overflow:auto}
#list div{padding:9px 12px;border-bottom:1px solid var(--line);cursor:pointer}
#list div:hover{background:var(--bg)}
#list div.sel{background:var(--bg);border-left:3px solid var(--accent)}
#list .en{color:var(--dim);font-size:12.5px;margin-top:2px}
input[type=search]{width:100%;font:inherit;padding:8px 12px;border:0;
      border-bottom:1px solid var(--line);background:transparent;color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:12px 14px;margin-bottom:9px}
.q{border-left:3px solid var(--accent)}
.card .en{color:var(--dim);font-size:13.5px;margin-top:5px}
.acts{display:flex;gap:6px;margin-top:9px;align-items:center;flex-wrap:wrap}
.acts button{padding:3px 11px;font-size:15px;line-height:1.4}
.up.on{background:var(--up);border-color:var(--up);color:#fff}
.down.on{background:var(--down);border-color:var(--down);color:#fff}
.who{font-size:11.5px;color:var(--dim);margin-left:auto;font-family:ui-monospace,monospace}
.muted{color:var(--dim);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left;font-family:ui-monospace,monospace}
tbody tr:hover{background:var(--bg)}
</style></head><body>
<header>
  <h1>Model compare &mdash; blind. Thumb what genuinely connects.</h1>
  <div class="row" id="models"></div>
  <div class="row" style="margin-top:7px">
    <button id="reveal">show which model</button>
    <button id="scores">scores</button>
    <button id="export">export votes</button>
    <button id="reset">reset votes</button>
    <span class="muted" id="count"></span>
  </div>
</header>
<div class="wrap">
  <div id="list"><input type="search" id="q" placeholder="filter lines..."></div>
  <div id="panel"><p class="muted">Pick a line on the left.</p></div>
</div>
<script>
const DATA = __DATA__;
const KEY = 'shabad-compare-votes';
let votes = JSON.parse(localStorage.getItem(KEY) || '{}');
let active = new Set(DATA.models.slice(0, 4));
let reveal = false, cur = null;

const save = () => localStorage.setItem(KEY, JSON.stringify(votes));
const esc = s => s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
// Attention falls off down a list, but not off a cliff -- #2 is worth about
// 2.7x #19 here, where a plain 1/rank would say 9.5x. rank is 0-indexed.
const discount = rank => 1 / Math.log2(rank + 2);

function modelBar() {
  const el = document.getElementById('models');
  el.innerHTML = '';
  DATA.models.forEach(m => {
    const b = document.createElement('button');
    b.textContent = m;
    b.className = active.has(m) ? 'on' : '';
    b.onclick = () => { active.has(m) ? active.delete(m) : active.add(m);
                        modelBar(); if (cur !== null) show(cur); };
    el.appendChild(b);
  });
}

function drawList(filter = '') {
  const el = document.getElementById('list');
  [...el.querySelectorAll('div')].forEach(d => d.remove());
  const f = filter.toLowerCase();
  DATA.lines.forEach((L, i) => {
    if (f && !L.g.includes(filter) && !L.e.toLowerCase().includes(f)) return;
    const d = document.createElement('div');
    if (i === cur) d.className = 'sel';
    d.innerHTML = `<div class="gur">${esc(L.g)}</div><div class="en">${esc(L.e)}</div>`;
    d.onclick = () => { cur = i; drawList(filter); show(i); };
    el.appendChild(d);
  });
}

// One row per RESULT LINE, not per model: a line both models returned is shown
// once and thumbed once, and every model that surfaced it shares the verdict.
function merged(qi) {
  const by = new Map();
  [...active].forEach(m => (DATA.neighbours[m][qi] || []).forEach((j, rank) => {
    if (!by.has(j)) by.set(j, {});
    by.get(j)[m] = rank;
  }));
  const out = [...by.entries()].map(([j, ranks]) => ({ j, ranks }));
  // Shuffled, so position on screen carries no hint about who ranked it where.
  for (let i = out.length - 1; i > 0; i--) {
    const k = (qi * 7919 + i * 104729) % (i + 1);   // stable per query
    [out[i], out[k]] = [out[k], out[i]];
  }
  return out;
}

function show(qi) {
  const p = document.getElementById('panel');
  if (!active.size) { p.innerHTML = '<p class="muted">Turn on at least one model.</p>'; return; }
  const L = DATA.lines[qi], rows = merged(qi);
  let h = `<div class="card q"><div class="gur">${esc(L.g)}</div>
           <div class="en">${esc(L.e)}</div></div>
           <p class="muted">${rows.length} results from ${active.size} model(s),
           merged and shuffled.</p>`;
  rows.forEach(({ j, ranks }) => {
    const v = (votes[qi] || {})[j];
    const who = Object.entries(ranks).sort((a, b) => a[1] - b[1])
                  .map(([m, r]) => `${m} #${r + 1}`).join('  ');
    h += `<div class="card"><div class="gur">${esc(DATA.lines[j].g)}</div>
      <div class="en">${esc(DATA.lines[j].e)}</div>
      <div class="acts">
        <button class="up ${v === 1 ? 'on' : ''}" data-q="${qi}" data-j="${j}" data-v="1">&#128077;</button>
        <button class="down ${v === -1 ? 'on' : ''}" data-q="${qi}" data-j="${j}" data-v="-1">&#128078;</button>
        <span class="who">${reveal ? esc(who) : ''}</span>
      </div></div>`;
  });
  p.innerHTML = h;
  p.querySelectorAll('.acts button').forEach(b => b.onclick = () => {
    const q = b.dataset.q, j = b.dataset.j, val = +b.dataset.v;
    votes[q] = votes[q] || {};
    if (votes[q][j] === val) delete votes[q][j]; else votes[q][j] = val;
    save(); show(+q); tally();
  });
  tally();
}

function tally() {
  let n = 0;
  Object.values(votes).forEach(o => n += Object.keys(o).length);
  document.getElementById('count').textContent = `${n} votes cast`;
}

function scoreTable() {
  const S = {};
  DATA.models.forEach(m => S[m] = { up: 0, down: 0, dcg: 0, ideal: 0, uup: 0, udown: 0 });
  Object.entries(votes).forEach(([qi, per]) => {
    Object.entries(per).forEach(([j, v]) => {
      // Who offered this line, and how prominently?
      const offered = DATA.models.filter(m => active.has(m) &&
                        (DATA.neighbours[m][qi] || []).includes(+j));
      offered.forEach(m => {
        const rank = DATA.neighbours[m][qi].indexOf(+j);
        S[m].dcg += v * discount(rank);
        S[m].ideal += discount(rank);
        v > 0 ? S[m].up++ : S[m].down++;
        // A line every model returned separates nobody. Uniques are the signal.
        if (offered.length === 1) v > 0 ? S[m].uup++ : S[m].udown++;
      });
    });
  });
  let h = `<p class="muted">Credit is discounted by rank &mdash; a result at #2 is
    worth about 2.7&times; one at #19. "Unique" counts only results no other
    active model offered; lines everyone returned separate nobody.</p>
    <table><thead><tr><th>model</th><th>&#128077;</th><th>&#128078;</th>
    <th>score</th><th>unique &#128077;/&#128078;</th></tr></thead><tbody>`;
  DATA.models.filter(m => active.has(m))
    .map(m => [m, S[m].ideal ? S[m].dcg / S[m].ideal : 0])
    .sort((a, b) => b[1] - a[1])
    .forEach(([m, norm]) => {
      const s = S[m];
      h += `<tr><td>${m}</td><td>${s.up}</td><td>${s.down}</td>
            <td>${s.ideal ? (norm * 100).toFixed(0) + '%' : '&mdash;'}</td>
            <td>${s.uup}/${s.udown}</td></tr>`;
    });
  document.getElementById('panel').innerHTML = h + '</tbody></table>';
}

document.getElementById('q').oninput = e => drawList(e.target.value);
document.getElementById('reveal').onclick = e => {
  reveal = !reveal; e.target.className = reveal ? 'on' : '';
  if (cur !== null) show(cur);
};
document.getElementById('scores').onclick = scoreTable;
document.getElementById('reset').onclick = () => {
  if (confirm('Delete every vote?')) { votes = {}; save(); tally(); if (cur !== null) show(cur); }
};
document.getElementById('export').onclick = () => {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(votes, null, 1)],
             { type: 'application/json' }));
  a.download = 'compare-votes.json'; a.click();
};
modelBar(); drawList(); tally();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15,
                    help="results per model per line")
    args = ap.parse_args()

    data = build_data(args.top)
    html = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False,
                                               separators=(",", ":")))
    os.makedirs(OUT_DIR, exist_ok=True)
    io.open(OUT_PATH, "w", encoding="utf-8").write(html)

    kb = os.path.getsize(OUT_PATH) / 1024
    print(f"\nwrote {OUT_PATH}  ({kb:.0f} KB)")
    print(f"  {len(data['lines'])} lines, {len(data['models'])} models, "
          f"top {args.top} each")
    print(f"  models: {' '.join(data['models'])}")
    print("\nopen it in a browser. votes are kept in that browser's local")
    print("storage, so use the same one; `export votes` writes them to a file.")


if __name__ == "__main__":
    main()
