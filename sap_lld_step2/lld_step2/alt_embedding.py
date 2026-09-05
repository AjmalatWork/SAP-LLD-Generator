"""Computes and stores embeddings from an alternate (e.g. code-aware) model,
side by side with the original step-2 pipeline's embeddings, for the
embedding-quality investigation.

Deliberately kept outside `embedding.py` / the core schema: it creates and
writes only to its own `investigation_chunk_embeddings` table (created here,
not in sql/init.sql), and never touches `code_chunks.embedding`. This is
additive, investigation-only storage — the original model's vectors are
never modified.

Stores embeddings as a plain float array column rather than pgvector's
`vector` type, since `vector` columns have a fixed dimension and this table
needs to hold models of differing output dimensions side by side (e.g. the
default 384-dim MiniLM alongside a 768-dim code model). Pairwise distances
for the benchmark are computed in Python (see embedding_bench.py), so no
in-database ANN index is needed here.

Usage:
    python -m lld_step2.alt_embedding --model flax-sentence-embeddings/st-codesearch-distilroberta-base
"""
from __future__ import annotations

import argparse

import psycopg
from sentence_transformers import SentenceTransformer

from .config import load_config
from .db import connect

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS investigation_chunk_embeddings (
    chunk_id    INTEGER NOT NULL REFERENCES code_chunks(id) ON DELETE CASCADE,
    model_name  TEXT NOT NULL,
    embedding   DOUBLE PRECISION[] NOT NULL,
    PRIMARY KEY (chunk_id, model_name)
)
"""


def ensure_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLE_SQL)
    conn.commit()


def embed_all_chunks_with_model(conn: psycopg.Connection, model_name: str) -> int:
    """Embeds every chunk currently in `code_chunks` with `model_name` and
    (re)stores the vectors in `investigation_chunk_embeddings`, keyed by
    (chunk_id, model_name). Safe to re-run: replaces this model's rows only.

    Returns the number of chunks embedded.
    """
    ensure_table(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT id, chunk_text FROM code_chunks ORDER BY id")
        rows = cur.fetchall()

    if not rows:
        return 0

    chunk_ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]

    model = SentenceTransformer(model_name)
    embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM investigation_chunk_embeddings WHERE model_name = %s", (model_name,)
        )
        for chunk_id, vec in zip(chunk_ids, embeddings):
            cur.execute(
                """
                INSERT INTO investigation_chunk_embeddings (chunk_id, model_name, embedding)
                VALUES (%s, %s, %s)
                """,
                (chunk_id, model_name, vec.tolist()),
            )
    conn.commit()
    return len(chunk_ids)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="sentence-transformers model name")
    args = parser.parse_args()

    config = load_config()
    conn = connect(config)
    try:
        n = embed_all_chunks_with_model(conn, args.model)
        print(f"embedded {n} chunks with model {args.model!r}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
