import os, tempfile, traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from pydantic import BaseModel
from app.config import MASTER_UPLOADS_DIR, server_error
from app.db import get_db, rebuild_master_fts
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


class AddProductRequest(BaseModel):
    product: str
    original_model: str = ""
    brand: str = ""
    specification: str = ""
    hsn_code: str = ""
    gst_pct: float = 18
    price: float = 0     # single price — mirrored into 3★/4★ (house rule)
    cost: float = 0
    image_data: str = ""  # optional data URL from the manual-add form
    file_name: str = ""   # which existing batch to file it under; default below


@router.post("/api/master-table/add-product")
def add_master_product(req: AddProductRequest, admin: dict = Depends(require_role("admin"))):
    """One product straight into the master table — the quote screen's
    'Add Product Manually' can persist its entry so the next quote finds it."""
    product = req.product.strip()
    if not product:
        raise HTTPException(400, "Product name is required")
    model = req.original_model.strip()
    conn = get_db()
    try:
        dup = conn.execute(
            """SELECT 1 FROM master_products
                WHERE LOWER(TRIM(product))=LOWER(?)
                  AND LOWER(TRIM(COALESCE(original_model,'')))=LOWER(?) LIMIT 1""",
            (product, model)).fetchone()
        if dup:
            raise HTTPException(409, "Already in the master table (same name and model).")
        image_path = ""
        if req.image_data.startswith("data:"):
            import base64
            from app.images import _save_image_to_disk
            try:
                image_path = _save_image_to_disk(
                    base64.b64decode(req.image_data.split(",", 1)[1]))
            except Exception:
                pass
        _insert_master_items(conn, [{
            "file_name": req.file_name.strip() or "Added manually",
            "sl_no": "", "product": product,
            "original_model": model, "brand": req.brand.strip(),
            "specification": req.specification.strip(),
            "price_3star": req.price, "price_4star": req.price,
            "price_3star_usd": 0, "price_4star_usd": 0,
            "hsn_code": req.hsn_code.strip(), "gst_pct": req.gst_pct,
            "original_brand": req.brand.strip(), "mrp": 0,
            "cost": req.cost, "cost_currency": "INR",
            "category": "", "unit": "", "product_group": "",
            "image_path": image_path,
            "image_match": "exact" if image_path else "",
            "uploaded_at": datetime.now().isoformat(),
        }])
        conn.commit()
        rebuild_master_fts(conn)
    finally:
        conn.close()
    log_action(admin, "add_master_product", target=product,
               after={"model": model, "price": req.price, "cost": req.cost})
    return {"message": f"'{product}' added to the master table."}


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
        # How many rows actually carry a price? 0 with products present means
        # the price column wasn't recognised (AMOUNT, PRICES IN INR, ...) —
        # the UI uses this to demand a column pick at scan time instead of
        # letting the import hit the Phase D gate later.
        priced = sum(1 for it in items
                     if (it.get("price_3star") or 0) > 0 or (it.get("cost") or 0) > 0)
        return {
            "filename": file.filename,
            "total_products": len(items),
            "priced_products": priced,
            "images_found": images_exact,
            "unmatched_columns": unmatched,
            "columns": columns,
            "file_type": file_type,
            "preview": items[:15],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise server_error(e, "Scan")


@router.post("/api/master-table/upload")
async def upload_master_table(file: UploadFile = File(...), force: str = Form(""),
                              admin: dict = Depends(require_role("admin"))):
    """Import a master-table file. Admin-only — see project memory
    'master-table-access-control': employees may only ever read this data."""
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")
    if force != "1":
        conn = get_db()
        exists = conn.execute("SELECT 1 FROM master_products WHERE file_name=? LIMIT 1",
                              (file.filename,)).fetchone()
        conn.close()
        if exists:
            raise HTTPException(409,
                f"A catalogue named '{file.filename}' is already in the master table. "
                f"Re-importing it will delete and replace all its existing products.")
    dest = MASTER_UPLOADS_DIR / file.filename
    try:
        _save_upload_validated(file, dest)
        items, unmatched = parse_master_excel(str(dest), file.filename)

        # Phase D: block the imports that are certainly broken, instead of
        # writing them and letting someone discover the damage later.
        if not items:
            raise HTTPException(400,
                "No products could be read from this file — the header row was "
                "not recognised. Scan it first to see how the columns map.")
        priced = sum(1 for it in items
                     if (it.get("price_3star") or 0) > 0 or (it.get("cost") or 0) > 0)
        if priced == 0 and force != "1":
            # Exactly how the Nilkamal import failed: 493 products landed with
            # no price because "PRICES IN INR" wasn't a recognised heading.
            # Blocking with the unmapped columns named lets the admin teach
            # the right one and re-import; force=1 overrides deliberately.
            raise HTTPException(409,
                f"Blocked: all {len(items)} products would import with NO price and NO cost. "
                f"A price column probably wasn't recognised — unmapped fields: "
                f"{', '.join(unmatched) or 'none'}. Teach the price column in the scan "
                f"report, or import anyway if this file genuinely has no prices.")

        conn = get_db()
        conn.execute("DELETE FROM master_products WHERE file_name=?", (file.filename,))
        _insert_master_items(conn, items)
        conn.commit()
        # keep the full-text index in step with the catalogue
        rebuild_master_fts(conn)
        total = conn.execute("SELECT COUNT(*) FROM master_products").fetchone()[0]
        conn.close()

        images_exact = sum(1 for it in items if it["image_match"] == "exact")
        log_action(admin, "upload_master_table", target=file.filename,
                   after={"products": len(items), "forced": force == "1"})
        # The canary: state the priced count in every import result, so a
        # partial pricing failure can never pass silently again.
        warn = ""
        if priced < len(items):
            warn = f" ⚠️ {len(items) - priced} product(s) came in WITHOUT a price — check the source file."
        return {
            "message": f"Imported '{file.filename}' — {len(items)} products, "
                       f"{priced} priced, {images_exact} with a confirmed image. "
                       f"Master table total: {total} items.{warn}",
            "products_imported": len(items),
            "priced_products": priced,
            "images_found": images_exact,
            "unmatched_columns": unmatched,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise server_error(e, "Import")


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
        raise server_error(e, "Scan")


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
        rebuild_master_fts(conn)   # keep the search index in step
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
        raise server_error(e, "Import")


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


_CATEGORY_EXPR = "COALESCE(NULLIF(TRIM(category), ''), 'Uncategorised')"


@router.get("/api/master-table/summary")
def master_table_summary(by: str = "file", user: dict = Depends(get_current_user)):
    """Group names + product counts only — what the folder list needs to
    draw itself without shipping every product row (Phase 2: the master page
    must survive lakh-scale catalogues). by=file groups per catalogue,
    by=category per the CATEGORY column. Both shapes use the file_name key
    so the folder renderer stays one code path."""
    conn = get_db()
    if by == "category":
        rows = conn.execute(
            f"SELECT {_CATEGORY_EXPR} AS file_name, COUNT(*) AS count "
            "FROM master_products GROUP BY 1 ORDER BY 1").fetchall()
    else:
        rows = conn.execute(
            "SELECT file_name, COUNT(*) AS count FROM master_products "
            "GROUP BY file_name ORDER BY file_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/api/master-table/page")
def master_table_page(file: str = "", q: str = "", category: str = "",
                      limit: int = 200, offset: int = 0,
                      user: dict = Depends(get_current_user)):
    """One page of master products — by catalogue (folder expand / Load more)
    or by search term across all catalogues. Same read rules as the full
    listing above: any authenticated user, cost fields stripped for
    non-admins server-side."""
    is_admin = (user or {}).get("role") == "admin"
    limit = max(1, min(int(limit or 200), 500))
    offset = max(0, int(offset or 0))
    where, params = [], []
    if file:
        where.append("file_name = ?"); params.append(file)
    if category:
        where.append(f"{_CATEGORY_EXPR} = ?"); params.append(category)
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(product LIKE ? OR brand LIKE ? OR original_model LIKE ?)")
        params += [like, like, like]
    w = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) FROM master_products {w}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM master_products {w} ORDER BY file_name, sl_no LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    conn.close()
    COST_FIELDS = ("cost", "cost_currency", "mrp")
    items = []
    for r in rows:
        item = dict(r)
        item["has_image"] = bool(_image_file_path(item.get("image_path", "")))
        if not is_admin:
            for f in COST_FIELDS:
                item.pop(f, None)
        items.append(item)
    return {"items": items, "total": total, "offset": offset}


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
    percentage off each product's ORIGINAL price (pre-bulk snapshot, so
    repeat applies don't compound) — admin-only. Products with no price
    recorded are left untouched rather than zeroed out, and counted
    separately so the caller knows some rows didn't get updated. If usd_rate is given
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
    elif not (0 <= req.pct_3star <= 100 and 0 <= req.pct_4star <= 100):
        raise HTTPException(400, "Percentages must be between 0 and 100 — a discount over 100% would make prices negative.")
    if req.usd_rate < 0:
        raise HTTPException(400, "USD rate can't be negative")

    conn = get_db()
    rows = conn.execute(
        "SELECT id, cost, price_3star, price_4star, orig_price_3star, orig_price_4star "
        "FROM master_products WHERE file_name=?",
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

        # Discount off each product's PRICE, not its cost. Use the pre-bulk
        # snapshot as the base when one exists so re-applying a percentage
        # never compounds on an already-discounted number.
        base3 = r["orig_price_3star"] if r["orig_price_3star"] is not None else (r["price_3star"] or 0)
        base4 = r["orig_price_4star"] if r["orig_price_4star"] is not None else (r["price_4star"] or 0)
        if base3 <= 0 and base4 <= 0:
            skipped += 1
            continue
        p3 = round(base3 * (1 - req.pct_3star / 100), 2)
        p4 = round(base4 * (1 - req.pct_4star / 100), 2)
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
        msg = f"Updated {updated} product(s) — 3★ = price -{req.pct_3star}%, 4★ = price -{req.pct_4star}%"
        msg += f", USD @ ₹{req.usd_rate}/$." if req.usd_rate > 0 else "."
        if skipped:
            msg += f" Skipped {skipped} product(s) with no price recorded (left unchanged)."
    return {"message": msg, "updated": updated, "skipped": skipped}


@router.get("/api/master-table/dedupe-report")
def dedupe_report(admin: dict = Depends(require_role("admin"))):
    """The data-hygiene report behind SEARCH_FINDINGS: junk rows, catalogue
    pairs sharing model codes (double imports), duplicate product names.
    Read-only — deletions are separate, explicit admin actions."""
    conn = get_db()
    try:
        junk = [dict(r) for r in conn.execute(
            "SELECT id, product, file_name, price_3star FROM master_products "
            "WHERE LENGTH(TRIM(product)) < 6 "
            "OR product NOT GLOB '*[A-Za-z][A-Za-z][A-Za-z]*' LIMIT 100")]
        # One linear pass instead of a self-join — the join version took
        # minutes on a lakh-scale table and timed out the browser request.
        from collections import defaultdict, Counter
        files_by_model = defaultdict(set)
        for m, f in conn.execute(
                "SELECT LOWER(original_model), file_name FROM master_products "
                "WHERE LENGTH(original_model) >= 5"):
            files_by_model[m].add(f)
        pair_counts = Counter()
        for fs in files_by_model.values():
            if 1 < len(fs) <= 8:   # a model in 9+ files is generic, not a dup import
                fl = sorted(fs)
                for i in range(len(fl)):
                    for j in range(i + 1, len(fl)):
                        pair_counts[(fl[i], fl[j])] += 1
        pairs = [{"file_a": a, "file_b": b, "shared_models": n}
                 for (a, b), n in pair_counts.most_common(20) if n >= 5]
        counts = {r["file_name"]: r["n"] for r in conn.execute(
            "SELECT file_name, COUNT(*) n FROM master_products GROUP BY file_name")}
        for p in pairs:
            p["rows_a"] = counts.get(p["file_a"], 0)
            p["rows_b"] = counts.get(p["file_b"], 0)
        dup_names = [dict(r) for r in conn.execute("""
            SELECT LOWER(TRIM(product)) AS name, COUNT(*) AS n,
                   COUNT(DISTINCT file_name) AS files
            FROM master_products GROUP BY LOWER(TRIM(product))
            HAVING n > 1 ORDER BY n DESC LIMIT 30""")]
        dup_total = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM master_products "
            "GROUP BY LOWER(TRIM(product)) HAVING COUNT(*) > 1)").fetchone()[0]
    finally:
        conn.close()
    return {"junk": junk, "overlapping_imports": pairs,
            "dup_names": dup_names, "dup_name_groups_total": dup_total}


@router.get("/api/master-table/dedupe-pair-preview")
def dedupe_pair_preview(file_a: str, file_b: str,
                        admin: dict = Depends(require_role("admin"))):
    """Side-by-side sample of the models two catalogues share — the evidence
    an admin needs before deleting one of a suspected double import."""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT a.original_model AS model,
                   a.product AS product_a, a.price_3star AS price_a,
                   b.product AS product_b, b.price_3star AS price_b
            FROM master_products a
            JOIN master_products b
              ON LOWER(a.original_model) = LOWER(b.original_model)
            WHERE a.file_name = ? AND b.file_name = ?
              AND LENGTH(a.original_model) >= 5
            LIMIT 30""", (file_a, file_b)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


class DeleteRowsRequest(BaseModel):
    ids: list


@router.post("/api/master-table/delete-rows")
def delete_master_rows(req: DeleteRowsRequest, admin: dict = Depends(require_role("admin"))):
    """Delete specific product rows by id (the dedupe screen's junk cleanup).
    Hard-capped and audit-logged; never touches files on disk."""
    ids = [int(i) for i in (req.ids or [])][:200]
    if not ids:
        raise HTTPException(400, "No rows given.")
    conn = get_db()
    try:
        ph = ",".join("?" * len(ids))
        sample = [r[0] for r in conn.execute(
            f"SELECT product FROM master_products WHERE id IN ({ph}) LIMIT 5", ids)]
        cur = conn.execute(f"DELETE FROM master_products WHERE id IN ({ph})", ids)
        conn.commit()
        rebuild_master_fts(conn)
        n = cur.rowcount
    finally:
        conn.close()
    log_action(admin, "delete_master_rows", target=f"{n} row(s)",
               after={"sample": sample})
    return {"message": f"Deleted {n} row(s).", "deleted": n}


class UpdateRowsRequest(BaseModel):
    ids: list
    file_name: str = ""
    category: str = ""


@router.post("/api/master-table/update-rows")
def update_master_rows(req: UpdateRowsRequest, admin: dict = Depends(require_role("admin"))):
    """Move rows to another batch (file_name) and/or set their category —
    the master-table selection bar. DB-only: the original Excel files on
    disk are never touched, so a re-upload restores the old grouping."""
    ids = [int(i) for i in (req.ids or [])][:200]
    if not ids:
        raise HTTPException(400, "No rows given.")
    sets, vals = [], []
    if req.file_name.strip():
        sets.append("file_name=?"); vals.append(req.file_name.strip())
    if req.category.strip():
        sets.append("category=?"); vals.append(req.category.strip())
    if not sets:
        raise HTTPException(400, "Nothing to change — give a batch or a category.")
    conn = get_db()
    try:
        ph = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE master_products SET {', '.join(sets)} WHERE id IN ({ph})",
            vals + ids)
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    log_action(admin, "update_master_rows", target=f"{n} row(s)",
               after={"file_name": req.file_name or None, "category": req.category or None})
    return {"message": f"Updated {n} row(s).", "updated": n}


class ClearCategoryRequest(BaseModel):
    category: str


@router.post("/api/master-table/clear-category")
def clear_category(req: ClearCategoryRequest, admin: dict = Depends(require_role("admin"))):
    """Dissolve a category: its products keep existing, just move back to
    Uncategorised. Deleting actual products stays in batch view / selection."""
    cat = req.category.strip()
    if not cat:
        raise HTTPException(400, "No category given.")
    conn = get_db()
    try:
        if cat == "Uncategorised":
            raise HTTPException(400, "Uncategorised isn't a real category — nothing to clear.")
        cur = conn.execute(
            f"UPDATE master_products SET category='' WHERE {_CATEGORY_EXPR} = ?", (cat,))
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    if not n:
        raise HTTPException(404, "No products found in that category.")
    log_action(admin, "clear_category", target=cat, after={"rows": n})
    return {"message": f"Removed category '{cat}' — {n} product(s) moved to Uncategorised.", "cleared": n}


@router.get("/api/master-table/download-file/{filename:path}")
def download_master_file(filename: str, admin: dict = Depends(require_role("admin"))):
    """Download the original uploaded catalogue file. Admin-only — source
    files carry purchase costs. Catalogues created from a BOQ (add-missing)
    have no source file to download."""
    from fastapi.responses import FileResponse
    dest = (MASTER_UPLOADS_DIR / filename).resolve()
    if MASTER_UPLOADS_DIR.resolve() not in dest.parents:
        raise HTTPException(400, "Invalid filename.")
    if not dest.is_file():
        raise HTTPException(404, "No source file is stored for this catalogue.")
    log_action(admin, "download_master_file", target=filename)
    return FileResponse(str(dest), filename=filename)


@router.post("/api/detect-file")
async def detect_file(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """What kind of workbook is this? Powers the dashboard's Smart Import
    card: the answer decides which flow the file is routed into (master
    import, BOQ coverage, generate-from-BOQ). Read-only — nothing is stored."""
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")
    suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".xls"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_path)
    try:
        _save_upload_validated(file, tmp_path)
        return detect_file_type(str(tmp_path))
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


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
        # Phase F: the reverse of the master-import check — warn when what was
        # uploaded as a client BOQ looks like a price list or a quotation.
        try:
            file_type = detect_file_type(str(tmp_path))
        except Exception:
            file_type = None
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass   # Windows may hold the handle briefly — non-fatal

    src = [r for r in rows if (r.get("product") or "").strip()]
    if not src:
        # Self-service instead of a dead end: show the admin the headers we
        # saw so they can TEACH the right mapping and re-check — same
        # column_mappings table the master import learns from.
        from app.master_table import suggest_field, FIELD_LABELS
        headers = []
        try:
            import pandas as _pd
            # The tmp copy is already deleted — re-read from the upload
            # stream itself; rewind first, name the engine explicitly since
            # a file object carries no extension for pandas to sniff.
            file.file.seek(0)
            df = _pd.read_excel(
                file.file, header=None, nrows=12,
                engine="xlrd" if file.filename.lower().endswith(".xls") else "openpyxl")
            best = max(range(len(df)), default=None,
                       key=lambda i: sum(1 for v in df.iloc[i]
                                         if isinstance(v, str) and v.strip()))
            if best is not None:
                for v in df.iloc[best]:
                    if isinstance(v, str) and v.strip():
                        sug, conf, why = suggest_field(v)
                        headers.append({"header": v.strip(), "suggested": sug,
                                        "label": FIELD_LABELS.get(sug) if sug else None})
        except Exception:
            pass
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={
            "detail": "No product rows could be read from this file.",
            "teachable": True, "headers": headers})

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
                # Content-hash of the row's embedded image — the parser already
                # persisted the bytes under data/images/, so the hash alone is
                # enough for the UI to render it and for add-from-boq to keep it.
                "image_path": (row.get("image_path") or "").strip(),
            })

    total = len(src)
    # Recorded so the dashboard can show the latest coverage figure — and so
    # there is a history of what was checked, by whom, with what result.
    log_action(user, "check_boq_coverage", target=file.filename,
               after={"total": total, "found": len(found), "missing": len(missing)})
    return {
        "filename": file.filename,
        "file_type": file_type,
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
                VALUES (?,?,?,?,?,?,0,0,0,0,?,0,?,0,0,'INR','',?,'',?,?,?)
            """, (req.file_name, "", product, model, (it.get("brand") or "").strip(),
                  (it.get("specification") or "").strip(), (it.get("hsn_code") or "").strip(),
                  (it.get("brand") or "").strip(), (it.get("unit") or "").strip(),
                  (it.get("image_path") or "").strip(),
                  "exact" if (it.get("image_path") or "").strip() else "",
                  datetime.now().isoformat()))
            added += 1
        conn.commit()
        rebuild_master_fts(conn)   # keep the search index in step
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
    rebuild_master_fts(conn)   # keep the search index in step
    conn.close()
    dest = MASTER_UPLOADS_DIR / filename
    if dest.exists():
        dest.unlink()
    log_action(admin, "delete_master_table_file", target=filename)
    return {"message": f"'{filename}' removed from master table."}
