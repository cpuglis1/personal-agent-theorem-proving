"""
OWUI tool architecture note — not an importable module.

The canonical web search implementation lives in:
    agents/hyperion/src/hyperion/tools/web_search.py

It is exposed over HTTP at:
    GET http://localhost:4100/tools/search?q=<query>[&top_k=10][&categories=general,news]

OWUI tool plugins call that endpoint and are intentionally thin HTTP wrappers.
This means updating web search behavior (SearXNG params, reranking, sanitization)
only requires editing the Hyperion tool — the OWUI plugin picks up the change
automatically since it delegates to the same endpoint.

The same pattern applies to second-brain search:
    Hyperion tool:  agents/hyperion/src/hyperion/tools/second_brain.py
    HTTP endpoint:  GET http://localhost:4100/tools/second-brain?q=<query>[&limit=5]
    OWUI plugin:    skills/tools/second_brain_search.py

See secondbrain/CLAUDE.md for the pinned reminder.
"""
