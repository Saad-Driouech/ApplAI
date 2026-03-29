"""
ApplAI main pipeline orchestrator.

This module wires all components together and is called by n8n
(or from the command line) to run a complete cycle:

  1. Scrape jobs from all configured boards
  2. Score 'new' jobs via Gemini
  3. Generate documents (CV + cover letter) for 'queued' jobs
  4. Send bundles to Discord for review
  5. Log everything to Notion

Each phase can also be run independently via the CLI flags below.

Usage (from inside Docker or locally):
  python -m src.pipeline --phase all        # full run
  python -m src.pipeline --phase scrape
  python -m src.pipeline --phase score
  python -m src.pipeline --phase generate
  python -m src.pipeline --phase deliver
"""
from __future__ import annotations

import argparse
import uuid
from pathlib import Path

import src.database as db
import src.logger as logger_module
from src.config import get as get_config
from src.logger import get_logger

log = get_logger(__name__)


def _build_scrapers(cfg, db_path: Path) -> list:
    from src.scrapers.arbeitnow import ArbeitnowScraper
    from src.scrapers.linkedin_rss import LinkedInRssScraper
    from src.scrapers.remotive import RemotiveScraper
    # Legacy scrapers (currently blocked by anti-bot — kept for future fixes)
    from src.scrapers.rss import IndeedRssScraper, StepStoneRssScraper, BaytRssScraper

    scrapers = []

    # ── Active scrapers ───────────────────────────────────────────────────
    # Arbeitnow: Germany tech jobs, free API
    scrapers.append(ArbeitnowScraper(db_path))
    # Remotive: remote ML/AI roles globally
    scrapers.append(RemotiveScraper(db_path))

    # ── Legacy scrapers (blocked/dead — enable once fixed) ────────────────
    # scrapers.append(LinkedInRssScraper(db_path, country="DE"))  # RSS feed dead (404)
    # scrapers.append(LinkedInRssScraper(db_path, country="AE"))
    # scrapers.append(LinkedInRssScraper(db_path, country="SA"))
    # scrapers.append(IndeedRssScraper(db_path, country="DE"))    # 403 anti-bot
    # scrapers.append(IndeedRssScraper(db_path, country="AE"))
    # scrapers.append(StepStoneRssScraper(db_path, country="DE")) # 404
    # scrapers.append(BaytRssScraper(db_path, country="SA"))      # 403 anti-bot
    # scrapers.append(BaytRssScraper(db_path, country="AE"))

    return scrapers


def phase_scrape(cfg) -> int:
    """Run all scrapers. Returns total new jobs found."""
    # TODO: make these configurable via env vars
    QUERY = "Machine Learning Engineer OR AI Engineer OR Applied AI Researcher OR Data Scientist"
    LOCATIONS = {
        "DE": "Germany",
        "AE": "Dubai",
        "SA": "Riyadh",
    }

    scrapers = _build_scrapers(cfg, cfg.paths.db_path)
    total_new = 0

    for scraper in scrapers:
        location = LOCATIONS.get(
            getattr(scraper, "_country", "DE"), "Germany"
        )
        new_jobs = scraper.run(query=QUERY, location=location)
        total_new += len(new_jobs)

    log.info("Scrape phase complete: %d new jobs total", total_new)
    return total_new


def phase_score(cfg) -> dict:
    """Score all 'new' jobs via Gemini."""
    from src.matching.gemini_client import GeminiClient
    from src.matching.scorer import Scorer

    # CV summary — loaded from a text file if present, else a placeholder
    cv_summary_path = cfg.paths.working_dir / "cv_summary.txt"
    if cv_summary_path.exists():
        cv_summary = cv_summary_path.read_text(encoding="utf-8")
    else:
        log.warning(
            "cv_summary.txt not found at %s — using placeholder. "
            "Create this file for accurate scoring.",
            cv_summary_path,
        )
        cv_summary = (
            "No profile summary provided. "
            "Create cv_summary.txt in your working directory for accurate scoring."
        )

    if cfg.tier1_provider == "groq":
        from src.matching.groq_client import GroqClient
        scoring_client = GroqClient(
            api_key=cfg.groq.api_key,
            model=cfg.groq.model,
            rpm=cfg.groq.requests_per_minute,
            rpd=cfg.groq.requests_per_day,
        )
    else:
        scoring_client = GeminiClient(
            api_key=cfg.gemini.api_key,
            model=cfg.gemini.model,
            rpm=cfg.gemini.requests_per_minute,
            rpd=cfg.gemini.requests_per_day,
        )
    scorer = Scorer(
        db_path=cfg.paths.db_path,
        client=scoring_client,
        cv_summary=cv_summary,
        score_threshold=cfg.score_threshold,
    )
    stats = scorer.run()
    log.info("Score phase complete: %s", stats)
    return stats


def phase_generate(cfg) -> int:
    """Generate CV + cover letter for all 'queued' jobs."""
    from src.documents.cv_generator import CVGenerator
    from src.documents.cover_letter import CoverLetterGenerator

    import os

    # CV template
    cv_template_path = Path(os.environ.get("APPLAI_CV_TEMPLATE", ""))
    if not cv_template_path.exists():
        log.error(
            "CV template not found. Set APPLAI_CV_TEMPLATE env var to the path of your .tex file."
        )
        return 0

    # Cover letter template
    cl_template_path = Path(os.environ.get("APPLAI_CL_TEMPLATE", ""))
    if not cl_template_path.exists():
        log.error(
            "Cover letter template not found. Set APPLAI_CL_TEMPLATE env var to the path of your .tex file."
        )
        return 0

    profile_path = cfg.paths.working_dir / "profile_summary.md"
    if not profile_path.exists():
        log.warning(
            "profile_summary.md not found at %s — documents will lack candidate context",
            profile_path,
        )
        profile_path = None

    candidate_name = os.environ.get("CANDIDATE_NAME", "Candidate Name")
    candidate_email = os.environ.get("CANDIDATE_EMAIL", "candidate@email.com")

    cv_gen = CVGenerator(
        template_path=cv_template_path,
        output_dir=cfg.paths.working_dir / "applications",
        profile_path=profile_path,
    )

    cl_gen = CoverLetterGenerator(
        template_path=cl_template_path,
        output_dir=cfg.paths.working_dir / "applications",
        profile_path=profile_path,
        candidate_name=candidate_name,
        candidate_email=candidate_email,
    )

    generated = 0
    with db.get_conn(cfg.paths.db_path) as conn:
        jobs = db.get_jobs_by_status(conn, "queued", limit=10)

    for row in jobs:
        job = dict(row)
        job_id = job["id"]
        reasoning = job.get("score_reasoning", "")

        try:
            db.update_status_direct(cfg.paths.db_path, job_id, "generating")

            cv_path = cv_gen.generate(job, gemini_reasoning=reasoning)
            cl_path = cl_gen.generate(job, gemini_reasoning=reasoning)

            app_id = str(uuid.uuid4())
            with db.get_conn(cfg.paths.db_path) as conn:
                db.create_application(conn, {
                    "id": app_id,
                    "job_id": job_id,
                    "cv_path": str(cv_path),
                    "cover_letter_path": str(cl_path),
                })
                db.update_status(conn, job_id, "ready")

            generated += 1
            log.info("Documents generated for job %s", job_id)

        except Exception as exc:
            log.error("Document generation failed for job %s: %s", job_id, exc, exc_info=True)
            with db.get_conn(cfg.paths.db_path) as conn:
                db.update_status(conn, job_id, "queued")  # retry next cycle

    log.info("Generate phase complete: %d application bundles created", generated)
    return generated


def phase_deliver(cfg) -> int:
    """Send ready bundles to Discord and log to Notion."""
    from src.delivery.discord_bot import DiscordDelivery
    from src.delivery.notion_tracker import NotionTracker

    discord = DiscordDelivery(
        bot_token=cfg.discord.bot_token,
        channel_id=cfg.discord.channel_id,
    )
    notion = NotionTracker(
        api_token=cfg.notion.api_token,
        database_id=cfg.notion.job_tracker_db_id,
    )

    delivered = 0
    with db.get_conn(cfg.paths.db_path) as conn:
        # Find applications whose job status is 'ready'
        rows = conn.execute("""
            SELECT a.*, j.title, j.company, j.country, j.city, j.score, j.score_reasoning,
                   j.source_url, j.id as job_id
            FROM applications a
            JOIN jobs j ON a.job_id = j.id
            WHERE j.status = 'ready' AND a.user_decision IS NULL
            LIMIT 10
        """).fetchall()

    for row in rows:
        app = dict(row)
        job = {
            "id": app["job_id"],
            "title": app["title"],
            "company": app["company"],
            "country": app["country"],
            "city": app.get("city", ""),
            "score": app["score"],
            "source_url": app["source_url"],
        }

        try:
            cv_path = Path(app["cv_path"])
            cl_path = Path(app["cover_letter_path"])

            if not cv_path.exists() or not cl_path.exists():
                log.warning("Documents missing for app %s — skipping delivery", app["id"])
                continue

            msg_id = discord.send_bundle(
                app_id=app["id"],
                job=job,
                cv_path=cv_path,
                cover_letter_path=cl_path,
                score=app["score"] or 0.0,
                reasoning=app["score_reasoning"] or "",
            )

            notion_page_id = notion.log_job(job, app)

            with db.get_conn(cfg.paths.db_path) as conn:
                conn.execute(
                    "UPDATE applications SET discord_msg_id=?, notion_page_id=? WHERE id=?",
                    (msg_id, notion_page_id, app["id"]),
                )
                db.update_status(conn, app["job_id"], "submitted")

            delivered += 1

        except Exception as exc:
            log.error("Delivery failed for app %s: %s", app["id"], exc, exc_info=True)

    discord.close()
    log.info("Deliver phase complete: %d bundles sent", delivered)
    return delivered


def run_all(cfg) -> None:
    log.info("=== ApplAI pipeline starting ===")
    new_jobs = phase_scrape(cfg)
    if new_jobs > 0:
        phase_score(cfg)
    phase_generate(cfg)
    phase_deliver(cfg)
    log.info("=== ApplAI pipeline complete ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="ApplAI pipeline runner")
    parser.add_argument(
        "--phase",
        choices=["all", "scrape", "score", "generate", "deliver"],
        default="all",
        help="Pipeline phase to run",
    )
    args = parser.parse_args()

    cfg = get_config()
    logger_module.setup(logs_dir=cfg.paths.logs_dir, debug=cfg.debug)

    # Ensure database is initialised
    db.init_db(cfg.paths.db_path)

    if args.phase == "all":
        run_all(cfg)
    elif args.phase == "scrape":
        phase_scrape(cfg)
    elif args.phase == "score":
        phase_score(cfg)
    elif args.phase == "generate":
        phase_generate(cfg)
    elif args.phase == "deliver":
        phase_deliver(cfg)


if __name__ == "__main__":
    main()
