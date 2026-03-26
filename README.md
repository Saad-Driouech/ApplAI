# ApplAI

Job intelligence and document preparation pipeline for AI/ML/Data Science roles.

Discovers jobs across 20+ sources in 6 countries, scores them with Gemini Flash,
tailors your CV and cover letter with Claude Code, and delivers a ready-to-apply
package via Telegram. **You submit manually** — the system never logs into anything
as you, stores no passwords, and runs no browser automation.

## How it works

```
Scrape 20+ job boards (every 12h)
  → Sanitize JDs (strip prompt injection, hidden text)
  → Score with Gemini 2.5 Flash (free tier)
  → Notify you via Telegram with top matches
  → You approve → Claude Code tailors CV (.tex→PDF) + cover letter (.docx)
  → LaTeX safety check → You review documents in Telegram
  → Delivery package: apply link + documents + copy-paste fields
  → You submit (2 min) → Reply /applied → Notion tracks everything
```

## Architecture

```
n8n (orchestrator)
 ├── Job Scrapers → jd_sanitizer.py → SQLite + Notion
 ├── Gemini 2.5 Flash (Tier 1: scoring, research, follow-ups)
 ├── Claude Code (Tier 2: CV tailoring + cover letters only)
 │   └── via claude_bridge.py (sanitized, no shell injection)
 ├── latex_safety.py → pdflatex --no-shell-escape
 ├── Telegram Bot (notifications + permanent document review gate)
 └── Notion (dashboard + tracking)
```

## Security model

- **No auto-submission**: System prepares everything, you click submit
- **No stored credentials**: No password vault, no browser automation
- **JD sanitization**: Three-stage pipeline strips prompt injection before any LLM sees the text
- **LaTeX safety**: Generated .tex files scanned for dangerous primitives, compiled with --no-shell-escape
- **Claude isolation**: Invoked via `claude_bridge.py` — never raw shell interpolation
- **Permanent review gate**: You review every CV and cover letter before it goes anywhere
- **n8n hardened**: Env var blocking, community packages disabled, capability drops

## Quick start

```bash
cp config/.env.example .env        # Fill in your tokens
docker compose up -d               # Start n8n
chmod +x scripts/verify-setup.sh
./scripts/verify-setup.sh          # Verify everything works
open http://localhost:5678          # Open n8n
```

## Project structure

```
applai/
├── docker-compose.yml
├── config/
│   └── .env.example
├── src/
│   ├── claude_bridge.py            # Safe Claude Code wrapper
│   ├── scrapers/                   # Job board fetchers
│   ├── matching/                   # Gemini scoring integration
│   ├── documents/                  # Document generation helpers
│   ├── feedback/                   # Learning system
│   └── utils/
│       ├── jd_sanitizer.py         # Stage 1: JD safety
│       ├── latex_safety.py         # LaTeX validation + safe compilation
│       ├── sanitize.py             # Scraper input sanitization
│       └── database.py             # Parameterized SQL only
├── scripts/
│   └── verify-setup.sh
├── data/
│   ├── n8n/                        # (gitignored)
│   └── db/                         # (gitignored)
├── tests/
├── SECURITY.md
└── README.md
```

## Cost

| Component | Cost |
|-----------|------|
| Gemini 2.5 Flash (scoring) | Free tier (1,000 req/day) |
| Claude Code (documents) | Included in Claude Pro |
| n8n | Free (self-hosted) |
| **Total** | **~$0/month** (free tier) to **~$15/month** (at scale) |
