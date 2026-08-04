import os, json, secrets
from datetime import datetime
import bcrypt
from fastapi import HTTPException, Depends, Request
from app.db import get_db


def _hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(pw: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), pw_hash.encode("utf-8"))
    except Exception:
        return False


def _bootstrap_admin():
    """Create the first Admin account on startup if no users exist yet — an
    internal tool has no public signup, so someone has to be able to log in
    to create everyone else."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count == 0:
        email = os.environ.get("ADMIN_EMAIL", "admin@local")
        password = os.environ.get("ADMIN_PASSWORD", "")
        generated = not password
        if generated:
            password = secrets.token_urlsafe(12)
        conn.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            ("Admin", email, _hash_password(password), "admin", datetime.now().isoformat())
        )
        conn.commit()
        if generated:
            print(f"\n{'='*60}\nNo users existed — created initial admin account:\n"
                  f"  email:    {email}\n  password: {password}\n"
                  f"Log in and change this, or set ADMIN_EMAIL/ADMIN_PASSWORD "
                  f"in .env to control it directly.\n{'='*60}\n")
    conn.close()  

_bootstrap_admin()


def get_current_user(request: Request) -> dict:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "Not logged in")
    conn = get_db()
    row = conn.execute("SELECT id, name, email, role, is_active FROM users WHERE id=?",
                       (user_id,)).fetchone()
    conn.close()
    if not row:
        request.session.clear()
        raise HTTPException(401, "Session invalid")
    # Checked here, on EVERY request, not just at login — deactivating someone
    # must end their access now, not whenever their session cookie expires.
    if not row["is_active"]:
        request.session.clear()
        raise HTTPException(401, "This account has been deactivated")
    u = dict(row)
    u.pop("is_active", None)
    return u


def require_role(*roles):
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, f"Requires role: {' or '.join(roles)}")
        return user
    return _check


def _check_quote_access(quote_row, user: dict):
    """Admin can access any quotation; Employees only their own."""
    if user["role"] == "admin":
        return
    if quote_row["created_by"] != user["id"]:
        raise HTTPException(403, "You can only access your own quotations")


def log_action(user: dict, action: str, target: str = "", before=None, after=None):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (user_id, action, target, before_json, after_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (user["id"], action, target,
         json.dumps(before) if before is not None else None,
         json.dumps(after) if after is not None else None,
         datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
