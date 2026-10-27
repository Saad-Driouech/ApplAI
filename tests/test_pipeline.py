"""Tests for pipeline orchestration (src/pipeline.py)."""
from types import SimpleNamespace
from unittest import mock

import src.database as db
import src.pipeline as pipeline


def test_generation_gives_up_after_max_attempts(tmp_path, monkeypatch):
    for name in ("cv.tex", "cl.tex"):
        (tmp_path / name).write_text("template")
    monkeypatch.setenv("APPLAI_CV_TEMPLATE", str(tmp_path / "cv.tex"))
    monkeypatch.setenv("APPLAI_CL_TEMPLATE", str(tmp_path / "cl.tex"))
    cfg = SimpleNamespace(
        paths=SimpleNamespace(db_path=tmp_path / "t.db", working_dir=tmp_path), job_max_age_days=30,
    )
    db.init_db(cfg.paths.db_path)
    with db.get_conn(cfg.paths.db_path) as conn:
        db.upsert_job(conn, {
            "id": "j1", "external_id": "e1", "source": "test", "title": "ML Engineer",
            "company": "Acme", "country": "DE", "source_url": "https://x",
        })
        db.update_status(conn, "j1", "queued")

    with mock.patch("src.documents.cv_generator.CVGenerator.generate", side_effect=RuntimeError("LLM down")):
        for _ in range(db.MAX_ATTEMPTS):
            assert pipeline.phase_generate(cfg) == 0

    with db.get_conn(cfg.paths.db_path) as conn:
        row = conn.execute("SELECT status, attempts, last_error FROM jobs WHERE id = 'j1'").fetchone()
    assert tuple(row) == ("failed", db.MAX_ATTEMPTS, "RuntimeError: LLM down")
    assert not any((tmp_path / ".pending").iterdir())  # failed attempts leave no pending folders


def test_run_all_scores_even_when_scrape_finds_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "phase_scrape", lambda cfg: calls.append("scrape") or 0)
    for phase in ("score", "generate", "deliver"):
        monkeypatch.setattr(pipeline, f"phase_{phase}", lambda cfg, p=phase: calls.append(p))

    pipeline.run_all(cfg=None)
    assert calls == ["scrape", "score", "generate", "deliver"]
