"""
config.py — load all Hyperion settings from environment.

Reads (in priority order):
  1. Process env (already set)
  2. agents/hyperion/.env
  3. ~/ai/ai-router/.env (fallback for shared infra vars)

## Configuring models

Models are specified as LiteLLM alias names. The proxy handles provider
routing and fallback automatically based on which API keys are present in
ai-router/.env. You can use:

  - Role aliases (recommended): "smart", "worker", "cheap", "fast"
    These are multi-provider groups — the proxy picks whichever provider's
    key is available, so adding/removing keys automatically changes routing.

  - Named models: "gpt-4o", "claude-sonnet-4-6", "gemini-2.5-pro", etc.
    Pin a specific model and provider. Fails if that provider's key is absent.

Override any role by setting the env var in agents/hyperion/.env:

  MODEL_PLANNER=smart          # default — Claude Opus → Gemini Pro → GPT-4o
  MODEL_WORKER=worker          # default — Claude Sonnet → Gemini Pro → GPT-4o
  MODEL_CHEAP=cheap            # default — Claude Haiku → Gemini Flash → GPT-4o-mini

Examples:
  MODEL_PLANNER=gemini-2.5-pro     # always use Gemini Pro for planning
  MODEL_WORKER=gpt-4o              # always use GPT-4o for research/synthesis
  MODEL_CHEAP=fast                 # use Gemini-first cheap alias
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# ---------------------------------------------------------------------------
# Load .env files (lowest priority first so hyperion/.env wins over router/.env)
# In Docker: env vars are injected by docker compose; .env loading is a no-op.
# ---------------------------------------------------------------------------
try:
    _ROUTER_ENV = Path(__file__).parents[4] / "ai-router" / ".env"
    load_dotenv(_ROUTER_ENV, override=False)
except IndexError:
    pass  # Docker: parents[4] doesn't exist; vars come from compose environment block

try:
    _HYPERION_ENV = Path(__file__).parents[3] / ".env"
    load_dotenv(_HYPERION_ENV, override=True)
except IndexError:
    pass


class Settings(BaseSettings):
    # LiteLLM proxy
    litellm_base_url: str = "http://localhost:4000/v1"
    litellm_master_key: str = ""
    litellm_hyperion_key: str = ""

    # Hyperion FastAPI service (used by the MCP server to reuse API orchestration).
    # In Docker, set HYPERION_API_URL=http://hyperion:4100 (service name, internal port).
    hyperion_api_url: str = os.getenv("HYPERION_API_URL", "http://localhost:4100")

    # Qdrant
    qdrant_url: str = "http://localhost:6333"

    # SearXNG
    searxng_url: str = "http://localhost:8888"

    # Infinity reranker
    infinity_url: str = "http://localhost:7997"

    # Langfuse
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3001"
    # Browser-facing Langfuse URL for UI deep-links. In Docker, langfuse_host is the
    # internal callback target (http://langfuse:3000) which a browser can't resolve;
    # set this to the host-published URL (http://localhost:3001). Falls back to
    # langfuse_host when unset.
    langfuse_public_url: str = ""
    # Optional: set to build fully-resolvable per-session deep-links
    # ({host}/project/{id}/sessions/{task_id}). Unset → link to the host root.
    langfuse_project_id: str = ""

    # ---------------------------------------------------------------------------
    # Model assignments — override any of these in agents/hyperion/.env
    # Alias names are multi-provider groups; named models pin a specific provider.
    # ---------------------------------------------------------------------------
    model_planner: str = "smart"    # high-stakes planning; best available reasoning
    model_worker: str = "worker"    # researcher + synthesizer; balanced cost/quality
    model_cheap: str = "cheap"      # tool sub-calls, summarization, compression

    # Workflow used when a task does not name one. Persisted via PUT /config and
    # resolved against config/workflows/<id>.json. "research-default" reproduces
    # the original plan -> research -> synthesize pipeline.
    default_workflow: str = "research-default"

    # Task caps (can be overridden per-request via POST /tasks body)
    cap_input_tokens: int = 400_000
    cap_output_tokens: int = 80_000
    cap_tool_loop: int = 3              # consecutive identical calls before abort
    cap_wall_seconds: int = 900         # 15 min

    # Workspace root (per-task dirs created here)
    # Override with HYPERION_TASKS_DIR env var (set to /app/tasks in Docker compose)
    tasks_dir: Path = Path(os.getenv(
        "HYPERION_TASKS_DIR",
        str(Path(__file__).parents[3] / "tasks"),
    ))

    # Agent + config records root (config/agents/*.json live here).
    # Override with HYPERION_CONFIG_DIR env var (set to /app/config in Docker compose).
    # Local default resolves to agents/hyperion/config (parents[2] == project root).
    config_dir: Path = Path(os.getenv(
        "HYPERION_CONFIG_DIR",
        str(Path(__file__).parents[2] / "config"),
    ))

    # Critic (off | planner | always)
    hyperion_critic_default: str = "off"

    # Deviation alerts (on | off) — soft-threshold warnings during a run (Phase 6)
    hyperion_hitl_alerts: str = "on"

    # Notion follow-up (Phase 9): "save to Notion" affordance writes the final
    # artifact to this database. Both must be set for the follow-up to work.
    notion_api_key: str = ""
    notion_database_id: str = ""

    # Outbound webhook SSRF guard (Phase 9). callback_url hosts must resolve to a
    # private/loopback address by default. Set to "off" only on a trusted network.
    hyperion_callback_ssrf_guard: str = "on"

    class Config:
        env_file_encoding = "utf-8"
        extra = "ignore"

    @property
    def llm_api_key(self) -> str:
        """
        Resolve the LiteLLM API key. Prefers the per-agent virtual key when set
        to a real value; falls back to the master key when the virtual key is
        absent or still the placeholder (`sk-hyperion-placeholder`/`sk-hyperion-change-me`).
        """
        key = (self.litellm_hyperion_key or "").strip()
        placeholders = {"sk-hyperion-placeholder", "sk-hyperion-change-me", ""}
        if key in placeholders:
            return self.litellm_master_key
        return key

    def provider_keys_present(self) -> dict[str, bool]:
        """Return which provider API keys are configured (for /config endpoint)."""
        return {
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
            "openai": bool(os.getenv("OPENAI_API_KEY")),
            "gemini": bool(os.getenv("GEMINI_API_KEY")),
        }


settings = Settings()
