"""Prover evaluation & observability (Post-work).

Read-only instruments over a prover run — they never touch the hot path:

  - :mod:`hyperion.eval.trace` — reconstruct a single run's per-stage, per-sub-goal
    output from the durable blackboard (``context.json``) + plan + ``result.lean``, and
    pretty-print it ("see how each stage performs/outputs").
  - :mod:`hyperion.eval.thesis_curve` — aggregate the Phase-5 ``(retrieved, synthesized,
    winner)`` triple logs across runs into the thesis read-out (solved-rate, Path-A
    win-rate, retrieval-beats-synthesis-in-contest, and the running snowball curve).
  - :mod:`hyperion.eval.demo` — a self-contained, offline (mocked LLM + Lean) driver that
    runs sample theorems through the REAL runner so the pipeline and the tracer can be
    exercised with no live toolchain. The same harness runs live once the Lean sidecar
    and an LLM proxy are reachable.
"""
