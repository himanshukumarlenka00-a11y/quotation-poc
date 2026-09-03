import os, re, json, base64, tempfile, threading, time
import concurrent.futures
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from groq import Groq
from app.config import (limiter, GROQ_API_KEY_DEFAULT, CEREBRAS_API_KEY, CEREBRAS_MODEL,
                        ANTHROPIC_MODEL, make_llm_client, server_error,
                        ANTHROPIC_CREDIT_USD, ANTHROPIC_PRICE_IN, ANTHROPIC_PRICE_OUT,
                        ANTHROPIC_SPENT_OFFSET_USD, USD_INR)
from app.db import get_db
from app.auth import get_current_user, require_role, _check_quote_access, log_action
from app.matching import get_boq_context, get_feedback_context, generate_ref_no, get_latest_template
from app.export import build_company_quotation, build_final_bill
from app.images import _save_image_to_disk
from app.parser import parse_boq_excel
from app.routers.catalog import _save_upload_validated

router = APIRouter()



class BuildQuotationRequest(BaseModel):
    client_name: str = ""
    items: list = []

class GenerateRequest(BaseModel):
    prompt: str = Field(max_length=8000)
    client_name: str = ""
    catalogs: list = []  # list of file_name strings; empty = search all
    tiers: list = ["3star"]  # subset of ["3star", "4star"] — which master-table price tier(s) to show
    # Set by the From-Excel flow: hash-name of the retained uploaded workbook
    # (DATA_DIR/boq_sources/) — the quotation then exports as a revised copy
    # of that file instead of the company template.
    source_file: str = Field(default="", max_length=64)





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

def _record_ai_usage(model, input_tokens, output_tokens):
    """Append one Claude call's token counts to the ai_usage table — feeds the
    dashboard 'AI usage' tile. Best-effort and self-contained: it creates the
    table if absent and swallows every error, so it can never disturb matching.
    The table is tiny (a few rows per quote) and only weak lines ever call the
    LLM, so the per-call insert is negligible."""
    try:
        c = get_db()
        try:
            c.execute("CREATE TABLE IF NOT EXISTS ai_usage ("
                      "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, model TEXT, "
                      "input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0)")
            c.execute("INSERT INTO ai_usage (ts, model, input_tokens, output_tokens) "
                      "VALUES (?,?,?,?)",
                      (datetime.now().isoformat(), model,
                       int(input_tokens or 0), int(output_tokens or 0)))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


def _llm_chat(client, messages, max_tokens, temperature):
    """One LLM chat call. Claude (Anthropic) when the client is an Anthropic
    client — the primary, best-judgment path. Otherwise Groq, with a Cerebras
    HTTP fallback when Groq answers 429. Returns the content string.

    The OpenAI-style `messages` (with `system` role entries) and `temperature`
    are the Groq/Cerebras shape; the Claude branch adapts them — system becomes
    a separate field, and `temperature` is dropped (the Sonnet/Opus 5 family
    rejects it with a 400). Both providers take the same call sites unchanged."""
    if client is not None and type(client).__module__.split(".")[0] == "anthropic":
        system = "\n\n".join(m["content"] for m in messages
                             if m.get("role") == "system" and m.get("content"))
        conv = [{"role": m["role"], "content": m["content"]}
                for m in messages if m.get("role") in ("user", "assistant")]
        kwargs = dict(model=ANTHROPIC_MODEL, max_tokens=max(max_tokens, 1024),
                      messages=conv,
                      # These are bounded extraction/verify calls. Sonnet's
                      # base judgment already far exceeds the old model here, so
                      # adaptive thinking (on by default) only adds several
                      # seconds PER call — a slow quote. Off = fast, still sharp.
                      thinking={"type": "disabled"})
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)
        try:
            u = getattr(resp, "usage", None)
            _record_ai_usage(ANTHROPIC_MODEL,
                             getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0))
        except Exception:
            pass   # usage tracking is best-effort; never break a real request
        return "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
    try:
        r = client.chat.completions.create(
            model="openai/gpt-oss-120b", messages=messages,
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
    groq_client = make_llm_client()
    if groq_client is None:
        raise HTTPException(400, "No LLM API key configured (set ANTHROPIC_API_KEY or GROQ_API_KEY)")

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
    src = (req.source_file or "").strip()
    data   = {"ref_no": ref_no, "client_name": req.client_name,
              # Which tiers the user actually asked for — [] means none
              # (single plain PRICE column in the quote view).
              "tiers": [t for t in (req.tiers or []) if t in ("3star", "4star")],
              # hash-named retained upload only — never a client-chosen path
              "source_file": src if re.fullmatch(r"[0-9a-f]{40}\.(xlsx|xls)", src) else "",
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


def _record_history(conn, phrase, product, model, client, source, uid):
    """One learning observation: this phrase resolved to this product and a
    human stood by it (approved the quote / switched to it). Evidence only —
    ranking boost, never a pin like match_corrections."""
    pn = _norm_phrase(phrase)
    if not pn or not (product or "").strip():
        return
    try:
        conn.execute(
            "INSERT INTO match_history (phrase_norm, product, original_model, "
            "client_name, source, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (pn, product.strip(), (model or "").strip(), (client or "").strip(),
             source, uid, datetime.now().isoformat()))
    except Exception as e:
        print(f"history record skipped (non-fatal): {e}")


def _phrase_history(conn, phrase, limit=6):
    """Aggregated past choices for a phrase: [(product_l, model_l, count,
    display_product, display_model, last_client)] strongest first."""
    pn = _norm_phrase(phrase)
    if not pn:
        return []
    try:
        return conn.execute(
            "SELECT LOWER(TRIM(product)) pl, "
            "       LOWER(TRIM(COALESCE(original_model,''))) ml, "
            "       COUNT(*) c, MAX(product) p, MAX(original_model) m, "
            "       MAX(client_name) cl "
            "FROM match_history WHERE phrase_norm=? "
            "GROUP BY pl, ml ORDER BY c DESC, MAX(created_at) DESC LIMIT ?",
            (pn, limit)).fetchall()
    except Exception:
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
        if not row:
            # Catalogue re-imports rename product texts and strand the
            # stored identity ("serving tong" -> its correct MELANGE tong
            # went stale and junk answered instead). The model number plus
            # the brand prefix of the stored name still identify the row.
            model = (r["original_model"] or "").strip()
            brand = (r["product"] or "").split("-", 1)[0].strip()
            if model and brand:
                row = conn.execute(
                    "SELECT * FROM master_products "
                    "WHERE LOWER(TRIM(COALESCE(original_model,'')))=LOWER(?) "
                    "AND UPPER(TRIM(COALESCE(brand,'')))=UPPER(?) LIMIT 1",
                    (model.lower(), brand)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None       # a missing table must never break matching


# Words that mean the prompt is prose rather than a list. Their presence sends
# the request to the LLM, because a parser that guesses at sentence structure
# would quietly produce a wrong quotation.
_PROSE_MARKERS = re.compile(
    r"\b(we|i|need|needs|needed|require|requires|required|want|wants|please|"
    r"looking|setup|set up|for the|opening|room|hotel|kitchen|section|"
    r"department|also|plus|including|approx|around|about)\b",
    re.I)
# "with"/"without" are NOT prose markers: "coffee pot WITH lid" and "ice box
# W/O tap" are product phrases in every hotel BOQ, and one such line sent a
# whole clean list to the LLM.


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
    if not p or len(p) > 8000:
        return None
    # Dimensions are atoms. "crate 300 x 200" must never lose its "x 200"
    # to the qty-marker rule (qty=200, then bare "300" accidentally
    # matched model JBC5436300 — a 540x360 crate). Gluing NUMxNUM before
    # any qty parsing makes that split impossible, and the matcher's
    # dimension qualifier already understands the glued form. 2-4 digit
    # numbers only, so "2 x 40" (qty two) keeps meaning quantity.
    p = re.sub(r"(?<=\d\d)\s*[x×*]\s*(?=\d{2,4}\b)", "x", p, flags=re.I)
    # A lone HSN-style line ("69111000 100") is code + qty — the qty-first
    # rule would otherwise mangle the 8-digit code.
    mh = re.match(r"^(\d{6,8})(?:\s+(\d{1,5}))?$", p.strip())
    if mh:
        return [{"product": mh.group(1), "qty": int(mh.group(2) or 1)}]

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
    # A single line that is a comma list or starts with a number belongs to
    # the qty-first segment parser below ("100 soup bowl, 60 ice box").
    _single_listy = (len(lines) == 1 and not re.search(r"\[[^\]\[]+\]", lines[0])
                     and (re.search(r",|;|\band\b", lines[0], re.I)
                          or re.match(r"^\d", lines[0])))
    if lines and not _single_listy:
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
            # Quantity styles the team actually types: "kettle 1000",
            # "kettle 1000 qty", "kettle qty 50", "kettle /50 qty",
            # "kettle x 50". A '/' separator only counts in the explicit
            # qty-word forms, so product names like "set/4" keep their 4.
            m = re.match(
                r"^(.{3,90}?)"
                r"(?:"
                r"[\s\-–/]+(?:qty|x|nos\.?|pcs\.?)[\s.:]*(\d{1,5})"
                r"|[\s\-–/]+(\d{1,5})\s*(?:qty|nos\.?|pcs\.?|pieces?|units?)\.?"
                r"|[\s\-–]+(\d{1,5})"
                r")?$", line, re.I)
            if not m or not (re.search(r"[A-Za-z]", m.group(1))
                             or re.fullmatch(r"\d{6,8}", m.group(1).strip())):
                # a product needs letters — except a bare HSN code line
                items = None
                break
            qty = next((g for g in m.groups()[1:] if g), None)
            items.append({"product": m.group(1).strip(" .-/"),
                          "qty": int(qty) if qty else 1})
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


def _model_exact(model, mtoks, t, t_is_code, numtoks):
    """True when a requested code equals the row's model exactly
    (separator-insensitive) — not merely a substring of a longer code."""
    model_n = re.sub(r"[^a-z0-9]", "", model)
    if not model_n:
        return False
    cand = [re.sub(r"[^a-z0-9]", "", mt) for mt in mtoks]
    if t_is_code:
        cand.append(re.sub(r"[^a-z0-9]", "", t))
    cand += [nt for nt in numtoks if len(nt) >= 5]
    return any(c and c == model_n for c in cand)


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


# Hoisted out of _resolve_master_matches so the suggestion endpoint can use
# the same vocabulary and word test the matcher does — two copies would drift.
_UNITS = {"inch", "inches", "cm", "mm", "mtr", "mtrs", "meter", "metre",
          "meters", "kg", "kgs", "ltr", "litre", "liter", "size", "set",
          "nos", "pcs", "pc"}


from functools import lru_cache

# Vendor-abbreviation equivalents, filled by the resolver's vocab build:
# CAMBRO writes "OVL" where MELANGE writes "Oval" — when BOTH spellings are
# real catalogue vocabulary, each must cover rows using the other.
_WORD_ALTS = {}


@lru_cache(maxsize=16384)
def _covered_pats(word):
    """Compiled patterns for one request word — the matcher calls _covered
    ~570k times on a 120-line BOQ and re-building these regexes per call
    (1.7M re.compile + 1.5M re.escape) was HALF its runtime."""
    forms = {word}
    alts = set(_WORD_ALTS.get(word, ()))
    if word.endswith('s') and len(word) > 3:
        forms.add(word[:-1])
    else:
        forms.add(word + 's')
    exact = [re.compile(r"\b" + re.escape(f) + r"\b") for f in forms | alts]
    # Compound test ("bedsheet" must cover "bed sheet") — WORD-START
    # anchored, chars joined by optional whitespace. A plain substring
    # check on the space-stripped name fabricated words across joints:
    # "crock" was found straddling "classiC ROCK" and a rocks GLASS
    # outranked the real crocks. Alternates join a letter shorter: the
    # catalogue glues its abbreviations ("3COMP", "W/HDL").
    # anchor = "not preceded by a letter": mid-word starts are blocked
    # (the classiC-ROCK straddle) while digit-glued starts still match
    # ("3COMP", "SEALLID12").
    ns = [re.compile(r"(?<![a-z])" + r"\s*".join(re.escape(ch) for ch in f))
          for f in ({f for f in forms if len(f) >= 5}
                    | {a for a in alts if len(a) >= 4})]
    morph = (re.compile(r"\b" + re.escape(word) + r"(?:[a-z]{2,5}|s)\b")
             if len(word) >= 4 else None)
    return exact, ns, morph


def _covered(word, s, s_ns):
    """Is `word` present in a product name as a WORD (not a substring)?

    Plain substring matching silently produced nonsense matches: "pin" is
    inside "chop-pin-g", so "rolling pin" scored against "Wire Stand For
    Chopping Board". Anchoring on word boundaries kills that whole class of
    false positive. The space-stripped form is still consulted, but only for
    words long enough to be a real compound (bedsheet vs bed sheet) rather
    than short fragments that hit by accident.

    Morphology: 'wood' must cover WOODEN, 'rect' RECTANGULAR, 'gold'
    GOLDEN — the request word as a PREFIX of a name word. Only for words
    of 4+ chars, and only 2-5 letters of suffix or a bare plural 's': one
    free letter let "whisk" match WHISKY (wood->WOODEN still covered).
    """
    exact, ns, morph = _covered_pats(word)
    for p in exact:
        if p.search(s):
            return True
    for p in ns:
        if p.search(s):
            return True
    return bool(morph and morph.search(s))


def _glued_rows(conn, words, catalogs=None, limit=200):
    """Rows whose text contains a request word GLUED inside a longer token.

    FTS5 matches token prefixes, so `"dustbin"*` finds DUSTBIN and DUSTBINS
    but never WALTHR-IR-RD001-OVL-ROOMDUSTBIN — that is one token starting
    with "room", and a prefix query cannot see into the middle of it. The
    index is not stale (51,938 rows indexed, zero missing); prefix matching
    simply cannot reach these. Measured against the live master: 19 of 63
    dustbins and 233 of 1,476 trays are invisible to FTS for this reason.

    A bounded substring scan is the cheap fix — under 8ms worst case on
    52k rows, versus a second trigram index to build and keep in step.
    Short and numeric words are skipped: "ss" or "18" would match half the
    catalogue as substrings and drown the pool in noise.

    ponytail: full scan per line, ~8ms at 52k rows. If the master reaches
    a few hundred thousand, add an FTS5 trigram index instead.
    """
    core = [w for w in words if len(w) >= 4 and w.isalpha()]
    if not core:
        return []
    where = " OR ".join(
        "product LIKE ? COLLATE NOCASE OR specification LIKE ? COLLATE NOCASE "
        "OR original_model LIKE ? COLLATE NOCASE" for _ in core)
    params = []
    for w in core:
        params += ["%" + w + "%"] * 3
    sql = f"SELECT * FROM master_products WHERE ({where})"
    if catalogs:
        sql += " AND file_name IN (%s)" % ",".join("?" * len(catalogs))
        params += list(catalogs)
    try:
        return [dict(r) for r in conn.execute(sql + " LIMIT ?", [*params, limit]).fetchall()]
    except Exception:
        return []


def _merge_glued(conn, pool, words, catalogs=None, limit=200):
    """Append substring-only matches to an FTS pool, without reordering it.

    Appended rather than merged by rank: the FTS rows are already sorted by
    bm25 and the scorer downstream re-ranks everything anyway, so putting
    these at the end keeps the pool's existing order meaningful if it is
    ever truncated.
    """
    extra = _glued_rows(conn, words, catalogs, limit)
    if not extra:
        return pool
    have = {r.get("id") for r in pool}
    return pool + [r for r in extra if r.get("id") not in have]


def suggest_products(conn, term, catalogs=None, limit=6):
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
    core = [w for w in toks if len(w) >= 3 and w.isalpha() and w not in _UNITS]
    if not core:
        return []
    # Query for THIS line's words rather than reusing rows_pool. That pool
    # is one FTS match for every requested phrase at once, capped at
    # LIMIT 4000 with no ordering — for a long list the right product is
    # simply not in it. Measured: reusing the pool returned "WALL DRYER"
    # for "Hair Dryer, Color - Black…" because "HAIR DRYER" never made the
    # 4000. This runs only when a line already failed to match, so one
    # narrow query per failure is affordable.
    pool = []
    try:
        match = " OR ".join(f'"{w}"*' for w in core[:12])
        if catalogs:
            ph = ",".join("?" * len(catalogs))
            pool = [dict(r) for r in conn.execute(
                f"SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                f"WHERE master_fts MATCH ? AND m.file_name IN ({ph}) "
                f"ORDER BY f.rank LIMIT 600",
                [match, *catalogs]).fetchall()]
        else:
            pool = [dict(r) for r in conn.execute(
                "SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                "WHERE master_fts MATCH ? ORDER BY f.rank LIMIT 600", (match,)).fetchall()]
    except Exception:
        pass
    # ...plus rows where the word is glued inside a longer token, which a
    # prefix query can never reach. Find is exactly where this matters: the
    # line already failed to match, so a missing candidate is the whole bug.
    pool = _merge_glued(conn, pool, core[:12], catalogs)
    if not pool:
        return []                 # FTS unavailable — no guesses
    out = []
    for r in pool:
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



# Progressive matching: files/quotes above THRESHOLD lines resolve FIRST
# inline (fast screen), the rest stream in CHUNK-sized batches from worker
# processes while the UI polls /live.
PROG_THRESHOLD, PROG_FIRST, PROG_CHUNK = 40, 25, 30


def _pending_stub(it, sl_no, tiers_requested):
    """Placeholder row for a line the background matcher hasn't reached yet."""
    return {
        "sl_no": sl_no,
        "product": (it.get("product") or "").strip(), "qty": int(it.get("qty") or 1),
        "description": "", "model_no": it.get("model_no", ""), "brand": "",
        "specification": "", "hsn_code": "", "price_per_pc": 0,
        "price_currency": "INR", "cost": 0, "gst_pct": 18.0, "image_path": "",
        "tiers": tiers_requested or ["3star"],
        "price_3star": 0, "price_3star_usd": 0, "price_4star": 0, "price_4star_usd": 0,
        "requested": (it.get("product") or "").strip(),
        "matched_by": "pending", "not_in_catalog": True,
        "section": it.get("section", ""),
        "src_key": it.get("src_key", ""),
        "boq_price": float(it.get("boq_price") or 0),
    }


def _llm_apply_variant(it, v):
    """Server-side equivalent of the UI's Switch: point the line at one of
    its own variants."""
    it["product"] = v.get("product", "") or it.get("product", "")
    it["model_no"] = v.get("model_no", "")
    it["brand"] = v.get("brand", "")
    it["specification"] = v.get("specification", "")
    it["description"] = v.get("description", "")
    it["hsn_code"] = v.get("hsn_code", "")
    it["image_path"] = v.get("image_path", "") or ""
    it["price_per_pc"] = v.get("price", 0)
    it["cost"] = v.get("cost", 0)
    it["gst_pct"] = v.get("gst_pct", 18.0)
    for k in ("price_3star", "price_3star_usd", "price_4star",
              "price_4star_usd", "orig_price_3star", "orig_price_4star"):
        it[k] = v.get(k, 0 if not k.startswith("orig") else None)
    it["size_note"] = _size_note(it.get("requested", ""),
                                 f'{v.get("product", "")} '
                                 f'{v.get("specification", "")}')


def _llm_demote_placeholder(it):
    """No candidate is the requested product: honest fill-in row instead of
    a confident wrong pick. Variants stay so Switch still offers them."""
    it["product"] = (it.get("req_raw") or it.get("_req_raw")
                     or it.get("requested") or it.get("product") or "").strip()
    for k in ("description", "model_no", "brand", "specification", "hsn_code",
              "image_path", "size_note"):
        it[k] = ""
    for k in ("price_per_pc", "cost", "price_3star", "price_3star_usd",
              "price_4star", "price_4star_usd"):
        it[k] = 0
    it["gst_pct"] = 18.0
    it["not_in_catalog"] = True


_LLM_WEAK = {"name", "name+spec", "rname", "part", "spec", "sem"}

# The meaning-check batches run CONCURRENTLY. The Anthropic paid tier allows
# ~1000 req/min and 500k input tok/min, so a big BOQ's checks overlap instead
# of queueing one-at-a-time behind a fixed pause (a Groq-free-tier habit).
# Env-overridable; a burst this size stays well under the per-minute limits.
_VERIFY_WORKERS = max(1, int(os.environ.get("LLM_VERIFY_WORKERS", "8")))

# Worker PROCESSES that match the background BOQ lines in parallel (matching is
# CPU-bound Python, so processes, not threads). Was a fixed 4 — fine for small
# BOQs but it left most of a 20-core box idle on a 2,000-line file. Default 8
# (still leaves headroom for the web server on a shared box); env-overridable.
_MATCH_WORKERS = max(1, int(os.environ.get("MATCH_WORKERS", "8")))


def _sig_words(text):
    """Significant words of a request or product name: alphabetic, 3+
    letters, not a bare unit token. Used to gauge how much of a product
    NAME the request actually accounts for."""
    return [w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) >= 3 and w not in _UNITS]


def _needs_verify(it):
    """Should this line get the LLM meaning-check? Yes for a weak-tier AI
    match that is EITHER low-scoring (the original rule) OR a thin
    word-collision: a short request (1-2 significant words) matched to a
    much longer product name it barely accounts for. That second case is
    how "iron" landed on "Cast Iron Round Casserole" — the one request word
    is fully present, so the match scored just over the old 1000 cutoff and
    skipped the check, yet a casserole is not an iron. Detailed multi-word
    lines (the common case) never trip the thin rule, so cost barely moves."""
    vs = it.get("_variants") or []
    if not (vs and vs[0].get("_tier") in _LLM_WEAK):
        return False
    v0 = vs[0]
    if (v0.get("_score") or 0) < 1000:
        return True
    req = (it.get("req_raw") or it.get("_req_raw")
           or it.get("requested") or it.get("product") or "")
    req_w = _sig_words(req)
    name_w = _sig_words(v0.get("product") or "")
    return bool(req_w and len(req_w) <= 2 and len(name_w) - len(req_w) >= 2)


def _llm_cands_of(it):
    return (it.get("llm_cands") or (it.get("_variants") or [])[:5])


def _llm_verify_matches(groq_client, items, batch=20, paced=False):
    """Ask the LLM, for each WEAK-evidence match, which of the line's own
    top candidates really is the requested product — or none. Weak means
    the word tiers below name-certainty, unboosted by brand or history;
    exact/model/HSN/learned lines are never questioned. Verdict A..E
    switches the line to that variant, none demotes it to a fill-in
    placeholder. Every failure path leaves the line untouched.
    Batches run CONCURRENTLY (see _VERIFY_WORKERS) and duplicate lines share
    one verdict. `paced` is kept for call-site compatibility; the fixed
    inter-batch pause it used to add (a Groq-free-tier guard) is gone now the
    primary path is the paid Anthropic tier, whose limits are generous."""
    idxs = []
    for i, it in enumerate(items):
        if it.get("not_in_catalog") or it.get("matched_by") != "ai":
            continue
        if it.get("llm_cands"):
            idxs.append(i)
            continue
        if _needs_verify(it):
            idxs.append(i)
    if not idxs:
        return

    def _key(it):
        return ((it.get("req_raw") or it.get("_req_raw")
                 or it.get("requested") or "").lower(),
                tuple((c.get("product") or "") for c in _llm_cands_of(it)))
    verdict_cache = {}
    uniq = []
    for i in idxs:
        k = _key(items[i])
        if k not in verdict_cache:
            verdict_cache[k] = None
            uniq.append(i)

    chunks = [uniq[start:start + batch] for start in range(0, len(uniq), batch)]

    def _run_chunk(chunk):
        lines = []
        for n, i in enumerate(chunk, 1):
            it = items[i]
            req = (it.get("req_raw") or it.get("_req_raw")
                   or it.get("requested") or it.get("product") or "")[:90]
            cands = [f"{L}) {v.get('brand', '')} {(v.get('product') or '')[:70]}"
                     for L, v in zip("ABCDE", _llm_cands_of(it))]
            lines.append(f"{n}. NEED: {req}\n   " + "\n   ".join(cands))
        prompt = (
            "A hotel-supply client asked for products; our word-matcher "
            "proposed candidates.\nFor each numbered item say which "
            "candidate IS the requested product TYPE (a different size or "
            "series of the right type still counts), or none if no "
            "candidate is that type of product.\n"
            'Answer with ONLY compact JSON, no explanation, like '
            '{"1":"A","2":"none"}.\n\n' + "\n".join(lines))
        # Retry a few times; back off ONLY on failure (e.g. a rare 429). The
        # paid tier's limits are generous, so this is a safety net, not routine.
        for attempt in range(3):
            try:
                txt = _llm_chat(groq_client,
                                [{"role": "user", "content": prompt}],
                                max_tokens=1200, temperature=0)
                m = re.search(r"\{[^{}]*\}", txt or "", re.S)
                if m:
                    return chunk, json.loads(m.group(0))
                print("LLM verify: no JSON in reply")
            except Exception as e:
                print(f"LLM verify attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        return chunk, None

    # Batches run CONCURRENTLY (see _VERIFY_WORKERS): a big BOQ's checks overlap
    # instead of queueing one-by-one behind a fixed pause. One chunk runs inline.
    workers = min(_VERIFY_WORKERS, len(chunks))
    if workers <= 1:
        pairs = [_run_chunk(c) for c in chunks]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            pairs = list(ex.map(_run_chunk, chunks))

    for chunk, verdicts in pairs:
        if not verdicts:
            continue
        for n, i in enumerate(chunk, 1):
            verdict_cache[_key(items[i])] = \
                str(verdicts.get(str(n), "") or "").strip().upper()

    for i in idxs:
        it = items[i]
        v = verdict_cache.get(_key(it))
        cands = _llm_cands_of(it)
        if not v or v == "A":
            continue
        if v in ("B", "C", "D", "E") and "ABCDE".index(v) < len(cands):
            _llm_apply_variant(it, cands["ABCDE".index(v)])
            it["llm_check"] = "switched"
        elif v == "NONE":
            _llm_demote_placeholder(it)
            it["llm_check"] = "rejected"


_CAP_ML = {"l": 1000, "ltr": 1000, "ltrs": 1000, "litre": 1000, "litres": 1000,
           "liter": 1000, "liters": 1000, "lt": 1000, "ml": 1, "cl": 10,
           "oz": 29.57, "qt": 946}
_SIZE_RX = (r"\d+(?:\.\d+)?\s*(?:litres?|liters?|ltrs?|lt|ml|cl|oz|qt|l|cm|mm)\b"
            r"|\d{2,4}\s*[x*×]\s*\d{2,4}")


def _size_facts(text):
    """Capacities (as ml), lengths (as mm, inches converted) and NxN dims
    stated in a text."""
    s = (text or "").lower()
    caps = {round(float(v) * _CAP_ML[u]) for v, u in re.findall(
        r"(\d+(?:\.\d+)?)\s*(litres?|liters?|ltrs?|lt|ml|cl|oz|qt|l)\b", s)}
    lens = {round(float(v) * (10 if u == "cm" else 1)) for v, u in
            re.findall(r"(\d+(?:\.\d+)?)\s*(cm|mm)\b", s)}
    lens |= {round(float(v) * 25.4) for v in re.findall(
        r"(\d+(?:\.\d+)?)\s*(?:\"|''|inch(?:es)?\b)", s)}
    dims = set(re.findall(r"\d{2,4}\s*[x*×]\s*\d{2,4}", s.replace(" ", "")))
    return caps, lens, dims


def _size_note(req_text, got_text):
    """'client asked 9 L — this is 7 L' when the matched product's stated
    size conflicts with the client's. Empty when compatible or when either
    side states no size (offering the nearest stocked size is normal — the
    note just says so instead of quoting silently)."""
    qc, ql, qd = _size_facts(req_text)
    gc, gl, gd = _size_facts(got_text)

    def fml(ml):
        return f"{ml/1000:g} L" if ml >= 1000 else f"{ml:g} ml"

    def fmm(mm):
        return f"{mm/10:g} cm" if mm >= 10 else f"{mm:g} mm"

    if qc and gc and not any(abs(a - b) <= 0.12 * max(a, b)
                             for a in qc for b in gc):
        return (f"client asked {' / '.join(fml(m) for m in sorted(qc))}"
                f" — this is {' / '.join(fml(m) for m in sorted(gc))}")
    # Lengths compare only within the same magnitude class — a client
    # spec's 19MM board THICKNESS must not be held against a plate's
    # 21 cm DIAMETER (<40mm ≈ thickness/edge, 40-400mm ≈ dish sizes,
    # >400mm ≈ furniture).
    def _buckets(vals):
        b = {}
        for v in vals:
            b.setdefault(0 if v < 40 else (1 if v <= 400 else 2),
                         set()).add(v)
        return b
    if ql and gl:
        qb, gb = _buckets(ql), _buckets(gl)
        for k in sorted(qb.keys() & gb.keys(), reverse=True):
            if not any(abs(a - b) <= 0.08 * max(a, b)
                       for a in qb[k] for b in gb[k]):
                return (f"client asked "
                        f"{' / '.join(fmm(m) for m in sorted(qb[k]))}"
                        f" — this is "
                        f"{' / '.join(fmm(m) for m in sorted(gb[k]))}")
    if qd and gd and not (qd & gd):
        return f"client asked {sorted(qd)[0]} — this is {sorted(gd)[0]}"
    return ""


def _term_with_sizes(term, full):
    """The 20-word search-term cap can drop a size buried deep in a client
    spec (then a 1 Qt bucket answers a 5 Ltr line) — re-append any size
    tokens the cap cut off so the qualifier machinery sees them."""
    tl = (term or "").lower()
    extra = []
    for m in re.finditer(_SIZE_RX, (full or "").lower()):
        tok = re.sub(r"\s+", " ", m.group(0).strip())
        if tok not in tl and tok not in extra:
            extra.append(tok)
        if len(extra) >= 4:
            break
    return term + (" " + " ".join(extra) if extra else "")


def _resolve_master_matches(conn, extracted, catalogs, tiers_req, groq_client, prompt="",
                            variant_cap=15, llm_verify=True):
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

    # ── Typo correction against the catalogue's own vocabulary ──────────────
    # "coktail table" found nothing, ever: FTS is exact-prefix and the name
    # matcher needs real words. Deterministic fix: any query word UNKNOWN to
    # the catalogue is corrected to the closest catalogue word at edit
    # distance 1 (most frequent wins). Known words are never touched, so
    # this cannot "correct" a legitimate term into something else.
    global _VOCAB_CACHE
    try:
        _VOCAB_CACHE
    except NameError:
        _VOCAB_CACHE = (None, None)
    if _VOCAB_CACHE[0] != len(all_products):
        from collections import Counter
        freq = Counter(w for p in all_products
                       for w in re.findall(r"[a-z]{3,}", (p or "").lower()))
        # One lowercase blob of every name — cheap "does this exact phrase
        # appear anywhere in the catalogue" checks for the compound merger.
        blob = "\n".join((p or "").lower() for p in all_products)
        # ── Abbreviation map: people shorten words by dropping vowels
        # (lrg→large, rnd→round, ovl→oval, brd→board) or truncating
        # (med→medium). Precomputed over NAME + SPEC words (identity often
        # lives in the spec — CAMBRO's "SQUARE 12QT"), most frequent word
        # wins each skeleton. Only OOV tokens ever consult this, so real
        # words and model codes are never rewritten.
        wfreq = Counter()
        try:
            for (pn, sp) in conn.execute(
                    "SELECT product, COALESCE(specification,'') FROM master_products"):
                for w in re.findall(r"[a-z]{3,}", ((pn or "") + " " + sp).lower()):
                    wfreq[w] += 1
        except Exception:
            wfreq = Counter(freq)
        skmap = {}
        for w, n in wfreq.items():
            if n < 2 or len(w) < 4:
                continue
            sk = w[0] + re.sub(r"[aeiou]", "", w[1:])
            if sk != w:
                cur = skmap.get(sk)
                if not cur or n > cur[1]:
                    skmap[sk] = (w, n)
        # Vendor-abbreviation equivalents: a vocab token that IS its own
        # skeleton ("ovl" — vowelless) paired with the full word sharing
        # that skeleton ("oval"). Both directions become coverage
        # alternates, so "ovl tong" finds MELANGE's Oval Tong and "oval
        # tray" finds CAMBRO's OVL Camtreads.
        _WORD_ALTS.clear()
        for a, na in wfreq.items():
            if len(a) < 3 or a != a[0] + re.sub(r"[aeiou]", "", a[1:]):
                continue
            hit = skmap.get(a)
            if hit and hit[0] != a:
                _WORD_ALTS[a] = (hit[0],)
                _WORD_ALTS[hit[0]] = _WORD_ALTS.get(hit[0], ()) + (a,)
        # Pairs the skeleton rule can't derive but the data uses on both
        # sides: "6-Compartment Tray" vs "3COMP", "W/HDL" vs handled rows.
        for a, b in (("comp", "compartment"), ("hdl", "handle"),
                     ("glass", "barware")):
            if wfreq.get(a) and wfreq.get(b):
                _WORD_ALTS[a] = _WORD_ALTS.get(a, ()) + (b,)
                _WORD_ALTS[b] = _WORD_ALTS.get(b, ()) + (a,)
        _covered_pats.cache_clear()
        _VOCAB_CACHE = (len(all_products), freq, blob, skmap, wfreq)
    _vocab = _VOCAB_CACHE[1]
    _name_blob = _VOCAB_CACHE[2]
    _skmap, _wfreq = _VOCAB_CACHE[3], _VOCAB_CACHE[4]

    def _ed1(a, b):
        """Edit distance <= 1 (substitute / insert / delete one char),
        plus one ADJACENT transposition — "tabel"/"frok" are the most
        common real typing errors and are 2 substitutions otherwise."""
        la, lb = len(a), len(b)
        if abs(la - lb) > 1:
            return False
        if la == lb:
            d = [i for i in range(la) if a[i] != b[i]]
            return (len(d) <= 1
                    or (len(d) == 2 and d[1] == d[0] + 1
                        and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]]))
        if la > lb:
            a, b, la, lb = b, a, lb, la
        i = j = diff = 0
        while i < la and j < lb:
            if a[i] == b[j]:
                i += 1; j += 1
            else:
                diff += 1
                if diff > 1:
                    return False
                j += 1
        return True

    _ABBR_2L = {"sq": "square", "sm": "small", "lg": "large"}

    # Measurement words must NEVER be "typo-corrected" into products —
    # ed1 once turned "liter jar" into "LIFTER jar".
    _UNIT_SAFE = {"liter", "liters", "litre", "litres", "ltr", "ltrs", "lt",
                  "quart", "quarts", "qt", "qts", "gram", "grams", "gm", "gms",
                  "oz", "comp", "inch", "inches", "dia", "diameter", "swg",
                  "gauge"}

    def _expand_abbrev(lw):
        """Expand a shorthand token to the catalogue word it abbreviates —
        vowel-dropped (lrg→large), skeleton/word prefix (sml→small,
        med→medium), or a tiny curated 2-letter list (sq→square). Fires
        ONLY on tokens the catalogue has never seen, and only when one
        expansion clearly dominates."""
        if not lw.isalpha() or not (2 <= len(lw) <= 6) or _wfreq.get(lw, 0):
            return None
        # 3+ letters only for skeleton lookups: "cm" is the skeleton of
        # "Como" and every 2-letter unit would drift into some collection
        # name. Two-letter shorthands live in the curated list alone.
        hit = _skmap.get(lw) if len(lw) >= 3 else None
        if not hit and len(lw) >= 3:
            cands = [v for sk, v in _skmap.items() if sk.startswith(lw)]
            cands += [(v, n) for v, n in _wfreq.items()
                      if n >= 2 and len(v) >= len(lw) + 2 and v.startswith(lw)]
            if cands:
                cands.sort(key=lambda x: -x[1])
                if len(cands) == 1 or cands[0][1] >= 3 * cands[1][1]:
                    hit = cands[0]
        if not hit and lw in _ABBR_2L and _wfreq.get(_ABBR_2L[lw], 0) >= 2:
            hit = (_ABBR_2L[lw], 1)
        if not hit and len(lw) >= 6:
            # CONTRACTION: the user types the full word, the catalogue only
            # knows the short one — "compartment"→COMP, "transparent"→TRANS,
            # "rectangular"→RECT. Same dominance rule as expansion.
            cands = [(v, n) for v, n in _wfreq.items()
                     if n >= 3 and len(v) >= 3 and len(lw) - len(v) >= 3
                     and lw.startswith(v)]
            if cands:
                cands.sort(key=lambda x: -x[1])
                if len(cands) == 1 or cands[0][1] >= 3 * cands[1][1]:
                    hit = cands[0]
        return hit[0] if hit else None

    def _fix_typos(term):
        out = []
        for w in re.findall(r"\S+", term or ""):
            lw = w.lower()
            if lw in _UNIT_SAFE:
                out.append(w)
                continue
            ex = _expand_abbrev(lw)
            if ex:
                out.append(ex)
                continue
            # Unknown words get corrected; so do words the catalogue knows
            # only from 1-2 (likely misspelt) rows when a hugely more common
            # neighbour exists — two products named "Coktail ..." once made
            # 'coktail' a "legitimate" word and blocked its own correction.
            # KNOWN and the candidate pool both consult names+specs (_wfreq):
            # this catalogue keeps identity in specs — "seal" (13 spec rows,
            # 0 name rows) was being "corrected" to SEAT.
            known = (_vocab.get(lw, 0) or _vocab.get(lw.rstrip("s"), 0)
                     or _wfreq.get(lw, 0) or _wfreq.get(lw.rstrip("s"), 0))
            if len(lw) >= 4 and lw.isalpha() and known <= 2:
                cands = [(v, n) for v, n in _wfreq.items()
                         if v[0] == lw[0] and _ed1(lw, v)
                         and (known == 0 or n >= 25 * known)]
                if cands:
                    best = max(cands, key=lambda x: x[1])
                    if known == 0 or best[1] > known:
                        w = best[0]
            out.append(w)
        return " ".join(out)

    def _merge_compounds(term):
        """'pop corn' finds nothing while the catalogue says POPCORN — join
        adjacent words when the joined form is a catalogue word at least as
        common as both halves (so 'bar spoon' is never mangled)."""
        words = re.findall(r"\S+", term or "")
        out, i = [], 0
        while i < len(words):
            if i + 1 < len(words):
                a, b = words[i].lower(), words[i + 1].lower()
                j = a + b
                # Merge only when the joined word is real catalogue vocabulary
                # AND the two-word phrase never occurs in any product name —
                # 'tea pot' (155 phrase rows) stays split, 'pop corn' (0
                # phrase rows, POPCORN exists) joins.
                if (a.isalpha() and b.isalpha() and _vocab.get(j, 0) >= 2
                        and f"{a} {b}" not in _name_blob):
                    out.append(words[i] + words[i + 1])
                    i += 2
                    continue
            out.append(words[i])
            i += 1
        return " ".join(out)

    _GN_FRACS = {"full": "1/1", "half": "1/2", "third": "1/3",
                 "quarter": "1/4", "sixth": "1/6", "ninth": "1/9"}

    def _norm_units(term):
        # "1 litre" / "1 Ltr" / "1liter" all become "1l" so every capacity
        # spelling takes the identical scoring path as "1L"; "1.0L" collapses
        # to "1l" the same way.
        s = re.sub(r"(\d+(?:\.\d+)?)\s*(litres?|liters?|ltrs?|lt)\b", r"\1l",
                   term or "", flags=re.I)
        s = re.sub(r"(\d+)\.0\s*(l|ml|kw|w|v|hz|mm|cm|kg)\b", r"\1\2",
                   s, flags=re.I)
        # Hotel-speak → catalogue notation. GN fraction words: "half size
        # food pan" means the 1/2 pan, not whichever pan ranks first (it
        # once returned the FULL-size). BOTH forms convert only in GN
        # context (pan/lid/colander/carrier): chafing dishes spell the
        # words out ("FULL SIZE INDUCTION CHAFING DISH"), and rewriting
        # "Full Size Chafing Dish 9 Ltr" to "1/1 …" unanchored the right
        # row so a 7L dish beat the 9L one asked for.
        if (re.search(r"\b(pan|pans|lid|lids|colander|carrier)\b", s, re.I)
                and not re.search(r"\bchaff?ing\b", s, re.I)):
            s = re.sub(r"\b(full|half|third|quarter|sixth|ninth)[\s-]+size\b",
                       lambda m: _GN_FRACS[m.group(1).lower()], s, flags=re.I)
            s = re.sub(r"\b(half|third|quarter|sixth|ninth)\b",
                       lambda m: _GN_FRACS[m.group(1).lower()], s, flags=re.I)
        s = re.sub(r"\bdouble[\s-]*wall(?:ed)?\b", "dw", s, flags=re.I)
        s = re.sub(r"\bdozen\b", "dz", s, flags=re.I)
        return re.sub(r"\bounces?\b", "oz", s, flags=re.I)

    for it in extracted:
        # The client's untouched wording, kept for the LLM verify pass —
        # the typo-fixer can corrupt real labels ("CART" -> "card") and an
        # LLM judging the corrupted text would bless the wrong verdict.
        it.setdefault("_req_raw", (it.get("product") or "").strip())
        for key in ("product", "search_term"):
            if it.get(key):
                # Merge BEFORE typo-fixing: 'corn' alone is unknown to the
                # catalogue and would get "corrected" to 'cork' first.
                fixed = _fix_typos(_merge_compounds(_norm_units(it[key])))
                if fixed != it[key]:
                    it[key] = fixed

    # One BATCH embedding for every line's semantic lookup — terms are final
    # right here, so the keys match what _line_pool will ask for. On a
    # 1,900-line BOQ this replaces ~1,900 model calls with one.
    try:
        from app.semantic import prime_queries
        prime_queries([(it.get("search_term") or it.get("product") or "")
                       .lower().strip()
                       for it in extracted
                       if not re.search(r"\d{3}", it.get("search_term")
                                        or it.get("product") or "")])
    except Exception:
        pass

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
        # Plurals cut one way only under prefix search: "cup"* reaches
        # "cups" but "cups"* can NEVER reach "cup" — a request for "cups"
        # once matched a kettle tray whose SPEC mentioned cups while every
        # row actually NAMED "cup" was absent from the pool. Add the
        # stripped-s form of each word so both directions work.
        for w in list(words):
            if w.endswith("s") and len(w) > 3:
                words.add(w[:-1])
        return " OR ".join(f'"{w}"*' for w in sorted(words)[:40])

    search_terms = [it.get("search_term") or it.get("product") or "" for it in extracted]

    # Brand preference: naming ANY catalogue brand in the request ("i need
    # walther items / dustbin 10") biases every line toward that brand's
    # rows — other brands stay right behind as switch options, nothing is
    # filtered out. The typo layer runs on the haystack first so "walther"
    # finds brand WALTHR. Boost is name-tier only, so typed model codes and
    # human corrections still outrank it.
    global _BRANDS_CACHE
    try:
        _BRANDS_CACHE
    except NameError:
        _BRANDS_CACHE = (None, None)
    if _BRANDS_CACHE[0] != len(all_products):
        bs = {str(b or "").strip().upper()
              for (b,) in conn.execute(
                  "SELECT DISTINCT brand FROM master_products "
                  "WHERE LENGTH(TRIM(COALESCE(brand,''))) >= 3")}
        bs = {b for b in bs if re.match(r"^[A-Z][A-Z &.'-]+$", b)}
        # A brand whose name is an everyday product word ("BAR") must not
        # hijack ordinary requests ("bar spoon"). Trigger only brands that
        # rarely appear in OTHER brands' product names.
        keep = set()
        rows_bn = conn.execute(
            "SELECT UPPER(COALESCE(brand,'')), UPPER(product) FROM master_products").fetchall()
        for b in bs:
            pat = re.compile(r"\b" + re.escape(b) + r"\b")
            # Only FOREIGN-branded rows count as cross-use: unbranded rows
            # named "WALTHR-…" are that brand's own products with a blank
            # brand cell, not evidence the word is generic.
            cross = sum(1 for rb, name in rows_bn
                        if rb and b not in rb and name and pat.search(name))
            if cross < 25:
                keep.add(b)
        _BRANDS_CACHE = (len(all_products), keep)
    # PROMPT-only: deriving preferences from every line's text at once let
    # one line's brand word boost that brand for the whole call — in a
    # 40-line BOQ chunk a single AMAYDA mention pushed AMAYDA junk onto
    # every Flatware line. Per-line text is handled by _line_prefs below.
    _pref_hay = _fix_typos(prompt or "").upper()
    brand_pref = {b for b in _BRANDS_CACHE[1]
                  if re.search(r"\b" + re.escape(b) + r"\b", _pref_hay)}

    # Catalogue-name preference: "only indigo products" names a FILE, not a
    # brand (all INDIGO rows are branded MELANGE) — distinctive words from
    # file names count as preference too, biasing rows from those files.
    global _FILETOK_CACHE
    try:
        _FILETOK_CACHE
    except NameError:
        _FILETOK_CACHE = (None, None)
    if _FILETOK_CACHE[0] != len(all_products):
        _GENERIC_F = {"price", "list", "lists", "pricelist", "master", "final",
                      "done", "copy", "part", "items", "item", "table", "preview",
                      "quotation", "compressed", "digital", "added", "from",
                      "boq", "products", "professional", "collection", "series"}
        ftoks = {}
        for (fn,) in conn.execute("SELECT DISTINCT file_name FROM master_products"):
            for w in re.findall(r"[A-Za-z]{4,}", fn or ""):
                lw = w.lower()
                # A file token that is also a common PRODUCT word (induction,
                # chafing, kitchen...) must not become a preference trigger —
                # only distinctive catalogue identifiers (indigo, kutahya).
                if lw not in _GENERIC_F and _vocab.get(lw, 0) < 25:
                    ftoks.setdefault(lw.upper(), set()).add(fn)
        _FILETOK_CACHE = (len(all_products), ftoks)
    file_pref = set()
    _pref_words = set()
    for tok, fns in _FILETOK_CACHE[1].items():
        if re.search(r"\b" + re.escape(tok) + r"\b", _pref_hay):
            file_pref |= fns
            _pref_words.add(tok.lower())

    def _line_prefs(t):
        """Preferences named in THIS line's own text, unioned with the
        prompt-wide ones — so 'fns finesse fork' still prefers FNS, but
        one line's brand word can't contaminate its chunk-mates."""
        tU = (t or "").upper()
        ttoks = set(re.findall(r"[A-Z0-9]+", tU))
        bl = brand_pref | {b for b in _BRANDS_CACHE[1]
                           if (b in ttoks if " " not in b else
                               re.search(r"\b" + re.escape(b) + r"\b", tU))}
        fl = set(file_pref)
        pw = set(_pref_words)
        for tok, fns in _FILETOK_CACHE[1].items():
            if tok in ttoks:
                fl |= fns
                pw.add(tok.lower())
        return bl, fl, pw

    # Collection prefixes ("FNS-Casper-Table Fork") are decoration, not a
    # different product — a Casper table fork IS a table fork, so it must
    # compete on price. Learned from the data, not a hardcoded list: a
    # digit-free MIDDLE segment word repeating across 5+ rows (casper,
    # harmony, slimline) is a line name; one-off middle identities
    # ("Labelholdersmall", "Vending Lid") stay type-changing extras.
    global _COLL_CACHE
    try:
        _COLL_CACHE
    except NameError:
        _COLL_CACHE = (None, set())
    if _COLL_CACHE[0] != len(all_products):
        _midf, _lastf = {}, {}
        for (pn, br) in conn.execute(
                "SELECT product, COALESCE(brand,'') FROM master_products"):
            segs = [s for s in re.split(r"\s*-\s*", (pn or "").lower())
                    if s.strip()]
            if segs and segs[0].strip() == br.strip().lower():
                segs = segs[1:]
            for w in re.findall(r"[a-z]+", segs[-1] if segs else ""):
                if len(w) >= 3:
                    _lastf[w] = _lastf.get(w, 0) + 1
            for s in segs[:-1]:
                if any(c.isdigit() for c in s):
                    continue
                for w in re.findall(r"[a-z]+", s):
                    if len(w) >= 3:
                        _midf[w] = _midf.get(w, 0) + 1
        # A real collection name (Casper, Slimline) lives ONLY in the
        # middle; a word that also names products in final segments
        # (spoon, dessert, pizza) is a type-word and must keep counting.
        _COLL_CACHE = (len(all_products),
                       {w for w, c in _midf.items()
                        if c >= 5 and _lastf.get(w, 0) < 5})
    _collections = _COLL_CACHE[1]

    # A line that is ONLY brand names + filler ("only nilkamal products") is
    # context, not an order line — it has done its job setting brand_pref
    # above; quoting it would just produce a junk placeholder row.
    if brand_pref or file_pref:
        _FILLER = {"only", "items", "item", "products", "product", "all",
                   "from", "brand", "brands", "everything", "things", "stuff",
                   "pls", "plz", "please", "give", "want", "need", "needed"}
        _brand_words = ({w.lower() for b in brand_pref for w in b.split()}
                        | _pref_words)
        def _is_context_line(it):
            toks = re.findall(r"[a-z]+", _fix_typos(it.get("product") or "").lower())
            # 1-2 letter scraps ("me", "of") are filler by definition.
            sig = [t for t in toks if len(t) > 2
                   and t not in _FILLER and t not in _brand_words]
            return bool(toks) and not sig
        kept = [it for it in extracted if not _is_context_line(it)]
        if kept:                      # never drop everything
            extracted = kept
            search_terms = [it.get("search_term") or it.get("product") or "" for it in extracted]
    rows_pool, used_fts = [], False
    try:
        match = _fts_query(search_terms)
        if match:
            if catalogs:
                ph = ",".join("?" * len(catalogs))
                sql = (f"SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                       f"WHERE master_fts MATCH ? AND m.file_name IN ({ph}) "
                       f"ORDER BY f.rank LIMIT 4000")
                rows_pool = [dict(r) for r in conn.execute(sql, [match, *catalogs]).fetchall()]
            else:
                rows_pool = [dict(r) for r in conn.execute(
                    "SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                    "WHERE master_fts MATCH ? ORDER BY f.rank LIMIT 4000", (match,)).fetchall()]
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
    _pool_cache = {}
    # Per-line history context: set before each search_catalog call so the
    # scoring loop can boost previously-chosen rows for THIS phrase.
    _hist_ctx = {}

    def _line_pool(term):
        """Candidate rows for ONE line, ranked by relevance.

        The shared rows_pool is a single FTS query for the WHOLE request:
        sorted(words)[:40] then LIMIT 4000. Measured on a 40-line batch it
        saw 147 distinct words, kept 40 of them alphabetically, matched
        30,199 rows and kept 4,000 — and all 17 lines that failed were simply
        absent from it, every one of which matched when sent alone. Both caps
        get worse as the request grows, so a big BOQ was mostly guesswork.

        One query per line removes both caps, and is cheaper: scoring 40
        lines against 4,000 shared rows is 160k comparisons, against ~400
        of their own is 16k. Cached per term so repeats cost nothing.
        """
        key = (term or "").lower().strip()
        if key in _pool_cache:
            return _pool_cache[key]
        _bl, _fl, _ = _line_prefs(key)
        words = {w.lower() for w in re.findall(r"[A-Za-z0-9]{2,}", key)}
        pool = []
        if words:
            match = " OR ".join(f'"{w}"*' for w in sorted(words)[:24])
            try:
                if catalogs:
                    ph = ",".join("?" * len(catalogs))
                    pool = [dict(r) for r in conn.execute(
                        f"SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                        f"WHERE master_fts MATCH ? AND m.file_name IN ({ph}) "
                        f"ORDER BY f.rank LIMIT 220", [match, *catalogs]).fetchall()]
                else:
                    pool = [dict(r) for r in conn.execute(
                        "SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                        "WHERE master_fts MATCH ? ORDER BY f.rank LIMIT 220", (match,)).fetchall()]
            except Exception:
                pool = []
            # A named brand/catalogue preference is useless if the preferred
            # rows never enter the pool: "plate" matches 7,900 rows and the
            # 400-cap keeps none of INDIGO's 14. Pull the preferred sources'
            # own matches in explicitly so the scoring boost has candidates.
            if (_fl or _bl) and not catalogs:
                try:
                    extra = []
                    if _fl:
                        phf = ",".join("?" * len(_fl))
                        extra += conn.execute(
                            f"SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                            f"WHERE master_fts MATCH ? AND m.file_name IN ({phf}) "
                            f"ORDER BY f.rank LIMIT 200", [match, *_fl]).fetchall()
                    for b in _bl:
                        extra += conn.execute(
                            "SELECT m.* FROM master_fts f JOIN master_products m ON m.id = f.rowid "
                            "WHERE master_fts MATCH ? AND UPPER(m.brand) LIKE ? "
                            "ORDER BY f.rank LIMIT 200", (match, f"%{b}%")).fetchall()
                    seen_ids = {r["id"] for r in pool}
                    pool += [dict(r) for r in extra if r["id"] not in seen_ids]
                except Exception:
                    pass
            # Meaning-similar rows join the pool carrying their similarity —
            # the scoring loop fuses it as a bonus/floor. Code-like terms
            # skip this (a typed model number has no useful "meaning").
            if not re.search(r"\d{3}", key):
                try:
                    from app.semantic import semantic_topk
                    sem = semantic_topk(key, 40)
                except Exception:
                    sem = None
                if sem:
                    simmap = dict(sem)
                    have = {r["id"] for r in pool}
                    missing = [i for i in simmap if i not in have]
                    if missing:
                        ph = ",".join("?" * len(missing))
                        sql = f"SELECT * FROM master_products WHERE id IN ({ph})"
                        args = list(missing)
                        if catalogs:
                            phc = ",".join("?" * len(catalogs))
                            sql += f" AND file_name IN ({phc})"
                            args += list(catalogs)
                        pool += [dict(r) for r in conn.execute(sql, args)]
                    for r in pool:
                        s = simmap.get(r["id"])
                        if s:
                            r["_semsim"] = s
            pool = _merge_glued(conn, pool, words, catalogs)
        _pool_cache[key] = pool
        return pool

    def search_catalog(term):
        """Find catalog rows matching a request by product NAME, MODEL NO, or
        SPECIFICATION. Returns all candidates ranked best-first — used both to
        pick the line item and to populate the variant switcher.
        Ranking: model-number hit > full name hit > partial name hit > spec hit."""
        t = (term or "").lower().strip()
        if not t:
            return []
        brand_pref_l, file_pref_l, pref_words_l = _line_prefs(t)
        toks = re.findall(r"[a-z0-9]+", t)
        core = [w for w in toks if len(w) >= 3 and w.isalpha() and w not in _UNITS]
        # A catalogue-file word used inline ("montavo dessert knife") has
        # done its job selecting the file — product names never contain it
        # (the vocab filter guarantees that), so requiring coverage of it
        # would fail every row it just boosted.
        if file_pref_l and pref_words_l:
            core = [w for w in core if w not in pref_words_l]
        # Two-letter designators like the "GN" in "GN pan" are real product
        # qualifiers, but too short for `core` (which needs 3+ chars to avoid
        # noise). Dropping them left "GN pan" as bare "pan", which matched
        # "Pan, Roasting Large" (Rs 19,380) over the actual GN PAN (Rs 845).
        # Scored as a bonus rather than a requirement, so they can only
        # promote the right row, never exclude a legitimate one.
        short = [w for w in toks if len(w) == 2 and w.isalpha() and w not in _UNITS]
        # model-number-like tokens in the request (mix of letters+digits,
        # codes). Trailing punctuation is stripped BEFORE classifying:
        # a client's "8 Ltr." normalized to "8l." and the dot made it fail
        # the size test below, minting an unsatisfiable model-token
        # qualifier that wiped every correct candidate.
        mtoks = [w for w in (m.rstrip("./-")
                             for m in re.findall(r"[a-z0-9][a-z0-9\-/\.]+", t))
                 if any(c.isdigit() for c in w) and any(c.isalpha() for c in w)
                 # "1l" / "450ml" / "28cm" / "300x200" are SIZES, not model
                 # codes — they must not trip the code gates (brand boost,
                 # semantic skip).
                 and not re.fullmatch(
                     r"\d+(?:\.\d+)?(?:l|ml|kw|w|v|hz|mm|cm|kg|qt|ltrs?|lt|in|inch)"
                     r"|\d{2,4}(?:x\d{2,4}){1,2}(?:mm|cm)?",
                     w)]
        # Bare numbers are model codes too. mtoks needs letters AND digits, so
        # "WCCE001-SS" counted but "2688" did not — and "ARDACAM 2688 Plate"
        # then tied with ARDACAM-2447-Plate on name alone and picked whichever
        # came first. Used only as a tie-breaker below: it promotes a row that
        # already matched, never conjures one, so a size like "500" cannot
        # drag in an unrelated model 500.
        # HSN-looking tokens: 6-8 digit pure numbers (4-digit chapters would
        # collide with sizes and model fragments). An exact or prefix hit on
        # a row's HSN code lists that whole GST family.
        hsn_toks = re.findall(r"\b\d{6,8}\b", t)
        numtoks = [w for w in re.findall(r"\d{3,}", t)]
        # Numbers inside a dimension are sizes, not model references —
        # "crate 540 x 360" once gave its +300 model bonus to LID5436000
        # because that code happens to contain "360".
        _dims = re.findall(r"\d{2,4}\s*[x*×]\s*\d{2,4}(?:\s*[x*×]\s*\d{2,4})?", t)
        if _dims:
            numtoks = [n for n in numtoks if not any(n in d for d in _dims)]
        t_ns = t.replace(" ", "")
        # BOQ lines carry the client's own PRODUCT label separate from the
        # spec blob appended into the term. Weak-tier candidates must anchor
        # in those label words — "Mobile Bar" once matched an Oil Can purely
        # because its spec said "prevent OIL immersion … bearing CAN
        # effectively prevent". Only active for the line currently being
        # resolved (label != term means spec/model text was appended);
        # typed prompts and side-searches are exempt.
        _pl = (_cur_line.get("product") or "").lower().strip()
        prod_core = ([w for w in re.findall(r"[a-z]+", _pl)
                      if len(w) >= 3 and w not in _UNITS]
                     if _pl and _pl != t
                     and (_cur_line.get("search_term") or "").lower().strip() == t
                     else [])
        # this line's own candidates; rows_pool only as a fallback when FTS
        # is unavailable, so a missing index still cannot fail a quotation
        pool = _line_pool(t) or rows_pool
        scored = []
        for r in pool:
            name = (r.get('product') or '').lower(); name_ns = name.replace(' ', '')
            # Significant words of the PRODUCT name, for the reverse test
            # below — but only of its HUMAN part. Catalogue names are written
            # "BRAND-CODE-Real Name" ("MELANGE-SMLE0054-Pillow Twin Feather"),
            # and demanding "melange"/"smle" appear in the request made the
            # reverse test fail for almost every branded row: it only ever
            # worked for bare names like "HAIR DRYER". Leading segments that
            # carry a digit, or repeat the brand, are packaging — drop them.
            segs = [s for s in re.split(r"\s*-\s*", name) if s.strip()]
            brand_l = (r.get('brand') or '').strip().lower()
            # Only SPACE-FREE segments are code packaging ("SMKU0475",
            # "RIC250LTR"). A digit-bearing segment WITH spaces ("50LTR
            # Vending Lid") is descriptive identity — dropping it turned
            # "50LTR Vending Lid-Ice Box" into an EXACT match for "ice
            # box", so the ₹250 lid outranked every actual ice box.
            while len(segs) > 1 and (segs[0].strip() == brand_l
                                      or (any(c.isdigit() for c in segs[0])
                                          and not re.search(r"\s", segs[0].strip()))):
                segs.pop(0)
            human = " ".join(segs) if segs else name
            name_core = [w for w in re.findall(r"[a-z]+", human)
                         if len(w) >= 3 and w not in _UNITS]
            model = (r.get('original_model') or '').lower()
            spec  = (r.get('specification') or '').lower()
            spec_ns = spec.replace(' ', '')
            _sem = r.get('_semsim') or 0
            score = 0
            tier = ''
            r['_boosted'] = ''   # pooled dicts are shared between terms
            # A model-number hit scores 1000 and the 0.6 cutoff below then
            # discards everything under 600 — so this branch must fire ONLY on
            # something that really is a code. The whole-term test needs a
            # DIGIT: plenty of "models" in the master are descriptive text
            # ("DCTC 1014 (PP) - PP Tray", "LV LID HANGER"), so a bare product
            # word matched them as a substring and scored 1000. A request for
            # "tray" then cut off at 621, just above the ~619 a name match can
            # reach, and all 473 rows actually named "...Tray..." were dropped
            # in favour of 3 rows whose model text happened to say "tray".
            # mtoks already covers letter+digit codes like WCCE001-SS.
            t_is_code = len(t) >= 4 and any(c.isdigit() for c in t)
            # Normalized full-string equality: typing a product's EXACT name
            # (or its name minus the BRAND-CODE- prefix) must beat every
            # sibling variant — "Moove Plate 22*" once lost to "Moove Plate".
            tn = re.sub(r"[^a-z0-9]+", " ", t).strip()
            if tn and (re.sub(r"[^a-z0-9]+", " ", name).strip() == tn
                       or re.sub(r"[^a-z0-9]+", " ", human).strip() == tn):
                score = 2000; tier = 'exact'                   # exact name — supreme
            elif model and (_model_exact(model, mtoks, t, t_is_code, numtoks)
                            or (mtoks and any(mt in model for mt in mtoks))
                            or (t_is_code and t in model)
                            or any(len(nt) >= 5 and nt in model for nt in numtoks)):
                # EXACT model equality must beat substring containment: asking
                # for WBS001-SS repeatedly matched EEWBS001-SS (a longer code
                # CONTAINING it) — every correction the team has logged was
                # this one bug. Separators stripped on both sides.
                score = 1500 if _model_exact(model, mtoks, t, t_is_code, numtoks) else 1000
                tier = 'model' if score == 1500 else 'model~'
            elif hsn_toks and (lambda h: h and any(
                    ht == h or (len(ht) >= 6 and h.startswith(ht))
                    for ht in hsn_toks))(
                        (r.get('hsn_code') or '').strip()):
                # HSN search: a typed 6-8 digit code lists its whole GST
                # family; sits under model codes (a model is more specific)
                # and the cheapest-first band orders the family by price.
                score = 1400
                tier = 'hsn'
            elif core and all(_covered(w, name, name_ns) for w in core):
                score = 600 - min(len(name), 120)              # full name match; tighter ranks higher
                tier = 'name'
            elif (len(core) > 1
                    and (any(_covered(w, name, name_ns) for w in core)
                         or _sem >= 0.72)
                    and all(_covered(w, name, name_ns)
                            or _covered(w, spec, spec_ns)
                    for w in core)):
                # Spec-completed coverage: words missing from the NAME are in
                # the SPECIFICATION — "round tray" finds a "Tray" whose spec
                # says "Round", and "food pan" finds CAMBRO's pans (named
                # just "Storage", identity in the spec). Anchored by a name
                # word OR by strong semantic agreement, and needs 2+ words —
                # so a lone word buried in an unrelated product's spec can't
                # fire (the old "SAFE" -> GLOVE bug). Ranks a step under a
                # pure name match of the same length.
                score = 500 - min(len(name), 120)
                tier = 'name+spec'
            elif (name_core and all(_covered(w, t, t_ns) for w in name_core)
                    and (len(name_core) >= 2 or len(core) < 2)):
                # (single-word names only qualify against single-word
                # requests: a product called just "Board" is not a credible
                # option for "iron board" — it drops the defining modifier.)
                # Reverse coverage: the product's ENTIRE name appears inside
                # the request. Measuring request->name punishes the user for
                # being specific — "Hair Dryer, Color - Black / Grey
                # Wall-Mounted" covers only 2 of its 7 words in "HAIR DRYER"
                # (29%) and was rejected, while the catalogue plainly had it.
                # Measured the other way it is 2/2, and the guard still holds:
                # "waste bin" vs "Ice bin module" is 1/3, because ice and
                # module were never asked for. Longer names score higher so
                # "Cup Dispenser" beats a bare "CUP", and this sits below the
                # forward full match so IRON ORGANISER still outranks IRON.
                score = 400 + 20 * len(name_core)
                tier = 'rname'
            elif core and (lambda hits: hits and hits / len(core) >= 0.6)(
                    sum(1 for w in core if _covered(w, name, name_ns))):
                # Partial name — but only if MOST of the request is accounted
                # for. Accepting a single shared word made "waste bin" match
                # "Ice bin module" (₹65,082) with full confidence.
                score = 200 + 10 * sum(1 for w in core if _covered(w, name, name_ns))
                tier = 'part'
            elif (core
                    and (lambda cnt: cnt >= 1 and cnt >= (
                        len(core) - 1 if (short or any(c.isdigit() for c in t))
                        else len(core)) and (cnt == len(core) and len(core) > 1
                                             or short
                                             or any(c.isdigit() for c in t)))(
                        sum(1 for w in core if _covered(w, spec, spec_ns)))):
                # Spec-only match needs TWO significant words — on one word it
                # fired on any product whose spec happened to contain it (a
                # request for "SAFE" returned "GLOVE LARGE"). Exceptions: a
                # single word WITH a size ("crock 1.2 qt") or a short
                # designator ("dw bowl", "gn pan") is already two
                # constraints — and with a size present, ONE uncovered word
                # is forgiven ("burgundy GLASS 750ml": LUCARIS never writes
                # the word glass; the size qualifier still narrows hard).
                score = 120; tier = 'spec'                     # specification match
            # Semantic fusion: similarity refines keyword scores (a dinner
            # plate outranks a gold-PLATED jigger for "plate"), and a strong
            # pure-meaning match becomes a candidate even with zero keyword
            # overlap ("keep food warm" -> FOOD WARMER). Sits below every
            # explicit tier: codes, exact names, corrections all still win.
            if score and _sem > 0.62:
                score += int((_sem - 0.62) * 600)
            elif not score and _sem >= 0.74:
                score = 150 + int((_sem - 0.74) * 1200)
                tier = 'sem'
            if score and prod_core and tier in ('name', 'name+spec', 'rname',
                                                'part', 'spec', 'sem'):
                # Product-label anchoring (see prod_core above). Word tiers
                # need at least one label word in the row; the scrap tiers
                # (spec/sem) need a strict majority OR the label's most
                # specific (longest) word — "Creamer, Small" may keep a
                # CREAMER on "creamer" alone, but a shared generic "board"
                # must not turn Flipchart into Chop Board.
                cov = {w for w in prod_core
                       if _covered(w, name, name_ns)
                       or _covered(w, spec, spec_ns)}
                # Spec-side coverage alone is not an anchor: series specs
                # list the whole range ("London" cutlery names every piece
                # in every row's spec), which let Cake Server ride into
                # TABLESPOON. At least one label word must be in the row
                # NAME — or the row must agree semantically (keeps "food
                # pan" -> CAMBRO rows named just "Storage").
                name_cov = any(_covered(w, name, name_ns) for w in prod_core)
                ok = name_cov or _sem >= 0.72
                if ok and tier in ('spec', 'sem'):
                    lw = max(len(w) for w in prod_core)
                    ok = (len(cov) > len(prod_core) // 2
                          or any(len(w) == lw and _covered(w, name, name_ns)
                                 for w in cov))
                if not ok:
                    score = 0
                    tier = ''
            if score:
                if ((brand_pref_l or file_pref_l)
                        and not (mtoks or t_is_code or re.search(r"\d{3}", t))):
                    # Gate only on real code-like tokens (3+ digit runs):
                    # "andy 27431" must resolve by the CODE, but a size like
                    # "kettle 1l" keeps its brand preference.
                    rb = (r.get('brand') or '').strip().upper()
                    if ((rb and any(b == rb or b in rb or rb in b for b in brand_pref_l))
                            or r.get('file_name') in file_pref_l):
                        # 1600 on EVERY tier of the named brand — uniform, so
                        # relative order inside the brand survives. Gating it
                        # to sub-1000 scores once let the plain "Finesse
                        # Table Fork" (reverse-name 460, boosted) leapfrog
                        # the typed "Finesse BLACK Table Fork" (exact 2000,
                        # unboosted). A named brand still outranks foreign
                        # exacts ("only indigo products; plate"), model-code
                        # queries never reach here (gate above), and generic
                        # file tokens can't trigger preference (vocab filter).
                        score += 1600      # the request named this brand/catalogue
                        r['_boosted'] = 'brand'
                if short:
                    score += 100 * sum(1 for w in short
                                       if re.search(r"\b" + re.escape(w) + r"\b", name))
                if numtoks and model and any(nt in model for nt in numtoks):
                    score += 300                               # the request named this model number
                if (r.get('price_3star') or 0) > 0: score += 30  # prefer rows that have a price
                if r.get('image_path'):       score += 5       # prefer rows that have an image
                if (_hist_ctx and score and score < 1500
                        and not (mtoks or t_is_code or re.search(r"\d{3}", t))):
                    # Passive learning. A RECURRING choice (2+) outranks even a
                    # foreign exact-name row — "go with last time" — while a
                    # single observation is only a nudge. Code-like queries are
                    # exempt (typed codes resolve by code, always), and human
                    # corrections still sit above everything.
                    hc = _hist_ctx.get(((r.get('product') or '').strip().lower(),
                                        (r.get('original_model') or '').strip().lower()))
                    if hc:
                        score += 1650 if hc >= 2 else 400
                        r['_boosted'] = 'hist'
                # Unrequested words in the HUMAN name (the space-aware
                # segment split keeps "50LTR Vending Lid" but drops pure
                # codes, so accessory words count and code fragments like
                # "RIFL"/"LWT" don't). Used by _cheap_first: a row adding
                # nothing the user didn't ask for ("Plastic Crate") is
                # closer than its accessory ("Plastic Crate Lid"), no
                # matter which is cheaper.
                r['_extra'] = sum(
                    1 for w in re.findall(r"[a-z]+", human)
                    if len(w) >= 3 and w not in _UNITS
                    and w not in (r.get('brand') or '').lower()
                    and w not in _collections
                    and not _covered(w, t, t_ns))
                if tier in ('name+spec', 'spec'):
                    # The identity lives (partly) in the spec here, so
                    # unrequested spec words count too: for "food pan",
                    # "FOOD PAN LID" is one step farther than "FOOD PAN".
                    # Only real catalogue-name words count — spec junk
                    # codes ("HDLN", "AMBHP") are noise, not identity.
                    r['_extra'] += sum(
                        1 for w in re.findall(r"[a-z]+", spec)
                        if len(w) >= 3 and w not in _UNITS
                        and _vocab.get(w, 0) >= 1
                        and not _covered(w, t, t_ns))
                r['_tier'] = tier; r['_score'] = score
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return []

        def _cheap_first(rows):
            # "The actual product, cheapest type first." Among rows that ARE
            # the requested product (strong match tier, no unrequested
            # type-words beyond what the top pick has), price decides —
            # a Casper table fork and a plain Table Fork both compete, the
            # ₹56 one wins. Rows that add a type-word (Crate LID, chill
            # PAD, LabelHOLDER) never enter the band. Weak tiers
            # (partial/spec-only/semantic-only) keep score order — there
            # the ranking IS the accuracy signal. History picks stay
            # pinned; a named brand competes only within that brand.
            strong = ('exact', 'model', 'model~', 'hsn', 'name', 'name+spec', 'rname')
            if len(rows) < 2 or rows[0].get('_tier') not in strong:
                return rows
            pf = ("price_4star"
                  if (tiers_req or ["3star"])[0] == "4star" else "price_3star")
            top = rows[0]
            ts = top.get('_score', 0)
            tsem = top.get('_semsim') or 0
            tex = top.get('_extra', 0)
            _pkey = lambda r: (r.get('_extra', 0),
                               (r.get(pf) or 0) <= 0, r.get(pf) or 0)
            if bool(re.search(r"\d", t)) or top.get('_tier') in ('model', 'model~'):
                # Digit-bearing requests (sizes, codes) stay conservative:
                # a contiguous same-tier, near-equal-score band only, so
                # "moove plate 22" can never be undercut by a cheaper
                # plain "Moove Plate".
                n = 1
                while (n < len(rows)
                       and rows[n].get('_boosted') == top.get('_boosted')
                       and rows[n].get('_tier') == top.get('_tier')
                       and ts - rows[n].get('_score', 0) <= 120
                       and (top.get('_tier') in ('exact', 'model', 'hsn')
                            or abs((rows[n].get('_semsim') or 0) - tsem) <= 0.06)):
                    n += 1
                if n < 2:
                    return rows
                if top.get('_tier') == 'hsn':
                    # extras are noise against a numeric code — priced
                    # first, then cheapest, nothing else
                    return sorted(rows[:n],
                                  key=lambda r: ((r.get(pf) or 0) <= 0,
                                                 r.get(pf) or 0)) + rows[n:]
                return sorted(rows[:n], key=_pkey) + rows[n:]
            # Word-only requests: EVERY row that is the same actual product
            # competes on price, wherever score ranked it — membership is
            # the guard, not list position. Members must FULLY cover the
            # request (exact/name/name+spec): a reverse-coverage row drops
            # requested words by definition ("insulated ice box" -> plain
            # "Ice Box"), so it can never buy its way up. Added type-words
            # beyond the top pick, different boost state, or distant
            # meaning also disqualify.
            full_cov = ('exact', 'name', 'name+spec')
            if top.get('_tier') not in full_cov:
                return rows
            members, others = [], []
            for rw in rows[:80]:
                if (rw.get('_tier') in full_cov
                        and rw.get('_boosted') == top.get('_boosted')
                        and rw.get('_extra', 0) <= tex
                        and abs((rw.get('_semsim') or 0) - tsem) <= 0.10):
                    members.append(rw)
                else:
                    others.append(rw)
            if len(members) < 2:
                return rows
            members.sort(key=_pkey)
            return members + others + rows[80:]
        # Keep only same-tier matches so the variant switcher shows genuine
        # alternatives. Drops weak partials (e.g. a row matching only "electric"
        # for a request of "electric kettle"). Capped at 420: an exact-name or
        # exact-model top hit (1500-2000) must not starve the switcher — "food
        # warmer" matched 5 rows named exactly that and hid the other 23
        # full-coverage food warmers behind a 1200 cutoff.
        cutoff = min(scored[0][0] * 0.6, 420)
        result = [r for s, r in scored if s >= cutoff]

        # Progressive refinement: if the request carries a spec value (wattage,
        # capacity, voltage…) or a model code, narrow to the rows that actually
        # have it — e.g. "kettle" → all kettles, "kettle 1500W" → only the 1500W.
        # Every capacity/size notation for the same value must behave alike:
        # "1L", "1.0L", "1 litre", "1 ltr" all narrow to the 1-litre rows.
        # Each detected qual expands into its notation family; a row passes
        # if it carries ANY spelling of ANY asked qual (same OR semantics).
        _UNIT_FAMILY = {"litre": "l", "litres": "l", "liter": "l",
                        "liters": "l", "ltr": "l", "ltrs": "l", "lt": "l",
                        "gram": "g", "grams": "g", "gm": "g", "gms": "g",
                        "qts": "qt", "quart": "qt", "quarts": "qt"}
        # Qualifier GROUPS: one group per distinct constraint the user
        # typed, alternate spellings INSIDE a group. A row must satisfy
        # EVERY group ("gn pan 1/3 150mm" needs the fraction AND the
        # depth — OR across groups once let a 1/9-150MM pan through on
        # the depth alone and price-first shipped the wrong size).
        qgroups = []
        covered_decimals = set()
        # dimension spans own their numbers — "oval plate 31x24 cm" must not
        # ALSO demand a standalone "24cm"
        _dimspans = [mm.span() for mm in re.finditer(
            r"\d{2,4}\s*[x*×]\s*\d{2,4}(?:\s*[x*×]\s*\d{2,4})?", t)]
        for um in re.finditer(
                r"(\d+(?:\.\d+)?)\s*(litres?|liters?|ltrs?|lt|grams?|gms?|quarts?|qts?|oz|kw|kg|g|w|ml|l|v|hz|mm|cm)\b",
                t):
            if any(a <= um.start() < b for a, b in _dimspans):
                continue
            val, unit = um.group(1), um.group(2)
            u = _UNIT_FAMILY.get(unit, unit)
            vals = {val}
            vals.add(val[:-2] if val.endswith(".0") else val + ".0")
            units = ({u, "litre", "liter", "ltr", "lt"} if u == "l"
                     else {u, "gram", "grams", "gm", "gms"} if u == "g" else {u})
            qgroups.append([v + un for v in vals for un in units])
            covered_decimals.add(val)
        # bare decimals not already claimed by a unit group
        for d in re.findall(r"\b\d+\.\d+\b", t):
            if d not in covered_decimals:
                qgroups.append([d])
        # Dimensions — "300x200", "300 x 200", "300*200*70". Normalized to a
        # glued x-form so any spacing/separator in prompt or catalogue meets.
        for d in re.findall(r"\d{2,4}\s*[x*×]\s*\d{2,4}(?:\s*[x*×]\s*\d{2,4})?", t):
            qgroups.append([re.sub(r"\s*[x*×]\s*", "x", d)])
        # Inch sizes — 5", 9'', 6 inch. These name most of the catalog's
        # near-identical variants (Scraper 3"/4"/5") and were previously
        # invisible to this filter, so every size collapsed onto one row.
        # Multiple inch values are a dia x ht pair ('4.5" dia x 1.25" ht'):
        # only the LARGEST (the defining size) becomes a constraint — the
        # height as its own AND-group made every row unsatisfiable.
        _inches = re.findall(r"(\d+(?:\.\d+)?)\s*(?:\"|''|inch|inches)", t)
        if _inches:
            qgroups.append([max(_inches, key=float) + '"'])
        # GN pan fractions — "food pan 1/1" must never return a 1/9 pan.
        for f in re.findall(r"\b[1-9]\s*/\s*[1-9]\b", t):
            qgroups.append([re.sub(r"\s", "", f)])
        for mt in mtoks:
            qgroups.append([mt])
        qgroups = [[q.replace(" ", "") for q in g if q.strip()]
                   for g in qgroups]
        qgroups = [g for g in qgroups if g]
        quals = [q for g in qgroups for q in g]
        def _hay(r):
            # The product NAME must be searched too: "Scraper 5"" carries
            # its size in the name, not the spec, so a model+spec-only
            # haystack could never match it.
            return ((r.get('product') or '') + ' ' +
                    (r.get('original_model') or '') + ' ' +
                    (r.get('specification') or '')).lower().replace(' ', '') \
                   .replace('*', 'x').replace('×', 'x')
        if qgroups:
            # Digit-left boundary: "ice box 25 ltr" must not match the
            # "25ltr" hiding inside "RIC125LTRWT". (Substring is fine when
            # the qual starts mid-token: "x200" etc. keep plain matching.)
            gpats = [[(re.compile(r"(?<![0-9.])" + re.escape(q))
                       if q[:1].isdigit() else None, q) for q in g]
                     for g in qgroups]
            def _qhit(r):
                hay = _hay(r)
                return all(any((p.search(hay) if p else q in hay)
                               for p, q in g) for g in gpats)
            narrowed = [r for r in result if _qhit(r)]
            if not narrowed:
                # Tier starvation: junk category-header rows ("CROCKERY-
                # Bone China") once outranked the real crocks and the
                # cutoff dropped them before this size filter could keep
                # them. The size IS the user's strongest signal — rescue
                # any scored row that carries it before giving up.
                narrowed = [r for s, r in scored if _qhit(r)]
            # No silent fallback. If the request names a size and nothing
            # carries it, returning the other sizes is worse than returning
            # nothing — it quotes the wrong goods at full confidence.
            return _cheap_first(narrowed)
        # A bare small number with no unit ("scraper 5 20" typed without the
        # inch mark) is still a size hint: SOFT-narrow to rows carrying that
        # number glued to a size marker (5", 5in, 5cm, 5qt...). Soft — if no
        # row carries it as a size, keep the full list rather than failing,
        # because a bare number might also be a harmless stray.
        nums = re.findall(r"\b(\d{1,2})\b", t)
        if nums and result:
            # Digit boundary on the left, or "scraper 5" matches the 5mm
            # hiding inside "145x95mm".
            pat = re.compile("|".join(
                rf"(?<![0-9.]){n}(?:\"|''|in|inch|cm|mm|l|ml|qt|ltr|oz|comp)" for n in nums))
            soft = [r for r in result if pat.search(_hay(r))]
            if soft:
                return _cheap_first(soft)
        return _cheap_first(result)

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

    # Lowered once for deterministic_match — re-lowering 3,000 names per
    # CALL made the fallback 28ms/line (42s of a giant BOQ's resolve).
    _low_products = [((p or ''), (p or '').lower(),
                      (p or '').lower().replace(" ", "")) for p in all_products]

    def deterministic_match(term):
        raw = re.findall(r"[a-z0-9]+", term.lower())
        core = [t for t in raw if len(t) >= 3 and t.isalpha() and t not in _UNITS]
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
        for p, pl, pl_ns in _low_products:
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
        # Giant BOQs (1,500+ unmatched lines) once ran a full suggestion
        # search PER miss — minutes of work for panels nobody has opened.
        # Past 150 lines the Find panel fetches its own suggestions live.
        if len(extracted) > 150:
            return []
        return suggest_products(conn, term, catalogs, limit) or []

    _cur_line = {}   # the loop points this at the item being resolved, so
                     # placeholders inherit its source-sheet section

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
            "section": _cur_line.get("section", ""),
            "src_key": _cur_line.get("src_key", ""),
            # Best-effort candidates so the Find panel opens with something
            # instead of an empty box. Suggestions only — never auto-applied.
            "_suggestions": suggestions or [],
        })

    for item in extracted:
        _cur_line.clear(); _cur_line.update(item)
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

        # This phrase's past human choices, loaded BEFORE searching so the
        # scoring loop can boost them (and the UI can badge them).
        hist_rows = _phrase_history(conn, original_kw)
        _hist_ctx.clear()
        _hist_ctx.update({(h["pl"], h["ml"]): h["c"] for h in hist_rows})

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
                             if len(w) >= 3 and w not in _UNITS}
                sug_words = {w for w in re.findall(r"[a-z]+", str(sem_match).lower())
                             if len(w) >= 3 and w not in _UNITS}
                # Check the HEAD noun — the last significant word, which
                # carries the product type ("waste BIN", "chef KNIFE"). An
                # adjective in common is not enough: "waste bin" vs
                # "Insulated Ice Box" shares nothing meaningful, while
                # "waste bin" vs "DUSTBIN" shares the head as a compound.
                # Size tokens masquerade as the head ("shaker 650mls",
                # "trolley 4compartments") — skip unit-plurals and check the
                # last TWO significant words, not just the very last one.
                heads = [w for w in reversed(
                    [w for w in re.findall(r"[a-z]+", original_kw.lower())
                     if len(w) >= 3 and w not in _UNITS
                     and w.rstrip("s") not in _UNITS])][:2]
                related = any(
                    h == b or h in b or b in h for h in heads for b in sug_words)
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
                # Match-quality debug — tiny, and makes "why did it pick
                # THIS row" answerable from the network tab alone.
                "_tier": v.get("_tier"), "_score": v.get("_score"),
                "_extra": v.get("_extra"),
            }

        # Deduplicate by product + model so the switcher shows distinct options
        seen = set(); uniq = []
        for v in variants:
            k = ((v.get("product") or "").strip().lower(), (v.get("original_model") or "").strip().lower())
            if k in seen:
                continue
            seen.add(k); uniq.append(_normalize(v))
        # Default 15 keeps the generate payload small — 34 lines x 50 variants
        # is a megabyte of JSON nobody scrolls. /api/product-variants raises it
        # for one product at a time, when the user asks to see the rest.
        variants_sorted = uniq[:variant_cap]
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
            # Past human choices for this phrase — badge + pinned Switch cards.
            "_hist": [{"product": h["p"], "model": h["m"] or "",
                       "count": h["c"], "client": h["cl"] or ""}
                      for h in hist_rows],
            # How many the cap held back, so the Switch panel can say what is
            # left instead of offering a "show more" that turns out to be empty.
            "_variants_total": len(uniq),
            "_requested":   item.get("product", ""),
            # Persisted (no underscore) — the learning loop needs to know, at
            # edit time, which phrase produced this line and who matched it.
            # Without `requested` a correction cannot be attributed; without
            # `matched_by` a re-save would re-learn lines a human already set.
            "requested":    item.get("product", ""),
            # non-underscore: survives the save, so the central LLM verify
            # phase judges the client's raw wording, not the typo-fixed one
            "req_raw":      item.get("_req_raw", ""),
            "matched_by":   matched_by,
            "section":      item.get("section", ""),
            "src_key":      item.get("src_key", ""),
            "boq_price":    float(item.get("boq_price") or 0),
            # "client asked 9 L — this is 7 L": nearest stocked size is a
            # normal offer, but it must be SAID, not silently quoted.
            "size_note":    _size_note(item.get("_fulltext") or search_term,
                                       f'{best.get("product", "")} '
                                       f'{best.get("specification", "")}'),
        })

    # LLM sanity pass over weak-evidence matches: the word scorer can't
    # tell a Teapot from a TEA CUP it part-covered — an LLM reads the
    # names. Fail-open: any API problem leaves the deterministic picks.
    # Worker processes defer it (llm_verify=False): 4 workers bursting
    # batches tripped the free-tier rate limit and throttled batches
    # silently kept junk — they attach slim candidate lists instead, and
    # _bg_match_rest verifies centrally, one paced batch at a time.
    if llm_verify:
        if groq_client is not None and result_items:
            try:
                _llm_verify_matches(groq_client, result_items)
            except Exception as e:
                print(f"LLM verify pass skipped (non-fatal): {e}")
    else:
        for it in result_items:
            if it.get("not_in_catalog") or it.get("matched_by") != "ai":
                continue
            vs = it.get("_variants") or []
            if _needs_verify(it):
                it["llm_cands"] = [
                    {k: v.get(k) for k in
                     ("product", "model_no", "brand", "specification",
                      "description", "hsn_code", "image_path", "price",
                      "cost", "gst_pct", "price_3star", "price_3star_usd",
                      "price_4star", "price_4star_usd")}
                    for v in vs[:5]]

    return result_items, not_found


@router.post("/api/boq-sections")
@limiter.limit("30/minute")
def boq_sections(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """List the sheets in an uploaded BOQ with each one's product-row count, so
    the UI can offer to match a giant multi-sheet workbook one section at a time
    instead of all 2,000+ rows at once. Parse only — no matching, so it's fast."""
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")
    suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".xls"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_path)
    try:
        _save_upload_validated(file, tmp_path)
        rows, _ = parse_boq_excel(str(tmp_path), file.filename, skip_images=True)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    counts, order = {}, []
    for r in rows:
        if not (r.get("product") or "").strip():
            continue
        s = (r.get("sheet_name") or "").strip()
        if s not in counts:
            counts[s] = 0
            order.append(s)
        counts[s] += 1
    return {"sections": [{"sheet": s, "lines": counts[s]} for s in order],
            "total": sum(counts.values())}


def _extract_bill_to(path):
    """Pull the client BILL & SHIP TO block out of a Melange quote/PI template —
    the column-A lines between the 'BILL & SHIP TO' label and the 'SUB:' line.
    The company letterhead ABOVE the label and the sales-person block (a
    different column) are deliberately left out. Returns '' when the layout
    has no such header (a plain client BOQ), so those uploads contribute
    nothing and the field stays blank."""
    try:
        import pandas as pd
        df = pd.read_excel(path, sheet_name=0, header=None, nrows=30)
    except Exception:
        return ""

    def a(r):
        try:
            v = df.iat[r, 0]
        except Exception:
            return ""
        s = "" if v is None else str(v).strip()
        return "" if s.lower() == "nan" else s

    start = None
    for r in range(len(df)):
        v = a(r).lower()
        if "bill" in v and "ship" in v:
            start = r + 1
            break
    if start is None:
        return ""
    stop = ("sub:", "sub ", "dear", "ref no", "ref:", "date",
            "sl.no", "sl no", "s.no", "sr.no")
    lines = []
    for r in range(start, min(len(df), start + 12)):
        v = a(r)
        if v.lower().startswith(stop):
            break
        if v:
            lines.append(v)
    return "\n".join(lines).strip()


def _extract_sales_person(path):
    """Pull the salesperson from a Melange quote/PI header — the name after
    'SALES CONCERN PERSON' and the 'MAIL ID' email, wherever they sit in the
    header. Returns (name, email); either may be '' when not present."""
    try:
        import pandas as pd
        df = pd.read_excel(path, sheet_name=0, header=None, nrows=30)
    except Exception:
        return "", ""
    name = email = ""
    for r in range(len(df)):
        for c in range(min(df.shape[1], 15)):
            try:
                v = df.iat[r, c]
            except Exception:
                continue
            s = "" if v is None else str(v).strip()
            if not s or s.lower() == "nan":
                continue
            low = s.lower()
            if "sales concern person" in low and not name:
                after = s.split(":", 1)[1].strip() if ":" in s else ""
                name = re.sub(r"^(mr|mrs|ms|m/s)\.?\s+", "", after, flags=re.I).strip()
            if not email:
                m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", s)
                if m:
                    email = m.group(0)
    return name, email


@router.post("/api/smart-generate-from-boq")
@limiter.limit("30/minute")
def smart_generate_from_boq(
    request: Request,
    file: UploadFile = File(...),
    client_name: str = Form(""),
    tiers: str = Form("3star"),
    sheets: str = Form(""),
    user: dict = Depends(get_current_user),
):
    """Client requirement BOQ upload — the file-based counterpart to
    /api/smart-generate. A client's own Excel (product + qty per row, no
    pricing needed) is parsed and every row matched against the Master
    Table via the exact same resolver, so typing a requirement and
    uploading one produce identically-priced results. `sheets` (a "|"-joined
    list of sheet names) limits matching to those sections; empty = all."""
    try:
        return _strip_cost(
            _smart_generate_from_boq(file, client_name, tiers, user, sheets), user)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        raise server_error(e, "Request")


def _smart_generate_from_boq(file: UploadFile, client_name: str, tiers_str: str,
                             user: dict, sheets_str: str = ""):
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")

    api_key = os.environ.get("GROQ_API_KEY", "") or GROQ_API_KEY_DEFAULT
    if make_llm_client() is None:
        raise HTTPException(400, "No LLM API key configured (set ANTHROPIC_API_KEY or GROQ_API_KEY)")

    suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".xls"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_path)
    source_file = ""
    bill_to_extracted = ""
    sp_name = sp_email = ""
    try:
        _save_upload_validated(file, tmp_path)
        # skip_images: matched items show MASTER-catalogue photos; reading
        # the client file's 1,000+ embedded pictures here cost ~85s for data
        # this flow never uses.
        rows, _structure = parse_boq_excel(str(tmp_path), file.filename,
                                           skip_images=True)
        try:
            from app.master_table import detect_file_type
            file_type = detect_file_type(str(tmp_path))
        except Exception:
            file_type = None
        # Keep the ORIGINAL workbook so the quotation can export as a
        # revised copy of it — same sheets and format, new prices.
        try:
            import hashlib
            from app.config import DATA_DIR
            raw = tmp_path.read_bytes()
            src_dir = Path(DATA_DIR) / "boq_sources"
            src_dir.mkdir(parents=True, exist_ok=True)
            source_file = hashlib.sha1(raw).hexdigest() + suffix
            dest = src_dir / source_file
            if not dest.exists():
                dest.write_bytes(raw)
        except Exception:
            source_file = ""
        # Client BILL & SHIP TO block + salesperson from the header (Melange
        # quote/PI template) — used below to pre-fill the quote.
        try:
            bill_to_extracted = _extract_bill_to(str(tmp_path))
        except Exception:
            bill_to_extracted = ""
        try:
            sp_name, sp_email = _extract_sales_person(str(tmp_path))
        except Exception:
            sp_name = sp_email = ""
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass  # Windows may still hold a lock briefly — non-fatal

    # Section-by-section: a giant multi-sheet workbook (65 sheets, 2,000+ rows)
    # can be matched one area at a time instead of all at once. sheets_str is a
    # "|"-joined list of sheet names to keep (| because a sheet name can contain
    # commas); empty means every sheet, the original behaviour. The saved source
    # workbook above stays whole, so the export still rebuilds every sheet.
    if sheets_str:
        want = {s.strip() for s in sheets_str.split("|") if s.strip()}
        if want:
            rows = [r for r in rows if (r.get("sheet_name") or "").strip() in want]
            if not rows:
                raise HTTPException(
                    400, "None of the selected sections had readable product rows.")

    # A client's BOQ often lists the SAME generic category label across many
    # rows (e.g. "Bowl Kitchen S/S Conical" for five different sizes) with the
    # actual distinguishing detail sitting in the MODEL NO / SPECIFICATION
    # columns instead. Matching on the label alone would collapse all of them
    # onto whichever single master-table row scores highest for that label —
    # so the search term folds in model_no + specification too, giving
    # search_catalog's model-number/spec ranking the signal it needs to tell
    # rows apart. "product" itself stays the clean label, used for display
    # and the not_found list.
    def _full_text(r):
        parts = [r.get("product") or "", r.get("model_no") or "", r.get("specification") or ""]
        return " ".join(p.strip() for p in parts if p and p.strip())

    def _search_term(r):
        term = _full_text(r)
        # Scoring cost grows with every word × every candidate row; past
        # ~20 words a spec is boilerplate, not signal — but sizes cut off
        # by the cap are re-appended (they are the signal).
        return _term_with_sizes(" ".join(term.split()[:20]), term)

    # The client's own price per row (their budget/target, or a competitor's
    # quote) — carried through so it can be compared against our Master Table
    # price for a profit/margin view. Absent entirely for sheets with no
    # price column at all (a pure requirement list).
    extracted = [
        {"product": r.get("product", ""), "search_term": _search_term(r),
         "_fulltext": _full_text(r),
         "model_no": r.get("model_no", ""),
         # source sheet — the quote view groups by it (tabs) and the final
         # bill rebuilds one sheet per section, mirroring the upload
         "section": r.get("sheet_name", ""),
         "src_key": f"{r.get('sheet_name','')}|{(r.get('_src') or {}).get('row',0)}",
         "qty": int(r.get("qty") or 1), "boq_price": float(r.get("price") or 0)}
        for r in rows if (r.get("product") or "").strip()
    ]
    if not extracted:
        raise HTTPException(400, "No product/quantity rows could be read from this file.")

    has_boq_pricing = any(it["boq_price"] > 0 for it in extracted)

    tiers_requested = [t.strip() for t in (tiers_str or "").split(",") if t.strip() in ("3star", "4star")]
    tiers = tiers_requested or ["3star"]

    groq_client = make_llm_client()
    conn = get_db()

    # Progressive matching for GIANT BOQs: resolve the first chunk inline so
    # the quote is on screen in seconds, park the rest as pending rows, and
    # let a background thread fill them in (the UI polls /live and re-renders
    # as chunks land). Small files keep the plain synchronous path.
    progressive = len(extracted) > PROG_THRESHOLD
    rest = []
    if progressive:
        first, rest = extracted[:PROG_FIRST], extracted[PROG_FIRST:]
        result_items, not_found = _resolve_master_matches(conn, first, [], tiers, groq_client, prompt="")
        for it in rest:
            result_items.append(
                _pending_stub(it, len(result_items) + 1, tiers_requested))
    else:
        result_items, not_found = _resolve_master_matches(conn, extracted, [], tiers, groq_client, prompt="")

    if not progressive and all(i.get("not_in_catalog") for i in result_items):
        # Nothing in the BOQ matched (placeholders don't count) — don't save
        # an empty quotation.
        conn.close()
        return {"ref_no": None, "client_name": client_name, "items": [],
                "not_found": not_found, "unsaved": True}

    ref_no = f"QT-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    data = {"ref_no": ref_no, "client_name": client_name, "has_boq_pricing": has_boq_pricing,
            "file_type": file_type, "tiers": tiers_requested,
            "source_file": source_file,
            "items": result_items, "not_found": not_found}
    # Pre-fill Bill & Ship To from the uploaded file's header, but only when the
    # user didn't type a client themselves — never overwrite what they entered.
    if bill_to_extracted and not (client_name or "").strip():
        data["bill_to"] = bill_to_extracted
    # Pre-fill the salesperson from the header. Prefer matching the file's name
    # or email to the saved team (so the stored phone/email/region flow in and
    # the "Prepared By" dropdown pre-selects); fall back to the file's raw name.
    if (sp_name or sp_email) and not data.get("sales_person"):
        sp = None
        try:
            if sp_email:
                sp = conn.execute("SELECT id,name,phone,email,region FROM sales_persons "
                                  "WHERE lower(email)=lower(?)", (sp_email,)).fetchone()
            if sp is None and sp_name:
                sp = conn.execute("SELECT id,name,phone,email,region FROM sales_persons "
                                  "WHERE upper(name)=upper(?)", (sp_name,)).fetchone()
        except Exception:
            sp = None
        if sp:
            data["sales_person"] = {"id": sp["id"], "name": sp["name"], "phone": sp["phone"],
                                    "email": sp["email"], "region": sp["region"]}
        elif sp_name:
            data["sales_person"] = {"name": sp_name, "email": sp_email}

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
    if progressive:
        qid = data["id"]
        _MATCH_JOBS[qid] = {"done": PROG_FIRST, "total": len(extracted), "running": True}
        threading.Thread(target=_bg_match_rest,
                         args=(qid, rest, tiers, PROG_FIRST, api_key, PROG_CHUNK),
                         daemon=True).start()
        data["matching"] = dict(_MATCH_JOBS[qid])
    return data


# Background matching registry for progressive BOQ quotes — in-memory is
# enough: if the process restarts mid-job, the remaining rows simply stay
# as pending placeholders the user can price by hand or regenerate.
_MATCH_JOBS = {}


def _match_chunk_worker(payload):
    """Runs in a WORKER PROCESS: resolve one chunk read-only and return the
    cleaned items. No DB writes here — the parent is the single writer."""
    offset, chunk, tiers, api_key = payload
    conn = get_db()
    try:
        groq_client = make_llm_client()
        new_items, nf = _resolve_master_matches(
            conn, chunk, [], tiers, groq_client, prompt="", llm_verify=False)
        clean = [{k: v for k, v in x.items() if not k.startswith("_")}
                 for x in new_items]
        return offset, clean, nf
    finally:
        conn.close()


def _bg_match_rest(qid, rest, tiers, start_idx, api_key, chunk_size):
    """Resolve the remaining BOQ lines in PARALLEL worker processes (the
    matcher is CPU-bound Python, so threads can't scale it), merging each
    finished chunk into the saved quotation as it lands. Single writer:
    only this thread touches items_json while matching runs."""
    import concurrent.futures as cf
    import multiprocessing as mp
    try:
        payloads = [(i, rest[i:i + chunk_size], tiers, api_key)
                    for i in range(0, len(rest), chunk_size)]
        done_lines = 0
        # spawn, not fork: forking a threaded uvicorn process can deadlock.
        # Worker count is _MATCH_WORKERS (default 8, env MATCH_WORKERS) — a
        # 2k-line BOQ was pinned to 4 cores on a 20-core box; more workers
        # cut its wall-clock proportionally while leaving headroom for serving.
        with cf.ProcessPoolExecutor(max_workers=_MATCH_WORKERS,
                                    mp_context=mp.get_context("spawn")) as pool:
            for fut in cf.as_completed([pool.submit(_match_chunk_worker, p)
                                        for p in payloads]):
                offset, clean, nf = fut.result()
                conn = get_db()
                try:
                    row = conn.execute(
                        "SELECT items_json FROM quotations WHERE id=?",
                        (qid,)).fetchone()
                    if not row:
                        break
                    data = json.loads(row["items_json"])
                    items = data.get("items", [])
                    for j, ni in enumerate(clean):
                        idx = start_idx + offset + j
                        if idx < len(items):
                            ni["sl_no"] = idx + 1
                            items[idx] = ni
                    data["items"] = items
                    if nf:
                        data["not_found"] = (data.get("not_found") or []) + nf
                    conn.execute("UPDATE quotations SET items_json=? WHERE id=?",
                                 (json.dumps(data), qid))
                    conn.commit()
                finally:
                    conn.close()
                done_lines += len(clean)
                job = _MATCH_JOBS.get(qid)
                if job:
                    job["done"] = min(start_idx + done_lines, job["total"])
        # ── Central LLM verify phase ──
        # Workers attach llm_cands instead of verifying inline; here the
        # whole quote's weak lines verify in ONE paced sequence (retry on
        # throttle, duplicates share a verdict) — bursting from 4 workers
        # hit the rate limit and silently kept junk picks.
        job = _MATCH_JOBS.get(qid)
        if job:
            job["phase"] = "verifying"
        groq_client = make_llm_client()
        if groq_client:
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT items_json FROM quotations WHERE id=?",
                    (qid,)).fetchone()
                if row:
                    data = json.loads(row["items_json"])
                    items = data.get("items", [])
                    try:
                        _llm_verify_matches(groq_client, items, paced=True)
                    except Exception as e:
                        print(f"central LLM verify skipped (non-fatal): {e}")
                    for it in items:
                        it.pop("llm_cands", None)
                    data["items"] = items
                    conn.execute(
                        "UPDATE quotations SET items_json=? WHERE id=?",
                        (json.dumps(data), qid))
                    conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"background BOQ matching failed for quote {qid}: {e}")
    finally:
        job = _MATCH_JOBS.get(qid)
        if job:
            job["running"] = False


@router.post("/api/quotations/{qid}/rematch")
def rematch_quotation(qid: int, user: dict = Depends(get_current_user)):
    """Re-run every line through the CURRENT matcher. Saved quotations keep
    their generation-time picks by design; this button re-decides them with
    today's logic. BOQ-sourced quotes re-parse their stored source workbook
    so the client's spec/section context is kept; typed quotes re-resolve
    from each line's original phrase. Current quantities survive; manual
    per-line price edits on re-matched lines are replaced by current prices.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Not found")
    _check_quote_access(row, user)
    data = json.loads(row["items_json"])
    old_items = data.get("items", [])

    extracted = None
    src = data.get("source_file") or ""
    if re.fullmatch(r"[0-9a-f]{40}\.(xlsx|xls)", src):
        from app.config import DATA_DIR
        sp = Path(DATA_DIR) / "boq_sources" / src
        if sp.exists():
            try:
                rows_p, _ = parse_boq_excel(str(sp), src, skip_images=True)

                def _full(r):
                    return " ".join(x.strip() for x in
                                    [r.get("product") or "", r.get("model_no") or "",
                                     r.get("specification") or ""] if x and x.strip())

                def _sterm(r):
                    t = _full(r)
                    return _term_with_sizes(" ".join(t.split()[:20]), t)
                extracted = [
                    {"product": r.get("product", ""), "search_term": _sterm(r),
                     "_fulltext": _full(r),
                     "model_no": r.get("model_no", ""),
                     "section": r.get("sheet_name", ""),
                     "src_key": f"{r.get('sheet_name','')}|{(r.get('_src') or {}).get('row',0)}",
                     "qty": int(r.get("qty") or 1),
                     "boq_price": float(r.get("price") or 0)}
                    for r in rows_p if (r.get("product") or "").strip()]
            except Exception as e:
                print(f"rematch source re-parse failed, using saved lines: {e}")
    if extracted is None:
        extracted = [
            {"product": (it.get("requested") or it.get("product") or "").strip(),
             "section": it.get("section", ""),
                "src_key": it.get("src_key", ""),
             "qty": int(it.get("qty") or 1),
             "boq_price": float(it.get("boq_price") or 0)}
            for it in old_items
            if (it.get("requested") or it.get("product") or "").strip()]
    if not extracted:
        conn.close()
        raise HTTPException(400, "Nothing to re-match on this quotation.")

    # current on-screen quantities win over the file's originals
    for i, it in enumerate(extracted):
        if i < len(old_items) and int(old_items[i].get("qty") or 0) > 0:
            it["qty"] = int(old_items[i]["qty"])

    api_key = os.environ.get("GROQ_API_KEY", "") or GROQ_API_KEY_DEFAULT
    groq_client = make_llm_client()
    tiers = [t for t in (data.get("tiers") or ["3star"]) if t in ("3star", "4star")] or ["3star"]

    # Giant quotes re-match PROGRESSIVELY like uploads: the old synchronous
    # path was single-core (~5 min for 1,900 lines) against nginx's 300s
    # timeout — the request died mid-way and the screen kept a mix of old
    # and new picks.
    progressive = len(extracted) > PROG_THRESHOLD
    if progressive:
        first, rest = extracted[:PROG_FIRST], extracted[PROG_FIRST:]
        result_items, not_found = _resolve_master_matches(
            conn, first, [], tiers, groq_client, prompt="")
        for it in rest:
            result_items.append(
                _pending_stub(it, len(result_items) + 1,
                              data.get("tiers") or ["3star"]))
    else:
        result_items, not_found = _resolve_master_matches(
            conn, extracted, [], tiers, groq_client, prompt="")

    data["items"] = [{k: v for k, v in i.items() if not k.startswith("_")}
                     for i in result_items]
    data["not_found"] = not_found
    conn.execute("UPDATE quotations SET items_json=? WHERE id=?",
                 (json.dumps(data), qid))
    conn.commit()
    conn.close()
    log_action(user, "rematch_quotation", target=data.get("ref_no", str(qid)),
               after={"lines": len(result_items)})
    resp = dict(data)
    resp["items"] = result_items          # keep _variants etc. for the UI
    resp["id"] = qid
    if progressive:
        _MATCH_JOBS[qid] = {"done": PROG_FIRST, "total": len(extracted),
                            "running": True}
        threading.Thread(target=_bg_match_rest,
                         args=(qid, rest, tiers, PROG_FIRST, api_key,
                               PROG_CHUNK),
                         daemon=True).start()
        resp["matching"] = dict(_MATCH_JOBS[qid])
    return _strip_cost(resp, user)


@router.get("/api/quotations/{qid}/live")
def quotation_live(qid: int, user: dict = Depends(get_current_user)):
    """Current items + matching progress for a progressively-matched quote —
    the result screen polls this while the background job runs."""
    conn = get_db()
    row = conn.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Not found")
    _check_quote_access(row, user)
    data = json.loads(row["items_json"])
    items = data.get("items", [])
    pending = sum(1 for i in items if i.get("matched_by") == "pending")
    job = _MATCH_JOBS.get(qid) or {}
    return _strip_cost({"items": items, "not_found": data.get("not_found", []),
                        "matching": {"done": len(items) - pending,
                                     "total": len(items),
                                     "running": bool(job.get("running")),
                                     "phase": job.get("phase", "")}}, user)




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
    # Screen-only brand-wise discount percentages {brand: pct} — persisted
    # so the quote view restores them; exports never read this.
    brand_discounts: dict | None = None
    # Manual per-quote packing/freight charge — flat add, flows into BOTH
    # downloaded bill formats.
    freight_charge: float | None = None


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
    source_file = ""
    try:
        raw = await file.read()
        with open(tmp, "wb") as f:
            f.write(raw)
        rows, _ = parse_boq_excel(tmp, file.filename, skip_images=True)
        # Keep the ORIGINAL workbook: a quotation generated from it exports
        # as a revised copy of this very file (same sheets, same format,
        # new prices) instead of the company template.
        if rows:
            import hashlib
            from app.config import DATA_DIR
            src_dir = Path(DATA_DIR) / "boq_sources"
            src_dir.mkdir(parents=True, exist_ok=True)
            source_file = hashlib.sha1(raw).hexdigest() + suffix
            dest = src_dir / source_file
            if not dest.exists():
                dest.write_bytes(raw)
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
    return {"lines": lines, "source_file": source_file,
            "source_name": file.filename}


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
                # Every product change is EVIDENCE for the passive learner,
                # remembered or not — a one-off preference still says "a human
                # thought this fits that phrase".
                _record_history(conn, item.get("requested"), item.get("product"),
                                item.get("model_no"), row["client_name"],
                                "switch", user["id"])
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
            elif not (item.get("not_in_catalog") or item.get("matched_by") == "not_found"):
                # Line saved untouched — a confirmation. It may only STRENGTHEN
                # an existing correction for the same product, never create
                # one: the old INSERT turned every untouched AI line into an
                # authoritative mapping on every save, so a 1,900-line BOQ
                # canonized its own junk ("mobile bar" -> Oil Can was
                # "confirmed" 6x by nobody). _lookup_correction serves these
                # rows as matched_by=learned, which bypasses every guard —
                # only a human choice may mint one.
                conn.execute("""
                    UPDATE match_corrections
                       SET times_confirmed = times_confirmed + 1
                     WHERE phrase_norm = ?
                       AND LOWER(TRIM(product)) = LOWER(TRIM(?))
                       AND LOWER(TRIM(COALESCE(original_model,''))) =
                           LOWER(TRIM(COALESCE(?,'')))
                """, (ph, item.get("product") or "",
                      item.get("model_no") or ""))
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
    if req.brand_discounts is not None:
        data["brand_discounts"] = {str(k)[:60]: max(0.0, min(100.0, float(v or 0)))
                                   for k, v in req.brand_discounts.items()}
    if req.freight_charge is not None:
        data["freight_charge"] = max(0.0, float(req.freight_charge))
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


@router.get("/api/suggest-products")
def suggest_products_endpoint(q: str = "", limit: int = 6,
                              user: dict = Depends(get_current_user)):
    """Closest catalogue products for a line nothing matched.

    The generate response carries these as _suggestions, but underscore keys
    are stripped before the quotation is saved, so a reopened quote had none
    and the Find panel opened empty. Fetching on demand costs one narrow FTS
    query and works for any line at any time, rather than persisting six raw
    master rows per placeholder inside items_json.
    """
    term = (q or "").strip()
    if len(term) < 2:
        return {"items": []}
    conn = get_db()
    try:
        rows = suggest_products(conn, term, None, max(1, min(int(limit or 6), 20)))
    finally:
        conn.close()
    # Same rule as everywhere else: employees never see purchase cost.
    if (user or {}).get("role") != "admin":
        for r in rows:
            r.pop("cost", None)
    return {"items": rows}


@router.get("/api/product-variants")
def product_variants(q: str = "", limit: int = 60,
                     user: dict = Depends(get_current_user)):
    """The full alternatives list for one product, for the Switch panel.

    Generate caps each line at 15 variants, because a 34-line quote carrying
    50 alternatives each is a megabyte of JSON that nobody scrolls. That cap
    is invisible in the UI though — "dustbin" has 50 distinct matches and the
    panel silently showed 15. This re-runs the SAME resolver for a single
    product with a bigger cap, so the extra cards are ranked identically to
    the ones already on screen rather than by some second, different scorer.
    """
    term = (q or "").strip()
    if len(term) < 2:
        return {"items": []}
    conn = get_db()
    try:
        matched, _nf = _resolve_master_matches(
            conn, [{"product": term, "model_no": "", "qty": 1}], [], ["3star"], None,
            prompt="", variant_cap=max(1, min(int(limit or 60), 200)))
    finally:
        conn.close()
    items = (matched[0].get("_variants") or []) if matched else []
    total = (matched[0].get("_variants_total") or len(items)) if matched else 0
    # Same rule as everywhere else: employees never see purchase cost.
    if (user or {}).get("role") != "admin":
        for r in items:
            r.pop("cost", None)
    return {"items": items, "total": total}


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
    # One pass over the master table instead of one LOWER(TRIM())-scan per
    # line — that per-item query could not use an index and made this button
    # feel stuck on longer quotes.
    price_map = {}
    for r in conn.execute(
            "SELECT product, original_model, price_3star, price_3star_usd, "
            "price_4star, price_4star_usd FROM master_products"):
        k = ((r["product"] or "").strip().lower(),
             (r["original_model"] or "").strip().lower())
        price_map.setdefault(k, r)
    for item in items:
        product = (item.get("product") or "").strip()
        if not product:
            skipped += 1
            continue
        m = price_map.get((product.lower(),
                           (item.get("model_no") or "").strip().lower()))
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

    # A quotation generated from an UPLOADED workbook exports back into that
    # very file: priced client files become a revised copy (new prices, _V
    # bump), price-less BOQs get the SMI response columns appended per sheet
    # (MODEL|BRAND|IMAGE|SPECIFICATIONS|PRICE/PC|AMOUNT). Typed quotations
    # ship in the company's CYM-GWL design. On any write-back failure the
    # company format is the fallback, never an error page.
    # Every quotation — typed OR uploaded — downloads in the company's
    # QUOTATION bill format (letterhead, BILL & SHIP TO, PREPARED BY, priced
    # item sheets). The client's own uploaded layout is no longer handed back:
    # the deliverable is always a Melange quotation, populated with the matched
    # products and the Bill & Ship To / salesperson pulled from the file.
    path = build_final_bill(data, items)

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
            # AI usage tile: estimated Claude spend from logged token counts
            # vs the starting credit. Admin-only — it is a cost figure. The
            # real balance lives in the Anthropic console; this is the
            # at-a-glance estimate (accurate to ~a rupee).
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS ai_usage ("
                             "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, model TEXT, "
                             "input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0)")
                u = conn.execute("SELECT COALESCE(SUM(input_tokens),0) it, "
                                 "COALESCE(SUM(output_tokens),0) ot, COUNT(*) n "
                                 "FROM ai_usage").fetchone()
                it, ot, calls = u["it"], u["ot"], u["n"]
                spent = it / 1e6 * ANTHROPIC_PRICE_IN + ot / 1e6 * ANTHROPIC_PRICE_OUT
                mrow = conn.execute(
                    "SELECT COALESCE(SUM(input_tokens),0) it, COALESCE(SUM(output_tokens),0) ot "
                    "FROM ai_usage WHERE substr(ts,1,7)=?",
                    (datetime.now().strftime("%Y-%m"),)).fetchone()
                spent_m = mrow["it"] / 1e6 * ANTHROPIC_PRICE_IN + mrow["ot"] / 1e6 * ANTHROPIC_PRICE_OUT
                # Fold in spend made outside the app (isolated-copy testing etc.,
                # never logged per-call) into the LIFETIME total and remaining so
                # they match the real console balance; the monthly figure stays
                # the tool's own this-month usage.
                spent += ANTHROPIC_SPENT_OFFSET_USD or 0
                cred = ANTHROPIC_CREDIT_USD or 0
                # Daily spend (last 14 active days, oldest-first) for the graph.
                drows = conn.execute(
                    "SELECT substr(ts,1,10) d, COALESCE(SUM(input_tokens),0) it, "
                    "COALESCE(SUM(output_tokens),0) ot FROM ai_usage "
                    "GROUP BY d ORDER BY d DESC LIMIT 14").fetchall()
                daily = [{"d": r["d"][5:],
                          "inr": round((r["it"] / 1e6 * ANTHROPIC_PRICE_IN
                                        + r["ot"] / 1e6 * ANTHROPIC_PRICE_OUT) * USD_INR, 2)}
                         for r in reversed(drows)]
                out["ai_usage"] = {
                    "spent_usd": round(spent, 4), "spent_inr": round(spent * USD_INR, 2),
                    "spent_month_usd": round(spent_m, 4), "spent_month_inr": round(spent_m * USD_INR, 2),
                    "credit_usd": cred, "remaining_usd": round(max(0.0, cred - spent), 3),
                    "pct_used": round(min(100.0, spent / cred * 100), 2) if cred else 0,
                    "calls": calls, "input_tokens": it, "output_tokens": ot,
                    "daily": daily,
                }
            except Exception:
                out["ai_usage"] = None
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
        # An approved quote is the strongest signal there is: a human stood
        # behind every line. Feed each matched line to the passive learner.
        qrow = conn.execute("SELECT client_name, items_json FROM quotations WHERE id=?",
                            (req.quotation_id,)).fetchone()
        if qrow:
            try:
                for it in json.loads(qrow["items_json"]).get("items", []):
                    if it.get("requested") and it.get("product") and not it.get("not_in_catalog"):
                        _record_history(conn, it["requested"], it["product"],
                                        it.get("model_no"), qrow["client_name"],
                                        "approved", user["id"])
            except Exception as e:
                print(f"approval learning skipped (non-fatal): {e}")
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
