import openpyxl

wb = openpyxl.load_workbook("data/converted/iron_board.xlsx")
ws = wb["QUOTE"]
imgs = getattr(ws, "_images", [])
print("IMAGE ANCHORS:")
img_rows = []
for im in imgs:
    try:
        r = im.anchor._from.row + 1
        c = im.anchor._from.col + 1
        img_rows.append((r, c))
        print(f"   row={r}, col={c}")
    except Exception as e:
        print("   err", e)

print("\nROWS 13-25 (full width):")
for r in range(13, 26):
    vals = [ws.cell(row=r, column=c).value for c in range(1, 12)]
    mark = "  <-- IMG" if any(ir == r for ir, ic in img_rows) else ""
    print(f"  row {r}: {vals}{mark}")
