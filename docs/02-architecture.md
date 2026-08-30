# 第二章 系统架构与模块划分

## 2.1 整体架构概览

系统运行在 GitHub Actions 上，由 cron 定时触发，零成本、免鉴权。核心设计原则：

- **配置驱动**：所有阈值、周期列表、并发参数写入 `config.yaml`，代码零硬编码
- **双源容错**：Binance USDⓈ-M Futures 公开 API 为主，OKX v5 为兜底
- **四周期独立扫描 + 交叉确认**：15m / 1h / 4h / 1d 各自跑完整识别流程，再做跨周期共振打分
- **状态持久化**：actions/cache 为主 + `state/` JSON 文件 git 回写为兜底

### 数据流总览

```
[GitHub Actions Cron 触发 / 手动 workflow_dispatch]
        │
        ▼
┌──────────────────────────────┐
│ ① 行情获取模块 (MarketData)   │
│   - Binance /fapi (主)        │
│   - OKX /api/v5 (兜底)        │
│   - 输出: Top300 标的 × OHLCV   │
└──────────┬───────────────────┘
           │ raw klines (list of [time,O,H,L,C,V])
           ▼
┌──────────────────────────────┐
│ ② 指标计算模块 (Indicators)   │
│   - ZigZag 摆动点提取         │
│   - ATR(14) Wilder 平滑       │
│   - Volume MA(20)             │
│   - EMA20/EMA50 趋向          │
│   - 输出: pivots + indicators  │
└──────────┬───────────────────┘
           │ structured data
           ▼
┌──────────────────────────────┐
│ ③ 形态识别引擎 (PatternEngine)│
│   - 17 种检测器并行调用        │
│   - 每个 (symbol, interval)    │
│     独立扫描                   │
│   - 输出: Pattern[] 候选列表    │
└──────────┬───────────────────┘
           │ candidates (可能多个/空)
           ▼
┌──────────────────────────────┐
│ ④ 多周期交叉确认 (CrossTF)     │
│   - 大周期方向校验             │
│   - 共振加分 / 矛盾扣分        │
│   - 小周期领先确认             │
│   - 输出: confirmed Signal[]   │
└──────────┬───────────────────┘
           │ scored signals
           ▼
┌──────────────────────────────┐
│ ⑤ 信号过滤与去重 (Filter)      │
│   - 量能 / 突破幅度 / 噪音过滤  │
│   - 冷却期去重                 │
│   - R:R ≥ 1.5 校验            │
│   - 强度分档 (≥75强/60~75中)   │
│   - 输出: final Signal[]       │
└──────────┬───────────────────┘
           │ actionable signals
           ▼
┌──────────────────────────────┐
│ ⑥ 推送与渲染 (Notifier)        │
│   - 企业微信 Webhook           │
│     · markdown 正文消息         │
│     · image 消息(base64+md5)   │
│   - matplotlib Agg 渲染标注图   │
│   - 状态文件更新               │
└──────────────────────────────┘
```

---

## 2.2 模块详细设计

### 2.2.1 行情获取模块 (MarketData)

#### 数据源：币安为主 + OKX 兜底

两个数据源均为**公开 REST API，无需任何鉴权**。

##### Binance USDⓈ-M Futures（主源）

| 端点 | 用途 | Weight | 单次返回 |
|------|------|--------|---------|
| `GET /fapi/v1/exchangeInfo` | 合约列表与交易规则 | 1 | 全部 symbol |
| `GET /fapi/v1/ticker/24hr` | 全量 24h 统计 | ~40 | 全部 ticker（不带 symbol） |
| `GET /fapi/v1/klines` | K线数据 | **2** | 最多 1000 根 |

**限流预算（关键计算）：**

```
IP 限制: 2400 weight / 分钟
目标: 300 标的 × 4 周期 = 1200 次 klines 请求
      1200 × 2 weight = 2400 weight → 正好打满！
```

这意味着**没有任何余量给重试和 ticker 请求**，必须精心调度：

```pseudo
function fetchAllKlines(symbols, intervals, cfg):
    results = {}
    batch = []
    for sym in symbols:
        for iv in intervals:
            batch.append((sym, iv))

    # 分批执行，每批 cfg.batch_size 个请求
    for chunk in chunks(batch, cfg.batch_size):
        futures = []
        for (sym, iv) in chunk:
            f = executor.submit(fetchSingleKline, sym, iv, cfg)
            futures.append(f)
        # 等待本批全部完成
        wait(futures, timeout=cfg.timeout_seconds)
        # 批间暂停，释放 weight 预算
        if chunk is not last:
            sleep(cfg.batch_pause_sec)

    return results
```

**推荐节流参数：**

| 参数 | 推荐值 | 理由 |
|------|-------|------|
| concurrency | 3 | 3 并发 × (200ms 响应 + 100ms 间隔) ≈ 900ms/批，50 个/批 → 45s/批 |
| request_interval_ms | 100 | 缓冲，防止瞬时突发触发 429 |
| batch_size | 50 | 50 × 3并发 = 150 请求，约消耗 300 weight |
| batch_pause_sec | 2 | 让 weight 计数器回退 |
| 预计总耗时 | ~120~180 秒 | 含网络延迟和批间暂停 |

加上 exchangeInfo(1) + ticker24hr(40)，总计约 **2441 weight**——略超 2400 的硬限制。解决方案：
1. **ticker 用缓存**：exchangeInfo 和 ticker 每 6 小时才刷新一次（标的列表不会频繁变动），存入 cache
2. **klines 错峰**：如果某次 run 的 ticker 权重还没回退完，自动降低 concurrency 到 2

##### OKX API v5（兜底源）

| 端点 | 用途 | 限制 |
|------|------|------|
| `/api/v5/market/tickers?instType=SWAP` | 全量 SWAP ticker | 20 req / 2s |
| `/api/v5/market/candles` | K线 | 20 req / 2s, limit ≤ 100 |

OKX 的限制是请求数而非 weight，更宽松。但单次最多返回 100 根 K 线（vs Binance 1000），长周期需要分页。

**切换逻辑：**

```pseudo
function fetchKlinesWithFallback(symbol, interval, neededBars):
    try:
        data = binanceGetKlines(symbol, interval, limit=neededBars)
        return normalizeToStandard(data, source="binance")
    except RateLimitError as e:
        log("Binance rate limited, backing off", e.retryAfter)
        sleep(e.retryAfter or exponentialBackoff(attempt))
        retry up to 3 times
    except (TimeoutError, ConnectionError) as e:
        log("Binance unreachable, falling back to OKX", e)
        try:
            data = okxGetKlines(symbol, interval, limit=100)
            # OKX 最多 100 根，需要分页补齐
            while len(data) < neededBars:
                more = okxGetKlines(symbol, interval,
                    limit=100, before=data[0].time - 1)
                if not more: break
                data = more + data              # OKX 返回倒序，新在前
                sleep(150ms)                    # OKX 限流
            return normalizeToStandard(data[:neededBars], source="okx")
        except:
            log("Both sources failed for", symbol, interval)
            return null                        # 该对跳过，不阻塞整体
```

#### Top300 标的选取

```pseudo
function getTopSymbols(limit=300):
    tickers = binanceGetAll24hrTickers()       # 一次 HTTP 调用拿全部
    pairs = []
    for t in tickers:
        if not t.symbol.endswith("USDT"): continue
        if t.quoteVolume < MIN_VOLUME_USDT: continue   # 过滤低流动性
        pairs.append(t)

    sort(pairs, key=quoteVolume, desc=True)
    return pairs[:limit].map(symbol)
```

过滤规则：
- 仅 `quoteAsset == "USDT"` 且 `contractType == "PERPETUAL"`
- 排除 `status != "TRADING"`
- 最低 24h 成交额 `10,000,000 USDT`（排除僵尸合约）

#### 各周期所需 K 线数量

| 周期 | 需要根数 | 覆盖时间 | 设计理由 |
|------|---------|---------|---------|
| 15m | 480 | 5 天 | 头肩形态通常需 2~5 天形成；480 根足够 ZigZag 提取 30+ 摆动点 |
| 1h | 240 | 10 天 | 中等形态（双顶/三角形）的典型跨度 |
| 4h | 120 | 20 天 | 较大级别形态（日线级头肩） |
| 1d | 60 | 60 天 | 长期趋势中的大形态 |

Binance 单次最多返回 1000 根，以上均在一次请求内完成。
OKX 单次上限 100 根，15m 需要 5 次分页请求。

---

### 2.2.2 指标计算模块 (Indicators)

输入：原始 K 线数组 `List[Kline]`
输出：结构化指标对象 `IndicatorSet`

```pseudo
struct IndicatorSet:
    pivots: List[Pivot]          # ZigZag 摆动点
    atr: float                   # ATR(14) 最新值
    atrHistory: List[float]      # ATR 序列（用于趋势线突破幅度归一化）
    volumeMa: float              # 成交量 20 周期均值
    ema20: float                 # EMA(20)
    ema50: float                 # EMA(50)
    trendDirection: Enum{UP, DOWN, SIDEWAYS}
```

**关键设计决策：**
- 所有价格类指标基于**收盘价**计算（不用 typical price `(H+L+C)/3`，避免插针干扰）
- ATR 使用 Wilder 平滑（`alpha = 1/period`），不是简单 SMA
- 趋向判断用 EMA20 vs EMA50 的位置关系（金叉/死叉），简单且跨周期一致
- Volume MA 用算术平均（不用 EMA），因为量能突发放大比平滑更重要

---

### 2.2.3 形态识别引擎 (PatternEngine)

**核心原则：每对 (symbol, interval) 完全独立扫描。**

17 种检测函数之间无依赖关系，可以并行调用（在 Python 中用线程池或简单的循环）：

```pseudo
function scanSymbolInterval(klines, cfg):
    indicators = calcIndicators(klines, cfg)

    candidates = []
    for detector in ALL_DETECTORS:              # 17 个检测器实例
        result = detector.detect(
            pivots=indicators.pivots,
            klines=klines,
            atr=indicators.atr,
            volMa=indicators.volumeMa,
            trend=indicators.trendDirection,
            params=cfg.patterns.get(detector.name)
        )
        if result is not null:
            candidates.append(result)

    # 同一 (symbol,interval) 可能匹配多种形态
    # 例如：同时检测到"对称三角形候选"和"上升旗形候选"
    return candidates
```

**Pattern 对象结构（贯穿全系统的核心数据结构）：**

```pseudo
struct Pattern:
    symbol: string                  # "BTCUSDT"
    interval: string                # "4h"
    patternType: string             # "head_shoulders_top"
    direction: Enum{LONG, SHORT}    # 看涨/看跌
    confidence: float               # 0~1 几何完整度

    # === 几何坐标（供图表渲染使用）===
    pivotPoints: List[Pivot]        # 参与构成该形态的所有摆动点
    neckline: Line|null             # 颈线（如有）
    upperBoundary: Line|null        # 上边界（三角形/旗形/楔形）
    lowerBoundary: Line|null        # 下边界
    breakoutIndex: int              # 突破确认的 K 线索引
    breakoutPrice: float            # 突破价

    # === 交易信号（经第四章计算后填充）===
    entryPrice: float|null          # 入场价
    stopLossPrice: float|null       # 止损价
    takeProfit1: float|null         # 第一目标位
    takeProfit2: float|null         # 第二目标位
    riskRewardRatio: float|null     # 风险回报比
    strengthScore: int|null         # 0~100 信号强度

    detectedAt: datetime            # 检测时间戳
```

---

### 2.2.4 多周期交叉确认 (CrossTimeframeConfirm)

这是整套系统中最有"智能感"的模块。四周期各自独立跑完后，做两层验证：

#### 第一层：方向一致性校验

```pseudo
function crossTimeframeConfirm(allSignals, cfg):
    confirmed = []

    for sig in allSignals:
        score = sig.strengthScore          # 基础分（来自第一章的七维评分）

        # --- 更大周期是否同向？---
        largerIntervals = LARGER_THAN[sig.interval]
        for ltf in largerIntervals:
            largerSig = findSignalFor(sig.symbol, ltf, allSignals)
            if largerSig is not null:
                if largerSig.direction == sig.direction:
                    score += cfg.resonance.bonus[ltf]     # 共振加分
                else:
                    score += cfg.resonance.penalty[ltf]    # 矛盾扣分

        # --- 更小周期是否已率先突破？（领先确认）---
        smallerIntervals = SMALLER_THAN[sig.interval]
        for stf in smallerIntervals:
            smallerSig = findSignalFor(sig.symbol, stf, allSignals)
            if smallerSig and smallerSig.direction == sig.direction:
                score += cfg.resonance.leadBonus

        sig.finalScore = clamp(score, 0, 100)
        if sig.finalScore >= cfg.filter.minStrength:
            confirmed.append(sig)

    return sorted(confirmed, key=finalScore, desc=True)
```

#### 共振规则表

| 当前周期 | 确认周期 | 加分 | 条件说明 |
|---------|---------|------|---------|
| 15m | 1h 同向 | **+15** | 1h 也检测到同向形态或同向趋势 |
| 15m | 4h 同向 | **+10** | 4h 大级别也同向 |
| 1h | 4h 同向 | **+15** | 最常用的共振组合 |
| 1h | 1d 同向 | **+10** | 日线确认 |
| 4h | 1d 同向 | **+15** | 高确定性组合 |
| 任意 | 更小周期已先突破 | **+8** | 小周期领先于大周期突破（时间优先） |

#### 矛盾惩罚表

| 冲突情况 | 扣分 | 说明 |
|---------|------|------|
| 1h 与 4h 方向相反 | **-20** | 中期矛盾，大幅降权 |
| 4h 与 1d 方向相反 | **-25** | 大级别矛盾，几乎否定信号 |
| 三个周期中两两矛盾 | **-40** | 直接放弃推送 |

> **注意**：反转形态（头肩顶/底、双顶/底）天然与大趋势相反——这是正常的。"矛盾惩罚"主要针对持续形态（三角形/旗形/通道）的方向冲突。

---

### 2.2.5 信号过滤与去重 (Filter)

详见第三章。此处仅列接口契约：

```pseudo
function filterAndDedup(signals, stateStore, cfg):
    # ① 量能过滤：突破 K 线成交量 / 20 周期均量 ≥ minVolumeRatio
    signals = filter(s => s.volumeRatio >= cfg.filter.minVolumeRatio, signals)

    # ② 突破幅度过滤：突破距离 / ATR ≥ minBreakoutATR
    signals = filter(s => s.breakoutMagnitude >= cfg.filter.minBreakoutATR * s.atr, signals)

    # ③ 噪音过滤：ATR / 价格 ≥ minVolatility（剔除横盘/低流动性）
    signals = filter(s => s.atr / s.price >= cfg.filter.minVolatility, signals)

    # ④ R:R 校验：风险回报比 ≥ minRR
    signals = filter(s => s.riskRewardRatio >= cfg.filter.minRR, signals)

    # ⑤ 去重：同一 (symbol, patternType, interval) 在冷却期内不重复推
    final = []
    for s in signals:
        if not stateStore.isInCooldown(s.symbol, s.patternType, s.interval, cfg.filter.cooldown_minutes[s.interval]):
            final.append(s)
            stateStore.recordSignal(s)

    return final
```

---

### 2.2.6 推送与渲染模块 (Notifier)

详见第四章。接口摘要：

```pseudo
function notify(signals, cfg):
    for sig in signals[:cfg.notification.max_per_run]:   # 单次上限防刷屏
        # 1. 渲染标注图
        chartImg = renderChart(sig.klines, sig)         # matplotlib → PNG bytes
        b64 = base64encode(chartImg)
        md5 = hashlib(chartImg).hexdigest()

        # 2. 构造企微 markdown 消息
        msg = buildWeComMessage(sig)

        # 3. 发送（先文字后图片）
        postWebhook(cfg.notification.webhook_url, msg)   # markdown
        postWebhook(cfg.notification.webhook_url, {      # image
            msgtype: "image",
            image: { base64: b64, md5: md5 }
        })

        sleep(3)                                        # 企微限流 20条/分钟
```

---

## 2.3 配置驱动设计

**核心理念：代码中不出现任何魔法数字。** 所有可调参数集中在 `config.yaml`，不同环境（测试/生产）只需换配置文件。

### 配置项全景表

| 分类 | 参数名 | 类型 | 默认值 | 说明 |
|------|-------|------|-------|------|
| **数据源** | primary | string | "binance" | 主数据源 |
| | fallback | string | "okx" | 兜底源 |
| | timeout_sec | int | 30 | HTTP 超时 |
| | retry_max | int | 3 | 最大重试次数 |
| | backoff_base | float | 2.0 | 指数退避基数(秒) |
| **扫描目标** | top_n | int | 300 | 成交额排名前 N |
| | min_volume_usdt | int | 10_000_000 | 最低 24h 成交额 |
| | intervals | list | ["15m","1h","4h","1d"] | 扫描周期列表 |
| **限流** | concurrency | int | 3 | 并发数 |
| | req_interval_ms | int | 100 | 请求间隔 |
| | batch_size | int | 50 | 每批数量 |
| | batch_pause_sec | int | 2 | 批间暂停 |
| **ZigZag** | left_15m | int | 5 | 15m/1h 左窗口 |
| | right_15m | int | 5 | 右窗口 |
| | left_4h | int | 3 | 4h 左窗口 |
| | right_4h | int | 3 | |
| | left_1d | int | 2 | 日线左窗口 |
| | right_1d | int | 2 | |
| **形态阈值** | peak_tolerance | float | 0.03 | 双顶/头肩 两峰价差容差 |
| | neck_slope_max | float | 0.03 | 颈线最大斜率 |
| | touch_tolerance | float | 0.02 | 趋势线触点穿透容差 |
| | breakout_candles | int | 2 | 连续收盘确认根数 |
| | breakout_atr_ratio | float | 0.5 | 突破最小幅度(ATR倍) |
| | volume_ratio_min | float | 1.5 | 突破量/均量最小比值 |
| **过滤** | min_strength | int | 60 | 最低推送分数 |
| | min_rr | float | 1.5 | 最小风险回报比 |
| | cooldown_15m | int | 30 | 15m冷却期(分钟) |
| | cooldown_1h | int | 120 | |
| | cooldown_4h | int | 480 | |
| | cooldown_1d | int | 1440 | |
| **推送** | webhook_secret | string | "${WECOM_WEBHOOK}" | 从 Secrets 读 |
| | max_per_run | int | 20 | 单次最大推送数 |
| | chart_width | int | 900 | 图宽(px) |
| | chart_candles | int | 120 | 图显示K线数 |

完整 YAML 示例见项目根目录 `config.yaml`。

---

## 2.4 GitHub Actions 工作流骨架

```yaml
name: Crypto Pattern Scanner
on:
  schedule:
    - cron: '*/15 * * * *'          # 每 15 分钟（实际受 Actions 调度影响）
  workflow_dispatch:                 # 支持手动触发调试

concurrency:
  group: scanner
  cancel-in-progress: false          # 不取消正在运行的 job

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 30              # 总超时保护
    env:
      PYTHONUNBUFFERED: "1"

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install dependencies
        run: |
          pip install requests pandas matplotlib numpy pyyaml Pillow

      - name: Restore dedup state cache
        id: restore-cache
        uses: actions/cache@v4
        with:
          path: state/
          key: pattern-state-${{ github.run_id }}
          restore-keys: |
            pattern-state-

      - name: Run scanner
        env:
          WECOM_WEBHOOK: ${{ secrets.WECOM_WEBHOOK }}
        run: python main.py --config config.yaml

      - name: Persist state to repo
        run: |
          git config user.name "scanner-bot"
          git config user.email "bot@scanner.local"
          git add state/ || true
          git diff --cached --quiet || git commit -m "scan state $(date -u +%Y%m%dT%H%M%SZ)" || true
          git push || true

      - name: Upload scan artifacts
        uses: actions/upload-artifact@v4
        with:
          name: scan-results-${{ github.run_id }}
          path: output/
          retention-days: 7
```

### 关键设计决策说明

| 决策 | 为什么这样做 |
|------|------------|
| `cancel-in-progress: false` | 正在跑的扫描不应被新 cron 取消，否则会丢信号 |
| cache key 用 `run_id` | 每次 run 有独立缓存；`restore-keys: pattern-state-` 保证未命中时拿到最近一次的状态 |
| state 目录 git commit 回写 | cache 可能被清理（Actions 保留 7 天），git 是永久兜底 |
| `timeout-minutes: 30` | 300 标的 × 4 周期 + 推送，正常 2~3 分钟完成；30 分钟留足异常重试空间 |
| artifact 保留 7 天 | 事后审计信号质量、排查误报 |
| Secrets 存 webhook URL | 不把 URL 写进代码或 yaml 明文 |

---

## 2.5 异常处理与降级策略

| 异常场景 | 处理方式 | 是否继续 |
|---------|---------|---------|
| Binance 429 (限流) | 读 Retry-After header，指数退避重试 | 是，自动降速 |
| Binance 418 (IP 封禁) | 切换到 OKX 兜底源 | 是 |
| 单个标的请求超时 | 跳过该标的，记录日志，继续下一个 | 是 |
| OKX 也不可用 | 该 (symbol, interval) 对标记为 SKIP | 是 |
| 整体超时 (接近 30min) | 停止拉新数据，用已有数据完成识别和推送 | 部分 |
| 企微 Webhook 返回非 0 | 重试 1 次，失败则记日志不阻塞 | 是 |
| 图片生成失败 (matplotlib 报错) | 只发送 markdown 文字消息，附注"图表生成失败" | 是 |
| config.yaml 格式错误 | 启动即退出，打印明确报错信息 | 否 |
