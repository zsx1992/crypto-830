# -*- coding: utf-8 -*-
"""
加密货币合约图表形态扫描器 —— 入口

完整链路:
  取数 → 指标 → ZigZag → 形态识别 → 多周期交叉确认
       → 强度过滤 → 去重 → 图表渲染 → 企微推送 → 状态持久化

用法:
  # 连通性测试
  python main.py --ping

  # 单标的调试（不推送，只打印）
  python main.py --symbol BTCUSDT --interval 4h

  # 全量扫描 + 真实推送
  python main.py --full

  # 全量扫描但不真发消息（强烈建议首次用这个）
  python main.py --full --dry-run

  # 小规模验证（10 标的）
  python main.py --full --dry-run --top-n 10

  # 查看 Top N 标的
  python main.py --list-symbols --top-n 20
"""

import os
import sys
import argparse
import logging
from typing import Optional

# 让 src/ 下的模块可以互相 import
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

try:
    import yaml
except ImportError:
    raise SystemExit("需要 pyyaml: pip install pyyaml")

from market_data import MarketDataClient, Kline
from indicators import calc_indicators
from zigzag import (find_pivots, get_params_for_interval,
                    pivot_health_check, find_pending_pivot)
from detector import PatternEngine, PATTERN_NAMES
from scanner import Scanner
from chart import test_font_availability

logger = logging.getLogger(__name__)


DEFAULT_CONFIG = {
    "data_source": {
        "primary": "binance",
        "fallback": "okx",
        "timeout_seconds": 30,
        "retry_max": 3,
        "retry_backoff_base": 2.0,
    },
    "scan": {
        "top_n": 300,
        "min_volume_usdt": 5_000_000,
        "intervals": ["15m", "1h", "4h", "1d"],
        "kline_counts": {"15m": 600, "1h": 400, "4h": 300, "1d": 240},
    },
    "throttling": {
        "concurrency": 3,
        "request_interval_ms": 100,
        "batch_size": 50,
        "batch_pause_sec": 2,
    },
    "zigzag": {
        "15m": {"left": 5, "right": 5},
        "1h": {"left": 5, "right": 5},
        "2h": {"left": 4, "right": 4},
        "4h": {"left": 3, "right": 3},
        "1d": {"left": 3, "right": 3},
        "multiscale": [3, 5, 8, 12],
    },
    "logging": {"level": "INFO"},
}


def load_config(path: Optional[str] = None) -> dict:
    """加载 YAML 配置，失败时回退到内置默认值"""
    if not path:
        logger.info("未指定配置文件，使用内置默认配置")
        return DEFAULT_CONFIG
    if not os.path.exists(path):
        logger.warning(f"配置文件不存在: {path}，使用内置默认配置")
        return DEFAULT_CONFIG
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        logger.info(f"已加载配置: {path}")
        return cfg or DEFAULT_CONFIG
    except Exception as e:
        logger.error(f"配置文件解析失败: {e}，使用内置默认配置")
        return DEFAULT_CONFIG


def get_zigzag_params(cfg: dict, interval: str) -> tuple:
    zz = cfg.get("zigzag", {})
    if interval in zz:
        return zz[interval].get("left", 5), zz[interval].get("right", 5)
    return get_params_for_interval(interval)


# ============================================================
#  单标的调试
# ============================================================

def analyze_symbol(client: MarketDataClient, symbol: str, interval: str,
                   cfg: dict, verbose: bool = True,
                   use_multiscale: bool = True) -> Optional[dict]:
    """单标的单周期完整分析（不推送，只打印）"""
    kline_counts = cfg.get("scan", {}).get("kline_counts", {})
    limit = kline_counts.get(interval, 240)

    t0 = __import__("time").time()
    klines, source = client.get_klines(symbol, interval, limit)
    fetch_time = __import__("time").time() - t0

    if not klines:
        logger.error(f"{symbol} {interval}: 未获取到 K 线数据")
        return None

    indicators = calc_indicators(klines)
    left, right = get_zigzag_params(cfg, interval)
    pivots = find_pivots(klines, left=left, right=right)
    health = pivot_health_check(klines, pivots)

    engine = PatternEngine(cfg)
    if use_multiscale:
        all_patterns = engine.scan_multiscale(klines, symbol, interval)
    else:
        all_patterns = engine.scan(klines, pivots, indicators.atr_current,
                                   symbol, interval)

    confirmed = engine.filter_signals(
        all_patterns, max_age=engine.freshness_for(interval))
    pending = find_pending_pivot(klines, pivots[-1] if pivots else None, left=left)

    result = {
        "symbol": symbol,
        "interval": interval,
        "source": source,
        "klines_count": len(klines),
        "fetch_seconds": round(fetch_time, 2),
        "last_close": indicators.last_close,
        "atr": round(indicators.atr_current, 6),
        "atr_pct": round(indicators.atr_current / indicators.last_close * 100, 3)
                   if indicators.last_close else 0,
        "trend": indicators.trend,
        "volume_ma": round(indicators.volume_ma_current, 2),
        "pivots": pivots,
        "pivots_count": len(pivots),
        "health": health,
        "pending_pivot": pending,
        "zigzag_params": {"left": left, "right": right},
        "patterns": all_patterns,
        "confirmed": confirmed,
    }

    if verbose:
        _print_analysis(result)
    return result


def _print_analysis(r: dict):
    print()
    print("=" * 66)
    print(f"  {r['symbol']}  {r['interval']}   [数据源: {r['source']}]")
    print("=" * 66)
    print(f"  K线数量     : {r['klines_count']} 根 (耗时 {r['fetch_seconds']}s)")
    print(f"  最新收盘    : {r['last_close']:.4f}")
    print(f"  ATR(14)     : {r['atr']:.4f}  ({r['atr_pct']:.2f}% of price)")
    print(f"  趋势判定    : {r['trend']}")
    print(f"  成交量均线  : {r['volume_ma']:.2f}")
    print(f"  ZigZag参数  : left={r['zigzag_params']['left']}, "
          f"right={r['zigzag_params']['right']}")
    print(f"  摆动点数量  : {r['pivots_count']}   "
          f"[健康度: {r['health']['status']}]")
    print(f"  压缩比      : {r['health']['compression_ratio']}:1")

    if r['pivots']:
        print()
        print("  摆动点序列（最近 12 个）:")
        for p in r['pivots'][-12:]:
            t = "峰 H" if p.type.value == "high" else "谷 L"
            bar = "\u2588" * max(1, int(p.price / r['last_close'] * 20))
            print(f"    #{p.index:<4} {t}  {p.price:>12.4f}  {bar}")

    if r['pending_pivot']:
        p = r['pending_pivot']
        t = "峰" if p.type.value == "high" else "谷"
        print()
        print(f"  \u26a0 未确认的潜在摆动{t}: #{p.index} @ {p.price:.4f}")
        print(f"    (需再等 {r['zigzag_params']['right']} 根 K 线才能确认)")

    patterns = r.get('patterns', [])
    confirmed = r.get('confirmed', [])

    print()
    if not patterns:
        print("  形态识别    : 未检测到任何候选形态")
    else:
        print(f"  形态识别    : 检出 {len(patterns)} 个候选，"
              f"其中 {len(confirmed)} 个通过全部过滤")
        print()
        for p in patterns:
            name = PATTERN_NAMES.get(p.pattern_type, p.pattern_type)
            icon = "\U0001F4C8" if p.direction.value == "LONG" else "\U0001F4C9"
            mark = "\u2713已确认" if p.status.value == "CONFIRMED" else "\u2026候选"
            extra = " \u2717已失效" if p.status.value == "FAILED" else ""
            print(f"    {icon} {name:<10} {mark}{extra}  置信度={p.confidence:.2f}")
            print(f"       方向={p.direction.value}  形态高度={p.height:.4f}")

            if p.status.value == "CONFIRMED":
                print(f"       突破 @#{p.breakout_index} 价={p.breakout_price:.4f}  "
                      f"幅度={p.breakout_magnitude_atr:.2f}\u00d7ATR  "
                      f"距今={p.breakout_age}根")
                print(f"       量能比={p.volume_ratio:.2f}\u00d7  "
                      f"连确认={p.confirmed_candles}根")
                print(f"       入场={p.entry_price:.4f}  "
                      f"止损={p.stop_loss:.4f}  "
                      f"止盈1={p.take_profit_1:.4f}  "
                      f"止盈2={p.take_profit_2:.4f}")
                print(f"       风险回报比=1:{p.risk_reward:.2f}")
            elif p.status.value == "FAILED":
                print(f"       突破后价格回到边界错误一侧，已判定为假突破")
            else:
                reasons = []
                if p.volume_ratio > 0 and p.volume_ratio < 1.5:
                    reasons.append(f"量能不足({p.volume_ratio:.2f}\u00d7<1.5)")
                if p.breakout_index < 0:
                    reasons.append("未突破")
                elif p.confirmed_candles < 2:
                    reasons.append(f"确认不足({p.confirmed_candles}根<2)")
                if p.risk_reward > 0 and p.risk_reward < 1.5:
                    reasons.append(f"R:R不足(1:{p.risk_reward:.2f})")
                print(f"       未确认原因: {', '.join(reasons) if reasons else '突破前'}")
            print()

    if r['health']['status'] == "over_smoothed":
        print("  \u26a0 摆动点过少，考虑调小 left/right（大周期尤其注意）")
    elif r['health']['status'] == "broken":
        print("  \u26a0 摆动点过多，参数基本失效，考虑调大 left/right")


def list_symbols(client: MarketDataClient, cfg: dict, top_n: int):
    min_vol = cfg.get("scan", {}).get("min_volume_usdt", 10_000_000)
    symbols = client.get_top_symbols(top_n=top_n, min_volume_usdt=min_vol)
    if not symbols:
        print("未获取到标的列表")
        return
    print()
    print(f"  24h 成交额前 {len(symbols)} 的 USDT 本位永续合约:")
    print("  " + "-" * 56)
    for i, s in enumerate(symbols, 1):
        print(f"  {i:>3}. {s}")
    print()


# ============================================================
#  CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="加密货币合约图表形态扫描器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python main.py --ping                              测试数据源连通性
  python main.py --symbol BTCUSDT --interval 4h      单标的调试
  python main.py --full --dry-run --top-n 10         小规模试跑(不发消息)
  python main.py --full                              全量扫描并推送
        """)

    parser.add_argument("--config", default="config.yaml",
                        help="配置文件路径 (默认: config.yaml)")
    parser.add_argument("--symbol", help="单个标的调试，如 BTCUSDT")
    parser.add_argument("--interval", default="4h", help="周期 (默认: 4h)")
    parser.add_argument("--full", action="store_true", help="全量扫描模式")
    parser.add_argument("--list-symbols", action="store_true",
                        help="只列出 Top N 标的")
    parser.add_argument("--top-n", type=int, help="覆盖配置中的 top_n")
    parser.add_argument("--ping", action="store_true", help="测试数据源连通性")
    parser.add_argument("--dry-run", action="store_true",
                        help="跑完整流程但不发送消息（首次运行强烈建议加）")
    parser.add_argument("--no-multiscale", action="store_true",
                        help="关闭多尺度扫描（用于对比单尺度效果）")
    parser.add_argument("--check-fonts", action="store_true",
                        help="检查中文字体可用性（决定图上能否用中文标注）")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="DEBUG 级别日志")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S")

    cfg = load_config(args.config)

    ds = cfg.get("data_source", {})
    client = MarketDataClient(
        primary=ds.get("primary", "binance"),
        timeout=ds.get("timeout_seconds", 30),
        retry_max=ds.get("retry_max", 3),
        backoff_base=ds.get("retry_backoff_base", 2.0))

    if args.check_fonts:
        info = test_font_availability()
        print(f"\n中文字体检查: {info}\n")
        return 0

    if args.ping:
        print("\n测试数据源连通性...")
        for name, ok in client.ping().items():
            print(f"  {name:<10} {'✓ 可用' if ok else '✗ 不可用'}")
        print()
        return 0

    if args.list_symbols:
        list_symbols(client, cfg, args.top_n or 20)
        return 0

    if args.symbol:
        analyze_symbol(client, args.symbol, args.interval, cfg,
                       use_multiscale=not args.no_multiscale)
        return 0

    if args.full:
        import os
        webhook = os.environ.get("WECOM_WEBHOOK")
        if not webhook and not args.dry_run:
            print("\n[!] 未设置 WECOM_WEBHOOK 环境变量，且未指定 --dry-run")
            print("    首次运行建议: python main.py --full --dry-run --top-n 10\n")
            return 1

        scanner = Scanner(cfg, dry_run=args.dry_run, webhook_url=webhook)
        result = scanner.run(top_n=args.top_n)
        Scanner.print_report(result)
        return 0

    parser.print_help()
    print("\n提示: 先跑 --ping 验证网络，再用 --full --dry-run 试跑。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
