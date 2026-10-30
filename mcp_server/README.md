# ApplAI MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets Claude Desktop (or any MCP client) work with ApplAI and with the application folders in `job_applications/`. It can score a posting against your profile, check whether you've already applied, list and summarise applications across the pipeline database and your folders, generate a tailored CV and cover letter, and run pipeline phases.

It's a thin layer on top of ApplAI. Every pipeline action goes through the ApplAI HTTP API (`X-API-Key`). The server never opens the SQLite database, and it only reads `job_applications/`.

```
Claude Desktop ──stdio──▶ applai-mcp ──HTTP + X-API-Key──▶ ApplAI FastAPI (Docker) ──▶ SQLite, LLMs, Discord, Notion
                              │
                              └──read-only──▶ job_applications/  (CVs, JD.txt, interview prep)
```

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `score_job` | read | Scores pasted JD text or a public https URL from 0 to 10. It reports gaps, must-haves, whether the pipeline's keyword filter would keep it, pipeline duplicates and your existing folders for the company. Nothing is stored. |
| `check_duplicate` | read | Exact match in the pipeline (company + title, normalised the same way as ApplAI's dedup key), plus a fuzzy match against your application folders. |
| `list_applications` | read | Merges pipeline jobs and application folders, newest first. A folder that belongs to a pipeline job shows up once, as `source: "both"`. Filters: status, stage, country, company, since. |
| `get_stats` | read | Pipeline counts, approval rate and threshold advice from past decisions, folders by stage and country, and recent activity. |
| `generate_application` | **write**, gated | Generates a CV and cover letter for an existing `job_id` or a new posting, into `.pending/`. The next `deliver` run posts it to Discord for approval, the same as any pipeline job. |
| `run_pipeline_phase` | **write**, gated | Starts `scrape`, `score`, `generate` or `deliver` in the background and returns a `run_id` straight away. |
| `get_pipeline_run` | read | Status and result of the runs started in this session. |

Folder stages:
- `researching`: JD only.
- `prepared`: a CV exists.
- `interviewing`: prep or Q&A files exist, either in the role folder or in company-level folders like `Interviews/`.

The three read tools that don't score still work while the API is down. They return folder data plus a `warnings` entry.

## Safety model

**Confirmation gate.** `generate_application` and `run_pipeline_phase` never act on the first call:
- **Clients that support elicitation:** the server sends the user a confirmation prompt showing a preview (score, duplicates, cost, side effects). The model can't answer it on the user's behalf.
- **Other clients:** the first call returns `confirmation_required`, a preview and a token.
  - The token is an HMAC-SHA256 over the tool name, the exact arguments and an expiry, signed with a random key generated for each process.
  - It's valid for 10 minutes and can be used once.
  - Only a repeat call with identical arguments and the token runs. Changed arguments, an expired token, a reused token or a token from a previous server process are all rejected.
  - This mode guarantees the preview round-trip. The human approval still comes from the client's own tool-approval prompt.
- **Previews check before anything is spent.** A known duplicate, a job that already has documents, or a missing country fails at preview time.

**Inputs and outputs.**
- Every tool input and output is a Pydantic model with field descriptions.
- Template names are validated identifiers and never paths.
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

## Development

```bash
uv run pytest                          # tools end to end via the SDK's in-memory client
uv run bandit -r applai_mcp -ll -ii
```

The tests don't need ApplAI, Docker or the network:
- The API is faked with `httpx2.MockTransport`.
- `job_applications/` is a temporary tree.
- Both confirmation paths are covered: an elicitation callback, and the token flow.

Layout: `server.py` (factory), `state.py` (lifespan), `client.py` (API), `confirm.py` (gate), `applications_fs.py` (folder index), `runs.py` (background phases) and `tools/`.

## Limitations

- **Process-local runs.** Run tracking lives in memory. If the MCP server restarts, a phase keeps running in ApplAI but `get_pipeline_run` forgets it.
- **Extra scoring cost.** Previewing a new posting costs one scoring call, and generation scores it once more server-side.
- **Login-walled pages.** These can't be fetched; paste the text instead.
- **DNS rebinding.** The URL guard checks resolved addresses before connecting, and the HTTP client resolves again afterwards. That gap is accepted because callers must hold the API key.
- **Gmail and Calendar (Phase 2).** Not part of this release.
