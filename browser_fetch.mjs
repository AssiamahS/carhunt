#!/usr/bin/env node
// Load pages in a real browser for the sites that turn away plain requests.
//
//   echo '["https://..."]' | node browser_fetch.mjs   ->  {"https://...": "<html>"} on stdout
//
// Uses patchright (Playwright with the automation tells patched out) and a real,
// visible Chrome window parked off-screen: headless Chrome is what these sites block.
// A page that could not be loaded comes back as an empty string.
import { createRequire } from "node:module";

const MODULES = process.env.CARHUNT_PATCHRIGHT || process.env.HOME + "/.claude/mcp-servers/apartments/node_modules/";
const SETTLE_MS = 6000;      // let the page finish rendering its listings
const GAP_MS = 7000;         // going faster gets the next page a bot check
const CHALLENGE_MS = 25000;  // how long to wait for a "Just a moment" check to clear
const HARD_STOP_MS = 8 * 60 * 1000;

let chromium;
try {
  ({ chromium } = createRequire(MODULES)("patchright"));
} catch (err) {
  console.error("patchright not found in " + MODULES + " (set CARHUNT_PATCHRIGHT): " + err.message);
  process.exit(3);
}

let input = "";
for await (const chunk of process.stdin) input += chunk;
const urls = JSON.parse(input);
const pages = {};

const browser = await chromium.launch({ headless: false, args: ["--window-position=-2400,-2400", "--window-size=1400,1000"] });
// Never leave a browser behind, whatever happens.
const hardStop = setTimeout(async () => { try { await browser.close(); } catch {} process.exit(2); }, HARD_STOP_MS);
try {
  const page = await (await browser.newContext({ viewport: null })).newPage();
  for (const [index, url] of urls.entries()) {
    pages[url] = "";
    try {
      if (index) await page.waitForTimeout(GAP_MS);
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 40000 });
      const started = Date.now();
      while (/just a moment/i.test(await page.title()) && Date.now() - started < CHALLENGE_MS) await page.waitForTimeout(2000);
      await page.waitForTimeout(SETTLE_MS);
      pages[url] = await page.content();
    } catch (err) {
      console.error("failed " + url + ": " + String(err).slice(0, 120));
    }
  }
} finally {
  clearTimeout(hardStop);
  await browser.close();
}
process.stdout.write(JSON.stringify(pages));
