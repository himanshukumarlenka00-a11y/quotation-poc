# Search & Matching — Audit Findings (2026-08-05)

Method: 270 real products sampled from the live 51,938-row master table;
~1,100 realistic query variants generated per style; each run through the
actual resolver (`_resolve_master_matches`, deterministic layers only, no
LLM). Strict scoring: only the exact sampled product counts as a hit — so
percentages UNDERSTATE quality when many legitimate near-twins exist.

## Accuracy by query style

| Style                      | Hit rate | Verdict |
|----------------------------|---------:|---------|
| hyphen→space in model      | 90%      | fine |
| model code (full, exact)   | 84%      | fine |
| model lowercase/spaced     | 79%      | fine |
| exact core name            | 48%      | ambiguity-dominated (see data section) |
| lowercase / & → and        | 48%      | tracks exact-core |
| unit spacing (8cm vs 8 cm) | 44%      | small sample, acceptable |
| plurals ("cups")           | 36%      | **BUG 2** |
| brand-stripped name        | 36%      | ambiguity-dominated |
| word order reversed        | 33%      | acceptable-ish |
| model code partial prefix  | 23%      | ambiguity (shared prefixes) |
| two-word fragment          | 18%      | expected (fragment ≠ identity) |
| unit stripped from name    | 12%      | small sample |
| **typos (1 letter off)**   | **0%**   | **BUG/GAP 5 — zero fuzz tolerance** |

## Confirmed CODE defects (fix order)

1. **Exact-name equality is not supreme.** Typing a product's full exact
   name can return a sibling variant (query `Moove Plate 22*` → returned
   `Moove Plate`). Fix: normalized full-string equality (also vs name minus
   brand/model prefix) scores above everything (e.g. 2000). ~1k tokens.
2. **Plural/singular blind spot in FTS.** FTS uses prefix matching, so
   "cups" can NEVER reach rows named "cup" — a kettle-tray whose SPEC
   mentions "cups" won instead (real result: `cups` → KETTLETRAY). Fix:
   expand each FTS word with stripped-s / +s variants. ~1k tokens.
3. **Digit-only model codes never model-ranked.** `1052418` (AndyManhart
   style) skips the model-token path, which requires letters+digits. Fix:
   accept all-digit tokens len>=5 as model tokens. ~0.5k.
4. **Head-noun veto misfires on size tokens.** `snack trolley
   4compartments`, `shaker 650mls`: last "word" is a size token, so the
   semantic-guess sanity check vetoes correct matches. Fix: head-noun
   selection skips tokens containing digits. ~0.5k.
5. **Typos: nothing matches, ever** (`coktail table`, `Kuahya`, `ASTRAY`).
   Currently by design (no fuzzy layer). Options, cheapest first:
   a) on zero-FTS-hit, retry with per-word edit-distance-1 expansion
      against a vocabulary of catalog words (~4-6k tokens, deterministic);
   b) the roadmapped semantic/vector search (memory:
      llm-learning-caching-roadmap) — solves typos + synonyms together.

## DATA-QUALITY findings (bigger lever than code!)

The 52k import wave introduced heavy duplication — ambiguity now dominates
mismatch complaints, and no ranking tweak can fix data twins:

- **3,538 exact-duplicate product names** (same normalized name, multiple
  rows). Example: `Table Knife` exists in Costa Nova AND Bonna files;
  `FLOOR ASHTRAY` several times within one brand.
- **Same model code in 2 catalogues** with different names: HM9036 (24
  rows/2 files), HM9405B, HM8236… — looks like the same supplier list
  imported twice under different file names. Candidates: the two MELANGE
  hotel-supply files, AMAYDA files.
- **Junk rows imported as products**: `'` (backtick), `Chair`, `FRAME`,
  `IRON` ×2, `SAFE` — header/fragment rows that slipped a parse.
- Same physical item worded twice: `Cocktail Table with LED` vs
  `LED cocktail table` (different rows, both priced).

Suggested cleanup pass (admin decisions needed, not auto-delete):
  1) report of duplicate-model groups across files → admin picks canonical
  file, deletes the other import; 2) delete the 6 junk rows; 3) merge
  double-worded items. A "dedupe report" screen is the honest tool here.

## Not bugs (leave alone)

- Fragment queries ("Coffee Cup", "RACK") matching *a* coffee cup/rack —
  correct behaviour at this catalog size; the variant Switch button is the
  designed answer. Ambiguity ≠ inaccuracy.
- Partial model prefixes matching a sibling (RBLRIT → RBLRIT01KT).

## Fix batch 1 — SHIPPED 2026-08-05 (commit pending)

Fixes 1-4 implemented and re-audited: model codes 84->89%, plurals 36->43%,
unit-spacing 44->83%, model-spaced 79->85%, unit-stripped 12->50%,
brand-stripped 36->40%. exact-core stuck ~50% — capped by duplicate-name
data, not ranking. Typo layer also shipped same day: unknown query words spell-corrected to
the catalogue's own vocabulary at edit-distance 1 (deterministic, known
words never touched). Typos 0% -> 22% == the plain-fragment baseline, i.e.
the typo penalty is gone; the residual is ordinary ambiguity.

## Recommended sequence

1. Code fixes 1-4 (one batch, ~4-5k tokens, re-run audit to prove).
2. Dedupe report screen + junk-row cleanup (admin-driven, ~8-10k).
3. Typo layer: edit-distance-1 retry (4-6k) — or fold into the semantic
   search roadmap item when it lands.

Audit scripts: /tmp/search_audit.py, /tmp/search_audit2.py on the server
(rerunnable read-only; regenerate from this session's scratchpad if lost).

## Cleanup executed 2026-08-17 (with user approval per pair)

Deleted: 6 junk rows; KUTAHYA COPY (799, fully contained in DONE); WALTHR
double-space copy (966, base prices — user chose to keep the +14.12%
repriced copy); BONNA 2024-25 PART 3 (3,468, stale year, 179 unique items
accepted as lost — re-importable from the stored file). Master 51,938 ->
46,699. Post-cleanup audit: model-spaced 96%, unit-spacing 92%, exact-core
59%, dup-name groups 3,538 -> 1,555, junk 0. Remaining ambiguity is mostly
color variants sharing a model code (GOP231-PNK vs -CHM) — human Switch
territory. Remaining cross-file model overlaps are small (6-12 rows) and
look like genuinely shared items, not double imports.

## Round 4 — 2026-08-18 (fair-metric audit + vocabulary pollution fix)

Strict row-identity scoring was penalising correct answers (size twins like
BLHTON10KS vs 14KS share a name; mixed-case brands defeated the core stripper).
Re-scored fairly: result must contain everything the query asked for
(plural/prefix tolerant), model queries must match the returned model.

| pass | fair accuracy |
|---|---|
| baseline (strict scoring) | 46% (misleading) |
| fair scoring, same matcher | 96% |
| + name cleanup + typo-pollution fix | **98% (650/661)** |

Fixes this round:
1. **Data**: 1,787 product names contained embedded newlines / whitespace runs
   (e.g. "\nWAFFLE MACHINES") — could never exact-match. Cleaned + FTS rebuilt
   (scripts/clean_names.py, idempotent).
2. **Matcher**: typo layer refused to correct words "known" to the catalogue
   even when known only from 1-2 misspelt rows ('coktail' from two dirty
   names blocked correction of every Cocktail query). Now corrects words with
   ≤2 occurrences when an edit-distance-1 neighbour is ≥25× more frequent.

Remaining 11 failures: genuinely ambiguous fragments ("Size Crock", "Andy 10")
where multiple catalogue rows are equally valid answers.

## Round 5 — 2026-08-18 (variant starvation)

"food warmer" showed only 5 switchable variants of 27: products named exactly
"...FOOD WARMER" scored 2000 (exact-name supremacy) and the switcher's
same-tier cutoff (60% of top) discarded every other family member. Fixed by
capping the cutoff at 420 — the automatic pick is untouched, the alternatives
list always keeps full-coverage matches. Also added compound-word merging
("pop corn" -> POPCORN when the joined form exists and the phrase doesn't).

Systematic check: scripts/variant_coverage_audit.py compares, for the ~140
most common family terms, catalogue family size vs resolver variant count.
Post-fix: no product-phrase family under 50% coverage. Remaining flags are
(a) umbrella words (7,900 "plate" rows — pool caps trim these by design) and
(b) brand+model-prefix queries where the hard model constraint intentionally
returns one row. Re-run after any scoring change.

Round 5 addendum: the "brand+model-prefix shows 1 of N" flags (melange smle,
cambro mpsk) were audit artifacts — the bigram generator sliced letter-only
fragments out of SKU codes and counted unrelated products as one family.
Verified real code-prefix queries list their full family already (SMLEDCB ->
7/7, MPSK30 -> 24/24). No matcher change needed; treat letter-only fragments
of alphanumeric codes as noise when reading the coverage audit.

## Round 6 — 2026-08-18 (attribute + compound sweep)

New audit (scripts/attribute_audit.py): 290 attribute+noun queries generated
from real catalogue pairs (steel kettle, copper mug, wooden tray...) in both
word orders, plus 100 compound-split probes.

Findings: word-form blindness — 'wood' did not cover WOODEN, 'rect' did not
cover RECTANGULAR — cost 8/290. Fixed in _covered(): a 4+ char request word
now also matches name words extending it by up to 4 letters. Post-fix:
288/290 correct; the 2 flags are counter artifacts ('pot' inside SPOT-Glass).
Compound layer: 96/100 resolve, 4 flags are checker artifacts (correct answers
in singular form). Full fair audit after the change: 96%, no regression.
