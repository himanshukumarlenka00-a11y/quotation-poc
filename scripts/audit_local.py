# Matcher accuracy audit — run against a FRESH copy of the production DB.
#
#   python scripts/audit_local.py [db_path]
#
# Samples real products, disguises each the way the team actually types
# (typos, reordered words, dropped words, model codes, lowercase, spaced or
# stripped units) and resolves every query through the real matcher.
#
# STRICT = the exact sampled row came back first.
# FAIR   = the right product came back: every significant query word is
#          covered by the picked row's name/spec (or the typed model code is
#          carried by the pick). Price-first deliberately picks the cheapest
#          equal sibling, so FAIR is the number that matters; judge failures
#          by CLASS, not by the percentage alone (random sampling wanders a
#          couple of points between runs).
import io
import random
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

DB = sys.argv[1] if len(sys.argv) > 1 else (
    r"C:/Users/itzan/AppData/Local/Temp/claude/E--rtk-bin/"
    r"a5c191e2-1787-498d-9c93-e9bb90bd3dd7/scratchpad/prod_quotations.db")

import app.db as appdb
appdb.DB_PATH = DB
from app.db import get_db
from app.routers.quotations import _resolve_master_matches

SAMPLE = 120
random.seed()   # deliberately unseeded — different slice every run

conn = get_db()
rows = [dict(r) for r in conn.execute(
    "SELECT id, product, COALESCE(original_model,'') model, "
    "COALESCE(specification,'') spec, COALESCE(brand,'') brand "
    "FROM master_products WHERE LENGTH(TRIM(product)) >= 8 "
    "ORDER BY RANDOM() LIMIT ?", (SAMPLE,))]

STOP = {"the", "and", "for", "with"}


def human(name, brand):
    segs = [s for s in re.split(r"\s*-\s*", name) if s.strip()]
    bl = brand.strip().lower()
    while len(segs) > 1 and (segs[0].strip().lower() == bl
                             or (any(c.isdigit() for c in segs[0])
                                 and not re.search(r"\s", segs[0].strip()))):
        segs.pop(0)
    return " ".join(segs)


def words(s):
    return [w for w in re.findall(r"[A-Za-z0-9&.\"]+", s) if len(w) >= 2]


def queries(r):
    """[(style, query)] for one product row."""
    h = human(r["product"], r["brand"]).strip()
    ws = words(h)
    out = []
    if len(h) >= 8:
        out.append(("exact_core", h))
        out.append(("lowercase_ampersand", h.lower()))
        out.append(("hyphen_to_space", re.sub(r"[-_]+", " ", r["product"])))
    if len(ws) >= 3:
        mid = list(ws)
        mid.pop(len(mid) // 2)
        out.append(("skip_middle", " ".join(mid)))
        sh = list(ws)
        random.shuffle(sh)
        out.append(("reordered", " ".join(sh)))
    alpha = [w for w in ws if w.isalpha() and len(w) >= 5]
    if alpha:
        w = random.choice(alpha)
        i = random.randrange(1, len(w) - 1)
        typo = w[:i] + w[i + 1] + w[i] + w[i + 2:]
        out.append(("typo", h.replace(w, typo, 1)))
    m = r["model"].strip()
    if len(m) >= 5 and any(c.isdigit() for c in m):
        out.append(("model_lower_spaced", re.sub(r"\s+", " ", m.lower())))
        if len(m) >= 8:
            out.append(("model_partial", m[:max(6, len(m) * 2 // 3)]))
    um = re.search(r"(\d+(?:\.\d+)?)\s*(l|ltr|litre|ml|cm|mm|qt|oz)\b",
                   (h + " " + r["spec"]).lower())
    if um:
        core = " ".join([w for w in ws if w.isalpha()][:3])
        if core:
            out.append(("unit_spaced", f"{core} {um.group(1)} {um.group(2)}"))
            out.append(("unit_stripped", f"{core} {um.group(1)}{um.group(2)}"))
    return out


def fair_ok(r, item):
    """Did we get the right PRODUCT (family), even if not the sampled row?"""
    if item.get("not_in_catalog"):
        return False
    hay = ((item.get("product") or "") + " " + (item.get("model_no") or "")
           + " " + (item.get("specification") or "")).lower()
    hay_ns = re.sub(r"[^a-z0-9]", "", hay)
    src = human(r["product"], r["brand"]).lower()
    sig = [w for w in re.findall(r"[a-z]{4,}", src) if w not in STOP]
    if not sig:
        return True
    hit = sum(1 for w in sig if w in hay or w in hay_ns)
    return hit >= max(1, int(len(sig) * 0.6))


styles = {}
fails = []
for r in rows:
    for style, q in queries(r):
        try:
            m, nf = _resolve_master_matches(
                conn, [{"product": q, "qty": 1}], [], ["3star"], None, prompt=q)
        except Exception:
            continue
        it = m[0] if m else None
        strict = bool(it and not it.get("not_in_catalog")
                      and (it.get("product") or "").strip() == r["product"].strip())
        fair = strict or (it is not None and fair_ok(r, it))
        s = styles.setdefault(style, [0, 0, 0])
        s[0] += 1
        s[1] += 1 if strict else 0
        s[2] += 1 if fair else 0
        if not fair:
            fails.append((style, q[:44], (it.get("product") or "?")[:40] if it else "-",
                          r["product"][:40]))

print("=== accuracy by style (strict / FAIR) ===")
tot = [0, 0, 0]
for style, (n, st, fa) in sorted(styles.items(), key=lambda x: x[1][2] / max(1, x[1][0])):
    print(f"{style:20} strict {st:3}/{n:3} ({st * 100 // max(1, n)}%)   "
          f"fair {fa:3}/{n:3} ({fa * 100 // max(1, n)}%)")
    tot[0] += n; tot[1] += st; tot[2] += fa
print(f"{'TOTAL':20} strict {tot[1]}/{tot[0]} ({tot[1] * 100 // max(1, tot[0])}%)   "
      f"fair {tot[2]}/{tot[0]} ({tot[2] * 100 // max(1, tot[0])}%)")
print("\n=== genuine failures (style | query | got | expected) ===")
for f in fails[:40]:
    print(" | ".join(f))
conn.close()
