# AI Memory — Human-Brain-Inspired, Local-First, Pluggable

> General memory layer for any agent harness. Local by default, pluggable via MCP.

## 1. Vision

Human memory is not one vector DB. It is layered, consolidating, forgetting, and reconstructive.
This system adapts that to AI:

- fast, lossy working memory for the current task
- high-fidelity episodic log (hippocampus)
- distilled semantic knowledge (neocortex)
- reusable procedures/skills (striatum/cerebellum)
- salience + reflection to decide what to keep (amygdala + prefrontal)

Non-goals v0.1: cloud sync, multi-user auth, perfect recall. Goals: **single `memory.db` file, zero docker, works offline, plugs into any MCP client.**

## 2. Brain Mapping

| Human system | AI equivalent | Store | Retention | Recall pattern |
|---|---|---|---|---|
| Sensory register | Raw ingest buffer | in-memory queue | seconds | filter by salience |
| Working memory (PFC) | Rolling context window | `working` (RAM + SQLite) | session | recency only |
| Hippocampus | Episodic log | `episodes` append-only | months, decayed | time + similarity |
| Neocortex | Semantic facts | `facts` (entity/key/value + vec + FTS) | years | hybrid search |
| Striatum | Procedural skills | `procedures` (trigger→steps) | years, reinforced | trigger match |
| Amygdala | Salience scorer | `importance 0-1` on every row | — | rerank boost |
| PFC reflection (sleep) | Consolidator worker | background job | periodic | dedupe, distill, forget |

Mental model: `capture everything cheap → score → consolidate nightly → recall hybrid → forget deliberately.`

## 3. Architecture Overview

```
                +------------------ any harness ------------------+
                | OpenCode / Claude Code / Cursor / custom Python |
                                |  MCP (stdio / http)  |  SDK
                                v                      v
                      +-------------------------------+
                      |          MCP Server           |
                      | memory_store / recall / forget|
                      | consolidate / stats / profile |
                      +---------------+---------------+
                                      |
        +-----------------------------+-----------------------------+
        |                             |                             |
   +----+----+                  +-----+------+               +------+------+
   | Capture |                  |   Recall   |               | Consolidator|
   | +Filter |                  |  (hybrid)  |               | (async job) |
   +----+----+                  +-----+------+               +------+------+
        | write                       | read                        | read/write
        v                             v                             v
+--------------------------------------------------------------------------+
|                              Storage (SQLite)                            |
|  episodes (+FTS) | facts (+FTS +vec) | procedures | embeddings | working  |
|                         single file: memory.db                           |
+--------------------------------------------------------------------------+
        ^
        | optional
+-------+--------+
| Embedding Prov.|
| hash (built-in)|
| s-transformers |
| ollama / openai|
+----------------+
```

### Request flows

**Store:** `harness → mcp.memory_store → salience.score → episodes.insert (+ facts.insert if type=fact) → embeddings.insert (async/best-effort)`

**Recall:** `query → embed(query) → FTS hits ∪ vec hits → recency*importance rerank → return with citations (source_episode_ids)`

**Consolidate (background):** `unprocessed episodes → LLM/local rules → candidate facts/procedures → dedupe/merge → mark episodes.processed → decay low-importance`

## 4. Data Model

```sql
-- raw experience, never updated except processed flag
episodes(id TEXT PK, ts REAL, actor TEXT, text TEXT, harness TEXT,
         importance REAL, processed INT DEFAULT 0);

-- distilled knowledge
facts(id TEXT PK, entity TEXT, key TEXT, value TEXT,
      confidence REAL, importance REAL, updated REAL,
      source_episodes TEXT); -- JSON list

-- reusable skills
procedures(id TEXT PK, trigger TEXT, steps TEXT,
           uses INT, success REAL, updated REAL);

-- working memory (short-lived)
working(session_id TEXT, ts REAL, role TEXT, text TEXT);

-- FTS5 (exact match)
episodes_fts(text, entity) / facts_fts(entity, key, value)

-- vectors (sqlite-vec, best-effort; fallback = FTS only)
vec_episodes(rowid, embedding float[384]) / vec_facts(...)
```

IDs: `mem_<ulid-ish>` via `time + random`. Timestamps: unix float UTC.

## 5. Storage Choice: Why SQLite + sqlite-vec + FTS5

- **Local-first:** one file, survives restarts, `scp` to backup.
- **Zero ops:** no Postgres/Redis/Docker for v0.1.
- **Hybrid recall without a server:** FTS5 for exact (names, IDs), `sqlite-vec` for fuzzy.
- **Graceful degradation:** if `sqlite-vec` or embedding model missing → FTS-only still works. Core has **zero required deps** (stdlib only).
- **Scale:** fine to ~1M episodes/facts on laptop. Migrate to Postgres pgvector later without API change.

## 6. Recall Ranking (v0.2 implemented)

```
candidates = fts_top_50 (rank-norm 1/(1+rank)) ∪ vec_top_50 (sqlite-vec cosine distance)
vec_sim = clip(1 - cosine_distance, 0, 1)
score = 0.5*vec_sim + 0.3*fts_norm + 0.15*importance + 0.05*recency_decay
recency_decay = exp(-0.01 * age_days)
```

- `vec0` tables `vec_episodes` / `vec_facts`, 384-dim, `serialize_float32`, indexed at store time.
- FTS5 query sanitized (`OR` of alnum tokens, LIKE fallback).
- Eval: `tests/test_eval.py`, 8 cases (exact + paraphrase + procedural), `recall@3 = 1.0` **with correct top-1 on all 8**, both `hash` and `st` providers. FTS MATCH drops stopwords (`user/like/what/...`) so lexical noise can't outvote vector signal.

## 7. Salience, Consolidation, Forgetting

**Salience (amygdala) v0.1 rules, LLM later:**
- 0.9-1.0: user says "remember", "always", "never", profile facts
- 0.6-0.9: decisions, prefs, errors + fixes
- 0.3-0.6: normal task context
- 0.0-0.3: greetings, noise → dropped on consolidation

**Consolidator (PFC during sleep):**
1. fetch `episodes WHERE processed=0 LIMIT 50`
2. extract `facts/procedures` (regex rules now, LLM later)
3. merge: same `(entity,key)` → update value, `confidence = max + 0.1`, append sources
4. mark processed, decay: `importance *= 0.99` per run if never recalled

**Forgetting:** explicit `memory_forget(id|query)` always wins. Plus passive decay + `consolidate --vacuum` drops `importance<0.05 and age>90d`.

## 8. Pluggability (MCP-first)

MCP server `ai-memory` (stdio for local, http for shared):

- Tools: `memory_store`, `memory_recall`, `memory_forget`, `memory_consolidate`, `memory_stats`
- Resources: `memory://profile`, `memory://episodic/{date}`, `memory://stats`
- SDK: `sdk.py` wraps same `Memory` class for in-process use (tests, scripts).

Add to any harness (`mcp.json`):
```json
{ "mcpServers": { "ai-memory": { "command": "uv", "args": ["run", "ai-memory-mcp"] } } }
```

Harness-agnostic: `harness` field tags provenance (`opencode`, `cursor`, etc.) so shared DB doesn't collide.

## 9. Privacy & Safety

- local file by default, no telemetry.
- `forget` is hard delete (row + vec + FTS).
- PII: `entity=user` namespace isolated; export/delete per-entity.
- Future: encryption at rest (sqlcipher), per-harness tokens.

## 10. Build Plan

- [x] **v0.1 Core:** `db.py`, `store/recall/forget`, `server.py`, `sdk.py`, FTS + hash-vec fallback, tests pass offline
- [x] **v0.2 Recall quality (done):** real `sqlite-vec` (`vec0`, cosine distance) + `sentence-transformers`/`ollama` embedders, hybrid rerank, `tests/test_eval.py`
- [x] **v0.3 Brain (done):** multi-extractor consolidator (`my X is Y`, remember, decisions, error→fix, runbooks), merge-on-same-key, procedural mining, decay + `vacuum_old` (FTS+vec consistent), `ai-memory-consolidate [--loop]` worker, `tests/test_consolidate.py` (distill/merge/vacuum)
- [x] **v0.4.1 MCP hardening:** thread-safe SDK (`check_same_thread=False` + RLock), OpenCode global plug + `ai-memory` skill + seeded profile
- [x] **v0.5 HTTP transport:** `ai-memory-serve` (streamable HTTP `/mcp`), Bearer-token middleware, LAN-without-token refusal, `tests/test_http.py`
- [x] **v0.6 LLM hook + maintenance:** opt-in `AI_MEMORY_LLM=ollama|openai`, `memory_maintenance` (integrity, orphans, FTS optimize, VACUUM), `tests/test_llm_maint.py`
- [x] **v0.7 OpenCode plugin:** global `ai-memory` plugin — first-turn profile briefing via `context` hook (CLI recall, best-effort, per-session flag), `/remember` command (store + consolidate), `recall/store/consolidate` CLI subcommands, hook trace at `/tmp/ai-memory-hook.log`
- [x] **v0.8 Safety net (done):** `forget` soft-deletes into `trash` (unrecallable immediately, `memory_restore` within 30d, auto-purge in vacuum), `backup --keep N` retention, daily 02:00 cron (`backup --keep 14`), `tests/test_safety.py`

## 11. Repo Layout

```
ai-memory/
  architecture.md
  README.md
  pyproject.toml
  src/ai_memory/
    __init__.py  config.py  db.py  models.py
    embeddings.py  salience.py  store.py  recall.py
    consolidator.py  server.py  sdk.py
  tests/test_basic.py
```

See `README.md` for quickstart.
