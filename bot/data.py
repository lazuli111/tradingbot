"""Market data: exchange construction, paginated OHLCV / funding-rate fetching, CSV cache."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

from .config import Config

log = logging.getLogger(__name__)

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def make_exchange(exchange_id: str, market: str, credentials: dict | None = None,
                  testnet: bool = False) -> ccxt.Exchange:
    if not hasattr(ccxt, exchange_id):
        raise ValueError(f"ccxt 不支持交易所 {exchange_id!r}")
    if market == "spot":
        default_type = "spot"
    else:
        default_type = "future" if exchange_id.startswith("binance") else "swap"
    params = {"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": default_type,
                                                                     # 本机时钟比交易所快 1 秒以上时，
                                                                     # 签名请求会被拒（Binance -1021）；
                                                                     # 打开后 load_markets 会先校准时间差
                                                                     "adjustForTimeDifference": True}}
    params.update(credentials or {})
    ex = getattr(ccxt, exchange_id)(params)
    if testnet:
        enable_testnet(ex)
    return ex


def enable_testnet(ex: ccxt.Exchange) -> None:
    # Binance retired the futures sandbox in favour of "demo trading"; other exchanges use sandbox mode.
    if ex.id.startswith("binance") and hasattr(ex, "enable_demo_trading"):
        ex.enable_demo_trading(True)
    else:
        ex.set_sandbox_mode(True)


def timeframe_ms(timeframe: str) -> int:
    return ccxt.Exchange.parse_timeframe(timeframe) * 1000


def to_ms(value) -> int:
    return int(pd.Timestamp(value, tz="UTC").timestamp() * 1000) if not isinstance(value, int) else value


def _call_with_retry(fn, *args, retries: int = 3, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.RateLimitExceeded) as e:
            if attempt == retries - 1:
                raise
            log.warning("请求失败 (%s)，%ds 后重试", e.__class__.__name__, 2 ** attempt)
            time.sleep(2 ** attempt)


def fetch_ohlcv_range(ex: ccxt.Exchange, symbol: str, timeframe: str,
                      since_ms: int, until_ms: int) -> pd.DataFrame:
    """Fetch closed candles with timestamp in [since_ms, until_ms)."""
    step = timeframe_ms(timeframe)
    until_ms = min(until_ms, (int(time.time() * 1000) // step) * step)  # drop the still-open bar
    rows: list[list] = []
    cursor = since_ms
    while cursor < until_ms:
        batch = _call_with_retry(ex.fetch_ohlcv, symbol, timeframe, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        last = batch[-1][0]
        if last < cursor:
            break
        cursor = last + step
    df = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
    df = df.drop_duplicates("timestamp").sort_values("timestamp")
    df = df[(df["timestamp"] >= since_ms) & (df["timestamp"] < until_ms)]
    return df.reset_index(drop=True).astype({"timestamp": "int64"})


def fetch_funding_range(ex: ccxt.Exchange, symbol: str, since_ms: int, until_ms: int) -> pd.DataFrame:
    rows: list[dict] = []
    cursor = since_ms
    while cursor < until_ms:
        batch = _call_with_retry(ex.fetch_funding_rate_history, symbol, since=cursor, limit=100)
        if not batch:
            break
        rows.extend({"timestamp": b["timestamp"], "rate": b["fundingRate"]} for b in batch)
        last = batch[-1]["timestamp"]
        if last < cursor:
            break
        cursor = last + 1
    df = pd.DataFrame(rows, columns=["timestamp", "rate"])
    df = df.drop_duplicates("timestamp").sort_values("timestamp")
    df = df[(df["timestamp"] >= since_ms) & (df["timestamp"] < until_ms)]
    return df.reset_index(drop=True).astype({"timestamp": "int64", "rate": "float64"})


def _cache_path(cfg: Config, kind: str) -> Path:
    safe = cfg.market_symbol.replace("/", "-").replace(":", "_")
    suffix = f"_{cfg.timeframe}" if kind == "ohlcv" else ""
    return Path(cfg.data_dir) / f"{cfg.exchange}_{cfg.market}_{safe}_{kind}{suffix}.csv"


def _cached_fetch(path: Path, fetch, since_ms: int, until_ms: int, step: int) -> pd.DataFrame:
    """Reuse cached rows and only fetch the parts of [since, until) the cache doesn't cover."""
    cached = pd.read_csv(path) if path.exists() else pd.DataFrame()
    parts = [cached]
    if cached.empty:
        parts.append(fetch(since_ms, until_ms))
    else:
        first, last = int(cached["timestamp"].iloc[0]), int(cached["timestamp"].iloc[-1])
        if since_ms < first:
            parts.append(fetch(since_ms, first))
        if until_ms > last + step:
            parts.append(fetch(last + 1, until_ms))
    merged = pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(
        not p.empty for p in parts) else cached
    if not merged.empty:
        merged = merged.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(path, index=False)
        merged = merged[(merged["timestamp"] >= since_ms) & (merged["timestamp"] < until_ms)]
    return merged.reset_index(drop=True)


def load_ohlcv(cfg: Config, since, until=None, ex: ccxt.Exchange | None = None) -> pd.DataFrame:
    ex = ex or make_exchange(cfg.exchange, cfg.market)
    since_ms = to_ms(since)
    until_ms = to_ms(until) if until is not None else int(time.time() * 1000)
    step = timeframe_ms(cfg.timeframe)
    df = _cached_fetch(_cache_path(cfg, "ohlcv"),
                       lambda s, u: fetch_ohlcv_range(ex, cfg.market_symbol, cfg.timeframe, s, u),
                       since_ms, until_ms, step)
    if df.empty:
        raise RuntimeError(f"没有获取到 {cfg.market_symbol} {cfg.timeframe} 的 K 线数据")
    return df


def load_funding(cfg: Config, since, until=None, ex: ccxt.Exchange | None = None) -> pd.DataFrame:
    ex = ex or make_exchange(cfg.exchange, cfg.market)
    since_ms = to_ms(since)
    until_ms = to_ms(until) if until is not None else int(time.time() * 1000)
    df = _cached_fetch(_cache_path(cfg, "funding"),
                       lambda s, u: fetch_funding_range(ex, cfg.market_symbol, s, u),
                       since_ms, until_ms, 8 * 3600 * 1000)
    if df.empty:
        log.warning("没有获取到资金费率历史，回测将不计资金费")
    elif int(df["timestamp"].iloc[0]) > since_ms + 24 * 3600 * 1000:
        start = pd.Timestamp(int(df["timestamp"].iloc[0]), unit="ms", tz="UTC")
        log.warning("交易所只提供 %s 之后的资金费率，此前的区间不计资金费", start.date())
    return df
