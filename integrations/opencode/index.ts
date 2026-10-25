import { Plugin } from "@opencode/plugin"
import { execFile, spawn } from "node:child_process"
import { appendFileSync, existsSync } from "node:fs"
import { promisify } from "node:util"

const TRACE = process.env.AI_MEMORY_HOOK_TRACE || "/tmp/ai-memory-hook.log"
function trace(obj: Record<string, unknown>) {
  try {
    appendFileSync(TRACE, JSON.stringify({ t: Date.now(), ...obj }) + "\n")
  } catch {
    // tracing must never break the loop
  }
}

const runFile = promisify(execFile)

// ai-memory lives here. Two paths to it:
//
// 1. HOT PATH (preferred): one long-lived `ai-memory-mcp` child over stdio,
//    spawned by this plugin with AI_MEMORY_EMBED=st. The ST model stays warm
//    (~10s cold load, ~50ms warm), so recall is semantic AND fast, and writes
//    land with real vectors immediately. It is a child of the OpenCode plugin,
//    not a service: it dies with the editor and needs no supervision.
//
// 2. FALLBACK: shell out to the CLI with AI_MEMORY_EMBED=off (~0.22s, FTS +
//    importance only, text-only writes that the worker backfills). Used when the
//    child is dead or still warming, so a broken child can never break prompts.
//
// Never run the CLI with AI_MEMORY_EMBED=hash: hash vectors are not comparable
// with the ST vectors in the shared index (cosine ~0), which silently poisons
// recall (fixed in v0.9.2).
const HOME = process.env.HOME || ""
const UV =
  process.env.AI_MEMORY_UV ||
  ["/usr/local/bin/uv", `${HOME}/.local/bin/uv`, "/usr/bin/uv"].find((p) => existsSync(p)) ||
  "uv"
const PROJECT = process.env.AI_MEMORY_PROJECT || `${HOME}/ai-memory`
const DB = process.env.AI_MEMORY_DB || `${PROJECT}/memory.db`
// Escape hatch: AI_MEMORY_PLUGIN_MCP=off keeps the CLI fallback path only.
const USE_MCP = (process.env.AI_MEMORY_PLUGIN_MCP || "on").toLowerCase() !== "off"

function slugify(s: string): string {
  const slug = s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32)
  return slug || "default"
}

function nsFromDir(dir?: string): string {
  if (!dir) return "default"
  const clean = dir.replace(/\/+$/, "")
  if (clean === "/home/llm" || clean === "/home" || clean === "/") return "default"
  const parts = clean.split("/").filter(Boolean)
  const base = parts[parts.length - 1] || "default"
  if (["home", "llm", "workspace", "projects", "tmp"].includes(base.toLowerCase())) return "default"
  return slugify(base)
}

async function cli(args: string[], timeoutMs: number, namespace: string): Promise<string> {
  const { stdout } = await runFile(UV, ["--directory", PROJECT, "run", "ai-memory", ...args], {
    timeout: timeoutMs,
    env: {
      ...process.env,
      AI_MEMORY_DB: DB,
      AI_MEMORY_EMBED: "off",
      AI_MEMORY_NS: namespace,
    },
    maxBuffer: 256 * 1024,
  })
  return stdout.trim()
}

function clip(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + "…" : s
}

type Hit = { ref_id?: string; store?: string; text?: string; score?: number }

/** One long-lived MCP stdio child, shared by every hook in this plugin. */
class MemoryChild {
  private child: any = null
  private buf = ""
  private nextId = 0
  private pending = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void; timer: any }>()
  private chain: Promise<any> = Promise.resolve()
  private readyP: Promise<boolean> | null = null
  private restarts = 0
  private deadUntil = 0
  private toolCount = 0

  get stats() {
    return { alive: !!this.child, restarts: this.restarts, tools: this.toolCount }
  }

  private spawnChild() {
    const c = spawn(UV, ["--directory", PROJECT, "run", "ai-memory-mcp"], {
      env: { ...process.env, AI_MEMORY_DB: DB, AI_MEMORY_EMBED: "st" },
      stdio: ["pipe", "pipe", "ignore"],
    })
    c.stdout?.setEncoding("utf8")
    c.stdout?.on("data", (d: string) => this.onData(d))
    c.on("exit", (code: number) => this.onExit(code))
    c.on("error", () => this.onExit(-1))
    this.child = c
    this.buf = ""
  }

  private onExit(code: number) {
    this.child = null
    this.readyP = null
    for (const [, p] of this.pending) {
      clearTimeout(p.timer)
      p.reject(new Error(`memory child exited (${code})`))
    }
    this.pending.clear()
    this.restarts += 1
    if (this.restarts > 3) this.deadUntil = Date.now() + 5 * 60_000
    trace({ hook: "mcp", outcome: "exit", code, restarts: this.restarts })
  }

  private onData(chunk: string) {
    this.buf += chunk
    let idx: number
    while ((idx = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, idx).trim()
      this.buf = this.buf.slice(idx + 1)
      if (!line.startsWith("{")) continue // banners/logs must not break the client
      let msg: any
      try {
        msg = JSON.parse(line)
      } catch {
        continue
      }
      if (typeof msg.id !== "number") continue
      const p = this.pending.get(msg.id)
      if (!p) continue
      this.pending.delete(msg.id)
      clearTimeout(p.timer)
      if (msg.error) p.reject(new Error(String(msg.error.message ?? msg.error)))
      else p.resolve(msg.result)
    }
  }

  private send(payload: any) {
    if (!this.child?.stdin) throw new Error("memory child not running")
    this.child.stdin.write(JSON.stringify(payload) + "\n")
  }

  private request(method: string, params: any, timeoutMs: number): Promise<any> {
    const id = ++this.nextId
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`${method} timeout after ${timeoutMs}ms`))
      }, timeoutMs)
      this.pending.set(id, { resolve, reject, timer })
      try {
        this.send({ jsonrpc: "2.0", id, method, params })
      } catch (e) {
        clearTimeout(timer)
        this.pending.delete(id)
        reject(e)
      }
    })
  }

  /** Spawn + handshake. Resolves true when tools are callable. */
  start(): Promise<boolean> {
    if (!USE_MCP) return Promise.resolve(false)
    if (this.readyP) return this.readyP
    if (Date.now() < this.deadUntil) return Promise.resolve(false)
    this.spawnChild()
    this.readyP = (async () => {
      try {
        await this.request(
          "initialize",
          {
            protocolVersion: "2024-11-05",
            capabilities: {},
            clientInfo: { name: "opencode-ai-memory-plugin", version: "1" },
          },
          90_000,
        )
        this.send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} })
        const list = await this.request("tools/list", {}, 30_000)
        this.toolCount = list?.tools?.length ?? 0
        // A handshake that worked means the child is healthy: reset the crash
        // counter here, NOT on spawn, otherwise a crash loop never trips the
        // dead window and we respawn uv+torch forever.
        this.restarts = 0
        trace({ hook: "mcp", outcome: "ready", tools: this.toolCount })
        return this.toolCount > 0
      } catch (err) {
        trace({ hook: "mcp", outcome: "handshake-failed", err: String(err).slice(0, 200) })
        try {
          this.child?.kill()
        } catch {
          /* ignore */
        }
        this.child = null
        // Clear the cached promise so a later hook can retry (the crash
        // counter above still throttles a broken child).
        this.readyP = null
        return false
      }
    })()
    return this.readyP
  }

  private enqueue<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.chain.then(fn, fn)
    this.chain = next.catch(() => {})
    return next
  }

  async call(name: string, args: any, timeoutMs = 3000): Promise<any> {
    const ready = await Promise.race([
      this.start(),
      new Promise<boolean>((r) => setTimeout(() => r(false), Math.min(timeoutMs, 1500))),
    ])
    if (!ready) throw new Error("memory child not ready")
    return this.enqueue(() => this.request("tools/call", { name, arguments: args }, timeoutMs)).then(
      (res: any) => {
        if (res?.isError) throw new Error(`tool ${name} error`)
        const sc = res?.structuredContent ?? {}
        const keys = Object.keys(sc)
        return keys.length === 1 && keys[0] === "result" ? sc.result : sc
      },
    )
  }

  kill() {
    try {
      this.child?.kill()
    } catch {
      /* ignore */
    }
    this.child = null
  }
}

function fmtHits(hits: Hit[]): string {
  return hits
    .filter((h) => h?.text)
    .map((h) => `${Number(h.score ?? 0).toFixed(3)} [${h.store ?? "?"}] ${clip(String(h.text).replace(/\s+/g, " "), 240)}`)
    .join("\n")
}

export default Plugin.define({
  id: "ai-memory",
  async setup(ctx) {
    // Namespace for this plugin instance (per project/location).
    const optNs = (ctx.options as any)?.namespace as string | undefined
    const defaultNs = optNs || nsFromDir((ctx.location as any)?.directory || (ctx.location as any)?.project?.canonical)
    trace({ hook: "setup", defaultNs, dir: (ctx.location as any)?.directory })

    const mem = new MemoryChild()
    // Warm the child now: plugin setup happens at editor start, so the ~10s ST
    // load is paid long before the first prompt needs it.
    if (USE_MCP) void mem.start()

    /** Recall via the warm child, CLI fallback. Returns formatted lines. */
    async function recallText(query: string, k: number, ns: string, budgetMs: number): Promise<{ text: string; src: string }> {
      try {
        const hits = (await mem.call("memory_recall", { query: clip(query, 300), k, namespace: ns }, budgetMs)) as Hit[]
        return { text: fmtHits(hits ?? []), src: "mcp" }
      } catch (err) {
        trace({ hook: "recall", outcome: "fallback", err: String(err).slice(0, 120) })
        const text = await cli(["recall", clip(query, 300), "-k", String(k), "--namespace", ns], budgetMs, ns).catch(() => "")
        return { text, src: "cli" }
      }
    }

    /** Store via the warm child (real vectors now), CLI off fallback. */
    async function storeText(text: string, ns: string, budgetMs = 5000): Promise<string> {
      try {
        const id = await mem.call("memory_store", { text, kind: "episode", namespace: ns }, budgetMs)
        return String(id ?? "")
      } catch (err) {
        trace({ hook: "store", outcome: "fallback", err: String(err).slice(0, 120) })
        return await cli(["store", text, "--kind", "episode", "--namespace", ns], budgetMs, ns).catch(() => "")
      }
    }

    // ---- 1+6. Project-scoped briefing + usage instruction (first model call) ----
    await ctx.session.hook("context", async (event: any) => {
      try {
        const sid: string | undefined = event.sessionID
        if (!sid) return
        if (await ctx.storage.get(`briefed/${sid}`)) return
        const t0 = Date.now()
        // 3 parallel recalls: profile, decisions, procedures — all namespaced.
        const [profile, decisions, procs] = await Promise.all([
          recallText("user profile and preferences", 3, defaultNs, 6000),
          recallText("decisions made in this project", 3, defaultNs, 6000),
          recallText("project procedures runbooks how to", 3, defaultNs, 6000),
        ])
        const briefing = [profile.text, decisions.text, procs.text].map((s) => s.trim()).filter(Boolean).join("\n").slice(0, 4000)
        if (!briefing) {
          trace({ hook: "context", outcome: "empty-recall", sid })
          return
        }
        event.system.push({
          type: "text",
          text:
            `Long-term memory briefing (ai-memory, ns=${defaultNs}, session ${sid}):\n${briefing}\n` +
            `Rules: call memory_recall before coding (project facts first, ns=${defaultNs}); ` +
            `call memory_store after decisions, prefs, or error→fix (kind=episode, never secrets); ` +
            `run memory_consolidate at session end. Manual: /remember, /recall, /forget, /stats.`,
        })
        await ctx.storage.set(`briefed/${sid}`, true)
        trace({
          hook: "context",
          outcome: "briefed",
          sid,
          ns: defaultNs,
          chars: briefing.length,
          ms: Date.now() - t0,
          src: `${profile.src}/${decisions.src}/${procs.src}`,
        })
      } catch (err) {
        trace({ hook: "context", outcome: "error", err: String(err).slice(0, 200) })
      }
    })

    // ---- 2. Task-aware recall on every prompt (best-effort, fast) ----
    await ctx.session.hook("prompt", async (event: any) => {
      try {
        const text: string = event.prompt?.text ?? ""
        const sid: string | undefined = event.sessionID
        if (!text || text.length < 12 || text.startsWith("/")) return
        // log prompt for later auto-capture
        if (sid) {
          try {
            const key = `prompts/${sid}`
            const prev = ((await ctx.storage.get(key)) as string[] | undefined) ?? []
            prev.push(text.slice(0, 500))
            await ctx.storage.set(key, prev.slice(-10))
          } catch {
            /* ignore */
          }
        }
        const t0 = Date.now()
        const { text: hits, src } = await recallText(text, 3, defaultNs, 4000)
        if (!hits) return
        // Prepend compact memory context; keep retry-safe (pure text transform).
        event.prompt.text = `[Relevant memory ns=${defaultNs}]\n${clip(hits, 1500)}\n\n${text}`
        trace({ hook: "prompt", outcome: "injected", sid, chars: hits.length, ms: Date.now() - t0, src })
      } catch (err) {
        trace({ hook: "prompt", outcome: "error", err: String(err).slice(0, 200) })
      }
    })

    // ---- 3+4. Auto-capture on idle + throttled consolidate ----
    const controller = new AbortController()
    void (async () => {
      try {
        for await (const ev of ctx.event.subscribe({ signal: controller.signal }) as AsyncIterable<any>) {
          try {
            const type: string = ev?.type ?? ""
            if (type !== "session.idle" && type !== "session.compacted") continue
            const data = ev?.data ?? {}
            const sid: string | undefined = data.sessionID || data.sessionId
            if (!sid) continue
            // namespace: prefer event location dir, else instance default
            const evDir: string | undefined = ev?.location?.directory || data?.location?.directory
            const ns = evDir ? nsFromDir(evDir) : defaultNs
            // debounce: min 90s between auto-stores per session
            const lastKey = `autostore/${sid}`
            const last = ((await ctx.storage.get(lastKey)) as number | undefined) ?? 0
            if (Date.now() - last < 90_000) continue
            // build episode from logged prompts (robust, no fragile message parsing)
            const prompts = ((await ctx.storage.get(`prompts/${sid}`)) as string[] | undefined) ?? []
            let stored = ""
            if (type === "session.compacted") {
              const summary = clip(String(data.summary ?? prompts.slice(-3).join(" | ") ?? ""), 1000)
              if (summary.length < 20) continue
              stored = await storeText(`Session ${sid} compaction summary: ${summary}`, ns, 8000)
            } else {
              // session.idle: store last user intent (skip trivial)
              const lastPrompt = prompts[prompts.length - 1] ?? ""
              if (lastPrompt.length < 20) continue
              if (/^(hi|hello|hey|thanks|thank you|ok|okay)\.?$/i.test(lastPrompt.trim())) continue
              stored = await storeText(`Session ${sid} in ${ns}: user asked: ${clip(lastPrompt, 800)}`, ns, 8000)
            }
            await ctx.storage.set(lastKey, Date.now())
            trace({ hook: "autostore", outcome: "stored", sid, ns, type, id: stored.slice(0, 32) })
            // throttled consolidate: max once per 10 min per namespace
            const consKey = `consolidated/${ns}`
            const consLast = ((await ctx.storage.get(consKey)) as number | undefined) ?? 0
            if (Date.now() - consLast > 10 * 60_000) {
              await mem
                .call("memory_consolidate", { limit: 20 }, 30_000)
                .catch(() => cli(["consolidate", "--limit", "20"], 30000, ns).catch(() => ""))
              await ctx.storage.set(consKey, Date.now())
              trace({ hook: "autostore", outcome: "consolidated", ns })
            }
          } catch (inner) {
            trace({ hook: "autostore", outcome: "error", err: String(inner).slice(0, 200) })
          }
        }
      } catch (err) {
        trace({ hook: "autostore", outcome: "loop-ended", err: String(err).slice(0, 200) })
      }
    })()

    // ---- 5. Commands: /remember, /recall, /forget, /stats, /consolidate ----
    await ctx.command.transform((editor) => {
      editor.add({
        name: "remember",
        description: "Save text to long-term ai-memory and consolidate (prefix 'proc: trigger -> steps' to save a procedure)",
        execute: async ({ sessionID, prompt }: any) => {
          const text = String(prompt.text ?? "").replace(/^\/?remember\s+/, "").trim()
          if (!text || text === String(prompt.text ?? "").trim()) {
            await ctx.session.synthetic({ sessionID, text: "Usage: /remember <something worth keeping across sessions> | /remember proc: <trigger> -> <steps>" })
            return
          }
          await storeText(text, defaultNs, 8000)
          await mem
            .call("memory_consolidate", { limit: 20 }, 30_000)
            .catch(() => cli(["consolidate", "--limit", "20"], 30000, defaultNs).catch(() => ""))
          await ctx.session.synthetic({ sessionID, text: `Saved to ai-memory (ns=${defaultNs}) and consolidated.` })
        },
      })
      editor.add({
        name: "recall",
        description: "Recall long-term ai-memory for a query",
        execute: async ({ sessionID, prompt }: any) => {
          const q = String(prompt.text ?? "").replace(/^\/?recall\s+/, "").trim()
          if (!q || q === String(prompt.text ?? "").trim()) {
            await ctx.session.synthetic({ sessionID, text: "Usage: /recall <query>" })
            return
          }
          const out = await recallText(q, 5, defaultNs, 6000).catch((e) => ({
            text: `recall failed: ${String(e).slice(0, 200)}`,
            src: "error",
          }))
          await ctx.session.synthetic({ sessionID, text: out.text || "(no memories)" })
        },
      })
      editor.add({
        name: "forget",
        description: "Forget (soft-delete) memories matching text",
        execute: async ({ sessionID, prompt }: any) => {
          const q = String(prompt.text ?? "").replace(/^\/?forget\s+/, "").trim()
          if (!q || q === String(prompt.text ?? "").trim()) {
            await ctx.session.synthetic({ sessionID, text: "Usage: /forget <text to forget>" })
            return
          }
          const out = await cli(["forget", "--query", q], 15000, defaultNs).catch((e) => `forget failed: ${String(e).slice(0, 200)}`)
          await ctx.session.synthetic({ sessionID, text: `Forgot ${out} row(s) in ns=${defaultNs} (restorable 30d).` })
        },
      })
      editor.add({
        name: "stats",
        description: "Show ai-memory stats",
        execute: async ({ sessionID }: any) => {
          const out = await cli(["stats"], 15000, defaultNs).catch((e) => `stats failed: ${String(e).slice(0, 200)}`)
          await ctx.session.synthetic({ sessionID, text: out || "(no stats)" })
        },
      })
      editor.add({
        name: "consolidate",
        description: "Distill unprocessed episodes into facts",
        execute: async ({ sessionID }: any) => {
          const out = await mem
            .call("memory_consolidate", { limit: 50 }, 60_000)
            .then((r: any) => JSON.stringify(r))
            .catch(() => cli(["consolidate", "--limit", "50"], 30000, defaultNs).catch((e) => `consolidate failed: ${String(e).slice(0, 200)}`))
          await ctx.session.synthetic({ sessionID, text: out || "consolidated" })
        },
      })
    })

    return () => {
      controller.abort()
      mem.kill()
    }
  },
})
