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
    user_id: str = "me"


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


RANKED_FILE = os.environ.get("RANKED_FILE", "ranked_jobs.json")


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
    if not os.path.exists(RANKED_FILE):
        return []
    with open(RANKED_FILE) as f:
        return [_shape(j) for j in json.load(f)]


def _from_db(user_id, tier):
    conn = db.connect()
    if not conn:
        return None
    try:
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
    finally:
        conn.close()


def _load(user_id="me", tier=None):
    data = _from_db(user_id, tier)
    source = "db"
    if data is None:
        data = _from_file()
        source = "file"
    if tier and tier != "all":
        data = [j for j in data if j["tier"] == tier]
    return data, source


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    _, source = _load()
    return {"status": "ok", "source": source}


@app.get("/api/jobs")
def list_jobs(user_id: str = "me", tier: str = "all"):
    data, source = _load(user_id, tier)
    return {"source": source, "count": len(data), "jobs": data}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user_id: str = "me"):
    data, _ = _load(user_id)
    for j in data:
        if j["id"] == job_id:
            return j
    raise HTTPException(status_code=404, detail="job not found")


def _load_profile(user_id):
    """Load the user's profile from DB or my_profile.json fallback."""
    conn = db.connect()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM profiles WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if row:
                    return row["data"]
        finally:
            conn.close()
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
    profile = _load_profile(inp.user_id)
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
    """Serve the hosted single-page app (the ranked feed). OPEN to everyone:
    viewing is free, only the Refresh action inside it is gated."""
    for path in ("app.html", "viewer.html"):
        if os.path.exists(path):
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="no app UI found (build app.html)")


@app.get("/start")
def start_ui(request: Request):
    """Serve the onboarding page (build your profile). OPEN to view; the actions
    inside it (onboard / save-profile) are gated server-side."""
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
    """Check the access code; on success set the unlock cookie."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    code = (data.get("code") or "").strip()
    if not ACCESS_CODE:
        # gate disabled; treat as already unlocked
        return {"ok": True}
    if code and _secrets.compare_digest(code, ACCESS_CODE):
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            "jr_access", _valid_token(),
            max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
            secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        )
        return resp
    return JSONResponse({"ok": False, "error": "Incorrect code."}, status_code=403)


@app.post("/api/profile")
async def save_profile(request: Request):
    """Save a profile JSON (from the bring-your-own-AI flow) to disk. Owner-only
    so a visitor can't overwrite the shared profile."""
    _require_unlock(request)
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Body was not valid JSON."}
    if not isinstance(data, dict):
        return {"ok": False, "error": "Expected a single JSON object."}
    path = os.environ.get("PROFILE_PATH", "my_profile.json")
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        return {"ok": False, "error": f"Could not write profile: {e}"}
    return {"ok": True, "saved": path, "name": data.get("name")}


@app.post("/api/onboard")
async def onboard_resume(request: Request, file: UploadFile = File(...)):
    """Accept a resume upload, parse it into a profile, save it. Owner-only
    (parsing spends the API budget and overwrites the shared profile)."""
    _require_unlock(request)
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
        return {"ok": True, "saved": path, "name": profile.get("name")}
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
