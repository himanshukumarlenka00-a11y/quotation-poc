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
  if (tab === 'master')    { loadMasterFiles(); loadMasterTable(); }
  if (tab === 'generate')  { document.getElementById('sec-generate').classList.remove('boq-only'); loadCatalogSelector(); }
  if (tab === 'repository') loadRepository();
  if (tab === 'audit')     loadAuditLog();
  if (tab === 'users')     loadUsers();
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
  loadRepository();
}

// Topbar search: jump to the master catalogue with the term applied
function topbarSearch(e) {
  if (e.key !== 'Enter') return;
  const term = e.target.value.trim();
  show('master');
  setTimeout(() => {
    const box = document.getElementById('master-search');
    if (box) {
      box.value = term;
      document.getElementById('master-search-box')?.classList.add('active');
      filterMasterTable();
    }
  }, 120);
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

function updateReqCount() {
  const ta = document.getElementById('req-prompt');
  const c = document.getElementById('req-count');
  if (ta && c) c.textContent = `${ta.value.length} / 1000`;
}

// Ctrl+K / Cmd+K jumps to the topbar search from anywhere
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    const s = document.getElementById('global-search');
    if (s) { e.preventDefault(); s.focus(); s.select(); }
  }
});

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
function setUserActive(id, active) {
  if (!active && !confirm('End this person\'s access now? Their account and history stay, and you can reactivate later.')) {
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
  if (!confirm(`Remove '${filename}' from catalog?`)) return;
  const res = await fetch(`${API}/api/boq-files/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  const d = await res.json();
  alert(d.message);
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
        // Phase D gate: the import would land every product without a price.
        // Not an error to hide behind — show why, and let the admin either
        // teach the price column (re-scan) or knowingly force it through.
        results.push(`<div class="alert alert-error">⛔ '${file.name}': ${apiErr(data)}
          <div style="margin-top:8px;"><button class="btn btn-sm btn-danger"
            onclick="forceImportMaster(${i})">Import anyway (no prices)</button></div></div>`);
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
  loadMasterFiles();
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
    if (res.ok) { loadMasterFiles(); loadMasterTable(); }
  } catch (e) {
    status.innerHTML += `<div class="alert alert-error">${e.message}</div>`;
  }
}

async function loadMasterFiles() {
  const res = await fetch(`${API}/api/master-table/files`);
  const files = await res.json();
  const el = document.getElementById('master-files');
  if (!files.length) { el.innerHTML = '<div class="empty-state"><span class="es-icon">📁</span><div class="es-title">No files imported yet</div><div class="es-hint">Imported catalogues will be listed here.</div></div>'; return; }
  const isAdmin = currentUser && currentUser.role === 'admin';
  el.innerHTML = files.map(f => `
    <div class="repo-item">
      <div>
        <strong>📄 ${f.file_name}</strong>
        <div class="meta">${f.count} products &nbsp;·&nbsp; imported ${new Date(f.uploaded_at).toLocaleDateString('en-IN')}</div>
      </div>
      ${isAdmin ? `<button class="btn btn-sm btn-danger" onclick="deleteMasterFile('${f.file_name}')">🗑 Delete</button>` : ''}
    </div>`).join('');
}

async function deleteMasterFile(filename) {
  if (!confirm(`Remove '${filename}' from the master table?`)) return;
  const res = await fetch(`${API}/api/master-table/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  const d = await res.json();
  alert(d.message);
  loadMasterFiles();
  loadMasterTable();
}

let masterAllItems = [];

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

async function loadMasterTable() {
  const res = await fetch(`${API}/api/master-table`);
  masterAllItems = await res.json();
  document.getElementById('master-count').textContent = masterAllItems.length;
  // Respect a search term typed before the fetch resolved (topbar search races
  // this load) — rendering unfiltered here would clobber the filtered view.
  const term = document.getElementById('master-search')?.value.trim();
  if (term) filterMasterTable(); else renderMasterProducts(masterAllItems);
}

function filterMasterTable() {
  const term = document.getElementById('master-search').value.trim().toLowerCase();
  pulseMasterSearchBox();
  if (!term) { renderMasterProducts(masterAllItems); return; }
  const matches = masterAllItems.filter(i =>
    (i.product || '').toLowerCase().includes(term) ||
    (i.brand || '').toLowerCase().includes(term) ||
    (i.original_model || '').toLowerCase().includes(term));
  // Searching implies "show me the matches now" — auto-expand every catalog
  // that has a hit instead of making the user click each folder open.
  const matchedFiles = new Set(matches.map(i => i.file_name || 'Unknown'));
  renderMasterProducts(matches, matchedFiles, term);
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

function renderMasterProducts(items, forceExpanded, searchTerm) {
  const el = document.getElementById('master-table-view');
  if (!items.length) {
    el.innerHTML = `<p style="color:var(--muted);font-size:var(--fs-base);">${searchTerm ? 'No products match your search.' : 'No products yet.'}</p>`;
    return;
  }

  // A bulk-price apply or a per-product edit both refresh this whole view
  // by replacing its innerHTML — without remembering which catalog was
  // expanded, that folder silently snaps shut on every save, making the
  // just-applied prices seem to "vanish" until the user re-expands it.
  // Capture what's open now so the rebuild below can restore it exactly.
  // (Search results instead use forceExpanded, passed in by the caller.)
  const expandedFiles = forceExpanded || (() => {
    const set = new Set();
    document.querySelectorAll('#master-table-view > div').forEach(div => {
      const body = div.querySelector('.master-folder-body');
      const nameEl = div.querySelector('strong');
      if (body && nameEl && body.classList.contains('open')) {
        set.add(nameEl.textContent.trim());
      }
    });
    return set;
  })();

  const groups = {};
  items.forEach(i => {
    const key = i.file_name || 'Unknown';
    if (!groups[key]) groups[key] = [];
    groups[key].push(i);
  });

  const isAdminView = currentUser && currentUser.role === 'admin';

  el.innerHTML = Object.entries(groups).map(([fname, prods], gIdx) => {
    const headerCells = `<th class="num">3★ Price (₹)</th><th class="num">3★ Price ($)</th><th class="num">4★ Price (₹)</th><th class="num">4★ Price ($)</th>`;

    // Every catalog gets independent 3★/4★ fields now — a catalog that
    // started as single-price (e.g. a matched-BOQ import, where both tiers
    // were just the one real price mirrored) is no exception; admins can
    // now give it its own distinct 4★ price the same way KMW already has.
    // USD stays display-only — it's a separately-stored field, not derived,
    // so editing it isn't part of this.
    const priceCells = i => {
      if (!isAdminView) {
        return `<td class="num">₹${(i.price_3star||0).toLocaleString('en-IN')}</td>
                 <td class="num">$${(i.price_3star_usd||0).toFixed(2)}</td>
                 <td class="num">₹${(i.price_4star||0).toLocaleString('en-IN')}</td>
                 <td class="num">$${(i.price_4star_usd||0).toFixed(2)}</td>`;
      }
      return `<td><input id="mt-3star-${i.id}" type="number" step="0.01" class="mt-price-input" value="${i.price_3star||0}"
                 onchange="updateMasterPrice(${i.id}, this.value, document.getElementById('mt-4star-${i.id}').value)"
                 onkeydown="stopEnterSubmit(event)" style="width:90px"></td>
               <td>$${(i.price_3star_usd||0).toFixed(2)}</td>
               <td><input id="mt-4star-${i.id}" type="number" step="0.01" class="mt-price-input" value="${i.price_4star||0}"
                 onchange="updateMasterPrice(${i.id}, document.getElementById('mt-3star-${i.id}').value, this.value)"
                 onkeydown="stopEnterSubmit(event)" style="width:90px"></td>
               <td>$${(i.price_4star_usd||0).toFixed(2)}</td>`;
    };

    const fnameEsc = fname.replace(/'/g, "\\'");
    const bulkPricingBar = isAdminView ? `
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
        <button class="btn btn-sm btn-primary" id="bulk-apply-${gIdx}" onclick="applyBulkTierPricing(${gIdx}, '${fnameEsc}')">✨ Apply to all ${prods.length}</button>
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
      <div class="master-folder-head" onclick="toggleMasterFolder(${gIdx})">
        <div class="mf-title">
          <span class="mf-icon">📁</span>
          <strong>${fname}</strong>
          <span class="mf-count">${prods.length} products</span>
        </div>
        <span id="master-arrow-${gIdx}" class="master-arrow${wasExpanded ? ' open' : ''}">▶</span>
      </div>
      <div id="master-folder-${gIdx}" class="master-folder-body${wasExpanded ? ' open' : ''}">
        <div class="master-folder-content">
        ${bulkPricingBar}
        <div class="table-wrap">
          <table>
            <thead><tr><th>Image</th><th>Product</th><th>Brand</th><th>Model</th>${headerCells}<th>GST%</th><th>HSN</th></tr></thead>
            <tbody>${prods.map(i => `<tr>
              <td style="text-align:center;">${i.has_image
                ? `<img src="${API}/api/image/${i.image_path}" style="width:64px;height:52px;object-fit:contain;border-radius:4px;border:1px solid #ddd;cursor:zoom-in;" onclick="showImageLightbox('${API}/api/image/${i.image_path}')" title="Click to enlarge" onerror="this.style.display='none'">`
                : `<span style="color:#ccc;font-size:var(--fs-xs);">—</span>`}</td>
              <td><strong>${i.product}</strong></td>
              <td>${i.brand||'—'}</td>
              <td style="font-size:var(--fs-sm)">${i.original_model||'—'}</td>
              ${priceCells(i)}
              <td>${i.gst_pct||0}%</td>
              <td>${i.hsn_code||'—'}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function updateMasterPrice(id, price3, price4) {
  const p3 = parseFloat(price3), p4 = parseFloat(price4);
  if (isNaN(p3) || isNaN(p4) || p3 < 0 || p4 < 0) {
    alert('Enter a valid, non-negative price.');
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
      alert(d.detail || 'Update failed');
      loadMasterTable();
    }
  } catch (e) {
    alert('Update failed: ' + e.message);
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

function toggleMasterFolder(idx) {
  const content = document.getElementById(`master-folder-${idx}`);
  const arrow = document.getElementById(`master-arrow-${idx}`);
  const open = content.classList.contains('open');
  content.classList.toggle('open', !open);
  arrow.classList.toggle('open', !open);
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
  if (!isSelected) {
    el.style.setProperty('background', 'var(--accent-soft2, #efeafe)', 'important');
    el.style.setProperty('border', '1.5px solid var(--primary, #6a4cf0)', 'important');
  } else {
    el.style.removeProperty('background');
    el.style.removeProperty('border');
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

  if (!selectedItems.length) { alert('Please select at least one variant.'); return; }

  const btn = document.querySelector('#sec-variants .btn-primary');
  btn.disabled = true; btn.textContent = '⏳ Building...';

  try {
    const res = await fetch(`${API}/api/build-quotation`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ client_name: vpClient, items: selectedItems })
    });
    const q = await res.json();
    if (!res.ok) { alert(q.detail || 'Build failed'); return; }
    currentQuotation = q;
    renderResult(q);
    show('result');
  } catch(e) {
    alert('Error: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '📋 Build Quotation →';
  }
}

// ── Generate ─────────────────────────────────────────────────────────────────
async function generateQuote() {
  const prompt = document.getElementById('req-prompt').value.trim();
  const client = document.getElementById('client-name').value.trim();
  if (!prompt) { alert('Please enter customer requirements.'); return; }
  if (!selectedTiers.length) {
    alert('Please select a price tier (3 Star or 4 Star) first.');
    ['tier-3star', 'tier-4star'].forEach(id => {
      const el = document.getElementById(id);
      el.classList.remove('tier-shake'); void el.offsetWidth; el.classList.add('tier-shake');
    });
    return;
  }

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
      body: JSON.stringify({ prompt, client_name: client, catalogs: selectedCatalogs, tiers: selectedTiers })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Generation failed');
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
  boqReqDropZone.querySelector('p').innerHTML = `<strong>${file.name}</strong> selected`;
  document.getElementById('gen-boq-btn').disabled = false;
  document.getElementById('check-boq-btn').disabled = false;
  document.getElementById('gen-boq-status').innerHTML = '';
  document.getElementById('boq-coverage').innerHTML = '';
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
    if (!res.ok) throw new Error(apiErr(d, 'Coverage check failed'));
    boqMissingItems = d.missing || [];
    renderBoqCoverage(d);
  } catch (e) {
    out.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
  btn.disabled = false;
  btn.innerHTML = '🔍 Check what we stock';
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
          <table><thead><tr><th>Product</th><th>Model</th><th>Brand</th><th>Specification</th><th>Qty</th></tr></thead>
          <tbody>${d.missing.map(m => `<tr>
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
  if (!selectedTiers.length) {
    alert('Please select a price tier (3 Star or 4 Star) first.');
    ['tier-3star', 'tier-4star'].forEach(id => {
      const el = document.getElementById(id);
      el.classList.remove('tier-shake'); void el.offsetWidth; el.classList.add('tier-shake');
    });
    return;
  }
  const client = document.getElementById('client-name').value.trim();
  const btn = document.getElementById('gen-boq-btn');
  const status = document.getElementById('gen-boq-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="loader"></span> Generating...';
  status.innerHTML = '<div class="alert alert-info">🤖 Reading the file and matching against the Master Table...</div>';

  const fd = new FormData();
  fd.append('file', boqReqSelectedFile);
  fd.append('client_name', client);
  fd.append('tiers', selectedTiers.join(','));

  try {
    const res = await fetch(`${API}/api/smart-generate-from-boq`, { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Generation failed');
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
    btn.innerHTML = '📤 Generate from BOQ';
  }
}

// Add forgotten items to the CURRENT quote (from the result screen)
// ── "Frequently quoted together" suggestions ─────────────────────────────────
// Mined from past quotations — the history is the training set. Refreshed
// whenever the quote's items change, so adding a suggestion can surface the
// next one.
async function loadSuggestions() {
  const strip = document.getElementById('suggest-strip');
  if (!strip || !currentQuotation || !(currentQuotation.items || []).length) {
    if (strip) strip.style.display = 'none';
    return;
  }
  try {
    const res = await fetch(`${API}/api/suggestions`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ products: currentQuotation.items.map(i => i.product) })
    });
    if (!res.ok) { strip.style.display = 'none'; return; }
    const sugg = await res.json();
    if (!sugg.length) { strip.style.display = 'none'; return; }
    strip.innerHTML = `<span class="sugg-label">Frequently quoted together:</span>` +
      sugg.map(s => `
        <button class="sugg-chip" onclick="addSuggestion('${String(s.product).replace(/'/g, "\\'")}')"
                title="${s.brand || ''} ${s.model_no || ''} — quoted together ${s.times_together}×">
          ➕ ${s.product.length > 38 ? s.product.slice(0, 37) + '…' : s.product}
          <span class="sugg-count">${s.times_together}×</span>
        </button>`).join('');
    strip.style.display = 'flex';
  } catch (e) {
    strip.style.display = 'none';   // suggestions must never break the page
  }
}

function addSuggestion(product) {
  // Reuse the existing add flow end to end — same matching, same learning.
  const input = document.getElementById('add-item-input');
  input.value = product;
  addToQuote();
}

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

function renderItemRow(item, idx, show3, show4, hasBoqPricing) {
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

  item._priceInr = priceInr;
  item._amtInr   = amtInr;
  item._gstVal   = gstVal;
  item._cost     = cost;
  item._profit   = profit;

  const activeTier = item.active_tier || (item.tiers && item.tiers[0]) || '3star';

  const hasVariants = item._variants && item._variants.length > 1;
  const switchBtn   = hasVariants
    ? `<button class="btn-switch" onclick="switchVariant(${idx})" style="display:block;margin-top:4px;">🔄 Switch</button>`
    : '';

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

  return `<tr data-idx="${idx}">
    <td style="text-align:center;">${item.sl_no||idx+1}</td>
    <td style="width:96px;text-align:center;">${imgCell}</td>
    <td><strong>${item.product||''}</strong>${switchBtn}</td>
    <td><input type="number" class="qty-input" value="${qty}" onchange="recalcRow(${idx})" style="width:60px"></td>
    <td style="font-size:var(--fs-sm)">${item.model_no||''}</td>
    <td>${item.brand||''}</td>
    <td><div class="spec-text">${(item.specification||'').replace(/\\n/g,'\n')}</div></td>
    <td>${item.hsn_code||''}</td>
    ${show3 ? `<td class="col-3star">
      <div class="tier-price-cell">
        <span class="tier-tick ${activeTier==='3star'?'checked':''}" onclick="setRowTier(${idx},'3star')">${activeTier==='3star'?'✓':''}</span>
        <span class="tier-price-text"><span class="tier-price-inr">₹${(item.price_3star||0).toLocaleString('en-IN')}</span><span class="tier-price-usd">$${(item.price_3star_usd||0).toFixed(2)}</span></span>
      </div>
    </td>` : ''}
    ${show4 ? `<td class="col-4star">
      <div class="tier-price-cell">
        <span class="tier-tick ${activeTier==='4star'?'checked':''}" onclick="setRowTier(${idx},'4star')">${activeTier==='4star'?'✓':''}</span>
        <span class="tier-price-text"><span class="tier-price-inr">₹${(item.price_4star||0).toLocaleString('en-IN')}</span><span class="tier-price-usd">$${(item.price_4star_usd||0).toFixed(2)}</span></span>
      </div>
    </td>` : ''}
    <td><input type="number" step="0.01" class="price-input" value="${(Math.round(priceInr*100)/100)}" onchange="recalcRow(${idx})" style="width:100px"></td>
    ${hasBoqPricing ? `<td class="boq-price-cell num">₹${fmt(boqPrice, 2)}</td>
    <td class="profit-cell num" style="color:${profit >= 0 ? '#1e9e56' : '#d64545'};font-weight:600;">₹${fmt(profit)}</td>` : ''}
    <td class="amount-cell num">₹${fmt(amtInr)}</td>
    <td><input type="number" class="gst-input" value="${gst}" onchange="recalcRow(${idx})" style="width:50px"></td>
    <td class="gst-val-cell num">₹${fmt(gstVal)}</td>
    <td><button class="btn btn-sm btn-danger" onclick="removeRow(${idx})">✕</button></td>
  </tr>`;
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

  const cards = item._variants.map((v, vi) => {
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
      <div class="switch-card ${isCur?'active':''}" onclick="applySwitch(${idx},${vi},this)" style="animation-delay:${vi * 35}ms">
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
      <button onclick="document.querySelectorAll('.switch-panel').forEach(p=>p.remove())"
        style="margin-top:10px;background:none;border:none;cursor:pointer;font-size:var(--fs-sm);color:var(--muted);">
        ✕ Close
      </button>
    </div>
  </td>`;
  row.insertAdjacentElement('afterend', panel);
}

function applySwitch(idx, vi, cardEl) {
  const item = currentQuotation && currentQuotation.items[idx];
  if (!item || !item._variants) return;
  const v = item._variants[vi];
  if (!v) return;
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
  loadSuggestions();   // async, fire-and-forget — the strip fills in when ready

  // Fill document header
  const refNo = q.ref_no || '—';
  document.getElementById('stat-ref').textContent  = '📄 ' + refNo;
  document.getElementById('doc-ref').textContent   = refNo;
  document.getElementById('doc-client').textContent = q.client_name || '—';
  const now = new Date();
  const fd  = d => d.toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'});
  document.getElementById('doc-date').textContent  = fd(now);
  const vd = new Date(now); vd.setDate(vd.getDate()+30);
  document.getElementById('doc-valid').textContent = fd(vd);

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

  // BOQ Price / Profit columns only appear for a quotation generated from an
  // uploaded client BOQ that actually had its own pricing — a manually typed
  // quote has nothing to compare our Master Table price against.
  const hasBoqPricing = !!q.has_boq_pricing;

  // Single unified header — all in INR
  const thead = document.querySelector('#items-table thead tr');
  thead.innerHTML = `<th style="width:40px">SL</th><th style="width:96px">Image</th><th>Product</th><th style="width:56px">QTY</th>
    <th>Model No</th><th>Brand</th><th>Specification</th>
    <th>HSN</th>${show3 ? '<th class="col-3star" style="width:110px">⭐ 3★ Price</th>' : ''}${show4 ? '<th class="col-4star" style="width:110px">⭐⭐ 4★ Price</th>' : ''}
    <th class="num" style="width:90px">Price/Pc (₹)</th>${hasBoqPricing ? '<th class="num" style="width:100px">BOQ Price (₹)</th><th class="num" style="width:90px">Profit (₹)</th>' : ''}<th class="num" style="width:100px">Amount (₹)</th><th style="width:56px">GST%</th><th class="num" style="width:100px">GST Val (₹)</th><th style="width:40px"></th>`;

  // Cost/Profit are deliberately NOT shown on a typed quotation — that view is
  // for checking what we stock, not for margin analysis. They appear only on a
  // BOQ-sourced quote, where BOQ Price gives something real to compare against.
  const colSpan = 13 + (show3?1:0) + (show4?1:0) + (hasBoqPricing?2:0);
  let html = '';

  items.forEach(item => { html += renderItemRow(item, items.indexOf(item), show3, show4, hasBoqPricing); });

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
  const trailingCols = 4 + (hasBoqPricing ? 1 : 0);
  const labelColspan = colSpan - trailingCols;

  html += `<tr class="quot-subrow" style="background:#eaf4fb;font-weight:700;">
    <td colspan="${labelColspan}" ${tdR}>SUB TOTAL</td>
    ${hasBoqPricing ? `<td id="foot-profit">₹${fmt(profitTotal)}</td>` : ''}
    <td id="foot-sub">₹${fmt(subTotal)}</td><td></td><td id="foot-gst">₹${fmt(gstTotal)}</td><td></td>
  </tr>`;
  html += `<tr class="quot-grandrow" style="background:#1a3a6b;color:#fff;font-weight:700;">
    <td colspan="${labelColspan}" ${tdR} style="text-align:right;padding-right:16px;font-weight:700;color:#fff;">GRAND TOTAL (incl. GST)</td>
    <td colspan="${trailingCols}" id="foot-grand" style="font-size:var(--fs-md);color:#fff;">₹${fmt(grandTotal)}</td>
  </tr>`;

  document.getElementById('items-body').innerHTML = html;
  document.getElementById('items-foot').innerHTML = '';

  // Update grand total
  const allInrGrand = items.reduce((s,i) => s+(i._amtInr||0)+(i._gstVal||0), 0);
  document.getElementById('stat-total').textContent = '₹ ' + fmt(allInrGrand);
  const wEl = document.getElementById('doc-words');
  if (wEl) wEl.textContent = amtWords(allInrGrand);

  selectedRating = null;
  document.getElementById('btn-good').classList.remove('selected');
  document.getElementById('btn-bad').classList.remove('selected');
  document.getElementById('feedback-details').style.display = 'none';
  document.getElementById('feedback-status').innerHTML = '';
}

function toggleTierColumn() {
  if (!currentQuotation) return;
  renderResult(currentQuotation);
}

function setRowTier(idx, tier) {
  if (!currentQuotation) return;
  const item = currentQuotation.items[idx];
  if (!item) return;
  item.active_tier = tier;
  item.price_per_pc = tier === '3star' ? (item.price_3star||0) : (item.price_4star||0);
  item.price_currency = 'INR';
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
  document.getElementById('stat-total').textContent = '₹ ' + fmt(grand);
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

async function saveEdits(silent) {
  if (!currentQuotation) return false;
  const res = await fetch(`${API}/api/quotations/${currentQuotation.id}`, {
    method: 'PUT',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({items: currentQuotation.items, client_name: currentQuotation.client_name})
  });
  if (!silent) { if (res.ok) alert('Changes saved!'); else alert('Save failed.'); }
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
  const [approved, all] = await Promise.all([
    fetch(`${API}/api/quotations?status=approved`).then(r=>r.json()),
    fetch(`${API}/api/quotations`).then(r=>r.json())
  ]);
  const drafts = all.filter(q => q.status === 'draft');

  document.getElementById('repo-list').innerHTML = approved.length
    ? approved.map(q => repoCard(q, 'approved')).join('')
    : '<div class="empty-state"><span class="es-icon">✅</span><div class="es-title">No approved quotations yet</div><div class="es-hint">Approved quotes will appear here.</div></div>';

  document.getElementById('drafts-list').innerHTML = drafts.length
    ? drafts.map(q => repoCard(q, 'draft')).join('')
    : '<div class="empty-state"><span class="es-icon">📝</span><div class="es-title">No drafts</div><div class="es-hint">Quotations you generate are saved here as drafts.</div></div>';
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
  return `<div class="repo-item" id="repo-${q.id}">
    <div>
      <strong>${d.ref_no || q.ref_no}</strong> &nbsp; <span class="badge badge-${status}">${status}</span>
      <div class="meta">${d.client_name || q.client_name} &nbsp;·&nbsp; ${items.length} items &nbsp;·&nbsp; ₹${grand.toLocaleString('en-IN',{maximumFractionDigits:0})}</div>
      <div class="meta">${q.created_at ? new Date(q.created_at).toLocaleDateString('en-IN') : ''}</div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <button class="btn btn-sm btn-outline" onclick="viewQuote(${q.id})"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>View</button>
      <button class="btn btn-sm btn-accent" onclick="window.open(API+'/api/download/${q.id}')"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="3" x2="12" y2="15"/></svg>XLS</button>
      ${isAdmin ? `<button class="btn btn-sm btn-outline" onclick="toggleMargin(${q.id})"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>Margin</button>` : ''}
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
  if (!confirm('Delete this quotation?')) return;
  const res = await fetch(`${API}/api/quotations/${id}`, { method: 'DELETE' });
  if (res.ok) loadRepository();
  else alert('Delete failed.');
}

async function clearAllQuotations() {
  if (!confirm('Delete ALL quotations (approved + drafts)? This cannot be undone.')) return;
  const res = await fetch(`${API}/api/quotations/clear-all`, { method: 'DELETE' });
  if (res.ok) {
    loadRepository();
    alert('All quotations cleared.');
  } else {
    alert('Failed to clear.');
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