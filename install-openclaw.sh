#!/bin/bash
#
# Bee Agent — OpenCLAW Installer
# ================================
# Installs the Bee wearable agent into an OpenCLAW instance.
# Run this script on any machine with OpenCLAW installed.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/macsiah/bee-agent/main/install-openclaw.sh | bash
#   -- or --
#   git clone https://github.com/macsiah/bee-agent.git && cd bee-agent && bash install-openclaw.sh
#
set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

OPENCLAW_DIR="${OPENCLAW_DIR:-$HOME/.openclaw}"
BEE_AGENT_DIR="$OPENCLAW_DIR/bee-agent"
BEE_DATA_DIR="$HOME/.bee-agent"
SKILL_DIR="$OPENCLAW_DIR/skills/bee-wearable"
REPO_URL="https://github.com/macsiah/bee-agent.git"

echo -e "${CYAN}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║   🐝  Bee Agent for OpenCLAW  🐝    ║"
echo "  ║   Real-time wearable intelligence    ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${NC}"

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

echo -e "${BLUE}[1/7]${NC} Checking prerequisites..."

# Check OpenCLAW
if [ ! -d "$OPENCLAW_DIR" ]; then
    echo -e "${RED}✗ OpenCLAW not found at $OPENCLAW_DIR${NC}"
    echo "  Install OpenCLAW first, or set OPENCLAW_DIR to your install path."
    exit 1
fi
echo -e "  ${GREEN}✓${NC} OpenCLAW found at $OPENCLAW_DIR"

# Check Python 3.10+
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}✗ Python 3 not found${NC}"
    echo "  Install Python 3.10+ first."
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "  ${GREEN}✓${NC} Python $PY_VER"

# Check bee-cli
if ! command -v bee &>/dev/null; then
    echo -e "${YELLOW}⚠ bee-cli not found${NC}"
    echo "  Installing bee-cli..."
    if command -v npm &>/dev/null; then
        npm install -g @beeai/cli
    elif command -v bun &>/dev/null; then
        bun install -g @beeai/cli
    else
        echo -e "${RED}✗ Neither npm nor bun found. Install bee-cli manually:${NC}"
        echo "  npm install -g @beeai/cli"
        exit 1
    fi
fi
echo -e "  ${GREEN}✓${NC} bee-cli installed"

# Check bee authentication
if ! bee status &>/dev/null 2>&1; then
    echo -e "${YELLOW}⚠ bee-cli not authenticated${NC}"
    echo "  Run 'bee login' to authenticate, then re-run this installer."
    exit 1
fi
BEE_USER=$(bee status 2>/dev/null | sed -n 's/.*Verified as //p' || echo "authenticated")
echo -e "  ${GREEN}✓${NC} Bee authenticated as: $BEE_USER"

# ---------------------------------------------------------------------------
# Clone or update the repo
# ---------------------------------------------------------------------------

echo -e "\n${BLUE}[2/7]${NC} Installing bee-agent code..."

if [ -d "$BEE_AGENT_DIR/.git" ]; then
    echo "  Updating existing installation..."
    cd "$BEE_AGENT_DIR"
    git pull --ff-only 2>/dev/null || {
        echo -e "${YELLOW}  ⚠ Could not fast-forward, using existing code${NC}"
    }
else
    echo "  Cloning from $REPO_URL..."
    git clone "$REPO_URL" "$BEE_AGENT_DIR" 2>/dev/null || {
        # If clone fails (repo might already exist without .git), copy from local
        if [ -d "$(dirname "$0")/mcp_server" ]; then
            echo "  Copying from local directory..."
            mkdir -p "$BEE_AGENT_DIR"
            cp -R "$(dirname "$0")"/* "$BEE_AGENT_DIR/"
        else
            echo -e "${RED}✗ Could not clone repo and no local files found${NC}"
            exit 1
        fi
    }
fi
echo -e "  ${GREEN}✓${NC} Code installed at $BEE_AGENT_DIR"

# ---------------------------------------------------------------------------
# Install Python dependencies
# ---------------------------------------------------------------------------

echo -e "\n${BLUE}[3/7]${NC} Installing Python dependencies..."

pip3 install --quiet --break-system-packages "mcp[cli]>=1.4.0" "pydantic>=2.0.0" 2>/dev/null || \
pip3 install --quiet "mcp[cli]>=1.4.0" "pydantic>=2.0.0" 2>/dev/null || {
    echo -e "${YELLOW}  ⚠ pip install had issues, trying with --user${NC}"
    pip3 install --user "mcp[cli]>=1.4.0" "pydantic>=2.0.0"
}
echo -e "  ${GREEN}✓${NC} Python dependencies installed"

# ---------------------------------------------------------------------------
# Create directory structure
# ---------------------------------------------------------------------------

echo -e "\n${BLUE}[4/7]${NC} Creating directory structure..."

# Bee agent data directories
mkdir -p "$BEE_DATA_DIR/inbox/commands"
mkdir -p "$BEE_DATA_DIR/inbox/conversations"
mkdir -p "$BEE_DATA_DIR/inbox/journals"
mkdir -p "$BEE_DATA_DIR/inbox/thoughts"
mkdir -p "$BEE_DATA_DIR/inbox/actions"
mkdir -p "$BEE_DATA_DIR/knowledge"
mkdir -p "$BEE_DATA_DIR/digests"

# Categorized inbox directories
for cat in work personal health projects ideas learning finance meta; do
    mkdir -p "$BEE_DATA_DIR/inbox/$cat"
done

# Create seed contacts file if it doesn't exist
if [ ! -f "$BEE_DATA_DIR/contacts.json" ]; then
    cat > "$BEE_DATA_DIR/contacts.json" << 'CONTACTS_EOF'
{
  "mom": "+1XXXXXXXXXX",
  "dad": "+1XXXXXXXXXX",
  "example": "name@example.com"
}
CONTACTS_EOF
    echo "  Created seed contacts.json — edit ~/.bee-agent/contacts.json with your contacts"
fi

# Check for imsg CLI
if command -v imsg &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} imsg CLI found (iMessage support enabled)"
else
    echo -e "  ${YELLOW}⚠${NC} imsg CLI not found — iMessage sending will be queued for OpenCLAW"
    echo "  Install with: brew install imsg"
fi

echo -e "  ${GREEN}✓${NC} Data directories created at $BEE_DATA_DIR"

# ---------------------------------------------------------------------------
# Install OpenCLAW skill
# ---------------------------------------------------------------------------

echo -e "\n${BLUE}[5/7]${NC} Installing OpenCLAW skill..."

# Remove broken symlink if exists
if [ -L "$OPENCLAW_DIR/skills/bee-cli" ]; then
    rm -f "$OPENCLAW_DIR/skills/bee-cli"
    echo "  Removed stale bee-cli symlink"
fi

# Create skill directory
mkdir -p "$SKILL_DIR"
cp "$BEE_AGENT_DIR/skill/SKILL.md" "$SKILL_DIR/SKILL.md"
echo -e "  ${GREEN}✓${NC} Skill installed at $SKILL_DIR"

# ---------------------------------------------------------------------------
# Install launchd service (macOS background monitor)
# ---------------------------------------------------------------------------

echo -e "\n${BLUE}[6/7]${NC} Setting up background monitor..."

PLIST_NAME="com.openclaw.bee-monitor"
PLIST_SRC="$BEE_AGENT_DIR/standalone_agent/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

if [ "$(uname)" = "Darwin" ]; then
    # Unload if already loaded
    launchctl unload "$PLIST_DST" 2>/dev/null || true

    # Create plist with correct paths
    cat > "$PLIST_DST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which python3)</string>
        <string>$BEE_AGENT_DIR/standalone_agent/bee_monitor.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$BEE_AGENT_DIR/standalone_agent</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$HOME/.bun/bin:$(dirname "$(which node 2>/dev/null)" 2>/dev/null || echo "/usr/local/bin")</string>
        <key>PYTHONPATH</key>
        <string>$BEE_AGENT_DIR/standalone_agent</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/bee-monitor.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/bee-monitor.err</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
PLIST_EOF

    echo -e "  ${GREEN}✓${NC} LaunchAgent installed at $PLIST_DST"
    echo ""
    echo -e "  ${YELLOW}To start the monitor now:${NC}"
    echo "    launchctl load $PLIST_DST"
    echo ""
    echo -e "  ${YELLOW}To check if it's running:${NC}"
    echo "    launchctl list | grep bee-monitor"
    echo "    tail -f /tmp/bee-monitor.log"
else
    echo -e "  ${YELLOW}⚠ Not macOS — skipping launchd setup${NC}"
    echo "  You can run the monitor manually:"
    echo "    python3 $BEE_AGENT_DIR/standalone_agent/bee_monitor.py"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo -e "\n${BLUE}[7/7]${NC} Installation complete!"
echo ""
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  🐝  Bee Agent installed successfully!  🐝${NC}"
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}What was installed:${NC}"
echo "    • MCP Server:  $BEE_AGENT_DIR/mcp_server/bee_mcp_server.py"
echo "    • Monitor:     $BEE_AGENT_DIR/standalone_agent/bee_monitor.py"
echo "    • Classifier:  $BEE_AGENT_DIR/standalone_agent/bee_classifier.py"
echo "    • Skill:       $SKILL_DIR/SKILL.md"
echo "    • Data:        $BEE_DATA_DIR/"
echo ""
echo -e "  ${CYAN}Directory structure:${NC}"
echo "    ~/.bee-agent/"
echo "    ├── inbox/"
echo "    │   ├── commands/      # Voice-triggered commands"
echo "    │   ├── conversations/ # Completed conversation transcripts"
echo "    │   ├── journals/      # Dictated notes"
echo "    │   ├── thoughts/      # Periodic thought digests"
echo "    │   ├── work/          # Work-related items"
echo "    │   ├── personal/      # Personal items"
echo "    │   ├── health/        # Health-related items"
echo "    │   ├── projects/      # Project-related items"
echo "    │   ├── ideas/         # Ideas and brainstorms"
echo "    │   ├── learning/      # Learning and research"
echo "    │   ├── finance/       # Financial items"
echo "    │   └── meta/          # System/config items"
echo "    ├── knowledge/         # Markdown knowledge base (per-category)"
echo "    └── digests/           # Daily digest summaries"
echo ""
echo -e "  ${CYAN}Next steps:${NC}"
echo "    1. Start the monitor:  launchctl load ~/Library/LaunchAgents/$PLIST_NAME.plist"
echo "    2. Test with Bee:      Hold button and dictate a note"
echo "    3. Check logs:         tail -f /tmp/bee-monitor.log"
echo "    4. View knowledge:     ls ~/.bee-agent/knowledge/"
echo "    5. View digests:       ls ~/.bee-agent/digests/"
echo ""
echo -e "  ${CYAN}Memory integration:${NC}"
echo "    Bee data is automatically written to OpenCLAW's daily memory files at:"
echo "    $OPENCLAW_DIR/workspace/memory/YYYY-MM-DD.md"
echo "    This enables semantic search across all your Bee data via OpenCLAW."
echo ""
