#!/usr/bin/env python3
"""
YouTube Fact-Check — one video, full transcript, no chunking.

Usage:
  python3 fact-check-one.py <youtube_url>

Strategy:
  1. Fetch full transcript (translate to English when possible, otherwise use raw)
  2. Single LLM call: extract up to 8 verifiable claims from the full text
  3. Sequential per-claim verification (DuckDuckGo + LLM verdict)
  4. Print verdict summary
"""

import json, os, re, sys, urllib.request, urllib.parse, time
from pathlib import Path
from youtube_transcript_api import (
    YouTubeTranscriptApi, NoTranscriptFound,
    TranscriptList, FetchedTranscriptSnippet
)

# ── .env ──────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ─────────────────────────────────────────────────────────────────
LMSTUDIO_URL    = 'http://192.168.0.195:1234'
LMSTUDIO_MODEL  = 'qwen/qwen3.6-35b-a3b'
LMSTUDIO_TOKEN  = os.environ.get('LMSTUDIO_TOKEN', '')
EXTRACT_TIMEOUT = 600
VERIFY_TIMEOUT  = 300
SEARCH_TIMEOUT  = 15
MAX_CLAIMS      = 8


# ── Transcript ─────────────────────────────────────────────────────────────

def get_transcript(url: str) -> tuple[str | None, str]:
    """Return (transcript_text, video_id) or (None, vid).

    Fallback chain:
    1. English (manual or auto-generated, translated if needed)
    2. Any auto-generated transcript translated to English
    3. Any available transcript raw + language tag prefix
    """
    # Normalise URL → video ID
    if 'youtu.be/' in url:
        m = re.search(r'youtu\.be/([a-zA-Z0-9_-]+)', url)
    else:
        m = re.search(r'v=([a-zA-Z0-9_-]+)', url)
    if not m:
        return None, ''

    vid = m.group(1)
    yt  = YouTubeTranscriptApi()

    # 1. Explicit English — manual or auto-generated, both work
    try:
        fetched = yt.fetch(vid, languages=['en'])
        return ' '.join(s.text for s in fetched.snippets), vid
    except NoTranscriptFound:
        pass

    # 2. Auto-generate (any language) → translate to English
    try:
        for tr in yt.list(vid, languages=None):
            if tr.is_generated and tr.is_translatable:
                en = tr.translate('en')              # transcript stub, not yet fetched
                snippets = en.fetch()                # actually fetch the translated text
                return f'[translated from {tr.language_code}] ' + \
                       ' '.join(s.text for s in snippets), vid
    except Exception:
        pass

    # 3. Any available transcript (may be non-English, language-tagged)
    try:
        for tr in yt.list(vid, languages=None):
            snippets = tr.fetch()                  # fetch in its native language
            return f'[{tr.language_code}] ' + \
                   ' '.join(s.text for s in snippets), vid
    except Exception:
        pass

    return None, vid


# ── LLM ─────────────────────────────────────────────────────────────────────

def call_llm(prompt: str, max_tokens: int = 1024, timeout: int = 300) -> str:
    payload = json.dumps({
        'model': LMSTUDIO_MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.3,
        'max_tokens': max_tokens,
    }).encode()

    headers = {'Content-Type': 'application/json'}
    if LMSTUDIO_TOKEN:
        headers['Authorization'] = f'Bearer {LMSTUDIO_TOKEN}'
    req = urllib.request.Request(
        f'{LMSTUDIO_URL}/v1/chat/completions',
        data=payload, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            msg  = data['choices'][0]['message']
            return msg.get('content', '') or msg.get('reasoning_content', '') or ''
    except Exception as e:
        return f'[LLM ERROR] {e}'


# ── Search ──────────────────────────────────────────────────────────────────

def search(query: str) -> list[str]:
    try:
        url = f'https://duckduckgo.com/html/?q={urllib.parse.quote(query)}&kl=us-en'
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
            html = resp.read().decode()
        snippets = re.findall(r'class="result__snippet">(.*?)</a>', html, re.DOTALL)
        return [re.sub(r'<[^>]+>', '', s).strip() for s in snippets][:5]
    except Exception as e:
        return [f'[SEARCH ERROR] {e}]']


# ── Verdict ─────────────────────────────────────────────────────────────────

def verify(claim: str) -> dict:
    results  = search(claim)
    ctx      = '\n'.join(f'- {r}' for r in results) or 'No results found.'
    prompt   = (
        'You are a neutral fact-checker. Decide whether the WEB RESULTS SUPPORT, '
        'CONTRADICT, DISPUTE, or leave the CLAIM UNVERIFIABLE.\n\n'
        f'CLAIM: "{claim}"\n\nRESULTS:\n{ctx}\n\n'
        'Reply with ONLY valid JSON on one line, no markdown fences, no extra text:\n'
        '{"verdict":"supported|contradicted|disputed|unverifiable",'
        '"confidence":0-100,'
        '"note":"one-sentence explanation"}'
    )
    raw = call_llm(prompt, max_tokens=1024, timeout=VERIFY_TIMEOUT)
    m   = re.search(r'\{.+?\}', raw, re.DOTALL)
    if not m:
        return {'verdict': 'error', 'confidence': 0, 'note': f'no-json: {raw[:120]}'}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {'verdict': 'error', 'confidence': 0, 'note': 'json-decode-error'}


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(f'Usage: {sys.argv[0]} <youtube_url>')
        sys.exit(1)

    url = sys.argv[1]
    print(f'▶  Fetching transcript: {url}')
    result = get_transcript(url)
    if not result or not result[0]:
        print('No transcript available for this video.')
        sys.exit(1)
    transcript, vid = result
    src_tag = transcript[:25] if transcript.startswith('[') else 'English'
    print(f'   Got {len(transcript)} chars  [{src_tag}]  (ID: {vid})')

    # ── Claim extraction — one LLM call, full transcript ───────────────────
    print('\n▶  Extracting claims (single LLM call, full transcript in context)...')
    t0 = time.time()
    raw = call_llm(
        f'Given this full video transcript, extract up to {MAX_CLAIMS} SHORT, '
        f'SPECIFIC, VERIFIABLE factual claims (numbers, dates, names, specs, events).\n\n'
        f'IGNORE: opinions, jokes, predictions, "the speaker says", "the video argues".\n\n'
        f'OUTPUT — strict JSON array, no markdown fences, no extra text:\n'
        f'[{{"claim":"claim text here","type":"type"}}]\n\n'
        f'=== TRANSCRIPT ===\n{transcript}',
        max_tokens=2048,
        timeout=EXTRACT_TIMEOUT,
    )
    print(f'   Extraction done in {time.time()-t0:.0f}s')

    # ── Parse claims ────────────────────────────────────────────────────────
    claims = []
    for pattern in [r'\[.*?\]', r'\{.*?\}']:
        m = re.search(pattern, raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
                if isinstance(parsed, list):
                    claims = [
                        c for c in parsed
                        if isinstance(c, dict)
                        and 'claim' in c
                        and isinstance(c['claim'], str)
                        and len(c['claim']) > 25
                    ]
                    break
            except json.JSONDecodeError:
                continue

    if not claims:
        print('\n   ⚠️  Could not extract verifiable claims. Raw output:')
        print(raw[:500])
        sys.exit(0)

    print(f'   ✓ {len(claims)} claim(s):\n')
    for i, c in enumerate(claims, 1):
        print(f'   {i}. [{c.get("type","?")}] {c["claim"][:90]}')

    # ── Per-claim verification ───────────────────────────────────────────────
    print(f'\n▶  Verifying {len(claims)} claim(s)...\n')
    verifications = []
    for i, claim in enumerate(claims, 1):
        label = claim['claim'][:75]
        print(f'   [{i}/{len(claims)}] {label}')
        v     = verify(claim['claim'])
        verifications.append({**claim, **v})
        icon  = {'supported':'✅','contradicted':'❌','disputed':'⚠️',
                  'unverifiable':'⏭️','error':'❓'}.get(v.get('verdict',''), '❓')
        conf  = v.get('confidence', 0)
        note  = v.get('note', '')
        print(f'       {icon} {v.get("verdict","?"):12s}  [{conf:3d}%]  {note[:100]}')

    # ── Score ───────────────────────────────────────────────────────────────
    n = len(verifications)
    supported    = sum(1 for v in verifications if v['verdict'] == 'supported')
    contradicted = sum(1 for v in verifications if v['verdict'] == 'contradicted')
    disputed     = sum(1 for v in verifications if v['verdict'] == 'disputed')
    unverifiable = sum(1 for v in verifications if v['verdict'] == 'unverifiable')
    errors       = sum(1 for v in verifications if v['verdict'] == 'error')

    usable    = n - unverifiable - errors
    net_score = supported / max(usable, 1)

    if contradicted >= 2:
        verdict = '❌ LIKELY INCORRECT'
    elif contradicted + disputed >= 2:
        verdict = '⚠️  CAUTION'
    elif supported >= max(1, usable // 2):
        verdict = '✅ LIKELY ACCURATE'
    else:
        verdict = '⚠️  PARTIALLY VERIFIED'

    # ── Summary ─────────────────────────────────────────────────────────────
    print(f'\n{"="*60}')
    print(f'Claims checked:   {n}')
    print(f'   ✅ Supported:    {supported}')
    print(f'   ❌ Contradicted: {contradicted}')
    print(f'   ⚠️  Disputed:     {disputed}')
    print(f'   ⏭️  Unverifiable: {unverifiable}')
    print(f'   ❓ Errors:       {errors}')
    print(f'\nNet score: {net_score:.0%}  (only {usable} usable claim(s))')
    print(f'\nVerdict: {verdict}')


if __name__ == '__main__':
    main()
