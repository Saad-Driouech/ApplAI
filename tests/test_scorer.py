"""Tests for src/matching/scorer.py — keyword pre-filter."""
import pytest

from src.matching.scorer import has_ai_keywords


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
