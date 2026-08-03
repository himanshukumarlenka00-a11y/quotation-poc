import sqlite3
from app.config import DB_PATH


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS boq_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT,
            product TEXT,
            description TEXT,
            model_no TEXT,
            brand TEXT,
            specification TEXT,
            hsn_code TEXT,
            price REAL,
            price_currency TEXT DEFAULT 'INR',
            gst_pct REAL,
            image_path TEXT,
            uploaded_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_boq_product ON boq_items(product);
        CREATE INDEX IF NOT EXISTS idx_boq_model_no ON boq_items(model_no);
        CREATE INDEX IF NOT EXISTS idx_boq_file_name ON boq_items(file_name);
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT,
            file_path TEXT,
            structure_json TEXT,
            uploaded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS quotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_no TEXT,
            client_name TEXT,
            items_json TEXT,
            status TEXT DEFAULT 'draft',
            created_by INTEGER,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quotation_id INTEGER,
            rating TEXT,
            missing_items TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','employee')),
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            target TEXT,
            before_json TEXT,
            after_json TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token);
        CREATE TABLE IF NOT EXISTS master_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT,
            sl_no TEXT,
            product TEXT,
            original_model TEXT,
            brand TEXT,
            specification TEXT,
            price_3star REAL,
            price_4star REAL,
            price_3star_usd REAL,
            price_4star_usd REAL,
            hsn_code TEXT,
            gst_pct REAL,
            original_brand TEXT,
            mrp REAL,
            cost REAL,
            cost_currency TEXT DEFAULT 'INR',
            category TEXT,
            unit TEXT,
            product_group TEXT,
            image_path TEXT,
            image_match TEXT DEFAULT '',
            uploaded_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_master_product ON master_products(product);
        CREATE INDEX IF NOT EXISTS idx_master_model ON master_products(original_model);
        CREATE INDEX IF NOT EXISTS idx_master_file_name ON master_products(file_name);
    """)
    conn.commit()
    conn.close()


init_db()

# Add new columns to existing DBs that predate schema changes
def migrate_db():
    conn = get_db()
    for col, definition in [
        ("sheet_name", "TEXT DEFAULT ''"),
        ("price_currency", "TEXT DEFAULT 'INR'"),
        ("image_match", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE boq_items ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_boq_sheet_name ON boq_items(sheet_name)")
    # image_data (base64-in-DB) is retired in favor of image_path (disk-based,
    # see scripts/migrate_images_to_disk.py) — drop it from any DB that predates this.
    try:
        conn.execute("ALTER TABLE boq_items DROP COLUMN image_data")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE quotations ADD COLUMN created_by INTEGER")
        conn.commit()
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quotations_created_by ON quotations(created_by)")

    # Sales team: who a quotation is prepared by — selectable on the quote,
    # written into the exported letterhead. Seeded once from the company list;
    # rows can be edited in the DB later without touching code.
    conn.execute("""CREATE TABLE IF NOT EXISTS sales_persons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT DEFAULT '', email TEXT DEFAULT '',
        region TEXT DEFAULT '', active INTEGER DEFAULT 1)""")
    if conn.execute("SELECT COUNT(*) FROM sales_persons").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO sales_persons (name, phone, email, region) VALUES (?,?,?,?)",
            [("KASHYAP BHATT", "9898715649", "kashyap@melangeindia.in", "GUJARAT, RAJASTHAN"),
             ("AASHISH JAIN", "7795527228", "aashish@melangeindia.in", "BENGALURU"),
             ("HARISH MENON", "9606448344", "harish.menon@melangeindia.in", "MUMBAI, MP, GOA"),
             ("ABHISHEK JAIN", "9901708514", "abhishek@melangeindia.in", "SOUTH"),
             ("NIKUNJ LAKHOTIA", "9674059963", "nikunj@melangeindia.in", "KOLKATA, NORTHEAST"),
             ("JANMESH SONI", "9187191970", "janmesh@melangeindia.in", "RAJASTHAN - JAIPUR"),
             ("ANIL KUMAR", "8178243005", "anil@melangeindia.in", "DELHI, UP"),
             ("SURAJ KOUL", "8867921718", "suraaj@melangeindia.in", "COUNTRY HEAD"),
             ("SUNIL", "8861924848", "sunil@melangeindia.in", "BANGALORE"),
             ("NAGESH", "7204071304", "nagesh.k@melangeindia.in", "BENGALURU")])
        conn.commit()

    # Snapshot columns backing the "reset pricing" action. They stay NULL until
    # the first bulk edit touches a row, at which point that row's pre-edit
    # prices are copied in — so reset restores what the row held before we
    # ever changed it, rather than a baseline invented at migration time.
    for col in ("orig_price_3star", "orig_price_4star",
                "orig_price_3star_usd", "orig_price_4star_usd"):
        try:
            conn.execute(f"ALTER TABLE master_products ADD COLUMN {col} REAL")
            conn.commit()
        except Exception:
            pass

    # Learned column mappings: a spreadsheet header an admin has explained
    # once, so the next file using the same wording maps itself. The built-in
    # alias list can only match headers we anticipated — that gap is why the
    # Nilkamal sheet's "PRICES IN INR" imported 493 products with no price.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS column_mappings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            header_norm  TEXT NOT NULL UNIQUE,   -- header as _norm() produces it
            field        TEXT NOT NULL,          -- internal field it means
            confirmed_by INTEGER,
            confirmed_at TEXT,
            times_seen   INTEGER DEFAULT 1,
            source_file  TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_colmap_header ON column_mappings(header_norm)")

    # Access can be switched off without deleting the account. Deletion would
    # orphan the user's audit history and quotations; deactivation preserves
    # both while ending access — checked on every request, not just at login,
    # so it takes effect immediately.
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1")
        conn.commit()
    except Exception:
        pass

    # Human corrections to product matching. When someone edits a saved
    # quotation and changes WHICH product a line resolved to, that is the
    # strongest signal the system can get: the matcher was wrong and a person
    # fixed it. Stored by phrase so the same request never repeats the mistake.
    # Product identity is text (product + original_model), NOT a row id —
    # catalogue re-imports delete and reinsert rows, so ids do not survive
    # but the text identity does.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_corrections (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase_norm     TEXT NOT NULL UNIQUE,  -- normalized requested phrase
            product         TEXT NOT NULL,         -- the human's choice
            original_model  TEXT DEFAULT '',
            corrected_from  TEXT DEFAULT '',       -- what the AI had picked
            corrected_by    INTEGER,
            created_at      TEXT,
            times_confirmed INTEGER DEFAULT 1
        )
    """)

    # How a lesson arrived: 'correction' (human changed the product — strong)
    # or 'confirmed' (human saved the line untouched — weaker, but every
    # accepted BOQ line becomes one, so it compounds). One table for both:
    # they answer the same question, differing only in authority.
    try:
        conn.execute("ALTER TABLE match_corrections ADD COLUMN source TEXT DEFAULT 'correction'")
        conn.commit()
    except Exception:
        pass

    # Full-text index over the searchable product fields.
    #
    # Matching previously loaded EVERY master_products row into Python on every
    # request: 57ms and 7MB at 3,000 products, which extrapolates to ~5.7s and
    # ~0.7GB at the planned 300,000 — per request, per concurrent employee.
    # FTS5 lets SQLite return the few dozen plausible rows instead, in
    # milliseconds, with no extra service to run. That matters specifically
    # because this is destined for the company's own server.
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS master_fts USING fts5(
                product, original_model, specification, brand,
                content='master_products', content_rowid='id',
                tokenize="unicode61 remove_diacritics 2"
            )
        """)
        conn.commit()
    except Exception:
        pass   # an older SQLite without FTS5 falls back to the in-memory scan

    conn.commit()
    conn.close()


def rebuild_master_fts(conn=None):
    """Repopulate the full-text index from master_products.

    Called after an import rather than maintained by triggers: imports are
    occasional and wholesale (a catalogue is deleted and reinserted), so one
    rebuild is simpler and cannot drift out of sync the way triggers can.
    Returns the number of indexed rows, or None if FTS5 is unavailable.
    """
    own = conn is None
    conn = conn or get_db()
    try:
        conn.execute("INSERT INTO master_fts(master_fts) VALUES('rebuild')")
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM master_fts").fetchone()[0]
    except Exception:
        return None
    finally:
        if own:
            conn.close()

migrate_db()
