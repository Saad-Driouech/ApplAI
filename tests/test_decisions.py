"""Tests for the Discord approve/reject handler (src/api.py::_process_decision)."""
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import src.api as api
import src.database as db
from src.utils.file_manager import cleanup_pending, pending_dir


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A working dir with one pending bundle and an existing role folder at the same company."""
    db_path = tmp_path / "t.db"
    db.init_db(db_path)
    monkeypatch.setattr(api, "_cfg", SimpleNamespace(
        paths=SimpleNamespace(db_path=db_path, working_dir=tmp_path),
        discord=SimpleNamespace(bot_token="x", channel_id="1"),
        notion=SimpleNamespace(api_token="x", job_tracker_db_id="x"),
    ))
    monkeypatch.setenv("CANDIDATE_NAME", "Jane Doe")

    # Manually curated application at the same company — must never be deleted
    role_dir = tmp_path / "germany" / "Acme_GmbH" / "ML_Engineer"
    role_dir.mkdir(parents=True)
    (role_dir / "JD.txt").write_text("earlier JD")

    app_id = str(uuid.uuid4())
    pending = pending_dir(tmp_path, app_id)
    pending.mkdir(parents=True)
    for name in ("cv.tex", "cv.pdf", "cover_letter.tex", "cover_letter.pdf"):
        (pending / name).write_text(name)

    with db.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (id, external_id, source, title, company, country, source_url, scraped_at, status) "
            "VALUES ('j1', 'e1', 'test', 'AI Engineer', 'Acme GmbH', 'DE', 'https://x', 'now', 'submitted')"
        )
        db.create_application(conn, {
            "id": app_id, "job_id": "j1",
            "cv_path": str(pending / "cv.pdf"),
            "cover_letter_path": str(pending / "cover_letter.pdf"),
        })

    with mock.patch("src.delivery.discord_bot.DiscordDelivery") as discord, \
         mock.patch("src.delivery.notion_tracker.NotionTracker") as notion:
        yield SimpleNamespace(
            db_path=db_path, app_id=app_id, pending=pending, role_dir=role_dir,
            company_dir=role_dir.parent, discord=discord, notion=notion,
        )


def _decision(env):
    with db.get_conn(env.db_path) as conn:
        return conn.execute(
            "SELECT a.user_decision, j.status FROM applications a JOIN jobs j ON a.job_id = j.id WHERE a.id = ?",
            (env.app_id,),
        ).fetchone()


class TestProcessDecision:
    def test_approve_promotes_files(self, env):
        api._process_decision(env.app_id, "approved", "m1")
        assert (env.company_dir / "Jane_Doe_Acme_GmbH_CV.pdf").exists()
        assert not env.pending.exists()
        assert tuple(_decision(env)) == ("approved", "approved")

    def test_double_approve_keeps_company_folder(self, env):
        api._process_decision(env.app_id, "approved", "m1")
        api._process_decision(env.app_id, "approved", "m1")
        assert (env.role_dir / "JD.txt").exists()
        assert (env.company_dir / "Jane_Doe_Acme_GmbH_CV.pdf").exists()

    def test_reject_after_approve_is_ignored(self, env):
        api._process_decision(env.app_id, "approved", "m1")
        api._process_decision(env.app_id, "rejected", "m1")
        assert (env.role_dir / "JD.txt").exists()
        assert tuple(_decision(env)) == ("approved", "approved")
        # Repeated click re-syncs Discord to the recorded decision, not the new click
        last_call = env.discord.return_value.update_decision.call_args
        assert last_call.args[1] == "approved"

    def test_reject_removes_only_pending(self, env):
        api._process_decision(env.app_id, "rejected", "m1")
        assert not env.pending.exists()
        assert (env.role_dir / "JD.txt").exists()
        assert tuple(_decision(env)) == ("rejected", "rejected")

    def test_missing_documents_release_claim(self, env):
        (env.pending / "cv.pdf").unlink()
        with pytest.raises(FileNotFoundError):
            api._process_decision(env.app_id, "approved", "m1")
        assert _decision(env)["user_decision"] is None  # user can retry

    def test_notion_failure_still_updates_discord(self, env):
        with db.get_conn(env.db_path) as conn:
            conn.execute("UPDATE applications SET notion_page_id = 'p1' WHERE id = ?", (env.app_id,))
        env.notion.return_value.record_decision.side_effect = RuntimeError("Notion down")
        api._process_decision(env.app_id, "approved", "m1")
        env.discord.return_value.update_decision.assert_called_once()
        assert _decision(env)["user_decision"] == "approved"


class TestDecisionClaim:
    def test_only_first_decision_wins(self, env):
        with db.get_conn(env.db_path) as conn:
            assert db.record_user_decision(conn, env.app_id, "approved") is True
            assert db.record_user_decision(conn, env.app_id, "rejected") is False
        assert _decision(env)["user_decision"] == "approved"


class TestCleanupPendingGuard:
    def test_refuses_non_pending_folder(self, tmp_path):
        company_dir = tmp_path / "germany" / "Acme_GmbH"
        company_dir.mkdir(parents=True)
        with pytest.raises(ValueError):
            cleanup_pending(company_dir)
        assert company_dir.exists()

    def test_deletes_pending_folder(self, tmp_path):
        pending = pending_dir(tmp_path, "abc")
        pending.mkdir(parents=True)
        cleanup_pending(pending)
        assert not pending.exists()
