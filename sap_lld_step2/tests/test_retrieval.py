"""Tests for step 3 retrieval, run against ZORDER_MGMT_extract.txt (preferred
over the synthetic mock generator per the brief, since its graph structure is
already independently validated).

Per the labeling-mistake finding from the embedding investigation: every
"this requirement should match object X" expectation here is justified by a
direct query against object_calls/object_uses_table in the test itself, not
by eyeballing what "looks related."
"""
from pathlib import Path

import pytest

from lld_step2.graph_loader import CALL_LIKE_EDGE_KINDS
from lld_step2.mock_generator import generate
from lld_step2.parser import parse_extraction_file
from lld_step2.pipeline import run_pipeline
from lld_step2.retrieval import (
    extract_object_blocks,
    export_candidates_to_file,
    retrieve,
)

ZORDER_FILE = Path(__file__).parent.parent / "data" / "ZORDER_MGMT_extract.txt"

# Reused verbatim from the scoring-fix calibration (see
# reports/step3_scoring_fix_report.md) so these tests exercise the same
# inputs that surfaced and then validated the seed-floor fix.
NONSENSE_REQUIREMENTS = [
    "asdkj alksjd laksjd laksjdlk ajsdlkaj sdlkaj sdlkj",
    "xyzzy plugh frobnicate qux wibble wobble",
    "Migrate the mainframe COBOL payroll batch job to a quantum computing cluster",
]


@pytest.fixture(scope="module")
def zorder_conn(db_config):
    import psycopg

    try:
        conn = psycopg.connect(db_config.database_url, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable at {db_config.database_url}: {exc}")
        return

    # Autocommit, not the psycopg default (autocommit=False): this connection
    # is module-scoped and stays open across every read-only test in this
    # file. Without autocommit, each SELECT starts an implicit transaction
    # that's never explicitly committed, so the connection accumulates one
    # long-lived open transaction holding shared locks for the whole
    # module's run - which then deadlocks any other test in this file that
    # needs an exclusive lock (e.g. a TRUNCATE via the function-scoped
    # db_conn fixture, used by the cross-package scoping test below): that
    # TRUNCATE can't proceed until zorder_conn's transaction ends, but
    # zorder_conn's fixture teardown can't run until every test in the
    # module (including the blocked one) finishes. Found the hard way via a
    # genuine multi-minute hang, not a theoretical concern.
    conn.autocommit = True

    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE object_calls, object_uses_table, code_chunks, ddic_objects, objects CASCADE"
        )

    run_pipeline(str(ZORDER_FILE), conn=conn)

    yield conn
    conn.close()


def _bidirectional_neighbors(conn, object_name: str) -> set[str]:
    # Matches retrieval.py's own _fetch_bidirectional_adjacency: restricted
    # to CALL_LIKE_EDGE_KINDS, since object_calls now also holds DDIC/
    # message/transaction-type references (the real extraction format
    # loads every dependency type there) that are not "neighbors" for
    # certain-filter/structural-scoring purposes.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT target_object FROM object_calls WHERE source_object = %s AND edge_kind = ANY(%s) "
            "UNION SELECT source_object FROM object_calls WHERE target_object = %s AND edge_kind = ANY(%s)",
            (object_name, list(CALL_LIKE_EDGE_KINDS), object_name, list(CALL_LIKE_EDGE_KINDS)),
        )
        return {r[0] for r in cur.fetchall()}


def _objects_using_table(conn, table_name: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT object_name FROM object_uses_table WHERE ddic_object_name = %s AND ddic_type = 'TABL'",
            (table_name,),
        )
        return {r[0] for r in cur.fetchall()}


# =============================================================================
# Input validation
# =============================================================================


def test_scoping_note_requires_confidence(zorder_conn):
    with pytest.raises(ValueError):
        retrieve(
            "some requirement",
            packages=["ZORDER_MGMT"],
            scoping_note="ZCL_ORDER_LOGGER",
            confidence=None,
            conn=zorder_conn,
        )


def test_confidence_requires_scoping_note(zorder_conn):
    with pytest.raises(ValueError):
        retrieve(
            "some requirement",
            packages=["ZORDER_MGMT"],
            scoping_note=None,
            confidence="certain",
            conn=zorder_conn,
        )


# =============================================================================
# `packages` is mandatory and hard-fails on an unknown package
# =============================================================================


def test_packages_is_required_and_rejects_empty_list(zorder_conn):
    with pytest.raises(ValueError):
        retrieve("some requirement", packages=[], conn=zorder_conn)


def test_unknown_package_is_a_hard_error(zorder_conn):
    with pytest.raises(ValueError, match="Unknown package"):
        retrieve("some requirement", packages=["ZTOTALLY_MADE_UP_PACKAGE"], conn=zorder_conn)


def test_unknown_package_in_mixed_list_is_still_a_hard_error(zorder_conn):
    # A real package alongside a typo'd one must still fail loudly, not
    # silently fall back to searching only the valid one.
    with pytest.raises(ValueError, match="Unknown package"):
        retrieve(
            "some requirement",
            packages=["ZORDER_MGMT", "ZTOTALLY_MADE_UP_PACKAGE"],
            conn=zorder_conn,
        )


# =============================================================================
# 1. A requirement that clearly matches one object ranks it first
# =============================================================================


def test_clear_match_ranks_top_object_first(zorder_conn):
    # Ground truth: ZORDER_LOG is used by exactly one object (graph fact, not
    # a judgment call) -- a requirement squarely about purging that log
    # should be unambiguous.
    users_of_zorder_log = _objects_using_table(zorder_conn, "ZORDER_LOG")
    assert users_of_zorder_log == {"ZCL_ORDER_LOGGER"}

    result = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
        packages=["ZORDER_MGMT"],
        conn=zorder_conn,
    )
    assert result.candidates[0].object_name == "ZCL_ORDER_LOGGER"
    assert result.unadjusted_ranking[0].object_name == "ZCL_ORDER_LOGGER"


# =============================================================================
# 2. `certain` filtering excludes out-of-scope objects
# =============================================================================


def test_certain_filtering_excludes_out_of_scope_objects(zorder_conn):
    neighbors = _bidirectional_neighbors(zorder_conn, "ZCL_TAX_CALCULATOR")
    allowed = neighbors | {"ZCL_TAX_CALCULATOR"}
    assert allowed == {"ZCL_TAX_CALCULATOR", "ZCL_PRICING_ENGINE"}  # graph fact, recount visible above

    result = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
        packages=["ZORDER_MGMT"],
        scoping_note="ZCL_TAX_CALCULATOR",
        confidence="certain",
        conn=zorder_conn,
    )
    returned_names = {c.object_name for c in result.candidates}
    assert returned_names <= allowed
    assert returned_names  # non-empty
    assert "ZCL_ORDER_LOGGER" not in returned_names  # clearly out of scope, must be excluded


# =============================================================================
# 3. `likely` boosting can be overridden by a clearly better independent match
# =============================================================================


def test_likely_boost_overridden_by_better_independent_match(zorder_conn):
    unadjusted = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
        packages=["ZORDER_MGMT"],
        conn=zorder_conn,
    )
    independent_top = unadjusted.candidates[0].object_name
    assert independent_top == "ZCL_ORDER_LOGGER"

    result = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
        packages=["ZORDER_MGMT"],
        scoping_note="ZCL_TAX_CALCULATOR",
        confidence="likely",
        conn=zorder_conn,
    )
    # The hint should NOT win -- the independent match is far stronger.
    assert result.candidates[0].object_name == independent_top
    assert result.candidates[0].object_name != "ZCL_TAX_CALCULATOR"


# =============================================================================
# 4. A certain-hint conflict is correctly flagged
# =============================================================================


def test_certain_hint_conflict_flagged(zorder_conn):
    result = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
        packages=["ZORDER_MGMT"],
        scoping_note="ZCL_TAX_CALCULATOR",
        confidence="certain",
        conn=zorder_conn,
    )
    conflicts = [d for d in result.disagreements if d.kind == "certain_hint_conflict"]
    assert len(conflicts) == 1
    assert "ZCL_ORDER_LOGGER" in conflicts[0].message

    # Recompute the margin independently from returned data, not trusting
    # the flag's own message.
    best_in_filter = max(c.base_score for c in result.candidates)
    top_unadjusted = result.unadjusted_ranking[0]
    assert top_unadjusted.object_name == "ZCL_ORDER_LOGGER"
    assert (top_unadjusted.base_score - best_in_filter) > 0.15


# =============================================================================
# 5. An unresolvable scoping note produces a warning, not a crash
# =============================================================================


def test_unresolvable_scoping_note_warns_not_crashes(zorder_conn):
    result = retrieve(
        "Add a purge job for old log entries",
        packages=["ZORDER_MGMT"],
        scoping_note="ZCL_TOTALLY_MADE_UP_OBJECT_9000",
        confidence="certain",
        conn=zorder_conn,
    )
    unresolved = [d for d in result.disagreements if d.kind == "unresolvable_scoping_note"]
    assert len(unresolved) == 1
    assert result.query_metadata["resolved_named_objects"] == []
    # Falls back to unrestricted ranking rather than an empty/broken result.
    assert result.candidates


# =============================================================================
# 6. likely_new_object fires on requirements unrelated to the package,
#    and does not fire on requirements grounded in known object behavior.
# =============================================================================


@pytest.mark.parametrize("requirement", NONSENSE_REQUIREMENTS)
def test_likely_new_object_fires_on_unrelated_requirement(zorder_conn, requirement):
    result = retrieve(requirement, packages=["ZORDER_MGMT"], conn=zorder_conn)
    assert result.likely_new_object is True
    assert result.likely_new_object_reason


@pytest.mark.parametrize(
    "requirement,expected_top",
    [
        (
            "Check whether a customer has exceeded their credit limit before allowing a large order",
            "ZCL_ORDER_VALIDATOR",
        ),
        (
            "Calculate the shipping cost for an order based on customer location zone and item count",
            "ZCL_SHIPPING_CALCULATOR",
        ),
        (
            "Apply a customer tier based percentage discount to the gross order amount",
            "ZCL_DISCOUNT_CALCULATOR",
        ),
    ],
)
def test_likely_new_object_false_on_grounded_requirement(zorder_conn, requirement, expected_top):
    result = retrieve(requirement, packages=["ZORDER_MGMT"], conn=zorder_conn)
    assert result.likely_new_object is False
    assert result.candidates[0].object_name == expected_top


# =============================================================================
# 6b. `packages` actually scopes the search when multiple packages are loaded
# =============================================================================


def test_packages_excludes_objects_outside_given_scope(tmp_path, db_conn):
    # db_conn (unlike zorder_conn) starts truncated per-test, so this test can
    # load a second, unrelated package into the same database without
    # disturbing the module-scoped ZORDER_MGMT fixture used everywhere else.
    order_path = tmp_path / "order.txt"
    order_path.write_text(ZORDER_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    run_pipeline(str(order_path), conn=db_conn)

    other_text, other_specs = generate(count=6, seed=7, package="ZMOCK_OTHER")
    other_path = tmp_path / "other.txt"
    other_path.write_text(other_text, encoding="utf-8")
    run_pipeline(str(other_path), conn=db_conn)

    with db_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT package FROM objects ORDER BY package")
        loaded_packages = {r[0] for r in cur.fetchall()}
    assert loaded_packages == {"ZORDER_MGMT", "ZMOCK_OTHER"}

    # Same requirement that correctly matches ZCL_ORDER_LOGGER package-wide
    # (see test_clear_match_ranks_top_object_first) but scoped to the OTHER
    # package only -- must never surface a ZORDER_MGMT object, however well
    # it would otherwise score.
    result = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
        packages=["ZMOCK_OTHER"],
        conn=db_conn,
    )
    returned_packages = {c.package for c in result.candidates}
    assert returned_packages <= {"ZMOCK_OTHER"}
    returned_names = {c.object_name for c in result.candidates}
    assert "ZCL_ORDER_LOGGER" not in returned_names

    # Scoped to ZORDER_MGMT only, behaves exactly as the unscoped-but-single-
    # package tests above already prove.
    result_scoped = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
        packages=["ZORDER_MGMT"],
        conn=db_conn,
    )
    assert result_scoped.candidates[0].object_name == "ZCL_ORDER_LOGGER"
    assert all(c.package == "ZORDER_MGMT" for c in result_scoped.candidates)

    # Given both packages, either may appear -- the point is only that scope
    # is a strict superset/subset relationship, not that it changes the
    # ranking outcome here.
    result_both = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
        packages=["ZORDER_MGMT", "ZMOCK_OTHER"],
        conn=db_conn,
    )
    assert result_both.candidates[0].object_name == "ZCL_ORDER_LOGGER"


# =============================================================================
# 7. Exported file parses correctly with the step-2 parser and carries the
#    flag header
# =============================================================================


def test_export_parses_with_step2_parser_and_contains_flag_header(zorder_conn, tmp_path):
    result = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
        packages=["ZORDER_MGMT"],
        scoping_note="ZCL_TAX_CALCULATOR",
        confidence="certain",
        conn=zorder_conn,
    )
    assert result.disagreements  # sanity: this scenario does produce a flag

    out_path = tmp_path / "candidates.txt"
    export_candidates_to_file(result, str(out_path), conn=zorder_conn)
    full_text = out_path.read_text(encoding="utf-8")

    # Header must be present and must carry the actual flag content, not just
    # exist as an empty shell.
    assert "=== RETRIEVAL METADATA ===" in full_text
    assert "=== END RETRIEVAL METADATA ===" in full_text
    assert "certain_hint_conflict" in full_text
    assert "LIKELY_NEW_OBJECT:" in full_text

    objects_text = extract_object_blocks(full_text)
    assert objects_text, "expected a real extraction-file header + === OBJECT: === blocks"

    extraction = parse_extraction_file(objects_text)
    assert extraction.run_type == "INCREMENTAL"
    parsed_names = {p.name for p in extraction.objects}
    assert parsed_names == {c.object_name for c in result.candidates}


# =============================================================================
# 7b. Exported per-candidate scores match retrieve()'s own computed values,
#     and the parser tolerates their presence without choking on them.
# =============================================================================


def test_export_score_fields_match_retrieve_computed_values(zorder_conn, tmp_path):
    result = retrieve(
        "Calculate the shipping cost for an order based on customer location zone "
        "and item count",
        packages=["ZORDER_MGMT"],
        scoping_note="ZCL_STOCK_MANAGER",
        confidence="likely",
        conn=zorder_conn,
    )
    assert result.candidates  # sanity

    out_path = tmp_path / "candidates_with_scores.txt"
    export_candidates_to_file(result, str(out_path), conn=zorder_conn)
    full_text = out_path.read_text(encoding="utf-8")

    for candidate in result.candidates:
        block_start = full_text.index(f"=== OBJECT: {candidate.object_name} ===")
        block_end = full_text.index("--- SOURCE ---", block_start)
        header = full_text[block_start:block_end]

        # Exact string match against the same f"{:.4f}" formatting the
        # exporter uses -- not just "parses to a close-enough float" --
        # since the point is verifying the artifact reflects retrieve()'s
        # real numbers, not merely that some number is present.
        assert f"FINAL_SCORE: {candidate.final_score:.4f}" in header
        assert f"SEMANTIC_SCORE: {candidate.semantic_score:.4f}" in header
        assert f"STRUCTURAL_SCORE: {candidate.structural_score:.4f}" in header
        assert f"TIER_ADJUSTMENT_APPLIED: {candidate.tier_adjustment}" in header

    # Round-trip: the parser must tolerate these new fields' presence
    # (skip, not choke), per the "parser doesn't need to use these fields
    # but must not break on them" requirement.
    objects_text = extract_object_blocks(full_text)
    extraction = parse_extraction_file(objects_text)
    assert {p.name for p in extraction.objects} == {c.object_name for c in result.candidates}


def test_top_n_and_unadjusted_ranking_sizes(zorder_conn):
    result = retrieve(
        "Calculate the shipping cost for an order",
        packages=["ZORDER_MGMT"],
        top_n=2,
        conn=zorder_conn,
    )
    assert len(result.candidates) <= 2
    assert len(result.unadjusted_ranking) <= 5


# =============================================================================
# Known issue tracking (see ISSUES.md #1) -- kept as a live, failing test
# rather than only a line in a report, so it stays visible on every test run
# instead of relying on someone remembering it.
# =============================================================================


@pytest.mark.xfail(
    reason=(
        "Weak semantic ranking: ZFM_ORDER_NUMBER_RANGE's own chunk (which literally "
        "calls NUMBER_GET_NEXT) scores semantic_score=0.675, while an unrelated chunk "
        "(ZCL_ORDER_PROCESSOR.build_item_list, a plain SELECT * FROM zorder_item) "
        "scores higher at 0.734 and ends up ranked first instead. The correct object "
        "still surfaces at rank 4 of 5, so this is a ranking-precision issue, not a "
        "total retrieval miss. Documented in ISSUES.md #1 and "
        "reports/step3_scoring_fix_report.md. Do not 'fix' this by tweaking weights "
        "for this one case -- that's the exact ad hoc pattern the embedding "
        "investigation already found harmful; a real fix needs another proper "
        "benchmark run, ideally against a larger real extraction."
    ),
    strict=True,
)
def test_order_numbering_semantic_ranking_miss(zorder_conn):
    # Ground truth: ZFM_ORDER_NUMBER_RANGE is the only object in the package
    # whose calls include NUMBER_GET_NEXT (graph fact, not a judgment call).
    with zorder_conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT source_object FROM object_calls WHERE target_object = 'NUMBER_GET_NEXT'"
        )
        callers_of_number_get_next = {r[0] for r in cur.fetchall()}
    assert callers_of_number_get_next == {"ZFM_ORDER_NUMBER_RANGE"}

    result = retrieve(
        "Generate the next order number using a number range object",
        packages=["ZORDER_MGMT"],
        conn=zorder_conn,
    )
    assert result.candidates[0].object_name == "ZFM_ORDER_NUMBER_RANGE"
