#!/bin/bash
# =============================================================================
# setup_env.sh — One-time environment setup for the research-agent project
# =============================================================================
#
# Purpose:
#   Bootstraps a self-contained Python virtual environment (.venv) for the
#   standalone research-agent and installs its LangChain-based dependency stack,
#   then smoke-tests the LLM connection before declaring success.
#
# Role in the system:
#   research-agent is a standalone agent under ~/ai/agents/. Like the rest of
#   the ~/ai ecosystem, it does NOT call provider APIs directly — all LLM
#   traffic is expected to flow through the LiteLLM proxy at
#   http://localhost:4000/v1 (see test_gemini.py / agent.py). This script only
#   provisions the local environment; it does not start any services.
#
# Usage:
#   Run from anywhere — the script cd's to its own project directory:
#     bash ~/ai/agents/research-agent/setup_env.sh
#
# Required external state / env vars:
#   - python3 must be on PATH (used to create the venv).
#   - Network access to PyPI for `pip install`.
#   - The LiteLLM proxy must be running and reachable for the verification
#     step (test_gemini.py); that script reads its credentials (e.g.
#     LITELLM_MASTER_KEY) from the project's .env, sourced from ~/ai/ai-router/.env.
#
# Idempotency:
#   Safe to re-run. The venv is only created when absent; dependencies are
#   upgraded in place on every run.
#
# Side effects:
#   - Creates ~/ai/agents/research-agent/.venv on first run.
#   - Installs/upgrades pip and the runtime dependencies into that venv.
#   - Activates the venv within this shell process only (not the caller's shell).
# =============================================================================

# Abort immediately on any command failure so a half-provisioned environment
# is never reported as a success.
set -e

echo "=== Setting up research-agent Python environment ==="

# All subsequent relative paths (.venv, source) resolve against the project root.
cd ~/ai/agents/research-agent

# Create venv if it doesn't exist (first-run only; keeps re-runs fast/idempotent).
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "✅ Virtual environment created."
else
    echo "ℹ️  Virtual environment already exists, skipping creation."
fi

# Install / upgrade dependencies into the venv.
# Note: activation only affects this script's subshell, not the caller's shell.
source .venv/bin/activate
pip install --quiet --upgrade pip
# Runtime stack: LangChain core + OpenAI-compatible client (used to talk to the
# LiteLLM proxy) + community integrations, dotenv for .env loading, and the
# Qdrant client for vector-store access.
pip install --quiet langchain langchain-openai langchain-community python-dotenv qdrant-client
echo "✅ Dependencies installed."

echo ""
echo "=== Verifying Gemini/LiteLLM connection ==="
# Smoke test: confirms the agent can reach a model through the LiteLLM proxy.
# Because `set -e` is active, a failed connection here aborts setup so the
# environment is never reported as ready when it cannot actually call an LLM.
python ~/ai/agents/research-agent/test_gemini.py

echo ""
echo "✅ Setup complete. Run the agent with:"
echo "   source ~/ai/agents/research-agent/.venv/bin/activate"
echo "   python ~/ai/agents/research-agent/agent.py"
