"""一个进程跑多个币（main.py testnet-multi）。

为什么：东京服务器一个机器人进程约 340 MB，大头是 ccxt 的市场表（load_markets），9 个币
各开一个进程放不下。这里每个币仍然是一个完整的 LiveRunner：自己的 Trader / RiskManager /
CcxtBroker、自己的存档（Storage 的 run 名带 symbol）、对账和软止损，只是行情和下单各共用
一个 ccxt 实例。每个币按 live.equity_fraction 用账户权益的一份（FractionalEquityBroker）。

一个币出错不影响其他币：
- 轮询出错：LiveRunner.poll 自己捕获，下一轮先对账、再重做这根 K 线（_needs_reconcile）；
- 启动出错（建 broker、读持仓、拉历史 K 线）：记日志，这个币下一轮再试，其他币照常跑。
  只有配置写错的币（ccxt.BadSymbol）在启动时直接报错退出。

请求量（Binance U 本位按 IP 每分钟 2400 权重；行情走正式站、下单走 Demo，两边各算各的）：
平时每个币每轮一次 ticker（权重 1），9 个币每 30 秒 9 权重。K 线收盘那一轮每个币多一次
K 线（limit 1000，权重 5）和一次 ticker；Demo 上对账读持仓 5、算权益读余额 5，下单不计
IP 权重。所有币在一个线程里依次处理，ccxt 的限速器会把请求错开，不需要另外错峰。
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from dataclasses import replace

import ccxt

from ..config import Config
from . import live

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(symbol)s]: %(message)s"
_current_symbol: contextvars.ContextVar[str] = contextvars.ContextVar("symbol", default="-")


class SymbolTag(logging.Filter):
    """Puts the coin being processed into every record as `symbol`: all of them log into one file."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.symbol = _current_symbol.get()
        return True


def install_symbol_tag(logger: logging.Logger | None = None) -> None:
    for handler in (logger or logging.getLogger()).handlers:
        handler.addFilter(SymbolTag())


@contextlib.contextmanager
def tagged(symbol: str):
    token = _current_symbol.set(symbol)
    try:
        yield
    finally:
        _current_symbol.reset(token)


class MultiRunner:
    def __init__(self, cfg: Config, mode: str):
        if not cfg.symbols:
            raise ValueError("没有币种：在配置里写 symbols，或用 --symbols")
        self.cfg = cfg
        self.mode = mode
        self.symbols = list(cfg.symbols)
        self.data_ex = live.make_exchange(cfg.exchange, cfg.market)  # public market data
        self.trade_ex = live.make_trade_exchange(cfg) if mode == "testnet" else None
        self.runners: dict[str, live.LiveRunner] = {}
        self.started: set[str] = set()
        if self.trade_ex is not None:
            self._check_balance()
        for symbol in self.symbols:
            self._create(symbol, reraise=(ccxt.BadSymbol,))  # a typo in the config: stop right away

    def _check_balance(self) -> None:
        """Every coin sizes off the margin asset's balance: with none, they'd all size off 0."""
        quote = self.symbols[0].split(":")[0].split("/")[1]
        total = float((self.trade_ex.fetch_balance().get(quote) or {}).get("total") or 0)
        if total <= 0:
            raise RuntimeError(
                f"交易账户读不到 {quote} 余额（{total}）。网页上明明有的话，可能是 ccxt 跳过了"
                f" updateTime=0（从没动过）的资产，见 DEPLOY.md 第 12 节")
        log.info("账户 %s 权益 %.2f，每个币按 %.2f 定仓位", quote, total, total * self.cfg.live.equity_fraction)

    def _create(self, symbol: str, reraise: tuple[type[Exception], ...] = ()) -> None:
        with tagged(symbol):
            try:
                self.runners[symbol] = live.LiveRunner(replace(self.cfg, symbol=symbol), self.mode,
                                                       data_ex=self.data_ex, trade_ex=self.trade_ex)
            except reraise:
                raise
            except Exception:
                log.exception("初始化失败，下一轮重试（其他币照常）")

    def _round(self) -> bool:
        """One pass over every coin. Returns True if a new bar closed for any of them."""
        new_bar = False
        for symbol in self.symbols:
            if symbol not in self.runners:
                self._create(symbol)
            runner = self.runners.get(symbol)
            if runner is None:
                continue
            with tagged(symbol):
                try:
                    if symbol in self.started:
                        new_bar |= runner.poll()
                    else:
                        log.info("启动: %s %s 策略 %s", runner.symbol, self.cfg.timeframe, runner.strategy)
                        runner.start()
                        self.started.add(symbol)
                except Exception:  # poll() catches its own errors: this is start(), or a bug
                    log.exception("本轮处理出错，下一轮重试（其他币照常）")
        return new_bar

    def _save_all(self) -> None:
        for symbol in self.started:
            with tagged(symbol):
                try:
                    self.runners[symbol]._save()
                except Exception:
                    log.exception("保存状态失败")

    def run(self, max_bars: int | None = None) -> None:
        """max_bars: stop after this many rounds in which a new bar closed (the coins share
        one timeframe, so their bars close in the same round)."""
        log.info("启动 %s 模式（一个进程 %d 个币）: %s %s %s 每个币按权益的 %.2f%%，停机标志 %s",
                 self.mode, len(self.symbols), self.cfg.exchange, ",".join(self.symbols),
                 self.cfg.timeframe, self.cfg.live.equity_fraction * 100, self.cfg.live.halt_file)
        processed = 0
        try:
            self._round()
            while max_bars is None or processed < max_bars:
                time.sleep(self.cfg.live.poll_seconds)
                processed += self._round()
        except KeyboardInterrupt:
            log.info("收到中断信号，保存状态后退出（持仓保持不变）")
        finally:
            self._save_all()
