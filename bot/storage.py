"""SQLite journal for live / paper runs: fills, equity snapshots and resumable state."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .broker import Fill

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run TEXT NOT NULL, ts INTEGER NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
    price REAL NOT NULL, fee REAL NOT NULL, pnl REAL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    run TEXT NOT NULL, ts INTEGER NOT NULL, equity REAL NOT NULL, PRIMARY KEY (run, ts)
);
CREATE TABLE IF NOT EXISTS state (
    run TEXT PRIMARY KEY, data TEXT NOT NULL
);
"""
# fills columns added after the first ledgers were written; added in place on open
FILL_EXTRAS = {"ref_price": "REAL", "trigger_price": "REAL", "order_id": "TEXT"}


class Storage:
    """`run` identifies one bot instance, e.g. 'paper:okx:perp:BTC/USDT:USDT:1h:ma_cross'."""

    def __init__(self, path: str | Path, run: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        # 多品种并行时会有多个进程写同一个账本，WAL 下读写不互相阻塞
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._add_fill_columns()
        self.run = run

    def _add_fill_columns(self) -> None:
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(fills)")}
        for name, type_ in FILL_EXTRAS.items():
            if name in have:
                continue
            try:
                self.conn.execute(f"ALTER TABLE fills ADD COLUMN {name} {type_}")
            except sqlite3.OperationalError as e:  # the other bot process got there first
                if "duplicate column" not in str(e):
                    raise
        self.conn.commit()

    def record_fill(self, fill: Fill) -> None:
        self.conn.execute(
            "INSERT INTO fills (run, ts, side, qty, price, fee, pnl, reason, ref_price, trigger_price, order_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.run, fill.ts, fill.side, fill.qty, fill.price, fill.fee, fill.pnl, fill.reason,
             fill.ref_price, fill.trigger_price, fill.order_id))
        self.conn.commit()

    def record_equity(self, ts: int, equity: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?)", (self.run, ts, equity))
        self.conn.commit()

    def save_state(self, state: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (self.run, json.dumps(state)))
        self.conn.commit()

    def load_state(self) -> dict | None:
        row = self.conn.execute("SELECT data FROM state WHERE run = ?", (self.run,)).fetchone()
        return json.loads(row[0]) if row else None

    def fills(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT ts, side, qty, price, fee, pnl, reason FROM fills WHERE run = ? ORDER BY id",
            (self.run,)).fetchall()
