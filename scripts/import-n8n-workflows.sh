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

for file in "$WORKFLOWS_DIR"/*.json; do
  name=$(python3 -c "import json,sys; print(json.load(open('$file'))['name'])")
  echo "Importing: $name"
  curl -sf -X POST "$N8N_URL/api/v1/workflows" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    -H "Content-Type: application/json" \
    -d @"$file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'  → id={d[\"id\"]} name={d[\"name\"]}')"
done

echo "Done."
