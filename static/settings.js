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
  await Promise.all([loadMe(), loadPrefs(), loadModelToggles(), loadScores()]);
}

/* Who is signed in, and whether the admin-only machinery should be on screen.
 *
 * Hiding it is a courtesy, not the defence: the server refuses those endpoints
 * with a 403 whatever the page shows. A UI-only check would be security by
 * politeness, which is no security at all. */
async function loadMe() {
  try {
    const me = await api('/api/me');
    window.ME = me;
    $('me-name').textContent = me.username;
    $('me-role').textContent = me.is_admin
      ? 'Administrator — you can manage accounts and indexing'
      : 'Your library is private to this account';
    for (const el of document.querySelectorAll('[data-admin]'))
      el.hidden = !me.is_admin;
  } catch { /* the middleware would have redirected if we were not signed in */ }
}

$('sign-out').onclick = async () => {
  try { await api('/api/logout', json('POST', {})); } catch { /* going anyway */ }
  window.location.href = '/login';
};

async function loadPrefs() {
  try {
    const s = (await api('/api/settings')).settings;
    $('set-auto-index').checked = s.auto_index === '1';
    $('set-wake-lock').checked = s.wake_lock === '1';
  } catch { /* the toggles just stay as they are */ }
}

$('set-wake-lock').onchange = async (e) => {
  const on = e.target.checked;
  // Apply first, save second. The lock is what the switch is FOR, and it should
  // take effect on the tap rather than after a round trip.
  setWakeLock(on);
  try {
    await api('/api/settings/wake_lock', json('PATCH', { value: on ? '1' : '0' }));
    const st = wakeStatus();
    toast(on ? (st.state === 'active' ? 'Screen will stay awake' : st.detail)
             : 'Screen can sleep normally', on && st.state !== 'active');
  } catch (err) {
    setWakeLock(!on);
    e.target.checked = !on;
    toast('Could not save that: ' + err.message, true);
  }
};

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

/* Switching OFF is free and instant. Switching ON may cost money, so it goes
 * through the estimate dialog first -- see confirmEnable below. */
$('set-models').addEventListener('change', async (e) => {
  const box = e.target.closest('input[data-model]');
  if (!box) return;
  if (box.checked) return confirmEnable(box);
  await setEnabled(box, false);
});

async function setEnabled(box, on) {
  try {
    const r = await api('/api/models/' + encodeURIComponent(box.dataset.model),
                        json('PATCH', { enabled: on }));
    box.checked = on;
    // Switching off doubles as the stop switch: if a run was indexing this
    // model, it has just been asked to stop. Say so with the numbers, because
    // "switched off" alone would hide the more consequential half.
    if (r.stopped) {
      toast(`Switched off — stopping indexing at ${r.stopped.done.toLocaleString()}`
            + ` of ${r.stopped.total.toLocaleString()}`
            + ` ($${r.stopped.spent.toFixed(2)} spent)`);
    } else {
      toast(on ? 'Model switched on' : 'Model switched off');
    }
    loadScores();            // the unique counts depend on who is active
    return true;
  } catch (err) {
    box.checked = !on;                        // put the switch back
    toast('Could not change that: ' + err.message, true);
    return false;
  }
}

/* Price the catch-up, then ask.
 *
 * The checkbox is put BACK to off for the duration. A switch that shows "on"
 * while a dialog is still asking whether to switch it on is lying about the
 * state of the system, and if the dialog is dismissed by tapping outside or
 * pressing Escape -- neither of which runs any handler of ours -- it would stay
 * lying. Reverting first means the visible state is always the true one, and
 * the confirm path is the only thing that can change it.
 */
let enablePending = null;

async function confirmEnable(box) {
  box.checked = false;
  const name = box.dataset.model;
  const dlg = $('enable-dialog');
  $('enable-body').innerHTML = '<p class="muted">Working out what that would cost…</p>';
  $('en-go').disabled = true;
  if (!dlg.open) dlg.showModal();

  let est;
  try {
    est = await api('/api/models/' + encodeURIComponent(name) + '/estimate');
  } catch (err) {
    $('enable-body').innerHTML =
      `<p class="muted">Could not work out the cost: ${esc(err.message)}</p>`;
    return;
  }

  // Nothing outstanding: this model has already done every line. No spend, no
  // decision to make, so don't manufacture one.
  if (!est.unique) {
    dlg.close();
    await setEnabled(box, true);
    return;
  }

  enablePending = { box, name, est };
  const short = est.cost !== null && est.balance !== null && est.balance < est.cost;
  const doneLines = Math.max(0, est.total - est.lines);
  const pct = est.total ? Math.round((doneLines / est.total) * 100) : 0;

  // Three numbers, because three is what the decision actually needs: how much
  // work, what it costs, what you have. The prose version said the same thing in
  // four sentences and was slower to read.
  $('enable-body').innerHTML = `
    <div class="en-head">
      <b>${esc(nameOf(name))}</b>
      <span class="muted mono">${esc(name)}</span>
    </div>

    <div class="en-bar" role="img"
         aria-label="${pct}% of the library indexed by this model">
      <span style="width:${pct}%"></span>
    </div>
    <div class="en-bar-label">
      <span>${doneLines.toLocaleString()} indexed</span>
      <span>${est.lines.toLocaleString()} to go</span>
    </div>

    <div class="en-stats">
      <div class="en-stat">
        <b>${est.unique.toLocaleString()}</b>
        <span>to summarise</span>
      </div>
      <div class="en-stat">
        <b>${est.cost === null ? '&mdash;' : '$' + est.cost.toFixed(2)}</b>
        <span>estimated</span>
      </div>
      <div class="en-stat${short ? ' low' : ''}">
        <b>${est.balance === null ? '&mdash;' : '$' + est.balance.toFixed(2)}</b>
        <span>your credit</span>
      </div>
    </div>

    ${est.cost === null ? `<p class="warn">Price list unreachable &mdash; this will
       stop at the $0.25 automatic allowance rather than guess.</p>` : ''}
    ${short ? `<p class="warn"><b>$${(est.cost - est.balance).toFixed(2)} short.</b>
       It indexes what it can afford, then stops. Run it again later to carry on.</p>` : ''}
    ${est.busy ? `<p class="warn">Indexing already running &mdash; the catch-up
       starts on the next run.</p>` : ''}

    <p class="en-foot muted">${est.unique.toLocaleString()} unique of
      ${est.lines.toLocaleString()} &mdash; repeated tuks are summarised once.
      Runs in the background, resumes if interrupted.</p>`;
  $('en-go').disabled = false;
}

function nameOf(name) {
  const box = $('set-models').querySelector(`input[data-model="${CSS.escape(name)}"]`);
  return box ? box.closest('.set-row').querySelector('b').textContent : name;
}

$('en-go').onclick = async () => {
  const p = enablePending;
  $('enable-dialog').close();
  if (!p) return;
  if (!await setEnabled(p.box, true)) return;
  try {
    // budget: echo back exactly the figure that was on screen. The server caps
    // it to its own estimate regardless, so this can only ever ask for less.
    const r = await api('/api/models/' + encodeURIComponent(p.name) + '/index',
                        json('POST', { budget: p.est.cost }));
    toast(r.started ? 'Catching up in the background…'
                    : 'Switched on. ' + (r.reason || 'Nothing to index.'));
  } catch (err) {
    toast('Switched on, but indexing did not start: ' + err.message, true);
  }
};

$('en-cancel').onclick = () => $('enable-dialog').close();
$('en-close').onclick = () => $('enable-dialog').close();
$('enable-dialog').addEventListener('close', () => { enablePending = null; });

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
