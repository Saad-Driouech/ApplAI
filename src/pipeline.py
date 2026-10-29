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
import functools
import uuid
from pathlib import Path

import src.database as db
import src.logger as logger_module
from src.config import get as get_config
from src.logger import get_logger
from src.utils.locks import phase_lock

log = get_logger(__name__)


def _exclusive(phase: str):
    """Run the phase under its lock; raises PhaseBusy if another run holds it."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(cfg, *args, **kwargs):
            with phase_lock(cfg.paths.db_path.parent, phase):
                return fn(cfg, *args, **kwargs)
        return wrapper
    return decorator


_SCRAPE_QUERY = "Machine Learning Engineer OR AI Engineer OR Applied AI Researcher OR Data Scientist"


def _build_scrapers(cfg, db_path: Path) -> list:
    from src.scrapers.arbeitnow import ArbeitnowScraper
    from src.scrapers.remotive import RemotiveScraper
    from src.scrapers.remoteok import RemoteOKScraper

    scrapers = []

    # ── Active scrapers ───────────────────────────────────────────────────
    # Arbeitnow: Germany tech jobs, free API
    scrapers.append(ArbeitnowScraper(db_path))
    # Remotive: remote ML/AI roles globally, free API
    scrapers.append(RemotiveScraper(db_path))
    # RemoteOK: remote tech jobs globally, free public JSON API
    scrapers.append(RemoteOKScraper(db_path))

    # Adzuna: broad coverage across DE/AE/NL/CH, free API key required
    if cfg.adzuna.enabled:
        from src.scrapers.adzuna import AdzunaScraper
        scrapers.append(AdzunaScraper(db_path, app_id=cfg.adzuna.app_id, app_key=cfg.adzuna.app_key))
        log.info("Adzuna scraper enabled")
    else:
        log.debug("Adzuna scraper disabled (ADZUNA_APP_ID / ADZUNA_APP_KEY not set)")

    # ── Legacy scrapers (blocked/dead — enable once fixed) ────────────────
    # scrapers.append(IndeedRssScraper(db_path, country="DE"))    # 403 anti-bot
    # scrapers.append(StepStoneRssScraper(db_path, country="DE")) # 404
    # scrapers.append(BaytRssScraper(db_path, country="SA"))      # 403 anti-bot

    return scrapers


@_exclusive("scrape")
def phase_scrape(cfg) -> int:
    """Run all scrapers. Returns total new jobs found."""
    scrapers = _build_scrapers(cfg, cfg.paths.db_path)
    total_new = 0

    # Only Arbeitnow uses these; the other sources filter by category or their own query
    for scraper in scrapers:
        new_jobs = scraper.run(query=_SCRAPE_QUERY, location="Germany")
        total_new += len(new_jobs)

    log.info("Scrape phase complete: %d new jobs total", total_new)
    return total_new


def _expire_stale(cfg) -> int:
    with db.get_conn(cfg.paths.db_path) as conn:
        expired = db.expire_stale_jobs(conn, cfg.job_max_age_days)
    if expired:
        log.info("Expired %d jobs older than %d days", expired, cfg.job_max_age_days)
    return expired


def load_cv_summary(cfg) -> str:
    """Profile summary used as scoring context, or a placeholder if missing."""
    profile_path = cfg.paths.working_dir / "profile_summary.md"
    if profile_path.exists():
        return profile_path.read_text(encoding="utf-8")
    log.warning(
        "profile_summary.md not found at %s — using placeholder. "
        "Create this file for accurate scoring.",
        profile_path,
    )
    return (
        "No profile summary provided. "
        "Create profile_summary.md in your working directory for accurate scoring."
    )


def build_tier1_clients(cfg) -> tuple:
    """Return (scoring_client, fallback_client) for the configured Tier-1 provider."""
    from src.matching.gemini_client import GeminiClient

    if cfg.tier1_provider == "ollama":
        from src.matching.ollama_client import OllamaClient
        scoring_client = OllamaClient(
            endpoint=cfg.ollama.endpoint,
            model=cfg.ollama.model,
        )
    elif cfg.tier1_provider == "groq":
        from src.matching.groq_client import GroqClient
        scoring_client = GroqClient(
            api_key=cfg.groq.api_key,
            model=cfg.groq.model,
            rpm=cfg.groq.requests_per_minute,
            rpd=cfg.groq.requests_per_day,
            usage_db=cfg.paths.db_path,
        )
    else:
        scoring_client = GeminiClient(
            api_key=cfg.gemini.api_key,
            model=cfg.gemini.model,
            rpm=cfg.gemini.requests_per_minute,
            rpd=cfg.gemini.requests_per_day,
            usage_db=cfg.paths.db_path,
        )

    # Ollama fallback: if enabled and not already the primary, create a fallback client
    fallback_client = None
    if cfg.ollama.enabled and cfg.tier1_provider != "ollama":
        from src.matching.ollama_client import OllamaClient
        fallback_client = OllamaClient(
            endpoint=cfg.ollama.endpoint,
            model=cfg.ollama.model,
        )
        log.info("Ollama fallback enabled (%s)", cfg.ollama.model)
    return scoring_client, fallback_client


@_exclusive("score")
def phase_score(cfg) -> dict:
    """Expire stale jobs, then score 'new' jobs with the Tier-1 LLM."""
    from src.matching.scorer import Scorer

    expired = _expire_stale(cfg)
    cv_summary = load_cv_summary(cfg)
    scoring_client, fallback_client = build_tier1_clients(cfg)

    # Build preference context from past decisions (if enough data)
    from src.feedback.preferences import build_preference_context
    user_preferences = build_preference_context(cfg.paths.db_path)
    if user_preferences:
        log.info("Injecting learned preferences into scoring prompt (%d chars)", len(user_preferences))

    scorer = Scorer(
        db_path=cfg.paths.db_path,
        client=scoring_client,
        cv_summary=cv_summary,
        score_threshold=cfg.score_threshold,
        user_preferences=user_preferences,
        fallback_client=fallback_client,
        allowed_countries=cfg.allowed_countries,
        batch_size=cfg.score_batch_size,
    )
    stats = scorer.run()
    stats["expired"] = expired
    log.info("Score phase complete: %s", stats)
    return stats


def score_single_job(cfg, job: dict):
    """Score one job dict (title, company, city, country, description) without touching its DB row."""
    from src.feedback.preferences import build_preference_context
    from src.matching.scorer import Scorer

    scoring_client, fallback_client = build_tier1_clients(cfg)
    scorer = Scorer(
        db_path=cfg.paths.db_path,
        client=scoring_client,
        cv_summary=load_cv_summary(cfg),
        score_threshold=cfg.score_threshold,
        user_preferences=build_preference_context(cfg.paths.db_path),
        fallback_client=fallback_client,
    )
    return scorer.score_single({"id": job.get("id", "adhoc"), **job})


def resolve_cv_template(name: str | None = None) -> Path:
    """
    Map a template name to its path via APPLAI_CV_TEMPLATES ("name=/path,...").
    No name returns APPLAI_CV_TEMPLATE. Raises ValueError for unknown names.
    """
    import os

    if not name:
        return Path(os.environ.get("APPLAI_CV_TEMPLATE", ""))
    templates = {}
    for entry in os.environ.get("APPLAI_CV_TEMPLATES", "").split(","):
        key, sep, path = entry.partition("=")
        if sep and key.strip() and path.strip():
            templates[key.strip()] = Path(path.strip())
    if name not in templates:
        raise ValueError(f"Unknown CV template {name!r}; available: {sorted(templates) or 'none'}")
    return templates[name]


def build_generators(cfg, cv_template_path: Path | None = None) -> tuple | None:
    """Return (cv_gen, cl_gen), or None if a template is missing."""
    from src.documents.cv_generator import CVGenerator
    from src.documents.cover_letter import CoverLetterGenerator

    import os

    # CV template
    cv_template_path = cv_template_path or resolve_cv_template()
    if not cv_template_path.exists():
        log.error(
            "CV template not found. Set APPLAI_CV_TEMPLATE env var to the path of your .tex file."
        )
        return None

    # Cover letter template
    cl_template_path = Path(os.environ.get("APPLAI_CL_TEMPLATE", ""))
    if not cl_template_path.exists():
        log.error(
            "Cover letter template not found. Set APPLAI_CL_TEMPLATE env var to the path of your .tex file."
        )
        return None

    profile_path = cfg.paths.working_dir / "profile_summary.md"
    if not profile_path.exists():
        log.warning(
            "profile_summary.md not found at %s — documents will lack candidate context",
            profile_path,
        )
        profile_path = None

    cv_gen = CVGenerator(
        template_path=cv_template_path,
        output_dir=cfg.paths.working_dir,
        profile_path=profile_path,
    )

    cl_gen = CoverLetterGenerator(
        template_path=cl_template_path,
        output_dir=cfg.paths.working_dir,
        profile_path=profile_path,
        candidate_name=os.environ.get("CANDIDATE_NAME", "Candidate Name"),
        candidate_email=os.environ.get("CANDIDATE_EMAIL", "candidate@email.com"),
        candidate_address=os.environ.get("CANDIDATE_ADDRESS", ""),
        candidate_phone=os.environ.get("CANDIDATE_PHONE", ""),
        candidate_city=os.environ.get("CANDIDATE_CITY", ""),
    )
    return cv_gen, cl_gen


def generate_one(cfg, job: dict, cv_gen, cl_gen) -> str | None:
    """
    Generate a bundle for one 'queued' job into .pending/<app_id>/ and mark it 'ready'.
    Returns the application id, or None if another run claimed the job.
    On failure the pending folder is removed, the attempt recorded, and the error re-raised.
    """
    from src.utils.file_manager import cleanup_pending, pending_dir

    job_id = job["id"]
    reasoning = job.get("score_reasoning") or ""
    pending = None

    try:
        with db.get_conn(cfg.paths.db_path) as conn:
            if not db.start_generation(conn, job_id):
                return None

        app_id = str(uuid.uuid4())
        pending = pending_dir(cfg.paths.working_dir, app_id)
        pending.mkdir(parents=True, exist_ok=True)

        cv_path = cv_gen.generate(job, gemini_reasoning=reasoning, output_folder=pending)
        cl_path = cl_gen.generate(job, gemini_reasoning=reasoning, output_folder=pending)

        with db.get_conn(cfg.paths.db_path) as conn:
            db.create_application(conn, {
                "id": app_id,
                "job_id": job_id,
                "cv_path": str(cv_path),
                "cover_letter_path": str(cl_path),
            })
            db.complete_stage(conn, job_id, "ready")
    except Exception as exc:
        if pending is not None:
            cleanup_pending(pending)
        with db.get_conn(cfg.paths.db_path) as conn:
            gave_up = db.record_failure(conn, job_id, "queued", f"{type(exc).__name__}: {exc}")
        if gave_up:
            log.error("Giving up on job %s after %d failed attempts", job_id, db.MAX_ATTEMPTS)
        raise

    log.info("Documents generated for job %s", job_id)
    return app_id


@_exclusive("generate")
def phase_generate(cfg) -> int:
    """Generate CV + cover letter for all 'queued' jobs."""
    from src.utils.file_manager import cleanup_stale_pending

    with db.get_conn(cfg.paths.db_path) as conn:
        awaiting = {r["id"] for r in conn.execute("SELECT id FROM applications WHERE user_decision IS NULL")}
    cleanup_stale_pending(cfg.paths.working_dir, keep=awaiting)
    _expire_stale(cfg)

    generators = build_generators(cfg)
    if generators is None:
        return 0
    cv_gen, cl_gen = generators

    generated = 0
    with db.get_conn(cfg.paths.db_path) as conn:
        recovered = db.recover_interrupted_generation(conn)
        jobs = db.get_jobs_by_status(conn, "queued", limit=cfg.generate_batch_size)
    if recovered:
        log.warning("Recovered %d jobs left in 'generating' by an interrupted run", recovered)

    for row in jobs:
        job = dict(row)
        try:
            if generate_one(cfg, job, cv_gen, cl_gen):
                generated += 1
        except Exception as exc:
            log.error("Document generation failed for job %s: %s", job["id"], exc, exc_info=True)

    log.info("Generate phase complete: %d application bundles created", generated)
    return generated


@_exclusive("deliver")
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
        rows = conn.execute("""
            SELECT a.*, j.title, j.company, j.country, j.city, j.score, j.score_reasoning,
                   j.source_url, j.id as job_id
            FROM applications a
            JOIN jobs j ON a.job_id = j.id
            WHERE j.status = 'ready' AND a.user_decision IS NULL AND a.discord_msg_id IS NULL
            LIMIT 10
        """).fetchall()

    try:
        for row in rows:
            app = dict(row)
            job_id = app["job_id"]
            job = {
                "id": job_id,
                "title": app["title"],
                "company": app["company"],
                "country": app["country"],
                "city": app.get("city", ""),
                "score": app["score"],
                "source_url": app["source_url"],
            }

            cv_path = Path(app["cv_path"] or "")
            cl_path = Path(app["cover_letter_path"] or "")
            try:
                if not cv_path.is_file() or not cl_path.is_file():
                    raise FileNotFoundError(f"documents missing: {cv_path}, {cl_path}")
                msg_id = discord.send_bundle(
                    app_id=app["id"],
                    job=job,
                    cv_path=cv_path,
                    cover_letter_path=cl_path,
                    score=app["score"] or 0.0,
                    reasoning=app["score_reasoning"] or "",
                )
            except Exception as exc:
                log.error("Delivery failed for app %s: %s", app["id"], exc, exc_info=True)
                with db.get_conn(cfg.paths.db_path) as conn:
                    db.record_failure(conn, job_id, "ready", f"{type(exc).__name__}: {exc}")
                continue

            # Record the send immediately: a later failure must never cause a re-send
            with db.get_conn(cfg.paths.db_path) as conn:
                conn.execute("UPDATE applications SET discord_msg_id = ? WHERE id = ?", (msg_id, app["id"]))
                db.complete_stage(conn, job_id, "delivered")
            delivered += 1

            try:
                notion_page_id = notion.log_job(job, app)
                with db.get_conn(cfg.paths.db_path) as conn:
                    conn.execute(
                        "UPDATE applications SET notion_page_id = ? WHERE id = ?", (notion_page_id, app["id"])
                    )
            except Exception as exc:
                log.error("Notion logging failed for app %s (bundle already delivered): %s", app["id"], exc)
    finally:
        discord.close()

    log.info("Deliver phase complete: %d bundles sent", delivered)
    return delivered


def run_all(cfg) -> None:
    log.info("=== ApplAI pipeline starting ===")
    phase_scrape(cfg)
    phase_score(cfg)   # always: the backlog may hold jobs from earlier runs
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

    # Local runs: read config/.env; variables already set (e.g. by Docker) win
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / "config" / ".env", override=False)

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
