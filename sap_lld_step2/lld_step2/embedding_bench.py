"""Embedding-quality benchmark: labels chunk pairs as related/unrelated using
only ground truth already in the graph tables (`object_calls`,
`object_uses_table`), then compares cosine-distance separation between the
two groups for a given embedding source.

This is an investigation tool, not part of the step-2 pipeline. It reads
existing data (objects/object_calls/object_uses_table/code_chunks) and,
for a non-default embedding source, an investigation-only side table (see
`alt_embedding.py`) — it does not modify the core schema or the pipeline.

Usage:
    python -m lld_step2.embedding_bench --model original
    python -m lld_step2.embedding_bench --model <alt-model-name-as-stored>

Reusable: `run_benchmark(conn, embedding_source)` takes the embedding source
as a parameter, so the same benchmark can run against the original model,
any alt model already stored via alt_embedding.py, or a future one.
"""
from __future__ import annotations

import argparse
import itertools
import statistics
from dataclasses import dataclass, field

import numpy as np
import psycopg

from .config import load_config
from .db import connect

ORIGINAL_SOURCE = "original"


@dataclass
class PairResult:
    chunk_id_a: int
    chunk_id_b: int
    object_a: str
    object_b: str
    label: str  # "related" | "unrelated"
    distance: float


@dataclass
class BenchmarkReport:
    embedding_source: str
    object_pair_counts: dict
    chunk_pair_counts: dict
    related_distances: list = field(default_factory=list)
    unrelated_distances: list = field(default_factory=list)

    def stats(self, distances: list[float]) -> dict:
        if not distances:
            return {"n": 0, "mean": None, "median": None, "stdev": None}
        return {
            "n": len(distances),
            "mean": statistics.mean(distances),
            "median": statistics.median(distances),
            "stdev": statistics.pstdev(distances) if len(distances) > 1 else 0.0,
            "min": min(distances),
            "max": max(distances),
        }

    def overlap_fraction(self) -> float | None:
        """Fraction of unrelated-pair distances that are <= the median
        related-pair distance (i.e. score at least as "similar" as a
        typical related pair despite having no known relationship).
        """
        if not self.related_distances or not self.unrelated_distances:
            return None
        median_related = statistics.median(self.related_distances)
        closer_or_equal = sum(1 for d in self.unrelated_distances if d <= median_related)
        return closer_or_equal / len(self.unrelated_distances)

    def histogram_text(self, bins: int = 10) -> str:
        all_d = self.related_distances + self.unrelated_distances
        if not all_d:
            return "(no data)"
        lo, hi = min(all_d), max(all_d)
        if lo == hi:
            hi = lo + 1e-6
        width = (hi - lo) / bins
        lines = []
        for i in range(bins):
            b_lo = lo + i * width
            b_hi = b_lo + width
            r_count = sum(1 for d in self.related_distances if b_lo <= d < b_hi or (i == bins - 1 and d == b_hi))
            u_count = sum(1 for d in self.unrelated_distances if b_lo <= d < b_hi or (i == bins - 1 and d == b_hi))
            lines.append(
                f"  [{b_lo:.3f}-{b_hi:.3f}) related:{'#' * r_count:<20} ({r_count})  "
                f"unrelated:{'*' * u_count:<20} ({u_count})"
            )
        return "\n".join(lines)


def _fetch_object_calls(conn: psycopg.Connection) -> set[frozenset]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT source_object, target_object
            FROM object_calls
            WHERE target_object IN (SELECT name FROM objects)
            """
        )
        return {frozenset((a, b)) for a, b in cur.fetchall() if a != b}


def _fetch_shared_table_pairs(conn: psycopg.Connection) -> set[frozenset]:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name, object_name FROM object_uses_table")
        rows = cur.fetchall()
    by_table: dict[str, list[str]] = {}
    for table_name, object_name in rows:
        by_table.setdefault(table_name, []).append(object_name)
    pairs = set()
    for objs in by_table.values():
        for a, b in itertools.combinations(sorted(set(objs)), 2):
            pairs.add(frozenset((a, b)))
    return pairs


def build_labeled_object_pairs(conn: psycopg.Connection) -> list[tuple[str, str, str]]:
    """Every distinct object pair, labeled `related` (direct call edge in
    either direction, OR shares a table) or `unrelated` (neither), derived
    purely from graph ground truth already in the database.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM objects ORDER BY name")
        all_objects = [row[0] for row in cur.fetchall()]

    call_pairs = _fetch_object_calls(conn)
    table_pairs = _fetch_shared_table_pairs(conn)
    related_pairs = call_pairs | table_pairs

    labeled = []
    for a, b in itertools.combinations(all_objects, 2):
        label = "related" if frozenset((a, b)) in related_pairs else "unrelated"
        labeled.append((a, b, label))
    return labeled


def build_chunk_pairs(
    conn: psycopg.Connection,
    object_pairs: list[tuple[str, str, str]],
    min_chunk_length: int = 0,
) -> list[tuple[int, int, str, str, str]]:
    """Expands each labeled object pair into every (chunk_a, chunk_b) cross
    combination between the two objects' chunks. Returns tuples of
    (chunk_id_a, chunk_id_b, object_a, object_b, label).

    `min_chunk_length` excludes trivial chunks (e.g. a lone "ENDCLASS." left
    over after the last method boundary) from the benchmark's pair set. This
    is a benchmark-time analysis filter only — it does not change how step 2
    itself chunks or stores anything.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, object_name FROM code_chunks WHERE length(chunk_text) >= %s "
            "ORDER BY object_name, chunk_index",
            (min_chunk_length,),
        )
        rows = cur.fetchall()

    chunks_by_object: dict[str, list[int]] = {}
    for chunk_id, object_name in rows:
        chunks_by_object.setdefault(object_name, []).append(chunk_id)

    chunk_pairs = []
    for obj_a, obj_b, label in object_pairs:
        for chunk_a in chunks_by_object.get(obj_a, []):
            for chunk_b in chunks_by_object.get(obj_b, []):
                chunk_pairs.append((chunk_a, chunk_b, obj_a, obj_b, label))
    return chunk_pairs


def fetch_embeddings(conn: psycopg.Connection, embedding_source: str) -> dict[int, np.ndarray]:
    """Returns {chunk_id: embedding vector} for the given source.

    `embedding_source == "original"` reads the vectors already stored in
    `code_chunks.embedding` (the step-2 pipeline's own model). Any other
    value is looked up in the investigation-only `investigation_chunk_embeddings`
    side table (see alt_embedding.py), keyed by model_name.
    """
    with conn.cursor() as cur:
        if embedding_source == ORIGINAL_SOURCE:
            cur.execute("SELECT id, embedding::text FROM code_chunks")
            rows = cur.fetchall()
            return {
                chunk_id: np.array(
                    [float(x) for x in text.strip("[]").split(",")], dtype=np.float64
                )
                for chunk_id, text in rows
            }
        else:
            cur.execute(
                "SELECT chunk_id, embedding FROM investigation_chunk_embeddings WHERE model_name = %s",
                (embedding_source,),
            )
            rows = cur.fetchall()
            if not rows:
                raise ValueError(
                    f"No stored embeddings found for model_name={embedding_source!r}. "
                    "Run alt_embedding.py first to compute and store them."
                )
            return {chunk_id: np.array(vec, dtype=np.float64) for chunk_id, vec in rows}


def cosine_distance(v1: np.ndarray, v2: np.ndarray) -> float:
    denom = np.linalg.norm(v1) * np.linalg.norm(v2)
    if denom == 0:
        return 1.0
    return float(1.0 - np.dot(v1, v2) / denom)


def run_benchmark(
    conn: psycopg.Connection, embedding_source: str, min_chunk_length: int = 0
) -> BenchmarkReport:
    object_pairs = build_labeled_object_pairs(conn)
    chunk_pairs = build_chunk_pairs(conn, object_pairs, min_chunk_length=min_chunk_length)
    embeddings = fetch_embeddings(conn, embedding_source)

    object_pair_counts = {
        "related": sum(1 for *_r, label in object_pairs if label == "related"),
        "unrelated": sum(1 for *_r, label in object_pairs if label == "unrelated"),
        "total": len(object_pairs),
    }

    report = BenchmarkReport(
        embedding_source=embedding_source,
        object_pair_counts=object_pair_counts,
        chunk_pair_counts={"related": 0, "unrelated": 0, "total": len(chunk_pairs)},
    )

    for chunk_a, chunk_b, _obj_a, _obj_b, label in chunk_pairs:
        if chunk_a not in embeddings or chunk_b not in embeddings:
            continue
        d = cosine_distance(embeddings[chunk_a], embeddings[chunk_b])
        if label == "related":
            report.related_distances.append(d)
            report.chunk_pair_counts["related"] += 1
        else:
            report.unrelated_distances.append(d)
            report.chunk_pair_counts["unrelated"] += 1

    return report


def print_report(report: BenchmarkReport) -> None:
    print(f"=== Embedding benchmark: {report.embedding_source} ===")
    print(f"Object pairs: {report.object_pair_counts}")
    print(f"Chunk pairs:  {report.chunk_pair_counts}")

    r_stats = report.stats(report.related_distances)
    u_stats = report.stats(report.unrelated_distances)
    print(f"related   n={r_stats['n']:<4} mean={r_stats['mean']:.4f} median={r_stats['median']:.4f} "
          f"stdev={r_stats['stdev']:.4f} min={r_stats['min']:.4f} max={r_stats['max']:.4f}")
    print(f"unrelated n={u_stats['n']:<4} mean={u_stats['mean']:.4f} median={u_stats['median']:.4f} "
          f"stdev={u_stats['stdev']:.4f} min={u_stats['min']:.4f} max={u_stats['max']:.4f}")

    overlap = report.overlap_fraction()
    if overlap is not None:
        print(f"overlap: {overlap * 100:.1f}% of unrelated pairs score <= median related distance")

    print("Distribution (10 bins across the combined range):")
    print(report.histogram_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=ORIGINAL_SOURCE,
        help=f"embedding source: '{ORIGINAL_SOURCE}' (default, uses code_chunks.embedding) "
        "or a model_name already stored via alt_embedding.py",
    )
    parser.add_argument(
        "--min-chunk-length",
        type=int,
        default=0,
        help="exclude chunks shorter than this many characters from the pair set "
        "(analysis-time filter only; does not affect step 2's stored chunks)",
    )
    args = parser.parse_args()

    config = load_config()
    conn = connect(config)
    try:
        report = run_benchmark(conn, args.model, min_chunk_length=args.min_chunk_length)
        print_report(report)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
