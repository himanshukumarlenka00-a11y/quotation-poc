"""Variant-starvation audit: for common product families, compare the number
of catalogue rows whose NAME contains every query word (ground truth) with the
resolver's variant count. A big gap = the switcher is hiding alternatives."""
import io
import re
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, "E:/rtk-bin/quotation-poc")
import app.db as appdb
appdb.DB_PATH = r"C:/Users/itzan/AppData/Local/Temp/claude/E--rtk-bin/a5c191e2-1787-498d-9c93-e9bb90bd3dd7/scratchpad/prod_quotations.db"
from app.db import get_db
from app.routers.quotations import _resolve_master_matches

conn = get_db()
names = [r[0] or "" for r in conn.execute("SELECT product FROM master_products")]

# Common single words and adjacent-word phrases from the catalogue itself
STOP = {"and", "with", "for", "the", "set", "pcs", "size", "small", "large",
        "medium", "big", "mini", "new", "done", "list", "price"}
words = Counter()
bigrams = Counter()
for n in names:
    toks = [w for w in re.findall(r"[a-z]{4,}", n.lower()) if w not in STOP]
    words.update(set(toks))
    for a, b in zip(toks, toks[1:]):
        if a != b:
            bigrams.update({f"{a} {b}": 1})

terms = [w for w, c in words.most_common(60) if c >= 30]
terms += [b for b, c in bigrams.most_common(80) if c >= 15]

def family_size(term):
    ws = term.split()
    return sum(1 for n in names if all(w in n.lower() for w in ws))

print(f"auditing {len(terms)} family terms…")
suspects = []
for t in terms:
    truth = family_size(t)
    if truth < 8:
        continue
    m, _ = _resolve_master_matches(conn, [{"product": t, "qty": 1}], [], ["3star"], None,
                                   prompt="", variant_cap=500)
    got = (m[0].get("_variants_total") or 0) if m else 0
    ratio = got / truth if truth else 1
    if ratio < 0.5:
        suspects.append((ratio, t, got, truth))

suspects.sort()
print(f"\n=== families where the switcher shows <50% of the catalogue ({len(suspects)}) ===")
print(f"{'term':26} {'shown':>6} {'in catalogue':>12}")
for ratio, t, got, truth in suspects[:25]:
    print(f"{t:26} {got:>6} {truth:>12}")
if not suspects:
    print("none — every common family surfaces at least half its members")
conn.close()
