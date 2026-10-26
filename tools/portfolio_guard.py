"""组合层的风险闸门：只看一个数（账户总权益），只做一件事（回撤到阈值就清仓并停机）。

每 30 秒由 tools/forward_bot.ps1 调一次 `--once`。峰值记在 data/portfolio_guard.json，
所以重启不会丢；触发后写 halted=true，之后只报告、不再交易（要 `--resume` 人工恢复）。

触发时先写停机标志 data/HALT（见 bot/halt.py）再清仓：机器人开新仓前都会检查它，所以信号
再翻转也不会重新开仓。`--resume` 会删掉它。人工建这个文件也能让机器人停止开仓，`--status` 会显示。

    python tools/portfolio_guard.py --status     # 看一眼：权益、峰值、回撤、持仓
    python tools/portfolio_guard.py --once       # 检查一次，必要时清仓
    python tools/portfolio_guard.py --resume     # 解除停机（删掉 data/HALT），峰值重置为当前权益
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ccxt  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from bot.config import api_credentials  # noqa: E402
from bot.halt import HALT_FILE, clear_halt, halt_reason, write_halt  # noqa: E402

STATE = Path("data/portfolio_guard.json")
HALT = Path(HALT_FILE)


def make_demo_exchange(exchange_id: str, creds: dict):
    """这里刻意不走 bot.data：那条路会 import pandas，常驻/定时跑一次要多占 ~120 MB，
    而 1 GB 的机器上这个闸门每 5 分钟才跑一次，没必要背着数据栈。"""
    ex = getattr(ccxt, exchange_id)({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "future", "adjustForTimeDifference": True},
        **creds,
    })
    if exchange_id.startswith("binance") and hasattr(ex, "enable_demo_trading"):
        ex.enable_demo_trading(True)
    else:
        ex.set_sandbox_mode(True)
    return ex


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


def check(ex, args) -> int:
    """报告一次；需要时清仓并停机。返回 0 正常、3 触发停机、1 缺配置。"""
    balance = ex.fetch_balance()["USDT"]
    equity = float(balance.get("total") or 0)
    positions = open_positions(ex)
    state = load_state(equity)

    if args.resume:
        state = {"peak": equity, "halted": False, "reason": None, "updated": None}
        save_state(state)
        removed = clear_halt(HALT)
        print(f"{now()}  已解除停机{'（已删除 ' + str(HALT) + '）' if removed else ''}，峰值重置为 {equity:.2f}")
        return 0

    if equity > state["peak"]:
        state["peak"] = equity
    dd = equity / state["peak"] - 1 if state["peak"] else 0.0
    flag = halt_reason(HALT)

    line = (f"{now()}  权益 {equity:.2f}  峰值 {state['peak']:.2f}  回撤 {dd:+.2%}  "
            f"持仓 {len(positions)}  免费 {float(balance.get('free') or 0):.2f}")
    if state["halted"]:
        line += "  [已停机]"
    elif flag:
        line += f"  [停机标志：{flag}，机器人不开新仓]"
    print(line, flush=True)

    if args.status:
        for p in positions:
            print(f"    {p['symbol']} {p['side']} {p['contracts']} 入场 {p['entryPrice']}")
        return 0

    if args.flatten:
        print(f"{now()}  手动清仓，共 {len(positions)} 个仓位")
        flatten(ex)
        return 0

    if state["halted"]:
        if flag is None:  # 停机标志被删了，或者是加这个标志之前就停机的：补上，机器人才会停止开仓
            write_halt(state.get("reason") or "组合已停机", HALT)
            print(f"{now()}  已停机但没有 {HALT}，已补写", flush=True)
        save_state(state)
        return 0

    if dd <= -args.max_dd:
        reason = f"组合回撤 {dd:.2%}（阈值 {args.max_dd:.0%}）"
        print(f"{now()}  组合回撤 {dd:.2%} 触发阈值 {args.max_dd:.0%}，清仓并停机")
        write_halt(reason, HALT)  # 先挡住新开仓，再清仓
        closed = flatten(ex)
        state.update(halted=True, reason=reason, updated=None, closed=closed)
        save_state(state)
        return 3

    save_state(state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="组合层风险闸门")
    ap.add_argument("--once", action="store_true", help="检查一次，必要时清仓")
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="常驻：每 N 秒检查一次")
    ap.add_argument("--status", action="store_true", help="只报告")
    ap.add_argument("--resume", action="store_true", help="解除停机状态")
    ap.add_argument("--flatten", action="store_true", help="立即市价平掉所有仓位（不改停机状态）")
    ap.add_argument("--max-dd", type=float, default=0.20, help="组合回撤阈值")
    ap.add_argument("--exchange", default="binance")
    args = ap.parse_args()

    load_dotenv()
    creds = api_credentials(args.exchange)
    if not {"apiKey", "secret"} <= creds.keys():
        print("缺少 API Key")
        return 1
    ex = make_demo_exchange(args.exchange, creds)
    ex.load_markets()

    if not args.loop:
        return check(ex, args)

    while True:  # 常驻模式：单次失败不应该让闸门退出
        try:
            code = check(ex, args)
            if code == 3:
                print(f"{now()}  已清仓并停机；继续值守，人工 --resume 后恢复", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"{now()}  本轮检查失败: {e}", flush=True)
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())