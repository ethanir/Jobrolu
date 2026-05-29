"""
FastAPI server - serves the ranked feed to the UI.

Access model (simple and wallet-safe):
    - Viewing the feed is OPEN to everyone (landing, /app, /api/jobs).
    - Anything that spends the API budget or changes data is OWNER-ONLY, gated
      behind a single access code: refresh, scan, onboard, save-profile.
    - Unlock once with the code and a cookie keeps you unlocked. The feed itself
      never asks for a code.

Set ACCESS_CODE in the host's env (e.g. ACCESS_CODE=ethan) to turn the gate on.
If ACCESS_CODE is unset, the gate is OFF (handy for local dev).
Optionally set COOKIE_SECRET to a fixed random string so the unlock survives
redeploys (otherwise it resets each restart and you re-enter the code once).

Identity (Phase 2):
    Every visitor gets a stable per-browser id in an httponly cookie (jr_uid),
    resolved on every request as request.state.user_id and best-effort upserted
    into the users table. This is the foundation for per-user profiles and
    rankings. It is invisible today: the feed still serves the shared file feed
    for everyone, because no one has per-user rankings yet. See the browser
    identity section below.

Data source resolution:
    1. If DATABASE_URL is set and reachable -> Postgres (db.py).
    2. Otherwise -> read ranked_jobs.json produced by main.py (dev fallback).

Run:
    pip install -r requirements.txt
    uvicorn server:app --reload --port 8000
"""
import json
import os
import threading
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel

import db
import scan as scan_mod
import main as pipeline
import jobcache


class ScanInput(BaseModel):
    text: str
    user_id: str = "me"   # accepted for backward compat; identity comes from the cookie


# ---------------------------------------------------------------- refresh state
# A single in-process refresh job. Tracks live progress for the UI to poll.
_refresh = {
    "running": False,
    "stage": "idle",
    "pct": 0,
    "detail": "",
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}
_refresh_lock = threading.Lock()


def _run_refresh(profile_path, top_n=None):
    def progress(stage, pct, detail):
        with _refresh_lock:
            _refresh.update(stage=stage, pct=pct, detail=detail)
    try:
        with _refresh_lock:
            _refresh.update(running=True, stage="Starting", pct=0, detail="",
                            started_at=time.time(), finished_at=None,
                            result=None, error=None)
        summary = pipeline.run(profile_path, progress=progress, top_n=top_n)
        # Persist the freshly written pool to Postgres so it survives a redeploy
        # (the host's disk is ephemeral). Best-effort, never breaks the refresh.
        _persist_pool()
        with _refresh_lock:
            _refresh.update(running=False, stage="Done", pct=100,
                            finished_at=time.time(), result=summary)
    except Exception as e:
        with _refresh_lock:
            _refresh.update(running=False, stage="Error", error=str(e),
                            finished_at=time.time())

app = FastAPI(title="Jobrolu API", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ---------------------------------------------------------------- access gate
# A single shared access code protects every paid / data-changing action, so
# only people with the code can spend the API budget. The feed stays public.
# Set ACCESS_CODE in the host's env; if it's unset, the gate is OFF (local dev).
import hashlib as _hashlib
import secrets as _secrets
import hmac as _hmac

ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()
# A per-process secret so the unlock cookie can't be guessed/forged. Set a fixed
# COOKIE_SECRET in env to keep people unlocked across redeploys.
_COOKIE_SECRET = os.environ.get("COOKIE_SECRET", _secrets.token_hex(16))


def _valid_token():
    """The cookie value a correctly-unlocked client should carry."""
    return _hashlib.sha256((ACCESS_CODE + _COOKIE_SECRET).encode()).hexdigest()


def _is_unlocked(request):
    """True if this request carries a valid unlock cookie. If no ACCESS_CODE is
    configured the gate is disabled (everything is treated as unlocked)."""
    if not ACCESS_CODE:
        return True
    token = request.cookies.get("jr_access")
    return bool(token) and _secrets.compare_digest(token, _valid_token())


def _require_unlock(request):
    if not _is_unlocked(request):
        raise HTTPException(status_code=401, detail="locked")


# ---------------------------------------------------------------- account sessions
# A signed, stateless session cookie. Its value is "uid.signature" where the
# signature is HMAC-SHA256(COOKIE_SECRET, uid); it cannot be forged without the
# secret. Sign in sets it, sign out clears it, and the identity middleware reads
# it to resolve the signed-in account.
SESSION_COOKIE = "jr_session"
_SESSION_MAX_AGE = 60 * 60 * 24 * 30   # 30 days


def _session_sig(uid: str) -> str:
    return _hmac.new(_COOKIE_SECRET.encode(), uid.encode(), _hashlib.sha256).hexdigest()


def _make_session(uid: str) -> str:
    return uid + "." + _session_sig(uid)


def _read_session(request):
    """Return the signed-in account id from a valid session cookie, or None."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw or "." not in raw:
        return None
    uid, sig = raw.rsplit(".", 1)
    if uid and _secrets.compare_digest(sig, _session_sig(uid)):
        return uid
    return None


def _signed_in(request) -> bool:
    return bool(getattr(request.state, "signed_in", False))


def _require_auth(request):
    if not _signed_in(request):
        raise HTTPException(status_code=401, detail="sign in required")


def _valid_email(e) -> bool:
    e = (e or "").strip()
    return 3 < len(e) <= 254 and "@" in e and "." in e.split("@")[-1] and " " not in e


# ---------------------------------------------------------------- browser identity
# Every visitor gets a stable per-browser id in an httponly cookie (jr_uid). The
# server resolves the current user_id from it on every request and best-effort
# upserts a users row, so per-user profiles and rankings have something to attach
# to. This phase is invisible to visitors: the feed still serves the shared file
# because no one has per-user rankings yet, so behaviour is unchanged.
#
# Owner identity: the human who owns the deployment is whoever holds a valid
# unlock cookie (jr_access). On unlock we promote their users row to is_owner
# (sticky). Optionally set OWNER_USER_ID in env to a long RANDOM value (e.g. a
# uuid4 hex) to give the owner ONE identity across their devices: on unlock the
# owner's browser adopts it. Leaving it unset is fine, each browser the owner
# unlocks on simply becomes its own owner row. Spending is ALWAYS gated by the
# unlock cookie regardless of jr_uid, so this can never let a visitor spend.
OWNER_USER_ID = os.environ.get("OWNER_USER_ID", "").strip()
JR_UID_COOKIE = "jr_uid"
_UID_MAX_AGE = 60 * 60 * 24 * 365 * 2   # ~2 years


def _valid_uid(v) -> bool:
    """Accept only an id we would have issued: a uuid, or the configured
    OWNER_USER_ID. Anything malformed or oversized is rejected and re-minted."""
    if not v or len(v) > 64:
        return False
    if OWNER_USER_ID and v == OWNER_USER_ID:
        return True
    try:
        uuid.UUID(v)
        return True
    except Exception:
        return False


@app.middleware("http")
async def _identity(request: Request, call_next):
    """Resolve the current user. A valid signed-in account session wins; otherwise
    fall back to (or mint) the anonymous per-browser id. The jr_uid Set-Cookie only
    goes out when we minted a fresh one, so returning visitors are not re-cookied."""
    account = _read_session(request)
    uid = request.cookies.get(JR_UID_COOKIE)
    fresh = None
    if not _valid_uid(uid):
        uid = uuid.uuid4().hex
        fresh = uid
    # The account session is the identity when present; the browser id is the
    # anonymous fallback and is what a new account adopts when it signs up.
    request.state.signed_in = bool(account)
    request.state.user_id = account or uid
    request.state.browser_id = uid
    response = await call_next(request)
    if fresh:
        response.set_cookie(
            JR_UID_COOKIE, fresh,
            max_age=_UID_MAX_AGE, httponly=True, samesite="lax",
            secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        )
    return response


RANKED_FILE = os.environ.get("RANKED_FILE", "ranked_jobs.json")

# In-memory cache of the parsed shared pool. The pool is large, so we avoid
# re-reading it on every request: we keep the parsed list here keyed by a cheap
# freshness token (the DB row's updated_at, or the file mtime), and only reload
# the big payload when that token changes. This also gives durability: the pool
# is read from Postgres first (which survives redeploys) and only falls back to
# the committed file when the DB is empty or off (first boot / local dev).
_POOL_CACHE = {"src": None, "token": None, "data": None}
_POOL_LOCK = threading.Lock()


def _pool_list():
    """Return the raw shared job pool as a parsed list, DB-first with a file
    fallback, cached by freshness token. Treat the result as READ-ONLY: callers
    that mutate (the scorer) must copy first (see _raw_pool)."""
    # Postgres first (durable). A cheap meta probe decides cache freshness.
    if db.has_db():
        try:
            with db.get_conn() as conn:
                if conn:
                    meta = db.pool_meta(conn)
                    if meta is not None:
                        token = ("db", str(meta[1]))
                        with _POOL_LOCK:
                            if _POOL_CACHE["src"] == "db" and _POOL_CACHE["token"] == token:
                                return _POOL_CACHE["data"]
                        data = db.load_pool(conn)
                        if data is not None:
                            with _POOL_LOCK:
                                _POOL_CACHE.update(src="db", token=token, data=data)
                            return data
        except Exception:
            pass
    # File fallback (committed seed, or local dev with no DB).
    if not os.path.exists(RANKED_FILE):
        return []
    try:
        token = ("file", os.path.getmtime(RANKED_FILE))
        with _POOL_LOCK:
            if _POOL_CACHE["src"] == "file" and _POOL_CACHE["token"] == token:
                return _POOL_CACHE["data"]
        with open(RANKED_FILE) as f:
            data = json.load(f)
        with _POOL_LOCK:
            _POOL_CACHE.update(src="file", token=token, data=data)
        return data
    except Exception:
        return []


def _persist_pool():
    """After a refresh, copy the freshly written ranked file into Postgres so it
    survives redeploys, and refresh the in-memory cache. Best-effort: never
    raises, so it can never break the refresh that produced the data."""
    if not db.has_db():
        return
    try:
        if not os.path.exists(RANKED_FILE):
            return
        with open(RANKED_FILE) as f:
            jobs = json.load(f)
        with db.get_conn() as conn:
            if conn:
                db.save_pool(conn, jobs)
        # Drop the cache token so the next read reloads from the DB.
        with _POOL_LOCK:
            _POOL_CACHE.update(src=None, token=None, data=None)
    except Exception:
        pass


# Scoring the whole pool against a profile is the expensive step (seconds for a
# large pool), and the feed only ever shows the best matches, so we score once
# per (pool version, profile) and cache the top slice. The verified-rankings
# overlay is applied fresh on every request on top of this base, so a user's own
# AI rankings always show immediately. BYO-AI export only ever offers the top
# RANK_EXPORT_MAX jobs, so a job a user can verify is always inside this slice;
# capping it can never hide a verified match.
BASE_KEEP = 400                       # most jobs the feed keeps after ranking
_BASE_CACHE = {}                      # profile-hash -> {"token","jobs"}
_BASE_LOCK = threading.Lock()
_BASE_MAX_PROFILES = 32               # bound memory: keep recent profiles only


def _pool_token():
    """A cheap value that changes whenever the pool changes, used to invalidate
    the scored-base cache (DB updated_at, else the file mtime)."""
    if db.has_db():
        try:
            with db.get_conn() as conn:
                if conn:
                    meta = db.pool_meta(conn)
                    if meta is not None:
                        return ("db", str(meta[1]))
        except Exception:
            pass
    try:
        if os.path.exists(RANKED_FILE):
            return ("file", os.path.getmtime(RANKED_FILE))
    except Exception:
        pass
    return ("none", 0)


def _profile_hash(profile):
    try:
        return _hashlib.sha1(json.dumps(profile, sort_keys=True, default=str).encode()).hexdigest()
    except Exception:
        return repr(profile)[:200]


def _scored_base(profile):
    """Return the top BASE_KEEP jobs ranked for this profile, cached by
    (pool token, profile hash). Each entry is a plain dict carrying the fields
    the feed and the rank export need, plus the heuristic _score. Recomputed
    only when the pool or the profile changes."""
    import score
    token = _pool_token()
    key = _profile_hash(profile)
    with _BASE_LOCK:
        hit = _BASE_CACHE.get(key)
        if hit and hit["token"] == token:
            return hit["jobs"]
    pool = _raw_pool()          # shallow copies, safe for the scorer to mutate
    if not pool:
        return []
    score.rank_free(pool, profile)      # sets _score/_matched, sorts best-first
    base = []
    for j in pool[:BASE_KEEP]:
        base.append({
            "id": db.job_hash(j),
            "company": j.get("company", "") or "",
            "title": j.get("title", "") or "",
            "location": j.get("location", "") or "",
            "url": j.get("url", "") or "",
            "source": j.get("source", "") or j.get("ats", "") or "",
            "description": (j.get("description", "") or "")[:1200],
            "is_new": j.get("is_new", False),
            "_score": j.get("_score", 0),
            "_matched": j.get("_matched", []),
        })
    with _BASE_LOCK:
        if len(_BASE_CACHE) >= _BASE_MAX_PROFILES and key not in _BASE_CACHE:
            # evict an arbitrary existing entry to stay bounded
            _BASE_CACHE.pop(next(iter(_BASE_CACHE)), None)
        _BASE_CACHE[key] = {"token": token, "jobs": base}
    return base


# ---------------------------------------------------------------- shaping
def _shape(job):
    """Normalize any internal job dict into the exact shape the UI consumes."""
    fit = job.get("fit") or {}
    return {
        "id": job.get("id") or db.job_hash(job),
        "company": job.get("company", ""),
        "title": job.get("title", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "posted": job.get("posted", ""),
        "source": job.get("source", "") or job.get("ats", ""),
        "tier": job.get("tier") or fit.get("tier") or "possible",
        "score": job.get("score") if job.get("score") is not None else fit.get("score") or 0,
        "reasons": job.get("reasons") or fit.get("reasons") or [],
        "matched": job.get("matched") or fit.get("matched_skills") or [],
        "missing": job.get("missing") or fit.get("missing_skills") or [],
        "contacts": [
            {"name": c.get("name", ""), "title": c.get("title", ""),
             "email": c.get("email", ""),
             "status": c.get("status") or c.get("email_status") or "unknown",
             "linkedin": c.get("linkedin", "")}
            for c in (job.get("contacts") or [])
        ],
        "draft": job.get("draft", ""),
        "linkedin_search": job.get("linkedin_search", ""),
        "is_new": job.get("is_new", False),
    }


def _from_file():
    # Shaped feed from the shared pool (DB-first, file fallback). _shape builds
    # fresh dicts, so reading the cached list here is safe.
    return [_shape(j) for j in _pool_list()]


def _from_db(user_id, tier):
    """Per-user ranked feed from Postgres, or None to fall back to the file feed
    (Postgres off, unreachable, or this user has no rankings yet)."""
    with db.get_conn() as conn:
        if not conn:
            return None
        rows = db.fetch_ranked(conn, user_id)
        if not rows:
            # Postgres is on but this user has no rankings yet (typical right
            # after Postgres is first wired up). Fall back to the shared file
            # feed instead of showing an empty page.
            return None
        out = []
        for r in rows:
            out.append(_shape({
                "id": r["id"], "company": r["company"], "title": r["title"],
                "location": r["location"], "source": r["source"],
                "tier": r["tier"], "score": r["score"],
                "reasons": r["reasons"] or [], "matched": r["matched"] or [],
                "missing": r["missing"] or [],
            }))
        return out


def _load(user_id="me", tier=None):
    data = _from_db(user_id, tier)
    source = "db"
    if data is None:
        data = _from_file()
        source = "file"
    if tier and tier != "all":
        data = [j for j in data if j["tier"] == tier]
    return data, source


def _raw_pool():
    """The shared, unshaped job pool (the latest owner refresh output), DB-first
    with a file fallback. Returns shallow COPIES of each job dict: the heuristic
    scorer mutates jobs (adds _score/_matched) and sorts the list, so it must
    never touch the shared in-memory cache."""
    return [dict(j) for j in _pool_list()]


def _user_profile(user_id):
    """This user's OWN saved profile from Postgres, or None. No file fallback,
    so a visitor with no profile is correctly treated as having none (and gets
    the shared sample feed rather than the owner's profile)."""
    if not db.has_db():
        return None
    try:
        with db.get_conn() as conn:
            if conn:
                return db.get_profile(conn, user_id)
    except Exception:
        return None
    return None


def _visitor_feed(user_id, profile, tier):
    """A feed ranked for THIS visitor: the cached heuristic base for their
    profile ($0), with any of their own verified rankings (e.g. bring-your-own-AI)
    overlaid fresh on top. The heuristic never awards 'strong' (only a real AI
    ranking can), so unverified jobs show as estimates in the UI."""
    import score
    base = _scored_base(profile)
    if not base:
        return _from_file(), "file"
    overlay = {}
    if db.has_db():
        try:
            with db.get_conn() as conn:
                if conn:
                    overlay = db.get_rankings_map(conn, user_id)
        except Exception:
            overlay = {}
    out = []
    for j in base:
        jid = j["id"]
        if jid in overlay:
            f = overlay[jid]
            fit = {"tier": f.get("tier"), "score": f.get("score"),
                   "reasons": f.get("reasons") or [],
                   "matched_skills": f.get("matched_skills") or [],
                   "missing_skills": f.get("missing_skills") or []}
        else:
            fit = score.heuristic_fit(j)
        out.append(_shape({
            "id": jid,
            "company": j["company"], "title": j["title"],
            "location": j["location"], "url": j["url"],
            "source": j["source"],
            "fit": fit,
            "is_new": j["is_new"],
        }))
    order = {"strong": 0, "possible": 1, "skip": 2}
    out.sort(key=lambda x: (order.get(x["tier"], 3), -(x["score"] or 0)))
    if tier and tier != "all":
        out = [j for j in out if j["tier"] == tier]
    return out, "heuristic"


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    _, source = _load()
    return {"status": "ok", "source": source}


@app.get("/api/jobs")
def list_jobs(request: Request, tier: str = "all"):
    user_id = request.state.user_id
    # With no database (local/dev) there are no accounts or per-user profiles, so
    # we serve the shared file feed rather than locking everyone out.
    if not db.has_db():
        data, source = _load(user_id, tier)
        return {"source": source, "count": len(data), "jobs": data, "needs_auth": False, "needs_profile": False}
    # Everyone is an account now. You must be signed in to see a feed, and you
    # need a profile so it can be ranked for you.
    if not _signed_in(request):
        return {"source": "none", "count": 0, "jobs": [], "needs_auth": True, "needs_profile": False}
    try:
        with db.get_conn() as conn:
            if conn:
                db.ensure_user(conn, user_id)
    except Exception:
        pass
    prof = _user_profile(user_id)
    if not prof:
        return {"source": "none", "count": 0, "jobs": [], "needs_auth": False, "needs_profile": True}
    try:
        data, source = _visitor_feed(user_id, prof, tier)
    except Exception:
        # A malformed profile should never leak another feed; show an empty
        # personalized feed rather than the shared sample.
        data, source = [], "heuristic"
    return {"source": source, "count": len(data), "jobs": data, "needs_auth": False, "needs_profile": False}


@app.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    data, _ = _load(request.state.user_id)
    for j in data:
        if j["id"] == job_id:
            return j
    raise HTTPException(status_code=404, detail="job not found")


def _load_profile(user_id):
    """Load the user's profile from DB or my_profile.json fallback."""
    with db.get_conn() as conn:
        if conn:
            prof = db.get_profile(conn, user_id)
            if prof is not None:
                return prof
    for path in ("my_profile.json", "profile.example.json"):
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    return None


@app.post("/api/scan")
def scan_endpoint(request: Request, inp: ScanInput):
    """Paste-a-JD-or-URL endpoint. Runs fit-rank + contacts + draft on one role.
    Owner-only (spends the API budget)."""
    _require_unlock(request)
    if not inp.text.strip():
        raise HTTPException(status_code=400, detail="text is empty")
    profile = _load_profile(request.state.user_id)
    if not profile:
        raise HTTPException(status_code=400, detail="no profile found for user")
    try:
        result = scan_mod.scan(inp.text, profile)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"scan failed: {e}")
    return _shape(result)


# ---------------------------------------------------------------- refresh
class RefreshInput(BaseModel):
    profile_path: str = os.environ.get("PROFILE_PATH", "my_profile.json")
    top_n: Optional[int] = None


# Hard ceiling on how many jobs one refresh may send to the LLM, regardless of
# what the client posts. The slider tops out at 1000; this is the backstop.
MAX_TOP_N = 2000


@app.post("/api/refresh")
def start_refresh(request: Request, inp: RefreshInput = RefreshInput()):
    """Kick off a background pipeline run. Owner-only: this is the paid step, so
    it requires the unlock cookie. Old jobs are preserved; new postings are
    appended and flagged. Returns immediately; poll /api/refresh/status."""
    _require_unlock(request)
    with _refresh_lock:
        if _refresh["running"]:
            return {"started": False, "reason": "a refresh is already running"}
    path = inp.profile_path
    if not os.path.exists(path):
        for alt in ("my_profile.json", "profile.example.json"):
            if os.path.exists(alt):
                path = alt
                break
    top_n = inp.top_n
    if top_n is not None:
        top_n = max(0, min(int(top_n), MAX_TOP_N))
    t = threading.Thread(target=_run_refresh, args=(path, top_n), daemon=True)
    t.start()
    return {"started": True, "top_n": top_n}


@app.get("/api/refresh/status")
def refresh_status():
    """Live progress of the running (or last) refresh, for the UI progress bar."""
    with _refresh_lock:
        return dict(_refresh)


@app.get("/api/cache/stats")
def cache_stats():
    """How many jobs the AI has already ranked (cached). The UI uses this to show
    the real marginal cost of a refresh: cached jobs are free, so only depth beyond
    what's already scanned actually costs money."""
    try:
        cache = jobcache.load()
        return {"ranked": len(cache.get("ranked", {})),
                "seen": len(cache.get("seen", {}))}
    except Exception:
        return {"ranked": 0, "seen": 0}


# ---------------------------------------------------------------- web UI
@app.get("/")
def index():
    """Serve the marketing landing page."""
    if os.path.exists("landing.html"):
        return FileResponse("landing.html")
    for path in ("app.html", "viewer.html"):
        if os.path.exists(path):
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="no UI file found (build landing.html / app.html)")


@app.get("/app")
def app_ui(request: Request):
    """Serve the ranked-feed app. Requires a signed-in account; we redirect
    server-side (before the page renders) so a signed-out visitor never sees a
    flash of the app before being sent to sign in."""
    if db.has_db() and not _signed_in(request):
        return RedirectResponse("/signin", status_code=303)
    for path in ("app.html", "viewer.html"):
        if os.path.exists(path):
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="no app UI found (build app.html)")


@app.get("/start")
def start_ui(request: Request):
    """Serve the profile page. Requires a signed-in account; redirect server-side
    (before render) so a signed-out visitor is sent straight to sign in with no
    flash of the profile page."""
    if db.has_db() and not _signed_in(request):
        return RedirectResponse("/signin", status_code=303)
    if os.path.exists("start.html"):
        return FileResponse("start.html")
    raise HTTPException(status_code=404, detail="no start.html found")


@app.get("/unlock")
def unlock_page():
    """The access-code entry page (kept for direct access; the app has its own
    inline unlock now)."""
    if os.path.exists("unlock.html"):
        return FileResponse("unlock.html")
    raise HTTPException(status_code=404, detail="no unlock.html found")


@app.get("/api/unlock/status")
def unlock_status(request: Request):
    """Lets the UI know whether this browser is already unlocked, so it can show
    the unlocked state on load. The unlock cookie is httponly, so the client
    cannot read it directly and asks here instead."""
    return {"unlocked": _is_unlocked(request), "gate_on": bool(ACCESS_CODE)}


@app.post("/api/unlock")
async def do_unlock(request: Request):
    """Check the access code; on success set the unlock cookie and promote this
    browser's users row to owner."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    code = (data.get("code") or "").strip()
    if not ACCESS_CODE:
        # gate disabled; treat as already unlocked
        return {"ok": True}
    if code and _secrets.compare_digest(code, ACCESS_CODE):
        # Promote the owner's users row (sticky). If OWNER_USER_ID is set, the
        # owner is that fixed identity across devices; otherwise it's this
        # browser's id.
        owner_uid = OWNER_USER_ID or request.state.user_id
        try:
            with db.get_conn() as conn:
                if conn:
                    db.ensure_user(conn, owner_uid, is_owner=True)
        except Exception:
            pass
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            "jr_access", _valid_token(),
            max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
            secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        )
        # Adopt the canonical owner identity on this browser so the owner is one
        # user across devices. Safe: spending is gated by jr_access, not jr_uid.
        if OWNER_USER_ID and OWNER_USER_ID != request.state.user_id:
            resp.set_cookie(
                JR_UID_COOKIE, OWNER_USER_ID,
                max_age=_UID_MAX_AGE, httponly=True, samesite="lax",
                secure=os.environ.get("COOKIE_SECURE", "1") == "1",
            )
        return resp
    return JSONResponse({"ok": False, "error": "Incorrect code."}, status_code=403)


# ---------------------------------------------------------------- accounts (auth)
def _cookie_kw():
    return dict(httponly=True, samesite="lax",
                secure=os.environ.get("COOKIE_SECURE", "1") == "1")


@app.get("/signin")
def signin_page(request: Request):
    """The sign in / create account page. If already signed in, go to the app."""
    if db.has_db() and _signed_in(request):
        return RedirectResponse("/app", status_code=303)
    if os.path.exists("signin.html"):
        return FileResponse("signin.html")
    raise HTTPException(status_code=404, detail="no signin.html found")


@app.get("/api/auth/me")
def auth_me(request: Request):
    """Who is signed in, for the nav and gating. Reads the (httponly) session."""
    if not _signed_in(request):
        return {"signed_in": False}
    email = None
    try:
        with db.get_conn() as conn:
            if conn:
                u = db.get_user(conn, request.state.user_id)
                email = (u or {}).get("email")
    except Exception:
        email = None
    return {"signed_in": True, "email": email}


@app.post("/api/auth/register")
async def auth_register(request: Request):
    """Create an account. Reuses this browser's id, so any profile already built
    is kept. Sets the session cookie on success."""
    if not db.has_db():
        return JSONResponse({"ok": False, "error": "Accounts are unavailable right now."}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        data = {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not _valid_email(email):
        return JSONResponse({"ok": False, "error": "Enter a valid email address."}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"ok": False, "error": "Use a password of at least 8 characters."}, status_code=400)
    uid = request.state.browser_id      # upgrade THIS browser's anonymous identity
    try:
        with db.get_conn() as conn:
            if not conn:
                return JSONResponse({"ok": False, "error": "Accounts are unavailable right now."}, status_code=503)
            db.ensure_user(conn, uid)
            ok = db.set_account(conn, uid, email, db.hash_password(password))
    except Exception:
        return JSONResponse({"ok": False, "error": "Could not create the account. Try again."}, status_code=500)
    if not ok:
        return JSONResponse({"ok": False, "error": "That email is already registered. Sign in instead."}, status_code=409)
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie(SESSION_COOKIE, _make_session(uid), max_age=_SESSION_MAX_AGE, **_cookie_kw())
    return resp


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """Verify email + password; on success set the session cookie to the account's
    id (which may differ from this browser's anonymous id)."""
    if not db.has_db():
        return JSONResponse({"ok": False, "error": "Accounts are unavailable right now."}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        data = {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    acct = None
    try:
        with db.get_conn() as conn:
            if conn:
                acct = db.get_account_by_email(conn, email)
    except Exception:
        acct = None
    # One generic message whether the email is unknown or the password is wrong,
    # so we never reveal which emails have accounts.
    if not acct or not acct.get("password_hash") or not db.verify_password(password, acct["password_hash"]):
        return JSONResponse({"ok": False, "error": "Wrong email or password."}, status_code=401)
    resp = JSONResponse({"ok": True, "email": acct.get("email")})
    resp.set_cookie(SESSION_COOKIE, _make_session(acct["id"]), max_age=_SESSION_MAX_AGE, **_cookie_kw())
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    """Clear the session cookie."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/profile")
def read_profile(request: Request):
    """Return the current user's saved profile, or null if they have none yet,
    so the onboarding UI can prefill. Whoever holds the refresh code falls back to
    the shared file so it prefills too; everyone else gets null (a blank form)."""
    user_id = request.state.user_id
    prof = None
    if db.has_db():
        try:
            with db.get_conn() as conn:
                if conn:
                    prof = db.get_profile(conn, user_id)
        except Exception:
            prof = None
    if prof is None and (_is_unlocked(request) or not db.has_db()):
        for path in (os.environ.get("PROFILE_PATH", "my_profile.json"),
                     "profile.example.json"):
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        prof = json.load(f)
                    break
                except Exception:
                    pass
    return {"profile": prof}


@app.post("/api/profile")
async def save_profile(request: Request):
    """Save the signed-in account's own profile. Free and only affects their own
    feed. The shared my_profile.json that the paid pipeline reads is only written
    by someone holding the refresh code (or in local dev), so a normal account
    can never overwrite it."""
    if db.has_db():
        _require_auth(request)
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Body was not valid JSON."}
    if not isinstance(data, dict):
        return {"ok": False, "error": "Expected a single JSON object."}
    user_id = request.state.user_id
    saved = None
    if db.has_db():
        try:
            with db.get_conn() as conn:
                if conn:
                    # users row must exist first: profiles.user_id references it
                    db.ensure_user(conn, user_id)
                    db.save_profile(conn, user_id, data)
                    saved = "db"
        except Exception:
            pass
    # Whoever holds the refresh code (or local dev with no Postgres) also updates
    # the shared file the pipeline reads, so their edits flow into the next refresh.
    if _is_unlocked(request) or not db.has_db():
        path = os.environ.get("PROFILE_PATH", "my_profile.json")
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            saved = saved or "file"
        except Exception as e:
            if saved is None:
                return {"ok": False, "error": f"Could not write profile: {e}"}
    if saved is None:
        return {"ok": False, "error": "Could not save your profile. Try again."}
    return {"ok": True, "name": data.get("name")}


@app.post("/api/onboard")
async def onboard_resume(request: Request, file: UploadFile = File(...)):
    """Accept a resume upload and parse it into a profile for review. Open to any
    signed-in account (parsing is a small, cheap model call). The result is
    returned for review and is not committed to the feed here."""
    if db.has_db():
        _require_auth(request)
    import tempfile
    suffix = os.path.splitext(file.filename or "")[1].lower() or ".txt"
    if suffix not in (".pdf", ".docx", ".txt", ".md"):
        return {"ok": False, "error": "Use a PDF, DOCX, or TXT resume."}
    try:
        import onboard as onboard_mod
    except Exception as e:
        return {"ok": False, "error": f"Onboarding unavailable: {e}"}
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name
        path = os.environ.get("PROFILE_PATH", "my_profile.json")
        profile = onboard_mod.onboard(tmp_path, path)
        os.unlink(tmp_path)
        # Review-first: return the parsed profile so the UI can prefill the manual
        # form for the user to check and correct. We do NOT save it to their feed
        # here; that happens when they review and hit Save (POST /api/profile).
        return {"ok": True, "name": profile.get("name"), "profile": profile}
    except Exception as e:
        msg = str(e)
        if "pypdf" in msg:
            friendly = ("Couldn't read that PDF on the server. Try uploading a .docx or "
                        ".txt version, or use the 'bring your own AI' option below.")
        elif "python-docx" in msg or "docx" in msg.lower():
            friendly = ("Couldn't read that Word file. Try a .pdf or .txt version, or use "
                        "the 'bring your own AI' option below.")
        elif "unsupported resume type" in msg:
            friendly = "Please upload a PDF, DOCX, or TXT resume."
        else:
            friendly = ("Couldn't read that file. Try a different format, or use the "
                        "'bring your own AI' option below.")
        return {"ok": False, "error": friendly}


# ---------------------------------------------------------------- BYO-AI ranking
# Let a visitor turn their free keyword-estimate feed into real, verified AI fits
# using their OWN ChatGPT/Claude, at no cost to the owner. The feed already
# overlays a user's stored rankings (Phase 3); these two endpoints produce and
# store them. The prompt format mirrors export_rank.py / import_rank.py so the
# web flow and the CLI stay consistent.
RANK_EXPORT_MAX = 50          # most jobs we ask a web chat to score at once
RANK_IMPORT_MAX = 200         # hard cap on rankings accepted per import call
_VALID_TIERS = ("strong", "possible", "skip")


def _rank_candidates(user_id, profile, limit):
    """The visitor's top-N jobs to hand their AI: highest free score first,
    skipping ones they have already had verified, each keyed by the SAME id the
    feed uses (db.job_hash) so the AI's reply maps straight back. Uses the cached
    scored base, so it does not re-score the whole pool."""
    base = _scored_base(profile)
    if not base:
        return []
    already = {}
    if db.has_db():
        try:
            with db.get_conn() as conn:
                if conn:
                    already = db.get_rankings_map(conn, user_id)
        except Exception:
            already = {}
    out = []
    for j in base:
        if j["id"] in already:
            continue
        out.append({
            "id": j["id"],
            "company": j["company"],
            "title": j["title"],
            "location": j["location"],
            "description": (j.get("description", "") or "")[:1200],
        })
        if len(out) >= limit:
            break
    return out


def _build_rank_prompt(profile, jobs):
    """The exact prompt a visitor pastes into their own AI. Same shape as
    export_rank.py: a JSON array of {id, score, tier, reasons, matched_skills,
    missing_skills}, one object per job."""
    slim = {
        "target_titles": profile.get("target_titles"),
        "years_experience": profile.get("years_experience"),
        "work_authorization": profile.get("work_authorization"),
        "requires_sponsorship": profile.get("requires_sponsorship"),
        "skills": profile.get("skills"),
        "preferences": profile.get("preferences"),
        "projects": [p.get("name") for p in (profile.get("projects") or []) if isinstance(p, dict)],
    }
    lines = [
        "You are an expert technical recruiter. Score how well THIS candidate "
        "fits EACH job below. Be honest and strict: correctly rejecting a bad "
        "fit is more useful than inflating a score. A new grad applying to a 5+ "
        "year role is a skip. Senior, staff, lead, or manager titles are a skip "
        "for an early-career candidate.",
        "",
        "Return ONLY a JSON array, one object per job, in this exact shape, and "
        "nothing else:",
        '[{"id": "<the id shown>", "score": 0-100, "tier": "strong|possible|skip", '
        '"reasons": ["short", "short"], "matched_skills": ["..."], "missing_skills": ["..."]}]',
        "",
        "Keep the id exactly as shown for each job. Do not use em dashes in your output.",
        "",
        "CANDIDATE PROFILE:",
        json.dumps(slim, indent=2),
        "",
        "JOBS TO SCORE:",
        "",
    ]
    for j in jobs:
        lines.append(f"--- id: {j['id']}")
        lines.append(f"Title: {j['title']}")
        lines.append(f"Company: {j['company']}")
        lines.append(f"Location: {j['location']}")
        if j["description"]:
            lines.append(f"Description: {j['description']}")
        lines.append("")
    return "\n".join(lines)


def _extract_json_array(text):
    """Pull the first JSON array out of arbitrary pasted text (handles code
    fences and surrounding chatter). Mirrors import_rank.py."""
    import re
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        v = json.loads(text)
        return v if isinstance(v, list) else None
    except Exception:
        pass
    i, j = text.find("["), text.rfind("]")
    if i != -1 and j != -1 and j > i:
        try:
            v = json.loads(text[i:j + 1])
            return v if isinstance(v, list) else None
        except Exception:
            return None
    return None


def _clean_str_list(v, max_items, max_len):
    """Coerce arbitrary pasted input into a safe list of short strings."""
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if not isinstance(x, (str, int, float)):
            continue
        s = str(x).strip()
        if s:
            out.append(s[:max_len])
        if len(out) >= max_items:
            break
    return out


def _sanitize_fit(r):
    """Validate one ranking object from the user's AI. Returns a clean fit dict
    or None if it isn't usable. This is untrusted input, so every field is
    bounded: tier whitelisted, score clamped to 0-100, lists capped."""
    if not isinstance(r, dict):
        return None
    tier = str(r.get("tier", "")).strip().lower()
    if tier not in _VALID_TIERS:
        return None
    try:
        score_val = int(round(float(r.get("score", 0))))
    except (TypeError, ValueError):
        score_val = 0
    score_val = max(0, min(100, score_val))
    return {
        "tier": tier,
        "score": score_val,
        "reasons": _clean_str_list(r.get("reasons"), 8, 300),
        "matched_skills": _clean_str_list(r.get("matched_skills"), 30, 60),
        "missing_skills": _clean_str_list(r.get("missing_skills"), 30, 60),
    }


@app.get("/api/rank/byo")
def rank_export(request: Request, n: int = RANK_EXPORT_MAX):
    """Return a ready-to-paste prompt (and the jobs it covers) so the current
    user can rank their top jobs with their own AI for free. `n` lets the user
    pick how many to send (clamped to a sane range). Needs a profile."""
    user_id = request.state.user_id
    profile = _user_profile(user_id)
    if not profile:
        return {"ok": False, "error": "Build a profile first, then you can rank your matches."}
    try:
        n = max(5, min(int(n), RANK_EXPORT_MAX))
    except Exception:
        n = RANK_EXPORT_MAX
    jobs = _rank_candidates(user_id, profile, n)
    if not jobs:
        return {"ok": False, "error": "No new jobs to rank right now. Your top matches are already verified."}
    return {"ok": True, "count": len(jobs),
            "prompt": _build_rank_prompt(profile, jobs),
            "jobs": [{"id": j["id"], "company": j["company"],
                      "title": j["title"], "location": j["location"]} for j in jobs]}


@app.post("/api/rank/byo")
async def rank_import(request: Request):
    """Accept the JSON array the user's AI returned, validate it, and store the
    rankings as THEIRS (ranked_by 'ai_byoai'). Free, so it is open to visitors;
    every field is sanitized because this is untrusted input. The stored
    rankings then overlay this user's feed automatically."""
    if not db.has_db():
        return {"ok": False, "error": "Saved rankings need the database, which is off right now."}
    user_id = request.state.user_id
    profile = _user_profile(user_id)
    if not profile:
        return {"ok": False, "error": "Build a profile first, then you can rank your matches."}
    try:
        body = await request.json()
    except Exception:
        body = None
    # Accept either a raw array, a {"text": "...pasted..."} blob, or a JSON string.
    rankings = None
    if isinstance(body, list):
        rankings = body
    elif isinstance(body, dict) and isinstance(body.get("text"), str):
        rankings = _extract_json_array(body["text"])
    elif isinstance(body, str):
        rankings = _extract_json_array(body)
    if not isinstance(rankings, list) or not rankings:
        return {"ok": False, "error": "Couldn't find the JSON list your AI returned. Paste the whole thing."}

    # Map the user's top candidates by id so we can attach company/title/location
    # to each stored ranking (and reject ids that aren't real jobs for them).
    valid = {j["id"]: j for j in _rank_candidates(user_id, profile, RANK_EXPORT_MAX)}
    saved = 0
    skipped = 0
    try:
        with db.get_conn() as conn:
            if not conn:
                return {"ok": False, "error": "Couldn't reach the database. Try again."}
            db.ensure_user(conn, user_id)
            for r in rankings[:RANK_IMPORT_MAX]:
                jid = str((r or {}).get("id", "")).strip() if isinstance(r, dict) else ""
                job = valid.get(jid)
                fit = _sanitize_fit(r)
                if not job or not fit:
                    skipped += 1
                    continue
                db.save_ranking(conn, user_id, {
                    "id": jid, "company": job["company"],
                    "title": job["title"], "location": job["location"],
                    "source": "byoai",
                }, fit, ranked_by="ai_byoai")
                saved += 1
    except Exception:
        if saved == 0:
            return {"ok": False, "error": "Couldn't save those rankings. Try again."}
    if saved == 0:
        return {"ok": False, "error": "None of those rankings matched your current jobs. Re-copy the prompt and try again."}
    return {"ok": True, "saved": saved, "skipped": skipped}
