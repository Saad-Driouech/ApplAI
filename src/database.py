"""
SQLite database layer for ApplAI.

Schema:
  jobs           — raw scraped + scored job records
  applications   — document generation + user decisions
  scrape_runs    — audit trail of each scraper execution

All queries use parameterised statements (no f-strings in SQL).
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from src.logger import get_logger

log = get_logger(__name__)

# Bump this when the schema changes.
SCHEMA_VERSION = 8

# Failed attempts at the current stage (scoring or generation) before a job is marked 'failed'
MAX_ATTEMPTS = 3

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ── Jobs ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,          -- SHA-256 of (source + external_id)
    external_id     TEXT NOT NULL,             -- board-specific ID / URL hash
    source          TEXT NOT NULL,             -- "indeed", "glassdoor", "stepstone", "bayt"
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    city            TEXT,
    country         TEXT NOT NULL,             -- ISO 3166-1 alpha-2
    salary_info     TEXT,
    source_url      TEXT NOT NULL,
    description     TEXT,
    posted_at       TEXT,                      -- ISO-8601, nullable if board hides it
    scraped_at      TEXT NOT NULL,
    score           REAL,                      -- Gemini score 0-10 (NULL = not yet scored)
    score_reasoning TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
                    -- new | queued | generating | ready | delivered | approved | rejected | skipped | failed
    dedup_key       TEXT,                      -- normalized company+title for cross-source dedup
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_country  ON jobs(country);
CREATE INDEX IF NOT EXISTS idx_jobs_score    ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped  ON jobs(scraped_at DESC);

-- ── Applications ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS applications (
    id              TEXT PRIMARY KEY,          -- UUID
    job_id          TEXT NOT NULL REFERENCES jobs(id),
    cv_path         TEXT,                      -- absolute path to generated PDF
    cover_letter_path TEXT,                    -- absolute path to generated .docx
    generated_at    TEXT,                      -- ISO-8601
    discord_msg_id TEXT,                       -- Discord message ID for the review gate
    user_decision   TEXT,                      -- NULL | "approved" | "rejected"
    decision_at     TEXT,
    notion_page_id  TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_apps_job_id ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_apps_decision ON applications(user_decision);

-- ── Scrape runs ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    jobs_found  INTEGER DEFAULT 0,
    jobs_new    INTEGER DEFAULT 0,
    error       TEXT
);

-- ── Feedback events ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS feedback_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    event_type  TEXT NOT NULL,             -- "rescue", "approve", "reject"
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_job_id ON feedback_events(job_id);
CREATE INDEX IF NOT EXISTS idx_feedback_type   ON feedback_events(event_type);
"""

_MIGRATIONS = {
    2: [
        "ALTER TABLE jobs ADD COLUMN skip_reason TEXT",
    ],
    3: [
        "ALTER TABLE jobs ADD COLUMN dedup_key TEXT",
        "CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key)",
    ],
    4: [
        # Re-queue jobs whose unparseable LLM reply was stored as score 0
        """
        UPDATE jobs
        SET status = 'new', score = NULL, score_reasoning = NULL, skip_reason = NULL
        WHERE status = 'skipped' AND score = 0 AND score_reasoning LIKE 'Parse error%'
        """,
    ],
    5: [
        lambda conn: _migrate_v5_normalize_text(conn),
    ],
    6: [
        "ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN last_error TEXT",
        "ALTER TABLE jobs ADD COLUMN attempted_at TEXT",
    ],
    7: [
        # 'submitted' meant "sent to Discord for review", not applied
        "UPDATE jobs SET status = 'delivered' WHERE status = 'submitted'",
    ],
    8: [
        """
        CREATE TABLE IF NOT EXISTS llm_usage (
            provider  TEXT NOT NULL,
            model     TEXT NOT NULL,
            day       TEXT NOT NULL,           -- provider's quota calendar day
            requests  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (provider, model, day)
        )
        """,
    ],
}

def _migrate_v5_normalize_text(conn: sqlite3.Connection) -> None:
    """Re-normalize stored text (previously HTML-escaped) and recompute dedup keys."""
    from src.utils.sanitize import CONTENT_FIELDS, normalize_content

    updates = []
    rows = conn.execute(
        "SELECT id, title, company, city, description, salary_info FROM jobs"
    ).fetchall()
    for row in rows:
        fixed = {f: normalize_content(row[f], f) if row[f] is not None else None for f in CONTENT_FIELDS}
        if any(fixed[f] != row[f] for f in CONTENT_FIELDS):
            fixed["dedup_key"] = _compute_dedup_key(fixed["company"] or "", fixed["title"] or "")
            fixed["id"] = row["id"]
            updates.append(fixed)

    conn.executemany(
        """
        UPDATE jobs
        SET title = :title, company = :company, city = :city,
            description = :description, salary_info = :salary_info, dedup_key = :dedup_key
        WHERE id = :id
        """,
        updates,
    )
    log.info("Normalized stored text for %d job records", len(updates))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_dedup_key(company: str, title: str) -> str:
    """
    Normalized key for cross-source duplicate detection.

    Lowercases, removes gender suffixes (m/f/d etc.), strips punctuation,
    and collapses whitespace so that the same role posted on two boards
    maps to the same key.
    """
    def _norm(s: str) -> str:
        s = s.lower().strip()
        # Remove European gender suffixes: (m/f/d), (w/m/d), [all genders], etc.
        s = re.sub(
            r'\s*[\(\[]\s*(?:m/f/d|f/m/d|m/w/d|w/m/d|f/m/x|all genders?|diverse)\s*[\)\]]\s*',
            '',
            s,
            flags=re.IGNORECASE,
        )
        # Replace punctuation with spaces
        s = re.sub(r'[^\w\s]', ' ', s)
        # Collapse whitespace
        s = re.sub(r'\s+', ' ', s).strip()
        return s

    return f"{_norm(company)}|{_norm(title)}"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """
    Initialise (or migrate) the database.
    Returns an open connection; the caller owns the connection lifecycle.
    """
    conn = _connect(db_path)
    conn.executescript(_DDL)

    # Determine current version
    current = 0
    try:
        row = conn.execute(
            "SELECT MAX(version) as v FROM schema_meta"
        ).fetchone()
        if row and row["v"]:
            current = row["v"]
    except sqlite3.OperationalError:
        pass  # schema_meta doesn't exist yet

    # Run pending migrations
    for version in sorted(_MIGRATIONS.keys()):
        if version > current:
            for step in _MIGRATIONS[version]:
                if callable(step):
                    step(conn)
                    continue
                try:
                    conn.execute(step)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )
            conn.commit()
            log.info("Applied database migration to version %d", version)

    # Record current schema version if fresh DB
    if current == 0:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _now()),
        )
        conn.commit()
        log.info("Database initialised at schema version %d", SCHEMA_VERSION)

    # Backfill dedup_key for any rows that predate the column
    _backfill_dedup_keys(conn)

    return conn


def _backfill_dedup_keys(conn: sqlite3.Connection) -> None:
    """Populate dedup_key for existing rows that have NULL."""
    rows = conn.execute(
        "SELECT id, company, title FROM jobs WHERE dedup_key IS NULL"
    ).fetchall()
    if not rows:
        return
    for row in rows:
        key = _compute_dedup_key(row["company"] or "", row["title"] or "")
        conn.execute("UPDATE jobs SET dedup_key = ? WHERE id = ?", (key, row["id"]))
    conn.commit()
    log.info("Backfilled dedup_key for %d existing job records", len(rows))


@contextmanager
def get_conn(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Context manager: opens a connection, commits on success, rolls back on error."""
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Job helpers ───────────────────────────────────────────────────────────


def upsert_job(conn: sqlite3.Connection, job: dict[str, Any]) -> bool:
    """
    Insert a new job record.  Returns True if inserted as a new unique job,
    False if it already existed (same source) or is a cross-source duplicate.

    Cross-source deduplication: if a job with the same normalized company+title
    already exists from any other source, this record is inserted as
    status='skipped' / skip_reason='duplicate' so it is never scored again.

    The caller must have sanitized `job` through sanitize.sanitize_job() first.
    """
    job.setdefault("scraped_at", _now())
    job.setdefault("posted_at", None)
    job.setdefault("salary_info", None)
    job.setdefault("description", None)
    job.setdefault("city", None)

    dedup_key = _compute_dedup_key(job.get("company", ""), job.get("title", ""))
    job["dedup_key"] = dedup_key

    # Check for a cross-source duplicate: same company+title, different record,
    # not itself a duplicate (to avoid chaining).
    existing = conn.execute(
        """
        SELECT id FROM jobs
        WHERE dedup_key = ?
          AND id != ?
          AND (skip_reason IS NULL OR skip_reason != 'duplicate')
        LIMIT 1
        """,
        (dedup_key, job["id"]),
    ).fetchone()

    if existing:
        # Insert as a skipped duplicate so the audit trail is preserved
        cursor = conn.execute(
            """
            INSERT INTO jobs
                (id, external_id, source, title, company, city, country,
                 salary_info, source_url, description, posted_at, scraped_at,
                 status, skip_reason, dedup_key)
            VALUES
                (:id, :external_id, :source, :title, :company, :city, :country,
                 :salary_info, :source_url, :description, :posted_at, :scraped_at,
                 'skipped', 'duplicate', :dedup_key)
            ON CONFLICT(source, external_id) DO NOTHING
            """,
            job,
        )
        if cursor.rowcount > 0:
            log.debug(
                "Cross-source duplicate skipped: %s @ %s (matches job %s)",
                job.get("title"), job.get("company"), existing["id"],
            )
        return False

    cursor = conn.execute(
        """
        INSERT INTO jobs
            (id, external_id, source, title, company, city, country,
             salary_info, source_url, description, posted_at, scraped_at,
             status, dedup_key)
        VALUES
            (:id, :external_id, :source, :title, :company, :city, :country,
             :salary_info, :source_url, :description, :posted_at, :scraped_at,
             'new', :dedup_key)
        ON CONFLICT(source, external_id) DO NOTHING
        """,
        job,
    )
    return cursor.rowcount > 0


def update_score(
    conn: sqlite3.Connection,
    job_id: str,
    score: float,
    reasoning: str,
    new_status: str = "scored",
    skip_reason: Optional[str] = None,
) -> None:
    # A completed score starts the next stage with a clean attempt count
    conn.execute(
        """
        UPDATE jobs
        SET score = ?, score_reasoning = ?, status = ?, skip_reason = ?,
            attempts = 0, last_error = NULL
        WHERE id = ?
        """,
        (score, reasoning, new_status, skip_reason, job_id),
    )


def start_generation(conn: sqlite3.Connection, job_id: str) -> bool:
    """Atomically claim a queued job for generation. Returns False if it is no longer queued."""
    cursor = conn.execute(
        "UPDATE jobs SET status = 'generating', attempted_at = ? WHERE id = ? AND status = 'queued'",
        (_now(), job_id),
    )
    return cursor.rowcount > 0


def complete_stage(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    """Move a job to its next status with a clean attempt count."""
    conn.execute(
        "UPDATE jobs SET status = ?, attempts = 0, last_error = NULL WHERE id = ?",
        (status, job_id),
    )


def record_failure(
    conn: sqlite3.Connection,
    job_id: str,
    retry_status: str,
    error: str,
    max_attempts: int = MAX_ATTEMPTS,
) -> bool:
    """Count a failed attempt; mark the job 'failed' once out of attempts. Returns True if it gave up."""
    conn.execute(
        """
        UPDATE jobs
        SET attempts = attempts + 1,
            last_error = ?,
            status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE ? END
        WHERE id = ?
        """,
        (error[:1000], max_attempts, retry_status, job_id),
    )
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row is not None and row["status"] == "failed"


def recover_interrupted_generation(
    conn: sqlite3.Connection,
    older_than_minutes: int = 30,
    max_attempts: int = MAX_ATTEMPTS,
) -> int:
    """Count jobs stuck in 'generating' (crashed run) as a failed attempt. Returns the count."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
    stuck = conn.execute(
        "SELECT id FROM jobs WHERE status = 'generating' AND (attempted_at IS NULL OR attempted_at < ?)",
        (cutoff,),
    ).fetchall()
    for row in stuck:
        record_failure(conn, row["id"], "queued", "Generation interrupted (process stopped)", max_attempts)
    return len(stuck)


def update_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))


def get_jobs_by_status(
    conn: sqlite3.Connection,
    status: str,
    limit: Optional[int] = 100,
) -> list[sqlite3.Row]:
    """Best score first, then newest. limit=None returns all."""
    return conn.execute(
        "SELECT * FROM jobs WHERE status = ? ORDER BY score DESC, scraped_at DESC LIMIT ?",
        (status, -1 if limit is None else limit),
    ).fetchall()


def expire_stale_jobs(conn: sqlite3.Connection, max_age_days: int) -> int:
    """Skip 'new'/'queued' jobs scraped more than max_age_days ago. Returns the count."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    cursor = conn.execute(
        """
        UPDATE jobs SET status = 'skipped', skip_reason = 'expired'
        WHERE status IN ('new', 'queued') AND scraped_at < ?
        """,
        (cutoff,),
    )
    return cursor.rowcount


def job_exists(conn: sqlite3.Connection, source: str, external_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    return row is not None


# ── Application helpers ───────────────────────────────────────────────────


def create_application(conn: sqlite3.Connection, app: dict[str, Any]) -> None:
    sql = """
        INSERT INTO applications
            (id, job_id, cv_path, cover_letter_path, generated_at)
        VALUES
            (:id, :job_id, :cv_path, :cover_letter_path, :generated_at)
    """
    app.setdefault("generated_at", _now())
    conn.execute(sql, app)


def record_user_decision(
    conn: sqlite3.Connection,
    app_id: str,
    decision: str,          # "approved" | "rejected"
    notion_page_id: Optional[str] = None,
) -> bool:
    """Atomically record a decision if none exists. Returns True only for the first caller."""
    cursor = conn.execute(
        """
        UPDATE applications
        SET user_decision = ?, decision_at = ?,
            notion_page_id = COALESCE(?, notion_page_id)
        WHERE id = ? AND user_decision IS NULL
        """,
        (decision, _now(), notion_page_id, app_id),
    )
    return cursor.rowcount > 0


def release_user_decision(conn: sqlite3.Connection, app_id: str) -> None:
    """Undo a claimed decision so the user can retry after a failure."""
    conn.execute(
        "UPDATE applications SET user_decision = NULL, decision_at = NULL WHERE id = ?",
        (app_id,),
    )


# ── Scrape run helpers ────────────────────────────────────────────────────


def start_scrape_run(conn: sqlite3.Connection, source: str) -> int:
    cursor = conn.execute(
        "INSERT INTO scrape_runs (source, started_at) VALUES (?, ?)",
        (source, _now()),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def finish_scrape_run(
    conn: sqlite3.Connection,
    run_id: int,
    jobs_found: int,
    jobs_new: int,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        """
        UPDATE scrape_runs
        SET finished_at = ?, jobs_found = ?, jobs_new = ?, error = ?
        WHERE id = ?
        """,
        (_now(), jobs_found, jobs_new, error, run_id),
    )


# ── LLM usage helpers ─────────────────────────────────────────────────────


def get_llm_usage(conn: sqlite3.Connection, provider: str, model: str, day: str) -> int:
    row = conn.execute(
        "SELECT requests FROM llm_usage WHERE provider = ? AND model = ? AND day = ?",
        (provider, model, day),
    ).fetchone()
    return row["requests"] if row else 0


def increment_llm_usage(conn: sqlite3.Connection, provider: str, model: str, day: str) -> None:
    conn.execute(
        """
        INSERT INTO llm_usage (provider, model, day, requests) VALUES (?, ?, ?, 1)
        ON CONFLICT(provider, model, day) DO UPDATE SET requests = requests + 1
        """,
        (provider, model, day),
    )


# ── Feedback helpers ─────────────────────────────────────────────────────


def record_feedback_event(
    conn: sqlite3.Connection,
    job_id: str,
    event_type: str,
) -> None:
    conn.execute(
        "INSERT INTO feedback_events (job_id, event_type, created_at) VALUES (?, ?, ?)",
        (job_id, event_type, _now()),
    )


def get_recent_skipped(
    conn: sqlite3.Connection,
    days: int = 7,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Recently skipped jobs worth offering for rescue (not duplicates or expired), best score first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return conn.execute(
        """
        SELECT id, title, company, country, score, skip_reason, source_url
        FROM jobs
        WHERE status = 'skipped'
          AND COALESCE(skip_reason, '') NOT IN ('duplicate', 'expired')
          AND scraped_at >= ?
        ORDER BY score DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()


def rescue_job(conn: sqlite3.Connection, job_id: str) -> bool:
    """Atomically move a skipped job to 'queued'. Returns False if it was not skipped."""
    cursor = conn.execute(
        """
        UPDATE jobs SET status = 'queued', skip_reason = NULL, attempts = 0, last_error = NULL
        WHERE id = ? AND status = 'skipped'
        """,
        (job_id,),
    )
    return cursor.rowcount > 0
