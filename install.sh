#!/bin/bash
# MONETA INSTALL SCRIPT
# Run this once from your WSL terminal: bash ~/moneta/install.sh

set -e

echo ""
echo "  ╔════════════════════════════════╗"
echo "  ║   MONETA — INSTALL SEQUENCE    ║"
echo "  ╚════════════════════════════════╝"
echo ""

MONETA_DIR="$HOME/moneta"
VENV="$MONETA_DIR/venv"

# 1. Create venv
echo "→ Creating Python virtual environment..."
python3 -m venv "$VENV"

# 2. Install dependencies
echo "→ Installing dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet flask anthropic

# 3. Create .env if it doesn't exist
if [ ! -f "$MONETA_DIR/.env" ]; then
  echo "→ Creating .env file..."
  cat > "$MONETA_DIR/.env" << 'EOF'
ANTHROPIC_API_KEY=your_api_key_here
MONETA_SECRET=moneta-sto-plains-lair-2026
EOF
  echo ""
  echo "  ⚠  IMPORTANT: Edit ~/moneta/.env and add your Anthropic API key"
  echo "     nano ~/moneta/.env"
  echo ""
else
  echo "→ .env already exists, skipping..."
fi

# 4. Initialize database
echo "→ Initializing database..."
"$VENV/bin/python" -c "
import sys
sys.path.insert(0, '$MONETA_DIR')
import sqlite3, os
db = os.path.expanduser('~/moneta/moneta.db')
schema = open(os.path.expanduser('~/moneta/schema.sql')).read()
conn = sqlite3.connect(db)
conn.executescript(schema)
conn.commit()
conn.close()
print('  Database initialized: ~/moneta/moneta.db')
"

# 5. Install systemd service
echo "→ Installing systemd service..."
sudo cp "$MONETA_DIR/moneta.service" /etc/systemd/system/moneta.service
sudo systemctl daemon-reload
sudo systemctl enable moneta

echo ""
echo "  ╔════════════════════════════════════════════╗"
echo "  ║   INSTALL COMPLETE                         ║"
echo "  ╠════════════════════════════════════════════╣"
echo "  ║                                            ║"
echo "  ║   1. Add your API key:                     ║"
echo "  ║      nano ~/moneta/.env                    ║"
echo "  ║                                            ║"
echo "  ║   2. Start Moneta:                         ║"
echo "  ║      sudo systemctl start moneta           ║"
echo "  ║                                            ║"
echo "  ║   3. Open in browser:                      ║"
echo "  ║      http://100.111.142.83:5001            ║"
echo "  ║                                            ║"
echo "  ║   RESTART COMMAND:                         ║"
echo "  ║   sudo systemctl stop moneta &&            ║"
echo "  ║   sudo pkill -f moneta.py;                 ║"
echo "  ║   sleep 2 &&                               ║"
echo "  ║   sudo systemctl start moneta              ║"
echo "  ║                                            ║"
echo "  ╚════════════════════════════════════════════╝"
echo ""
