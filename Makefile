HYPERION_DIR := agents/hyperion
HYPERION_UV := $(HYPERION_DIR)/.venv/bin/uv

.PHONY: hyperion-test hyperion-test-offline hyperion-test-prover hyperion-api hyperion-mcp ui-build
.PHONY: lean-up lean-rebuild lean-smoke

hyperion-test:
	cd $(HYPERION_DIR) && .venv/bin/uv run pytest tests -q

hyperion-test-offline:
	cd $(HYPERION_DIR) && .venv/bin/uv run pytest tests -q -m 'not lean'

hyperion-test-prover:
	cd $(HYPERION_DIR) && .venv/bin/uv run pytest \
		tests/test_plan_contract_lean.py \
		tests/test_candidate_from_lemma.py \
		tests/test_lemma_bank.py \
		tests/test_lemma_seed.py \
		tests/test_lemma_retrieval.py \
		tests/test_lean_verify.py \
		tests/test_lean_prove_workflow.py \
		tests/test_abstractor.py \
		tests/test_compare.py \
		tests/test_eval.py \
		tests/test_native_node.py \
		tests/test_native_stage_trace.py \
		tests/test_prover_trace_surface.py \
		-q -m 'not lean'

hyperion-api:
	cd $(HYPERION_DIR) && .venv/bin/uv run hyperion-api

hyperion-mcp:
	cd $(HYPERION_DIR) && .venv/bin/uv run hyperion-mcp

ui-build:
	cd agents/hyperion-ui && npm run build

lean-up:
	docker compose \
		-f ai-router/docker-compose.yml \
		-f agents/hyperion/docker-compose.override.yml \
		-f agents/hyperion/docker-compose.lean.yml \
		up -d lean

lean-rebuild:
	docker compose \
		-f ai-router/docker-compose.yml \
		-f agents/hyperion/docker-compose.override.yml \
		-f agents/hyperion/docker-compose.lean.yml \
		up -d --build --force-recreate lean

lean-smoke:
	curl -sS http://localhost:8900/health
	curl -sS -X POST http://localhost:8900/verify \
		-H 'Content-Type: application/json' \
		-d '{"source":"theorem t : True := trivial","mode":"full"}'
