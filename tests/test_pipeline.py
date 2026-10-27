"""Tests for pipeline orchestration (src/pipeline.py)."""
import src.pipeline as pipeline


def test_run_all_scores_even_when_scrape_finds_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "phase_scrape", lambda cfg: calls.append("scrape") or 0)
    for phase in ("score", "generate", "deliver"):
        monkeypatch.setattr(pipeline, f"phase_{phase}", lambda cfg, p=phase: calls.append(p))

    pipeline.run_all(cfg=None)
    assert calls == ["scrape", "score", "generate", "deliver"]
