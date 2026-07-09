"""
运行示例策略
"""
import sys
import os

# 添加当前目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from examples.simple_strategy import run_momentum_strategy, run_mean_reversion_strategy


def main():
    """主函数"""
    print("量化交易框架 - 示例策略运行")
    print("=" * 50)
    
    try:
        # 运行动量策略
        print("1. 运行动量策略...")
        run_momentum_strategy()
        
        print("\n" + "=" * 50 + "\n")
        
        # 运行均值回归策略
        print("2. 运行均值回归策略...")
        run_mean_reversion_strategy()
        
        print("\n" + "=" * 50)
        print("示例策略运行完成！")
        print("请查看生成的图表文件。")
        
    except Exception as e:
        print(f"运行出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()