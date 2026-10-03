"""`ai-memory` CLI: export / import / backup / stats."""
from __future__ import annotations
import argparse
import os

from .config import Config
from .db import connect
from .sdk import Memory


def main(argv: list[str] | None = None) -> int:
    cfg = Config()
    ap = argparse.ArgumentParser(description="AI memory portable CLI")
    ap.add_argument("--db", default=os.environ.get("AI_MEMORY_DB", cfg.db_path))
    ap.add_argument("--embed", default=os.environ.get("AI_MEMORY_EMBED", "hash"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("export", help="dump memory to JSONL")
    p.add_argument("-o", "--out", required=True)

    p = sub.add_parser("import", help="load JSONL export (vectors recomputed)")
    p.add_argument("-i", "--inp", required=True)

    p = sub.add_parser("backup", help="timestamped copy of memory.db")
    p.add_argument("--dest", default=None)
    p.add_argument("--keep", type=int, default=None,
                   help="retain newest N .bak files, prune rest")

    p = sub.add_parser("restore", help="restore trashed rows by id or text")
    p.add_argument("--ref-id", default=None)
    p.add_argument("--query", default=None)

    sub.add_parser("maintenance", help="integrity check, orphan cleanup, VACUUM")

    p = sub.add_parser("recall", help="hybrid recall, one hit per line")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--namespace", default=None)

    p = sub.add_parser("forget", help="soft-delete rows matching id or text")
    p.add_argument("--ref-id", default=None)
    p.add_argument("--query", default=None)

    p = sub.add_parser("store", help="store an episode/fact/procedure, prints id")
    p.add_argument("text")
    p.add_argument("--kind", default="episode")
    p.add_argument("--entity", default="user")
    p.add_argument("--key", default="note")
    p.add_argument("--namespace", default=None)

    p = sub.add_parser("consolidate", help="distill unprocessed episodes")
    p.add_argument("--limit", type=int, default=50)

    sub.add_parser("stats", help="print counts")

    args = ap.parse_args(argv)
    m = Memory(db_path=args.db, embed_provider=args.embed)
    if args.cmd == "export":
        print(m.export(args.out))
    elif args.cmd == "import":
        print(m.import_(args.inp))
    elif args.cmd == "backup":
        print(m.backup(args.dest, keep=args.keep))
    elif args.cmd == "restore":
        print(m.restore(ref_id=args.ref_id, query=args.query))
    elif args.cmd == "maintenance":
        print(m.maintenance())
    elif args.cmd == "recall":
        for h in m.recall(args.query, k=args.k, namespace=args.namespace):
            print(f"{h['score']:.3f} [{h['store']}] {h['text']}")
    elif args.cmd == "forget":
        print(m.forget(ref_id=args.ref_id, query=args.query))
    elif args.cmd == "store":
        print(m.store(args.text, kind=args.kind, entity=args.entity,
                      key=args.key, harness="cli", namespace=args.namespace))
    elif args.cmd == "consolidate":
        print(m.consolidate(limit=args.limit))
    elif args.cmd == "stats":
        print(m.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
