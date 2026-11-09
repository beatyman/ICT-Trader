/*
 * Pure ICT overlay calculations.
 *
 * This file has no DOM dependency.  The browser uses it through
 * window.ICTMath; Node tests import it through module.exports.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ICTMath = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const TF_MS = {
    "1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000,
    "4h": 14400000, "1d": 86400000,
  };
  const num = v => Number(v || 0);

  function timeOf(ms) {
    return Math.floor(num(ms) / 1000);
  }

  function palette(direction) {
    return direction === "bullish"
      ? { fill: "rgba(38,166,154,.17)", border: "#26a69a", labelColor: "#26a69a" }
      : direction === "bearish"
        ? { fill: "rgba(239,83,80,.15)", border: "#ef5350", labelColor: "#ef5350" }
        : { fill: "rgba(120,123,134,.10)", border: "#787b86", labelColor: "#787b86" };
  }

  function pivots(bars, key, left = 2, right = 2) {
    const out = [];
    for (let i = left; i < bars.length - right; i++) {
      const v = num(bars[i][key]);
      let ok = true;
      for (let j = i - left; j <= i + right; j++) {
        if (j === i) continue;
        if (key === "high" ? num(bars[j].high) > v : num(bars[j].low) < v) {
          ok = false;
          break;
        }
      }
      if (ok) out.push({ i, time: timeOf(bars[i].open_time), price: v });
    }
    return out;
  }

  function computeSMT(bars, ethBars) {
    if (!bars.length || !ethBars.length) return null;
    const ethByTime = new Map(ethBars.map(b => [timeOf(b.open_time), b]));
    const aligned = bars
      .map(b => ({ s: b, e: ethByTime.get(timeOf(b.open_time)) }))
      .filter(x => x.e);
    if (aligned.length < 20) return null;
    const sBars = aligned.map(x => x.s);
    const eBars = aligned.map(x => x.e);
    const sHighs = pivots(sBars, "high", 2, 2);
    const eHighs = pivots(eBars, "high", 2, 2);
    const sLows = pivots(sBars, "low", 2, 2);
    const eLows = pivots(eBars, "low", 2, 2);
    if (sHighs.length >= 2 && eHighs.length >= 2) {
      const sLast = sHighs.at(-1), sPrev = sHighs.at(-2);
      const eLast = eHighs.at(-1), ePrev = eHighs.at(-2);
      if (sLast.price > sPrev.price && eLast.price <= ePrev.price) {
        return { direction: "bearish", time: sLast.time, price: sLast.price, label: "SMT HIGH" };
      }
    }
    if (sLows.length >= 2 && eLows.length >= 2) {
      const sLast = sLows.at(-1), sPrev = sLows.at(-2);
      const eLast = eLows.at(-1), ePrev = eLows.at(-2);
      if (sLast.price < sPrev.price && eLast.price >= ePrev.price) {
        return { direction: "bullish", time: sLast.time, price: sLast.price, label: "SMT LOW" };
      }
    }
    return null;
  }

  function dailyLevels(daily, priceLines) {
    if (!Array.isArray(daily) || daily.length < 8) return;
    const last = daily[daily.length - 1];
    const d = new Date(num(last.open_time));
    const dayStart = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
    const prevDays = daily.filter(b => num(b.open_time) < dayStart);
    const prev = prevDays[prevDays.length - 1];
    if (prev) {
      priceLines.push({ price: num(prev.high), title: "PDH", color: "#f5a623", lineStyle: 2, lineWidth: 1 });
      priceLines.push({ price: num(prev.low), title: "PDL", color: "#f5a623", lineStyle: 2, lineWidth: 1 });
    }
    const dow = (d.getUTCDay() + 6) % 7;
    const weekStart = dayStart - dow * 86400000;
    const prevWeekStart = weekStart - 7 * 86400000;
    const week = daily.filter(b => {
      const t = num(b.open_time);
      return t >= prevWeekStart && t < weekStart;
    });
    if (week.length) {
      priceLines.push({ price: Math.max(...week.map(b => num(b.high))), title: "PWH", color: "#b89dff", lineStyle: 2, lineWidth: 1 });
      priceLines.push({ price: Math.min(...week.map(b => num(b.low))), title: "PWL", color: "#b89dff", lineStyle: 2, lineWidth: 1 });
    }
    const monthStart = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1);
    const prevMonthStart = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - 1, 1);
    const month = daily.filter(b => {
      const t = num(b.open_time);
      return t >= prevMonthStart && t < monthStart;
    });
    if (month.length) {
      priceLines.push({ price: Math.max(...month.map(b => num(b.high))), title: "PMH", color: "#54c8f8", lineStyle: 3, lineWidth: 1 });
      priceLines.push({ price: Math.min(...month.map(b => num(b.low))), title: "PML", color: "#54c8f8", lineStyle: 3, lineWidth: 1 });
    }
  }

  function buildICTData(bars, options = {}) {
    if (!Array.isArray(bars) || !bars.length) {
      return { rectangles: [], priceLines: [], markers: [] };
    }
    const tf = options.tf || "15m";
    const snap = options.snap || {};
    const smt = options.smt || null;
    const daily = options.dailyBars || [];
    const chan = options.chan || null;
    const backendOverlays = options.backendOverlays || null;
    const chanVisibility = options.chanVisibility || {};
    const step = TF_MS[tf] || 900000;
    const last = bars[bars.length - 1];
    const end = timeOf(last.open_time) + step / 1000;
    const atr = (() => {
      const n = Math.min(14, bars.length - 1);
      if (n < 1) return 0;
      let total = 0;
      for (let i = bars.length - n; i < bars.length; i++) total += Math.max(0, num(bars[i].high) - num(bars[i].low));
      return total / n;
    })();
    const rectangles = [];
    const priceLines = [];
    const markers = [];
    const segments = [];
    const startIdx = Math.max(2, bars.length - 140);
    const minGap = Math.max(atr * 0.035, Math.abs(num(last.close)) * 0.00005);
    const addRect = (kind, direction, startTime, low, high, label, endTime = end) => {
      const colors = palette(direction);
      rectangles.push({
        kind, direction, startTime, endTime, low, high,
        fill: colors.fill, border: colors.border, label, labelColor: colors.labelColor,
      });
    };

    // FVG / IFVG. Backend rectangles are canonical when available.
    if (backendOverlays?.rectangles?.length) {
      for (const r of backendOverlays.rectangles) {
        if (!["FVG", "IFVG", "OB"].includes(r.kind)) continue;
        const colors = palette(r.direction);
        rectangles.push({
          kind: r.kind, direction: r.direction,
          startTime: Number(r.start_time), endTime: Number(r.end_time),
          low: Number(r.low), high: Number(r.high),
          fill: colors.fill, border: colors.border, label: r.label || r.kind,
          labelColor: colors.labelColor,
        });
      }
    } else {
    for (let i = startIdx; i < bars.length - 1; i++) {
      const a = bars[i - 2], c = bars[i];
      if (!a || !c) continue;
      const bullGap = num(c.low) - num(a.high);
      const bearGap = num(a.low) - num(c.high);
      if (bullGap > minGap) {
        let failed = false;
        for (let j = i + 1; j < bars.length; j++) {
          if (num(bars[j].close) < num(a.high)) { failed = true; break; }
        }
        if (failed) addRect("IFVG", "bearish", timeOf(a.open_time), num(a.high), num(c.low), "IFVG");
        else addRect("FVG", "bullish", timeOf(a.open_time), num(a.high), num(c.low), "FVG");
      } else if (bearGap > minGap) {
        let failed = false;
        for (let j = i + 1; j < bars.length; j++) {
          if (num(bars[j].close) > num(a.low)) { failed = true; break; }
        }
        if (failed) addRect("IFVG", "bullish", timeOf(a.open_time), num(c.high), num(a.low), "IFVG");
        else addRect("FVG", "bearish", timeOf(a.open_time), num(c.high), num(a.low), "FVG");
      }
    }

    }
    // CISD: close through the open of the first candle in a 2+ run.
    let bearRun = 0, bearFirstOpen = 0;
    let bullRun = 0, bullFirstOpen = 0;
    for (let i = startIdx; i < bars.length; i++) {
      const b = bars[i], o = num(b.open), c = num(b.close);
      if (c < o) {
        if (bullRun >= 2 && c < bullFirstOpen) {
          markers.push({
            time: timeOf(b.open_time), position: "aboveBar", shape: "square",
            color: "#ef5350", text: "CISD", kind: "CISD", direction: "bearish",
          });
        }
        if (bearRun === 0) bearFirstOpen = o;
        bearRun += 1;
        bullRun = 0;
      } else if (c > o) {
        if (bearRun >= 2 && c > bearFirstOpen) {
          markers.push({
            time: timeOf(b.open_time), position: "belowBar", shape: "square",
            color: "#26a69a", text: "CISD", kind: "CISD", direction: "bullish",
          });
        }
        if (bullRun === 0) bullFirstOpen = o;
        bullRun += 1;
        bearRun = 0;
      } else {
        bearRun = 0;
        bullRun = 0;
      }
    }

    // OB.
    if (!backendOverlays?.rectangles?.length) {
    for (let i = startIdx + 1; i < bars.length; i++) {
      const prev = bars[i - 1], cur = bars[i];
      const body = Math.abs(num(cur.close) - num(cur.open));
      if (body < atr * 0.95) continue;
      const bullish = num(cur.close) > num(cur.open);
      const prevBear = num(prev.close) < num(prev.open);
      if (bullish !== prevBear) continue;
      addRect(
        "OB", bullish ? "bullish" : "bearish", timeOf(prev.open_time),
        Math.min(num(prev.open), num(prev.close), num(prev.low)),
        Math.max(num(prev.open), num(prev.close), num(prev.high)),
        "OB",
      );
    }

    }
    // Asian session.
    const lastDate = new Date(num(last.open_time)).toISOString().slice(0, 10);
    let asianBars = bars.filter(b => {
      const d = new Date(num(b.open_time));
      return d.toISOString().slice(0, 10) === lastDate && d.getUTCHours() < 8;
    });
    if (asianBars.length < 4) asianBars = bars.slice(-Math.min(32, bars.length));
    if (asianBars.length) {
      const aHi = Math.max(...asianBars.map(b => num(b.high)));
      const aLo = Math.min(...asianBars.map(b => num(b.low)));
      const aStart = timeOf(asianBars[0].open_time);
      const aEnd = timeOf(asianBars[asianBars.length - 1].open_time) + step / 1000;
      addRect("AsianRange", "neutral", aStart, aLo, aHi, "ASIAN RANGE", aEnd);
      priceLines.push({ price: aHi, title: "Asian High", color: "#787b86", lineStyle: 2, lineWidth: 1, startTime: aStart, endTime: aEnd, axisLabel: false });
      priceLines.push({ price: aLo, title: "Asian Low", color: "#787b86", lineStyle: 2, lineWidth: 1, startTime: aStart, endTime: aEnd, axisLabel: false });
    }
    const mdo = [...bars].reverse().find(b => new Date(num(b.open_time)).getUTCHours() === 0);
    if (mdo) priceLines.push({ price: num(mdo.open), title: "MDO", color: "#2962ff", lineStyle: 2, lineWidth: 1, axisLabel: false });

    // BSL / SSL / EQ.
    const lookback = bars.slice(-Math.min(96, bars.length));
    if (lookback.length) {
      const hi = Math.max(...lookback.map(b => num(b.high)));
      const lo = Math.min(...lookback.map(b => num(b.low)));
      const lastClose = num(bars[bars.length - 1].close);
      const swingHighs = pivots(lookback, "high", 2, 2)
        .filter(p => p.price > lastClose)
        .filter(p => !lookback.slice(p.i + 1).some(b => num(b.high) > p.price))
        .sort((a, b) => b.i - a.i);
      const swingLows = pivots(lookback, "low", 2, 2)
        .filter(p => p.price < lastClose)
        .filter(p => !lookback.slice(p.i + 1).some(b => num(b.low) < p.price))
        .sort((a, b) => b.i - a.i);
      // BSL/SSL are the nearest unswept swing liquidity, not the 24h range.
      const bsl = swingHighs.length ? swingHighs[0].price : hi;
      const ssl = swingLows.length ? swingLows[0].price : lo;
      const first = timeOf(lookback[0].open_time);
      priceLines.push({ price: bsl, title: "BSL", color: "#ef5350", lineStyle: 3, lineWidth: 1, startTime: first, endTime: end });
      priceLines.push({ price: ssl, title: "SSL", color: "#26a69a", lineStyle: 3, lineWidth: 1, startTime: first, endTime: end });
      priceLines.push({ price: (hi + lo) / 2, title: "EQ (50%)", color: "#787b86", lineStyle: 0, lineWidth: 1, startTime: first, endTime: end, axisLabel: false });
    }

    // PO3 / AMD.
    if (asianBars.length >= 4) {
      const aHi = Math.max(...asianBars.map(b => num(b.high)));
      const aLo = Math.min(...asianBars.map(b => num(b.low)));
      const asianEndTime = timeOf(asianBars[asianBars.length - 1].open_time) + step / 1000;
      const after = bars.filter(b => timeOf(b.open_time) >= asianEndTime && new Date(num(b.open_time)).toISOString().slice(0, 10) === lastDate);
      const sweep = after.find(b => (num(b.high) > aHi && num(b.close) < aHi) || (num(b.low) < aLo && num(b.close) > aLo));
      if (sweep) {
        const bearSweep = num(sweep.high) > aHi && num(sweep.close) < aHi;
        markers.push({
          time: timeOf(sweep.open_time), position: bearSweep ? "aboveBar" : "belowBar",
          shape: "circle", color: bearSweep ? "#ef5350" : "#26a69a",
          text: "AMD", kind: "AMD", direction: bearSweep ? "bearish" : "bullish",
        });
        const sweepIdx = bars.indexOf(sweep);
        const displacement = bars.slice(sweepIdx + 1).find(b => {
          const body = Math.abs(num(b.close) - num(b.open));
          const sameDir = bearSweep ? num(b.close) < num(b.open) : num(b.close) > num(b.open);
          return sameDir && body > atr * 1.1;
        });
        if (displacement) {
          markers.push({
            time: timeOf(displacement.open_time), position: bearSweep ? "aboveBar" : "belowBar",
            shape: "arrowDown", color: bearSweep ? "#ef5350" : "#26a69a",
            text: "PO3", kind: "PO3", direction: bearSweep ? "bearish" : "bullish",
          });
        }
      }
    }

    // Daily levels.
    dailyLevels(daily, priceLines);

    // SMT.
    if (smt) {
      priceLines.push({
        price: smt.price, title: smt.label,
        color: smt.direction === "bearish" ? "#ef5350" : "#26a69a",
        lineStyle: 2, lineWidth: 1,
      });
      markers.push({
        time: smt.time, position: smt.direction === "bearish" ? "aboveBar" : "belowBar",
        shape: "circle", color: smt.direction === "bearish" ? "#ef5350" : "#26a69a",
        text: smt.label, kind: "SMT", direction: smt.direction,
      });
    }

    if (chan) {
      if (chanVisibility.bi !== false) for (const b of (chan.bis || []).slice(-120)) {
        segments.push({
          startTime: Math.floor(b.start_time / 1000),
          endTime: Math.floor(b.end_time / 1000),
          startPrice: b.start_price, endPrice: b.end_price,
          color: "rgba(120,123,134,.70)", width: 1, label: "BI",
        });
      }
      if (chanVisibility.seg !== false) for (const s of (chan.segs || []).slice(-60)) {
        segments.push({
          startTime: Math.floor(s.start_time / 1000),
          endTime: Math.floor(s.end_time / 1000),
          startPrice: s.start_price, endPrice: s.end_price,
          color: "rgba(84,200,248,.90)", width: 2, label: "SEG",
        });
      }
      for (const z of (chan.zss || []).slice(-6)) {
        rectangles.push({
          kind: "ChanZS", direction: "neutral",
          startTime: Math.floor(z.start_time / 1000),
          endTime: Math.floor(z.end_time / 1000),
          low: z.low, high: z.high,
          fill: "rgba(184,157,255,.13)", border: "rgba(184,157,255,.52)",
          label: "CHAN ZS", labelColor: "rgba(184,157,255,.52)",
        });
      }
      for (const b of (chan.bsps || []).slice(-16)) {
        markers.push({
          time: Math.floor(b.time / 1000),
          position: b.is_buy ? "belowBar" : "aboveBar",
          shape: "circle", color: b.is_buy ? "#26a69a" : "#ef5350",
          text: b.type || (b.is_buy ? "BSP" : "BSP"), kind: "BSP",
          direction: b.is_buy ? "bullish" : "bearish",
        });
      }
    }

    // Chan zone from live engine evidence only.
    let chanZone = null;
    const candidates = [];
    for (const st of (snap.setups || [])) {
      if (st.engine === "chan" && st.engine_signals?.length) candidates.push(st.engine_signals[0]);
    }
    for (const es of (snap.engine_signals || [])) if (es.engine === "chan") candidates.push(es);
    for (const es of candidates) {
      const ev = es.evidence || {};
      const zd = Number(ev.last_zs_zd), zg = Number(ev.last_zs_zg);
      if (Number.isFinite(zd) && Number.isFinite(zg) && zd < zg) {
        chanZone = { zd, zg, time: timeOf(es.source_kline_open_time), label: ev.bsp_type || "CHAN" };
        break;
      }
    }
    if (chanZone) {
      rectangles.push({
        kind: "ChanZS", direction: "neutral", startTime: chanZone.time, endTime: end,
        low: chanZone.zd, high: chanZone.zg,
        fill: "rgba(184,157,255,.15)", border: "rgba(184,157,255,.55)",
        label: `CHAN ZS · ${chanZone.label}`, labelColor: "rgba(184,157,255,.55)",
      });
    }

    // MSS/BOS/Sweep.
    const pivotH = [], pivotL = [];
    for (let i = startIdx + 2; i < bars.length - 2; i++) {
      const h = num(bars[i].high), l = num(bars[i].low);
      if (h >= num(bars[i - 1].high) && h >= num(bars[i - 2].high) && h >= num(bars[i + 1].high) && h >= num(bars[i + 2].high)) pivotH.push({ i, price: h });
      if (l <= num(bars[i - 1].low) && l <= num(bars[i - 2].low) && l <= num(bars[i + 1].low) && l <= num(bars[i + 2].low)) pivotL.push({ i, price: l });
    }
    const hMap = new Map(pivotH.map(p => [p.i, p]));
    const lMap = new Map(pivotL.map(p => [p.i, p]));
    let swingH = null, swingL = null, trend = 0;
    for (let i = startIdx + 2; i < bars.length; i++) {
      const candidateH = hMap.get(i);
      const candidateL = lMap.get(i);
      const b = bars[i], c = num(b.close), h = num(b.high), l = num(b.low);
      if (swingH && i > swingH.i) {
        if (h > swingH.price && c < swingH.price) {
          markers.push({ time: timeOf(b.open_time), position: "aboveBar", shape: "circle", color: "#ef5350", text: "BSL Sweep", kind: "BSL_SWEEP", direction: "bearish" });
          swingH = null;
        } else if (c > swingH.price) {
          const kind = trend < 0 ? "MSS" : "BOS";
          markers.push({ time: timeOf(b.open_time), position: "belowBar", shape: kind === "BOS" ? "square" : "arrowUp", color: "#26a69a", text: kind, kind, direction: "bullish" });
          trend = 1; swingH = null;
        }
      }
      if (swingL && i > swingL.i) {
        if (l < swingL.price && c > swingL.price) {
          markers.push({ time: timeOf(b.open_time), position: "belowBar", shape: "circle", color: "#26a69a", text: "SSL Sweep", kind: "SSL_SWEEP", direction: "bullish" });
          swingL = null;
        } else if (c < swingL.price) {
          const kind = trend > 0 ? "MSS" : "BOS";
          markers.push({ time: timeOf(b.open_time), position: "aboveBar", shape: kind === "BOS" ? "square" : "arrowDown", color: "#ef5350", text: kind, kind, direction: "bearish" });
          trend = -1; swingL = null;
        }
      }
      if (candidateH) swingH = candidateH;
      if (candidateL) swingL = candidateL;
    }

    const fvg = rectangles.filter(r => r.kind === "FVG").slice(-8);
    const ifvg = rectangles.filter(r => r.kind === "IFVG").slice(-4);
    const ob = rectangles.filter(r => r.kind === "OB").slice(-4);
    const session = rectangles.filter(r => r.kind === "AsianRange");
    const chanRect = rectangles.filter(r => r.kind === "ChanZS").slice(-6);
    return {
      rectangles: [...fvg, ...ifvg, ...ob, ...session, ...chanRect].slice(-20),
      priceLines: priceLines.slice(-12),
      markers: markers.slice(-20),
      segments: segments.slice(-140),
    };
  }

  function layoutPriceLabels(items, height, minGap = 14, padding = 6) {
    const rows = items
      .filter(i => Number.isFinite(i.y))
      .slice()
      .sort((a, b) => a.y - b.y);
    for (let i = 0; i < rows.length; i++) {
      rows[i] = { ...rows[i], labelY: Math.max(padding, Math.min(height - padding, rows[i].y)) };
      if (i > 0 && rows[i].labelY - rows[i - 1].labelY < minGap) {
        rows[i].labelY = rows[i - 1].labelY + minGap;
      }
    }
    if (rows.length && rows[rows.length - 1].labelY > height - padding) {
      const overflow = rows[rows.length - 1].labelY - (height - padding);
      for (const row of rows) row.labelY = Math.max(padding, row.labelY - overflow);
      for (let i = 1; i < rows.length; i++) {
        if (rows[i].labelY - rows[i - 1].labelY < minGap) {
          rows[i].labelY = rows[i - 1].labelY + minGap;
        }
      }
    }
    return rows;
  }

  function projectSegment(segment, timeToX, priceToY) {
    const x1 = timeToX(segment.startTime);
    const x2 = timeToX(segment.endTime);
    const y1 = priceToY(segment.startPrice);
    const y2 = priceToY(segment.endPrice);
    if (x1 == null || x2 == null || y1 == null || y2 == null) return null;
    return { x1, x2, y1, y2 };
  }

  function projectRectangle(rect, timeToX, priceToY) {
    const y1 = priceToY(rect.high);
    const y2 = priceToY(rect.low);
    if (y1 == null || y2 == null) return null;
    let x1 = timeToX(rect.startTime);
    let x2 = timeToX(rect.endTime);
    if (x1 == null) x1 = 0;
    if (x2 == null) x2 = Infinity;
    return {
      x1, x2,
      top: Math.min(y1, y2),
      height: Math.abs(y2 - y1),
    };
  }

  return {
    TF_MS, timeOf, pivots, computeSMT, dailyLevels, buildICTData,
    projectRectangle, projectSegment, layoutPriceLabels, palette,
  };
});
