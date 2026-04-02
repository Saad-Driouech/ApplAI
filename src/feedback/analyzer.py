"""
Feedback loop analyzer.

Examines historical approve/reject decisions to recommend score threshold
adjustments. The goal: minimize wasted document generation (rejected bundles)
while not filtering out jobs the user would approve.

Requires a minimum number of decisions before making recommendations.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import src.database as db
from src.logger import audit, get_logger

log = get_logger(__name__)

MIN_DECISIONS = 10  # don't recommend changes with fewer data points

# Score bands for analysis
_BANDS = [
    (5.0, 6.0),
    (6.0, 7.0),
    (7.0, 8.0),
    (8.0, 10.1),
]


@dataclass
class BandStats:
    low: float
    high: float
    total: int
    approved: int
    rejected: int

    @property
    def approval_rate(self) -> float:
        return self.approved / self.total if self.total > 0 else 0.0

    @property
    def label(self) -> str:
        high = "10" if self.high > 10 else f"{self.high:.0f}"
        return f"{self.low:.0f}-{high}"


@dataclass
class FeedbackReport:
    total_decisions: int
    total_approved: int
    total_rejected: int
    bands: list[BandStats]
    current_threshold: float
    recommended_threshold: float | None
    recommendation_reason: str

    @property
    def overall_approval_rate(self) -> float:
        return self.total_approved / self.total_decisions if self.total_decisions > 0 else 0.0


def analyze(db_path: Path, current_threshold: float = 6.0) -> FeedbackReport:
    """
    Analyze historical decisions and return a feedback report.

    The recommendation logic:
    - If approval rate in the band just below threshold is > 60%,
      suggest lowering threshold (we're missing good jobs).
    - If approval rate in the band at/above threshold is < 40%,
      suggest raising threshold (we're wasting generation on bad jobs).
    - Otherwise, threshold is well-calibrated.
    """
    with db.get_conn(db_path) as conn:
        rows = conn.execute("""
            SELECT j.score, a.user_decision
            FROM applications a
            JOIN jobs j ON a.job_id = j.id
            WHERE a.user_decision IS NOT NULL AND j.score IS NOT NULL
        """).fetchall()

    total = len(rows)
    total_approved = sum(1 for r in rows if r["user_decision"] == "approved")
    total_rejected = total - total_approved

    # Build band stats
    bands = []
    for low, high in _BANDS:
        in_band = [r for r in rows if low <= r["score"] < high]
        approved = sum(1 for r in in_band if r["user_decision"] == "approved")
        rejected = len(in_band) - approved
        bands.append(BandStats(
            low=low, high=high,
            total=len(in_band), approved=approved, rejected=rejected,
        ))

    # Generate recommendation
    recommended = None
    reason = ""

    if total < MIN_DECISIONS:
        reason = f"Not enough data yet ({total}/{MIN_DECISIONS} decisions). Keep reviewing to calibrate."
    else:
        # Check each band from lowest up — if rejection rate is high,
        # suggest raising threshold to avoid wasting document generation.
        for band in bands:
            if band.total >= 3 and band.approval_rate < 0.4:
                recommended = band.high
                reason = (
                    f"You rejected {1 - band.approval_rate:.0%} of jobs in the "
                    f"{band.label} band. Consider raising threshold to {band.high:.1f} "
                    f"to reduce wasted generations."
                )
                break

        if not reason:
            reason = "Threshold looks well-calibrated based on your decisions."

    report = FeedbackReport(
        total_decisions=total,
        total_approved=total_approved,
        total_rejected=total_rejected,
        bands=bands,
        current_threshold=current_threshold,
        recommended_threshold=recommended,
        recommendation_reason=reason,
    )

    audit(
        "feedback_analysis",
        total=total,
        approved=total_approved,
        rejected=total_rejected,
        current_threshold=current_threshold,
        recommended_threshold=recommended,
    )

    return report
