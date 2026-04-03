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

- **Multi-source scraping** — Arbeitnow (Germany-focused), Remotive (global remote), with extensible scraper base class
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
# Edit config/.env with your API keys and paths

# Start services
docker compose up -d

# Verify setup
chmod +x scripts/verify-setup.sh
./scripts/verify-setup.sh

# Open n8n to import the pipeline workflow
open http://localhost:5678
# Import n8n/workflows/pipeline.json via the UI
```

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

## Configuration

All configuration is via environment variables in `config/.env`. See [`config/.env.example`](config/.env.example) for the full list.

| Variable | Purpose | Default |
|----------|---------|---------|
| `LLM_TIER1_PROVIDER` | Scoring LLM (`gemini` or `groq`) | `gemini` |
| `DOCUMENT_LLM_PROVIDER` | Document generation LLM (`anthropic` or `groq`) | `anthropic` |
| `SCORE_THRESHOLD` | Minimum score to queue a job (0-10) | `6.0` |
| `APPLAI_WORKING_DIR` | Directory for job applications and profile files | — |
| `APPLAI_CV_TEMPLATE` | Path to your LaTeX CV template | — |
| `APPLAI_CL_TEMPLATE` | Path to your LaTeX cover letter template | — |
| `CANDIDATE_NAME` | Your full name (used in cover letters) | — |
| `CANDIDATE_EMAIL` | Your email | — |
| `CANDIDATE_PHONE` | Your phone number | — |
| `CANDIDATE_ADDRESS` | Your address line | — |
| `CANDIDATE_CITY` | City you're writing from | — |

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
│   │   └── remotive.py         # Global remote jobs
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
- **LaTeX safety** — Generated `.tex` files scanned for dangerous primitives (`\write18`, `\input`, `\include`), compiled with `--no-shell-escape`
- **Discord verification** — Ed25519 signature verification on every interaction webhook
- **n8n hardened** — Environment variable access blocked, community packages disabled, capability drops, no-new-privileges
- **Parameterized SQL** — All database queries use parameterized statements
- **Localhost-only ports** — Both `8000` (API) and `5678` (n8n) bound to `127.0.0.1`

See [SECURITY.md](SECURITY.md) for the full threat model and mitigation details.

## Current Progress

- [x] Job scraping (Arbeitnow, Remotive)
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
- [ ] Ollama fallback for offline/free LLM scoring
- [ ] Additional scraper sources

## Cost

| Component | Cost |
|-----------|------|
| Gemini 2.5 Flash (scoring) | Free tier (1,000 req/day) |
| Anthropic API (documents) | ~$0.02–0.05 per CV+cover letter pair |
| n8n (orchestration) | Free (self-hosted) |
| Discord + Notion | Free |
| **Total** | **~$0–15/month** depending on volume |
