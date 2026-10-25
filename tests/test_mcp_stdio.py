"""MCP stdio wire test (Track 3, T3.1).

The OpenCode plugin keeps one long-lived `ai-memory-mcp` child and speaks
newline-delimited JSON-RPC to it. That contract is easier to break than it looks
(handshake, tool result shape, embedder env), so it gets a real subprocess test
instead of a mock.
"""

import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class McpStdio:
    """Minimal JSON-RPC client for a stdio MCP server."""

    def __init__(self, env: dict):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ai_memory.server"],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src"), **env},
        )
        self.next_id = 0

    def _send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _read(self, want_id: int, timeout: float = 60.0) -> dict:
        assert self.proc.stdout is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue  # banners / logs must never break the client
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                return msg
        raise AssertionError(f"no response for id={want_id}")

    def call(
        self, method: str, params: dict | None = None, timeout: float = 60.0
    ) -> dict:
        self.next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self.next_id,
                "method": method,
                "params": params or {},
            }
        )
        return self._read(self.next_id, timeout=timeout)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def handshake(self) -> dict:
        res = self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )
        self.notify("notifications/initialized")
        return res

    def tool(self, name: str, args: dict, timeout: float = 60.0):
        """Return the tool's value (FastMCP wraps it in structuredContent)."""
        res = self.call(
            "tools/call", {"name": name, "arguments": args}, timeout=timeout
        )
        assert "error" not in res, res
        result = res["result"]
        assert result.get("isError") is False, result
        sc = result.get("structuredContent") or {}
        # FastMCP wraps scalars/lists as {"result": X} but spreads dict returns
        return sc["result"] if set(sc) == {"result"} else sc

    def close(self) -> None:
        try:
            self.proc.kill()
        finally:
            self.proc.wait(timeout=10)


@pytest.fixture
def client(tmp_path):
    c = McpStdio(
        {
            "AI_MEMORY_DB": str(tmp_path / "mcp.db"),
            "AI_MEMORY_EMBED": "hash",
        }
    )
    try:
        c.handshake()
        yield c
    finally:
        c.close()


def test_initialize_and_tool_list(client):
    res = client.call("tools/list")
    names = {t["name"] for t in res["result"]["tools"]}
    assert {"memory_store", "memory_recall", "memory_stats"} <= names


def test_store_then_recall_roundtrip(client):
    stored = client.tool(
        "memory_store",
        {
            "text": "plugin child writes through the MCP path",
            "kind": "episode",
            "namespace": "t3",
        },
    )
    ref_id = stored
    assert isinstance(ref_id, str) and ref_id.startswith("ep_")

    hits = client.tool(
        "memory_recall",
        {"query": "plugin child writes", "k": 3, "namespace": "t3"},
    )
    assert [h["ref_id"] for h in hits] == [ref_id]

    stats = client.tool("memory_stats", {})
    assert stats["vec_episodes"] >= 1, stats


def test_namespace_isolation(client):
    client.tool("memory_store", {"text": "alpha note", "namespace": "one"})
    hits = client.tool("memory_recall", {"query": "alpha note", "namespace": "two"})
    assert hits == []
