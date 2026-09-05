"""Loads parsed objects into the `objects` / `object_calls` / `object_uses_table`
tables, and provides query helpers for callers/callees/table-usage/traversal.

Re-running the loader on the same input does not duplicate rows: each object's
own calls and table-usage rows are deleted and re-inserted (upsert-by-replace),
scoped to that object, on every load. `objects` rows are upserted by primary
key. This is simpler than diffing individual call/usage rows and gives the
same end state either way.
"""
from __future__ import annotations

import json
from typing import Iterable

import psycopg

from .parser import ParsedObject


def load_objects(conn: psycopg.Connection, objects: Iterable[ParsedObject]) -> None:
    objects = list(objects)
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

        for obj in objects:
            cur.execute("DELETE FROM object_calls WHERE source_object = %s", (obj.name,))
            for target in obj.calls:
                cur.execute(
                    """
                    INSERT INTO object_calls (source_object, target_object)
                    VALUES (%s, %s)
                    ON CONFLICT (source_object, target_object) DO NOTHING
                    """,
                    (obj.name, target),
                )

            cur.execute(
                "DELETE FROM object_uses_table WHERE object_name = %s", (obj.name,)
            )
            for usage in obj.tables_used:
                cur.execute(
                    """
                    INSERT INTO object_uses_table (object_name, table_name, fields)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (object_name, table_name) DO UPDATE
                        SET fields = EXCLUDED.fields
                    """,
                    (obj.name, usage.table, json.dumps(usage.fields)),
                )
    conn.commit()


def get_direct_callers(conn: psycopg.Connection, object_name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_object FROM object_calls WHERE target_object = %s ORDER BY source_object",
            (object_name,),
        )
        return [row[0] for row in cur.fetchall()]


def get_direct_callees(conn: psycopg.Connection, object_name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT target_object FROM object_calls WHERE source_object = %s ORDER BY target_object",
            (object_name,),
        )
        return [row[0] for row in cur.fetchall()]


def get_tables_used(conn: psycopg.Connection, object_name: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, fields FROM object_uses_table WHERE object_name = %s ORDER BY table_name",
            (object_name,),
        )
        return [{"table": row[0], "fields": row[1]} for row in cur.fetchall()]


def get_reachable_within_hops(
    conn: psycopg.Connection, object_name: str, max_hops: int
) -> list[dict]:
    """Recursive CTE: all objects reachable from `object_name` via `calls`
    edges within `max_hops` hops, with the hop distance at which each was
    first reached.
    """
    query = """
        WITH RECURSIVE reachable(target_object, hops) AS (
            SELECT target_object, 1
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
