#!/usr/bin/env bash
# Create (or recreate) the Hyperion LiteLLM virtual key.
# Requires: LITELLM_MASTER_KEY exported, LiteLLM running on localhost:4000.
#
# Usage:
#   export LITELLM_MASTER_KEY=sk-...
#   bash agents/hyperion/scripts/create_hyperion_key.sh
#
# The script prints the new key; copy it to agents/hyperion/.env as
# LITELLM_HYPERION_KEY=<value> and also to ai-router/.env as LITELLM_HYPERION_KEY.

set -euo pipefail

LITELLM_URL="${LITELLM_URL:-http://localhost:4000}"

if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "ERROR: LITELLM_MASTER_KEY is not set." >&2
  exit 1
fi

RESPONSE=$(curl -sf -X POST "${LITELLM_URL}/key/generate" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "hyperion",
    "max_budget": 10,
    "budget_duration": "1d",
    "rpm_limit": 60,
    "metadata": {"project": "hyperion"}
  }')

KEY=$(echo "${RESPONSE}" | python3 -c "import sys, json; print(json.load(sys.stdin)['key'])")

echo ""
echo "Hyperion virtual key created:"
echo "  ${KEY}"
echo ""
echo "Add to agents/hyperion/.env and ai-router/.env:"
echo "  LITELLM_HYPERION_KEY=${KEY}"
