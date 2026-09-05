"""Generates a synthetic extraction file matching the format in parser.py.

Used to develop and test steps 2+ before a real SAP extraction file exists.
Produces a realistic mix of object types, call chains (including some 3+
hops deep), table usage, and at least one object with no dependencies.
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass

_TYPES = ["CLASS", "PROGRAM", "FUNCTION_MODULE"]
_PACKAGES = ["ZFI_CORE", "ZSD_SALES", "ZMM_MATERIALS", "ZHR_PAYROLL", "ZBASIS_UTIL"]
_TABLES = [
    ("VBAK", ["VBELN", "ERDAT", "KUNNR", "NETWR"]),
    ("VBAP", ["VBELN", "POSNR", "MATNR", "KWMENG"]),
    ("MARA", ["MATNR", "MTART", "MATKL"]),
    ("KNA1", ["KUNNR", "NAME1", "LAND1"]),
    ("BSEG", ["BUKRS", "BELNR", "GJAHR", "DMBTR"]),
    ("BKPF", ["BUKRS", "BELNR", "GJAHR", "BLDAT"]),
    ("LFA1", ["LIFNR", "NAME1", "LAND1"]),
    ("EKKO", ["EBELN", "BUKRS", "LIFNR"]),
    ("EKPO", ["EBELN", "EBELP", "MATNR", "MENGE"]),
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


@dataclass
class MockObjectSpec:
    name: str
    type: str
    package: str
    calls: list[str]
    tables_used: list[tuple[str, list[str]]]
    line_count: int


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


def _build_specs(rng: random.Random, count: int) -> list[MockObjectSpec]:
    names = []
    for idx in range(count):
        obj_type = rng.choice(_TYPES)
        prefix = {"CLASS": "ZCL", "PROGRAM": "ZP", "FUNCTION_MODULE": "Z_FM"}[obj_type]
        names.append((f"{prefix}_{idx:03d}_{rng.choice(['CORE','UTIL','PROC','MGR','CALC'])}", obj_type))

    specs: list[MockObjectSpec] = []
    for i, (name, obj_type) in enumerate(names):
        specs.append(
            MockObjectSpec(
                name=name,
                type=obj_type,
                package=rng.choice(_PACKAGES),
                calls=[],
                tables_used=[],
                line_count=rng.randint(50, 300),
            )
        )

    # No-dependency object: guaranteed at least one leaf with nothing.
    leaf_idx = 0
    specs[leaf_idx].calls = []
    specs[leaf_idx].tables_used = [rng.choice(_TABLES)] if rng.random() < 0.5 else []

    # Build a few explicit chains of 3+ objects deep: obj[a] -> obj[b] -> obj[c] -> ...
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
            if specs[b].name not in specs[a].calls:
                specs[a].calls.append(specs[b].name)

    # Sprinkle additional random calls (including some external/unresolved names)
    external_targets = ["BAPI_SALESORDER_CREATEFROMDAT2", "RFC_READ_TABLE", "Z_EXTERNAL_LEGACY_FM"]
    for i, spec in enumerate(specs):
        if i == leaf_idx:
            continue
        extra_call_count = rng.randint(0, 2)
        candidates = [s.name for j, s in enumerate(specs) if j != i]
        for _ in range(extra_call_count):
            if rng.random() < 0.2:
                target = rng.choice(external_targets)
            else:
                target = rng.choice(candidates)
            if target != spec.name and target not in spec.calls:
                spec.calls.append(target)

    # Table usage for everyone except the deliberately dependency-free leaf.
    for i, spec in enumerate(specs):
        if i == leaf_idx:
            continue
        table_count = rng.randint(1, 3)
        chosen = rng.sample(_TABLES, k=table_count)
        for table_name, all_fields in chosen:
            field_count = rng.randint(2, len(all_fields))
            spec.tables_used.append((table_name, rng.sample(all_fields, k=field_count)))

    return specs


def render_extraction_file(specs: list[MockObjectSpec], rng: random.Random) -> str:
    blocks = []
    for spec in specs:
        source = _generate_source(rng, spec.type, spec.line_count)
        deps = {
            "calls": spec.calls,
            "tables_used": [
                {"table": t, "fields": f} for t, f in spec.tables_used
            ],
        }
        block = (
            f"=== OBJECT: {spec.name} ===\n"
            f"TYPE: {spec.type}\n"
            f"PACKAGE: {spec.package}\n"
            f"\n"
            f"--- SOURCE ---\n"
            f"{source}\n"
            f"\n"
            f"--- DEPENDENCIES ---\n"
            f"{json.dumps(deps, indent=2)}\n"
            f"=== END OBJECT ===\n"
        )
        blocks.append(block)
    return "\n".join(blocks)


def generate(count: int = 18, seed: int = 42) -> tuple[str, list[MockObjectSpec]]:
    rng = random.Random(seed)
    specs = _build_specs(rng, count)
    text = render_extraction_file(specs, rng)
    return text, specs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=18, help="number of objects to generate (>=15)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--out", type=str, default="data/mock_extraction.txt", help="output file path"
    )
    args = parser.parse_args()
    if args.count < 15:
        raise SystemExit("count must be >= 15 per the brief")

    text, specs = generate(count=args.count, seed=args.seed)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"Wrote {len(specs)} objects to {args.out}")
    total_calls = sum(len(s.calls) for s in specs)
    total_tables = sum(len(s.tables_used) for s in specs)
    print(f"Total calls: {total_calls}, total table-usage rows: {total_tables}")


if __name__ == "__main__":
    main()
