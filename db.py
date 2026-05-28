"""
db.py — Postgres layer for per-user Jobrolu.

Designed to coexist with the file fallback that server.py already uses:
connect() returns None when DATABASE_URL is unset or the driver is missing,
so every consumer degrades safely. Tables are created on first successful
connect, so the moment Postgres is wired up on Railway, per-user mode is on
and the feed will not 500 because tables are missing (that was the prior bug).

Schema (all four tables created lazily, IF NOT EXISTS):
  users      every visitor; browser-id PK; is_owner flags the human who owns
             the deployment and is allowed to spend the API budget.
  profiles   one structured profile per user; column `data` is JSONB so the
             existing server.py _load_profile query `SELECT data FROM profiles`
             keeps working unchanged.
  rankings   per-user, per-job fit. Replaces the single shared jobcache. The
             columns match what server.py's _from_db / _shape already read,
             so the live feed reads identically.
  usage      per-user, per-month counters for the rate and spend limits that
             keep visitor traffic from running away.

Public surface (kept stable for server.py):
  has_db, connect, job_hash, fetch_ranked
Plus new helpers used by the per-user pipeline:
  ensure_user, get_user, get_profile, save_profile,
  get_ranking, get_rankings_map, save_ranking,
  get_usage, increment_usage
"""
import hashlib
import os
from typing import Optional

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json
    _HAS_DRIVER = True
except Exception:
    _HAS_DRIVER = False

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_schema_ready = False


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT        PRIMARY KEY,
    is_owner      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id     TEXT         PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data        JSONB        NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rankings (
    user_id     TEXT        NOT NULL,
    job_id      TEXT        NOT NULL,
    company     TEXT,
    title       TEXT,
    location    TEXT,
    source      TEXT,
    tier        TEXT,
    score       INTEGER,
    reasons     JSONB,
    matched     JSONB,
    missing     JSONB,
    ranked_by   TEXT,
    ranked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, job_id)
);

CREATE INDEX IF NOT EXISTS rankings_user_tier_idx
    ON rankings (user_id, tier);

CREATE TABLE IF NOT EXISTS usage (
    user_id     TEXT    NOT NULL,
    month       TEXT    NOT NULL,
    ai_calls    INTEGER NOT NULL DEFAULT 0,
    paid_cents  INTEGER NOT NULL DEFAULT 0,
    refreshes   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, month)
);
"""


def has_db() -> bool:
    """True if DATABASE_URL is set and the driver is installed.
    Cheap, does not actually attempt to open a connection."""
    return bool(DATABASE_URL) and _HAS_DRIVER


def connect():
    """Open a Postgres connection, or return None if Postgres is not configured.
    The first successful connect creates the schema (IF NOT EXISTS), so setting
    DATABASE_URL on Railway is enough to bring per-user mode online.
    Callers are responsible for conn.close().
    """
    global _schema_ready
    if not has_db():
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception:
        return None
    if not _schema_ready:
        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()
            _schema_ready = True
        except Exception:
            conn.rollback()
            # Leave the flag false; we'll retry on the next call rather than
            # crash the request. The caller will still get a usable conn.
    return conn


def job_hash(job) -> str:
    """Stable id for a job posting: company + title + location, lowercased and
    whitespace-stripped, so cosmetic differences collide on the same id."""
    raw = "|".join(str(job.get(k, "") or "").lower().strip()
                   for k in ("company", "title", "location"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- users
def ensure_user(conn, user_id: str, is_owner: bool = False) -> None:
    """Create the user row if missing; otherwise refresh last_seen_at."""
    if not conn or not user_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, is_owner) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET last_seen_at = NOW()",
            (user_id, is_owner),
        )
    conn.commit()


def get_user(conn, user_id: str) -> Optional[dict]:
    if not conn or not user_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


# ---------------------------------------------------------------- profiles
def get_profile(conn, user_id: str) -> Optional[dict]:
    if not conn or not user_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM profiles WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row["data"] if row else None


def save_profile(conn, user_id: str, profile: dict) -> None:
    if not conn or not user_id or profile is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (user_id, data) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE "
            "  SET data = EXCLUDED.data, updated_at = NOW()",
            (user_id, Json(profile)),
        )
    conn.commit()


# ---------------------------------------------------------------- rankings
def fetch_ranked(conn, user_id: str):
    """Return this user's ranked rows sorted strong, possible, skip then score
    desc. Shape matches what server.py's _from_db / _shape consume."""
    if not conn or not user_id:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id AS id, company, title, location, source, "
            "       tier, score, reasons, matched, missing, ranked_by "
            "FROM rankings WHERE user_id = %s "
            "ORDER BY "
            "  CASE tier WHEN 'strong' THEN 0 "
            "            WHEN 'possible' THEN 1 "
            "            WHEN 'skip' THEN 2 ELSE 3 END, "
            "  score DESC NULLS LAST",
            (user_id,),
        )
        return cur.fetchall()


def get_ranking(conn, user_id: str, job_id: str) -> Optional[dict]:
    if not conn or not user_id or not job_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier, score, reasons, matched, missing, ranked_by "
            "FROM rankings WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
        return cur.fetchone()


def get_rankings_map(conn, user_id: str) -> dict:
    """Bulk-load every ranking for a user as {job_id: fit-dict}, so the pipeline
    can apply the cache to thousands of jobs in one query."""
    if not conn or not user_id:
        return {}
    out = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, tier, score, reasons, matched, missing, ranked_by "
            "FROM rankings WHERE user_id = %s",
            (user_id,),
        )
        for r in cur.fetchall():
            out[r["job_id"]] = {
                "tier": r["tier"],
                "score": r["score"],
                "reasons": r["reasons"] or [],
                "matched_skills": r["matched"] or [],
                "missing_skills": r["missing"] or [],
                "ranked_by": r["ranked_by"],
            }
    return out


def save_ranking(conn, user_id: str, job: dict, fit: dict, ranked_by: str) -> None:
    """Upsert one ranking. `job` carries the listing columns
    (company/title/location); `fit` carries tier/score/reasons/matched/missing.
    `ranked_by` is one of 'ai_paid', 'ai_byoai', 'heuristic'."""
    if not conn or not user_id or not fit:
        return
    jid = job.get("id") or job_hash(job)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rankings ("
            "  user_id, job_id, company, title, location, source, "
            "  tier, score, reasons, matched, missing, ranked_by"
            ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id, job_id) DO UPDATE SET "
            "  company   = EXCLUDED.company, "
            "  title     = EXCLUDED.title, "
            "  location  = EXCLUDED.location, "
            "  source    = EXCLUDED.source, "
            "  tier      = EXCLUDED.tier, "
            "  score     = EXCLUDED.score, "
            "  reasons   = EXCLUDED.reasons, "
            "  matched   = EXCLUDED.matched, "
            "  missing   = EXCLUDED.missing, "
            "  ranked_by = EXCLUDED.ranked_by, "
            "  ranked_at = NOW()",
            (
                user_id, jid,
                job.get("company"), job.get("title"), job.get("location"),
                job.get("source"),
                fit.get("tier"), fit.get("score"),
                Json(fit.get("reasons") or []),
                Json(fit.get("matched_skills") or []),
                Json(fit.get("missing_skills") or []),
                ranked_by,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------- usage
def get_usage(conn, user_id: str, month: str) -> dict:
    """Return this month's counters for the user, zeros if no row exists."""
    if not conn or not user_id or not month:
        return {"ai_calls": 0, "paid_cents": 0, "refreshes": 0}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ai_calls, paid_cents, refreshes FROM usage "
            "WHERE user_id = %s AND month = %s",
            (user_id, month),
        )
        row = cur.fetchone()
    return row or {"ai_calls": 0, "paid_cents": 0, "refreshes": 0}


def increment_usage(conn, user_id: str, month: str,
                    ai_calls: int = 0, paid_cents: int = 0,
                    refreshes: int = 0) -> None:
    if not conn or not user_id or not month:
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO usage (user_id, month, ai_calls, paid_cents, refreshes) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id, month) DO UPDATE SET "
            "  ai_calls   = usage.ai_calls   + EXCLUDED.ai_calls, "
            "  paid_cents = usage.paid_cents + EXCLUDED.paid_cents, "
            "  refreshes  = usage.refreshes  + EXCLUDED.refreshes",
            (user_id, month, ai_calls, paid_cents, refreshes),
        )
    conn.commit()
