"""
agent.py — Gemini Second Brain Agent (Phase 1)

A LangChain 1.x agent (backed by LangGraph) that uses Gemini 2.5 Pro via
LiteLLM and has one tool: search_notion_second_brain.

Role in the system
------------------
This is a standalone, single-file research agent living under
``~/ai/agents/research-agent/``. It is the simplest member of Charlie's local
AI ecosystem: a thin conversational wrapper that performs retrieval-augmented
generation (RAG) over the Notion-backed "second brain" knowledge base. Unlike
the larger multi-agent ``hyperion`` orchestrator, this file is meant to be run
directly as a script for ad-hoc Q&A and as a smoke test of the RAG plumbing.

Data / call flow
----------------
1. User query → the LangGraph agent (Gemini 2.5 Pro).
2. The model decides whether to call the ``search_notion_second_brain`` tool.
3. That tool (defined in the sibling ``agents/_tools`` package) embeds the query
   and performs a vector search against the Qdrant ``second_brain`` collection,
   which was populated by ``secondbrain/ingest_notion.py``.
4. Retrieved snippets are fed back to the model, which composes the final answer.

Key design decisions / non-obvious context
------------------------------------------
* All LLM traffic is routed through the local LiteLLM proxy
  (``http://localhost:4000/v1``) per the repo-wide convention — provider APIs are
  never called directly. ``ChatOpenAI`` is used as a generic OpenAI-compatible
  client pointed at LiteLLM, even though the underlying model is Gemini.
* ``temperature=0.0`` is intentional: retrieval/QA should be deterministic.
* The shared tool package lives at ``../_tools`` (outside this project dir), so
  it is added to ``sys.path`` at import time; the resulting ``import notion_tools``
  is therefore deliberately placed after that ``sys.path`` mutation (hence the
  ``# noqa: E402`` to silence the "import not at top of file" linter warning).
* Two ``.env`` files are loaded with ai-router taking precedence: the
  ai-router ``.env`` provides ``LITELLM_MASTER_KEY``, while the secondbrain
  ``.env`` (loaded with ``override=False``) supplies Qdrant/Notion credentials
  used transitively by the tool without clobbering the proxy key.

Run:
    source ~/ai/agents/research-agent/.venv/bin/activate
    python ~/ai/agents/research-agent/agent.py

Prerequisites:
    • LiteLLM running at localhost:4000  (cd ~/ai/ai-router && docker compose up -d)
    • Qdrant running at localhost:6333   (docker compose up -d qdrant)
    • Qdrant populated                   (cd ~/ai/secondbrain && python ingest_notion.py)

Required environment variables:
    • LITELLM_MASTER_KEY — auth key for the LiteLLM proxy (from ai-router/.env).
      Startup aborts with a non-zero exit code if this is missing.
"""
import os
import sys

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent

# ── 1. Environment ────────────────────────────────────────────────────────────
# Load ai-router/.env first (provides LITELLM_MASTER_KEY), then layer in the
# secondbrain/.env for Qdrant/Notion creds. override=False ensures the second
# load never clobbers keys already set by the first (or by the real environment).
load_dotenv(dotenv_path=os.path.expanduser("~/ai/ai-router/.env"))
load_dotenv(dotenv_path=os.path.expanduser("~/ai/secondbrain/.env"), override=False)

LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY")
LITELLM_BASE_URL = "http://localhost:4000/v1"

# Fail fast: without the proxy key every downstream LLM call would 401.
if not LITELLM_MASTER_KEY:
    print("❌ Error: LITELLM_MASTER_KEY not found. Check ~/ai/ai-router/.env.")
    sys.exit(1)

# ── 2. LLM: Gemini 2.5 Pro via LiteLLM ───────────────────────────────────────
# OpenAI-compatible client pointed at the local LiteLLM proxy, which transparently
# routes "gemini-2.5-pro" to Google's API. temperature=0.0 → deterministic QA.
llm_gemini = ChatOpenAI(
    model_name="gemini-2.5-pro",
    openai_api_base=LITELLM_BASE_URL,
    openai_api_key=LITELLM_MASTER_KEY,
    temperature=0.0,
)

# ── 3. Tools ──────────────────────────────────────────────────────────────────
# The shared tool package lives outside this project (sibling agents/_tools dir),
# so prepend it to sys.path before importing. This import MUST stay below the
# sys.path mutation, which is why it trips E402 (intentionally suppressed).
_tools_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_tools"))
if _tools_path not in sys.path:
    sys.path.insert(0, _tools_path)

from notion_tools import search_notion_second_brain  # noqa: E402

# The agent's full toolset — currently just the second-brain vector search.
all_tools = [search_notion_second_brain]

# ── 4. Agent (LangChain 1.x / LangGraph) ─────────────────────────────────────
SYSTEM_PROMPT = (
    "You are an intelligent personal AI assistant with access to Charlie's Notion second brain. "
    "Your primary function is to help by leveraging stored notes, projects, career records, "
    "and research. Always call `search_notion_second_brain` before answering any question that "
    "might relate to personal notes, projects, career, or investments — use retrieved context "
    "to form your answer. For clearly general-knowledge questions (e.g. geography, definitions) "
    "you may answer directly without searching."
)

agent = create_agent(
    model=llm_gemini,
    tools=all_tools,
    system_prompt=SYSTEM_PROMPT,
)


def run_query(query: str) -> str:
    """Run a single user query through the agent, streaming progress to stdout.

    Streams the agent's LangGraph execution in ``stream_mode="updates"`` so each
    node emits incremental message deltas. As events arrive, the function prints
    a human-readable trace distinguishing three message kinds:
      * tool *calls* the model decides to make (name + args),
      * tool *results* coming back (truncated to 300 chars), and
      * the model's natural-language response text (truncated to 200 chars in the
        trace, but returned in full).

    Args:
        query: The user's question to send to the agent as a single ``user`` turn.

    Returns:
        The agent's final natural-language answer as a string. May be empty if the
        run produced no content message (e.g. it only emitted tool calls).

    Raises:
        Exception: Propagates any error from the underlying agent stream (model,
            proxy, or tool failures). Callers are expected to handle these; the
            ``__main__`` block wraps each call in try/except.

    Side effects:
        Writes a formatted trace of the run to stdout.
    """
    print(f"\n{'─' * 60}")
    print(f"Query: {query}")
    print("─" * 60)

    final_answer = ""
    # Each streamed event is a dict {node_name: {"messages": [...]}}; iterate the
    # nodes, then classify every message so we can render a readable trace.
    for event in agent.stream(
        {"messages": [{"role": "user", "content": query}]},
        stream_mode="updates",
    ):
        for node_name, node_output in event.items():
            msgs = node_output.get("messages", [])
            for msg in msgs:
                # Order of the branches matters: an assistant message carrying
                # tool_calls is handled first, then tool-result messages (which
                # have a .name), and only plain content counts as the answer.
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    for tc in tool_calls:
                        print(f"[{node_name}] → tool call: {tc['name']}({tc.get('args', {})})")
                elif hasattr(msg, "name") and msg.name:
                    # Tool result message — truncate to avoid flooding the console.
                    content = msg.content[:300] if msg.content else ""
                    print(f"[{node_name}] ← tool result ({msg.name}): {content}…")
                elif hasattr(msg, "content") and msg.content:
                    # Plain assistant text: keep the latest as the final answer.
                    final_answer = msg.content
                    print(f"[{node_name}] response: {msg.content[:200]}…" if len(msg.content) > 200 else f"[{node_name}] response: {msg.content}")

    return final_answer


# ── 5. Test run ───────────────────────────────────────────────────────────────
# Manual smoke test: exercises the agent against two retrieval-style queries and
# one general-knowledge query (which the system prompt allows answering without a
# tool call). Each query is isolated in try/except so one failure doesn't abort
# the rest; failures print remediation hints about the LiteLLM/Qdrant services.
if __name__ == "__main__":
    print("=" * 60)
    print("Gemini Second Brain Agent — Phase 1 test run")
    print("=" * 60)

    test_queries = [
        "What are my notes on 'FastAPI deployment'?",
        "Can you tell me about the key projects in my second brain?",
        "What is the capital of France?",
    ]

    for query in test_queries:
        try:
            answer = run_query(query)
            print(f"\n🤖 Agent response:\n{answer}")
        except Exception as e:
            print(f"\n❌ Error during agent execution: {e}")
            import traceback
            traceback.print_exc()
            print("   Check that LiteLLM (port 4000) and Qdrant (port 6333) are running.")

    print("\n" + "=" * 60)
    print("Test run complete.")
    print("=" * 60)
