# -*- coding: utf-8 -*-
"""锁死 OKX 标的池排序用的是【真实 USDT 成交额】而非【币数量】。

背景（2026-09-08 严重 bug）：
  OkxClient._sorted_usdt_swaps 原先把 volCcy24h 直接当 USDT 成交额用。
  但 U 本位合约的 volCcy24h 单位是【币数量(base ccy)】，不是 USDT。
  后果：SATS/SHIB/PEPE 等超低价币凭"币数量"天文数字霸占 top300，
  BTC/ETH/SOL 主力币被排挤出扫描池 —— 线上连推几天没有一条像样形态。

这 5 个用例把修正后的语义钉死，谁再改回去都会红。
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from market_data import OkxClient  # noqa: E402

# 真实 OKX /tickers 返回片段（2026-09-08 实取样，字段已裁剪）
FAKE_TICKERS = [
    # 超低价币：币数量天文数字，但真实成交额只有 0.01~0.12 亿美元
    {"instId": "SATS-USDT-SWAP", "last": "1.1336e-08",
     "vol24h": "6135740", "volCcy24h": "61357400000000"},
    {"instId": "SHIB-USDT-SWAP", "last": "5.388e-06",
     "vol24h": "2253887", "volCcy24h": "2253887100000"},
    {"instId": "PEPE-USDT-SWAP", "last": "3.605e-06",
     "vol24h": "2798353", "volCcy24h": "27983526000000"},
    # 主力币：币数量小，真实成交额几十亿美元
    {"instId": "BTC-USDT-SWAP", "last": "78364.9",
     "vol24h": "5004651", "volCcy24h": "50046.51"},
    {"instId": "ETH-USDT-SWAP", "last": "2476.91",
     "vol24h": "19388012", "volCcy24h": "1938801.15"},
    {"instId": "SOL-USDT-SWAP", "last": "102.83",
     "vol24h": "8082847", "volCcy24h": "8082846.69"},
]


class _FakeOkx(OkxClient):
    """绕过网络：_request 直接吐固定 tickers"""
    def __init__(self):
        super().__init__()
        self.session = None          # 确保不会被真的用到

    def _request(self, path, params=None):
        assert path == "/tickers", path
        return FAKE_TICKERS


class TestOkxVolumeRanking(unittest.TestCase):
    def setUp(self):
        self.cli = _FakeOkx()

    def test_main_coins_rank_above_meme_coins(self):
        """主力币必须排在超低价 meme 币之前（修复前恰好相反）"""
        ranked = self.cli._sorted_usdt_swaps(0)
        order = [inst.replace("-USDT-SWAP", "USDT") for inst, _ in ranked]
        self.assertEqual(order[:3], ["ETHUSDT", "BTCUSDT", "SOLUSDT"],
                         f"实际排序: {order}")
        # meme 币被挤到最后
        self.assertEqual(order[-3:],
                         ["PEPEUSDT", "SHIBUSDT", "SATSUSDT"], order)

    def test_volume_is_usdt_not_coin_count(self):
        """返回的数值必须是 USDT 成交额 = 币数量 × 价格"""
        ranked = dict(self.cli._sorted_usdt_swaps(0))
        # BTC: 50046.51 币 × 78364.9 ≈ 39.2 亿美元
        btc = ranked["BTC-USDT-SWAP"]
        self.assertAlmostEqual(btc, 50046.51 * 78364.9, delta=1e6)
        self.assertGreater(btc, 3e9)            # 30 亿美元以上
        # SATS: 61.3574e12 币 × 1.1336e-08 ≈ 0.007 亿美元
        sats = ranked["SATS-USDT-SWAP"]
        self.assertAlmostEqual(sats, 61357400000000 * 1.1336e-08, delta=1e5)
        self.assertLess(sats, 5e7)              # 不到 5000 万美元

    def test_min_volume_filter_uses_usdt(self):
        """门槛过滤也按 USDT 额：10 亿美元门槛下只有 ETH/BTC 留下"""
        ranked = self.cli._sorted_usdt_swaps(1e9)      # 10 亿美元
        syms = [i.replace("-USDT-SWAP", "USDT") for i, _ in ranked]
        self.assertEqual(syms, ["ETHUSDT", "BTCUSDT"], syms)

    def test_get_top_symbols_returns_main_coins(self):
        """get_top_symbols 端到端：top2 必须是 ETH/BTC，不能是 SATS/SHIB"""
        syms = self.cli.get_top_symbols(top_n=2, min_volume_usdt=0)
        self.assertEqual(syms, ["ETHUSDT", "BTCUSDT"], syms)

    def test_tiered_pools_use_usdt_boundary(self):
        """分层池按 USDT 额划分：core 拿主力币，tail 拿小币"""
        core, tail = self.cli.get_symbols_tiered(
            core_top_n=10, core_min_volume_usdt=1e9,
            tail_top_n=10, tail_min_volume_usdt=0)
        self.assertEqual(core, ["ETHUSDT", "BTCUSDT"], core)
        self.assertEqual(set(tail), {"SOLUSDT", "PEPEUSDT", "SHIBUSDT",
                                     "SATSUSDT"}, tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
