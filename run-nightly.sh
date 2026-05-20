#!/usr/bin/env bash
# YouTube Fact-Check Nightly Pipeline
# Runs: scrape → fact-check → report

set -euo pipefail

export HOME="${HOME:-/home/wojtek}"
export PYTHONIOENCODING=utf-8

cd "$HOME/Projects/youtube-factcheck"
LOG="$HOME/Projects/youtube-factcheck/nightly.log"

# Load .env if present (LMSTUDIO_TOKEN etc.)
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

exec > >(tee -a "$LOG") 2>&1

echo ""
echo "===================================================================="
echo "  YouTube Fact-Check — $(date '+%Y-%m-%d %H:%M')"
echo "===================================================================="

# 1. RSS aggregator → yt-subscriptions-today.txt
echo ""
echo "▶ Step 1: Aggregating subscriptions via RSS..."
python3 rss-aggregator.py

if [ ! -s yt-subscriptions-today.txt ]; then
  echo "✗ No videos to fact-check today (empty feed or all filtered). Exiting."
  exit 0
fi

COUNT=$(wc -l < yt-subscriptions-today.txt)
echo "✓ Collected $COUNT video URLs"

# 2. Run fact-check pipeline
echo ""
echo "▶ Step 2: Running fact-check pipeline..."
python3 fact-check-pipeline.py

# 3. Done
echo ""
echo "✓ Nightly pipeline complete (all outputs in $HOME/Projects/youtube-factcheck/)"
