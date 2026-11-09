(function (root) {
  "use strict";

  function createOverlayPrimitive() {
    const state = {
      chart: null, series: null, requestUpdate: null,
      data: { rectangles: [], segments: [], lines: [], axisLines: [] },
    };

    // View layer: business data -> CSS-pixel coordinates.
    function coordinates() {
      const ts = state.chart?.timeScale();
      if (!ts || !state.series) return null;
      const width = (() => {
        try { return ts.width(); } catch (_) { return 0; }
      })() || document.getElementById("lwcChart")?.clientWidth || 1000;
      return {
        width,
        toX: t => {
          const x = ts.timeToCoordinate(t);
          return x == null ? null : x;
        },
        toY: p => {
          const y = state.series.priceToCoordinate(Number(p));
          return y == null ? null : y;
        },
      };
    }

    function projectRectangles() {
      const c = coordinates();
      if (!c) return [];
      const out = [];
      for (const z of (state.data.rectangles || [])) {
        const p = window.ICTMath?.projectRectangle(z, c.toX, c.toY);
        if (!p || p.x1 == null) continue;
        let { x1, x2, top, height } = p;
        if (x2 == null || !Number.isFinite(x2)) x2 = c.width;
        if (x2 < x1) [x1, x2] = [x2, x1];
        if (x2 < 0 || x1 > c.width) continue;
        out.push({ ...z, x1, x2, top, height: Math.max(1, height) });
      }
      return out;
    }

    function projectSegments() {
      const c = coordinates();
      if (!c) return [];
      const out = [];
      for (const seg of (state.data.segments || [])) {
        const p = window.ICTMath?.projectSegment(seg, c.toX, c.toY);
        if (!p) continue;
        if (Math.max(p.x1, p.x2) < 0 || Math.min(p.x1, p.x2) > c.width) continue;
        out.push({ ...seg, ...p });
      }
      return out;
    }

    function projectLines() {
      const c = coordinates();
      if (!c) return [];
      const out = [];
      for (const l of (state.data.lines || [])) {
        const y = c.toY(l.price);
        if (y == null) continue;
        let x1 = c.toX(l.startTime);
        let x2 = c.toX(l.endTime);
        if (x1 == null) x1 = 0;
        if (x2 == null) x2 = c.width;
        if (x2 < 0 || x1 > c.width) continue;
        out.push({ ...l, x1, x2, y });
      }
      return out;
    }

    // Renderer layer: canvas calls only; no chart/series/price access.
    function drawRectangles(pixels, target) {
      target.useBitmapCoordinateSpace(scope => {
        const ctx = scope.context, hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio;
        for (const z of pixels) {
          const x1 = z.x1 * hr, x2 = z.x2 * hr;
          const top = z.top * vr, height = z.height * vr;
          ctx.fillStyle = z.fill;
          ctx.fillRect(x1, top, x2 - x1, height);
          ctx.strokeStyle = z.border;
          ctx.lineWidth = Math.max(1, hr);
          ctx.setLineDash([3 * hr, 3 * hr]);
          ctx.strokeRect(x1, top, x2 - x1, height);
          ctx.setLineDash([]);
        }
      });
    }

    function drawLines(lines, segments, target) {
      target.useBitmapCoordinateSpace(scope => {
        const ctx = scope.context, hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio;
        for (const seg of segments) {
          ctx.strokeStyle = seg.color || "rgba(120,123,134,.7)";
          ctx.lineWidth = Math.max(1, (seg.width || 1) * hr);
          ctx.setLineDash(seg.dash ? [5 * hr, 4 * hr] : []);
          ctx.beginPath();
          ctx.moveTo(seg.x1 * hr, seg.y1 * vr);
          ctx.lineTo(seg.x2 * hr, seg.y2 * vr);
          ctx.stroke();
          ctx.setLineDash([]);
        }
        for (const l of lines) {
          ctx.strokeStyle = l.color;
          ctx.lineWidth = Math.max(1, hr);
          ctx.setLineDash([5 * hr, 4 * hr]);
          ctx.beginPath();
          ctx.moveTo(l.x1 * hr, l.y * vr);
          ctx.lineTo(l.x2 * hr, l.y * vr);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      });
    }

    function drawLabels(pixels, target) {
      target.useBitmapCoordinateSpace(scope => {
        const ctx = scope.context, hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio;
        ctx.font = `${9 * hr}px Cascadia Code, Consolas, monospace`;
        ctx.textAlign = "right";
        ctx.textBaseline = "bottom";
        for (const z of pixels) {
          if (!z.label) continue;
          ctx.fillStyle = z.labelColor || z.border;
          ctx.fillText(z.label, z.x2 * hr - 3 * hr, z.top * vr - 2 * vr);
        }
      });
    }

    const views = [
      {
        zOrder: () => "bottom",
        renderer: () => {
          const pixels = projectRectangles();
          return { draw: target => drawRectangles(pixels, target) };
        },
      },
      {
        zOrder: () => "normal",
        renderer: () => {
          const lines = projectLines();
          const segments = projectSegments();
          return { draw: target => drawLines(lines, segments, target) };
        },
      },
      {
        zOrder: () => "top",
        renderer: () => {
          const pixels = projectRectangles();
          return { draw: target => drawLabels(pixels, target) };
        },
      },
    ];
    return {
      attached(param) {
        state.chart = param.chart;
        state.series = param.series;
        state.requestUpdate = param.requestUpdate;
      },
      detached() {},
      paneViews() { return views; },
      priceAxisViews() {
        return (state.data.axisLines || []).map(l => ({
          coordinate: () => state.series
            ? state.series.priceToCoordinate(Number(l.price)) : null,
          text: () => l.axisLabelText || `${l.label} ${Number(l.price).toFixed(2)}`,
          textColor: () => l.color || "#c9d2dc",
          backColor: () => "rgba(7,12,18,.92)",
          visible: () => true,
          renderer: () => ({
            draw: target => target.useBitmapCoordinateSpace(scope => {
              const ctx = scope.context;
              const y = (state.series?.priceToCoordinate(Number(l.price)) ?? -1)
                * scope.verticalPixelRatio;
              if (y < 0 || y > scope.bitmapSize.height) return;
              const text = l.axisLabelText || `${l.label} ${Number(l.price).toFixed(2)}`;
              ctx.font = `${7 * scope.horizontalPixelRatio}px Cascadia Code, Consolas, monospace`;
              const w = ctx.measureText(text).width + 6 * scope.horizontalPixelRatio;
              ctx.fillStyle = "rgba(7,12,18,.92)";
              ctx.fillRect(0, y - 8 * scope.verticalPixelRatio, w, 13 * scope.verticalPixelRatio);
              ctx.fillStyle = l.color || "#c9d2dc";
              ctx.textAlign = "left";
              ctx.textBaseline = "middle";
              ctx.fillText(text, 3 * scope.horizontalPixelRatio, y);
            }),
          }),
        }));
      },
      updateAllViews() {},
      setData(data) {
        state.data = data || { rectangles: [], segments: [], lines: [], axisLines: [] };
        state.requestUpdate?.();
      },
    };
  }


  root.ICTOverlayPrimitive = { create: createOverlayPrimitive };
})(typeof window !== "undefined" ? window : globalThis);
