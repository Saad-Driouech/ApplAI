#!/usr/bin/env bash
# applai/scripts/verify-setup.sh — Run after docker compose up
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }

echo "══════════════════════════════════════"
echo "  ApplAI v5 Setup Verification"
echo "══════════════════════════════════════"
echo ""

# .env check
echo "── Configuration ──"
if [ -f .env ]; then
    if grep -q "CHANGE_ME" .env; then
        fail ".env has default password — change N8N_PASSWORD"
    else
        pass ".env configured"
    fi
    if grep -q "GOOGLE_AI_API_KEY=." .env; then
        pass "Gemini API key set"
    else
        warn "Gemini API key not set — get one at https://aistudio.google.com/apikey"
    fi
else
    fail ".env missing — cp config/.env.example .env"
fi

# git-secrets
echo ""
echo "── Security ──"
if command -v git-secrets &>/dev/null; then
    pass "git-secrets installed"
else
    warn "git-secrets not installed — brew install git-secrets"
fi

if git check-ignore .env >/dev/null 2>&1; then
    pass ".env is gitignored"
else
    fail ".env is NOT gitignored — SECURITY RISK"
fi

# Docker
echo ""
echo "── Docker ──"
if docker info >/dev/null 2>&1; then
    pass "Docker running"
else
    fail "Docker not running"; exit 1
fi

if docker ps --format '{{.Names}}' | grep -q applai-n8n; then
    pass "n8n container running"
else
    fail "n8n container not found — docker compose up -d"
fi

# Port binding
PORT_BIND=$(docker port applai-n8n 5678 2>/dev/null || echo "")
if echo "$PORT_BIND" | grep -q "127.0.0.1"; then
    pass "Port bound to localhost only"
elif echo "$PORT_BIND" | grep -q "0.0.0.0"; then
    fail "Port exposed to network — fix docker-compose.yml"
fi

# n8n accessible
if curl -s -o /dev/null -w "%{http_code}" http://localhost:5678 2>/dev/null | grep -qE "200|401"; then
    pass "n8n accessible at http://localhost:5678"
else
    warn "n8n not responding yet (may still be starting)"
fi

# Safety modules
echo ""
echo "── Safety Modules ──"
for f in src/utils/jd_sanitizer.py src/utils/latex_safety.py src/utils/sanitize.py src/claude_bridge.py; do
    if [ -f "$f" ]; then
        pass "$f exists"
    else
        fail "$f missing"
    fi
done

# Data directories
echo ""
echo "── Data Directories ──"
for dir in data/n8n data/db; do
    [ -d "$dir" ] && pass "$dir/" || { mkdir -p "$dir"; pass "$dir/ created"; }
done

# No credential directory (v5 doesn't use one)
if [ -d "data/credentials" ]; then
    warn "data/credentials/ exists but v5 doesn't use credential storage — safe to remove"
fi

echo ""
echo "══════════════════════════════════════"
echo "  Next steps:"
echo "  1. Open http://localhost:5678"
echo "  2. Test Gemini: curl with your API key"
echo "  3. Set up Telegram + Notion in n8n"
echo "══════════════════════════════════════"
