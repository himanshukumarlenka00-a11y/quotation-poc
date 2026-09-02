#!/usr/bin/env python3
# READ-ONLY. Show the "dirty" product names (embedded newlines / multi-space)
# and exactly what a whitespace-normalise cleanup would produce.
# Product wording only. Opens prod DB strictly read-only.
import sqlite3, re, collections
conn = sqlite3.connect("file:/srv/quotegen-data/quotations.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def visible(s):
    return (s or "").replace("\n", "⏎").replace("\r", "⏎").replace("  ", "··")

rows = conn.execute("SELECT id, file_name, product FROM master_products "
                    "WHERE product LIKE '%'||char(10)||'%' OR product LIKE '%'||char(13)||'%' "
                    "OR product LIKE '%  %'").fetchall()

cat_newline = cat_space = cat_both = 0
no_change = 0
per_cat = collections.Counter()
examples = []
for r in rows:
    p = r["product"] or ""
    has_nl = ("\n" in p) or ("\r" in p)
    has_sp = "  " in p
    if has_nl and has_sp: cat_both += 1
    elif has_nl: cat_newline += 1
    elif has_sp: cat_space += 1
    per_cat[r["file_name"]] += 1
    c = clean(p)
    if c == p:
        no_change += 1        # already clean after normalise (edge)
    if len(examples) < 26 and c != p:
        examples.append((visible(p)[:52], c[:48]))

print("dirty product names: %d" % len(rows))
print("  embedded newline only : %d" % cat_newline)
print("  double-space only     : %d" % cat_space)
print("  both                  : %d" % cat_both)
print()
print("by catalogue (top):")
for cat, n in per_cat.most_common(8):
    print("  %5d  %s" % (n, cat))
print()
print("cleanup preview  (before  ->  after)   [ ⏎ = newline, ·· = double space ]")
print("-" * 76)
for b, a in examples:
    print("  %-52s -> %s" % (b, a))
conn.close()
