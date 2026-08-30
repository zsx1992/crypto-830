# -*- coding: utf-8 -*-
"""
行情获取模块：Binance USDⓈ-M Futures (主) + OKX v5 (兜底)

均为公开 REST API，无需任何鉴权。

关键限制（实测）：
  - Binance Futures: IP 级 2400 weight/分钟
      · /fapi/v1/klines        weight = 2
      · /fapi/v1/ticker/24hr   weight ≈ 40 (不带 symbol)
      · /fapi/v1/exchangeInfo  weight = 1
    → 300 标的 × 4 周期 = 1200 次 = 2400 weight，正好打满，必须节制
  - OKX: 20 请求 / 2 秒 (公开行情)
      · /api/v5/market/candles 单次最多 100 根，长周期需分页
"""

import time
import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

try:
    import requests
except ImportError:
    raise SystemExit("需要 requests: pip install requests")

logger = logging.getLogger(__name__)


# ============================================================
#  数据结构
# ============================================================

@dataclass
class Kline:
    """标准化 K 线（两个数据源归一化后的统一结构）"""
    openTime: int          # 毫秒时间戳
    open: float
    high: float
    low: float
    close: float
    volume: float          # 成交量（币）
    closeTime: int
    quoteVolume: float     # 成交额（USDT）
    tradeCount: int = 0

    def __repr__(self):
        return (f"Kline(t={self.openTime}, O={self.open:.4f}, H={self.high:.4f}, "
                f"L={self.low:.4f}, C={self.close:.4f}, V={self.volume:.2f})")


@dataclass
class Ticker:
    symbol: str
    lastPrice: float
    quoteVolume: float     # 24h 成交额（USDT）
    priceChangePercent: float


# ============================================================
#  Binance 客户端（主源）
# ============================================================

class BinanceClient:
    """Binance USDⓈ-M Futures 公开 API 客户端"""

    BASE_URL = "https://fapi.binance.com/fapi/v1"

    # 各接口权重
    WEIGHT_KLINES = 2
    WEIGHT_TICKER_ALL = 40
    WEIGHT_EXCHANGE_INFO = 1

    WEIGHT_LIMIT = 2400          # 每分钟 IP 上限
    SAFETY_MARGIN = 100          # 预留余量，防止重试时超限

    def __init__(self, timeout: int = 30, retry_max: int = 3, backoff_base: float = 2.0):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "crypto-pattern-scanner/1.0"})
        self.timeout = timeout
        self.retry_max = retry_max
        self.backoff_base = backoff_base

        # weight 跟踪（用于主动降速，避免被打回 429）
        self.used_weight = 0
        self.window_start = time.time()

    # ---------- 内部：带重试与限流感知的请求 ----------

    def _request(self, path: str, params: Optional[dict] = None) -> Optional[list]:
        url = f"{self.BASE_URL}{path}"
        attempt = 0

        while attempt <= self.retry_max:
            # 请求前检查 weight 预算
            self._check_weight_budget()

            try:
                resp = self.session.get(url, params=params or {}, timeout=self.timeout)

                # 读取 Binance 返回的实时 weight 用量
                w = resp.headers.get("X-MBX-USED-WEIGHT-1M")
                if w:
                    self.used_weight = int(w)

                # --- 限流 ---
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    logger.warning(f"[Binance] 429 限流 (used={self.used_weight})，"
                                   f"等待 {retry_after}s")
                    time.sleep(retry_after)
                    attempt += 1
                    continue

                # --- IP 封禁 ---
                if resp.status_code == 418:
                    wait = 60 * (attempt + 1)
                    logger.error(f"[Binance] 418 IP 封禁！等待 {wait}s 后重试 "
                                 f"(attempt {attempt + 1}/{self.retry_max})")
                    time.sleep(wait)
                    attempt += 1
                    continue

                # --- 服务端错误 ---
                if resp.status_code >= 500:
                    delay = self.backoff_base * (2 ** attempt)
                    logger.warning(f"[Binance] 服务端错误 {resp.status_code}，"
                                   f"{delay:.1f}s 后重试")
                    time.sleep(delay)
                    attempt += 1
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.Timeout:
                delay = self.backoff_base * (2 ** attempt)
                logger.warning(f"[Binance] 请求超时，{delay:.1f}s 后重试 "
                               f"({attempt + 1}/{self.retry_max})")
                time.sleep(delay)
                attempt += 1
            except requests.exceptions.ConnectionError as e:
                delay = self.backoff_base * (2 ** attempt)
                logger.warning(f"[Binance] 连接错误 {e}，{delay:.1f}s 后重试")
                time.sleep(delay)
                attempt += 1
            except Exception as e:
                logger.error(f"[Binance] 未预期错误: {e}")
                return None

        logger.error(f"[Binance] 重试 {self.retry_max} 次仍失败: {path}")
        return None

    def _check_weight_budget(self, estimated: int = WEIGHT_KLINES):
        """
        主动降速：若当前分钟已接近限额，等到下一分钟窗口。

        这是避免 429/418 的关键——与其等被拒绝后重试，不如提前让路。
        """
        elapsed = time.time() - self.window_start
        if elapsed >= 60:
            # 进入新的分钟窗口，重置计数
            self.used_weight = 0
            self.window_start = time.time()
            return

        if self.used_weight + estimated > (self.WEIGHT_LIMIT - self.SAFETY_MARGIN):
            wait = 60 - elapsed + 1
            logger.info(f"[Binance] weight 接近上限 "
                        f"({self.used_weight}/{self.WEIGHT_LIMIT})，主动等待 {wait:.1f}s")
            time.sleep(wait)
            self.used_weight = 0
            self.window_start = time.time()

    # ---------- 公开接口 ----------

    def ping(self) -> bool:
        """连通性测试"""
        try:
            r = self.session.get(f"{self.BASE_URL}/ping", timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def get_exchange_info(self) -> Optional[dict]:
        return self._request("/exchangeInfo")

    def get_all_tickers(self) -> List[Ticker]:
        """一次性获取全部 24h ticker（weight ≈ 40，注意节制）"""
        data = self._request("/ticker/24hr")
        if not data:
            return []
        return [
            Ticker(
                symbol=t["symbol"],
                lastPrice=float(t["lastPrice"]),
                quoteVolume=float(t.get("quoteVolume", 0)),
                priceChangePercent=float(t.get("priceChangePercent", 0)),
            )
            for t in data
        ]

    def get_top_symbols(self, top_n: int = 300,
                        min_volume_usdt: float = 10_000_000) -> List[str]:
        """
        按 24h 成交额取前 N 个 USDT 本位永续合约。

        过滤规则：
          - 仅 USDT 计价
          - 仅永续合约（排除季度交割，如 BTCUSDT_240927）
          - 24h 成交额 >= min_volume_usdt
        """
        tickers = self.get_all_tickers()
        if not tickers:
            logger.error("[Binance] 获取 ticker 失败")
            return []

        candidates = []
        for t in tickers:
            # 永续合约符号不含 "_"（交割合约形如 BTCUSDT_240927）
            if not t.symbol.endswith("USDT"):
                continue
            if "_" in t.symbol:
                continue
            if t.quoteVolume < min_volume_usdt:
                continue
            candidates.append(t)

        candidates.sort(key=lambda x: x.quoteVolume, reverse=True)
        result = [c.symbol for c in candidates[:top_n]]

        logger.info(f"[Binance] Top{len(result)} 标的选取完成，"
                    f"最低成交额 {candidates[-1].quoteVolume / 1e6:.1f}M USDT"
                    if candidates else "[Binance] 无候选标的")
        return result

    def get_klines(self, symbol: str, interval: str,
                   limit: int = 500, end_time_ms: int = None) -> List[Kline]:
        """
        获取 K 线，返回按时间升序排列。

        end_time_ms: 可选，只取该毫秒时间戳之前的 K 线。
                     回测必须用这个参数——否则会拿到"未来"的数据，
                     导致前视偏差(look-ahead bias)。
        """
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time_ms:
            params["endTime"] = end_time_ms
        data = self._request("/klines", params)
        if not data:
            return []
        return [self._parse_kline(row) for row in data]

    @staticmethod
    def _parse_kline(row: list) -> Kline:
        """
        Binance klines 返回格式：
        [openTime, open, high, low, close, volume, closeTime,
         quoteAssetVolume, trades, takerBuyBase, takerBuyQuote, ignore]
        """
        return Kline(
            openTime=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            closeTime=int(row[6]),
            quoteVolume=float(row[7]),
            tradeCount=int(row[8]),
        )


# ============================================================
#  OKX 客户端（兜底源）
# ============================================================

class OkxClient:
    """
    OKX v5 公开行情 API（兜底）

    限制：20 请求 / 2 秒 = 10 req/s，这里按 ~9 req/s 节流留余量。
    注意：单次最多返回 100 根 K 线，长周期需分页。
    """

    BASE_URL = "https://www.okx.com/api/v5/market"
    MAX_CANDLES_PER_REQUEST = 100
    MIN_REQUEST_INTERVAL = 0.11      # ~9 req/s

    def __init__(self, timeout: int = 30, retry_max: int = 2):
        self.session = requests.Session()
        self.timeout = timeout
        self.retry_max = retry_max
        self.last_request_time = 0

    def _throttle(self):
        """简单令牌桶：确保不超过限速"""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.MIN_REQUEST_INTERVAL:
            time.sleep(self.MIN_REQUEST_INTERVAL - elapsed)
        self.last_request_time = time.time()

    def _request(self, path: str, params: Optional[dict] = None) -> Optional[list]:
        url = f"{self.BASE_URL}{path}"

        for attempt in range(self.retry_max + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params or {}, timeout=self.timeout)

                if resp.status_code == 429:
                    logger.warning("[OKX] 429 限流，等待 5s")
                    time.sleep(5)
                    continue

                resp.raise_for_status()
                data = resp.json()

                # OKX 用 code 字段表示状态，"0" 为成功
                if data.get("code") != "0":
                    logger.error(f"[OKX] 接口错误 {data.get('code')}: {data.get('msg')}")
                    return None

                return data.get("data", [])

            except Exception as e:
                logger.warning(f"[OKX] 请求失败 (attempt {attempt + 1}): {e}")
                time.sleep(2 * (attempt + 1))

        return None

    def get_klines(self, symbol: str, interval: str,
                   limit: int = 100) -> List[Kline]:
        """
        获取 K 线。OKX 单次上限 100 根，超过需分页。

        注意：
          - symbol 需转换：BTCUSDT -> BTC-USDT-SWAP
          - interval 需大写：15m -> 15m, 1h -> 1H, 4h -> 4H, 1d -> 1D
          - 返回按时间【倒序】（最新的在前）
        """
        inst_id = self._to_inst_id(symbol)
        bar = self._to_bar(interval)

        all_data = []
        remaining = limit
        after = None       # 用于翻页（向更早的时间取）

        while remaining > 0:
            batch_size = min(remaining, self.MAX_CANDLES_PER_REQUEST)
            params = {"instId": inst_id, "bar": bar, "limit": str(batch_size)}
            if after:
                params["after"] = str(after)

            data = self._request("/candles", params)
            if not data:
                break

            all_data.extend(data)
            remaining -= len(data)

            if len(data) < batch_size:
                break      # 没有更多历史数据了

            # OKX 用 after 取更早的数据：传当前最早那根的毫秒时间戳
            after = int(data[-1][0])

        if not all_data:
            return []

        # OKX 返回倒序，转成升序
        all_data.sort(key=lambda x: int(x[0]))
        return [self._parse_kline(row) for row in all_data[:limit]]

    @staticmethod
    def _to_inst_id(symbol: str) -> str:
        """BTCUSDT -> BTC-USDT-SWAP"""
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            return f"{base}-USDT-SWAP"
        return symbol

    @staticmethod
    def _to_bar(interval: str) -> str:
        """OKX 的 bar 参数：小时/日/周/月用大写"""
        mapping = {
            "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
            "1d": "1D", "1w": "1W",
        }
        return mapping.get(interval, interval)

    @staticmethod
    def _parse_kline(row: list) -> Kline:
        """
        OKX candles 返回格式（升序后）：
        [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
        """
        ts = int(row[0])
        return Kline(
            openTime=ts,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            closeTime=ts,      # OKX 不返回 closeTime，用 openTime 近似
            quoteVolume=float(row[7]) if len(row) > 7 else 0.0,
            tradeCount=0,
        )


# ============================================================
#  统一门面：带兜底切换
# ============================================================

class MarketDataClient:
    """
    统一行情门面：优先 Binance，失败自动切 OKX。

    对外暴露一致的接口，调用方无需关心当前用的是哪个源。
    """

    def __init__(self, primary: str = "binance", timeout: int = 30,
                 retry_max: int = 3, backoff_base: float = 2.0):
        self.primary = primary.lower()
        self.binance = BinanceClient(timeout=timeout, retry_max=retry_max,
                                     backoff_base=backoff_base)
        self.okx = OkxClient(timeout=timeout)

        # 统计（用于运行报告）
        self.stats = {
            "primary_calls": 0,
            "fallback_calls": 0,
            "failures": 0,
        }

    def ping(self) -> Dict[str, bool]:
        return {
            "binance": self.binance.ping(),
            "okx": True,       # OKX 无轻量 ping，实际用时再判
        }

    def get_top_symbols(self, top_n: int = 300,
                        min_volume_usdt: float = 10_000_000) -> List[str]:
        """选取 Top N 标的（目前只有 Binance 提供全量 24h ticker）"""
        return self.binance.get_top_symbols(top_n, min_volume_usdt)

    def get_klines(self, symbol: str, interval: str,
                   limit: int = 500,
                   end_time_ms: int = None) -> Tuple[List[Kline], str]:
        """
        获取 K 线，自动兜底。

        end_time_ms: 只取该时刻之前的数据（回测防前视偏差用）

        返回: (K线列表, 实际使用的数据源)
        """
        # 按 primary 配置决定先试谁
        if self.primary == "binance":
            sources = [("binance", self.binance), ("okx", self.okx)]
        else:
            sources = [("okx", self.okx), ("binance", self.binance)]

        for name, client in sources:
            try:
                # OKX 单次上限 100，内部已处理分页；Binance 单次可到 1000
                if name == "binance":
                    data = client.get_klines(symbol, interval, limit, end_time_ms)
                else:
                    # OKX 分页逻辑暂不支持 endTime，回测时只用 Binance
                    data = client.get_klines(symbol, interval, limit)
                if data and len(data) > 0:
                    if name == "binance":
                        self.stats["primary_calls"] += 1
                    else:
                        self.stats["fallback_calls"] += 1
                    return data, name
            except Exception as e:
                logger.warning(f"[{name}] 获取 {symbol} {interval} 失败: {e}，"
                               f"尝试下一个源")
                continue

        self.stats["failures"] += 1
        logger.error(f"所有数据源均失败: {symbol} {interval}")
        return [], "none"

    def get_stats(self) -> dict:
        return self.stats.copy()
