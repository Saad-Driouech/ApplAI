"""
SQLite database layer for ApplAI.

Schema:
  jobs           — raw scraped + scored job records
  applications   — document generation + user decisions
  scrape_runs    — audit trail of each scraper execution

All queries use parameterised statements (no f-strings in SQL).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from src.logger import get_logger

log = get_logger(__name__)

# Bump this when the schema changes.
SCHEMA_VERSION = 1

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
                    -- new | scored | queued | generating | ready | submitted | rejected | skipped
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    # Record schema version if not already present
    row = conn.execute(
        "SELECT version FROM schema_meta WHERE version = ?", (SCHEMA_VERSION,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _now()),
        )
        conn.commit()
        log.info("Database initialised at schema version %d", SCHEMA_VERSION)
    else:
        log.debug("Database schema version %d already applied", SCHEMA_VERSION)

    return conn


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
    Insert a new job record.  Returns True if inserted, False if it already existed.

    The caller must have sanitized `job` through sanitize.sanitize_job() first.
    """
    sql = """
        INSERT INTO jobs
            (id, external_id, source, title, company, city, country,
             salary_info, source_url, description, posted_at, scraped_at, status)
        VALUES
            (:id, :external_id, :source, :title, :company, :city, :country,
             :salary_info, :source_url, :description, :posted_at, :scraped_at, 'new')
        ON CONFLICT(source, external_id) DO NOTHING
    """
    job.setdefault("scraped_at", _now())
    job.setdefault("posted_at", None)
    job.setdefault("salary_info", None)
    job.setdefault("description", None)
    job.setdefault("city", None)

    cursor = conn.execute(sql, job)
    return cursor.rowcount > 0


def update_score(
    conn: sqlite3.Connection,
    job_id: str,
    score: float,
    reasoning: str,
    new_status: str = "scored",
) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET score = ?, score_reasoning = ?, status = ?
        WHERE id = ?
        """,
        (score, reasoning, new_status, job_id),
    )


def update_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))


def update_status_direct(db_path, job_id: str, status: str) -> None:
    """Open a connection, update status, commit, close. Convenience for pipeline."""
    with get_conn(db_path) as conn:
        update_status(conn, job_id, status)


def get_jobs_by_status(
    conn: sqlite3.Connection,
    status: str,
    limit: int = 100,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE status = ? ORDER BY score DESC, scraped_at DESC LIMIT ?",
        (status, limit),
    ).fetchall()


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
) -> None:
    conn.execute(
        """
        UPDATE applications
        SET user_decision = ?, decision_at = ?, notion_page_id = ?
        WHERE id = ?
        """,
        (decision, _now(), notion_page_id, app_id),
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
