import sqlite3, secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel
from app.config import limiter, SIGNUP_ALLOWED_DOMAINS
from app.db import get_db
from app.auth import _hash_password, _verify_password, get_current_user, require_role, log_action
from app.email_utils import send_email

router = APIRouter()

RESET_TOKEN_TTL_HOURS = 1


class LoginRequest(BaseModel):
    email: str
    password: str

class CreateUserRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


@router.post("/api/auth/login")
@limiter.limit("10/minute")
def login(request: Request, req: LoginRequest):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (req.email,)).fetchone()
    conn.close()
    if not row or not _verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    # Explicit message, distinct from a wrong password — a deactivated
    # colleague should know to talk to an admin, not retry their password.
    try:
        if not row["is_active"]:
            raise HTTPException(403, "This account has been deactivated. Contact an administrator.")
    except (KeyError, IndexError):
        pass   # column predates migration on this DB — treat as active
    request.session["user_id"] = row["id"]
    return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]}


@router.post("/api/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"message": "Logged out"}


@router.post("/api/auth/forgot-password")
@limiter.limit("5/minute")
def forgot_password(request: Request, req: ForgotPasswordRequest):
    """Always returns the same generic message whether or not the email is
    registered — otherwise this endpoint would let anyone check which emails
    have accounts. Rate-limited by IP (no session exists yet at this point)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (req.email,)).fetchone()
    if row:
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now() + timedelta(hours=RESET_TOKEN_TTL_HOURS)).isoformat()
        conn.execute(
            "INSERT INTO password_resets (user_id, token, expires_at, created_at) VALUES (?,?,?,?)",
            (row["id"], token, expires_at, datetime.now().isoformat())
        )
        conn.commit()
        reset_link = f"{str(request.base_url).rstrip('/')}/?reset_token={token}"
        try:
            send_email(
                row["email"], "Reset your QuoteGen AI password",
                f"Hi {row['name']},\n\nClick the link below to reset your password. "
                f"This link is valid for {RESET_TOKEN_TTL_HOURS} hour(s).\n\n{reset_link}\n\n"
                f"If you didn't request this, you can ignore this email."
            )
        except Exception as e:
            # Don't leak email-service errors to the caller (would reveal the
            # email is registered) — log server-side for the admin to see.
            print(f"[forgot-password] failed to send email to {row['email']}: {e}")
    conn.close()
    return {"message": "If that email is registered, a reset link has been sent."}


@router.post("/api/auth/reset-password")
def reset_password(req: ResetPasswordRequest):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM password_resets WHERE token=? AND used=0", (req.token,)
    ).fetchone()
    if not row or row["expires_at"] < datetime.now().isoformat():
        conn.close()
        raise HTTPException(400, "This reset link is invalid or has expired. Please request a new one.")
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (_hash_password(req.new_password), row["user_id"]))
    conn.execute("UPDATE password_resets SET used=1 WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    return {"message": "Password reset successfully — you can now log in."}


@router.post("/api/auth/register")
@limiter.limit("5/minute")
def register(request: Request, req: RegisterRequest):
    """Self-service signup, restricted to company email domains — no Admin
    approval needed, but only an address on the allowed domain list can
    create an account at all. New accounts are always 'employee' role;
    Admin accounts are still only ever created via /api/auth/users."""
    email_domain = req.email.strip().lower().rsplit("@", 1)[-1] if "@" in req.email else ""
    if email_domain not in SIGNUP_ALLOWED_DOMAINS:
        raise HTTPException(
            403, f"Sign-up is only available for company email addresses "
                 f"({', '.join('@' + d for d in SIGNUP_ALLOWED_DOMAINS)})"
        )
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            (req.name, req.email, _hash_password(req.password), "employee", datetime.now().isoformat())
        )
        conn.commit()
        new_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(409, "An account with that email already exists")
    conn.close()
    user = {"id": new_id, "name": req.name, "email": req.email, "role": "employee"}
    log_action(user, "self_register", target=req.email)
    request.session["user_id"] = new_id
    return user


@router.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return user


@router.post("/api/auth/users")
def create_user(req: CreateUserRequest, admin: dict = Depends(require_role("admin"))):
    if req.role not in ("admin", "employee"):
        raise HTTPException(400, "role must be admin or employee")
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            (req.name, req.email, _hash_password(req.password), req.role, datetime.now().isoformat())
        )
        conn.commit()
        new_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(409, "A user with that email already exists")
    conn.close()
    log_action(admin, "create_user", target=req.email, after={"name": req.name, "role": req.role})
    return {"id": new_id, "name": req.name, "email": req.email, "role": req.role}


@router.get("/api/auth/users")
def list_users(admin: dict = Depends(require_role("admin"))):
    """Admin-only — the Users page and the activity log picker."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, email, role, is_active, created_at FROM users ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


class UpdateUserRequest(BaseModel):
    role: str = None            # "admin" | "employee", or None to leave as-is
    is_active: bool = None      # False deactivates, True reactivates, None leaves


@router.put("/api/auth/users/{user_id}")
def update_user(user_id: int, req: UpdateUserRequest,
                admin: dict = Depends(require_role("admin"))):
    """Change a user's role or switch their access on/off. Admin-only.

    Deactivation, never deletion — deleting a row would orphan the person's
    audit history and quotations. Two lockout guards:
    - you cannot demote or deactivate YOURSELF (one misclick would otherwise
      end the session you're doing it from), and
    - the last active admin cannot be demoted or deactivated, because a system
      with no admin cannot create one.
    """
    if req.role is not None and req.role not in ("admin", "employee"):
        raise HTTPException(400, "role must be admin or employee")

    conn = get_db()
    try:
        row = conn.execute("SELECT id, name, email, role, is_active FROM users WHERE id=?",
                           (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No such user")

        losing_admin = (row["role"] == "admin" and
                        ((req.role is not None and req.role != "admin") or
                         req.is_active is False))
        if losing_admin and user_id == admin["id"]:
            raise HTTPException(400, "You can't demote or deactivate your own account.")
        if losing_admin:
            others = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1 AND id<>?",
                (user_id,)).fetchone()[0]
            if others == 0:
                raise HTTPException(400, "That is the last active admin — promote someone else first.")

        changes, before, after = [], {}, {}
        if req.role is not None and req.role != row["role"]:
            changes.append(("role", req.role))
            before["role"], after["role"] = row["role"], req.role
        if req.is_active is not None and bool(req.is_active) != bool(row["is_active"]):
            changes.append(("is_active", 1 if req.is_active else 0))
            before["is_active"], after["is_active"] = bool(row["is_active"]), bool(req.is_active)
        if not changes:
            return {"message": "Nothing to change.", "id": user_id}

        conn.execute(f"UPDATE users SET {', '.join(f'{c}=?' for c, _ in changes)} WHERE id=?",
                     [v for _, v in changes] + [user_id])
        conn.commit()
    finally:
        conn.close()

    action = ("deactivate_user" if after.get("is_active") is False
              else "reactivate_user" if after.get("is_active") is True
              else "update_user_role")
    log_action(admin, action, target=row["email"], before=before, after=after)
    return {"message": f"Updated {row['name'] or row['email']}.", "id": user_id, **after}


@router.get("/api/audit-log")
def get_audit_log(user_id: int = None, admin: dict = Depends(require_role("admin"))):
    """Admin-only. Every employee's activity is tracked separately (tagged by
    user_id) and never merged — pass ?user_id=<id> to see just one person's
    history; omit it to see everyone's, newest first."""
    conn = get_db()
    if user_id:
        rows = conn.execute(
            "SELECT a.*, u.name as user_name, u.email as user_email FROM audit_log a "
            "LEFT JOIN users u ON u.id = a.user_id "
            "WHERE a.user_id=? ORDER BY a.created_at DESC LIMIT 500", (user_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT a.*, u.name as user_name, u.email as user_email FROM audit_log a "
            "LEFT JOIN users u ON u.id = a.user_id ORDER BY a.created_at DESC LIMIT 500"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
