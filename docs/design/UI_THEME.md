# UI 与双主题设计（Lightweight Charts，无 Node 构建）

> 现状：单页暗色终端，`static/index.html` 14.5 KB、`styles.css` 41.2 KB、`app.js` 46.1 KB，
> 无构建步骤、无第三方图表库。**双主题尚未实现**（无 `data-theme`、无 `localStorage`），
> Canvas 颜色硬编码在 `app.js`（如 `rgba(44,229,163,.78)`、`#2ce5a3`）。
> 图表是原生 Canvas 2D：`devicePixelRatio` 上限 2，绘制最近 190 根 bar，`/api/klines` 取 240 根。
> 页面切换为 `overview / signals / risk / replay` 四个 nav；Setup 详情、统计、错误、性能页
> **均不存在**。

## 1. 技术选型（已定，不再摇摆）

- 使用 **Lightweight Charts**（本地 vendored）；不引入 ECharts / Node 构建；
- 后端下发 candles + levels/signals；EMA overlay 仍由浏览器计算，目标期改为后端 overlay；
- 双主题 = CSS 设计 token + `localStorage` 持久化 + `data-theme` 属性切换，切换不重载；
- 首屏仍是纯静态文件，FastAPI 直接 `FileResponse`，无 SSR。

## 2. Token 分层

现有 23 个 token 直接沿用，避免重写 41 KB CSS：
`--bg --bg-2 --panel --panel-2 --panel-solid --line --line-strong --text --muted --muted-2
--green --green-soft --red --red-soft --amber --amber-soft --cyan --blue --violet
--mono --sans --radius --shadow`。

分层规则：
1. **原始色板层**（只出现在 `:root[data-theme=...]` 里）：`--bg`、`--panel`、`--text` 等；
2. **语义层**（组件只用语义名）：`--up`/`--down` 映射到 `--green`/`--red`，
   新增 `--chart-grid --chart-axis --chart-crosshair --zone-bull --zone-bear --zone-neutral
   --marker-entry --marker-tp --marker-sl --label-bg`；
3. **Canvas 专用层**：Canvas 需要带 alpha 的颜色，用独立 token 承载
   （`--zone-bull-fill: rgba(44,229,163,.14)` 等），JS 只读 token，不再写死 rgba。

亮色建议值（需做对比度与截图回归，不视为最终）：
`--bg #f4f7fb`、`--panel-solid #ffffff`、`--text #0f1b2a`、`--muted #5a6b80`、
`--line rgba(16,32,52,.10)`、`--green #0f8f63`、`--red #c0354a`、`--amber #a8730a`、
`--cyan #0b7ea6`、`--blue #3a5bd9`、`--violet #6b4fc4`。
正文对比度按 WCAG AA ≥4.5:1 校验；`--green/--red` 用作小号文字时需复核（当前值可能低于 4.5）。

## 3. 切换与持久化

- `document.documentElement.dataset.theme = "dark" | "light"`；CSS 写
  `:root[data-theme="light"] { ... }`，暗色为默认 `:root`，保持现有页面不变；
- `localStorage` 键固定 `ict.theme`；首次访问取 `prefers-color-scheme`，
  用户手动切换后不再跟随系统（仅在无存储值时监听 `matchMedia` change）；
- 防闪烁：在 `<head>` 的样式表**之前**放一段内联脚本读取 `localStorage` 并设置
  `data-theme`；脚本内部 `try/catch`（隐私模式下 `localStorage` 可能抛错）；
- 切换按钮放 nav 末尾，`aria-pressed` + `title`，点击后只做三件事：
  写 `data-theme`、写 `localStorage`、调用一次 `applyChartPalette()` + `drawChart()`；
- 过渡只作用于 `body` 的 `background-color/color`（150 ms），不要 `transition: all`，
  避免 Canvas 区域出现布局抖动。

## 4. Canvas 主题化

- `applyChartPalette()`：`getComputedStyle(document.documentElement)` 读一次全部 Canvas token，
  存到模块级 `PALETTE` 对象；主题切换、`resize` 时调用，**不要**在 `drawChart()` 里逐帧读；
- 用 `PALETTE.zoneBullFill` 等替换当前 `app.js` 里硬编码的 20 余处 rgba；
  涨跌色、网格、坐标轴、crosshair、标注文字各用一个 token；
- 绘制顺序固定：网格 → 云/区间（FVG/OB/中枢）→ 蜡烛 → 均线/level 线 → markers →
  labels → crosshair；每层 `ctx.save()/restore()`，避免样式泄漏；
- `devicePixelRatio` 继续 `min(2, dpr)`；高 DPI 下 1600×520 的 backbuffer 约 3200×1040×4B ≈ 13 MB，
  是单页最大内存项，不再放大；
- 标注预算（每帧）：区间 ≤60、水平线 ≤40、marker ≤80、文字标签默认只画选中/悬停项，
  其余用色块；超出预算丢弃最旧（按 `confirmed_at`）；
- 标签用 `ctx.measureText` 截断，禁止自动换行；价格轴用固定 6 位有效数字。

## 5. 页面与区块

| 区块 | 现状 | 目标 |
|---|---|---|
| 图表标注 | 只有 entry/stop/target 与信号线（`app.js` 638–648 行） | 叠加 FVG/OB/PD array/云层/中枢/Wyckoff 事件，后端下发 overlays |
| Setup 详情 | 只有一张 setup 卡（grade/quality/checks） | 新页：planned vs actual、成员信号+evidence、AI 决策与状态、`tp_sl_events` 时间线、版本号 |
| 统计 | 仅 `account/stats` 汇总卡 | 新页：权益/回撤、胜率+Wilson 区间、PF、期望、按 engine/symbol/interval/session 下钻；SIM 数据必须显示隔离标记 |
| 错误 | 只有 `events` 事件流 | 新页：按 module/severity 的趋势、Top 消息、MTTR、resolved 切换 |
| 性能 | 仅 infra 面板的 health/queue | 新页：每引擎 P50/P99、超时率、队列深度、CH batch、loop lag |

新增只读接口（待实现，均走 repository 查询，不阻塞事件循环）：
`GET /api/setups`、`GET /api/setups/{id}`、`GET /api/stats`、`GET /api/errors`、`GET /api/perf`。
所有查询参数带 `from/to`（UTC ms）与 `limit` 上限；响应里带 `as_of_ms` 与 `revision`。

## 6. WebSocket 数据聚合

现状：`api.py::broadcast_loop` 每 `BROADCAST_SECONDS=1.0` 把 `engine.snapshot()` 全量广播给所有
连接；快照含 30 条 signals、40 条 trades、160 点 equity、positions、events、health、
`metrics`、以及**全部** session 明细。50 symbols × 3 intervals 时 sessions 有 150 项，
仅这一项每秒就可能几十 KB。当前 payload 大小**未测量**，需先加埋点（`needs measurement`）。

聚合方案（分两步，不换协议栈）：
1. 轻改：快照增加 `rev`（每区块单调计数），客户端跳过未变化区块；sessions 只发当前选中 symbol；
   events 上限降到 20；`signals` 只发 top 20；
2. 拆分：`/ws` 支持客户端订阅消息（当前服务端只读消息以探测断开，需加协议）：
   `{"type":"sub","topics":["core","positions","chart:BTCUSDT:15m"]}`；
   `chart` 只在 `bar_closed` 时推单根 bar（`symbol/interval/bar`），不再整包；
   历史 bar 走 `/api/klines`（首屏 240，切 symbol/interval 才重新拉）。

服务端约束：广播前 `orjson.dumps` 一次复用（已是此实现）；单连接发送失败即移除；
发送慢的客户端不能拖住广播循环（当前串行 `await send_text`，客户端多时要加 per-connection 队列与超时）。

## 7. 内存与体积

- 不新增前端依赖；预算：`app.js` ≤120 KB（现 46 KB）、`styles.css` ≤60 KB（现 41 KB）、
  首屏 gzip 后 ≤150 KB；
- 客户端数组全部有界：bars ≤240、signals ≤80、trades ≤40、equity ≤160、events ≤50，
  用 ring buffer 覆盖，不做历史累积；
- 标签页隐藏时 `visibilitychange` 暂停重绘与 WS 处理，回到前台再补一次快照；
- 主题切换不重建 DOM、不重拉数据，仅重读 token 与重绘一次；
- 服务端：overlays 查询结果按 `(symbol, interval, last_bar_time)` 缓存，单条 ≤64 KB，
  TTL 到下一根 bar 收盘。

## 8. 验收与回归

- 亮/暗两套截图（overview、setup、stats、errors、perf）对比，无低对比度文本；
- 主题切换 <100 ms，无整页重载，`localStorage` 刷新后保持；
- `prefers-color-scheme` 在无存储值时生效；
- WS 单条消息大小、首屏请求数与 bytes 有实测记录（`needs measurement`）；
- 150 条 stream 时 sessions 不再全量下发，单条广播 <32 KB（目标，待实测）；
- 无新依赖、无 Node 工具链进入 CI。
