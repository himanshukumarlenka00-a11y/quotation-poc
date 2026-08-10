"""A word glued inside a longer token must still be findable.

FTS5 matches token PREFIXES, so `"dustbin"*` reaches DUSTBIN and DUSTBINS but
never ROOMDUSTBIN — that token starts with "room". On the live master this
hides 19 of 63 dustbins and 233 of 1,476 trays. Run: python test_glued_tokens.py
"""
import shutil, sqlite3, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SRC = Path(__file__).parent / "data" / "quotations.db"
GLUED = "WALTHR-IR-RD001-OVL-ROOMDUSTBIN"
PLAIN = "MELANGE PLAIN DUSTBIN 5LTR"


def seed(db):
    """Add one glued-token product and one ordinary one, both indexed."""
    conn = sqlite3.connect(db)
    for name, model in ((GLUED, "IR-RD001-OVL"), (PLAIN, "PD-5L")):
        cur = conn.execute(
            "INSERT INTO master_products (product, original_model, brand, specification, "
            "hsn_code, price_3star, cost, gst_pct, file_name) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, model, "WALTHR", "stainless steel bin", "73239390", 950.0, 700.0, 18.0, "T.xls"))
        conn.execute("INSERT INTO master_fts (rowid, product, original_model, specification, brand)"
                     " VALUES (?,?,?,?,?)", (cur.lastrowid, name, model, "stainless steel bin", "WALTHR"))
    conn.commit()
    conn.close()


def main():
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    shutil.copy(SRC, tmp)
    seed(tmp)

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row

    # 1. the premise: prefix FTS genuinely cannot see the glued row
    fts = [r["product"] for r in conn.execute(
        "SELECT m.product FROM master_fts f JOIN master_products m ON m.id=f.rowid "
        "WHERE master_fts MATCH ?", ('"dustbin"*',))]
    assert PLAIN in fts, "sanity: the ordinary row must be FTS-findable"
    assert GLUED not in fts, "premise broken: FTS already finds the glued row"

    # 2. the fix: the resolver's pool reaches it anyway
    from app.routers.quotations import _resolve_master_matches, _glued_rows
    names = [r["product"] for r in _glued_rows(conn, ["dustbin"])]
    assert GLUED in names, f"_glued_rows missed it: {names[:5]}"

    matched, _ = _resolve_master_matches(
        conn, [{"product": "dustbin", "model_no": "", "qty": 1}], [], ["3star"], None,
        prompt="", variant_cap=200)
    variants = [v["product"] for v in (matched[0].get("_variants") or [])]
    assert GLUED in variants, f"glued row absent from {len(variants)} variants"
    assert PLAIN in variants, "the ordinary row must still be there"

    # 3. short/numeric words must NOT trigger a substring sweep — "ss" as a
    #    substring matches half the catalogue and would drown the pool
    assert _glued_rows(conn, ["ss"]) == [], "2-letter word should be skipped"
    assert _glued_rows(conn, ["18"]) == [], "numeric word should be skipped"

    # 4. the catalogue filter still applies
    assert _glued_rows(conn, ["dustbin"], ["nope.xls"]) == [], "catalogue filter ignored"
    assert any(r["product"] == GLUED for r in _glued_rows(conn, ["dustbin"], ["T.xls"]))

    conn.close()
    shutil.rmtree(tmp.parent, ignore_errors=True)
    print(f"ok — glued token reachable, {len(variants)} variants for 'dustbin'")


if __name__ == "__main__":
    main()
