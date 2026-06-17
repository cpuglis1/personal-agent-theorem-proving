-- Library root for the «Scratch» target. Importing Mathlib here makes `lake build`
-- realize the (cached) Mathlib oleans at image-build time, so the first real request
-- is warm. Submitted sources are checked standalone via `lake env lean <file>` and
-- carry their own imports; they do not depend on this module.
import Mathlib
