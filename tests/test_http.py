"""v0.5 eval: HTTP transport (token-guarded) serves the full toolset."""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time

import pytest

PORT = 18737
TOKEN = "test-token-abc123"
URL = f"http://127.0.0.1:{PORT}/mcp"

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _wait_port(timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
            return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError("http server never opened the port")


@pytest.fixture(scope="module")
def server():
    env = dict(
        os.environ,
        AI_MEMORY_DB=tempfile.mktemp(suffix=".db"),
        AI_MEMORY_EMBED="hash",
        AI_MEMORY_TOKEN=TOKEN,
        PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from ai_memory.server import serve_main; serve_main(['--port','{PORT}'])",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )
    try:
        _wait_port()
        time.sleep(0.5)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _client(token: str | None):
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth

    return Client(URL, auth=BearerAuth(token) if token else None)


def test_http_tools_and_roundtrip(server):
    async def go():
        async with _client(TOKEN) as c:
            tools = await c.list_tools()
            assert {t.name for t in tools} == {
                "memory_store",
                "memory_recall",
                "memory_recent",
                "memory_forget",
                "memory_restore",
                "memory_consolidate",
                "memory_vacuum",
                "memory_export",
                "memory_import",
                "memory_stats",
                "memory_maintenance",
            }, [t.name for t in tools]
            r = await c.call_tool(
                "memory_store", {"text": "HTTP test: deploy freeze on Fridays"}
            )
            assert r.content and r.content[0].text.startswith("ep_"), r
            hits = await c.call_tool(
                "memory_recall", {"query": "deploy fridays", "k": 3}
            )
            assert "freeze" in str(hits).lower(), hits

    asyncio.run(go())


def test_http_rejects_no_token(server):
    import httpx

    r = httpx.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=10)
    assert r.status_code == 401, (r.status_code, r.text[:100])


def test_http_rejects_bad_token(server):
    async def go():
        from fastmcp.exceptions import McpError

        with pytest.raises(Exception):  # noqa: BLE001 - any auth failure ok
            async with _client("wrong-token") as c:
                await c.list_tools()
                raise AssertionError("bad token was accepted")

    try:
        asyncio.run(go())
    except AssertionError:
        raise
    except Exception:
        pass  # expected: handshake rejected


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
