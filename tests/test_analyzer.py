"""Tests for src/feedback/analyzer.py threshold recommendations."""
import uuid

import src.database as db
from src.feedback.analyzer import analyze


def _decided(path, score, decision):
    job_id = uuid.uuid4().hex
    with db.get_conn(path) as conn:
        db.upsert_job(conn, {
            "id": job_id, "external_id": job_id, "source": "t", "title": job_id, "company": job_id,
            "country": "DE", "source_url": "https://x",
        })
        db.update_score(conn, job_id, score, "x", "queued")
        app_id = uuid.uuid4().hex
        db.create_application(conn, {"id": app_id, "job_id": job_id, "cv_path": "a", "cover_letter_path": "b"})
        db.record_user_decision(conn, app_id, decision)


def _db(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    return path


def test_rejected_band_below_threshold_does_not_recommend_current_value(tmp_path):
    path = _db(tmp_path)
    for _ in range(4):
        _decided(path, 5.5, "rejected")   # rescued jobs, below threshold
    for _ in range(6):
        _decided(path, 7.5, "approved")
    report = analyze(path, current_threshold=6.0)
    assert report.recommended_threshold is None


def test_mostly_rejected_band_above_threshold_recommends_raise(tmp_path):
    path = _db(tmp_path)
    for _ in range(4):
        _decided(path, 6.5, "rejected")
    for _ in range(6):
        _decided(path, 8.5, "approved")
    assert analyze(path, current_threshold=6.0).recommended_threshold == 7.0


def test_mostly_approved_rescues_recommend_lowering(tmp_path):
    path = _db(tmp_path)
    for _ in range(4):
        _decided(path, 5.5, "approved")
    for _ in range(6):
        _decided(path, 8.5, "approved")
    assert analyze(path, current_threshold=6.0).recommended_threshold == 5.0
