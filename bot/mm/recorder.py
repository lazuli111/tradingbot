"""Binance U 本位合约公开盘口录制器：买一/卖一（bookTicker）+ 逐笔成交（aggTrade）。

    python -m bot.mm.recorder select                       # 用公开 REST 挑 tick 相对价格较大的品种
    python -m bot.mm.recorder record --out data/mm         # 录制默认品种，直到 SIGTERM
    python -m bot.mm.recorder record --symbols BTCUSDT,SOLUSDT --duration-hours 0.1

只读公开行情，不需要 API Key，不下单。只依赖标准库 + aiohttp（服务器 venv 里已有），
不导入 pandas，常驻内存约 40~60 MB。

输出目录结构（时间都是毫秒 UTC，小时按本地接收时间切）：

    <out>/bookTicker/<SYMBOL>/<YYYYMMDD-HH>.csv.gz   E,T,recv,bid,bid_qty,ask,ask_qty
    <out>/aggTrade/<SYMBOL>/<YYYYMMDD-HH>.csv.gz     E,T,recv,agg_id,price,qty,buyer_maker,n_fills
    <out>/sessions.csv                              event,recv,detail（connect/alive/disconnect…）
    <out>/meta.json                                 启动时的 tickSize / stepSize

每次刷盘向 .csv.gz 追加一个新的 gzip member，进程被强杀最多丢最后一个刷盘周期，
已写入的部分仍可读。断线空档由 sessions.csv 推出来（见 read_coverage），分析时剔除。
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import os
import shutil
import signal
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType

log = logging.getLogger("mm.recorder")

WS_BASE = "wss://fstream.binance.com/stream?streams="
REST_BASE = "https://fapi.binance.com"
HOUR_MS = 3_600_000

# 主流 3 个 + tick 相对价格较大（≥1bp）、日成交额较高的山寨（2026-10-21 用 select 挑的）
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT",
    "DOGEUSDT", "NEARUSDT", "WLDUSDT", "UNIUSDT", "AVAXUSDT", "LTCUSDT",
    "ADAUSDT", "COMPUSDT", "CRVUSDT", "FETUSDT", "TRUMPUSDT",
]

HEADERS = {
    "bookTicker": "E,T,recv,bid,bid_qty,ask,ask_qty",
    "aggTrade": "E,T,recv,agg_id,price,qty,buyer_maker,n_fills",
}


def stream_url(symbols: list[str], base: str = WS_BASE) -> str:
    streams = [f"{s.lower()}@{kind}" for s in symbols for kind in ("bookTicker", "aggTrade")]
    return base + "/".join(streams)


def hour_key(ms: int) -> str:
    return time.strftime("%Y%m%d-%H", time.gmtime(ms // 1000))


def parse_message(msg: dict, recv_ms: int) -> tuple[str, str, str, tuple[str, str]] | None:
    """组合流消息 -> (类型, 品种, CSV 行, 买一卖一价)。不认识的消息返回 None。

    价格和数量原样保留交易所给的字符串，避免浮点格式化的误差和开销。
    aggTrade 的 m=true 表示买方是挂单方，也就是主动卖出（打买一）。
    """
    d = msg.get("data", msg)
    kind = d.get("e")
    if kind == "bookTicker":
        row = f"{d['E']},{d['T']},{recv_ms},{d['b']},{d['B']},{d['a']},{d['A']}"
        return kind, d["s"], row, (d["b"], d["a"])
    if kind == "aggTrade":
        n_fills = int(d["l"]) - int(d["f"]) + 1
        row = f"{d['E']},{d['T']},{recv_ms},{d['a']},{d['p']},{d['q']},{int(bool(d['m']))},{n_fills}"
        return kind, d["s"], row, ("", "")
    return None


class BookThrottle:
    """买一/卖一价格变化的行全部保留；只有挂单量变化的行，每个品种最多每 interval_ms 留一行。

    BTC 这类品种的挂单量每秒变几百次，全录 7 天要几十 GB；分析只需要价格序列和大致的挂单量。
    interval_ms=0 表示全部保留。
    """

    def __init__(self, interval_ms: int):
        self.interval_ms = interval_ms
        self._last: dict[str, tuple[str, str, int]] = {}

    def keep(self, symbol: str, bid: str, ask: str, recv_ms: int) -> bool:
        last = self._last.get(symbol)
        if (self.interval_ms <= 0 or last is None or last[0] != bid or last[1] != ask
                or recv_ms - last[2] >= self.interval_ms):
            self._last[symbol] = (bid, ask, recv_ms)
            return True
        return False


@dataclass
class HourlyGzipWriter:
    """按 (类型, 品种, 小时) 缓冲 CSV 行，flush 时各追加一个 gzip member。"""
    root: Path
    compresslevel: int = 6
    _buf: dict[tuple[str, str, int], list[str]] = field(default_factory=dict)
    rows_buffered: int = 0
    rows_written: int = 0

    def add(self, kind: str, symbol: str, recv_ms: int, row: str) -> None:
        self._buf.setdefault((kind, symbol, recv_ms // HOUR_MS), []).append(row)
        self.rows_buffered += 1

    def path(self, kind: str, symbol: str, hour: int) -> Path:
        return self.root / kind / symbol / f"{hour_key(hour * HOUR_MS)}.csv.gz"

    def flush(self) -> None:
        buf, self._buf = self._buf, {}
        for (kind, symbol, hour), rows in buf.items():
            path = self.path(kind, symbol, hour)
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with gzip.open(path, "at", compresslevel=self.compresslevel, newline="") as f:
                if new:
                    f.write(HEADERS[kind] + "\n")
                f.write("\n".join(rows) + "\n")
            self.rows_written += len(rows)
        self.rows_buffered = 0


class SessionLog:
    """sessions.csv：connect / alive / disconnect / start / stop，每行立刻落盘。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("event,recv,detail\n")

    def write(self, event: str, ts_ms: int, detail: str = "") -> None:
        detail = detail.replace(",", ";").replace("\n", " ")[:200]
        with open(self.path, "a") as f:
            f.write(f"{event},{ts_ms},{detail}\n")


def read_coverage(lines: list[str]) -> list[tuple[int, int]]:
    """由 sessions.csv 的行推出“确实在收数据”的时间段 [(start, end)]（毫秒，本地接收时间）。

    connect 开一段；alive 把段尾推到最后收到消息的时间；disconnect / stop 结束这一段。
    进程被强杀时没有 disconnect，下一次 start / connect 时把上一段截在最后一次 alive。
    空档就是这些时间段之间的部分。
    """
    out: list[tuple[int, int]] = []
    start = end = None
    for line in lines:
        parts = line.strip().split(",", 2)
        if len(parts) < 2 or parts[0] == "event" or not parts[1].isdigit():
            continue
        event, ts = parts[0], int(parts[1])
        if event == "connect":
            if start is not None and end is not None and end > start:
                out.append((start, end))
            start, end = ts, ts
        elif start is None:
            continue
        elif event == "alive":
            end = max(end, ts)
        elif event in ("disconnect", "stop", "start"):
            end = max(end, ts) if event == "disconnect" else end
            if end > start:
                out.append((start, end))
            start = end = None
    if start is not None and end is not None and end > start:
        out.append((start, end))
    return out


def rss_mb() -> float:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except OSError:
        return 0.0


@dataclass
class RecorderConfig:
    symbols: list[str]
    out: Path
    flush_seconds: float = 15.0
    qty_interval_ms: int = 1000
    idle_timeout: float = 30.0
    max_backoff: float = 60.0
    max_rss_mb: float = 150.0
    min_free_gb: float = 5.0
    duration_hours: float | None = None
    stats_minutes: float = 10.0
    ws_base: str = WS_BASE  # 测试时指向本地假服务器
    fetch_meta: bool = True


class Recorder:
    def __init__(self, cfg: RecorderConfig):
        self.cfg = cfg
        self.writer = HourlyGzipWriter(cfg.out)
        self.sessions = SessionLog(cfg.out / "sessions.csv")
        self.throttle = BookThrottle(cfg.qty_interval_ms)
        self.stop = asyncio.Event()
        self.last_msg_ms = 0
        self.connected = False
        self.counts = {"bookTicker": 0, "aggTrade": 0, "kept_book": 0}
        self.max_latency_ms = 0
        self.reconnects = 0
        self.bad_messages = 0

    def handle(self, raw: str, recv_ms: int) -> None:
        try:
            parsed = parse_message(json.loads(raw), recv_ms)
        except (ValueError, KeyError, TypeError) as e:
            self.bad_messages += 1
            if self.bad_messages <= 10:
                log.warning("无法解析的消息（%s）：%.200s", e, raw)
            return
        if parsed is None:
            return
        kind, symbol, row, (bid, ask) = parsed
        self.counts[kind] += 1
        self.last_msg_ms = recv_ms
        if kind == "bookTicker":
            if not self.throttle.keep(symbol, bid, ask, recv_ms):
                return
            self.counts["kept_book"] += 1
        self.max_latency_ms = max(self.max_latency_ms, recv_ms - int(row.split(",", 1)[0]))
        self.writer.add(kind, symbol, recv_ms, row)

    def flush(self) -> None:
        self.writer.flush()
        if self.connected and self.last_msg_ms:
            self.sessions.write("alive", self.last_msg_ms)

    def check_limits(self) -> str | None:
        rss = rss_mb()
        if rss > self.cfg.max_rss_mb:
            return f"rss {rss:.0f}MB > {self.cfg.max_rss_mb:.0f}MB"
        free_gb = shutil.disk_usage(self.cfg.out).free / 2**30
        if free_gb < self.cfg.min_free_gb:
            return f"disk free {free_gb:.1f}GB < {self.cfg.min_free_gb:.1f}GB"
        return None

    async def periodic(self) -> None:
        started = time.monotonic()
        next_stats = started + self.cfg.stats_minutes * 60
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), self.cfg.flush_seconds)
            except asyncio.TimeoutError:
                pass
            self.flush()
            reason = self.check_limits()
            if reason:
                log.error("超出资源上限，停止录制：%s", reason)
                self.sessions.write("limit", now_ms(), reason)
                self.stop.set()
            if self.cfg.duration_hours and time.monotonic() - started > self.cfg.duration_hours * 3600:
                log.info("达到 --duration-hours，停止")
                self.stop.set()
            if time.monotonic() >= next_stats:
                next_stats += self.cfg.stats_minutes * 60
                log.info("stats book=%d kept=%d trades=%d written=%d rss=%.0fMB max_latency=%dms reconnects=%d",
                         self.counts["bookTicker"], self.counts["kept_book"], self.counts["aggTrade"],
                         self.writer.rows_written, rss_mb(), self.max_latency_ms, self.reconnects)
                self.max_latency_ms = 0

    async def save_meta(self, http) -> None:
        try:
            async with http.get(REST_BASE + "/fapi/v1/exchangeInfo", timeout=30) as r:
                info = await r.json()
            meta = {}
            for s in info["symbols"]:
                if s["symbol"] in self.cfg.symbols:
                    filters = {f["filterType"]: f for f in s["filters"]}
                    meta[s["symbol"]] = {"tickSize": filters["PRICE_FILTER"]["tickSize"],
                                         "stepSize": filters["LOT_SIZE"]["stepSize"]}
            (self.cfg.out / "meta.json").write_text(json.dumps({"fetched": now_ms(), "symbols": meta}, indent=1))
        except Exception as e:  # meta 只是辅助信息，拿不到时分析会从数据里推 tick
            log.warning("exchangeInfo 获取失败：%s", e)

    async def _close_on_stop(self, ws) -> None:
        await self.stop.wait()
        await ws.close()

    async def stream(self, http) -> None:
        backoff = 1.0
        url = stream_url(self.cfg.symbols, self.cfg.ws_base)
        while not self.stop.is_set():
            detail = ""
            try:
                async with http.ws_connect(url, autoping=True, heartbeat=None, max_msg_size=2**22,
                                           timeout=aiohttp.ClientWSTimeout(ws_close=5.0)) as ws:
                    self.connected = True
                    self.sessions.write("connect", now_ms())
                    log.info("已连接，%d 个品种", len(self.cfg.symbols))
                    closer = asyncio.create_task(self._close_on_stop(ws))
                    got_first = False
                    try:
                        while True:
                            try:
                                msg = await ws.receive(timeout=self.cfg.idle_timeout)
                            except asyncio.TimeoutError:
                                detail = f"idle>{self.cfg.idle_timeout:.0f}s"
                                break
                            if msg.type == WSMsgType.TEXT:
                                self.handle(msg.data, now_ms())
                                if not got_first:
                                    got_first, backoff = True, 1.0
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                                              WSMsgType.ERROR):
                                detail = f"ws {msg.type.name} {ws.close_code}"
                                break
                    finally:
                        closer.cancel()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
                detail = f"{type(e).__name__}: {e}"
            if self.connected:
                # 段尾记最后收到消息的时间（没收到过就记连上的时间，read_coverage 会丢掉空段）
                self.writer.flush()
                self.sessions.write("disconnect", self.last_msg_ms or now_ms(), detail or "stop")
                self.connected = False
            if self.stop.is_set():
                break
            self.reconnects += 1
            log.warning("断线（%s），%.0f 秒后重连", detail, backoff)
            try:
                await asyncio.wait_for(self.stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.cfg.max_backoff)

    async def run(self) -> None:
        self.cfg.out.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        self.sessions.write("start", now_ms(), " ".join(self.cfg.symbols))
        log.info("开始录制 %s -> %s", ",".join(self.cfg.symbols), self.cfg.out)
        async with aiohttp.ClientSession() as http:
            if self.cfg.fetch_meta:
                await self.save_meta(http)
            periodic = asyncio.create_task(self.periodic())
            try:
                await self.stream(http)
            finally:
                self.stop.set()
                await periodic
                self.writer.flush()
                self.sessions.write("stop", now_ms())
                log.info("已退出，共写入 %d 行", self.writer.rows_written)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def select_symbols(min_quote_volume: float = 20e6, min_tick_bp: float = 1.0) -> list[dict]:
    """用公开 REST（exchangeInfo + 24h ticker + bookTicker）列出 USDT 永续的 tick 相对价格和成交额。"""
    def get(path: str):
        with urllib.request.urlopen(REST_BASE + path, timeout=30) as r:
            return json.load(r)

    info = get("/fapi/v1/exchangeInfo")
    t24 = {d["symbol"]: d for d in get("/fapi/v1/ticker/24hr")}
    book = {d["symbol"]: d for d in get("/fapi/v1/ticker/bookTicker")}
    rows = []
    for s in info["symbols"]:
        sym = s["symbol"]
        if (s["contractType"] != "PERPETUAL" or s["quoteAsset"] != "USDT" or s["status"] != "TRADING"
                or sym not in t24 or sym not in book):
            continue
        bid, ask = float(book[sym]["bidPrice"]), float(book[sym]["askPrice"])
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        tick = float(next(f["tickSize"] for f in s["filters"] if f["filterType"] == "PRICE_FILTER"))
        rows.append({"symbol": sym, "tick_bp": tick / mid * 1e4, "spread_bp": (ask - bid) / mid * 1e4,
                     "quote_volume_m": float(t24[sym]["quoteVolume"]) / 1e6, "trades": int(t24[sym]["count"])})
    rows.sort(key=lambda r: -r["quote_volume_m"])
    return [r for r in rows if r["quote_volume_m"] >= min_quote_volume / 1e6 and r["tick_bp"] >= min_tick_bp]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Binance U 本位合约公开盘口录制")
    sub = p.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="录制 bookTicker + aggTrade")
    rec.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="逗号分隔，如 BTCUSDT,SOLUSDT")
    rec.add_argument("--out", default="data/mm", help="输出目录")
    rec.add_argument("--flush-seconds", type=float, default=15.0)
    rec.add_argument("--qty-interval-ms", type=int, default=1000,
                     help="只有挂单量变化的 bookTicker，每个品种最多每多少毫秒留一行（0=全留）")
    rec.add_argument("--max-rss-mb", type=float, default=150.0, help="常驻内存超过就停止")
    rec.add_argument("--min-free-gb", type=float, default=5.0, help="磁盘剩余低于就停止")
    rec.add_argument("--duration-hours", type=float, help="录多久后自动退出（默认一直录）")
    sel = sub.add_parser("select", help="列出 tick 相对价格较大、成交额较高的品种")
    sel.add_argument("--min-volume", type=float, default=20e6, help="24h 成交额下限（USDT）")
    sel.add_argument("--min-tick-bp", type=float, default=1.0)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "select":
        print(f"{'symbol':<16}{'tick_bp':>9}{'spread_bp':>11}{'vol_M':>10}{'trades':>10}")
        for r in select_symbols(args.min_volume, args.min_tick_bp):
            print(f"{r['symbol']:<16}{r['tick_bp']:>9.2f}{r['spread_bp']:>11.2f}"
                  f"{r['quote_volume_m']:>10.0f}{r['trades']:>10}")
        return
    cfg = RecorderConfig(symbols=[s.strip().upper() for s in args.symbols.split(",") if s.strip()],
                         out=Path(args.out).expanduser(), flush_seconds=args.flush_seconds,
                         qty_interval_ms=args.qty_interval_ms, max_rss_mb=args.max_rss_mb,
                         min_free_gb=args.min_free_gb, duration_hours=args.duration_hours)
    asyncio.run(Recorder(cfg).run())


if __name__ == "__main__":
    main()
