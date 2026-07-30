"""One-time move of the image store from a flat layout to a sharded one.

    before   data/images/<hash>.jpg             data/images/thumb/<hash>.jpg
    after    data/images/<xx>/<hash>.jpg        data/images/thumb/<xx>/<hash>.jpg

where <xx> is the first two hex characters of the hash — 256 buckets, so at
the planned 300,000-product scale each folder holds ~700 files instead of one
folder holding ~350,000, which is where directory listing, backups and
antivirus scans start to hurt.

Safe to run at any time, including more than once:
- the read path (app/images.py::_image_file_path) checks the sharded location
  first and falls back to the flat one, so a partial move breaks nothing
- files are moved with os.replace (atomic on the same volume)
- a non-hash filename (anything not 64 hex chars) is left where it is

Usage:
    python scripts/shard_images.py
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import IMAGES_DIR, IMAGES_THUMB_DIR   # noqa: E402

HASH_RE = re.compile(r"^[0-9a-f]{64}\.jpg$")


def shard(base: Path) -> tuple[int, int]:
    moved = skipped = 0
    for entry in list(base.iterdir()):
        if not entry.is_file():
            continue                       # bucket dirs, thumb/ subdir
        if not HASH_RE.match(entry.name):
            skipped += 1
            continue
        dest = base / entry.name[:2] / entry.name
        dest.parent.mkdir(exist_ok=True)
        os.replace(entry, dest)            # atomic on the same volume
        moved += 1
    return moved, skipped


def main():
    for label, base in (("full-size", IMAGES_DIR), ("thumbnails", IMAGES_THUMB_DIR)):
        moved, skipped = shard(Path(base))
        print(f"{label:11}: moved {moved:,}  (skipped {skipped} non-hash files)")

    # sanity: nothing hash-like left at either top level
    for base in (IMAGES_DIR, IMAGES_THUMB_DIR):
        left = [f for f in Path(base).iterdir() if f.is_file() and HASH_RE.match(f.name)]
        if left:
            print(f"WARNING: {len(left)} unmoved files remain in {base}")


if __name__ == "__main__":
    main()
