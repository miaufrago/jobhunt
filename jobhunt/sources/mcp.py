"""Minimal streamable-HTTP MCP client.

Enough JSON-RPC to talk to an MCP server over HTTP: initialize, the initialized
notification, then tools/list and tools/call. Responses arrive as either JSON or
a single SSE frame, so both are handled.

Shared by the Dice source and the self-hosted LinkedIn server.
"""
import json

from .http import TIMEOUT, session, wait_turn

PROTOCOL = "2025-06-18"


class MCPError(RuntimeError):
    pass


def parse_response(resp) -> dict:
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return json.loads(payload)
        raise MCPError(f"no data frame in SSE response: {resp.text[:200]}")
    return json.loads(resp.text)


class MCPClient:
    def __init__(self, url: str, client_name: str = "jobhunt"):
        self.url = url
        self.client_name = client_name
        self.s = session()
        self.s.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        self._id = 0

    def _rpc(self, method: str, params: dict | None = None, notify: bool = False,
             timeout=None):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        # MCP sources called requests directly, skipping both the politeness
        # pacing and the STOP check that lives in wait_turn. http.py's own
        # comment claims "every request checks this before waiting its turn" --
        # which was untrue here, so a phase that had already timed out could not
        # stop a LinkedIn detail loop. A stalled fetch then held the run lock
        # for hours past its budget (twice in two days). Going through wait_turn
        # bounds that to the one request already in flight.
        wait_turn(self.url)
        r = self.s.post(self.url, json=body, timeout=timeout or TIMEOUT)
        if r.status_code >= 400:
            raise MCPError(f"{method} -> HTTP {r.status_code}: {r.text[:200]}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.s.headers["Mcp-Session-Id"] = sid
        if notify or not r.text.strip():
            return None
        msg = parse_response(r)
        if "error" in msg:
            raise MCPError(f"{method} -> {msg['error']}")
        return msg.get("result")

    def connect(self):
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": "0.1.0"},
        })
        self._rpc("notifications/initialized", {}, notify=True)
        return self

    def list_tools(self) -> list[str]:
        return [t.get("name") for t in (self._rpc("tools/list", {}) or {}).get("tools", [])]

    def call(self, name: str, arguments: dict, timeout=None) -> dict:
        # A browser-backed tool can take a while; allow a longer read.
        return self._rpc("tools/call", {"name": name, "arguments": arguments},
                         timeout=timeout or (10, 180)) or {}
