"""Embedding + storage of code chunks.

Model is configurable (see config.py / LLD_EMBEDDING_MODEL) so a code-specific
model can be swapped in later without touching the rest of the pipeline. The
model is loaded lazily and cached at module level so repeated calls within one
process don't reload it.
"""
from __future__ import annotations

from functools import lru_cache

import psycopg
from sentence_transformers import SentenceTransformer

from .chunking import split_source_into_chunks
from .parser import ParsedObject


@lru_cache(maxsize=4)
def _get_model(model_name: str) -> SentenceTransformer:
    return SentenceTransformer(model_name)


def embed_and_store_object(
    conn: psycopg.Connection,
    obj: ParsedObject,
    model_name: str,
    chunk_size_lines: int,
) -> int:
    """Chunk, embed, and (re)store an object's code_chunks rows.

    Deletes any existing chunk rows for the object first, so re-running is
    safe and does not duplicate or leave stale chunks behind.

    Returns the number of chunks stored.
    """
    chunks = split_source_into_chunks(obj.source, fallback_chunk_size_lines=chunk_size_lines)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM code_chunks WHERE object_name = %s", (obj.name,))

        if not chunks:
            conn.commit()
            return 0

        model = _get_model(model_name)
        embeddings = model.encode(chunks, show_progress_bar=False, normalize_embeddings=True)

        for idx, (chunk_text, embedding) in enumerate(zip(chunks, embeddings)):
            vector_literal = "[" + ",".join(f"{v:.8f}" for v in embedding.tolist()) + "]"
            cur.execute(
                """
                INSERT INTO code_chunks (object_name, chunk_index, chunk_text, embedding)
                VALUES (%s, %s, %s, %s::vector)
                ON CONFLICT (object_name, chunk_index) DO UPDATE
                    SET chunk_text = EXCLUDED.chunk_text, embedding = EXCLUDED.embedding
                """,
                (obj.name, idx, chunk_text, vector_literal),
            )

    conn.commit()
    return len(chunks)


def find_similar_chunks(
    conn: psycopg.Connection, chunk_id: int, top_k: int = 5
) -> list[dict]:
    """Find the `top_k` chunks most similar (cosine distance) to a given chunk,
    excluding the chunk itself.
    """
    query = """
        SELECT c2.id, c2.object_name, c2.chunk_index, c2.chunk_text,
               c2.embedding <=> c1.embedding AS distance
        FROM code_chunks c1
        JOIN code_chunks c2 ON c2.id != c1.id
        WHERE c1.id = %s
        ORDER BY distance ASC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(query, (chunk_id, top_k))
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "object_name": r[1],
            "chunk_index": r[2],
            "chunk_text": r[3],
            "distance": float(r[4]),
        }
        for r in rows
    ]
