#!/bin/bash
# ============================================================
# Bee Agent Setup Script
# ============================================================
# Sets up the complete Bee Wearable → OpenCLAW pipeline:
#   1. Hybrid MCP Server (for Claude Desktop / Cowork)
#   2. Background Monitoring Agent (always-on, keyword detection)
#   3. Inbox directory for structured outputs
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo "============================================"
echo "  Bee Agent — Setup"
echo "============================================"
echo ""

# ------ Check Prerequisites ------

echo "Checking prerequisites..."

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Python 3 not found.${NC}"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} Python $(python3 --version | awk '{print $2}')"

if command -v bee &> /dev/null; then
    echo -e "  ${GREEN}✓${NC} bee-cli installed"
else
    echo -e "  ${YELLOW}!${NC} bee-cli not found. Installing..."
    npm install -g @beeai/cli
fi

# Check authentication
BEE_STATUS=$(bee status 2>&1 || true)
if echo "$BEE_STATUS" | grep -qi "not logged in\|not authenticated\|error"; then
    echo -e "  ${RED}✗${NC} Not authenticated. Run: bee login"
    echo "    Then re-run this script."
    exit 1
fi
echo -e "  ${GREEN}✓${NC} Bee authenticated"

# ------ Install Dependencies ------

echo ""
echo "Installing MCP server dependencies..."
pip3 install --break-system-packages -q "mcp[cli]>=1.4.0" "pydantic>=2.0.0" 2>/dev/null || \
    pip3 install -q "mcp[cli]>=1.4.0" "pydantic>=2.0.0"
echo -e "  ${GREEN}✓${NC} Dependencies installed"

# ------ Create Inbox Directory ------

echo ""
echo "Creating inbox directory..."
mkdir -p ~/.bee-agent/inbox/{commands,conversations,journals,thoughts}
echo -e "  ${GREEN}✓${NC} ~/.bee-agent/inbox/ created"

# ------ Configure Claude Desktop MCP ------

echo ""
echo "============================================"
echo "  Step 1: Add MCP Server to Claude Desktop"
echo "============================================"
echo ""
echo "Add this to your claude_desktop_config.json:"
echo -e "${CYAN}"
echo "  Location: ~/Library/Application Support/Claude/claude_desktop_config.json"
echo ""
cat << JSONEOF
{
  "mcpServers": {
    "bee-wearable": {
      "command": "python3",
      "args": ["$SCRIPT_DIR/mcp_server/bee_mcp_server.py"]
    }
  }
}
JSONEOF
echo -e "${NC}"

# ------ Install Background Service ------

echo "============================================"
echo "  Step 2: Install Background Monitor"
echo "============================================"
echo ""

PLIST_SRC="$SCRIPT_DIR/standalone_agent/com.openclaw.bee-monitor.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.openclaw.bee-monitor.plist"

# Update the plist with the actual path
sed "s|REPLACE_WITH_FULL_PATH|$SCRIPT_DIR|g" "$PLIST_SRC" > "$PLIST_DST"

echo "Installed launchd service: $PLIST_DST"
echo ""
echo "To start the background monitor now:"
echo -e "  ${CYAN}launchctl load $PLIST_DST${NC}"
echo ""
echo "To stop it:"
echo -e "  ${CYAN}launchctl unload $PLIST_DST${NC}"
echo ""
echo "View logs:"
echo -e "  ${CYAN}tail -f /tmp/bee-monitor.log${NC}"

# ------ Summary ------

echo ""
echo "============================================"
echo "  How it works"
echo "============================================"
echo ""
echo "  1. The background monitor connects to 'bee stream'"
echo "     and listens to everything your Bee hears."
echo ""
echo "  2. When you PUSH the button (conversation), it tracks"
echo "     all utterances and writes the full transcript to"
echo "     ~/.bee-agent/inbox/conversations/ when it ends."
echo ""
echo "  3. When you HOLD the button (dictate a note), the"
echo "     transcription is written immediately to"
echo "     ~/.bee-agent/inbox/journals/"
echo ""
echo "  4. When you say 'OpenCLAW' followed by a command,"
echo "     the agent captures your words and writes them to"
echo "     ~/.bee-agent/inbox/commands/"
echo ""
echo "  5. Every 5 minutes, a thought digest of recent"
echo "     conversations is written to"
echo "     ~/.bee-agent/inbox/thoughts/"
echo ""
echo "  6. The MCP server gives Claude direct access to all"
echo "     Bee data (live stream + on-demand queries) in"
echo "     Claude Desktop and Cowork."
echo ""
echo "============================================"
echo -e "  ${GREEN}Setup complete!${NC}"
echo "============================================"
