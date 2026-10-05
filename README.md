# AI Memory — local-first, brain-inspired, MCP-pluggable

v0.8 + OpenCode harness v2. See `architecture.md` for design.

- Hybrid recall (sqlite-vec + FTS5 + importance + recency), `recall@3 = 1.0` on 8-case eval
- Consolidation worker (rules + opt-in LLM), namespaces, export/import, backup retention
- Safety net: `forget` soft-deletes into `trash` (30d restore), vacuum purges
- OpenCode harness: project-scoped briefing, per-prompt recall, auto-capture on idle, `/remember /recall /forget /stats /consolidate`

## Quickstart (offline, hash embedder)

```bash
cd ai-memory
python3 -m tests.test_basic
AI_MEMORY_EMBED=hash python3 -m tests.test_eval
```

## Full (real embeddings)

```bash
cd ai-memory
uv sync
AI_MEMORY_EMBED=st uv run pytest -q        # sentence-transformers all-MiniLM-L6-v2
AI_MEMORY_EMBED=hash uv run pytest -q      # offline fallback
uv run ai-memory-mcp
```

Embedders (`AI_MEMORY_EMBED`): `hash` (built-in, offline) | `st` / `sentence-transformers` (default for quality) | `ollama` (nomic-embed-text via `ollama serve`).

## SDK

```python
from ai_memory.sdk import Memory
m = Memory(db_path="memory.db", embed_provider="st")
m.store("user prefers dark mode", kind="fact", entity="user", key="theme")
print(m.recall("what theme does user like?"))
print(m.stats())  # includes vec_episodes / vec_facts counts
```

## Sleep cycle (consolidation worker)

```bash
uv run ai-memory-consolidate                 # one pass
uv run ai-memory-consolidate --loop --interval 300 --vacuum   # daemon
```

Distills unprocessed episodes → facts (`user.*`, `decisions.*`, `learnings.*`) + procedures, merges same-key updates, decays trivia, vacuums old junk (FTS + vec index kept consistent).
Procedures mine `Fixed X with Y`, `Next:/Remaining:` follow-ups, `gotcha ... so run Y`,
and explicit `proc: trigger -> steps` (also routable via `store(kind=procedure)`).
`--reprocess` re-extracts already-processed episodes after rule changes.

## Portability + namespaces

```bash
uv run ai-memory export -o backup.jsonl
uv run ai-memory import -i backup.jsonl   # vectors recomputed with current embedder
uv run ai-memory backup --keep 14         # timestamped copy, prune to newest 14
uv run ai-memory restore --query "oops"   # undo a forget within 30 days
uv run ai-memory forget --query "oops"    # soft-delete into trash (same as memory_forget)
uv run ai-memory recall "what did we decide?" -k 5 --namespace myproject
```

`forget` never hard-deletes: rows move to `trash` (immediately unrecallable),
restorable for 30 days, then auto-purged by vacuum. Scheduled upkeep
(see `crontab -l`): `backup --keep 14` daily 02:00, `ai-memory-consolidate`
every 30min, `maintenance` Sundays 03:00.

## OpenCode harness (plugin `ai-memory`)

Global plugin at `~/.config/opencode/plugins/ai-memory` (auto-reloads on edit,
trace at `/tmp/ai-memory-hook.log`):

- First model call: project-scoped briefing (profile + decisions + procedures
  for the project namespace derived from the working directory) plus usage rules.
- Every prompt: task-relevant recall prepended (`[Relevant memory ns=...]`).
- On `session.idle` / `session.compacted`: auto-store episode (90s debounce) +
  throttled consolidate (1x per 10min per namespace).
- Commands: `/remember <text>` (`proc: trigger -> steps` forces procedure
  capture), `/recall <query>`, `/forget <text>`,
  `/stats`, `/consolidate --reprocess` (backfill after rule changes).

Namespaces isolate projects (`AI_MEMORY_NS=work` or per-call `namespace=`); facts merge per-namespace, consolidation never leaks across, old DBs auto-migrate (`ns='default'`).

## LAN / second PC (HTTP transport)

On this machine (one-time token + serve):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"  # -> TOKEN
export AI_MEMORY_TOKEN=<TOKEN> AI_MEMORY_EMBED=st
uv run ai-memory-serve --host 0.0.0.0 --port 8000
# serving LAN without a token is refused; loopback may use --no-auth
```

Allow the port through the firewall (`sudo ufw allow 8000/tcp`) and find this
machine's LAN IP (`hostname -I`).

On the other PC, in OpenCode (`opencode mcp add ai-memory-lan --global --url http://<LAN-IP>:8000/mcp`),
then add auth in its `~/.config/opencode/opencode.json`:

```json
{ "mcp": { "servers": { "ai-memory-lan": {
  "type": "remote", "url": "http://<LAN-IP>:8000/mcp", "oauth": false,
  "headers": { "Authorization": "Bearer {env:AI_MEMORY_TOKEN}" }
} } } }
```

with `AI_MEMORY_TOKEN=<same TOKEN>` in that machine's environment. Plain HTTP is
fine on a trusted LAN; don't expose the port to the internet without TLS
(reverse proxy) — the token is a shared secret, not identity.

## LLM distillation (opt-in) + maintenance

Rules stay the default (offline). For episodes rules can't parse:

```bash
export AI_MEMORY_LLM=ollama AI_MEMORY_LLM_MODEL=llama3.1:8b  # needs ollama serve
# or: export AI_MEMORY_LLM=openai AI_MEMORY_LLM_MODEL=gpt-4o-mini AI_MEMORY_LLM_API_KEY=...
uv run ai-memory-consolidate --llm ollama
```

Only rule-misses with importance ≥ 0.4 cost an LLM call (budget 20/run); malformed
output is ignored, LLM errors degrade to rules-only. Keep the DB fast with
`uv run ai-memory maintenance` (integrity check, orphan purge, FTS optimize,
VACUUM + reclaim report) or the `memory_maintenance` MCP tool.

## MCP (`mcp.json`)

```json
{ "mcpServers": { "ai-memory": {
  "command": "uv",
  "args": ["--directory", "/home/llm/ai-memory", "run", "ai-memory-mcp"],
  "env": {"AI_MEMORY_EMBED": "st", "AI_MEMORY_DB": "/home/llm/ai-memory/memory.db"}
} } }
```
