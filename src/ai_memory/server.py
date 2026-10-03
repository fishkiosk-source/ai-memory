"""MCP server: stdio (local) or streamable HTTP (LAN) with Bearer token auth."""
from __future__ import annotations
import argparse
import json
import os
import secrets

from .sdk import Memory

DB = os.environ.get("AI_MEMORY_DB", "memory.db")
EMBED = os.environ.get("AI_MEMORY_EMBED", "hash")
mem = Memory(db_path=DB, embed_provider=EMBED)

try:
    from fastmcp import FastMCP

    mcp = FastMCP("ai-memory")

    @mcp.tool()
    def memory_store(text: str, kind: str = "episode", entity: str = "user",
                     key: str = "note", importance: float | None = None,
                     namespace: str | None = None) -> str:
        """Store an episode, fact, or procedure. Returns id."""
        return mem.store(text, kind=kind, entity=entity, key=key,
                         importance=importance, namespace=namespace)

    @mcp.tool()
    def memory_recall(query: str, k: int = 5,
                      namespace: str | None = None) -> list[dict]:
        """Hybrid recall (sqlite-vec cosine + FTS + importance + recency)."""
        return mem.recall(query, k, namespace=namespace)

    @mcp.tool()
    def memory_forget(ref_id: str | None = None, query: str | None = None) -> int:
        """Soft-delete into trash (30d restore grace). Returns rows moved."""
        return mem.forget(ref_id, query)

    @mcp.tool()
    def memory_restore(ref_id: str | None = None, query: str | None = None) -> int:
        """Restore trashed rows by id or text query. Returns rows restored."""
        return mem.restore(ref_id, query)

    @mcp.tool()
    def memory_consolidate(limit: int = 50, vacuum: bool = False,
                           llm: str | None = None) -> dict:
        """Distill unprocessed episodes into facts/procedures. llm=ollama|openai enables the LLM hook."""
        return mem.consolidate(limit, vacuum=vacuum, llm=llm)

    @mcp.tool()
    def memory_vacuum(older_than_days: int = 90) -> int:
        """Delete old low-importance trivia. Returns rows removed."""
        return mem.vacuum(older_than_days)

    @mcp.tool()
    def memory_export(path: str) -> dict:
        """Dump memory to JSONL (vectors recomputed on import)."""
        return mem.export(path)

    @mcp.tool()
    def memory_import(path: str) -> dict:
        """Load a JSONL export."""
        return mem.import_(path)

    @mcp.tool()
    def memory_stats() -> dict:
        return mem.stats()

    @mcp.tool()
    def memory_maintenance() -> dict:
        """Integrity check, orphan cleanup, FTS optimize, VACUUM. Returns space stats."""
        return mem.maintenance()

except Exception:  # fastmcp not installed: SDK still works
    mcp = None


class BearerAuthMiddleware:
    """Pure-ASGI Bearer check. Rejects without valid Authorization header."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        got = headers.get(b"authorization", b"").decode("latin1")
        if secrets.compare_digest(got, f"Bearer {self.token}"):
            return await self.app(scope, receive, send)
        body = json.dumps({"error": "unauthorized"}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def main(argv: list[str] | None = None):
    if mcp is None:
        raise SystemExit("fastmcp not installed. Run: uv sync  (core SDK works without it)")
    ap = argparse.ArgumentParser(description="AI memory MCP server")
    ap.add_argument("--transport", default=os.environ.get("AI_MEMORY_TRANSPORT", "stdio"),
                    choices=["stdio", "http"])
    ap.add_argument("--host", default=os.environ.get("AI_MEMORY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("AI_MEMORY_PORT", "8000")))
    ap.add_argument("--path", default=os.environ.get("AI_MEMORY_PATH", "/mcp"))
    ap.add_argument("--token", default=os.environ.get("AI_MEMORY_TOKEN"))
    ap.add_argument("--no-auth", action="store_true",
                    help="allow unauthenticated HTTP (loopback only)")
    args = ap.parse_args(argv)

    if args.transport == "stdio":
        mcp.run()
        return

    middleware = []
    if args.token:
        from starlette.middleware import Middleware

        middleware = [Middleware(BearerAuthMiddleware, token=args.token)]
    elif not args.no_auth and not _is_loopback(args.host):
        raise SystemExit("refusing to serve LAN without a token: set --token or AI_MEMORY_TOKEN")
    mcp.run(transport="http", host=args.host, port=args.port, path=args.path,
            middleware=middleware or None, show_banner=False)


def serve_main(argv: list[str] | None = None):
    """`ai-memory-serve`: HTTP by default, safe loopback default."""
    os.environ.setdefault("AI_MEMORY_TRANSPORT", "http")
    main(argv)


if __name__ == "__main__":
    main()
