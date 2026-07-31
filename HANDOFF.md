# HANDOFF — quotation-poc session state (2026-07-31)

Read this first in a new session. Cross-session rules also live in
`C:\Users\itzan\.claude\projects\E--rtk-bin\memory\` (auto-loaded).

## What this is
FastAPI + vanilla JS quotation tool for Melange. NO React — three frontend
files: `static/index.html`, `static/js/app.js`, `static/css/main.css`.
Server: `run_local.ps1` (uvicorn, auto-reload on `app/` only). Port 8000.
Frontend changes need a cache-bust: bump `?v=NN` on both assets in index.html
(currently v24). Repo: github.com/ai-eng67/quotation-poc (PRIVATE — contains
the DB with costs), branch `restructure-auth-smart-import`, all pushed.

## The three core motives (all BUILT)
1. Search master table — master-only matching; employees never see cost
   (stripped server-side, incl. quotation payloads + _variants).
2. Master vs past quotations — 📊 Margin button per quote (admin): per-line
   cost/profit/margin, honest states ok|no_cost|not_in_master, unknowns
   EXCLUDED from totals with a visible caveat.
3. BOQ coverage — "Check what we stock" + coverage bar + admin add-missing.

## Built this session (all committed & pushed)
- Smart import A–F: column report at scan; learned mappings
  (`column_mappings`, teach once); suggestions+Teach dropdown; Phase D gate
  (all-priceless import → 409, force=1 audited); file-type detection
  (price_list|client_boq|quotation) on every upload path.
- Learning: `match_corrections` (phrase→product, TEXT identity so re-imports
  survive; corrections outrank confirmations, enforced in SQL); suggestions
  strip ("frequently quoted together", mined live from last 500 quotes,
  baskets>60 excluded).
- Perf: FTS5 candidate lookup (`master_fts`, rebuild via
  `rebuild_master_fts()` after row changes) — 0.3ms vs full scan; LLM
  shortlist (relevant candidates, 91–98% fewer tokens); list-shaped prompts
  parsed WITHOUT the LLM (`_parse_items_deterministically`); images sharded
  data/images/<xx>/<hash>.jpg with flat-path fallback read.
- Access: users page (roles, deactivate — kills live sessions via
  get_current_user is_active check; self/last-admin lockout guards);
  Activity page (audit log, deletions tinted); cost hidden from employees.
- Shell redesign: light theme default (tokens; dark = html[data-theme=dark]),
  sidebar (.snav[data-tab] → show(tab) → #sec-<tab>), live dashboard
  (GET /api/dashboard, role-aware payload), Smart Import card on dashboard
  (POST /api/detect-file → routes file into setMasterFiles/setBoqReqFile —
  NO second upload path), topbar pill search (reuses .pill-search-box).

## LLM setup
llama-3.3-70b-versatile on Groq, TWO call sites only (extraction + semantic
matching fallback). /api/generate and /api/variants were dead and DELETED.
Groq free tier: 12k TPM / 100k TPD — was exhausted once; fixed by shortlist.
Multiple models: deliberately deferred until measured need.

## Data state
Catalogues: KMW 743 (real margins avg ₹38.76), NILKAMAL 493, RENA 359,
OPM 705 ×2 (old quotation imported as catalogue — cost intentionally 0,
prices = what we quoted). DB `data/quotations.db`; backups in data/backups.
Test user: emplyoee@melangeindia.in / 12345678 (employee role; email typo'd).

## OPEN ITEMS (priority order)
1. UI logic audit — a 12-agent workflow was launched then STOPPED by user
   (runId wf_b6638d21-6c3; resumable via scriptPath+resumeFromRunId, cached).
   Goal: audit every button/nav/flow mapping for logic, fix confirmed issues.
2. GST = 0 on both OPM catalogues (705 rows each) — WAITING ON USER's rates
   (task #11). Source files have no GST column; HSN mapping ambiguous.
3. Light-theme polish: inner screens inherit tokens but dark-tuned hardcoded
   colours remain (mbp panels, hint-box, alerts, login) — un-audited.
4. Phase 2 (3-lakh batch import) — when the real master sheet arrives.
5. Phase 5 deploy (company server, network-restricted) — server not ready.
6. Phase 6 hardening: raw tracebacks still leak in some endpoints; no tests
   for pricing/GST math; backups manual only.
7. sec-home landing page unreachable (dead code); static/demo.html stale.

## Standing rules (also in memory files)
- Master table writes = admin only, always `require_role("admin")`.
- Typed quotation view: NO cost/profit columns (inventory check only);
  cost/profit only on BOQ-sourced quotes (has_boq_pricing) + Margin panel.
- Single price column ("PRICES IN INR"/AMOUNT/RATE) mirrors into 3★ AND 4★;
  ask user if pricing columns are ambiguous.
- Never run bulk tier pricing on catalogues with real selling prices
  (overwrites them with discount-off-cost; reset button recovers).
- After a classifier block on credentials: hand the command to the user.
- Verify in the real browser after frontend edits (a broken template escape
  once killed ALL of app.js silently).
