import Lake
open Lake DSL

-- Lean verifier sidecar project. Depends on Mathlib pinned to the release tag that
-- matches `lean-toolchain` (v4.15.0). `lake exe cache get` fetches prebuilt oleans
-- at image-build time so verifications never cold-build Mathlib.
require mathlib from git
  "https://github.com/leanprover-community/mathlib4.git" @ "v4.15.0"

package «leanproject» where
  -- no extra leanOptions; defaults are fine for type-checking scratch sources

-- A library root so `lake build` has a target; scratch sources are checked
-- standalone via `lake env lean <file>` and need not be listed here.
@[default_target]
lean_lib «Scratch» where
