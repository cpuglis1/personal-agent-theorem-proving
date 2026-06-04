#!/usr/bin/env bash
# =============================================================================
# create_hyperion_key.sh — Provision the Hyperion LiteLLM virtual key
# =============================================================================
#
# PURPOSE
#   Create (or recreate) the per-project "virtual key" that the Hyperion
#   multi-agent orchestrator uses to authenticate against the shared LiteLLM
#   proxy (the ai-router stack). Virtual keys let LiteLLM enforce per-project
#   spend budgets and rate limits while every agent still routes its LLM calls
#   through the single proxy endpoint (the ~/ai convention: never call provider
#   APIs directly).
#
# ROLE IN THE SYSTEM
#   The Hyperion services read LITELLM_HYPERION_KEY (NOT the master key) so that
#   their usage is scoped, budgeted, and rate-limited independently of other
#   workspace tooling. This script is the one-time/admin step that mints that
#   key by calling the LiteLLM /key/generate management endpoint with the
#   master key. Run it again any time the key needs to be rotated/recreated.
#
# REQUIRED ENV VARS
#   LITELLM_MASTER_KEY   (required) Admin key for the LiteLLM proxy. Found in
#                        ~/ai/ai-router/.env. Authorizes the /key/generate call.
#   LITELLM_URL          (optional) Base URL of the LiteLLM proxy.
#                        Defaults to http://localhost:4000.
#
# PRECONDITIONS
#   - The LiteLLM proxy must be running and reachable at LITELLM_URL
#     (e.g. `cd ~/ai/ai-router && docker compose up -d`).
#   - python3 must be on PATH (used to parse the JSON response).
#
# USAGE
#   export LITELLM_MASTER_KEY=sk-...
#   bash agents/hyperion/scripts/create_hyperion_key.sh
#
# OUTPUT / NEXT STEPS
#   The script prints the newly generated key. Copy it into BOTH:
#     - agents/hyperion/.env  as  LITELLM_HYPERION_KEY=<value>
#     - ai-router/.env        as  LITELLM_HYPERION_KEY=<value>
#   (.env files are gitignored by convention; the value is never committed.)
#
# KEY POLICY (encoded in the /key/generate request body below)
#   key_alias       "hyperion"  — human-readable label for this key in LiteLLM.
#   max_budget      10          — spend cap (USD) per budget_duration window.
#   budget_duration "1d"        — budget resets daily.
#   rpm_limit       60          — max requests per minute for this key.
#   metadata.project"hyperion"  — tags spend/usage records for attribution.
# =============================================================================

# Fail fast: -e exit on error, -u error on unset vars, -o pipefail propagate
# failures through pipes (so a failed curl/python in a pipeline aborts).
set -euo pipefail

# Proxy base URL; overridable via env for non-default hosts/ports.
LITELLM_URL="${LITELLM_URL:-http://localhost:4000}"

# Guard: the master key is mandatory to authorize key generation. Bail with a
# clear message (to stderr) rather than letting curl fail with a 401.
if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "ERROR: LITELLM_MASTER_KEY is not set." >&2
  exit 1
fi

# Call the LiteLLM management API to mint the scoped key.
#   -s  silent (no progress meter)
#   -f  fail (non-zero exit on HTTP errors, so set -e aborts on a bad response)
# The JSON body is the key policy documented in the header above.
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

# Extract the generated key from the JSON response. python3 is used (instead of
# requiring jq) since python3 is already a workspace dependency. KeyError/JSON
# parse failures will surface as a non-zero exit and abort via set -e/pipefail.
KEY=$(echo "${RESPONSE}" | python3 -c "import sys, json; print(json.load(sys.stdin)['key'])")

# Print the key plus copy-paste-ready .env lines for the operator.
echo ""
echo "Hyperion virtual key created:"
echo "  ${KEY}"
echo ""
echo "Add to agents/hyperion/.env and ai-router/.env:"
echo "  LITELLM_HYPERION_KEY=${KEY}"
