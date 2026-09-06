"""End-to-end orchestration: read extraction file -> parse -> load graph
(+ DDIC, FULL/INCREMENTAL semantics) -> chunk -> embed -> load vector table.

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
from .graph_loader import load_extraction_file
from .parser import ExtractionFile, ParseError, parse_extraction_file

# Real files from ZLLD_PACKAGE_EXTRACTOR (GUI_DOWNLOAD, filetype='ASC') come
# out in the SAP GUI's local codepage, confirmed cp1252 on the one real file
# seen so far - not UTF-8. Synthetic files from mock_generator.py are plain
# Python-written UTF-8. Try UTF-8 first (the common case for anything not
# from a real SAP download) and fall back to cp1252 rather than guessing
# from the filename, since either kind of file can show up with any name.
_ENCODINGS_TO_TRY = ("utf-8", "cp1252")


def _read_extraction_file(path: str) -> str:
    last_error: UnicodeDecodeError | None = None
    for encoding in _ENCODINGS_TO_TRY:
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError as exc:
            last_error = exc
    raise SystemExit(
        f"Could not decode {path} as any of {_ENCODINGS_TO_TRY}: {last_error}"
    )


def run_pipeline(extraction_file_path: str, conn: psycopg.Connection | None = None) -> dict:
    """Runs the full pipeline. Returns a summary dict for programmatic use
    (tests, callers). Opens/closes its own connection if one isn't provided.
    """
    config = load_config()

    text = _read_extraction_file(extraction_file_path)

    try:
        extraction: ExtractionFile = parse_extraction_file(text)
    except ParseError as exc:
        raise SystemExit(f"Failed to parse extraction file: {exc}") from exc

    print(
        f"parsed {len(extraction.objects)} objects "
        f"(RUN_TYPE={extraction.run_type}, PACKAGE={extraction.package})"
    )
    if extraction.removed_objects:
        removed_str = ", ".join(f"{t}:{n}" for t, n in extraction.removed_objects)
        print(f"removed objects to process: {removed_str}")

    owns_conn = conn is None
    if conn is None:
        conn = connect(config)

    try:
        load_extraction_file(conn, extraction)

        total_dependencies = sum(len(o.dependencies) for o in extraction.objects)
        print(
            f"loaded {len(extraction.objects)} objects, {total_dependencies} dependency "
            f"entries, {len(extraction.ddic_objects)} DDIC objects, "
            f"{len(extraction.removed_objects)} removed objects processed"
        )

        total_chunks = 0
        for obj in extraction.objects:
            n_chunks = embed_and_store_object(
                conn, obj, config.embedding_model, config.chunk_size_lines
            )
            total_chunks += n_chunks
            print(f"embedded {n_chunks} chunks for {obj.name}")

        print(f"embedded {total_chunks} chunks total across {len(extraction.objects)} objects")
    finally:
        if owns_conn:
            conn.close()

    return {
        "run_type": extraction.run_type,
        "package": extraction.package,
        "object_count": len(extraction.objects),
        "dependency_count": total_dependencies,
        "ddic_object_count": len(extraction.ddic_objects),
        "removed_object_count": len(extraction.removed_objects),
        "chunk_count": total_chunks,
        "object_names": [o.name for o in extraction.objects],
        "removed_objects": extraction.removed_objects,
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
