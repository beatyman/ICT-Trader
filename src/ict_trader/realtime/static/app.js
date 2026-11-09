(() => {
  "use strict";

  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const S = {
    snap: null,
    bars: [],
    symbol: null,
    tf: "15m",
    ws: null,
    wsOpen: false,
    phase: "starting",
    chartLoaded: false,
    chartLoading: false,
    mouse: null,
    latencyHistory: [],
    seenSignals: new Set(),
    firstSnapshot: true,
    ema: true,
    notify: true,
    filter: "all",
    fetchSeq: 0,
    aiBusy: false,
    aiReview: null,
    aiHashOpened: false,
    anchorSignalId: null,
    signalLinkOpened: false,
  };

  const TF_MS = { "1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000 };

  function themeColors() {
    const light = document.documentElement.dataset.theme === "light";
    return light ? {
      grid: "rgba(23,44,74,.07)", gridStrong: "rgba(23,44,74,.13)",
      axis: "rgba(75,94,118,.72)", axisFaint: "rgba(75,94,118,.58)",
      volUp: "rgba(7,132,94,.16)", volDown: "rgba(196,59,76,.16)",
      up: "#07845e", down: "#c43b4c",
      upFill: "rgba(7,132,94,.72)", downFill: "rgba(196,59,76,.72)",
      ema20: "rgba(8,114,143,.85)", ema50: "rgba(94,73,180,.78)",
      cross: "rgba(40,60,85,.3)", crossBg: "rgba(255,255,255,.97)",
      crossText: "#26384e", tagText: "#ffffff",
      sig: "#98610b", entry: "rgba(152,97,11,.8)",
      stop: "rgba(196,59,76,.75)", target: "rgba(7,132,94,.75)",
      noData: "rgba(75,94,118,.7)",
    } : {
      grid: "rgba(157,184,214,.055)", gridStrong: "rgba(157,184,214,.11)",
      axis: "rgba(123,138,158,.58)", axisFaint: "rgba(123,138,158,.5)",
      volUp: "rgba(44,229,163,.18)", volDown: "rgba(255,98,115,.18)",
      up: "#2ce5a3", down: "#ff6273",
      upFill: "rgba(44,229,163,.78)", downFill: "rgba(255,98,115,.78)",
      ema20: "rgba(84,200,248,.8)", ema50: "rgba(184,157,255,.72)",
      cross: "rgba(207,222,240,.25)", crossBg: "rgba(18,28,42,.96)",
      crossText: "#cfd9e6", tagText: "#07110e",
      sig: "#f2b84b", entry: "rgba(242,184,75,.75)",
      stop: "rgba(255,98,115,.7)", target: "rgba(44,229,163,.7)",
      noData: "rgba(123,138,158,.65)",
    };
  }
  let CHART = themeColors();

  const COINS = {
    BTCUSDT: ["₿", "coin-btc"],
    ETHUSDT: ["Ξ", "coin-eth"],
    SOLUSDT: ["◎", "coin-sol"],
    BNBUSDT: ["B", "coin-bnb"],
    XRPUSDT: ["X", "coin-xrp"],
    DOGEUSDT: ["Ð", "coin-doge"],
  };

  const STATUS_LABEL = {
    ARMED: "ARMED", EXECUTED: "FILLED", REJECTED: "REJECTED",
    HISTORICAL: "HISTORICAL", CLOSED: "CLOSED",
  };

  function coin(symbol) {
    const key = (symbol || "").toUpperCase();
    return COINS[key] || [key.slice(0, 1), ""];
  }

  function num(value, fallback = 0) {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  }

  function fmtPrice(v) {
    v = num(v);
    if (!v) return "—";
    const a = Math.abs(v);
    let digits = 2;
    if (a < 0.01) digits = 6;
    else if (a < 1) digits = 5;
    else if (a < 100) digits = 3;
    else if (a < 10000) digits = 2;
    else digits = 1;
    return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function fmtMoney(v, digits = 2) {
    v = num(v);
    return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function fmtSigned(v, digits = 2) {
    v = num(v);
    return (v >= 0 ? "+" : "") + fmtMoney(v, digits);
  }

  function fmtQty(v) {
    v = num(v);
    if (!v) return "—";
    if (Math.abs(v) >= 1000) return v.toFixed(2);
    if (Math.abs(v) >= 1) return v.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    return v.toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
  }

  function timeAgo(ms) {
    if (!ms) return "—";
    const sec = Math.max(0, Math.round((Date.now() - Number(ms)) / 1000));
    if (sec < 60) return sec + "s ago";
    if (sec < 3600) return Math.floor(sec / 60) + "m ago";
    if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
    return Math.floor(sec / 86400) + "d ago";
  }

  function clockTime(ms) {
    if (!ms) return "—";
    return new Date(Number(ms)).toLocaleTimeString("en-GB", {
      hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Shanghai",
    });
  }

  function shortDate(ms) {
    if (!ms) return "—";
    return new Date(Number(ms)).toLocaleString("en-GB", {
      month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
      hour12: false, timeZone: "Asia/Shanghai",
    });
  }

  function escapeHtml(str) {
    return String(str ?? "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ------------------------------------------------------------- socket
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${proto}://${location.host}/ws`);
    S.ws = socket;
    socket.onopen = () => {
      S.wsOpen = true;
      setPill("status-ws", true);
      showToast("Stream connected", "Dashboard attached to the live engine.", "success");
    };
    socket.onmessage = evt => {
      try {
        const snap = JSON.parse(evt.data);
        applySnapshot(snap);
      } catch (err) {
        console.warn("bad snapshot", err);
      }
    };
    socket.onerror = () => setPill("status-ws", false);
    socket.onclose = () => {
      S.wsOpen = false;
      setPill("status-ws", false);
      setTimeout(connectWS, 2200);
    };
  }

  async function fetchSnapshot() {
    try {
      const res = await fetch("/api/snapshot", { cache: "no-store" });
      if (res.ok) applySnapshot(await res.json());
    } catch (_) { /* socket will retry */ }
  }

  function setPill(id, ok) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.toggle("is-ok", !!ok);
    el.classList.toggle("is-err", !ok);
  }

  // ------------------------------------------------------------- snapshot
  function applySnapshot(snap) {
    const wasFirst = !S.snap;
    S.snap = snap;
    S.phase = snap.phase || "starting";
    const watch = snap.watchlist || [];
    if (!S.symbol && watch.length) S.symbol = snap.selected || watch[0].symbol;
    if (!S.symbol && (snap.config?.symbols || []).length) S.symbol = snap.config.symbols[0];

    if (!S.firstSnapshot) detectNewSignals(snap.signals || []);
    if (S.firstSnapshot) {
      (snap.signals || []).forEach(x => S.seenSignals.add(x.id));
      S.firstSnapshot = false;
    }

    if (snap.ai?.last_review && !S.aiReview) S.aiReview = snap.ai.last_review;
    if (S.anchorSignalId && !(snap.signals || []).some(s => s.id === S.anchorSignalId)) {
      S.anchorSignalId = null;
    }

    renderAll(snap);
    if (applyLiveBar(snap)) {
      renderChartHeader(snap);
      drawChart();
      if (!S.mouse) showLatestOhlc();
    }

    if (S.phase === "ready" && !S.chartLoaded && !S.chartLoading) {
      loadBars();
    }
    if (location.hash === "#ai" && !S.aiHashOpened) {
      S.aiHashOpened = true;
      openAiModal();
    }
    const linkedSignal = new URLSearchParams(location.search).get("signal");
    if (linkedSignal && !S.signalLinkOpened) {
      S.signalLinkOpened = true;
      const sig = (snap.signals || []).find(s => s.id === linkedSignal);
      if (sig) selectSignal(sig.id);
    }
    if (S.phase === "ready" && wasFirst) {
      showToast("Engine ready", `${snap.config.symbols.length} symbols · paper execution only`, "success");
    }
  }

  function detectNewSignals(signals) {
    const fresh = [];
    for (const s of signals) {
      if (!S.seenSignals.has(s.id)) {
        S.seenSignals.add(s.id);
        if (s.actionable) fresh.push(s);
      }
    }
    for (const s of fresh.slice(0, 2)) {
      showToast(
        `${s.symbol} ${s.direction.toUpperCase()} setup`,
        `Grade ${s.grade} · ${num(s.rr).toFixed(2)}R · ${s.status}`,
        "success",
      );
    }
  }

  function renderAll(snap) {
    renderWatchlist(snap);
    renderKpis(snap);
    renderSetup(snap);
    renderSignals(snap);
    renderPositions(snap);
    renderEvents(snap);
    renderHealth(snap);
    renderChartHeader(snap);
    renderFooter(snap);
    renderPhase(snap);
    if (S.chartLoaded) maybeRedraw(snap);
  }

  function renderPhase(snap) {
    const ready = snap.phase === "ready";
    const err = snap.phase === "error";
    const dot = $("#engineDot");
    dot.className = "dot " + (ready ? "ok" : err ? "err" : "");
    $("#engineLabel").textContent = err ? "ENGINE ERROR" : ready ? "ENGINE READY · PAPER" : "ENGINE BOOTING";
    $("#engineLabel").style.color = err ? "var(--red)" : ready ? "var(--green)" : "var(--amber)";
    const boot = snap.boot || {};
    $("#bootMeta").textContent = err ? "BOOT FAILED" : ready
      ? `${(snap.config.symbols || []).length} SYMBOLS · ${(snap.config.intervals || []).length} INTERVALS`
      : `LOADING ${boot.done || 0}/${boot.total || 0}`;
    if (err) {
      $("#bootMeta").textContent = "BOOT FAILED — SEE EVENT LOG";
      $("#bootMeta").style.color = "var(--red)";
    }
    $("#modeBadge").textContent = snap.mode === "demo" ? "SIM" : "LIVE";
    $("#modeBadge").classList.toggle("live", snap.mode !== "demo");
    $("#feedModeLabel").textContent = snap.mode === "demo" ? "SIMULATED TAPE · ACCELERATED" : "LIVE BINANCE WEBSOCKET";
  }

  // ------------------------------------------------------------- watchlist
  function sparkSvg(values, positive) {
    if (!values || values.length < 2) return "";
    const w = 50, h = 17;
    let min = Math.min(...values), max = Math.max(...values);
    if (!isFinite(min) || !isFinite(max) || max === min) { max = min + 1; }
    const points = values.map((v, i) => {
      const x = (i / (values.length - 1)) * w;
      const y = h - ((v - min) / (max - min)) * (h - 2) - 1;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<svg class="spark-line" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${points}" fill="none" stroke="${positive ? "var(--green)" : "var(--red)"}" stroke-width="1" opacity=".85"/></svg>`;
  }

  function renderWatchlist(snap) {
    const list = snap.watchlist || [];
    $("#marketCount").textContent = list.length;
    if (!list.length) return;
    const html = list.map(row => {
      const [glyph, cls] = coin(row.symbol);
      const change = num(row.change_pct);
      const up = change >= 0;
      return `
        <div class="watch-row ${row.symbol === S.symbol ? "active" : ""}" data-symbol="${escapeHtml(row.symbol)}" role="button" tabindex="0">
          <div class="coin ${cls}">${glyph}</div>
          <div class="watch-main">
            <b>${escapeHtml(row.symbol.replace("USDT", ""))}<span class="quote">/USDT</span></b>
            <span>${escapeHtml((snap.config?.venue || "BINANCE").split(" ")[0])} · SPOT</span>
          </div>
          <div class="watch-price">${fmtPrice(row.price)}</div>
          <div class="watch-delta ${up ? "up" : "down"}">${up ? "+" : ""}${change.toFixed(2)}%</div>
          ${sparkSvg(row.spark, up)}
        </div>`;
    }).join("");
    $("#watchlist").innerHTML = html;
  }

  function renderKpis(snap) {
    const acct = snap.account || {};
    const stats = snap.stats || {};
    $("#kpiEquity").textContent = "$" + fmtMoney(acct.equity || 0);
    const ret = num(acct.return_pct);
    const rEl = $("#kpiReturn");
    rEl.textContent = (ret >= 0 ? "+" : "") + ret.toFixed(2) + "%";
    rEl.className = "delta" + (ret < 0 ? " down" : "");

    const pnl = num(acct.daily_pnl);
    const pEl = $("#kpiPnl");
    pEl.textContent = (pnl >= 0 ? "+$" : "-$") + fmtMoney(Math.abs(pnl));
    pEl.style.color = pnl > 0 ? "var(--green)" : pnl < 0 ? "var(--red)" : "var(--text)";
    $("#pnlMark").textContent = pnl > 0 ? "▲" : pnl < 0 ? "▼" : "·";
    $("#pnlMark").style.color = pnl >= 0 ? "var(--green)" : "var(--red)";
    $("#kpiTrades").textContent = `${stats.trades || 0} closed`;

    const risk = num(acct.open_risk);
    $("#kpiRisk").textContent = "$" + fmtMoney(risk);
    $("#kpiRiskPct").textContent = `${num(acct.open_risk_pct).toFixed(2)}% of equity`;
    $("#kpiRiskCount").textContent = `${(snap.positions || []).length} position${(snap.positions || []).length === 1 ? "" : "s"}`;
    const riskPct = Math.min(100, num(acct.open_risk_pct) * 10);
    $("#riskBar").style.width = riskPct + "%";

    const wr = num(stats.win_rate);
    $("#kpiWinRate").textContent = (stats.trades ? wr.toFixed(1) : "—") + (stats.trades ? "%" : "");
    $("#kpiPf").textContent = "PF " + (stats.profit_factor === 99 ? "∞" : num(stats.profit_factor).toFixed(2));
    $("#kpiAvgR").textContent = "avg R " + (stats.trades ? fmtSigned(stats.avg_r, 2) : "—");
    $("#winBar").style.width = Math.min(100, wr) + "%";

    // Equity sparkline from closed-trade equity points.
    const curve = (snap.equity_curve || []).map(x => num(x.equity));
    const vals = curve.length > 1 ? curve : [num(acct.initial_equity), num(acct.equity)];
    const svg = $("#equitySpark");
    if (vals.length > 1) {
      const min = Math.min(...vals), max = Math.max(...vals);
      const span = max - min || 1;
      const pts = vals.map((v, i) => {
        const x = (i / (vals.length - 1)) * 180;
        const y = 42 - ((v - min) / span) * 36;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(" ");
      svg.innerHTML = `<defs><linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2ce5a3" stop-opacity=".24"/><stop offset="1" stop-color="#2ce5a3" stop-opacity="0"/></linearGradient></defs><polygon points="0,44 ${pts} 180,44" fill="url(#eqfill)"/><polyline points="${pts}" fill="none" stroke="#2ce5a3" stroke-width="1.2" vector-effect="non-scaling-stroke"/>`;
    }

    const trades = (snap.trades || []).slice(-9).reverse();
    $("#pnlBars").innerHTML = trades.map(t => {
      const p = num(t.pnl), h = Math.max(3, Math.min(20, Math.abs(p) / 3 + 3));
      return `<i class="${p < 0 ? "neg" : ""}" style="height:${h}px"></i>`;
    }).join("");
  }

  // ------------------------------------------------------------- setup
  function latestSignal(snap) {
    const signals = snap.signals || [];
    return signals.find(s => ["ARMED", "EXECUTED", "CLOSED", "REJECTED"].includes(s.status)) || signals[0] || null;
  }

  function renderSetup(snap) {
    const sig = latestSignal(snap);
    const content = $("#setupContent");
    const status = $("#setupStatus");
    if (!sig) {
      status.textContent = "WAITING";
      status.className = "setup-status";
      content.innerHTML = `<div class="setup-empty"><div class="scanner"><i></i></div><b>Scanning for ICT confluence</b><span>${escapeHtml(snap.config?.stack || "4h_15m")} · closed-bar decisions only</span></div>`;
      return;
    }
    const dir = sig.direction === "long" ? "LONG" : "SHORT";
    const isLong = sig.direction === "long";
    const statusKey = sig.status || "HISTORICAL";
    const cls = ["ARMED"].includes(statusKey) ? "armed" : ["EXECUTED", "CLOSED"].includes(statusKey) ? "live" : "";
    status.textContent = STATUS_LABEL[statusKey] || statusKey;
    status.className = "setup-status " + cls;
    const score = Math.round((num(sig.quality) || (num(sig.grade) / 7)) * 100);
    const checks = sig.checks || {};
    const chips = ["sweep", "pda", "volume", "session", "targets", "smt"]
      .map(k => `<span class="check-chip ${checks[k] ? "on" : ""}">${k.toUpperCase()}</span>`).join("");
    const [glyph, coinCls] = coin(sig.symbol);
    content.innerHTML = `
      <div class="setup-body">
        <div class="setup-top">
          <div class="setup-symbol"><span class="coin ${coinCls}" style="width:24px;height:24px;font-size:10px">${glyph}</span><b>${escapeHtml(sig.symbol)}</b><span class="setup-dir ${isLong ? "long" : "short"}">${dir}</span></div>
          <div class="setup-score">QUALITY<b>${score}</b></div>
        </div>
        <div class="setup-reason">${statusKey === "HISTORICAL" ? '<span style="color:var(--amber)">Historical sample · not executable.</span> ' : ""}${escapeHtml(sig.reason || "Closed-bar CISD setup")}</div>
        <div class="ladder">
          <div class="ladder-row"><span>ENTRY</span><div class="ladder-track"><i class="en"></i></div><b>${fmtPrice(sig.entry)}</b></div>
          <div class="ladder-row"><span>STOP</span><div class="ladder-track"><i class="sl"></i></div><b style="color:var(--red)">${fmtPrice(sig.stop)}</b></div>
          <div class="ladder-row"><span>TARGET</span><div class="ladder-track"><i class="tp"></i></div><b style="color:var(--green)">${fmtPrice(sig.target)}</b></div>
        </div>
        <div class="setup-metrics">
          <div class="setup-metric"><span>R:R</span><b class="good">${num(sig.rr).toFixed(2)}R</b></div>
          <div class="setup-metric"><span>GRADE</span><b>${num(sig.grade)} / 7</b></div>
          <div class="setup-metric"><span>TF</span><b>${escapeHtml(sig.interval || "15m")}</b></div>
        </div>
        <div class="setup-checks">${chips}</div>
        <div class="setup-foot"><span>${shortDate(sig.bar_time)} · UTC+8</span><span>${escapeHtml(sig.id.slice(-6))}</span></div>
      </div>`;
  }

  // ------------------------------------------------------------- signals
  function renderSignals(snap) {
    const all = snap.signals || [];
    const rows = S.filter === "live"
      ? all.filter(s => s.actionable)
      : all;
    const list = $("#signalList");
    if (!rows.length) {
      list.innerHTML = `<div class="empty-mini">${S.filter === "live" ? "no actionable setup yet" : "awaiting first evaluation"}</div>`;
      return;
    }
    list.innerHTML = rows.slice(0, 8).map(s => {
      const isLong = s.direction === "long";
      const st = (s.status || "").toLowerCase();
      return `
        <div class="signal-item ${s.id === S.anchorSignalId ? "active" : ""}" data-symbol="${escapeHtml(s.symbol)}" data-signal-id="${escapeHtml(s.id)}" title="${escapeHtml(s.reason || "")}">
          <div class="signal-arrow ${isLong ? "long" : "short"}">${isLong ? "↗" : "↘"}</div>
          <div class="signal-main">
            <div class="line1"><b>${escapeHtml(s.symbol.replace("USDT", ""))}</b><em>${isLong ? "LONG" : "SHORT"}</em><em style="color:var(--muted-2)">${shortDate(s.bar_time).split(",")[1] || ""}</em></div>
            <div class="line2"><span>${fmtPrice(s.entry)}</span><span>${num(s.rr).toFixed(2)}R</span><span>G${num(s.grade)}</span></div>
          </div>
          <span class="signal-status ${st}">${STATUS_LABEL[s.status] || s.status || "—"}</span>
        </div>`;
    }).join("");
  }

  // ------------------------------------------------------------- positions
  function renderPositions(snap) {
    const positions = snap.positions || [];
    $("#positionCount").textContent = positions.length;
    $("#openRiskChip").textContent = `RISK ${num(snap.account?.open_risk_pct).toFixed(2)}%`;
    const body = $("#positionsBody");
    const empty = $("#positionsEmpty");
    if (!positions.length) {
      body.innerHTML = "";
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    body.innerHTML = positions.map(p => {
      const pnl = num(p.unrealized);
      const ur = num(p.unrealized_r);
      return `<tr>
        <td class="sym">${escapeHtml(p.symbol)}</td>
        <td><span class="side-tag ${p.direction}">${p.direction}</span></td>
        <td>${fmtPrice(p.entry)}</td>
        <td>${fmtPrice(p.last)}</td>
        <td style="color:var(--red)">${fmtPrice(p.stop)}</td>
        <td style="color:var(--green)">${fmtPrice(p.target)}</td>
        <td>${fmtQty(p.quantity)}</td>
        <td class="${ur >= 0 ? "pos-pnl up" : "pos-pnl down"}">${ur >= 0 ? "+" : ""}${ur.toFixed(2)}R</td>
        <td class="${pnl >= 0 ? "pos-pnl up" : "pos-pnl down"}">${pnl >= 0 ? "+" : ""}$${fmtMoney(Math.abs(pnl))}</td>
      </tr>`;
    }).join("");
  }

  // ------------------------------------------------------------- events
  function renderEvents(snap) {
    const events = (snap.metrics?.events || []).slice(0, 12);
    if (!events.length) {
      $("#eventList").innerHTML = `<div class="empty-mini">no events yet</div>`;
      return;
    }
    $("#eventList").innerHTML = events.map(e => {
      const level = (e.level || "").toLowerCase();
      const t = e.ts ? new Date(e.ts).toLocaleTimeString("en-GB", { hour12: false, timeZone: "Asia/Shanghai" }) : "—";
      return `<div class="event-item ${level}"><time>${t}</time><span><b>${escapeHtml(e.kind || "sys")}</b> · ${escapeHtml(e.message || "")}</span></div>`;
    }).join("");
  }

  // ------------------------------------------------------------- health
  function renderHealth(snap) {
    const h = snap.health || {};
    const feed = h.feed || {};
    const redis = h.redis || {};
    const ch = h.clickhouse || {};
    const wal = h.wal || {};
    const metrics = snap.metrics || {};
    const lat = metrics.latency?.pipeline_ms || {};
    const p50 = num(lat.p50), p99 = num(lat.p99);

    setPill("status-redis", !!redis.ok);
    setPill("status-ch", !!ch.ok);
    if (!S.wsOpen) setPill("status-ws", false); else setPill("status-ws", true);
    $("#healthLabel").textContent = (redis.ok && ch.ok) ? "HEALTHY" : "DEGRADED";
    $("#healthLabel").style.color = (redis.ok && ch.ok) ? "var(--green)" : "var(--red)";

    $("#infra-ws").textContent = snap.mode === "demo" ? "SIM" : `${feed.connections || 0} CONN`;
    $("#infra-ws").classList.toggle("err", snap.mode !== "demo" && !feed.connected);
    $("#infra-ws-sub").textContent = snap.mode === "demo" ? `${feed.streams || 0} synthetic streams` : `${feed.streams || 0} streams · ${feed.reconnects || 0} reconnects`;
    $("#infra-redis").textContent = redis.ok ? `${num(redis.latency_ms).toFixed(2)}ms` : "DOWN";
    $("#infra-redis").classList.toggle("err", !redis.ok);
    $("#infra-redis-sub").textContent = redis.used_memory_human ? `${redis.used_memory_human} used` : "pipeline cache";
    $("#infra-ch").textContent = ch.ok ? `${num(ch.latency_ms).toFixed(1)}ms` : "DOWN";
    $("#infra-ch").classList.toggle("err", !ch.ok);
    $("#infra-ch-sub").textContent = ch.queue ? `${ch.queue} rows queued` : "ReplacingMergeTree";

    const p99El = $("#latencyP99"), p50El = $("#latencyP50");
    p99El.textContent = p99 ? `${p99.toFixed(1)}ms` : "—";
    p50El.textContent = p50 ? `${p50.toFixed(1)}ms` : "—";
    if (p99) {
      S.latencyHistory.push(p99);
      if (S.latencyHistory.length > 22) S.latencyHistory.shift();
    }
    const hist = S.latencyHistory.length ? S.latencyHistory : [0];
    const max = Math.max(...hist, 1);
    $("#latencyBars").innerHTML = hist.map(v => {
      const hh = Math.max(3, Math.round((v / max) * 26));
      return `<i class="${v > 5 ? "hot" : ""}" style="height:${hh}px"></i>`;
    }).join("");
    $("#writeRate").textContent = `${num(metrics.rates?.closed_bars_per_min).toFixed(1)}/m`;
    const used = num(redis.used_memory), maxMem = num(redis.maxmemory);
    $("#redisMem").textContent = redis.used_memory_human || "—";
    $("#redisMemBar").style.width = `${maxMem ? Math.min(100, used / maxMem * 100) : 0}%`;
    $("#walLabel").textContent = wal.pending ? `${wal.pending} WAL rows pending` : "WAL clear";
    $("#walLabel").style.color = wal.pending ? "var(--amber)" : "var(--muted-2)";
    $("#chRows").textContent = `${ch.rows != null ? fmtMoney(ch.rows, 0) : "—"} rows`;
    $("#redisMem").title = maxMem ? `${(used / 1048576).toFixed(0)} / ${(maxMem / 1048576).toFixed(0)} MB` : "";
    $("#sbMsgRate").textContent = `${num(metrics.rates?.ws_msg_per_sec).toFixed(1)}/s`;
    $("#sbP50").textContent = p50 ? `${p50.toFixed(1)}ms` : "—";
    $("#sbP99").textContent = p99 ? `${p99.toFixed(1)}ms` : "—";
    let gaps = 0;
    Object.values(h.sessions || {}).forEach(s => { gaps += num(s.gaps); });
    $("#sbGaps").textContent = String(gaps);
    $("#sbRows").textContent = ch.rows != null ? fmtMoney(ch.rows, 0) : "—";
    const ai = snap.ai || snap.config?.ai || {};
    for (const id of ["aiReviewBtn", "aiReviewBtnTop"]) {
      const aiBtn = $("#" + id);
      if (!aiBtn) continue;
      aiBtn.disabled = !ai.configured || S.aiBusy;
      if (id === "aiReviewBtn") aiBtn.textContent = S.aiBusy ? "REVIEWING" : "REVIEW";
      else aiBtn.textContent = S.aiBusy ? "…" : "AI";
    }
    if (ai.configured) {
      const reviews = num(ai.reviews);
      $("#aiStatus").textContent = `${ai.model_name || "DeepSeek-V4.1-Flash"} · ${reviews} review${reviews === 1 ? "" : "s"}`;
    } else {
      $("#aiStatus").textContent = "API key not configured";
    }
  }

  // ------------------------------------------------------------- chart header
  function renderChartHeader(snap) {
    const row = (snap.watchlist || []).find(x => x.symbol === S.symbol);
    const sym = S.symbol || "—";
    const [glyph, cls] = coin(sym);
    const icon = $("#coinIcon");
    icon.textContent = glyph;
    icon.className = "coin " + cls;
    $("#chartSymbol").textContent = sym;
    const anchorSignal = S.anchorSignalId
      ? (snap.signals || []).find(s => s.id === S.anchorSignalId)
      : null;
    $("#chartPrice").textContent = anchorSignal
      ? fmtPrice(anchorSignal.entry)
      : (row ? fmtPrice(row.price) : "—");
    const ch = num(row?.change_pct);
    const chEl = $("#chartChange");
    if (anchorSignal) {
      chEl.textContent = "SIGNAL BAR";
      chEl.className = "delta";
      chEl.style.color = "var(--amber)";
    } else {
      chEl.textContent = row ? `${ch >= 0 ? "+" : ""}${ch.toFixed(2)}%` : "—";
      chEl.className = "delta" + (ch < 0 ? " down" : "");
      chEl.style.color = "";
    }
    const lastBar = S.bars[S.bars.length - 1];
    const liveEdge = lastBar && !lastBar.is_closed;
    $("#chartSource").textContent = anchorSignal
      ? `${S.tf.toUpperCase()} · SIGNAL CONTEXT`
      : `${S.tf.toUpperCase()} · ${liveEdge ? "LIVE BAR + CLOSED" : "CLOSED BARS"}`;
    const badge = $("#chartAnchor");
    badge.hidden = !anchorSignal;
    if (anchorSignal) badge.textContent = `SIG ${shortDate(anchorSignal.bar_time)}`;
    $("#liveChartBtn").hidden = !anchorSignal;
    const freshness = liveEdge && lastBar?.ingest_time
      ? Math.max(0, num(snap.server_time) - num(lastBar.ingest_time))
      : null;
    $("#chartFeedNote").textContent = anchorSignal
      ? "historical signal context"
      : freshness !== null
        ? `live candle · ${freshness}ms old · closed-bar signals`
        : (snap.mode === "demo" ? "simulated tape · closed-bar signals" : "Binance WS · closed-bar signals");
    $("#sbSymbols").textContent = `${(snap.config?.symbols || []).length} symbols · ${S.tf}`;
  }

  function renderFooter(snap) {
    const p = snap.metrics?.latency?.pipeline_ms || {};
    $("#sbP50").textContent = p.p50 ? `${p.p50.toFixed(1)}ms` : "—";
    $("#sbP99").textContent = p.p99 ? `${p.p99.toFixed(1)}ms` : "—";
  }

  // ------------------------------------------------------------- chart data
  async function loadBars() {
    if (!S.symbol) return;
    if (S.phase !== "ready") return;
    const seq = ++S.fetchSeq;
    const firstLoad = !S.bars.length;
    S.chartLoading = true;
    if (firstLoad) $("#chartLoading").classList.remove("hidden");
    const anchor = S.anchorSignalId
      ? (S.snap?.signals || []).find(s => s.id === S.anchorSignalId)
      : null;
    const endTime = anchor
      ? num(anchor.bar_time) + 70 * (TF_MS[S.tf] || 900000)
      : null;
    const url = `/api/klines?symbol=${encodeURIComponent(S.symbol)}&interval=${encodeURIComponent(S.tf)}&limit=220`
      + (endTime ? `&end_time=${endTime}` : "&include_live=true");
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      if (seq !== S.fetchSeq) return;
      S.bars = data.bars || [];
      S.chartLoaded = S.bars.length > 0;
      S.mouse = null;
      applyLiveBar(S.snap || {});
      renderChartHeader(S.snap || {});
      drawChart();
      if (S.bars.length) showLatestOhlc();
    } catch (err) {
      if (seq === S.fetchSeq) {
        S.chartLoaded = false;
        renderNoData("price feed reconnecting");
        setTimeout(() => { if (S.phase === "ready") loadBars(); }, 3500);
      }
    } finally {
      if (seq === S.fetchSeq) {
        S.chartLoading = false;
        if (S.bars.length) $("#chartLoading").classList.add("hidden");
      }
    }
  }

  function selectSymbol(symbol) {
    if (!symbol) return;
    if (S.symbol === symbol && S.chartLoaded && !S.anchorSignalId) return;
    S.anchorSignalId = null;
    S.symbol = symbol;
    S.chartLoaded = false;
    S.bars = [];
    renderWatchlist(S.snap || {});
    renderChartHeader(S.snap || {});
    renderSignals(S.snap || {});
    loadBars();
  }

  function selectSignal(signalId) {
    const signal = (S.snap?.signals || []).find(s => s.id === signalId);
    if (!signal) return;
    S.anchorSignalId = signal.id;
    S.symbol = signal.symbol;
    if (S.tf !== signal.interval) {
      S.tf = signal.interval;
      $$("#tfTabs .tf").forEach(b => b.classList.toggle("active", b.dataset.tf === S.tf));
    }
    S.chartLoaded = false;
    S.bars = [];
    renderWatchlist(S.snap || {});
    renderChartHeader(S.snap || {});
    renderSignals(S.snap || {});
    loadBars();
  }

  function applyLiveBar(snap) {
    if (S.anchorSignalId || !S.chartLoaded || !S.bars.length) return false;
    const live = (snap.live_bars || []).find(
      b => b.symbol === S.symbol && b.interval === S.tf,
    );
    if (!live) return false;
    const last = S.bars[S.bars.length - 1];
    if (num(live.open_time) === num(last.open_time)) {
      S.bars[S.bars.length - 1] = live;
      return true;
    }
    if (num(live.open_time) > num(last.open_time)) {
      S.bars.push(live);
      if (S.bars.length > 260) S.bars.shift();
      return true;
    }
    return false;
  }

  function showLatestOhlc() {
    const anchor = S.anchorSignalId
      ? (S.snap?.signals || []).find(s => s.id === S.anchorSignalId)
      : null;
    const b = anchor
      ? S.bars.find(x => num(x.open_time) === num(anchor.bar_time))
      : S.bars[S.bars.length - 1];
    if (!b) return;
    const up = b.close >= b.open;
    const prefix = anchor ? "SIG " : "";
    $("#chartOhlc").innerHTML = `<span>${prefix}O <b>${fmtPrice(b.open)}</b></span> <span>H <b>${fmtPrice(b.high)}</b></span> <span>L <b>${fmtPrice(b.low)}</b></span> <span>C <b class="${up ? "up" : "down"}">${fmtPrice(b.close)}</b></span>`;
  }

  // ------------------------------------------------------------- canvas chart
  function setupCanvas() {
    const wrap = $("#chartWrap");
    const canvas = $("#chart");
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(100, wrap.clientWidth);
    const h = Math.max(100, wrap.clientHeight);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function ema(values, period) {
    const out = new Array(values.length).fill(null);
    if (!values.length) return out;
    const k = 2 / (period + 1);
    let prev = values[0];
    out[0] = prev;
    for (let i = 1; i < values.length; i++) {
      prev = values[i] * k + prev * (1 - k);
      out[i] = prev;
    }
    return out;
  }

  function renderNoData(msg) {
    const { ctx, w, h } = setupCanvas();
    ctx.strokeStyle = CHART.grid;
    ctx.lineWidth = 1;
    for (let i = 1; i < 6; i++) {
      ctx.beginPath(); ctx.moveTo(10, i * h / 6); ctx.lineTo(w - 64, i * h / 6); ctx.stroke();
    }
    ctx.fillStyle = CHART.noData;
    ctx.font = "10px Cascadia Code, Consolas, monospace";
    ctx.textAlign = "center";
    ctx.fillText(msg || "no closed bars yet", w / 2, h / 2);
  }

  function chartLevels() {
    if (!S.snap) return [];
    const pos = (S.snap.positions || []).find(p => p.symbol === S.symbol);
    if (pos) {
      return [
        { price: pos.entry, color: CHART.entry, label: "ENTRY", dash: [4, 4] },
        { price: pos.stop, color: CHART.stop, label: "STOP", dash: [4, 4] },
        { price: pos.target, color: CHART.target, label: "TARGET", dash: [4, 4] },
      ];
    }
    const anchored = S.anchorSignalId
      ? (S.snap.signals || []).find(s => s.id === S.anchorSignalId)
      : null;
    const sig = anchored || (S.snap.signals || []).find(
      s => s.symbol === S.symbol && s.status === "ARMED",
    );
    if (sig) {
      return [
        { price: sig.entry, color: CHART.entry, label: "SIGNAL", dash: [3, 5] },
        { price: sig.stop, color: CHART.stop, label: "SL", dash: [3, 5] },
        { price: sig.target, color: CHART.target, label: "TP", dash: [3, 5] },
      ];
    }
    return [];
  }

  function drawChart() {
    if (!S.chartLoaded || !S.bars.length) {
      renderNoData(S.phase === "ready" ? "loading closed bars" : "engine booting");
      return;
    }
    const { ctx, w, h } = setupCanvas();
    $("#chartLoading").classList.add("hidden");
    const bars = S.bars.slice(-190);
    const m = { l: 12, r: 66, t: 16, b: 25 };
    const volH = 43;
    const priceH = h - m.t - m.b - volH - 9;
    const plotW = w - m.l - m.r;
    const step = plotW / bars.length;
    const bodyW = Math.max(1.6, Math.min(8, step * 0.62));

    const levels = chartLevels();
    let lo = Math.min(...bars.map(b => num(b.low)));
    let hi = Math.max(...bars.map(b => num(b.high)));
    levels.forEach(l => {
      if (l.price > 0) { lo = Math.min(lo, l.price); hi = Math.max(hi, l.price); }
    });
    const pad = (hi - lo) * 0.09 || hi * 0.01 || 1;
    lo -= pad;
    hi += pad;
    const yFor = p => m.t + (hi - p) / (hi - lo) * priceH;
    const xFor = i => m.l + (i + 0.5) * step;
    const maxVol = Math.max(...bars.map(b => num(b.volume)));

    // Grid + price axis.
    ctx.font = "9px Cascadia Code, Consolas, monospace";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 5; i++) {
      const y = m.t + (priceH / 5) * i;
      ctx.strokeStyle = i === 5 ? CHART.gridStrong : CHART.grid;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(m.l, Math.round(y) + .5); ctx.lineTo(m.l + plotW, Math.round(y) + .5); ctx.stroke();
      const price = hi - (hi - lo) * (i / 5);
      ctx.fillStyle = CHART.axis;
      ctx.textAlign = "left";
      ctx.fillText(fmtPrice(price), m.l + plotW + 7, y);
    }

    // Vertical time grid.
    const gridEvery = Math.max(24, Math.floor(bars.length / 6));
    for (let i = gridEvery; i < bars.length; i += gridEvery) {
      const x = xFor(i);
      ctx.strokeStyle = CHART.grid;
      ctx.beginPath(); ctx.moveTo(x, m.t); ctx.lineTo(x, m.t + priceH); ctx.stroke();
    }

    // Volume.
    const volTop = m.t + priceH + 10;
    bars.forEach((b, i) => {
      const up = b.close >= b.open;
      const vh = Math.max(1, (num(b.volume) / (maxVol || 1)) * volH);
      ctx.fillStyle = up ? CHART.volUp : CHART.volDown;
      ctx.fillRect(xFor(i) - bodyW / 2, volTop + volH - vh, bodyW, vh);
    });
    ctx.strokeStyle = CHART.grid;
    ctx.beginPath(); ctx.moveTo(m.l, volTop - 5); ctx.lineTo(m.l + plotW, volTop - 5); ctx.stroke();

    // Candles.
    bars.forEach((b, i) => {
      const up = b.close >= b.open;
      const color = up ? CHART.up : CHART.down;
      const x = xFor(i);
      ctx.strokeStyle = color;
      ctx.fillStyle = up ? CHART.upFill : CHART.downFill;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + .5, yFor(num(b.high)));
      ctx.lineTo(Math.round(x) + .5, yFor(num(b.low)));
      ctx.stroke();
      const yo = yFor(num(b.open)), yc = yFor(num(b.close));
      const top = Math.min(yo, yc);
      const bh = Math.max(1.2, Math.abs(yc - yo));
      ctx.fillRect(x - bodyW / 2, top, bodyW, bh);
    });

    // EMAs.
    if (S.ema) {
      const closes = bars.map(b => num(b.close));
      [[20, CHART.ema20], [50, CHART.ema50]].forEach(([period, color]) => {
        const vals = ema(closes, period);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.1;
        ctx.beginPath();
        vals.forEach((v, i) => {
          const x = xFor(i), y = yFor(v);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
      });
    }

    // Position / setup levels.
    levels.forEach(level => {
      if (!(level.price > 0)) return;
      const y = yFor(level.price);
      if (y < m.t || y > m.t + priceH) return;
      ctx.save();
      ctx.setLineDash(level.dash);
      ctx.strokeStyle = level.color;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(m.l, y); ctx.lineTo(m.l + plotW, y); ctx.stroke();
      ctx.restore();
      ctx.fillStyle = level.color;
      ctx.textAlign = "right";
      ctx.font = "7px Cascadia Code, Consolas, monospace";
      ctx.fillText(level.label, m.l + plotW - 4, y - 7);
    });

    // Selected signal must point to the exact source K-line open_time.
    const anchor = S.anchorSignalId
      ? (S.snap?.signals || []).find(s => s.id === S.anchorSignalId)
      : null;
    if (anchor) {
      const idx = bars.findIndex(b => num(b.open_time) === num(anchor.bar_time));
      if (idx >= 0) {
        const x = xFor(idx);
        ctx.save();
        ctx.setLineDash([2, 4]);
        ctx.strokeStyle = CHART.entry;
        ctx.beginPath(); ctx.moveTo(x, m.t); ctx.lineTo(x, m.t + priceH + volH + 7); ctx.stroke();
        ctx.restore();
        const y = yFor(num(anchor.entry));
        ctx.fillStyle = CHART.sig;
        ctx.beginPath();
        ctx.moveTo(x, y - 9);
        ctx.lineTo(x - 4, y - 16);
        ctx.lineTo(x + 4, y - 16);
        ctx.closePath(); ctx.fill();
        ctx.font = "bold 7px Cascadia Code, Consolas, monospace";
        ctx.textAlign = "left";
        ctx.fillText("SIG", x + 5, m.t + 8);
      }
    }

    // Last price tag.
    const last = bars[bars.length - 1];
    const lastY = yFor(num(last.close));
    const upLast = last.close >= last.open;
    ctx.save();
    ctx.setLineDash([2, 4]);
    ctx.strokeStyle = upLast ? CHART.upFill : CHART.downFill;
    ctx.beginPath(); ctx.moveTo(m.l, lastY); ctx.lineTo(m.l + plotW, lastY); ctx.stroke();
    ctx.restore();
    const tagW = 58, tagH = 16;
    ctx.fillStyle = upLast ? CHART.up : CHART.down;
    ctx.fillRect(m.l + plotW + 4, lastY - tagH / 2, tagW, tagH);
    ctx.fillStyle = CHART.tagText;
    ctx.font = "bold 9px Cascadia Code, Consolas, monospace";
    ctx.textAlign = "center";
    ctx.fillText(fmtPrice(last.close), m.l + plotW + 4 + tagW / 2, lastY);

    // Time labels.
    ctx.fillStyle = CHART.axisFaint;
    ctx.font = "8px Cascadia Code, Consolas, monospace";
    ctx.textAlign = "center";
    for (let i = 0; i < bars.length; i += gridEvery) {
      if (i === 0) continue;
      const t = new Date(num(bars[i].open_time));
      const label = S.tf === "1d"
        ? t.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "Asia/Shanghai" })
        : t.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai" });
      ctx.fillText(label, xFor(i), h - 9);
    }

    // Crosshair.
    if (S.mouse && S.mouse.index >= 0 && S.mouse.index < bars.length) {
      const i = S.mouse.index;
      const b = bars[i];
      const x = xFor(i);
      const y = Math.max(m.t, Math.min(m.t + priceH, S.mouse.y));
      ctx.save();
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = CHART.cross;
      ctx.beginPath(); ctx.moveTo(x, m.t); ctx.lineTo(x, m.t + priceH + volH + 7); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(m.l, y); ctx.lineTo(m.l + plotW, y); ctx.stroke();
      ctx.restore();
      const price = hi - (y - m.t) / priceH * (hi - lo);
      ctx.fillStyle = CHART.crossBg;
      ctx.fillRect(m.l + plotW + 3, y - 8, 60, 16);
      ctx.fillStyle = CHART.crossText;
      ctx.font = "8px Cascadia Code, Consolas, monospace";
      ctx.textAlign = "center";
      ctx.fillText(fmtPrice(price), m.l + plotW + 33, y);

      const tip = $("#crosshairTip");
      const chg = (b.close / b.open - 1) * 100;
      tip.innerHTML = `<b>${shortDate(b.open_time)}</b>
        <div class="tip-row"><span>O</span><span>${fmtPrice(b.open)}</span></div>
        <div class="tip-row"><span>H</span><span>${fmtPrice(b.high)}</span></div>
        <div class="tip-row"><span>L</span><span>${fmtPrice(b.low)}</span></div>
        <div class="tip-row"><span>C</span><span style="color:${chg >= 0 ? "var(--green)" : "var(--red)"}">${fmtPrice(b.close)}</span></div>
        <div class="tip-row"><span>VOL</span><span>${fmtMoney(b.volume, 0)}</span></div>`;
      tip.style.display = "block";
      const left = Math.min(w - 150, x + 16);
      const top = Math.max(8, Math.min(h - 128, S.mouse.y - 40));
      tip.style.left = left + "px";
      tip.style.top = top + "px";
      $("#chartOhlc").innerHTML = `<span>O <b>${fmtPrice(b.open)}</b></span> <span>H <b>${fmtPrice(b.high)}</b></span> <span>L <b>${fmtPrice(b.low)}</b></span> <span>C <b class="${chg >= 0 ? "up" : "down"}">${fmtPrice(b.close)}</b></span>`;
    } else {
      $("#crosshairTip").style.display = "none";
    }
  }

  function maybeRedraw(snap) {
    // Redraw only when a visual input changes, not on every 1s heartbeat.
    const pos = (snap.positions || []).find(p => p.symbol === S.symbol);
    const sig = (snap.signals || []).find(s => s.symbol === S.symbol && s.status === "ARMED");
    const lastBar = S.bars[S.bars.length - 1];
    const key = [S.anchorSignalId, pos?.id, pos?.last, sig?.id, sig?.status, lastBar?.close_time].join("|");
    if (key !== S.lastChartKey) {
      S.lastChartKey = key;
      drawChart();
    }
  }

  function chartMouse(evt) {
    if (!S.chartLoaded) return;
    const wrap = $("#chartWrap");
    const rect = wrap.getBoundingClientRect();
    const x = evt.clientX - rect.left;
    const y = evt.clientY - rect.top;
    const m = { l: 12, r: 66, t: 16, b: 25 };
    const plotW = rect.width - m.l - m.r;
    const bars = S.bars.slice(-190);
    const step = plotW / bars.length;
    const idx = Math.floor((x - m.l) / step);
    if (idx < 0 || idx >= bars.length || x > m.l + plotW) {
      S.mouse = null;
    } else {
      S.mouse = { index: idx, x, y };
    }
    drawChart();
  }

  // ------------------------------------------------------------- AI review
  const AI_VERDICT = {
    support: ["支持", "SUPPORT"],
    caution: ["谨慎", "CAUTION"],
    reject: ["否决", "REJECT"],
  };

  function openAiModal() {
    const modal = $("#aiModal");
    modal.hidden = false;
    if (!S.aiReview) {
      $("#aiModalBody").innerHTML = `<div class="ai-empty">点击 REVIEW，让 DeepSeek V4.1 Flash 对当前 ${escapeHtml(S.symbol || "市场")} 的 paper setup、仓位、风险和系统健康做一次只读审查。模型不会下单。</div>`;
    } else {
      renderAiReview(S.aiReview);
    }
  }

  function closeAiModal() {
    $("#aiModal").hidden = true;
  }

  async function runAiReview() {
    if (S.aiBusy) return;
    const configured = S.snap?.ai?.configured ?? S.snap?.config?.ai?.configured;
    if (!configured) {
      showToast("DeepSeek not configured", "Set DEEPSEEK_API_KEY in .env and restart.", "error");
      return;
    }
    S.aiBusy = true;
    openAiModal();
    $("#aiModalBody").innerHTML = `
      <div class="ai-loading">
        <div class="ring"></div>
        <span>DEEPSEEK V4.1 FLASH // REVIEWING ${escapeHtml(S.symbol || "")}</span>
        <small style="color:var(--muted-2);font-size:7px">paper snapshot · closed bars · no order authority</small>
      </div>`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 100000);
    try {
      const res = await fetch("/api/ai/review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: S.symbol }),
        signal: controller.signal,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      S.aiReview = data.review;
      renderAiReview(data.review);
      fetchSnapshot();
      showToast("AI review ready", `${data.review.verdict.toUpperCase()} · ${data.review.confidence}/100`, "success");
    } catch (err) {
      const msg = err.name === "AbortError" ? "request timed out after 100s" : err.message;
      $("#aiModalBody").innerHTML = `<div class="ai-error"><b>REVIEW FAILED</b><br>${escapeHtml(msg)}<br><br>检查 DeepSeek 余额/网络，或稍后重试。API key 不会在浏览器中显示。</div>`;
      showToast("AI review failed", msg, "error");
    } finally {
      clearTimeout(timer);
      S.aiBusy = false;
      renderHealth(S.snap || {});
    }
  }

  function aiList(items) {
    if (!items || !items.length) return "<p>—</p>";
    return `<ul>${items.map(x => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
  }

  function renderAiReview(review) {
    const v = AI_VERDICT[review.verdict] || AI_VERDICT.caution;
    const usage = review.usage || {};
    const tokens = usage.total_tokens ?? "—";
    $("#aiModalBody").innerHTML = `
      <div class="ai-result">
        <div class="ai-verdict-row">
          <div class="ai-verdict-card ${escapeHtml(review.verdict)}">
            <div class="ai-verdict-kicker">RISK VERDICT · ${escapeHtml(review.symbol || S.symbol || "")}</div>
            <div class="ai-verdict-label ${escapeHtml(review.verdict)}">${v[0]} <span style="font-size:11px;opacity:.55">${v[1]}</span></div>
            <div class="ai-headline">${escapeHtml(review.headline || "")}</div>
          </div>
          <div class="ai-conf"><b>${num(review.confidence)}</b><span>CONFIDENCE</span></div>
        </div>
        <div class="ai-summary">${escapeHtml(review.summary || "")}</div>
        <div class="ai-grid">
          <div class="ai-section risk"><h3>RISK FLAGS</h3>${aiList(review.risk_flags)}</div>
          <div class="ai-section notes"><h3>EXECUTION NOTES</h3>${aiList(review.execution_notes)}</div>
          <div class="ai-section"><h3>INVALIDATION</h3><p>${escapeHtml(review.invalidation || "—")}</p></div>
          <div class="ai-section"><h3>DATA QUALITY</h3><p>${escapeHtml(review.data_quality || "—")}</p></div>
        </div>
        <div class="ai-result-foot">
          <span><b>${escapeHtml(review.model_name || "DeepSeek-V4.1-Flash")}</b> · ${num(review.latency_ms).toFixed(0)}ms · ${tokens} tokens</span>
          <span>${escapeHtml(review.reviewed_at || "")} · ${escapeHtml(review.disclaimer || "advisory only")}</span>
        </div>
      </div>`;
  }

  // ------------------------------------------------------------- controls
  function bindControls() {
    $("#watchlist").addEventListener("click", e => {
      const row = e.target.closest(".watch-row");
      if (row) selectSymbol(row.dataset.symbol);
    });
    $("#watchlist").addEventListener("keydown", e => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const row = e.target.closest(".watch-row");
      if (row) { e.preventDefault(); selectSymbol(row.dataset.symbol); }
    });
    $("#signalList").addEventListener("click", e => {
      const row = e.target.closest(".signal-item");
      if (row?.dataset.signalId) selectSignal(row.dataset.signalId);
      else if (row) selectSymbol(row.dataset.symbol);
    });
    $("#tfTabs").addEventListener("click", e => {
      const btn = e.target.closest(".tf");
      if (!btn) return;
      $$("#tfTabs .tf").forEach(b => b.classList.toggle("active", b === btn));
      S.tf = btn.dataset.tf;
      S.anchorSignalId = null;
      S.chartLoaded = false;
      S.bars = [];
      renderChartHeader(S.snap || {});
      renderSignals(S.snap || {});
      loadBars();
    });
    $("#liveChartBtn").addEventListener("click", () => {
      S.anchorSignalId = null;
      S.chartLoaded = false;
      S.bars = [];
      renderChartHeader(S.snap || {});
      renderSignals(S.snap || {});
      loadBars();
    });
    $("#refreshBtn").addEventListener("click", () => {
      fetchSnapshot();
      if (S.symbol) loadBars();
      showToast("Refreshed", "Snapshot and chart reloaded.");
    });
    $("#fitBtn").addEventListener("click", () => {
      S.mouse = null;
      if (S.anchorSignalId) {
        S.anchorSignalId = null;
        S.chartLoaded = false;
        S.bars = [];
        renderChartHeader(S.snap || {});
        renderSignals(S.snap || {});
        loadBars();
      } else {
        drawChart();
      }
    });
    $("#indicatorBtn").addEventListener("click", e => {
      S.ema = !S.ema;
      e.currentTarget.classList.toggle("active", S.ema);
      e.currentTarget.style.color = S.ema ? "var(--cyan)" : "";
      drawChart();
    });
    $("#soundBtn").addEventListener("click", e => {
      S.notify = !S.notify;
      e.currentTarget.style.color = S.notify ? "var(--green)" : "var(--muted)";
      showToast("Notifications", S.notify ? "Signal alerts enabled." : "Signal alerts muted.");
    });
    $("#themeBtn").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      applyTheme(next, true);
    });
    $("#aiReviewBtn").addEventListener("click", runAiReview);
    $("#aiReviewBtnTop").addEventListener("click", runAiReview);
    $("#aiCloseBtn").addEventListener("click", closeAiModal);
    $("#aiModal").addEventListener("click", e => {
      if (e.target.id === "aiModal") closeAiModal();
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && !$("#aiModal").hidden) closeAiModal();
    });
    $("#flattenBtn").addEventListener("click", async () => {
      if (!(S.snap?.positions || []).length) { showToast("No exposure", "There are no paper positions to close."); return; }
      try {
        const res = await fetch("/api/paper/flatten", { method: "POST" });
        const data = await res.json();
        showToast("Flatten sent", `${data.closed || 0} paper position(s) closed.`, "success");
        fetchSnapshot();
      } catch (_) { showToast("Flatten failed", "Paper API unavailable.", "error"); }
    });
    const filter = $("#signalFilter");
    filter.addEventListener("click", e => {
      const btn = e.target.closest("button");
      if (!btn) return;
      S.filter = btn.dataset.filter;
      $$("#signalFilter button").forEach(b => b.classList.toggle("active", b === btn));
      renderSignals(S.snap || {});
    });
    $$(".nav-item").forEach(btn => {
      btn.addEventListener("click", () => {
        $$(".nav-item").forEach(b => b.classList.toggle("active", b === btn));
        const view = btn.dataset.view;
        if (view === "signals") $(".signals-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
        else if (view === "risk") $(".positions-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
        else if (view === "replay") {
          showToast("Replay", S.snap?.mode === "demo"
            ? "Demo tape is running at accelerated speed. See DEMO_SPEED in .env."
            : "Switch to demo with: python -m ict_trader.realtime --mode demo");
        } else {
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      });
    });
    const wrap = $("#chartWrap");
    wrap.addEventListener("mousemove", chartMouse);
    wrap.addEventListener("mouseleave", () => { S.mouse = null; drawChart(); });
    if ("ResizeObserver" in window) {
      new ResizeObserver(() => { if (S.chartLoaded) drawChart(); }).observe(wrap);
    }
    window.addEventListener("resize", () => { if (S.chartLoaded) drawChart(); });
    setInterval(() => {
      if (!S.wsOpen) fetchSnapshot();
    }, 5000);
    // Historical bars refresh slowly; the latest candle is merged from the
    // 1 Hz snapshot, so the chart is not dependent on this poll.
    setInterval(() => {
      if (S.phase === "ready") loadBars();
    }, 60000);
  }

  function showToast(title, message, kind = "") {
    if (!S.notify && kind !== "error") return;
    const stack = $("#toastStack");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.innerHTML = `<b>${escapeHtml(title)}</b><div style="color:var(--muted);margin-top:3px">${escapeHtml(message)}</div>`;
    stack.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transform = "translateY(5px)"; }, 3600);
    setTimeout(() => el.remove(), 4100);
  }

  function applyTheme(theme, persist = true) {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    if (persist) {
      try { localStorage.setItem("ict-theme", next); } catch (_) { /* private mode */ }
    }
    CHART = themeColors();
    const btn = $("#themeBtn");
    if (btn) {
      btn.innerHTML = next === "light"
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M20.5 14.2A8.2 8.2 0 0 1 9.8 3.5 8.5 8.5 0 1 0 20.5 14.2Z"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
      btn.setAttribute("aria-label", next === "light" ? "Switch to dark theme" : "Switch to light theme");
    }
    if (S.chartLoaded) drawChart();
  }

  function clock() {
    const el = $("#clock");
    if (!el) return;
    el.textContent = new Date().toLocaleTimeString("en-GB", {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Shanghai",
    });
  }

  function init() {
    applyTheme(document.documentElement.dataset.theme || "dark", false);
    bindControls();
    clock();
    setInterval(clock, 1000);
    connectWS();
    fetchSnapshot();
    renderNoData("connecting to engine");
  }

  init();
})();
