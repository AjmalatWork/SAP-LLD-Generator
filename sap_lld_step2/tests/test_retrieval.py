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

    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE object_calls, object_uses_table, code_chunks, ddic_objects, objects CASCADE"
        )
    conn.commit()

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
        retrieve("some requirement", scoping_note="ZCL_ORDER_LOGGER", confidence=None, conn=zorder_conn)


def test_confidence_requires_scoping_note(zorder_conn):
    with pytest.raises(ValueError):
        retrieve("some requirement", scoping_note=None, confidence="certain", conn=zorder_conn)


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
        conn=zorder_conn,
    )
    independent_top = unadjusted.candidates[0].object_name
    assert independent_top == "ZCL_ORDER_LOGGER"

    result = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
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
    result = retrieve(requirement, conn=zorder_conn)
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
    result = retrieve(requirement, conn=zorder_conn)
    assert result.likely_new_object is False
    assert result.candidates[0].object_name == expected_top


# =============================================================================
# 7. Exported file parses correctly with the step-2 parser and carries the
#    flag header
# =============================================================================


def test_export_parses_with_step2_parser_and_contains_flag_header(zorder_conn, tmp_path):
    result = retrieve(
        "Add a purge job that deletes old log entries older than a given number "
        "of days from the order log table",
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


def test_top_n_and_unadjusted_ranking_sizes(zorder_conn):
    result = retrieve("Calculate the shipping cost for an order", top_n=2, conn=zorder_conn)
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
        "Generate the next order number using a number range object", conn=zorder_conn
    )
    assert result.candidates[0].object_name == "ZFM_ORDER_NUMBER_RANGE"
