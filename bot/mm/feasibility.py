"""盘口做市可行性分析：读 recorder 录下的 bookTicker + aggTrade，逐品种估算朴素做市的期望。

口径（全部用交易所成交时间 T，单位 bp）：

- 报价价差：按持续时间加权的 (ask - bid)，以 tick 为单位精确统计，再按平均 tick_bp 折成 bp。
- 主动成交：aggTrade 的 m=1 是主动卖（打买一），m=0 是主动买（吃卖一）。taker 方向 side=+1 买 / -1 卖。
- 成交前报价：严格早于成交时间的最后一笔 bookTicker，中间价 m0。
- 碰到买一/卖一的成交（touch）：主动卖的价格 == 成交前买一，或主动买 == 成交前卖一。
  这是挂在最优价上的小单可能拿到的成交。扫多档的大单只有第一笔算 touch。
- 清掉整档的成交（cleared）：touch 且成交后（同一毫秒内的更新也算）最优价被打穿。排在队尾的挂单
  只有在整档被吃光时才成交，所以这是悲观口径；touch 是乐观口径（假设总排在队首）。
- 被动方 markout（h 秒）：side * (p - m_h) / m0 = 半价差 side*(p-m0)/m0 − 逆向选择 side*(m_h-m0)/m0。
  价格朝 taker 方向走，挂单方就亏。
- 朴素做市每个来回（买一成交一次 + 卖一成交一次）：
  2 × 平均被动 markout − 2 × maker 费率 = 成交时价差 − 2 × 逆向选择 − 2 × maker 费。
- 断线空档：由 sessions.csv 推出有效时间段，两端各缩 gap_margin_ms；查报价、算 markout 都不跨段，
  落在空档里或 t+h 越过段尾的成交整笔剔除（所有 horizon 用同一批成交）。
- 标准误：按小时分块的聚类标准误（相邻成交的 markout 时间上重叠，逐笔算会低估误差）。
"""
from __future__ import annotations

import calendar
import io
import json
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from bot.mm.recorder import HOUR_MS, read_coverage

MAX_TICKS = 5000  # 价差直方图的上限（tick），更宽的并入最后一格


@dataclass
class Params:
    maker_fee: float = 0.0002          # 0.02%，按当前官方普通费率改
    horizons: tuple[int, ...] = (1, 5, 30, 60)  # 秒
    verdict_horizon: int = 30
    gap_margin_ms: int = 1000
    chunk_hours: int = 24
    since_ms: int | None = None        # 只分析这个时间段（毫秒 UTC），比如剔除网络被限速的时段
    until_ms: int | None = None

    @property
    def fee_bp(self) -> float:
        return self.maker_fee * 1e4


# ---------------------------------------------------------------- 读文件

def gunzip_tolerant(raw: bytes) -> bytes:
    """解压多 member 的 gzip；最后一个 member 被截断（进程被强杀）时保留已解出的部分。

    分 1MB 喂给 zlib，避免对每个 member 切片拷贝剩余数据（一小时的文件有几百个 member）。
    """
    out, pos, n, mv = [], 0, len(raw), memoryview(raw)
    while pos < n:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            while not d.eof and pos < n:
                end = min(pos + (1 << 20), n)
                out.append(d.decompress(mv[pos:end]))
                pos = end
        except zlib.error:
            break
        if not d.eof:
            break
        pos -= len(d.unused_data)
    return b"".join(out)


def read_gz_csv(path: Path, usecols: list[str] | None = None) -> pd.DataFrame:
    """读 recorder 写的 .csv.gz，只保留完整的行。"""
    data = gunzip_tolerant(Path(path).read_bytes())
    data = data[: data.rfind(b"\n") + 1]
    if data.count(b"\n") < 2:
        return pd.DataFrame(columns=usecols or [])
    return pd.read_csv(io.BytesIO(data), usecols=usecols)


def hour_index(key: str) -> int:
    return calendar.timegm(time.strptime(key, "%Y%m%d-%H")) * 1000 // HOUR_MS


def hour_files(root: Path, kind: str, symbol: str) -> dict[int, Path]:
    d = root / kind / symbol
    return {hour_index(p.name[:11]): p for p in sorted(d.glob("*.csv.gz"))} if d.exists() else {}


def load_coverage(root: Path, margin_ms: int, since_ms: int | None = None,
                  until_ms: int | None = None) -> np.ndarray:
    """有效时间段 [[start, end], ...]（毫秒），两端各缩 margin_ms，再截到 [since_ms, until_ms]。"""
    path = root / "sessions.csv"
    if not path.exists():
        return np.empty((0, 2), dtype=np.int64)
    lo, hi = since_ms if since_ms is not None else -2**62, until_ms if until_ms is not None else 2**62
    segs = [(max(s + margin_ms, lo), min(e - margin_ms, hi))
            for s, e in read_coverage(path.read_text().splitlines())]
    return np.array([s for s in segs if s[1] > s[0]], dtype=np.int64).reshape(-1, 2)


def segment_of(ts: np.ndarray, segs: np.ndarray) -> np.ndarray:
    """每个时间戳属于第几段，不在任何段内为 -1。"""
    if len(segs) == 0:
        return np.full(len(ts), -1, dtype=np.int64)
    i = np.searchsorted(segs[:, 0], ts, side="right") - 1
    ok = (i >= 0) & (ts <= segs[np.clip(i, 0, None), 1])
    return np.where(ok, i, -1)


def load_tick(root: Path, symbol: str, book: pd.DataFrame | None = None) -> float:
    meta = root / "meta.json"
    if meta.exists():
        info = json.loads(meta.read_text()).get("symbols", {}).get(symbol)
        if info:
            return float(info["tickSize"])
    if book is not None and len(book):  # 没有 meta 时从买一价的最小变动推
        diffs = np.diff(np.unique(book["bid"].to_numpy()))
        diffs = diffs[diffs > 0]
        if len(diffs):
            return float(np.round(diffs.min(), 12))
    raise ValueError(f"{symbol}: 没有 meta.json，也推不出 tick")


# ---------------------------------------------------------------- 纯计算（测试覆盖这里）

def book_durations(bt: np.ndarray, seg_b: np.ndarray, segs: np.ndarray) -> np.ndarray:
    """每行报价持续多久（毫秒）：到同段下一行为止，段内最后一行到段尾；不在段内为 0。"""
    dur = np.zeros(len(bt), dtype=np.float64)
    if len(bt) == 0:
        return dur
    nxt = np.r_[bt[1:], 0]
    same = np.r_[seg_b[1:] == seg_b[:-1], False]
    inside = seg_b >= 0
    seg_end = segs[np.clip(seg_b, 0, None), 1] if len(segs) else np.zeros(len(bt))
    dur = np.where(same, nxt - bt, seg_end - bt).astype(np.float64)
    return np.where(inside, np.clip(dur, 0, None), 0.0)


def trade_metrics(book: pd.DataFrame, trades: pd.DataFrame, segs: np.ndarray,
                  horizons_s: tuple[int, ...], data_end: int | None = None) -> pd.DataFrame:
    """逐笔主动成交的被动方指标（bp）。只返回所有 horizon 都有效的成交。

    book: T, bid, ask（按 T 升序）；trades: T, price, qty, buyer_maker（按 T 升序）。
    data_end: 传入的 book 只覆盖到这个时间（分块读取时），t+h 超过它的成交剔除。
    """
    bt = book["T"].to_numpy(np.int64)
    bid = book["bid"].to_numpy(np.float64)
    ask = book["ask"].to_numpy(np.float64)
    mid = (bid + ask) / 2
    seg_b = segment_of(bt, segs)
    tt = trades["T"].to_numpy(np.int64)
    price = trades["price"].to_numpy(np.float64)
    side = np.where(trades["buyer_maker"].to_numpy() == 1, -1.0, 1.0)
    seg_t = segment_of(tt, segs)
    n = len(tt)
    if n == 0 or len(bt) == 0:
        return pd.DataFrame()

    def asof(ts: np.ndarray, strict: bool) -> tuple[np.ndarray, np.ndarray]:
        j = np.searchsorted(bt, ts, side="left" if strict else "right") - 1
        ok = (j >= 0) & (seg_b[np.clip(j, 0, None)] == seg_t) & (seg_t >= 0)
        return np.clip(j, 0, None), ok

    pre, ok = asof(tt, strict=True)
    post, ok_post = asof(tt, strict=False)
    ok &= ok_post
    max_h = max(horizons_s) * 1000
    seg_end = np.where(seg_t >= 0, segs[np.clip(seg_t, 0, None), 1] if len(segs) else 0, 0)
    ok &= tt + max_h <= seg_end
    if data_end is not None:
        ok &= tt + max_h <= data_end
    m0 = mid[pre]
    ok &= ask[pre] > bid[pre]  # 交叉 / 锁死的报价不算

    is_touch = ((side < 0) & (price == bid[pre])) | ((side > 0) & (price == ask[pre]))
    cleared = is_touch & (((side < 0) & (bid[post] < price)) | ((side > 0) & (ask[post] > price)))
    out = {
        "T": tt, "side": side, "price": price,
        "notional": price * trades["qty"].to_numpy(np.float64),
        "touch": is_touch, "cleared": cleared,
        "spread_bp": (ask[pre] - bid[pre]) / m0 * 1e4,
        "half_spread_bp": side * (price - m0) / m0 * 1e4,
    }
    for h in horizons_s:
        j, ok_h = asof(tt + h * 1000, strict=False)
        ok &= ok_h
        out[f"adverse_{h}s"] = side * (mid[j] - m0) / m0 * 1e4
    df = pd.DataFrame(out)[ok]
    for h in horizons_s:
        df[f"markout_{h}s"] = df["half_spread_bp"] - df[f"adverse_{h}s"]
    return df.reset_index(drop=True)


def round_trip_bp(mean_markout_bp: float, fee_bp: float) -> float:
    """一个来回 = 买一成交一次 + 卖一成交一次：2 × 每笔被动 markout − 2 × maker 费。"""
    return 2 * mean_markout_bp - 2 * fee_bp


def cluster_mean_se(sums: np.ndarray, counts: np.ndarray) -> tuple[float, float]:
    """由每小时的 Σx 和笔数算总体均值 + 按小时聚类的标准误。

    相邻成交的 markout 时间上重叠、彼此相关，逐笔当独立样本会低估误差；按小时分块更保守。
    """
    n = counts.sum()
    if n == 0:
        return float("nan"), float("nan")
    mean = float(sums.sum() / n)
    k = int((counts > 0).sum())
    if k < 2:
        return mean, float("nan")
    g = sums - counts * mean
    return mean, float(np.sqrt((g ** 2).sum() * k / (k - 1)) / n)


# ---------------------------------------------------------------- 逐品种累加

@dataclass
class SymbolAccumulator:
    symbol: str
    tick: float
    horizons: tuple[int, ...]
    spread_ticks_w: np.ndarray = field(default_factory=lambda: np.zeros(MAX_TICKS + 1))
    tick_bp_w: float = 0.0           # Σ tick_bp × 持续时间
    depth_w: np.ndarray = field(default_factory=lambda: np.zeros(2))  # Σ 买一/卖一挂单额 × 持续时间
    covered_ms: float = 0.0
    hourly: list[pd.DataFrame] = field(default_factory=list)  # 每小时、每种口径的笔数和各指标之和
    n_trades_all: int = 0
    latency_ms: list[float] = field(default_factory=list)

    def add_book(self, book: pd.DataFrame, dur: np.ndarray) -> None:
        bid, ask = book["bid"].to_numpy(), book["ask"].to_numpy()
        ok = (dur > 0) & (ask > bid)
        d = dur[ok]
        ticks = np.clip(np.rint((ask[ok] - bid[ok]) / self.tick).astype(np.int64), 0, MAX_TICKS)
        self.spread_ticks_w += np.bincount(ticks, weights=d, minlength=MAX_TICKS + 1)
        mid = (bid[ok] + ask[ok]) / 2
        self.tick_bp_w += float((self.tick / mid * 1e4 * d).sum())
        self.depth_w += [float((book["bid_qty"].to_numpy()[ok] * bid[ok] * d).sum()),
                         float((book["ask_qty"].to_numpy()[ok] * ask[ok] * d).sum())]
        self.covered_ms += float(d.sum())

    def add_trades(self, metrics: pd.DataFrame, n_all: int) -> None:
        """逐笔指标当场按小时汇总，不保留逐笔数据（BTC 一周几千万笔）。"""
        self.n_trades_all += n_all
        if not len(metrics):
            return
        cols = ["notional", "spread_bp", "half_spread_bp"]
        cols += [f"{p}_{h}s" for h in self.horizons for p in ("adverse", "markout")]
        m = metrics.assign(hour=metrics["T"] // HOUR_MS, n=1, n_buy=(metrics["side"] > 0).astype(int))
        for name, mask in (("all", np.ones(len(m), bool)), ("touch", m["touch"].to_numpy(bool)),
                           ("cleared", m["cleared"].to_numpy(bool))):
            g = m[mask].groupby("hour")[["n", "n_buy", *cols]].sum().reset_index()
            self.hourly.append(g.assign(kind=name))

    def summary(self, params: Params) -> dict:
        hours = self.covered_ms / HOUR_MS
        w = self.spread_ticks_w
        total = w.sum()
        tick_bp = self.tick_bp_w / total if total else float("nan")
        cum = np.cumsum(w)
        q = {p: int(np.searchsorted(cum, p * total)) if total else 0 for p in (0.1, 0.25, 0.5, 0.75, 0.9)}
        hourly = (pd.concat(self.hourly, ignore_index=True).groupby(["kind", "hour"]).sum()
                  if self.hourly else pd.DataFrame())

        def part(kind: str) -> pd.DataFrame:
            return hourly.loc[kind] if len(hourly) and kind in hourly.index.get_level_values(0) else pd.DataFrame(
                {c: [] for c in ("n", "n_buy", "notional", "spread_bp")})

        def per_h(x: float) -> float:
            return x / hours if hours else float("nan")

        everything, touch, cleared = part("all"), part("touch"), part("cleared")
        n_valid, n_touch = everything["n"].sum(), touch["n"].sum()
        n_touch_buy = touch["n_buy"].sum()
        out = {
            "symbol": self.symbol,
            "hours": hours,
            "tick_bp": tick_bp,
            "spread_p10_ticks": q[0.1], "spread_p25_ticks": q[0.25], "spread_med_ticks": q[0.5],
            "spread_p75_ticks": q[0.75], "spread_p90_ticks": q[0.9],
            "spread_med_bp": q[0.5] * tick_bp, "spread_p90_bp": q[0.9] * tick_bp,
            "one_tick_time": w[1] / total if total else float("nan"),
            "depth_bid_usd": self.depth_w[0] / total if total else float("nan"),
            "depth_ask_usd": self.depth_w[1] / total if total else float("nan"),
            "trades_per_h": per_h(self.n_trades_all),
            "valid_trades": int(n_valid),
            "touch_share": n_touch / n_valid if n_valid else float("nan"),
            "bid_hits_per_h": per_h(n_touch - n_touch_buy),
            "ask_lifts_per_h": per_h(n_touch_buy),
            "cleared_per_h": per_h(cleared["n"].sum()),
            "touch_usd_per_h": per_h(touch["notional"].sum()),
            # 最优价上的成交额 / 买一+卖一挂单额：每小时把最优价那一档换手几遍，越大排到队首越快
            "queue_turnover_per_h": (per_h(touch["notional"].sum()) / (self.depth_w.sum() / total)
                                     if total and self.depth_w.sum() else float("nan")),
            "fill_spread_bp": touch["spread_bp"].sum() / n_touch if n_touch else float("nan"),
        }
        for name, sub in (("touch", touch), ("cleared", cleared)):
            counts = sub["n"].to_numpy(np.float64)
            for h in self.horizons:
                if len(sub):
                    adv, _ = cluster_mean_se(sub[f"adverse_{h}s"].to_numpy(), counts)
                    mk, se = cluster_mean_se(sub[f"markout_{h}s"].to_numpy(), counts)
                else:
                    adv = mk = se = float("nan")
                out[f"{name}_adverse_{h}s"] = adv
                out[f"{name}_markout_{h}s"] = mk
                out[f"{name}_rt_{h}s"] = round_trip_bp(mk, params.fee_bp)
                out[f"{name}_rt_se_{h}s"] = 2 * se
        h = params.verdict_horizon
        out["breakeven_fee_bp"] = out[f"touch_markout_{h}s"]  # 每笔 markout 等于费率时一个来回打平
        out["verdict"] = verdict(out[f"touch_rt_{h}s"], out[f"touch_rt_se_{h}s"], out[f"cleared_rt_{h}s"])
        out["latency_p50_ms"] = float(np.median(self.latency_ms)) if self.latency_ms else float("nan")
        return out


def verdict(rt_touch: float, se_touch: float, rt_cleared: float) -> str:
    if not np.isfinite(rt_touch):
        return "数据不足"
    if rt_touch <= 0:
        return "负"
    if np.isfinite(se_touch) and rt_touch - 2 * se_touch <= 0:
        return "不显著"
    if np.isfinite(rt_cleared) and rt_cleared > 0:
        return "正（悲观口径也为正）"
    return "仅乐观口径为正"


def analyze_symbol(root: Path, symbol: str, params: Params, log=print) -> dict | None:
    books = hour_files(root, "bookTicker", symbol)
    trades = hour_files(root, "aggTrade", symbol)
    if not books:
        return None
    segs = load_coverage(root, params.gap_margin_ms, params.since_ms, params.until_ms)
    if len(segs) == 0:
        log(f"{symbol}: 没有 sessions.csv 或有效时间段，跳过")
        return None
    hours = sorted(books)
    acc = None
    book_cols = ["T", "recv", "bid", "bid_qty", "ask", "ask_qty"]
    trade_cols = ["T", "recv", "price", "qty", "buyer_maker"]
    for c0 in range(0, len(hours), params.chunk_hours):
        own = hours[c0:c0 + params.chunk_hours]
        # 前后各多读一小时：成交前报价要往前看，markout 要往后看最多 max(horizons) 秒
        span = [h for h in (own[0] - 1, *own, own[-1] + 1) if h in books]
        bframes = []
        for h in span:
            df = read_gz_csv(books[h], book_cols)
            df["own"] = h in own
            bframes.append(df)
        book = pd.concat(bframes, ignore_index=True).sort_values("T", kind="stable").reset_index(drop=True)
        if acc is None:
            acc = SymbolAccumulator(symbol, load_tick(root, symbol, book), params.horizons)
        seg_b = segment_of(book["T"].to_numpy(np.int64), segs)
        dur = book_durations(book["T"].to_numpy(np.int64), seg_b, segs)
        own_mask = book["own"].to_numpy(bool)
        acc.add_book(book[own_mask].reset_index(drop=True), dur[own_mask])
        tframes = [read_gz_csv(trades[h], trade_cols) for h in own if h in trades]
        tr = (pd.concat(tframes, ignore_index=True).sort_values("T", kind="stable").reset_index(drop=True)
              if tframes else pd.DataFrame(columns=trade_cols))
        n_in_cov = int((segment_of(tr["T"].to_numpy(np.int64), segs) >= 0).sum()) if len(tr) else 0
        # 后面那一小时读进来了就能看到它的结尾；没有后一小时说明数据到此为止，段尾会截住
        data_end = (own[-1] + 2) * HOUR_MS - 60_000 if own[-1] + 1 in books else None
        acc.add_trades(trade_metrics(book, tr, segs, params.horizons, data_end), n_in_cov)
        if len(tr):
            acc.latency_ms.append(float((tr["recv"] - tr["T"]).median()))
        log(f"{symbol}: {len(own)} 小时块完成（{c0 + len(own)}/{len(hours)}）")
    return acc.summary(params) if acc else None


def analyze(root: Path, symbols: list[str] | None, params: Params, log=print) -> pd.DataFrame:
    root = Path(root)
    if not symbols:
        symbols = sorted(p.name for p in (root / "bookTicker").iterdir() if p.is_dir())
    rows = [r for s in symbols if (r := analyze_symbol(root, s, params, log))]
    return pd.DataFrame(rows)


def coverage_report(root: Path, margin_ms: int = 0, since_ms: int | None = None,
                    until_ms: int | None = None) -> dict:
    segs = load_coverage(Path(root), margin_ms, since_ms, until_ms)
    if len(segs) == 0:
        return {"segments": 0}
    total = segs[-1, 1] - segs[0, 0]
    covered = int((segs[:, 1] - segs[:, 0]).sum())
    gaps = segs[1:, 0] - segs[:-1, 1]
    return {"segments": len(segs), "start": int(segs[0, 0]), "end": int(segs[-1, 1]),
            "span_h": total / HOUR_MS, "covered_h": covered / HOUR_MS,
            "gap_s_total": float(gaps.sum() / 1000) if len(gaps) else 0.0,
            "gap_s_max": float(gaps.max() / 1000) if len(gaps) else 0.0}
