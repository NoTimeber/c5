"""购买流水（sqlite）。数量和预算上限都从这里累计，重启不清零。"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    out_trade_no TEXT PRIMARY KEY,          -- 商户单号，我方生成
    ts           REAL NOT NULL,
    mode         TEXT NOT NULL,             -- dry / live
    name         TEXT NOT NULL,
    product_id   TEXT NOT NULL,             -- 在售 id；快速购买下单前不知道，为空
    price        REAL NOT NULL,             -- 下单价（快速购买记 max_price）
    status       TEXT NOT NULL,             -- unknown 结果未知 / ok / failed / cancelled
    order_id     TEXT,
    actual_pay   REAL,
    order_status INTEGER,                   -- 平台订单状态，见 sweeper.ORDER_*
    checked_at   REAL NOT NULL DEFAULT 0,   -- 上次向平台查询这笔单的时间
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchases_mode_status ON purchases (mode, status);
"""

COUNTED = ("ok", "unknown")  # 占用数量和预算的状态；结果未知的按已花费算，宁可少买
_UPDATABLE = {"status", "product_id", "order_id", "actual_pay", "order_status", "checked_at", "error"}


@dataclass(frozen=True)
class Purchase:
    out_trade_no: str
    ts: float
    mode: str
    name: str
    product_id: str
    price: float
    status: str
    order_id: str | None
    actual_pay: float | None
    order_status: int | None
    checked_at: float
    error: str | None

    @property
    def cost(self) -> float:
        return self.actual_pay if self.actual_pay is not None else self.price


@dataclass(frozen=True)
class Total:
    qty: int = 0
    spent: float = 0.0


class Store:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(str(path), timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def add(self, out_trade_no: str, *, mode: str, name: str, product_id: int | str, price: float,
            status: str, ts: float | None = None) -> None:
        self._db.execute(
            "INSERT INTO purchases (out_trade_no, ts, mode, name, product_id, price, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (out_trade_no, time.time() if ts is None else ts, mode, name, str(product_id), price, status),
        )
        self._db.commit()

    def update(self, out_trade_no: str, **fields) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"不能更新的字段: {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(f"UPDATE purchases SET {sets} WHERE out_trade_no = ?",
                         (*fields.values(), out_trade_no))
        self._db.commit()

    def get(self, out_trade_no: str) -> Purchase | None:
        row = self._db.execute("SELECT * FROM purchases WHERE out_trade_no = ?", (out_trade_no,)).fetchone()
        return Purchase(**row) if row else None

    def select(self, mode: str, *, statuses: tuple[str, ...] | None = None) -> list[Purchase]:
        sql, args = "SELECT * FROM purchases WHERE mode = ?", [mode]
        if statuses:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            args += statuses
        return [Purchase(**r) for r in self._db.execute(sql + " ORDER BY ts, rowid", args)]

    def totals(self, mode: str) -> dict[str, Total]:
        """每个饰品已占用的数量和花费。"""
        out: dict[str, Total] = {}
        for p in self.select(mode, statuses=COUNTED):
            t = out.get(p.name, Total())
            out[p.name] = Total(t.qty + 1, t.spent + p.cost)
        return out

    def product_ids(self, mode: str) -> set[int]:
        """已经下过单（含结果未知）的在售 id，不再重复买。"""
        return {int(p.product_id) for p in self.select(mode, statuses=COUNTED) if p.product_id}

    def clear(self, mode: str) -> None:
        self._db.execute("DELETE FROM purchases WHERE mode = ?", (mode,))
        self._db.commit()
