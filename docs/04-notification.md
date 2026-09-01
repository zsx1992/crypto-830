# 第四章 推送内容与图表渲染

## 4.1 企业微信机器人消息格式

### 4.1.1 Markdown 正文消息

每条信号推送包含两条消息：**先 markdown 文字，后 image 图片**。分开发送是为了确保文字可读（markdown 不支持图片内嵌）。

#### 消息模板

```pseudo
function buildWeComMessage(signal):
    directionEmoji = "📈" if signal.direction == LONG else "📉"
    strengthLabel = "强" if signal.finalScore >= 75 else "中"

    content = f"""{directionEmoji} **{signal.symbol} {PATTERN_NAMES[signal.patternType]}**
> 周期: `{signal.interval}` | 方向: **{signal.direction.name}** | 强度: **{signal.finalScore}/100** ({strengthLabel})
>
> 💰 当前价: `{signal.breakoutPrice:.4f}`
>
> 📌 **入场建议**:
> · 入场价: `{signal.entryPrice:.4f}`
> · 止损价: `{signal.stopLossPrice:.4f}` (距入场 `{abs(signal.stopLossPrice-signal.entryPrice)/signal.entryPrice*100:.1f}%`)
> · 止盈1: `{signal.takeProfit1:.4f}` (目标 `{(signal.takeProfit1-signal.entryPrice)/signal.entryPrice*100:.1f}%`)
> · 止盈2: `{signal.takeProfit2:.4f}` (目标 `{(signal.takeProfit2-signal.entryPrice)/signal.entryPrice*100:.1f}%`)
> · 风险回报比: `1:{signal.riskRewardRatio:.1f}`
>
> 📐 **计算依据**:
> · 颈线/边界: `{signal.neckline or signal.boundary:.4f}`
> · 形态高度: `{signal.patternHeight:.4f}`
> · ATR(14): `{signal.atr:.4f}`
> · 突破幅度: `{signal.breakoutMagnitude:.2f}×ATR`
> · 共振周期: `{", ".join(signal.resonantWith) or "无"}`
>
> ⏰ 检测时间: `{signal.detectedAt.strftime("%m-%d %H:%M")}` UTC
> ⚠️ 仅供参考，不构成投资建议"""

    return {
        "msgtype": "markdown",
        "markdown": {"content": content}
    }
```

#### 推送效果示例（企微中实际显示）

```
📉 BTCUSDT 头肩顶
> 周期: `4h` | 方向: **SHORT** | 强度: **82/100** (强)
>
> 💰 当前价: `64250.00`
>
> 📌 入场建议:
> · 入场价: `64180.00`
> · 止损价: `64850.00` (距入场 1.05%)
> · 止盈1: `62800.00` (目标 -2.16%)
> · 止盈2: `61800.00` (目标 -3.73%)
> · 风险回报比: 1:2.1
>
> 📐 计算依据:
> · 颈线: `64620.00`
> · 形态高度: 860.00
> · ATR(14): 520.00
> · 突破幅度: 0.83×ATR
> · 共振周期: 1h, 1d
>
> ⏰ 检测时间: 08-30 12:05 UTC
> ⚠️ 仅供参考，不构成投资建议
```

### 4.1.2 图片消息（base64 + md5）

```pseudo
function buildImagePayload(imageBytes):
    return {
        "msgtype": "image",
        "image": {
            "base64": base64encode(imageBytes).decode("ascii"),
            "md5": md5hex(imageBytes)
        }
    }
```

**企微硬限制：**

| 限制项 | 值 | 应对策略 |
|-------|-----|---------|
| 图片大小 | ≤ **2MB** (base64 前) | 渲染时控制尺寸，超 1.8MB 则降低 DPI |
| 格式 | 仅 **JPG / PNG** | 统一输出 PNG |
| 单机器人频率 | **≤ 20 条/分钟** | PushLimiter 限速（见第三章） |
| 文字长度 | markdown ≤ **4096 字节** | 上述模板约 600 字节，安全 |
| Webhook URL 泄露 | 即失效 | 存 GitHub Secrets，不出现在代码/日志中 |

---

## 4.2 入场 / 止盈 / 止损 计算依据

### 4.2.1 核心公式汇总

所有价格计算基于 `Pattern` 对象中的几何坐标和 ATR 值：

```
形态高度 H = |峰价 - 颈线价|          （头肩/双顶）
           = |上边界 - 下边界|         （三角形/矩形/旗形）

入场价 P_entry = 突破确认K线的收盘价

止损价 P_stop = 颈线价 + 1.5 × ATR      （做空时在颈线上方）
             或 边界外 + 0.5 × ATR     （三角形/楔形用边界替代颈线）

止盈1 P_tp1 = P_entry - H × tp1_ratio    （做空；tp1_ratio 默认 0.5）
止盈2 P_tp2 = P_entry - H × tp2_ratio    （做空；tp2_ratio 默认 1.618）

风险回报比 R:R = |P_tp1 - P_entry| / |P_entry - P_stop|
```

> **【2026-09-01 更新】止盈比例调优**（依据 docs/05-delivery.md D.6 回测）：
> `tp1_ratio` 由 1.0 调整为 **0.5**（config.yaml `patterns.trade_levels`）。
> 回测 49 标的×2100根 4h：1.0x → -0.09R（胜率30%），0.5x → **+0.56R**（胜率48.5%）。
> 1× 形态高度的目标太远，经常先回踩打止损；0.5x 更早落袋。
> 止损仍为颈线外 1.5×ATR。R:R 口径随 tp1 减半，`filter.min_rr` 同步 1.5→0.75（等效原口径）。

### 4.2.2 各形态的参数化计算伪代码

```pseudo
function calcTradeLevels(pattern, klines, atr):
    H = pattern.height                    # 形态高度（已在识别阶段计算）
    entry = pattern.breakoutPrice          # 突破确认收盘价

    # --- 止损：三选一取最保守（离入场最近的有效位）---
    stopCandidates = []

    if pattern.neckline is not null:
        if pattern.direction == SHORT:
            stopCandidates.append(pattern.neckline.price + 1.5 * atr)
        else:
            stopCandidates.append(pattern.neckline.price - 1.5 * atr)

    if pattern.lowerBoundary is not null:   # 三角形/楔形/矩形
        if pattern.direction == SHORT:
            stopCandidates.append(pattern.lowerBoundary.valueAt(breakoutIndex) + 0.5 * atr)
        else:
            stopCandidates.append(pattern.upperBoundary.valueAt(breakoutIndex) - 0.5 * atr)

    # ATR 固定倍数兜底
    if pattern.direction == SHORT:
        stopCandidates.append(entry + 2.0 * atr)
    else:
        stopCandidates.append(entry - 2.0 * atr)

    # 取最近的（最小亏损）有效止损
    if pattern.direction == SHORT:
        stop = min(s for s in stopCandidates if s > entry)    # 做空止损在上方
    else:
        stop = max(s for s in stopCandidates if s < entry)    # 做多止损在下方

    # --- 止盈 ---
    if pattern.direction == SHORT:
        tp1 = entry - H * 1.0
        tp2 = entry - H * 1.618
    else:
        tp1 = entry + H * 1.0
        tp2 = entry + H * 1.618

    # 确保 TP > Entry（做多）或 TP < Entry（做空），且 TP 不越过 0
    tp1 = clamp(tp1, min=0.0001)           # 价格不能为负或零
    tp2 = clamp(tp2, min=0.0001)

    rr = abs(tp1 - entry) / abs(entry - stop)

    return {
        "entryPrice": entry,
        "stopLossPrice": stop,
        "takeProfit1": tp1,
        "takeProfit2": tp2,
        "riskRewardRatio": rr,
        "patternHeight": H,
        "atr": atr
    }
```

### 4.2.3 各形态的特殊处理

| 形态 | 颈线/边界来源 | 止损特殊规则 |
|------|-------------|------------|
| 头肩顶/底 | 两谷连线（允许 ±3% 斜率） | 首选颈线外 1.5×ATR |
| 双顶/双底 | 中间谷/峰的价格水平线 | 同上 |
| 三角形 | 对侧边界（非突破方向的那条） | 用边界代替颈线 |
| 旗形 | 旗身边界（与旗杆反向那条） | 旗身外 1×ATR（旗形止损较紧） |
| 楔形 | 与预期方向相反的边界 | 边界外 1.5×ATR |
| 矩形 | 对侧水平边界 | 边界外 1×ATR |

---

## 4.3 图表渲染（matplotlib Agg 后端）

### 4.3.1 为什么选 matplotlib

| 方案 | 免费 | 无需鉴权 | Actions 可用 | 标注能力 | 结论 |
|------|-----|---------|------------|---------|------|
| matplotlib + Agg | ✅ | ✅ | ✅ | 强（任意几何标注） | **选用** |
| Plotly (static PNG) | ✅ | ✅ | ✅ | 中 | 备选 |
| TradingView widget | ❌ 需嵌入 | ✅ | ✅ | 最强但复杂 | 过度工程 |
| 在线 API (QuickChart等) | ✅ | ✅ | ✅ | 弱 | 依赖外部服务 |

**关键约束：GitHub Actions runner 上没有 GUI 显示设备。** 必须使用 `Agg` 后端：

```python
import matplotlib
matplotlib.use("Agg")       # 必须在 import pyplot 之前
import matplotlib.pyplot as plt
```

### 4.3.2 图表渲染完整流程

```pseudo
function renderChart(klines, pattern):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    import matplotlib.dates as mdates

    fig, (ax_main, ax_vol) = plt.subplots(
        2, 1, figsize=(12, 7),
        gridspec_kw={"height_ratios": [4, 1]},
        sharex=True
    )
    fig.patch.set_facecolor("white")

    # === 主图：K线 ===
    tailKlines = klines[-pattern.config.chartCandles:]   # 默认最后 120 根
    candlestick(ax_main, tailKlines)                     # 自绘 OHLC（见下方）

    # === 标注几何结构 ===
    if pattern.neckline is not null:
        x_range = [tailKlines[0].openTime, tailKlines[-1].closeTime]
        ax_main.axhline(y=pattern.neckline.price,
                        color="red", linestyle="--", linewidth=1.2,
                        alpha=0.8, label=f"Neckline ({pattern.neckline.price:.2f})")

    if pattern.upperBoundary is not null:
        plotTrendline(ax_main, pattern.upperBoundary, color="blue",
                      label="Upper Boundary")

    if pattern.lowerBoundary is not null:
        plotTrendline(ax_main, pattern.lowerBoundary, color="green",
                      label="Lower Boundary")

    # === 标注突破点 ===
    bo = pattern.breakoutIndex - (len(klines) - len(tailKlines))
    if 0 <= bo < len(tailKlines):
        k = tailKlines[bo]
        marker = "v" if pattern.direction == SHORT else "^"
        color = "red" if pattern.direction == SHORT else "green"
        ax_main.scatter(mdates.num2date(k.openTime / 1000), k.close,
                       marker=marker, s=150, color=color, zorder=5,
                       label=f"Breakout ({k.close:.2f})")

    # === 标注目标位 ===
    if pattern.takeProfit1 is not null:
        ax_main.axhline(y=pattern.takeProfit1, color="purple",
                        linestyle=":", linewidth=1, alpha=0.6,
                        label=f"TP1 ({pattern.takeProfit1:.2f})")
    if pattern.takeProfit2 is not null:
        ax_main.axhline(y=pattern.takeProfit2, color="orange",
                        linestyle=":", linewidth=1, alpha=0.4,
                        label=f"TP2 ({pattern.takeProfit2:.2f})")
    if pattern.stopLossPrice is not null:
        ax_main.axhline(y=pattern.stopLossPrice, color="#CC0000",
                        linestyle="-.", linewidth=1, alpha=0.5,
                        label=f"Stop ({pattern.stopLossPrice:.2f})")

    # === 标注摆动点（小圆点）===
    for p in pattern.pivotPoints:
        relIdx = p.index - (len(klines) - len(tailKlines))
        if 0 <= relIdx < len(tailKlines):
            t = tailKlines[relIdx]
            color = "#E24B4A" if p.type == HIGH else "#3B6D11"
            ax_main.scatter(mdates.num2date(t.openTime / 1000), p.price,
                           marker="o" if p.type == HIGH else "o",
                           s=30, color=color, alpha=0.6, zorder=4)

    ax_main.legend(loc="upper left", fontsize=8)
    ax_main.set_title(f"{pattern.symbol} {pattern.interval} "
                      f"{PATTERN_NAMES[pattern.patternType]}",
                      fontsize=12, fontweight=500)
    ax_main.grid(True, alpha=0.2)

    # === 成交量子图 ===
    times = [k.openTime for k in tailKlines]
    volumes = [k.volume for k in tailKlines]
    colors = ["#E24B4A" if k.close >= k.open else "#3B6D11"
              for k in tailKlines]
    ax_vol.bar(times, volumes, color=colors, width=0.8, alpha=0.5)
    ax_vol.set_ylabel("Vol", fontsize=9)

    # === 输出为 PNG bytes ===
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120,
                bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)

    img_bytes = buf.getvalue()

    # === 大小检查与压缩 ===
    if len(img_bytes) > 1.8 * 1024 * 1024:        # 超过 1.8MB 则降质
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=80,
                    bbox_inches="tight", optimize=True)
        img_bytes = buf.getvalue()

    return img_bytes
```

### 4.3.3 K 线绘制（无 mpl_finance 依赖）

```python
def candlestick(ax, klines, width=0.6, up_color="#E24B4A", down_color="#3B6D11"):
    """纯手绘 K 线，不依赖已废弃的 mpl_finance"""
    import matplotlib.dates as mdates

    times = [k.openTime / 1000 for k in klines]   # 转秒级 timestamp
    for i, k in enumerate(klines):
        color = up_color if k.close >= k.open else down_color
        # 实体
        body_bottom = min(k.open, k.close)
        body_top = max(k.open, k.close)
        body_height = body_top - body_bottom if body_top != body_bottom else 0.001
        ax.bar(times[i], body_height, bottom=body_bottom,
               width=width, color=color, edgecolor=color, linewidth=0.3)
        # 影线
        ax.vlines(times[i], ymin=k.low, ymax=k.high,
                 color=color, linewidth=0.5, alpha=0.7)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
```

### 4.3.4 趋势线绘制辅助函数

```python
def plotTrendline(ax, line, color="blue", **kwargs):
    """根据 Line 对象的两个端点绘制趋势线"""
    import matplotlib.dates as mdates

    t1 = mdates.num2date(line.p1.timestamp / 1000)
    t2 = mdates.num2date(line.p2.timestamp / 1000)
    ax.plot([t1, t2], [line.p1.price, line.p2.price],
            color=color, linewidth=1.2, alpha=0.75,
            linestyle="-", **kwargs)
```

### 4.3.5 字体问题

GitHub Actions runner 上通常**没有中文字体**。解决方案：

1. **图表上只用英文/数字标签**（Neckline, Breakout, TP1 等）——如上代码所示
2. 中文全部放在企微 markdown 正文中——不受字体影响
3. 如果未来需要中文标注图：
   ```bash
   # Ubuntu 上安装中文字体
   sudo apt-get install -y fonts-wqy-microhei
   # 或下载 Noto Sans CJK 并放入 ~/.fonts/
   ```
   但这增加构建时间和依赖，当前方案不需要。

### 4.3.6 图片大小控制

| 参数 | 值 | 说明 |
|------|---|------|
| figsize | (12, 7) | 1200×700 像素逻辑尺寸 |
| dpi | 120 | 输出 1440×840 物理像素 |
| 预估文件大小 | ~200~500 KB | 典型单图（远低于 2MB 限制） |
| 压缩触发阈值 | 1.8 MB | 超过则降 dpi 到 80 |
| 最大宽度 | 900 px | config.yaml 可配 |

---

## 4.4 推送模块完整接口

```pseudo
class WeComNotifier:
    def __init__(self, webhook_url, limiter=None):
        self.webhook_url = webhook_url
        self.limiter = limiter or PushLimiter(maxPerMinute=18)

    def push(self, signal, chartImageBytes=null):
        # 1. 发送 markdown 文字消息
        msg = buildWeComMessage(signal)
        resp = self._post(msg)
        if resp.errcode != 0:
            log("Markdown push failed", resp)
            # 不抛异常，继续尝试图片

        sleep(1)   # 间隔，防企微限流

        # 2. 发送图片消息
        if chartImageBytes is not null:
            self.limiter.acquire()    # 频率保护
            imgPayload = buildImagePayload(chartImageBytes)
            resp = self._post(imgPayload)
            if resp.errcode != 0:
                log("Image push failed", resp)

    def _post(self, payload):
        resp = requests.post(self.webhook_url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            log("Push OK", payload["msgtype"])
        elif data.get("errcode") == 45009:
            log("Image too large (>2MB)")
        return data
```
