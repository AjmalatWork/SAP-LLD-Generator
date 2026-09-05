"""Parser for the extraction file format.

Format (one or more blocks concatenated):

    === OBJECT: <NAME> ===
    TYPE: <CLASS|PROGRAM|FUNCTION_MODULE>
    PACKAGE: <PACKAGE_NAME>

    --- SOURCE ---
    <raw source, arbitrary number of lines>

    --- DEPENDENCIES ---
    <single JSON object: {"calls": [...], "tables_used": [{"table": ..., "fields": [...]}]}>
    === END OBJECT ===

This module does not know or care about ABAP syntax. It only understands the
delimiters above. Minor formatting variation (extra blank lines, trailing
whitespace) is tolerated; missing/malformed delimiters raise ParseError rather
than being silently skipped, since a malformed object here likely means the
upstream extractor changed format.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterator


class ParseError(ValueError):
    """Raised when the extraction file does not match the expected format."""


@dataclass
class TableUsage:
    table: str
    fields: list[str] = field(default_factory=list)


@dataclass
class ParsedObject:
    name: str
    type: str
    package: str
    source: str
    calls: list[str]
    tables_used: list[TableUsage]


_OBJECT_HEADER_RE = re.compile(r"^=== OBJECT:\s*(?P<name>\S+)\s*===\s*$")
_END_OBJECT_RE = re.compile(r"^=== END OBJECT ===\s*$")
_TYPE_RE = re.compile(r"^TYPE:\s*(?P<type>\S+)\s*$")
_PACKAGE_RE = re.compile(r"^PACKAGE:\s*(?P<package>\S+)\s*$")
_SOURCE_MARKER = "--- SOURCE ---"
_DEPENDENCIES_MARKER = "--- DEPENDENCIES ---"

_VALID_TYPES = {"CLASS", "PROGRAM", "FUNCTION_MODULE"}


def parse_extraction_file(text: str) -> Iterator[ParsedObject]:
    """Yield one ParsedObject per `=== OBJECT ===` block found in `text`.

    Raises ParseError on the first structurally malformed block.
    """
    lines = text.splitlines()
    i = 0
    n = len(lines)
    found_any = False

    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        header_match = _OBJECT_HEADER_RE.match(line.strip())
        if not header_match:
            raise ParseError(
                f"Expected '=== OBJECT: <NAME> ===' at line {i + 1}, got: {line!r}"
            )
        found_any = True
        name = header_match.group("name")
        i += 1

        obj_type, i = _consume_field(lines, i, _TYPE_RE, "type", name)
        if obj_type not in _VALID_TYPES:
            raise ParseError(
                f"Object {name!r}: TYPE must be one of {sorted(_VALID_TYPES)}, "
                f"got {obj_type!r}"
            )

        package, i = _consume_field(lines, i, _PACKAGE_RE, "package", name)

        i = _skip_blank(lines, i)
        i = _expect_marker(lines, i, _SOURCE_MARKER, name)

        source_lines: list[str] = []
        while i < n and lines[i].strip() != _DEPENDENCIES_MARKER:
            if _END_OBJECT_RE.match(lines[i].strip()) or _OBJECT_HEADER_RE.match(
                lines[i].strip()
            ):
                raise ParseError(
                    f"Object {name!r}: reached {lines[i].strip()!r} before "
                    f"'{_DEPENDENCIES_MARKER}'"
                )
            source_lines.append(lines[i])
            i += 1
        if i >= n:
            raise ParseError(
                f"Object {name!r}: missing '{_DEPENDENCIES_MARKER}' section"
            )
        source_text = "\n".join(source_lines).strip("\n")
        i += 1  # consume the DEPENDENCIES marker line

        json_lines: list[str] = []
        while i < n and not _END_OBJECT_RE.match(lines[i].strip()):
            json_lines.append(lines[i])
            i += 1
        if i >= n:
            raise ParseError(f"Object {name!r}: missing '=== END OBJECT ===' line")
        json_blob = "\n".join(json_lines).strip()
        if not json_blob:
            raise ParseError(
                f"Object {name!r}: '{_DEPENDENCIES_MARKER}' section is empty"
            )
        try:
            deps = json.loads(json_blob)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"Object {name!r}: DEPENDENCIES is not valid JSON: {exc}"
            ) from exc

        calls, tables_used = _validate_dependencies(deps, name)

        i += 1  # consume END OBJECT line

        yield ParsedObject(
            name=name,
            type=obj_type,
            package=package,
            source=source_text,
            calls=calls,
            tables_used=tables_used,
        )

    if not found_any:
        raise ParseError("No '=== OBJECT: ... ===' blocks found in input")


def _consume_field(lines, i, pattern, field_name, obj_name):
    n = len(lines)
    while i < n and not lines[i].strip():
        i += 1
    if i >= n:
        raise ParseError(f"Object {obj_name!r}: missing {field_name.upper()} field")
    m = pattern.match(lines[i].strip())
    if not m:
        raise ParseError(
            f"Object {obj_name!r}: expected {field_name.upper()} field, "
            f"got: {lines[i]!r}"
        )
    return m.group(field_name), i + 1


def _skip_blank(lines, i):
    n = len(lines)
    while i < n and not lines[i].strip():
        i += 1
    return i


def _expect_marker(lines, i, marker, obj_name):
    if i >= len(lines) or lines[i].strip() != marker:
        got = lines[i].strip() if i < len(lines) else "<end of file>"
        raise ParseError(f"Object {obj_name!r}: expected {marker!r}, got: {got!r}")
    return i + 1


def _validate_dependencies(deps, obj_name):
    if not isinstance(deps, dict):
        raise ParseError(f"Object {obj_name!r}: DEPENDENCIES JSON must be an object")

    calls = deps.get("calls", [])
    if not isinstance(calls, list) or not all(isinstance(c, str) for c in calls):
        raise ParseError(f"Object {obj_name!r}: 'calls' must be a list of strings")

    tables_used_raw = deps.get("tables_used", [])
    if not isinstance(tables_used_raw, list):
        raise ParseError(f"Object {obj_name!r}: 'tables_used' must be a list")

    tables_used: list[TableUsage] = []
    for entry in tables_used_raw:
        if not isinstance(entry, dict) or "table" not in entry:
            raise ParseError(
                f"Object {obj_name!r}: each 'tables_used' entry needs a 'table' key"
            )
        fields_raw = entry.get("fields", [])
        if not isinstance(fields_raw, list) or not all(
            isinstance(f, str) for f in fields_raw
        ):
            raise ParseError(
                f"Object {obj_name!r}: 'fields' for table {entry.get('table')!r} "
                "must be a list of strings"
            )
        tables_used.append(TableUsage(table=entry["table"], fields=fields_raw))

    return calls, tables_used
