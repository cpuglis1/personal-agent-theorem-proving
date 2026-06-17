# Lean verifier sidecar

The prover's oracle (build-plan Phase 1, deliverable 2). A long-lived service on
`ai-net` exposing `POST /verify {source, mode} -> {ok, errors, elaborated_term}`,
which the Hyperion `lean_verify` tool calls.

## Contract

```
POST /verify
  { "source": "<full Lean 4 source>", "mode": "full" | "skeleton" }
->
  { "ok": bool, "errors": [string], "elaborated_term": string | null }
```

- **full** — no errors AND no `sorry` (the proof must close).
- **skeleton** — no errors; `sorry` permitted (the scaffold must type-check and its
  `have`-chain compose).

`GET /health` returns `{"status":"ok"}`.

## Pin

- **Mathlib:** release tag `v4.15.0` (see `lakefile.lean`).
- **Lean toolchain:** `lean-toolchain` (`leanprover/lean4:v4.15.0`) — matches the
  Mathlib release. Bump both together, then rebuild.
- **Warm cache:** `lake exe cache get` runs at image-build time so verifications never
  cold-build Mathlib.

## Build & run

Brought up together with the base override (service names resolve on `ai-net`):

```bash
docker compose \
  -f docker-compose.override.yml \
  -f docker-compose.lean.yml \
  up -d --build lean
```

The image build is large and slow (it realizes the Mathlib cache) — expected, one-time.

## Test tiers

- The default `uv run pytest` is hermetic: the `@pytest.mark.lean` integration tests
  are **skipped** unless `lake` is on PATH (see `tests/conftest.py`).
- To exercise the live tier, run the suite where the sidecar is reachable at
  `LEAN_URL` (default `http://localhost:8900`) and `lake` is installed:
  `uv run pytest -m lean`.
