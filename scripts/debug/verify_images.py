import sqlite3, base64, os
conn = sqlite3.connect("data/quotations.db")
conn.row_factory = sqlite3.Row

# Check specific products
for name in ["IRON", "IRON BOARD", "Bath Towel", "Hair Dryer", "Electric Kettle"]:
    r = conn.execute("SELECT product, file_name, LENGTH(image_data) as imglen FROM boq_items WHERE UPPER(product) LIKE ? LIMIT 1", (f"%{name.upper()}%",)).fetchone()
    if r:
        print(f"  {r['product'][:35]:35} | img bytes: {r['imglen'] or 0}")

# Save a few images to disk to eyeball
os.makedirs("data/verify", exist_ok=True)
rows = conn.execute("SELECT product, image_data FROM boq_items WHERE image_data != '' LIMIT 5").fetchall()
for i, r in enumerate(rows):
    b64 = r["image_data"].split(",", 1)[1]
    with open(f"data/verify/{i}_{r['product'][:20].replace('/','_')}.jpg", "wb") as f:
        f.write(base64.b64decode(b64))
    print(f"  saved: {r['product'][:30]}")
conn.close()
