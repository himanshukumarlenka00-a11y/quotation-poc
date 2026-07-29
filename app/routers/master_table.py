import os, tempfile, traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from pydantic import BaseModel
from app.config import MASTER_UPLOADS_DIR
from app.db import get_db
from app.auth import get_current_user, require_role, log_action
from app.images import _image_file_path
from app.master_table import (parse_master_excel, parse_matched_boq_workbook,
                              describe_columns, detect_file_type)
from app.routers.catalog import _save_upload_validated

router = APIRouter()


class UpdateMasterPriceRequest(BaseModel):
    price_3star: float
    price_4star: float


class BulkTierPricingRequest(BaseModel):
    # None (both blank) = "USD-only" mode: keep the INR tier prices exactly as
    # they are and just convert them at usd_rate. Supplying both percentages
    # recomputes the INR prices off cost as before.
    pct_3star: Optional[float] = None
    pct_4star: Optional[float] = None
    usd_rate: float = 0   # INR per $1 — 0 means "don't touch USD prices"


def _insert_master_items(conn, items):
    for it in items:
        conn.execute(
            """INSERT INTO master_products
            (file_name, sl_no, product, original_model, brand, specification,
             price_3star, price_4star, price_3star_usd, price_4star_usd,
             hsn_code, gst_pct, original_brand, mrp, cost, cost_currency,
             category, unit, product_group, image_path, image_match, uploaded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (it["file_name"], it["sl_no"], it["product"], it["original_model"], it["brand"], it["specification"],
             it["price_3star"], it["price_4star"], it["price_3star_usd"], it["price_4star_usd"],
             it["hsn_code"], it["gst_pct"], it["original_brand"], it["mrp"], it["cost"], it["cost_currency"],
             it["category"], it["unit"], it["product_group"], it["image_path"], it["image_match"], it["uploaded_at"])
        )


@router.post("/api/master-table/scan")
async def scan_master_table(file: UploadFile = File(...), admin: dict = Depends(require_role("admin"))):
    """Preview a master-table file — parses it but never touches the
    database, so an admin can verify the extraction before committing via
    /upload. Same access rule as the regular upload."""
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")
    try:
        suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".xls"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
        _save_upload_validated(file, Path(tmp_path))
        items, unmatched = parse_master_excel(str(tmp_path), file.filename)
        # Column report — what each header will be interpreted as. Surfaced so
        # a misread price column is caught here rather than discovered later
        # as 493 unsellable products. Must run BEFORE the temp file is removed.
        try:
            columns = describe_columns(str(tmp_path))
        except Exception:
            columns = None
        # What KIND of file is this? Importing a past quotation as a catalog
        # is quiet and expensive — it happened with the OPM file and produced
        # 705 rows of client wording, a cost column holding our old selling
        # price, GST 0% throughout, and wrong product matches.
        try:
            file_type = detect_file_type(str(tmp_path))
        except Exception:
            file_type = None
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # Windows may still hold a lock briefly — non-fatal

        images_exact = sum(1 for it in items if it["image_match"] == "exact")
        return {
            "filename": file.filename,
            "total_products": len(items),
            "images_found": images_exact,
            "unmatched_columns": unmatched,
            "columns": columns,
            "file_type": file_type,
            "preview": items[:15],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Scan error: {type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}")


@router.post("/api/master-table/upload")
async def upload_master_table(file: UploadFile = File(...), admin: dict = Depends(require_role("admin"))):
    """Import a master-table file. Admin-only — see project memory
    'master-table-access-control': employees may only ever read this data."""
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")
    dest = MASTER_UPLOADS_DIR / file.filename
    try:
        _save_upload_validated(file, dest)
        items, unmatched = parse_master_excel(str(dest), file.filename)

        conn = get_db()
        conn.execute("DELETE FROM master_products WHERE file_name=?", (file.filename,))
        _insert_master_items(conn, items)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM master_products").fetchone()[0]
        conn.close()

        images_exact = sum(1 for it in items if it["image_match"] == "exact")
        log_action(admin, "upload_master_table", target=file.filename, after={"products": len(items)})
        return {
            "message": f"Imported '{file.filename}' — {len(items)} products, "
                       f"{images_exact} with a confirmed image. Master table total: {total} items.",
            "products_imported": len(items),
            "images_found": images_exact,
            "unmatched_columns": unmatched,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Import error: {type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}")


@router.post("/api/master-table/scan-matched-boq")
async def scan_matched_boq(
    file: UploadFile = File(...),
    admin: dict = Depends(require_role("admin")),
):
    """Preview a matched-BOQ workbook — parses it (single price per product,
    no tier markup applied here), but never touches the database. Lets an
    admin verify the extraction before committing via /upload-matched-boq."""
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")
    try:
        suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".xls"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
        _save_upload_validated(file, Path(tmp_path))
        items, skipped_sheets = parse_matched_boq_workbook(str(tmp_path), file.filename)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # Windows may still hold a lock briefly — non-fatal, matches scan_boq's approach

        images_exact = sum(1 for it in items if it["image_match"] == "exact")
        by_category = {}
        for it in items:
            by_category[it["category"]] = by_category.get(it["category"], 0) + 1
        return {
            "filename": file.filename,
            "total_products": len(items),
            "images_found": images_exact,
            "skipped_sheets": skipped_sheets,
            "categories": by_category,
            "preview": items[:15],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Scan error: {type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}")


@router.post("/api/master-table/upload-matched-boq")
async def upload_matched_boq(
    file: UploadFile = File(...),
    admin: dict = Depends(require_role("admin")),
):
    """Import a client-facing "matched BOQ" workbook (one sheet per product
    category, client's ask + our matched answer columns side by side, a
    single base price — stored as-is, no tier markup computed or persisted
    here) as its own separate Master Table entry — see
    parse_matched_boq_workbook's docstring for the column-matching rules.
    Admin-only, same access rule as the regular master-table upload."""
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")
    dest = MASTER_UPLOADS_DIR / file.filename
    try:
        _save_upload_validated(file, dest)
        items, skipped_sheets = parse_matched_boq_workbook(str(dest), file.filename)

        conn = get_db()
        conn.execute("DELETE FROM master_products WHERE file_name=?", (file.filename,))
        _insert_master_items(conn, items)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM master_products").fetchone()[0]
        conn.close()

        images_exact = sum(1 for it in items if it["image_match"] == "exact")
        log_action(admin, "upload_matched_boq", target=file.filename, after={"products": len(items)})
        msg = (f"Imported '{file.filename}' — {len(items)} products, "
               f"{images_exact} with a confirmed image. Master table total: {total} items.")
        if skipped_sheets:
            msg += f" Skipped {len(skipped_sheets)} sheet(s) with no matched-catalog data: {', '.join(skipped_sheets)}."
        return {
            "message": msg,
            "products_imported": len(items),
            "images_found": images_exact,
            "skipped_sheets": skipped_sheets,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Import error: {type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}")


@router.get("/api/master-table/files")
def list_master_files(user: dict = Depends(get_current_user)):
    """Any authenticated user may view — see 'master-table-access-control'."""
    conn = get_db()
    rows = conn.execute(
        "SELECT file_name, COUNT(*) as count, MAX(uploaded_at) as uploaded_at "
        "FROM master_products GROUP BY file_name ORDER BY uploaded_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/api/master-table/suggest")
def suggest_master_products(q: str = "", tier: str = "3star", user: dict = Depends(get_current_user)):
    """Type-ahead suggestions for the Generate Quote textarea — matches the
    current word/phrase being typed against the Master Table, sorted by
    price ascending. Any authenticated user may query this (read-only)."""
    term = (q or "").strip()
    if len(term) < 2:
        return []
    tier = tier if tier in ("3star", "4star") else "3star"
    price_col = f"price_{tier}"
    conn = get_db()
    rows = conn.execute(
        f"SELECT product, brand, {price_col} as price FROM master_products "
        f"WHERE product LIKE ? AND {price_col} > 0 "
        f"ORDER BY price ASC LIMIT 20",
        (f"%{term}%",)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/api/master-table")
def list_master_products(user: dict = Depends(get_current_user)):
    """Any authenticated user may view (read-only) — see 'master-table-access-control'.

    Purchase cost is stripped for non-admins. Employees need selling prices to
    build a quote, but what we PAY a supplier — and so our margin on every
    product — is commercially sensitive and was previously returned to every
    logged-in user. Removed server-side rather than hidden in the UI, so it
    never reaches the browser at all.
    """
    is_admin = (user or {}).get("role") == "admin"
    conn = get_db()
    rows = conn.execute("SELECT * FROM master_products ORDER BY file_name, sl_no").fetchall()
    conn.close()
    COST_FIELDS = ("cost", "cost_currency", "mrp")
    result = []
    for r in rows:
        item = dict(r)
        item["has_image"] = bool(_image_file_path(item.get("image_path", "")))
        if not is_admin:
            for f in COST_FIELDS:
                item.pop(f, None)
        result.append(item)
    return result


@router.put("/api/master-table/product/{product_id}")
def update_master_product_price(product_id: int, req: UpdateMasterPriceRequest,
                                 admin: dict = Depends(require_role("admin"))):
    """Edit a single product's 3-star/4-star price directly — admin-only,
    same access rule as every other master-table write. Used for quick
    corrections without re-uploading the whole source file."""
    if req.price_3star < 0 or req.price_4star < 0:
        raise HTTPException(400, "Price can't be negative")
    conn = get_db()
    row = conn.execute("SELECT id, product, price_3star, price_4star FROM master_products WHERE id=?",
                        (product_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Product not found")
    conn.execute("UPDATE master_products SET price_3star=?, price_4star=? WHERE id=?",
                 (req.price_3star, req.price_4star, product_id))
    conn.commit()
    conn.close()
    log_action(admin, "edit_master_product_price", target=row["product"],
               before={"price_3star": row["price_3star"], "price_4star": row["price_4star"]},
               after={"price_3star": req.price_3star, "price_4star": req.price_4star})
    return {"message": "Price updated.", "id": product_id,
            "price_3star": req.price_3star, "price_4star": req.price_4star}


@router.put("/api/master-table/bulk-tier-pricing/{filename:path}")
def bulk_set_tier_pricing(filename: str, req: BulkTierPricingRequest,
                           admin: dict = Depends(require_role("admin"))):
    """Set 3-star/4-star prices for an entire catalog at once from a discount
    percentage off each product's cost price — admin-only. Products with no
    cost recorded (cost=0, e.g. a file that never had that column filled
    in) are left untouched rather than zeroed out, and counted separately
    so the caller knows some rows didn't get updated. If usd_rate is given
    (INR per $1), the USD 3-star/4-star prices are computed from it too;
    usd_rate=0 leaves the USD columns untouched."""
    # Both percentages blank = convert the existing INR tier prices to USD and
    # leave the INR values alone. Anything else needs both percentages, since
    # setting only one tier off cost would leave the other stale.
    usd_only = req.pct_3star is None and req.pct_4star is None
    if usd_only:
        if req.usd_rate <= 0:
            raise HTTPException(400, "Enter both %, or a $ rate on its own to convert existing prices.")
    elif req.pct_3star is None or req.pct_4star is None:
        raise HTTPException(400, "Enter both percentages, or leave both blank and set only a $ rate.")
    elif req.pct_3star < 0 or req.pct_4star < 0:
        raise HTTPException(400, "Percentages can't be negative")
    if req.usd_rate < 0:
        raise HTTPException(400, "USD rate can't be negative")

    conn = get_db()
    rows = conn.execute(
        "SELECT id, cost, price_3star, price_4star FROM master_products WHERE file_name=?",
        (filename,)).fetchall()
    if not rows:
        conn.close()
        raise HTTPException(404, "No products found for that file")

    # Take a one-time snapshot of the pre-edit prices so "Reset" has something
    # to restore. Only rows never snapshotted before are touched, so repeated
    # bulk edits never overwrite the true original.
    conn.execute("""
        UPDATE master_products
           SET orig_price_3star     = price_3star,
               orig_price_4star     = price_4star,
               orig_price_3star_usd = price_3star_usd,
               orig_price_4star_usd = price_4star_usd
         WHERE file_name = ? AND orig_price_3star IS NULL
    """, (filename,))

    updated = 0
    skipped = 0

    for r in rows:
        if usd_only:
            # Convert whatever INR tier prices already exist. A row with no
            # tier price yet has nothing to convert — skip rather than zero it.
            p3 = r["price_3star"] or 0
            p4 = r["price_4star"] or 0
            if p3 <= 0 and p4 <= 0:
                skipped += 1
                continue
            conn.execute(
                "UPDATE master_products SET price_3star_usd=?, price_4star_usd=? WHERE id=?",
                (round(p3 / req.usd_rate, 2) if p3 > 0 else 0,
                 round(p4 / req.usd_rate, 2) if p4 > 0 else 0, r["id"]))
            updated += 1
            continue

        cost = r["cost"] or 0
        if cost <= 0:
            skipped += 1
            continue
        p3 = round(cost * (1 - req.pct_3star / 100), 2)
        p4 = round(cost * (1 - req.pct_4star / 100), 2)
        if req.usd_rate > 0:
            p3_usd = round(p3 / req.usd_rate, 2)
            p4_usd = round(p4 / req.usd_rate, 2)
            conn.execute("UPDATE master_products SET price_3star=?, price_4star=?, price_3star_usd=?, price_4star_usd=? WHERE id=?",
                         (p3, p4, p3_usd, p4_usd, r["id"]))
        else:
            conn.execute("UPDATE master_products SET price_3star=?, price_4star=? WHERE id=?", (p3, p4, r["id"]))
        updated += 1

    conn.commit()
    conn.close()
    log_action(admin, "bulk_set_tier_pricing", target=filename,
               after={"pct_3star": req.pct_3star, "pct_4star": req.pct_4star, "usd_rate": req.usd_rate,
                      "usd_only": usd_only, "updated": updated, "skipped": skipped})

    if usd_only:
        msg = (f"Converted {updated} product(s) to USD @ ₹{req.usd_rate}/$ "
               f"— 3★/4★ ₹ prices left unchanged.")
        if skipped:
            msg += f" Skipped {skipped} product(s) with no ₹ tier price to convert."
    else:
        msg = f"Updated {updated} product(s) — 3★ = cost -{req.pct_3star}%, 4★ = cost -{req.pct_4star}%"
        msg += f", USD @ ₹{req.usd_rate}/$." if req.usd_rate > 0 else "."
        if skipped:
            msg += f" Skipped {skipped} product(s) with no cost price recorded (left unchanged)."
    return {"message": msg, "updated": updated, "skipped": skipped}


class ConfirmMappingRequest(BaseModel):
    header: str          # header text exactly as it appears in the sheet
    field: str           # internal field it means, or "" to ignore the column
    source_file: str = ""


@router.get("/api/master-table/column-mappings")
def list_column_mappings(user: dict = Depends(get_current_user)):
    """Everything the system has been taught about column headers, plus the
    fields a header can be mapped to (so the UI never has to hardcode them)."""
    from app.master_table import COLUMN_ALIASES, FIELD_LABELS
    conn = get_db()
    try:
        learned = [dict(r) for r in conn.execute(
            "SELECT * FROM column_mappings ORDER BY times_seen DESC, header_norm")]
    finally:
        conn.close()
    return {
        "learned": learned,
        # Paired with readable names so the picker doesn't ask an admin to
        # choose between raw identifiers like "price_3star_usd".
        "fields": [{"field": f, "label": FIELD_LABELS.get(f, f)}
                   for f in sorted(COLUMN_ALIASES.keys())],
        "builtin": {f: a for f, a in COLUMN_ALIASES.items()},
    }


@router.post("/api/master-table/confirm-mapping")
def confirm_column_mapping(req: ConfirmMappingRequest,
                           admin: dict = Depends(require_role("admin"))):
    """Teach the system what a spreadsheet header means.

    Admin-only — a mapping decides which column becomes the price, so getting
    it wrong misprices a whole catalog. Per [[master-table-access-control]]
    only admins may change anything that feeds the master table.
    """
    from app.master_table import COLUMN_ALIASES, _norm
    header = _norm(req.header)
    field = (req.field or "").strip()
    if not header:
        raise HTTPException(400, "Header text is required.")
    if field and field not in COLUMN_ALIASES:
        raise HTTPException(400, f"Unknown field '{field}'.")

    conn = get_db()
    try:
        if not field:
            # Empty field = "ignore this column" — drop any prior instruction
            # rather than storing a mapping to nothing.
            conn.execute("DELETE FROM column_mappings WHERE header_norm=?", (header,))
            conn.commit()
            msg = f"'{header}' will be ignored from now on."
        else:
            conn.execute("""
                INSERT INTO column_mappings (header_norm, field, confirmed_by, confirmed_at, times_seen, source_file)
                VALUES (?,?,?,?,1,?)
                ON CONFLICT(header_norm) DO UPDATE SET
                    field=excluded.field,
                    confirmed_by=excluded.confirmed_by,
                    confirmed_at=excluded.confirmed_at,
                    times_seen=column_mappings.times_seen+1
            """, (header, field, admin["id"], datetime.now().isoformat(), req.source_file))
            conn.commit()
            msg = f"Learned: '{header}' means {field}. Future files using that heading map automatically."
    finally:
        conn.close()
    log_action(admin, "confirm_column_mapping", target=header, after={"field": field})
    return {"message": msg, "header": header, "field": field}


@router.delete("/api/master-table/column-mappings/{mapping_id}")
def delete_column_mapping(mapping_id: int, admin: dict = Depends(require_role("admin"))):
    """Forget a learned mapping — the undo for a wrong lesson."""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM column_mappings WHERE id=?", (mapping_id,))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(404, "No such mapping.")
    finally:
        conn.close()
    log_action(admin, "delete_column_mapping", target=str(mapping_id))
    return {"message": "Mapping removed."}


class AddFromBoqRequest(BaseModel):
    file_name: str = "Added from BOQ"
    items: list          # [{product, original_model, brand, specification, unit, hsn_code}]


@router.post("/api/master-table/check-boq")
async def check_boq_coverage(file: UploadFile = File(...),
                             user: dict = Depends(get_current_user)):
    """Do we already stock what this client BOQ asks for?

    Coverage check only — it reports which rows the Master Table covers and
    which it doesn't, and never writes anything or creates a quotation. The
    matching is the same resolver the quotation flow uses, so a row reported
    as "found" here will resolve to that same product when quoted.
    """
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")

    # Imported here rather than at module scope to keep the routers from
    # importing each other at startup.
    from app.parser import parse_boq_excel
    from app.routers.quotations import _resolve_master_matches
    from app.config import GROQ_API_KEY_DEFAULT
    from groq import Groq

    suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".xls"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_path)
    try:
        _save_upload_validated(file, tmp_path)
        rows, _ = parse_boq_excel(str(tmp_path), file.filename)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass   # Windows may hold the handle briefly — non-fatal

    src = [r for r in rows if (r.get("product") or "").strip()]
    if not src:
        raise HTTPException(400, "No product rows could be read from this file.")

    extracted = [{
        "product": r.get("product", ""),
        "qty": int(r.get("qty") or 1),
        # model + spec fold into the search term so near-identically named
        # rows (five sizes of one bowl) don't collapse onto one master row.
        "search_term": " ".join(p.strip() for p in (
            r.get("product") or "", r.get("model_no") or "",
            r.get("specification") or "") if p and p.strip()),
    } for r in src]

    api_key = os.environ.get("GROQ_API_KEY", "") or GROQ_API_KEY_DEFAULT
    conn = get_db()
    try:
        matched, _ = _resolve_master_matches(
            conn, extracted, [], ["3star"], Groq(api_key=api_key), prompt="")
    finally:
        conn.close()

    # The resolver returns only the rows it matched, in input order — walk
    # both together to line results back up with the original BOQ rows.
    found, missing, j = [], [], 0
    for row, ex in zip(src, extracted):
        if j < len(matched) and matched[j].get("_requested") == ex["product"]:
            m = matched[j]; j += 1
            found.append({
                "requested": ex["product"], "qty": ex["qty"],
                "matched": m.get("product", ""), "model_no": m.get("model_no", ""),
                "brand": m.get("brand", ""), "price": m.get("price_per_pc", 0),
            })
        else:
            missing.append({
                "product": (row.get("product") or "").strip(),
                "original_model": (row.get("model_no") or "").strip(),
                "brand": (row.get("brand") or "").strip(),
                "specification": (row.get("specification") or "").strip(),
                "unit": (row.get("unit") or "").strip(),
                "hsn_code": (row.get("hsn_code") or "").strip(),
                "qty": ex["qty"],
            })

    total = len(src)
    return {
        "filename": file.filename,
        "total": total,
        "found_count": len(found),
        "missing_count": len(missing),
        "coverage_pct": round(len(found) * 100 / total, 1) if total else 0,
        "found": found[:200],
        "missing": missing,
    }


@router.post("/api/master-table/add-from-boq")
def add_products_from_boq(req: AddFromBoqRequest,
                          admin: dict = Depends(require_role("admin"))):
    """Add BOQ rows we don't stock into the Master Table.

    Admin-only: this writes to the master catalog, which per the project's
    access rule only admins may modify. Products arrive with no pricing —
    they are placeholders for an admin to price, not sellable rows yet.
    """
    items = [i for i in (req.items or []) if (i.get("product") or "").strip()]
    if not items:
        raise HTTPException(400, "No products supplied.")

    conn = get_db()
    added = skipped = 0
    try:
        for it in items:
            product = (it.get("product") or "").strip()
            model = (it.get("original_model") or "").strip()
            # Don't create a duplicate if an identical product+model already
            # exists — re-checking the same BOQ shouldn't multiply rows.
            dup = conn.execute(
                """SELECT 1 FROM master_products
                    WHERE LOWER(TRIM(product))=LOWER(?)
                      AND LOWER(TRIM(COALESCE(original_model,'')))=LOWER(?) LIMIT 1""",
                (product, model)).fetchone()
            if dup:
                skipped += 1
                continue
            conn.execute("""
                INSERT INTO master_products
                    (file_name, sl_no, product, original_model, brand, specification,
                     price_3star, price_4star, price_3star_usd, price_4star_usd,
                     hsn_code, gst_pct, original_brand, mrp, cost, cost_currency,
                     category, unit, product_group, image_path, image_match, uploaded_at)
                VALUES (?,?,?,?,?,?,0,0,0,0,?,0,?,0,0,'INR','',?,'','','',?)
            """, (req.file_name, "", product, model, (it.get("brand") or "").strip(),
                  (it.get("specification") or "").strip(), (it.get("hsn_code") or "").strip(),
                  (it.get("brand") or "").strip(), (it.get("unit") or "").strip(),
                  datetime.now().isoformat()))
            added += 1
        conn.commit()
    finally:
        conn.close()

    log_action(admin, "add_products_from_boq", target=req.file_name,
               after={"added": added, "skipped": skipped})
    msg = f"Added {added} product(s) to the Master Table."
    if skipped:
        msg += f" Skipped {skipped} already present."
    if added:
        msg += " They have no price yet — set it before quoting them."
    return {"message": msg, "added": added, "skipped": skipped}


@router.put("/api/master-table/reset-tier-pricing/{filename:path}")
def reset_tier_pricing(filename: str, admin: dict = Depends(require_role("admin"))):
    """Undo bulk tier pricing for a catalog — restore each product's ₹/$ tier
    prices to the snapshot taken before the first bulk edit. Rows that were
    never bulk-edited have no snapshot and are left exactly as they are."""
    conn = get_db()
    rows = conn.execute("SELECT COUNT(*) c FROM master_products WHERE file_name=?",
                        (filename,)).fetchone()
    if not rows or not rows["c"]:
        conn.close()
        raise HTTPException(404, "No products found for that file")

    restorable = conn.execute(
        "SELECT COUNT(*) c FROM master_products WHERE file_name=? AND orig_price_3star IS NOT NULL",
        (filename,)).fetchone()["c"]
    if not restorable:
        conn.close()
        raise HTTPException(400, "Nothing to reset — this catalog's prices haven't been bulk-changed yet.")

    cur = conn.execute("""
        UPDATE master_products
           SET price_3star     = orig_price_3star,
               price_4star     = orig_price_4star,
               price_3star_usd = orig_price_3star_usd,
               price_4star_usd = orig_price_4star_usd
         WHERE file_name = ? AND orig_price_3star IS NOT NULL
    """, (filename,))
    restored = cur.rowcount

    # Clear the snapshot so the next bulk edit captures a fresh "original".
    conn.execute("""
        UPDATE master_products
           SET orig_price_3star=NULL, orig_price_4star=NULL,
               orig_price_3star_usd=NULL, orig_price_4star_usd=NULL
         WHERE file_name = ?
    """, (filename,))
    conn.commit()
    conn.close()
    log_action(admin, "reset_tier_pricing", target=filename, after={"restored": restored})
    return {"message": f"Reset {restored} product(s) back to their prices before the last bulk change.",
            "restored": restored}


@router.delete("/api/master-table/{filename:path}")
def delete_master_file(filename: str, admin: dict = Depends(require_role("admin"))):
    """Admin-only — see 'master-table-access-control'."""
    conn = get_db()
    conn.execute("DELETE FROM master_products WHERE file_name=?", (filename,))
    conn.commit()
    conn.close()
    dest = MASTER_UPLOADS_DIR / filename
    if dest.exists():
        dest.unlink()
    log_action(admin, "delete_master_table_file", target=filename)
    return {"message": f"'{filename}' removed from master table."}
