"""Prover native handlers — the deterministic DAG citizens of ``lean-prove``.

This module registers the Lean prover's four native handlers (build plan §1 / Phase 4)
on import, mirroring how ``crews.native`` self-registers its ``echo`` handler:

  - ``retrieve``       — Path A: pull applicable banked lemmas for a sub-goal and write
                         the top candidate (+ ranked next-best list) to the blackboard.
  - ``skeleton_check`` — type-check the decomposer's scaffold in ``skeleton`` mode (the
                         have-chain composes to the target; ``sorry`` elaborates — P1).
  - ``verify``         — the native CONTROLLER (§1a): the kernel is the verdict; the
                         handler owns the deterministic routing (Path-A next-best vs.
                         Path-B repair) and the bounded repair loop, delegating ONLY the
                         repair *proposal* to the ``repair`` agent. Never an LLM verdict.
  - ``bank``           — assemble the sorry-free ``artifacts/result.lean`` from the
                         scaffold + discharged sub-goals, full-verify it, and store each
                         winning lemma (loud writes — surfaces a failed bank into the
                         node result).

Fan-out across multiple ``sorry`` sub-goals is expressed as ordinary DAG nodes sharing
one blackboard, namespaced by sub-goal id (see :func:`_subgoal_id` / :func:`_bb_key`):
``retrieve`` ‖ ``synthesize`` for a sub-goal share an upstream so they run in one wave
(Path A ‖ Path B for free), and ``verify`` lists both as upstream so it waits for both.
This is preferred over the subworkflow hand-off (which is ``result.md``/blackboard-
isolated and awkward for proof-carrying); the subworkflow seam stays available and is
exercised by ``test_subworkflow.py``.

The oracle, imported BY NAME
----------------------------
``verify_lean`` is imported by name into this module so tests can patch it where it is
used: ``mock_lean(targets=("hyperion.crews.lean_handlers.verify_lean",))``. Likewise
:func:`propose_repair` is a module-level seam tests patch with an ``AsyncMock`` to assert
the repair agent is delegated to — without an LLM, and without ever letting a repair
proposal fake a pass (only ``verify_lean`` sets ``ok``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from hyperion.config import settings
from hyperion.crews.lemma_compare import build_triple, choose_winner, generality_score
from hyperion.crews.lean_statement import (
    context_dict_to_decompose_request,
    formal_to_context_dict,
    parse_formal_statement,
)
from hyperion.crews.native import NativeNodeCtx, register_native_handler
from hyperion.crews.plan_contract import Subtask, parse_plan
from hyperion.crews.soundness import soundness_ok, source_declares_gap
from hyperion.memory import concept_bank, lemma_bank
from hyperion.tools.lean_verify import verify_lean
from hyperion.tools.lemma_retrieval import _lemma_type, retrieve_applicable_lemmas

logger = logging.getLogger(__name__)


class ProofFailed(RuntimeError):
    """Raised by ``verify`` when no path closes the sub-goal after Path-A candidates
    and the bounded repair loop are exhausted. Propagates through the runner's normal
    try/except into a ``failed`` run result — a sub-goal that cannot be discharged must
    fail the run cleanly, never silently report ``done`` with an undischarged ``sorry``."""


# ---------------------------------------------------------------------------
# Sub-goal resolution + blackboard key namespacing
# ---------------------------------------------------------------------------


def _subgoal_id(ctx: NativeNodeCtx) -> str:
    """The sub-goal id this node operates on.

    Convention: a prover native node's ``instruction`` (when set) names the sub-goal id
    it handles, so the same handler can be instantiated once per ``sorry`` (each node
    carrying a different id). When unset, fall back to the first active subtask's id
    (the single-sub-goal happy path of the shipped ``lean-prove`` workflow), else ``"0"``.
    """
    if ctx.node.instruction:
        return ctx.node.instruction.strip()
    subs = parse_plan(ctx.task_id).active_subtasks()
    return subs[0].id if subs else "0"


def _subgoal(ctx: NativeNodeCtx, sg_id: str) -> Optional[Subtask]:
    """The :class:`Subtask` for ``sg_id`` from the active plan option, or None."""
    for s in parse_plan(ctx.task_id).active_subtasks():
        if s.id == sg_id:
            return s
    return None


# Leading natural-language framing the planner/user wraps a goal in, e.g.
# "Prove that 0 + 7 = 7." → "0 + 7 = 7". Stripped so the *retrieval query* (and the repair
# goal) is the bare Lean type, not English — prose embeds far from the banked lemma types,
# so a request like "Prove that 0 + 7 = 7." retrieves NOTHING while "0 + 7 = 7" surfaces the
# applicable ∀-lemma. The load-bearing safety net for when the decomposer's YAML is
# unparseable (the prover then falls back to ``ctx.request`` for the goal type).
_GOAL_PROSE_PREFIX_RE = re.compile(
    r"^\s*(?:please\s+)?(?:prove|show|verify|establish|demonstrate)\b"
    r"(?:\s+(?:in\s+lean(?:\s*4)?|that|the\s+following|the\s+statement|the\s+theorem|:))*"
    r"\s*:?\s*",
    re.IGNORECASE,
)


def _prose_to_goal_type(request: str) -> str:
    """Best-effort strip of natural-language framing from a goal request.

    Removes a leading ``Prove/Show/Verify … that`` clause and a trailing period so the
    result is the bare Lean type when the request is a thin wrapper around one. Conservative:
    returns the original (trimmed) text unchanged when nothing matches or stripping would
    empty it — a degraded query still runs, it just never *worsens* the request.
    """
    if not request:
        return request
    stripped = _GOAL_PROSE_PREFIX_RE.sub("", request).strip().rstrip(".").strip()
    return stripped or request.strip()


def _goal_type(ctx: NativeNodeCtx, sg_id: str) -> str:
    """The Lean type of sub-goal ``sg_id`` (the retrieval query + synthesis target).

    Prefers the plan contract's ``lean_type``; degrades to the run request (the target
    theorem, with natural-language framing stripped) when the plan has no typed sub-goal —
    a degraded query still runs, and stripping the prose keeps Path-A retrieval usable.
    """
    sub = _subgoal(ctx, sg_id)
    if sub and sub.lean_type:
        return _threaded_goal_type(ctx, sub)
    return _prose_to_goal_type(ctx.request)


def _formal_context(ctx: NativeNodeCtx) -> dict[str, Any] | None:
    raw = ctx.get("formal_statement_ingestion")
    return raw if isinstance(raw, dict) and raw.get("goal") and raw.get("header") else None


def _target_goal_type(ctx: NativeNodeCtx) -> str:
    formal = _formal_context(ctx)
    if formal:
        return str(formal["goal"])
    return _prose_to_goal_type(ctx.request)


def _formal_command_from_body(body: str, formal: dict[str, Any]) -> str:
    preamble = (formal.get("preamble") or "").strip()
    header = (formal.get("header") or "").strip()
    goal = (formal.get("goal") or "").strip()
    command = f"{header} :\n  {goal} := by\n{_indent_tactic_body(body)}"
    return f"{preamble}\n\n{command}" if preamble else command


def _formal_binding_names(formal: dict[str, Any] | None) -> list[str]:
    names: list[str] = []
    if not formal:
        return names
    for item in formal.get("local_context") or []:
        for name in item.get("names") or []:
            names.append(str(name))
    return names


def _subgoal_mentions_formal_context(sub: Subtask, formal: dict[str, Any] | None) -> bool:
    names = set(_formal_binding_names(formal))
    if not names:
        return False
    tokens = set(_LEAN_IDENT_RE.findall(sub.lean_type or ""))
    return bool(tokens & names)


def _threaded_goal_type_from_formal(sub: Subtask, formal: dict[str, Any] | None) -> str:
    """Closed goal used by independent subgoal workers."""
    lean_type = sub.lean_type or ""
    if not _subgoal_mentions_formal_context(sub, formal):
        return lean_type
    binders = " ".join((item.get("raw") or "").strip() for item in formal.get("local_context") or [])
    return f"∀ {binders}, {lean_type}" if binders else lean_type


def _threaded_goal_type(ctx: NativeNodeCtx, sub: Subtask) -> str:
    """Goal used by retrieve/synthesize for a subgoal.

    If a subgoal mentions theorem-local variables from an exact formal statement, prove it as
    a closed universally quantified proposition; the bank later instantiates that proof back
    inside the theorem body.
    """
    return _threaded_goal_type_from_formal(sub, _formal_context(ctx))


def _threaded_instantiation_args(sub: Subtask, formal: dict[str, Any] | None) -> list[str]:
    return _formal_binding_names(formal) if _subgoal_mentions_formal_context(sub, formal) else []


def _split_top_level_conjunction(goal: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    in_string = False
    escaped = False
    pairs = {"(": ")", "{": "}", "[": "]"}
    closing = set(pairs.values())
    for i, ch in enumerate(goal or ""):
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
        if ch == "∧" and depth == 0:
            parts.append(goal[start:i].strip())
            start = i + 1
    if parts:
        parts.append(goal[start:].strip())
    return [p for p in parts if p]


def _lean_type_key(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip())


def _native_closing_for_subtasks(
    goal: str, subtasks: list[Subtask], explicit_closer: str | None = None
) -> str:
    if not subtasks:
        return ""
    # (B) Prefer the decomposer's own closing tactic when it supplied one. The kernel
    # (skeleton_check, then the final bank verify) arbitrates whether it actually composes
    # the sub-goals into the target, so any closer shape is allowed — `exact h1.trans h2`,
    # `linarith [h1, h2]`, a `calc`, etc. — not just the two the heuristic below can guess.
    if explicit_closer and explicit_closer.strip():
        return explicit_closer.strip()
    conjuncts = _split_top_level_conjunction(goal)
    if len(conjuncts) == len(subtasks) and all(
        _lean_type_key(part) == _lean_type_key(sub.lean_type)
        for part, sub in zip(conjuncts, subtasks)
    ):
        return "exact " + "⟨" + ", ".join(sub.id for sub in subtasks) + "⟩"
    return f"exact {subtasks[-1].id}"


def _native_scaffold_from_subtasks(
    ctx: NativeNodeCtx, subtasks: list[Subtask], closer: str | None = None
) -> str:
    lines = [f"have {sub.id} : {sub.lean_type} := sorry" for sub in subtasks if sub.id and sub.lean_type]
    if not lines:
        return ""
    close = _native_closing_for_subtasks(_target_goal_type(ctx), subtasks, closer)
    return "\n".join([*lines, close]) + "\n"


def _proof_body_for_hole(proof: str, sub: Subtask, formal: dict[str, Any] | None) -> str:
    args = _threaded_instantiation_args(sub, formal)
    if not args:
        return proof
    # The threaded proof closes the universally-quantified form (∀ <binders>, T); to fill the
    # instance hole we instantiate it at the parent theorem's binders. The proof term may be a
    # bare ``by`` tactic block, which Lean cannot elaborate as a function applied to arguments
    # without an expected type ("invalid 'by' tactic, expected type has not been provided").
    # Ascribe it to the threaded ∀-type first so the block elaborates against a known goal, then
    # apply the binders. The kernel (final bank verify) still arbitrates the assembled proof.
    threaded = _threaded_goal_type_from_formal(sub, formal)
    return f"by exact (({proof} : {threaded})) {' '.join(args)}"


def _proof_body_from_scaffold(scaffold: str) -> str:
    s = _sanitize_scaffold((scaffold or "").strip())
    if not _looks_like_declaration(s):
        return s
    marker = ":= by"
    idx = s.find(marker)
    if idx < 0:
        return s
    return s[idx + len(marker) :].strip()


def _scaffold_target_command(ctx: NativeNodeCtx, scaffold: str) -> str:
    formal = _formal_context(ctx)
    if formal:
        return _formal_command_from_body(_proof_body_from_scaffold(scaffold), formal)
    return _scaffold_as_command(scaffold, _target_goal_type(ctx))


async def formal_ingest_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Parse an exact Lean command before LLM decomposition, when present."""
    formal = parse_formal_statement(ctx.request)
    if formal is None:
        ctx.put("formal_statement_ingestion", None)
        return {"handler": "formal_ingest", "ok": True, "ingested": False}
    data = formal_to_context_dict(formal)
    ctx.put("formal_statement_ingestion", data)
    ctx.put("formal_statement", formal.original)
    ctx.put("formal_preamble", formal.preamble)
    ctx.put("formal_header", formal.header)
    ctx.put("formal_goal", formal.goal)
    ctx.put("formal_local_context", data["local_context"])
    ctx.put("decompose_request", context_dict_to_decompose_request(data))
    ctx.progress(
        f"[formal_ingest] parsed formal statement with {len(formal.local_context)} local binding group(s)"
    )
    return {
        "handler": "formal_ingest",
        "ok": True,
        "ingested": True,
        "goal": formal.goal,
        "local_context": data["local_context"],
        "has_preamble": bool(formal.preamble),
    }


def _identity_plan_md(task_id: str, request: str, goal: str) -> str:
    """The plan.md for the identity decomposition: the whole goal as one sub-lemma,
    closed by ``exact h1``. This scaffold ALWAYS type-checks in skeleton mode (it is
    literally ``have h1 : G := sorry; exact h1``), so it is both the deterministic
    smoke-path plan and the guaranteed-valid floor for the skeleton gate."""
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "task_type: code\n"
        f"original_request: {json.dumps(request)}\n"
        "selected_option: a\n"
        "scaffold: |\n"
        f"  have h1 : {goal} := sorry\n"
        "  exact h1\n"
        "options:\n"
        "  - id: a\n"
        "    summary: Prove the target proposition directly.\n"
        "    closer: exact h1\n"
        "    subtasks:\n"
        "      - id: h1\n"
        "        description: Prove the target proposition.\n"
        f"        lean_type: {json.dumps(goal)}\n"
        "---\n\n"
        "# Lean decomposition\n"
        "\n"
        "Single-subgoal scaffold for the target proposition.\n"
    )


def write_identity_plan(task_id: str, request: str, goal: str) -> None:
    """Persist the identity decomposition plan.md for ``task_id``.

    Shared by the deterministic decomposer (:func:`lean_decompose_handler`) and the
    skeleton-gate floor in the runner: when an LLM decomposition cannot be made to
    compose, falling back to this single-hole scaffold lets the prover attempt the goal
    directly instead of failing the run on the (mechanically trivial) skeleton step.
    """
    path = settings.tasks_dir / task_id / "plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_identity_plan_md(task_id, request, goal), encoding="utf-8")


async def lean_decompose_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Write a deterministic single-subgoal Lean plan for the prover workflow.

    This keeps the critical ``lean-prove`` smoke path out of the agent/ReAct loop while
    preserving the same ``plan.md`` contract consumed by skeleton/retrieve/synthesize.
    Richer decomposition can return to the decomposer agent later; for now one typed
    ``have`` is enough to make the native prover pipeline deterministic.
    """
    goal = _target_goal_type(ctx)
    write_identity_plan(ctx.task_id, ctx.request, goal)
    ctx.progress("[decompose] wrote deterministic single-subgoal Lean plan")
    return {
        "handler": "lean_decompose",
        "subgoals": 1,
        "selected_option": "a",
        "goal": goal,
        "scaffold": f"have h1 : {goal} := sorry\nexact h1",
    }


def _bb_key(base: str, sg_id: str) -> str:
    """Blackboard key for ``base`` namespaced to sub-goal ``sg_id`` (e.g. ``candidate_a:0``).

    Namespacing lets multiple sub-goal pipelines share one blackboard without colliding,
    so a workflow can fan out N sub-goals as N node-triples over a single context store.
    """
    return f"{base}:{sg_id}"


_IMPORT_LINE_RE = re.compile(r"^\s*import\s+\S+.*$", re.MULTILINE)
_LEADING_LEMMA_RE = re.compile(r"(?m)^(\s*)lemma(\s)")
# Synthesizers reliably name a self-contained proof ``example`` — but ``example`` is a Lean
# keyword (the anonymous-declaration form), not a legal identifier, so ``lemma example : T
# := p`` / ``theorem example : T := p`` fail to parse. The intent is the anonymous
# ``example : T := p``; drop the redundant keyword. Run before the lemma→theorem rewrite so
# both spellings collapse here and a plain ``lemma foo`` still rewrites to ``theorem foo``.
_NAMED_EXAMPLE_RE = re.compile(r"(?m)^(\s*)(?:lemma|theorem)\s+example\b")


def _sanitize_lean_source(source: str, *, profile: str | None = None) -> str:
    """Scrub dialect tics LLM synthesizers reliably emit.

    Core profile keeps the historical behavior: strip import lines, normalize
    ``lemma`` to ``theorem``, and turn ``lemma/theorem example`` into an anonymous
    ``example``. Mathlib profile preserves imports while retaining the parser-safe
    keyword normalizations.
    """
    if not source:
        return source
    selected_profile = (profile or settings.lean_profile or "core").strip().lower()
    cleaned = source if selected_profile == "mathlib" else _IMPORT_LINE_RE.sub("", source)
    cleaned = _NAMED_EXAMPLE_RE.sub(r"\1example", cleaned)
    cleaned = _LEADING_LEMMA_RE.sub(r"\1theorem\2", cleaned)
    return cleaned.lstrip("\n")


def _indent_tactic_body(body: str) -> str:
    return "\n".join(f"  {line}" if line.strip() else line for line in body.strip().splitlines())


# Lean-3 / Mathlib habit LLM decomposers reliably emit: a comma separating tactics
# (``have h : T := sorry,``). Lean 4 tactic blocks are newline/``;``-separated — a comma
# at end of a tactic line makes the kernel reject the whole block ("unexpected token ','").
_TRAILING_COMMA_RE = re.compile(r",[ \t]*$", re.MULTILINE)


def _sanitize_scaffold(scaffold: str) -> str:
    """Scrub decomposer dialect tics from a have-chain scaffold.

    Mechanical and idempotent (mirrors :func:`_sanitize_lean_source`): strip a comma at the
    end of a tactic line — a Lean-3 ``:= sorry,`` separator that makes the kernel reject the
    whole block. Scoped to *end-of-line* commas, which in a have-chain are always the stray
    separator (commas inside terms/types sit mid-line).

    The closing tactic is left intact on purpose. Composition is now the decomposer's own
    ``closer`` (see :meth:`PlanOption.active_closer`) and the kernel arbitrates whether it
    discharges the goal — so a transitivity/``▸``/``calc`` close is a *legitimate* proposal,
    not a tic to rewrite. A bad closer simply fails skeleton_check and triggers revision (and
    ultimately the identity-decomposition floor), never a silent rewrite to ``exact <last>``.

    A clean scaffold passes through unchanged.
    """
    if not scaffold:
        return scaffold
    return _TRAILING_COMMA_RE.sub("", scaffold)


def _scaffold_as_command(scaffold: str, goal_type: str) -> str:
    """Coerce a planner scaffold into a Lean command.

    Live planners often emit the body of a ``by`` proof (`have …; exact …`) rather than
    a top-level declaration. The verifier runs files, so that body must be wrapped in an
    ``example : goal := by`` command for skeleton/final checks. The scaffold is scrubbed of
    Lean-3 trailing-comma separators first (see :func:`_sanitize_scaffold`) so the same
    fix-up covers both the skeleton check and the final ``bank`` assembly.
    """
    s = _sanitize_scaffold((scaffold or "").strip())
    if not s:
        return s
    if _looks_like_declaration(s):
        return s
    return f"example : {goal_type} := by\n{_indent_tactic_body(s)}"


_LEAN_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_']*\b")
_LEAN_KEYWORDS = {
    "by", "have", "show", "exact", "intro", "intros", "fun", "from", "let", "in",
    "if", "then", "else", "match", "with", "forall", "Prop", "Type", "Sort",
}


def _bound_names_from_formal(formal: dict[str, Any] | None) -> set[str]:
    names: set[str] = set()
    if not formal:
        return names
    for item in formal.get("local_context") or []:
        for name in item.get("names") or []:
            names.add(str(name))
    return names


def _intro_names(scaffold: str) -> set[str]:
    names: set[str] = set()
    for line in (scaffold or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith(("intro ", "intros ")):
            continue
        parts = stripped.split()
        for tok in parts[1:]:
            tok = tok.strip("(),;")
            if _LEAN_IDENT_RE.fullmatch(tok):
                names.add(tok)
    return names


def _internally_bound_names(lean_type: str) -> set[str]:
    names: set[str] = set()
    for binder in re.findall(r"[\(\{\[]([^()\{\}\[\]]+:[^()\{\}\[\]]+)[\)\}\]]", lean_type or ""):
        before_colon = binder.split(":", 1)[0]
        names.update(_LEAN_IDENT_RE.findall(before_colon))
    for match in re.finditer(r"∀\s+([A-Za-z_][A-Za-z0-9_']*)\b", lean_type or ""):
        names.add(match.group(1))
    return names


def _subgoal_unbound_context_hits(
    subtasks: list[Subtask],
    scaffold: str,
    formal: dict[str, Any] | None,
) -> list[dict[str, str]]:
    # Formal theorem locals are now explicitly threaded into independent subgoal targets.
    # Locals introduced only by free-form scaffold text are still invalid for independent
    # proving because there is no formal binder set the native target can quantify over.
    context_names = _intro_names(scaffold) - _bound_names_from_formal(formal)
    if not context_names:
        return []
    hits: list[dict[str, str]] = []
    for sub in subtasks:
        lean_type = sub.lean_type or ""
        internal = _internally_bound_names(lean_type)
        tokens = {
            tok for tok in _LEAN_IDENT_RE.findall(lean_type)
            if tok not in _LEAN_KEYWORDS
        }
        for name in sorted((tokens & context_names) - internal):
            hits.append({"id": sub.id, "identifier": name, "lean_type": lean_type})
    return hits


def _synthesized_candidate(ctx: NativeNodeCtx, sg_id: str) -> Optional[dict[str, Any]]:
    """The Path-B candidate the synthesizer staged for sub-goal ``sg_id``.

    The ``lemma_synthesizer`` agent persists its candidate via ``context_put`` under the
    *un-namespaced* key ``candidate_b`` — that is its documented tool contract (see the
    agent goal / synthesize node instruction) and the agent has no reliable handle on the
    sub-goal id to namespace it. The rest of the prover blackboard, however, is sub-goal
    namespaced (``candidate_b:<sg>``, the form the ``eval`` harness writes). Prefer the
    namespaced key when present; fall back to the plain one so the live synthesize→verify
    hand-off actually finds the candidate on the shipped single-sub-goal ``lean-prove``
    workflow. Without this fallback Path B is silently empty and every live proof "fails".

    The value is also normalized to a dict: the ``context_put`` tool persists the agent's
    candidate as a JSON *string*, whereas the native handlers (and the eval harness) store
    it as a dict. Parse a string form here so callers can always do ``cand["source"]``.
    """
    raw = ctx.get(_bb_key("candidate_b", sg_id)) or ctx.get("candidate_b")
    if isinstance(raw, str):
        import json

        try:
            # strict=False permits raw control characters (literal newlines/tabs) inside
            # string values. The synthesizer hand-writes this JSON and routinely embeds an
            # un-escaped newline in the multi-line Lean ``source`` — strict parsing rejects
            # that ("Invalid control character"), silently dropping an otherwise-valid Path
            # B candidate so verify finds nothing to check and the run fails.
            raw = json.loads(raw, strict=False)
        except (ValueError, TypeError):
            logger.warning("candidate_b is a non-JSON string; ignoring Path B candidate")
            return None
    if not isinstance(raw, dict):
        return None
    # Scrub dialect tics from the synthesized source. Core strips imports; Mathlib keeps
    # them so profile=mathlib can prove against the sidecar project.
    if raw.get("source"):
        raw["source"] = _sanitize_lean_source(
            raw["source"],
            profile=ctx.get("lean_profile", settings.lean_profile),
        )
        concepts = ctx.get(_bb_key("concept_context", sg_id)) or []
        if concepts:
            preamble = "\n\n".join(_concept_preamble(c) for c in concepts if _concept_preamble(c))
            if preamble:
                raw["source"] = f"{preamble}\n\n{raw['source']}"
    return raw


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


def _normalize_proof_rhs(proof_term: str) -> str:
    """Coerce a banked ``proof_term`` into a valid right-hand side for ``:= <here>``.

    Banked proof terms are heterogeneous: seeds store *term* proofs (``fun n => Nat...``,
    ``Nat.add_comm``, ``rfl``); live runs store *tactic blocks* (``\\n  intro n\\n  rfl``)
    and ``by``-prefixed blocks (``by\\n  rfl``). Pasted verbatim into ``have h : T := …``,
    a multi-line tactic block breaks on indentation. We:

      * keep term proofs (``fun``/dotted-name/parenthesized/bare ident) as-is;
      * collapse any tactic block — ``by``-prefixed or bare — to a single-line
        ``by t1; t2; …`` so column-sensitive parsing can't bite inside the ``have``.

    A degraded/empty term falls back to ``by exact?`` (the kernel rejects it cleanly, so
    the candidate simply fails to verify rather than crashing the controller).
    """
    pt = (proof_term or "").strip()
    if not pt:
        return "by exact?"
    if pt.startswith("by"):
        tacs = [t.strip() for t in re.split(r"[\n;]", pt[2:]) if t.strip()]
        return "by " + "; ".join(tacs) if tacs else "by exact?"
    # Term proofs: lambda, dotted/qualified name, application, or parenthesized term.
    if pt.startswith(("fun ", "@", "(")) or ("\n" not in pt and "=>" not in pt):
        return pt
    # Otherwise it's a bare (un-``by``-ed) tactic block — collapse to one line.
    tacs = [t.strip() for t in re.split(r"[\n;]", pt) if t.strip()]
    return "by " + "; ".join(tacs) if tacs else "by exact?"


def _candidate_from_lemma(goal_type: str, lemma: dict[str, Any]) -> dict[str, Any]:
    """Assemble a verifiable Path-A candidate from a retrieved lemma payload.

    Produces a self-contained ``full``-mode source that discharges ``goal_type`` by
    *applying* the banked lemma — the construction the applicability gate already proved
    works (its probe unifies via ``exact h``/``apply h``). The earlier form pasted the
    lemma's ``proof_term`` verbatim into ``example : {goal_type} := {proof_term}``, which
    is ill-typed whenever ``goal_type`` is an *instance* of a ∀-lemma (e.g.
    ``example : 0 + 0 = 0 := fun n => Nat.zero_add n``). Instead we re-prove the lemma as
    a local ``h`` of its own type and close the goal through it::

        example : {goal_type} := by
          have h : {lemma_type} := {proof}
          first | exact h | apply h | simpa using h

    ``exact h`` closes a goal that *is* the lemma's statement; ``apply h``/``simpa`` let
    the kernel instantiate the lemma's binders for an instance goal. ``lemma_type`` prefers
    the payload's ``lean_type``, degrading to the type extracted from ``statement`` (which
    may be a bare type or a full ``theorem … := …`` decl). Verified live across seed
    (term-proof) and banked (tactic-block) payloads for both exact-∀ and instance goals.
    """
    statement = lemma.get("statement", "") or f"example : {goal_type}"
    proof_term = lemma.get("proof_term", "") or "by exact?"
    lemma_type = lemma.get("lean_type") or _lemma_type(statement)
    proof = _normalize_proof_rhs(proof_term)
    source = (
        f"example : {goal_type} := by\n"
        f"  have h : {lemma_type} := {proof}\n"
        f"  first | exact h | apply h | simpa using h"
    )
    return {
        "id": lemma.get("id"),
        "source": source,
        "statement": statement,
        "proof_term": proof_term,
        "origin": lemma.get("origin") or "skill_library",
        "source_collection": lemma.get("source_collection"),
        "lean_type": lemma.get("lean_type") or goal_type,
        # Provenance for the reuse-depth metric: a single-lemma candidate composes exactly
        # one banked lemma. Multi-lemma candidates (depth>=2) carry several ids here.
        "lemmas_used": [lemma["id"]] if lemma.get("id") else [],
        "times_retrieved": int(lemma.get("times_retrieved") or 0),
        "times_won": int(lemma.get("times_won") or 0),
    }


def _compose_multi_source(goal_type: str, lemmas: list[dict[str, Any]]) -> str:
    """Build a ``full``-mode source that discharges ``goal_type`` by *composing* lemmas.

    Each banked lemma becomes a local ``have hᵢ`` of its own type; the goal is then closed
    by a banked-only closer (no ambient Mathlib normalizers, so a verified proof's depth is
    honestly "how many banked lemmas it took"). The ``first | …`` ladder tries, in order:
    pure ``simp only`` over the haves, the same after ``intros`` (∀-goals), and ``rw`` chains
    — enough to cover rewrite- and simp-shaped compositions without importing AC power lemmas.
    """
    haves = []
    names = []
    for i, lem in enumerate(lemmas):
        name = f"h{i}"
        names.append(name)
        ltype = lem.get("lean_type") or _lemma_type(lem.get("statement", "") or f"example : {goal_type}")
        proof = _normalize_proof_rhs(lem.get("proof_term", "") or "by exact?")
        haves.append(f"  have {name} : {ltype} := {proof}")
    hset = ", ".join(names)
    closers = (
        f"  first\n"
        f"    | simp only [{hset}]\n"
        f"    | (intros; simp only [{hset}])\n"
        f"    | rw [{hset}]\n"
        f"    | (intros; rw [{hset}])"
    )
    return f"example : {goal_type} := by\n" + "\n".join(haves) + "\n" + closers


def _multi_candidate_from_lemmas(goal_type: str, lemmas: list[dict[str, Any]]) -> dict[str, Any]:
    """A depth>=2 Path-A candidate that *composes* several banked lemmas (build-plan depth axis).

    The single-lemma :func:`_candidate_from_lemma` ceilings reuse at depth 1 by construction;
    this is the form that makes composition reachable. ``lemmas_used`` starts as the full
    offered set, but the verify controller ablates it down to the lemmas actually *needed*
    (single-drop necessity) before crediting :func:`lemma_compare.reuse_depth` — so depth is a
    verified lower bound, never the (gameable) count of lemmas handed to ``simp``.
    """
    return {
        "id": None,  # a composition is not itself a banked lemma
        "source": _compose_multi_source(goal_type, lemmas),
        "statement": f"example : {goal_type}",
        "proof_term": "",  # filled from the verified source at bank time if it wins
        "origin": "compose",
        "source_collection": None,
        "lean_type": goal_type,
        "lemmas_used": [lem["id"] for lem in lemmas if lem.get("id")],
        "multi": True,
        # The full lemma payloads, kept so verify can re-compose subsets during ablation.
        "compose_lemmas": lemmas,
        "times_retrieved": 0,
        "times_won": 0,
    }


# ---------------------------------------------------------------------------
# repair proposal — the one generative sub-step (§1a)
# ---------------------------------------------------------------------------


async def propose_repair(
    goal: str,
    candidate_source: str,
    errors: list[str],
    weak: bool = False,
    profile: str | None = None,
) -> str:
    """Ask the ``repair`` agent for ONE revised candidate that closes ``goal``.

    When ``weak`` (the weak-prover headline regime), the prompt forbids strong closers so
    repair aims for an *eligible* proof — but the gate is still enforced mechanically by
    :func:`_uses_only_weak_tactics`, never by trusting the model to comply.

    A scoped, structured LLM call (à la ``runner._summarize_context``) that reads its
    model and persona from the ``repair`` agent record — so the model/prompt stay
    operator-configurable in JSON without the weight of a CrewAI crew or a back-edge in
    the DAG. The verify controller owns the loop; this makes exactly one proposal.

    Returns the revised Lean source (falls back to the unchanged candidate on any error,
    so a flaky proxy degrades to "re-check the same source" rather than crashing the
    controller). The kernel still judges the result on the very next line — a proposal
    can never fake a pass.

    The §1a upgrade path: swap this body for a fully autonomous repair agent that owns
    its own inner loop (calling ``lean_verify`` as a tool) — same seam, only the body
    changes.
    """
    from hyperion.agents.registry import load_agent

    try:
        record = load_agent("repair")
    except Exception as exc:  # missing record must not crash the controller
        logger.warning("propose_repair: could not load 'repair' agent (%s)", exc)
        return candidate_source

    system = f"{record.role}\n\n{record.goal}\n\n{record.backstory}"
    weak_clause = (
        f"\n\nCONSTRAINT: do NOT use any of these tactics — "
        f"{', '.join(settings.prover_path_b_banned_tactics)}, or bare `simp`. "
        f"Use only primitive steps (rfl, exact, apply, intro, rw, `simp only [...]`)."
        if weak else ""
    )
    user = (
        f"Verifier profile: {(profile or settings.lean_profile or 'core').strip().lower()}\n\n"
        f"Goal type:\n{goal}\n\n"
        f"Candidate proof that FAILED to type-check:\n{candidate_source}\n\n"
        f"Kernel diagnostics:\n" + "\n".join(errors or ["(no diagnostics)"]) + weak_clause + "\n\n"
        "Return ONLY the full revised Lean 4 source."
    )
    try:
        from openai import OpenAI

        client = OpenAI(base_url=settings.litellm_base_url, api_key=settings.llm_api_key)
        resp = client.chat.completions.create(
            model=record.model_alias,
            temperature=record.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or candidate_source
    except Exception as exc:
        logger.warning("propose_repair: LLM call failed (%s) — re-checking unchanged", exc)
        return candidate_source


# ---------------------------------------------------------------------------
# abstraction proposal — the one generative sub-step of `abstract` (§Phase 5 (a))
# ---------------------------------------------------------------------------


async def propose_abstraction(
    statement: str, proof_term: str, lean_type: str
) -> list[dict[str, Any]]:
    """Ask the ``abstractor`` agent for lifted lemmas, ORDERED most-general first.

    The :func:`propose_repair` twin (build plan §Phase 5 decision a): a scoped, structured
    LLM call that reads its model/persona from the ``abstractor`` agent record, so the
    generalization model/prompt stay operator-configurable in JSON without a CrewAI crew or
    a DAG back-edge. The :func:`abstract_handler` controller owns the re-verify + fallback
    loop; this only proposes.

    Returns a list of candidate dicts (``{source, statement, proof_term, lean_type}``)
    ordered from boldest generalization to most conservative — the controller keeps the
    first that still type-checks (the most-general form that re-verifies). Returns ``[]``
    on any error so the controller falls back to the concrete verified lemma; the kernel
    re-verifies every proposal, so an over-abstraction can never sneak into the bank.
    """
    from hyperion.agents.registry import load_agent

    try:
        record = load_agent("abstractor")
    except Exception as exc:  # missing record must not crash the controller
        logger.warning("propose_abstraction: could not load 'abstractor' agent (%s)", exc)
        return []

    system = f"{record.role}\n\n{record.goal}\n\n{record.backstory}"
    user = (
        f"Goal type the lemma was derived for:\n{lean_type}\n\n"
        f"Verified lemma statement:\n{statement}\n\n"
        f"Proof term that closed it:\n{proof_term}\n\n"
        "Return ONLY a JSON array of candidate lemmas, MOST GENERAL FIRST, each "
        '{"source", "statement", "proof_term", "lean_type"}.'
    )
    try:
        import json

        from openai import OpenAI

        client = OpenAI(base_url=settings.litellm_base_url, api_key=settings.llm_api_key)
        resp = client.chat.completions.create(
            model=record.model_alias,
            temperature=record.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        data = json.loads(text)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and d.get("source")]
        return []
    except Exception as exc:
        logger.warning("propose_abstraction: LLM call failed (%s) — no proposals", exc)
        return []


# ---------------------------------------------------------------------------
# retrieve — Path A
# ---------------------------------------------------------------------------


async def retrieve_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Path A: source applicable banked lemmas for the sub-goal and stage candidates.

    Writes ``candidate_a:<sg>`` (the top applier, or None when the bank yields nothing)
    and ``candidates_a:<sg>`` (the full ranked list, so ``verify`` can advance to a
    next-best match on a both-fail) to the blackboard. Fail-soft throughout: an empty /
    degraded bank simply stages no Path-A candidate (Path B can still close the goal).
    """
    sg_id = _subgoal_id(ctx)
    goal_type = _goal_type(ctx, sg_id)
    lemmas = retrieve_applicable_lemmas(goal_type)
    concepts = concept_bank.retrieve_concepts(goal_type)
    ctx.put(_bb_key("concept_context", sg_id), concepts)
    candidates = [_candidate_from_lemma(goal_type, lem) for lem in lemmas]
    # Depth axis: also stage ONE composition of the top-k banked lemmas, appended AFTER the
    # single-lemma candidates. verify tries singles first, so a goal that any one lemma closes
    # stays honestly depth-1; the composition is only reached when no single lemma suffices —
    # which is exactly when genuine multi-lemma reuse (depth>=2) is happening.
    top_k = [lem for lem in lemmas if lem.get("id")][: settings.compose_top_k]
    if len(top_k) >= 2:
        candidates.append(_multi_candidate_from_lemmas(goal_type, top_k))
    top = candidates[0] if candidates else None
    ctx.put(_bb_key("candidate_a", sg_id), top)
    ctx.put(_bb_key("candidates_a", sg_id), candidates)
    ctx.progress(
        f"[retrieve] {sg_id}: {len(lemmas)} applicable lemma(s), {len(concepts)} concept(s)"
        f"{f' (+1 composition of {len(top_k)})' if len(top_k) >= 2 else ''}"
    )
    return {
        "handler": "retrieve",
        "subgoal": sg_id,
        "n_candidates": len(candidates),
        "n_concepts": len(concepts),
        "has_candidate": top is not None,
    }


# ---------------------------------------------------------------------------
# skeleton_check — P1 scaffold type-check (skeleton mode)
# ---------------------------------------------------------------------------


async def skeleton_check_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Type-check the decomposer's scaffold in ``skeleton`` mode (``sorry`` permitted).

    Confirms the have-chain composes to the target before any sub-goal sourcing. A verifier
    outage is still distinct from a kernel rejection, but it is terminal for this stage:
    without a skeleton verdict, downstream retrieve/synthesize work has no sound scaffold
    contract to rely on.
    """
    plan = parse_plan(ctx.task_id)
    subtasks = plan.active_subtasks()
    scaffold = _native_scaffold_from_subtasks(ctx, subtasks, plan.active_closer()) or (plan.scaffold or "")
    if not scaffold:
        ctx.put("skeleton_ok", False)
        ctx.put("skeleton_errors", ["no scaffold in plan"])
        return {"handler": "skeleton_check", "ok": False, "errors": ["no scaffold in plan"]}
    formal = _formal_context(ctx)
    unbound_hits = _subgoal_unbound_context_hits(subtasks, scaffold, formal)
    ctx.put("subgoal_unbound_context", unbound_hits)
    for hit in unbound_hits:
        ctx.put(_bb_key("subgoal_unbound_context", hit["id"]), hit)
    if unbound_hits:
        errors = [
            f"subgoal_unbound_context: {hit['id']} references unbound {hit['identifier']}"
            for hit in unbound_hits
        ]
        ctx.put("skeleton_ok", False)
        ctx.put("skeleton_errors", errors)
        return {
            "handler": "skeleton_check",
            "ok": False,
            "error_code": "subgoal_unbound_context",
            "errors": errors,
            "subgoal_unbound_context": unbound_hits,
        }
    res = verify_lean(
        _scaffold_target_command(ctx, scaffold),
        mode="skeleton",
        profile=ctx.get("lean_profile", settings.lean_profile),
    )
    if not res["infra_ok"]:
        error_code = (
            "verifier_timeout"
            if any("timed out" in str(e).lower() for e in res["errors"])
            else "verifier_unavailable"
        )
        ctx.put("skeleton_ok", None)
        ctx.put("skeleton_errors", res["errors"])
        return {
            "handler": "skeleton_check",
            "ok": False,
            "infra_ok": False,
            "error_code": error_code,
            "errors": res["errors"],
        }
    ctx.put("skeleton_ok", res["ok"])
    ctx.put("skeleton_errors", res["errors"])
    return {"handler": "skeleton_check", "ok": res["ok"], "errors": res["errors"]}


# ---------------------------------------------------------------------------
# verify — the native controller (§1a)
# ---------------------------------------------------------------------------


def _full_verdict(source: str, *, profile: str | None = None) -> tuple[bool, list[str]]:
    """Run the kernel in ``full`` mode; return (closed, errors).

    ``closed`` is True ONLY on a real ``ok=True`` verdict. An infra-down result
    (``infra_ok=False``) is NOT a pass — the verdict is load-bearing ground truth, so we
    never discharge a goal on a verifier we could not reach.
    """
    res = verify_lean(source, mode="full", profile=profile or settings.lean_profile)
    if not res["infra_ok"]:
        return False, res["errors"]
    return bool(res["ok"]), res["errors"]


def _uses_only_weak_tactics(source: str) -> bool:
    """Whether ``source`` avoids every strong closer — the deterministic weak-prover gate.

    The value claim of the snowball ("retrieval is doing real work") is only honest if Path B
    can't trivially front-run the bank with a decision procedure. We enforce that the way the
    kernel enforces proofs — mechanically, not by trusting the LLM to obey a prompt: a proof
    that contains ``omega``/``ring``/``decide``/… (config ``prover_path_b_banned_tactics``)
    is ineligible to *win*. Full ``simp`` is strong; ``simp only [...]`` (explicit lemmas) is
    allowed, since that is itself reuse of named facts.
    """
    for t in settings.prover_path_b_banned_tactics:
        if re.search(rf"\b{re.escape(t)}\b", source):
            return False
    if re.search(r"\bsimp\b(?!\s*only)", source):
        return False
    return True


def _necessary_lemma_ids(goal_type: str, lemmas: list[dict[str, Any]]) -> list[str]:
    """Single-drop ablation: which banked lemmas the composition actually *needed*.

    A lemma is necessary iff removing it (and re-running the kernel on the remaining set)
    breaks the proof. The credited :func:`lemma_compare.reuse_depth` is the count of such
    lemmas — a verified lower bound on genuine composition that can't be inflated by handing
    extra lemmas to ``simp``. Conservative on interactions (two lemmas redundant alone but
    needed together would both read as droppable ⇒ we *under*-credit, never over-credit).
    """
    if len(lemmas) <= 1:
        return [lem["id"] for lem in lemmas if lem.get("id")]
    necessary: list[str] = []
    for i, lem in enumerate(lemmas):
        if not lem.get("id"):
            continue
        without = lemmas[:i] + lemmas[i + 1 :]
        still_closes, _ = _full_verdict(_compose_multi_source(goal_type, without))
        if not still_closes:
            necessary.append(lem["id"])
    return necessary


@dataclass
class ProofOutcome:
    """Result of proving one proposition via the kernel + bounded repair loop.

    The reusable proving kernel's return value (see :func:`prove_proposition`). It carries
    both verdicts the weak-prover regime needs:

    Attributes:
        closed: True iff some attempt closed the goal at *full strength* (any tactics).
        source: The full-strength closing source — the counterfactual ``b_strong`` (could
            a strong prover close it?), or None.
        weak_source: The closing source that ALSO passes :func:`_uses_only_weak_tactics`
            — the proof eligible to *win* under the snowball value claim. Equals ``source``
            when ``weak=False``; None when only a strong proof closed under the weak gate.
        proof_term: The bare proof body of the winning source (``weak_source or source``).
        repair_iters: Repair rounds performed (each is one ``repair`` agent call).
        verdicts: Per-attempt ``{"path": "seed"|"repair", "ok": bool}`` trace.
        errors: Diagnostics from the last attempt (empty when the last attempt closed).
        axioms / axioms_clean: The soundness contract result on the winning source when a
            ``decl`` was supplied; otherwise ``[]`` / None (probe skipped). ``axioms_clean``
            is the bridge/lemma/ablation acceptance gate (Phases 2-4).
    """

    closed: bool
    source: Optional[str]
    weak_source: Optional[str]
    proof_term: Optional[str]
    repair_iters: int
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    axioms: list[str] = field(default_factory=list)
    axioms_clean: Optional[bool] = None

    @property
    def won(self) -> bool:
        """True iff a *win-eligible* (weak-gated) proof closed the goal."""
        return self.weak_source is not None


async def prove_proposition(
    goal_type: str,
    seed_source: str,
    *,
    weak: bool = False,
    max_repair: Optional[int] = None,
    decl: Optional[str] = None,
    strict_soundness: Optional[bool] = None,
    profile: str | None = None,
) -> ProofOutcome:
    """Prove ``goal_type`` from ``seed_source`` via the kernel + bounded repair loop.

    The proving kernel extracted from ``verify_handler`` Path B, made callable on any goal
    so bridge lemmas, planned lemmas, and the same-budget ablation re-proofs (Phases 2-4)
    all share one path. Verify the seed; on failure (or weak-gate ineligibility) delegate
    ONE repair proposal per round to the ``repair`` agent (:func:`propose_repair`) and
    re-check it with the kernel, up to ``max_repair`` rounds (default
    ``settings.cap_repair_iters``).

    The verdict is ALWAYS the kernel's — a repair proposal can be arbitrarily creative and
    still cannot fake a pass. Honors the weak-prover gate exactly as the verify node does:
    ``source`` is the full-strength close (the counterfactual), ``weak_source`` additionally
    passes the weak-tactic gate.

    When ``decl`` is given, the soundness contract (:func:`soundness.soundness_ok`) runs on
    the winning source and fills ``axioms``/``axioms_clean`` — the kernel-grounded acceptance
    gate for bridges/lemmas. With no ``decl`` (the verify node's anonymous ``example``
    sources) the axiom probe is skipped; full-mode verification already forbids ``sorry``.

    Args:
        goal_type: The proposition being proved (passed to the repair agent as context).
        seed_source: The initial candidate source to verify (e.g. a synthesized proof).
        weak: Enforce the weak-prover gate on win-eligibility (snowball value claim).
        max_repair: Repair-round cap (defaults to ``settings.cap_repair_iters``).
        decl: Declaration name to run the soundness contract against; None skips the probe.
        strict_soundness: Override ``settings.prover_soundness_strict`` for the probe.

    Returns:
        A :class:`ProofOutcome`.
    """
    cap = settings.cap_repair_iters if max_repair is None else max_repair
    verdicts: list[dict[str, Any]] = []
    strong_source: Optional[str] = None
    weak_source: Optional[str] = None

    closed, errors = _full_verdict(seed_source, profile=profile)
    verdicts.append({"path": "seed", "ok": closed})
    if closed:
        strong_source = seed_source
        if not weak or _uses_only_weak_tactics(seed_source):
            weak_source = seed_source

    cur_source = seed_source
    repair_iters = 0
    # Repair while we lack a win-eligible proof — covers both "did not close" and "closed
    # but gated out by the weak prover". A strong close is kept as the counterfactual.
    if weak_source is None:
        for _ in range(cap):
            repair_iters += 1
            cur_source = _sanitize_lean_source(
                await propose_repair(goal_type, cur_source, errors, weak=weak, profile=profile),
                profile=profile,
            )
            closed, errors = _full_verdict(cur_source, profile=profile)
            verdicts.append({"path": "repair", "ok": closed})
            if not closed:
                continue
            if strong_source is None:
                strong_source = cur_source
            if not weak or _uses_only_weak_tactics(cur_source):
                weak_source = cur_source
                break

    win = weak_source or strong_source
    proof_term = _bare_proof_term({"source": win}) if win else None

    axioms: list[str] = []
    axioms_clean: Optional[bool] = None
    if win is not None and decl is not None:
        strict = (
            settings.prover_soundness_strict if strict_soundness is None else strict_soundness
        )
        sres = soundness_ok(win, decl, strict=strict, profile=profile)
        axioms = sres.axioms
        axioms_clean = sres.ok

    return ProofOutcome(
        closed=strong_source is not None,
        source=strong_source,
        weak_source=weak_source,
        proof_term=proof_term,
        repair_iters=repair_iters,
        verdicts=verdicts,
        errors=errors,
        axioms=axioms,
        axioms_clean=axioms_clean,
    )


# ---------------------------------------------------------------------------
# Deterministic closer battery — the Path-B seed (regime-gated win-eligibility)
# ---------------------------------------------------------------------------

# Standard one-shot closers, cheapest/most-primitive first. Win-eligibility is NOT encoded
# here — it is decided downstream by the SAME ``_uses_only_weak_tactics`` gate Path B uses
# (one enforcement path, never a hand-labelled flag). Consequence (the thesis contract):
#   • strong regime: every closer may WIN  → external/SOTA-comparison number.
#   • weak regime  : only primitives (``rfl`` / structural ``intros`` / narrow ``simp only``)
#     may win; the strong closers are still TRIED and recorded as the ``b_strong``
#     counterfactual but are BANNED FROM WINNING — the control condition that forces
#     composition + definition-mediation to carry the non-trivial goals.
_BATTERY_CLOSERS = (
    "rfl",            # primitive: kernel computation / definitional
    "simp only []",   # primitive: narrow simp (weak-eligible)
    "decide", "norm_num", "ring", "ring_nf", "omega",
    "simp", "linarith", "nlinarith", "positivity", "aesop",
)


def _battery_source(goal_type: str, tactic: str) -> str:
    """A bare ``example`` for one battery tactic — no ``import`` header.

    Matches the convention of every other source builder here (e.g.
    :func:`_compose_multi_source`): the Mathlib environment comes from ``profile='mathlib'``
    (the sidecar strips imports against its hot ``import Mathlib`` BASE_ENV), and keeping the
    source a bare declaration lets :func:`_bare_proof_term` recover ``by <tactic>`` as the
    hole-fitting proof term (an ``import`` first line would defeat ``_looks_like_declaration``).
    """
    return f"example : {goal_type} := by {tactic}"


def _closer_battery_tactics(goal_type: str) -> list[str]:
    """The battery tactics for ``goal_type``, primitives first.

    ∀/→ goals also get a structural-``intros`` prefix so a closer fires after binders are
    introduced. ``intros`` is a no-op (not an error) on a non-arrow goal — verified live — so
    the prefixed variant is always safe to try.
    """
    needs_intro = goal_type.strip().startswith("∀") or "→" in goal_type
    out: list[str] = []
    for c in _BATTERY_CLOSERS:
        out.append(c)
        if needs_intro:
            out.append(f"intros; {c}")
    return out


def _run_closer_battery(
    goal_type: str, *, weak: bool, profile: str | None
) -> tuple[Optional[str], Optional[str], list[dict[str, Any]]]:
    """Try the deterministic battery against the kernel; return (weak_source, strong_source,
    verdicts).

    ``strong_source`` is the first attempt that closes at full strength (the ``b_strong``
    counterfactual / the strong-regime win); ``weak_source`` is the first close that ALSO
    passes the :func:`_uses_only_weak_tactics` gate. Strong regime (``weak=False``) ⇒
    ``weak_source == strong_source``. The kernel is the only judge — a tactic that does not
    close is skipped; nothing is faked.
    """
    strong_source: Optional[str] = None
    weak_source: Optional[str] = None
    verdicts: list[dict[str, Any]] = []
    for tac in _closer_battery_tactics(goal_type):
        src = _battery_source(goal_type, tac)
        closed, _errors = _full_verdict(src, profile=profile)
        verdicts.append({"tactic": tac, "ok": closed})
        if not closed:
            continue
        if strong_source is None:
            strong_source = src
        if not weak or _uses_only_weak_tactics(src):
            weak_source = src
            break
    return weak_source, strong_source, verdicts


def subgoal_battery_closes(ctx: NativeNodeCtx) -> bool:
    """Whether the closer battery yields a *win-eligible* proof for this sub-goal — the
    runner's "battery first" signal to skip the LLM synth node.

    Pure kernel, no LLM. Resolves the goal/profile/regime exactly as :func:`verify_handler`
    does and runs the SAME :func:`_run_closer_battery`, so a True here means verify will
    discharge the goal from the battery without ever needing the synthesized seed. (The
    battery is intentionally re-run in verify — both are cheap kernel calls — so verify stays
    the single authoritative owner of ``verified_b``/``b_strong``.)"""
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    profile = ctx.get("lean_profile", settings.lean_profile)
    weak_source, _strong, _verdicts = _run_closer_battery(
        goal, weak=settings.prover_weak_path_b, profile=profile
    )
    return weak_source is not None


def _battery_candidate(source: str, goal_type: str) -> dict[str, Any]:
    """Wrap a closing battery source as a Path-B candidate.

    ``origin='battery'`` lets the thesis read-out separate a trivially-closed goal from one
    carried by composition/retrieval; ``path='B'`` keeps the compare/bank wiring unchanged.
    """
    return {
        "source": source,
        "statement": f"example : {goal_type}",
        "proof_term": _bare_proof_term({"source": source}),
        "origin": "battery",
        "lean_type": goal_type,
        "path": "B",
    }


async def verify_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """The native controller: kernel verdict + deterministic routing + bounded repair.

    Routing (build plan §1a; RESEARCH/DEPLOY per §Phase 5 decision b):
      1. Try each Path-A candidate (top, then next-best) in ranked order — the first that
         closes is the verified Path-A lemma (``verified_a``).
      2. Path B (always in RESEARCH mode; in DEPLOY only when Path A did not verify):
         check the synthesized candidate; on failure run the repair loop — delegate ONE
         proposal per iteration to the ``repair`` agent (:func:`propose_repair`) and re-check
         it, up to ``settings.cap_repair_iters`` — yielding the verified Path-B lemma
         (``verified_b``).
      3. Nothing verifies ⇒ record a structured stall. The Phase-4 definition-synthesis
         branch may then try to birth a concept and write ``discharged:<sg>``; the final
         bank step remains the hard proof-completion gate.

    DEPLOY (``prover_research_mode`` False, default) is exploit-first: it short-circuits
    once Path A closes and never pays to verify Path B — the historical behavior. RESEARCH
    verifies BOTH so ``compare`` has a genuine A-vs-B contest and ``abstract`` can fire on a
    fresh Path-B lemma even when Path A also closed (anti-starvation, decision e).

    The verdict is ALWAYS the kernel's: an LLM repair can be arbitrarily creative and
    still cannot hallucinate a pass, because every proposal is checked on the next line.
    Writes ``verified_a``/``verified_b`` (the compare inputs), a provisional ``discharged``
    (exploit-first pick — its single-winner contract, finalized later by ``compare``), and
    the full routing trace to the blackboard for the thesis log.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    profile = ctx.get("lean_profile", settings.lean_profile)
    research = settings.prover_research_mode
    decision: dict[str, Any] = {
        "subgoal": sg_id, "winner_path": None, "a_attempts": 0,
        "repair_iters": 0, "verdicts": [], "mode": "research" if research else "deploy",
    }

    def _record(path: str, closed: bool) -> None:
        decision["verdicts"].append({"path": path, "ok": closed})

    verified_a: Optional[dict[str, Any]] = None
    verified_b: Optional[dict[str, Any]] = None

    # ---- Path A: top candidate, then next-best, in ranked order ----
    a_list: list[dict[str, Any]] = []
    top = ctx.get(_bb_key("candidate_a", sg_id))
    if top:
        a_list.append(top)
    for cand in ctx.get(_bb_key("candidates_a", sg_id), []) or []:
        if cand not in a_list:
            a_list.append(cand)
    for cand in a_list:
        decision["a_attempts"] += 1
        closed, _errors = _full_verdict(cand["source"], profile=profile)
        _record("A", closed)
        if not closed:
            continue
        if cand.get("multi"):
            # Composition closed — credit only the lemmas single-drop ablation proves
            # necessary, so reuse_depth is a verified count, not the number fed to simp.
            necessary = _necessary_lemma_ids(goal, cand.get("compose_lemmas", []))
            if not necessary:
                # Closed without genuinely needing any banked lemma (e.g. simp/rfl alone) —
                # not a reuse win. Don't let Path A claim it; fall through to Path B.
                _record("A-compose-spurious", False)
                continue
            verified_a = {**cand, "path": "A", "lemmas_used": necessary}
        else:
            verified_a = {**cand, "path": "A"}
        break

    # ---- Path B: synthesized candidate, then the bounded repair loop ----
    # RESEARCH: always verify B (the comparison is the experiment). DEPLOY: only when
    # Path A failed to close (exploit-first — don't pay for B once A has won).
    #
    # weak gate (build plan: snowball value claim): when ``prover_weak_path_b``, a proof is
    # only *eligible to win* (verified_b) if it uses no strong closer; the full-strength
    # verdict is logged regardless as the counterfactual ``b_strong`` (could a strong prover
    # have solved it). Repair is given a fair shot at an eligible (weak) proof.
    weak = settings.prover_weak_path_b
    b_strong: Optional[dict[str, Any]] = None
    if research or verified_a is None:
        # Path B (deterministic): the closer battery, FIRST — try standard one-shot closers
        # via the kernel before paying for the LLM seed + repair. Win-eligibility is
        # regime-gated by _run_closer_battery via the same weak gate; the full-strength close
        # is preserved as the ``b_strong`` counterfactual.
        bat_weak, bat_strong, bat_verdicts = _run_closer_battery(goal, weak=weak, profile=profile)
        for v in bat_verdicts:
            _record("Bdet", v["ok"])
        if bat_strong is not None:
            b_strong = _battery_candidate(bat_strong, goal)
        if bat_weak is not None:
            verified_b = _battery_candidate(bat_weak, goal)

        # Path B (synthesized): LLM seed + bounded repair — only when the battery produced no
        # win-eligible proof. The battery's b_strong counterfactual is kept either way.
        if verified_b is None:
            cb = _synthesized_candidate(ctx, sg_id)
            if cb:
                # The proving kernel (verify + bounded repair + weak gate) is shared with the
                # bridge/lemma/ablation paths via prove_proposition; the verify node owns only
                # the blackboard wiring around it.
                outcome = await prove_proposition(goal, cb["source"], weak=weak, profile=profile)
                for v in outcome.verdicts:
                    _record("B" if v["path"] == "seed" else "B-repair", v["ok"])
                decision["repair_iters"] += outcome.repair_iters

                def _as_candidate(src: str, cb=cb) -> dict[str, Any]:
                    # The synthesized seed keeps the synthesizer's metadata; a repaired source
                    # is wrapped with the BARE proof body so the bank/scaffold get a
                    # hole-fitting term, mirroring the prior inline construction.
                    if src == cb["source"]:
                        return {**cb, "path": "B"}
                    return {
                        "source": src,
                        "statement": cb.get("statement", ""),
                        "proof_term": _bare_proof_term({"source": src}),
                        "origin": "repair",
                        "lean_type": cb.get("lean_type") or goal,
                        "path": "B",
                    }

                if outcome.source is not None and b_strong is None:
                    b_strong = _as_candidate(outcome.source)
                if outcome.weak_source is not None:
                    verified_b = _as_candidate(outcome.weak_source)

    # b_strong closed at full strength but no eligible (weak) proof won ⇒ the gate is what
    # forces Path A to carry the goal — the load-bearing signal of the weak-prover headline.
    decision["b_strong_closed"] = b_strong is not None
    decision["b_gated_out"] = b_strong is not None and verified_b is None

    # verified_a/verified_b are the compare inputs (anti-starvation reads verified_b);
    # verified_b_strong is the full-strength counterfactual for the dual thesis read-out.
    ctx.put(_bb_key("verified_a", sg_id), verified_a)
    ctx.put(_bb_key("verified_b", sg_id), verified_b)
    ctx.put(_bb_key("verified_b_strong", sg_id), b_strong)

    # Provisional winner — exploit-first prefers A; compare finalizes when both verified.
    winner: Optional[dict[str, Any]] = verified_a or verified_b
    if winner is not None:
        decision["winner_path"] = winner["path"]

    ctx.put(_bb_key("verify_decision", sg_id), decision)
    if winner is None:
        # Clean stall: record the trace and let the Phase-4 escalation branch try to
        # discharge the goal. ``bank`` raises later if no branch produced a proof.
        ctx.put(_bb_key("discharged", sg_id), None)
        ctx.put(_bb_key("stall_errors", sg_id), [
            f"no candidate closed after {decision['a_attempts']} Path-A attempt(s) "
            f"and {decision['repair_iters']} repair iteration(s)"
        ])
        ctx.progress(f"[verify] {sg_id}: no path closed the goal (gave up)")
        return {
            "handler": "verify",
            "subgoal": sg_id,
            "ok": False,
            "winner_path": None,
            "stalled": True,
            "decision": decision,
        }

    winner["lean_type"] = winner.get("lean_type") or goal
    ctx.put(_bb_key("discharged", sg_id), winner)
    ctx.progress(f"[verify] {sg_id}: discharged via Path {winner['path']}")
    return {
        "handler": "verify",
        "subgoal": sg_id,
        "ok": True,
        "winner_path": winner["path"],
        "decision": decision,
    }


async def escalation_gate_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Route a normal proof stall into definition synthesis.

    The workflow engine is a pure DAG, not a conditional graph. This handler therefore
    writes the branch predicate to the blackboard and the downstream definition-synthesis
    nodes no-op when it is false. It also normalizes the context fields that the concept
    proposer reads.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    profile = ctx.get("lean_profile", settings.lean_profile)
    decision = ctx.get(_bb_key("verify_decision", sg_id)) or {}
    discharged = ctx.get(_bb_key("discharged", sg_id))
    stalled = discharged is None and decision.get("winner_path") is None
    if stalled:
        ctx.put(_bb_key("escalated", sg_id), True)
        ctx.put(_bb_key("stall_errors", sg_id), ctx.get(_bb_key("stall_errors", sg_id)) or [
            f"normal prover stalled on {sg_id}"
        ])
        ctx.put(_bb_key("informal_proof", sg_id), ctx.get(_bb_key("informal_proof", sg_id)) or ctx.request)
        ctx.put(_bb_key("lemma_plan", sg_id), ctx.get(_bb_key("lemma_plan", sg_id)) or "")
        ctx.put(_bb_key("formalized_lemmas", sg_id), ctx.get(_bb_key("formalized_lemmas", sg_id)) or [])
        ctx.put(_bb_key("parent_name", sg_id), ctx.get(_bb_key("parent_name", sg_id)) or "target")
    else:
        ctx.put(_bb_key("escalated", sg_id), False)
    ctx.progress(
        f"[escalation_gate] {sg_id}: "
        + ("stalled; routing to definition synthesis" if stalled else "normal proof discharged; skipped")
    )
    return {
        "handler": "escalation_gate",
        "subgoal": sg_id,
        "ok": True,
        "escalated": bool(stalled),
        "goal": goal,
    }


# ---------------------------------------------------------------------------
# compare — finalize the winner + write the thesis triple log (§Phase 5 (c)/(d))
# ---------------------------------------------------------------------------


async def compare_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Pick the preferred verified candidate and log the ``(retrieved, synthesized,
    winner)`` triple — the experiment's core measurement (build plan §Phase 5 (c)/(d)).

    Reads the considered candidates (``candidate_a``/``candidate_b``) and what actually
    verified (``verified_a``/``verified_b``, written by :func:`verify_handler`). Delegates
    the choice to the pure, deterministic :func:`lemma_compare.choose_winner` (more general
    / shorter / reuse-first), finalizes ``discharged:<sg>`` to that winner (carrying its
    ``generality_score`` for the bank), and writes the fixed :class:`lemma_compare.TripleLog`
    to ``triple_log:<sg>`` for Post-work's thesis-curve harness. Pure logic lives in
    ``lemma_compare``; this handler is just the blackboard plumbing around it.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    retrieved = ctx.get(_bb_key("candidate_a", sg_id))
    synthesized = _synthesized_candidate(ctx, sg_id)
    verified_a = ctx.get(_bb_key("verified_a", sg_id))
    verified_b = ctx.get(_bb_key("verified_b", sg_id))
    verified_b_strong = ctx.get(_bb_key("verified_b_strong", sg_id))

    winner = choose_winner(verified_a, verified_b)
    if winner is not None:
        winner["lean_type"] = winner.get("lean_type") or goal
        winner["generality_score"] = generality_score(winner)
        winner["times_won"] = lemma_bank.bump_times_won(winner)
        ctx.put(_bb_key("discharged", sg_id), winner)  # finalize the verify provisional

    mode = "research" if settings.prover_research_mode else "deploy"
    triple = build_triple(
        subgoal=sg_id, goal_type=goal,
        retrieved=retrieved, synthesized=synthesized,
        verified_a=verified_a, verified_b=verified_b,
        verified_b_strong=verified_b_strong,
        winner=winner, mode=mode,
    )
    ctx.put(_bb_key("triple_log", sg_id), triple)
    ctx.progress(
        f"[compare] {sg_id}: winner Path {triple['winner_path']} "
        f"(compared={triple['compared']})"
    )
    return {
        "handler": "compare",
        "subgoal": sg_id,
        "winner_path": triple["winner_path"],
        "compared": triple["compared"],
        "scores": triple["scores"],
    }


# ---------------------------------------------------------------------------
# abstract — the anti-unification abstractor (§Phase 5 (a)/(e); baseline §5/§6.6)
# ---------------------------------------------------------------------------


async def abstract_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Generalize a fresh verified Path-B lemma, re-verify, keep the most-general form.

    The novel module (build plan §Phase 5), built as a native CONTROLLER mirroring
    verify/repair: the deterministic re-verify + most-general-that-type-checks selection +
    over-abstraction rejection/fallback live here; only the generative lift is delegated to
    the ``abstractor`` agent (:func:`propose_abstraction`).

    Anti-starvation trigger (decision e): fires iff ``verified_b:<sg>`` is set — i.e. Path B
    produced a kernel-verified lemma — read INDEPENDENTLY of who won the compare. So when
    RESEARCH mode verified both and compare picked Path A, the bespoke Path-B lemma is still
    generalized into the bank rather than starved. In DEPLOY mode Path B isn't verified once
    A wins ⇒ ``verified_b`` is None ⇒ this cleanly no-ops (nothing fresh to generalize).

    Selection: :func:`propose_abstraction` returns proposals most-general-first; the kernel
    (``full`` mode) re-verifies each in order and the FIRST that type-checks is kept. An
    over-abstraction that no longer type-checks is rejected; if none type-check, fall back
    to the concrete verified lemma. The chosen form is written to ``abstracted:<sg>`` so the
    bank stores the generalized (or fallback) lemma.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    profile = ctx.get("lean_profile", settings.lean_profile)
    fresh_b = ctx.get(_bb_key("verified_b", sg_id))
    if not fresh_b:
        ctx.put(_bb_key("abstracted", sg_id), None)
        ctx.progress(f"[abstract] {sg_id}: no fresh Path-B lemma — skipped")
        return {"handler": "abstract", "subgoal": sg_id, "fired": False,
                "reason": "no fresh Path-B lemma (verified_b unset)"}

    statement = fresh_b.get("statement", "")
    proof_term = fresh_b.get("proof_term", "")
    lean_type = fresh_b.get("lean_type") or goal
    proposals = await propose_abstraction(statement, proof_term, lean_type)

    chosen: Optional[dict[str, Any]] = None
    n_rejected = 0
    for p in proposals:  # most-general first
        src = p.get("source", "")
        if not src:
            continue
        closed, _errors = _full_verdict(src, profile=profile)
        if closed:
            chosen = {
                "source": src,
                "statement": p.get("statement", statement),
                "proof_term": p.get("proof_term", src),
                "origin": "abstract",
                "lean_type": p.get("lean_type") or lean_type,
                "path": fresh_b.get("path", "B"),
            }
            break
        n_rejected += 1  # over-abstraction rejected — try the next, more conservative one

    if chosen is None:
        # No proposal type-checked (or none offered) → fall back to the concrete lemma.
        chosen = {**fresh_b, "origin": "abstract-fallback"}
        abstracted = False
    else:
        abstracted = True
    chosen["generality_score"] = generality_score(chosen)
    discharged = ctx.get(_bb_key("discharged", sg_id)) or {}
    if discharged.get("path") == fresh_b.get("path"):
        chosen["times_won"] = int(discharged.get("times_won") or 0)
    ctx.put(_bb_key("abstracted", sg_id), chosen)
    ctx.progress(
        f"[abstract] {sg_id}: {'abstracted' if abstracted else 'fell back'} "
        f"({n_rejected} over-abstraction(s) rejected)"
    )
    return {
        "handler": "abstract",
        "subgoal": sg_id,
        "fired": True,
        "abstracted": abstracted,
        "n_rejected": n_rejected,
        "origin": chosen["origin"],
    }


# ---------------------------------------------------------------------------
# bank — assemble result.lean + store winners (loud)
# ---------------------------------------------------------------------------

_DECL_KEYWORDS = ("theorem", "lemma", "example", "def", "instance", "abbrev")


def _proof_body_after_assign(src: str) -> Optional[str]:
    """Return the text after the first top-level ``:=`` of ``src``, or None if there is none.

    Top-level = at bracket depth 0, so a ``:=`` inside ``(…)``/``{…}``/``[…]`` (or a type
    ascription ``:`` not followed by ``=``) is skipped. This is the proof body of a
    declaration ``theorem t : T := <body>``.
    """
    depth = 0
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0 and i + 1 < n and src[i + 1] == "=":
            return src[i + 2:].strip()
        i += 1
    return None


def _looks_like_declaration(text: str) -> bool:
    """True if ``text`` begins (past comment/blank lines) with a Lean decl keyword."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        return stripped.split(None, 1)[0] in _DECL_KEYWORDS
    return False


def _bare_proof_term(win: dict[str, Any]) -> str:
    """The bare proof body that fits a ``have … := <here>`` hole, for any winner shape.

    Path-A and synthesized winners carry a bare ``proof_term`` already; a **repair** winner
    sets ``proof_term`` to the *full* ``theorem … := proof`` source (the thing the kernel
    verified). Substituting that whole declaration into a ``sorry`` hole yields malformed
    Lean (``have h : T := theorem … := proof``). So: if the proof term (or source) is a full
    declaration, extract the body after its top-level ``:=``; otherwise it is already bare.
    """
    proof = (win.get("proof_term") or "").strip()
    source = (win.get("source") or "").strip()
    if win.get("source_collection") and _looks_like_declaration(source):
        body = _proof_body_after_assign(source)
        if body:
            return body
    for text in (proof, source):
        if not text:
            continue
        if _looks_like_declaration(text):
            body = _proof_body_after_assign(text)
            if body:
                return body
        return text
    return ""


def _assemble(
    scaffold: str,
    subtasks: list[Subtask],
    discharged: dict[str, dict],
    formal: dict[str, Any] | None = None,
) -> str:
    """Substitute each discharged sub-goal's proof into the scaffold's ``sorry`` holes.

    Replaces the first remaining ``sorry`` token with each sub-goal's **bare** proof term
    (see :func:`_bare_proof_term` — a repair winner's ``proof_term`` is a full declaration
    and must be reduced to its body), in subtask order, so the have-chain that the skeleton
    check accepted becomes a closed proof. Sub-goals with no discharge keep their ``sorry``
    (the final full-mode verify will then reject the artifact — the loss is not hidden).

    Each proof is funneled through :func:`_normalize_proof_rhs` so a *multi-line* tactic
    block (the common Path-A winner shape, ``by\\n  have h … := rfl\\n  first | exact h | …``)
    is collapsed to a single-line ``by t1; t2; …`` before substitution. Pasted verbatim into
    a ``have hᵢ : T := <here>`` hole, a multi-line block's inner lines inherit the scaffold's
    shallow indent instead of nesting under the hole's ``by`` — the kernel then rejects the
    artifact (``expected … indented tactic sequence``). Collapsing makes the RHS
    indentation-insensitive.
    """
    result = scaffold
    for sub in subtasks:
        win = discharged.get(sub.id)
        if not win:
            continue
        proof = _proof_body_for_hole(_normalize_proof_rhs(_bare_proof_term(win)), sub, formal)
        result = result.replace("sorry", proof, 1)
    return result


async def bank_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Assemble the sorry-free ``artifacts/result.lean`` and bank each winning lemma.

    Reads the scaffold + active subtasks from the plan and the per-sub-goal ``discharged``
    winners from the blackboard, stitches them into the scaffold, writes
    ``artifacts/result.lean``, full-verifies the assembled proof (the final ground-truth
    gate), then stores each winner in the lemma bank. Bank writes are LOUD (risk #4): a
    failed ``store_lemma`` is surfaced in the node result (``bank_failures``) rather than
    swallowed, because a lost verified lemma stalls the snowball.
    """
    plan = parse_plan(ctx.task_id)
    subtasks = plan.active_subtasks()
    # Collect discharged winners under the SAME sub-goal ids the rest of the pipeline
    # used. ``_subgoal_id`` resolves to the first active subtask, or the literal ``"0"``
    # fallback on the single-sub-goal happy path (no typed subtasks). Bank must mirror
    # that fallback — otherwise a proof closed under ``discharged:0`` is invisible here
    # and a *verified* lemma is silently never banked (banked 0/0), stalling the snowball.
    candidate_ids = [s.id for s in subtasks] or ["0"]
    if "0" not in candidate_ids:
        candidate_ids.append("0")  # always probe the happy-path fallback id too
    discharged = {sid: ctx.get(_bb_key("discharged", sid)) for sid in candidate_ids}
    discharged = {k: v for k, v in discharged.items() if v}
    if ctx.node.instruction and ctx.node.instruction in candidate_ids:
        required_ids = [ctx.node.instruction]
    else:
        required_ids = [s.id for s in subtasks] or (["0"] if candidate_ids else [])
    missing = [sid for sid in required_ids if sid not in discharged]
    if missing:
        raise ProofFailed(
            "cannot assemble result.lean; undischarged sub-goal(s): " + ", ".join(missing)
        )

    formal = _formal_context(ctx)
    scaffold = _native_scaffold_from_subtasks(ctx, subtasks, plan.active_closer()) or _sanitize_scaffold(plan.scaffold or "")
    assembled_body = _assemble(scaffold, subtasks, discharged, formal) if scaffold else ""
    if assembled_body and formal:
        assembled = _formal_command_from_body(_proof_body_from_scaffold(assembled_body), formal)
    elif assembled_body:
        assembled = _scaffold_as_command(assembled_body, _prose_to_goal_type(ctx.request))
    else:
        assembled = ""

    # Final ground-truth gate on the assembled proof.
    final_ok: Optional[bool] = None
    final_errors: list[str] = []
    if assembled:
        res = verify_lean(assembled, mode="full", profile=ctx.get("lean_profile", settings.lean_profile))
        final_ok = res["ok"] if res["infra_ok"] else None
        final_errors = res["errors"]
        result_path = settings.tasks_dir / ctx.task_id / "artifacts" / "result.lean"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(assembled, encoding="utf-8")
        ctx.put("final_verify", {
            "ok": final_ok,
            "infra_ok": res["infra_ok"],
            "errors": final_errors,
            "result_path": str(result_path),
        })
        if final_ok is not True:
            raise ProofFailed(
                "assembled result.lean failed final verification: "
                + "; ".join(final_errors or ["verifier unavailable"])
            )

    # Store each winning lemma (loud writes — surface failures). Prefer the ABSTRACTED
    # form (§Phase 5): the abstractor's most-general type-checking generalization is what
    # grows the bank's reuse, so store it instead of the concrete winner when present.
    # (result.lean above is still assembled from the concrete ``discharged`` proof that
    # fits the scaffold hole — only the banked lemma is the generalized one.)
    bank_failures: list[dict[str, str]] = []
    banked = 0
    learning_writes = bool(ctx.get("learning_writes_enabled", True))
    for sg_id, win in discharged.items():
        sub = next((s for s in subtasks if s.id == sg_id), None)
        to_bank = ctx.get(_bb_key("abstracted", sg_id)) or win
        if not learning_writes:
            continue
        store = lemma_bank.store_lemma(
            to_bank.get("statement", "") or sg_id,
            to_bank.get("proof_term", ""),
            source_goal=ctx.request,
            verification_mode="full",
            generality_score=float(to_bank.get("generality_score", 0.0) or 0.0),
            lean_type=(sub.lean_type if sub else None) or to_bank.get("lean_type"),
            times_retrieved=int(to_bank.get("times_retrieved") or 0),
            times_won=int(to_bank.get("times_won") or 0),
            origin=to_bank.get("origin") or "skill_library",
        )
        if store["ok"]:
            banked += 1
        else:
            bank_failures.append({"subgoal": sg_id, "error": store["error"] or "unknown"})

    if bank_failures:
        logger.error("bank: %d lemma write(s) failed: %s", len(bank_failures), bank_failures)

    if learning_writes:
        ctx.progress(f"[bank] assembled result.lean; banked {banked}/{len(discharged)} lemma(s)")
    else:
        ctx.progress("[bank] assembled result.lean; lemma banking skipped (eval no-write mode)")
    return {
        "handler": "bank",
        "ok": final_ok,
        "errors": final_errors,
        "n_discharged": len(discharged),
        "n_banked": banked,
        "bank_writes_enabled": learning_writes,
        "bank_failures": bank_failures,
    }


# ===========================================================================
# Definition synthesis (PLAN-definition-synthesis Phase 2)
# ---------------------------------------------------------------------------
# The ONLY operation that extends the language L. When the normal path stalls on a
# lemma, the system synthesizes a *new local definition* (vocabulary) + kernel-verified
# *bridge lemmas* and (later, Phase 3) re-proves the theorem THROUGH that vocabulary.
# Definitions may be invented freely (a conservative extension — a bad def can't make
# anything false, only be useless); ALL soundness lives in the bridges, governed by the
# Phase 0 contract. This section provides the proposer + degeneracy gates + the verifier
# that elaborates the def and proves every bridge soundness-clean. Stall-detection and
# DAG wiring are Phase 4; birth ablation is Phase 3.
# ===========================================================================

# A definition body that is literally ``True``/``False`` carries no content — the cheap
# degeneracy gate rejects it before any proving budget is spent.
_TRIVIAL_DEF_RHS_RE = re.compile(r":=\s*(True|False)\s*$")


def _concept_id(candidate: dict[str, Any]) -> str:
    """Deterministic id for a concept, keyed on its definition source (like the bank)."""
    import hashlib

    src = ((candidate.get("definition") or {}).get("source") or "").strip()
    return hashlib.sha1(src.encode("utf-8")).hexdigest()[:16] if src else ""


async def propose_definition(
    stuck_lemma: str,
    informal_proof: str,
    lemma_plan: str,
    formalized_lemmas: list[str],
    lean_errors: list[str],
    *,
    n: int = 4,
) -> list[dict[str, Any]]:
    """Ask the ``definition_synthesizer`` agent for ``n`` concept candidates.

    The :func:`propose_repair` / :func:`propose_abstraction` twin: a scoped, structured LLM
    call reading model/persona from the ``definition_synthesizer`` agent record. Each
    candidate is a *concept* — a new local definition (vocabulary) plus the bridge lemmas
    that make it usable — aimed at the stuck lemma. The proposer may invent freely; the
    kernel (def must elaborate) and the soundness contract (every bridge) judge it in
    :func:`verify_concept_handler`, so a useless or unsound proposal can never sneak in.

    Returns a list of candidate dicts ``{"definition": {"name", "source"}, "bridges":
    [{"name", "source", "lean_type", "statement"}], "vacuity_probe"?}``. Returns ``[]`` on
    any error so the caller degrades to "no concept synthesized" rather than crashing.
    """
    from hyperion.agents.registry import load_agent

    try:
        record = load_agent("definition_synthesizer")
    except Exception as exc:  # missing record must not crash the controller
        logger.warning("propose_definition: could not load agent (%s)", exc)
        return []

    system = f"{record.role}\n\n{record.goal}\n\n{record.backstory}"
    user = (
        f"The normal proof STALLED on this lemma:\n{stuck_lemma}\n\n"
        f"Informal proof sketch:\n{informal_proof or '(none)'}\n\n"
        f"Lemma plan:\n{lemma_plan or '(none)'}\n\n"
        f"Already-formalized statements:\n" + ("\n".join(formalized_lemmas) or "(none)") + "\n\n"
        f"Kernel diagnostics from the stall:\n" + ("\n".join(lean_errors) or "(none)") + "\n\n"
        f"Invent up to {n} candidate CONCEPTS. Return ONLY a JSON array, each object "
        '{"definition": {"name": <Lean ident>, "source": <full `def ...` source, NO sorry>}, '
        '"bridges": [{"name": <Lean ident>, "source": <full `theorem ...` source, NO sorry>, '
        '"lean_type": <the bare proposition>, "statement": <the signature line>}], '
        '"vacuity_probe": <optional: a complete `example ... := by trivial` that SHOULD FAIL '
        "if the definition is non-vacuous>}. No commentary, no fences."
    )
    try:
        from openai import OpenAI

        client = OpenAI(base_url=settings.litellm_base_url, api_key=settings.llm_api_key)
        resp = client.chat.completions.create(
            model=record.model_alias,
            temperature=record.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        data = json.loads(text)
    except Exception as exc:
        logger.warning("propose_definition: LLM call failed (%s) — no candidates", exc)
        return []

    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        defn = d.get("definition")
        if isinstance(defn, dict) and defn.get("source") and isinstance(d.get("bridges"), list):
            out.append(d)
    return out[:n]


def definition_degeneracy_reasons(
    candidate: dict[str, Any],
    *,
    parent_name: str = "",
    parent_goal: str = "",
) -> list[str]:
    """Cheap, pre-proving degeneracy gates — kill bad definitions before spending budget.

    Pure and offline-testable (no Lean): structural/textual checks only. The Lean-backed
    non-vacuity probe (``example ... := by trivial`` must FAIL) runs in
    :func:`verify_concept_handler`, which elaborates the definition anyway. Returns the
    list of reasons the candidate is degenerate (empty ⇒ it passes the cheap gates).
    """
    reasons: list[str] = []
    defn = candidate.get("definition") or {}
    def_src = (defn.get("source") or "").strip()
    bridges = candidate.get("bridges") or []

    if not def_src:
        reasons.append("empty definition source")
        return reasons  # nothing else is meaningful
    if source_declares_gap(def_src):
        reasons.append("definition contains sorry/admit or a user-declared axiom")
    if _TRIVIAL_DEF_RHS_RE.search(def_src):
        reasons.append("definition body is literally True/False (no content)")
    # Must not be defined IN TERMS OF the parent theorem (would be a renamed hypothesis,
    # not a concept) — neither by name nor as a verbatim copy of the parent goal.
    if parent_name and re.search(rf"\b{re.escape(parent_name)}\b", def_src):
        reasons.append(f"definition mentions the parent theorem name {parent_name!r}")
    if parent_goal:
        norm = lambda s: re.sub(r"\s+", " ", s).strip()
        rhs = def_src.split(":=", 1)[1] if ":=" in def_src else def_src
        if norm(parent_goal) and norm(parent_goal) in norm(rhs):
            reasons.append("definition is defeq to the parent goal (renamed hypothesis)")
    if not bridges:
        reasons.append("no bridge lemmas (≥1 required)")
    for i, b in enumerate(bridges):
        if not isinstance(b, dict) or not (b.get("source") or "").strip():
            reasons.append(f"bridge {i} has no source")
        elif source_declares_gap(b["source"]):
            reasons.append(f"bridge {i} contains sorry/admit/axiom")
        elif parent_name and re.search(rf"\b{re.escape(parent_name)}\b", b["source"]):
            reasons.append(f"bridge {i} mentions the parent theorem name {parent_name!r}")
    return reasons


def _compose_concept_source(def_src: str, bridge_src: str) -> str:
    """Source for proving/elaborating a bridge: the definition precedes the bridge so the
    bridge (and its ``#print axioms``) sees the new vocabulary."""
    return f"{def_src.strip()}\n\n{bridge_src.strip()}\n"


async def synthesize_definition_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Native step: propose concept candidates for a stuck lemma + apply the cheap gates.

    Reads the stall context from the blackboard (the goal type, informal sketch, lemma
    plan, and the kernel diagnostics from the failed verify), asks
    :func:`propose_definition` for ``settings.concept_candidates`` candidates, and keeps
    only those that pass :func:`definition_degeneracy_reasons`. Survivors are written to
    ``concept_candidates:<sg>`` for :func:`verify_concept_handler`. Proving is NOT done
    here — that is the next node, so the cheap gates spend no proving budget.
    """
    sg_id = _subgoal_id(ctx)
    escalated = ctx.get(_bb_key("escalated", sg_id))
    if escalated is False:
        ctx.put(_bb_key("concept_candidates", sg_id), [])
        ctx.put(_bb_key("synthesize_definition", sg_id),
                {"subgoal": sg_id, "skipped": True, "reason": "not escalated"})
        ctx.progress(f"[synthesize_definition] {sg_id}: skipped (not escalated)")
        return {
            "handler": "synthesize_definition",
            "subgoal": sg_id,
            "ok": False,
            "skipped": True,
            "reason": "not escalated",
        }
    goal = _goal_type(ctx, sg_id)
    parent_name = ctx.get(_bb_key("parent_name", sg_id)) or ctx.get("parent_name") or ""
    decision = ctx.get(_bb_key("verify_decision", sg_id)) or {}
    lean_errors = ctx.get(_bb_key("stall_errors", sg_id)) or []
    informal = ctx.get(_bb_key("informal_proof", sg_id)) or ctx.get("informal_proof") or ""
    lemma_plan = ctx.get(_bb_key("lemma_plan", sg_id)) or ctx.get("lemma_plan") or ""
    formalized = ctx.get(_bb_key("formalized_lemmas", sg_id)) or []

    n = settings.concept_candidates
    raw = await propose_definition(goal, informal, lemma_plan, formalized, lean_errors, n=n)

    survivors: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for cand in raw:
        reasons = definition_degeneracy_reasons(cand, parent_name=parent_name, parent_goal=goal)
        cand = {**cand, "concept_id": _concept_id(cand)}
        if reasons:
            rejected.append({"concept_id": cand["concept_id"], "reasons": reasons})
        else:
            survivors.append(cand)

    ctx.put(_bb_key("concept_candidates", sg_id), survivors)
    ctx.put(
        _bb_key("synthesize_definition", sg_id),
        {"subgoal": sg_id, "n_proposed": len(raw), "n_survived": len(survivors),
         "rejected": rejected, "decision_seen": bool(decision)},
    )
    ctx.progress(
        f"[synthesize_definition] {sg_id}: {len(survivors)}/{len(raw)} candidate(s) passed gates"
    )
    return {
        "handler": "synthesize_definition",
        "subgoal": sg_id,
        "ok": bool(survivors),
        "n_proposed": len(raw),
        "n_survived": len(survivors),
    }


async def verify_concept_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Native step: elaborate each candidate's definition + prove every bridge soundness-clean.

    For each surviving candidate (in order): the definition must **elaborate** with no
    ``sorry`` (it's a conservative extension — no proof obligation), it must pass the
    Lean-backed non-vacuity probe when one is supplied (``example ... := by trivial`` must
    FAIL), and EVERY bridge must close via :func:`prove_proposition` **soundness-clean**
    (the Phase 0 ``#print axioms`` contract, scoped to the bridge's decl, with the
    definition in scope). The first candidate that fully verifies is written to
    ``verified_concept:<sg>`` — the package Phase 3 birth ablation re-proves the theorem
    through. No bank write here (that's Phase 4).
    """
    sg_id = _subgoal_id(ctx)
    candidates = ctx.get(_bb_key("concept_candidates", sg_id)) or []
    weak = settings.prover_weak_path_b
    strict = settings.prover_soundness_strict
    profile = ctx.get("lean_profile", settings.lean_profile)

    verified: Optional[dict[str, Any]] = None
    attempts: list[dict[str, Any]] = []

    for cand in candidates:
        cid = cand.get("concept_id") or _concept_id(cand)
        defn = cand.get("definition") or {}
        def_src = (defn.get("source") or "").strip()
        record: dict[str, Any] = {"concept_id": cid, "def_ok": False, "vacuous": False,
                                  "bridges": []}

        # 1. Definition must elaborate (no sorry). Conservative: an infra outage is not a
        #    rejection of the concept, just an un-decidable attempt.
        def_res = verify_lean(def_src, mode="full", profile=profile)
        if not def_res["infra_ok"]:
            record["error"] = "verifier unavailable (definition elaboration)"
            attempts.append(record)
            continue
        record["def_ok"] = bool(def_res["ok"])
        if not def_res["ok"]:
            record["error"] = "definition did not elaborate"
            attempts.append(record)
            continue

        # 2. Non-vacuity probe (optional): the supplied `example ... := by trivial` must FAIL.
        probe = (cand.get("vacuity_probe") or "").strip()
        if probe:
            probe_src = _compose_concept_source(def_src, probe)
            probe_res = verify_lean(probe_src, mode="full", profile=profile)
            if probe_res["infra_ok"] and probe_res["ok"]:
                record["vacuous"] = True
                record["error"] = "vacuity probe closed by trivial (definition is vacuous)"
                attempts.append(record)
                continue

        # 3. Every bridge must close soundness-clean, with the definition in scope.
        proven_bridges: list[dict[str, Any]] = []
        all_ok = True
        for b in cand.get("bridges") or []:
            seed = _compose_concept_source(def_src, b.get("source") or "")
            outcome = await prove_proposition(
                b.get("lean_type") or "", seed, weak=weak, decl=b.get("name"),
                strict_soundness=strict, profile=profile,
            )
            bridge_ok = outcome.won and bool(outcome.axioms_clean)
            record["bridges"].append({
                "name": b.get("name"), "closed": outcome.closed, "won": outcome.won,
                "axioms_clean": outcome.axioms_clean, "repair_iters": outcome.repair_iters,
            })
            if not bridge_ok:
                all_ok = False
                break
            proven_bridges.append({
                "name": b.get("name"),
                "source": outcome.weak_source or outcome.source,
                "proof_term": outcome.proof_term,
                "lean_type": b.get("lean_type") or "",
                "statement": b.get("statement") or "",
                "axioms": outcome.axioms,
            })

        record["all_bridges_ok"] = all_ok
        attempts.append(record)
        if all_ok and proven_bridges:
            verified = {
                "concept_id": cid,
                "definition": {"name": defn.get("name"), "source": def_src},
                "bridges": proven_bridges,
                "origin": "synthesized",
            }
            break

    ctx.put(_bb_key("verified_concept", sg_id), verified)
    ctx.put(
        _bb_key("verify_concept", sg_id),
        {"subgoal": sg_id, "n_candidates": len(candidates), "attempts": attempts,
         "verified": verified is not None},
    )
    ctx.progress(
        f"[verify_concept] {sg_id}: "
        + (f"verified concept {verified['concept_id']}" if verified else "no concept verified")
    )
    return {
        "handler": "verify_concept",
        "subgoal": sg_id,
        "ok": verified is not None,
        "concept_id": verified["concept_id"] if verified else None,
        "n_candidates": len(candidates),
    }


# ===========================================================================
# Birth ablation (PLAN-definition-synthesis Phase 3) — same-budget causal test
# ---------------------------------------------------------------------------
# A concept is provisionally accepted ONLY if it *caused* the proof: re-prove the goal
# THROUGH the package and WITHOUT it at an IDENTICAL budget; accept iff solves-WITH
# (soundness-clean) AND fails-WITHOUT. solves-without ⇒ the concept caused nothing ⇒
# reject (crutch/redundant). The budget must be exactly equal across arms or the causal
# claim collapses — both arms call prove_proposition with the same max_repair, weak gate,
# and goal; only the in-scope vocabulary differs.
# ===========================================================================

_ABLATION_DECL = "ablation_target"


def _concept_preamble(concept: dict[str, Any]) -> str:
    """The definition + all bridge sources, the vocabulary the WITH-arm proves through."""
    def_src = ((concept.get("definition") or {}).get("source") or "").strip()
    bridges = "\n\n".join((b.get("source") or "").strip() for b in concept.get("bridges") or [])
    return f"{def_src}\n\n{bridges}".strip()


async def birth_ablation_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Native step: the same-budget with/without causal test for a verified concept.

    Reads ``verified_concept:<sg>`` and re-proves the sub-goal twice at an identical
    budget (``settings.cap_repair_iters`` repair rounds, same weak gate, same goal):
    the WITH arm has the concept's definition + bridges in scope, the WITHOUT arm does
    not. Accept (provisionally) iff the WITH arm solves soundness-clean AND the WITHOUT
    arm fails. ``solves-without`` ⇒ reject (the concept caused nothing). The accepted
    package is staged at ``accepted_concept:<sg>`` with provisional bank fields
    (``necessity_hits``/``times_won``/``provisional``) for Phase 4 banking + promotion.

    No bank write here; ``bank_concept`` persists only concepts that pass this causal test.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    concept = ctx.get(_bb_key("verified_concept", sg_id))
    if not concept:
        ctx.put(_bb_key("birth_ablation", sg_id),
                {"subgoal": sg_id, "ran": False, "reason": "no verified concept"})
        ctx.progress(f"[birth_ablation] {sg_id}: no verified concept to test")
        return {"handler": "birth_ablation", "subgoal": sg_id, "ok": False,
                "birth_ablation_pass": False, "reason": "no verified concept"}

    weak = settings.prover_weak_path_b
    strict = settings.prover_soundness_strict
    profile = ctx.get("lean_profile", settings.lean_profile)
    budget = settings.cap_repair_iters  # IDENTICAL across arms — B_ablate
    target = f"theorem {_ABLATION_DECL} : {goal} := by sorry"
    with_seed = f"{_concept_preamble(concept)}\n\n{target}\n"
    without_seed = f"{target}\n"

    # Same call shape both arms; the ONLY difference is whether the vocabulary is in scope.
    with_out = await prove_proposition(
        goal, with_seed, weak=weak, max_repair=budget, decl=_ABLATION_DECL,
        strict_soundness=strict, profile=profile,
    )
    without_out = await prove_proposition(
        goal, without_seed, weak=weak, max_repair=budget, decl=_ABLATION_DECL,
        strict_soundness=strict, profile=profile,
    )

    with_solves = with_out.won and bool(with_out.axioms_clean)
    without_solves = without_out.won
    accept = with_solves and not without_solves
    reject_reason = (
        None if accept
        else "solves without the concept (crutch/redundant)" if without_solves
        else "with-arm did not solve soundness-clean"
    )

    result = {
        "subgoal": sg_id, "ran": True, "concept_id": concept.get("concept_id"),
        "budget": budget, "weak": weak,
        "with_solves": with_solves, "without_solves": without_solves,
        "with_axioms_clean": with_out.axioms_clean,
        "with_repair_iters": with_out.repair_iters,
        "without_repair_iters": without_out.repair_iters,
        "accept": accept, "reject_reason": reject_reason,
    }
    ctx.put(_bb_key("birth_ablation", sg_id), result)

    if accept:
        accepted = {
            **concept,
            "with_proof": with_out.weak_source or with_out.source,
            "birth_ablation": {"budget": budget, "with_repair_iters": with_out.repair_iters},
            "provisional": True,
            "necessity_hits": 0,   # later, distinct theorems that need it (Phase 4 promotion)
            "times_won": 1,        # this birth
        }
        ctx.put(_bb_key("accepted_concept", sg_id), accepted)

    ctx.progress(
        f"[birth_ablation] {sg_id}: "
        + (f"ACCEPT concept {concept.get('concept_id')}" if accept
           else f"reject ({reject_reason})")
    )
    return {
        "handler": "birth_ablation", "subgoal": sg_id, "ok": accept,
        "birth_ablation_pass": accept, "concept_id": concept.get("concept_id"),
        "with_solves": with_solves, "without_solves": without_solves,
    }


async def bank_concept_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Persist an accepted concept and expose its with-arm proof as a discharge.

    The final ``bank`` handler still owns result.lean assembly and lemma banking. This
    step only makes the language-extension branch visible to that existing path:
    accepted concepts are stored in the ``concepts`` collection, and their birth proof is
    staged as ``discharged:<sg>`` with path ``"C"``.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    accepted = ctx.get(_bb_key("accepted_concept", sg_id))
    if not accepted:
        ctx.put(_bb_key("bank_concept", sg_id),
                {"subgoal": sg_id, "banked": False, "reason": "no accepted concept"})
        ctx.progress(f"[bank_concept] {sg_id}: no accepted concept")
        return {"handler": "bank_concept", "subgoal": sg_id, "ok": False,
                "banked": False, "reason": "no accepted concept"}

    learning_writes = bool(ctx.get("learning_writes_enabled", True))
    if learning_writes:
        store = concept_bank.store_concept(accepted, source_goal=ctx.request, theorem_id=ctx.task_id)
    else:
        store = {
            "ok": False,
            "id": None,
            "error": "concept banking skipped (eval no-write mode)",
            "skipped": True,
        }
    if store["ok"]:
        accepted = {**accepted, "bank_id": store["id"]}
        ctx.put(_bb_key("accepted_concept", sg_id), accepted)

    definition = accepted.get("definition") or {}
    discharged = {
        "source": accepted.get("with_proof") or "",
        "statement": f"theorem {_ABLATION_DECL} : {goal}",
        "proof_term": _bare_proof_term({"source": accepted.get("with_proof") or ""})
        or accepted.get("with_proof") or "",
        "origin": "concept",
        "path": "C",
        "lean_type": goal,
        "concept_id": accepted.get("concept_id"),
        "definition_name": definition.get("name"),
        "times_won": int(accepted.get("times_won") or 1),
        "necessity_hits": int(accepted.get("necessity_hits") or 0),
    }
    ctx.put(_bb_key("discharged", sg_id), discharged)
    result = {
        "subgoal": sg_id,
        "banked": store["ok"],
        "bank_writes_enabled": learning_writes,
        "store": store,
        "concept_id": accepted.get("concept_id"),
        "discharged": True,
    }
    ctx.put(_bb_key("bank_concept", sg_id), result)
    ctx.progress(
        f"[bank_concept] {sg_id}: "
        + (f"banked concept {accepted.get('concept_id')}" if store["ok"]
           else f"concept write skipped ({store['error']})" if store.get("skipped")
           else f"concept write failed ({store['error']})")
    )
    return {"handler": "bank_concept", "subgoal": sg_id, "ok": store["ok"], **result}


# ---------------------------------------------------------------------------
# Registration (mirrors crews.native's echo registration)
# ---------------------------------------------------------------------------

register_native_handler("formal_ingest", formal_ingest_handler)
register_native_handler("retrieve", retrieve_handler)
register_native_handler("lean_decompose", lean_decompose_handler)
register_native_handler("skeleton_check", skeleton_check_handler)
register_native_handler("verify", verify_handler)
register_native_handler("escalation_gate", escalation_gate_handler)
register_native_handler("compare", compare_handler)
register_native_handler("abstract", abstract_handler)
register_native_handler("bank", bank_handler)
register_native_handler("synthesize_definition", synthesize_definition_handler)
register_native_handler("verify_concept", verify_concept_handler)
register_native_handler("birth_ablation", birth_ablation_handler)
register_native_handler("bank_concept", bank_concept_handler)
