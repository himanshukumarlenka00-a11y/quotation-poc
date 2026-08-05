import os, json
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, HTTPException, Response, Depends
import pandas as pd
from app.config import UPLOADS_DIR
from app.db import get_db
from app.auth import get_current_user, require_role, log_action
from app.images import _image_file_path
from app.parser import parse_boq_excel

router = APIRouter()


@router.post("/api/scan-boq")
async def scan_boq(file: UploadFile = File(...), admin: dict = Depends(require_role("admin"))):
    """Preview file contents without saving to DB."""
    import traceback, tempfile
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")
    try:
        suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".xls"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
        _save_upload_validated(file, Path(tmp_path))
        items, structure = parse_boq_excel(tmp_path, file.filename)
        try:
            os.unlink(tmp_path)
        except OSError:
            # Best-effort cleanup — a lingering file lock (Windows, antivirus,
            # etc.) shouldn't fail a scan that already succeeded. The temp
            # file just sits in the OS temp dir until it's cleaned up later.
            pass
        return {
            "filename": file.filename,
            "columns_detected": list(structure.get("col_map", {}).keys()),
            "total_products": len(items),
            "images_found": sum(1 for it in items if it.get("image_path")),
            "preview": items[:8],  # first 8 rows
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Scan error: {type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")


MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200MB — matches nginx's client_max_body_size; a real
                                       # multi-category catalog with hundreds of embedded product
                                       # photos (e.g. MARTELLATO PRICE LIST.xls at ~148MB) can
                                       # comfortably exceed the old 100MB cap


def _save_upload_validated(file: UploadFile, dest: Path):
    """Save an uploaded file with a size cap, then verify it actually parses
    as a real Excel workbook before letting it anywhere near the importer —
    the old check only looked at the filename extension."""
    size = 0
    with open(str(dest), "wb") as f:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"File too large (max {MAX_UPLOAD_BYTES // (1024*1024)}MB)")
            f.write(chunk)
    try:
        # Close explicitly (not just let it go out of scope) — on Windows an
        # unclosed handle keeps the file locked, breaking cleanup later.
        pd.ExcelFile(str(dest)).close()
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "File is not a valid/readable Excel workbook")


@router.post("/api/upload-boq")
async def upload_boq(file: UploadFile = File(...), admin: dict = Depends(require_role("admin"))):
    import traceback
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls/.xlsx files allowed")
    try:
        result = await _upload_boq(file)
        log_action(admin, "upload_catalog", target=file.filename, after={"products": result.get("images_found")})
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Upload error: {type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}")

async def _upload_boq(file: UploadFile):

    dest = UPLOADS_DIR / file.filename
    _save_upload_validated(file, dest)

    items, structure = parse_boq_excel(str(dest), file.filename)

    conn = get_db()
    # Check if already uploaded
    existing = conn.execute("SELECT COUNT(*) FROM boq_items WHERE file_name=?", (file.filename,)).fetchone()[0]
    if existing:
        conn.close()
        return {"message": f"⚠️ '{file.filename}' is already in the catalog ({existing} products). Delete it first if you want to re-upload.", "already_exists": True}
    for item in items:
        conn.execute(
            "INSERT INTO boq_items (file_name,product,description,model_no,brand,"
            "specification,hsn_code,price,price_currency,gst_pct,image_path,image_match,sheet_name,uploaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item["file_name"], item["product"], item["description"], item["model_no"],
             item["brand"], item["specification"], item["hsn_code"], item["price"],
             item.get("price_currency","INR"), item["gst_pct"], item["image_path"],
             item.get("image_match",""), item.get("sheet_name",""), item["uploaded_at"])
        )

    # Save template structure (supports xlsx only for template-based generation)
    if file.filename.endswith(".xlsx"):
        conn.execute(
            "INSERT INTO templates (file_name, file_path, structure_json, uploaded_at) VALUES (?,?,?,?)",
            (file.filename, str(dest), json.dumps(structure), datetime.now().isoformat())
        )

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM boq_items").fetchone()[0]
    exact_imgs = sum(1 for it in items if it.get("image_match") == "exact")
    guessed_imgs = sum(1 for it in items if it.get("image_match") == "guess")
    missing_imgs = sum(1 for it in items if not it.get("image_path"))
    conn.close()

    msg = f"Uploaded '{file.filename}' — extracted {len(items)} products, {exact_imgs} with a confirmed image"
    if guessed_imgs:
        msg += f", {guessed_imgs} with a best-effort matched image (please verify these)"
    if missing_imgs:
        msg += f", {missing_imgs} with no image found"
    msg += f". Total catalog: {total} items."

    return {
        "message": msg,
        "columns_detected": list(structure.get("col_map", {}).keys()),
        "images_found": exact_imgs + guessed_imgs,
        "images_exact": exact_imgs,
        "images_guessed": guessed_imgs,
        "images_missing": missing_imgs,
    }


@router.get("/api/boq-files")
def list_boq_files(user: dict = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT file_name, COUNT(*) as count, MAX(uploaded_at) as uploaded_at FROM boq_items GROUP BY file_name ORDER BY uploaded_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@router.delete("/api/boq-files/{filename:path}")
def delete_boq_file(filename: str, admin: dict = Depends(require_role("admin"))):
    conn = get_db()
    conn.execute("DELETE FROM boq_items WHERE file_name=?", (filename,))
    conn.commit()
    conn.close()
    # Also delete physical file if exists
    dest = UPLOADS_DIR / filename
    if dest.exists():
        dest.unlink()
    log_action(admin, "delete_catalog_file", target=filename)
    return {"message": f"'{filename}' removed from catalog."}

@router.get("/api/boq-items")
def list_boq_items(user: dict = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM boq_items ORDER BY uploaded_at DESC").fetchall()
    conn.close()
    result = []
    for r in rows:
        item = dict(r)
        item["has_image"] = bool(_image_file_path(item.get("image_path", "")))
        result.append(item)
    return result


@router.get("/api/product-image/{item_id}")
def product_image(item_id: int, full: bool = False, user: dict = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute("SELECT image_path FROM boq_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not row or not row["image_path"]:
        raise HTTPException(404, "No image")
    p = _image_file_path(row["image_path"], full=full)
    if not p:
        raise HTTPException(404, "Image file not found")
    return Response(content=p.read_bytes(), media_type="image/jpeg")


@router.get("/api/image/{image_hash}")
def image_by_hash(image_hash: str, full: bool = False, user: dict = Depends(get_current_user)):
    """Serve an image directly by its content hash — used wherever the
    caller already has image_path from a JSON response (e.g. the quote
    builder) and doesn't need a DB round-trip."""
    p = _image_file_path(image_hash, full=full)
    if not p:
        raise HTTPException(404, "Image file not found")
    return Response(content=p.read_bytes(), media_type="image/jpeg")
