"""Dice via its official MCP server (https://mcp.dice.com/mcp).

Minimal streamable-HTTP JSON-RPC client: initialize -> notifications/initialized ->
tools/call. Responses arrive as either JSON or an SSE frame, so both are parsed.
"""
import json
import time

from ..normalize import strip_html
from .http import TIMEOUT, session

PROTOCOL = "2025-06-18"
MAX_DETAIL = 40  # cap detail lookups per run


class MCPError(RuntimeError):
    pass


def _parse(resp) -> dict:
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return json.loads(payload)
        raise MCPError(f"no data frame in SSE response: {text[:200]}")
    return json.loads(text)


class DiceMCP:
    def __init__(self, url: str):
        self.url = url
        self.s = session()
        self.s.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        self._id = 0

    def _call(self, method: str, params: dict | None = None, notify: bool = False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        r = self.s.post(self.url, json=body, timeout=TIMEOUT)
        if r.status_code >= 400:
            raise MCPError(f"{method} -> HTTP {r.status_code}: {r.text[:200]}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.s.headers["Mcp-Session-Id"] = sid
        if notify or not r.text.strip():
            return None
        msg = _parse(r)
        if "error" in msg:
            raise MCPError(f"{method} -> {msg['error']}")
        return msg.get("result")

    def connect(self):
        self._call("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "jobhunt", "version": "0.1.0"},
        })
        self._call("notifications/initialized", {}, notify=True)
        return self

    def job_details(self, job_id: str) -> str:
        res = self._call("tools/call", {
            "name": "get_job_details", "arguments": {"job_id": job_id},
        }) or {}
        for block in res.get("content", []) or []:
            if block.get("type") != "text":
                continue
            text = block.get("text", "")
            try:
                parsed = json.loads(text)
            except Exception:
                return strip_html(text)
            if isinstance(parsed, dict):
                data = parsed.get("data") or parsed
                for key in ("description", "jobDescription", "detailedDescription", "summary"):
                    if data.get(key):
                        return strip_html(data[key])
        return ""

    def list_tools(self) -> list[str]:
        res = self._call("tools/list", {}) or {}
        return [t.get("name") for t in res.get("tools", [])]

    def search(self, query: str, location: str, page_size: int = 50,
               radius: int = 50, posted_date: str = "SEVEN") -> list[dict]:
        args = {
            "keyword": query,
            "location": location,
            "radius": radius,
            "radius_unit": "mi",
            "jobs_per_page": page_size,
            "page_number": 1,
            "sort": "datePosted",
            "posted_date": posted_date,
        }
        res = self._call("tools/call", {"name": "search_jobs", "arguments": args}) or {}
        return _extract_records(res)


def _extract_records(res: dict) -> list[dict]:
    """MCP tool results carry either structuredContent or JSON inside a text block."""
    if isinstance(res.get("structuredContent"), dict):
        sc = res["structuredContent"]
        for key in ("data", "jobs", "results", "items"):
            if isinstance(sc.get(key), list):
                return sc[key]
    for block in res.get("content", []) or []:
        if block.get("type") == "text":
            try:
                parsed = json.loads(block.get("text", ""))
            except Exception:
                continue
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for key in ("data", "jobs", "results", "items"):
                    if isinstance(parsed.get(key), list):
                        return parsed[key]
    return []


def _first(rec: dict, *keys, default=""):
    for k in keys:
        v = rec.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("displayName")
        if v:
            return v
    return default


def fetch_dice(terms: list[str], cfg: dict) -> list[dict]:
    d = cfg["sources"]["dice"]
    client = DiceMCP(d.get("mcp_url", "https://mcp.dice.com/mcp")).connect()
    location = cfg["sources"]["aggregators"].get("location", "Seattle, WA")
    out: list[dict] = []
    seen: set[str] = set()
    for term in terms:
        for rec in client.search(term, location):
            job_id = _first(rec, "guid", "jobId", "id")  # detail lookup keys off guid
            url = _first(rec, "url", "jobUrl", "detailsPageUrl", "applyUrl").split("?")[0]
            key = job_id or url
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({
                "source": "dice",
                "company": _first(rec, "companyName", "company", "hiringOrganization"),
                "title": _first(rec, "title", "jobTitle", "name"),
                "location": _first(rec, "jobLocation", "location", "city"),
                "url": url,
                "posted_at": _first(rec, "postedDate", "datePosted", "modifiedDate"),
                "description": strip_html(_first(rec, "description", "summary")),
                "_compensation": rec.get("salary"),
                "_id": job_id,
            })

    # The search payload truncates the JD; get_job_details returns the full text.
    from .ats import _worth_detail
    for j in _worth_detail(out, MAX_DETAIL):
        jid = j.pop("_id", "")
        if not jid:
            continue
        try:
            full = client.job_details(jid)
            if len(full) > len(j["description"]):
                j["description"] = full
        except Exception:
            pass
    for j in out:
        j.pop("_id", None)
    return out
