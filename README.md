# ApplAI

**Automated job application pipeline for AI/ML/Data Science roles.**

Scrapes job boards, scores relevance with LLMs, generates tailored CVs and cover letters, and delivers ready-to-apply packages via Discord for human review. You submit manually — the system never logs into anything as you, stores no passwords, and runs no browser automation.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" alt="ApplAI Architecture" width="800"/>
</p>

## Pipeline Flow

<p align="center">
  <img src="docs/pipeline-flow.svg" alt="Pipeline Flow" width="700"/>
</p>

## Features

- **Multi-source scraping** — Arbeitnow (Germany), Remotive (global remote), RemoteOK (global remote), Adzuna (DE/AE/NL/CH, optional API key), with extensible scraper base class
- **LLM-powered scoring** — Gemini 2.5 Flash (free tier) or Groq as Tier 1 scorer; configurable threshold (default 6.0/10)
- **AI keyword pre-filter** — Jobs without AI/ML keywords in title or description are auto-skipped before LLM scoring, saving API quota
- **Document generation** — Anthropic API tailors a LaTeX CV and a LaTeX cover letter, both compiled to PDF
- **Pending folder workflow** — Documents generated into `.pending/{app_id}/`, promoted to `{country}/{company}/` on approve, deleted on reject
- **Discord review gate** — Approve/Reject buttons on each application bundle (two PDFs + job link); decisions sync to Notion and database
- **Notion dashboard** — Tracks every application with status, score, country, and document links
- **Security-first design** — 5-stage JD sanitizer, LaTeX safety checks, Ed25519 Discord signature verification, parameterized SQL
- **n8n orchestration** — 12-hour cron workflow triggers all pipeline phases via HTTP
- **Natural writing style** — Strict anti-AI-slop rules in prompts (no em dashes, no "leverage/spearheaded/synergy", direct tone)
- **Feedback loop** — Learns from your approve/reject decisions: extracts title/country/role preferences and injects them into future scoring prompts
- **Skipped jobs digest** — Periodic Discord digest of recently skipped jobs with Rescue buttons to recover false negatives
- **Keyword filter suggestions** — Analyzes rescued jobs to suggest new keywords for the AI pre-filter
- **Country-based output** — Documents saved in `{country}/{company}/` matching your existing job applications folder structure
- **Dual LLM provider support** — Anthropic (production) or Groq (testing) for document generation, selectable via env var

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Orchestration | n8n 1.91.3 (self-hosted) |
| API | FastAPI + Uvicorn |
| Scoring (Tier 1) | Gemini 2.5 Flash / Groq (Llama 3.3 70B) |
| Documents (Tier 2) | Anthropic API (Claude Sonnet) |
| CV Compilation | pdflatex (texlive) |
| Cover Letters | pdflatex (texlive) |
| Database | SQLite |
| Delivery | Discord Bot API + Notion API |
| Security | Ed25519 signatures, bleach, JD sanitizer |
| Container | Docker Compose |

## Quick Start

### Prerequisites

- Docker and Docker Compose
- API keys: Google AI (Gemini), Anthropic, Discord bot token, Notion integration
- A LaTeX CV template (`.tex` file) and a LaTeX cover letter template (`.tex` file)
- A profile summary (`profile_summary.md` in your working directory) — used for both scoring and document generation

### Setup

```bash
git clone https://github.com/Saad-Driouech/applai.git
cd applai

# Configure
cp config/.env.example config/.env
# Edit config/.env with your API keys and paths, and set APPLAI_API_KEY:
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Start services
docker compose up -d --build

# Verify setup
chmod +x scripts/verify-setup.sh
./scripts/verify-setup.sh

# Open n8n, create the owner account, then add a Header Auth credential named
# "ApplAI API key" (Name: X-API-Key, Value: your APPLAI_API_KEY) and import
# n8n/workflows/pipeline.json via the UI
open http://localhost:5678
```

The dashboard at http://localhost:8000 asks for the API key once and remembers it in the browser.

### Discord Setup

1. Create a Discord application at [discord.com/developers](https://discord.com/developers/applications)
2. Create a bot, copy the token to `DISCORD_BOT_TOKEN`
3. Copy the application's public key to `DISCORD_PUBLIC_KEY`
4. Invite the bot to your server with Send Messages + Attach Files permissions
5. Set the Interactions Endpoint URL to your public URL + `/discord/interactions`
   - Use [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) for tunneling:
     ```bash
     cloudflared tunnel --url http://localhost:8000
     ```
   - Only `/discord/interactions` (signature-verified) and `/health` work without the API key
6. Optionally set `DISCORD_ALLOWED_USER_IDS` so only you can Approve/Reject

## Configuration

All configuration is via environment variables in `config/.env`. See [`config/.env.example`](config/.env.example) for the full list.

| Variable | Purpose | Default |
|----------|---------|---------|
| `APPLAI_API_KEY` | Required `X-API-Key` for all API routes except `/health` and the Discord webhook | — |
| `LLM_TIER1_PROVIDER` | Scoring LLM (`gemini`, `groq` or `ollama`) | `gemini` |
| `DOCUMENT_LLM_PROVIDER` | Document generation LLM (`anthropic` or `groq`) | `anthropic` |
| `SCORE_THRESHOLD` | Minimum score to queue a job (0-10) | `6.0` |
| `JOB_MAX_AGE_DAYS` | Unscored/ungenerated jobs older than this expire | `30` |
| `SCORE_BATCH_SIZE` | Max LLM scoring calls per run | `50` |
| `GENERATE_BATCH_SIZE` | Max document bundles generated per run | `10` |
| `DISCORD_ALLOWED_USER_IDS` | Comma-separated Discord user IDs allowed to decide | anyone |
| `APPLAI_WORKING_DIR` | Directory for job applications and profile files | — |
| `APPLAI_CV_TEMPLATE` | Path to your LaTeX CV template | — |
| `APPLAI_CL_TEMPLATE` | Path to your LaTeX cover letter template | — |
| `CANDIDATE_NAME` | Your full name (used in cover letters) | — |
| `CANDIDATE_EMAIL` | Your email | — |
| `CANDIDATE_PHONE` | Your phone number | — |
| `CANDIDATE_ADDRESS` | Your address line | — |
| `CANDIDATE_CITY` | City you're writing from | — |
| `ADZUNA_APP_ID` | Adzuna API app ID (optional) | — |
| `ADZUNA_APP_KEY` | Adzuna API app key (optional) | — |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Liveness check |
| `GET` | `/stats` | Job counts grouped by status |
| `POST` | `/scrape` | Run all active scrapers |
| `POST` | `/score` | Score new jobs via Tier 1 LLM |
| `POST` | `/generate` | Generate CV + cover letter for queued jobs |
| `POST` | `/deliver` | Send ready bundles to Discord + Notion |
| `POST` | `/digest` | Send skipped jobs digest to Discord for review |
| `GET` | `/feedback` | Approval stats, threshold recommendation, keyword suggestions |
| `POST` | `/discord/interactions` | Discord button webhook (Approve/Reject/Rescue) |
| `POST` | `/jobs/score` | Score one job (pasted text or public https URL) without storing it |
| `POST` | `/jobs/generate` | Generate a bundle for one job (`job_id` or a new job) into `.pending/`; delivery and approval follow the normal flow |
| `GET` | `/jobs` | Jobs filtered by `status`, `country`, `company`, `since`, `min_score` |
| `GET` | `/jobs/duplicates` | Jobs matching a `company` + `title` after dedup normalization |
| `GET` | `/jobs/{job_id}` | One job with its latest application |

All routes except `/health` and `/discord/interactions` require the `X-API-Key` header. A phase that is already running returns `409`. The `/jobs/*` routes back the MCP server and use typed request/response models (see `/docs`).

### MCP server

[`mcp_server/`](mcp_server/README.md) exposes ApplAI to Claude Desktop and other MCP clients: score or dedupe a posting, list applications across the pipeline and `job_applications/`, and generate documents or run phases behind a human confirmation gate.

### Job statuses

`new` → `queued` → `generating` → `ready` → `delivered` (sent to Discord) → `approved` / `rejected`.
Jobs leave the flow as `skipped` (with a `skip_reason`: `keyword_filter`, `location`, `low_score`, `not_a_job`, `duplicate`, `expired`) or `failed` after 3 failed attempts (see `last_error`).

## Project Structure

```
applai/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── config/
│   └── .env.example
├── src/
│   ├── api.py                  # FastAPI endpoints
│   ├── pipeline.py             # Pipeline orchestrator (4 phases)
│   ├── claude_bridge.py        # Safe Anthropic/Groq API bridge
│   ├── config.py               # Dataclass-based configuration
│   ├── database.py             # SQLite with parameterized queries
│   ├── logger.py               # Structured logging + audit trail
│   ├── scrapers/
│   │   ├── base.py             # Abstract base with retry + dedup
│   │   ├── arbeitnow.py        # Germany tech jobs
│   │   ├── remotive.py         # Global remote jobs (free API)
│   │   ├── remoteok.py         # Global remote jobs (free public API)
│   │   └── adzuna.py           # DE/AE/NL/CH jobs (free API key required)
│   ├── matching/
│   │   ├── scorer.py           # Scoring orchestrator + keyword pre-filter
│   │   ├── gemini_client.py    # Google Gemini integration
│   │   └── groq_client.py      # Groq integration
│   ├── documents/
│   │   ├── cv_generator.py     # LaTeX CV → PDF
│   │   └── cover_letter.py     # LaTeX cover letter → PDF
│   ├── delivery/
│   │   ├── discord_bot.py      # Discord delivery + digest + buttons
│   │   └── notion_tracker.py   # Notion page management
│   ├── feedback/
│   │   ├── analyzer.py         # Threshold recommendation from decisions
│   │   └── preferences.py      # Preference learning + keyword suggestions
│   └── utils/
│       ├── file_manager.py     # Pending/final document workflow
│       ├── jd_sanitizer.py     # 5-stage JD sanitization
│       ├── latex_safety.py     # LaTeX validation
│       └── sanitize.py         # Scraper input sanitization
├── scripts/
│   ├── verify-setup.sh         # Post-deploy verification
│   └── import-n8n-workflows.sh # n8n workflow import via API
├── n8n/
│   └── workflows/
│       └── pipeline.json       # 12h cron workflow
├── tests/
│   ├── test_config.py
│   ├── test_database.py
│   ├── test_jd_sanitizer.py
│   └── test_latex_safety.py
└── data/                       # gitignored
    ├── db/                     # SQLite database
    └── n8n/                    # n8n persistent data
```

## Security

- **No auto-submission** — System prepares everything; you click submit
- **JD sanitization** — 5-stage pipeline strips prompt injection, hidden Unicode, and LaTeX exploits before any LLM sees the text
- **API key** — All pipeline and data routes require `X-API-Key` (constant-time compare, fails closed when unset)
- **LaTeX safety** — Generated `.tex` files scanned for file-I/O and code-execution primitives, then compiled by a sandboxed pdflatex (paranoid file access, no secrets in its environment, `--no-shell-escape`)
- **Discord verification** — Ed25519 signature verification, a 5-minute replay window, and an optional user allowlist
- **Unprivileged containers** — The API runs as a non-root user with all capabilities dropped
- **n8n hardened** — Environment variable access blocked, community packages disabled, capability drops, no-new-privileges
- **Parameterized SQL** — All database queries use parameterized statements
- **Localhost-only ports** — Both `8000` (API) and `5678` (n8n) bound to `127.0.0.1`
- **CI scanning** — `pip-audit`, `bandit` and `detect-secrets` run on every push

## Current Progress

- [x] Job scraping (Arbeitnow, Remotive, RemoteOK, Adzuna)
- [x] LLM scoring (Gemini Flash, Groq)
- [x] CV generation (LaTeX → PDF via Anthropic API)
- [x] Cover letter generation (LaTeX → PDF via Anthropic API)
- [x] Discord delivery with Approve/Reject buttons
- [x] Discord interactions endpoint (Ed25519 verified)
- [x] Notion tracking dashboard
- [x] n8n 12h cron workflow
- [x] End-to-end pipeline tested
- [x] AI keyword pre-filter (saves LLM quota)
- [x] Feedback loop (preference learning, threshold recommendations)
- [x] Skipped jobs digest with rescue flow
- [x] Ollama fallback for offline/free LLM scoring
- [x] Additional scraper sources (RemoteOK, Adzuna)

## Cost

| Component | Cost |
|-----------|------|
| Gemini 2.5 Flash (scoring) | Free tier (1,000 req/day) |
| Anthropic API (documents) | ~$0.02–0.05 per CV+cover letter pair |
| n8n (orchestration) | Free (self-hosted) |
| Discord + Notion | Free |
| **Total** | **~$0–15/month** depending on volume |
