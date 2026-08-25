"""src/research 模块单元测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research import build_tearsheet, compute_comps, classify_industry, build_scenario


class TestTearsheet(unittest.TestCase):
    def test_technicals(self):
        from src.research.tearsheet import compute_technicals
        closes = [10 + i * 0.05 for i in range(60)]  # 上升趋势
        t = compute_technicals(closes)
        self.assertIsNotNone(t["ma5"])
        self.assertIsNotNone(t["ma20"])
        self.assertGreater(t["rsi14"], 50)  # 上涨趋势 RSI 高
        self.assertGreater(t["price_percentile"], 0.8)  # 价格在高位

    def test_build_tearsheet_buy(self):
        closes = [10 + i * 0.05 for i in range(120)]
        signal = {
            "action": "buy", "confidence": 0.7, "potential_score": 75,
            "reason": "多头排列", "buy_low": 12.5, "buy_high": 13.2,
            "stop_loss": 11.8, "target_price": 15.0, "risk_reward": 2.1,
            "suggested_qty": 500,
        }
        quote = {"price": 13.0, "change_pct": 1.5, "pe_ttm": 25, "pb": 4,
                 "mcap_yi": 800, "turnover_pct": 2.0, "vol_ratio": 1.2, "amount": 5e8}
        ts = build_tearsheet(
            symbol="sz300750", name="宁德时代", quote=quote,
            closes=closes, volumes=[1e6] * len(closes), signal=signal,
            capital={"main_net": 1.2e7, "direction": "inflow"},
            market_session="连续竞价",
        )
        self.assertEqual(ts["rating"]["action"], "buy")
        self.assertGreater(ts["rating"]["score"], 60)
        self.assertEqual(ts["trade_zone"]["target_price"], 15.0)
        self.assertEqual(ts["name"], "宁德时代")

    def test_build_tearsheet_insufficient_data(self):
        ts = build_tearsheet(
            symbol="sz000001", name="平安银行", quote={"price": 10},
            closes=[10, 10.1], signal={"action": "hold"}, market_session="",
        )
        self.assertIsNone(ts["technicals"]["ma5"])  # 数据不足不崩溃


class TestComps(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_industry("sh600519"), "白酒")
        self.assertEqual(classify_industry("sz300750"), "新能源车")
        self.assertIsNone(classify_industry("sh000001"))

    def test_compute_comps(self):
        quotes = {
            "sh600519": {"name": "贵州茅台", "price": 1500, "pe_ttm": 30, "pb": 8, "mcap_yi": 18800},
            "sz000858": {"name": "五粮液", "price": 130, "pe_ttm": 20, "pb": 5, "mcap_yi": 5000},
            "sh600809": {"name": "山西汾酒", "price": 200, "pe_ttm": 28, "pb": 7, "mcap_yi": 2400},
        }
        c = compute_comps("sh600519", "白酒", quotes)
        self.assertEqual(c["industry"], "白酒")
        self.assertEqual(c["peer_count"], 2)
        self.assertIsNotNone(c["median_pe"])
        self.assertTrue(c["rows"][0]["is_target"])
        self.assertIn("conclusion", c)
        # 茅台 PE 30 vs 同业中位数 (20+28)/2=24 → 偏贵
        self.assertEqual(c["conclusion"]["verdict"], "偏贵")

    def test_comps_unknown_industry(self):
        c = compute_comps("sh000001", None, {})
        self.assertEqual(c["industry"], "未知")
        self.assertEqual(c["peer_count"], 0)


class TestScenario(unittest.TestCase):
    def test_earnings_beat(self):
        s = build_scenario(symbol="sz300750", name="宁德时代", price=200,
                           event="earnings_beat", volatility_pct=30)
        self.assertEqual(s["event_label"], "业绩超预期")
        self.assertEqual(len(s["scenarios"]), 3)
        optimistic = next(x for x in s["scenarios"] if x["key"] == "optimistic")
        base = next(x for x in s["scenarios"] if x["key"] == "base")
        pessimistic = next(x for x in s["scenarios"] if x["key"] == "pessimistic")
        self.assertGreater(optimistic["target_price"], base["target_price"])
        self.assertGreater(base["target_price"], pessimistic["target_price"])
        self.assertEqual(s["advice"]["action"], "关注回踩买入")

    def test_earnings_miss(self):
        s = build_scenario(symbol="sh600519", name="贵州茅台", price=1500,
                           event="earnings_miss", volatility_pct=20)
        self.assertEqual(s["advice"]["action"], "回避或减仓")

    def test_custom_impact(self):
        s = build_scenario(symbol="sz000001", name="平安银行", price=10,
                           event="custom", custom_impact=0.20, volatility_pct=25)
        base = next(x for x in s["scenarios"] if x["key"] == "base")
        self.assertAlmostEqual(base["change_pct"], 10.0, delta=0.1)  # 0.2*0.5=10%


if __name__ == "__main__":
    unittest.main(verbosity=2)
