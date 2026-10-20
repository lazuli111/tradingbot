"""Polling loop for paper trading (local SimBroker on live prices) and exchange testnets."""
from __future__ import annotations

import logging
import time

import pandas as pd

from ..broker import Broker, Fill, SimBroker
from ..config import Config, api_credentials
from ..data import fetch_funding_range, fetch_ohlcv_range, make_exchange, timeframe_ms
from ..indicators import atr as compute_atr
from ..risk import RiskManager
from ..storage import Storage
from ..strategies import create_strategy
from ..trader import Trader

log = logging.getLogger(__name__)


def _fmt_ts(ts: int) -> str:
    return pd.Timestamp(ts, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")


class FractionalEquityBroker:
    """按比例缩放权益再交给风控。

    多品种并行时每个进程只该用自己那一份：比例 = 1/品种数。缩放只影响“拿多少
    钱去算仓位”，下单、持仓、统计仍然走真实 broker。
    """

    def __init__(self, inner: Broker, fraction: float):
        self._inner = inner
        self.fraction = fraction

    def equity(self, price: float) -> float:
        return self._inner.equity(price) * self.fraction

    def __getattr__(self, name):
        return getattr(self._inner, name)


class LiveRunner:
    def __init__(self, cfg: Config, mode: str):
        if mode not in ("paper", "testnet"):
            raise ValueError("mode 只能是 paper 或 testnet")
        self.cfg = cfg
        self.mode = mode
        self.symbol = cfg.market_symbol
        self.step = timeframe_ms(cfg.timeframe)
        self.strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
        self.data_ex = make_exchange(cfg.exchange, cfg.market)  # public market data

        run = f"{mode}:{cfg.exchange}:{cfg.market}:{self.symbol}:{cfg.timeframe}:{self.strategy.name}"
        self.storage = Storage(cfg.live.db_path, run)
        broker = self._make_broker()
        if cfg.live.equity_fraction < 1:
            broker = FractionalEquityBroker(broker, cfg.live.equity_fraction)
            log.info("按账户权益的 %.0f%% 计算仓位（多品种并行）", cfg.live.equity_fraction * 100)
        risk = RiskManager(cfg.risk, cfg.is_perp, cfg.initial_equity)
        self.trader = Trader(risk, broker, allow_short=cfg.is_perp, on_fill=self._on_fill)

        saved = self.storage.load_state()
        if saved:
            self.trader.restore(saved["trader"])
            self.last_funding_ts = saved.get("last_funding_ts")
            log.info("已恢复上次运行状态 (%s)", run)
        else:
            self.last_funding_ts = None
            if mode == "testnet":  # size breakers against the real testnet balance
                equity = broker.equity(self._last_price())
                risk.peak_equity = risk.day_start_equity = equity
        if mode == "testnet" and cfg.is_perp:
            broker.sync_position()
        self.df: pd.DataFrame | None = None

    def _make_broker(self) -> Broker:
        cfg = self.cfg
        leverage = cfg.risk.leverage if cfg.is_perp else 1.0
        if self.mode == "paper":
            return SimBroker(cfg.is_perp, cfg.initial_equity, cfg.fee_rate, cfg.slippage,
                             leverage, cfg.maintenance_margin_rate)
        from ..broker.ccxt_broker import CcxtBroker

        creds = api_credentials(cfg.exchange)
        if not {"apiKey", "secret"} <= creds.keys():
            raise RuntimeError(f"缺少测试网 API Key：请在 .env 中设置 {cfg.exchange.upper()}_API_KEY / "
                               f"{cfg.exchange.upper()}_API_SECRET")
        trade_ex = make_exchange(cfg.exchange, cfg.market, creds, testnet=True)
        return CcxtBroker(trade_ex, self.symbol, cfg.is_perp, leverage, testnet=True)

    # ---- callbacks -----------------------------------------------------
    def _on_fill(self, fill: Fill) -> None:
        pnl = f" 盈亏 {fill.pnl:+.2f}" if fill.pnl is not None else ""
        log.info("[%s] %s %.6f @ %.2f 手续费 %.4f 原因 %s%s", self.mode, fill.side, fill.qty,
                 fill.price, fill.fee, fill.reason, pnl)
        self.storage.record_fill(fill)

    def _save(self) -> None:
        self.storage.save_state({"trader": self.trader.state(), "last_funding_ts": self.last_funding_ts})

    # ---- market data ---------------------------------------------------
    def _last_price(self) -> float:
        return float(self.data_ex.fetch_ticker(self.symbol)["last"])

    def _load_history(self) -> None:
        now = int(time.time() * 1000)
        since = now - (self._history_bars() + 2) * self.step
        self.df = fetch_ohlcv_range(self.data_ex, self.symbol, self.cfg.timeframe, since, now)
        need = max(self.strategy.warmup, self.cfg.risk.atr_period) + 1
        if len(self.df) < need:
            raise RuntimeError(f"历史 K 线不足：需要 {need} 根，只拿到 {len(self.df)} 根")
        log.info("已加载 %d 根 K 线，最新收盘 %s", len(self.df), _fmt_ts(int(self.df["timestamp"].iloc[-1])))

    def _history_bars(self) -> int:
        return max(self.cfg.live.warmup_bars, self.strategy.warmup + self.cfg.risk.atr_period + 20)

    def _new_closed_bars(self) -> pd.DataFrame:
        last_ts = int(self.df["timestamp"].iloc[-1])
        new = fetch_ohlcv_range(self.data_ex, self.symbol, self.cfg.timeframe,
                                last_ts + self.step, int(time.time() * 1000))
        if not new.empty:
            keep = self._history_bars() + 50
            self.df = pd.concat([self.df, new], ignore_index=True).tail(keep).reset_index(drop=True)
        return new

    def _funding_due(self, price: float) -> float:
        """Paper perp only: total funding rate settled since the last check."""
        if self.mode != "paper" or not self.cfg.is_perp:
            return 0.0
        now = int(time.time() * 1000)
        if self.last_funding_ts is None:
            self.last_funding_ts = now
            return 0.0
        rates = fetch_funding_range(self.data_ex, self.symbol, self.last_funding_ts + 1, now)
        if rates.empty:
            return 0.0
        self.last_funding_ts = int(rates["timestamp"].iloc[-1])
        return float(rates["rate"].sum())

    # ---- main loop -----------------------------------------------------
    def _on_bar_closed(self) -> None:
        signals = self.strategy.signals(self.df)
        atr = compute_atr(self.df, self.cfg.risk.atr_period)
        bar = self.df.iloc[-1]
        ts = int(bar["timestamp"])
        signal, atr_now = int(signals.iloc[-1]), float(atr.iloc[-1])
        equity = self.trader.on_bar_close(ts, float(bar["close"]), signal, atr_now)
        self.storage.record_equity(ts, equity)
        pos = self.trader.broker.position
        log.info("K 线收盘 %s close=%.2f 信号=%+d 权益=%.2f 持仓=%s", _fmt_ts(ts), bar["close"],
                 signal, equity, f"{pos.side:+d} {pos.qty:.6f} 止损 {pos.stop:.2f}" if pos else "无")

        # the next bar has already opened: execute at the current price
        price = self._last_price()
        now = int(time.time() * 1000)
        self.trader.on_bar_open(now, price, self._funding_due(price))
        self._save()

    def run(self, max_bars: int | None = None) -> None:
        log.info("启动 %s 模式: %s %s %s 策略 %s", self.mode, self.cfg.exchange, self.symbol,
                 self.cfg.timeframe, self.strategy)
        self._load_history()
        self._on_bar_closed()  # act on the latest closed bar right away
        processed = 0
        try:
            while max_bars is None or processed < max_bars:
                time.sleep(self.cfg.live.poll_seconds)
                try:
                    price = self._last_price()
                    now = int(time.time() * 1000)
                    self.trader.check_exits(now, price, price, price)
                    if not self._new_closed_bars().empty:
                        self._on_bar_closed()
                        processed += 1
                    else:
                        self._save()
                except Exception:  # keep running through transient API errors
                    log.exception("本轮处理出错，下一轮重试")
        except KeyboardInterrupt:
            log.info("收到中断信号，保存状态后退出（持仓保持不变）")
        finally:
            self._save()
