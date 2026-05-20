#!/usr/bin/env python3
"""
YouTube Fact-Check Pipeline

Reads: ~/Projects/youtube-factcheck/yt-subscriptions-today.txt
One video at a time:
  1. Fetches transcript (youtube-transcript-api, 3 min timeout)
  2. Extracts factual claims via LM Studio (600s timeout — local GPU)
  3. Verifies each claim via DuckDuckGo + LM Studio (300s each)
  4. Scores video: ACCURATE / PARTIAL / CAUTION / INCORRECT / UNKNOWN

Outputs:
  ~/Projects/youtube-factcheck/fact-check-report.txt   (human-readable)
  ~/Projects/youtube-factcheck/channel-scores.jsonl     (append-only history)
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# ── .env fallback (loaded here so direct `python3` runs work) ──────────────
_env_path = Path(__file__).parent / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ─────────────────────────────────────────────────────────────────
HOME            = Path(os.environ['HOME'])
INPUT_FILE      = HOME / 'Projects/youtube-factcheck/yt-subscriptions-today.txt'
REPORT_FILE     = HOME / 'Projects/youtube-factcheck/fact-check-report.txt'
SCORES_FILE     = HOME / 'Projects/youtube-factcheck/channel-scores.jsonl'
LMSTUDIO_URL    = 'http://192.168.0.195:1234'
LMSTUDIO_MODEL  = 'qwen/qwen3.6-35b-a3b'
LMSTUDIO_TOKEN  = os.environ.get('LMSTUDIO_TOKEN', '')
TRANSCRIPT_MAX  = 1500          # chars of transcript sent to LLM (keep it short)
CLAIMS_MAX      = 6             # max claims to extract per video
VERIFY_TIMEOUT  = 300           # per-claim verification timeout
EXTRACT_TIMEOUT = 600           # claim extraction timeout (local GPU inference)

# ── Auto-skip patterns ─────────────────────────────────────────────────────
SKIP_PATTERNS = [
    re.compile(r'short',                    re.I),
    re.compile(r'music|music video|mv',     re.I),
    re.compile(r'song|sing',               re.I),
    re.compile(r'album',                   re.I),
    re.compile(r'lyric',                   re.I),
    re.compile(r'meme',                    re.I),
    re.compile(r'ytp',                     re.I),
    re.compile(r'youtube poop',            re.I),
    re.compile(r'reaction',                re.I),
    re.compile(r'react',                   re.I),
    re.compile(r'prank',                   re.I),
    re.compile(r'skit',                    re.I),
    re.compile(r'gaming',                  re.I),
    re.compile(r'let.?s play',             re.I),
    re.compile(r'gameplay',                re.I),
    re.compile(r'vlog',                    re.I),
    re.compile(r'stream',                  re.I),
    re.compile(r'podcast',                 re.I),
    re.compile(r'edm|electronic|techno|dubstep|remix|festival', re.I),  # note: dropped 'bass' and 'rave' — too many false positives
    re.compile(r'dance|dancing',          re.I),
    re.compile(r'asmr',                    re.I),
]


# ── Helpers ────────────────────────────────────────────────────────────────

def get_title(url: str) -> str:
    """Fetch the actual YouTube video title for skip-filtering."""
    m = re.search(r'v=([a-zA-Z0-9_-]+)', url)
    if not m:
        m = re.search(r'youtu\.be/([a-zA-Z0-9_-]+)', url)
    if not m:
        return ''
    vid = m.group(1)
    try:
        oembed_url = f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json'
        req = urllib.request.Request(
            oembed_url,
            headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data  = json.loads(resp.read().decode())
            title = data.get('title', '')
            return title.strip()
    except Exception:
        return ''


def shouldSkip(title: str) -> bool:
    """Return True if title matches any auto-skip keyword."""
    return any(p.search(title) for p in SKIP_PATTERNS)


def log(msg: str):
    print(msg, flush=True)


def run(cmd: str, timeout: int = 120) -> str:
    """Run shell command, return stdout on success."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            log(f'  [CMD ERROR] {cmd}\n  {result.stderr[:300]}')
            return ''
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log(f'  [TIMEOUT] {cmd}')
        return ''
    except Exception as e:
        log(f'  [EXCEPTION] {e}')
        return ''


# ── Transcript ─────────────────────────────────────────────────────────────

def get_transcript(url: str) -> str | None:
    """Fetch transcript via youtube-transcript-api."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        video_id_match = re.search(r'v=([a-zA-Z0-9_-]+)', url)
        if not video_id_match:
            log(f'  Cannot extract video ID from: {url}')
            return None
        video_id = video_id_match.group(1)

        yt = YouTubeTranscriptApi()
        fetched = yt.fetch(video_id)
        return ' '.join(s.text for s in fetched.snippets)
    except Exception as e:
        log(f'  No transcript: {e}')
        return None


# ── LM Studio ──────────────────────────────────────────────────────────────

def call_lmstudio(prompt: str, temperature: float = 0.3, max_tokens: int = 2048,
                  timeout: int = VERIFY_TIMEOUT) -> dict:
    """Call local LM Studio OpenAI-compatible endpoint.

    Always returns a dict:  {content, reasoning_content, finish_reason}
    On error returns:       {content: '', reasoning_content: '', finish_reason: 'error'}
    """
    payload = json.dumps({
        'model': LMSTUDIO_MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }).encode('utf-8')

    headers = {'Content-Type': 'application/json'}
    if LMSTUDIO_TOKEN:
        headers['Authorization'] = f'Bearer {LMSTUDIO_TOKEN}'
    req = urllib.request.Request(
        f'{LMSTUDIO_URL}/v1/chat/completions',
        data=payload,
        headers=headers
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            msg = data['choices'][0]['message']
            return {
                'content':          msg.get('content', '').strip(),
                'reasoning_content': msg.get('reasoning_content', '').strip(),
                'finish_reason':    data['choices'][0].get('finish_reason', ''),
            }
    except Exception as e:
        log(f'  [LMSTUDIO ERROR] {e}')
        return {'content': '', 'reasoning_content': '', 'finish_reason': 'error'}


def _extract_text(resp: dict) -> str:
    """Best available text from an LLM response (content first, then reasoning)."""
    return resp.get('content', '') or resp.get('reasoning_content', '')


def _strip_preamble(text: str) -> str:
    """Remove common LLM thinking preambles before parsed JSON output."""
    markers = [
        "Here's a thinking process:",
        "Here's my thinking:",
        "```json", "```",
        "Here's the JSON:",
        'As requested, here is the JSON:',
    ]
    for m in markers:
        idx = text.find(m)
        if idx != -1:
            text = text[text.find('\n', idx) + 1:]
            break
    return text


def _last_json_array(text: str) -> list:
    """Extract the *last* JSON array found in a text block."""
    # Strip preamble then find the last complete [ … ]
    text = _strip_preamble(text)
    pos = text.rfind(']')
    while pos > 0:
        candidate = text[:pos + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pos = text.rfind(']', 0, pos - 1)
    return []


def _first_json_block(text: str) -> str | None:
    """Extract the first balanced {...} block from text, skipping preambles."""
    text = _strip_preamble(text)
    depth = 0
    start = None
    for i, c in enumerate(text):
        if c == '{':
            if depth == 0:
                start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start is not None:
                return text[start:i + 1]
    return None


def _extract_json(text: str) -> dict | None:
    """Try multiple strategies to extract a JSON object from LLM output."""
    # Strategy 1: first balanced {...} block
    block = _first_json_block(text)
    if block:
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass

    # Strategy 2: last balanced {...} block
    pos = text.rfind('}')
    while pos > 0:
        candidate = text[pos:]
        depth = 0
        for i in range(len(candidate) - 1, -1, -1):
            if candidate[i] == '}':
                depth += 1
            elif candidate[i] == '{':
                depth -= 1
                if depth == 0:
                    candidate = candidate[i:]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        pass
                    break
        pos = text.rfind('}', 0, pos - 1)

    # Strategy 3: try parsing the whole text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    return None


# ── Claim extraction ───────────────────────────────────────────────────────

def extract_claims(transcript: str, title: str) -> list[dict]:
    """Use LLM to extract ONLY suspicious factual claims from a transcript."""
    prompt = f"""You are a skeptical fact-checker. From this YouTube video transcript, extract up to {CLAIMS_MAX} factual claims that are SUSPICIOUS — i.e., claims that seem potentially false, misleading, or worth verifying.

Focus on claims that:
- Make specific, verifiable assertions (numbers, dates, names, events, technical specs)
- Could be wrong, exaggerated, or taken out of context
- Would be worth double-checking against web sources

IGNORE:
- Opinions, predictions, subjective statements
- "The video argues/says" framing
- Vague or untestable statements
- Things that are clearly true by definition
- Hypothetical scenarios ("imagine if", "what if", "suppose", "let's say")
- Made-up names for illustrative examples (e.g., "Bob went to the store")
- Fictional stories or thought experiments
- Clearly fictional or satirical content

OUTPUT — strict JSON array, NOTHING ELSE. No explanation, no preamble, no markdown:
[
  {{"claim": "exact factual claim text", "type": "numerical|historical|scientific|technical|entity"}},
  {{"claim": "...", "type": "..."}}
]

Title: {title}
Transcript (first {TRANSCRIPT_MAX} chars):
{transcript[:TRANSCRIPT_MAX]}"""

    resp  = call_lmstudio(prompt, temperature=0.2, max_tokens=4096, timeout=EXTRACT_TIMEOUT)
    text  = _extract_text(resp)

    claims = _last_json_array(text)
    return [
        c for c in claims
        if isinstance(c, dict) and 'claim' in c
        and isinstance(c['claim'], str) and len(c['claim']) > 25
    ]


# ── Per-claim verification ─────────────────────────────────────────────────

def verify_claim(claim: str) -> dict:
    """Verify one claim via DuckDuckGo HTML scrape + LM Studio synthesis."""
    # Step 1 — web search
    try:
        query     = urllib.parse.quote(claim)
        search_url = f'https://duckduckgo.com/html/?q={query}&kl=us-en'
        req = urllib.request.Request(
            search_url,
            headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
    except Exception as e:
        log(f'    [SEARCH FAILED] {e}')
        return {'verdict': 'error', 'confidence': 0, 'note': f'search failed: {e}'}

    snippets  = re.findall(r'class="result__snippet">(.*?)</a>', html, re.DOTALL)
    snippets  = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets][:5]
    snippets_text = '\n'.join(f'- {s}' for s in snippets if s) or 'No readable results found.'

    # Step 2 — LLM judges match
    prompt = f"""You are a neutral fact-checker. Decide whether the WEB SEARCH RESULTS SUPPORT, CONTRADICT, DISPUTE, or leave the CLAIM UNVERIFIABLE.

CLAIM:
"{claim}"

WEB SEARCH RESULTS:
{snippets_text}

IMPORTANT: If this claim is from a hypothetical scenario ("imagine if", "what if", "suppose"), a made-up example with fictional names, a thought experiment, or clearly fictional content — classify it as "hypothetical" and explain why.

Reply with ONLY this JSON object. NO preamble, NO explanation, NO markdown code blocks:
{{"verdict": "supported|contradicted|disputed|unverifiable|hypothetical", "confidence": 0-100, "note": "one-sentence explanation"}}"""

    resp = call_lmstudio(prompt, temperature=0.1, max_tokens=1024, timeout=VERIFY_TIMEOUT)
    text = _extract_text(resp)
    result = _extract_json(text)
    if not result:
        log(f'    [WARN] No verdict JSON; raw: {text[:120]}')
        return {'verdict': 'error', 'confidence': 0, 'note': 'json parse error'}

    return result


# ── Scoring ────────────────────────────────────────────────────────────────

def score_video(claims: list[dict], verifications: list[dict]) -> dict:
    """Aggregate per-claim verdicts into an overall video reliability score."""
    if not claims:
        return {'verdict': 'UNKNOWN', 'score': 0.0, 'note': 'No verifiable claims'}

    verdicts = [v.get('verdict', '') for v in verifications]
    supported   = verdicts.count('supported')
    contradicted = verdicts.count('contradicted')
    disputed    = verdicts.count('disputed')
    unverifiable = verdicts.count('unverifiable')
    errors      = verdicts.count('error')
    hypothetical = verdicts.count('hypothetical')
    total       = len(verifications)

    # Hypothetical claims don't count toward the score
    verifiable_total = max(total - unverifiable - errors - hypothetical, 1)

    if contradicted >= 2:
        verdict = 'INCORRECT'
    elif contradicted + disputed >= 2:
        verdict = 'CAUTION'
    elif supported >= verifiable_total:
        verdict = 'ACCURATE'
    else:
        verdict = 'PARTIAL'

    return {
        'verdict':       verdict,
        'score':         supported / verifiable_total,
        'supported':     supported,
        'contradicted':  contradicted,
        'disputed':      disputed,
        'unverifiable':  unverifiable,
        'hypothetical':  hypothetical,
        'errors':        errors,
        'total':         total,
    }


# ── Output helpers ─────────────────────────────────────────────────────────

def append_score(url: str, title: str, channel: str, date: str, score: dict):
    """Append one line to the rolling channel-scores JSONL file."""
    record = {
        'date': date, 'url': url, 'title': title, 'channel': channel,
        'verdict': score['verdict'], 'score': score['score'],
        'detail': score,
    }
    with open(SCORES_FILE, 'a') as f:
        f.write(json.dumps(record) + '\n')


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    log('==========================================')
    log(f'  YouTube Fact-Check Pipeline — {datetime.now():%Y-%m-%d %H:%M}')
    log('==========================================')

    date_str = datetime.now().strftime('%Y-%m-%d')
    urls     = [u.strip() for u in INPUT_FILE.read_text().splitlines() if u.strip()]

    if not urls:
        log('No URLs found — stopping.')
        REPORT_FILE.write_text('No URLs found in input file.')
        return

    log(f'Input: {len(urls)} URLs from {INPUT_FILE.name}')
    log(f'Claim extraction: {CLAIMS_MAX} max each, {TRANSCRIPT_MAX} char transcript')
    log(f'Timeouts: extraction={EXTRACT_TIMEOUT}s  verification={VERIFY_TIMEOUT}s\n')

    report_lines = [
        f'YouTube Fact-Check Report — {date_str}',
        '=' * 60,
        f'Config: {CLAIMS_MAX} claims/video | {TRANSCRIPT_MAX}-char transcripts',
        f'US EXTRACT_TIMEOUT={EXTRACT_TIMEOUT}s  VERIFY_TIMEOUT={VERIFY_TIMEOUT}s',
        '',
    ]
    processed = 0

    for url in urls[:20]:     # hard cap at 20 per run
        log(f'\n--- {url} ---')
        real_title = get_title(url)   # fetch actual YouTube title before anything else
        title_guess = real_title or f'Video {re.search(r"v=([a-zA-Z0-9_-]+)", url).group(1) if re.search(r"v=([a-zA-Z0-9_-]+)", url) else "unknown"}'
        channel_guess = 'unknown'

        # 0. Title guard — skip non-factual content immediately, before transcript fetch
        if shouldSkip(title_guess):
            report_lines.append(f'⏭️  SKIP\n  URL: {url}\n  Reason: auto-skipped ("{title_guess}")\n')
            log(f'  Skipped — title match: "{title_guess}"')
            processed += 1
            continue

        # 1. Transcript
        transcript = get_transcript(url)
        if not transcript:
            report_lines.append(f'⏭️  SKIP\n  URL: {url}\n  Reason: No transcript available\n')
            log('  Skipped — no transcript')
            processed += 1
            continue

        # 2. Extract claims
        claims = extract_claims(transcript, title_guess)
        if not claims:
            report_lines.append(f'⚠️  NO CLAIMS\n  URL: {url}\n  No verifiable facts extracted.\n')
            append_score(url, title_guess, channel_guess, date_str,
                         {'verdict': 'UNKNOWN', 'score': 0.0, 'note': 'no_claims'})
            processed += 1
            continue

        log(f'  ✓ Found {len(claims)} claims')

        # 4. Verify each claim (sequential — no parallel LLM calls)
        verifications = []
        for claim in claims[:CLAIMS_MAX]:
            log(f'    Verifying: {claim["claim"][:80]}...')
            v = verify_claim(claim['claim'])
            verifications.append({**claim, **v})

        # 5. Score
        avg_confidence = sum(v.get('confidence', 0) for v in verifications) / max(len(verifications), 1)
        video_score    = score_video(claims, verifications)
        icons = {'ACCURATE': '✅', 'PARTIAL': '⚠️', 'CAUTION': '⚠️',
                 'INCORRECT': '❌', 'UNKNOWN': '⏭️'}
        verdict_icon = icons.get(video_score['verdict'], '❓')

        # 6. Build report block
        block = (
            f"{verdict_icon}  {video_score['verdict']} — {video_score['score']:.0%}\n"
            f"  URL: {url}\n"
            f"  Claims: {len(verifications)} verified (of {len(claims)} extracted)\n"
            f"  Supported: {video_score.get('supported',0)} | "
            f"Contradicted: {video_score.get('contradicted',0)} | "
            f"Unverifiable: {video_score.get('unverifiable',0)} | "
            f"Hypothetical: {video_score.get('hypothetical',0)} | "
            f"Avg confidence: {avg_confidence:.0f}%\n"
        )
        for v in verifications:
            icon = {'supported': '✅', 'contradicted': '❌', 'disputed': '⚠️',
                    'unverifiable': '⏭️', 'hypothetical': '🧪', 'error': '❓'}.get(v['verdict'], '❓')
            block += f"    {icon} [{v.get('confidence',0)}%] {v['claim'][:90]}\n"
            note = v.get('note', '')
            if note:
                block += f"        {note[:120]}\n"

        report_lines.append(block + '\n')
        append_score(url, title_guess, channel_guess, date_str,
                     {**video_score, 'avg_confidence': avg_confidence})
        processed += 1
        log(f"  Verdict: {video_score['verdict']}")

    # ── Final report ──────────────────────────────────────────────────────
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    report_lines.insert(3, f'{processed} video(s) processed this run\n')
    report_lines.append('\nHistory (per-video JSON): channel-scores.jsonl')
    REPORT_FILE.write_text('\n'.join(report_lines))

    log(f'\n✓ Done — {processed} video(s) written to {REPORT_FILE}')
    log(f'✓ Scores appended to {SCORES_FILE}')


if __name__ == '__main__':
    main()
