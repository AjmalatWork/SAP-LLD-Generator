"""Manual-testing CLI for step 3 retrieval.

    python -m lld_step2.retrieval_cli "Allow partial shipment when stock is insufficient" \\
        --packages ZORDER_MGMT --scoping-note "ZCL_STOCK_MANAGER" --confidence likely --export out.txt
"""
from __future__ import annotations

import argparse

from .retrieval import export_candidates_to_file, retrieve


def _print_candidate(rank: int, c) -> None:
    print(f"  {rank}. {c.object_name}  ({c.type}, {c.package})")
    print(
        f"     final={c.final_score:.3f}  base={c.base_score:.3f}  "
        f"semantic={c.semantic_score:.3f}  structural={c.structural_score:.3f}  "
        f"tier: {c.tier_adjustment}"
    )
    if c.callers:
        print(f"     callers:  {', '.join(c.callers)}")
    if c.callees:
        print(f"     callees:  {', '.join(c.callees)}")
    if c.tables_used:
        tables = ", ".join(t["table"] for t in c.tables_used)
        print(f"     tables:   {tables}")
    for chunk in c.top_chunks:
        preview = chunk.chunk_text.strip().splitlines()[0][:70]
        print(f"     chunk[{chunk.chunk_index}] sim={chunk.similarity:.3f}: {preview}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("requirement", help="plain-English business requirement")
    parser.add_argument(
        "--packages",
        required=True,
        help="required, comma-separated package name(s) the application lives in "
        "(e.g. ZZBA91 or ZZBA91,ZZBA92) - retrieval only searches these packages' "
        "loaded data; an unknown package name is a hard error",
    )
    parser.add_argument("--scoping-note", default=None, help="optional object/flow name(s)")
    parser.add_argument(
        "--confidence",
        default=None,
        choices=["certain", "likely", "unsure"],
        help="required if --scoping-note is given",
    )
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--export", default=None, help="optional path to export the candidate file")
    args = parser.parse_args()
    packages = [p.strip() for p in args.packages.split(",") if p.strip()]

    result = retrieve(
        requirement=args.requirement,
        packages=packages,
        scoping_note=args.scoping_note,
        confidence=args.confidence,
        top_n=args.top_n,
    )

    print(f"Requirement: {args.requirement}")
    print(f"Packages: {packages}")
    if args.scoping_note:
        print(f"Scoping note: {args.scoping_note!r} (confidence: {args.confidence})")
        resolved = result.query_metadata["resolved_named_objects"]
        print(f"Resolved to: {resolved if resolved else '(unresolved)'}")
    print(f"Top {result.query_metadata['constants']['top_seed_objects']} by semantic score: "
          f"{result.query_metadata['top3_semantic_candidates']}")
    print(f"Trusted seed objects (also clear semantic floor "
          f"{result.query_metadata['constants']['seed_min_semantic_score']}): "
          f"{result.query_metadata['seed_objects'] or '(none)'}")
    print()

    print(f"=== Candidates (top {len(result.candidates)}) ===")
    for i, c in enumerate(result.candidates, start=1):
        _print_candidate(i, c)
    print()

    print("=== Unadjusted ranking (pure base_score, top 5) ===")
    for i, c in enumerate(result.unadjusted_ranking, start=1):
        print(f"  {i}. {c.object_name}  base_score={c.base_score:.3f}")
    print()

    print(f"=== Disagreements ({len(result.disagreements)}) ===")
    for d in result.disagreements:
        print(f"  [{d.kind}] {d.message}")
    if not result.disagreements:
        print("  (none)")
    print()

    print(f"likely_new_object: {result.likely_new_object}")
    if result.likely_new_object_reason:
        print(f"  reason: {result.likely_new_object_reason}")

    if args.export:
        export_candidates_to_file(result, args.export)
        print(f"\nExported {len(result.candidates)} candidates to {args.export}")


if __name__ == "__main__":
    main()
