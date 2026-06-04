"""
notion_tools.py — LangChain tool: search_notion_second_brain

Purpose
-------
Exposes a single LangChain ``@tool`` (``search_notion_second_brain``) that lets
an LLM agent perform semantic retrieval over Charlie's "second brain" — the
Notion/Obsidian PARA vault that has been embedded and indexed into the Qdrant
``second_brain`` collection. The tool is the agent-facing, RAG-style entry point
for personal notes, projects, career records, and saved research.

Role in the system
------------------
This module lives in ``agents/_tools/`` (shared agent tooling) and is intended
to be imported by any LangChain-based agent in the workspace (e.g. the
research-agent / ai-workflow-agent) that needs to ground its answers in the
user's private knowledge base. The actual vector search + result formatting is
delegated to ``agents/_shared/qdrant_client.py``; this file is a thin LangChain
binding layer on top of those helpers.

Key design decision — importlib-based loading (non-obvious)
-----------------------------------------------------------
The shared helper module is loaded by *absolute file path* via ``importlib``
rather than a normal ``import``. This deliberately avoids a name collision:
the helper file is itself named ``qdrant_client.py``, and it internally does
``from qdrant_client import QdrantClient`` to use the installed ``qdrant-client``
pip package. If we added ``_shared/`` to ``sys.path`` and imported it normally,
that internal import would resolve to the helper file itself instead of the pip
package, producing a circular import. Loading it under the private module name
``_second_brain_helper`` sidesteps the shadowing entirely.
"""
import os
import importlib.util

from langchain.tools import tool

# ── Load _shared/qdrant_client.py by absolute file path ─────────────────────
# We cannot use sys.path manipulation here: adding _shared to sys.path would
# cause `from qdrant_client import QdrantClient` inside that file to find
# itself instead of the pip package, creating a circular import.
# Resolve agents/_shared/qdrant_client.py relative to this file's location so the
# import works regardless of the process's current working directory.
_shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_shared"))
_helper_path = os.path.join(_shared_dir, "qdrant_client.py")

# Build a module spec from the file path and execute it under a private name
# (`_second_brain_helper`) so it does NOT register as `qdrant_client` and shadow
# the pip package that the helper itself depends on. See module docstring.
_spec = importlib.util.spec_from_file_location("_second_brain_helper", _helper_path)
_sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sb)

# Re-export the two helper callables used by the tool below:
#   search_second_brain(query, limit, score_threshold) -> list of result dicts
#   format_context(results) -> human/LLM-readable context block (str)
search_second_brain = _sb.search_second_brain
format_context = _sb.format_context


# ── Tool definition ──────────────────────────────────────────────────────────

@tool
def search_notion_second_brain(query: str, limit: int = 5, score_threshold: float = 0.30) -> str:
    """
    Search your Qdrant-indexed Notion second brain for semantically relevant notes,
    projects, career records, or investment pages.

    Use this tool whenever the user asks about something that might be in their
    personal notes, projects, career history, or saved research. Always prefer
    searching here before falling back to general knowledge.

    Args:
        query: A natural-language description of what to find (e.g. "FastAPI deployment notes").
        limit: Max number of results to return (default 5).
        score_threshold: Minimum relevance score 0–1 (default 0.30 = broad match;
                         use 0.70+ for more precise results).

    Returns:
        Formatted context block with matching note titles, content previews, and
        Notion URLs. Returns a "not found" message if nothing matches.
    """
    try:
        # Trace line (stdout) so tool invocations are visible in agent logs.
        print(
            f"[Tool] search_notion_second_brain: query={query!r} "
            f"limit={limit} threshold={score_threshold}"
        )
        results = search_second_brain(query=query, limit=limit, score_threshold=score_threshold)
        if not results:
            # Empty result set is a normal outcome, not an error — return a
            # plain-language message the LLM can act on.
            return "No relevant information found in the Notion second brain for that query."
        formatted = format_context(results)
        print(f"[Tool] Found {len(results)} relevant entries.")
        return formatted
    except Exception as e:
        # Never raise out of a LangChain tool: surface the failure as a string so
        # the agent can recover/explain instead of crashing the run.
        return f"Error searching Notion second brain: {e}"
