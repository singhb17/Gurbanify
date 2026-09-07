/* Control panel: everything about this system that is normally invisible.
 *
 * The rest of the app is deliberately quiet about its machinery -- a badge says
 * "indexed" or "not indexed" and nothing more, which is right for the screens
 * you use every day. But that quietness means a run that died halfway, a lock
 * left behind by a reboot, or an embedding phase that never finished all look
 * identical from outside: a shabad that simply isn't searchable yet. This page
 * is where those become distinguishable.
 *
 * READ-ONLY, on purpose. Nothing here starts, stops or repairs anything. Where
 * something is wrong it names the command that fixes it and leaves the running
 * of it to you. Diagnosis and action are separate so that opening this page is
 * never itself a risk -- which matters most exactly when something is already
 * broken and the temptation is to press whatever is nearest.
 */

const LEVEL_ICON = { error: '&#9888;', warn: '&#9888;', info: '&#8505;' };

async function loadStatus() {
  const body = $('st-alerts');
  body.innerHTML = '<p class="muted">Checking…</p>';
  let d;
  try {
    d = await api('/api/status');
  } catch (err) {
    body.innerHTML = `<div class="alert error">
        <b>Could not read status</b>
        <div>${esc(err.message)}</div>
      </div>`;
    return;
  }
  statusCache = d;
  renderAlerts(d.alerts);
  renderAccount(d.account);
  renderModels(d.models);
  renderJobs(d.jobs);
  renderLibrary(d.library);
  renderUsers();
  renderBackups(d.backups);
  renderSystem(d);
  $('st-log').textContent = d.log.length ? d.log.join('\n') : 'Log is empty.';
  $('st-time').textContent = d.now.replace('T', ' ');
}

let statusCache = null;

function renderAlerts(alerts) {
  if (!alerts.length) {
    $('st-alerts').innerHTML = `<div class="alert ok">
        <b>&#10003; Nothing wrong</b>
        <div>Balance, indexing, backups and integrity checks all pass.</div>
      </div>`;
    return;
  }
  // Worst first: an error is something already broken, a warning is something
  // heading that way, and info is merely unusual. Reading order is priority
  // order so the top of the page is always the thing to deal with first.
  const rank = { error: 0, warn: 1, info: 2 };
  const sorted = [...alerts].sort((a, b) => rank[a.level] - rank[b.level]);
  $('st-alerts').innerHTML = sorted.map((a) => `
    <div class="alert ${a.level}">
      <b>${LEVEL_ICON[a.level] || ''} ${esc(a.title)}</b>
      <div>${esc(a.detail)}</div>
    </div>`).join('');
}

function renderAccount(acc) {
  if (!acc) {
    $('st-account').innerHTML = `<p class="muted">Could not reach OpenRouter.
      Everything else on this page is local and still accurate.</p>`;
    return;
  }
  $('st-account').innerHTML = `
    <div class="st-stats">
      ${statTile('$' + acc.remaining.toFixed(2), 'remaining',
                 acc.remaining < 1 ? 'low' : '')}
      ${statTile('$' + acc.spent.toFixed(2), 'spent')}
      ${statTile('$' + acc.purchased.toFixed(2), 'purchased')}
    </div>`;
}

const statTile = (big, label, cls) =>
  `<div class="st-stat ${cls || ''}"><b>${big}</b><span>${label}</span></div>`;

function renderModels(models) {
  if (!models.length) {
    $('st-models').innerHTML = '<p class="muted">No models registered.</p>';
    return;
  }
  $('st-models').innerHTML = models.map((m) => {
    const pct = Math.round(m.coverage * 100);
    const flags = [];
    if (m.orphan) flags.push('<span class="flag">not registered</span>');
    else if (!m.enabled) flags.push('<span class="flag">off</span>');
    if (m.summarised > m.embedded)
      flags.push(`<span class="flag bad">${fmt(m.summarised - m.embedded)} unembedded</span>`);
    if (m.malformed) flags.push(`<span class="flag bad">${fmt(m.malformed)} corrupt</span>`);
    if (m.stale) flags.push(`<span class="flag bad">${fmt(m.stale)} stale</span>`);
    return `
      <div class="st-model${m.enabled ? '' : ' off'}">
        <div class="st-model-head">
          <b>${esc(m.label)}</b>
          <span class="muted mono">${esc(m.name)}</span>
          ${flags.join('')}
          <span class="spacer"></span>
          <span class="mono">${pct}%</span>
        </div>
        <div class="en-bar"><span style="width:${pct}%"></span></div>
        <div class="st-model-nums muted">
          ${fmt(m.summarised)} summarised &middot; ${fmt(m.embedded)} embedded
          &middot; of ${fmt(m.total)} lines${m.bytes ? ' &middot; ' + mb(m.bytes) : ''}
        </div>
      </div>`;
  }).join('');
}

function renderJobs(jobs) {
  if (!jobs.length) {
    $('st-jobs').innerHTML = '<p class="muted">No indexing has been run yet.</p>';
    return;
  }
  $('st-jobs').innerHTML = `
    <table class="scores st-jobs">
      <thead><tr>
        <th>#</th><th>model</th><th>phase</th><th>state</th>
        <th>done</th><th>spent</th><th>when</th>
      </tr></thead>
      <tbody>${jobs.map((j) => `
        <tr class="job-${j.state}">
          <td class="mono">${j.id}</td>
          <td>${esc(j.model)}</td>
          <td class="muted">${esc(j.phase || '—')}</td>
          <td><span class="job-state ${j.state}">${j.state}</span></td>
          <td class="mono">${fmt(j.done)}/${fmt(j.total)}</td>
          <td class="mono">${j.spent ? '$' + j.spent.toFixed(2) : '—'}</td>
          <td class="muted">${ago(j.quiet_s)}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

function renderLibrary(c) {
  const rows = [
    ['Accounts', c.accounts],
    ['Shabads', c.shabads], ['Lines', c.lines], ['Tags', c.tags],
    ['Interested', c.shortlist], ['Learning', c.learning],
    ['Opens logged', c.history], ['Similarity votes', c.votes],
    ['Added by hand', c.user_added],
  ];
  // Gaps in the RAW layer, shown rather than filled. §12: a missing teeka stays
  // missing and visible; nothing invents one.
  $('st-library').innerHTML = `
    <div class="st-grid">
      ${rows.map(([k, v]) =>
        `<div class="st-cell"><b>${v === null ? '—' : fmt(v)}</b><span>${k}</span></div>`
      ).join('')}
    </div>
    <p class="muted st-gaps">
      ${fmt(c.no_teeka)} line${c.no_teeka === 1 ? '' : 's'} have no Punjabi teeka
      and ${fmt(c.no_english)} have no English translation. BaniDB simply does
      not carry them; the gap is left visible rather than filled in.</p>`;
}

/* Account management. Deliberately plain: three actions, each a prompt, no
 * forms to design for something used perhaps twice a year. */
async function renderUsers() {
  let d;
  try {
    d = await api('/api/admin/users');
  } catch (err) {
    $('st-users').innerHTML = `<p class="muted">Could not load: ${esc(err.message)}</p>`;
    return;
  }
  $('st-users').innerHTML = `
    <div class="st-rows">${d.users.map((u) => `
      <div class="st-row user-row" data-id="${u.id}">
        <div class="st-row-k">${u.is_admin ? 'admin' : 'account'}${
          u.id === d.me ? ' · you' : ''}</div>
        <div class="st-row-v"><b>${esc(u.username)}</b></div>
        <div class="st-row-why muted">
          ${fmt(u.shabads)} shabads ·
          ${u.sessions ? u.sessions + ' signed-in device' + (u.sessions === 1 ? '' : 's')
                       : 'not signed in'} ·
          since ${esc((u.created_at || '').slice(0, 10))}
        </div>
        <div class="user-actions">
          <button class="secondary u-pass" data-id="${u.id}"
                  data-name="${esc(u.username)}">Reset password</button>
          ${u.id === d.me ? ''
            : `<button class="danger u-del" data-id="${u.id}"
                       data-name="${esc(u.username)}">Delete</button>`}
        </div>
      </div>`).join('')}
    </div>`;
}

$('st-add-user').onclick = async () => {
  const name = prompt('Username for the new account:');
  if (!name) return;
  const pw = prompt(`Password for ${name} (at least 8 characters):`);
  if (!pw) return;
  try {
    await api('/api/admin/users', json('POST', { username: name, password: pw }));
    toast(`Created ${name}`);
    renderUsers();
  } catch (err) { toast(err.message, true); }
};

$('st-users').addEventListener('click', async (e) => {
  const pass = e.target.closest('.u-pass');
  if (pass) {
    const pw = prompt(`New password for ${pass.dataset.name} `
                    + '(at least 8 characters).\n\nThis signs them out everywhere.');
    if (!pw) return;
    try {
      await api(`/api/admin/users/${pass.dataset.id}/password`,
                json('POST', { password: pw }));
      toast('Password changed; they will need to sign in again');
      renderUsers();
    } catch (err) { toast(err.message, true); }
    return;
  }
  const del = e.target.closest('.u-del');
  if (del) {
    // The one genuinely destructive button in the app, so it asks with the
    // name typed back -- a mis-tap cannot delete somebody's library.
    const typed = prompt(
      `Delete ${del.dataset.name} and EVERYTHING in their library — shabads, `
      + 'tags, notes, shortlist, learning list, votes?\n\n'
      + 'The Gurbani itself is kept, since others may have the same shabads.\n\n'
      + `Type the username to confirm:`);
    if (typed !== del.dataset.name) {
      if (typed !== null) toast('Name did not match; nothing deleted');
      return;
    }
    try {
      await api('/api/admin/users/' + del.dataset.id, { method: 'DELETE' });
      toast(`Deleted ${del.dataset.name}`);
      renderUsers();
      loadStatus();
    } catch (err) { toast(err.message, true); }
  }
});

function renderBackups(b) {
  $('st-backups').innerHTML = b.age_days === null
    ? `<p class="muted">Nothing in <span class="mono">backups/</span> yet.</p>`
    : `<div class="st-stats">
         ${statTile(b.age_days < 1 ? 'today' : b.age_days.toFixed(0) + 'd',
                    'since last', b.age_days > 7 ? 'low' : '')}
         ${statTile(fmt(b.count), 'kept')}
         ${statTile(mb(b.bytes), 'newest')}
       </div>
       <p class="muted st-gaps mono">${esc(b.newest)}</p>`;
}

$('st-backup-run').onclick = async () => {
  const btn = $('st-backup-run');
  const out = $('st-backup-out');
  btn.disabled = true;
  $('st-backup-note').textContent = 'Copying the database…';
  out.hidden = true;
  try {
    const r = await api('/api/backup', json('POST', {}));
    out.textContent = r.output;
    out.hidden = false;
    $('st-backup-note').textContent = '';
    toast(r.ok ? 'Backed up' : 'Backup reported a problem', !r.ok);
    // The alert and the age tile were both computed before this ran, so the
    // page is now out of date about the one thing it just changed.
    await loadStatus();
    checkAlerts();
  } catch (err) {
    $('st-backup-note').textContent = '';
    out.textContent = 'Failed: ' + err.message;
    out.hidden = false;
    toast('Backup failed: ' + err.message, true);
  } finally {
    btn.disabled = false;
  }
};

function renderSystem(d) {
  const rows = [
    ['Prompt version', `<span class="mono">${esc(d.prompt_ver || 'unknown')}</span>`,
     'Summaries written under a different one count as stale.'],
    ['Auto-index', d.auto_index ? 'On' : 'Off',
     'Whether adding a shabad starts a background pass.'],
    // Read from the live browser, not the server: whether the screen is
    // actually being held awake is a fact about THIS device and this tab, and
    // the server cannot see it. When it fails the reason is otherwise
    // invisible, and "needs https" is a very different problem from "your
    // browser refused".
    // esc: unlike the other rows, this text can carry a browser error message.
    ['Screen wake lock', wakeLabel(), esc(wakeStatus().detail)],
    ['Indexer lock', d.lock.held
      ? `Held ${ago(d.lock.age_s)}${d.lock.stale ? ' — stale' : ''}` : 'Free',
     'Only one indexing run may exist at a time, machine-wide.'],
    ['Library database', mb(d.storage.library_db),
     `Includes ${mb(d.storage.vectors)} of vectors.`],
    ['BaniDB corpus', mb(d.storage.corpus_db), 'Re-fetchable; not backed up.'],
  ];
  $('st-system').innerHTML = `
    <div class="st-rows">${rows.map(([k, v, why]) => `
      <div class="st-row">
        <div class="st-row-k">${k}</div>
        <div class="st-row-v">${v}</div>
        <div class="st-row-why muted">${why}</div>
      </div>`).join('')}
    </div>`;
}

const WAKE_LABEL = {
  active: 'Holding — screen will not sleep',
  off: 'Off',
  blocked: 'Blocked — not an https address',
  unsupported: 'Not supported by this browser',
  idle: 'Not held',
};
const wakeLabel = () => {
  const s = wakeStatus();
  const cls = s.state === 'active' ? 'ok' : s.state === 'off' ? '' : 'warn-text';
  return `<span class="${cls}">${esc(WAKE_LABEL[s.state] || s.state)}</span>`;
};

const fmt = (n) => (n === null || n === undefined) ? '—' : Number(n).toLocaleString();
const mb = (n) => (n === null || n === undefined) ? '—'
  : n < 1e6 ? (n / 1e3).toFixed(0) + ' KB' : (n / 1e6).toFixed(1) + ' MB';

function ago(secs) {
  if (secs === null || secs === undefined) return '—';
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.round(secs / 60) + 'm ago';
  if (secs < 86400) return Math.round(secs / 3600) + 'h ago';
  return Math.round(secs / 86400) + 'd ago';
}

$('st-refresh').onclick = () => loadStatus();
$('set-status-link').onclick = () => go('status');

/* The gear grows a dot when something needs attention.
 *
 * This is the price of keeping the page out of the nav: if nothing ever points
 * at it, a stalled run or a week-old backup sits unnoticed until the next time
 * curiosity strikes. Checked once at startup rather than polled -- these are
 * slow-moving conditions, and a background request every few seconds to report
 * that the backup is still fine would cost more than it is worth.
 */
async function checkAlerts() {
  try {
    const d = await api('/api/status');
    const bad = d.alerts.filter((a) => a.level !== 'info').length;
    $('nav-settings').classList.toggle('has-alert', bad > 0);
    if (bad) $('nav-settings').title = `Settings — ${bad} thing(s) need attention`;
  } catch { /* the dot simply doesn't appear */ }
}
setTimeout(checkAlerts, 1500);     // let the library paint first
