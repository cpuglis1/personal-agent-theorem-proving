# Handoff: warm-REPL Lean sidecar → dev baseline

You are picking up a theorem-proving (lean-prove) workflow. Your job, in order:

1. Replace the Lean sidecar's cold-spawn verifier with a **long-lived warm-Mathlib REPL** (the durable
   fix — no interim timeout bandaids).
2. Rebuild the sidecar image and validate the REPL is sound (identical verdicts to the cold oracle).
3. Run the **dev baseline** (3 public miniF2F rows) through it and report the pass-rate.

**Do NOT run the `test` split** — it is a frozen holdout; running it destroys its value.
**Do NOT add interim fast paths** (raising `LEAN_TIMEOUT`, narrowing imports, etc.). The user explicitly
rejected short-term bandaids; build the REPL fix properly even though the image rebuild is slow.

---

## Context (already done — do not redo)

- **Binder-threading fix is DONE and verified** (commit `e3106fb`). Subgoals mentioning a theorem-local
  (e.g. `y : ℂ`) are threaded to `∀ (y : ℂ), ...` for independent proving and instantiated back as
  `by exact (...) y`. The unbound-context check returns `[]` for these. Stale failing artifacts under
  `ai-router/tasks/` (e.g. `8984a2d2`) predate this fix — ignore them.
- `hyperion` API runs `reload=False` and bind-mounts `agents/hyperion/src` live, but does **not**
  auto-reload. **If you edit `agents/hyperion/src`, `docker restart hyperion`** or it keeps the stale module.
- Docker Desktop RAM is now **16 GB** (was 8.2 GB). A hot Mathlib process needs ~5 GB resident; 16 GB has
  the headroom for one warm REPL plus litellm (~2.25 GB) and the rest of the stack.

## Why the REPL (root cause)

The sidecar spawns a fresh `lake env lean` per verify, cold-loading the full `import Mathlib` umbrella
(~5500 oleans, ~5 GB) every call. That is intrinsically slow (tens of seconds even with warm page cache,
and was 200s+ / timing out under the old 8.2 GB allocation). The only real fix is to load Mathlib **once**
into a persistent process and verify each snippet against that hot environment → sub-second per check.

## Environment facts

- Repo root: `/Users/cep4u/personal-agent-theorem-proving`
- Python venv (use this, NOT system python): `agents/hyperion/.venv/bin/python`
- Hyperion API: `http://localhost:4100` (no `/health`; use `GET /config` for liveness)
- Lean sidecar: `http://localhost:8900` (`GET /health` works)
- Dev cases (3 rows, all `lean_profile: mathlib`): `agents/hyperion/evals/lean_prove_splits/dev.jsonl`
- Benchmark entry: `python -m hyperion.eval.lean_prove_benchmark`
- Sidecar source (version-controlled): `agents/hyperion/lean-sidecar/{server.py,Dockerfile,lakefile.lean,lean-toolchain}`
- Lean/Mathlib pin: `leanprover/lean4:v4.15.0` + Mathlib `v4.15.0` — **the REPL MUST use this same toolchain**
  or it cannot load the built oleans.

## The HTTP contract you MUST preserve (`lean-sidecar/server.py`)

- `POST /verify {source, mode, profile} -> {ok, errors, elaborated_term}`
  - `mode="full"`: `ok` iff no errors AND no `sorry`.
  - `mode="skeleton"`: `ok` iff no errors (`sorry` permitted).
  - `profile="core"`: reject any `import` line (policy `_profile_errors`); `profile="mathlib"`: allow.
- `POST /axioms {source, decl, profile} -> {ok, axioms, errors}`: appends `#print axioms <decl>`, parses
  `depends on axioms: [...]` / `does not depend on any axioms`. This is the soundness gate (`sorryAx`
  surfaces here). Clients: `hyperion/tools/lean_verify.py`, `hyperion/crews/soundness.py`.
- Keep `/health`.

---

## STEP A — sanity

```bash
cd /Users/cep4u/personal-agent-theorem-proving
docker info --format 'TotalMem={{.MemTotal}}'        # expect ~16.7e9
curl -s -o /dev/null -w 'lean health=%{http_code}\n' http://localhost:8900/health
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'hyperion|lean'
```

## STEP B — implement the warm REPL in the sidecar

Edit `agents/hyperion/lean-sidecar/Dockerfile` and `server.py`. Design:

1. **Dockerfile**: after the existing `lake update -R && lake exe cache get && lake build` step, add a stage
   that builds the REPL executable pinned to the same toolchain:
   ```dockerfile
   # Build leanprover-community/repl against the SAME toolchain (v4.15.0) so it loads our oleans.
   RUN git clone https://github.com/leanprover-community/repl /app/repl \
       && cd /app/repl && git checkout <commit-or-tag-matching-v4.15.0> \
       && cp /app/leanproject/lean-toolchain /app/repl/lean-toolchain \
       && lake build
   ```
   Pick the repl revision whose `lean-toolchain` is `v4.15.0` (check the repl repo's tags/history; the
   repo tracks Lean releases). If exact match is unavailable, use the nearest v4.15.x-compatible revision
   and verify it boots in STEP D before trusting anything.

2. **server.py**: replace the per-call `_run_lean` (which does `subprocess.run(["lake","env","lean",file])`)
   with a persistent REPL driver:
   - On startup, launch the repl binary as a long-lived subprocess with `cwd=/app/leanproject` (so it sees
     the Mathlib deps), e.g. `lake env /app/repl/.lake/build/bin/repl`. Communicate via line-delimited JSON
     on stdin/stdout (the repl protocol: send one JSON object, read one JSON object back).
   - Send `{"cmd": "import Mathlib", "env": null}` **once** at boot. The response carries an environment id
     (`env`, typically `0`). Store it as `BASE_ENV`. This is the hot Mathlib environment.
   - Per `/verify`: strip ALL `import` lines from `source` (the base env already has `import Mathlib`; the
     repl rejects `import` in a non-fresh env). Keep `open ...` lines and the theorem/example. Send
     `{"cmd": <stripped_source>, "env": BASE_ENV}`. **Always branch from `BASE_ENV`; never reuse a returned
     env id** — each verify must start from pristine Mathlib (see correctness trap below).
   - Map the repl response to the contract: repl returns `messages` (each with `severity` ∈
     {error,warning,info}, `pos`, `data`) and a `sorries` array. Build `errors` from severity==error
     messages (format `"{line}:{col}: {msg}"` to match existing parsing expectations); `saw_sorry` =
     `sorries` non-empty OR `"uses 'sorry'"` in any message. Then apply the same `mode`/`profile` logic the
     current code uses.
   - Per `/axioms`: send `{"cmd": <stripped_source> + "\n#print axioms " + decl, "env": BASE_ENV}` and parse
     the `depends on axioms:` / `does not depend on any axioms` text out of the returned messages (reuse the
     existing `_parse_axioms` regexes against the concatenated message data).
   - **Robustness**: guard the subprocess with a lock (one in-flight repl command at a time — the repl is
     single-threaded). If the repl process dies, restart it and re-init `BASE_ENV` before serving. Keep a
     per-command wall guard so a pathological proof can't hang the server forever (return a structured
     timeout error like the current code, not a 500).

### Correctness trap (do not get this wrong)

The repl environment is **stateful**. If you reuse an env id returned by a prior verify, snippet N will see
definitions from snippet N-1 — silently changing accept/reject and making results non-reproducible vs. the
cold `lake env lean` oracle. **Every verify must use `env: BASE_ENV`** and discard the returned env id.

## STEP C — rebuild the sidecar image (slow; expected)

```bash
cd /Users/cep4u/personal-agent-theorem-proving
docker compose \
  -f ai-router/docker-compose.yml \
  -f agents/hyperion/docker-compose.override.yml \
  -f agents/hyperion/docker-compose.lean.yml \
  up -d --build --force-recreate lean
# or: make lean-rebuild
```
This refetches/builds Mathlib + builds the repl — tens of minutes. Then wait for health:
`curl -s http://localhost:8900/health`. First request after boot pays the one-time `import Mathlib` load;
subsequent verifies should be sub-second.

## STEP D — validate REPL soundness BEFORE trusting any baseline

The warm REPL must return **identical verdicts** to the cold oracle. Check all four:

```bash
# (a) known-good closed proof, mathlib  -> ok:true
curl -s -X POST http://localhost:8900/verify -H 'content-type: application/json' \
  -d '{"source":"import Mathlib\n\nexample : (2:Nat)+2=4 := by norm_num","mode":"full","profile":"mathlib"}'
# (b) known-bad proof                    -> ok:false, errors non-empty
curl -s -X POST http://localhost:8900/verify -H 'content-type: application/json' \
  -d '{"source":"import Mathlib\n\nexample : (2:Nat)+2=5 := by norm_num","mode":"full","profile":"mathlib"}'
# (c) sorry scaffold, skeleton mode      -> ok:true ; full mode -> ok:false
curl -s -X POST http://localhost:8900/verify -H 'content-type: application/json' \
  -d '{"source":"import Mathlib\n\nexample : (2:Nat)+2=4 := by sorry","mode":"skeleton","profile":"mathlib"}'
# (d) axioms gate on a clean proof       -> ok:true, no sorryAx in axioms
curl -s -X POST http://localhost:8900/axioms -H 'content-type: application/json' \
  -d '{"source":"import Mathlib\n\ntheorem t : (2:Nat)+2=4 := by norm_num","decl":"t","profile":"mathlib"}'
```
Also confirm **state isolation**: run a verify that defines a name, then a second verify referencing that
name — the second MUST fail (proving env 0 was not mutated). If any of these disagree with expectation, fix
the driver before proceeding. Then run the existing sidecar tests if present:
`cd agents/hyperion && ./.venv/bin/python -m pytest -m lean -q` (or the lean-marked tier).

## STEP E — run the dev baseline, report

```bash
cd /Users/cep4u/personal-agent-theorem-proving/agents/hyperion
rm -f tasks/dev-results.jsonl
HYPERION_API_URL=http://localhost:4100 ./.venv/bin/python -m hyperion.eval.lean_prove_benchmark \
  --cases evals/lean_prove_splits/dev.jsonl \
  --eval-mode dev \
  --out tasks/dev-results.jsonl \
  --poll-seconds 5
cat tasks/dev-results.jsonl
```

**Report to the user:** pass/fail per `case_id`, the `final_verify` verdict, and `subgoal_unbound_context`
(should be empty — confirms the step-1 threading fix held on live rows). This is the SOTA-comparable dev
metric (pass@1 on the unmodified `formal_statement`). **Then STOP and report — do not run `test`.**

(If the benchmark client crashes mid-run — it has a hardcoded 90s per-call socket timeout — the tasks still
complete server-side; collect results via `GET /tasks` and `GET /tasks/{id}/trace` → `.prover.final_verify`.
With sub-second REPL verifies this should not happen.)

---

## Guardrails (do not violate)

- **Never run the `test` split.** Only `smoke` (wiring), `train` (learning writes), `dev` (this baseline).
- **No interim bandaids** — build the REPL fix, not a timeout/import workaround.
- Keep `formal_ingest` strictly deterministic / no-LLM. It only structurally splits the given statement;
  it must never infer goals, rewrite the statement, or retrieve the target — that would taint SOTA comparison.
- If you change `agents/hyperion/src` code, `docker restart hyperion` (no auto-reload).
- After the dev baseline, STOP and report before any further steps (thesis machinery, etc.).
