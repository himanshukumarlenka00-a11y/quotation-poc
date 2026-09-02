#!/usr/bin/env python3
# Measurement harness for the matcher name-fallback fix.
#   APP_DIR   = which code to test (/opt/quotegen = prod, or a fixed copy)
#   DATA_DIR  = isolated DB copy (never the live prod DB)
# Runs the REAL matcher with all LLM calls neutralised. Two sets:
#   REGRESSION - currently-matching lines, fed PRODUCTION-STYLE search_term
#                (product + model + spec). Snapshot on run 1; run 2 flags any
#                pick the code change altered.  Must stay stable.
#   SHOULD-FIX - not_found lines whose product IS in the catalogue. Reports
#                the bare-label recovery rate (the ceiling the fix delivers).

import os, sys, json, re
sys.path.insert(0, os.environ.get("APP_DIR", "/opt/quotegen"))

import app.routers.quotations as Q
Q._llm_chat = lambda *a, **k: ""          # neutralise every LLM call

import sqlite3
DB = os.path.join(os.environ["DATA_DIR"], "quotations.db")
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row

UNITS = {"cm","mm","ml","cl","ltr","ltrs","l","kg","g","pc","pcs","set","sets",
         "inch","in","mtr","kw","w","v","hz","oz","qt","no","nos","x"}
def sig(t):
    return [w for w in (x.lower() for x in re.findall(r"[A-Za-z]+", t or ""))
            if len(w) >= 3 and w not in UNITS]

def findable(phrase):
    words = sig(phrase)
    if not words: return None
    q = " OR ".join('"%s"*' % w for w in sorted(set(words))[:12])
    try:
        rows = conn.execute("SELECT m.product FROM master_fts f JOIN master_products m "
            "ON m.id=f.rowid WHERE master_fts MATCH ? ORDER BY rank LIMIT 15",(q,)).fetchall()
    except Exception: return None
    head, others = words[-1], set(words[:-1])
    for r in rows:
        nl=(r["product"] or "").lower()
        if re.search(r"\b"+re.escape(head)+r"\b", nl) and (len(words)==1 or (others & set(sig(nl)))):
            return words[-1]
    return None

def match(product, model="", spec=""):
    """Run the real matcher for one line. search_term reconstructs production:
    product + model + spec (what _search_term feeds). Returns picked product."""
    full = " ".join(x.strip() for x in (product, model, spec) if x and x.strip())
    item = {"product":product, "search_term":full, "_fulltext":full,
            "model_no":model, "section":"", "src_key":"t|0", "qty":1, "boq_price":0}
    try:
        items,_ = Q._resolve_master_matches(conn, [item], [], ["3star"],
                                            groq_client=object(), llm_verify=False)
    except Exception as e:
        return ("ERR", str(e)[:90])
    it = items[0] if items else None
    if not it or it.get("not_in_catalog") or it.get("matched_by")=="not_found":
        return None
    return it.get("product")

# ---- build test sets from the copy ----
reg, fix = [], []
for row in conn.execute("SELECT items_json FROM quotations ORDER BY created_at DESC LIMIT 120"):
    try: d=json.loads(row["items_json"])
    except Exception: continue
    if not isinstance(d, dict): continue
    for it in (d.get("items") or []):
        req=(it.get("requested") or it.get("product") or "").strip()
        if not req: continue
        mb=it.get("matched_by")
        if mb in ("ai","learned") and it.get("product") and not it.get("not_in_catalog"):
            # real fields so search_term reconstructs the production full-text
            reg.append((req, it.get("model_no","") or "", it.get("specification","") or "", it.get("product")))
        elif mb=="not_found" or it.get("not_in_catalog"):
            if findable(req): fix.append(req)

def dd(seq, keyf):
    seen=set(); out=[]
    for x in seq:
        k=keyf(x)
        if k in seen: continue
        seen.add(k); out.append(x)
    return out
reg = dd(reg, lambda x: x[0].lower().strip())[:160]
fix = dd(fix, lambda x: x.lower().strip())[:120]

# ---- REGRESSION: snapshot current behavior; later runs compare to it ----
SNAP = os.path.join(os.environ["DATA_DIR"], "reg_snapshot.json")
cur = {}
for req, model, spec, prod in reg:
    got = match(req, model, spec)
    cur[req] = None if isinstance(got, tuple) else got
if os.path.exists(SNAP):
    base = json.load(open(SNAP))
    common = [r for r in base if r in cur]
    reg_same = sum(1 for r in common if cur[r] == base[r])
    reg_diff = [r for r in common if cur[r] != base[r]]
    reg_mode = "COMPARED to before-fix snapshot"
else:
    json.dump(cur, open(SNAP, "w"))
    reg_same, reg_diff = len(cur), []
    reg_mode = "SNAPSHOT SAVED (before-fix reference)"
reg_lost = sum(1 for v in cur.values() if v is None)

# ---- SHOULD-FIX: bare-label recovery (the ceiling the fix delivers) ----
fix_pass=0; fix_fail_ex=[]
for req in fix:
    got = match(req)                       # blank model/spec -> bare label
    head = findable(req)
    ok = (not isinstance(got,tuple)) and got and head and re.search(r"\b"+re.escape(head)+r"\b", got.lower())
    if ok: fix_pass+=1
    elif len(fix_fail_ex)<8: fix_fail_ex.append(req[:44])

R=len(reg) or 1; F=len(fix) or 1
print("=== MATCHER HARNESS  (APP_DIR=%s) ===" % os.environ.get("APP_DIR","/opt/quotegen"))
print()
print("REGRESSION (%d currently-matching lines) — %s" % (len(reg), reg_mode))
print("  unchanged : %d / %d  (%d%%)" % (reg_same, len(reg), 100*reg_same//R))
print("  CHANGED   : %d" % len(reg_diff))
for r in reg_diff[:6]:
    print("      * %-38s  before=%s  now=%s" % (r[:38], base[r], cur[r]))
print("  no-match  : %d" % reg_lost)
print()
print("SHOULD-FIX (%d not_found lines whose product IS in catalogue):" % len(fix))
print("  bare-label resolves: %d / %d  (%d%%)" % (fix_pass, len(fix), 100*fix_pass//F))
for e in fix_fail_ex: print("      still hard:", e)
conn.close()
