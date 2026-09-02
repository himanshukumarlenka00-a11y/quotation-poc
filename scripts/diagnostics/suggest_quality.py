#!/usr/bin/env python3
# Measure CURRENT suggestion quality on a real BOQ. For each line that ends
# not_found but whose product IS in the catalogue (findable), does
# suggest_products already surface a plausible match in the top-6?
# Read-only, isolated copy. No code change — measures the code as-is.
import os, sys, json, re
sys.path.insert(0, os.environ.get("APP_DIR", "/opt/quotegen"))
import app.routers.quotations as Q
Q._llm_chat = lambda *a, **k: ""
from app.parser import parse_boq_excel
import sqlite3
conn = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "quotations.db"))
conn.row_factory = sqlite3.Row

UNITS = getattr(Q, "_UNITS", set())
def sig(t):
    return [w for w in (x.lower() for x in re.findall(r"[A-Za-z]+", t or "")) if len(w)>=3]
def findable(phrase):
    ws=sig(phrase)
    if not ws: return None
    q=" OR ".join('"%s"*'%w for w in sorted(set(ws))[:12])
    try:
        rows=conn.execute("SELECT m.product FROM master_fts f JOIN master_products m ON m.id=f.rowid "
            "WHERE master_fts MATCH ? ORDER BY rank LIMIT 15",(q,)).fetchall()
    except Exception: return None
    head,others=ws[-1],set(ws[:-1])
    for r in rows:
        nl=(r["product"] or "").lower()
        if re.search(r"\b"+re.escape(head)+r"\b",nl) and (len(ws)==1 or (others&set(sig(nl)))): return head
    return None

path=sys.argv[1]
rows,_=parse_boq_excel(path, os.path.basename(path), skip_images=True)
def full_text(r):
    return " ".join(p.strip() for p in [r.get("product") or "", r.get("model_no") or "", r.get("specification") or ""] if p and p.strip())

extracted=[{"product":r.get("product",""),"search_term":" ".join(full_text(r).split()[:20]),
    "_fulltext":full_text(r),"model_no":r.get("model_no","") or "","section":"","src_key":"x|0",
    "qty":int(r.get("qty") or 1),"boq_price":float(r.get("price") or 0)}
    for r in rows if (r.get("product") or "").strip()]
items,_=Q._resolve_master_matches(conn,extracted,[],["3star"],object(),llm_verify=False)

# map result back to source rows by position to recover the full_text
findable_nf=0; have_sugg=0; examples=[]
for it,src in zip(items, [r for r in rows if (r.get("product") or "").strip()]):
    if not (it.get("matched_by")=="not_found" or it.get("not_in_catalog")): continue
    label=it.get("requested") or it.get("product") or ""
    head=findable(label)
    if not head: continue          # genuinely absent -> suggestions correctly N/A
    findable_nf+=1
    sugg=Q.suggest_products(conn, full_text(src), None, 6) or []
    hit=any(re.search(r"\b"+re.escape(head)+r"\b",(s.get("product") or "").lower()) for s in sugg)
    if hit: have_sugg+=1
    elif len(examples)<10: examples.append((label[:34], head, [ (s.get("product") or "")[:26] for s in sugg[:3] ]))

print("FILE:", os.path.basename(path))
print("findable not_found lines: %d" % findable_nf)
print("  right product IS in top-6 suggestions: %d  (%d%%)" % (have_sugg, 100*have_sugg//(findable_nf or 1)))
print("  MISSED by suggestions                : %d" % (findable_nf-have_sugg))
print()
print("examples suggestions MISSED (label / wanted-head / top-3 shown):")
for lab,head,top in examples:
    print("   - %-34s [%s]  ->  %s" % (lab, head, top))
conn.close()
