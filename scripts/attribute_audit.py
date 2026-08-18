"""Attribute-accuracy sweep: for real attribute+noun pairs in the catalogue
(steel kettle, copper mug, wooden tray...), ask the matcher both word orders
and check the TOP pick actually carries the attribute — the 'copper kettle
for a stainless request' class. Plus a compound-word sweep for more
popcorn-style split/joined mismatches."""
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
low = [n.lower() for n in names]

ATTRS = ["stainless", "steel", "copper", "wooden", "wood", "black", "white",
         "glass", "plastic", "ceramic", "gold", "silver", "bamboo", "marble",
         "acrylic", "brass", "electric", "round", "square", "rect"]
NOUNS = ["kettle", "tray", "bowl", "plate", "spoon", "fork", "knife", "mug",
         "jar", "bucket", "stand", "board", "pot", "cup", "basket", "dispenser",
         "glass", "shaker", "tong", "ladle", "dustbin", "hanger", "mirror",
         "lamp", "trolley", "warmer", "chafing"]

def top(term):
    try:
        m, _ = _resolve_master_matches(conn, [{"product": term, "qty": 1}], [], ["3star"], None, prompt="")
        return (m[0].get("product") or "") if m else ""
    except Exception:
        return ""

fails, tested = [], 0
for a in ATTRS:
    for nnoun in NOUNS:
        if a == nnoun:
            continue
        family = sum(1 for l in low if a in l and nnoun in l)
        if family < 3:
            continue
        for q in (f"{a} {nnoun}", f"{nnoun} {a}"):
            tested += 1
            got = top(q).lower()
            ok = bool(got) and a in got and (nnoun.rstrip("s") in got.replace(" ", "") or nnoun in got)
            if not ok:
                fails.append((q, top(q)[:48] or "NOT FOUND", family))

print(f"attribute+noun: {tested} queries, {len(fails)} misses")
for q, got, fam in fails[:25]:
    print(f"  {q:24} -> {got:50} ({fam} valid rows exist)")

# Compound sweep: joined vocab words that split into two real words with no
# spaced phrase in the catalogue (popcorn class) — do split queries resolve?
words = Counter(w for l in low for w in re.findall(r"[a-z]{3,}", l))
blob = "\n".join(low)
comp_fails = comp_ok = 0
checked = []
for j, cnt in words.items():
    if cnt < 3 or len(j) < 7:
        continue
    for cut in range(3, len(j) - 2):
        a, b = j[:cut], j[cut:]
        if words.get(a, 0) >= 5 and words.get(b, 0) >= 5 and f"{a} {b}" not in blob:
            q = f"{a} {b}"
            got = top(q).lower()
            if j in got.replace(" ", ""):
                comp_ok += 1
            else:
                comp_fails += 1
                checked.append((q, j, top(q)[:40] or "NOT FOUND"))
            break

print(f"\ncompound splits: {comp_ok} resolve to the joined product, {comp_fails} miss")
for q, j, got in checked[:15]:
    print(f"  '{q}' (should find {j}) -> {got}")
conn.close()
