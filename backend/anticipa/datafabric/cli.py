"""`anticipa datafabric <command>`: data-fabric operator commands (mounted by anticipa.cli.module_clis)."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "judge-subset":
        from anticipa.datafabric.judge_subset import main as judge_subset

        return judge_subset(argv[1:])
    if argv and argv[0] == "hapi-load":
        from anticipa.datafabric.hapi_load import main as hapi_load

        return hapi_load(argv[1:])
    print(
        "usage: anticipa datafabric judge-subset [--dry-run] [--target N] [--max-mb MB]\n"
        "       anticipa datafabric hapi-load [--dry-run] [--verify-only] [--workers N]",
        file=sys.stderr,
    )
    return 2
