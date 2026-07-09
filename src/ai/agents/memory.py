"""
反思记忆 - 抄袭 TradingAgents 的 reflection + realized-return 回灌

平仓时:计算已实现盈亏 -> 让 LLM 写一段「经验教训」-> 存入 SQLite。
下次对该股决策前:取最近 N 条反思注入提示词,让多智能体从历史盈亏中学习。

复用项目 SQLite 风格(参照 src/utils/state_manager.py 的线程局部连接 + WAL)。
离线时 LLM 写不了反思,就存一条基于盈亏的规则文本,依然可回灌。
"""
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional


class ReflectionMemory:
    def __init__(self, db_path: str = "data/trading_state.db", llm=None,
                 deep_model: Optional[str] = None):
        self.db_path = db_path
        self.llm = llm
        self.deep_model = deep_model
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    @property
    def conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=5)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                decision TEXT,
                entry_price REAL,
                exit_price REAL,
                pnl REAL,
                pnl_pct REAL,
                reflection TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_reflect_symbol
                ON agent_reflections(symbol, id DESC);
        """)
        conn.commit()
        conn.close()

    def record_trade_close(self, symbol: str, entry_price: float, exit_price: float,
                           quantity: int, decision_reason: str = '') -> str:
        """平仓时调用:算盈亏 -> 生成反思 -> 存库。返回反思文本。"""
        try:
            pnl = (exit_price - entry_price) * quantity
            pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0.0
        except Exception:
            pnl, pnl_pct = 0.0, 0.0

        reflection = self._generate_reflection(symbol, entry_price, exit_price,
                                               pnl, pnl_pct, decision_reason)
        try:
            self.conn.execute(
                "INSERT INTO agent_reflections "
                "(symbol, decision, entry_price, exit_price, pnl, pnl_pct, reflection) "
                "VALUES (?,?,?,?,?,?,?)",
                (symbol, decision_reason[:300], entry_price, exit_price,
                 round(pnl, 2), round(pnl_pct, 4), reflection),
            )
            self.conn.commit()
        except Exception:
            pass
        return reflection

    def _generate_reflection(self, symbol, entry, exit_price, pnl, pnl_pct, reason) -> str:
        outcome = '盈利' if pnl > 0 else ('亏损' if pnl < 0 else '持平')
        rule_text = (f"{symbol} 本次{outcome} {pnl_pct:+.2%}(买入{entry:.2f}->卖出{exit_price:.2f})。"
                     f"决策依据: {reason[:80]}")

        if self.llm is not None and self.llm.is_available():
            sys = ('你是交易复盘专家。基于一次已平仓交易的结果,用不超过80字中文总结'
                   '「经验教训」,指出决策中对/错之处,供未来同类决策参考。')
            user = (f"股票{symbol}: 买入价{entry:.2f},卖出价{exit_price:.2f},"
                    f"盈亏{pnl_pct:+.2%}。当时决策依据: {reason[:120]}")
            text = self.llm.chat(sys, user, model=self.deep_model, fallback=rule_text)
            return text or rule_text
        return rule_text

    def get_recent(self, symbol: str, limit: int = 3) -> str:
        """取该股最近 N 条反思,拼成文本供提示词注入。无则返回空串。"""
        try:
            cur = self.conn.execute(
                "SELECT pnl_pct, reflection FROM agent_reflections "
                "WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            )
            rows = cur.fetchall()
        except Exception:
            return ''
        if not rows:
            return ''
        lines = [f"- ({pct:+.1%}) {refl}" for pct, refl in rows if refl]
        return "\n".join(lines)

    def count(self, symbol: Optional[str] = None) -> int:
        try:
            if symbol:
                cur = self.conn.execute(
                    "SELECT COUNT(*) FROM agent_reflections WHERE symbol=?", (symbol,))
            else:
                cur = self.conn.execute("SELECT COUNT(*) FROM agent_reflections")
            return int(cur.fetchone()[0])
        except Exception:
            return 0
