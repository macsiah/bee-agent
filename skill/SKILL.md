---
name: bee-wearable
description: "Access real-time context and life data from the Bee wearable AI. ALWAYS start with 'bee now' to get the last 10 hours of conversations with full utterances — this is the most valuable context for relevant assistance. Use this skill when: (1) you need to understand what's happening RIGHT NOW — recent conversations, current context, what was just discussed, (2) the user asks about something that recently happened or someone they just talked to, (3) you need life context — who the owner is, their relationships, work, preferences, (4) searching past conversations, managing facts/todos, or reviewing journals and daily summaries, (5) identifying speakers in conversations or managing speaker profiles, (6) checking calendar events or recent emails via integrations, (7) tracing fact sources or filling transcript gaps with AI, (8) launching the web dashboard for visual data browsing with 'bee ui', (9) the user wants proactive awareness of their recent activity and commitments."
---

# Bee Wearable AI — Agent Skill

Comprehensive skill for accessing real-time and historical data from the Bee wearable AI device.

## About Bee

Bee is a wearable AI device that continuously captures and transcribes ambient audio from the owner's daily life. The device listens to conversations, meetings, phone calls, and any spoken interactions throughout the day, creating a comprehensive record of the owner's verbal communications and experiences.

### How Bee Works

Bee uses advanced speech recognition to transcribe all ambient audio in real-time. This includes:

- Face-to-face conversations with friends, family, and colleagues
- Business meetings and professional discussions
- Phone calls and video conferences
- Personal reflections and voice notes (journals)
- Overheard conversations in the owner's environment

From these transcriptions, Bee automatically extracts and learns facts about the owner — their preferences, relationships, work projects, commitments, and personal details mentioned in conversations.

### Privacy and Security

**Bee data is extremely sensitive.** The transcriptions contain intimate details of the owner's personal and professional life, including private conversations that were never intended to be recorded or shared.

**All Bee data is end-to-end encrypted and accessible only to the owner.** The encryption ensures that:

- Only the authenticated owner can access their conversation transcripts
- No third parties, including Bee's servers, can read the decrypted content
- The data remains private even if storage systems are compromised
- Access requires explicit authentication through the owner's credentials

**When working with Bee data, treat all information as highly confidential.** The owner has entrusted access to their most private conversations and personal details.

## When to Use This Skill

### Real-Time Context (Use First!)

**Always start with `bee now` to understand what's happening right now.** This is the most valuable context for providing relevant assistance:

- The owner just finished a conversation and needs help following up
- The owner is asking about something that was just discussed
- You need current context to provide relevant suggestions
- The owner wants to recall what someone just said

### Life Overview

Use for broader context about who the owner is:

- **Learning about the owner**: Access facts that Bee has extracted from conversations to understand preferences, relationships, work, and personal details
- **Searching for relevant conversations**: Find past conversations on specific topics, with certain people, or about particular projects
- **Managing personal knowledge**: View, update, or organize the facts Bee has learned
- **Tracking commitments**: Access todos and action items extracted from conversations
- **Reviewing daily activity**: See summaries of each day's conversations and interactions

### Speaker Identification

Use when you need to know who said what:

- **Identifying unknown speakers**: Use AI to match speakers against known profiles
- **Building speaker fingerprints**: Learn speech patterns from assigned conversations
- **Managing speaker profiles**: Create, list, and manage profiles for people the owner interacts with

### Calendar & Mail Awareness

Use when the owner's schedule or communications are relevant:

- **Checking upcoming events**: View calendar events from connected CalDAV providers
- **Reading recent emails**: Check recent messages or search across mail integrations
- **Cross-referencing**: Combine calendar/mail context with conversation data for richer understanding

### Web Dashboard

Use when you want a visual interface instead of CLI output:

- **Launching the dashboard**: `bee ui` opens a local web UI for browsing all Bee data
- **Managing integrations**: Add, test, and remove calendar/mail providers through the UI
- **Running AI inference**: Analyze conversation transcripts for unclear utterances directly in the browser

## Prerequisites

Check if `bee` CLI is installed and authenticated:
```bash
bee --version
bee status
```

If not installed: `npm install -g @beeai/cli`
If not authenticated: `bee login` (follow the interactive flow)

## Authentication

Check authentication status:
```bash
bee status
```

If not authenticated, initiate login:
```bash
bee login
```

### Authentication Flow

The login command initiates a secure authentication flow:

1. **Read and relay the output**: The command prints a welcome message, explains the process, and provides an authentication link. Present this information to the user clearly.
2. **Authentication link**: The output includes a URL like `https://bee.computer/connect/{requestId}`. The user must open this link in their browser and approve the connection.
3. **Wait for approval**: The CLI automatically polls and waits for the user to complete authorization. Do not interrupt this process.
4. **Resumable sessions**: If interrupted, `bee login` again will resume the previous session if it hasn't expired.
5. **Expiration**: Authentication requests expire after approximately 5 minutes. If expired, a new session starts automatically.
6. **Success confirmation**: Once approved, the CLI outputs a success message with the authenticated user's name. Only proceed with other commands after seeing this confirmation.

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
bee changed --json               # As JSON with next_cursor
```

### Conversations
```bash
bee conversations list                        # Browse summaries (paginated)
bee conversations list --limit 50             # More results
bee conversations list --cursor <cursor>      # Next page
bee conversations get <id>                    # FULL transcript with utterances
```

**Important:** Summaries from `list` are AI-generated and may contain inaccuracies or hallucinations. Always use `get` for accurate information — the actual utterances are the source of truth.

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

**Confirmed vs Non-Confirmed Facts:**

Facts are categorized as either confirmed or non-confirmed:

- **Confirmed facts**: Explicitly verified by the owner or clearly stated in conversations. These are reliable and should be trusted.
- **Non-confirmed facts**: Inferred or extracted from context but not explicitly verified. These may be accurate but could also be misinterpretations.

When using facts, always prefer confirmed facts first. Non-confirmed facts can provide additional context, but treat them as potentially inaccurate. If making decisions based on non-confirmed facts, acknowledge the uncertainty.

Facts include information like: personal preferences, relationships, work details, interests and hobbies, contact information, and important dates.

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
bee journals list --json    # JSON output
bee journals get <id>       # Full journal text
bee journals get <id> --json
```

Journals are intentional recordings (unlike ambient conversations). States: PREPARING → ANALYZING → READY.

Use journals to understand the owner's personal thoughts, ideas they wanted to remember, notes to themselves, and voice memos about tasks or plans.

### Daily Summaries
```bash
bee daily                   # Recent daily summaries
bee daily --date 2026-03-15 # Specific date
```

### Speakers — Speaker Identification
```bash
bee speakers list                                          # List all profiles
bee speakers create --name <name> [--notes <notes>]        # Create profile
bee speakers delete <name>                                 # Delete profile
bee speakers assign <conversation_id> <speaker_label> <profile_name>  # Assign speaker
bee speakers identify <conversation_id>                    # AI identification
bee speakers learn [--profile <name>] [--limit N]          # Learn fingerprints
```

**Speaker identification workflow:**

1. **Create profiles** for people the owner regularly talks to:
   ```bash
   bee speakers create --name "Sarah" --notes "Wife"
   bee speakers create --name "John" --notes "Manager at work"
   ```

2. **Manually assign** speakers in a few conversations where you know who's talking:
   ```bash
   bee speakers assign 123 "Speaker 0" "Sarah"
   bee speakers assign 123 "Speaker 1" "Owner"
   ```

3. **Learn fingerprints** from the assigned conversations:
   ```bash
   bee speakers learn
   ```

4. **Auto-identify** speakers in new conversations:
   ```bash
   bee speakers identify 456
   ```

The more conversations you manually assign, the better the AI identification becomes. Speakers with 80%+ confidence are auto-assigned.

### Cite — Fact Source Tracking
```bash
bee cite <fact_id>                         # Show citations for a fact
bee cite search --query "..." [--limit N]  # Search for supporting evidence
bee cite rebuild                           # Rebuild citation index for all confirmed facts
```

Track where facts came from by linking them back to the conversations they were extracted from. Displays relevance scores and text snippets.

### Infer — AI Transcript Gap Filling
```bash
bee infer <conversation_id>                          # Analyze and infer unclear utterances
bee infer list [--conversation <id>] [--limit N]     # List stored inferences
bee infer clear [--conversation <id>]                # Clear stored inferences
```

Use AI to fill in unclear or low-confidence portions of conversation transcripts. Only processes utterances with low confidence. Results are stored only if inference confidence exceeds 0.3.

**Important**: Inferred text is always marked as `[AI INFERRED]` and never overwrites original transcriptions. Treat inferences as best guesses, not ground truth.

### Config — AI Provider Setup
```bash
bee config set <key> <value>   # Set configuration
bee config get <key>           # Get configuration
bee config list                # List all configuration
bee config delete <key>        # Delete entry
```

**Common configuration keys:**
- `ai_provider` — AI backend to use (`openai` or `anthropic`)
- `openai_api_key` — OpenAI API key
- `anthropic_api_key` — Anthropic API key

Several commands (speaker identification, inference, citation verification) require an AI provider to be configured.

### Integrations — Provider Management
```bash
bee integrations add <calendar|mail>   # Add integration (interactive)
bee integrations list                  # List configured integrations
bee integrations remove <name>         # Remove integration
bee integrations test <name>           # Test connection
```

Set up connections to external calendar and mail providers. Walks through provider selection (iCloud, Google, Outlook, or Generic), server configuration, and credential setup.

### Calendar — CalDAV Events
```bash
bee calendar list                                        # List all calendars
bee calendar events [--from YYYY-MM-DD] [--to YYYY-MM-DD] # View events
```

Requires at least one calendar integration (`bee integrations add calendar`). Defaults to today through 7 days ahead. Results sorted by start time.

### Mail — IMAP Messages
```bash
bee mail recent [--limit N]                                   # Recent messages
bee mail search --query "..." [--provider NAME] [--limit N]   # Search mail
```

Requires at least one mail integration (`bee integrations add mail`). Default limit is 20. Sorted by date (newest first).

### UI — Web Dashboard
```bash
bee ui [--port N]    # Default port 3773
```

Opens a local web dashboard in your browser with full GUI for:
- Conversations: Browse, search, read full transcripts with speaker labels
- Facts: View, create, edit, delete with confirmation status
- Todos: Manage action items with completion tracking
- Journals: Read voice memos and personal recordings
- Calendar & Mail: Browse events/emails, manage integrations
- Settings: Configure AI provider keys, view connection status
- AI Inference: Run inference on transcripts from conversation detail view
- Getting Started: Built-in guided setup

### Data Export
```bash
bee sync                                     # Export everything to ./bee-sync/
bee sync --output /path/to/dir               # Custom output directory
bee sync --only facts,todos                  # Specific data types
```

Sync exports all Bee data to markdown files. Useful for offline backup, full-text search, and initial setup before using `bee changed` for incremental updates. Note: sync overwrites existing files and does not tell you what changed.

### Change Feed (Incremental Updates)
```bash
bee changed                      # Recent changes
bee changed --cursor <cursor>    # Changes since cursor
bee changed --json               # As JSON with next_cursor
```

**Cursor workflow:**
1. Read stored cursor from `.bee-cursor` file (if exists)
2. Call `bee changed --cursor <cursor>` (or without cursor on first run)
3. Note the `Next Cursor` value from output — do NOT save yet
4. Process all returned changes
5. Only after processing succeeds, save the new cursor to `.bee-cursor`
6. Repeat

**Critical**: Save cursor only AFTER processing. If you save before and processing fails, you lose those changes forever.

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

## Common Workflows

### Quick Context — Understanding the Owner

Always run these commands in this order:
```bash
bee now
bee facts list
bee conversations list
```

Start with `bee now` — full utterance transcripts from the last 10 hours. Then supplement with facts (who the owner is) and conversation list (historical context).

### Finding Information from Past Conversations

```bash
bee conversations list
bee conversations get <relevant-conversation-id>
```

List to find the relevant timeframe or topic, then view the full transcript for accuracy.

### Setting Up AI Features

```bash
bee config set ai_provider openai
bee config set openai_api_key sk-...
bee config list
```

Or for Anthropic: `bee config set ai_provider anthropic` and `bee config set anthropic_api_key sk-ant-...`

### Setting Up Calendar & Mail

```bash
bee integrations add calendar
bee integrations add mail
bee integrations test my-calendar
bee calendar events
bee mail recent
```

### Speaker Assignment Workflow

1. Create profiles → 2. View and assign known speakers → 3. Learn fingerprints → 4. Auto-identify in new conversations.

## Deep Learning Workflow

For comprehensive user profiling, process the entire conversation history using a multi-agent chain.

### Overview

Each subagent in the chain:
1. Fetches a batch of conversations (100 at a time)
2. Reads the current `user.md` profile
3. Analyzes conversations and extracts insights
4. Updates `user.md` with new information (merge, don't overwrite)
5. Writes a summary to `bee-learning-summary.md`
6. Spawns the next subagent to continue processing

### File Structure

- `user.md` — Cumulative profile of the owner (persistent, updated by each subagent)
- `bee-learning-summary.md` — Handoff file with latest summary and cursor for next batch
- `bee-learning-progress.md` — Progress log showing what was processed and when

### Step 1: Initialize

Create initial `user.md` with sections: Basic Information, Relationships, Work & Projects, Interests & Hobbies, Preferences, Important Dates & Events, Notes.

### Step 2: Launch Processing Chain

Spawn subagents that:
1. Fetch 100 conversations using cursor from `bee-learning-summary.md`
2. Read current `user.md`
3. Extract: relationships, topics, personal details, commitments, dates
4. Merge new info into `user.md` with timestamps
5. Update `bee-learning-summary.md` with cursor and counts
6. Update `bee-learning-progress.md`
7. If more conversations exist, spawn next subagent

### Best Practices for Deep Learning

1. **File-based handoff**: Pass state between subagents via files, not by copying text into prompts
2. **Incremental updates**: Merge new info into `user.md`, never replace
3. **Progress tracking**: Maintain `bee-learning-progress.md` for resumability
4. **Weekly summaries**: Report to user periodically (after each week of conversations)
5. **Graceful completion**: When cursor is null, write final summary and notify
6. **Error handling**: Next attempt reads progress files and resumes from where it left off

### When to Use Deep Learning

Use when: first establishing a relationship, user requests comprehensive analysis, building long-term assistant context, or user wants to understand patterns in their conversations.

Don't use for: quick questions (use `bee daily`), specific facts (use `bee facts list`), or finding a particular conversation (use `bee conversations list`).

## Best Practices

1. **Always start with `bee now`** — real-time context makes assistance immediately relevant
2. **Use `get` over `list`** when accuracy matters — summaries can hallucinate
3. **Neural search for questions** — keyword search for specific terms
4. **Persist cursors** for `bee changed` to avoid reprocessing
5. **Treat all data as confidential** — this is the owner's private life
6. **Prefer confirmed facts** over unconfirmed ones
7. **Create facts proactively** when you learn something important about the owner
8. **Use `bee changed` for periodic monitoring** — more efficient than full sync
9. **Build speaker profiles early** — better identification improves over time
