"""Brute-force cosine-similarity retrieval over content_chunks — plenty
fast for a corpus of a few hundred chunks (sub-millisecond), so no vector
index (sqlite-vec, Chroma) is needed.
"""

import numpy as np

from command_center import db
from command_center.assistant import embeddings


def top_k(query: str, k: int = 5) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT source, section, content, embedding FROM content_chunks"
        ).fetchall()

    if not rows:
        return []

    query_vector = embeddings.embed_query(query)
    matrix = np.array([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])

    query_norm = query_vector / np.linalg.norm(query_vector)
    matrix_norms = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    scores = matrix_norms @ query_norm

    order = np.argsort(-scores)[:k]
    return [
        {
            "source": rows[i]["source"],
            "section": rows[i]["section"],
            "content": rows[i]["content"],
            "score": float(scores[i]),
        }
        for i in order
    ]
