#!/usr/bin/env python3
from __future__ import annotations
"""
Bee Wearable MCP Server (Hybrid)
=================================
A Model Context Protocol server for the Bee wearable AI device that combines
on-demand bee-cli queries with a persistent background SSE stream for instant
real-time context.

Architecture:
  - On startup, a background task connects to `bee stream --json` via SSE
  - Incoming events (utterances, conversation updates, todos, journals, etc.)
    are cached in a rolling in-memory buffer
  - Tools like `bee_get_live_stream` return cached events instantly (<1ms)
  - Tools like `bee_get_conversation` shell out to bee-cli for full data
  - If the stream disconnects, it auto-reconnects with exponential backoff

Usage:
    python bee_mcp_server.py

Requirements:
    - bee-cli installed and authenticated (`npm install -g @beeai/cli && bee login`)
    - Python 3.10+
    - mcp[cli] >= 1.4.0
"""

import asyncio
import glob as globmod
import json
import os
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field, ConfigDict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CACHED_EVENTS = 500
MAX_CACHED_UTTERANCES = 200
STREAM_RECONNECT_BASE_DELAY = 3  # seconds
STREAM_RECONNECT_MAX_DELAY = 60

# ---------------------------------------------------------------------------
# Bee CLI wrapper
# ---------------------------------------------------------------------------

def _find_bee_cli() -> str:
    """Locate the bee CLI binary on this system."""
    candidates = [
        "bee",
        os.path.expanduser("~/.bun/bin/bee"),
        "/usr/local/bin/bee",
        "/opt/homebrew/bin/bee",
    ]
    # Expand nvm glob pattern to actual paths
    nvm_matches = globmod.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/bee"))
    candidates.extend(nvm_matches)

    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return "bee"


BEE_CLI = _find_bee_cli()


async def _run_bee(args: list[str], timeout: int = 30) -> str:
    """Run a bee-cli command asynchronously and return stdout."""
    cmd = [BEE_CLI] + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode()
        if proc.returncode != 0:
            err = stderr.decode().strip() or output.strip()
            return f"Error: {err or f'bee-cli exited with code {proc.returncode}'}"
        return output
    except FileNotFoundError:
        return "Error: bee-cli not found. Install with: npm install -g @beeai/cli"
    except asyncio.TimeoutError:
        return "Error: bee-cli command timed out."
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


async def _run_bee_json(args: list[str], timeout: int = 30) -> dict | list | str:
    """Run a bee-cli command with --json and return parsed JSON."""
    output = await _run_bee(args + ["--json"], timeout=timeout)
    if output.startswith("Error:"):
        return output
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def _handle_error(e: Exception) -> str:
    """Format exceptions into actionable error messages."""
    if isinstance(e, FileNotFoundError):
        return "Error: bee-cli not found. Install with: npm install -g @beeai/cli"
    if isinstance(e, subprocess.TimeoutExpired):
        return "Error: Command timed out. The Bee API may be slow — try again."
    return f"Error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Live Stream Cache — the heart of the hybrid architecture
# ---------------------------------------------------------------------------

class LiveStreamCache:
    """
    Maintains a rolling buffer of recent SSE events from `bee stream --json`.
    Provides instant access to real-time context without shelling out to bee-cli.
    """

    def __init__(self, max_events: int = MAX_CACHED_EVENTS, max_utterances: int = MAX_CACHED_UTTERANCES):
        self.events: deque[dict] = deque(maxlen=max_events)
        self.utterances: deque[dict] = deque(maxlen=max_utterances)
        self.active_conversations: dict[str, dict] = {}
        self.recent_todos: dict[str, dict] = {}
        self.recent_journals: dict[str, dict] = {}
        self.last_location: Optional[dict] = None
        self.connected: bool = False
        self.last_event_time: Optional[str] = None
        self.stats = {
            "total_events": 0,
            "utterances": 0,
            "conversations_started": 0,
            "conversations_ended": 0,
            "todos": 0,
            "journals": 0,
            "errors": 0,
            "reconnects": 0,
        }

    def ingest(self, raw_json: dict):
        """Process a single SSE event and update all caches."""
        now = datetime.now(timezone.utc).isoformat()
        event_type = self._classify_event(raw_json)
        event = {"type": event_type, "data": raw_json, "time": now}

        self.events.append(event)
        self.last_event_time = now
        self.stats["total_events"] += 1

        # Route to specialized caches
        if event_type == "new-utterance":
            self._cache_utterance(raw_json, now)
        elif event_type in ("new-conversation", "update-conversation"):
            self._cache_conversation(raw_json, event_type)
        elif event_type == "update-conversation-summary":
            self._cache_summary(raw_json)
        elif event_type == "delete-conversation":
            self._remove_conversation(raw_json)
        elif event_type in ("todo-created", "todo-updated", "todo-deleted"):
            self._cache_todo(raw_json, event_type)
        elif event_type in ("journal-created", "journal-updated"):
            self._cache_journal(raw_json)
        elif event_type == "update-location":
            self._cache_location(raw_json)

    def _classify_event(self, data: dict) -> str:
        """Determine the event type — uses the 'type' field from the stream
        when present, falls back to payload inference for older formats."""
        # The real bee stream --json includes a "type" field directly
        if "type" in data:
            return data["type"]
        # Connection event is just {"timestamp": ...}
        if "timestamp" in data and len(data) == 1:
            return "connected"
        # Fallback: infer from payload structure
        if "utterance" in data:
            return "new-utterance"
        if "conversation" in data:
            conv = data["conversation"]
            state = conv.get("state", "")
            if state in ("completed", "ended", "finalized"):
                return "update-conversation"
            conv_id = str(conv.get("id", ""))
            if conv_id in self.active_conversations:
                return "update-conversation"
            return "new-conversation"
        if "short_summary" in data and "conversation_id" in data:
            return "update-conversation-summary"
        if "todo" in data:
            return "todo-updated" if data["todo"].get("completed") else "todo-created"
        if "journal" in data:
            return "journal-created"
        if "location" in data:
            return "update-location"
        if "journalId" in data:
            return "journal-text" if "text" in data else "journal-deleted"
        return "unknown"

    def _cache_utterance(self, data: dict, timestamp: str):
        utterance = data.get("utterance", {})
        self.utterances.append({
            "speaker": utterance.get("speaker", "me"),  # may be absent in solo convos
            "text": utterance.get("text", ""),
            "conversation_uuid": data.get("conversation_uuid", ""),
            "spoken_at": utterance.get("spoken_at", timestamp),
            "time": timestamp,
        })
        self.stats["utterances"] += 1

    def _cache_conversation(self, data: dict, event_type: str):
        conv = data.get("conversation", {})
        conv_id = str(conv.get("id", ""))
        state = conv.get("state", "unknown")
        conv_uuid = conv.get("conversation_uuid") or conv.get("uuid", "")
        self.active_conversations[conv_id] = {
            "id": conv.get("id"),
            "uuid": conv_uuid,
            "state": state,
            "title": conv.get("title", ""),
            "short_summary": conv.get("short_summary", ""),
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        if event_type == "new-conversation":
            self.stats["conversations_started"] += 1
        if state in ("completed", "ended", "finalized"):
            self.stats["conversations_ended"] += 1

    def _cache_summary(self, data: dict):
        conv_id = str(data.get("conversation_id", ""))
        if conv_id in self.active_conversations:
            self.active_conversations[conv_id]["short_summary"] = data.get("short_summary", "")

    def _remove_conversation(self, data: dict):
        conv = data.get("conversation", {})
        conv_id = str(conv.get("id", ""))
        self.active_conversations.pop(conv_id, None)

    def _cache_todo(self, data: dict, event_type: str):
        todo = data.get("todo", {})
        todo_id = str(todo.get("id", ""))
        if event_type == "todo-deleted":
            self.recent_todos.pop(todo_id, None)
        else:
            self.recent_todos[todo_id] = {
                "id": todo.get("id"),
                "text": todo.get("text", ""),
                "completed": todo.get("completed", False),
                "alarm_at": todo.get("alarmAt"),
                "updated": datetime.now(timezone.utc).isoformat(),
            }
        self.stats["todos"] += 1

    def _cache_journal(self, data: dict):
        journal = data.get("journal", {})
        journal_id = str(journal.get("id", ""))
        self.recent_journals[journal_id] = {
            "id": journal.get("id"),
            "state": journal.get("state", "unknown"),
            "text": journal.get("text", ""),
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        self.stats["journals"] += 1

    def _cache_location(self, data: dict):
        loc = data.get("location", {})
        self.last_location = {
            "latitude": loc.get("latitude"),
            "longitude": loc.get("longitude"),
            "name": loc.get("name", ""),
            "conversation_id": data.get("conversation_id"),
            "time": datetime.now(timezone.utc).isoformat(),
        }

    def get_snapshot(self) -> dict:
        """Return the complete current state as a JSON-serializable dict."""
        return {
            "stream_connected": self.connected,
            "last_event_time": self.last_event_time,
            "stats": self.stats,
            "active_conversations": list(self.active_conversations.values()),
            "recent_utterances": list(self.utterances)[-30:],  # last 30
            "recent_todos": list(self.recent_todos.values()),
            "recent_journals": list(self.recent_journals.values()),
            "last_location": self.last_location,
        }

    def get_recent_utterances(self, count: int = 50) -> list[dict]:
        """Return the N most recent utterances."""
        return list(self.utterances)[-count:]

    def get_recent_events(self, count: int = 50, event_type: str = "") -> list[dict]:
        """Return recent events, optionally filtered by type."""
        events = list(self.events)
        if event_type:
            events = [e for e in events if e["type"] == event_type]
        return events[-count:]


# ---------------------------------------------------------------------------
# Background SSE stream task
# ---------------------------------------------------------------------------

async def _run_stream(cache: LiveStreamCache):
    """
    Connect to `bee stream --json` and continuously ingest events into the cache.
    Auto-reconnects with exponential backoff on disconnection.
    """
    delay = STREAM_RECONNECT_BASE_DELAY
    proc = None

    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                BEE_CLI, "stream", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NO_COLOR": "1"},
            )
            cache.connected = True
            delay = STREAM_RECONNECT_BASE_DELAY  # reset on successful connect

            async for line in proc.stdout:
                text = line.decode().strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                    cache.ingest(data)
                except json.JSONDecodeError:
                    pass  # skip non-JSON lines (status messages, etc.)

            # Process exited normally
            cache.connected = False
            cache.stats["reconnects"] += 1

        except asyncio.CancelledError:
            cache.connected = False
            if proc is not None and proc.returncode is None:
                proc.terminate()
            return
        except Exception:
            cache.connected = False
            cache.stats["errors"] += 1

        # Exponential backoff
        await asyncio.sleep(delay)
        delay = min(delay * 2, STREAM_RECONNECT_MAX_DELAY)


# ---------------------------------------------------------------------------
# Server lifespan — start/stop the background stream
# ---------------------------------------------------------------------------

@asynccontextmanager
async def server_lifespan():
    """Start the background SSE stream on server startup, stop on shutdown."""
    cache = LiveStreamCache()
    stream_task = asyncio.create_task(_run_stream(cache))
    try:
        yield {"cache": cache, "stream_task": stream_task}
    finally:
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "bee_mcp",
    instructions=(
        "Bee is a wearable AI device that captures and transcribes the owner's "
        "conversations throughout the day. This server provides both LIVE streaming "
        "context (from a persistent SSE connection) and on-demand queries (via bee-cli). "
        "For real-time awareness, use bee_get_live_stream first — it returns instantly "
        "from an in-memory cache. For detailed data, use the specific query tools. "
        "All Bee data is end-to-end encrypted and highly confidential."
    ),
    lifespan=server_lifespan,
)

# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class PaginationInput(BaseModel):
    """Standard pagination parameters for list operations."""
    model_config = ConfigDict(str_strip_whitespace=True)

    limit: int = Field(default=20, description="Maximum results to return (1-100)", ge=1, le=100)
    cursor: Optional[str] = Field(default=None, description="Pagination cursor from a previous response")


class SearchInput(BaseModel):
    """Parameters for conversation search."""
    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(..., description="Search query text", min_length=1, max_length=500)
    limit: int = Field(default=10, description="Maximum results (1-50)", ge=1, le=50)
    neural: bool = Field(default=False, description="Use semantic/neural search instead of keyword matching")
    since: Optional[int] = Field(default=None, description="Start time as epoch milliseconds")
    until: Optional[int] = Field(default=None, description="End time as epoch milliseconds")


class TodoInput(BaseModel):
    """Parameters for creating/updating a todo."""
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(..., description="Todo text content", min_length=1, max_length=1000)
    alarm_at: Optional[str] = Field(default=None, description="Optional alarm time as ISO 8601 datetime")


class TodoUpdateInput(BaseModel):
    """Parameters for updating an existing todo."""
    model_config = ConfigDict(str_strip_whitespace=True)

    todo_id: int = Field(..., description="The todo ID to update", ge=1)
    text: Optional[str] = Field(default=None, description="New text content", max_length=1000)
    completed: Optional[bool] = Field(default=None, description="Set completion status")
    alarm_at: Optional[str] = Field(default=None, description="New alarm time (ISO 8601) or empty to clear")
    clear_alarm: bool = Field(default=False, description="Remove the alarm entirely")


class FactInput(BaseModel):
    """Parameters for creating a fact."""
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(..., description="Fact text about the owner", min_length=1, max_length=1000)


class FactUpdateInput(BaseModel):
    """Parameters for updating a fact."""
    model_config = ConfigDict(str_strip_whitespace=True)

    fact_id: int = Field(..., description="The fact ID to update", ge=1)
    text: str = Field(..., description="Updated fact text", min_length=1, max_length=1000)
    confirmed: bool = Field(default=False, description="Whether to mark as confirmed")


# ======================== LIVE STREAM (INSTANT) ========================

@mcp.tool(
    name="bee_get_live_stream",
    annotations={
        "title": "Get Live Bee Stream",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_get_live_stream(ctx: Context) -> str:
    """Get a real-time snapshot from the live SSE event stream.

    Returns instantly from the in-memory cache — no bee-cli subprocess needed.
    Includes: active conversations, recent utterances (who said what in the last
    few minutes), pending todos, journal updates, and the owner's last known location.

    This is the FASTEST way to understand what's happening RIGHT NOW. Always call
    this first for real-time context. Falls back to `bee now` if the stream is not
    connected.

    Returns:
        str: JSON snapshot of current live state including active conversations,
             recent utterances, todos, journals, location, and stream statistics.
    """
    cache: LiveStreamCache = ctx.request_context.lifespan_state["cache"]
    snapshot = cache.get_snapshot()

    if not snapshot["stream_connected"] and snapshot["stats"]["total_events"] == 0:
        # Stream hasn't connected yet — fall back to bee now
        fallback = await _run_bee(["now"])
        return json.dumps({
            "source": "fallback_bee_now",
            "note": "Live stream not yet connected. Showing bee-cli output.",
            "data": fallback,
        }, indent=2)

    return json.dumps({"source": "live_stream_cache", **snapshot}, indent=2)


@mcp.tool(
    name="bee_get_recent_utterances",
    annotations={
        "title": "Get Recent Utterances",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_get_recent_utterances(count: int, ctx: Context) -> str:
    """Get the most recent spoken utterances from the live stream cache.

    Returns verbatim transcribed words with speaker identification from the
    owner's active conversations. Useful for understanding what was just said
    without fetching the full conversation.

    Args:
        count: Number of recent utterances to return (1-200, default 30).

    Returns:
        str: JSON array of utterance objects with speaker, text, conversation_uuid, time.
    """
    count = max(1, min(count or 30, MAX_CACHED_UTTERANCES))
    cache: LiveStreamCache = ctx.request_context.lifespan_state["cache"]
    utterances = cache.get_recent_utterances(count)
    return json.dumps(utterances, indent=2)


@mcp.tool(
    name="bee_get_recent_events",
    annotations={
        "title": "Get Recent Stream Events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_get_recent_events(count: int, event_type: str, ctx: Context) -> str:
    """Get recent events from the live stream, optionally filtered by type.

    Available event types: new-utterance, new-conversation, update-conversation,
    update-conversation-summary, delete-conversation, update-location,
    todo-created, todo-updated, todo-deleted, journal-created, journal-updated,
    journal-deleted, journal-text.

    Args:
        count: Number of events to return (1-500, default 50).
        event_type: Filter to a specific event type, or empty for all events.

    Returns:
        str: JSON array of event objects with type, data, and timestamp.
    """
    count = max(1, min(count or 50, MAX_CACHED_EVENTS))
    cache: LiveStreamCache = ctx.request_context.lifespan_state["cache"]
    events = cache.get_recent_events(count, event_type or "")
    return json.dumps(events, indent=2)


# ======================== ON-DEMAND CONTEXT ========================

@mcp.tool(
    name="bee_get_current_context",
    annotations={
        "title": "Get Current Context (Full)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_current_context() -> str:
    """Get the owner's full context from the last 10 hours via bee-cli.

    Returns all conversations with complete utterance transcripts (verbatim
    words spoken), speaker identification, timestamps, and summaries. This
    is more comprehensive than the live stream snapshot but takes a few seconds
    to execute.

    Use bee_get_live_stream for instant results, use this for complete data.

    Returns:
        str: Markdown-formatted conversation context from bee-cli.
    """
    return await _run_bee(["now"])


@mcp.tool(
    name="bee_get_today_brief",
    annotations={
        "title": "Get Today's Brief",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_today_brief() -> str:
    """Get today's brief including calendar events, emails, and schedule.

    Returns:
        str: Markdown-formatted daily brief.
    """
    return await _run_bee(["today"])


# ======================== CONVERSATIONS ========================

@mcp.tool(
    name="bee_list_conversations",
    annotations={
        "title": "List Conversations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_list_conversations(params: PaginationInput) -> str:
    """List conversation summaries with pagination.

    Returns AI-generated summaries (not full transcripts). Use bee_get_conversation
    with a specific ID for accurate, complete transcripts.

    Args:
        params: Pagination parameters (limit, cursor).

    Returns:
        str: Markdown-formatted list of conversation summaries.
    """
    args = ["conversations", "list", "--limit", str(params.limit)]
    if params.cursor:
        args.extend(["--cursor", params.cursor])
    return await _run_bee(args)


@mcp.tool(
    name="bee_get_conversation",
    annotations={
        "title": "Get Full Conversation",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_conversation(conversation_id: int) -> str:
    """Get FULL conversation details by ID with complete utterance transcripts.

    Includes verbatim words spoken, speaker identification, timestamps,
    location, and AI-suggested links. Always use this when you need accurate
    information — summaries from list may contain inaccuracies.

    Args:
        conversation_id: The numeric conversation ID.

    Returns:
        str: Markdown-formatted conversation with full transcript.
    """
    return await _run_bee(["conversations", "get", str(conversation_id)])


# ======================== SEARCH ========================

@mcp.tool(
    name="bee_search_conversations",
    annotations={
        "title": "Search Conversations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_search_conversations(params: SearchInput) -> str:
    """Search through conversation history by keyword or semantically.

    Set neural=true for semantic search that understands meaning and context
    (e.g., 'what was discussed about the project deadline'). Use keyword search
    for specific terms or names.

    Args:
        params: Search parameters including query, limit, neural flag, time bounds.

    Returns:
        str: Markdown-formatted search results with matching conversations.
    """
    args = ["search", "--query", params.query, "--limit", str(params.limit)]
    if params.neural:
        args.append("--neural")
    if params.since:
        args.extend(["--since", str(params.since)])
    if params.until:
        args.extend(["--until", str(params.until)])
    return await _run_bee(args)


# ======================== FACTS ========================

@mcp.tool(
    name="bee_list_facts",
    annotations={
        "title": "List Facts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_list_facts(params: PaginationInput, unconfirmed: bool = False) -> str:
    """List facts Bee has learned about the owner from conversations.

    Facts include personal preferences, relationships, work details, interests.
    Confirmed facts are verified by the owner. Unconfirmed facts are inferred
    and may be inaccurate — always caveat them.

    Args:
        params: Pagination parameters.
        unconfirmed: If true, show pending/inferred facts instead of confirmed.

    Returns:
        str: Markdown-formatted list of facts.
    """
    args = ["facts", "list", "--limit", str(params.limit)]
    if params.cursor:
        args.extend(["--cursor", params.cursor])
    if unconfirmed:
        args.append("--unconfirmed")
    return await _run_bee(args)


@mcp.tool(
    name="bee_get_fact",
    annotations={
        "title": "Get Fact Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_fact(fact_id: int) -> str:
    """Get full details of a specific fact by ID.

    Args:
        fact_id: The numeric fact ID.

    Returns:
        str: Fact details including text, tags, confirmation status, and source.
    """
    return await _run_bee(["facts", "get", str(fact_id)])


@mcp.tool(
    name="bee_create_fact",
    annotations={
        "title": "Create Fact",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bee_create_fact(params: FactInput) -> str:
    """Create a new fact about the owner.

    The fact starts as unconfirmed until the owner verifies it via the Bee app.
    Use this to record important information learned during conversations.

    Args:
        params: Fact text.

    Returns:
        str: The created fact details.
    """
    return await _run_bee(["facts", "create", "--text", params.text])


@mcp.tool(
    name="bee_update_fact",
    annotations={
        "title": "Update Fact",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_update_fact(params: FactUpdateInput) -> str:
    """Update an existing fact's text and/or confirmation status.

    Args:
        params: Fact ID, new text, and optional confirmed flag.

    Returns:
        str: The updated fact details.
    """
    args = ["facts", "update", str(params.fact_id), "--text", params.text]
    args.extend(["--confirmed", str(params.confirmed).lower()])
    return await _run_bee(args)


@mcp.tool(
    name="bee_delete_fact",
    annotations={
        "title": "Delete Fact",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_delete_fact(fact_id: int) -> str:
    """Delete a fact. Only do this if the owner explicitly requests it.

    Args:
        fact_id: The numeric fact ID to delete.

    Returns:
        str: Confirmation of deletion.
    """
    return await _run_bee(["facts", "delete", str(fact_id)])


# ======================== TODOS ========================

@mcp.tool(
    name="bee_list_todos",
    annotations={
        "title": "List Todos",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_list_todos(params: PaginationInput) -> str:
    """List all todos/action items extracted from conversations.

    Shows both open and completed items with alarm times if set.

    Args:
        params: Pagination parameters.

    Returns:
        str: Markdown-formatted todo list.
    """
    args = ["todos", "list", "--limit", str(params.limit)]
    if params.cursor:
        args.extend(["--cursor", params.cursor])
    return await _run_bee(args)


@mcp.tool(
    name="bee_get_todo",
    annotations={
        "title": "Get Todo Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_todo(todo_id: int) -> str:
    """Get full details of a specific todo by ID.

    Args:
        todo_id: The numeric todo ID.

    Returns:
        str: Todo details including text, completion status, alarm time.
    """
    return await _run_bee(["todos", "get", str(todo_id)])


@mcp.tool(
    name="bee_create_todo",
    annotations={
        "title": "Create Todo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bee_create_todo(params: TodoInput) -> str:
    """Create a new todo/reminder for the owner.

    Args:
        params: Todo text and optional alarm_at (ISO 8601 datetime for reminder).

    Returns:
        str: The created todo details.
    """
    args = ["todos", "create", "--text", params.text]
    if params.alarm_at:
        args.extend(["--alarm-at", params.alarm_at])
    return await _run_bee(args)


@mcp.tool(
    name="bee_update_todo",
    annotations={
        "title": "Update Todo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_update_todo(params: TodoUpdateInput) -> str:
    """Update an existing todo — change text, completion, or alarm.

    Args:
        params: Todo ID and fields to update.

    Returns:
        str: The updated todo details.
    """
    args = ["todos", "update", str(params.todo_id)]
    if params.text:
        args.extend(["--text", params.text])
    if params.completed is not None:
        args.extend(["--completed", str(params.completed).lower()])
    if params.alarm_at:
        args.extend(["--alarm-at", params.alarm_at])
    if params.clear_alarm:
        args.append("--clear-alarm")
    return await _run_bee(args)


@mcp.tool(
    name="bee_complete_todo",
    annotations={
        "title": "Complete Todo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_complete_todo(todo_id: int) -> str:
    """Mark a todo as completed.

    Args:
        todo_id: The numeric todo ID to complete.

    Returns:
        str: Updated todo showing completed status.
    """
    return await _run_bee(["todos", "update", str(todo_id), "--completed", "true"])


@mcp.tool(
    name="bee_delete_todo",
    annotations={
        "title": "Delete Todo",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_delete_todo(todo_id: int) -> str:
    """Delete a todo. Only do this if the owner explicitly requests it.

    Args:
        todo_id: The numeric todo ID to delete.

    Returns:
        str: Confirmation of deletion.
    """
    return await _run_bee(["todos", "delete", str(todo_id)])


# ======================== JOURNALS ========================

@mcp.tool(
    name="bee_list_journals",
    annotations={
        "title": "List Journals",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_list_journals(params: PaginationInput) -> str:
    """List voice memo journal entries.

    Journals are intentional recordings the owner makes to capture thoughts,
    ideas, or notes — unlike conversations which are ambient recordings.

    Args:
        params: Pagination parameters.

    Returns:
        str: Markdown-formatted journal list.
    """
    args = ["journals", "list", "--limit", str(params.limit)]
    if params.cursor:
        args.extend(["--cursor", params.cursor])
    return await _run_bee(args)


@mcp.tool(
    name="bee_get_journal",
    annotations={
        "title": "Get Journal Entry",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_journal(journal_id: str) -> str:
    """Get full details of a journal entry including complete transcribed text.

    Args:
        journal_id: The journal entry UUID.

    Returns:
        str: Full journal entry with transcribed text and AI analysis.
    """
    return await _run_bee(["journals", "get", journal_id])


# ======================== DAILY SUMMARIES ========================

@mcp.tool(
    name="bee_get_daily_summary",
    annotations={
        "title": "Get Daily Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_daily_summary(date: str = "") -> str:
    """Get the AI-generated daily summary for a specific date or today.

    Args:
        date: Date in YYYY-MM-DD format, or empty for today.

    Returns:
        str: Markdown-formatted daily summary of conversations and activities.
    """
    args = ["daily"]
    if date:
        args.extend(["--date", date])
    return await _run_bee(args)


# ======================== CHANGE FEED ========================

@mcp.tool(
    name="bee_get_changes",
    annotations={
        "title": "Get Recent Changes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bee_get_changes(cursor: str = "") -> str:
    """Get recently changed entities since the last check.

    Returns new/updated facts, todos, conversations, journals, and daily
    summaries. Save the next_cursor value and pass it in subsequent calls
    to get only new changes (incremental updates).

    Args:
        cursor: Cursor from a previous call, or empty for recent changes.

    Returns:
        str: Markdown-formatted change report with next_cursor for pagination.
    """
    args = ["changed"]
    if cursor:
        args.extend(["--cursor", cursor])
    return await _run_bee(args)


# ======================== PROFILE & STATUS ========================

@mcp.tool(
    name="bee_get_profile",
    annotations={
        "title": "Get User Profile",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_profile() -> str:
    """Get the authenticated Bee user's profile information.

    Returns:
        str: User profile details.
    """
    return await _run_bee(["me"])


@mcp.tool(
    name="bee_get_status",
    annotations={
        "title": "Get Bee Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_get_status() -> str:
    """Check Bee CLI authentication status and connectivity.

    Returns:
        str: Authentication and connection status.
    """
    return await _run_bee(["status"])


# ======================== SYNC (EXPORT) ========================

@mcp.tool(
    name="bee_sync_to_markdown",
    annotations={
        "title": "Export Data to Markdown",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_sync_to_markdown(output_dir: str = "./bee-sync", targets: str = "") -> str:
    """Export all Bee data to local markdown files.

    Creates a structured directory with facts.md, todos.md, and daily/
    conversation files. Useful for local backup or feeding data to other tools.

    Args:
        output_dir: Directory to write files to (default: ./bee-sync).
        targets: Comma-separated data types to export (facts, todos, daily, conversations).
                 Empty means export everything.

    Returns:
        str: Summary of exported data.
    """
    args = ["sync", "--output", output_dir]
    if targets:
        args.extend(["--only", targets])
    return await _run_bee(args, timeout=120)


# ======================== SPEAKERS ========================

class SpeakerCreateInput(BaseModel):
    """Parameters for creating a speaker profile."""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., description="Name for the speaker profile", min_length=1, max_length=200)
    notes: str = Field(default="", description="Optional notes (e.g., relationship, context)")


@mcp.tool(
    name="bee_list_speakers",
    annotations={
        "title": "List Speaker Profiles",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_list_speakers() -> str:
    """List all speaker profiles that have been created for identification.

    Speaker profiles map real names to the generic 'Speaker 0' / 'Speaker 1'
    labels in Bee transcripts. After profiles are created and trained, Bee can
    auto-identify who is speaking in new conversations.

    Returns:
        str: List of speaker profiles with names, notes, and fingerprint status.
    """
    return await _run_bee(["speakers", "list"])


@mcp.tool(
    name="bee_create_speaker",
    annotations={
        "title": "Create Speaker Profile",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def bee_create_speaker(params: SpeakerCreateInput) -> str:
    """Create a new speaker profile for someone the owner regularly talks to.

    After creating a profile, assign it to speaker labels in known conversations,
    then use bee_learn_speakers to build speech fingerprints for auto-identification.

    Args:
        params: Speaker name and optional notes (e.g., 'Wife', 'Manager at work').

    Returns:
        str: Confirmation of profile creation.
    """
    args = ["speakers", "create", "--name", params.name]
    if params.notes:
        args.extend(["--notes", params.notes])
    return await _run_bee(args)


@mcp.tool(
    name="bee_delete_speaker",
    annotations={
        "title": "Delete Speaker Profile",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_delete_speaker(name: str) -> str:
    """Delete a speaker profile and its learned fingerprints.

    Args:
        name: The speaker profile name to delete.

    Returns:
        str: Confirmation of deletion.
    """
    return await _run_bee(["speakers", "delete", name])


@mcp.tool(
    name="bee_assign_speaker",
    annotations={
        "title": "Assign Speaker in Conversation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_assign_speaker(conversation_id: int, speaker_label: str, profile_name: str) -> str:
    """Assign a speaker label in a conversation to a named profile.

    Maps generic labels like 'Speaker 0' to real names like 'Sarah'. This is
    the manual step that trains the AI — the more conversations you assign,
    the better auto-identification becomes.

    Args:
        conversation_id: The conversation containing the speaker.
        speaker_label: The label to assign (e.g., 'Speaker 0', 'Speaker 1').
        profile_name: The speaker profile name to assign it to.

    Returns:
        str: Confirmation of assignment.
    """
    return await _run_bee(["speakers", "assign", str(conversation_id), speaker_label, profile_name])


@mcp.tool(
    name="bee_identify_speakers",
    annotations={
        "title": "AI Speaker Identification",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_identify_speakers(conversation_id: int) -> str:
    """Use AI to identify speakers in a conversation based on learned fingerprints.

    Analyzes utterances and matches them against known speaker profiles.
    Speakers identified with 80%+ confidence are automatically assigned.
    Requires an AI provider to be configured (see bee_config_set).

    Args:
        conversation_id: The conversation to analyze.

    Returns:
        str: Identification results with confidence scores per speaker.
    """
    return await _run_bee(["speakers", "identify", str(conversation_id)], timeout=60)


@mcp.tool(
    name="bee_learn_speakers",
    annotations={
        "title": "Learn Speaker Fingerprints",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_learn_speakers(profile_name: str = "", limit: int = 20) -> str:
    """Learn speech fingerprints from conversations where speakers have been manually assigned.

    Extracts vocabulary, topics, and speech patterns to build a fingerprint for
    each speaker profile. The more assigned conversations available, the more
    accurate future auto-identification will be.

    Args:
        profile_name: Optional — learn only for a specific profile. Empty = all profiles.
        limit: Maximum conversations to process per profile (default 20).

    Returns:
        str: Summary of learned fingerprints per speaker.
    """
    args = ["speakers", "learn"]
    if profile_name:
        args.extend(["--profile", profile_name])
    args.extend(["--limit", str(limit)])
    return await _run_bee(args, timeout=120)


# ======================== CITE (Fact Source Tracking) ========================

@mcp.tool(
    name="bee_cite_fact",
    annotations={
        "title": "Show Fact Citations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_cite_fact(fact_id: int) -> str:
    """Show which conversations support a specific fact, with relevance scores.

    Traces a fact back to its source conversations with text snippets showing
    exactly where the information was mentioned. Useful for verifying accuracy
    or understanding context behind a fact.

    Args:
        fact_id: The fact ID to find citations for.

    Returns:
        str: Citations with conversation IDs, relevance scores, and text snippets.
    """
    return await _run_bee(["cite", str(fact_id)], timeout=60)


@mcp.tool(
    name="bee_cite_search",
    annotations={
        "title": "Search for Evidence",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_cite_search(query: str, limit: int = 10) -> str:
    """Search conversations for evidence supporting or relating to a claim.

    Unlike regular search, this specifically looks for citeable evidence —
    statements, assertions, and data points that can back up a fact or claim.

    Args:
        query: The claim or topic to find evidence for.
        limit: Maximum results to return (default 10).

    Returns:
        str: Matching conversation excerpts with relevance scores.
    """
    args = ["cite", "search", "--query", query, "--limit", str(limit)]
    return await _run_bee(args, timeout=60)


@mcp.tool(
    name="bee_cite_rebuild",
    annotations={
        "title": "Rebuild Citation Index",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_cite_rebuild() -> str:
    """Rebuild the citation index for all confirmed facts.

    Re-indexes every confirmed fact against all conversations. Uses AI to
    verify relevance when an AI provider is configured. Run this after
    significant new conversation data has been processed.

    Returns:
        str: Summary of rebuild results (facts indexed, citations found).
    """
    return await _run_bee(["cite", "rebuild"], timeout=300)


# ======================== INFER (AI Transcript Gap Filling) ========================

@mcp.tool(
    name="bee_infer_conversation",
    annotations={
        "title": "Infer Unclear Utterances",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_infer_conversation(conversation_id: int) -> str:
    """Use AI to fill in unclear or low-confidence portions of a conversation transcript.

    Analyzes utterances where speech recognition produced garbled or uncertain
    text and uses surrounding context to reconstruct what was likely said.
    Only processes low-confidence utterances. Results are stored separately
    and never overwrite original transcriptions.

    Inferred text is always marked as [AI INFERRED] — treat as best guesses.
    Requires an AI provider to be configured (see bee_config_set).

    Args:
        conversation_id: The conversation to analyze for gaps.

    Returns:
        str: Inference results showing original vs inferred text with confidence scores.
    """
    return await _run_bee(["infer", str(conversation_id)], timeout=120)


@mcp.tool(
    name="bee_infer_list",
    annotations={
        "title": "List Stored Inferences",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_infer_list(conversation_id: int = 0, limit: int = 20) -> str:
    """List stored AI inferences (gap fills) for conversations.

    Shows previously inferred text alongside original transcriptions.
    Filter by conversation or view all inferences.

    Args:
        conversation_id: Optional — filter to a specific conversation. 0 = all.
        limit: Maximum inferences to return (default 20).

    Returns:
        str: List of inferences with original text, inferred text, and confidence.
    """
    args = ["infer", "list", "--limit", str(limit)]
    if conversation_id:
        args.extend(["--conversation", str(conversation_id)])
    return await _run_bee(args)


@mcp.tool(
    name="bee_infer_clear",
    annotations={
        "title": "Clear Stored Inferences",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_infer_clear(conversation_id: int = 0) -> str:
    """Clear stored AI inferences. Removes inferred text but never affects originals.

    Args:
        conversation_id: Optional — clear only for a specific conversation. 0 = all.

    Returns:
        str: Confirmation of cleared inferences.
    """
    args = ["infer", "clear"]
    if conversation_id:
        args.extend(["--conversation", str(conversation_id)])
    return await _run_bee(args)


# ======================== INTEGRATIONS ========================

@mcp.tool(
    name="bee_list_integrations",
    annotations={
        "title": "List Integrations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_list_integrations() -> str:
    """List all configured calendar and mail integrations.

    Shows provider names, types (calendar/mail), and connection status.

    Returns:
        str: List of configured integrations.
    """
    return await _run_bee(["integrations", "list"])


@mcp.tool(
    name="bee_add_integration",
    annotations={
        "title": "Add Integration",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bee_add_integration(integration_type: str) -> str:
    """Start the interactive flow to add a calendar or mail integration.

    Walks through provider selection (iCloud, Google, Outlook, or Generic),
    server configuration, and credential setup.

    Note: This command is interactive — the output will contain prompts that
    need to be relayed to the user for input.

    Args:
        integration_type: Either 'calendar' or 'mail'.

    Returns:
        str: Interactive setup prompts and instructions.
    """
    if integration_type not in ("calendar", "mail"):
        return "Error: integration_type must be 'calendar' or 'mail'"
    return await _run_bee(["integrations", "add", integration_type], timeout=60)


@mcp.tool(
    name="bee_remove_integration",
    annotations={
        "title": "Remove Integration",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_remove_integration(name: str) -> str:
    """Remove a configured calendar or mail integration.

    Args:
        name: The integration name to remove (from bee_list_integrations).

    Returns:
        str: Confirmation of removal.
    """
    return await _run_bee(["integrations", "remove", name])


@mcp.tool(
    name="bee_test_integration",
    annotations={
        "title": "Test Integration",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_test_integration(name: str) -> str:
    """Test connectivity for a configured integration.

    For calendar integrations, lists available calendars.
    For mail integrations, fetches 3 recent messages to verify connectivity.

    Args:
        name: The integration name to test (from bee_list_integrations).

    Returns:
        str: Test results showing available calendars or sample messages.
    """
    return await _run_bee(["integrations", "test", name], timeout=30)


# ======================== CALENDAR ========================

@mcp.tool(
    name="bee_list_calendars",
    annotations={
        "title": "List Calendars",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_list_calendars() -> str:
    """List all available calendars from configured CalDAV providers.

    Requires at least one calendar integration (set up via bee_add_integration).

    Returns:
        str: List of calendars with names and provider info.
    """
    return await _run_bee(["calendar", "list"])


@mcp.tool(
    name="bee_get_calendar_events",
    annotations={
        "title": "Get Calendar Events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_calendar_events(from_date: str = "", to_date: str = "") -> str:
    """View calendar events in a date range from connected CalDAV providers.

    Defaults to today through 7 days ahead. Results are sorted by start time
    and include date/time, summary, and location.

    Requires at least one calendar integration (set up via bee_add_integration).

    Args:
        from_date: Start date in YYYY-MM-DD format (default: today).
        to_date: End date in YYYY-MM-DD format (default: 7 days from start).

    Returns:
        str: Calendar events with times, titles, and locations.
    """
    args = ["calendar", "events"]
    if from_date:
        args.extend(["--from", from_date])
    if to_date:
        args.extend(["--to", to_date])
    return await _run_bee(args)


# ======================== MAIL ========================

@mcp.tool(
    name="bee_get_recent_mail",
    annotations={
        "title": "Get Recent Mail",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_recent_mail(limit: int = 20) -> str:
    """Show recent email messages from connected IMAP providers.

    Displays provider, date, sender, and subject. Sorted by date (newest first).
    Requires at least one mail integration (set up via bee_add_integration).

    Args:
        limit: Maximum messages to return (default 20).

    Returns:
        str: Recent emails with sender, subject, and timestamps.
    """
    return await _run_bee(["mail", "recent", "--limit", str(limit)])


@mcp.tool(
    name="bee_search_mail",
    annotations={
        "title": "Search Mail",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_search_mail(query: str, provider: str = "", limit: int = 20) -> str:
    """Search across all connected mail integrations.

    Optionally filter to a specific provider. Requires at least one mail
    integration (set up via bee_add_integration).

    Args:
        query: Search query text.
        provider: Optional — filter to a specific mail provider name.
        limit: Maximum results (default 20).

    Returns:
        str: Matching emails with sender, subject, date, and provider.
    """
    args = ["mail", "search", "--query", query, "--limit", str(limit)]
    if provider:
        args.extend(["--provider", provider])
    return await _run_bee(args)


# ======================== CONFIG ========================

@mcp.tool(
    name="bee_config_list",
    annotations={
        "title": "List Configuration",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_config_list() -> str:
    """List all Bee CLI configuration values.

    Shows AI provider settings, API keys (masked), and other local config.

    Returns:
        str: Current configuration key-value pairs.
    """
    return await _run_bee(["config", "list"])


@mcp.tool(
    name="bee_config_get",
    annotations={
        "title": "Get Config Value",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_config_get(key: str) -> str:
    """Get a specific configuration value.

    Common keys: ai_provider, openai_api_key, anthropic_api_key.

    Args:
        key: The configuration key to read.

    Returns:
        str: The configuration value (API keys may be masked).
    """
    return await _run_bee(["config", "get", key])


@mcp.tool(
    name="bee_config_set",
    annotations={
        "title": "Set Config Value",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_config_set(key: str, value: str) -> str:
    """Set a configuration value.

    Required for AI-powered features (speaker identification, inference, citations):
        bee_config_set('ai_provider', 'openai')
        bee_config_set('openai_api_key', 'sk-...')

    Or for Anthropic:
        bee_config_set('ai_provider', 'anthropic')
        bee_config_set('anthropic_api_key', 'sk-ant-...')

    Args:
        key: The configuration key to set.
        value: The value to assign.

    Returns:
        str: Confirmation of the configuration change.
    """
    return await _run_bee(["config", "set", key, value])


@mcp.tool(
    name="bee_config_delete",
    annotations={
        "title": "Delete Config Value",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_config_delete(key: str) -> str:
    """Delete a configuration entry.

    Args:
        key: The configuration key to remove.

    Returns:
        str: Confirmation of deletion.
    """
    return await _run_bee(["config", "delete", key])


# ======================== UI (Web Dashboard) ========================

@mcp.tool(
    name="bee_launch_ui",
    annotations={
        "title": "Launch Web Dashboard",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_launch_ui(port: int = 3773) -> str:
    """Launch the Bee web dashboard in the owner's default browser.

    Provides a full GUI for browsing conversations, managing facts/todos,
    viewing calendar/mail, configuring AI settings, running inference,
    and managing speaker profiles and integrations.

    The dashboard runs on localhost and stops when the process is terminated.

    Args:
        port: Port to run on (default 3773).

    Returns:
        str: Confirmation that the dashboard is starting.
    """
    # Use a short timeout since bee ui is a long-running process —
    # we just need to confirm it started successfully
    args = ["ui"]
    if port != 3773:
        args.extend(["--port", str(port)])
    # Start as background process, don't wait for it to finish
    try:
        proc = await asyncio.create_subprocess_exec(
            BEE_CLI, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )
        # Give it a moment to start
        await asyncio.sleep(2)
        if proc.returncode is not None:
            stderr = (await proc.stderr.read()).decode().strip()
            return f"Error: Dashboard failed to start — {stderr}"
        return f"Bee dashboard starting on http://localhost:{port}"
    except Exception as e:
        return f"Error launching dashboard: {e}"


# ======================== MCP RESOURCES ========================

@mcp.resource("bee://status", name="bee-status", description="Authentication and connection status.")
async def resource_status() -> str:
    return await _run_bee(["status"])


@mcp.resource("bee://profile", name="bee-profile", description="The authenticated user's profile.")
async def resource_profile() -> str:
    return await _run_bee(["me"])


@mcp.resource("bee://today", name="bee-today", description="Today's brief with calendar and emails.")
async def resource_today() -> str:
    return await _run_bee(["today"])


@mcp.resource("bee://now", name="bee-now", description="Current context — last 10 hours with full transcripts.")
async def resource_now() -> str:
    return await _run_bee(["now"])


@mcp.resource("bee://facts", name="bee-facts", description="All confirmed facts about the owner.")
async def resource_facts() -> str:
    return await _run_bee(["facts", "list"])


@mcp.resource("bee://todos", name="bee-todos", description="All todos and action items.")
async def resource_todos() -> str:
    return await _run_bee(["todos", "list"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the Bee Wearable MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
