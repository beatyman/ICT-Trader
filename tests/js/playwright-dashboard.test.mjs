import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs";
import { chromium } from "playwright-core";

const ROOT = path.resolve(import.meta.dirname, "../..");
const ARTIFACTS = path.join(ROOT, "tests/js/artifacts");
const CHROME = process.env.CHROME_PATH
  || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const BASE = Date.UTC(2026, 0, 5);
const STEP = 900000;

function candles(n = 240, base = 1.1) {
  return Array.from({ length: n }, (_, i) => {
    const wave = Math.sin(i / 17) * 0.012 + (i > n - 25 ? 0.015 : 0);
    return {
      symbol: "XRPUSDT", interval: "15m",
      open_time: BASE + i * STEP,
      close_time: BASE + i * STEP + STEP - 1,
      open: base + wave, high: base + wave + 0.006,
      low: base + wave - 0.006, close: base + wave + 0.002,
      volume: 100 + (i % 20) * 3, quote_volume: 1000,
      trades: 10, is_closed: i < n - 1, ingest_time: BASE + i * STEP + 1000,
      source: "fixture",
    };
  });
}

test("full dashboard renders a fixed signal scenario", async () => {
  fs.mkdirSync(ARTIFACTS, { recursive: true });
  const snapshot = JSON.parse(fs.readFileSync(
    path.join(ROOT, "tests/js/fixtures/dashboard-snapshot.json"), "utf8",
  ));
  const bars = candles();
  const last = bars[bars.length - 1];
  snapshot.mode = "live";
  snapshot.phase = "ready";
  snapshot.boot = { done: 24, total: 24 };
  snapshot.server_time = last.close_time;
  snapshot.live_bars = [last];
  snapshot.signals = [];
  snapshot.engine_signals = [{
    signal_id: "SIG-CHAN-FIXTURE", engine: "chan", engine_version: "chan-test",
    symbol: "XRPUSDT", interval: "15m", direction: "short", signal_type: "chan_bsp",
    trigger_price_zone: { low: 1.08, high: 1.09 },
    invalidation_price: 1.12, confidence: 0.66,
    source_kline_open_time: last.open_time - 20 * STEP,
    evidence: { bsp_type: "2卖", last_zs_zd: 1.07, last_zs_zg: 1.10, target: 1.04 },
  }];
  snapshot.setups = [{
    setup_id: "SET-FIXTURE", symbol: "XRPUSDT", interval: "15m",
    direction: "short", status: "OPEN", created_at: last.open_time,
    opened_at: last.open_time, closed_at: null,
    entry_low: 1.08, entry_high: 1.09, entry: 1.085,
    stop: 1.12, target: 1.015, rr: 2.0, confidence: 0.66,
    engine: "chan", engine_version: "chan-test",
    signal_ids: ["SIG-CHAN-FIXTURE"],
    engine_signals: [{
      signal_id: "SIG-CHAN-FIXTURE", engine: "chan", symbol: "XRPUSDT",
      interval: "15m", direction: "short", source_kline_open_time: last.open_time - 20 * STEP,
      evidence: { bsp_type: "2卖", last_zs_zd: 1.07, last_zs_zg: 1.10 },
    }],
    ai_decision_id: "", ai_confidence: 0, ai_decision: "",
    bars_alive: 2, last_price: last.close,
  }];
  snapshot.ai_decisions = [{
    decision_id: "AI-FIXTURE", setup_id: "SET-FIXTURE", symbol: "XRPUSDT",
    verdict: "support", confidence: 72, headline: "fixed review",
    summary: "fixture", status: 1, created_at: last.close_time,
    parsed: { verdict: "support", confidence: 72, headline: "fixed review", summary: "fixture" },
  }];

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
    page.on("pageerror", e => console.log("PAGEERROR", e.message));
    page.on("console", m => { if (m.type() === "error") console.log("CONSOLE", m.text()); });
    await page.addInitScript(() => {
      class DummyWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        static CLOSED = 3;
        constructor() {
          this.readyState = 3;
          setTimeout(() => this.onclose && this.onclose({}), 30);
        }
        send() {}
        close() {}
      }
      window.WebSocket = DummyWebSocket;
    });
    await page.route("**/api/snapshot*", route => route.fulfill({ json: snapshot }));
    await page.route("**/api/klines*", route => {
      const u = new URL(route.request().url());
      const interval = u.searchParams.get("interval") || "15m";
      const symbol = (u.searchParams.get("symbol") || "").toUpperCase();
      if (interval === "1d") {
        const daily = Array.from({ length: 90 }, (_, i) => ({
          ...bars[i % bars.length],
          open_time: Date.UTC(2025, 10, 1) + i * 86400000,
          interval: "1d", is_closed: true,
        }));
        return route.fulfill({ json: { bars: daily } });
      }
      const out = symbol === "ETHUSDT" ? candles(240, 2400) : bars;
      return route.fulfill({ json: { bars: out } });
    });
    await page.route("**/api/chart/overlays*", route => route.fulfill({
      json: { source: "backend", rectangles: [{
        kind: "FVG", direction: "bullish",
        start_time: last.open_time - 5 * STEP, end_time: last.open_time,
        low: 1.09, high: 1.10, label: "FVG",
      }] },
    }));
    await page.route("**/api/chan/structure*", route => route.fulfill({
      json: { bi_count: 1, seg_count: 1, zs_count: 1, bsp_count: 1,
        bis: [], segs: [], zss: [], bsps: [], divergences: [], reasons: [] },
    }));
    await page.route("**/api/ai/decisions*", route => route.fulfill({ json: [] }));

    await page.goto("http://127.0.0.1:8080/?theme=dark&symbol=XRPUSDT", { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#lwcChart canvas", { timeout: 15000 });
    await page.waitForTimeout(1800);

    assert.equal(await page.locator("#chartSymbol").textContent(), "XRPUSDT");
    assert.match(await page.locator("#setupContent").innerText(), /2\.00R|SETUP|ENTRY/i);
    assert.ok(await page.locator(".signal-item").count() >= 1);
    await page.screenshot({ path: path.join(ARTIFACTS, "dashboard-debug.png"), fullPage: false });
    const labelStats = await page.evaluate(() => window.__chartLabelStats || {});
    assert.ok(labelStats.axis >= 1, "plan price-axis labels expected");
    assert.equal(labelStats.html, 0, "plan lines must not duplicate in HTML layer");
    assert.ok(await page.locator("#aiLogList .ai-log-row").count() >= 1);
    assert.ok(await page.locator("#setupCard").isVisible());
    const overlapRatio = await page.evaluate(() => {
      const boxes = [...document.querySelectorAll("#priceLabelLayer .price-label")]
        .map(el => el.getBoundingClientRect());
      let overlaps = 0, pairs = 0;
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        pairs++;
        const a = boxes[i], b = boxes[j];
        if (!(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top)) overlaps++;
      }
      return pairs ? overlaps / pairs : 0;
    });
    assert.ok(overlapRatio <= 0.05, `label overlap ratio ${overlapRatio}`);
    const fps = await page.evaluate(() => new Promise(resolve => {
      let frames = 0;
      const start = performance.now();
      const step = now => {
        frames++;
        if (now - start < 350) requestAnimationFrame(step);
        else resolve(frames * 1000 / (now - start));
      };
      requestAnimationFrame(step);
    }));
    assert.ok(fps >= 20, `fps ${fps}`);
    assert.ok(await page.locator("#chartWrap canvas").count() >= 1);
    await page.locator("#chanBtn").click();
    assert.equal(await page.locator(".legend-toggle[data-chan].active").count(), 0);
    await page.locator("#chanBtn").click();
    assert.equal(await page.locator(".legend-toggle[data-chan].active").count(), 2);

    await page.screenshot({
      path: path.join(ARTIFACTS, "dashboard-fixed-signal.png"),
      fullPage: false, animations: "disabled",
    });
  } finally {
    await browser.close();
  }
});
