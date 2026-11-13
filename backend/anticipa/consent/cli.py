"""`anticipa consent <command>`: consent operator commands (mounted by anticipa.cli.module_clis).

seed [--seed N] [--dataset V] [--dry-run]
    Insert the deterministic SYNTHETIC opt-out set (about 2.5% of residents: 0.5% opted out of all
    outreach, 1.5% of one channel, 0.5% of AI processing). Idempotent. `--dry-run` only counts.
remove-seed
    Delete the synthetic seed rows (never a decision a person recorded).
"""

from __future__ import annotations

import argparse
import json
import sys

from anticipa.consent import service
from anticipa.db import get_sessionmaker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anticipa consent", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed", help="insert the synthetic opt-out set (idempotent)")
    seed.add_argument("--seed", type=int, default=20261113)
    seed.add_argument("--dataset", default=None, help="dataset_version to seed (default: every resident)")
    seed.add_argument("--dry-run", action="store_true")
    sub.add_parser("remove-seed", help="delete the synthetic seed rows")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    with get_sessionmaker()() as session:
        if args.command == "seed":
            result = service.seed_synthetic_optouts(
                session, seed=args.seed, dataset_version=args.dataset, dry_run=args.dry_run
            )
            session.commit()
        else:
            result = {"deleted": service.remove_seeded(session)}
            session.commit()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
