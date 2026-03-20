#!/usr/bin/env python3
from __future__ import annotations
"""
Bee Wearable Monitoring Agent
==============================
An always-on agent that monitors the Bee wearable's real-time event stream,
detects voice commands (keyword activation), collates thoughts, and writes
structured outputs (todos, conversation logs, command queue) for OpenCLAW.

Core behaviors:
  1. CONVERSATIONS (button push) — Detected, tracked, and summarized when they end.
     Full transcript written to the inbox for OpenCLAW processing.
  2. JOURNALS/NOTES (button hold) — Detected immediately. Transcribed text written
     to the inbox as a thought/note for collation.
  3. KEYWORD ACTIVATION — When the owner says "OpenCLAW" (or configured trigger),
     the following utterances are captured as a voice command and written to the
     command queue for OpenCLAW to execute.
  4. AMBIENT INTELLIGENCE — All utterances are monitored. Periodically, the agent
     writes a collated thought digest and auto-generated todo list.

Architecture:
  bee stream --json → this agent → writes files to ~/.bee-agent/inbox/
  OpenCLAW scheduled task → reads inbox → processes commands, collates thoughts

Usage:
    python bee_monitor.py                    # Start with defaults
    python bee_monitor.py --trigger openclaw # Custom trigger word
    python bee_monitor.py --mode digest      # One-shot summary
    python bee_monitor.py --inbox ~/my-inbox # Custom inbox directory

Requirements:
    - bee-cli installed and authenticated
    - Python 3.10+
"""

import argparse
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

from dataclasses import asdict

from bee_classifier import BeeClassifier, ContentRouter, classify_and_route, ClassificationResult
from bee_actions import CommandParser, ActionWriter, ActionExecutor, ActionRequest


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_INBOX_DIR = Path.home() / ".bee-agent" / "inbox"
COMMANDS_DIR_NAME = "commands"
CONVERSATIONS_DIR_NAME = "conversations"
JOURNALS_DIR_NAME = "journals"
THOUGHTS_DIR_NAME = "thoughts"

CURSOR_FILE = Path.home() / ".bee-agent" / "cursor"
LOG_FILE = Path.home() / ".bee-agent" / "event-log.jsonl"
STATE_FILE = Path.home() / ".bee-agent" / "state.json"

# How many seconds of utterances to capture after the trigger keyword
COMMAND_CAPTURE_WINDOW_SEC = 15
# How many seconds of silence (no utterances) ends a command capture
COMMAND_SILENCE_TIMEOUT_SEC = 5
# Minimum collation interval (don't collate more often than this)
COLLATION_INTERVAL_SEC = 300  # 5 minutes

# Default trigger keywords (case-insensitive, fuzzy matched)
DEFAULT_TRIGGERS = ["openclaw", "open claw", "hey openclaw", "hey open claw"]
NOTE_TO_SELF_PATTERNS = [
    re.compile(r"^(?:hey\s+)?note\s+to\s+self\b", re.IGNORECASE),
]


def find_bee_cli() -> str:
    """Locate the bee CLI binary."""
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


BEE_CLI = find_bee_cli()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_bee(args: list[str], timeout: int = 30) -> str:
    """Run a bee-cli command and return stdout."""
    cmd = [BEE_CLI] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if result.returncode != 0:
            return f"[ERROR] {result.stderr.strip() or result.stdout.strip()}"
        return result.stdout
    except FileNotFoundError:
        return "[ERROR] bee-cli not found"
    except subprocess.TimeoutExpired:
        return "[ERROR] Command timed out"


def run_bee_json(args: list[str], timeout: int = 30) -> dict | list | None:
    """Run a bee-cli command with --json and return parsed JSON."""
    output = run_bee(args + ["--json"], timeout=timeout)
    if output.startswith("[ERROR]"):
        print(output, file=sys.stderr)
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


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


def load_cursor() -> Optional[str]:
    """Load the last saved cursor from disk."""
    if CURSOR_FILE.exists():
        return CURSOR_FILE.read_text().strip() or None
    return None


def save_cursor(cursor: str):
    """Save cursor to disk after successful processing."""
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(cursor)


# ---------------------------------------------------------------------------
# Keyword Trigger Detection
# ---------------------------------------------------------------------------

class TriggerDetector:
    """Detects the activation keyword in utterance text."""

    def __init__(self, triggers: list[str]):
        # Build regex patterns that prefer command-style invocations at the
        # start of an utterance or after punctuation, not casual mentions
        # in the middle of a sentence.
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
                # Everything after the trigger word is the start of the command
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
# Inbox Writer — writes structured files for OpenCLAW to consume
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
            "context": context_utterances[-10:],  # last 10 utterances for context
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
# Event Handler — the brain
# ---------------------------------------------------------------------------

class EventHandler:
    """Processes Bee events with keyword detection and ambient intelligence."""

    def __init__(self, inbox: InboxWriter, triggers: list[str],
                 webhook_url: Optional[str] = None, quiet: bool = False):
        self.inbox = inbox
        self.trigger = TriggerDetector(triggers)
        self.webhook_url = webhook_url
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
        self.DIGEST_INTERVAL_SEC: float = 3600.0  # generate digest hourly

        # Command parser & action executor
        self.command_parser = CommandParser()
        self.action_writer = ActionWriter(inbox_dir=inbox.inbox)
        self.action_executor = ActionExecutor()

        # State
        self.active_conversations: dict[str, dict] = {}
        self.conversation_utterances: dict[str, list[dict]] = {}  # conv_uuid -> utterances
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
        if now - self.last_digest_time < self.DIGEST_INTERVAL_SEC:
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
        # macOS notification
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

        # Check if we should finalize a pending command capture
        self._check_command_capture()

        # Check if we should write a thought digest
        self._check_collation()

        # Check if we should generate a daily digest
        self._check_digest()

    # --- Core: Utterance handling with keyword detection ---

    def _on_utterance(self, data: dict):
        utterance = data.get("utterance", {})
        # "speaker" may be absent in single-person conversations
        speaker = utterance.get("speaker", "me")
        text = utterance.get("text", "")
        # conversation_uuid is at the top level of the event, not inside utterance
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

        # Track per-conversation utterances
        if conv_uuid:
            if conv_uuid not in self.conversation_utterances:
                self.conversation_utterances[conv_uuid] = []
            self.conversation_utterances[conv_uuid].append(entry)

        self._print(f"💬 [{speaker}] {text[:120]}")

        # --- KEYWORD DETECTION ---
        if self.command_capture is not None:
            # We're actively capturing a command — add this utterance
            self.command_capture.add_utterance(text)
            self._print(f"  🎤 [capturing command] {text[:80]}")
        else:
            # Check if this utterance contains the trigger keyword
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

        # Write raw command to inbox
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

            # Try direct channel execution for transports the monitor can handle.
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
                # Re-write with updated status
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
            # Any other action type the LLM inferred
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
        # Real field is "conversation_uuid", not "uuid"
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

            # Write full conversation to inbox for processing
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
        pass  # quiet

    # --- Journals (dictated notes — HIGH PRIORITY) ---

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
            # Write to original inbox
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
            # Write partial transcription too
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
            return  # not enough content to collate

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
# SSE Event Type Inference
# ---------------------------------------------------------------------------

def infer_event_type(data: dict) -> str:
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
        return "new-conversation"
    if "short_summary" in data and "conversation_id" in data:
        return "update-conversation-summary"
    if "todo" in data:
        return "todo-updated" if data["todo"].get("completed") else "todo-created"
    if "journal" in data:
        return "journal-updated" if data["journal"].get("state") == "READY" else "journal-created"
    if "location" in data:
        return "update-location"
    if "journalId" in data:
        return "journal-text" if "text" in data else "journal-deleted"
    return "unknown"


# ---------------------------------------------------------------------------
# Stream Mode
# ---------------------------------------------------------------------------

async def stream_mode(handler: EventHandler):
    """Connect to the Bee SSE event stream and process events in real-time."""
    args = [BEE_CLI, "stream", "--json"]

    handler._print("Starting Bee event stream...")
    handler._print(f"Trigger keywords: {DEFAULT_TRIGGERS}")
    handler._print(f"Inbox: {handler.inbox.inbox}")
    delay = 3
    proc = None

    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NO_COLOR": "1"},
            )
            delay = 3  # reset on successful connect

            async for line in proc.stdout:
                text = line.decode().strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                    event_type = infer_event_type(data)
                    handler.handle_event(event_type, data)
                except json.JSONDecodeError:
                    pass

            return_code = await proc.wait()
            handler._print(f"Stream ended (exit {return_code}). Reconnecting in {delay}s...")
            handler.save_state()
            await asyncio.sleep(delay)

        except asyncio.CancelledError:
            handler._print("Stream cancelled")
            if proc is not None and proc.returncode is None:
                proc.terminate()
            break
        except Exception as e:
            handler._print(f"Stream error: {e}. Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

        delay = min(delay * 2, 60)


# ---------------------------------------------------------------------------
# Poll Mode
# ---------------------------------------------------------------------------

async def poll_mode(handler: EventHandler, interval: int = 60):
    """Periodically poll for changes using `bee changed`."""
    handler._print(f"Starting poll mode (every {interval}s)")

    while True:
        try:
            cursor = load_cursor()
            args = ["changed", "--json"]
            if cursor:
                args.extend(["--cursor", cursor])

            data = run_bee_json(args)
            if data and isinstance(data, dict):
                meta = data.get("meta", {})
                next_cursor = meta.get("next_cursor")

                for conv in data.get("conversations", []):
                    handler._print(f"💬 Changed conversation {conv.get('id', '?')}")

                for journal in data.get("journals", []):
                    text = journal.get("text", "")
                    if text:
                        handler.inbox.write_journal(
                            str(journal.get("id", "unknown")), text, "READY"
                        )
                        handler._print(f"📓 Journal detected via poll: {text[:100]}")

                for todo in data.get("todos", []):
                    handler._print(f"📌 Todo change: {todo.get('text', '')[:100]}")

                if next_cursor:
                    save_cursor(next_cursor)

        except Exception as e:
            handler._print(f"Poll error: {e}")

        handler.save_state()
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Digest Mode
# ---------------------------------------------------------------------------

def digest_mode():
    """Generate a one-shot digest of current state."""
    print("=" * 60)
    print("  BEE WEARABLE — STATUS DIGEST")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    for label, cmd in [
        ("Status", ["status"]),
        ("Today's Brief", ["today"]),
        ("Recent Context", ["now"]),
        ("Todos", ["todos", "list"]),
        ("Facts", ["facts", "list", "--limit", "20"]),
        ("Journals", ["journals", "list", "--limit", "5"]),
    ]:
        print(f"\n--- {label} ---")
        print(run_bee(cmd))

    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bee Wearable Monitoring Agent for OpenCLAW",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  stream    Real-time SSE monitoring with keyword detection (default)
  poll      Periodic change-feed polling
  digest    One-shot status summary

The agent monitors your Bee wearable and writes structured files to the
inbox directory (~/.bee-agent/inbox/) for OpenCLAW to process:
  - commands/   Voice commands triggered by saying "OpenCLAW"
  - conversations/  Completed conversation transcripts
  - journals/   Dictated notes
  - thoughts/   Periodic thought digests for collation

Examples:
  %(prog)s                                # Start streaming
  %(prog)s --trigger "hey claude"         # Custom trigger word
  %(prog)s --inbox /path/to/inbox         # Custom inbox directory
  %(prog)s --mode poll --interval 120     # Poll every 2 minutes
  %(prog)s --mode digest                  # One-shot summary
        """,
    )
    parser.add_argument(
        "--mode", choices=["stream", "poll", "digest"],
        default="stream", help="Monitoring mode (default: stream)",
    )
    parser.add_argument(
        "--trigger", type=str, default="",
        help="Additional trigger keyword(s), comma-separated (added to defaults)",
    )
    parser.add_argument(
        "--inbox", type=str, default=str(DEFAULT_INBOX_DIR),
        help=f"Inbox directory for output files (default: {DEFAULT_INBOX_DIR})",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Poll interval in seconds (poll mode, default: 60)",
    )
    parser.add_argument(
        "--webhook-url", type=str, default="",
        help="URL to forward alerts via POST webhook",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress non-alert output",
    )

    args = parser.parse_args()

    if args.mode == "digest":
        digest_mode()
        return

    # Build trigger list
    triggers = list(DEFAULT_TRIGGERS)
    if args.trigger:
        triggers.extend([t.strip() for t in args.trigger.split(",") if t.strip()])

    inbox_dir = Path(args.inbox)
    inbox = InboxWriter(inbox_dir)
    handler = EventHandler(
        inbox=inbox,
        triggers=triggers,
        webhook_url=args.webhook_url or None,
        quiet=args.quiet,
    )

    # Graceful shutdown
    loop = asyncio.new_event_loop()

    def shutdown(sig):
        handler._print(f"Received {sig.name}, shutting down...")
        handler.save_state()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: shutdown(s))

    try:
        if args.mode == "stream":
            loop.run_until_complete(stream_mode(handler))
        elif args.mode == "poll":
            loop.run_until_complete(poll_mode(handler, args.interval))
    except asyncio.CancelledError:
        pass
    finally:
        handler.save_state()
        handler._print(f"Session stats: {json.dumps(handler.stats)}")
        loop.close()


if __name__ == "__main__":
    main()
