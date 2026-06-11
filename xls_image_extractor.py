"""
Pure-Python image extractor for legacy .xls (BIFF8) files.

Reads embedded pictures and maps each one to its anchor cell (sheet + row),
WITHOUT requiring LibreOffice. Used at upload time so every catalog upload
gets product images automatically.

Returns: list of dicts:
    {"sheet_index": int, "row": int, "col": int, "ext": "jpg"|"png", "data": bytes}
"""
import olefile, struct

# ── BIFF record types ──
BOF              = 0x0809
EOF              = 0x000A
MSODRAWINGGROUP  = 0x00EB
MSODRAWING       = 0x00EC
OBJ              = 0x005D
CONTINUE         = 0x003C

# ── Escher record types ──
ESCHER_DGG_CONTAINER   = 0xF000
ESCHER_BSTORE_CONTAINER= 0xF001
ESCHER_DG_CONTAINER    = 0xF002
ESCHER_SPGR_CONTAINER  = 0xF003
ESCHER_SP_CONTAINER    = 0xF004
ESCHER_BSE             = 0xF007
ESCHER_OPT             = 0xF00B
ESCHER_CLIENT_ANCHOR   = 0xF010
# BLIP types
BLIP_TYPES = {
    0xF01A: "emf", 0xF01B: "wmf", 0xF01C: "pict",
    0xF01D: "jpg", 0xF01E: "png", 0xF01F: "dib",
    0xF029: "tiff", 0xF02A: "jpg",
}

OPT_BLIP_ID = 0x0104   # property id for "pib" = BLIP index (1-based)


def _biff_records(data):
    """Yield (rectype, recdata) and merge CONTINUE records into the prior record."""
    i, n = 0, len(data)
    records = []
    while i + 4 <= n:
        rectype, reclen = struct.unpack("<HH", data[i:i+4])
        i += 4
        recdata = data[i:i+reclen]
        i += reclen
        if rectype == CONTINUE and records:
            # append to previous record's data
            records[-1] = (records[-1][0], records[-1][1] + recdata)
        else:
            records.append((rectype, recdata))
    return records


def _escher_walk(data, offset=0, end=None):
    """Yield (recVer, recInstance, recType, recData) for Escher records in data[offset:end]."""
    if end is None:
        end = len(data)
    i = offset
    while i + 8 <= end:
        ver_inst, rectype, reclen = struct.unpack("<HHI", data[i:i+8])
        rec_ver = ver_inst & 0x000F
        rec_inst = (ver_inst >> 4) & 0x0FFF
        body_start = i + 8
        body_end = body_start + reclen
        if body_end > end:
            body_end = end
        yield rec_ver, rec_inst, rectype, body_start, body_end
        i = body_end


def _is_container(rec_ver):
    return rec_ver == 0x0F


def _parse_blip_store(data):
    """Locate BstoreContainer, read its blip count (instance), then walk that many
    BSE records sequentially across the (contiguous) buffer — robust to BIFF record
    splitting since the buffer is already concatenated.
    Returns list (1-based by position) of (ext, bytes) or None for vector/unsupported."""
    # Find the BstoreContainer (0xF001) header anywhere in the buffer
    f001_pos = None
    n_blips = 0
    body_start = None
    i = 0
    while i + 8 <= len(data):
        ver_inst, rectype, reclen = struct.unpack("<HHI", data[i:i+8])
        if rectype == ESCHER_BSTORE_CONTAINER:
            n_blips = (ver_inst >> 4) & 0x0FFF   # instance = number of BLIPs
            body_start = i + 8
            f001_pos = i
            break
        i += 1
    if body_start is None or n_blips == 0:
        return []

    # Walk exactly n_blips BSE records, each by its own length
    blips = []
    p = body_start
    end = len(data)
    for _ in range(n_blips):
        if p + 8 > end:
            break
        bse_vi, bse_type, bse_len = struct.unpack("<HHI", data[p:p+8])
        if bse_type != ESCHER_BSE:
            # try to resync: scan forward for next BSE
            nxt = data.find(struct.pack("<H", ESCHER_BSE), p+2)
            if nxt == -1:
                break
            p = nxt - 2
            continue
        bse_body = p + 8
        bse_end = bse_body + bse_len
        blips.append(_parse_one_bse(data, bse_body, min(bse_end, end)))
        p = bse_end
    return blips


def _parse_one_bse(data, off, end):
    """BSE header is 36 bytes, then the BLIP record."""
    blip_start = off + 36
    if blip_start + 8 > end:
        return None
    ver_inst, rt, reclen = struct.unpack("<HHI", data[blip_start:blip_start+8])
    inst = (ver_inst >> 4) & 0x0FFF
    ext = BLIP_TYPES.get(rt)
    if not ext or ext in ("emf", "wmf", "pict"):
        return None  # vector — occupies an index but no raster bytes
    bs = blip_start + 8
    be = min(bs + reclen, end)
    img = _extract_blip_bytes(data, bs, be, rt, inst, ext)
    return (ext, img) if img else None


def _extract_blip_bytes(data, bs, be, rt, inst, ext):
    """Return raw image bytes by locating the real signature inside the BLIP body
    (robust against varying UID/tag header sizes)."""
    body = data[bs:be]
    if ext == "jpg":
        s = body.find(b"\xff\xd8\xff")
        if s != -1:
            e = body.rfind(b"\xff\xd9")
            return body[s:e+2] if e > s else body[s:]
    elif ext == "png":
        s = body.find(b"\x89PNG\r\n\x1a\n")
        if s != -1:
            e = body.find(b"IEND", s)
            return body[s:e+8] if e != -1 else body[s:]
    return None


def _parse_drawing_shapes(data):
    """Walk a sheet's MSODRAWING Escher data → list of (blip_index, anchor_row, anchor_col)."""
    shapes = []

    def parse_sp_container(off, end):
        blip_idx = None
        anchor_row = None
        anchor_col = None
        for ver, inst, rt, bs, be in _escher_walk(data, off, end):
            if rt == ESCHER_OPT:
                blip_idx = _read_opt_blip(data, bs, be)
            elif rt == ESCHER_CLIENT_ANCHOR:
                a = _read_client_anchor(data, bs, be)
                if a:
                    anchor_col, anchor_row = a
        if blip_idx is not None and anchor_row is not None:
            shapes.append((blip_idx, anchor_row, anchor_col))

    def recurse(off, end):
        for ver, inst, rt, bs, be in _escher_walk(data, off, end):
            if rt == ESCHER_SP_CONTAINER:
                parse_sp_container(bs, be)
            elif _is_container(ver):
                recurse(bs, be)

    recurse(0, len(data))
    return shapes


def _read_opt_blip(data, off, end):
    """OPT is an array of property records: id(2, with flags), value(4)."""
    i = off
    while i + 6 <= end:
        prop_id_full, val = struct.unpack("<HI", data[i:i+6])
        prop_id = prop_id_full & 0x3FFF
        is_complex = (prop_id_full >> 15) & 1
        i += 6
        if prop_id == OPT_BLIP_ID:
            return val  # 1-based index into BLIP store
    return None


def _read_client_anchor(data, off, end):
    """BIFF client anchor: flag(2), col1(2), dx1(2), row1(2), dy1(2), col2(2)..."""
    if end - off < 8:
        return None
    # flag(2), col1(2), dx1(2), row1(2)
    try:
        flag, col1, dx1, row1 = struct.unpack("<HHHH", data[off:off+8])
        return (col1, row1)
    except struct.error:
        return None


def extract_images(path):
    """Main entry: return list of {sheet_index, row, col, ext, data}."""
    if not olefile.isOleFile(path):
        return []
    ole = olefile.OleFileIO(path)
    try:
        wb = ole.openstream("Workbook").read()
    except Exception:
        try:
            wb = ole.openstream("Book").read()
        except Exception:
            ole.close()
            return []
    ole.close()

    records = _biff_records(wb)

    # 1. Global BLIP store — concatenate ALL MSODRAWINGGROUP payloads into one
    #    contiguous buffer, then walk the BSE list (handles BIFF record splitting).
    combined = b"".join(rd for rt, rd in records if rt == MSODRAWINGGROUP)
    blips = _parse_blip_store(combined) if combined else []

    if not blips:
        return []

    # 2. Walk sheets: track sheet index, collect MSODRAWING per sheet
    results = []
    sheet_index = -1
    for rectype, recdata in records:
        if rectype == BOF:
            # BOF dt field: 0x0010 = worksheet
            if len(recdata) >= 4:
                dt = struct.unpack("<H", recdata[2:4])[0]
                if dt == 0x0010:
                    sheet_index += 1
        elif rectype == MSODRAWING and sheet_index >= 0:
            shapes = _parse_drawing_shapes(recdata)
            for blip_idx, row, col in shapes:
                if 1 <= blip_idx <= len(blips) and blips[blip_idx - 1]:
                    ext, img = blips[blip_idx - 1]
                    if img and len(img) > 500:
                        results.append({
                            "sheet_index": sheet_index,
                            "row": row, "col": col,
                            "ext": ext, "data": img,
                        })
    return results


if __name__ == "__main__":
    import sys, os
    p = sys.argv[1] if len(sys.argv) > 1 else "uploads/iron_board.xls"
    imgs = extract_images(p)
    print(f"{os.path.basename(p)}: {len(imgs)} images mapped")
    by_sheet = {}
    for im in imgs:
        by_sheet.setdefault(im["sheet_index"], []).append(im)
    for si, lst in sorted(by_sheet.items()):
        rows = sorted(set(i["row"] for i in lst))
        print(f"  Sheet {si}: {len(lst)} images, anchor rows: {rows[:20]}")
