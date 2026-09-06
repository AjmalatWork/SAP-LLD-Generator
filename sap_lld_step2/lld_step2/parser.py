"""Parser for the real LLD extraction file format produced by step 1
(ZLLD_PACKAGE_EXTRACTOR).

Format:

    RUN_TYPE: <FULL|INCREMENTAL>
    PACKAGE: <PACKAGE_NAME>
    EXTRACTED_AT: <timestamp>
    REMOVED_OBJECTS: <comma-separated "TYPE:NAME" list, or NONE>

    --- DDIC ---
    <single JSON blob: Z_GET_DDIC_INFO's real output, package-scoped>

    === OBJECT: <NAME> ===
    TYPE: <CLASS|PROGRAM|FUNCTION_MODULE>
    PACKAGE: <PACKAGE_NAME>

    --- SOURCE ---
    <raw source, arbitrary number of lines>

    --- DEPENDENCIES ---
    <single JSON blob: ZCR_GET_DEPENDENCY_OBJ_NEW's real output - a list
     containing exactly one entry with this object's own "DEPENDENCIES"
     array of typed entries>
    === END OBJECT ===

    (repeated for every object)

This supersedes the earlier simplified {"calls": [...], "tables_used": [...]}
mock format entirely - this parser does not attempt to also understand that
shape. See lld_step2/mock_generator.py for the fixture generator, updated to
emit this real shape.

This module does not know or care about ABAP syntax. It only understands the
delimiters and JSON shapes above. Minor formatting variation (extra blank
lines, trailing whitespace) is tolerated; missing/malformed delimiters raise
ParseError rather than being silently skipped, since a malformed file here
likely means the upstream extractor changed format.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Real dependency-entry TYPE values observed to carry a SIGNATURE blob in
# real extraction output. Everything else's signature is None (the source
# JSON omits the SIGNATURE key entirely for those, rather than sending an
# empty object) - this is a closed, small list because signature ENRICHMENT
# is a deliberate choice in ZCR_GET_OBJECT_SIGNATURE, unlike the dependency
# TYPE vocabulary itself (see DependencyEntry), which is open-ended.
_SIGNATURE_BEARING_TYPES = {"METH", "OM", "FUNC", "INCL", "PROG"}

# object_uses_table gets an edge only for these four ddic-reference types
# (per the brief) - STRU and everything else are plain object_calls edges.
DDIC_REFERENCE_TYPES = {"DOMA", "DTEL", "TABL", "TTYP"}

_VALID_RUN_TYPES = {"FULL", "INCREMENTAL"}
_VALID_OBJECT_TYPES = {"CLASS", "PROGRAM", "FUNCTION_MODULE"}

_OBJECT_HEADER_RE = re.compile(r"^=== OBJECT:\s*(?P<name>\S+)\s*===\s*$")
_END_OBJECT_RE = re.compile(r"^=== END OBJECT ===\s*$")
_TYPE_RE = re.compile(r"^TYPE:\s*(?P<type>\S+)\s*$")
_PACKAGE_RE = re.compile(r"^PACKAGE:\s*(?P<package>\S+)\s*$")
# Step 3's export (lld_step2.retrieval.export_candidates_to_file) writes
# these retrieval-metadata fields on an object header line - real step 1
# extraction never does. Tolerated (skipped, not parsed into ParsedObject)
# so an exported candidate file round-trips through this same parser
# without a special case; this parser never emits or requires them itself.
_RETRIEVAL_METADATA_FIELD_RE = re.compile(
    r"^(FINAL_SCORE|SEMANTIC_SCORE|STRUCTURAL_SCORE|TIER_ADJUSTMENT_APPLIED):.*$"
)
_RUN_TYPE_RE = re.compile(r"^RUN_TYPE:\s*(?P<run_type>\S+)\s*$")
_EXTRACTED_AT_RE = re.compile(r"^EXTRACTED_AT:\s*(?P<extracted_at>.*\S)\s*$")
_REMOVED_OBJECTS_RE = re.compile(r"^REMOVED_OBJECTS:\s*(?P<removed_objects>.*\S)\s*$")
_SOURCE_MARKER = "--- SOURCE ---"
_DEPENDENCIES_MARKER = "--- DEPENDENCIES ---"
_DDIC_MARKER = "--- DDIC ---"


class ParseError(ValueError):
    """Raised when the extraction file does not match the expected format."""


@dataclass
class DependencyEntry:
    type: str
    name: str
    signature: dict[str, Any] | None = None


@dataclass
class ParsedObject:
    name: str
    type: str
    package: str
    source: str
    dependencies: list[DependencyEntry]


@dataclass
class DdicObject:
    ddic_type: str  # DOMA / DTEL / TABL / STRU / TTYP
    name: str
    detail: dict[str, Any]


@dataclass
class ExtractionFile:
    run_type: str  # FULL | INCREMENTAL
    package: str
    extracted_at: str
    removed_objects: list[tuple[str, str]]  # (obj_type, obj_name)
    ddic_objects: list[DdicObject]
    objects: list[ParsedObject]


# Maps a shared-DDIC-section array key to the ddic_type bucket it represents.
_DDIC_SECTION_KEYS = {
    "DOMAIN": "DOMA",
    "DATA_ELEMENT": "DTEL",
    "TABLE": "TABL",
    "STRUCTURE": "STRU",
    "TABLE_TYPE": "TTYP",
}


def parse_extraction_file(text: str) -> ExtractionFile:
    """Parses a full extraction file: header, shared DDIC section, and every
    `=== OBJECT ===` block. Raises ParseError on the first structurally
    malformed part.
    """
    lines = text.splitlines()
    i = 0
    n = len(lines)

    run_type, i = _consume_field(lines, i, _RUN_TYPE_RE, "run_type", "<file header>")
    if run_type not in _VALID_RUN_TYPES:
        raise ParseError(
            f"RUN_TYPE must be one of {sorted(_VALID_RUN_TYPES)}, got {run_type!r}. "
            "This value controls loader behavior and cannot be guessed - fix the "
            "extraction file rather than defaulting silently."
        )

    package, i = _consume_field(lines, i, _PACKAGE_RE, "package", "<file header>")
    extracted_at, i = _consume_field(
        lines, i, _EXTRACTED_AT_RE, "extracted_at", "<file header>"
    )
    removed_raw, i = _consume_field(
        lines, i, _REMOVED_OBJECTS_RE, "removed_objects", "<file header>"
    )
    removed_objects = _parse_removed_objects(removed_raw)

    i = _skip_blank(lines, i)
    i = _expect_marker(lines, i, _DDIC_MARKER, "<file header>")

    ddic_json_lines: list[str] = []
    while i < n and not _OBJECT_HEADER_RE.match(lines[i].strip()):
        ddic_json_lines.append(lines[i])
        i += 1
    ddic_blob = "\n".join(ddic_json_lines).strip()
    if not ddic_blob:
        raise ParseError("File header: '--- DDIC ---' section is empty")
    try:
        ddic_raw = json.loads(ddic_blob)
    except json.JSONDecodeError as exc:
        raise ParseError(f"File header: DDIC JSON is not valid JSON: {exc}") from exc
    ddic_objects = _parse_ddic_section(ddic_raw)

    objects: list[ParsedObject] = []
    while i < n:
        if not lines[i].strip():
            i += 1
            continue

        header_match = _OBJECT_HEADER_RE.match(lines[i].strip())
        if not header_match:
            raise ParseError(
                f"Expected '=== OBJECT: <NAME> ===' at line {i + 1}, got: {lines[i]!r}"
            )
        name = header_match.group("name")
        i += 1

        obj_type, i = _consume_field(lines, i, _TYPE_RE, "type", name)
        if obj_type not in _VALID_OBJECT_TYPES:
            raise ParseError(
                f"Object {name!r}: TYPE must be one of {sorted(_VALID_OBJECT_TYPES)}, "
                f"got {obj_type!r}"
            )

        obj_package, i = _consume_field(lines, i, _PACKAGE_RE, "package", name)

        # Optional, step-3-export-only fields (see _RETRIEVAL_METADATA_FIELD_RE)
        # - zero or more of these may appear here in real extraction files
        # they never do, since step 1 doesn't emit them.
        while i < n and _RETRIEVAL_METADATA_FIELD_RE.match(lines[i].strip()):
            i += 1

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
            raise ParseError(f"Object {name!r}: missing '{_DEPENDENCIES_MARKER}' section")
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
            raise ParseError(f"Object {name!r}: '{_DEPENDENCIES_MARKER}' section is empty")
        try:
            deps_raw = json.loads(json_blob)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"Object {name!r}: DEPENDENCIES is not valid JSON: {exc}"
            ) from exc

        dependencies = _parse_object_dependencies(deps_raw, name)

        i += 1  # consume END OBJECT line

        objects.append(
            ParsedObject(
                name=name,
                type=obj_type,
                package=obj_package,
                source=source_text,
                dependencies=dependencies,
            )
        )

    if not objects:
        raise ParseError("No '=== OBJECT: ... ===' blocks found in input")

    return ExtractionFile(
        run_type=run_type,
        package=package,
        extracted_at=extracted_at,
        removed_objects=removed_objects,
        ddic_objects=ddic_objects,
        objects=objects,
    )


def _consume_field(lines, i, pattern, field_name, obj_name):
    n = len(lines)
    while i < n and not lines[i].strip():
        i += 1
    if i >= n:
        raise ParseError(f"Object {obj_name!r}: missing {field_name.upper()} field")
    m = pattern.match(lines[i].strip())
    if not m:
        raise ParseError(
            f"Object {obj_name!r}: expected {field_name.upper()} field, got: {lines[i]!r}"
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


def _parse_removed_objects(raw: str) -> list[tuple[str, str]]:
    raw = raw.strip()
    if raw.upper() == "NONE":
        return []

    removed = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ParseError(
                f"REMOVED_OBJECTS entry {entry!r} is not in 'TYPE:NAME' form"
            )
        obj_type, obj_name = entry.split(":", 1)
        removed.append((obj_type.strip(), obj_name.strip()))
    return removed


def _parse_ddic_section(raw: Any) -> list[DdicObject]:
    if not isinstance(raw, dict) or "PACKAGES" not in raw:
        raise ParseError("File header: DDIC JSON must be an object with a 'PACKAGES' key")

    ddic_objects: list[DdicObject] = []
    for pkg_entry in raw["PACKAGES"]:
        if not isinstance(pkg_entry, dict):
            raise ParseError("File header: each DDIC 'PACKAGES' entry must be an object")
        for section_key, ddic_type in _DDIC_SECTION_KEYS.items():
            for item in pkg_entry.get(section_key, []):
                if not isinstance(item, dict) or "NAME" not in item:
                    raise ParseError(
                        f"File header: DDIC section {section_key!r} has an entry "
                        "with no 'NAME' key"
                    )
                ddic_objects.append(
                    DdicObject(ddic_type=ddic_type, name=item["NAME"], detail=item)
                )
    return ddic_objects


def _parse_object_dependencies(raw: Any, obj_name: str) -> list[DependencyEntry]:
    if not isinstance(raw, list):
        raise ParseError(f"Object {obj_name!r}: DEPENDENCIES JSON must be a list")
    if len(raw) != 1:
        raise ParseError(
            f"Object {obj_name!r}: expected exactly one entry in the DEPENDENCIES "
            f"list (ZCR_GET_DEPENDENCY_OBJ_NEW is called per-object), got {len(raw)}"
        )

    entry = raw[0]
    if not isinstance(entry, dict) or "DEPENDENCIES" not in entry:
        raise ParseError(
            f"Object {obj_name!r}: DEPENDENCIES entry must be an object with its "
            "own 'DEPENDENCIES' list"
        )

    dependencies = []
    for dep in entry["DEPENDENCIES"]:
        if not isinstance(dep, dict) or "TYPE" not in dep or "NAME" not in dep:
            raise ParseError(
                f"Object {obj_name!r}: each dependency entry needs 'TYPE' and 'NAME'"
            )
        dep_type = dep["TYPE"]
        signature = dep.get("SIGNATURE")
        if signature is None and dep_type in _SIGNATURE_BEARING_TYPES:
            # A signature-bearing type with no SIGNATURE key at all just means
            # every one of its fields was blank (e.g. a METH with no
            # parameters/exceptions and default visibility) - compress=abap_true
            # drops the whole structure in that case. Not an error.
            signature = {}
        dependencies.append(
            DependencyEntry(type=dep_type, name=dep["NAME"], signature=signature)
        )
    return dependencies
