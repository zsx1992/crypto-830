# 第一章 形态清单与量化判定阈值

> **适用范围**：加密货币与股票市场的 K 线形态自动识别系统。本文档定义 17 种经典形态的几何判定条件、参数默认值、可检测性分级与信号评分模型。
> **阅读约定**：所有伪代码使用 ```pseudo 围栏，为与语言无关的结构化描述；所有价格类参数若无特别说明，均为**相对比例**（如 0.03 = 3%），避免不同品种价格量级差异导致阈值失效。
> **实证依据**：文末附录的频次分布来自 360 张人工标注图（其中 173 张含有效形态标注）的真实统计，分级（A/B/C）与开发优先级直接由该分布决定。

---

## 1.1 前置算法：摆动点检测 (ZigZag)

### 1.1.1 为什么所有形态识别都以 ZigZag 为前置步骤

形态识别的本质是**在序列中寻找特定几何结构**。若直接在原始 K 线上做匹配，会面临三个无法回避的问题：

1. **数据量爆炸**：800 根 K 线做五点结构匹配，组合数为 C(800,5) ≈ 10^11 量级，实时扫描不可行。
2. **噪声主导**：每根 K 线的高低价都含有市场微结构噪声，直接匹配会产生海量假信号。
3. **不可复现**：原始 K 线上的"高低点"依赖主观判断，同一张图不同分析者会画出不同形态，无法形成可回测的规则。

ZigZag 把原始序列压缩为**摆动点序列 (pivot sequence)**，把问题转化为：在一个长度 30~60 的有序高/低交替序列上做结构匹配。这一转换带来三个直接收益：

- **降维**：候选组合数从 10^11 降到 10^4 量级，可实时扫描多品种多周期。
- **去噪**：只有被左右 `left`/`right` 根 K 线共同确认的极值才被保留，过滤掉单根插针与小幅抖动。
- **结构化**：摆动点天然带有 `HIGH`/`LOW` 类型与时间戳，形态的几何约束（如"头高于两肩"）可直接表达为对序列中相邻元素的不等式判断。

> **重要认知**：ZigZag 不是一个"可选优化"，而是形态识别的**数据模型定义**。后续所有形态的伪代码，入参都是 `pivots`（摆动点数组），而不是 `klines`。ATR、成交量等连续量仅在**确认阶段**（突破、量能、止损）使用。

### 1.1.2 摆动点定义

对第 `i` 根 K 线，取窗口 `w = klines[i-left : i+right+1]`：

- 若 `klines[i].high == max(w.high)` → 标记为**摆动高点 (Pivot HIGH)**
- 若 `klines[i].low == min(w.low)` → 标记为**摆动低点 (Pivot LOW)**

一个自然推论：**同一个 `i` 不可能同时是摆动高点与摆动低点**（除非窗口内所有 K 线完全相同），但在剧烈波动段可能出现"高点在窗口左侧、低点在窗口右侧"这类时间错位，因此需要**强制高低交替**处理。

### 1.1.3 强制高低交替 (enforceAlternation)

原始扫描可能产生连续两个 HIGH（中间的低点未被识别）或连续两个 LOW。这会导致形态匹配时"两个高点相邻"，几何约束失效。`enforceAlternation()` 按时间顺序扫描，遇到同类型连续点时保留**更极端**的一个（高点取更高者，低点取更低者），删除另一个。

特殊边界情况需要显式处理：

| 情况 | 处理方式 |
|------|---------|
| 连续 2 个 HIGH，后一个更高 | 删除前一个（旧点被新极值替代） |
| 连续 2 个 HIGH，后一个更低 | 删除后一个（不构成新高，是噪声） |
| 同价位（相等） | 保留先出现的，删除后者（避免自比较） |
| 序列首尾 | 不强制删除，但形态匹配时首尾点标记为 `unconfirmed` |

### 1.1.4 主伪代码

```pseudo
function findPivots(klines, left=5, right=5):
    pivots = []
    for i in range(left, len(klines) - right):
        w = klines[i-left : i+right+1]
        if klines[i].high == max(w.high): pivots.add(Pivot(i, klines[i].high, HIGH))
        if klines[i].low  == min(w.low):  pivots.add(Pivot(i, klines[i].low,  LOW))
    return enforceAlternation(pivots)
```

```pseudo
function enforceAlternation(pivots):
    result = []
    for p in pivots.sortBy(index):
        if result.isEmpty():
            result.add(p); continue
        last = result[-1]
        if last.type != p.type:
            result.add(p); continue
        # 同类型连续：保留更极端的一个
        if p.type == HIGH:
            if p.price >= last.price: result[-1] = p      # 新高替代旧点
            # else: 丢弃 p（未创新高，是回调噪声）
        else:
            if p.price <= last.price: result[-1] = p      # 新低替代旧点
            # else: 丢弃 p
    return result
```

### 1.1.5 left/right 参数的灵敏度权衡

`left` 与 `right` 的作用并不对称，理解这一点对调参至关重要：

- **`left` 是回溯确认**：判断 `i` 是否为极值时需要看左侧已确定的 `left` 根 K 线，不引入延迟。
- **`right` 是前瞻确认**：必须等待右侧 `right` 根 K 线走完才能确认 `i` 是极值，**因此 ZigZag 天然滞后 `right` 根 K 线**。这是形态识别系统延迟的主要来源，无法消除，只能权衡。

| 参数配置 | 灵敏度 | 噪声 | 滞后 | 摆动点数量（800根） | 适用场景 |
|---------|-------|------|------|------------------|---------|
| `left=2, right=2` | 极高 | 极多 | 2 根 | 100~180 | 仅用于超短线剥头皮，形态误报严重 |
| `left=3, right=3` | 高 | 较多 | 3 根 | 60~100 | 4h 级别，趋势明显、噪声相对低的品种 |
| `left=5, right=5` | 中 | 少 | 5 根 | **30~60** | **15m / 1h 默认配置** |
| `left=8, right=8` | 低 | 极少 | 8 根 | 18~35 | 1d 及以上，或高波动山寨币 |
| `left=5, right=2` | 中高 | 中 | 2 根 | 40~75 | 牺牲稳定性换取低延迟，需配合突破二次确认 |
| `left=2, right=8` | 低 | 极少 | 8 根 | 25~45 | 非对称配置：快速捕捉起点，缓慢确认终点 |

**关键权衡规律**：

- `right` 越小 → 越灵敏、滞后越低，但**形态会被反复重绘**（同一位置先识别为双顶，几根 K 线后变成三重顶），推送信号抖动严重。
- `right` 越大 → 形态越稳定，但**入场点已经错过一大段行情**，尤其在 15m 周期下滞后 8 根 = 2 小时。
- `left` 过小 → 摆动点数量激增，形态完整度评分失真；`left` 过大 → 短形态（旗形、三角旗形）会被整体吞掉，导致漏检。

> **工程建议**：对同一品种同时运行两套 ZigZag（快参数 `3/3` 用于信号初筛，慢参数 `5/5` 或 `8/8` 用于确认），只有当两套参数在**同一时间窗口内指向同一形态**时才推送。这是降低误报最有效且成本最低的手段。

### 1.1.6 默认参数建议表

| 周期 | left | right | 预期摆动点数/800根 | 滞后 | 备注 |
|------|------|-------|-----------------|------|------|
| 5m | 6 | 6 | 25~50 | 30 分钟 | 噪声极重，建议配合成交量过滤 |
| 15m | **5** | **5** | 30~60 | 75 分钟 | **主周期，推荐默认** |
| 1h | **5** | **5** | 30~60 | 5 小时 | **主周期，推荐默认** |
| 4h | 3 | 3 | 60~100 | 12 小时 | K 线数量少，需更小窗口保留结构 |
| 1d | 2 | 2 | 100~180 | 2 天 | 日线噪声已被大幅平滑，可用小窗口 |

> **为什么周期越大、`left/right` 越小**：高频周期单根 K 线信噪比低，需要更多邻域确认；日线级别一根 K 线已聚合了全天多空博弈，极值本身的可靠性高，无需大窗口。若在高周期沿用 `5/5`，会因样本量不足导致 800 根日线只能提取十几个摆动点，大量形态被漏检。

### 1.1.7 摆动点数据结构与压缩率

```pseudo
struct Pivot:
    index       : int       # 在 klines 中的下标
    timestamp   : datetime
    price       : float     # 摆动高点取 high，摆动低点取 low
    type        : HIGH | LOW
    confirmed   : bool      # 右侧 right 根 K 线是否已走完
    barRange    : float     # 该K线的 high-low，用于噪音过滤
```

**压缩率校验**：800 根 K 线应压缩到 **30~60 个摆动点**（压缩比约 6%~8%）。这是一个有价值的**健康检查指标**：

| 实测摆动点数 | 诊断 | 处理 |
|------------|------|------|
| < 15 | 窗口过大，结构被过度平滑 | 减小 `left/right` |
| 15~30 | 偏少，短形态（旗形/三角旗）会漏检 | 适当减小 `left`，或为短形态单独跑一套快参数 |
| **30~60** | **健康区间** | 保持 |
| 60~100 | 偏多，噪声开始进入 | 增大 `right`，或加强制最小摆动幅度过滤 |
| > 100 | 参数失效或品种异常波动 | 必须增大 `left/right`，否则形态识别不可用 |

可选的额外过滤器（在极端行情下启用）：

```pseudo
# 最小摆动幅度过滤：摆动幅度 < 0.8×ATR 的相邻摆动点合并
function filterMinorPivots(pivots, atr, minSwingATR=0.8):
    repeat:
        for each adjacent pair (a, b) in pivots:
            if abs(a.price - b.price) < minSwingATR * atr:
                # 合并：若中间存在更极端点则保留，否则删除幅度贡献小的那个
                removeLessSignificant(a, b)
    until noChange
    return pivots
```

---

## 1.2 共用指标计算

本节定义的三个工具（ATR、趋势线拟合、颈线提取）与一个公式（Measured Move）被后续所有形态共用。

### 1.2.1 ATR(14) — Wilder 平滑

```pseudo
function calcATR(klines, period=14):
    tr = [max(hi-lo, abs(hi-prevClose), abs(lo-prevClose)) for each bar]
    atr = SMA(tr, period)   # Wilder: alpha = 1/period
    return atr[-1]           # 最新值
```

Wilder 平滑的递推形式（实际实现应使用此形式，避免重复计算 SMA）：

```pseudo
function calcATR_Wilder(klines, period=14):
    tr[0..period-1] = trueRange(klines[0..period-1])
    atr = mean(tr[0..period-1])                      # 首个值用简单均值播种
    for i in range(period, len(klines)):
        tr_i = max(klines[i].high - klines[i].low,
                   abs(klines[i].high - klines[i-1].close),
                   abs(klines[i].low  - klines[i-1].close))
        atr = atr + (1.0 / period) * (tr_i - atr)    # alpha = 1/period
    return atr
```

**ATR 的三个核心用途**：

| 用途 | 规则 | 数值 | 说明 |
|------|------|------|------|
| 突破幅度确认 | 突破距离 ≥ `0.5 × ATR` | 0.5 | 低于此值视为"贴边摩擦"，任何形态都可能是假突破 |
| 止损距离 | 颈线外侧 `1.5 ~ 2.0 × ATR` | 1.5（默认）/ 2.0（高波动） | 太小会被随机波动扫损，太大则风险回报比恶化 |
| 噪音过滤 | 单根 K 线振幅 < `0.3 × ATR` | 0.3 | 低波动 K 线不参与摆动点竞争，减少无意义摆动 |

**ATR 周期选择**：

| 周期 | ATR period | 理由 |
|------|-----------|------|
| 15m / 1h | 14 | 标准配置 |
| 4h | 14 | 标准配置 |
| 1d | 14 | 标准配置 |
| 5m | 20 | 缩短 ATR 对单根异常 K 线的敏感度 |

> **注意**：ATR 是**绝对价格量**，跨品种比较必须用 `ATR / 当前价`（归一化波动率）。突破幅度判定用 ATR 绝对值（同一品种内自洽），但日志与评分区间校准建议同时记录归一化值。

### 1.2.2 趋势线拟合

```pseudo
function fitTrendline(pivots, direction, minTouches=3, tolerance=0.02):
    # direction = UP(上升) / DOWN(下降)
    candidates = pivots.filter(type=HIGH if direction==DOWN else LOW)
    for each pair of candidates:
        line = Line(p1, p2)
        touches = countPivotsNearLine(line, tolerance)
        penetrations = countPivotsPenetrate(line, tolerance*1.5)
        if touches >= minTouches and penetrations <= 1:
            return ValidLine(line, touches, slope)
    return null
```

**关键概念定义**：

- **触点 (touch)**：摆动点到直线的**垂直距离**（按价格轴计算）≤ `tolerance × 当前价`。注意不是欧氏距离，因为 K 线图的横轴（时间）与纵轴（价格）量纲不同。
- **穿透 (penetration)**：摆动点越过趋势线超过 `tolerance × 1.5`。趋势线允许**轻微穿越**（市场不会走出完美直线），但穿越次数过多说明这条线不成立。
- **斜率 (slope)**：定义为 `Δprice / (Δbars × 当前价)`，即**每根 K 线的相对变化率**。这样定义使斜率可跨周期、跨品种比较，是后续三角形分类（水平/上升/下降）的统一判据。

**触点优先级**：当多条候选线都满足 `minTouches` 时，按以下优先级选取：

```pseudo
function rankTrendlines(validLines):
    sort validLines by:
        1. touches DESC              # 触点越多越可靠
        2. penetrations ASC          # 穿透越少越干净
        3. spanBars DESC             # 跨度越长越有代表性
        4. recencyOfLastTouch DESC   # 最近触点越新越贴近当前结构
    return validLines[0]
```

**趋势线参数表**：

| 参数 | 默认值 | 取值范围 | 影响 |
|------|-------|---------|------|
| `minTouches` | 3 | 2~5 | 2 太松（任意两点成线），4+ 太严（短形态无法识别） |
| `tolerance` | 0.02 (2%) | 0.01~0.03 | 加密货可放宽到 0.025，股票可收紧到 0.015 |
| `penetrationFactor` | 1.5 | 1.2~2.0 | 容忍度倍数，越大越宽松 |
| `maxPenetrations` | 1 | 0~2 | 允许的最大穿透摆动点数 |
| `minSpanBars` | 10 | 5~20 | 趋势线两端点最小跨度，防止相邻两点拟合出无意义陡线 |

### 1.2.3 颈线提取

颈线是反转形态的**确认触发器**，其位置的准确性直接决定入场点与止损点。

**水平颈线**（默认首选）：

```pseudo
function extractNecklineHorizontal(p1, p2, p3 = null):
    if p3 == null:
        # 两点：取算术均值
        return (p1.price + p2.price) / 2
    else:
        # 三点及以上：线性回归取截距（更稳健，抵抗单点异常）
        return linearRegressionPrice(p1, p2, p3)
```

**倾斜颈线**（仅在头肩形态中启用）：

```pseudo
function extractNecklineSloped(L1, L3, currentPrice):
    rawSlope = (L3.price - L1.price) / ((L3.index - L1.index) * currentPrice)
    if abs(rawSlope) > cfg.neckSlopeMax:     # 默认 0.0003/bar，总斜率限制 ±3%
        return null                           # 斜率过大，颈线不可靠，降级为水平颈线或丢弃形态
    line = Line(L1, L3)
    return line
```

**实践建议（重要）**：

| 原则 | 说明 |
|------|------|
| **优先水平颈线** | 水平颈线的突破信号最干净，回测表现最稳定。倾斜颈线会引入"价格沿颈线漂移"的模糊区间，导致突破判定摇摆。 |
| **倾斜颈线仅用于头肩底/顶** | 头肩形态的两谷（或两峰）连线在真实图表中常明显倾斜，强制水平会大幅降低识别率。 |
| **总斜率限制 ±3%** | 从头肩左谷到右谷的**累计**价格偏移不超过 3%（不是每根 K 线）。超过此值说明该结构更像趋势通道而非头肩，应交给通道模块处理。 |
| **颈线突破以收盘价为准** | 盘中插针突破不算。要求**收盘价**越过颈线，且突破幅度 ≥ `0.5 × ATR`。 |
| **颈线需二次确认** | 突破后至少 1 根 K 线仍收在颈线外侧，或连续 2 根中有 2 根收在外侧，避免单根假突破。 |

颈线突破确认的伪代码：

```pseudo
function confirmNecklineBreak(klines, necklineLevel, direction, atr, cfg):
    # direction = BULLISH(向上突破) / BEARISH(向下跌破)
    breakoutIndex = null
    for j in range(lastPivotIndex, len(klines)):
        k = klines[j]
        if direction == BULLISH:
            dist = k.close - necklineLevel
        else:
            dist = necklineLevel - k.close
        if dist >= cfg.minBreakATR * atr:            # 0.5 × ATR
            if dist >= cfg.strongBreakATR * atr:      # 1.0 × ATR → 强突破，无需等第二根
                return Breakout(j, k.close, STRONG)
            # 弱突破：等待后续 confirmBars 根中有 requiredConfirm 根收在外侧
            confirm = 0
            for m in range(j+1, min(j+1+cfg.confirmBars, len(klines))):
                if (direction==BULLISH and klines[m].close > necklineLevel) or
                   (direction==BEARISH and klines[m].close < necklineLevel):
                    confirm += 1
            if confirm >= cfg.requiredConfirm:
                return Breakout(j, k.close, NORMAL)
    return null
```

**颈线突破参数表**：

| 参数 | 默认值 | 说明 |
|------|-------|------|
| `minBreakATR` | 0.5 | 有效突破的最小幅度（× ATR） |
| `strongBreakATR` | 1.0 | 强突破阈值，达到则跳过二次确认 |
| `confirmBars` | 2 | 弱突破后的确认窗口（根） |
| `requiredConfirm` | 2 | 确认窗口内需满足的根数 |
| `neckSlopeMax` | 0.03 | 倾斜颈线总斜率上限（3%） |

### 1.2.4 形态高度 → 目标位 (Measured Move)

```
目标价 = 突破价 ± 形态高度 × 投射倍数
形态高度 = |峰价 - 颈线价|     （头肩/双顶）
         = |上边界 - 下边界|   （三角形/矩形）
投射倍数：1.0（保守） / 1.618（标准） / 2.0（激进）
```

不同形态的"形态高度"提取方式必须区分，这是回测中最容易出错的地方：

| 形态族 | 高度定义 | 基准点 |
|-------|---------|-------|
| 头肩顶/底 | 头部极值 − 颈线价（垂直距离） | 突破价 = 颈线突破点 |
| 双顶/底、三重顶/底 | 峰/谷极值 − 中间谷/峰价 | 突破价 = 颈线突破点 |
| 三角形（三种） | **最大**垂直宽度 = 左端起点处上边界 − 下边界 | 突破价 = 边界突破点 |
| 矩形 | 箱体高度 = 上边界 − 下边界 | 突破价 = 边界突破点 |
| 旗形/三角旗形 | **旗杆高度** = 旗杆起点到终点的垂直距离 | 突破价 = 旗身突破点 |
| 楔形 | 左端最大垂直宽度 | 突破价 = 边界突破点 |

> **三角形的高度陷阱**：三角形是收敛的，若在突破点处测量高度会得到接近 0 的值。**必须测量形态最左端（起点的两边界间距）**，这是经典定义，也是回测中目标位唯一有效的算法。

> **旗形的高度陷阱**：旗形目标位源自**旗杆**，不是旗身。因为旗身只是整理，旗杆的动量才是被"测量"的对象。这与所有其他形态都不同。

投射倍数的选择依据：

| 倍数 | 档位 | 适用场景 | 历史达成率（经验值） |
|------|------|---------|------------------|
| 1.0 | 保守 | 逆大趋势的反转型形态、低流动性品种 | 65%~75% |
| **1.618** | **标准** | 顺大趋势的持续型形态（三角形、旗形） | 45%~55% |
| 2.0 | 激进 | 强趋势 + 成交量显著放大 + 多周期共振 | 25%~35% |

---

## 1.3 十七种形态逐一详解

### 1.3.1 总览表（按样本集出现频次降序）

| # | 形态 | 样本频次 | 占比 | 方向 | 可检测性 | 最小K线数 | 优先级 |
|---|------|---------|------|------|---------|----------|-------|
| 1 | 头肩顶 | 15 (+50 未区分) | 26.0% | 看跌 | **A** | 40 | P0 |
| 2 | 头肩底 | 26 (+50 未区分) | 26.0% | 看涨 | **B** | 40 | P0 |
| 3 | 双底 / W底 | 37 | 14.9% | 看涨 | **A** | 25 | P0 |
| 4 | 下降三角形 | 24 | 9.7% | 看跌 | **A** | 30 | P0 |
| 5 | 对称三角形 | 24 | 9.7% | 双向 | **A** | 30 | P0 |
| 6 | 上升三角形 | 17 | 6.9% | 看涨 | **A** | 30 | P0 |
| 7 | 三角形（未指明） | 16 | 6.5% | 双向 | **A** | 30 | P0 |
| 8 | 旗形 | 7 | 2.8% | 顺旗杆 | **A** | 20 | P1 |
| 9 | 楔形（未指明） | 9 | 3.6% | 双向 | **B** | 30 | P1 |
| 10 | 下降楔形 | 3 | 1.2% | 看涨 | **B** | 30 | P1 |
| 11 | 上升楔形 | 2 | 0.8% | 看跌 | **B** | 30 | P1 |
| 12 | 三重顶 | — | — | 看跌 | **B** | 50 | P2 |
| 13 | 三重底 | — | — | 看涨 | **B** | 50 | P2 |
| 14 | 三角旗形 | — | — | 顺旗杆 | **B** | 15 | P2 |
| 15 | 矩形 / 箱体 | **0** | 0% | 双向 | **A** | 25 | P1* |
| 16 | 圆弧顶 | 2（归类） | 0.8% | 看跌 | **C** | 60 | P3 |
| 17 | 圆弧底 | 2 | 0.8% | 看涨 | **C** | 60 | P3 |
| 18 | V 型反转 | 1 | 0.4% | 双向 | **C** | — | P3 |

> **说明**：头肩形态若计入 50 次"未区分顶底"标注，其合并占比达 **26%**，是绝对的第一大形态。占比基于 173 张标注图中 248 次标注计算（单图可含多个形态），因此占比之和超过 100%。
> **矩形 0 次**是一个值得注意的现象：矩形形态在人工标注中几乎不被使用，但它是几何约束最严格、自动化最容易的形态，建议仍实现（P1*），因为它能捕获大量被人工标注为"未指明三角形"的边界情况。
> **通道（14 次）与杯柄（1 次）** 不在本文档 17 形态内，处理建议见附录 A.3。

### 1.3.2 可检测性分级定义

| 分级 | 定义 | 工程处理 |
|------|------|---------|
| **A** | 几何约束明确、参数鲁棒（±20% 参数扰动下识别结果稳定）、可全自动推送 | 实现自动推送，无需人工复核 |
| **B** | 几何约束明确但存在歧义（需前置趋势/量能等外部条件消歧），或样本量偏少 | 实现自动识别，但推送前附加确认条件；信号标注"待确认" |
| **C** | 无稳定几何结构，识别结果对参数极度敏感，或本质为事后统计 | **不实现自动化**，仅在人工复核界面提供辅助参考 |

---

### 形态 1：头肩顶 (Head & Shoulders Top)

| 项目 | 内容 |
|------|------|
| 识别要点 | 三个依次抬升后回落的峰，中间峰（头）最高，两侧峰（肩）近似等高，两谷连线为颈线 |
| 方向 | **看跌** |
| 失效条件 | 右肩高于头部；或价格突破头部高点；或跌破颈线后快速收回并站回颈线上方超过 2×ATR |
| 可检测性 | **A** |
| 最小K线数 | 40（整形态通常 40~150 根） |
| 关键阈值 | `peakTolerance=0.03`、`neckSlopeMax=0.03`、`minShoulderSeparation=8`、`minHeadProminence=0.02` |
| 样本频次 | 15 次（另有 50 次未区分顶底，合并为最高频形态） |

**结构要求**：`(H1, L1, H2, L2, H3)` 五个交替摆动点。

```
判定条件：
  - 中间头(H3) > 左肩(H1) 且 H3 > 右肩(H5)
  - 两肩高度差 ≤ 3% (peakTolerance)
  - L1 和 L3 的连线为颈线（允许 ±3% 斜率）
  - H1-H3 间距 ≥ 10 根 K 线，H3-H5 间距 ≥ 8 根
  - 右肩形成后价格跌破颈线
方向：看跌
失效：右肩高于头部（则不是头肩）
默认参数：peakTolerance=0.03, neckSlopeMax=0.03, minShoulderSeparation=8
```

> **命名对齐说明**：正文描述中的 `(H1,H2,H3)` 与用户需求稿的 `(H3,H5)` 为同一结构的两种编号。本文档统一采用**五点序列编号** `(H1, L1, H2, L2, H3)`，其中 `H2` 为头部、`L1` 与 `L2` 为两谷。原稿的"H1-H3 间距 ≥ 10"对应本文档的 `H1→H2 ≥ 10`，"H3-H5 间距 ≥ 8"对应 `H2→H3 ≥ 8`。

```pseudo
function detectHSTop(pivots, klines, atr, cfg):
    if len(pivots) < 5: return null
    results = []
    for i in range(0, len(pivots) - 4):
        seq = pivots[i : i+5]
        if not matchesTypes(seq, [HIGH, LOW, HIGH, LOW, HIGH]): continue
        H1, L1, H2, L2, H3 = seq

        # 1) 头部必须最高
        if not (H2.price > H1.price and H2.price > H3.price): continue

        # 2) 头部突出度：头至少高于较高一侧肩 2%，否则只是普通三峰震荡
        higherShoulder = max(H1.price, H3.price)
        if (H2.price - higherShoulder) / H2.price < cfg.minHeadProminence: continue

        # 3) 两肩近似等高
        if abs(H1.price - H3.price) / H2.price > cfg.peakTolerance: continue

        # 4) 肩部间距
        if H2.index - H1.index < cfg.minHeadSeparation:  continue    # 10
        if H3.index - H2.index < cfg.minShoulderSeparation: continue # 8

        # 5) 形态跨度
        span = H3.index - H1.index
        if span < cfg.minPatternBars or span > cfg.maxPatternBars: continue  # 40 / 150

        # 6) 两谷不能在颈线下方过深（L2 不应远低于 L1，否则是下降通道）
        if (L1.price - L2.price) / L1.price > cfg.maxNeckValleyDivergence: continue  # 0.05

        # 7) 颈线
        neckline = extractNecklineSloped(L1, L2, H2.price)
        if neckline == null: continue
        neckPrice = neckline.priceAt(H3.index)

        # 8) 突破确认
        brk = confirmNecklineBreak(klines, neckPrice, BEARISH, atr, cfg)
        if brk == null: continue     # 未突破 → 形态待定，不推送

        height = H2.price - neckPrice
        results.add(Pattern(
            type      = HS_TOP,
            direction = BEARISH,
            entry     = brk.price,
            stop      = neckPrice + cfg.stopATR * atr,          # 1.5
            target1   = brk.price - height * 1.0,
            target2   = brk.price - height * 1.618,
            height    = height,
            points    = seq,
            spanBars  = span
        ))
    return results
```

**头肩顶参数表**：

| 参数 | 默认值 | 含义 | 调参建议 |
|------|-------|------|---------|
| `peakTolerance` | 0.03 | 两肩相对高度差上限 | 加密货 0.04，股票 0.025 |
| `minHeadProminence` | 0.02 | 头高于较高肩的最小幅度 | 增至 0.03 可显著降噪 |
| `minHeadSeparation` | 10 | H1→H2 最小 K 线数 | 与周期无关，按形态完整性设定 |
| `minShoulderSeparation` | 8 | H2→H3 最小 K 线数 | 右肩可略紧凑（市场情绪加速） |
| `neckSlopeMax` | 0.03 | 颈线总斜率上限 | 超过则降级为水平颈线 |
| `maxNeckValleyDivergence` | 0.05 | L2 相对 L1 最大下沉比例 | 防止把下降通道误判为头肩顶 |
| `minPatternBars` | 40 | 形态最小跨度 | 15m 周期可降至 30 |
| `maxPatternBars` | 150 | 形态最大跨度 | 超过则结构松散，可靠性下降 |
| `stopATR` | 1.5 | 止损 = 颈线 + N×ATR | 高波动品种 2.0 |

---

### 形态 2：头肩底 (Head & Shoulders Bottom)

| 项目 | 内容 |
|------|------|
| 识别要点 | 头肩顶的镜像：三个谷，中间谷（头）最低，两肩近似等高，两峰连线为颈线 |
| 方向 | **看涨** |
| 失效条件 | 右肩低于头部；或跌破头部低点；或突破颈线后回落至颈线下方超过 2×ATR |
| 可检测性 | **B**（必须附加前置下跌趋势确认，否则在上涨中继中大量误报） |
| 最小K线数 | 40（整形态 40~150 根）+ 前置趋势 20 根 |
| 关键阈值 | 同头肩顶；前置趋势 `minPriorBars=20`、`minPriorDrop=0.05` |
| 样本频次 | 26 次（另有 50 次未区分顶底） |

```
结构同上反转：(L,H,L,H,L)
额外条件：形态前需有一段 ≥20 根 K 线的下跌趋势（跌幅 >5%）
其余阈值同头肩顶
方向：看涨
```

**为什么头肩底是 B 级而头肩顶是 A 级**：

在上涨趋势中，"回踩—反弹—再回踩"的震荡结构极易满足头肩底的几何约束，但它只是普通的中继整理。因此头肩底**必须**验证形态左侧存在一段实质下跌（跌幅 > 5%，跨度 ≥ 20 根），否则反转逻辑不成立。头肩顶同理在下跌趋势中存在镜像问题，但统计上"上涨中继被误判为头肩顶"的比例显著更低（上涨中继的低点通常逐步抬高，破坏两谷近似等高的约束），故定为 A 级。

```pseudo
function detectHSBottom(pivots, klines, atr, cfg):
    if len(pivots) < 5: return null
    results = []
    for i in range(0, len(pivots) - 4):
        seq = pivots[i : i+5]
        if not matchesTypes(seq, [LOW, HIGH, LOW, HIGH, LOW]): continue
        L1, H1, L2, H2, L3 = seq      # L2 = 头（最低）

        # 1) 头部必须最低
        if not (L2.price < L1.price and L2.price < L3.price): continue

        # 2) 头部突出度
        lowerShoulder = min(L1.price, L3.price)
        if (lowerShoulder - L2.price) / L2.price < cfg.minHeadProminence: continue

        # 3) 两肩近似等高
        if abs(L1.price - L3.price) / L2.price > cfg.peakTolerance: continue

        # 4) 肩部间距
        if L2.index - L1.index < cfg.minHeadSeparation:  continue
        if L3.index - L2.index < cfg.minShoulderSeparation: continue

        # 5) 前置下跌趋势确认（B 级判定的核心）
        prior = evalPriorTrend(klines, L1.index, cfg.minPriorBars)   # 20
        if prior == null: continue
        if prior.drop < cfg.minPriorDrop: continue                   # 0.05 (5%)
        if prior.direction != DOWN: continue

        # 6) 两峰不应显著抬升（否则是上升通道）
        if (H2.price - H1.price) / H1.price > cfg.maxNeckPeakDivergence: continue  # 0.05

        # 7) 颈线与突破
        neckline = extractNecklineSloped(H1, H2, L2.price)
        if neckline == null: continue
        neckPrice = neckline.priceAt(L3.index)

        brk = confirmNecklineBreak(klines, neckPrice, BULLISH, atr, cfg)
        if brk == null: continue

        height = neckPrice - L2.price
        results.add(Pattern(
            type      = HS_BOTTOM,
            direction = BULLISH,
            entry     = brk.price,
            stop      = neckPrice - cfg.stopATR * atr,
            target1   = brk.price + height * 1.0,
            target2   = brk.price + height * 1.618,
            height    = height,
            points    = seq,
            priorTrend= prior
        ))
    return results

function evalPriorTrend(klines, endIndex, lookback):
    if endIndex - lookback < 0: return null
    seg = klines[endIndex - lookback : endIndex]
    startPrice = seg[0].high
    endPrice   = seg[-1].low
    drop = (startPrice - endPrice) / startPrice
    lowerHighs = countConsecutiveLowerHighs(seg)
    return TrendSegment(direction = (DOWN if drop > 0 else UP),
                        drop = drop, bars = lookback, quality = lowerHighs)
```

**头肩底附加参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `minPriorBars` | 20 | 前置趋势最小跨度（根） |
| `minPriorDrop` | 0.05 | 前置趋势最小跌幅（5%） |
| `maxNeckPeakDivergence` | 0.05 | 两峰最大抬升比例（防上升通道误判） |
| `priorTrendMinLowerHighs` | 2 | 前置段至少出现 2 个递降高点（趋势质量） |

---

### 形态 3：双顶 / M顶 (Double Top)

| 项目 | 内容 |
|------|------|
| 识别要点 | 两个近似等高的峰，中间夹一个明显回撤谷，跌破谷底确认 |
| 方向 | **看跌** |
| 失效条件 | 价格突破第二峰高点（演变为双底失败/新高延续）；或两峰间距超过 120 根（结构失效） |
| 可检测性 | **A** |
| 最小K线数 | 25（整形态 25~130 根） |
| 关键阈值 | `peakTolerance=0.03`、`minValleyDepth=0.05`、`minPeakGap=10`、`maxPeakGap=120` |
| 样本频次 | 与双底共享别名归入统计（M顶/W底） |

```
结构：(H, L, H) 三个摆动点
判定条件：
  - 两峰价差 ≤ 3%
  - 中间谷深 ≥ 峰高的 5%（即 (H-L)/H >= 0.05）
  - 两峰间距 10~120 根 K 线
  - 价格跌破中间谷的低点（颈线）
方向：看跌
```

```pseudo
function detectDoubleTop(pivots, klines, atr, cfg):
    if len(pivots) < 3: return null
    results = []
    for i in range(0, len(pivots) - 2):
        seq = pivots[i : i+3]
        if not matchesTypes(seq, [HIGH, LOW, HIGH]): continue
        H1, L1, H2 = seq
        refPrice = max(H1.price, H2.price)

        # 1) 两峰近似等高
        if abs(H1.price - H2.price) / refPrice > cfg.peakTolerance: continue

        # 2) 中间谷深度：(H - L) / H >= 5%
        valleyDepth = (refPrice - L1.price) / refPrice
        if valleyDepth < cfg.minValleyDepth: continue       # 0.05
        if valleyDepth > cfg.maxValleyDepth: continue       # 0.35，过深视为 V 型/其他结构

        # 3) 峰间距
        gap = H2.index - H1.index
        if gap < cfg.minPeakGap or gap > cfg.maxPeakGap: continue   # 10 / 120

        # 4) 谷的位置：应在两峰中部（不对称双顶可靠性低）
        posRatio = (L1.index - H1.index) / gap
        if posRatio < cfg.valleyPosMin or posRatio > cfg.valleyPosMax: continue  # 0.25 / 0.75

        # 5) 颈线 = 中间谷低点（水平）
        neckPrice = L1.price

        # 6) 突破确认（跌破颈线）
        brk = confirmNecklineBreak(klines, neckPrice, BEARISH, atr, cfg)
        if brk == null: continue

        height = refPrice - neckPrice
        results.add(Pattern(
            type      = DOUBLE_TOP,
            direction = BEARISH,
            entry     = brk.price,
            stop      = refPrice + cfg.stopATR * atr,
            target1   = brk.price - height * 1.0,
            target2   = brk.price - height * 1.618,
            height    = height,
            points    = seq,
            spanBars  = gap
        ))
    return results
```

**双顶参数表**：

| 参数 | 默认值 | 含义 | 说明 |
|------|-------|------|------|
| `peakTolerance` | 0.03 | 两峰相对价差上限 | 加密货 0.035 |
| `minValleyDepth` | 0.05 | 中间谷最小回撤深度 | 低于此值两峰粘连，不是有效 M 顶 |
| `maxValleyDepth` | 0.35 | 中间谷最大回撤深度 | 超过则更像 V 型反转 |
| `minPeakGap` | 10 | 两峰最小间距（根） | 太近则只是双针探顶 |
| `maxPeakGap` | 120 | 两峰最大间距（根） | 太远则两峰无关联 |
| `valleyPosMin` | 0.25 | 谷在两峰间的最早位置比例 | 对称性约束 |
| `valleyPosMax` | 0.75 | 谷在两峰间的最晚位置比例 | 对称性约束 |
| `stopATR` | 1.5 | 止损 = 峰价 + N×ATR | 或用颈线 + 1.5×ATR（更紧） |

---

### 形态 4：双底 / W底 (Double Bottom)

| 项目 | 内容 |
|------|------|
| 识别要点 | 两个近似等低的谷，中间夹一个明显反弹峰，突破峰顶确认；第二底量能通常萎缩 |
| 方向 | **看涨** |
| 失效条件 | 价格跌破第二底低点（演变为下降延续）；或两谷间距超过 120 根 |
| 可检测性 | **A** |
| 最小K线数 | 25（整形态 25~130 根） |
| 关键阈值 | `valleyTolerance=0.03`、`minPeakHeight=0.05`、`minValleyGap=10`、`maxValleyGap=120`、`volumeDryUpRatio=1.0` |
| 样本频次 | **37 次 — 第二高频形态** |

```
结构：(L, H, L) 反转
判定条件同双顶，方向看涨
额外：第二底的成交量通常低于第一底（量能萎缩确认）
```

**量能萎缩确认的实现**：双底的第二底成交量低于第一底，代表抛压衰竭。这是一个**加分项而非否决项**——若强制要求会导致大量漏检（尤其在加密市场 24h 交易量分布不均的情况下）。实现方式：量能条件满足时在评分的"成交量确认"维度加分，不满足时不否决。

```pseudo
function detectDoubleBottom(pivots, klines, atr, cfg):
    if len(pivots) < 3: return null
    results = []
    for i in range(0, len(pivots) - 2):
        seq = pivots[i : i+3]
        if not matchesTypes(seq, [LOW, HIGH, LOW]): continue
        L1, H1, L2 = seq
        refPrice = min(L1.price, L2.price)

        # 1) 两谷近似等低
        if abs(L1.price - L2.price) / refPrice > cfg.valleyTolerance: continue   # 0.03

        # 2) 中间峰高度：(H - L) / L >= 5%
        peakHeight = (H1.price - refPrice) / refPrice
        if peakHeight < cfg.minPeakHeight: continue      # 0.05
        if peakHeight > cfg.maxPeakHeight: continue      # 0.35

        # 3) 谷间距
        gap = L2.index - L1.index
        if gap < cfg.minValleyGap or gap > cfg.maxValleyGap: continue   # 10 / 120

        # 4) 中间峰位置对称性
        posRatio = (H1.index - L1.index) / gap
        if posRatio < cfg.peakPosMin or posRatio > cfg.peakPosMax: continue  # 0.25 / 0.75

        # 5) 量能萎缩确认（加分项，不否决）
        vol1 = meanVolume(klines, L1.index, cfg.volWindow)   # 第一底附近均量
        vol2 = meanVolume(klines, L2.index, cfg.volWindow)   # 第二底附近均量
        volumeDryUp = (vol2 < vol1 * cfg.volumeDryUpRatio)   # 1.0 → 第二底量能更小

        # 6) 颈线 = 中间峰高点
        neckPrice = H1.price

        # 7) 突破确认（向上突破颈线）
        brk = confirmNecklineBreak(klines, neckPrice, BULLISH, atr, cfg)
        if brk == null: continue

        height = neckPrice - refPrice
        results.add(Pattern(
            type         = DOUBLE_BOTTOM,
            direction    = BULLISH,
            entry        = brk.price,
            stop         = refPrice - cfg.stopATR * atr,
            target1      = brk.price + height * 1.0,
            target2      = brk.price + height * 1.618,
            height       = height,
            points       = seq,
            spanBars     = gap,
            volumeDryUp  = volumeDryUp,      # 传入评分模型，命中则量能维度额外加分
            vol1         = vol1,
            vol2         = vol2
        ))
    return results
```

**双底参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `valleyTolerance` | 0.03 | 两谷相对价差上限 |
| `minPeakHeight` | 0.05 | 中间峰最小反弹高度（5%） |
| `maxPeakHeight` | 0.35 | 中间峰最大反弹高度 |
| `minValleyGap` | 10 | 两谷最小间距（根） |
| `maxValleyGap` | 120 | 两谷最大间距（根） |
| `peakPosMin` / `peakPosMax` | 0.25 / 0.75 | 中间峰位置对称区间 |
| `volWindow` | 5 | 量能比较窗口（根） |
| `volumeDryUpRatio` | 1.0 | 第二底量能 < 第一底 × 该比值 → 确认量能萎缩 |
| `stopATR` | 1.5 | 止损 = 谷价 − N×ATR |

---

### 形态 5：三重顶 (Triple Top)

| 项目 | 内容 |
|------|------|
| 识别要点 | 三个近似等高的峰，中间夹两个近似等低的谷，跌破谷底连线确认 |
| 方向 | **看跌** |
| 失效条件 | 任一峰显著高于另外两峰（演变为头肩顶或上升通道）；或跌破后快速收回颈线上方 |
| 可检测性 | **B**（结构要求严格，样本稀少，参数标定数据不足） |
| 最小K线数 | 50（整形态 50~180 根） |
| 关键阈值 | `peakTolerance=0.03`、`valleyTolerance=0.05`、`minPeakGap=10`、`maxTotalSpan=180` |
| 样本频次 | 样本集中未单独统计（并入其他/未指明） |

```
结构：(H,L,H,L,H)
三峰高度差均 ≤ 3%，两谷高度差 ≤ 5%
方向：看跌
```

**与头肩顶的消歧规则**：三重顶与头肩顶结构同为 `(H,L,H,L,H)`，必须明确分流：

| 判据 | 三重顶 | 头肩顶 |
|------|-------|--------|
| 中间峰高度 | 三峰近似等高（差 ≤ 3%） | 中间峰显著高于两肩（≥ 2%） |
| 优先判定 | 头肩顶优先 | 先跑头肩顶，未命中再跑三重顶 |

```pseudo
function detectTripleTop(pivots, klines, atr, cfg):
    if len(pivots) < 5: return null
    results = []
    for i in range(0, len(pivots) - 4):
        seq = pivots[i : i+5]
        if not matchesTypes(seq, [HIGH, LOW, HIGH, LOW, HIGH]): continue
        H1, L1, H2, L2, H3 = seq
        refPrice = max(H1.price, H2.price, H3.price)

        # 1) 三峰两两价差均 ≤ 3%
        peaks = [H1.price, H2.price, H3.price]
        if (max(peaks) - min(peaks)) / refPrice > cfg.peakTolerance: continue   # 0.03

        # 2) 两谷价差 ≤ 5%
        valleys = [L1.price, L2.price]
        if abs(L1.price - L2.price) / refPrice > cfg.valleyTolerance: continue  # 0.05

        # 3) 排除头肩顶：中间峰不得显著突出
        sideMax = max(H1.price, H3.price)
        if (H2.price - sideMax) / refPrice > cfg.maxCenterProminence: continue  # 0.01

        # 4) 相邻峰间距
        if H2.index - H1.index < cfg.minPeakGap: continue   # 10
        if H3.index - H2.index < cfg.minPeakGap: continue

        # 5) 谷深
        if (refPrice - min(L1.price, L2.price)) / refPrice < cfg.minValleyDepth: continue  # 0.04

        # 6) 总跨度
        span = H3.index - H1.index
        if span < cfg.minTotalSpan or span > cfg.maxTotalSpan: continue   # 50 / 180

        # 7) 颈线 = 两谷均值（水平）
        neckPrice = (L1.price + L2.price) / 2

        brk = confirmNecklineBreak(klines, neckPrice, BEARISH, atr, cfg)
        if brk == null: continue

        height = refPrice - neckPrice
        results.add(Pattern(
            type      = TRIPLE_TOP,
            direction = BEARISH,
            entry     = brk.price,
            stop      = refPrice + cfg.stopATR * atr,
            target1   = brk.price - height * 1.0,
            target2   = brk.price - height * 1.618,
            height    = height,
            points    = seq,
            spanBars  = span
        ))
    return results
```

**三重顶参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `peakTolerance` | 0.03 | 三峰极差上限 |
| `valleyTolerance` | 0.05 | 两谷相对价差上限 |
| `maxCenterProminence` | 0.01 | 中间峰最大突出度（超过则为头肩顶） |
| `minPeakGap` | 10 | 相邻峰最小间距 |
| `minValleyDepth` | 0.04 | 谷相对峰的最小回撤 |
| `minTotalSpan` / `maxTotalSpan` | 50 / 180 | 形态总跨度区间 |
| `stopATR` | 1.5 | 止损倍数 |

---

### 形态 6：三重底 (Triple Bottom)

| 项目 | 内容 |
|------|------|
| 识别要点 | 三重顶的镜像：三个近似等低的谷，中间夹两个近似等高的峰，突破峰顶连线确认 |
| 方向 | **看涨** |
| 失效条件 | 任一谷显著低于另外两谷（演变为头肩底或下降通道）；或突破后回落至颈线下方 |
| 可检测性 | **B**（需前置下跌趋势确认） |
| 最小K线数 | 50（+ 前置趋势 20 根） |
| 关键阈值 | 同三重顶 + `minPriorBars=20`、`minPriorDrop=0.05` |
| 样本频次 | 样本集中未单独统计 |

```
反转版本，需前置下跌趋势
```

```pseudo
function detectTripleBottom(pivots, klines, atr, cfg):
    if len(pivots) < 5: return null
    results = []
    for i in range(0, len(pivots) - 4):
        seq = pivots[i : i+5]
        if not matchesTypes(seq, [LOW, HIGH, LOW, HIGH, LOW]): continue
        L1, H1, L2, H2, L3 = seq
        refPrice = min(L1.price, L2.price, L3.price)

        # 1) 三谷极差 ≤ 3%
        valleys = [L1.price, L2.price, L3.price]
        if (max(valleys) - min(valleys)) / refPrice > cfg.valleyTolerance: continue  # 0.03

        # 2) 两峰价差 ≤ 5%
        if abs(H1.price - H2.price) / refPrice > cfg.peakTolerance: continue          # 0.05

        # 3) 排除头肩底：中间谷不得显著下凹
        sideMin = min(L1.price, L3.price)
        if (sideMin - L2.price) / refPrice > cfg.maxCenterProminence: continue       # 0.01

        # 4) 相邻谷间距
        if L2.index - L1.index < cfg.minValleyGap: continue   # 10
        if L3.index - L2.index < cfg.minValleyGap: continue

        # 5) 前置下跌趋势确认
        prior = evalPriorTrend(klines, L1.index, cfg.minPriorBars)
        if prior == null or prior.drop < cfg.minPriorDrop: continue    # 20 / 0.05

        # 6) 峰高
        if (max(H1.price, H2.price) - refPrice) / refPrice < cfg.minPeakHeight: continue  # 0.04

        # 7) 总跨度
        span = L3.index - L1.index
        if span < cfg.minTotalSpan or span > cfg.maxTotalSpan: continue   # 50 / 180

        # 8) 颈线 = 两峰均值
        neckPrice = (H1.price + H2.price) / 2

        brk = confirmNecklineBreak(klines, neckPrice, BULLISH, atr, cfg)
        if brk == null: continue

        height = neckPrice - refPrice
        results.add(Pattern(
            type      = TRIPLE_BOTTOM,
            direction = BULLISH,
            entry     = brk.price,
            stop      = refPrice - cfg.stopATR * atr,
            target1   = brk.price + height * 1.0,
            target2   = brk.price + height * 1.618,
            height    = height,
            points    = seq,
            spanBars  = span,
            priorTrend= prior
        ))
    return results
```

**三重底参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `valleyTolerance` | 0.03 | 三谷极差上限 |
| `peakTolerance` | 0.05 | 两峰相对价差上限 |
| `maxCenterProminence` | 0.01 | 中间谷最大下凹度（超过则为头肩底） |
| `minValleyGap` | 10 | 相邻谷最小间距 |
| `minPeakHeight` | 0.04 | 峰相对谷的最小反弹 |
| `minPriorBars` / `minPriorDrop` | 20 / 0.05 | 前置下跌趋势要求 |
| `minTotalSpan` / `maxTotalSpan` | 50 / 180 | 形态总跨度区间 |
| `stopATR` | 1.5 | 止损倍数 |

---

### 形态 7：上升三角形 (Ascending Triangle)

| 项目 | 内容 |
|------|------|
| 识别要点 | 水平（或微升）阻力线 + 上升支撑线，卖方在同一价位被反复消化，买方低点不断抬高 |
| 方向 | **看涨**（突破上边界） |
| 失效条件 | 跌破上升支撑线（下边界）且跌幅 ≥ 0.5×ATR；或形态持续时间超过收敛交叉点后仍未突破 |
| 可检测性 | **A** |
| 最小K线数 | 30（整形态 30~120 根） |
| 关键阈值 | `upperSlopeMax=+0.02`、`lowerSlopeMin=+0.03`、`minTouches=4`、`maxApexBars=30` |
| 样本频次 | 17 次 |

```
上边界：水平或微升（slope ≤ +2%）
下边界：上升（slope > +3%）
至少 4 个触点（上下各 ≥2）
收敛交叉点在未来 ≤ 30 根 K 线内
方向：看涨（突破上边界）
```

```pseudo
function detectAscendingTriangle(pivots, klines, atr, cfg):
    # 上边界用摆动高点拟合，下边界用摆动低点拟合
    upper = fitTrendline(pivots, DOWN, minTouches=2, tolerance=cfg.tolerance)
    lower = fitTrendline(pivots, UP,   minTouches=2, tolerance=cfg.tolerance)
    if upper == null or lower == null: return null

    # 1) 上边界：水平或微升，总斜率 ≤ +2%
    if upper.totalSlope > cfg.upperSlopeMax:  return null   # +0.02
    if upper.totalSlope < cfg.upperSlopeMin:  return null   # -0.005，明显下降则是下降三角/对称三角

    # 2) 下边界：上升，总斜率 > +3%
    if lower.totalSlope < cfg.lowerSlopeMin:  return null   # +0.03

    # 3) 触点总数 ≥ 4，且上下各 ≥ 2
    if upper.touches + lower.touches < cfg.minTouches: return null       # 4
    if upper.touches < 2 or lower.touches < 2: return null

    # 4) 收敛：两线向右收敛而非发散
    apexIndex = intersectIndex(upper, lower)
    if apexIndex == null: return null                       # 平行线 → 是通道，不是三角形
    span = apexIndex - lower.startIndex
    if span > cfg.maxApexBars * cfg.apexSpanFactor: return null   # 收敛太慢

    # 5) 形态跨度
    patSpan = lastTouchIndex(upper, lower) - firstTouchIndex(upper, lower)
    if patSpan < cfg.minPatternBars or patSpan > cfg.maxPatternBars: return null  # 30 / 120

    # 6) 形态高度 = 起点处两线垂直距离（左端最宽处）
    height = upper.priceAt(lower.startIndex) - lower.priceAt(lower.startIndex)
    if height < cfg.minHeightATR * atr: return null         # 1.5 × ATR，太窄无交易价值

    # 7) 突破确认：向上突破上边界
    brk = confirmBoundaryBreak(klines, upper, BULLISH, atr, cfg)
    if brk == null: return null

    results.add(Pattern(
        type      = ASCENDING_TRIANGLE,
        direction = BULLISH,
        entry     = brk.price,
        stop      = lower.priceAt(brk.index) - cfg.stopATR * atr,
        target1   = brk.price + height * 1.0,
        target2   = brk.price + height * 1.618,
        height    = height,
        upperLine = upper,
        lowerLine = lower,
        spanBars  = patSpan
    ))
    return results

function confirmBoundaryBreak(klines, line, direction, atr, cfg):
    for j in range(line.lastTouchIndex, len(klines)):
        linePrice = line.priceAt(j)
        k = klines[j]
        dist = (k.close - linePrice) if direction == BULLISH else (linePrice - k.close)
        if dist >= cfg.minBreakATR * atr:
            if dist >= cfg.strongBreakATR * atr:
                return Breakout(j, k.close, STRONG)
            confirm = countClosesBeyond(klines, j+1, j+cfg.confirmBars, line, direction)
            if confirm >= cfg.requiredConfirm:
                return Breakout(j, k.close, NORMAL)
    return null
```

**上升三角形参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `upperSlopeMax` | +0.02 | 上边界总斜率上限（+2%） |
| `upperSlopeMin` | -0.005 | 上边界总斜率下限（-0.5%），低于此则不是上升三角 |
| `lowerSlopeMin` | +0.03 | 下边界总斜率下限（+3%） |
| `minTouches` | 4 | 总触点数下限（上下各 ≥ 2） |
| `tolerance` | 0.02 | 触点容差（相对价格） |
| `maxApexBars` | 30 | 收敛交叉点距当前的最大 K 线数 |
| `apexSpanFactor` | 4.0 | 交叉点距离与形态跨度之比上限 |
| `minPatternBars` / `maxPatternBars` | 30 / 120 | 形态跨度区间 |
| `minHeightATR` | 1.5 | 形态最小高度（× ATR） |
| `stopATR` | 1.5 | 止损倍数（下边界下方） |

---

### 形态 8：下降三角形 (Descending Triangle)

| 项目 | 内容 |
|------|------|
| 识别要点 | 下降阻力线 + 水平（或微降）支撑线，买方在同一价位反复承接但反弹高点持续降低 |
| 方向 | **看跌**（跌破下边界） |
| 失效条件 | 突破下降阻力线（上边界）且涨幅 ≥ 0.5×ATR；或形态超过收敛交叉点仍未跌破 |
| 可检测性 | **A** |
| 最小K线数 | 30（整形态 30~120 根） |
| 关键阈值 | `upperSlopeMax=-0.03`、`lowerSlopeMin=-0.02`、`minTouches=4` |
| 样本频次 | **24 次 — 高频** |

```
上边界：下降（slope < -3%）
下边界：水平或微降（slope ≥ -2%）
至少 4 个触点
方向：看跌（跌破下边界）
```

```pseudo
function detectDescendingTriangle(pivots, klines, atr, cfg):
    upper = fitTrendline(pivots, DOWN, minTouches=2, tolerance=cfg.tolerance)
    lower = fitTrendline(pivots, UP,   minTouches=2, tolerance=cfg.tolerance)
    if upper == null or lower == null: return null

    # 1) 上边界：下降，总斜率 < -3%
    if upper.totalSlope > cfg.upperSlopeMax: return null    # -0.03

    # 2) 下边界：水平或微降，总斜率 ≥ -2%
    if lower.totalSlope < cfg.lowerSlopeMin: return null    # -0.02
    if lower.totalSlope > cfg.lowerSlopeMax: return null    # +0.005，明显上升则是上升/对称三角

    # 3) 触点
    if upper.touches + lower.touches < cfg.minTouches: return null   # 4
    if upper.touches < 2 or lower.touches < 2: return null

    # 4) 收敛
    apexIndex = intersectIndex(upper, lower)
    if apexIndex == null: return null
    if apexIndex - lower.startIndex > cfg.maxApexBars * cfg.apexSpanFactor: return null

    # 5) 跨度
    patSpan = lastTouchIndex(upper, lower) - firstTouchIndex(upper, lower)
    if patSpan < cfg.minPatternBars or patSpan > cfg.maxPatternBars: return null  # 30 / 120

    # 6) 高度（左端最宽处）
    height = upper.priceAt(lower.startIndex) - lower.priceAt(lower.startIndex)
    if height < cfg.minHeightATR * atr: return null

    # 7) 跌破下边界确认
    brk = confirmBoundaryBreak(klines, lower, BEARISH, atr, cfg)
    if brk == null: return null

    results.add(Pattern(
        type      = DESCENDING_TRIANGLE,
        direction = BEARISH,
        entry     = brk.price,
        stop      = upper.priceAt(brk.index) + cfg.stopATR * atr,
        target1   = brk.price - height * 1.0,
        target2   = brk.price - height * 1.618,
        height    = height,
        upperLine = upper,
        lowerLine = lower,
        spanBars  = patSpan
    ))
    return results
```

**下降三角形参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `upperSlopeMax` | -0.03 | 上边界总斜率上限（-3%） |
| `lowerSlopeMin` | -0.02 | 下边界总斜率下限（-2%） |
| `lowerSlopeMax` | +0.005 | 下边界总斜率上限（+0.5%） |
| `minTouches` | 4 | 总触点数下限 |
| `tolerance` | 0.02 | 触点容差 |
| `maxApexBars` | 30 | 收敛交叉点最大距离 |
| `minPatternBars` / `maxPatternBars` | 30 / 120 | 形态跨度区间 |
| `minHeightATR` | 1.5 | 形态最小高度（× ATR） |
| `stopATR` | 1.5 | 止损倍数（上边界上方） |

---

### 形态 9：对称三角形 (Symmetrical Triangle)

| 项目 | 内容 |
|------|------|
| 识别要点 | 上边界下降、下边界上升，两线收敛，多空力量均衡；方向由突破侧决定 |
| 方向 | **双向**（必须等待突破确认，禁止预判方向） |
| 失效条件 | 出现第 6 个触点仍未突破（形态过度成熟，收敛后易假突破）；或突破幅度 < 0.5×ATR |
| 可检测性 | **A** |
| 最小K线数 | 30（整形态 30~120 根） |
| 关键阈值 | `upperSlopeMax=-0.02`、`lowerSlopeMin=+0.02`、`slopeRatioMin=0.5`、`slopeRatioMax=2.0`、`minTouches=5` |
| 样本频次 | **24 次 — 高频** |

```
上边界下降（slope < -2%），下边界上升（slope > +2%）
斜率绝对值接近（|slopeUp| / |slopeDown| 在 0.5~2.0 之间）
至少 5 个触点（上下合计）
方向：取决于突破方向（需等待突破确认）
注意：用户的标注中常用 A/B/C/D/E 五点标记法！e点是突破/入场点
```

**A/B/C/D/E 五点标记法的工程映射**（这是本形态最重要的标注惯例）：

| 标记点 | 摆动点类型 | 含义 | 工程处理 |
|-------|-----------|------|---------|
| A | LOW | 第一个低点（下边界起点） | `lower.startIndex` |
| B | HIGH | 第一个高点（上边界起点，最高） | `upper.startIndex` |
| C | LOW | 第二个低点（高于 A） | 下边界第二触点 |
| D | HIGH | 第二个高点（低于 B） | 上边界第二触点 |
| E | — | **突破点 / 入场点** | `brk.index`，通常位于收敛区（形态跨度 60%~85% 处） |

> **E 点的统计特征**：E 点通常出现在形态总跨度的 **60%~85%** 位置（而非接近收敛顶点）。若价格走到收敛顶点仍未突破，往往演变为横向漂移而非有效突破。因此工程上可设**形态有效期**：`E 点最晚出现位置 = 起点 + 0.85 × 跨度`，超过则废弃该形态（记为 `EXPIRED`，不推送）。

```pseudo
function detectSymmetricalTriangle(pivots, klines, atr, cfg):
    upper = fitTrendline(pivots, DOWN, minTouches=2, tolerance=cfg.tolerance)
    lower = fitTrendline(pivots, UP,   minTouches=2, tolerance=cfg.tolerance)
    if upper == null or lower == null: return null

    # 1) 上边界下降
    if upper.totalSlope > cfg.upperSlopeMax: return null    # -0.02
    # 2) 下边界上升
    if lower.totalSlope < cfg.lowerSlopeMin: return null    # +0.02

    # 3) 斜率对称性：|slopeUp| / |slopeDown| ∈ [0.5, 2.0]
    ratio = abs(lower.totalSlope) / abs(upper.totalSlope)
    if ratio < cfg.slopeRatioMin or ratio > cfg.slopeRatioMax: return null  # 0.5 / 2.0

    # 4) 触点 ≥ 5（对称三角比直角三角要求更严，因为方向未定）
    if upper.touches + lower.touches < cfg.minTouches: return null   # 5
    if upper.touches < 2 or lower.touches < 2: return null

    # 5) 收敛
    apexIndex = intersectIndex(upper, lower)
    if apexIndex == null: return null

    startIndex = min(upper.startIndex, lower.startIndex)
    span = apexIndex - startIndex
    if span < cfg.minPatternBars or span > cfg.maxPatternBars: return null  # 30 / 120

    # 6) 高度（左端最宽处）
    height = upper.priceAt(startIndex) - lower.priceAt(startIndex)
    if height < cfg.minHeightATR * atr: return null

    # 7) 双向突破检测：谁先突破算谁的
    brkUp   = confirmBoundaryBreak(klines, upper, BULLISH, atr, cfg)
    brkDown = confirmBoundaryBreak(klines, lower, BEARISH, atr, cfg)

    if brkUp == null and brkDown == null:
        # 未突破：检查是否已过期（超过 E 点最晚位置）
        if len(klines) - startIndex > cfg.maxWaitRatio * span:   # 0.85
            return Pattern(type=SYMMETRICAL_TRIANGLE, status=EXPIRED)
        return null

    brk     = (brkUp if brkUp != null else brkDown)
    upFirst = (brkUp != null) and (brkDown == null or brkUp.index <= brkDown.index)
    direction = BULLISH if upFirst else BEARISH

    results.add(Pattern(
        type      = SYMMETRICAL_TRIANGLE,
        direction = direction,
        entry     = brk.price,
        stop      = (lower.priceAt(brk.index) - cfg.stopATR * atr) if upFirst
                    else (upper.priceAt(brk.index) + cfg.stopATR * atr),
        target1   = (brk.price + height * 1.0) if upFirst else (brk.price - height * 1.0),
        target2   = (brk.price + height * 1.618) if upFirst else (brk.price - height * 1.618),
        height    = height,
        upperLine = upper,
        lowerLine = lower,
        spanBars  = span,
        pointE    = brk.index,
        abcde     = [lower.startIndex, upper.startIndex, ..., brk.index]
    ))
    return results
```

**对称三角形参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `upperSlopeMax` | -0.02 | 上边界总斜率上限（-2%） |
| `lowerSlopeMin` | +0.02 | 下边界总斜率下限（+2%） |
| `slopeRatioMin` | 0.5 | 下/上斜率绝对值之比下限 |
| `slopeRatioMax` | 2.0 | 下/上斜率绝对值之比上限 |
| `minTouches` | 5 | 总触点数下限（多于直角三角） |
| `tolerance` | 0.02 | 触点容差 |
| `minPatternBars` / `maxPatternBars` | 30 / 120 | 形态跨度区间 |
| `minHeightATR` | 1.5 | 形态最小高度（× ATR） |
| `maxWaitRatio` | 0.85 | E 点最晚位置 = 起点 + ratio × 跨度 |
| `maxTouchesBeforeExpire` | 6 | 超过此触点数仍未突破则判定形态过度成熟 |
| `stopATR` | 1.5 | 止损倍数 |

---

### 形态 10：旗形 (Flag)

| 项目 | 内容 |
|------|------|
| 识别要点 | 陡峭旗杆后的短暂反向倾斜整理，两条边界近似平行，突破后延续原趋势 |
| 方向 | **与旗杆同向**（顺趋势持续形态） |
| 失效条件 | 旗身持续时间 > 25 根（整理过久，动量衰竭）；或旗身向旗杆同向倾斜（那是趋势延续不是整理）；或跌破旗杆起点 |
| 可检测性 | **A** |
| 最小K线数 | 20（旗杆 5~15 根 + 旗身 5~25 根） |
| 样本频次 | 7 次 |

```
前提：旗形前有一段陡峭的"旗杆"（涨幅/跌幅 >5%，<15根K线完成）
旗身：两条近似平行的趋势线（斜率差 < 1%）
旗身持续时间 5~25 根 K 线
旗身斜率与旗杆反向（上涨趋势中的旗形向下倾斜）
方向：与旗杆同向（突破旗身后继续原趋势）
```

**旗形与其他形态的本质区别**：旗形是**唯一以旗杆而非旗身高度作为目标位基准**的形态。旗杆代表被暂时消化的动量，旗身只是"休息"。这意味着：

- 旗形的目标位 = 旗杆高度 × 投射倍数，从**旗身突破点**起算（经典用法）或从**旗杆起点**起算（另一种流派，本文档采用前者）。
- 旗身越短，信号越强（整理时间越短，动量保存越完整）。这一特性应直接写入评分模型的"形态时长合理性"维度。

```pseudo
function detectFlag(pivots, klines, atr, cfg):
    results = []
    # 1) 遍历所有可能的旗杆：一段快速大幅单向运动
    for pole in detectPoles(klines, cfg):
        # 旗杆：5%~? 幅度，≤15 根完成
        if abs(pole.move) < cfg.poleMinMove: continue         # 0.05
        if abs(pole.move) > cfg.poleMaxMove: continue         # 0.30，过大则不可持续
        if pole.bars > cfg.poleMaxBars: continue              # 15
        if pole.bars < cfg.poleMinBars: continue              # 5

        poleDir = BULLISH if pole.move > 0 else BEARISH

        # 2) 旗身区间：旗杆终点之后
        bodyStart = pole.endIndex
        bodyEnd   = min(bodyStart + cfg.bodyMaxBars, len(klines) - 1)   # +25
        bodyPivots = pivots.filter(index >= bodyStart and index <= bodyEnd)
        if len(bodyPivots) < 4: continue

        # 3) 拟合旗身两条边界
        upper = fitTrendline(bodyPivots, DOWN, minTouches=2, tolerance=cfg.tolerance)
        lower = fitTrendline(bodyPivots, UP,   minTouches=2, tolerance=cfg.tolerance)
        if upper == null or lower == null: continue

        # 4) 近似平行：斜率差 < 1%
        slopeDiff = abs(upper.totalSlope - lower.totalSlope)
        if slopeDiff > cfg.maxSlopeDiff: continue             # 0.01

        # 5) 旗身斜率与旗杆反向
        bodySlope = (upper.totalSlope + lower.totalSlope) / 2
        if poleDir == BULLISH and bodySlope >= cfg.antiTrendSlope: continue   # 上涨旗杆需旗身向下/走平
        if poleDir == BEARISH and bodySlope <= -cfg.antiTrendSlope: continue  # 0.001

        # 6) 旗身持续时间
        bodyBars = bodyEnd - bodyStart
        if bodyBars < cfg.bodyMinBars or bodyBars > cfg.bodyMaxBars: continue  # 5 / 25

        # 7) 旗身回撤幅度：不应回吐过多旗杆（通常 1/3 ~ 2/3）
        retrace = abs(bodyStart_price - bodyLowestOrHighest) / abs(pole.priceRange)
        if retrace < cfg.minRetrace or retrace > cfg.maxRetrace: continue     # 0.20 / 0.75

        # 8) 突破确认：与旗杆同向突破旗身边界
        breakLine = (upper if poleDir == BULLISH else lower)
        brk = confirmBoundaryBreak(klines, breakLine, poleDir, atr, cfg)
        if brk == null: continue

        poleHeight = abs(pole.priceRange)
        results.add(Pattern(
            type       = FLAG,
            direction  = poleDir,
            entry      = brk.price,
            stop       = (lower.priceAt(brk.index) - cfg.stopATR * atr) if poleDir==BULLISH
                         else (upper.priceAt(brk.index) + cfg.stopATR * atr),
            target1    = (brk.price + poleHeight * 1.0)   if poleDir==BULLISH
                         else (brk.price - poleHeight * 1.0),
            target2    = (brk.price + poleHeight * 1.618) if poleDir==BULLISH
                         else (brk.price - poleHeight * 1.618),
            height     = poleHeight,        # 注意：旗形高度 = 旗杆高度
            pole       = pole,
            bodyBars   = bodyBars,
            spanBars   = pole.bars + bodyBars
        ))
    return results

function detectPoles(klines, cfg):
    poles = []
    for i in range(cfg.poleMinBars, len(klines) - cfg.poleMinBars):
        for L in range(cfg.poleMinBars, min(cfg.poleMaxBars, len(klines)-i)):
            seg = klines[i : i+L]
            # 要求段内单向性强：收盘方向一致的比例高
            move = (seg[-1].close - seg[0].close) / seg[0].close
            if abs(move) < cfg.poleMinMove: continue
            monotonicity = countSameDirectionCloses(seg) / L
            if monotonicity < cfg.poleMinMonotonicity: continue      # 0.65
            poles.add(Pole(startIndex=i, endIndex=i+L, bars=L, move=move,
                           priceRange = (max(seg.high) - min(seg.low))))
    return dedupeOverlappingPoles(poles)     # 保留幅度最大的，删除重叠的
```

**旗形参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `poleMinMove` | 0.05 | 旗杆最小幅度（5%） |
| `poleMaxMove` | 0.30 | 旗杆最大幅度（30%） |
| `poleMinBars` | 5 | 旗杆最短 K 线数 |
| `poleMaxBars` | 15 | 旗杆最长 K 线数 |
| `poleMinMonotonicity` | 0.65 | 旗杆单向性（同向收盘占比）下限 |
| `maxSlopeDiff` | 0.01 | 旗身两边界斜率差上限（1%） |
| `antiTrendSlope` | 0.001 | 判定旗身反向倾斜的斜率阈值 |
| `bodyMinBars` | 5 | 旗身最短持续时间 |
| `bodyMaxBars` | 25 | 旗身最长持续时间 |
| `minRetrace` / `maxRetrace` | 0.20 / 0.75 | 旗身回吐旗杆幅度区间 |
| `tolerance` | 0.015 | 旗身触点容差（旗形规模小，需更紧） |
| `stopATR` | 1.5 | 止损倍数 |

---

### 形态 11：三角旗形 (Pennant)

| 项目 | 内容 |
|------|------|
| 识别要点 | 旗杆后的小型对称三角整理，边界收敛，比旗形更紧凑 |
| 方向 | **与旗杆同向**（顺趋势持续） |
| 失效条件 | 旗身 > 15 根；或旗身边界发散（那是扩张三角/喇叭口，本文档不处理） |
| 可检测性 | **B**（与对称三角形在几何上高度重叠，需靠"旗杆存在"与"规模小"两个外部条件区分） |
| 最小K线数 | 15（旗杆 5~15 根 + 旗身 5~15 根） |
| 样本频次 | 样本集中未单独统计（少见） |

```
类似旗形但边界收敛（像小号对称三角形）
旗杆同样需要存在
比旗形更紧凑（持续 5~15 根）
```

**与对称三角形的消歧**（关键）：

| 判据 | 三角旗形 | 对称三角形 |
|------|---------|-----------|
| 前置旗杆 | **必须有**（幅度 ≥ 5%，≤ 15 根） | 无要求 |
| 形态规模 | 小（旗身 5~15 根） | 大（30~120 根） |
| 触点数 | 4（上下各 2） | ≥ 5 |
| 性质 | 持续形态（顺旗杆） | 中性（由突破决定） |
| 目标位基准 | 旗杆高度 | 三角形左端宽度 |

```pseudo
function detectPennant(pivots, klines, atr, cfg):
    results = []
    for pole in detectPoles(klines, cfg):
        if abs(pole.move) < cfg.poleMinMove: continue      # 0.05
        if pole.bars > cfg.poleMaxBars: continue           # 15
        poleDir = BULLISH if pole.move > 0 else BEARISH

        bodyStart = pole.endIndex
        bodyEnd   = min(bodyStart + cfg.bodyMaxBars, len(klines) - 1)   # +15
        bodyPivots = pivots.filter(index >= bodyStart and index <= bodyEnd)
        if len(bodyPivots) < 4: continue

        upper = fitTrendline(bodyPivots, DOWN, minTouches=2, tolerance=cfg.tolerance)
        lower = fitTrendline(bodyPivots, UP,   minTouches=2, tolerance=cfg.tolerance)
        if upper == null or lower == null: continue

        # 1) 收敛（与旗形的平行要求相反）
        if upper.totalSlope >= 0: continue      # 上边界必须下降
        if lower.totalSlope <= 0: continue      # 下边界必须上升
        slopeDiff = abs(upper.totalSlope - lower.totalSlope)
        if slopeDiff < cfg.minSlopeDiff: continue     # 0.005，太平行则是旗形

        apexIndex = intersectIndex(upper, lower)
        if apexIndex == null: continue

        # 2) 规模小
        bodyBars = bodyEnd - bodyStart
        if bodyBars < cfg.bodyMinBars or bodyBars > cfg.bodyMaxBars: continue   # 5 / 15

        # 3) 触点
        if upper.touches + lower.touches < cfg.minTouches: continue    # 4

        # 4) 突破：与旗杆同向
        breakLine = (upper if poleDir == BULLISH else lower)
        brk = confirmBoundaryBreak(klines, breakLine, poleDir, atr, cfg)
        if brk == null: continue

        poleHeight = abs(pole.priceRange)
        results.add(Pattern(
            type      = PENNANT,
            direction = poleDir,
            entry     = brk.price,
            stop      = (lower.priceAt(brk.index) - cfg.stopATR * atr) if poleDir==BULLISH
                        else (upper.priceAt(brk.index) + cfg.stopATR * atr),
            target1   = (brk.price + poleHeight * 1.0)   if poleDir==BULLISH
                        else (brk.price - poleHeight * 1.0),
            target2   = (brk.price + poleHeight * 1.618) if poleDir==BULLISH
                        else (brk.price - poleHeight * 1.618),
            height    = poleHeight,
            pole      = pole,
            bodyBars  = bodyBars
        ))
    return results
```

**三角旗形参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `poleMinMove` | 0.05 | 旗杆最小幅度 |
| `poleMaxBars` | 15 | 旗杆最长 K 线数 |
| `bodyMinBars` | 5 | 旗身最短持续时间 |
| `bodyMaxBars` | 15 | 旗身最长持续时间（比旗形更紧凑） |
| `minSlopeDiff` | 0.005 | 两边界斜率差下限（区分旗形的平行） |
| `minTouches` | 4 | 总触点数下限（上下各 2） |
| `tolerance` | 0.012 | 触点容差（规模小，需更紧） |
| `stopATR` | 1.5 | 止损倍数 |

---

### 形态 12：上升楔形 (Rising Wedge)

| 项目 | 内容 |
|------|------|
| 识别要点 | 两条边界都向上倾斜但上边界更陡，价格在高位以递减的幅度创新高，动量衰竭 |
| 方向 | **看跌**（突破下边界）— **楔形是反转信号，不是持续信号** |
| 失效条件 | 向上突破上边界且站稳（演变为上升通道延续）；或两边界发散（喇叭口） |
| 可检测性 | **B**（样本仅 2 次，参数标定数据不足；且方向判断依赖"前置上升趋势"这一外部条件） |
| 最小K线数 | 30（整形态 30~100 根） |
| 关键阈值 | `slopeDiffMin=0.02`、`slopeDiffMax=0.08`、`minTouches=5` |
| 样本频次 | 2 次 |

```
两条边界都向上倾斜
上边界斜率 > 下边界斜率（收敛向上）
斜率差通常在 2%~8%
至少 5 个触点
方向：看跌（突破下边界）— 楔形通常是反转信号而非持续！
```

> **方向判断的关键认知**：这是最容易被误用的形态。两条边界都向上、边界收敛——直观上像上涨，但收敛意味着**每次新高的幅度在递减**，是典型的动量衰竭。上升楔形出现在**上升趋势末端**时才是可靠的看跌反转信号。工程上必须验证前置上升趋势的存在，否则在下跌中继反弹中会出现大量误报（这种结构应归类为反弹通道，不是楔形）。

```pseudo
function detectRisingWedge(pivots, klines, atr, cfg):
    upper = fitTrendline(pivots, DOWN, minTouches=2, tolerance=cfg.tolerance)
    lower = fitTrendline(pivots, UP,   minTouches=2, tolerance=cfg.tolerance)
    if upper == null or lower == null: return null

    # 1) 两条边界都向上
    if upper.totalSlope <= cfg.minUpSlope: return null     # +0.005
    if lower.totalSlope <= cfg.minUpSlope: return null

    # 2) 上边界更陡（收敛）
    slopeDiff = upper.totalSlope - lower.totalSlope
    if slopeDiff < cfg.slopeDiffMin: return null           # 0.02
    if slopeDiff > cfg.slopeDiffMax: return null           # 0.08，过陡则形态不稳定

    # 3) 收敛（不能发散）
    apexIndex = intersectIndex(upper, lower)
    if apexIndex == null: return null

    # 4) 触点 ≥ 5
    if upper.touches + lower.touches < cfg.minTouches: return null   # 5
    if upper.touches < 2 or lower.touches < 2: return null

    # 5) 前置上升趋势确认（B 级核心：楔形是反转形态，必须有可反转的趋势）
    prior = evalPriorTrend(klines, lower.startIndex, cfg.minPriorBars)   # 25
    if prior == null: return null
    if prior.direction != UP or prior.rise < cfg.minPriorRise: return null   # 0.08 (8%)

    # 6) 跨度
    startIndex = min(upper.startIndex, lower.startIndex)
    span = apexIndex - startIndex
    if span < cfg.minPatternBars or span > cfg.maxPatternBars: return null  # 30 / 100

    # 7) 高度（左端最宽处）
    height = upper.priceAt(startIndex) - lower.priceAt(startIndex)
    if height < cfg.minHeightATR * atr: return null

    # 8) 跌破下边界确认
    brk = confirmBoundaryBreak(klines, lower, BEARISH, atr, cfg)
    if brk == null: return null

    results.add(Pattern(
        type      = RISING_WEDGE,
        direction = BEARISH,           # 注意：边界向上，方向看跌
        entry     = brk.price,
        stop      = upper.priceAt(brk.index) + cfg.stopATR * atr,
        target1   = brk.price - height * 1.0,
        target2   = brk.price - height * 1.618,
        height    = height,
        upperLine = upper,
        lowerLine = lower,
        spanBars  = span,
        priorTrend= prior
    ))
    return results
```

**上升楔形参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `minUpSlope` | +0.005 | 判定边界"向上"的最小总斜率（+0.5%） |
| `slopeDiffMin` | 0.02 | 上-下斜率差下限（2%） |
| `slopeDiffMax` | 0.08 | 上-下斜率差上限（8%） |
| `minTouches` | 5 | 总触点数下限 |
| `tolerance` | 0.02 | 触点容差 |
| `minPriorBars` | 25 | 前置上升趋势最小跨度 |
| `minPriorRise` | 0.08 | 前置上升趋势最小涨幅（8%） |
| `minPatternBars` / `maxPatternBars` | 30 / 100 | 形态跨度区间 |
| `minHeightATR` | 1.5 | 形态最小高度（× ATR） |
| `stopATR` | 1.5 | 止损倍数 |

---

### 形态 13：下降楔形 (Falling Wedge)

| 项目 | 内容 |
|------|------|
| 识别要点 | 两条边界都向下倾斜但下边界更陡，价格在低位以递减的幅度创新低，抛压衰竭 |
| 方向 | **看涨**（突破上边界） |
| 失效条件 | 向下跌破下边界且站稳（演变为下降通道延续）；或两边界发散 |
| 可检测性 | **B**（样本仅 3 次，需前置下跌趋势确认） |
| 最小K线数 | 30（+ 前置趋势 25 根） |
| 关键阈值 | `slopeDiffMin=0.02`、`slopeDiffMax=0.08`、`minTouches=5`、`minPriorDrop=0.08` |
| 样本频次 | 3 次 |

```
两条边界都向下倾斜，下边界更陡
方向：看涨（突破上边界）
```

```pseudo
function detectFallingWedge(pivots, klines, atr, cfg):
    upper = fitTrendline(pivots, DOWN, minTouches=2, tolerance=cfg.tolerance)
    lower = fitTrendline(pivots, UP,   minTouches=2, tolerance=cfg.tolerance)
    if upper == null or lower == null: return null

    # 1) 两条边界都向下
    if upper.totalSlope >= -cfg.minDownSlope: return null    # -0.005
    if lower.totalSlope >= -cfg.minDownSlope: return null

    # 2) 下边界更陡（收敛）：|lowerSlope| > |upperSlope|
    slopeDiff = abs(lower.totalSlope) - abs(upper.totalSlope)
    if slopeDiff < cfg.slopeDiffMin: return null             # 0.02
    if slopeDiff > cfg.slopeDiffMax: return null             # 0.08

    # 3) 收敛
    apexIndex = intersectIndex(upper, lower)
    if apexIndex == null: return null

    # 4) 触点 ≥ 5
    if upper.touches + lower.touches < cfg.minTouches: return null   # 5
    if upper.touches < 2 or lower.touches < 2: return null

    # 5) 前置下跌趋势确认
    prior = evalPriorTrend(klines, upper.startIndex, cfg.minPriorBars)   # 25
    if prior == null: return null
    if prior.direction != DOWN or prior.drop < cfg.minPriorDrop: return null   # 0.08

    # 6) 跨度
    startIndex = min(upper.startIndex, lower.startIndex)
    span = apexIndex - startIndex
    if span < cfg.minPatternBars or span > cfg.maxPatternBars: return null  # 30 / 100

    # 7) 高度
    height = upper.priceAt(startIndex) - lower.priceAt(startIndex)
    if height < cfg.minHeightATR * atr: return null

    # 8) 向上突破上边界确认
    brk = confirmBoundaryBreak(klines, upper, BULLISH, atr, cfg)
    if brk == null: return null

    results.add(Pattern(
        type      = FALLING_WEDGE,
        direction = BULLISH,           # 注意：边界向下，方向看涨
        entry     = brk.price,
        stop      = lower.priceAt(brk.index) - cfg.stopATR * atr,
        target1   = brk.price + height * 1.0,
        target2   = brk.price + height * 1.618,
        height    = height,
        upperLine = upper,
        lowerLine = lower,
        spanBars  = span,
        priorTrend= prior
    ))
    return results
```

**下降楔形参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `minDownSlope` | 0.005 | 判定边界"向下"的最小斜率绝对值（0.5%） |
| `slopeDiffMin` | 0.02 | \|下斜率\| − \|上斜率\| 下限（2%） |
| `slopeDiffMax` | 0.08 | 同上上限（8%） |
| `minTouches` | 5 | 总触点数下限 |
| `tolerance` | 0.02 | 触点容差 |
| `minPriorBars` | 25 | 前置下跌趋势最小跨度 |
| `minPriorDrop` | 0.08 | 前置下跌趋势最小跌幅（8%） |
| `minPatternBars` / `maxPatternBars` | 30 / 100 | 形态跨度区间 |
| `minHeightATR` | 1.5 | 形态最小高度（× ATR） |
| `stopATR` | 1.5 | 止损倍数 |

**楔形（上升/下降）与三角形的关键区别**：

| 维度 | 楔形 | 三角形 |
|------|------|-------|
| 边界方向 | **两条同向倾斜**（都上或都下） | 一条水平或反向 |
| 形态性质 | **反转**（需前置趋势可反转） | 持续或中性 |
| 前置趋势要求 | **必须有**（≥ 25 根，幅度 ≥ 8%） | 无要求 |
| 典型位置 | 趋势末端 | 趋势中途 |
| 斜率差 | 0.02 ~ 0.08（较宽） | 视类型而定 |

---

### 形态 14：矩形 / 箱体 (Rectangle)

| 项目 | 内容 |
|------|------|
| 识别要点 | 价格在两条近似水平的边界之间反复震荡，多空在区间内完全均衡 |
| 方向 | **双向**（由突破侧决定） |
| 失效条件 | 出现第 5 个触点仍未突破（区间过久，通常演变为更大级别结构）；或箱体高度 < 3×ATR |
| 可检测性 | **A**（几何约束最严格，是**自动化最容易**的形态） |
| 最小K线数 | 25（整形态 25~150 根） |
| 关键阈值 | `maxAbsSlope=0.01`、`minTouchesPerSide=2`、`minHeightATR=3.0` |
| 样本频次 | **0 次**（样本集中完全未出现） |

```
上下两条近似水平的边界（斜率绝对值均 < 1%）
至少各 2 个触点
箱体高度 ≥ 3×ATR(14)（否则太窄无交易价值）
方向：取决于突破方向
备注：虽然用户样本中未出现，但矩形是最规则的形态之一，自动化最容易
```

**为什么样本 0 次仍要实现**：

1. **标注偏差**：人工标注倾向于标记"有故事"的形态（头肩、三角、W底），纯箱体震荡常被视为"无形态"而跳过。这不代表真实市场中不存在矩形。
2. **兜底价值**：矩形是所有边界类形态的退化形式。当三角形两边界斜率趋近 0 时，矩形识别器能捕获三角形识别器因斜率阈值不满足而漏掉的结构。
3. **成本极低**：几何约束最简单，判定逻辑最少，误报率最低，实现与维护成本远低于其他形态。
4. **可交易性最好**：箱体边界清晰，止损位明确（边界外侧 0.5×ATR），风险回报比可直接计算。

```pseudo
function detectRectangle(pivots, klines, atr, cfg):
    upper = fitTrendline(pivots, DOWN, minTouches=2, tolerance=cfg.tolerance)
    lower = fitTrendline(pivots, UP,   minTouches=2, tolerance=cfg.tolerance)
    if upper == null or lower == null: return null

    # 1) 两条边界都近似水平
    if abs(upper.totalSlope) > cfg.maxAbsSlope: return null   # 0.01
    if abs(lower.totalSlope) > cfg.maxAbsSlope: return null

    # 2) 每边至少 2 个触点（总 ≥ 4）
    if upper.touches < cfg.minTouchesPerSide: return null     # 2
    if lower.touches < cfg.minTouchesPerSide: return null

    # 3) 不能收敛（否则是三角形）
    if intersectIndex(upper, lower) != null:
        if intersectIndex(upper, lower) < len(klines) + cfg.minApexDistance: return null  # 30

    # 4) 箱体高度 ≥ 3×ATR
    boxTop    = mean(upper.touchPrices)
    boxBottom = mean(lower.touchPrices)
    height = boxTop - boxBottom
    if height < cfg.minHeightATR * atr: return null           # 3.0 × ATR

    # 5) 跨度
    startIndex = min(upper.startIndex, lower.startIndex)
    endIndex   = max(upper.lastTouchIndex, lower.lastTouchIndex)
    span = endIndex - startIndex
    if span < cfg.minPatternBars or span > cfg.maxPatternBars: return null  # 25 / 150

    # 6) 双向突破
    brkUp   = confirmBoundaryBreak(klines, upper, BULLISH, atr, cfg)
    brkDown = confirmBoundaryBreak(klines, lower, BEARISH, atr, cfg)
    if brkUp == null and brkDown == null: return null

    upFirst   = (brkUp != null) and (brkDown == null or brkUp.index <= brkDown.index)
    brk       = brkUp if upFirst else brkDown
    direction = BULLISH if upFirst else BEARISH

    results.add(Pattern(
        type       = RECTANGLE,
        direction  = direction,
        entry      = brk.price,
        stop       = (boxBottom - cfg.stopATR * atr) if upFirst else (boxTop + cfg.stopATR * atr),
        target1    = (brk.price + height * 1.0)   if upFirst else (brk.price - height * 1.0),
        target2    = (brk.price + height * 1.618) if upFirst else (brk.price - height * 1.618),
        height     = height,
        boxTop     = boxTop,
        boxBottom  = boxBottom,
        upperLine  = upper,
        lowerLine  = lower,
        spanBars   = span
    ))
    return results
```

**矩形参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `maxAbsSlope` | 0.01 | 边界斜率绝对值上限（1%） |
| `minTouchesPerSide` | 2 | 每条边界最小触点数 |
| `tolerance` | 0.015 | 触点容差（箱体边界应更精确） |
| `minHeightATR` | **3.0** | 箱体最小高度（× ATR），高于其他形态 |
| `minApexDistance` | 30 | 两线交点距当前的最小 K 线数（防止近似平行的三角形混入） |
| `minPatternBars` / `maxPatternBars` | 25 / 150 | 形态跨度区间 |
| `stopATR` | 1.5 | 止损倍数（对侧边界外侧） |

---

### 形态 15：圆弧顶 (Rounding Top)

| 项目 | 内容 |
|------|------|
| 识别要点 | 多个摆动高点构成向下凸的平滑弧线，价格在高位缓慢转向，伴随成交量递减 |
| 方向 | **看跌** |
| 失效条件 | 弧线上出现明显尖峰（演变为头肩顶或双顶）；或价格重新创出新高 |
| 可检测性 | **C — 不建议自动化** |
| 最小K线数 | 60（理论值，实际不稳定） |
| 关键阈值 | 无稳定阈值 |
| 样本频次 | 2 次（归类统计） |

```
需要曲率拟合：多个摆动高点构成向下凸的弧线
问题：对 left/right 参数极敏感，不同参数可能完全改变识别结果
建议：不自动化，仅在人工复核时参考
```

**为什么不自动化（技术论证）**：

圆弧形态的识别依赖**曲率拟合**：需要至少 5~7 个摆动高点拟合出一条平滑曲线，并检验其二阶导数为负（向下凸）。这带来三个致命问题：

1. **参数敏感性灾难**：圆弧由**多个**摆动点共同定义。ZigZag 参数从 `5/5` 变到 `4/4`，可能新增或删除一个摆动高点，导致拟合出的曲率符号翻转——同一段行情在两套参数下得出"圆弧顶"与"无形态"两个相反结论。这不是精度问题，是**结论不稳定**。
2. **样本量不足**：全部样本集中仅 2 次，无法标定阈值的统计分布，任何参数都是拍脑袋。
3. **与头肩/双顶高度重叠**：圆弧顶在离散摆动点上通常表现为"递减的高点序列"，这与头肩顶的右肩结构难以区分。强行区分会导致头肩顶识别器（最高频形态，26%）的误判率上升——**收益远小于代价**。

**替代方案（推荐）**：不实现独立识别器，改用**间接检测**——当检测到以下组合时，在人工复核界面给出"疑似圆弧顶"提示：

```pseudo
function hintRoundingTop(pivots, klines, cfg):
    # 仅作为人工复核提示，不产生自动信号
    highs = pivots.filter(type=HIGH).lastN(5)
    if len(highs) < 5: return null

    # 1) 高点序列近似单调递减（允许一次反叛）
    inversions = countInversions(highs.price)     # 逆序对数量
    if inversions > cfg.maxInversions: return null        # 1

    # 2) 递减速度均匀：相邻高点跌幅的变异系数小
    drops = [highs[i].price - highs[i+1].price for i in range(len(highs)-1)]
    cv = std(drops) / mean(drops)
    if cv > cfg.maxDropCV: return null                    # 0.8

    # 3) 高点间距均匀（圆弧的时间轴也应平滑）
    gaps = [highs[i+1].index - highs[i].index for i in range(len(highs)-1)]
    if std(gaps) / mean(gaps) > cfg.maxGapCV: return null # 0.6

    # 4) 成交量递减（圆弧顶的典型特征）
    if not isVolumeDeclining(klines, highs[0].index, highs[-1].index): return null

    # 5) 参数敏感性自检：用两套 ZigZag 参数重新检测，结论必须一致
    altHighs = findPivots(klines, left=cfg.altLeft, right=cfg.altRight).filter(HIGH).lastN(5)
    if not similarShape(highs, altHighs): return null     # 不一致 → 放弃，不提示

    return Hint(type=ROUNDING_TOP, confidence=LOW, needsManualReview=true,
                reason="曲率拟合对参数敏感，请人工确认")
```

**圆弧顶提示参数表**（仅用于人工复核提示，不用于自动推送）：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `maxInversions` | 1 | 高点序列允许的最大逆序对数 |
| `maxDropCV` | 0.8 | 相邻高点跌幅变异系数上限 |
| `maxGapCV` | 0.6 | 高点时间间隔变异系数上限 |
| `altLeft` / `altRight` | 4 / 4 | 参数敏感性自检的备用 ZigZag 参数 |
| `minHighs` | 5 | 参与曲率评估的最少高点数量 |
| `confidence` | LOW | 固定为低置信度，**永不自动推送** |

---

### 形态 16：圆弧底 (Rounding Bottom)

| 项目 | 内容 |
|------|------|
| 识别要点 | 圆弧顶的镜像：多个摆动低点构成向上凹的平滑弧线，价格在低位缓慢转向 |
| 方向 | **看涨** |
| 失效条件 | 弧线上出现明显深谷（演变为头肩底或双底）；或价格创出新低 |
| 可检测性 | **C — 不建议自动化** |
| 最小K线数 | 60（理论值，实际不稳定） |
| 关键阈值 | 无稳定阈值 |
| 样本频次 | 2 次 |

```
同上反转。样本中出现"圆底+三角复合"的情况（doge 2h 圆底+三角）
```

**复合形态的处理原则**：样本中出现"圆底 + 三角"的复合标注（DOGE 2h）。这类复合结构在自动识别中应**分解处理**：

| 处理策略 | 说明 |
|---------|------|
| **只识别可自动化部分** | 复合形态中，识别三角形部分（A 级）作为主信号，圆底部分降级为备注文本 |
| **不做复合形态匹配** | 复合形态的组合空间爆炸（17 × 17 × 排列方式），且样本量极低，投入产出比为负 |
| **信号合并去重** | 若同一区域同时命中多个形态，保留评分最高的一个，其余记为 `overlapped` |

```pseudo
function hintRoundingBottom(pivots, klines, cfg):
    # 圆弧顶提示逻辑的镜像，同样仅用于人工复核
    lows = pivots.filter(type=LOW).lastN(5)
    if len(lows) < 5: return null

    inversions = countInversions([-p for p in lows.price])   # 递增序列
    if inversions > cfg.maxInversions: return null            # 1

    rises = [lows[i+1].price - lows[i].price for i in range(len(lows)-1)]
    if std(rises) / mean(rises) > cfg.maxRiseCV: return null  # 0.8

    gaps = [lows[i+1].index - lows[i].index for i in range(len(lows)-1)]
    if std(gaps) / mean(gaps) > cfg.maxGapCV: return null     # 0.6

    # 圆弧底：成交量应先萎缩后在右侧放量（杯形量能）
    if not isVolumeUShape(klines, lows[0].index, lows[-1].index): return null

    altLows = findPivots(klines, left=cfg.altLeft, right=cfg.altRight).filter(LOW).lastN(5)
    if not similarShape(lows, altLows): return null

    return Hint(type=ROUNDING_BOTTOM, confidence=LOW, needsManualReview=true)

function resolveCompositePatterns(detectedPatterns):
    # 复合形态去重：同一时间窗口内保留评分最高的
    detectedPatterns.sortByDesc(score)
    kept = []
    for p in detectedPatterns:
        overlap = kept.any(k => overlapRatio(k, p) > cfg.maxOverlapRatio)   # 0.6
        if not overlap:
            kept.add(p)
        else:
            p.status = OVERLAPPED          # 记录但不推送
            log(p)
    return kept
```

**圆弧底提示参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `maxInversions` | 1 | 低点序列允许的最大逆序对数 |
| `maxRiseCV` | 0.8 | 相邻低点涨幅变异系数上限 |
| `maxGapCV` | 0.6 | 低点时间间隔变异系数上限 |
| `altLeft` / `altRight` | 4 / 4 | 参数敏感性自检备用参数 |
| `maxOverlapRatio` | 0.6 | 复合形态去重的时间重叠率阈值 |
| `confidence` | LOW | 固定低置信度，永不自动推送 |

---

### 形态 17：V 型反转 (V-Reversal)

| 项目 | 内容 |
|------|------|
| 识别要点 | 急跌后以几乎相同的速度快速回补，中间无整理平台，转折点尖锐 |
| 方向 | **双向**（由反转方向决定） |
| 失效条件 | 不适用——本形态无稳定结构，任何"失效条件"都是事后定义 |
| 可检测性 | **C — 强烈不建议自动化** |
| 最小K线数 | 无意义 |
| 关键阈值 | 无 |
| 样本频次 | 1 次 |

```
本质是一段急跌后快速回补，没有固定的几何结构
任何"V型识别"都是事后统计，无法给出前瞻性规则
强烈不建议自动化
```

**为什么不自动化（技术论证，这是本文档最明确的否决项）**：

| 问题 | 说明 |
|------|------|
| **无几何结构** | V 型的定义是"下跌后以同等速度上涨"，唯一的量化特征是**两段斜率接近、中间转折尖锐**。但"尖锐"在离散 K 线上无法定义——取决于 `left/right` 参数，同一段行情可以是 V 型（小参数，转折点被识别）或双底（大参数，转折点两侧各有一个摆动点）。 |
| **本质是事后统计** | 识别 V 型需要知道"之后快速回补了"。在转折点出现的**当下**，V 型与"单边下跌的中途"在几何上完全无法区分。任何规则都只能在事后成立，对交易无前瞻价值。 |
| **无法设定止损** | V 型没有颈线、没有边界、没有形态高度，因此无法定义止损位与目标位。即使"识别成功"，也无法生成可执行的交易信号。 |
| **样本量 1 次** | 单个样本无法支撑任何参数标定。 |

**替代方案**：V 型反转在实践中的可交易部分，本质上是**急跌后的动能回补**，应由以下已有模块覆盖，无需独立识别器：

| 覆盖方式 | 说明 |
|---------|------|
| 双底识别器（快参数） | 用 `left=3, right=3` 的 ZigZag 跑双底，V 型的转折点会被拆成两个相邻低点，形成"窄双底" |
| 超卖反弹模块 | RSI < 30 + 单根 K 线振幅 > 2×ATR + 成交量放大，可捕获 V 型的启动点 |
| 不做处理 | V 型是低概率事件（1/173），投入产出比为负 |

```pseudo
# 明确不实现。以下仅为说明为何无法实现：
function whyVReversalCannotBeAutomated():
    # 1) 转折点定义依赖参数：同一段行情在不同 ZigZag 参数下结构完全不同
    p1 = findPivots(klines, 5, 5)    # → 可能得到 (L, H, L) 双底
    p2 = findPivots(klines, 2, 2)    # → 可能得到 (L) 单谷，即 V 型
    #    两者互斥，无客观的"正确"答案

    # 2) 前瞻性缺失：在转折点 index t，无法区分以下两者
    #    a) V 型反转（之后快速上涨）
    #    b) 单边下跌的中途回调（之后继续下跌）
    #    两者在 t 时刻及之前的所有可观测数据上统计不可分

    # 3) 无止损/目标位定义：缺少颈线与形态高度，Measured Move 公式不适用
    #    target = breakout ± height × ratio 中，height 无定义

    return NOT_IMPLEMENTED

# 替代：用急跌反弹捕获 V 型的启动（属于动量模块，不属于形态模块）
function detectOversoldBounce(klines, atr, cfg):
    i = len(klines) - 1
    if rsi(klines, 14)[-1] > cfg.rsiMax: return null              # 30
    if klines[i].close < klines[i].open: return null              # 必须是收阳
    if (klines[i].high - klines[i].low) < cfg.minRangeATR * atr: return null   # 2.0
    if klines[i].volume < cfg.volRatio * meanVolume(klines, 20): return null   # 1.5
    return MomentumSignal(type=OVERSOLD_BOUNCE, entry=klines[i].close,
                          stop=klines[i].low - cfg.stopATR * atr,
                          note="覆盖 V 型启动，非形态识别")
```

---

## 1.4 信号强度打分模型 (0~100)

### 1.4.1 维度与权重

| 维度 | 权重 | 打分逻辑 |
|------|------|---------|
| 形态完整度 | 25% | 所有摆动点是否齐全、几何约束满足程度 |
| 突破幅度/ATR | 20% | 突破幅度 ÷ ATR，>1.5得满分，<0.5不得分 |
| 成交量确认 | 15% | 突破K线量 ÷ 20周期均量，>1.5得满分 |
| 多周期共振 | 15% | 更大级别周期是否同向（如4h看涨+1d也看涨） |
| 形态时长合理性 | 10% | 形态跨度是否在合理区间（不过短也不过长） |
| 与大趋势一致性 | 10% | 形态方向是否与更大级别趋势一致 |
| 距离关键位远近 | 5% | 当前价距目标位的距离比例 |

### 1.4.2 各维度打分细则

**① 形态完整度（25%）**

```pseudo
function scoreCompleteness(pattern):
    s = 0
    # a) 必需摆动点齐全（50 分）
    s += 50 * (pattern.foundPoints / pattern.requiredPoints)
    # b) 几何约束裕度：各约束的实际值与阈值之比的平均（30 分）
    #    例如 peakTolerance=0.03，实际两肩差 0.015 → 裕度 0.5
    margins = [constraintMargin(c) for c in pattern.constraints]
    s += 30 * mean(margins)
    # c) 趋势线质量：触点数达标 + 无穿透（20 分）
    s += 20 * lineQuality(pattern.lines)
    return clamp(s, 0, 100)
```

| 子项 | 满分 | 打分方式 |
|------|------|---------|
| 摆动点齐全度 | 50 | 实际点数 / 必需点数 × 50 |
| 几何约束裕度 | 30 | 各约束 `(阈值 − 实际偏差) / 阈值` 的均值 × 30 |
| 趋势线质量 | 20 | `触点数达标(10) + 穿透 ≤ 1(10)` |

**② 突破幅度/ATR（20%）**

```pseudo
function scoreBreakoutATR(breakoutRange, atr):
    ratio = breakoutRange / atr
    # <0.5 → 0 分；>1.5 → 100 分；中间线性插值
    return clamp((ratio - 0.5) / (1.5 - 0.5), 0, 1) * 100
```

| 突破幅度 / ATR | 得分 |
|---------------|------|
| < 0.5 | **0**（且直接否决，不进入后续评分） |
| 0.5 | 0 |
| 0.75 | 25 |
| 1.0 | 50 |
| 1.25 | 75 |
| ≥ 1.5 | 100 |

**③ 成交量确认（15%）**

```pseudo
function scoreVolume(breakoutVolume, meanVolume20, pattern):
    ratio = breakoutVolume / meanVolume20
    s = clamp((ratio - 0.8) / (1.5 - 0.8), 0, 1) * 100   # ≤0.8 得 0；≥1.5 得 100
    # 形态专属量能确认（如双底量能萎缩）额外加分，上限 100
    if pattern.volumeDryUp: s = min(100, s + 15)
    return s
```

| 量比（突破量 / 20周期均量） | 得分 |
|--------------------------|------|
| ≤ 0.8 | 0 |
| 1.0 | 29 |
| 1.2 | 57 |
| 1.5 | 100 |
| ≥ 2.0 | 100（可额外记录为"异常放量"，人工关注） |

**④ 多周期共振（15%）**

| 共振情况 | 得分 |
|---------|------|
| 更大级别周期（如 1h 信号的 4h）存在同向形态 | 100 |
| 更大级别周期无形态，但趋势方向同向 | 70 |
| 更大级别周期无形态，趋势中性 | 40 |
| 更大级别周期无形态，趋势反向 | 10 |
| 更大级别周期存在反向形态 | 0 |

```pseudo
function scoreMTF(signalTF, higherTF, direction):
    hPattern = detectOnTimeframe(higherTF)
    if hPattern != null:
        return 100 if hPattern.direction == direction else 0
    hTrend = evalTrend(higherTF, lookback=50)
    if hTrend.direction == direction:  return 70
    if hTrend.direction == NEUTRAL:    return 40
    return 10
```

**⑤ 形态时长合理性（10%）**

```pseudo
function scoreDuration(spanBars, optimalRange):
    lo, hi = optimalRange          # 各形态不同，见下表
    if lo <= spanBars <= hi: return 100
    if spanBars < lo:  return max(0, 100 * spanBars / lo)              # 过短线性衰减
    return max(0, 100 * (1 - (spanBars - hi) / hi))                    # 过长线性衰减
```

| 形态 | 最优跨度（根） | 说明 |
|------|-------------|------|
| 头肩顶/底 | 50 ~ 120 | 太短则肩部不清晰，太长则结构松散 |
| 双顶/底 | 30 ~ 90 | |
| 三重顶/底 | 60 ~ 150 | |
| 上升/下降/对称三角 | 35 ~ 90 | |
| 旗形 | **15 ~ 30** | 旗身越短越强，超过 30 根动量已衰竭 |
| 三角旗形 | **10 ~ 20** | 比旗形更短 |
| 楔形 | 35 ~ 80 | |
| 矩形 | 30 ~ 100 | |

**⑥ 与大趋势一致性（10%）**

```pseudo
function scoreTrendAlign(pattern, higherTrend):
    if pattern.isReversal:                     # 反转型：头肩、双顶底、三重顶底、楔形
        # 反转形态需要与前置趋势反向（形态方向 == 反转方向）
        return 100 if pattern.direction != higherTrend.direction else 30
    else:                                      # 持续型：三角形、旗形、三角旗形、矩形
        return 100 if pattern.direction == higherTrend.direction else 30
```

> **注意**：反转形态与持续形态的评分方向**相反**。头肩底（看涨）出现在下跌趋势中才是有效的反转信号，若出现在上涨趋势中则大概率是中继震荡，应扣分。

**⑦ 距离关键位远近（5%）**

```pseudo
function scoreDistance(entry, target1, nearbyLevels, atr):
    # a) 目标位空间占比（60%）：距目标越远，潜在收益越大，但达成概率下降
    room = abs(target1 - entry) / atr
    s1 = clamp(room / 5.0, 0, 1) * 100        # 5×ATR 视为满空间
    # b) 前方无阻力/支撑阻挡（40%）
    blockers = countLevelsBetween(entry, target1, nearbyLevels)
    s2 = max(0, 100 - blockers * 25)
    return 0.6 * s1 + 0.4 * s2
```

### 1.4.3 总分计算与分档

```pseudo
function scoreSignal(pattern, ctx):
    s = {}
    s.completeness = scoreCompleteness(pattern)                       # 25%
    s.breakoutATR  = scoreBreakoutATR(pattern.breakoutRange, ctx.atr) # 20%
    s.volume       = scoreVolume(ctx.breakoutVolume, ctx.volMA20, pattern)  # 15%
    s.mtf          = scoreMTF(ctx.signalTF, ctx.higherTF, pattern.direction) # 15%
    s.duration     = scoreDuration(pattern.spanBars, pattern.optimalRange)   # 10%
    s.trendAlign   = scoreTrendAlign(pattern, ctx.higherTrend)        # 10%
    s.distance     = scoreDistance(pattern.entry, pattern.target1,
                                   ctx.nearbyLevels, ctx.atr)         # 5%

    total = 0.25 * s.completeness
          + 0.20 * s.breakoutATR
          + 0.15 * s.volume
          + 0.15 * s.mtf
          + 0.10 * s.duration
          + 0.10 * s.trendAlign
          + 0.05 * s.distance

    # 硬否决项（veto）：命中任一，总分直接置 0
    if pattern.breakoutRange < 0.5 * ctx.atr:         total = 0   # 突破幅度不足
    if pattern.riskReward < 1.5:                      total = 0   # R:R 不足（见 1.5.4）
    if pattern.detectability == C:                    total = 0   # C 级形态永不推送
    if pattern.status == EXPIRED or OVERLAPPED:       total = 0

    return Score(total=clamp(total,0,100), breakdown=s)
```

**分档**：

| 总分区间 | 档位 | 处理方式 |
|---------|------|---------|
| **≥ 75** | 强信号 | **立即推送** |
| **60 ~ 75** | 中等信号 | **推送并标注"待进一步确认"** |
| **< 60** | 弱信号 | **仅记录不推送**（存入日志供回测） |

```pseudo
function routeSignal(scored):
    if scored.total >= 75:
        return PUSH(immediate=true,  label="强信号")
    if scored.total >= 60:
        return PUSH(immediate=true,  label="待进一步确认")
    return LOG_ONLY(reason="弱信号", storeForBacktest=true)
```

### 1.4.4 评分区间校准建议

初始上线时，权重与阈值来自经验值，必须用历史数据校准：

```pseudo
function calibrateThresholds(historicalSignals):
    # 1) 用初始权重跑全量历史，记录每个信号的 score 与实际结果（达成 target1 / 止损 / 超时）
    results = backtest(historicalSignals)
    # 2) 计算各分数段的历史胜率与期望收益
    for bucket in [0,60,65,70,75,80,85,90,100]:
        seg  = results.filter(score in [bucket, bucket+5))
        winRate = seg.filter(outcome == TARGET1).count / seg.count
        expectancy = mean(seg.pnl)
        log(bucket, winRate, expectancy, seg.count)
    # 3) 调整分档边界，使得：
    #    - 强信号档（≥75）历史胜率 ≥ 55%，期望收益 > 0
    #    - 中等档（60~75）期望收益 ≥ 0（至少不亏）
    #    - 弱信号档（<60）期望收益 < 0（证明过滤有效）
    # 4) 若强信号档胜率不足，优先调高以下维度权重：
    #    突破幅度/ATR（最能区分真假突破）、成交量确认
    # 5) 校准周期：每积累 200 个新信号重跑一次；参数变更需重新校准
```

**校准检查表**：

| 检查项 | 合格标准 | 不合格处理 |
|-------|---------|-----------|
| 强信号档样本量 | ≥ 50 | 样本不足，放宽阈值或延长回测区间 |
| 强信号档胜率 | ≥ 55% | 提高突破幅度 / 成交量权重 |
| 强信号档期望收益 | > 0 | 检查止损设置是否过宽 |
| 中等档期望收益 | ≥ 0 | 提高分档下限 |
| 弱信号档期望收益 | < 0 | **达标**（证明过滤有效）；若 ≥ 0 说明过滤过严，错失机会 |
| 各维度区分度 | 胜/负样本在某维度得分差异 > 15 分 | 无区分度的维度应降权或重新定义 |

---

## 1.5 入场/止盈/止损计算规则

### 1.5.1 入场价

```
突破入场 = 突破确认K线的收盘价
回调入场 = 回踩至颈线/边界附近时的价格（通常在突破价的 38.2%~50% 回撤区间）
```

| 入场方式 | 定义 | 优点 | 缺点 | 适用 |
|---------|------|------|------|------|
| **突破入场** | 突破确认 K 线的**收盘价** | 成交确定性高，不踏空 | 成本较高，遇假突破即亏损 | 强信号（≥75）、强突破（≥1.0×ATR） |
| **回调入场** | 价格回踩至颈线/边界时的限价单 | 成本更优，R:R 更好 | 可能不回踩而踏空 | 中等信号（60~75）、弱突破（0.5~1.0×ATR） |

```pseudo
function calcEntry(pattern, klines, atr, cfg):
    if cfg.entryMode == BREAKOUT:
        return pattern.breakout.price           # 突破确认K线收盘价

    if cfg.entryMode == PULLBACK:
        # 回撤区间：从突破点回撤 38.2% ~ 50%（相对突破幅度）
        breakRange = abs(pattern.breakout.price - pattern.necklinePrice)
        if pattern.direction == BULLISH:
            lo = pattern.breakout.price - breakRange * cfg.pullbackMax   # 0.50
            hi = pattern.breakout.price - breakRange * cfg.pullbackMin   # 0.382
            return LimitOrder(price = (lo + hi) / 2, validBars = cfg.pullbackValidBars)  # 10
        else:
            lo = pattern.breakout.price + breakRange * cfg.pullbackMin
            hi = pattern.breakout.price + breakRange * cfg.pullbackMax
            return LimitOrder(price = (lo + hi) / 2, validBars = cfg.pullbackValidBars)

    if cfg.entryMode == ADAPTIVE:
        # 强信号 + 强突破 → 突破入场；否则等回调
        if pattern.score >= 75 and pattern.breakoutRange >= 1.0 * atr:
            return MarketOrder(pattern.breakout.price)
        return calcEntry(pattern, klines, atr, cfg.with(PULLBACK))
```

**入场参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `entryMode` | `ADAPTIVE` | 入场模式：BREAKOUT / PULLBACK / ADAPTIVE |
| `pullbackMin` | 0.382 | 回调入场的最小回撤比例（斐波那契） |
| `pullbackMax` | 0.50 | 回调入场的最大回撤比例 |
| `pullbackValidBars` | 10 | 限价单有效期（根），超时未成交则撤销 |
| `adaptiveScoreThreshold` | 75 | ADAPTIVE 模式切换为突破入场的分数线 |
| `adaptiveBreakATR` | 1.0 | ADAPTIVE 模式切换为突破入场的突破幅度线 |

### 1.5.2 止损价

```
止损 = 颈线价 - 1.5×ATR(14)    （做多时，在颈线下方）
       或 颈线价 + 1.5×ATR(14)  （做空时，在颈线上方）
       或 形态边界外侧 + 0.5×ATR
```

三种止损方式的适用场景不同，选择依据是**形态是否有明确的结构失效点**：

| 止损方式 | 公式 | 适用形态 | 说明 |
|---------|------|---------|------|
| **颈线外止损** | 颈线 ∓ 1.5×ATR | 头肩、双顶底、三重顶底 | 跌回颈线内侧即证明形态失败 |
| **边界外止损** | 对侧边界 ∓ 1.5×ATR | 三角形、矩形、楔形 | 回到箱体内即证明突破失败 |
| **形态极值止损** | 形态极值 ∓ 0.5×ATR | 全部（最宽） | 最保守，只有在价格超出整个形态范围才算失败 |

```pseudo
function calcStop(pattern, atr, cfg):
    d = pattern.direction
    if pattern.hasNeckline:
        # 颈线外止损：颈线 + 缓冲
        buffer = cfg.neckStopATR * atr                 # 1.5
        return (pattern.necklinePrice - buffer) if d == BULLISH
               else (pattern.necklinePrice + buffer)

    if pattern.hasBoundaries:
        # 边界外止损：突破边界的对侧边界 + 缓冲
        opposite = pattern.lowerLine if d == BULLISH else pattern.upperLine
        buffer = cfg.boundaryStopATR * atr             # 1.5
        return (opposite.priceAt(pattern.breakout.index) - buffer) if d == BULLISH
               else (opposite.priceAt(pattern.breakout.index) + buffer)

    # 兜底：形态极值外 0.5×ATR
    extreme = pattern.lowestPrice if d == BULLISH else pattern.highestPrice
    buffer = cfg.extremeStopATR * atr                  # 0.5
    return (extreme - buffer) if d == BULLISH else (extreme + buffer)

function tightenStop(pattern, atr, cfg):
    # 止损距离上限保护：止损幅度不得超过形态高度的 60%，否则 R:R 必然不达标
    stopDist = abs(pattern.entry - pattern.stop)
    if stopDist > cfg.maxStopVsHeight * pattern.height:      # 0.60
        return pattern.entry - sign * cfg.maxStopVsHeight * pattern.height
    # 止损距离下限保护：不得小于 1.0×ATR，否则极易被随机波动扫损
    if stopDist < cfg.minStopATR * atr:                      # 1.0
        return pattern.entry - sign * cfg.minStopATR * atr
    return pattern.stop
```

**止损参数表**：

| 参数 | 默认值 | 含义 | 调整建议 |
|------|-------|------|---------|
| `neckStopATR` | **1.5** | 颈线外缓冲（× ATR） | 高波动品种 2.0；低波动 1.2 |
| `boundaryStopATR` | **1.5** | 边界外缓冲（× ATR） | 同上 |
| `extremeStopATR` | **0.5** | 形态极值外缓冲（× ATR） | 极值本身已是结构边界，缓冲可更小 |
| `minStopATR` | 1.0 | 止损距离下限 | 低于此值扫损率显著上升 |
| `maxStopVsHeight` | 0.60 | 止损距离 / 形态高度上限 | 超过则 R:R 无法达标 |

### 1.5.3 止盈价（两档）

```
第一目标 = 突破价 + 形态高度 × 1.0      （保守，1:1 R:R）
第二目标 = 突破价 + 形态高度 × 1.618    （标准，黄金分割）
或按 ATR 倍数：
第一目标 = 入场价 + 3×ATR
第二目标 = 入场价 + 5×ATR
```

两套目标位算法（形态高度法 vs ATR 倍数法）需要**取更保守者**，避免单一方法失真：

```pseudo
function calcTargets(pattern, atr, cfg):
    d   = pattern.direction
    sgn = +1 if d == BULLISH else -1
    e   = pattern.entry

    # 方法 A：形态高度投射
    a1 = e + sgn * pattern.height * cfg.ratio1        # 1.0
    a2 = e + sgn * pattern.height * cfg.ratio2        # 1.618

    # 方法 B：ATR 倍数
    b1 = e + sgn * cfg.atrTarget1 * atr               # 3.0
    b2 = e + sgn * cfg.atrTarget2 * atr               # 5.0

    # 取更保守者（距入场更近的目标），避免任一方法因形态畸形而给出离谱值
    t1 = (a1 if abs(a1-e) < abs(b1-e) else b1)
    t2 = (a2 if abs(a2-e) < abs(b2-e) else b2)

    # 保证 t2 > t1（同方向）
    if sgn * (t2 - t1) <= 0:
        t2 = e + sgn * max(abs(a2-e), abs(b2-e), abs(t1-e) * 1.618)

    # 分批止盈：第一目标平仓 50%，剩余移动止损
    return Targets(t1=t1, t1CloseRatio=cfg.t1CloseRatio,      # 0.5
                   t2=t2, t2CloseRatio=1.0,
                   trailingAfterT1=cfg.trailingAfterT1)       # true
```

**目标位参数表**：

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `ratio1` | 1.0 | 第一目标的形态高度投射倍数（保守） |
| `ratio2` | 1.618 | 第二目标的形态高度投射倍数（标准，黄金分割） |
| `atrTarget1` | 3.0 | 第一目标的 ATR 倍数 |
| `atrTarget2` | 5.0 | 第二目标的 ATR 倍数 |
| `t1CloseRatio` | 0.5 | 第一目标平仓比例（50%） |
| `trailingAfterT1` | true | 第一目标后剩余仓位启用移动止损 |
| `trailingATR` | 2.0 | 移动止损距离（× ATR） |

**目标位选择的自适应规则**：

| 场景 | 建议投射倍数 | 理由 |
|------|------------|------|
| 顺大趋势的持续形态（三角、旗形） | 1.618 | 动量延续性强 |
| 逆大趋势的反转形态（头肩、双顶底） | 1.0 | 反转本身阻力大 |
| 强信号（≥75）+ 多周期共振 | 2.0 | 高质量信号可博取更大空间 |
| 前方存在明显阻力/支撑位 | 取**更近者**（目标位 vs 关键位） | 不与目标位之外的结构对抗 |

### 1.5.4 风险回报比校验

```
如果 (止盈1 - 入场) / (入场 - 止损) < 1.5:
    不推送（风险回报比不足）
```

```pseudo
function validateRiskReward(pattern, cfg):
    reward = abs(pattern.target1 - pattern.entry)
    risk   = abs(pattern.entry  - pattern.stop)
    if risk <= 0: return REJECT(reason="止损距离为零")

    rr = reward / risk
    if rr < cfg.minRR:                       # 1.5
        return REJECT(reason="R:R 不足", value=rr, threshold=cfg.minRR)

    # 附加校验：第二目标位的 R:R 也不应过低（保证至少部分仓位能跑到好位置）
    rr2 = abs(pattern.target2 - pattern.entry) / risk
    if rr2 < cfg.minRR2:                     # 2.0
        pattern.target2 = pattern.entry + sign * risk * cfg.minRR2   # 强制上修

    return ACCEPT(rr=rr, rr2=rr2)
```

**R:R 阈值表**：

| 阈值 | 默认值 | 含义 |
|------|-------|------|
| `minRR` | **1.5** | 第一目标的最低风险回报比，低于此值不推送 |
| `minRR2` | 2.0 | 第二目标的最低风险回报比（可强制上修目标位） |
| `preferredRR` | 2.5 | 理想 R:R，达到此值可在评分中额外加分 |

**R:R 校验失败的处理流程**：

```pseudo
function onRRRejected(pattern, cfg):
    # 1) 尝试收紧止损（改用形态极值止损或减小 ATR 倍数）
    tighter = tightenStop(pattern, cfg.atr, cfg.with(neckStopATR=1.0))
    if validateRiskReward(pattern.with(stop=tighter), cfg) == ACCEPT:
        return pattern.with(stop=tighter, note="止损已收紧至 1.0×ATR")

    # 2) 尝试改用回调入场（降低入场成本，提升 R:R）
    pb = calcEntry(pattern, cfg.with(PULLBACK))
    if validateRiskReward(pattern.with(entry=pb.price), cfg) == ACCEPT:
        return pattern.with(entry=pb.price, entryMode=PULLBACK,
                            note="改用回调入场以满足 R:R")

    # 3) 两者都不行 → 记录并放弃
    log(pattern, reason="R:R 不足且无法优化")
    return LOG_ONLY
```

### 1.5.5 完整信号生成流程

```pseudo
function generateSignal(klines, cfg):
    # Step 1: 摆动点
    pivots = findPivots(klines, cfg.zigLeft, cfg.zigRight)
    if len(pivots) < 5: return null
    atr = calcATR_Wilder(klines, 14)

    # Step 2: 并行运行所有识别器
    candidates = []
    candidates += detectHSTop(pivots, klines, atr, cfg)
    candidates += detectHSBottom(pivots, klines, atr, cfg)
    candidates += detectDoubleTop(pivots, klines, atr, cfg)
    candidates += detectDoubleBottom(pivots, klines, atr, cfg)
    candidates += detectTripleTop(pivots, klines, atr, cfg)
    candidates += detectTripleBottom(pivots, klines, atr, cfg)
    candidates += detectAscendingTriangle(pivots, klines, atr, cfg)
    candidates += detectDescendingTriangle(pivots, klines, atr, cfg)
    candidates += detectSymmetricalTriangle(pivots, klines, atr, cfg)
    candidates += detectFlag(pivots, klines, atr, cfg)
    candidates += detectPennant(pivots, klines, atr, cfg)
    candidates += detectRisingWedge(pivots, klines, atr, cfg)
    candidates += detectFallingWedge(pivots, klines, atr, cfg)
    candidates += detectRectangle(pivots, klines, atr, cfg)
    # 注：圆弧顶/底、V 型不在此列（C 级，仅生成人工复核提示）

    # Step 3: 复合形态去重
    candidates = resolveCompositePatterns(candidates)

    signals = []
    for p in candidates:
        # Step 4: 止损/止盈
        p.stop    = calcStop(p, atr, cfg)
        p.targets = calcTargets(p, atr, cfg)

        # Step 5: R:R 校验
        rr = validateRiskReward(p, cfg)
        if rr == REJECT:
            fixed = onRRRejected(p, cfg)
            if fixed == LOG_ONLY: continue
            p = fixed

        # Step 6: 评分
        p.score = scoreSignal(p, buildContext(klines, atr))

        # Step 7: 路由
        action = routeSignal(p.score)
        if action == LOG_ONLY:
            log(p, "弱信号，仅记录")
            continue
        signals.add(p.with(action=action, label=action.label))

    return signals
```

---

## 附录：样本集统计分析

### A.1 数据来源

| 项目 | 内容 |
|------|------|
| 样本总量 | 360 张人工标注图 |
| 含有效形态标注 | **173 张** |
| 标注总次数 | **248 次**（单张图可含多个形态标注，故次数 > 图片数） |
| 平均每图标注 | 1.43 个形态 |
| 覆盖品种 | BTC、ETH、DOGE 等主流加密货币 |
| 覆盖周期 | 15m / 1h / 2h / 4h / 1d |

### A.2 形态频次分布

**从样本集统计结果（173张有标注）：**

- 头肩(未区分顶底) 50次 + 头肩底26次 + 头肩顶15次 = **91次 → 最高频**
- 双底/W底 37次 → **第二**
- 下降三角24次 + 对称三角24次 + 上升三角17次 + 三角未指明16次 = **81次 → 第三**
- 通道14次 + 楔形未指明9次 + 旗形7次 + 下降楔3次 + 上升楔2次 = **35次**
- 圆弧底2次 + V型1次 + 杯柄1次 = **4次**
- **矩形 0 次**

| 排名 | 形态族 | 细分 | 次数 | 族合计 | 占标注总数 | 可检测性 |
|-----|-------|------|------|-------|-----------|---------|
| 1 | **头肩** | 未区分顶底 | 50 | **91** | **36.7%** | A/B |
| | | 头肩底 | 26 | | | B |
| | | 头肩顶 | 15 | | | A |
| 2 | **双底/W底** | 双底 / W底（含 M顶别名） | 37 | **37** | **14.9%** | A |
| 3 | **三角形** | 下降三角 | 24 | **81** | **32.7%** | A |
| | | 对称三角 | 24 | | | A |
| | | 上升三角 | 17 | | | A |
| | | 三角（未指明） | 16 | | | A |
| 4 | **持续/通道族** | 通道 | 14 | **35** | **14.1%** | 见 A.3 |
| | | 楔形（未指明） | 9 | | | B |
| | | 旗形 | 7 | | | A |
| | | 下降楔 | 3 | | | B |
| | | 上升楔 | 2 | | | B |
| 5 | **弧形/急转族** | 圆弧底 | 2 | **4** | **1.6%** | C |
| | | V 型 | 1 | | | C |
| | | 杯柄 | 1 | | | 见 A.3 |
| 6 | **矩形** | 矩形 / 箱体 | **0** | **0** | **0%** | A（未出现） |

> 占比之和 > 100%，因为单张图可含多个形态标注，占比以 248 次标注总数为分母。

### A.3 未纳入 17 形态的标注项处理建议

| 标注项 | 次数 | 处理建议 |
|-------|------|---------|
| **通道（14 次）** | 14 | 通道（上升/下降/水平）是**持续形态**，其交易逻辑与矩形高度重合（边界突破）。建议：① 优先用矩形识别器捕获水平通道；② 倾斜通道的斜率超过矩形阈值（1%）后，应作为**趋势跟踪**而非形态交易处理，交由趋势模块，不计入形态清单。若后续需要，可单独增加"通道"模块（预计为 A 级）。 |
| **杯柄（1 次）** | 1 | 杯柄本质是"圆弧底 + 小型三角旗形"的复合结构。样本仅 1 次，且其组成部分（圆弧、三角旗）已分别在清单中。建议：不做独立识别器，通过复合形态去重机制（`resolveCompositePatterns`）间接覆盖。 |

### A.4 分布对工程决策的直接影响

| 观察 | 决策 |
|------|------|
| **头肩族占 36.7%，是第一大形态** | 头肩识别器的准确性决定系统整体表现的 1/3。必须投入最多资源做参数标定与鲁棒性测试；建议为其单独实现"未区分顶底"的通用检测（50 次标注未区分，说明人工标注时顶底常可互换识别）。 |
| **头肩底（26）多于头肩顶（15）** | 与加密市场长期偏多的样本偏差有关。头肩底的 B 级判定（前置下跌趋势）必须严格，否则在上涨中继中误报率会显著高于头肩顶。 |
| **双底/W底 37 次，单一形态第二** | 独立高频形态，应优先实现并重点标定。其量能萎缩特征（第二底量能低于第一底）是低成本的加分维度，务必实现。 |
| **三角形族 81 次（32.7%）** | 三种三角形合计频次接近头肩族。其中 16 次"未指明"，说明人工标注时三角形的子类边界模糊——工程上应注意：三种三角形的斜率阈值之间应**留有缓冲带**（如上升三角要求下边界 > +3%，对称三角要求 > +2%，两者之间 2%~3% 的区域可归入"未指明三角"或取评分高者），避免在阈值边界反复跳变。 |
| **下降三角（24）+ 对称三角（24）> 上升三角（17）** | 与样本期市场偏空/震荡有关，不代表普遍规律。参数应对称设计，不做方向性偏置。 |
| **楔形合计 14 次（未指明 9 + 下降 3 + 上升 2）** | 样本量低但非零，仍应实现（B 级）。楔形的方向判断（边界向上→看跌）是核心难点，必须依赖前置趋势验证，不可仅凭几何。 |
| **旗形 7 次** | A 级但频次低。旗形对"旗杆"的依赖使其误报率低，实现成本可控，建议实现。 |
| **弧形 + V型 + 杯柄仅 4 次（1.6%）** | **明确不自动化**。这 4 次全部为 C 级，投入产出比为负。仅在人工复核界面提供低置信度提示。 |
| **矩形 0 次** | 人工标注的盲区，而非市场不存在。因几何最规则、自动化成本最低，仍建议实现（P1），作为所有边界类形态的兜底识别器与三角形退化情形的补充。 |

### A.5 开发优先级建议

| 优先级 | 形态 | 理由 |
|-------|------|------|
| **P0** | 头肩顶、头肩底、双顶、双底、上升三角、下降三角、对称三角 | 覆盖 **81.3%** 的标注（91 + 37 + 81 − 16 未指明 ≈ 201/248，含未指明则 84.3%），是系统可用性的基础 |
| **P1** | 旗形、矩形、上升楔、下降楔 | 覆盖至 **90%**；矩形虽 0 次但作为兜底，楔形有 14 次实际样本 |
| **P2** | 三重顶、三重底、三角旗形 | 样本稀少，但结构明确，可在 P0/P1 稳定后补充 |
| **P3** | 圆弧顶、圆弧底、V 型反转 | **不实现自动化**，仅提供人工复核提示 |

> **P0 覆盖率计算**：头肩族 91 + 双底族 37 + 三角族 81 = 209 次，占 248 次的 **84.3%**。这意味着仅实现 7 个 P0 形态即可覆盖超过 84% 的真实标注场景。这是资源受限时最重要的决策依据。

### A.6 待补充事项

| 事项 | 说明 |
|------|------|
| 标注数据的时间戳对齐 | 当前频次统计未区分周期。建议后续按周期（15m/1h/4h）分别统计，因不同周期的最优 ZigZag 参数与形态跨度阈值不同。 |
| 形态达成率回测 | 本附录只统计**出现频次**，未统计**形态成功率**。频次高不代表可盈利，P0 形态上线后必须逐个回测其 target1 达成率。 |
| 未指明标注的二次校验 | 50 次"头肩未区分"与 16 次"三角未指明"需人工复核细分，可显著提升各子类的参数标定精度。 |
| 矩形 0 次的验证 | 建议用矩形识别器跑一遍历史数据，确认是"标注遗漏"还是"参数阈值过严导致确实识别不到"。 |

---

*文档版本：v1.0 | 参数默认值均为经验起点，上线前需按 1.4.4 节流程用历史数据校准。*


---

# 附录 B：ZigZag 参数实测标定

> 本节数据来自**真实 Binance API 拉取的行情**，不是理论推演。
> 标定时间：2026-08-30 | 样本：24h 成交额 Top8~Top12 标的

## B.1 为什么必须用密度而不是绝对数量判定健康度

最初的健康检查用绝对阈值（"摆动点 < 15 个 = 过度平滑"），结果日线周期被**系统性误判**：

| 周期 | K线数 | 摆动点 | 密度 | 绝对阈值判定 | 密度判定 | 哪个对 |
|------|-------|--------|------|-------------|---------|--------|
| 15m | 480 | 50 | 0.104 | healthy | healthy | 一致 |
| 1d | 60 | 14 | **0.233** | over_smoothed ❌ | sensitive | **密度判定对** |

日线 14 个摆动点的密度（0.233）其实**高于** 15m 的（0.104），根本不是"过度平滑"。
绝对阈值对短数组不成立，已改为密度判定。

**密度区间定义（实测标定）：**

| 密度 | 判定 | 含义 |
|------|------|------|
| < 0.04 | over_smoothed | 会漏掉中等级别形态 |
| 0.04 ~ 0.18 | **healthy** | 推荐工作区间 |
| 0.18 ~ 0.30 | sensitive | 噪声偏多但可用 |
| > 0.30 | broken | 参数失效 |

## B.2 各周期参数实测对比

| 周期 | 参数 | K线数 | 摆动点(中位数) | 密度 | 判定 |
|------|------|-------|---------------|------|------|
| 15m | (5,5) | 480 | 50 | 0.104 | healthy ✅ |
| 15m | (7,7) | 480 | 38 | 0.079 | healthy |
| 1h | (5,5) | 240 | 23 | 0.096 | healthy ✅ |
| 1h | (6,6) | 240 | 19 | 0.079 | healthy |
| 4h | (3,3) | 120 | 18 | 0.150 | healthy ✅ |
| 4h | (4,4) | 120 | 14 | 0.117 | healthy |
| 1d | (2,2) | 60 | 14 | 0.233 | sensitive ❌ |
| 1d | **(3,3)** | **120** | **18** | **0.150** | **healthy ✅** |
| 1d | (4,4) | 120 | 13 | 0.108 | healthy（但点数偏少）|

## B.3 日线周期的两个反直觉发现

**发现一：日线不能沿用"周期越大窗口越小"的直觉。**

最初按"大周期 K 线少 → 窗口要小"的逻辑给 1d 配了 `(2,2)`。实测发现密度高达 **0.233**，噪声形态过多。改成 `(3,3)` 后密度降到 0.150，进入健康区间。

**发现二：日线 60 根 K 线不够用。**

| 1d K线数 | 参数 | 摆动点 | 头肩(需5点) | 复杂形态(需8点) |
|---------|------|--------|------------|----------------|
| 60 | (3,3) | 7 | 勉强够 | **不够** ❌ |
| **120** | **(3,3)** | **18** | 够 | **够** ✅ |
| 180 | (3,3) | 27 | 够 | 够 |

日线形态（如头肩顶/底）通常需要 2~4 个月形成，60 根 K 线只覆盖 2 个月，
ZigZag 只能提取 7 个摆动点——头肩需要 5 个点，勉强够但没有任何冗余。
**已将 1d 的 K 线数从 60 上调到 120。**

## B.4 修正前后的效果对比

Top12 标的 × 4 周期 = 48 次扫描：

| 版本 | healthy | sensitive | over_smoothed |
|------|---------|-----------|---------------|
| 修正前（1d 用 60根+(2,2)） | 31 | 0 | 9 |
| **修正后（1d 用 120根+(3,3)）** | **46** | 2 | **0** |

## B.5 最终采用的参数

```yaml
zigzag:
  "15m": { left: 5, right: 5 }
  "1h":  { left: 5, right: 5 }
  "4h":  { left: 3, right: 3 }
  "1d":  { left: 3, right: 3 }   # 注意不是(2,2)

scan:
  kline_counts:
    "15m": 480
    "1h": 240
    "4h": 120
    "1d": 120      # 从 60 上调
```

## B.6 实测吞吐量

| 指标 | 实测值 |
|------|-------|
| 单次 klines 请求耗时 | ~0.73 秒 |
| 48 次请求（Top12×4周期） | 6.6 秒 |
| 折算单次请求 | ~0.14 秒 |
| **推算 1200 次（Top300×4周期）** | **~165 秒 + 批间暂停 ≈ 3.5 分钟** |
| 限流命中次数 | 0（主动降速生效） |
| 兜底源切换次数 | 0（Binance 全程可用） |

结论：**3.5 分钟完成全量扫描，远低于 Actions 30 分钟超时上限。**
当前实现为串行请求（未启用并发），若后续需要进一步提速，
可按 config.yaml 的 `throttling.concurrency` 启用并发——
但需注意 Binance 2400 weight/分钟的硬限制，并发提升空间有限。

---

*附录 B 为实测数据，标定于 2026-08-30，市场环境变化后建议重新标定。*
