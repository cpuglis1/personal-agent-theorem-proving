#!/usr/bin/env python3
"""
Create the hyperion_memory Qdrant collection.

Dimensions: 1536  (matches text-embedding-3-small)
Distance:   Cosine

Run once after Qdrant starts:
    python agents/hyperion/scripts/init_qdrant.py
"""
import os
import sys

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION = "hyperion_memory"
DIMS = 1536


def main() -> None:
    client = QdrantClient(url=QDRANT_URL)
    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION in existing:
        print(f"Collection '{COLLECTION}' already exists — skipping.")
        return
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=DIMS, distance=Distance.COSINE),
    )
    info = client.get_collection(COLLECTION)
    print(f"Created '{COLLECTION}': {info.status}")


if __name__ == "__main__":
    main()
    sys.exit(0)
