#!/usr/bin/env python3
"""
Bee Wearable Standalone Monitoring Agent
========================================
A standalone agent that monitors the Bee wearable's real-time event stream
and provides proactive alerts, summaries, and insights.

Features:
  - Real-time SSE event monitoring via `bee stream`
  - Automatic conversation summaries when conversations end
  - Todo tracking with alarm notifications
  - Periodic context digests
  - Change-feed polling with cursor persistence
  - Webhook support for forwarding events to external services

Usage:
    python bee_monitor.py                          # Start monitoring
    python bee_monitor.py --mode stream            # SSE stream mode
    python bee_monitor.py --mode poll --interval 60  # Polling mode (every 60s)
    python bee_monitor.py --mode digest            # One-shot digest
    python bee_monitor.py --webhook-url https://...  # Forward events via webhook

Requirements:
    - bee-cli installed and authenticated
    - Python 3.10+
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CURSOR_FILE = Path.home() / ".bee-agent-cursor"
LOG_FILE = Path.home() / ".bee-agent-log.jsonl"
USER_PROFILE_FILE = Path.home() / ".bee-user-profile.md"


def find_bee_cli() -> str:
    """Locate the bee CLI binary."""
    for candidate in ["bee", os.path.expanduser("~/.bun/bin/bee"), "/usr/local/bin/bee"]:
        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "bee"


BEE_CLI = find_bee_cli()


# ---------------------------------------------------------------------------
# Bee CLI Wrapper
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


# ---------------------------------------------------------------------------
# Cursor Persistence
# ---------------------------------------------------------------------------

def load_cursor() -> Optional[str]:
    """Load the last saved cursor from disk."""
    if CURSOR_FILE.exists():
        return CURSOR_FILE.read_text().strip() or None
    return None


def save_cursor(cursor: str):
    """Save cursor to disk after successful processing."""
    CURSOR_FILE.write_text(cursor)


# ---------------------------------------------------------------------------
# Event Logging
# ---------------------------------------------------------------------------

def log_event(event_type: str, data: dict):
    """Append an event to the JSONL log file."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "data": data,
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Event Handlers
# ---------------------------------------------------------------------------

class EventHandler:
    """Processes Bee events and generates alerts/summaries."""

    def __init__(self, webhook_url: Optional[str] = None, quiet: bool = False):
        self.webhook_url = webhook_url
        self.quiet = quiet
        self.active_conversations: dict[str, dict] = {}
        self.recent_utterances: list[dict] = []
        self.stats = {
            "utterances": 0,
            "conversations_started": 0,
            "conversations_ended": 0,
            "todos_created": 0,
            "journals_created": 0,
        }

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
        elif not self.quiet:
            self._print(f"[{event_type}] {json.dumps(data)[:200]}")

    def _print(self, msg: str):
        """Print a formatted message with timestamp."""
        if not self.quiet:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] {msg}")

    def _alert(self, title: str, body: str):
        """Send a high-priority alert."""
        self._print(f"🔔 {title}: {body}")
        if self.webhook_url:
            self._send_webhook({"alert": title, "body": body})

    def _send_webhook(self, payload: dict):
        """Send event data to a webhook endpoint."""
        if not self.webhook_url:
            return
        try:
            import urllib.request
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[WEBHOOK ERROR] {e}", file=sys.stderr)

    # --- Event Handlers ---

    def _on_connected(self, data: dict):
        self._print("Connected to Bee event stream")

    def _on_utterance(self, data: dict):
        utterance = data.get("utterance", {})
        speaker = utterance.get("speaker", "unknown")
        text = utterance.get("text", "")
        conv_uuid = data.get("conversation_uuid", "")

        self.stats["utterances"] += 1
        self.recent_utterances.append({
            "speaker": speaker,
            "text": text,
            "conversation_uuid": conv_uuid,
            "time": datetime.now(timezone.utc).isoformat(),
        })
        # Keep only last 100 utterances in memory
        if len(self.recent_utterances) > 100:
            self.recent_utterances = self.recent_utterances[-100:]

        self._print(f"💬 [{speaker}] {text[:120]}")

    def _on_new_conversation(self, data: dict):
        conv = data.get("conversation", {})
        conv_id = conv.get("id", "?")
        self.stats["conversations_started"] += 1
        self.active_conversations[str(conv_id)] = {
            "id": conv_id,
            "start_time": datetime.now(timezone.utc).isoformat(),
            "state": conv.get("state", "unknown"),
        }
        self._print(f"🟢 New conversation started (id={conv_id})")

    def _on_update_conversation(self, data: dict):
        conv = data.get("conversation", {})
        conv_id = str(conv.get("id", "?"))
        state = conv.get("state", "unknown")
        title = conv.get("title", "")
        summary = conv.get("short_summary", "")

        if state in ("completed", "ended", "finalized"):
            self.stats["conversations_ended"] += 1
            self.active_conversations.pop(conv_id, None)
            self._alert(
                "Conversation ended",
                f"ID {conv_id}: {title or summary or '(no summary yet)'}",
            )
        else:
            if conv_id in self.active_conversations:
                self.active_conversations[conv_id]["state"] = state
            if title:
                self._print(f"📝 Conversation {conv_id} updated: {title[:100]}")

    def _on_update_summary(self, data: dict):
        conv_id = data.get("conversation_id", "?")
        summary = data.get("short_summary", "")
        if summary:
            self._print(f"📋 Summary for conversation {conv_id}: {summary[:150]}")

    def _on_delete_conversation(self, data: dict):
        conv = data.get("conversation", {})
        conv_id = str(conv.get("id", "?"))
        self.active_conversations.pop(conv_id, None)
        self._print(f"🗑️ Conversation {conv_id} deleted")

    def _on_todo_created(self, data: dict):
        todo = data.get("todo", {})
        text = todo.get("text", "")
        self.stats["todos_created"] += 1
        self._alert("New todo", text[:200])

    def _on_todo_updated(self, data: dict):
        todo = data.get("todo", {})
        text = todo.get("text", "")
        completed = todo.get("completed", False)
        if completed:
            self._print(f"✅ Todo completed: {text[:100]}")
        else:
            self._print(f"📌 Todo updated: {text[:100]}")

    def _on_todo_deleted(self, data: dict):
        todo = data.get("todo", {})
        self._print(f"🗑️ Todo {todo.get('id', '?')} deleted")

    def _on_journal_created(self, data: dict):
        journal = data.get("journal", {})
        state = journal.get("state", "unknown")
        self.stats["journals_created"] += 1
        self._print(f"📓 New journal entry (state={state})")

    def _on_journal_updated(self, data: dict):
        journal = data.get("journal", {})
        state = journal.get("state", "unknown")
        text = journal.get("text", "")
        if state == "READY" and text:
            self._alert("Journal ready", text[:200])
        else:
            self._print(f"📓 Journal updated (state={state})")

    def _on_journal_text(self, data: dict):
        text = data.get("text", "")
        self._print(f"📓 Journal text: {text[:150]}")

    def _on_location_update(self, data: dict):
        loc = data.get("location", {})
        name = loc.get("name", "")
        lat = loc.get("latitude", "?")
        lng = loc.get("longitude", "?")
        if name:
            self._print(f"📍 Location: {name}")
        else:
            self._print(f"📍 Location: {lat}, {lng}")

    def get_status(self) -> dict:
        """Return current monitoring status."""
        return {
            "active_conversations": len(self.active_conversations),
            "recent_utterances": len(self.recent_utterances),
            "stats": self.stats,
        }


# ---------------------------------------------------------------------------
# Stream Mode: Connect to bee stream and process SSE events
# ---------------------------------------------------------------------------

async def stream_mode(handler: EventHandler, event_types: Optional[list[str]] = None):
    """Connect to the Bee SSE event stream and process events in real-time."""
    args = [BEE_CLI, "stream", "--json"]
    if event_types:
        args.extend(["--types", ",".join(event_types)])

    handler._print("Starting Bee event stream...")
    handler._print(f"Command: {' '.join(args)}")

    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NO_COLOR": "1"},
            )

            async for line in proc.stdout:
                text = line.decode().strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                    # The stream --json output is raw event data
                    # We need to determine the event type from the data
                    event_type = _infer_event_type(data)
                    handler.handle_event(event_type, data)
                except json.JSONDecodeError:
                    # Not JSON — might be a status message
                    if not handler.quiet:
                        handler._print(f"[raw] {text[:200]}")

            # Process exited
            return_code = await proc.wait()
            handler._print(f"Stream ended (exit code {return_code}). Reconnecting in 5s...")
            await asyncio.sleep(5)

        except asyncio.CancelledError:
            handler._print("Stream cancelled")
            if proc and proc.returncode is None:
                proc.terminate()
            break
        except Exception as e:
            handler._print(f"Stream error: {e}. Reconnecting in 10s...")
            await asyncio.sleep(10)


def _infer_event_type(data: dict) -> str:
    """Infer the SSE event type from the JSON data structure."""
    if "utterance" in data:
        return "new-utterance"
    if "conversation" in data:
        conv = data["conversation"]
        state = conv.get("state", "")
        if state in ("completed", "ended"):
            return "update-conversation"
        return "new-conversation"
    if "todo" in data:
        return "todo-created"
    if "journal" in data:
        return "journal-created"
    if "location" in data:
        return "update-location"
    if "short_summary" in data and "conversation_id" in data:
        return "update-conversation-summary"
    if "journalId" in data:
        if "text" in data:
            return "journal-text"
        return "journal-deleted"
    return "unknown"


# ---------------------------------------------------------------------------
# Poll Mode: Periodically check for changes
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

                # Process each category of changes
                for fact in data.get("facts", []):
                    handler._alert("Fact changed", fact.get("text", "")[:200])

                for todo in data.get("todos", []):
                    text = todo.get("text", "")
                    completed = todo.get("completed", False)
                    if completed:
                        handler._print(f"✅ Todo completed: {text[:100]}")
                    else:
                        handler._alert("Todo update", text[:200])

                for conv in data.get("conversations", []):
                    summary = conv.get("summary", "")
                    handler._print(f"💬 Conversation {conv.get('id', '?')}: {(summary or '(no summary)')[:150]}")

                for journal in data.get("journals", []):
                    text = journal.get("text", "")
                    handler._print(f"📓 Journal: {(text or '(processing)')[:150]}")

                for daily in data.get("dailies", []):
                    summary = daily.get("summary", "")
                    handler._print(f"📅 Daily: {(summary or '(no summary)')[:150]}")

                # Save cursor only after successful processing
                if next_cursor:
                    save_cursor(next_cursor)

        except Exception as e:
            handler._print(f"Poll error: {e}")

        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Digest Mode: One-shot summary
# ---------------------------------------------------------------------------

def digest_mode(handler: EventHandler):
    """Generate a one-shot digest of current state."""
    print("=" * 60)
    print("  BEE WEARABLE — CURRENT STATUS DIGEST")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Status
    print("\n--- Authentication ---")
    print(run_bee(["status"]))

    # Today's brief
    print("\n--- Today's Brief ---")
    print(run_bee(["today"]))

    # Recent conversations
    print("\n--- Recent Conversations (last 10 hours) ---")
    print(run_bee(["now"]))

    # Open todos
    print("\n--- Todos ---")
    print(run_bee(["todos", "list"]))

    # Recent facts
    print("\n--- Recent Facts ---")
    print(run_bee(["facts", "list", "--limit", "20"]))

    # Recent journals
    print("\n--- Recent Journals ---")
    print(run_bee(["journals", "list", "--limit", "5"]))

    print("\n" + "=" * 60)
    print("  DIGEST COMPLETE")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bee Wearable Monitoring Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  stream    Connect to the real-time SSE event stream (default)
  poll      Periodically check for changes using `bee changed`
  digest    Generate a one-shot summary of current state

Examples:
  %(prog)s                                    # Start streaming
  %(prog)s --mode stream --types new-utterance,todo-created
  %(prog)s --mode poll --interval 120         # Poll every 2 minutes
  %(prog)s --mode digest                      # One-shot summary
  %(prog)s --webhook-url https://hooks.example.com/bee
        """,
    )
    parser.add_argument(
        "--mode", choices=["stream", "poll", "digest"],
        default="stream", help="Monitoring mode (default: stream)",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Poll interval in seconds (poll mode only, default: 60)",
    )
    parser.add_argument(
        "--types", type=str, default="",
        help="Comma-separated event types to filter (stream mode only)",
    )
    parser.add_argument(
        "--webhook-url", type=str, default="",
        help="URL to forward events to via POST webhook",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress non-alert output",
    )

    args = parser.parse_args()
    handler = EventHandler(
        webhook_url=args.webhook_url or None,
        quiet=args.quiet,
    )

    if args.mode == "digest":
        digest_mode(handler)
        return

    # Set up graceful shutdown
    loop = asyncio.new_event_loop()

    def shutdown(sig):
        handler._print(f"Received {sig.name}, shutting down...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: shutdown(s))

    try:
        if args.mode == "stream":
            event_types = [t.strip() for t in args.types.split(",") if t.strip()] or None
            loop.run_until_complete(stream_mode(handler, event_types))
        elif args.mode == "poll":
            loop.run_until_complete(poll_mode(handler, args.interval))
    except asyncio.CancelledError:
        pass
    finally:
        handler._print("Agent stopped")
        status = handler.get_status()
        handler._print(f"Session stats: {json.dumps(status['stats'])}")
        loop.close()


if __name__ == "__main__":
    main()
