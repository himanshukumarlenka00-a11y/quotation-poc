"""Collapse embedded newlines/whitespace runs in master product names and fix
the two known 'Coktail' misspellings. Idempotent; rebuilds FTS afterwards."""
import os
import re
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/opt/quotegen"))
import app.db as appdb
if os.environ.get("QUOTEGEN_DB"):
    appdb.DB_PATH = os.environ["QUOTEGEN_DB"]
from app.db import get_db, rebuild_master_fts

conn = get_db()
rows = conn.execute(
    "SELECT id, product FROM master_products "
    "WHERE product LIKE '%' || CHAR(10) || '%' OR product LIKE '%' || CHAR(13) || '%'"
    "   OR product LIKE '%  %' OR product != TRIM(product)"
    "   OR product LIKE '%oktail%'").fetchall()
fixed = []
for r in rows:
    new = re.sub(r"\s+", " ", r["product"]).strip()
    new = re.sub(r"\b[Cc][Oo][Kk][Tt][Aa][Ii][Ll]\b",
                 lambda m: "Cocktail" if m.group(0)[0] == "C" else "cocktail", new)
    if new != r["product"]:
        fixed.append((new, r["id"]))
print(f"{len(rows)} candidates, {len(fixed)} need fixing")
for new, rid in fixed[:8]:
    print("  ->", repr(new[:70]))
if "--apply" in sys.argv:
    conn.executemany("UPDATE master_products SET product=? WHERE id=?", fixed)
    conn.commit()
    rebuild_master_fts(conn)
    print("APPLIED + FTS rebuilt")
conn.close()
