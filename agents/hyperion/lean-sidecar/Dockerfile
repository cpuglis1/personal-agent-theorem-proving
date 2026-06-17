# =============================================================================
# Lean verifier sidecar image — warm Mathlib cache + a tiny FastAPI /verify server.
#
# Pin (build-plan Pre-work DECISION): Mathlib release tag v4.15.0, and the Lean
# toolchain is taken from that project's `lean-toolchain` (here pinned to the
# matching leanprover/lean4:v4.15.0). Bump = change MATHLIB_TAG and lean-toolchain
# together, then rebuild.
#
# The heavy step (`lake exe cache get` + building the project against Mathlib) runs
# at BUILD time so the resulting image carries prebuilt oleans — no per-request
# Mathlib build (risk B2). This image is large and slow to build; that is expected.
# =============================================================================
FROM python:3.12-slim

ARG MATHLIB_TAG=v4.15.0

ENV DEBIAN_FRONTEND=noninteractive \
    ELAN_HOME=/root/.elan \
    PATH=/root/.elan/bin:$PATH \
    LEAN_PROJECT_DIR=/app/leanproject

# git + curl for elan/lake; build-essential for any native deps Lake may compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install elan (the Lean toolchain manager); the toolchain itself is selected by the
# project's lean-toolchain file on first `lake` invocation.
RUN curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
        -o /tmp/elan-init.sh \
    && sh /tmp/elan-init.sh -y --default-toolchain none \
    && rm /tmp/elan-init.sh

WORKDIR /app/leanproject

# Copy the project manifest first so the (expensive) Mathlib fetch/build layer caches
# independently of the server code.
COPY lean-toolchain lakefile.lean ./
# Pin Mathlib to the release tag, fetch the prebuilt cache, then build.
RUN lake update -R \
    && lake exe cache get \
    && lake build

# Pre-create the scratch package dir the server writes into.
RUN mkdir -p /app/leanproject/Scratch

# The FastAPI verify server.
WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" pydantic
COPY server.py /app/server.py

EXPOSE 8900
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8900"]
