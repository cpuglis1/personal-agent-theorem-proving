"""
The single planner output contract (the implementation plan §4.2).

This module is the one canonical place in Hyperion that defines, parses, and
mutates the planner agent's structured output. The planner writes a markdown
file (`plan.md`) for each task; that file begins with a YAML frontmatter block
whose schema is described by :class:`PlanFrontmatter`. Every downstream consumer
reads the *same* parsed frontmatter rather than re-parsing the planner's prose.

plan.md carries one YAML frontmatter schema that serves every consumer:
  - routing signals      task_type, keywords            (Phase 2)
  - HITL alternatives    options[], selected_option     (Phase 3)
  - auto context         context_brief                  (Phase 4)
  - critic trigger       needs_review                   (existing)

Design notes / non-obvious context:
  - The parser is written ONCE here; the planner record's prompt is edited over
    time to emit more of the schema. The parser is therefore deliberately
    tolerant: it must keep working against plans produced by older or
    not-yet-fully-trained prompts.
  - Graceful degradation is the guiding principle. Missing/malformed input never
    raises out of :func:`parse_plan`; it falls back to safe defaults
    (e.g. missing task_type → "mixed", missing options → one implicit option)
    so the router and HITL flow can always make progress.
  - On-disk location of plan.md is derived from ``settings.tasks_dir`` keyed by
    ``task_id`` (see :func:`_plan_path`), so this module owns both the schema
    and the file layout.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from hyperion.config import settings

# Matches a leading YAML frontmatter block delimited by `---` fences at the very
# top of the document. Group 1 captures the YAML body between the fences.
# re.DOTALL lets `.` span newlines so multi-line frontmatter is captured; the
# non-greedy `.*?` stops at the first closing `---` line.
_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# An *unclosed* leading fence: an opening ``---`` with no closing one. The planner LLM
# routinely emits the whole plan as a frontmatter block and forgets the closing fence, so
# the strict regex above never matches and the entire typed plan (options/subtasks) is
# silently dropped — the prover then degrades to the raw prose request, and Path-A lemma
# retrieval (which needs the clean Lean type) always misses. Group 1 captures everything
# after the opening fence to EOF.
_UNCLOSED_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*)\Z", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[Optional[str], str]:
    """Split plan.md into ``(frontmatter_yaml, markdown_body)``.

    Prefers a properly fenced ``---\\n…\\n---\\n`` block. Falls back to an *unclosed*
    leading fence (opening ``---`` with no closing one), treating the remainder of the file
    as frontmatter and the body as empty — recovering plans the planner wrote without a
    closing fence instead of losing the whole typed plan. Returns ``(None, text)`` when
    there is no leading fence at all, so callers degrade to defaults.
    """
    m = _FRONTMATTER_RE.match(text)
    if m:
        return m.group(1), text[m.end():]
    m = _UNCLOSED_FRONTMATTER_RE.match(text)
    if m:
        return m.group(1), ""
    return None, text

# A ``key: value`` line — at any indent, optionally a ``- `` list-item mapping — whose
# plain-scalar value itself contains a colon (e.g. ``original_request: Prove in Lean 4: …``
# or, nested under an option, ``summary: split into two steps: a then b``). To YAML the
# second colon reads as a nested mapping ("mapping values are not allowed here") and the
# whole block fails to parse — taking the typed sub-goals down with it. The negative
# lookahead skips values the planner already quoted or that open a block/flow/anchor
# (``| > " ' [ { & *``). Block-scalar bodies (the ``scaffold: |`` proof text, whose
# ``have h : T := …`` lines also look like ``key: value``) are excluded structurally in
# :func:`_sanitize_frontmatter`, not by this regex.
_SCALAR_COLON_RE = re.compile(
    r"""^(?P<prefix>\s*(?:-\s+)?)(?P<key>[A-Za-z_][\w-]*):[ \t]+(?![|>&*"'\[{])(?P<val>.*:.*\S)\s*$"""
)
_LEAN_TYPE_RE = re.compile(
    r"""^(?P<prefix>\s*(?:-\s+)?)(?P<key>lean_type):[ \t]+(?P<val>.+\S)\s*$"""
)
# A line that *introduces* a block scalar (``key: |`` / ``key: >`` with optional chomping
# indicator), at any indent and possibly as a list item. Its body is every following line
# that is blank or indented deeper than the introducer — and must be left untouched.
_BLOCK_SCALAR_RE = re.compile(r"""^(?P<indent>\s*)(?:-\s+)?[A-Za-z_][\w-]*:[ \t]*[|>][+-]?\d*[ \t]*$""")
_SCAFFOLD_HAVE_RE = re.compile(
    r"""^\s*have\s+(?P<id>[A-Za-z_][\w']*)\s*:\s*(?P<lean_type>.*?)\s*:=\s*sorry\b""",
    re.MULTILINE,
)


def _sanitize_frontmatter(raw: str) -> str:
    """Quote scalar values (top-level OR nested) that carry an unescaped colon.

    A recovery pass for the common planner failure where an LLM writes a value like
    ``Prove in Lean 4: ...`` or ``summary: two steps: a then b`` into a frontmatter scalar,
    producing YAML that ``safe_load`` rejects. We re-emit only the offending ``key: value``
    lines as double-quoted scalars (escaping any embedded quote/backslash), at any indent,
    while structurally skipping ``|``/``>`` block-scalar bodies (e.g. the ``scaffold`` proof
    text, which legitimately holds colons) so they pass through byte-for-byte.
    """
    out: list[str] = []
    block_indent: Optional[int] = None  # indent of an open block scalar's introducer, or None
    for line in raw.splitlines():
        if block_indent is not None:
            # Inside a block scalar: body is blank lines or deeper-indented content.
            if line.strip() == "" or (len(line) - len(line.lstrip())) > block_indent:
                out.append(line)
                continue
            block_indent = None  # dedented to introducer level or shallower → block closed
        bm = _BLOCK_SCALAR_RE.match(line)
        if bm:
            block_indent = len(bm.group("indent"))
            out.append(line)
            continue
        lm = _LEAN_TYPE_RE.match(line)
        if lm:
            val = lm.group("val").rstrip()
            try:
                yaml.safe_load(f"x: {val}")
                out.append(line)
            except yaml.YAMLError:
                escaped = val.replace("\\", "\\\\").replace('"', '\\"')
                out.append(f'{lm.group("prefix")}{lm.group("key")}: "{escaped}"')
            continue
        m = _SCALAR_COLON_RE.match(line)
        if m:
            val = m.group("val").rstrip().replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'{m.group("prefix")}{m.group("key")}: "{val}"')
        else:
            out.append(line)
    return "\n".join(out)


def _load_frontmatter(raw: str) -> dict:
    """``yaml.safe_load`` a frontmatter block, with a colon-quoting recovery retry.

    Returns the parsed mapping, or ``{}`` when even the sanitized form won't parse (or
    parses to a non-mapping) — so every caller degrades to "empty frontmatter" rather
    than raising. Centralizes the parse so both the reader (:func:`parse_plan`) and the
    writer (:func:`update_plan_frontmatter`) recover identically instead of one crashing.
    """
    for candidate in (raw, _sanitize_frontmatter(raw)):
        try:
            loaded = yaml.safe_load(candidate)
        except yaml.YAMLError:
            continue
        if isinstance(loaded, dict):
            return loaded
        # Parsed cleanly but to a scalar/list — not frontmatter; no retry will help.
        return {}
    return {}


class Subtask(BaseModel):
    """A single unit of work within a :class:`PlanOption`.

    Attributes:
        id: Stable identifier for the subtask (planner-assigned).
        description: Human-readable description of what the subtask entails.
            Defaults to empty so older/looser planner output still validates.
        lean_type: The Lean 4 type of this sub-goal — the proposition a
            ``have <id> : <lean_type> := sorry`` in the scaffold must close
            (the prover's Path-A retrieval query and Path-B synthesis target).
            Defaults to empty so non-prover plans (and older prover plans the
            decomposer hasn't fully learned) still validate.
    """

    id: str
    description: str = ""
    lean_type: str = ""


class PlanOption(BaseModel):
    """One candidate approach the planner proposes for a task.

    Multiple options enable the Phase 3 HITL flow, where a human (or the system)
    picks one via ``PlanFrontmatter.selected_option``.

    Attributes:
        id: Stable identifier for this option (referenced by ``selected_option``).
        summary: Short description of the approach this option represents.
        subtasks: Ordered list of :class:`Subtask` items that make up the option.
        est_tool_calls: Optional planner estimate of tool calls required, used as
            a rough cost/effort signal. ``None`` when the planner omits it.
    """

    id: str
    summary: str = ""
    subtasks: list[Subtask] = Field(default_factory=list)
    est_tool_calls: Optional[int] = None


class PlanFrontmatter(BaseModel):
    """Parsed representation of plan.md's YAML frontmatter — the planner contract.

    This is the single shared structure read by routing (Phase 2), HITL option
    selection (Phase 3), automatic context injection (Phase 4), and the critic
    trigger. All fields have defaults so a partially-populated or empty plan
    still produces a valid model.

    Attributes:
        task_id: The task identifier, if the planner echoed it into the plan.
        original_request: The user's original request text, if captured.
        task_type: Routing signal; normalized to one of
            ``"research" | "code" | "mixed"`` (defaults to ``"mixed"``).
        keywords: Routing/retrieval keywords extracted by the planner.
        context_brief: Auto-generated context summary injected for downstream
            agents (Phase 4); ``None`` until populated.
        needs_review: Whether the critic agent should review the result.
        subtasks: Flat list of subtask strings (legacy/simple shape, distinct
            from the richer :class:`Subtask` objects nested under options).
        options: Candidate :class:`PlanOption` approaches (Phase 3 HITL).
        selected_option: ``id`` of the chosen option, if one has been selected.
        scaffold: The prover's have-chain proof text (one
            ``have <id> : <lean_type> := sorry`` per sub-goal, composed to the
            target theorem) that the Phase-1 skeleton check type-checks in
            ``skeleton`` mode. ``None`` for non-prover plans (and prover plans
            the decomposer hasn't fully learned), so the contract stays tolerant.
    """

    task_id: Optional[str] = None
    original_request: Optional[str] = None
    task_type: str = "mixed"                       # research | code | mixed
    keywords: list[str] = Field(default_factory=list)
    context_brief: Optional[str] = None
    needs_review: bool = False
    subtasks: list[str] = Field(default_factory=list)
    options: list[PlanOption] = Field(default_factory=list)
    selected_option: Optional[str] = None
    scaffold: Optional[str] = None                 # have-chain proof text (prover)

    @field_validator("keywords", mode="before")
    @classmethod
    def _coerce_keywords(cls, v: Any) -> list[str]:
        """Coerce the planner's ``keywords`` into ``list[str]`` before validation.

        Load-bearing tolerance: the LLM emits ``keywords`` in shapes Pydantic's strict
        ``list[str]`` rejects — a bare comma string (``natural numbers, subtraction``),
        a bool when the theorem mentions ``True``/``False`` (YAML coerces it), or a list
        with bool/number elements. A rejection here used to bubble to the salvage path,
        which **drops ``options``** — so a perfectly good plan lost its sub-goals and the
        verified lemma never reached the bank (the snowball stalled). Coercing here keeps
        validation green so ``options`` survive:

          - ``None`` → ``[]``
          - ``str``  → comma-split into trimmed keywords (single token ⇒ one element)
          - scalar (bool/int/float) → ``[str(v)]``
          - list/tuple → each element stringified (bools/numbers included)
        """
        if v is None:
            return []
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        if isinstance(v, (list, tuple)):
            return [str(item).strip() for item in v if str(item).strip()]
        return [str(v)]

    @field_validator("task_id", "original_request", "context_brief", "scaffold", mode="before")
    @classmethod
    def _coerce_str_scalar(cls, v: Any) -> Optional[str]:
        """Stringify scalar fields YAML may have typed as non-``str`` before validation.

        Same tolerance posture as :meth:`_coerce_keywords`: the planner echoes ``task_id: 1``
        (YAML reads it as ``int``) and can emit a numeric/bool ``original_request`` or
        ``scaffold``. Strict ``Optional[str]`` rejects those, and the rejection bubbles to the
        salvage path which **drops ``options``** — so the typed sub-goals are lost and the
        prover degrades to the raw prose request (Path-A retrieval then misses). Coerce any
        non-string scalar to ``str`` here, leaving ``None`` untouched.
        """
        return v if v is None or isinstance(v, str) else str(v)

    def active_subtasks(self) -> list[Subtask]:
        """Return the subtasks of whichever option is currently in effect.

        Resolution order:
          1. The option whose ``id`` matches ``selected_option``, if any.
          2. Otherwise the first option (the planner's default/preferred plan).
          3. Typed ``have`` holes recovered from ``scaffold`` when no options
             exist at all.
          4. An empty list when neither options nor typed scaffold holes exist.

        Returns:
            The list of :class:`Subtask` objects for the active option, or an
            empty list when there are no options.
        """
        if not self.options:
            return _subtasks_from_scaffold(self.scaffold or "")
        chosen = None
        if self.selected_option:
            # Find the explicitly selected option by id; may be None if the id
            # doesn't match any option (e.g. stale/invalid selection).
            chosen = next((o for o in self.options if o.id == self.selected_option), None)
        # Fall back to the first option when nothing was selected or the
        # selection didn't resolve.
        return (chosen or self.options[0]).subtasks


def _subtasks_from_scaffold(scaffold: str) -> list[Subtask]:
    """Recover typed prover subtasks from a scaffold-only decomposer plan.

    Decomposer output sometimes contains the useful have-chain scaffold but omits
    ``options[].subtasks[]`` entirely. In that case, the scaffold itself is the
    typed contract: every ``have hᵢ : T := sorry`` hole is an independently
    dischargeable sub-goal. Recovering those holes preserves fan-out without
    affecting normal structured plans.
    """
    out: list[Subtask] = []
    for match in _SCAFFOLD_HAVE_RE.finditer(scaffold or ""):
        sid = match.group("id").strip()
        lean_type = match.group("lean_type").strip()
        if sid and lean_type:
            out.append(Subtask(id=sid, description=f"prove {lean_type}", lean_type=lean_type))
    return out


def _plan_path(task_id: str):
    """Return the on-disk path to a task's plan.md.

    Args:
        task_id: The task identifier used as the per-task directory name.

    Returns:
        A ``Path`` to ``<settings.tasks_dir>/<task_id>/plan.md`` (the file may
        not exist yet).
    """
    return settings.tasks_dir / task_id / "plan.md"


def parse_plan(task_id: str) -> PlanFrontmatter:
    """Parse a task's plan.md frontmatter into a :class:`PlanFrontmatter`.

    This function never raises on bad input — that is intentional. Every failure
    mode (missing file, no frontmatter block, invalid YAML, non-mapping YAML,
    option shapes the planner hasn't learned yet) degrades to safe defaults
    (notably ``task_type='mixed'``) so the router and HITL flow keep working.

    Args:
        task_id: The task whose plan.md should be read.

    Returns:
        A populated :class:`PlanFrontmatter`. On any parse problem, a default or
        partially-populated model is returned rather than raising.
    """
    path = _plan_path(task_id)
    if not path.exists():
        # No plan written yet -> all defaults.
        return PlanFrontmatter()
    text = path.read_text(encoding="utf-8")
    fm, _body = _split_frontmatter(text)
    if fm is None:
        # File exists but has no leading frontmatter block.
        return PlanFrontmatter()
    # Parse via the shared loader: tolerates malformed YAML (incl. the unquoted-colon
    # scalar the planner sometimes emits) by recovering or degrading to {} — never raises.
    data = _load_frontmatter(fm)
    if not data:
        # No usable mapping (missing/blank/irrecoverable frontmatter) -> defaults.
        return PlanFrontmatter()
    # Normalize task_type to the known set.
    tt = str(data.get("task_type", "mixed")).strip().lower()
    if tt not in ("research", "code", "mixed"):
        tt = "mixed"
    data["task_type"] = tt
    try:
        return PlanFrontmatter.model_validate(data)
    except Exception:
        # Tolerate option shapes the planner hasn't learned yet: salvage the
        # fields we can validate cheaply (coercing types defensively) and drop
        # the parts that failed validation rather than losing the whole plan.
        scaffold = data.get("scaffold")
        return PlanFrontmatter(
            task_type=tt,
            keywords=list(data.get("keywords") or []),
            needs_review=bool(data.get("needs_review", False)),
            subtasks=[str(s) for s in (data.get("subtasks") or [])],
            scaffold=str(scaffold) if scaffold is not None else None,
        )


def update_plan_frontmatter(task_id: str, **fields) -> None:
    """Merge fields into plan.md's frontmatter, preserving the markdown body.
    Used by Phase 3 (selected_option) and Phase 4 (context_brief)."""
    path = _plan_path(task_id)
    body = ""
    data: dict = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(text)
        if fm is not None:
            # Shared loader: recovers the unquoted-colon case (and any malformed YAML)
            # to a dict so the merge below preserves the existing plan instead of
            # crashing the task — the bug that failed runs whose request held a colon.
            # ``_split_frontmatter`` also recovers an unclosed leading fence so a write
            # re-emits a properly fenced block instead of dropping the typed plan.
            data = _load_frontmatter(fm)
    data.update(fields)
    fm = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body.lstrip()}", encoding="utf-8")
