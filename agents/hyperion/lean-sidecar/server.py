"""Lean verifier sidecar — a minimal HTTP wrapper around ``lake env lean``.

Exposes ``POST /verify {source, mode} -> {ok, errors, elaborated_term}``, the
contract the Hyperion ``lean_verify`` tool depends on. Runs inside the warm-cache
Mathlib image (see Dockerfile) so a request never cold-builds Mathlib.

How a verification runs
-----------------------
1. The posted ``source`` is written to a throwaway ``.lean`` file inside the
   pre-built Lean project (which already depends on Mathlib, with oleans cached).
2. ``lake env lean <file>`` elaborates it; diagnostics go to stderr as
   ``<file>:<line>:<col>: error|warning: <msg>``.
3. We parse the diagnostics into ``errors`` and decide ``ok`` per mode:
   - ``full``    — no errors AND no ``sorry`` (a ``declaration uses 'sorry'`` warning
     fails the check; the proof must actually close).
   - ``skeleton`` — no errors; ``sorry`` is permitted (the scaffold must type-check
     and its ``have``-chain compose, but the holes are allowed to remain).

``elaborated_term`` is reported as ``None`` for now (extracting it generically needs
a ``#print``/metaprogram pass; the contract permits None and downstream code treats
it as optional).

The soundness chokepoint
------------------------
``POST /axioms {source, decl} -> {ok, axioms, errors}`` appends ``#print axioms <decl>``
to the source, elaborates it, and parses the dependency list Lean reports. This is the
operationalized ``sorryAx`` gate: an unclosed sketch hole is a ``sorry`` that elaborates
to ``sorryAx``, so the axiom set *is* the soundness-and-completeness signal. The client
(``tools/lean_verify.lean_axioms``) and ``crews/soundness.soundness_ok`` decide whether
that set is within the standard sound base. We stay on this sidecar rather than Pantograph
for v1; a Pantograph swap is a later, isolated change.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel

# The pre-built Lean project root (created in the Dockerfile). Overridable for local runs.
PROJECT_DIR = Path(os.environ.get("LEAN_PROJECT_DIR", "/app/leanproject"))
# Where scratch sources are written — must live inside the project so imports resolve.
SCRATCH_DIR = PROJECT_DIR / "Scratch"
# Per-call elaboration timeout (seconds).
LEAN_TIMEOUT = float(os.environ.get("LEAN_TIMEOUT", "120"))

# Matches a Lean diagnostic line: "path:line:col: error: message" (severity captured).
_DIAG_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<sev>error|warning):\s*(?P<msg>.*)$")

# ``#print axioms <decl>`` reports one of two shapes. Parse over the *whole* combined
# output (DOTALL): the bracketed list can wrap across lines, and continuation lines carry
# no ``file:line:col:`` prefix, so a line-by-line scan would split it.
_AXIOMS_LIST_RE = re.compile(r"depends on axioms:\s*\[(?P<names>[^\]]*)\]", re.DOTALL)
_AXIOMS_NONE_RE = re.compile(r"does not depend on any axioms")

app = FastAPI(title="lean-verifier", version="1.0")


class VerifyRequest(BaseModel):
    source: str
    mode: Literal["skeleton", "full"] = "full"


class VerifyResponse(BaseModel):
    ok: bool
    errors: list[str]
    elaborated_term: Optional[str] = None


class AxiomsRequest(BaseModel):
    source: str
    decl: str


class AxiomsResponse(BaseModel):
    # ok: the source elaborated AND a ``#print axioms`` verdict was found for ``decl``.
    # When ok is False the axiom set is meaningless — inspect ``errors``.
    ok: bool
    axioms: list[str]
    errors: list[str]


def _parse_diagnostics(stdout: str, stderr: str) -> tuple[list[str], bool]:
    """Split combined Lean output into (error_messages, saw_sorry).

    Errors are any ``error``-severity diagnostics. ``saw_sorry`` is true when Lean
    reports a ``declaration uses 'sorry'`` warning (or the source plainly contains a
    ``sorry`` token), which distinguishes a not-yet-closed proof from a closed one.
    """
    errors: list[str] = []
    saw_sorry = False
    combined = "\n".join(filter(None, [stdout, stderr]))
    for line in combined.splitlines():
        m = _DIAG_RE.match(line.strip())
        if not m:
            continue
        msg = m.group("msg").strip()
        if m.group("sev") == "error":
            errors.append(f"{m.group('line')}:{m.group('col')}: {msg}")
        if "uses 'sorry'" in msg or "declaration uses 'sorry'" in msg:
            saw_sorry = True
    return errors, saw_sorry


def _parse_axioms(stdout: str, stderr: str) -> tuple[Optional[list[str]], bool]:
    """Parse a ``#print axioms`` verdict from combined output.

    Returns ``(axioms, found)``: ``found`` is True iff a recognizable verdict was
    present. ``axioms`` is the dependency list (``[]`` for "does not depend on any
    axioms"), or ``None`` when no verdict was found (e.g. the decl failed to elaborate).
    """
    combined = "\n".join(filter(None, [stdout, stderr]))
    m = _AXIOMS_LIST_RE.search(combined)
    if m:
        names = [n.strip() for n in re.split(r"[,\s]+", m.group("names")) if n.strip()]
        return names, True
    if _AXIOMS_NONE_RE.search(combined):
        return [], True
    return None, False


def _run_lean(source: str) -> subprocess.CompletedProcess[str]:
    """Write ``source`` to a unique scratch module and elaborate it with ``lake env lean``.

    The caller owns interpreting stdout/stderr. Cleanup of the scratch file + build
    artifacts is best-effort and happens here so every entry point shares it.
    """
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    # A unique module name per call avoids any cross-request olean caching surprises.
    stem = f"S{uuid.uuid4().hex}"
    src_path = SCRATCH_DIR / f"{stem}.lean"
    src_path.write_text(source, encoding="utf-8")
    try:
        return subprocess.run(
            ["lake", "env", "lean", str(src_path)],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=LEAN_TIMEOUT,
        )
    finally:
        for p in (src_path, src_path.with_suffix(".olean"), src_path.with_suffix(".ilean")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/verify", response_model=VerifyResponse)
def verify(req: VerifyRequest) -> VerifyResponse:
    try:
        proc = _run_lean(req.source)
        errors, saw_sorry = _parse_diagnostics(proc.stdout, proc.stderr)
        # A non-zero exit with no parsed error line still means failure — surface raw.
        if proc.returncode != 0 and not errors:
            tail = (proc.stderr or proc.stdout or "lean exited non-zero").strip().splitlines()
            errors = tail[-5:] or ["lean exited non-zero"]
        sorry_present = saw_sorry or ("sorry" in req.source)
        if req.mode == "full":
            ok = not errors and not sorry_present
        else:  # skeleton: sorry permitted, but real errors are not
            ok = not errors
        return VerifyResponse(ok=ok, errors=errors, elaborated_term=None)
    except subprocess.TimeoutExpired:
        return VerifyResponse(ok=False, errors=[f"lean timed out after {LEAN_TIMEOUT}s"])
    except Exception as exc:
        # The sidecar is the prover's oracle, so clients need a structured verdict even
        # when the wrapper itself is misconfigured. Returning 200 with a diagnostic lets
        # health gates and tests distinguish "server process is alive" from "verifier
        # can actually run Lean", instead of surfacing an opaque FastAPI 500.
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return VerifyResponse(ok=False, errors=[f"lean sidecar exception: {detail}"])


@app.post("/axioms", response_model=AxiomsResponse)
def axioms(req: AxiomsRequest) -> AxiomsResponse:
    """Elaborate ``source`` with ``#print axioms <decl>`` appended and return the deps.

    The soundness chokepoint. Elaboration errors (including an unknown ``decl`` because
    the proof itself failed) come back as ``ok=False`` with diagnostics — the axiom set
    is only trustworthy when ``ok`` is True. ``sorryAx`` is reported like any other
    dependency, which is exactly how an unclosed hole surfaces here.
    """
    try:
        probe = f"{req.source}\n\n#print axioms {req.decl}\n"
        proc = _run_lean(probe)
        errors, _ = _parse_diagnostics(proc.stdout, proc.stderr)
        parsed, found = _parse_axioms(proc.stdout, proc.stderr)
        if not found:
            if proc.returncode != 0 and not errors:
                tail = (proc.stderr or proc.stdout or "lean exited non-zero").strip().splitlines()
                errors = tail[-5:] or ["lean exited non-zero"]
            if not errors:
                errors = [f"no '#print axioms' verdict found for '{req.decl}'"]
            return AxiomsResponse(ok=False, axioms=[], errors=errors)
        return AxiomsResponse(ok=True, axioms=parsed or [], errors=errors)
    except subprocess.TimeoutExpired:
        return AxiomsResponse(ok=False, axioms=[], errors=[f"lean timed out after {LEAN_TIMEOUT}s"])
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return AxiomsResponse(ok=False, axioms=[], errors=[f"lean sidecar exception: {detail}"])
