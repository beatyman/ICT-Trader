import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ICT = require("../../src/ict_trader/realtime/static/ict-math.js");

const STEP = 900000;
const BASE = Date.UTC(2026, 0, 5, 0, 0, 0);

function bar(i, { open = 100, high = 100.5, low = 99.5, close = 100, volume = 1 } = {}) {
  return {
    open_time: BASE + i * STEP,
    open, high, low, close, volume,
  };
}

function flat(n = 80) {
  return Array.from({ length: n }, (_, i) => bar(i));
}

test("bullish FVG uses exact candle1.high and candle3.low", () => {
  const bars = flat();
  bars[40] = bar(40, { open: 100, high: 100, low: 99, close: 99.5 });
  bars[42] = bar(42, { open: 103, high: 104, low: 103, close: 103.5 });
  const data = ICT.buildICTData(bars, { tf: "15m" });
  const fvg = data.rectangles.find(r => r.kind === "FVG" && r.direction === "bullish");
  assert.ok(fvg);
  assert.equal(fvg.low, 100);
  assert.equal(fvg.high, 103);
  assert.equal(fvg.startTime, ICT.timeOf(bars[40].open_time));
});

test("failed bullish FVG becomes bearish IFVG", () => {
  const bars = flat();
  bars[40] = bar(40, { open: 100, high: 100, low: 99, close: 99.5 });
  bars[42] = bar(42, { open: 103, high: 104, low: 103, close: 103.5 });
  bars[43] = bar(43, { open: 103, high: 103, low: 98, close: 99 });
  const data = ICT.buildICTData(bars, { tf: "15m" });
  assert.equal(data.rectangles.some(r => r.kind === "FVG" && r.direction === "bullish"), false);
  const ifvg = data.rectangles.find(r => r.kind === "IFVG" && r.direction === "bearish");
  assert.ok(ifvg);
  assert.equal(ifvg.low, 100);
  assert.equal(ifvg.high, 103);
});

test("bullish CISD closes through the first bearish candle open", () => {
  const bars = flat();
  bars[60] = bar(60, { open: 100, high: 100.2, low: 99, close: 99.2 });
  bars[61] = bar(61, { open: 99.2, high: 99.4, low: 98.8, close: 99 });
  bars[62] = bar(62, { open: 99.2, high: 101.5, low: 99.1, close: 101 });
  const data = ICT.buildICTData(bars, { tf: "15m" });
  assert.ok(data.markers.some(m => m.kind === "CISD" && m.direction === "bullish" && m.time === ICT.timeOf(bars[62].open_time)));
});

test("bullish and bearish order blocks retain direction", () => {
  const bars = flat();
  bars[50] = bar(50, { open: 102, high: 102.2, low: 99.8, close: 100 });
  bars[51] = bar(51, { open: 100, high: 104.5, low: 99.8, close: 104 });
  bars[56] = bar(56, { open: 100, high: 102.2, low: 99.8, close: 102 });
  bars[57] = bar(57, { open: 102, high: 102.2, low: 97.8, close: 98 });
  const data = ICT.buildICTData(bars, { tf: "15m" });
  assert.ok(data.rectangles.some(r => r.kind === "OB" && r.direction === "bullish"));
  assert.ok(data.rectangles.some(r => r.kind === "OB" && r.direction === "bearish"));
});

test("PO3 detects AMD sweep and distribution displacement", () => {
  const bars = [];
  for (let i = 0; i < 32; i++) bars.push(bar(i, { open: 100, high: 101, low: 99, close: 100 }));
  bars.push(bar(32, { open: 100, high: 102.5, low: 99.5, close: 100.2 }));
  bars.push(bar(33, { open: 100.2, high: 100.4, low: 94.5, close: 95 }));
  const data = ICT.buildICTData(bars, { tf: "15m" });
  assert.ok(data.markers.some(m => m.kind === "AMD" && m.direction === "bearish"));
  assert.ok(data.markers.some(m => m.kind === "PO3" && m.direction === "bearish"));
});

test("daily context produces PDH/PDL, PWH/PWL and PMH/PML", () => {
  const bars = flat(40);
  const daily = [];
  for (let i = 0; i < 90; i++) {
    const d = new Date(Date.UTC(2025, 10, 1) + i * 86400000);
    const t = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
    daily.push({ open_time: t, open: 100, high: 110 + i, low: 90 - i, close: 100, volume: 1 });
  }
  const data = ICT.buildICTData(bars, { tf: "15m", dailyBars: daily });
  const titles = data.priceLines.map(l => l.title);
  for (const t of ["PDH", "PDL", "PWH", "PWL", "PMH", "PML"]) assert.ok(titles.includes(t), t);
});

test("SMT compares the selected symbol pivots against ETH", () => {
  const bars = Array.from({ length: 24 }, (_, i) => bar(i, { high: 95, low: 85, open: 90, close: 90 }));
  const eth = Array.from({ length: 24 }, (_, i) => bar(i, { high: 95, low: 85, open: 90, close: 90 }));
  bars[10].high = 100;
  bars[20].high = 105;
  eth[10].high = 100;
  eth[17].high = 100;
  eth[20].high = 99;
  const smt = ICT.computeSMT(bars, eth);
  assert.ok(smt);
  assert.equal(smt.direction, "bearish");
  assert.equal(smt.price, 105);
});

test("projectRectangle maps time/price to exact pixels", () => {
  const projected = ICT.projectRectangle(
    { startTime: 10, endTime: 20, low: 10, high: 20 },
    t => t * 2,
    p => 100 - p,
  );
  assert.deepEqual(projected, { x1: 20, x2: 40, top: 80, height: 10 });
});

test("all overlay timestamps are Unix seconds and prices are finite", () => {
  const bars = flat();
  bars[40] = bar(40, { open: 100, high: 100, low: 99, close: 99.5 });
  bars[42] = bar(42, { open: 103, high: 104, low: 103, close: 103.5 });
  const data = ICT.buildICTData(bars, { tf: "15m" });
  for (const r of data.rectangles) {
    assert.ok(Number.isInteger(r.startTime) && Number.isInteger(r.endTime));
    assert.ok(Number.isFinite(r.low) && Number.isFinite(r.high) && r.low <= r.high);
  }
  for (const l of data.priceLines) {
    assert.ok(Number.isFinite(l.price));
  }
});


test("Chan structure is converted to BI/SEG lines, ZS rectangles and BSP markers", () => {
  const bars = flat();
  const chan = {
    bis: [{ start_time: BASE, end_time: BASE + 5 * STEP, start_price: 90, end_price: 100, direction: "up", sure: true }],
    segs: [{ start_time: BASE, end_time: BASE + 10 * STEP, start_price: 88, end_price: 105, direction: "up", sure: true }],
    zss: [{ start_time: BASE + 2 * STEP, end_time: BASE + 8 * STEP, low: 95, high: 102 }],
    bsps: [{ time: BASE + 5 * STEP, price: 100, is_buy: true, type: "2买", divergence: false }],
    divergences: [{ time: BASE + 5 * STEP, price: 100, is_buy: true, type: "1买", divergence: true }],
  };
  const data = ICT.buildICTData(bars, { tf: "15m", chan });
  assert.ok(data.segments.some(s => s.label === "BI"));
  assert.ok(data.segments.some(s => s.label === "SEG" && s.width === 2));
  assert.ok(data.rectangles.some(r => r.kind === "ChanZS" && r.low === 95 && r.high === 102));
  assert.ok(data.markers.some(m => m.kind === "BSP" && m.text === "2买" && m.direction === "bullish"));
  assert.equal(data.markers.some(m => m.text === "DIV"), false);
  assert.deepEqual(ICT.projectSegment(data.segments[0], t => t, p => p), {
    x1: ICT.timeOf(BASE), x2: ICT.timeOf(BASE + 5 * STEP), y1: 90, y2: 100,
  });
});


test("price labels avoid collisions while staying inside the pane", () => {
  const rows = ICT.layoutPriceLabels([
    { title: "A", y: 100 }, { title: "B", y: 102 },
    { title: "C", y: 104 }, { title: "D", y: 350 },
  ], 360, 14, 8);
  assert.equal(rows.length, 4);
  for (let i = 1; i < rows.length; i++) {
    assert.ok(rows[i].labelY - rows[i - 1].labelY >= 14);
  }
  assert.ok(rows[0].labelY >= 8);
  assert.ok(rows[rows.length - 1].labelY <= 352);
});


test("Chan BI and SEG can be hidden independently", () => {
  const bars = flat();
  const chan = {
    bis: [{ start_time: BASE, end_time: BASE + 5 * STEP, start_price: 90, end_price: 100, direction: "up" }],
    segs: [{ start_time: BASE, end_time: BASE + 10 * STEP, start_price: 88, end_price: 105, direction: "up" }],
    zss: [], bsps: [], divergences: [],
  };
  const hiddenAll = ICT.buildICTData(bars, { tf: "15m", chan, chanVisibility: { bi: false, seg: false } });
  assert.equal(hiddenAll.segments.length, 0);
  const noBi = ICT.buildICTData(bars, { tf: "15m", chan, chanVisibility: { bi: false, seg: true } });
  assert.equal(noBi.segments.some(s => s.label === "BI"), false);
  assert.equal(noBi.segments.some(s => s.label === "SEG"), true);
});
