import warnings
warnings.filterwarnings("ignore")

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import BASE, ALLOWED_ORIGINS, SESSION_SECRET, SESSION_MAX_AGE, limiter
from app import db as _db_init          # noqa: F401 — import triggers init_db()/migrate_db()
from app import auth as _auth_init      # noqa: F401 — import triggers admin bootstrap
from app.routers import auth, catalog, quotations, master_table

app = FastAPI(title="QuoteGen AI")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=SESSION_MAX_AGE)

class NoCacheStaticFiles(StaticFiles):
    """Browsers otherwise cache CSS/JS aggressively with no way to force a
    refresh — an update here can silently not reach a user until they
    manually hard-refresh. Fine to always revalidate for a low-traffic
    internal tool; simpler than versioned filenames."""
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.mount("/static", NoCacheStaticFiles(directory=BASE / "static"), name="static")

app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(quotations.router)
app.include_router(master_table.router)


@app.on_event("startup")
def _semantic_startup():
    """Build/refresh the semantic index in the background if missing or the
    catalogue moved. Fully non-fatal: the app runs fine without semantics."""
    try:
        from app.semantic import ensure_index_async
        ensure_index_async()
    except Exception as e:
        print(f"semantic startup skipped (non-fatal): {e}")


@app.get("/health")
def health():
    """Liveness for monitoring: process up AND the database answers."""
    from app.db import get_db
    conn = get_db()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(
        str(BASE / "static" / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"},
    )
