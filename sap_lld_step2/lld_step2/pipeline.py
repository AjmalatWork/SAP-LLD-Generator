"""End-to-end orchestration: read extraction file -> parse -> load graph ->
chunk -> embed -> load vector table.

Single entry point for the whole step-2 pipeline. Logs progress per object and
exits with a clear message (no raw stack trace) on malformed input.
"""
from __future__ import annotations

import argparse
import sys

import psycopg

from .config import load_config
from .db import connect
from .embedding import embed_and_store_object
from .graph_loader import load_objects
from .parser import ParseError, ParsedObject, parse_extraction_file


def run_pipeline(extraction_file_path: str, conn: psycopg.Connection | None = None) -> dict:
    """Runs the full pipeline. Returns a summary dict for programmatic use
    (tests, callers). Opens/closes its own connection if one isn't provided.
    """
    config = load_config()

    with open(extraction_file_path, "r", encoding="utf-8") as f:
        text = f.read()

    try:
        objects: list[ParsedObject] = list(parse_extraction_file(text))
    except ParseError as exc:
        raise SystemExit(f"Failed to parse extraction file: {exc}") from exc

    print(f"parsed {len(objects)} objects")

    owns_conn = conn is None
    if conn is None:
        conn = connect(config)

    try:
        load_objects(conn, objects)
        total_calls = sum(len(o.calls) for o in objects)
        total_tables = sum(len(o.tables_used) for o in objects)
        print(f"loaded {len(objects)} objects, {total_calls} calls, {total_tables} table-usage rows")

        total_chunks = 0
        for obj in objects:
            n_chunks = embed_and_store_object(
                conn, obj, config.embedding_model, config.chunk_size_lines
            )
            total_chunks += n_chunks
            print(f"embedded {n_chunks} chunks for {obj.name}")

        print(f"embedded {total_chunks} chunks total across {len(objects)} objects")
    finally:
        if owns_conn:
            conn.close()

    return {
        "object_count": len(objects),
        "call_count": total_calls,
        "table_usage_count": total_tables,
        "chunk_count": total_chunks,
        "object_names": [o.name for o in objects],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the step-2 graph + embedding pipeline")
    parser.add_argument("extraction_file", help="path to the extraction .txt file")
    args = parser.parse_args()

    try:
        run_pipeline(args.extraction_file)
    except FileNotFoundError:
        print(f"Error: extraction file not found: {args.extraction_file}", file=sys.stderr)
        sys.exit(1)
    except psycopg.OperationalError as exc:
        print(f"Error: could not connect to the database: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
