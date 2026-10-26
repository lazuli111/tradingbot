"""组合层停机标志（LIVE_PLAN C8）。

tools/portfolio_guard.py 触发时写 data/HALT（内容是原因），`--resume` 时删掉；机器人每次要开新仓前
都检查它，在就不开。平仓、止损照常执行。人工想让机器人停止开仓，也可以直接建这个文件。

只用标准库：guard 刻意不 import pandas。
"""
from __future__ import annotations

from pathlib import Path

HALT_FILE = "data/HALT"


def halt_reason(path: str | Path = HALT_FILE) -> str | None:
    """停机标志在就返回原因，不在返回 None。文件存在但读不了，也按已停机处理（宁可少开一单）。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        return f"读不了停机标志 {p}（{e}），按已停机处理"
    return text or "停机标志存在"


def write_halt(reason: str, path: str | Path = HALT_FILE) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(reason + "\n", encoding="utf-8")
    tmp.replace(p)          # 机器人不会读到写了一半的文件


def clear_halt(path: str | Path = HALT_FILE) -> bool:
    """删掉停机标志；原来就没有时返回 False。"""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    return True
