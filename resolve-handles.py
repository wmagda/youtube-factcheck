#!/usr/bin/env python3
"""
Resolve @handles to UC channel IDs by scraping the channel page canonical URL.

Input:  ~/Projects/youtube-factcheck/yt-channel-ids.json  (with @handle idOrHandle)
Output: ~/Projects/youtube-factcheck/yt-channel-ids-resolved.json  (with idOrHandle = UC...)
        ~/Projects/youtube-factcheck/handle-cache.json          (handle → UC map)
"""

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

CHANNELS_FILE = Path('/home/wojtek/Projects/youtube-factcheck/yt-channel-ids.json')
OUTPUT_FILE   = Path('/home/wojtek/Projects/youtube-factcheck/yt-channel-ids-resolved.json')
CACHE_FILE    = Path('/home/wojtek/Projects/youtube-factcheck/handle-cache.json')
DELAY         = 0.5   # seconds between requests (polite)

def resolve_handle(handle: str) -> str | None:
    """Fetch youtube.com/@handle, extract canonical channel URL, return UC... ID."""
    url = f'https://www.youtube.com/{handle}'
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f'  [FAIL] {handle}: {e}', file=sys.stderr)
        return None

    # Try canonical link first: <link rel="canonical" href="https://www.youtube.com/channel/UC...">
    m = re.search(r'<link[^>]+rel="canonical"[^>]+href="[^"]+/channel/([^"/?#]+)"', html, re.I)
    if m:
        return m.group(1)

    # Fallback: look for "channelId" in page JSON
    m = re.search(r'"channelId"\s*:\s*"(UC[^"]{20,})"', html)
    if m:
        return m.group(1)

    # Last resort: externalId attribute (rare)
    m = re.search(r'"externalId"\s*:\s*"(UC[^"]{20,})"', html)
    if m:
        return m.group(1)

    print(f'  [MISS] {handle}: could not find UC ID in page', file=sys.stderr)
    return None


def main():
    print(f'Resolving @handles to UC... channel IDs — {time.strftime("%Y-%m-%d %H:%M")}')
    channels = json.loads(CHANNELS_FILE.read_text())
    print(f'Loaded {len(channels)} channels')

    # Load existing cache
    cache: dict[str, str] = {}
    if CACHE_FILE.exists():
        cache.update(json.loads(CACHE_FILE.read_text()))
        print(f'Loaded {len(cache)} cached resolutions')

    unresolved = [c for c in channels if c['idOrHandle'].startswith('@') and c['idOrHandle'] not in cache]
    resolved_uc = [c for c in channels if c['idOrHandle'].startswith('UC')]

    print(f'Need to resolve: {len(unresolved)}')

    for i, ch in enumerate(unresolved):
        handle = ch['idOrHandle']
        print(f'  [{i+1}/{len(unresolved)}] {handle} → ', end='', flush=True)
        uc_id = resolve_handle(handle)
        if uc_id:
            cache[handle] = uc_id
            ch['idOrHandleResolved'] = uc_id
            print(uc_id)
        else:
            ch['idOrHandleResolved'] = None
            print('FAILED')
        time.sleep(DELAY)

    # Write output: channels uc-primary if resolved, otherwise keep original idOrHandle
    final: list[dict] = []
    for ch in channels:
        entry = {**ch}
        uc = ch.get('idOrHandleResolved') or (ch['idOrHandle'] if ch['idOrHandle'].startswith('UC') else None)
        if uc:
            entry['idOrHandle'] = uc   # replace @handle with UC... for RSS
        final.append(entry)

    OUTPUT_FILE.write_text(json.dumps(final, indent=2))
    CACHE_FILE.write_text(json.dumps(cache, indent=2))

    total_uc = sum(1 for c in final if c['idOrHandle'].startswith('UC'))
    print(f'\n✓ Wrote {OUTPUT_FILE}  ({total_uc}/{len(final)} have UC IDs)')
    print(f'✓ Wrote {CACHE_FILE}  ({len(cache)} handle→UC mappings cached)')


if __name__ == '__main__':
    main()
