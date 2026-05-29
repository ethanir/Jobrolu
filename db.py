"""
db.py - Postgres layer for per-user Jobrolu.

Designed to coexist with the file fallback that server.py already uses:
get_conn() yields None when DATABASE_URL is unset or the driver is missing,
so every consumer degrades safely. Tables are created on first successful
connect, so the moment Postgres is wired up on Railway, per-user mode is on
and the feed will not 500 because tables are missing (that was the prior bug).

Connections are served from a pooled, self-healing context manager (get_conn).
The pool is purely an optimization layer: if it cannot hand back a healthy
connection for any reason, get_conn falls back to a fresh direct connection,
so this is never worse than opening one connection per request, and never
leaks a connection (it is always returned to the pool or closed). This is what
keeps per-user traffic from exhausting Railway's connection limit.

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
  has_db, connect, get_conn, job_hash, fetch_ranked
Plus the per-user helpers:
  ensure_user, get_user, get_profile, save_profile,
  get_ranking, get_rankings_map, save_ranking,
  get_usage, increment_usage

NOTE on job ids: job_hash here is identical to jobcache.job_id (same
company|title|location normalization). The two MUST stay in lockstep, because
the pipeline keys cache lookups by `_id` (jobcache.job_id) and Phase 3 reads
them back from this table. save_ranking therefore prefers the job's own `_id`
so the round-trip is exact.
"""
import hashlib
import json
import os
import secrets
import threading
from contextlib import contextmanager
from typing import Optional

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json
    _HAS_DRIVER = True
except Exception:
    _HAS_DRIVER = False

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_schema_ready = False

# Pooling state. The pool is created lazily on first use and guarded by a lock.
_pool = None
_pool_failed = False
_pool_lock = threading.Lock()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT        PRIMARY KEY,
    email         TEXT,
    password_hash TEXT,
    is_owner      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Migrate older deployments whose users table predates accounts (must run
-- before the email index below, which references these columns).
ALTER TABLE users ADD COLUMN IF NOT EXISTS email         TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS users_email_idx ON users (LOWER(email)) WHERE email IS NOT NULL;

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

CREATE TABLE IF NOT EXISTS pool (
    id          INTEGER     PRIMARY KEY DEFAULT 1,
    data        TEXT        NOT NULL,
    n           INTEGER     NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pool_single_row CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS job_status (
    user_id     TEXT        NOT NULL,
    job_id      TEXT        NOT NULL,
    status      TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, job_id)
);

CREATE TABLE IF NOT EXISTS saved_jobs (
    user_id      TEXT        NOT NULL,
    job_id       TEXT        NOT NULL,
    company      TEXT,
    title        TEXT,
    location     TEXT,
    description  TEXT,
    url          TEXT,
    source       TEXT,
    date_posted  BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, job_id)
);

CREATE TABLE IF NOT EXISTS ext_tokens (
    token_hash   TEXT        PRIMARY KEY,
    user_id      TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ext_tokens_user_idx ON ext_tokens (user_id);
"""


def has_db() -> bool:
    """True if DATABASE_URL is set and the driver is installed.
    Cheap, does not actually attempt to open a connection."""
    return bool(DATABASE_URL) and _HAS_DRIVER


def _ensure_schema(conn) -> None:
    """Create the schema once per process (IF NOT EXISTS, so it is idempotent).
    Never raises: a failed create leaves _schema_ready False so we retry on the
    next call rather than crash the request."""
    global _schema_ready
    if _schema_ready:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
        _schema_ready = True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def connect():
    """Open a single direct Postgres connection, or None if Postgres is not
    configured. The caller is responsible for conn.close(). Kept for callers
    that manage their own connection lifecycle; server.py uses get_conn()."""
    if not has_db():
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception:
        return None
    _ensure_schema(conn)
    return conn


def _get_pool():
    """Lazily build the connection pool. Returns the pool, or None if pooling is
    unavailable (in which case get_conn falls back to direct connections)."""
    global _pool, _pool_failed
    if _pool is not None or _pool_failed:
        return _pool
    if not has_db():
        return None
    try:
        from psycopg2 import pool as _pgpool
        mx = max(2, int(os.environ.get("DB_POOL_MAX", "10")))
        _pool = _pgpool.ThreadedConnectionPool(
            1, mx, dsn=DATABASE_URL, cursor_factory=RealDictCursor,
        )
    except Exception:
        _pool = None
        _pool_failed = True
    return _pool


@contextmanager
def get_conn():
    """Yield a healthy Postgres connection (schema ensured), or None if Postgres
    is unavailable. Always returns the connection to the pool (or closes a direct
    one) on exit, and rolls back any open transaction first so a pooled
    connection is never reused mid-transaction.

    A pooled connection that has died (Postgres restart, idle timeout) is
    detected by a cheap SELECT 1 probe and discarded; if the pool cannot give a
    live connection, we open a fresh direct one. So the worst case equals the
    old behaviour (a connection per request), never worse, and never a leak.
    """
    if not has_db():
        yield None
        return

    with _pool_lock:
        pool = _get_pool()

    conn = None
    from_pool = False

    if pool is not None:
        try:
            conn = pool.getconn()
            from_pool = True
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                conn.rollback()
            except Exception:
                # dead connection: drop it and fall through to a fresh one
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = None
                from_pool = False
        except Exception:
            # pool exhausted or errored: fall back to a direct connection
            conn = None
            from_pool = False

    if conn is None:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            from_pool = False
        except Exception:
            yield None
            return

    try:
        _ensure_schema(conn)
        yield conn
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        if from_pool and pool is not None:
            try:
                pool.putconn(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
        else:
            try:
                conn.close()
            except Exception:
                pass


def job_hash(job) -> str:
    """Stable id for a job posting: company + title + location, each lowercased
    and whitespace-stripped, so cosmetic differences collide on the same id.

    This MUST stay byte-for-byte identical to jobcache.job_id, or per-user cache
    lookups (Phase 3) will miss and silently re-charge the LLM for the same job
    every run."""
    raw = "|".join(str(job.get(k, "") or "").lower().strip()
                   for k in ("company", "title", "location"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- users
def ensure_user(conn, user_id: str, is_owner: bool = False) -> None:
    """Create the user row if missing; otherwise refresh last_seen_at. is_owner
    is sticky and only ever promoted (a normal page load with is_owner=False can
    never demote an already-owner row, and an unlock with is_owner=True promotes
    and stays)."""
    if not conn or not user_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, is_owner) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  last_seen_at = NOW(), "
            "  is_owner = users.is_owner OR EXCLUDED.is_owner",
            (user_id, is_owner),
        )
    conn.commit()


def get_user(conn, user_id: str) -> Optional[dict]:
    if not conn or not user_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


# ---------------------------------------------------------------- accounts
# Real email + password accounts, stored on the existing users row. Passwords
# are hashed with PBKDF2-HMAC-SHA256 (stdlib, no extra dependency) using a random
# per-user salt, and verified in constant time. An account reuses the browser's
# id, so any profile built before signing up is preserved.
import base64 as _base64
import hmac as _hmac

_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return _base64.b64encode(salt).decode() + "$" + _base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, hash_b64 = (stored or "").split("$", 1)
        salt = _base64.b64decode(salt_b64)
        expected = _base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
        return _hmac.compare_digest(dk, expected)
    except Exception:
        return False


def get_account_by_email(conn, email: str) -> Optional[dict]:
    """Return the user row for this email (case-insensitive), or None."""
    if not conn or not email:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(%s)", (email,))
        return cur.fetchone()


def set_account(conn, user_id: str, email: str, password_hash: str) -> bool:
    """Attach an email + password to the given user row (creating it if needed).
    Returns False if the email is already taken by a different user."""
    if not conn or not user_id or not email or not password_hash:
        return False
    existing = get_account_by_email(conn, email)
    if existing and existing.get("id") != user_id:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  email = EXCLUDED.email, password_hash = EXCLUDED.password_hash, "
            "  last_seen_at = NOW()",
            (user_id, email, password_hash),
        )
    conn.commit()
    return True


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
    `ranked_by` is one of 'ai_paid', 'ai_byoai', 'heuristic'.

    The job key prefers the pipeline's own id (`id` or `_id`) and only computes
    job_hash as a last resort, so the value stored here matches the value the
    pipeline uses to look the ranking back up (jobcache.job_id). Get this wrong
    and the cache silently misses for that job on every run."""
    if not conn or not user_id or not fit:
        return
    jid = job.get("id") or job.get("_id") or job_hash(job)
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


# ---------------------------------------------------------------- application status
def get_status_map(conn, user_id: str) -> dict:
    """Bulk-load every application status for a user as {job_id: status}, so the
    feed can overlay them on thousands of jobs in one query. Statuses are one of
    'applied', 'interviewing', 'rejected'."""
    if not conn or not user_id:
        return {}
    out = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, status FROM job_status WHERE user_id = %s",
            (user_id,),
        )
        for r in cur.fetchall():
            out[r["job_id"]] = r["status"]
    return out


def set_status(conn, user_id: str, job_id: str, status: str) -> None:
    """Set or clear one job's application status for a user. An empty status (or
    'none') clears it by deleting the row, so the default 'no status' state never
    leaves stray rows behind."""
    if not conn or not user_id or not job_id:
        return
    if not status or status == "none":
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM job_status WHERE user_id = %s AND job_id = %s",
                (user_id, job_id),
            )
        conn.commit()
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job_status (user_id, job_id, status) VALUES (%s,%s,%s) "
            "ON CONFLICT (user_id, job_id) DO UPDATE SET "
            "  status = EXCLUDED.status, updated_at = NOW()",
            (user_id, job_id, status),
        )
    conn.commit()


# ---------------------------------------------------------------- saved jobs
def get_saved_jobs(conn, user_id: str) -> list:
    """Every job this user added by hand (paste) or via the extension, newest
    first. These live alongside the sourced pool and are merged into the user's
    feed, so a pasted LinkedIn or Handshake role is ranked with the same brain
    and the same profile context as everything else."""
    if not conn or not user_id:
        return []
    out = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, company, title, location, description, url, source, "
            "date_posted FROM saved_jobs WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
        for r in cur.fetchall():
            out.append({
                "id": r["job_id"],
                "company": r["company"] or "",
                "title": r["title"] or "",
                "location": r["location"] or "",
                "description": r["description"] or "",
                "url": r["url"] or "",
                "source": r["source"] or "saved",
                "date_posted": r["date_posted"],
                "saved": True,
            })
    return out


def save_saved_job(conn, user_id: str, job: dict) -> str:
    """Upsert one user-added job, keyed by a stable id (the caller's explicit id
    when present, e.g. a LinkedIn job id from the extension, else
    company+title+location), so re-adding or re-viewing the same posting never
    creates a duplicate or wastes a rank. A re-add with an empty description keeps
    the fuller one already stored. Returns the job_id used."""
    if not conn or not user_id:
        return ""
    jid = job.get("id") or job.get("_id") or job_hash(job)
    dp = job.get("date_posted")
    try:
        dp = int(dp) if dp not in (None, "") else None
    except (TypeError, ValueError):
        dp = None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO saved_jobs ("
            "  user_id, job_id, company, title, location, description, url, source, date_posted"
            ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id, job_id) DO UPDATE SET "
            "  company = EXCLUDED.company, title = EXCLUDED.title, "
            "  location = EXCLUDED.location, "
            "  description = COALESCE(NULLIF(EXCLUDED.description, ''), saved_jobs.description), "
            "  url = COALESCE(NULLIF(EXCLUDED.url, ''), saved_jobs.url), "
            "  source = EXCLUDED.source, "
            "  date_posted = COALESCE(EXCLUDED.date_posted, saved_jobs.date_posted)",
            (
                user_id, jid,
                job.get("company"), job.get("title"), job.get("location"),
                job.get("description"), job.get("url"),
                job.get("source") or "saved", dp,
            ),
        )
    conn.commit()
    return jid


def delete_saved_job(conn, user_id: str, job_id: str) -> None:
    """Remove a user-added job and any ranking attached to it, so 'remove' fully
    clears it from the feed."""
    if not conn or not user_id or not job_id:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM saved_jobs WHERE user_id = %s AND job_id = %s",
                    (user_id, job_id))
        cur.execute("DELETE FROM rankings WHERE user_id = %s AND job_id = %s",
                    (user_id, job_id))
    conn.commit()


# ---------------------------------------------------------------- extension tokens
def _hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def create_ext_token(conn, user_id: str) -> str:
    """Issue a fresh connect token for the browser extension, replacing any
    previous one for this user (so re-connecting rotates the key). Only the hash
    is stored; the raw token is returned once for the user to paste into the
    extension and is never recoverable from the database afterward."""
    if not conn or not user_id:
        return ""
    raw = secrets.token_urlsafe(32)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ext_tokens WHERE user_id = %s", (user_id,))
        cur.execute("INSERT INTO ext_tokens (token_hash, user_id) VALUES (%s, %s)",
                    (_hash_token(raw), user_id))
    conn.commit()
    return raw


def user_for_ext_token(conn, raw: str) -> Optional[str]:
    """Resolve a connect token to its user_id (or None), and stamp last_used_at.
    This is how the extension authenticates: it cannot use the site cookie because
    it runs on linkedin.com / handshake, not on jobrolu.com."""
    if not conn or not raw:
        return None
    h = _hash_token(raw)
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM ext_tokens WHERE token_hash = %s", (h,))
        row = cur.fetchone()
        if not row:
            return None
        uid = row["user_id"]
        cur.execute("UPDATE ext_tokens SET last_used_at = NOW() WHERE token_hash = %s", (h,))
    conn.commit()
    return uid


def has_ext_token(conn, user_id: str) -> bool:
    """Whether this user currently has the extension connected."""
    if not conn or not user_id:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ext_tokens WHERE user_id = %s LIMIT 1", (user_id,))
        return cur.fetchone() is not None


def revoke_ext_tokens(conn, user_id: str) -> None:
    """Disconnect the extension by deleting this user's connect token(s)."""
    if not conn or not user_id:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ext_tokens WHERE user_id = %s", (user_id,))
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


# ---------------------------------------------------------------- shared pool
# The shared job pool (the owner's latest Refresh output) is stored here so it
# survives a redeploy, instead of living only on the host's ephemeral disk. It
# is one row holding the same JSON the feed already parses, plus a count and a
# timestamp the read path uses as a cheap cache-freshness check.
def save_pool(conn, jobs: list) -> None:
    """Persist the whole ranked pool as one row (id=1). `jobs` is the parsed
    list exactly as written to ranked_jobs.json."""
    if not conn or jobs is None:
        return
    payload = json.dumps(jobs)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pool (id, data, n, updated_at) VALUES (1, %s, %s, NOW()) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  data = EXCLUDED.data, n = EXCLUDED.n, updated_at = NOW()",
            (payload, len(jobs)),
        )
    conn.commit()


def pool_meta(conn):
    """Cheap freshness probe: (n, updated_at) for the stored pool, or None if
    none stored. Does NOT fetch the large data column, so it is safe to call on
    every feed read to decide whether an in-memory cache is stale."""
    if not conn:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT n, updated_at FROM pool WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return None
    return (row["n"], row["updated_at"])


def load_pool(conn):
    """Return the stored pool as a parsed list, or None if none stored or it
    cannot be parsed. This is the large read; callers cache it."""
    if not conn:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM pool WHERE id = 1")
        row = cur.fetchone()
    if not row or not row.get("data"):
        return None
    try:
        return json.loads(row["data"])
    except Exception:
        return None
