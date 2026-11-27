"""Seam 1: an MCP client that talks to the service the way Claude Code does.

The service runs under uvicorn on a local port, and each call opens an MCP session over
Streamable HTTP (initialize, then the request), exactly as `claude mcp add --transport
http` would against pi5.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import anyio
import httpx
import uvicorn
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Tool

T = TypeVar("T")


class McpCaller:
    def __init__(self, app: FastAPI, host_header: str | None = None) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        port = self._sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.host_header = host_header
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._sock]}, daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("MCP test server didn't start")
            time.sleep(0.01)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._sock.close()

    def tools(self) -> list[Tool]:
        async def go(session: ClientSession) -> list[Tool]:
            return (await session.list_tools()).tools

        return self._session(go)

    def call(self, tool: str, **arguments: Any) -> CallToolResult:
        async def go(session: ClientSession) -> CallToolResult:
            return await session.call_tool(tool, arguments)

        return self._session(go)

    def _session(self, work: Callable[[ClientSession], Awaitable[T]]) -> T:
        headers = {"Host": self.host_header} if self.host_header else None

        async def run() -> T:
            async with (
                httpx.AsyncClient(headers=headers, timeout=60) as http,
                streamable_http_client(self.url, http_client=http) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                return await work(session)

        return anyio.run(run)
