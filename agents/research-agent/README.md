# research-agent

Agent that runs automated investment due diligence: given a ticker or company
name, fetches public info, applies the due-diligence checklist, and saves a
structured research note to Notion.

## Status: stub — not yet implemented

## Planned stack
- Framework: LangChain AgentExecutor with tool use
- LLM routing: `gemini-1.5-pro` for long docs, `claude-opus-4-6` for reasoning, `o1-mini` for math
- Tools: web search, Qdrant second-brain search, Notion page creation
- Input: ticker symbol or company name
- Output: Notion research note with bull/bear/verdict sections

## To build
```bash
cd ~/ai/agents/research-agent
python3 -m venv .venv && source .venv/bin/activate
pip install langchain langchain-openai qdrant-client notion-client python-dotenv
```
