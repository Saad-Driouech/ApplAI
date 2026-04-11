"""Tests for src/feedback/preferences.py"""
import hashlib
import tempfile
import uuid
from pathlib import Path

import pytest

from src.database import (
    get_conn,
    init_db,
    record_feedback_event,
    update_score,
    upsert_job,
    create_application,
    record_user_decision,
)
from src.feedback.preferences import _tokenize_title, build_preference_context, suggest_keyword_additions


def _db():
    tmp = tempfile.mktemp(suffix=".db")
    init_db(Path(tmp))
    return Path(tmp)


def _make_job(db_path, title, company="Acme", country="DE", source="remotive", ext_id=None):
    ext_id = ext_id or title.replace(" ", "-").lower()
    job_id = hashlib.sha256(f"{source}:{ext_id}".encode()).hexdigest()
    job = {
        "id": job_id,
        "external_id": ext_id,
        "source": source,
        "title": title,
        "company": company,
        "city": "Remote",
        "country": country,
        "source_url": "https://example.com",
    }
    with get_conn(db_path) as conn:
        upsert_job(conn, job)
        update_score(conn, job_id, 7.5, "Good fit", "queued")
    return job_id


def _approve(db_path, job_id):
    app_id = str(uuid.uuid4())
    with get_conn(db_path) as conn:
        create_application(conn, {
            "id": app_id,
            "job_id": job_id,
            "cv_path": "/tmp/cv.pdf",
            "cover_letter_path": "/tmp/cl.pdf",
        })
        record_user_decision(conn, app_id, "approved")


def _reject(db_path, job_id):
    app_id = str(uuid.uuid4())
    with get_conn(db_path) as conn:
        create_application(conn, {
            "id": app_id,
            "job_id": job_id,
            "cv_path": "/tmp/cv.pdf",
            "cover_letter_path": "/tmp/cl.pdf",
        })
        record_user_decision(conn, app_id, "rejected")


class TestTokenizeTitle:
    def test_basic_tokenization(self):
        tokens = _tokenize_title("Senior ML Engineer")
        assert "senior" in tokens
        assert "engineer" in tokens

    def test_stop_words_removed(self):
        tokens = _tokenize_title("ML Engineer at Acme and more")
        assert "and" not in tokens
        assert "at" not in tokens

    def test_short_words_removed(self):
        tokens = _tokenize_title("AI ML DL")
        # Words shorter than 3 chars are excluded
        assert "ai" not in tokens
        assert "ml" not in tokens
        assert "dl" not in tokens

    def test_gender_suffix_words_removed(self):
        tokens = _tokenize_title("ML Engineer (m/f/d)")
        assert "m" not in tokens
        assert "f" not in tokens
        assert "d" not in tokens


class TestBuildPreferenceContext:
    def test_returns_empty_below_threshold(self):
        db_path = _db()
        # Only 3 decisions — below MIN_DECISIONS=5
        for i in range(3):
            job_id = _make_job(db_path, f"ML Engineer {i}", ext_id=f"job-{i}")
            _approve(db_path, job_id)
        result = build_preference_context(db_path)
        assert result == ""

    def test_returns_context_with_enough_decisions(self):
        db_path = _db()
        # 6 decisions — above threshold
        for i in range(4):
            job_id = _make_job(db_path, f"Senior ML Engineer {i}", ext_id=f"approve-{i}")
            _approve(db_path, job_id)
        for i in range(2):
            job_id = _make_job(db_path, f"Junior Sales Intern {i}", ext_id=f"reject-{i}")
            _reject(db_path, job_id)

        result = build_preference_context(db_path)
        assert result != ""
        assert "User Preferences" in result
        assert "6" in result  # total decisions shown in header

    def test_includes_country_preferences(self):
        db_path = _db()
        for i in range(3):
            job_id = _make_job(db_path, f"ML Engineer {i}", country="DE", ext_id=f"de-{i}")
            _approve(db_path, job_id)
        for i in range(3):
            job_id = _make_job(db_path, f"Sales Rep {i}", country="SA", ext_id=f"sa-{i}")
            _reject(db_path, job_id)

        result = build_preference_context(db_path)
        assert "DE" in result
        assert "SA" in result


class TestSuggestKeywordAdditions:
    def test_returns_empty_when_no_rescued_jobs(self):
        db_path = _db()
        result = suggest_keyword_additions(db_path)
        assert result == []

    def test_suggests_words_from_rescued_keyword_filtered_jobs(self):
        db_path = _db()
        # Create two keyword-filtered jobs with the same unusual title word, then rescue them
        for i in range(2):
            job_id = _make_job(db_path, f"Quantitative Researcher {i}", ext_id=f"quant-{i}")
            with get_conn(db_path) as conn:
                conn.execute(
                    "UPDATE jobs SET skip_reason='keyword_filter', status='skipped' WHERE id=?",
                    (job_id,),
                )
            with get_conn(db_path) as conn:
                record_feedback_event(conn, job_id, "rescue")

        suggestions = suggest_keyword_additions(db_path)
        assert "quantitative" in suggestions or "researcher" in suggestions
