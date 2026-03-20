# Bee Agent for OpenCLAW

A real-time intelligence system for the [Bee wearable AI](https://bee.computer) device. Monitors your Bee's event stream, captures conversations and notes, classifies content into categories, executes voice commands (send messages, set reminders), and feeds everything into OpenCLAW's memory for long-term recall.

Built for [OpenCLAW](https://github.com/openclaw) but also works standalone with Claude Desktop or Claude Code.

## What It Does

You wear your Bee throughout the day. When you push the button, it records a conversation. When you hold the button, it captures a dictated note. This agent sits in the background, listens to the Bee's real-time event stream, and does five things with that data:

1. **Captures and stores** every conversation transcript, journal note, and todo
2. **Classifies** each piece of content into categories (work, personal, health, projects, etc.)
3. **Routes** classified content to three places simultaneously: categorized JSON inbox, markdown knowledge base, and OpenCLAW's daily memory files
4. **Executes voice commands** when you say "OpenCLAW" — send iMessages, Telegram messages, set reminders, take notes
5. **Generates daily digests** with consolidated action items across all categories

## Project Structure

```
bee-agent/
├── mcp_server/
│   ├── bee_mcp_server.py          # Hybrid MCP server (26 tools, 6 resources)
│   └── pyproject.toml             # Python package config
│
├── standalone_agent/
│   ├── bee_monitor.py             # Always-on background monitor (main entry point)
│   ├── bee_classifier.py          # Content classification and routing engine
│   ├── bee_actions.py             # Voice command parser and executor
│   └── com.openclaw.bee-monitor.plist  # macOS launchd service definition
│
├── skill/
│   └── SKILL.md                   # Claude Code / OpenCLAW skill file
│
├── install-openclaw.sh            # One-command OpenCLAW installer
├── setup.sh                       # Manual setup script
├── claude_desktop_config.example.json
├── LICENSE                        # MIT
└── README.md
```

## How Each Component Works

### `bee_monitor.py` — The Background Monitor

This is the heart of the system. It runs as a persistent background process (via macOS launchd) and connects to `bee stream --json`, which is the Bee's real-time Server-Sent Events (SSE) stream.

**Startup flow:**
1. Locates the `bee` CLI binary (checks `~/.bun/bin/bee`, `/usr/local/bin/bee`, `/opt/homebrew/bin/bee`)
2. Spawns `bee stream --json` as a subprocess
3. Parses the SSE output line-by-line as JSON events
4. Routes each event through the `EventHandler` class

**Event processing pipeline:**

```
bee stream --json
    │
    ▼
JSON line received
    │
    ▼
infer_event_type(data)          # Check data["type"] field first,
    │                           # fall back to payload structure inference
    ▼
EventHandler.handle_event()
    │
    ├── log_event()             # Append to ~/.bee-agent/event-log.jsonl
    │
    ├── handler_map[type]()     # Dispatch to specific handler:
    │   ├── _on_connected       # Stream connected notification
    │   ├── _on_new_conversation    # Track conversation start
    │   ├── _on_utterance       # Cache utterance, check for keyword trigger
    │   ├── _on_update_conversation # Detect conversation end, write transcript
    │   ├── _on_journal_created # Notification: recording started
    │   ├── _on_journal_text    # Write partial text, check for keyword trigger
    │   ├── _on_journal_updated # Write final text, classify & route
    │   ├── _on_todo_created    # Notification
    │   ├── _on_location_update # Cache location
    │   └── ... (13 event types total + connected)
    │
    ├── _check_command_capture()    # Finalize any active voice command
    ├── _check_collation()          # Generate thought digest every 5 min
    └── _check_digest()             # Generate daily digest every hour
```

**Key classes in `bee_monitor.py`:**

| Class | Purpose |
|-------|---------|
| `TriggerDetector` | Regex-based keyword matching. Looks for "openclaw", "open claw", "hey openclaw", "hey open claw" in transcribed text. Case-insensitive with word boundaries. |
| `CommandCapture` | State machine that activates after trigger detection. Captures utterances for 15 seconds, with a 5-second silence timeout. All captured text becomes a single command string. |
| `InboxWriter` | Writes JSON files to `~/.bee-agent/inbox/{commands,conversations,journals,thoughts}/`. Each file is a self-contained event payload with timestamp and metadata. |
| `EventHandler` | The main orchestrator. Maintains in-memory state: active conversations, per-conversation utterance lists (`deque(maxlen=200)`), recent todos, and a rolling utterance buffer. |

**Utterance handling detail:**

When a `new-utterance` event arrives, the monitor:
1. Extracts `utterance.text`, `utterance.speaker` (defaults to `"me"` if absent), and `utterance.spoken_at`
2. Appends to the rolling `recent_utterances` deque (max 200 entries)
3. Appends to `conversation_utterances[conv_uuid]` for per-conversation tracking
4. Checks if a `CommandCapture` is active — if so, adds the text to it
5. If no capture is active, runs `TriggerDetector.detect(text)` to check for the keyword

**Conversation lifecycle:**
1. `new-conversation` → Creates entry in `active_conversations` dict, initializes empty utterance list
2. `new-utterance` → Accumulates in `conversation_utterances[conv_uuid]`
3. `update-conversation` with state `completed`/`ended`/`finalized` → Writes full transcript to `inbox/conversations/`, classifies combined text, routes to all destinations

### `bee_classifier.py` — Content Classification

A hybrid classification system that uses predefined categories with keyword/pattern matching for speed, plus structured metadata extraction for nuance.

**Classification flow:**

```
Input text
    │
    ▼
Score each category (0.0 → 1.0)
    ├── Keyword matching: +0.1 per keyword found in text
    ├── Pattern matching: +0.15 per regex pattern match
    └── Score capped at 1.0
    │
    ▼
Rank categories by score
    ├── Primary: highest score
    └── Secondary: next 3 with score > 0.1
    │
    ▼
Extract metadata
    ├── Action items (regex: "I need to...", "remind me to...", "don't forget to...")
    ├── Priority (urgent/important/normal keywords)
    ├── Sentiment (positive/negative/neutral word counts)
    └── Topics (which keywords matched)
    │
    ▼
ClassificationResult
```

**The 8 categories:**

| Category | Trigger keywords (examples) | What goes here |
|----------|---------------------------|----------------|
| `work` | meeting, deadline, sprint, client, PowerSchool, district | Meetings, work tasks, professional conversations |
| `personal` | family, friend, grocery, dog, dinner, pick up | Errands, family, social plans |
| `health` | doctor, appointment, exercise, gym, sleep, therapy | Medical, fitness, wellbeing |
| `projects` | build, code, github, deploy, app, MCP, bee agent | Active coding, creative builds |
| `ideas` | idea, what if, brainstorm, imagine, I wonder | Raw brainstorms, inspiration |
| `learning` | learn, research, article, tutorial, harmonica | Study, exploration, curiosity |
| `finance` | money, budget, payment, invest, retirement, Tesla | Purchases, budgets, investments |
| `meta` | bee, openclaw, agent, config, setup | System config, self-referential |

Each category has both keyword lists and compiled regex patterns. Keywords are matched as substrings (case-insensitive). Patterns use word boundaries for precision.

**Action item extraction patterns:**

The classifier pulls action items from natural language using these regex patterns:
- `"I need to / should / must / have to / gotta / gonna / will / want to [ACTION]"`
- `"remind me to / don't forget to / remember to [ACTION]"`
- `"todo / to do / action item / task: [ACTION]"`
- `"let's / let me [ACTION]"`

**Priority detection:**
- **High**: urgent, asap, immediately, emergency, critical, right now
- **Medium**: important, priority, must, crucial, don't forget, need to

**Triple-destination routing (ContentRouter):**

Every classified item gets written to three places:

1. **Categorized inbox** (`~/.bee-agent/inbox/{category}/`) — Full JSON payload with classification metadata. Machine-readable. OpenCLAW agents can scan specific category folders.

2. **Markdown knowledge base** (`~/.bee-agent/knowledge/{category}.md`) — Append-only markdown file per category. Human-readable. Builds up over time as a personal wiki. Format:
   ```markdown
   ### 2026-03-16 14:30 — journal
   **Priority:** high

   Reminder to call Dr. Smith about the knee pain appointment next Tuesday.

   - [ ] call Dr. Smith about the knee pain appointment next Tuesday

   *Topics: doctor, appointment, pain*
   ---
   ```

3. **OpenCLAW daily memory** (`~/.openclaw/workspace/memory/YYYY-MM-DD.md`) — Appended to OpenCLAW's existing daily memory files. These get indexed by OpenCLAW's embedding system (Gemini embeddings) and become searchable via semantic memory search. Format:
   ```markdown
   ### 📓 [Health] 14:30
   *Source: journal | Priority: high*

   Reminder to call Dr. Smith about the knee pain appointment next Tuesday.

   - [ ] call Dr. Smith about the knee pain appointment next Tuesday

   *Topics: doctor, appointment, pain*
   ```

### `bee_actions.py` — Voice Command Parser & Executor

When the trigger keyword ("OpenCLAW") is detected and a command is captured, `bee_actions.py` parses the raw text into a structured `ActionRequest` and optionally executes it.

**Parsing pipeline:**

```
Raw command text (e.g., "send a message via telegram to John saying the build is green")
    │
    ▼
CommandParser.parse()
    │
    ├── _try_message()      # 8 regex patterns for messaging commands
    ├── _try_reminder()     # 5 patterns for reminders
    ├── _try_search()       # 4 patterns for search queries
    ├── _try_note()         # 3 patterns for notes
    └── fallback → run_command (generic OpenCLAW command)
    │
    ▼
ActionRequest {
    action_type: "send_message",
    channel: "telegram",
    recipient: "John",
    message_body: "the build is green",
    confidence: 0.9,
    ...
}
```

**Supported action types:**

| Action | Trigger phrases | Fields extracted |
|--------|----------------|-----------------|
| `send_message` | "send a message via...", "text [name] saying...", "tell [name] via...", "message [name] on..." | channel, recipient, message_body |
| `set_reminder` | "remind me to...", "set a reminder for...", "don't forget to..." | reminder_text, reminder_time |
| `search` | "search for...", "look up...", "find out about...", "google..." | search_query |
| `note` | "save a note...", "add a note...", "note to self..." | note_text, note_category |
| `run_command` | (anything that doesn't match above) | openclaw_command |

**Message channel resolution:**

When you say a channel name, it gets normalized through `CHANNEL_ALIASES`:

| You say | Resolved channel |
|---------|-----------------|
| "text", "iMessage", "SMS", "message" | `imessage` |
| "telegram", "TG" | `telegram` |
| "WhatsApp", "WA" | `whatsapp` |
| "email", "mail" | `email` |

If no channel is specified, defaults to `imessage`.

**Contact resolution:**

The `ContactResolver` maps spoken names to phone numbers or email addresses using `~/.bee-agent/contacts.json`:

```json
{
  "mom": "+15551234567",
  "john": "+15559876543",
  "sarah": "sarah@example.com",
  "the team": "team-group-id"
}
```

Matching is case-insensitive with partial matching (e.g., "john smith" matches the "john" entry). If no match is found, the raw name is passed through — `imsg` may resolve it from your macOS Contacts.

**Execution flow for iMessage:**

```
ActionRequest (channel=imessage, recipient="Mom", body="I'll be home for dinner")
    │
    ▼
ContactResolver.resolve("Mom")  →  ("+15551234567", True)
    │
    ▼
IMessageSender.send("+15551234567", "I'll be home for dinner")
    │
    ▼
subprocess: imsg send --to +15551234567 --text "I'll be home for dinner"
    │
    ▼
macOS notification: "Message Sent: iMessage to Mom (+15551234567)"
```

`imsg` is a macOS CLI tool installed at `/opt/homebrew/bin/imsg` that interfaces with Messages.app. It supports `--to` (phone/email), `--text` (message body), `--service` (imessage/sms/auto), and `--file` (attachments).

**Execution flow for Telegram / WhatsApp:**

These channels can't be executed directly by the monitor. Instead, the parsed action is written as a JSON file to `~/.bee-agent/inbox/actions/`:

```json
{
  "action_type": "send_message",
  "raw_command": "send a message via telegram to John saying the build is green",
  "channel": "telegram",
  "recipient": "John",
  "message_body": "the build is green",
  "confidence": 0.9,
  "status": "pending",
  "timestamp": "2026-03-16T14:30:00+00:00"
}
```

OpenCLAW reads this file and dispatches the message through its configured Telegram bot (configured in `openclaw.json` under `channels.telegram`).

### `bee_mcp_server.py` — Hybrid MCP Server

A Model Context Protocol server that gives Claude (via Claude Desktop or Claude Code) direct access to Bee data through 26 tools and 6 resources.

**Hybrid architecture:**

The server maintains two data pathways:

1. **Live cache (instant, <1ms)** — A background `asyncio` task runs `bee stream --json` and feeds events into a `LiveStreamCache`:
   - `events: deque(maxlen=500)` — rolling buffer of all events
   - `utterances: deque(maxlen=200)` — recent utterance text with speaker/timestamp
   - `active_conversations: dict` — currently open conversations
   - `recent_todos: list` — recently created/modified todos
   - `recent_journals: list` — recently created journals
   - `last_location: dict` — most recent GPS update

2. **On-demand CLI (2-5 seconds)** — For historical queries, the server shells out to `bee-cli` via `asyncio.create_subprocess_exec`:
   ```python
   proc = await asyncio.create_subprocess_exec(
       BEE_CLI, *args, "--json",
       stdout=asyncio.subprocess.PIPE,
       stderr=asyncio.subprocess.PIPE,
   )
   stdout, stderr = await proc.communicate()
   return json.loads(stdout)
   ```

**Server lifecycle (FastMCP lifespan pattern):**

```python
@asynccontextmanager
async def server_lifespan(server):
    cache = LiveStreamCache()
    task = asyncio.create_task(cache.start_stream())
    try:
        yield {"cache": cache}
    finally:
        task.cancel()
```

The cache starts with the server and stops when the server shuts down. MCP tool handlers access it via the lifespan context.

**All 26 tools:**

| Tool | Source | Description |
|------|--------|-------------|
| `bee_get_live_stream` | Cache | Snapshot of recent events from the live SSE stream |
| `bee_get_recent_utterances` | Cache | Last N utterances with speaker and timestamp |
| `bee_get_recent_events` | Cache | Last N events of any type |
| `bee_get_current_context` | CLI | `bee now` — all conversations from last 10 hours |
| `bee_get_today_brief` | CLI | `bee today` — today's calendar, emails, summary |
| `bee_list_conversations` | CLI | List conversations with date/limit filters |
| `bee_get_conversation` | CLI | Get a single conversation by ID with full transcript |
| `bee_search_conversations` | CLI | Keyword and neural search across conversations |
| `bee_list_facts` | CLI | List all confirmed facts about the owner |
| `bee_get_fact` | CLI | Get a specific fact by ID |
| `bee_create_fact` | CLI | Create a new fact |
| `bee_update_fact` | CLI | Update an existing fact |
| `bee_delete_fact` | CLI | Delete a fact |
| `bee_list_todos` | CLI | List all todos |
| `bee_get_todo` | CLI | Get a specific todo by ID |
| `bee_create_todo` | CLI | Create a new todo |
| `bee_update_todo` | CLI | Update a todo |
| `bee_complete_todo` | CLI | Mark a todo as completed |
| `bee_delete_todo` | CLI | Delete a todo |
| `bee_list_journals` | CLI | List journal entries |
| `bee_get_journal` | CLI | Get a specific journal entry |
| `bee_get_daily_summary` | CLI | Get summary for a specific date |
| `bee_get_changes` | CLI | Change feed with cursor pagination |
| `bee_get_profile` | CLI | Owner's profile information |
| `bee_get_status` | CLI | Authentication and connection status |
| `bee_sync_to_markdown` | CLI | Export Bee data as markdown files |

**6 resources:**

| URI | Description |
|-----|-------------|
| `bee://status` | Authentication status |
| `bee://profile` | User profile |
| `bee://today` | Today's brief |
| `bee://now` | Current context (last 10 hours) |
| `bee://facts` | All facts |
| `bee://todos` | All todos |

### `skill/SKILL.md` — Claude Skill

A markdown file with YAML frontmatter that teaches Claude how to use `bee-cli` commands. When installed as an OpenCLAW skill (`~/.openclaw/skills/bee-wearable/SKILL.md`), OpenCLAW loads it into agent context automatically.

The skill covers:
- Core workflow (always start with `bee now`, then `bee facts list`, then `bee todos`)
- All `bee-cli` subcommands with usage examples
- The 14 SSE event types and their JSON payloads
- Tips for conversation processing (multi-speaker detection, summary patterns)

### `install-openclaw.sh` — Installer

A self-contained bash script that installs everything for any OpenCLAW instance. Can be run from a fresh clone or piped from curl.

**What it does (7 steps):**

1. **Prerequisites check** — Verifies OpenCLAW directory exists, Python 3.10+, bee-cli installed and authenticated
2. **Code install** — Clones the repo to `~/.openclaw/bee-agent/` (or updates if already present)
3. **Python dependencies** — Installs `mcp[cli]>=1.4.0` and `pydantic>=2.0.0`
4. **Directory structure** — Creates all inbox, knowledge, digest, and category directories; seeds `contacts.json`; checks for `imsg` CLI
5. **Skill registration** — Copies `SKILL.md` to `~/.openclaw/skills/bee-wearable/`
6. **launchd service** — Generates a macOS LaunchAgent plist with correct paths, Python binary, and `PYTHONPATH` set so `bee_classifier` and `bee_actions` can be imported
7. **Summary** — Prints next steps and directory layout

### `com.openclaw.bee-monitor.plist` — macOS Service

A launchd property list that keeps `bee_monitor.py` running at all times:

- `KeepAlive: true` — Restarts if it crashes
- `RunAtLoad: true` — Starts on login
- `ThrottleInterval: 10` — Waits 10 seconds before restarting after crash
- Logs to `/tmp/bee-monitor.log` and `/tmp/bee-monitor.err`
- Sets `PYTHONPATH` to the `standalone_agent/` directory so imports work

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Bee Wearable Device                          │
│                                                                     │
│   [Push button]              [Hold button]           [Ambient]      │
│   → Conversation mode        → Journal/dictation     → Location,    │
│   → Transcribes speech       → Transcribes note        todos, etc.  │
│   → Identifies speakers      → AI cleanup                          │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
                  │  SSE stream via bee-cli (bee stream --json)
                  │  14 event types, JSON payloads
                  │
                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     bee_monitor.py (always-on)                       │
│                                                                     │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐     │
│  │ EventHandler  │  │ TriggerDetect │  │ CommandCapture       │     │
│  │              │  │               │  │ (15s window,         │     │
│  │ Routes each  │──│ Scans text    │──│  5s silence timeout) │     │
│  │ event type   │  │ for "OpenCLAW"│  │                      │     │
│  └──────┬───────┘  └───────────────┘  └──────────┬───────────┘     │
│         │                                         │                 │
│         │  Every finalized journal                 │  Captured       │
│         │  and conversation                        │  command text   │
│         ▼                                         ▼                 │
│  ┌──────────────┐                         ┌──────────────┐         │
│  │ bee_classifier│                         │ bee_actions   │         │
│  │              │                         │              │         │
│  │ Classify into│                         │ Parse into   │         │
│  │ 8 categories │                         │ ActionRequest│         │
│  │ + extract    │                         │              │         │
│  │ action items │                         │ Resolve      │         │
│  │ + priority   │                         │ contacts     │         │
│  └──────┬───────┘                         └──────┬───────┘         │
│         │                                         │                 │
│         ▼                                         ▼                 │
│  ┌──────────────────────────────┐  ┌──────────────────────────┐    │
│  │ ContentRouter (triple write) │  │ ActionExecutor           │    │
│  │                              │  │                          │    │
│  │ 1. inbox/{category}/ (JSON)  │  │ iMessage → imsg CLI      │    │
│  │ 2. knowledge/{cat}.md        │  │ Telegram → inbox/actions/ │    │
│  │ 3. openclaw/memory/date.md   │  │ WhatsApp → inbox/actions/ │    │
│  └──────────────────────────────┘  └──────────────────────────┘    │
│                                                                     │
│  ┌─────────────────────┐                                           │
│  │ Periodic tasks       │                                           │
│  │ • Thought digest (5m)│                                           │
│  │ • Daily digest (1h)  │                                           │
│  └─────────────────────┘                                           │
└─────────────────────────────────────────────────────────────────────┘
                  │                              │
                  ▼                              ▼
┌────────────────────────┐        ┌──────────────────────────────────┐
│ ~/.bee-agent/           │        │ ~/.openclaw/                      │
│                        │        │                                  │
│ inbox/                 │        │ workspace/memory/                │
│   commands/            │        │   2026-03-16.md  ← Bee entries   │
│   actions/  ──────────────────► │                    appended here │
│   conversations/       │        │ skills/bee-wearable/             │
│   journals/            │        │   SKILL.md                       │
│   thoughts/            │        │                                  │
│   work/                │        │ Agents read inbox/actions/ and   │
│   personal/            │        │ dispatch via Telegram/WhatsApp   │
│   health/              │        │ channels configured in           │
│   projects/            │        │ openclaw.json                    │
│   ideas/               │        └──────────────────────────────────┘
│   learning/            │
│   finance/             │
│   meta/                │
│ knowledge/             │
│   work.md              │
│   personal.md          │
│   health.md            │
│   ...                  │
│ digests/               │
│   digest-20260316.md   │
│ contacts.json          │
│ event-log.jsonl        │
└────────────────────────┘
```

## Supported Bee Event Types

The Bee developer API sends 14 event types via its SSE stream at `/v1/stream`:

| Event | JSON payload structure | Monitor handler |
|-------|----------------------|-----------------|
| `connected` | `{"timestamp": 1773681366424}` (no `type` field) | `_on_connected` |
| `new-conversation` | `{"type": "new-conversation", "conversation": {"id": 123, "conversation_uuid": "abc...", "state": "IN_PROGRESS"}}` | `_on_new_conversation` |
| `new-utterance` | `{"type": "new-utterance", "conversation_uuid": "abc...", "utterance": {"text": "...", "speaker": "me", "spoken_at": "..."}}` | `_on_utterance` |
| `update-conversation` | `{"type": "update-conversation", "conversation": {"id": 123, "state": "completed", "title": "...", "short_summary": "..."}}` | `_on_update_conversation` |
| `update-conversation-summary` | `{"type": "update-conversation-summary", "conversation_id": 123, "short_summary": "..."}` | `_on_update_summary` |
| `delete-conversation` | `{"type": "delete-conversation", "conversation": {"id": 123}}` | `_on_delete_conversation` |
| `journal-created` | `{"type": "journal-created", "journal": {"id": 456, "state": "PREPARING"}}` | `_on_journal_created` |
| `journal-text` | `{"type": "journal-text", "journalId": 456, "text": "partial transcription..."}` | `_on_journal_text` |
| `journal-updated` | `{"type": "journal-updated", "journal": {"id": 456, "state": "READY", "text": "final text..."}}` | `_on_journal_updated` |
| `journal-deleted` | `{"type": "journal-deleted", "journalId": 456}` | — |
| `todo-created` | `{"type": "todo-created", "todo": {"id": 789, "text": "...", "completed": false}}` | `_on_todo_created` |
| `todo-updated` | `{"type": "todo-updated", "todo": {"id": 789, "completed": true}}` | `_on_todo_updated` |
| `todo-deleted` | `{"type": "todo-deleted", "todo": {"id": 789}}` | `_on_todo_deleted` |
| `update-location` | `{"type": "update-location", "location": {"latitude": 46.6, "longitude": -120.5, "name": "..."}}` | `_on_location_update` |

**Important field notes discovered from live stream testing:**
- The `type` field is present in the JSON payload itself (not just the SSE `event:` line)
- The `connected` event has no `type` field — it's just `{"timestamp": <epoch_ms>}`
- Conversations use `conversation_uuid` (not `uuid`)
- The `speaker` field may be absent in single-person conversations (defaults to `"me"`)
- Utterances include a `spoken_at` timestamp separate from the event timestamp

## Configuration

### Contacts (`~/.bee-agent/contacts.json`)

Maps spoken names to phone numbers or email addresses for iMessage sending:

```json
{
  "mom": "+15091234567",
  "dad": "+15091234567",
  "john": "john@example.com",
  "the team": "+15091234568"
}
```

### Trigger Keywords

Default triggers (configurable via `--trigger` flag):
- "openclaw"
- "open claw"
- "hey openclaw"
- "hey open claw"

### Custom Categories

The `BeeClassifier` accepts a `custom_categories` dict to add or override categories:

```python
classifier = BeeClassifier(custom_categories={
    "music": {
        "keywords": ["harmonica", "blues", "guitar", "practice", "song"],
        "patterns": [r"\b(harmonica|blues|guitar|music)\b"],
        "description": "Music practice and learning",
    }
})
```

## Installation

### One-Command OpenCLAW Install (Recommended)

```bash
git clone https://github.com/macsiah/bee-agent.git
cd bee-agent
bash install-openclaw.sh
```

### Manual Setup

**1. Install dependencies:**
```bash
pip3 install "mcp[cli]>=1.4.0" "pydantic>=2.0.0"
```

**2. Create directories:**
```bash
mkdir -p ~/.bee-agent/inbox/{commands,actions,conversations,journals,thoughts}
mkdir -p ~/.bee-agent/inbox/{work,personal,health,projects,ideas,learning,finance,meta}
mkdir -p ~/.bee-agent/{knowledge,digests}
```

**3. Set up contacts:**
```bash
echo '{"mom": "+15551234567"}' > ~/.bee-agent/contacts.json
```

**4. Start the monitor:**
```bash
cd ~/.openclaw/bee-agent/standalone_agent
python3 bee_monitor.py
```

**5. Install as persistent service (macOS):**
```bash
launchctl load ~/Library/LaunchAgents/com.openclaw.bee-monitor.plist
```

**6. Add MCP server to Claude Desktop** (optional):

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "bee-wearable": {
      "command": "python3",
      "args": ["/Users/YOU/.openclaw/bee-agent/mcp_server/bee_mcp_server.py"]
    }
  }
}
```

## Voice Command Examples

During a conversation (button push) or journal dictation (button hold), say "OpenCLAW" followed by:

| You say | Parsed action | Execution |
|---------|--------------|-----------|
| "send a message via Telegram to John saying the build is green" | `send_message` → telegram → John | Written to `inbox/actions/` for OpenCLAW |
| "text Mom saying I'll be home for dinner" | `send_message` → imessage → Mom | `imsg send --to +1555... --text "..."` |
| "tell Sarah via WhatsApp that the meeting is at 3" | `send_message` → whatsapp → Sarah | Written to `inbox/actions/` for OpenCLAW |
| "remind me to call the doctor at 3pm" | `set_reminder` → "call the doctor" @ 3pm | Written to `inbox/actions/` |
| "search for Python async best practices" | `search` → "Python async best practices" | Written to `inbox/actions/` |
| "note to self buy a new monitor" | `note` → "buy a new monitor" | Written to `inbox/actions/` |
| "don't forget to pick up groceries" | `set_reminder` → "pick up groceries" | Written to `inbox/actions/` |

## Troubleshooting

**Monitor won't connect:**
```bash
bee status          # Check authentication
bee stream --json   # Test stream directly (Ctrl+C to stop)
```

**No events appearing:**
- The Bee only sends events when you interact with it (push/hold button)
- There's no ambient audio transcription — the device handles that locally
- Check the log: `tail -f /tmp/bee-monitor.log`

**imsg not sending:**
```bash
which imsg                    # Should be /opt/homebrew/bin/imsg
imsg send --help              # Check available flags
imsg chats                    # List recent conversations
```

**Keyword trigger not firing:**
- Triggers only work during active conversation or journal dictation
- The Bee must be transcribing (button pressed) for text to appear in the stream
- Check that your trigger words are being spoken clearly enough for transcription

**Check service status:**
```bash
launchctl list | grep bee-monitor
tail -f /tmp/bee-monitor.log
tail -f /tmp/bee-monitor.err
```

## Related Projects

- [bee-cli](https://github.com/bee-computer/bee-cli) — Official Bee CLI (TypeScript)
- [bee-skill](https://github.com/bee-computer/bee-skill) — Official Bee agent skill
- [77degrees/bee-skill](https://github.com/77degrees/bee-skill) — Extended fork with UI docs (our SKILL.md incorporates content from here, MIT licensed)
- [beemcp](https://github.com/OkGoDoIt/beemcp) — Community MCP server (uses older API)
- [imsg](https://github.com/nicklama/imsg) — Terminal iMessage/SMS client

## License

MIT — Copyright 2026 OpenCLAW
