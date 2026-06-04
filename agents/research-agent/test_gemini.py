"""
test_gemini.py — Verify LiteLLM can route calls to Gemini.

Purpose
    Smoke test / connectivity check that confirms the local LiteLLM proxy
    (http://localhost:4000/v1) is up and can successfully route a chat
    completion to the Gemini backend ("gemini-2.5-pro"). It is meant to be run
    by hand during environment setup or when debugging the research-agent's LLM
    connectivity, NOT as part of an automated pytest suite (there are no test
    functions or assertions — it executes top-to-bottom and prints results).

Role in the system
    The research-agent (and the wider ~/ai ecosystem) follows the convention
    that ALL LLM calls go through the LiteLLM proxy rather than provider APIs
    directly. This script exercises that path end-to-end using LangChain's
    OpenAI-compatible client (ChatOpenAI) pointed at the LiteLLM base URL, which
    is exactly how the agent itself talks to models.

Design / non-obvious context
    - LiteLLM is OpenAI-compatible, so a Gemini model is reached via the
      OpenAI ChatOpenAI client; the model is selected purely by model_name and
      LiteLLM does the provider routing behind the scenes.
    - Credentials are read from ~/ai/ai-router/.env (the shared stack env file),
      not from a local .env, because LITELLM_MASTER_KEY lives with the proxy.
    - temperature=0.0 makes the response deterministic for a stable check.
    - Exit codes: the script calls exit(1) on missing key or on any failure so
      it can be used in shell pipelines; success falls through with exit code 0.

Run:
    source .venv/bin/activate && python test_gemini.py

Required environment:
    LITELLM_MASTER_KEY — API key for the LiteLLM proxy (from ~/ai/ai-router/.env).
"""
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

# Load environment variables from ai-router/.env
load_env_path = os.path.expanduser("~/ai/ai-router/.env")
print(f"Loading .env from: {load_env_path}")
load_dotenv(dotenv_path=load_env_path)

LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY")
# OpenAI-compatible endpoint exposed by the local LiteLLM proxy container.
LITELLM_BASE_URL = "http://localhost:4000/v1"

# Fail fast if the proxy key is missing — every downstream call would 401 anyway.
if not LITELLM_MASTER_KEY:
    print("Error: LITELLM_MASTER_KEY not found in .env. Check ~/ai/ai-router/.env.")
    exit(1)

print("Initializing ChatOpenAI client for Gemini via LiteLLM...")
try:
    # Reach Gemini via the OpenAI-compatible client; LiteLLM resolves the
    # provider from model_name, so no Gemini-specific SDK is needed here.
    llm_gemini = ChatOpenAI(
        model_name="gemini-2.5-pro",
        openai_api_base=LITELLM_BASE_URL,
        openai_api_key=LITELLM_MASTER_KEY,
        temperature=0.0,
    )

    print("Testing Gemini LLM call...")
    messages = [
        SystemMessage(content="You are a helpful AI assistant."),
        HumanMessage(content="What is the capital of France?"),
    ]
    response = llm_gemini.invoke(messages)
    print("\nGemini Response (via LiteLLM):")
    print(response.content)
    print("\n✅ Successfully connected to Gemini via LiteLLM!")

except Exception as e:
    print(f"\n❌ Error connecting to Gemini via LiteLLM: {e}")
    print(f"   LITELLM_BASE_URL: {LITELLM_BASE_URL}")
    print(f"   LITELLM_MASTER_KEY (first 5 chars): {LITELLM_MASTER_KEY[:5]}...")
    print("   Ensure the LiteLLM Docker container is running: cd ~/ai/ai-router && docker compose ps")
    exit(1)
