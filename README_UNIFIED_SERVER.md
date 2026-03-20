# Unified Bee Wearable FastMCP Server

## Overview

The `bee_server.py` file consolidates the standalone Bee monitoring agent (`bee_monitor.py`) and the Bee MCP server (`bee_mcp_server.py`) into a single, production-grade FastMCP server.

**What this means:**
- One unified process replaces two separate daemons
- No more separate monitor and MCP server to manage
- Background event monitoring happens automatically in the MCP server's lifespan
- All 51+ MCP tools are preserved
- All monitor capabilities (keyword detection, action execution, classification) are preserved

## File Location

```
/sessions/wonderful-beautiful-euler/mnt/.openclaw/bee-agent/bee_server.py
```

**Size:** 85KB, 2622 lines (production-ready, complete file)

## Architecture

### Unified Server Design

```
┌─────────────────────────────────────────────────────────┐
│                  FastMCP Server                         │
├─────────────────────────────────────────────────────────┤
│  Lifespan Manager                                       │
│  ├─ Creates LiveStreamCache (in-memory event buffer)   │
│  ├─ Creates EventHandler (monitor logic)                │
│  ├─ Starts background SSE stream task                   │
│  └─ Manages graceful shutdown                           │
├─────────────────────────────────────────────────────────┤
│  Background SSE Stream Task                             │
│  ├─ Connects to: bee stream --json                      │
│  ├─ Ingests events into cache                           │
│  ├─ Passes events to handler for processing             │
│  └─ Auto-reconnects with exponential backoff            │
├─────────────────────────────────────────────────────────┤
│  Event Processing Pipeline                              │
│  ├─ LiveStreamCache (caches last 500 events)            │
│  ├─ EventHandler                                        │
│  │  ├─ Trigger detection (keyword activation)           │
│  │  ├─ Command capture state machine                    │
│  │  ├─ Classification & routing (BeeClassifier)         │
│  │  ├─ Action execution (notes, reminders, messages)    │
│  │  ├─ Conversation tracking                            │
│  │  └─ Ambient intelligence (periodic digests)          │
│  └─ Inbox Writer (writes structured files)              │
├─────────────────────────────────────────────────────────┤
│  55 MCP Tools                                           │
│  ├─ Live Stream (3)       - Real-time cache queries     │
│  ├─ Monitor Control (4)   - Monitor status & control    │
│  ├─ Context (2)           - Bee-cli based queries       │
│  ├─ Conversations (3)     - List, get, search           │
│  ├─ Facts (6)             - CRUD + citations            │
│  ├─ Todos (7)             - Full todo management        │
│  ├─ Journals (3)          - Journal queries             │
│  ├─ Sync (2)              - Changes & markdown export   │
│  ├─ Profile (2)           - User & status               │
│  ├─ Speakers (7)          - Speaker profiles            │
│  ├─ Inference (6)         - LLM-powered insights        │
│  ├─ Integrations (4)      - Calendar & mail setup       │
│  ├─ Calendar (2)          - Events queries              │
│  ├─ Mail (2)              - Recent & search             │
│  ├─ Config (4)            - Configuration management    │
│  └─ UI (1)                - Dashboard launcher          │
└─────────────────────────────────────────────────────────┘
```

## Key Components

### 1. LiveStreamCache (Enhanced)

Maintains a rolling buffer of recent events from `bee stream --json`:
- Caches up to 500 events
- Caches up to 200 utterances
- Tracks active conversations, todos, journals, location
- **NEW:** Directly calls EventHandler for event processing
- Provides instant access to real-time state

### 2. EventHandler (Integrated)

Full monitoring logic integrated into the server:
- **Trigger Detection:** Detects activation keywords (default: "OpenCLAW")
- **Command Capture:** State machine captures voice commands post-activation
- **Classification:** Uses BeeClassifier to route content to knowledge base
- **Action Execution:** Uses ActionExecutor to handle notes, reminders, messages
- **Conversation Tracking:** Tracks active conversations and utterances
- **Ambient Intelligence:** Periodically generates thought digests
- **Macros Notifications:** Alerts via osascript
- **Event Logging:** Writes events to JSONL log for recovery

### 3. Lifespan Manager

Handles server startup and shutdown:
- Creates LiveStreamCache and EventHandler
- Starts background SSE stream task
- Passes cache and handler to all MCP tools
- Gracefully cancels stream task on shutdown
- Saves state for recovery

### 4. Background Stream Task

Connects and maintains the SSE event stream:
- Executes: `bee stream --json`
- Continuously ingests events
- Auto-reconnects with exponential backoff (3s → 60s)
- Handles disconnections gracefully
- Updates cache.connected status

## Usage

### Direct Execution

```bash
cd /path/to/bee-agent
python bee_server.py
```

The server will:
1. Start the MCP server (ready to accept tool calls)
2. Launch the background SSE stream
3. Begin processing events automatically
4. Stay running until interrupted (Ctrl+C)

### Via Claude Desktop Config

Add to `~/.claude/config.json`:

```json
{
  "mcpServers": {
    "bee_unified": {
      "command": "python",
      "args": ["/path/to/bee-agent/bee_server.py"]
    }
  }
}
```

Then restart Claude and the server will auto-connect.

### Via MCP CLI

```bash
mcp run python /path/to/bee-agent/bee_server.py
```

## MCP Tools (55 Total)

### Live Stream Tools (Instant, from cache)
- `bee_get_live_stream` - Full snapshot of current state
- `bee_get_recent_utterances` - Last N utterances
- `bee_get_recent_events` - Recent events (optionally filtered)

### Monitor Control Tools (NEW)
- `bee_monitor_status` - Current monitor stats and state
- `bee_monitor_set_trigger` - Change trigger keyword
- `bee_monitor_get_inbox` - List recent inbox items
- `bee_monitor_force_digest` - Generate thought digest now

### Context Tools (Bee-cli based)
- `bee_get_current_context` - Full 10-hour context
- `bee_get_today_brief` - Calendar, emails, schedule

### Conversation Tools
- `bee_list_conversations` - List with pagination
- `bee_get_conversation` - Full conversation with transcript
- `bee_search_conversations` - Keyword or semantic search

### Facts Tools
- `bee_list_facts` - List (confirmed or unconfirmed)
- `bee_get_fact` - Single fact details
- `bee_create_fact` - Create new fact
- `bee_update_fact` - Update text/confirmation
- `bee_delete_fact` - Delete fact
- `bee_cite_fact` - Source conversations

### Todos Tools
- `bee_list_todos` - All todos
- `bee_get_todo` - Single todo
- `bee_create_todo` - Create todo
- `bee_update_todo` - Update todo
- `bee_complete_todo` - Mark complete
- `bee_delete_todo` - Delete todo

### Journals Tools
- `bee_list_journals` - List journals
- `bee_get_journal` - Single journal
- `bee_get_daily_summary` - Daily summary

### And 20+ more tools for speakers, inference, integrations, calendar, mail, config, etc.

## MCP Resources (6 Total)

- `bee://status` - Auth & connection status
- `bee://profile` - User profile
- `bee://today` - Today's brief
- `bee://now` - Last 10 hours context
- `bee://facts` - All facts
- `bee://todos` - All todos

## Configuration

The server uses environment variables and config files:

- **Inbox Directory:** `~/.bee-agent/inbox/` (for writing commands, conversations, journals, thoughts, actions)
- **Event Log:** `~/.bee-agent/event-log.jsonl` (for event history)
- **State File:** `~/.bee-agent/state.json` (for recovery)
- **Cursor File:** `~/.bee-agent/cursor` (for polling mode)
- **Trigger Keywords:** Default ["openclaw", "open claw", "hey openclaw", "hey open claw"] (changeable via tool)

## Integration with Bee Modules

The unified server imports from the standalone_agent directory:

```python
sys.path.insert(0, str(Path(__file__).parent / "standalone_agent"))

from bee_classifier import BeeClassifier, ContentRouter
from bee_actions import CommandParser, ActionWriter, ActionExecutor
```

This allows the EventHandler to:
1. Classify events (BeeClassifier)
2. Route to knowledge base (ContentRouter)
3. Parse voice commands (CommandParser)
4. Execute actions (ActionExecutor)
5. Write structured actions (ActionWriter)

## Startup Process

1. **Server starts:** FastMCP initializes
2. **Lifespan enters:** EventHandler and cache created
3. **Stream task launches:** Background task connects to bee stream
4. **Events begin flowing:** Cache ingests, handler processes
5. **Tools available:** All 55 tools ready for queries
6. **Monitor active:** Keyword detection, command capture, etc. running

## Event Processing Flow

```
bee stream --json output
    ↓
Background stream task (asyncio)
    ↓
JSON parsing
    ↓
LiveStreamCache.ingest(raw_json)
    ├─ Classify event type
    ├─ Update specialized caches (utterances, conversations, todos, etc.)
    └─ Call handler.handle_event(event_type, data)
        ├─ Route to appropriate handler (e.g., _on_utterance)
        ├─ Check for trigger keyword
        ├─ Start/continue command capture if needed
        ├─ Classify & route content
        ├─ Execute actions
        ├─ Track conversations
        └─ Check for collation/digest time
    ↓
Inbox files written (~/.bee-agent/inbox/)
Knowledge base updated
OpenCLAW memory appended
macOS notifications sent
Event log written
```

## Production Considerations

### Monitoring
- Check `~/.bee-agent/event-log.jsonl` for event history
- Check `~/.bee-agent/state.json` for current state
- Use `bee_monitor_status` tool to check handler stats

### Logging
All events are logged to JSONL format:
```json
{"timestamp": "2026-03-19T...", "event": "new-utterance", "data": {...}}
```

### Keyboard Interrupt
Graceful shutdown on Ctrl+C:
1. Stream task is cancelled
2. EventHandler saves state
3. All tasks cleaned up
4. Server exits cleanly

### State Recovery
On restart, EventHandler loads:
- Previous state from `~/.bee-agent/state.json`
- Continues from where it left off

## Differences from Original Setup

| Aspect | Original | Unified |
|--------|----------|---------|
| Processes | 2 (monitor + MCP) | 1 (unified) |
| Configuration | CLI args for monitor | Tools for monitor control |
| Startup | Manual shell script | Automatic in MCP lifespan |
| State sync | File-based between processes | Integrated in single process |
| Event routing | Monitor → files → OpenCLAW | Monitor → cache → tools + files |
| Tools | 51+ MCP tools | 55 MCP tools (added 4 monitor control) |
| Resources | 6 resources | 6 resources (unchanged) |

## Testing

Syntax validation:
```bash
python -m py_compile bee_server.py
```

Import validation:
```bash
python -c "import sys; sys.path.insert(0, './standalone_agent'); from bee_classifier import BeeClassifier; from bee_actions import CommandParser"
```

## Dependencies

- `python >= 3.10`
- `mcp >= 1.4.0`
- `pydantic`
- bee-cli (npm package)
- Google Generative AI (for Gemini Flash intent parsing)
- Optional: httpx or urllib (for API calls)

## File Completeness

✓ Complete 2622-line production-ready file
✓ All imports included and working
✓ All classes fully implemented
✓ All 55 tools with full docstrings
✓ All 6 resources implemented
✓ Full EventHandler logic integrated
✓ Full LiveStreamCache logic integrated
✓ Complete Pydantic models for input validation
✓ Graceful error handling
✓ Exponential backoff for reconnection
✓ Lifespan management
✓ Event logging
✓ State persistence

## Future Enhancements

Possible additions (not in current scope):
- Persistent event storage (database instead of JSONL)
- Web dashboard integration
- Advanced analytics on event patterns
- Custom plugin system for action handlers
- Distributed cache for multiple devices
