#!/bin/bash
# ==============================================================================
# run_all.sh — Full setup + test for research-agent (Phase 1)
# ==============================================================================
#
# Purpose:
#   One-shot bootstrap + smoke-test for the standalone "Gemini Second Brain"
#   research agent. It brings up the shared infrastructure, refreshes the
#   vector index from Notion, provisions both Python virtualenvs, and finally
#   runs the agent's connectivity + end-to-end checks.
#
# Role in the system:
#   This script ties together three components of Charlie's ~/ai workspace:
#     1. ai-router/   — the Docker Compose stack (Qdrant + LiteLLM proxy).
#     2. secondbrain/ — the Notion → Qdrant ingestion pipeline.
#     3. research-agent/ — the LangChain agent under test.
#   It is the canonical "does the whole pipeline work end-to-end?" entrypoint.
#
# What it does (4 ordered steps; order matters — see notes):
#   Step 1: Start the Qdrant vector DB and LiteLLM proxy, then block until both
#           report healthy. Must come first: ingestion (Step 2) and the agent
#           (Step 4) both depend on these services being reachable.
#   Step 2: (Re)build the secondbrain venv if needed and run an incremental
#           Notion ingest into Qdrant so the agent has fresh data to query.
#   Step 3: Provision the research-agent venv and install its LangChain deps.
#   Step 4: Run test_gemini.py (LiteLLM/Gemini reachability) then agent.py
#           (full second-brain agent run).
#
# Usage:
#   bash ~/ai/agents/research-agent/run_all.sh
#
# Required environment / prerequisites:
#   - Docker + Docker Compose available and the ai-router stack defined.
#   - Network access to Notion (used by ingest_notion.py).
#   - LITELLM_MASTER_KEY and any Notion/API credentials present in the
#     relevant .env files (loaded by the Python scripts, not by this shell).
#   - python3 available on PATH; a pre-existing ~/.venv used only to probe the
#     installed notion-client version.
#
# Design notes / non-obvious context:
#   - `set -e` aborts on the first failing command, so a failed service
#     start, ingest, or test stops the whole run rather than continuing in a
#     broken state.
#   - notion-client MUST stay on the 2.x line; the 3.x API is incompatible
#     with the ingestion pipeline. This script defensively detects and refuses
#     to proceed with a 3.x install (see Steps 2 checks below).
#   - Health checks poll with bounded retries and hard-fail on timeout so the
#     script never hangs indefinitely waiting on Docker.
# ==============================================================================

# Abort immediately if any command exits non-zero (fail fast; no partial runs).
set -e

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║      Gemini Second Brain Agent — Phase 1 Setup           ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Start services and wait for healthy ───────────────────────────────
# Bring up only the two services the agent needs (not the whole stack) and
# block until each is actually serving requests, so later steps don't race
# against a container that is "up" but not yet ready.
echo "▶ Step 1/4: Starting Docker services..."
cd ~/ai/ai-router
docker compose up -d qdrant litellm

# Poll Qdrant's /collections endpoint up to 20 times (~40s max at 2s each).
echo "   Waiting for Qdrant..."
for i in {1..20}; do
    # -sf: silent + fail-on-HTTP-error so a non-2xx counts as "not ready yet".
    if curl -sf http://localhost:6333/collections > /dev/null 2>&1; then
        echo "   ✅ Qdrant is healthy."
        break
    fi
    # On the final attempt, give up and abort the whole script.
    [ $i -eq 20 ] && echo "   ❌ Qdrant timeout" && exit 1
    sleep 2
done

# Poll LiteLLM's liveness endpoint up to 20 times (~60s max at 3s each — the
# proxy is slower to warm up than Qdrant, hence the longer sleep).
echo "   Waiting for LiteLLM..."
for i in {1..20}; do
    if curl -sf http://localhost:4000/health/liveliness > /dev/null 2>&1; then
        echo "   ✅ LiteLLM is healthy."
        break
    fi
    [ $i -eq 20 ] && echo "   ❌ LiteLLM timeout" && exit 1
    sleep 3
done

# ── Step 2: Ingest Notion data ────────────────────────────────────────────────
echo ""
echo "▶ Step 2/4: Ingesting Notion data into Qdrant..."
cd ~/ai/secondbrain
# Only rebuild the venv if it doesn't exist or notion-client is the wrong major version.
# Probe the *existing* venv's notion-client version via ~/.venv. The trailing
# `|| true` keeps `set -e` from aborting when the package isn't installed yet
# (empty NC_VER simply falls through to the rebuild condition below).
NC_VER=$(~/.venv/bin/pip show notion-client 2>/dev/null | grep ^Version | awk '{print $2}' || true)
# Rebuild from scratch if there is no venv, or if a forbidden 3.x is present
# (we delete and recreate to guarantee a clean, downgradable environment).
if [ ! -d ".venv" ] || [[ "$NC_VER" == 3* ]]; then
    echo "   ℹ️  Building secondbrain venv..."
    rm -rf .venv
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
# Re-read the version from the now-active venv to confirm what actually landed.
NC_VER=$(pip show notion-client 2>/dev/null | grep ^Version | awk '{print $2}')
echo "   ✅ secondbrain dependencies ready (notion-client==${NC_VER})."
# Hard guard: the 3.x API is incompatible with ingest_notion.py — refuse to run.
if [[ "$NC_VER" == 3* ]]; then
    echo "   ❌ notion-client 3.x installed — check requirements.txt"; exit 1
fi
# Incremental ingest: only re-embed pages changed since the last run (fast).
python ingest_notion.py --incremental
echo "   ✅ Ingestion complete."
# Leave the secondbrain venv before Step 3 activates the research-agent venv.
deactivate

# ── Step 3: Set up research-agent venv ───────────────────────────────────────
echo ""
echo "▶ Step 3/4: Setting up research-agent Python environment..."
cd ~/ai/agents/research-agent

# Reuse the agent venv across runs; only create it the first time.
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "   ✅ Virtual environment created."
else
    echo "   ℹ️  Virtual environment already exists."
fi

source .venv/bin/activate
pip install --quiet --upgrade pip
# LangChain stack + qdrant-client (retrieval) + python-dotenv (.env loading).
# Note: langchain-openai is used to talk to the LiteLLM OpenAI-compatible proxy.
pip install --quiet langchain langchain-openai langchain-community python-dotenv qdrant-client
echo "   ✅ Dependencies installed."

# ── Step 4: Run tests ─────────────────────────────────────────────────────────
# Run the cheap connectivity check first; if Gemini/LiteLLM is unreachable it
# fails fast (via `set -e`) before the heavier end-to-end agent run.
echo ""
echo "▶ Step 4/4: Running tests..."
echo ""
echo "── test_gemini.py (LiteLLM/Gemini connection check) ──"
python ~/ai/agents/research-agent/test_gemini.py

echo ""
echo "── agent.py (end-to-end second brain agent) ──"
python ~/ai/agents/research-agent/agent.py

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║                  ✅ All done!                            ║"
echo "╚══════════════════════════════════════════════════════════╝"
