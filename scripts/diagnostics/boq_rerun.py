#!/usr/bin/env python3
# Definitive test: re-run a REAL saved client BOQ through the matcher with its
# real product/model/spec text, LLM neutralised. Counts not_found before vs
# after the fix. Run under prod code and fixed code; the not_found delta is
# the fix's real benefit. Read-only; isolated copy DB.
import os, sys, json, re
sys.path.insert(0, os.environ.get("APP_DIR", "/opt/quotegen"))
import app.routers.quotations as Q
Q._llm_chat = lambda *a, **k: ""
from app.parser import parse_boq_excel
import sqlite3
conn = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "quotations.db"))
conn.row_factory = sqlite3.Row

path = sys.argv[1]
rows, _ = parse_boq_excel(path, os.path.basename(path), skip_images=True)

def full_text(r):
    parts=[r.get("product") or "", r.get("model_no") or "", r.get("specification") or ""]
    return " ".join(p.strip() for p in parts if p and p.strip())

extracted=[]
for r in rows:
    if not (r.get("product") or "").strip(): continue
    ft=full_text(r)
    st=" ".join(ft.split()[:20])
    extracted.append({"product":r.get("product",""), "search_term":st, "_fulltext":ft,
        "model_no":r.get("model_no","") or "", "section":r.get("sheet_name","") or "",
        "src_key":"x|0", "qty":int(r.get("qty") or 1), "boq_price":float(r.get("price") or 0)})

items, nf = Q._resolve_master_matches(conn, extracted, [], ["3star"],
                                      groq_client=object(), llm_verify=False)
total=len(items)
notfound=sum(1 for it in items if it.get("matched_by")=="not_found" or it.get("not_in_catalog"))
matched=total-notfound
print("FILE: %s   (%d product lines)" % (os.path.basename(path), total))
print("  matched   : %d  (%d%%)" % (matched, 100*matched//(total or 1)))
print("  not_found : %d  (%d%%)" % (notfound, 100*notfound//(total or 1)))
# per-line request -> pick (null if not_found), for before/after diffing
mp={}
for it in items:
    req=(it.get("requested") or it.get("product") or "").strip()
    pick=None if (it.get("matched_by")=="not_found" or it.get("not_in_catalog")) else it.get("product")
    mp[req]=pick
print("###JSON###"+json.dumps(mp, ensure_ascii=False))
conn.close()
