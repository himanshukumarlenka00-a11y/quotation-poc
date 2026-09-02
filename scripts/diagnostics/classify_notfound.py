#!/usr/bin/env python3
# READ-ONLY. Classify WHY not_found lines failed, from the annotation the
# matcher writes into the not_found LIST:
#   bare label            -> fell through `if not variants`  (fix CAN help)
#   "(model X not in ...)" -> hard model gate                (fix does NOT help)
#   "(under / over ...)"   -> price constraint               (fix does NOT help)
# Product wording only. Read-only.
import os, sqlite3, json, re
conn = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "quotations.db"))
conn.row_factory = sqlite3.Row

bare=model=price=other=0
bare_ex=[]
for row in conn.execute("SELECT items_json FROM quotations ORDER BY created_at DESC LIMIT 80"):
    try: d=json.loads(row["items_json"])
    except Exception: continue
    if not isinstance(d,dict): continue
    for s in (d.get("not_found") or []):
        s=str(s)
        if re.search(r"model .*not in master|not in master", s, re.I): model+=1
        elif re.search(r"\((?:over|under|upto|max|between).*₹|₹.*\)", s, re.I): price+=1
        elif "(" in s and ")" in s: other+=1
        else:
            bare+=1
            if len(bare_ex)<10: bare_ex.append(s[:46])

tot=bare+model+price+other or 1
print("not_found LIST entries classified (recent 80 quotes):")
print("  BARE label   (fix's target path): %5d  (%d%%)" % (bare, 100*bare//tot))
print("  model-gate   (fix can't help)   : %5d  (%d%%)" % (model, 100*model//tot))
print("  price limit  (fix can't help)   : %5d  (%d%%)" % (price, 100*price//tot))
print("  other paren.                    : %5d  (%d%%)" % (other, 100*other//tot))
print("  total: %d" % tot)
print()
print("examples of BARE not_found (the ones the fix retries):")
for e in bare_ex: print("   -", e)
conn.close()
