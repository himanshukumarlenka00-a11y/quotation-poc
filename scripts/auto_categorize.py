"""One-time (re-runnable) auto-categorization of master_products rows with an
empty category. Two passes:

  1. Keyword pass — free, deterministic, covers the obvious majority.
  2. LLM pass (Groq) — batches the leftovers, picks from the same fixed list.

Only rows with an empty category are touched, so it's resumable and never
overwrites human-assigned categories (including ones set via the selection
bar). Run on the server:  python3 scripts/auto_categorize.py [--dry-run]
"""
import json
import os
import re
import sqlite3
import sys
import time

DB = os.environ.get("QUOTEGEN_DB", "/srv/quotegen-data/quotations.db")

CATS = [
    "Crockery", "Cutlery & Flatware", "Glassware", "Barware",
    "Buffetware & Chafers", "Cookware", "Bakeware", "Kitchen Utensils",
    "Knives", "Kitchen Equipment", "Storage & GN Pans", "Trolleys",
    "Housekeeping", "Furniture", "Linen", "Table Accessories",
    "Copperware", "Woodenware", "Waste Management", "General",
]

# ponytail: naive first-match keyword table; the LLM pass catches what it misses
KEYWORDS = [
    (r"\b(plate|bowl|cup|saucer|mug|platter|dish\b|porcelain|dinner set|ramekin|tureen)", "Crockery"),
    (r"\b(fork|spoon|knife set|flatware|cutlery|teaspoon|ladle spoon)", "Cutlery & Flatware"),
    (r"\b(glass(es|ware)?\b|tumbler|goblet|decanter|carafe|wine|champagne|highball|stemware)", "Glassware"),
    (r"\b(bar |cocktail|shaker|jigger|muddler|strainer|corkscrew|ice bucket|bottle opener)", "Barware"),
    (r"\b(chafing|chafer|buffet|induction warmer|food warmer|display stand|juice dispenser|cereal dispenser|station\b|plinth|riser)", "Buffetware & Chafers"),
    (r"\b(fry ?pan|saucepan|stockpot|casserole|wok|cookware|kadai|tawa|sauteuse|braising)", "Cookware"),
    (r"\b(baking|bakeware|cake|muffin|tart|loaf|pastry|dough|piping|mould|mold)", "Bakeware"),
    (r"\b(whisk|spatula|tong|peeler|grater|masher|scoop|turner|skimmer|colander|strainer|opener|utensil)", "Kitchen Utensils"),
    (r"\b(chef knife|paring|cleaver|santoku|boning|filleting|sharpen)", "Knives"),
    (r"\b(machine|mixer|blender|grinder|cooktop|oven|refrigerat|freezer|griddle|fryer|slicer|toaster|equipment)", "Kitchen Equipment"),
    (r"\b(waste|garbage|dustbin|pedal bin|trash|ash ?bin)", "Waste Management"),
    (r"\b(gn pan|gastronorm|container|storage|crate|bin\b|canister|dispenser)", "Storage & GN Pans"),
    (r"\b(trolley|cart\b|caddy)", "Trolleys"),
    (r"\b(housekeep|mop|broom|dustpan|cleaning|janitor|caution|lobby|squeegee)", "Housekeeping"),
    (r"\b(table\b.*\b(folding|banquet)|chair|stool|furniture)", "Furniture"),
    (r"\b(linen|napkin|table ?cloth|runner|skirting|towel)", "Linen"),
    (r"\b(lamp|candle|menu stand|table number|holder|vase|centerpiece|centrepiece)", "Table Accessories"),
    (r"\b(copper)", "Copperware"),
    (r"\b(wooden|wood\b|bamboo|acacia)", "Woodenware"),
]
KEYWORDS = [(re.compile(p, re.I), c) for p, c in KEYWORDS]

BATCH = 50
MODEL = "openai/gpt-oss-20b"


def llm_classify(client, names):
    """Return a category index (into CATS) for each name, or None on failure."""
    cat_list = "\n".join(f"{i}. {c}" for i, c in enumerate(CATS))
    item_list = "\n".join(f"{i}. {n[:90]}" for i, n in enumerate(names))
    prompt = (
        "Classify each hotel/restaurant supply product into exactly one category.\n"
        f"Categories:\n{cat_list}\n\nProducts:\n{item_list}\n\n"
        f"Reply with ONLY a JSON array of {len(names)} integers (the category "
        "number for each product, in order). No other text."
    )
    attempt = 0
    while attempt < 4:
        try:
            r = client.chat.completions.create(
                model=MODEL, temperature=0, max_tokens=4000,
                reasoning_effort="low",
                messages=[{"role": "user", "content": prompt}])
            text = r.choices[0].message.content
            m = re.search(r"\[[\d,\s]+\]", text)
            arr = json.loads(m.group(0))
            if len(arr) == len(names) and all(0 <= int(x) < len(CATS) for x in arr):
                return [int(x) for x in arr]
            print(f"  bad shape (got {len(arr)}), retrying")
            attempt += 1
        except Exception as e:
            msg = str(e)
            if "per day" in msg or "TPD" in msg:
                # Daily quota exhausted — tokens free up on a rolling 24h
                # window, so wait it out instead of burning retry attempts.
                print("  daily quota hit, sleeping 30min", flush=True)
                time.sleep(1800)
            elif "429" in msg or "rate" in msg.lower():
                print("  rate limited, sleeping 60s", flush=True)
                time.sleep(60)
            else:
                print(f"  LLM error: {msg[:200]}", flush=True)
                time.sleep(5)
                attempt += 1
    return None


def main():
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, product FROM master_products "
        "WHERE TRIM(COALESCE(category,'')) = ''").fetchall()
    print(f"{len(rows)} uncategorised rows")

    # Pass 1: keywords
    kw_hits, leftovers = [], []
    for rid, name in rows:
        for rx, cat in KEYWORDS:
            if rx.search(name or ""):
                kw_hits.append((cat, rid))
                break
        else:
            leftovers.append((rid, name))
    print(f"keyword pass: {len(kw_hits)} matched, {len(leftovers)} left for LLM")
    if not dry:
        conn.executemany("UPDATE master_products SET category=? WHERE id=?", kw_hits)
        conn.commit()

    if dry:
        from collections import Counter
        print(Counter(c for c, _ in kw_hits).most_common())
        for rid, name in leftovers[:20]:
            print("  LLM-bound:", name[:80])
        return

    # Pass 2: LLM
    if not leftovers:
        return
    from groq import Groq
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        sys.exit("GROQ_API_KEY not set")
    # timeout guards against a dropped connection hanging a request forever
    # (observed on this LAN); llm_classify's retry loop handles the failure.
    client = Groq(api_key=key, timeout=90, max_retries=0)
    done = 0
    for i in range(0, len(leftovers), BATCH):
        chunk = leftovers[i:i + BATCH]
        idxs = llm_classify(client, [n for _, n in chunk])
        if idxs is None:
            print(f"  batch {i // BATCH} failed permanently, skipping")
            continue
        conn.executemany(
            "UPDATE master_products SET category=? WHERE id=?",
            [(CATS[ix], rid) for (rid, _), ix in zip(chunk, idxs)])
        conn.commit()
        done += len(chunk)
        print(f"LLM: {done}/{len(leftovers)}")
        time.sleep(2)  # ponytail: fixed pacing; tune if Groq limits allow faster
    left = conn.execute("SELECT COUNT(*) FROM master_products "
                        "WHERE TRIM(COALESCE(category,''))=''").fetchone()[0]
    print(f"done — {left} rows still uncategorised")


if __name__ == "__main__":
    main()
