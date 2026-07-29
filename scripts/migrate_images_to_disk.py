"""
One-time migration: decode existing base64 image_data rows, save them to disk
under the new content-hash file layout, and backfill image_path.

Safe to re-run — rows that already have image_path are skipped. NOTE: this
migration already ran during the Phase 1 image-storage overhaul, and the
source image_data column has since been dropped from the schema — kept here
for historical reference only.
"""
import sqlite3, base64, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.images import _save_image_to_disk
from app.config import DB_PATH


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, image_data FROM boq_items "
        "WHERE image_data IS NOT NULL AND image_data != '' "
        "AND (image_path IS NULL OR image_path = '')"
    ).fetchall()

    print(f"Found {len(rows)} rows with image_data to migrate")
    migrated, failed = 0, 0
    for row in rows:
        try:
            b64 = row["image_data"].split(",", 1)[1] if "," in row["image_data"] else row["image_data"]
            raw = base64.b64decode(b64)
            h = _save_image_to_disk(raw)
            if h:
                conn.execute("UPDATE boq_items SET image_path=? WHERE id=?", (h, row["id"]))
                migrated += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  row {row['id']} failed: {e}")
            failed += 1
    conn.commit()

    total_with_path = conn.execute(
        "SELECT COUNT(*) FROM boq_items WHERE image_path IS NOT NULL AND image_path != ''"
    ).fetchone()[0]
    conn.close()

    print(f"Migrated: {migrated}, failed: {failed}")
    print(f"Total rows with image_path now: {total_with_path}")


if __name__ == "__main__":
    main()
