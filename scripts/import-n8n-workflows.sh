#!/usr/bin/env bash
# Import all workflow JSON files into n8n via the REST API.
#
# Usage:
#   N8N_API_KEY=<your-key> ./scripts/import-n8n-workflows.sh
#
# Generate an API key: n8n UI → Settings → n8n API → Create an API Key
# Default URL is http://localhost:5678 — override with N8N_URL env var.

set -euo pipefail

N8N_URL="${N8N_URL:-http://localhost:5678}"
WORKFLOWS_DIR="$(dirname "$0")/../n8n/workflows"

if [[ -z "${N8N_API_KEY:-}" ]]; then
  echo "Error: N8N_API_KEY is not set." >&2
  exit 1
fi

# The public API accepts only these fields (`active`, `meta`, ... are rejected)
API_FIELDS='["name", "nodes", "connections", "settings", "staticData"]'

for file in "$WORKFLOWS_DIR"/*.json; do
  body=$(python3 -c 'import json, sys; wf = json.load(open(sys.argv[1])); print(json.dumps({k: wf[k] for k in json.loads(sys.argv[2]) if k in wf}))' "$file" "$API_FIELDS")
  echo "Importing: $(basename "$file")"
  printf '%s' "$body" | curl -sf -X POST "$N8N_URL/api/v1/workflows" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    -H "Content-Type: application/json" \
    -d @- | python3 -c 'import json, sys; d = json.load(sys.stdin); print(f"  -> id={d[\"id\"]} name={d[\"name\"]}")'
done

echo "In n8n, open each HTTP node once to confirm the \"ApplAI API key\" credential is selected."

echo "Done."
