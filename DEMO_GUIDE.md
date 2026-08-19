# QuoteGen — Demo Guide & Feature Checklist

*Updated: 19 Aug 2026 · everything below is live on the server (v136)*

---

## 1. Ready-made demo prompts (all verified against live data)

### Brand context — "it understands supplier context"
```
walther items
dustbin 10
minibar 20
kettle 15
safe 50
```
→ All four lines auto-pick WALTHR. The context line disappears instead of becoming a junk row.

### Catalogue context — "it knows your catalogues, not just brands"
```
only nilkamal products
plastic crate 20
dustbin 5
```
→ Crate from NILKAMAL. The dustbin honestly falls back to the best overall match (Nilkamal makes none) — no silent blanks.

### Typos & joined words
```
pop corn machine 2
coktail shaker 6
ice cream scoop 12
```
→ "pop corn" finds the Popcorn Machine; "coktail" self-corrects.

### Sizes in any notation
```
kettle 1 litre 9 qty
scraper 5 20
chafing dish 8
```
→ "1 litre" = "1L" = "1.0L" — same kettle. "scraper 5" finds the 5-inch even without the `"` mark.

### Model codes are surgical
```
WBS001-SS 4
JJ004-SS 6
```
→ Exact BARKRAFT products by code. Brand hints can never override a typed code.

### Bigger themed lists (also verified)
- **Hotel room**: `walther items` + kettle / minibar / safe / hair dryer / mirror / dustbin
- **Banquet**: chafing dish 12 / soup station 2 / juice dispenser 4 / food warmer 6 / buffet stand 10 / serving tawa 8
- **Bar**: cocktail shaker 6 / bar spoon 12 / jigger 12 / ice bucket 8 / wine glass 48 / muddler 6
- **Kitchen**: fry pan 28cm 6 / sauce pan 6 / stock pot 4 / chef knife 8 / cutting board 10

---

## 2. Input syntax cheat-sheet

| You type | Meaning |
|---|---|
| `kettle 1000` / `kettle 1000 qty` / `kettle qty 50` / `kettle /50 qty` / `kettle x 50` / `1000 kettle` | quantity |
| `kettle 1000W`, `kettle 450ml`, `plate 27cm` | specification (narrows to that spec) |
| `kettle under 1000` | price cap |
| `scraper 5` | size hint (finds 5", 5cm, 5qt…) |
| `PRODUCT [MODEL-CODE] 4` | exact model, qty 4 |
| one product per **line** (or comma-separated) | separate items — `/` never splits items |
| brand/catalogue name anywhere | prefers that source; others stay in Switch |

---

## 3. What shipped recently — where to see it

| Feature | Where |
|---|---|
| Batch wise / Category wise submenu | Sidebar → Master Catalogue |
| AI auto-categorization (70%+ done, finishing in background) | Master Catalogue → Category wise |
| Select → move / recategorise / delete rows | Batch wise → ☑ Select on a batch |
| ＋ New category with in-dialog product picker | Category wise → header button |
| Per-folder filter search | Any expanded batch/category |
| Cost & Margin columns (admin only, toggleable) | Master table + Current Quote + quote footer |
| Cost field + "save to Master Catalogue" (with batch picker) | Current Quote → Enter a product manually |
| Styled confirm dialogs (no browser popups) | Every delete action |
| Dedupe: fast scan + 👁 side-by-side pair preview | Sidebar → Dedupe |
| Switch panel: filter-as-you-type + whole-catalogue search | Any quote row → Switch |
| Switch on single-match rows too | Any matched row |
| Refresh prices from master (now instant) | ↺ button on the quotation banner |
| Auto-update pill ("new version — click to refresh") | Appears by itself after deploys |
| Animated sidebar icons + Generate-page artwork | Sidebar / Generate page |
| 90% density default, full-height sidebar | Everywhere |
| No SL number on packing/freight row | Exported Excel |

---

## 4. Search accuracy (audited)

- **96-98% fair accuracy** across ~660 queries in 10 phrasing styles, re-audited after every matcher change.
- Typos 100% · model codes ~100% · units 100% · reordered words ~98%.
- Audit tools (run after any matcher change): `scripts/variant_coverage_audit.py`, `scripts/attribute_audit.py`, plus the accuracy audit; findings history in `SEARCH_FINDINGS.md`.

---

## 5. Background / pending

- **Categorization job**: running on the server, ~70% done; limited by Groq free tier (200k tokens/day shared with the app). Groq Dev Tier (<$1) finishes it in ~1 hour — console.groq.com → Billing.
- **Cost-less catalogues** (show ₹0 cost / no margin): Atlantic Chef, LED Table Lamps, OPM ×2, Corby Hall (no selling prices either), COTELL, AMAYDA Stoneware. Fix = re-import with cost columns (WALTHR in-room already refilled).
- **Queued when wanted**: match/extraction caching (saves Groq quota, instant repeat quotes).

---

## 6. If something looks stale

The app updates itself: a purple **"⬆ new version — click to refresh"** pill appears in open tabs within ~4 minutes of a deploy. If a screen ever looks old, press **F5** once.
