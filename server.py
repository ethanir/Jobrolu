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
from urllib.parse import quote
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
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


def _run_refresh(profile_path, top_n=None, draft=True):
    def progress(stage, pct, detail):
        with _refresh_lock:
            _refresh.update(stage=stage, pct=pct, detail=detail)
    try:
        with _refresh_lock:
            _refresh.update(running=True, stage="Starting", pct=0, detail="",
                            started_at=time.time(), finished_at=None,
                            result=None, error=None)
        summary = pipeline.run(profile_path, progress=progress, top_n=top_n, draft=draft)
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
# Compress responses over ~1KB. The feed payload is the big one (hundreds of KB
# to ~1MB of JSON); gzip typically shrinks it 5-7x, which is the single biggest
# win for how fast the feed arrives, with no change to behavior.
app.add_middleware(GZipMiddleware, minimum_size=1024)
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

# ---------------------------------------------------------------- billing config
# A single one-time lifetime upgrade, sold through a Stripe Payment Link (no-code,
# created in the Stripe dashboard). The server makes NO outbound calls to Stripe:
# it only appends the account id to the link so the webhook can map the payment
# back, and verifies the signed webhook Stripe sends after a successful payment.
# PLAN_PRO_NAME is the display name; change it in one place to rename the tier.
STRIPE_PAYMENT_LINK = os.environ.get("STRIPE_PAYMENT_LINK", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
PRO_PRICE_LABEL = os.environ.get("PRO_PRICE_LABEL", "$1.99").strip()
PLAN_PRO_NAME = os.environ.get("PLAN_PRO_NAME", "Pro").strip()
_WEBHOOK_TOLERANCE = 300   # seconds; reject webhooks whose timestamp is older (replay guard)


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


# ---------------------------------------------------------------- plan / pro gating
def _user_plan(user_id) -> str:
    """This user's plan ('free' or 'pro') from the database. Free when there is no
    database (local dev) or on any read error."""
    if not db.has_db():
        return "free"
    try:
        with db.get_conn() as conn:
            if conn:
                return db.get_plan(conn, user_id) or "free"
    except Exception:
        pass
    return "free"


def _is_pro(request) -> bool:
    """Whether this request may use the paid AI features. The owner (a valid unlock
    cookie, or the configured OWNER_USER_ID) always qualifies; otherwise the user's
    stored plan must be 'pro'."""
    if _is_unlocked(request):
        return True
    uid = getattr(request.state, "user_id", None)
    if OWNER_USER_ID and uid == OWNER_USER_ID:
        return True
    return _user_plan(uid) == "pro"


def _pro_gate(request):
    """Return an upgrade response dict if this request is NOT entitled to a paid
    feature, else None. Billing only applies when a database is configured, so
    local/dev (no db) is never gated."""
    if not db.has_db():
        return None
    if _is_pro(request):
        return None
    return {"ok": False, "upgrade": True,
            "error": ("This is a " + PLAN_PRO_NAME + " feature. Unlock it for life for "
                      + PRO_PRICE_LABEL + ".")}


# Best-effort per-user rate limit for the now-free resume parser. In-memory and
# per-process: it resets on restart and is not shared across workers, which is
# fine, because its only job is to stop one client scripting a flood of parses
# (each costs a fraction of a cent), not to be a hard security control.
_ONBOARD_HITS = {}                                   # uid -> [unix timestamps]
_ONBOARD_LOCK = threading.Lock()
_ONBOARD_MAX = int(os.environ.get("ONBOARD_DAILY_LIMIT", "10") or 0)
_ONBOARD_WINDOW = 24 * 3600                          # rolling window, seconds


def _onboard_rate_limited(uid):
    """True if uid has used up its resume-parse allowance in the rolling window
    (and the request should be refused). Records the attempt when it allows it.
    A missing uid or a non-positive limit disables the check entirely."""
    if not uid or _ONBOARD_MAX <= 0:
        return False
    now = time.time()
    with _ONBOARD_LOCK:
        hits = [t for t in _ONBOARD_HITS.get(uid, []) if now - t < _ONBOARD_WINDOW]
        if len(hits) >= _ONBOARD_MAX:
            _ONBOARD_HITS[uid] = hits
            return True
        hits.append(now)
        _ONBOARD_HITS[uid] = hits
        return False


def _verify_stripe_event(payload: bytes, sig_header: str, secret: str, tolerance: int = _WEBHOOK_TOLERANCE):
    """Verify a Stripe webhook signature WITHOUT the stripe library, following the
    documented scheme: signed_payload = '<t>.' + raw_body, HMAC-SHA256 keyed on the
    whsec signing secret, hex-encoded, constant-time compared against the v1
    signature(s) in the Stripe-Signature header, with a replay window on the
    timestamp. Returns the parsed event dict, or None if anything is off.

    The payload MUST be the exact raw request bytes; a re-serialized body breaks
    the signature."""
    if not payload or not sig_header or not secret:
        return None
    ts = None
    v1s = []
    for part in sig_header.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "t":
            ts = v
        elif k == "v1":
            v1s.append(v)
    if not ts or not v1s:
        return None
    try:
        if tolerance and abs(time.time() - int(ts)) > tolerance:
            return None
    except Exception:
        return None
    signed = ts.encode("utf-8") + b"." + payload
    expected = _hmac.new(secret.encode("utf-8"), signed, _hashlib.sha256).hexdigest()
    if not any(_secrets.compare_digest(expected, s) for s in v1s):
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return None


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
    raises, so it can never break the refresh that produced the data.

    Safety: if a scan comes back with dramatically fewer jobs than are already
    stored (a transient sourcing failure, say), keep the bigger stored pool rather
    than overwriting it with a thin one. New jobs only ever add; a bad day cannot
    empty the feed."""
    if not db.has_db():
        return
    try:
        if not os.path.exists(RANKED_FILE):
            return
        with open(RANKED_FILE) as f:
            jobs = json.load(f)
        with db.get_conn() as conn:
            if conn:
                existing = 0
                try:
                    meta = db.pool_meta(conn)
                    existing = meta[0] if meta else 0
                except Exception:
                    existing = 0
                if existing > 200 and len(jobs) < existing * 0.4:
                    return                  # suspiciously thin pull, keep what we have
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
BASE_KEEP = 2000                      # most jobs the feed keeps after ranking
_BASE_CACHE = {}                      # profile-hash -> {"token","jobs"}
_BASE_LOCK = threading.Lock()
_BASE_MAX_PROFILES = 12               # bound memory: keep recent profiles only

# Per-profile locks that serialize the heavy scoring. The point is that if several
# requests for the same profile arrive before the first finishes (a user mashing
# refresh, or two tabs), only ONE scores the pool; the others wait and then read
# the cached result. Bounded so the dict cannot grow without limit: dropping an
# old lock can at worst cause one extra recompute, never a wrong result, because
# the real guard is the cache re-check under _BASE_LOCK.
_COMPUTE_LOCKS = {}
_COMPUTE_LOCKS_GUARD = threading.Lock()
_COMPUTE_LOCKS_MAX = 64


def _compute_lock(key):
    with _COMPUTE_LOCKS_GUARD:
        lk = _COMPUTE_LOCKS.get(key)
        if lk is None:
            if len(_COMPUTE_LOCKS) >= _COMPUTE_LOCKS_MAX:
                _COMPUTE_LOCKS.pop(next(iter(_COMPUTE_LOCKS)), None)
            lk = threading.Lock()
            _COMPUTE_LOCKS[key] = lk
        return lk


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
    token = _pool_token()
    key = _profile_hash(profile)
    with _BASE_LOCK:
        hit = _BASE_CACHE.get(key)
        if hit and hit["token"] == token:
            return hit["jobs"]
    # Serialize the heavy scoring per profile so refreshing can never pile up:
    # only the first request computes, the rest wait here and return its result.
    with _compute_lock(key):
        with _BASE_LOCK:                    # re-check: someone may have just done it
            hit = _BASE_CACHE.get(key)
            if hit and hit["token"] == token:
                return hit["jobs"]
        return _compute_scored_base(profile, token, key)


def _compute_scored_base(profile, token, key):
    """Heavy path, run only while holding the per-profile compute lock: score the
    whole pool for this profile, keep the top BASE_KEEP, cache them in memory, and
    return them. Runs at most once per profile per pool version."""
    import score
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
            "description": (j.get("description", "") or "")[:700],
            "is_new": j.get("is_new", False),
            "date_posted": j.get("date_posted"),
            "_score": j.get("_score", 0),
            "_matched": j.get("_matched", []),
            "_why": j.get("_why", []),
            "_flags": j.get("_flags", []),
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
    pts = job.get("date_posted")
    posted_ts = int(pts) if isinstance(pts, (int, float)) and pts else None
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
        "status": job.get("status", "") or "",
        "posted_ts": posted_ts,
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
    statusmap = {}
    if db.has_db():
        try:
            with db.get_conn() as conn:
                if conn:
                    overlay = db.get_rankings_map(conn, user_id)
                    # Status is independent of rankings: a status failure (e.g. an
                    # older DB before this table existed) must never discard the
                    # rankings overlay, so it gets its own guard.
                    try:
                        statusmap = db.get_status_map(conn, user_id)
                    except Exception:
                        statusmap = {}
        except Exception:
            overlay = {}
            statusmap = {}
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
            "date_posted": j.get("date_posted"),
            "status": statusmap.get(jid, ""),
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


# Human labels for the job systems we pull from (for the public coverage page).
_SYSTEM_LABELS = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters", "recruitee": "Recruitee",
    "workable": "Workable", "adzuna": "Adzuna", "usajobs": "USAJOBS",
    "workday": "Workday", "icims": "iCIMS",
}


@app.get("/api/coverage")
def coverage():
    """Self-measured coverage stats from Jobrolu's OWN live pool and registry.

    Every number here is real and grows as we add jobs and companies; nothing
    claims to know the total size of any outside job market, because that number
    is genuinely unknowable. Roles are classified into fields by the same engine
    that ranks them, so a single role can count toward more than one field (an ML
    engineer is both software and data); field counts therefore do not sum to the
    role total, and the page presents them as relative volume, not a share."""
    import score
    pool = _pool_list()  # READ-ONLY shared pool (DB-first, file fallback)
    companies = set()
    by_field = {}
    by_system = {}
    for j in pool:
        title = j.get("title", "") or ""
        comp = (j.get("company", "") or "").strip().lower()
        if comp:
            companies.add(comp)
        src = (j.get("source") or j.get("ats") or "").strip().lower()
        if src:
            by_system[src] = by_system.get(src, 0) + 1
        discs = score.role_disciplines(title)
        if discs:
            for d in discs:
                by_field[d] = by_field.get(d, 0) + 1
        else:
            by_field["other"] = by_field.get("other", 0) + 1
    # Companies we actively track (registry), which can exceed those with live
    # openings right now. Guarded so a registry hiccup never breaks the page.
    tracked = 0
    try:
        import registry
        reg = registry.load()
        tracked = len(reg) if isinstance(reg, (dict, list)) else 0
    except Exception:
        tracked = 0
    fields = [{
        "key": k,
        "label": ("other roles" if k == "other"
                  else score._DISC_NAMES.get(k, k.replace("_", " ").title())),
        "count": v,
    } for k, v in by_field.items()]
    fields.sort(key=lambda x: -x["count"])
    systems = [{"key": s, "label": _SYSTEM_LABELS.get(s, s.title()),
                "count": by_system[s]} for s in by_system]
    systems.sort(key=lambda x: -x["count"])
    return {
        "live_roles": len(pool),
        "companies_live": len(companies),
        "companies_tracked": tracked,
        "fields": fields,
        "systems": systems,
        "updated": int(time.time()),
    }


_STATS_CACHE = {"data": None, "at": 0.0}
_STATS_LOCK = threading.Lock()
_STATS_TTL = 30.0          # seconds; the underlying numbers change at most daily


@app.get("/api/stats")
def stats():
    """Tiny public counters for the live tracker on the landing page: total roles
    tracked and companies in the registry. Deliberately cheap, it uses pool_meta (a
    stored count, not a scan of the whole pool) plus a short cache, so it stays fast
    even when hit on every page load and refreshed periodically by the counter."""
    now = time.time()
    with _STATS_LOCK:
        c = _STATS_CACHE
        if c["data"] is not None and (now - c["at"]) < _STATS_TTL:
            return c["data"]
    roles = 0
    try:
        if db.has_db():
            with db.get_conn() as conn:
                meta = db.pool_meta(conn) if conn else None
                if meta:
                    roles = int(meta[0] or 0)
    except Exception:
        roles = 0
    if not roles:                      # file-backed fallback (no DB configured)
        try:
            roles = len(_pool_list())
        except Exception:
            roles = 0
    companies = 0
    try:
        import registry
        reg = registry.load()
        companies = len(reg) if isinstance(reg, (dict, list)) else 0
    except Exception:
        companies = 0
    data = {"roles": roles, "companies": companies, "updated": int(now)}
    with _STATS_LOCK:
        _STATS_CACHE["data"] = data
        _STATS_CACHE["at"] = now
    return data


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


_VALID_STATUSES = {"applied", "interviewing", "rejected", "none"}


@app.post("/api/jobs/{job_id}/status")
async def set_job_status(request: Request, job_id: str):
    """Set or clear this account's application status for a job: 'applied',
    'interviewing', 'rejected', or 'none' to clear. Free and per-user; it only
    affects the signer's own feed."""
    if db.has_db():
        _require_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = (body.get("status") or "none").strip().lower()
    if status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    out = "" if status == "none" else status
    # Local/dev has no accounts, so there is nothing to persist; report success
    # so the UI behaves identically without a database.
    if not db.has_db():
        return {"ok": True, "status": out}
    try:
        with db.get_conn() as conn:
            if conn:
                db.set_status(conn, request.state.user_id, job_id, status)
    except Exception:
        raise HTTPException(status_code=500, detail="could not save status")
    return {"ok": True, "status": out}


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


# ----------------------------------------------------- automatic free job scan
# Everyone benefits from fresh jobs without anyone clicking Refresh, so on a
# fixed schedule the server runs the FREE part of the pipeline (pull the newest
# postings + keyword-score them) with top_n=0 and draft off, so it never spends
# the API budget. The paid AI fit-rank stays on the manual, code-gated Refresh.
# The countdown the UI shows is derived from the pool's last-updated time, which
# every refresh (manual or scheduled) bumps, so the timer resets after each pull.
SCAN_INTERVAL_HOURS = float(os.environ.get("SCAN_INTERVAL_HOURS", "24"))
AUTO_SCAN = os.environ.get("AUTO_SCAN", "1") != "0"


def _scan_profile_path():
    """The profile the shared pool is shaped against, same resolution as refresh."""
    for p in (os.environ.get("PROFILE_PATH", "my_profile.json"),
              "my_profile.json", "profile.example.json"):
        if p and os.path.exists(p):
            return p
    return "profile.example.json"


def _last_scan_epoch():
    """Epoch seconds of the last pool refresh: the Postgres pool's updated_at when
    a DB is configured, else the committed file's mtime. None if neither exists."""
    if db.has_db():
        try:
            with db.get_conn() as conn:
                if conn:
                    meta = db.pool_meta(conn)
                    if meta and meta[1] is not None:
                        return meta[1].timestamp()
        except Exception:
            pass
    try:
        if os.path.exists(RANKED_FILE):
            return os.path.getmtime(RANKED_FILE)
    except Exception:
        pass
    return None


def _auto_scan_loop():
    """Background loop: when the pool is older than the interval and no refresh is
    already running, run the free pull. Polls once a minute so it survives restarts
    (it reads the persisted last-scan time) and never double-runs with a manual
    refresh (it respects the same running flag)."""
    interval = SCAN_INTERVAL_HOURS * 3600
    time.sleep(20)   # let the app finish starting before any heavy pull
    while True:
        try:
            last = _last_scan_epoch()
            due = (last is None) or (time.time() >= last + interval)
            with _refresh_lock:
                busy = _refresh["running"]
            if due and not busy:
                _run_refresh(_scan_profile_path(), top_n=0, draft=False)
        except Exception:
            pass
        time.sleep(60)


@app.get("/api/scan/schedule")
def scan_schedule():
    """Public: when the last free scan ran and when the next one is due, so the
    feed can show a countdown to everyone. next_scan is null when auto-scan is off
    (e.g. no database), in which case the UI shows 'updated X ago' instead."""
    last = _last_scan_epoch()
    with _refresh_lock:
        running = _refresh["running"]
    auto = bool(AUTO_SCAN and db.has_db())
    nxt = (last + SCAN_INTERVAL_HOURS * 3600) if (last is not None and auto) else None
    return {"last_scan": last, "next_scan": nxt,
            "interval_hours": SCAN_INTERVAL_HOURS, "auto": auto, "running": running}


@app.on_event("startup")
def _start_auto_scan():
    """Spin up the scheduler only on a real, DB-backed deployment. Tests use a
    bare TestClient (no lifespan) and have no DB, so this never runs there."""
    if db.has_db() and AUTO_SCAN:
        threading.Thread(target=_auto_scan_loop, daemon=True).start()


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


@app.get("/privacy")
def privacy_page():
    """Serve the privacy policy (public)."""
    if os.path.exists("privacy.html"):
        return FileResponse("privacy.html")
    raise HTTPException(status_code=404, detail="no privacy.html found")


@app.get("/terms")
def terms_page():
    """Serve the terms of service (public)."""
    if os.path.exists("terms.html"):
        return FileResponse("terms.html")
    raise HTTPException(status_code=404, detail="no terms.html found")


@app.get("/coverage")
def coverage_page():
    """Serve the live coverage page (public)."""
    if os.path.exists("coverage.html"):
        return FileResponse("coverage.html")
    raise HTTPException(status_code=404, detail="no coverage.html found")


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
    pro = _is_pro(request)
    return {"signed_in": True, "email": email,
            "is_pro": pro, "plan": ("pro" if pro else "free"),
            "plan_name": PLAN_PRO_NAME, "price": PRO_PRICE_LABEL}


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
    uid = request.state.browser_id      # adopt THIS browser's anonymous identity
    try:
        with db.get_conn() as conn:
            if not conn:
                return JSONResponse({"ok": False, "error": "Accounts are unavailable right now."}, status_code=503)
            db.ensure_user(conn, uid)
            # ...but only if it is not already an account. If this browser already
            # holds an account, a second sign-up must NOT reuse its id, or it would
            # overwrite the first account and inherit its profile. Give the new
            # account a fresh identity instead.
            existing = db.get_user(conn, uid)
            if existing and existing.get("email"):
                uid = uuid.uuid4().hex
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
    """Fully sign out on this browser: clear the account session, the owner unlock
    cookie, and the per-browser id. Clearing jr_access matters because otherwise a
    browser that had unlocked as owner would still read as Pro (via _is_unlocked)
    for a freshly created account, and clearing jr_uid prevents an anonymous visit
    from resolving back to an adopted OWNER_USER_ID. Re-entering the access code
    re-unlocks owner powers."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie("jr_access", path="/")
    resp.delete_cookie(JR_UID_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------- billing
@app.get("/api/billing/status")
def billing_status(request: Request):
    """What the UI needs to render the plan badge and the upgrade flow: the current
    plan, whether billing is configured, and (only for a signed-in free user) the
    Stripe checkout URL with their account id attached so the webhook can map the
    payment back to them. The account id (a uuid hex) is a valid client_reference_id."""
    uid = request.state.user_id
    pro = _is_pro(request)
    signed_in = _signed_in(request)
    configured = bool(STRIPE_PAYMENT_LINK)
    checkout_url = ""
    if configured and signed_in and not pro:
        sep = "&" if "?" in STRIPE_PAYMENT_LINK else "?"
        checkout_url = STRIPE_PAYMENT_LINK + sep + "client_reference_id=" + quote(uid, safe="")
        try:
            with db.get_conn() as conn:
                if conn:
                    u = db.get_user(conn, uid)
                    em = (u or {}).get("email")
                    if em:
                        checkout_url += "&prefilled_email=" + quote(em, safe="")
        except Exception:
            pass
    return {"ok": True, "plan": ("pro" if pro else "free"), "is_pro": pro,
            "plan_name": PLAN_PRO_NAME, "price": PRO_PRICE_LABEL,
            "configured": configured, "signed_in": signed_in,
            "checkout_url": checkout_url}


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request):
    """Stripe calls this after a successful payment. We verify the signature, then
    grant the buyer the lifetime plan. The webhook is the SOURCE OF TRUTH for who
    has paid; the browser redirect alone is never trusted. Idempotent: a duplicate
    or retried event for the same Checkout Session is a no-op.

    Status codes matter to Stripe: 400 on a bad signature, 5xx on a transient error
    (so Stripe retries), 200 once handled (so it stops)."""
    raw = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not STRIPE_WEBHOOK_SECRET:
        # Not configured yet: 200 so Stripe does not retry forever during setup.
        return JSONResponse({"ok": False, "error": "billing not configured"}, status_code=200)
    event = _verify_stripe_event(raw, sig, STRIPE_WEBHOOK_SECRET)
    if event is None:
        return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=400)
    etype = event.get("type")
    if etype not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return {"ok": True, "ignored": etype}
    session = (event.get("data") or {}).get("object") or {}
    if session.get("payment_status") not in ("paid", "no_payment_required"):
        return {"ok": True, "pending": True}
    session_id = (session.get("id") or "").strip()
    cref = (session.get("client_reference_id") or "").strip()
    email = (((session.get("customer_details") or {}).get("email"))
             or session.get("customer_email") or "").strip()
    if not db.has_db():
        return JSONResponse({"ok": False, "error": "db unavailable"}, status_code=500)
    try:
        with db.get_conn() as conn:
            if not conn:
                return JSONResponse({"ok": False, "error": "db unavailable"}, status_code=500)
            if session_id and db.was_session_processed(conn, session_id):
                return {"ok": True, "already": True}
            granted = False
            if cref:
                db.ensure_user(conn, cref)
                granted = db.set_plan(conn, cref, "pro", session_id or None)
            if not granted and email:
                granted = bool(db.set_plan_by_email(conn, email, "pro", session_id or None))
            return {"ok": True, "granted": granted}
    except Exception:
        return JSONResponse({"ok": False, "error": "could not record payment"}, status_code=500)


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
    # Prefill ONLY from the user's own saved profile. A signed-in account with no
    # profile of its own gets a blank form (it must never see the owner's profile).
    # The my_profile.json fallback is for pure local dev (no database) only.
    if prof is None and not db.has_db():
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
    """Accept a resume upload and parse it into a profile for review. This is a
    free feature: the parse is a single small, cheap model call. It still requires
    a signed-in account (when a database is configured) and is rate-limited per
    user, best effort, to prevent abuse. The parsed result is returned for review
    and is NOT committed to the feed here; that happens on POST /api/profile."""
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
    # Free, but rate-limited per user (best effort) so nobody can script a flood
    # of parses. Only real attempts count (we are past the file-type check), and
    # only when a database is configured; local dev is never limited.
    if db.has_db() and _onboard_rate_limited(getattr(request.state, "user_id", None)):
        return {"ok": False, "error": ("You've reached today's resume-upload limit. "
                "Fill in your profile manually below, or try again tomorrow.")}
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
RANK_EXPORT_MAX = 150         # most jobs we ask a web chat to score at once
RANK_IMPORT_MAX = 300         # hard cap on rankings accepted per import call
RANK_DESC_CHARS = 1200        # chars of each posting handed to the AI to read
RANK_ENRICH_CAP = 60          # max thin-desc jobs to backfill per rank (when enabled)
_VALID_TIERS = ("strong", "possible", "skip")

# Depth presets for the bring-your-own-AI rank. The pasted prompt has a roughly
# fixed budget (jobs x chars-per-posting); each preset spends it differently:
# 'broad' covers the most roles with a quick read of each, 'deep' reads fewer
# postings in full. 'broad' equals the long-standing default, so it is a no-op
# for anyone who does not change the setting. Both numbers stay clamped to the
# export/desc maxima so a preset can never exceed the global ceilings.
RANK_DEPTH_PRESETS = {
    "broad":    (RANK_EXPORT_MAX, RANK_DESC_CHARS),   # 150 jobs, ~1200 chars each
    "balanced": (80, 2400),                           # 80 jobs, ~2400 chars each
    "deep":     (40, 4000),                           # 40 jobs, full posting each
}


def _trim_words(text, n):
    """Cut text to at most n chars on a word boundary, adding a trailing ellipsis
    when it was actually shortened, so a posting that hits the cap never ends
    mid-word."""
    text = text or ""
    if len(text) <= n:
        return text
    cut = text[:n]
    sp = cut.rfind(" ")
    if sp > int(n * 0.6):          # back off to the last space if it is reasonably near the end
        cut = cut[:sp]
    return cut.rstrip() + " ..."


def _rank_candidates(user_id, profile, limit, desc_chars=RANK_DESC_CHARS):
    """The visitor's top-N jobs to hand their AI: highest free score first,
    skipping ones they have already had verified, each keyed by the SAME id the
    feed uses (db.job_hash) so the AI's reply maps straight back. Uses the cached
    scored base for ORDER, but pulls each job's FULL description from the pool (the
    feed cache trims descriptions for display) so the AI judges the complete posting,
    not a snippet. `desc_chars` caps how much of each posting is included, so a
    'deep' rank can hand over far more of each description than a 'broad' one."""
    try:
        desc_chars = max(200, min(int(desc_chars), 8000))
    except Exception:
        desc_chars = RANK_DESC_CHARS
    base = _scored_base(profile)
    if not base:
        return []
    # id -> full description, straight from the pool (read-only; no copy).
    full = {}
    try:
        for j in _pool_list():
            full[db.job_hash(j)] = (j.get("description") or "")
    except Exception:
        full = {}
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
        desc = full.get(j["id"], j.get("description", "") or "")
        out.append({
            "id": j["id"],
            "company": j["company"],
            "title": j["title"],
            "location": j["location"],
            "source": j.get("source", ""),
            "url": j.get("url", ""),
            "description": _trim_words(desc, desc_chars),
        })
        if len(out) >= limit:
            break
    # Optional: fetch full text for the handful of top jobs whose board omits it,
    # so the AI has the complete posting for them too. Off by default; enable with
    # ENRICH_DESCRIPTIONS=1 once the live detail endpoints are smoke-tested.
    if os.environ.get("ENRICH_DESCRIPTIONS") == "1":
        try:
            import enrich_desc
            enrich_desc.backfill(out, cap=RANK_ENRICH_CAP)
            for c in out:
                c["description"] = _trim_words(c.get("description", ""), desc_chars)
        except Exception:
            pass
    return out


def _candidate_brief(profile):
    """One honest line describing the candidate's level + authorization, so the AI
    scores against THIS person (a new grad, a senior, whoever) rather than a fixed
    'early-career' assumption. Mirrors score.desired_level."""
    try:
        import score as _score
        level = _score.desired_level(profile)
    except Exception:
        level = "mid"
    titles = ", ".join(str(t) for t in (profile.get("target_titles") or [])) or "software roles"
    yrs = profile.get("years_experience")
    phrase = {"intern": "an internship candidate",
              "entry": "an early-career / new-grad candidate",
              "mid": "a mid-level candidate",
              "senior": "a senior candidate"}.get(level, "a mid-level candidate")
    bits = [f"This candidate is {phrase}"]
    if yrs not in (None, ""):
        bits.append(f"with about {yrs} years of experience")
    bits.append(f"targeting: {titles}.")
    line = " ".join(bits)
    if profile.get("requires_sponsorship"):
        line += " They require visa sponsorship, so a role that explicitly does not sponsor is a skip."
    return line, level


def _slim_experience(experience):
    """Serialize the candidate's work history for the ranking prompt: title,
    company, dates, and a short summary when present. For an experienced
    candidate this is the strongest signal there is (domain, real seniority,
    accomplishments), so we surface it the same way we surface projects. It is
    absent for most early-career profiles, in which case it is simply omitted and
    the years-of-experience number plus skills still drive the score."""
    out = []
    for e in (experience or []):
        if isinstance(e, str):
            s = e.strip()
            if s:
                out.append({"title": s})
            continue
        if not isinstance(e, dict):
            continue
        entry = {}
        for src, dst in (("title", "title"), ("role", "title"), ("position", "title"),
                         ("company", "company"), ("employer", "company"),
                         ("dates", "dates"), ("duration", "dates")):
            v = e.get(src)
            if isinstance(v, str) and v.strip() and dst not in entry:
                entry[dst] = v.strip()
        summary = e.get("summary") or e.get("description")
        if isinstance(summary, str) and summary.strip():
            entry["summary"] = summary.strip()[:300]
        if entry:
            out.append(entry)
    return out


def _slim_projects(projects):
    """Serialize the candidate's projects for the ranking prompt. A bare name
    tells the AI nothing, so we include each project's tech stack and a short
    summary when they exist (the resume parser fills these). This both grounds
    the ranking in real, demonstrated skills and keeps the stack-to-project link
    available for later tailored outreach. Projects with only a name still pass
    the name through; anything richer carries its detail."""
    out = []
    for p in (projects or []):
        if isinstance(p, str):
            name = p.strip()
            if name:
                out.append({"name": name})
            continue
        if not isinstance(p, dict):
            continue
        entry = {}
        name = p.get("name")
        if isinstance(name, str) and name.strip():
            entry["name"] = name.strip()
        stack = p.get("stack") or p.get("skills") or p.get("technologies")
        if isinstance(stack, list):
            cleaned = [str(s).strip() for s in stack if str(s).strip()]
            if cleaned:
                entry["stack"] = cleaned[:12]
        summary = p.get("summary") or p.get("description")
        if isinstance(summary, str) and summary.strip():
            entry["summary"] = summary.strip()[:240]
        if entry:
            out.append(entry)
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
        "experience": _slim_experience(profile.get("experience")),
        "projects": _slim_projects(profile.get("projects")),
    }
    brief, level = _candidate_brief(profile)
    # The seniority rule is written to match THIS candidate, not a fixed persona.
    if level in ("entry", "intern"):
        seniority_rule = ("A role needing 5+ years, or a senior/staff/lead/principal/"
                          "manager title, is a skip for this candidate.")
    elif level == "senior":
        seniority_rule = ("A junior, new-grad, or internship role is a poor fit (the "
                          "candidate is over-qualified); score it low.")
    else:
        seniority_rule = ("A senior/staff/lead/principal title is usually a stretch, "
                          "and an internship is a poor fit; score those low.")
    lines = [
        "You are an expert technical recruiter. Score how well THIS candidate "
        "fits EACH job below. Be honest and strict: correctly rejecting a bad "
        "fit is more useful than inflating a score.",
        brief,
        seniority_rule,
        "Judge on: title/role match, how many required skills the candidate has, "
        "seniority fit, and location vs their preferences. If a posting has little "
        "or no description, score from the title and say the detail was limited.",
        "A role in a different discipline than the candidate targets (for example a "
        "data science, data analytics, product management, design, or hardware role "
        "for a software engineer, or vice versa) is a poor fit and should score low, "
        "even when some skills overlap.",
        "",
        "Return ONLY a JSON array, one object per job, in this exact shape, and "
        "nothing else:",
        '[{"id": "<the id shown>", "score": 0-100, "tier": "strong|possible|skip", '
        '"reasons": ["one short reason"], "matched_skills": ["up to 3"], "missing_skills": ["up to 3"]}]',
        "",
        "Keep it compact so you can finish every job: ONE short reason and at most "
        "3 skills each. Score every job in the list, in order. Keep the id exactly "
        "as shown. Do not use em dashes in your output.",
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
    # A clear close: a long final description can hit the per-job cap, so without
    # this the prompt would end mid-posting. The output instruction is repeated
    # here so it is the last thing the AI reads on a long prompt.
    lines.append(f"--- end of {len(jobs)} jobs ---")
    lines.append("")
    lines.append("Now return ONLY the JSON array described above: one object per "
                 "job, in the same order, and nothing else.")
    return "\n".join(lines)


def _extract_json_array(text):
    """Pull the ranking objects out of arbitrary pasted text. Tolerant by
    design: it handles code fences, chatter before/after the array, AND a reply
    that got cut off partway through, which is a very common outcome for a long
    list. We walk the text and collect every COMPLETE {...} object, tracking
    string state so braces inside strings never miscount, and ignore a trailing
    half-written object. A truncated paste therefore still saves everything that
    made it through, which is exactly what the modal promises."""
    import re
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    # Fast path: a clean, complete array (or object) parses directly.
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            return [v]
    except Exception:
        pass
    # Tolerant path: collect complete top-level objects one at a time. This
    # survives a missing closing bracket and a cut-off final object.
    start = text.find("[")
    if start == -1:
        start = 0
    objs = []
    depth = 0          # brace depth of the object currently being read
    obj_start = -1
    in_str = False
    escape = False
    for k in range(start, len(text)):
        c = text[k]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                obj_start = k
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start != -1:
                    try:
                        o = json.loads(text[obj_start:k + 1])
                        if isinstance(o, dict):
                            objs.append(o)
                    except Exception:
                        pass
                    obj_start = -1
    return objs or None


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
def rank_export(request: Request, n: int = RANK_EXPORT_MAX, depth: Optional[str] = None):
    """Return a ready-to-paste prompt (and the jobs it covers) so the current
    user can rank their top jobs with their own AI. A Pro feature (gated below);
    the ranking itself costs Jobrolu nothing because it runs on the user's own
    AI. `depth` picks a breadth/detail preset (broad|balanced|deep) and the UI
    always sends it; when it is absent a raw `n` count is honored (clamped).
    Needs a profile."""
    user_id = request.state.user_id
    gate = _pro_gate(request)
    if gate:
        return gate
    profile = _user_profile(user_id)
    if not profile:
        return {"ok": False, "error": "Build a profile first, then you can rank your matches."}
    preset = RANK_DEPTH_PRESETS.get((depth or "").strip().lower())
    if preset:
        n, desc_chars = preset
    else:
        try:
            n = max(5, min(int(n), RANK_EXPORT_MAX))
        except Exception:
            n = RANK_EXPORT_MAX
        desc_chars = RANK_DESC_CHARS
    jobs = _rank_candidates(user_id, profile, n, desc_chars=desc_chars)
    if not jobs:
        return {"ok": False, "error": "No new jobs to rank right now. Your top matches are already verified."}
    return {"ok": True, "count": len(jobs),
            "prompt": _build_rank_prompt(profile, jobs),
            "jobs": [{"id": j["id"], "company": j["company"],
                      "title": j["title"], "location": j["location"]} for j in jobs]}


@app.post("/api/rank/byo")
async def rank_import(request: Request):
    """Accept the JSON array the user's AI returned, validate it, and store the
    rankings as THEIRS (ranked_by 'ai_byoai'). A Pro feature (gated below); every
    field is sanitized because this is untrusted input. The stored rankings then
    overlay this user's feed automatically."""
    if not db.has_db():
        return {"ok": False, "error": "Saved rankings need the database, which is off right now."}
    user_id = request.state.user_id
    gate = _pro_gate(request)
    if gate:
        return gate
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
        # Tell apart "these already saved" (a refresh mid-save, or a double
        # submit of the same reply) from a genuinely unrecognized paste, so the
        # user is never told they did something wrong when their rankings in fact
        # already landed.
        already = {}
        try:
            with db.get_conn() as conn:
                if conn:
                    already = db.get_rankings_map(conn, user_id)
        except Exception:
            already = {}
        hits = 0
        for r in rankings[:RANK_IMPORT_MAX]:
            jid = str((r or {}).get("id", "")).strip() if isinstance(r, dict) else ""
            if jid and jid in already:
                hits += 1
        if hits:
            return {"ok": True, "saved": 0, "already": hits,
                    "message": "Those were already saved. Loaded your next batch."}
        return {"ok": False, "error": "None of those rankings matched your current jobs. Re-copy the prompt and try again."}
    return {"ok": True, "saved": saved, "skipped": skipped}
