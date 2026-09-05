"""End-to-end acceptance test: generate mock data, run the full pipeline
against a real Postgres+pgvector instance, and confirm graph + vector
retrieval both work as expected.

Requires Postgres to be reachable (see README: `docker compose up -d`).
Automatically skipped if it isn't.
"""
from lld_step2.embedding import find_similar_chunks
from lld_step2.graph_loader import (
    get_direct_callees,
    get_direct_callers,
    get_reachable_within_hops,
    get_tables_used,
)
from lld_step2.mock_generator import generate, render_extraction_file
from lld_step2.pipeline import run_pipeline


def _write_mock_file(tmp_path, count=18, seed=7):
    text, specs = generate(count=count, seed=seed)
    path = tmp_path / "mock_extraction.txt"
    path.write_text(text, encoding="utf-8")
    return path, specs


def test_pipeline_loads_expected_object_call_and_table_counts(tmp_path, db_conn):
    path, specs = _write_mock_file(tmp_path)

    summary = run_pipeline(str(path), conn=db_conn)

    assert summary["object_count"] == len(specs)
    assert set(summary["object_names"]) == {s.name for s in specs}

    expected_calls = sum(len(s.calls) for s in specs)
    expected_tables = sum(len(s.tables_used) for s in specs)
    assert summary["call_count"] == expected_calls
    assert summary["table_usage_count"] == expected_tables

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM objects")
        assert cur.fetchone()[0] == len(specs)

        cur.execute("SELECT COUNT(*) FROM object_calls")
        assert cur.fetchone()[0] == expected_calls

        cur.execute("SELECT COUNT(*) FROM object_uses_table")
        assert cur.fetchone()[0] == expected_tables

        cur.execute("SELECT COUNT(*) FROM code_chunks")
        assert cur.fetchone()[0] == summary["chunk_count"]
        assert summary["chunk_count"] > 0


def test_pipeline_spot_checks_specific_object_edges(tmp_path, db_conn):
    path, specs = _write_mock_file(tmp_path)
    run_pipeline(str(path), conn=db_conn)

    spec_by_name = {s.name: s for s in specs}

    # Pick an object that has at least one outgoing call to a local object.
    source_spec = next(
        s for s in specs if any(c in spec_by_name for c in s.calls)
    )
    local_targets = sorted(c for c in source_spec.calls if c in spec_by_name)

    callees = get_direct_callees(db_conn, source_spec.name)
    assert set(local_targets) <= set(callees)

    for target in local_targets:
        callers = get_direct_callers(db_conn, target)
        assert source_spec.name in callers

    tables = get_tables_used(db_conn, source_spec.name)
    expected_table_names = {t for t, _fields in source_spec.tables_used}
    assert {t["table"] for t in tables} == expected_table_names


def test_rerunning_pipeline_does_not_duplicate_rows(tmp_path, db_conn):
    path, specs = _write_mock_file(tmp_path)

    run_pipeline(str(path), conn=db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM objects")
        first_object_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM object_calls")
        first_call_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM code_chunks")
        first_chunk_count = cur.fetchone()[0]

    run_pipeline(str(path), conn=db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM objects")
        assert cur.fetchone()[0] == first_object_count
        cur.execute("SELECT COUNT(*) FROM object_calls")
        assert cur.fetchone()[0] == first_call_count
        cur.execute("SELECT COUNT(*) FROM code_chunks")
        assert cur.fetchone()[0] == first_chunk_count


def test_multi_hop_traversal_matches_known_chain(tmp_path, db_conn):
    path, specs = _write_mock_file(tmp_path)
    run_pipeline(str(path), conn=db_conn)

    spec_by_name = {s.name: s for s in specs}

    # Find an object with a callee that itself has a local callee (2-hop chain).
    start = None
    hop1 = None
    hop2 = None
    for s in specs:
        for c1 in s.calls:
            if c1 in spec_by_name:
                for c2 in spec_by_name[c1].calls:
                    if c2 in spec_by_name and c2 != s.name:
                        start, hop1, hop2 = s.name, c1, c2
                        break
            if start:
                break
        if start:
            break

    assert start is not None, "mock data should contain at least one 2-hop chain"

    reachable_1hop = {r["object"] for r in get_reachable_within_hops(db_conn, start, 1)}
    assert hop1 in reachable_1hop

    reachable_2hop = {r["object"] for r in get_reachable_within_hops(db_conn, start, 2)}
    assert hop1 in reachable_2hop
    assert hop2 in reachable_2hop


def test_similarity_search_ranks_by_distance_and_returns_raw_text(tmp_path, db_conn):
    path, specs = _write_mock_file(tmp_path)
    run_pipeline(str(path), conn=db_conn)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM code_chunks ORDER BY id LIMIT 1")
        chunk_id = cur.fetchone()[0]

    results = find_similar_chunks(db_conn, chunk_id, top_k=5)
    assert 0 < len(results) <= 5

    distances = [r["distance"] for r in results]
    assert distances == sorted(distances)

    for r in results:
        assert isinstance(r["chunk_text"], str) and r["chunk_text"].strip()
        assert r["id"] != chunk_id
