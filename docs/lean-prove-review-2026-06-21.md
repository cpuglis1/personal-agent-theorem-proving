# Lean-Prove Pipeline Review - 2026-06-21

Assessment only. I did not change proving, decomposition, scaffold, runner, or banking logic. Added only a read-only classification harness and its generated evidence artifact:

- Harness: `agents/hyperion/scripts/lean_prove_failure_classifier.py`
- Output: `docs/lean-prove-failure-classification-2026-06-21.json`

The harness reads existing SQLite rows, `context.json`, `progress.log`, `plan.md`, and benchmark result jsonl files; it does not submit tasks or call Lean/LLMs.

## Executive Finding

The next architecture decision is proof-state/threaded subgoal support, but only after two hygiene fixes: terminalize verifier timeouts and reconcile orphaned `running` rows. In the available miniF2F dev slice, 1/3 statements definitely needs theorem-local binder context in subgoals (`mathd_algebra_182`); 2/3 are binder-free (`mathd_algebra_462`, `mathd_numbertheory_132`) but still expose scaffold composition/proof-sourcing issues. Evidence: split rows are three miniF2F valid statements in `dev_mathlib.jsonl` lines 1-3; the binder failure is task `8984a2d2` with `subgoal_unbound_context` carrying `y` in h1/h2, and `skeleton_ok=false` in `ai-router/tasks/8984a2d2/context.json:34-60`.

Current passing benchmark signal is not credible as held-out theorem proving: the two core dev passes are curated-core/self-authored (`core-dev-exp-chain-001`, `core-dev-string-conj-001`) and both passed; the available miniF2F mathlib rows are 0/4 passed. Evidence: core result rows at `agents/hyperion/tasks/benchmark-core-dev-results.jsonl:1-2`; miniF2F failures at `agents/hyperion/tasks/benchmark-mathlib-dev-results.jsonl:1` and `agents/hyperion/tasks/benchmark-mathlib-dev-proposition-results.jsonl:1-3`; core split source is `curated-core` at `agents/hyperion/evals/lean_prove_splits/dev.jsonl:1-2`.

## Failure Taxonomy

Classifier scope: 81 task rows across `ai-router/tasks` and `agents/hyperion/tasks` (`docs/lean-prove-failure-classification-2026-06-21.json:17`, roots at lines 40-42).

Primary counts: 42 success, 13 decomposer scaffold contract, 6 operational/infra, 4 proof-sourcing/downstream stall, 2 eval contamination, 2 verifier latency, 1 binder-threading ceiling, 11 unclassified historical failures (`docs/lean-prove-failure-classification-2026-06-21.json:7-16`). Secondary tags count 4 verifier-latency cases because the two orphaned running rows also have skeleton timeouts (`docs/lean-prove-failure-classification-2026-06-21.json:18-23`).

### 1. Operational / Infra

Root cause: background task state is fire-and-forget; startup rehydrates progress but does not reconcile stale `running` DB rows. The API documents background tasks via `asyncio.create_task` at `agents/hyperion/src/hyperion/server/api.py:15-18`, startup calls `_rehydrate_progress()` at `agents/hyperion/src/hyperion/server/api.py:543-564`, and `_run_and_update` marks tasks `running` at `agents/hyperion/src/hyperion/server/api.py:881-909`.

Evidence: tasks `145b9bb6` and `16918b9b` are classified `operational/infra`, still `running`, with no terminal progress marker (`docs/lean-prove-failure-classification-2026-06-21.json:45-96` and `:98-147`). Both also have `skeleton_ok=null` and `lean verifier unavailable: timed out` (`ai-router/tasks/145b9bb6/context.json:34-38`, `ai-router/tasks/16918b9b/context.json:19-23`).

Current handling: no terminal status is written for worker-gone rows. This is operational noise, but it masks verifier timeout evidence because these rows still appear live.

Scrubs: no scaffold scrub fixes this.

### 2. Verifier Latency

Root cause: Lean verification timeout is represented as `infra_ok=False`; `skeleton_check_handler` writes `skeleton_ok=None` and returns `ok=None` on infra failure (`agents/hyperion/src/hyperion/crews/lean_handlers.py:821-831`). The runner only revises/fails on `ok is False` (`agents/hyperion/src/hyperion/crews/runner.py:1147-1153`) and only fans out on `ok is True` (`agents/hyperion/src/hyperion/crews/runner.py:1220-1227`), so `ok=None` is neither clean failure nor normal pass.

Evidence: `145b9bb6` and `16918b9b` are running with `skeleton_ok=null` and timeout errors (`docs/lean-prove-failure-classification-2026-06-21.json:70-77`, `:121-128`). The verifier default timeout is 120 seconds in `agents/hyperion/src/hyperion/tools/lean_verify.py:46-49`, and infra failures become `lean verifier unavailable: ...` at `agents/hyperion/src/hyperion/tools/lean_verify.py:76-84` and `:115-127`.

Current handling: degrades instead of terminalizing. For Mathlib dev this should be a terminal `verifier_timeout`, because continuing after an inconclusive skeleton check confuses later trace interpretation.

Scrubs: no scrub fixes latency. Bad scaffolds may cause long kernel work, but the timeout must still be terminal and separately classified.

### 3. Decomposer Scaffold Contract

Root cause: decomposer output contract is too broad: it emits a free-form scaffold with Lean syntax, subgoal list, and closing tactic. Existing code then scrubs recurring dialect/output errors before asking Lean.

Evidence: `mathd_algebra_462` generated a non-compositional rational scaffold: h1/h2 simplify the factors, h3 proves only `(5 / 6) * (1 / 6) = 5 / 36`, and the close is `exact h3` (`ai-router/tasks/16918b9b/plan.md:8-14`; same pattern in task `1c681809`, `ai-router/tasks/1c681809/plan.md:7-12`). Task `1c681809` was cleanly rejected at skeleton after revision budget with `skeleton_ok=false` (`docs/lean-prove-failure-classification-2026-06-21.json:221-283`). Task `16918b9b` did not get a clean verdict because skeleton timed out (`ai-router/tasks/16918b9b/context.json:19-23`).

Current handling: skeleton failure revises decomposer up to budget, then fails (`agents/hyperion/src/hyperion/crews/runner.py:1142-1187`). Missing/invalid composition is cause; timeout and orphaned running rows are symptoms/noise.

Scrubs currently in scope:

- `_sanitize_scaffold` strips end-of-line commas such as `:= sorry,` (`agents/hyperion/src/hyperion/crews/lean_handlers.py:285-288`, `:326-341`).
- `_canonicalize_closing` rewrites final exact lines containing `▸` or `.trans` to `exact <last_have>` (`agents/hyperion/src/hyperion/crews/lean_handlers.py:290-323`).
- `_sanitize_frontmatter` quotes plain YAML scalar values containing colons at any indent, while skipping block scalar bodies (`agents/hyperion/src/hyperion/crews/plan_contract.py:72-83`, `:97-137`).
- `_sanitize_frontmatter` separately retries invalid `lean_type:` scalars by quoting/escaping them (`agents/hyperion/src/hyperion/crews/plan_contract.py:84-86`, `:121-130`).

Assessment: these scrubs mask an underspecified decomposer/kernel boundary. The cleaner contract is: decomposer emits only typed `have` holes plus metadata; native code owns deterministic closing/composition. That shrinks the LLM surface at the kernel boundary and removes the need to scrub free closing text.

### 4. Binder-Threading Ceiling

Root cause: the independent-subgoal contract cannot prove subgoals that mention theorem-local variables unless those variables are carried into the subgoal theorem. The detector computes formal-bound names and flags subgoal `lean_type` references not internally bound (`agents/hyperion/src/hyperion/crews/lean_handlers.py:402-420`).

Evidence: task `8984a2d2` formal ingestion parsed `(y : ℂ)` and goal `7 * (3 * y + 2) = 21 * y + 14` (`docs/lean-prove-failure-classification-2026-06-21.json:524-541`), then flagged h1/h2 as referencing unbound `y` (`docs/lean-prove-failure-classification-2026-06-21.json:546-559`, also `ai-router/tasks/8984a2d2/context.json:34-60`). The emitted plan indeed has subgoals over `y` (`ai-router/tasks/8984a2d2/plan.md:7-13`).

Current handling: skeleton immediately fails with `error_code=subgoal_unbound_context` and runner returns failed without revision (`agents/hyperion/src/hyperion/crews/lean_handlers.py:803-820`, `agents/hyperion/src/hyperion/crews/runner.py:1155-1161`).

Scrubs: no scrub can make independent theorem subgoals carry theorem-local binders. This needs a contract change.

### 5. Eval Methodology / Self-Authoring Contamination

Root cause: the current green benchmark signal comes from curated core cases that match the hand-tuned, rfl-friendly regime. Dev core cases are `source=curated-core` (`agents/hyperion/evals/lean_prove_splits/dev.jsonl:1-2`), while test is a placeholder and explicitly not a real holdout (`agents/hyperion/evals/lean_prove_splits/test.jsonl:1`).

Evidence: both core dev result rows pass final verification (`agents/hyperion/tasks/benchmark-core-dev-results.jsonl:1-2`), and classifier marks them as eval contamination (`docs/lean-prove-failure-classification-2026-06-21.json:458-517`, `:628-687`). The public miniF2F dev rows are failures (`agents/hyperion/tasks/benchmark-mathlib-dev-results.jsonl:1`, `agents/hyperion/tasks/benchmark-mathlib-dev-proposition-results.jsonl:1-3`).

Current handling: `eval_mode=dev/test` disables learning writes (`agents/hyperion/src/hyperion/crews/runner.py:1339-1342`), and the benchmark helper refuses `test` (`agents/hyperion/src/hyperion/eval/lean_prove_benchmark.py:97-107`). That is good hygiene but not a self-authoring firewall.

## Load-Bearing Questions

### B1. Ceiling Quantification

Available miniF2F dev slice: 3 statements (`agents/hyperion/evals/lean_prove_splits/dev_mathlib.jsonl:1-3`).

Split:

- Binder-free / no theorem-local context needed: 2/3 (`mathd_algebra_462`, `mathd_numbertheory_132`). Evidence: `mathd_algebra_462` formal statement has no local binding group in the theorem header (`agents/hyperion/evals/lean_prove_splits/dev_mathlib.jsonl:2`); `mathd_numbertheory_132` also has no theorem-local binder (`agents/hyperion/evals/lean_prove_splits/dev_mathlib.jsonl:3`). The generated `numbertheory_132` subgoals are binder-free (`ai-router/tasks/601c3b10/plan.md:15-21`).
- Requires theorem-local binder context: 1/3 (`mathd_algebra_182` with `(y : ℂ)`). Evidence: split row has `(y : ℂ)` (`agents/hyperion/evals/lean_prove_splits/dev_mathlib.jsonl:1`); formal-ingested task `8984a2d2` records local context `y` and fails h1/h2 as unbound (`docs/lean-prove-failure-classification-2026-06-21.json:524-559`).

Interpretation: proof-state/threaded subgoals are not premature if miniF2F/mathlib is the target. Even in this tiny slice, 1/3 hits the independent-subgoal ceiling immediately. But 2/3 failures are not fixed by binder threading alone; they need scaffold composition and proof-sourcing hygiene.

### B2. Skeleton Soundness

Does skeleton reject `exact h3` when it actually runs to verdict? Yes for task `1c681809`: scaffold lines show h1/h2/h3 then `exact h3` against the original product target (`ai-router/tasks/1c681809/plan.md:7-12`), and the benchmark/provenance row records `status=failed`, `skeleton_check failed`, `n_subgoals=3`, `trace_events=6` (`agents/hyperion/tasks/benchmark-mathlib-dev-proposition-results.jsonl:2`).

Did the specific `16918b9b` rational case get caught? No clean verdict: it timed out with `skeleton_ok=null` (`ai-router/tasks/16918b9b/context.json:19-23`) and is still a `running` orphan (`docs/lean-prove-failure-classification-2026-06-21.json:98-147`). Its plan shows the same non-compositional `exact h3` (`ai-router/tasks/16918b9b/plan.md:8-14`).

Does final bank verify against original formal statement? For formal-ingested runs, yes by code path: formal ingestion stores `formal_statement`, `formal_header`, `formal_goal`, and local context (`agents/hyperion/src/hyperion/crews/lean_handlers.py:174-189`); skeleton command uses `_formal_command_from_body` when formal context exists (`agents/hyperion/src/hyperion/crews/lean_handlers.py:167-171`); bank assembly also wraps assembled body with the formal command when formal context exists (`agents/hyperion/src/hyperion/crews/lean_handlers.py:1497-1499`). For non-formal runs, bank wraps against `_prose_to_goal_type(ctx.request)` (`agents/hyperion/src/hyperion/crews/lean_handlers.py:1500-1501`), so exact benchmark prompts are safer than natural-language prompts.

### B3. Scrub Accumulation

The scrub list is in the taxonomy above. The pattern indicates the decomposer output contract is underspecified: the model controls both typed holes and closing proof text, while native code later patches syntax (`:= sorry,`), YAML frontmatter quoting, malformed `lean_type`, and fragile closing tactics.

Recommendation: stop adding new closing-text scrubs. Move to a structural contract: decomposer emits only a list of typed `have` holes and optional dependency/composition metadata; native code builds the skeleton and closing deterministically. That does not remove Lean verification; it moves more syntax/composition ownership out of the LLM.

### B4. Thesis-Machinery Reach

Across all 81 available task rows, stage reach by task count: decompose 81, skeleton_check 75, retrieve 64, synthesize 63, verify 63, compare 53, abstract 53, escalation_gate 33, synthesize_definition 33, verify_concept 33, birth_ablation 33, bank_concept 33, bank 53 (`docs/lean-prove-failure-classification-2026-06-21.json:24-33`).

Benchmark-specific read: mathlib mostly bottlenecks at skeleton. Tasks `199fd24d` and `1c681809` have skeleton failure with no retrieve/synthesize/verify stage reach (`docs/lean-prove-failure-classification-2026-06-21.json:151-214`, `:221-283`). Task `8984a2d2` fails at binder-threading skeleton gate (`docs/lean-prove-failure-classification-2026-06-21.json:562-621`). Task `601c3b10` reaches escalation/definition/concept stages but verifies no concept: `concept_candidates` exist, `verified_concept` is null, birth ablation does not run, bank concept has no accepted concept (`ai-router/tasks/601c3b10/context.json:157-165`, `:304-390`).

Conclusion: thesis machinery is implemented and exercised in toy/historical runs, but on current benchmark mathlib it is rarely reached; when reached (`601c3b10`), it does not produce a verified concept. The real bottleneck is still decompose/skeleton/proof sourcing, not concept banking.

### B5. Eval Integrity

Passing benchmark signal: 2/2 current benchmark passes are self-authored/curated core (`agents/hyperion/tasks/benchmark-core-dev-results.jsonl:1-2`, source rows at `agents/hyperion/evals/lean_prove_splits/dev.jsonl:1-2`). Held-out/public miniF2F signal: 0/4 available result rows pass (`agents/hyperion/tasks/benchmark-mathlib-dev-results.jsonl:1`, `agents/hyperion/tasks/benchmark-mathlib-dev-proposition-results.jsonl:1-3`).

Recommended firewall:

- `train`: self-authored and diagnostic cases allowed; may influence prompts/scrubs.
- `dev_public`: public benchmark valid/dev only; no prompt or scrub changes can be justified by a single dev row without adding a category-level rule.
- `dev_private`: private held-out statements not visible in prompts/docs; run only after freezing a change.
- `test`: immutable final holdout; keep current helper refusal to run test by default (`agents/hyperion/src/hyperion/eval/lean_prove_benchmark.py:97-107`), but populate it with real cases instead of the placeholder (`agents/hyperion/evals/lean_prove_splits/test.jsonl:1`).

## Operational Hygiene

1. Reconcile orphaned `running` rows on startup. On boot, mark prior `running` rows as `interrupted` or `failed_interrupted` with a restart timestamp unless there is a live worker registry entry. Evidence: startup rehydrates progress only (`agents/hyperion/src/hyperion/server/api.py:543-564`), and current rows `145b9bb6`/`16918b9b` remain `running` (`docs/lean-prove-failure-classification-2026-06-21.json:45-96`, `:98-147`).

2. Treat Mathlib skeleton verifier timeout as terminal `verifier_timeout`. `verify_lean` distinguishes infra from proof failure (`agents/hyperion/src/hyperion/tools/lean_verify.py:18-28`, `:76-84`); `skeleton_check_handler` currently turns infra into `skeleton_ok=None` (`agents/hyperion/src/hyperion/crews/lean_handlers.py:826-831`). For dev benchmark methodology, `None` should not proceed to retrieve/synthesize.

3. Re-evaluate 120 seconds only after terminalizing timeout. The timeout is currently hard-coded as 120 seconds (`agents/hyperion/src/hyperion/tools/lean_verify.py:46-49`). Some timeouts are likely bad-scaffold symptoms, but the system cannot measure that cleanly while inconclusive skeletons continue or become orphaned running rows.

## Recommendation

One architecture decision: implement proof-state/threaded subgoal handling for theorem-local binders and dependent subgoals, but do it behind the two hygiene fixes above. B1 shows 1/3 current miniF2F dev statements already needs binder threading; B2/B3 show binder threading alone will not fix non-compositional closing or timeout handling.

Stop building:

- Stop treating self-authored rfl-tuned core dev passes as progress signal. They are useful smoke tests only (`agents/hyperion/evals/lean_prove_splits/dev.jsonl:1-2`, `agents/hyperion/tasks/benchmark-core-dev-results.jsonl:1-2`).
- Stop adding mechanical closing scrubs after the current comma/frontmatter/fragile-close set. The structural alternative is native composition from typed holes.
- Do not run `eval_mode=test` until there is a real frozen holdout; the current test row is placeholder (`agents/hyperion/evals/lean_prove_splits/test.jsonl:1`).

Sequenced plan:

1. Must-fix infra: startup reconciliation for stale `running`; terminal `verifier_timeout` for skeleton infra failure; report these distinctly in result jsonl.
2. Contract cleanup: change decomposer contract to typed `have` holes only; native skeleton/closer composes the target and rejects if no deterministic close exists.
3. Architecture bet: proof-state/threaded subgoals carrying formal local context and prior subgoals as premises. Use `mathd_algebra_182`/`8984a2d2` as the first acceptance trace.
4. Eval firewall: freeze public/private dev/test structure, move curated-core to smoke/train, and only count public/private benchmark statements as progress.

Do not do yet: new concept-banking features, more abstractor/birth-ablation work, or additional natural-language benchmark runs. Current evidence says real benchmark work is bottlenecked before those stages.
