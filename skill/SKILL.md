---
name: bee-wearable
description: "Access real-time context and life data from the Bee wearable AI. Use this skill when: (1) you need to understand what's happening RIGHT NOW — recent conversations, current context, what was just discussed, (2) the user asks about something that recently happened or someone they just talked to, (3) you need life context — who the owner is, their relationships, work, preferences, (4) searching past conversations, managing facts/todos, or reviewing journals and daily summaries, (5) the user wants proactive awareness of their recent activity and commitments."
---

# Bee Wearable AI — Agent Skill

Comprehensive skill for accessing real-time and historical data from the Bee wearable AI device.

## About Bee

Bee is a wearable AI device that continuously captures and transcribes ambient audio from the owner's daily life — face-to-face conversations, meetings, phone calls, personal reflections, and overheard interactions. From these transcriptions, Bee automatically extracts facts, action items, and summaries.

**All Bee data is end-to-end encrypted and highly confidential.** Treat all information as private.

## Prerequisites

Check if `bee` CLI is installed and authenticated:
```bash
bee status
```

If not installed: `npm install -g @beeai/cli`
If not authenticated: `bee login` (follow the interactive flow)

## Core Workflow — Always Start Here

**Step 1: Get real-time context (ALWAYS do this first)**
```bash
bee now
```
This returns all conversations from the last 10 hours with **full utterance transcripts** (verbatim words, speaker-identified). This is the single most valuable piece of context.

**Step 2: Get facts about the owner**
```bash
bee facts list
```
Returns confirmed facts Bee has learned: preferences, relationships, work details, interests.

**Step 3: Check pending action items**
```bash
bee todos list
```

## Commands Reference

### Real-Time Context
```bash
bee now                          # Last 10 hours of conversations with utterances
bee now --json                   # Same, as structured JSON
bee today                        # Today's brief (calendar events, emails)
bee stream                       # Live SSE event stream (real-time)
bee stream --json                # Live events as JSON (for programmatic use)
bee stream --types new-utterance # Filter to specific event types
bee changed                      # Incremental changes since last check
bee changed --cursor <cursor>    # Changes since specific cursor position
```

### Conversations
```bash
bee conversations list                        # Browse summaries (paginated)
bee conversations list --limit 50             # More results
bee conversations list --cursor <cursor>      # Next page
bee conversations get <id>                    # FULL transcript with utterances
```

**Important:** Summaries from `list` are AI-generated and may contain inaccuracies. Always use `get` for accurate information.

### Search
```bash
bee search --query "project deadline"                   # Keyword search
bee search --query "restaurant recommendation" --neural # Semantic search
bee search --query "budget" --since 1710000000000       # Time-bounded
```

Neural search understands meaning and context — use it for questions like "what did we talk about regarding the launch?"

### Facts (About the Owner)
```bash
bee facts list                                    # Confirmed facts
bee facts list --unconfirmed                      # Pending/inferred facts
bee facts get <id>                                # Fact details
bee facts create --text "Prefers morning meetings"  # Create fact
bee facts update <id> --text "Updated text"       # Update fact
bee facts update <id> --text "X" --confirmed true # Confirm a fact
bee facts delete <id>                             # Delete fact
```

**Confirmed vs Unconfirmed:** Confirmed facts are verified. Unconfirmed facts are inferred and may be inaccurate — always caveat them.

### Todos
```bash
bee todos list                                          # All todos
bee todos get <id>                                      # Todo details
bee todos create --text "Follow up with Sarah"          # Create
bee todos create --text "Call dentist" --alarm-at <ISO> # With reminder
bee todos update <id> --completed true                  # Mark done
bee todos update <id> --text "Updated text"             # Edit
bee todos update <id> --clear-alarm                     # Remove alarm
bee todos delete <id>                                   # Delete
```

### Journals (Voice Memos)
```bash
bee journals list           # List journal entries
bee journals get <id>       # Full journal text
```

Journals are intentional recordings (unlike ambient conversations). States: PREPARING → ANALYZING → READY.

### Daily Summaries
```bash
bee daily                   # Recent daily summaries
bee daily --date 2026-03-15 # Specific date
```

### Data Export
```bash
bee sync                                     # Export everything to ./bee-sync/
bee sync --output /path/to/dir               # Custom output directory
bee sync --only facts,todos                  # Specific data types
```

### Change Feed (Incremental Updates)
```bash
bee changed                      # Recent changes
bee changed --cursor <cursor>    # Changes since cursor
bee changed --json               # As JSON with next_cursor
```

**Cursor workflow:** Call without cursor → process changes → save next_cursor → call with cursor next time. Only save cursor AFTER processing succeeds.

## SSE Stream Event Types

When using `bee stream`, these events are available:
- `new-utterance` — Someone spoke (includes speaker, text, conversation_uuid)
- `new-conversation` — New conversation started
- `update-conversation` — Conversation updated (title, summary)
- `update-conversation-summary` — Summary regenerated
- `delete-conversation` — Conversation removed
- `update-location` — Location changed
- `todo-created` / `todo-updated` / `todo-deleted` — Todo changes
- `journal-created` / `journal-updated` / `journal-deleted` — Journal changes
- `journal-text` — Journal transcription text

## Deep Learning Workflow

For comprehensive user profiling, use a multi-agent chain:

1. Initialize `user.md` profile document
2. Spawn subagent to process conversations in batches of 100
3. Each batch: read conversations → extract insights → update user.md → pass cursor to next agent
4. Chain agents until all history is processed
5. Use `bee changed` for subsequent incremental updates

See the bee-skill repository for detailed instructions on this workflow.

## Best Practices

1. **Always start with `bee now`** — real-time context makes assistance immediately relevant
2. **Use `get` over `list`** when accuracy matters — summaries can hallucinate
3. **Neural search for questions** — keyword search for specific terms
4. **Persist cursors** for `bee changed` to avoid reprocessing
5. **Treat all data as confidential** — this is the owner's private life
6. **Prefer confirmed facts** over unconfirmed ones
7. **Create facts proactively** when you learn something important about the owner
