#!/usr/bin/env python3
# Whitespace-normalise dirty product names. Runs against TARGET_DB (an
# ISOLATED COPY here — never prod). SAFETY: every change must be
# whitespace-only; if the non-whitespace characters would differ at all, the
# row is SKIPPED, never written. Reports before/after so the result is
# verifiable. Idempotent (re-running finds 0 dirty).
import os, sqlite3, re
DB = os.environ["TARGET_DB"]
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

def clean(s):    return re.sub(r"\s+", " ", s or "").strip()
def nowhite(s):  return re.sub(r"\s", "", s or "")

DIRTY = ("product LIKE '%'||char(10)||'%' OR product LIKE '%'||char(13)||'%' "
         "OR product LIKE '%  %'")

total_before = conn.execute("SELECT COUNT(*) FROM master_products").fetchone()[0]
rows = conn.execute("SELECT id, product FROM master_products WHERE "+DIRTY).fetchall()
dirty_before = len(rows)

changed = unsafe = 0
examples = []
for r in rows:
    old = r["product"]; new = clean(old)
    if new == old:
        continue
    if nowhite(old) != nowhite(new):      # HARD SAFETY: letters must be identical
        unsafe += 1
        continue
    conn.execute("UPDATE master_products SET product=? WHERE id=?", (new, r["id"]))
    changed += 1
    if len(examples) < 5:
        examples.append((old.replace("\n","⏎")[:44], new[:40]))
conn.commit()

dirty_after = conn.execute("SELECT COUNT(*) FROM master_products WHERE "+DIRTY).fetchone()[0]
total_after = conn.execute("SELECT COUNT(*) FROM master_products").fetchone()[0]

print("=== cleanup on COPY:", DB, "===")
print("  total products     : %d -> %d   (must be equal)" % (total_before, total_after))
print("  dirty names before : %d" % dirty_before)
print("  changed (ws-only)  : %d" % changed)
print("  UNSAFE skipped     : %d   (must be 0)" % unsafe)
print("  dirty names after  : %d   (should be 0)" % dirty_after)
print()
for o, n in examples:
    print("   %-44s -> %s" % (o, n))
conn.close()
