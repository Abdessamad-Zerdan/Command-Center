"""Brute-force cosine-similarity retrieval over content_chunks — plenty
fast for a corpus of a few hundred chunks (sub-millisecond), so no vector
index (sqlite-vec, Chroma) is needed.
"""

import numpy as np

from command_center import db
from command_center.assistant import embeddings
from command_center.triage import TriageProviderError


def top_k(query: str, k: int = 5) -> list[dict]:
    return []  # withheld
