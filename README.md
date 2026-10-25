# ai-memory

Long-term memory for AI agents that you own and can inspect. One SQLite file,
local embeddings, an MCP server, and a CLI. No cloud, no account, no vector
database to run.

Agents forget everything between sessions. This gives them a place to put what
they learned — preferences, decisions, runbooks — and a way to get the relevant
part back on the next prompt instead of re-reading a transcript.

- **Hybrid recall**: sqlite-vec cosine + FTS5 + importance + recency + frequency,
  fused into one score (`recall@3 = 1.0` on the 11-case fixture eval).
- **It forgets on purpose**: same-key writes supersede older values, cross-key
  near-duplicates auto-resolve, and `forget` soft-deletes into a 30-day trash.
- **Consolidation worker**: episodes distill into facts and procedures with
  rules; an LLM hook is opt-in for the cases rules can't parse.
- **Auditable**: `doctor facts` is a read-only audit of conflicts, duplicate
  keys, and dead index slots. Nothing is hidden in a remote service.
- **Portable**: JSONL export/import, timestamped backups with retention,
  namespaces per project.
- **Embedder safety**: a failed embedding model fails closed (text-only rows)
  instead of writing incomparable vectors into an existing index.

Python 3.10+, SQLite, single-user scale (thousands of rows is the comfortable
range). See [`architecture.md`](architecture.md) for the design.

## Install

```bash
git clone https://github.com/fishkiosk-source/ai-memory
cd ai-memory
uv sync
```

`uv sync` installs the package plus `sentence-transformers` and `sqlite-vec`.
Offline or CPU-only box? Set `AI_MEMORY_EMBED=hash` to skip the model download;
recall quality drops (no paraphrase matching) but everything else works.

Embedders (`AI_MEMORY_EMBED`): `st` / `sentence-transformers` (all-MiniLM-L6-v2,
the quality default) | `hash` (built-in, offline, lexical only) | `off`
(text-only writes, no vectors) | `ollama` (nomic-embed-text via `ollama serve`).

## Use it from an MCP client

```json
{ "mcpServers": { "ai-memory": {
  "command": "uv",
  "args": ["--directory", "<path-to>/ai-memory", "run", "ai-memory-mcp"],
  "env": { "AI_MEMORY_EMBED": "st", "AI_MEMORY_DB": "<path-to>/ai-memory/memory.db" }
} } }
```

That is the generic shape (Claude Desktop, Cursor, most clients). OpenCode uses
`{"mcp": {"servers": {...}}}` with `"type": "local"` and a command array; add
`"codemode": true` there if you want the tools callable from Code Mode sessions.

Tools: `memory_store`, `memory_recall`, `memory_recent`, `memory_consolidate`,
`memory_forget`, `memory_restore`, `memory_stats`, `memory_temporal`,
`memory_export`, `memory_import`, `memory_vacuum`, `memory_maintenance`,
`memory_backfill_vecs`, `memory_doctor_facts`, `memory_supersede`.

## CLI and SDK

```bash
uv run ai-memory store "user prefers dark mode" --kind fact --entity user --key theme
uv run ai-memory recall "what theme does user like?" -k 5
uv run ai-memory recall "plasticity" --since 2026-10-15 --until 2026-10-17
uv run ai-memory stats
```

```python
from ai_memory.sdk import Memory

m = Memory(db_path="memory.db", embed_provider="st")
m.store("user prefers dark mode", kind="fact", entity="user", key="theme")
print(m.recall("what theme does user like?"))
print(m.stats())  # includes vec_episodes / vec_facts counts
```

## How recall scores

Candidates come from FTS5 (bm25-ordered, top 50) and from the vector index
(top 50), then get one fused score:

| signal | weight |
| --- | --- |
| vector similarity | 0.45 |
| FTS rank (normalized) | 0.30 |
| importance | 0.15 |
| recency (λ = 0.01) | 0.05 |
| access frequency | 0.05 |

Superseded and trashed rows never surface. Recency is deliberately light — it
does not age out a fact you still rely on.

## Sleep cycle (consolidation)

```bash
uv run ai-memory-consolidate                            # one pass
uv run ai-memory-consolidate --loop --interval 300 --vacuum   # daemon
```

Distills unprocessed episodes into facts (`user.*`, `decisions.*`, `learnings.*`)
and procedures. Session wraps mine up to 6 procedures per episode: numbered
lists, `(1)`-style parenthetical items (`(part N)` triggers), `Fixed X with/by Y`,
`Next:/Remaining:` follow-ups, `gotcha ... so run Y`, and explicit
`proc: trigger -> steps` (also routable via `store(kind="procedure")`).
Near-duplicate procedures (vec sim ≥ 0.90) merge into the existing row and bump
`uses`, so repeated sessions don't pile up junk. `--reprocess` re-extracts
already-processed episodes after a rule change (a real backfill went 12 → 44
procedures over 78 episodes with zero LLM calls).

## Fact conflict policy

Same `(entity, key, ns)` → reinforce (confidence +0.05) or supersede: the new
value wins, the old row becomes `status='superseded'`, and its FTS/vec slots are
dropped immediately. Cross-key near-duplicate (vec sim ≥ 0.88, same entity and
namespace, new confidence within −0.10 of the old) → auto-supersede on write.
It only runs when both rows have vectors; without them the write is a plain
insert. Anything between 0.85 and 0.88 stays visible for `doctor`.

## Facts doctor + explicit verdicts

```bash
uv run ai-memory --embed st doctor facts                    # read-only audit
uv run ai-memory supersede --old fact_abc --new fact_def     # owner verdict
```

`doctor facts` reports cross-key conflicts (same entity and namespace, sim ≥
threshold, different keys), same-key double-actives, dead FTS/vec slots
(including ghost index rows), and active facts missing vectors. It never mutates.
`supersede` marks the old fact `superseded` with `superseded_by` and drops its
index slots; the value stays in the audit trail. `backfill-vecs` fills missing
vectors for **active** rows only and evicts vec slots of superseded ones.
MCP equivalents: `memory_doctor_facts`, `memory_supersede`.

## Portability and namespaces

```bash
uv run ai-memory export -o backup.jsonl
uv run ai-memory import -i backup.jsonl    # vectors recomputed with current embedder
uv run ai-memory backup --keep 5           # timestamped copy, prune to newest 5
uv run ai-memory forget --query "oops"     # soft-delete into trash
uv run ai-memory restore --query "oops"    # undo within 30 days
uv run ai-memory recall "what did we decide?" -k 5 --namespace myproject
uv run ai-memory backfill-vecs             # recompute rows missing from vec tables
```

`forget` never hard-deletes: rows move to `trash`, are immediately unrecallable,
restorable for 30 days, then purged by vacuum. Suggested upkeep:
`backup --keep 5` daily, `ai-memory-consolidate` every 30 minutes,
`maintenance` weekly.

Namespaces isolate projects (`AI_MEMORY_NS=work` or per-call `namespace=`). Facts
merge per namespace, consolidation never leaks across, and old DBs auto-migrate
to `ns='default'`.

## OpenCode plugin (automatic memory)

`integrations/opencode/` is a plugin that removes the "remember to call the tool"
step: a session briefing on the first model call, task-relevant recall on every
prompt, auto-capture on idle/compaction, and `/remember` `/recall` `/forget`
`/stats` `/consolidate` commands.

```bash
integrations/opencode/install.sh
```

It keeps one warm `ai-memory-mcp` child over stdio (`AI_MEMORY_EMBED=st`), so the
model load is paid once at editor start and hooks get semantic recall in ~17ms.
If that child is dead or still warming, hooks fall back to the CLI with
`AI_MEMORY_EMBED=off` (FTS-only recall, text-only writes that the worker
backfills). A broken child degrades recall quality; it never breaks a prompt.
See [`integrations/opencode/README.md`](integrations/opencode/README.md) for
config knobs (`AI_MEMORY_PROJECT`, `AI_MEMORY_DB`, `AI_MEMORY_PLUGIN_MCP=off`,
`AI_MEMORY_HOOK_TRACE`).

## LAN / second PC (HTTP transport)

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> TOKEN
export AI_MEMORY_TOKEN=<TOKEN> AI_MEMORY_EMBED=st
uv run ai-memory-serve --host 0.0.0.0 --port 8000
# serving LAN without a token is refused; loopback may use --no-auth
```

Allow the port through the firewall and point the other machine at
`http://<LAN-IP>:8000/mcp`:

```json
{ "mcp": { "servers": { "ai-memory-lan": {
  "type": "remote", "url": "http://<LAN-IP>:8000/mcp", "oauth": false,
  "headers": { "Authorization": "Bearer {env:AI_MEMORY_TOKEN}" }
} } } }
```

Plain HTTP is fine on a trusted LAN. Don't expose the port to the internet
without TLS in front — the token is a shared secret, not identity.

## LLM distillation (opt-in) and maintenance

Rules stay the default (offline). For episodes rules can't parse:

```bash
export AI_MEMORY_LLM=ollama AI_MEMORY_LLM_MODEL=llama3.1:8b   # needs ollama serve
# or: export AI_MEMORY_LLM=openai AI_MEMORY_LLM_MODEL=gpt-4o-mini AI_MEMORY_LLM_API_KEY=...
uv run ai-memory-consolidate --llm ollama
```

Only rule-misses with importance ≥ 0.4 cost a call (budget 20/run); malformed
output is ignored and LLM errors degrade to rules-only. Keep the DB fast with
`uv run ai-memory maintenance` (integrity check, orphan purge, FTS optimize,
VACUUM, reclaim report) or the `memory_maintenance` MCP tool.

### Reloading the MCP server (OpenCode)

OpenCode does not retry an MCP server after its stdio child dies: the connection
stays `failed` until something calls the reconnect endpoint. After changing
server code, use the helper instead of a bare `kill`:

```bash
scripts/mcp-reload.sh              # kill ai-memory-mcp children + reconnect
scripts/mcp-reload.sh --no-kill    # reconnect only (child died on its own)
scripts/mcp-reload.sh --dry-run    # show what would be killed
```

It reads `OPENCODE_SERVER_PASSWORD` from the `opencode serve` process env, POSTs
`/api/experimental/mcp/<name>/connect`, and polls until `connected` (a cold ST
load is 10-20s). Only descendants of the HTTP server are killed, so an
`opencode serve --service` instance (no HTTP port) keeps its connection.

## Tests

```bash
AI_MEMORY_EMBED=hash uv run pytest -q    # fast, offline
AI_MEMORY_EMBED=st uv run pytest -q      # same suite with real embeddings
```

The suite covers the write semantics (supersede, reinforce, cross-key conflict),
recall ranking, extraction rules, the consolidation worker, portability,
backfill, the doctor audit, and a real MCP stdio round trip
(`tests/test_mcp_stdio.py`).

## Changelog

- **v0.9.3** — OpenCode plugin moved in-tree (`integrations/opencode/`) and now
  keeps a warm MCP child instead of shelling out per hook: semantic recall at
  ~17ms and writes with real vectors immediately.
- **v0.9.2** — correctness pass found by exercising the MCP: `doctor facts`
  scoped its missing-vec count to active rows; `AI_MEMORY_EMBED` is read per
  instance and a failed non-hash embedder fails closed; `recall()` orders FTS by
  bm25 (rank used to follow rowid, so an exact new match landed 17th of 18);
  added `AI_MEMORY_EMBED=off`, shared `vecfill.py`, and post-consolidate
  backfill.
- **v0.9.1** — fact conflict policy: same-key supersession, cross-key
  auto-resolution on write, `doctor facts` audit, `supersede` verdicts.
- **v0.9.0** — procedure farming: multi-procedure extraction per episode,
  parenthetical `(1)` bullets, semantic dedupe, `--reprocess` backfill.
- **v0.8** — OpenCode harness v2: briefing, per-prompt recall, auto-capture.

## License

MIT — see [`LICENSE`](LICENSE).
