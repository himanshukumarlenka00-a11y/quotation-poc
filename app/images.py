import io, hashlib, zipfile, posixpath
import xml.etree.ElementTree as ET
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


_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _rels_map(zf: zipfile.ZipFile, rels_path: str) -> dict:
    """rId -> Target, from an OOXML .rels part. {} if the part doesn't exist."""
    if rels_path not in zf.namelist():
        return {}
    root = ET.fromstring(zf.read(rels_path))
    return {rel.get("Id"): rel.get("Target") for rel in root.findall(f"{{{_NS_RELS}}}Relationship")}


def _resolve_target(base_dir: str, target: str) -> str:
    """OOXML relationship targets are relative to the part's own directory
    (e.g. "../media/image1.png" from xl/drawings/), except ones starting
    with "/" which are package-root-absolute."""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def _xlsx_sheet_images_raw(filepath: str) -> dict:
    """Direct-XML fallback for when openpyxl's drawing reader finds nothing —
    happens when a sheet's drawing mixes <xdr:pic> (real pictures) with
    non-picture shapes such as decorative divider lines or textboxes.
    openpyxl's SpreadsheetDrawing reader can drop EVERY image on that sheet in
    that case (not just the unsupported shapes) — a known upstream limitation,
    confirmed against BOROSIL Price List 2021-2022 (628 pictures + 70 shapes
    in one drawing part, openpyxl returned zero images for any sheet).

    Reads the drawing XML ourselves and only ever looks at <xdr:pic> nodes,
    so shapes/lines/textboxes are simply invisible to it rather than
    poisoning the whole sheet's picture list."""
    out = {}
    try:
        zf = zipfile.ZipFile(filepath)
    except Exception:
        return out
    try:
        wb_xml = ET.fromstring(zf.read("xl/workbook.xml"))
        wb_rels = _rels_map(zf, "xl/_rels/workbook.xml.rels")
        sheets_el = wb_xml.find(f"{{{_NS_MAIN}}}sheets")
        sheet_targets = []
        if sheets_el is not None:
            for sheet_el in sheets_el:
                rid = sheet_el.get(f"{{{_NS_R}}}id")
                target = wb_rels.get(rid)
                if target:
                    sheet_targets.append(_resolve_target("xl", target))

        for sidx, ws_path in enumerate(sheet_targets):
            try:
                ws_dir, ws_name = posixpath.dirname(ws_path), posixpath.basename(ws_path)
                ws_rels = _rels_map(zf, f"{ws_dir}/_rels/{ws_name}.rels")
                drawing_target = next((_resolve_target(ws_dir, t) for t in ws_rels.values()
                                       if "drawing" in t), None)
                if not drawing_target or drawing_target not in zf.namelist():
                    continue

                drawing_dir = posixpath.dirname(drawing_target)
                drawing_name = posixpath.basename(drawing_target)
                drawing_rels = _rels_map(zf, f"{drawing_dir}/_rels/{drawing_name}.rels")
                drawing_xml = ET.fromstring(zf.read(drawing_target))

                for anchor_el in list(drawing_xml):
                    if anchor_el.tag.split("}")[-1] not in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"):
                        continue
                    pic_el = anchor_el.find(f"{{{_NS_XDR}}}pic")
                    from_el = anchor_el.find(f"{{{_NS_XDR}}}from")
                    if pic_el is None or from_el is None:
                        continue
                    col_el, row_el = from_el.find(f"{{{_NS_XDR}}}col"), from_el.find(f"{{{_NS_XDR}}}row")
                    if col_el is None or row_el is None:
                        continue
                    blip = pic_el.find(f".//{{{_NS_A}}}blip")
                    if blip is None:
                        continue
                    media_target = drawing_rels.get(blip.get(f"{{{_NS_R}}}embed"))
                    if not media_target:
                        continue
                    media_path = _resolve_target(drawing_dir, media_target)
                    if media_path not in zf.namelist():
                        continue
                    h = _save_image_to_disk(zf.read(media_path))
                    if h:
                        out.setdefault(sidx, []).append((int(row_el.text), int(col_el.text), h))
            except Exception:
                continue
    except Exception:
        pass
    finally:
        zf.close()
    return out


def _xlsx_sheet_images(filepath: str) -> dict:
    """Extract images from ALL sheets of an .xlsx as
    {sheet_index: [(anchor_row, anchor_col, data_url), ...]} (0-indexed)."""
    out = {}
    num_sheets = 0
    try:
        wb = openpyxl.load_workbook(filepath)
    except Exception:
        wb = None
    if wb is not None:
        try:
            num_sheets = len(wb.worksheets)
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

    # Only reach for the slower raw-XML path on sheets openpyxl came back
    # empty for — normal files (the overwhelming majority) never pay for it.
    if not out or any(not out.get(i) for i in range(num_sheets)):
        try:
            for sidx, imgs in _xlsx_sheet_images_raw(filepath).items():
                if not out.get(sidx):
                    out[sidx] = imgs
        except Exception:
            pass
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
