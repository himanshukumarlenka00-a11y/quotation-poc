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

## Recommended sequence

1. Code fixes 1-4 (one batch, ~4-5k tokens, re-run audit to prove).
2. Dedupe report screen + junk-row cleanup (admin-driven, ~8-10k).
3. Typo layer: edit-distance-1 retry (4-6k) — or fold into the semantic
   search roadmap item when it lands.

Audit scripts: /tmp/search_audit.py, /tmp/search_audit2.py on the server
(rerunnable read-only; regenerate from this session's scratchpad if lost).
