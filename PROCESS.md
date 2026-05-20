# Technical Reference — YouTube Fact-Check System

## Data Flow

```
cron (11 PM)
  │
  ▼
run-nightly.sh
  └─ rss-aggregator.py   → yt-subscriptions-today.txt
  └─ fact-check-pipeline.py
         ├── youtube-transcript-api  → per-video transcript
         ├── LM Studio (local)       → extract factual claims (3-10 per video)
         ├── DuckDuckGo HTML scrape   → search each claim
         ├── LM Studio (local)       → parse verdicts from search
         ├── score_video()            → aggregate verdict
         ├── fact-check-report.txt   ← human-readable report
         └── channel-scores.jsonl    ← append-only reliability log
```

---

## Channel ID Extraction (one-time, manual)

Open Chromium → `https://www.youtube.com/feed/subscriptions` → DevTools → Console:

```javascript
const channels = Array.from(document.querySelectorAll('ytd-channel-renderer')).map(el => {
  const name = el.querySelector('#channel-title #text')?.textContent.trim();
  const link = el.querySelector('a#main-link')?.href;
  const idOrHandle = link ? link.split('/').pop() : null;
  return { name, idOrHandle, link };
}).filter(ch => ch.name && ch.idOrHandle);
console.table(channels);
copy(JSON.stringify(channels));   // copies JSON to clipboard
```

Paste the result into `yt-channel-ids.json` (located in this folder).

Format:
```json
[
  { "name": "Channel Name", "idOrHandle": "UCabcdef...", "link": "https://youtube.com/@handle" }
]
```

---

## Video URL Extraction (one-time / alternative to RSS)

If you ever want to scrape the visual feed instead of RSS, run in DevTools Console
on the subscriptions page (scroll first to load content):

```javascript
const results = [];
const seen = new Set();

document
  .querySelectorAll('a.ytLockupViewModelContentImage[href*="/watch?v="]')
  .forEach(a => {
    const href = 'https://www.youtube.com' + a.getAttribute('href');
    if (seen.has(href)) return;
    seen.add(href);

    const container =
      a.closest('ytd-rich-item-renderer') ||
      a.closest('yt-lockup-view-model') ||
      a.parentElement?.parentElement?.parentElement;
    if (!container) return;

    const titleEl = container.querySelector('h3 a, h3, yt-formatted-string');
    const title = titleEl?.textContent?.trim();
    if (title) results.push({ title, href });
  });
copy(results.map(r => r.href).join('\n'));
```

---

## RSS Feed Format

YouTube RSS feeds are public; no OAuth needed.

```
https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID
```

Each feed returns the 15 most recent videos with `<yt:videoId>` and `<media:title>` tags.
Python's `rss-aggregator.py` fetches all channels, de-duplicates, applies the auto-skip
keyword filter, and caps output at 30 URLs per night (`MAX_TOTAL = 30`, `MAX_PER_CHANNEL = 2`).

---

## Claim Extraction Prompt

Used in `fact-check-pipeline.py` → `extract_claims()`:

```
Given this YouTube video transcript, extract 3–10 SPECIFIC factual claims that can be verified.
Focus on: numerical stats, dates, historical events, scientific facts, technical specs, named entities.
Ignore opinions, predictions, or subjective statements.

Format as JSON array:
[{"claim": "...", "type": "numerical|historical|scientific|technical|other"}]

Title: {title}
Transcript (first 3000 chars):
{transcript[:3000]}
```

---

## Verification Prompt

Used in `fact-check-pipeline.py` → `verify_claim()`:

```
Given:
- CLAIM: "{claim}"
- WEB SEARCH RESULTS (first 5 DuckDuckGo snippets):

{snippet_text}

Respond with ONLY JSON:
{"verdict": "supported|contradicted|disputed|unverifiable", "confidence": 0-100, "note": "..."}
```

---

## Video Scoring Logic

```
if contradicted >= 2: verdict = INCORRECT
elif contradicted == 1 or disputed >= 2: verdict = CAUTION
elif supported >= total - (unverifiable + errors): verdict = VERIFIED
else: verdict = PARTIAL

score = supported / max(total - unverifiable - errors, 1)
```

---

## Auto-Skip Keyword List

Defined in `rss-aggregator.py`; mirrored in `fact-check-pipeline.py` as a guard:

`short`, `shorts`, `music`, `song`, `album`, `lyric`, `meme`, `ytp`, `youtube poop`,
`reaction`, `react`, `prank`, `skit`, `gaming`, `let's play`, `gameplay`,
`vlog`, `stream`, `live`, `podcast`, `electronic`, `techno`, `dubstep`, `remix`, `asmr`

---

## Configuration

| Variable | Default | Location |
|---|---|---|
| LM Studio URL | `http://192.168.0.195:1234` | `fact-check-pipeline.py` |
| LM Studio Model | `qwen/qwen3.6-35b-a3b` | `fact-check-pipeline.py` |
| Max videos / run | 20 | `fact-check-pipeline.py` (`urls[:20]`) |
| Max videos / channel / day | 2 | `rss-aggregator.py` |
| Total cap / day | 30 | `rss-aggregator.py` |
| Chromium path | `/usr/bin/chromium` | `scrape-subscriptions.js` (unused in nightly path) |
| Chrome profile | `~/.config/chromium/Default` | `scrape-subscriptions.js` (unused in nightly path) |

---

## Notes

- The nightly cron path is: `cron → ~/.hermes/scripts/run-yt-factcheck.sh → run-nightly.sh`
- LM Studio runs inference locally (no API costs); model quality is high (Qwen 35B).
- DuckDuckGo scraping has no rate-limiting for this use volume (~160 GETs/night).
- The system skips videos without transcripts by design (~30–40% of uploads have no auto-captions).
- `yt-subscriptions-rss.txt` (shown in an earlier draft of README) is not used — removed to avoid confusion. RSS goes straight to the aggregator.
