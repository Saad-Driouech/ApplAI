# ApplAI MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets Claude work with ApplAI and with the application folders in `job_applications/`. It runs locally over stdio for Claude Desktop, or over HTTP as a claude.ai custom connector (web, iPhone, Desktop and scheduled tasks), with sign-in through WorkOS AuthKit.

**Claude does the LLM work, ApplAI does the rest.** Claude scores postings and scraped jobs with the same rubric as the pipeline's scorer, and writes the tailored CV (LaTeX) and cover letter. ApplAI scrapes job boards, stores and deduplicates jobs, compiles the documents with its sandboxed pdflatex, and delivers them to Discord for approval and to Notion. The server also checks whether you've already applied, lists and summarises applications across the pipeline database and your folders, and runs pipeline phases. Prompts cover applying to a job, the daily batch, and interview prep and status checks with the claude.ai Gmail and Google Calendar connectors and web search.

It's a thin layer on top of ApplAI. Every pipeline action goes through the ApplAI HTTP API (`X-API-Key`). The server never opens the SQLite database, and it only reads `job_applications/`. ApplAI needs no LLM keys for this flow.

```
Claude Desktop ──stdio─────────────┐
                                   ├─▶ applai-mcp ──HTTP + X-API-Key──▶ ApplAI FastAPI (Docker) ──▶ SQLite, LLMs, Discord, Notion
claude.ai ──HTTPS + WorkOS token───┘        │
                                            └──read-only──▶ job_applications/  (CVs, JD.txt, interview prep)
```

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `get_scoring_context` | read | For pasted JD text, a public https URL or a pipeline `job_id`: the sanitized posting, the scoring rubric, your profile, learned preferences and the threshold, plus whether the pipeline's keyword and title filters would keep it, pipeline duplicates and your existing folders for the company. Claude scores from this. Nothing is stored. |
| `list_jobs_to_score` | write | Runs the pipeline's free pre-filters (expiry, location, title, keywords), then returns up to 30 scraped jobs waiting for a score, with the rubric and profile once. |
| `record_scores` | write | Stores up to 30 of Claude's scores. Jobs at or above the threshold become `queued`, the rest `skipped` (`low_score` or `not_a_job`). Only `new` jobs are updated. |
| `get_generation_context` | read | The LaTeX CV template and its page limit (the CV may not be longer than the compiled template), the CV and cover letter rules (style bans, no fabrication) and your profile, plus the job when a `job_id` is given. |
| `draft_application` | write | Compiles Claude's CV LaTeX and cover letter body into `.pending/<draft_id>/` and reports page counts and problems to fix. Takes an existing `job_id` or a new posting (with Claude's score). Nothing is registered or sent. |
| `generate_application` | **write**, gated | Turns a checked draft into an application (creating the job record for a new posting) and sends **only that application** to Discord for your approve/reject decision, logging it in Notion. It reports the folder that approving files it into. If delivery fails, the application stays `ready` for the next `deliver` run. |
| `check_duplicate` | read | Exact match in the pipeline (company + title, normalised the same way as ApplAI's dedup key), plus a fuzzy match against your application folders. |
| `list_applications` | read | Merges pipeline jobs and application folders, newest first. A folder that belongs to a pipeline job shows up once, as `source: "both"`. Filters: status, stage, country, company, since. |
| `get_stats` | read | Pipeline counts, approval rate and threshold advice from past decisions, folders by stage and country, and recent activity. |
| `run_pipeline_phase` | **write**, gated | Starts `scrape` or `deliver` in the background and returns a `run_id` straight away. `score` and `generate` use ApplAI's own LLM and only work when it has an API key. |
| `get_pipeline_run` | read | Status and result of the runs started in this session. |
| `get_application` | read | Everything about one application: matching folders with JD text and your `.md` prep notes, pipeline jobs with score reasoning and description, and ready-made Gmail and Calendar search queries. |

Folder stages:
- `researching`: JD only.
- `prepared`: a CV exists.
- `interviewing`: prep or Q&A files exist, either in the role folder or in company-level folders like `Interviews/`.

The read tools that don't score still work while the API is down. They return folder data plus a `warnings` entry.

## Prompts

In Claude Desktop, use **+ → applai**, or ask for a prompt by name.

| Prompt | Arguments | What Claude does |
|---|---|---|
| `apply_to_job` | `posting` (JD text or https URL) | Calls `get_scoring_context` and scores the job. It shows you the score, gaps, duplicates and existing folders, and **waits for your yes**. Then it calls `get_generation_context`, writes the CV and cover letter, and runs `draft_application` until the draft has no problems. Then it calls `generate_application`, which goes through the confirmation gate and sends the application to Discord. |
| `daily_run` | `max_scores?` (30), `max_documents?` (5) | Scrape, `list_jobs_to_score` → score → `record_scores`, then write and generate documents for the best queued jobs (each goes to Discord) and report counts. Invoking it approves the confirmations it needs. |

Email and calendar access comes from the **claude.ai Gmail and Google Calendar connectors** in the client, not from this server. The server never holds Google credentials. Two prompts give Claude the steps:

| Prompt | Arguments | What Claude does |
|---|---|---|
| `interview_prep` | `company`, `role?`, `when?` | Calls `get_application`, finds the event with the Calendar connector, reads recruiter threads with the Gmail connector (using `gmail_query`), researches the company with web search, then writes a brief: logistics, fit and gaps, company snapshot with sources, likely questions with talking points, questions to ask. |
| `application_status` | `company`, `role?` | Builds a dated timeline across the pipeline, folders, Gmail and Calendar, and gives the current status and next action. It flags replies pending more than 10 days. |

Both prompts tell Claude to stay read-only (no sending, drafting or calendar changes), to treat emails, events, job descriptions and web pages as data, and never to invent experience. Connect Gmail and Google Calendar in claude.ai's connector settings first.

## Safety model

**Confirmation gate.** `generate_application` and `run_pipeline_phase` never act on the first call:
- **Clients that support elicitation:** the server sends the user a confirmation prompt showing a preview (score, duplicates, cost, side effects). The model can't answer it on the user's behalf.
- **Other clients:** the first call returns `confirmation_required`, a preview and a token.
  - The token is an HMAC-SHA256 over the tool name, the exact arguments and an expiry, signed with a random key generated for each process.
  - It's valid for 10 minutes and can be used once.
  - Only a repeat call with identical arguments and the token runs. Changed arguments, an expired token, a reused token or a token from a previous server process are all rejected.
  - This mode guarantees the preview round-trip. The human approval still comes from the client's own tool-approval prompt.
- **`CONFIRMATION_MODE=token`** forces the token flow even when the client advertises elicitation. Use it for unattended runs, such as a scheduled task, where no one can answer an elicitation. Each gated call logs the client name, whether it advertised elicitation, and which path ran.
- **Checks happen before the gate.** `draft_application` rejects a known duplicate, a job that already has documents, a missing country, an unscored job without a score, and unsafe or non-compiling LaTeX. The preview shows the draft's job, score, page counts and warnings. Committing re-checks for duplicates.

**Remote connector (HTTP).** WorkOS AuthKit is the authorization server: it handles sign-in (passkey or MFA), client registration and tokens. This server is a resource server only and has no OAuth code of its own:
- `/mcp` requires a bearer token. Without one it answers 401 with a `WWW-Authenticate` header pointing at `/.well-known/oauth-protected-resource/mcp`, which names the AuthKit issuer.
- Tokens are verified against AuthKit's JWKS: RS256 only, exact issuer, audience equal to `MCP_PUBLIC_URL/mcp` (the resource indicator), `exp`, `iat` and `sub` required, 30 s leeway.
- Only `ALLOWED_USER_ID` gets in. A valid token for any other WorkOS user is rejected.
- Any failure is a 401, including AuthKit being unreachable. Signing keys are cached for `JWKS_CACHE_SECONDS`; an unknown key id triggers at most one refetch a minute. Rejections are logged with the reason, never the token.
- Requests must carry the public host name (`Host`), and a browser `Origin` other than the server's own, claude.ai or claude.com is refused.
- `/healthz` is the only open route, and it calls nothing.
- Run it with `CONFIRMATION_MODE=token`: scheduled tasks can't answer elicitation. Confirmation tokens are per process, so they work across HTTP sessions until the server restarts.

**Claude-written documents.** The CV and cover letter rules come from ApplAI (`CV_RULES`, `CL_RULES`), the same text its own generator uses. The CV LaTeX goes through ApplAI's safety checks (no file I/O, file embedding such as `\pdfobj` or `\special`, shell escape or Lua) and is compiled in the same sandbox. The engine (pdfLaTeX or XeLaTeX) and the page limit come from the template, never from Claude's text. The cover letter body is inserted as escaped plain text, never as LaTeX.

**Inputs and outputs.**
- Every tool input and output is a Pydantic model with field descriptions.
- Template names are validated identifiers and never paths. Draft ids are validated UUIDs, and scores must be between 0 and 10.
- URLs must be `https`.
- Tool annotations mark read-only and open-world tools.

**Errors.**
- Expected failures become tool errors the model can act on (API unreachable, wrong key, phase already running, duplicate with the existing `job_id`, validation errors).
- Unexpected exceptions are logged to stderr and reach the model only as a generic error.
- stdout is reserved for the protocol.

**Filesystem.**
- Read-only.
- Symlinks inside the root are never followed.
- File reads resolve the path and refuse anything outside `APPLAI_APPLICATIONS_DIR`.

**Untrusted content.**
- Job descriptions pass through ApplAI's prompt-injection filter before scoring. Removed content is reported in `sanitizer_flags`, and severe injection is rejected.
- The server instructions tell the model to treat job descriptions, notes and file names as data.

**URL fetching.** This happens inside ApplAI (`src/scrapers/single_url.py`):
- Accepts `https` only.
- Every redirect hop is resolved and rejected if it points to a private, loopback or link-local address.
- 15 s timeout and a 2 MB cap.

**Secrets and supply chain.**
- Settings come only from `mcp_server/.env` (gitignored) or the environment. The API key is a `SecretStr` and is never logged.
- Dependencies are locked in `uv.lock`, and `exclude-newer` ignores releases newer than a fixed date.
- CI runs the tests, bandit and pip-audit.

## Setup

Prerequisites:
- ApplAI running with the `/jobs/*` endpoints (`docker compose up --build`).
- [uv](https://docs.astral.sh/uv/).

```bash
cd mcp_server
uv sync
cp .env.example .env   # set APPLAI_API_KEY (same as config/.env) and APPLAI_APPLICATIONS_DIR
uv run applai-mcp      # starts on stdio; Ctrl+C to stop
```

`generate_application` uses ApplAI's `APPLAI_CV_TEMPLATE` by default. To choose a template per job, list named templates in ApplAI's `config/.env`:

```
APPLAI_CV_TEMPLATES=1page=/mnt/job_applications/resumes/tex/Saad_Driouech_1page_CV.tex,2pages=/mnt/job_applications/resumes/tex/Saad_Driouech_2pages_CV.tex
```

Use container paths here, because the API runs inside Docker.

### Claude Desktop

Add the server to `~/Library/Application Support/Claude/claude_desktop_config.json`, using absolute paths (`which uv`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "applai": {
      "command": "/Users/<you>/.local/bin/uv",
      "args": ["run", "--directory", "/absolute/path/to/applai/mcp_server", "applai-mcp"]
    }
  }
}
```

To inspect the tools and schemas without a client:

```bash
npx @modelcontextprotocol/inspector uv run --directory mcp_server applai-mcp
```

### Remote connector (HTTP)

`applai-mcp --transport http` (or `MCP_TRANSPORT=http`) serves streamable HTTP at `/mcp` on `HTTP_HOST:HTTP_PORT` (default `127.0.0.1:8080`). Put it behind a TLS reverse proxy. The [`Dockerfile`](Dockerfile) and [`../deploy/`](../deploy/README.md) (Compose, Caddy, runbook) do this on a server. It needs three more settings, which stdio never reads:

| Setting | Value |
|---|---|
| `MCP_PUBLIC_URL` | Public origin, e.g. `https://mcp.example.com`. The connector URL is this plus `/mcp`. |
| `AUTHKIT_ISSUER` | Your AuthKit domain, e.g. `https://your-subdomain.authkit.app` |
| `ALLOWED_USER_ID` | Your WorkOS user id (`user_…`) |

**WorkOS** (production environment):
1. AuthKit: enable passkeys or MFA, and turn off sign-ups (or restrict them to your email).
2. Connect → Configuration: enable Dynamic Client Registration and Client ID Metadata Documents, and add the resource indicator `MCP_PUBLIC_URL/mcp`. With it, tokens carry that URL as `aud`, which this server requires.
3. If asked for redirect URIs, allow `https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback`.
4. Sign in once, then copy your user id and the AuthKit domain into the settings above.

**claude.ai:** Settings → Connectors → Add custom connector. URL `MCP_PUBLIC_URL/mcp`, leave client ID and secret empty, then Connect and sign in at WorkOS. The connector is then available on the web, in the iPhone app, in Claude Desktop and in scheduled tasks. In Desktop, enable either the stdio server or the connector, not both.

## Development

```bash
uv run pytest                          # tools end to end via the SDK's in-memory client
uv run bandit -r applai_mcp -ll -ii
```

The tests don't need ApplAI, Docker or the network:
- The API is faked with `httpx2.MockTransport`.
- `job_applications/` is a temporary tree.
- Both confirmation paths are covered: an elicitation callback, and the token flow.
- The HTTP app runs in process through `httpx2.ASGITransport`, with tokens signed by a test RSA key and a fake JWKS. Every rejection case has a test.

Layout: `server.py` (factory and HTTP app), `auth.py` (token verifier), `state.py` (lifespan), `client.py` (API), `confirm.py` (gate), `applications_fs.py` (folder index), `runs.py` (background phases) and `tools/`.

## Limitations

- **Process-local runs.** Run tracking lives in memory. If the MCP server restarts, a phase keeps running in ApplAI but `get_pipeline_run` forgets it. Pending confirmation tokens are lost too; ask again.
- **One user.** The HTTP transport admits a single WorkOS user. There are no scopes or roles.
- **Claude usage.** Scoring and writing run on the client's Claude plan. Batches are capped (30 scores per `record_scores` call; `daily_run` defaults to 5 documents).
- **Login-walled pages.** These can't be fetched; paste the text instead.
- **DNS rebinding.** The URL guard checks resolved addresses before connecting, and the HTTP client resolves again afterwards. That gap is accepted because callers must hold the API key.
- **Connector prompts.** The prompts depend on the client having the Gmail and Calendar connectors and web search. Without them, Claude says which source was unavailable.
