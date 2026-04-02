"""
Preference learning from user decisions.

Analyzes approved/rejected/rescued jobs to extract patterns and build
a natural-language preference context that gets injected into the
scoring prompt. Pure SQL + Python — no LLM calls.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import src.database as db
from src.logger import get_logger

log = get_logger(__name__)

MIN_DECISIONS = 5  # need at least this many to extract meaningful patterns

# Common words to ignore when analyzing title patterns
_STOP_WORDS = {
    "m", "w", "d", "f", "gn", "mwd", "fmd", "and", "or", "the", "a", "an",
    "in", "at", "for", "of", "to", "with", "als", "und", "der", "die", "das",
    "ein", "eine", "fur", "von", "bei", "im", "am",
}


def _tokenize_title(title: str) -> list[str]:
    """Extract meaningful words from a job title."""
    words = re.findall(r"[a-zA-Z]{3,}", title)
    return [w.lower() for w in words if w.lower() not in _STOP_WORDS]


def build_preference_context(db_path: Path) -> str:
    """
    Analyze historical decisions and return a natural-language preference
    summary to inject into the scoring prompt.

    Returns empty string if not enough decisions yet.
    """
    with db.get_conn(db_path) as conn:
        # Get all decisions (approve/reject from applications)
        decisions = conn.execute("""
            SELECT j.title, j.company, j.country, j.score, a.user_decision
            FROM applications a
            JOIN jobs j ON a.job_id = j.id
            WHERE a.user_decision IS NOT NULL AND j.score IS NOT NULL
        """).fetchall()

        # Get rescued jobs (treated as implicit approval signal)
        rescues = conn.execute("""
            SELECT j.title, j.company, j.country, j.score
            FROM feedback_events fe
            JOIN jobs j ON fe.job_id = j.id
            WHERE fe.event_type = 'rescue'
        """).fetchall()

    total = len(decisions) + len(rescues)
    if total < MIN_DECISIONS:
        return ""

    approved_titles = []
    rejected_titles = []
    approved_countries = Counter()
    rejected_countries = Counter()
    approved_scores = []
    rejected_scores = []

    for row in decisions:
        tokens = _tokenize_title(row["title"])
        if row["user_decision"] == "approved":
            approved_titles.extend(tokens)
            approved_countries[row["country"]] += 1
            approved_scores.append(row["score"])
        else:
            rejected_titles.extend(tokens)
            rejected_countries[row["country"]] += 1
            rejected_scores.append(row["score"])

    # Rescued jobs count as positive signal
    for row in rescues:
        tokens = _tokenize_title(row["title"])
        approved_titles.extend(tokens)
        approved_countries[row["country"]] += 1
        if row["score"]:
            approved_scores.append(row["score"])

    # Find distinctive words (appear in approved but rarely in rejected, or vice versa)
    approved_freq = Counter(approved_titles)
    rejected_freq = Counter(rejected_titles)

    preferred_words = []
    avoided_words = []

    for word, count in approved_freq.most_common(20):
        if count >= 2 and rejected_freq.get(word, 0) <= count * 0.3:
            preferred_words.append(word.title())
    for word, count in rejected_freq.most_common(20):
        if count >= 2 and approved_freq.get(word, 0) <= count * 0.3:
            avoided_words.append(word.title())

    # Build the context paragraph
    lines = [f"## User Preferences (learned from {total} past decisions)"]

    if preferred_words:
        lines.append(f"- Approved roles commonly include: {', '.join(preferred_words[:8])}.")
    if avoided_words:
        lines.append(f"- Rejected roles commonly include: {', '.join(avoided_words[:8])}.")

    if approved_countries:
        top = ", ".join(f"{c}" for c, _ in approved_countries.most_common(3))
        lines.append(f"- Preferred countries: {top}.")
    if rejected_countries:
        top = ", ".join(f"{c}" for c, _ in rejected_countries.most_common(3))
        lines.append(f"- Less preferred countries: {top}.")

    if approved_scores:
        avg_approved = sum(approved_scores) / len(approved_scores)
        lines.append(f"- Average score of approved jobs: {avg_approved:.1f}.")
    if rejected_scores:
        avg_rejected = sum(rejected_scores) / len(rejected_scores)
        lines.append(f"- Average score of rejected jobs: {avg_rejected:.1f}.")

    lines.append("Adjust your scoring to reflect these observed preferences.")

    return "\n".join(lines)


def suggest_keyword_additions(db_path: Path) -> list[str]:
    """
    Find potential keywords to add to the AI pre-filter by analyzing
    rescued jobs that were originally keyword-filtered.

    Returns a list of suggested keywords (words from rescued job titles
    that aren't in the current filter).
    """
    from src.matching.scorer import _AI_KEYWORDS

    with db.get_conn(db_path) as conn:
        rows = conn.execute("""
            SELECT j.title
            FROM feedback_events fe
            JOIN jobs j ON fe.job_id = j.id
            WHERE fe.event_type = 'rescue' AND j.skip_reason = 'keyword_filter'
        """).fetchall()

    if not rows:
        return []

    # Tokenize titles and find words not in current keyword set
    word_counts = Counter()
    existing_lower = {kw.lower() for kw in _AI_KEYWORDS}

    for row in rows:
        tokens = _tokenize_title(row["title"])
        for token in tokens:
            if token not in existing_lower:
                word_counts[token] += 1

    # Only suggest words that appear in 2+ rescued titles
    return [word for word, count in word_counts.most_common(10) if count >= 2]
