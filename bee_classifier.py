#!/usr/bin/env python3
from __future__ import annotations
"""
Bee Content Classifier & Router
================================
Hybrid categorization system for Bee wearable data. Uses predefined top-level
categories with keyword/pattern matching for fast routing, plus structured
metadata extraction for nuance (topics, action items, priority, sentiment).

Integrates with OpenCLAW's memory system:
  - Writes daily markdown memory files to ~/.openclaw/workspace/memory/
  - Updates categorized inbox folders: ~/.bee-agent/inbox/{category}/
  - Generates markdown knowledge base: ~/.bee-agent/knowledge/
  - Produces daily digests: ~/.bee-agent/digests/

Categories:
  work       - Professional, meetings, projects, deadlines, colleagues
  personal   - Family, friends, personal plans, errands
  health     - Medical, exercise, diet, mental health, appointments
  projects   - Specific builds, coding, creative work, side projects
  ideas      - Brainstorms, shower thoughts, random inspiration
  learning   - Research, courses, articles, things to explore
  finance    - Money, purchases, budgets, investments
  meta       - About the Bee itself, OpenCLAW config, system stuff
"""

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Categories & Rules
# ---------------------------------------------------------------------------

CATEGORIES = {
    "work": {
        "keywords": [
            "meeting", "deadline", "project", "client", "boss", "team",
            "presentation", "report", "email", "schedule", "office",
            "colleague", "manager", "sprint", "standup", "review",
            "powerschool", "district", "school", "student", "teacher",
            "admin", "sunnyside", "SIS", "data governance",
        ],
        "patterns": [
            r"\b(meeting|deadline|sprint|standup)\b",
            r"\b(work|office|professional)\b",
            r"\b(client|customer|vendor)\b",
        ],
        "description": "Professional activities, meetings, and work tasks",
    },
    "personal": {
        "keywords": [
            "family", "friend", "dinner", "weekend", "home", "dog", "cat",
            "pet", "grocery", "errand", "birthday", "anniversary",
            "vacation", "trip", "plan", "pick up", "drop off",
            "walk the dog", "feed the", "clean the", "laundry",
        ],
        "patterns": [
            r"\b(family|friend|personal|home)\b",
            r"\b(errand|grocery|groceries|shopping)\b",
            r"\b(dinner|lunch|breakfast)\s+(?:with|at|for)\b",
            r"\bwalk\s+the\s+dog\b",
            r"\bpick\s+up\b",
        ],
        "description": "Family, friends, and personal life",
    },
    "health": {
        "keywords": [
            "doctor", "appointment", "medicine", "exercise", "gym", "run",
            "walk", "diet", "sleep", "therapy", "mental health", "stress",
            "headache", "pain", "sick", "hospital", "prescription",
        ],
        "patterns": [
            r"\b(doctor|hospital|appointment|prescription)\b",
            r"\b(exercise|workout|gym|run|walk)\b",
            r"\b(sleep|diet|health|therapy)\b",
        ],
        "description": "Health, medical, fitness, and wellbeing",
    },
    "projects": {
        "keywords": [
            "build", "code", "app", "swift", "swiftui", "python", "react",
            "deploy", "github", "repo", "feature", "bug", "api",
            "bee agent", "mcp", "openclaw", "video journal", "grant research",
            "quickrinse", "macsiah", "cricut", "blender", "3d model",
        ],
        "patterns": [
            r"\b(build|code|deploy|develop|ship)\b",
            r"\b(github|repo|branch|commit|PR)\b",
            r"\b(app|feature|bug|api)\b",
        ],
        "description": "Active builds, coding, and creative projects",
    },
    "ideas": {
        "keywords": [
            "idea", "thought", "what if", "imagine", "brainstorm",
            "concept", "inspiration", "random", "shower thought",
            "wouldn't it be", "we could", "i wonder",
        ],
        "patterns": [
            r"\b(idea|thought|imagine|brainstorm|concept)\b",
            r"\bwhat\s+if\b",
            r"wouldn't it be\b",
            r"\bi\s+wonder\b",
            r"\bwe\s+could\b",
            r"\bwhat\s+about\b",
            r"\bhow\s+about\b",
        ],
        "description": "Brainstorms, inspiration, and raw ideas",
    },
    "learning": {
        "keywords": [
            "learn", "research", "article", "course", "tutorial", "book",
            "read", "study", "explore", "understand", "interesting",
            "harmonica", "blues", "music theory",
        ],
        "patterns": [
            r"\b(learn|research|study|explore)\b",
            r"\b(article|course|tutorial|book)\b",
        ],
        "description": "Research, learning, and exploration",
    },
    "finance": {
        "keywords": [
            "money", "budget", "payment", "buy", "purchase", "invest",
            "expense", "cost", "price", "retirement", "savings",
            "tesla", "subscription",
        ],
        "patterns": [
            r"\b(money|budget|payment|invest)\b",
            r"\b(cost|price|purchase|expense)\b",
            r"\$\d+",
        ],
        "description": "Financial matters, purchases, and budgets",
    },
    "meta": {
        "keywords": [
            "bee", "wearable", "openclaw", "claude", "agent", "config",
            "setup", "install", "system", "settings",
        ],
        "patterns": [
            r"\b(bee|openclaw|claude|agent)\b.*\b(setup|config|settings)\b",
        ],
        "description": "System, configuration, and meta topics",
    },
}

# Priority signals
PRIORITY_SIGNALS = {
    "urgent": {
        "keywords": ["urgent", "asap", "immediately", "now", "emergency", "critical"],
        "patterns": [r"\b(urgent|asap|emergency|critical|right now)\b"],
        "level": "high",
    },
    "important": {
        "keywords": ["important", "priority", "must", "need to", "don't forget", "remember"],
        "patterns": [r"\b(important|priority|must|crucial)\b", r"don't forget", r"need to"],
        "level": "medium",
    },
}

# Action item detection
ACTION_PATTERNS = [
    r"(?:i |we )(?:need to|should|must|have to|gotta|gonna|will|want to)\s+(.+?)(?:\.|$|,)",
    r"(?:remind me to|don't forget to|remember to)\s+(.+?)(?:\.|$|,)",
    r"(?:todo|to do|action item|task):\s*(.+?)(?:\.|$|,)",
    r"(?:let's|let me)\s+(.+?)(?:\.|$|,)",
]


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    """Result of classifying a piece of Bee content."""
    primary_category: str
    confidence: float  # 0.0-1.0
    secondary_categories: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    priority: str = "normal"  # low, normal, medium, high
    sentiment: str = "neutral"  # positive, neutral, negative
    people_mentioned: list[str] = field(default_factory=list)
    source_type: str = ""  # conversation, journal, command, thought
    summary: str = ""


class BeeClassifier:
    """Hybrid classifier: keyword/pattern rules + structured extraction."""

    def __init__(self, custom_categories: dict | None = None):
        self.categories = {**CATEGORIES}
        if custom_categories:
            self.categories.update(custom_categories)
        # Pre-compile patterns
        self._compiled = {}
        for cat, config in self.categories.items():
            self._compiled[cat] = [
                re.compile(p, re.IGNORECASE) for p in config.get("patterns", [])
            ]
        self._action_patterns = [re.compile(p, re.IGNORECASE) for p in ACTION_PATTERNS]
        self._priority_compiled = {}
        for level, config in PRIORITY_SIGNALS.items():
            self._priority_compiled[level] = [
                re.compile(p, re.IGNORECASE) for p in config["patterns"]
            ]

    def classify(self, text: str, source_type: str = "",
                 metadata: dict | None = None) -> ClassificationResult:
        """Classify text content into categories with metadata extraction."""
        if not text or not text.strip():
            return ClassificationResult(
                primary_category="meta",
                confidence=0.0,
                source_type=source_type,
            )

        text_lower = text.lower()
        scores: dict[str, float] = {}

        # Score each category
        for cat, config in self.categories.items():
            score = 0.0

            # Keyword matching (0.1 per keyword hit)
            for kw in config.get("keywords", []):
                if kw.lower() in text_lower:
                    score += 0.1

            # Pattern matching (0.15 per pattern hit)
            for pattern in self._compiled.get(cat, []):
                if pattern.search(text):
                    score += 0.15

            scores[cat] = min(score, 1.0)

        # Sort by score
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        primary = ranked[0] if ranked else ("meta", 0.0)
        secondary = [cat for cat, s in ranked[1:4] if s > 0.1]

        # Extract action items
        action_items = []
        for pattern in self._action_patterns:
            matches = pattern.findall(text)
            for m in matches:
                cleaned = m.strip().rstrip(".")
                if len(cleaned) > 3:
                    action_items.append(cleaned)

        # Detect priority
        priority = "normal"
        for level, patterns in self._priority_compiled.items():
            for p in patterns:
                if p.search(text):
                    priority = PRIORITY_SIGNALS[level]["level"]
                    break

        # Basic sentiment from keywords
        sentiment = self._detect_sentiment(text_lower)

        # Extract mentioned topics (top nouns/phrases from keywords that matched)
        topics = self._extract_topics(text_lower, primary[0])

        # Build summary (first ~100 chars, cleaned)
        summary = text.strip()[:150].replace("\n", " ")
        if len(text.strip()) > 150:
            summary += "..."

        return ClassificationResult(
            primary_category=primary[0],
            confidence=primary[1],
            secondary_categories=secondary,
            topics=topics,
            action_items=action_items,
            priority=priority,
            sentiment=sentiment,
            source_type=source_type,
            summary=summary,
        )

    def _detect_sentiment(self, text_lower: str) -> str:
        positive = ["great", "awesome", "excited", "happy", "love", "perfect",
                     "amazing", "wonderful", "good news", "progress"]
        negative = ["frustrated", "angry", "annoyed", "worried", "stress",
                     "problem", "issue", "broken", "failed", "bad"]
        pos_count = sum(1 for w in positive if w in text_lower)
        neg_count = sum(1 for w in negative if w in text_lower)
        if pos_count > neg_count:
            return "positive"
        elif neg_count > pos_count:
            return "negative"
        return "neutral"

    def _extract_topics(self, text_lower: str, primary_cat: str) -> list[str]:
        topics = []
        config = self.categories.get(primary_cat, {})
        for kw in config.get("keywords", []):
            if kw.lower() in text_lower and len(kw) > 3:
                topics.append(kw)
        return topics[:5]


# ---------------------------------------------------------------------------
# Router — sends classified content to the right destinations
# ---------------------------------------------------------------------------

class ContentRouter:
    """Routes classified Bee content to organized folders, knowledge base,
    and OpenCLAW memory system."""

    def __init__(
        self,
        inbox_dir: Path = Path.home() / ".bee-agent" / "inbox",
        knowledge_dir: Path = Path.home() / ".bee-agent" / "knowledge",
        digest_dir: Path = Path.home() / ".bee-agent" / "digests",
        openclaw_memory_dir: Path = Path.home() / ".openclaw" / "workspace" / "memory",
    ):
        self.inbox_dir = inbox_dir
        self.knowledge_dir = knowledge_dir
        self.digest_dir = digest_dir
        self.openclaw_memory_dir = openclaw_memory_dir

        # Ensure all directories exist
        for cat in CATEGORIES:
            (self.inbox_dir / cat).mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.digest_dir.mkdir(parents=True, exist_ok=True)
        # Don't create openclaw_memory_dir — it should already exist

    def route(self, content: dict, classification: ClassificationResult) -> dict:
        """Route classified content to all destinations. Returns paths written."""
        paths = {}

        # 1. Write to categorized inbox folder (JSON)
        inbox_path = self._write_categorized_inbox(content, classification)
        paths["inbox"] = str(inbox_path)

        # 2. Append to knowledge base markdown
        kb_path = self._append_knowledge_base(content, classification)
        paths["knowledge"] = str(kb_path)

        # 3. Append to OpenCLAW daily memory file
        if self.openclaw_memory_dir.exists():
            memory_path = self._append_openclaw_memory(content, classification)
            paths["memory"] = str(memory_path)

        return paths

    def _write_categorized_inbox(self, content: dict, cl: ClassificationResult) -> Path:
        """Write JSON to the categorized inbox folder."""
        cat_dir = self.inbox_dir / cl.primary_category
        cat_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{cl.source_type}-{ts}.json"

        payload = {
            **content,
            "classification": asdict(cl),
            "routed_at": datetime.now(timezone.utc).isoformat(),
        }

        path = cat_dir / filename
        path.write_text(json.dumps(payload, indent=2))
        return path

    def _append_knowledge_base(self, content: dict, cl: ClassificationResult) -> Path:
        """Append to a per-category markdown knowledge base file."""
        kb_file = self.knowledge_dir / f"{cl.primary_category}.md"
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%H:%M")

        # Build markdown entry
        lines = []
        lines.append(f"\n### {today} {now} — {cl.source_type}")
        if cl.priority != "normal":
            lines.append(f"**Priority:** {cl.priority}")
        lines.append(f"")
        lines.append(cl.summary)

        if cl.action_items:
            lines.append("")
            for item in cl.action_items:
                lines.append(f"- [ ] {item}")

        if cl.topics:
            lines.append(f"\n*Topics: {', '.join(cl.topics)}*")

        if cl.secondary_categories:
            lines.append(f"*Also relates to: {', '.join(cl.secondary_categories)}*")

        lines.append("")
        lines.append("---")

        entry = "\n".join(lines)

        # Create file with header if it doesn't exist
        if not kb_file.exists():
            header = f"# {cl.primary_category.title()} — Bee Knowledge Base\n\n"
            header += f"*{CATEGORIES.get(cl.primary_category, {}).get('description', '')}*\n"
            header += f"*Auto-populated from Bee wearable data.*\n\n---\n"
            kb_file.write_text(header + entry)
        else:
            with open(kb_file, "a") as f:
                f.write(entry)

        return kb_file

    def _append_openclaw_memory(self, content: dict, cl: ClassificationResult) -> Path:
        """Append to OpenCLAW's daily memory file for embedding/search."""
        today = datetime.now().strftime("%Y-%m-%d")
        memory_file = self.openclaw_memory_dir / f"{today}.md"
        now = datetime.now().strftime("%H:%M")

        lines = []

        # If file doesn't exist, create with header
        if not memory_file.exists():
            lines.append(f"# {today}\n")
            lines.append("## Bee Wearable Activity\n")

        # Build concise memory entry
        icon_map = {
            "conversation": "💬",
            "journal": "📓",
            "command": "⚡",
            "thought": "🧠",
        }
        icon = icon_map.get(cl.source_type, "📌")
        cat_label = cl.primary_category.title()

        lines.append(f"### {icon} [{cat_label}] {now}")
        lines.append(f"*Source: {cl.source_type} | Priority: {cl.priority}*\n")
        lines.append(cl.summary)

        if cl.action_items:
            lines.append("")
            for item in cl.action_items:
                lines.append(f"- [ ] {item}")

        if cl.topics:
            lines.append(f"\n*Topics: {', '.join(cl.topics)}*")

        lines.append("")

        entry = "\n".join(lines)

        with open(memory_file, "a") as f:
            f.write(entry)

        return memory_file

    def generate_daily_digest(self, date: str | None = None) -> Path:
        """Generate a daily digest from all categorized inbox items for a given date."""
        target_date = date or datetime.now().strftime("%Y%m%d")
        display_date = date or datetime.now().strftime("%Y-%m-%d")

        digest_lines = [
            f"# Daily Bee Digest — {display_date}\n",
            f"*Generated at {datetime.now().strftime('%H:%M')}*\n",
        ]

        total_items = 0
        category_summaries = {}

        for cat in CATEGORIES:
            cat_dir = self.inbox_dir / cat
            if not cat_dir.exists():
                continue

            items = []
            for f in sorted(cat_dir.glob(f"*-{target_date}-*.json")):
                try:
                    data = json.loads(f.read_text())
                    cl = data.get("classification", {})
                    items.append({
                        "summary": cl.get("summary", ""),
                        "priority": cl.get("priority", "normal"),
                        "action_items": cl.get("action_items", []),
                        "source_type": cl.get("source_type", ""),
                        "topics": cl.get("topics", []),
                    })
                except (json.JSONDecodeError, IOError):
                    continue

            if items:
                category_summaries[cat] = items
                total_items += len(items)

        digest_lines.append(f"**Total items captured:** {total_items}\n")

        # Summary by category
        digest_lines.append("## Overview\n")
        for cat, items in category_summaries.items():
            icon_map = {
                "work": "💼", "personal": "👤", "health": "🏥",
                "projects": "🔧", "ideas": "💡", "learning": "📚",
                "finance": "💰", "meta": "⚙️",
            }
            icon = icon_map.get(cat, "📌")
            digest_lines.append(f"- {icon} **{cat.title()}:** {len(items)} items")

        # Detail by category
        for cat, items in category_summaries.items():
            digest_lines.append(f"\n## {cat.title()}\n")
            for item in items:
                priority_badge = ""
                if item["priority"] == "high":
                    priority_badge = " 🔴"
                elif item["priority"] == "medium":
                    priority_badge = " 🟡"

                digest_lines.append(f"- **[{item['source_type']}]**{priority_badge} {item['summary']}")
                for action in item.get("action_items", []):
                    digest_lines.append(f"  - [ ] {action}")

        # All action items consolidated
        all_actions = []
        for cat, items in category_summaries.items():
            for item in items:
                for action in item.get("action_items", []):
                    all_actions.append({"action": action, "category": cat, "priority": item["priority"]})

        if all_actions:
            digest_lines.append("\n## Action Items (Consolidated)\n")
            # High priority first
            for a in sorted(all_actions, key=lambda x: {"high": 0, "medium": 1, "normal": 2, "low": 3}.get(x["priority"], 2)):
                badge = {"high": "🔴", "medium": "🟡"}.get(a["priority"], "")
                digest_lines.append(f"- [ ] {badge} [{a['category']}] {a['action']}")

        digest_lines.append(f"\n---\n*Digest generated by Bee Agent for OpenCLAW*\n")

        digest_path = self.digest_dir / f"digest-{target_date}.md"
        digest_path.write_text("\n".join(digest_lines))

        # Also write to OpenCLAW memory as a digest entry
        if self.openclaw_memory_dir.exists():
            today_file = self.openclaw_memory_dir / f"{display_date}.md"
            with open(today_file, "a") as f:
                f.write(f"\n## Daily Bee Digest\n")
                f.write(f"**{total_items} items captured across {len(category_summaries)} categories.**\n")
                if all_actions:
                    f.write(f"\n### Outstanding Action Items\n")
                    for a in all_actions:
                        f.write(f"- [ ] [{a['category']}] {a['action']}\n")
                f.write("\n")

        return digest_path


# ---------------------------------------------------------------------------
# Convenience: classify and route in one call
# ---------------------------------------------------------------------------

_default_classifier: BeeClassifier | None = None
_default_router: ContentRouter | None = None


def get_classifier() -> BeeClassifier:
    global _default_classifier
    if _default_classifier is None:
        _default_classifier = BeeClassifier()
    return _default_classifier


def get_router(
    inbox_dir: Path | None = None,
    knowledge_dir: Path | None = None,
    digest_dir: Path | None = None,
    openclaw_memory_dir: Path | None = None,
) -> ContentRouter:
    global _default_router
    if _default_router is None:
        _default_router = ContentRouter(
            inbox_dir=inbox_dir or Path.home() / ".bee-agent" / "inbox",
            knowledge_dir=knowledge_dir or Path.home() / ".bee-agent" / "knowledge",
            digest_dir=digest_dir or Path.home() / ".bee-agent" / "digests",
            openclaw_memory_dir=openclaw_memory_dir or Path.home() / ".openclaw" / "workspace" / "memory",
        )
    return _default_router


def classify_and_route(text: str, source_type: str, raw_content: dict,
                        classifier: BeeClassifier | None = None,
                        router: ContentRouter | None = None) -> tuple[ClassificationResult, dict]:
    """One-shot: classify text and route to all destinations."""
    cl = (classifier or get_classifier()).classify(text, source_type=source_type)
    paths = (router or get_router()).route(raw_content, cl)
    return cl, paths
