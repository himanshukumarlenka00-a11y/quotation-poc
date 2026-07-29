import re
from datetime import datetime
from app.db import get_db

# ── Helpers ───────────────────────────────────────────────────────────────────
# NOTE: this is also where Phase 3's vector search / query-memory lookups
# will be added, replacing get_boq_context's keyword-scan for large catalogs.

def get_boq_context(conn, prompt: str = "", catalogs: list = None) -> str:
    # Fetch items — filter by selected catalogs if specified
    if catalogs:
        placeholders = ",".join("?" * len(catalogs))
        rows = conn.execute(
            f"SELECT * FROM boq_items WHERE file_name IN ({placeholders}) ORDER BY file_name, product",
            catalogs
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM boq_items ORDER BY file_name, product").fetchall()
    if not rows:
        return "No product catalog uploaded yet."

    # Extract keywords — letters only (no numbers), ≥3 chars, skip stop words
    stop_words = {"the", "and", "for", "we", "need", "want", "give", "get",
                  "our", "us", "me", "a", "an", "of", "in", "is", "are",
                  "also", "with", "some", "please", "nos", "nos.", "pcs"}
    keywords = []
    for w in re.split(r'\W+', prompt):
        wl = w.lower()
        if len(w) >= 3 and w.isalpha() and wl not in stop_words:
            keywords.append(wl)
            # Add singular form so plurals match (e.g. "irons" → "iron",
            # "chairs" → "chair") without losing the original word.
            if wl.endswith('s') and len(wl) > 3:
                keywords.append(wl[:-1])

    def relevance(r):
        # Compare everything in lowercase for case-insensitive matching.
        # Product-name hits are weighted far higher than brand/description/spec
        # hits, so a directly-requested item (e.g. "yoga mat") always outranks
        # an incidental substring hit (e.g. "mat" inside "bath mat").
        name = (r['product'] or '').lower()
        other = " ".join([
            (r['brand'] or ''),
            (r['description'] or ''),
            (r['specification'] or ''),
        ]).lower()
        score = 0
        for k in keywords:
            if k in name:
                score += 10
            elif k in other:
                score += 1
        return score

    # Phase 1: find all directly matching rows (relevance > 0)
    matched_bases = set()
    for r in rows:
        if relevance(r) > 0:
            # Collect base name (first 2 words, uppercase) to pull in all variants
            base = ' '.join((r['product'] or '').upper().split()[:2])
            matched_bases.add(base)

    # Phase 2: include all rows that either matched OR share a base name with a match
    # This ensures ALL variants from ALL catalogs are included
    included = []
    for r in rows:
        base = ' '.join((r['product'] or '').upper().split()[:2])
        if relevance(r) > 0 or base in matched_bases:
            included.append(r)

    if not included:
        # No keyword match — return top 15 by recency as fallback
        included = conn.execute(
            "SELECT * FROM boq_items ORDER BY uploaded_at DESC LIMIT 15"
        ).fetchall()

    # Sort included by relevance score (strongest matches first) so that
    # directly-requested products survive the 30-item cap below. Ties broken
    # alphabetically for stable output.
    def sort_key(r):
        return (-relevance(r), (r['product'] or ''))
    included.sort(key=sort_key)

    # Deduplicate: if same product name exists with both ₹0 and non-zero price,
    # keep only the non-zero one. Case-insensitive comparison.
    seen_products = {}
    for r in included:
        key = (r['product'] or '').upper().strip()
        if key not in seen_products:
            seen_products[key] = r
        else:
            # Prefer non-zero price
            if (seen_products[key]['price'] or 0) == 0 and (r['price'] or 0) > 0:
                seen_products[key] = r
    included = list(seen_products.values())

    # Cap at 30 items to stay within Groq 12K TPM limit
    included = included[:30]

    # Build compact catalog lines — keep fields minimal to save tokens
    lines = []
    for r in included:
        spec = (r['specification'] or '')[:30].replace('\n', ' ')
        price = r['price'] if r['price'] else 0
        lines.append(
            f"{r['product']}|{r['brand'] or '-'}|{r['model_no'] or '-'}"
            f"|{spec}|HSN:{r['hsn_code'] or '-'}|INR:{price}|GST:{r['gst_pct'] or 18}%"
        )
    return "\n".join(lines)


def get_feedback_context(conn) -> str:
    rows = conn.execute(
        "SELECT f.rating, f.missing_items FROM feedback f "
        "ORDER BY f.created_at DESC LIMIT 30"
    ).fetchall()
    if not rows:
        return "No feedback yet."
    return "\n".join(f"- Rating: {r['rating']} | Issue: {r['missing_items']}" for r in rows)


def generate_ref_no() -> str:
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM quotations").fetchone()[0]
    conn.close()
    return f"SMI-{datetime.now().strftime('%Y%m')}-{count + 1:04d}"


def get_latest_template(conn):
    return conn.execute("SELECT * FROM templates ORDER BY uploaded_at DESC LIMIT 1").fetchone()
