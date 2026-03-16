#!/bin/bash
# ============================================================
# Bee Agent Setup Script
# ============================================================
# Installs and configures the Bee Wearable Agent ecosystem:
#   1. MCP Server (for Claude Desktop / Claude Code)
#   2. Claude Code Skill
#   3. Standalone Monitoring Agent
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo "============================================"
echo "  Bee Wearable Agent — Setup"
echo "============================================"
echo ""

# ------ Check Prerequisites ------

echo "Checking prerequisites..."

# Check Node.js
if ! command -v node &> /dev/null; then
    echo -e "${RED}Node.js not found. Install from https://nodejs.org${NC}"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} Node.js $(node --version)"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Python 3 not found.${NC}"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} Python $(python3 --version | awk '{print $2}')"

# Check bee-cli
if command -v bee &> /dev/null; then
    echo -e "  ${GREEN}✓${NC} bee-cli $(bee --version 2>/dev/null || echo 'installed')"
else
    echo -e "  ${YELLOW}!${NC} bee-cli not found. Installing..."
    npm install -g @beeai/cli
    echo -e "  ${GREEN}✓${NC} bee-cli installed"
fi

# Check bee authentication
echo ""
echo "Checking Bee authentication..."
BEE_STATUS=$(bee status 2>&1 || true)
if echo "$BEE_STATUS" | grep -qi "not logged in\|not authenticated\|error"; then
    echo -e "  ${YELLOW}!${NC} Not authenticated. Run: bee login"
    echo "     Then re-run this setup script."
else
    echo -e "  ${GREEN}✓${NC} Bee authenticated"
fi

# ------ Install MCP Server Dependencies ------

echo ""
echo "Installing MCP server dependencies..."
cd "$SCRIPT_DIR/mcp_server"
pip3 install --break-system-packages -q "mcp[cli]>=1.4.0" 2>/dev/null || \
    pip3 install -q "mcp[cli]>=1.4.0"
echo -e "  ${GREEN}✓${NC} MCP server dependencies installed"

# ------ Configure Claude Desktop ------

echo ""
echo "============================================"
echo "  Configuration Instructions"
echo "============================================"

echo ""
echo -e "${GREEN}1. MCP Server for Claude Desktop${NC}"
echo "   Add this to your Claude Desktop settings (claude_desktop_config.json):"
echo ""
echo "   {\"mcpServers\": {"
echo "     \"bee-wearable\": {"
echo "       \"command\": \"python3\","
echo "       \"args\": [\"$SCRIPT_DIR/mcp_server/bee_mcp_server.py\"]"
echo "     }"
echo "   }}"
echo ""
echo "   Config file locations:"
echo "   - macOS: ~/Library/Application Support/Claude/claude_desktop_config.json"
echo "   - Windows: %APPDATA%\\Claude\\claude_desktop_config.json"

echo ""
echo -e "${GREEN}2. MCP Server for Claude Code${NC}"
echo "   Run this command:"
echo ""
echo "   claude mcp add bee-wearable -- python3 $SCRIPT_DIR/mcp_server/bee_mcp_server.py"

echo ""
echo -e "${GREEN}3. Claude Code Skill${NC}"
echo "   Copy the skill to your Claude Code skills directory:"
echo ""
echo "   mkdir -p ~/.claude/skills/bee-wearable"
echo "   cp $SCRIPT_DIR/skill/SKILL.md ~/.claude/skills/bee-wearable/SKILL.md"

echo ""
echo -e "${GREEN}4. Standalone Monitoring Agent${NC}"
echo "   Start real-time monitoring:"
echo ""
echo "   python3 $SCRIPT_DIR/standalone_agent/bee_monitor.py"
echo ""
echo "   Or use different modes:"
echo "   python3 $SCRIPT_DIR/standalone_agent/bee_monitor.py --mode stream   # Real-time SSE"
echo "   python3 $SCRIPT_DIR/standalone_agent/bee_monitor.py --mode poll     # Periodic polling"
echo "   python3 $SCRIPT_DIR/standalone_agent/bee_monitor.py --mode digest   # One-shot summary"

echo ""
echo "============================================"
echo -e "  ${GREEN}Setup complete!${NC}"
echo "============================================"
