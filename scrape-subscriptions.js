#!/usr/bin/env node

const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const CHROME_PROFILE = path.join(
  process.env.HOME,
  '.config/chromium/Default'
);
const CHROMIUM_PATH = '/usr/bin/chromium';

const OUTPUT_DIR = path.join(process.env.HOME, 'Documents/youtube-factcheck');
const CHANNELS_FILE = path.join(OUTPUT_DIR, 'yt-channel-ids.json');
const VIDEOS_FILE = path.join(OUTPUT_DIR, 'yt-subscriptions-today.txt');

// Auto-skip keywords in titles
const SKIP_KEYWORDS = [
  /short/i, /shorts/i,
  /music/i, /song/i, /album/i, /lyrics?/i,
  /meme/i, /ytp/i, /youtube poop/i,
  /reaction/i, /react/i,
  /prank/i, /skit/i,
  /gaming/i, /let'?s play/i, /gameplay/i,
  /vlog/i, /stream/i, /live/i,
  /podcast/i,
  /dubstep|edm|techno|electronic|remix/i,
  /asmr/i
];

function shouldSkip(title) {
  return SKIP_KEYWORDS.some(re => re.test(title));
}

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function scrapeSubscriptions() {
  const browser = await puppeteer.launch({
    executablePath: CHROMIUM_PATH,
    userDataDir: CHROME_PROFILE,
    headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage']
  });

  const page = await browser.newPage();
  
  // Set a realistic user agent
  await page.setUserAgent(
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
  );

  try {
    // Step 1: Channels
    console.log('Scraping channel list...');
    await page.goto('https://www.youtube.com/feed/subscriptions', {
      waitUntil: 'networkidle2',
      timeout: 30000
    });

    // Scroll near the top first to trigger load
    await page.evaluate(() => window.scrollTo(0, 300));
    await sleep(2000);

    // Collect channels
    const channels = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('ytd-channel-renderer')).map(el => {
        const nameEl = el.querySelector('#channel-title #text');
        const linkEl = el.querySelector('a#main-link');
        const name = nameEl?.textContent?.trim();
        const link = linkEl?.href;
        const idOrHandle = link ? link.split('/').pop() : null;
        return { name, idOrHandle, link };
      }).filter(ch => ch.name && ch.idOrHandle);
    });

    console.log(`Found ${channels.length} channels`);

    fs.writeFileSync(CHANNELS_FILE, JSON.stringify(channels, null, 2));
    console.log('Saved channels to', CHANNELS_FILE);

    // Step 2: Videos - scroll through feed
    console.log('Scraping video feed (will scroll to load 100+ items)...');
    await page.goto('https://www.youtube.com/feed/subscriptions', {
      waitUntil: 'networkidle2',
      timeout: 30000
    });

    // Scroll gradually to load bulk content
    let lastHeight = 0;
    let scrollAttempts = 0;
    const maxScrolls = 30;  // ~30 scrolls should load 100+ videos

    while (scrollAttempts < maxScrolls) {
      const height = await page.evaluate(() => document.body.scrollHeight);
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await sleep(1500);  // wait for lazy load
      
      if (height === lastHeight) {
        scrollAttempts++;
        if (scrollAttempts >= 3) break;  // no new content 3 times → stop
      } else {
        scrollAttempts = 0;
      }
      lastHeight = height;
    }

    // Extract videos
    const videos = await page.evaluate(() => {
      const seen = new Set();
      const results = [];

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

      return results;
    });

    console.log(`Found ${videos.length} unique videos`);

    // Filter out skip-worthy videos
    const filtered = videos.filter(v => !shouldSkip(v.title));
    console.log(`After auto-skip (music/memes/shorts): ${filtered.length} videos remain`);
    console.log('Skipped samples:', videos.filter(v => shouldSkip(v.title)).slice(0, 5).map(v => v.title));

    // Write URLs only, one per line
    const urlLines = filtered.map(v => v.href).join('\n');
    fs.writeFileSync(VIDEOS_FILE, urlLines);
    console.log('Saved video URLs to', VIDEOS_FILE);

    // Stats: which channels are included
    const channelMap = {};
    filtered.forEach(v => {
      const match = v.href.match(/\/channel\/([^\/\?]+)/) || v.href.match(/\/user\/([^\/\?]+)/) || v.href.match(/@([^\/\?]+)/);
      if (match) channelMap[match[1]] = (channelMap[match[1]] || 0) + 1;
    });
    console.log('Distinct channels in today’s feed:', Object.keys(channelMap).length);

  } catch (err) {
    console.error('Error during scraping:', err.message);
    if (err.message.includes('Navigation timeout')) {
      console.log('Retrying without networkidle2...');
      await page.goto('https://www.youtube.com/feed/subscriptions', { timeout: 30000 });
      await sleep(5000);
      // retry logic here
    }
  } finally {
    await browser.close();
  }
}

scrapeSubscriptions().catch(console.error);
