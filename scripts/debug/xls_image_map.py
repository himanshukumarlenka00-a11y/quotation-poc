"""
Extract images from a BIFF8 .xls file AND map each image to its anchor row.

Approach:
- Workbook globals stream contains MSODRAWINGGROUP (0x00EB) with the BLIP store
  (all image binaries, implicitly indexed 1..N).
- Each sheet substream contains MSODRAWING (0x00EC) Escher records with
  SpContainer shapes. Each picture shape has:
    * OPT record property 0x0104 (pib) = BLIP index
    * ClientAnchor giving from-row
- We walk records, collect BLIPs in order, and per-sheet collect (row, blip_idx).
"""
import olefile, struct, os, sys

# ---- low level record reader over a byte stream ----
def read_records(data):
    """Yield (record_type, record_data) for BIFF records."""
    i, n = 0, len(data)
    while i + 4 <= n:
        rectype, reclen = struct.unpack_from("<HH", data, i)
        i += 4
        if i + reclen > n:
            break
        yield rectype, data[i:i+reclen], i
        i += reclen

def get_workbook_stream(path):
    ole = olefile.OleFileIO(path)
    name = "Workbook" if ole.exists("Workbook") else "Book"
    data = ole.openstream(name).read()
    ole.close()
    return data

# ---- Escher parsing ----
def parse_escher(blob, blips, shapes):
    """Recursively parse Escher container; collect BLIPs and shapes."""
    i, n = 0, len(blob)
    while i + 8 <= n:
        ver_inst, fbt, length = struct.unpack_from("<HHI", blob, i)
        ver = ver_inst & 0x000F
        inst = (ver_inst & 0xFFF0) >> 4
        hdr_end = i + 8
        body_end = hdr_end + length
        if body_end > n:
            break
        if ver == 0x0F:
            # container -> recurse
            parse_escher(blob[hdr_end:body_end], blips, shapes)
        else:
            if fbt == 0xF007:  # FBSE (BLIP store entry) - we count these for indexing
                pass
            if 0xF018 <= fbt <= 0xF117:  # BLIP records (image data)
                blips.append((fbt, blob[hdr_end:body_end]))
        i = body_end

def extract_blip_images(msodrawinggroup):
    """From MSODRAWINGGROUP escher blob, pull image binaries in order."""
    blips = []
    parse_escher(msodrawinggroup, blips, [])
    images = []
    for fbt, body in blips:
        # Each BLIP: skip the rgbUid (16 bytes) + possible tag; find image magic
        # JPEG
        j = body.find(b'\xff\xd8\xff')
        if j != -1:
            end = body.find(b'\xff\xd9', j)
            if end != -1:
                images.append(("jpg", body[j:end+2]))
                continue
        # PNG
        p = body.find(b'\x89PNG\r\n\x1a\n')
        if p != -1:
            end = body.find(b'IEND', p)
            if end != -1:
                images.append(("png", body[p:end+8]))
                continue
    return images

def main(path):
    data = get_workbook_stream(path)

    # 1) Collect MSODRAWINGGROUP (globals) for the BLIP store
    drawing_group = b""
    # Also handle Continue records (0x003C) that follow big records
    records = list(read_records(data))
    for idx, (rt, rd, off) in enumerate(records):
        if rt == 0x00EB:  # MSODRAWINGGROUP
            blob = rd
            # append following Continue records
            k = idx + 1
            while k < len(records) and records[k][0] == 0x003C:
                blob += records[k][1]
                k += 1
            drawing_group += blob

    images = extract_blip_images(drawing_group)
    print(f"BLIP store images: {len(images)}")
    return images

if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "uploads/QUOTATION FOR 5SU REQUIREMENT.xls"
    imgs = main(p)
    out = "data/blip_test"
    os.makedirs(out, exist_ok=True)
    for i, (ext, blob) in enumerate(imgs[:10]):
        with open(f"{out}/blip_{i}.{ext}", "wb") as f:
            f.write(blob)
    print(f"Saved first 10 to {out}")
