"""Local embeddings via fastembed (ONNX Runtime, no torch) — deliberately
lighter than HukukDenge's sentence-transformers/torch setup since this
corpus is a few hundred short English chunks, not a legal corpus. Model
loads once (first call) and is reused for the life of the process.
"""

import numpy as np
from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-small-en-v1.5"

# BGE models are trained asymmetrically: retrieval quality is
# meaningfully better when queries (not documents) get this instruction
# prefix prepended. Documents are embedded as-is.
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=MODEL_NAME)
    return _model


def embed_documents(texts: list[str]) -> np.ndarray:
    return np.array(list(_get_model().embed(texts)))


def embed_query(text: str) -> np.ndarray:
    return np.array(list(_get_model().embed([_QUERY_PREFIX + text])))[0]
