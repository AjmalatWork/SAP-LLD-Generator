"""Tests for the real-format dependency loading and FULL/INCREMENTAL loader
semantics introduced by the step 2 real-format update. This is the part
with genuine new risk per the brief: step 2 had previously only ever been
tested against a single full load, re-run twice to check idempotency - never
against an incremental load that's supposed to leave most existing data
untouched while updating a subset and deleting another subset.
"""
from lld_step2.graph_loader import get_tables_used
from lld_step2.mock_generator import _LLD_TYPE_TO_DEPENDENCY_CODE, generate, generate_incremental
from lld_step2.pipeline import run_pipeline


def test_om_and_intf_dependencies_load_with_correct_edge_kind(tmp_path, db_conn):
    text, specs = generate(count=18, seed=42)
    path = tmp_path / "full.txt"
    path.write_text(text, encoding="utf-8")
    run_pipeline(str(path), conn=db_conn)

    om_source = next(s for s in specs if any(d.type == "OM" for d in s.dependencies))
    om_dep = next(d for d in om_source.dependencies if d.type == "OM")
    intf_source = next(s for s in specs if any(d.type == "INTF" for d in s.dependencies))
    intf_dep = next(d for d in intf_source.dependencies if d.type == "INTF")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT edge_kind, signature_json FROM object_calls "
            "WHERE source_object = %s AND target_object = %s AND dependency_type = 'OM'",
            (om_source.name, om_dep.name),
        )
        row = cur.fetchone()
        assert row is not None, "expected an OM-type object_calls row"
        assert row[0] == "calls_method"
        assert row[1] is not None  # OM carries a signature in the mock data

        cur.execute(
            "SELECT edge_kind, signature_json FROM object_calls "
            "WHERE source_object = %s AND target_object = %s AND dependency_type = 'INTF'",
            (intf_source.name, intf_dep.name),
        )
        row = cur.fetchone()
        assert row is not None, "expected an INTF-type object_calls row"
        assert row[0] == "implements"
        assert row[1] is None  # INTF never carries a signature


def test_table_reference_resolves_against_shared_ddic_objects(tmp_path, db_conn):
    text, specs = generate(count=18, seed=42)
    path = tmp_path / "full.txt"
    path.write_text(text, encoding="utf-8")
    run_pipeline(str(path), conn=db_conn)

    source = next(s for s in specs if any(t == "TABL" for t, _n in s.ddic_refs))
    table_name = next(n for t, n in source.ddic_refs if t == "TABL")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT detail_json FROM ddic_objects WHERE ddic_type = 'TABL' AND name = %s",
            (table_name,),
        )
        row = cur.fetchone()
        assert row is not None, "expected a ddic_objects row for the referenced table"
        assert "FIELDS" in row[0]

    tables = get_tables_used(db_conn, source.name)
    match = next(t for t in tables if t["table"] == table_name)
    assert match["fields"], "expected get_tables_used to resolve field names via ddic_objects"


def test_full_run_wipes_and_reloads_scoped_to_package(tmp_path, db_conn):
    text, specs = generate(count=18, seed=42, package="ZPKG_A")
    path = tmp_path / "full_a.txt"
    path.write_text(text, encoding="utf-8")
    run_pipeline(str(path), conn=db_conn)

    text_b, specs_b = generate(count=5, seed=99, package="ZPKG_B")
    path_b = tmp_path / "full_b.txt"
    path_b.write_text(text_b, encoding="utf-8")
    run_pipeline(str(path_b), conn=db_conn)

    # A second FULL run of package A must not touch package B's rows.
    run_pipeline(str(path), conn=db_conn)

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM objects WHERE package = 'ZPKG_B'")
        assert cur.fetchone()[0] == len(specs_b)
        cur.execute("SELECT COUNT(*) FROM objects WHERE package = 'ZPKG_A'")
        assert cur.fetchone()[0] == len(specs)


def test_incremental_run_deletes_removed_updates_target_leaves_others_untouched(tmp_path, db_conn):
    full_text, specs = generate(count=18, seed=42)
    full_path = tmp_path / "full.txt"
    full_path.write_text(full_text, encoding="utf-8")
    run_pipeline(str(full_path), conn=db_conn)

    updated_index, removed_index = 1, 2
    updated_name_preview = specs[updated_index].name
    removed_name_preview = specs[removed_index].name
    untouched_names = [
        s.name for i, s in enumerate(specs) if i not in (updated_index, removed_index)
    ]

    def snapshot(names):
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT name, type, package FROM objects WHERE name = ANY(%s) ORDER BY name",
                (names,),
            )
            objects_snap = cur.fetchall()
            # Exclude edges targeting the object about to be removed - those
            # are LEGITIMATELY cleaned up per the brief even from otherwise-
            # untouched sources, so they must not count as a violation of
            # "untouched" below.
            cur.execute(
                "SELECT source_object, target_object, dependency_type, edge_kind, signature_json "
                "FROM object_calls WHERE source_object = ANY(%s) AND target_object != %s "
                "ORDER BY source_object, target_object, dependency_type",
                (names, removed_name_preview),
            )
            calls_snap = cur.fetchall()
            cur.execute(
                "SELECT object_name, ddic_object_name, ddic_type FROM object_uses_table "
                "WHERE object_name = ANY(%s) ORDER BY object_name, ddic_object_name",
                (names,),
            )
            uses_table_snap = cur.fetchall()
            cur.execute(
                "SELECT object_name, chunk_index, chunk_text, embedding::text FROM code_chunks "
                "WHERE object_name = ANY(%s) ORDER BY object_name, chunk_index",
                (names,),
            )
            chunks_snap = cur.fetchall()
        return objects_snap, calls_snap, uses_table_snap, chunks_snap

    before = snapshot(untouched_names)

    inc_text, updated_spec, removed_spec = generate_incremental(
        specs, seed=43, updated_index=updated_index, removed_index=removed_index
    )
    assert updated_spec.name == updated_name_preview
    assert removed_spec.name == removed_name_preview

    inc_path = tmp_path / "incremental.txt"
    inc_path.write_text(inc_text, encoding="utf-8")
    summary = run_pipeline(str(inc_path), conn=db_conn)

    assert summary["run_type"] == "INCREMENTAL"
    assert summary["object_names"] == [updated_spec.name]
    expected_removed_code = _LLD_TYPE_TO_DEPENDENCY_CODE[removed_spec.type]
    assert summary["removed_objects"] == [(expected_removed_code, removed_spec.name)]

    # Removed object is fully gone.
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM objects WHERE name = %s", (removed_spec.name,))
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT COUNT(*) FROM object_calls WHERE source_object = %s OR target_object = %s",
            (removed_spec.name, removed_spec.name),
        )
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT COUNT(*) FROM code_chunks WHERE object_name = %s", (removed_spec.name,))
        assert cur.fetchone()[0] == 0

    # Updated object reflects the new dependency count from the incremental file.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM object_calls WHERE source_object = %s", (updated_spec.name,)
        )
        assert cur.fetchone()[0] == len(updated_spec.dependencies)

    # Every object not mentioned in the incremental file (and not removed)
    # is byte-identical to its pre-incremental-run snapshot.
    after = snapshot(untouched_names)
    assert after == before
