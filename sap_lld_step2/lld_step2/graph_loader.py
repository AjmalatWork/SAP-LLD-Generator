"""Loads a parsed ExtractionFile into `objects` / `object_calls` /
`object_uses_table` / `ddic_objects`, and provides query helpers for
callers/callees/table-usage/traversal.

FULL vs INCREMENTAL semantics (see load_extraction_file):
  - FULL wipes and reloads everything scoped to the file's package.
  - INCREMENTAL processes REMOVED_OBJECTS first, then upserts only the
    objects actually present in the file - everything else in the database
    is left untouched. This is the part that needed real, tested logic
    rather than an assumption that "upsert-or-truncate" from the old
    single-shot design already handled it correctly - it did not, since
    step 2 had only ever been tested against single full loads before.
"""
from __future__ import annotations

import json
from typing import Iterable

import psycopg

from .parser import DDIC_REFERENCE_TYPES, DdicObject, DependencyEntry, ExtractionFile, ParsedObject

# Normalizes the open-ended raw dependency_type vocabulary (observed broader
# in real data than any single source document lists exhaustively - CLAS,
# DGT, DTEL, FUGR, FUNC, INCL, INTF, MESS, METH, MSAG, OA, OM, PROG, STRU,
# TABL, TRAN, TTYP, TYPE all seen in one real package) into a small set of
# semantically-equivalent groups traversal queries can use without
# enumerating every raw value. Confidence varies: METH/OM/FUNC/INCL/INTF/
# PROG/CLAS/FUGR are well-understood SAP repository object types; DGT and OA
# are best-effort guesses (SENVI-TYPE's own environment-scan vocabulary is
# broader than documented DDIC/TADIR object types) - anything not listed
# here, including future unseen values, falls back to DEFAULT_EDGE_KIND
# rather than raising an error.
EDGE_KIND_MAP = {
    "METH": "calls_method",
    "OM": "calls_method",
    "FUNC": "calls_function",
    "INCL": "includes",
    "INTF": "implements",
    "PROG": "program_ref",
    "CLAS": "class_ref",
    "FUGR": "function_group_ref",
    "DOMA": "domain_ref",
    "DTEL": "data_element_ref",
    "TABL": "table_ref",
    "TTYP": "table_type_ref",
    "STRU": "structure_ref",
    "MESS": "message_ref",
    "MSAG": "message_class_ref",
    "TRAN": "transaction_ref",
    "TYPE": "type_ref",
    "DGT": "generic_type_ref",  # low confidence - see module docstring
}
DEFAULT_EDGE_KIND = "reference"

# The edge_kinds that represent one object genuinely referencing another's
# code (as opposed to both objects merely referencing the same DDIC object,
# message class, transaction, etc.). Since the real extraction format loads
# EVERY dependency type into object_calls, consumers that care specifically
# about call-graph proximity (embedding_bench.py's "related" labeling,
# retrieval.py's 1-/2-hop structural scoring) filter to this set rather than
# treating any object_calls row as a "call."
CALL_LIKE_EDGE_KINDS = (
    "calls_method",
    "calls_function",
    "includes",
    "implements",
    "program_ref",
    "class_ref",
    "function_group_ref",
)


def load_extraction_file(conn: psycopg.Connection, extraction: ExtractionFile) -> None:
    """Applies one parsed extraction file's graph/DDIC data to the database,
    per its RUN_TYPE. Does not touch code_chunks/embeddings - that stays the
    caller's responsibility (see pipeline.py), same separation as before.
    """
    if extraction.run_type == "FULL":
        wipe_package(conn, extraction.package)
    else:
        delete_removed_objects(conn, extraction.removed_objects)

    upsert_objects(conn, extraction.objects)
    for obj in extraction.objects:
        load_object_dependencies(conn, obj)
    upsert_ddic_objects(conn, extraction.package, extraction.ddic_objects)
    conn.commit()


def wipe_package(conn: psycopg.Connection, package: str) -> None:
    """FULL run: delete every `objects` row for this package (cascades to
    object_calls-as-source, object_uses_table, and code_chunks via existing
    FK ON DELETE CASCADE), plus its ddic_objects rows.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM objects WHERE package = %s", (package,))
        cur.execute("DELETE FROM ddic_objects WHERE package = %s", (package,))


def delete_removed_objects(
    conn: psycopg.Connection, removed_objects: Iterable[tuple[str, str]]
) -> None:
    """INCREMENTAL run: process REMOVED_OBJECTS before loading anything new.
    Deleting the `objects` row cascades to object_calls-as-source,
    object_uses_table, and code_chunks. object_calls has no FK on
    target_object (by design, to allow unresolved external references), so
    rows where a removed object appears as a TARGET are cleaned up
    explicitly here too.
    """
    with conn.cursor() as cur:
        for _obj_type, obj_name in removed_objects:
            cur.execute("DELETE FROM object_calls WHERE target_object = %s", (obj_name,))
            cur.execute("DELETE FROM objects WHERE name = %s", (obj_name,))


def upsert_objects(conn: psycopg.Connection, objects: Iterable[ParsedObject]) -> None:
    with conn.cursor() as cur:
        for obj in objects:
            cur.execute(
                """
                INSERT INTO objects (name, type, package)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE
                    SET type = EXCLUDED.type, package = EXCLUDED.package
                """,
                (obj.name, obj.type, obj.package),
            )


def load_object_dependencies(conn: psycopg.Connection, obj: ParsedObject) -> None:
    """Replaces one object's object_calls and object_uses_table rows
    entirely from its parsed dependency list (delete-then-insert - simpler
    and safer than diffing individual rows, same approach step 2 already
    used for the old format). Every dependency becomes an object_calls
    edge; DOMA/DTEL/TABL/TTYP-typed ones additionally get an
    object_uses_table edge (STRU does not, per the brief).
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM object_calls WHERE source_object = %s", (obj.name,))
        cur.execute("DELETE FROM object_uses_table WHERE object_name = %s", (obj.name,))

        for dep in obj.dependencies:
            _insert_dependency(cur, obj.name, dep)


def _insert_dependency(cur, source_object: str, dep: DependencyEntry) -> None:
    edge_kind = EDGE_KIND_MAP.get(dep.type, DEFAULT_EDGE_KIND)
    signature_json = json.dumps(dep.signature) if dep.signature is not None else None

    cur.execute(
        """
        INSERT INTO object_calls
            (source_object, target_object, dependency_type, edge_kind, signature_json)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (source_object, target_object, dependency_type) DO UPDATE
            SET edge_kind = EXCLUDED.edge_kind, signature_json = EXCLUDED.signature_json
        """,
        (source_object, dep.name, dep.type, edge_kind, signature_json),
    )

    if dep.type in DDIC_REFERENCE_TYPES:
        cur.execute(
            """
            INSERT INTO object_uses_table (object_name, ddic_object_name, ddic_type)
            VALUES (%s, %s, %s)
            ON CONFLICT (object_name, ddic_object_name, ddic_type) DO NOTHING
            """,
            (source_object, dep.name, dep.type),
        )


def upsert_ddic_objects(
    conn: psycopg.Connection, package: str, ddic_objects: Iterable[DdicObject]
) -> None:
    """Upserts entries from a file's shared DDIC section. Never deletes -
    an incremental file's shared section may legitimately only cover the
    DDIC objects relevant to what changed, not the whole package, and that
    narrower scope must not be read as "everything else was removed."
    Only REMOVED_OBJECTS drives deletion, and it does not touch this table.
    """
    with conn.cursor() as cur:
        for d in ddic_objects:
            cur.execute(
                """
                INSERT INTO ddic_objects (package, ddic_type, name, detail_json)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (package, ddic_type, name) DO UPDATE
                    SET detail_json = EXCLUDED.detail_json
                """,
                (package, d.ddic_type, d.name, json.dumps(d.detail)),
            )


def get_direct_callers(conn: psycopg.Connection, object_name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT source_object FROM object_calls WHERE target_object = %s "
            "ORDER BY source_object",
            (object_name,),
        )
        return [row[0] for row in cur.fetchall()]


def get_direct_callees(conn: psycopg.Connection, object_name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT target_object FROM object_calls WHERE source_object = %s "
            "ORDER BY target_object",
            (object_name,),
        )
        return [row[0] for row in cur.fetchall()]


def get_tables_used(conn: psycopg.Connection, object_name: str) -> list[dict]:
    """Returns [{"table": ddic_object_name, "fields": [field names]}, ...]
    for an object's DOMA/DTEL/TABL/TTYP references. Field-level detail no
    longer lives on object_uses_table itself (see schema notes in
    sql/init.sql) - this joins through to ddic_objects.detail_json to
    recover it for TABL-type references (the only ddic_type whose detail
    shape has a meaningful field list); other ddic_types get an empty
    fields list. Missing DDIC detail (e.g. a standard SAP table never
    itself extracted via Z_GET_DDIC_INFO for this package) degrades to an
    empty fields list rather than an error.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ut.ddic_object_name, ut.ddic_type, dd.detail_json
            FROM object_uses_table ut
            JOIN objects o ON o.name = ut.object_name
            LEFT JOIN ddic_objects dd
                ON dd.package = o.package
               AND dd.ddic_type = ut.ddic_type
               AND dd.name = ut.ddic_object_name
            WHERE ut.object_name = %s
            ORDER BY ut.ddic_object_name
            """,
            (object_name,),
        )
        rows = cur.fetchall()

    result = []
    for name, ddic_type, detail_json in rows:
        fields: list[str] = []
        if ddic_type == "TABL" and detail_json:
            fields = [f["NAME"] for f in detail_json.get("FIELDS", []) if "NAME" in f]
        result.append({"table": name, "fields": fields})
    return result


def get_reachable_within_hops(
    conn: psycopg.Connection, object_name: str, max_hops: int
) -> list[dict]:
    """Recursive CTE: all objects reachable from `object_name` via
    object_calls edges (any dependency_type) within `max_hops` hops, with
    the hop distance at which each was first reached.
    """
    query = """
        WITH RECURSIVE reachable(target_object, hops) AS (
            SELECT DISTINCT target_object, 1
            FROM object_calls
            WHERE source_object = %(start)s

            UNION

            SELECT oc.target_object, r.hops + 1
            FROM object_calls oc
            JOIN reachable r ON oc.source_object = r.target_object
            WHERE r.hops < %(max_hops)s
        )
        SELECT target_object, MIN(hops) AS hops
        FROM reachable
        GROUP BY target_object
        ORDER BY hops, target_object
    """
    with conn.cursor() as cur:
        cur.execute(query, {"start": object_name, "max_hops": max_hops})
        return [{"object": row[0], "hops": row[1]} for row in cur.fetchall()]


def print_object_summary(conn: psycopg.Connection, object_name: str) -> None:
    callers = get_direct_callers(conn, object_name)
    callees = get_direct_callees(conn, object_name)
    tables = get_tables_used(conn, object_name)

    print(f"Object: {object_name}")
    print(f"  Direct callers ({len(callers)}): {callers}")
    print(f"  Direct callees ({len(callees)}): {callees}")
    print(f"  Tables used ({len(tables)}):")
    for t in tables:
        print(f"    - {t['table']}: {t['fields']}")
