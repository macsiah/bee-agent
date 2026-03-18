#!/usr/bin/env python3
from __future__ import annotations
"""
Bee Voice Command -> Action Parser (LLM-powered)
====================================================
Parses natural-language voice commands captured from the Bee wearable into
structured, executable actions using Gemini Flash for intent inference.

Instead of regex pattern matching, this module sends the raw utterance to
Gemini Flash which infers the user's intent, extracts parameters, and
returns a structured ActionRequest. This means the user can say anything
in natural language and the system will figure out what they actually want.

Supported action types (non-exhaustive -- the LLM can infer new ones):
  - send_message: Send via iMessage, Telegram, WhatsApp, or generic
  - set_reminder: Create a reminder/todo with optional time
  - search: Search for information / answer a question
  - note: Save a note to a specific category
  - run_command: Execute an OpenCLAW command
  - query: Ask OpenCLAW a question or request information
  - control: System control (turn on/off, configure, etc.)
  - schedule: Schedule something for later
  - summarize: Summarize conversations, content, etc.
  - play_media: Play music, podcasts, etc.
  - (any other intent the LLM identifies)

Architecture:
  Bee voice -> bee_monitor (trigger detection) -> bee_actions (LLM intent parsing) -> inbox/actions/
  OpenCLAW scheduled task -> reads inbox/actions/ -> dispatches via channels

The parser uses Gemini Flash via the Google Generative AI API for fast,
low-latency intent inference. Falls back to a simple keyword heuristic
if the API is unavailable.
"""

import json
import os
import re
import subprocess
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    import urllib.request
    import urllib.error
    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Gemini Flash endpoint -- uses the v1beta generateContent API
GEMINI_MODEL = os.environ.get("BEE_INTENT_MODEL", "gemini-2.5-flash")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Where to find the API key (in order of priority):
#   1. BEE_GEMINI_API_KEY env var
#   2. GEMINI_API_KEY env var
#   3. GOOGLE_API_KEY env var
def _get_api_key() -> str:
    for var in ("BEE_GEMINI_API_KEY", "GEMINI_API_KEY", "GEMINI_API_TOKEN", "GOOGLE_API_KEY"):
        key = os.environ.get(var, "").strip()
        if key:
            return key
    return ""


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------

@dataclass
class ActionRequest:
    """A structured, executable action parsed from a voice command."""
    action_type: str      # send_message, set_reminder, search, note, run_command, query, control, schedule, summarize, play_media, unknown
    raw_command: str       # The original voice command text
    timestamp: str = ""
    status: str = "pending"  # pending, executed, failed

    # Messaging fields
    channel: str = ""       # imessage, telegram, whatsapp, sms, email
    recipient: str = ""     # Name or number/handle
    message_body: str = ""

    # Reminder fields
    reminder_text: str = ""
    reminder_time: str = ""  # Natural language time ("in 5 minutes", "at 3pm", "tomorrow")

    # Search / query fields
    search_query: str = ""

    # Note fields
    note_text: str = ""
    note_category: str = ""

    # Generic command fields
    openclaw_command: str = ""

    # LLM-inferred intent details (for open-ended commands)
    intent_summary: str = ""    # Human-readable summary of what the LLM thinks you want
    intent_params: dict = field(default_factory=dict)  # Any extra parameters the LLM extracted

    # Confidence
    confidence: float = 0.0
    parse_notes: str = ""


# ---------------------------------------------------------------------------
# Channel normalization
# ---------------------------------------------------------------------------

CHANNEL_ALIASES = {
    "imessage": "imessage", "i message": "imessage", "iphone": "imessage",
    "text": "imessage", "text message": "imessage", "sms": "imessage",
    "message": "imessage",
    "telegram": "telegram", "tg": "telegram",
    "whatsapp": "whatsapp", "whats app": "whatsapp", "wa": "whatsapp",
    "email": "email", "e-mail": "email", "mail": "email",
}

def normalize_channel(raw: str) -> str:
    """Normalize a channel name from LLM output."""
    key = raw.strip().lower()
    return CHANNEL_ALIASES.get(key, key)


# ---------------------------------------------------------------------------
# LLM Intent Parser -- the brain
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an intent parser for a voice command system called OpenCLAW.
The user speaks a command after saying the wake word "OpenClaw".
Your job is to understand what they want and return structured JSON.

Return ONLY valid JSON (no markdown, no explanation) with these fields:

{
  "action_type": "<one of: send_message, set_reminder, search, note, run_command, query, control, schedule, summarize, play_media, unknown>",
  "confidence": <0.0-1.0>,
  "intent_summary": "<1-sentence human-readable description of what the user wants>",
  "channel": "<messaging channel if applicable: imessage, telegram, whatsapp, email, or empty string>",
  "recipient": "<who to send to, if applicable, or empty string>",
  "message_body": "<message content if applicable, or empty string>",
  "reminder_text": "<what to remember, if applicable>",
  "reminder_time": "<when, in natural language, if applicable>",
  "search_query": "<what to search/ask about, if applicable>",
  "note_text": "<note content if applicable>",
  "note_category": "<category like work, personal, ideas, health, finance, or empty>",
  "openclaw_command": "<for run_command type: the command to execute>",
  "intent_params": {<any additional key-value parameters you extract>}
}

Guidelines:
- "send_message" = user wants to send a message to someone via some channel
- "set_reminder" = user wants to be reminded of something
- "search" = user wants to find information or look something up
- "note" = user wants to save/record a thought or note
- "query" = user is asking OpenCLAW a question or requesting information (not a web search)
- "control" = user wants to change a setting, turn something on/off, etc.
- "schedule" = user wants to schedule an event or task for a specific time
- "summarize" = user wants a summary of conversations, content, their day, etc.
- "play_media" = user wants to play music, a podcast, video, etc.
- "run_command" = user wants OpenCLAW to execute a specific system/tool command
- "unknown" = you genuinely cannot figure out what they want (use sparingly)

Be generous in your interpretation. The user is speaking naturally and may be vague.
If they say "tell mom I'll be late" -> send_message, channel=imessage (default), recipient=mom, message_body="I'll be late"
If they say "what's the weather" -> query, intent_summary="User wants to know the current weather"
If they say "remember to buy milk" -> set_reminder, reminder_text="buy milk"
If they say "check my calendar" -> query, intent_summary="User wants to see their calendar/schedule"

Always pick the most specific action_type that fits. Set confidence based on how clear the intent is.
"""


def _call_gemini(command_text: str, context: list[dict] | None = None) -> dict | None:
    """Call Gemini Flash API to parse intent. Returns parsed JSON dict or None."""
    api_key = _get_api_key()
    if not api_key:
        return None

    # Build the user prompt with optional context
    user_prompt = f'Parse this voice command:\n"{command_text}"'
    if context:
        recent = context[-5:]  # last 5 utterances for context
        ctx_lines = []
        for u in recent:
            speaker = u.get("speaker", "?")
            text = u.get("text", "")
            ctx_lines.append(f"  [{speaker}]: {text}")
        if ctx_lines:
            user_prompt += "\n\nRecent conversation context:\n" + "\n".join(ctx_lines)

    url = f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}]
            }
        ],
        "systemInstruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
        }
    }

    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    # Try httpx first (async-friendly), fall back to urllib
    try:
        if HAS_HTTPX:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, content=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        elif HAS_URLLIB:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        else:
            return None
    except Exception as e:
        print(f"[bee_actions] Gemini API error: {e}", flush=True)
        return None

    # Extract the text from Gemini's response
    try:
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return None
        text = parts[0].get("text", "")
        if not text:
            return None

        # Parse JSON -- handle markdown code fences if present despite our instructions
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)

        return json.loads(text)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"[bee_actions] Failed to parse Gemini response: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Fallback: Simple keyword heuristic (when API is unavailable)
# ---------------------------------------------------------------------------

def _fallback_parse(command_text: str) -> dict:
    """Basic keyword-based intent detection as fallback when Gemini is unavailable."""
    text = command_text.lower().strip()

    # Messaging keywords
    if any(kw in text for kw in ["send", "text", "message", "tell", "msg"]):
        channel = ""
        for ch_name in ["telegram", "whatsapp", "email", "imessage"]:
            if ch_name in text:
                channel = ch_name
                break
        return {
            "action_type": "send_message",
            "confidence": 0.5,
            "intent_summary": f"User wants to send a message (fallback parse)",
            "channel": channel or "imessage",
            "message_body": command_text,
        }

    # Reminder keywords
    if any(kw in text for kw in ["remind", "reminder", "don't forget", "remember to"]):
        return {
            "action_type": "set_reminder",
            "confidence": 0.5,
            "intent_summary": "User wants to set a reminder (fallback parse)",
            "reminder_text": command_text,
        }

    # Search keywords
    if any(kw in text for kw in ["search", "look up", "google", "find"]):
        return {
            "action_type": "search",
            "confidence": 0.5,
            "intent_summary": "User wants to search for something (fallback parse)",
            "search_query": command_text,
        }

    # Note keywords
    if any(kw in text for kw in ["note", "save", "write down", "jot"]):
        return {
            "action_type": "note",
            "confidence": 0.5,
            "intent_summary": "User wants to save a note (fallback parse)",
            "note_text": command_text,
        }

    # Default: treat as a query to OpenCLAW
    return {
        "action_type": "query",
        "confidence": 0.4,
        "intent_summary": f"User command (fallback parse, API unavailable): {command_text[:100]}",
        "openclaw_command": command_text,
    }


# ---------------------------------------------------------------------------
# Command Parser (LLM-powered)
# ---------------------------------------------------------------------------

class CommandParser:
    """Parses natural language voice commands into structured ActionRequests
    using Gemini Flash for intent inference.

    Falls back to simple keyword matching if the API is unavailable.
    """

    def parse(self, command_text: str, context: list[dict] | None = None) -> ActionRequest:
        """Parse a voice command into a structured action.

        Args:
            command_text: The raw voice command after the wake word.
            context: Optional list of recent utterances for conversational context.

        Returns:
            ActionRequest with inferred intent and extracted parameters.
        """
        text = command_text.strip()
        if not text:
            return ActionRequest(
                action_type="unknown",
                raw_command=command_text,
                timestamp=self._ts(),
                parse_notes="Empty command",
            )

        # Try LLM-based parsing first
        result = _call_gemini(text, context=context)

        if result is None:
            # Fallback to keyword heuristic
            result = _fallback_parse(text)
            result["parse_notes"] = result.get("parse_notes", "") + "Used fallback parser (Gemini unavailable)"

        # Build the ActionRequest from the LLM's response
        action = ActionRequest(
            action_type=result.get("action_type", "unknown"),
            raw_command=command_text,
            timestamp=self._ts(),
            confidence=float(result.get("confidence", 0.5)),
            intent_summary=result.get("intent_summary", ""),
            intent_params=result.get("intent_params", {}),
            parse_notes=result.get("parse_notes", "Parsed by Gemini Flash"),
        )

        # Populate specific fields based on action_type
        if action.action_type == "send_message":
            action.channel = normalize_channel(result.get("channel", "imessage"))
            action.recipient = result.get("recipient", "")
            action.message_body = result.get("message_body", "")

        elif action.action_type == "set_reminder":
            action.reminder_text = result.get("reminder_text", "")
            action.reminder_time = result.get("reminder_time", "")

        elif action.action_type in ("search", "query"):
            action.search_query = result.get("search_query", "") or result.get("intent_summary", "")

        elif action.action_type == "note":
            action.note_text = result.get("note_text", "")
            action.note_category = result.get("note_category", "")

        elif action.action_type == "run_command":
            action.openclaw_command = result.get("openclaw_command", text)

        else:
            # For any other action type (control, schedule, summarize, play_media, etc.)
            # store the command in openclaw_command for OpenCLAW to handle
            action.openclaw_command = result.get("openclaw_command", text)

        return action

    def _ts(self) -> str:
        return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Contact Resolver -- maps names to phone numbers / email addresses
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
                self._contacts = {k.lower().strip(): v for k, v in data.items()}
            except (json.JSONDecodeError, IOError):
                self._contacts = {}

    def resolve(self, name: str) -> tuple[str, bool]:
        """Resolve a name to a phone/email.

        Returns:
            (resolved_value, was_resolved) -- if not resolved, returns original name
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
        self.contacts_file.write_text(json.dumps(self._contacts, indent=2))


# ---------------------------------------------------------------------------
# Action Writer -- writes structured actions for OpenCLAW
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
# iMessage executor (via imsg CLI -- https://github.com/nicklama/imsg)
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
      imsg send <recipient> "message"   -- send a message
      imsg chats                         -- list recent chats
      imsg history <chat>                -- show messages for a chat
      imsg watch                         -- stream incoming messages
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
            return False, "imsg CLI not found -- install via: brew install imsg"

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
