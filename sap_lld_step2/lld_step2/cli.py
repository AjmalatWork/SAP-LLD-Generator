"""Small CLI to eyeball the loaded graph: callers, callees, tables used, and
multi-hop reachability for a given object, via plain SQL.

    python -m lld_step2.cli inspect ZCL_000_CORE
    python -m lld_step2.cli reachable ZCL_000_CORE --hops 2
"""
from __future__ import annotations

import argparse

from .config import load_config
from .db import connect
from .graph_loader import get_reachable_within_hops, print_object_summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_p = sub.add_parser("inspect", help="show direct callers/callees/tables for an object")
    inspect_p.add_argument("object_name")

    reach_p = sub.add_parser("reachable", help="show objects reachable within N hops")
    reach_p.add_argument("object_name")
    reach_p.add_argument("--hops", type=int, default=2)

    args = parser.parse_args()
    config = load_config()
    conn = connect(config)
    try:
        if args.command == "inspect":
            print_object_summary(conn, args.object_name)
        elif args.command == "reachable":
            results = get_reachable_within_hops(conn, args.object_name, args.hops)
            print(f"Reachable from {args.object_name} within {args.hops} hop(s):")
            for r in results:
                print(f"  ({r['hops']} hop{'s' if r['hops'] != 1 else ''}) {r['object']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
