"""Fill a client's requirement workbook with our pricing — via Excel itself.

Same job as build_client_quotation.py, but Excel does the writing instead of
openpyxl. The reason is picture formats: a client workbook typically carries
EMF/WMF images (the OPM requirement file has 229 of them), and openpyxl
silently drops those on save — the rows come out blank where the client had a
photo. Excel understands its own formats, so opening and re-saving through COM
preserves every image, merged cell and style byte-for-byte.

openpyxl is still used, but read-only, purely to pull the text out for
matching. All writing goes through Excel.

Requires Excel installed (pywin32 is already a project dependency). If this
ever has to run on a server without Excel, fall back to
build_client_quotation.py and accept the EMF loss, or do ZIP-level surgery.

Usage:
    python scripts/build_client_quotation_excel.py "<req.xlsx>" "<out.xlsx>"
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.build_client_quotation import (      # noqa: E402
    OUR_COLS, collect_rows, match_rows,
)
from app.images import _image_file_path           # noqa: E402

import openpyxl                                   # noqa: E402

XL_LEFT, XL_CENTER = -4131, -4108
MSO_FALSE, MSO_TRUE = 0, -1


def build(src, dest):
    src, dest = os.path.abspath(src), os.path.abspath(dest)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)

    # Read + match with openpyxl (text only — dropped images don't matter here)
    wb = openpyxl.load_workbook(src, data_only=True)
    rows = collect_rows(wb)
    print(f"requirement rows : {len(rows)}")
    rows, _ = match_rows(rows)
    wb.close()

    # Group by sheet title so Excel touches each sheet once
    by_sheet = {}
    for r in rows:
        by_sheet.setdefault(r["ws"].title, []).append(r)

    # Work on a copy so the client's original is never touched
    shutil.copy2(src, dest)

    import win32com.client as win32
    app = win32.DispatchEx("Excel.Application")
    app.Visible = False
    app.DisplayAlerts = False
    app.ScreenUpdating = False
    filled = missing = 0
    try:
        book = app.Workbooks.Open(dest)
        try:
            for title, sheet_rows in by_sheet.items():
                ws = book.Worksheets(title)
                hdr = sheet_rows[0]["hdr"]
                start = ws.UsedRange.Column + ws.UsedRange.Columns.Count + 1

                for i, name in enumerate(OUR_COLS):
                    cell = ws.Cells(hdr, start + i)
                    cell.Value = name
                    cell.Font.Bold = True
                    cell.Font.Size = 9
                    cell.Font.Color = 0xFFFFFF          # BGR
                    cell.Interior.Color = 0x6B3A1A      # BGR of #1A3A6B
                    cell.HorizontalAlignment = XL_CENTER
                    ws.Columns(start + i).ColumnWidth = (
                        30 if name in ("DESCRIPTION", "SPECIFICATION") else 14)

                for row in sheet_rows:
                    r, m = row["row"], row["match"]
                    if not m:
                        missing += 1
                        c = ws.Cells(r, start)
                        c.Value = "NOT IN MASTER TABLE"
                        c.Font.Italic = True
                        c.Font.Color = 0x3C29C0
                        continue

                    filled += 1
                    price = float(m.get("price_per_pc") or 0)
                    vals = [m.get("product", ""), m.get("model_no", ""),
                            m.get("brand", ""), "", m.get("specification", ""),
                            m.get("hsn_code", ""), price, price * (row["qty"] or 0),
                            "Model No" if row["model"] else "Product + Spec"]
                    for i, v in enumerate(vals):
                        cell = ws.Cells(r, start + i)
                        cell.Value = v
                        cell.Font.Size = 9
                        if i in (6, 7):
                            cell.NumberFormat = "#,##0.00"

                    img = (m.get("image_path") or "").strip()
                    if img:
                        path = _image_file_path(img)
                        if path and os.path.exists(path):
                            try:
                                anchor = ws.Cells(r, start + 3)
                                if ws.Rows(r).RowHeight < 40:
                                    ws.Rows(r).RowHeight = 40
                                ws.Shapes.AddPicture(
                                    os.path.abspath(path), MSO_FALSE, MSO_TRUE,
                                    anchor.Left + 3, anchor.Top + 2, 52, 36)
                            except Exception:
                                pass   # a bad image must never abort the quote
            book.Save()
        finally:
            book.Close(SaveChanges=True)
    finally:
        app.ScreenUpdating = True
        app.Quit()

    total = sum(float(r["match"].get("price_per_pc") or 0) * (r["qty"] or 0)
                for r in rows if r["match"])
    print(f"priced           : {filled}")
    print(f"not in master    : {missing}")
    print(f"quotation value  : Rs {total:,.2f}")
    print(f"saved            : {dest}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    build(sys.argv[1], sys.argv[2])
