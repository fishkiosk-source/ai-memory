"""Background worker: `ai-memory-consolidate [--loop]`. The 'sleep' cycle."""

from __future__ import annotations
import argparse
import os
import time

from .config import Config
from .db import connect
from . import consolidator as C


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI memory consolidation worker")
    ap.add_argument("--db", default=os.environ.get("AI_MEMORY_DB", Config().db_path))
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument(
        "--interval",
        type=float,
        default=300,
        help="seconds between runs in --loop mode",
    )
    ap.add_argument("--loop", action="store_true", help="run forever until Ctrl-C")
    ap.add_argument("--vacuum", action="store_true", help="vacuum old trivia each run")
    ap.add_argument(
        "--reprocess",
        action="store_true",
        help="re-extract already-processed episodes (backfill after rule changes)",
    )
    ap.add_argument(
        "--llm",
        default=os.environ.get("AI_MEMORY_LLM", "off"),
        help="LLM hook: off|ollama|openai (rules-only when off)",
    )
    ap.add_argument(
        "--embed",
        default=os.environ.get("AI_MEMORY_EMBED", Config().embed_provider),
        help="embedder: hash|st|ollama (must match server or vec space mixes)",
    )
    args = ap.parse_args(argv)

    from .embeddings import get_embedder, DIM_DEFAULT

    try:
        _embedder = get_embedder(args.embed, DIM_DEFAULT)
        _dim = getattr(_embedder, "dim", DIM_DEFAULT)
    except Exception:
        _embedder, _dim = None, DIM_DEFAULT

    def _vec(text: str):
        try:
            if _embedder is None:
                return None
            v = _embedder.embed(text)
            if v is not None and len(v) == _dim:
                return v
            return None
        except Exception:
            return None

    def run() -> dict:
        con = connect(args.db)
        try:
            res = C.run_once(
                con,
                limit=args.limit,
                vacuum=args.vacuum,
                llm=args.llm,
                reprocess=args.reprocess,
                embed_fn=_vec,
            )
        finally:
            con.close()
        print(f"[consolidate] {res}", flush=True)
        return res

    run()
    if args.loop:
        while True:
            time.sleep(args.interval)
            run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
