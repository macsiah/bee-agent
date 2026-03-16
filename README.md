# Bee Agent

A hybrid MCP server and agent toolkit for the [Bee wearable AI](https://bee.computer) device. Gives Claude real-time access to your conversations, facts, todos, journals, and more — with a persistent SSE stream for instant context.

## What's inside

### `mcp_server/` — Hybrid MCP Server

The core of the project. A Python MCP server that:

- **Maintains a persistent SSE connection** to `bee stream` in the background
- **Caches live events** (utterances, conversation updates, todos, journals, location) in a rolling in-memory buffer
- **Serves instant responses** from cache for real-time tools (`bee_get_live_stream`, `bee_get_recent_utterances`)
- **Falls back to bee-cli** for full historical queries (conversations, search, daily summaries)
- **Auto-reconnects** with exponential backoff if the stream disconnects

**28 tools** including: live stream snapshot, recent utterances, conversation search (keyword + neural), facts CRUD, todos CRUD, journals, daily summaries, change feed, data export.

**6 resources**: `bee://status`, `bee://profile`, `bee://today`, `bee://now`, `bee://facts`, `bee://todos`

### `skill/` — Claude Code Skill

A `SKILL.md` file that teaches Claude how to use all `bee` CLI commands effectively. Drop it into your Claude Code skills directory for context-aware Bee integration.

### `standalone_agent/` — Monitoring Agent

A standalone Python agent for background monitoring with three modes:

- **stream** — Real-time SSE event monitoring with alerts
- **poll** — Periodic change-feed polling with cursor persistence
- **digest** — One-shot summary of current state

Supports webhook forwarding and JSONL event logging.

## Prerequisites

1. **Bee wearable device** with the iOS companion app
2. **Developer Mode** enabled (tap app version 5 times in Settings)
3. **bee-cli** installed and authenticated:
   ```bash
   npm install -g @beeai/cli
   bee login
   ```
4. **Python 3.10+**

## Quick Start

### MCP Server (Claude Desktop)

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "bee-wearable": {
      "command": "python3",
      "args": ["/path/to/bee-agent/mcp_server/bee_mcp_server.py"]
    }
  }
}
```

### MCP Server (Claude Code)

```bash
claude mcp add bee-wearable -- python3 /path/to/bee-agent/mcp_server/bee_mcp_server.py
```

### Claude Code Skill

```bash
mkdir -p ~/.claude/skills/bee-wearable
cp skill/SKILL.md ~/.claude/skills/bee-wearable/SKILL.md
```

### Standalone Monitor

```bash
# Real-time stream monitoring
python3 standalone_agent/bee_monitor.py

# Periodic polling (every 2 minutes)
python3 standalone_agent/bee_monitor.py --mode poll --interval 120

# One-shot digest
python3 standalone_agent/bee_monitor.py --mode digest

# Forward events to a webhook
python3 standalone_agent/bee_monitor.py --webhook-url https://hooks.example.com/bee
```

## How the hybrid architecture works

```
┌─────────────────────────────────────────────────┐
│              Claude / MCP Client                 │
│                                                  │
│   bee_get_live_stream ──► LiveStreamCache ◄──┐   │
│   bee_get_recent_utterances ──► (instant)    │   │
│                                              │   │
│   bee_get_conversation ──► bee-cli subprocess │   │
│   bee_search_conversations ──► (2-5 sec)     │   │
│                                              │   │
└──────────────────────────────────────────────┘   │
                                                   │
  Background task: bee stream --json ──────────────┘
  (persistent SSE connection, auto-reconnect)
```

The live stream tools (`bee_get_live_stream`, `bee_get_recent_utterances`, `bee_get_recent_events`) return from the in-memory cache in under 1ms. The on-demand tools shell out to bee-cli for full data, taking 2-5 seconds.

## Related Projects

- [bee-cli](https://github.com/bee-computer/bee-cli) — Official Bee CLI (TypeScript)
- [bee-skill](https://github.com/bee-computer/bee-skill) — Official Bee agent skill
- [beemcp](https://github.com/OkGoDoIt/beemcp) — Community MCP server (uses older API)

## License

MIT
