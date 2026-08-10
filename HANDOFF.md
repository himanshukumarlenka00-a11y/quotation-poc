# HANDOFF — quotation-poc session state (2026-08-10)

## Session 2026-08-10 (14 commits, f6878c5..66d6d47, all deployed + pushed)

STANDING RULE ADDED — VISUALISE BEFORE BUILDING. For any change with a UI
surface, show an interactive mockup via the visualize tool and get approval
BEFORE writing code. Stated verbatim: "before doing anything we will
visualize the things — always remember this things". Saved as memory
`visualize-before-implementing`. Does not apply to pure backend work.
It has already paid: the batching design and the Margin-Analysis structure
both changed shape after the mock.

### THE RECURRING MISTAKE THIS SESSION — read this before building anything
Three times I built a parallel version of something the app already had,
because I checked whether the CODE existed without checking whether the
USER could reach it, or without checking what the existing view already
rendered. Cost: ~500 lines written and then deleted.
  1. Margin analysis table + Margin_*.xlsx (4b11142) — deleted five commits
     later (66d6d47) because Current Quote ALREADY renders BOQ Price,
     Profit and a total-profit footer, and build_company_quotation ALREADY
     appends the same two as columns N/O.
  2. Claimed "the Generate page already has a file upload" from the markup
     alone; CSS was hiding it (82b1410).
  3. Nearly added a second drop zone next to an identical one.
BEFORE writing a view: grep what the target page already renders, and open
the page. `has_boq_pricing` in particular gates a lot of already-built UI.

### Margin analysis: deleted, not moved (66d6d47, net -314 lines)
The button now runs the same generation and LANDS ON CURRENT QUOTE, flashing
#foot-profit so it is obvious what you were sent to look at. A file with no
prices toasts instead of showing an empty profit view.
GONE: /api/analyse-quotation, /api/analyse-quotation/export,
build_margin_analysis, renderQuotationAnalysis, downloadAnalysisXlsx, all
.ma-* CSS. Both endpoints verified 404 in prod.
DELIBERATELY LOST, user chose this over keeping a second surface: per-line
COST, MARGIN %, and the costed / no-cost / unmatched counts. Current Quote
shows profit but never cost. If those are ever wanted back, they belong as
columns in Current Quote (gated on role — _strip_cost removes cost from
employee payloads), not as a new page.

### Non-array API payloads — a whole bug class, now half-fixed
A lapsed session answers valid JSON `{detail: "Not logged in"}`, which
`.json()` parses happily and the next `.filter`/`.flatMap` then throws on,
taking the page down with only a console trace. Two sites fixed with
`asList()`: loadRepository (32d0914) and /api/sales-persons (66d6d47) —
the latter's try/catch covered network failure only, so a lapsed session
killed the entire quotation render. The fetch interceptor shows the login
gate, but it does NOT stop the throw. Any OTHER loader that assumes an
array is still exposed; asList() is there, use it.

### Matcher: glued tokens (b4fc852) — real defect, fixed and measured
FTS5 matches token PREFIXES, so `"dustbin"*` finds DUSTBIN and DUSTBINS but
never WALTHR-IR-RD001-OVL-ROOMDUSTBIN — one token, starting with "room".
The index is NOT stale (51,938 = 51,938, zero missing rows); a prefix query
simply cannot see into the middle of a token. Invisible rows on live data:
dustbin 19/63, tray 233/1476, bowl 27/4401, shaker 4/412, kettle 0/79.
Fix: `_glued_rows()` + `_merge_glued()` — a bounded substring scan (<8ms
worst case on 52k) appended to the FTS pool, wired into BOTH FTS call sites
(`_line_pool` and `suggest_products`) so generator, Switch and Find gain it
together. Words <4 chars or numeric are skipped ("ss", "18" as substrings
match half the catalogue). Chose this over a trigram index: no rebuild, no
second index to keep in step. test_glued_tokens.py, mutation-checked.
MEASURED AFTER DEPLOY (live, variant counts): dustbin 31 -> 50 (+19, exactly
as predicted), kettle 79 -> 79, shaker 344 -> 345, latency unchanged.
BUT tray stayed 3 -> 3. The 233 rows now enter the pool and are then
REJECTED BY THE SCORER, which is a different defect: a one-word generic
request like "tray" covers too little of a long product name. 3 variants out
of 1,476 rows containing the word. NEXT THING WORTH FIXING.

### Switch panel: the 15-variant cap (2b3553a, 97d9abc)
"Dustbin" has 50 distinct matches; the panel showed 15 with nothing saying
more existed. `uniq[:15]` is now a `variant_cap` PARAMETER (default 15 — a
34-line quote carrying 50 variants each is a megabyte of JSON nobody
scrolls), and GET /api/product-variants re-runs THE SAME resolver for one
product with a bigger cap. Same resolver deliberately: a second scorer would
reorder the list under the user mid-decision (verified top variant identical
at cap 15 and 200). Loads 30 per click; the endpoint also returns the true
total so the note reads "45 of 50" and the button retires itself at the end.
Fetched variants are APPENDED, never substituted — applySwitch() indexes
into _variants, so the matcher's pick must keep index 0.

### Generate page: one card, two ways in (003cd57, 82b1410, 995a779)
Three commits because I got it wrong twice, in an instructive way:
 - 003cd57 merged the margin action into the "file" card believing that card
   was already on the Generate page. Its MARKUP is, but a later single-column
   redesign hid it: `#sec-generate:not(.boq-only) .card-alt{display:none}`.
   It only ever rendered in BOQ Coverage. LESSON: reading the markup is not
   reading the page — check the CSS before claiming a thing is visible.
 - 82b1410 un-hid it.
 - 995a779 merged both cards into ONE (user picked "both visible" from a
   mocked A/B): textarea | or | drop zone inside Create a Quotation,
   stacking below 900px. ONE Generate button dispatches — a chosen file
   wins over the textarea, and the card SAYS SO ("Generate will use
   bar.xlsx ✕ clear") instead of deciding silently.
`.card-alt`, `.or-divider`, `gen-boq-btn` and the two-column grid are gone.
BOQ Coverage shares the section and now hides the typed half + tier picker
+ Generate rather than a whole second card.
NOTE: /api/smart-generate-from-boq already turned any .xls/.xlsx into an
editable quote with the file's own prices kept as boq_price, and
renderResult shows BOQ Price + Profit columns whenever has_boq_pricing —
that capability existed all along, nobody could find the button.

### Margin analysis (4b11142) — SUPERSEDED, all of this was deleted
Built the analysis in quotation shape plus a Margin_*.xlsx, then removed it
whole in 66d6d47 (see above). Kept here only as the worked example of the
recurring mistake: it duplicated Current Quote's existing profit view.
One thing survives and is worth knowing — `_resolve_master_matches` returns
brand, spec, HSN, image_path and gst_pct on each matched row; the analyse
endpoint had simply never passed them through.

### Session expiry (32d0914)
loadRepository() assumed its fetch returned an array; on a lapsed session the
API answers {detail:"Not logged in"}, .filter threw, and the user got a blank
page with the reason only in the console. EVERY loader shared that
assumption. Fixed once, in a fetch interceptor: any 401 outside /api/auth/
while authed drops to the login gate with "Your session expired." /api/auth/
is excluded because a wrong password also 401s.

### Smaller (f6878c5, 902d569, 0f4fe9b, e8dddf8)
- Login tagline said "Innovating Jamsetjiar"; the real logo reads
  "innovating hospitality". Brand animation reworked 4.1s -> 2.4s, and the
  "QuoteGen AI" line removed.
- REDUCED MOTION IS ON, ON THIS MACHINE: HKCU\Control Panel\Desktop\
  WindowMetrics\MinAnimate = 0, so Chrome reports prefers-reduced-motion.
  A reduce block that pins everything to its final state therefore reads as
  "no animation is there" to this user. Degrade to an opacity fade instead —
  and remember every other animation in the app is silently off for them.
- Hover details panel is now a click target for its card (pointer-events
  auto while shown, 140ms grace over the 10px gap, forwards the click).
- Request cap 1000 -> 8000 chars in all four places, plus a
  Field(max_length=8000) the server never had at all.

### Still open from today
- "tray" scoring (above) — the biggest remaining matcher gap. 1,476 rows
  contain the word, 3 become variants. Glued tokens are NOT the cause; the
  scorer rejects generic one-word requests against long product names.
- The 400-row FTS pool cap: tray 1428 hits -> 400 reach the scorer,
  mixing bowl 4651 -> 400. Proposal was to keep 400 for batch generation and
  raise it for the one-product Switch/Find path; not done, needs timings.
- Other loaders still assume array payloads (see the bug-class note above).
- Margin Analysis page is now lists-only, and its upload is gone. Its
  Approved / BOQ-priced Drafts sections filter on has_boq_pricing, so they
  fill only from quotes generated off a priced file — which the Generate
  page now does, so they should stop being empty.

### Cache-busting state at end of session
index.html references main.css?v=123 and app.js?v=127. index.html itself is
NOT versioned, so a stale index means stale asset URLs too — Ctrl+Shift+R.

# (previous session notes follow)

## LIVE IN PRODUCTION (see memory: ubuntu-server-deployment)
Server melange@192.168.0.146 (crm-server). App :8000 + HTTPS :8443 (nginx,
self-signed, cert trusted on user's PC). Master table 51,938 products.

TWO ADDRESSES FOR THE SAME BOX — do not panic when the LAN one dies:
  LAN        192.168.0.146    office network only
  Tailscale  100.94.230.77    anywhere with internet (tailscaled is active
                              on the server; the user's PC is on the tailnet)
Both verified: https://100.94.230.77:8443/health -> 200, SSH works. The
ed25519 host key is identical on both addresses
(SHA256:4s71FMUiP+1JG7gRa455kwPi7Rhf3g48l2FzNfdgCUE) — compare before
trusting a new one rather than blind-accepting.

Diagnosing "server is down": check YOUR OWN ip first (ipconfig). Twice now
the box was fine — up 2+ days — while this PC had dropped off the LAN and
held only Cloudflare WARP / Tailscale / a 10.204.x wifi address, no
192.168.0.x at all. Ping failing tells you nothing about the server. The
CRM on port 80 of the same box is another liveness check. Git/GitHub never
needs the LAN.

Deploy flow: edit dev → verify cheaply → commit → tar-pipe to /opt/quotegen
→ restart quotegen if Python changed. ASK BEFORE EVERY SERVER-CHANGING
COMMAND. Report est. tokens after each task. RTK proxy corrupts piped grep —
use python for pipes.

## Standing behaviour rules (don't relearn these)
- Explicit model number = HARD GATE. "[WCCE001-SS]" never becomes WCCE002.
  Exact model wins; no match → placeholder row, never a substitute.
  Human corrections still outrank the gate.
- Model lookup is brand-prefix tolerant: sheets say "[KMW-TB770]", master
  stores original_model='TB770' with the brand in the product name.
- Teaching match_corrections is OPT-IN. Switch/Find carry a "Remember this
  choice" checkbox, OFF by default, and only a ticked box writes a
  correction. Supersedes the old blanket "placeholders never teach" rule.
  Reason: switching means either "the matcher was wrong" or "this client
  wants something else once", and treating the second as the first is how
  "[WCCE001-SS]" got pinned to WCCE002. The `remember` flag is stripped
  before save — it is an instruction, not a property of the line.
  Bad learned row id 273 was deleted from prod.
- Empty-quote guard ignores placeholders (all-placeholder → nothing saved).
- Placeholder lines are NEVER written to match_corrections, by either branch.
  The confirmation branch (which records every unchanged line on each save)
  was storing a placeholder's raw request text as if it were a product:
  "Strogae Rack" -> "Strogae Rack". 33 of 99 rows were that junk; deleted
  2026-08-10 after a backup, 66 real ones remain. Harmless while nothing in
  the master matches the text, but a landmine — import a product with that
  name and the row becomes a live auto-selection learned from nothing.
- Switch panel sorts cheapest-first for DISPLAY ONLY — _variants[0] stays the
  matcher's pick, sorting the array would re-point every quote.

## Matching overhaul 2026-08-06 (accuracy 86% -> 94%, audited)
Four separate root causes, all found by measuring against the live 52k
catalogue rather than reasoning from screenshots:

1. FTS pools used LIMIT with NO ORDER BY, so SQLite returned rowid order and
   the cap discarded the NEWEST imports. A real 20-line request matched
   16,518 rows, kept 4,000, threw away 12,518 — IRON ORGANISER (id 61220),
   HAIR DRYER (61215), IRON BOARD (61219) all sat outside it, so the matcher
   never saw them. Manual search found them because it LIKEs
   master_products directly and never touches the pool. Any recently
   imported catalogue was systematically invisible. Fixed with
   ORDER BY f.rank (bm25) on all four pool queries.
2. Coverage was only measured request->name, so every qualifier the client
   typed counted AGAINST the product: "Hair Dryer, Color - Black / Grey
   Wall-Mounted" was 2/7 = 29% against "HAIR DRYER" and got rejected. Added
   a reverse test — the product's whole name appearing inside the request
   (2/2). The old guard survives because it turns on words never asked for:
   "waste bin" vs "Ice bin module" is 1/3, so the Rs65,082 mismatch stays
   blocked. Scored 400 + 20/word: under the forward full match, longer names
   win, so IRON ORGANISER beats IRON and "Cup Dispenser" beats bare "CUP".
3. That reverse test ignores the brand/code prefix, or it would demand
   "melange"/"smle" be typed for MELANGE-SMLE0054-Pillow Twin Feather.
4. Bare numbers are model codes. mtoks needed letters AND digits, so
   "WCCE001-SS" counted but "2688" did not, and "ARDACAM 2688 Plate" tied
   with ARDACAM-2447-Plate on name alone. Now a tie-breaker (+300) on rows
   that already match, so a size like "500" cannot drag in model 500.
   Fixed 7 of 12 audit failures on its own.
Also: spec-only matches now need >1 significant word ("SAFE" was returning
"GLOVE LARGE" because the word sits in that glove's spec).

A failed line is no longer a dead end: suggest_products() runs a loose
second pass (its OWN per-line FTS query — reusing the shared pool returned
"WALL DRYER" because HAIR DRYER was outside the 4000) and the Find panel
opens pre-loaded with candidates. Nothing is auto-applied.

_suggestions is underscore-prefixed and therefore stripped before save, so
it only ever existed in the fresh generate response. Rather than persist six
raw master rows per placeholder, the panel FETCHES them when they are absent:
GET /api/suggest-products?q=... (skipped if the user has already typed).
Works on a quote of any age. suggest_products, _covered and _UNITS are
module level so the endpoint and the matcher cannot drift apart.

TRAP, cost 20 minutes: hoisting _covered through a shell heredoc ate its
\b word-boundary anchors, leaving re.search(r"" + ...) — plain substring
matching, so "pin" matched "chopping". Accuracy fell 94% -> 73% and the
audit caught it. Edit that regex with a file edit, never through a heredoc.

DATA ISSUE for the user, not code: 349 product names are duplicated within
a single catalogue. e.g. "ROUND DAMPING HINGED  CHAFING DISH  LARGE
CAPACITY" (8060C, Rs21,812) vs the same name with one less space (8040C,
Rs20,482) — indistinguishable by name, only a model code separates them.

## Matcher: per-line candidate pools (the big one)
rows_pool was ONE FTS query for the whole request, capped twice — 
sorted(words)[:40] and LIMIT 4000 — and both caps tighten as the request
grows. Found by auditing 40 RANDOM products instead of 14 hand-spread ones:
exact catalogue names failed 17/40, all 17 absent from the pool, 8/8 of them
matching when sent alone. A 40-line batch had 147 distinct words, so most
lines were never searched for at all; a 700-line BOQ was mostly guesswork.
ORDER BY rank (the earlier fix) only reordered a pool already too small.

Each line now gets its own ranked query (LIMIT 400), cached per term.
Cheaper too: 40 lines x 4000 shared rows = 160k comparisons, vs ~400 of
their own = 16k. rows_pool stays as the FTS-unavailable fallback.

  exact name  22/40 (17 no-match) -> 39/40 (0 no-match)
  human name  21/40 -> 37/40      + qualifiers 20/40 -> 36/40
  plural      18/40 -> 34/40      typo         17/40 -> 22/40
  20-line   2,859ms -> 1,207ms    100-line  20,555ms -> 5,187ms
Unchanged: 0 price mismatches, no cost in employee payloads, 0/10
fabricated model codes substituted, 14-product audit still 94%.

LESSON: a small hand-picked sample hid this completely. Audit with RANDOM
products, and at batch sizes matching real BOQs.

KNOWN WEAK SPOT: typos (22/40). A transposed letter defeats word matching.
Fuzzy matching would trade precision for recall — not done, ask first.

## UI 2026-08-06
- Global interaction feedback: press-dip on buttons that are not .btn,
  :focus-visible ring (there were ZERO app-wide), and a
  prefers-reduced-motion block retiring all 37 animations. The press rule is
  deliberately low specificity (button:active = 0,1,1) so .btn:active and
  .mbp-reset:active (spins 180deg) still win.
- Manual product entry, uploaded images now saved to disk at save time
  (were base64 inside items_json, and the XLS only reads image_path so they
  came out blank).
- Deleted the unreachable sec-home landing page, static/demo.html and the
  dead suggestion-strip code/CSS: -478 lines.
- 8 endpoints stopped returning tracebacks to the browser; they now log the
  trace with a short error id (config.server_error).
- test_pricing.py guards the money maths, force-added past the test_*.py
  gitignore, mutation-checked both ways.
- 18 blocking alert() dialogs replaced with toasts (bottom-right, stacked,
  errors linger 5.2s vs 3.2s). Checked first that none relied on alert()
  blocking. confirm() deliberately left alone: 5 sites guard destructive
  actions and are synchronous, so converting them means async reworking each
  call site — cosmetic gain, real risk of breaking a delete guard.

## Built 2026-08-05
- CSS TOKENS FIXED (big one): --fs-xs/sm/base/md/lg, --ctl-h, --sp-3, --sp-4
  were used ~270× but never defined, so every one of those declarations was
  dropped — body fell back to 16px, inputs had zero padding and auto height.
  Now defined 11/12/13/14/16px, 40px, 10px, 14px in :root.
- Manual product entry (v105-108): "Enter a product manually" row above SUB
  TOTAL — image upload, name/model/brand/spec/qty/price/HSN/GST. Visible
  validation (silent focus() read as a dead button). Its CSS is ID-scoped:
  ".quot-doc td input{width:78px}" and "td:first-child{text-align:center}"
  hijack it otherwise, since the form lives in one colspan <td>.
- Refresh prices: ⟳ in the QUOTATION bar → POST /api/quotations/{id}/
  refresh-prices. Keeps pre-refresh value in prev_price_3star/4star, only on
  the FIRST refresh. "Original price" checkbox shows it; "Set price" dropdown
  in Price/Pc offers 3★/4★/Orig (deferred via setTimeout — the select
  destroyed itself mid-change-event otherwise).
- Uploaded photos now decode to disk at save (update_quotation) instead of
  living as base64 in items_json — export only reads image_path, so they used
  to come out blank in the XLS.
- images.py raw-XML fallback: openpyxl returns ZERO images for a sheet whose
  drawing mixes <xdr:pic> with shapes (BOROSIL: 628 pics + 70 lines → 0).
  Reads the drawing XML directly. BOROSIL re-imported: 624/639 images.
- Uploads raised to 200MB (nginx client_max_body_size + MAX_UPLOAD_BYTES);
  MARTELLATO is 148MB. Re-uploading an existing filename now 409s with a
  "Replace existing catalogue" override.
- Master catalogue: Original column always shows a real price (falls back to
  current when never bulk-discounted); .c/.num alignment classes; Imported
  Files collapse toggle. Removed the "Frequently quoted together" strip.
- _lookup_by_model has an indexed fast path (was a full scan: ~95ms per line
  at 52k rows ≈ 60s on a 700-row BOQ; now ~0.05ms).

## Known gaps / next
1. DONE (2026-08-06): cleanup, traceback leak, pricing test.
2. DONE (2026-08-06): suggestions now fetched on demand, so they survive a
   reload.
3. Page redesigns left: Quotations list, Margin Analysis, Upload BOQ,
   Activity, Users & Roles. Dashboard empty-space plan approved, not built.
4. UI logic audit areas 3-8 (areas 1-2 done).
5. BLOCKED on user: OPM GST rates (both catalogues still 0%); the real
   3-lakh sheet; Cerebras free tier (key is in /etc/quotegen/env and the
   fallback is live but the org 402s until Billing is activated).
6. Excel export still carries its own salutation + footer lines — user
   removed them from the web view only, hasn't asked about the XLS.

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
