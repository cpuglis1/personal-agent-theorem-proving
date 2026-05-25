#!/bin/bash
# ~/ai/setup.sh — One-time setup script
# Run: bash ~/ai/setup.sh

set -e
echo "=== Step 1: Symlink Claude Code slash commands ==="
mkdir -p ~/.claude/commands
ln -sf ~/ai/skills/commands/resume.md   ~/.claude/commands/resume.md
ln -sf ~/ai/skills/commands/research.md ~/.claude/commands/research.md
ln -sf ~/ai/skills/commands/standup.md  ~/.claude/commands/standup.md
echo "  ✓ Slash commands linked"

echo ""
echo "=== Step 2: Restart Docker stack (adds Qdrant) ==="
cd ~/ai/ai-router
docker compose down
docker compose up -d
echo "  ✓ Stack restarting — run 'docker compose ps' to watch"

echo ""
echo "=== Step 3: Install secondbrain Python deps ==="
cd ~/ai/secondbrain
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt --quiet
echo "  ✓ Dependencies installed"

echo ""
echo "=== Done with automated steps ==="
echo ""
echo "Next — complete these manually:"
echo "  1. Edit ~/ai/secondbrain/.env with your NOTION_API_KEY and database IDs"
echo "     (Cowork will guide you through this)"
echo "  2. Then run: cd ~/ai/secondbrain && source .venv/bin/activate && python ingest_notion.py"
echo "  3. Then run: python sync_md_to_notion.py"
