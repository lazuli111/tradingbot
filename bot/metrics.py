"""Performance statistics for an equity curve and a list of closed trades."""
from __future__ import annotations

import math

import pandas as pd

MS_PER_YEAR = 365 * 24 * 3600 * 1000


def compute_metrics(equity: pd.Series, trades: list[dict], initial_equity: float,
                    bar_ms: int, benchmark_return: float | None = None,
                    fees: float = 0.0, funding: float = 0.0) -> dict:
    """`equity` is indexed by bar timestamp (ms), one mark-to-market value per bar."""
    final = float(equity.iloc[-1])
    total_return = final / initial_equity - 1
    years = len(equity) * bar_ms / MS_PER_YEAR
    cagr = (final / initial_equity) ** (1 / years) - 1 if years > 0 and final > 0 else float("nan")

    returns = equity.pct_change().dropna()
    std = returns.std()
    sharpe = returns.mean() / std * math.sqrt(MS_PER_YEAR / bar_ms) if std and std > 0 else float("nan")
    drawdown = equity / equity.cummax() - 1

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_loss = -sum(losses)
    return {
        "final_equity": final,
        "total_return": total_return,
        "benchmark_return": benchmark_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else float("nan"),
        "profit_factor": sum(wins) / gross_loss if gross_loss > 0 else float("inf") if wins else float("nan"),
        "avg_trade_pnl": sum(pnls) / len(pnls) if pnls else float("nan"),
        "fees": fees,
        "funding": funding,
        "liquidations": sum(1 for t in trades if t["reason"] == "liquidation"),
    }
