"""Conservative parsing for full Lean theorem/example commands.

The parser is intentionally narrow: it recognizes a single top-level
``theorem``/``lemma``/``example`` command with a ``:=`` proof body and extracts the
declaration header and target proposition. It is not a Lean parser; Lean still
does the authority check later. This exists so benchmark prompts can hand the
pipeline exact formal statements without asking the decomposer to parse imports
or top-level declarations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalBinding:
    names: list[str]
    type: str
    raw: str


@dataclass(frozen=True)
class FormalStatement:
    original: str
    preamble: str
    header: str
    goal: str
    local_context: list[LocalBinding]


_DECL_RE = re.compile(r"(?m)^\s*(?:theorem|lemma|example)\b")
_BINDER_RE = re.compile(r"(\([^()]+\)|\{[^{}]+\}|\[[^\[\]]+\])")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")


def _line_is_preamble(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("import ")
        or stripped.startswith("open ")
        or stripped.startswith("namespace ")
    )


def _find_decl_start(source: str) -> int | None:
    match = _DECL_RE.search(source)
    if not match:
        return None
    prefix = source[: match.start()]
    if all(_line_is_preamble(line) for line in prefix.splitlines()):
        return match.start()
    return None


def _find_top_level_colon(text: str) -> int | None:
    depth = 0
    last: int | None = None
    pairs = {"(": ")", "{": "}", "[": "]"}
    closing = set(pairs.values())
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in pairs:
            depth += 1
            continue
        if ch in closing and depth > 0:
            depth -= 1
            continue
        if ch == ":" and depth == 0:
            last = i
    return last


def _extract_local_context(header: str) -> list[LocalBinding]:
    bindings: list[LocalBinding] = []
    for match in _BINDER_RE.finditer(header):
        raw = match.group(1)
        inner = raw[1:-1].strip()
        if ":" not in inner:
            continue
        names_part, type_part = inner.split(":", 1)
        names = [name for name in _IDENT_RE.findall(names_part) if name != "_"]
        if not names:
            continue
        bindings.append(LocalBinding(names=names, type=type_part.strip(), raw=raw))
    return bindings


def parse_formal_statement(source: str) -> FormalStatement | None:
    """Extract a formal declaration into preamble, header, goal, and local context."""
    if not source or ":=" not in source:
        return None
    original = source.strip()
    decl_start = _find_decl_start(original)
    if decl_start is None:
        return None
    assign = original.find(":=", decl_start)
    if assign < 0:
        return None
    before_assign = original[decl_start:assign].rstrip()
    colon = _find_top_level_colon(before_assign)
    if colon is None:
        return None
    preamble = original[:decl_start].strip()
    header = before_assign[:colon].strip()
    goal = before_assign[colon + 1 :].strip()
    if not header or not goal:
        return None
    return FormalStatement(
        original=original,
        preamble=preamble,
        header=header,
        goal=goal,
        local_context=_extract_local_context(header),
    )


def formal_to_context_dict(formal: FormalStatement) -> dict:
    return {
        "original": formal.original,
        "preamble": formal.preamble,
        "header": formal.header,
        "goal": formal.goal,
        "local_context": [
            {"names": b.names, "type": b.type, "raw": b.raw}
            for b in formal.local_context
        ],
    }


def context_dict_to_decompose_request(data: dict) -> str:
    local = data.get("local_context") or []
    local_lines = [
        f"- {', '.join(item.get('names') or [])} : {item.get('type') or ''}".rstrip()
        for item in local
    ]
    return (
        "Decompose this exact Lean theorem target. The top-level formal statement "
        "has already been parsed by native code; do not author imports or a top-level "
        "theorem command.\n\n"
        f"formal_signature:\n{data.get('header') or ''}\n\n"
        "local_context:\n"
        + ("\n".join(local_lines) if local_lines else "- (none)")
        + "\n\n"
        f"target_goal:\n{data.get('goal') or ''}\n\n"
        "Return a plan.md scaffold for the proof body only. Any independent subgoal "
        "that mentions a local-context identifier must quantify or bind it inside "
        "that subgoal's own Lean type."
    )
