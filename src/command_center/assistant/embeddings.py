"""Local embeddings via fastembed (ONNX Runtime, no torch) — deliberately
lighter than HukukDenge's sentence-transformers/torch setup since this
corpus is a few hundred short English chunks, not a legal corpus. Model
loads once (first call) and is reused for the life of the process.
"""

import numpy as np
from fastembed import TextEmbedding
from command_center.triage import TriageProviderError

MODEL_NAME = "BAAI/bge-small-en-v1.5"

# BGE models are trained asymmetrically: retrieval quality is
# meaningfully better when queries (not documents) get this instruction
# prefix prepended. Documents are embedded as-is.


def _get_model() -> TextEmbedding:
    raise TriageProviderError("_get_model: withheld from the public showcase - see README.md")


def embed_documents(texts: list[str]) -> np.ndarray:
    raise TriageProviderError("embed_documents: withheld from the public showcase - see README.md")


def embed_query(text: str) -> np.ndarray:
    raise TriageProviderError("embed_query: withheld from the public showcase - see README.md")
