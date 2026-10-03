"""LLM distillation hook: optional second pass over episodes rules can't parse.

Default OFF (rules-only, offline). Enable with AI_MEMORY_LLM=ollama (local) or
AI_MEMORY_LLM=openai (any OpenAI-compatible endpoint). stdlib HTTP, no new deps.
Any failure degrades to rules-only — consolidation never crashes on LLM errors.
"""
from __future__ import annotations
import json
import os
import urllib.request

PROMPT = """Extract durable memory from this chat episode. Reply with JSON only.

Schema: {"facts": [{"entity": string, "key": string, "value": string, "confidence": 0.0-1.0}], "procedures": [{"trigger": string, "steps": string}]}
Rules:
- entity is "user" for personal prefs/identity, "project" for stack/commands, "decisions" for choices made, "learnings" for error->fix.
- key is snake_case, short. value max 280 chars. Skip greetings, chit-chat, and anything already obvious.
- Empty result is fine: {"facts": [], "procedures": []}.

Episode:
\"\"\"%s\"\"\""""


def complete(prompt: str, provider: str, model: str, host: str,
             api_key: str | None, timeout: float = 60.0) -> str:
    if provider == "ollama":
        data = json.dumps({"model": model, "prompt": prompt,
                           "stream": False, "format": "json",
                           "options": {"temperature": 0.2}}).encode()
        req = urllib.request.Request(f"{host}/api/generate", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()).get("response", "")
    # OpenAI-compatible (OpenAI, OpenRouter, vLLM, llama.cpp server, ...)
    body = {"model": model, "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"{host}/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
        return out["choices"][0]["message"]["content"]


def _clean(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


def extract_with_llm(text: str, provider: str = "ollama",
                     model: str | None = None, host: str | None = None,
                     api_key: str | None = None) -> dict:
    """Return {'facts': [(entity,key,value,conf)], 'procedures': [(trigger,steps)]}."""
    facts: list = []
    procs: list = []
    try:
        if provider == "ollama":
            model = model or os.environ.get("AI_MEMORY_LLM_MODEL", "llama3.1:8b")
            host = host or os.environ.get("AI_MEMORY_LLM_HOST", "http://localhost:11434")
        else:
            model = model or os.environ.get("AI_MEMORY_LLM_MODEL", "gpt-4o-mini")
            host = host or os.environ.get("AI_MEMORY_LLM_HOST", "https://api.openai.com/v1")
            api_key = api_key if api_key is not None else os.environ.get("AI_MEMORY_LLM_API_KEY")
        raw = complete(PROMPT % text[:2000], provider, model, host, api_key)
        out = json.loads(_clean(raw))
        for f in out.get("facts", [])[:10]:
            if all(k in f for k in ("entity", "key", "value")) and str(f["value"]).strip():
                try:
                    conf = min(1.0, max(0.0, float(f.get("confidence", 0.6))))
                except (TypeError, ValueError):
                    conf = 0.6
                facts.append((str(f["entity"])[:40], str(f["key"])[:60],
                              str(f["value"]).strip()[:280], conf))
        for p in out.get("procedures", [])[:5]:
            if p.get("trigger") and p.get("steps"):
                procs.append((str(p["trigger"]).strip()[:120],
                              str(p["steps"]).strip()[:400]))
    except Exception:
        pass  # rules-only fallback
    return {"facts": facts, "procedures": procs}
