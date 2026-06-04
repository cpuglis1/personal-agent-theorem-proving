# AI Workflow Agent — Open WebUI Tool Suite

**Project:** AI Workflow Agent
**Owner:** Charlie Tolleson
**Drafted:** 2026-05-26
**Status:** Plan — ready for implementation
**Target host:** macOS, Docker Desktop, stack rooted at `~/ai`

---

## 0. Scope and intent

Upgrade the existing Open WebUI / LiteLLM / Qdrant stack so that any chat model exposed by LiteLLM (GPT‑4o, Gemini 2.5, Claude 4.6) can, from the Open WebUI chat:

1. Read, write, append, and list files inside a single sandboxed host directory.
2. Search, read, create, and append Notion pages.
3. Read and send Gmail, and read/write Google Drive files.

This is delivered as **three Open WebUI native Python Tools** plus a small set of Compose / env / OAuth changes. No new services, no new ports.

> **Critical correction to the prior plan:** Tools do **not** run "inside LiteLLM." LiteLLM is a stateless OpenAI‑compatible proxy — it has no notion of tools. Open WebUI assembles the `tools=[...]` array in the chat completion request, the model returns `tool_calls`, and Open WebUI executes the Python function **inside the `open-webui` container**, then feeds the result back. This shapes every other decision below (paths are container paths; secrets live in Open WebUI's Valves, not LiteLLM's env; package deps are declared in the tool's frontmatter so OWUI installs them inside its own venv).

---

## 1. Prerequisites and model wiring

Before any of the tools below will actually be *invoked* by the model:

1. **Tool-calling models only.** All three chat models in `litellm_config.yaml` (`gpt-4o`, `claude-opus-4-6`, `claude-sonnet-4-6`, `gemini-2.5-pro`, `gemini-2.5-flash`) support OpenAI-format tool calls. `o1-mini` does **not** — exclude it from any chat where tools matter.
2. **`drop_params: true` is already set** in `litellm_settings`, which is required for Gemini to accept the `tools` array Open WebUI sends (Gemini's native schema differs; LiteLLM translates it). Keep it.
3. **Enable per-model in Open WebUI** after installing each tool:
   `Admin Panel → Settings → Models → <model> → Edit → Tools` and toggle the tool on. Also turn on **Function Calling: Native** (not "default"). The default uses an inline prompt-based shim that is much less reliable across providers.
4. **Recommended default for tool-heavy chats:** `claude-sonnet-4-6` or `gemini-2.5-pro`. Both are noticeably more obedient about chained tool calls than `gpt-4o-mini`.

---

## 2. Step 1 — Sandboxed host filesystem mount

### 2.1 Choose and create the workspace

```bash
mkdir -p ~/agent_workspace
chmod 750 ~/agent_workspace            # owner rwx, group rx, others nothing
```

This is the **only** host directory the file tool will be able to touch. Pick a path that contains nothing sensitive; never mount `~`, `~/Documents`, or `~/ai` itself.

### 2.2 Compose changes

Edit `~/ai/ai-router/docker-compose.yml` and modify **only** the `open-webui` service. Add a bind mount and two env vars:

```yaml
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    restart: unless-stopped
    ports:
      - "3000:8080"
    volumes:
      - open-webui-data:/app/backend/data
      # NEW — sandboxed host workspace exposed to the file tool
      - ${AGENT_WORKSPACE_HOST:-${HOME}/agent_workspace}:/workspace:rw
      # NEW — Google OAuth token (read-only, host-side refresh; see §5)
      - ${GOOGLE_TOKEN_DIR:-${HOME}/.config/agent-google}:/secrets/google:ro
    environment:
      OPENAI_API_BASE_URL: "http://litellm:4000/v1"
      OPENAI_API_KEY: "${LITELLM_MASTER_KEY}"
      OLLAMA_BASE_URL: ""
      ENABLE_OLLAMA_API: "false"
      WEBUI_SECRET_KEY: "${WEBUI_SECRET_KEY:-change-me-in-env-file}"
      QDRANT_URI: "http://qdrant:6333"
      VECTOR_DB: "qdrant"
      # NEW — the file tool reads this; never let it be overridden by a Valve
      AGENT_WORKSPACE: "/workspace"
      # NEW — Google tool reads creds/token from this directory
      GOOGLE_SECRETS_DIR: "/secrets/google"
    depends_on:
      litellm:
        condition: service_healthy
      qdrant:
        condition: service_started
    networks:
      - ai-net
```

Then append to `~/ai/ai-router/.env.example` (and copy into your real `.env`):

```bash
# -----------------------------------------------------------------------------
# Agent workspace (mounted into open-webui container at /workspace)
# -----------------------------------------------------------------------------
AGENT_WORKSPACE_HOST=${HOME}/agent_workspace
GOOGLE_TOKEN_DIR=${HOME}/.config/agent-google
```

Apply:

```bash
cd ~/ai/ai-router
docker compose up -d open-webui    # recreate just open-webui; others untouched
docker compose exec open-webui ls -la /workspace
# expected: contents (or empty) of ~/agent_workspace, writable
```

### 2.3 Permissions — what you do and do not need

- **macOS / Docker Desktop:** Docker Desktop runs Linux containers in a lightweight VM and shares host paths through VirtioFS, which **transparently maps the host user to the container user**. You do **not** need to match UIDs, run `chown`, or pass `user:` to the service. The container will read and write as you.
- **If you ever move this stack to a Linux host:** revisit this. Open WebUI's official image runs as UID/GID `0` by default; you would either set `user: "${UID}:${GID}"` on the service or `chown` the workspace to that UID. Not needed today.
- **Security perimeter:** the *only* line that confines the model is the bind-mount root + the path-resolution check in §3.1. Do not symlink things into `~/agent_workspace` you would not want the model to read or overwrite. The file tool refuses to follow symlinks that escape `/workspace` (verified by `Path.resolve()` + `is_relative_to`), but a symlink whose **target** lives inside `/workspace` is still followed.

---

## 3. Step 2 — Open WebUI native Tools

### 3.0 Anatomy of an Open WebUI tool (one-time read)

Every tool is a single Python file pasted into `Admin Panel → Workspace → Tools → +`. Open WebUI parses the frontmatter, installs any pip packages listed in `requirements:` into its venv, then loads the file. The file must define a class named exactly `Tools`. Public methods on that class become callable tools; their **type hints + docstrings** are converted to JSON schema and shown to the model. Conventions used below:

- `Valves` — admin-set config; visible to all chats using this tool (e.g. Notion token).
- `UserValves` — per-user overrides (not used here; mentioned for future expansion).
- `__user__: dict` — Open WebUI passes the current user; we use it for audit logging.
- `self.citation = True` — surfaces a "source" chip in the UI for every result.
- Return values must be **strings** (JSON-encoded for structured data). The model never sees Python objects.
- Errors should be returned as strings starting with `ERROR:` rather than raised — Open WebUI shows raised exceptions to the user but the model handles the string form better.

### 3.1 Tool A — Local File Manager

Save in OWUI as **`Local Files`**.

```python
"""
title: Local Files
author: Charlie Tolleson
author_url: https://github.com/charlie
version: 0.2.0
license: MIT
required_open_webui_version: 0.4.0
requirements:
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# Hard limits — defended in code, not just docs.
_MAX_READ_BYTES = 1_000_000          # 1 MB; refuse larger reads
_MAX_WRITE_BYTES = 5_000_000         # 5 MB per write/append
_MAX_LIST_ENTRIES = 500              # truncate huge directories
_TEXT_SAMPLE_BYTES = 8192            # bytes sniffed for binary detection
_AUDIT_PATH = "/workspace/.agent_audit.log"   # inside container


class Tools:
    class Valves(BaseModel):
        workspace_root: str = Field(
            default=os.environ.get("AGENT_WORKSPACE", "/workspace"),
            description=(
                "Absolute path inside the container that bounds all file ops. "
                "Should match the bind-mount in docker-compose.yml. "
                "Changing this in the UI does not move the mount — keep in sync."
            ),
        )
        allow_write: bool = Field(
            default=True,
            description="Master switch for write_file / append_file. Read-only when false.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self.citation = True

    # ------------------------------------------------------------------ helpers

    def _root(self) -> Path:
        return Path(self.valves.workspace_root).resolve()

    def _resolve(self, rel_or_abs: str) -> Path:
        """
        Resolve a user-supplied path against the workspace root and guarantee it
        does not escape (symlinks included).
        """
        root = self._root()
        candidate = (root / rel_or_abs).resolve() if not Path(rel_or_abs).is_absolute() \
                    else Path(rel_or_abs).resolve()
        if not candidate.is_relative_to(root):
            raise PermissionError(
                f"path '{rel_or_abs}' resolves outside the workspace root '{root}'"
            )
        return candidate

    def _audit(self, user: dict, op: str, target: Path, extra: str = "") -> None:
        try:
            line = json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "user": (user or {}).get("email") or (user or {}).get("name") or "unknown",
                "op": op,
                "path": str(target),
                "extra": extra,
            })
            with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # Never let audit failures break a tool call.
            pass

    @staticmethod
    def _looks_binary(sample: bytes) -> bool:
        if b"\x00" in sample:
            return True
        # Mostly-non-printable: treat as binary.
        printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in sample)
        return len(sample) > 0 and (printable / len(sample)) < 0.70

    # ------------------------------------------------------------------ tools

    def list_directory(self, path: str = ".", __user__: Optional[dict] = None) -> str:
        """
        List the contents of a directory inside the agent workspace.

        :param path: Directory path relative to the workspace root. Use "." for the root.
        :return: JSON array of entries: [{"name": str, "type": "file"|"dir", "size": int|null}, ...]
        """
        try:
            target = self._resolve(path)
            if not target.exists():
                return f"ERROR: path does not exist: {path}"
            if not target.is_dir():
                return f"ERROR: not a directory: {path}"
            entries = []
            for i, entry in enumerate(sorted(target.iterdir(), key=lambda p: p.name.lower())):
                if i >= _MAX_LIST_ENTRIES:
                    entries.append({"name": "...", "type": "truncated",
                                    "size": None,
                                    "note": f"more than {_MAX_LIST_ENTRIES} entries; showing first {_MAX_LIST_ENTRIES}"})
                    break
                entries.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else None,
                })
            self._audit(__user__ or {}, "list", target, f"count={len(entries)}")
            return json.dumps({"root": str(target), "entries": entries}, indent=2)
        except PermissionError as e:
            return f"ERROR: {e}"
        except Exception as e:
            return f"ERROR: list_directory failed: {e!r}"

    def read_file(self, path: str, __user__: Optional[dict] = None) -> str:
        """
        Read a text file from the agent workspace. Refuses binary files and files larger than 1 MB.

        :param path: File path relative to the workspace root.
        :return: File contents as a string, or an ERROR message.
        """
        try:
            target = self._resolve(path)
            if not target.is_file():
                return f"ERROR: not a regular file: {path}"
            size = target.stat().st_size
            if size > _MAX_READ_BYTES:
                return (f"ERROR: file is {size} bytes; refuse to read more than "
                        f"{_MAX_READ_BYTES}. Ask the user to chunk or summarize it.")
            with open(target, "rb") as f:
                sample = f.read(_TEXT_SAMPLE_BYTES)
                if self._looks_binary(sample):
                    return f"ERROR: '{path}' looks like a binary file; refusing to return raw bytes."
                rest = f.read()
            content = (sample + rest).decode("utf-8", errors="replace")
            self._audit(__user__ or {}, "read", target, f"bytes={size}")
            return content
        except PermissionError as e:
            return f"ERROR: {e}"
        except Exception as e:
            return f"ERROR: read_file failed: {e!r}"

    def write_file(self, path: str, content: str, __user__: Optional[dict] = None) -> str:
        """
        Write (or overwrite) a text file in the agent workspace. Creates parent directories.

        :param path: File path relative to the workspace root.
        :param content: Full file contents. UTF-8. Overwrites if the file exists.
        :return: A short status message.
        """
        if not self.valves.allow_write:
            return "ERROR: writes are disabled by the admin (valves.allow_write=False)."
        try:
            data = content.encode("utf-8")
            if len(data) > _MAX_WRITE_BYTES:
                return f"ERROR: content is {len(data)} bytes; max is {_MAX_WRITE_BYTES}."
            target = self._resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
            self._audit(__user__ or {}, "write", target, f"bytes={len(data)}")
            return f"OK: wrote {len(data)} bytes to {path}"
        except PermissionError as e:
            return f"ERROR: {e}"
        except Exception as e:
            return f"ERROR: write_file failed: {e!r}"

    def append_file(self, path: str, content: str, __user__: Optional[dict] = None) -> str:
        """
        Append text to a file in the agent workspace. Creates the file if missing.

        :param path: File path relative to the workspace root.
        :param content: Text to append (no separator added — include your own newline if needed).
        :return: A short status message.
        """
        if not self.valves.allow_write:
            return "ERROR: writes are disabled by the admin (valves.allow_write=False)."
        try:
            data = content.encode("utf-8")
            if len(data) > _MAX_WRITE_BYTES:
                return f"ERROR: content is {len(data)} bytes; max is {_MAX_WRITE_BYTES}."
            target = self._resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "ab") as f:
                f.write(data)
            self._audit(__user__ or {}, "append", target, f"bytes={len(data)}")
            return f"OK: appended {len(data)} bytes to {path}"
        except PermissionError as e:
            return f"ERROR: {e}"
        except Exception as e:
            return f"ERROR: append_file failed: {e!r}"
```

**Why this differs from Gemini's version:**

| Concern | Gemini | This plan |
|---|---|---|
| Path traversal | string check on `..` | `Path.resolve()` + `is_relative_to`; symlinks can't escape |
| Binary files | not handled | refused with explicit error |
| Read size | unbounded | 1 MB hard cap → prevents context blowup |
| Write size | unbounded | 5 MB hard cap |
| Audit log | none | append-only JSONL inside workspace |
| Admin lockdown | none | `allow_write` valve flips it to read-only |
| Secrets in code | env-only | OWUI Valves (admin-set in UI) |

### 3.2 Tool B — Notion

Save in OWUI as **`Notion`**. This intentionally re-implements (rather than imports) the small slice of the existing `agents/_shared/notion_client.py` that we need, because OWUI tools execute inside the `open-webui` container and cannot reach the host's `~/ai/agents` tree.

```python
"""
title: Notion
author: Charlie Tolleson
version: 0.2.0
license: MIT
required_open_webui_version: 0.4.0
requirements: notion-client==2.2.1
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from notion_client import Client
from notion_client.errors import APIResponseError, RequestTimeoutError
from pydantic import BaseModel, Field


_NOTION_VERSION = "2022-06-28"
_BLOCK_CHUNK = 100                  # Notion API caps appended blocks per call


def _markdown_to_blocks(md: str) -> list[dict]:
    """
    Minimal markdown → Notion block converter.
    Supports: # / ## / ### headings, bullet (- / *) and numbered (1.) lists,
    fenced ```code``` blocks, and paragraphs. Inline formatting is passed through
    as plain text — Notion's rich_text grammar isn't worth re-implementing here.
    """
    lines = md.splitlines()
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Fenced code block
        m = re.match(r"^```([a-zA-Z0-9_+-]*)\s*$", line)
        if m:
            lang = m.group(1) or "plain text"
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1  # skip closing fence
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(buf)[:1900]}}],
                    "language": lang.lower() if lang else "plain text",
                },
            })
            continue

        if line.startswith("### "):
            blocks.append(_heading(3, line[4:])); i += 1; continue
        if line.startswith("## "):
            blocks.append(_heading(2, line[3:])); i += 1; continue
        if line.startswith("# "):
            blocks.append(_heading(1, line[2:])); i += 1; continue

        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            blocks.append(_bullet(m.group(1))); i += 1; continue

        m = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if m:
            blocks.append(_numbered(m.group(1))); i += 1; continue

        if line.strip() == "":
            i += 1; continue

        blocks.append(_paragraph(line)); i += 1

    return blocks


def _rt(text: str) -> list[dict]:
    # Notion caps rich_text content at 2000 chars per chunk.
    return [{"type": "text", "text": {"content": text[:1900]}}]

def _heading(level: int, text: str) -> dict:
    return {"object": "block", "type": f"heading_{level}",
            f"heading_{level}": {"rich_text": _rt(text)}}

def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rt(text)}}

def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rt(text)}}

def _numbered(text: str) -> dict:
    return {"object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": _rt(text)}}


class Tools:
    class Valves(BaseModel):
        notion_token: str = Field(
            default=os.environ.get("NOTION_API_KEY", ""),
            description="Notion Internal Integration Token. The integration must be invited "
                        "to every database/page you want to touch (··· → Connections).",
        )
        default_parent_database_id: str = Field(
            default="f467ff6f772044cda727acfef0d778aa",   # 💻 Projects
            description="Database used by create_notion_page when no parent_id is given.",
        )
        search_page_size: int = Field(default=10, ge=1, le=50,
            description="Max search results to return.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self.citation = True

    def _client(self) -> Client:
        if not self.valves.notion_token:
            raise RuntimeError("NOTION_API_KEY is not configured. Set the Notion valve in OWUI admin.")
        return Client(auth=self.valves.notion_token, notion_version=_NOTION_VERSION)

    # ------------------------------------------------------------------ tools

    def search_notion(self, query: str) -> str:
        """
        Search the Notion workspace for pages and databases matching a query.

        :param query: Free-text search query (Notion does prefix and substring matching).
        :return: JSON array of hits: [{"id": str, "title": str, "url": str, "type": str}, ...]
        """
        try:
            notion = self._client()
            res = notion.search(query=query, page_size=self.valves.search_page_size)
            hits = []
            for r in res.get("results", []):
                title = ""
                if r["object"] == "page":
                    # Title lives in the property whose type is "title".
                    for prop in r.get("properties", {}).values():
                        if prop.get("type") == "title" and prop["title"]:
                            title = "".join(t["plain_text"] for t in prop["title"]); break
                elif r["object"] == "database":
                    title = "".join(t["plain_text"] for t in r.get("title", []))
                hits.append({
                    "id": r["id"],
                    "title": title or "(untitled)",
                    "url": r.get("url", ""),
                    "type": r["object"],
                })
            return json.dumps(hits, indent=2)
        except (APIResponseError, RequestTimeoutError) as e:
            return f"ERROR: notion API: {e}"
        except Exception as e:
            return f"ERROR: search_notion failed: {e!r}"

    def create_notion_page(
        self,
        title: str,
        markdown_content: str,
        parent_id: Optional[str] = None,
    ) -> str:
        """
        Create a new Notion page from markdown content.

        :param title: Page title.
        :param markdown_content: Page body in markdown. Converted to Notion blocks.
        :param parent_id: Optional Notion page or database ID. If omitted, the page is created
                          in the admin-configured default database (currently 💻 Projects).
        :return: JSON {"id": str, "url": str, "title": str} or an ERROR message.
        """
        try:
            notion = self._client()
            parent_id = parent_id or self.valves.default_parent_database_id

            # Determine whether the parent is a page or a database — one extra round trip,
            # but it removes the most common source of 400-errors ("invalid parent").
            try:
                notion.databases.retrieve(database_id=parent_id)
                parent = {"database_id": parent_id}
                properties = {"Name": {"title": [{"text": {"content": title[:200]}}]}}
            except APIResponseError:
                notion.pages.retrieve(page_id=parent_id)
                parent = {"page_id": parent_id}
                properties = {"title": [{"text": {"content": title[:200]}}]}

            blocks = _markdown_to_blocks(markdown_content)
            first_batch = blocks[:_BLOCK_CHUNK]
            rest = blocks[_BLOCK_CHUNK:]

            page = notion.pages.create(parent=parent, properties=properties, children=first_batch)

            # Append remaining blocks in 100-block batches (API limit).
            for start in range(0, len(rest), _BLOCK_CHUNK):
                notion.blocks.children.append(
                    block_id=page["id"],
                    children=rest[start:start + _BLOCK_CHUNK],
                )

            return json.dumps({"id": page["id"], "url": page.get("url", ""), "title": title})
        except (APIResponseError, RequestTimeoutError) as e:
            return f"ERROR: notion API: {e}"
        except Exception as e:
            return f"ERROR: create_notion_page failed: {e!r}"

    def append_to_page(self, page_id: str, markdown_content: str) -> str:
        """
        Append markdown content as new blocks to an existing Notion page.

        :param page_id: Notion page ID (with or without hyphens).
        :param markdown_content: Markdown to append. Converted to Notion blocks.
        :return: JSON {"appended_blocks": int} or an ERROR message.
        """
        try:
            notion = self._client()
            blocks = _markdown_to_blocks(markdown_content)
            appended = 0
            for start in range(0, len(blocks), _BLOCK_CHUNK):
                batch = blocks[start:start + _BLOCK_CHUNK]
                notion.blocks.children.append(block_id=page_id, children=batch)
                appended += len(batch)
            return json.dumps({"appended_blocks": appended, "page_id": page_id})
        except (APIResponseError, RequestTimeoutError) as e:
            return f"ERROR: notion API: {e}"
        except Exception as e:
            return f"ERROR: append_to_page failed: {e!r}"

    def update_page_properties(self, page_id: str, properties_json: str) -> str:
        """
        Update one or more native properties on an existing Notion page (e.g. Status, Tags).

        :param page_id: Notion page ID.
        :param properties_json: JSON object matching Notion's properties payload, e.g.
                                '{"Status": {"select": {"name": "In progress"}}}'.
        :return: JSON {"id": str, "url": str} or an ERROR message.
        """
        try:
            props = json.loads(properties_json)
        except json.JSONDecodeError as e:
            return f"ERROR: properties_json is not valid JSON: {e}"
        try:
            page = self._client().pages.update(page_id=page_id, properties=props)
            return json.dumps({"id": page["id"], "url": page.get("url", "")})
        except (APIResponseError, RequestTimeoutError) as e:
            return f"ERROR: notion API: {e}"
        except Exception as e:
            return f"ERROR: update_page_properties failed: {e!r}"
```

**Differences from Gemini's version:**

- Uses the official `notion-client` (pinned) instead of raw `requests`. The pinned version is declared in `requirements:` so OWUI installs it on first load.
- Real markdown → block conversion that respects Notion's 100-blocks-per-call and 2000-chars-per-rich-text limits — these silently truncate / 400 otherwise.
- Auto-detects whether `parent_id` is a database or a page; Gemini's version assumed page.
- Adds `update_page_properties` so the model can flip a Status / Tag on an existing project page, which is the most common workflow operation and was missing.
- Secrets via `Valves` (set in OWUI admin UI) with env fallback for first boot.

### 3.3 Tool C — Google Workspace (Drive + Gmail)

Save in OWUI as **`Google Workspace`**.

This one needs a one-time **OAuth bootstrap on the host** before the tool can do anything. We use OAuth (not a service account) because:

- Service accounts can't access *personal* Gmail without Workspace domain-wide delegation.
- A user OAuth token + refresh token gives the model the same Drive/Gmail surface you have, no admin console needed.

#### 3.3.1 One-time host bootstrap

```bash
mkdir -p ~/.config/agent-google
chmod 700 ~/.config/agent-google
```

1. In Google Cloud Console: create (or reuse) a project → **APIs & Services → Enable APIs** → enable **Google Drive API** and **Gmail API**.
2. **OAuth consent screen** → External → add yourself as a Test User. Scopes you'll add in step 4 don't need verification while in Testing.
3. **Credentials → Create Credentials → OAuth client ID → Desktop application** → download the JSON → save as `~/.config/agent-google/credentials.json`.
4. Run the bootstrap script (saved at `~/ai/agents/ai-workflow-agent/google_bootstrap.py`, see §3.3.3) once:
   ```bash
   cd ~/ai/agents/ai-workflow-agent
   python -m venv .venv && source .venv/bin/activate
   pip install google-auth google-auth-oauthlib google-api-python-client
   python google_bootstrap.py
   ```
   This opens a browser, you consent, and `token.json` is written into `~/.config/agent-google/`. The tool inside the container reads `/secrets/google/token.json` (read-only mount) and refreshes the access token in memory.

> Why read-only mount: the refresh token is the long-lived credential. Keeping the mount RO means a compromised container can't overwrite or poison it. Token refresh happens in process memory only.

#### 3.3.2 The tool itself

```python
"""
title: Google Workspace
author: Charlie Tolleson
version: 0.2.0
license: MIT
required_open_webui_version: 0.4.0
requirements: google-auth==2.35.0, google-auth-oauthlib==1.2.1, google-api-python-client==2.149.0
"""

from __future__ import annotations

import base64
import json
import os
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
from pydantic import BaseModel, Field

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

_MAX_DOC_BYTES = 1_000_000


class Tools:
    class Valves(BaseModel):
        secrets_dir: str = Field(
            default=os.environ.get("GOOGLE_SECRETS_DIR", "/secrets/google"),
            description="Container path of the read-only directory holding token.json (and credentials.json).",
        )
        send_from: str = Field(
            default="",
            description="Optional 'From' header for Gmail sends. Defaults to the authenticated user.",
        )
        allow_send_email: bool = Field(
            default=False,
            description="Master switch for send_email. Defaults to OFF so the model cannot send mail "
                        "until you explicitly enable it.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self.citation = True
        self._creds: Optional[Credentials] = None

    # ------------------------------------------------------------------ auth

    def _credentials(self) -> Credentials:
        if self._creds and self._creds.valid:
            return self._creds
        token_path = Path(self.valves.secrets_dir) / "token.json"
        if not token_path.exists():
            raise RuntimeError(
                f"token.json not found at {token_path}. Run google_bootstrap.py on the host first."
            )
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())            # in-memory refresh; mount is RO by design
        self._creds = creds
        return creds

    def _drive(self):
        return build("drive", "v3", credentials=self._credentials(), cache_discovery=False)

    def _gmail(self):
        return build("gmail", "v1", credentials=self._credentials(), cache_discovery=False)

    # ------------------------------------------------------------------ Drive

    def drive_search(self, query: str, max_results: int = 10) -> str:
        """
        Search Google Drive using Drive's native query syntax.

        :param query: Drive query, e.g. "name contains 'report'" or "mimeType='application/pdf'".
        :param max_results: Max files to return (1–50).
        :return: JSON array of {"id", "name", "mimeType", "modifiedTime", "webViewLink"}.
        """
        max_results = max(1, min(50, max_results))
        try:
            res = self._drive().files().list(
                q=query, pageSize=max_results,
                fields="files(id, name, mimeType, modifiedTime, webViewLink)",
            ).execute()
            return json.dumps(res.get("files", []), indent=2)
        except HttpError as e:
            return f"ERROR: drive API: {e}"
        except Exception as e:
            return f"ERROR: drive_search failed: {e!r}"

    def drive_read(self, file_id: str) -> str:
        """
        Read the text content of a Drive file. Google Docs are exported to plain text;
        text/* files are returned as UTF-8; binary files are refused.

        :param file_id: Drive file ID.
        :return: File contents as text, or an ERROR message.
        """
        try:
            drive = self._drive()
            meta = drive.files().get(fileId=file_id, fields="id, name, mimeType, size").execute()
            mime = meta.get("mimeType", "")
            size = int(meta.get("size") or 0)
            if size and size > _MAX_DOC_BYTES:
                return f"ERROR: file is {size} bytes; max is {_MAX_DOC_BYTES}."

            if mime == "application/vnd.google-apps.document":
                request = drive.files().export_media(fileId=file_id, mimeType="text/plain")
            elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
                request = drive.files().get_media(fileId=file_id)
            elif mime == "application/vnd.google-apps.spreadsheet":
                request = drive.files().export_media(fileId=file_id, mimeType="text/csv")
            else:
                return f"ERROR: unsupported mimeType '{mime}' for text read."

            from io import BytesIO
            buf = BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue().decode("utf-8", errors="replace")
        except HttpError as e:
            return f"ERROR: drive API: {e}"
        except Exception as e:
            return f"ERROR: drive_read failed: {e!r}"

    def drive_write(self, name: str, content: str, parent_folder_id: Optional[str] = None,
                    mime_type: str = "text/plain") -> str:
        """
        Create a new file in Google Drive with the given text content.

        :param name: File name (including extension, e.g. "summary.md").
        :param content: UTF-8 text content.
        :param parent_folder_id: Optional Drive folder ID. If omitted, the file is placed at root of "My Drive".
        :param mime_type: MIME type to record on the file (default text/plain).
        :return: JSON {"id", "name", "webViewLink"}.
        """
        try:
            metadata = {"name": name}
            if parent_folder_id:
                metadata["parents"] = [parent_folder_id]
            media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type, resumable=False)
            file = self._drive().files().create(
                body=metadata, media_body=media,
                fields="id, name, webViewLink",
            ).execute()
            return json.dumps(file)
        except HttpError as e:
            return f"ERROR: drive API: {e}"
        except Exception as e:
            return f"ERROR: drive_write failed: {e!r}"

    # ------------------------------------------------------------------ Gmail

    def gmail_search(self, query: str, max_results: int = 10) -> str:
        """
        Search Gmail using standard Gmail search syntax.

        :param query: Gmail query, e.g. "from:boss@example.com is:unread newer_than:7d".
        :param max_results: Max messages to return (1–25).
        :return: JSON array of {"id", "threadId", "subject", "from", "date", "snippet"}.
        """
        max_results = max(1, min(25, max_results))
        try:
            gmail = self._gmail()
            res = gmail.users().messages().list(
                userId="me", q=query, maxResults=max_results,
            ).execute()
            out = []
            for m in res.get("messages", []):
                full = gmail.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
                out.append({
                    "id": full["id"],
                    "threadId": full["threadId"],
                    "subject": headers.get("Subject", ""),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                    "snippet": full.get("snippet", ""),
                })
            return json.dumps(out, indent=2)
        except HttpError as e:
            return f"ERROR: gmail API: {e}"
        except Exception as e:
            return f"ERROR: gmail_search failed: {e!r}"

    def gmail_read(self, message_id: str) -> str:
        """
        Read the plain-text body of a Gmail message.

        :param message_id: Gmail message ID.
        :return: JSON {"subject", "from", "to", "date", "body"} or an ERROR message.
        """
        try:
            msg = self._gmail().users().messages().get(
                userId="me", id=message_id, format="full",
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

            def walk(part) -> str:
                if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                for child in part.get("parts", []) or []:
                    text = walk(child)
                    if text:
                        return text
                return ""

            body = walk(msg["payload"]) or msg.get("snippet", "")
            return json.dumps({
                "subject": headers.get("Subject", ""),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "date": headers.get("Date", ""),
                "body": body,
            })
        except HttpError as e:
            return f"ERROR: gmail API: {e}"
        except Exception as e:
            return f"ERROR: gmail_read failed: {e!r}"

    def send_email(self, to: str, subject: str, body: str, cc: Optional[str] = None) -> str:
        """
        Send a plain-text email from the authenticated Gmail account.
        Disabled by default — admin must flip `allow_send_email` valve to True.

        :param to: Comma-separated list of recipient addresses.
        :param subject: Email subject.
        :param body: Plain-text body.
        :param cc: Optional comma-separated CC list.
        :return: JSON {"id", "threadId"} or an ERROR message.
        """
        if not self.valves.allow_send_email:
            return "ERROR: sending email is disabled. Enable the 'allow_send_email' valve to use this tool."
        try:
            mime = EmailMessage()
            mime["To"] = to
            if cc:
                mime["Cc"] = cc
            mime["Subject"] = subject
            if self.valves.send_from:
                mime["From"] = self.valves.send_from
            mime.set_content(body)

            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
            sent = self._gmail().users().messages().send(
                userId="me", body={"raw": raw},
            ).execute()
            return json.dumps({"id": sent["id"], "threadId": sent["threadId"]})
        except HttpError as e:
            return f"ERROR: gmail API: {e}"
        except Exception as e:
            return f"ERROR: send_email failed: {e!r}"
```

#### 3.3.3 Bootstrap helper

Save as `~/ai/agents/ai-workflow-agent/google_bootstrap.py`:

```python
"""One-time OAuth flow: produces ~/.config/agent-google/token.json."""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

CFG = Path.home() / ".config" / "agent-google"
CREDS = CFG / "credentials.json"
TOKEN = CFG / "token.json"

def main() -> None:
    if not CREDS.exists():
        raise SystemExit(f"Place your OAuth client JSON at {CREDS} first.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    print(f"Wrote {TOKEN}")

if __name__ == "__main__":
    main()
```

**Differences from Gemini's stub:**

- Picks OAuth-user (not service account) — correct for personal Gmail.
- Send-email is gated behind an admin valve so the model can't autonomously fire emails until you flip the switch.
- Drive read auto-exports Google Docs to text and Sheets to CSV (Gemini's stub left this as TODO).
- Refresh happens in memory; the credentials file is mounted RO.
- Real bootstrap script provided so this isn't a "TODO: implement auth."

---

## 4. Step 3 — Deployment and verification

### 4.1 Apply the Compose change

```bash
cd ~/ai/ai-router
# Edit docker-compose.yml per §2.2, edit .env per §2.2 (set AGENT_WORKSPACE_HOST and GOOGLE_TOKEN_DIR).
docker compose up -d open-webui
docker compose exec open-webui sh -c 'ls -la /workspace && ls -la /secrets/google'
```

Expected: `/workspace` is writable, `/secrets/google` exists (even if empty until you finish §3.3.1).

### 4.2 Install the tools in Open WebUI

For each of the three tool files in §3:

1. Visit `http://localhost:3000` → sign in → **Admin Panel** (top-right user menu).
2. **Workspace → Tools → "+" New Tool**.
3. Paste the full Python file. Save. OWUI installs the packages listed in the `requirements:` frontmatter into its venv (first-time installs take 30–60 s; watch with `docker compose logs -f open-webui`).
4. Open the tool, click the **Valves** tab, and fill in any admin-only fields:
   - **Notion** → `notion_token` = your Notion Internal Integration Token.
   - **Google Workspace** → leave `secrets_dir` as default; set `send_from` only if you want a non-default From; leave `allow_send_email = False` until you've tested read flows.
   - **Local Files** → defaults are fine; flip `allow_write` to `false` if you want a read-only chat.

### 4.3 Enable tools on a model

`Admin Panel → Settings → Models → claude-sonnet-4-6 (or your default) → Edit`:

- **Tools:** check `Local Files`, `Notion`, `Google Workspace`.
- **Function Calling:** select **Native** (not Default).
- Save.

Repeat for any other model you want to use with tools (`gpt-4o`, `gemini-2.5-pro`, etc.).

### 4.4 Where each secret actually lives

| Secret | Source of truth | How OWUI reads it |
|---|---|---|
| `LITELLM_MASTER_KEY` | `~/ai/ai-router/.env` | already wired through env into `open-webui` service |
| `NOTION_API_KEY` | OWUI **Notion** tool → Valves (preferred), or env var on the `open-webui` service | tool reads from valve; env is only the boot-time default |
| Google OAuth (`token.json`, `credentials.json`) | `~/.config/agent-google/` on host | bind-mount RO at `/secrets/google` |

Avoid the temptation to add `NOTION_API_KEY` to the `open-webui` service env *and* set the valve — the valve wins, and you'll forget which one is real next month. Pick one (valve).

### 4.5 End-to-end smoke test

```bash
# 1. Drop a file in the workspace for the model to find.
mkdir -p ~/agent_workspace/demo
cat > ~/agent_workspace/demo/quarterly_notes.md <<'EOF'
# Q2 review notes
- Shipped the LiteLLM proxy and migrated all agent scripts to /v1.
- Stood up Qdrant + Notion ingest; daily incremental sync via LaunchAgent.
- Open WebUI is now the unified chat for GPT-4o, Gemini 2.5, and Claude 4.6.
- Next: tool-equip the chat (file IO, Notion, Google Workspace).
EOF
```

Then in Open WebUI, pick the `claude-sonnet-4-6` model (or any tool-enabled one) and send:

```
Using your tools:
1. list_directory("demo")
2. read_file("demo/quarterly_notes.md")
3. Summarize the file in 4 bullet points.
4. create_notion_page titled "Q2 review summary — 2026-05-26" in the 💻 Projects database
   (id f467ff6f772044cda727acfef0d778aa) with the summary as markdown_content.
Return the new Notion page URL.
```

**Pass criteria:**
- The chat shows three tool-call chips: `Local Files.list_directory`, `Local Files.read_file`, `Notion.create_notion_page`.
- The final message includes a `notion.so/...` URL that opens to the new page.
- `~/agent_workspace/.agent_audit.log` contains two JSON lines for the `list` and `read` operations.

**If it doesn't fire any tool calls:** the most common cause is `Function Calling: Default` instead of `Native` on the model (§4.3). Second-most-common: tool not enabled on this specific model.

**If the Notion call 404s:** the integration isn't shared with the database. Open the 💻 Projects database in Notion → ··· menu → **Connections** → add the integration.

### 4.6 Google smoke test (after §3.3.1 bootstrap)

```
Search my Gmail for "is:unread newer_than:2d", list the senders and subjects,
then create a Drive file called "inbox-triage-2026-05-26.md" with a markdown
table of the results.
```

Pass criteria: `gmail_search` and `drive_write` chips appear; a new file appears in your Drive root.

---

## 5. Hardening and follow-ups (not in v1, but worth knowing about)

- **Per-tool audit dashboard.** The PostToolUse hook for Claude Code already writes to `~/ai/logs/tool_calls.jsonl`. Consider tailing `/workspace/.agent_audit.log` into the same file so OWUI tool calls and Claude Code tool calls share one timeline.
- **Sub-workspace scopes.** If you start letting *multiple* users into Open WebUI, swap the single `/workspace` mount for per-user subdirectories keyed by `__user__["id"]` and have the tool refuse access outside `/workspace/<user_id>/`.
- **Notion: respect rate limits.** The official client retries on 429 by default; if you start doing bulk appends, batch into background jobs rather than blocking the chat.
- **Google: scope down.** `gmail.modify` is broader than you usually need. If you only want read + send (no label management), use `gmail.readonly` + `gmail.send`.
- **MCP alternative.** Open WebUI ≥0.5 also speaks MCP. If you ever want the same toolset usable from Claude Code *and* OWUI without re-implementing, port these three tools to a single MCP server and connect both clients to it. Not worth doing for v1 — the duplication is small and OWUI's native Tools UI is nicer.

---

## 6. Implementation checklist (handoff)

The implementing agent should execute this in order; each item is independently verifiable.

- [ ] Create `~/agent_workspace` with mode 750.
- [ ] Edit `~/ai/ai-router/docker-compose.yml`: add the two `volumes:` lines and four `environment:` lines on `open-webui` per §2.2.
- [ ] Edit `~/ai/ai-router/.env.example` and `~/ai/ai-router/.env`: add `AGENT_WORKSPACE_HOST` and `GOOGLE_TOKEN_DIR`.
- [ ] `docker compose up -d open-webui`; verify `docker compose exec open-webui ls -la /workspace`.
- [ ] Create `~/.config/agent-google/` (mode 700). Defer populating it until the user obtains OAuth client JSON; the tool tolerates an empty dir and only errors when first invoked.
- [ ] Write `~/ai/agents/ai-workflow-agent/google_bootstrap.py` per §3.3.3.
- [ ] Write the three OWUI tool files into `~/ai/agents/ai-workflow-agent/tools/` for source-control purposes: `local_files.py`, `notion.py`, `google_workspace.py`. (OWUI loads them from its admin UI, not the filesystem, but committing them to git is how we track changes.)
- [ ] Stop. Hand back to the user for the manual steps that can't be scripted: Notion integration share, Google OAuth bootstrap, OWUI Admin Panel paste + valve fill + per-model enable, and the smoke tests in §4.5 and §4.6.
