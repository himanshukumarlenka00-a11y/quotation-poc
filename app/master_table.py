"""
Parser for the Master Table — a distinct, admin-only product catalog
separate from the ad-hoc BOQ uploads in app/parser.py. Expects a
tiered-pricing column layout (3 Star / 4 Star price, each with an INR and
a USD column) and auto-maps header names case-insensitively so minor
wording differences across supplier files don't break it.
"""
import os, tempfile
from datetime import datetime
from difflib import SequenceMatcher
import pandas as pd
from app.images import _save_image_to_disk, _xlsx_sheet_images, _assign_images_by_span
from app.xls_converter import convert_xls_to_xlsx

try:
    from app.xls_image_extractor import extract_images_with_fallback as _xls_extract_images_fb
except Exception:
    _xls_extract_images_fb = None


COLUMN_ALIASES = {
    "sl_no":            ["SL NO", "SL. NO", "SLNO", "SL", "SR NO", "S NO"],
    "product":          ["PRODUCT"],
    # "MODEL NO" is how most supplier sheets write it; without it the column
    # was silently ignored on every file that used that heading.
    "original_model":   ["ORIGINAL MODEL", "MODEL", "MODEL NO", "MODEL NO.", "MODEL NUMBER"],
    "brand":            ["BRAND"],
    "specification":    ["SPECIFICATION", "SPEC"],
    "price_3star":      ["3 STAR PRICE"],
    "price_4star":      ["4 STAR PRICE"],
    "price_3star_usd":  ["3 STAR PRICE IN USED", "3 STAR PRICE IN USD", "3 STAR PRICE USD"],
    "price_4star_usd":  ["4 STAR PRICE IN USED", "4 STAR PRICE IN USD", "4 STAR PRICE USD"],
    # Single-price sheets: one selling price rather than a 3★/4★ split (e.g.
    # the Nilkamal list's "PRICES IN INR"). Mirrored into BOTH tiers when no
    # explicit tier columns exist — see the note where items are built.
    # Matching is exact on the normalized header, so plain "PRICE" here can
    # never swallow "3 STAR PRICE".
    "price_inr":        ["PRICES IN INR", "PRICE IN INR", "PRICE (INR)", "PRICE INR",
                         "PRICE", "PRICES", "AMOUNT", "AMOUNT IN INR", "RATE", "SELLING PRICE",
                         "BASE PRICE", "BASE PRICE (₹)", "BASE PRICE (INR)", "UNIT PRICE"],
    "price_usd":        ["PRICES IN USD", "PRICE IN USD", "PRICE (USD)", "PRICE USD",
                         "AMOUNT IN USD", "RATE USD"],
    "hsn_code":         ["HSN CODE", "HSN"],
    "gst_pct":          ["GST %", "GST%", "GST"],
    "original_brand":   ["ORIGINAL BRAND"],
    "mrp":              ["MRP"],
    "cost":             ["COST"],
    "cost_currency":    ["COST CURRENCY"],
    "category":         ["CATEGERY", "CATEGORY"],
    "unit":             ["UNIT"],
    "product_group":    ["GROUP"],
}


def _norm(s):
    return str(s or "").strip().upper()


def _find_header_row(df, max_scan=25):
    """Locate the header row and its field mapping.

    Shared by the parser and the scan report so the report can never describe
    a different mapping than the one the import will actually use.

    Scans well past the first few rows: supplier sheets routinely carry a
    title block, logo or notes above the table. A file with headers on row 7
    previously found nothing at all and imported 0 products with every column
    reported as missing. Rows are scored by how many fields they resolve, so a
    stray cell containing the word "product" can't beat the real header.
    """
    best_idx, best_map, best_score = 0, {}, -1
    for i in range(min(max_scan, len(df))):
        cm = _build_col_map(df.iloc[i].values)
        if "product" not in cm:
            continue
        score = len(cm)
        if score > best_score:
            best_idx, best_map, best_score = i, cm, score

    if best_score < 0:                      # no row looked like a header
        return 0, _build_col_map(df.iloc[0].values)
    return best_idx, best_map


# Fields a single source column deliberately feeds into more than one place —
# surfaced in the scan report so "one price column became both tiers" is
# visible rather than a hidden behaviour.
_FANOUT = {
    "price_inr": "price → 3★ and 4★ (₹)",
    "price_usd": "price → 3★ and 4★ ($)",
}


def describe_columns(filepath, max_samples=1):
    """Report how each column in the sheet will be interpreted on import.

    Read-only: it parses nothing and writes nothing, it just explains the
    mapping so an admin can catch a misread column BEFORE 493 products land
    with no price — which is exactly how the Nilkamal import failed silently.
    """
    import pandas as _pd
    xl = _pd.ExcelFile(filepath)
    sheet_name = xl.sheet_names[0]
    xl.close()
    df = _pd.read_excel(filepath, sheet_name=sheet_name, header=None)
    if df.empty:
        return {"sheet": sheet_name, "header_row": 0, "columns": [], "missing_fields": []}

    header_row_idx, col_map = _find_header_row(df)
    by_index = {idx: field for field, idx in col_map.items()}
    # Resolve again with learned mappings switched off, so the report can say
    # whether a column matched out of the box or because someone taught it.
    builtin_only = _build_col_map(df.iloc[header_row_idx].values, learned={})
    builtin_idx = set(builtin_only.values())

    cols = []
    for i, raw in enumerate(df.iloc[header_row_idx].values):
        header = str(raw).strip() if raw is not None and str(raw) != "nan" else ""
        if not header:
            continue
        sample = ""
        for r in range(header_row_idx + 1, min(header_row_idx + 1 + 8, len(df))):
            v = df.iloc[r, i]
            if v is not None and str(v).strip() and str(v) != "nan":
                sample = str(v).strip()[:44]
                break
        field = by_index.get(i)
        # The picture column is located by header name and used to attach
        # extracted images to rows — it holds no text to map. Reporting it as
        # "not recognised" invited an admin to teach it a field, which would
        # have broken image placement.
        if field is None and _norm(header) in ("IMAGE", "IMAGES"):
            cols.append({
                "header": header, "field": None, "label": "product photo — images attached automatically",
                "recognised": True, "source": "builtin", "special": "image", "sample": sample,
            })
            continue

        entry = {
            "header": header,
            "field": field,
            "label": _FANOUT.get(field, field) if field else None,
            "recognised": field is not None,
            "source": None if field is None else ("builtin" if i in builtin_idx else "learned"),
            "sample": sample,
        }
        if field is None:
            # Offer a starting point for the admin's decision, never a verdict.
            sug, conf, why = suggest_field(header)
            entry["suggested"] = sug
            entry["suggested_label"] = FIELD_LABELS.get(sug, sug) if sug else None
            entry["confidence"] = conf
            entry["reason"] = why
        cols.append(entry)

    return {
        "sheet": sheet_name,
        "header_row": header_row_idx + 1,          # 1-based, matches Excel
        "columns": cols,
        "recognised_count": sum(1 for c in cols if c["recognised"]),
        "unrecognised": [c["header"] for c in cols if not c["recognised"]],
        "missing_fields": [f for f in COLUMN_ALIASES if f not in col_map],
    }


# Human-readable names for the picker, so an admin isn't choosing between
# raw column identifiers like "price_3star_usd".
FIELD_LABELS = {
    "sl_no": "Serial number", "product": "Product name",
    "original_model": "Model / SKU code", "brand": "Brand",
    "specification": "Specification", "price_3star": "3★ price (₹)",
    "price_4star": "4★ price (₹)", "price_3star_usd": "3★ price ($)",
    "price_4star_usd": "4★ price ($)",
    "price_inr": "Selling price (₹) — fills both tiers",
    "price_usd": "Selling price ($) — fills both tiers",
    "hsn_code": "HSN code", "gst_pct": "GST %", "original_brand": "Original brand",
    "mrp": "MRP / list price", "cost": "Purchase cost", "cost_currency": "Cost currency",
    "category": "Category", "unit": "Unit (Nos/Pcs)", "product_group": "Product group",
}

# Words that hint at a field when the header is worded unfamiliarly. Checked
# only after exact aliases fail, so they can never override a known heading.
_FIELD_HINTS = {
    "price_inr": ["price", "rate", "amount", "value", "inr", "rs", "selling"],
    "price_usd": ["usd", "dollar", "$"],
    "cost": ["cost", "purchase", "buying", "landed"],
    "mrp": ["mrp", "list", "retail"],
    "gst_pct": ["gst", "tax", "vat", "slab"],
    "hsn_code": ["hsn", "sac", "tariff"],
    "original_model": ["model", "sku", "art", "item code", "code", "part"],
    "product": ["product", "item", "description", "particular", "material"],
    "brand": ["brand", "make", "manufacturer"],
    "specification": ["spec", "size", "dimension", "detail"],
    "unit": ["unit", "uom", "nos", "pcs"],
    "category": ["category", "categery", "type"],
    "product_group": ["group", "family", "segment"],
    "sl_no": ["sl", "sr", "serial", "s.no", "#"],
}


def suggest_field(header):
    """Best guess at what an unrecognised header means, with a confidence.

    Deliberately conservative: it returns a suggestion for the admin to
    accept or override, never a decision. A wrong guess applied silently is
    how a whole catalog gets mispriced.
    """
    h = _norm(header)
    if not h:
        return None, 0.0, ""
    hl = h.lower()

    # Near-miss against a known alias — "PRICES IN INR " vs "PRICES IN INR".
    best_field, best_score, why = None, 0.0, ""
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            a = alias.lower()
            if hl == a:
                return field, 1.0, f"exactly matches the known heading '{alias}'"
            ratio = SequenceMatcher(None, hl, a).ratio()
            if ratio > best_score:
                best_field, best_score, why = field, ratio, f"close to the known heading '{alias}'"
    if best_score >= 0.72:
        return best_field, round(best_score, 2), why

    # Keyword hints — catches wording no alias anticipated ("RATE PER PC").
    hint_best, hint_hits, hint_word = None, 0, ""
    for field, words in _FIELD_HINTS.items():
        matched = [w for w in words if w in hl]
        if len(matched) > hint_hits:
            hint_best, hint_hits, hint_word = field, len(matched), matched[0]
    if hint_best:
        # Quote the word that actually matched. Deriving it from the field
        # name produced misleading text — "ART. NO" was explained as
        # "mentions 'original'" when what matched was "art".
        return hint_best, 0.6, f"the heading contains \"{hint_word}\""

    # A weak character-similarity score is not evidence. At 0.5, "STOCK"
    # scored against "GST" and would have been offered as the GST column —
    # a suggestion that wrong is worse than admitting we don't know.
    return (best_field, round(best_score, 2), why) if best_score >= 0.65 else (None, 0.0, "")


def _learned_mappings():
    """Header -> field pairs an admin has previously confirmed.

    Kept in the DB rather than the alias list so a new supplier's wording is
    taught once and applied from then on, without a code change. Failures are
    swallowed: a missing table must never stop an import.
    """
    try:
        from app.db import get_db
        conn = get_db()
        try:
            return {r["header_norm"]: r["field"] for r in
                    conn.execute("SELECT header_norm, field FROM column_mappings")}
        finally:
            conn.close()
    except Exception:
        return {}


def _build_col_map(header_row, learned=None):
    """Map our internal field names -> column index (0-based).

    Resolution order: built-in COLUMN_ALIASES first, then anything an admin
    has taught us. Built-ins win so a learned entry can never silently
    override known-correct behaviour; learned entries only fill the gaps
    that previously made a column vanish from the import.
    """
    norm_headers = {_norm(v): i for i, v in enumerate(header_row)}
    col_map = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in norm_headers:
                col_map[field] = norm_headers[alias]
                break

    if learned is None:
        learned = _learned_mappings()
    for header, field in learned.items():
        if header in norm_headers:
            # An explicit lesson overrides the built-in guess. Restricting
            # this to unclaimed fields made teaching a no-op whenever a
            # built-in alias already held the field — the admin's instruction
            # appeared to be accepted and then did nothing. Wrong lessons are
            # recoverable: they are admin-only, audit-logged and deletable.
            col_map[field] = norm_headers[header]
    return col_map


def parse_master_excel(filepath: str, filename: str):
    """Parse a master-table Excel file into a list of product dicts.
    Returns (items, unmatched_fields) — unmatched_fields lists any of our
    expected fields that couldn't be found in the header, so the caller can
    surface a warning rather than silently importing incomplete data."""
    converted_temp_path = None
    if filepath.endswith(".xls"):
        fd, converted_temp_path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        if convert_xls_to_xlsx(filepath, converted_temp_path):
            filepath = converted_temp_path
        else:
            try:
                os.unlink(converted_temp_path)
            except OSError:
                pass
            converted_temp_path = None

    sheet_images = {}
    if filepath.endswith(".xlsx"):
        try:
            sheet_images = _xlsx_sheet_images(filepath)
        except Exception:
            sheet_images = {}
    elif _xls_extract_images_fb:
        try:
            exact, _leftover = _xls_extract_images_fb(filepath)
            for im in exact:
                h = _save_image_to_disk(im["data"])
                if h:
                    sheet_images.setdefault(im["sheet_index"], []).append((im["row"], im["col"], h))
        except Exception:
            pass

    try:
        xl = pd.ExcelFile(filepath)
        sheet_name = xl.sheet_names[0]
        xl.close()
    except Exception:
        if converted_temp_path:
            try:
                os.unlink(converted_temp_path)
            except OSError:
                pass
        return [], list(COLUMN_ALIASES.keys())

    df = pd.read_excel(filepath, sheet_name=sheet_name, header=None)

    header_row_idx, col_map = _find_header_row(df)
    unmatched = [f for f in COLUMN_ALIASES if f not in col_map]

    image_col = None
    header_vals = [_norm(v) for v in df.iloc[header_row_idx].values]
    if "IMAGE" in header_vals:
        image_col = header_vals.index("IMAGE")

    span_img = {}
    if image_col is not None and "product" in col_map:
        span_img, _guessed = _assign_images_by_span(df, header_row_idx, col_map["product"],
                                                      sheet_images.get(0, []), image_col, None)

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    items = []
    for r in range(header_row_idx + 1, len(df)):
        row = df.iloc[r]
        product = row[col_map["product"]] if "product" in col_map else None
        if pd.isna(product) or not str(product).strip():
            continue

        def gv(field):
            idx = col_map.get(field)
            if idx is None:
                return None
            v = row[idx]
            return None if pd.isna(v) else v

        gst_raw = _num(gv("gst_pct"))
        image_path = span_img.get(r, "")
        items.append({
            "file_name": filename,
            "sl_no": str(gv("sl_no") or ""),
            "product": str(gv("product") or "").strip(),
            "original_model": str(gv("original_model") or ""),
            "brand": str(gv("brand") or ""),
            "specification": str(gv("specification") or ""),
            # A sheet with a single "PRICES IN INR"/"AMOUNT" column has one
            # selling price and no tier split — mirror it into both tiers so
            # the product is immediately sellable. An explicit 3★/4★ column
            # always wins; the fallback only fills a tier left at 0.
            "price_3star": _num(gv("price_3star")) or _num(gv("price_inr")),
            "price_4star": _num(gv("price_4star")) or _num(gv("price_inr")),
            "price_3star_usd": _num(gv("price_3star_usd")) or _num(gv("price_usd")),
            "price_4star_usd": _num(gv("price_4star_usd")) or _num(gv("price_usd")),
            "hsn_code": str(gv("hsn_code") or ""),
            "gst_pct": gst_raw * 100 if gst_raw <= 1 else gst_raw,
            "original_brand": str(gv("original_brand") or ""),
            "mrp": _num(gv("mrp")),
            "cost": _num(gv("cost")),
            "cost_currency": str(gv("cost_currency") or "INR"),
            "category": str(gv("category") or ""),
            "unit": str(gv("unit") or ""),
            "product_group": str(gv("product_group") or ""),
            "image_path": image_path,
            "image_match": "exact" if image_path else "",
            "uploaded_at": datetime.now().isoformat(),
        })

    if converted_temp_path:
        try:
            os.unlink(converted_temp_path)
        except OSError:
            pass

    return items, unmatched


def parse_matched_boq_workbook(filepath: str, filename: str):
    """Parse a client-facing "matched BOQ" workbook — one sheet per product
    category, each holding the CLIENT's own original request columns
    (Product/Specification/Brand/Model No/Image...) immediately followed by
    OUR matched-catalog answer columns, which reuse several of the same
    field names (DESCRIPTION/MODEL NO/BRAND/IMAGE/SPECIFICATION). Because the
    names repeat, matching must be EXACT-CASE — the matched block is always
    written in ALL CAPS while the client's own columns are mixed-case — or a
    naive case-insensitive lookup silently grabs the client's original
    wording instead of the actual matched product (verified against real
    data: a case-insensitive match returned "Plate Flat -17 Cm-Arn-A", the
    client's own spec, instead of the correct "PLATE FLAT-PRIME-17 CM-ARN-A").

    This file format has no dedicated 3-star/4-star price columns like the
    KMW format — just a single PRICE/PC. The Master Table stores that one
    price only (mirrored into both price_3star and price_4star so the
    existing tier-selection UI still has a value to read regardless of
    which tier is picked) — it does NOT compute or persist a marked-up
    3-star/4-star split here. Any tier markup happens later, at quote
    generation, not baked into the catalog data itself.

    No GST column exists in this format; gst_pct is stored as 0 (not left
    as a None-driven default) so it's visibly flaggable for correction
    rather than silently reading as a plausible-looking guess.

    Returns (items, skipped_sheets) — skipped_sheets lists any sheet with no
    recognizable matched-catalog block (e.g. SUMMARY SHEET, or a pure
    client ask-list with no answer columns at all) that was skipped
    entirely, so the caller can surface that rather than silently dropping
    products.
    """
    sheet_images = {}
    try:
        sheet_images = _xlsx_sheet_images(filepath)
    except Exception:
        sheet_images = {}

    try:
        xl = pd.ExcelFile(filepath)
        sheet_names = xl.sheet_names
        xl.close()
    except Exception:
        return [], []

    items = []
    skipped_sheets = []

    for sheet_pos, sheet_name in enumerate(sheet_names):
        if sheet_name.strip().upper() == "SUMMARY SHEET":
            continue
        try:
            df = pd.read_excel(filepath, sheet_name=sheet_name, header=None)
        except Exception:
            skipped_sheets.append(sheet_name)
            continue

        header_row = None
        for i, row in df.iterrows():
            vals = [str(v).strip() if pd.notna(v) else "" for v in row.values]
            if "DESCRIPTION" in vals and "PRICE/PC" in vals:
                header_row = i
                break
        if header_row is None:
            skipped_sheets.append(sheet_name)
            continue

        header = [str(v).strip() if pd.notna(v) else "" for v in df.iloc[header_row].values]

        def find_exact(name):
            try:
                return header.index(name)
            except ValueError:
                return None

        desc_col  = find_exact("DESCRIPTION")
        model_col = find_exact("MODEL NO")
        brand_col = find_exact("BRAND")
        image_col = find_exact("IMAGE")
        spec_col  = find_exact("SPECIFICATION")
        hsn_col   = find_exact("HSN CODE")
        price_col = find_exact("PRICE/PC")

        if desc_col is None or price_col is None:
            skipped_sheets.append(sheet_name)
            continue

        # The client's own columns always sit BEFORE the matched block in
        # every sheet observed — used as a fallback name/spec/image source
        # for custom line items the matched block left blank.
        def find_fallback(*names):
            for idx in range(desc_col):
                if header[idx].upper() in names:
                    return idx
            return None

        fallback_desc_col  = find_fallback("PRODUCT", "DESCRIPTION")
        fallback_spec_col  = find_fallback("SPECIFICATION", "SPEC")
        fallback_image_col = find_fallback("IMAGE", "IMAGES")

        # Row boundaries for image row-span matching come from the client's
        # own description column, not the matched one — matched DESCRIPTION
        # is blank on exactly the custom/unmatched rows that still need an
        # image assigned.
        span_pcol = fallback_desc_col if fallback_desc_col is not None else desc_col
        span_img, _guessed = _assign_images_by_span(
            df, header_row, span_pcol, sheet_images.get(sheet_pos, []),
            image_col, fallback_image_col)

        def g(row_vals, idx):
            if idx is None:
                return ""
            v = row_vals[idx]
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return str(v).strip()

        def gn(row_vals, idx, default=0.0):
            if idx is None:
                return default
            try:
                v = row_vals[idx]
                return float(v) if pd.notna(v) else default
            except (TypeError, ValueError):
                return default

        sl_no = 0
        for df_row_idx, row in df.iloc[header_row + 1:].iterrows():
            vals = row.values

            price = gn(vals, price_col)
            if price <= 0:
                continue  # nothing to price/quote from this row

            description = g(vals, desc_col) or g(vals, fallback_desc_col)
            if not description:
                continue  # no usable product identity at all

            specification = g(vals, spec_col) or g(vals, fallback_spec_col)
            image_path = span_img.get(df_row_idx, "")
            sl_no += 1

            items.append({
                "file_name": filename,
                "sl_no": str(sl_no),
                "product": description,
                "original_model": g(vals, model_col),
                "brand": g(vals, brand_col),
                "specification": specification,
                "price_3star": price,
                "price_4star": price,
                "price_3star_usd": 0.0,
                "price_4star_usd": 0.0,
                "hsn_code": g(vals, hsn_col),
                "gst_pct": 0.0,
                "original_brand": "",
                "mrp": 0.0,
                "cost": price,
                "cost_currency": "INR",
                "category": sheet_name.strip(),
                "unit": "",
                "product_group": "",
                "image_path": image_path,
                "image_match": "exact" if image_path else "",
                "uploaded_at": datetime.now().isoformat(),
            })

    return items, skipped_sheets
