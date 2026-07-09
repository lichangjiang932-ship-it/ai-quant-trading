import sqlite3
import json
import os
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from pathlib import Path
import pickle
import time


class StateManager:
    def __init__(self, db_path: str = "data/trading_state.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    @property
    def conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=5)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS account_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                quantity INTEGER NOT NULL DEFAULT 0,
                avg_cost REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                filled_quantity INTEGER DEFAULT 0,
                filled_price REAL,
                commission REAL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                commission REAL DEFAULT 0,
                stamp_tax REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                strategy TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                price REAL,
                quantity INTEGER,
                reason TEXT,
                confidence REAL,
                strategy TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS market_data_cache (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL DEFAULT 'tick',
                data BLOB,
                cached_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (symbol, timeframe)
            );

            CREATE TABLE IF NOT EXISTS daily_summary (
                date TEXT PRIMARY KEY,
                total_trades INTEGER DEFAULT 0,
                total_buy_amount REAL DEFAULT 0,
                total_sell_amount REAL DEFAULT 0,
                total_commission REAL DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                start_equity REAL DEFAULT 0,
                end_equity REAL DEFAULT 0,
                max_drawdown REAL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(created_at);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(created_at);
        """)
        conn.commit()
        conn.close()

    def save_account_state(self, key: str, value: Any):
        self.conn.execute(
            "INSERT OR REPLACE INTO account_state (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, json.dumps(value, ensure_ascii=False, default=str))
        )
        self.conn.commit()

    def load_account_state(self, key: str, default: Any = None) -> Any:
        cursor = self.conn.execute("SELECT value FROM account_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return row[0]
        return default

    def save_position(self, symbol: str, quantity: int, avg_cost: float):
        self.conn.execute(
            """INSERT OR REPLACE INTO positions (symbol, quantity, avg_cost, updated_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (symbol, quantity, avg_cost)
        )
        self.conn.commit()

    def remove_position(self, symbol: str):
        self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        self.conn.commit()

    def get_positions(self) -> List[Dict]:
        cursor = self.conn.execute(
            "SELECT symbol, quantity, avg_cost FROM positions WHERE quantity > 0"
        )
        return [
            {'symbol': row[0], 'quantity': row[1], 'avg_cost': row[2]}
            for row in cursor.fetchall()
        ]

    def save_order(self, order_dict: Dict):
        self.conn.execute(
            """INSERT OR REPLACE INTO orders
               (order_id, symbol, direction, quantity, price, status, filled_quantity, filled_price, commission, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                order_dict.get('order_id', ''),
                order_dict.get('symbol', ''),
                order_dict.get('direction', ''),
                order_dict.get('quantity', 0),
                order_dict.get('price'),
                order_dict.get('status', 'pending'),
                order_dict.get('filled_quantity', 0),
                order_dict.get('filled_price'),
                order_dict.get('commission', 0),
            )
        )
        self.conn.commit()

    def get_pending_orders(self) -> List[Dict]:
        cursor = self.conn.execute(
            "SELECT * FROM orders WHERE status IN ('pending', 'submitted') ORDER BY created_at"
        )
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def save_trade(self, trade: Dict):
        self.conn.execute(
            """INSERT INTO trades (order_id, symbol, direction, quantity, price, amount, commission, stamp_tax, pnl, strategy, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.get('order_id', ''),
                trade.get('symbol', ''),
                trade.get('direction', ''),
                trade.get('quantity', 0),
                trade.get('price', 0),
                trade.get('amount', 0),
                trade.get('commission', 0),
                trade.get('stamp_tax', 0),
                trade.get('pnl', 0),
                trade.get('strategy', ''),
                trade.get('reason', ''),
            )
        )
        self.conn.commit()

    def get_recent_trades(self, limit: int = 100) -> List[Dict]:
        cursor = self.conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def save_signal(self, signal: Dict):
        self.conn.execute(
            """INSERT INTO signals (symbol, signal_type, price, quantity, reason, confidence, strategy)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                signal.get('symbol', ''),
                signal.get('signal_type', ''),
                signal.get('price', 0),
                signal.get('quantity', 0),
                signal.get('reason', ''),
                signal.get('confidence', 0),
                signal.get('strategy', ''),
            )
        )
        self.conn.commit()

    def cache_market_data(self, symbol: str, data: Any, timeframe: str = 'tick'):
        blob = pickle.dumps(data)
        self.conn.execute(
            "INSERT OR REPLACE INTO market_data_cache (symbol, timeframe, data, cached_at) VALUES (?, ?, ?, datetime('now'))",
            (symbol, timeframe, blob)
        )
        self.conn.commit()

    def get_cached_market_data(self, symbol: str, timeframe: str = 'tick', max_age_seconds: int = 5) -> Optional[Any]:
        cursor = self.conn.execute(
            """SELECT data, cached_at FROM market_data_cache
               WHERE symbol = ? AND timeframe = ?
               AND (julianday('now') - julianday(cached_at)) * 86400 <= ?""",
            (symbol, timeframe, max_age_seconds)
        )
        row = cursor.fetchone()
        if row:
            return pickle.loads(row[0])
        return None

    def update_daily_summary(self, date: str = None):
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')

        cursor = self.conn.execute(
            """SELECT
                   COUNT(*) as total_trades,
                   COALESCE(SUM(CASE WHEN direction='buy' THEN amount ELSE 0 END), 0) as total_buy,
                   COALESCE(SUM(CASE WHEN direction='sell' THEN amount ELSE 0 END), 0) as total_sell,
                   COALESCE(SUM(commission), 0) as total_comm,
                   COALESCE(SUM(pnl), 0) as total_pnl
               FROM trades WHERE date(created_at) = ?""",
            (date,)
        )
        row = cursor.fetchone()

        self.conn.execute(
            """INSERT OR REPLACE INTO daily_summary
               (date, total_trades, total_buy_amount, total_sell_amount, total_commission, total_pnl, created_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (date, row[0], row[1], row[2], row[3], row[4])
        )
        self.conn.commit()

    def get_daily_summary(self, days: int = 30) -> List[Dict]:
        cursor = self.conn.execute(
            "SELECT * FROM daily_summary ORDER BY date DESC LIMIT ?", (days,)
        )
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_account_snapshot(self) -> Dict:
        positions = self.get_positions()
        cash = self.load_account_state('cash', 0)
        total_position_value = self.load_account_state('position_value', 0)
        return {
            'cash': cash,
            'position_value': total_position_value,
            'total_asset': cash + total_position_value,
            'positions': positions,
            'updated_at': datetime.now().isoformat()
        }

    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
