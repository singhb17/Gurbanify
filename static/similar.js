/* Similarity search, and the blind model comparison that rides on it.
 *
 * CLAUDE.md §3: tap a line, get other lines in the library that mean something
 * similar. That screen is the product. The model comparison is a temporary
 * passenger on it -- a Blind toggle and a pair of thumbs -- so that judging
 * which summariser to keep does not need a whole screen of its own that would
 * be deleted a month later.
 *
 * Results arrive merged and shuffled from the server: one row per RESULT LINE,
 * however many models offered it, so a line is judged once and every model that
 * surfaced it shares the verdict.
 *
 * §7: rank, never threshold, and always show the actual tuk next to the score.
 * Whether two lines truly connect is my judgement, not the machine's.
 */

let simState = { lineId: null, data: null };

async function loadSimilar(lineId) {
  simState.lineId = lineId;
  $('similar').innerHTML = '<p class="muted">Looking...</p>';
  try {
    simState.data = await api('/api/similar/' + lineId);
  } catch (err) {
    $('similar').innerHTML = `<p class="muted">Could not load: ${esc(err.message)}</p>`;
    return;
  }
  renderSimilar();
}

function renderSimilar() {
  const d = simState.data;
  if (!d) return;
  const blind = $('sim-blind').checked;
  const q = d.query;

  // All three readings, same as the shabad page. The English flattens the
  // meaning and the teeka carries the grammar and implied subject it drops
  // (CLAUDE.md §6) -- judging whether two lines connect needs both.
  const texts = (r) => `
    <div class="gurmukhi">${esc(r.gurmukhi)}</div>
    ${r.translation_en ? `<div class="en">${esc(r.translation_en)}</div>` : ''}
    ${r.teeka_pa ? `<div class="pa">${esc(r.teeka_pa)}</div>` : ''}`;

  let h = `
    <div class="sim-query">
      <div class="sim-label">Finding lines similar to</div>
      ${texts(q)}
      <div class="meta">
        <span>ang ${esc(q.ang)}</span><span>${esc(q.raag_en)}</span>
        <span>${esc(q.writer)}</span><span>line ${esc(q.line_no)}</span>
      </div>
    </div>`;

  if (!d.results.length) {
    // An empty list with no explanation reads as "nothing is similar", which is
    // a very different statement from "this has not been indexed yet".
    h += `<div class="empty-state">
            <p><b>Nothing to compare yet.</b></p>
            <p class="muted">${d.reason === 'not indexed yet'
              ? 'No summaries or vectors exist for this line. Run the indexing '
                + 'pass in <code>search/</code>, then this fills in.'
              : 'No results came back for this line.'}</p>
            <p class="muted">Models switched on:
              ${d.models.length ? d.models.map((m) => esc(m.label)).join(', ')
                                : '<b>none</b> &mdash; turn one on in Settings'}</p>
          </div>`;
    $('similar').innerHTML = h;
    return;
  }

  h += d.results.map((r) => {
    // The score is safe to show while blind -- it says how close the match is,
    // not who found it. Only the model NAME is withheld.
    const who = Object.entries(r.by)
      .sort((a, b) => a[1].rank - b[1].rank)
      .map(([name, v]) => `${esc(name)} #${v.rank + 1} &middot; ${v.score.toFixed(3)}`)
      .join('   ');
    // The whole card opens the shabad on THIS line -- no button. The line that
    // matched is rarely the line the shabad is filed under, so it also shows
    // which shabad this is, the way the library list does.
    const own = r.line_no === r.source_line_no;
    return `
      <div class="sim-result tappable ${r.verdict > 0 ? 'voted-up' : r.verdict < 0 ? 'voted-down' : ''}"
           data-result="${r.id}" data-shabad="${r.shabad_id}"
           role="button" tabindex="0" title="Open this shabad at this line">
        <div class="sim-score" title="cosine similarity">${r.score.toFixed(3)}</div>
        ${texts(r)}
        <div class="sim-from">
          ${own ? '<span class="tag-src">this is the shabad&rsquo;s own line</span>'
                : `<span class="muted">from</span>
                   <span class="gur-sm">${esc(r.source_line)}</span>`}
          <span class="muted sim-len">${r.line_count} lines</span>
        </div>
        <div class="sim-foot">
          <button class="vote up ${r.verdict > 0 ? 'on' : ''}"
                  data-v="1" title="These genuinely connect">&#128077;</button>
          <button class="vote down ${r.verdict < 0 ? 'on' : ''}"
                  data-v="-1" title="No connection at all">&#128078;</button>
          <span class="who">${blind ? '' : who}</span>
        </div>
      </div>`;
  }).join('');

  $('similar').innerHTML = h;
}

// Delegated: the list is rebuilt after every vote, so per-node handlers would
// be dead on the second click.
$('similar').addEventListener('click', async (e) => {
  const btn = e.target.closest('.vote');
  if (!btn) {
    // Anywhere else on the card opens the shabad at this line. As in the shabad
    // page, a click that ends a text selection is someone reading, not tapping.
    const card = e.target.closest('.sim-result[data-shabad]');
    if (card && !String(window.getSelection() || '').trim()) {
      go('detail', { id: card.dataset.shabad, line: card.dataset.result });
    }
    return;
  }
  const card = btn.closest('.sim-result');
  const resultId = Number(card.dataset.result);
  const want = Number(btn.dataset.v);
  const row = simState.data.results.find((r) => r.id === resultId);
  // clicking the same thumb again clears it, so a mis-tap is undoable
  const verdict = row.verdict === want ? 0 : want;
  row.verdict = verdict;
  renderSimilar();                  // optimistic: the tap should feel instant
  try {
    await api('/api/relations', json('POST', {
      query_line_id: Number(simState.lineId),
      result_line_id: resultId,
      verdict,
    }));
  } catch (err) {
    row.verdict = 0;
    renderSimilar();
    toast('Could not save that vote: ' + err.message, true);
  }
});

$('similar').addEventListener('keydown', (e) => {
  const card = e.target.closest('.sim-result[data-shabad]');
  if (card && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    go('detail', { id: card.dataset.shabad, line: card.dataset.result });
  }
});

$('sim-blind').onchange = renderSimilar;
$('sim-back').onclick = goBack;
$('sim-open').onclick = () => {
  const q = simState.data && simState.data.query;
  if (q) go('detail', { id: q.shabad_id });
};
