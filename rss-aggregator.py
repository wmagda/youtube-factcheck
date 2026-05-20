#!/usr/bin/env python3
"""
RSS subscription aggregator for YouTube channels.

Reads:  ~/Projects/youtube-factcheck/yt-channel-ids.json
Writes: ~/Projects/youtube-factcheck/yt-subscriptions-today.txt  (filtered URLs)

For each channel, fetches the YouTube RSS feed, grabs the most recent video URL,
applies auto-skip keyword filtering, and outputs up to N URLs per channel.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ['HOME'])
CHANNELS_FILE = HOME / 'Projects/youtube-factcheck/yt-channel-ids.json'
OUTPUT_FILE = HOME / 'Projects/youtube-factcheck/yt-subscriptions-today.txt'
MAX_PER_CHANNEL = 2   # how many recent videos per channel to include
MAX_TOTAL = 30         # hard cap on total output

SKIP_PATTERNS = [
    r'short', r'shorts',
    r'music', r'song', r'album', r'lyric',
    r'meme', r'ytp', r'youtube poop',
    r'reaction', r'react',
    r'prank', r'skit',
    r'gaming', r"let'?s play", r'gameplay',
    r'vlog', r'stream', r'live',
    r'podcast',
    r'electronic|techno|dubstep|remix',
    r'asmr'
]


def should_skip_title(title: str) -> bool:
    text = title.lower()
    return any(re.search(pat, text) for pat in SKIP_PATTERNS)


def fetch_videos_from_rss(channel_id: str, channel_name: str) -> list[str]:
    """Return list of recent video URLs from a channel's RSS feed."""
    rss_url = f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'
    urls = []
    try:
        req = urllib.request.Request(
            rss_url,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; yt-factcheck/1.0)'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f'  [RSS ERROR] {channel_name}: {e}', file=sys.stderr)
        return []

    # Extract <yt:videoId> entries and <media:title> entries
    video_ids = re.findall(r'<yt:videoId>([^<]+)</yt:videoId>', xml)
    titles = re.findall(r'<media:title[^>]*>([^<]+)</media:title>', xml)

    # Pair them (RSS gives them in order)
    for vid, title in zip(video_ids, titles):
        if len(urls) >= MAX_PER_CHANNEL:
            break
        if should_skip_title(title):
            continue
        urls.append(f'https://www.youtube.com/watch?v={vid}')

    return urls


def main():
    print(f'YouTube RSS Aggregator — {datetime.now().strftime("%Y-%m-%d %H:%M")}')

    if not CHANNELS_FILE.exists():
        print(f'ERROR: {CHANNELS_FILE} not found. Run the channel scrape first.', file=sys.stderr)
        sys.exit(1)

    channels = json.loads(CHANNELS_FILE.read_text())
    print(f'Loaded {len(channels)} channels')

    all_urls: list[str] = []
    seen: set[str] = set()
    stats = {'total_raw': 0, 'skipped_ent': 0, 'skipped_dup': 0, 'output': 0}

    for ch in channels:
        ch_id = ch.get('idOrHandle') or ch.get('id')
        ch_name = ch.get('name', 'Unknown')
        if not ch_id or ch_id.startswith('@'):
            # @handle works too in RSS; but store/pull by ID for reliability
            ch_id = ch.get('link', '').rstrip('/').split('/')[-1]

        urls = fetch_videos_from_rss(ch_id, ch_name)
        stats['total_raw'] += len(urls)

        for url in urls:
            if url in seen:
                stats['skipped_dup'] += 1
                continue
            seen.add(url)
            # Guard: double-check skip on title by fetching page title? Skip for speed; trust RSS title
            all_urls.append(url)
            if len(all_urls) >= MAX_TOTAL:
                break
        if len(all_urls) >= MAX_TOTAL:
            break

    stats['output'] = len(all_urls)

    # Write output
    OUTPUT_FILE.write_text('\n'.join(all_urls) + '\n')

    print(f'Videos collected:  {stats["total_raw"]} raw')
    print(f'Skipped dupes:     {stats["skipped_dup"]}')
    print(f'Final output:      {stats["output"]} URLs → {OUTPUT_FILE}')


if __name__ == '__main__':
    main()
