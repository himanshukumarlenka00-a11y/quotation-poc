# HANDOFF — quotation-poc session state (2026-08-10)

## Session 2026-09-02 — WORD-COLLISION FIX DEPLOYED + LIVE-VERIFIED; dashboard balance reconciled

Continues 2026-09-01 (Sonnet was deployed live earlier). This session fixed the
remaining wrong-match CLASS, deployed it, proved it LIVE in the user's browser,
reconciled the AI-usage balance, and cleaned the repo. All pushed.

### THE BUG FIXED — word-collision wrong matches ("Iron" -> "Cast Iron Round Casserole")
A short request whose word(s) are fully present in a LONGER product name of a
DIFFERENT type shipped confidently WITH A PRICE. Root cause: the Sonnet
meaning-check (`_llm_verify_matches`) only fired when a match scored < 1000;
"Iron" fully covered a longer name, scored 1017, so it SKIPPED the check. This is
a whole CLASS, not one product (steel, brass, corer, deep, square, gold, milk...).

FIX (app/routers/quotations.py, commit 9c98525): added `_sig_words()` +
`_needs_verify(it)`. The meaning-check now ALSO fires on a THIN weak-tier match —
request has 1-2 significant words AND the matched name has >=2 MORE significant
words — regardless of score. Both trigger sites (the central `_llm_verify_matches`
selector and the llm_verify=False else-branch that attaches llm_cands) call
`_needs_verify`. It reads the ORIGINAL request via req_raw/_req_raw/requested (NOT
`product`, which is overwritten with the matched name).

### MEASURED (isolated copy, read-only, Sonnet on) — the fix is SURGICAL
Test pattern: cp prod DB + semantic_index.npz to a /tmp copy; APP_DIR=/opt/quotegen
DATA_DIR=/tmp/copy; env from /etc/quotegen/env via sudo; stub `_llm_chat` for
zero-cost deterministic runs or use the real client for the meaning-check. NEVER
run bulk against the live prod DB (isolated copies keep prod ai_usage clean).
- Full catalogue vocabulary (1443 real words): fix changes only 18 matches. Sonnet
  on those 18: iron + corer REJECTED (real wrong matches), every genuine type match
  kept. ~₹1.
- ALL 11,342 distinct real BOQ request lines (boq_items.product): fix changes only
  15 (0.13%): 12 kept, 3 lateral same-type switches, 0 regressions.
- Broad meaning-check over all 1443 words: rejected 680 / switched 91 / kept 666
  (most rejects are non-product fragments correctly refused; ~30 clean real-word
  wrong matches caught — iron is one of a CLASS). ~₹41 (isolated, off the prod tile).

### DEPLOYED + LIVE-VERIFIED
Deployed to /opt/quotegen, `sudo systemctl restart quotegen`. Verified LIVE in the
user's own browser: BOQ line "Iron 10" -> "Nothing matched. No quotation was saved."
Server proof: POST /api/smart-generate at 17:27:03 + 3 fresh claude-sonnet-5 rows
in prod ai_usage (271/10 tokens each) = the meaning-check firing and answering
"none". Backup: /opt/quotegen/app/routers/quotations.py.bak-20260902-155539.

### AI-usage dashboard balance reconciled to the real console (commit 00fe0b4)
Dashboard ESTIMATES remaining = $5 - (spend from prod ai_usage tokens); a normal
API key can't read the real Anthropic balance. Today's isolated-copy testing spent
~$0.50 of real credit that logged to throwaway copies, NOT prod, so the tile drifted
above the console ($4.67 shown vs $4.17 real). Added ANTHROPIC_SPENT_OFFSET_USD
(app/config.py, env, default 0): a fixed reconciliation for untracked external spend,
folded into the LIFETIME total + remaining only (monthly stays the tool's own usage).
Prod env set ANTHROPIC_SPENT_OFFSET_USD=0.503 -> dashboard now reads remaining
$4.170 / total $0.83, matching the console. Backups: config.py.bak-* + a 2nd
quotations.py.bak-* (same timestamp).

### Repo cleanup (commit 24bc3cc)
Reverted stray local changes (two scripts had a STALE `E:/niewttdt/` path; a
run_local.ps1 machine path; ~12KB of dev-DB runtime noise — prod DB is separate on
the server, untouched). Added data/semantic_index*.npz to .gitignore (regenerated
from the catalog DB on startup). Working tree clean.

### STATE / OPEN ITEMS (next session start here)
- Branch: restructure-auth-smart-import. Pushed to
  github.com/himanshukumarlenka00-a11y/quotation-poc — 9c98525, 00fe0b4, 24bc3cc
  (+ earlier 48cfa84, e82bf69, 7274e33). Push works (creds cached; git binary at
  C:\Program Files\Git\cmd\git.exe; use GIT_TERMINAL_PROMPT=0 to fail fast).
- OPEN — API KEY ROTATION (pending user, NOT done): the sk-ant-...Upas... key was
  pasted in chat earlier — treat as compromised. User must create a new key + revoke
  the old one in the console, then on the server:
  `sudo sed -i 's|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=NEW|' /etc/quotegen/env`
  then Claude restarts + verifies. Claude must NOT write the key value itself.
- Optional: merge branch to main; delete /opt/quotegen/**/*.bak-* once deploy proven.
- Server: melange@192.168.0.146 (passwordless sudo), prod data /srv/quotegen-data.
  Real Anthropic balance $4.17 of $5 as of this session. Memory:
  [[word-collision-meaning-check-fix]].

## Session 2026-09-01 — REAL PROBLEM FOUND (hallucination) + CLAUDE SONNET FIX BUILT (not deployed)

Read this first. This session found the ACTUAL cause of the user's complaint
and built the fix. The fix is IN THE WORKING TREE, uncommitted, NOT deployed.

### THE REAL PROBLEM (not what we thought)
User's actual pain: paste an Excel BOQ / search, and results are "not accurate,"
sometimes "totally different" / hallucinating (ask X, get unrelated Y). This is
WRONG MATCHES, not blanks (not_found). We spent a day on not_found first — most
of that is genuinely-absent items (correct behaviour), a dead end for this.

ROOT CAUSE, proven from prod logs (`journalctl -u quotegen`): 235× "LLM verify
batch skipped: Error code: 429 - Rate limit reached ... tokens per day (TPD):
Limit 200000" (model openai/gpt-oss-120b, Groq FREE tier). The LLM verify pass
that CATCHES wrong picks gets rate-limited OFF once the 200k-tokens/day free cap
is hit — one big BOQ exhausts it, then every quote that day ships UNVERIFIED and
free-associated junk goes through. Logs show the hallucination directly (only
caught when zero shared word): "Sticks"->"Chopstick Stand", "Gel 14-3D"->"Bridge
Large", "Plisse"->"Classic Beer Plisner". App itself healthy (0 server errors).

### THE FIX — Claude Sonnet integration (BUILT + TESTED, in working tree, UNCOMMITTED)
Swapped the LLM layer to Claude Sonnet as primary, Groq auto-fallback, fully
reversible. Files changed (git diff will show them):
  - app/config.py       — ANTHROPIC_API_KEY_DEFAULT, ANTHROPIC_MODEL
                          (default "claude-sonnet-5"), and make_llm_client():
                          returns Anthropic client if ANTHROPIC_API_KEY set,
                          else Groq if GROQ_API_KEY set, else None.
  - app/routers/quotations.py — _llm_chat now dispatches by provider (detects
                          Anthropic by client module); Claude branch splits out
                          `system`, DROPS `temperature` (Sonnet 5 returns 400 on
                          it), roomy max_tokens. All 7 Groq(api_key=...) sites +
                          background workers now call make_llm_client().
  - app/routers/master_table.py — same helper.
  - requirements.txt    — added `anthropic>=0.40.0` (installed 1.2.0 locally).
TESTED WITHOUT A KEY (scratchpad/test_llm_wiring.py, dummy key + faked call):
routes to claude-sonnet-5, no temperature, system split, Groq fallback intact,
no-key->None, full app imports (75 routes). NOTHING deployed; prod still on Groq.

TO GO LIVE (needs the user's ANTHROPIC_API_KEY — buy $20 credit at
console.anthropic.com, generate sk-ant-... , ~₹2/quote at their volume):
  1. add ANTHROPIC_API_KEY=sk-ant-... to /etc/quotegen/env on the server
  2. copy the 3 changed .py files + requirements to /opt/quotegen
  3. /opt/quotegen/venv/bin/pip install anthropic   (server venv)
  4. sudo systemctl restart quotegen
  5. MEASURE: replay real BOQs and EYEBALL the picks for correctness (not just
     that it runs) — see the replay method below; then confirm the "429 verify
     skipped" log lines stop. To revert: unset ANTHROPIC_API_KEY, restart.
Model is env-swappable: ANTHROPIC_MODEL=claude-haiku-4-5 for ~5x cheaper if
Sonnet feels pricey; keep Sonnet for best judgment. Left Sonnet on default
thinking (best judgment, a bit more cost) — tune to lower effort after measuring.
CHEAPEST ALTERNATIVE if they don't want Claude: paid Groq "Dev" tier (billing
at console.groq.com/settings/billing) removes the 200k cap with ZERO code change
— fixes the rate-limit half but keeps the weaker model.

### Diagnosis findings (measured on prod, read-only) — report artifact:
https://claude.ai/code/artifact/cb676316-ab87-47e7-86e9-00e3da0cbf2a
- not_found is ~75% GENUINELY ABSENT (furniture, katori/sigri/tiffin, galvanized
  ware — out of the 14-catalogue range, correct). ~15-20% matcher-miss.
- The 14 loaded catalogues ARE the intended master by design (user confirmed);
  the ~120 files in E:\niewttdt\Master\ are raw archive, NOT to be imported.
- Find-panel SUGGESTIONS already good (~90% surface the right product) — nothing
  to improve. Name-fallback auto-match TESTED and REJECTED (traded blanks for
  wrong picks: "Wok"->Soup Station). See memory [[name-fallback-unsafe]].

### Master-data health audit (prod, 15,938 products)
Commercially solid: no-price 170 (1%), no-HSN 5, no-cost 141 (1%), GST=0 15,
no-brand 0. GAP: 9,453 (59%) have NO IMAGE (BONNA all 7,488, ARIANE 1,152) —
USER SAID LEAVE IMAGES AS-IS, no action. Hygiene: 591 dirty names (newline/
double-space, mostly NILKAMAL 298 + PM KITCHEN 191), 323 rows in 142 dup-name
groups (mostly legit — same name, diff model code).

### Dirty-name cleanup — TESTED on copy, BACKED UP, NOT APPLIED to prod
Whitespace-only normalise (scratchpad/master_clean.py). On an isolated copy:
591/591 cleaned, 0 unsafe (safety assert: non-whitespace chars must be
identical), 0 rows lost. BACKUP made: /srv/quotegen-data/backups/
quotations.pre-nameclean.bak (verified, 15,938 rows). PROD WRITE WAS BLOCKED by
the auto-mode classifier — apply needs the user to allow the write or run it.
Honest note: FTS already tokenises whitespace, so this mainly helps DISPLAY +
exact-name paths, not FTS matching. Modest win.

### REUSABLE TEST METHOD (how we measured everything, isolated from prod)
Real client BOQs are saved on the server: /srv/quotegen-data/boq_sources/*.xls[x]
(10 files). To replay one through the REAL matcher on an ISOLATED copy without
touching prod: cp prod db + semantic_index.npz to /tmp/qg_baseline; cp -r
/opt/quotegen/app to /tmp/qg_app (patch there, clear __pycache__); run with
/opt/quotegen/venv, DATA_DIR=/tmp/qg_baseline APP_DIR=/tmp/qg_app, monkeypatch
app.routers.quotations._llm_chat -> "" to neutralise LLM, call
_resolve_master_matches(conn,[items],[],["3star"],object(),llm_verify=False).
NEVER import the app against the live prod DB — import runs init_db() (db.py:117)
+ _bootstrap_admin() (auth.py:43). Scripts now carried in the repo at
scripts/diagnostics/ (see its README.md): boq_rerun.py (THE real-BOQ replay),
baseline_harness.py, suggest_quality.py, master_clean.py, master_audit.sql, etc.
LESSON: judge a matcher change by CORRECTNESS (eyeball request->pick), never by
not_found count alone — the name-fallback dropped the count but added wrong picks.

### Access / infra state
SSH works: `ssh -o BatchMode=yes melange@192.168.0.146` (key at ~/.ssh on THIS
machine). Tailscale addr 100.94.230.77 works off-LAN (use it instead of
192.168.0.x when not on office wifi). Prod: quotegen active, /health 200, app
/opt/quotegen, db /srv/quotegen-data/quotations.db (15,938 products).
git: branch restructure-auth-smart-import, 57+ commits ahead of origin, still
UNPUSHED. The Sonnet integration is UNCOMMITTED on top of that — carry the
FOLDER (not just git) to keep it, or commit it first.

## Session 2026-08-31 — NEW MACHINE, local server revived, SERVER ACCESS RESTORED

UPDATE (end of session): SSH access to the office server is BACK. User ran
the append-key command with the correct melange password, so this machine's
key (C:\Users\SMI\.ssh\id_ed25519) is now in melange@192.168.0.146's
authorized_keys. Verified: `ssh -o BatchMode=yes melange@192.168.0.146`
connects with NO password. Deploy path is open again.
Prod verified live this session: service `quotegen` active, /health 200, app
/opt/quotegen, prod db /srv/quotegen-data/quotations.db (29.9 MB, modified
during the session — it's live). PROD PRODUCT COUNT IS 15,938, not the 51,938
this doc claims below (that number is stale). The shipped prod semantic index
(15,450 ids) matches THIS prod db closely — it was only mismatched vs the
small local dev copy. Everything below about "SERVER ACCESS LOST" is now
resolved; kept for the record of how it was fixed.



Whole project was copied from the old PC (user `itzan`, drive `E:\rtk-bin`)
to this one (user `SMI`, drive `E:\niewttdt\rtk-bin`). This session was spent
getting local dev working again on the new box. NOTHING deployed — we can't
reach the server (see below). No commits; git tree still `restructure-auth-
smart-import`, 57 ahead of origin, unpushed, unchanged by this session.

THE DOC BELOW IS STALE. It is dated 2026-08-10 but there are 136 commits
after it (through 2026-08-26) that were never written up. Trust the code over
this doc. Verified-still-open items from the old backlog:
  - `.xls/.xlsx` price columns still don't parse into boq_price (needs one of
    the actual failing files; an export WE generate round-trips fine). The
    app now routes around it — price-less BOQs fall back to SMI/company
    format (a124596, c2745c6).
  - asList() guards only 2 of the many array-assuming loaders in app.js; the
    rest still throw the whole page down on a lapsed session.
Superseded, do NOT act on: the "400-row FTS pool cap" — _line_pool is now
LIMIT 220 by design, compensated by brand/file preference pulls + a semantic
embedding layer. The in-code comments still say "400"; that text is stale,
the logic is not.

### Machine migration — paths fixed (in-repo, safe)
Every launcher/script still pointed at the old `E:\rtk-bin`. Repointed to
`E:\niewttdt\rtk-bin`:
  - run_local.ps1 (Set-Location)
  - scripts/attribute_audit.py, scripts/variant_coverage_audit.py (sys.path)
  - scripts/debug/{fix_gst,test_db,test_lookup}.py (sqlite3 path)
  - .claude/launch.json — BUT the preview launcher reads the SESSION ROOT,
    which is now E:\niewttdt (one level above rtk-bin). Created a NEW
    E:\niewttdt\.claude\launch.json for it; the rtk-bin one is now a leftover.
TRAP: `sed -i` silently no-ops on backslash Windows-path patterns in this
shell (reports success, changes nothing). Forward-slash paths were fine.
Edit backslash paths with a file edit, not sed — same family as the existing
"RTK proxy corrupts piped grep" warning.

### Python + venv revived
This machine had NO Python/Node/sqlite3 (the python.exe on PATH is the MS
Store stub). User installed Python 3.10.11 to
C:\Users\SMI\AppData\Local\Programs\Python\Python310.
The existing venv is intact (112 pkgs, cp310 wheels) — only its pyvenv.cfg
pointed at the old `itzan` home. Repointed `home` + `version=3.10.11` and the
venv came back without reinstalling anything. Verified: 17/17 pinned deps
import, SQLite FTS5 available (3.40.1), app.main imports (75 routes).
fastembed was MISSING from the venv (import is lazy + swallowed, so the app
still boots, semantic layer just silently off) — `pip install fastembed`
(0.8.0 + onnxruntime 1.23.2, bge-small model downloaded).
RUN LOCAL: preview config "quotation-poc" (port 8000), or run_local.ps1.
Server confirmed up: /health 200, login renders, assets v144/v154.

### Semantic index rebuilt for the LOCAL db
data/semantic_index.npz shipped in the copy was built against PRODUCTION
(15,450 ids, range 71672–87121) — ZERO overlap with the local db's ids
(6,134 rows, range 2192–12227), so semantic matching contributed nothing
locally (no error, silent). Backed up the prod artifact to
data/semantic_index.prod.npz, then rebuilt against local: 6,134 vecs in 115s,
6,134/6,134 ids resolve. Spot-checked sane ("water jug" -> KMW water jugs,
"chafing dish" -> AMAYDA chafing dishes). _load_index() reloads on mtime, no
restart needed.

### LOCAL DB is a small DEV copy, not prod
6,134 products (prod = 51,938), 131 quotations, match_corrections EMPTY
(prod had 66 real rows). Local matching WILL diverge from prod on anything
catalogue-dependent. Biggest real quote for testing: QT-20260728-132711,
704 items, ARIANE tableware, has_boq_pricing false.
Admin accounts in the LOCAL db: #1 admin@local, #12
himanshukumarlenka00@gmail.com. Passwords are bcrypt (unrecoverable) but a
local dev db can have a hash written in directly if login is needed. Employee
test login unchanged: emplyoee@melangeindia.in / 12345678.

### SERVER ACCESS LOST — this is the blocker
The box is UP and reachable from this PC (LAN 192.168.0.146:8443/health 200,
Tailscale 100.94.230.77:8443/health 200; crm-server active on the tailnet).
Host key still SHA256:4s71FMUiP+1JG7gRa455kwPi7Rhf3g48l2FzNfdgCUE — same box,
not a rebuild. BUT no SSH: this PC has no ~/.ssh key at all (keys live in the
Windows profile, C:\Users\itzan\.ssh on the old machine — NOT in the copied
E:\ folder, so they didn't come across). Server offers publickey,password;
`Permission denied (publickey,password)`. The password the user has is
rejected by the server (do NOT keep retrying — fail2ban). Tailscale SSH is
NOT enabled (plain sshd only). No other account exists on the box.
Old deploy history proves the old PC had a working key (BatchMode=yes calls
succeeded). So the key isn't lost, it's on the itzan PC.
TO RECONNECT (any one):
  1. Copy C:\Users\itzan\.ssh\id_ed25519 + .pub from the OLD pc into
     C:\Users\SMI\.ssh\ — instant, no password, nothing changes on server.
  2. Have someone who can already get in add THIS machine's new public key
     (generated this session, at C:\Users\SMI\.ssh\id_ed25519.pub:
     ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGWXHFrLoxm1xWHM9ceBQfwYBznVGQAdFn18ESj9v5XU SMI-quotegen-DESKTOP-BAJ97JQ)
     to melange's ~/.ssh/authorized_keys — or give the real password.
  3. Console access to the box, add the key there.
Server layout (from deploy history): app /opt/quotegen, prod db
/srv/quotegen-data/quotations.db, service `quotegen` (sudo systemctl restart),
venv /opt/quotegen/venv. Deploy = tar-pipe to /opt/quotegen, restart if .py
changed. ASK BEFORE EVERY SERVER-CHANGING COMMAND still stands.

### In flight when session paused: Current Quote (#sec-result) density
User picked "table density / readability" for the Current Quote screen.
Problem is real and data-driven: renderResult renders up to 15 cols (20 with
Cost&margin on) and scrolls sideways. The widest cols are DUPLICATE data —
`product` already contains BRAND-MODEL- prefix + the spec text, sitting right
beside the separate Brand, Model and Specification cols. Mocked (visualize) a
15->8 col layout: fold brand/model/HSN into the product cell as a meta line,
strip the redundant prefix from the display name (DISPLAY ONLY — leave the
`product` field untouched so exports/corrections/matcher are unaffected; fall
back to full string when the prefix doesn't cleanly match), trim spec to the
non-redundant tail, collapse 3star/4star/Price into one price cell, merge
GST%+GST Val. Awaiting user's yes on direction before writing code.
renderItemRow is at app.js:3118 (140 lines), renderResult at :3860 (184
lines), called from 20+ sites; colSpan/trailingCols/labelColspan math must
stay in sync or the totals footer misaligns (a gap was measured this way
before). Check print/PDF too.
Also found, not yet fixed: index.html:404 not-found-banner has TWO style
attrs (2nd ignored -> no bottom margin). And the whole items table still uses
emoji glyphs (Switch/Add Image/size-note/history/tier ticks/etc.) against the
standing "emoji render as tofu on Windows, use inline SVG" rule — the bottom
action buttons already use SVG, the table doesn't.

## Session 2026-08-10 (24 commits, f6878c5..8f33e05, all deployed + pushed)

### "tray" solved (540aafd) — 3 variants -> 465
NOT the glued tokens, NOT the 400-row pool. search_catalog scores a
model-number hit 1000 and then keeps only rows above 0.6 x best. The
whole-term model test was a bare substring check, and plenty of "models" in
the master are descriptive text:
    DCTC 1014 (PP) - PP Tray        LV LID HANGER
So "tray" matched those as a SUBSTRING, scored 1000, and the cutoff landed at
621 — two points above the ~619 a real name match can reach
(600 - len(name) + 30 price + 5 image). All 473 rows in the pool actually
named "...Tray..." were discarded in favour of 3 whose model text said tray.
Fix: the whole-term test now needs a DIGIT in the term. mtoks already handles
letter+digit codes, and an inline code in a sentence still wins.
MEASURED AFTER DEPLOY: tray 3 -> 465, hanger -> 105 (silently broken the same
way), dustbin 50, kettle 79 unchanged, model codes still resolve, 60-107ms.
test_word_vs_model.py, mutation-checked (old condition -> exactly 1 row).

HOW IT WAS FOUND, because guessing failed twice: I blamed _UNITS, then the
price filter, both wrong. What settled it was wrapping the module-level
_merge_glued to CAPTURE the pool search_catalog actually received — 528 rows,
473 passing _covered. That ruled out the pool and the matching and left the
cutoff. When a nested function hides the state you need, monkeypatch the
module-level thing it calls; do not re-implement its query.

### Margin: gate on COST, not on the file's prices (ea2c587)
"Margin analysis shows nothing" was real, not a preference. Cost/Profit hung
off has_boq_pricing, true only when the UPLOADED FILE carries a price column
— and on this user's quotations it never parses. Verified in their own Chrome
on QT-20260810-172108: 17 items, 16 with cost, **0 with boq_price**, no Profit
column rendered. The master's cost was sitting right there (254.15 against a
price of 270.30).
Now gated on cost. _strip_cost removes cost from an employee payload
entirely, so its presence IS the permission check. Added Cost and Margin %
beside Profit, with totals, tracking live as prices are edited. Live figures
match the old deleted table exactly: ₹2,016 profit, 6.0%, ₹33,549.
TIGHTENED by b0313f6 — cost alone was too loose. It put purchase cost on
every quotation, including one typed in plain English, which is the wrong
thing to have on screen in front of a client. The gate is now INTENT **and**
data: `q.show_margin && items.some(cost > 0)`. show_margin is set only by the
Margin analysis button, persisted without an underscore so reopening the
saved quote keeps the view. Typed -> no margin; Generate from file -> no
margin; Margin analysis -> margin.
COROLLARY: the deleted analysis table was never comparing what we QUOTED
against cost — its "SOLD @" fell back to the master price. It was showing
master price vs cost all along, the same figure these columns show.

### One button per input (b782254, 1109817)
A single Generate had to guess, and with text typed AND a file dropped it
silently used the file. Now "Generate from this list" sits under the
textarea and "Check what we stock / Margin analysis / Generate from file"
under the drop zone. No precedence rule, nothing discarded, generateFromCard
deleted. .gen-split stretches so each half's margin-top:auto lands its row on
one baseline.
THEN THE CARD WENT LOPSIDED, and the cause is worth remembering:
    #sec-generate .card > div:has(#gen-btn) { display: flex; ... }
dated from when Generate was a direct child of the card. Moving the button
into .gen-split made that selector match THE SPLIT, and at id+class it beat
".gen-split { display:grid }" — the grid became a flex row and the typed half
floated right. A :has() selector keyed on a moving element re-targets itself
when that element moves, silently; the rule it overrides just stops applying.
Also fixed: Chrome restores a textarea's value on reload WITHOUT firing
`input`, so the counter read "0 / 8000" beside 777 real characters. Synced at
DOMContentLoaded.

### Per-half statuses (4b5855d)
#gen-status and #gen-boq-status both sat full width at the foot of the card,
so running one path then the other stacked two anonymous green "Quotation
ready" banners (10 items above 8 items) with nothing saying which input made
which. Each now lives inside the half that produced it, side by side,
collapsing via :empty when empty. #boq-coverage stays full width — it renders
a table, not a status line.

### The exported quotation is now a LIVE sheet (8f33e05)
Every number in the download was frozen, so changing a price in Excel left
AMOUNT, GST AMOUNT and the three totals stale and the file had to be
regenerated. In build_xls_from_template (the template path behind Download,
NOT build_xls_minimal) the derived cells are formulas:
    K21:K{last}  =C*J        AMOUNT    = QTY x PRICE/PC
    M21:M{last}  =K*L        GST AMOUNT= AMOUNT x GST%   (L holds .18, fmt 0%)
    TOTAL =SUM(K..)   GST VALUE =SUM(M..)   GRAND =K(total)+K(gst)
Ranges include the freight row, which sits directly under the items. QTY,
PRICE/PC and GST% are the only typed-in numbers left.

DO NOT "SIMPLIFY" THIS BACK TO PLAIN VALUES. The old code avoided formulas
on purpose — "some Excel installs sit in manual-calc mode and show formula
cells blank until F9". That is a real failure and the reason this is safe is
one line: `wb.calculation.fullCalcOnLoad = True`. Remove it and the export
ships blank cells to those users.

Verified on the live server against the user's own QT-20260810-182711:
40 formula cells, inputs still plain (C=4, J=270.3, L=0.18), fullCalcOnLoad
true. Re-upload still works — parse_boq_excel reads product/qty/price and
never AMOUNT, detect_file_type keys on header NAMES not values, so a
formula-bearing export parses 4/4 rows and is still called a quotation at
0.8 confidence. The AMOUNT fallback I expected to need was unnecessary;
checking beat assuming.

### Open, and worth doing next
- The user's .xls/.xlsx price columns do not parse into boq_price. Margin no
  longer depends on it, but "what we quoted then vs what we'd charge now" is
  unavailable until it does. NEEDS ONE OF THE ACTUAL FILES to diagnose.
  NARROWED 2026-08-10: an export WE generate round-trips perfectly —
  parse_boq_excel reads its product/qty/price back correctly. So the fault is
  in the specific files being uploaded, not the parser in general.
- The 400-row FTS pool cap (tray 1428 hits -> 400, mixing bowl 4651 -> 400).
  Untouched; needs timings before raising it for the Switch/Find path.
- ANSWERED by b0313f6: the Margin analysis button stays and now earns its
  place — it is the only thing that turns the Cost / Profit / Margin columns
  on. Generate from file no longer shows them.


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
the page. `has_boq_pricing` in particular gates a lot of already-built UI —
and it is FALSE on this user's uploads, which is what made margin look
missing when the data was there all along (ea2c587).
A fourth instance, different flavour: I twice guessed at a cause instead of
measuring (_UNITS, then the price filter, for "tray"). Capture the real
state — monkeypatch the module-level function the nested one calls — rather
than re-implementing its query and trusting the replica.

### Margin analysis: deleted, not moved (66d6d47, net -314 lines)
The button now runs the same generation and LANDS ON CURRENT QUOTE, flashing
#foot-profit so it is obvious what you were sent to look at. A file with no
prices toasts instead of showing an empty profit view.
GONE: /api/analyse-quotation, /api/analyse-quotation/export,
build_margin_analysis, renderQuotationAnalysis, downloadAnalysisXlsx, all
.ma-* CSS. Both endpoints verified 404 in prod.
Lost with it: per-line COST, MARGIN %, and the costed / no-cost / unmatched
counts — the user chose that over keeping a second surface.
SUPERSEDED SAME SESSION: cost and margin % came straight back as COLUMNS in
Current Quote (ea2c587, top of file), which is where the note below predicted
they belonged. Only the three counts are genuinely gone.

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

### Still open (mid-session list; see "Open, and worth doing next" at the top)
- "tray" scoring — SOLVED later the same session, see 540aafd at the top.
- Other loaders still assume array payloads (see the bug-class note above).
- Margin Analysis page is now lists-only, and its upload is gone. Its
  Approved / BOQ-priced Drafts sections filter on has_boq_pricing — which is
  FALSE on this user's uploads (their price columns do not parse), so those
  two lists stay empty until that parsing is fixed.

### Cache-busting state at end of session
index.html references main.css?v=127 and app.js?v=131 (unchanged by
8f33e05 — that commit touched app/export.py only, so it needed a service
restart rather than a cache bump). index.html itself is
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
