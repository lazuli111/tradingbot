"""Configuration loading and validation."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

MARKETS = ("spot", "perp")


@dataclass
class FeeConfig:
    spot: float = 0.001
    perp: float = 0.0005


@dataclass
class StrategyConfig:
    name: str = "ma_cross"
    params: dict = field(default_factory=dict)


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01
    atr_period: int = 14
    stop_atr_mult: float = 2.0
    take_profit_atr_mult: float | None = None
    leverage: float = 1.0
    max_leverage: float = 3.0
    max_position_pct: float = 1.0
    daily_loss_limit: float = 0.03
    max_drawdown: float = 0.20


@dataclass
class LiveConfig:
    poll_seconds: int = 30
    warmup_bars: int = 300
    db_path: str = "data/trades.db"


@dataclass
class Config:
    exchange: str = "binance"
    market: str = "spot"
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    initial_equity: float = 10000.0
    slippage: float = 0.0005
    maintenance_margin_rate: float = 0.004
    fees: FeeConfig = field(default_factory=FeeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    data_dir: str = "data"

    @property
    def is_perp(self) -> bool:
        return self.market == "perp"

    @property
    def fee_rate(self) -> float:
        return self.fees.perp if self.is_perp else self.fees.spot

    @property
    def market_symbol(self) -> str:
        """ccxt symbol: 'BTC/USDT' for spot, 'BTC/USDT:USDT' for a linear perpetual."""
        base_symbol = self.symbol.split(":")[0]
        if not self.is_perp:
            return base_symbol
        quote = base_symbol.split("/")[1]
        return f"{base_symbol}:{quote}"

    def validate(self) -> None:
        r = self.risk
        if self.market not in MARKETS:
            raise ValueError(f"market 必须是 {MARKETS} 之一，当前为 {self.market!r}")
        if "/" not in self.symbol:
            raise ValueError(f"symbol 格式应为 BASE/QUOTE，当前为 {self.symbol!r}")
        if self.initial_equity <= 0:
            raise ValueError("initial_equity 必须大于 0")
        if not 0 < r.risk_per_trade <= 0.1:
            raise ValueError("risk_per_trade 应在 (0, 0.1] 之间")
        if r.stop_atr_mult <= 0:
            raise ValueError("stop_atr_mult 必须大于 0（强制止损）")
        if not 0 < r.max_drawdown < 1 or not 0 < r.daily_loss_limit < 1:
            raise ValueError("max_drawdown / daily_loss_limit 应在 (0, 1) 之间")
        if self.is_perp:
            if not 1 <= r.leverage <= r.max_leverage:
                raise ValueError(f"leverage={r.leverage} 超出允许范围 [1, {r.max_leverage}]")
        else:
            if r.leverage != 1:
                raise ValueError("现货不支持杠杆，leverage 必须为 1")
            if not 0 < r.max_position_pct <= 1:
                raise ValueError("现货 max_position_pct 应在 (0, 1] 之间")


def _build(cls, data: dict | None):
    data = data or {}
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"{cls.__name__} 中有未知配置项: {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        default = known[name].default_factory() if callable(known[name].default_factory) else None
        kwargs[name] = _build(type(default), value) if is_dataclass(default) else value
    return cls(**kwargs)


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Config:
    """Load YAML config (falls back to config.example.yaml), apply top-level overrides, validate."""
    load_dotenv()
    if path is None:
        path = "config.yaml" if Path("config.yaml").exists() else "config.example.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key == "strategy_name":
            data.setdefault("strategy", {})["name"] = value
            data["strategy"]["params"] = {}  # params of the old strategy don't apply
        elif key == "leverage":
            data.setdefault("risk", {})["leverage"] = value
        else:
            data[key] = value
    cfg = _build(Config, data)
    cfg.validate()
    return cfg


def api_credentials(exchange_id: str) -> dict:
    prefix = exchange_id.upper()
    creds = {
        "apiKey": os.getenv(f"{prefix}_API_KEY"),
        "secret": os.getenv(f"{prefix}_API_SECRET"),
        "password": os.getenv(f"{prefix}_API_PASSWORD"),
    }
    return {k: v for k, v in creds.items() if v}
