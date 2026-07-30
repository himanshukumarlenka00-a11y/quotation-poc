import io, hashlib
import openpyxl
from app.config import IMAGES_DIR, IMAGES_THUMB_DIR


def _shard_path(base_dir, image_hash: str):
    """Sharded location for a hash: <base>/<first two hex chars>/<hash>.jpg.

    A flat directory holds every image in one folder — fine at 10k files,
    but the planned 300,000-product catalogue implies ~350k files, where
    directory listing, backups and antivirus scans all degrade. Two hex
    characters give 256 buckets, ~700 files each at full scale."""
    return base_dir / image_hash[:2] / f"{image_hash}.jpg"


def _save_image_to_disk(img_bytes: bytes) -> str:
    """Save a full-size + thumbnail JPEG to disk under a content-hash filename —
    identical images (common across supplier sheets) are auto-deduped since they
    hash to the same file. Returns the hash to store in image_path, or '' on
    failure/empty input."""
    if not img_bytes:
        return ""
    h = hashlib.sha256(img_bytes).hexdigest()
    full_path = _shard_path(IMAGES_DIR, h)
    thumb_path = _shard_path(IMAGES_THUMB_DIR, h)
    if full_path.exists() and thumb_path.exists():
        return h
    try:
        from PIL import Image as _PILImage
        im = _PILImage.open(io.BytesIO(img_bytes))
        if im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        full_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        full_im = im.copy()
        full_im.thumbnail((800, 800))
        full_im.save(str(full_path), format="JPEG", quality=85)
        thumb_im = im.copy()
        thumb_im.thumbnail((200, 200))
        thumb_im.save(str(thumb_path), format="JPEG", quality=72)
        return h
    except Exception:
        return ""


def _image_file_path(image_hash: str, full: bool = False):
    """Resolve an image_path hash to its file on disk. Returns a Path if the
    file exists, else None. full=True for the export/lightbox-quality image,
    False (default) for the small thumbnail.

    Checks the sharded location first, then the legacy flat one — so the
    layout migration can never break an image: a file that hasn't been moved
    yet still resolves."""
    if not image_hash:
        return None
    base = IMAGES_DIR if full else IMAGES_THUMB_DIR
    p = _shard_path(base, image_hash)
    if p.exists():
        return p
    legacy = base / f"{image_hash}.jpg"
    return legacy if legacy.exists() else None


# ── Image Extraction ──────────────────────────────────────────────────────────


def _xlsx_sheet_images(filepath: str) -> dict:
    """Extract images from ALL sheets of an .xlsx as
    {sheet_index: [(anchor_row, anchor_col, data_url), ...]} (0-indexed)."""
    out = {}
    try:
        wb = openpyxl.load_workbook(filepath)
    except Exception:
        return out
    try:
        for sidx, ws in enumerate(wb.worksheets):
            for img in getattr(ws, "_images", []):
                try:
                    anchor = img.anchor
                    if hasattr(anchor, "_from"):
                        row0 = anchor._from.row; col0 = anchor._from.col
                    elif hasattr(anchor, "row"):
                        row0 = anchor.row - 1; col0 = getattr(anchor, "col", 0)
                    else:
                        continue
                    data = img._data() if callable(img._data) else img._data
                    h = _save_image_to_disk(data)
                    if h:
                        out.setdefault(sidx, []).append((row0, col0, h))
                except Exception:
                    pass
    finally:
        wb.close()  # release the file handle — Windows can't delete an open file
    return out


def _assign_images_by_span(df, header_row, pcol, sheet_imgs, our_col, ref_col, fallback_blips=None):
    """Assign each product its image by ROW-RANGE (span): the image whose anchor
    row falls within [product_row, next_product_row) — preferring the OUR IMAGE
    column. This fixes the drift/cross-assignment of the old nearest-row method.
    sheet_imgs: list of (row, col, data_url). Returns (out, guessed) where out
    is {df_row_idx: data_url} and guessed is the set of df_row_idx filled from
    fallback_blips (a best-effort sequential match, not a confirmed anchor —
    see extract_images_with_fallback) rather than a real anchor match."""
    if pcol is None:
        return {}, set()
    import pandas as _pd
    prows = [r for r in range(header_row + 1, len(df))
             if _pd.notna(df.iloc[r, pcol]) and str(df.iloc[r, pcol]).strip()]
    out = {}
    if sheet_imgs:
        for k, pr in enumerate(prows):
            nxt = prows[k + 1] if k + 1 < len(prows) else len(df) + 5
            cands = [im for im in sheet_imgs if pr <= im[0] < nxt]
            if not cands:
                continue
            def rank(im):
                r, c, u = im
                cp = 2 if (our_col is not None and c == our_col) else (1 if (ref_col is not None and c == ref_col) else 0)
                return (cp, -abs(r - pr))
            out[pr] = max(cands, key=rank)[2]

    guessed = set()
    if fallback_blips:
        pool = iter(fallback_blips)
        for pr in prows:
            if pr in out:
                continue
            item = next(pool, None)
            if item is None:
                break
            ext, data = item
            h = _save_image_to_disk(data)
            if h:
                out[pr] = h
                guessed.add(pr)
    return out, guessed
