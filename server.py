"""
FastAPI server — serves the ranked feed to the UI.

Endpoints:
    GET  /api/health                 -> {"status": "ok", "source": "db"|"file"}
    GET  /api/jobs?user_id=&tier=     -> ranked job list (shape the UI expects)
    GET  /api/jobs/{job_id}           -> one job with contacts + draft

Data source resolution:
    1. If DATABASE_URL is set and reachable -> Postgres (db.py).
    2. Otherwise -> read ranked_jobs.json produced by main.py (dev fallback).

Run:
    pip install -r requirements.txt
    uvicorn server:app --reload --port 8000

CORS is open for local dev so the Vite/React UI on another port can call it.
"""
import json
import os
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

import db
import scan as scan_mod
import main as pipeline


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


def _run_refresh(profile_path):
    def progress(stage, pct, detail):
        with _refresh_lock:
            _refresh.update(stage=stage, pct=pct, detail=detail)
    try:
        with _refresh_lock:
            _refresh.update(running=True, stage="Starting", pct=0, detail="",
                            started_at=time.time(), finished_at=None,
                            result=None, error=None)
        summary = pipeline.run(profile_path, progress=progress)
        with _refresh_lock:
            _refresh.update(running=False, stage="Done", pct=100,
                            finished_at=time.time(), result=summary)
    except Exception as e:
        with _refresh_lock:
            _refresh.update(running=False, stage="Error", error=str(e),
                            finished_at=time.time())

app = FastAPI(title="JobMatch API", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

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
def scan_endpoint(inp: ScanInput):
    """Paste-a-JD-or-URL endpoint. Runs fit-rank + contacts + draft on one role."""
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


@app.post("/api/refresh")
def start_refresh(inp: RefreshInput = RefreshInput()):
    """Kick off a background pipeline run. Old jobs are preserved; new postings
    are appended and flagged. Returns immediately; poll /api/refresh/status."""
    with _refresh_lock:
        if _refresh["running"]:
            return {"started": False, "reason": "a refresh is already running"}
    path = inp.profile_path
    if not os.path.exists(path):
        for alt in ("my_profile.json", "profile.example.json"):
            if os.path.exists(alt):
                path = alt
                break
    t = threading.Thread(target=_run_refresh, args=(path,), daemon=True)
    t.start()
    return {"started": True}


@app.get("/api/refresh/status")
def refresh_status():
    """Live progress of the running (or last) refresh, for the UI progress bar."""
    with _refresh_lock:
        return dict(_refresh)


# ---------------------------------------------------------------- web UI
@app.get("/")
def index():
    """Serve the marketing landing page."""
    if os.path.exists("landing.html"):
        return FileResponse("landing.html")
    # fall back to the app if the landing page isn't present
    for path in ("app.html", "viewer.html"):
        if os.path.exists(path):
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="no UI file found (build landing.html / app.html)")


@app.get("/app")
def app_ui():
    """Serve the hosted single-page app (the ranked feed)."""
    for path in ("app.html", "viewer.html"):
        if os.path.exists(path):
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="no app UI found (build app.html)")
