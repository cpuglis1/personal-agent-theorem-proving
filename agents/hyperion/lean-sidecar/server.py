"""Lean verifier sidecar — an HTTP wrapper around a warm `import Mathlib` REPL.

Exposes ``POST /verify {source, mode, profile} -> {ok, errors, elaborated_term}``,
the contract the Hyperion ``lean_verify`` tool depends on, plus the soundness gate
``POST /axioms``. Runs inside the warm-cache Mathlib image (see Dockerfile).

Why a REPL (root cause)
-----------------------
The previous implementation spawned a fresh ``lake env lean`` per verify, cold-loading
the full ``import Mathlib`` umbrella (~5500 oleans, ~5 GB) on every call — tens of
seconds each, and timing out under tighter RAM. Instead we launch
``leanprover-community/repl`` (pinned to the same v4.15.0 toolchain) **once**, send
``import Mathlib`` a single time to build a hot base environment, and verify each
snippet against that environment → sub-second per check.

How a verification runs
-----------------------
1. ``import`` lines are stripped from ``source`` (the base env already imports Mathlib;
   the repl rejects ``import`` in a non-fresh env). ``open`` lines and the
   theorem/example are kept verbatim.
2. The stripped source is sent as ``{"cmd": <source>, "env": BASE_ENV}``. **Every
   command branches from BASE_ENV** — the pristine Mathlib environment — and the
   returned env id is discarded. The repl environment is stateful: reusing a returned
   env would let snippet N see snippet N-1's definitions, silently changing verdicts
   and breaking reproducibility vs. the cold oracle.
3. The repl response carries ``messages`` (each ``severity`` ∈ {error,warning,info},
   ``pos``, ``data``) and a ``sorries`` array. We build ``errors`` from error-severity
   messages and decide ``ok`` per mode:
   - ``full``    — no errors AND no ``sorry``.
   - ``skeleton`` — no errors; ``sorry`` permitted.

The soundness chokepoint
------------------------
``POST /axioms {source, decl} -> {ok, axioms, errors}`` sends the stripped source with
``#print axioms <decl>`` appended (against BASE_ENV) and parses the dependency list Lean
reports. This is the operationalized ``sorryAx`` gate: an unclosed sketch hole is a
``sorry`` that elaborates to ``sorryAx``, so the axiom set *is* the soundness signal.

Robustness
----------
The repl is single-threaded: one in-flight command at a time, serialized by a lock. A
per-command wall guard kills the process if a pathological proof hangs (the pipe is then
unrecoverable); the next request respawns it and re-loads ``import Mathlib`` before
serving. All failures come back as a structured ``ok=False`` verdict, never a 500, so
clients can distinguish "process alive" from "verifier can actually run Lean".
"""

from __future__ import annotations

import json
import os
import re
import threading
import traceback
from collections import deque
from pathlib import Path
from typing import Literal, Optional

import subprocess

from fastapi import FastAPI
from pydantic import BaseModel

# The pre-built Lean project root (created in the Dockerfile). Overridable for local runs.
PROJECT_DIR = Path(os.environ.get("LEAN_PROJECT_DIR", "/app/leanproject"))
# The repl executable built (against the same v4.15.0 toolchain) in the Dockerfile.
REPL_BIN = os.environ.get("LEAN_REPL_BIN", "/app/repl/.lake/build/bin/repl")
# Per-command elaboration wall guard (seconds). Not a fast path: with the warm REPL a
# real verify is sub-second; this only bounds a pathological hang.
LEAN_TIMEOUT = float(os.environ.get("LEAN_TIMEOUT", "120"))
# The one-time `import Mathlib` boot is far heavier than any single verify.
IMPORT_TIMEOUT = float(os.environ.get("LEAN_IMPORT_TIMEOUT", "600"))

# ``#print axioms <decl>`` reports one of two shapes. Parse over the *whole* combined
# message data (DOTALL): the bracketed list can wrap across lines.
_AXIOMS_LIST_RE = re.compile(r"depends on axioms:\s*\[(?P<names>[^\]]*)\]", re.DOTALL)
_AXIOMS_NONE_RE = re.compile(r"does not depend on any axioms")
# An `import ...` line (content only; the trailing newline is preserved so error line
# numbers stay aligned with the source the client sent).
_IMPORT_RE = re.compile(r"^[ \t]*import\b.*$", re.MULTILINE)

app = FastAPI(title="lean-verifier", version="2.0")


class VerifyRequest(BaseModel):
    source: str
    mode: Literal["skeleton", "full"] = "full"
    profile: Literal["core", "mathlib"] = "core"


class VerifyResponse(BaseModel):
    ok: bool
    errors: list[str]
    elaborated_term: Optional[str] = None


class AxiomsRequest(BaseModel):
    source: str
    decl: str
    profile: Literal["core", "mathlib"] = "core"


class AxiomsResponse(BaseModel):
    # ok: the source elaborated AND a ``#print axioms`` verdict was found for ``decl``.
    # When ok is False the axiom set is meaningless — inspect ``errors``.
    ok: bool
    axioms: list[str]
    errors: list[str]


class ReplError(RuntimeError):
    """The repl process is unusable (died, never booted, or protocol broke)."""


class ReplTimeout(RuntimeError):
    """A command exceeded its wall guard; the process has been killed."""


class LeanRepl:
    """Long-lived ``leanprover-community/repl`` process with a hot ``import Mathlib`` env.

    Thread-safe: a single lock serializes commands (the repl is single-threaded). Every
    public command branches from BASE_ENV so verdicts never leak state between requests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._base_env: Optional[int] = None
        self._stderr_tail: "deque[str]" = deque(maxlen=50)

    # -- process lifecycle (caller holds self._lock) -----------------------
    def _spawn(self) -> None:
        # `lake env` injects LEAN_PATH for /app/leanproject's deps (Mathlib oleans) and
        # then execs the repl, so `import Mathlib` resolves against the cached build.
        self._proc = subprocess.Popen(
            ["lake", "env", REPL_BIN],
            cwd=str(PROJECT_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stderr_tail.clear()
        threading.Thread(
            target=self._drain_stderr, args=(self._proc.stderr,), daemon=True
        ).start()

    def _drain_stderr(self, pipe) -> None:
        # Keep stderr drained so the process never blocks on a full pipe, and retain a
        # tail to explain a crash. (Elaboration diagnostics arrive on stdout as JSON;
        # stderr only carries process-level failures.)
        try:
            for line in pipe:
                self._stderr_tail.append(line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
        self._proc = None
        self._base_env = None

    def _alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.poll() is None
            and self._base_env is not None
        )

    def _ensure(self) -> None:
        if self._alive():
            return
        self._kill()
        self._spawn()
        # One-time hot Mathlib load → BASE_ENV. No `env` field ⇒ a fresh environment,
        # which is the only place `import` is allowed.
        resp = self._exchange({"cmd": "import Mathlib"}, IMPORT_TIMEOUT)
        env = resp.get("env")
        if env is None:
            tail = "; ".join(self.stderr_tail())
            self._kill()
            raise ReplError(
                f"repl returned no env for `import Mathlib`: {resp}"
                + (f" | stderr: {tail}" if tail else "")
            )
        self._base_env = int(env)

    # -- raw protocol (caller holds self._lock) ----------------------------
    def _exchange(self, obj: dict, timeout: float) -> dict:
        """Send one JSON command, read one JSON response, with a wall guard.

        The repl reads a command terminated by a blank line and prints its JSON response
        (pretty-printed, possibly multi-line) followed by a blank line. We read until that
        blank-line delimiter. A timeout means the pipe is mid-response and unrecoverable,
        so we kill the process (the next call respawns + reloads).
        """
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise ReplError("repl process is not running")
        try:
            proc.stdin.write(json.dumps(obj) + "\n\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ReplError(f"failed to write to repl: {exc}") from exc

        result: dict = {}
        errbox: list[Exception] = []

        def _reader() -> None:
            try:
                lines: list[str] = []
                while True:
                    line = proc.stdout.readline()
                    if line == "":  # EOF → the process died mid-response
                        raise ReplError("repl closed stdout (process died)")
                    if line.strip() == "":
                        if lines:
                            break  # blank-line delimiter after a response
                        continue  # tolerate leading blank lines
                    lines.append(line)
                result.update(json.loads("".join(lines)))
            except Exception as exc:  # noqa: BLE001 — re-raised to the caller below
                errbox.append(exc)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self._kill()  # unblocks the reader (EOF) and forces a clean respawn next call
            raise ReplTimeout(f"repl timed out after {timeout}s")
        if errbox:
            raise errbox[0]
        return result

    # -- public API --------------------------------------------------------
    def command(self, source: str, timeout: float = LEAN_TIMEOUT) -> dict:
        """Run ``source`` against pristine BASE_ENV; return the raw repl response dict."""
        with self._lock:
            self._ensure()
            return self._exchange({"cmd": source, "env": self._base_env}, timeout)

    def booted(self) -> bool:
        with self._lock:
            return self._alive()

    def stderr_tail(self, n: int = 5) -> list[str]:
        return list(self._stderr_tail)[-n:]


REPL = LeanRepl()


def _strip_imports(source: str) -> str:
    """Remove ``import`` lines (the base env already imports Mathlib); keep everything
    else, including ``open`` directives. Newlines are preserved so reported line/col
    positions still line up with the source the client posted."""
    return _IMPORT_RE.sub("", source)


def _errors_and_sorry(resp: dict) -> tuple[list[str], bool]:
    """Map a repl response to (error_messages, saw_sorry).

    Errors are error-severity messages, formatted ``"{line}:{col}: {msg}"`` to match the
    parsing the cold oracle produced. ``saw_sorry`` is true when the repl reports any
    ``sorries`` or a ``declaration uses 'sorry'`` warning.
    """
    errors: list[str] = []
    saw_sorry = bool(resp.get("sorries"))
    for m in resp.get("messages") or []:
        data = (m.get("data") or "").strip()
        pos = m.get("pos") or {}
        line = pos.get("line", 0)
        col = pos.get("column", 0)
        if m.get("severity") == "error":
            errors.append(f"{line}:{col}: {data}")
        if "uses 'sorry'" in data:
            saw_sorry = True
    return errors, saw_sorry


def _axioms_blob(resp: dict) -> str:
    """Concatenate all message data so the ``#print axioms`` verdict (an info message,
    possibly wrapped) can be parsed across lines."""
    return "\n".join((m.get("data") or "") for m in (resp.get("messages") or []))


def _parse_axioms(blob: str) -> tuple[Optional[list[str]], bool]:
    """Parse a ``#print axioms`` verdict. Returns ``(axioms, found)``: ``axioms`` is the
    dependency list (``[]`` for "does not depend on any axioms"), or ``None`` when no
    verdict was found (e.g. the decl failed to elaborate)."""
    m = _AXIOMS_LIST_RE.search(blob)
    if m:
        names = [n.strip() for n in re.split(r"[,\s]+", m.group("names")) if n.strip()]
        return names, True
    if _AXIOMS_NONE_RE.search(blob):
        return [], True
    return None, False


def _profile_errors(source: str, profile: str) -> list[str]:
    """Policy checks that sit above Lean elaboration."""
    if profile == "core" and re.search(r"^\s*import\b", source, re.MULTILINE):
        return ["core profile rejects import statements; use profile='mathlib' for Mathlib proofs"]
    return []


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "profiles": ["core", "mathlib"],
        "project_dir": str(PROJECT_DIR),
        "repl_booted": REPL.booted(),
    }


@app.post("/verify", response_model=VerifyResponse)
def verify(req: VerifyRequest) -> VerifyResponse:
    try:
        profile_errors = _profile_errors(req.source, req.profile)
        if profile_errors:
            return VerifyResponse(ok=False, errors=profile_errors, elaborated_term=None)
        resp = REPL.command(_strip_imports(req.source))
        errors, saw_sorry = _errors_and_sorry(resp)
        sorry_present = saw_sorry or ("sorry" in req.source)
        if req.mode == "full":
            ok = not errors and not sorry_present
        else:  # skeleton: sorry permitted, but real errors are not
            ok = not errors
        return VerifyResponse(ok=ok, errors=errors, elaborated_term=None)
    except ReplTimeout:
        return VerifyResponse(ok=False, errors=[f"lean timed out after {LEAN_TIMEOUT}s"])
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        tail = "; ".join(REPL.stderr_tail())
        msg = f"lean sidecar exception: {detail}" + (f" | stderr: {tail}" if tail else "")
        return VerifyResponse(ok=False, errors=[msg])


@app.post("/axioms", response_model=AxiomsResponse)
def axioms(req: AxiomsRequest) -> AxiomsResponse:
    """Elaborate ``source`` with ``#print axioms <decl>`` appended and return the deps.

    The soundness chokepoint. Elaboration errors (including an unknown ``decl`` because
    the proof itself failed) come back as ``ok=False`` with diagnostics — the axiom set
    is only trustworthy when ``ok`` is True. ``sorryAx`` is reported like any other
    dependency, which is exactly how an unclosed hole surfaces here.
    """
    try:
        profile_errors = _profile_errors(req.source, req.profile)
        if profile_errors:
            return AxiomsResponse(ok=False, axioms=[], errors=profile_errors)
        probe = f"{_strip_imports(req.source)}\n\n#print axioms {req.decl}\n"
        resp = REPL.command(probe)
        errors, _ = _errors_and_sorry(resp)
        parsed, found = _parse_axioms(_axioms_blob(resp))
        if not found:
            if not errors:
                errors = [f"no '#print axioms' verdict found for '{req.decl}'"]
            return AxiomsResponse(ok=False, axioms=[], errors=errors)
        return AxiomsResponse(ok=True, axioms=parsed or [], errors=errors)
    except ReplTimeout:
        return AxiomsResponse(ok=False, axioms=[], errors=[f"lean timed out after {LEAN_TIMEOUT}s"])
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        tail = "; ".join(REPL.stderr_tail())
        msg = f"lean sidecar exception: {detail}" + (f" | stderr: {tail}" if tail else "")
        return AxiomsResponse(ok=False, axioms=[], errors=[msg])
