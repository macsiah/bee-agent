#!/usr/bin/env python3
"""
Bee Voice Command → Action Parser
====================================
Parses natural-language voice commands captured from the Bee wearable into
structured, executable actions for OpenCLAW or direct macOS execution.

Supported action types:
  - send_message: Send via iMessage, Telegram, WhatsApp, or generic
  - set_reminder: Create a reminder/todo with optional time
  - search: Search for information
  - note: Save a note to a specific category
  - run_command: Execute an OpenCLAW command

The parser produces a structured ActionRequest that gets written to
~/.bee-agent/inbox/actions/ for OpenCLAW to consume, or in the case of
iMessage, can be executed directly via the `imsg` CLI tool.

Architecture:
  Bee voice → bee_monitor (trigger detection) → bee_actions (parsing) → inbox/actions/
  OpenCLAW scheduled task → reads inbox/actions/ → dispatches via channels
"""

import json
import re
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------

@dataclass
class ActionRequest:
    """A structured, executable action parsed from a voice command."""
    action_type: str  # send_message, set_reminder, search, note, run_command
    raw_command: str  # The original voice command text
    timestamp: str = ""
    status: str = "pending"  # pending, executed, failed

    # Messaging fields
    channel: str = ""  # imessage, telegram, whatsapp, sms, email
    recipient: str = ""  # Name or number/handle
    message_body: str = ""

    # Reminder fields
    reminder_text: str = ""
    reminder_time: str = ""  # Natural language time ("in 5 minutes", "at 3pm", "tomorrow")

    # Search fields
    search_query: str = ""

    # Note fields
    note_text: str = ""
    note_category: str = ""

    # Generic command fields
    openclaw_command: str = ""

    # Confidence
    confidence: float = 0.0
    parse_notes: str = ""


# ---------------------------------------------------------------------------
# Channel aliases — what people actually say
# ---------------------------------------------------------------------------

CHANNEL_ALIASES = {
    # iMessage
    "imessage": "imessage",
    "i message": "imessage",
    "iphone": "imessage",
    "text": "imessage",
    "text message": "imessage",
    "sms": "imessage",
    "message": "imessage",  # default if no channel specified
    # Telegram
    "telegram": "telegram",
    "tg": "telegram",
    # WhatsApp
    "whatsapp": "whatsapp",
    "whats app": "whatsapp",
    "wa": "whatsapp",
    # Email
    "email": "email",
    "e-mail": "email",
    "mail": "email",
}

# ---------------------------------------------------------------------------
# Parser patterns
# ---------------------------------------------------------------------------

# Messaging patterns — ordered by specificity
MESSAGE_PATTERNS = [
    # "send a message via telegram to John saying hey what's up"
    re.compile(
        r"send\s+(?:a\s+)?(?:message|text|msg)\s+"
        r"(?:via|through|on|using|over)\s+(?P<channel>\w[\w\s]*?)\s+"
        r"to\s+(?P<recipient>.+?)\s+"
        r"(?:saying|that says|with|telling them|and say)\s+(?P<body>.+)",
        re.IGNORECASE,
    ),
    # "send John a message via telegram saying hey"
    re.compile(
        r"send\s+(?P<recipient>.+?)\s+"
        r"(?:a\s+)?(?:message|text|msg)\s+"
        r"(?:via|through|on|using|over)\s+(?P<channel>\w[\w\s]*?)\s+"
        r"(?:saying|that says|with)\s+(?P<body>.+)",
        re.IGNORECASE,
    ),
    # "message John on telegram saying hey"
    re.compile(
        r"(?:message|text|msg)\s+(?P<recipient>.+?)\s+"
        r"(?:via|through|on|using|over)\s+(?P<channel>\w[\w\s]*?)\s+"
        r"(?:saying|that says|with|and say)\s+(?P<body>.+)",
        re.IGNORECASE,
    ),
    # "text John saying hey what's up" (no channel — defaults to imessage)
    re.compile(
        r"(?:text|message|msg)\s+(?P<recipient>.+?)\s+"
        r"(?:saying|that says|with|and say|and tell them)\s+(?P<body>.+)",
        re.IGNORECASE,
    ),
    # "send a message to John via telegram" (body might be missing)
    re.compile(
        r"send\s+(?:a\s+)?(?:message|text|msg)\s+"
        r"to\s+(?P<recipient>.+?)\s+"
        r"(?:via|through|on|using|over)\s+(?P<channel>\w[\w\s]*?)$",
        re.IGNORECASE,
    ),
    # "send a telegram to John saying hey"
    re.compile(
        r"send\s+(?:a\s+)?(?P<channel>telegram|whatsapp|whats\s*app|text|email|imessage|i\s*message)\s+"
        r"to\s+(?P<recipient>.+?)\s+"
        r"(?:saying|that says|with)\s+(?P<body>.+)",
        re.IGNORECASE,
    ),
    # "tell John via telegram that the meeting is at 3"
    re.compile(
        r"tell\s+(?P<recipient>.+?)\s+"
        r"(?:via|through|on|using|over)\s+(?P<channel>\w[\w\s]*?)\s+"
        r"(?:that|to)\s+(?P<body>.+)",
        re.IGNORECASE,
    ),
    # "tell John the meeting is at 3" (no channel)
    re.compile(
        r"tell\s+(?P<recipient>.+?)\s+"
        r"(?:that|to)\s+(?P<body>.+)",
        re.IGNORECASE,
    ),
]

# Reminder patterns
REMINDER_PATTERNS = [
    # "remind me to call the doctor at 3pm"
    re.compile(
        r"remind\s+me\s+to\s+(?P<text>.+?)\s+"
        r"(?:at|in|on|by|tomorrow|tonight|next)\s+(?P<time>.+)",
        re.IGNORECASE,
    ),
    # "remind me to call the doctor" (no time)
    re.compile(
        r"remind\s+me\s+to\s+(?P<text>.+)",
        re.IGNORECASE,
    ),
    # "set a reminder for the meeting at 3pm"
    re.compile(
        r"set\s+(?:a\s+)?reminder\s+(?:for|about|to)\s+(?P<text>.+?)\s+"
        r"(?:at|in|on|by)\s+(?P<time>.+)",
        re.IGNORECASE,
    ),
    # "set a reminder for the meeting"
    re.compile(
        r"set\s+(?:a\s+)?reminder\s+(?:for|about|to)\s+(?P<text>.+)",
        re.IGNORECASE,
    ),
    # "don't forget to ..."
    re.compile(
        r"don'?t\s+forget\s+to\s+(?P<text>.+)",
        re.IGNORECASE,
    ),
]

# Search patterns
SEARCH_PATTERNS = [
    re.compile(r"search\s+(?:for\s+)?(?P<query>.+)", re.IGNORECASE),
    re.compile(r"look\s+up\s+(?P<query>.+)", re.IGNORECASE),
    re.compile(r"find\s+(?:out\s+)?(?:about\s+)?(?P<query>.+)", re.IGNORECASE),
    re.compile(r"google\s+(?P<query>.+)", re.IGNORECASE),
]

# Note patterns
NOTE_PATTERNS = [
    re.compile(
        r"(?:save|add|write)\s+(?:a\s+)?note\s+"
        r"(?:to|in|under)\s+(?P<category>\w+)\s*[:\-]?\s*(?P<text>.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:save|add|write)\s+(?:a\s+)?note\s*[:\-]?\s*(?P<text>.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"note\s+to\s+self\s*[:\-]?\s*(?P<text>.+)",
        re.IGNORECASE,
    ),
]


# ---------------------------------------------------------------------------
# Command Parser
# ---------------------------------------------------------------------------

class CommandParser:
    """Parses natural language voice commands into structured ActionRequests."""

    def parse(self, command_text: str) -> ActionRequest:
        """Parse a voice command into a structured action."""
        text = command_text.strip()
        if not text:
            return ActionRequest(
                action_type="unknown",
                raw_command=command_text,
                timestamp=self._ts(),
                parse_notes="Empty command",
            )

        # Try each action type in order of specificity
        action = self._try_message(text)
        if action and action.confidence > 0.3:
            return action

        action = self._try_reminder(text)
        if action and action.confidence > 0.3:
            return action

        action = self._try_search(text)
        if action and action.confidence > 0.3:
            return action

        action = self._try_note(text)
        if action and action.confidence > 0.3:
            return action

        # Fall back to generic OpenCLAW command
        return ActionRequest(
            action_type="run_command",
            raw_command=command_text,
            timestamp=self._ts(),
            openclaw_command=text,
            confidence=0.5,
            parse_notes="Could not match a specific action pattern; treating as generic command",
        )

    def _try_message(self, text: str) -> Optional[ActionRequest]:
        for pattern in MESSAGE_PATTERNS:
            m = pattern.match(text)
            if m:
                groups = m.groupdict()
                channel_raw = groups.get("channel", "").strip().lower()
                channel = CHANNEL_ALIASES.get(channel_raw, channel_raw)
                # If no channel detected, default based on the verb
                if not channel:
                    if "text" in text.lower()[:20]:
                        channel = "imessage"
                    else:
                        channel = "imessage"  # default

                recipient = groups.get("recipient", "").strip()
                body = groups.get("body", "").strip()

                # Clean up recipient (remove trailing prepositions/articles)
                recipient = re.sub(r'\s+(a|the|an|via|on|through)$', '', recipient, flags=re.IGNORECASE)

                confidence = 0.8
                if body:
                    confidence = 0.9
                if not recipient:
                    confidence = 0.4

                return ActionRequest(
                    action_type="send_message",
                    raw_command=text,
                    timestamp=self._ts(),
                    channel=channel,
                    recipient=recipient,
                    message_body=body,
                    confidence=confidence,
                    parse_notes=f"Matched message pattern; channel_raw='{channel_raw}'",
                )
        return None

    def _try_reminder(self, text: str) -> Optional[ActionRequest]:
        for pattern in REMINDER_PATTERNS:
            m = pattern.match(text)
            if m:
                groups = m.groupdict()
                return ActionRequest(
                    action_type="set_reminder",
                    raw_command=text,
                    timestamp=self._ts(),
                    reminder_text=groups.get("text", "").strip(),
                    reminder_time=groups.get("time", "").strip(),
                    confidence=0.85,
                    parse_notes="Matched reminder pattern",
                )
        return None

    def _try_search(self, text: str) -> Optional[ActionRequest]:
        for pattern in SEARCH_PATTERNS:
            m = pattern.match(text)
            if m:
                return ActionRequest(
                    action_type="search",
                    raw_command=text,
                    timestamp=self._ts(),
                    search_query=m.group("query").strip(),
                    confidence=0.8,
                    parse_notes="Matched search pattern",
                )
        return None

    def _try_note(self, text: str) -> Optional[ActionRequest]:
        for pattern in NOTE_PATTERNS:
            m = pattern.match(text)
            if m:
                groups = m.groupdict()
                return ActionRequest(
                    action_type="note",
                    raw_command=text,
                    timestamp=self._ts(),
                    note_text=groups.get("text", "").strip(),
                    note_category=groups.get("category", "").strip(),
                    confidence=0.8,
                    parse_notes="Matched note pattern",
                )
        return None

    def _ts(self) -> str:
        return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Contact Resolver — maps names to phone numbers / email addresses
# ---------------------------------------------------------------------------

CONTACTS_FILE = Path.home() / ".bee-agent" / "contacts.json"

class ContactResolver:
    """Resolves contact names to phone numbers or email addresses.

    Uses a JSON contacts file at ~/.bee-agent/contacts.json:
    {
        "mom": "+15551234567",
        "john": "+15559876543",
        "sarah": "sarah@example.com",
        "the team": "team-chat-id"
    }

    Names are matched case-insensitively. If no match is found,
    the original name is returned (OpenCLAW can resolve it later,
    or imsg may accept it if it matches a contact in Messages.app).
    """

    def __init__(self, contacts_file: Path = CONTACTS_FILE):
        self.contacts_file = contacts_file
        self._contacts: dict[str, str] = {}
        self._load()

    def _load(self):
        """Load contacts from JSON file."""
        if self.contacts_file.exists():
            try:
                data = json.loads(self.contacts_file.read_text())
                # Normalize keys to lowercase
                self._contacts = {k.lower().strip(): v for k, v in data.items()}
            except (json.JSONDecodeError, IOError):
                self._contacts = {}

    def resolve(self, name: str) -> tuple[str, bool]:
        """Resolve a name to a phone/email.

        Returns:
            (resolved_value, was_resolved) — if not resolved, returns original name
        """
        key = name.lower().strip()
        if key in self._contacts:
            return self._contacts[key], True

        # Try partial match (e.g., "john smith" matches "john")
        for contact_name, value in self._contacts.items():
            if contact_name in key or key in contact_name:
                return value, True

        return name, False

    def add(self, name: str, value: str):
        """Add or update a contact mapping."""
        self._contacts[name.lower().strip()] = value
        self._save()

    def _save(self):
        """Save contacts to JSON file."""
        self.contacts_file.parent.mkdir(parents=True, exist_ok=True)
        # Write with original casing preserved where possible
        self.contacts_file.write_text(json.dumps(self._contacts, indent=2))


# ---------------------------------------------------------------------------
# Action Writer — writes structured actions for OpenCLAW
# ---------------------------------------------------------------------------

class ActionWriter:
    """Writes parsed actions to the inbox for OpenCLAW to execute."""

    def __init__(self, inbox_dir: Path = Path.home() / ".bee-agent" / "inbox"):
        self.actions_dir = inbox_dir / "actions"
        self.actions_dir.mkdir(parents=True, exist_ok=True)

    def write(self, action: ActionRequest) -> Path:
        """Write an action to the actions inbox."""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"action-{action.action_type}-{ts}.json"
        path = self.actions_dir / filename
        path.write_text(json.dumps(asdict(action), indent=2))
        return path


# ---------------------------------------------------------------------------
# iMessage executor (via imsg CLI — https://github.com/nicklama/imsg)
# ---------------------------------------------------------------------------

IMSG_CLI = "/opt/homebrew/bin/imsg"

def _find_imsg() -> str:
    """Locate the imsg CLI binary."""
    for candidate in [IMSG_CLI, "imsg", "/usr/local/bin/imsg"]:
        try:
            result = subprocess.run(
                [candidate, "--help"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return ""


class IMessageSender:
    """Send iMessages via the imsg CLI tool.

    imsg is a terminal tool for iMessage/SMS:
      imsg send <recipient> "message"   — send a message
      imsg chats                         — list recent chats
      imsg history <chat>                — show messages for a chat
      imsg watch                         — stream incoming messages
    """

    def __init__(self):
        self._binary = _find_imsg()

    def is_available(self) -> bool:
        """Check if imsg CLI is installed."""
        return bool(self._binary)

    def send(self, recipient: str, message: str,
             service: str = "auto", attachment: str = "") -> tuple[bool, str]:
        """Send an iMessage via imsg CLI.

        Args:
            recipient: Phone number or email address
            message: Message body text
            service: "imessage", "sms", or "auto" (default)
            attachment: Optional file path to attach

        Returns:
            (success, detail_message)

        Usage:
            imsg send --to <phone/email> --text "message" [--service imessage|sms|auto] [--file path]
        """
        if not self._binary:
            return False, "imsg CLI not found — install via: brew install imsg"

        if not recipient:
            return False, "No recipient specified"
        if not message:
            return False, "No message body specified"

        cmd = [self._binary, "send", "--to", recipient, "--text", message]

        if service and service != "auto":
            cmd.extend(["--service", service])

        if attachment:
            cmd.extend(["--file", attachment])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return True, f"iMessage sent to {recipient}"
            else:
                err = result.stderr.strip() or result.stdout.strip()
                return False, f"imsg error: {err}"
        except subprocess.TimeoutExpired:
            return False, "imsg send timed out"
        except Exception as e:
            return False, f"Error: {e}"

    def list_chats(self, limit: int = 20) -> list[dict]:
        """List recent chats. Returns parsed output."""
        if not self._binary:
            return []
        try:
            result = subprocess.run(
                [self._binary, "chats"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                # Return raw output lines as dicts
                lines = result.stdout.strip().split("\n")
                return [{"raw": line} for line in lines[:limit] if line.strip()]
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------------
# Action Executor (for actions that can be run directly by the monitor)
# ---------------------------------------------------------------------------

class ActionExecutor:
    """Executes actions that can be handled locally (iMessage via imsg).
    Other channels (Telegram, WhatsApp) are left for OpenCLAW."""

    def __init__(self):
        self.imessage = IMessageSender()
        self.contacts = ContactResolver()

    def can_execute_locally(self, action: ActionRequest) -> bool:
        """Check if this action can be executed directly by the monitor."""
        if action.action_type == "send_message" and action.channel == "imessage":
            return self.imessage.is_available()
        return False

    def execute(self, action: ActionRequest) -> tuple[bool, str]:
        """Attempt to execute an action locally. Returns (success, detail)."""
        if action.action_type == "send_message" and action.channel == "imessage":
            # Resolve contact name to phone/email
            resolved, was_resolved = self.contacts.resolve(action.recipient)
            if was_resolved:
                detail_recipient = f"{action.recipient} ({resolved})"
            else:
                detail_recipient = resolved  # use name as-is, imsg may match it

            success, detail = self.imessage.send(resolved, action.message_body)
            if success:
                return True, f"iMessage sent to {detail_recipient}"
            return False, detail
        return False, f"Cannot execute {action.action_type}/{action.channel} locally"
