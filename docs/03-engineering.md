# 第三章 工程细节

## 3.1 关键数据结构定义

### 3.1.1 原始 K 线

```pseudo
struct Kline:
    openTime: int64            # 毫秒时间戳
    open: float
    high: float
    low: float
    close: float
    volume: float              # 成交量(币)
    closeTime: int64
    quoteVolume: float         # 成交额(USDT)
    tradeCount: int
    # --- 指标计算后追加 ---
    atr: float|null            # ATR(14) 该根的值
```

### 3.1.2 摆动点

```pseudo
enum PivotType { HIGH, LOW }

struct Pivot:
    index: int                 # 在 klines 数组中的位置
    price: float               # high 或 low 的值
    type: PivotType
    timestamp: int64           # 对应 klines[index].openTime
```

### 3.1.3 趋势线

```pseudo
struct Line:
    p1: Pivot                  # 起点摆动点
    p2: Pivot                  # 终点摆动点
    slope: float               # 斜率 = (p2.price-p1.price)/(p2.index-p1.index)
    intercept: float           # 价格轴截距
    touchCount: int            # 触点数（拟合时统计）
    direction: Enum{UP, DOWN, FLAT}
```

### 3.1.4 形态候选

```pseudo
# 完整定义见第二章 2.2.3，此处补充状态机字段
struct Pattern extends BasePattern:
    status: Enum{
        CANDIDATE,             # 结构匹配完成，等待突破确认
        BREAKING,              # 已突破但未满足确认条件
        CONFIRMED,             # 突破已确认 → 可推送
        FAILED,                # 失效（价格回到形态内部）
        EXPIRED                # 超过最大持有期未突破 → 放弃
    }
    firstDetectedAt: datetime   # 首次检测到的时间
    lastUpdatedAt: datetime    # 最后更新时间
    cooldownUntil: datetime    # 冷却期截止时间
```

### 3.1.5 最终信号

```pseudo
struct Signal extends Pattern:
    finalScore: int            # 0~100
    resonantWith: List[string] # 共振周期列表，如 ["1h","4d"]
    pushAt: datetime           # 推送时间
    messageId: string|null     # 企微消息 ID（用于去重）
```

### 3.1.6 去重状态文件 schema

```json
{
  "version": 1,
  "lastScanAt": "2026-08-30T12:00:00Z",
  "pushedSignals": [
    {
      "symbol": "SOLUSDT",
      "patternType": "double_bottom",
      "interval": "4h",
      "direction": "LONG",
      "detectedAt": "2026-08-30T08:00:00Z",
      "pushedAt": "2026-08-30T08:05:00Z",
      "cooldownUntil": "2026-08-30T20:00:00Z",
      "signalHash": "a1b2c3d4"
    }
  ],
  "scanStats": {
    "totalSymbolsScanned": 300,
    "totalIntervalsScanned": 1200,
    "candidatesFound": 47,
    "afterFilter": 8,
    "pushed": 3,
    "durationSeconds": 142,
    "source": "binance",
    "fallbackCount": 0
  }
}
```

`signalHash` 的生成规则：
```pseudo
function signalHash(symbol, patternType, interval, direction):
    return sha256(f"{symbol}|{patternType}|{interval}|{direction}")
```
同一 hash + `cooldownUntil > now()` 的信号不重复推送。

---

## 3.2 API 限流与异常处理

### 3.2.1 Binance 限流模型

Binance Futures 使用 **IP 级 weight 限制**：

| 维度 | 限制 |
|------|------|
| Weight 预算 | 2400 / 分钟 / IP |
| 单次 klines | weight = **2** |
| 单次 ticker/24hr (全部) | weight ≈ **40** |
| 单次 exchangeInfo | weight = **1** |
| 超限响应 | HTTP 429 + `Retry-After` header |
| 严重超限 | HTTP 418 (IP 封禁 2min 起，递增) |

**实现要点：**

```pseudo
class BinanceClient:
    def __init__(self):
        self.base_url = "https://fapi.binance.com/fapi/v1"
        self.session = requests.Session()
        self.used_weight = 0          # 当前分钟累计 weight
        self.weight_window_start = now()

    def request(self, method, path, params={}):
        while True:
            resp = self.session.request(method, f"{self.base_url}{path}", params=params,
                                        timeout=30)

            # 从响应头读取实时 weight 用量
            self.used_weight = int(resp.headers.get("X-MBX-USED-WEIGHT-1M", 0))

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
                log(f"Rate limited. Used weight={self.used_weight}. Waiting {retry_after}s")
                sleep(retry_after)
                continue

            if resp.status_code == 418:
                log("CRITICAL: IP banned! Backing off 60s")
                sleep(60)
                continue

            if resp.status_code >= 500:
                log(f"Server error {resp.status_code}, retrying...")
                sleep(exponential_backoff(self.attempt))
                self.attempt += 1
                if self.attempt > 3: raise
                continue

            return resp.json()

    def pre_request_check(self, estimated_weight=2):
        """请求前检查是否接近限额"""
        elapsed = now() - self.weight_window_start
        if elapsed < 60 and self.used_weight + estimated_weight > 2300:   # 留 100 余量
            wait_time = 60 - elapsed + 1
            log(f"Approaching limit ({self.used_weight}/{2400}), waiting {wait_time}s")
            sleep(wait_time)
            self.used_weight = 0
            self.weight_window_start = now()
```

### 3.2.2 OKX 限流模型

OKX 使用**请求数限制**（非 weight）：

| 维度 | 限制 |
|------|------|
| 公开行情接口 | **20 请求 / 2 秒 / IP** |
| 全局桶 | 250 req/s |
| 超限响应 | HTTP 429 |

实现更简单——固定速率即可：

```pseudo
class OkxClient:
    def __init__(self):
        self.base_url = "https://www.okx.com/api/v5/market"
        self.last_request_time = 0
        self.request_interval = 0.11     # ~9 req/s，远低于 10 req/s 安全线

    def request(self, path, params={}):
        # 令牌桶：确保不超过 9 req/s
        elapsed = now() - self.last_request_time
        if elapsed < self.request_interval:
            sleep(self.request_interval - elapsed)

        resp = requests.get(f"{self.base_url}{path}", params=params, timeout=30)
        self.last_request_time = now()

        if resp.status_code == 429:
            sleep(5)                      # OKX 通常 2s 后恢复，留余量
            return self.request(path, params)

        data = resp.json()
        if data.get("code") != "0":
            log(f"OKX error: {data.get('msg')}")
            return null
        return data["data"]               # OKX 数据在 data 字段内
```

### 3.2.3 统一重试策略

```pseudo
function withRetry(fn, maxRetries=3, baseDelay=2.0):
    for attempt in range(maxRetries + 1):
        try:
            return fn()
        except RateLimitError as e:
            delay = e.retryAfter or (baseDelay * (2 ** attempt))
            log(f"Retry {attempt+1}/{maxRetries} after {delay:.1f}s (rate limited)")
            sleep(delay)
        except TimeoutError:
            log(f"Retry {attempt+1}/{maxRetries} after timeout")
            sleep(baseDelay * (2 ** attempt))
        except ConnectionError:
            log(f"Retry {attempt+1}/{maxRetries} after connection error")
            sleep(baseDelay * (2 ** attempt))
    raise MaxRetriesExceeded(fn)
```

**退避参数表：**

| 异常类型 | 首次延迟 | 最大延迟 | 最大次数 | 是否切换源 |
|---------|---------|---------|---------|-----------|
| 429 Rate Limit | Retry-After 或 2s | 60s | 3 | 否（等恢复） |
| 418 IP Ban | 60s | 300s | 2 | 是（切 OKX） |
| Timeout | 2s | 10s | 3 | 否 |
| Connection Error | 2s | 15s | 3 | 第 3 次切 OKX |
| OKX 429 | 5s | 30s | 2 | —（仅 OKX） |

---

## 3.3 误报过滤

误报是形态扫描系统最大的敌人。以下四层过滤按顺序执行，每一层都会削减候选数量。

### 3.3.1 第一层：成交量确认

**原理**：真正的突破通常伴随量能放大；缩量突破往往是假突破。

```pseudo
function volumeConfirmation(klines, breakoutIndex, minRatio=1.5):
    breakoutVol = klines[breakoutIndex].volume
    lookback = 20
    start = max(0, breakoutIndex - lookback)
    avgVol = mean(k[start:breakoutIndex].volume)
    ratio = breakoutVol / avgVol

    # 同时检查前一根是否也放量（避免单根插针）
    if breakoutIndex >= 1:
        prevRatio = klines[breakoutIndex-1].volume / avgVol
        ratio = max(ratio, prevRatio * 0.7)    # 前根打折计入

    return ratio >= minRatio, ratio
```

**阈值建议：**

| 场景 | minRatio | 说明 |
|------|----------|------|
| 标准确认 | **1.5** | 突破量 ≥ 1.5× 近 20 根均量 |
| 强确认 | **2.0** | 高确定性场景 |
| 弱确认（仅记录） | **1.0** | 不拒绝但降分 |

> **注意**：加密货币市场存在"无量突破后补量"的情况（尤其是低流动性币种）。因此量能不能作为唯一否决项，而是作为评分维度之一。

### 3.3.2 第二层：突破幅度与连续 K 线确认

**原理**：真突破不是影线刺穿，而是收盘价站稳。

```pseudo
function breakoutConfirmation(klines, breakoutIndex, boundaryPrice, atr, cfg):
    confirmedCandles = 0
    for i in range(breakoutIndex, min(breakoutIndex + cfg.maxCheckCandles, len(klines))):
        if direction == LONG and klines[i].close > boundaryPrice:
            confirmedCandles += 1
        elif direction == SHORT and klines[i].close < boundaryPrice:
            confirmedCandles += 1
        else:
            break       # 中断，未持续确认

    # 计算突破幅度（以 ATR 为单位归一化）
    magnitude = abs(klines[breakoutIndex].close - boundaryPrice) / atr

    return {
        "confirmed": confirmedCandles >= cfg.breakoutCandles,
        "confirmedCandles": confirmedCandles,
        "magnitude": magnitude,
        "magnitudeOk": magnitude >= cfg.breakoutAtrRatio
    }
```

**默认参数：**

| 参数 | 默认值 | 理由 |
|------|-------|------|
| breakoutCandles | **2** | 连续 2 根收盘在边界外（平衡灵敏度与可靠性） |
| maxCheckCandles | **5** | 最多往前看 5 根，超过则认为"尚未确认"而非"失败" |
| breakoutAtrRatio | **0.5** | 突破幅度至少半根 ATR（过滤毛刺） |

### 3.3.3 第三层：噪音过滤（低波动率与流动性）

**过滤规则：**

```pseudo
function noiseFilter(klines, symbol, atr, price, cfg):
    issues = []

    # ① 波动率过低 → 横盘，任何"形态"都是噪声
    volatility = atr / price
    if volatility < cfg.minVolatility:
        issues.append("low_volatility")

    # ② 单根振幅过大 → 可能是插针/闪崩
    recent = klines[-20:]
    for k in recent:
        candleRange = (k.high - k.low) / k.close
        if candleRange > cfg.maxCandleRange:
            issues.append("wick_spike")
            break

    # ③ 成交量骤减 → 流动性枯竭，形态不可信
    recentVol = [k.volume for k in recent]
    volRatio = recentVol[-1] / mean(recentVol[:-1])
    if volRatio < cfg.minRecentVolumeRatio:
        issues.append("volume_dryup")

    return len(issues) == 0, issues
```

**默认阈值：**

| 参数 | 默认值 | 含义 |
|------|-------|------|
| minVolatility | **0.005** (0.5%) | ATR/Price 低于此视为横盘 |
| maxCandleRange | **0.08** (8%) | 单根 K 线振幅超过此视为插针 |
| minRecentVolumeRatio | **0.3** | 最新一根量不足前 19 根均量的 30% |

### 3.3.4 第四层：形态合理性校验

有些"形态"虽然几何上匹配，但在交易逻辑上不合理：

| 不合理情况 | 检测方法 | 处理 |
|-----------|---------|------|
| 形态太窄（高度 < 1×ATR） | `patternHeight < atr` | 降级为 CANDIDATE 不推送 |
| 形态太宽（跨度 > 最大值） | `spanBars > maxSpan[interval]` | 同上 |
| 突破点已在形态末端很远 | `breakoutIndex - patternEnd > confirmationWindow` | 标记为 EXPIRED |
| 颈线过于倾斜（头肩中） | `abs(neckline.slope) > neckSlopeMax` | 降级 |
| 旗形无旗杆 | `flagpoleBars < 3 or flagpoleMove < 3%` | 不是旗形，丢弃 |

---

## 3.4 日志与告警去重

### 3.4.1 状态持久化方案：Cache 为主 + 文件兜底

```
┌─────────────────────────────────┐
│  Actions Cache (actions/cache)   │  ← 主存储：跨 run 持久化
│  key: pattern-state-{run_id}     │     保留 7 天（免费额度）
│  restore-keys: pattern-state-     │     读写快、零 commit 噪音
└──────────┬──────────────────────┘
           │ cache miss 时回退
           ▼
┌─────────────────────────────────┐
│  state/state.json (git repo)     │  ← 兜底：永久存储
│  每次 run 结束 git add+commit     │     历史可追溯
│  push 回仓库                       │     但产生自动提交
└─────────────────────────────────┘
```

**为什么两层？**
- Cache 快且干净，但可能被清理（Actions 保留政策变化）
- Git 永久但有 commit 噪音（每次 run 一个 commit）
- 两者内容完全一致（同一个 JSON），互为备份

### 3.4.2 去重逻辑详细伪代码

```pseudo
class StateStore:
    def __init__(self, statePath="state/state.json"):
        self.statePath = statePath
        self.state = self.load()         # 从 cache 或文件加载

    def load(self):
        # 优先从缓存路径读
        if exists(self.statePath):
            return json.load(open(self.statePath))
        return emptyState()

    def isInCooldown(self, symbol, patternType, interval, cooldownMinutes):
        h = signalHash(symbol, patternType, interval, "?")  # 方向无关的去重
        now = utcnow()
        for pushed in self.state.pushedSignals:
            if pushed.signalHash.startswith(h.split("|")[:-1]):  # 前3字段匹配
                if pushed.cooldownUntil > now:
                    remaining = (pushed.cooldownUntil - now).total_seconds() / 60
                    log(f"In cooldown: {symbol} {patternType} {interval}, "
                        f"{remaining:.0f}min remaining")
                    return True
        return False

    def recordSignal(self, signal):
        cooldownMinutes = COOLDOWN_DEFAULTS[signal.interval]
        entry = {
            "symbol": signal.symbol,
            "patternType": signal.patternType,
            "interval": signal.interval,
            "direction": signal.direction.name,
            "detectedAt": signal.detectedAt.isoformat(),
            "pushedAt": utcnow().isoformat(),
            "cooldownUntil": (utcnow() + timedelta(minutes=cooldownMinutes)).isoformat(),
            "signalHash": signalHash(signal.symbol, signal.patternType,
                                   signal.interval, signal.direction.name)
        }
        self.state.pushedSignals.append(entry)

        # 清理过期条目（防止 JSON 无限膨胀）
        cutoff = utcnow() - timedelta(days=7)
        self.state.pushedSignals = [
            s for s in self.state.pushedSignals
            if parse(s["pushedAt"]) > cutoff
        ]

    def save(self):
        # 写入文件（供 git commit）
        ensure_dir(self.statePath)
        json.dump(self.state, open(self.statePath, "w"),
                  indent=2, ensure_ascii=False)

    def getScanStats(self):
        return self.state.scanStats
```

### 3.4.3 冷却期配置表

| 周期 | 冷却期 | 设计理由 |
|------|-------|---------|
| 15m | **30 分钟** | 15m 形态小，同一信号半小时内重复无意义 |
| 1h | **2 小时** | 1h 形态中等，2 小时给足观察窗口 |
| 4h | **8 小时** | 4h 形态较大，8 小时 ≈ 2 个 4h K 线 |
| 1d | **24 小时** | 日线级别，一天最多推一次同信号 |

> **冷却期只对 `(symbol, patternType, interval)` 三元组生效。** 同一标的在不同周期检测到不同形态（如 15m 双底 + 4h 头肩底）可以分别推送。

### 3.4.4 推送频率保护

企微机器人硬限制：**每个 webhook ≤ 20 条/分钟**。

系统自身保护层：
```pseudo
class PushLimiter:
    def __init__(self, maxPerMinute=18):    # 留 2 条余量
        self.timestamps = []
        self.maxPerMinute = maxPerMinute

    def acquire(self):
        now_val = now()
        # 清理 1 分钟前的记录
        self.timestamps = [t for t in self.timestamps if now_val - t < 60]
        if len(self.timestamps) >= self.maxPerMinute:
            wait = 60 - (now_val - self.timestamps[0]).total_seconds()
            log(f"Push rate limit reached, waiting {wait:.0f}s")
            sleep(wait)
        self.timestamps.append(now_val)
```

配合 `config.yaml` 中的 `max_per_run: 20`（单次 run 总上限），双重保护不会刷屏。

---

## 3.5 日志规范

### 日志分级与格式

```
[TIME] [LEVEL] [MODULE] message {context}

示例：
[2026-08-30 12:03:15] [INFO ] [MarketData] Fetched 300 symbols from Binance ticker/24hr
[2026-08-30 12:03:17] [WARN ] [MarketData] SOLUSDT 15m klines timeout, retrying (1/3)
[2026-08-30 12:04:02] [ERROR] [PatternEngine] MATIC 4h ZigZag produced 0 pivots (skipped)
[2026-08-30 12:05:10] [NOTIFY] [Notifier] Pushed HEAD_SHOULDERS_TOP BTCUSDT 4h score=82
```

### 必须记录的关键事件

| 事件 | 级别 | 必记字段 |
|------|------|---------|
| 开始扫描 | INFO | run_id, source |
| 完成 Top300 选取 | INFO | count, min_volume_cutoff |
| 每个标的拉取完成 | DEBUG | symbol, interval, bars_count, source |
| 限流触发 | WARN | used_weight, wait_seconds |
| 切换到兜底源 | WARN | symbol, reason |
| 形态候选发现 | DEBUG | symbol, interval, pattern, confidence |
| 过滤掉某信号 | DEBUG | symbol, pattern, filter_reason, value, threshold |
| 信号确认推送 | INFO | symbol, pattern, interval, score, direction |
| 推送成功 | INFO | msg_type (markdown/image), response |
| 推送失败 | ERROR | http_status, body |
| 运行结束 | INFO | duration_sec, stats dict |

### 输出产物

每次 run 结束后在 `output/` 目录产出：

```
output/
├── signals_YYYYMMDD_HHMMSS.json    # 本次所有最终信号（含未推送的弱信号）
├── scan_log_YYYYMMDD_HHMMSS.txt    # 结构化日志
├── charts/                          # 渲染的标注图 PNG
│   ├── BTCUSDT_4h_head_shoulders_top.png
│   └── SOLUSDT_1h_double_bottom.png
└── stats_summary.json               # 本次运行统计摘要
```

`stats_summary.json` 示例：
```json
{
  "runId": "abc123",
  "startedAt": "2026-08-30T12:00:00Z",
  "finishedAt": "2026-08-30T12:02:45Z",
  "durationSeconds": 165,
  "source": "binance",
  "fallbackUsed": false,
  "symbolsScanned": 300,
  "intervalsScanned": 1197,          // 可能有少量跳过
  "candidatesFound": 52,
  "afterVolumeFilter": 38,
  "afterBreakoutFilter": 31,
  "afterNoiseFilter": 27,
  "afterDedup": 12,
  "afterRRFilter": 8,
  "pushed": 3,
  "rateLimitHits": 1,
  "errors": 0
}
```

这个摘要写入 `state/scanStats` 并随 state 文件一起持久化，可用于监控长期运行健康度。
