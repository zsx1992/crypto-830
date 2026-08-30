# Crypto Pattern Scanner

零成本、GitHub Actions 定时运行的加密货币合约图表形态扫描与提醒系统。

- 扫描 24h 成交额前 300 的 USDT 本位永续合约
- 15m / 1h / 4h / 1d 四周期独立识别 + 跨周期共振确认
- 形态：双顶/双底、头肩顶/底、三类三角形、旗形、楔形
- 企业微信推送，含交易价位与计算依据 + 形态标注图
- 全部基于公开 API，**零成本、免鉴权**

---

## 快速开始

### 1. 本机试跑（不需要任何 Key）

```bash
pip install -r requirements.txt

# ① 测试数据源连通性
python main.py --ping

# ② 单标的调试（打印摆动点、形态、价位）
python main.py --symbol BTCUSDT --interval 4h

# ③ 干跑：跑完整流程但不发消息（强烈建议首次用这个）
python main.py --full --dry-run --top-n 20
```

干跑会把"将要推送的消息"完整打印出来，用来验证格式和内容是否符合预期。

### 2. 部署到 GitHub Actions

**步骤一**：Fork / Push 本仓库

**步骤二**：配置 Secrets
`Settings → Secrets and variables → Actions → New repository secret`

| Name | Value |
|------|-------|
| `WECOM_WEBHOOK` | 企业微信群机器人的 Webhook 地址 |

Webhook 获取：群设置 → 群机器人 → 添加机器人 → 复制 Webhook 地址

**步骤三**：触发

- 自动：每 15 分钟跑一次（cron 在 `scan.yml` 里改）
- 手动：Actions → Crypto Pattern Scanner → Run workflow（可指定 `top_n` 和 `dry_run`）

### 3. 调整参数

所有阈值集中在 `config.yaml`，改参数不用改代码。常用项：

| 想调什么 | 改哪个 |
|---------|-------|
| 信号太少 | 调大 `filter.freshness_bars`，或降低 `filter.min_strength` |
| 信号太吵 | 调小 `patterns.tolerance.peak_price`（0.08→0.05），或提高 `min_strength` |
| 扫描标的数 | `scan.top_n` |
| 扫描周期 | `scan.intervals` |
| 推送条数上限 | `filter.max_per_run` |

---

## 项目结构

```
config.yaml                  完整配置（所有阈值，改这个就够了）
main.py                      入口
requirements.txt
.github/workflows/scan.yml   Actions 工作流

src/
  market_data.py             Binance(主) + OKX(兜底) 行情获取
  indicators.py              ATR(14) Wilder / EMA趋向 / 量能均线
  zigzag.py                  ZigZag 摆动点检测
  detector.py                形态检测引擎（多尺度扫描 + 去重 + 过滤）
  crosstf.py                 多周期交叉确认 + 七维信号评分
  state_store.py             去重状态持久化
  notifier.py                企业微信推送（markdown + image）
  chart.py                   matplotlib 形态标注图渲染
  scanner.py                 扫描编排器（串起完整链路）
  patterns/
    base.py                  Pattern/Line 结构、趋势线拟合、突破确认、价位计算
    double.py                双顶 / 双底
    head_shoulders.py        头肩顶 / 头肩底
    triangle.py              上升 / 下降 / 对称三角形
    flag_wedge.py            旗形 / 上升楔形 / 下降楔形

docs/
  01-pattern-catalog.md      17 种形态的量化阈值、A/B/C 分级
  02-architecture.md         系统架构与模块划分
  03-engineering.md          数据结构、限流、过滤、去重
  04-notification.md         推送内容与图表渲染
  05-delivery.md             实施路线、回测方法、实测验证结果

tools/
  analyze_samples.py         解析标注图文件名 → 结构化样本
  backtest_samples.py        金标准回放验证
samples/labels.json          360 张标注图的解析结果
```

---

## 当前实测状态（2026-08-30）

用真实 Binance 数据跑通，40 标的 × 4 周期 = 160 次扫描：

| 阶段 | 数量 |
|------|------|
| 候选形态 | 389 |
| 已确认突破 | 92 |
| 通过全部过滤 | 9 |
| 实际推送 | 9 |
| 耗时 | ~26 秒 |

推算全量 300 标的 × 4 周期 ≈ 1200 次扫描，约 3.5 分钟完成（Actions 限制 30 分钟）。

### 关于识别准确率，必须知道

与人工标注的一致率（82 个标注样本回放）：

| 判定标准 | 命中率 |
|---------|-------|
| 严格命中（形态末端距截图 ≤25 根K线） | 4.9% |
| 宽松命中（窗口内任意位置） | 18.3% |
| 多尺度「任意类型」命中 | 65.9% |

即：系统在 **66% 的位置能感知到"这里有结构"**，但归类到与你相同的形态名只有 5~18%。

作为参照，图表形态识别是公认难题，**人工标注者之间的一致率本身也只有 50~70%**。
5% 确实偏低（有改进空间），但"规则算法 vs 人眼"存在系统性差异是正常现象。

详见 `docs/05-delivery.md` 附录 C。

---

## 已知限制

- ⚠️ **形态识别是滞后指标**。ZigZag 需 `right` 根 K 线确认摆动点，信号必然晚于行情
- ⚠️ GitHub Actions cron 不保证精确时间，高峰期可能延迟 1~15 分钟
- ⚠️ 本工具仅提供技术分析参考，**不构成投资建议**。加密货币波动剧烈，假突破常见
- 圆弧顶/底、V 型反转、杯柄未实现（C 级形态，主观性太强，不建议自动化）

---

## License

MIT
