"""Bounded async HTTP adapter for the public Fly Where To? MCP endpoint."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from ubas_backend.topology.models import Destination, Observation

ENDPOINT = "https://flywhereto.com/api/mcp"
PROTOCOL = "2025-03-26"


class SourceError(RuntimeError):
    pass


async def read_body(response: httpx.Response, max_bytes: int) -> bytes:
    response.raise_for_status()
    parts = bytearray()
    async for part in response.aiter_bytes():
        parts.extend(part)
        if len(parts) > max_bytes:
            raise SourceError("Source response exceeds size budget")
    return bytes(parts)


def rpc_result(body: bytes, content_type: str, identity: int) -> dict[str, Any]:
    messages: list[Any] = []
    if "text/event-stream" in content_type:
        # SSE data can span several lines; comments and event/id fields are ignored.
        for event in body.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(
                line[5:].removeprefix(" ")
                for line in event.splitlines()
                if line.startswith("data:")
            )
            if data:
                messages.append(json.loads(data))
    elif "application/json" in content_type:
        messages.append(json.loads(body))
    else:
        raise SourceError("Unexpected MCP content type")
    matches = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("id") == identity
    ]
    if len(matches) != 1 or "error" in matches[0]:
        raise SourceError("Missing, repeated or failed MCP response")
    result = matches[0].get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise SourceError("MCP source reported a failure")
    return result


def parse_destinations(data: Any, origin: str, fetched_at: datetime) -> Observation:
    if not isinstance(data, dict):
        raise SourceError("Expected a destination object")
    source_origin = data.get("origin")
    if not isinstance(source_origin, dict) or source_origin.get("iata") != origin:
        raise SourceError("Origin mismatch")
    items = data.get("destinations")
    if (
        not isinstance(items, list)
        or data.get("destinationCount") != len(items)
        or "kindFilter" not in data
        or data.get("kindFilter") is not None
        or any(not isinstance(item, dict) for item in items)
    ):
        raise SourceError("Truncated or filtered destinations response")
    return Observation(
        origin=origin,
        fetched_at=fetched_at,
        destinations=tuple(
            Destination.model_validate(
                {"airport": item.get("iata"), "kinds": item.get("kinds")}
            )
            for item in items
        ),
    )


class FlyWhereTo:
    """Small fixed-source client; no generic plugin or tool execution framework."""

    def __init__(
        self, client: httpx.AsyncClient, *, interval_seconds: float = 1
    ) -> None:
        self.client = client
        self.interval = interval_seconds
        self._identity = 0
        self._session_headers: dict[str, str] = {}
        self._last_call = 0.0

    async def initialize(self) -> None:
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "ubas-topology", "version": "0.1"},
            },
        )
        if result.get("protocolVersion") != PROTOCOL:
            raise SourceError("Unsupported MCP protocol")
        self._session_headers["MCP-Protocol-Version"] = PROTOCOL
        async with self.client.stream(
            "POST",
            ENDPOINT,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        ) as response:
            response.raise_for_status()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/event-stream",
            **self._session_headers,
        }

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0, self.interval - (loop.time() - self._last_call)))
        self._last_call = loop.time()
        self._identity += 1
        async with asyncio.timeout(30):
            async with self.client.stream(
                "POST",
                ENDPOINT,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._identity,
                    "method": method,
                    "params": params,
                },
            ) as response:
                body = await read_body(response, 4_000_000)
                session = response.headers.get("Mcp-Session-Id")
                if session is not None:
                    self._session_headers["Mcp-Session-Id"] = session
                return rpc_result(
                    body, response.headers.get("Content-Type", ""), self._identity
                )

    async def destinations(self, origin: str) -> Observation:
        result = await self._rpc(
            "tools/call",
            {
                "name": "destinations_from",
                "arguments": {"origin": origin, "include_airlines": False},
            },
        )
        content = result.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise SourceError("Unexpected tool result")
        block = content[0]
        if not isinstance(block, dict) or block.get("type") != "text":
            raise SourceError("Expected structured text result")
        return parse_destinations(json.loads(block["text"]), origin, datetime.now(UTC))

    async def aclose(self) -> None:
        if "Mcp-Session-Id" in self._session_headers:
            async with self.client.stream(
                "DELETE", ENDPOINT, headers=self._headers()
            ) as response:
                if response.status_code != 405:
                    response.raise_for_status()
