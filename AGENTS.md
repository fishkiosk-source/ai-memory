# ai-memory — project instructions

- Quick checks: system `python3` + `PYTHONPATH=src` (never `uv add` for one-offs — it relocks the project).
- Full suite: `AI_MEMORY_EMBED=st uv run pytest -q` (ST cold start is slow; offline fallback `AI_MEMORY_EMBED=hash`).
- `memory.db`, `*.db-shm`, `*.db-wal`, `*.bak` are gitignored — never commit them.
- MCP startup timeout is 60s (ST embedder). SDK is `RLock`-guarded for FastMCP worker threads.
- Single-file DB, offline-first. Design docs in `architecture.md`; user wiki on GitHub.
