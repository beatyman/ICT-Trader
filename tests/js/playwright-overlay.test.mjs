import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs";
import { chromium } from "playwright-core";

const ROOT = path.resolve(import.meta.dirname, "../..");
const VENDOR = path.join(ROOT, "src/ict_trader/realtime/static/vendor/lightweight-charts.standalone.production.js");
const MATH = path.join(ROOT, "src/ict_trader/realtime/static/ict-math.js");
const ARTIFACTS = path.join(ROOT, "tests/js/artifacts");
const CHROME = process.env.CHROME_PATH
  || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

function fixtureBars() {
  const base = Date.UTC(2026, 0, 5);
  return Array.from({ length: 60 }, (_, i) => ({
    open_time: base + i * 900000,
    open: 100, high: 100.5, low: 99.5, close: 100, volume: 1,
  })).map((b, i) => {
    if (i === 40) return { ...b, open: 100, high: 100, low: 99, close: 99.5 };
    if (i === 42) return { ...b, open: 103, high: 104, low: 103, close: 103.5 };
    return b;
  });
}

test("fixed viewport: FVG rectangle and SMT marker map to chart pixels", async () => {
  fs.mkdirSync(ARTIFACTS, { recursive: true });
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 700 }, deviceScaleFactor: 1 });
  await page.setContent('<div id="chart" style="width:1000px;height:500px"></div>');
  await page.addScriptTag({ path: VENDOR });
  await page.addScriptTag({ path: MATH });

  const result = await page.evaluate(async () => {
    const bars = (() => {
      const base = Date.UTC(2026, 0, 5);
      return Array.from({ length: 60 }, (_, i) => ({
        open_time: base + i * 900000,
        open: 100, high: 100.5, low: 99.5, close: 100, volume: 1,
      })).map((b, i) => {
        if (i === 40) return { ...b, open: 100, high: 100, low: 99, close: 99.5 };
        if (i === 42) return { ...b, open: 103, high: 104, low: 103, close: 103.5 };
        return b;
      });
    })();
    const data = window.ICTMath.buildICTData(bars, { tf: "15m" });
    const chart = window.LightweightCharts.createChart(document.getElementById("chart"), {
      width: 1000, height: 500,
      layout: { background: { type: "solid", color: "#0b111a" }, textColor: "#b5c0ce" },
      rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.2 } },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    const series = chart.addCandlestickSeries();
    series.setData(bars.map(b => ({
      time: Math.floor(b.open_time / 1000),
      open: b.open, high: b.high, low: b.low, close: b.close,
    })));
    const overlay = {
      attached(param) { this.chart = param.chart; this.series = param.series; },
      paneViews() {
        const self = this;
        return [{ renderer: () => ({ draw(target) {
          target.useBitmapCoordinateSpace(scope => {
            const r = window.ICTMath.projectRectangle(
              data.rectangles.find(x => x.kind === "FVG"),
              t => self.chart.timeScale().timeToCoordinate(t) * scope.horizontalPixelRatio,
              p => self.series.priceToCoordinate(p) * scope.verticalPixelRatio,
            );
            if (!r) return;
            scope.context.fillStyle = "rgba(38,166,154,.75)";
            scope.context.fillRect(r.x1, r.top, r.x2 - r.x1, r.height);
          });
        } }) }];
      },
    };
    series.attachPrimitive(overlay);
    chart.timeScale().fitContent();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const fvg = data.rectangles.find(r => r.kind === "FVG" && r.direction === "bullish");
    const x1 = chart.timeScale().timeToCoordinate(fvg.startTime);
    const x2 = chart.timeScale().timeToCoordinate(fvg.endTime);
    const yHigh = series.priceToCoordinate(fvg.high);
    const yLow = series.priceToCoordinate(fvg.low);
    const projected = window.ICTMath.projectRectangle(
      fvg,
      t => chart.timeScale().timeToCoordinate(t),
      p => series.priceToCoordinate(p),
    );
    const smtBars = Array.from({ length: 24 }, (_, i) => ({
      open_time: Date.UTC(2026, 0, 5) + i * 900000,
      open: 90, high: 95, low: 85, close: 90, volume: 1,
    }));
    const ethBars = smtBars.map(b => ({ ...b }));
    smtBars[10].high = 100; smtBars[20].high = 102;
    ethBars[10].high = 100; ethBars[17].high = 100; ethBars[20].high = 99;
    const smt = window.ICTMath.computeSMT(smtBars, ethBars);
    return {
      fvg, x1, x2, yHigh, yLow, projected,
      smtX: smt ? chart.timeScale().timeToCoordinate(smt.time) : null,
      smtY: smt ? series.priceToCoordinate(smt.price) : null,
      chartWidth: chart.paneSize().width,
      chartHeight: chart.paneSize().height,
    };
  });

  assert.ok(result.fvg);
  assert.ok(result.x1 != null);
  assert.ok(result.yHigh < result.yLow);
  assert.equal(result.projected.x1, result.x1);
  assert.equal(result.projected.top, Math.min(result.yHigh, result.yLow));
  assert.equal(result.projected.height, Math.abs(result.yLow - result.yHigh));
  assert.ok(result.x1 >= 0 && result.x1 <= result.chartWidth);
  assert.ok(result.yHigh >= 0 && result.yLow <= result.chartHeight);
  assert.ok(Number.isFinite(result.smtX) && Number.isFinite(result.smtY));
  assert.ok(result.smtX >= 0 && result.smtX <= result.chartWidth);
  assert.ok(result.smtY >= 0 && result.smtY <= result.chartHeight);

  await page.screenshot({ path: path.join(ARTIFACTS, "overlay-fixed-viewport.png"), fullPage: false });
  } finally {
    await browser.close();
  }
});
