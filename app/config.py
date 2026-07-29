import os, secrets
from pathlib import Path
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

BASE = Path(__file__).parent.parent
DATA_DIR = BASE / "data"
UPLOADS_DIR = BASE / "uploads"
MASTER_UPLOADS_DIR = BASE / "uploads" / "master_table"
EXPORTS_DIR = BASE / "exports"
IMAGES_DIR = BASE / "data" / "images"
IMAGES_THUMB_DIR = IMAGES_DIR / "thumb"
for d in [DATA_DIR, UPLOADS_DIR, MASTER_UPLOADS_DIR, EXPORTS_DIR, IMAGES_DIR, IMAGES_THUMB_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "quotations.db"

# Session secret must survive restarts (else every restart logs everyone out) —
# read from env if set, otherwise persist a generated one to a local file.
SESSION_SECRET_PATH = DATA_DIR / ".session_secret"
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if not SESSION_SECRET:
    if SESSION_SECRET_PATH.exists():
        SESSION_SECRET = SESSION_SECRET_PATH.read_text().strip()
    else:
        SESSION_SECRET = secrets.token_hex(32)
        SESSION_SECRET_PATH.write_text(SESSION_SECRET)

SESSION_MAX_AGE = 60 * 60 * 8  # 8h idle session timeout

# Restrict CORS to known origins instead of "*" — configurable via env for
# when this moves to the real server domain (Phase 5); defaults to local dev.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
).split(",") if o.strip()]

GROQ_API_KEY_DEFAULT = os.environ.get("GROQ_API_KEY", "")

# Self-registration is only allowed for company email addresses — keeps
# random visitors from creating themselves an account if this is ever
# reachable off the internal network. Configurable via env in case the
# company domain changes; comma-separated for multiple domains.
SIGNUP_ALLOWED_DOMAINS = [d.strip().lower() for d in os.environ.get(
    "SIGNUP_ALLOWED_DOMAINS", "melangeindia.in"
).split(",") if d.strip()]

def _rate_limit_key(request: Request) -> str:
    """Rate-limit by logged-in user, not IP. Many employees on the same office
    network share one public IP behind NAT — IP-based limiting would treat all
    of them as a single client and throttle the whole office together instead
    of each person independently. Falls back to IP when there's no session yet
    (e.g. the login endpoint itself, which should stay IP-based to block
    password brute-forcing from one source)."""
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if user_id:
        return f"user:{user_id}"
    return get_remote_address(request)


# Shared rate limiter — a leaf singleton so both main.py (exception handler,
# app.state) and the routers (@limiter.limit(...) decorators) can import it
# without a circular import.
limiter = Limiter(key_func=_rate_limit_key)
