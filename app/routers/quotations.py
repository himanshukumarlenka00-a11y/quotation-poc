import os, re, json, base64, tempfile
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from groq import Groq
from app.config import limiter, GROQ_API_KEY_DEFAULT, CEREBRAS_API_KEY, CEREBRAS_MODEL, server_error
from app.db import get_db
from app.auth import get_current_user, require_role, _check_quote_access, log_action
from app.matching import get_boq_context, get_feedback_context, generate_ref_no, get_latest_template
from app.export import build_company_quotation
from app.images import _save_image_to_disk
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
        raise server_error(e, "Request")

def _llm_chat(groq_client, messages, max_tokens, temperature):
    """One LLM chat call: Groq first; if Groq is rate-limited and a Cerebras
    key is configured, the same prompt goes to Cerebras (OpenAI-compatible,
    plain stdlib HTTP — no extra dependency). Returns the content string.
    Cerebras gets a higher token budget because gpt-oss spends some of it on
    reasoning before the answer."""
    try:
        r = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile", messages=messages,
            max_tokens=max_tokens, temperature=temperature)
        return r.choices[0].message.content
    except Exception as e:
        rate_limited = "429" in str(e) or "rate limit" in str(e).lower()
        if not (rate_limited and CEREBRAS_API_KEY):
            raise
        try:
            import urllib.request
            body = json.dumps({"model": CEREBRAS_MODEL, "messages": messages,
                               "max_tokens": max(max_tokens * 4, 3000),
                               "temperature": temperature}).encode()
            http_req = urllib.request.Request(
                "https://api.cerebras.ai/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {CEREBRAS_API_KEY}",
                         # Their edge blocks the default Python UA with a 403
                         "User-Agent": "quotegen/1.0"})
            with urllib.request.urlopen(http_req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            print("LLM fallback: Groq 429 -> Cerebras answered")
            return data["choices"][0]["message"]["content"]
        except Exception as ce:
            # Fallback failing must not worsen the error — surface the
            # original Groq rate limit (clean, retryable) instead.
            print(f"LLM fallback failed (non-fatal): {ce}")
            raise e


def _smart_generate(req: GenerateRequest, user: dict):
    api_key = os.environ.get("GROQ_API_KEY", "") or GROQ_API_KEY_DEFAULT
    if not api_key:
        raise HTTPException(400, "Groq API key required")

    groq_client = Groq(api_key=api_key)

    # Step 1: read the request. A plainly list-shaped prompt is parsed here and
    # never reaches the LLM — no tokens, no latency, no rate limit. Anything
    # that reads like prose falls through to the model below.
    extracted = _parse_items_deterministically(req.prompt)
    if extracted:
        print(f"Extraction: parsed {len(extracted)} item(s) without the LLM")
        return _finish_smart_generate(req, user, extracted, groq_client)

    try:
        raw = _llm_chat(groq_client,
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
                 "Keep price constraints written after a product ('under 1000', 'below 1k', "
                 "'>= 500', 'above Rs 2000', 'between 200 and 2000') as part of that product's text — never drop them. "
                 "Keep each distinct requested item as its own entry; never merge two different items. "
                 "Default qty to 1 if not stated. Do not add any product not mentioned."},
                {"role": "user", "content": req.prompt}
            ],
            max_tokens=1200, temperature=0.1   # long pasted lists overflowed 400 mid-JSON
        )
        raw = (raw or "").strip()
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip().rstrip("```")
        # The model sometimes wraps or trails the JSON with prose — parse the
        # outermost {...} slice instead of failing on the decoration.
        if "{" in raw:
            raw = raw[raw.find("{"):raw.rfind("}") + 1]
        extracted = json.loads(raw).get("items", [])
    except Exception as e:
        # Surface Groq rate limits as a clear, retryable 429 instead of a 500
        if "429" in str(e) or "rate limit" in str(e).lower():
            raise HTTPException(429, "Server is busy right now (rate limit). Please wait a few seconds and try again.")
        raise HTTPException(500, f"Extraction error: {e}")

    return _finish_smart_generate(req, user, extracted, groq_client)


def _finish_smart_generate(req, user, extracted, groq_client):
    """Match, price and save — shared by both extraction paths.

    Kept as one function so a deterministically parsed prompt and an
    LLM-extracted one cannot diverge in how they resolve or store a quotation.
    """
    conn = get_db()
    result_items, not_found = _resolve_master_matches(conn, extracted, req.catalogs, req.tiers, groq_client, prompt=req.prompt)

    if all(i.get("not_in_catalog") for i in result_items):
        # Nothing matched (placeholders don't count) — an empty quotation is
        # clutter, not a record.
        conn.close()
        return {"ref_no": None, "client_name": req.client_name,
                "items": [], "not_found": not_found, "unsaved": True}

    ref_no = f"QT-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    data   = {"ref_no": ref_no, "client_name": req.client_name,
              # Which tiers the user actually asked for — [] means none
              # (single plain PRICE column in the quote view).
              "tiers": [t for t in (req.tiers or []) if t in ("3star", "4star")],
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


def _norm_phrase(s):
    """Normalize a requested phrase for correction lookup: lowercase, strip
    punctuation edges, collapse whitespace. Exact-match only by design — a
    fuzzy learned match firing on the wrong phrase would be precisely the bug
    this table exists to prevent."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(s or "").lower())).strip()


_SQL_NORM_MODEL = ("REPLACE(REPLACE(REPLACE(REPLACE(LOWER(original_model),'-',''),' ',''),'.',''),'/','')")


def _lookup_by_model(conn, req_model):
    """Master rows whose model matches `req_model`, brand-prefix tolerant.

    Clients write the model the way it reads on their sheet — "[KMW-TB770]" —
    while the master table stores original_model='TB770' and keeps the brand
    in the product name. A plain equality check therefore misses every
    branded model code, which is exactly how a list of 8 real KMW products
    came back as "nothing matched".

    So: exact normalised equality first, then rows whose model is a SUFFIX of
    what was asked (kmwtb770 ends with tb770). The suffix arm needs >=3 chars
    to avoid a two-character model swallowing everything, and it can't create
    the substitution the model gate exists to prevent — wcce001 does not end
    with wcce002. Exact matches are returned first so they always win."""
    rm = re.sub(r"[^a-z0-9]", "", (req_model or "").lower())
    if len(rm) < 3:
        return []
    # Indexed fast path first: the normalising REPLACE() below can't use
    # idx_master_model, so it scans every row — ~95ms per item at 52k rows,
    # i.e. a minute of pure scanning on a 700-row BOQ. Plain equality on the
    # obvious candidates ("KMW-TB770" -> also try "TB770") does use the index
    # and settles the overwhelming majority of lines in microseconds.
    raw = (req_model or "").strip()
    cands = {raw, raw.upper()}
    if "-" in raw:
        for part in (raw.split("-", 1)[1], raw.rsplit("-", 1)[-1]):
            if len(re.sub(r"[^a-z0-9]", "", part.lower())) >= 3:
                cands |= {part.strip(), part.strip().upper()}
    cands = [c for c in cands if c]
    try:
        hit = conn.execute(
            f"SELECT * FROM master_products WHERE original_model IN "
            f"({','.join('?' * len(cands))})", cands).fetchall()
        if hit:
            return [dict(r) for r in hit]
    except Exception as e:
        print(f"Model fast-path failed (non-fatal, falling back to scan): {e}")
    try:
        rows = conn.execute(
            f"""SELECT * FROM master_products
                 WHERE original_model IS NOT NULL AND TRIM(original_model) != ''
                   AND ( {_SQL_NORM_MODEL} = ?
                         OR ( LENGTH({_SQL_NORM_MODEL}) >= 3
                              AND ? LIKE '%' || {_SQL_NORM_MODEL} ) )
                 ORDER BY CASE WHEN {_SQL_NORM_MODEL} = ? THEN 0 ELSE 1 END,
                          LENGTH({_SQL_NORM_MODEL}) DESC""",
            (rm, rm, rm)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"Model lookup failed (non-fatal): {e}")
        return []


def _lookup_correction(conn, phrase):
    """A master-table row a human previously said this phrase means, or None.

    Resolves the stored (product, original_model) TEXT identity against the
    live master table — if the product has since been removed, this misses and
    matching falls through to the normal path rather than serving something
    stale."""
    pn = _norm_phrase(phrase)
    if not pn:
        return None
    try:
        r = conn.execute("SELECT product, original_model FROM match_corrections "
                         "WHERE phrase_norm=?", (pn,)).fetchone()
        if not r:
            return None
        row = conn.execute(
            "SELECT * FROM master_products WHERE LOWER(TRIM(product))=LOWER(TRIM(?)) "
            "AND LOWER(TRIM(COALESCE(original_model,'')))=LOWER(TRIM(COALESCE(?,''))) LIMIT 1",
            (r["product"], r["original_model"])).fetchone()
        return dict(row) if row else None
    except Exception:
        return None       # a missing table must never break matching


# Words that mean the prompt is prose rather than a list. Their presence sends
# the request to the LLM, because a parser that guesses at sentence structure
# would quietly produce a wrong quotation.
_PROSE_MARKERS = re.compile(
    r"\b(we|i|need|needs|needed|require|requires|required|want|wants|please|"
    r"looking|setup|set up|for the|opening|room|hotel|kitchen|section|"
    r"department|also|plus|with|without|including|approx|around|about)\b",
    re.I)


def _parse_items_deterministically(prompt):
    """Read a list-shaped prompt without calling the LLM.

    "100 soup bowl, 60 ice box, 25 pizza tray" is a list, not language — a
    strict pattern reads it exactly, instantly, and cannot be rate-limited.
    On a sample of real prompts this handles about half of them, which is
    half the extraction calls gone.

    Deliberately conservative: it returns None at the first sign of doubt, so
    anything ambiguous still goes to the model. A false parse here would put
    wrong quantities on a customer quotation, which is far worse than the cost
    of an API call.
    """
    p = (prompt or "").strip()
    if not p or len(p) > 1000:
        return None

    # Pasted-list shape: one product per LINE with the qty at the END —
    # "MIRROR KORIKO BOSTON SHAKER [WBS001-SS]  4". Common when copying
    # straight out of a client's sheet. Model codes in the text make the
    # matcher's model-number scoring kick in, so these lines never need
    # the LLM. All-or-nothing: one non-conforming line sends the whole
    # prompt to the model rather than risking wrong quantities.
    lines = [l.strip().strip('"') for l in p.splitlines() if l.strip().strip('"')]
    # A single line qualifies too when it carries a bracketed model code —
    # "PRODUCT [WCCE001-SS] 5" is unambiguously a list row, and sending it to
    # the LLM burns quota to learn nothing (today's 429s came from exactly
    # this shape).
    if len(lines) >= 2 or (len(lines) == 1 and re.search(r"\[[^\]\[]+\]", lines[0])):
        items = []
        for line in lines:
            # Trailing qty is optional — a bare product line means qty 1.
            # A line reading like a sentence sends the prompt to the LLM —
            # unless it carries a bracketed model code, which outranks any
            # prose word (product names legitimately contain "with", "for
            # the", etc.: "caddy with 6 holders [GC002]").
            if len(line) > 90 or (_PROSE_MARKERS.search(line)
                                  and not re.search(r"\[[^\]\[]+\]", line)):
                items = None
                break
            m = re.match(r"^(.{3,90}?)(?:[\s\-–]+(\d{1,5}))?$", line)
            if not m or not re.search(r"[A-Za-z]", m.group(1)):
                items = None
                break
            items.append({"product": m.group(1).strip(" .-"),
                          "qty": int(m.group(2)) if m.group(2) else 1})
        if items:
            return items

    if _PROSE_MARKERS.search(p):
        return None

    segments = [s.strip() for s in re.split(r",|;|\band\b", p, flags=re.I) if s.strip()]
    if not segments or len(segments) > 30:
        return None

    items = []
    for seg in segments:
        # "<qty> <product>" — quantity first, which is how every real prompt
        # in this app is written. Anything else is not a list.
        m = re.match(r"^(\d{1,6})\s*(?:x|nos\.?|pcs\.?|pieces?)?\s+(.{2,70})$", seg, re.I)
        if not m:
            return None
        qty, product = int(m.group(1)), m.group(2).strip(" .")
        # A product must contain a letter; "50 200" is not a product.
        if qty <= 0 or not re.search(r"[A-Za-z]", product):
            return None
        items.append({"product": product, "qty": qty})
    return items or None


_PRICE_NUM = r"(?:rs\.?|inr|₹)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?"
# Word forms need \b on their own (so "thunder 500" isn't "under 500");
# symbol forms (<, <=) sit next to non-word chars where \b can't match.
_MAX_WORDS = r"(?:\b(?:under|below|less\s+th[ae]n|cheaper\s+than|up\s?to|within|max(?:imum)?|budget)\b|<=|<)"
_MIN_WORDS = r"(?:\b(?:above|over|more\s+than|greater\s+than|at\s+least|min(?:imum)?)\b|>=|>)"


def _strip_price_constraint(text):
    """Pull a per-unit price constraint out of a requested phrase.

    "cups under 1k" -> ("cups", None, 1000.0); "cups >= 500" -> ("cups", 500.0, None).
    Constraints are per PIECE — master-table prices are per piece.
    Returns (clean_text, min_price, max_price).
    """
    def _num(m, g=1):
        v = float(m.group(g).replace(",", ""))
        return v * 1000 if m.group(g + 1) else v
    t, pmin, pmax = str(text or ""), None, None
    # Ranges first: "between 200 and 2000", "range 200 to 2k", "200 - 2000".
    # ponytail: bare "200-2000" (no spaces/₹/k) is left alone — it looks like a
    # model code; loosen only if a real prompt needs it.
    m = re.search(rf"\b(?:between|range|price)\s*{_PRICE_NUM}\s*(?:and|to|-)\s*{_PRICE_NUM}\b", t, re.I) or \
        re.search(rf"{_PRICE_NUM}\s+(?:to|-)\s+{_PRICE_NUM}\b", t, re.I)
    if m:
        a, b = _num(m, 1), _num(m, 3)
        pmin, pmax = min(a, b), max(a, b)
        return re.sub(r"\s+", " ", t.replace(m.group(0), " ")).strip(" ,.-"), pmin, pmax
    m = re.search(rf"{_MAX_WORDS}\s*{_PRICE_NUM}\b", t, re.I)
    if m:
        pmax = _num(m); t = t.replace(m.group(0), " ")
    m = re.search(rf"{_MIN_WORDS}\s*{_PRICE_NUM}\b", t, re.I)
    if m:
        pmin = _num(m); t = t.replace(m.group(0), " ")
    return re.sub(r"\s+", " ", t).strip(" ,.-"), pmin, pmax


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
            # _suggestions carries raw master rows too — the same devtools
            # exposure the _variants strip exists to prevent.
            for key in ("_variants", "_suggestions"):
                for v in item.get(key) or []:
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
    # Items carrying an explicit model number skip the semantic call — the
    # model-number gate below refuses lookalike substitutes anyway, so asking
    # the LLM for one would burn quota to produce a rejected answer.
    def _has_model(it):
        if (it.get("model_no") or "").strip():
            return True
        return bool(re.search(r"\[[^\]\[]*\d[^\]\[]*\]", it.get("product") or ""))
    unresolved = [it for it in extracted
                  if not det_map.get((it.get("product") or "").strip().lower())
                  and not _has_model(it)]

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
            sem_resp = candidates and _llm_chat(groq_client,
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
            sem_raw = sem_resp.strip() if sem_resp else "{}"
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

    def suggest_catalog(term, limit=6):
        """Loose candidates for a request that nothing matched confidently.

        search_catalog() refuses to guess: it needs most of the request's
        words present in the product name, so "Hair Dryer, Color - Black /
        Grey Wall-Mounted" scores 2/7 against "HAIR DRYER" and returns
        nothing at all. That refusal is right for auto-selecting — it is what
        stops "waste bin" becoming "Ice bin module" — but the row it discards
        is often exactly what the user wanted, and an empty Find box makes
        them retype what we already knew.

        Ranks by how many of the request's significant words appear in the
        name. Shown as suggestions only; the line stays a placeholder until a
        human picks one."""
        toks = re.findall(r"[a-z0-9]+", (term or "").lower())
        core = [w for w in toks if len(w) >= 3 and w.isalpha() and w not in units]
        if not core:
            return []
        out = []
        for r in rows_pool:
            name = (r.get("product") or "").lower()
            name_ns = name.replace(" ", "")
            # Earlier words carry more weight: these descriptions are written
            # "<product>, <qualifiers>", so "hair"/"dryer" identify the item
            # while "black"/"wall"/"mounted" merely describe it. Weighting by
            # position is what keeps "WALL-MOUNTED ASHTRAY" out of the results
            # for "Hair Dryer, Color - Black / Grey Wall-Mounted" — verified
            # against the real 52k catalogue.
            hits = sum((len(core) - i) for i, w in enumerate(core)
                       if _covered(w, name, name_ns))
            if not hits:
                continue
            # shorter names break ties, so a bare "HAIR DRYER" outranks
            # "HAIR DRYER BAG"
            score = hits * 1000 - min(len(name), 120)
            if (r.get("price_3star") or 0) > 0:
                score += 50
            if r.get("image_path"):
                score += 10
            out.append((score, r))
        out.sort(key=lambda x: -x[0])
        return [dict(r) for _, r in out[:limit]]

    def _add_placeholder(label, qty, suggestions=None):
        # Not-in-catalogue rows keep their place in the quote (name + qty from
        # the client, everything else blank) so the sequence order survives —
        # the user fills the price inline or swaps the product later.
        result_items.append({
            "sl_no": len(result_items) + 1, "product": label, "qty": qty,
            "description": "", "model_no": "", "brand": "", "specification": "",
            "hsn_code": "", "price_per_pc": 0, "price_currency": "INR",
            "cost": 0, "gst_pct": 18.0, "image_path": "",
            "tiers": [t for t in (tiers_req or ["3star"]) if t in ("3star", "4star")] or ["3star"],
            "price_3star": 0, "price_3star_usd": 0, "price_4star": 0, "price_4star_usd": 0,
            "_variants": [], "_requested": label, "requested": label,
            "matched_by": "not_found", "not_in_catalog": True, "boq_price": 0,
            # Best-effort candidates so the Find panel opens with something
            # instead of an empty box. Suggestions only — never auto-applied.
            "_suggestions": suggestions or [],
        })

    for item in extracted:
        original_kw = item.get("product", "").strip()
        # BOQ-file rows can supply a richer search_term (product + model_no +
        # specification) than the plain label — falls back to original_kw for
        # the free-text prompt flow, which has no such field.
        search_term = (item.get("search_term") or original_kw).strip()
        # "cups under 1k" — the constraint filters candidates, it is not part
        # of the product name (it would poison name matching and corrections).
        # Only the typed label carries constraints: a BOQ search_term embeds
        # SPECIFICATION text, where "capacity up to 1000 ml" is a spec, not a
        # price budget — stripping there mispriced real coverage checks.
        original_kw, pmin, pmax = _strip_price_constraint(original_kw)
        if not item.get("search_term"):
            search_term = original_kw
        kw  = original_kw.upper()
        qty = int(item.get("qty") or 1)
        if not kw:
            continue

        # Search across NAME + MODEL NO + SPECIFICATION; returns all candidates.
        variants = search_catalog(search_term)

        # A human correction outranks everything below. If someone previously
        # fixed what this exact phrase resolves to, put their pick first —
        # the matcher's candidates stay behind it as switchable variants.
        corr_row = _lookup_correction(conn, original_kw)
        matched_by = "ai"
        if corr_row:
            matched_by = "learned"
            ck = ((corr_row.get("product") or "").strip().lower(),
                  (corr_row.get("original_model") or "").strip().lower())
            variants = [corr_row] + [
                v for v in variants
                if ((v.get("product") or "").strip().lower(),
                    (v.get("original_model") or "").strip().lower()) != ck]

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

        # An explicit model number is a hard constraint, never a hint. A line
        # like "... chiller with ear [WCCE001-SS]" must NOT silently become
        # WCCE002-SS just because the names read alike — a wrong model on a
        # customer quotation is worse than an honest blank. Exact model match
        # goes first (rest stay as switch options); no exact match -> the row
        # stays a placeholder for the user to Find/fill by hand. A human
        # correction ("learned") still outranks this gate.
        req_model = (item.get("model_no") or "").strip()
        if not req_model:
            brackets = re.findall(r"\[([^\]\[]{2,40})\]", item.get("product", ""))
            req_model = brackets[-1].strip() if brackets else ""
        # Runs whether or not the NAME search found anything: the model is the
        # stronger signal, and the client's label often describes the product
        # differently than our catalogue does ("1/4-Segment colander" vs
        # "Triangle Pasta Basket") while naming the very same model code.
        if req_model and matched_by != "learned" and any(c.isdigit() for c in req_model):
            by_model = _lookup_by_model(conn, req_model)
            if by_model:
                key = lambda v: ((v.get("product") or "").strip().lower(),
                                 (v.get("original_model") or "").strip().lower())
                seen_m = {key(v) for v in by_model}
                variants = by_model + [v for v in variants if key(v) not in seen_m]
            else:
                not_found.append(f"{original_kw} (model {req_model} not in master — nothing substituted)")
                _add_placeholder(original_kw, qty, suggest_catalog(search_term))
                continue

        if not variants:
            not_found.append(original_kw)
            _add_placeholder(original_kw, qty, suggest_catalog(search_term))
            continue

        tiers = [t for t in (tiers_req or ["3star"]) if t in ("3star", "4star")] or ["3star"]

        if pmin is not None or pmax is not None:
            pf = "price_4star" if tiers[0] == "4star" else "price_3star"
            priced = [v for v in variants
                      if (v.get(pf) or 0) > 0
                      and (pmin is None or v[pf] >= pmin)
                      and (pmax is None or v[pf] <= pmax)]
            if not priced:
                # Honest miss beats quoting outside the asked budget.
                have = sorted(v[pf] for v in variants if (v.get(pf) or 0) > 0)
                closest = (max(have) if pmin is None else min(have)) if have else None
                cons = " & ".join(filter(None, [
                    f"over ₹{pmin:g}" if pmin is not None else None,
                    f"under ₹{pmax:g}" if pmax is not None else None]))
                not_found.append(f"{original_kw} ({cons}"
                                 + (f" — closest is ₹{closest:g}" if closest else "") + ")")
                _add_placeholder(original_kw, qty)
                continue
            variants = priced

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
                # Pre-bulk-discount snapshot (see master_table.py) — None
                # unless this catalogue has ever had a bulk % applied, so the
                # frontend can tell "never discounted" from "discounted to 0".
                "orig_price_3star": v.get("orig_price_3star"),
                "orig_price_4star": v.get("orig_price_4star"),
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
            "orig_price_3star": best.get("orig_price_3star"),
            "orig_price_4star": best.get("orig_price_4star"),
            "_variants":    variants_sorted,
            "_requested":   item.get("product", ""),
            # Persisted (no underscore) — the learning loop needs to know, at
            # edit time, which phrase produced this line and who matched it.
            # Without `requested` a correction cannot be attributed; without
            # `matched_by` a re-save would re-learn lines a human already set.
            "requested":    item.get("product", ""),
            "matched_by":   matched_by,
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
        raise server_error(e, "Request")


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
        try:
            from app.master_table import detect_file_type
            file_type = detect_file_type(str(tmp_path))
        except Exception:
            file_type = None
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
         "model_no": r.get("model_no", ""),
         "qty": int(r.get("qty") or 1), "boq_price": float(r.get("price") or 0)}
        for r in rows if (r.get("product") or "").strip()
    ]
    if not extracted:
        raise HTTPException(400, "No product/quantity rows could be read from this file.")

    has_boq_pricing = any(it["boq_price"] > 0 for it in extracted)

    tiers_requested = [t.strip() for t in (tiers_str or "").split(",") if t.strip() in ("3star", "4star")]
    tiers = tiers_requested or ["3star"]

    groq_client = Groq(api_key=api_key)
    conn = get_db()
    result_items, not_found = _resolve_master_matches(conn, extracted, [], tiers, groq_client, prompt="")

    if all(i.get("not_in_catalog") for i in result_items):
        # Nothing in the BOQ matched (placeholders don't count) — don't save
        # an empty quotation.
        conn.close()
        return {"ref_no": None, "client_name": client_name, "items": [],
                "not_found": not_found, "unsaved": True}

    ref_no = f"QT-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    data = {"ref_no": ref_no, "client_name": client_name, "has_boq_pricing": has_boq_pricing,
            "file_type": file_type, "tiers": tiers_requested,
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
    if not enriched:
        # Same rule as the typed flow: never save an empty quotation.
        return {"ref_no": None, "client_name": req.client_name,
                "items": [], "unsaved": True}
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
    sales_person: dict | None = None
    bill_to: str | None = None


@router.post("/api/extract-pdf")
async def extract_pdf(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """Pull the text lines out of a (text-based) PDF so they can fill the
    requirement box. Scanned/image PDFs have no text layer — reported
    honestly rather than returning garbage."""
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Only .pdf files are supported here.")
    try:
        from pypdf import PdfReader
        reader = PdfReader(file.file)
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:20])
    except Exception as e:
        raise HTTPException(400, f"Could not read this PDF: {type(e).__name__}")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        raise HTTPException(422, "This PDF has no readable text — it looks like a "
                                 "scanned image. Type or paste the items instead.")
    return {"lines": lines}


@router.post("/api/extract-excel")
async def extract_excel(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """Turn an Excel requirement sheet into text lines for the requirement
    box — same parser the BOQ flows use, so taught columns apply here too."""
    if not (file.filename or "").lower().endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files are supported here.")
    from app.parser import parse_boq_excel
    suffix = ".xlsx" if file.filename.lower().endswith(".xlsx") else ".xls"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(await file.read())
        rows, _ = parse_boq_excel(tmp, file.filename)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    lines = []
    for r in rows:
        prod = (r.get("product") or "").strip()
        if not prod:
            continue
        model = (r.get("model_no") or "").strip()
        if model and model.lower() not in prod.lower():
            prod += f" [{model}]"
        qty = int(r.get("qty") or 1)
        lines.append(f"{prod} {qty}")
    if not lines:
        raise HTTPException(422, "No product rows could be read — check the sheet's "
                                 "column headings (they can be taught on the BOQ Coverage page).")
    return {"lines": lines}


@router.get("/api/sales-persons")
def list_sales_persons(user: dict = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT id, name, phone, email, region FROM sales_persons "
                        "WHERE active=1 ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.put("/api/quotations/{qid}")
def update_quotation(qid: int, req: UpdateItemsRequest, user: dict = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    _check_quote_access(row, user)
    data = json.loads(row["items_json"])

    # A hand-uploaded photo arrives as a base64 data URL in local_image. Park
    # it on disk like every catalogue image instead of embedding it in the
    # quote row: a 2MB photo is ~2.7MB of base64 inside items_json, and the
    # Excel export only ever reads image_path — so an uploaded image looked
    # fine on screen and then came out blank in the XLS. Done here because
    # every save routes through this endpoint, so it covers the manual-entry
    # form and the per-row "Add Image" alike.
    for it in (req.items or []):
        li = it.get("local_image") or ""
        if li.startswith("data:image") and "," in li:
            try:
                h = _save_image_to_disk(base64.b64decode(li.split(",", 1)[1]))
                if h:
                    it["image_path"] = h
                    it["local_image"] = ""
            except Exception as e:
                print(f"Inline image save skipped (non-fatal): {e}")

    # Learn from what the human changed, BEFORE the old state is overwritten.
    # Diffing server-side (rather than trusting the frontend to report edits)
    # catches every way a product can change and cannot be skipped by a buggy
    # client. Lines pair up on their `requested` phrase; a line whose product
    # identity changed is a correction. Qty/price edits are not — the product
    # is the same, the human just tuned the numbers.
    learned = []      # audit-logged AFTER commit — log_action opens its own
                      # connection, and calling it while this write transaction
                      # is still open deadlocks against ourselves ("database is
                      # locked") and silently loses the audit entry.
    try:
        old_by_phrase = {_norm_phrase(i.get("requested")): i
                         for i in data.get("items", []) if i.get("requested")}
        for item in (req.items or []):
            ph = _norm_phrase(item.get("requested") or "")
            old = old_by_phrase.get(ph)
            if not old:
                continue        # nothing to compare against
            ident = lambda x: ((str(x.get("product") or "")).strip().lower(),
                               (str(x.get("model_no") or "")).strip().lower())
            if ident(old) != ident(item):
                # Teaching is OPT-IN: only when the user ticked "remember this
                # choice" in the Switch/Find panel.
                #
                # Switching used to teach automatically, conflating two acts
                # that look identical in the UI: "the matcher was wrong, fix
                # it for good" and "this client wants something else just this
                # once". The second is the common case, and treating it as the
                # first is how "[WCCE001-SS]" got permanently pinned to
                # WCCE002. A one-off preference must never re-point the
                # catalogue mapping for everyone.
                if not item.get("remember"):
                    continue
                conn.execute("""
                    INSERT INTO match_corrections
                        (phrase_norm, product, original_model, corrected_from,
                         corrected_by, created_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(phrase_norm) DO UPDATE SET
                        product=excluded.product,
                        original_model=excluded.original_model,
                        corrected_from=excluded.corrected_from,
                        corrected_by=excluded.corrected_by,
                        created_at=excluded.created_at,
                        source='correction',
                        times_confirmed=match_corrections.times_confirmed+1
                """, (ph, item.get("product") or "", item.get("model_no") or "",
                      old.get("product") or "", user["id"],
                      datetime.now().isoformat()))
                item["matched_by"] = "human"
                learned.append((item.get("requested") or ph,
                                old.get("product"), item.get("product")))
            else:
                # Line saved untouched — a confirmation. Weaker than a
                # correction, so the ON CONFLICT guard only bumps the counter
                # when the stored row already points at this same product;
                # it can never overwrite a human correction aimed elsewhere.
                # No audit entry: every save confirms every untouched line,
                # and logging each would drown the Activity page.
                conn.execute("""
                    INSERT INTO match_corrections
                        (phrase_norm, product, original_model, corrected_by,
                         created_at, source)
                    VALUES (?,?,?,?,?,'confirmed')
                    ON CONFLICT(phrase_norm) DO UPDATE SET
                        times_confirmed = match_corrections.times_confirmed + 1
                    WHERE LOWER(TRIM(match_corrections.product)) = LOWER(TRIM(excluded.product))
                      AND LOWER(TRIM(COALESCE(match_corrections.original_model,''))) =
                          LOWER(TRIM(COALESCE(excluded.original_model,'')))
                """, (ph, item.get("product") or "", item.get("model_no") or "",
                      user["id"], datetime.now().isoformat()))
    except Exception as e:
        print(f"Correction learning skipped (non-fatal): {e}")

    # "remember" is a one-shot instruction, not a property of the line. If it
    # were persisted, every later save of this quote would re-teach the same
    # phrase — and worse, would re-teach it after a subsequent one-off switch.
    for it in (req.items or []):
        it.pop("remember", None)

    data["items"] = req.items
    if req.client_name:
        data["client_name"] = req.client_name
    if req.sales_person is not None:
        data["sales_person"] = req.sales_person
    if req.bill_to is not None:
        data["bill_to"] = req.bill_to
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
    for phrase, was, now in learned:
        log_action(user, "learned_correction", target=phrase,
                   after={"from": was, "to": now})
    return data


@router.post("/api/quotations/{qid}/refresh-prices")
def refresh_quotation_prices(qid: int, user: dict = Depends(get_current_user)):
    """Pull each line's CURRENT master-table 3star/4star price into an
    already-generated quote — for when prices moved in the Master Catalogue
    after this quote was made. Matches by exact (product, model_no); a line
    that no longer resolves (renamed/removed from master) is left untouched
    and counted as skipped, never guessed at.

    The pre-refresh value is kept as prev_price_3star/4star, but ONLY the
    first time a line is ever refreshed — a second refresh must not overwrite
    that reference point, or "Original" would drift to mean "yesterday"
    instead of "when this quote was generated"."""
    conn = get_db()
    row = conn.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Not found")
    _check_quote_access(row, user)
    data = json.loads(row["items_json"])
    items = data.get("items", [])
    updated = skipped = 0
    for item in items:
        product = (item.get("product") or "").strip()
        if not product:
            skipped += 1
            continue
        m = conn.execute(
            "SELECT price_3star, price_3star_usd, price_4star, price_4star_usd "
            "FROM master_products WHERE LOWER(TRIM(product))=LOWER(TRIM(?)) "
            "AND LOWER(TRIM(COALESCE(original_model,'')))=LOWER(TRIM(?)) LIMIT 1",
            (product, (item.get("model_no") or "").strip())
        ).fetchone()
        if not m:
            skipped += 1
            continue
        new3, new4 = float(m["price_3star"] or 0), float(m["price_4star"] or 0)
        old3, old4 = float(item.get("price_3star") or 0), float(item.get("price_4star") or 0)
        if new3 == old3 and new4 == old4:
            continue   # already current — not an error, just nothing to do
        if item.get("prev_price_3star") is None:
            item["prev_price_3star"] = old3
            item["prev_price_4star"] = old4
        item["price_3star"], item["price_3star_usd"] = new3, float(m["price_3star_usd"] or 0)
        item["price_4star"], item["price_4star_usd"] = new4, float(m["price_4star_usd"] or 0)
        updated += 1
    data["items"] = items
    conn.execute("UPDATE quotations SET items_json=? WHERE id=?", (json.dumps(data), qid))
    conn.commit()
    conn.close()
    log_action(user, "refresh_quotation_prices", target=row["ref_no"],
               after={"updated": updated, "skipped": skipped})
    return {"items": items, "updated": updated, "skipped": skipped}


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


class SuggestRequest(BaseModel):
    products: list = []      # product names currently on the quote


@router.post("/api/suggestions")
def get_suggestions(req: SuggestRequest, user: dict = Depends(get_current_user)):
    """Products frequently quoted together with what's on this quote.

    Mined live from quotations already in the repository — no new data is
    collected, the history IS the training set. Computed per request rather
    than precomputed: it runs only when an item lands on a quote, and a live
    scan of the recent 500 quotes is milliseconds at this scale while never
    serving stale counts.
    """
    base = {str(p or "").strip().lower() for p in (req.products or []) if str(p or "").strip()}
    if not base:
        return []
    from collections import Counter
    conn = get_db()
    try:
        cnt = Counter()
        for (ij,) in conn.execute(
                "SELECT items_json FROM quotations ORDER BY id DESC LIMIT 500"):
            try:
                items = json.loads(ij).get("items", [])
            except Exception:
                continue
            # A 700-line bulk BOQ pairs everything with everything — diffuse
            # noise, not preference. Classic market-basket practice: cap the
            # basket size so co-occurrence keeps meaning "chosen together".
            if not items or len(items) > 60:
                continue
            names = {str(i.get("product") or "").strip() for i in items
                     if str(i.get("product") or "").strip()}
            if {n.lower() for n in names} & base:
                for n in names:
                    if n.lower() not in base:
                        cnt[n] += 1

        # Resolve against the live master table — a suggestion for a product
        # no longer in the catalogue would be un-addable.
        out = []
        for name, together in cnt.most_common(24):
            r = conn.execute(
                "SELECT product, original_model, brand, price_3star, image_path "
                "FROM master_products WHERE LOWER(TRIM(product))=LOWER(TRIM(?)) LIMIT 1",
                (name,)).fetchone()
            if not r:
                continue
            out.append({"product": r["product"], "model_no": r["original_model"] or "",
                        "brand": r["brand"] or "", "price_3star": r["price_3star"] or 0,
                        "image_path": r["image_path"] or "", "times_together": together})
            if len(out) >= 6:
                break
        return out
    finally:
        conn.close()


@router.get("/api/dashboard")
def dashboard(user: dict = Depends(get_current_user)):
    """Live numbers for the landing dashboard. Role-aware by construction:
    margin, learning counts, coverage and the activity feed involve cost or
    other people's actions, so they are computed only for admins and simply
    absent from an employee's payload — the UI can't leak what it never gets."""
    is_admin = user.get("role") == "admin"
    conn = get_db()
    try:
        out = {
            "is_admin": is_admin,
            "products": conn.execute("SELECT COUNT(*) FROM master_products").fetchone()[0],
            "images": conn.execute(
                "SELECT COUNT(*) FROM master_products WHERE image_path<>''").fetchone()[0],
            "catalogues": conn.execute(
                "SELECT COUNT(DISTINCT file_name) FROM master_products").fetchone()[0],
        }
        if is_admin:
            out["quotes"] = conn.execute("SELECT COUNT(*) FROM quotations").fetchone()[0]
            recent_rows = conn.execute(
                "SELECT id, ref_no, client_name, status, created_at, items_json "
                "FROM quotations ORDER BY id DESC LIMIT 5").fetchall()
        else:
            out["quotes"] = conn.execute(
                "SELECT COUNT(*) FROM quotations WHERE created_by=?", (user["id"],)).fetchone()[0]
            recent_rows = conn.execute(
                "SELECT id, ref_no, client_name, status, created_at, items_json "
                "FROM quotations WHERE created_by=? ORDER BY id DESC LIMIT 5",
                (user["id"],)).fetchall()

        recent = []
        for r in recent_rows:
            try:
                items = json.loads(r["items_json"]).get("items", [])
            except Exception:
                items = []
            recent.append({
                "id": r["id"], "ref_no": r["ref_no"], "client_name": r["client_name"] or "",
                "status": r["status"], "created_at": r["created_at"], "n": len(items),
                "total": sum((i.get("qty") or 0) * (i.get("price_per_pc") or 0) for i in items),
            })
        out["recent"] = recent

        # Sparkline data: quotations per day (last 14 days with any activity),
        # role-filtered the same way as the count above.
        if is_admin:
            spark_rows = conn.execute(
                "SELECT substr(created_at,1,10) d, COUNT(*) c FROM quotations "
                "GROUP BY d ORDER BY d DESC LIMIT 14").fetchall()
        else:
            spark_rows = conn.execute(
                "SELECT substr(created_at,1,10) d, COUNT(*) c FROM quotations "
                "WHERE created_by=? GROUP BY d ORDER BY d DESC LIMIT 14",
                (user["id"],)).fetchall()
        out["quotes_spark"] = [r["c"] for r in reversed(spark_rows)]
        # Mini bars: products per catalogue, biggest first.
        out["cat_bars"] = [{"name": r["file_name"], "n": r["c"]} for r in conn.execute(
            "SELECT file_name, COUNT(*) c FROM master_products "
            "GROUP BY file_name ORDER BY c DESC LIMIT 6")]

        if is_admin:
            m = conn.execute(
                "SELECT AVG((price_3star-cost)*100.0/price_3star) FROM master_products "
                "WHERE cost>0 AND price_3star>cost").fetchone()[0]
            out["avg_margin"] = round(m, 1) if m else None
            out["learned"] = conn.execute("SELECT COUNT(*) FROM match_corrections").fetchone()[0]
            out["mappings"] = conn.execute("SELECT COUNT(*) FROM column_mappings").fetchone()[0]
            cov = conn.execute(
                "SELECT after_json FROM audit_log WHERE action='check_boq_coverage' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            out["coverage"] = json.loads(cov[0]) if cov and cov[0] else None
            out["activity"] = [dict(r) for r in conn.execute(
                "SELECT a.action, a.target, a.created_at, u.name AS user_name "
                "FROM audit_log a LEFT JOIN users u ON u.id=a.user_id "
                "ORDER BY a.id DESC LIMIT 6")]
        return out
    finally:
        conn.close()


@router.get("/api/quotations/{qid}/margin")
def quotation_margin(qid: int, admin: dict = Depends(require_role("admin"))):
    """What did we actually make on this quotation? Admin-only — it exposes
    purchase cost, which employees never see.

    Each line is resolved against the LIVE master table by text identity
    (product + model), the same identity the learning layer uses, so a
    catalogue re-import doesn't orphan the analysis. Three honest states per
    line rather than fake numbers:
      ok            — cost known, profit computed
      no_cost       — product found but cost is 0/blank (the OPM catalogues)
      not_in_master — product no longer exists in the master table
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        data = json.loads(row["items_json"])

        lines, revenue, known_rev, profit_total = [], 0.0, 0.0, 0.0
        counts = {"ok": 0, "no_cost": 0, "not_in_master": 0}
        for it in data.get("items", []):
            qty = float(it.get("qty") or 0)
            sold = float(it.get("price_per_pc") or 0)
            amount = qty * sold
            revenue += amount
            m = conn.execute(
                "SELECT cost FROM master_products WHERE LOWER(TRIM(product))=LOWER(TRIM(?)) "
                "AND LOWER(TRIM(COALESCE(original_model,'')))=LOWER(TRIM(COALESCE(?,''))) LIMIT 1",
                (it.get("product") or "", it.get("model_no") or "")).fetchone()
            if m is None:
                state, cost, profit, margin = "not_in_master", None, None, None
            elif not m["cost"] or m["cost"] <= 0:
                state, cost, profit, margin = "no_cost", None, None, None
            else:
                cost = float(m["cost"])
                profit = qty * (sold - cost)
                margin = round((sold - cost) * 100 / sold, 1) if sold else None
                known_rev += amount
                profit_total += profit
                state = "ok"
            counts[state] += 1
            lines.append({"product": it.get("product") or "", "model_no": it.get("model_no") or "",
                          "qty": qty, "sold": sold, "amount": amount,
                          "cost": cost, "profit": profit, "margin_pct": margin, "state": state})

        return {
            "id": qid, "ref_no": data.get("ref_no") or row["ref_no"],
            "client_name": data.get("client_name") or row["client_name"],
            "lines": lines, "counts": counts,
            "revenue": revenue,
            "known_cost_revenue": known_rev,
            "profit": profit_total,
            "margin_pct": round(profit_total * 100 / known_rev, 1) if known_rev else None,
        }
    finally:
        conn.close()


@router.get("/api/corrections")
def list_corrections(admin: dict = Depends(require_role("admin"))):
    """Everything matching has learned from human edits — admin-only, the
    review surface for unlearning a wrong lesson."""
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT c.*, u.name AS corrected_by_name FROM match_corrections c "
            "LEFT JOIN users u ON u.id = c.corrected_by "
            "ORDER BY c.created_at DESC LIMIT 500")]
    finally:
        conn.close()
    return rows


@router.delete("/api/corrections/{correction_id}")
def delete_correction(correction_id: int, admin: dict = Depends(require_role("admin"))):
    """Forget a learned correction — the undo for a wrong human edit."""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM match_corrections WHERE id=?", (correction_id,))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(404, "No such correction.")
    finally:
        conn.close()
    log_action(admin, "delete_correction", target=str(correction_id))
    return {"message": "Correction removed — matching returns to its own judgement for that phrase."}


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
