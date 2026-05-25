#!/bin/bash
# finish_setup.sh — run after adding NOTION_API_KEY and LITELLM_MASTER_KEY to secondbrain/.env
set -e

echo "=== Step 1: Symlink Claude Code slash commands ==="
mkdir -p ~/.claude/commands
ln -sf ~/ai/skills/commands/resume.md   ~/.claude/commands/resume.md
ln -sf ~/ai/skills/commands/research.md ~/.claude/commands/research.md
ln -sf ~/ai/skills/commands/standup.md  ~/.claude/commands/standup.md
echo "  ✓ Slash commands linked"

echo ""
echo "=== Step 2: Restart Docker stack (Qdrant healthcheck now fixed) ==="
cd ~/ai/ai-router
docker compose down
docker compose up -d --remove-orphans
echo "  ✓ Stack restarting — watching for healthy..."
sleep 30
docker compose ps

echo ""
echo "=== Step 3: Install secondbrain Python deps ==="
cd ~/ai/secondbrain
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt --quiet
echo "  ✓ Dependencies installed"

echo ""
echo "=== Step 4: Sync local .md files → Notion ==="
python sync_md_to_notion.py
echo "  ✓ MD files synced"

echo ""
echo "=== Step 5: Ingest Notion → Qdrant ==="
python ingest_notion.py
echo "  ✓ Second brain indexed in Qdrant"

echo ""
echo "✅ All done! Open http://localhost:3000 — Qdrant RAG is live."
