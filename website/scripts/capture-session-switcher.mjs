/**
 * Screenshot harness for the hold-to-pick session switcher overlay.
 *
 * Runs the REAL built SPA (website/dist) behind the repo's gateway-free fixture
 * server and answers every /api/** call from fixtures (no kiro-cli, no live
 * backend, no dashboard token). The overlay is keyboard-opened, so this harness
 * seeds a handful of open sessions, drives the recent-session chord
 * (Alt+Backquote on this platform fixture), waits past the ~120ms paint delay,
 * and captures the grid.
 *
 * The strings are generic on purpose: these frames land in a public PR, so they
 * must not name anything from a private codebase.
 *
 * Usage: node scripts/capture-session-switcher.mjs [outDir] [prefix] [dark|light]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-switcher'
const PREFIX = process.argv[3] || 'after'
const THEME = process.argv[4] || 'dark'

mkdirSync(OUT, { recursive: true })

const folders = [
  { id: 'work', name: 'Work', order: 0 },
  { id: 'pipelines', name: 'Pipelines', order: 0, parent_id: 'work' },
]

const NOW = Date.parse('2026-08-06T01:00:00Z')
const SEED = [
  { key: 's1', title: 'Streaming responses over the message transport', hoursAgo: 0.05, running: true, folder_id: 'pipelines' },
  { key: 's2', title: 'File chips — fade the line highlight, accept ranges', hoursAgo: 1, pending_approval: true },
  { key: 's3', title: 'Dependency upgrade sweep across the monorepo', hoursAgo: 3, project: '/workspace/example/PaymentsService' },
  { key: 's4', title: 'Localise the app manifest strings', hoursAgo: 6 },
  { key: 's5', title: 'Terminal nav active-state fix', hoursAgo: 26, needs_input: true },
  { key: 's6', title: 'Printable logo, third pass', hoursAgo: 30 },
]
const slots = SEED.map(s => ({
  key: s.key,
  title: s.title,
  messages: 6,
  running: !!s.running,
  pending_approval: !!s.pending_approval,
  needs_input: !!s.needs_input,
  agent: 'kirocrew',
  created: '2026-07-20T01:00:00Z',
  last_ts: new Date(NOW - s.hoursAgo * 3600_000).toISOString(),
  folder_id: s.folder_id || '',
  project: s.project,
}))

const OVERLAY = '[role="listbox"]'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { folders, slots, theme: THEME })
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // Build a little MRU history by switching through a couple of sessions via
  // the sidebar, so the overlay's order is the real active-first / newest-first
  // sequence rather than just the seeded sidebar order.
  for (const key of ['s3', 's2']) {
    const row = page.locator(`[data-slot-key="${key}"]`).first()
    if (await row.count()) { await row.click(); await page.waitForTimeout(400) }
  }

  // Open the overlay with the recent-session chord. The fixture reports a
  // non-mac platform, so the chord is Alt+Backquote; hold it (keydown, no keyup)
  // so the grid paints, then screenshot.
  await page.evaluate(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: '`', code: 'Backquote', altKey: true, bubbles: true, cancelable: true,
    }))
  })
  await page.waitForSelector(OVERLAY, { timeout: 3000 })
  await page.waitForTimeout(300)

  const probe = await page.evaluate((sel) => {
    const box = document.querySelector(sel)
    const options = Array.from(box.querySelectorAll('[role="option"]'))
    return {
      ariaLabel: box.getAttribute('aria-label'),
      cardCount: options.length,
      selectedIndex: options.findIndex(o => o.getAttribute('aria-selected') === 'true'),
      currentCard: options.findIndex(o => o.getAttribute('aria-current') === 'true'),
      titles: options.map(o => o.textContent.trim().replace(/\s+/g, ' ').slice(0, 60)),
    }
  }, OVERLAY)
  console.log(`PROBE ${PREFIX} ${THEME} ${JSON.stringify(probe, null, 2)}`)
  if (probe.cardCount < 2 || probe.cardCount > 5) {
    throw new Error(`expected 2..5 cards, got ${probe.cardCount}`)
  }
  if (probe.currentCard !== 0) {
    throw new Error(`index-0 card must be the current session, got ${probe.currentCard}`)
  }

  await page.screenshot({ path: `${OUT}/${PREFIX}-${THEME}-01-overlay.png` })

  // Tight crop of just the overlay, for review at a readable size.
  const box = await page.locator(OVERLAY).boundingBox()
  await page.screenshot({
    path: `${OUT}/${PREFIX}-${THEME}-02-overlay-crop.png`,
    clip: { x: box.x - 16, y: box.y - 16, width: box.width + 32, height: box.height + 32 },
  })

  // Step once with the chord to prove selection moves, then screenshot.
  await page.evaluate(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: '`', code: 'Backquote', altKey: true, bubbles: true, cancelable: true,
    }))
  })
  await page.waitForTimeout(150)
  const stepped = await page.evaluate((sel) => {
    const options = Array.from(document.querySelector(sel).querySelectorAll('[role="option"]'))
    return options.findIndex(o => o.getAttribute('aria-selected') === 'true')
  }, OVERLAY)
  console.log(`STEP ${PREFIX} ${THEME} selectedIndex -> ${stepped}`)
  await page.screenshot({ path: `${OUT}/${PREFIX}-${THEME}-03-stepped.png` })

  await context.close()
  await browser.close()
  srv.close()
  console.log('wrote frames to', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
