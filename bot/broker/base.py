"""Broker interface shared by the simulator and real exchange connections."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field


@dataclass
class Position:
    side: int                 # 1 long, -1 short
    qty: float                # base-asset quantity
    entry: float
    opened_at: int
    entry_fee: float = 0.0
    margin: float = 0.0       # perp isolated margin
    liq_price: float | None = None
    stop: float | None = None
    take_profit: float | None = None
    funding: float = 0.0      # funding received (+) / paid (-) while open


@dataclass
class Fill:
    ts: int
    side: str                 # "buy" / "sell"
    qty: float
    price: float
    fee: float
    reason: str
    pnl: float | None = None  # net realized PnL of the round trip, on closing fills


@dataclass
class BrokerStats:
    fees: float = 0.0
    funding: float = 0.0      # net funding paid (+) / received (-)
    closed_trades: list[dict] = field(default_factory=list)


class Broker(ABC):
    position: Position | None
    stats: BrokerStats
    # an order whose outcome couldn't be determined; no new orders until it's resolved
    unresolved: dict | None = None

    @abstractmethod
    def equity(self, price: float) -> float:
        """Account equity marked at `price`."""

    @abstractmethod
    def open(self, side: int, qty: float, price: float, ts: int, reason: str) -> Fill | None:
        """Open a position at roughly `price`. Returns None if nothing was filled."""

    @abstractmethod
    def close(self, price: float, ts: int, reason: str) -> Fill | None:
        """Close the whole position at roughly `price`."""

    def liquidate(self, ts: int) -> Fill | None:
        raise NotImplementedError

    def apply_funding(self, rate: float, price: float, ts: int) -> None:
        pass

    def reconcile(self, price: float, ts: int) -> Fill | None:
        """Bring the local position in line with the account. Returns a Fill the Trader
        hasn't seen yet (e.g. a position that turned out to be closed already), else None.
        Local brokers are always in line."""
        return None

    def _record_close(self, pos: Position, exit_price: float, ts: int, exit_fee: float,
                      net_pnl: float | None, reason: str) -> None:
        self.stats.closed_trades.append({
            "entry_ts": pos.opened_at, "exit_ts": ts,
            "side": "long" if pos.side == 1 else "short", "qty": pos.qty,
            "entry": pos.entry, "exit": exit_price,
            "fees": pos.entry_fee + exit_fee, "funding": pos.funding,
            "pnl": net_pnl,
            "return_pct": None if net_pnl is None else net_pnl / (pos.qty * pos.entry),
            "reason": reason,
        })

    def state(self) -> dict:
        return {"position": asdict(self.position) if self.position else None}

    def restore(self, state: dict) -> None:
        self.position = Position(**state["position"]) if state.get("position") else None
