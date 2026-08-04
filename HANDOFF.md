# HANDOFF — quotation-poc session state (2026-08-04)

## LIVE IN PRODUCTION (see memory: ubuntu-server-deployment)
Server melange@192.168.0.146 (crm-server). App at :8000 + HTTPS :8443
(nginx, self-signed; cert trusted on user's PC). HEAD b0ff7f5, cache v88.
2026-08-04 (later session): feedback card restyled (theme card + SVG pills);
removed doc footer line, generate hint bar, salutation block, topbar Ctrl+K
search. Excel export still has its own salutation + footer lines (export.py)
— user hasn't asked to drop them there. Export writes computed VALUES not
formulas (user's Excel is in manual-calc mode; told them, unfixed on PC).
Not-in-catalog items -> in-place placeholder rows (name+qty, amber tint,
inline model/brand/spec/HSN inputs via setItemField; empty-quote guard
ignores placeholders). Export DESCRIPTION col = item.requested (client's
wording), SPECIFICATION = master spec. Drag-to-reorder quote rows via SL
handle (initRowDrag, renumbers + silent save). Phase 2 paging DONE:
/api/master-table/summary + /page (limit<=500, q search, cost stripped);
master page lazy-loads 200/folder + Load more; server-side search cap 500.
Phase 2 chunked-import half DROPPED — user will split the 3-lakh sheet and
upload chunks himself (open q: append chunks into one catalogue or not).
Later (v89-v92): class-based column alignment (killed nth-last-child(-n+7)
rule); removed dup grand-total block + edit-hint + amount-in-words lines
(web only; export keeps its own); action buttons moved BELOW doc, ref badge
left/buttons right; Amount/GST spacing; placeholder rows got 🔍 Find (live
search via /api/master-table/page, applyFind fills row); export HSN col
width 11. Night session (v93+): Revert button on Find-picked rows
(was_placeholder flag). Explicit model number = HARD GATE in matching
(never auto-substitute WCCE001->WCCE002; exact model first, else
placeholder; corrections outrank gate). LLM: _llm_chat helper — Groq
primary, Cerebras gpt-oss-120b fallback on 429 (CEREBRAS_API_KEY in
/etc/quotegen/env; org still 402 until user activates free tier in
Cerebras Billing tab — fallback dormant, fail-safe). Quota savers:
1-line prompts with [MODEL] parse with no LLM; bracketed model outranks
prose markers; explicit-model items skip semantic call. USER RULE:
placeholder-lineage lines NEVER teach match_corrections (Find pick =
stand-in for that quote only); bad learned row id 273 deleted from prod
DB. HEAD 4caef3b, cache v93, server current. NOT pushed to GitHub.
Deploy flow: edit dev → verify → commit → scp files → restart quotegen →
ASK USER BEFORE EVERY SERVER-CHANGING COMMAND. Report est. tokens after
each task. RTK proxy corrupts piped grep output — use python for pipes.

## Built 2026-08-03/04 (all deployed)
- Sales person picker (region-first, auto-select single-region); editable
  Bill & Ship To (bill_to, exports to A10); export sales block at M11-13.
- Optional tiers: none selected = single plain PRICE column (data.tiers).
- Teachable BOQ columns (shared column_mappings; qty teachable; 422 +
  teach UI on coverage page when a BOQ can't be read).
- Generate box: pasted lists PRODUCT [MODEL] [qty] parse without LLM
  (qty optional → 1); From PDF (pypdf) + From Excel fill buttons;
  LLM extraction max_tokens 1200 + tolerant JSON slicing.
- .xls images on Linux via LibreOffice convert (installed on server);
  CRITICAL: parser reads VALUES from ORIGINAL file, conversion is
  images-only (formula columns lost cached values otherwise — BARKRAFT
  1100/1100 priced + 1077 images after re-import). Import result always
  reports priced count (canary).
- Per-catalogue download of original file (admin, audit-logged).
- Gmail sidebar: hamburger above Dashboard, fixed position, mini icon
  rail (.slbl spans — never font-size:0), main-col margin-left.
- Add-to-quote = sleek pill bar (no heading/helper prose).
- Never-save-empty-quotations guard on all three generate paths.

# Older state (2026-08-02)

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
  ALSO report the estimated tokens actually used AFTER each completed task
  (small table or one line; estimates are fine, state they're estimates).
- No browser-verification theatre: verify cheaply (node --check, targeted JS
  eval), report when done; the user tests visually themselves.
- After a classifier block on credentials: hand the command to the user.
- Emoji glyphs in UI render grey/tofu on Windows — use inline SVG.
