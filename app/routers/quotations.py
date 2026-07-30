import os, re, json, tempfile
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from groq import Groq
from app.config import limiter, GROQ_API_KEY_DEFAULT
from app.db import get_db
from app.auth import get_current_user, require_role, _check_quote_access, log_action
from app.matching import get_boq_context, get_feedback_context, generate_ref_no, get_latest_template
from app.export import build_company_quotation
from app.parser import parse_boq_excel
from app.routers.catalog import _save_upload_validated

router = APIRouter()



class BuildQuotationRequest(BaseModel):
    client_name: str = ""
    items: list = []

class GenerateRequest(BaseModel):
    prompt: str
    client_name: str = ""
    catalogs: list = []  # list of file_name strings; empty = search all
    tiers: list = ["3star"]  # subset of ["3star", "4star"] — which master-table price tier(s) to show





@router.post("/api/smart-generate")
@limiter.limit("30/minute")
def smart_generate(request: Request, req: GenerateRequest, user: dict = Depends(get_current_user)):
    try:
        return _strip_cost(_smart_generate(req, user), user)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")

def _smart_generate(req: GenerateRequest, user: dict):
    api_key = os.environ.get("GROQ_API_KEY", "") or GROQ_API_KEY_DEFAULT
    if not api_key:
        raise HTTPException(400, "Groq API key required")

    # Step 1: LLM extracts product names + qty (tiny prompt, fast)
    try:
        groq_client = Groq(api_key=api_key)
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content":
                 "Extract product names and quantities from the customer requirement. "
                 "Return ONLY valid JSON: {\"items\":[{\"product\":\"iron\",\"qty\":30}]} "
                 "Preserve the FULL product name exactly as the customer wrote it, including "
                 "qualifiers (e.g. 'housekeeping trolley', 'soiled linen trolley', 'laundry box big', "
                 "'lobby luggage cart' — do NOT shorten these to just 'trolley', 'box' or 'cart'). "
                 "ALWAYS keep spec qualifiers and model codes that follow a product, such as "
                 "wattage, capacity, voltage, size or a model number (e.g. 'kettle 1500W', "
                 "'iron 1600W', 'bucket 25L', 'IR-EK005') — these distinguish variants, never drop them. "
                 "Keep each distinct requested item as its own entry; never merge two different items. "
                 "Default qty to 1 if not stated. Do not add any product not mentioned."},
                {"role": "user", "content": req.prompt}
            ],
            max_tokens=400, temperature=0.1
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip().rstrip("```")
        extracted = json.loads(raw).get("items", [])
    except Exception as e:
        # Surface Groq rate limits as a clear, retryable 429 instead of a 500
        if "429" in str(e) or "rate limit" in str(e).lower():
            raise HTTPException(429, "Server is busy right now (rate limit). Please wait a few seconds and try again.")
        raise HTTPException(500, f"Extraction error: {e}")

    conn = get_db()
    result_items, not_found = _resolve_master_matches(conn, extracted, req.catalogs, req.tiers, groq_client, prompt=req.prompt)

    ref_no = f"QT-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    data   = {"ref_no": ref_no, "client_name": req.client_name,
              "items": result_items, "not_found": not_found}

    # Save to DB (strip internal _ keys)
    clean_items = [{k: v for k, v in i.items() if not k.startswith("_")} for i in result_items]
    data_db = {**data, "items": clean_items}
    cur = conn.execute(
        "INSERT INTO quotations (ref_no,client_name,items_json,status,created_by,created_at) VALUES (?,?,?,?,?,?)",
        (ref_no, req.client_name, json.dumps(data_db), "draft", user["id"], datetime.now().isoformat())
    )
    data["id"] = cur.lastrowid
    conn.commit()
    conn.close()
    log_action(user, "smart_generate_quotation", target=ref_no)
    return data


def _strip_cost(data, user):
    """Remove purchase cost from a quotation payload for non-admins.

    Quotation items carry the master-table `cost` so margin can be shown to an
    admin. Employees have no need for it, and leaving it in the JSON exposed
    what we pay suppliers to anyone who opened devtools — the on-screen table
    hiding the column is not a control.
    """
    if (user or {}).get("role") == "admin" or not isinstance(data, dict):
        return data
    for item in data.get("items") or []:
        if isinstance(item, dict):
            item.pop("cost", None)
            for v in item.get("_variants") or []:
                if isinstance(v, dict):
                    v.pop("cost", None)
    return data


def _resolve_master_matches(conn, extracted, catalogs, tiers_req, groq_client, prompt=""):
    """Match extracted {product, qty} items against the Master Table only —
    shared by both the free-text prompt flow and the client-BOQ-file-upload
    flow, so the two entry points always resolve products identically."""
    # Get all product names from the Master Table for semantic mapping.
    # Matching searches the Master Table only, not the ad-hoc BOQ catalog —
    # the master table is the trusted product source; BOQ uploads are treated
    # as client requirement lists, not catalog data (see the master-table
    # architecture decision).
    try:
        if catalogs:
            ph = ",".join("?" * len(catalogs))
            all_products = [r[0] for r in conn.execute(
                f"SELECT DISTINCT product FROM master_products WHERE file_name IN ({ph}) AND product IS NOT NULL ORDER BY product",
                catalogs
            ).fetchall()]
        else:
            all_products = [r[0] for r in conn.execute(
                "SELECT DISTINCT product FROM master_products WHERE product IS NOT NULL ORDER BY product"
            ).fetchall()]
    except Exception:
        all_products = []

    # Candidate rows for field-wide searching.
    #
    # This used to load EVERY master_products row into Python on every request.
    # Measured: 25-57ms and ~7MB at 3,000 products, which extrapolates to ~5.7s
    # and ~0.7GB at the planned 300,000 — per request, per concurrent employee.
    # SQLite's FTS5 index narrows it to the few dozen rows that share a word
    # with what was asked, in well under a millisecond, and needs no extra
    # service to run on the company's own server.
    def _fts_query(terms):
        """Build an FTS5 MATCH expression from the requested phrases."""
        words = set()
        for t in terms:
            for w in re.findall(r"[A-Za-z0-9]{2,}", str(t or "")):
                words.add(w.lower())
        # Prefix-match each word so "knive"/"knives" still reach "knife"-ish
        # rows, OR'd because any shared word makes a row worth scoring.
        return " OR ".join(f'"{w}"*' for w in sorted(words)[:40])

    search_terms = [it.get("search_term") or it.get("product") or "" for it in extracted]
    rows_pool, used_fts = [], False
    try:
        match = _fts_query(search_terms)
        if match:
            if catalogs:
                ph = ",".join("?" * len(catalogs))
                sql = (f"SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                       f"WHERE master_fts MATCH ? AND m.file_name IN ({ph}) LIMIT 4000")
                rows_pool = [dict(r) for r in conn.execute(sql, [match, *catalogs]).fetchall()]
            else:
                rows_pool = [dict(r) for r in conn.execute(
                    "SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                    "WHERE master_fts MATCH ? LIMIT 4000", (match,)).fetchall()]
            used_fts = True
    except Exception:
        rows_pool = []          # FTS missing or query rejected — fall through

    if not rows_pool:
        # Fallback: the original full scan. Correctness over speed — a missing
        # FTS index must never mean a failed quotation.
        try:
            if catalogs:
                ph = ",".join("?" * len(catalogs))
                rows_pool = [dict(r) for r in conn.execute(
                    f"SELECT * FROM master_products WHERE file_name IN ({ph}) AND product IS NOT NULL",
                    catalogs).fetchall()]
            else:
                rows_pool = [dict(r) for r in conn.execute(
                    "SELECT * FROM master_products WHERE product IS NOT NULL").fetchall()]
        except Exception:
            rows_pool = []

    # Step 2c: deterministic name match — takes priority over the LLM semantic
    # guess, which sometimes maps a request to a similar-but-wrong product
    # (e.g. "housekeeping trolley" → "Airport Trolley"). If every significant
    # word of the requested phrase appears in a catalog product NAME, that name
    # is an unambiguous match and we use it directly instead of guessing.
    units = {"inch", "inches", "cm", "mm", "mtr", "mtrs", "meter", "metre",
             "meters", "kg", "kgs", "ltr", "litre", "liter", "size", "set",
             "nos", "pcs", "pc"}

    def _covered(word, s, s_ns):
        """Is `word` present in a product name as a WORD (not a substring)?

        Plain substring matching silently produced nonsense matches: "pin"
        is inside "chop-pin-g", so "rolling pin" scored against "Wire Stand
        For Chopping Board". Anchoring on word boundaries kills that whole
        class of false positive. The space-stripped form is still consulted,
        but only for words long enough to be a real compound (bedsheet vs
        bed sheet) rather than short fragments that hit by accident.
        """
        forms = {word}
        if word.endswith('s') and len(word) > 3: forms.add(word[:-1])
        else: forms.add(word + 's')
        for f in forms:
            if re.search(r"\b" + re.escape(f) + r"\b", s):
                return True
            if len(f) >= 5 and f in s_ns:
                return True
        return False

    def search_catalog(term):
        """Find catalog rows matching a request by product NAME, MODEL NO, or
        SPECIFICATION. Returns all candidates ranked best-first — used both to
        pick the line item and to populate the variant switcher.
        Ranking: model-number hit > full name hit > partial name hit > spec hit."""
        t = (term or "").lower().strip()
        if not t:
            return []
        toks = re.findall(r"[a-z0-9]+", t)
        core = [w for w in toks if len(w) >= 3 and w.isalpha() and w not in units]
        # Two-letter designators like the "GN" in "GN pan" are real product
        # qualifiers, but too short for `core` (which needs 3+ chars to avoid
        # noise). Dropping them left "GN pan" as bare "pan", which matched
        # "Pan, Roasting Large" (Rs 19,380) over the actual GN PAN (Rs 845).
        # Scored as a bonus rather than a requirement, so they can only
        # promote the right row, never exclude a legitimate one.
        short = [w for w in toks if len(w) == 2 and w.isalpha() and w not in units]
        # model-number-like tokens in the request (mix of letters+digits, codes)
        mtoks = [w for w in re.findall(r"[a-z0-9][a-z0-9\-/\.]+", t)
                 if any(c.isdigit() for c in w) and any(c.isalpha() for c in w)]
        scored = []
        for r in rows_pool:
            name = (r.get('product') or '').lower(); name_ns = name.replace(' ', '')
            model = (r.get('original_model') or '').lower()
            spec  = (r.get('specification') or '').lower()
            score = 0
            if model and ((mtoks and any(mt in model for mt in mtoks)) or (len(t) >= 4 and t in model)):
                score = 1000                                   # model-number match (most specific)
            elif core and all(_covered(w, name, name_ns) for w in core):
                score = 600 - min(len(name), 120)              # full name match; tighter ranks higher
            elif core and (lambda hits: hits and hits / len(core) >= 0.6)(
                    sum(1 for w in core if _covered(w, name, name_ns))):
                # Partial name — but only if MOST of the request is accounted
                # for. Accepting a single shared word made "waste bin" match
                # "Ice bin module" (₹65,082) with full confidence.
                score = 200 + 10 * sum(1 for w in core if _covered(w, name, name_ns))
            elif core and all(w in spec for w in core):
                score = 120                                    # specification match
            if score:
                if short:
                    score += 100 * sum(1 for w in short
                                       if re.search(r"\b" + re.escape(w) + r"\b", name))
                if (r.get('price_3star') or 0) > 0: score += 30  # prefer rows that have a price
                if r.get('image_path'):       score += 5       # prefer rows that have an image
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return []
        # Keep only same-tier matches so the variant switcher shows genuine
        # alternatives. Drops weak partials (e.g. a row matching only "electric"
        # for a request of "electric kettle").
        cutoff = scored[0][0] * 0.6
        result = [r for s, r in scored if s >= cutoff]

        # Progressive refinement: if the request carries a spec value (wattage,
        # capacity, voltage…) or a model code, narrow to the rows that actually
        # have it — e.g. "kettle" → all kettles, "kettle 1500W" → only the 1500W.
        quals = re.findall(r"\d+(?:\.\d+)?\s*(?:kw|w|ml|l|v|hz|mm|cm|kg)\b", t)
        quals += re.findall(r"\b\d+\.\d+\b", t)
        # Inch sizes — 5", 9'', 6 inch. These name most of the catalog's
        # near-identical variants (Scraper 3"/4"/5") and were previously
        # invisible to this filter, so every size collapsed onto one row.
        quals += [m + '"' for m in re.findall(r"(\d+(?:\.\d+)?)\s*(?:\"|''|inch|inches)", t)]
        quals += mtoks
        quals = [q.replace(" ", "") for q in quals if q.strip()]
        if quals:
            def _hay(r):
                # The product NAME must be searched too: "Scraper 5"" carries
                # its size in the name, not the spec, so a model+spec-only
                # haystack could never match it.
                return ((r.get('product') or '') + ' ' +
                        (r.get('original_model') or '') + ' ' +
                        (r.get('specification') or '')).lower().replace(' ', '')
            narrowed = [r for r in result if any(q in _hay(r) for q in quals)]
            # No silent fallback. If the request names a size and nothing
            # carries it, returning the other sizes is worse than returning
            # nothing — it quotes the wrong goods at full confidence.
            return narrowed
        return result

    # Ensure model-number codes typed in the prompt (e.g. "IR-CHS002") are
    # searched even when the extraction step drops them — a model number isn't a
    # "product name", so the LLM often omits it.
    existing_terms = " ".join((it.get("product") or "") for it in extracted).lower()
    _pwords = re.split(r"[\s,]+", prompt or "")
    for _i, _w in enumerate(_pwords):
        tok = _w.strip(".:;()")
        if (any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok)
                and len(tok) >= 4 and ('-' in tok or '/' in tok or len(tok) >= 5)
                and tok.lower() not in existing_terms and search_catalog(tok)):
            _qty = int(_pwords[_i - 1]) if _i > 0 and _pwords[_i - 1].isdigit() else 1
            extracted.append({"product": tok, "qty": _qty})
            existing_terms += " " + tok.lower()

    def deterministic_match(term):
        raw = re.findall(r"[a-z0-9]+", term.lower())
        core = [t for t in raw if len(t) >= 3 and t.isalpha() and t not in units]
        spec = [t for t in raw if any(c.isdigit() for c in t) or (t.isalpha() and len(t) < 3)]
        if not core:
            return None
        def covered(word, pl, pl_ns):
            forms = {word}
            if word.endswith('s') and len(word) > 3:
                forms.add(word[:-1])
            else:
                forms.add(word + 's')
            return any(f in pl or f in pl_ns for f in forms)
        best = None
        for p in all_products:
            pl = (p or '').lower()
            pl_ns = pl.replace(" ", "")
            if all(covered(w, pl, pl_ns) for w in core):
                cand = (-sum(1 for s in spec if s in pl), len(pl), p)
                if best is None or cand < best:
                    best = cand
        return best[2] if best else None

    # Resolve everything deterministically first; only the leftovers need the
    # (slower, token-heavy, rate-limit-prone) LLM semantic call.
    det_map = {}
    for it in extracted:
        term = (it.get("product") or "").strip()
        if term:
            det_map[term.lower()] = deterministic_match(term)
    unresolved = [it for it in extracted if not det_map.get((it.get("product") or "").strip().lower())]

    # Step 2b: LLM semantic mapping — only for items deterministic match missed
    def _shortlist(terms, pool, per_term=12, cap=70):
        """Catalog names plausibly related to what was actually asked for.

        This replaced sending `pool[:350]` — the first 350 products
        alphabetically, chosen without reference to the request. That was the
        worst of both worlds: it produced 5,000-token requests (which tripped
        Groq's per-request limit and burned the daily quota) while often not
        containing the right product at all. A request for "chef knife" was
        sent 350 names that might include no knives.
        """
        picked, seen = [], set()
        for t in terms:
            words = {w for w in re.findall(r"[a-z]{3,}", (t or "").lower())}
            if not words:
                continue
            scored = []
            for p in pool:
                pl = (p or "").lower()
                hits = sum(1 for w in words if w in pl)
                if hits:
                    scored.append((hits, -len(pl), p))   # more words, then tighter name
            scored.sort(reverse=True)
            for _h, _l, p in scored[:per_term]:
                if p not in seen:
                    seen.add(p)
                    picked.append(p)
            if len(picked) >= cap:
                break
        return picked[:cap]

    semantic_map = {}  # requested_term → matched catalog product name (or None)
    if unresolved and all_products:
        try:
            # Only ask about a bounded number of items at once — a 700-row BOQ
            # left hundreds unresolved and put them all in one request.
            batch = unresolved[:40]
            candidates = _shortlist([i.get("product", "") for i in batch], all_products)
            if not candidates:
                # Nothing in the catalog shares a word with the request (e.g.
                # "laptop"). Asking the model to pick from an empty list wastes
                # a call and invites a made-up answer. Not an error — a skip.
                print(f"Semantic mapping skipped: nothing in the catalog resembles "
                      f"{[i.get('product') for i in batch][:5]}")
                candidates = []
            items_str    = ", ".join([f"{i['product']} (qty:{i.get('qty',1)})" for i in batch])
            products_str = "\n".join(candidates)
            sem_resp = candidates and groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content":
                     "You are a hotel supply product matching assistant. "
                     "Map each requested item to the BEST matching product from the catalog. "
                     "Return ONLY valid JSON: {\"mappings\":[{\"requested\":\"towels\",\"matched\":\"Bath Towel\"},{\"requested\":\"laptop\",\"matched\":null}]} "
                     "Rules: "
                     "- Context is HOTEL/HOSPITALITY supplies "
                     "- Use semantic understanding: 'towels'→'Bath Towel', 'kettles'→'Electric Kettle', 'dryers'→'Hair Dryer', 'pillowcases'→'Pillowcase', 'bed sheets'→'Bed Sheet' "
                     "- Only match if the product is GENUINELY the same item (e.g. 'chairs' should NOT match 'wheel chair' or 'bench' — those are completely different things) "
                     "- 'chairs' = seating furniture. 'wheel chair' = medical equipment. These are NOT the same. Set matched to null if no genuine match. "
                     "- Match plural/singular freely: 'dryers' → 'Hair Dryer', 'towels' → 'Bath Towel' "
                     "- If no genuine match exists in catalog, set matched to null"},
                    {"role": "user", "content":
                     f"Items requested: {items_str}\n\nCatalog products:\n{products_str}"}
                ],
                max_tokens=500, temperature=0.1
            )
            sem_raw = sem_resp.choices[0].message.content.strip() if sem_resp else "{}"
            sem_raw = re.sub(r"```[a-z]*\n?", "", sem_raw).strip().rstrip("```")
            sem_data = json.loads(sem_raw)
            for m in sem_data.get("mappings", []):
                req_key = (m.get("requested") or "").lower().strip()
                matched = m.get("matched")
                semantic_map[req_key] = matched
        except Exception as e:
            print(f"Semantic mapping error (non-fatal): {e}")
            # Fall back to keyword matching if semantic mapping fails

    result_items = []
    not_found    = []

    for item in extracted:
        original_kw = item.get("product", "").strip()
        # BOQ-file rows can supply a richer search_term (product + model_no +
        # specification) than the plain label — falls back to original_kw for
        # the free-text prompt flow, which has no such field.
        search_term = (item.get("search_term") or original_kw).strip()
        kw  = original_kw.upper()
        qty = int(item.get("qty") or 1)
        if not kw:
            continue

        # Search across NAME + MODEL NO + SPECIFICATION; returns all candidates.
        variants = search_catalog(search_term)

        # Synonym fallback: if nothing matched directly (e.g. "dustbin"→"bin"),
        # use the LLM's suggested product name and search again.
        if not variants:
            sem_match = det_map.get(original_kw.lower()) or semantic_map.get(original_kw.lower())
            # Sanity-check the LLM's suggestion before trusting it. Asked for
            # "waste bin" it proposed "Insulated Ice Box" — no word in common,
            # yet the result was priced with full confidence at Rs 7,380.
            # Requiring a shared significant word keeps genuine synonym hops
            # ("dustbin" -> "Trash Bin") while rejecting free association.
            if sem_match:
                req_words = {w for w in re.findall(r"[a-z]+", original_kw.lower())
                             if len(w) >= 3 and w not in units}
                sug_words = {w for w in re.findall(r"[a-z]+", str(sem_match).lower())
                             if len(w) >= 3 and w not in units}
                # Check the HEAD noun — the last significant word, which
                # carries the product type ("waste BIN", "chef KNIFE"). An
                # adjective in common is not enough: "waste bin" vs
                # "Insulated Ice Box" shares nothing meaningful, while
                # "waste bin" vs "DUSTBIN" shares the head as a compound.
                head = next((w for w in reversed(
                    [w for w in re.findall(r"[a-z]+", original_kw.lower())
                     if len(w) >= 3 and w not in units])), None)
                related = head is not None and any(
                    head == b or head in b or b in head for b in sug_words)
                if not req_words or related:
                    variants = search_catalog(sem_match)
                else:
                    print(f"Rejected semantic guess: {original_kw!r} -> {sem_match!r} "
                          f"(no shared term)")

        if not variants:
            not_found.append(original_kw)
            continue

        tiers = [t for t in (tiers_req or ["3star"]) if t in ("3star", "4star")] or ["3star"]

        def _normalize(v):
            # Master-table rows use original_model/price_3star/price_4star —
            # translate to the field names the frontend (switch panel, export,
            # inline edit) already expects, plus keep the full tier breakdown.
            return {
                "product": v.get("product", ""),
                "model_no": v.get("original_model", ""),
                "brand": v.get("brand", ""),
                "specification": v.get("specification", ""),
                # The Master Table has only one descriptive text field — mirror
                "description": v.get("specification", ""),
                "hsn_code": v.get("hsn_code", ""),
                "image_path": v.get("image_path", "") or "",
                "file_name": v.get("file_name", ""),
                "price": float(v.get(f"price_{tiers[0]}") or 0),
                "price_currency": "INR",
                # Master Table's single true cost — carried through so the
                # quotation can show real margin (selling price - cost).
                "cost": float(v.get("cost") or 0),
                # None-aware — a genuine 0% GST product must stay 0%, not
                # silently become 18% ("or 18" would treat 0 as missing).
                "gst_pct": float(v.get("gst_pct")) if v.get("gst_pct") is not None else 18.0,
                "price_3star": float(v.get("price_3star") or 0),
                "price_3star_usd": float(v.get("price_3star_usd") or 0),
                "price_4star": float(v.get("price_4star") or 0),
                "price_4star_usd": float(v.get("price_4star_usd") or 0),
            }

        # Deduplicate by product + model so the switcher shows distinct options
        seen = set(); uniq = []
        for v in variants:
            k = ((v.get("product") or "").strip().lower(), (v.get("original_model") or "").strip().lower())
            if k in seen:
                continue
            seen.add(k); uniq.append(_normalize(v))
        variants_sorted = uniq[:15]          # cap the switch list
        best = variants_sorted[0]

        result_items.append({
            "sl_no":        len(result_items) + 1,
            "product":      best.get("product", ""),
            "qty":          qty,
            "description":  best.get("description", ""),
            "model_no":     best.get("model_no", ""),
            "brand":        best.get("brand", ""),
            "specification":best.get("specification", ""),
            "hsn_code":     best.get("hsn_code", ""),
            "price_per_pc": best.get("price", 0),
            "price_currency":"INR",
            "cost":         best.get("cost", 0),
            # best.get("gst_pct") was already resolved (None-aware) in
            # _normalize() above — don't re-apply "or 18" here, it would
            # wrongly turn a genuine 0% GST product back into 18%.
            "gst_pct":      best.get("gst_pct", 18.0),
            "image_path":   best.get("image_path", "") or "",
            "tiers":        tiers,
            "price_3star":      best.get("price_3star", 0),
            "price_3star_usd":  best.get("price_3star_usd", 0),
            "price_4star":      best.get("price_4star", 0),
            "price_4star_usd":  best.get("price_4star_usd", 0),
            "_variants":    variants_sorted,
            "_requested":   item.get("product", ""),
            "boq_price":    float(item.get("boq_price") or 0),
        })

    return result_items, not_found


@router.post("/api/smart-generate-from-boq")
@limiter.limit("30/minute")
def smart_generate_from_boq(
    request: Request,
    file: UploadFile = File(...),
    client_name: str = Form(""),
    tiers: str = Form("3star"),
    user: dict = Depends(get_current_user),
):
    """Client requirement BOQ upload — the file-based counterpart to
    /api/smart-generate. A client's own Excel (product + qty per row, no
    pricing needed) is parsed and every row matched against the Master
    Table via the exact same resolver, so typing a requirement and
    uploading one produce identically-priced results."""
    try:
        return _strip_cost(_smart_generate_from_boq(file, client_name, tiers, user), user)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")


def _smart_generate_from_boq(file: UploadFile, client_name: str, tiers_str: str, user: dict):
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")

    api_key = os.environ.get("GROQ_API_KEY", "") or GROQ_API_KEY_DEFAULT
    if not api_key:
        raise HTTPException(400, "Groq API key required")

    suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".xls"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_path)
    try:
        _save_upload_validated(file, tmp_path)
        rows, _structure = parse_boq_excel(str(tmp_path), file.filename)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass  # Windows may still hold a lock briefly — non-fatal

    # A client's BOQ often lists the SAME generic category label across many
    # rows (e.g. "Bowl Kitchen S/S Conical" for five different sizes) with the
    # actual distinguishing detail sitting in the MODEL NO / SPECIFICATION
    # columns instead. Matching on the label alone would collapse all of them
    # onto whichever single master-table row scores highest for that label —
    # so the search term folds in model_no + specification too, giving
    # search_catalog's model-number/spec ranking the signal it needs to tell
    # rows apart. "product" itself stays the clean label, used for display
    # and the not_found list.
    def _search_term(r):
        parts = [r.get("product") or "", r.get("model_no") or "", r.get("specification") or ""]
        return " ".join(p.strip() for p in parts if p and p.strip())

    # The client's own price per row (their budget/target, or a competitor's
    # quote) — carried through so it can be compared against our Master Table
    # price for a profit/margin view. Absent entirely for sheets with no
    # price column at all (a pure requirement list).
    extracted = [
        {"product": r.get("product", ""), "search_term": _search_term(r),
         "qty": int(r.get("qty") or 1), "boq_price": float(r.get("price") or 0)}
        for r in rows if (r.get("product") or "").strip()
    ]
    if not extracted:
        raise HTTPException(400, "No product/quantity rows could be read from this file.")

    has_boq_pricing = any(it["boq_price"] > 0 for it in extracted)

    tiers = [t.strip() for t in (tiers_str or "").split(",") if t.strip() in ("3star", "4star")] or ["3star"]

    groq_client = Groq(api_key=api_key)
    conn = get_db()
    result_items, not_found = _resolve_master_matches(conn, extracted, [], tiers, groq_client, prompt="")

    ref_no = f"QT-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    data = {"ref_no": ref_no, "client_name": client_name, "has_boq_pricing": has_boq_pricing,
            "items": result_items, "not_found": not_found}

    clean_items = [{k: v for k, v in i.items() if not k.startswith("_")} for i in result_items]
    data_db = {**data, "items": clean_items}
    cur = conn.execute(
        "INSERT INTO quotations (ref_no,client_name,items_json,status,created_by,created_at) VALUES (?,?,?,?,?,?)",
        (ref_no, client_name, json.dumps(data_db), "draft", user["id"], datetime.now().isoformat())
    )
    data["id"] = cur.lastrowid
    conn.commit()
    conn.close()
    log_action(user, "smart_generate_from_boq", target=ref_no)
    return data




@router.post("/api/build-quotation")
def build_quotation(req: BuildQuotationRequest, user: dict = Depends(get_current_user)):
    """Step 2: save user-selected variants as a proper quotation."""
    ref_no = f"QT-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    enriched = []
    for i, item in enumerate(req.items):
        enriched.append({
            "sl_no": i + 1,
            "product":       item.get("product", ""),
            "qty":           item.get("qty", 1),
            "description":   item.get("description", ""),
            "model_no":      item.get("model_no", ""),
            "brand":         item.get("brand", ""),
            "specification": item.get("specification", ""),
            "hsn_code":      item.get("hsn_code", ""),
            "price_per_pc":  float(item.get("price", 0) or item.get("price_per_pc", 0)),
            "price_currency":item.get("price_currency", "INR"),
            "gst_pct":       float(item.get("gst_pct", 18)),
            "image_path":    item.get("image_path", ""),
        })
    data = {"ref_no": ref_no, "client_name": req.client_name, "items": enriched}
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO quotations (ref_no, client_name, items_json, status, created_by, created_at) VALUES (?,?,?,?,?,?)",
        (ref_no, req.client_name, json.dumps(data), "draft", user["id"], datetime.now().isoformat())
    )
    data["id"] = cur.lastrowid
    conn.commit()
    conn.close()
    log_action(user, "build_quotation", target=ref_no)
    return data


@router.get("/api/quotations")
def list_quotations(status: str = None, user: dict = Depends(get_current_user)):
    conn = get_db()
    # Admin/Manager see everyone's quotations (needed to review/approve);
    # Employees see only their own.
    own_only = user["role"] == "employee"
    if status and own_only:
        rows = conn.execute("SELECT * FROM quotations WHERE status=? AND created_by=? ORDER BY created_at DESC", (status, user["id"])).fetchall()
    elif status:
        rows = conn.execute("SELECT * FROM quotations WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
    elif own_only:
        rows = conn.execute("SELECT * FROM quotations WHERE created_by=? ORDER BY created_at DESC", (user["id"],)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM quotations ORDER BY created_at DESC").fetchall()
    conn.close()
    result = []
    for r in rows:
        q = dict(r)
        q["items_json"] = json.loads(q["items_json"])
        result.append(q)
    return result


class UpdateItemsRequest(BaseModel):
    items: list
    client_name: str = ""


@router.put("/api/quotations/{qid}")
def update_quotation(qid: int, req: UpdateItemsRequest, user: dict = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    _check_quote_access(row, user)
    data = json.loads(row["items_json"])
    data["items"] = req.items
    if req.client_name:
        data["client_name"] = req.client_name
    for item in data["items"]:
        qty = int(item.get("qty") or 0)
        price = float(item.get("price_per_pc") or 0)
        gst_pct = float(item.get("gst_pct") or 18)
        item["amount"] = qty * price
        item["gst_value"] = item["amount"] * gst_pct / 100
    conn.execute("UPDATE quotations SET items_json=?, client_name=? WHERE id=?",
                 (json.dumps(data), data["client_name"], qid))
    conn.commit()
    conn.close()
    data["id"] = qid
    log_action(user, "edit_quotation", target=str(qid))
    return data


@router.get("/api/download/{qid}")
def download_quotation(qid: int, user: dict = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
    tmpl = get_latest_template(conn)
    conn.close()
    if not row:
        raise HTTPException(404, "Not found")
    _check_quote_access(row, user)

    data = json.loads(row["items_json"])
    items = data.get("items", [])

    path = build_company_quotation(data, items)

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"Quote_{data.get('ref_no', qid)}.xlsx"
    )


class FeedbackRequest(BaseModel):
    quotation_id: int
    rating: str
    missing_items: str = ""


@router.post("/api/feedback")
def submit_feedback(req: FeedbackRequest, user: dict = Depends(get_current_user)):
    conn = get_db()
    conn.execute(
        "INSERT INTO feedback (quotation_id, rating, missing_items, created_at) VALUES (?,?,?,?)",
        (req.quotation_id, req.rating, req.missing_items, datetime.now().isoformat())
    )
    if req.rating == "good":
        conn.execute("UPDATE quotations SET status='approved' WHERE id=?", (req.quotation_id,))
    conn.commit()
    conn.close()
    return {"message": "Feedback saved. Thank you!" if req.rating == "good" else "Feedback recorded — we'll improve!"}


# NOTE: the static "clear-all" route MUST be declared before the dynamic
# "{qid}" route, otherwise FastAPI matches "clear-all" as a qid and it fails.
@router.delete("/api/quotations/clear-all")
def clear_all_quotations(admin: dict = Depends(require_role("admin"))):
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM quotations").fetchone()[0]
    conn.execute("DELETE FROM quotations")
    conn.commit()
    conn.close()
    log_action(admin, "clear_all_quotations", after={"deleted": count})
    return {"deleted": count}


@router.delete("/api/quotations/{qid}")
def delete_quotation(qid: int, user: dict = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute("SELECT created_by FROM quotations WHERE id=?", (qid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Not found")
    if user["role"] != "admin" and row["created_by"] != user["id"]:
        conn.close()
        raise HTTPException(403, "You can only delete your own quotations")
    conn.execute("DELETE FROM quotations WHERE id=?", (qid,))
    conn.commit()
    conn.close()
    log_action(user, "delete_quotation", target=str(qid))
    return {"deleted": qid}
