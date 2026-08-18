let currentQuotation = null;
let selectedRating = null;
let usdToInr = 1; // USD column removed for now — prices shown as-is in INR (no conversion)
let currentUser = null;

const API = window.location.origin;

// FastAPI returns `detail` as a plain string for HTTPException, but as an
// ARRAY of {loc, msg, type} objects for request-validation (422) errors.
// Interpolating that array straight into a template renders the useless
// "[object Object],[object Object]" — so unpack it into readable text.
function apiErr(body, fallback = 'Request failed') {
  const d = body && body.detail;
  if (!d) return (body && body.message) || fallback;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map(e => {
      const field = Array.isArray(e.loc) ? e.loc[e.loc.length - 1] : '';
      return field ? `${field}: ${e.msg}` : e.msg;
    }).join('; ');
  }
  return d.msg || fallback;
}

// A 401 anywhere means the 8-hour session lapsed. Every loader assumed its
// fetch returned data, so an expired session produced a silently broken page
// rather than a prompt — loadRepository() died on "all.filter is not a
// function" with nothing on screen and only a console trace to show for it.
// One interceptor covers every caller, including ones not written yet, which
// is a smaller and more durable fix than a guard in each of twenty loaders.
(function () {
  const realFetch = window.fetch;
  window.fetch = async function (input, init) {
    const res = await realFetch(input, init);
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    // /api/auth/* answers 401 for a wrong password too — that is a failed
    // login attempt, not a lapsed session, and must not bounce the user.
    if (res.status === 401 && !/\/api\/auth\//.test(url)
        && document.body.classList.contains('authed')) {
      sessionExpired();
    }
    return res;
  };
})();

let _sessionGone = false;

function sessionExpired() {
  if (_sessionGone) return;   // one notice, not one per parallel fetch
  _sessionGone = true;
  currentUser = null;
  document.body.classList.remove('authed');
  if (typeof showLoginForm === 'function') showLoginForm();
  const err = document.getElementById('login-err');
  if (err) err.textContent = 'Your session expired. Log in again.';
}

// Returns [] for anything that is not a list, so a loader renders an empty
// state instead of throwing partway through and leaving the page half-built.
function asList(x) {
  return Array.isArray(x) ? x : [];
}

// These inputs live outside any <form> — in some browsers, Enter inside a
// loose (non-form) input can still trigger an implicit page reload/flicker.
// Blocking it here (and committing the value via blur, which fires
// whatever onchange handler the input already has) avoids that entirely.
function stopEnterSubmit(event) {
  if (event.key === 'Enter') {
    event.preventDefault();
    event.target.blur();
  }
}

// ── Auth ────────────────────────────────────────────────────────────────────
async function checkAuth() {
  try {
    const res = await fetch(`${API}/api/auth/me`);
    if (res.status === 200) {
      currentUser = await res.json();
      onAuthed();
      return;
    }
  } catch (e) {}
  currentUser = null;
  document.body.classList.remove('authed');
}

function onAuthed() {
  _sessionGone = false;   // re-armed, so the next lapse notifies again
  document.body.classList.add('authed');
  const nav = document.getElementById('nav-user');
  const showName = (currentUser.name || '').trim().toLowerCase() !== (currentUser.role || '').toLowerCase();
  nav.innerHTML = `${showName ? `<span>${currentUser.name}</span>` : ''}<span class="role-badge">${currentUser.role}</span><button onclick="doLogout()">Log out</button>`;
  // Only Admins manage the BOQ catalog — hide the tab for everyone else.
  document.getElementById('tab-upload').style.display = currentUser.role === 'admin' ? '' : 'none';
  // Master Table is viewable by everyone, but only Admins can import/replace it.
  document.getElementById('master-admin-upload').style.display = currentUser.role === 'admin' ? '' : 'none';
  // Activity log is admin-only — the endpoint enforces it too, this just
  // avoids showing a tab that would 403.
  document.getElementById('tab-audit').style.display = currentUser.role === 'admin' ? '' : 'none';
  document.getElementById('tab-users').style.display = currentUser.role === 'admin' ? '' : 'none';
  const td = document.getElementById('tab-dedupe');
  if (td) td.style.display = currentUser.role === 'admin' ? '' : 'none';
  // Margin analysis exposes purchase cost — the endpoint is admin-only, so
  // don't offer employees a button that would 403.
  document.getElementById('margin-analyse-btn').style.display =
    currentUser.role === 'admin' ? '' : 'none';
  // Cost & margin toggle on the quote table — same admin-only rule; the
  // server strips cost from employee payloads either way.
  const smw = document.getElementById('show-margin-wrap');
  if (smw) smw.style.display = currentUser.role === 'admin' ? '' : 'none';
  const su = document.getElementById('side-user');
  if (su) su.innerHTML = `<div class="savatar">${(currentUser.name || '?')[0].toUpperCase()}</div>
    <div><b>${(currentUser.name || '').split(' ')[0]}</b><small>${currentUser.role}</small></div>`;
  show('dashboard');
}

async function doLogin(evt) {
  evt.preventDefault();
  const email = document.getElementById('login-email').value.trim();
  const password = document.getElementById('login-password').value;
  const errBox = document.getElementById('login-err');
  errBox.textContent = '';
  try {
    const res = await fetch(`${API}/api/auth/login`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password})
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      errBox.textContent = body.detail || 'Login failed';
      return;
    }
    currentUser = await res.json();
    document.getElementById('login-password').value = '';
    onAuthed();
  } catch (e) {
    errBox.textContent = 'Could not reach the server';
  }
}

async function doLogout() {
  await fetch(`${API}/api/auth/logout`, {method: 'POST'});
  currentUser = null;
  document.body.classList.remove('authed');
  document.getElementById('login-email').value = '';
  document.getElementById('login-password').value = '';
}

function getResetTokenFromUrl() {
  return new URLSearchParams(window.location.search).get('reset_token');
}

const LOGIN_GATE_FORMS = ['login-form', 'forgot-form', 'reset-form', 'register-form'];

function _showOnlyForm(idToShow) {
  LOGIN_GATE_FORMS.forEach(id => {
    document.getElementById(id).style.display = (id === idToShow) ? '' : 'none';
  });
}

function showLoginForm(evt) {
  if (evt) evt.preventDefault();
  _showOnlyForm('login-form');
}

function showForgotForm(evt) {
  if (evt) evt.preventDefault();
  _showOnlyForm('forgot-form');
  document.getElementById('forgot-err').textContent = '';
  document.getElementById('forgot-msg').textContent = '';
}

function showResetForm() {
  _showOnlyForm('reset-form');
}

function showRegisterForm(evt) {
  if (evt) evt.preventDefault();
  _showOnlyForm('register-form');
  document.getElementById('register-err').textContent = '';
}

async function doForgotPassword(evt) {
  evt.preventDefault();
  const email = document.getElementById('forgot-email').value.trim();
  const errBox = document.getElementById('forgot-err');
  const msgBox = document.getElementById('forgot-msg');
  errBox.textContent = '';
  msgBox.textContent = '';
  try {
    const res = await fetch(`${API}/api/auth/forgot-password`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email})
    });
    const body = await res.json().catch(() => ({}));
    msgBox.textContent = body.message || 'If that email is registered, a reset link has been sent.';
  } catch (e) {
    errBox.textContent = 'Could not reach the server';
  }
}

async function doResetPassword(evt) {
  evt.preventDefault();
  const pw = document.getElementById('reset-password').value;
  const pw2 = document.getElementById('reset-password-confirm').value;
  const errBox = document.getElementById('reset-err');
  const msgBox = document.getElementById('reset-msg');
  errBox.textContent = '';
  msgBox.textContent = '';
  if (pw !== pw2) {
    errBox.textContent = 'Passwords do not match';
    return;
  }
  if (pw.length < 8) {
    errBox.textContent = 'Password must be at least 8 characters';
    return;
  }
  const token = getResetTokenFromUrl();
  try {
    const res = await fetch(`${API}/api/auth/reset-password`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token, new_password: pw})
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      errBox.textContent = body.detail || 'Reset failed';
      return;
    }
    msgBox.textContent = body.message || 'Password reset — please log in.';
    // Drop the token from the URL so a page refresh doesn't re-show this form.
    window.history.replaceState({}, '', window.location.pathname);
    setTimeout(showLoginForm, 1500);
  } catch (e) {
    errBox.textContent = 'Could not reach the server';
  }
}

async function doRegister(evt) {
  evt.preventDefault();
  const name = document.getElementById('register-name').value.trim();
  const email = document.getElementById('register-email').value.trim();
  const password = document.getElementById('register-password').value;
  const errBox = document.getElementById('register-err');
  errBox.textContent = '';
  try {
    const res = await fetch(`${API}/api/auth/register`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, email, password})
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      errBox.textContent = body.detail || 'Sign up failed';
      return;
    }
    // Registration logs the new account straight in — same shape as /login.
    currentUser = body;
    document.getElementById('register-password').value = '';
    onAuthed();
  } catch (e) {
    errBox.textContent = 'Could not reach the server';
  }
}

// Rate is fixed (no live fetch) so quotations stay consistent between sessions.
async function fetchUsdRate() { /* fixed rate; intentionally no-op */ }

window.addEventListener('DOMContentLoaded', () => {
  if (getResetTokenFromUrl()) {
    // A password-reset link always shows the reset form, regardless of
    // whether there's already a valid session — skip the normal auth check.
    showResetForm();
  } else {
    checkAuth();
  }
  // Scroll-reveal for landing-page elements
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { threshold: .12 });
    document.querySelectorAll('.reveal').forEach(el => io.observe(el));
  } else {
    document.querySelectorAll('.reveal').forEach(el => el.classList.add('in'));
  }
});

// Fill the requirement box from a one-click example, then scroll to Generate
function useExample(btn) {
  const ta = document.getElementById('req-prompt');
  ta.value = btn.textContent.trim();
  ta.focus();
  document.getElementById('gen-btn').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// ── Navigation ──────────────────────────────────────────────────────────────
function show(tab) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.snav').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === tab));
  document.getElementById('sec-' + tab).classList.add('active');
  window.scrollTo({ top: 0, behavior: 'instant' in document.documentElement.style ? 'instant' : 'auto' });
  if (tab === 'upload')    { loadCatalog(); loadUploadedFiles(); }
  if (tab === 'master')    { if (!masterSummary.length) loadMasterTable(); }
  if (tab === 'generate')  { document.getElementById('sec-generate').classList.remove('boq-only'); loadCatalogSelector(); }
  if (tab === 'repository') { window._marginOnly = false; loadRepository(); }
  if (tab === 'audit')     loadAuditLog();
  if (tab === 'users')     loadUsers();
  if (tab === 'dedupe')    loadDedupe();
  if (tab === 'dashboard') loadDashboard();
}

// ── Dashboard ────────────────────────────────────────────────────────────────
function fmtINR(n) { return '₹' + (n || 0).toLocaleString('en-IN', {maximumFractionDigits: 0}); }

async function loadDashboard() {
  const hello = document.getElementById('dash-hello');
  if (hello && currentUser) {
    const h = new Date().getHours();
    const part = h < 12 ? 'morning' : h < 17 ? 'afternoon' : 'evening';
    hello.textContent = `Good ${part}, ${(currentUser.name || '').split(' ')[0]} 👋`;
    const sub = document.querySelector('.dash-sub');
    if (sub) sub.textContent = new Date().toLocaleDateString('en-IN',
      { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
      + ' · every number on this page is live from the database';
  }
  const view = document.getElementById('dash-view');
  try {
    const res = await fetch(`${API}/api/dashboard`);
    const d = await res.json();
    if (!res.ok) { view.innerHTML = `<div class="alert alert-error">${apiErr(d)}</div>`; return; }
    renderDashboard(d);
  } catch (e) {
    view.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
}

// Tiny inline visuals for the stat tiles — no chart library.
function sparkline(vals, color) {
  if (!vals || vals.length < 2) return '';
  const w = 120, h = 28, mx = Math.max(...vals, 1);
  const pts = vals.map((v, i) =>
    `${(i * w / (vals.length - 1)).toFixed(1)},${(h - 3 - (v / mx) * (h - 6)).toFixed(1)}`).join(' ');
  return `<svg class="dviz" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${pts.split(' ').pop().split(',')[0]}" cy="${pts.split(' ').pop().split(',')[1]}" r="2.5" fill="${color}"/></svg>`;
}
function meterbar(pct, color) {
  return `<div class="dmeter"><div style="width:${Math.max(2, Math.min(100, pct))}%;background:${color}"></div></div>`;
}
function minibars(rows, color) {
  if (!rows || !rows.length) return '';
  const mx = Math.max(...rows.map(r => r.n), 1);
  return `<div class="dbars">${rows.map(r =>
    `<i title="${r.name}: ${r.n}" style="height:${Math.max(12, r.n * 100 / mx)}%;background:${color}"></i>`).join('')}</div>`;
}

function renderDashboard(d) {
  const view = document.getElementById('dash-view');
  const stat = (chipBg, icon, label, value, sub, viz) => `
    <div class="dstat">
      <div class="dhead"><span class="dchip" style="background:${chipBg}">${icon}</span>
        <span class="dlbl">${label}</span></div>
      <div class="dval">${value}</div><div class="dsub">${sub || ''}</div>
      ${viz || ''}
    </div>`;

  const imgPct = Math.round(d.images * 100 / Math.max(d.products, 1));
  let stats = stat('var(--accent-soft2)', '📄', d.is_admin ? 'Quotations' : 'My Quotations',
                   d.quotes, 'in the repository', sparkline(d.quotes_spark, '#6366f1'))
            + stat('var(--blue-soft)', '📦', 'Products in Master', d.products.toLocaleString('en-IN'),
                   `${d.catalogues} catalogues`, minibars(d.cat_bars, '#3b82f6'))
            + stat('var(--blue-soft)', '🖼', 'Product Images',
                   d.images.toLocaleString('en-IN'),
                   `${imgPct}% of catalogue`, meterbar(imgPct, '#3b82f6'));
  if (d.is_admin) {
    stats += stat('var(--green-soft2)', '₹', 'Avg. Margin',
                  d.avg_margin != null ? d.avg_margin + '%' : '—', 'on priced products',
                  d.avg_margin != null ? meterbar(d.avg_margin, '#16a34a') : '');
    if (d.coverage) {
      const covPct = Math.round(d.coverage.found * 100 / Math.max(d.coverage.total, 1));
      stats += stat('var(--amber-soft2)', '☑', 'BOQ Coverage', covPct + '%',
                    `last check: ${d.coverage.found}/${d.coverage.total} stocked`,
                    meterbar(covPct, covPct < 30 ? '#dc2626' : covPct < 70 ? '#f59e0b' : '#16a34a'));
    }
    stats += stat('var(--pink-soft2)', '🧠', 'Learning',
                  d.learned + d.mappings,
                  `${d.learned} corrections · ${d.mappings} mappings`);
  }

  const recent = (d.recent || []).map(q => `
    <div class="drow" onclick="viewQuote(${q.id})" title="Open this quotation">
      <span class="dico">📋</span>
      <span><b>${q.ref_no}</b><small>${q.n} item${q.n === 1 ? '' : 's'}${q.client_name ? ' · ' + q.client_name : ''}</small></span>
      <span class="dright"><b>${fmtINR(q.total)}</b><br>
        <span class="badge badge-${q.status}">${q.status}</span></span>
    </div>`).join('') ||
    '<div class="empty-state"><span class="es-icon">📝</span><div class="es-title">No quotations yet</div><div class="es-hint">Generate your first one.</div></div>';

  const activity = d.is_admin ? `
    <div class="dcard">
      <h3>Activity <a href="#" onclick="show('audit');return false">View all</a></h3>
      ${(d.activity || []).map(a => `
        <div class="drow" style="cursor:default">
          <span class="dico">◷</span>
          <span><b>${(typeof AUDIT_LABELS !== 'undefined' && AUDIT_LABELS[a.action]) || a.action}</b>
            <small>${a.user_name || 'system'} · ${String(a.target || '').slice(0, 34)}</small></span>
        </div>`).join('')}
    </div>` : '';

  const qa = `
    <div class="dcard">
      <h3>Quick Actions</h3>
      <div class="dqa">
        <button onclick="showBoqCoverage()">☑ Check What We Stock<small>BOQ coverage</small></button>
        <button onclick="show('generate')">⚡ Generate a Quotation<small>plain English or BOQ</small></button>
        ${d.is_admin ? `<button onclick="showMargins()">◔ Margin Analysis<small>cost & profit</small></button>
        <button onclick="show('audit')">◷ Activity Log<small>audit & history</small></button>` : `
        <button onclick="show('master')">📦 Browse Master Catalogue<small>${d.products.toLocaleString('en-IN')} products</small></button>
        <button onclick="show('repository')">🗂 My Quotations<small>drafts & approved</small></button>`}
      </div>
    </div>`;

  const smartImport = `
    <div class="dcard">
      <h3>Smart Import</h3>
      <div class="ddrop" id="dash-drop"
           ondragover="event.preventDefault();this.classList.add('drag')"
           ondragleave="this.classList.remove('drag')"
           ondrop="event.preventDefault();this.classList.remove('drag');dashFileChosen(event.dataTransfer.files[0])"
           onclick="if(!window._dashBusy)document.getElementById('dash-file-input').click()">
        <div id="dash-stage">${window._dashSaved ? `<div class="dfade">${window._dashSaved}</div>` : dashIdleHTML()}</div>
        <input type="file" id="dash-file-input" accept=".xls,.xlsx" style="display:none"
               onchange="dashFileChosen(this.files[0]);this.value=''">
      </div>
      <div class="dsteps" id="dash-steps">
        <div class="dstep" data-s="detect"><span class="ddot">1</span>Detect</div>
        <div class="dstep" data-s="route"><span class="ddot">2</span>Route</div>
        <div class="dstep" data-s="map"><span class="ddot">3</span>Map</div>
        <div class="dstep" data-s="approve"><span class="ddot">4</span>Approve</div>
        <div class="dstep" data-s="import"><span class="ddot">5</span>Import</div>
      </div>
      <div id="dash-si-status" style="margin-top:10px;"></div>
    </div>`;

  view.innerHTML = `
    <div class="dstats">${stats}</div>
    <div class="dgrid">
      ${smartImport}
      <div class="dcard">
        <h3>Recent Quotations <a href="#" onclick="show('repository');return false">View all</a></h3>
        ${recent}
      </div>
      ${qa}
      ${activity}
    </div>`;
  // Re-apply the stepper ticks for a restored in-flight/finished flow.
  Object.entries(window._dashStepState || {}).forEach(([n, s]) => dashStep(n, s));
}

// Smart Import (dashboard): detect what a workbook is, then hand it to the
// flow that owns that kind of file. Detection is Phase E server-side; the
// routing here only ever passes the File object into the existing, tested
// entry points — no second upload path to keep in sync.
function dashStep(name, state) {
  window._dashStepState = window._dashStepState || {};
  window._dashStepState[name] = state;
  const el = document.querySelector(`#dash-steps .dstep[data-s="${name}"]`);
  if (!el) return;
  el.classList.remove('now', 'done');
  if (state) el.classList.add(state);
  const dot = el.querySelector('.ddot');
  if (dot) dot.textContent = state === 'done' ? '✓'
    : ['detect','route','map','approve','import'].indexOf(name) + 1;
}

function dashIdleHTML() {
  return `
    <div class="ddoc2"><span class="dtile">${DSVG_DOC}</span><i class="dspark s1">✦</i><i class="dspark s2">✦</i><i class="dspark s3">＋</i></div>
    <b>Drop your BOQ, Price List or Quotation</b>
    <small class="dsub-idle">The file type is detected and routed to the right flow</small>
    <small class="dsub-drag">Release to upload the file</small><br>
    <button class="btn btn-primary" style="margin-top:12px"
            onclick="event.stopPropagation();document.getElementById('dash-file-input').click()">⇪ Upload File</button>`;
}

// Fake-but-honest progress: creeps to 90% while the real request runs, jumps
// to 100% when it resolves. Never shows "done" before the server said so.
function dashProgHTML(id, pct) {
  return `<div class="dprogc"><div class="dprog" id="${id}"></div></div>${pct ? `<span class="dpct" id="${id}-pct">0%</span>` : ''}`;
}
function dashProgRun(id) {
  let p = 0;
  return setInterval(() => {
    const el = document.getElementById(id);
    if (!el) return;
    p = Math.min(90, p + 4 + Math.random() * 10);
    el.style.width = p + '%';
    const l = document.getElementById(id + '-pct');
    if (l) l.textContent = Math.round(p) + '%';
  }, 200);
}
function dashProgDone(timer, id) {
  clearInterval(timer);
  const el = document.getElementById(id);
  if (el) el.style.width = '100%';
  const l = document.getElementById(id + '-pct');
  if (l) l.textContent = '100%';
}

// Swap the drop zone's content for the current flow state. Buttons inside
// must stopPropagation so a click doesn't reopen the file picker.
function dashStage(html) {
  // Remembered so leaving the dashboard (e.g. "View full report") and coming
  // back re-renders the same state instead of a blank drop zone.
  window._dashSaved = html;
  const s = document.getElementById('dash-stage');
  if (s) s.innerHTML = `<div class="dfade">${html}</div>`;
}
// Inline SVGs — emoji glyphs render grey/tofu on Windows, these don't.
const DSVG_DOC = `<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>`;
const DSVG_SHUFFLE = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg>`;

function escHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function dashChip(file, spin) {
  return `<div class="dfchip"><span class="dfic">XLS</span>
    <span class="dfchip-nm"><b>${escHtml(file.name)}</b><small>${(file.size / 1048576).toFixed(1)} MB</small></span>
    <span class="dfchip-rt">${spin ? '<span class="dspin"></span>' : '<span class="dok">✓</span>'}</span></div>`;
}
function dashReset() {
  window._dashBusy = false;
  ['detect', 'route', 'map', 'approve', 'import'].forEach(x => dashStep(x, null));
  dashStage(dashIdleHTML());
  window._dashSaved = null;
  window._dashStepState = null;
  const st = document.getElementById('dash-si-status');
  if (st) st.innerHTML = '';
}
function dashFail(msg) {
  window._dashBusy = false;
  dashStage(`<div class="dmsg err">${msg}</div>
    <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();dashReset()">↩ Try another file</button>`);
}

async function dashFileChosen(file) {
  if (!file || window._dashBusy) return;
  if (!/\.xlsx?$/i.test(file.name)) { dashFail('Only .xls / .xlsx files are supported.'); return; }
  window._dashBusy = true;
  window._dashFile = file;
  document.getElementById('dash-si-status').innerHTML = '';
  ['detect', 'route', 'map', 'approve', 'import'].forEach(x => dashStep(x, null));

  // State 3: file received — quick fill of the progress bar, then detect.
  dashStage(`${dashChip(file, false)}<div class="dmsg ok">File uploaded successfully!</div>${dashProgHTML('dash-up')}`);
  setTimeout(() => { const b = document.getElementById('dash-up'); if (b) b.style.width = '100%'; }, 30);

  setTimeout(async () => {
    dashStep('detect', 'now');
    dashStage(`${dashChip(file, true)}
      <div class="dmsg">Detecting file type and structure…</div>
      <div class="ddots"><i></i><i></i><i></i></div>`);
    try {
      const fd = new FormData(); fd.append('file', file);
      const res = await fetch(`${API}/api/detect-file`, { method: 'POST', body: fd });
      const d = await res.json();
      if (!res.ok) { dashStep('detect', null); dashFail(apiErr(d)); return; }
      dashStep('detect', 'done');
      dashStep('route', 'now');
      // State 5: routing animation — doc → switch → destination.
      dashStage(`${dashChip(file, true)}
        <div class="dmsg">Routing to the right flow…</div>
        <div class="droute"><span class="drico">${DSVG_DOC}</span><span class="drdash"></span>
          <span class="drico hub">${DSVG_SHUFFLE}</span><span class="drdash"></span><span class="drico tgt"><i></i></span></div>`);
      setTimeout(() => dashRouteFile(d, file), 900);
    } catch (e) {
      dashStep('detect', null); dashFail(e.message);
    }
  }, 650);
}

function dashRouteFile(d, file) {
  const isAdmin = currentUser && currentUser.role === 'admin';
  const st = document.getElementById('dash-si-status');
  dashStep('route', 'done');
  if (d.type === 'price_list') {
    if (!isAdmin) {
      dashFail('This looks like a <b>supplier price list</b> — only an admin can import those into the Master Table.');
      return;
    }
    dashMapScan(file, d.label);
  } else if (d.type === 'client_boq' || d.type === 'quotation') {
    const extra = d.type === 'quotation'
      ? '<br><small>It looks like a quotation rather than a requirement sheet — the coverage screen will show the same warning.</small>'
      : '';
    window._dashBusy = false;
    dashStage(`
      <div class="dmsg ok">✅ Detected: <b>${d.label}</b>${extra}</div>
      <div style="display:flex;gap:8px;justify-content:center;margin-top:10px;">
        <button class="btn btn-sm btn-primary"
          onclick="event.stopPropagation();dashBoqCheck()">☑ Check what we stock</button>
        <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();dashReset()">↩ Cancel</button>
      </div>`);
  } else {
    dashStage(`
      <div class="dmsg">Couldn't tell what this file is. Where should it go?</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:8px;">
        ${isAdmin ? `<button class="btn btn-sm btn-primary" onclick="event.stopPropagation();dashMapScan(window._dashFile,'Master Table file')">Master Table import</button>` : ''}
        <button class="btn btn-sm btn-accent" onclick="event.stopPropagation();dashReset();setBoqReqFile(window._dashFile);showBoqCoverage()">BOQ coverage check</button>
        <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();dashReset()">↩ Cancel</button>
      </div>`);
  }
}

// Price list (admin): the whole flow stays inside the card — scan the file
// (Map), show what was found (Approve), then import right here.
async function dashMapScan(file, label) {
  dashStep('map', 'now');
  dashStage(`${dashChip(file, true)}
    <div class="dmsg">Detected: <b>${label}</b> — mapping columns with your fields…</div>
    <div style="display:flex;align-items:center;gap:8px">${dashProgHTML('dash-map', true)}</div>`);
  const timer = dashProgRun('dash-map');
  try {
    const fd = new FormData(); fd.append('file', file);
    const res = await fetch(`${API}/api/master-table/scan`, { method: 'POST', body: fd });
    const d = await res.json();
    dashProgDone(timer, 'dash-map');
    if (!res.ok) { dashStep('map', null); dashFail(apiErr(d)); return; }
    dashStep('map', 'done');
    const review = `event.stopPropagation();dashReset();setMasterFiles([window._dashFile]);show('master');setTimeout(scanMasterFiles,150)`;
    if (d.total_products > 0 && d.priced_products === 0) {
      dashFail(`<b>No price column was detected</b> — every product would import at ₹0.
        Teach the price column in the scan report, then re-try.
        <br><button class="btn btn-sm btn-primary" style="margin-top:8px" onclick="${review}">Open scan report</button>`);
      return;
    }
    dashStep('approve', 'now');
    dashStage(`
      <span class="dokbig">✓</span>
      <div class="dmsg ok"><b>Mapping looks good!</b></div>
      <div class="dmsg"><b>${d.total_products}</b> products · <b>${d.priced_products}</b> priced · <b>${d.images_found}</b> images</div>
      <div style="display:flex;gap:8px;justify-content:center;margin-top:10px;">
        <button class="btn btn-sm btn-outline" onclick="${review}">👁 Review Mapping</button>
        <button class="btn btn-sm btn-primary" onclick="event.stopPropagation();dashImportNow()">✓ Approve &amp; Import</button>
      </div>`);
  } catch (e) {
    dashProgDone(timer, 'dash-map');
    dashStep('map', null); dashFail(e.message);
  }
}

function dashConfetti() {
  return `<div class="dcfx">${Array.from({length: 12}, (_, i) => `<i class="c${i % 6}"></i>`).join('')}</div>`;
}

async function dashImportNow() {
  const file = window._dashFile;
  if (!file) return;
  dashStep('approve', 'done');
  dashStep('import', 'now');
  // State 8: donut % + task checklist. Donut creeps to 90 while the real
  // upload runs; checklist rows tick on a schedule matched to typical timing.
  dashStage(`
    <div class="dimp">
      <div class="ddonut" id="dash-donut" style="--p:0"><span id="dash-donut-t">0%</span></div>
      <div class="dchk">
        <div class="dmsg" style="margin:0 0 6px"><b>Importing data…</b></div>
        <div id="chk-0" class="dchkrow now">● Validating rows</div>
        <div id="chk-1" class="dchkrow">○ Importing items</div>
        <div id="chk-2" class="dchkrow">○ Saving to repository</div>
      </div>
    </div>`);
  let p = 0;
  const timer = setInterval(() => {
    p = Math.min(90, p + 3 + Math.random() * 8);
    const el = document.getElementById('dash-donut');
    const t = document.getElementById('dash-donut-t');
    if (el) el.style.setProperty('--p', p);
    if (t) t.textContent = Math.round(p) + '%';
    const tick = (i, s) => { const r = document.getElementById('chk-' + i);
      if (r) { r.className = 'dchkrow ' + s; r.textContent = (s === 'ok' ? '✔ ' : s === 'now' ? '● ' : '○ ') + r.textContent.slice(2); } };
    if (p > 30) { tick(0, 'ok'); tick(1, 'now'); }
    if (p > 65) { tick(1, 'ok'); tick(2, 'now'); }
  }, 220);
  try {
    const fd = new FormData(); fd.append('file', file);
    const res = await fetch(`${API}/api/master-table/upload`, { method: 'POST', body: fd });
    const d = await res.json();
    clearInterval(timer);
    if (!res.ok) {
      dashStep('import', null);
      dashFail(`${apiErr(d)}<br><button class="btn btn-sm btn-primary" style="margin-top:8px"
        onclick="event.stopPropagation();dashReset();setMasterFiles([window._dashFile]);show('master');setTimeout(scanMasterFiles,150)">Open Master import</button>`);
      return;
    }
    dashStep('import', 'done');
    window._dashBusy = false;
    dashStage(`${dashConfetti()}
      <span class="dokbig">✓</span>
      <div class="dmsg ok" style="font-size:16px"><b>Import Successful!</b></div>
      <div class="dmsg">${d.message}</div>
      <button class="btn btn-sm btn-outline" style="margin-top:8px"
        onclick="event.stopPropagation();dashReset();show('master')">View Imported Data →</button>`);
  } catch (e) {
    clearInterval(timer);
    dashStep('import', null); dashFail(e.message);
  }
}

// BOQ/quotation: run the coverage check right here in the card. The full
// report (missing-item table, add-to-master) stays on the coverage screen —
// one click away, never automatic.
async function dashBoqCheck() {
  const file = window._dashFile;
  if (!file) return;
  window._dashBusy = true;
  dashStep('map', 'now');
  dashStage(`${dashChip(file, true)}
    <div class="dmsg">Matching every BOQ line against the Master Table…</div>
    <div style="display:flex;align-items:center;gap:8px">${dashProgHTML('dash-boq', true)}</div>`);
  const timer = dashProgRun('dash-boq');
  try {
    const fd = new FormData(); fd.append('file', file);
    const res = await fetch(`${API}/api/master-table/check-boq`, { method: 'POST', body: fd });
    const d = await res.json();
    dashProgDone(timer, 'dash-boq');
    if (!res.ok) { dashStep('map', null); dashFail(apiErr(d, 'Coverage check failed')); return; }
    dashStep('map', 'done');
    window._dashBusy = false;
    window._dashCov = d;
    dashStage(`
      <div class="dmsg ok" style="font-size:16px"><b>${d.coverage_pct}% covered</b></div>
      <div class="cov-bar" style="margin:10px 0 8px"><div class="cov-fill" style="width:${d.coverage_pct}%"></div></div>
      <div class="dcov">
        <span><b>${d.total}</b> lines</span>
        <span class="cok">✔ <b>${d.found_count}</b> we stock</span>
        <span class="cmiss">✕ <b>${d.missing_count}</b> we don't</span>
      </div>
      <div style="display:flex;gap:8px;justify-content:center;margin-top:12px;">
        <button class="btn btn-sm btn-primary"
          onclick="event.stopPropagation();setBoqReqFile(window._dashFile);showBoqCoverage();boqMissingItems=window._dashCov.missing||[];renderBoqCoverage(window._dashCov)">View full report →</button>
        <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();dashReset()">Done</button>
      </div>`);
  } catch (e) {
    dashProgDone(timer, 'dash-boq');
    dashStep('map', null); dashFail(e.message);
  }
}

// Sidebar shortcuts to features that live inside other screens
function showBoqCoverage() {
  document.querySelectorAll('.snav').forEach(b => b.classList.toggle('active', b.dataset.tab === 'coverage'));
  document.querySelectorAll('.section').forEach(x => x.classList.remove('active'));
  const sec = document.getElementById('sec-generate');
  sec.classList.add('active', 'boq-only');   // coverage view: BOQ card only
  loadCatalogSelector();
}
function showMargins() {
  document.querySelectorAll('.snav').forEach(b => b.classList.toggle('active', b.dataset.tab === 'margin'));
  document.querySelectorAll('.section').forEach(x => x.classList.remove('active'));
  document.getElementById('sec-repository').classList.add('active');
  window._marginOnly = true;   // margin view: only BOQ-priced quotes
  loadRepository();
}

// Theme: light is the default; the choice sticks per browser
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const b = document.getElementById('theme-btn');
  if (b) b.textContent = t === 'dark' ? '☀️' : '🌙';
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('theme', next);
  applyTheme(next);
}
applyTheme(localStorage.getItem('theme') || 'light');

// Gmail-style sidebar collapse: hamburger shrinks it to an icon rail.
function toggleSidebar() {
  const mini = document.body.classList.toggle('sb-mini');
  localStorage.setItem('sb-mini', mini ? '1' : '');
}
if (localStorage.getItem('sb-mini') === '1') document.body.classList.add('sb-mini');
// Icon-rail tooltips: each nav item's own label, shown on hover when collapsed.
document.querySelectorAll('.snav').forEach(n => n.title = n.textContent.trim());

// Fill the requirement box from a PDF or Excel — user reviews, then generates.
function fillFromPdf(file) { return fillFromFile(file, 'pdf'); }
async function fillFromFile(file, kind) {
  if (!file) return;
  const ta = document.getElementById('req-prompt');
  const st = document.getElementById('gen-status');
  st.innerHTML = `<div class="alert alert-info">Reading '${escHtml(file.name)}'…</div>`;
  try {
    const fd = new FormData(); fd.append('file', file);
    const res = await fetch(`${API}/api/extract-${kind}`, { method: 'POST', body: fd });
    const d = await res.json();
    if (!res.ok) { st.innerHTML = `<div class="alert alert-error">${apiErr(d)}</div>`; return; }
    let text = (d.lines || []).join('\n');
    const cut = text.length > REQ_MAX;
    if (cut) text = text.slice(0, REQ_MAX);
    ta.value = text; updateReqCount(); ta.focus();
    st.innerHTML = `<div class="alert alert-success">Read ${d.lines.length} line(s) from the file —
      check the list, then press Generate.${cut ? ` ⚠️ Trimmed to the first ${REQ_MAX} characters.` : ''}</div>`;
  } catch (e) {
    st.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
}

// Kept in step with the textarea's maxlength in index.html and with
// GenerateRequest.prompt's max_length on the server.
const REQ_MAX = 8000;

// Chrome restores a textarea's value on reload WITHOUT firing `input`, so the
// counter read "0 / 8000" beside 777 characters of restored text. Sync at load.
window.addEventListener('DOMContentLoaded', () => updateReqCount());

function updateReqCount() {
  const ta = document.getElementById('req-prompt');
  const c = document.getElementById('req-count');
  if (ta && c) c.textContent = `${ta.value.length} / ${REQ_MAX}`;
}

// ── Dedupe (admin only): junk rows, double imports, duplicate names ──────────
async function loadDedupe() {
  const view = document.getElementById('dedupe-view');
  view.innerHTML = '<div class="loading-state">Scanning the master table…</div>';
  try {
    const res = await fetch(`${API}/api/master-table/dedupe-report`);
    const d = await res.json();
    if (!res.ok) { view.innerHTML = `<div class="alert alert-error">${apiErr(d)}</div>`; return; }
    renderDedupe(d);
  } catch (e) {
    view.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
}

function renderDedupe(d) {
  const view = document.getElementById('dedupe-view');
  const junk = d.junk.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th></th><th>Product name</th><th>Catalogue</th><th>3★ ₹</th></tr></thead>
      <tbody>${d.junk.map(j => `<tr>
        <td><input type="checkbox" class="junk-cb" value="${j.id}" checked></td>
        <td><strong>${escHtml(j.product)}</strong></td>
        <td style="font-size:var(--fs-sm)">${escHtml(j.file_name || '')}</td>
        <td>₹${j.price_3star || 0}</td></tr>`).join('')}</tbody>
    </table></div>
    <button class="btn btn-sm btn-danger" style="margin-top:10px" onclick="deleteJunkRows()">🗑 Delete selected rows</button>`
    : '<div class="alert alert-success">No junk rows found.</div>';

  const pairs = d.overlapping_imports.length ? d.overlapping_imports.map((p, i) => `
    <div class="dedupe-pair">
      <div class="dp-info">
        <b>${escHtml(p.file_a)}</b> <small>(${p.rows_a} products)</small>
        &nbsp;↔&nbsp; <b>${escHtml(p.file_b)}</b> <small>(${p.rows_b} products)</small>
        <span class="dp-badge">${p.shared_models} shared model codes</span>
      </div>
      <div class="dp-actions">
        <button class="btn btn-sm" onclick="previewPair(${i}, '${escHtml(p.file_a).replace(/'/g, "\\'")}', '${escHtml(p.file_b).replace(/'/g, "\\'")}')">👁 Preview</button>
        <button class="btn btn-sm btn-danger" onclick="deleteWholeImport('${escHtml(p.file_a).replace(/'/g, "\\'")}', ${p.rows_a})">Delete left import</button>
        <button class="btn btn-sm btn-danger" onclick="deleteWholeImport('${escHtml(p.file_b).replace(/'/g, "\\'")}', ${p.rows_b})">Delete right import</button>
      </div>
      <div id="dp-prev-${i}" class="dp-preview" style="display:none"></div>
    </div>`).join('')
    : '<div class="alert alert-success">No overlapping imports found.</div>';

  const dups = d.dup_names.length ? `
    <p style="color:var(--muted);font-size:var(--fs-sm);margin-bottom:10px;">
      ${d.dup_name_groups_total.toLocaleString('en-IN')} name groups exist more than once.
      Most are legitimate size/brand variants — review manually in the Master Catalogue; nothing here is auto-deleted.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Name (top ${d.dup_names.length})</th><th>Rows</th><th>Catalogues</th></tr></thead>
      <tbody>${d.dup_names.map(n => `<tr><td>${escHtml(n.name)}</td><td>${n.n}</td><td>${n.files}</td></tr>`).join('')}</tbody>
    </table></div>` : '';

  view.innerHTML = `
    <div class="card"><h2 data-icon="🗑">Junk rows <span class="mf-count">${d.junk.length}</span></h2>
      <p style="color:var(--muted);font-size:var(--fs-sm);margin-bottom:10px;">Header fragments imported as products — safe to delete after a glance.</p>${junk}</div>
    <div class="card"><h2 data-icon="📑">Possible double imports <span class="mf-count">${d.overlapping_imports.length}</span></h2>
      <p style="color:var(--muted);font-size:var(--fs-sm);margin-bottom:10px;">Catalogue pairs sharing many model codes — usually the same supplier list imported twice. Keep one, delete the other. <b>Check both in the Master Catalogue before deleting.</b></p>${pairs}</div>
    <div class="card"><h2 data-icon="👥">Duplicate product names</h2>${dups}</div>`;
}

async function previewPair(idx, fileA, fileB) {
  const el = document.getElementById(`dp-prev-${idx}`);
  if (el.style.display !== 'none') { el.style.display = 'none'; return; }
  el.style.display = '';
  el.innerHTML = '<div class="loading-state">Loading shared models…</div>';
  const r = await fetch(`${API}/api/master-table/dedupe-pair-preview?file_a=${encodeURIComponent(fileA)}&file_b=${encodeURIComponent(fileB)}`);
  const rows = await r.json();
  if (!r.ok || !rows.length) { el.innerHTML = '<div class="alert alert-error">Nothing to preview.</div>'; return; }
  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Model</th><th>Left product</th><th class="num">Left ₹</th><th>Right product</th><th class="num">Right ₹</th><th class="c">Same price?</th></tr></thead>
    <tbody>${rows.map(x => `<tr>
      <td style="font-size:var(--fs-sm)">${escHtml(x.model)}</td>
      <td>${escHtml(x.product_a)}</td><td class="num">₹${(x.price_a||0).toLocaleString('en-IN')}</td>
      <td>${escHtml(x.product_b)}</td><td class="num">₹${(x.price_b||0).toLocaleString('en-IN')}</td>
      <td class="c">${x.price_a === x.price_b ? '✔' : `<b style="color:#e04444">✘</b>`}</td>
    </tr>`).join('')}</tbody>
  </table></div>
  <p style="color:var(--muted);font-size:var(--fs-sm);margin-top:6px;">Showing up to 30 shared models. ✘ means the two imports carry different prices — check which is current before deleting.</p>`;
}

async function deleteJunkRows() {
  const ids = [...document.querySelectorAll('.junk-cb:checked')].map(c => +c.value);
  if (!ids.length) return;
  if (!await appConfirm({ title: 'Delete junk rows', term: `${ids.length} selected`,
    message: 'Header fragments and empty names removed from the master table.',
    confirmLabel: '🗑 Delete rows', footer: 'This action cannot be undone.' })) return;
  const res = await fetch(`${API}/api/master-table/delete-rows`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ ids })
  });
  const d = await res.json();
  alert(res.ok ? d.message : apiErr(d));
  masterAllItems = [];        // master view cache is now stale
  loadDedupe();
}

async function deleteWholeImport(fname, rows) {
  if (!await appConfirm({ title: 'Delete import', term: fname,
    message: `All ${rows} products in this import will be removed.`,
    note: '<b>Check first.</b> Do this only if you have confirmed it duplicates another catalogue in the Master Catalogue view.',
    confirmLabel: '🗑 Delete import', footer: 'This action cannot be undone.' })) return;
  const res = await fetch(`${API}/api/master-table/${encodeURIComponent(fname)}`, { method: 'DELETE' });
  const d = await res.json();
  alert(res.ok ? (d.message || 'Deleted.') : apiErr(d));
  masterAllItems = [];
  loadDedupe();
}

// ── Users (admin only) ───────────────────────────────────────────────────────
async function loadUsers() {
  const view = document.getElementById('users-view');
  view.innerHTML = '<div class="loading-state">Loading...</div>';
  try {
    const res = await fetch(`${API}/api/auth/users`);
    const d = await res.json();
    if (!res.ok) { view.innerHTML = `<div class="alert alert-error">${apiErr(d)}</div>`; return; }
    view.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Email</th><th style="width:160px">Role</th>
                     <th style="width:110px">Status</th><th style="width:150px"></th></tr></thead>
          <tbody>${d.map(u => {
            const self = currentUser && u.id === currentUser.id;
            const inactive = !u.is_active;
            return `<tr class="${inactive ? 'user-inactive' : ''}">
              <td><strong>${u.name || '—'}</strong>${self ? ' <span class="user-you">you</span>' : ''}</td>
              <td>${u.email}</td>
              <td>
                <select class="user-role-sel" ${self ? 'disabled title="You cannot change your own role"' : ''}
                        onchange="setUserRole(${u.id}, this.value, this)">
                  <option value="employee"${u.role === 'employee' ? ' selected' : ''}>Employee</option>
                  <option value="admin"${u.role === 'admin' ? ' selected' : ''}>Admin</option>
                </select>
              </td>
              <td>${inactive ? '<span class="user-badge off">deactivated</span>'
                             : '<span class="user-badge on">active</span>'}</td>
              <td>${self ? '' : inactive
                    ? `<button class="btn btn-sm btn-success" onclick="setUserActive(${u.id}, true)">Reactivate</button>`
                    : `<button class="btn btn-sm btn-danger" onclick="setUserActive(${u.id}, false)">Deactivate</button>`}</td>
            </tr>`;
          }).join('')}</tbody>
        </table>
      </div>
      <div id="users-status" style="margin-top:10px;"></div>`;
  } catch (e) {
    view.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
}

async function _updateUser(id, body) {
  const st = document.getElementById('users-status');
  try {
    const res = await fetch(`${API}/api/auth/users/${id}`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const d = await res.json();
    st.innerHTML = res.ok
      ? `<div class="alert alert-success">${d.message}</div>`
      : `<div class="alert alert-error">${apiErr(d)}</div>`;
  } catch (e) {
    st.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
  loadUsers();   // re-render from the server's truth either way
}

function setUserRole(id, role, sel) { _updateUser(id, { role }); }
async function setUserActive(id, active) {
  if (!active && !await appConfirm({ title: 'End this person\'s access', danger: false,
    message: 'They are signed out and can no longer log in.',
    note: '<b>Nothing is lost.</b> Their account and history stay, and you can reactivate them any time.',
    confirmLabel: 'End access' })) {
    loadUsers();
    return;
  }
  _updateUser(id, { is_active: active });
}

// ── Activity / audit log ─────────────────────────────────────────────────────
// Admin-only. Answers "who changed this, and when" — the log is what showed
// the KMW catalog had been deleted rather than lost to a bug.
let auditRows = [];

// Plain-English names; the raw action strings read like database events.
const AUDIT_LABELS = {
  upload_master_table: 'Imported master table', delete_master_table_file: 'Deleted master catalog',
  edit_master_product_price: 'Edited a product price', bulk_set_tier_pricing: 'Bulk tier pricing',
  reset_tier_pricing: 'Reset tier pricing', add_products_from_boq: 'Added products from a BOQ',
  confirm_column_mapping: 'Taught a column mapping', delete_column_mapping: 'Removed a column mapping',
  upload_catalog: 'Uploaded a BOQ catalog', delete_catalog_file: 'Deleted a BOQ catalog',
  upload_matched_boq: 'Imported a matched BOQ',
  smart_generate_quotation: 'Generated a quotation', smart_generate_from_boq: 'Generated from a BOQ',
  edit_quotation: 'Edited a quotation', learned_correction: 'Learned a correction',
  delete_correction: 'Removed a learned correction',
  update_user_role: 'Changed a role', check_boq_coverage: 'Checked BOQ coverage', deactivate_user: 'Deactivated a user',
  reactivate_user: 'Reactivated a user', create_user: 'Created a user', delete_quotation: 'Deleted a quotation',
  clear_all_quotations: 'Cleared all quotations', self_register: 'Signed up',
};
// Destructive actions get called out — they're the ones worth spotting fast.
const AUDIT_DANGER = new Set(['delete_master_table_file', 'delete_catalog_file', 'delete_quotation',
                              'clear_all_quotations', 'delete_column_mapping']);

async function loadAuditLog() {
  const view = document.getElementById('audit-view');
  const uid = document.getElementById('audit-user').value;
  view.innerHTML = '<div class="loading-state">Loading...</div>';
  try {
    const res = await fetch(`${API}/api/audit-log${uid ? '?user_id=' + uid : ''}`);
    const d = await res.json();
    if (!res.ok) { view.innerHTML = `<div class="alert alert-error">${apiErr(d)}</div>`; return; }
    auditRows = d;
    populateAuditFilters();
    renderAuditLog();
  } catch (e) {
    view.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
}

function populateAuditFilters() {
  const actSel = document.getElementById('audit-action');
  const keep = actSel.value;
  const actions = [...new Set(auditRows.map(r => r.action))].sort();
  actSel.innerHTML = '<option value="">All activity</option>' +
    actions.map(a => `<option value="${a}">${AUDIT_LABELS[a] || a}</option>`).join('');
  actSel.value = keep;

  const userSel = document.getElementById('audit-user');
  if (userSel.options.length <= 1) {
    const users = [...new Map(auditRows.filter(r => r.user_id)
      .map(r => [r.user_id, r.user_name || r.user_email || ('User #' + r.user_id)])).entries()];
    userSel.innerHTML = '<option value="">Everyone</option>' +
      users.map(([id, name]) => `<option value="${id}">${name}</option>`).join('');
  }
}

function renderAuditLog() {
  const view = document.getElementById('audit-view');
  const act = document.getElementById('audit-action').value;
  const term = document.getElementById('audit-search').value.trim().toLowerCase();

  let rows = auditRows;
  if (act) rows = rows.filter(r => r.action === act);
  if (term) rows = rows.filter(r =>
    (r.target || '').toLowerCase().includes(term) ||
    (r.user_name || '').toLowerCase().includes(term) ||
    (AUDIT_LABELS[r.action] || r.action).toLowerCase().includes(term));

  document.getElementById('audit-count').textContent =
    `${rows.length} of ${auditRows.length} entries`;

  if (!rows.length) {
    view.innerHTML = '<div class="empty-state"><span class="es-icon">🔍</span><div class="es-title">Nothing matches</div><div class="es-hint">Try a different filter or search term.</div></div>';
    return;
  }

  view.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr><th style="width:150px">When</th><th style="width:150px">Who</th>
                   <th style="width:210px">Action</th><th>Details</th></tr></thead>
        <tbody>${rows.map(r => {
          const when = r.created_at ? String(r.created_at).replace('T', ' ').slice(0, 19) : '—';
          const who  = r.user_name || r.user_email || (r.user_id ? '#' + r.user_id : 'system');
          const label = AUDIT_LABELS[r.action] || r.action;
          return `<tr class="${AUDIT_DANGER.has(r.action) ? 'audit-danger' : ''}">
            <td class="audit-when">${when}</td>
            <td>${who}</td>
            <td><span class="audit-action">${label}</span></td>
            <td class="audit-target">${r.target || '—'}${renderAuditExtra(r)}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>`;
}

// after_json holds what actually changed (rows updated, price set, etc.) —
// summarised inline rather than dumped as raw JSON.
function renderAuditExtra(r) {
  if (!r.after_json) return '';
  try {
    const a = JSON.parse(r.after_json);
    const bits = Object.entries(a)
      .filter(([, v]) => v !== null && v !== '' && v !== undefined)
      .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`);
    return bits.length ? `<span class="audit-extra">${bits.join(' · ')}</span>` : '';
  } catch (e) { return ''; }
}

// ── Upload ───────────────────────────────────────────────────────────────────
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');

let selectedFiles = [];

dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag'));
dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.classList.remove('drag'); setFiles(e.dataTransfer.files); });
fileInput.addEventListener('change', e => setFiles(e.target.files));

function setFiles(files) {
  selectedFiles = Array.from(files);
  if (selectedFiles.length) {
    dropZone.querySelector('p').innerHTML = `<strong>${selectedFiles.map(f=>f.name).join(', ')}</strong> selected`;
    document.getElementById('scan-btn').disabled = false;
    document.getElementById('upload-btn').disabled = false;
    document.getElementById('scan-result').innerHTML = '';
    document.getElementById('upload-status').innerHTML = '';
  }
}

async function scanFile() {
  if (!selectedFiles.length) return;
  const btn = document.getElementById('scan-btn');
  btn.disabled = true; btn.textContent = '🔍 Scanning...';
  const out = document.getElementById('scan-result');
  out.innerHTML = '';
  for (const file of selectedFiles) {
    const fd = new FormData(); fd.append('file', file);
    try {
      const res = await fetch(`${API}/api/scan-boq`, { method: 'POST', body: fd });
      const d = await res.json();
      if (!res.ok) { out.innerHTML += `<div class="alert alert-error">${apiErr(d)}</div>`; continue; }
      out.innerHTML += `
        <div class="card" style="margin-top:0;border-left:4px solid var(--primary);position:relative;">
          <button onclick="this.closest('.card').remove()" style="position:absolute;top:10px;right:10px;background:none;border:none;font-size:var(--fs-lg);cursor:pointer;color:var(--muted);" title="Close">✕</button>
          <strong>📄 ${d.filename}</strong>
          <div style="margin-top:8px;font-size:var(--fs-base);color:var(--muted)">
            ✅ <b>${d.total_products}</b> products found &nbsp;·&nbsp;
            🖼 <b>${d.images_found}</b> images &nbsp;·&nbsp;
            📋 Columns: <code style="background:#f0f3f8;padding:2px 6px;border-radius:4px">${d.columns_detected.join(', ')}</code>
          </div>
          <div class="table-wrap" style="margin-top:12px">
            <table>
              <thead><tr><th>Product</th><th>Brand</th><th>Model</th><th>Price</th><th>GST%</th></tr></thead>
              <tbody>${(d.preview||[]).map(i=>`<tr>
                <td><strong>${i.product||'—'}</strong></td>
                <td>${i.brand||'—'}</td>
                <td style="font-size:var(--fs-sm)">${i.model_no||'—'}</td>
                <td>₹${i.price||0}</td>
                <td>${i.gst_pct??18}%</td>
              </tr>`).join('')}</tbody>
            </table>
          </div>
          ${d.total_products > 8 ? `<p style="font-size:var(--fs-sm);color:var(--muted);margin-top:6px">...and ${d.total_products - 8} more products</p>` : ''}
        </div>`;
    } catch(e) { out.innerHTML += `<div class="alert alert-error">Scan failed: ${e.message}</div>`; }
  }
  btn.disabled = false; btn.textContent = '🔍 Scan First';
}

async function uploadFile() {
  if (!selectedFiles.length) return;
  const btn = document.getElementById('upload-btn');
  btn.disabled = true; btn.textContent = '📤 Uploading...';
  const status = document.getElementById('upload-status');
  status.innerHTML = '';
  for (const file of selectedFiles) {
    const fd = new FormData(); fd.append('file', file);
    try {
      const res = await fetch(`${API}/api/upload-boq`, { method: 'POST', body: fd });
      const data = await res.json();
      status.innerHTML += `<div class="alert alert-${res.ok ? 'success' : 'error'}">${res.ok ? data.message : apiErr(data)}</div>`;
    } catch (e) {
      status.innerHTML += `<div class="alert alert-error">Upload failed: ${e.message}</div>`;
    }
  }
  btn.disabled = false; btn.textContent = '📤 Upload to Catalog';
  loadCatalog();
}

async function handleFiles(files) { setFiles(files); }

async function loadUploadedFiles() {
  const res = await fetch(`${API}/api/boq-files`);
  const files = await res.json();
  const el = document.getElementById('uploaded-files');
  if (!files.length) { el.innerHTML = '<div class="empty-state"><span class="es-icon">📁</span><div class="es-title">No files uploaded yet</div><div class="es-hint">Uploaded BOQ files will be listed here.</div></div>'; return; }
  el.innerHTML = files.map(f => `
    <div class="repo-item">
      <div>
        <strong>📄 ${f.file_name}</strong>
        <div class="meta">${f.count} products &nbsp;·&nbsp; uploaded ${new Date(f.uploaded_at).toLocaleDateString('en-IN')}</div>
      </div>
      <button class="btn btn-sm btn-danger" onclick="deleteFile('${f.file_name}')">🗑 Delete</button>
    </div>`).join('');
}

async function deleteFile(filename) {
  if (!await appConfirm({ title: 'Remove file', term: filename,
    message: 'The file and its learned products leave the BOQ catalog.',
    confirmLabel: '🗑 Remove' })) return;
  const res = await fetch(`${API}/api/boq-files/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  const d = await res.json();
  toast(d.message, 'success');
  loadUploadedFiles();
  loadCatalog();
}

async function loadCatalog() {
  const res = await fetch(`${API}/api/boq-items`);
  const items = await res.json();
  document.getElementById('catalog-count').textContent = items.length;
  const el = document.getElementById('catalog-table');
  if (!items.length) { el.innerHTML = '<div class="empty-state"><span class="es-icon">📦</span><div class="es-title">No products yet</div><div class="es-hint">Import a master table to get started.</div></div>'; return; }

  // Group by file
  const groups = {};
  items.forEach(i => {
    const key = i.file_name || 'Unknown';
    if (!groups[key]) groups[key] = [];
    groups[key].push(i);
  });

  el.innerHTML = Object.entries(groups).map(([fname, prods], gIdx) => `
    <div style="border:1px solid var(--border);border-radius:8px;margin-bottom:10px;overflow:hidden;">
      <div onclick="toggleFolder(${gIdx})" style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;background:var(--card-soft);cursor:pointer;user-select:none;">
        <div>
          <span style="font-size:var(--fs-md);">📁</span>
          <strong style="margin-left:8px;font-size:var(--fs-md);">${fname}</strong>
          <span style="margin-left:10px;background:var(--accent);color:#fff;font-size:var(--fs-xs);padding:2px 8px;border-radius:10px;">${prods.length} products</span>
        </div>
        <span id="arrow-${gIdx}" style="font-size:var(--fs-lg);transition:transform .2s;">▶</span>
      </div>
      <div id="folder-${gIdx}" style="display:none;">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Image</th><th>Product</th><th>Sheet</th><th>Brand</th><th>Model No</th><th>Price (₹)</th><th>GST%</th><th>HSN</th></tr></thead>
            <tbody>${prods.map(i => `<tr>
              <td style="text-align:center;position:relative;">${i.has_image
                ? `<img src="${API}/api/product-image/${i.id}" style="width:76px;height:60px;object-fit:contain;border-radius:4px;border:1px solid ${i.image_match==='guess' ? '#e6a817' : '#ddd'};cursor:zoom-in;" onclick="showImageLightbox('${API}/api/product-image/${i.id}')" title="${i.image_match==='guess' ? 'Best-effort match — please verify this is the right photo' : 'Click to enlarge'}" onerror="this.style.display='none'">
                   ${i.image_match==='guess' ? `<span style="position:absolute;top:2px;right:2px;background:#e6a817;color:#fff;font-size:var(--fs-xs);font-weight:600;padding:1px 4px;border-radius:6px;" title="Best-effort match — please verify">?</span>` : ''}`
                : `<span style="color:#ccc;font-size:var(--fs-xs);">—</span>`}</td>
              <td><strong>${i.product}</strong></td>
              <td style="font-size:var(--fs-sm);color:var(--muted)">${i.sheet_name||'—'}</td>
              <td>${i.brand||'—'}</td>
              <td style="font-size:var(--fs-sm)">${i.model_no||'—'}</td>
              <td>₹${(i.price||0).toLocaleString('en-IN')}</td>
              <td>${i.gst_pct??18}%</td>
              <td>${i.hsn_code||'—'}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
      </div>
    </div>`).join('');
}

function toggleFolder(idx) {
  const content = document.getElementById(`folder-${idx}`);
  const arrow = document.getElementById(`arrow-${idx}`);
  const open = content.style.display === 'block';
  content.style.display = open ? 'none' : 'block';
  arrow.style.transform = open ? '' : 'rotate(90deg)';
}

// ── Master Table ─────────────────────────────────────────────────────────────
const masterDropZone = document.getElementById('master-drop-zone');
const masterFileInput = document.getElementById('master-file-input');
let masterSelectedFiles = [];

masterDropZone.addEventListener('dragover', e => { e.preventDefault(); masterDropZone.classList.add('drag'); });
masterDropZone.addEventListener('dragleave', () => masterDropZone.classList.remove('drag'));
masterDropZone.addEventListener('drop', e => {
  e.preventDefault(); masterDropZone.classList.remove('drag');
  setMasterFiles(e.dataTransfer.files);
});
masterFileInput.addEventListener('change', e => setMasterFiles(e.target.files));

function setMasterFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  masterSelectedFiles = list;
  masterDropZone.querySelector('p').innerHTML = `<strong>${list.map(f => f.name).join(', ')}</strong> selected`;
  document.getElementById('master-scan-btn').disabled = false;
  document.getElementById('master-upload-status').innerHTML = '';
  document.getElementById('master-scan-result').innerHTML = '';
  // A new selection (or re-scan) invalidates any previously-scanned preview —
  // must scan again before Confirm & Import is trusted to match what's shown.
  const uploadBtn = document.getElementById('master-upload-btn');
  uploadBtn.style.display = 'none';
  uploadBtn.disabled = true;
}

async function scanMasterFiles() {
  if (!masterSelectedFiles.length) return;
  const status = document.getElementById('master-upload-status');
  const resultEl = document.getElementById('master-scan-result');
  const btn = document.getElementById('master-scan-btn');
  btn.disabled = true; btn.textContent = '🔍 Scanning...';
  status.innerHTML = '';
  resultEl.innerHTML = '';
  document.getElementById('master-upload-btn').style.display = 'none';
  await ensureMappableFields();   // populates the per-column pickers below
  let anyOk = false;
  for (const file of masterSelectedFiles) {
    const fd = new FormData(); fd.append('file', file);
    try {
      const res = await fetch(`${API}/api/master-table/scan`, { method: 'POST', body: fd });
      const d = await res.json();
      if (!res.ok) {
        resultEl.innerHTML += `<div class="alert alert-error">'${file.name}': ${apiErr(d)}</div>`;
        continue;
      }
      anyOk = true;
      resultEl.innerHTML += `
        <div class="card" style="margin-top:0;margin-bottom:12px;border-left:4px solid var(--primary);">
          <strong>📄 ${d.filename}</strong>
          <div style="margin-top:8px;font-size:var(--fs-base);color:var(--muted)">
            ✅ <b>${d.total_products}</b> products found &nbsp;·&nbsp;
            🖼 <b>${d.images_found}</b> with a confirmed image
            ${d.unmatched_columns.length ? `&nbsp;·&nbsp; ⚠️ columns not found: ${d.unmatched_columns.join(', ')}` : ''}
          </div>
          ${d.total_products > 0 && d.priced_products === 0 ? `
            <div class="alert alert-error" style="margin-top:10px">
              ⚠️ <b>No price column was detected</b> — every product would import at ₹0.
              This file probably names it something like AMOUNT or PRICES IN INR.
              In the column report below, pick the price field for the right column,
              press <b>Teach</b>, then <b>re-scan</b> this file.
            </div>` : ''}
          ${renderFileTypeNotice(d.file_type)}
          ${renderColumnReport(d.columns)}
          <div class="table-wrap" style="margin-top:12px">
            <table>
              <thead><tr><th>Product</th><th>Brand</th><th>Model</th><th>3★ (₹)</th><th>4★ (₹)</th><th>GST%</th></tr></thead>
              <tbody>${d.preview.map(i => `<tr>
                <td><strong>${i.product||'—'}</strong></td>
                <td>${i.brand||'—'}</td>
                <td style="font-size:var(--fs-sm)">${i.original_model||'—'}</td>
                <td>₹${i.price_3star||0}</td>
                <td>₹${i.price_4star||0}</td>
                <td>${i.gst_pct??0}%</td>
              </tr>`).join('')}</tbody>
            </table>
          </div>
          ${d.total_products > 15 ? `<p style="font-size:var(--fs-sm);color:var(--muted);margin-top:6px">...and ${d.total_products - 15} more products</p>` : ''}
        </div>`;
    } catch (e) {
      resultEl.innerHTML += `<div class="alert alert-error">'${file.name}' scan failed: ${e.message}</div>`;
    }
  }
  if (anyOk) {
    const uploadBtn = document.getElementById('master-upload-btn');
    uploadBtn.style.display = '';
    uploadBtn.disabled = false;
  }
  btn.disabled = false; btn.textContent = '🔍 Scan First';
}

async function uploadMasterFile() {
  if (!masterSelectedFiles.length) return;
  const btn = document.getElementById('master-upload-btn');
  btn.disabled = true;
  const status = document.getElementById('master-upload-status');
  status.innerHTML = '';
  const results = [];
  for (let i = 0; i < masterSelectedFiles.length; i++) {
    const file = masterSelectedFiles[i];
    btn.textContent = `📥 Importing ${i + 1}/${masterSelectedFiles.length}...`;
    const fd = new FormData(); fd.append('file', file);
    try {
      const res = await fetch(`${API}/api/master-table/upload`, { method: 'POST', body: fd });
      const data = await res.json();
      if (res.status === 409) {
        // Two distinct 409 gates share this handler: no-price (Phase D) and
        // duplicate filename (below) — both need the admin to knowingly
        // force it through, so both get the same retry button, worded for
        // whichever this response actually is.
        const isDup = apiErr(data).includes('already in the master table');
        results.push(`<div class="alert alert-error">⛔ '${file.name}': ${apiErr(data)}
          <div style="margin-top:8px;"><button class="btn btn-sm btn-danger"
            onclick="forceImportMaster(${i})">${isDup ? 'Replace existing catalogue' : 'Import anyway (no prices)'}</button></div></div>`);
      } else {
        results.push(`<div class="alert alert-${res.ok ? 'success' : 'error'}">${res.ok ? data.message : apiErr(data)}</div>`);
      }
    } catch (e) {
      results.push(`<div class="alert alert-error">'${file.name}' failed: ${e.message}</div>`);
    }
    status.innerHTML = results.join('');
  }
  btn.disabled = false; btn.textContent = '✅ Confirm & Import';
  btn.style.display = 'none';
  document.getElementById('master-scan-result').innerHTML = '';
  loadMasterTable();
}

// Deliberate override of the Phase D no-price gate — the server records the
// import as forced in the audit log.
async function forceImportMaster(idx) {
  const file = masterSelectedFiles[idx];
  if (!file) return;
  const status = document.getElementById('master-upload-status');
  const fd = new FormData(); fd.append('file', file); fd.append('force', '1');
  try {
    const res = await fetch(`${API}/api/master-table/upload`, { method: 'POST', body: fd });
    const data = await res.json();
    status.innerHTML += `<div class="alert alert-${res.ok ? 'success' : 'error'}">${res.ok ? data.message : apiErr(data)}</div>`;
    if (res.ok) { loadMasterTable(); }
  } catch (e) {
    status.innerHTML += `<div class="alert alert-error">${e.message}</div>`;
  }
}

async function clearCategory(cat) {
  if (!await appConfirm({ title: 'Remove category', term: cat,
    message: 'This category will be removed from your list.',
    note: '<b>Your products are safe.</b> All products currently in this category will move back to <b>Uncategorised</b>.',
    confirmLabel: '🗑 Remove category' })) return;
  const res = await fetch(`${API}/api/master-table/clear-category`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ category: cat })
  });
  const d = await res.json();
  toast(res.ok ? d.message : (d.detail || 'Failed'), res.ok ? 'success' : 'error');
  if (res.ok) { masterFolders = {}; masterSummary = []; loadMasterTable(); }
}

async function deleteMasterFile(filename) {
  if (!await appConfirm({ title: 'Delete catalogue', term: filename,
    message: 'All of its products are removed from the master table.',
    confirmLabel: '🗑 Delete catalogue', footer: 'This action cannot be undone.' })) return;
  const res = await fetch(`${API}/api/master-table/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  const d = await res.json();
  toast(d.message, res.ok ? 'success' : 'error');
  masterFolders = {}; masterAllItems = [];
  loadMasterTable();
}


function onMasterSearchFocus() {
  document.getElementById('master-search-box').classList.add('active');
}
function onMasterSearchBlur() {
  // Stay expanded while there's an active search term — collapsing on
  // blur would hide the very filter the user just typed.
  const box = document.getElementById('master-search-box');
  if (!document.getElementById('master-search').value.trim()) {
    box.classList.remove('active');
  }
}

// Phase 2 paging: the page loads a names+counts summary only; product rows
// come per catalogue on expand / "Load more" (200 a page), and search runs
// server-side — so a 3-lakh master table can't flood the browser.
let masterSummary = [];        // [{file_name, count}] — key is file OR category label
let masterFolders = {};        // group key -> {items: [], total: N}
let masterSearchTerm = '';
const MASTER_PAGE = 200;

// Batch wise (per uploaded file) or Category wise (per CATEGORY column) —
// same folder renderer, different grouping. Sticky per browser.
let masterMode = localStorage.getItem('master-mode') || 'file';
function _groupParam(key) {
  return (masterMode === 'file' ? 'file=' : 'category=') + encodeURIComponent(key);
}
function masterNavClick() {
  const sub = document.getElementById('master-sub');
  if (sub) sub.style.display = sub.style.display === 'none' ? '' : 'none';
  show('master');
  _syncMasterSub();
}
function setMasterMode(m) {
  if (m !== masterMode) {
    masterMode = m;
    localStorage.setItem('master-mode', m);
    masterSummary = []; masterFolders = {};
  }
  show('master');
  _syncMasterSub();  // after show(), which resets .snav active states
  loadMasterTable();
}
function _syncMasterSub() {
  document.querySelectorAll('#master-sub .snav').forEach(n =>
    n.classList.toggle('active', n.dataset.sub === masterMode));
}

// ── In-app confirm modal: replaces every browser confirm() popup ─────────────
// appConfirm({title, term, message, note, confirmLabel, danger, footer}) -> Promise<bool>
// term: highlighted (red) word inside the title. note: amber "your data is
// safe" callout (app-authored HTML allowed). footer: small lock line.
function appConfirm(o) {
  return new Promise(resolve => {
    const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const danger = o.danger !== false;
    const wrap = document.createElement('div');
    wrap.className = 'cfm-overlay';
    wrap.innerHTML = `
      <div class="cfm-card" role="dialog" aria-modal="true">
        <button class="cfm-x" aria-label="Close">×</button>
        <div class="cfm-icon ${danger ? 'danger' : 'accent'}">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
        </div>
        <h3 class="cfm-title">${esc(o.title)}${o.term ? ` <span class="cfm-hl">'${esc(o.term)}'</span>` : ''}?</h3>
        <p class="cfm-msg">${esc(o.message || '')}</p>
        ${o.note ? `<div class="cfm-note">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>
          <div>${o.note}</div>
        </div>` : ''}
        <div class="cfm-actions">
          <button class="btn cfm-cancel">✕ Cancel</button>
          <button class="btn ${danger ? 'btn-danger-solid' : 'btn-primary'} cfm-ok">${esc(o.confirmLabel || 'Confirm')}</button>
        </div>
        ${o.footer ? `<div class="cfm-foot">🔒 ${esc(o.footer)}</div>` : ''}
      </div>`;
    const done = val => {
      wrap.classList.remove('open');
      document.removeEventListener('keydown', onKey);
      setTimeout(() => wrap.remove(), 200);
      resolve(val);
    };
    const onKey = e => { if (e.key === 'Escape') done(false); };
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('click', e => { if (e.target === wrap) done(false); });
    wrap.querySelector('.cfm-x').onclick = () => done(false);
    wrap.querySelector('.cfm-cancel').onclick = () => done(false);
    wrap.querySelector('.cfm-ok').onclick = () => done(true);
    document.body.appendChild(wrap);
    requestAnimationFrame(() => requestAnimationFrame(() => wrap.classList.add('open')));
  });
}

// appPrompt: appConfirm with a text input — resolves the typed string, or null.
function appPrompt(o) {
  return new Promise(resolve => {
    const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const wrap = document.createElement('div');
    wrap.className = 'cfm-overlay';
    wrap.innerHTML = `
      <div class="cfm-card" role="dialog" aria-modal="true">
        <button class="cfm-x" aria-label="Close">×</button>
        <div class="cfm-icon accent">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        </div>
        <h3 class="cfm-title">${esc(o.title)}</h3>
        <p class="cfm-msg">${esc(o.message || '')}</p>
        <input type="text" class="cfm-input" placeholder="${esc(o.placeholder || '')}">
        <div class="cfm-actions">
          <button class="btn cfm-cancel">✕ Cancel</button>
          <button class="btn btn-primary cfm-ok">${esc(o.confirmLabel || 'OK')}</button>
        </div>
      </div>`;
    const inp = wrap.querySelector('.cfm-input');
    const done = val => {
      wrap.classList.remove('open');
      document.removeEventListener('keydown', onKey);
      setTimeout(() => wrap.remove(), 200);
      resolve(val);
    };
    const ok = () => { const v = inp.value.trim(); if (!v) { inp.focus(); return; } done(v); };
    const onKey = e => { if (e.key === 'Escape') done(null); };
    document.addEventListener('keydown', onKey);
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); ok(); } });
    wrap.addEventListener('click', e => { if (e.target === wrap) done(null); });
    wrap.querySelector('.cfm-x').onclick = () => done(null);
    wrap.querySelector('.cfm-cancel').onclick = () => done(null);
    wrap.querySelector('.cfm-ok').onclick = ok;
    document.body.appendChild(wrap);
    requestAnimationFrame(() => requestAnimationFrame(() => { wrap.classList.add('open'); inp.focus(); }));
  });
}

// "＋ New category" (category view): name it, then add products right in the
// dialog — search the whole master table and tick. No page-hopping needed.
let pendingNewCat = '';
async function newCategoryFlow() {
  const name = await appPrompt({ title: 'Create a new category',
    message: 'Next you can search and tick products for it right here.',
    placeholder: 'e.g. Chafing Dishes', confirmLabel: 'Next: pick products' });
  if (!name) return;
  openCategoryPicker(name);
}

// Search-and-tick picker modal: assigns the ticked products to the category.
function openCategoryPicker(catName) {
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const picked = new Set();
  const wrap = document.createElement('div');
  wrap.className = 'cfm-overlay';
  wrap.innerHTML = `
    <div class="cfm-card cfm-wide" role="dialog" aria-modal="true">
      <button class="cfm-x" aria-label="Close">×</button>
      <h3 class="cfm-title">Add products to <span class="cfm-hl">'${esc(catName)}'</span></h3>
      <p class="cfm-msg">Search the whole master table, tick what belongs, then save.</p>
      <input type="text" class="cfm-input" id="ncp-search" placeholder="Search by product, brand, or model…">
      <div id="ncp-results" class="ncp-results"><p class="ncp-hint">Type to search 46,000+ products.</p></div>
      <div class="cfm-actions">
        <button class="btn cfm-cancel">✕ Cancel</button>
        <button class="btn cfm-alt" title="Tick rows batch-by-batch in the catalogue view instead">Pick from catalogue</button>
        <button class="btn btn-primary cfm-ok">Save (<span id="ncp-count">0</span>)</button>
      </div>
    </div>`;
  const results = wrap.querySelector('#ncp-results');
  const countEl = wrap.querySelector('#ncp-count');
  const close = () => { wrap.classList.remove('open'); setTimeout(() => wrap.remove(), 200); };
  let t = null;
  wrap.querySelector('#ncp-search').addEventListener('input', e => {
    clearTimeout(t);
    const q = e.target.value.trim();
    t = setTimeout(async () => {
      if (!q) { results.innerHTML = '<p class="ncp-hint">Type to search 46,000+ products.</p>'; return; }
      const r = await fetch(`${API}/api/master-table/page?q=${encodeURIComponent(q)}&limit=50`);
      const d = await r.json();
      if (!d.items || !d.items.length) { results.innerHTML = '<p class="ncp-hint">No matches.</p>'; return; }
      results.innerHTML = d.items.map(i => `
        <label class="ncp-row">
          <input type="checkbox" ${picked.has(i.id) ? 'checked' : ''}
            onchange="this.checked ? window._ncpPicked.add(${i.id}) : window._ncpPicked.delete(${i.id});
                      document.getElementById('ncp-count').textContent = window._ncpPicked.size;">
          <span class="ncp-name">${esc(i.product)}</span>
          <small>${esc(i.brand || '')} · ${esc(i.file_name)}</small>
        </label>`).join('') +
        (d.total > 50 ? `<p class="ncp-hint">Showing 50 of ${d.total} — refine the search.</p>` : '');
    }, 300);
  });
  window._ncpPicked = picked;
  wrap.querySelector('.cfm-x').onclick = close;
  wrap.querySelector('.cfm-cancel').onclick = close;
  wrap.querySelector('.cfm-alt').onclick = () => {
    close();
    pendingNewCat = catName;
    masterSelMode = true; masterSel.clear();
    setMasterMode('file');
    toast(`Tick products for '${catName}', then press Set in the top bar.`, 'success');
  };
  wrap.querySelector('.cfm-ok').onclick = async () => {
    if (!picked.size) { toast('Tick at least one product.', 'error'); return; }
    if (picked.size > 200) { toast('Max 200 at a time.', 'error'); return; }
    const res = await fetch(`${API}/api/master-table/update-rows`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ids: [...picked], category: catName })
    });
    const d = await res.json();
    if (!res.ok) { toast(d.detail || 'Failed', 'error'); return; }
    toast(d.message, 'success');
    close();
    masterFolders = {}; masterSummary = [];
    loadMasterTable();
  };
  document.body.appendChild(wrap);
  requestAnimationFrame(() => requestAnimationFrame(() => { wrap.classList.add('open'); wrap.querySelector('#ncp-search').focus(); }));
}

// ── Per-folder filter: server-side search scoped to one catalogue/category ──
const masterFolderQ = {};
let _ffTimer = null;
function filterFolder(gIdx, fname, val) {
  masterFolderQ[fname] = val;
  clearTimeout(_ffTimer);
  _ffTimer = setTimeout(async () => {
    const q = (masterFolderQ[fname] || '').trim();
    const r = await fetch(`${API}/api/master-table/page?${_groupParam(fname)}&q=${encodeURIComponent(q)}&limit=${MASTER_PAGE}`);
    const d = await r.json();
    masterFolders[fname] = { items: d.items, total: d.total };
    renderMasterProducts(_currentGroups(), _expandedMasterFolders());
    const inp = document.getElementById(`mf-q-${gIdx}`);
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  }, 300);
}

// ── Batch-wise selection mode: tick rows, then move / recategorise / delete ──
let masterSelMode = false;          // true while ANY folder is in select mode
const masterSelFolders = new Set(); // which folders show checkboxes
const masterSel = new Set();
let masterCatList = [];    // category names for the "Set category…" dropdown

function toggleMasterSelect(gIdx, fname) {
  // Per-folder: only the clicked batch enters/leaves select mode. Ticks from
  // several batches can still be combined by clicking Select on each.
  if (fname === undefined) {           // "Done" in the bottom bar: clear all
    masterSelFolders.clear();
  } else if (masterSelFolders.has(fname)) {
    masterSelFolders.delete(fname);
    (masterFolders[fname]?.items || []).forEach(i => masterSel.delete(i.id));
  } else {
    masterSelFolders.add(fname);
  }
  masterSelMode = masterSelFolders.size > 0;
  if (!masterSelMode) masterSel.clear();
  renderMasterProducts(_currentGroups(), _expandedMasterFolders());
}
function toggleMasterRow(id, on) {
  if (on) masterSel.add(id); else masterSel.delete(id);
  _updSelBar();
}
function toggleMasterFolderAll(fname, on) {
  const g = masterFolders[fname];
  if (!g) return;
  g.items.forEach(i => {
    if (on) masterSel.add(i.id); else masterSel.delete(i.id);
    const cb = document.getElementById(`msel-${i.id}`);
    if (cb) cb.checked = on;
  });
  _updSelBar();
}
function _updSelBar() {
  const e = document.getElementById('msel-count');
  if (e) e.textContent = `${masterSel.size} selected`;
}
async function _selApply(url, body, verb) {
  if (!masterSel.size) { toast('Tick some products first.', 'error'); return; }
  if (masterSel.size > 200) { toast('Max 200 rows at a time — narrow the selection.', 'error'); return; }
  try {
    const res = await fetch(`${API}${url}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ids: [...masterSel], ...body })
    });
    const d = await res.json();
    if (!res.ok) { toast(d.detail || `${verb} failed`, 'error'); return; }
    toast(d.message, 'success');
    masterSel.clear();
    masterFolders = {}; masterAllItems = [];
    loadMasterTable();
  } catch (e) { toast(`${verb} failed: ${e.message}`, 'error'); }
}
function masterSelMove(sel) {
  const fname = sel.value; sel.value = '';
  if (fname) _selApply('/api/master-table/update-rows', { file_name: fname }, 'Move');
}
function masterSelCategory(sel) {
  const cat = sel.value; sel.value = '';
  if (cat) _selApply('/api/master-table/update-rows', { category: cat }, 'Categorise');
}
function masterSelNewCat() {
  const inp = document.getElementById('msel-newcat');
  const cat = (inp.value || '').trim();
  if (!cat) { toast('Type the new category name first.', 'error'); return; }
  inp.value = ''; pendingNewCat = '';
  _selApply('/api/master-table/update-rows', { category: cat }, 'Categorise');
}
async function masterSelDelete() {
  if (!masterSel.size) { toast('Tick some products first.', 'error'); return; }
  if (!await appConfirm({ title: 'Delete products', term: `${masterSel.size} selected`,
    message: 'The ticked products are removed from the master table.',
    confirmLabel: '🗑 Delete', footer: 'This action cannot be undone.' })) return;
  _selApply('/api/master-table/delete-rows', {}, 'Delete');
}

function _currentGroups() {
  const g = {};
  masterSummary.forEach(f => g[f.file_name] = masterFolders[f.file_name] || { items: [], total: f.count });
  return g;
}

function _expandedMasterFolders() {
  const set = new Set();
  document.querySelectorAll('#master-table-view > div').forEach(div => {
    const body = div.querySelector('.master-folder-body');
    const nameEl = div.querySelector('strong');
    if (body && nameEl && body.classList.contains('open')) set.add(nameEl.textContent.trim());
  });
  return set;
}

async function loadMasterTable() {
  const openSet = _expandedMasterFolders();
  const res = await fetch(`${API}/api/master-table/summary?by=${masterMode === 'category' ? 'category' : 'file'}`);
  masterSummary = await res.json();
  document.getElementById('master-count').textContent =
    masterSummary.reduce((s, f) => s + f.count, 0);
  const ncBtn = document.getElementById('btn-new-cat');
  if (ncBtn) ncBtn.style.display =
    (masterMode === 'category' && currentUser && currentUser.role === 'admin') ? '' : 'none';
  if (masterMode === 'file' && currentUser && currentUser.role === 'admin') {
    try {
      const cats = await (await fetch(`${API}/api/master-table/summary?by=category`)).json();
      masterCatList = cats.map(c => c.file_name).filter(c => c !== 'Uncategorised');
    } catch (e) { /* dropdown just stays shorter */ }
  }
  // Folders the user has open must show fresh rows (a bulk-price apply or an
  // inline edit reloads through here) — refetch what was loaded, first page
  // minimum. Closed folders keep whatever they had; they refetch on expand.
  for (const f of masterSummary) {
    const g = masterFolders[f.file_name];
    if (openSet.has(f.file_name)) {
      const want = Math.max((g && g.items.length) || 0, MASTER_PAGE);
      const r = await fetch(`${API}/api/master-table/page?${_groupParam(f.file_name)}&limit=${want}`);
      const d = await r.json();
      masterFolders[f.file_name] = { items: d.items, total: d.total };
    } else if (!g) {
      masterFolders[f.file_name] = { items: [], total: f.count };
    } else {
      g.total = f.count;
    }
  }
  // Respect a search term typed before the fetch resolved — rendering
  // unfiltered here would clobber the filtered view.
  const term = document.getElementById('master-search')?.value.trim();
  if (term) _doMasterSearch(); else renderMasterProducts(_currentGroups(), openSet);
}

let _msTimer = null;
function filterMasterTable() {
  pulseMasterSearchBox();
  clearTimeout(_msTimer);
  _msTimer = setTimeout(_doMasterSearch, 300);   // debounce keystrokes — each search is a server round-trip now
}

async function _doMasterSearch() {
  const term = document.getElementById('master-search').value.trim();
  masterSearchTerm = term;
  if (!term) { renderMasterProducts(_currentGroups(), _expandedMasterFolders()); return; }
  const r = await fetch(`${API}/api/master-table/page?q=${encodeURIComponent(term)}&limit=500`);
  const d = await r.json();
  if (masterSearchTerm !== term) return;   // a newer keystroke superseded this response
  const groups = {};
  d.items.forEach(i => {
    const k = masterMode === 'category'
      ? ((i.category || '').trim() || 'Uncategorised')
      : (i.file_name || 'Unknown');
    (groups[k] = groups[k] || { items: [], total: 0 }).items.push(i);
  });
  Object.values(groups).forEach(g => g.total = g.items.length);
  // Searching implies "show me the matches now" — auto-expand every catalog
  // that has a hit instead of making the user click each folder open.
  renderMasterProducts(groups, new Set(Object.keys(groups)), term, d.total);
}

async function loadMoreFolder(fname) {
  const g = masterFolders[fname] || (masterFolders[fname] = { items: [], total: 0 });
  const fq = (masterFolderQ[fname] || '').trim();
  const r = await fetch(`${API}/api/master-table/page?${_groupParam(fname)}${fq ? `&q=${encodeURIComponent(fq)}` : ''}&offset=${g.items.length}&limit=${MASTER_PAGE}`);
  const d = await r.json();
  g.total = d.total;
  g.items = g.items.concat(d.items);
  renderMasterProducts(_currentGroups(), _expandedMasterFolders());
}

let _masterPulseTimer = null;
function pulseMasterSearchBox() {
  const box = document.getElementById('master-search-box');
  if (!box) return;
  box.classList.remove('pulse');
  // Force reflow so the animation restarts on every keystroke instead of
  // only firing once (re-adding a class mid-animation is a no-op otherwise).
  void box.offsetWidth;
  box.classList.add('pulse');
  clearTimeout(_masterPulseTimer);
  _masterPulseTimer = setTimeout(() => box.classList.remove('pulse'), 500);
}

function renderMasterProducts(groups, forceExpanded, searchTerm, searchTotal) {
  const el = document.getElementById('master-table-view');
  if (!Object.keys(groups).length) {
    el.innerHTML = `<p style="color:var(--muted);font-size:var(--fs-base);">${searchTerm ? 'No products match your search.' : 'No products yet.'}</p>`;
    return;
  }

  // A bulk-price apply or a per-product edit both refresh this whole view
  // by replacing its innerHTML — without remembering which catalog was
  // expanded, that folder silently snaps shut on every save, making the
  // just-applied prices seem to "vanish" until the user re-expands it.
  // (Search results instead use forceExpanded, passed in by the caller.)
  const expandedFiles = forceExpanded || _expandedMasterFolders();

  const isAdminView = currentUser && currentUser.role === 'admin';

  const capNote = (searchTerm && searchTotal > 500)
    ? `<p style="color:var(--muted);font-size:var(--fs-sm);margin-bottom:10px;">Showing the first 500 of ${searchTotal} matches — refine the search to narrow it down.</p>`
    : '';

  const selAny = masterSelMode && isAdminView && masterMode === 'file' && !searchTerm;
  const esc = s => s.replace(/"/g, '&quot;');

  el.innerHTML = capNote + Object.entries(groups).map(([fname, g], gIdx) => {
    const prods = g.items;
    const headerCells = `${isAdminView ? '<th class="num">Cost (₹)</th>' : ''}<th class="num">Original (₹)</th><th class="num">3★ Price (₹)</th><th class="num">3★ Price ($)</th><th class="num">4★ Price (₹)</th><th class="num">4★ Price ($)</th>${isAdminView ? '<th class="c">Margin</th>' : ''}`;

    // Cost + live margin vs the 3★ selling price — admin only (the API
    // already strips cost for employees, so this renders nothing for them).
    const costCell = i => !isAdminView ? '' :
      `<td class="num" style="color:var(--muted)">${i.cost > 0 ? '₹' + (+i.cost).toLocaleString('en-IN') : '—'}</td>`;
    const marginCell = i => {
      if (!isAdminView) return '';
      if (!(i.cost > 0) || !(i.price_3star > 0)) return '<td class="c" style="color:var(--muted)">—</td>';
      const m = (i.price_3star - i.cost) / i.cost * 100;
      const cls = m >= 20 ? 'mgn-good' : m >= 5 ? 'mgn-mid' : 'mgn-low';
      return `<td class="c"><span class="mgn-chip ${cls}">${m >= 0 ? '+' : ''}${m.toFixed(1)}%</span></td>`;
    };

    // The original price always exists — it's the pre-bulk-discount snapshot
    // (orig_price_*) when one was taken, or simply today's price when the
    // catalogue has never been bulk-discounted (nothing has changed it yet,
    // so current IS original). Struck-through only when it actually differs
    // from the current price — a plain, unchanged number otherwise.
    const origCell = i => {
      const o3 = i.orig_price_3star != null ? i.orig_price_3star : i.price_3star;
      const o4 = i.orig_price_4star != null ? i.orig_price_4star : i.price_4star;
      const changed = i.orig_price_3star != null || i.orig_price_4star != null;
      const parts = [];
      if (o3 != null) parts.push(`₹${(o3||0).toLocaleString('en-IN')}`);
      if (o4 != null && o4 !== o3) parts.push(`₹${(o4||0).toLocaleString('en-IN')} (4★)`);
      const style = changed
        ? 'color:var(--muted);text-decoration:line-through;font-size:var(--fs-sm);'
        : 'color:var(--muted);';
      return `<td class="num" style="${style}">${parts.join(' / ') || '—'}</td>`;
    };

    // Every catalog gets independent 3★/4★ fields now — a catalog that
    // started as single-price (e.g. a matched-BOQ import, where both tiers
    // were just the one real price mirrored) is no exception; admins can
    // now give it its own distinct 4★ price the same way KMW already has.
    // USD stays display-only — it's a separately-stored field, not derived,
    // so editing it isn't part of this.
    const priceCells = i => {
      if (!isAdminView) {
        return `${origCell(i)}<td class="num">₹${(i.price_3star||0).toLocaleString('en-IN')}</td>
                 <td class="num">$${(i.price_3star_usd||0).toFixed(2)}</td>
                 <td class="num">₹${(i.price_4star||0).toLocaleString('en-IN')}</td>
                 <td class="num">$${(i.price_4star_usd||0).toFixed(2)}</td>`;
      }
      return `${origCell(i)}<td class="num"><input id="mt-3star-${i.id}" type="number" step="0.01" class="mt-price-input" value="${i.price_3star||0}"
                 onchange="updateMasterPrice(${i.id}, this.value, document.getElementById('mt-4star-${i.id}').value)"
                 onkeydown="stopEnterSubmit(event)" style="width:90px"></td>
               <td class="num">$${(i.price_3star_usd||0).toFixed(2)}</td>
               <td class="num"><input id="mt-4star-${i.id}" type="number" step="0.01" class="mt-price-input" value="${i.price_4star||0}"
                 onchange="updateMasterPrice(${i.id}, document.getElementById('mt-3star-${i.id}').value, this.value)"
                 onkeydown="stopEnterSubmit(event)" style="width:90px"></td>
               <td class="num">$${(i.price_4star_usd||0).toFixed(2)}</td>`;
    };

    const fnameEsc = fname.replace(/'/g, "\\'");
    // Bulk pricing + file download are per-CATALOGUE tools — meaningless
    // when the group key is a category spanning many files.
    const fileMode = masterMode === 'file' && !searchTerm;
    const bulkPricingBar = (isAdminView && fileMode) ? `
      <div class="master-bulk-pricing">
        <span class="mbp-icon">%</span>
        <span class="mbp-label">Set whole catalog</span>
        <div class="mbp-field">
          <span class="mbp-caption"><span class="mbp-stars">⭐</span>3★ off price</span>
          <input type="number" id="bulk-pct3-${gIdx}" placeholder="%" step="0.1" min="0" onkeydown="stopEnterSubmit(event)">
        </div>
        <div class="mbp-field">
          <span class="mbp-caption"><span class="mbp-stars">⭐⭐</span>4★ off price</span>
          <input type="number" id="bulk-pct4-${gIdx}" placeholder="%" step="0.1" min="0" onkeydown="stopEnterSubmit(event)">
        </div>
        <div class="mbp-field">
          <span class="mbp-caption">$ rate (₹ per $1)</span>
          <input type="number" id="bulk-usdrate-${gIdx}" class="mbp-usd-input" placeholder="e.g. 83" step="0.01" min="0" onkeydown="stopEnterSubmit(event)">
        </div>
        <button class="btn btn-sm btn-primary" id="bulk-apply-${gIdx}" onclick="applyBulkTierPricing(${gIdx}, '${fnameEsc}')">✨ Apply to all ${g.total}</button>
        <button class="mbp-reset" id="bulk-reset-${gIdx}" onclick="resetBulkTierPricing(${gIdx}, '${fnameEsc}')"
                title="Revert to the prices before the last bulk change" aria-label="Reset pricing">
          <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/>
            <path d="M3 3.4v4.2h4.2" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
        <span id="bulk-status-${gIdx}" class="mbp-hint"></span>
      </div>` : '';

    const wasExpanded = expandedFiles.has(fname);
    return `
    <div class="master-folder">
      <div class="master-folder-head" onclick="toggleMasterFolder(${gIdx}, '${fnameEsc}')">
        <div class="mf-title">
          <span class="mf-icon">📁</span>
          <strong>${fname}</strong>
          <span class="mf-count">${searchTerm ? `${prods.length} match(es)` : `${g.total} products`}</span>
        </div>
        <span style="display:flex;align-items:center;gap:8px;">
          ${(isAdminView && fileMode) ? `<button class="mf-sel${masterSelFolders.has(fname) ? ' on' : ''}" title="Select rows to move, recategorise or delete"
            onclick="event.stopPropagation();toggleMasterSelect(${gIdx}, '${fnameEsc}')">☑ Select</button>` : ''}
          ${(isAdminView && fileMode) ? `<button class="mf-dl" title="Download the original file"
            onclick="event.stopPropagation();window.open(API+'/api/master-table/download-file/'+encodeURIComponent('${fnameEsc}'))">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="3" x2="12" y2="15"/></svg></button>
          <button class="mf-dl mf-del" title="Delete this catalogue and all its products"
            onclick="event.stopPropagation();deleteMasterFile('${fnameEsc}')">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>` : ''}
          ${(isAdminView && masterMode === 'category' && !searchTerm && fname !== 'Uncategorised') ? `<button class="mf-dl mf-del" title="Remove this category (products go back to Uncategorised)"
            onclick="event.stopPropagation();clearCategory('${fnameEsc}')">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>` : ''}
          <span id="master-arrow-${gIdx}" class="master-arrow${wasExpanded ? ' open' : ''}">▶</span>
        </span>
      </div>
      <div id="master-folder-${gIdx}" class="master-folder-body${wasExpanded ? ' open' : ''}">
        <div class="master-folder-content">
        ${!searchTerm ? `<input id="mf-q-${gIdx}" class="mf-q" type="text" placeholder="🔍 Filter within this ${masterMode === 'category' ? 'category' : 'catalogue'}…"
          value="${esc(masterFolderQ[fname] || '')}" oninput="filterFolder(${gIdx}, '${fnameEsc}', this.value)"
          onkeydown="stopEnterSubmit(event)">` : ''}
        ${bulkPricingBar}
        <div class="table-wrap">
          <table>
            <thead><tr>${(selAny && masterSelFolders.has(fname)) ? `<th class="c"><input type="checkbox" onclick="event.stopPropagation();toggleMasterFolderAll('${fnameEsc}', this.checked)" title="Select all loaded rows"></th>` : ''}<th class="c">Image</th><th>Product</th><th>Brand</th><th>Model</th>${headerCells}<th class="c">GST%</th><th class="c">HSN</th></tr></thead>
            <tbody>${prods.map(i => `<tr>
              ${(selAny && masterSelFolders.has(fname)) ? `<td class="c"><input type="checkbox" id="msel-${i.id}" ${masterSel.has(i.id) ? 'checked' : ''} onclick="toggleMasterRow(${i.id}, this.checked)"></td>` : ''}
              <td class="c">${i.has_image
                ? `<img src="${API}/api/image/${i.image_path}" style="width:64px;height:52px;object-fit:contain;border-radius:4px;border:1px solid #ddd;cursor:zoom-in;" onclick="showImageLightbox('${API}/api/image/${i.image_path}')" title="Click to enlarge" onerror="this.style.display='none'">`
                : `<span style="color:#ccc;font-size:var(--fs-xs);">—</span>`}</td>
              <td><strong>${i.product}</strong></td>
              <td>${i.brand||'—'}</td>
              <td style="font-size:var(--fs-sm)">${i.original_model||'—'}</td>
              ${costCell(i)}${priceCells(i)}${marginCell(i)}
              <td class="c">${i.gst_pct||0}%</td>
              <td class="c">${i.hsn_code||'—'}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
        ${(!searchTerm && prods.length < g.total) ? `
        <div style="text-align:center;padding:10px 0 2px;">
          <button class="btn btn-sm" onclick="loadMoreFolder('${fnameEsc}')">
            Load more — showing ${prods.length} of ${g.total}
          </button>
        </div>` : ''}
        </div>
      </div>
    </div>`;
  }).join('');

  const barSlot = document.getElementById('master-sel-bar-slot');
  if (barSlot) {
    if (selAny) {
      const batchOpts = masterSummary.map(f =>
        `<option value="${esc(f.file_name)}">${f.file_name}</option>`).join('');
      const catOpts = masterCatList.map(c =>
        `<option value="${esc(c)}">${c}</option>`).join('');
      barSlot.innerHTML = `
      <div class="master-sel-bar">
        <strong id="msel-count">${masterSel.size} selected</strong>
        <span style="flex:1"></span>
        <select onchange="masterSelMove(this)"><option value="">Move to batch…</option>${batchOpts}</select>
        <select onchange="masterSelCategory(this)"><option value="">Set category…</option>${catOpts}</select>
        <input id="msel-newcat" type="text" placeholder="…or new category name" style="width:170px"
          value="${esc(pendingNewCat)}"
          onkeydown="if(event.key==='Enter'){event.preventDefault();masterSelNewCat();}">
        <button class="btn btn-sm" onclick="masterSelNewCat()">Set</button>
        <button class="btn btn-sm btn-danger" onclick="masterSelDelete()">🗑 Delete</button>
        <button class="btn btn-sm" onclick="toggleMasterSelect()">Done</button>
      </div>`;
    } else {
      barSlot.innerHTML = '';
    }
  }
}

async function updateMasterPrice(id, price3, price4) {
  const p3 = parseFloat(price3), p4 = parseFloat(price4);
  if (isNaN(p3) || isNaN(p4) || p3 < 0 || p4 < 0) {
    toast('Enter a valid, non-negative price.', 'error');
    loadMasterTable();
    return;
  }
  try {
    const res = await fetch(`${API}/api/master-table/product/${id}`, {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({price_3star: p3, price_4star: p4})
    });
    if (!res.ok) {
      const d = await res.json();
      toast(d.detail || 'Update failed', 'error');
      loadMasterTable();
    }
  } catch (e) {
    toast('Update failed: ' + e.message, 'error');
    loadMasterTable();
  }
}

async function applyBulkTierPricing(gIdx, fname) {
  const pct3 = document.getElementById(`bulk-pct3-${gIdx}`).value;
  const pct4 = document.getElementById(`bulk-pct4-${gIdx}`).value;
  const usdRateEl = document.getElementById(`bulk-usdrate-${gIdx}`);
  const usdRate = usdRateEl ? usdRateEl.value : '';
  const statusEl = document.getElementById(`bulk-status-${gIdx}`);
  // Two valid ways to use this: both percentages (recompute ₹ off cost, USD
  // rate optional), or the $ rate alone (just convert the existing ₹ prices).
  const bothPct = pct3 !== '' && pct4 !== '';
  const usdOnly = pct3 === '' && pct4 === '' && usdRate !== '';
  if (!bothPct && !usdOnly) {
    statusEl.innerHTML = '<span style="color:#c0392b;">Enter both %, or just the $ rate on its own.</span>';
    return;
  }
  const btn = document.getElementById(`bulk-apply-${gIdx}`);
  btn.disabled = true; btn.textContent = 'Applying...';
  statusEl.innerHTML = '';
  try {
    const res = await fetch(`${API}/api/master-table/bulk-tier-pricing/${encodeURIComponent(fname)}`, {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        pct_3star: pct3 === '' ? null : parseFloat(pct3),
        pct_4star: pct4 === '' ? null : parseFloat(pct4),
        usd_rate: usdRate === '' ? 0 : parseFloat(usdRate)
      })
    });
    const d = await res.json();
    if (!res.ok) {
      statusEl.innerHTML = `<span style="color:#c0392b;">${apiErr(d)}</span>`;
    } else {
      statusEl.innerHTML = `<span style="color:#1e9e56;">✅ ${d.message}</span>`;
      loadMasterTable();
    }
  } catch (e) {
    statusEl.innerHTML = `<span style="color:#c0392b;">${e.message}</span>`;
  }
  btn.disabled = false; btn.textContent = `Apply to all`;
}

async function resetBulkTierPricing(gIdx, fname) {
  const statusEl = document.getElementById(`bulk-status-${gIdx}`);
  const btn = document.getElementById(`bulk-reset-${gIdx}`);

  btn.disabled = true; btn.classList.add('spinning');
  statusEl.innerHTML = '';
  try {
    const res = await fetch(`${API}/api/master-table/reset-tier-pricing/${encodeURIComponent(fname)}`,
                            { method: 'PUT' });
    const d = await res.json();
    if (!res.ok) {
      statusEl.innerHTML = `<span style="color:#c0392b;">${apiErr(d)}</span>`;
    } else {
      statusEl.innerHTML = `<span style="color:#1e9e56;">✅ ${d.message}</span>`;
      // Clear the inputs too, so the panel doesn't still show the values that
      // produced the pricing we just undid.
      ['bulk-pct3', 'bulk-pct4', 'bulk-usdrate'].forEach(p => {
        const el = document.getElementById(`${p}-${gIdx}`);
        if (el) el.value = '';
      });
      loadMasterTable();
    }
  } catch (e) {
    statusEl.innerHTML = `<span style="color:#c0392b;">${e.message}</span>`;
  }
  btn.disabled = false; btn.classList.remove('spinning');
}

async function toggleMasterFolder(idx, fname) {
  const content = document.getElementById(`master-folder-${idx}`);
  const arrow = document.getElementById(`master-arrow-${idx}`);
  const open = content.classList.contains('open');
  content.classList.toggle('open', !open);
  arrow.classList.toggle('open', !open);
  // First expand fetches the folder's first page (search results already
  // carry their rows, so only the normal folder view lazy-loads).
  if (!open && !masterSearchTerm && fname) {
    const g = masterFolders[fname];
    if (g && !g.items.length && g.total) await loadMoreFolder(fname);
  }
}

// ── Catalog Selector ─────────────────────────────────────────────────────────
async function loadCatalogSelector() {
  const res = await fetch(`${API}/api/boq-files`);
  const files = await res.json();
  const el = document.getElementById('catalog-selector');
  if (!files.length) {
    el.innerHTML = '<span style="color:var(--muted);font-size:var(--fs-base);">No catalogs uploaded yet.</span>';
    return;
  }
  el.innerHTML = files.map(f => `
    <label style="display:flex;align-items:center;gap:8px;padding:8px 14px;background:#fff;border:1.5px solid var(--border);border-radius:8px;cursor:pointer;font-size:var(--fs-base);user-select:none;" id="lbl-${CSS.escape(f.file_name)}">
      <input type="checkbox" class="catalog-check" value="${f.file_name}" onchange="highlightCatalogLabel(this)" style="width:16px;height:16px;cursor:pointer;">
      <span>📁 ${f.file_name}</span>
      <span style="background:var(--accent);color:#fff;font-size:var(--fs-xs);padding:1px 7px;border-radius:10px;">${f.count}</span>
    </label>`).join('');
}

function highlightCatalogLabel(cb) {
  const lbl = cb.closest('label');
  if (cb.checked) {
    lbl.style.borderColor = 'var(--primary)';
    lbl.style.background = '#eaf4fb';
  } else {
    lbl.style.borderColor = 'var(--border)';
    lbl.style.background = '#fff';
  }
}

function getSelectedCatalogs() {
  return Array.from(document.querySelectorAll('.catalog-check:checked')).map(c => c.value);
}

// ── Price Tier ───────────────────────────────────────────────────────────────
let selectedTiers = [];   // nothing pre-selected — the user must actively choose

function toggleTier(tier) {
  const isSelected = selectedTiers.includes(tier);
  selectedTiers = isSelected ? selectedTiers.filter(t => t !== tier) : [...selectedTiers, tier];
  const el = document.getElementById(`tier-${tier}`);
  el.classList.toggle('active', !isSelected);
  // Inline because a stylesheet .active rule mysteriously never lands on
  // these two specific nodes (a clone of them styles fine) — engine quirk.
  const dot = el.querySelector('.tc-dot');
  if (!isSelected) {
    el.style.setProperty('background', 'var(--accent-soft2, #efeafe)', 'important');
    el.style.setProperty('border', '1.5px solid var(--primary, #6a4cf0)', 'important');
    if (dot) { dot.style.borderColor = 'var(--primary, #6a4cf0)';
               dot.style.borderWidth = '5px'; dot.style.background = 'var(--card)'; }
  } else {
    el.style.removeProperty('background');
    el.style.removeProperty('border');
    if (dot) { dot.style.borderColor = ''; dot.style.borderWidth = ''; dot.style.background = ''; }
  }
  el.classList.remove('tier-pop'); void el.offsetWidth; el.classList.add('tier-pop');
}


// ── Type-ahead product suggestions (Generate Quote textarea) ─────────────────
let suggestTimer = null;
let suggestResults = [];
let suggestActiveIdx = -1;

const SUGGEST_STOPWORDS = new Set(['give','me','need','want','please','the','a','an','and',
  'for','of','to','in','at','than','then','greater','less','under','above','over','below',
  'between','with','also','some','more','around','about']);

function currentWords(textarea) {
  // Comma still splits distinct items when the user writes that way
  // ("10 irons, 5 towels"), but real single-sentence requests often have no
  // commas at all ("give me 90 Colander Dia under 1000") — so within
  // whichever chunk we're in, narrow further to the last couple of words
  // actively being typed, skipping quantities and filler/price words.
  // Returns {phrase, start} — phrase for searching, start (offset into the
  // full textarea value) for precise replacement on pick.
  const cursor = textarea.selectionStart;
  const upToCursor = textarea.value.slice(0, cursor);
  const chunkStart = upToCursor.lastIndexOf(',') + 1;
  const chunk = upToCursor.slice(chunkStart);
  const tokenRe = /\S+/g;
  const tokens = [];
  let m;
  while ((m = tokenRe.exec(chunk))) {
    const clean = m[0].replace(/[^a-z0-9]/gi, '');
    if (clean && !/^\d+$/.test(clean) && !SUGGEST_STOPWORDS.has(clean.toLowerCase())) {
      tokens.push({index: m.index});
    }
  }
  if (!tokens.length) return { phrase: '', start: cursor };
  const picked = tokens.slice(-2);
  const start = chunkStart + picked[0].index;
  return { phrase: chunk.slice(picked[0].index).trim(), start };
}

function onPromptInput(e) {
  clearTimeout(suggestTimer);
  const { phrase } = currentWords(e.target);
  if (phrase.length < 2) { hideSuggestions(); return; }
  suggestTimer = setTimeout(() => fetchSuggestions(phrase), 200);
}

async function fetchSuggestions(term) {
  const tier = selectedTiers[0] || '3star';
  const isStale = () => currentWords(document.getElementById('req-prompt')).phrase !== term;
  try {
    let res = await fetch(`${API}/api/master-table/suggest?q=${encodeURIComponent(term)}&tier=${tier}`);
    let data = res.ok ? await res.json() : [];
    // A multi-word phrase ("Colander Dia") is more precise, but if it finds
    // nothing, retry with just the last word — better a broader match than
    // none at all.
    const lastWord = term.split(/\s+/).pop();
    if (!data.length && lastWord !== term) {
      res = await fetch(`${API}/api/master-table/suggest?q=${encodeURIComponent(lastWord)}&tier=${tier}`);
      data = res.ok ? await res.json() : [];
    }
    if (isStale()) return;  // textarea moved on while this request was in flight
    renderSuggestions(data);
  } catch (e) { hideSuggestions(); }
}

function renderSuggestions(results) {
  suggestResults = results;
  suggestActiveIdx = -1;
  const box = document.getElementById('suggest-dropdown');
  if (!results.length) { hideSuggestions(); return; }
  box.innerHTML = `<div class="suggest-header">Matching products · lowest price first</div>` +
    `<div class="suggest-list">` +
    results.map((r, i) => `
      <div class="suggest-opt" data-idx="${i}" style="animation-delay:${Math.min(i, 10) * 30}ms" onmousedown="event.preventDefault(); pickSuggestion(${i})">
        <span class="so-name">${r.product}${r.brand ? ` <span class="so-brand">· ${r.brand}</span>` : ''}</span>
        <span class="so-price">₹${(r.price||0).toLocaleString('en-IN')}</span>
      </div>`).join('') +
    `</div>`;
  box.style.display = 'block';
}

function hideSuggestions() {
  suggestResults = [];
  suggestActiveIdx = -1;
  document.getElementById('suggest-dropdown').style.display = 'none';
}

function pickSuggestion(i) {
  const r = suggestResults[i];
  if (!r) return;
  const textarea = document.getElementById('req-prompt');
  const cursor = textarea.selectionStart;
  const before = textarea.value.slice(0, cursor);
  const after = textarea.value.slice(cursor);

  // Replace only the word actively being typed (same word currentSegment
  // matched against) with the full product name — everything else in the
  // sentence, before and after the cursor, stays exactly as written.
  const chunkStart = before.lastIndexOf(',') + 1;
  const chunk = before.slice(chunkStart);
  const wordMatch = chunk.match(/(\S+)$/);
  const wordStart = wordMatch ? chunkStart + chunk.length - wordMatch[1].length : cursor;

  const newValue = before.slice(0, wordStart) + r.product + after;
  textarea.value = newValue;
  const newCursor = wordStart + r.product.length;
  textarea.focus();
  textarea.setSelectionRange(newCursor, newCursor);
  hideSuggestions();
}

function onPromptKeydown(e) {
  if (!suggestResults.length || document.getElementById('suggest-dropdown').style.display === 'none') return;
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    suggestActiveIdx = Math.min(suggestActiveIdx + 1, suggestResults.length - 1);
    updateSuggestActive();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    suggestActiveIdx = Math.max(suggestActiveIdx - 1, 0);
    updateSuggestActive();
  } else if (e.key === 'Enter' && suggestActiveIdx >= 0) {
    e.preventDefault();
    pickSuggestion(suggestActiveIdx);
  } else if (e.key === 'Escape') {
    hideSuggestions();
  }
}

function updateSuggestActive() {
  document.querySelectorAll('#suggest-dropdown .suggest-opt').forEach((el, i) => {
    el.classList.toggle('suggest-active', i === suggestActiveIdx);
  });
}

const promptTextarea = document.getElementById('req-prompt');
if (promptTextarea) {
  promptTextarea.addEventListener('input', onPromptInput);
  promptTextarea.addEventListener('keydown', onPromptKeydown);
  promptTextarea.addEventListener('blur', () => setTimeout(hideSuggestions, 150));
}

// ── Variant Picker ───────────────────────────────────────────────────────────
let vpGroups = [];   // holds last /api/variants response
let vpClient = '';

function renderVariantPicker(data) {
  vpGroups  = data.groups || [];
  vpClient  = data.client_name || '';

  const found    = vpGroups.filter(g => g.found);
  const notFound = vpGroups.filter(g => !g.found);

  // Not-found banner
  const nfBox = document.getElementById('vp-notfound-box');
  if (notFound.length) {
    nfBox.style.display = 'block';
    document.getElementById('vp-notfound-items').textContent =
      notFound.map(g => g.requested).join(', ');
  } else {
    nfBox.style.display = 'none';
  }

  document.getElementById('vp-summary').textContent =
    `${found.length} product type(s) found — pick your preferred variant for each`;

  const container = document.getElementById('vp-groups');
  container.innerHTML = found.map((g, gi) => {
    const multi = g.variants.length > 1;
    const inputType = 'checkbox';   // allow picking multiple variants

    const variantRows = g.variants.map((v, vi) => {
      const priceINR = `₹${fmt(v.price||0, 2)}`;   // USD column removed — show as-is in INR
      const priceClass = '';
      const fileShort  = (v.file_name||'').replace(/QUOTATION FOR /i,'').substring(0,30);
      const checked    = vi === 0 && (v.price||0) > 0 ? 'checked' : '';
      const rowId      = `vp-${gi}-${vi}`;
      return `
        <label class="vp-variant-row ${checked ? 'selected' : ''}" id="row-${rowId}" onclick="vpToggle(this)">
          <input type="${inputType}" name="vp-group-${gi}" id="${rowId}"
            data-gi="${gi}" data-vi="${vi}" ${checked} onchange="vpToggle(this.closest('label'))">
          <span class="vp-prod-name">${v.product||''}</span>
          <span class="vp-price ${priceClass}">${priceINR}</span>
          <span class="vp-file-tag">📁 ${fileShort}</span>
        </label>`;
    }).join('');

    return `
      <div class="vp-group">
        <div class="vp-group-header">
          <span class="vp-group-icon">📦</span>
          <span class="vp-group-name">${g.requested}</span>
          <span class="vp-group-badge vp-badge-found">${g.variants.length} variant${g.variants.length>1?'s':''}</span>
        </div>
        <div class="vp-variants">${variantRows}</div>
        <div class="vp-qty-row">
          <label>Qty requested:</label>
          <input type="number" id="vp-qty-${gi}" value="${g.qty}" min="1">
        </div>
      </div>`;
  }).join('');

  show('variants');
}

function vpToggle(label) {
  label.classList.toggle('selected', label.querySelector('input').checked);
}

function vpBestPrice() {
  // For each group, check only the first variant (already sorted best price first)
  vpGroups.forEach((g, gi) => {
    g.variants.forEach((v, vi) => {
      const inp = document.getElementById(`vp-${gi}-${vi}`);
      const row = document.getElementById(`row-vp-${gi}-${vi}`);
      if (!inp) return;
      inp.checked = vi === 0 && (v.price||0) > 0;
      row?.classList.toggle('selected', inp.checked);
    });
  });
}

async function buildQuotation() {
  const selectedItems = [];
  vpGroups.forEach((g, gi) => {
    const qty = parseInt(document.getElementById(`vp-qty-${gi}`)?.value) || g.qty || 1;
    g.variants.forEach((v, vi) => {
      const inp = document.getElementById(`vp-${gi}-${vi}`);
      if (inp && inp.checked) {
        selectedItems.push({ ...v, qty, price_per_pc: v.price, sl_no: selectedItems.length + 1 });
      }
    });
  });

  if (!selectedItems.length) { toast('Please select at least one variant.', 'error'); return; }

  const btn = document.querySelector('#sec-variants .btn-primary');
  btn.disabled = true; btn.textContent = '⏳ Building...';

  try {
    const res = await fetch(`${API}/api/build-quotation`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ client_name: vpClient, items: selectedItems })
    });
    const q = await res.json();
    if (!res.ok) { toast(q.detail || 'Build failed', 'error'); return; }
    currentQuotation = q;
    renderResult(q);
    show('result');
  } catch(e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = '📋 Build Quotation →';
  }
}

// ── Generate ─────────────────────────────────────────────────────────────────
async function generateQuote() {
  const prompt = document.getElementById('req-prompt').value.trim();
  const client = document.getElementById('client-name')?.value.trim() || '';
  if (!prompt) { toast('Please enter customer requirements.', 'error'); return; }
  // No tier picked is fine: the quote prices off the base price and shows a
  // single PRICE column (no star columns) — never a roadblock.
  const tiersToUse = selectedTiers;

  const btn = document.getElementById('gen-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="loader"></span> Generating...';
  document.getElementById('gen-status').innerHTML = '<div class="alert alert-info">AI is analyzing requirements and matching products...</div>';

  try {
    const selectedCatalogs = getSelectedCatalogs();
    document.getElementById('gen-status').innerHTML = '<div class="alert alert-info">🤖 AI is matching your requirements to the catalog...</div>';
    const res = await fetch(`${API}/api/smart-generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, client_name: client, catalogs: selectedCatalogs, tiers: tiersToUse })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Generation failed');
    if (data.unsaved) {
      document.getElementById('gen-status').innerHTML =
        `<div class="alert alert-error">Nothing matched${(data.not_found||[]).length ? ` — not found: ${data.not_found.join(', ')}` : ''}. No quotation was saved.</div>`;
      return;
    }
    currentQuotation = data;
    renderResult(data);
    show('result');
    const nf = (data.not_found||[]).length;
    document.getElementById('gen-status').innerHTML =
      `<div class="alert alert-success">✅ Quotation ready — ${(data.items||[]).length} item(s) matched.${nf ? ` ⚠️ ${nf} not found.` : ''}</div>`;
  } catch (e) {
    document.getElementById('gen-status').innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span>✨</span> Generate Quotation';
  }
}

// ── Generate from an uploaded client BOQ (requirement) file ───────────────────
// File-based counterpart to generateQuote() — same Master Table matching,
// same tier selection, just a client's Excel instead of typed text.
const boqReqDropZone  = document.getElementById('boq-req-drop-zone');
const boqReqFileInput = document.getElementById('boq-req-file-input');
let boqReqSelectedFile = null;

boqReqDropZone.addEventListener('dragover', e => { e.preventDefault(); boqReqDropZone.classList.add('drag'); });
boqReqDropZone.addEventListener('dragleave', () => boqReqDropZone.classList.remove('drag'));
boqReqDropZone.addEventListener('drop', e => {
  e.preventDefault(); boqReqDropZone.classList.remove('drag');
  setBoqReqFile(e.dataTransfer.files[0]);
});
boqReqFileInput.addEventListener('change', e => setBoqReqFile(e.target.files[0]));

function setBoqReqFile(file) {
  if (!file) return;
  boqReqSelectedFile = file;
  boqReqDropZone.querySelector('p').innerHTML = `<strong>${escHtml(file.name)}</strong> selected`;
  boqReqDropZone.classList.add('has-file');
  document.getElementById('check-boq-btn').disabled = false;
  // Admin-only, and hidden outright for everyone else — see onAuthed()
  const ma = document.getElementById('margin-analyse-btn');
  if (ma) ma.disabled = false;
  document.getElementById('gen-boq-btn').disabled = false;
  const chosen = document.getElementById('boq-file-chosen');
  chosen.style.display = 'flex';
  chosen.innerHTML = `<span><b>${escHtml(file.name)}</b></span>
    <button type="button" onclick="clearBoqReqFile()" title="Remove this file">✕ clear</button>`;
  document.getElementById('gen-boq-status').innerHTML = '';
  document.getElementById('boq-coverage').innerHTML = '';
}

function clearBoqReqFile() {
  boqReqSelectedFile = null;
  boqReqFileInput.value = '';
  boqReqDropZone.classList.remove('has-file');
  boqReqDropZone.querySelector('p').innerHTML = '<strong>Click or drag &amp; drop</strong> the file here';
  document.getElementById('boq-file-chosen').style.display = 'none';
  document.getElementById('check-boq-btn').disabled = true;
  document.getElementById('gen-boq-btn').disabled = true;
  const ma = document.getElementById('margin-analyse-btn');
  if (ma) ma.disabled = true;
  ['gen-boq-status', 'boq-coverage'].forEach(id => {
    document.getElementById(id).innerHTML = '';
  });
}

// ── Phase A: show how each column will be read, before anything is imported ──
// The parser used to decide silently: the Nilkamal sheet's "PRICES IN INR"
// went unrecognised and 493 products imported with no price at all, which
// only surfaced days later. This makes that decision visible at scan time.
// Phase E: warn when the file doesn't look like a supplier price list.
// Importing a past quotation as a catalog is silent and expensive — it turned
// 705 lines of a client's own BOQ wording into "products", loaded our old
// selling price into the cost column, and left GST at 0% on every row.
function renderFileTypeNotice(ft, expected = 'price_list') {
  if (!ft || ft.type === expected || ft.type === 'unknown') return '';
  const heads = { price_list: "This doesn't look like a supplier price list",
                  client_boq: "This doesn't look like a client requirement / BOQ" };
  const why = (ft.reasons || []).map(r => `<li>${r}</li>`).join('');
  return `
    <div class="ftype-warn">
      <div class="ftype-head">⚠️ ${heads[expected] || heads.price_list}</div>
      <div class="ftype-body">
        It looks like <strong>${ft.label}</strong>.
        ${ft.type === 'quotation'
          ? 'Importing a quotation as a catalog stores the client\'s wording as product names and your <em>selling</em> price as cost — margins and matching both go wrong.'
          : 'A requirement sheet has no pricing, so the products would import with no price.'}
        <ul>${why}</ul>
        You can still import it if that\'s what you intended.
      </div>
    </div>`;
}

let mappableFields = [];   // [{field,label}] — loaded once, used by every picker

async function ensureMappableFields() {
  if (mappableFields.length) return mappableFields;
  try {
    const res = await fetch(`${API}/api/master-table/column-mappings`);
    if (res.ok) mappableFields = (await res.json()).fields || [];
  } catch (e) { /* picker falls back to a plain list below */ }
  return mappableFields;
}

function renderColumnReport(cols) {
  if (!cols || !cols.columns || !cols.columns.length) return '';
  const isAdmin = currentUser && currentUser.role === 'admin';
  const bad = cols.columns.filter(c => !c.recognised).length;

  const picker = (c, i) => {
    if (!isAdmin) return `<span class="colmap-none">not recognised — ignored</span>`;
    const opts = mappableFields.map(f =>
      `<option value="${f.field}"${f.field === c.suggested ? ' selected' : ''}>${f.label}</option>`).join('');
    // A suggestion is pre-selected but never applied on its own — the admin
    // still has to press Teach. Guessing a price column silently is how a
    // whole catalog gets mispriced.
    return `
      <div class="colmap-fix">
        <select id="cmsel-${i}" class="colmap-select">
          <option value="">— ignore this column —</option>${opts}
        </select>
        <button class="btn btn-sm btn-primary" onclick="teachColumn(${i}, '${String(c.header).replace(/'/g, "\\'")}')">Teach</button>
        ${c.suggested ? `<span class="colmap-why">suggested: ${c.reason}</span>` : ''}
      </div>`;
  };

  const rows = cols.columns.map((c, i) => `
    <tr class="${c.recognised ? '' : 'colmap-warn'}" id="cmrow-${i}">
      <td class="colmap-head">${c.header}</td>
      <td class="colmap-tick">${c.recognised ? '✓' : '✕'}</td>
      <td>${c.recognised
            ? `<span class="colmap-field">${c.label || c.field}</span>${
                c.source === 'learned' ? ' <span class="colmap-learned">learned</span>' : ''}`
            : picker(c, i)}</td>
      <td class="colmap-sample">${c.sample ? String(c.sample).slice(0, 44) : '—'}</td>
    </tr>`).join('');

  return `
    <div class="colmap">
      <div class="colmap-bar">
        <strong>How your columns will be read</strong>
        <span class="colmap-ok">✓ ${cols.recognised_count} matched</span>
        ${bad ? `<span class="colmap-bad">✕ ${bad} not recognised</span>` : ''}
        <span class="mbp-hint">sheet "${cols.sheet}", header on row ${cols.header_row}</span>
      </div>
      <div class="table-wrap" style="max-height:300px;overflow:auto;">
        <table>
          <thead><tr><th>Column in your file</th><th></th><th>Understood as</th><th>First value</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
}

// Teach the system what one unrecognised header means. The lesson persists,
// so the next file using that wording maps itself (Phase B).
async function teachColumn(idx, header) {
  const sel = document.getElementById(`cmsel-${idx}`);
  const row = document.getElementById(`cmrow-${idx}`);
  if (!sel || !row) return;
  const field = sel.value;
  const btn = row.querySelector('button');
  btn.disabled = true; btn.textContent = '...';
  try {
    const res = await fetch(`${API}/api/master-table/confirm-mapping`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ header, field })
    });
    const d = await res.json();
    if (!res.ok) {
      btn.disabled = false; btn.textContent = 'Teach';
      row.insertAdjacentHTML('afterend',
        `<tr><td colspan="4" class="colmap-msg err">${apiErr(d, 'Could not save')}</td></tr>`);
      return;
    }
    row.classList.remove('colmap-warn');
    row.querySelector('.colmap-tick').textContent = '✓';
    row.querySelector('td:nth-child(3)').innerHTML = field
      ? `<span class="colmap-field">${sel.options[sel.selectedIndex].text}</span> <span class="colmap-learned">learned</span>`
      : `<span class="colmap-none">ignored on purpose</span>`;
    row.insertAdjacentHTML('afterend',
      `<tr><td colspan="4" class="colmap-msg ok">${d.message} — re-scan the file to apply it.</td></tr>`);
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Teach';
  }
}

// ── Motive 3: does the Master Table already cover this client's BOQ? ─────────
// Read-only — reports what we stock and what we don't. Nothing is written and
// no quotation is created; adding the missing products is a separate,
// explicit admin action below.
let boqMissingItems = [];

async function checkBoqCoverage() {
  if (!boqReqSelectedFile) return;
  const btn = document.getElementById('check-boq-btn');
  const out = document.getElementById('boq-coverage');
  btn.disabled = true;
  btn.innerHTML = '<span class="loader"></span> Checking...';
  out.innerHTML = '<div class="alert alert-info">Matching every BOQ line against the Master Table...</div>';

  const fd = new FormData();
  fd.append('file', boqReqSelectedFile);
  try {
    const res = await fetch(`${API}/api/master-table/check-boq`, { method: 'POST', body: fd });
    const d = await res.json();
    if (res.status === 422 && d.teachable) { await renderBoqTeach(d, out); btn.disabled = false; btn.innerHTML = '🔍 Check what we stock'; return; }
    if (!res.ok) throw new Error(apiErr(d, 'Coverage check failed'));
    boqMissingItems = d.missing || [];
    renderBoqCoverage(d);
  } catch (e) {
    out.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
  btn.disabled = false;
  btn.innerHTML = '🔍 Check what we stock';
}

// The file's columns weren't recognised — let the admin teach them right
// here, then re-check. Teaching writes to the shared column_mappings table.
async function renderBoqTeach(d, out) {
  const isAdmin = currentUser && currentUser.role === 'admin';
  if (!isAdmin) {
    out.innerHTML = `<div class="alert alert-error">${d.detail} An admin can teach this file's column names.</div>`;
    return;
  }
  await ensureMappableFields();
  const opts = f => mappableFields.map(x =>
    `<option value="${x.field}"${x.field === f ? ' selected' : ''}>${x.label}</option>`).join('');
  const rows = (d.headers || []).map((h, i) => `
    <tr id="bteach-${i}">
      <td class="colmap-head">${escHtml(h.header)}</td>
      <td><select id="bsel-${i}" class="colmap-select">
        <option value="">— ignore —</option>${opts(h.suggested)}</select></td>
      <td><button class="btn btn-sm btn-primary"
        onclick="teachBoqCol(${i}, '${String(h.header).replace(/'/g, "\\'")}')">Teach</button></td>
    </tr>`).join('');
  out.innerHTML = `
    <div class="alert alert-error">${d.detail}</div>
    <div class="colmap">
      <div class="colmap-bar">These are the column headings the file uses — tell the
        system what each one means, then re-check.</div>
      <table><thead><tr><th>Heading in file</th><th>Means</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
      <button class="btn btn-sm btn-primary" style="margin-top:10px"
        onclick="checkBoqCoverage()">↻ Re-check this file</button>
    </div>`;
}

async function teachBoqCol(i, header) {
  const sel = document.getElementById(`bsel-${i}`);
  const row = document.getElementById(`bteach-${i}`);
  if (!sel || !row) return;
  const res = await fetch(`${API}/api/master-table/confirm-mapping`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ header, field: sel.value })
  });
  row.style.opacity = res.ok ? '.55' : '1';
  if (!res.ok) toast('Could not save that mapping.', 'error');
}

function renderBoqCoverage(d) {
  const out = document.getElementById('boq-coverage');
  const typeNotice = renderFileTypeNotice(d.file_type, 'client_boq');
  const isAdmin = currentUser && currentUser.role === 'admin';
  const pct = d.coverage_pct;
  const bar = `
    <div class="cov-bar"><div class="cov-fill" style="width:${pct}%"></div></div>
    <div class="cov-legend">
      <span><b>${d.total}</b> lines in the BOQ</span>
      <span class="cov-ok">✔ ${d.found_count} we stock</span>
      <span class="cov-miss">✕ ${d.missing_count} we don't</span>
      <span class="cov-pct">${pct}% covered</span>
    </div>`;

  let missing = '';
  if (d.missing_count) {
    missing = `
      <div class="cov-missing">
        <div class="cov-missing-head">
          <strong>Not in the Master Table (${d.missing_count})</strong>
          ${isAdmin ? `<button class="btn btn-sm btn-primary" id="add-missing-btn"
              onclick="addMissingToMaster()">➕ Add all to Master Table</button>` : ''}
        </div>
        <div class="table-wrap" style="max-height:320px;overflow:auto;">
          <table><thead><tr><th>Image</th><th>Product</th><th>Model</th><th>Brand</th><th>Specification</th><th>Qty</th></tr></thead>
          <tbody>${d.missing.map(m => `<tr>
            <td>${m.image_path ? `<img class="cov-thumb" src="${API}/api/image/${m.image_path}" loading="lazy" alt="">` : '—'}</td>
            <td><strong>${m.product}</strong></td><td>${m.original_model || '—'}</td>
            <td>${m.brand || '—'}</td><td style="font-size:var(--fs-sm)">${m.specification || '—'}</td>
            <td>${m.qty}</td></tr>`).join('')}</tbody></table>
        </div>
        ${isAdmin ? '' : '<p class="mbp-hint">Only an admin can add products to the Master Table.</p>'}
      </div>`;
  } else {
    missing = `<div class="alert alert-success">Every line in this BOQ is already in the Master Table.</div>`;
  }
  out.innerHTML = typeNotice + bar + missing;
}

async function addMissingToMaster() {
  if (!boqMissingItems.length) return;
  const btn = document.getElementById('add-missing-btn');
  btn.disabled = true; btn.textContent = 'Adding...';
  try {
    const res = await fetch(`${API}/api/master-table/add-from-boq`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        file_name: `Added from BOQ — ${boqReqSelectedFile ? boqReqSelectedFile.name : 'client BOQ'}`,
        items: boqMissingItems
      })
    });
    const d = await res.json();
    const box = document.getElementById('boq-coverage');
    if (!res.ok) {
      box.innerHTML += `<div class="alert alert-error">${apiErr(d, 'Add failed')}</div>`;
    } else {
      box.innerHTML += `<div class="alert alert-success">${d.message}</div>`;
      loadMasterTable();
    }
  } catch (e) {
    document.getElementById('boq-coverage').innerHTML += `<div class="alert alert-error">${e.message}</div>`;
  }
  btn.disabled = false; btn.textContent = '➕ Add all to Master Table';
}

async function generateFromBoqFile() {
  if (!boqReqSelectedFile) return;
  // No tier picked is fine: the quote prices off the base price and shows a
  // single PRICE column (no star columns) — never a roadblock.
  const tiersToUse = selectedTiers;
  const client = document.getElementById('client-name')?.value.trim() || '';
  const btn = document.getElementById('gen-boq-btn');
  const status = document.getElementById('gen-boq-status');
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="loader"></span> Generating...';
  status.innerHTML = '<div class="alert alert-info">🤖 Reading the file and matching against the Master Table...</div>';

  const fd = new FormData();
  fd.append('file', boqReqSelectedFile);
  fd.append('client_name', client);
  fd.append('tiers', tiersToUse.join(','));

  try {
    const res = await fetch(`${API}/api/smart-generate-from-boq`, { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Generation failed');
    if (data.unsaved) {
      status.innerHTML = `<div class="alert alert-error">Nothing in this BOQ matched the Master Table. No quotation was saved.</div>`;
      btn.disabled = false; btn.innerHTML = label;
      return;
    }
    currentQuotation = data;
    renderResult(data);
    show('result');
    const nf = (data.not_found || []).length;
    status.innerHTML = `<div class="alert alert-success">✅ Quotation ready — ${(data.items||[]).length} item(s) matched.${nf ? ` ⚠️ ${nf} not found.` : ''}</div>`
      + renderFileTypeNotice(data.file_type, 'client_boq');
  } catch (e) {
    status.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = label;
  }
}

// Add forgotten items to the CURRENT quote (from the result screen)
// ── "Frequently quoted together" suggestions ─────────────────────────────────
// Mined from past quotations — the history is the training set. Refreshed
// whenever the quote's items change, so adding a suggestion can surface the
// next one.
async function addToQuote() {
  if (!currentQuotation) return;
  const inp = document.getElementById('add-item-input');
  const prompt = (inp.value || '').trim();
  if (!prompt) return;
  const btn = document.getElementById('add-item-btn');
  const status = document.getElementById('add-item-status');
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<span class="loader"></span> Adding...';
  status.innerHTML = 'Searching catalogues…';
  try {
    const res = await fetch(`${API}/api/smart-generate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, client_name: currentQuotation.client_name || '', catalogs: [] })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Add failed');
    const newItems = data.items || [];
    const nf = data.not_found || [];
    if (!newItems.length) {
      status.innerHTML = `<span style="color:#f5a097;">No matching product found${nf.length ? ': ' + nf.join(', ') : ''}.</span>`;
    } else {
      currentQuotation.items = (currentQuotation.items || []).concat(newItems);
      currentQuotation.items.forEach((it, i) => it.sl_no = i + 1);
      renderResult(currentQuotation);
      status.innerHTML = `<span style="color:#9fe1cb;">✅ Added ${newItems.length} item(s) to the quote.${nf.length ? ' ⚠️ Not found: ' + nf.join(', ') : ''}</span>`;
      inp.value = '';
    }
  } catch (e) {
    status.innerHTML = `<span style="color:#f5a097;">${e.message}</span>`;
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

// ── Result ───────────────────────────────────────────────────────────────────
function fmt(n, dec=0) { return n.toLocaleString('en-IN', {maximumFractionDigits: dec}); }

function renderItemRow(item, idx, show3, show4, hasBoqPricing, showOrig, showMargin) {
  const isINR   = (item.price_currency || 'INR') === 'INR';
  const qty     = item.qty || 0;
  const price   = item.price_per_pc || 0;
  const gst     = item.gst_pct ?? 18;  // ?? not || — a genuine 0% GST must stay 0%

  // Always convert to INR
  const priceInr = isINR ? price : price * usdToInr;
  const amtInr   = qty * priceInr;
  const gstVal   = amtInr * gst / 100;
  // Two different profit meanings, depending on where the quote came from:
  //  • BOQ flow  — what the client's BOQ said they'd pay minus our price.
  //  • Quotation flow — real margin: our selling price minus the Master
  //    Table cost. boq_price is 0 here, so the BOQ formula would render
  //    every line as a large negative number.
  // Both are line TOTALS (× qty), like Amount — not per-unit figures.
  const boqPrice = item.boq_price || 0;
  const cost     = item.cost || 0;
  const profit   = hasBoqPricing ? qty * (boqPrice - priceInr)
                                 : qty * (priceInr - cost);
  // Per-unit percentage, so it stays readable whatever the quantity.
  const marginPct = (cost > 0 && priceInr > 0) ? (priceInr - cost) * 100 / priceInr : null;

  item._priceInr = priceInr;
  item._amtInr   = amtInr;
  item._gstVal   = gstVal;
  item._cost     = cost;
  item._profit   = profit;

  const activeTier = item.active_tier || (item.tiers && item.tiers[0]) || '3star';

  const hasVariants = item._variants && item._variants.length > 1;
  // Placeholder rows get a catalogue search instead — the matcher found
  // nothing, so the user picks the right product by hand. Once picked, the
  // row keeps Find (choose differently) and gains Revert (back to the
  // client's original wording as a blank placeholder).
  const findBtn = `<button class="btn-switch" onclick="findProduct(${idx})" style="display:block;margin-top:4px;">
         <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><line x1="16.4" y1="16.4" x2="21" y2="21"/></svg> Find</button>`;
  const revertBtn = `<button class="btn-switch" onclick="revertFind(${idx})" title="Back to the client's original line" style="display:block;margin-top:4px;">
         <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg> Revert</button>`;
  const switchBtn = item.not_in_catalog
    ? findBtn
    : item.was_placeholder
      ? findBtn + revertBtn
      : (hasVariants
        ? `<button class="btn-switch" onclick="switchVariant(${idx})" style="display:block;margin-top:4px;">🔄 Switch</button>`
        : '');

  // Image cell — manually uploaded image first, else the catalog image (served
  // from disk via the API, never shipped inline in the JSON response)
  const imgSrc = item.local_image || (item.image_path ? `${API}/api/image/${item.image_path}` : '');
  const imgCell = imgSrc
    ? `<img src="${imgSrc}" style="width:84px;height:66px;object-fit:contain;border-radius:4px;border:1px solid #ddd;display:block;margin:0 auto;cursor:zoom-in;" onclick="showImageLightbox('${imgSrc.replace(/'/g,"\\'")}')" title="Click to enlarge">
       <span onclick="reAddImage(${idx})" style="cursor:pointer;font-size:var(--fs-xs);color:var(--primary);display:block;text-align:center;margin-top:2px;">✎ change</span>`
    : `<label style="cursor:pointer;font-size:var(--fs-xs);color:var(--primary);display:block;text-align:center;padding:4px;">
         📷 Add<br>Image
         <input type="file" accept="image/*" style="display:none" onchange="handleRowImage(${idx},this)">
       </label>`;

  // Not-in-catalogue placeholder: the details the master table would have
  // supplied are typed in by hand instead — each input writes straight back
  // onto the item, so Save Edits persists them like any price/qty change.
  const ph = !!item.not_in_catalog;
  const phInput = (f, v, w) =>
    `<input type="text" value="${(v||'').replace(/"/g,'&quot;')}" placeholder="fill in"
       onchange="setItemField(${idx},'${f}',this.value)" style="width:${w}px;font-size:var(--fs-sm)">`;

  // "Original" merges two distinct snapshots into one number per tier: a
  // price refresh (prev_price_*, set the first time refreshQuotePrices()
  // ever changes this line) takes priority when present; otherwise fall
  // back to the master table's pre-bulk-discount snapshot (orig_price_*).
  const orig3 = item.prev_price_3star != null ? item.prev_price_3star : item.orig_price_3star;
  const orig4 = item.prev_price_4star != null ? item.prev_price_4star : item.orig_price_4star;

  return `<tr data-idx="${idx}"${ph ? ' style="background:rgba(232,160,32,.07)"' : ''}>
    <td style="text-align:center;white-space:nowrap;"><span class="drag-h" title="Drag to reorder"
      style="cursor:grab;color:var(--muted);user-select:none;letter-spacing:1px;"
      onmousedown="this.closest('tr').draggable=true">⠿</span> ${item.sl_no||idx+1}</td>
    <td style="width:96px;text-align:center;">${imgCell}</td>
    <td><strong>${item.product||''}</strong>${switchBtn}</td>
    <td class="c"><input type="number" class="qty-input" value="${qty}" onchange="recalcRow(${idx})" style="width:60px"></td>
    <td style="font-size:var(--fs-sm)">${ph ? phInput('model_no', item.model_no, 90) : (item.model_no||'')}</td>
    <td>${ph ? phInput('brand', item.brand, 90) : (item.brand||'')}</td>
    <td>${ph ? phInput('specification', item.specification, 160)
             : `<div class="spec-text">${(item.specification||'').replace(/\\n/g,'\n')}</div>`}</td>
    <td>${ph ? phInput('hsn_code', item.hsn_code, 80) : (item.hsn_code||'')}</td>
    ${(() => {
      const cell3 = `<td class="col-3star">
        <div class="tier-price-cell">
          <span class="tier-tick ${activeTier==='3star'?'checked':''}" onclick="setRowTier(${idx},'3star')">${activeTier==='3star'?'✓':''}</span>
          <span class="tier-price-text">
            ${(showOrig && orig3 != null && orig3 !== item.price_3star)
              ? `<span class="tier-price-orig">₹${orig3.toLocaleString('en-IN')}</span>` : ''}
            <span class="tier-price-inr">₹${(item.price_3star||0).toLocaleString('en-IN')}</span><span class="tier-price-usd">$${(item.price_3star_usd||0).toFixed(2)}</span>
          </span>
        </div>
      </td>`;
      const cell4 = `<td class="col-4star">
        <div class="tier-price-cell">
          <span class="tier-tick ${activeTier==='4star'?'checked':''}" onclick="setRowTier(${idx},'4star')">${activeTier==='4star'?'✓':''}</span>
          <span class="tier-price-text">
            ${(showOrig && orig4 != null && orig4 !== item.price_4star)
              ? `<span class="tier-price-orig">₹${orig4.toLocaleString('en-IN')}</span>` : ''}
            <span class="tier-price-inr">₹${(item.price_4star||0).toLocaleString('en-IN')}</span><span class="tier-price-usd">$${(item.price_4star_usd||0).toFixed(2)}</span>
          </span>
        </div>
      </td>`;
      return (show3 ? cell3 : '') + (show4 ? cell4 : '');
    })()}
    <td class="num">
      <input type="number" step="0.01" class="price-input" value="${(Math.round(priceInr*100)/100)}" onchange="recalcRow(${idx})" style="width:100px">
      ${(item.price_3star || item.price_4star) ? `
      <select class="price-tier-pick" title="Set this line's price" onchange="const v=this.value; this.value=''; setTimeout(()=>setRowTier(${idx}, v));" style="display:block;margin:4px auto 0;width:100px;font-size:var(--fs-xs);">
        <option value="" selected disabled hidden>Set price</option>
        <option value="3star">3★ ₹${(item.price_3star||0).toLocaleString('en-IN')}</option>
        <option value="4star">4★ ₹${(item.price_4star||0).toLocaleString('en-IN')}</option>
        ${(orig3 != null || orig4 != null) ? `<option value="orig">Orig ₹${(activeTier==='4star' ? (orig4??orig3) : (orig3??orig4)).toLocaleString('en-IN')}</option>` : ''}
      </select>` : ''}
    </td>
    ${hasBoqPricing ? `<td class="boq-price-cell num">₹${fmt(boqPrice, 2)}</td>` : ''}
    ${showMargin ? `<td class="cost-cell num">₹${fmt(cost, 2)}</td>` : ''}
    ${(hasBoqPricing || showMargin) ? `<td class="profit-cell num" style="color:${profit >= 0 ? '#1e9e56' : '#d64545'};font-weight:600;">₹${fmt(profit)}</td>` : ''}
    ${showMargin ? `<td class="margin-cell num">${marginPct == null ? '—' : marginPct.toFixed(1) + '%'}</td>` : ''}
    <td class="amount-cell num">₹${fmt(amtInr)}</td>
    <td class="c"><input type="number" class="gst-input" value="${gst}" onchange="recalcRow(${idx})" style="width:50px"></td>
    <td class="gst-val-cell num">₹${fmt(gstVal)}</td>
    <td class="c"><button class="btn btn-sm btn-danger" onclick="removeRow(${idx})">✕</button></td>
  </tr>`;
}

// Drag-to-reorder: a row only becomes draggable on mousedown over its ⠿
// handle (so text/inputs in the row still select normally). Drop moves the
// item, renumbers SL and silently saves the new order.
let _dragFrom = null;
function initRowDrag() {
  const tb = document.getElementById('items-body');
  if (!tb) return;
  tb.querySelectorAll('tr[data-idx]').forEach(tr => {
    tr.addEventListener('dragstart', () => { _dragFrom = +tr.dataset.idx; tr.style.opacity = '.4'; });
    tr.addEventListener('dragend', () => {
      tr.style.opacity = ''; tr.draggable = false;
      tb.querySelectorAll('tr[data-idx]').forEach(t => t.style.outline = '');
    });
    tr.addEventListener('dragover', e => {
      e.preventDefault();
      tr.style.outline = '2px solid var(--primary)'; tr.style.outlineOffset = '-2px';
    });
    tr.addEventListener('dragleave', () => { tr.style.outline = ''; });
    tr.addEventListener('drop', e => {
      e.preventDefault();
      const to = +tr.dataset.idx;
      if (_dragFrom === null || _dragFrom === to || !currentQuotation) return;
      const its = currentQuotation.items;
      its.splice(to, 0, its.splice(_dragFrom, 1)[0]);
      its.forEach((it, i) => it.sl_no = i + 1);
      _dragFrom = null;
      renderResult(currentQuotation);
      saveEdits(true);
    });
  });
}

// Non-blocking replacement for alert(). The app had 18 of them: a native
// alert freezes the page and demands a click to acknowledge "Changes saved!",
// which is a lot of ceremony for a confirmation. Errors linger longer than
// successes; clicking a toast dismisses it early. textContent, never
// innerHTML — these carry server messages.
function toast(msg, type = 'info') {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  const kill = () => { el.classList.remove('in'); setTimeout(() => el.remove(), 220); };
  el.onclick = kill;
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add('in'));
  setTimeout(kill, type === 'error' ? 5200 : 3200);
}

// Placeholder rows: write a hand-typed field straight onto the item.
function setItemField(idx, field, val) {
  const it = currentQuotation && currentQuotation.items && currentQuotation.items[idx];
  if (it) it[field] = val.trim();
}

// "Margin analysis" is the same generation as the Generate button — it just
// says what you are looking for. Current Quote already renders BOQ Price,
// Profit and a total-profit footer whenever the uploaded file carried prices
// (renderResult, has_boq_pricing), and /api/download appends the same two as
// columns N/O. A second table and a second Excel were duplicating a view the
// app already had, so they are gone; this lands you on the built one.
async function analyseQuotationFile() {
  if (!boqReqSelectedFile) return;
  await generateFromBoqFile();
  if (!currentQuotation) return;                       // generation failed; it reported why
  // Margin needs the master's COST, not the file's prices — see renderResult.
  if (!(currentQuotation.items || []).some(i => (i.cost || 0) > 0)) {
    toast('No master cost on these lines, so margin cannot be worked out', 'error');
    return;
  }
  // Re-render with the margin columns on — this is the one entry point that
  // asks for them. Persisted without an underscore so reopening the saved
  // quote still shows margin rather than silently dropping back.
  currentQuotation.show_margin = true;
  renderResult(currentQuotation);
  const foot = document.getElementById('foot-profit');
  if (foot) {
    foot.scrollIntoView({ behavior: 'smooth', block: 'center' });
    foot.classList.add('flash-profit');
    setTimeout(() => foot.classList.remove('flash-profit'), 1600);
  }
}

// ── Hover details for Switch / Find cards ───────────────────────────────────
// Both panels render .switch-card, so one handler serves both. The panel is
// position:fixed and placed by JS rather than an absolute child, because
// .switch-cards is a scroll container (max-height 360px, overflow-y auto)
// and .quot-doc is overflow:hidden — an absolute popover gets clipped by
// both. Fixed also lets it flip when the card sits near a screen edge.
let _hoverTimer = null, _hoverHideTimer = null, _hoverCard = null;

function _hoverRow(el) {
  const pos = +el.dataset.hpos;
  if (el.dataset.hsrc === 'f') return _findResults[pos];
  const it = currentQuotation && currentQuotation.items[+el.dataset.hidx];
  return it && it._variants && it._variants[pos];
}

function _hoverPanelEl() {
  let p = document.getElementById('sc-pop');
  if (!p) {
    p = document.createElement('div');
    p.id = 'sc-pop';
    // The panel is a second, larger hit target for the card it describes:
    // keep it open while the pointer is inside it, and forward a click to
    // the card so selection stays in one place (its inline onclick).
    p.addEventListener('mouseenter', () => clearTimeout(_hoverHideTimer));
    p.addEventListener('mouseleave', _hoverHide);
    p.addEventListener('click', () => {
      const card = _hoverCard;
      _hoverHide(0);
      if (card && document.body.contains(card)) card.click();
    });
    document.body.appendChild(p);
  }
  return p;
}

function _hoverShow(el) {
  const v = _hoverRow(el);
  if (!v) return;
  _hoverCard = el;
  // Switch variants and Find suggestions carry the same facts under
  // different keys (model_no vs original_model, price vs price_3star).
  const model = v.model_no || v.original_model || '';
  const p3 = v.price_3star || v.price || 0;
  const p4 = v.price_4star || 0;
  const spec = (v.specification || v.description || '').replace(/\n/g, ' ').trim();
  const img = v.image_path ? `${API}/api/image/${v.image_path}` : '';
  const row = (k, val) => val
    ? `<div class="scp-row"><span class="scp-k">${k}</span><span>${escHtml(String(val))}</span></div>` : '';

  const p = _hoverPanelEl();
  p.innerHTML = `
    <div class="scp-top">
      <div class="scp-img">${img ? `<img src="${img}" alt="">`
                                 : '<span class="scp-noimg">no image</span>'}</div>
      <div class="scp-meta">
        <div class="scp-title">${escHtml(v.product || '')}</div>
        <div class="scp-tiers">
          <div class="scp-tier"><span>3★</span><b>₹${(p3 || 0).toLocaleString('en-IN')}</b></div>
          ${p4 ? `<div class="scp-tier"><span>4★</span><b>₹${p4.toLocaleString('en-IN')}</b></div>` : ''}
        </div>
        ${row('Model', model)}
        ${row('Brand', v.brand)}
        ${row('HSN', v.hsn_code)}
        ${row('GST', v.gst_pct != null ? v.gst_pct + '%' : '')}
      </div>
    </div>
    ${row('Catalogue', (v.file_name || '').replace(/\.xlsx?$/i, ''))}
    ${spec ? `<div class="scp-spec">${escHtml(spec)}</div>` : ''}`;

  // place it, flipping when the card is near the right or bottom edge
  p.classList.add('on');
  const r = el.getBoundingClientRect();
  const pw = p.offsetWidth, ph = p.offsetHeight, gap = 10;
  let left = r.left, top = r.bottom + gap;
  if (left + pw > innerWidth - 12) left = Math.max(12, r.right - pw);
  if (top + ph > innerHeight - 12) top = Math.max(12, r.top - ph - gap);
  p.style.left = left + 'px';
  p.style.top = top + 'px';
}

// Grace period, because there is a 10px gap between the card and the panel —
// hiding on the card's mouseout would kill the panel while the pointer is
// still travelling towards it.
function _hoverHide(delay = 140) {
  clearTimeout(_hoverTimer);
  clearTimeout(_hoverHideTimer);
  _hoverHideTimer = setTimeout(() => {
    const p = document.getElementById('sc-pop');
    if (p) p.classList.remove('on');
    _hoverCard = null;
  }, delay);
}

// Delegated, so cards rendered later are covered without rebinding.
document.addEventListener('mouseover', e => {
  const card = e.target.closest && e.target.closest('.switch-card[data-hsrc]');
  if (!card) return;
  clearTimeout(_hoverTimer);
  clearTimeout(_hoverHideTimer);   // moving back from the panel onto the card
  // short delay so sweeping the mouse across a grid does not strobe panels
  _hoverTimer = setTimeout(() => _hoverShow(card), 220);
});
document.addEventListener('mouseout', e => {
  const card = e.target.closest && e.target.closest('.switch-card[data-hsrc]');
  if (card) _hoverHide();
});
document.addEventListener('scroll', () => _hoverHide(0), true);
document.addEventListener('scroll', _hoverHide, true);

// ── Find in catalogue (placeholder rows) ────────────────────────────────────
// The matcher found nothing for this line, so give the user a live search
// over the master table; picking a result fills the whole row.
let _findResults = [];
let _findTimer = null;

// Shared card renderer for both the pre-loaded suggestions and live search
// results, so a suggestion behaves exactly like something you typed to find.
function _findCards(idx, list, isSuggestion) {
  _findResults = list;
  if (!list.length) {
    return `<p class="find-empty">Start typing — matching products appear here; click one to fill this row.</p>`;
  }
  const head = isSuggestion
    ? `<p class="find-empty" style="width:100%">Closest matches in the catalogue — click one, or search above:</p>` : '';
  return head + list.map((v, ri) => {
    const price = v.price_3star || v.price_4star || 0;
    const thumb = v.image_path ? `<img class="sc-thumb-img" src="${API}/api/image/${v.image_path}" alt="">`
                               : '<div class="sc-thumb-placeholder">📦</div>';
    return `
      <div class="switch-card" onclick="applyFind(${idx},${ri})" data-hsrc="f" data-hpos="${ri}" style="animation-delay:${ri * 35}ms">
        <div class="sc-thumb">${thumb}</div>
        <div class="sc-body">
          <div class="sc-name">${escHtml(v.product || '')}</div>
          <div class="sc-price">₹${price.toLocaleString('en-IN', {maximumFractionDigits: 2})}</div>
          <div class="sc-file">📁 ${escHtml((v.file_name || '').substring(0, 28))}</div>
        </div>
      </div>`;
  }).join('');
}

function findProduct(idx) {
  document.querySelectorAll('.switch-panel').forEach(p => p.remove());
  const item = currentQuotation && currentQuotation.items[idx];
  const row = document.querySelector(`#items-body tr[data-idx="${idx}"]`);
  if (!item || !row) return;
  const panel = document.createElement('tr');
  panel.className = 'switch-panel';
  panel.innerHTML = `<td colspan="${row.cells.length}">
    <div class="switch-panel-inner find-panel">
      <div class="find-head">
        <span class="find-ico"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><line x1="16.4" y1="16.4" x2="21" y2="21"/></svg></span>
        <div class="find-txt">
          <div class="find-title">Find in catalogue</div>
          <div class="find-sub">Replacing: ${escHtml(item.product || '')}</div>
        </div>
        <button class="find-close" title="Close" onclick="document.querySelectorAll('.switch-panel').forEach(p=>p.remove())">✕</button>
      </div>
      <div class="find-search">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><line x1="16.4" y1="16.4" x2="21" y2="21"/></svg>
        <input type="text" id="find-input-${idx}" placeholder="Search by product, brand or model no…"
          oninput="findProductSearch(${idx})" onkeydown="stopEnterSubmit(event)">
      </div>
      <div class="switch-cards" id="find-results-${idx}">${_findCards(idx, item._suggestions || [], true)}</div>
      <label class="remember-choice" title="Off = this quote only. On = the matcher learns this phrase for good.">
        <input type="checkbox" id="remember-${idx}">
        Remember this choice for "<b>${escHtml(item.product || '')}</b>" in future quotations
      </label>
    </div>
  </td>`;
  row.insertAdjacentElement('afterend', panel);
  document.getElementById(`find-input-${idx}`).focus();

  // A reopened quote has no _suggestions — underscore keys are stripped
  // before the quotation is saved — so fetch them rather than leaving the
  // panel empty on every quote that wasn't generated seconds ago.
  if (!(item._suggestions || []).length) {
    const term = (item.requested || item.product || '').trim();
    if (term.length >= 2) {
      fetch(`${API}/api/suggest-products?q=${encodeURIComponent(term)}`)
        .then(r => r.json())
        .then(d => {
          const box = document.getElementById(`find-results-${idx}`);
          const inp = document.getElementById(`find-input-${idx}`);
          // don't stomp on results the user has already typed for
          if (!box || (inp && inp.value.trim())) return;
          item._suggestions = d.items || [];
          box.innerHTML = _findCards(idx, item._suggestions, true);
        })
        .catch(() => {});
    }
  }
}

function findProductSearch(idx) {
  clearTimeout(_findTimer);
  _findTimer = setTimeout(async () => {
    const inp = document.getElementById(`find-input-${idx}`);
    const box = document.getElementById(`find-results-${idx}`);
    if (!inp || !box) return;
    const term = inp.value.trim();
    if (term.length < 2) {
      // back to the suggestions we opened with, rather than an empty box
      const it = currentQuotation && currentQuotation.items[idx];
      box.innerHTML = _findCards(idx, (it && it._suggestions) || [], true);
      return;
    }
    const r = await fetch(`${API}/api/master-table/page?q=${encodeURIComponent(term)}&limit=12`);
    const d = await r.json();
    const cur = document.getElementById(`find-input-${idx}`);
    if (!cur || cur.value.trim() !== term) return;   // superseded by a newer keystroke
    if (!(d.items || []).length) {
      _findResults = [];
      box.innerHTML = `<p class="find-empty">No match for “${escHtml(term)}” — try fewer or different words.</p>`;
      return;
    }
    box.innerHTML = _findCards(idx, d.items, false);
  }, 300);
}

function applyFind(idx, ri) {
  const item = currentQuotation && currentQuotation.items[idx];
  const v = _findResults[ri];
  if (!item || !v) return;
  const tier = (item.tiers && item.tiers[0]) || '3star';
  item.product        = v.product || '';
  item.brand          = v.brand || '';
  item.model_no       = v.original_model || '';
  item.specification  = v.specification || '';
  item.description    = v.specification || '';
  item.hsn_code       = v.hsn_code || '';
  item.image_path     = v.image_path || '';
  item.gst_pct        = v.gst_pct != null ? v.gst_pct : 18;
  item.cost           = v.cost || 0;
  item.price_3star    = v.price_3star || 0;
  item.price_4star    = v.price_4star || 0;
  item.price_per_pc   = (tier === '4star' ? v.price_4star : v.price_3star) || v.price_3star || v.price_4star || 0;
  item.price_currency = 'INR';
  item.remember       = !!document.getElementById(`remember-${idx}`)?.checked;
  item.matched_by     = 'manual';
  item.not_in_catalog = false;
  item.was_placeholder = true;   // persisted — enables Revert after reload too
  document.querySelectorAll('.switch-panel').forEach(p => p.remove());
  renderResult(currentQuotation);
  saveEdits(true);
}

// Undo a Find pick: back to the client's original wording as a blank
// placeholder row (qty kept, price cleared).
function revertFind(idx) {
  const item = currentQuotation && currentQuotation.items[idx];
  if (!item || !item.was_placeholder) return;
  Object.assign(item, {
    product: item.requested || item.product,
    model_no: '', brand: '', specification: '', description: '',
    hsn_code: '', image_path: '', local_image: '',
    price_per_pc: 0, cost: 0, gst_pct: 18,
    price_3star: 0, price_3star_usd: 0, price_4star: 0, price_4star_usd: 0,
    matched_by: 'not_found', not_in_catalog: true,
  });
  delete item.was_placeholder;
  renderResult(currentQuotation);
  saveEdits(true);
}

// ── Switch Variant ───────────────────────────────────────────────────────────
function switchVariant(idx) {
  // Close any open panel first
  document.querySelectorAll('.switch-panel').forEach(p => p.remove());

  const item = currentQuotation && currentQuotation.items[idx];
  if (!item || !item._variants || item._variants.length <= 1) return;

  const row = document.querySelector(`#items-body tr[data-idx="${idx}"]`);
  if (!row) return;
  const colCount = row.cells.length;

  // Cheapest first. Sorted for DISPLAY only — a copy carrying each variant's
  // original index, because applySwitch() indexes into item._variants and the
  // matcher's own pick (_variants[0]) must stay the matched product. Sorting
  // the underlying array would silently re-point every quote at the cheapest
  // unrelated row. Unpriced variants (0) sink to the bottom: "no price" isn't
  // cheap.
  const ordered = item._variants
    .map((v, i) => ({ v, i }))
    .sort((a, b) => ((a.v.price || Infinity) - (b.v.price || Infinity)));

  const cards = ordered.map(({ v, i: vi }, pos) => {
    const isCur    = v.product === item.product && String(v.price) === String(item.price_per_pc);
    const priceInr = v.price||0;   // USD column removed — show as-is in INR
    const priceStr = `₹${priceInr.toLocaleString('en-IN', {maximumFractionDigits:2})}`;
    const fileShort = (v.file_name||'').replace(/QUOTATION FOR /i,'').substring(0,28);
    const thumbSrc = v.image_path ? `${API}/api/image/${v.image_path}` : '';
    const thumb = thumbSrc
      ? `<img class="sc-thumb-img" src="${thumbSrc}" alt="">`
      : `<div class="sc-thumb-placeholder">📦</div>`;
    const thumbClick = thumbSrc
      ? `onclick="event.stopPropagation(); showImageLightbox('${thumbSrc}')" title="Click to enlarge"`
      : '';
    return `
      <div class="switch-card ${isCur?'active':''}" onclick="applySwitch(${idx},${vi},this)" data-hsrc="v" data-hidx="${idx}" data-hpos="${vi}" style="animation-delay:${pos * 35}ms">
        <div class="sc-thumb" ${thumbClick}>${thumb}</div>
        <div class="sc-body">
          <div class="sc-name">${v.product||''}</div>
          <div class="sc-price">${priceStr}</div>
          <div class="sc-file">📁 ${fileShort}</div>
          ${isCur ? '<div class="sc-tick">✓ Currently selected</div>' : ''}
        </div>
      </div>`;
  }).join('');

  const panel = document.createElement('tr');
  panel.className = 'switch-panel';
  panel.innerHTML = `<td colspan="${colCount}">
    <div class="switch-panel-inner">
      <div class="switch-panel-title">🔄 Switch Variant — "${item._requested || item.product}"</div>
      <div class="switch-cards">${cards}</div>
      ${item._variantsFull ? `<div class="switch-more-note">Showing all ${ordered.length} matches.</div>` : `
        <div class="switch-more">
          <button class="btn btn-sm btn-outline" onclick="loadMoreVariants(${idx}, this)">
            Show ${VARIANT_BATCH} more
          </button>
          <span class="switch-more-note">Showing ${ordered.length}${
            item._variantsTotal ? ` of ${item._variantsTotal}` : ' best matches'}.</span>
        </div>`}
      <label class="remember-choice" title="Off = this quote only. On = the matcher learns this phrase for good.">
        <input type="checkbox" id="remember-${idx}">
        Remember this choice for "<b>${escHtml(item._requested || item.product || '')}</b>" in future quotations
      </label>
      <button onclick="document.querySelectorAll('.switch-panel').forEach(p=>p.remove())"
        style="margin-top:10px;background:none;border:none;cursor:pointer;font-size:var(--fs-sm);color:var(--muted);">
        ✕ Close
      </button>
    </div>
  </td>`;
  row.insertAdjacentElement('afterend', panel);
}

const VARIANT_BATCH = 30;   // how many more the Switch panel loads per click

// Loads the next batch of alternatives and reopens the panel. Fetched
// variants are APPENDED, never substituted — applySwitch() indexes into
// _variants, so the matcher's own pick has to keep index 0 and every card
// already on screen has to keep the index it was rendered with.
async function loadMoreVariants(idx, btn) {
  const item = currentQuotation && currentQuotation.items[idx];
  if (!item) return;
  const term = item._requested || item.product || '';
  const next = (item._variants.length || 0) + VARIANT_BATCH;
  btn.disabled = true; btn.textContent = 'Loading…';
  try {
    const res = await fetch(`${API}/api/product-variants?q=${encodeURIComponent(term)}&limit=${next}`);
    const d = await res.json();
    if (!res.ok) { toast(apiErr(d), 'error'); return; }
    const key = v => `${(v.product || '').trim().toLowerCase()}|${(v.model_no || '').trim().toLowerCase()}`;
    const have = new Set(item._variants.map(key));
    const added = (d.items || []).filter(v => !have.has(key(v)));
    item._variants = item._variants.concat(added);
    item._variantsTotal = d.total || item._variants.length;
    // Nothing new, or the catalogue is exhausted — stop offering the button
    // rather than letting it sit there returning zero every time.
    if (!added.length || item._variants.length >= item._variantsTotal) item._variantsFull = true;
    switchVariant(idx);
    toast(added.length ? `${added.length} more option(s)` : 'No further matches');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    if (btn.isConnected) { btn.disabled = false; btn.textContent = `Show ${VARIANT_BATCH} more`; }
  }
}

function applySwitch(idx, vi, cardEl) {
  const item = currentQuotation && currentQuotation.items[idx];
  if (!item || !item._variants) return;
  const v = item._variants[vi];
  if (!v) return;
  // Opt-in teaching: unticked (the default) means this quote only, so a
  // client's one-time preference can't re-point the mapping for everyone.
  item.remember = !!document.getElementById(`remember-${idx}`)?.checked;
  // Apply selected variant to item
  item.product       = v.product      || item.product;
  item.price_per_pc  = v.price        || 0;
  item.price_currency= v.price_currency || 'INR';
  item.brand         = v.brand        || '';
  item.model_no      = v.model_no     || '';
  item.description   = v.description  || '';
  item.specification = v.specification|| '';
  item.hsn_code      = v.hsn_code     || '';
  item.image_path     = v.image_path  || '';
  item.local_image   = '';            // use the variant's catalog image
  item.price_3star     = v.price_3star     || 0;
  item.price_3star_usd = v.price_3star_usd || 0;
  item.price_4star     = v.price_4star     || 0;
  item.price_4star_usd = v.price_4star_usd || 0;

  const finish = () => {
    document.querySelectorAll('.switch-panel').forEach(p => p.remove());
    renderResult(currentQuotation);
  };
  if (cardEl) {
    cardEl.classList.add('picked');
    setTimeout(finish, 220);   // let the pick animation play before the panel closes
  } else {
    finish();
  }
}

function amtWords(n) {
  const a=['','One','Two','Three','Four','Five','Six','Seven','Eight','Nine','Ten','Eleven','Twelve','Thirteen','Fourteen','Fifteen','Sixteen','Seventeen','Eighteen','Nineteen'];
  const b=['','','Twenty','Thirty','Forty','Fifty','Sixty','Seventy','Eighty','Ninety'];
  function w(n){if(n===0)return '';if(n<20)return a[n];if(n<100)return b[Math.floor(n/10)]+(n%10?' '+a[n%10]:'');if(n<1e3)return a[Math.floor(n/100)]+' Hundred'+(n%100?' '+w(n%100):'');if(n<1e5)return w(Math.floor(n/1e3))+' Thousand'+(n%1e3?' '+w(n%1e3):'');if(n<1e7)return w(Math.floor(n/1e5))+' Lakh'+(n%1e5?' '+w(n%1e5):'');return w(Math.floor(n/1e7))+' Crore'+(n%1e7?' '+w(n%1e7):'');}
  return (w(Math.round(n))||'Zero')+' Rupees Only';
}

function renderResult(q) {
  document.getElementById('result-empty').style.display = 'none';
  document.getElementById('result-content').style.display = 'block';

  // Fill document header
  const refNo = q.ref_no || '—';
  // Star columns follow what was asked at generation time: none selected →
  // both boxes off → the table shows only the plain PRICE column.
  const tq = Array.isArray(q.tiers) ? q.tiers : null;
  const c3 = document.getElementById('show-3star-col'), c4 = document.getElementById('show-4star-col');
  if (tq && c3 && c4 && !q._tiersApplied) {
    c3.checked = tq.includes('3star'); c4.checked = tq.includes('4star');
    q._tiersApplied = true;   // user's later checkbox clicks win over this default
  }
  document.getElementById('stat-ref').textContent  = '📄 ' + refNo;
  document.getElementById('doc-ref').textContent   = refNo;
  const bt = document.getElementById('doc-billto');
  if (bt) bt.value = q.bill_to
    || (q.client_name ? `${q.client_name}\nAttn: Purchase Manager` : '');
  initSalesPersonPicker(q);

  // Not-found banner
  const nfBanner = document.getElementById('not-found-banner');
  const nfList   = q.not_found || [];
  if (nfList.length && nfBanner) {
    nfBanner.style.display = 'block';
    document.getElementById('not-found-items').textContent = nfList.join(', ');
  } else if (nfBanner) {
    nfBanner.style.display = 'none';
  }

  const items = q.items || [];

  // Tier columns are only ever rendered when their checkbox is checked — no
  // display:none hiding. A hidden-but-still-in-the-DOM column can leave the
  // browser's table column model out of sync with the footer's colspan math
  // (a real gap was measured this way once already), so the column simply
  // doesn't exist in the markup when it's toggled off.
  const show3 = document.getElementById('show-3star-col') ? document.getElementById('show-3star-col').checked : true;
  const show4 = document.getElementById('show-4star-col') ? document.getElementById('show-4star-col').checked : true;
  const showOrig = document.getElementById('show-orig-col') ? document.getElementById('show-orig-col').checked : false;

  // BOQ Price / Profit columns only appear for a quotation generated from an
  // uploaded client BOQ that actually had its own pricing — a manually typed
  // quote has nothing to compare our Master Table price against.
  const hasBoqPricing = !!q.has_boq_pricing;
  // Cost / Profit / Margin appear ONLY when the user asked for margin, i.e.
  // came in through the Margin analysis button. Generating a quotation —
  // typed or from a file — is about what we stock and what the client pays;
  // putting our purchase cost on that screen answers a question nobody asked
  // and is the wrong thing to have open in front of a client.
  // Two conditions, both needed: the intent (the admin-only "Cost & margin"
  // checkbox, on by default; falls back to show_margin for the Margin
  // Analysis flow) and the data (_strip_cost deletes cost from an employee's
  // payload entirely, so its presence is the permission check, not a hidden
  // column).
  const smChk = document.getElementById('show-margin-col');
  const wantMargin = (currentUser && currentUser.role === 'admin' && smChk)
    ? smChk.checked : !!q.show_margin;
  const showMargin = wantMargin && items.some(i => (i.cost || 0) > 0);

  // Single unified header — all in INR
  const thead = document.querySelector('#items-table thead tr');
  thead.innerHTML = `<th style="width:40px">SL</th><th class="c" style="width:96px">Image</th><th>Product</th><th class="c" style="width:56px">QTY</th>
    <th>Model No</th><th>Brand</th><th>Specification</th>
    <th>HSN</th>${show3 ? '<th class="col-3star c" style="width:110px">⭐ 3★ Price</th>' : ''}${show4 ? '<th class="col-4star c" style="width:110px">⭐⭐ 4★ Price</th>' : ''}
    <th class="num" style="width:90px">Price/Pc (₹)</th>${hasBoqPricing ? '<th class="num" style="width:100px">BOQ Price (₹)</th>' : ''}${showMargin ? '<th class="num" style="width:90px">Cost (₹)</th>' : ''}${(hasBoqPricing || showMargin) ? '<th class="num" style="width:90px">Profit (₹)</th>' : ''}${showMargin ? '<th class="num" style="width:78px">Margin %</th>' : ''}<th class="num" style="width:100px">Amount (₹)</th><th class="c" style="width:56px">GST%</th><th class="num" style="width:100px">GST Val (₹)</th><th style="width:40px"></th>`;

  // Cost/Profit are deliberately NOT shown on a typed quotation — that view is
  // for checking what we stock, not for margin analysis. They appear only on a
  // BOQ-sourced quote, where BOQ Price gives something real to compare against.
  const colSpan = 13 + (show3?1:0) + (show4?1:0)
                    + (hasBoqPricing?1:0) + (showMargin?2:0)
                    + ((hasBoqPricing || showMargin)?1:0);
  let html = '';

  items.forEach(item => { html += renderItemRow(item, items.indexOf(item), show3, show4, hasBoqPricing, showOrig, showMargin); });

  html += `<tr class="manual-add-row"><td colspan="${colSpan}" style="padding:10px 14px;">
    <button type="button" class="btn-manual-add" id="manual-add-btn" onclick="toggleManualAdd()">✎ + Enter a product manually</button>
    <div id="manual-add-form" style="display:none;"></div>
  </td></tr>`;

  // Totals
  const subTotal = items.reduce((s,i) => s+(i._amtInr||0), 0);
  const gstTotal = items.reduce((s,i) => s+(i._gstVal||0), 0);
  const profitTotal = items.reduce((s,i) => s+(i._profit||0), 0);
  const grandTotal = subTotal + gstTotal;
  const tdR = `style="text-align:right;padding-right:16px;font-weight:700;"`;

  // Trailing (non-label) footer cells: Amount, GST%, GST Val, delete — plus
  // Profit only when BOQ pricing is present. Label colspan is whatever's left,
  // so the two rows always sum to the table's real column count regardless of
  // which optional columns are showing.
  const costTotal = items.reduce((s,i) => s + (i._cost||0) * (i.qty||0), 0);
  const marginTotal = subTotal > 0 ? (subTotal - costTotal) * 100 / subTotal : null;
  const trailingCols = 4 + (hasBoqPricing ? 1 : 0) + (showMargin ? 2 : 0)
                         + ((hasBoqPricing || showMargin) ? 1 : 0);
  const labelColspan = colSpan - trailingCols;

  html += `<tr class="quot-subrow" style="background:#eaf4fb;font-weight:700;">
    <td colspan="${labelColspan}" ${tdR}>SUB TOTAL</td>
    ${hasBoqPricing ? '<td></td>' : ''}
    ${showMargin ? `<td class="num" id="foot-cost">₹${fmt(costTotal)}</td>` : ''}
    ${(hasBoqPricing || showMargin) ? `<td class="num" id="foot-profit">₹${fmt(profitTotal)}</td>` : ''}
    ${showMargin ? `<td class="num" id="foot-margin">${marginTotal == null ? '—' : marginTotal.toFixed(1) + '%'}</td>` : ''}
    <td class="num" id="foot-sub">₹${fmt(subTotal)}</td><td></td><td class="num" id="foot-gst">₹${fmt(gstTotal)}</td><td></td>
  </tr>`;
  html += `<tr class="quot-grandrow" style="background:#1a3a6b;color:#fff;font-weight:700;">
    <td colspan="${labelColspan}" ${tdR} style="text-align:right;padding-right:16px;font-weight:700;color:#fff;">GRAND TOTAL (incl. GST)</td>
    <td colspan="${trailingCols}" class="num" id="foot-grand" style="font-size:var(--fs-md);color:#fff;">₹${fmt(grandTotal)}</td>
  </tr>`;

  document.getElementById('items-body').innerHTML = html;
  document.getElementById('items-foot').innerHTML = '';
  initRowDrag();

  // Update grand total (stat-total element removed — table's grand row is the
  // single source now; guard kept in case a cached page still has it)
  const allInrGrand = items.reduce((s,i) => s+(i._amtInr||0)+(i._gstVal||0), 0);
  const stEl = document.getElementById('stat-total');
  if (stEl) stEl.textContent = '₹ ' + fmt(allInrGrand);
  const wEl = document.getElementById('doc-words');
  if (wEl) wEl.textContent = amtWords(allInrGrand);

  selectedRating = null;
  document.getElementById('btn-good').classList.remove('selected');
  document.getElementById('btn-bad').classList.remove('selected');
  document.getElementById('feedback-details').style.display = 'none';
  document.getElementById('feedback-status').innerHTML = '';
}

// Pull the current master-table 3star/4star prices into an already-saved
// quote — the line items only snapshot prices at generation time, so a
// price change in the Master Catalogue afterward never shows up here on
// its own.
async function refreshQuotePrices() {
  if (!currentQuotation || !currentQuotation.id) return;
  const btn = document.getElementById('refresh-prices-btn');
  if (btn) { btn.disabled = true; btn.classList.add('spinning'); }
  try {
    const res = await fetch(`${API}/api/quotations/${currentQuotation.id}/refresh-prices`, { method: 'POST' });
    const d = await res.json();
    if (!res.ok) { toast(apiErr(d), 'error'); return; }
    currentQuotation.items = d.items;
    document.getElementById('show-orig-col').checked = d.updated > 0;
    renderResult(currentQuotation);
    const msg = d.updated
      ? `${d.updated} price(s) updated from the master table${d.skipped ? `, ${d.skipped} skipped (not found in master)` : ''}.`
      : 'Already up to date — no price changes found in the master table.';
    toast(msg, 'success');
  } catch (e) {
    toast('Refresh failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('spinning'); }
  }
}

function toggleTierColumn() {
  if (!currentQuotation) return;
  renderResult(currentQuotation);
}

function setRowTier(idx, tier) {
  if (!currentQuotation || !tier) return;
  const item = currentQuotation.items[idx];
  if (!item) return;
  if (tier === 'orig') {
    // A one-off action, not a persistent tier — applies the Original price
    // to this line without changing which tier tick stays checked.
    const orig3 = item.prev_price_3star != null ? item.prev_price_3star : item.orig_price_3star;
    const orig4 = item.prev_price_4star != null ? item.prev_price_4star : item.orig_price_4star;
    const activeTier = item.active_tier || (item.tiers && item.tiers[0]) || '3star';
    const orig = activeTier === '4star' ? (orig4 ?? orig3) : (orig3 ?? orig4);
    if (orig != null) item.price_per_pc = orig;
    item.price_currency = 'INR';
  } else {
    item.active_tier = tier;
    item.price_per_pc = tier === '3star' ? (item.price_3star||0) : (item.price_4star||0);
    item.price_currency = 'INR';
  }
  renderResult(currentQuotation);
}

function recalcRow(idx) {
  if (!currentQuotation) return;
  const item = currentQuotation.items[idx];
  if (!item) return;
  const row = document.querySelector('#items-body tr[data-idx="' + idx + '"]');
  if (!row) return;
  const isINR  = (item.price_currency||'INR') === 'INR';
  const qty    = parseFloat(row.querySelector('.qty-input').value) || 0;
  const priceInr = parseFloat(row.querySelector('.price-input').value) || 0;
  const gst    = parseFloat(row.querySelector('.gst-input').value) || 0;
  const amtInr = qty * priceInr;
  const gstVal = amtInr * gst / 100;
  // Must mirror renderItemRow's rule, or editing a row would flip it back to
  // the BOQ formula and show a negative margin on a typed quote.
  const boqPrice = item.boq_price || 0;
  const cost     = item.cost || 0;
  const hasBoqPricing = !!(currentQuotation && currentQuotation.has_boq_pricing);
  const profit   = hasBoqPricing ? qty * (boqPrice - priceInr)
                                 : qty * (priceInr - cost);

  row.querySelector('.amount-cell').textContent  = '₹' + fmt(amtInr);
  row.querySelector('.gst-val-cell').textContent = '₹' + fmt(gstVal);
  const marginCell = row.querySelector('.margin-cell');
  if (marginCell) {
    const m = (cost > 0 && priceInr > 0) ? (priceInr - cost) * 100 / priceInr : null;
    marginCell.textContent = m == null ? '—' : m.toFixed(1) + '%';
  }
  const profitCell = row.querySelector('.profit-cell');
  if (profitCell) {
    profitCell.textContent = '₹' + fmt(profit);
    profitCell.style.color = profit >= 0 ? '#1e9e56' : '#d64545';
  }

  item.qty = qty; item.price_per_pc = isINR ? priceInr : priceInr / usdToInr;
  item.gst_pct = gst; item._amtInr = amtInr; item._gstVal = gstVal; item._profit = profit;
  updateGrandTotal();
}

function renderFoot(items) {}

function updateGrandTotal() {
  if (!currentQuotation) return;
  const items = currentQuotation.items || [];
  const subTotal = items.reduce((s,i) => s+(i._amtInr||0), 0);
  const gstTotal = items.reduce((s,i) => s+(i._gstVal||0), 0);
  const profitTotal = items.reduce((s,i) => s+(i._profit||0), 0);
  const grand = subTotal + gstTotal;
  // update the in-document SUB TOTAL / GRAND TOTAL rows too (not just the footer)
  const fs = document.getElementById('foot-sub');   if (fs) fs.textContent = '₹' + fmt(subTotal);
  const fg = document.getElementById('foot-gst');   if (fg) fg.textContent = '₹' + fmt(gstTotal);
  const fp = document.getElementById('foot-profit'); if (fp) fp.textContent = '₹' + fmt(profitTotal);
  const fgr = document.getElementById('foot-grand'); if (fgr) fgr.textContent = '₹' + fmt(grand);
  const st = document.getElementById('stat-total'); if (st) st.textContent = '₹ ' + fmt(grand);
  const wEl = document.getElementById('doc-words');
  if (wEl) wEl.textContent = amtWords(grand);
}

// ── Image Lightbox (click image to enlarge) ──────────────────────────────────
function showImageLightbox(src) {
  let ov = document.getElementById('img-lightbox');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'img-lightbox';
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.8);display:flex;align-items:center;justify-content:center;z-index:9999;cursor:zoom-out;';
    ov.onclick = () => ov.style.display = 'none';
    ov.innerHTML = '<img style="max-width:90vw;max-height:90vh;width:auto;height:auto;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.5);background:#fff;">';
    document.body.appendChild(ov);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') ov.style.display = 'none'; });
  }
  ov.querySelector('img').src = src;
  ov.style.display = 'flex';
}

function handleRowImage(idx, input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    if (currentQuotation && currentQuotation.items[idx]) {
      currentQuotation.items[idx].local_image = e.target.result;
    }
    // Re-render just that row's image cell
    const row = document.querySelector(`#items-body tr[data-idx="${idx}"]`);
    if (row && row.cells[1]) {
      row.cells[1].innerHTML = `<img src="${e.target.result}" style="width:84px;height:66px;object-fit:contain;border-radius:4px;border:1px solid #ddd;display:block;margin:0 auto;cursor:zoom-in;" onclick="showImageLightbox('${e.target.result.replace(/'/g,"\\'")}')" title="Click to enlarge">
         <span onclick="reAddImage(${idx})" style="cursor:pointer;font-size:var(--fs-xs);color:var(--primary);display:block;text-align:center;margin-top:2px;">✎ change</span>`;
    }
  };
  reader.readAsDataURL(file);
}

// ── Manual line item (not in the catalogue, typed in from scratch) ─────────
let _manualImageData = '';

function toggleManualAdd() {
  const form = document.getElementById('manual-add-form');
  const btn = document.getElementById('manual-add-btn');
  const opening = form.style.display === 'none';
  if (opening) {
    _manualImageData = '';
    const isAdminUser = currentUser && currentUser.role === 'admin';
    const field = (id, label, ph, extra, required) =>
      `<div class="man-field"><label>${label}${required ? ' <span class="man-req">*</span>' : ''}</label>
         <input id="${id}" placeholder="${ph||''}" ${extra||''} onkeydown="stopEnterSubmit(event)"></div>`;
    form.innerHTML = `
      <div class="manual-add-card">
        <div class="manual-add-head">
          <span class="manual-add-ico">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg>
          </span>
          <span>Add Product Manually</span>
        </div>
        <div class="manual-add-row1">
          <div>
            <label class="man-field-label">Product Image</label>
            <label id="man-img-box" class="man-img-box">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
              <span class="man-img-label">Upload</span>
              <span class="man-img-hint">JPG, PNG (Max. 2MB)</span>
              <input type="file" accept="image/*" style="display:none" onchange="handleManualImage(this)">
            </label>
          </div>
          <div class="manual-add-top-fields">
            ${field('man-product', 'Product Name', 'e.g. Cup Dispenser', 'oninput="this.style.borderColor=\'\';manualAddError(\'\')"', true)}
            ${field('man-model', 'Model No', 'e.g. PCD01')}
            ${field('man-brand', 'Brand', 'e.g. KMW')}
            <div class="man-field man-spec-slot"><label>Specification</label>
              <textarea id="man-spec" placeholder="e.g. Cup Dispenser (Disposable Cup, Cone, Glass Dispenser 16in W/SS Flip Cap...)" style="min-height:52px;width:100%;"></textarea>
            </div>
          </div>
        </div>
        <div class="manual-add-row2">
          <div class="man-field"><label>Qty <span class="man-req">*</span></label>
            <input id="man-qty" type="number" value="1" onkeydown="stopEnterSubmit(event)"></div>
          ${field('man-price', 'Price/PC (₹)', 'e.g. 679', 'type="number" step="0.01"', true)}
          ${isAdminUser ? field('man-cost', 'Cost (₹)', 'e.g. 450', 'type="number" step="0.01"') : ''}
          <div class="man-field"><label>GST % <span class="man-req">*</span></label>
            <select id="man-gst">
              <option value="0">0</option><option value="5">5</option><option value="12">12</option>
              <option value="18" selected>18</option><option value="28">28</option>
            </select></div>
          ${field('man-hsn', 'HSN Code', 'e.g. 73239390')}
        </div>
        ${isAdminUser ? `<label class="man-master-check">
          <input type="checkbox" id="man-to-master" checked>
          <span>💾 Save to the <b>Master Catalogue</b> too — future quotes find it automatically</span>
        </label>` : ''}
        <div id="man-error" class="man-error" style="display:none;"></div>
        <div class="manual-add-actions">
          <button type="button" class="btn-manual-cancel" onclick="toggleManualAdd()">Cancel</button>
          <button type="button" class="btn-manual-submit" onclick="addManualItem()">Add Product</button>
        </div>
      </div>`;
  } else {
    form.innerHTML = '';
  }
  form.style.display = opening ? 'block' : 'none';
  btn.style.display = opening ? 'none' : 'inline-flex';
}

function handleManualImage(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    _manualImageData = e.target.result;
    const box = document.getElementById('man-img-box');
    if (box) box.innerHTML = `<img src="${e.target.result}" style="width:100%;height:100%;object-fit:cover;border-radius:8px;">`;
  };
  reader.readAsDataURL(file);
}

function manualAddError(msg) {
  const box = document.getElementById('man-error');
  if (box) { box.textContent = msg; box.style.display = msg ? 'block' : 'none'; }
}

function addManualItem() {
  manualAddError('');
  if (!currentQuotation) { manualAddError('No quotation is open — generate or open one first.'); return; }
  if (!Array.isArray(currentQuotation.items)) currentQuotation.items = [];
  const val = id => (document.getElementById(id)?.value || '').trim();
  const product = val('man-product');
  // Silent focus() alone read as "the button does nothing" — say why.
  if (!product) {
    manualAddError('Product Name is required.');
    const el = document.getElementById('man-product');
    if (el) { el.style.borderColor = 'var(--danger)'; el.focus(); }
    return;
  }
  const qty = parseInt(val('man-qty')) || 1;
  const price = parseFloat(val('man-price')) || 0;
  const cost = parseFloat(val('man-cost')) || 0;
  const gst = val('man-gst') === '' ? 18 : parseFloat(val('man-gst'));
  const toMaster = !!document.getElementById('man-to-master')?.checked;
  const imgData = _manualImageData;

  try {
  currentQuotation.items.push({
    sl_no: currentQuotation.items.length + 1,
    product, qty,
    description: val('man-spec'), specification: val('man-spec'),
    model_no: val('man-model'), brand: val('man-brand'), hsn_code: val('man-hsn'),
    price_per_pc: price, price_currency: 'INR', cost: cost, gst_pct: gst,
    image_path: '', local_image: _manualImageData,
    tiers: (currentQuotation.tiers && currentQuotation.tiers.length) ? currentQuotation.tiers : ['3star'],
    price_3star: 0, price_3star_usd: 0, price_4star: 0, price_4star_usd: 0,
    _variants: [], _requested: product, requested: product,
    matched_by: 'manual', not_in_catalog: false, boq_price: 0,
  });
  if (toMaster) {
    // Fire-and-report: the quote row is already added either way.
    fetch(`${API}/api/master-table/add-product`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ product, original_model: val('man-model'), brand: val('man-brand'),
        specification: val('man-spec'), hsn_code: val('man-hsn'), gst_pct: gst,
        price, cost, image_data: imgData })
    }).then(async r => {
      const d = await r.json().catch(() => ({}));
      toast(r.ok ? d.message : (d.detail || 'Master table add failed'), r.ok ? 'success' : 'error');
      if (r.ok) { masterFolders = {}; masterSummary = []; }
    }).catch(e => toast('Master table add failed: ' + e.message, 'error'));
  }
  _manualImageData = '';
  renderResult(currentQuotation);
  saveEdits(true);
  } catch (e) {
    // Never fail silently — a thrown error here looked identical to a dead button.
    manualAddError('Could not add the product: ' + e.message);
  }
}

function reAddImage(idx) {
  const inp = document.createElement('input');
  inp.type = 'file'; inp.accept = 'image/*';
  inp.onchange = () => handleRowImage(idx, inp);
  inp.click();
}

function removeRow(idx) {
  if (!currentQuotation) return;
  currentQuotation.items.splice(idx, 1);
  renderResult(currentQuotation);
}

// ── Sales person on the quote: pick → animated card → saved with the quote ──
let salesPersons = null;
function _spRegions(p) {
  return (p.region || '').split(',').map(r => r.trim().toUpperCase()).filter(Boolean);
}
function _fillPersonOptions(regionFilter) {
  const sel = document.getElementById('sales-person-sel');
  const list = (salesPersons || []).filter(p =>
    !regionFilter || _spRegions(p).includes(regionFilter));
  sel.innerHTML = '<option value="">Sales Team</option>' + list.map(p =>
    `<option value="${p.id}">${p.name}</option>`).join('');
  return list;
}
async function initSalesPersonPicker(q) {
  const sel = document.getElementById('sales-person-sel');
  const rsel = document.getElementById('sales-region-sel');
  if (!sel) return;
  if (!salesPersons) {
    // asList, not just catch: a lapsed session answers 200-shaped JSON
    // ({detail: "Not logged in"}), which parses fine and then dies on
    // .flatMap below — taking the whole quotation render down with it.
    try { salesPersons = asList(await (await fetch(`${API}/api/sales-persons`)).json()); }
    catch (e) { salesPersons = []; }
  }
  if (rsel) {
    const regions = [...new Set(salesPersons.flatMap(_spRegions))].sort();
    rsel.innerHTML = '<option value="">All regions</option>' + regions.map(r =>
      `<option value="${r}">${r}</option>`).join('');
  }
  const cur = (q && q.sales_person) || null;
  if (rsel) rsel.value = '';                 // fresh view starts unfiltered
  _fillPersonOptions('');
  sel.value = cur && cur.id ? String(cur.id) : '';
  renderSalesPersonCard(cur, false);
}
function filterByRegion(region) {
  const list = _fillPersonOptions(region);
  const sel = document.getElementById('sales-person-sel');
  if (region && list.length === 1) {
    // Only one person covers this region — pick them automatically.
    sel.value = String(list[0].id);
    setSalesPerson(sel.value);
  } else {
    // Two or more (or none): let the user choose between them.
    const cur = currentQuotation && currentQuotation.sales_person;
    sel.value = cur && list.some(p => p.id === cur.id) ? String(cur.id) : '';
    if (!sel.value && cur) { currentQuotation.sales_person = null; renderSalesPersonCard(null, false); setSalesPerson(''); }
  }
}
function renderSalesPersonCard(p, animate) {
  const card = document.getElementById('sp-card');
  if (!card) return;
  if (!p || !p.name) { card.style.display = 'none'; card.innerHTML = ''; return; }
  card.innerHTML = `
    <span class="sp-avatar">${p.name.trim()[0]}</span>
    <span class="sp-info"><b>${p.name}</b>
      <small>📞 ${p.phone || '—'} &nbsp; ✉ ${p.email || '—'}</small></span>
    ${p.region ? `<span class="sp-region">${p.region}</span>` : ''}`;
  card.style.display = 'flex';
  if (animate) { card.classList.remove('sp-pop'); void card.offsetWidth; card.classList.add('sp-pop'); }
}
async function setBillTo(text) {
  if (!currentQuotation) return;
  currentQuotation.bill_to = text;
  // First non-empty line doubles as the client name for lists/search.
  const first = (text.split('\n').find(l => l.trim()) || '').trim();
  if (first) currentQuotation.client_name = first;
  try {
    await fetch(`${API}/api/quotations/${currentQuotation.id}`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ items: currentQuotation.items,
        client_name: currentQuotation.client_name, bill_to: text })
    });
  } catch (e) { /* Save Edits carries it next time */ }
}

async function setSalesPerson(idStr) {
  if (!currentQuotation) return;
  const p = (salesPersons || []).find(x => String(x.id) === idStr) || null;
  currentQuotation.sales_person = p;
  renderSalesPersonCard(p, true);
  // Persist immediately — the export reads it server-side.
  try {
    await fetch(`${API}/api/quotations/${currentQuotation.id}`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ items: currentQuotation.items,
        client_name: currentQuotation.client_name, sales_person: p })
    });
  } catch (e) { /* next Save Edits will carry it */ }
}

async function saveEdits(silent) {
  if (!currentQuotation) return false;
  const res = await fetch(`${API}/api/quotations/${currentQuotation.id}`, {
    method: 'PUT',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({items: currentQuotation.items, client_name: currentQuotation.client_name})
  });
  if (!silent) { if (res.ok) toast('Changes saved', 'success'); else toast('Save failed', 'error'); }
  return res.ok;
}

async function downloadXLS() {
  if (!currentQuotation) return;
  // Save the current (edited) values first so the file matches the screen.
  await saveEdits(true);
  window.open(`${API}/api/download/${currentQuotation.id}`);
}

// ── Feedback ─────────────────────────────────────────────────────────────────
function setRating(val) {
  selectedRating = val;
  document.getElementById('btn-good').classList.toggle('selected', val === 'good');
  document.getElementById('btn-bad').classList.toggle('selected', val === 'not_good');
  document.getElementById('feedback-details').style.display = val === 'not_good' ? 'block' : 'none';
  if (val === 'good') submitFeedback();
}

async function submitFeedback() {
  if (!currentQuotation || !selectedRating) return;
  const missing = document.getElementById('feedback-missing').value;
  const res = await fetch(`${API}/api/feedback`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({quotation_id: currentQuotation.id, rating: selectedRating, missing_items: missing})
  });
  const data = await res.json();
  document.getElementById('feedback-status').innerHTML = `<div class="alert alert-${res.ok?'success':'error'}">${res.ok ? data.message : apiErr(data)}</div>`;
  if (selectedRating === 'good') loadRepository();
}

// ── Repository ────────────────────────────────────────────────────────────────
async function loadRepository() {
  let [approved, all] = (await Promise.all([
    fetch(`${API}/api/quotations?status=approved`).then(r=>r.json()),
    fetch(`${API}/api/quotations`).then(r=>r.json())
  ])).map(asList);
  let drafts = all.filter(q => q.status === 'draft');

  // Margin view: only quotes with client BOQ pricing — margin is a
  // comparison against what the client priced; typed quotes have nothing
  // to compare and only produce "cost unknown" noise.
  const marginView = !!window._marginOnly;
  if (marginView) {
    const boq = q => !!(q.items_json && q.items_json.has_boq_pricing);
    approved = approved.filter(boq);
    drafts = drafts.filter(boq);
  }
  const t = document.getElementById('repo-title'), s = document.getElementById('repo-sub'),
        dt = document.getElementById('drafts-title');
  if (t)  t.textContent  = marginView ? 'Margin Analysis — Approved' : 'Approved Quotations Repository';
  if (s)  s.textContent  = marginView ? 'Cost, profit and margin per BOQ-priced quotation.'
                                      : 'All approved quotations are stored here.';
  if (dt) dt.textContent = marginView ? 'BOQ-priced Drafts' : 'All Drafts';

  document.getElementById('repo-list').innerHTML = approved.length
    ? approved.map(q => repoCard(q, 'approved')).join('')
    : `<div class="empty-state"><span class="es-icon">${marginView ? '◔' : '✅'}</span><div class="es-title">${marginView ? 'No BOQ-priced approved quotes' : 'No approved quotations yet'}</div><div class="es-hint">${marginView ? 'Margin needs a client BOQ with prices — generate a quote from a priced BOQ file.' : 'Approved quotes will appear here.'}</div></div>`;

  document.getElementById('drafts-list').innerHTML = drafts.length
    ? drafts.map(q => repoCard(q, 'draft')).join('')
    : `<div class="empty-state"><span class="es-icon">📝</span><div class="es-title">${marginView ? 'No BOQ-priced drafts' : 'No drafts'}</div><div class="es-hint">${marginView ? 'Typed quotations don\'t appear here — they have no client prices to compare.' : 'Quotations you generate are saved here as drafts.'}</div></div>`;
}

function repoCard(q, status) {
  const d = q.items_json;
  const items = d.items || [];
  // USD column removed — every item treated as INR (raw price)
  const grand = items.reduce((s,i) => {
    const amt = (i.qty||0) * (i.price_per_pc||0);
    return s + amt + amt*((i.gst_pct||0)/100);
  }, 0);
  const isAdmin = currentUser && currentUser.role === 'admin';
  const fromBoq = !!d.has_boq_pricing;
  return `<div class="repo-item" id="repo-${q.id}">
    <div>
      <strong>${d.ref_no || q.ref_no}</strong> &nbsp; <span class="badge badge-${status}">${status}</span>
      <span class="src-badge ${fromBoq ? 'boq' : 'typed'}">${fromBoq ? 'from BOQ' : 'typed'}</span>
      <div class="meta">${d.client_name || q.client_name} &nbsp;·&nbsp; ${items.length} items &nbsp;·&nbsp; ₹${grand.toLocaleString('en-IN',{maximumFractionDigits:0})}</div>
      <div class="meta">${q.created_at ? new Date(q.created_at).toLocaleDateString('en-IN') : ''}</div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <button class="btn btn-sm btn-outline" onclick="viewQuote(${q.id})"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>View</button>
      <button class="btn btn-sm btn-accent" onclick="window.open(API+'/api/download/${q.id}')"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="3" x2="12" y2="15"/></svg>XLS</button>
      ${isAdmin && fromBoq ? `<button class="btn btn-sm btn-outline" onclick="toggleMargin(${q.id})"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>Margin</button>` : ''}
      ${status==='draft' ? `<button class="btn btn-sm btn-success" onclick="approveQuote(${q.id})">✓ Approve</button>` : ''}
      <button class="btn btn-sm btn-danger" onclick="deleteQuote(${q.id})" title="Delete" style="padding:6px 10px;">✕</button>
    </div>
  </div><div class="margin-panel" id="margin-${q.id}" style="display:none;"></div>`;
}

// ── Motive 2: what did we make on this quotation? (admin only) ───────────────
async function toggleMargin(qid) {
  const panel = document.getElementById(`margin-${qid}`);
  if (!panel) return;
  if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  panel.innerHTML = '<div class="loading-state">Comparing against the master table...</div>';
  try {
    const res = await fetch(`${API}/api/quotations/${qid}/margin`);
    const d = await res.json();
    if (!res.ok) { panel.innerHTML = `<div class="alert alert-error">${apiErr(d)}</div>`; return; }
    renderMarginPanel(panel, d);
  } catch (e) {
    panel.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
}

function renderMarginPanel(panel, d) {
  const fmtIN = n => (n == null ? '—' : '₹' + n.toLocaleString('en-IN', {maximumFractionDigits: 2}));
  const stateCell = l => l.state === 'ok'
    ? `<td class="num" style="color:${l.profit >= 0 ? '#2ecc71' : '#e74c3c'};font-weight:600;">${fmtIN(l.profit)}</td>
       <td class="num">${l.margin_pct}%</td>`
    : `<td colspan="2" class="margin-flag">${l.state === 'no_cost' ? 'cost unknown' : 'not in master table'}</td>`;

  const caveat = (d.counts.no_cost || d.counts.not_in_master)
    ? `<span class="mbp-hint">· ${d.counts.no_cost ? d.counts.no_cost + ' line(s) without a known cost' : ''}
       ${d.counts.not_in_master ? ' · ' + d.counts.not_in_master + ' no longer in master' : ''} — excluded from totals</span>`
    : '';

  panel.innerHTML = `
    <div class="margin-summary">
      <span><b>Revenue</b> ${fmtIN(d.revenue)}</span>
      <span><b>Profit</b> <span style="color:${d.profit >= 0 ? '#2ecc71' : '#e74c3c'}">${fmtIN(d.profit)}</span></span>
      <span><b>Margin</b> ${d.margin_pct == null ? '—' : d.margin_pct + '%'}</span>
      ${caveat}
    </div>
    <div class="table-wrap" style="max-height:320px;overflow:auto;">
      <table>
        <thead><tr><th>Product</th><th class="num">Qty</th><th class="num">Sold @</th>
                   <th class="num">Cost @</th><th class="num">Profit</th><th class="num">Margin</th></tr></thead>
        <tbody>${d.lines.map(l => `<tr>
          <td><strong>${l.product}</strong>${l.model_no ? ` <span class="meta">${l.model_no}</span>` : ''}</td>
          <td class="num">${l.qty}</td>
          <td class="num">${fmtIN(l.sold)}</td>
          <td class="num">${fmtIN(l.cost)}</td>
          ${stateCell(l)}
        </tr>`).join('')}</tbody>
      </table>
    </div>`;
}

async function deleteQuote(id) {
  if (!await appConfirm({ title: 'Delete this quotation',
    message: 'It disappears from the repository for everyone.',
    confirmLabel: '🗑 Delete' })) return;
  const res = await fetch(`${API}/api/quotations/${id}`, { method: 'DELETE' });
  if (res.ok) loadRepository();
  else toast('Delete failed', 'error');
}

async function clearAllQuotations() {
  if (!await appConfirm({ title: 'Delete ALL quotations',
    message: 'Every approved quotation and draft is removed for everyone.',
    confirmLabel: '🗑 Delete everything', footer: 'This action cannot be undone.' })) return;
  const res = await fetch(`${API}/api/quotations/clear-all`, { method: 'DELETE' });
  if (res.ok) {
    loadRepository();
    toast('All quotations cleared', 'success');
  } else {
    toast('Failed to clear', 'error');
  }
}

async function approveQuote(id) {
  await fetch(`${API}/api/approve/${id}`, {method:'POST'});
  loadRepository();
}

async function viewQuote(id) {
  const res = await fetch(`${API}/api/quotations`);
  const all = await res.json();
  const q = all.find(x => x.id === id);
  if (!q) return;
  currentQuotation = { ...q.items_json, id: q.id };
  renderResult(currentQuotation);
  show('result');
}

// Init
fetchUsdRate();
loadCatalog();
loadUploadedFiles();
loadCatalogSelector();