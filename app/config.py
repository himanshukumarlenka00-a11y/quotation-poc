import os, secrets, traceback, uuid
from pathlib import Path
from fastapi import Request, HTTPException
from slowapi import Limiter
from slowapi.util import get_remote_address

BASE = Path(__file__).parent.parent
# Env override so tests/scripts can point at a copy of the real data —
# without it every "DATA_DIR=…" invocation silently used the demo DB.
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE / "data"))
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

# Fallback LLM provider — used only when Groq answers 429 (rate limit).
# Key lives in /etc/quotegen/env on the server, never in the repo.
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")

# Primary LLM = Claude (Anthropic) when ANTHROPIC_API_KEY is set — the best
# judgment for product-match verification, and no free-tier daily token cap
# (the cap on the old Groq free tier was silently skipping the verify pass
# and letting wrong matches through). Falls back to Groq automatically when
# only a Groq key is present, so nothing breaks before the Anthropic key is
# added, and unsetting ANTHROPIC_API_KEY reverts cleanly. Key lives in
# /etc/quotegen/env on the server, never in the repo.
ANTHROPIC_API_KEY_DEFAULT = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# For the dashboard "AI usage" tile: the app can't read the real Anthropic
# balance with a normal API key, so it ESTIMATES spend from each call's token
# counts × these per-1M prices, counted down from the starting credit. All
# env-overridable so the figures track a model swap or a top-up without a code
# change. Prices below are Claude Sonnet's ($/1M tokens); USD_INR is display-only.
ANTHROPIC_CREDIT_USD = float(os.environ.get("ANTHROPIC_CREDIT_USD", "5"))
ANTHROPIC_PRICE_IN = float(os.environ.get("ANTHROPIC_PRICE_IN", "2"))
ANTHROPIC_PRICE_OUT = float(os.environ.get("ANTHROPIC_PRICE_OUT", "10"))
# Spend that hit the account OUTSIDE the app (e.g. diagnostic runs against
# isolated DB copies) and so was never logged per-call. Added to the lifetime
# total so "credit remaining" matches the real console balance instead of
# drifting above it. A fixed reconciliation constant; env-overridable.
ANTHROPIC_SPENT_OFFSET_USD = float(os.environ.get("ANTHROPIC_SPENT_OFFSET_USD", "0"))
USD_INR = float(os.environ.get("USD_INR", "88"))


def make_llm_client():
    """The configured LLM client: Anthropic (Claude) when ANTHROPIC_API_KEY is
    set, else Groq when GROQ_API_KEY is set, else None. _llm_chat dispatches on
    the client's provider (detected from its module), so switching providers is
    only a matter of which key is present in the environment — no code change."""
    if ANTHROPIC_API_KEY_DEFAULT:
        from anthropic import Anthropic
        return Anthropic(api_key=ANTHROPIC_API_KEY_DEFAULT)
    if GROQ_API_KEY_DEFAULT:
        from groq import Groq
        return Groq(api_key=GROQ_API_KEY_DEFAULT)
    return None

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


def server_error(e: Exception, what: str = "Request") -> HTTPException:
    """Log the traceback here; hand the client only a short error id.

    Every 500 used to inline traceback.format_exc() straight into the HTTP
    body, so absolute file paths and source lines were published to anyone
    who could make an endpoint fail. The trace still goes to the journal
    (journalctl -u quotegen), keyed by the same id the user is shown, so
    debugging costs one grep instead of a screenshot."""
    eid = uuid.uuid4().hex[:8]
    print(f"[error {eid}] {what}: {type(e).__name__}: {e}")
    traceback.print_exc()
    return HTTPException(500, f"{what} failed — please try again. "
                              f"Quote error id {eid} if it keeps happening.")
