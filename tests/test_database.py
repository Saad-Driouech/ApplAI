"""Tests for src/database.py"""
import hashlib
import tempfile
from pathlib import Path

import pytest

from src.database import (
    _compute_dedup_key,
    create_application,
    expire_stale_jobs,
    get_conn,
    get_jobs_by_status,
    get_recent_skipped,
    init_db,
    job_exists,
    record_feedback_event,
    update_score,
    update_status,
    upsert_job,
)


def _db():
    tmp = tempfile.mktemp(suffix=".db")
    conn = init_db(Path(tmp))
    return conn, Path(tmp)


def _sample_job(source="indeed", ext_id="j001"):
    job_id = hashlib.sha256(f"{source}:{ext_id}".encode()).hexdigest()
    return {
        "id": job_id,
        "external_id": ext_id,
        "source": source,
        "title": "ML Engineer",
        "company": "Acme GmbH",
        "city": "Berlin",
        "country": "DE",
        "salary_info": "80k-100k EUR",
        "source_url": "https://indeed.com/viewjob?jk=j001",
        "description": "Build ML pipelines.",
        "posted_at": "2026-03-01",
    }


class TestUpsertJob:
    def test_insert_new_job(self):
        conn, _ = _db()
        job = _sample_job()
        assert upsert_job(conn, job) is True

    def test_dedup_returns_false(self):
        conn, _ = _db()
        job = _sample_job()
        upsert_job(conn, job)
        assert upsert_job(conn, job) is False

    def test_job_exists(self):
        conn, _ = _db()
        job = _sample_job()
        upsert_job(conn, job)
        assert job_exists(conn, "indeed", "j001") is True
        assert job_exists(conn, "indeed", "notexist") is False


class TestScoring:
    def test_update_score(self):
        conn, _ = _db()
        job = _sample_job()
        upsert_job(conn, job)
        update_score(conn, job["id"], 8.5, "Great fit", "queued")
        rows = get_jobs_by_status(conn, "queued")
        assert len(rows) == 1
        assert rows[0]["score"] == pytest.approx(8.5)

    def test_skip_below_threshold(self):
        conn, _ = _db()
        job = _sample_job()
        upsert_job(conn, job)
        update_score(conn, job["id"], 3.0, "Poor fit", "skipped")
        assert get_jobs_by_status(conn, "skipped")[0]["score"] == pytest.approx(3.0)
        assert get_jobs_by_status(conn, "queued") == []


class TestApplications:
    def test_create_application(self):
        import uuid
        conn, _ = _db()
        job = _sample_job()
        upsert_job(conn, job)
        app_id = str(uuid.uuid4())
        create_application(conn, {
            "id": app_id,
            "job_id": job["id"],
            "cv_path": "/tmp/cv.pdf",
            "cover_letter_path": "/tmp/cl.docx",
        })
        row = conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        assert row is not None
        assert row["cv_path"] == "/tmp/cv.pdf"


class TestDedupKey:
    def test_basic_normalization(self):
        assert _compute_dedup_key("OpenAI", "Senior ML Engineer") == "openai|senior ml engineer"

    def test_gender_suffix_stripped(self):
        base = _compute_dedup_key("Acme", "ML Engineer")
        assert _compute_dedup_key("Acme", "ML Engineer (m/f/d)") == base
        assert _compute_dedup_key("Acme", "ML Engineer (w/m/d)") == base
        assert _compute_dedup_key("Acme", "ML Engineer (f/m/d)") == base
        assert _compute_dedup_key("Acme", "ML Engineer [all genders]") == base

    def test_case_insensitive(self):
        assert _compute_dedup_key("GOOGLE", "DATA SCIENTIST") == _compute_dedup_key("google", "data scientist")

    def test_punctuation_stripped(self):
        assert _compute_dedup_key("Meta, Inc.", "AI/ML Engineer") == _compute_dedup_key("Meta Inc", "AI ML Engineer")

    def test_different_companies_differ(self):
        assert _compute_dedup_key("OpenAI", "ML Engineer") != _compute_dedup_key("DeepMind", "ML Engineer")


class TestCrossSourceDedup:
    def test_same_job_different_source_is_duplicate(self):
        conn, path = _db()
        job1 = _sample_job(source="remotive", ext_id="r-001")
        job1["title"] = "Senior ML Engineer"
        job1["company"] = "OpenAI"

        job2_id = hashlib.sha256("remoteok:rok-001".encode()).hexdigest()
        job2 = {**job1, "id": job2_id, "external_id": "rok-001", "source": "remoteok",
                "source_url": "https://remoteok.com/job/1"}

        upsert_job(conn, job1)
        result = upsert_job(conn, job2)

        assert result is False
        row = conn.execute("SELECT status, skip_reason FROM jobs WHERE id = ?", (job2_id,)).fetchone()
        assert row["status"] == "skipped"
        assert row["skip_reason"] == "duplicate"

    def test_duplicate_chain_prevented(self):
        """A duplicate of a duplicate should not block a new real job."""
        conn, path = _db()
        # Insert a duplicate record directly
        dup_id = hashlib.sha256("src1:ext1".encode()).hexdigest()
        conn.execute(
            """INSERT INTO jobs (id, external_id, source, title, company, country,
               source_url, scraped_at, status, skip_reason, dedup_key)
               VALUES (?, 'ext1', 'src1', 'ML Eng', 'Acme', 'DE',
               'https://example.com', datetime('now'), 'skipped', 'duplicate', 'acme|ml eng')""",
            (dup_id,),
        )
        conn.commit()

        # A new real job with same key should still insert as 'new' (not blocked by the duplicate)
        new_id = hashlib.sha256("src2:ext2".encode()).hexdigest()
        new_job = {
            "id": new_id, "external_id": "ext2", "source": "src2",
            "title": "ML Eng", "company": "Acme", "country": "DE",
            "source_url": "https://example2.com",
        }
        result = upsert_job(conn, new_job)
        assert result is True
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (new_id,)).fetchone()
        assert row["status"] == "new"

    def test_truly_different_jobs_not_deduped(self):
        conn, _ = _db()
        job1 = _sample_job(source="remotive", ext_id="r-001")
        job1["title"] = "ML Engineer"
        job1["company"] = "OpenAI"

        job2_id = hashlib.sha256("remoteok:rok-002".encode()).hexdigest()
        job2 = {**job1, "id": job2_id, "external_id": "rok-002", "source": "remoteok",
                "title": "Data Scientist", "company": "DeepMind",
                "source_url": "https://remoteok.com/job/2"}

        upsert_job(conn, job1)
        result = upsert_job(conn, job2)
        assert result is True


class TestExpireStaleJobs:
    def test_expires_old_new_and_queued_only(self):
        conn, _ = _db()
        jobs = {name: _sample_job(ext_id=name) for name in ("old_new", "old_queued", "old_submitted", "fresh_new")}
        for name, job in jobs.items():
            job["title"] = name  # distinct dedup keys
            upsert_job(conn, job)
        update_status(conn, jobs["old_queued"]["id"], "queued")
        update_status(conn, jobs["old_submitted"]["id"], "submitted")
        for name in ("old_new", "old_queued", "old_submitted"):
            conn.execute("UPDATE jobs SET scraped_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (jobs[name]["id"],))

        assert expire_stale_jobs(conn, max_age_days=30) == 2
        status = {r["title"]: (r["status"], r["skip_reason"]) for r in conn.execute("SELECT * FROM jobs")}
        assert status["old_new"] == ("skipped", "expired")
        assert status["old_queued"] == ("skipped", "expired")
        assert status["old_submitted"] == ("submitted", None)
        assert status["fresh_new"] == ("new", None)


class TestMigrations:
    def test_v4_requeues_jobs_wrongly_scored_zero_by_parse_errors(self):
        conn, path = _db()
        victim, real_low = _sample_job(ext_id="a"), _sample_job(ext_id="b")
        real_low["title"] = "Other role"
        upsert_job(conn, victim)
        upsert_job(conn, real_low)
        update_score(conn, victim["id"], 0.0, "Parse error — see raw_response", "skipped")
        update_score(conn, real_low["id"], 2.0, "Parse error — see raw_response", "skipped")
        # Pretend the DB is still at schema v3
        conn.execute("DELETE FROM schema_meta WHERE version >= 4")
        conn.commit()

        conn = init_db(path)
        rows = {r["id"]: r for r in conn.execute("SELECT id, status, score FROM jobs")}
        assert rows[victim["id"]]["status"] == "new" and rows[victim["id"]]["score"] is None
        # A score the old regex actually recovered is a real (low) score — left alone
        assert rows[real_low["id"]]["status"] == "skipped"


class TestTextMigration:
    def test_v5_decodes_stored_text_and_recomputes_dedup_key(self):
        conn, path = _db()
        job = _sample_job()
        job.update(company="AT&amp;T Labs", title="R&amp;D Engineer",
                   description="Youâ\u0080\u0099ll build models")
        upsert_job(conn, job)
        conn.execute("DELETE FROM schema_meta WHERE version >= 5")
        conn.commit()

        conn = init_db(path)
        row = conn.execute("SELECT company, title, description, dedup_key FROM jobs").fetchone()
        assert row["company"] == "AT&T Labs"
        assert row["title"] == "R&D Engineer"
        assert row["description"] == "You’ll build models"
        assert row["dedup_key"] == _compute_dedup_key("AT&T Labs", "R&D Engineer")


class TestFeedback:
    def test_record_and_retrieve_feedback_event(self):
        import uuid
        conn, path = _db()
        job = _sample_job()
        upsert_job(conn, job)
        record_feedback_event(conn, job["id"], "rescue")
        row = conn.execute(
            "SELECT event_type FROM feedback_events WHERE job_id = ?", (job["id"],)
        ).fetchone()
        assert row["event_type"] == "rescue"

    def test_get_recent_skipped(self):
        conn, path = _db()
        job = _sample_job()
        upsert_job(conn, job)
        update_score(conn, job["id"], 3.0, "Low fit", "skipped", skip_reason="low_score")
        rows = get_recent_skipped(conn, days=7)
        assert len(rows) == 1
        assert rows[0]["skip_reason"] == "low_score"

    def test_get_recent_skipped_excludes_old(self):
        conn, path = _db()
        job = _sample_job()
        upsert_job(conn, job)
        # Manually set scraped_at to 30 days ago
        conn.execute("UPDATE jobs SET scraped_at = datetime('now', '-30 days') WHERE id = ?", (job["id"],))
        update_score(conn, job["id"], 3.0, "Old", "skipped", skip_reason="low_score")
        rows = get_recent_skipped(conn, days=7)
        assert len(rows) == 0

    def test_skip_reason_persisted(self):
        conn, _ = _db()
        job = _sample_job()
        upsert_job(conn, job)
        update_score(conn, job["id"], 0.0, "No AI keywords", "skipped", skip_reason="keyword_filter")
        row = conn.execute("SELECT skip_reason FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        assert row["skip_reason"] == "keyword_filter"
