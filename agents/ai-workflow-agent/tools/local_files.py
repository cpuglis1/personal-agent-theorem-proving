"""
title: Local Files
author: Charlie Tolleson
author_url: https://github.com/charlie
version: 0.2.0
license: MIT
required_open_webui_version: 0.4.0
requirements:
description: Read, write, append, and list files in the sandboxed agent workspace. All paths are confined to /workspace; includes audit logging and binary/size guards.
"""

# NOTE: The triple-quoted block above is the Open WebUI tool front-matter. OWUI
# parses these `key: value` lines (title/author/version/requirements/description)
# to register and render the tool in its admin UI. It is NOT a Python module
# docstring in the conventional sense — it is metadata that OWUI reads from the
# file's first string literal. The narrative module docstring for human/AI
# readers lives in the second string literal further down ("Local Files — Open
# WebUI tool"); both are intentionally preserved.
#
# ---------------------------------------------------------------------------
# File: agents/ai-workflow-agent/tools/local_files.py
#
# Role in the system:
#     An Open WebUI (OWUI) "tool" plugin that exposes filesystem access to chat
#     models as callable functions. OWUI discovers the `Tools` class by
#     convention, instantiates it once, reads any `Valves` for admin config, and
#     turns each public method (list_directory / read_file / write_file /
#     append_file) into a function the LLM can call. Within Charlie's ~/ai stack
#     this gives agents a scratch directory to read inputs from and persist
#     outputs to between turns.
#
# Key design decisions / non-obvious context:
#     * Sandboxing is path-based, not OS-level: every caller path is joined to
#       the workspace root and re-checked with Path.is_relative_to() AFTER
#       resolve(), so symlinks that point outside the root are rejected. There is
#       no chroot — the guarantee is only as strong as _resolve().
#     * The workspace root differs by vantage point: ~/agent_workspace on the
#       host, /workspace inside the open-webui container (a docker-compose bind
#       mount). The AGENT_WORKSPACE env var carries the in-container path.
#     * All tool methods return STRINGS (never raise to the caller). LLM tool
#       calls expect a string result, so every error is converted to an
#       "ERROR: ..." string. The only exception raised internally is
#       PermissionError from _resolve(), which each method catches.
#     * `__user__` is an OWUI-injected kwarg (the calling user's dict). It is
#       used only for the audit log and is optional/defensive everywhere.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


"""
Local Files — Open WebUI tool

Purpose:
    Gives chat models safe, sandboxed access to a single host directory
    (~/agent_workspace on the host, /workspace inside the open-webui container).
    Models can list directories, read text files, write new files, and append
    to existing ones.

Implementation:
    - The workspace root is set via the AGENT_WORKSPACE env var (injected by
      docker-compose) and surfaced as an admin Valve so it's visible in the UI.
    - Every user-supplied path is resolved with Path.resolve() and checked with
      is_relative_to() before any I/O, preventing symlink-based escapes.
    - Binary files are detected by sampling the first 8 KB for null bytes and
      non-printable characters; they are refused rather than returned as garbage.
    - Read and write sizes are hard-capped (1 MB / 5 MB) to prevent context blowup.
    - Every operation is appended as a JSON line to /workspace/.agent_audit.log.
    - The allow_write Valve lets an admin flip the tool to read-only without
      redeploying the container.
"""


_MAX_READ_BYTES = 1_000_000          # 1 MB; refuse larger reads
_MAX_WRITE_BYTES = 5_000_000         # 5 MB per write/append
_MAX_LIST_ENTRIES = 500              # truncate huge directories
_TEXT_SAMPLE_BYTES = 8192            # bytes sniffed for binary detection
_AUDIT_PATH = "/workspace/.agent_audit.log"   # inside container


class Tools:
    """Open WebUI tool entry point exposing sandboxed workspace file operations.

    OWUI instantiates this class once at load time and treats each public method
    as an LLM-callable function. Configuration is provided through the nested
    ``Valves`` model (rendered as admin settings in the OWUI UI). All file
    operations are confined to ``valves.workspace_root`` via :meth:`_resolve`.

    Attributes:
        valves: Admin-configurable settings (workspace root, write toggle).
        citation: OWUI flag; when True the tool's output is shown as a citation
            in the chat transcript.
    """

    class Valves(BaseModel):
        """Admin-configurable settings surfaced in the OWUI tool settings panel.

        Attributes:
            workspace_root: Absolute in-container path that bounds every file
                operation. Defaults to the AGENT_WORKSPACE env var (injected by
                docker-compose), falling back to "/workspace". Editing this in
                the UI changes path resolution only — it does NOT move the
                underlying bind mount, so it must stay in sync with
                docker-compose.yml.
            allow_write: Master switch for write_file / append_file. When False
                the tool is effectively read-only, letting an admin disable
                writes without redeploying the container.
        """

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
        """Initialize the tool with default valves and enable citation output.

        Called once by OWUI when the tool is loaded. Reads the default valve
        values (which themselves pull from the AGENT_WORKSPACE env var).

        Side effects:
            Sets ``self.valves`` and ``self.citation``.
        """
        self.valves = self.Valves()
        self.citation = True

    # ------------------------------------------------------------------ helpers

    def _root(self) -> Path:
        """Return the workspace root as a fully resolved absolute Path.

        Returns:
            The canonicalized (symlinks/.. collapsed) workspace root directory.
            Used as the containment boundary for :meth:`_resolve`.
        """
        return Path(self.valves.workspace_root).resolve()

    def _resolve(self, rel_or_abs: str) -> Path:
        """
        Resolve a user-supplied path against the workspace root and guarantee it
        does not escape (symlinks included).

        This is the single chokepoint enforcing the sandbox. Both relative and
        absolute inputs are supported, but in every case the resolved result
        must live under the workspace root.

        Args:
            rel_or_abs: A path from the LLM/caller. Relative paths are joined to
                the workspace root; absolute paths are resolved as-is.

        Returns:
            The canonicalized absolute Path, guaranteed to be inside the root.

        Raises:
            PermissionError: If the resolved path escapes the workspace root
                (e.g. via "../" traversal or a symlink pointing outside).
        """
        root = self._root()
        # Relative paths are anchored to the root; absolute paths are taken
        # verbatim. resolve() collapses ".." and follows symlinks so the
        # containment check below sees the real on-disk target.
        candidate = (root / rel_or_abs).resolve() if not Path(rel_or_abs).is_absolute() \
                    else Path(rel_or_abs).resolve()
        # Containment check happens AFTER resolve() — this is what defeats
        # symlink/".." escapes.
        if not candidate.is_relative_to(root):
            raise PermissionError(
                f"path '{rel_or_abs}' resolves outside the workspace root '{root}'"
            )
        return candidate

    def _audit(self, user: dict, op: str, target: Path, extra: str = "") -> None:
        """Append a single JSON-line audit record for one file operation.

        Args:
            user: The OWUI ``__user__`` dict (may be empty/None). The caller's
                email, then name, then "unknown" is used as the actor.
            op: Short operation name ("list" | "read" | "write" | "append").
            target: The resolved Path that was operated on.
            extra: Free-form detail (e.g. "bytes=123", "count=10").

        Side effects:
            Appends one JSON line to ``_AUDIT_PATH``.

        Notes:
            Best-effort only: any exception (e.g. read-only filesystem) is
            swallowed so that audit failures never break the actual tool call.
        """
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
        """Heuristically decide whether a byte sample is binary (not text).

        Args:
            sample: The leading bytes of a file (typically the first
                ``_TEXT_SAMPLE_BYTES``).

        Returns:
            True if the sample contains a NUL byte, or if fewer than 70% of its
            bytes are printable ASCII / common whitespace; otherwise False.
            An empty sample is treated as text (returns False).
        """
        # A NUL byte is a near-certain binary signal; short-circuit on it.
        if b"\x00" in sample:
            return True
        # Mostly-non-printable: treat as binary. Printable = visible ASCII
        # (32..126) plus tab(9)/newline(10)/carriage-return(13).
        printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in sample)
        return len(sample) > 0 and (printable / len(sample)) < 0.70

    # ------------------------------------------------------------------ tools

    def list_directory(self, path: str = ".", __user__: Optional[dict] = None) -> str:
        """
        List the contents of a directory inside the agent workspace.

        Entries are sorted case-insensitively by name and capped at
        ``_MAX_LIST_ENTRIES``; if more exist, a final "truncated" sentinel entry
        is appended. Errors are returned as "ERROR: ..." strings, not raised.

        :param path: Directory path relative to the workspace root. Use "." for the root.
        :param __user__: OWUI-injected caller dict, used only for audit logging.
        :return: A JSON object string {"root": str, "entries": [...]} where each
            entry is {"name": str, "type": "file"|"dir"|"truncated",
            "size": int|null}; or an "ERROR: ..." string on failure.

        Side effects:
            Writes a "list" record to the audit log on success.
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

        Guards run in order: existence/regular-file check, size cap
        (``_MAX_READ_BYTES``), then a binary sniff on the first
        ``_TEXT_SAMPLE_BYTES``. Content is decoded as UTF-8 with errors replaced
        so a stray invalid byte never aborts the read.

        :param path: File path relative to the workspace root.
        :param __user__: OWUI-injected caller dict, used only for audit logging.
        :return: File contents as a string, or an "ERROR: ..." message.

        Side effects:
            Writes a "read" record to the audit log on success.
        """
        try:
            target = self._resolve(path)
            if not target.is_file():
                return f"ERROR: not a regular file: {path}"
            size = target.stat().st_size
            if size > _MAX_READ_BYTES:
                return (f"ERROR: file is {size} bytes; refuse to read more than "
                        f"{_MAX_READ_BYTES}. Ask the user to chunk or summarize it.")
            # Read in two stages: sniff the leading sample first so we can bail
            # out on binary files before pulling the whole file into memory, then
            # read the remainder and concatenate.
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

        Gated by ``valves.allow_write`` and capped at ``_MAX_WRITE_BYTES``
        (measured on the UTF-8-encoded bytes, not character count). Existing
        files are fully replaced.

        :param path: File path relative to the workspace root.
        :param content: Full file contents. UTF-8. Overwrites if the file exists.
        :param __user__: OWUI-injected caller dict, used only for audit logging.
        :return: A short "OK: ..." status message, or an "ERROR: ..." message.

        Side effects:
            Creates parent directories, writes/overwrites the target file, and
            writes a "write" record to the audit log on success.
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

        Gated by ``valves.allow_write`` and capped at ``_MAX_WRITE_BYTES`` per
        call (the cap applies to this chunk, not the resulting file size).

        :param path: File path relative to the workspace root.
        :param content: Text to append (no separator added — include your own newline if needed).
        :param __user__: OWUI-injected caller dict, used only for audit logging.
        :return: A short "OK: ..." status message, or an "ERROR: ..." message.

        Side effects:
            Creates parent directories and the file if missing, appends bytes to
            the target, and writes an "append" record to the audit log on success.
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
