/* Settings: which summariser models contribute, and how they are scoring.
 *
 * Switching a model off is not deleting it (CLAUDE.md §3). Its summaries and
 * every vote already cast against it survive, so a model that looks weak after
 * a fortnight can be switched off and back on again with nothing lost.
 *
 * The scores here come from my own thumbs on the Similar screen, not from the
 * bench in search/. The bench measures agreement with a grouping I invented;
 * this measures connections I actually endorsed on the real library, which is
 * the thing the bench cannot see.
 */

async function loadSettings() {
  await Promise.all([loadPrefs(), loadModelToggles(), loadScores()]);
}

async function loadPrefs() {
  try {
    const s = (await api('/api/settings')).settings;
    $('set-auto-index').checked = s.auto_index === '1';
  } catch { /* the toggle just stays as it is */ }
}

$('set-auto-index').onchange = async (e) => {
  const on = e.target.checked;
  try {
    await api('/api/settings/auto_index', json('PATCH', { value: on ? '1' : '0' }));
    toast(on ? 'New shabads will be indexed automatically' : 'Automatic indexing off');
  } catch (err) {
    e.target.checked = !on;
    toast('Could not save that: ' + err.message, true);
  }
};

async function loadModelToggles() {
  let models;
  try {
    models = (await api('/api/models')).models;
  } catch (err) {
    $('set-models').innerHTML = `<p class="muted">Could not load: ${esc(err.message)}</p>`;
    return;
  }
  if (!models.length) {
    $('set-models').innerHTML = '<p class="muted">No models registered.</p>';
    return;
  }
  $('set-models').innerHTML = models.map((m) => `
    <label class="set-row">
      <input type="checkbox" data-model="${esc(m.name)}" ${m.enabled ? 'checked' : ''}>
      <div>
        <b>${esc(m.label)}</b>
        <div class="muted mono">${esc(m.name)}</div>
      </div>
    </label>`).join('');
}

$('set-models').addEventListener('change', async (e) => {
  const box = e.target.closest('input[data-model]');
  if (!box) return;
  try {
    await api('/api/models/' + encodeURIComponent(box.dataset.model),
              json('PATCH', { enabled: box.checked }));
    toast(box.checked ? 'Model switched on' : 'Model switched off');
    loadScores();            // the unique counts depend on who is active
  } catch (err) {
    box.checked = !box.checked;               // put the switch back
    toast('Could not change that: ' + err.message, true);
  }
});

async function loadScores() {
  let data;
  try {
    data = await api('/api/scores');
  } catch (err) {
    $('set-scores').innerHTML = `<p class="muted">Could not load: ${esc(err.message)}</p>`;
    return;
  }
  if (!data.votes) {
    $('set-scores').innerHTML = `
      <div class="empty-state">
        <p><b>No votes yet.</b></p>
        <p class="muted">Open a shabad, press <b>Similar</b> on a line, and thumb
          the results. Scores appear here once there is something to count.</p>
      </div>`;
    return;
  }
  $('set-scores').innerHTML = `
    <p class="muted">${data.votes} line pair${data.votes === 1 ? '' : 's'} judged</p>
    <table class="scores">
      <thead><tr>
        <th>model</th><th>&#128077;</th><th>&#128078;</th>
        <th>score</th><th>unique &#128077;/&#128078;</th>
      </tr></thead>
      <tbody>${data.models.map((m) => `
        <tr class="${m.enabled ? '' : 'off'}">
          <td>${esc(m.label)}</td>
          <td>${m.up}</td>
          <td>${m.down}</td>
          <td>${m.score === null ? '&mdash;' : Math.round(m.score * 100) + '%'}</td>
          <td>${m.unique_up}/${m.unique_down}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}
