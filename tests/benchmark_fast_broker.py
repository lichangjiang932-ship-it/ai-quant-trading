import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.execution.fast_broker import FastBroker

def benchmark():
    broker = FastBroker(initial_capital=1000000)
    broker.update_price('sh600000', 10.0)

    num = 500
    buy_lat = []
    sell_lat = []

    for i in range(num):
        t0 = time.perf_counter_ns()
        ok, oid, order = broker.buy('sh600000', 100, 10.0, 'benchmark')
        t1 = time.perf_counter_ns()
        buy_lat.append((t1 - t0) / 1_000_000)

        t0 = time.perf_counter_ns()
        ok, oid, order = broker.sell('sh600000', 0, 10.5, 'benchmark')
        t1 = time.perf_counter_ns()
        sell_lat.append((t1 - t0) / 1_000_000)

    avg_b = sum(buy_lat) / len(buy_lat)
    avg_s = sum(sell_lat) / len(sell_lat)
    info = broker.get_account_info()

    print("=" * 50)
    print(f"FastBroker 基准测试 - {num} 笔往返交易")
    print("=" * 50)
    print(f"买入延迟:")
    print(f"  平均: {avg_b:.4f}ms")
    print(f"  最小: {min(buy_lat):.4f}ms")
    print(f"  最大: {max(buy_lat):.4f}ms")
    print(f"  P99: {sorted(buy_lat)[int(len(buy_lat)*0.99)]:.4f}ms")
    print(f"卖出延迟:")
    print(f"  平均: {avg_s:.4f}ms")
    print(f"  最小: {min(sell_lat):.4f}ms")
    print(f"  最大: {max(sell_lat):.4f}ms")
    print(f"账户总资产: {info['total_asset']:.2f}")
    print(f"平均延迟(综合): {info['avg_latency_ms']:.4f}ms")

    print(f"\n预期: 买卖价差产生利润, 资产 > 初始本金")
    print(f"平均延迟: {info['avg_latency_ms']:.4f}ms (目标 < 0.1ms)")
    assert info['avg_latency_ms'] < 0.1
    print("延迟达标")
    print("所有测试通过!")

if __name__ == "__main__":
    benchmark()
