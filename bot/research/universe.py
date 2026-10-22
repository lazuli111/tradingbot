"""Binance U 本位永续全市场日线：含已下架品种，避免幸存者偏差。

    python -m bot.research.universe fetch --root data/universe            # 下载（可重复执行，只补缺的文件）
    python -m bot.research.universe build --root data/universe            # 把月度 zip 拼成面板，存 panels.pkl
    python -m bot.research.universe summary --root data/universe          # 品种数、时间范围、已下架品种

数据来自 data.binance.vision（Binance 公开的批量历史数据站，不是交易 API）。每个品种取三类月度文件：
1d K 线、fundingRate（每次结算一行）、premiumIndexKlines 1d（永续相对指数价的溢价，即基差）。
品种清单来自 S3 目录列表，已下架的品种也在里面。月度文件只到上个月，所以面板截到最近一个完整月。

成交额为 0 的日子（下架后的“僵尸”日线、停牌）全部置空，见 drop_dead_days。

非加密资产剔除：指数合约（BTCDOM 等）、稳定币和黄金代币整列去掉；TradFi 合约（股票、商品，
2025-12-11 起上线）的名字可能和以前下架的币重名，所以只去掉 TRADFI_SINCE 之后的数据。
"""
from __future__ import annotations

import argparse
import io
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FILE_URL = "https://data.binance.vision/"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
PREFIXES = {
    "klines": "data/futures/um/monthly/klines/{s}/1d/",
    "funding": "data/futures/um/monthly/fundingRate/{s}/",
    "premium": "data/futures/um/monthly/premiumIndexKlines/{s}/1d/",
}
KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
                 "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
DAY = pd.Timedelta(days=1)

# 2026-10-22 从 exchangeInfo 取：underlyingType=INDEX，加稳定币和黄金代币，再加已下架的指数合约
NON_CRYPTO = {
    "BTCDOMUSDT", "DEFIUSDT", "ALLUSDT", "BLUEBIRDUSDT", "FOOTBALLUSDT",
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT", "USDEUSDT",
    "PAXGUSDT", "XAUTUSDT",
}
# contractType=TRADIFI_PERPETUAL 的 224 个合约（2026-10-22），最早 2025-12-11 上线
TRADFI_SINCE = pd.Timestamp("2025-12-11")
TRADFI = set("""
AAOI AAPL ACN ADBE AGPU AKAM ALAB AMAT AMC AMD AMZN ANET ANTHROPIC APLD APP ARM ASML ASTS AVGO AXTI BABA BAC BBX
BE BITO BMNR BNC BOT BRKB BSP BWET BX BYD BZ CAT CBRS CEG CIEN CL COHR COIN COPPER COST CRCL CRDO CRML CRM CRWD
CRWV CSCO CSOPSAMSUNG2L CSOPSKHYNIX2L CVNA CXMT CYPH DDOG DELL DIS DJT DKNG DRAM EBAY EWJ EWT EWY EWZ FLEX FLNC
FWDI GDX GEV GIGADEV GLW GME GOOGL GPRO GS GTLB HANMI HD HIMS HK0625 HK0700 HK0992 HK1810 HOOD HPE HUT HYUNDAI
IBKR IBM INTC INTW IONQ IREN IWM JPM KLAC KODEX200 KORU KO KSTR KUAISHOU LGELECTRONICS LITE LLY LRCX LYTE MARA
MCD MDB MDT MEITUAN META MINIMAX MOONSHOT MP MRK MRNA MRVL MSFT MSTR MSTU MU MUU MVLL NATGAS NAVER NBIS NET NFLX
NKE NOK NOW NVDA NVDL NVO OKLO ONDS OPENAI ORCL OURA PANW PATH PAYP PDD PENG PLTR POPMART PYPL QCOM QNTX QQQ RAM
RDDT RDW RIVN RKLB RUM SAMSUNGEM SAMSUNG SECZ SHAZ SHOP SKDD SKHYNIX SKHY SKUU SMCI SMH SNDK SNOW SNPS SNXX SOFI
SONY SOXL SOXS SPCX SPY SQQQ STRC STXX TBT TEAM TEM TENCENT TER TMF TQQQ TSLA TSLL TSM TTWO TWST TXN TZA UBER UNH
UNITREE URNM USAR USDBRL USDEX UVXY VKTX VRT VST V WDC WEN WMT XAG XAU XBI XLE XOM XPD XPT ZCSH ZHIPU ZHONGJI ZM ZS
""".split())
TRADFI = {f"{b}USDT" for b in TRADFI}


# ---------------------------------------------------------------- 下载

def _get(url: str, retries: int = 4) -> bytes:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - 网络错误统一重试
            if attempt == retries - 1:
                raise
            log.debug("重试 %s：%s", url, e)
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def list_bucket(prefix: str, delimiter: str | None = None) -> tuple[list[str], list[str]]:
    """S3 目录列表（分页）：返回 (文件 key, 子目录前缀)。"""
    keys, prefixes, marker = [], [], ""
    while True:
        q = {"prefix": prefix, "marker": marker}
        if delimiter:
            q["delimiter"] = delimiter
        root = ET.fromstring(_get(LIST_URL + "?" + urllib.parse.urlencode(q)))
        keys += [e.text for e in root.iter(S3_NS + "Key")]
        prefixes += [e.text for e in root.iter(S3_NS + "Prefix") if e.text != prefix]
        if root.findtext(S3_NS + "IsTruncated") != "true":
            return keys, prefixes
        marker = root.findtext(S3_NS + "NextMarker") or (keys[-1] if keys else prefixes[-1])


def list_symbols() -> list[str]:
    _, prefixes = list_bucket("data/futures/um/monthly/klines/", delimiter="/")
    names = [p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes]
    return sorted(n for n in names if re.fullmatch(r"[A-Z0-9]+USDT", n))


def month_of(key: str) -> str | None:
    m = re.search(r"(\d{4}-\d{2})\.zip$", key)
    return m.group(1) if m else None


def fetch(root: Path, symbols: list[str] | None = None, since: str = "2020-01", until: str = "2026-09",
          kinds: tuple[str, ...] = tuple(PREFIXES), workers: int = 8) -> int:
    """下载 [since, until] 月份的 zip 到 root/raw/<kind>/<symbol>/，已有的跳过。返回新下载的文件数。"""
    symbols = symbols or list_symbols()
    log.info("%d 个品种", len(symbols))

    def plan(job):
        sym, kind = job
        keys, _ = list_bucket(PREFIXES[kind].format(s=sym))
        out = []
        for k in keys:
            m = month_of(k)
            dest = root / "raw" / kind / sym / k.rsplit("/", 1)[-1]
            if m and since <= m <= until and not dest.exists():
                out.append((k, dest))
        return out

    with ThreadPoolExecutor(workers) as pool:
        todo = [t for batch in pool.map(plan, [(s, k) for s in symbols for k in kinds]) for t in batch]
    log.info("需要下载 %d 个文件", len(todo))

    def grab(item):
        key, dest = item
        data = _get(FILE_URL + key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dest)

    done = 0
    with ThreadPoolExecutor(workers) as pool:
        for _ in pool.map(grab, todo):
            done += 1
            if done % 2000 == 0:
                log.info("已下载 %d / %d", done, len(todo))
    return done


# ---------------------------------------------------------------- 解析和拼面板

def read_zip_csv(path: Path, kind: str) -> pd.DataFrame:
    """读一个月度 zip。较早的 K 线文件没有表头，较新的有。"""
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    has_header = not raw[:1].isdigit() and not raw[:1] == b"-"
    if kind == "funding":
        df = pd.read_csv(io.BytesIO(raw))
        return df.rename(columns={"calc_time": "time", "last_funding_rate": "rate"})[["time", "rate"]]
    df = pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)
    df.columns = KLINE_COLUMNS
    return df


def daily_index(ms: pd.Series) -> pd.DatetimeIndex:
    return pd.to_datetime(ms, unit="ms", utc=True).dt.tz_localize(None).dt.normalize()


def load_symbol(root: Path, sym: str) -> dict[str, pd.Series]:
    """一个品种的日线序列：open/high/low/close/quote_volume，日资金费合计 funding，基差 premium（日收盘）。"""
    out: dict[str, pd.Series] = {}
    files = sorted((root / "raw" / "klines" / sym).glob("*.zip"))
    if files:
        k = pd.concat([read_zip_csv(f, "klines") for f in files], ignore_index=True)
        k = k.drop_duplicates("open_time").sort_values("open_time")
        idx = daily_index(k["open_time"])
        for col in ("open", "high", "low", "close", "quote_volume"):
            out[col] = pd.Series(k[col].astype(float).to_numpy(), index=idx)
    files = sorted((root / "raw" / "funding" / sym).glob("*.zip"))
    if files:
        f = pd.concat([read_zip_csv(p, "funding") for p in files], ignore_index=True).dropna()
        f = f.drop_duplicates("time")
        out["funding"] = f.groupby(daily_index(f["time"]))["rate"].sum().astype(float)
    files = sorted((root / "raw" / "premium" / sym).glob("*.zip"))
    if files:
        p = pd.concat([read_zip_csv(f, "premium") for f in files], ignore_index=True)
        p = p.drop_duplicates("open_time").sort_values("open_time")
        out["premium"] = pd.Series(p["close"].astype(float).to_numpy(), index=daily_index(p["open_time"]))
    return out


FIELDS = ("open", "high", "low", "close", "quote_volume", "funding", "premium")


def build_panels(root: Path, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """拼成 {字段: 日期 × 品种} 的面板，并剔除非加密资产。缓存到 root/panels.pkl。"""
    symbols = symbols or sorted(p.name for p in (root / "raw" / "klines").iterdir() if p.is_dir())
    per = {s: load_symbol(root, s) for s in symbols}
    panels = {f: pd.DataFrame({s: d[f] for s, d in per.items() if f in d}).sort_index() for f in FIELDS}
    panels = exclude_non_crypto(drop_dead_days(panels))
    tmp = root / "panels.pkl.part"   # 先写临时文件再改名，别的进程不会读到写了一半的文件
    pd.to_pickle(panels, tmp)
    tmp.replace(root / "panels.pkl")
    return panels


def drop_dead_days(panels: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """成交额为 0 的日子所有字段置空。

    data.binance.vision 在品种下架后还会继续生成成交额为 0、价格不变的日线（例如 FTT、BTCST、FTM
    一直延续到 2026 年），停牌日也一样。不去掉的话，下架品种会被当成还在交易，回测里既不会强平，
    也可能被选进可交易池。
    """
    dead = ~(panels["quote_volume"] > 0)
    out = {}
    for f, df in panels.items():
        mask = dead.reindex(index=df.index, columns=df.columns, fill_value=True)
        out[f] = df.mask(mask)
    return out


def exclude_non_crypto(panels: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    out = {}
    for f, df in panels.items():
        df = df.drop(columns=[c for c in df.columns if c in NON_CRYPTO])
        tradfi = [c for c in df.columns if c in TRADFI]
        if tradfi:
            df = df.copy()
            df.loc[df.index >= TRADFI_SINCE, tradfi] = np.nan
        out[f] = df.dropna(axis=1, how="all")
    return out


def load_panels(root: Path) -> dict[str, pd.DataFrame]:
    return pd.read_pickle(Path(root) / "panels.pkl")


# ---------------------------------------------------------------- 按时点确定可交易池

def point_in_time_universe(close: pd.DataFrame, quote_volume: pd.DataFrame, top_n: int,
                           vol_window: int = 30, min_age_days: int = 60) -> tuple[pd.DataFrame, pd.DataFrame]:
    """每月第一天，用前一天及以前的数据选出成交额前 top_n 的品种，整月有效。

    条件：过去 vol_window 天平均成交额排名前 top_n；首根 K 线早于月初 min_age_days 天；
    前一天有收盘价（已下架的不选）。返回 (成员 bool 面板, 月初排名面板)，日期 × 品种。
    月中下架的品种仍是成员，回测引擎在它最后一天强制平仓。
    """
    first_day = close.apply(lambda s: s.first_valid_index())
    avg_vol = quote_volume.rolling(vol_window, min_periods=vol_window // 2).mean()
    member = pd.DataFrame(False, index=close.index, columns=close.columns)
    rank = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    month_starts = close.index[close.index.is_month_start]
    for m in month_starts:
        prev = m - DAY
        if prev not in close.index:
            continue
        eligible = (first_day <= m - pd.Timedelta(days=min_age_days)) & close.loc[prev].notna()
        vol = avg_vol.loc[prev].where(eligible).dropna()
        if vol.empty:
            continue
        r = vol.rank(ascending=False, method="first")
        chosen = r[r <= top_n]
        rows = (close.index >= m) & (close.index < m + pd.offsets.MonthBegin(1))
        member.loc[rows, chosen.index] = True
        rank.loc[rows, chosen.index] = chosen.to_numpy()
    return member, rank


def summary(panels: dict[str, pd.DataFrame]) -> dict:
    close = panels["close"]
    last = close.apply(lambda s: s.last_valid_index())
    end = close.index[-1]
    delisted = last[last < end - pd.Timedelta(days=7)]
    return {"symbols": close.shape[1], "start": close.index[0].date(), "end": end.date(),
            "delisted": len(delisted), "examples": sorted(delisted.index)[:15]}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Binance U 本位永续全市场日线（含已下架品种）")
    p.add_argument("command", choices=["fetch", "build", "summary"])
    p.add_argument("--root", default="data/universe")
    p.add_argument("--symbols", help="逗号分隔，默认全部")
    p.add_argument("--since", default="2020-01")
    p.add_argument("--until", default="2026-09")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(args.root)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    if args.command == "fetch":
        n = fetch(root, symbols, args.since, args.until, workers=args.workers)
        print(f"新下载 {n} 个文件")
    elif args.command == "build":
        panels = build_panels(root, symbols)
        print(summary(panels))
    else:
        print(summary(load_panels(root)))


if __name__ == "__main__":
    main()
