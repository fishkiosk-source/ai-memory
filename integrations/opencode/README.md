# OpenCode plugin for ai-memory

Gives OpenCode automatic long-term memory without the model remembering to call
tools: a session briefing on the first model call, task-relevant recall on every
prompt, auto-capture on idle/compaction, and `/remember` `/recall` `/forget`
`/stats` `/consolidate` commands.

## How it talks to ai-memory

Two paths, in order of preference:

1. **Hot path** — one long-lived `ai-memory-mcp` child over stdio, spawned at
   plugin setup with `AI_MEMORY_EMBED=st`. The embedding model load (~10s) is
   paid once when the editor starts, so hooks get semantic recall in ~17ms and
   writes land with real vectors immediately. The child is owned by the plugin:
   it dies with OpenCode, and there is no service, port, or secret to manage.
2. **Fallback** — if the child is dead or still warming, hooks shell out to the
   `ai-memory` CLI with `AI_MEMORY_EMBED=off` (FTS + importance only, text-only
   writes that the consolidation worker backfills later). A broken child
   degrades recall quality; it never breaks prompts.

Do not run this path with `AI_MEMORY_EMBED=hash`: hash vectors are not
comparable with the `st` vectors in the index (cosine ~0) and silently poison
recall.

## Install

```bash
./install.sh          # copies index.ts into ~/.config/opencode/plugins/ai-memory
```

`install.sh` installs `@opencode/plugin` into the plugin directory if needed.
OpenCode reloads plugins on file change; no restart required.

## Configuration (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `AI_MEMORY_PROJECT` | `~/ai-memory` | checkout used for `uv run ai-memory-mcp` |
| `AI_MEMORY_DB` | `$AI_MEMORY_PROJECT/memory.db` | database file |
| `AI_MEMORY_UV` | first `uv` found on disk | uv binary |
| `AI_MEMORY_EMBED` | `st` (hot path) / `off` (fallback) | embedder, forced per path |
| `AI_MEMORY_PLUGIN_MCP` | `on` | `off` = never spawn the child, CLI fallback only |
| `AI_MEMORY_HOOK_TRACE` | `/tmp/ai-memory-hook.log` | JSONL hook trace (latency, source, errors) |

Set these in the environment OpenCode inherits (shell profile or the `environment`
block of your `opencode.json`).

## Namespaces

The namespace is derived from the working directory (`/home/llm` → `default`,
`/home/llm/work/foo` → `foo`), overridable with the `namespace` plugin option.
Briefing, recall, writes, and auto-capture all use it.

## Trace

Every hook appends one JSON line to the trace file:

```json
{"t":1792949070314,"hook":"prompt","outcome":"injected","chars":3639,"ms":1792,"src":"cli"}
{"t":1792948838142,"hook":"mcp","outcome":"ready","tools":15}
```

`src` tells you which path served the hook. `mcp ready` after a reload means the
warm child handshook successfully.

## Caveats

- The plugin targets the OpenCode plugin API v2 (`@opencode/plugin` ^2.0.22):
  `ctx.session.hook`, `ctx.command.transform`, `ctx.storage`, `ctx.event`.
  Hook names are not part of ai-memory, so a major OpenCode plugin API change
  breaks this file, not the server.
- One child per OpenCode server process (~850MB RSS with the ST model). Running
  several OpenCode servers means several children; that is expected.
- The child is restarted at most 3 times, then the plugin waits 5 minutes before
  trying again (a crash loop must not respawn a model loader forever).
- Writes from the hot path and the MCP server configured in `opencode.json` both
  go to the same SQLite file (WAL); keep both on the same `AI_MEMORY_EMBED`.
