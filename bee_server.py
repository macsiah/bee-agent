#!/usr/bin/env python3
from __future__ import annotations
"""
Unified Bee Wearable FastMCP Server
====================================
Consolidates the standalone monitor (bee_monitor.py) and MCP server (bee_mcp_server.py)
into a single FastMCP server with integrated event handling, classification, and action execution.

Architecture:
  - One FastMCP server using the lifespan pattern
  - Background SSE stream runs in the lifespan, connected to `bee stream --json`
  - LiveStreamCache is enhanced to run EventHandler logic as events arrive
  - EventHandler provides keyword detection, command capture, classification, and action execution
  - All 51+ MCP tools from bee_mcp_server.py are preserved
  - New monitor control tools added (status, trigger control, inbox, force digest)
  - Single unified process — no separate monitor daemon required

Usage:
    python bee_server.py                          # Direct execution
    (add to Claude Desktop config for MCP access)

Requirements:
    - bee-cli installed and authenticated
    - Python 3.10+
    - mcp[cli] >= 1.4.0
"""

import asyncio
import glob as globmod
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Callable

from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field, ConfigDict

# bee_classifier.py and bee_actions.py live alongside this file
from bee_classifier import BeeClassifier, ContentRouter, ClassificationResult
from bee_actions import CommandParser, ActionWriter, ActionExecutor, ActionRequest


# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------

MAX_CACHED_EVENTS = 500
MAX_CACHED_UTTERANCES = 200
STREAM_RECONNECT_BASE_DELAY = 3
STREAM_RECONNECT_MAX_DELAY = 60

DEFAULT_INBOX_DIR = Path.home() / ".bee-agent" / "inbox"
COMMANDS_DIR_NAME = "commands"
CONVERSATIONS_DIR_NAME = "conversations"
JOURNALS_DIR_NAME = "journals"
THOUGHTS_DIR_NAME = "thoughts"

CURSOR_FILE = Path.home() / ".bee-agent" / "cursor"
LOG_FILE = Path.home() / ".bee-agent" / "event-log.jsonl"
STATE_FILE = Path.home() / ".bee-agent" / "state.json"

# Timing
COMMAND_CAPTURE_WINDOW_SEC = 15
COMMAND_SILENCE_TIMEOUT_SEC = 5
COLLATION_INTERVAL_SEC = 300  # 5 minutes
DIGEST_INTERVAL_SEC = 3600.0  # 1 hour

DEFAULT_TRIGGERS = ["openclaw", "open claw", "hey openclaw", "hey open claw"]
NOTE_TO_SELF_PATTERNS = [
    re.compile(r"^(?:hey\s+)?note\s+to\s+self\b", re.IGNORECASE),
]


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


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def ts() -> str:
    """Current timestamp for display."""
    return datetime.now().strftime("%H:%M:%S")


def ts_iso() -> str:
    """Current UTC ISO timestamp."""
    return datetime.now(timezone.utc).isoformat()


def ts_file() -> str:
    """Timestamp safe for filenames."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dirs(inbox: Path):
    """Create the inbox directory structure."""
    for subdir in [COMMANDS_DIR_NAME, CONVERSATIONS_DIR_NAME, JOURNALS_DIR_NAME, THOUGHTS_DIR_NAME, "actions"]:
        (inbox / subdir).mkdir(parents=True, exist_ok=True)


def log_event(event_type: str, data: dict):
    """Append an event to the JSONL log file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": ts_iso(), "event": event_type, "data": data}
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Trigger Detection
# ---------------------------------------------------------------------------

class TriggerDetector:
    """Detects the activation keyword in utterance text."""

    def __init__(self, triggers: list[str]):
        self.patterns = []
        for trigger in triggers:
            trigger_pattern = re.escape(trigger).replace(r'\ ', r'\s+')
            pattern = re.compile(
                r'(?:^|[.!?]\s+|[,;:]\s*)' + trigger_pattern + r'\b',
                re.IGNORECASE,
            )
            self.patterns.append(pattern)

    def detect(self, text: str) -> tuple[bool, str]:
        """
        Check if text contains a trigger keyword.
        Returns (triggered, remaining_text_after_trigger).
        """
        for pattern in self.patterns:
            match = pattern.search(text)
            if match:
                remaining = text[match.end():].strip()
                return True, remaining
        return False, ""


# ---------------------------------------------------------------------------
# Command Capture State Machine
# ---------------------------------------------------------------------------

class CommandCapture:
    """
    After the trigger keyword is detected, captures subsequent utterances
    as a voice command until silence timeout or capture window expires.
    """

    def __init__(self, initial_text: str = ""):
        self.started_at = time.time()
        self.last_utterance_at = time.time()
        self.fragments: list[str] = []
        if initial_text:
            self.fragments.append(initial_text)

    def add_utterance(self, text: str):
        """Add a new utterance to the command being captured."""
        self.fragments.append(text)
        self.last_utterance_at = time.time()

    def is_expired(self) -> bool:
        """Check if the capture window has expired."""
        now = time.time()
        elapsed = now - self.started_at
        silence = now - self.last_utterance_at
        return elapsed > COMMAND_CAPTURE_WINDOW_SEC or silence > COMMAND_SILENCE_TIMEOUT_SEC

    def get_command(self) -> str:
        """Get the full captured command text."""
        return " ".join(self.fragments).strip()


# ---------------------------------------------------------------------------
# Inbox Writer
# ---------------------------------------------------------------------------

class InboxWriter:
    """Writes events to the inbox directory for OpenCLAW processing."""

    def __init__(self, inbox_dir: Path):
        self.inbox = inbox_dir
        ensure_dirs(self.inbox)

    def write_command(self, command_text: str, context_utterances: list[dict]):
        """Write a voice command to the commands queue."""
        filename = f"cmd-{ts_file()}.json"
        payload = {
            "type": "voice_command",
            "command": command_text,
            "timestamp": ts_iso(),
            "context": context_utterances[-10:],
        }
        path = self.inbox / COMMANDS_DIR_NAME / filename
        path.write_text(json.dumps(payload, indent=2))
        return path

    def write_conversation_end(self, conv_id: str, summary: str, utterances: list[dict]):
        """Write a completed conversation for processing."""
        filename = f"conv-{conv_id}-{ts_file()}.json"
        payload = {
            "type": "conversation_ended",
            "conversation_id": conv_id,
            "summary": summary,
            "utterance_count": len(utterances),
            "utterances": utterances,
            "timestamp": ts_iso(),
        }
        path = self.inbox / CONVERSATIONS_DIR_NAME / filename
        path.write_text(json.dumps(payload, indent=2))
        return path

    def write_journal(self, journal_id: str, text: str, state: str):
        """Write a journal entry (dictated note) for processing."""
        filename = f"journal-{ts_file()}.json"
        payload = {
            "type": "journal_note",
            "journal_id": journal_id,
            "text": text,
            "state": state,
            "timestamp": ts_iso(),
        }
        path = self.inbox / JOURNALS_DIR_NAME / filename
        path.write_text(json.dumps(payload, indent=2))
        return path

    def write_thought_digest(self, utterances: list[dict], todos: list[dict],
                              active_conversations: dict):
        """Write a periodic thought collation digest."""
        filename = f"thoughts-{ts_file()}.json"
        payload = {
            "type": "thought_digest",
            "timestamp": ts_iso(),
            "recent_utterances": utterances[-50:],
            "active_conversations": list(active_conversations.values()),
            "pending_todos": todos,
            "instruction": (
                "Collate these recent utterances into: (1) key themes/topics discussed, "
                "(2) any action items or commitments mentioned, (3) important information "
                "to remember, (4) suggested todos. Write the output as a structured summary."
            ),
        }
        path = self.inbox / THOUGHTS_DIR_NAME / filename
        path.write_text(json.dumps(payload, indent=2))
        return path


# ---------------------------------------------------------------------------
# Live Stream Cache (enhanced with EventHandler logic)
# ---------------------------------------------------------------------------

class LiveStreamCache:
    """
    Maintains a rolling buffer of recent SSE events from `bee stream --json`.
    Enhanced to also run EventHandler logic as events arrive.
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

        # EventHandler state
        self.handler: Optional[EventHandler] = None

    def set_handler(self, handler: EventHandler):
        """Attach the event handler after initialization."""
        self.handler = handler

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

        # Also pass to EventHandler for processing
        if self.handler:
            self.handler.handle_event(event_type, raw_json)

    def _classify_event(self, data: dict) -> str:
        """Determine the event type."""
        if "type" in data:
            return data["type"]
        if "timestamp" in data and len(data) == 1:
            return "connected"
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
            "speaker": utterance.get("speaker", "me"),
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
            "recent_utterances": list(self.utterances)[-30:],
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
# Event Handler — processes events with keyword detection and actions
# ---------------------------------------------------------------------------

class EventHandler:
    """Processes Bee events with keyword detection and ambient intelligence."""

    def __init__(self, inbox: InboxWriter, triggers: list[str], quiet: bool = False):
        self.inbox = inbox
        self.trigger = TriggerDetector(triggers)
        self.quiet = quiet

        # Content classifier & router
        self.classifier = BeeClassifier()
        self.router = ContentRouter(
            inbox_dir=inbox.inbox,
            knowledge_dir=Path.home() / ".bee-agent" / "knowledge",
            digest_dir=Path.home() / ".bee-agent" / "digests",
            openclaw_memory_dir=Path.home() / ".openclaw" / "workspace" / "memory",
        )
        self.last_digest_time: float = 0.0

        # Command parser & action executor
        self.command_parser = CommandParser()
        self.action_writer = ActionWriter(inbox_dir=inbox.inbox)
        self.action_executor = ActionExecutor()

        # State
        self.active_conversations: dict[str, dict] = {}
        self.conversation_utterances: dict[str, list[dict]] = {}
        self.recent_utterances: deque[dict] = deque(maxlen=200)
        self.recent_todos: list[dict] = []
        self.command_capture: Optional[CommandCapture] = None
        self.last_collation_time: float = time.time()
        self.finalized_conversations: set[str] = set()

        self.stats = {
            "utterances": 0,
            "conversations_started": 0,
            "conversations_ended": 0,
            "journals_received": 0,
            "commands_captured": 0,
            "thought_digests": 0,
            "items_classified": 0,
        }

    def _is_note_to_self(self, text: str) -> bool:
        return any(pattern.search(text.strip()) for pattern in NOTE_TO_SELF_PATTERNS)

    def _write_local_note_to_self(self, text: str, source_type: str, source_id: str) -> Optional[Path]:
        action = ActionRequest(
            action_type="note",
            raw_command=text,
            timestamp=ts_iso(),
            confidence=1.0,
            intent_summary=f"Save {source_type} note to Apple Notes",
            note_text=text,
            note_category="note-to-self",
        )
        action_path = self.action_writer.write(action)
        success, detail = self.action_executor.execute(action)
        action.status = "executed" if success else "failed"
        action.parse_notes = f"{source_type}:{source_id}"
        action_path.write_text(json.dumps(asdict(action), indent=2))

        if success:
            self._print(f'  📝 Note to Self saved: {detail}')
            self._alert("Note to Self Saved", text[:100])
        else:
            self._print(f"  ⚠️  Note to Self failed: {detail}")

        return action_path

    def _classify_and_route(self, text: str, source_type: str, raw_content: dict):
        """Classify content and route to categorized folders + knowledge base + memory."""
        try:
            cl = self.classifier.classify(text, source_type=source_type)
            paths = self.router.route(raw_content, cl)
            self.stats["items_classified"] += 1
            cat = cl.primary_category
            conf = f"{cl.confidence:.0%}"
            self._print(f"  🏷️  Classified as [{cat}] ({conf})")
            if cl.action_items:
                for item in cl.action_items:
                    self._print(f"  📋 Action: {item[:80]}")
            if cl.priority != "normal":
                self._print(f"  ⚠️  Priority: {cl.priority}")
        except Exception as e:
            self._print(f"  ⚠️  Classification error: {e}")

    def _check_trigger_in_text(self, text: str, source: str = "text"):
        """Check for trigger keyword in text and start command capture if found."""
        if self.command_capture is not None:
            self.command_capture.add_utterance(text)
            self._print(f"  🎤 [capturing command from {source}] {text[:80]}")
        else:
            triggered, remaining = self.trigger.detect(text)
            if triggered:
                self._print(f"  ⚡ TRIGGER DETECTED in {source}! Starting command capture...")
                self._alert("OpenCLAW Activated", f"Listening for command... ({remaining[:50]})")
                self.command_capture = CommandCapture(initial_text=remaining)

    def _check_digest(self):
        """Periodically generate a daily digest."""
        now = time.time()
        if now - self.last_digest_time < DIGEST_INTERVAL_SEC:
            return
        self.last_digest_time = now
        try:
            path = self.router.generate_daily_digest()
            self._print(f"📊 Daily digest written to: {path}")
        except Exception as e:
            self._print(f"⚠️  Digest error: {e}")

    def _print(self, msg: str):
        if not self.quiet:
            print(f"[{ts()}] {msg}")

    def _alert(self, title: str, body: str):
        self._print(f"🔔 {title}: {body}")
        try:
            subprocess.run([
                "osascript", "-e",
                f'display notification "{body[:200]}" with title "Bee: {title}"'
            ], capture_output=True, timeout=5)
        except Exception:
            pass

    def handle_event(self, event_type: str, data: dict):
        """Route an event to the appropriate handler."""
        log_event(event_type, data)

        handler_map: dict[str, Callable] = {
            "connected": self._on_connected,
            "new-utterance": self._on_utterance,
            "new-conversation": self._on_new_conversation,
            "update-conversation": self._on_update_conversation,
            "update-conversation-summary": self._on_update_summary,
            "delete-conversation": self._on_delete_conversation,
            "todo-created": self._on_todo_created,
            "todo-updated": self._on_todo_updated,
            "todo-deleted": self._on_todo_deleted,
            "journal-created": self._on_journal_created,
            "journal-updated": self._on_journal_updated,
            "journal-text": self._on_journal_text,
            "update-location": self._on_location_update,
        }

        handler = handler_map.get(event_type)
        if handler:
            handler(data)

        self._check_command_capture()
        self._check_collation()
        self._check_digest()

    # --- Core: Utterance handling with keyword detection ---

    def _on_utterance(self, data: dict):
        utterance = data.get("utterance", {})
        speaker = utterance.get("speaker", "me")
        text = utterance.get("text", "")
        conv_uuid = data.get("conversation_uuid", "")
        spoken_at = utterance.get("spoken_at", ts_iso())

        self.stats["utterances"] += 1
        entry = {
            "speaker": speaker,
            "text": text,
            "conversation_uuid": conv_uuid,
            "spoken_at": spoken_at,
            "time": ts_iso(),
        }
        self.recent_utterances.append(entry)

        if conv_uuid:
            if conv_uuid not in self.conversation_utterances:
                self.conversation_utterances[conv_uuid] = []
            self.conversation_utterances[conv_uuid].append(entry)

        self._print(f"💬 [{speaker}] {text[:120]}")

        # --- KEYWORD DETECTION ---
        if self.command_capture is not None:
            self.command_capture.add_utterance(text)
            self._print(f"  🎤 [capturing command] {text[:80]}")
        else:
            triggered, remaining = self.trigger.detect(text)
            if triggered:
                self._print(f"  ⚡ TRIGGER DETECTED! Starting command capture...")
                self._alert("OpenCLAW Activated", f"Listening for command... ({remaining[:50]})")
                self.command_capture = CommandCapture(initial_text=remaining)

    def _check_command_capture(self):
        """Check if an active command capture has expired, and finalize it."""
        if self.command_capture is None:
            return
        if not self.command_capture.is_expired():
            return

        command_text = self.command_capture.get_command()
        self.command_capture = None

        if not command_text.strip():
            self._print("  🎤 Command capture expired with no content.")
            return

        self.stats["commands_captured"] += 1
        self._print(f"  🎤 COMMAND CAPTURED: {command_text[:200]}")
        self._alert("Command Captured", command_text[:100])

        context = list(self.recent_utterances)[-15:]
        path = self.inbox.write_command(command_text, context)
        self._print(f"  📥 Written to: {path}")

        # --- PARSE into structured action via LLM ---
        action = self.command_parser.parse(command_text, context=context)
        action_path = self.action_writer.write(action)
        self._print(f"  🎯 Action: {action.action_type} (confidence={action.confidence:.0%})")
        if action.intent_summary:
            self._print(f"  💡 Intent: {action.intent_summary[:120]}")

        if action.action_type == "send_message":
            channel = action.channel or "unknown"
            self._print(f"  📨 [{channel}] To: {action.recipient} | Body: {action.message_body[:80]}")

            if self.action_executor.can_execute_locally(action):
                success, detail = self.action_executor.execute(action)
                if success:
                    self._print(f"  ✅ {detail}")
                    target = action.recipient or "self"
                    self._alert("Message Sent", f"{channel} to {target}")
                    action.status = "executed"
                else:
                    self._print(f"  ⚠️  {channel} send failed: {detail} — queued for OpenCLAW")
                    action.status = "pending"
                action_path.write_text(json.dumps(asdict(action), indent=2))
            else:
                self._print(f"  📬 Queued for OpenCLAW ({channel})")

        elif action.action_type == "set_reminder":
            self._print(f"  ⏰ Reminder: {action.reminder_text[:80]}")
            if action.reminder_time:
                self._print(f"  🕐 Time: {action.reminder_time}")
            if self.action_executor.can_execute_locally(action):
                success, detail = self.action_executor.execute(action)
                action.status = "executed" if success else "failed"
                action_path.write_text(json.dumps(asdict(action), indent=2))
                if success:
                    self._print(f"  ✅ {detail}")
                    self._alert("Reminder Saved", action.reminder_text[:100])
                else:
                    self._print(f"  ⚠️  Reminder save failed: {detail}")
            else:
                self._alert("Reminder Set", action.reminder_text[:100])

        elif action.action_type in ("search", "query"):
            query = action.search_query or action.intent_summary
            self._print(f"  🔍 Query: {query[:80]}")

        elif action.action_type == "note":
            self._print(f"  📝 Note: {action.note_text[:80]}")
            if self.action_executor.can_execute_locally(action):
                success, detail = self.action_executor.execute(action)
                action.status = "executed" if success else "failed"
                action_path.write_text(json.dumps(asdict(action), indent=2))
                if success:
                    self._print(f"  ✅ {detail}")
                    self._alert("Note Saved", (action.note_text or command_text)[:100])
                else:
                    self._print(f"  ⚠️  Note save failed: {detail}")

        elif action.action_type == "summarize":
            self._print(f"  📊 Summarize: {action.intent_summary[:80]}")

        elif action.action_type == "control":
            self._print(f"  ⚙️  Control: {action.intent_summary[:80]}")

        elif action.action_type == "schedule":
            self._print(f"  📅 Schedule: {action.intent_summary[:80]}")

        elif action.action_type == "play_media":
            self._print(f"  🎵 Media: {action.intent_summary[:80]}")

        else:
            self._print(f"  🔧 Command: {action.intent_summary or action.openclaw_command[:80]}")

        self._print(f"  📥 Action written to: {action_path}")

        # --- CLASSIFY & ROUTE command ---
        self._classify_and_route(command_text, "command", {
            "type": "voice_command", "command": command_text,
            "action": asdict(action),
            "timestamp": ts_iso(), "context": context[-10:],
        })

    # --- Conversations ---

    def _on_connected(self, data: dict):
        self._print("Connected to Bee event stream")
        self._alert("Bee Connected", "Real-time monitoring active")

    def _on_new_conversation(self, data: dict):
        conv = data.get("conversation", {})
        conv_id = str(conv.get("id", "?"))
        conv_uuid = conv.get("conversation_uuid") or conv.get("uuid", conv_id)
        self.stats["conversations_started"] += 1
        self.active_conversations[conv_id] = {
            "id": conv.get("id"),
            "uuid": conv_uuid,
            "state": conv.get("state", "unknown"),
            "title": conv.get("title", ""),
            "short_summary": "",
            "started": ts_iso(),
        }
        self.conversation_utterances[conv_uuid] = []
        self._print(f"🟢 New conversation started (id={conv_id}, uuid={conv_uuid[:8]}...)")
        self._alert("Conversation Started", f"ID {conv_id}")

    def _on_update_conversation(self, data: dict):
        conv = data.get("conversation", {})
        conv_id = str(conv.get("id", "?"))
        conv_uuid = conv.get("conversation_uuid") or conv.get("uuid", conv_id)
        state = conv.get("state", "unknown")
        title = conv.get("title", "")
        summary = conv.get("short_summary", "")
        end_time = conv.get("end_time")
        has_ended = state in ("completed", "ended", "finalized") or bool(end_time)

        if has_ended:
            if conv_id in self.finalized_conversations:
                return

            self.stats["conversations_ended"] += 1
            self.finalized_conversations.add(conv_id)
            conv_data = self.active_conversations.pop(conv_id, {})
            utterances = self.conversation_utterances.pop(conv_uuid, [])

            self._alert("Conversation Ended", f"{title or summary or f'ID {conv_id}'}")

            full_summary = (
                conv.get("summary")
                or summary
                or title
                or conv_data.get("short_summary", "")
                or conv_data.get("title", "")
                or "(no summary)"
            )
            path = self.inbox.write_conversation_end(conv_id, full_summary, utterances)
            self._print(f"  📥 Conversation written to: {path} ({len(utterances)} utterances)")

            # --- CLASSIFY & ROUTE conversation ---
            all_text = full_summary + " " + " ".join(u.get("text", "") for u in utterances)
            self._classify_and_route(all_text, "conversation", {
                "type": "conversation_ended", "conversation_id": conv_id,
                "summary": full_summary, "utterance_count": len(utterances),
                "utterances": utterances, "timestamp": ts_iso(),
                "state": state, "end_time": end_time,
            })
        else:
            if conv_id in self.active_conversations:
                self.active_conversations[conv_id]["state"] = state
                if title:
                    self.active_conversations[conv_id]["title"] = title
                if summary:
                    self.active_conversations[conv_id]["short_summary"] = summary
            if title:
                self._print(f"📝 Conversation {conv_id} updated: {title[:100]}")

    def _on_update_summary(self, data: dict):
        conv_id = str(data.get("conversation_id", "?"))
        summary = data.get("short_summary", "")
        if conv_id in self.active_conversations and summary:
            self.active_conversations[conv_id]["short_summary"] = summary
            self._print(f"📋 Summary for {conv_id}: {summary[:120]}")

    def _on_delete_conversation(self, data: dict):
        conv = data.get("conversation", {})
        conv_id = str(conv.get("id", "?"))
        self.active_conversations.pop(conv_id, None)

    # --- Todos ---

    def _on_todo_created(self, data: dict):
        todo = data.get("todo", {})
        text = todo.get("text", "")
        self.recent_todos.append({"id": todo.get("id"), "text": text, "completed": False})
        self._alert("New Todo", text[:200])

    def _on_todo_updated(self, data: dict):
        todo = data.get("todo", {})
        if todo.get("completed"):
            self._print(f"✅ Todo completed: {todo.get('text', '')[:100]}")

    def _on_todo_deleted(self, data: dict):
        pass

    # --- Journals ---

    def _on_journal_created(self, data: dict):
        journal = data.get("journal", {})
        state = journal.get("state", "unknown")
        self.stats["journals_received"] += 1
        self._print(f"📓 Journal recording started (state={state})")
        self._alert("Note Recording", "Dictation in progress...")

    def _on_journal_updated(self, data: dict):
        journal = data.get("journal", {})
        state = journal.get("state", "unknown")
        text = journal.get("text", "")
        journal_id = str(journal.get("id", "unknown"))

        if state == "READY" and text:
            self._alert("Note Ready", text[:150])
            path = self.inbox.write_journal(journal_id, text, state)
            self._print(f"  📥 Journal written to: {path}")

            if self._is_note_to_self(text):
                self._write_local_note_to_self(text, "journal", journal_id)

            # --- CLASSIFY & ROUTE ---
            self._classify_and_route(text, "journal", {
                "type": "journal_note", "journal_id": journal_id,
                "text": text, "state": state, "timestamp": ts_iso(),
            })

            # --- KEYWORD DETECTION in finalized journal ---
            self._check_trigger_in_text(text, source="journal")

    def _on_journal_text(self, data: dict):
        text = data.get("text", "")
        journal_id = str(data.get("journalId", "unknown"))
        if text:
            self._print(f"📓 Journal text: {text[:150]}")
            path = self.inbox.write_journal(journal_id, text, "TRANSCRIBING")
            self._print(f"  📥 Journal text written to: {path}")

            # --- KEYWORD DETECTION in journal text ---
            self._check_trigger_in_text(text, source="journal-text")

    # --- Location ---

    def _on_location_update(self, data: dict):
        loc = data.get("location", {})
        name = loc.get("name", "")
        if name:
            self._print(f"📍 Location: {name}")

    # --- Ambient Intelligence: Periodic thought collation ---

    def _check_collation(self):
        """Periodically write a thought digest for OpenCLAW to process."""
        elapsed = time.time() - self.last_collation_time
        if elapsed < COLLATION_INTERVAL_SEC:
            return
        if len(self.recent_utterances) < 5:
            return

        self.last_collation_time = time.time()
        self.stats["thought_digests"] += 1

        path = self.inbox.write_thought_digest(
            list(self.recent_utterances),
            self.recent_todos,
            self.active_conversations,
        )
        self._print(f"🧠 Thought digest written to: {path}")

    def save_state(self):
        """Persist current state to disk for recovery."""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "timestamp": ts_iso(),
            "stats": self.stats,
            "active_conversations": self.active_conversations,
            "recent_todos": self.recent_todos[-20:],
        }
        STATE_FILE.write_text(json.dumps(state, indent=2))


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
            delay = STREAM_RECONNECT_BASE_DELAY

            async for line in proc.stdout:
                text = line.decode().strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                    cache.ingest(data)
                except json.JSONDecodeError:
                    pass

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

        await asyncio.sleep(delay)
        delay = min(delay * 2, STREAM_RECONNECT_MAX_DELAY)


# ---------------------------------------------------------------------------
# Server lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def server_lifespan():
    """Start the background SSE stream on server startup, stop on shutdown."""
    inbox = InboxWriter(DEFAULT_INBOX_DIR)
    handler = EventHandler(inbox=inbox, triggers=list(DEFAULT_TRIGGERS), quiet=False)

    cache = LiveStreamCache()
    cache.set_handler(handler)

    stream_task = asyncio.create_task(_run_stream(cache))
    try:
        yield {"cache": cache, "handler": handler, "inbox": inbox}
    finally:
        handler.save_state()
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# MCP Server & Input Models
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "bee_unified",
    instructions=(
        "Bee is a wearable AI device that captures and transcribes the owner's "
        "conversations throughout the day. This unified server combines live streaming "
        "context with on-demand queries. Use bee_get_live_stream for instant real-time "
        "awareness from the in-memory cache. For detailed data, use specific query tools. "
        "The integrated monitor automatically processes events: keyword detection, command "
        "capture, classification, and action execution all happen automatically. "
        "All Bee data is end-to-end encrypted and highly confidential."
    ),
    lifespan=server_lifespan,
)


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


class SpeakerCreateInput(BaseModel):
    """Parameters for creating a speaker profile."""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., description="Speaker name", min_length=1, max_length=100)
    samples: int = Field(default=30, description="Number of utterances to use for profile", ge=5, le=100)


# ---------------------------------------------------------------------------
# LIVE STREAM (INSTANT)
# ---------------------------------------------------------------------------

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

    Returns instantly from the in-memory cache. Includes active conversations,
    recent utterances, pending todos, journal updates, location, and stream stats.

    Returns:
        str: JSON snapshot of current live state.
    """
    cache: LiveStreamCache = ctx.request_context.lifespan_state["cache"]
    snapshot = cache.get_snapshot()

    if not snapshot["stream_connected"] and snapshot["stats"]["total_events"] == 0:
        fallback = await _run_bee(["now"])
        return json.dumps({
            "source": "fallback_bee_now",
            "note": "Live stream not yet connected.",
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

    Args:
        count: Number of recent utterances to return (1-200, default 30).

    Returns:
        str: JSON array of utterance objects.
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

    Args:
        count: Number of events to return (1-500, default 50).
        event_type: Filter to a specific event type, or empty for all.

    Returns:
        str: JSON array of event objects.
    """
    count = max(1, min(count or 50, MAX_CACHED_EVENTS))
    cache: LiveStreamCache = ctx.request_context.lifespan_state["cache"]
    events = cache.get_recent_events(count, event_type or "")
    return json.dumps(events, indent=2)


# ---------------------------------------------------------------------------
# MONITOR CONTROL TOOLS (NEW)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="bee_monitor_status",
    annotations={
        "title": "Get Monitor Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_monitor_status(ctx: Context) -> str:
    """Get current monitor statistics and state.

    Returns active conversations, trigger state, command capture state, and stats.

    Returns:
        str: JSON with monitor statistics and state.
    """
    handler: EventHandler = ctx.request_context.lifespan_state["handler"]
    return json.dumps({
        "stats": handler.stats,
        "active_conversations": handler.active_conversations,
        "command_capture_active": handler.command_capture is not None,
        "recent_utterances_count": len(handler.recent_utterances),
        "recent_todos": handler.recent_todos,
        "last_collation": handler.last_collation_time,
    }, indent=2)


@mcp.tool(
    name="bee_monitor_set_trigger",
    annotations={
        "title": "Set Trigger Keyword",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_monitor_set_trigger(trigger_text: str, ctx: Context) -> str:
    """Change the trigger keyword for command activation.

    Args:
        trigger_text: New trigger keyword (e.g., 'openclaw', 'hey claude').

    Returns:
        str: Confirmation of trigger change.
    """
    handler: EventHandler = ctx.request_context.lifespan_state["handler"]
    handler.trigger = TriggerDetector([trigger_text.strip()])
    return json.dumps({
        "status": "success",
        "trigger": trigger_text.strip(),
        "message": "Trigger keyword updated"
    }, indent=2)


@mcp.tool(
    name="bee_monitor_get_inbox",
    annotations={
        "title": "List Recent Inbox Items",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_monitor_get_inbox(limit: int = 20, ctx: Context = None) -> str:
    """List recent items written to the monitor's inbox.

    Returns recent commands, conversations, journals, and actions.

    Args:
        limit: Maximum items to return per category (default 20).

    Returns:
        str: JSON with recent inbox items by type.
    """
    handler: EventHandler = ctx.request_context.lifespan_state["handler"]
    inbox_dir = handler.inbox.inbox

    result = {}
    for category in ["commands", "conversations", "journals", "thoughts", "actions"]:
        cat_dir = inbox_dir / category
        if cat_dir.exists():
            files = sorted(cat_dir.glob("*.json"), reverse=True)[:limit]
            result[category] = [f.name for f in files]
        else:
            result[category] = []

    return json.dumps({
        "inbox_directory": str(inbox_dir),
        "items": result,
    }, indent=2)


@mcp.tool(
    name="bee_monitor_force_digest",
    annotations={
        "title": "Force Thought Digest",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def bee_monitor_force_digest(ctx: Context) -> str:
    """Force immediate generation of a thought digest.

    Returns:
        str: JSON with digest file path and status.
    """
    handler: EventHandler = ctx.request_context.lifespan_state["handler"]
    handler.last_collation_time = 0  # Force check on next event
    path = handler.inbox.write_thought_digest(
        list(handler.recent_utterances),
        handler.recent_todos,
        handler.active_conversations,
    )
    return json.dumps({
        "status": "success",
        "digest_file": str(path),
        "message": "Thought digest generated"
    }, indent=2)


# ---------------------------------------------------------------------------
# ON-DEMAND CONTEXT
# ---------------------------------------------------------------------------

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

    Returns all conversations with complete utterance transcripts.

    Returns:
        str: Markdown-formatted conversation context.
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


# ---------------------------------------------------------------------------
# CONVERSATIONS
# ---------------------------------------------------------------------------

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

    Args:
        conversation_id: The numeric conversation ID.

    Returns:
        str: Markdown-formatted conversation with full transcript.
    """
    return await _run_bee(["conversations", "get", str(conversation_id)])


# ---------------------------------------------------------------------------
# SEARCH
# ---------------------------------------------------------------------------

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

    Args:
        params: Search parameters including query, limit, neural flag, time bounds.

    Returns:
        str: Markdown-formatted search results.
    """
    args = ["search", "--query", params.query, "--limit", str(params.limit)]
    if params.neural:
        args.append("--neural")
    if params.since:
        args.extend(["--since", str(params.since)])
    if params.until:
        args.extend(["--until", str(params.until)])
    return await _run_bee(args)


# ---------------------------------------------------------------------------
# FACTS
# ---------------------------------------------------------------------------

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
    if params.confirmed:
        args.append("--confirmed")
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
    """Delete a fact by ID.

    Args:
        fact_id: The numeric fact ID to delete.

    Returns:
        str: Confirmation of deletion.
    """
    return await _run_bee(["facts", "delete", str(fact_id)])


# ---------------------------------------------------------------------------
# TODOS
# ---------------------------------------------------------------------------

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
    """List all todos and action items with pagination.

    Args:
        params: Pagination parameters (limit, cursor).

    Returns:
        str: Markdown-formatted list of todos.
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
        str: Todo details including text, completion status, and alarm time.
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
    """Create a new todo item.

    Args:
        params: Todo text and optional alarm time.

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
    """Update an existing todo.

    Args:
        params: Todo ID, and optional new text, completion status, or alarm time.

    Returns:
        str: The updated todo details.
    """
    args = ["todos", "update", str(params.todo_id)]
    if params.text:
        args.extend(["--text", params.text])
    if params.completed is not None:
        args.append("--completed" if params.completed else "--not-completed")
    if params.clear_alarm:
        args.append("--clear-alarm")
    elif params.alarm_at:
        args.extend(["--alarm-at", params.alarm_at])
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
        todo_id: The numeric todo ID.

    Returns:
        str: Confirmation of completion.
    """
    return await _run_bee(["todos", "complete", str(todo_id)])


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
    """Delete a todo by ID.

    Args:
        todo_id: The numeric todo ID to delete.

    Returns:
        str: Confirmation of deletion.
    """
    return await _run_bee(["todos", "delete", str(todo_id)])


# ---------------------------------------------------------------------------
# JOURNALS
# ---------------------------------------------------------------------------

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
    """List journal entries with pagination.

    Args:
        params: Pagination parameters (limit, cursor).

    Returns:
        str: Markdown-formatted list of journal entries.
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
    """Get full details of a specific journal entry by ID.

    Args:
        journal_id: The journal ID.

    Returns:
        str: Journal entry with full text and metadata.
    """
    return await _run_bee(["journals", "get", journal_id])


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
    """Get a summarized summary of a specific day's activity.

    Args:
        date: Date in YYYY-MM-DD format (default: today).

    Returns:
        str: Markdown-formatted daily summary.
    """
    args = ["daily", "summary"]
    if date:
        args.extend(["--date", date])
    return await _run_bee(args)


# ---------------------------------------------------------------------------
# CHANGES & SYNC
# ---------------------------------------------------------------------------

@mcp.tool(
    name="bee_get_changes",
    annotations={
        "title": "Get Changes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_get_changes(cursor: str = "") -> str:
    """Poll for incremental changes using cursor-based pagination.

    Useful for syncing data incrementally. Returns conversations, journals, todos
    that have changed since the last cursor position.

    Args:
        cursor: Cursor from previous response, or empty for latest changes.

    Returns:
        str: JSON with changed items and next cursor for polling.
    """
    args = ["changed"]
    if cursor:
        args.extend(["--cursor", cursor])
    return await _run_bee(args)


@mcp.tool(
    name="bee_sync_to_markdown",
    annotations={
        "title": "Sync to Markdown",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bee_sync_to_markdown(output_dir: str = "./bee-sync", targets: str = "") -> str:
    """Sync Bee data to local Markdown files for external tools (Obsidian, logseq, etc).

    Writes facts, todos, journal entries, and conversations to organized markdown.

    Args:
        output_dir: Directory to write markdown files (default ./bee-sync).
        targets: Comma-separated targets to sync (facts, todos, journals, all).

    Returns:
        str: Confirmation and file paths written.
    """
    args = ["sync", "--output-dir", output_dir]
    if targets:
        args.extend(["--targets", targets])
    return await _run_bee(args)


# ---------------------------------------------------------------------------
# PROFILE & STATUS
# ---------------------------------------------------------------------------

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
    """Get the authenticated user's profile.

    Returns name, email, subscription status, and device info.

    Returns:
        str: User profile details.
    """
    return await _run_bee(["me"])


@mcp.tool(
    name="bee_get_status",
    annotations={
        "title": "Get Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bee_get_status() -> str:
    """Get authentication and connection status.

    Returns:
        str: Status information including logged-in user and device status.
    """
    return await _run_bee(["status"])


# ---------------------------------------------------------------------------
# SPEAKER IDENTIFICATION & LEARNING
# ---------------------------------------------------------------------------

@mcp.tool(
    name="bee_list_speakers",
    annotations={
        "title": "List Speakers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_list_speakers() -> str:
    """List all speaker profiles learned by Bee.

    Returns speaker names and acoustic profiles.

    Returns:
        str: List of speaker profiles.
    """
    return await _run_bee(["speakers", "list"])


@mcp.tool(
    name="bee_create_speaker",
    annotations={
        "title": "Create Speaker Profile",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bee_create_speaker(params: SpeakerCreateInput) -> str:
    """Create a new speaker profile by learning from utterances.

    Analyzes the N most recent utterances from a speaker and builds
    an acoustic profile for future identification.

    Args:
        params: Speaker name and number of utterance samples to learn from.

    Returns:
        str: Confirmation of speaker profile creation.
    """
    args = ["speakers", "create", params.name, "--samples", str(params.samples)]
    return await _run_bee(args)


@mcp.tool(
    name="bee_delete_speaker",
    annotations={
        "title": "Delete Speaker",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_delete_speaker(name: str) -> str:
    """Delete a speaker profile by name.

    Args:
        name: The speaker name to delete.

    Returns:
        str: Confirmation of deletion.
    """
    return await _run_bee(["speakers", "delete", name])


@mcp.tool(
    name="bee_assign_speaker",
    annotations={
        "title": "Assign Speaker to Conversation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_assign_speaker(conversation_id: int, speaker_label: str, profile_name: str) -> str:
    """Assign a speaker label in a conversation to a learned speaker profile.

    Args:
        conversation_id: The numeric conversation ID.
        speaker_label: The speaker label in the conversation (e.g., "speaker_2").
        profile_name: The speaker profile name to assign.

    Returns:
        str: Confirmation of assignment.
    """
    return await _run_bee([
        "speakers", "assign", str(conversation_id), speaker_label, profile_name
    ])


@mcp.tool(
    name="bee_identify_speakers",
    annotations={
        "title": "Identify Speakers in Conversation",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_identify_speakers(conversation_id: int) -> str:
    """Use acoustic profiles to identify speakers in a conversation.

    Matches unknown speakers against learned profiles to identify them by name.

    Args:
        conversation_id: The numeric conversation ID.

    Returns:
        str: Speaker identification results for the conversation.
    """
    return await _run_bee(["speakers", "identify", str(conversation_id)])


@mcp.tool(
    name="bee_learn_speakers",
    annotations={
        "title": "Bulk Learn Speakers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bee_learn_speakers(profile_name: str = "", limit: int = 20) -> str:
    """Learn speaker profiles from recent conversations in bulk.

    Args:
        profile_name: Optional — only learn utterances from a specific speaker.
        limit: Maximum recent conversations to learn from (default 20).

    Returns:
        str: Summary of learned profiles.
    """
    args = ["speakers", "learn", "--limit", str(limit)]
    if profile_name:
        args.extend(["--speaker", profile_name])
    return await _run_bee(args)


# ---------------------------------------------------------------------------
# FACTS: CITATION & INFERENCE
# ---------------------------------------------------------------------------

@mcp.tool(
    name="bee_cite_fact",
    annotations={
        "title": "Cite Fact",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_cite_fact(fact_id: int) -> str:
    """Get the source conversation(s) for a fact with full citations.

    Args:
        fact_id: The numeric fact ID.

    Returns:
        str: Fact details with source conversations and citations.
    """
    return await _run_bee(["citations", "fact", str(fact_id)])


@mcp.tool(
    name="bee_cite_search",
    annotations={
        "title": "Search with Citations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_cite_search(query: str, limit: int = 10) -> str:
    """Search conversations and return results with source citations.

    Useful for attributing information back to the owner's conversations.

    Args:
        query: Search query text.
        limit: Maximum results (default 10).

    Returns:
        str: Search results with citations to source conversations.
    """
    return await _run_bee(["citations", "search", "--query", query, "--limit", str(limit)])


@mcp.tool(
    name="bee_cite_rebuild",
    annotations={
        "title": "Rebuild Citation Index",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bee_cite_rebuild() -> str:
    """Rebuild the citation index from scratch.

    Regenerates the mapping between facts and source conversations.

    Returns:
        str: Confirmation and statistics about the rebuild.
    """
    return await _run_bee(["citations", "rebuild"])


# ---------------------------------------------------------------------------
# AI INFERENCE (LLM-powered)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="bee_infer_conversation",
    annotations={
        "title": "Run Inference on Conversation",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bee_infer_conversation(conversation_id: int) -> str:
    """Run LLM inference on a conversation to extract insights, facts, and summaries.

    Args:
        conversation_id: The numeric conversation ID.

    Returns:
        str: LLM-generated insights and inferred facts.
    """
    return await _run_bee(["inferences", "run", str(conversation_id)])


@mcp.tool(
    name="bee_infer_list",
    annotations={
        "title": "List Inferences",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_infer_list(conversation_id: int = 0, limit: int = 20) -> str:
    """List recent LLM inferences.

    Args:
        conversation_id: Optional — filter to a specific conversation.
        limit: Maximum results to return (default 20).

    Returns:
        str: List of inferences with content and metadata.
    """
    args = ["inferences", "list", "--limit", str(limit)]
    if conversation_id:
        args.extend(["--conversation", str(conversation_id)])
    return await _run_bee(args)


@mcp.tool(
    name="bee_infer_clear",
    annotations={
        "title": "Clear Inferences",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bee_infer_clear(conversation_id: int = 0) -> str:
    """Clear cached inferences to regenerate them.

    Args:
        conversation_id: Optional — clear only for a specific conversation.

    Returns:
        str: Confirmation of cache clear.
    """
    args = ["inferences", "clear"]
    if conversation_id:
        args.extend(["--conversation", str(conversation_id)])
    return await _run_bee(args)


# ---------------------------------------------------------------------------
# INTEGRATIONS (Calendar & Mail)
# ---------------------------------------------------------------------------

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
        name: The integration name to remove.

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

    Args:
        name: The integration name to test.

    Returns:
        str: Test results showing available calendars or sample messages.
    """
    return await _run_bee(["integrations", "test", name], timeout=30)


# ---------------------------------------------------------------------------
# CALENDAR
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# MAIL
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# UI (Web Dashboard)
# ---------------------------------------------------------------------------

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

    Args:
        port: Port to run on (default 3773).

    Returns:
        str: Confirmation that the dashboard is starting.
    """
    args = ["ui"]
    if port != 3773:
        args.extend(["--port", str(port)])
    try:
        proc = await asyncio.create_subprocess_exec(
            BEE_CLI, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )
        await asyncio.sleep(2)
        if proc.returncode is not None:
            stderr = (await proc.stderr.read()).decode().strip()
            return f"Error: Dashboard failed to start — {stderr}"
        return f"Bee dashboard starting on http://localhost:{port}"
    except Exception as e:
        return f"Error launching dashboard: {e}"


# ---------------------------------------------------------------------------
# MCP RESOURCES
# ---------------------------------------------------------------------------

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
    """Run the Bee Unified MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
