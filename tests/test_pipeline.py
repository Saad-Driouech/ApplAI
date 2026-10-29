"""Tests for pipeline orchestration (src/pipeline.py)."""
from types import SimpleNamespace
from unittest import mock

import pytest

import src.database as db
import src.pipeline as pipeline


def test_generation_gives_up_after_max_attempts(tmp_path, monkeypatch):
    for name in ("cv.tex", "cl.tex"):
        (tmp_path / name).write_text("template")
    monkeypatch.setenv("APPLAI_CV_TEMPLATE", str(tmp_path / "cv.tex"))
    monkeypatch.setenv("APPLAI_CL_TEMPLATE", str(tmp_path / "cl.tex"))
    cfg = SimpleNamespace(
        paths=SimpleNamespace(db_path=tmp_path / "t.db", working_dir=tmp_path),
        job_max_age_days=30, generate_batch_size=10,
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


def _deliver_cfg(tmp_path):
    cfg = SimpleNamespace(
        paths=SimpleNamespace(db_path=tmp_path / "t.db", working_dir=tmp_path),
        discord=SimpleNamespace(bot_token="x", channel_id="1"),
        notion=SimpleNamespace(api_token="x", job_tracker_db_id="x"),
    )
    db.init_db(cfg.paths.db_path)
    cv, cl = tmp_path / "cv.pdf", tmp_path / "cl.pdf"
    cv.write_text("cv")
    cl.write_text("cl")
    with db.get_conn(cfg.paths.db_path) as conn:
        db.upsert_job(conn, {
            "id": "j1", "external_id": "e1", "source": "test", "title": "ML Engineer",
            "company": "Acme", "country": "DE", "source_url": "https://x",
        })
        db.update_status(conn, "j1", "ready")
        db.create_application(conn, {"id": "a1", "job_id": "j1", "cv_path": str(cv), "cover_letter_path": str(cl)})
    return cfg


def _job(cfg):
    with db.get_conn(cfg.paths.db_path) as conn:
        return conn.execute(
            "SELECT j.status, j.attempts, a.discord_msg_id FROM jobs j JOIN applications a ON a.job_id = j.id"
        ).fetchone()


class TestDeliver:
    def test_notion_failure_does_not_resend(self, tmp_path):
        cfg = _deliver_cfg(tmp_path)
        with mock.patch("src.delivery.discord_bot.DiscordDelivery") as discord, \
             mock.patch("src.delivery.notion_tracker.NotionTracker") as notion:
            discord.return_value.send_bundle.return_value = "m1"
            notion.return_value.log_job.side_effect = RuntimeError("Notion down")
            assert pipeline.phase_deliver(cfg) == 1
            assert pipeline.phase_deliver(cfg) == 0
        discord.return_value.send_bundle.assert_called_once()
        discord.return_value.close.assert_called()
        assert tuple(_job(cfg)) == ("delivered", 0, "m1")

    def test_discord_failures_are_counted(self, tmp_path):
        cfg = _deliver_cfg(tmp_path)
        with mock.patch("src.delivery.discord_bot.DiscordDelivery") as discord, \
             mock.patch("src.delivery.notion_tracker.NotionTracker"):
            discord.return_value.send_bundle.side_effect = RuntimeError("400 Bad Request")
            for _ in range(db.MAX_ATTEMPTS):
                pipeline.phase_deliver(cfg)
        assert tuple(_job(cfg)) == ("failed", db.MAX_ATTEMPTS, None)

    def test_missing_documents_counted_not_sent(self, tmp_path):
        cfg = _deliver_cfg(tmp_path)
        (tmp_path / "cv.pdf").unlink()
        with mock.patch("src.delivery.discord_bot.DiscordDelivery") as discord, \
             mock.patch("src.delivery.notion_tracker.NotionTracker"):
            pipeline.phase_deliver(cfg)
        discord.return_value.send_bundle.assert_not_called()
        assert tuple(_job(cfg))[:2] == ("ready", 1)


def test_concurrent_phase_run_is_rejected(tmp_path):
    from src.utils.locks import PhaseBusy, phase_lock

    cfg = SimpleNamespace(paths=SimpleNamespace(db_path=tmp_path / "t.db", working_dir=tmp_path))
    with phase_lock(tmp_path, "deliver"):
        with pytest.raises(PhaseBusy):
            pipeline.phase_deliver(cfg)
    with phase_lock(tmp_path, "deliver"):
        pass  # released after the first holder exits
