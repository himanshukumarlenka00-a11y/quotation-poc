# HANDOFF — quotation-poc session state (2026-08-02)

Read this first in a new session. Cross-session rules also live in
`C:\Users\itzan\.claude\projects\E--rtk-bin\memory\` (auto-loaded).

## What this is
FastAPI + vanilla JS quotation tool for Melange. NO React — three frontend
files: `static/index.html`, `static/js/app.js`, `static/css/main.css`.
Server: `run_local.ps1` (uvicorn, auto-reload on `app/` only) or the
`.claude/launch.json` "quotation-poc" preview config. Port 8000.
Frontend changes need a cache-bust: bump `?v=NN` on both assets in index.html
(currently v61). Repo: github.com/ai-eng67/quotation-poc (PRIVATE — contains
the DB with costs), branch `restructure-auth-smart-import`, all pushed
(HEAD 19e4c10).

## The three core motives (all BUILT)
1. Search master table — master-only matching; employees never see cost
   (stripped server-side, incl. quotation payloads + _variants).
2. Master vs past quotations — 📊 Margin button per quote (admin): per-line
   cost/profit/margin, honest states ok|no_cost|not_in_master.
3. BOQ coverage — in-card check on the dashboard + full report screen +
   admin add-missing. The BOQ Coverage tab shows the BOQ card alone
   (`sec-generate.boq-only`); Generate shows the two-column page.

## Built since 2026-07-31 (all committed & pushed)
- Price-constraint queries: "10 cups under 1k", <=/>=/above/upto/max/budget,
  ranges "between 200 and 2000", 1k/commas/Rs forms. Parsed by
  `_strip_price_constraint` (quotations.py) from the TYPED label only —
  never from BOQ search_term (spec text like "up to 1000 ml" must not
  become a price cap). Filter applies to first selected tier only (user
  decision: discounts come later). Constraint regex is word-boundary-safe
  ("thunder 500" ≠ "under 500"). Local tests: test_price_constraint.py
  (gitignored by repo convention, 29 asserts).
- Bulk tier pricing: % now discounts off ORIGINAL price snapshot (not cost);
  clamped 0–100. Scan returns priced_products; UI demands a Teach when a
  file has products but zero prices.
- Smart Import card: full in-card animated flow (detect→route→map→approve→
  import→success, SVG icons, stepper ✓s, state persists across navigation,
  nothing auto-redirects). BOQ files get an in-card coverage check.
  Unknown-type state has Cancel. Filenames HTML-escaped (escHtml).
- Dashboard: stat-tile sparkline/meters/mini-bars (quotes_spark + cat_bars
  from /api/dashboard), dated greeting, hover polish. User lukewarm on the
  tile graphs — may restyle/remove later.
- Generate page redesign: two-column full-height (create card + BOQ card),
  tier cards with descriptions, soft-filled inputs, 0/1000 counter,
  Ctrl+K topbar search (tsearch2), gradient CTAs, ghost-pill row buttons
  with SVG icons (no emoji — they render grey on Windows).
- Login redesign: CSS-animated Melange lockup (login-brand2: arcs draw in,
  serif MELANGE letter-fade, tagline, light sweep). The AI-generated
  melange_5sec.mp4 was REJECTED (Gemini watermark) and deleted from static/.
- Light-theme fixes: nav-user white-on-white, drop zones, folder-row hover,
  gold BOQ card → purple. Login/margin-panel/hint-box dark leftovers remain.

## Known quirk
The tier-card .active stylesheet rule never applied to the two tier nodes in
the embedded test browser (a clone styled fine — unexplained). toggleTier now
sets inline !important styles as the working fix; redundant CSS layers were
deleted in the review pass. Not yet visually confirmed on the user's browser.

## LLM setup
llama-3.3-70b-versatile on Groq, TWO call sites (extraction + semantic
fallback). Free tier 12k TPM / 100k TPD. Approved future roadmap (memory:
llm-learning-caching-roadmap): few-shot corrections injection at ~50–100
corrections + DB-table extraction/semantic caches. No fine-tuning, no Redis.

## Data state
~52 MB total: DB 9.6 MB (6,058 products, 10 catalogues, 131+ quotes),
13,232 images = 42 MB. Catalogues incl. KMW 743, ARIANE 2297, NILKAMAL 493,
RENA 359, OPM 705 ×2 (GST 0 — waiting on user's rates), AMAYDA 104.
Test user: emplyoee@melangeindia.in / 12345678 (employee; email typo'd).

## OPEN ITEMS (priority order)
1. Deployment: company server is Ubuntu, NOT yet reachable — waiting on
   IP + SSH user (key-based; never type passwords). Interim option approved
   in principle: serve from this PC (0.0.0.0:8000 + firewall + Task
   Scheduler). Prep worth doing: deploy.sh, /health endpoint, backup cron.
   230 GB free disk = plenty. Production-audit doc requested but NOT written.
2. Page redesigns remaining: Current Quote, Quotations list, Master
   Catalogue, Margin Analysis, Upload BOQ, Activity, Users & Roles.
   Dashboard empty-space plan approved (equal-height cards + longer lists +
   "Top quoted products" card, ~4–6k) — not built.
3. UI logic audit areas 3–8 (import flows, master search, quotation flows,
   margin/coverage, admin pages, endpoint xref). Areas 1–2 done.
4. GST = 0 on both OPM catalogues — WAITING ON USER's rates.
5. Phase 2 (3-lakh batch import) — needs server-side paging for the master
   page + chunked import when the real sheet arrives.
6. Phase 6 hardening: tracebacks leak in some endpoints; no pricing/GST
   tests in repo; backups manual.
7. Cleanup: sec-home dead code; static/demo.html stale.

## Standing rules (also in memory files)
- Master table writes = admin only, always `require_role("admin")`.
- Typed quotation view: NO cost/profit columns; cost/profit only on
  BOQ-sourced quotes (has_boq_pricing) + Margin panel.
- Single price column ("PRICES IN INR"/AMOUNT/RATE) mirrors into 3★ AND 4★;
  ask user if pricing columns are ambiguous.
- User is highly token-conscious: give a token estimate BEFORE sizeable work
  and get a go-ahead; work one item at a time; no multi-agent workflows.
- No browser-verification theatre: verify cheaply (node --check, targeted JS
  eval), report when done; the user tests visually themselves.
- After a classifier block on credentials: hand the command to the user.
- Emoji glyphs in UI render grey/tofu on Windows — use inline SVG.
