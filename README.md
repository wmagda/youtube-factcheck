# YouTube Fact-Check System

Nightly automated pipeline that reads your YouTube subscription feed via RSS,
extracts factual claims from video transcripts, and cross-checks them against
web sources using your local LM Studio instance.

All output files live in this folder: `/home/wojtek/Projects/youtube-factcheck/`.

---

## Files

| File | Purpose |
|---|---|
| `rss-aggregator.py` | Reads `yt-channel-ids.json` → fetches RSS feeds → writes `yt-subscriptions-today.txt` |
| `fact-check-pipeline.py` | Reads `yt-subscriptions-today.txt` → transcripts → claims → web search → `fact-check-report.txt` + `channel-scores.jsonl` |
| `run-nightly.sh` | Orchestrator — calls both in order; called by cron |
| `PROCESS.md` | Technical reference (data flow, JS snippets, RSS details) |

---

## One-Time Setup (channels)

You need **one manual step** to seed the system with your subscriptions.

### 1. Get your channel IDs

1. Open Chromium (already logged in to YouTube)
2. Go to https://www.youtube.com/feed/subscriptions
3. DevTools → Console → paste:

```javascript
const channels = Array.from(document.querySelectorAll('ytd-channel-renderer')).map(el => {
  const name = el.querySelector('#channel-title #text')?.textContent.trim();
  const link = el.querySelector('a#main-link')?.href;
  const idOrHandle = link ? link.split('/').pop() : null;
  return { name, idOrHandle, link };
}).filter(ch => ch.name && ch.idOrHandle);
console.table(channels);
copy(JSON.stringify(channels));
```

4. `copy()` puts the JSON into your clipboard — paste it here in this chat and I'll save it to `yt-channel-ids.json`.

### 2. Dependencies

```bash
pip install youtube-transcript-api --break-system-packages
```

### 3. LM Studio token

LM Studio requires an API token. Get it from **LM Studio → Settings → API → "API Token"**.

Create `~/Projects/youtube-factcheck/.env`:

```bash
cat > /home/wojtek/Projects/youtube-factcheck/.env <<EOF
LMSTUDIO_TOKEN=your_token_here
EOF
```

This file is git-ignored by convention and never committed.

### 4. Confirm LM Studio is reachable

```bash
curl -s -H "Authorization: Bearer $LMSTUDIO_TOKEN" http://192.168.0.195:1234/v1/models | head
```

---

## Manual Test

```bash
cd /home/wojtek/Projects/youtube-factcheck

# Fill today's video list from RSS
python3 rss-aggregator.py

# Fact-check those videos
python3 fact-check-pipeline.py

# Read the report
cat fact-check-report.txt
```

---

## Nightly Cron

Runs every night at **11:00 PM**:

```
0 23 * * * DISPLAY=:0 /home/wojtek/.hermes/scripts/run-yt-factcheck.sh >> /home/wojtek/Projects/youtube-factcheck/cron.log 2>&1
```

Check/edit with `crontab -e` or `crontab -l`.

---

## Output Files

| File | Description |
|---|---|
| `yt-channel-ids.json` | Your subscription channel list (you populate this once) |
| `yt-subscriptions-today.txt` | URLs from today's RSS feed, auto-filtered (one per line) |
| `fact-check-report.txt` | Nightly human-readable report (✅/⚠️/❌ per video) |
| `channel-scores.jsonl` | Append-only per-video record (date, channel, verdict, stats) |
| `nightly.log` | Raw cron output / debug log |
| `cron.log` | Cron daemon output |

---

## Auto-Skip Keywords

These titles are excluded before analysis:
`short`, `music`, `song`, `album`, `lyrics`, `meme`, `ytp`, `youtube poop`,
`reaction`, `react`, `prank`, `skit`, `gaming`, `let's play`, `gameplay`,
`vlog`, `stream`, `live`, `podcast`, `electronic`, `techno`, `dubstep`, `remix`, `asmr`

Edit `SKIP_PATTERNS` in `rss-aggregator.py` to change.

---

## Troubleshooting

- **No videos in report** — Your channels haven't posted today, or all got filtered. Check `nightly.log`.
- **All transcripts missing** — Normal for ~30% of videos (music/YT shorts/etc).
- **LM Studio connection refused** — Verify `http://192.168.0.195:1234/v1/models` returns JSON; restart LM Studio.
- **Empty `yt-channel-ids.json`** — You haven't run the console snippet yet. Do that first.

---

## Files

```
/home/wojtek/Projects/youtube-factcheck/
├── fact-check-pipeline.py
├── rss-aggregator.py
├── run-nightly.sh
├── PROCESS.md
├── README.md
├── yt-channel-ids.json         ← populate this now
├── yt-subscriptions-today.txt  (generated)
├── fact-check-report.txt       (generated)
├── channel-scores.jsonl        (generated)
├── nightly.log                 (generated)
└── cron.log                    (generated)
```
