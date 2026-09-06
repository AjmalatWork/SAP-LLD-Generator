"""Generates synthetic extraction files matching the REAL LLD extraction
format (see lld_step2/parser.py) - the same shape ZLLD_PACKAGE_EXTRACTOR
actually produces: a file-level header, a shared --- DDIC --- section, and
per-object DEPENDENCIES as a flat list of typed entries with real-shaped
SIGNATURE enrichment.

Used to develop and test steps 2+ without a real SAP extraction file. Two
entry points:
  - generate(): a FULL-run file with a realistic mix of object types, call
    chains (3+ hops deep), DDIC references, and at least one object with no
    dependencies.
  - generate_incremental(): a small INCREMENTAL-run file built from an
    existing FULL run's specs - one object mutated (simulating a real
    change), one object listed in REMOVED_OBJECTS, everything else absent
    (simulating a real scoped sync). This is what tests use to exercise
    the FULL/INCREMENTAL loader logic without needing a second real sync.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

_TYPES = ["CLASS", "PROGRAM", "FUNCTION_MODULE"]
_LLD_TYPE_TO_DEPENDENCY_CODE = {"CLASS": "CLAS", "PROGRAM": "PROG", "FUNCTION_MODULE": "FUNC"}

# (table_name, description, [(field_name, data_element), ...])
_TABLES = [
    ("VBAK", "Sales Document: Header Data", [("VBELN", "VBELN_VA"), ("ERDAT", "ERDAT"), ("KUNNR", "KUNNR"), ("NETWR", "NETWR_AK")]),
    ("VBAP", "Sales Document: Item Data", [("VBELN", "VBELN_VA"), ("POSNR", "POSNR_VA"), ("MATNR", "MATNR"), ("KWMENG", "KWMENG")]),
    ("MARA", "General Material Data", [("MATNR", "MATNR"), ("MTART", "MTART"), ("MATKL", "MATKL")]),
    ("KNA1", "General Customer Data", [("KUNNR", "KUNNR"), ("NAME1", "NAME1"), ("LAND1", "LAND1")]),
    ("BSEG", "Accounting Document Segment", [("BUKRS", "BUKRS"), ("BELNR", "BELNR_D"), ("GJAHR", "GJAHR"), ("DMBTR", "DMBTR")]),
    ("BKPF", "Accounting Document Header", [("BUKRS", "BUKRS"), ("BELNR", "BELNR_D"), ("GJAHR", "GJAHR"), ("BLDAT", "BLDAT")]),
    ("LFA1", "Vendor Master (General Section)", [("LIFNR", "LIFNR"), ("NAME1", "NAME1"), ("LAND1", "LAND1")]),
    ("EKKO", "Purchasing Document Header", [("EBELN", "EBELN"), ("BUKRS", "BUKRS"), ("LIFNR", "LIFNR")]),
    ("EKPO", "Purchasing Document Item", [("EBELN", "EBELN"), ("EBELP", "EBELP"), ("MATNR", "MATNR"), ("MENGE", "MENGE_D")]),
]

# (domain_name, description, abap_type, size)
_DOMAINS = [
    ("ZMOCK_STATUS", "Mock status domain", "CHAR", 1),
    ("ZMOCK_PRIORITY", "Mock priority domain", "CHAR", 1),
]

# (table_type_name, description, line_type_table_name)
_TABLE_TYPES = [
    ("ZTT_MOCK_ITEMS", "Mock item table type", "VBAP"),
]

_ABAP_KEYWORDS_BY_TYPE = {
    "CLASS": ("METHOD", "ENDMETHOD"),
    "PROGRAM": ("FORM", "ENDFORM"),
    "FUNCTION_MODULE": ("FUNCTION", "ENDFUNCTION"),
}

_METHOD_NAMES = [
    "VALIDATE_INPUT", "CALCULATE_TOTAL", "GET_HEADER_DATA", "GET_ITEM_DATA",
    "CHECK_AUTHORIZATION", "UPDATE_STATUS", "POST_DOCUMENT", "READ_MASTER_DATA",
    "APPLY_PRICING", "SEND_NOTIFICATION", "LOG_ERROR", "BUILD_OUTPUT_TABLE",
]

_INTERFACES = ["IF_ZMOCK_SERIALIZABLE", "IF_ZMOCK_VALIDATABLE"]

# object_calls edge_kinds that represent a genuine reference to another
# object (see graph_loader.CALL_LIKE_EDGE_KINDS) - used here only to decide
# which dependency TYPEs should carry a graph-traversable NAME (i.e. another
# mock object's own name) versus a bare/synthetic name.
_OBJECT_NAME_BEARING_TYPES = {"CLAS", "PROG", "FUNC", "INCL", "FUGR"}
_DDIC_REFERENCE_TYPES = {"DOMA", "DTEL", "TABL", "TTYP"}


@dataclass
class MockDependency:
    type: str
    name: str
    signature: dict | None = None


@dataclass
class MockObjectSpec:
    name: str
    type: str
    package: str
    line_count: int
    dependencies: list[MockDependency] = field(default_factory=list)

    @property
    def object_ref_targets(self) -> list[str]:
        """Names of other mock objects this one references (CLAS/PROG/FUNC/
        INCL/FUGR-type deps whose name matches another spec) - the
        traversable-graph-edge equivalent of the old `.calls` list."""
        return [d.name for d in self.dependencies if d.type in _OBJECT_NAME_BEARING_TYPES]

    @property
    def ddic_refs(self) -> list[tuple[str, str]]:
        """(ddic_type, name) pairs for this object's DOMA/DTEL/TABL/TTYP
        dependencies - the equivalent of the old `.tables_used` list."""
        return [(d.type, d.name) for d in self.dependencies if d.type in _DDIC_REFERENCE_TYPES]


def _lorem_statement(rng: random.Random) -> str:
    verbs = ["SELECT", "MOVE", "APPEND", "CLEAR", "LOOP AT", "IF", "ENDIF", "PERFORM",
              "CALL METHOD", "DATA:", "CONCATENATE", "WRITE:", "CHECK", "TRY.", "CATCH"]
    nouns = ["lt_data", "ls_header", "lv_result", "lt_items", "lv_count", "ls_item",
             "lv_flag", "lt_output", "lv_msg", "ls_config"]
    return f"  {rng.choice(verbs)} {rng.choice(nouns)}."


def _generate_source(rng: random.Random, obj_type: str, line_count: int) -> str:
    open_kw, close_kw = _ABAP_KEYWORDS_BY_TYPE[obj_type]
    lines: list[str] = []
    remaining = line_count
    block_names = rng.sample(_METHOD_NAMES, k=min(len(_METHOD_NAMES), max(1, line_count // 40)))
    if not block_names:
        block_names = [rng.choice(_METHOD_NAMES)]

    per_block = max(5, remaining // max(1, len(block_names)))
    for block_name in block_names:
        lines.append(f"{open_kw} {block_name}.")
        for _ in range(per_block):
            lines.append(_lorem_statement(rng))
        lines.append(close_kw + ".")
        lines.append("")
    while len(lines) < line_count:
        lines.append(_lorem_statement(rng))
    return "\n".join(lines[:line_count])


def _mock_method_signature(rng: random.Random) -> dict:
    sig: dict = {"VISIBILITY": rng.choice(["PUBLIC", "PUBLIC", "PRIVATE"])}
    if rng.random() < 0.3:
        sig["IS_STATIC"] = "X"
    params = []
    for _ in range(rng.randint(0, 3)):
        param: dict = {
            "NAME": f"I{'W' if rng.random() < 0.5 else 'V'}_{rng.choice(['DATA', 'VALUE', 'FLAG', 'COUNT'])}",
            "KIND": rng.choice(["IMPORTING", "IMPORTING", "EXPORTING", "RETURNING"]),
            "TYPE": rng.choice(["STRING", "I", "ZMOCK_STATUS", "ABAP_BOOL"]),
        }
        if rng.random() < 0.3:
            param["OPTIONAL"] = "X"
        params.append(param)
    if params:
        sig["PARAMETERS"] = params
    return sig


def _build_specs(rng: random.Random, count: int, package: str) -> list[MockObjectSpec]:
    # Object names are only unique within a naming scheme that varies by
    # index and a small suffix vocabulary - two independent generate() calls
    # (different seeds/counts) can otherwise collide on a name (e.g. both
    # producing "ZCL_002_UTIL"), which would corrupt package-scoped tests
    # since `objects.name` is a global primary key. Folding the package name
    # into the identifier avoids this - real ABAP object names are already
    # globally unique system-wide, so this also mirrors a realistic
    # per-package naming convention, not just a test workaround.
    names = []
    for idx in range(count):
        obj_type = rng.choice(_TYPES)
        prefix = {"CLASS": "ZCL", "PROGRAM": "ZP", "FUNCTION_MODULE": "Z_FM"}[obj_type]
        names.append(
            (f"{prefix}_{package}_{idx:03d}_{rng.choice(['CORE','UTIL','PROC','MGR','CALC'])}", obj_type)
        )

    specs: list[MockObjectSpec] = [
        MockObjectSpec(name=name, type=obj_type, package=package, line_count=rng.randint(50, 300))
        for name, obj_type in names
    ]

    # No-dependency object: guaranteed at least one leaf with nothing.
    leaf_idx = 0

    # Build a few explicit chains of 3+ objects deep: obj[a] -> obj[b] -> obj[c] -> ...
    # via CLAS-type entries, mirroring real data where CLAS/PROG/FUNC/INCL
    # entries (not METH/OM, whose names are typically bare method names) are
    # what carry a traversable reference to another object.
    remaining_indices = list(range(1, len(specs)))
    rng.shuffle(remaining_indices)
    chains: list[list[int]] = []
    cursor = 0
    while cursor + 3 <= len(remaining_indices) and len(chains) < 3:
        chain_len = rng.randint(3, 5)
        chain = remaining_indices[cursor:cursor + chain_len]
        if len(chain) >= 3:
            chains.append(chain)
        cursor += chain_len

    for chain in chains:
        for a, b in zip(chain, chain[1:]):
            if specs[b].name not in specs[a].object_ref_targets:
                specs[a].dependencies.append(MockDependency(type="CLAS", name=specs[b].name))

    # Every non-leaf class gets its own METH entries (self-inventory, matching
    # ZCR_GET_DEPENDENCY_OBJ_NEW's real CLAS-branch behavior) and, for one of
    # them, an INTF entry - guarantees at least one object with an INTF-type
    # dependency, per the deliverable requirement.
    for i, spec in enumerate(specs):
        if i == leaf_idx or spec.type != "CLASS":
            continue
        for method_name in rng.sample(_METHOD_NAMES, k=rng.randint(1, 3)):
            spec.dependencies.append(
                MockDependency(type="METH", name=method_name, signature=_mock_method_signature(rng))
            )
    class_indices = [i for i, s in enumerate(specs) if s.type == "CLASS" and i != leaf_idx]
    if class_indices:
        specs[class_indices[0]].dependencies.append(
            MockDependency(type="INTF", name=rng.choice(_INTERFACES))
        )

    # PROGRAM/FUNCTION_MODULE objects get OM-type entries (bare method-call
    # references, matching REPOSITORY_ENVIRONMENT_RFC's real behavior) -
    # guarantees at least one object with an OM-type dependency.
    external_targets = ["BAPI_SALESORDER_CREATEFROMDAT2", "RFC_READ_TABLE", "Z_EXTERNAL_LEGACY_FM"]
    for i, spec in enumerate(specs):
        if i == leaf_idx:
            continue
        if spec.type in ("PROGRAM", "FUNCTION_MODULE"):
            for _ in range(rng.randint(1, 2)):
                spec.dependencies.append(
                    MockDependency(
                        type="OM", name=rng.choice(_METHOD_NAMES), signature=_mock_method_signature(rng)
                    )
                )

        # Sprinkle additional random object references (including some
        # external/unresolved names).
        extra_call_count = rng.randint(0, 2)
        candidates = [s.name for j, s in enumerate(specs) if j != i]
        for _ in range(extra_call_count):
            target = rng.choice(external_targets) if rng.random() < 0.2 else rng.choice(candidates)
            if target != spec.name and target not in spec.object_ref_targets:
                spec.dependencies.append(MockDependency(type="CLAS", name=target))

    # DDIC references for everyone except the deliberately dependency-free leaf.
    for i, spec in enumerate(specs):
        if i == leaf_idx:
            continue
        table_count = rng.randint(1, 3)
        chosen = rng.sample(_TABLES, k=table_count)
        for table_name, _description, _fields in chosen:
            spec.dependencies.append(MockDependency(type="TABL", name=table_name))
        if rng.random() < 0.3:
            spec.dependencies.append(MockDependency(type="DTEL", name="ZMOCK_STATUS"))

    return specs


def _build_shared_ddic_section(package: str, referenced_ddic: set[tuple[str, str]]) -> dict:
    """Builds the shared --- DDIC --- JSON, scoped to only the DDIC names
    actually referenced by the objects being rendered (mirrors how a real
    incremental file's shared section may legitimately be a subset, not the
    whole package)."""
    referenced_names = {name for _t, name in referenced_ddic}

    domains = [
        {"NAME": name, "DESCRIPTION": desc, "TYPE": abap_type, "SIZE": size, "POSSIBLE_VALUES": ""}
        for name, desc, abap_type, size in _DOMAINS
        if name in referenced_names
    ]
    data_elements = [
        {"NAME": "ZMOCK_STATUS", "DESCRIPTION": "Mock status data element", "DOMAIN": "ZMOCK_STATUS"}
    ] if "ZMOCK_STATUS" in referenced_names else []
    tables = []
    for table_name, description, fields in _TABLES:
        if table_name not in referenced_names:
            continue
        tables.append(
            {
                "NAME": table_name,
                "DESCRIPTION": description,
                "TYPE": "TRANSP",
                "SM30": False,
                "LOCK_OBJECT": "",
                "FIELD": len(fields),
                "INDEX": 0,
                "FIELDS": [
                    {"NAME": fname, "KEY": "X" if i == 0 else "", "DATA_ELEMENT": delement}
                    for i, (fname, delement) in enumerate(fields)
                ],
                "INDEXES": [],
            }
        )
    table_types = [
        {"NAME": name, "DESCRIPTION": desc, "LINE_TYPE": line_type}
        for name, desc, line_type in _TABLE_TYPES
        if name in referenced_names
    ]

    return {
        "PACKAGES": [
            {
                "PACKAGE": package,
                "DOMAIN": domains,
                "DATA_ELEMENT": data_elements,
                "TABLE": tables,
                "STRUCTURE": [],
                "TABLE_TYPE": table_types,
            }
        ]
    }


def _render_object_block(spec: MockObjectSpec, rng: random.Random) -> str:
    source = _generate_source(rng, spec.type, spec.line_count)
    dep_entries = []
    for dep in spec.dependencies:
        entry: dict = {"TYPE": dep.type, "NAME": dep.name}
        if dep.signature is not None:
            entry["SIGNATURE"] = dep.signature
        dep_entries.append(entry)

    dep_json = json.dumps(
        [
            {
                "TYPE": _LLD_TYPE_TO_DEPENDENCY_CODE[spec.type],
                "NAME": spec.name,
                "DEPENDENCIES": dep_entries,
            }
        ]
    )

    return (
        f"=== OBJECT: {spec.name} ===\n"
        f"TYPE: {spec.type}\n"
        f"PACKAGE: {spec.package}\n"
        f"\n"
        f"--- SOURCE ---\n"
        f"{source}\n"
        f"\n"
        f"--- DEPENDENCIES ---\n"
        f"{dep_json}\n"
        f"=== END OBJECT ===\n"
    )


def _render_file(
    specs: list[MockObjectSpec],
    rng: random.Random,
    run_type: str,
    package: str,
    removed_objects: list[tuple[str, str]],
) -> str:
    referenced_ddic: set[tuple[str, str]] = set()
    for spec in specs:
        referenced_ddic.update(spec.ddic_refs)

    header_lines = [
        f"RUN_TYPE: {run_type}",
        f"PACKAGE: {package}",
        f"EXTRACTED_AT: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
        "REMOVED_OBJECTS: "
        + (", ".join(f"{t}:{n}" for t, n in removed_objects) if removed_objects else "NONE"),
        "",
        "--- DDIC ---",
        json.dumps(_build_shared_ddic_section(package, referenced_ddic)),
        "",
    ]

    blocks = [_render_object_block(spec, rng) for spec in specs]
    return "\n".join(header_lines) + "\n" + "\n".join(blocks)


def generate(
    count: int = 18, seed: int = 42, package: str = "ZMOCK_PKG"
) -> tuple[str, list[MockObjectSpec]]:
    """A FULL-run file: every object, REMOVED_OBJECTS: NONE."""
    rng = random.Random(seed)
    specs = _build_specs(rng, count, package)
    text = _render_file(specs, rng, run_type="FULL", package=package, removed_objects=[])
    return text, specs


def generate_incremental(
    specs: list[MockObjectSpec],
    seed: int = 43,
    updated_index: int = 1,
    removed_index: int = 2,
) -> tuple[str, MockObjectSpec, MockObjectSpec]:
    """An INCREMENTAL-run file built from an existing FULL run's specs (same
    object identities, so a prior FULL load's data is what it's diffed
    against). Contains exactly ONE object block - `specs[updated_index]`,
    mutated (an extra dependency + longer source) to simulate a real
    change - and lists `specs[removed_index]` in REMOVED_OBJECTS. Every
    other object from `specs` is deliberately absent, simulating a real
    scoped sync that only mentions what changed.

    Returns (file_text, updated_spec, removed_spec) so a test can assert
    against the mutated spec's new shape and the removed spec's identity,
    without re-deriving either from the rendered text.
    """
    rng = random.Random(seed)

    updated_spec = copy.deepcopy(specs[updated_index])
    updated_spec.line_count += 20
    # Add a genuinely new TABL reference (not one it already has) so the
    # mutated spec's dependency count is unambiguous - real
    # ZCR_GET_DEPENDENCY_OBJ_NEW output never emits a duplicate dependency
    # for one object (it dedupes internally), so a mock spec with a
    # literal duplicate would be unrepresentative, not just untidy.
    existing_tabl_names = {d.name for d in updated_spec.dependencies if d.type == "TABL"}
    new_table = next(name for name, _desc, _fields in _TABLES if name not in existing_tabl_names)
    updated_spec.dependencies.append(MockDependency(type="TABL", name=new_table))

    removed_spec = specs[removed_index]

    text = _render_file(
        [updated_spec],
        rng,
        run_type="INCREMENTAL",
        package=updated_spec.package,
        removed_objects=[(_LLD_TYPE_TO_DEPENDENCY_CODE[removed_spec.type], removed_spec.name)],
    )
    return text, updated_spec, removed_spec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=18, help="number of objects to generate (>=15)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--package", type=str, default="ZMOCK_PKG")
    parser.add_argument(
        "--out", type=str, default="data/mock_extraction.txt", help="output file path"
    )
    args = parser.parse_args()
    if args.count < 15:
        raise SystemExit("count must be >= 15 per the brief")

    text, specs = generate(count=args.count, seed=args.seed, package=args.package)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"Wrote {len(specs)} objects to {args.out}")
    total_refs = sum(len(s.object_ref_targets) for s in specs)
    total_ddic = sum(len(s.ddic_refs) for s in specs)
    print(f"Total object references: {total_refs}, total DDIC references: {total_ddic}")


if __name__ == "__main__":
    main()
