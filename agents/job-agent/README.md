# job-agent

Agent that automates job search research: given a job description URL or text,
it pulls Charlie's background from the second brain, scores the fit, highlights
gaps, and drafts tailored resume bullets + a cover letter.

## Status: stub — not yet implemented

## Planned stack
- Framework: CrewAI or LangChain AgentExecutor
- LLM: `claude-sonnet-4-6` via `localhost:4000/v1`
- Tools: Qdrant second-brain search, Notion page creation
- Input: job description (text or URL)
- Output: Notion page with tailored bullets + cover letter draft

## To build
Start here when ready:
```bash
cd ~/ai/agents/job-agent
python3 -m venv .venv && source .venv/bin/activate
pip install crewai openai qdrant-client notion-client python-dotenv
```
Copy `../_shared/` imports into your agent entrypoint.
