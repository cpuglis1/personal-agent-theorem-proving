#!/usr/bin/env python3
"""One-shot bootstrap script that provisions the ``hyperion_memory`` Qdrant collection.

Role in the system
-------------------
Hyperion (the multi-agent orchestrator at ``agents/hyperion``) stores its
episodic / long-term memory as vectors in Qdrant, the shared vector DB that runs
in the ``ai-router`` Docker stack on port 6333. Before Hyperion can write or
search those vectors, the target collection must exist with the correct vector
geometry. This script creates that collection if it is missing.

It is meant to be run **once** as a setup step right after the Qdrant container
starts (and re-running it is safe — see the idempotency note below)::

    python agents/hyperion/scripts/init_qdrant.py

Key design decisions / non-obvious context
------------------------------------------
- Dimensions: 1536. This must match the embedding model used elsewhere in
  Hyperion, ``text-embedding-3-small``. Changing the embedding model would
  require recreating the collection with a matching ``size``.
- Distance: Cosine. Standard choice for normalized text embeddings; must agree
  with how query vectors are produced at search time.
- Idempotent: the script checks for an existing collection and exits without
  modifying it, so it will not clobber stored vectors on re-runs.
- Connection target is configurable via the ``QDRANT_URL`` environment variable
  (defaults to the local Docker stack at ``http://localhost:6333``).

Environment variables
---------------------
- ``QDRANT_URL`` (optional): base URL of the Qdrant server. Defaults to
  ``http://localhost:6333``.
"""
import os
import sys

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

# Qdrant server endpoint; overridable via env so the same script works against
# the local Docker stack, CI, or a remote instance.
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
# Name of the collection Hyperion reads/writes its memory vectors to.
COLLECTION = "hyperion_memory"
# Embedding dimensionality — must match text-embedding-3-small (see module docstring).
DIMS = 1536


def main() -> None:
    """Create the ``hyperion_memory`` collection in Qdrant if it does not exist.

    Connects to the Qdrant server at :data:`QDRANT_URL`, checks whether the
    :data:`COLLECTION` already exists, and — only when it is absent — creates it
    with a vector config of :data:`DIMS` dimensions and cosine distance.

    Side effects:
        - Opens a network connection to the Qdrant server.
        - May create a new collection on the server (mutates server state).
        - Prints a human-readable status line to stdout describing whether the
          collection was skipped (already present) or newly created.

    Returns:
        None.

    Raises:
        Propagates any exception raised by the underlying ``QdrantClient``
        (e.g. connection errors if Qdrant is unreachable, or API errors during
        collection creation/inspection).
    """
    client = QdrantClient(url=QDRANT_URL)
    # Set of names of collections that already exist on the server.
    existing = {c.name for c in client.get_collections().collections}
    # Idempotency guard: never recreate (and thus never wipe) an existing collection.
    if COLLECTION in existing:
        print(f"Collection '{COLLECTION}' already exists — skipping.")
        return
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=DIMS, distance=Distance.COSINE),
    )
    # Read back the collection to surface its post-creation status to the operator.
    info = client.get_collection(COLLECTION)
    print(f"Created '{COLLECTION}': {info.status}")


def seed_bank() -> None:
    """Provision + seed the prover's ``lemma_bank`` with the cold-start starter set.

    Delegates to :func:`hyperion.memory.lemma_seed.seed_lemma_bank`, which self-creates
    the collection (cosine, 1536-wide) on first write and dedups on the normalized
    statement — so this is idempotent and re-running never duplicates. Unlike
    ``hyperion_memory`` above (created empty here), the bank is provisioned *by* the
    seed write itself, so an empty bank and a missing collection can never diverge.

    Import is lazy + guarded: when run from a host without the Hyperion package
    installed, collection creation for ``hyperion_memory`` still succeeds and only the
    bank seed is skipped (with a note), so the script stays runnable standalone.
    """
    try:
        from hyperion.memory.lemma_seed import seed_lemma_bank
    except Exception as exc:  # package not importable (host-only run) — skip, don't fail.
        print(f"Skipping lemma_bank seed (Hyperion package not importable: {exc}).")
        return
    summary = seed_lemma_bank()
    print(f"Seeded 'lemma_bank': {summary['ok']} stored, {summary['failed']} failed.")


if __name__ == "__main__":
    # Run the bootstrap and exit 0 explicitly so callers/CI get a clean success code.
    main()
    seed_bank()
    sys.exit(0)
