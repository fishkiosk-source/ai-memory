# ai-memory plan of record — 2026-10-25

Working plan for the two agreed priorities: fact conflict/staleness (Track 1)
and procedure farming (Track 2). Written after code re-read + live probe of the
read/write paths; corrections to earlier session claims are recorded inline.
Decisions are closed unless reopened in "Deferred".

## Ground truth verified today (supersedes earlier session claims)

- Supersession EXISTS for same-key: `store_fact()` (store.py) re-indexes
  `status='superseded', superseded_by` when entity+key+ns match and value
  differs. Same value = reinforce in place (confidence +0.05, uses/merged src).
- Superseded facts never surface in recall: `_fetch()` (recall.py) filters
  `status='active'` on every path. Earlier claim "old supersedes score fine"
  was WRONG.
- Recency decay exists (`_recency`, lambda=0.01) but is cosmetically weightless:
  W_REC=0.05 → a 90-day-old fact loses ~0.02 of total score. Effectively no
  staleness pressure. This gap is real.
- Cross-key conflicts are the real blind spot: two facts about the same topic
  with different slug keys are both 'active' forever; nothing detects the
  collision.
- Candidate-budget leak: on supersede the old row keeps its facts_fts + vec
  entries; dead rows still consume the LIMIT-50 candidate slots before being
  dropped by `_fetch` (wasted budget, plus audit-only value).
- `extract()` (consolidator.py) yields AT MOST ONE procedure per episode — the
  `if not procs:` fallback chain short-circuits. Session wraps with 3–4 items
  lose the rest. This is the #2 farming bottleneck; the regex gap for
  `(1)`-style parenthetical bullets is #3 (NUMBERED_LIST_RE only matches
  `\d+[.)]` and `-`).
- Ollama: NOT installed on this box. LLM hook path, if ever used, must target
  llamacpp.service (http://127.0.0.1:8082/v1, OpenAI-compatible, model gemma4 —
  the 0.929-strict extraction champion). No new services allowed; offline-first
  stands: regex extraction is the hard path, LLM is opt-in only.
- Prod DB is tiny leverage: 78 episodes / 51 facts / 12 procedures. Backfill
  runs are minutes, not hours.

## Track 1 — fact conflict + staleness (read/write path)

Goal: conflicting facts auto-resolve or flag; stale facts stop crowding recall.
No destructive deletes; supersede/trash semantics stay.

- [ ] T1.1 `memory-doctor facts` (read-only audit first, zero mutation).
      Groups facts by (entity, ns); finds near-duplicate/conflict pairs via
      cosine on existing vec_facts (same entity, sim ≥ ~0.85, different
      key/value); prints pairs + suggested action. Purpose: real numbers before
      automating anything. Also reports superseded rows still holding vec/fts
      slots.
- [ ] T1.2 Semantic conflict detection on write. In `store_fact()`: new fact
      (entity, ns) with vec sim ≥ threshold against an existing 'active' fact
      with a DIFFERENT key → conflict. If confidence comparable
      (-0.1..+0.1 window): auto-supersede (new wins, old superseded_by new).
      If clearly weaker: drop with logged note. Ambiguous: store both, mark
      pair `status='conflict'`. Optional LLM adjudication (llamacpp hook) later
      as opt-in; not required for v1 of this feature.
- [ ] T1.3 Candidate-budget hygiene. On supersede, remove the old row from
      facts_fts and vec_facts (facts table row kept with status columns for
      audit/restore). Backfill cleanup for historical degrees.
- [ ] T1.4 Staleness that bites. Options measured against recall eval:
      (a) raise W_REC, (b) importance touch: recall reinforcement already
      exists via access_log — add optional time-since-'updated' divisor into
      fact importance presented to scorer. Decide after eval A/B; do not ship
      both blind.
- [ ] T1.5 Expanded recall eval: current eval set is 8 queries (proves design,
      guards little). Add ~20 queries incl. stale-fact and conflict pairs
      fixtures.

## Track 2 — procedure farming

Goal: 12 procedures → representative coverage of what sessions actually teach.
Regex-first, zero new services.

- [ ] T2.1 Multi-procedure extraction. Allow extract() to return >1 procedure
      per episode: restructure fallback chain so RUNBOOK / NUMBERED /
      FIRST_THEN / etc. can each contribute (with per-pattern dedupe) instead
      of first-match-wins.
- [ ] T2.2 Regex widening: parenthetical bullets `(1)` `(2)`, "(3) ..." wraps.
      Cheap, verifiable on real session-wrap texts.
- [ ] T2.3 Reprocess backfill (rules only, no LLM): `reprocess=true` over all
      episodes, measure yield delta + sample quality. Expect large jump in
      procedure count; verify no duplicate-trigger floods into procedures
      table.
- [ ] T2.4 Semantic procedure dedupe: store_procedure() currently dedupes on
      exact trigger text only. Add vec-sim merge on trigger (sim ≥ ~0.9 →
      update steps of winner, bump uses) so repeated sessions don't multiply
      rows. Fall back to exact match when embedder off.
- [ ] T2.5 Optional LLM assist (only if T2.3 yield still thin on sample):
      `extract_with_llm` pointed at llamacpp gemma4, opt-in env, importance
      gate unchanged. Off by default. Never a dependency.
- [ ] T2.6 Procedure quality eval: fixture episodes → expected
      (trigger, steps) pairs; assert precision on mined set like recall eval.

## Deferred (decided, not now)

- Embedder upgrade: user has a candidate in mind; park until Track 1/2 land.
- Namespace auto-routing by content.
- LLM hook provider rename: keep `openai` provider + document base-URL knob
  pointing at llamacpp; do not implement `ollama` provider theory on this box.

## Test & verification protocol

- Fast loop: `AI_MEMORY_EMBED=hash uv run pytest -q`; final gate: ST embedder.
- Every behavior change gets a fixture test; backfills verified on a COPY of
  memory.db first (backup --keep exists; daily 02:00 cron live).
- Never run destructive phases (T1.3 backfill, T2.3 reprocess) against prod
  before the copy run diff looks right.
