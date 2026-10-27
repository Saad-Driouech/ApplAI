"""Tests for src/matching/scorer.py — keyword pre-filter and scoring outcomes."""
import pytest

import src.database as db
from src.matching.gemini_client import BudgetExceeded
from src.matching.response_parser import ScoreParseError, parse_score_response
from src.matching.scorer import Scorer, has_ai_keywords


class _FakeClient:
    """Stands in for a Tier-1 LLM client: returns a fixed raw reply."""

    def __init__(self, raw):
        self._raw = raw

    def score_job(self, job, cv_summary, user_preferences=""):
        return parse_score_response(self._raw)


@pytest.fixture
def db_with_job(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    with db.get_conn(path) as conn:
        db.upsert_job(conn, {
            "id": "j1", "external_id": "e1", "source": "test",
            "title": "Machine Learning Engineer", "company": "Acme", "country": "DE",
            "source_url": "https://x", "description": "PyTorch models",
        })
    return path


def _job_row(path):
    with db.get_conn(path) as conn:
        return conn.execute("SELECT status, score, score_reasoning FROM jobs WHERE id = 'j1'").fetchone()


class TestScoringOutcomes:
    def test_unparseable_reply_leaves_job_for_retry(self, db_with_job):
        stats = Scorer(db_with_job, _FakeClient("I think this is a great fit!"), cv_summary="").run()
        assert stats["parse_errors"] == 1 and stats["scored"] == 0
        row = _job_row(db_with_job)
        assert row["status"] == "new" and row["score"] is None

    def test_quota_exhausted_switches_to_fallback(self, db_with_job):
        class _QuotaClient:
            def score_job(self, *args, **kwargs):
                raise BudgetExceeded("quota")

        stats = Scorer(
            db_with_job, _QuotaClient(), cv_summary="",
            fallback_client=_FakeClient('{"score": 7, "reasoning": "fit"}'),
        ).run()
        assert stats["quota_hit"] == 1 and stats["queued"] == 1
        assert _job_row(db_with_job)["status"] == "queued"

    def test_quota_exhausted_without_fallback_stops_batch(self, db_with_job):
        class _QuotaClient:
            def score_job(self, *args, **kwargs):
                raise BudgetExceeded("quota")

        stats = Scorer(db_with_job, _QuotaClient(), cv_summary="").run()
        assert stats["quota_hit"] == 1
        assert _job_row(db_with_job)["status"] == "new"

    def test_location_outside_targets_skipped_without_llm_call(self, db_with_job):
        with db.get_conn(db_with_job) as conn:
            conn.execute("UPDATE jobs SET country = 'XX', city = 'Remote · USA only' WHERE id = 'j1'")

        class _MustNotBeCalled:
            def score_job(self, *args, **kwargs):
                raise AssertionError("LLM called for an out-of-target job")

        stats = Scorer(
            db_with_job, _MustNotBeCalled(), cv_summary="", allowed_countries=frozenset({"DE"}),
        ).run()
        assert stats["filtered"] == 1
        with db.get_conn(db_with_job) as conn:
            row = conn.execute("SELECT status, skip_reason FROM jobs WHERE id = 'j1'").fetchone()
        assert tuple(row) == ("skipped", "location")

    def test_non_job_skipped_even_with_high_score(self, db_with_job):
        reply = '{"score": 8, "reasoning": "AI product launch", "is_job_posting": false}'
        stats = Scorer(db_with_job, _FakeClient(reply), cv_summary="").run()
        assert stats["not_a_job"] == 1 and stats["queued"] == 0
        with db.get_conn(db_with_job) as conn:
            row = conn.execute("SELECT status, skip_reason FROM jobs WHERE id = 'j1'").fetchone()
        assert tuple(row) == ("skipped", "not_a_job")

    def test_config_error_aborts_batch(self, db_with_job):
        with db.get_conn(db_with_job) as conn:
            db.upsert_job(conn, {
                "id": "j2", "external_id": "e2", "source": "test", "title": "Data Scientist",
                "company": "Other", "country": "DE", "source_url": "https://y",
            })

        class _ModelNotFound(Exception):
            status_code = 404

        class _Client:
            calls = 0

            def score_job(self, *args, **kwargs):
                _Client.calls += 1
                raise _ModelNotFound("model_not_found")

        stats = Scorer(db_with_job, _Client(), cv_summary="").run()
        assert _Client.calls == 1 and stats["errors"] == 1 and stats["deferred"] == 1

    def test_batch_size_limits_llm_calls_not_prefilters(self, db_with_job):
        with db.get_conn(db_with_job) as conn:
            for i, title in enumerate(["Data Scientist", "Accountant", "Bank Clerk"]):
                db.upsert_job(conn, {
                    "id": f"k{i}", "external_id": f"k{i}", "source": "test", "title": title,
                    "company": f"Co{i}", "country": "DE", "source_url": "https://y",
                })

        class _Counting(_FakeClient):
            calls = 0

            def score_job(self, *args, **kwargs):
                _Counting.calls += 1
                return super().score_job(*args, **kwargs)

        stats = Scorer(
            db_with_job, _Counting('{"score": 8, "reasoning": "fit"}'), cv_summary="", batch_size=1,
        ).run()
        assert _Counting.calls == 1 and stats["scored"] == 1
        assert stats["filtered"] == 2   # both non-AI jobs handled despite the batch limit
        assert stats["deferred"] == 1   # second AI job left for the next run

    def test_repeated_parse_failures_mark_job_failed(self, db_with_job):
        for _ in range(db.MAX_ATTEMPTS):
            Scorer(db_with_job, _FakeClient("no json here"), cv_summary="").run()
        with db.get_conn(db_with_job) as conn:
            row = conn.execute("SELECT status, attempts, last_error FROM jobs WHERE id = 'j1'").fetchone()
        assert row["status"] == "failed" and row["attempts"] == db.MAX_ATTEMPTS
        assert "ScoreParseError" in row["last_error"]

    def test_valid_reply_is_scored(self, db_with_job):
        stats = Scorer(db_with_job, _FakeClient('{"score": 8, "reasoning": "fit"}'), cv_summary="").run()
        assert stats["queued"] == 1
        assert tuple(_job_row(db_with_job)) == ("queued", 8.0, "fit")


class TestHasAiKeywords:
    # ── Positive cases — should match ────────────────────────────────────

    def test_matches_title_machine_learning(self):
        assert has_ai_keywords("Machine Learning Engineer", "") is True

    def test_matches_title_data_scientist(self):
        assert has_ai_keywords("Data Scientist", "") is True

    def test_matches_title_nlp(self):
        assert has_ai_keywords("NLP Researcher", "") is True

    def test_matches_description_pytorch(self):
        assert has_ai_keywords("Software Engineer", "Experience with PyTorch required.") is True

    def test_matches_description_llm(self):
        assert has_ai_keywords("Backend Developer", "We build LLM-powered products.") is True

    def test_matches_description_generative_ai(self):
        assert has_ai_keywords("Product Manager", "Focus on generative AI roadmap.") is True

    def test_matches_description_mlops(self):
        assert has_ai_keywords("Platform Engineer", "MLOps experience preferred.") is True

    def test_matches_description_huggingface(self):
        assert has_ai_keywords("Engineer", "Familiarity with Hugging Face models.") is True

    # ── Negative cases — should not match ────────────────────────────────

    def test_no_match_unrelated_role(self):
        assert has_ai_keywords("Bank Clerk", "Processing invoices and account transactions.") is False

    def test_no_match_accounting(self):
        assert has_ai_keywords("Financial Controller", "Budget management and reporting.") is False

    def test_no_match_empty(self):
        assert has_ai_keywords("", "") is False

    # ── False-positive guard — German words containing short substrings ──

    def test_no_false_positive_german_fragestellungen(self):
        """'Fragestellungen' must not match — it previously triggered 'rag'."""
        assert has_ai_keywords(
            "Projektmanager",
            "Analyse von komplexen Fragestellungen im Bereich Finanzen.",
        ) is False

    def test_no_false_positive_training_role(self):
        """A generic 'Training Manager' role without AI context should not match."""
        assert has_ai_keywords(
            "Training Manager",
            "Design and deliver employee onboarding programs.",
        ) is False
