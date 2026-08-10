"""A plain product word must not be mistaken for a model number.

The model branch scores 1000 and search_catalog then keeps only rows above
0.6 x best. Many "models" in the master are descriptive text ("DCTC 1014
(PP) - PP Tray"), so a bare word matched one as a substring, scored 1000,
and the cutoff landed at 621 — just above the ~619 a genuine name match can
reach. On the live catalogue "tray" returned 3 rows out of 1,476 named
"...Tray...". Run: python test_word_vs_model.py
"""
import shutil, sqlite3, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SRC = Path(__file__).parent / "data" / "quotations.db"
DECOY = "KEN FORD-DCTC1014(PP)-PPTray-6 Cmpt Tray"   # its MODEL says "Tray"
REAL = ["MELANGE-SMWT17-Wire Tray", "FNS-BATR102-Tray", "CAMBRO-1014CL-Tray"]
CODED = "ACME-IR-CHS002-Chest Freezer"


def seed(db):
    conn = sqlite3.connect(db)
    rows = [(DECOY, "DCTC 1014 (PP) - PP Tray"), (CODED, "IR-CHS002")]
    rows += [(n, n.split("-")[1]) for n in REAL]
    for name, model in rows:
        cur = conn.execute(
            "INSERT INTO master_products (product, original_model, brand, specification,"
            " hsn_code, price_3star, cost, gst_pct, file_name) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, model, "T", "steel", "7323", 500.0, 400.0, 18.0, "T.xls"))
        conn.execute("INSERT INTO master_fts (rowid, product, original_model, specification, brand)"
                     " VALUES (?,?,?,?,?)", (cur.lastrowid, name, model, "steel", "T"))
    conn.commit(); conn.close()


def variants(conn, term):
    from app.routers.quotations import _resolve_master_matches
    m, _ = _resolve_master_matches(conn, [{"product": term, "model_no": "", "qty": 1}],
                                   [], ["3star"], None, prompt="", variant_cap=500)
    return [v["product"] for v in (m[0].get("_variants") or [])]


def main():
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    shutil.copy(SRC, tmp)
    seed(tmp)
    conn = sqlite3.connect(tmp); conn.row_factory = sqlite3.Row

    # 1. the bug: rows genuinely NAMED tray must survive a decoy model
    got = variants(conn, "tray")
    missing = [n for n in REAL if n not in got]
    assert not missing, f"name matches dropped: {missing} (got {len(got)}: {got[:6]})"
    assert len(got) > 3, f"expected the full tray list, got {len(got)}"

    # 2. a real model code must STILL hard-match, and still dominate
    coded = variants(conn, "IR-CHS002")
    assert coded and coded[0] == CODED, f"model lookup broke: {coded[:3]}"

    # 3. and a code typed inside a sentence still wins (mtoks path)
    sentence = variants(conn, "chest freezer IR-CHS002")
    assert sentence and sentence[0] == CODED, f"inline model code broke: {sentence[:3]}"

    conn.close()
    shutil.rmtree(tmp.parent, ignore_errors=True)
    print(f"ok — 'tray' returns {len(got)} variants incl. all {len(REAL)} real ones; "
          f"model codes still win")


if __name__ == "__main__":
    main()
