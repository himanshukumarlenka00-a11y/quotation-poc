"""
Back up quotations.db (and the images directory) to data/backups/, keeping
the last N daily backups. Intended to be run on a schedule (cron / Windows
Task Scheduler) once deployed — see Phase 5.
"""
import shutil, sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent.parent
DB_PATH = BASE / "data" / "quotations.db"
BACKUP_DIR = BASE / "data" / "backups"
KEEP_LAST = 14


def main():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"quotations_{stamp}.db"

    # Use SQLite's own backup API rather than a raw file copy — safe to run
    # while the app is live (WAL mode), avoids copying a half-written file.
    src_conn = sqlite3.connect(str(DB_PATH))
    dst_conn = sqlite3.connect(str(dest))
    with dst_conn:
        src_conn.backup(dst_conn)
    src_conn.close()
    dst_conn.close()
    print(f"Backed up to {dest}")

    # Prune old backups beyond KEEP_LAST
    backups = sorted(BACKUP_DIR.glob("quotations_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[KEEP_LAST:]:
        old.unlink()
        print(f"Pruned old backup {old.name}")


if __name__ == "__main__":
    main()
