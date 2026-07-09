"""
交易日志系统
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path
import pandas as pd


class TradeLogger:
    """交易日志类"""
    
    def __init__(self, log_dir: str = "logs"):
        """
        初始化交易日志
        
        Args:
            log_dir: 日志目录
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 日志文件路径
        self.trade_log_file = self.log_dir / "trades.json"
        self.signal_log_file = self.log_dir / "signals.json"
        self.error_log_file = self.log_dir / "errors.log"
        self.daily_report_dir = self.log_dir / "daily_reports"
        self.daily_report_dir.mkdir(exist_ok=True)
    
    def log_trade(self, trade: Dict):
        """
        记录交易
        
        Args:
            trade: 交易数据
        """
        trade['timestamp'] = datetime.now().isoformat()
        
        # 读取现有日志
        trades = self._read_json(self.trade_log_file)
        trades.append(trade)
        
        # 写入日志
        self._write_json(self.trade_log_file, trades)
        
        # 打印交易信息
        direction = trade.get('direction', '')
        symbol = trade.get('symbol', '')
        price = trade.get('price', 0)
        quantity = trade.get('quantity', 0)
        print(f"[交易] {direction} {symbol} {quantity}股 @ {price}")
    
    def log_signal(self, signal: Dict):
        """
        记录信号
        
        Args:
            signal: 信号数据
        """
        signal['timestamp'] = datetime.now().isoformat()
        
        # 读取现有日志
        signals = self._read_json(self.signal_log_file)
        signals.append(signal)
        
        # 写入日志
        self._write_json(self.signal_log_file, signals)
    
    def log_error(self, error: str, context: str = ""):
        """
        记录错误
        
        Args:
            error: 错误信息
            context: 上下文
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {context}: {error}\n"
        
        with open(self.error_log_file, 'a', encoding='utf-8') as f:
            f.write(log_entry)
        
        print(f"[错误] {error}")
    
    def get_trades(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        symbol: Optional[str] = None
    ) -> pd.DataFrame:
        """
        获取交易记录
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            symbol: 股票代码
        
        Returns:
            DataFrame: 交易记录
        """
        trades = self._read_json(self.trade_log_file)
        
        if not trades:
            return pd.DataFrame()
        
        df = pd.DataFrame(trades)
        
        # 过滤日期
        if start_date:
            df = df[df['timestamp'] >= start_date]
        if end_date:
            df = df[df['timestamp'] <= end_date]
        
        # 过滤股票代码
        if symbol:
            df = df[df['symbol'] == symbol]
        
        return df
    
    def get_signals(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        symbol: Optional[str] = None
    ) -> pd.DataFrame:
        """
        获取信号记录
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            symbol: 股票代码
        
        Returns:
            DataFrame: 信号记录
        """
        signals = self._read_json(self.signal_log_file)
        
        if not signals:
            return pd.DataFrame()
        
        df = pd.DataFrame(signals)
        
        # 过滤日期
        if start_date:
            df = df[df['timestamp'] >= start_date]
        if end_date:
            df = df[df['timestamp'] <= end_date]
        
        # 过滤股票代码
        if symbol:
            df = df[df['symbol'] == symbol]
        
        return df
    
    def generate_daily_report(self, date: Optional[str] = None) -> Dict:
        """
        生成日报
        
        Args:
            date: 日期（默认今天）
        
        Returns:
            Dict: 日报数据
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        # 获取当日交易
        trades = self.get_trades(start_date=date, end_date=date + " 23:59:59")
        
        if trades.empty:
            return {'date': date, 'trades': [], 'summary': {}}
        
        # 计算统计信息
        buy_trades = trades[trades['direction'] == 'buy']
        sell_trades = trades[trades['direction'] == 'sell']
        
        total_buy_amount = buy_trades['amount'].sum() if not buy_trades.empty else 0
        total_sell_amount = sell_trades['amount'].sum() if not sell_trades.empty else 0
        total_commission = trades['commission'].sum() if 'commission' in trades.columns else 0
        total_stamp_tax = trades['stamp_tax'].sum() if 'stamp_tax' in trades.columns else 0
        
        summary = {
            'date': date,
            'total_trades': len(trades),
            'buy_trades': len(buy_trades),
            'sell_trades': len(sell_trades),
            'total_buy_amount': total_buy_amount,
            'total_sell_amount': total_sell_amount,
            'net_amount': total_sell_amount - total_buy_amount,
            'total_commission': total_commission,
            'total_stamp_tax': total_stamp_tax,
            'total_cost': total_commission + total_stamp_tax
        }
        
        report = {
            'date': date,
            'trades': trades.to_dict('records'),
            'summary': summary
        }
        
        # 保存日报
        report_file = self.daily_report_dir / f"{date}.json"
        self._write_json(report_file, report)
        
        return report
    
    def get_performance_summary(self, days: int = 30) -> Dict:
        """
        获取绩效摘要
        
        Args:
            days: 天数
        
        Returns:
            Dict: 绩效摘要
        """
        # 获取最近N天的交易
        from datetime import timedelta
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        trades = self.get_trades(
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d")
        )
        
        if trades.empty:
            return {}
        
        # 计算绩效指标
        total_trades = len(trades)
        buy_amount = trades[trades['direction'] == 'buy']['amount'].sum()
        sell_amount = trades[trades['direction'] == 'sell']['amount'].sum()
        
        return {
            'period_days': days,
            'total_trades': total_trades,
            'total_buy_amount': buy_amount,
            'total_sell_amount': sell_amount,
            'net_amount': sell_amount - buy_amount,
            'avg_daily_trades': total_trades / days
        }
    
    def _read_json(self, file_path: Path) -> List:
        """读取JSON文件"""
        if not file_path.exists():
            return []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    
    def _write_json(self, file_path: Path, data):
        """写入JSON文件"""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入日志文件失败: {e}")