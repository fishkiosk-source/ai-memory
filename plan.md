# ai-memory plan of record — 2026-10-25 (rev 2, after v0.9.2)

Working plan for the agreed priorities. Rev 1 covered Track 1 (fact
conflict/staleness) and Track 2 (procedure farming); both shipped in v0.9 /
v0.9.1. Rev 2 records what shipped, the v0.9.2 correctness fixes found while
sanity-checking the MCP, and adds Track 3 (the hook recall path — the part the
plugin uses on every prompt). Decisions are closed unless reopened in "Deferred".

## Ground truth verified (supersedes earlier session claims)

- Supersession EXISTS for same-key: `store_fact()` (store.py) re-indexes
  `status='superseded', superseded_by` when entity+key+ns match and value
  differs. Same value = reinforce in place (confidence +0.05, uses/merged src).
- Superseded facts never surface in recall: `_fetch()` (recall.py) filters
  `status='active'` on every path.
- Cross-key conflicts are the real blind spot: two facts about the same topic
  with different slug keys stay 'active' forever unless the write path resolves
  them (T1.2, shipped) — the rest are reported by `doctor facts`.
- Ollama: NOT installed. Any LLM hook must target llamacpp.service
  (http://127.0.0.1:8082/v1, model gemma4, 0.929-strict extraction champion).
  **No new services allowed.** Regex extraction is the hard path; LLM is opt-in.
- OpenCode does NOT retry an MCP server after its stdio child dies: the
  connection stays `failed` until `/api/experimental/mcp/<name>/connect` is
  called. Never bare-kill ai-memory-mcp — use `scripts/mcp-reload.sh`.
- Hash and ST embeddings are NOT interchangeable (cosine ~0). Any writer that
  embeds must use the embedder the index was built with. `AI_MEMORY_EMBED=off`
  is the honest way to skip vectors.
- ST cold load costs ~10s per CLI invocation; hash/off costs ~0.22s. Per-prompt
  hooks (8s budget) therefore cannot use ST through the CLI.

## Shipped (v0.9 / v0.9.1 / v0.9.2)

Track 1 — fact conflict + staleness:
- [x] T1.1 `doctor facts` read-only audit (`doctor.fact_audit`, CLI + MCP tool).
- [x] T1.2 Write-time semantic conflict resolution (`store_fact`, vec sim ≥ 0.88,
      confidence window -0.10, cross-key auto-supersede).
- [x] T1.3 Supersede slot hygiene (`_supersede_row` drops facts_fts + vec_facts;
      `backfill_vecs` evicts superseded slots, fills active rows only).
- [ ] T1.4 Staleness pressure — deferred; recency weight 0.05 is cosmetic.
- [x] T1.5 Recall eval at 11 cases (`tests/test_eval.py`, recall@3 = 1.0 ST).
      Expanding to ~20 cases (stale + conflict fixtures) still open.

Track 2 — procedure farming:
- [x] T2.1 Multi-procedure extraction (`extract_multi` tests).
- [x] T2.2 Parenthetical `(1)`-style bullet widening.
- [x] T2.3 Reprocess backfill on prod (12 → 44 procedures, llm_calls 0).
- [x] T2.4 Semantic procedure dedupe (PROC_MERGE_SIM 0.90, bumps uses).
- [ ] T2.5 LLM assist — not needed (rules yield was sufficient).
- [ ] T2.6 Procedure precision eval — open.

v0.9.2 correctness fixes (found by exercising the MCP, not by reading code):
- [x] `doctor facts` counted superseded facts as "missing vec rows" forever
      (backfill only fills active) → scoped to active rows.
- [x] `Memory(embed_provider="hash")` hard default ignored `AI_MEMORY_EMBED`;
      a failed ST load silently became hash → env honored per instance, non-hash
      failure fails closed (text-only). 7 prod rows re-embedded with ST.
- [x] `recall()` ran FTS without `ORDER BY bm25()`, so rank followed rowid and
      fts_norm rewarded age (new exact match ranked 17/18) → bm25 ordering.
- [x] `AI_MEMORY_EMBED=off` mode + `vecfill.py` shared backfill + worker
      backfills after each consolidate run.
- [x] OpenCode: `ai-memory` MCP now `codemode: true` (15 tools in the Code Mode
      catalog); `scripts/mcp-reload.sh` for kill + reconnect; reconnect procedure
      documented in README and the ai-memory skill.

## Track 3 — hook recall path (the plugin, i.e. what runs on every prompt)

Problem: the plugin shells out to the CLI, so it must run with
`AI_MEMORY_EMBED=off` (ST would cost 10s per call). Hook recall is therefore
FTS/importance only, and hook writes are text-only until the worker backfills
(≤30 min). Measured cost on `tests/test_eval.py`: off = recall@3 0.909, st = 1.0.
The single failure is a pure paraphrase ("what theme does user like?" vs "dark
mode"), which is exactly the class hooks exist for.

Constraint: **no new services.** So no systemd unit, no extra port, no auth.
The warm process must be something OpenCode already starts or the plugin owns.

Design: the plugin spawns ONE long-lived `ai-memory-mcp` child over stdio
(JSON-RPC, newline-delimited) with `AI_MEMORY_EMBED=st`, and talks to it for
both recall and store. It is a child of the OpenCode plugin, dies with OpenCode,
and needs no supervision beyond the plugin's own restart logic. The CLI `off`
path stays as the fallback so a dead child degrades to today's behavior.

- [x] T3.1 `tests/test_mcp_stdio.py`: spawn `ai-memory-mcp`, `initialize`,
      `notifications/initialized`, `tools/call memory_store` + `memory_recall`,
      assert round trip and that the stored row has a vector. Found the real
      result shape on the way: FastMCP spreads dict returns into
      `structuredContent` but wraps scalars/lists as `{"result": X}` — the
      client must handle both (test helper and plugin both do).
- [x] T3.2 Plugin client (`index.ts`): spawn on setup, handshake once, serialize
      calls through a promise chain, per-call timeout (recall 2-4s, store 5-8s),
      restart on child exit (max 3, then 5min dead window), `ready` promise that
      hooks await with a 1.5s cap; any failure → CLI fallback. Cold handshake
      measured at 9.8s, so the spawn happens in `setup()` (editor start), never
      inside a prompt hook.
- [x] T3.3 Wire hooks: briefing (3 recalls), per-prompt recall, autostore,
      throttled consolidate, and `/remember` go through the child;
      `AI_MEMORY_EMBED=off` remains only for the fallback path. Namespace passed
      per call (`namespace` arg).
- [x] T3.4 Verified by execution: warm recall p50 **17ms** (first call 237ms
      while the encoder warms), store 19ms, and a row stored through the child is
      the top hit on the very next recall — vectors exist at write time.
      Paraphrase probe ("how does he feel about long planning documents?")
      returns `user.collab_style` (0.687) through the child while the FTS-only
      path does not. Prod `doctor facts`: 0 conflicts, 0 dead fts/vec rows.
      Suite: 69 passed on both hash and ST.
- [x] T3.5 Docs: README plugin section, skill note (extra `ai-memory-mcp`
      children are expected), this plan updated with measured numbers.
      Rollback = flip the plugin back to `cli()` only.

Cost note: one child per OpenCode server means 4 ST processes on this box (2
MCP servers + 2 plugin children), ~3.4GB RSS nominal, ~9GB still available.
Accepted: recall quality on the hot path is worth it, and nothing new to
supervise.

Acceptance criteria:
1. [x] `tests/test_mcp_stdio.py` green in the normal suite (hash + ST gate).
2. [x] Hook recall for a paraphrase query returns the semantic match.
3. [x] Hook writes have vec rows at write time; worker backfill reports 0 missing.
4. [ ] Child death (kill -9) does not break prompts: fallback injection still
       fires. (Fallback path is the old, already-verified CLI code; still wants a
       deliberate kill test in a live session.)
5. [x] No new service, no new port, no new secret.

## Deferred (decided, not now)

- Embedder upgrade: user has a candidate in mind; park until Track 3 lands.
- Namespace auto-routing by content.
- LLM hook provider rename: keep `openai` provider + document base-URL knob
  pointing at llamacpp; do not implement `ollama` provider theory on this box.
- T1.4 staleness weighting and T2.6 procedure eval (both need bigger fixtures).

## Test & verification protocol

- Fast loop: `AI_MEMORY_EMBED=hash uv run pytest -q`; final gate: ST embedder.
- Every behavior change gets a fixture test; backfills verified on a COPY of
  memory.db first (backup --keep exists; daily 02:00 cron live).
- Never run destructive phases against prod before the copy run diff looks right.
- MCP changes: reload with `scripts/mcp-reload.sh`, then verify
  `GET /api/mcp` says `connected` and the tool catalog still lists ai-memory.
