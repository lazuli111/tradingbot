"""组合层的风险闸门：只看一个数（账户总权益），只做一件事（回撤到阈值就清仓并停机）。

每 30 秒由 tools/forward_bot.ps1 调一次 `--once`。峰值记在 data/portfolio_guard.json，
所以重启不会丢；触发后写 halted=true，之后只报告、不再交易（要 `--resume` 人工恢复）。

    python tools/portfolio_guard.py --status     # 看一眼：权益、峰值、回撤、持仓
    python tools/portfolio_guard.py --once       # 检查一次，必要时清仓
    python tools/portfolio_guard.py --resume     # 解除停机，峰值重置为当前权益
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from bot.config import api_credentials  # noqa: E402
from bot.data import make_exchange  # noqa: E402

STATE = Path("data/portfolio_guard.json")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_state(equity: float) -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"peak": equity, "halted": False, "reason": None, "updated": None}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state["updated"] = now()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def open_positions(ex) -> list[dict]:
    return [p for p in ex.fetch_positions() if float(p.get("contracts") or 0)]


def flatten(ex) -> int:
    closed = 0
    for p in open_positions(ex):
        symbol, qty = p["symbol"], float(p["contracts"])
        side = "sell" if p["side"] == "long" else "buy"
        try:
            amount = float(ex.amount_to_precision(symbol, qty))
            if amount <= 0:
                continue
            ex.create_order(symbol, "market", side, amount, None, {"reduceOnly": True})
            print(f"{now()}  清仓 {symbol} {side} {amount}")
            closed += 1
        except Exception as e:  # noqa: BLE001 - 清仓时任何异常都要继续尝试其它仓位
            print(f"{now()}  清仓失败 {symbol}: {e}")
    return closed


def main() -> int:
    ap = argparse.ArgumentParser(description="组合层风险闸门")
    ap.add_argument("--once", action="store_true", help="检查一次，必要时清仓")
    ap.add_argument("--status", action="store_true", help="只报告")
    ap.add_argument("--resume", action="store_true", help="解除停机状态")
    ap.add_argument("--max-dd", type=float, default=0.20, help="组合回撤阈值")
    ap.add_argument("--exchange", default="binance")
    args = ap.parse_args()

    load_dotenv()
    creds = api_credentials(args.exchange)
    if not {"apiKey", "secret"} <= creds.keys():
        print("缺少 API Key")
        return 1
    ex = make_exchange(args.exchange, "perp", creds, testnet=True)
    ex.load_markets()

    balance = ex.fetch_balance()["USDT"]
    equity = float(balance.get("total") or 0)
    positions = open_positions(ex)
    state = load_state(equity)

    if args.resume:
        state = {"peak": equity, "halted": False, "reason": None, "updated": None}
        save_state(state)
        print(f"{now()}  已解除停机，峰值重置为 {equity:.2f}")
        return 0

    if equity > state["peak"]:
        state["peak"] = equity
    dd = equity / state["peak"] - 1 if state["peak"] else 0.0

    line = (f"{now()}  权益 {equity:.2f}  峰值 {state['peak']:.2f}  回撤 {dd:+.2%}  "
            f"持仓 {len(positions)}  免费 {float(balance.get('free') or 0):.2f}")
    print(line + ("  [已停机]" if state["halted"] else ""))

    if args.status:
        for p in positions:
            print(f"    {p['symbol']} {p['side']} {p['contracts']} 入场 {p['entryPrice']}")
        return 0

    if state["halted"]:
        save_state(state)
        return 0

    if dd <= -args.max_dd:
        print(f"{now()}  组合回撤 {dd:.2%} 触发阈值 {args.max_dd:.0%}，清仓并停机")
        closed = flatten(ex)
        state.update(halted=True, reason=f"组合回撤 {dd:.2%}（阈值 {args.max_dd:.0%}）",
                     updated=None, closed=closed)
        save_state(state)
        return 3

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
